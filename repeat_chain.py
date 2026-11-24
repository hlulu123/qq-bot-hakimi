"""Repeat chains (复读) shared by onebot-ws-guard.py and agy-acp-wrapper.py.

When enough group members post the same message back to back, ws-guard hands
that message to the model (skipping the usual lurking pace), the wrapper tells
the model the group is repeating, and the model decides: reply with exactly
FOLLOW_TOKEN to join in, or NO_REPLY. ws-guard then swaps the token for an
exact copy of the repeated message (text, QQ face or sticker), so the model
never has to retype it and can't alter it.
"""

import os
import re

import bot_env  # noqa: F401  (loads bot.env before any setting is read)


def _flag(name, default):
    return os.environ.get(name, default).strip().lower() not in ("", "0", "false", "no", "off")


def _words(name, default=""):
    return [x.strip().lower() for x in os.environ.get(name, default).split(",") if x.strip()]


ENABLED = _flag("REPEAT_ENABLED", "1")
# Offer the chain to the model once this many identical messages are in a row
MIN_COUNT = max(2, int(os.environ.get("REPEAT_MIN_COUNT", "2")))
# Two identical messages further apart than this don't continue a chain
WINDOW_SECS = float(os.environ.get("REPEAT_WINDOW_SECS", "180"))
# At most one offer per group in this many seconds
COOLDOWN_SECS = float(os.environ.get("REPEAT_COOLDOWN_SECS", "60"))
# Only text up to this many characters
MAX_CHARS = int(os.environ.get("REPEAT_MAX_CHARS", "60"))
# Ordinary photos (not stickers) can be repeated too
PHOTOS = _flag("REPEAT_PHOTOS", "1")
# Never offer text containing any of these words
BLOCK_WORDS = _words("REPEAT_BLOCK_WORDS")

# The model's whole reply when it decides to join the chain
FOLLOW_TOKEN = "FOLLOW_REPEAT"

_CALL_WORDS = _words("BOT_CALL_NAMES", "哈基米,及其猫,机器猫,本猫,紫毛猫,紫发猫") + _words(
    "BOT_SOFT_CALL_CUES", "猫猫,小猫,小紫,紫毛,紫发,那只猫,某猫,咪咪,哈基,及其,猫娘"
)
_LOW_SIGNAL_RE = re.compile(r"^[\s\.,。，!！~～\-_+=()（）\[\]【】]*$")
_UNSAFE_RE = re.compile(r"https?://|www\.|[@＠]", re.IGNORECASE)
# Long digit runs look like QQ / phone numbers; "666666" style runs are fine
_DIGIT_RUN_RE = re.compile(r"\d{6,}")


def _cq_unescape(text):
    return text.replace("&#91;", "[").replace("&#93;", "]").replace("&amp;", "&")


def _text_ok(text):
    if not text or len(text) > MAX_CHARS or text.startswith("/"):
        return False
    if _LOW_SIGNAL_RE.match(text) or _UNSAFE_RE.search(text):
        return False
    if any(len(set(run)) > 1 for run in _DIGIT_RUN_RE.findall(text)):
        return False
    low = text.lower()
    # Calling the bot is a normal turn, not a repeat chain
    if any(w in low for w in _CALL_WORDS):
        return False
    return not any(w in low for w in BLOCK_WORDS)


def signature(message):
    """(key, segments_to_send) for a message that can be repeated, else None.

    key compares two messages for "the same content"; segments are an exact copy
    to send. Only text, QQ faces, stickers and photos qualify: anything with an
    @, a reply, a file, voice, card or forward is never repeated.
    """
    if not ENABLED:
        return None
    if isinstance(message, str):
        if "[CQ:" in message:
            return None
        message = [{"type": "text", "data": {"text": _cq_unescape(message)}}]
    if not isinstance(message, list) or not message:
        return None

    items = []  # [key_part, segment_to_send]
    for seg in message:
        if not isinstance(seg, dict):
            return None
        st = seg.get("type", "")
        dt = seg.get("data") or {}
        if st == "text":
            t = str(dt.get("text", ""))
            if items and items[-1][0][0] == "t":
                items[-1][1]["data"]["text"] += t
            else:
                items.append([("t",), {"type": "text", "data": {"text": t}}])
        elif st == "face":
            fid = str(dt.get("id", "")).strip()
            if not fid:
                return None
            items.append([("f", fid), {"type": "face", "data": {"id": fid}}])
        elif st == "mface":
            eid = str(dt.get("emoji_id", "")).strip()
            if not eid or not dt.get("key"):
                return None
            data = {k: dt[k] for k in ("emoji_id", "emoji_package_id", "key", "summary") if k in dt}
            items.append([("m", eid), {"type": "mface", "data": data}])
        elif st == "image":
            summary = str(dt.get("summary") or "").strip()
            is_sticker = str(dt.get("sub_type", "")) == "1" or "表情" in summary
            fname = str(dt.get("file") or "").strip().lower()
            url = str(dt.get("url") or "").strip()
            if not (is_sticker or PHOTOS) or not fname or not url.startswith("http"):
                return None
            data = {"file": url, "sub_type": 1 if is_sticker else 0}
            if summary:
                data["summary"] = summary
            items.append([("s" if is_sticker else "p", fname), {"type": "image", "data": data}])
        else:
            return None

    # Trim outer whitespace so "666 " and "666" count as the same message
    if items and items[0][0][0] == "t":
        items[0][1]["data"]["text"] = items[0][1]["data"]["text"].lstrip()
    if items and items[-1][0][0] == "t":
        items[-1][1]["data"]["text"] = items[-1][1]["data"]["text"].rstrip()
    items = [it for it in items if it[0][0] != "t" or it[1]["data"]["text"]]
    if not items:
        return None

    text = "".join(seg["data"]["text"] for kp, seg in items if kp[0] == "t").strip()
    if text and not _text_ok(text):
        return None
    key = tuple(("t", seg["data"]["text"]) if kp[0] == "t" else kp for kp, seg in items)
    return key, [seg for _, seg in items]


_KIND_LABEL = {"f": "[QQ表情]", "m": "[表情包]", "s": "[表情包]", "p": "[图片]"}


def describe(key):
    """Human-readable form of a chain key, for the model's hint and logs."""
    return "".join(k[1] if k[0] == "t" else _KIND_LABEL.get(k[0], "[表情]") for k in key)


def run_in_history(messages, sender_id, bot_id=""):
    """The repeat run that sender_id's latest message belongs to, from group history.

    messages: NapCat get_group_msg_history list, oldest first. Returns
    {"key", "text", "count", "users", "bot_in_run"} or None. count/users only
    cover people; bot_in_run is True if the bot itself already joined the run.
    """
    if not messages or not sender_id:
        return None
    target = None
    for idx in range(len(messages) - 1, max(-1, len(messages) - 9), -1):
        if str(messages[idx].get("user_id", "")) == str(sender_id):
            target = idx
            break
    if target is None:
        return None
    sig = signature(messages[target].get("message"))
    if not sig:
        return None
    key = sig[0]

    def same(a, b):
        # b continues a's run: same content, and not too long after it
        sb = signature(messages[b].get("message"))
        if not sb or sb[0] != key:
            return False
        ta, tb = messages[a].get("time"), messages[b].get("time")
        if isinstance(ta, (int, float)) and isinstance(tb, (int, float)):
            return abs(tb - ta) <= WINDOW_SECS
        return True

    lo = target
    while lo > 0 and same(lo, lo - 1):
        lo -= 1
    hi = target
    while hi + 1 < len(messages) and same(hi, hi + 1):
        hi += 1

    users, count, bot_in_run = set(), 0, False
    for m in messages[lo:hi + 1]:
        uid = str(m.get("user_id", ""))
        if bot_id and uid == str(bot_id):
            bot_in_run = True
            continue
        count += 1
        users.add(uid)
    return {"key": key, "text": describe(key), "count": count, "users": len(users), "bot_in_run": bot_in_run}


def is_offer(run):
    """True if this run is long enough to ask the model whether to join in."""
    return bool(
        ENABLED and run and run["count"] >= MIN_COUNT and run["users"] >= 2 and not run["bot_in_run"]
    )
