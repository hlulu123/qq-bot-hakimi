#!/usr/bin/env python3
"""Persistent per-chat verbatim history archive & self-service lookup engine.

Enables the QQ bot to browse and search its own chat history whenever:
- Earlier messages scrolled beyond the recent injected window (`GROUP_HISTORY_COUNT`),
- The ACP context was compressed (`/compact` / `auto_compress`) and exact wording was lost,
- The session reset after idle timeout or bot restart,
- Messages were skipped by ambient pacing or arrived while the bot was offline,
- A user replies to or references an older message ID.

Data sources (combined transparently, strictly scoped to the CURRENT chat only):
1. Local SQLite WAL archive (`data/chat_history.db`):
   - Recorded in real time by `onebot-ws-guard.py` (all inbound group/DM messages
     and outbound bot replies) via a non-blocking background writer queue.
   - Enriched continuously by `agy-acp-wrapper.py` when fetching recent NapCat history.
2. Live NapCat OneBot v11 HTTP API:
   - Paginated backward fetch via `get_group_msg_history` / `get_friend_msg_history`
     using `message_seq` (automatically cached into SQLite for fast subsequent queries).
   - Direct single-message resolution via `get_msg` with strict chat ownership verification.
3. `cc-connect` session archive fallback (`data/sessions/*.json`):
   - Strictly restricted to sessions owned by `qq:g:<group_id>` or `qq:<dm_user_id>`,
     allowing lookup of pre-archive / pre-compression / pre-idle-reset history immediately.

Security & privacy guarantees:
- Strict chat isolation: every query is hard-bound to the verified `(is_group, group_id,
  dm_user_id)` of the current turn. Any `group_id`/`chat_id` passed by the model is ignored.
- Group chats can NEVER read DM history or another group's history; DMs can NEVER read
  other users' DMs or group chats.
- All returned history text is scrubbed of fake system tags and `[[QQ:...]]` directives
  to prevent stored prompt injection from old chat messages.

Python 3.9 standard library only.
"""

import datetime
import json
import os
import queue
import re
import sqlite3
import sys
import threading
import time

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

import bot_env  # noqa: E402,F401  (loads bot.env before settings are read)
import onebot_common  # noqa: E402

try:
    import bot_plugins  # noqa: E402
except Exception:
    bot_plugins = None


def _plugin_enabled(pid, default=True):
    """Fail-open hot-reloaded plugin toggle query."""
    if bot_plugins is None:
        return default
    try:
        return bool(bot_plugins.is_enabled(pid))
    except Exception:
        return default


def _plugin_setting(pid, key, default=None):
    """Fail-open hot-reloaded plugin setting query."""
    if bot_plugins is None:
        return default
    try:
        return bot_plugins.get_setting(pid, key, default)
    except Exception:
        return default


def _archive_allowed():
    return _plugin_enabled("chat_history_lookup") or bool(
        _plugin_setting("chat_history_lookup", "passive_archive", True)
    )

TZ_CN = datetime.timezone(datetime.timedelta(hours=8))

HISTORY_ENABLED = onebot_common.env_bool("CHAT_HISTORY_ENABLED", True)
HISTORY_DB_PATH = (
    os.environ.get("CHAT_HISTORY_DB", "").strip()
    or os.path.join(_SCRIPT_DIR, "data", "chat_history.db")
)
SESSIONS_DIR = (
    os.environ.get("CC_SESSIONS_DIR", "").strip()
    or os.path.join(_SCRIPT_DIR, "data", "sessions")
)
HISTORY_MAX_ROUNDS = max(1, min(6, int(os.environ.get("CHAT_HISTORY_MAX_ROUNDS", "3") or "3")))
HISTORY_NAPCAT_MAX_PAGES = max(
    0, min(15, int(os.environ.get("CHAT_HISTORY_NAPCAT_MAX_PAGES", "6") or "6"))
)
HISTORY_NAPCAT_PAGE_SIZE = max(
    20, min(100, int(os.environ.get("CHAT_HISTORY_NAPCAT_PAGE_SIZE", "60") or "60"))
)
HISTORY_RETENTION_DAYS = max(0, int(os.environ.get("CHAT_HISTORY_RETENTION_DAYS", "30") or "30"))
HISTORY_MAX_MSGS_PER_CHAT = max(
    0, int(os.environ.get("CHAT_HISTORY_MAX_MSGS_PER_CHAT", "50000") or "50000")
)
HISTORY_RESULT_MAX_CHARS = max(
    1500, min(12000, int(os.environ.get("CHAT_HISTORY_RESULT_MAX_CHARS", "5200") or "5200"))
)

# Matches [[QQ:history ...]] directives emitted by the model
HISTORY_DIRECTIVE_RE = re.compile(
    r"\[\[\s*QQ\s*:\s*history\b([^\]]*)\]\]",
    re.IGNORECASE,
)
_MSG_ID_RE = re.compile(r"^-?\d{1,20}$")
_CQ_CODE_RE = re.compile(r"\[CQ:([a-zA-Z0-9_]+)(?:,([^\]]*))?\]")
_CC_HEADER_RE = re.compile(
    r'^\[cc-connect\s+sender_id=(\S+)(?:\s+sender_name="([^"]*)")?\s+platform=(\S+)\s+chat_id=([^\]]+)\]\s*',
    re.DOTALL,
)
_ISO_FRAC_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:?\d{2})?$"
)
_NO_REPLY_RE = re.compile(
    r'^[\s`"\'\[\(\*（【]*(?:NO_REPLY|FOLLOW_REPEAT)[\s`"\'\]\)\*）】\.。!！~～]*$',
    re.IGNORECASE,
)
# Neutralize system tags and [[QQ:...]] inside untrusted historical chat text
_FAKE_TAG_RE = onebot_common.FAKE_TAG_RE
_INBOUND_DIRECTIVE_RE = re.compile(r"\[\[\s*(?=QQ\s*:)", re.IGNORECASE)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_key TEXT NOT NULL,
    message_id TEXT NOT NULL,
    message_seq INTEGER NOT NULL DEFAULT 0,
    ts INTEGER NOT NULL,
    user_id TEXT NOT NULL DEFAULT '',
    sender_name TEXT NOT NULL DEFAULT '',
    is_bot INTEGER NOT NULL DEFAULT 0,
    reply_to_id TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL DEFAULT '',
    has_image INTEGER NOT NULL DEFAULT 0,
    image_urls TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'live',
    UNIQUE(chat_key, message_id)
);
CREATE INDEX IF NOT EXISTS idx_msg_chat_ts ON messages(chat_key, ts, id);
CREATE INDEX IF NOT EXISTS idx_msg_chat_seq ON messages(chat_key, message_seq, ts);
CREATE INDEX IF NOT EXISTS idx_msg_chat_user ON messages(chat_key, user_id, ts);
"""

_init_lock = threading.Lock()
_initialized_dbs = set()
_writer_lock = threading.Lock()
_writer_queue = None
_writer_thread = None
_last_prune_ts = 0.0
_last_napcat_sync = {}  # chat_key -> monotonic ts
_last_napcat_sync_lock = threading.Lock()
_synthetic_seq = 0
_synthetic_lock = threading.Lock()


def _log(msg):
    sys.stderr.write(f"[chat-history] {msg}\n")
    sys.stderr.flush()


def neutralize_text(text):
    """Scrub fake system tags and [[QQ:...]] directives from untrusted chat text."""
    if not text:
        return ""
    s = onebot_common.neutralize_fake_tags(str(text))
    s = _INBOUND_DIRECTIVE_RE.sub("［［", s)
    return s


def make_chat_key(is_group, group_id="", dm_user_id=""):
    if is_group:
        gid = str(group_id or "").strip()
        return f"g:{gid}" if gid else ""
    uid = str(dm_user_id or "").strip()
    return f"dm:{uid}" if uid else ""


def _next_synthetic_id(prefix="out"):
    global _synthetic_seq
    with _synthetic_lock:
        _synthetic_seq += 1
        seq = _synthetic_seq
    return f"{prefix}:{int(time.time() * 1000)}:{seq}"


def _connect(db_path=None):
    path = db_path or HISTORY_DB_PATH
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA temp_store=MEMORY")
    if path not in _initialized_dbs:
        with _init_lock:
            if path not in _initialized_dbs:
                try:
                    conn.execute("PRAGMA journal_mode=WAL")
                    conn.execute("PRAGMA synchronous=NORMAL")
                except sqlite3.DatabaseError:
                    pass
                conn.executescript(_SCHEMA_SQL)
                _initialized_dbs.add(path)
    return conn


# ---------------------------------------------------------------------------
# OneBot v11 Message Normalization & Rendering
# ---------------------------------------------------------------------------

def _extract_json_card_summary(raw_data):
    """Best-effort compact summary of QQ JSON share cards (links/Bilibili/etc.)."""
    if not raw_data:
        return "[卡片消息]"
    obj = None
    if isinstance(raw_data, dict):
        obj = raw_data
    elif isinstance(raw_data, str):
        try:
            obj = json.loads(raw_data)
        except Exception:
            obj = None
    if not isinstance(obj, dict):
        return "[卡片消息]"
    prompt = str(obj.get("prompt") or "").strip()
    meta = obj.get("meta")
    title = ""
    desc = ""
    if isinstance(meta, dict):
        for val in meta.values():
            if isinstance(val, dict):
                title = str(val.get("title") or val.get("tag") or "").strip()
                desc = str(val.get("desc") or "").strip()
                if title or desc:
                    break
    snippet = " - ".join([x for x in (title, desc) if x]) or prompt
    snippet = re.sub(r"\s+", " ", snippet).strip()[:100]
    return f"[卡片: {snippet}]" if snippet else "[卡片消息]"


def _parse_cq_params(param_str):
    return onebot_common.parse_cq_params(param_str)


def render_onebot_message(segments, member_map=None, bot_qq_id=""):
    """Render OneBot v11 message segments (or CQ string) into verbatim searchable text.

    Returns dict: {"text": str, "reply_to_id": str, "image_urls": [str, ...]}
    """
    member_map = member_map or {}
    reply_to_id = ""
    image_urls = []
    parts = []

    if isinstance(segments, str):
        pos = 0
        for m in _CQ_CODE_RE.finditer(segments):
            if m.start() > pos:
                parts.append(segments[pos : m.start()])
            pos = m.end()
            ctype = m.group(1).strip().lower()
            cdata = _parse_cq_params(m.group(2) or "")
            if ctype == "at":
                qq = str(cdata.get("qq") or "").strip()
                if qq == "all":
                    parts.append("@全体成员")
                elif bot_qq_id and qq == str(bot_qq_id):
                    parts.append(f"@{member_map.get(qq, '机器人')}(你自己)")
                else:
                    nm = member_map.get(qq) or str(cdata.get("name") or "").strip()
                    parts.append(f"@{nm}({qq})" if nm else f"@{qq}")
            elif ctype == "reply":
                rid = str(cdata.get("id") or "").strip()
                if rid and not reply_to_id:
                    reply_to_id = rid
                if rid:
                    parts.append(f"[回复消息#{rid}] ")
            elif ctype == "image":
                url = str(cdata.get("url") or "").strip()
                if url.startswith(("http://", "https://")):
                    image_urls.append(url)
                summary = str(cdata.get("summary") or "").strip()
                parts.append(summary if summary else "[图片]")
            elif ctype == "face":
                parts.append("[表情]")
            elif ctype == "record":
                parts.append("[语音]")
            elif ctype == "video":
                parts.append("[视频]")
            elif ctype == "forward":
                parts.append("[合并转发]")
            else:
                parts.append(f"[{ctype}]")
        if pos < len(segments):
            parts.append(segments[pos:])
        text = re.sub(r"[ \t]+", " ", "".join(parts)).strip()
        return {"text": text[:4000], "reply_to_id": reply_to_id, "image_urls": image_urls[:4]}

    if not isinstance(segments, list):
        return {"text": "", "reply_to_id": "", "image_urls": []}

    for seg in segments:
        if not isinstance(seg, dict):
            continue
        st = str(seg.get("type") or "").strip().lower()
        dt = seg.get("data") or {}
        if st == "text":
            parts.append(str(dt.get("text") or ""))
        elif st == "at":
            qq = str(dt.get("qq") or "").strip()
            if qq == "all":
                parts.append("@全体成员")
            elif bot_qq_id and qq == str(bot_qq_id):
                parts.append(f"@{member_map.get(qq, '机器人')}(你自己)")
            else:
                nm = member_map.get(qq) or str(dt.get("name") or "").strip()
                parts.append(f"@{nm}({qq})" if nm else f"@{qq}")
        elif st == "reply":
            rid = str(dt.get("id") or "").strip()
            if rid and not reply_to_id:
                reply_to_id = rid
            if rid:
                parts.append(f"[回复消息#{rid}] ")
        elif st == "image":
            url = str(dt.get("url") or "").strip()
            if url.startswith(("http://", "https://")):
                image_urls.append(url)
            summary = str(dt.get("summary") or "").strip()
            if dt.get("sub_type") == 1 or "表情" in summary:
                parts.append(summary if summary else "[表情包]")
            else:
                parts.append(summary if summary else "[图片]")
        elif st == "mface":
            url = str(dt.get("url") or "").strip()
            if url.startswith(("http://", "https://")):
                image_urls.append(url)
            summary = str(dt.get("summary") or "").strip()
            parts.append(summary if summary else "[商城表情]")
        elif st == "face":
            raw_face = dt.get("raw")
            face_text = ""
            if isinstance(raw_face, dict):
                face_text = str(raw_face.get("faceText") or "").strip()
            parts.append(f"[表情:{face_text}]" if face_text else "[表情]")
        elif st == "record":
            parts.append("[语音]")
        elif st == "video":
            parts.append("[视频]")
        elif st == "file":
            fname = str(dt.get("name") or dt.get("file") or "").strip()
            parts.append(f"[文件:{fname}]" if fname else "[文件]")
        elif st == "forward":
            parts.append("[合并转发]")
        elif st == "json":
            parts.append(_extract_json_card_summary(dt.get("data")))
        elif st == "node":
            content = dt.get("content")
            if isinstance(content, (list, str)):
                sub = render_onebot_message(content, member_map=member_map, bot_qq_id=bot_qq_id)
                if sub["text"]:
                    parts.append(sub["text"])

    text = "".join(parts)
    # Preserve single newlines for multi-line messages while collapsing excess whitespace
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.splitlines()]
    text = "\n".join(ln for ln in lines if ln).strip()
    return {"text": text[:4000], "reply_to_id": reply_to_id, "image_urls": image_urls[:4]}


def onebot_msg_to_record(msg_obj, chat_key, bot_qq_id="", member_map=None, source="live"):
    """Convert a OneBot v11 message event or history item into a DB row dict."""
    if not isinstance(msg_obj, dict) or not chat_key:
        return None

    uid = str(msg_obj.get("user_id") or "").strip()
    sender = msg_obj.get("sender") if isinstance(msg_obj.get("sender"), dict) else {}
    sender_name = (
        str(sender.get("card") or "").strip()
        or str(sender.get("nickname") or "").strip()
        or (member_map or {}).get(uid, "")
        or uid
    )
    is_bot = 1 if (bot_qq_id and uid == str(bot_qq_id)) else 0

    raw_mid = msg_obj.get("message_id")
    mid = str(raw_mid).strip() if raw_mid is not None and str(raw_mid).strip() not in ("", "0") else ""
    if not mid:
        mid = _next_synthetic_id("in")

    raw_seq = msg_obj.get("message_seq")
    if raw_seq is None:
        raw_seq = msg_obj.get("real_seq")
    try:
        seq = int(raw_seq) if raw_seq is not None and str(raw_seq).strip().lstrip("-").isdigit() else 0
    except (TypeError, ValueError):
        seq = 0

    raw_ts = msg_obj.get("time")
    try:
        ts = int(float(raw_ts)) if raw_ts else int(time.time())
    except (TypeError, ValueError):
        ts = int(time.time())
    if ts <= 0:
        ts = int(time.time())

    segs = msg_obj.get("message")
    if segs is None:
        segs = msg_obj.get("raw_message") or ""
    rendered = render_onebot_message(segs, member_map=member_map, bot_qq_id=bot_qq_id)
    text = rendered["text"]
    if not text and not rendered["image_urls"]:
        return None

    return {
        "chat_key": chat_key,
        "message_id": mid,
        "message_seq": seq,
        "ts": ts,
        "user_id": uid,
        "sender_name": sender_name[:80],
        "is_bot": is_bot,
        "reply_to_id": rendered["reply_to_id"],
        "text": text,
        "has_image": 1 if rendered["image_urls"] else 0,
        "image_urls": json.dumps(rendered["image_urls"], ensure_ascii=False) if rendered["image_urls"] else "",
        "source": source,
    }


# ---------------------------------------------------------------------------
# Database Persistence & Non-Blocking Background Writer
# ---------------------------------------------------------------------------

def upsert_records(records, db_path=None):
    """Synchronously upsert a list of normalized message records into SQLite."""
    if not HISTORY_ENABLED or not records:
        return 0
    valid = [r for r in records if isinstance(r, dict) and r.get("chat_key") and r.get("message_id")]
    if not valid:
        return 0

    conn = None
    try:
        conn = _connect(db_path=db_path)
        conn.execute("BEGIN IMMEDIATE")
        sql = """
        INSERT INTO messages (
            chat_key, message_id, message_seq, ts, user_id, sender_name,
            is_bot, reply_to_id, text, has_image, image_urls, source
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(chat_key, message_id) DO UPDATE SET
            message_seq = CASE WHEN excluded.message_seq > 0 THEN excluded.message_seq ELSE messages.message_seq END,
            ts = CASE WHEN excluded.ts > 0 AND messages.ts <= 0 THEN excluded.ts ELSE messages.ts END,
            sender_name = CASE
                WHEN excluded.sender_name != '' AND excluded.sender_name != excluded.user_id THEN excluded.sender_name
                WHEN messages.sender_name = '' THEN excluded.sender_name
                ELSE messages.sender_name
            END,
            reply_to_id = CASE WHEN excluded.reply_to_id != '' THEN excluded.reply_to_id ELSE messages.reply_to_id END,
            text = CASE WHEN length(excluded.text) > length(messages.text) THEN excluded.text ELSE messages.text END,
            has_image = CASE WHEN excluded.has_image = 1 THEN 1 ELSE messages.has_image END,
            image_urls = CASE WHEN excluded.image_urls != '' THEN excluded.image_urls ELSE messages.image_urls END
        """
        params = [
            (
                r["chat_key"],
                str(r["message_id"]),
                int(r.get("message_seq") or 0),
                int(r.get("ts") or int(time.time())),
                str(r.get("user_id") or ""),
                str(r.get("sender_name") or ""),
                int(r.get("is_bot") or 0),
                str(r.get("reply_to_id") or ""),
                str(r.get("text") or ""),
                int(r.get("has_image") or 0),
                str(r.get("image_urls") or ""),
                str(r.get("source") or "live"),
            )
            for r in valid
        ]
        conn.executemany(sql, params)

        # Clean up any temporary synthetic outbound rows superseded by real NapCat bot messages
        for r in valid:
            if r.get("is_bot") and _MSG_ID_RE.match(str(r["message_id"])):
                ts_val = int(r.get("ts") or 0)
                if ts_val > 0:
                    conn.execute(
                        """
                        DELETE FROM messages
                        WHERE chat_key = ?
                          AND is_bot = 1
                          AND message_id LIKE 'out:%'
                          AND abs(ts - ?) <= 10
                          AND text = ?
                        """,
                        (r["chat_key"], ts_val, str(r.get("text") or "")),
                    )

        conn.execute("COMMIT")
        _maybe_prune_locked(conn)
        return len(valid)
    except Exception as exc:
        if conn is not None:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
        _log(f"upsert_records failed: {exc}")
        return 0
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _maybe_prune_locked(conn):
    """Infrequently prune expired history rows to keep the SQLite DB bounded."""
    global _last_prune_ts
    now = time.time()
    if now - _last_prune_ts < 3600:
        return
    _last_prune_ts = now
    try:
        if HISTORY_RETENTION_DAYS > 0:
            cutoff = int(now - HISTORY_RETENTION_DAYS * 86400)
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM messages WHERE ts > 0 AND ts < ?", (cutoff,))
            conn.execute("COMMIT")
        if HISTORY_MAX_MSGS_PER_CHAT > 0:
            chats = [
                row["chat_key"]
                for row in conn.execute(
                    "SELECT chat_key, COUNT(*) AS cnt FROM messages GROUP BY chat_key HAVING cnt > ?",
                    (HISTORY_MAX_MSGS_PER_CHAT,),
                ).fetchall()
            ]
            for ck in chats:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    """
                    DELETE FROM messages
                    WHERE chat_key = ?
                      AND id NOT IN (
                          SELECT id FROM messages
                          WHERE chat_key = ?
                          ORDER BY ts DESC, id DESC
                          LIMIT ?
                      )
                    """,
                    (ck, ck, HISTORY_MAX_MSGS_PER_CHAT),
                )
                conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass


def _ensure_writer_thread():
    global _writer_queue, _writer_thread
    if _writer_thread is not None and _writer_thread.is_alive():
        return _writer_queue
    with _writer_lock:
        if _writer_thread is not None and _writer_thread.is_alive():
            return _writer_queue
        _writer_queue = queue.Queue(maxsize=2000)

        def _loop():
            q = _writer_queue
            while True:
                try:
                    first = q.get()
                except Exception:
                    return
                batch = [first]
                while len(batch) < 50:
                    try:
                        batch.append(q.get_nowait())
                    except queue.Empty:
                        break
                # Group by db_path (normally always None -> default)
                by_db = {}
                for db_p, rec in batch:
                    by_db.setdefault(db_p, []).append(rec)
                for db_p, recs in by_db.items():
                    upsert_records(recs, db_path=db_p)

        _writer_thread = threading.Thread(target=_loop, name="chat-history-writer", daemon=True)
        _writer_thread.start()
        return _writer_queue


def record_record_async(record, db_path=None):
    """Enqueue a normalized record for non-blocking background persistence."""
    if not HISTORY_ENABLED or not record:
        return
    try:
        q = _ensure_writer_thread()
        q.put_nowait((db_path, record))
    except queue.Full:
        pass
    except Exception:
        pass


def record_onebot_event_async(obj, bot_qq_id="", ignored_bot_ids=None, db_path=None):
    """Archive an incoming OneBot v11 `message` event (group or private) without blocking."""
    if not HISTORY_ENABLED or not _archive_allowed() or not isinstance(obj, dict):
        return
    if obj.get("post_type") != "message":
        return
    mtype = obj.get("message_type")
    uid = str(obj.get("user_id") or "").strip()
    if ignored_bot_ids and uid in ignored_bot_ids:
        return
    if mtype == "group":
        gid = str(obj.get("group_id") or "").strip()
        chat_key = make_chat_key(True, group_id=gid)
    elif mtype == "private":
        chat_key = make_chat_key(False, dm_user_id=uid)
    else:
        return
    if not chat_key:
        return
    rec = onebot_msg_to_record(obj, chat_key=chat_key, bot_qq_id=bot_qq_id, source="live")
    if rec:
        record_record_async(rec, db_path=db_path)


def record_outbound_bot_message_async(
    chat_key,
    text,
    message_id="",
    bot_qq_id="",
    bot_name="哈基米",
    reply_to_id="",
    ts=None,
    db_path=None,
):
    """Archive an outbound bot reply (including merged-forward cards) without blocking."""
    if not HISTORY_ENABLED or not _archive_allowed() or not chat_key:
        return
    clean = str(text or "").strip()
    if not clean or _NO_REPLY_RE.match(clean):
        return
    mid = str(message_id or "").strip()
    if not mid or mid == "0":
        mid = _next_synthetic_id("out")
    rec = {
        "chat_key": chat_key,
        "message_id": mid,
        "message_seq": 0,
        "ts": int(ts) if ts else int(time.time()),
        "user_id": str(bot_qq_id or ""),
        "sender_name": str(bot_name or "哈基米")[:80],
        "is_bot": 1,
        "reply_to_id": str(reply_to_id or "").strip(),
        "text": clean[:4000],
        "has_image": 0,
        "image_urls": "",
        "source": "outbound",
    }
    record_record_async(rec, db_path=db_path)


def record_napcat_messages_async(
    messages, is_group, group_id="", dm_user_id="", bot_qq_id="", member_map=None, db_path=None
):
    """Archive a batch of messages returned by NapCat history API in background."""
    if (
        not HISTORY_ENABLED
        or not _archive_allowed()
        or not isinstance(messages, list)
        or not messages
    ):
        return
    chat_key = make_chat_key(is_group, group_id=group_id, dm_user_id=dm_user_id)
    if not chat_key:
        return
    for m in messages:
        rec = onebot_msg_to_record(
            m, chat_key=chat_key, bot_qq_id=bot_qq_id, member_map=member_map, source="napcat"
        )
        if rec:
            record_record_async(rec, db_path=db_path)


def lookup_message_local(chat_key, message_id, db_path=None):
    """Fast (<1ms) single-message lookup from local SQLite archive for quote resolution."""
    if not HISTORY_ENABLED or not chat_key or not message_id:
        return None
    mid = str(message_id).strip().lstrip("#")
    if not mid:
        return None
    if not os.path.exists(db_path or HISTORY_DB_PATH):
        return None
    conn = None
    try:
        conn = _connect(db_path=db_path)
        row = conn.execute(
            """
            SELECT message_id, message_seq, ts, user_id, sender_name, is_bot, reply_to_id, text, has_image, image_urls
            FROM messages
            WHERE chat_key = ? AND message_id = ?
            LIMIT 1
            """,
            (chat_key, mid),
        ).fetchone()
        return dict(row) if row else None
    except Exception:
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# NapCat Paginated Backfill & Ownership Verification
# ---------------------------------------------------------------------------

def _default_napcat_post(endpoint, payload, timeout=3.0):
    base_url = os.environ.get("NAPCAT_HTTP_URL", "http://127.0.0.1:3000").rstrip("/")
    token = os.environ.get("NAPCAT_TOKEN", "")
    ok, data, _ = onebot_common.napcat_api_post(base_url, token, endpoint, payload, timeout=timeout)
    return data if ok else None


def _verify_napcat_msg_ownership(msg_obj, is_group, group_id, dm_user_id, bot_qq_id):
    """Strictly verify a NapCat `get_msg` response belongs to the current chat."""
    if not isinstance(msg_obj, dict):
        return False
    if is_group:
        gid = str(group_id or "").strip()
        return bool(gid) and str(msg_obj.get("group_id") or "").strip() == gid
    return onebot_common.verify_dm_msg_ownership(msg_obj, dm_user_id, bot_qq_id)


def _fetch_napcat_history_page(is_group, group_id, dm_user_id, count, message_seq, napcat_fn, timeout=2.5):
    if is_group:
        try:
            gid = int(group_id)
        except (TypeError, ValueError):
            return []
        payload = {"group_id": gid, "count": int(count)}
        if message_seq and int(message_seq) > 0:
            payload["message_seq"] = int(message_seq)
        data = napcat_fn("get_group_msg_history", payload, timeout=timeout)
    else:
        try:
            uid = int(dm_user_id)
        except (TypeError, ValueError):
            return []
        payload = {"user_id": uid, "count": int(count)}
        if message_seq and int(message_seq) > 0:
            payload["message_seq"] = int(message_seq)
        data = napcat_fn("get_friend_msg_history", payload, timeout=timeout)

    if isinstance(data, dict) and isinstance(data.get("messages"), list):
        return data["messages"]
    if isinstance(data, list):
        return data
    return []


def _get_oldest_cursor_in_db(chat_key, db_path=None):
    """Return (oldest_ts, oldest_seq_or_id, newest_ts, total_count) for chat_key."""
    conn = None
    try:
        conn = _connect(db_path=db_path)
        stats = conn.execute(
            "SELECT COUNT(*) AS cnt, MIN(ts) AS min_ts, MAX(ts) AS max_ts FROM messages WHERE chat_key = ?",
            (chat_key,),
        ).fetchone()
        cnt = int(stats["cnt"] or 0)
        if cnt == 0:
            return 0, 0, 0, 0
        oldest = conn.execute(
            """
            SELECT message_seq, message_id, ts
            FROM messages
            WHERE chat_key = ? AND (message_seq > 0 OR message_id GLOB '[0-9]*' OR message_id GLOB '-[0-9]*')
            ORDER BY ts ASC, id ASC
            LIMIT 1
            """,
            (chat_key,),
        ).fetchone()
        cursor = 0
        if oldest:
            if int(oldest["message_seq"] or 0) > 0:
                cursor = int(oldest["message_seq"])
            elif _MSG_ID_RE.match(str(oldest["message_id"] or "")):
                val = int(oldest["message_id"])
                if val > 0:
                    cursor = val
        return int(stats["min_ts"] or 0), cursor, int(stats["max_ts"] or 0), cnt
    except Exception:
        return 0, 0, 0, 0
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def backfill_from_napcat(
    is_group,
    group_id="",
    dm_user_id="",
    bot_qq_id="",
    member_map=None,
    napcat_fn=None,
    db_path=None,
    max_pages=None,
    page_size=None,
    target_min_ts=0,
    start_cursor_seq=0,
    include_latest=True,
    time_budget_secs=4.5,
):
    """Paginate backward through NapCat history and persist pages into SQLite.

    Returns dict: {"fetched": int, "pages": int, "exhausted": bool}
    """
    call = napcat_fn or _default_napcat_post
    chat_key = make_chat_key(is_group, group_id=group_id, dm_user_id=dm_user_id)
    if not chat_key:
        return {"fetched": 0, "pages": 0, "exhausted": True}

    pages_limit = HISTORY_NAPCAT_MAX_PAGES if max_pages is None else max(0, int(max_pages))
    if pages_limit <= 0:
        return {"fetched": 0, "pages": 0, "exhausted": False}

    psize = page_size or HISTORY_NAPCAT_PAGE_SIZE
    deadline = time.monotonic() + max(0.8, float(time_budget_secs))
    total_fetched = 0
    pages_done = 0
    exhausted = False
    seen_mids = set()

    def _extract_oldest_cursor(raw_msgs):
        ordered = sorted(
            [m for m in raw_msgs if isinstance(m, dict)],
            key=lambda m: (
                int(m.get("time") or 0),
                int(m.get("message_seq") or 0)
                if str(m.get("message_seq") or "").lstrip("-").isdigit()
                else 0,
            ),
        )
        if not ordered:
            return 0, 0
        first = ordered[0]
        ts_val = int(first.get("time") or 0)
        seq_raw = first.get("message_seq")
        if seq_raw is None:
            seq_raw = first.get("real_seq")
        if seq_raw is not None and str(seq_raw).strip().isdigit() and int(seq_raw) > 0:
            return int(seq_raw), ts_val
        mid_raw = first.get("message_id")
        if mid_raw is not None and str(mid_raw).strip().isdigit() and int(mid_raw) > 0:
            return int(mid_raw), ts_val
        return 0, ts_val

    # Step 1: Refresh latest page if requested and stale (>8s)
    cursor = int(start_cursor_seq or 0)
    if include_latest and cursor == 0 and pages_done < pages_limit and time.monotonic() < deadline:
        now_mono = time.monotonic()
        should_refresh = True
        with _last_napcat_sync_lock:
            if now_mono - _last_napcat_sync.get(chat_key, -1e9) < 8.0:
                should_refresh = False
        if should_refresh:
            raw_latest = _fetch_napcat_history_page(
                is_group, group_id, dm_user_id, psize, 0, call, timeout=2.5
            )
            pages_done += 1
            if raw_latest:
                with _last_napcat_sync_lock:
                    _last_napcat_sync[chat_key] = time.monotonic()
                recs = []
                for m in raw_latest:
                    r = onebot_msg_to_record(
                        m, chat_key=chat_key, bot_qq_id=bot_qq_id, member_map=member_map, source="napcat"
                    )
                    if r:
                        seen_mids.add(r["message_id"])
                        recs.append(r)
                next_cur, oldest_ts = _extract_oldest_cursor(raw_latest)
                oldest_rec = min(recs, key=lambda r: (r["ts"], r["message_seq"])) if recs else None
                overlapped_existing = bool(
                    oldest_rec and lookup_message_local(chat_key, oldest_rec["message_id"], db_path=db_path)
                )
                total_fetched += upsert_records(recs, db_path=db_path)
                # Only skip directly to DB's oldest cursor when the latest NapCat page
                # overlapped already-archived messages (otherwise keep walking back to fill any offline gap)
                db_min_ts, db_cursor, _, _ = _get_oldest_cursor_in_db(chat_key, db_path=db_path)
                if overlapped_existing and db_cursor > 0 and (db_min_ts <= oldest_ts or next_cur == 0):
                    cursor = db_cursor
                else:
                    cursor = next_cur
                if len(raw_latest) < max(5, psize // 3):
                    exhausted = True
            else:
                exhausted = True

    if cursor == 0 and not exhausted:
        _, db_cursor, _, _ = _get_oldest_cursor_in_db(chat_key, db_path=db_path)
        cursor = db_cursor

    # Step 2: Walk backward page by page using message_seq
    prev_cursor = -1
    while (
        not exhausted
        and cursor > 0
        and cursor != prev_cursor
        and pages_done < pages_limit
        and time.monotonic() < deadline
    ):
        prev_cursor = cursor
        rem_timeout = min(2.5, max(0.6, deadline - time.monotonic()))
        raw_page = _fetch_napcat_history_page(
            is_group, group_id, dm_user_id, psize, cursor, call, timeout=rem_timeout
        )
        pages_done += 1
        if not raw_page:
            exhausted = True
            break

        recs = []
        new_count = 0
        for m in raw_page:
            r = onebot_msg_to_record(
                m, chat_key=chat_key, bot_qq_id=bot_qq_id, member_map=member_map, source="napcat"
            )
            if r:
                if r["message_id"] not in seen_mids:
                    seen_mids.add(r["message_id"])
                    new_count += 1
                recs.append(r)

        if recs:
            total_fetched += upsert_records(recs, db_path=db_path)

        next_cur, oldest_ts = _extract_oldest_cursor(raw_page)
        if new_count == 0 or next_cur == 0 or next_cur == cursor:
            exhausted = True
            break
        cursor = next_cur
        if target_min_ts > 0 and oldest_ts > 0 and oldest_ts <= target_min_ts:
            break

    return {"fetched": total_fetched, "pages": pages_done, "exhausted": exhausted}


# ---------------------------------------------------------------------------
# cc-connect Session History Fallback (Strictly Scoped to Current Chat!)
# ---------------------------------------------------------------------------

def _parse_iso_ts(ts_str):
    s = str(ts_str or "").strip()
    if not s:
        return 0
    m = _ISO_FRAC_RE.match(s)
    if not m:
        return 0
    base, frac, tz = m.group(1).replace(" ", "T"), m.group(2), m.group(3)
    iso = f"{base}.{(frac + '000000')[:6]}" if frac else base
    if tz:
        if tz == "Z":
            tz = "+00:00"
        elif len(tz) == 5 and ":" not in tz:
            tz = tz[:3] + ":" + tz[3:]
        iso += tz
    try:
        dt = datetime.datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ_CN)
        return int(dt.timestamp())
    except ValueError:
        return 0


_session_json_cache = {}  # (fpath, st_mtime_ns, st_size) -> parsed dict
_session_json_cache_lock = threading.Lock()
_SESSION_CACHE_MAX_ENTRIES = 32


def _load_session_json_cached(fpath):
    """Load & cache cc-connect session JSON keyed by (file_path, st_mtime_ns, st_size)."""
    try:
        st = os.stat(fpath)
    except OSError:
        return None
    cache_key = (fpath, st.st_mtime_ns, st.st_size)
    with _session_json_cache_lock:
        cached = _session_json_cache.get(cache_key)
        if cached is not None:
            return cached
    try:
        with open(fpath, "r", encoding="utf-8", errors="replace") as f:
            data = json.load(f)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    with _session_json_cache_lock:
        for old_k in [k for k in _session_json_cache if k[0] == fpath and k != cache_key]:
            _session_json_cache.pop(old_k, None)
        if len(_session_json_cache) >= _SESSION_CACHE_MAX_ENTRIES:
            oldest_key = next(iter(_session_json_cache))
            _session_json_cache.pop(oldest_key, None)
        _session_json_cache[cache_key] = data
    return data


def _load_cc_session_messages(is_group, group_id="", dm_user_id="", bot_qq_id="", sessions_dir=None):
    """Read verbatim history from `data/sessions/*.json` ONLY for this exact chat key.

    Strictly isolates by `cc_key = "qq:g:<gid>"` (group) or `"qq:<uid>"` (DM).
    """
    sdir = sessions_dir or SESSIONS_DIR
    if not sdir or not os.path.isdir(sdir):
        return []
    if is_group:
        gid = str(group_id or "").strip()
        if not gid:
            return []
        cc_key = f"qq:g:{gid}"
    else:
        uid = str(dm_user_id or "").strip()
        if not uid:
            return []
        cc_key = f"qq:{uid}"

    items = []
    try:
        fnames = sorted(os.listdir(sdir))
    except OSError:
        return []

    for fname in fnames:
        if not fname.endswith(".json"):
            continue
        fpath = os.path.join(sdir, fname)
        data = _load_session_json_cached(fpath)
        if not isinstance(data, dict):
            continue

        # Collect ONLY session IDs belonging to this exact cc_key
        session_ids = []
        us = data.get("user_sessions")
        if isinstance(us, dict) and isinstance(us.get(cc_key), list):
            for sid in us[cc_key]:
                if isinstance(sid, str) and sid not in session_ids:
                    session_ids.append(sid)
        act = data.get("active_session")
        if isinstance(act, dict):
            asid = act.get(cc_key)
            if isinstance(asid, str) and asid not in session_ids:
                session_ids.append(asid)

        sessions = data.get("sessions")
        if not isinstance(sessions, dict) or not session_ids:
            continue

        for sid in session_ids:
            sobj = sessions.get(sid)
            if not isinstance(sobj, dict):
                continue
            hist = sobj.get("history")
            if not isinstance(hist, list):
                continue
            for idx, entry in enumerate(hist):
                if not isinstance(entry, dict):
                    continue
                role = str(entry.get("role") or "").strip().lower()
                content = str(entry.get("content") or "").strip()
                if not content or _NO_REPLY_RE.match(content):
                    continue
                if content.startswith(("/compact", "/compress", "/new", "/reset")):
                    continue

                sender_id = ""
                sender_name = ""
                is_bot = 1 if role == "assistant" else 0
                if is_bot:
                    sender_id = str(bot_qq_id or "")
                    sender_name = "你自己"
                else:
                    hm = _CC_HEADER_RE.match(content)
                    if hm:
                        sender_id = hm.group(1) or ""
                        sender_name = hm.group(2) or sender_id
                        content = content[hm.end() :].strip()
                    elif not is_group:
                        sender_id = str(dm_user_id or "")
                        sender_name = "对方"
                    else:
                        sender_name = "群友"

                if not content:
                    continue
                ts = _parse_iso_ts(entry.get("timestamp"))
                items.append(
                    {
                        "message_id": f"sess:{sid}:{idx}",
                        "message_seq": 0,
                        "ts": ts,
                        "user_id": sender_id,
                        "sender_name": sender_name,
                        "is_bot": is_bot,
                        "reply_to_id": "",
                        "text": content[:4000],
                        "has_image": 0,
                        "image_urls": "",
                        "source": "session",
                    }
                )

    items.sort(key=lambda r: (int(r.get("ts") or 0), r["message_id"]))
    return items


# ---------------------------------------------------------------------------
# Time Expression Parsing & Search Matching
# ---------------------------------------------------------------------------

def parse_time_expr(raw, is_end=False, now_dt=None):
    """Parse flexible human/ISO time expressions in Asia/Shanghai (UTC+8) -> Unix ts or None."""
    s = str(raw or "").strip()
    if not s:
        return None
    now = now_dt or datetime.datetime.now(tz=TZ_CN)
    low = s.lower()

    # Pure unix timestamp
    if s.isdigit() and len(s) in (10, 13):
        val = int(s)
        return val // 1000 if len(s) == 13 else val

    # Relative shorthand: -2h / 2h / 30m / 3d
    m_rel = re.match(r"^-?(\d+(?:\.\d+)?)\s*([mhd]|min|mins|hour|hours|day|days|分钟|小时|天)$", low)
    if m_rel:
        amt = float(m_rel.group(1))
        unit = m_rel.group(2)
        if unit in ("m", "min", "mins", "分钟"):
            delta = datetime.timedelta(minutes=amt)
        elif unit in ("h", "hour", "hours", "小时"):
            delta = datetime.timedelta(hours=amt)
        else:
            delta = datetime.timedelta(days=amt)
        return int((now - delta).timestamp())

    # Chinese day words: 今天 / 昨天 / 前天 [+ HH:MM]
    m_cn = re.match(r"^(今天|今日|昨天|昨日|前天)(?:\s*(\d{1,2}):(\d{2})(?::(\d{2}))?)?$", s)
    if m_cn:
        word = m_cn.group(1)
        offset_days = 0 if word in ("今天", "今日") else (1 if word in ("昨天", "昨日") else 2)
        day = (now - datetime.timedelta(days=offset_days)).date()
        if m_cn.group(2) is not None:
            hh, mm, ss = int(m_cn.group(2)), int(m_cn.group(3)), int(m_cn.group(4) or 0)
        elif is_end:
            hh, mm, ss = 23, 59, 59
        else:
            hh, mm, ss = 0, 0, 0
        try:
            dt = datetime.datetime(day.year, day.month, day.day, hh, mm, ss, tzinfo=TZ_CN)
            return int(dt.timestamp())
        except ValueError:
            return None

    # Full date/datetime: YYYY-MM-DD [HH:MM[:SS]]
    m_full = re.match(
        r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})(?:[T\s]+(\d{1,2}):(\d{2})(?::(\d{2}))?)?$",
        s,
    )
    if m_full:
        yy, mo, dd = int(m_full.group(1)), int(m_full.group(2)), int(m_full.group(3))
        if m_full.group(4) is not None:
            hh, mm, ss = int(m_full.group(4)), int(m_full.group(5)), int(m_full.group(6) or 0)
        elif is_end:
            hh, mm, ss = 23, 59, 59
        else:
            hh, mm, ss = 0, 0, 0
        try:
            dt = datetime.datetime(yy, mo, dd, hh, mm, ss, tzinfo=TZ_CN)
            return int(dt.timestamp())
        except ValueError:
            return None

    # Month-day: MM-DD [HH:MM]
    m_md = re.match(r"^(\d{1,2})[-/](\d{1,2})(?:\s+(\d{1,2}):(\d{2})(?::(\d{2}))?)?$", s)
    if m_md:
        mo, dd = int(m_md.group(1)), int(m_md.group(2))
        if m_md.group(3) is not None:
            hh, mm, ss = int(m_md.group(3)), int(m_md.group(4)), int(m_md.group(5) or 0)
        elif is_end:
            hh, mm, ss = 23, 59, 59
        else:
            hh, mm, ss = 0, 0, 0
        try:
            dt = datetime.datetime(now.year, mo, dd, hh, mm, ss, tzinfo=TZ_CN)
            if dt > now + datetime.timedelta(days=2):
                dt = dt.replace(year=now.year - 1)
            return int(dt.timestamp())
        except ValueError:
            return None

    # Time only: HH:MM[:SS]
    m_hm = re.match(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$", s)
    if m_hm:
        hh, mm, ss = int(m_hm.group(1)), int(m_hm.group(2)), int(m_hm.group(3) or 0)
        try:
            dt = datetime.datetime(now.year, now.month, now.day, hh, mm, ss, tzinfo=TZ_CN)
            if not is_end and dt > now + datetime.timedelta(minutes=5):
                dt -= datetime.timedelta(days=1)
            return int(dt.timestamp())
        except ValueError:
            return None

    return None


def _resolve_time_bounds(args, now_dt=None):
    now = now_dt or datetime.datetime.now(tz=TZ_CN)
    start_ts = parse_time_expr(
        args.get("start") or args.get("since") or args.get("from_time"),
        is_end=False,
        now_dt=now,
    )
    end_ts = parse_time_expr(
        args.get("end") or args.get("until") or args.get("to_time"),
        is_end=True,
        now_dt=now,
    )
    if start_ts is None:
        raw_hours = args.get("hours")
        raw_days = args.get("days")
        if raw_hours:
            try:
                h = max(0.05, min(24 * 90, float(raw_hours)))
                start_ts = int((now - datetime.timedelta(hours=h)).timestamp())
            except (TypeError, ValueError):
                pass
        elif raw_days:
            try:
                d = max(0.05, min(365, float(raw_days)))
                start_ts = int((now - datetime.timedelta(days=d)).timestamp())
            except (TypeError, ValueError):
                pass
    return start_ts, end_ts


def _sender_matches(row, sender_filter, bot_qq_id=""):
    if not sender_filter:
        return True
    sf = sender_filter.strip().lower()
    if sf in ("me", "self", "bot", "我自己", "你自己", "自己", "机器人"):
        return bool(row.get("is_bot")) or (bool(bot_qq_id) and str(row.get("user_id") or "") == str(bot_qq_id))
    uid = str(row.get("user_id") or "").strip().lower()
    name = str(row.get("sender_name") or "").strip().lower()
    if sf == uid:
        return True
    return sf in name or sf in uid


def _split_search_terms(query):
    raw = str(query or "").strip()
    if not raw:
        return []
    # Split by whitespace, Chinese/English commas, or slash/pipe
    terms = [t.strip().lower() for t in re.split(r"[\s,，、|/]+", raw) if t.strip()]
    seen = set()
    out = []
    for t in terms:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out[:8]


def _score_search_row(row, terms):
    """Return (matched_all: bool, match_count: int) against row text + sender_name."""
    if not terms:
        return True, 1
    haystack = (str(row.get("text") or "") + " " + str(row.get("sender_name") or "")).lower()
    hits = sum(1 for t in terms if t in haystack)
    if hits == 0:
        return False, 0
    return (hits == len(terms)), hits


def _dedup_and_merge_session_rows(db_rows, session_rows):
    """Merge cc-connect session fallback rows that aren't already represented in db_rows.

    Uses 20-second timestamp buckets (`ts // 20`) to run in O(N + M) instead of O(N * M).
    """
    if not session_rows:
        return list(db_rows)
    out = list(db_rows)
    buckets = {}  # (ts // 20) -> list[(ts, norm_text)]
    for r in db_rows:
        t = re.sub(r"\s+", " ", str(r.get("text") or "")).strip().lower()
        ts = int(r.get("ts") or 0)
        if ts > 0 and t:
            buckets.setdefault(ts // 20, []).append((ts, t))

    for sr in session_rows:
        st = re.sub(r"\s+", " ", str(sr.get("text") or "")).strip().lower()
        sts = int(sr.get("ts") or 0)
        if not st:
            continue
        dup = False
        if sts > 0:
            b = sts // 20
            for bk in (b - 1, b, b + 1):
                cand_list = buckets.get(bk)
                if not cand_list:
                    continue
                for ets, et in cand_list:
                    if abs(sts - ets) <= 20:
                        if st == et or (len(st) >= 8 and (st in et or et in st)):
                            dup = True
                            break
                if dup:
                    break
        if not dup:
            out.append(sr)
            if sts > 0:
                buckets.setdefault(sts // 20, []).append((sts, st))
    return out


# ---------------------------------------------------------------------------
# Directive Parsing & Prompt Builder
# ---------------------------------------------------------------------------

def parse_directive_args(raw_args):
    return onebot_common.parse_directive_args(raw_args)


def normalize_history_query(raw_args):
    """Normalize raw `[[QQ:history ...]]` arguments into a canonical query spec.

    Security: deliberately ignores any chat/group/target identifier supplied by the model.
    """
    a = parse_directive_args(raw_args)

    action = str(a.get("action") or a.get("op") or a.get("mode") or "").strip().lower()
    query = str(
        a.get("query")
        or a.get("q")
        or a.get("keyword")
        or a.get("keywords")
        or a.get("text")
        or a.get("k")
        or ""
    ).strip()
    sender = str(
        a.get("sender")
        or a.get("user")
        or a.get("qq")
        or a.get("from")
        or a.get("author")
        or a.get("name")
        or ""
    ).strip()
    msg_id = str(a.get("msg_id") or a.get("id") or a.get("message_id") or "").strip().lstrip("#")
    before_id = str(a.get("before_id") or a.get("before") or a.get("cursor") or "").strip().lstrip("#")
    start = str(a.get("start") or a.get("since") or a.get("from_time") or "").strip()
    end = str(a.get("end") or a.get("until") or a.get("to_time") or "").strip()
    hours = str(a.get("hours") or "").strip()
    days = str(a.get("days") or "").strip()
    include_images = str(a.get("include_images") or a.get("images") or "").strip().lower() in (
        "1", "true", "yes", "on",
    )

    try:
        limit = int(float(a.get("limit") or a.get("count") or a.get("n") or 0))
    except (TypeError, ValueError):
        limit = 0
    try:
        offset = max(0, int(float(a.get("offset") or a.get("skip") or 0)))
    except (TypeError, ValueError):
        offset = 0
    try:
        window = int(float(a.get("window") or a.get("context") or a.get("radius") or 0))
    except (TypeError, ValueError):
        window = 0

    # Infer action if omitted or aliased
    if action in ("find", "grep", "filter"):
        action = "search"
    elif action in ("context", "neighbors", "nearby"):
        action = "around"
    elif action in ("get", "read", "message", "detail", "quote"):
        action = "msg"
    elif action in ("list", "scroll", "older", "back", "page"):
        action = "recent"
    elif action in ("range", "between", "date"):
        action = "time"

    if action not in ("search", "recent", "around", "msg", "time"):
        if msg_id and window > 0:
            action = "around"
        elif msg_id and not query and not before_id:
            action = "msg"
        elif query or sender:
            action = "search"
        elif before_id:
            action = "recent"
        elif start or end or hours or days:
            action = "time"
        else:
            action = "recent"

    if limit <= 0:
        limit = 20 if action == "search" else 35
    limit = max(1, min(50, limit))
    if window <= 0:
        window = 8
    window = max(1, min(20, window))

    return {
        "action": action,
        "query": query[:120],
        "sender": sender[:60],
        "msg_id": msg_id[:40],
        "before_id": before_id[:40],
        "start": start[:40],
        "end": end[:40],
        "hours": hours[:20],
        "days": days[:20],
        "limit": limit,
        "offset": offset,
        "window": window,
        "include_images": include_images,
    }


def extract_history_directives(text):
    """Extract all `[[QQ:history ...]]` directives from model output (normalized list)."""
    if not text or "history" not in text.lower():
        return []
    out = []
    for m in HISTORY_DIRECTIVE_RE.finditer(text):
        out.append(normalize_history_query(m.group(1)))
    return out


def strip_history_directives(text):
    """Remove any `[[QQ:history ...]]` directives from text so they never leak to QQ."""
    if not text:
        return ""
    cleaned = HISTORY_DIRECTIVE_RE.sub("", text)
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def query_fingerprint(q):
    """Canonical hashable tuple to prevent repeating identical queries in a loop."""
    return (
        q.get("action", ""),
        q.get("query", "").lower(),
        q.get("sender", "").lower(),
        q.get("msg_id", ""),
        q.get("before_id", ""),
        q.get("start", ""),
        q.get("end", ""),
        q.get("hours", ""),
        q.get("days", ""),
        int(q.get("limit", 0)),
        int(q.get("offset", 0)),
        int(q.get("window", 0)),
    )


def build_history_prompt_block(
    is_group=True,
    recently_compressed=False,
    fresh_session=False,
    is_lurking=False,
):
    """Build compact prompt guidance teaching the model when and how to look up history."""
    if not HISTORY_ENABLED or not _plugin_enabled("chat_history_lookup"):
        return ""

    scope_word = "当前群聊" if is_group else "当前私聊"
    lines = []
    if recently_compressed:
        lines.append(
            f"[上下文提醒: 本会话较早对话刚被压缩摘要，若当前消息涉及之前聊过的原话、具体细节、谁说过什么、早先的链接/报错/承诺，不要凭模糊印象猜，也不要直接说“不记得了”——请先用下面的 [[QQ:history ...]] 自行翻查{scope_word}原始记录后再答]"
        )
    elif fresh_session:
        lines.append(
            f"[上下文提醒: 这是一个新开启的会话，如对方提到之前聊过的事、旧梗、前文原话或历史记录，不要凭空猜测或说“不记得了”，请先用下面的 [[QQ:history ...]] 翻查{scope_word}历史记录后再答]"
        )

    lurk_note = (
        "（未点名潜水时一般直接判断是否接话；仅在有人公开问前文/旧事且确实需要翻记录才调用）"
        if is_lurking
        else ""
    )
    lines.append(
        f"[历史记录查阅: 仅限查阅{scope_word}的原始聊天记录{lurk_note}。"
        "当你不知道前面聊过什么、话题超出近期记录条数、引用了较早的 [#消息ID]、或上下文被压缩不记得原文时，"
        "不要编造或直接敷衍“不记得了”，请先只输出翻查指令（不要同时输出聊天正文），系统会把原文喂给你后再由你自然作答：\n"
        '- 按关键词/发言人搜原文：`[[QQ:history action=search query="关键词" sender="昵称或QQ或self(可选)" days=7 limit=20]]`\n'
        "- 往前翻更早的连续聊天：`[[QQ:history action=recent limit=40 before_id=最早消息ID(可选)]]`\n"
        "- 看某条消息前后的完整对话：`[[QQ:history action=around msg_id=消息ID window=10]]`\n"
        "- 看某条消息完整原文(可附原图)：`[[QQ:history action=msg msg_id=消息ID]]`\n"
        '- 按时间段翻看：`[[QQ:history action=time start="昨天 20:00" end="今天 01:00" limit=40]]` 或 `hours=6`\n'
        "查完后像平时聊天一样自然回复，不要向对方提起任何指令语法或“我去查了数据库”。]"
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Query Execution Engine
# ---------------------------------------------------------------------------

def _fmt_ts(ts):
    if not isinstance(ts, (int, float)) or ts <= 0:
        return "--:--"
    dt = datetime.datetime.fromtimestamp(int(ts), tz=TZ_CN)
    now = datetime.datetime.now(tz=TZ_CN)
    if dt.date() == now.date():
        return dt.strftime("%H:%M")
    return dt.strftime("%m-%d %H:%M")


def _format_row_line(row, bot_qq_id="", admin_qq_ids=None, max_text_len=300, highlight=False, quote_map=None):
    admins = admin_qq_ids or set()
    uid = str(row.get("user_id") or "").strip()
    is_bot = bool(row.get("is_bot")) or (bool(bot_qq_id) and uid == str(bot_qq_id))
    name = str(row.get("sender_name") or "").strip() or ("你自己" if is_bot else (uid or "未知"))
    role_tag = "(你自己)" if is_bot else ("(管理员)" if uid and uid in admins else "")
    if uid and name != "你自己":
        sender_label = f"{name}({uid}){role_tag}"
    else:
        sender_label = f"{name}{role_tag}" if role_tag and role_tag not in name else name

    t_str = _fmt_ts(row.get("ts"))
    mid = str(row.get("message_id") or "").strip()
    if mid.startswith("sess:"):
        id_tag = "[会话归档] "
    elif mid.startswith(("out:", "in:")):
        id_tag = ""
    elif mid:
        id_tag = f"[#{mid}] "
    else:
        id_tag = ""

    raw_text = str(row.get("text") or "").strip()
    # Enrich [回复消息#rid] with snippet if known in quote_map
    rid = str(row.get("reply_to_id") or "").strip()
    if rid and quote_map and rid in quote_map:
        q_sender, q_text = quote_map[rid]
        q_snip = q_text.replace("\n", " ")[:40]
        raw_text = raw_text.replace(f"[回复消息#{rid}]", f'[回复 {q_sender}: "{q_snip}" (#{rid})]')

    if max_text_len > 0 and len(raw_text) > max_text_len:
        raw_text = raw_text[:max_text_len] + "…(截断，可用 action=msg 查看全文)"
    raw_text = raw_text.replace("\n", " / ")

    prefix = ">>> " if highlight else "- "
    return neutralize_text(f"{prefix}[{t_str}] {id_tag}{sender_label}: {raw_text}")


def _extract_row_image_urls(row):
    raw = row.get("image_urls")
    if isinstance(raw, list):
        return [str(u).strip() for u in raw if isinstance(u, str) and u.strip()]
    if isinstance(raw, str) and raw.strip():
        try:
            arr = json.loads(raw)
            if isinstance(arr, list):
                return [str(u).strip() for u in arr if isinstance(u, str) and u.strip()]
        except Exception:
            pass
    return []


def _fetch_db_rows(chat_key, start_ts=None, end_ts=None, limit=5000, db_path=None):
    conn = None
    try:
        conn = _connect(db_path=db_path)
        clauses = ["chat_key = ?"]
        params = [chat_key]
        if start_ts is not None:
            clauses.append("ts >= ?")
            params.append(int(start_ts))
        if end_ts is not None:
            clauses.append("ts <= ?")
            params.append(int(end_ts))
        sql = (
            "SELECT id, chat_key, message_id, message_seq, ts, user_id, sender_name, "
            "is_bot, reply_to_id, text, has_image, image_urls, source "
            f"FROM messages WHERE {' AND '.join(clauses)} "
            "ORDER BY ts DESC, id DESC LIMIT ?"
        )
        params.append(int(limit))
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
        rows.reverse()  # chronological (oldest -> newest)
        return rows
    except Exception as exc:
        _log(f"_fetch_db_rows failed: {exc}")
        return []
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _find_anchor_row_in_db(chat_key, msg_id, db_path=None):
    conn = None
    try:
        conn = _connect(db_path=db_path)
        row = conn.execute(
            """
            SELECT id, chat_key, message_id, message_seq, ts, user_id, sender_name,
                   is_bot, reply_to_id, text, has_image, image_urls, source
            FROM messages
            WHERE chat_key = ? AND message_id = ?
            LIMIT 1
            """,
            (chat_key, str(msg_id)),
        ).fetchone()
        return dict(row) if row else None
    except Exception:
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _fetch_db_rows_around(chat_key, anchor_row, window=8, db_path=None):
    """Indexed fetch of up to `window` rows before and `window` rows after `anchor_row`.

    Uses `idx_msg_chat_ts (chat_key, ts, id)` to avoid scanning thousands of rows.
    Returns `(before_rows_chronological, after_rows_chronological)`.
    """
    conn = None
    try:
        conn = _connect(db_path=db_path)
        anchor_ts = int(anchor_row.get("ts") or 0)
        anchor_id = anchor_row.get("id")
        anchor_mid = str(anchor_row.get("message_id") or "")
        win = max(1, int(window))

        if anchor_id is not None:
            aid = int(anchor_id)
            before = [
                dict(r)
                for r in conn.execute(
                    """
                    SELECT id, chat_key, message_id, message_seq, ts, user_id, sender_name,
                           is_bot, reply_to_id, text, has_image, image_urls, source
                    FROM messages
                    WHERE chat_key = ? AND ts <= ? AND (ts < ? OR id < ?)
                    ORDER BY ts DESC, id DESC
                    LIMIT ?
                    """,
                    (chat_key, anchor_ts, anchor_ts, aid, win),
                ).fetchall()
            ]
            before.reverse()
            after = [
                dict(r)
                for r in conn.execute(
                    """
                    SELECT id, chat_key, message_id, message_seq, ts, user_id, sender_name,
                           is_bot, reply_to_id, text, has_image, image_urls, source
                    FROM messages
                    WHERE chat_key = ? AND ts >= ? AND (ts > ? OR id > ?)
                    ORDER BY ts ASC, id ASC
                    LIMIT ?
                    """,
                    (chat_key, anchor_ts, anchor_ts, aid, win),
                ).fetchall()
            ]
            return before, after

        before = [
            dict(r)
            for r in conn.execute(
                """
                SELECT id, chat_key, message_id, message_seq, ts, user_id, sender_name,
                       is_bot, reply_to_id, text, has_image, image_urls, source
                FROM messages
                WHERE chat_key = ? AND ts <= ? AND message_id != ?
                ORDER BY ts DESC, id DESC
                LIMIT ?
                """,
                (chat_key, anchor_ts, anchor_mid, win),
            ).fetchall()
        ]
        before.reverse()
        before_mids = {str(r["message_id"]) for r in before}
        raw_after = [
            dict(r)
            for r in conn.execute(
                """
                SELECT id, chat_key, message_id, message_seq, ts, user_id, sender_name,
                       is_bot, reply_to_id, text, has_image, image_urls, source
                FROM messages
                WHERE chat_key = ? AND ts >= ? AND message_id != ?
                ORDER BY ts ASC, id ASC
                LIMIT ?
                """,
                (chat_key, anchor_ts, anchor_mid, win + len(before_mids)),
            ).fetchall()
        ]
        after = [r for r in raw_after if str(r["message_id"]) not in before_mids][:win]
        return before, after
    except Exception as exc:
        _log(f"_fetch_db_rows_around failed: {exc}")
        return [], []
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _fetch_db_recent_before(chat_key, before_row=None, before_ts=None, limit=35, db_path=None):
    """Indexed fetch of up to `limit` latest rows (or rows strictly before `before_row`/`before_ts`).

    Uses `idx_msg_chat_ts (chat_key, ts, id)` and returns rows in chronological order.
    """
    conn = None
    try:
        conn = _connect(db_path=db_path)
        lim = max(1, int(limit))
        if isinstance(before_row, dict) and before_row.get("id") is not None:
            bts = int(before_row.get("ts") or 0)
            bid = int(before_row["id"])
            rows = [
                dict(r)
                for r in conn.execute(
                    """
                    SELECT id, chat_key, message_id, message_seq, ts, user_id, sender_name,
                           is_bot, reply_to_id, text, has_image, image_urls, source
                    FROM messages
                    WHERE chat_key = ? AND ts <= ? AND (ts < ? OR id < ?)
                    ORDER BY ts DESC, id DESC
                    LIMIT ?
                    """,
                    (chat_key, bts, bts, bid, lim),
                ).fetchall()
            ]
        elif before_ts is not None and int(before_ts) > 0:
            rows = [
                dict(r)
                for r in conn.execute(
                    """
                    SELECT id, chat_key, message_id, message_seq, ts, user_id, sender_name,
                           is_bot, reply_to_id, text, has_image, image_urls, source
                    FROM messages
                    WHERE chat_key = ? AND ts < ?
                    ORDER BY ts DESC, id DESC
                    LIMIT ?
                    """,
                    (chat_key, int(before_ts), lim),
                ).fetchall()
            ]
        else:
            rows = [
                dict(r)
                for r in conn.execute(
                    """
                    SELECT id, chat_key, message_id, message_seq, ts, user_id, sender_name,
                           is_bot, reply_to_id, text, has_image, image_urls, source
                    FROM messages
                    WHERE chat_key = ?
                    ORDER BY ts DESC, id DESC
                    LIMIT ?
                    """,
                    (chat_key, lim),
                ).fetchall()
            ]
        rows.reverse()
        return rows
    except Exception as exc:
        _log(f"_fetch_db_recent_before failed: {exc}")
        return []
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _build_quote_map(rows, chat_key, db_path=None):
    qmap = {}
    missing = []
    missing_seen = set()
    for r in rows:
        mid = str(r.get("message_id") or "").strip()
        if mid and not mid.startswith(("sess:", "out:", "in:")):
            qmap[mid] = (str(r.get("sender_name") or r.get("user_id") or ""), str(r.get("text") or ""))
    for r in rows:
        rid = str(r.get("reply_to_id") or "").strip()
        if rid and rid not in qmap and rid not in missing_seen:
            missing_seen.add(rid)
            missing.append(rid)
    if missing:
        batch = list(missing)[:20]
        conn = None
        try:
            conn = _connect(db_path=db_path)
            placeholders = ",".join(["?"] * len(batch))
            found = conn.execute(
                f"SELECT message_id, sender_name, user_id, text FROM messages WHERE chat_key = ? AND message_id IN ({placeholders})",
                [chat_key] + batch,
            ).fetchall()
            for frow in found:
                qmap[str(frow["message_id"])] = (
                    str(frow["sender_name"] or frow["user_id"] or ""),
                    str(frow["text"] or ""),
                )
        except Exception:
            pass
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
    return qmap


def _truncate_lines_to_budget(lines, max_chars=None, keep_tail=False):
    budget = max_chars or HISTORY_RESULT_MAX_CHARS
    if not lines:
        return [], 0
    selected = []
    used = 0
    ordered = list(reversed(lines)) if keep_tail else list(lines)
    for ln in ordered:
        cost = len(ln) + 1
        if selected and used + cost > budget:
            break
        selected.append(ln)
        used += cost
    if keep_tail:
        selected.reverse()
    omitted = len(lines) - len(selected)
    return selected, omitted


def run_query(
    query_spec,
    is_group,
    group_id="",
    dm_user_id="",
    bot_qq_id="",
    admin_qq_ids=None,
    member_map=None,
    napcat_fn=None,
    db_path=None,
    sessions_dir=None,
    max_chars=None,
):
    """Execute one normalized history lookup strictly scoped to the current chat.

    Returns dict:
        {
            "ok": bool,
            "summary": str,       # Formatted text block for the model
            "count": int,         # Number of messages returned
            "image_urls": [str],  # Optional public image URLs (for msg / include_images)
        }
    """
    if not HISTORY_ENABLED:
        return {"ok": False, "summary": "[历史翻查已关闭]", "count": 0, "image_urls": []}

    chat_key = make_chat_key(is_group, group_id=group_id, dm_user_id=dm_user_id)
    if not chat_key:
        return {"ok": False, "summary": "[无法确定当前会话标识]", "count": 0, "image_urls": []}

    q = dict(query_spec or {})
    action = q.get("action", "recent")
    call = napcat_fn or _default_napcat_post
    admins = {str(x).strip() for x in (admin_qq_ids or ()) if str(x).strip()}
    budget = max_chars or HISTORY_RESULT_MAX_CHARS
    image_urls = []

    # ------------------------------------------------------------------
    # Action 1: `msg` — Exact full-text lookup of a single message ID
    # ------------------------------------------------------------------
    if action == "msg":
        mid = str(q.get("msg_id") or "").strip().lstrip("#")
        if not mid:
            return {"ok": False, "summary": "缺少 msg_id 参数，无法查询指定消息。", "count": 0, "image_urls": []}

        target = _find_anchor_row_in_db(chat_key, mid, db_path=db_path)
        if target is None and _MSG_ID_RE.match(mid):
            raw_msg = call("get_msg", {"message_id": int(mid)}, timeout=3.0)
            if isinstance(raw_msg, dict) and _verify_napcat_msg_ownership(
                raw_msg, is_group, group_id, dm_user_id, bot_qq_id
            ):
                rec = onebot_msg_to_record(
                    raw_msg, chat_key=chat_key, bot_qq_id=bot_qq_id, member_map=member_map, source="napcat"
                )
                if rec:
                    upsert_records([rec], db_path=db_path)
                    target = _find_anchor_row_in_db(chat_key, mid, db_path=db_path) or rec

        if target is None:
            return {
                "ok": False,
                "summary": f"未在当前会话找到消息 #{mid}（可能不存在、已过期，或不属于当前聊天）。",
                "count": 0,
                "image_urls": [],
            }

        # Grab tiny surrounding context (+/- 2) if available locally
        ctx_rows = [target]
        if target.get("ts"):
            all_near = _fetch_db_rows(
                chat_key,
                start_ts=int(target["ts"]) - 1800,
                end_ts=int(target["ts"]) + 1800,
                limit=80,
                db_path=db_path,
            )
            idx = next((i for i, r in enumerate(all_near) if str(r["message_id"]) == str(target["message_id"])), -1)
            if idx != -1:
                ctx_rows = all_near[max(0, idx - 2) : min(len(all_near), idx + 3)]

        qmap = _build_quote_map(ctx_rows, chat_key, db_path=db_path)
        lines = []
        for r in ctx_rows:
            is_target = str(r["message_id"]) == str(target["message_id"])
            lines.append(
                _format_row_line(
                    r,
                    bot_qq_id=bot_qq_id,
                    admin_qq_ids=admins,
                    max_text_len=2400 if is_target else 200,
                    highlight=is_target,
                    quote_map=qmap,
                )
            )
        image_urls = _extract_row_image_urls(target)[:2]
        hdr = f"查询指定消息 #{mid}（>>> 为目标消息完整原文）："
        return {
            "ok": True,
            "summary": hdr + "\n" + "\n".join(lines),
            "count": len(ctx_rows),
            "image_urls": image_urls,
        }

    # ------------------------------------------------------------------
    # Action 2: `around` — Conversation window around a message ID
    # ------------------------------------------------------------------
    if action == "around":
        mid = str(q.get("msg_id") or "").strip().lstrip("#")
        window = max(1, min(20, int(q.get("window") or 8)))
        if not mid:
            return {"ok": False, "summary": "缺少 msg_id 参数，无法查看前后上下文。", "count": 0, "image_urls": []}

        target = _find_anchor_row_in_db(chat_key, mid, db_path=db_path)
        if target is None and _MSG_ID_RE.match(mid):
            raw_msg = call("get_msg", {"message_id": int(mid)}, timeout=3.0)
            if isinstance(raw_msg, dict) and _verify_napcat_msg_ownership(
                raw_msg, is_group, group_id, dm_user_id, bot_qq_id
            ):
                rec = onebot_msg_to_record(
                    raw_msg, chat_key=chat_key, bot_qq_id=bot_qq_id, member_map=member_map, source="napcat"
                )
                if rec:
                    upsert_records([rec], db_path=db_path)
                    target = _find_anchor_row_in_db(chat_key, mid, db_path=db_path) or rec

        if target is None:
            # Also try cc-connect session IDs (sess:s1:123)
            if mid.startswith("sess:"):
                srows = _load_cc_session_messages(
                    is_group, group_id=group_id, dm_user_id=dm_user_id, bot_qq_id=bot_qq_id, sessions_dir=sessions_dir
                )
                sidx = next((i for i, r in enumerate(srows) if r["message_id"] == mid), -1)
                if sidx != -1:
                    chunk = srows[max(0, sidx - window) : min(len(srows), sidx + window + 1)]
                    lines = [
                        _format_row_line(
                            r, bot_qq_id=bot_qq_id, admin_qq_ids=admins, max_text_len=360, highlight=(r["message_id"] == mid)
                        )
                        for r in chunk
                    ]
                    trimmed, omitted = _truncate_lines_to_budget(lines, max_chars=budget)
                    note = f"\n（因篇幅限制省略 {omitted} 条）" if omitted else ""
                    return {
                        "ok": True,
                        "summary": f"消息 {mid} 前后上下文（共 {len(trimmed)} 条，>>> 为目标消息）：\n" + "\n".join(trimmed) + note,
                        "count": len(trimmed),
                        "image_urls": [],
                    }
            return {
                "ok": False,
                "summary": f"未在当前会话找到消息 #{mid}（可能不存在或不属于当前聊天）。",
                "count": 0,
                "image_urls": [],
            }

        # Pull surrounding rows via indexed range queries; if too few before target, backfill via NapCat
        before_rows, after_rows = _fetch_db_rows_around(chat_key, target, window=window, db_path=db_path)
        if len(before_rows) < window:
            anchor_seq = int(target.get("message_seq") or 0)
            if anchor_seq <= 0 and _MSG_ID_RE.match(str(target.get("message_id") or "")):
                anchor_seq = max(0, int(target["message_id"]))
            if anchor_seq > 0:
                backfill_from_napcat(
                    is_group,
                    group_id=group_id,
                    dm_user_id=dm_user_id,
                    bot_qq_id=bot_qq_id,
                    member_map=member_map,
                    napcat_fn=call,
                    db_path=db_path,
                    max_pages=2,
                    start_cursor_seq=anchor_seq,
                    include_latest=False,
                    time_budget_secs=2.5,
                )
                refreshed = _find_anchor_row_in_db(chat_key, str(target["message_id"]), db_path=db_path)
                if refreshed is not None:
                    target = refreshed
                before_rows, after_rows = _fetch_db_rows_around(chat_key, target, window=window, db_path=db_path)

        chunk = before_rows + [target] + after_rows

        qmap = _build_quote_map(chunk, chat_key, db_path=db_path)
        lines = [
            _format_row_line(
                r,
                bot_qq_id=bot_qq_id,
                admin_qq_ids=admins,
                max_text_len=500 if str(r["message_id"]) == str(target["message_id"]) else 280,
                highlight=(str(r["message_id"]) == str(target["message_id"])),
                quote_map=qmap,
            )
            for r in chunk
        ]
        trimmed, omitted = _truncate_lines_to_budget(lines, max_chars=budget)
        if q.get("include_images"):
            image_urls = _extract_row_image_urls(target)[:2]

        oldest_id = next(
            (str(r["message_id"]) for r in chunk if _MSG_ID_RE.match(str(r.get("message_id") or ""))),
            "",
        )
        foot_parts = []
        if omitted:
            foot_parts.append(f"因篇幅限制省略 {omitted} 条")
        if oldest_id:
            foot_parts.append(f"如需继续往前翻可用 before_id={oldest_id}")
        foot = ("\n（" + "；".join(foot_parts) + "）") if foot_parts else ""
        return {
            "ok": True,
            "summary": f"消息 #{mid} 前后上下文（共 {len(trimmed)} 条，>>> 为目标消息）：\n" + "\n".join(trimmed) + foot,
            "count": len(trimmed),
            "image_urls": image_urls,
        }

    # ------------------------------------------------------------------
    # Action 3: `recent` — Chronological backward scrolling (with optional before_id)
    # ------------------------------------------------------------------
    if action == "recent":
        limit = max(1, min(50, int(q.get("limit") or 35)))
        before_id = str(q.get("before_id") or "").strip().lstrip("#")

        # Refresh latest from NapCat if needed
        backfill_from_napcat(
            is_group,
            group_id=group_id,
            dm_user_id=dm_user_id,
            bot_qq_id=bot_qq_id,
            member_map=member_map,
            napcat_fn=call,
            db_path=db_path,
            max_pages=1,
            include_latest=True,
            time_budget_secs=2.0,
        )

        b_row = _find_anchor_row_in_db(chat_key, before_id, db_path=db_path) if before_id else None
        anchor_seq = 0
        anchor_ts = 0

        if not before_id:
            earlier = _fetch_db_recent_before(chat_key, limit=limit, db_path=db_path)
        elif b_row is not None:
            earlier = _fetch_db_recent_before(chat_key, before_row=b_row, limit=limit, db_path=db_path)
            anchor_ts = int(b_row.get("ts") or 0)
            anchor_seq = int(b_row.get("message_seq") or 0)
            if anchor_seq <= 0 and _MSG_ID_RE.match(before_id):
                anchor_seq = max(0, int(before_id))
        else:
            earlier = None
            if _MSG_ID_RE.match(before_id):
                raw_b = call("get_msg", {"message_id": int(before_id)}, timeout=2.0)
                if isinstance(raw_b, dict) and _verify_napcat_msg_ownership(
                    raw_b, is_group, group_id, dm_user_id, bot_qq_id
                ):
                    rec_b = onebot_msg_to_record(
                        raw_b, chat_key=chat_key, bot_qq_id=bot_qq_id, member_map=member_map, source="napcat"
                    )
                    if rec_b:
                        upsert_records([rec_b], db_path=db_path)
                        b_row = _find_anchor_row_in_db(chat_key, before_id, db_path=db_path)
                        anchor_seq = int(rec_b.get("message_seq") or 0) or max(0, int(before_id))
                        anchor_ts = int(rec_b.get("ts") or 0)
                else:
                    anchor_seq = max(0, int(before_id))

        # Backfill older pages from NapCat if we don't yet have `limit` messages before cursor
        if earlier is None or len(earlier) < limit:
            backfill_from_napcat(
                is_group,
                group_id=group_id,
                dm_user_id=dm_user_id,
                bot_qq_id=bot_qq_id,
                member_map=member_map,
                napcat_fn=call,
                db_path=db_path,
                max_pages=min(HISTORY_NAPCAT_MAX_PAGES, 4),
                start_cursor_seq=anchor_seq,
                include_latest=False,
                time_budget_secs=3.5,
            )
            if before_id and b_row is None:
                b_row = _find_anchor_row_in_db(chat_key, before_id, db_path=db_path)
            if b_row is not None:
                earlier = _fetch_db_recent_before(chat_key, before_row=b_row, limit=limit, db_path=db_path)
                anchor_ts = int(b_row.get("ts") or anchor_ts or 0)
            elif before_id and anchor_ts > 0:
                earlier = _fetch_db_recent_before(chat_key, before_ts=anchor_ts, limit=limit, db_path=db_path)
            else:
                earlier = _fetch_db_recent_before(chat_key, limit=limit, db_path=db_path)

        # Supplement with cc-connect session archive if still sparse
        if len(earlier) < limit:
            srows = _load_cc_session_messages(
                is_group, group_id=group_id, dm_user_id=dm_user_id, bot_qq_id=bot_qq_id, sessions_dir=sessions_dir
            )
            if before_id:
                cutoff_ts = int((b_row or {}).get("ts") or anchor_ts or 0)
                if cutoff_ts > 0:
                    srows = [r for r in srows if int(r.get("ts") or 0) > 0 and int(r["ts"]) < cutoff_ts]
            merged = _dedup_and_merge_session_rows(earlier, srows)
            merged.sort(key=lambda r: (int(r.get("ts") or 0), int(r.get("id") or 0)))
            earlier = merged

        if not earlier:
            return {
                "ok": True,
                "summary": "已经翻到当前能查到的最早记录，没有更早的聊天记录了。",
                "count": 0,
                "image_urls": [],
            }

        page = earlier[-limit:]
        qmap = _build_quote_map(page, chat_key, db_path=db_path)
        lines = [
            _format_row_line(r, bot_qq_id=bot_qq_id, admin_qq_ids=admins, max_text_len=280, quote_map=qmap)
            for r in page
        ]
        trimmed, omitted = _truncate_lines_to_budget(lines, max_chars=budget, keep_tail=True)
        oldest_real_id = next(
            (str(r["message_id"]) for r in page if _MSG_ID_RE.match(str(r.get("message_id") or ""))),
            "",
        )
        hdr = (
            f"翻看 #{before_id} 之前的连续聊天记录（共 {len(trimmed)} 条，按时间先后排列）："
            if before_id
            else f"近期连续聊天记录（共 {len(trimmed)} 条，按时间先后排列）："
        )
        foot_parts = []
        if omitted:
            foot_parts.append(f"较早 {omitted} 条因字数限制省略")
        if oldest_real_id:
            foot_parts.append(f"如需继续往更早翻，请传 before_id={oldest_real_id}")
        foot = ("\n（" + "；".join(foot_parts) + "）") if foot_parts else ""
        return {
            "ok": True,
            "summary": hdr + "\n" + "\n".join(trimmed) + foot,
            "count": len(trimmed),
            "image_urls": [],
        }

    # ------------------------------------------------------------------
    # Action 4: `time` — Chronological messages in a time window
    # ------------------------------------------------------------------
    if action == "time":
        limit = max(1, min(50, int(q.get("limit") or 40)))
        offset = max(0, int(q.get("offset") or 0))
        start_ts, end_ts = _resolve_time_bounds(q)
        if start_ts is None and end_ts is None:
            # Default to last 6 hours
            start_ts = int(time.time() - 6 * 3600)

        # Backfill NapCat down to start_ts if DB doesn't cover that far back yet
        db_min_ts, _, _, db_cnt = _get_oldest_cursor_in_db(chat_key, db_path=db_path)
        if db_cnt == 0 or (start_ts is not None and (db_min_ts == 0 or db_min_ts > start_ts)):
            backfill_from_napcat(
                is_group,
                group_id=group_id,
                dm_user_id=dm_user_id,
                bot_qq_id=bot_qq_id,
                member_map=member_map,
                napcat_fn=call,
                db_path=db_path,
                max_pages=HISTORY_NAPCAT_MAX_PAGES,
                target_min_ts=int(start_ts or 0),
                include_latest=True,
                time_budget_secs=4.0,
            )

        rows = _fetch_db_rows(chat_key, start_ts=start_ts, end_ts=end_ts, limit=3000, db_path=db_path)
        srows = _load_cc_session_messages(
            is_group, group_id=group_id, dm_user_id=dm_user_id, bot_qq_id=bot_qq_id, sessions_dir=sessions_dir
        )
        if srows:
            srows = [
                r
                for r in srows
                if (start_ts is None or int(r.get("ts") or 0) >= start_ts)
                and (end_ts is None or int(r.get("ts") or 0) <= end_ts)
            ]
            rows = _dedup_and_merge_session_rows(rows, srows)
            rows.sort(key=lambda r: (int(r.get("ts") or 0), int(r.get("id") or 0)))

        sender_filter = q.get("sender") or ""
        if sender_filter:
            rows = [r for r in rows if _sender_matches(r, sender_filter, bot_qq_id=bot_qq_id)]

        total = len(rows)
        if total == 0:
            span = f"{_fmt_ts(start_ts) if start_ts else '最早'} ~ {_fmt_ts(end_ts) if end_ts else '现在'}"
            return {
                "ok": True,
                "summary": f"时间段 {span} 内未查到聊天记录。",
                "count": 0,
                "image_urls": [],
            }

        page = rows[offset : offset + limit]
        qmap = _build_quote_map(page, chat_key, db_path=db_path)
        lines = [
            _format_row_line(r, bot_qq_id=bot_qq_id, admin_qq_ids=admins, max_text_len=280, quote_map=qmap)
            for r in page
        ]
        trimmed, omitted = _truncate_lines_to_budget(lines, max_chars=budget)
        span = f"{_fmt_ts(start_ts) if start_ts else '最早'} ~ {_fmt_ts(end_ts) if end_ts else '现在'}"
        foot_parts = []
        shown_end = offset + len(trimmed)
        if shown_end < total:
            foot_parts.append(f"该时间段共 {total} 条，当前展示第 {offset + 1}~{shown_end} 条，可用 offset={shown_end} 翻下一页")
        foot = ("\n（" + "；".join(foot_parts) + "）") if foot_parts else ""
        return {
            "ok": True,
            "summary": f"时间段 {span} 的聊天记录（共匹配 {total} 条，按时间先后排列）：\n" + "\n".join(trimmed) + foot,
            "count": len(trimmed),
            "image_urls": [],
        }

    # ------------------------------------------------------------------
    # Action 5: `search` — Keyword & sender search across local DB + NapCat + sessions
    # ------------------------------------------------------------------
    query_str = str(q.get("query") or "").strip()
    sender_filter = str(q.get("sender") or "").strip()
    limit = max(1, min(40, int(q.get("limit") or 20)))
    offset = max(0, int(q.get("offset") or 0))
    start_ts, end_ts = _resolve_time_bounds(q)
    terms = _split_search_terms(query_str)

    if not terms and not sender_filter:
        # Degenerate search with neither query nor sender -> treat as recent
        q2 = dict(q)
        q2["action"] = "recent"
        return run_query(
            q2,
            is_group=is_group,
            group_id=group_id,
            dm_user_id=dm_user_id,
            bot_qq_id=bot_qq_id,
            admin_qq_ids=admins,
            member_map=member_map,
            napcat_fn=call,
            db_path=db_path,
            sessions_dir=sessions_dir,
            max_chars=budget,
        )

    # First ensure fresh latest page from NapCat
    backfill_from_napcat(
        is_group,
        group_id=group_id,
        dm_user_id=dm_user_id,
        bot_qq_id=bot_qq_id,
        member_map=member_map,
        napcat_fn=call,
        db_path=db_path,
        max_pages=1,
        include_latest=True,
        time_budget_secs=2.0,
    )

    def _run_local_search():
        db_rows = _fetch_db_rows(chat_key, start_ts=start_ts, end_ts=end_ts, limit=15000, db_path=db_path)
        srows = _load_cc_session_messages(
            is_group, group_id=group_id, dm_user_id=dm_user_id, bot_qq_id=bot_qq_id, sessions_dir=sessions_dir
        )
        if srows:
            srows = [
                r
                for r in srows
                if (start_ts is None or int(r.get("ts") or 0) >= start_ts)
                and (end_ts is None or int(r.get("ts") or 0) <= end_ts)
            ]
            combined = _dedup_and_merge_session_rows(db_rows, srows)
        else:
            combined = db_rows

        strict_hits = []
        partial_hits = []
        for r in combined:
            if sender_filter and not _sender_matches(r, sender_filter, bot_qq_id=bot_qq_id):
                continue
            if not terms:
                strict_hits.append((1, int(r.get("ts") or 0), r))
                continue
            all_ok, hit_cnt = _score_search_row(r, terms)
            if all_ok:
                strict_hits.append((hit_cnt, int(r.get("ts") or 0), r))
            elif hit_cnt > 0:
                partial_hits.append((hit_cnt, int(r.get("ts") or 0), r))
        return strict_hits, partial_hits, len(combined)

    strict_hits, partial_hits, scanned_count = _run_local_search()

    # If few strict hits locally, paginate older history from NapCat and re-search!
    if len(strict_hits) < min(limit, 5) and HISTORY_NAPCAT_MAX_PAGES > 1:
        bf = backfill_from_napcat(
            is_group,
            group_id=group_id,
            dm_user_id=dm_user_id,
            bot_qq_id=bot_qq_id,
            member_map=member_map,
            napcat_fn=call,
            db_path=db_path,
            max_pages=HISTORY_NAPCAT_MAX_PAGES,
            target_min_ts=int(start_ts or 0),
            include_latest=False,
            time_budget_secs=4.0,
        )
        if bf.get("fetched", 0) > 0:
            strict_hits, partial_hits, scanned_count = _run_local_search()

    used_partial = False
    if strict_hits:
        # Sort newest first for paging, then display selected page chronologically
        ranked = sorted(strict_hits, key=lambda x: (x[1], x[0]), reverse=True)
    elif partial_hits:
        used_partial = True
        ranked = sorted(partial_hits, key=lambda x: (x[0], x[1]), reverse=True)
    else:
        ranked = []

    desc_parts = []
    if query_str:
        desc_parts.append(f'关键词="{query_str}"')
    if sender_filter:
        desc_parts.append(f'发言人="{sender_filter}"')
    if start_ts or end_ts:
        desc_parts.append(f"时间={_fmt_ts(start_ts) if start_ts else '最早'}~{_fmt_ts(end_ts) if end_ts else '现在'}")
    desc = "，".join(desc_parts)

    if not ranked:
        return {
            "ok": True,
            "summary": f"搜索条件（{desc}）在已归档的 {scanned_count} 条本会话记录中未找到匹配消息。可尝试换更短的同义词/关键词，或用 action=recent 往前翻。",
            "count": 0,
            "image_urls": [],
        }

    total_hits = len(ranked)
    page_rows = [item[2] for item in ranked[offset : offset + limit]]
    # Present chosen hits in chronological order so reading flow is natural
    page_rows.sort(key=lambda r: (int(r.get("ts") or 0), int(r.get("id") or 0)))

    qmap = _build_quote_map(page_rows, chat_key, db_path=db_path)
    lines = [
        _format_row_line(r, bot_qq_id=bot_qq_id, admin_qq_ids=admins, max_text_len=320, quote_map=qmap)
        for r in page_rows
    ]
    trimmed, omitted = _truncate_lines_to_budget(lines, max_chars=budget, keep_tail=True)

    match_note = "（未找到同时含全部词的记录，以下为部分关键词匹配结果）" if used_partial else ""
    hdr = f"历史搜索（{desc}），共命中 {total_hits} 条，展示 {len(trimmed)} 条{match_note}："
    foot_parts = ["如需看某条命中的完整前后对话，请用 action=around msg_id=消息ID"]
    if offset + len(page_rows) < total_hits:
        foot_parts.append(f"更多更早匹配可用 offset={offset + len(page_rows)}")
    if omitted:
        foot_parts.append(f"有 {omitted} 条因字数限制省略")
    return {
        "ok": True,
        "summary": hdr + "\n" + "\n".join(trimmed) + "\n（" + "；".join(foot_parts) + "）",
        "count": len(trimmed),
        "image_urls": [],
    }
