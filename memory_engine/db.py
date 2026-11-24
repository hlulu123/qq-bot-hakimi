"""SQLite WAL database layer, schema, and privacy-isolation primitives for memory_engine.

Standard-library only. Safe for concurrent multi-process / multi-thread access
via WAL mode, busy_timeout, and BEGIN IMMEDIATE retry loops.
"""

from __future__ import annotations

import contextlib
import datetime
import json
import math
import os
import re
import sqlite3
import sys
import threading
import time
import uuid

# ---------------------------------------------------------------------------
# Project root & bot_env bootstrap (no hardcoded personal paths)
# ---------------------------------------------------------------------------

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

try:
    import bot_env  # noqa: E402,F401  (loads bot.env before reading os.environ)
except Exception:
    pass

# ---------------------------------------------------------------------------
# Time & Date Helpers (UTC+8)
# ---------------------------------------------------------------------------

TZ_CN = datetime.timezone(datetime.timedelta(hours=8))

_ISO_FRAC_RE = re.compile(r"(\.\d+)")
_NUM_STR_RE = re.compile(r"^[+-]?\d+(?:\.\d+)?$")
_DATE_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y-%m-%d",
    "%Y/%m/%d",
)


def now_ts() -> int:
    """Return current Unix epoch timestamp in integer seconds."""
    return int(time.time())


def parse_ts(val, default: int = 0) -> int:
    """Parse int/float epoch, datetime/date, YYYY-MM-DD[ HH:MM[:SS]], or ISO string -> epoch seconds."""
    if val is None or isinstance(val, bool):
        return default

    if isinstance(val, (int, float)):
        if isinstance(val, float) and (math.isnan(val) or math.isinf(val)):
            return default
        num = float(val)
        if abs(num) >= 1e12:
            num /= 1000.0
        return int(num)

    if isinstance(val, datetime.datetime):
        dt = val if val.tzinfo is not None else val.replace(tzinfo=TZ_CN)
        return int(dt.timestamp())

    if isinstance(val, datetime.date):
        dt = datetime.datetime(val.year, val.month, val.day, tzinfo=TZ_CN)
        return int(dt.timestamp())

    if not isinstance(val, str):
        return default

    s = val.strip()
    if not s:
        return default

    if _NUM_STR_RE.match(s):
        try:
            num = float(s)
            if math.isnan(num) or math.isinf(num):
                return default
            if abs(num) >= 1e12:
                num /= 1000.0
            return int(num)
        except ValueError:
            return default

    for fmt in _DATE_FORMATS:
        try:
            dt = datetime.datetime.strptime(s, fmt).replace(tzinfo=TZ_CN)
            return int(dt.timestamp())
        except ValueError:
            continue

    # ISO-8601 fallback (compatible with Python 3.9)
    iso = s
    if iso.endswith(("Z", "z")):
        iso = iso[:-1] + "+00:00"
    # Normalize fractional seconds to 6 digits for Python 3.9 fromisoformat()
    def _norm_frac(match: re.Match) -> str:
        digits = match.group(1)[1:]
        if len(digits) > 6:
            digits = digits[:6]
        else:
            digits = digits.ljust(6, "0")
        return "." + digits

    iso_norm = _ISO_FRAC_RE.sub(_norm_frac, iso, count=1)
    try:
        dt = datetime.datetime.fromisoformat(iso_norm)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ_CN)
        return int(dt.timestamp())
    except ValueError:
        return default


def now_str(ts=None) -> str:
    """Format epoch timestamp (default: now) as 'YYYY-MM-DD HH:MM' in UTC+8."""
    if ts is None:
        val = now_ts()
    else:
        val = parse_ts(ts, -1)
        if val < 0:
            return ""
    try:
        return datetime.datetime.fromtimestamp(val, TZ_CN).strftime("%Y-%m-%d %H:%M")
    except (OSError, OverflowError, ValueError):
        return ""


def today_str(ts=None) -> str:
    """Format epoch timestamp (default: now) as 'YYYY-MM-DD' in UTC+8."""
    if ts is None:
        val = now_ts()
    else:
        val = parse_ts(ts, -1)
        if val < 0:
            return ""
    try:
        return datetime.datetime.fromtimestamp(val, TZ_CN).strftime("%Y-%m-%d")
    except (OSError, OverflowError, ValueError):
        return ""


def ts_to_date(ts) -> str:
    """Convert positive epoch timestamp (or parseable date) to 'YYYY-MM-DD' in UTC+8, else ''."""
    val = parse_ts(ts, 0)
    if val <= 0:
        return ""
    try:
        return datetime.datetime.fromtimestamp(val, TZ_CN).strftime("%Y-%m-%d")
    except (OSError, OverflowError, ValueError):
        return ""


# ---------------------------------------------------------------------------
# Environment & Config Helpers
# ---------------------------------------------------------------------------

def get_env_str(key: str, default: str = "") -> str:
    raw = os.environ.get(key)
    if raw is None:
        return default
    val = raw.strip()
    return val if val else default


def get_env_int(key: str, default: int = 0) -> int:
    raw = os.environ.get(key)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except (ValueError, TypeError):
        return default


def get_env_float(key: str, default: float = 0.0) -> float:
    raw = os.environ.get(key)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw.strip())
    except (ValueError, TypeError):
        return default


def get_env_bool(key: str, default: bool = False) -> bool:
    raw = os.environ.get(key)
    if raw is None or not raw.strip():
        return default
    val = raw.strip().lower()
    if val in ("1", "true", "yes", "on", "y"):
        return True
    if val in ("0", "false", "no", "off", "n"):
        return False
    return default


def is_memory_enabled() -> bool:
    return get_env_bool("USER_MEMORY_ENABLED", True)


def default_db_path() -> str:
    raw = get_env_str("MEMORY_DB_PATH", "")
    if raw:
        return os.path.expanduser(raw)
    return os.path.join(PROJECT_DIR, "data", "memory.db")


def default_extractor_cwd() -> str:
    raw = get_env_str("MEMORY_EXTRACTOR_CWD", "")
    if raw:
        return os.path.expanduser(raw)
    return os.path.join(PROJECT_DIR, "data", "memory_extractor_cwd")


def is_group_memory_enabled() -> bool:
    return get_env_bool("MEMORY_GROUP_MEMORY_ENABLED", True)


# ---------------------------------------------------------------------------
# Identifiers, Chat Keys & Privacy Hard-Isolation Helpers
# ---------------------------------------------------------------------------

_UID_STRIP_RE = re.compile(r"[^0-9A-Za-z_-]")


def sanitize_uid(user_id) -> str:
    """Keep only [0-9A-Za-z_-] characters from a user/group identifier."""
    return _UID_STRIP_RE.sub("", str(user_id or ""))


def make_chat_key(is_group: bool, group_id: str = "", user_id: str = "") -> str:
    if is_group:
        return f"g:{sanitize_uid(group_id)}"
    return f"dm:{sanitize_uid(user_id)}"


def parse_chat_key(chat_key: str) -> tuple[bool, str, str]:
    """Parse chat_key into (is_group, group_id, user_id)."""
    s = str(chat_key or "").strip()
    if not s:
        return (False, "", "")
    if s.startswith("g:"):
        return (True, sanitize_uid(s[2:]), "")
    if s.startswith("group:"):
        return (True, sanitize_uid(s[6:]), "")
    if s.startswith("dm:"):
        return (False, "", sanitize_uid(s[3:]))
    if s.startswith("private:"):
        return (False, "", sanitize_uid(s[8:]))
    return (False, "", sanitize_uid(s))


def chat_scope_for(is_group: bool, group_id: str = "", user_id: str = "") -> str:
    if is_group:
        return f"group:{sanitize_uid(group_id)}"
    return f"dm:{sanitize_uid(user_id)}"


def allowed_scopes_for_chat(
    is_group: bool,
    group_id: str = "",
    user_id: str = "",
    include_group_memory: bool | None = None,
) -> list[str]:
    """Return the exact list of scopes readable in this chat (hard privacy boundary).

    - Group chat: ['user_public', 'group:<gid>'] when gid non-empty & group memory
      is enabled, otherwise ['user_public']. Never includes any 'dm:*' or other 'group:*'.
    - DM chat: ['user_public', 'dm:<uid>'] when uid non-empty, otherwise ['user_public'].
      Never includes other users' 'dm:*' or any 'group:*'.
    """
    if is_group:
        gid = sanitize_uid(group_id)
        grp_ok = (
            is_group_memory_enabled()
            if include_group_memory is None
            else bool(include_group_memory)
        )
        if gid and grp_ok:
            return ["user_public", f"group:{gid}"]
        return ["user_public"]

    uid = sanitize_uid(user_id)
    if uid:
        return ["user_public", f"dm:{uid}"]
    return ["user_public"]


def is_write_scope_allowed(
    chat_is_group: bool,
    chat_group_id: str,
    chat_user_id: str,
    target_scope: str,
) -> bool:
    """Enforce hard write-scope isolation so DM secrets can never leak to public/group.

    - Group window: allows 'user_public', and 'group:<chat_group_id>' when
      chat_group_id is non-empty and group memory is enabled. Rejects all 'dm:*'
      and all other 'group:*'.
    - DM window: allows ONLY 'dm:<chat_user_id>' when chat_user_id is non-empty.
      Rejects 'user_public', all 'group:*', and all other 'dm:*'.
    """
    scope = str(target_scope or "").strip()
    if not scope:
        return False

    if chat_is_group:
        if scope == "user_public":
            return True
        gid = sanitize_uid(chat_group_id)
        if gid and is_group_memory_enabled() and scope == f"group:{gid}":
            return True
        return False

    uid = sanitize_uid(chat_user_id)
    if uid and scope == f"dm:{uid}":
        return True
    return False


def new_memory_id() -> str:
    return "mem_" + uuid.uuid4().hex[:16]


def new_window_id() -> str:
    return "win_" + uuid.uuid4().hex[:16]


# ---------------------------------------------------------------------------
# JSON Helpers
# ---------------------------------------------------------------------------

def json_loads_safe(raw, default):
    """Safely decode a JSON string into a Python object, falling back to `default`."""
    if raw is None:
        return default
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            return default
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return default
        try:
            parsed = json.loads(s)
        except (ValueError, TypeError):
            return default
    elif isinstance(raw, (dict, list)):
        parsed = raw
    else:
        return default

    if parsed is None:
        return default
    if isinstance(default, list) and not isinstance(parsed, list):
        return default
    if isinstance(default, dict) and not isinstance(parsed, dict):
        return default
    return parsed


def json_dumps_compact(obj) -> str:
    """Serialize object to compact UTF-8 JSON without ASCII escaping."""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


# ---------------------------------------------------------------------------
# SQLite Schema (8 Core Tables + meta_kv)
# ---------------------------------------------------------------------------

SCHEMA_VERSION = "2"
USER_MEMORY_DELETE_GENERATION = "user_memory_delete_generation"

_SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS raw_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_key TEXT NOT NULL,
        scope TEXT NOT NULL,
        group_id TEXT NOT NULL DEFAULT '',
        sender_uid TEXT NOT NULL DEFAULT '',
        sender_name TEXT NOT NULL DEFAULT '',
        role TEXT NOT NULL DEFAULT 'user',
        text TEXT NOT NULL,
        dedup_hash TEXT NOT NULL DEFAULT '',
        ts INTEGER NOT NULL,
        distilled INTEGER NOT NULL DEFAULT 0,
        window_id TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_raw_messages_chat_distilled ON raw_messages(chat_key, distilled, id)",
    "CREATE INDEX IF NOT EXISTS idx_raw_messages_chat_ts ON raw_messages(chat_key, ts)",
    "CREATE INDEX IF NOT EXISTS idx_raw_messages_dedup ON raw_messages(chat_key, dedup_hash, ts)",
    "CREATE INDEX IF NOT EXISTS idx_raw_messages_sender_ts ON raw_messages(sender_uid, ts)",
    "CREATE INDEX IF NOT EXISTS idx_raw_messages_window ON raw_messages(window_id)",
    "CREATE INDEX IF NOT EXISTS idx_raw_messages_undistilled ON raw_messages(chat_key, scope, ts) WHERE distilled = 0",
    "CREATE INDEX IF NOT EXISTS idx_raw_messages_chat_id ON raw_messages(chat_key, id)",
    """
    CREATE TABLE IF NOT EXISTS session_handoffs (
        window_id TEXT PRIMARY KEY,
        chat_key TEXT NOT NULL,
        scope TEXT NOT NULL,
        msg_start_id INTEGER NOT NULL DEFAULT 0,
        msg_end_id INTEGER NOT NULL DEFAULT 0,
        summary TEXT NOT NULL DEFAULT '',
        open_threads TEXT NOT NULL DEFAULT '',
        created_at INTEGER NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_session_handoffs_chat_created ON session_handoffs(chat_key, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_session_handoffs_scope_created ON session_handoffs(scope, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_handoffs_created ON session_handoffs(created_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS profiles (
        profile_key TEXT PRIMARY KEY,
        scope TEXT NOT NULL,
        entity_id TEXT NOT NULL,
        nicknames_json TEXT NOT NULL DEFAULT '[]',
        seen_groups_json TEXT NOT NULL DEFAULT '[]',
        relationship TEXT NOT NULL DEFAULT '',
        summary TEXT NOT NULL DEFAULT '',
        msg_count INTEGER NOT NULL DEFAULT 0,
        first_seen_ts INTEGER NOT NULL DEFAULT 0,
        last_seen_ts INTEGER NOT NULL DEFAULT 0,
        updated_at INTEGER NOT NULL DEFAULT 0,
        profile_updated_at INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_profiles_scope_entity ON profiles(scope, entity_id)",
    "CREATE INDEX IF NOT EXISTS idx_profiles_entity ON profiles(entity_id)",
    "CREATE INDEX IF NOT EXISTS idx_profiles_last_seen ON profiles(last_seen_ts DESC)",
    """
    CREATE TABLE IF NOT EXISTS memories (
        id TEXT PRIMARY KEY,
        scope TEXT NOT NULL,
        owner_uid TEXT NOT NULL DEFAULT '',
        group_id TEXT NOT NULL DEFAULT '',
        kind TEXT NOT NULL DEFAULT 'stable',
        category TEXT NOT NULL DEFAULT '其他',
        fact_key TEXT NOT NULL DEFAULT '',
        text TEXT NOT NULL,
        keywords_json TEXT NOT NULL DEFAULT '[]',
        importance INTEGER NOT NULL DEFAULT 3,
        proof_count INTEGER NOT NULL DEFAULT 1,
        occurred_at TEXT NOT NULL DEFAULT '',
        ended_at INTEGER NOT NULL DEFAULT 0,
        last_mentioned_ts INTEGER NOT NULL DEFAULT 0,
        last_recalled_ts INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'active',
        superseded_by TEXT NOT NULL DEFAULT '',
        history_json TEXT NOT NULL DEFAULT '[]',
        source_window_id TEXT NOT NULL DEFAULT '',
        embedding BLOB,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_memories_scope_status_owner ON memories(scope, status, owner_uid)",
    "CREATE INDEX IF NOT EXISTS idx_memories_scope_status_group ON memories(scope, status, group_id)",
    "CREATE INDEX IF NOT EXISTS idx_memories_fact_key ON memories(scope, owner_uid, group_id, fact_key, status)",
    "CREATE INDEX IF NOT EXISTS idx_memories_status_kind_updated ON memories(status, kind, updated_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_memories_owner_status ON memories(owner_uid, status)",
    "CREATE INDEX IF NOT EXISTS idx_memories_source_window ON memories(source_window_id)",
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
        mem_id UNINDEXED,
        scope UNINDEXED,
        owner_uid UNINDEXED,
        group_id UNINDEXED,
        tokens,
        keywords,
        raw_text,
        tokenize = 'unicode61 remove_diacritics 0'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS relations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        scope TEXT NOT NULL,
        group_id TEXT NOT NULL DEFAULT '',
        src_type TEXT NOT NULL DEFAULT 'user',
        src_id TEXT NOT NULL,
        rel_type TEXT NOT NULL,
        dst_type TEXT NOT NULL DEFAULT 'user',
        dst_id TEXT NOT NULL,
        note TEXT NOT NULL DEFAULT '',
        proof_count INTEGER NOT NULL DEFAULT 1,
        source_window_id TEXT NOT NULL DEFAULT '',
        updated_at INTEGER NOT NULL
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_relations_unique ON relations(scope, group_id, src_type, src_id, rel_type, dst_type, dst_id)",
    "CREATE INDEX IF NOT EXISTS idx_relations_src ON relations(scope, src_type, src_id)",
    "CREATE INDEX IF NOT EXISTS idx_relations_dst ON relations(scope, dst_type, dst_id)",
    "CREATE INDEX IF NOT EXISTS idx_relations_group ON relations(scope, group_id)",
    """
    CREATE TABLE IF NOT EXISTS audit_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_type TEXT NOT NULL,
        chat_key TEXT NOT NULL DEFAULT '',
        action TEXT NOT NULL,
        target_id TEXT NOT NULL DEFAULT '',
        detail_json TEXT NOT NULL DEFAULT '{}',
        created_at INTEGER NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_audit_logs_created ON audit_logs(created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_audit_logs_chat_created ON audit_logs(chat_key, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_audit_logs_run_action ON audit_logs(run_type, action, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_audit_logs_target ON audit_logs(target_id)",
    """
    CREATE TABLE IF NOT EXISTS recall_traces (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_key TEXT NOT NULL,
        session_id TEXT NOT NULL DEFAULT '',
        sender_uid TEXT NOT NULL DEFAULT '',
        query_text TEXT NOT NULL DEFAULT '',
        gate_info_json TEXT NOT NULL DEFAULT '{}',
        selected_json TEXT NOT NULL DEFAULT '[]',
        omitted_json TEXT NOT NULL DEFAULT '[]',
        created_at INTEGER NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_recall_traces_chat_created ON recall_traces(chat_key, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_recall_traces_session ON recall_traces(session_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_recall_traces_sender ON recall_traces(sender_uid, created_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS meta_kv (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at INTEGER NOT NULL
    )
    """,
)


# ---------------------------------------------------------------------------
# Thread-Local Connection Pool & Lifecycle
# ---------------------------------------------------------------------------

_THREAD_LOCAL = threading.local()
_INIT_LOCK = threading.Lock()
_INIT_PATHS: set[tuple[str, tuple[int, int] | None]] = set()


def _resolve_db_path(db_path: str | None = None) -> str:
    raw = str(db_path).strip() if db_path is not None else ""
    if not raw:
        raw = default_db_path()
    if raw == ":memory:" or raw.startswith("file:"):
        return raw
    return os.path.abspath(os.path.expanduser(raw))


def _is_file_db(resolved: str) -> bool:
    return bool(resolved) and resolved != ":memory:" and not resolved.startswith("file:")


def _file_identity(path: str) -> tuple[int, int] | None:
    if not _is_file_db(path):
        return None
    try:
        st = os.stat(path)
        return (st.st_dev, st.st_ino)
    except OSError:
        return None


def _is_locked_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "locked" in msg or "busy" in msg


def _thread_conns() -> dict[str, tuple[sqlite3.Connection, tuple[int, int] | None]]:
    conns = getattr(_THREAD_LOCAL, "conns", None)
    if conns is None:
        conns = {}
        _THREAD_LOCAL.conns = conns
    return conns


_VALID_WAL_CHECKPOINT_MODES = frozenset({"PASSIVE", "FULL", "RESTART", "TRUNCATE"})


def _apply_pragmas(conn: sqlite3.Connection, retries: int = 5) -> None:
    """Apply standard WAL and performance pragmas to a freshly opened connection."""
    conn.execute("PRAGMA busy_timeout=5000;")
    attempts = max(1, int(retries))
    for attempt in range(attempts):
        try:
            row = conn.execute("PRAGMA journal_mode;").fetchone()
            cur_mode = str(row[0] or "").strip().lower() if row else ""
            if cur_mode != "wal":
                conn.execute("PRAGMA journal_mode=WAL;")
            break
        except sqlite3.OperationalError as exc:
            if _is_locked_error(exc) and attempt + 1 < attempts:
                time.sleep(min(1.0, 0.05 * (2 ** attempt)))
                continue
            raise
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA wal_autocheckpoint=200;")
    conn.execute("PRAGMA journal_size_limit=2097152;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA temp_store=MEMORY;")


def checkpoint_wal(db_path: str | None = None, mode: str = "TRUNCATE") -> dict:
    """Run ``PRAGMA wal_checkpoint(mode)`` to keep WAL size bounded.

    Validates ``mode`` in ``("PASSIVE", "FULL", "RESTART", "TRUNCATE")`` and falls
    back to ``"PASSIVE"`` if ``"TRUNCATE"`` raises ``sqlite3.OperationalError``.
    """
    eff_mode = str(mode or "TRUNCATE").strip().upper()
    if eff_mode not in _VALID_WAL_CHECKPOINT_MODES:
        raise ValueError(f"Unsupported WAL checkpoint mode: {mode!r}")

    conn = get_connection(db_path)
    used_mode = eff_mode
    row = None
    try:
        row = conn.execute(f"PRAGMA wal_checkpoint({used_mode});").fetchone()
    except sqlite3.OperationalError:
        if used_mode == "TRUNCATE":
            used_mode = "PASSIVE"
            try:
                row = conn.execute("PRAGMA wal_checkpoint(PASSIVE);").fetchone()
            except sqlite3.Error:
                return {
                    "ok": False,
                    "mode": used_mode,
                    "busy": 1,
                    "log": -1,
                    "checkpointed": -1,
                }
        else:
            return {
                "ok": False,
                "mode": used_mode,
                "busy": 1,
                "log": -1,
                "checkpointed": -1,
            }
    except sqlite3.Error:
        return {
            "ok": False,
            "mode": used_mode,
            "busy": 1,
            "log": -1,
            "checkpointed": -1,
        }

    if row is None or len(row) < 3:
        return {
            "ok": False,
            "mode": used_mode,
            "busy": 1,
            "log": -1,
            "checkpointed": -1,
        }
    busy = int(row[0])
    log_frames = int(row[1])
    checkpointed = int(row[2])
    return {
        "ok": busy == 0,
        "mode": used_mode,
        "busy": busy,
        "log": log_frames,
        "checkpointed": checkpointed,
    }


def _schema_exists(conn: sqlite3.Connection) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta_kv'"
        ).fetchone()
        return row is not None
    except sqlite3.Error:
        return False


def _ensure_schema(conn: sqlite3.Connection, retries: int = 5) -> None:
    """Create all tables, additive columns, and indexes idempotently inside BEGIN IMMEDIATE."""
    attempts = max(1, int(retries))
    for attempt in range(attempts):
        started = False
        try:
            if not conn.in_transaction:
                conn.execute("BEGIN IMMEDIATE")
                started = True
            for stmt in _SCHEMA_STATEMENTS:
                conn.execute(stmt)
            # Additive migration for existing live DBs missing profiles.profile_updated_at
            prof_cols = {
                str(r[1])
                for r in conn.execute("PRAGMA table_info(profiles)").fetchall()
                if len(r) >= 2
            }
            added_profile_updated_at = False
            if "profile_updated_at" not in prof_cols:
                conn.execute(
                    "ALTER TABLE profiles ADD COLUMN profile_updated_at INTEGER NOT NULL DEFAULT 0"
                )
                added_profile_updated_at = True
            mig_row = conn.execute(
                "SELECT value FROM meta_kv WHERE key = 'mig:profile_updated_at'"
            ).fetchone()
            if added_profile_updated_at or mig_row is None:
                conn.execute(
                    "UPDATE profiles SET profile_updated_at = updated_at "
                    "WHERE profile_updated_at = 0 AND (summary != '' OR relationship != '')"
                )
                conn.execute(
                    "INSERT OR REPLACE INTO meta_kv(key, value, updated_at) VALUES (?, ?, ?)",
                    ("mig:profile_updated_at", "1", now_ts()),
                )
            memory_cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(memories)")}
            if "ended_at" not in memory_cols:
                conn.execute("ALTER TABLE memories ADD COLUMN ended_at INTEGER NOT NULL DEFAULT 0")
                archived_rows = conn.execute(
                    "SELECT id, history_json, updated_at FROM memories WHERE status = 'archived'"
                ).fetchall()
                for row in archived_rows:
                    history = json_loads_safe(row["history_json"], [])
                    last_entry = history[-1] if isinstance(history, list) and history else None
                    if isinstance(last_entry, dict) and last_entry.get("action") == "end":
                        conn.execute(
                            "UPDATE memories SET status = 'ended', ended_at = ? WHERE id = ?",
                            (parse_ts(last_entry.get("ts"), int(row["updated_at"])), row["id"]),
                        )
            conn.execute(
                "INSERT OR IGNORE INTO meta_kv(key, value, updated_at) VALUES (?, ?, ?)",
                ("schema_version", SCHEMA_VERSION, now_ts()),
            )
            if not get_meta(conn, "mig:index_archived_memories"):
                missing = conn.execute(
                    "SELECT id, scope, owner_uid, group_id, text, keywords_json FROM memories AS m "
                    "WHERE status = 'archived' AND id NOT IN "
                    "(SELECT mem_id FROM memories_fts WHERE mem_id IS NOT NULL)"
                ).fetchall()
                for row in missing:
                    sync_memory_fts(conn, *row)
                set_meta(conn, "mig:index_archived_memories", "1")
            if started:
                conn.execute("COMMIT")
            return
        except sqlite3.OperationalError as exc:
            if started and conn.in_transaction:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            if _is_locked_error(exc) and attempt + 1 < attempts:
                time.sleep(min(2.0, 0.05 * (2 ** attempt)))
                continue
            raise
        except BaseException:
            if started and conn.in_transaction:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            raise


def _open_configured_connection(resolved: str) -> tuple[sqlite3.Connection, tuple[int, int] | None]:
    if _is_file_db(resolved):
        parent = os.path.dirname(resolved)
        if parent:
            os.makedirs(parent, exist_ok=True)
    uri = resolved.startswith("file:")
    conn = sqlite3.connect(
        resolved,
        timeout=5.0,
        isolation_level=None,
        uri=uri,
    )
    try:
        conn.row_factory = sqlite3.Row
        _apply_pragmas(conn)
        file_id = _file_identity(resolved)
        need_init = True
        if _is_file_db(resolved) and file_id is not None:
            with _INIT_LOCK:
                in_cache = (resolved, file_id) in _INIT_PATHS
            if in_cache:
                # Perform SQLite I/O outside _INIT_LOCK to avoid blocking other threads.
                if _schema_exists(conn):
                    need_init = False
                else:
                    with _INIT_LOCK:
                        _INIT_PATHS.discard((resolved, file_id))
        if need_init:
            _ensure_schema(conn)
            file_id = _file_identity(resolved)
            if _is_file_db(resolved) and file_id is not None:
                with _INIT_LOCK:
                    _INIT_PATHS.add((resolved, file_id))
        return conn, file_id
    except BaseException:
        try:
            conn.close()
        except sqlite3.Error:
            pass
        raise


def get_connection(db_path: str | None = None) -> sqlite3.Connection:
    """Return a configured thread-local SQLite connection with schema guaranteed ready."""
    resolved = _resolve_db_path(db_path)
    conns = _thread_conns()
    cached = conns.get(resolved)

    if cached is not None:
        conn, cached_fid = cached
        alive = True
        try:
            _ = conn.in_transaction
        except sqlite3.Error:
            alive = False

        if alive and _is_file_db(resolved):
            cur_fid = _file_identity(resolved)
            if cur_fid is None or cur_fid != cached_fid:
                alive = False

        if alive:
            return conn

        # Drop stale/deleted/closed connection
        conns.pop(resolved, None)
        with _INIT_LOCK:
            _INIT_PATHS.discard((resolved, cached_fid))
        try:
            conn.close()
        except sqlite3.Error:
            pass

    conn, file_id = _open_configured_connection(resolved)
    conns[resolved] = (conn, file_id)
    return conn


def init_db(db_path: str | None = None) -> str:
    """Create parent directory, initialize schema idempotently, and return resolved db_path."""
    resolved = _resolve_db_path(db_path)
    if _is_file_db(resolved):
        parent = os.path.dirname(resolved)
        if parent:
            os.makedirs(parent, exist_ok=True)
    conn = get_connection(resolved)
    _ensure_schema(conn)
    if _is_file_db(resolved):
        fid = _file_identity(resolved)
        with _INIT_LOCK:
            _INIT_PATHS.add((resolved, fid))
    return resolved


def close_connections(db_path: str | None = None) -> None:
    """Close thread-local cached SQLite connection(s) cleanly (useful for tests/cleanup)."""
    conns = _thread_conns()
    if db_path is not None:
        resolved = _resolve_db_path(db_path)
        entry = conns.pop(resolved, None)
        if entry is not None:
            conn, _ = entry
            try:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            try:
                conn.close()
            except sqlite3.Error:
                pass
        with _INIT_LOCK:
            to_drop = [item for item in _INIT_PATHS if item[0] == resolved]
            for item in to_drop:
                _INIT_PATHS.discard(item)
        return

    keys = list(conns.keys())
    for key in keys:
        entry = conns.pop(key, None)
        if entry is not None:
            conn, _ = entry
            try:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            try:
                conn.close()
            except sqlite3.Error:
                pass
    with _INIT_LOCK:
        _INIT_PATHS.clear()


@contextlib.contextmanager
def read_conn(db_path: str | None = None):
    """Yield a configured SQLite connection for read queries."""
    conn = get_connection(db_path)
    yield conn


@contextlib.contextmanager
def write_tx(db_path: str | None = None, retries: int = 5):
    """Run a write transaction with BEGIN IMMEDIATE and exponential backoff on lock contention.

    Supports nested write_tx() calls on the same thread via SQLite SAVEPOINTs.
    """
    conn = get_connection(db_path)
    attempts = max(1, int(retries))

    if conn.in_transaction:
        sp_name = f"sp_{uuid.uuid4().hex[:12]}"
        conn.execute(f"SAVEPOINT {sp_name}")
        try:
            yield conn
        except BaseException:
            try:
                conn.execute(f"ROLLBACK TO SAVEPOINT {sp_name}")
                conn.execute(f"RELEASE SAVEPOINT {sp_name}")
            except sqlite3.Error:
                pass
            raise
        else:
            conn.execute(f"RELEASE SAVEPOINT {sp_name}")
        return

    for attempt in range(attempts):
        try:
            conn.execute("BEGIN IMMEDIATE")
            break
        except sqlite3.OperationalError as exc:
            if _is_locked_error(exc) and attempt + 1 < attempts:
                time.sleep(min(2.0, 0.05 * (2 ** attempt)))
                continue
            raise

    try:
        yield conn
    except BaseException:
        if conn.in_transaction:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
        raise
    else:
        if conn.in_transaction:
            for commit_attempt in range(attempts):
                try:
                    conn.execute("COMMIT")
                    break
                except sqlite3.OperationalError as exc:
                    if _is_locked_error(exc) and commit_attempt + 1 < attempts:
                        time.sleep(min(2.0, 0.05 * (2 ** commit_attempt)))
                        continue
                    if conn.in_transaction:
                        try:
                            conn.execute("ROLLBACK")
                        except sqlite3.Error:
                            pass
                    raise


# ---------------------------------------------------------------------------
# FTS5 Index Sync Helpers
# ---------------------------------------------------------------------------

def _parse_keywords_list(keywords) -> list[str]:
    if keywords is None:
        return []
    if isinstance(keywords, (list, tuple, set)):
        return [str(k).strip() for k in keywords if str(k).strip()]
    if isinstance(keywords, str):
        s = keywords.strip()
        if not s:
            return []
        if s.startswith("[") and s.endswith("]"):
            parsed = json_loads_safe(s, None)
            if isinstance(parsed, list):
                return [str(k).strip() for k in parsed if str(k).strip()]
        return [part for part in s.split() if part]
    s = str(keywords).strip()
    return [s] if s else []


def _normalize_keywords_text(keywords) -> str:
    return " ".join(_parse_keywords_list(keywords))


def _tokenize_for_fts_fallback(raw_text: str, kw_text: str | list[str] = "") -> str:
    try:
        from . import tokenizer as _tokenizer

        tok_fn = getattr(_tokenizer, "tokenize_for_fts", None)
        if callable(tok_fn):
            kw_list = _parse_keywords_list(kw_text)
            tokenized = str(tok_fn(raw_text, extra_keywords=kw_list or None) or "").strip()
            if tokenized:
                return tokenized
    except Exception:
        pass
    return raw_text


def sync_memory_fts(
    conn: sqlite3.Connection,
    mem_id: str,
    scope: str,
    owner_uid: str,
    group_id: str,
    text: str,
    keywords: list[str] | str,
    tokens_str: str = "",
) -> None:
    """Replace the FTS5 index row for `mem_id`."""
    mid = str(mem_id or "").strip()
    if not mid:
        return
    raw_text = str(text or "")
    kw_list = _parse_keywords_list(keywords)
    kw_text = " ".join(kw_list)
    tokens_val = str(tokens_str or "").strip()
    if not tokens_val:
        tokens_val = _tokenize_for_fts_fallback(raw_text, kw_list)

    conn.execute("DELETE FROM memories_fts WHERE mem_id = ?", (mid,))
    conn.execute(
        "INSERT INTO memories_fts(mem_id, scope, owner_uid, group_id, tokens, keywords, raw_text) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            mid,
            str(scope or ""),
            str(owner_uid or ""),
            str(group_id or ""),
            tokens_val,
            kw_text,
            raw_text,
        ),
    )


def delete_memory_fts(conn: sqlite3.Connection, mem_id: str) -> None:
    """Remove `mem_id` from the FTS5 index."""
    mid = str(mem_id or "").strip()
    if mid:
        conn.execute("DELETE FROM memories_fts WHERE mem_id = ?", (mid,))


def rebuild_fts_index(conn: sqlite3.Connection) -> int:
    """Rebuild the FTS index, including ended rows for explicit historical recall."""
    conn.execute("DELETE FROM memories_fts")
    rows = conn.execute(
        "SELECT id, scope, owner_uid, group_id, text, keywords_json "
        "FROM memories WHERE status IN ('active', 'archived', 'ended')"
    ).fetchall()
    count = 0
    for row in rows:
        mem_id = str(row[0] or "").strip()
        if not mem_id:
            continue
        scope = str(row[1] or "")
        owner_uid = str(row[2] or "")
        group_id = str(row[3] or "")
        raw_text = str(row[4] or "")
        kw_list = _parse_keywords_list(row[5])
        kw_text = " ".join(kw_list)
        tokens_val = _tokenize_for_fts_fallback(raw_text, kw_list)
        conn.execute(
            "INSERT INTO memories_fts(mem_id, scope, owner_uid, group_id, tokens, keywords, raw_text) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (mem_id, scope, owner_uid, group_id, tokens_val, kw_text, raw_text),
        )
        count += 1
    return count


# ---------------------------------------------------------------------------
# Audit Logs, Meta KV & Profile Touch Helpers
# ---------------------------------------------------------------------------

def log_audit(
    conn: sqlite3.Connection,
    run_type: str,
    chat_key: str,
    action: str,
    target_id: str = "",
    detail=None,
    created_at: int | None = None,
) -> int:
    """Append an audit log record and return its inserted row id."""
    ts = now_ts() if created_at is None else parse_ts(created_at, now_ts())
    if detail is None:
        detail_json = "{}"
    elif isinstance(detail, str):
        stripped = detail.strip()
        if not stripped:
            detail_json = "{}"
        else:
            try:
                json.loads(stripped)
                detail_json = stripped
            except (ValueError, TypeError):
                detail_json = json_dumps_compact({"detail": stripped})
    else:
        detail_json = json_dumps_compact(detail)

    cur = conn.execute(
        "INSERT INTO audit_logs(run_type, chat_key, action, target_id, detail_json, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            str(run_type or ""),
            str(chat_key or ""),
            str(action or ""),
            str(target_id or ""),
            detail_json,
            int(ts),
        ),
    )
    return int(cur.lastrowid or 0)


def get_meta(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM meta_kv WHERE key = ?", (str(key),)).fetchone()
    if row is None or row[0] is None:
        return default
    return str(row[0])


def set_meta(
    conn: sqlite3.Connection,
    key: str,
    value: str,
    updated_at: int | None = None,
) -> None:
    ts = now_ts() if updated_at is None else parse_ts(updated_at, now_ts())
    conn.execute(
        "INSERT INTO meta_kv(key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (str(key), str(value), int(ts)),
    )


def acquire_lease(
    lease_key: str,
    owner: str = "",
    ttl_secs: int = 120,
    db_path: str | None = None,
    now_ts: int | None = None,
) -> tuple[bool, str]:
    """Atomically acquire a cross-process lease stored as ``lease:{lease_key}`` in ``meta_kv``.

    Succeeds if missing or expired (``expires_at <= now``).
    Returns ``(True, owner_token)`` on acquisition or ``(False, existing_owner)`` if held.
    """
    clean_key = str(lease_key or "").strip()
    if not clean_key:
        return False, ""
    meta_key = f"lease:{clean_key}"
    cur_epoch = int(time.time())
    now = cur_epoch if now_ts is None else int(parse_ts(now_ts, cur_epoch))
    ttl = max(5, int(ttl_secs))
    token = str(owner or "").strip() or f"pid{os.getpid()}_{uuid.uuid4().hex[:12]}"

    with write_tx(db_path) as conn:
        row = conn.execute(
            "SELECT value FROM meta_kv WHERE key = ?",
            (meta_key,),
        ).fetchone()
        if row is not None and row[0]:
            existing = json_loads_safe(row[0], {})
            existing_exp = parse_ts(existing.get("expires_at"), 0)
            existing_owner = str(existing.get("owner") or "")
            if existing_exp > now:
                return False, existing_owner

        payload = {
            "owner": token,
            "pid": os.getpid(),
            "acquired_at": now,
            "expires_at": now + ttl,
        }
        set_meta(conn, meta_key, json_dumps_compact(payload), updated_at=now)
        return True, token


def release_lease(
    lease_key: str,
    owner_token: str = "",
    db_path: str | None = None,
) -> bool:
    """Release ``lease:{lease_key}`` in ``meta_kv`` if ``owner_token`` is empty or matches owner."""
    clean_key = str(lease_key or "").strip()
    if not clean_key:
        return False
    meta_key = f"lease:{clean_key}"
    tok = str(owner_token or "").strip()

    with write_tx(db_path) as conn:
        row = conn.execute(
            "SELECT value FROM meta_kv WHERE key = ?",
            (meta_key,),
        ).fetchone()
        if row is None:
            return False
        if tok:
            stored = json_loads_safe(row[0], {})
            stored_owner = str(stored.get("owner") or "")
            if stored_owner != tok:
                return False
        conn.execute("DELETE FROM meta_kv WHERE key = ?", (meta_key,))
        return True


def renew_lease(
    lease_key: str,
    owner_token: str,
    ttl_secs: int = 120,
    db_path: str | None = None,
    now_ts: int | None = None,
) -> bool:
    """Extend ``expires_at`` of ``lease:{lease_key}`` if currently owned by ``owner_token``."""
    clean_key = str(lease_key or "").strip()
    tok = str(owner_token or "").strip()
    if not clean_key or not tok:
        return False
    meta_key = f"lease:{clean_key}"
    cur_epoch = int(time.time())
    now = cur_epoch if now_ts is None else int(parse_ts(now_ts, cur_epoch))
    ttl = max(5, int(ttl_secs))

    with write_tx(db_path) as conn:
        row = conn.execute(
            "SELECT value FROM meta_kv WHERE key = ?",
            (meta_key,),
        ).fetchone()
        if row is None or not row[0]:
            return False
        stored = json_loads_safe(row[0], {})
        stored_owner = str(stored.get("owner") or "")
        if stored_owner != tok:
            return False
        stored["expires_at"] = now + ttl
        stored["pid"] = os.getpid()
        set_meta(conn, meta_key, json_dumps_compact(stored), updated_at=now)
        return True


def upsert_profile_touch(
    conn: sqlite3.Connection,
    profile_key: str,
    scope: str,
    entity_id: str,
    nickname: str = "",
    group_id: str = "",
    ts: int | None = None,
    inc_msg: int = 1,
) -> None:
    """Atomically create or update a profile's nicknames, seen groups, and activity stats.

    - `nicknames_json`: capped at 8 most recent (newest at the end).
    - `seen_groups_json`: capped at 30 most recent (newest at the end).
    - NEVER bumps `profile_updated_at` (stays 0 on INSERT, untouched on UPDATE) so
      L0 recall can distinguish semantic profile updates from message activity touches.
    """
    pkey = str(profile_key or "").strip()
    if not pkey:
        return
    now = now_ts() if ts is None else (parse_ts(ts, now_ts()) or now_ts())
    nick = str(nickname or "").strip()
    gid = sanitize_uid(group_id)
    inc = int(inc_msg or 0)

    row = conn.execute(
        "SELECT nicknames_json, seen_groups_json, msg_count, first_seen_ts, last_seen_ts "
        "FROM profiles WHERE profile_key = ?",
        (pkey,),
    ).fetchone()

    if row is None:
        nicknames = [nick] if nick else []
        seen_groups = [gid] if gid else []
        conn.execute(
            "INSERT INTO profiles("
            "profile_key, scope, entity_id, nicknames_json, seen_groups_json, "
            "relationship, summary, msg_count, first_seen_ts, last_seen_ts, updated_at, profile_updated_at"
            ") VALUES (?, ?, ?, ?, ?, '', '', ?, ?, ?, ?, 0)",
            (
                pkey,
                str(scope or ""),
                str(entity_id or ""),
                json_dumps_compact(nicknames),
                json_dumps_compact(seen_groups),
                max(0, inc),
                now,
                now,
                now,
            ),
        )
        return

    nicknames = [
        str(x).strip()
        for x in json_loads_safe(row[0], [])
        if str(x).strip()
    ]
    if nick:
        if nick in nicknames:
            nicknames.remove(nick)
        nicknames.append(nick)
    nicknames = nicknames[-8:]

    seen_groups = [
        sanitize_uid(x)
        for x in json_loads_safe(row[1], [])
        if sanitize_uid(x)
    ]
    if gid:
        if gid in seen_groups:
            seen_groups.remove(gid)
        seen_groups.append(gid)
    seen_groups = seen_groups[-30:]

    msg_count = max(0, int(row[2] or 0) + inc)
    existing_first = int(row[3] or 0)
    first_seen = now if existing_first <= 0 else min(existing_first, now)
    last_seen = max(int(row[4] or 0), now)

    conn.execute(
        "UPDATE profiles SET "
        "nicknames_json = ?, seen_groups_json = ?, msg_count = ?, "
        "first_seen_ts = ?, last_seen_ts = ?, updated_at = ? "
        "WHERE profile_key = ?",
        (
            json_dumps_compact(nicknames),
            json_dumps_compact(seen_groups),
            msg_count,
            first_seen,
            last_seen,
            now,
            pkey,
        ),
    )


__all__ = [
    "PROJECT_DIR",
    "SCHEMA_VERSION",
    "TZ_CN",
    "acquire_lease",
    "allowed_scopes_for_chat",
    "chat_scope_for",
    "checkpoint_wal",
    "close_connections",
    "default_db_path",
    "default_extractor_cwd",
    "delete_memory_fts",
    "get_connection",
    "get_env_bool",
    "get_env_float",
    "get_env_int",
    "get_env_str",
    "get_meta",
    "init_db",
    "is_group_memory_enabled",
    "is_memory_enabled",
    "is_write_scope_allowed",
    "json_dumps_compact",
    "json_loads_safe",
    "log_audit",
    "make_chat_key",
    "new_memory_id",
    "new_window_id",
    "now_str",
    "now_ts",
    "parse_chat_key",
    "parse_ts",
    "read_conn",
    "rebuild_fts_index",
    "release_lease",
    "renew_lease",
    "sanitize_uid",
    "set_meta",
    "sync_memory_fts",
    "today_str",
    "ts_to_date",
    "upsert_profile_touch",
    "write_tx",
]
