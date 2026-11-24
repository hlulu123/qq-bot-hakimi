"""Built-in plugin: Read-only web search tool (search_web)."""

from typing import Any, Dict, List

from bot_plugins import BotPlugin
from plugins._helpers import strip_markdown_items


class WebSearchPlugin(BotPlugin):
    id = "web_search"
    name = "只读联网搜索"
    version = "1.0.0"
    category = "capabilities"
    order = 20
    description = (
        "允许机器人在群聊和普通私聊中调用只读联网搜索工具（search_web）核实最新动态、版本号与事实。"
        "关闭后可避免搜索工具调用与大量网页摘要返回带来的 Token 消耗。"
    )
    author = "内置"
    builtin = True
    default_enabled = True
    hot_reload = True
    affects_sandbox_prompt = True
    capabilities = ("web_search",)
    related_views = ("permissions",)

    token_impact = {
        "level": "medium",
        "per_turn_tokens": 25,
        "per_session_tokens": 145,
        "background_llm": False,
        "extra_turns": True,
        "summary": "每次联网查证额外消耗约 1,000~4,000+ 工具返回 Token 与额外推理轮次",
    }

    def filter_sandbox_gemini_lines(
        self, lines: List[str], enabled: bool, settings: Dict[str, Any]
    ) -> List[str]:
        if enabled:
            return lines
        return strip_markdown_items(
            lines,
            lambda s: (
                ("涉及最新动态" in s and "`search_web`" in s)
                or "认真提问，先联网核实" in s
                or "群友（查不到）" in s
            ),
        )
