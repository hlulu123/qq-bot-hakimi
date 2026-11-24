"""SQLite v2 User & Group Memory Store for QQ Bot「哈基米」Web UI (Python 3.9 stdlib only).

Extracted from `webui/stores.py` for modularity; all public & test-used symbols
are re-exported by `webui.stores` for 100% backwards compatibility.
"""

import datetime
import hashlib
import os
import re
import sys
import threading

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

import bot_env  # noqa: E402
import bot_plugins  # noqa: E402
import user_memory  # noqa: E402

from .config_schema import NotFoundError, SensitiveMemoryError, StoreError

BOT_ENV_PATH = os.path.join(PROJECT_DIR, "bot.env")

_USER_MEMORY_KINDS = frozenset({"stable", "episodic", "joke"})
_DATE_YMD_RE = re.compile(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})(?:[T\s].*)?$")

_sleep_running_lock = threading.Lock()
_sleep_in_progress = False

# Registered by webui.stores on import so test monkeypatches on
# `webui.stores._get_memory_db_path` / `webui.stores.parse_bot_env` propagate seamlessly.
_stores_mod = None


def _split_csv(raw):
    return [x.strip() for x in str(raw or "").split(",") if x.strip()]


def _bool_flag(val, default=False):
    if val is None:
        return default
    return str(val).strip().lower() not in ("", "0", "false", "no", "off")


def _get_stores_mod():
    if _stores_mod is not None:
        return _stores_mod
    pkg = __package__ or "webui"
    return sys.modules.get(f"{pkg}.stores") or sys.modules.get("webui.stores")


def _resolve_parse_bot_env():
    mod = _get_stores_mod()
    if mod is not None and hasattr(mod, "parse_bot_env"):
        return mod.parse_bot_env()
    parser = getattr(bot_env, "parse_file", None) or getattr(bot_env, "parse_bot_env", None)
    if callable(parser):
        try:
            res = parser(BOT_ENV_PATH)
            if isinstance(res, dict):
                return res
        except Exception:
            pass
    data = {}
    try:
        with open(BOT_ENV_PATH, "r", encoding="utf-8-sig") as f:
            for line in f:
                parsed = bot_env._parse_line(line)
                if parsed:
                    data[parsed[0]] = parsed[1]
    except OSError:
        pass
    return data


def _resolve_memory_db_path(env=None):
    mod = _get_stores_mod()
    if mod is not None and hasattr(mod, "_get_memory_db_path"):
        return mod._get_memory_db_path(env)
    if env is None:
        env = _resolve_parse_bot_env()
    raw = (env.get("MEMORY_DB_PATH") or "").strip() if isinstance(env, dict) else ""
    if not raw:
        raw = (user_memory.get_db_path() or "").strip()
    if not raw:
        return os.path.join(PROJECT_DIR, "data", "memory.db")
    if raw == ":memory:" or raw.startswith("file:"):
        return raw
    expanded = os.path.expanduser(raw)
    if not os.path.isabs(expanded):
        return os.path.abspath(os.path.join(PROJECT_DIR, expanded))
    return expanded


def _norm_mem_text(text):
    return re.sub(r"\s+", " ", str(text or "")).strip().lower()


def _scope_active_memories(conn, scope, uid=""):
    sql = "SELECT * FROM memories WHERE scope = ? AND status = 'active'"
    params = (scope,)
    if uid:
        sql += " AND owner_uid = ?"
        params += (uid,)
    return conn.execute(
        sql + " ORDER BY importance DESC, proof_count DESC, updated_at DESC, id",
        params,
    ).fetchall()


def _memory_revision(profile, memories):
    profile_fields = ("nicknames_json", "summary", "relationship")
    memory_fields = ("id", "text", "kind", "category", "fact_key", "importance",
                     "occurred_at", "keywords_json")
    profile_data = [profile[field] for field in profile_fields] if profile else ["[]", "", ""]
    memory_data = sorted(tuple(row[field] for field in memory_fields) for row in memories)
    encoded = user_memory.db.json_dumps_compact([profile_data, memory_data]).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _check_memory_revision(conn, expected, scope, profile_key, uid=""):
    if expected is None:
        return
    profile = conn.execute(
        "SELECT * FROM profiles WHERE profile_key = ?", (profile_key,),
    ).fetchone()
    if expected != _memory_revision(profile, _scope_active_memories(conn, scope, uid)):
        raise StoreError("记忆档案已更新。请保留本次修改，重新载入最新档案后再保存。", status_code=409)


def _clean_nicknames(value):
    if not isinstance(value, list):
        return None
    names = (str(item).strip() for item in value if item is not None)
    return list(dict.fromkeys(name for name in names if name))[-10:]


def _normalize_date_input(raw):
    if raw is None or isinstance(raw, bool):
        return ""
    s = str(raw).strip()
    if not s:
        return ""
    m = _DATE_YMD_RE.match(s)
    if m:
        try:
            dt = datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            return ""
    return user_memory.db.ts_to_date(s)


def _normalize_keywords_input(raw_keywords, fallback_text="", limit=12):
    items = []
    if isinstance(raw_keywords, str):
        items = [p.strip() for p in re.split(r"[,，、;\s]+", raw_keywords) if p.strip()]
    elif isinstance(raw_keywords, (list, tuple, set)):
        items = [str(x).strip() for x in raw_keywords if str(x).strip()]

    out = []
    seen = set()
    for kw in items:
        clean = re.sub(r"\s+", " ", kw).strip()[:24]
        if not clean:
            continue
        low = clean.lower()
        if low in seen:
            continue
        seen.add(low)
        out.append(clean)
        if len(out) >= limit:
            break

    if not out and fallback_text:
        for kw in user_memory.tokenizer.auto_keywords(fallback_text, max_keywords=8):
            clean = str(kw).strip()[:24]
            if not clean:
                continue
            low = clean.lower()
            if low not in seen:
                seen.add(low)
                out.append(clean)
                if len(out) >= limit:
                    break
    return out


def _append_mem_history(history_json, entry, max_entries=50):
    hist = user_memory.db.json_loads_safe(history_json, [])
    if not isinstance(hist, list):
        hist = []
    hist.append(entry)
    if len(hist) > max_entries:
        hist = hist[-max_entries:]
    return user_memory.db.json_dumps_compact(hist)


def _extract_conflict_info(history_json):
    """Return (partner_id, reason, flagged_ts) from latest conflict_flagged history entry."""
    hist = user_memory.db.json_loads_safe(history_json, [])
    if not isinstance(hist, list):
        return "", "", 0
    for entry in reversed(hist):
        if isinstance(entry, dict) and str(entry.get("action") or "") == "conflict_flagged":
            pid = str(entry.get("partner_id") or "").strip()
            reason = str(entry.get("reason") or "").strip()
            ts = int(user_memory.db.parse_ts(entry.get("ts"), 0) or 0)
            return pid, reason, ts
    return "", "", 0


def _count_conflict_pairs(conn):
    rows = conn.execute(
        "SELECT id, history_json FROM memories WHERE status = 'conflict_pending'"
    ).fetchall()
    seen_pairs = set()
    count = 0
    for row in rows:
        mid = str(row["id"] or "").strip()
        if not mid:
            continue
        pid, _, _ = _extract_conflict_info(row["history_json"])
        if pid and pid != mid:
            pair_key = tuple(sorted((mid, pid)))
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)
        count += 1
    return count


def _sync_live_memory_runtime_config(env):
    if not isinstance(env, dict):
        return
    for k, v in env.items():
        ks = str(k or "").strip()
        if ks == "USER_MEMORY_ENABLED" or ks.startswith("MEMORY_"):
            os.environ[ks] = str(v if v is not None else "")
    if "USER_MEMORY_ENABLED" in env:
        user_memory.MEMORY_ENABLED = _bool_flag(env.get("USER_MEMORY_ENABLED", "1"), True)
    else:
        user_memory.MEMORY_ENABLED = user_memory.db.is_memory_enabled()
    model = (env.get("MEMORY_MODEL") or "").strip()
    effort = (env.get("MEMORY_EFFORT") or "").strip()
    if model:
        user_memory.MEMORY_MODEL = model
        user_memory.consolidate.MEMORY_MODEL = model
    if effort:
        user_memory.MEMORY_EFFORT = effort
        user_memory.consolidate.MEMORY_EFFORT = effort


def _table_has_column(conn, table_name, col_name):
    try:
        rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        for r in rows:
            name = r["name"] if hasattr(r, "keys") else r[1]
            if str(name) == col_name:
                return True
    except Exception:
        return False
    return False


def _user_exists_in_db(conn, uid):
    row = conn.execute(
        "SELECT 1 FROM profiles WHERE profile_key IN (?, ?) LIMIT 1",
        (f"user_public:{uid}", f"dm:{uid}"),
    ).fetchone()
    if row is not None:
        return True
    row = conn.execute(
        "SELECT 1 FROM memories WHERE owner_uid = ? OR scope = ? LIMIT 1",
        (uid, f"dm:{uid}"),
    ).fetchone()
    if row is not None:
        return True
    row = conn.execute(
        "SELECT 1 FROM raw_messages "
        "WHERE (sender_uid = ? AND distilled = 0) OR chat_key = ? LIMIT 1",
        (uid, f"dm:{uid}"),
    ).fetchone()
    return row is not None


def _format_memory_item(row):
    db = user_memory.db
    kws = [
        str(k).strip()
        for k in db.json_loads_safe(row["keywords_json"], [])
        if str(k).strip()
    ]
    occ = str(row["occurred_at"] or "").strip()
    upd_ts = int(row["updated_at"] or 0)
    upd = db.ts_to_date(upd_ts) or occ
    return {
        "id": str(row["id"] or ""),
        "text": str(row["text"] or ""),
        "category": str(row["category"] or "其他"),
        "kind": str(row["kind"] or "stable"),
        "fact_key": str(row["fact_key"] or ""),
        "proof_count": int(row["proof_count"] or 1),
        "importance": int(row["importance"] or 3),
        "occurred_at": occ,
        "status": str(row["status"] or "active"),
        "keywords": kws,
        "updated_at": upd,
    }


def _format_archived_item(row):
    db = user_memory.db
    scope = str(row["scope"] or "")
    if scope == "user_public":
        scope_label = "public"
    elif scope.startswith("dm:"):
        scope_label = "dm"
    elif scope.startswith("group:"):
        scope_label = "group"
    else:
        scope_label = scope
    kws = [
        str(k).strip()
        for k in db.json_loads_safe(row["keywords_json"], [])
        if str(k).strip()
    ]
    occ = str(row["occurred_at"] or "").strip()
    upd_ts = int(row["updated_at"] or 0)
    return {
        "id": str(row["id"] or ""),
        "scope": scope,
        "scope_label": scope_label,
        "kind": str(row["kind"] or "stable"),
        "category": str(row["category"] or "其他"),
        "fact_key": str(row["fact_key"] or ""),
        "text": str(row["text"] or ""),
        "proof_count": int(row["proof_count"] or 1),
        "importance": int(row["importance"] or 3),
        "occurred_at": occ,
        "status": str(row["status"] or "archived"),
        "ended_at": db.ts_to_date(row["ended_at"]),
        "superseded_by": str(row["superseded_by"] or ""),
        "keywords": kws,
        "updated_at": db.now_str(upd_ts) or occ,
    }


def _scan_sensitive_texts(checks):
    """Validate `(field, text)` pairs against `user_memory.SENSITIVE_MEMORY_RE`."""
    matches = []
    for field, text in checks:
        s = str(text or "")
        if not s:
            continue
        for m in user_memory.SENSITIVE_MEMORY_RE.finditer(s):
            matches.append({
                "field": field,
                "text": s[:120],
                "matched": m.group(0),
            })
    return matches


def _scan_sensitive_profile(profile):
    """Validate public_profile text fields against user_memory.SENSITIVE_MEMORY_RE."""
    if not isinstance(profile, dict):
        return []
    checks = [
        ("public_profile.summary", profile.get("summary", "")),
        ("public_profile.relationship", profile.get("relationship", "")),
    ]
    for bucket in ("facts", "inside_jokes"):
        for idx, item in enumerate(profile.get(bucket) or []):
            if isinstance(item, dict):
                checks.append((f"public_profile.{bucket}[{idx}]", item.get("text", "")))
                if item.get("category"):
                    checks.append((f"public_profile.{bucket}[{idx}].category", item.get("category", "")))
                if item.get("fact_key"):
                    checks.append((f"public_profile.{bucket}[{idx}].fact_key", item.get("fact_key", "")))
                raw_kws = item.get("keywords")
                if isinstance(raw_kws, (list, tuple)):
                    for kw in raw_kws:
                        checks.append((f"public_profile.{bucket}[{idx}].keywords", kw))
                elif isinstance(raw_kws, str) and raw_kws:
                    checks.append((f"public_profile.{bucket}[{idx}].keywords", raw_kws))
            else:
                checks.append((f"public_profile.{bucket}[{idx}]", str(item)))
    return _scan_sensitive_texts(checks)


def _scan_sensitive_group_payload(summary, items):
    """Validate group memory summary and items against user_memory.SENSITIVE_MEMORY_RE."""
    checks = [("group_memory.summary", summary)]
    for idx, item in enumerate(items or []):
        if isinstance(item, dict):
            checks.append((f"group_memory.items[{idx}]", item.get("text", "")))
            if item.get("category"):
                checks.append((f"group_memory.items[{idx}].category", item.get("category", "")))
            if item.get("fact_key"):
                checks.append((f"group_memory.items[{idx}].fact_key", item.get("fact_key", "")))
            raw_kws = item.get("keywords")
            if isinstance(raw_kws, (list, tuple)):
                for kw in raw_kws:
                    checks.append((f"group_memory.items[{idx}].keywords", kw))
            elif isinstance(raw_kws, str) and raw_kws:
                checks.append((f"group_memory.items[{idx}].keywords", raw_kws))
        else:
            checks.append((f"group_memory.items[{idx}]", str(item)))
    return _scan_sensitive_texts(checks)


def _clean_v2_item_input(item, is_joke=False, is_group=False):
    if isinstance(item, str):
        text = item.strip()[:160]
        if not text:
            return None
        return {
            "id": "",
            "text": text,
            "category": "群事件" if is_group else ("梗" if is_joke else "其他"),
            "has_category": False,
            "kind": "group_event" if is_group else ("joke" if is_joke else "stable"),
            "has_kind": False,
            "fact_key": "",
            "has_fact_key": False,
            "importance": 3,
            "has_importance": False,
            "occurred_at": "",
            "has_occurred_at": False,
            "keywords": [],
            "has_keywords": False,
            "is_joke": bool(is_joke),
        }

    if not isinstance(item, dict):
        return None

    text = str(item.get("text") or "").strip()[:160]
    if not text:
        return None

    mem_id = str(item.get("id") or "").strip()[:64]

    has_category = "category" in item and item.get("category") is not None
    raw_cat = str(item.get("category") or "").strip()[:16]
    default_cat = "群事件" if is_group else ("梗" if is_joke else "其他")
    category = raw_cat or default_cat
    if not raw_cat:
        has_category = False

    has_kind = False
    kind = "group_event" if is_group else ("joke" if is_joke else "stable")
    if not is_group and "kind" in item and item.get("kind") is not None:
        cand_kind = str(item.get("kind") or "").strip().lower()
        if cand_kind in _USER_MEMORY_KINDS:
            kind = cand_kind
            has_kind = True

    has_fact_key = "fact_key" in item and item.get("fact_key") is not None
    raw_fk = str(item.get("fact_key") or "").strip() if has_fact_key else ""

    has_importance = False
    importance = 3
    if "importance" in item and item.get("importance") is not None and str(item.get("importance")).strip() != "":
        try:
            imp_val = int(item.get("importance"))
            importance = max(1, min(5, imp_val))
            has_importance = True
        except (TypeError, ValueError):
            pass

    has_occurred_at = "occurred_at" in item and item.get("occurred_at") is not None
    occurred_at = _normalize_date_input(item.get("occurred_at")) if has_occurred_at else ""

    has_keywords = "keywords" in item and item.get("keywords") is not None
    raw_keywords = item.get("keywords") if has_keywords else []

    return {
        "id": mem_id,
        "text": text,
        "category": category,
        "has_category": has_category,
        "kind": kind,
        "has_kind": has_kind,
        "fact_key": raw_fk,
        "has_fact_key": has_fact_key,
        "importance": importance,
        "has_importance": has_importance,
        "occurred_at": occurred_at,
        "has_occurred_at": has_occurred_at,
        "keywords": raw_keywords,
        "has_keywords": has_keywords,
        "is_joke": bool(is_joke),
    }


def _clean_profile_input(raw_profile):
    out = user_memory._empty_profile()
    if not isinstance(raw_profile, dict):
        return out

    out["summary"] = str(raw_profile.get("summary") or "").strip()[:300]
    out["relationship"] = str(raw_profile.get("relationship") or "").strip()[:80]

    facts = []
    seen_f_ids = set()
    seen_f_texts = set()
    for item in raw_profile.get("facts") or []:
        cleaned = _clean_v2_item_input(item, is_joke=False, is_group=False)
        if not cleaned:
            continue
        norm_t = _norm_mem_text(cleaned["text"])
        if cleaned["id"]:
            if cleaned["id"] in seen_f_ids:
                continue
            seen_f_ids.add(cleaned["id"])
        elif norm_t in seen_f_texts:
            continue
        seen_f_texts.add(norm_t)
        facts.append(cleaned)
    out["facts"] = facts

    jokes = []
    seen_j_ids = set()
    seen_j_texts = set()
    for item in raw_profile.get("inside_jokes") or []:
        cleaned = _clean_v2_item_input(item, is_joke=True, is_group=False)
        if not cleaned:
            continue
        norm_t = _norm_mem_text(cleaned["text"])
        if cleaned["id"]:
            if cleaned["id"] in seen_j_ids or cleaned["id"] in seen_f_ids:
                continue
            seen_j_ids.add(cleaned["id"])
        elif norm_t in seen_j_texts:
            continue
        seen_j_texts.add(norm_t)
        jokes.append(cleaned)
    out["inside_jokes"] = jokes
    return out


def _diff_and_sync_scope_memories(
    conn,
    scope,
    owner_uid,
    group_id,
    chat_key,
    submitted_items,
    now,
    default_kind="stable",
    is_group=False,
    loaded_at=0,
):
    """Metadata-preserving diff for active memories in one scope, syncing FTS5 & audit logs."""
    db = user_memory.db
    if is_group:
        existing_rows = conn.execute(
            "SELECT * FROM memories WHERE scope = ? AND status = 'active'",
            (scope,),
        ).fetchall()
    else:
        existing_rows = conn.execute(
            "SELECT * FROM memories WHERE scope = ? AND owner_uid = ? AND status = 'active'",
            (scope, owner_uid),
        ).fetchall()

    existing_by_id = {str(r["id"]): r for r in existing_rows}
    matched_existing_ids = set()
    assignments = [None] * len(submitted_items)

    # Pass 1: exact ID match
    for idx, sub in enumerate(submitted_items):
        sid = sub.get("id") or ""
        if sid and sid in existing_by_id and sid not in matched_existing_ids:
            assignments[idx] = existing_by_id[sid]
            matched_existing_ids.add(sid)

    # Pass 2: fallback normalized text match (prefer same joke bucket first)
    for idx, sub in enumerate(submitted_items):
        if assignments[idx] is not None:
            continue
        norm_sub = _norm_mem_text(sub["text"])
        if not norm_sub:
            continue
        same_bucket = None
        any_bucket = None
        for eid, erow in existing_by_id.items():
            if eid in matched_existing_ids:
                continue
            if _norm_mem_text(erow["text"]) != norm_sub:
                continue
            erow_is_joke = str(erow["kind"] or "").strip().lower() == "joke"
            if erow_is_joke == bool(sub.get("is_joke")):
                same_bucket = erow
                break
            if any_bucket is None:
                any_bucket = erow
        chosen = same_bucket or any_bucket
        if chosen is not None:
            assignments[idx] = chosen
            matched_existing_ids.add(str(chosen["id"]))

    updated_cnt = 0
    created_cnt = 0
    deleted_cnt = 0
    skipped_concurrent_cnt = 0

    # Apply updates & inserts
    for idx, sub in enumerate(submitted_items):
        old = assignments[idx]
        new_text = sub["text"]
        if old is not None:
            old_id = str(old["id"])
            old_text = str(old["text"] or "")
            old_kind = str(old["kind"] or "")
            old_category = str(old["category"] or "")
            old_fk = str(old["fact_key"] or "")
            old_imp = int(old["importance"] or 3)
            old_occ = str(old["occurred_at"] or "")
            old_kws = db.json_loads_safe(old["keywords_json"], [])
            old_kws_json = db.json_dumps_compact(old_kws)
            text_changed = new_text != old_text

            if sub["has_category"]:
                new_category = sub["category"]
            else:
                new_category = old_category or (
                    "群事件" if is_group else ("梗" if sub["is_joke"] else "其他")
                )

            if is_group:
                new_kind = "group_event"
            elif sub["has_kind"]:
                new_kind = sub["kind"]
            else:
                new_kind = old_kind.strip() or (
                    "joke" if sub["is_joke"] else default_kind
                )

            if sub["has_fact_key"]:
                new_fk = user_memory.quality_gate.normalize_fact_key(sub["fact_key"])
            else:
                new_fk = old_fk

            new_imp = sub["importance"] if sub["has_importance"] else old_imp
            new_occ = sub["occurred_at"] if sub["has_occurred_at"] else old_occ

            if sub["has_keywords"]:
                new_kws = _normalize_keywords_input(sub["keywords"], fallback_text=new_text)
                new_kws_json = db.json_dumps_compact(new_kws)
            elif text_changed:
                new_kws = user_memory.tokenizer.auto_keywords(new_text, max_keywords=8)
                new_kws_json = db.json_dumps_compact(new_kws)
            else:
                new_kws = old_kws
                new_kws_json = old_kws_json

            # Skip UPDATE and FTS rewrite completely if nothing changed (do NOT bump updated_at)
            if (
                new_text == old_text
                and new_kind == old_kind
                and new_category == old_category
                and new_fk == old_fk
                and new_imp == old_imp
                and new_occ == old_occ
                and new_kws_json == old_kws_json
            ):
                continue

            # Preserve proof_count, created_at, history_json, source_window_id,
            # last_mentioned_ts, last_recalled_ts; invalidate stale embedding only if text changed.
            if text_changed:
                conn.execute(
                    "UPDATE memories SET kind = ?, category = ?, fact_key = ?, text = ?, "
                    "keywords_json = ?, importance = ?, occurred_at = ?, embedding = NULL, "
                    "updated_at = ? WHERE id = ?",
                    (
                        new_kind,
                        new_category,
                        new_fk,
                        new_text,
                        new_kws_json,
                        new_imp,
                        new_occ,
                        now,
                        old_id,
                    ),
                )
            else:
                conn.execute(
                    "UPDATE memories SET kind = ?, category = ?, fact_key = ?, text = ?, "
                    "keywords_json = ?, importance = ?, occurred_at = ?, updated_at = ? "
                    "WHERE id = ?",
                    (
                        new_kind,
                        new_category,
                        new_fk,
                        new_text,
                        new_kws_json,
                        new_imp,
                        new_occ,
                        now,
                        old_id,
                    ),
                )

            db.sync_memory_fts(
                conn,
                mem_id=old_id,
                scope=scope,
                owner_uid=str(old["owner_uid"] or owner_uid),
                group_id=str(old["group_id"] or group_id),
                text=new_text,
                keywords=new_kws,
            )
            updated_cnt += 1
        else:
            new_id = db.new_memory_id()
            new_kind = (
                "group_event"
                if is_group
                else (sub["kind"] if sub["has_kind"] else ("joke" if sub["is_joke"] else default_kind))
            )
            new_category = (
                sub["category"]
                if sub["has_category"]
                else ("群事件" if is_group else ("梗" if sub["is_joke"] else "其他"))
            )
            new_fk = (
                user_memory.quality_gate.normalize_fact_key(sub["fact_key"])
                if sub["has_fact_key"]
                else ""
            )
            new_imp = sub["importance"] if sub["has_importance"] else 3
            new_occ = sub["occurred_at"] if sub["has_occurred_at"] else ""
            if sub["has_keywords"]:
                new_kws = _normalize_keywords_input(sub["keywords"], fallback_text=new_text)
            else:
                new_kws = user_memory.tokenizer.auto_keywords(new_text, max_keywords=8)
            new_kws_json = db.json_dumps_compact(new_kws)
            hist_json = db.json_dumps_compact([{"action": "webui_create", "ts": now}])

            conn.execute(
                "INSERT INTO memories("
                "id, scope, owner_uid, group_id, kind, category, fact_key, text, "
                "keywords_json, importance, proof_count, occurred_at, "
                "last_mentioned_ts, last_recalled_ts, status, superseded_by, "
                "history_json, source_window_id, embedding, created_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, 0, 'active', '', ?, '', NULL, ?, ?)",
                (
                    new_id,
                    scope,
                    owner_uid,
                    group_id,
                    new_kind,
                    new_category,
                    new_fk,
                    new_text,
                    new_kws_json,
                    new_imp,
                    new_occ,
                    now,
                    hist_json,
                    now,
                    now,
                ),
            )
            db.sync_memory_fts(
                conn,
                mem_id=new_id,
                scope=scope,
                owner_uid=owner_uid,
                group_id=group_id,
                text=new_text,
                keywords=new_kws,
            )
            created_cnt += 1

    # Soft-archive previously active rows genuinely omitted from submission
    loaded_ts = int(loaded_at or 0)
    for old_id, old_row in existing_by_id.items():
        if old_id in matched_existing_ids:
            continue
        if loaded_ts > 0 and int(old_row["created_at"] or 0) > loaded_ts:
            # Newly distilled after the edit form was loaded — avoid lost-update deletion
            skipped_concurrent_cnt += 1
            continue
        new_hist = _append_mem_history(
            old_row["history_json"],
            {"action": "webui_archive", "ts": now},
        )
        conn.execute(
            "UPDATE memories SET status = 'archived', history_json = ?, updated_at = ? "
            "WHERE id = ?",
            (new_hist, now, old_id),
        )
        db.sync_memory_fts(
            conn, old_id, scope, str(old_row["owner_uid"] or ""),
            str(old_row["group_id"] or ""), old_row["text"], old_row["keywords_json"],
        )
        db.log_audit(
            conn,
            run_type="webui",
            chat_key=chat_key,
            action="archive_item",
            target_id=old_id,
            detail={
                "scope": scope,
                "owner_uid": owner_uid,
                "group_id": group_id,
                "text": str(old_row["text"] or "")[:120],
            },
            created_at=now,
        )
        deleted_cnt += 1

    return {
        "updated": updated_cnt,
        "created": created_cnt,
        "deleted": deleted_cnt,
        "archived": deleted_cnt,
        "skipped_concurrent": skipped_concurrent_cnt,
    }


def _profile_has_any_content(prof):
    if not isinstance(prof, dict):
        return False
    return bool(
        (prof.get("summary") or "").strip()
        or (prof.get("relationship") or "").strip()
        or (prof.get("facts") or [])
        or (prof.get("inside_jokes") or [])
    )


def get_memory_summary_stats():
    """Fast SQLite aggregate stats for /api/overview (avoids full list_memories scan)."""
    env = _resolve_parse_bot_env()
    db_path = _resolve_memory_db_path(env)
    db = user_memory.db

    stats = {
        "total_users": 0,
        "with_public": 0,
        "with_dm": 0,
        "total_facts": 0,
        "total_jokes": 0,
        "active_memories": 0,
        "pending_queue": 0,
        "group_memories_count": 0,
        "groups_count": 0,
        "conflicts_count": 0,
        "raw_messages_count": 0,
    }

    with db.read_conn(db_path) as conn:
        row = conn.execute(
            """
            SELECT
                (SELECT COUNT(DISTINCT uid) FROM (
                    SELECT entity_id AS uid FROM profiles
                     WHERE (scope = 'user_public' OR scope LIKE 'dm:%')
                       AND entity_id != '' AND entity_id != 'bot'
                    UNION
                    SELECT owner_uid AS uid FROM memories
                     WHERE status = 'active' AND owner_uid != '' AND owner_uid != 'bot'
                       AND (scope = 'user_public' OR scope LIKE 'dm:%')
                    UNION
                    SELECT sender_uid AS uid FROM raw_messages
                     WHERE role = 'user' AND distilled = 0 AND chat_key LIKE 'g:%'
                       AND sender_uid != '' AND sender_uid != 'bot'
                    UNION
                    SELECT SUBSTR(chat_key, 4) AS uid FROM raw_messages
                     WHERE role = 'user' AND distilled = 0 AND chat_key LIKE 'dm:%'
                       AND LENGTH(chat_key) > 3 AND SUBSTR(chat_key, 4) != 'bot'
                )) AS total_users,
                (SELECT COUNT(DISTINCT uid) FROM (
                    SELECT entity_id AS uid FROM profiles
                     WHERE scope = 'user_public' AND entity_id != '' AND entity_id != 'bot'
                       AND (TRIM(summary) != '' OR TRIM(relationship) != '')
                    UNION
                    SELECT owner_uid AS uid FROM memories
                     WHERE status = 'active' AND scope = 'user_public'
                       AND owner_uid != '' AND owner_uid != 'bot'
                )) AS with_public,
                (SELECT COUNT(DISTINCT uid) FROM (
                    SELECT entity_id AS uid FROM profiles
                     WHERE scope LIKE 'dm:%' AND entity_id != '' AND entity_id != 'bot'
                       AND (TRIM(summary) != '' OR TRIM(relationship) != '')
                    UNION
                    SELECT owner_uid AS uid FROM memories
                     WHERE status = 'active' AND scope LIKE 'dm:%'
                       AND owner_uid != '' AND owner_uid != 'bot'
                )) AS with_dm,
                (SELECT COUNT(*) FROM memories
                  WHERE status = 'active' AND owner_uid != '' AND owner_uid != 'bot'
                    AND (scope = 'user_public' OR scope LIKE 'dm:%')
                    AND LOWER(kind) != 'joke') AS total_facts,
                (SELECT COUNT(*) FROM memories
                  WHERE status = 'active' AND owner_uid != '' AND owner_uid != 'bot'
                    AND (scope = 'user_public' OR scope LIKE 'dm:%')
                    AND LOWER(kind) = 'joke') AS total_jokes,
                (SELECT COUNT(*) FROM memories
                  WHERE status = 'active') AS active_memories,
                (SELECT COUNT(*) FROM memories
                  WHERE status = 'active' AND scope LIKE 'group:%') AS group_memories_count,
                (SELECT COUNT(DISTINCT entity_id) FROM profiles
                  WHERE scope LIKE 'group:%' AND entity_id != '') AS groups_count,
                (SELECT COUNT(*) FROM raw_messages
                  WHERE role = 'user' AND distilled = 0
                    AND ((chat_key LIKE 'g:%' AND sender_uid != '' AND sender_uid != 'bot')
                      OR (chat_key LIKE 'dm:%' AND LENGTH(chat_key) > 3 AND SUBSTR(chat_key, 4) != 'bot'))
                ) AS pending_queue,
                (SELECT COUNT(*) FROM raw_messages) AS raw_messages_count
            """
        ).fetchone()
        if row is not None:
            for k in (
                "total_users",
                "with_public",
                "with_dm",
                "total_facts",
                "total_jokes",
                "active_memories",
                "group_memories_count",
                "groups_count",
                "pending_queue",
                "raw_messages_count",
            ):
                stats[k] = int(row[k] or 0)
        stats["conflicts_count"] = _count_conflict_pairs(conn)

    plugin_cfg = bot_plugins.load_config(force=False)
    plugin_flags = {
        "memory_recall": bot_plugins.is_enabled("memory_recall", config=plugin_cfg),
        "memory_continuity": bot_plugins.is_enabled("memory_continuity", config=plugin_cfg),
        "memory_extraction": bot_plugins.is_enabled("memory_extraction", config=plugin_cfg),
        "memory_sleep": bot_plugins.is_enabled("memory_sleep", config=plugin_cfg),
    }

    return {
        "stats": stats,
        "memory_enabled": _bool_flag(env.get("USER_MEMORY_ENABLED", "1"), True),
        "engine_version": 2,
        "plugin_flags": plugin_flags,
    }


def list_memories(q="", filter_mode="all", group_id="", sort_by="last_seen", group_map=None):
    env = _resolve_parse_bot_env()
    db_path = _resolve_memory_db_path(env)
    admin_ids = set(_split_csv(env.get("ADMIN_QQ_IDS", "")))
    group_map = dict(group_map or {})
    db = user_memory.db

    stats = {
        "total_users": 0,
        "with_public": 0,
        "with_dm": 0,
        "total_facts": 0,
        "total_jokes": 0,
        "pending_queue": 0,
        "group_memories_count": 0,
        "conflicts_count": 0,
        "raw_messages_count": 0,
    }
    all_groups = set()
    items = []

    q_low = (q or "").strip().lower()
    gid_filter = str(group_id or "").strip()

    users_map = {}

    def _ensure_user(uid):
        entry = users_map.get(uid)
        if entry is None:
            entry = {
                "user_id": uid,
                "nicknames": [],
                "seen_groups": [],
                "first_ts": [],
                "last_ts": [],
                "msg_count": 0,
                "pub_summary": "",
                "pub_relationship": "",
                "pub_facts": 0,
                "pub_jokes": 0,
                "dm_summary": "",
                "dm_relationship": "",
                "dm_facts": 0,
                "dm_jokes": 0,
                "pend_pub": 0,
                "pend_dm": 0,
                "search_parts": [],
            }
            users_map[uid] = entry
        return entry

    with db.read_conn(db_path) as conn:
        prof_rows = conn.execute(
            "SELECT profile_key, scope, entity_id, nicknames_json, seen_groups_json, "
            "relationship, summary, msg_count, first_seen_ts, last_seen_ts "
            "FROM profiles "
            "WHERE scope = 'user_public' OR scope LIKE 'dm:%' "
            "ORDER BY updated_at ASC"
        ).fetchall()

        for row in prof_rows:
            scope = str(row["scope"] or "")
            uid = db.sanitize_uid(row["entity_id"] or (scope[3:] if scope.startswith("dm:") else ""))
            if not uid or uid == "bot":
                continue
            u = _ensure_user(uid)
            for n in db.json_loads_safe(row["nicknames_json"], []):
                ns = str(n).strip()
                if ns and ns not in u["nicknames"]:
                    u["nicknames"].append(ns)
            for g in db.json_loads_safe(row["seen_groups_json"], []):
                gs = db.sanitize_uid(g)
                if gs and gs not in u["seen_groups"]:
                    u["seen_groups"].append(gs)
            u["msg_count"] += int(row["msg_count"] or 0)
            fts = int(row["first_seen_ts"] or 0)
            lts = int(row["last_seen_ts"] or 0)
            if fts > 0:
                u["first_ts"].append(fts)
            if lts > 0:
                u["last_ts"].append(lts)

            summ = str(row["summary"] or "").strip()
            rel = str(row["relationship"] or "").strip()
            if scope == "user_public":
                u["pub_summary"] = summ
                u["pub_relationship"] = rel
            elif scope.startswith("dm:"):
                u["dm_summary"] = summ
                u["dm_relationship"] = rel
            if summ:
                u["search_parts"].append(summ)
            if rel:
                u["search_parts"].append(rel)

        mem_rows = conn.execute(
            "SELECT scope, owner_uid, kind, category, fact_key, text, keywords_json, "
            "created_at, updated_at "
            "FROM memories "
            "WHERE status = 'active' AND owner_uid != '' "
            "  AND (scope = 'user_public' OR scope LIKE 'dm:%')"
        ).fetchall()

        for row in mem_rows:
            uid = db.sanitize_uid(row["owner_uid"])
            if not uid or uid == "bot":
                continue
            u = _ensure_user(uid)
            scope = str(row["scope"] or "")
            kind = str(row["kind"] or "stable").strip().lower()
            if scope == "user_public":
                if kind == "joke":
                    u["pub_jokes"] += 1
                else:
                    u["pub_facts"] += 1
            elif scope.startswith("dm:"):
                if kind == "joke":
                    u["dm_jokes"] += 1
                else:
                    u["dm_facts"] += 1
            cts = int(row["created_at"] or 0)
            uts = int(row["updated_at"] or 0)
            if cts > 0:
                u["first_ts"].append(cts)
            if uts > 0:
                u["last_ts"].append(uts)
            if q_low:
                u["search_parts"].append(str(row["text"] or ""))
                u["search_parts"].append(str(row["category"] or ""))
                fk = str(row["fact_key"] or "")
                if fk:
                    u["search_parts"].append(fk)
                for kw in db.json_loads_safe(row["keywords_json"], []):
                    ks = str(kw).strip()
                    if ks:
                        u["search_parts"].append(ks)

        pend_pub_rows = conn.execute(
            "SELECT sender_uid, group_id, MAX(sender_name) AS sname, "
            "COUNT(*) AS cnt, MIN(ts) AS min_ts, MAX(ts) AS max_ts "
            "FROM raw_messages "
            "WHERE role = 'user' AND distilled = 0 AND chat_key LIKE 'g:%' AND sender_uid != '' "
            "GROUP BY sender_uid, group_id"
        ).fetchall()

        for row in pend_pub_rows:
            uid = db.sanitize_uid(row["sender_uid"])
            if not uid or uid == "bot":
                continue
            u = _ensure_user(uid)
            u["pend_pub"] += int(row["cnt"] or 0)
            gid = db.sanitize_uid(row["group_id"])
            if gid and gid not in u["seen_groups"]:
                u["seen_groups"].append(gid)
            sname = str(row["sname"] or "").strip()
            if sname and sname not in u["nicknames"]:
                u["nicknames"].append(sname)
            if row["min_ts"]:
                u["first_ts"].append(int(row["min_ts"]))
            if row["max_ts"]:
                u["last_ts"].append(int(row["max_ts"]))

        pend_dm_rows = conn.execute(
            "SELECT chat_key, MAX(sender_name) AS sname, "
            "COUNT(*) AS cnt, MIN(ts) AS min_ts, MAX(ts) AS max_ts "
            "FROM raw_messages "
            "WHERE role = 'user' AND distilled = 0 AND chat_key LIKE 'dm:%' "
            "GROUP BY chat_key"
        ).fetchall()

        for row in pend_dm_rows:
            ck = str(row["chat_key"] or "")
            uid = db.sanitize_uid(ck[3:] if ck.startswith("dm:") else "")
            if not uid or uid == "bot":
                continue
            u = _ensure_user(uid)
            u["pend_dm"] += int(row["cnt"] or 0)
            sname = str(row["sname"] or "").strip()
            if sname and sname not in u["nicknames"]:
                u["nicknames"].append(sname)
            if row["min_ts"]:
                u["first_ts"].append(int(row["min_ts"]))
            if row["max_ts"]:
                u["last_ts"].append(int(row["max_ts"]))

        grp_cnt_row = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE scope LIKE 'group:%' AND status = 'active'"
        ).fetchone()
        stats["group_memories_count"] = int(grp_cnt_row[0] or 0) if grp_cnt_row else 0

        raw_cnt_row = conn.execute("SELECT COUNT(*) FROM raw_messages").fetchone()
        stats["raw_messages_count"] = int(raw_cnt_row[0] or 0) if raw_cnt_row else 0

        stats["conflicts_count"] = _count_conflict_pairs(conn)

        grp_prof_rows = conn.execute(
            "SELECT entity_id FROM profiles WHERE scope LIKE 'group:%'"
        ).fetchall()
        for gr in grp_prof_rows:
            gid = db.sanitize_uid(gr["entity_id"])
            if gid:
                all_groups.add(gid)

    for uid, u in users_map.items():
        has_pub = bool(u["pub_summary"] or u["pub_relationship"] or u["pub_facts"] or u["pub_jokes"])
        has_dm = bool(u["dm_summary"] or u["dm_relationship"] or u["dm_facts"] or u["dm_jokes"])
        pend_pub = u["pend_pub"]
        pend_dm = u["pend_dm"]
        pend_total = pend_pub + pend_dm
        seen_groups = u["seen_groups"]

        stats["total_users"] += 1
        if has_pub:
            stats["with_public"] += 1
        if has_dm:
            stats["with_dm"] += 1
        stats["total_facts"] += u["pub_facts"] + u["dm_facts"]
        stats["total_jokes"] += u["pub_jokes"] + u["dm_jokes"]
        stats["pending_queue"] += pend_total
        all_groups.update(seen_groups)

        if filter_mode == "has_memory" and not (has_pub or has_dm):
            continue
        if filter_mode == "has_dm" and not has_dm:
            continue
        if filter_mode == "has_pending" and pend_total <= 0:
            continue
        if gid_filter and gid_filter not in seen_groups:
            continue

        nicks = u["nicknames"]
        display_name = nicks[-1] if nicks else uid

        if q_low:
            haystack = " ".join(
                [uid.lower(), " ".join(n.lower() for n in nicks)]
                + [p.lower() for p in u["search_parts"] if p]
            )
            if q_low not in haystack:
                continue

        first_seen = db.now_str(min(u["first_ts"])) if u["first_ts"] else ""
        last_seen = db.now_str(max(u["last_ts"])) if u["last_ts"] else ""

        items.append({
            "user_id": uid,
            "display_name": display_name,
            "nicknames": nicks,
            "seen_groups": seen_groups,
            "first_seen": first_seen,
            "last_seen": last_seen,
            "msg_count": u["msg_count"],
            "is_admin": uid in admin_ids,
            "has_public": has_pub,
            "has_dm": has_dm,
            "public_summary": u["pub_summary"],
            "public_relationship": u["pub_relationship"],
            "public_facts_count": u["pub_facts"],
            "public_jokes_count": u["pub_jokes"],
            "dm_summary": u["dm_summary"],
            "dm_relationship": u["dm_relationship"],
            "dm_facts_count": u["dm_facts"],
            "dm_jokes_count": u["dm_jokes"],
            "facts_count": u["pub_facts"] + u["dm_facts"],
            "jokes_count": u["pub_jokes"] + u["dm_jokes"],
            "pending_public_count": pend_pub,
            "pending_dm_count": pend_dm,
            "pending_total": pend_total,
        })

    if sort_by == "msg_count":
        items.sort(key=lambda x: (x["msg_count"], x["last_seen"]), reverse=True)
    elif sort_by == "facts":
        items.sort(key=lambda x: (x["facts_count"] + x["jokes_count"], x["msg_count"]), reverse=True)
    else:
        items.sort(key=lambda x: (x["last_seen"], x["msg_count"]), reverse=True)

    groups_dict = {}
    for gid in sorted(all_groups):
        groups_dict[gid] = group_map.get(gid) or f"QQ 群 {gid}"
    for gid, gname in group_map.items():
        groups_dict.setdefault(str(gid), gname)

    plugin_cfg = bot_plugins.load_config(force=True)
    plugin_flags = {
        "memory_recall": bot_plugins.is_enabled("memory_recall", config=plugin_cfg),
        "memory_continuity": bot_plugins.is_enabled("memory_continuity", config=plugin_cfg),
        "memory_extraction": bot_plugins.is_enabled("memory_extraction", config=plugin_cfg),
        "memory_sleep": bot_plugins.is_enabled("memory_sleep", config=plugin_cfg),
    }

    return {
        "stats": stats,
        "users": items,
        "total_filtered": len(items),
        "groups": groups_dict,
        "memory_enabled": _bool_flag(env.get("USER_MEMORY_ENABLED", "1"), True),
        "engine_version": 2,
        "plugin_flags": plugin_flags,
    }


def _memory_user_map():
    """Lightweight user_id -> display_name lookup for enriching sessions & analytics."""
    db_path = _resolve_memory_db_path()
    db = user_memory.db
    out = {}
    try:
        with db.read_conn(db_path) as conn:
            rows = conn.execute(
                "SELECT entity_id, nicknames_json "
                "FROM profiles "
                "WHERE scope = 'user_public' OR scope LIKE 'dm:%' "
                "ORDER BY updated_at ASC, CASE WHEN scope = 'user_public' THEN 1 ELSE 0 END"
            ).fetchall()
    except Exception:
        return out
    for row in rows:
        uid = str(row["entity_id"] or "").strip()
        if not uid:
            continue
        nicks = [
            str(n).strip()
            for n in db.json_loads_safe(row["nicknames_json"], [])
            if str(n).strip()
        ]
        if nicks:
            out[uid] = nicks[-1]
    return out


def get_memory(user_id, group_map=None):
    uid = user_memory._sanitize_uid(user_id)
    if not uid:
        raise StoreError("无效的用户 QQ 号")

    env = _resolve_parse_bot_env()
    db_path = _resolve_memory_db_path(env)
    admin_ids = set(_split_csv(env.get("ADMIN_QQ_IDS", "")))
    db = user_memory.db
    dm_scope = f"dm:{uid}"

    with db.read_conn(db_path) as conn:
        pub_prof = conn.execute(
            "SELECT * FROM profiles WHERE profile_key = ?",
            (f"user_public:{uid}",),
        ).fetchone()
        dm_prof = conn.execute(
            "SELECT * FROM profiles WHERE profile_key = ?",
            (dm_scope,),
        ).fetchone()
        public_mems = _scope_active_memories(conn, "user_public", uid)
        dm_mems = _scope_active_memories(conn, dm_scope, uid)
        active_mems = public_mems + dm_mems
        archived_mems = conn.execute(
            "SELECT * FROM memories WHERE owner_uid = ? AND scope IN ('user_public', ?) "
            "AND status != 'active' ORDER BY updated_at DESC LIMIT 100",
            (uid, dm_scope),
        ).fetchall()
        pending_pub = conn.execute(
            "SELECT id, group_id, sender_name, text, ts FROM raw_messages "
            "WHERE sender_uid = ? AND role = 'user' AND distilled = 0 AND chat_key LIKE 'g:%' "
            "ORDER BY id ASC LIMIT 50",
            (uid,),
        ).fetchall()
        pending_dm = conn.execute(
            "SELECT id, sender_name, text, ts FROM raw_messages "
            "WHERE chat_key = ? AND role = 'user' AND distilled = 0 "
            "ORDER BY id ASC LIMIT 50",
            (dm_scope,),
        ).fetchall()
        rel_rows = conn.execute(
            "SELECT id, scope, group_id, src_type, src_id, rel_type, dst_type, dst_id, "
            "note, proof_count, updated_at "
            "FROM relations WHERE src_id = ? OR dst_id = ? "
            "ORDER BY proof_count DESC, updated_at DESC LIMIT 50",
            (uid, uid),
        ).fetchall()

        has_any_raw = False
        if not (pub_prof or dm_prof or active_mems or archived_mems or pending_pub or pending_dm):
            raw_probe = conn.execute(
                "SELECT 1 FROM raw_messages "
                "WHERE (sender_uid = ? AND distilled = 0) OR chat_key = ? LIMIT 1",
                (uid, dm_scope),
            ).fetchone()
            has_any_raw = raw_probe is not None

        if not (
            pub_prof
            or dm_prof
            or active_mems
            or archived_mems
            or pending_pub
            or pending_dm
            or has_any_raw
        ):
            raise NotFoundError(f"找不到用户 {uid} 的记忆档案")

        last_dm_cons_raw = db.get_meta(conn, f"last_consolidated:{dm_scope}", "0")

        # Resolve display names for relation endpoints
        rel_uids = set()
        for r in rel_rows:
            if str(r["src_type"] or "user") == "user" and r["src_id"]:
                rel_uids.add(str(r["src_id"]))
            if str(r["dst_type"] or "user") == "user" and r["dst_id"]:
                rel_uids.add(str(r["dst_id"]))
        rel_name_map = {}
        if rel_uids:
            ph = ",".join("?" for _ in rel_uids)
            nr_rows = conn.execute(
                f"SELECT entity_id, nicknames_json FROM profiles "
                f"WHERE (scope = 'user_public' OR scope LIKE 'dm:%') AND entity_id IN ({ph}) "
                f"ORDER BY updated_at ASC",
                tuple(rel_uids),
            ).fetchall()
            for nr in nr_rows:
                eid = str(nr["entity_id"] or "")
                ns = [str(x).strip() for x in db.json_loads_safe(nr["nicknames_json"], []) if str(x).strip()]
                if eid and ns:
                    rel_name_map[eid] = ns[-1]

    rec = user_memory._empty_record(uid)
    rec["public_nicknames"] = db.json_loads_safe(pub_prof["nicknames_json"], []) if pub_prof else []
    rec["dm_nicknames"] = db.json_loads_safe(dm_prof["nicknames_json"], []) if dm_prof else []
    nicks = []
    seen_groups = []
    first_candidates = []
    last_candidates = []
    msg_count = 0

    for prof in (pub_prof, dm_prof):
        if not prof:
            continue
        for n in db.json_loads_safe(prof["nicknames_json"], []):
            s = str(n).strip()
            if s and s not in nicks:
                nicks.append(s)
        for g in db.json_loads_safe(prof["seen_groups_json"], []):
            gs = db.sanitize_uid(g)
            if gs and gs not in seen_groups:
                seen_groups.append(gs)
        msg_count += int(prof["msg_count"] or 0)
        if prof["first_seen_ts"]:
            first_candidates.append(int(prof["first_seen_ts"]))
        if prof["last_seen_ts"]:
            last_candidates.append(int(prof["last_seen_ts"]))

    for r in pending_pub:
        gs = db.sanitize_uid(r["group_id"])
        if gs and gs not in seen_groups:
            seen_groups.append(gs)
        sn = str(r["sender_name"] or "").strip()
        if sn and sn not in nicks:
            nicks.append(sn)
        if r["ts"]:
            first_candidates.append(int(r["ts"]))
            last_candidates.append(int(r["ts"]))

    for r in pending_dm:
        sn = str(r["sender_name"] or "").strip()
        if sn and sn not in nicks:
            nicks.append(sn)
        if r["ts"]:
            first_candidates.append(int(r["ts"]))
            last_candidates.append(int(r["ts"]))

    rec["nicknames"] = nicks
    rec["seen_groups"] = seen_groups
    rec["msg_count"] = msg_count
    if first_candidates:
        rec["first_seen"] = db.now_str(min(first_candidates))
    if last_candidates:
        rec["last_seen"] = db.now_str(max(last_candidates))
    try:
        rec["last_consolidated_dm_at"] = float(last_dm_cons_raw or 0.0)
    except (ValueError, TypeError):
        rec["last_consolidated_dm_at"] = 0.0

    if pub_prof:
        rec["public_profile"]["summary"] = str(pub_prof["summary"] or "")
        rec["public_profile"]["relationship"] = str(pub_prof["relationship"] or "")
    if dm_prof:
        rec["dm_profile"]["summary"] = str(dm_prof["summary"] or "")
        rec["dm_profile"]["relationship"] = str(dm_prof["relationship"] or "")

    for m in active_mems:
        target = rec["public_profile"] if m["scope"] == "user_public" else rec["dm_profile"]
        item = _format_memory_item(m)
        if item["kind"] == "joke":
            target["inside_jokes"].append(item)
        else:
            target["facts"].append(item)

    rec["pending_public_msgs"] = [
        {
            "id": str(r["id"]),
            "ts": db.now_str(r["ts"]),
            "name": str(r["sender_name"] or uid),
            "text": str(r["text"]),
            "group": str(r["group_id"] or ""),
        }
        for r in pending_pub
    ]
    rec["pending_dm_msgs"] = [
        {
            "id": str(r["id"]),
            "ts": db.now_str(r["ts"]),
            "name": str(r["sender_name"] or uid),
            "text": str(r["text"]),
        }
        for r in pending_dm
    ]
    rec["archived_items"] = [_format_archived_item(m) for m in archived_mems]
    rec["relations"] = [
        {
            "id": int(r["id"]),
            "scope": str(r["scope"] or ""),
            "group_id": str(r["group_id"] or ""),
            "src_id": str(r["src_id"] or ""),
            "src_name": rel_name_map.get(str(r["src_id"] or ""), str(r["src_id"] or "")),
            "rel_type": str(r["rel_type"] or ""),
            "dst_id": str(r["dst_id"] or ""),
            "dst_name": rel_name_map.get(str(r["dst_id"] or ""), str(r["dst_id"] or "")),
            "note": str(r["note"] or ""),
            "proof_count": int(r["proof_count"] or 1),
            "updated_at": db.now_str(int(r["updated_at"] or 0)),
        }
        for r in rel_rows
    ]

    display_name = nicks[-1] if nicks else uid

    preview_group = user_memory.retrieve.build_profile_preview_block(
        user_id=uid,
        sender_name=rec["public_nicknames"][-1] if rec["public_nicknames"] else uid,
        is_group=True,
        admin_ids=admin_ids,
        db_path=db_path,
    )
    preview_dm = user_memory.retrieve.build_profile_preview_block(
        user_id=uid,
        sender_name=display_name,
        is_group=False,
        admin_ids=admin_ids,
        db_path=db_path,
    )

    gmap = dict(group_map or {})
    group_names = {
        str(gid): gmap.get(str(gid)) or f"QQ 群 {gid}"
        for gid in seen_groups
    }

    return {
        "record": rec,
        "display_name": display_name,
        "is_admin": uid in admin_ids,
        "preview_group_prompt": preview_group,
        "preview_dm_prompt": preview_dm,
        "group_names": group_names,
        "loaded_at": db.now_ts(),
        "revisions": {
            "public": _memory_revision(pub_prof, public_mems),
            "dm": _memory_revision(dm_prof, dm_mems),
        },
        "previews": {
            "group": preview_group,
            "dm": preview_dm,
        },
    }


def update_memory(user_id, payload, group_map=None):
    uid = user_memory._sanitize_uid(user_id)
    if not uid:
        raise StoreError("无效的用户 QQ 号")
    if not isinstance(payload, dict):
        raise StoreError("请求体必须是 JSON 对象")

    if "public_profile" in payload:
        pub_clean = _clean_profile_input(payload["public_profile"])
        matches = _scan_sensitive_profile(pub_clean)
        if matches:
            raise SensitiveMemoryError(matches)
    else:
        pub_clean = None

    dm_clean = _clean_profile_input(payload["dm_profile"]) if "dm_profile" in payload else None

    public_nicknames = _clean_nicknames(payload.get("public_nicknames"))
    dm_nicknames = _clean_nicknames(payload.get("dm_nicknames"))
    legacy_nicknames = _clean_nicknames(payload.get("nicknames"))
    revisions = payload.get("revisions", {})
    if not isinstance(revisions, dict):
        raise StoreError("无效的记忆档案版本")

    env = _resolve_parse_bot_env()
    db_path = _resolve_memory_db_path(env)
    db = user_memory.db
    now = db.now_ts()
    loaded_at = int(db.parse_ts(payload.get("loaded_at"), 0) or 0)
    pub_pkey = f"user_public:{uid}"
    dm_scope = f"dm:{uid}"
    audit_detail = {}

    with db.write_tx(db_path) as conn:
        if pub_clean is not None or public_nicknames is not None or legacy_nicknames is not None:
            _check_memory_revision(conn, revisions.get("public"), "user_public", pub_pkey, uid)
        if dm_clean is not None or dm_nicknames is not None:
            _check_memory_revision(conn, revisions.get("dm"), dm_scope, dm_scope, uid)

        # Older clients submit the combined display list. Keep known DM-only aliases private.
        if public_nicknames is None and legacy_nicknames is not None:
            nick_rows = conn.execute(
                "SELECT scope, nicknames_json FROM profiles WHERE profile_key IN (?, ?)",
                (pub_pkey, dm_scope),
            )
            stored = {
                row["scope"]: {str(name).strip() for name in db.json_loads_safe(row["nicknames_json"], [])}
                for row in nick_rows
            }
            known_public = stored.get("user_public", set())
            known_private = stored.get(dm_scope, set())
            known_private.update(
                row[0].strip() for row in conn.execute(
                    "SELECT DISTINCT sender_name FROM raw_messages WHERE chat_key = ? AND role = 'user'",
                    (dm_scope,),
                )
            )
            private_only = known_private - known_public
            public_nicknames = [name for name in legacy_nicknames if name not in private_only]
        matches = _scan_sensitive_texts(("public_nicknames", name) for name in public_nicknames or [])
        if matches:
            raise SensitiveMemoryError(matches)

        has_prof_upd_col = _table_has_column(conn, "profiles", "profile_updated_at")
        pub_exists = (
            conn.execute("SELECT 1 FROM profiles WHERE profile_key = ?", (pub_pkey,)).fetchone()
            is not None
        )
        dm_exists = (
            conn.execute("SELECT 1 FROM profiles WHERE profile_key = ?", (dm_scope,)).fetchone()
            is not None
        )

        if pub_clean is not None:
            if pub_exists:
                if has_prof_upd_col:
                    conn.execute(
                        "UPDATE profiles SET summary = ?, relationship = ?, "
                        "profile_updated_at = ?, updated_at = ? "
                        "WHERE profile_key = ?",
                        (pub_clean["summary"], pub_clean["relationship"], now, now, pub_pkey),
                    )
                else:
                    conn.execute(
                        "UPDATE profiles SET summary = ?, relationship = ?, updated_at = ? "
                        "WHERE profile_key = ?",
                        (pub_clean["summary"], pub_clean["relationship"], now, pub_pkey),
                    )
            elif _profile_has_any_content(pub_clean):
                if has_prof_upd_col:
                    conn.execute(
                        "INSERT INTO profiles("
                        "profile_key, scope, entity_id, nicknames_json, seen_groups_json, "
                        "relationship, summary, msg_count, first_seen_ts, last_seen_ts, "
                        "profile_updated_at, updated_at"
                        ") VALUES (?, 'user_public', ?, ?, '[]', ?, ?, 0, ?, ?, ?, ?)",
                        (
                            pub_pkey,
                            uid,
                            db.json_dumps_compact(public_nicknames or []),
                            pub_clean["relationship"],
                            pub_clean["summary"],
                            now,
                            now,
                            now,
                            now,
                        ),
                    )
                else:
                    conn.execute(
                        "INSERT INTO profiles("
                        "profile_key, scope, entity_id, nicknames_json, seen_groups_json, "
                        "relationship, summary, msg_count, first_seen_ts, last_seen_ts, updated_at"
                        ") VALUES (?, 'user_public', ?, ?, '[]', ?, ?, 0, ?, ?, ?)",
                        (
                            pub_pkey,
                            uid,
                            db.json_dumps_compact(public_nicknames or []),
                            pub_clean["relationship"],
                            pub_clean["summary"],
                            now,
                            now,
                            now,
                        ),
                    )
                pub_exists = True

            pub_items = list(pub_clean["facts"]) + list(pub_clean["inside_jokes"])
            audit_detail["public_diff"] = _diff_and_sync_scope_memories(
                conn,
                scope="user_public",
                owner_uid=uid,
                group_id="",
                chat_key="",
                submitted_items=pub_items,
                now=now,
                default_kind="stable",
                is_group=False,
                loaded_at=loaded_at,
            )

        if dm_clean is not None:
            if dm_exists:
                if has_prof_upd_col:
                    conn.execute(
                        "UPDATE profiles SET summary = ?, relationship = ?, "
                        "profile_updated_at = ?, updated_at = ? "
                        "WHERE profile_key = ?",
                        (dm_clean["summary"], dm_clean["relationship"], now, now, dm_scope),
                    )
                else:
                    conn.execute(
                        "UPDATE profiles SET summary = ?, relationship = ?, updated_at = ? "
                        "WHERE profile_key = ?",
                        (dm_clean["summary"], dm_clean["relationship"], now, dm_scope),
                    )
            elif _profile_has_any_content(dm_clean):
                if has_prof_upd_col:
                    conn.execute(
                        "INSERT INTO profiles("
                        "profile_key, scope, entity_id, nicknames_json, seen_groups_json, "
                        "relationship, summary, msg_count, first_seen_ts, last_seen_ts, "
                        "profile_updated_at, updated_at"
                        ") VALUES (?, ?, ?, ?, '[]', ?, ?, 0, ?, ?, ?, ?)",
                        (
                            dm_scope,
                            dm_scope,
                            uid,
                            db.json_dumps_compact(dm_nicknames or []),
                            dm_clean["relationship"],
                            dm_clean["summary"],
                            now,
                            now,
                            now,
                            now,
                        ),
                    )
                else:
                    conn.execute(
                        "INSERT INTO profiles("
                        "profile_key, scope, entity_id, nicknames_json, seen_groups_json, "
                        "relationship, summary, msg_count, first_seen_ts, last_seen_ts, updated_at"
                        ") VALUES (?, ?, ?, ?, '[]', ?, ?, 0, ?, ?, ?)",
                        (
                            dm_scope,
                            dm_scope,
                            uid,
                            db.json_dumps_compact(dm_nicknames or []),
                            dm_clean["relationship"],
                            dm_clean["summary"],
                            now,
                            now,
                            now,
                        ),
                    )
                dm_exists = True

            dm_items = list(dm_clean["facts"]) + list(dm_clean["inside_jokes"])
            audit_detail["dm_diff"] = _diff_and_sync_scope_memories(
                conn,
                scope=dm_scope,
                owner_uid=uid,
                group_id="",
                chat_key=dm_scope,
                submitted_items=dm_items,
                now=now,
                default_kind="stable",
                is_group=False,
                loaded_at=loaded_at,
            )

        for pkey, scope, names in (
            (pub_pkey, "user_public", public_nicknames),
            (dm_scope, dm_scope, dm_nicknames),
        ):
            if names is not None:
                conn.execute(
                    "INSERT INTO profiles(profile_key, scope, entity_id, nicknames_json, "
                    "first_seen_ts, last_seen_ts, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(profile_key) DO UPDATE SET nicknames_json = excluded.nicknames_json, "
                    "updated_at = excluded.updated_at",
                    (pkey, scope, uid, db.json_dumps_compact(names), now, now, now),
                )

        # Ensure brand-new user record stays readable if created with empty profiles
        if not pub_exists and not dm_exists and not _user_exists_in_db(conn, uid):
            conn.execute(
                "INSERT INTO profiles("
                "profile_key, scope, entity_id, nicknames_json, seen_groups_json, "
                "relationship, summary, msg_count, first_seen_ts, last_seen_ts, updated_at"
                ") VALUES (?, 'user_public', ?, '[]', '[]', '', '', 0, ?, ?, ?)",
                (pub_pkey, uid, now, now, now),
            )

        if payload.get("clear_pending_public"):
            cur = conn.execute(
                "UPDATE raw_messages SET distilled = 2 "
                "WHERE sender_uid = ? AND chat_key LIKE 'g:%' AND distilled = 0",
                (uid,),
            )
            audit_detail["cleared_pending_public"] = int(cur.rowcount or 0)

        if payload.get("clear_pending_dm"):
            cur = conn.execute(
                "UPDATE raw_messages SET distilled = 2 WHERE chat_key = ? AND distilled = 0",
                (dm_scope,),
            )
            audit_detail["cleared_pending_dm"] = int(cur.rowcount or 0)

        db.log_audit(
            conn,
            run_type="webui",
            chat_key="",
            action="update_user_memory",
            target_id=uid,
            detail=audit_detail,
            created_at=now,
        )

    return get_memory(uid, group_map=group_map)


def delete_memory(user_id):
    uid = user_memory._sanitize_uid(user_id)
    if not uid:
        raise StoreError("无效的用户 QQ 号")

    env = _resolve_parse_bot_env()
    db_path = _resolve_memory_db_path(env)
    db = user_memory.db
    dm_scope = f"dm:{uid}"

    with db.read_conn(db_path) as conn:
        if not _user_exists_in_db(conn, uid):
            raise NotFoundError(f"找不到用户 {uid} 的记忆档案")

    with db.write_tx(db_path) as conn:
        if not _user_exists_in_db(conn, uid):
            raise NotFoundError(f"找不到用户 {uid} 的记忆档案")

        mem_rows = conn.execute(
            "SELECT id FROM memories WHERE owner_uid = ? OR scope = ?",
            (uid, dm_scope),
        ).fetchall()
        mem_ids = [str(r["id"]) for r in mem_rows if r["id"]]
        for mid in mem_ids:
            db.delete_memory_fts(conn, mid)

        conn.execute(
            "DELETE FROM memories WHERE owner_uid = ? OR scope = ?",
            (uid, dm_scope),
        )
        conn.execute(
            "DELETE FROM profiles WHERE profile_key IN (?, ?)",
            (f"user_public:{uid}", dm_scope),
        )
        conn.execute(
            "DELETE FROM relations WHERE src_id = ? OR dst_id = ?",
            (uid, uid),
        )
        conn.execute(
            "DELETE FROM raw_messages WHERE chat_key = ?",
            (dm_scope,),
        )
        conn.execute(
            "DELETE FROM raw_messages "
            "WHERE sender_uid = ? AND chat_key LIKE 'g:%' AND role = 'user' AND distilled = 0",
            (uid,),
        )
        conn.execute(
            "DELETE FROM session_handoffs WHERE chat_key = ?",
            (dm_scope,),
        )
        conn.execute(
            "DELETE FROM recall_traces WHERE sender_uid = ?",
            (uid,),
        )
        generation = int(db.get_meta(conn, db.USER_MEMORY_DELETE_GENERATION, "0"))
        db.set_meta(conn, db.USER_MEMORY_DELETE_GENERATION, str(generation + 1))
        db.log_audit(
            conn,
            run_type="webui",
            chat_key=dm_scope,
            action="delete_user_memory",
            target_id=uid,
            detail={"deleted_memories": len(mem_ids)},
        )

    return {"deleted": True, "user_id": uid}


def trigger_consolidation(user_id, scope="public"):
    uid = user_memory._sanitize_uid(user_id)
    if not uid:
        raise StoreError("无效的用户 QQ 号")
    scope = str(scope or "public").strip().lower()
    if scope not in ("public", "dm", "both"):
        raise StoreError("scope 必须为 public、dm 或 both")

    env = _resolve_parse_bot_env()
    db_path = _resolve_memory_db_path(env)
    db = user_memory.db
    dm_scope = f"dm:{uid}"

    with db.read_conn(db_path) as conn:
        if not _user_exists_in_db(conn, uid):
            raise NotFoundError(f"找不到用户 {uid} 的记忆档案")
        pub_row = conn.execute(
            "SELECT COUNT(*) FROM raw_messages "
            "WHERE sender_uid = ? AND role = 'user' AND distilled = 0 AND chat_key LIKE 'g:%'",
            (uid,),
        ).fetchone()
        dm_row = conn.execute(
            "SELECT COUNT(*) FROM raw_messages "
            "WHERE chat_key = ? AND role = 'user' AND distilled = 0",
            (dm_scope,),
        ).fetchone()

    pend_pub = int(pub_row[0] or 0) if pub_row else 0
    pend_dm = int(dm_row[0] or 0) if dm_row else 0

    target_scopes = ["public", "dm"] if scope == "both" else [scope]
    runnable = []
    for sc in target_scopes:
        cnt = pend_pub if sc == "public" else pend_dm
        if cnt > 0:
            runnable.append(sc)

    if not runnable:
        return {
            "triggered": False,
            "scopes": [],
            "chat_keys": [],
            "pending_public": pend_pub,
            "pending_dm": pend_dm,
            "message": "该用户当前没有待整理的聊天消息队列，无需触发整理。",
        }

    _sync_live_memory_runtime_config(env)
    res = user_memory.consolidate.consolidate_user_pending(
        uid,
        scope=scope,
        db_path=db_path,
        async_run=True,
    )
    chat_keys = list(res.get("chat_keys") or [])
    triggered_ok = bool(res.get("triggered"))
    if triggered_ok:
        msg = f"已提交后台记忆整理任务（范围：{', '.join(runnable)}，窗口：{len(chat_keys)} 个），完成后刷新档案即可看到结果。"
    else:
        msg = "相关会话窗口已在后台整理队列中（或长期记忆开关未启用），请稍后刷新查看。"

    return {
        "triggered": triggered_ok,
        "scopes": runnable if triggered_ok else [],
        "chat_keys": chat_keys,
        "pending_public": pend_pub,
        "pending_dm": pend_dm,
        "message": msg,
    }


def list_group_memories(group_map=None):
    env = _resolve_parse_bot_env()
    db_path = _resolve_memory_db_path(env)
    db = user_memory.db
    gmap = {db.sanitize_uid(k): str(v) for k, v in (group_map or {}).items() if db.sanitize_uid(k)}

    groups_info = {}

    def _ensure_group(gid):
        entry = groups_info.get(gid)
        if entry is None:
            entry = {
                "group_id": gid,
                "group_name": gmap.get(gid) or f"QQ 群 {gid}",
                "summary": "",
                "msg_count": 0,
                "last_ts": 0,
                "active_count": 0,
                "archived_count": 0,
                "pending_count": 0,
            }
            groups_info[gid] = entry
        return entry

    for gid in gmap:
        _ensure_group(gid)

    with db.read_conn(db_path) as conn:
        prof_rows = conn.execute(
            "SELECT scope, entity_id, summary, msg_count, last_seen_ts "
            "FROM profiles WHERE scope LIKE 'group:%'"
        ).fetchall()
        for row in prof_rows:
            scope = str(row["scope"] or "")
            gid = db.sanitize_uid(row["entity_id"] or scope[6:])
            if not gid:
                continue
            g = _ensure_group(gid)
            g["summary"] = str(row["summary"] or "")
            g["msg_count"] = int(row["msg_count"] or 0)
            g["last_ts"] = max(g["last_ts"], int(row["last_seen_ts"] or 0))

        mem_rows = conn.execute(
            "SELECT scope, group_id, status, updated_at "
            "FROM memories WHERE scope LIKE 'group:%'"
        ).fetchall()
        for row in mem_rows:
            scope = str(row["scope"] or "")
            gid = db.sanitize_uid(row["group_id"] or scope[6:])
            if not gid:
                continue
            g = _ensure_group(gid)
            if str(row["status"] or "") == "active":
                g["active_count"] += 1
            else:
                g["archived_count"] += 1
            g["last_ts"] = max(g["last_ts"], int(row["updated_at"] or 0))

        raw_rows = conn.execute(
            "SELECT chat_key, group_id, "
            "SUM(CASE WHEN distilled = 0 AND role = 'user' THEN 1 ELSE 0 END) AS pending_cnt, "
            "MAX(ts) AS max_ts "
            "FROM raw_messages WHERE chat_key LIKE 'g:%' "
            "GROUP BY chat_key, group_id"
        ).fetchall()
        for row in raw_rows:
            ck = str(row["chat_key"] or "")
            gid = db.sanitize_uid(row["group_id"] or ck[2:])
            if not gid:
                continue
            g = _ensure_group(gid)
            g["pending_count"] += int(row["pending_cnt"] or 0)
            g["last_ts"] = max(g["last_ts"], int(row["max_ts"] or 0))

    groups_list = []
    for gid, g in groups_info.items():
        groups_list.append({
            "group_id": gid,
            "group_name": g["group_name"],
            "summary": g["summary"],
            "msg_count": g["msg_count"],
            "last_seen": db.now_str(g["last_ts"]) if g["last_ts"] > 0 else "",
            "active_count": g["active_count"],
            "archived_count": g["archived_count"],
            "pending_count": g["pending_count"],
        })

    groups_list.sort(
        key=lambda x: (
            1 if (x["active_count"] > 0 or x["summary"]) else 0,
            x["last_seen"],
            x["active_count"],
            x["msg_count"],
        ),
        reverse=True,
    )
    return {"groups": groups_list}


def get_group_memory(group_id, group_map=None):
    gid = user_memory._sanitize_uid(group_id)
    if not gid:
        raise StoreError("无效的群号")

    env = _resolve_parse_bot_env()
    db_path = _resolve_memory_db_path(env)
    db = user_memory.db
    scope = f"group:{gid}"
    chat_key = f"g:{gid}"
    gmap = dict(group_map or {})

    with db.read_conn(db_path) as conn:
        prof = conn.execute(
            "SELECT * FROM profiles WHERE profile_key = ?",
            (scope,),
        ).fetchone()
        active_rows = _scope_active_memories(conn, scope)
        archived_rows = conn.execute(
            "SELECT * FROM memories WHERE scope = ? AND status != 'active' "
            "ORDER BY updated_at DESC LIMIT 100",
            (scope,),
        ).fetchall()
        handoff_rows = conn.execute(
            "SELECT window_id, chat_key, scope, msg_start_id, msg_end_id, "
            "summary, open_threads, created_at "
            "FROM session_handoffs WHERE chat_key = ? "
            "ORDER BY created_at DESC, rowid DESC LIMIT 10",
            (chat_key,),
        ).fetchall()
        pend_row = conn.execute(
            "SELECT COUNT(*) AS cnt, MAX(ts) AS max_ts FROM raw_messages "
            "WHERE chat_key = ? AND role = 'user' AND distilled = 0",
            (chat_key,),
        ).fetchone()
        max_raw_row = conn.execute(
            "SELECT MAX(ts) FROM raw_messages WHERE chat_key = ?",
            (chat_key,),
        ).fetchone()

    last_ts = 0
    if prof and prof["last_seen_ts"]:
        last_ts = max(last_ts, int(prof["last_seen_ts"]))
    if max_raw_row and max_raw_row[0]:
        last_ts = max(last_ts, int(max_raw_row[0]))
    if active_rows and active_rows[0]["updated_at"]:
        last_ts = max(last_ts, max(int(r["updated_at"] or 0) for r in active_rows))

    handoffs = [
        {
            "window_id": str(r["window_id"] or ""),
            "chat_key": str(r["chat_key"] or ""),
            "msg_start_id": int(r["msg_start_id"] or 0),
            "msg_end_id": int(r["msg_end_id"] or 0),
            "summary": str(r["summary"] or ""),
            "open_threads": str(r["open_threads"] or ""),
            "created_at": db.now_str(int(r["created_at"] or 0)),
            "created_at_ts": int(r["created_at"] or 0),
        }
        for r in handoff_rows
    ]

    return {
        "group_id": gid,
        "group_name": gmap.get(gid) or f"QQ 群 {gid}",
        "summary": str(prof["summary"] or "") if prof else "",
        "msg_count": int(prof["msg_count"] or 0) if prof else 0,
        "last_seen": db.now_str(last_ts) if last_ts > 0 else "",
        "items": [_format_memory_item(r) for r in active_rows],
        "archived_items": [_format_archived_item(r) for r in archived_rows],
        "handoffs": handoffs,
        "pending_count": int(pend_row["cnt"] or 0) if pend_row else 0,
        "loaded_at": db.now_ts(),
        "revision": _memory_revision(prof, active_rows),
    }


def update_group_memory(group_id, payload, group_map=None):
    gid = user_memory._sanitize_uid(group_id)
    if not gid:
        raise StoreError("无效的群号")
    if not isinstance(payload, dict):
        raise StoreError("请求体必须是 JSON 对象")

    has_summary = "summary" in payload
    summary_val = str(payload.get("summary") or "").strip()[:300] if has_summary else ""

    has_items = "items" in payload
    cleaned_items = []
    if has_items:
        seen_ids = set()
        seen_texts = set()
        for raw_item in payload.get("items") or []:
            cleaned = _clean_v2_item_input(raw_item, is_joke=False, is_group=True)
            if not cleaned:
                continue
            norm_t = _norm_mem_text(cleaned["text"])
            if cleaned["id"]:
                if cleaned["id"] in seen_ids:
                    continue
                seen_ids.add(cleaned["id"])
            elif norm_t in seen_texts:
                continue
            seen_texts.add(norm_t)
            cleaned_items.append(cleaned)

    matches = _scan_sensitive_group_payload(summary_val if has_summary else "", cleaned_items)
    if matches:
        raise SensitiveMemoryError(
            matches,
            message="群共享记忆命中敏感词或隐私规则，禁止写入群聊可见档案",
        )

    env = _resolve_parse_bot_env()
    db_path = _resolve_memory_db_path(env)
    db = user_memory.db
    scope = f"group:{gid}"
    chat_key = f"g:{gid}"
    now = db.now_ts()
    loaded_at = int(db.parse_ts(payload.get("loaded_at"), 0) or 0)
    audit_detail = {"group_id": gid}

    with db.write_tx(db_path) as conn:
        _check_memory_revision(conn, payload.get("revision"), scope, scope)
        has_prof_upd_col = _table_has_column(conn, "profiles", "profile_updated_at")
        if has_summary:
            prof_exists = (
                conn.execute("SELECT 1 FROM profiles WHERE profile_key = ?", (scope,)).fetchone()
                is not None
            )
            if prof_exists:
                if has_prof_upd_col:
                    conn.execute(
                        "UPDATE profiles SET summary = ?, profile_updated_at = ?, updated_at = ? "
                        "WHERE profile_key = ?",
                        (summary_val, now, now, scope),
                    )
                else:
                    conn.execute(
                        "UPDATE profiles SET summary = ?, updated_at = ? WHERE profile_key = ?",
                        (summary_val, now, scope),
                    )
            else:
                if has_prof_upd_col:
                    conn.execute(
                        "INSERT INTO profiles("
                        "profile_key, scope, entity_id, nicknames_json, seen_groups_json, "
                        "relationship, summary, msg_count, first_seen_ts, last_seen_ts, "
                        "profile_updated_at, updated_at"
                        ") VALUES (?, ?, ?, '[]', ?, '', ?, 0, ?, ?, ?, ?)",
                        (scope, scope, gid, db.json_dumps_compact([gid]), summary_val, now, now, now, now),
                    )
                else:
                    conn.execute(
                        "INSERT INTO profiles("
                        "profile_key, scope, entity_id, nicknames_json, seen_groups_json, "
                        "relationship, summary, msg_count, first_seen_ts, last_seen_ts, updated_at"
                        ") VALUES (?, ?, ?, '[]', ?, '', ?, 0, ?, ?, ?)",
                        (scope, scope, gid, db.json_dumps_compact([gid]), summary_val, now, now, now),
                    )
            audit_detail["summary_updated"] = True

        if has_items:
            audit_detail["items_diff"] = _diff_and_sync_scope_memories(
                conn,
                scope=scope,
                owner_uid="",
                group_id=gid,
                chat_key=chat_key,
                submitted_items=cleaned_items,
                now=now,
                default_kind="group_event",
                is_group=True,
                loaded_at=loaded_at,
            )

        db.log_audit(
            conn,
            run_type="webui",
            chat_key=chat_key,
            action="update_group_memory",
            target_id=gid,
            detail=audit_detail,
            created_at=now,
        )

    return get_group_memory(gid, group_map=group_map)


def get_user_memory_traces(user_id, limit=20):
    uid = user_memory._sanitize_uid(user_id)
    if not uid:
        raise StoreError("无效的用户 QQ 号")

    try:
        lim = max(1, min(100, int(limit)))
    except (TypeError, ValueError):
        lim = 20

    env = _resolve_parse_bot_env()
    db_path = _resolve_memory_db_path(env)
    db = user_memory.db
    dm_chat_key = f"dm:{uid}"

    with db.read_conn(db_path) as conn:
        trace_rows = conn.execute(
            "SELECT id, chat_key, session_id, sender_uid, query_text, "
            "gate_info_json, selected_json, omitted_json, created_at "
            "FROM recall_traces "
            "WHERE sender_uid = ? "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            (uid, lim),
        ).fetchall()

        uid_like_escaped = uid.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        owner_json_pattern = f'%"owner_uid":"{uid_like_escaped}"%'
        audit_rows = conn.execute(
            "SELECT id, run_type, chat_key, action, target_id, detail_json, created_at "
            "FROM audit_logs "
            "WHERE target_id = ? "
            "   OR target_id IN (SELECT id FROM memories WHERE owner_uid = ?) "
            "   OR chat_key = ? "
            "   OR detail_json LIKE ? ESCAPE '\\' "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            (uid, uid, dm_chat_key, owner_json_pattern, lim),
        ).fetchall()

    recall_traces = []
    for r in trace_rows:
        cts = int(r["created_at"] or 0)
        recall_traces.append({
            "id": int(r["id"]),
            "chat_key": str(r["chat_key"] or ""),
            "session_id": str(r["session_id"] or ""),
            "sender_uid": str(r["sender_uid"] or ""),
            "query_text": str(r["query_text"] or ""),
            "gate_info": db.json_loads_safe(r["gate_info_json"], {}),
            "selected": db.json_loads_safe(r["selected_json"], []),
            "omitted": db.json_loads_safe(r["omitted_json"], []),
            "created_at": db.now_str(cts),
            "created_at_ts": cts,
        })

    audit_logs = []
    for r in audit_rows:
        cts = int(r["created_at"] or 0)
        audit_logs.append({
            "id": int(r["id"]),
            "run_type": str(r["run_type"] or ""),
            "chat_key": str(r["chat_key"] or ""),
            "action": str(r["action"] or ""),
            "target_id": str(r["target_id"] or ""),
            "detail": db.json_loads_safe(r["detail_json"], {}),
            "created_at": db.now_str(cts),
            "created_at_ts": cts,
        })

    return {
        "user_id": uid,
        "recall_traces": recall_traces,
        "audit_logs": audit_logs,
    }


def list_memory_conflicts():
    env = _resolve_parse_bot_env()
    db_path = _resolve_memory_db_path(env)
    db = user_memory.db

    with db.read_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM memories WHERE status = 'conflict_pending' "
            "ORDER BY updated_at DESC, id ASC"
        ).fetchall()

        by_id = {str(r["id"]): r for r in rows}
        partner_ids_missing = set()
        owner_uids = set()
        parsed_meta = {}

        for r in rows:
            mid = str(r["id"])
            pid, reason, flagged_ts = _extract_conflict_info(r["history_json"])
            parsed_meta[mid] = (pid, reason, flagged_ts)
            if pid and pid not in by_id:
                partner_ids_missing.add(pid)
            if r["owner_uid"]:
                owner_uids.add(str(r["owner_uid"]))

        if partner_ids_missing:
            ph = ",".join("?" for _ in partner_ids_missing)
            extra_rows = conn.execute(
                f"SELECT * FROM memories WHERE id IN ({ph})",
                tuple(partner_ids_missing),
            ).fetchall()
            for er in extra_rows:
                by_id[str(er["id"])] = er
                if er["owner_uid"]:
                    owner_uids.add(str(er["owner_uid"]))

        name_map = {}
        if owner_uids:
            ph = ",".join("?" for _ in owner_uids)
            p_rows = conn.execute(
                f"SELECT entity_id, nicknames_json FROM profiles "
                f"WHERE (scope = 'user_public' OR scope LIKE 'dm:%') AND entity_id IN ({ph}) "
                f"ORDER BY updated_at ASC",
                tuple(owner_uids),
            ).fetchall()
            for pr in p_rows:
                eid = str(pr["entity_id"] or "")
                nicks = [
                    str(n).strip()
                    for n in db.json_loads_safe(pr["nicknames_json"], [])
                    if str(n).strip()
                ]
                if eid and nicks:
                    name_map[eid] = nicks[-1]

    def _resolve_owner_name(row):
        ouid = str(row["owner_uid"] or "")
        gid = str(row["group_id"] or "")
        if ouid:
            return name_map.get(ouid, ouid)
        if gid:
            return f"QQ 群 {gid}"
        return str(row["scope"] or "")

    def _serialize_side(row):
        base = _format_memory_item(row)
        scope = str(row["scope"] or "")
        upd_ts = int(row["updated_at"] or 0)
        base.update({
            "scope": scope,
            "scope_label": "public" if scope == "user_public" else ("dm" if scope.startswith("dm:") else "group"),
            "owner_uid": str(row["owner_uid"] or ""),
            "owner_name": _resolve_owner_name(row),
            "group_id": str(row["group_id"] or ""),
            "updated_at": db.now_str(upd_ts) or base["updated_at"],
            "updated_at_ts": upd_ts,
        })
        return base

    conflicts = []
    seen_pairs = set()

    for r in rows:
        mid = str(r["id"])
        pid, reason, flagged_ts = parsed_meta[mid]
        if pid and pid != mid:
            pair_key = tuple(sorted((mid, pid)))
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)

        item = _serialize_side(r)
        partner_row = by_id.get(pid) if pid else None
        item["reason"] = reason
        item["flagged_at"] = db.now_str(flagged_ts) if flagged_ts else item["updated_at"]
        item["partner_id"] = pid
        item["partner"] = _serialize_side(partner_row) if partner_row is not None else None
        conflicts.append(item)

    return {"conflicts": conflicts, "total": len(conflicts)}


def resolve_memory_conflict(mem_id, action, partner_id=""):
    mid = str(mem_id or "").strip()
    if not mid:
        raise StoreError("无效的记忆 ID")
    act = str(action or "").strip().lower()
    if act not in ("keep_this", "keep_partner", "keep_both", "archive_both"):
        raise StoreError("无效的冲突处理动作，必须为 keep_this、keep_partner、keep_both 或 archive_both")

    env = _resolve_parse_bot_env()
    db_path = _resolve_memory_db_path(env)
    db = user_memory.db
    now = db.now_ts()

    with db.write_tx(db_path) as conn:
        row_a = conn.execute("SELECT * FROM memories WHERE id = ?", (mid,)).fetchone()
        if row_a is None:
            raise NotFoundError(f"找不到记忆条目：{mid}")

        pid = str(partner_id or "").strip()
        if not pid:
            pid, _, _ = _extract_conflict_info(row_a["history_json"])
        if pid == mid:
            pid = ""

        row_b = (
            conn.execute("SELECT * FROM memories WHERE id = ?", (pid,)).fetchone()
            if pid
            else None
        )
        if act == "keep_partner" and row_b is None:
            raise NotFoundError("找不到对应的冲突配对记忆，无法执行 keep_partner")

        def _sync_row_fts(row):
            db.sync_memory_fts(
                conn,
                mem_id=str(row["id"]),
                scope=str(row["scope"] or ""),
                owner_uid=str(row["owner_uid"] or ""),
                group_id=str(row["group_id"] or ""),
                text=str(row["text"] or ""),
                keywords=db.json_loads_safe(row["keywords_json"], []),
            )

        if act == "keep_this":
            hist_a = _append_mem_history(
                row_a["history_json"],
                {"action": "webui_conflict_resolved", "decision": "keep_this", "partner_id": pid, "ts": now},
            )
            conn.execute(
                "UPDATE memories SET status = 'active', superseded_by = '', "
                "history_json = ?, updated_at = ? WHERE id = ?",
                (hist_a, now, mid),
            )
            _sync_row_fts(row_a)
            if row_b is not None:
                hist_b = _append_mem_history(
                    row_b["history_json"],
                    {
                        "action": "webui_conflict_resolved",
                        "decision": "superseded_by_partner",
                        "superseded_by": mid,
                        "ts": now,
                    },
                )
                conn.execute(
                    "UPDATE memories SET status = 'superseded', superseded_by = ?, "
                    "history_json = ?, updated_at = ? WHERE id = ?",
                    (mid, hist_b, now, pid),
                )
                db.delete_memory_fts(conn, pid)

        elif act == "keep_partner":
            hist_b = _append_mem_history(
                row_b["history_json"],
                {"action": "webui_conflict_resolved", "decision": "keep_partner", "partner_id": mid, "ts": now},
            )
            conn.execute(
                "UPDATE memories SET status = 'active', superseded_by = '', "
                "history_json = ?, updated_at = ? WHERE id = ?",
                (hist_b, now, pid),
            )
            _sync_row_fts(row_b)

            hist_a = _append_mem_history(
                row_a["history_json"],
                {
                    "action": "webui_conflict_resolved",
                    "decision": "superseded_by_partner",
                    "superseded_by": pid,
                    "ts": now,
                },
            )
            conn.execute(
                "UPDATE memories SET status = 'superseded', superseded_by = ?, "
                "history_json = ?, updated_at = ? WHERE id = ?",
                (pid, hist_a, now, mid),
            )
            db.delete_memory_fts(conn, mid)

        elif act == "keep_both":
            pair_sig = user_memory.sleep._conflict_pair_sig(dict(row_a), dict(row_b)) if row_b else ""
            hist_a = _append_mem_history(
                row_a["history_json"],
                {"action": "webui_conflict_resolved", "decision": "keep_both", "partner_id": pid,
                 "sig": pair_sig, "ts": now},
            )
            conn.execute(
                "UPDATE memories SET status = 'active', superseded_by = '', "
                "history_json = ?, updated_at = ? WHERE id = ?",
                (hist_a, now, mid),
            )
            _sync_row_fts(row_a)
            if row_b is not None:
                hist_b = _append_mem_history(
                    row_b["history_json"],
                    {"action": "webui_conflict_resolved", "decision": "keep_both", "partner_id": mid,
                     "sig": pair_sig, "ts": now},
                )
                conn.execute(
                    "UPDATE memories SET status = 'active', superseded_by = '', "
                    "history_json = ?, updated_at = ? WHERE id = ?",
                    (hist_b, now, pid),
                )
                _sync_row_fts(row_b)

        elif act == "archive_both":
            hist_a = _append_mem_history(
                row_a["history_json"],
                {"action": "webui_conflict_resolved", "decision": "archive_both", "partner_id": pid, "ts": now},
            )
            conn.execute(
                "UPDATE memories SET status = 'archived', superseded_by = '', "
                "history_json = ?, updated_at = ? WHERE id = ?",
                (hist_a, now, mid),
            )
            # Keep archived rows indexed in FTS5 so explicit >=2-term queries can still recall them
            _sync_row_fts(row_a)
            if row_b is not None:
                hist_b = _append_mem_history(
                    row_b["history_json"],
                    {"action": "webui_conflict_resolved", "decision": "archive_both", "partner_id": mid, "ts": now},
                )
                conn.execute(
                    "UPDATE memories SET status = 'archived', superseded_by = '', "
                    "history_json = ?, updated_at = ? WHERE id = ?",
                    (hist_b, now, pid),
                )
                _sync_row_fts(row_b)

        db.log_audit(
            conn,
            run_type="webui",
            chat_key="",
            action="resolve_conflict",
            target_id=mid,
            detail={"action": act, "mem_id": mid, "partner_id": pid},
            created_at=now,
        )

    return {
        "resolved": True,
        "action": act,
        "mem_id": mid,
        "partner_id": pid,
    }


def run_memory_maintenance_flush(async_run=True, idle_mins=0.0):
    env = _resolve_parse_bot_env()
    if idle_mins is None:
        try:
            idle_val = max(0.0, float(env.get("MEMORY_IDLE_FLUSH_MINS", 10) or 10))
        except (TypeError, ValueError):
            idle_val = 10.0
    else:
        try:
            idle_val = max(0.0, float(idle_mins))
        except (TypeError, ValueError):
            idle_val = 0.0

    db_path = _resolve_memory_db_path(env)
    _sync_live_memory_runtime_config(env)

    windows = user_memory.ingest.find_idle_windows(db_path=db_path, idle_mins=idle_val)

    if async_run:
        if not windows:
            return {
                "triggered": False,
                "async": True,
                "windows_queued": 0,
                "scheduled_count": 0,
                "chat_keys": [],
                "message": "当前没有包含有效待整理消息的会话窗口。",
            }
        scheduled = []
        for ck in windows:
            if user_memory.consolidate.schedule_window_consolidation(ck, delay=0.1, db_path=db_path):
                scheduled.append(ck)
        count = len(scheduled)
        return {
            "triggered": bool(count),
            "async": True,
            "windows_queued": count,
            "scheduled_count": count,
            "chat_keys": scheduled,
            "message": (
                f"已提交 {count} 个会话窗口的后台空闲整理任务，片刻后刷新即可查看结果。"
                if count else "本次没有新增后台整理任务。"
            ),
        }

    # Call consolidate.run_idle_flush directly so admin manual flush still works
    # even when the background memory_extraction plugin is toggled OFF.
    result = user_memory.consolidate.run_idle_flush(db_path=db_path, idle_mins=idle_val)
    return {
        **result,
        "triggered": True,
        "async": False,
        "windows_queued": len(windows),
        "chat_keys": windows,
    }


def run_memory_maintenance_sleep(async_run=True, force=True, skip_llm=False):
    global _sleep_in_progress
    env = _resolve_parse_bot_env()
    db_path = _resolve_memory_db_path(env)
    _sync_live_memory_runtime_config(env)
    llm_arg = False if skip_llm else None

    with _sleep_running_lock:
        if _sleep_in_progress:
            return {
                "started": False,
                "triggered": False,
                "reason": "already_running",
                "async": bool(async_run),
                "message": "Sleep 深度整理已在后台运行中，请稍后再查看结果。",
            }
        _sleep_in_progress = True

    if async_run:
        def _worker():
            global _sleep_in_progress
            try:
                user_memory.run_sleep_cycle(
                    db_path=db_path,
                    force=bool(force),
                    llm_fn=llm_arg,
                )
            except Exception as exc:
                try:
                    sys.stderr.write(f"[webui] memory sleep cycle error: {exc}\n")
                    sys.stderr.flush()
                except Exception:
                    pass
            finally:
                with _sleep_running_lock:
                    _sleep_in_progress = False

        try:
            threading.Thread(target=_worker, daemon=True, name="webui-memory-sleep").start()
        except Exception:
            with _sleep_running_lock:
                _sleep_in_progress = False
            raise
        return {
            "started": True,
            "triggered": True,
            "async": True,
            "message": "已提交后台 Sleep 深度整理任务，片刻后刷新即可查看结果。",
        }

    try:
        report = user_memory.run_sleep_cycle(
            db_path=db_path,
            force=bool(force),
            llm_fn=llm_arg,
        )
    finally:
        with _sleep_running_lock:
            _sleep_in_progress = False
    return {
        "started": True,
        "triggered": True,
        "async": False,
        "report": report,
    }


def restore_memory_item(mem_id):
    mid = str(mem_id or "").strip()
    if not mid:
        raise StoreError("无效的记忆 ID")

    env = _resolve_parse_bot_env()
    db_path = _resolve_memory_db_path(env)
    db = user_memory.db
    now = db.now_ts()

    with db.write_tx(db_path) as conn:
        row = conn.execute("SELECT * FROM memories WHERE id = ?", (mid,)).fetchone()
        if row is None:
            raise NotFoundError(f"找不到记忆条目：{mid}")

        old_status = str(row["status"] or "")
        new_hist = _append_mem_history(
            row["history_json"],
            {"action": "webui_restore", "prev_status": old_status, "ts": now},
        )
        conn.execute(
            "UPDATE memories SET status = 'active', superseded_by = '', ended_at = 0, "
            "history_json = ?, updated_at = ? WHERE id = ?",
            (new_hist, now, mid),
        )
        kws = db.json_loads_safe(row["keywords_json"], [])
        db.sync_memory_fts(
            conn,
            mem_id=mid,
            scope=str(row["scope"] or ""),
            owner_uid=str(row["owner_uid"] or ""),
            group_id=str(row["group_id"] or ""),
            text=str(row["text"] or ""),
            keywords=kws,
        )
        db.log_audit(
            conn,
            run_type="webui",
            chat_key="",
            action="restore_item",
            target_id=mid,
            detail={
                "prev_status": old_status,
                "scope": str(row["scope"] or ""),
                "owner_uid": str(row["owner_uid"] or ""),
                "group_id": str(row["group_id"] or ""),
            },
            created_at=now,
        )
        updated_row = conn.execute("SELECT * FROM memories WHERE id = ?", (mid,)).fetchone()

    item = _format_memory_item(updated_row)
    item["scope"] = str(updated_row["scope"] or "")
    item["owner_uid"] = str(updated_row["owner_uid"] or "")
    item["group_id"] = str(updated_row["group_id"] or "")
    return {"restored": True, "item": item}
