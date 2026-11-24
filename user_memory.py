#!/usr/bin/env python3
"""Unified facade over the SQLite WAL + FTS5 memory engine (`memory_engine/`).

Preserves backwards compatibility with existing callers (`agy-acp-wrapper.py`,
`onebot-ws-guard.py`, `webui/stores.py`) while exposing the v2 API:
- `record_chat_message(...)`: <1ms real-time raw message persistence + window triggers
- `build_prompt_memory_blocks(...)`: session-first-turn continuity + L0/L2 hybrid recall
- `run_idle_flush(...)`: periodic idle-window consolidation
- `run_sleep_cycle(...)` / `should_run_sleep(...)`: overnight maintenance
"""
from __future__ import annotations

import importlib
import inspect
import os
import sys

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

import bot_env  # noqa: E402,F401  (loads bot.env before reading settings)

try:
    import bot_plugins as _bot_plugins  # noqa: E402
except Exception:
    _bot_plugins = None


def _plugin_enabled(plugin_id: str, default: bool = True) -> bool:
    if _bot_plugins is None:
        return default
    try:
        return bool(_bot_plugins.is_enabled(plugin_id))
    except Exception:
        return default


def _plugin_setting(plugin_id: str, key: str, default=None):
    if _bot_plugins is None:
        return default
    try:
        return _bot_plugins.get_setting(plugin_id, key, default)
    except Exception:
        return default

from memory_engine import (  # noqa: E402
    consolidate as _consolidate,
    continuity as _continuity,
    db as _db,
    embedder as _embedder,
    ingest as _ingest,
    quality_gate as _quality_gate,
    retrieve as _retrieve,
    sleep as _sleep,
    tokenizer as _tokenizer,
)

# ---------------------------------------------------------------------------
# Backwards-compatible module constants & symbols
# ---------------------------------------------------------------------------

AGY_BIN = bot_env.resolve_agy_bin()
MEMORY_DIR = (
    os.environ.get("USER_MEMORY_DIR", "").strip()
    or os.path.join(_SCRIPT_DIR, "data", "user_memories")
)
MEMORY_DB_PATH = _db.default_db_path()
EXTRACTOR_CWD = _db.default_extractor_cwd()
MEMORY_MODEL = os.environ.get("MEMORY_MODEL", "gemini-4-neon").strip() or "gemini-4-neon"
MEMORY_EFFORT = os.environ.get("MEMORY_EFFORT", "medium").strip() or "medium"
MEMORY_ENABLED = _db.is_memory_enabled()
MEMORY_MAX_CONCURRENT_JOBS = _db.get_env_int("MEMORY_MAX_CONCURRENT_JOBS", 2)

SENSITIVE_MEMORY_RE = _quality_gate.SENSITIVE_MEMORY_RE
MEMORY_TRIGGER_CUES = _ingest.MEMORY_TRIGGER_CUES
TRIVIAL_MSG_RE = _tokenizer.TRIVIAL_MSG_RE

# Re-export key submodules for advanced callers (`migrate_v1` is lazy-loaded via __getattr__)
db = _db
ingest = _ingest
consolidate = _consolidate
retrieve = _retrieve
continuity = _continuity
sleep = _sleep
embedder = _embedder
quality_gate = _quality_gate
tokenizer = _tokenizer

_KW_SUPPORT_CACHE: dict[tuple, bool] = {}


def __getattr__(name: str):
    if name == "migrate_v1":
        mod = importlib.import_module("memory_engine.migrate_v1")
        globals()["migrate_v1"] = mod
        return mod
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _log_err(where: str, exc: Exception) -> None:
    try:
        sys.stderr.write(f"[user-memory] {where} error: {exc}\n")
        sys.stderr.flush()
    except Exception:
        pass


def _fn_accepts_kw(fn, kw: str) -> bool:
    cache_key = None
    try:
        cache_key = (fn, kw)
        cached = _KW_SUPPORT_CACHE.get(cache_key)
        if cached is not None:
            return cached
    except TypeError:
        cache_key = None
    try:
        params = inspect.signature(fn).parameters
        supported = kw in params or any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
        )
    except (TypeError, ValueError):
        supported = True
    if cache_key is not None and len(_KW_SUPPORT_CACHE) < 128:
        _KW_SUPPORT_CACHE[cache_key] = supported
    return supported


def get_db_path(db_path: str | None = None) -> str:
    """Resolve the active SQLite database path."""
    if db_path:
        return str(db_path)
    env_path = os.environ.get("MEMORY_DB_PATH", "").strip()
    if env_path:
        return env_path
    return MEMORY_DB_PATH or _db.default_db_path()


def _is_enabled() -> bool:
    return bool(MEMORY_ENABLED) and _db.is_memory_enabled()


def _now_str(ts: int | None = None) -> str:
    return _db.now_str(ts)


def _today_str(ts: int | None = None) -> str:
    return _db.today_str(ts)


def _sanitize_uid(user_id) -> str:
    return _db.sanitize_uid(user_id)


def _empty_profile() -> dict:
    return {
        "summary": "",
        "facts": [],
        "inside_jokes": [],
        "relationship": "",
    }


def _empty_record(user_id) -> dict:
    now = _now_str()
    return {
        "version": 2,
        "user_id": str(user_id),
        "nicknames": [],
        "seen_groups": [],
        "first_seen": now,
        "last_seen": now,
        "msg_count": 0,
        "pending_public_msgs": [],
        "pending_dm_msgs": [],
        "public_profile": _empty_profile(),
        "dm_profile": _empty_profile(),
        "last_consolidated_public_at": 0.0,
        "last_consolidated_dm_at": 0.0,
    }


def _profile_has_content(profile) -> bool:
    if not isinstance(profile, dict):
        return False
    return bool(
        (profile.get("summary") or "").strip()
        or profile.get("facts")
        or profile.get("inside_jokes")
        or (profile.get("relationship") or "").strip()
    )


def _is_sensitive_memory(text: str) -> bool:
    return _quality_gate.is_sensitive(text)


# ---------------------------------------------------------------------------
# v2 Core Facade API
# ---------------------------------------------------------------------------

def record_chat_message(
    chat_key: str = "",
    role: str = "user",
    text: str = "",
    sender_uid: str = "",
    sender_name: str = "",
    is_group: bool = True,
    group_id: str = "",
    group_name: str = "",
    auto_consolidate: bool = True,
    ts: int | None = None,
    db_path: str | None = None,
) -> dict:
    """Persist a clean chat message into `raw_messages` (<1ms) and trigger async
    window consolidation when high-signal or full-window thresholds fire.
    """
    if not _is_enabled():
        return {"recorded": False, "reason": "disabled", "should_consolidate": False}

    ext_on = _plugin_enabled("memory_extraction")
    cont_on = _plugin_enabled("memory_continuity")
    rec_on = _plugin_enabled("memory_recall")
    if not ext_on and not cont_on and not rec_on:
        return {"recorded": False, "reason": "plugin_disabled", "should_consolidate": False}
    if not ext_on:
        auto_consolidate = False

    resolved_db = get_db_path(db_path)
    try:
        ingest_kwargs = dict(
            chat_key=chat_key,
            role=role,
            text=text,
            sender_uid=sender_uid,
            sender_name=sender_name,
            is_group=is_group,
            group_id=group_id,
            ts=ts,
            db_path=resolved_db,
        )
        if _fn_accepts_kw(_ingest.record_message, "group_name"):
            ingest_kwargs["group_name"] = group_name
        res = _ingest.record_message(**ingest_kwargs)
    except Exception as exc:
        _log_err("record_chat_message", exc)
        return {"recorded": False, "reason": "error", "should_consolidate": False}

    if auto_consolidate and res.get("recorded") and res.get("should_consolidate"):
        ck = str(res.get("chat_key") or chat_key)
        delay = float(res.get("delay") or 1.0)
        if ck:
            try:
                _consolidate.schedule_window_consolidation(
                    ck,
                    delay=delay,
                    db_path=resolved_db,
                )
            except Exception as exc:
                _log_err("schedule_window_consolidation", exc)
    return res


def build_prompt_memory_blocks(
    sender_id: str,
    sender_name: str = "",
    query: str = "",
    is_group: bool = True,
    group_id: str = "",
    peer_users=None,
    admin_ids=None,
    session_id: str = "",
    is_first_turn: bool = False,
    napcat_recent_lines=None,
    db_path: str | None = None,
    record_trace: bool = True,
    apply_session_dedup: bool = True,
    is_admin: bool = False,
    group_names: dict | None = None,
    current_user_text: str = "",
) -> dict:
    """Assemble continuity (first turn only) + L0 hot profiles + L2 hybrid recall.

    Privacy guarantee:
    - Group chats ONLY read `user_public` and `group:<group_id>` (never `dm:*`).
    - Private DMs ONLY read `user_public` and `dm:<sender_id>` (never other users' DMs).
    """
    empty = {
        "continuity_block": "",
        "l0_block": "",
        "l2_block": "",
        "briefing_block": "",
        "memory_block": "",
        "blocks": [],
        "selected": [],
        "omitted": [],
        "relations": [],
        "gate_info": {},
    }
    if not _is_enabled():
        return empty

    rec_on = _plugin_enabled("memory_recall")
    cont_on = _plugin_enabled("memory_continuity")
    if not rec_on and not cont_on:
        return empty

    resolved_db = get_db_path(db_path)
    uid = _sanitize_uid(sender_id)
    gid = _sanitize_uid(group_id) if is_group else ""
    chat_key = _db.make_chat_key(is_group, gid, uid)

    continuity_block = ""
    if cont_on and is_first_turn and chat_key:
        try:
            cont_max_chars = int(_plugin_setting("memory_continuity", "max_chars", 2400))
        except (TypeError, ValueError):
            cont_max_chars = 2400
        try:
            cont_kwargs = dict(
                chat_key=chat_key,
                is_group=is_group,
                group_id=gid,
                user_id=uid,
                napcat_recent_lines=napcat_recent_lines,
                max_chars=cont_max_chars,
                db_path=resolved_db,
            )
            if _fn_accepts_kw(_continuity.build_continuity_block, "current_user_text"):
                cont_kwargs["current_user_text"] = current_user_text or query
            continuity_block = _continuity.build_continuity_block(**cont_kwargs)
        except Exception as exc:
            _log_err("build_continuity_block", exc)
            continuity_block = ""

    recall = {
        "l0_block": "",
        "l2_block": "",
        "briefing_block": "",
        "combined_block": "",
        "selected": [],
        "omitted": [],
        "relations": [],
        "gate_info": {},
    }
    if rec_on:
        try:
            rec_max_chars = int(_plugin_setting("memory_recall", "max_chars_override", 0) or 0)
        except (TypeError, ValueError):
            rec_max_chars = 0
        try:
            rec_max_items = int(_plugin_setting("memory_recall", "max_items_override", 0) or 0)
        except (TypeError, ValueError):
            rec_max_items = 0
        rec_group_mem = bool(_plugin_setting("memory_recall", "group_memory_enabled", True))
        try:
            rec_kwargs = dict(
                sender_id=uid,
                sender_name=sender_name,
                query=query,
                is_group=is_group,
                group_id=gid,
                peer_users=peer_users,
                admin_ids=admin_ids,
                session_id=session_id,
                db_path=resolved_db,
                record_trace=record_trace,
                apply_session_dedup=apply_session_dedup,
                max_chars=rec_max_chars if rec_max_chars > 0 else None,
                max_items=rec_max_items if rec_max_items > 0 else None,
                group_memory_enabled=False if not rec_group_mem else None,
            )
            if _fn_accepts_kw(_retrieve.build_recall_blocks, "is_admin"):
                rec_kwargs["is_admin"] = bool(is_admin)
            if _fn_accepts_kw(_retrieve.build_recall_blocks, "group_names"):
                rec_kwargs["group_names"] = group_names
            recall = _retrieve.build_recall_blocks(**rec_kwargs)
        except Exception as exc:
            _log_err("build_recall_blocks", exc)

    briefing_block = recall.get("briefing_block") or ""
    memory_block = recall.get("combined_block") or ""
    blocks = []
    if continuity_block:
        blocks.append(continuity_block)
    if memory_block:
        blocks.append(memory_block)

    return {
        "continuity_block": continuity_block,
        "l0_block": recall.get("l0_block") or "",
        "l2_block": recall.get("l2_block") or "",
        "briefing_block": briefing_block,
        "memory_block": memory_block,
        "blocks": blocks,
        "selected": recall.get("selected") or [],
        "omitted": recall.get("omitted") or [],
        "relations": recall.get("relations") or [],
        "gate_info": recall.get("gate_info") or {},
    }


def run_idle_flush(
    db_path: str | None = None,
    idle_mins: float | None = None,
    llm_fn=None,
    max_windows: int = 10,
    respect_plugin: bool = True,
) -> dict:
    """Consolidate idle chat windows that have undistilled effective messages."""
    if not _is_enabled():
        return {"windows_checked": 0, "windows_flushed": 0, "results": []}
    if respect_plugin and not _plugin_enabled("memory_extraction"):
        return {
            "skipped": True,
            "reason": "plugin_disabled",
            "windows_checked": 0,
            "windows_flushed": 0,
            "results": [],
        }
    try:
        return _consolidate.run_idle_flush(
            db_path=get_db_path(db_path),
            idle_mins=idle_mins,
            llm_fn=llm_fn,
            max_windows=max_windows,
        )
    except Exception as exc:
        _log_err("run_idle_flush", exc)
        return {"windows_checked": 0, "windows_flushed": 0, "results": [], "error": str(exc)}


def should_run_sleep(
    db_path: str | None = None,
    now_ts: int | None = None,
    min_quiet_mins: int = 30,
) -> tuple[bool, str]:
    """Check whether the overnight Sleep cycle should run right now."""
    if not _is_enabled():
        return False, "disabled"
    return _sleep.should_run_sleep(
        db_path=get_db_path(db_path),
        now_ts=now_ts,
        min_quiet_mins=min_quiet_mins,
    )


def run_sleep_cycle(
    db_path: str | None = None,
    force: bool = False,
    llm_fn=None,
    interrupt_check=None,
    now_ts: int | None = None,
) -> dict:
    """Run the 4-phase overnight Sleep maintenance cycle."""
    if not _is_enabled() and not force:
        return {"ran": False, "reason": "disabled"}
    try:
        return _sleep.run_sleep_cycle(
            db_path=get_db_path(db_path),
            force=force,
            llm_fn=llm_fn,
            interrupt_check=interrupt_check,
            now_ts=now_ts,
        )
    except Exception as exc:
        _log_err("run_sleep_cycle", exc)
        return {"ran": False, "reason": "error", "error": str(exc)}


def checkpoint_wal(db_path: str | None = None, mode: str = "PASSIVE") -> dict:
    """Run a SQLite WAL checkpoint (`PASSIVE`, `FULL`, `RESTART`, or `TRUNCATE`)."""
    resolved_db = get_db_path(db_path)
    fn = getattr(_db, "checkpoint_wal", None)
    if fn is None:
        return {"ok": False, "mode": mode, "reason": "unsupported"}
    try:
        return fn(db_path=resolved_db, mode=mode)
    except Exception as exc:
        _log_err("checkpoint_wal", exc)
        return {"ok": False, "mode": mode, "error": str(exc)}


def clear_session_state(session_id: str | None = None) -> None:
    """Reset in-process L0/L2 session dedup and continuity bootstrap state."""
    _retrieve.clear_session_state(session_id)
    if session_id:
        _continuity.DEFAULT_BOOTSTRAP_TRACKER.reset_session(session_id)
    else:
        _continuity.DEFAULT_BOOTSTRAP_TRACKER.reset_all()
