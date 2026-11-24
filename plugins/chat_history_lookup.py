"""Built-in plugin: Model self-service chat history lookup ([[QQ:history ...]])."""

from typing import Any, Dict, List

from bot_plugins import BotPlugin
from plugins._helpers import strip_markdown_items


class ChatHistoryLookupPlugin(BotPlugin):
    id = "chat_history_lookup"
    name = "聊天记录自助翻查"
    version = "1.0.0"
    category = "memory"
    order = 50
    description = (
        "允许模型使用 [[QQ:history ...]] 指令自行搜索或往前翻阅本会话历史记录（最多 3 轮追查）。"
        "关闭后每轮立省约 560~690 字指令提示词，并彻底消除多轮内部翻查调用。"
    )
    author = "内置"
    builtin = True
    default_enabled = True
    hot_reload = True
    affects_sandbox_prompt = True
    capabilities = ("chat_history_lookup",)
    env_flags = ("CHAT_HISTORY_ENABLED",)
    related_views = ("permissions", "config")

    token_impact = {
        "level": "very_high",
        "per_turn_tokens": 410,
        "per_session_tokens": 155,
        "background_llm": False,
        "extra_turns": True,
        "summary": "每轮必注约 350~530 tokens 翻查说明；触发时额外消耗 1~3 整轮及最高 10k tokens",
    }

    settings_schema = (
        {
            "key": "passive_archive",
            "label": "关闭时仍保留零 Token 本地记录归档与引用解析",
            "type": "bool",
            "default": True,
            "description": "纯本地 SQLite 静默存档（零 LLM 消耗），可加速引用较早消息的原文展开",
        },
    )

    def filter_sandbox_gemini_lines(
        self, lines: List[str], enabled: bool, settings: Dict[str, Any]
    ) -> List[str]:
        if enabled:
            return lines
        return strip_markdown_items(
            lines,
            lambda s: "[[QQ:history" in s or "不记得原文或不知道前文就自己翻" in s,
        )
