#!/usr/bin/env python3
"""Group member join request store, auto-approval policy engine, NapCat sync & SSE event log.

Provides the shared persistence, policy evaluation, NapCat execution/sync, and
delta event stream used by:
- ``onebot-ws-guard.py`` (real-time OneBot v11 ``request.group`` interception,
  auto-approval/rejection, admin DM alerts, and external ``group_increase`` sync)
- ``webui/`` (request queue console, instant SSE monitor, policy editor, batch actions)
- ``qq_actions.py`` / bot chat commands (optional chat-driven request handling)

Storage:
- SQLite WAL database: ``data/group_requests.db`` (``group_requests`` + ``request_events``)
- Hot-reloaded JSON policy: ``data/group_requests_policy.json`` (cross-process locked
  via ``file_lock.py`` and atomically replaced, cached for 0.8s with mtime check)

Python 3.9 standard library only, zero external dependencies.
"""

from __future__ import annotations

import copy
import datetime
import json
import math
import os
import re
import sqlite3
import sys
import tempfile
import threading
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

import bot_env  # noqa: E402,F401  (loads bot.env before env defaults are read)
import onebot_common  # noqa: E402
from file_lock import atomic_replace, file_lock  # noqa: E402

# ---------------------------------------------------------------------------
# Paths, Constants & Globals
# ---------------------------------------------------------------------------

DEFAULT_DB_PATH = os.path.join(_SCRIPT_DIR, "data", "group_requests.db")
DEFAULT_POLICY_FILE = os.path.join(_SCRIPT_DIR, "data", "group_requests_policy.json")

VALID_MODES = ("manual", "auto_all", "keyword", "auto_reject")
VALID_INVITE_MODES = ("manual", "auto_all", "auto_reject")
VALID_STATUSES = ("pending", "approved", "rejected", "failed")
VALID_SUB_TYPES = ("add", "invite")

_POLICY_RULE_KEYS = (
    "mode",
    "approve_keywords",
    "reject_keywords",
    "reject_unmatched",
    "default_reject_reason",
    "min_qq_level",
    "handle_invite",
    "auto_delay_secs",
    "notify_admin_dm",
    "welcome_message",
)

_GROUP_ID_RE = re.compile(r"^\d{5,15}$")
_KEYWORD_SPLIT_RE = re.compile(r"[\r\n,，]+")

_MAX_EVENTS_KEEP = 2000
_EVENTS_PRUNE_INTERVAL = 200

_SCHEMA_INIT_LOCK = threading.Lock()
_INITIALIZED_DBS: set[str] = set()

_POLICY_CACHE_LOCK = threading.Lock()
_cached_policy: Optional[Dict[str, Any]] = None
_cached_mtime: Optional[Tuple[int, int]] = None
_cached_path: Optional[str] = None
_last_stat_check: float = -10.0

_DECISION_LOCKS_GUARD = threading.Lock()
_DECISION_LOCKS: Dict[str, threading.Lock] = {}

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS group_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    flag TEXT NOT NULL UNIQUE,
    sub_type TEXT NOT NULL DEFAULT 'add',
    group_id TEXT NOT NULL,
    group_name TEXT NOT NULL DEFAULT '',
    user_id TEXT NOT NULL,
    user_name TEXT NOT NULL DEFAULT '',
    invitor_id TEXT NOT NULL DEFAULT '',
    invitor_name TEXT NOT NULL DEFAULT '',
    comment TEXT NOT NULL DEFAULT '',
    qq_level INTEGER DEFAULT NULL,
    is_ignored INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    decision_source TEXT NOT NULL DEFAULT '',
    operator_id TEXT NOT NULL DEFAULT '',
    reject_reason TEXT NOT NULL DEFAULT '',
    error_msg TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'ws',
    raw_json TEXT NOT NULL DEFAULT '{}',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    handled_at INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_gr_status_created
    ON group_requests(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_gr_group_status_created
    ON group_requests(group_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_gr_group_user_status
    ON group_requests(group_id, user_id, status);

CREATE TABLE IF NOT EXISTS request_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    request_id INTEGER DEFAULT NULL,
    flag TEXT NOT NULL DEFAULT '',
    group_id TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at INTEGER NOT NULL
);
"""


def get_db_path(db_path: Optional[str] = None) -> str:
    """Resolve SQLite DB path: explicit arg > GROUP_REQUESTS_DB env > DEFAULT_DB_PATH."""
    raw = str(db_path or "").strip()
    if raw:
        return raw
    env_p = os.environ.get("GROUP_REQUESTS_DB", "").strip()
    return env_p or DEFAULT_DB_PATH


def get_policy_path(path: Optional[str] = None) -> str:
    """Resolve policy JSON path: explicit arg > GROUP_REQUESTS_POLICY_FILE env > DEFAULT_POLICY_FILE."""
    raw = str(path or "").strip()
    if raw:
        return raw
    env_p = os.environ.get("GROUP_REQUESTS_POLICY_FILE", "").strip()
    return env_p or DEFAULT_POLICY_FILE


# ---------------------------------------------------------------------------
# Generic Helpers
# ---------------------------------------------------------------------------

def _now_ts() -> int:
    return int(time.time())


def _now_iso() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _ts_to_iso(ts: Any) -> str:
    try:
        n = int(ts or 0)
    except (TypeError, ValueError):
        return ""
    if n <= 0:
        return ""
    try:
        return datetime.datetime.fromtimestamp(n).astimezone().isoformat(timespec="seconds")
    except Exception:
        return ""


def _to_bool(val: Any, default: bool = False) -> bool:
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return bool(val)
    if isinstance(val, str):
        low = val.strip().lower()
        if low in ("1", "true", "yes", "on"):
            return True
        if low in ("0", "false", "no", "off", ""):
            return False
    return default


def _parse_optional_int(val: Any) -> Optional[int]:
    """Parse a non-negative integer; return None for None/bool/empty/invalid."""
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, int):
        return val if val >= 0 else None
    if isinstance(val, float):
        return int(val) if math.isfinite(val) and val >= 0 else None
    s = str(val).strip()
    if not s:
        return None
    try:
        n = int(float(s))
        return n if n >= 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def _parse_unix_ts(val: Any, fallback: int) -> int:
    if val is None or isinstance(val, bool):
        return fallback
    try:
        v = float(val)
    except (TypeError, ValueError):
        return fallback
    if not math.isfinite(v) or v <= 0:
        return fallback
    # Normalize millisecond timestamps into seconds
    if v > 1e12:
        v /= 1000.0
    n = int(v)
    return n if n > 0 else fallback


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _get_decision_lock(flag: str) -> threading.Lock:
    key = str(flag or "")
    with _DECISION_LOCKS_GUARD:
        if len(_DECISION_LOCKS) > 512:
            # Prune unlocked entries to keep memory bounded
            stale = [k for k, lk in _DECISION_LOCKS.items() if not lk.locked()]
            for k in stale[:256]:
                _DECISION_LOCKS.pop(k, None)
        lk = _DECISION_LOCKS.get(key)
        if lk is None:
            lk = threading.Lock()
            _DECISION_LOCKS[key] = lk
        return lk


# ---------------------------------------------------------------------------
# SQLite WAL Connection & Transaction Helpers
# ---------------------------------------------------------------------------

def ensure_db(db_path: Optional[str] = None) -> str:
    """Ensure SQLite database file and WAL schema exist (thread-safe)."""
    path = get_db_path(db_path)
    norm = os.path.abspath(path)
    if norm in _INITIALIZED_DBS and os.path.isfile(norm):
        try:
            if os.path.getsize(norm) > 0:
                return path
        except OSError:
            pass
    with _SCHEMA_INIT_LOCK:
        if norm in _INITIALIZED_DBS and os.path.isfile(norm):
            try:
                if os.path.getsize(norm) > 0:
                    return path
            except OSError:
                pass
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


def _open_conn(db_path: Optional[str] = None) -> sqlite3.Connection:
    path = ensure_db(db_path)
    conn = sqlite3.connect(path, timeout=5.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA temp_store=MEMORY")
    return conn


def _run_write_tx(
    db_path: Optional[str],
    fn: Callable[[sqlite3.Connection], Any],
) -> Any:
    """Execute ``fn(conn)`` inside ``BEGIN IMMEDIATE`` with 3x retry on OperationalError."""
    last_exc: Optional[Exception] = None
    for attempt in range(3):
        conn: Optional[sqlite3.Connection] = None
        try:
            conn = _open_conn(db_path)
            conn.execute("BEGIN IMMEDIATE")
            result = fn(conn)
            conn.execute("COMMIT")
            return result
        except sqlite3.OperationalError as exc:
            last_exc = exc
            if conn is not None:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
            if attempt < 2:
                time.sleep(0.05 * (attempt + 1))
            else:
                raise
        except Exception:
            if conn is not None:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
            raise
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
    if last_exc is not None:
        raise last_exc
    return None


def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    """Serialize a ``group_requests`` SQLite row into the canonical public dict."""
    created_at = int(row["created_at"] or 0)
    updated_at = int(row["updated_at"] or 0)
    handled_at = int(row["handled_at"] or 0)
    raw_level = row["qq_level"]
    qq_level = int(raw_level) if raw_level is not None else None
    return {
        "id": int(row["id"]),
        "flag": str(row["flag"] or ""),
        "sub_type": str(row["sub_type"] or "add"),
        "group_id": str(row["group_id"] or ""),
        "group_name": str(row["group_name"] or ""),
        "user_id": str(row["user_id"] or ""),
        "user_name": str(row["user_name"] or ""),
        "invitor_id": str(row["invitor_id"] or ""),
        "invitor_name": str(row["invitor_name"] or ""),
        "comment": str(row["comment"] or ""),
        "qq_level": qq_level,
        "is_ignored": bool(row["is_ignored"]),
        "status": str(row["status"] or "pending"),
        "decision_source": str(row["decision_source"] or ""),
        "operator_id": str(row["operator_id"] or ""),
        "reject_reason": str(row["reject_reason"] or ""),
        "error_msg": str(row["error_msg"] or ""),
        "source": str(row["source"] or "ws"),
        "created_at": created_at,
        "created_iso": _ts_to_iso(created_at),
        "updated_at": updated_at,
        "updated_iso": _ts_to_iso(updated_at),
        "handled_at": handled_at,
        "handled_iso": _ts_to_iso(handled_at),
    }


def _find_request_row(conn: sqlite3.Connection, flag_or_id: Any) -> Optional[sqlite3.Row]:
    """Locate a request row by integer id or string flag (safe for numeric flags)."""
    if flag_or_id is None or isinstance(flag_or_id, bool):
        return None
    if isinstance(flag_or_id, int):
        row = conn.execute(
            "SELECT * FROM group_requests WHERE id = ? LIMIT 1",
            (flag_or_id,),
        ).fetchone()
        if row is not None:
            return row
        return conn.execute(
            "SELECT * FROM group_requests WHERE flag = ? LIMIT 1",
            (str(flag_or_id),),
        ).fetchone()

    s = str(flag_or_id).strip()
    if not s:
        return None
    row = conn.execute(
        "SELECT * FROM group_requests WHERE flag = ? LIMIT 1",
        (s,),
    ).fetchone()
    if row is not None:
        return row
    if s.isdigit():
        try:
            row_id = int(s)
        except ValueError:
            return None
        return conn.execute(
            "SELECT * FROM group_requests WHERE id = ? LIMIT 1",
            (row_id,),
        ).fetchone()
    return None


def _append_event(
    conn: sqlite3.Connection,
    event_type: str,
    request_id: Optional[int] = None,
    flag: str = "",
    group_id: str = "",
    payload: Optional[Dict[str, Any]] = None,
    created_at: Optional[int] = None,
) -> int:
    """Append one SSE delta event inside an active transaction and periodically prune."""
    ts = int(created_at) if created_at and int(created_at) > 0 else _now_ts()
    payload_str = json.dumps(payload if isinstance(payload, dict) else {}, ensure_ascii=False)
    cur = conn.execute(
        """
        INSERT INTO request_events (
            event_type, request_id, flag, group_id, payload_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            str(event_type or "updated"),
            int(request_id) if request_id is not None else None,
            str(flag or ""),
            str(group_id or ""),
            payload_str,
            ts,
        ),
    )
    seq = int(cur.lastrowid or 0)
    if seq > _MAX_EVENTS_KEEP and seq % _EVENTS_PRUNE_INTERVAL == 0:
        conn.execute(
            """
            DELETE FROM request_events
            WHERE seq NOT IN (
                SELECT seq FROM request_events ORDER BY seq DESC LIMIT ?
            )
            """,
            (_MAX_EVENTS_KEEP,),
        )
    return seq


def _emit_event_standalone(
    event_type: str,
    request_id: Optional[int] = None,
    flag: str = "",
    group_id: str = "",
    payload: Optional[Dict[str, Any]] = None,
    db_path: Optional[str] = None,
) -> int:
    def _tx(conn: sqlite3.Connection) -> int:
        return _append_event(
            conn,
            event_type=event_type,
            request_id=request_id,
            flag=flag,
            group_id=group_id,
            payload=payload,
        )

    return int(_run_write_tx(db_path, _tx) or 0)


# ---------------------------------------------------------------------------
# Policy Engine (Hot-reloaded JSON + File Lock + Atomic Replace)
# ---------------------------------------------------------------------------

def default_policy() -> Dict[str, Any]:
    """Return a clean default group join request auto-approval policy."""
    return {
        "version": 1,
        "updated_at": "",
        "enabled": True,
        "global": {
            "mode": "manual",
            "approve_keywords": [],
            "reject_keywords": [],
            "reject_unmatched": False,
            "default_reject_reason": "未通过入群验证",
            "min_qq_level": 0,
            "handle_invite": "manual",
            "auto_delay_secs": 1.0,
            "notify_admin_dm": False,
            "welcome_message": "",
        },
        "groups": {},
    }


def _sanitize_keywords(raw: Any) -> List[str]:
    if isinstance(raw, str):
        items = _KEYWORD_SPLIT_RE.split(raw)
    elif isinstance(raw, (list, tuple)):
        items = [str(x) for x in raw if x is not None]
    else:
        return []
    cleaned: List[str] = []
    seen_lower: set[str] = set()
    for item in items:
        s = str(item).strip()[:80]
        if not s:
            continue
        low = s.lower()
        if low in seen_lower:
            continue
        seen_lower.add(low)
        cleaned.append(s)
        if len(cleaned) >= 50:
            break
    return cleaned


def _sanitize_rule_fields(
    raw: Any,
    fallback: Dict[str, Any],
    partial: bool = False,
) -> Dict[str, Any]:
    """Validate and clamp rule fields. When ``partial=True``, only present keys are returned."""
    src = raw if isinstance(raw, dict) else {}
    out: Dict[str, Any] = {} if partial else copy.deepcopy(fallback)

    def _present(key: str) -> bool:
        return key in src and src[key] is not None

    if _present("mode"):
        m = str(src["mode"]).strip().lower()
        if m in VALID_MODES:
            out["mode"] = m
        elif not partial:
            out["mode"] = fallback["mode"]

    if _present("approve_keywords"):
        out["approve_keywords"] = _sanitize_keywords(src["approve_keywords"])

    if _present("reject_keywords"):
        out["reject_keywords"] = _sanitize_keywords(src["reject_keywords"])

    if _present("reject_unmatched"):
        out["reject_unmatched"] = _to_bool(
            src["reject_unmatched"], bool(fallback.get("reject_unmatched", False))
        )

    if _present("default_reject_reason"):
        out["default_reject_reason"] = str(src["default_reject_reason"]).strip()[:120]

    if _present("min_qq_level"):
        val = src["min_qq_level"]
        if not isinstance(val, bool):
            try:
                lvl = int(float(val))
                out["min_qq_level"] = max(0, min(200, lvl))
            except (TypeError, ValueError, OverflowError):
                pass

    if _present("handle_invite"):
        hm = str(src["handle_invite"]).strip().lower()
        if hm in VALID_INVITE_MODES:
            out["handle_invite"] = hm
        elif not partial:
            out["handle_invite"] = fallback["handle_invite"]

    if _present("auto_delay_secs"):
        val = src["auto_delay_secs"]
        if not isinstance(val, bool):
            try:
                sec = float(val)
                if math.isfinite(sec):
                    out["auto_delay_secs"] = round(max(0.0, min(30.0, sec)), 2)
            except (TypeError, ValueError, OverflowError):
                pass

    if _present("notify_admin_dm"):
        out["notify_admin_dm"] = _to_bool(
            src["notify_admin_dm"], bool(fallback.get("notify_admin_dm", False))
        )

    if _present("welcome_message"):
        out["welcome_message"] = str(src["welcome_message"]).strip()[:300]

    return out


def normalize_policy(raw: Any) -> Dict[str, Any]:
    """Validate and clamp arbitrary policy JSON into a clean canonical policy dict."""
    base = default_policy()
    if not isinstance(raw, dict):
        return base

    base["version"] = 1
    if isinstance(raw.get("updated_at"), str):
        base["updated_at"] = raw["updated_at"].strip()
    if "enabled" in raw and raw["enabled"] is not None:
        base["enabled"] = _to_bool(raw["enabled"], True)

    base["global"] = _sanitize_rule_fields(raw.get("global"), base["global"], partial=False)

    raw_groups = raw.get("groups") if isinstance(raw.get("groups"), dict) else {}
    clean_groups: Dict[str, Dict[str, Any]] = {}
    for raw_gid, gdata in raw_groups.items():
        gid = str(raw_gid or "").strip()
        if not _GROUP_ID_RE.match(gid) or not isinstance(gdata, dict):
            continue
        label = str(gdata.get("label") or "").strip()[:80]
        overrides = _sanitize_rule_fields(gdata, base["global"], partial=True)
        if "inherit" in gdata and gdata["inherit"] is not None:
            inherit = _to_bool(gdata["inherit"], True)
        else:
            inherit = not bool(overrides)
        entry: Dict[str, Any] = {
            "label": label,
            "inherit": inherit,
        }
        entry.update(overrides)
        clean_groups[gid] = entry

    base["groups"] = clean_groups
    return base


def _lock_path_for(path: str) -> str:
    return path + ".lock"


def load_policy(path: Optional[str] = None, force: bool = False) -> Dict[str, Any]:
    """Load policy JSON with 0.8s mtime hot-reload cache and shared file lock."""
    global _cached_policy, _cached_mtime, _cached_path, _last_stat_check
    target = get_policy_path(path)
    target_norm = os.path.abspath(target)
    now = time.monotonic()

    with _POLICY_CACHE_LOCK:
        if (
            not force
            and _cached_policy is not None
            and _cached_path == target_norm
            and (now - _last_stat_check) < 0.8
        ):
            return copy.deepcopy(_cached_policy)

        _last_stat_check = now
        try:
            st = os.stat(target)
            mtime: Optional[Tuple[int, int]] = (st.st_mtime_ns, st.st_size)
        except OSError:
            mtime = None

        if (
            not force
            and _cached_policy is not None
            and _cached_path == target_norm
            and _cached_mtime == mtime
        ):
            return copy.deepcopy(_cached_policy)

    if mtime is None:
        pol = default_policy()
    else:
        raw = None
        try:
            with file_lock(_lock_path_for(target), exclusive=False):
                with open(target, "r", encoding="utf-8-sig") as f:
                    raw = json.load(f)
        except Exception:
            raw = None
        pol = normalize_policy(raw)

    with _POLICY_CACHE_LOCK:
        _cached_policy = pol
        _cached_mtime = mtime
        _cached_path = target_norm
    return copy.deepcopy(pol)


def save_policy(
    policy: Dict[str, Any],
    path: Optional[str] = None,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Normalize, lock, atomically persist policy JSON, and emit ``rule_updated`` SSE event."""
    global _cached_policy, _cached_mtime, _cached_path, _last_stat_check
    target = get_policy_path(path)
    target_norm = os.path.abspath(target)
    clean = normalize_policy(policy)
    clean["updated_at"] = _now_iso()

    parent = os.path.dirname(target_norm)
    if parent:
        os.makedirs(parent, exist_ok=True)
    text = json.dumps(clean, ensure_ascii=False, indent=2) + "\n"

    with file_lock(_lock_path_for(target), exclusive=True):
        fd, tmp = tempfile.mkstemp(prefix=".group_req_policy_", suffix=".tmp", dir=parent)
        fd_owned = True
        try:
            f = os.fdopen(fd, "w", encoding="utf-8", newline="\n")
            fd_owned = False
            with f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            atomic_replace(tmp, target)
        except Exception:
            if fd_owned:
                try:
                    os.close(fd)
                except OSError:
                    pass
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    try:
        st = os.stat(target)
        mtime: Optional[Tuple[int, int]] = (st.st_mtime_ns, st.st_size)
    except OSError:
        mtime = None

    with _POLICY_CACHE_LOCK:
        _cached_policy = copy.deepcopy(clean)
        _cached_mtime = mtime
        _cached_path = target_norm
        _last_stat_check = time.monotonic()

    # Notify live SSE monitors that rules changed (fail-soft)
    try:
        _emit_event_standalone(
            "rule_updated",
            payload={
                "updated_at": clean["updated_at"],
                "enabled": clean["enabled"],
                "global_mode": clean["global"]["mode"],
                "groups_count": len(clean["groups"]),
            },
            db_path=db_path,
        )
    except Exception:
        pass

    return copy.deepcopy(clean)


def resolve_group_policy(
    group_id: Any,
    policy: Optional[Dict[str, Any]] = None,
    policy_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Return effective merged rule dict for ``group_id`` plus override metadata."""
    pol = normalize_policy(policy) if isinstance(policy, dict) else load_policy(path=policy_path)
    eff = copy.deepcopy(pol["global"])
    gid = str(group_id or "").strip()
    overridden = False
    group_label = ""

    g_entry = (pol.get("groups") or {}).get(gid)
    if isinstance(g_entry, dict):
        group_label = str(g_entry.get("label") or "")
        if not g_entry.get("inherit", True):
            overridden = True
            for key in _POLICY_RULE_KEYS:
                if key in g_entry and g_entry[key] is not None:
                    eff[key] = copy.deepcopy(g_entry[key])

    eff["overridden"] = overridden
    eff["group_label"] = group_label
    eff["master_enabled"] = bool(pol.get("enabled", True))
    return eff


def evaluate_request_policy(
    req: Dict[str, Any],
    policy: Optional[Dict[str, Any]] = None,
    policy_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Evaluate a join/invite request against the active auto-approval policy."""
    pol = normalize_policy(policy) if isinstance(policy, dict) else load_policy(path=policy_path)
    if not pol.get("enabled", True):
        return {
            "action": "manual",
            "source": "disabled",
            "reason": "自动审批总开关已关闭",
            "matched_keyword": "",
            "delay_secs": 0.0,
        }

    req_dict = req if isinstance(req, dict) else {}
    gid = str(req_dict.get("group_id") or "").strip()
    sub_type = str(req_dict.get("sub_type") or "add").strip().lower()
    if sub_type not in VALID_SUB_TYPES:
        sub_type = "invite" if "invite" in sub_type else "add"

    eff = resolve_group_policy(gid, policy=pol)
    delay_secs = float(eff.get("auto_delay_secs", 1.0))
    default_reason = str(eff.get("default_reject_reason") or "").strip()

    # 1. Group invite handling
    if sub_type == "invite":
        handle_invite = str(eff.get("handle_invite") or "manual")
        if handle_invite == "auto_all":
            return {
                "action": "approve",
                "source": "auto_all",
                "reason": "已开启自动同意群邀请",
                "matched_keyword": "",
                "delay_secs": delay_secs,
            }
        if handle_invite == "auto_reject":
            return {
                "action": "reject",
                "source": "auto_reject",
                "reason": default_reason or "已开启自动拒绝群邀请",
                "matched_keyword": "",
                "delay_secs": delay_secs,
            }
        return {
            "action": "manual",
            "source": "manual",
            "reason": "群邀请当前为人工审批",
            "matched_keyword": "",
            "delay_secs": 0.0,
        }

    # 2. Group join ('add') handling
    comment = str(req_dict.get("comment") or "")
    comment_lower = comment.lower()

    # Step 1: Reject keywords take highest priority
    for kw in eff.get("reject_keywords") or []:
        if kw and kw.lower() in comment_lower:
            return {
                "action": "reject",
                "source": "auto_reject_keyword",
                "reason": default_reason or f"命中拒绝关键词「{kw}」",
                "matched_keyword": kw,
                "delay_secs": delay_secs,
            }

    mode = str(eff.get("mode") or "manual")

    # Step 2: Manual mode
    if mode == "manual":
        return {
            "action": "manual",
            "source": "manual",
            "reason": "当前模式为人工审批",
            "matched_keyword": "",
            "delay_secs": 0.0,
        }

    # Step 3: Auto-reject mode
    if mode == "auto_reject":
        return {
            "action": "reject",
            "source": "auto_reject",
            "reason": default_reason or "已开启自动拒绝入群申请",
            "matched_keyword": "",
            "delay_secs": delay_secs,
        }

    # Step 4: Minimum QQ level gate
    min_level = int(eff.get("min_qq_level") or 0)
    if min_level > 0:
        qq_level = _parse_optional_int(req_dict.get("qq_level"))
        if qq_level is None:
            return {
                "action": "manual",
                "source": "manual",
                "reason": "未获取到QQ等级，转人工审批",
                "matched_keyword": "",
                "delay_secs": 0.0,
            }
        if qq_level < min_level:
            if eff.get("reject_unmatched"):
                return {
                    "action": "reject",
                    "source": "auto_level_reject",
                    "reason": default_reason or f"QQ等级低于自动通过门槛（需 Lv.{min_level}）",
                    "matched_keyword": "",
                    "delay_secs": delay_secs,
                }
            return {
                "action": "manual",
                "source": "manual",
                "reason": f"QQ等级低于自动通过门槛（当前 Lv.{qq_level} < Lv.{min_level}）",
                "matched_keyword": "",
                "delay_secs": 0.0,
            }

    # Step 5: Auto-approve all
    if mode == "auto_all":
        return {
            "action": "approve",
            "source": "auto_all",
            "reason": "已开启自动通过全部入群申请",
            "matched_keyword": "",
            "delay_secs": delay_secs,
        }

    # Step 6: Keyword mode
    if mode == "keyword":
        approve_kws = eff.get("approve_keywords") or []
        if not approve_kws:
            return {
                "action": "manual",
                "source": "manual",
                "reason": "未配置通过关键词，转人工审批",
                "matched_keyword": "",
                "delay_secs": 0.0,
            }
        for kw in approve_kws:
            if kw and kw.lower() in comment_lower:
                return {
                    "action": "approve",
                    "source": "auto_keyword",
                    "reason": f"匹配通过关键词「{kw}」",
                    "matched_keyword": kw,
                    "delay_secs": delay_secs,
                }
        if eff.get("reject_unmatched"):
            return {
                "action": "reject",
                "source": "auto_reject_unmatched",
                "reason": default_reason or "未通过入群验证",
                "matched_keyword": "",
                "delay_secs": delay_secs,
            }
        return {
            "action": "manual",
            "source": "manual",
            "reason": "验证消息未匹配关键词，转人工审批",
            "matched_keyword": "",
            "delay_secs": 0.0,
        }

    return {
        "action": "manual",
        "source": "manual",
        "reason": "当前模式为人工审批",
        "matched_keyword": "",
        "delay_secs": 0.0,
    }


# ---------------------------------------------------------------------------
# CRUD & SSE Event Log
# ---------------------------------------------------------------------------

def _record_request_internal(
    raw_event: Dict[str, Any],
    db_path: Optional[str] = None,
) -> Tuple[Dict[str, Any], bool, bool]:
    """Insert or enrich a request record. Returns ``(req_dict, is_new, was_updated)``."""
    if not isinstance(raw_event, dict):
        raise ValueError("raw_event must be a dict")

    flag = str(raw_event.get("flag") or raw_event.get("request_id") or "").strip()
    if not flag:
        raise ValueError("入群申请记录缺少 flag")

    raw_sub = str(raw_event.get("sub_type") or "add").strip().lower()
    if raw_sub in VALID_SUB_TYPES:
        sub_type = raw_sub
    else:
        sub_type = "invite" if "invite" in raw_sub else "add"

    group_id = str(raw_event.get("group_id") or "").strip()
    group_name = str(raw_event.get("group_name") or "").strip()[:120]

    invitor_id = str(
        raw_event.get("invitor_id") or raw_event.get("invitor_uin") or ""
    ).strip()
    invitor_name = str(
        raw_event.get("invitor_name") or raw_event.get("invitor_nick") or ""
    ).strip()[:120]

    user_id = str(
        raw_event.get("user_id")
        or raw_event.get("requester_uin")
        or invitor_id
        or ""
    ).strip()
    user_name = str(
        raw_event.get("user_name")
        or raw_event.get("requester_nick")
        or raw_event.get("nickname")
        or (invitor_name if sub_type == "invite" else "")
        or ""
    ).strip()[:120]

    if sub_type == "invite":
        if not invitor_id and user_id:
            invitor_id = user_id
        if not invitor_name and user_name:
            invitor_name = user_name

    raw_comment = raw_event.get("comment")
    if raw_comment is None:
        raw_comment = raw_event.get("message")
    comment = str(raw_comment or "").strip()[:1000]

    qq_level = _parse_optional_int(raw_event.get("qq_level"))
    is_ignored = 1 if _to_bool(raw_event.get("is_ignored"), False) else 0
    source = str(raw_event.get("source") or "ws").strip()[:32] or "ws"

    raw_status = str(raw_event.get("status") or "pending").strip().lower()
    status = raw_status if raw_status in VALID_STATUSES else "pending"
    decision_source = str(raw_event.get("decision_source") or "").strip()[:40]
    operator_id = str(raw_event.get("operator_id") or "").strip()[:40]
    reject_reason = str(raw_event.get("reject_reason") or "").strip()[:200]
    error_msg = str(raw_event.get("error_msg") or "").strip()[:300]

    now_ts = _now_ts()
    created_at = _parse_unix_ts(
        raw_event.get("created_at") if raw_event.get("created_at") is not None else raw_event.get("time"),
        fallback=now_ts,
    )
    handled_at = _parse_unix_ts(raw_event.get("handled_at"), fallback=0)
    if status in ("approved", "rejected") and handled_at <= 0:
        handled_at = now_ts

    try:
        raw_json = json.dumps(raw_event, ensure_ascii=False, default=str)[:8000]
    except Exception:
        raw_json = "{}"

    def _tx(conn: sqlite3.Connection) -> Tuple[Dict[str, Any], bool, bool]:
        existing = conn.execute(
            "SELECT * FROM group_requests WHERE flag = ? LIMIT 1",
            (flag,),
        ).fetchone()

        if existing is None:
            cur = conn.execute(
                """
                INSERT INTO group_requests (
                    flag, sub_type, group_id, group_name, user_id, user_name,
                    invitor_id, invitor_name, comment, qq_level, is_ignored,
                    status, decision_source, operator_id, reject_reason, error_msg,
                    source, raw_json, created_at, updated_at, handled_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    flag,
                    sub_type,
                    group_id,
                    group_name,
                    user_id,
                    user_name,
                    invitor_id,
                    invitor_name,
                    comment,
                    qq_level,
                    is_ignored,
                    status,
                    decision_source,
                    operator_id,
                    reject_reason,
                    error_msg,
                    source,
                    raw_json,
                    created_at,
                    now_ts,
                    handled_at,
                ),
            )
            row_id = int(cur.lastrowid or 0)
            row = conn.execute(
                "SELECT * FROM group_requests WHERE id = ? LIMIT 1",
                (row_id,),
            ).fetchone()
            req_dict = _row_to_dict(row)
            _append_event(
                conn,
                event_type="new",
                request_id=req_dict["id"],
                flag=req_dict["flag"],
                group_id=req_dict["group_id"],
                payload={"request": req_dict},
                created_at=now_ts,
            )
            return req_dict, True, False

        # Existing row: enrich empty fields without overwriting handled status
        updates: Dict[str, Any] = {}
        if not existing["group_name"] and group_name:
            updates["group_name"] = group_name
        if not existing["user_name"] and user_name:
            updates["user_name"] = user_name
        if not existing["invitor_id"] and invitor_id:
            updates["invitor_id"] = invitor_id
        if not existing["invitor_name"] and invitor_name:
            updates["invitor_name"] = invitor_name
        if not existing["comment"] and comment:
            updates["comment"] = comment
        if existing["qq_level"] is None and qq_level is not None:
            updates["qq_level"] = qq_level
        if not existing["is_ignored"] and is_ignored:
            updates["is_ignored"] = 1

        status_transitioned = False
        if existing["status"] == "pending" and status in ("approved", "rejected"):
            updates["status"] = status
            if decision_source:
                updates["decision_source"] = decision_source
            if operator_id:
                updates["operator_id"] = operator_id
            if reject_reason:
                updates["reject_reason"] = reject_reason
            updates["error_msg"] = ""
            updates["handled_at"] = handled_at if handled_at > 0 else now_ts
            status_transitioned = True

        if not updates:
            return _row_to_dict(existing), False, False

        updates["updated_at"] = now_ts
        set_clause = ", ".join(f"{col} = ?" for col in updates.keys())
        params = list(updates.values()) + [int(existing["id"])]
        conn.execute(f"UPDATE group_requests SET {set_clause} WHERE id = ?", params)
        updated_row = conn.execute(
            "SELECT * FROM group_requests WHERE id = ? LIMIT 1",
            (int(existing["id"]),),
        ).fetchone()
        req_dict = _row_to_dict(updated_row)
        _append_event(
            conn,
            event_type="handled" if status_transitioned else "updated",
            request_id=req_dict["id"],
            flag=req_dict["flag"],
            group_id=req_dict["group_id"],
            payload={"request": req_dict},
            created_at=now_ts,
        )
        return req_dict, False, True

    return _run_write_tx(db_path, _tx)


def record_incoming_request(
    raw_event: Dict[str, Any],
    db_path: Optional[str] = None,
) -> Tuple[Dict[str, Any], bool]:
    """Record a new incoming join/invite request or enrich an existing one by flag."""
    req, is_new, _ = _record_request_internal(raw_event, db_path=db_path)
    return req, is_new


def get_request(
    flag_or_id: Any,
    db_path: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Fetch a single serialized request by integer id or string flag."""
    conn = _open_conn(db_path)
    try:
        row = _find_request_row(conn, flag_or_id)
        return _row_to_dict(row) if row is not None else None
    finally:
        conn.close()


def list_requests(
    status: str = "all",
    group_id: str = "",
    sub_type: str = "",
    query: str = "",
    limit: int = 100,
    offset: int = 0,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """List requests with filters, status counts, and latest SSE event sequence."""
    st_filter = str(status or "all").strip().lower()
    gid_filter = str(group_id or "").strip()
    sub_filter = str(sub_type or "").strip().lower()
    q_filter = str(query or "").strip()
    lim = max(1, min(500, int(limit or 100)))
    off = max(0, int(offset or 0))

    base_clauses: List[str] = []
    base_params: List[Any] = []

    if gid_filter:
        base_clauses.append("group_id = ?")
        base_params.append(gid_filter)
    if sub_filter in VALID_SUB_TYPES:
        base_clauses.append("sub_type = ?")
        base_params.append(sub_filter)
    if q_filter:
        like_pat = f"%{_escape_like(q_filter)}%"
        base_clauses.append(
            "("
            "group_id LIKE ? ESCAPE '\\' OR "
            "group_name LIKE ? ESCAPE '\\' OR "
            "user_id LIKE ? ESCAPE '\\' OR "
            "user_name LIKE ? ESCAPE '\\' OR "
            "invitor_id LIKE ? ESCAPE '\\' OR "
            "invitor_name LIKE ? ESCAPE '\\' OR "
            "comment LIKE ? ESCAPE '\\' OR "
            "flag LIKE ? ESCAPE '\\' OR "
            "reject_reason LIKE ? ESCAPE '\\'"
            ")"
        )
        base_params.extend([like_pat] * 9)

    base_where = (" WHERE " + " AND ".join(base_clauses)) if base_clauses else ""

    item_clauses = list(base_clauses)
    item_params = list(base_params)
    if st_filter and st_filter != "all" and st_filter in VALID_STATUSES:
        item_clauses.append("status = ?")
        item_params.append(st_filter)
    item_where = (" WHERE " + " AND ".join(item_clauses)) if item_clauses else ""

    conn = _open_conn(db_path)
    try:
        rows = conn.execute(
            f"""
            SELECT * FROM group_requests
            {item_where}
            ORDER BY CASE WHEN status = 'pending' THEN 0 ELSE 1 END,
                     created_at DESC,
                     id DESC
            LIMIT ? OFFSET ?
            """,
            item_params + [lim, off],
        ).fetchall()
        items = [_row_to_dict(r) for r in rows]

        counts = {
            "pending": 0,
            "approved": 0,
            "rejected": 0,
            "failed": 0,
            "total": 0,
        }
        cnt_rows = conn.execute(
            f"SELECT status, COUNT(*) AS cnt FROM group_requests{base_where} GROUP BY status",
            base_params,
        ).fetchall()
        for cr in cnt_rows:
            st = str(cr["status"] or "")
            c = int(cr["cnt"] or 0)
            if st in counts:
                counts[st] = c
            counts["total"] += c

        seq_row = conn.execute("SELECT COALESCE(MAX(seq), 0) AS s FROM request_events").fetchone()
        latest_seq = int(seq_row["s"] or 0) if seq_row is not None else 0

        return {
            "items": items,
            "counts": counts,
            "latest_seq": latest_seq,
        }
    finally:
        conn.close()


def get_summary(
    db_path: Optional[str] = None,
    policy_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Return dashboard summary counters, policy status, and latest pending request."""
    now_dt = datetime.datetime.now().astimezone()
    today_start = int(now_dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())

    pol = load_policy(path=policy_path)
    auto_groups_count = 0
    for gid, g_entry in (pol.get("groups") or {}).items():
        if not isinstance(g_entry, dict) or g_entry.get("inherit", True):
            continue
        eff = resolve_group_policy(gid, policy=pol)
        if (
            eff.get("mode") != "manual"
            or eff.get("handle_invite") != "manual"
            or bool(eff.get("reject_keywords"))
        ):
            auto_groups_count += 1

    conn = _open_conn(db_path)
    try:
        agg = conn.execute(
            """
            SELECT
                COUNT(*) AS total_count,
                COALESCE(SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END), 0) AS pending_count,
                COALESCE(SUM(CASE WHEN status = 'pending' AND sub_type = 'add' THEN 1 ELSE 0 END), 0) AS pending_add_count,
                COALESCE(SUM(CASE WHEN status = 'pending' AND sub_type = 'invite' THEN 1 ELSE 0 END), 0) AS pending_invite_count,
                COALESCE(SUM(CASE WHEN created_at >= ? THEN 1 ELSE 0 END), 0) AS today_total,
                COALESCE(SUM(CASE WHEN status = 'approved' AND (handled_at >= ? OR (handled_at = 0 AND updated_at >= ?)) THEN 1 ELSE 0 END), 0) AS today_approved,
                COALESCE(SUM(CASE WHEN status = 'rejected' AND (handled_at >= ? OR (handled_at = 0 AND updated_at >= ?)) THEN 1 ELSE 0 END), 0) AS today_rejected,
                COALESCE(SUM(CASE WHEN decision_source LIKE 'auto_%' AND status IN ('approved', 'rejected') AND (handled_at >= ? OR (handled_at = 0 AND updated_at >= ?)) THEN 1 ELSE 0 END), 0) AS today_auto
            FROM group_requests
            """,
            (
                today_start,
                today_start,
                today_start,
                today_start,
                today_start,
                today_start,
                today_start,
            ),
        ).fetchone()

        latest_row = conn.execute(
            """
            SELECT * FROM group_requests
            WHERE status = 'pending'
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """
        ).fetchone()

        seq_row = conn.execute("SELECT COALESCE(MAX(seq), 0) AS s FROM request_events").fetchone()
        latest_seq = int(seq_row["s"] or 0) if seq_row is not None else 0

        return {
            "pending_count": int(agg["pending_count"] or 0),
            "pending_add_count": int(agg["pending_add_count"] or 0),
            "pending_invite_count": int(agg["pending_invite_count"] or 0),
            "today_total": int(agg["today_total"] or 0),
            "today_approved": int(agg["today_approved"] or 0),
            "today_rejected": int(agg["today_rejected"] or 0),
            "today_auto": int(agg["today_auto"] or 0),
            "total_count": int(agg["total_count"] or 0),
            "policy_enabled": bool(pol.get("enabled", True)),
            "global_mode": str((pol.get("global") or {}).get("mode") or "manual"),
            "auto_groups_count": auto_groups_count,
            "latest_seq": latest_seq,
            "latest_pending": _row_to_dict(latest_row) if latest_row is not None else None,
        }
    finally:
        conn.close()


def get_latest_seq(db_path: Optional[str] = None) -> int:
    """Return the latest ``seq`` in ``request_events`` (0 if empty)."""
    conn = _open_conn(db_path)
    try:
        row = conn.execute("SELECT COALESCE(MAX(seq), 0) AS s FROM request_events").fetchone()
        return int(row["s"] or 0) if row is not None else 0
    finally:
        conn.close()


def get_events_since(
    last_seq: int,
    limit: int = 100,
    db_path: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], int]:
    """Return ``(events_after_last_seq, new_cursor_seq)`` for SSE delta streaming."""
    after = max(0, int(last_seq or 0))
    lim = max(1, min(500, int(limit or 100)))

    conn = _open_conn(db_path)
    try:
        rows = conn.execute(
            """
            SELECT seq, event_type, request_id, flag, group_id, payload_json, created_at
            FROM request_events
            WHERE seq > ?
            ORDER BY seq ASC
            LIMIT ?
            """,
            (after, lim),
        ).fetchall()

        events: List[Dict[str, Any]] = []
        for r in rows:
            try:
                payload = json.loads(r["payload_json"] or "{}")
                if not isinstance(payload, dict):
                    payload = {}
            except Exception:
                payload = {}
            created_at = int(r["created_at"] or 0)
            events.append(
                {
                    "seq": int(r["seq"]),
                    "event_type": str(r["event_type"] or ""),
                    "request_id": int(r["request_id"]) if r["request_id"] is not None else None,
                    "flag": str(r["flag"] or ""),
                    "group_id": str(r["group_id"] or ""),
                    "payload": payload,
                    "created_at": created_at,
                    "created_iso": _ts_to_iso(created_at),
                }
            )

        if events:
            return events, int(events[-1]["seq"])

        seq_row = conn.execute("SELECT COALESCE(MAX(seq), 0) AS s FROM request_events").fetchone()
        latest_seq = int(seq_row["s"] or 0) if seq_row is not None else 0
        return [], latest_seq
    finally:
        conn.close()


def mark_request_handled(
    flag_or_id: Any,
    status: str,
    decision_source: str = "",
    operator_id: str = "",
    reject_reason: str = "",
    error_msg: str = "",
    db_path: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Update request handling status and emit an SSE event."""
    clean_status = str(status or "").strip().lower()
    if clean_status not in VALID_STATUSES:
        raise ValueError(f"无效的入群申请状态: {status}")

    clean_source = str(decision_source or "").strip()[:40]
    clean_op = str(operator_id or "").strip()[:40]
    clean_reason = str(reject_reason or "").strip()[:200]
    clean_err = str(error_msg or "").strip()[:300]
    now_ts = _now_ts()

    def _tx(conn: sqlite3.Connection) -> Optional[Dict[str, Any]]:
        row = _find_request_row(conn, flag_or_id)
        if row is None:
            return None

        if clean_status in ("approved", "rejected"):
            handled_at = now_ts
            err_to_save = ""
        elif clean_status == "pending":
            handled_at = 0
            err_to_save = clean_err
        else:
            handled_at = int(row["handled_at"] or 0)
            err_to_save = clean_err

        conn.execute(
            """
            UPDATE group_requests
            SET status = ?,
                decision_source = ?,
                operator_id = ?,
                reject_reason = ?,
                error_msg = ?,
                updated_at = ?,
                handled_at = ?
            WHERE id = ?
            """,
            (
                clean_status,
                clean_source,
                clean_op,
                clean_reason,
                err_to_save,
                now_ts,
                handled_at,
                int(row["id"]),
            ),
        )
        updated = conn.execute(
            "SELECT * FROM group_requests WHERE id = ? LIMIT 1",
            (int(row["id"]),),
        ).fetchone()
        req_dict = _row_to_dict(updated)
        ev_type = "handled" if clean_status in ("approved", "rejected", "failed") else "updated"
        _append_event(
            conn,
            event_type=ev_type,
            request_id=req_dict["id"],
            flag=req_dict["flag"],
            group_id=req_dict["group_id"],
            payload={"request": req_dict},
            created_at=now_ts,
        )
        return req_dict

    return _run_write_tx(db_path, _tx)


def mark_external_group_increase(
    group_id: Any,
    user_id: Any,
    operator_id: str = "",
    db_path: Optional[str] = None,
) -> int:
    """Mark pending ``add`` requests for ``(group_id, user_id)`` as externally approved."""
    gid = str(group_id or "").strip()
    uid = str(user_id or "").strip()
    op = str(operator_id or "").strip()[:40]
    if not gid or not uid:
        return 0

    now_ts = _now_ts()

    def _tx(conn: sqlite3.Connection) -> int:
        rows = conn.execute(
            """
            SELECT id FROM group_requests
            WHERE group_id = ? AND user_id = ? AND sub_type = 'add' AND status = 'pending'
            ORDER BY id ASC
            """,
            (gid, uid),
        ).fetchall()
        if not rows:
            return 0

        count = 0
        for r in rows:
            rid = int(r["id"])
            conn.execute(
                """
                UPDATE group_requests
                SET status = 'approved',
                    decision_source = 'external',
                    operator_id = ?,
                    error_msg = '',
                    updated_at = ?,
                    handled_at = ?
                WHERE id = ?
                """,
                (op, now_ts, now_ts, rid),
            )
            updated = conn.execute(
                "SELECT * FROM group_requests WHERE id = ? LIMIT 1",
                (rid,),
            ).fetchone()
            if updated is not None:
                req_dict = _row_to_dict(updated)
                _append_event(
                    conn,
                    event_type="handled",
                    request_id=req_dict["id"],
                    flag=req_dict["flag"],
                    group_id=req_dict["group_id"],
                    payload={"request": req_dict},
                    created_at=now_ts,
                )
            count += 1
        return count

    return int(_run_write_tx(db_path, _tx) or 0)


def clear_handled_requests(
    before_days: float = 0,
    db_path: Optional[str] = None,
) -> int:
    """Delete handled (non-pending) rows, optionally older than ``before_days`` days."""
    try:
        days = max(0.0, float(before_days or 0))
    except (TypeError, ValueError):
        days = 0.0

    now_ts = _now_ts()

    def _tx(conn: sqlite3.Connection) -> int:
        if days > 0:
            cutoff = int(now_ts - days * 86400.0)
            cur = conn.execute(
                "DELETE FROM group_requests WHERE status != 'pending' AND updated_at < ?",
                (cutoff,),
            )
        else:
            cur = conn.execute("DELETE FROM group_requests WHERE status != 'pending'")
        deleted = int(cur.rowcount or 0)
        if deleted > 0:
            _append_event(
                conn,
                event_type="synced",
                payload={
                    "action": "clear_handled",
                    "deleted": deleted,
                    "before_days": days,
                },
                created_at=now_ts,
            )
        return deleted

    return int(_run_write_tx(db_path, _tx) or 0)


# ---------------------------------------------------------------------------
# NapCat HTTP Caller, Sync, Decision Executor & WebSocket Handler
# ---------------------------------------------------------------------------

def _default_napcat_post(
    endpoint: str,
    payload: Optional[Dict[str, Any]] = None,
    timeout: float = 4.0,
) -> Tuple[bool, Any, str]:
    base_url = os.environ.get("NAPCAT_HTTP_URL", "http://127.0.0.1:3000").strip().rstrip("/")
    token = os.environ.get("NAPCAT_TOKEN", "").strip()
    return onebot_common.napcat_api_post(
        base_url,
        token,
        endpoint,
        payload if payload is not None else {},
        timeout=timeout,
    )


def _invoke_napcat(
    napcat_call: Optional[Callable[..., Tuple[bool, Any, str]]],
    endpoint: str,
    payload: Optional[Dict[str, Any]] = None,
    timeout: float = 4.0,
) -> Tuple[bool, Any, str]:
    caller = napcat_call if callable(napcat_call) else _default_napcat_post
    try:
        res = caller(endpoint, payload if payload is not None else {}, timeout=timeout)
        if isinstance(res, tuple) and len(res) == 3:
            return bool(res[0]), res[1], str(res[2] or "")
        return False, None, "无效的 NapCat 调用返回值"
    except Exception as exc:
        return False, None, str(exc)


def _extract_napcat_list(data: Any, *keys: str) -> List[Dict[str, Any]]:
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for k in keys:
            val = data.get(k)
            if isinstance(val, list):
                return [x for x in val if isinstance(x, dict)]
    return []


def _parse_napcat_sync_item(
    item: Dict[str, Any],
    sub_type: str,
    is_ignored: bool,
    group_map: Dict[str, str],
) -> Optional[Dict[str, Any]]:
    flag = str(item.get("flag") or item.get("request_id") or "").strip()
    if not flag:
        return None

    group_id = str(item.get("group_id") or "").strip()
    group_name = str(item.get("group_name") or group_map.get(group_id, "")).strip()[:120]

    invitor_id = str(item.get("invitor_uin") or item.get("invitor_id") or "").strip()
    invitor_name = str(item.get("invitor_nick") or item.get("invitor_name") or "").strip()[:120]

    user_id = str(
        item.get("requester_uin")
        or item.get("user_id")
        or item.get("invitor_uin")
        or ""
    ).strip()
    user_name = str(
        item.get("requester_nick")
        or item.get("user_name")
        or item.get("nickname")
        or item.get("invitor_nick")
        or ""
    ).strip()[:120]

    raw_comment = item.get("message")
    if raw_comment is None:
        raw_comment = item.get("comment")
    comment = str(raw_comment or "").strip()[:1000]

    checked = _to_bool(item.get("checked"), False)
    actor = str(item.get("actor") or "").strip()
    if actor == "0":
        actor = ""

    raw_status = str(item.get("status") or "").strip().lower()
    if checked or raw_status in ("approved", "agreed", "accepted", "rejected", "refused", "refuse", "reject", "denied"):
        if raw_status in ("rejected", "refused", "refuse", "reject", "denied"):
            status = "rejected"
        else:
            status = "approved"
        decision_source = "napcat_sync"
        operator_id = actor
    else:
        status = "pending"
        decision_source = ""
        operator_id = ""

    ts_val = (
        item.get("time")
        if item.get("time") is not None
        else item.get("create_time")
        if item.get("create_time") is not None
        else item.get("created_at")
    )

    return {
        "flag": flag,
        "sub_type": sub_type,
        "group_id": group_id,
        "group_name": group_name,
        "user_id": user_id,
        "user_name": user_name,
        "invitor_id": invitor_id,
        "invitor_name": invitor_name,
        "comment": comment,
        "qq_level": _parse_optional_int(
            item.get("qq_level") if item.get("qq_level") is not None else item.get("qqLevel")
        ),
        "is_ignored": bool(is_ignored),
        "status": status,
        "decision_source": decision_source,
        "operator_id": operator_id,
        "source": "napcat_sync",
        "time": ts_val,
        "_napcat_raw": item,
    }


def sync_from_napcat(
    napcat_call: Optional[Callable[..., Tuple[bool, Any, str]]] = None,
    group_map: Optional[Dict[str, str]] = None,
    count: int = 50,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Pull pending/recent join & invite requests from NapCat and merge into SQLite."""
    gmap = {str(k).strip(): str(v).strip() for k, v in (group_map or {}).items() if str(k).strip()}
    cnt = max(1, min(200, int(count or 50)))

    ok_sys, data_sys, err_sys = _invoke_napcat(
        napcat_call,
        "get_group_system_msg",
        {"count": cnt},
        timeout=5.0,
    )
    ok_ign, data_ign, _ = _invoke_napcat(
        napcat_call,
        "get_group_ignore_add_request",
        {},
        timeout=4.0,
    )

    if not ok_sys and not ok_ign:
        return {
            "ok": False,
            "synced_new": 0,
            "synced_updated": 0,
            "total_fetched": 0,
            "error": err_sys or "无法从 NapCat 同步入群申请",
        }

    # Collect & deduplicate by flag
    by_flag: Dict[str, Dict[str, Any]] = {}

    def _merge_parsed(parsed: Optional[Dict[str, Any]]) -> None:
        if not parsed:
            return
        flg = parsed["flag"]
        existing = by_flag.get(flg)
        if existing is None:
            by_flag[flg] = parsed
            return
        # Merge richer metadata without losing ignored flag or checked status
        if parsed["is_ignored"]:
            existing["is_ignored"] = True
        for k in ("group_id", "group_name", "user_id", "user_name", "invitor_id", "invitor_name", "comment"):
            if not existing.get(k) and parsed.get(k):
                existing[k] = parsed[k]
        if existing.get("qq_level") is None and parsed.get("qq_level") is not None:
            existing["qq_level"] = parsed["qq_level"]
        if existing.get("status") == "pending" and parsed.get("status") in ("approved", "rejected"):
            existing["status"] = parsed["status"]
            existing["decision_source"] = parsed["decision_source"]
            if parsed.get("operator_id"):
                existing["operator_id"] = parsed["operator_id"]

    if ok_sys and isinstance(data_sys, dict):
        for item in _extract_napcat_list(data_sys, "join_requests"):
            _merge_parsed(_parse_napcat_sync_item(item, "add", False, gmap))
        for inv_key in ("invited_requests", "InvitedRequest"):
            for item in _extract_napcat_list(data_sys, inv_key):
                _merge_parsed(_parse_napcat_sync_item(item, "invite", False, gmap))

    if ok_ign and data_ign is not None:
        for item in _extract_napcat_list(data_ign, "join_requests", "requests", "list"):
            _merge_parsed(_parse_napcat_sync_item(item, "add", True, gmap))

    synced_new = 0
    synced_updated = 0
    for parsed in by_flag.values():
        _, is_new, was_updated = _record_request_internal(parsed, db_path=db_path)
        if is_new:
            synced_new += 1
        elif was_updated:
            synced_updated += 1

    total_fetched = len(by_flag)
    if synced_new > 0 or synced_updated > 0:
        try:
            _emit_event_standalone(
                "synced",
                payload={
                    "synced_new": synced_new,
                    "synced_updated": synced_updated,
                    "total_fetched": total_fetched,
                },
                db_path=db_path,
            )
        except Exception:
            pass

    return {
        "ok": bool(ok_sys),
        "synced_new": synced_new,
        "synced_updated": synced_updated,
        "total_fetched": total_fetched,
        "error": "" if ok_sys else (err_sys or ""),
    }


def execute_decision(
    flag_or_id: Any,
    approve: bool,
    reason: str = "",
    decision_source: str = "webui",
    operator_id: str = "",
    napcat_call: Optional[Callable[..., Tuple[bool, Any, str]]] = None,
    db_path: Optional[str] = None,
    policy_path: Optional[str] = None,
    send_welcome: bool = True,
) -> Dict[str, Any]:
    """Execute approve/reject via NapCat ``set_group_add_request`` and persist outcome."""
    req = get_request(flag_or_id, db_path=db_path)
    if req is None:
        raise ValueError("找不到对应的入群申请记录")

    lock = _get_decision_lock(req["flag"])
    with lock:
        # Re-read under lock to prevent duplicate concurrent execution
        req = get_request(req["id"], db_path=db_path)
        if req is None:
            raise ValueError("找不到对应的入群申请记录")

        if req["status"] in ("approved", "rejected"):
            return {
                "ok": False,
                "already_handled": True,
                "request": req,
                "error": "该申请已处理",
                "welcome_sent": False,
            }

        approve_bool = bool(approve)
        clean_reason = str(reason or "").strip()[:120]
        payload: Dict[str, Any] = {
            "flag": req["flag"],
            "sub_type": req["sub_type"] or "add",
            "approve": approve_bool,
        }
        if not approve_bool and clean_reason:
            payload["reason"] = clean_reason

        ok, _, err = _invoke_napcat(
            napcat_call,
            "set_group_add_request",
            payload,
            timeout=6.0,
        )

        if ok:
            new_status = "approved" if approve_bool else "rejected"
            updated_req = mark_request_handled(
                req["id"],
                status=new_status,
                decision_source=decision_source,
                operator_id=operator_id,
                reject_reason="" if approve_bool else clean_reason,
                error_msg="",
                db_path=db_path,
            ) or req

            welcome_sent = False
            if approve_bool and send_welcome and (req["sub_type"] or "add") == "add":
                try:
                    eff = resolve_group_policy(req["group_id"], policy_path=policy_path)
                    tpl = str(eff.get("welcome_message") or "").strip()
                    gid_str = str(req["group_id"] or "").strip()
                    if tpl and gid_str.isdigit():
                        display_user = req["user_name"] or req["user_id"] or "新成员"
                        welcome_text = (
                            tpl.replace("{user}", display_user)
                            .replace("{qq}", req["user_id"] or "")
                            .strip()
                        )
                        if welcome_text:
                            w_ok, _, _ = _invoke_napcat(
                                napcat_call,
                                "send_group_msg",
                                {"group_id": int(gid_str), "message": welcome_text},
                                timeout=4.0,
                            )
                            welcome_sent = bool(w_ok)
                except Exception:
                    welcome_sent = False

            return {
                "ok": True,
                "request": updated_req,
                "error": "",
                "welcome_sent": welcome_sent,
            }

        err_msg = str(err or "NapCat 审批调用失败")
        updated_req = mark_request_handled(
            req["id"],
            status="failed",
            decision_source=decision_source,
            operator_id=operator_id,
            reject_reason="" if approve_bool else clean_reason,
            error_msg=err_msg,
            db_path=db_path,
        ) or req
        return {
            "ok": False,
            "request": updated_req,
            "error": err_msg,
            "welcome_sent": False,
        }


def _format_admin_dm_notice(
    req: Dict[str, Any],
    eval_res: Optional[Dict[str, Any]],
    dec_res: Optional[Dict[str, Any]],
) -> str:
    kind = "邀群申请" if req.get("sub_type") == "invite" else "入群申请"
    grp = f"{req['group_name']}({req['group_id']})" if req.get("group_name") else str(req.get("group_id") or "")
    usr = f"{req['user_name']}({req['user_id']})" if req.get("user_name") else str(req.get("user_id") or "")
    if req.get("qq_level") is not None:
        usr += f" Lv.{req['qq_level']}"

    status = req.get("status") or "pending"
    if status == "approved":
        state_line = f"处理结果：已自动通过（{(eval_res or {}).get('reason') or req.get('decision_source') or '自动规则'}）"
    elif status == "rejected":
        state_line = f"处理结果：已自动拒绝（{(eval_res or {}).get('reason') or req.get('reject_reason') or '自动规则'}）"
    elif status == "failed":
        state_line = f"处理结果：自动处理失败（{(dec_res or {}).get('error') or req.get('error_msg') or '未知错误'}）"
    else:
        reason = (eval_res or {}).get("reason") or "需人工审批"
        state_line = f"处理结果：待人工审批（{reason}）"

    lines = [
        f"【{kind}通知】",
        f"群聊：{grp}",
        f"用户：{usr}",
    ]
    if req.get("comment"):
        lines.append(f"验证信息：{req['comment'][:200]}")
    lines.append(state_line)
    return "\n".join(lines)


def handle_ws_request_event(
    obj: Dict[str, Any],
    napcat_call: Optional[Callable[..., Tuple[bool, Any, str]]] = None,
    group_map: Optional[Dict[str, str]] = None,
    admin_qq_ids: Optional[Iterable[Any]] = None,
    db_path: Optional[str] = None,
    policy_path: Optional[str] = None,
    sleep_fn: Callable[[float], Any] = time.sleep,
) -> Dict[str, Any]:
    """Process one OneBot v11 WebSocket ``request.group`` event end-to-end."""
    if (
        not isinstance(obj, dict)
        or obj.get("post_type") != "request"
        or obj.get("request_type") != "group"
    ):
        return {
            "handled": False,
            "is_new": False,
            "evaluation": None,
            "decision": None,
            "request": None,
        }

    enriched = dict(obj)
    enriched.setdefault("source", "ws")

    gid = str(enriched.get("group_id") or "").strip()
    uid = str(enriched.get("user_id") or "").strip()
    gmap = {str(k).strip(): str(v).strip() for k, v in (group_map or {}).items() if str(k).strip()}

    # Enrich group_name (cache map first, then NapCat get_group_info fail-open)
    if not str(enriched.get("group_name") or "").strip():
        if gid in gmap and gmap[gid]:
            enriched["group_name"] = gmap[gid]
        elif gid.isdigit():
            ok_g, data_g, _ = _invoke_napcat(
                napcat_call,
                "get_group_info",
                {"group_id": int(gid)},
                timeout=2.0,
            )
            if ok_g and isinstance(data_g, dict):
                gname = str(data_g.get("group_name") or "").strip()
                if gname:
                    enriched["group_name"] = gname

    # Enrich user_name and qq_level (NapCat get_stranger_info fail-open)
    need_uname = not str(enriched.get("user_name") or "").strip()
    need_level = enriched.get("qq_level") is None
    if (need_uname or need_level) and uid.isdigit():
        ok_u, data_u, _ = _invoke_napcat(
            napcat_call,
            "get_stranger_info",
            {"user_id": int(uid), "no_cache": False},
            timeout=2.0,
        )
        if ok_u and isinstance(data_u, dict):
            if need_uname:
                nick = str(data_u.get("nickname") or data_u.get("nick") or "").strip()
                if nick:
                    enriched["user_name"] = nick
            if need_level:
                raw_lvl = (
                    data_u.get("qqLevel")
                    if data_u.get("qqLevel") is not None
                    else data_u.get("level")
                )
                lvl = _parse_optional_int(raw_lvl)
                if lvl is not None:
                    enriched["qq_level"] = lvl

    req, is_new = record_incoming_request(enriched, db_path=db_path)

    if req["status"] != "pending":
        return {
            "handled": False,
            "is_new": is_new,
            "evaluation": None,
            "decision": None,
            "request": req,
        }

    pol = load_policy(path=policy_path)
    eval_res = evaluate_request_policy(req, policy=pol)

    dec_res: Optional[Dict[str, Any]] = None
    final_req = req
    handled = False

    if eval_res["action"] in ("approve", "reject"):
        delay = min(max(0.0, float(eval_res.get("delay_secs") or 0.0)), 15.0)
        if delay > 0.0 and callable(sleep_fn):
            sleep_fn(delay)
        dec_res = execute_decision(
            req["id"],
            approve=(eval_res["action"] == "approve"),
            reason=str(eval_res.get("reason") or ""),
            decision_source=str(eval_res.get("source") or "auto"),
            operator_id="auto",
            napcat_call=napcat_call,
            db_path=db_path,
            policy_path=policy_path,
            send_welcome=True,
        )
        final_req = dec_res.get("request") or req
        handled = bool(dec_res.get("ok"))

    # Optional admin DM notification (fail-open)
    eff = resolve_group_policy(req["group_id"], policy=pol)
    if eff.get("notify_admin_dm") and admin_qq_ids and (is_new or handled):
        dm_text = _format_admin_dm_notice(final_req, eval_res, dec_res)
        seen_admins: set[str] = set()
        for raw_adm in admin_qq_ids:
            adm = str(raw_adm or "").strip()
            if not adm.isdigit() or adm in seen_admins:
                continue
            seen_admins.add(adm)
            try:
                _invoke_napcat(
                    napcat_call,
                    "send_private_msg",
                    {"user_id": int(adm), "message": dm_text},
                    timeout=3.0,
                )
            except Exception:
                pass

    return {
        "handled": handled,
        "is_new": is_new,
        "evaluation": eval_res,
        "decision": dec_res,
        "request": final_req,
    }
