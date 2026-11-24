#!/usr/bin/env python3
"""Lossless, 100% idempotent v1 JSON -> SQLite v2 migration tool.

Reads v1 per-user memory JSON files from `data/user_memories/*.json` in strict
read-only mode (NEVER modifies or deletes any files in `v1_dir`) and populates
the SQLite v2 Memory Engine database (`profiles`, `memories`, `memories_fts`,
`raw_messages`, `meta_kv`, `audit_logs`).

Re-running on the same database produces zero duplicates and preserves existing
v2 state cleanly. Python 3 standard library ONLY.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from typing import Any

if __package__:
    from . import db, tokenizer
else:
    _ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _ROOT not in sys.path:
        sys.path.insert(0, _ROOT)
    from memory_engine import db, tokenizer


# ---------------------------------------------------------------------------
# Path & Deterministic ID Helpers
# ---------------------------------------------------------------------------

def default_v1_dir() -> str:
    """Return the v1 JSON user memory directory (respecting USER_MEMORY_DIR)."""
    raw = os.environ.get("USER_MEMORY_DIR", "").strip()
    if raw:
        return os.path.expanduser(raw)
    return os.path.join(db.PROJECT_DIR, "data", "user_memories")


def _v1_mem_id(uid: str, scope: str, kind: str, text: str) -> str:
    """Deterministic memory id so repeated migrations are 100% idempotent."""
    digest = hashlib.sha1(f"{uid}|{scope}|{kind}|{text}".encode("utf-8")).hexdigest()[:16]
    return f"v1_{digest}"


def _v1_raw_dedup(uid: str, scope: str, role: str, entry_id: str, text: str) -> str:
    """Deterministic dedup hash for migrated pending raw messages."""
    digest = hashlib.sha1(
        f"{uid}|{scope}|{role}|{entry_id}|{text[:120]}".encode("utf-8")
    ).hexdigest()[:20]
    return f"v1p_{digest}"


# ---------------------------------------------------------------------------
# Internal Helpers
# ---------------------------------------------------------------------------

def _dedup_ordered(items: list[Any], transform=None, limit: int = 0) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in items:
        val = transform(raw) if callable(transform) else str(raw or "").strip()
        if not val or val in seen:
            continue
        seen.add(val)
        out.append(val)
    if limit > 0 and len(out) > limit:
        out = out[-limit:]
    return out


def _upsert_profile_idempotent(
    conn: sqlite3.Connection,
    profile_key: str,
    scope: str,
    entity_id: str,
    nicknames: list[str],
    seen_groups: list[str],
    relationship: str,
    summary: str,
    msg_count: int,
    first_seen_ts: int,
    last_seen_ts: int,
    updated_at: int,
) -> None:
    """Insert or idempotently merge a v1 profile without doubling counts or lists."""
    row = conn.execute(
        "SELECT nicknames_json, seen_groups_json, relationship, summary, "
        "msg_count, first_seen_ts, last_seen_ts, updated_at "
        "FROM profiles WHERE profile_key = ?",
        (profile_key,),
    ).fetchone()

    clean_nicks = _dedup_ordered(nicknames, limit=8)
    clean_groups = _dedup_ordered(seen_groups, transform=db.sanitize_uid, limit=30)
    clean_rel = str(relationship or "").strip()
    clean_summary = str(summary or "").strip()
    clean_cnt = max(0, int(msg_count or 0))
    clean_first = max(0, int(first_seen_ts or 0))
    clean_last = max(0, int(last_seen_ts or 0))
    clean_upd = max(0, int(updated_at or 0))

    if row is None:
        conn.execute(
            "INSERT INTO profiles("
            "profile_key, scope, entity_id, nicknames_json, seen_groups_json, "
            "relationship, summary, msg_count, first_seen_ts, last_seen_ts, updated_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                profile_key,
                scope,
                entity_id,
                db.json_dumps_compact(clean_nicks),
                db.json_dumps_compact(clean_groups),
                clean_rel,
                clean_summary,
                clean_cnt,
                clean_first,
                clean_last,
                clean_upd,
            ),
        )
        return

    ex_nicks = _dedup_ordered(db.json_loads_safe(row["nicknames_json"], []), limit=8)
    ex_groups = _dedup_ordered(
        db.json_loads_safe(row["seen_groups_json"], []),
        transform=db.sanitize_uid,
        limit=30,
    )
    merged_nicks = _dedup_ordered(ex_nicks + clean_nicks, limit=8)
    merged_groups = _dedup_ordered(
        ex_groups + clean_groups,
        transform=db.sanitize_uid,
        limit=30,
    )

    ex_rel = str(row["relationship"] or "").strip()
    ex_sum = str(row["summary"] or "").strip()
    merged_rel = ex_rel if ex_rel else clean_rel
    merged_sum = ex_sum if ex_sum else clean_summary

    ex_cnt = max(0, int(row["msg_count"] or 0))
    merged_cnt = max(ex_cnt, clean_cnt)

    ex_first = int(row["first_seen_ts"] or 0)
    if ex_first > 0 and clean_first > 0:
        merged_first = min(ex_first, clean_first)
    else:
        merged_first = ex_first or clean_first

    ex_last = int(row["last_seen_ts"] or 0)
    merged_last = max(ex_last, clean_last)

    ex_upd = int(row["updated_at"] or 0)
    merged_upd = max(ex_upd, clean_upd)

    # Skip redundant write when nothing actually changed (strict idempotency)
    if (
        merged_nicks == ex_nicks
        and merged_groups == ex_groups
        and merged_rel == ex_rel
        and merged_sum == ex_sum
        and merged_cnt == ex_cnt
        and merged_first == ex_first
        and merged_last == ex_last
    ):
        return

    conn.execute(
        "UPDATE profiles SET "
        "nicknames_json = ?, seen_groups_json = ?, relationship = ?, summary = ?, "
        "msg_count = ?, first_seen_ts = ?, last_seen_ts = ?, updated_at = ? "
        "WHERE profile_key = ?",
        (
            db.json_dumps_compact(merged_nicks),
            db.json_dumps_compact(merged_groups),
            merged_rel,
            merged_sum,
            merged_cnt,
            merged_first,
            merged_last,
            merged_upd,
            profile_key,
        ),
    )


def _load_existing_ids_readonly(resolved_db: str) -> tuple[set[str], set[tuple[str, str]]]:
    """Inspect existing memory IDs and raw (chat_key, dedup_hash) pairs without modifying DB."""
    if not resolved_db or resolved_db == ":memory:" or not os.path.exists(resolved_db):
        return set(), set()

    mem_ids: set[str] = set()
    raw_keys: set[tuple[str, str]] = set()
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(f"file:{resolved_db}?mode=ro", uri=True, timeout=5.0)
        tables = {
            str(r[0])
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "memories" in tables:
            mem_ids = {
                str(r[0])
                for r in conn.execute("SELECT id FROM memories").fetchall()
                if r[0]
            }
        if "raw_messages" in tables:
            raw_keys = {
                (str(r[0] or ""), str(r[1]))
                for r in conn.execute(
                    "SELECT chat_key, dedup_hash FROM raw_messages WHERE dedup_hash != ''"
                ).fetchall()
                if r[1]
            }
    except sqlite3.Error:
        return set(), set()
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass
    return mem_ids, raw_keys


def _parse_fact_item(item: Any) -> tuple[str, str, str]:
    """Normalize a v1 fact dict/string into (text, category, occurred_at)."""
    if isinstance(item, dict):
        text = str(item.get("text") or "").strip()
        cat = str(item.get("category") or "其他").strip() or "其他"
        upd = str(item.get("updated_at") or "").strip()[:10]
        return text, cat, upd
    if isinstance(item, str):
        return item.strip(), "其他", ""
    return "", "其他", ""


def _parse_joke_item(item: Any) -> tuple[str, str]:
    """Normalize a v1 inside_joke dict/string into (text, occurred_at)."""
    if isinstance(item, dict):
        text = str(item.get("text") or "").strip()
        upd = str(item.get("updated_at") or "").strip()[:10]
        return text, upd
    if isinstance(item, str):
        return item.strip(), ""
    return "", ""


# ---------------------------------------------------------------------------
# Main Migration Entry Point
# ---------------------------------------------------------------------------

def migrate_v1_directory(
    v1_dir: str | None = None,
    db_path: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Migrate all v1 JSON user memory files into SQLite v2 without modifying v1_dir.

    Args:
        v1_dir: Directory containing v1 `<uid>.json` files (defaults to `default_v1_dir()`).
        db_path: Target SQLite database path (defaults to `db.default_db_path()`).
        dry_run: If True, perform zero database writes and return exact projected counts.

    Returns:
        Detailed migration statistics dict.
    """
    src_dir = os.path.abspath(os.path.expanduser(v1_dir)) if v1_dir else default_v1_dir()
    resolved_db = (
        db.default_db_path()
        if db_path is None or not str(db_path).strip()
        else (
            str(db_path).strip()
            if str(db_path).strip() == ":memory:" or str(db_path).strip().startswith("file:")
            else os.path.abspath(os.path.expanduser(str(db_path).strip()))
        )
    )

    stats: dict[str, Any] = {
        "v1_dir": src_dir,
        "db_path": resolved_db,
        "dry_run": bool(dry_run),
        "files_scanned": 0,
        "users_migrated": 0,
        "public_profiles": 0,
        "dm_profiles": 0,
        "group_profiles": 0,
        "public_facts": 0,
        "public_jokes": 0,
        "dm_facts": 0,
        "dm_jokes": 0,
        "memories_inserted": 0,
        "memories_skipped_existing": 0,
        "pending_public_msgs": 0,
        "pending_dm_msgs": 0,
        "raw_inserted": 0,
        "raw_skipped_existing": 0,
        "errors": [],
    }

    if not os.path.isdir(src_dir):
        stats["errors"].append(f"v1_dir_not_found:{src_dir}")
        return stats

    try:
        filenames = sorted(
            fname
            for fname in os.listdir(src_dir)
            if not fname.startswith(".") and fname.endswith(".json")
        )
    except OSError as exc:
        stats["errors"].append(f"v1_dir_list_error:{exc}")
        return stats

    # Phase A: Read & validate all v1 JSON files strictly read-only
    parsed_records: list[tuple[str, str, dict[str, Any]]] = []
    for fname in filenames:
        full_path = os.path.join(src_dir, fname)
        if not os.path.isfile(full_path):
            continue
        stats["files_scanned"] += 1
        try:
            with open(full_path, "r", encoding="utf-8") as fh:
                obj = json.load(fh)
        except Exception as exc:
            stats["errors"].append(f"json_read_error:{fname}:{exc}")
            continue
        if not isinstance(obj, dict):
            stats["errors"].append(f"invalid_json_root:{fname}")
            continue
        uid = db.sanitize_uid(obj.get("user_id") or fname[:-5])
        if not uid:
            stats["errors"].append(f"empty_uid:{fname}")
            continue
        parsed_records.append((fname, uid, obj))

    now = db.now_ts()
    seen_group_profiles: set[str] = set()

    # Dry-run path: zero DB writes, side-effect-free projection
    if dry_run:
        seen_mem_ids, seen_raw_hashes = _load_existing_ids_readonly(resolved_db)

        for _, uid, obj in parsed_records:
            stats["users_migrated"] += 1
            stats["public_profiles"] += 1

            seen_groups = _dedup_ordered(
                obj.get("seen_groups") if isinstance(obj.get("seen_groups"), list) else [],
                transform=db.sanitize_uid,
                limit=30,
            )
            for gid in seen_groups:
                seen_group_profiles.add(gid)

            pub = obj.get("public_profile") if isinstance(obj.get("public_profile"), dict) else {}
            dm = obj.get("dm_profile") if isinstance(obj.get("dm_profile"), dict) else {}
            pending_pub = (
                obj.get("pending_public_msgs")
                if isinstance(obj.get("pending_public_msgs"), list)
                else []
            )
            pending_dm = (
                obj.get("pending_dm_msgs")
                if isinstance(obj.get("pending_dm_msgs"), list)
                else []
            )

            dm_summary = str(dm.get("summary") or "").strip()
            dm_rel = str(dm.get("relationship") or "").strip()
            dm_facts = dm.get("facts") if isinstance(dm.get("facts"), list) else []
            dm_jokes = dm.get("inside_jokes") if isinstance(dm.get("inside_jokes"), list) else []
            if dm_summary or dm_rel or dm_facts or dm_jokes or pending_dm:
                stats["dm_profiles"] += 1

            def _sim_mem(scope: str, kind: str, text: str) -> None:
                mid = _v1_mem_id(uid, scope, kind, text)
                if mid in seen_mem_ids:
                    stats["memories_skipped_existing"] += 1
                else:
                    seen_mem_ids.add(mid)
                    stats["memories_inserted"] += 1

            for item in (pub.get("facts") if isinstance(pub.get("facts"), list) else []):
                text, cat, _ = _parse_fact_item(item)
                if not text:
                    continue
                stats["public_facts"] += 1
                kind = "episodic" if cat == "事件" else "stable"
                _sim_mem("user_public", kind, text)

            for item in (
                pub.get("inside_jokes") if isinstance(pub.get("inside_jokes"), list) else []
            ):
                text, _ = _parse_joke_item(item)
                if not text:
                    continue
                stats["public_jokes"] += 1
                _sim_mem("user_public", "joke", text)

            dm_scope = f"dm:{uid}"
            for item in dm_facts:
                text, cat, _ = _parse_fact_item(item)
                if not text:
                    continue
                stats["dm_facts"] += 1
                kind = "episodic" if cat == "事件" else "stable"
                _sim_mem(dm_scope, kind, text)

            for item in dm_jokes:
                text, _ = _parse_joke_item(item)
                if not text:
                    continue
                stats["dm_jokes"] += 1
                _sim_mem(dm_scope, "joke", text)

            def _sim_raw(chat_key: str, scope: str, role: str, entry_id: str, text: str) -> None:
                dh = _v1_raw_dedup(uid, scope, role, entry_id, text)
                rk = (chat_key, dh)
                if rk in seen_raw_hashes:
                    stats["raw_skipped_existing"] += 1
                else:
                    seen_raw_hashes.add(rk)
                    stats["raw_inserted"] += 1

            for idx, m in enumerate(pending_pub):
                if not isinstance(m, dict):
                    continue
                text = str(m.get("text") or "").strip()
                if not text:
                    continue
                stats["pending_public_msgs"] += 1
                gid = (
                    db.sanitize_uid(
                        m.get("group") or (seen_groups[-1] if seen_groups else "unknown")
                    )
                    or "unknown"
                )
                if gid != "unknown":
                    seen_group_profiles.add(gid)
                chat_key = f"g:{gid}"
                scope = f"group:{gid}"
                entry_id = str(m.get("id") or f"idx{idx}")
                _sim_raw(chat_key, scope, "user", entry_id, text)
                bot_reply = str(m.get("bot_reply") or "").strip()
                if bot_reply and bot_reply.upper() != "NO_REPLY":
                    _sim_raw(chat_key, scope, "assistant", entry_id, bot_reply)

            for idx, m in enumerate(pending_dm):
                if not isinstance(m, dict):
                    continue
                text = str(m.get("text") or "").strip()
                if not text:
                    continue
                stats["pending_dm_msgs"] += 1
                entry_id = str(m.get("id") or f"idx{idx}")
                _sim_raw(dm_scope, dm_scope, "user", entry_id, text)
                bot_reply = str(m.get("bot_reply") or "").strip()
                if bot_reply and bot_reply.upper() != "NO_REPLY":
                    _sim_raw(dm_scope, dm_scope, "assistant", entry_id, bot_reply)

        stats["group_profiles"] = len(seen_group_profiles)
        return stats

    # Phase B: Write to SQLite inside a single atomic BEGIN IMMEDIATE transaction
    with db.write_tx(resolved_db) as conn:
        pending_raw_rows: list[dict[str, Any]] = []

        # Helper to insert one pending raw message idempotently by (chat_key, dedup_hash)
        def _insert_raw(
            chat_key: str,
            scope: str,
            group_id: str,
            sender_uid: str,
            sender_name: str,
            role: str,
            text: str,
            dedup_hash: str,
            ts: int,
        ) -> None:
            exists = conn.execute(
                "SELECT 1 FROM raw_messages WHERE chat_key = ? AND dedup_hash = ? LIMIT 1",
                (chat_key, dedup_hash),
            ).fetchone()
            if exists is not None:
                stats["raw_skipped_existing"] += 1
                return
            conn.execute(
                "INSERT INTO raw_messages("
                "chat_key, scope, group_id, sender_uid, sender_name, role, text, "
                "dedup_hash, ts, distilled, window_id"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, '')",
                (
                    chat_key,
                    scope,
                    group_id,
                    sender_uid,
                    sender_name,
                    role,
                    text,
                    dedup_hash,
                    ts,
                ),
            )
            stats["raw_inserted"] += 1

        for _, uid, obj in parsed_records:
            first_seen_ts = db.parse_ts(obj.get("first_seen"), now) or now
            last_seen_ts = db.parse_ts(obj.get("last_seen"), first_seen_ts) or first_seen_ts
            msg_count = max(0, int(obj.get("msg_count") or 0))

            nicknames = _dedup_ordered(
                obj.get("nicknames") if isinstance(obj.get("nicknames"), list) else [],
                limit=8,
            )
            seen_groups = _dedup_ordered(
                obj.get("seen_groups") if isinstance(obj.get("seen_groups"), list) else [],
                transform=db.sanitize_uid,
                limit=30,
            )

            pub = obj.get("public_profile") if isinstance(obj.get("public_profile"), dict) else {}
            dm = obj.get("dm_profile") if isinstance(obj.get("dm_profile"), dict) else {}
            pending_pub = (
                obj.get("pending_public_msgs")
                if isinstance(obj.get("pending_public_msgs"), list)
                else []
            )
            pending_dm = (
                obj.get("pending_dm_msgs")
                if isinstance(obj.get("pending_dm_msgs"), list)
                else []
            )

            # 1. Upsert user_public profile
            _upsert_profile_idempotent(
                conn=conn,
                profile_key=f"user_public:{uid}",
                scope="user_public",
                entity_id=uid,
                nicknames=nicknames,
                seen_groups=seen_groups,
                relationship=str(pub.get("relationship") or "").strip(),
                summary=str(pub.get("summary") or "").strip(),
                msg_count=msg_count,
                first_seen_ts=first_seen_ts,
                last_seen_ts=last_seen_ts,
                updated_at=last_seen_ts,
            )
            stats["users_migrated"] += 1
            stats["public_profiles"] += 1

            # 2. Upsert DM profile if any DM data exists (strict DM isolation)
            dm_summary = str(dm.get("summary") or "").strip()
            dm_rel = str(dm.get("relationship") or "").strip()
            dm_facts = dm.get("facts") if isinstance(dm.get("facts"), list) else []
            dm_jokes = dm.get("inside_jokes") if isinstance(dm.get("inside_jokes"), list) else []
            dm_scope = f"dm:{uid}"
            if dm_summary or dm_rel or dm_facts or dm_jokes or pending_dm:
                _upsert_profile_idempotent(
                    conn=conn,
                    profile_key=f"dm:{uid}",
                    scope=dm_scope,
                    entity_id=uid,
                    nicknames=nicknames,
                    seen_groups=[],
                    relationship=dm_rel,
                    summary=dm_summary,
                    msg_count=len(pending_dm),
                    first_seen_ts=first_seen_ts,
                    last_seen_ts=last_seen_ts,
                    updated_at=last_seen_ts,
                )
                stats["dm_profiles"] += 1

            # 3. Ensure group:<gid> profile rows exist for seen_groups
            for gid in seen_groups:
                seen_group_profiles.add(gid)
                conn.execute(
                    "INSERT OR IGNORE INTO profiles("
                    "profile_key, scope, entity_id, nicknames_json, seen_groups_json, "
                    "relationship, summary, msg_count, first_seen_ts, last_seen_ts, updated_at"
                    ") VALUES (?, ?, ?, '[]', '[]', '', '', 0, ?, ?, ?)",
                    (f"group:{gid}", f"group:{gid}", gid, first_seen_ts, last_seen_ts, last_seen_ts),
                )

            # Helper to insert one v1 memory idempotently + sync FTS5
            def _insert_mem(
                scope: str,
                kind: str,
                category: str,
                text: str,
                occurred_at: str,
            ) -> None:
                mem_id = _v1_mem_id(uid, scope, kind, text)
                keywords = tokenizer.auto_keywords(text)
                keywords_json = db.json_dumps_compact(keywords)
                item_ts = db.parse_ts(occurred_at, 0) or last_seen_ts or now
                hist_json = db.json_dumps_compact([{"action": "migrated_v1", "ts": item_ts}])

                cur = conn.execute(
                    "INSERT OR IGNORE INTO memories("
                    "id, scope, owner_uid, group_id, kind, category, fact_key, text, "
                    "keywords_json, importance, proof_count, occurred_at, "
                    "last_mentioned_ts, last_recalled_ts, status, superseded_by, "
                    "history_json, source_window_id, embedding, created_at, updated_at"
                    ") VALUES (?, ?, ?, '', ?, ?, '', ?, ?, 3, 1, ?, ?, 0, 'active', '', ?, 'v1_migrate', NULL, ?, ?)",
                    (
                        mem_id,
                        scope,
                        uid,
                        kind,
                        category,
                        text,
                        keywords_json,
                        occurred_at,
                        item_ts,
                        hist_json,
                        item_ts,
                        item_ts,
                    ),
                )
                if cur.rowcount and cur.rowcount > 0:
                    db.sync_memory_fts(
                        conn,
                        mem_id=mem_id,
                        scope=scope,
                        owner_uid=uid,
                        group_id="",
                        text=text,
                        keywords=keywords,
                    )
                    stats["memories_inserted"] += 1
                else:
                    stats["memories_skipped_existing"] += 1

            # 4. Migrate public_profile.facts & inside_jokes
            pub_facts = pub.get("facts") if isinstance(pub.get("facts"), list) else []
            for item in pub_facts:
                text, cat, occ = _parse_fact_item(item)
                if not text:
                    continue
                stats["public_facts"] += 1
                kind = "episodic" if cat == "事件" else "stable"
                _insert_mem("user_public", kind, cat, text, occ)

            pub_jokes = pub.get("inside_jokes") if isinstance(pub.get("inside_jokes"), list) else []
            for item in pub_jokes:
                text, occ = _parse_joke_item(item)
                if not text:
                    continue
                stats["public_jokes"] += 1
                _insert_mem("user_public", "joke", "内部梗", text, occ)

            # 5. Migrate dm_profile.facts & inside_jokes (isolated under dm:<uid>)
            for item in dm_facts:
                text, cat, occ = _parse_fact_item(item)
                if not text:
                    continue
                stats["dm_facts"] += 1
                kind = "episodic" if cat == "事件" else "stable"
                _insert_mem(dm_scope, kind, cat, text, occ)

            for item in dm_jokes:
                text, occ = _parse_joke_item(item)
                if not text:
                    continue
                stats["dm_jokes"] += 1
                _insert_mem(dm_scope, "joke", "内部梗", text, occ)

            default_nick = nicknames[-1] if nicknames else ""

            # 6. Collect pending_public_msgs for chronologically sorted insertion
            for idx, m in enumerate(pending_pub):
                if not isinstance(m, dict):
                    continue
                text = str(m.get("text") or "").strip()
                if not text:
                    continue
                stats["pending_public_msgs"] += 1
                gid = (
                    db.sanitize_uid(
                        m.get("group") or (seen_groups[-1] if seen_groups else "unknown")
                    )
                    or "unknown"
                )
                if gid != "unknown" and gid not in seen_group_profiles:
                    seen_group_profiles.add(gid)
                    conn.execute(
                        "INSERT OR IGNORE INTO profiles("
                        "profile_key, scope, entity_id, nicknames_json, seen_groups_json, "
                        "relationship, summary, msg_count, first_seen_ts, last_seen_ts, updated_at"
                        ") VALUES (?, ?, ?, '[]', '[]', '', '', 0, ?, ?, ?)",
                        (
                            f"group:{gid}",
                            f"group:{gid}",
                            gid,
                            first_seen_ts,
                            last_seen_ts,
                            last_seen_ts,
                        ),
                    )
                chat_key = f"g:{gid}"
                scope = f"group:{gid}"
                sender_name = str(m.get("name") or default_nick).strip()
                entry_id = str(m.get("id") or f"idx{idx}")
                msg_ts = db.parse_ts(m.get("ts"), last_seen_ts) or last_seen_ts or now
                user_dedup = _v1_raw_dedup(uid, scope, "user", entry_id, text)
                pending_raw_rows.append(
                    {
                        "chat_key": chat_key,
                        "scope": scope,
                        "group_id": gid,
                        "sender_uid": uid,
                        "sender_name": sender_name,
                        "role": "user",
                        "text": text,
                        "dedup_hash": user_dedup,
                        "ts": msg_ts,
                    }
                )
                bot_reply = str(m.get("bot_reply") or "").strip()
                if bot_reply and bot_reply.upper() != "NO_REPLY":
                    bot_dedup = _v1_raw_dedup(uid, scope, "assistant", entry_id, bot_reply)
                    pending_raw_rows.append(
                        {
                            "chat_key": chat_key,
                            "scope": scope,
                            "group_id": gid,
                            "sender_uid": "bot",
                            "sender_name": "bot",
                            "role": "assistant",
                            "text": bot_reply,
                            "dedup_hash": bot_dedup,
                            "ts": msg_ts + 1,
                        }
                    )

            # 7. Collect pending_dm_msgs for chronologically sorted insertion
            for idx, m in enumerate(pending_dm):
                if not isinstance(m, dict):
                    continue
                text = str(m.get("text") or "").strip()
                if not text:
                    continue
                stats["pending_dm_msgs"] += 1
                chat_key = f"dm:{uid}"
                sender_name = str(m.get("name") or default_nick).strip()
                entry_id = str(m.get("id") or f"idx{idx}")
                msg_ts = db.parse_ts(m.get("ts"), last_seen_ts) or last_seen_ts or now
                user_dedup = _v1_raw_dedup(uid, dm_scope, "user", entry_id, text)
                pending_raw_rows.append(
                    {
                        "chat_key": chat_key,
                        "scope": dm_scope,
                        "group_id": "",
                        "sender_uid": uid,
                        "sender_name": sender_name,
                        "role": "user",
                        "text": text,
                        "dedup_hash": user_dedup,
                        "ts": msg_ts,
                    }
                )
                bot_reply = str(m.get("bot_reply") or "").strip()
                if bot_reply and bot_reply.upper() != "NO_REPLY":
                    bot_dedup = _v1_raw_dedup(uid, dm_scope, "assistant", entry_id, bot_reply)
                    pending_raw_rows.append(
                        {
                            "chat_key": chat_key,
                            "scope": dm_scope,
                            "group_id": "",
                            "sender_uid": "bot",
                            "sender_name": "bot",
                            "role": "assistant",
                            "text": bot_reply,
                            "dedup_hash": bot_dedup,
                            "ts": msg_ts + 1,
                        }
                    )

            # 8. Transfer non-zero consolidation timestamp metadata
            pub_cons = db.parse_ts(obj.get("last_consolidated_public_at"), 0)
            if pub_cons > 0:
                conn.execute(
                    "INSERT OR IGNORE INTO meta_kv(key, value, updated_at) VALUES (?, ?, ?)",
                    (f"v1_last_consolidated_public:{uid}", str(pub_cons), pub_cons),
                )
            dm_cons = db.parse_ts(obj.get("last_consolidated_dm_at"), 0)
            if dm_cons > 0:
                conn.execute(
                    "INSERT OR IGNORE INTO meta_kv(key, value, updated_at) VALUES (?, ?, ?)",
                    (f"v1_last_consolidated_dm:{uid}", str(dm_cons), dm_cons),
                )
                conn.execute(
                    "INSERT OR IGNORE INTO meta_kv(key, value, updated_at) VALUES (?, ?, ?)",
                    (f"last_consolidated:dm:{uid}", str(dm_cons), dm_cons),
                )

        # Insert all collected pending raw messages in chronological order across all users
        pending_raw_rows.sort(
            key=lambda item: (
                int(item["ts"]),
                str(item["chat_key"]),
                str(item["sender_uid"]),
                str(item["role"]),
            )
        )
        for row in pending_raw_rows:
            _insert_raw(
                chat_key=str(row["chat_key"]),
                scope=str(row["scope"]),
                group_id=str(row["group_id"]),
                sender_uid=str(row["sender_uid"]),
                sender_name=str(row["sender_name"]),
                role=str(row["role"]),
                text=str(row["text"]),
                dedup_hash=str(row["dedup_hash"]),
                ts=int(row["ts"]),
            )

        stats["group_profiles"] = len(seen_group_profiles)

        db.log_audit(
            conn,
            run_type="migrate",
            chat_key="",
            action="migrate_v1",
            target_id="",
            detail=stats,
            created_at=now,
        )

    return stats


# ---------------------------------------------------------------------------
# CLI Entry Point
# ---------------------------------------------------------------------------

def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Lossless, idempotent migration from v1 user_memories JSON to SQLite v2."
    )
    parser.add_argument(
        "--v1-dir",
        default=None,
        help="Path to v1 user_memories directory (defaults to USER_MEMORY_DIR or data/user_memories).",
    )
    parser.add_argument(
        "--db-path",
        default=None,
        help="Target SQLite v2 database path (defaults to MEMORY_DB_PATH or data/memory.db).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Inspect v1 JSON files and report projected migration stats without modifying the DB.",
    )
    args = parser.parse_args(argv)
    report = migrate_v1_directory(
        v1_dir=args.v1_dir,
        db_path=args.db_path,
        dry_run=args.dry_run,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not report.get("errors") else 1


if __name__ == "__main__":
    raise SystemExit(_main())


__all__ = [
    "default_v1_dir",
    "migrate_v1_directory",
]
