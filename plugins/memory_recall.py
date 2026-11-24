"""Built-in plugin: Long-term user & group memory retrieval injection."""

from typing import Any, Dict, List

from bot_plugins import BotPlugin
from plugins._helpers import strip_markdown_items


class MemoryRecallPlugin(BotPlugin):
    id = "memory_recall"
    name = "长期记忆检索注入"
    version = "1.0.0"
    category = "memory"
    order = 10
    description = (
        "每轮同步检索 SQLite 记忆库，按需注入 L0 热画像与 L2 混合召回记忆（[相关用户长期记忆]）。"
        "关闭后不再向提示词注入长期记忆，已有记忆数据仍保留在本地数据库并可在「长期记忆」页查看。"
    )
    author = "内置"
    builtin = True
    default_enabled = True
    hot_reload = True
    affects_sandbox_prompt = True
    env_flags = ("USER_MEMORY_ENABLED",)
    related_views = ("memory", "config")

    token_impact = {
        "level": "high",
        "per_turn_tokens": 380,
        "per_session_tokens": 75,
        "background_llm": False,
        "extra_turns": False,
        "summary": "L0 热画像（每 12 轮刷新，最高 ~850 tok）+ L2 混合召回（每轮最高 ~450 tok）",
    }

    settings_schema = (
        {
            "key": "group_memory_enabled",
            "label": "启用群共享记忆召回",
            "type": "bool",
            "default": True,
            "description": "关闭后群聊仅召回用户公开记忆，不再召回群事件与群内共享梗",
        },
        {
            "key": "max_chars_override",
            "label": "L2 召回字符预算上限（0 = 跟随 bot.env）",
            "type": "int",
            "default": 0,
            "min": 0,
            "max": 3000,
            "description": "热生效覆盖 MEMORY_RECALL_MAX_CHARS，调低如 300 可显著压缩记忆注入体积",
        },
        {
            "key": "max_items_override",
            "label": "L2 召回条数上限（0 = 跟随 bot.env）",
            "type": "int",
            "default": 0,
            "min": 0,
            "max": 12,
            "description": "热生效覆盖 MEMORY_RECALL_MAX_ITEMS（默认 4 条）",
        },
    )

    def estimate_token_usage(
        self, enabled: bool, settings: Dict[str, Any]
    ) -> Dict[str, Any]:
        base = super().estimate_token_usage(enabled, settings)
        if not enabled:
            return base
        cap = int(settings.get("max_chars_override") or 0)
        if 0 < cap < 600:
            ratio = max(0.35, float(cap) / 600.0)
            base["per_turn_active"] = int(round(base["per_turn_max"] * ratio))
        return base

    def filter_sandbox_gemini_lines(
        self, lines: List[str], enabled: bool, settings: Dict[str, Any]
    ) -> List[str]:
        if enabled:
            return lines
        return strip_markdown_items(
            lines,
            lambda s: "系统会附上" in s and "[相关用户长期记忆" in s,
        )
