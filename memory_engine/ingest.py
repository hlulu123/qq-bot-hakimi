#!/usr/bin/env python3
"""Real-time raw message ingestion, dedup, profile touch, and window trigger evaluation.

Designed for sub-millisecond (<1ms) execution on every inbound/outbound chat message
using ONLY the Python 3 standard library and the SQLite WAL layer in ``db.py``.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3

from . import db, quality_gate, tokenizer

# ---------------------------------------------------------------------------
# Constants & High-Signal Personal/Life Trigger Cues
# ---------------------------------------------------------------------------

MAX_STORED_MESSAGE_CHARS = 1000
MAX_SENDER_NAME_CHARS = 80
HIGH_SIGNAL_COOLDOWN_SECS = 45.0
HIGH_SIGNAL_DELAY_SECS = 3.0
WINDOW_FULL_COOLDOWN_SECS = 45.0
WINDOW_FULL_DELAY_SECS = 1.0

MEMORY_TRIGGER_CUES: tuple[str, ...] = (
    "记住",
    "别忘了",
    "记一下",
    "我叫",
    "叫我",
    "我喜欢",
    "我爱吃",
    "我不吃",
    "我不喜欢",
    "我讨厌",
    "我养了",
    "我家猫",
    "我家狗",
    "我住在",
    "我要去",
    "我生日",
    "我专业",
    "我工作",
    "我上班",
    "我辞职",
    "我入职",
    "我毕业",
    "我考研",
    "我玩",
    "我推",
    "本命",
    "最喜欢",
    "过敏",
    "老家",
    "昵称",
    "外号",
    "flag",
    "立个flag",
    "女朋友",
    "男朋友",
    "对象",
    "结婚",
    "搬家",
    "买了个",
)


def _compile_trigger_cue_regex(cues: tuple[str, ...]) -> re.Pattern[str]:
    parts: list[str] = []
    for cue in cues:
        if cue.isascii() and cue.isalpha():
            parts.append(rf"(?<![a-z]){re.escape(cue)}(?![a-z])")
        else:
            parts.append(re.escape(cue))
    return re.compile("|".join(parts), re.IGNORECASE)


_TRIGGER_CUE_RE = _compile_trigger_cue_regex(MEMORY_TRIGGER_CUES)
_EXPLICIT_CHAT_KEY_PREFIXES = ("g:", "group:", "dm:", "private:")


# ---------------------------------------------------------------------------
# Pure Helpers
# ---------------------------------------------------------------------------

def has_trigger_cue(text: str) -> bool:
    """Return True if text contains any high-signal personal/life memory trigger cue."""
    if not text:
        return False
    return bool(_TRIGGER_CUE_RE.search(str(text)))


_FALLBACK_CONTROL_REPLY_RE = re.compile(
    r'^[\s`"\'\[\(\*（【]*(?:NO_REPLY|FOLLOW_REPEAT)[\s`"\'\]\)\*）】\.。!！~～]*$',
    re.IGNORECASE,
)
_FALLBACK_UPSTREAM_ERROR_RE = re.compile(
    r"(?:这个我接不住|RESOURCE_EXHAUSTED|"
    r"HTTP\s*(?:429|500|502|503|504)\b|"
    r"500\s+Internal\s+Server\s+Error|502\s+Bad\s+Gateway|"
    r"503\s+Service\s+Unavailable|504\s+Gateway\s+Timeout|"
    r"Traceback\s*\(most\s+recent\s+call\s+last\))",
    re.IGNORECASE,
)
_FALLBACK_QQ_DIRECTIVE_RE = re.compile(r"\[\[QQ:[^\]]+\]\]")


def clean_message_for_storage(text: str, role: str = "user") -> str:
    """Strip synthetic prompt wrappers once, neutralize forged tags/fences, reject unworthy text, and cap at 1000 chars."""
    if not text:
        return ""
    norm_role = str(role or "user").strip().lower() or "user"
    cleaned = quality_gate.strip_synthetic_blocks(str(text)).strip()
    if not cleaned:
        return ""
    ctrl_re = getattr(quality_gate, "_CONTROL_REPLY_RE", _FALLBACK_CONTROL_REPLY_RE)
    err_re = getattr(quality_gate, "_UPSTREAM_ERROR_RE", _FALLBACK_UPSTREAM_ERROR_RE)
    if ctrl_re.match(cleaned) or err_re.search(cleaned):
        return ""
    if norm_role == "assistant":
        dir_re = getattr(quality_gate, "_QQ_DIRECTIVE_RE", _FALLBACK_QQ_DIRECTIVE_RE)
        if not dir_re.sub("", cleaned).strip():
            return ""
    cleaned = quality_gate.neutralize_prompt_text(cleaned).strip()
    if not cleaned:
        return ""
    if len(cleaned) > MAX_STORED_MESSAGE_CHARS:
        cleaned = cleaned[:MAX_STORED_MESSAGE_CHARS].rstrip()
    return cleaned


def compute_dedup_hash(role: str, sender_uid: str, clean_text: str) -> str:
    """Compute a compact 24-hex SHA-1 deduplication fingerprint over the full clean_text."""
    payload = f"{role}|{sender_uid}|{str(clean_text or '')}"
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:24]


def _resolve_chat_context(
    chat_key: str,
    is_group: bool,
    group_id: str,
    sender_uid: str,
    role: str,
) -> tuple[str, str, bool, str, str]:
    """Normalize (chat_key, scope, eff_is_group, gid, uid) with strict scope consistency."""
    raw_ck = str(chat_key or "").strip()
    gid = db.sanitize_uid(group_id)
    uid = db.sanitize_uid(sender_uid)
    eff_is_group = bool(is_group)

    if raw_ck and raw_ck.startswith(_EXPLICIT_CHAT_KEY_PREFIXES):
        ck_is_group, ck_gid, ck_uid = db.parse_chat_key(raw_ck)
        eff_is_group = ck_is_group
        if ck_is_group:
            if gid and ck_gid and gid != ck_gid:
                return "", "", True, "", uid
            gid = gid or ck_gid
            eff_chat_key = db.make_chat_key(True, gid, "")
            scope = db.chat_scope_for(True, gid, "")
        else:
            if gid:
                return "", "", False, "", uid
            peer_uid = ck_uid or uid
            if role == "user" and not uid:
                uid = peer_uid
            eff_chat_key = db.make_chat_key(False, "", peer_uid)
            scope = db.chat_scope_for(False, "", peer_uid)
        return eff_chat_key, scope, eff_is_group, gid, uid

    if eff_is_group:
        eff_chat_key = raw_ck or db.make_chat_key(True, gid, "")
        scope = db.chat_scope_for(True, gid, "")
    else:
        gid = ""
        eff_chat_key = raw_ck or db.make_chat_key(False, "", uid)
        scope = db.chat_scope_for(False, "", uid)
    return eff_chat_key, scope, eff_is_group, gid, uid


def _parse_float_meta(conn: sqlite3.Connection, key: str, default: float = 0.0) -> float:
    raw = db.get_meta(conn, key, "")
    if not raw:
        return default
    try:
        return float(raw)
    except (ValueError, TypeError):
        return default


def _evaluate_trigger(
    conn: sqlite3.Connection,
    chat_key: str,
    scope: str,
    is_group: bool,
    role: str,
    clean_text: str,
    ts: int,
    is_trivial: bool | None = None,
) -> tuple[bool, float, str]:
    """Evaluate whether a newly recorded message should trigger consolidation."""
    if role != "user":
        return False, 0.0, ""

    last_cons_ts = _parse_float_meta(conn, f"last_consolidated:{chat_key}", 0.0)
    elapsed = float(ts) - last_cons_ts
    msg_trivial = tokenizer.is_trivial_message(clean_text) if is_trivial is None else bool(is_trivial)

    # High-signal fast path
    if (
        not msg_trivial
        and has_trigger_cue(clean_text)
        and elapsed >= HIGH_SIGNAL_COOLDOWN_SECS
    ):
        return True, HIGH_SIGNAL_DELAY_SECS, "high_signal"

    # Full-window threshold path (strictly isolated by chat_key AND scope)
    if elapsed < WINDOW_FULL_COOLDOWN_SECS:
        return False, 0.0, ""

    threshold = (
        max(1, db.get_env_int("MEMORY_GROUP_WINDOW_SIZE", 12))
        if is_group
        else max(1, db.get_env_int("MEMORY_DM_WINDOW_SIZE", 6))
    )
    scan_limit = max(40, threshold * 4)

    rows = conn.execute(
        "SELECT text FROM raw_messages "
        "WHERE chat_key = ? AND scope = ? AND distilled = 0 AND role = 'user' "
        "ORDER BY id DESC LIMIT ?",
        (chat_key, scope, scan_limit),
    ).fetchall()

    effective_count = 0
    for row in rows:
        if not tokenizer.is_trivial_message(row[0]):
            effective_count += 1
            if effective_count >= threshold:
                return True, WINDOW_FULL_DELAY_SECS, "window_full"

    return False, 0.0, ""


# ---------------------------------------------------------------------------
# Public Ingestion & Idle-Window APIs
# ---------------------------------------------------------------------------

def record_message(
    chat_key: str = "",
    role: str = "user",
    text: str = "",
    sender_uid: str = "",
    sender_name: str = "",
    is_group: bool = True,
    group_id: str = "",
    ts: int | None = None,
    db_path: str | None = None,
    dedup_window_secs: int = 15,
    group_name: str = "",
) -> dict:
    """Persist one raw chat message with dedup, profile touch, and post-commit trigger evaluation."""
    norm_role = str(role or "user").strip().lower() or "user"
    eff_chat_key, scope, eff_is_group, gid, uid = _resolve_chat_context(
        chat_key=chat_key,
        is_group=is_group,
        group_id=group_id,
        sender_uid=sender_uid,
        role=norm_role,
    )

    clean_text = clean_message_for_storage(text, role=norm_role)
    if not clean_text:
        return {
            "recorded": False,
            "reason": "empty_or_filtered",
            "should_consolidate": False,
            "delay": 0.0,
            "trigger_reason": "",
            "chat_key": eff_chat_key,
            "scope": scope,
        }

    if not eff_chat_key or eff_chat_key in ("g:", "dm:") or scope in ("group:", "dm:"):
        return {
            "recorded": False,
            "reason": "invalid_chat_key",
            "should_consolidate": False,
            "delay": 0.0,
            "trigger_reason": "",
            "chat_key": eff_chat_key,
            "scope": scope,
        }

    now = db.now_ts() if ts is None else (db.parse_ts(ts, db.now_ts()) or db.now_ts())
    clean_sender_name = quality_gate.sanitize_prompt_singleline(
        str(sender_name or ""), max_len=MAX_SENDER_NAME_CHARS
    )
    clean_group_name = quality_gate.sanitize_prompt_singleline(
        str(group_name or ""), max_len=MAX_SENDER_NAME_CHARS
    )
    dedup_hash = compute_dedup_hash(norm_role, uid, clean_text)
    dedup_secs = max(0, int(dedup_window_secs or 0))
    msg_is_trivial = tokenizer.is_trivial_message(clean_text)

    with db.write_tx(db_path) as conn:
        if dedup_secs > 0:
            dup_row = conn.execute(
                "SELECT id FROM raw_messages "
                "WHERE chat_key = ? AND scope = ? AND sender_uid = ? "
                "AND dedup_hash = ? AND ts >= ? LIMIT 1",
                (eff_chat_key, scope, uid, dedup_hash, now - dedup_secs),
            ).fetchone()
            if dup_row is not None:
                return {
                    "recorded": False,
                    "reason": "duplicate",
                    "msg_id": int(dup_row[0]),
                    "chat_key": eff_chat_key,
                    "scope": scope,
                    "should_consolidate": False,
                    "delay": 0.0,
                    "trigger_reason": "",
                }

        cur = conn.execute(
            "INSERT INTO raw_messages("
            "chat_key, scope, group_id, sender_uid, sender_name, role, text, "
            "dedup_hash, ts, distilled, window_id"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, '')",
            (
                eff_chat_key,
                scope,
                gid if eff_is_group else "",
                uid,
                clean_sender_name,
                norm_role,
                clean_text,
                dedup_hash,
                now,
            ),
        )
        msg_id = int(cur.lastrowid or 0)

        if norm_role == "user" and uid:
            db.set_meta(conn, "last_inbound_ts", str(now), updated_at=now)
            if eff_is_group:
                db.upsert_profile_touch(
                    conn,
                    profile_key=f"user_public:{uid}",
                    scope="user_public",
                    entity_id=uid,
                    nickname=clean_sender_name,
                    group_id=gid,
                    ts=now,
                    inc_msg=1,
                )
                if gid and db.is_group_memory_enabled():
                    should_touch_group = True
                    if msg_is_trivial:
                        grp_row = conn.execute(
                            "SELECT nicknames_json FROM profiles WHERE profile_key = ?",
                            (f"group:{gid}",),
                        ).fetchone()
                        if grp_row is not None:
                            if not clean_group_name:
                                should_touch_group = False
                            else:
                                existing_nicks = [
                                    str(x).strip()
                                    for x in db.json_loads_safe(grp_row[0], [])
                                    if str(x).strip()
                                ]
                                if existing_nicks and existing_nicks[-1] == clean_group_name:
                                    should_touch_group = False
                    if should_touch_group:
                        db.upsert_profile_touch(
                            conn,
                            profile_key=f"group:{gid}",
                            scope=f"group:{gid}",
                            entity_id=gid,
                            nickname=clean_group_name,
                            group_id=gid,
                            ts=now,
                            inc_msg=1,
                        )
            else:
                # Keep DM activity isolated in dm:<uid>; never fabricate user_public data from DM.
                db.upsert_profile_touch(
                    conn,
                    profile_key=f"dm:{uid}",
                    scope=f"dm:{uid}",
                    entity_id=uid,
                    nickname=clean_sender_name,
                    group_id="",
                    ts=now,
                    inc_msg=1,
                )

    # Evaluate trigger OUTSIDE write_tx so we never hold the SQLite write lock while scanning.
    if norm_role == "user":
        with db.read_conn(db_path) as rconn:
            should_consolidate, delay, trigger_reason = _evaluate_trigger(
                conn=rconn,
                chat_key=eff_chat_key,
                scope=scope,
                is_group=eff_is_group,
                role=norm_role,
                clean_text=clean_text,
                ts=now,
                is_trivial=msg_is_trivial,
            )
    else:
        should_consolidate, delay, trigger_reason = False, 0.0, ""

    return {
        "recorded": True,
        "msg_id": msg_id,
        "chat_key": eff_chat_key,
        "scope": scope,
        "should_consolidate": should_consolidate,
        "delay": delay,
        "trigger_reason": trigger_reason,
    }


def _window_has_substantive_content(
    conn: sqlite3.Connection,
    chat_key: str,
    scope: str,
    min_chars: int,
) -> bool:
    user_rows = conn.execute(
        "SELECT text FROM raw_messages "
        "WHERE chat_key = ? AND scope = ? AND distilled = 0 AND role = 'user'",
        (chat_key, scope),
    ).fetchall()
    non_trivial_count = 0
    total_chars = 0
    for row in user_rows:
        msg = str(row[0] or "").strip()
        if not msg or tokenizer.is_trivial_message(msg):
            continue
        non_trivial_count += 1
        total_chars += len(msg)
    return non_trivial_count >= 1 and total_chars >= min_chars


def find_idle_windows(
    db_path: str | None = None,
    idle_mins: float | None = None,
    now_ts: int | None = None,
    min_chars: int = 12,
) -> list[str]:
    """Return sorted chat_keys whose undistilled messages have been idle long enough to flush."""
    eff_idle_mins = (
        db.get_env_float("MEMORY_IDLE_FLUSH_MINS", 10.0)
        if idle_mins is None
        else float(idle_mins)
    )
    eff_idle_mins = max(0.0, eff_idle_mins)
    now = (
        db.now_ts()
        if now_ts is None
        else (db.parse_ts(now_ts, db.now_ts()) or db.now_ts())
    )
    cutoff_ts = now - int(eff_idle_mins * 60)
    min_char_threshold = max(0, int(min_chars or 0))

    ready: set[str] = set()
    with db.read_conn(db_path) as conn:
        candidates = conn.execute(
            "SELECT chat_key, scope, MAX(ts) AS max_ts "
            "FROM raw_messages "
            "WHERE distilled = 0 "
            "GROUP BY chat_key, scope "
            "HAVING MAX(ts) <= ? "
            "ORDER BY chat_key ASC",
            (cutoff_ts,),
        ).fetchall()

        for cand in candidates:
            ck = str(cand[0] or "").strip()
            sc = str(cand[1] or "").strip()
            if not ck or not sc:
                continue
            if _window_has_substantive_content(conn, ck, sc, min_char_threshold):
                ready.add(ck)

    return sorted(ready)


def _query_stale_trivial_windows(
    conn: sqlite3.Connection,
    cutoff_ts: int,
    min_chars: int,
    limit: int,
) -> list[tuple[str, str]]:
    candidates = conn.execute(
        "SELECT chat_key, scope, MAX(ts) AS max_ts "
        "FROM raw_messages "
        "WHERE distilled = 0 "
        "GROUP BY chat_key, scope "
        "HAVING MAX(ts) <= ? "
        "ORDER BY MAX(ts) ASC, chat_key ASC",
        (cutoff_ts,),
    ).fetchall()

    stale: list[tuple[str, str]] = []
    seen_keys: set[str] = set()
    for cand in candidates:
        ck = str(cand[0] or "").strip()
        sc = str(cand[1] or "").strip()
        if not ck or not sc or ck in seen_keys:
            continue
        if not _window_has_substantive_content(conn, ck, sc, min_chars):
            seen_keys.add(ck)
            stale.append((ck, sc))
            if len(stale) >= limit:
                break
    return stale


def find_stale_trivial_windows(
    conn: sqlite3.Connection | str | None = None,
    min_age_secs: float = 1200.0,
    min_chars: int = 12,
    now_ts: int | None = None,
    limit: int = 20,
    db_path: str | None = None,
) -> list[str]:
    """Return chat_keys with stale undistilled windows containing only trivial/short messages.

    Accepts either an open ``sqlite3.Connection`` as ``conn``, a ``db_path`` string,
    or ``conn=None`` (opening a read connection to ``db_path`` / default DB).
    """
    eff_limit = max(1, int(limit or 20))
    now = (
        db.now_ts()
        if now_ts is None
        else (db.parse_ts(now_ts, db.now_ts()) or db.now_ts())
    )
    cutoff_ts = now - int(max(0.0, float(min_age_secs)))
    min_char_threshold = max(0, int(min_chars or 0))

    if isinstance(conn, sqlite3.Connection):
        return [
            ck for ck, _ in _query_stale_trivial_windows(conn, cutoff_ts, min_char_threshold, eff_limit)
        ]

    resolved_path = conn if isinstance(conn, str) else db_path
    with db.read_conn(resolved_path) as rconn:
        return [
            ck for ck, _ in _query_stale_trivial_windows(rconn, cutoff_ts, min_char_threshold, eff_limit)
        ]


def skip_stale_trivial_windows(
    db_path: str | None = None,
    min_age_secs: float = 1200.0,
    min_chars: int = 12,
    now_ts: int | None = None,
    limit: int = 20,
) -> int:
    """Mark stale trivial/short undistilled windows as ``distilled = 1, window_id = 'skip_trivial'``.

    Avoids burning LLM calls on permanently stuck short/chitchat windows.
    Returns the number of windows marked as skipped.
    """
    eff_limit = max(1, int(limit or 20))
    now = (
        db.now_ts()
        if now_ts is None
        else (db.parse_ts(now_ts, db.now_ts()) or db.now_ts())
    )
    cutoff_ts = now - int(max(0.0, float(min_age_secs)))
    min_char_threshold = max(0, int(min_chars or 0))

    with db.read_conn(db_path) as rconn:
        candidates = _query_stale_trivial_windows(rconn, cutoff_ts, min_char_threshold, eff_limit)
    if not candidates:
        return 0

    skipped_windows = 0
    with db.write_tx(db_path) as conn:
        for ck, sc in candidates:
            # Re-verify inside write_tx in case a new substantive message arrived concurrently
            max_row = conn.execute(
                "SELECT MAX(ts) FROM raw_messages "
                "WHERE chat_key = ? AND scope = ? AND distilled = 0",
                (ck, sc),
            ).fetchone()
            if max_row is None or max_row[0] is None or int(max_row[0]) > cutoff_ts:
                continue
            if _window_has_substantive_content(conn, ck, sc, min_char_threshold):
                continue
            cur = conn.execute(
                "UPDATE raw_messages SET distilled = 1, window_id = 'skip_trivial' "
                "WHERE chat_key = ? AND scope = ? AND distilled = 0 AND ts <= ?",
                (ck, sc, cutoff_ts),
            )
            if cur.rowcount and cur.rowcount > 0:
                skipped_windows += 1

    return skipped_windows


__all__ = [
    "MEMORY_TRIGGER_CUES",
    "clean_message_for_storage",
    "compute_dedup_hash",
    "find_idle_windows",
    "find_stale_trivial_windows",
    "has_trigger_cue",
    "record_message",
    "skip_stale_trivial_windows",
]
