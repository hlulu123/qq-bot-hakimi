#!/usr/bin/env python3
"""Memory write guardrails and synthetic prompt block filtering for QQ Bot SQLite v2 Memory Engine.

Implements the 4 Hard Guardrails plus ingestion hygiene filters using ONLY the
Python 3 standard library:
  1. Sensitive-data & write-scope boundary blocker
  2. Write quality gate (0..100, reject < 40)
  3. Fact key normalization & universal catch-all key blocker
  4. Near-duplicate detection & downgrade to reinforce
  +  Synthetic prompt wrapper block stripping & unworthy-text filter (for ingest.py)
"""

from __future__ import annotations

import difflib
import re

from . import db, tokenizer

# ---------------------------------------------------------------------------
# Prompt Tag / Fence Neutralization Helpers (Shared Contract)
# ---------------------------------------------------------------------------
# Canonical bracket tags injected by wrapper/history/memory/plugins.
# Any occurrence inside untrusted chat text, nicknames, profiles, or memories
# is neutralized by replacing the leading `[` with full-width `［`.
_FAKE_PROMPT_TAG_RE = re.compile(
    r"\[(?=\s*(?:"
    r"cc-connect"
    r"|权限边界|权限身份|权限"
    r"|QQ动作权限|可用QQ操作"
    r"|待审入群申请"
    r"|群聊近期消息记录|群聊触发状态|群聊监听状态|群聊复读|群聊新消息|群聊"
    r"|近期私聊消息ID|近期私聊消息记录|近期私聊消息"
    r"|视觉附件"
    r"|回复风格补充|回复风格"
    r"|相关用户长期记忆|近期对话续接|跨群动态与记忆速览"
    r"|当前引用较早消息"
    r"|历史记录查阅|历史聊天记录"
    r"|上下文提醒"
    r"|插件提示"
    r"|本轮状态|状态说明|群成员"
    r"|以上为近期群聊上下文"
    r"))",
    re.IGNORECASE,
)

_QQ_DIRECTIVE_OPEN_RE = re.compile(r"\[\[\s*(?=QQ\s*:)", re.IGNORECASE)


def neutralize_prompt_text(text: str) -> str:
    """Neutralize fake system bracket tags, [[QQ:...]] directives, and prompt fence markers.

    Guarantees that `<<<UNTRUSTED_CHAT_LOG_START>>>`, `<<<UNTRUSTED_CHAT_LOG_END>>>`,
    `[[QQ:...]]`, and forged system `[标签...]` prefixes never survive verbatim.
    """
    if not text:
        return ""
    s = str(text)
    s = _QQ_DIRECTIVE_OPEN_RE.sub("［［", s)
    s = _FAKE_PROMPT_TAG_RE.sub("［", s)
    if "<<<" in s:
        s = s.replace("<<<", "＜＜＜")
    if ">>>" in s:
        s = s.replace(">>>", "＞＞＞")
    return s


def sanitize_prompt_singleline(text: str, max_len: int = 0) -> str:
    """Collapse CR/LF and whitespace to a single line, neutralize tags/fences, and optionally cap length."""
    if not text:
        return ""
    s = re.sub(r"\s+", " ", str(text)).strip()
    if not s:
        return ""
    s = neutralize_prompt_text(s)
    if max_len and max_len > 0 and len(s) > max_len:
        s = s[:max_len].rstrip()
    return s

# ---------------------------------------------------------------------------
# Guardrail 1 — Sensitive Content Regex & Helpers
# ---------------------------------------------------------------------------
SENSITIVE_MEMORY_RE = re.compile(
    r"(?:"
    # Local Unix / macOS / Linux paths
    r"/Users/|/home/|/etc/|/var/|/tmp/|/root/|/private/|"
    r"(?:^|(?<=\s))~[\\/][A-Za-z0-9_.-]|"
    # Windows drive paths, UNC paths, and env vars
    r"(?<![A-Za-z0-9_])[A-Za-z]:[\\/]|\\\\[A-Za-z0-9_.\-]+\\|"
    r"%(?:USERPROFILE|APPDATA|LOCALAPPDATA|TEMP|WINDIR)%|"
    # URLs & transport schemes
    r"file://|https?://|wss?://|ssh://|"
    # Local / LAN IP addresses and localhost
    r"127\.0\.0\.1|(?<![A-Za-z0-9_])localhost(?![A-Za-z0-9_])|0\.0\.0\.0|"
    r"192\.168\.|(?<!\d)10\.\d{1,3}\.\d{1,3}\.\d{1,3}(?!\d)|"
    r"(?<!\d)172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}(?!\d)|"
    # API key / secret prefixes (e.g. sk-..., ghp_..., JWT eyJ..., Bearer ...)
    r"(?<![A-Za-z0-9_])sk-[A-Za-z0-9_-]{10,}|"
    r"(?<![A-Za-z0-9_])ghp_[A-Za-z0-9]{20,}|"
    r"(?<![A-Za-z0-9_])eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}|"
    r"(?<![A-Za-z0-9_])Bearer\s+(?:(?=[A-Za-z0-9_./+-]*[\d_./+-])[A-Za-z0-9_./+-]{10,}|[A-Za-z0-9_./+-]{20,})|"
    # Long hex strings (>= 24 hex chars) or long mixed alphanumeric tokens (>= 24 chars with letters+digits),
    # excluding known AI model IDs (claude-..., gemini-..., gpt-..., deepseek-..., qwen-..., llama-..., o1/o3/o4-...)
    r"(?<![0-9A-Za-z])[0-9a-fA-F]{24,}(?![0-9A-Za-z])|"
    r"(?<![0-9A-Za-z_-])"
    r"(?!(?:claude|gemini|gpt|deepseek|qwen|llama|o1|o3|o4)[0-9]*[-_])"
    r"(?=[A-Za-z0-9_-]{24,}(?![0-9A-Za-z_-]))"
    r"(?=[A-Za-z0-9_-]*[A-Za-z])"
    r"(?=[A-Za-z0-9_-]*\d)"
    r"[A-Za-z0-9_-]{24,}(?![0-9A-Za-z_-])|"
    # Chinese mobile phone numbers (11 digits starting 1[3-9])
    r"(?<!\d)1[3-9]\d{9}(?!\d)|"
    # 18-char Chinese national ID numbers
    r"(?<!\d)\d{17}[\dXx](?![\dXx])|"
    # 16-19 digit bank card numbers
    r"(?<!\d)\d{16,19}(?!\d)|"
    # Detailed residential address (building/unit + room number)
    r"(?:\d+号院|\d+号楼|\d+栋|\d+单元).{0,10}\d{2,4}室|"
    # Credential keywords only when paired with an assignment/secret-like value or high-entropy token
    r"(?:(?<![A-Za-z])(?:token|password|passwd|api[_\s-]?key|secret)(?![A-Za-z])|密码|口令|密钥)"
    r"(?:\s*[:=：是为]\s*[`\"']?[A-Za-z0-9_./+@!#$*-]{6,}"
    r"|\s+[`\"']?(?=[A-Za-z0-9_./+-]{8,})(?=[A-Za-z0-9_./+-]*[A-Za-z])(?=[A-Za-z0-9_./+-]*\d)[A-Za-z0-9_./+-]{8,})|"
    r"验证码|身份证|银行卡"
    r")",
    re.IGNORECASE,
)


def is_sensitive(text: str) -> bool:
    """Return True if text matches any sensitive path, URL, LAN IP, secret, token, or ID pattern."""
    if not text:
        return False
    return bool(SENSITIVE_MEMORY_RE.search(str(text)))


def find_sensitive_matches(text: str) -> list[str]:
    """Return deduplicated list of sensitive substrings found in text (preserving order)."""
    if not text:
        return []
    matches: list[str] = []
    seen: set[str] = set()
    for m in SENSITIVE_MEMORY_RE.finditer(str(text)):
        val = m.group(0)[:64]
        key = val.lower()
        if key not in seen:
            seen.add(key)
            matches.append(val)
    return matches


# ---------------------------------------------------------------------------
# Guardrail 2 — Write Quality Gate (0..100, reject < 40)
# ---------------------------------------------------------------------------
QUALITY_REJECT_THRESHOLD = 40

META_MEMORY_RE = re.compile(
    r"(?:记忆库|作为\s*AI|作为人工智能|"
    r"(?<![A-Za-z])system\s*prompts?(?![A-Za-z])|"
    r"(?<![A-Za-z])prompt\s*injection(?![A-Za-z])|"
    r"系统指令|系统提示词|泄露提示词|套取提示词|提示词注入|人设指令|"
    r"越狱指令|越狱模板|"
    r"忽略(?:以上|上面|上述|之前|前面|所有|全部).{0,10}(?:规则|指令|设定|提示|要求)|"
    r"无视(?:以上|所有|全部|系统)?.{0,8}(?:规则|指令|限制|设定)|"
    r"以后每次回复都必须|必须叫我主人|"
    r"(?<![A-Za-z])(?:ignore|disregard|forget)\s+(?:all\s+)?(?:(?:previous|above|prior|system)\s+)+(?:instructions?|rules?|prompts?)(?![A-Za-z])|"
    r"上下文窗口|长期记忆模块|后台整理器|"
    r"(?:\[\[|［［)\s*QQ:[^\]]*\]\]|(?:<<<|＜＜＜)\s*UNTRUSTED_CHAT_LOG)",
    re.IGNORECASE,
)

_SYNTHETIC_BLOCK_DETECT_RE = re.compile(
    r"(?:\[\s*(?:权限边界|权限身份|可用QQ操作|QQ动作权限|视觉附件|"
    r"回复风格(?:补充)?|群聊触发状态|群聊监听状态|群聊复读|群聊新消息|"
    r"相关用户长期记忆|近期对话续接|群聊近期消息记录|"
    r"以上为近期群聊上下文|近期私聊消息ID|近期私聊消息记录|"
    r"上下文提醒|历史聊天记录|历史记录查阅|跨群动态与记忆速览|cc-connect|"
    r"本轮状态|状态说明|群成员|插件提示|待审入群申请|当前引用较早消息)\b"
    r"|^\s*【(?:前情摘要|近期对话|人物关系|群聊动态速览)】"
    r"|^\s*Sender\s*[:：]"
    r"|\[\[QQ:[^\]]*\]\]"
    r"|<<<UNTRUSTED_CHAT_LOG)",
    re.MULTILINE,
)

_SHORT_REPEAT_BLOCK_RE = re.compile(r"(.{1,6})\1{2,}")


def _has_short_block_repetition(text: str) -> bool:
    """Return True if a short unit (1..6 chars) repeats >= 3 times covering >= 60% of content."""
    core = re.sub(r"[\s\W_]+", "", str(text or "")).lower()
    if len(core) < 6:
        return False
    for m in _SHORT_REPEAT_BLOCK_RE.finditer(core):
        span_len = m.end() - m.start()
        if span_len >= 6 and (span_len / len(core)) >= 0.6:
            return True
    return False


def score_memory_quality(text: str) -> tuple[int, list[str]]:
    """Score a memory candidate on 0..100 and return (score, flags).

    Candidates with score < 40 (QUALITY_REJECT_THRESHOLD) are rejected.
    """
    s = str(text or "").strip()
    score = 100
    flags: list[str] = []

    if len(s) < 6:
        score -= 80
        flags.append("too_short")

    if len(s) > 180:
        score -= 65
        flags.append("too_long")

    if tokenizer.is_trivial_message(s):
        score -= 70
        flags.append("trivial")

    if META_MEMORY_RE.search(s):
        # Single meta-memory / prompt-injection flag must reject (< 40)
        score -= 65
        flags.append("meta_memory_pollution")

    if tokenizer.compute_3gram_repetition_ratio(s) > 0.7:
        # Single looping / spam 3-gram repetition flag must reject (< 40)
        score -= 65
        flags.append("repetitive_3gram")
    elif _has_short_block_repetition(s):
        score -= 65
        flags.append("repetitive_block")

    if _SYNTHETIC_BLOCK_DETECT_RE.search(s):
        score -= 80
        flags.append("synthetic_block")

    score = max(0, min(100, score))
    return score, flags


# ---------------------------------------------------------------------------
# Guardrail 3 — Fact Key Normalization & Universal-Key Blocker
# ---------------------------------------------------------------------------
UNIVERSAL_TAIL_WORDS = frozenset(
    {
        "内容", "其他", "其它", "记录", "情况", "事情", "杂项", "信息",
        "备注", "说明", "详情", "综合", "默认", "描述", "事件", "资料",
        "misc", "other", "info", "content", "general", "item",
        "note", "notes", "thing", "stuff", "detail", "details",
        "default", "data",
    }
)

_FACT_KEY_SEP_RE = re.compile(r"[：:/\\\s]+")
_FACT_KEY_ALLOWED_RE = re.compile(r"[^a-z0-9._\-\u4e00-\u9fff]+")
_FACT_KEY_MULTI_DOT_RE = re.compile(r"\.{2,}")


def normalize_fact_key(raw_key: str) -> str:
    """Normalize a dot-separated fact key and block generic catch-all keys.

    Returns "" if the key is empty, too short, or ends with (or equals) a
    universal tail word (preventing unrelated facts from overwriting each other).
    """
    if not raw_key:
        return ""
    s = str(raw_key).strip()
    if not s:
        return ""

    # Convert colons, slashes, and whitespace to dot separators; lowercase ASCII
    s = _FACT_KEY_SEP_RE.sub(".", s).lower()
    # Keep only ASCII alnum, '.', '_', '-', and CJK characters
    s = _FACT_KEY_ALLOWED_RE.sub("", s)
    s = _FACT_KEY_MULTI_DOT_RE.sub(".", s).strip(".")
    if not s:
        return ""

    parts = [p.strip("_-") for p in s.split(".") if p.strip("_-")]
    if not parts:
        return ""

    key = ".".join(parts)
    tail = parts[-1]
    if tail in UNIVERSAL_TAIL_WORDS or key in UNIVERSAL_TAIL_WORDS:
        return ""

    # Also block underscore/hyphen catch-all tails like "user_info" or "work_other"
    sub_tail = re.split(r"[_-]", tail)[-1]
    if sub_tail in UNIVERSAL_TAIL_WORDS:
        return ""

    if len(parts) == 1 and len(parts[0]) < 2:
        return ""

    if len(key) > 48:
        key = key[:48].rstrip("._-")
        if not key:
            return ""
        trunc_parts = [p for p in key.split(".") if p]
        if not trunc_parts:
            return ""
        if trunc_parts[-1] in UNIVERSAL_TAIL_WORDS or key in UNIVERSAL_TAIL_WORDS:
            return ""
        if len(trunc_parts) == 1 and len(trunc_parts[0]) < 2:
            return ""

    return key


# ---------------------------------------------------------------------------
# Guardrail 4 — Near-Duplicate Detection & Factual Substitution Guard
# ---------------------------------------------------------------------------
_NUMBER_TOKEN_RE = re.compile(r"\d+(?:\.\d+)?")
_ASCII_ALNUM_RE = re.compile(r"[a-z0-9]+")

_SUBSTANTIVE_FILLER_WORDS = tokenizer.STOPWORDS | frozenset(
    {
        "非常", "特别", "十分", "挺", "超级", "平时", "经常", "常常",
        "目前", "现在", "当前", "如今", "最近", "近期", "每天", "每日",
        "总是", "一直", "已经", "正在", "曾经", "比较", "有点", "一些",
        "会", "来", "去", "将", "于", "并", "且", "较", "颇", "极",
    }
)

_SUBSTANTIVE_FILLER_CHARS = tokenizer.SINGLE_CHAR_STOPWORDS | frozenset(
    {"会", "来", "去", "将", "于", "并", "且", "挺", "超", "较", "颇", "极"}
)


def _has_differing_numbers(text_a: str, text_b: str) -> bool:
    """Return True when both texts contain numeric literals and those numbers differ."""
    norm_a = tokenizer._normalize_nfkc(text_a)
    norm_b = tokenizer._normalize_nfkc(text_b)
    nums_a = _NUMBER_TOKEN_RE.findall(norm_a)
    nums_b = _NUMBER_TOKEN_RE.findall(norm_b)
    return bool(nums_a and nums_b and nums_a != nums_b)


def _is_substantive_span(span: str) -> bool:
    """Return True if a differing normalized span carries non-filler factual payload."""
    if not span or span in _SUBSTANTIVE_FILLER_WORDS:
        return False
    for tok in _ASCII_ALNUM_RE.findall(span):
        if any(ch.isdigit() for ch in tok) or tok not in tokenizer._EN_STOPWORDS:
            return True
    cjk_only = "".join(ch for ch in span if "\u4e00" <= ch <= "\u9fff")
    if not cjk_only or cjk_only in _SUBSTANTIVE_FILLER_WORDS:
        return False
    for ch in cjk_only:
        if ch not in _SUBSTANTIVE_FILLER_CHARS:
            return True
    return False


def _has_factual_substitution(new_text: str, old_text: str) -> bool:
    """Return True if two lexically similar texts substitute concrete factual payload.

    Distinguishes genuine entity/city/company/role/number updates (e.g. 上海 -> 北京,
    腾讯 -> 字节, 前端 -> 后端, 猫 -> 狗, 200 -> 500) from wording/filler paraphrases
    (e.g. 都会 -> 都, 很喜欢 -> 喜欢, 在周末 -> 周末).
    """
    if not new_text or not old_text:
        return False
    if _has_differing_numbers(new_text, old_text):
        return True

    norm_new = tokenizer._strip_non_content_chars(tokenizer._normalize_nfkc(new_text)).lower()
    norm_old = tokenizer._strip_non_content_chars(tokenizer._normalize_nfkc(old_text)).lower()
    if not norm_new or not norm_old or norm_new == norm_old:
        return False

    matcher = difflib.SequenceMatcher(None, norm_old, norm_new, autojunk=False)
    has_substantive_del = False
    has_substantive_ins = False
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        old_span = norm_old[i1:i2]
        new_span = norm_new[j1:j2]
        if tag == "replace":
            old_sub = _is_substantive_span(old_span)
            new_sub = _is_substantive_span(new_span)
            if old_sub and new_sub:
                return True
            if old_sub:
                has_substantive_del = True
            if new_sub:
                has_substantive_ins = True
        elif tag == "delete":
            if _is_substantive_span(old_span):
                has_substantive_del = True
        elif tag == "insert":
            if _is_substantive_span(new_span):
                has_substantive_ins = True

    return has_substantive_del and has_substantive_ins


def find_near_duplicate(
    text: str,
    existing_active: list[dict],
    threshold: float = 0.85,
    fact_key: str = "",
) -> dict | None:
    """Return the highest-scoring active memory dict with similarity >= threshold, else None.

    Refuses to collapse differing numeric facts (even without a fact_key) or
    same-fact_key factual value substitutions so temporal updates proceed cleanly.
    """
    if not text or not existing_active:
        return None

    norm_fk = normalize_fact_key(fact_key) if fact_key else ""
    best_item: dict | None = None
    best_score = 0.0
    for item in existing_active:
        if not isinstance(item, dict):
            continue
        existing_text = str(item.get("text") or "").strip()
        if not existing_text:
            continue
        score = tokenizer.text_similarity(text, existing_text)
        if score < threshold:
            continue
        if _has_differing_numbers(text, existing_text):
            continue
        if norm_fk:
            raw_existing_fk = str(item.get("fact_key") or "").strip()
            existing_fk = (
                raw_existing_fk
                if raw_existing_fk == norm_fk
                else normalize_fact_key(raw_existing_fk)
            )
            if existing_fk == norm_fk and _has_factual_substitution(text, existing_text):
                continue
        if score > best_score:
            best_score = score
            best_item = item
    return best_item


# ---------------------------------------------------------------------------
# Unified Write Candidate Check (All 4 Guardrails)
# ---------------------------------------------------------------------------
def check_write_candidate(
    text: str,
    scope: str,
    fact_key: str = "",
    chat_is_group: bool | None = None,
    chat_group_id: str = "",
    chat_user_id: str = "",
    existing_active: list[dict] | None = None,
    dup_threshold: float = 0.85,
) -> dict:
    """Run all 4 hard guardrails on a candidate memory write.

    Returns:
      {
        "allowed": bool,
        "action": "add" | "reinforce" | "reject",
        "reason": str,
        "quality_score": int,
        "quality_flags": list[str],
        "fact_key": str,
        "matched_id": str,
        "sensitive_matches": list[str],
      }
    """
    clean_text = str(text or "").strip()
    clean_scope = str(scope or "").strip()
    norm_fact_key = normalize_fact_key(fact_key)
    quality_score, quality_flags = score_memory_quality(clean_text)

    # Guardrail 1a: Sensitive content blocker
    sensitive_matches = find_sensitive_matches(clean_text)
    if sensitive_matches:
        return {
            "allowed": False,
            "action": "reject",
            "reason": "sensitive_content",
            "quality_score": 0,
            "quality_flags": quality_flags,
            "fact_key": "",
            "matched_id": "",
            "sensitive_matches": sensitive_matches,
        }

    # Guardrail 1b: Scope isolation boundary
    if not clean_scope:
        return {
            "allowed": False,
            "action": "reject",
            "reason": "empty_scope",
            "quality_score": quality_score,
            "quality_flags": quality_flags,
            "fact_key": "",
            "matched_id": "",
            "sensitive_matches": [],
        }

    if chat_is_group is not None:
        if not db.is_write_scope_allowed(
            chat_is_group=bool(chat_is_group),
            chat_group_id=str(chat_group_id or ""),
            chat_user_id=str(chat_user_id or ""),
            target_scope=clean_scope,
        ):
            return {
                "allowed": False,
                "action": "reject",
                "reason": "scope_not_allowed",
                "quality_score": quality_score,
                "quality_flags": quality_flags,
                "fact_key": "",
                "matched_id": "",
                "sensitive_matches": [],
            }

    # Guardrail 2: Write quality gate
    if quality_score < QUALITY_REJECT_THRESHOLD:
        reason = "low_quality:" + ",".join(quality_flags) if quality_flags else "low_quality"
        return {
            "allowed": False,
            "action": "reject",
            "reason": reason,
            "quality_score": quality_score,
            "quality_flags": quality_flags,
            "fact_key": norm_fact_key,
            "matched_id": "",
            "sensitive_matches": [],
        }

    # Guardrail 3: Fact key already normalized & universal keys stripped -> norm_fact_key

    # Guardrail 4: Near-duplicate detection -> downgrade to reinforce
    if existing_active:
        dup = find_near_duplicate(
            clean_text,
            existing_active,
            threshold=dup_threshold,
            fact_key=norm_fact_key,
        )
        if dup is not None:
            return {
                "allowed": True,
                "action": "reinforce",
                "reason": "near_duplicate",
                "quality_score": quality_score,
                "quality_flags": quality_flags,
                "fact_key": norm_fact_key,
                "matched_id": str(dup.get("id") or ""),
                "sensitive_matches": [],
            }

    return {
        "allowed": True,
        "action": "add",
        "reason": "ok",
        "quality_score": quality_score,
        "quality_flags": quality_flags,
        "fact_key": norm_fact_key,
        "matched_id": "",
        "sensitive_matches": [],
    }


# ---------------------------------------------------------------------------
# Synthetic Prompt Block Filter & Ingestion Hygiene (for ingest.py)
# ---------------------------------------------------------------------------
# Bounded group-context span: [群聊近期消息记录...] ... [以上为近期群聊上下文...]
_GROUP_HISTORY_SPAN_RE = re.compile(
    r"\[群聊近期消息记录[^\n]*\][\s\S]*?\[以上为近期群聊上下文[^\n]*\]\s*",
    re.MULTILINE,
)

# Prompt fence markers that must never survive ingestion
_UNTRUSTED_FENCE_STRIP_RE = re.compile(
    r"<<<\s*UNTRUSTED_CHAT_LOG(?:_[A-Z0-9_]+)?\s*(?:>>>)?",
    re.IGNORECASE,
)

# Inline single-bracket synthetic wrapper tags
_INLINE_SYNTHETIC_TAG_RE = re.compile(
    r"\[(?:权限边界|权限身份|可用QQ操作|QQ动作权限|视觉附件|回复风格(?:补充)?|群聊触发状态|"
    r"群聊监听状态|群聊复读|群聊新消息|上下文提醒|以上为近期群聊上下文|跨群动态与记忆速览|"
    r"本轮状态|状态说明|群成员|插件提示|待审入群申请|当前引用较早消息)[^\]\n]*\]"
)

# Multi-line section headers whose following bullet/system lines belong to the block
_MULTILINE_SECTION_HEADER_RE = re.compile(
    r"^\s*(?:\[(?:"
    r"相关用户长期记忆|近期对话续接|近期私聊消息ID|近期私聊消息记录|"
    r"群聊近期消息记录|QQ动作权限|可用QQ操作|历史聊天记录|历史记录查阅|"
    r"跨群动态与记忆速览"
    r")[^\n]*|【(?:前情摘要|近期对话|人物关系|群聊动态速览)】[^\n]*)$"
)

# Single-line synthetic headers (even if truncated without closing bracket)
_SINGLELINE_SYNTHETIC_LINE_RE = re.compile(
    r"^\s*\[(?:"
    r"权限边界|权限身份|QQ动作权限|视觉附件|回复风格(?:补充)?|群聊触发状态|"
    r"群聊监听状态|群聊复读|群聊新消息|上下文提醒|以上为近期群聊上下文|cc-connect|"
    r"本轮状态|状态说明|群成员|插件提示|待审入群申请|当前引用较早消息"
    r")[^\n]*$"
)

# Lines that continue a multi-line synthetic block
_SECTION_CONT_LINE_RE = re.compile(
    r"^(?:\s*[-•*]\s+|\s{2,}\S|本轮可用\s*QQ\s*动作|本轮已禁止的\s*QQ\s*动作|\[\d{1,2}:\d{2}\]|\[#\d+\])"
)

_LEADING_SENDER_LINE_RE = re.compile(r"^\s*Sender\s*[:：][^\n]*$")


def strip_synthetic_blocks(text: str) -> str:
    """Remove injected prompt wrapper blocks, fence markers, and leading Sender headers from chat text."""
    if not text:
        return ""
    s = str(text).replace("\r\n", "\n").replace("\r", "\n")

    # 1. Strip full bounded group-history context spans and untrusted fence markers first
    s = _GROUP_HISTORY_SPAN_RE.sub("\n", s)
    s = _UNTRUSTED_FENCE_STRIP_RE.sub("", s)

    # 2. Line-by-line filter for multi-line sections, single-line blocks, and leading Sender headers
    lines = s.split("\n")
    kept: list[str] = []
    in_section = False
    at_leading_headers = True

    for line in lines:
        stripped = line.strip()

        if at_leading_headers:
            if not stripped:
                continue
            if _LEADING_SENDER_LINE_RE.match(line):
                continue

        if _MULTILINE_SECTION_HEADER_RE.match(line):
            in_section = True
            continue

        if _SINGLELINE_SYNTHETIC_LINE_RE.match(line):
            in_section = False
            continue

        if in_section:
            if not stripped:
                in_section = False
                continue
            if _SECTION_CONT_LINE_RE.match(line):
                continue
            # Non-continuation line ends the synthetic block and is kept
            in_section = False

        # Strip any inline wrapper tags embedded on an otherwise real line
        cleaned_line = _INLINE_SYNTHETIC_TAG_RE.sub("", line).strip()
        if cleaned_line:
            at_leading_headers = False
            kept.append(cleaned_line)

    result = "\n".join(kept).strip()
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result


_CONTROL_REPLY_RE = re.compile(
    r'^[\s`"\'\[\(\*（【]*(?:NO_REPLY|FOLLOW_REPEAT)[\s`"\'\]\)\*）】\.。!！~～]*$',
    re.IGNORECASE,
)

_UPSTREAM_ERROR_RE = re.compile(
    r"(?:这个我接不住|RESOURCE_EXHAUSTED|"
    r"HTTP\s*(?:429|500|502|503|504)\b|"
    r"500\s+Internal\s+Server\s+Error|502\s+Bad\s+Gateway|"
    r"503\s+Service\s+Unavailable|504\s+Gateway\s+Timeout|"
    r"Traceback\s*\(most\s+recent\s+call\s+last\))",
    re.IGNORECASE,
)

_QQ_DIRECTIVE_RE = re.compile(r"\[\[QQ:[^\]]+\]\]")


def is_unworthy_storage_text(text: str, role: str = "user") -> bool:
    """Return True for empty text, control tokens, upstream error dumps, or pure synthetic blocks."""
    if not text:
        return True
    raw = str(text).strip()
    if not raw:
        return True
    if _CONTROL_REPLY_RE.match(raw):
        return True
    if _UPSTREAM_ERROR_RE.search(raw):
        return True

    cleaned = strip_synthetic_blocks(raw)
    if not cleaned:
        return True
    if _CONTROL_REPLY_RE.match(cleaned):
        return True

    if role == "assistant":
        no_directives = _QQ_DIRECTIVE_RE.sub("", cleaned).strip()
        if not no_directives:
            return True

    return False
