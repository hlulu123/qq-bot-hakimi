"""Shared low-signal detection for onebot-ws-guard.py and agy-acp-wrapper.py.

Both processes decide whether an unaddressed (lurking) group message is worth
waking the model for. They used to keep two hand-written regexes that missed
"?", "？", "…" and pure emoji; this module is the single source of truth.

Stdlib only (the guard and the wrapper both run on the system python3).

Env knobs (read once at import; both processes load bot.env first):
  LOW_SIGNAL_FILLER=1        also treat pure filler words (哈哈哈 / 草 / 666 / 嗯 …) as noise
  LOW_SIGNAL_EXTRA_RE=...    extra full-match regex, applied after stripping punctuation
"""

import os
import re
import unicodedata

# Unicode general categories that never carry chat content on their own:
#   P* punctuation (incl. ? ？ … 「」), S* symbols (emoji are So, skin tones Sk,
#   ~ ^ are Sm/Sk), Z* separators, Cf format (ZWJ), Mn/Me combining marks
#   (VS16 U+FE0F, keycap U+20E3), Cc control chars.
_NOISE_MAJOR = ("P", "S", "Z")
_NOISE_MINOR = ("Cf", "Mn", "Me", "Cc", "Co")

# Bracketed placeholders cc-connect / NapCat render for non-text segments
MEDIA_NOISE_ONLY_RE = re.compile(
    r"^(?:\[(?:语音|文件(?::[^\]]*)?|卡片消息|合并转发|视频|Audio|Voice|Video)\]\s*)+$",
    re.IGNORECASE,
)
_FACE_PLACEHOLDER_RE = re.compile(
    r"\[(?:表情|动画表情|QQ表情|超级表情|戳一戳|face)(?:[:：][^\]\n]{0,16})?\]",
    re.IGNORECASE,
)

# Pure reaction words: laughter, "草", "666", acknowledgements. Matched after
# punctuation / emoji / spaces are removed, case-insensitive.
FILLER_ONLY_RE = re.compile(
    r"^(?:"
    r"[哈呵嘿嘻嗨h]{2,}|[哈呵]+[哈呵嘿嘻h]*"
    r"|[草艹]+|w{2,}|6{2,}|1{3,}|2{3,}|0{3,}"
    r"|[嗯恩哦噢喔啊呃额诶欸唉哎嗷啧呜]+"
    r"|ok+|okk+|好+|好的|行|收到|确实|对|是的|xs|hhh+|lol+|233+"
    r")$",
    re.IGNORECASE,
)


def _env_flag(name, default):
    return os.environ.get(name, default).strip().lower() not in ("", "0", "false", "no", "off")


FILLER_ENABLED = _env_flag("LOW_SIGNAL_FILLER", "1")
_extra_src = os.environ.get("LOW_SIGNAL_EXTRA_RE", "").strip()
try:
    EXTRA_RE = re.compile(_extra_src, re.IGNORECASE) if _extra_src else None
except re.error:
    EXTRA_RE = None


def _is_noise_char(ch):
    cat = unicodedata.category(ch)
    return cat[0] in _NOISE_MAJOR or cat in _NOISE_MINOR


def strip_noise_chars(text):
    """Drop punctuation / symbols / emoji / whitespace, keep letters, digits, CJK."""
    return "".join(ch for ch in (text or "") if not _is_noise_char(ch))


def is_punct_or_emoji_only(text):
    """True for '', '?', '？？？', '…', '😂😂', '👍🏻', '~', '(＾▽＾)' without letters, …"""
    return not strip_noise_chars(text)


def is_media_noise_only(text):
    return bool(MEDIA_NOISE_ONLY_RE.match((text or "").strip()))


def is_filler_only(text):
    core = strip_noise_chars(_FACE_PLACEHOLDER_RE.sub("", text or ""))
    if not core:
        return True
    if FILLER_ENABLED and FILLER_ONLY_RE.match(core):
        return True
    if EXTRA_RE is not None and EXTRA_RE.fullmatch(core):
        return True
    return False


def is_low_signal_text(text):
    """Whether a lurking-path text body is not worth waking the model for.

    Only call this on messages that do NOT address the bot (no @, no reply,
    no name/soft call); addressed messages always go through.
    """
    stripped = (text or "").strip()
    if not stripped:
        return True
    if is_media_noise_only(stripped):
        return True
    if is_punct_or_emoji_only(_FACE_PLACEHOLDER_RE.sub("", stripped)):
        return True
    return is_filler_only(stripped)
