#!/usr/bin/env python3
"""Persistent per-group / per-chat LLM token usage ledger & analytics engine.

Tracks token consumption across every LLM call site in the QQ bot:
1. Main chat turns & autonomous history lookup follow-up rounds (`agy-acp-wrapper.py`)
   with exact ACP `result.usage` breakdown (`inputTokens`, `cachedReadTokens`,
   `cachedWriteTokens`, `outputTokens`, `thoughtTokens`, `totalTokens`).
2. Background memory extraction & nightly sleep consolidation
   (`memory_engine/consolidate.py`, `memory_engine/sleep.py`) with exact
   `agy --output-format stream-json` `result.usage` attribution to each group/DM.
3. Incremental historical backfill from `data/cc-connect.log` (`auto-compress: decision`
   `estimate_source=exact` paired with `turn complete`), filtering out 0-token
   wrapper fast-silent short-circuits and automatically deduplicating against live
   exact records so existing groups show real token spend immediately.

Storage: SQLite WAL ledger (`data/token_usage.db`), safe for concurrent multi-process
writes (one `agy-acp-wrapper.py` process per live session + background memory threads +
WebUI reader). Python 3.9 standard library only.
"""

from __future__ import annotations

import collections
import datetime
import json
import os
import re
import sqlite3
import sys
import threading
import time
from typing import Any

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

import bot_env  # noqa: E402,F401

DEFAULT_DB_PATH = os.path.join(_SCRIPT_DIR, "data", "token_usage.db")
DEFAULT_CC_LOG_PATH = os.path.join(_SCRIPT_DIR, "data", "cc-connect.log")
DEFAULT_SESSIONS_DIR = os.path.join(_SCRIPT_DIR, "data", "sessions")

TOKEN_USAGE_DB_PATH = (
    os.environ.get("TOKEN_USAGE_DB", "").strip() or DEFAULT_DB_PATH
)

_SCHEMA_INIT_LOCK = threading.Lock()
_INITIALIZED_DBS: set[str] = set()
_SYNC_LOCK = threading.RLock()
_LAST_AUTO_SYNC_BY_DB: dict[str, float] = {}
_AUTO_SYNC_MIN_INTERVAL_SEC = 5.0

_LOGFMT_TOKEN_RE = re.compile(r'([A-Za-z0-9_]+)=("(?:\\.|[^"\\])*"|\S+)')
_GO_DUR_PART_RE = re.compile(r"(\d+(?:\.\d+)?)(ns|µs|us|ms|s|m|h)")
_GROUP_KEY_RE = re.compile(r"^(?:qq:)?(?:g|group):(\d+)$", re.IGNORECASE)
_DM_KEY_RE = re.compile(r"^(?:qq|dm|u|user|private):(\d+)$", re.IGNORECASE)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS token_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    day_key TEXT NOT NULL,
    hour_key TEXT NOT NULL,
    session_key TEXT NOT NULL,
    chat_type TEXT NOT NULL,
    group_id TEXT NOT NULL DEFAULT '',
    sender_id TEXT NOT NULL DEFAULT '',
    sender_name TEXT NOT NULL DEFAULT '',
    agent_session_id TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL,
    precision TEXT NOT NULL DEFAULT 'exact',
    model TEXT NOT NULL DEFAULT '',
    thought_level TEXT NOT NULL DEFAULT '',
    input_tokens INTEGER NOT NULL DEFAULT 0,
    cached_read_tokens INTEGER NOT NULL DEFAULT 0,
    cached_write_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    thought_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    context_used INTEGER NOT NULL DEFAULT 0,
    context_size INTEGER NOT NULL DEFAULT 0,
    is_silent INTEGER NOT NULL DEFAULT 0,
    is_ambient INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    response_len INTEGER NOT NULL DEFAULT 0,
    external_key TEXT UNIQUE
);

CREATE INDEX IF NOT EXISTS idx_te_ts ON token_events(ts);
CREATE INDEX IF NOT EXISTS idx_te_session_ts ON token_events(session_key, ts);
CREATE INDEX IF NOT EXISTS idx_te_group_ts ON token_events(group_id, ts);
CREATE INDEX IF NOT EXISTS idx_te_day ON token_events(day_key);
CREATE INDEX IF NOT EXISTS idx_te_source ON token_events(source, ts);
CREATE INDEX IF NOT EXISTS idx_te_asid_ts ON token_events(agent_session_id, ts);

CREATE TABLE IF NOT EXISTS sync_state (
    key TEXT PRIMARY KEY,
    val TEXT NOT NULL
);
"""


# ---------------------------------------------------------------------------
# Helpers: Normalization & Formatting
# ---------------------------------------------------------------------------

def _resolve_db_path(db_path: str | None = None) -> str:
    raw = str(db_path or "").strip()
    if raw:
        return raw
    env_p = os.environ.get("TOKEN_USAGE_DB", "").strip()
    return env_p or TOKEN_USAGE_DB_PATH


def _safe_int(val: Any, default: int = 0) -> int:
    if val is None or isinstance(val, bool):
        return default
    try:
        n = int(float(val))
        return n if n >= 0 else default
    except (TypeError, ValueError):
        return default


def normalize_chat_identity(
    session_key: str = "",
    chat_key: str = "",
    is_group: bool | None = None,
    group_id: str = "",
    dm_user_id: str = "",
    source: str = "",
) -> tuple[str, str, str]:
    """Return canonical `(session_key, chat_type, group_id)`.

    Examples:
        `qq:g:12345` / `g:12345` / `group_id="12345"` -> `("qq:g:12345", "group", "12345")`
        `qq:67890` / `dm:67890` / `dm_user_id="67890"` -> `("qq:67890", "dm", "")`
        memory sleep / global -> `("system:memory_sleep", "system", "")`
    """
    gid = re.sub(r"\D", "", str(group_id or "").strip())
    duid = re.sub(r"\D", "", str(dm_user_id or "").strip())

    for raw in (session_key, chat_key):
        s = str(raw or "").strip()
        if not s:
            continue
        mg = _GROUP_KEY_RE.match(s)
        if mg:
            g = mg.group(1)
            return f"qq:g:{g}", "group", g
        md = _DM_KEY_RE.match(s)
        if md:
            u = md.group(1)
            return f"qq:{u}", "dm", ""
        if s.startswith("system:"):
            return s, "system", ""

    if is_group is True and gid:
        return f"qq:g:{gid}", "group", gid
    if is_group is False and duid:
        return f"qq:{duid}", "dm", ""
    if gid:
        return f"qq:g:{gid}", "group", gid
    if duid:
        return f"qq:{duid}", "dm", ""

    fallback_raw = str(session_key or chat_key or "").strip()
    if fallback_raw:
        return fallback_raw, "system", ""
    if source == "memory_sleep":
        return "system:memory_sleep", "system", ""
    return "system:global", "system", ""


def extract_usage_fields(usage: Any) -> dict[str, int]:
    """Normalize ACP camelCase or stream-json snake_case usage dict."""
    if not isinstance(usage, dict):
        return {
            "input_tokens": 0,
            "cached_read_tokens": 0,
            "cached_write_tokens": 0,
            "output_tokens": 0,
            "thought_tokens": 0,
            "total_tokens": 0,
        }

    inp = _safe_int(
        usage.get("inputTokens")
        if usage.get("inputTokens") is not None
        else usage.get("input_tokens")
        if usage.get("input_tokens") is not None
        else usage.get("promptTokenCount"),
        0,
    )
    cached_read = _safe_int(
        usage.get("cachedReadTokens")
        if usage.get("cachedReadTokens") is not None
        else usage.get("cache_read_tokens")
        if usage.get("cache_read_tokens") is not None
        else usage.get("cached_read_tokens")
        if usage.get("cached_read_tokens") is not None
        else usage.get("cachedContentTokenCount"),
        0,
    )
    cached_write = _safe_int(
        usage.get("cachedWriteTokens")
        if usage.get("cachedWriteTokens") is not None
        else usage.get("cache_write_tokens")
        if usage.get("cache_write_tokens") is not None
        else usage.get("cached_write_tokens"),
        0,
    )
    out = _safe_int(
        usage.get("outputTokens")
        if usage.get("outputTokens") is not None
        else usage.get("output_tokens")
        if usage.get("output_tokens") is not None
        else usage.get("candidatesTokenCount"),
        0,
    )
    thought = _safe_int(
        usage.get("thoughtTokens")
        if usage.get("thoughtTokens") is not None
        else usage.get("thinking_tokens")
        if usage.get("thinking_tokens") is not None
        else usage.get("thought_tokens")
        if usage.get("thought_tokens") is not None
        else usage.get("thoughtsTokenCount"),
        0,
    )
    total = _safe_int(
        usage.get("totalTokens")
        if usage.get("totalTokens") is not None
        else usage.get("total_tokens")
        if usage.get("total_tokens") is not None
        else usage.get("totalTokenCount"),
        0,
    )
    summed = inp + cached_read + cached_write + out
    if total <= 0:
        total = summed
    return {
        "input_tokens": inp,
        "cached_read_tokens": cached_read,
        "cached_write_tokens": cached_write,
        "output_tokens": out,
        "thought_tokens": thought,
        "total_tokens": total,
    }


def _ts_to_local_keys(ts: float) -> tuple[str, str]:
    dt = datetime.datetime.fromtimestamp(ts).astimezone()
    return dt.strftime("%Y-%m-%d"), dt.strftime("%Y-%m-%dT%H:00")


def _ts_to_iso(ts: float) -> str:
    if not ts or ts <= 0:
        return ""
    try:
        return datetime.datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")
    except Exception:
        return ""


def _parse_iso_ts(raw: str) -> float | None:
    s = str(raw or "").strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    # Python 3.9 fromisoformat handles up to 6 fractional digits; trim nanoseconds
    s = re.sub(r"(\.\d{6})\d+", r"\1", s)
    try:
        dt = datetime.datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.astimezone()
        return dt.timestamp()
    except Exception:
        return None


def _parse_go_duration_sec(raw: str) -> float:
    s = str(raw or "").strip()
    if not s:
        return 0.0
    total = 0.0
    for val_s, unit in _GO_DUR_PART_RE.findall(s):
        v = float(val_s)
        if unit == "ns":
            total += v * 1e-9
        elif unit in ("µs", "us"):
            total += v * 1e-6
        elif unit == "ms":
            total += v * 1e-3
        elif unit == "s":
            total += v
        elif unit == "m":
            total += v * 60.0
        elif unit == "h":
            total += v * 3600.0
    return total


def _unescape_logfmt_val(val: str) -> str:
    if len(val) >= 2 and val[0] == '"' and val[-1] == '"':
        try:
            return str(json.loads(val))
        except Exception:
            return val[1:-1]
    return val


# ---------------------------------------------------------------------------
# SQLite Connection Management
# ---------------------------------------------------------------------------

def _open_conn(db_path: str | None = None, readonly: bool = False) -> sqlite3.Connection:
    path = _resolve_db_path(db_path)
    if not readonly:
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
    ensure_db(path)
    conn = sqlite3.connect(path, timeout=5.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA temp_store=MEMORY")
    return conn


def ensure_db(db_path: str | None = None) -> str:
    path = _resolve_db_path(db_path)
    norm = os.path.abspath(path)
    if norm in _INITIALIZED_DBS and os.path.isfile(norm):
        return path
    with _SCHEMA_INIT_LOCK:
        if norm in _INITIALIZED_DBS and os.path.isfile(norm):
            return path
        parent = os.path.dirname(norm)
        if parent:
            os.makedirs(parent, exist_ok=True)
        conn = sqlite3.connect(path, timeout=5.0)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.executescript(_SCHEMA_SQL)
            conn.commit()
        finally:
            conn.close()
        _INITIALIZED_DBS.add(norm)
    return path


# ---------------------------------------------------------------------------
# Write Path (Live Instrumentation)
# ---------------------------------------------------------------------------

def record_usage(
    *,
    usage: dict[str, Any] | None = None,
    session_key: str = "",
    chat_key: str = "",
    is_group: bool | None = None,
    group_id: str = "",
    dm_user_id: str = "",
    sender_id: str = "",
    sender_name: str = "",
    agent_session_id: str = "",
    source: str = "chat",
    model: str = "",
    thought_level: str = "",
    context_used: int = 0,
    context_size: int = 0,
    is_silent: bool = False,
    is_ambient: bool = False,
    duration_ms: int = 0,
    response_len: int = 0,
    ts: float | None = None,
    external_key: str | None = None,
    precision: str | None = None,
    db_path: str | None = None,
) -> bool:
    """Persist one LLM call's token usage to SQLite. Never raises."""
    try:
        u = extract_usage_fields(usage)
        ctx_used = _safe_int(context_used, 0)
        ctx_size = _safe_int(context_size, 0)

        total = u["total_tokens"]
        prec = str(precision or "").strip()
        if total > 0:
            if not prec:
                has_split = (
                    u["input_tokens"] > 0
                    or u["output_tokens"] > 0
                    or u["cached_read_tokens"] > 0
                )
                prec = "exact" if has_split else "log_total"
        elif ctx_used > 0:
            total = ctx_used
            prec = prec or "log_total"
        else:
            return False

        if ctx_used <= 0 and source in ("chat", "history_followup"):
            ctx_used = total

        canon_key, chat_type, canon_gid = normalize_chat_identity(
            session_key=session_key,
            chat_key=chat_key,
            is_group=is_group,
            group_id=group_id,
            dm_user_id=dm_user_id,
            source=source,
        )
        now_ts = float(ts) if ts is not None and float(ts) > 0 else time.time()
        day_key, hour_key = _ts_to_local_keys(now_ts)

        row = (
            now_ts,
            day_key,
            hour_key,
            canon_key,
            chat_type,
            canon_gid,
            str(sender_id or "").strip(),
            str(sender_name or "").strip()[:80],
            str(agent_session_id or "").strip()[:120],
            str(source or "chat").strip(),
            prec or "exact",
            str(model or "").strip()[:80],
            str(thought_level or "").strip()[:32],
            u["input_tokens"],
            u["cached_read_tokens"],
            u["cached_write_tokens"],
            u["output_tokens"],
            u["thought_tokens"],
            total,
            ctx_used,
            ctx_size,
            1 if is_silent else 0,
            1 if is_ambient else 0,
            _safe_int(duration_ms, 0),
            _safe_int(response_len, 0),
            external_key,
        )

        for attempt in range(3):
            conn = None
            try:
                conn = _open_conn(db_path)
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    """
                    INSERT OR IGNORE INTO token_events (
                        ts, day_key, hour_key, session_key, chat_type, group_id,
                        sender_id, sender_name, agent_session_id, source, precision,
                        model, thought_level, input_tokens, cached_read_tokens,
                        cached_write_tokens, output_tokens, thought_tokens, total_tokens,
                        context_used, context_size, is_silent, is_ambient, duration_ms,
                        response_len, external_key
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    row,
                )
                conn.execute("COMMIT")
                return True
            except sqlite3.OperationalError:
                if conn is not None:
                    try:
                        conn.execute("ROLLBACK")
                    except Exception:
                        pass
                if attempt < 2:
                    time.sleep(0.05 * (attempt + 1))
                else:
                    return False
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
    except Exception:
        return False
    return False


# ---------------------------------------------------------------------------
# Incremental Historical Backfill from cc-connect.log
# ---------------------------------------------------------------------------

def _load_sid_to_key_map(sessions_dir: str) -> dict[str, str]:
    sid_to_key: dict[str, str] = {}
    if not sessions_dir or not os.path.isdir(sessions_dir):
        return sid_to_key
    try:
        for fn in sorted(os.listdir(sessions_dir)):
            if not fn.endswith(".json"):
                continue
            fpath = os.path.join(sessions_dir, fn)
            try:
                with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                    data = json.load(f)
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            active = data.get("active_session") or {}
            if isinstance(active, dict):
                for skey, sid in active.items():
                    if sid and skey:
                        sid_to_key[str(sid)] = str(skey)
            user_sess = data.get("user_sessions") or {}
            if isinstance(user_sess, dict):
                for skey, sids in user_sess.items():
                    if isinstance(sids, list):
                        for sid in sids:
                            if sid and skey:
                                sid_to_key.setdefault(str(sid), str(skey))
    except OSError:
        pass
    return sid_to_key


def _sync_state_get(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT val FROM sync_state WHERE key = ?", (key,)).fetchone()
    return str(row["val"]) if row is not None else default


def _sync_state_set(conn: sqlite3.Connection, key: str, val: str) -> None:
    conn.execute(
        "INSERT INTO sync_state(key, val) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET val = excluded.val",
        (key, str(val)),
    )


def sync_from_cc_log(
    *,
    cc_log_path: str | None = None,
    sessions_dir: str | None = None,
    db_path: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Incrementally backfill real turns from `cc-connect.log` into `token_events`.

    - Pairs `auto-compress: decision` (`estimate_source=exact`) with the matching
      `turn complete` line within 1.5s.
    - Filters out 0-token wrapper fast-silent short-circuits (`silent=true` and
      `turn_duration < 0.8s` with unchanged/absent context estimate).
    - Automatically skips turns already recorded with exact precision by live
      `agy-acp-wrapper.py` (never double-counts).
    """
    log_path = str(cc_log_path or "").strip() or DEFAULT_CC_LOG_PATH
    sess_dir = str(sessions_dir or "").strip() or DEFAULT_SESSIONS_DIR
    resolved_db = _resolve_db_path(db_path)

    if not os.path.isfile(log_path):
        return {"ok": True, "inserted": 0, "skipped_fast_silent": 0, "skipped_exact_dup": 0}

    with _SYNC_LOCK:
        try:
            st = os.stat(log_path)
            cur_size = int(st.st_size)
            cur_inode = f"{getattr(st, 'st_dev', 0)}:{getattr(st, 'st_ino', 0)}"
        except OSError:
            return {"ok": False, "inserted": 0, "skipped_fast_silent": 0, "skipped_exact_dup": 0}

        conn = _open_conn(resolved_db)
        try:
            saved_path = _sync_state_get(conn, "cc_log_path", "")
            saved_inode = _sync_state_get(conn, "cc_log_inode", "")
            saved_offset = _safe_int(_sync_state_get(conn, "cc_log_offset", "0"), 0)

            reset = (
                force
                or saved_path != os.path.abspath(log_path)
                or (saved_inode and saved_inode != cur_inode)
                or cur_size < saved_offset
            )
            if reset:
                offset = 0
                msg_meta: dict[str, dict[str, str]] = {}
                prev_est_by_asid: dict[str, int] = {}
            else:
                offset = saved_offset
                if cur_size == offset:
                    return {
                        "ok": True,
                        "inserted": 0,
                        "skipped_fast_silent": 0,
                        "skipped_exact_dup": 0,
                        "unchanged": True,
                    }
                try:
                    msg_meta = json.loads(_sync_state_get(conn, "cc_log_msg_meta", "{}"))
                    if not isinstance(msg_meta, dict):
                        msg_meta = {}
                except Exception:
                    msg_meta = {}
                try:
                    raw_prev = json.loads(_sync_state_get(conn, "cc_log_prev_est", "{}"))
                    prev_est_by_asid = {
                        str(k): int(v) for k, v in raw_prev.items()
                    } if isinstance(raw_prev, dict) else {}
                except Exception:
                    prev_est_by_asid = {}

            sid_to_key = _load_sid_to_key_map(sess_dir)

            with open(log_path, "rb") as f:
                f.seek(offset)
                raw_chunk = f.read()
                new_offset = f.tell()

            if not raw_chunk:
                return {"ok": True, "inserted": 0, "skipped_fast_silent": 0, "skipped_exact_dup": 0}

            # Keep any trailing incomplete line for the next incremental pass
            last_nl = raw_chunk.rfind(b"\n")
            if last_nl == -1:
                return {"ok": True, "inserted": 0, "skipped_fast_silent": 0, "skipped_exact_dup": 0}
            if last_nl < len(raw_chunk) - 1:
                consumed = raw_chunk[: last_nl + 1]
                new_offset = offset + len(consumed)
            else:
                consumed = raw_chunk

            lines = consumed.decode("utf-8", errors="replace").splitlines()

            # Load timestamps/keys of existing exact live chat events in the covered range
            # so we never double-count turns that agy-acp-wrapper.py already recorded.
            pending_ac: collections.deque[tuple[int, float, int]] = collections.deque(maxlen=16)
            candidates: list[dict[str, Any]] = []
            skipped_fast_silent = 0

            for idx, line in enumerate(lines):
                if 'msg="message received"' in line:
                    pairs = dict(_LOGFMT_TOKEN_RE.findall(line))
                    mid = _unescape_logfmt_val(pairs.get("msg_id", ""))
                    if mid:
                        msg_meta[str(mid)] = {
                            "session_key": _unescape_logfmt_val(pairs.get("session", "")),
                            "user": _unescape_logfmt_val(pairs.get("user", "")),
                        }
                elif 'msg="auto-compress: decision"' in line:
                    pairs = dict(_LOGFMT_TOKEN_RE.findall(line))
                    est_src = _unescape_logfmt_val(pairs.get("estimate_source", ""))
                    if est_src != "exact":
                        continue
                    est = _safe_int(_unescape_logfmt_val(pairs.get("estimate", "0")), 0)
                    ts_val = _parse_iso_ts(_unescape_logfmt_val(pairs.get("time", "")))
                    if est > 0 and ts_val is not None:
                        pending_ac.append((idx, ts_val, est))
                elif 'msg="turn complete"' in line and "msg_id=" in line:
                    pairs = dict(_LOGFMT_TOKEN_RE.findall(line))
                    ts_str = _unescape_logfmt_val(pairs.get("time", ""))
                    ts_val = _parse_iso_ts(ts_str)
                    if ts_val is None:
                        continue
                    sid = _unescape_logfmt_val(pairs.get("session", ""))
                    asid = _unescape_logfmt_val(pairs.get("agent_session", ""))
                    mid = _unescape_logfmt_val(pairs.get("msg_id", ""))
                    dur_s = _parse_go_duration_sec(_unescape_logfmt_val(pairs.get("turn_duration", "")))
                    resp_len = _safe_int(_unescape_logfmt_val(pairs.get("response_len", "0")), 0)
                    silent = _unescape_logfmt_val(pairs.get("silent", "false")).lower() == "true"

                    # Match the earliest pending auto-compress decision within 1.5s and 10 lines
                    matched_est = 0
                    while pending_ac:
                        ac_idx, ac_ts, ac_est = pending_ac[0]
                        if (ts_val - ac_ts) > 2.0 or (idx - ac_idx) > 12:
                            pending_ac.popleft()
                            continue
                        if -0.5 <= (ts_val - ac_ts) <= 2.0:
                            pending_ac.popleft()
                            matched_est = ac_est
                        break

                    if matched_est <= 0:
                        continue

                    prev_est = prev_est_by_asid.get(asid) if asid else None
                    # Wrapper fast-silent filter responds in <0.15s with stale cached context estimate
                    is_fast_silent = silent and dur_s < 0.8 and (prev_est is None or matched_est == prev_est)
                    if is_fast_silent:
                        skipped_fast_silent += 1
                        continue

                    if asid:
                        prev_est_by_asid[asid] = matched_est

                    meta = msg_meta.get(mid) or {}
                    raw_skey = meta.get("session_key") or sid_to_key.get(sid) or ""
                    if not raw_skey:
                        continue
                    canon_key, chat_type, canon_gid = normalize_chat_identity(session_key=raw_skey)
                    candidates.append(
                        {
                            "ts": ts_val,
                            "session_key": canon_key,
                            "chat_type": chat_type,
                            "group_id": canon_gid,
                            "sender_name": str(meta.get("user") or "")[:80],
                            "agent_session_id": asid[:120],
                            "total_tokens": matched_est,
                            "context_used": matched_est,
                            "is_silent": 1 if silent else 0,
                            "duration_ms": int(round(dur_s * 1000.0)),
                            "response_len": resp_len,
                            "external_key": f"cclog:{ts_str}:{sid}:{mid}",
                        }
                    )

            inserted = 0
            skipped_exact_dup = 0

            # Trim state caches to keep sync_state compact
            if len(msg_meta) > 1200:
                keep_keys = list(msg_meta.keys())[-800:]
                msg_meta = {k: msg_meta[k] for k in keep_keys}
            if len(prev_est_by_asid) > 400:
                keep_asids = list(prev_est_by_asid.keys())[-200:]
                prev_est_by_asid = {k: prev_est_by_asid[k] for k in keep_asids}

            conn.execute("BEGIN IMMEDIATE")
            try:
                if candidates:
                    min_ts = min(c["ts"] for c in candidates) - 35.0
                    max_ts = max(c["ts"] for c in candidates) + 15.0
                    exact_rows = conn.execute(
                        """
                        SELECT session_key, agent_session_id, ts, total_tokens
                        FROM token_events
                        WHERE source IN ('chat', 'history_followup')
                          AND precision = 'exact'
                          AND ts >= ? AND ts <= ?
                        """,
                        (min_ts, max_ts),
                    ).fetchall()
                    exact_by_key: dict[str, list[tuple[str, float, int]]] = {}
                    for er in exact_rows:
                        exact_by_key.setdefault(str(er["session_key"]), []).append(
                            (str(er["agent_session_id"] or ""), float(er["ts"]), int(er["total_tokens"]))
                        )

                    for c in candidates:
                        # Check if live wrapper already captured this exact turn
                        is_dup = False
                        for ex_asid, ex_ts, ex_tot in exact_by_key.get(c["session_key"], ()):
                            dt_diff = c["ts"] - ex_ts
                            if -10.0 <= dt_diff <= 30.0:
                                if (c["agent_session_id"] and ex_asid == c["agent_session_id"]) or (
                                    ex_tot == c["total_tokens"]
                                ):
                                    is_dup = True
                                    break
                        if is_dup:
                            skipped_exact_dup += 1
                            continue

                        day_key, hour_key = _ts_to_local_keys(c["ts"])
                        cur = conn.execute(
                            """
                            INSERT OR IGNORE INTO token_events (
                                ts, day_key, hour_key, session_key, chat_type, group_id,
                                sender_id, sender_name, agent_session_id, source, precision,
                                model, thought_level, input_tokens, cached_read_tokens,
                                cached_write_tokens, output_tokens, thought_tokens, total_tokens,
                                context_used, context_size, is_silent, is_ambient, duration_ms,
                                response_len, external_key
                            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                            """,
                            (
                                c["ts"],
                                day_key,
                                hour_key,
                                c["session_key"],
                                c["chat_type"],
                                c["group_id"],
                                "",
                                c["sender_name"],
                                c["agent_session_id"],
                                "log_backfill",
                                "log_total",
                                "",
                                "",
                                0,
                                0,
                                0,
                                0,
                                0,
                                c["total_tokens"],
                                c["context_used"],
                                0,
                                c["is_silent"],
                                1 if c["is_silent"] and c["chat_type"] == "group" else 0,
                                c["duration_ms"],
                                c["response_len"],
                                c["external_key"],
                            ),
                        )
                        if cur.rowcount > 0:
                            inserted += 1

                _sync_state_set(conn, "cc_log_path", os.path.abspath(log_path))
                _sync_state_set(conn, "cc_log_inode", cur_inode)
                _sync_state_set(conn, "cc_log_offset", str(new_offset))
                _sync_state_set(conn, "cc_log_msg_meta", json.dumps(msg_meta, ensure_ascii=False))
                _sync_state_set(conn, "cc_log_prev_est", json.dumps(prev_est_by_asid, ensure_ascii=False))
                _sync_state_set(conn, "cc_log_last_sync", str(time.time()))
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

            return {
                "ok": True,
                "inserted": inserted,
                "skipped_fast_silent": skipped_fast_silent,
                "skipped_exact_dup": skipped_exact_dup,
                "offset": new_offset,
                "file_size": cur_size,
            }
        finally:
            conn.close()


def _maybe_auto_sync(
    db_path: str | None = None,
    cc_log_path: str | None = None,
    sessions_dir: str | None = None,
    force: bool = False,
) -> None:
    resolved_db = os.path.abspath(_resolve_db_path(db_path))
    now_m = time.monotonic()
    last = _LAST_AUTO_SYNC_BY_DB.get(resolved_db, 0.0)
    if not force and last > 0.0 and (now_m - last) < _AUTO_SYNC_MIN_INTERVAL_SEC:
        return
    with _SYNC_LOCK:
        now_m2 = time.monotonic()
        last2 = _LAST_AUTO_SYNC_BY_DB.get(resolved_db, 0.0)
        if not force and last2 > 0.0 and (now_m2 - last2) < _AUTO_SYNC_MIN_INTERVAL_SEC:
            return
        try:
            sync_from_cc_log(
                cc_log_path=cc_log_path,
                sessions_dir=sessions_dir,
                db_path=resolved_db,
                force=False,
            )
        except Exception:
            pass
        _LAST_AUTO_SYNC_BY_DB[resolved_db] = time.monotonic()


# ---------------------------------------------------------------------------
# Read / Analytics Engine
# ---------------------------------------------------------------------------

def _resolve_time_window(range_key: str, now_ts: float | None = None) -> dict[str, Any]:
    rk = str(range_key or "24h").strip().lower()
    if rk not in ("today", "24h", "7d", "30d", "all"):
        rk = "24h"
    now_epoch = float(now_ts) if now_ts is not None else time.time()
    now_local = datetime.datetime.fromtimestamp(now_epoch).astimezone()

    if rk == "today":
        start_dt = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        since_ts = start_dt.timestamp()
        label = "今日"
        granularity = "hour"
    elif rk == "24h":
        since_ts = now_epoch - 24 * 3600
        label = "近 24 小时"
        granularity = "hour"
    elif rk == "7d":
        start_dt = (now_local - datetime.timedelta(days=6)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        since_ts = start_dt.timestamp()
        label = "近 7 天"
        granularity = "day"
    elif rk == "30d":
        start_dt = (now_local - datetime.timedelta(days=29)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        since_ts = start_dt.timestamp()
        label = "近 30 天"
        granularity = "day"
    else:
        since_ts = 0.0
        label = "全部累计"
        granularity = "day"

    return {
        "range": rk,
        "label": label,
        "since_ts": since_ts,
        "until_ts": now_epoch,
        "now_local": now_local,
        "granularity": granularity,
    }


def _resolve_chat_display(
    session_key: str,
    chat_type: str,
    group_id: str,
    group_map: dict[str, str] | None = None,
    user_meta: dict[str, Any] | None = None,
    mem_names: dict[str, str] | None = None,
) -> tuple[str, str]:
    """Return `(display_name, target_id)`."""
    gmap = group_map or {}
    umeta = user_meta or {}
    mnames = mem_names or {}

    if chat_type == "group":
        gid = group_id
        if not gid:
            m = _GROUP_KEY_RE.match(session_key)
            gid = m.group(1) if m else ""
        name = gmap.get(gid) or (umeta.get(session_key) or {}).get("chat_name") or (f"QQ 群 {gid}" if gid else session_key)
        return str(name), gid

    if chat_type == "dm":
        m = _DM_KEY_RE.match(session_key)
        uid = m.group(1) if m else ""
        uname = (
            (umeta.get(session_key) or {}).get("user_name")
            or mnames.get(uid)
            or (f"QQ {uid}" if uid else session_key)
        )
        return str(uname), uid

    if session_key == "system:memory_sleep":
        return "夜间记忆整理 (Sleep)", ""
    return "系统后台任务", ""


def _build_trend_buckets(
    conn: sqlite3.Connection,
    win: dict[str, Any],
    where_sql: str,
    params: list[Any],
) -> list[dict[str, Any]]:
    gran = win["granularity"]
    now_local: datetime.datetime = win["now_local"]
    rk = win["range"]

    bucket_col = "hour_key" if gran == "hour" else "day_key"
    rows = conn.execute(
        f"""
        SELECT
            {bucket_col} AS bkey,
            COUNT(*) AS calls,
            COALESCE(SUM(total_tokens), 0) AS total_tokens,
            COALESCE(SUM(CASE WHEN source IN ('memory_extract', 'memory_sleep') THEN total_tokens ELSE 0 END), 0) AS memory_tokens,
            COALESCE(SUM(CASE WHEN source NOT IN ('memory_extract', 'memory_sleep') AND is_silent = 1 THEN total_tokens ELSE 0 END), 0) AS silent_tokens,
            COALESCE(SUM(CASE WHEN source NOT IN ('memory_extract', 'memory_sleep') AND is_silent = 0 THEN total_tokens ELSE 0 END), 0) AS reply_tokens
        FROM token_events
        WHERE {where_sql}
        GROUP BY {bucket_col}
        ORDER BY {bucket_col} ASC
        """,
        params,
    ).fetchall()

    by_key = {
        str(r["bkey"]): {
            "calls": int(r["calls"] or 0),
            "total_tokens": int(r["total_tokens"] or 0),
            "reply_tokens": int(r["reply_tokens"] or 0),
            "silent_tokens": int(r["silent_tokens"] or 0),
            "memory_tokens": int(r["memory_tokens"] or 0),
        }
        for r in rows
    }

    keys: list[tuple[str, str]] = []
    if gran == "hour":
        if rk == "today":
            start_h = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
            cur_h = now_local.replace(minute=0, second=0, microsecond=0)
            h = start_h
            while h <= cur_h:
                keys.append((h.strftime("%Y-%m-%dT%H:00"), h.strftime("%H:00")))
                h += datetime.timedelta(hours=1)
        else:
            cur_h = now_local.replace(minute=0, second=0, microsecond=0)
            for off in range(23, -1, -1):
                h = cur_h - datetime.timedelta(hours=off)
                keys.append((h.strftime("%Y-%m-%dT%H:00"), h.strftime("%H:00")))
    else:
        if rk == "7d":
            days_n = 7
        elif rk == "30d":
            days_n = 30
        else:
            # 'all': span from earliest bucket to today (at least 7 days, capped at 90)
            if by_key:
                earliest = min(by_key.keys())
                try:
                    ed = datetime.date.fromisoformat(earliest)
                    today_d = now_local.date()
                    span = (today_d - ed).days + 1
                    days_n = max(7, min(90, span))
                except Exception:
                    days_n = 14
            else:
                days_n = 7
        today_d = now_local.date()
        for off in range(days_n - 1, -1, -1):
            d = today_d - datetime.timedelta(days=off)
            keys.append((d.strftime("%Y-%m-%d"), d.strftime("%m-%d")))

    result = []
    for bk, lbl in keys:
        d = by_key.get(bk) or {}
        result.append(
            {
                "key": bk,
                "label": lbl,
                "calls": int(d.get("calls", 0)),
                "total_tokens": int(d.get("total_tokens", 0)),
                "reply_tokens": int(d.get("reply_tokens", 0)),
                "silent_tokens": int(d.get("silent_tokens", 0)),
                "memory_tokens": int(d.get("memory_tokens", 0)),
            }
        )
    return result


def _calc_cache_hit_rate(input_tokens: int, cached_read_tokens: int) -> float | None:
    denom = int(input_tokens) + int(cached_read_tokens)
    if denom <= 0:
        return None
    return round((int(cached_read_tokens) / denom) * 100.0, 1)


def get_overview_summary(
    *,
    group_map: dict[str, str] | None = None,
    user_meta: dict[str, Any] | None = None,
    mem_names: dict[str, str] | None = None,
    db_path: str | None = None,
    cc_log_path: str | None = None,
    sessions_dir: str | None = None,
    now_ts: float | None = None,
) -> dict[str, Any]:
    """Fast compact summary for `/api/overview` KPI card & sidebar badge."""
    _maybe_auto_sync(db_path=db_path, cc_log_path=cc_log_path, sessions_dir=sessions_dir)
    now_epoch = float(now_ts) if now_ts is not None else time.time()
    now_local = datetime.datetime.fromtimestamp(now_epoch).astimezone()
    today_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    since_24h = now_epoch - 24 * 3600

    conn = _open_conn(db_path)
    try:
        agg = conn.execute(
            """
            SELECT
                COUNT(*) AS all_calls,
                COALESCE(SUM(total_tokens), 0) AS all_tokens,
                COUNT(DISTINCT CASE WHEN chat_type = 'group' THEN session_key END) AS all_groups,
                COALESCE(SUM(CASE WHEN ts >= ? THEN total_tokens ELSE 0 END), 0) AS today_tokens,
                COALESCE(SUM(CASE WHEN ts >= ? THEN 1 ELSE 0 END), 0) AS today_calls,
                COUNT(DISTINCT CASE WHEN ts >= ? AND chat_type = 'group' THEN session_key END) AS today_groups,
                COALESCE(SUM(CASE WHEN ts >= ? THEN total_tokens ELSE 0 END), 0) AS h24_tokens,
                COALESCE(SUM(CASE WHEN ts >= ? THEN 1 ELSE 0 END), 0) AS h24_calls,
                COALESCE(SUM(CASE WHEN ts >= ? AND source NOT IN ('memory_extract','memory_sleep') AND is_silent = 1 THEN total_tokens ELSE 0 END), 0) AS h24_silent_tokens,
                COALESCE(SUM(CASE WHEN precision = 'exact' THEN 1 ELSE 0 END), 0) AS exact_calls,
                COALESCE(SUM(CASE WHEN precision != 'exact' THEN 1 ELSE 0 END), 0) AS log_calls,
                COALESCE(SUM(CASE WHEN precision = 'exact' THEN input_tokens ELSE 0 END), 0) AS exact_input,
                COALESCE(SUM(CASE WHEN precision = 'exact' THEN cached_read_tokens ELSE 0 END), 0) AS exact_cached,
                COALESCE(SUM(CASE WHEN precision = 'exact' THEN output_tokens ELSE 0 END), 0) AS exact_output
            FROM token_events
            """,
            (today_start, today_start, today_start, since_24h, since_24h, since_24h),
        ).fetchone()

        h24_tokens = int(agg["h24_tokens"] or 0)
        h24_calls = int(agg["h24_calls"] or 0)
        all_tokens = int(agg["all_tokens"] or 0)
        all_calls = int(agg["all_calls"] or 0)

        # Pick 24h for top groups preview if non-empty, else fallback to all-time
        use_window = since_24h if h24_tokens > 0 else 0.0
        denom_tokens = h24_tokens if h24_tokens > 0 else all_tokens
        top_rows = conn.execute(
            """
            SELECT
                session_key,
                chat_type,
                group_id,
                COUNT(*) AS calls,
                COALESCE(SUM(total_tokens), 0) AS total_tokens,
                COALESCE(SUM(CASE WHEN source NOT IN ('memory_extract','memory_sleep') AND is_silent = 1 THEN total_tokens ELSE 0 END), 0) AS silent_tokens
            FROM token_events
            WHERE ts >= ? AND chat_type = 'group'
            GROUP BY session_key, chat_type, group_id
            ORDER BY total_tokens DESC
            LIMIT 4
            """,
            (use_window,),
        ).fetchall()

        top_groups = []
        for r in top_rows:
            tot = int(r["total_tokens"] or 0)
            sil = int(r["silent_tokens"] or 0)
            name, tid = _resolve_chat_display(
                str(r["session_key"]),
                str(r["chat_type"]),
                str(r["group_id"] or ""),
                group_map=group_map,
                user_meta=user_meta,
                mem_names=mem_names,
            )
            top_groups.append(
                {
                    "session_key": str(r["session_key"]),
                    "group_id": tid,
                    "name": name,
                    "calls": int(r["calls"] or 0),
                    "total_tokens": tot,
                    "share_pct": round((tot / denom_tokens) * 100.0, 1) if denom_tokens > 0 else 0.0,
                    "silent_pct": round((sil / tot) * 100.0, 1) if tot > 0 else 0.0,
                }
            )

        # Also return quick per-session 24h token lookup map so Overview's Top Chats table can show Token column!
        chat_24h_rows = conn.execute(
            """
            SELECT session_key, COALESCE(SUM(total_tokens), 0) AS total_tokens
            FROM token_events
            WHERE ts >= ?
            GROUP BY session_key
            """,
            (use_window,),
        ).fetchall()
        tokens_by_session = {
            str(r["session_key"]): int(r["total_tokens"] or 0) for r in chat_24h_rows
        }

        h24_silent = int(agg["h24_silent_tokens"] or 0)
        return {
            "today_tokens": int(agg["today_tokens"] or 0),
            "today_calls": int(agg["today_calls"] or 0),
            "today_groups": int(agg["today_groups"] or 0),
            "last_24h_tokens": h24_tokens,
            "last_24h_calls": h24_calls,
            "last_24h_silent_tokens": h24_silent,
            "last_24h_silent_pct": round((h24_silent / h24_tokens) * 100.0, 1) if h24_tokens > 0 else 0.0,
            "all_time_tokens": all_tokens,
            "all_time_calls": all_calls,
            "all_time_groups": int(agg["all_groups"] or 0),
            "exact_calls": int(agg["exact_calls"] or 0),
            "log_backfill_calls": int(agg["log_calls"] or 0),
            "cache_hit_rate": _calc_cache_hit_rate(
                int(agg["exact_input"] or 0), int(agg["exact_cached"] or 0)
            ),
            "top_groups": top_groups,
            "top_groups_window": "24h" if h24_tokens > 0 else "all",
            "tokens_by_session": tokens_by_session,
        }
    finally:
        conn.close()


def get_usage_dashboard(
    *,
    range_key: str = "24h",
    chat_filter: str = "all",
    query: str = "",
    sort_by: str = "total_tokens",
    group_map: dict[str, str] | None = None,
    user_meta: dict[str, Any] | None = None,
    mem_names: dict[str, str] | None = None,
    db_path: str | None = None,
    cc_log_path: str | None = None,
    sessions_dir: str | None = None,
    now_ts: float | None = None,
) -> dict[str, Any]:
    """Full Token Usage dashboard data with per-group breakdown & trend."""
    _maybe_auto_sync(db_path=db_path, cc_log_path=cc_log_path, sessions_dir=sessions_dir)
    win = _resolve_time_window(range_key, now_ts=now_ts)
    cflt = str(chat_filter or "all").strip().lower()
    if cflt not in ("all", "group", "dm", "system"):
        cflt = "all"
    q_low = str(query or "").strip().lower()

    conn = _open_conn(db_path)
    try:
        where_parts = ["ts >= ?"]
        params: list[Any] = [win["since_ts"]]

        # Overall summary across the selected time window (before chat_filter so ratios stay accurate)
        ov = conn.execute(
            """
            SELECT
                COUNT(*) AS total_calls,
                COALESCE(SUM(total_tokens), 0) AS total_tokens,
                COALESCE(SUM(CASE WHEN chat_type = 'group' THEN total_tokens ELSE 0 END), 0) AS group_tokens,
                COALESCE(SUM(CASE WHEN chat_type = 'group' THEN 1 ELSE 0 END), 0) AS group_calls,
                COALESCE(SUM(CASE WHEN chat_type = 'dm' THEN total_tokens ELSE 0 END), 0) AS dm_tokens,
                COALESCE(SUM(CASE WHEN chat_type = 'dm' THEN 1 ELSE 0 END), 0) AS dm_calls,
                COALESCE(SUM(CASE WHEN chat_type = 'system' THEN total_tokens ELSE 0 END), 0) AS system_tokens,
                COALESCE(SUM(CASE WHEN chat_type = 'system' THEN 1 ELSE 0 END), 0) AS system_calls,
                COALESCE(SUM(CASE WHEN source NOT IN ('memory_extract','memory_sleep') AND is_silent = 0 THEN total_tokens ELSE 0 END), 0) AS reply_tokens,
                COALESCE(SUM(CASE WHEN source NOT IN ('memory_extract','memory_sleep') AND is_silent = 0 THEN 1 ELSE 0 END), 0) AS reply_calls,
                COALESCE(SUM(CASE WHEN source NOT IN ('memory_extract','memory_sleep') AND is_silent = 1 THEN total_tokens ELSE 0 END), 0) AS silent_tokens,
                COALESCE(SUM(CASE WHEN source NOT IN ('memory_extract','memory_sleep') AND is_silent = 1 THEN 1 ELSE 0 END), 0) AS silent_calls,
                COALESCE(SUM(CASE WHEN source IN ('memory_extract','memory_sleep') THEN total_tokens ELSE 0 END), 0) AS memory_tokens,
                COALESCE(SUM(CASE WHEN source IN ('memory_extract','memory_sleep') THEN 1 ELSE 0 END), 0) AS memory_calls,
                COALESCE(SUM(CASE WHEN source = 'history_followup' THEN total_tokens ELSE 0 END), 0) AS history_tokens,
                COALESCE(SUM(CASE WHEN source = 'history_followup' THEN 1 ELSE 0 END), 0) AS history_calls,
                COALESCE(MAX(context_used), 0) AS max_context_used,
                COALESCE(SUM(CASE WHEN precision = 'exact' THEN 1 ELSE 0 END), 0) AS exact_calls,
                COALESCE(SUM(CASE WHEN precision = 'exact' THEN total_tokens ELSE 0 END), 0) AS exact_total_tokens,
                COALESCE(SUM(CASE WHEN precision != 'exact' THEN 1 ELSE 0 END), 0) AS log_backfill_calls,
                COALESCE(SUM(CASE WHEN precision != 'exact' THEN total_tokens ELSE 0 END), 0) AS log_backfill_tokens,
                COALESCE(SUM(CASE WHEN precision = 'exact' THEN input_tokens ELSE 0 END), 0) AS input_tokens,
                COALESCE(SUM(CASE WHEN precision = 'exact' THEN cached_read_tokens ELSE 0 END), 0) AS cached_read_tokens,
                COALESCE(SUM(CASE WHEN precision = 'exact' THEN cached_write_tokens ELSE 0 END), 0) AS cached_write_tokens,
                COALESCE(SUM(CASE WHEN precision = 'exact' THEN output_tokens ELSE 0 END), 0) AS output_tokens,
                COALESCE(SUM(CASE WHEN precision = 'exact' THEN thought_tokens ELSE 0 END), 0) AS thought_tokens,
                COUNT(DISTINCT CASE WHEN chat_type = 'group' THEN session_key END) AS active_groups_count,
                COUNT(DISTINCT CASE WHEN chat_type = 'dm' THEN session_key END) AS active_dms_count
            FROM token_events
            WHERE ts >= ?
            """,
            (win["since_ts"],),
        ).fetchone()

        total_tokens = int(ov["total_tokens"] or 0)
        total_calls = int(ov["total_calls"] or 0)
        group_tokens = int(ov["group_tokens"] or 0)
        silent_tokens = int(ov["silent_tokens"] or 0)
        input_tokens = int(ov["input_tokens"] or 0)
        cached_read_tokens = int(ov["cached_read_tokens"] or 0)

        # Per-chat aggregation
        chat_rows = conn.execute(
            """
            SELECT
                session_key,
                chat_type,
                group_id,
                COUNT(*) AS calls,
                COALESCE(SUM(total_tokens), 0) AS total_tokens,
                COALESCE(SUM(CASE WHEN source NOT IN ('memory_extract','memory_sleep') AND is_silent = 0 THEN total_tokens ELSE 0 END), 0) AS reply_tokens,
                COALESCE(SUM(CASE WHEN source NOT IN ('memory_extract','memory_sleep') AND is_silent = 0 THEN 1 ELSE 0 END), 0) AS reply_calls,
                COALESCE(SUM(CASE WHEN source NOT IN ('memory_extract','memory_sleep') AND is_silent = 1 THEN total_tokens ELSE 0 END), 0) AS silent_tokens,
                COALESCE(SUM(CASE WHEN source NOT IN ('memory_extract','memory_sleep') AND is_silent = 1 THEN 1 ELSE 0 END), 0) AS silent_calls,
                COALESCE(SUM(CASE WHEN source IN ('memory_extract','memory_sleep') THEN total_tokens ELSE 0 END), 0) AS memory_tokens,
                COALESCE(SUM(CASE WHEN source IN ('memory_extract','memory_sleep') THEN 1 ELSE 0 END), 0) AS memory_calls,
                COALESCE(SUM(CASE WHEN source = 'history_followup' THEN total_tokens ELSE 0 END), 0) AS history_tokens,
                COALESCE(SUM(CASE WHEN source = 'history_followup' THEN 1 ELSE 0 END), 0) AS history_calls,
                COALESCE(SUM(CASE WHEN precision = 'exact' THEN 1 ELSE 0 END), 0) AS exact_calls,
                COALESCE(SUM(CASE WHEN precision != 'exact' THEN 1 ELSE 0 END), 0) AS log_backfill_calls,
                COALESCE(SUM(CASE WHEN precision = 'exact' THEN input_tokens ELSE 0 END), 0) AS input_tokens,
                COALESCE(SUM(CASE WHEN precision = 'exact' THEN cached_read_tokens ELSE 0 END), 0) AS cached_read_tokens,
                COALESCE(SUM(CASE WHEN precision = 'exact' THEN cached_write_tokens ELSE 0 END), 0) AS cached_write_tokens,
                COALESCE(SUM(CASE WHEN precision = 'exact' THEN output_tokens ELSE 0 END), 0) AS output_tokens,
                COALESCE(SUM(CASE WHEN precision = 'exact' THEN thought_tokens ELSE 0 END), 0) AS thought_tokens,
                COALESCE(MAX(context_used), 0) AS max_context_used,
                COALESCE(MAX(ts), 0) AS last_active_ts
            FROM token_events
            WHERE ts >= ?
            GROUP BY session_key, chat_type, group_id
            """,
            (win["since_ts"],),
        ).fetchall()

        # Latest non-zero context_used per session_key (fast grouped lookup)
        last_ctx_rows = conn.execute(
            """
            SELECT e.session_key, e.context_used
            FROM token_events e
            INNER JOIN (
                SELECT session_key, MAX(id) AS max_id
                FROM token_events
                WHERE ts >= ? AND context_used > 0
                GROUP BY session_key
            ) m ON e.id = m.max_id
            """,
            (win["since_ts"],),
        ).fetchall()
        last_ctx_map = {str(r["session_key"]): int(r["context_used"] or 0) for r in last_ctx_rows}

        all_chats: list[dict[str, Any]] = []
        for r in chat_rows:
            skey = str(r["session_key"])
            ctype = str(r["chat_type"])
            gid = str(r["group_id"] or "")
            name, tid = _resolve_chat_display(
                skey, ctype, gid, group_map=group_map, user_meta=user_meta, mem_names=mem_names
            )
            tot = int(r["total_tokens"] or 0)
            calls = int(r["calls"] or 0)
            sil = int(r["silent_tokens"] or 0)
            inp = int(r["input_tokens"] or 0)
            cr = int(r["cached_read_tokens"] or 0)
            last_ts = float(r["last_active_ts"] or 0.0)
            all_chats.append(
                {
                    "session_key": skey,
                    "chat_type": ctype,
                    "is_group": ctype == "group",
                    "group_id": gid if ctype == "group" else "",
                    "target_id": tid,
                    "name": name,
                    "total_tokens": tot,
                    "share_pct": round((tot / total_tokens) * 100.0, 1) if total_tokens > 0 else 0.0,
                    "calls": calls,
                    "avg_tokens": int(round(tot / calls)) if calls > 0 else 0,
                    "reply_tokens": int(r["reply_tokens"] or 0),
                    "reply_calls": int(r["reply_calls"] or 0),
                    "silent_tokens": sil,
                    "silent_calls": int(r["silent_calls"] or 0),
                    "silent_share_pct": round((sil / tot) * 100.0, 1) if tot > 0 else 0.0,
                    "memory_tokens": int(r["memory_tokens"] or 0),
                    "memory_calls": int(r["memory_calls"] or 0),
                    "history_tokens": int(r["history_tokens"] or 0),
                    "history_calls": int(r["history_calls"] or 0),
                    "exact_calls": int(r["exact_calls"] or 0),
                    "log_backfill_calls": int(r["log_backfill_calls"] or 0),
                    "input_tokens": inp,
                    "cached_read_tokens": cr,
                    "cached_write_tokens": int(r["cached_write_tokens"] or 0),
                    "output_tokens": int(r["output_tokens"] or 0),
                    "thought_tokens": int(r["thought_tokens"] or 0),
                    "cache_hit_rate": _calc_cache_hit_rate(inp, cr),
                    "max_context_used": int(r["max_context_used"] or 0),
                    "last_context_used": last_ctx_map.get(skey, 0),
                    "last_active_ts": last_ts,
                    "last_active_iso": _ts_to_iso(last_ts),
                }
            )

        # Top group (before UI filter)
        group_only_sorted = sorted(
            [c for c in all_chats if c["is_group"]],
            key=lambda c: c["total_tokens"],
            reverse=True,
        )
        top_group = None
        if group_only_sorted:
            tg = group_only_sorted[0]
            top_group = {
                "session_key": tg["session_key"],
                "group_id": tg["group_id"],
                "name": tg["name"],
                "total_tokens": tg["total_tokens"],
                "share_pct": tg["share_pct"],
                "calls": tg["calls"],
            }

        # Filter & sort chats list
        chats = all_chats
        if cflt != "all":
            chats = [c for c in chats if c["chat_type"] == cflt]
        if q_low:
            chats = [
                c
                for c in chats
                if q_low in c["name"].lower()
                or q_low in c["session_key"].lower()
                or q_low in c["target_id"].lower()
            ]

        sort_key = str(sort_by or "total_tokens").strip().lower()
        if sort_key == "calls":
            chats.sort(key=lambda c: (c["calls"], c["total_tokens"]), reverse=True)
        elif sort_key == "avg_tokens":
            chats.sort(key=lambda c: (c["avg_tokens"], c["total_tokens"]), reverse=True)
        elif sort_key == "silent_tokens":
            chats.sort(key=lambda c: (c["silent_tokens"], c["total_tokens"]), reverse=True)
        elif sort_key == "last_active":
            chats.sort(key=lambda c: (c["last_active_ts"], c["total_tokens"]), reverse=True)
        else:
            chats.sort(key=lambda c: (c["total_tokens"], c["calls"]), reverse=True)

        # Trend buckets (optionally filtered by chat_filter)
        if cflt != "all":
            where_parts.append("chat_type = ?")
            params.append(cflt)
        trend = _build_trend_buckets(conn, win, " AND ".join(where_parts), params)

        # Breakdown by source
        src_rows = conn.execute(
            """
            SELECT
                source,
                is_silent,
                COUNT(*) AS calls,
                COALESCE(SUM(total_tokens), 0) AS total_tokens
            FROM token_events
            WHERE ts >= ?
            GROUP BY source, is_silent
            ORDER BY total_tokens DESC
            """,
            (win["since_ts"],),
        ).fetchall()
        by_source = [
            {
                "source": str(r["source"]),
                "is_silent": bool(r["is_silent"]),
                "calls": int(r["calls"] or 0),
                "total_tokens": int(r["total_tokens"] or 0),
                "share_pct": round((int(r["total_tokens"] or 0) / total_tokens) * 100.0, 1)
                if total_tokens > 0
                else 0.0,
            }
            for r in src_rows
        ]

        # Breakdown by model / thought level
        model_rows = conn.execute(
            """
            SELECT
                COALESCE(NULLIF(model, ''), '未标注 (日志回溯)') AS model_name,
                COALESCE(NULLIF(thought_level, ''), '-') AS thought_level,
                COUNT(*) AS calls,
                COALESCE(SUM(total_tokens), 0) AS total_tokens
            FROM token_events
            WHERE ts >= ?
            GROUP BY model_name, thought_level
            ORDER BY total_tokens DESC
            LIMIT 20
            """,
            (win["since_ts"],),
        ).fetchall()
        by_model = [
            {
                "model": str(r["model_name"]),
                "thought_level": str(r["thought_level"]),
                "calls": int(r["calls"] or 0),
                "total_tokens": int(r["total_tokens"] or 0),
                "share_pct": round((int(r["total_tokens"] or 0) / total_tokens) * 100.0, 1)
                if total_tokens > 0
                else 0.0,
            }
            for r in model_rows
        ]

        return {
            "range": win["range"],
            "range_label": win["label"],
            "granularity": win["granularity"],
            "since_ts": win["since_ts"],
            "until_ts": win["until_ts"],
            "chat_filter": cflt,
            "summary": {
                "total_tokens": total_tokens,
                "total_calls": total_calls,
                "avg_tokens_per_call": int(round(total_tokens / total_calls)) if total_calls > 0 else 0,
                "group_tokens": group_tokens,
                "group_calls": int(ov["group_calls"] or 0),
                "group_share_pct": round((group_tokens / total_tokens) * 100.0, 1) if total_tokens > 0 else 0.0,
                "dm_tokens": int(ov["dm_tokens"] or 0),
                "dm_calls": int(ov["dm_calls"] or 0),
                "system_tokens": int(ov["system_tokens"] or 0),
                "system_calls": int(ov["system_calls"] or 0),
                "reply_tokens": int(ov["reply_tokens"] or 0),
                "reply_calls": int(ov["reply_calls"] or 0),
                "silent_tokens": silent_tokens,
                "silent_calls": int(ov["silent_calls"] or 0),
                "silent_share_pct": round((silent_tokens / total_tokens) * 100.0, 1) if total_tokens > 0 else 0.0,
                "memory_tokens": int(ov["memory_tokens"] or 0),
                "memory_calls": int(ov["memory_calls"] or 0),
                "history_tokens": int(ov["history_tokens"] or 0),
                "history_calls": int(ov["history_calls"] or 0),
                "max_context_used": int(ov["max_context_used"] or 0),
                "exact_calls": int(ov["exact_calls"] or 0),
                "exact_total_tokens": int(ov["exact_total_tokens"] or 0),
                "log_backfill_calls": int(ov["log_backfill_calls"] or 0),
                "log_backfill_tokens": int(ov["log_backfill_tokens"] or 0),
                "input_tokens": input_tokens,
                "cached_read_tokens": cached_read_tokens,
                "cached_write_tokens": int(ov["cached_write_tokens"] or 0),
                "output_tokens": int(ov["output_tokens"] or 0),
                "thought_tokens": int(ov["thought_tokens"] or 0),
                "cache_hit_rate": _calc_cache_hit_rate(input_tokens, cached_read_tokens),
                "active_groups_count": int(ov["active_groups_count"] or 0),
                "active_dms_count": int(ov["active_dms_count"] or 0),
                "top_group": top_group,
            },
            "trend": trend,
            "chats": chats,
            "group_shares": [
                {
                    "session_key": c["session_key"],
                    "group_id": c["group_id"],
                    "name": c["name"],
                    "total_tokens": c["total_tokens"],
                    "share_pct": c["share_pct"],
                    "calls": c["calls"],
                    "silent_share_pct": c["silent_share_pct"],
                }
                for c in group_only_sorted[:10]
            ],
            "by_source": by_source,
            "by_model": by_model,
        }
    finally:
        conn.close()


def get_chat_detail(
    session_key: str,
    *,
    range_key: str = "24h",
    limit: int = 50,
    group_map: dict[str, str] | None = None,
    user_meta: dict[str, Any] | None = None,
    mem_names: dict[str, str] | None = None,
    db_path: str | None = None,
    cc_log_path: str | None = None,
    sessions_dir: str | None = None,
    now_ts: float | None = None,
) -> dict[str, Any]:
    """Deep single-group / single-DM token consumption drill-down."""
    _maybe_auto_sync(db_path=db_path, cc_log_path=cc_log_path, sessions_dir=sessions_dir)
    canon_key, chat_type, canon_gid = normalize_chat_identity(session_key=session_key)
    win = _resolve_time_window(range_key, now_ts=now_ts)
    lim = min(max(int(limit or 50), 1), 200)

    conn = _open_conn(db_path)
    try:
        agg = conn.execute(
            """
            SELECT
                COUNT(*) AS calls,
                COALESCE(SUM(total_tokens), 0) AS total_tokens,
                COALESCE(SUM(CASE WHEN source NOT IN ('memory_extract','memory_sleep') AND is_silent = 0 THEN total_tokens ELSE 0 END), 0) AS reply_tokens,
                COALESCE(SUM(CASE WHEN source NOT IN ('memory_extract','memory_sleep') AND is_silent = 0 THEN 1 ELSE 0 END), 0) AS reply_calls,
                COALESCE(SUM(CASE WHEN source NOT IN ('memory_extract','memory_sleep') AND is_silent = 1 THEN total_tokens ELSE 0 END), 0) AS silent_tokens,
                COALESCE(SUM(CASE WHEN source NOT IN ('memory_extract','memory_sleep') AND is_silent = 1 THEN 1 ELSE 0 END), 0) AS silent_calls,
                COALESCE(SUM(CASE WHEN source IN ('memory_extract','memory_sleep') THEN total_tokens ELSE 0 END), 0) AS memory_tokens,
                COALESCE(SUM(CASE WHEN source IN ('memory_extract','memory_sleep') THEN 1 ELSE 0 END), 0) AS memory_calls,
                COALESCE(SUM(CASE WHEN source = 'history_followup' THEN total_tokens ELSE 0 END), 0) AS history_tokens,
                COALESCE(SUM(CASE WHEN source = 'history_followup' THEN 1 ELSE 0 END), 0) AS history_calls,
                COALESCE(SUM(CASE WHEN precision = 'exact' THEN 1 ELSE 0 END), 0) AS exact_calls,
                COALESCE(SUM(CASE WHEN precision != 'exact' THEN 1 ELSE 0 END), 0) AS log_backfill_calls,
                COALESCE(SUM(CASE WHEN precision = 'exact' THEN input_tokens ELSE 0 END), 0) AS input_tokens,
                COALESCE(SUM(CASE WHEN precision = 'exact' THEN cached_read_tokens ELSE 0 END), 0) AS cached_read_tokens,
                COALESCE(SUM(CASE WHEN precision = 'exact' THEN cached_write_tokens ELSE 0 END), 0) AS cached_write_tokens,
                COALESCE(SUM(CASE WHEN precision = 'exact' THEN output_tokens ELSE 0 END), 0) AS output_tokens,
                COALESCE(SUM(CASE WHEN precision = 'exact' THEN thought_tokens ELSE 0 END), 0) AS thought_tokens,
                COALESCE(MAX(context_used), 0) AS max_context_used,
                COALESCE(MAX(ts), 0) AS last_active_ts
            FROM token_events
            WHERE session_key = ? AND ts >= ?
            """,
            (canon_key, win["since_ts"]),
        ).fetchone()

        global_tot_row = conn.execute(
            "SELECT COALESCE(SUM(total_tokens), 0) AS tot FROM token_events WHERE ts >= ?",
            (win["since_ts"],),
        ).fetchone()
        global_tot = int(global_tot_row["tot"] or 0)

        last_ctx_row = conn.execute(
            """
            SELECT context_used
            FROM token_events
            WHERE session_key = ? AND ts >= ? AND context_used > 0
            ORDER BY id DESC LIMIT 1
            """,
            (canon_key, win["since_ts"]),
        ).fetchone()
        last_ctx = int(last_ctx_row["context_used"] or 0) if last_ctx_row else 0

        tot = int(agg["total_tokens"] or 0)
        calls = int(agg["calls"] or 0)
        sil = int(agg["silent_tokens"] or 0)
        inp = int(agg["input_tokens"] or 0)
        cr = int(agg["cached_read_tokens"] or 0)
        name, tid = _resolve_chat_display(
            canon_key,
            chat_type,
            canon_gid,
            group_map=group_map,
            user_meta=user_meta,
            mem_names=mem_names,
        )

        chat_summary = {
            "session_key": canon_key,
            "chat_type": chat_type,
            "is_group": chat_type == "group",
            "group_id": canon_gid if chat_type == "group" else "",
            "target_id": tid,
            "name": name,
            "total_tokens": tot,
            "share_pct": round((tot / global_tot) * 100.0, 1) if global_tot > 0 else 0.0,
            "calls": calls,
            "avg_tokens": int(round(tot / calls)) if calls > 0 else 0,
            "reply_tokens": int(agg["reply_tokens"] or 0),
            "reply_calls": int(agg["reply_calls"] or 0),
            "silent_tokens": sil,
            "silent_calls": int(agg["silent_calls"] or 0),
            "silent_share_pct": round((sil / tot) * 100.0, 1) if tot > 0 else 0.0,
            "memory_tokens": int(agg["memory_tokens"] or 0),
            "memory_calls": int(agg["memory_calls"] or 0),
            "history_tokens": int(agg["history_tokens"] or 0),
            "history_calls": int(agg["history_calls"] or 0),
            "exact_calls": int(agg["exact_calls"] or 0),
            "log_backfill_calls": int(agg["log_backfill_calls"] or 0),
            "input_tokens": inp,
            "cached_read_tokens": cr,
            "cached_write_tokens": int(agg["cached_write_tokens"] or 0),
            "output_tokens": int(agg["output_tokens"] or 0),
            "thought_tokens": int(agg["thought_tokens"] or 0),
            "cache_hit_rate": _calc_cache_hit_rate(inp, cr),
            "max_context_used": int(agg["max_context_used"] or 0),
            "last_context_used": last_ctx,
            "last_active_ts": float(agg["last_active_ts"] or 0.0),
            "last_active_iso": _ts_to_iso(float(agg["last_active_ts"] or 0.0)),
        }

        trend = _build_trend_buckets(
            conn, win, "session_key = ? AND ts >= ?", [canon_key, win["since_ts"]]
        )

        # Top triggering members in this chat
        member_rows = conn.execute(
            """
            SELECT
                CASE
                    WHEN sender_id != '' THEN sender_id
                    WHEN sender_name != '' THEN sender_name
                    ELSE '（后台/未知）'
                END AS member_key,
                MAX(sender_id) AS sender_id,
                MAX(sender_name) AS sender_name,
                COUNT(*) AS calls,
                COALESCE(SUM(CASE WHEN is_silent = 0 AND source NOT IN ('memory_extract','memory_sleep') THEN 1 ELSE 0 END), 0) AS reply_calls,
                COALESCE(SUM(CASE WHEN is_silent = 1 AND source NOT IN ('memory_extract','memory_sleep') THEN 1 ELSE 0 END), 0) AS silent_calls,
                COALESCE(SUM(total_tokens), 0) AS total_tokens
            FROM token_events
            WHERE session_key = ? AND ts >= ?
            GROUP BY member_key
            ORDER BY total_tokens DESC
            LIMIT 15
            """,
            (canon_key, win["since_ts"]),
        ).fetchall()
        mnames = mem_names or {}
        top_members = []
        for mr in member_rows:
            sid = str(mr["sender_id"] or "")
            sname = str(mr["sender_name"] or "") or mnames.get(sid) or (f"QQ {sid}" if sid else str(mr["member_key"]))
            mtot = int(mr["total_tokens"] or 0)
            top_members.append(
                {
                    "sender_id": sid,
                    "sender_name": sname,
                    "calls": int(mr["calls"] or 0),
                    "reply_calls": int(mr["reply_calls"] or 0),
                    "silent_calls": int(mr["silent_calls"] or 0),
                    "total_tokens": mtot,
                    "share_pct": round((mtot / tot) * 100.0, 1) if tot > 0 else 0.0,
                }
            )

        # Thought-level / source breakdown for this chat
        lvl_rows = conn.execute(
            """
            SELECT
                source,
                COALESCE(NULLIF(thought_level, ''), '-') AS thought_level,
                is_silent,
                COUNT(*) AS calls,
                COALESCE(SUM(total_tokens), 0) AS total_tokens
            FROM token_events
            WHERE session_key = ? AND ts >= ?
            GROUP BY source, thought_level, is_silent
            ORDER BY total_tokens DESC
            """,
            (canon_key, win["since_ts"]),
        ).fetchall()
        by_breakdown = [
            {
                "source": str(r["source"]),
                "thought_level": str(r["thought_level"]),
                "is_silent": bool(r["is_silent"]),
                "calls": int(r["calls"] or 0),
                "total_tokens": int(r["total_tokens"] or 0),
                "share_pct": round((int(r["total_tokens"] or 0) / tot) * 100.0, 1) if tot > 0 else 0.0,
            }
            for r in lvl_rows
        ]

        # Recent individual turns/calls
        recent_rows = conn.execute(
            """
            SELECT
                id, ts, sender_id, sender_name, source, precision, model, thought_level,
                input_tokens, cached_read_tokens, cached_write_tokens, output_tokens,
                thought_tokens, total_tokens, context_used, is_silent, is_ambient,
                duration_ms, response_len
            FROM token_events
            WHERE session_key = ? AND ts >= ?
            ORDER BY ts DESC, id DESC
            LIMIT ?
            """,
            (canon_key, win["since_ts"], lim),
        ).fetchall()
        recent_events = []
        for rr in recent_rows:
            ts_v = float(rr["ts"] or 0.0)
            sid = str(rr["sender_id"] or "")
            sname = str(rr["sender_name"] or "") or mnames.get(sid) or (f"QQ {sid}" if sid else "")
            recent_events.append(
                {
                    "id": int(rr["id"]),
                    "ts": ts_v,
                    "time_iso": _ts_to_iso(ts_v),
                    "sender_id": sid,
                    "sender_name": sname,
                    "source": str(rr["source"]),
                    "precision": str(rr["precision"]),
                    "model": str(rr["model"] or ""),
                    "thought_level": str(rr["thought_level"] or ""),
                    "input_tokens": int(rr["input_tokens"] or 0),
                    "cached_read_tokens": int(rr["cached_read_tokens"] or 0),
                    "cached_write_tokens": int(rr["cached_write_tokens"] or 0),
                    "output_tokens": int(rr["output_tokens"] or 0),
                    "thought_tokens": int(rr["thought_tokens"] or 0),
                    "total_tokens": int(rr["total_tokens"] or 0),
                    "context_used": int(rr["context_used"] or 0),
                    "is_silent": bool(rr["is_silent"]),
                    "is_ambient": bool(rr["is_ambient"]),
                    "duration_sec": round(int(rr["duration_ms"] or 0) / 1000.0, 2),
                    "response_len": int(rr["response_len"] or 0),
                }
            )

        return {
            "range": win["range"],
            "range_label": win["label"],
            "granularity": win["granularity"],
            "chat": chat_summary,
            "trend": trend,
            "top_members": top_members,
            "by_breakdown": by_breakdown,
            "recent_events": recent_events,
        }
    finally:
        conn.close()
