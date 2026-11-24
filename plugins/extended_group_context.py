"""Built-in plugin: Extended group recent context window."""

from typing import Any, Dict

from bot_plugins import BotPlugin


class ExtendedGroupContextPlugin(BotPlugin):
    id = "extended_group_context"
    name = "扩展群聊上下文窗口"
    version = "1.0.0"
    category = "enhancement"
    order = 20
    description = (
        "开启时每轮群聊注入完整的近期消息窗口（默认按 GROUP_HISTORY_COUNT=30 条）；"
        "关闭后自动收窄为轻量核心窗口（默认 8 条），在保持分清谁在说话的同时大幅压低每轮群聊输入。"
    )
    author = "内置"
    builtin = True
    default_enabled = True
    hot_reload = True
    affects_sandbox_prompt = False
    related_views = ("config",)

    token_impact = {
        "level": "very_high",
        "per_turn_tokens": 950,
        "per_session_tokens": 0,
        "background_llm": False,
        "extra_turns": False,
        "summary": "30 条完整群上下文比 8 条精简窗口每轮多约 1,400+ 字（~850~1,200 tokens，且每轮累积）",
    }

    settings_schema = (
        {
            "key": "lean_count",
            "label": "关闭本插件时保留的轻量群消息条数",
            "type": "int",
            "default": 8,
            "min": 3,
            "max": 20,
            "description": "关闭「扩展群聊上下文窗口」后实际注入提示词的最近群消息条数（推荐 6~10 条）",
        },
    )

    def estimate_token_usage(
        self, enabled: bool, settings: Dict[str, Any]
    ) -> Dict[str, Any]:
        base = super().estimate_token_usage(enabled, settings)
        if enabled:
            return base
        # When disabled, lean_count > 8 saves slightly less than lean_count == 8
        lean = max(3, min(20, int(settings.get("lean_count") or 8)))
        extra_over_base = max(0, lean - 8)
        base["per_turn_active"] = min(base["per_turn_max"], extra_over_base * 45)
        return base
