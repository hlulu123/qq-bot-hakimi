"""Built-in plugin: Group consecutive repeat-chain follow."""

from typing import Any, Dict, List

from bot_plugins import BotPlugin
from plugins._helpers import strip_markdown_items


class RepeatChainPlugin(BotPlugin):
    id = "repeat_chain"
    name = "群聊跟风复读"
    version = "1.0.0"
    category = "interaction"
    order = 30
    description = (
        "检测群内不同群友连续发送 2 条及以上相同文字或表情包，绕过主动冒泡节流询问模型是否输出 "
        "FOLLOW_REPEAT 原样跟风。关闭后不再检测复读链，也不触发额外的复读判断轮次。"
    )
    author = "内置"
    builtin = True
    default_enabled = True
    hot_reload = True
    affects_sandbox_prompt = True
    capabilities = ("repeat_follow",)
    env_flags = ("REPEAT_ENABLED",)
    related_views = ("permissions", "config", "workbench")

    token_impact = {
        "level": "medium",
        "per_turn_tokens": 110,
        "per_session_tokens": 175,
        "background_llm": False,
        "extra_turns": True,
        "summary": "每次复读链会额外触发 1 整轮模型判断并注入约 160 字指令",
    }

    def filter_sandbox_gemini_lines(
        self, lines: List[str], enabled: bool, settings: Dict[str, Any]
    ) -> List[str]:
        if enabled:
            return lines
        return strip_markdown_items(
            lines,
            lambda s: (
                "FOLLOW_REPEAT" in s
                or "**复读**" in s
                or "连着在复读" in s
            ),
        )
