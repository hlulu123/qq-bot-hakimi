#!/usr/bin/env python3
"""Zero-dependency Chinese/English tokenizer and text analysis for QQ Bot SQLite v2 Memory Engine.

Pure Python 3 standard library implementation designed for sub-millisecond
per-message execution:
- Chinese + English stopword filtering and glue-character segmentation
- Anti-collision content term extraction (ASCII, CJK segments/bigrams/trigrams, proper terms)
- Safe SQLite FTS5 indexing tokens and syntax-safe query builder
- Auto-keyword extraction
- Lexical near-duplicate similarity (polarity-aware Dice over n-grams/tokens)
- Beijing-time (UTC+8) Chinese temporal clue parsing
- 3-gram repetition ratio detector
"""

from __future__ import annotations

import datetime
import re
import unicodedata

# ---------------------------------------------------------------------------
# Timezone (Beijing UTC+8)
# ---------------------------------------------------------------------------
_TZ_CN = datetime.timezone(datetime.timedelta(hours=8))

# ---------------------------------------------------------------------------
# Stopwords (grammatical glue, pronouns, discourse fillers — NEVER semantic
# nouns/verbs like 喜欢/讨厌/过敏/养/猫/狗/游戏/吃/喝/工作/学校/搬家/辞职/
# 论文/生日 or time words like 今天/昨天/前天/上周).
# ---------------------------------------------------------------------------
_EN_STOPWORDS = frozenset(
    {
        "the", "is", "are", "was", "were", "be", "been", "being",
        "a", "an", "to", "of", "in", "on", "for", "and", "or",
        "it", "its", "this", "that", "these", "those",
        "do", "does", "did", "done", "doing",
        "i", "you", "he", "she", "we", "they",
        "my", "your", "his", "her", "our", "their", "me", "him", "us", "them",
        "at", "by", "with", "from", "as", "but", "not", "so", "if",
        "am", "has", "have", "had", "will", "would", "can", "could",
        "should", "may", "might", "must", "shall",
        "about", "into", "over", "after", "before", "then", "than",
        "too", "very", "just", "also", "all", "any", "some", "no", "nor",
        "what", "which", "who", "whom", "where", "when", "why", "how",
        "here", "there", "up", "down", "out", "off",
    }
)

_ZH_SINGLE_STOPWORDS = frozenset(
    {
        "的", "了", "着", "过", "得", "地",
        "是", "在", "有", "没", "不", "很", "太", "真", "更", "最",
        "我", "你", "他", "她", "它", "咱", "们", "谁", "啥", "哪",
        "这", "那", "就", "都", "也", "还", "又", "再", "才", "只",
        "和", "与", "或", "及", "而", "但", "却", "把", "被", "给",
        "让", "向", "往", "从", "到", "比", "跟", "对", "为", "以",
        "之", "乎", "者",
        "啊", "吧", "呢", "吗", "嘛", "哦", "嗯", "哈", "呀", "哇",
        "啦", "咯", "呗", "诶", "唉", "哎", "呜", "哟",
    }
)

_ZH_MULTI_STOPWORDS = frozenset(
    {
        "我们", "你们", "他们", "她们", "它们", "咱们",
        "一个", "一下", "一些", "一样", "一直", "一起", "一点", "有点",
        "什么", "怎么", "怎么样", "怎么说", "为什么", "为啥", "哪里", "哪儿", "哪个",
        "可以", "可能", "应该", "能够", "需要",
        "没有", "不是", "就是", "只是", "还是", "都是", "也是", "真是",
        "然后", "但是", "不过", "可是", "因为", "所以", "如果", "要是",
        "虽然", "尽管", "而且", "并且", "或者", "否则", "于是",
        "已经", "正在", "曾经", "居然", "竟然",
        "觉得", "感觉", "知道", "认为", "以为", "好像", "似乎", "大概", "估计",
        "这个", "那个", "这些", "那些", "这里", "那里", "这儿", "那儿",
        "这么", "那么", "这样", "那样",
        "自己", "大家", "别人", "人家", "各位",
        "其实", "确实", "真的", "本来", "原来", "反正", "顺便", "另外",
        "关于", "对于", "非常", "比较",
        "哈哈", "哈哈哈", "哈哈哈哈", "嗯嗯", "哦哦", "好好",
    }
)

SINGLE_CHAR_STOPWORDS = _ZH_SINGLE_STOPWORDS
STOPWORDS = _EN_STOPWORDS | _ZH_SINGLE_STOPWORDS | _ZH_MULTI_STOPWORDS

# Generic high-frequency 2-char Chinese words to filter out of auto_keywords
# and demote during FTS query term capping.
_GENERIC_AUTO_KEYWORDS = frozenset(
    {
        "关注", "使用", "国内", "工具", "体验", "情况", "打算", "目前",
        "最近", "已经", "平时", "习惯", "经常", "喜欢", "觉得", "比较",
        "一般", "主要", "相关", "问题", "东西", "事情", "方式", "时候",
        "地方", "朋友", "群友",
    }
)

# Real 2-3 char Chinese words that contain structural glue characters
# (e.g. 他/你/了/的/在/就/和/有/与/还/是/或) and MUST NOT be shredded into 1-char scraps.
_CJK_PROTECTED_WORDS = frozenset(
    {
        # Explicitly protected common words
        "吉他", "迷你", "了解", "目的", "在校", "就业", "和平", "有趣", "参与", "还原",
        # Other frequent compounds containing split characters
        "其他", "他人", "他乡", "利他", "排他",
        "在线", "存在", "所在", "内在", "外在", "现在", "自在", "实在", "潜在", "在职", "在读", "在乎", "在意", "在场", "在行",
        "成就", "就读", "就职", "就医", "就餐", "就近", "就算", "迁就", "造就", "将就",
        "和解", "和谐", "温和", "缓和", "柔和", "暖和", "平和", "饱和", "亲和", "总和", "共和", "随和", "和蔼", "和气", "和善", "和风", "和尚", "和面", "和弦", "和声", "和服", "和牛",
        "有效", "有用", "有名", "有限", "有望", "有意", "有益", "有害", "有机", "有氧", "有空", "有钱", "有力", "有感", "有爱", "有戏", "所有", "拥有", "持有", "享有", "私有", "公有", "国有", "稀有", "富有", "特有", "独有", "固有", "原有", "现有", "既有", "占有",
        "与其", "给予", "赠与", "与会",
        "归还", "偿还", "退还", "返还", "还手", "还击", "还价", "还钱", "还债", "还愿",
        "的确", "的士", "打的", "标的",
        "了不起", "了结", "了然", "了断", "明了", "知了",
        "自我", "忘我", "无我",
        "或许", "干嘛", "是非", "是否",
    }
)

# Structural glue characters safe to split contiguous CJK runs on without
# shredding common domain nouns/cities (e.g. keeps 过敏, 成都, 太原, 网吧,
# 哈士奇, 哈尔滨, 着急 intact by omitting 过/着/都/太/吧/哈 from mid-word splits).
_CJK_SPLIT_SINGLE_CHARS = (
    "的了吗呢哦嗯嘛啦咯呗诶唉哎呜哟"
    "很就也还在与或是有不这那我你他她它们谁啥"
)
# Multi-char stopwords safe to split on (sorted longest-first for regex alternation)
_CJK_SPLIT_MULTI_WORDS = sorted(
    (
        "为什么", "怎么样", "怎么说", "哈哈哈", "哈哈",
        "什么", "怎么", "为啥", "哪里", "哪儿", "哪个",
        "没有", "不是", "就是", "只是", "还是", "还有", "都是", "也是",
        "一个", "一下", "一些", "一样", "一直", "一起",
        "可以", "可能", "应该", "能够",
        "然后", "但是", "不过", "可是", "因为", "所以",
        "如果", "要是", "虽然", "尽管", "而且", "并且", "或者",
        "已经", "正在", "觉得", "感觉", "知道", "认为", "以为",
        "好像", "似乎", "大概", "估计",
        "这个", "那个", "这些", "那些", "这里", "那里", "这儿", "那儿",
        "这么", "那么", "这样", "那样",
        "自己", "大家", "其实", "确实", "真的", "本来", "原来", "反正",
        "我们", "你们", "他们", "她们", "它们", "咱们",
    ),
    key=len,
    reverse=True,
)

_CJK_SPLIT_SINGLE_CHARS_WITH_HE = _CJK_SPLIT_SINGLE_CHARS + "和"
_CJK_SPLIT_SINGLE_SET = frozenset(_CJK_SPLIT_SINGLE_CHARS_WITH_HE)
_CJK_PROTECTED_ALT = "|".join(
    re.escape(w) for w in sorted(_CJK_PROTECTED_WORDS, key=len, reverse=True)
)
_CJK_SPLIT_ALT = (
    "|".join(re.escape(w) for w in _CJK_SPLIT_MULTI_WORDS)
    + f"|[{re.escape(_CJK_SPLIT_SINGLE_CHARS_WITH_HE)}]"
)
# Group 1 = protected real word (never split); Group 2 = split separator.
_CJK_RUN_SCAN_RE = re.compile(f"({_CJK_PROTECTED_ALT})|({_CJK_SPLIT_ALT})")

# Sentence-final particles that may safely be trimmed from the edges of a CJK
# segment without breaking mid-word nouns (e.g. 网吧/贴吧 are NOT trimmed when
# len==2, only stripped when len >= 3 or standalone particle).
_CJK_EDGE_PARTICLES = frozenset("吧啊呀哇哈呢吗哦嗯")

# Mixed single-ASCII-letter + CJK entity detection (e.g. B站, C盘, G胖, C语言, U盘, A股, P图)
_MIXED_LETTER_CJK_RE = re.compile(r"(?<![a-zA-Z0-9])([a-zA-Z])([\u4e00-\u9fff]+)")
_MIXED_ENTITY_1CHAR_SUFFIXES = frozenset(
    "站盘胖群股图歌恤光超端区类级版卡号座楼栋组班队期卷册集季篇话章段档位人宝圈粉"
)
_MIXED_ENTITY_2CHAR_SUFFIXES = frozenset(
    {"语言", "世代", "系列", "型号", "接口", "插槽", "分区", "频道", "社区"}
)

# Standalone media placeholders emitted by OneBot / QQ wrapper
_MEDIA_PLACEHOLDER_RE = re.compile(
    r"\[(?:"
    r"表情(?::[^\]]*)?|表情包|商城表情|图片|语音|视频|"
    r"文件(?::[^\]]*)?|卡片消息|卡片:[^\]]*|合并转发|发送了图片/表情包"
    r")\]"
)

# Pure punctuation / whitespace / emoji character classes
_PUNCT_EMOJI_CLASS = (
    r"\s\.,。，!！?？~～\-_+=()（）\[\]【】…·、；;：:\"'“”‘’<>《》/\\|@#%^&*"
    r"\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u200D"
)

TRIVIAL_MSG_RE = re.compile(
    rf"^(?:[{_PUNCT_EMOJI_CLASS}]+"
    r"|(?:\[(?:表情(?::[^\]]*)?|表情包|商城表情|图片|语音|视频|文件(?::[^\]]*)?|卡片消息|卡片:[^\]]*|合并转发|发送了图片/表情包)\]\s*)+"
    rf"|[{_PUNCT_EMOJI_CLASS}]*(?:"
    r"嗯+|哦+|好+|好的|收到|行|行吧|可以|ok+|1|6+|？+|\?+|!+|哈+|草|笑死|"
    r"确实|nb|牛逼?|厉害|懂了|知道了|谢谢|谢了|多谢|感谢|"
    r"晚安|早安|早上好|下午好|晚上好|拜拜|再见|88|886|哦哦|嗯嗯|好好|"
    r"你好|您好|hi|hello|hey|yes|no|yep|nope|thx|thanks|www+"
    rf")[{_PUNCT_EMOJI_CLASS}]*)$",
    re.IGNORECASE,
)

# ASCII/alphanumeric token regex supporting internal +, -, _, ., # (e.g. m60p+, bge-m3, c++, c#, gpt-4, python3, node.js)
_ASCII_TOKEN_RE = re.compile(
    r"[a-zA-Z0-9](?:[a-zA-Z0-9._+-]*[a-zA-Z0-9+#])?|[a-zA-Z][+#]{1,2}"
)
_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")
_HAS_WORD_CHAR_RE = re.compile(r"[a-zA-Z0-9\u4e00-\u9fff]")

# Negation markers for polarity-aware near-duplicate detection.
# Negative lookarounds exclude common non-negation words (非常, 无线, 特别, 未来,
# 无论, 无聊, 别说, 别人, 别的, 未免, 区别, 分别, 非凡, 无敌, etc.) as well as
# double-negated forms like 不讨厌 / 不再讨厌. Note: 过敏 is intentionally excluded
# because "对花生过敏" is affirmative while "对花生不过敏" carries negation "不".
_NEGATION_RE = re.compile(
    r"(?:不(?!(?:太|再)?(?:讨厌|厌恶))|没|"
    r"无(?!线|论|聊|敌|限|所谓|非|语|奈|忧|妨|价|双)|"
    r"(?<!特|区|分|识|类|级|性|个|甄|辨|离|道|临|惜|送|告|有)别(?!人|的|说|致|处|样|离|墅)|"
    r"(?<!除|无|是)非(?!常|凡|洲|遗)|"
    r"未(?!来|免)|"
    r"莫(?!名|斯|扎)|"
    r"勿|(?<!不)(?<!不太)(?<!不再)(?:讨厌|厌恶)|"
    r"(?<![a-zA-Z])(?:not|no|never|don't|doesn't|didn't|won't|can't|cannot|hate|dislike)(?![a-zA-Z]))",
    re.IGNORECASE,
)


def _normalize_nfkc(text: str) -> str:
    if not text:
        return ""
    return unicodedata.normalize("NFKC", str(text))


def _strip_non_content_chars(text: str) -> str:
    """Remove whitespace, punctuation, symbols, and format/control characters."""
    out: list[str] = []
    for ch in text:
        cat = unicodedata.category(ch)
        if cat and cat[0] in ("P", "S", "Z", "C"):
            continue
        out.append(ch)
    return "".join(out)


def _clean_text_for_terms(text: str) -> str:
    if not text:
        return ""
    norm = _normalize_nfkc(text)
    cleaned = _MEDIA_PLACEHOLDER_RE.sub(" ", norm)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def _split_cjk_run(run: str) -> list[str]:
    """Split a contiguous CJK run on glue particles while keeping protected compounds intact."""
    if len(run) <= 1:
        return []
    if run in _CJK_PROTECTED_WORDS:
        return [run]
    parts: list[str] = []
    buf_start = 0
    for m in _CJK_RUN_SCAN_RE.finditer(run):
        if m.group(1) is not None:
            # Protected real word — keep intact inside current segment
            continue
        if m.start() > buf_start:
            parts.append(run[buf_start : m.start()])
        buf_start = m.end()
    if buf_start < len(run):
        parts.append(run[buf_start:])
    return parts


def _extract_mixed_entities(cleaned: str) -> list[str]:
    """Extract short mixed ASCII-letter + CJK entities like B站, C盘, G胖, C语言."""
    out: list[str] = []
    seen: set[str] = set()
    for m in _MIXED_LETTER_CJK_RE.finditer(cleaned):
        letter = m.group(1).lower()
        cjk_tail = m.group(2)
        if not cjk_tail:
            continue
        # Stop before structural glue particles (e.g. "B站的" -> head="站")
        head_chars: list[str] = []
        for ch in cjk_tail:
            if ch in _CJK_SPLIT_SINGLE_SET:
                break
            head_chars.append(ch)
        head = "".join(head_chars)
        if not head or head[0] in _ZH_SINGLE_STOPWORDS:
            continue

        entity = ""
        if len(head) >= 2 and head[:2] in _MIXED_ENTITY_2CHAR_SUFFIXES:
            entity = letter + head[:2]
        elif len(head) == 1:
            entity = letter + head
        elif head[0] in _MIXED_ENTITY_1CHAR_SUFFIXES:
            entity = letter + head[0]

        if entity and entity not in seen:
            seen.add(entity)
            out.append(entity)
    return out


def is_trivial_message(text: str) -> bool:
    """Return True if text is empty, media-only, pure punctuation/emoji, short filler, or trivial chitchat."""
    if not text:
        return True
    s = _normalize_nfkc(text).strip()
    if not s:
        return True
    if TRIVIAL_MSG_RE.match(s):
        return True
    # Strip media placeholders, punctuation, symbols, emojis, whitespace
    stripped_media = _MEDIA_PLACEHOLDER_RE.sub(" ", s).strip()
    if not stripped_media:
        return True
    if TRIVIAL_MSG_RE.match(stripped_media):
        return True
    core = _strip_non_content_chars(stripped_media)
    if len(core) < 2:
        return True
    if core.isdigit() and len(core) < 3:
        return True
    return False


def _extract_ordered_terms(text: str) -> dict:
    """Internal helper preserving natural appearance order for FTS query and auto_keywords."""
    cleaned = _clean_text_for_terms(text)
    if not cleaned:
        return {
            "ascii_list": [],
            "proper_list": [],
            "cjk_runs": [],
            "cjk_segments": [],
            "cjk_bigrams_list": [],
            "cjk_trigrams_list": [],
        }

    # 1. ASCII / alphanumeric tokens + short mixed ASCII+CJK entities (appearance order)
    ascii_list: list[str] = []
    seen_ascii: set[str] = set()
    proper_list: list[str] = []
    seen_proper: set[str] = set()

    def _push_ascii(tok: str) -> None:
        if tok not in seen_ascii:
            seen_ascii.add(tok)
            ascii_list.append(tok)
        if len(tok) >= 3 and (not tok.isdigit() or len(tok) >= 4):
            if tok not in seen_proper:
                seen_proper.add(tok)
                proper_list.append(tok)

    for match in _ASCII_TOKEN_RE.finditer(cleaned):
        tok = match.group(0).lower()
        if len(tok) < 2:
            continue
        if tok in _EN_STOPWORDS:
            continue
        if tok.isdigit() and len(tok) < 3:
            continue
        _push_ascii(tok)

    for ent in _extract_mixed_entities(cleaned):
        _push_ascii(ent)

    # 2. CJK runs -> split by structural glue particles & multi-char stopwords (protecting compounds)
    cjk_runs = _CJK_RUN_RE.findall(cleaned)
    cjk_segments: list[str] = []
    seen_segs: set[str] = set()
    cjk_bigrams_list: list[str] = []
    seen_bgs: set[str] = set()
    cjk_trigrams_list: list[str] = []
    seen_tgs: set[str] = set()

    for run in cjk_runs:
        parts = _split_cjk_run(run)
        for part in parts:
            if not part:
                continue
            # Gently trim trailing discourse particles when segment is > 2 chars
            # (keeps 2-char nouns like 网吧/贴吧 intact while turning 橘猫啊 -> 橘猫)
            seg = part
            while len(seg) > 2 and seg[-1] in _CJK_EDGE_PARTICLES:
                seg = seg[:-1]
            while len(seg) > 2 and seg[0] in ("啊", "呀", "哇", "哦", "嗯"):
                seg = seg[1:]
            if len(seg) < 2 or seg in STOPWORDS:
                continue
            if seg not in seen_segs:
                seen_segs.add(seg)
                cjk_segments.append(seg)

            seg_len = len(seg)
            # Bigrams inside segment (never crossing stopword boundaries)
            for i in range(seg_len - 1):
                bg = seg[i : i + 2]
                if bg in STOPWORDS:
                    continue
                if bg[0] in SINGLE_CHAR_STOPWORDS and bg[1] in SINGLE_CHAR_STOPWORDS:
                    continue
                if bg not in seen_bgs:
                    seen_bgs.add(bg)
                    cjk_bigrams_list.append(bg)

            # Trigrams inside segment
            for i in range(seg_len - 2):
                tg = seg[i : i + 3]
                if tg in STOPWORDS:
                    continue
                if tg not in seen_tgs:
                    seen_tgs.add(tg)
                    cjk_trigrams_list.append(tg)

            # Proper CJK terms: exact len 3..6 (2-char segments intentionally
            # excluded so random 2-char overlaps cannot bypass anti-collision)
            if 3 <= seg_len <= 6 and seg not in STOPWORDS:
                if seg not in seen_proper:
                    seen_proper.add(seg)
                    proper_list.append(seg)

    return {
        "ascii_list": ascii_list,
        "proper_list": proper_list,
        "cjk_runs": cjk_runs,
        "cjk_segments": cjk_segments,
        "cjk_bigrams_list": cjk_bigrams_list,
        "cjk_trigrams_list": cjk_trigrams_list,
    }


def extract_content_terms(text: str) -> dict:
    """Extract normalized ASCII/CJK terms and anti-collision proper terms from text.

    Returns dict with keys:
      - ascii_terms: set[str]
      - cjk_runs: list[str]
      - cjk_segments: list[str]
      - cjk_bigrams: set[str]
      - cjk_trigrams: set[str]
      - proper_terms: set[str]
      - all_terms: set[str]  (ascii_terms | cjk_bigrams | cjk_trigrams)
    """
    ordered = _extract_ordered_terms(text)
    ascii_terms = set(ordered["ascii_list"])
    cjk_bigrams = set(ordered["cjk_bigrams_list"])
    cjk_trigrams = set(ordered["cjk_trigrams_list"])
    proper_terms = set(ordered["proper_list"])
    all_terms = ascii_terms | cjk_bigrams | cjk_trigrams
    return {
        "ascii_terms": ascii_terms,
        "cjk_runs": ordered["cjk_runs"],
        "cjk_segments": ordered["cjk_segments"],
        "cjk_bigrams": cjk_bigrams,
        "cjk_trigrams": cjk_trigrams,
        "proper_terms": proper_terms,
        "all_terms": all_terms,
    }


def is_trivial_query(text: str) -> bool:
    """Return True if text is empty, matches TRIVIAL_MSG_RE, or yields zero meaningful terms.

    Never treats date/time queries (e.g. '12月25日', '2月29日', '今天', '昨天', '最近'),
    short mixed entities ('B站', 'C盘', 'G胖', 'C++'), or admin recap queries
    ('今天发生了什么', '有什么重点') as trivial.
    """
    if not text:
        return True
    s = _normalize_nfkc(text).strip()
    if not s or TRIVIAL_MSG_RE.match(s):
        return True
    if extract_time_clues(s) is not None:
        return False
    recap = detect_admin_recap_intent(s)
    if recap["is_recap"] or recap["is_cross_group"]:
        return False
    terms = extract_content_terms(s)
    if not (terms["ascii_terms"] or terms["cjk_bigrams"] or terms["proper_terms"]):
        return True
    return False


def _clean_fts_token(token: str) -> str:
    """Sanitize a token for safe inclusion in FTS5 content or double-quoted queries."""
    cleaned = token.replace('"', "").replace("\x00", "").strip()
    if not cleaned or not _HAS_WORD_CHAR_RE.search(cleaned):
        return ""
    return cleaned


def tokenize_for_fts(text: str, extra_keywords: list[str] | None = None) -> str:
    """Produce a space-separated, deduplicated FTS5 index string for text + extra_keywords.

    Omits noisy sliding CJK trigrams to keep the FTS5 index compact while preserving
    ASCII/mixed tokens, CJK segments, CJK bigrams, and verbatim extra_keywords.
    """
    tokens: list[str] = []
    seen: set[str] = set()

    def _add(tok: str) -> None:
        t = _clean_fts_token(tok)
        if t and t not in seen:
            seen.add(t)
            tokens.append(t)

    ordered = _extract_ordered_terms(text)
    for tok in ordered["ascii_list"]:
        _add(tok)
    for seg in ordered["cjk_segments"]:
        _add(seg)
    for bg in ordered["cjk_bigrams_list"]:
        _add(bg)

    if extra_keywords:
        for raw_kw in extra_keywords:
            if not raw_kw:
                continue
            kw = re.sub(r"\s+", " ", _normalize_nfkc(raw_kw)).strip().lower()
            if not kw:
                continue
            # Add verbatim keyword (splitting whitespace if multi-word)
            for part in kw.split(" "):
                _add(part)
            # Also add its extracted ASCII/mixed tokens, CJK segments & bigrams
            kw_ord = _extract_ordered_terms(kw)
            for tok in kw_ord["ascii_list"]:
                _add(tok)
            for seg in kw_ord["cjk_segments"]:
                _add(seg)
            for bg in kw_ord["cjk_bigrams_list"]:
                _add(bg)

    return " ".join(tokens)


def build_fts_query(text: str, max_terms: int = 16) -> str:
    """Build a syntax-safe SQLite FTS5 OR query string from text.

    Preserves natural appearance / specificity order (rather than Unicode alphabetical
    sorting) so late-Unicode nouns (香菜, 鸿蒙, 黑五, 鼠标, 黄金, 龙虾) are never
    dropped when capping to `max_terms`.
    Wraps each sanitized term in double quotes so FTS5 operators/punctuation
    never trigger syntax errors. Returns "" if no valid terms exist.
    """
    if not text or max_terms <= 0:
        return ""

    ord_terms = _extract_ordered_terms(text)
    ordered: list[str] = []
    seen: set[str] = set()

    def _push(items: list[str]) -> None:
        for item in items:
            if len(ordered) >= max_terms:
                return
            t = _clean_fts_token(item)
            if t and t not in seen:
                seen.add(t)
                ordered.append(t)

    # Non-generic terms prioritized first, preserving appearance order on length ties
    proper_non_generic = [t for t in ord_terms["proper_list"] if t not in _GENERIC_AUTO_KEYWORDS]
    ascii_non_generic = [t for t in ord_terms["ascii_list"] if t not in _GENERIC_AUTO_KEYWORDS]
    segs_non_generic = [t for t in ord_terms["cjk_segments"] if t not in _GENERIC_AUTO_KEYWORDS]
    bgs_non_generic = [t for t in ord_terms["cjk_bigrams_list"] if t not in _GENERIC_AUTO_KEYWORDS]
    bgs_generic = [t for t in ord_terms["cjk_bigrams_list"] if t in _GENERIC_AUTO_KEYWORDS]

    # Python sort is Timsort (stable), so key=lambda x: -len(x) preserves exact appearance order!
    _push(sorted(proper_non_generic, key=lambda x: -len(x)))
    _push(sorted(ascii_non_generic, key=lambda x: -len(x)))
    _push([s for s in segs_non_generic if len(s) <= 6])
    _push(bgs_non_generic)
    # Fallback passes if budget remains
    _push([s for s in segs_non_generic if len(s) > 6])
    _push(bgs_generic)

    if not ordered:
        return ""
    return " OR ".join(f'"{t}"' for t in ordered)


def auto_keywords(text: str, max_keywords: int = 8) -> list[str]:
    """Extract up to max_keywords clean, specific keywords from text.

    Keeps proper terms, ASCII/mixed entities, and clean CJK segments (<=10 chars)
    while filtering generic high-frequency words and avoiding sliding 3-char garbage.
    """
    if not text or max_keywords <= 0:
        return []

    ord_terms = _extract_ordered_terms(text)
    # (priority_bucket, negative_len, appearance_idx, token)
    candidates: list[tuple[int, int, int, str]] = []
    idx = 0

    for tok in ord_terms["proper_list"]:
        if tok in STOPWORDS or tok in _GENERIC_AUTO_KEYWORDS:
            continue
        candidates.append((0, -len(tok), idx, tok))
        idx += 1

    for tok in ord_terms["ascii_list"]:
        if tok in STOPWORDS or tok in _GENERIC_AUTO_KEYWORDS:
            continue
        candidates.append((1, -len(tok), idx, tok))
        idx += 1

    for seg in ord_terms["cjk_segments"]:
        if seg in STOPWORDS or seg in _GENERIC_AUTO_KEYWORDS:
            continue
        seg_len = len(seg)
        if 2 <= seg_len <= 6:
            candidates.append((1, -seg_len, idx, seg))
            idx += 1
        elif 7 <= seg_len <= 10:
            candidates.append((2, -seg_len, idx, seg))
            idx += 1

    candidates.sort()
    result: list[str] = []
    seen: set[str] = set()
    for _, _, _, tok in candidates:
        key = tok.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(tok)
        if len(result) >= max_keywords:
            break
    return result


def _similarity_token_set(norm_text: str) -> set[str]:
    """Build lexical token set (raw ASCII tokens + raw CJK bigrams) for Dice similarity."""
    tokens: set[str] = set()
    for m in _ASCII_TOKEN_RE.finditer(norm_text):
        tok = m.group(0)
        if len(tok) >= 2:
            tokens.add(tok)
    for run in _CJK_RUN_RE.findall(norm_text):
        if len(run) >= 2:
            for i in range(len(run) - 1):
                tokens.add(run[i : i + 2])
        else:
            tokens.add(run)
    if not tokens and norm_text:
        if len(norm_text) >= 2:
            for i in range(len(norm_text) - 1):
                tokens.add(norm_text[i : i + 2])
        else:
            tokens.add(norm_text)
    return tokens


def text_similarity(text_a: str, text_b: str) -> float:
    """Compute lexical similarity in [0.0, 1.0] tuned for near-duplicate detection (>= 0.85).

    Uses Dice coefficient over normalized ASCII tokens and CJK character bigrams,
    with a polarity penalty when one text contains negation/opposition and the
    other does not (preventing contradictions like 喜欢吃香菜 vs 不喜欢吃香菜
    from being collapsed as near-duplicates).
    """
    if not text_a or not text_b:
        return 0.0

    str_a = _normalize_nfkc(text_a)
    str_b = _normalize_nfkc(text_b)
    norm_a = _strip_non_content_chars(str_a).lower()
    norm_b = _strip_non_content_chars(str_b).lower()
    if not norm_a or not norm_b:
        return 0.0
    if norm_a == norm_b:
        return 1.0

    set_a = _similarity_token_set(norm_a)
    set_b = _similarity_token_set(norm_b)
    if not set_a or not set_b:
        return 0.0

    overlap = len(set_a & set_b)
    dice = (2.0 * overlap) / (len(set_a) + len(set_b))

    # Polarity mismatch check: avoid treating opposite statements as near-duplicates
    neg_a = len(_NEGATION_RE.findall(str_a))
    neg_b = len(_NEGATION_RE.findall(str_b))
    if neg_a != neg_b:
        dice *= 0.75

    return round(min(1.0, max(0.0, dice)), 4)


# ---------------------------------------------------------------------------
# Temporal Clue Extraction (Beijing UTC+8)
# ---------------------------------------------------------------------------
_EXPLICIT_YMD_RE = re.compile(
    r"(?<!\d)((?:19|20)\d{2})[-/年](\d{1,2})[-/月](\d{1,2})[日号]?(?!\d)"
)
_EXPLICIT_MD_RE = re.compile(r"(?<!\d)(\d{1,2})月(\d{1,2})[日号](?!\d)")

_CN_DIGIT_MAP = {
    "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}


def _parse_cn_or_arabic_int(raw: str) -> int | None:
    s = str(raw or "").strip()
    if not s:
        return None
    if s.isdigit():
        try:
            val = int(s)
            return val if val > 0 else None
        except ValueError:
            return None
    if s == "十":
        return 10
    if len(s) == 1 and s in _CN_DIGIT_MAP:
        return _CN_DIGIT_MAP[s]
    if "十" in s:
        parts = s.split("十", 1)
        tens = 1 if parts[0] == "" else _CN_DIGIT_MAP.get(parts[0])
        ones = 0 if parts[1] == "" else _CN_DIGIT_MAP.get(parts[1])
        if tens is not None and ones is not None:
            return tens * 10 + ones
    return None


def _today_cn(now_dt: datetime.datetime | None = None) -> datetime.date:
    if now_dt is None:
        return datetime.datetime.now(tz=_TZ_CN).date()
    if isinstance(now_dt, datetime.datetime):
        if now_dt.tzinfo is not None:
            return now_dt.astimezone(_TZ_CN).date()
        return now_dt.date()
    if isinstance(now_dt, datetime.date):
        return now_dt
    return datetime.datetime.now(tz=_TZ_CN).date()


def _resolve_md_date(today: datetime.date, month: int, day: int) -> datetime.date | None:
    """Resolve (month, day) relative to today, safely handling 2月29日 on non-leap years."""
    year = today.year
    if month == 2 and day == 29:
        for y in range(year, year - 9, -1):
            try:
                cand = datetime.date(y, 2, 29)
            except ValueError:
                continue
            if cand <= today + datetime.timedelta(days=30):
                return cand
        return None
    try:
        target = datetime.date(year, month, day)
    except ValueError:
        return None
    if target > today + datetime.timedelta(days=30):
        try:
            target = datetime.date(year - 1, month, day)
        except ValueError:
            pass
    return target


_RELATIVE_TIME_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Match "去年今天" BEFORE "今天" and "去年"!
    (re.compile(r"去年的?今天|去年今日"), "last_year_today"),
    # Match "最近N天" / "近N天" / "N天前" BEFORE generic "最近"!
    (re.compile(r"(?:最近|近|过去)(\d{1,3}|[一二两三四五六七八九十]{1,3})天"), "recent_nd"),
    (re.compile(r"(\d{1,3}|[一二两三四五六七八九十]{1,3})天前"), "n_days_ago"),
    (re.compile(r"今天早上|今天上午|今天下午|今天晚上|今天|今日|刚刚|刚才|今晚|今早"), "today"),
    (re.compile(r"明天早上|明天上午|明天下午|明天晚上|明天|明日|明早|明晚"), "tomorrow"),
    (re.compile(r"昨天早上|昨天上午|昨天下午|昨天晚上|昨天|昨日|昨晚"), "yesterday"),
    (re.compile(r"前天|前晚"), "day_before_yesterday"),
    (re.compile(r"最近|这几天|前几天|近几天|近期"), "recent_14d"),
    (re.compile(r"这周|本周|这星期|本星期|这礼拜|本礼拜"), "this_week"),
    (re.compile(r"上周|上星期|上礼拜"), "last_week"),
    (re.compile(r"这个月|本月"), "this_month"),
    (re.compile(r"上个月|上月"), "last_month"),
    (re.compile(r"今年"), "this_year"),
    (re.compile(r"去年"), "last_year"),
]


def extract_time_clues(
    text: str,
    now_dt: datetime.datetime | None = None,
) -> dict | None:
    """Extract a temporal window from Chinese date/time phrases in text.

    Uses Beijing time (UTC+8) when now_dt is None, and converts aware now_dt
    to Beijing time (UTC+8) before extracting the calendar date.
    Returns {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD", "label": matched_phrase}
    or None if no temporal clue is found.
    """
    if not text:
        return None
    s = _normalize_nfkc(text)
    today = _today_cn(now_dt)

    # 1. Explicit YYYY-MM-DD / YYYY/MM/DD / YYYY年M月D日
    m_ymd = _EXPLICIT_YMD_RE.search(s)
    if m_ymd:
        try:
            year, month, day = int(m_ymd.group(1)), int(m_ymd.group(2)), int(m_ymd.group(3))
            target = datetime.date(year, month, day)
            start = target - datetime.timedelta(days=1)
            end = target + datetime.timedelta(days=1)
            return {
                "start": start.strftime("%Y-%m-%d"),
                "end": end.strftime("%Y-%m-%d"),
                "label": m_ymd.group(0),
            }
        except ValueError:
            pass

    # 2. Explicit M月D日/号 (safely resolves 2月29日 on non-leap years)
    m_md = _EXPLICIT_MD_RE.search(s)
    if m_md:
        month, day = int(m_md.group(1)), int(m_md.group(2))
        target = _resolve_md_date(today, month, day)
        if target is not None:
            start = target - datetime.timedelta(days=1)
            end = target + datetime.timedelta(days=1)
            return {
                "start": start.strftime("%Y-%m-%d"),
                "end": end.strftime("%Y-%m-%d"),
                "label": m_md.group(0),
            }

    # 3. Relative Chinese time phrases
    for pattern, kind in _RELATIVE_TIME_PATTERNS:
        m = pattern.search(s)
        if not m:
            continue
        label = m.group(0)
        if kind == "last_year_today":
            try:
                d = datetime.date(today.year - 1, today.month, today.day)
            except ValueError:
                # Leap day Feb 29 -> Feb 28 of previous year
                d = datetime.date(today.year - 1, 2, 28)
            start, end = d, d
        elif kind == "recent_nd":
            n = _parse_cn_or_arabic_int(m.group(1))
            if not n:
                continue
            n = min(365, max(1, n))
            start = today - datetime.timedelta(days=n)
            end = today
        elif kind == "n_days_ago":
            n = _parse_cn_or_arabic_int(m.group(1))
            if not n:
                continue
            n = min(3650, max(1, n))
            d = today - datetime.timedelta(days=n)
            start, end = d, d
        elif kind == "today":
            start, end = today, today
        elif kind == "tomorrow":
            d = today + datetime.timedelta(days=1)
            start, end = d, d
        elif kind == "yesterday":
            d = today - datetime.timedelta(days=1)
            start, end = d, d
        elif kind == "day_before_yesterday":
            d = today - datetime.timedelta(days=2)
            start, end = d, d
        elif kind == "recent_14d":
            start, end = today - datetime.timedelta(days=14), today
        elif kind == "this_week":
            start = today - datetime.timedelta(days=today.weekday())
            end = today
        elif kind == "last_week":
            this_mon = today - datetime.timedelta(days=today.weekday())
            start = this_mon - datetime.timedelta(days=7)
            end = this_mon - datetime.timedelta(days=1)
        elif kind == "this_month":
            start = today.replace(day=1)
            end = today
        elif kind == "last_month":
            last_day_prev = today.replace(day=1) - datetime.timedelta(days=1)
            start = last_day_prev.replace(day=1)
            end = last_day_prev
        elif kind == "this_year":
            start = datetime.date(today.year, 1, 1)
            end = datetime.date(today.year, 12, 31)
        elif kind == "last_year":
            start = datetime.date(today.year - 1, 1, 1)
            end = datetime.date(today.year - 1, 12, 31)
        else:
            continue
        return {
            "start": start.strftime("%Y-%m-%d"),
            "end": end.strftime("%Y-%m-%d"),
            "label": label,
        }

    return None


# ---------------------------------------------------------------------------
# Admin DM Cross-Group Recap & Highlight Intent Detection
# ---------------------------------------------------------------------------
_ADMIN_RECAP_CUE_RE = re.compile(
    r"(?:"
    r"发生了(?:什么|啥)|发生啥了|"
    r"有(?:什么|啥)(?:重点|大事|新鲜事|新动态|动态|热点|要闻|好玩|消息)|"
    r"(?:今天|今日|昨天|昨日|前天|最近|近期|这几天|这周|本周|上周|这个月|本月|群里|各群|所有群|几个群|哪些群|主群)"
    r"[\u4e00-\u9fff]{0,4}(?:重点|大事|动态|亮点|要闻|热点|聊了(?:什么|啥)|聊啥|说了(?:什么|啥)|都在聊|在聊(?:什么|啥)|讨论了(?:什么|啥)|情况)|"
    r"(?:今天|昨天|最近|这周|上周)群里|"
    r"群里(?:今天|今日|昨天|昨日|最近|近期|这周|本周|上周|聊了(?:什么|啥)|聊啥|都在聊|在聊(?:什么|啥)|有(?:什么|啥)|发生了|发生啥|动态|情况|重点)|"
    r"大家(?:都在聊|在聊(?:什么|啥)|聊了(?:什么|啥)|最近在聊)|"
    r"(?:总结|汇总|概括|梳理|盘点)(?:一下|下)?(?:今天|今日|昨天|昨日|最近|这周|本周|上周|群里|各群|所有群|聊天|消息|重点|动态)?|"
    r"汇总|简报|日报|周报|复盘|大事记|"
    r"动态(?!规划|链接|库|数组|内存|分配|加载|编译|绑定|类型|代理)|"
    r"(?:各群|所有群|每个群|全部群|几个群|哪些群)(?:的)?(?:动态|情况|聊|重点|总结|汇总|消息|发生|活跃)?"
    r")"
)

_ADMIN_ALL_GROUPS_RE = re.compile(r"各群|所有群|全部群|每个群|几个群|哪些群")

_ADMIN_CROSS_GROUP_CUE_RE = re.compile(
    r"(?:群里|群友|群聊|群消息|群动态|各群|所有群|全部群|每个群|几个群|哪些群|主群|大群|小群|大家在聊|大家都在聊|大家最近)"
)

_NUMERIC_GROUP_WITH_WORD_RE = re.compile(
    r"(?:(?:群号|群聊|群)\s*(\d{5,12})(?!\d)|(?<!\d)(\d{5,12})\s*(?:这个|那个)?群)"
)
_BARE_GROUP_ID_RE = re.compile(r"(?<!\d)(\d{5,12})(?!\d)")
_GROUP_PHRASE_RE = re.compile(r"([\u4e00-\u9fffA-Za-z0-9_-]{2,12})群")
_GROUP_PHRASE_GENERIC_PREFIXES = frozenset(
    {
        "今天", "今日", "昨天", "昨日", "前天", "最近", "近期", "这周", "本周", "上周",
        "所有", "全部", "每个", "几个", "哪些", "这个", "那个", "我们", "你们", "他们",
        "大家", "聊天", "消息", "动态", "总结", "汇总", "复盘", "一下", "什么", "怎么",
        "一个", "两个", "三个", "几个", "很多", "好多",
    }
)


def detect_admin_recap_intent(
    query: str,
    known_groups: dict[str, str] | None = None,
    now_dt: datetime.datetime | None = None,
) -> dict:
    """Detect Admin DM cross-group recap, highlight, or group-targeted query intent.

    Returns:
      {
        "is_recap": bool,
        "is_cross_group": bool,
        "target_group_ids": list[str],
        "all_groups": bool,
        "time_clue": dict | None,
      }
    """
    if not query:
        return {
            "is_recap": False,
            "is_cross_group": False,
            "target_group_ids": [],
            "all_groups": False,
            "time_clue": None,
        }

    s = _normalize_nfkc(query).strip()
    if not s:
        return {
            "is_recap": False,
            "is_cross_group": False,
            "target_group_ids": [],
            "all_groups": False,
            "time_clue": None,
        }

    s_low = s.lower()
    groups_map: dict[str, str] = {}
    if isinstance(known_groups, dict):
        for raw_gid, raw_name in known_groups.items():
            gid = str(raw_gid or "").strip()
            if gid:
                groups_map[gid] = _normalize_nfkc(str(raw_name or "")).strip()

    target_group_ids: list[str] = []
    seen_gids: set[str] = set()

    def _add_gid(gid: str) -> None:
        g = str(gid or "").strip()
        if g and g not in seen_gids:
            seen_gids.add(g)
            target_group_ids.append(g)

    # 1. Explicit numeric group IDs paired with 群 (works even when known_groups is empty)
    for m in _NUMERIC_GROUP_WITH_WORD_RE.finditer(s):
        gid = m.group(1) or m.group(2)
        if gid:
            _add_gid(gid)

    # 2. Bare numeric group IDs matching known_groups keys
    if groups_map:
        for m in _BARE_GROUP_ID_RE.finditer(s):
            gid = m.group(1)
            if gid in groups_map:
                _add_gid(gid)

        # 3. Direct group name (or stem without trailing 群) substring match
        for gid, gname in groups_map.items():
            gname_low = gname.lower()
            if len(gname_low) < 2:
                continue
            if gname_low in s_low:
                _add_gid(gid)
                continue
            if gname_low.endswith("群") and len(gname_low) >= 3:
                stem = gname_low[:-1].strip()
                if len(stem) >= 2 and stem in s_low:
                    _add_gid(gid)
                    continue

        # 4. Query phrase "<alias>群" -> match alias against known group names
        for m in _GROUP_PHRASE_RE.finditer(s):
            raw_prefix = m.group(1).strip()
            if not raw_prefix or raw_prefix.isdigit():
                continue
            # Trim common leading glue/time words from phrase prefix
            candidates = [raw_prefix]
            if len(raw_prefix) >= 3:
                candidates.append(raw_prefix[-2:])
            if len(raw_prefix) >= 4:
                candidates.append(raw_prefix[-3:])
            for cand in candidates:
                if len(cand) < 2 or cand in _GROUP_PHRASE_GENERIC_PREFIXES or cand in STOPWORDS:
                    continue
                cand_low = cand.lower()
                for gid, gname in groups_map.items():
                    gname_low = gname.lower()
                    if len(gname_low) >= 2 and cand_low in gname_low:
                        _add_gid(gid)

    # Special handling for "主群" when no group literally contains "主群"
    mentions_main_group = "主群" in s
    explicit_all_groups = bool(_ADMIN_ALL_GROUPS_RE.search(s))
    is_recap = bool(_ADMIN_RECAP_CUE_RE.search(s))

    # Guard: targeted member/item question like "群里谁养猫" or "100000001群谁在玩黑神话"
    # is cross-group, NOT a broad recap, unless it also has an explicit recap cue.
    if is_recap and re.search(r"群里谁|群友谁", s) and not re.search(
        r"重点|大事|总结|汇总|简报|日报|周报|复盘|动态|发生了什么|发生啥|聊了什么|聊了啥", s
    ):
        is_recap = False

    is_cross_group = bool(
        is_recap
        or target_group_ids
        or explicit_all_groups
        or mentions_main_group
        or _ADMIN_CROSS_GROUP_CUE_RE.search(s)
    )

    if explicit_all_groups or (mentions_main_group and not target_group_ids):
        all_groups = True
    elif target_group_ids:
        all_groups = False
    else:
        all_groups = is_recap or is_cross_group

    time_clue = extract_time_clues(s, now_dt=now_dt)
    if is_recap and time_clue is None:
        today = _today_cn(now_dt)
        start = today - datetime.timedelta(days=1)
        time_clue = {
            "start": start.strftime("%Y-%m-%d"),
            "end": today.strftime("%Y-%m-%d"),
            "label": "近期/今天",
        }

    return {
        "is_recap": is_recap,
        "is_cross_group": is_cross_group,
        "target_group_ids": target_group_ids,
        "all_groups": all_groups,
        "time_clue": time_clue,
    }


def compute_3gram_repetition_ratio(text: str) -> float:
    """Compute sliding 3-char gram repetition ratio in [0.0, 1.0].

    Strips whitespace and punctuation/symbols first. Returns 0.0 when cleaned
    length < 9. High values (> 0.7) indicate spam/looping text.
    """
    if not text:
        return 0.0
    core = _strip_non_content_chars(_normalize_nfkc(text)).lower()
    if len(core) < 9:
        return 0.0
    grams = [core[i : i + 3] for i in range(len(core) - 2)]
    return 1.0 - (len(set(grams)) / len(grams))
