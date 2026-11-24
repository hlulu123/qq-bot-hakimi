"""Per-session prompt de-duplication for agy-acp-wrapper.py.

The ACP session is stateful: everything the wrapper puts into a prompt stays in
the model's context until the next compaction. Re-sending the same 30 group
messages and the same ~1,800 characters of rules on every turn therefore piles
up in the context instead of informing the model. This module tracks, per ACP
session, what the model has already been given:

- GroupHistoryCursor: which group messages the model has seen, and which
  "name = QQ number" roster entries it has been told. A turn then only carries
  the messages that arrived since the model last looked, with a short name per
  line instead of "name(10-digit QQ)".
- StaticBlockDedup: digests of rule blocks (permissions, QQ actions, history
  lookup syntax, reply style, listening-mode rules). A block is sent the first
  time, when its text changes, and on a periodic refresh; otherwise the
  one-line [本轮状态] header points the model at the copy already in context.

Nothing here talks to NapCat or the model; the wrapper builds the inputs and
applies the returned "pending" commits only after the model actually
processed the turn (a turn that failed upstream is simply re-sent in full).
After a compaction or a new session the wrapper calls reset() and the next
turn is a full one again.

Env knobs:
  BOT_PROMPT_MODE=lean|full     lean (default) de-duplicates rule blocks; full sends them every turn
  BOT_HISTORY_DELTA=1           group history as increments (0 = old full 30-line block every turn)
  BOT_PROMPT_REFRESH_TURNS=30   re-send every rule block after this many lean turns (0 = never)
"""

import collections
import hashlib
import os
import re


def _env_flag(name, default):
    return os.environ.get(name, default).strip().lower() not in ("", "0", "false", "no", "off")


PROMPT_MODE = (os.environ.get("BOT_PROMPT_MODE", "lean").strip().lower() or "lean")
if PROMPT_MODE not in ("lean", "full"):
    PROMPT_MODE = "lean"
HISTORY_DELTA = _env_flag("BOT_HISTORY_DELTA", "1")
try:
    REFRESH_TURNS = max(0, int(os.environ.get("BOT_PROMPT_REFRESH_TURNS", "30")))
except ValueError:
    REFRESH_TURNS = 30

_WS_RE = re.compile(r"\s+")


def digest(text):
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Group history
# ---------------------------------------------------------------------------

def make_entry(key, uid, name, text, time_str="", tag="", mid_prefix=""):
    """One rendered group message.

    key: stable id (OneBot message_id, or a synthetic one); tag: "self" for the
    bot's own lines, "admin" for administrators, "" otherwise; mid_prefix:
    "[#123] " when message ids are shown to the model, else "".
    """
    return {
        "key": str(key),
        "uid": str(uid or ""),
        "name": _WS_RE.sub(" ", str(name or uid or "")).strip() or str(uid or "?"),
        "text": text,
        "time": time_str or "",
        "tag": tag or "",
        "mid_prefix": mid_prefix or "",
    }


class GroupHistoryCursor:
    MAX_SEEN = 600

    def __init__(self):
        self.seen = collections.OrderedDict()
        self.roster = {}  # uid -> label the model was last told
        self.last_time = ""
        self.needs_full = True

    def reset(self):
        self.seen.clear()
        self.roster.clear()
        self.last_time = ""
        self.needs_full = True

    def commit(self, pending):
        if not pending:
            return
        if pending.get("full"):
            self.roster = {}
        for k in pending.get("keys", ()):
            self.seen.pop(k, None)
            self.seen[k] = True
        while len(self.seen) > self.MAX_SEEN:
            self.seen.popitem(last=False)
        self.roster.update(pending.get("roster") or {})
        if pending.get("last_time") is not None:
            self.last_time = pending["last_time"]
        self.needs_full = False


def _assign_labels(entries, roster):
    """uid -> short label; names shared by several people get a #last-4 suffix."""
    uids_by_name = collections.defaultdict(set)
    name_of = {}
    for e in entries:
        if e["tag"] == "self" or not e["uid"]:
            continue
        name_of[e["uid"]] = e["name"]
        uids_by_name[e["name"]].add(e["uid"])
    for uid, label in roster.items():
        base = label.split("#", 1)[0]
        if uid not in name_of:
            uids_by_name[base].add(uid)
    labels = {}
    for uid, name in name_of.items():
        labels[uid] = f"{name}#{uid[-4:]}" if len(uids_by_name[name]) > 1 else name
    return labels


def render_group_history(entries, cursor, title, current_key="", full_limit=30, delta=True):
    """Render the group-history section for one turn.

    Returns (text, pending). text may be "" (nothing new); pending must be
    passed to cursor.commit() once the model has processed the turn.
    """
    full_limit = max(1, int(full_limit or 1))
    visible = [e for e in entries if e["key"] != current_key]
    full_mode = (not delta) or cursor.needs_full or not cursor.seen
    gap = False
    if full_mode:
        show = visible[-full_limit:]
    else:
        overlap = any(e["key"] in cursor.seen for e in entries)
        show = [e for e in visible if e["key"] not in cursor.seen]
        gap = bool(show) and not overlap
        if len(show) > full_limit:
            show = show[-full_limit:]
            gap = True

    labels = _assign_labels(entries, {} if full_mode else cursor.roster)
    roster_add = {}
    for e in show:
        if e["tag"] == "self" or not e["uid"]:
            continue
        label = labels.get(e["uid"], e["name"])
        if full_mode or cursor.roster.get(e["uid"]) != label:
            roster_add[e["uid"]] = label

    prev_time = "" if full_mode else cursor.last_time
    lines = []
    for e in show:
        who = "你" if e["tag"] == "self" else labels.get(e["uid"], e["name"])
        prefix = ""
        if e["time"] and e["time"] != prev_time:
            prefix = f"[{e['time']}] "
            prev_time = e["time"]
        lines.append(f"- {prefix}{e['mid_prefix']}{who}: {e['text']}")

    pending = {
        "keys": [e["key"] for e in entries],
        "roster": roster_add,
        "full": full_mode,
        "last_time": prev_time,
    }
    if not lines:
        return "", pending

    roster_txt = "，".join(
        f"{label}={uid}" + ("(管理员)" if any(x["uid"] == uid and x["tag"] == "admin" for x in show) else "")
        for uid, label in roster_add.items()
    )
    out = []
    if full_mode:
        out.append(
            f"[群聊近期消息记录 — {title}，按时间先后排列，请严格区分不同发言者与对话对象；"
            "“你”是你自己发的，名字对应的 QQ 号见群成员表]"
        )
        if roster_txt:
            out.append(f"[群成员: {roster_txt}]")
        out.extend(lines)
        out.append("[以上为近期群聊上下文；下面是对你发出的当前消息]")
    else:
        note = "，更早还有没显示的消息，需要时可翻记录" if gap else ""
        out.append(f"[群聊新消息 — 你上一轮之后新增的 {len(lines)} 条{note}]")
        if roster_txt:
            out.append(f"[群成员新增: {roster_txt}]")
        out.extend(lines)
    return "\n".join(out), pending


# ---------------------------------------------------------------------------
# Static rule blocks
# ---------------------------------------------------------------------------

class StaticBlockDedup:
    def __init__(self):
        self.sent = {}  # key -> digest of the copy the model already has
        self.lean_turns = 0

    def reset(self):
        self.sent.clear()
        self.lean_turns = 0

    def due_refresh(self, refresh_turns=REFRESH_TURNS):
        return bool(refresh_turns) and self.lean_turns >= refresh_turns

    def select(self, items, force=False):
        """items: [(key_or_None, text)]. key None = always send.

        Returns (texts_to_send, pending). Same key + same text as the copy
        already sent -> dropped (unless force).
        """
        texts = []
        pending = {}
        for key, text in items:
            if not text:
                continue
            if key is None:
                texts.append(text)
                continue
            d = digest(text)
            if not force and self.sent.get(key) == d:
                continue
            texts.append(text)
            pending[key] = d
        return texts, {"blocks": pending, "forced": bool(force)}

    def commit(self, pending):
        if not pending:
            return
        self.sent.update(pending.get("blocks") or {})
        if pending.get("forced"):
            self.lean_turns = 0
        else:
            self.lean_turns += 1


class SessionDiet:
    """Everything the model of one ACP session has already been told."""

    def __init__(self):
        self.history = GroupHistoryCursor()
        self.static = StaticBlockDedup()

    def reset(self):
        self.history.reset()
        self.static.reset()

    def commit(self, pending):
        if not pending:
            return
        self.history.commit(pending.get("history"))
        self.static.commit(pending.get("static"))
