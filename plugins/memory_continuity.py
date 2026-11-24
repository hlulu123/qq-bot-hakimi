"""Built-in plugin: Cross-session continuity replay on fresh sessions."""

from typing import Any, Dict

from bot_plugins import BotPlugin


class MemoryContinuityPlugin(BotPlugin):
    id = "memory_continuity"
    name = "跨会话对话续接"
    version = "1.0.0"
    category = "memory"
    order = 20
    description = (
        "机器人重启或新开会话（/new）后的首轮对话，自动注入最近会话摘要（session_handoffs）与原文回放，"
        "避免重启失忆。关闭后每个新会话首轮最高可省约 2,400 字符上下文。"
    )
    author = "内置"
    builtin = True
    default_enabled = True
    hot_reload = True
    affects_sandbox_prompt = False
    env_flags = ("USER_MEMORY_ENABLED",)
    related_views = ("memory", "config")

    token_impact = {
        "level": "high",
        "per_turn_tokens": 0,
        "per_session_tokens": 1600,
        "background_llm": False,
        "extra_turns": False,
        "summary": "每个新会话首轮注入最多 2,400 字（~1,400~1,800 tokens）续接与原话回放",
    }

    settings_schema = (
        {
            "key": "max_chars",
            "label": "续接块字符预算上限",
            "type": "int",
            "default": 2400,
            "min": 400,
            "max": 4800,
            "description": "控制单次新会话首轮注入的最大字符数（默认 2400）",
        },
    )

    def estimate_token_usage(
        self, enabled: bool, settings: Dict[str, Any]
    ) -> Dict[str, Any]:
        base = super().estimate_token_usage(enabled, settings)
        if not enabled:
            return base
        cap = int(settings.get("max_chars") or 2400)
        ratio = max(0.2, min(1.5, float(cap) / 2400.0))
        base["per_session_active"] = int(round(base["per_session_max"] * ratio))
        return base
