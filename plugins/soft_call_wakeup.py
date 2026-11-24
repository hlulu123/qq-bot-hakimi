"""Built-in plugin: Fuzzy cat-word soft-call wakeups in group chats."""

from typing import Any, Dict, List

from bot_plugins import BotPlugin
from plugins._helpers import strip_markdown_items


class SoftCallWakeupPlugin(BotPlugin):
    id = "soft_call_wakeup"
    name = "模糊昵称唤醒"
    version = "1.0.0"
    category = "interaction"
    order = 20
    description = (
        "群聊中有人提到「猫猫 / 小猫 / 小紫 / 紫毛」等模糊猫系词时自动唤醒机器人判断是不是在叫自己。"
        "关闭后仅响应明确的 @、引用回复与精确点名词，避免聊真猫时误触发整轮模型调用。"
    )
    author = "内置"
    builtin = True
    default_enabled = True
    hot_reload = True
    affects_sandbox_prompt = True
    related_views = ("config",)

    token_impact = {
        "level": "high",
        "per_turn_tokens": 55,
        "per_session_tokens": 50,
        "background_llm": False,
        "extra_turns": True,
        "summary": "模糊词会绕过静默监听并触发额外整轮调用，关闭可彻底避免误唤醒",
    }

    def filter_sandbox_gemini_lines(
        self, lines: List[str], enabled: bool, settings: Dict[str, Any]
    ) -> List[str]:
        if enabled:
            return lines
        return strip_markdown_items(
            lines,
            lambda s: "有人喊" in s and ("猫猫" in s or "小紫" in s),
        )
