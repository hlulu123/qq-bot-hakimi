"""Built-in plugin: Ambient group listening & proactive bubbling."""

from typing import Any, Dict, List

from bot_plugins import BotPlugin
from plugins._helpers import strip_markdown_items


class AmbientListenPlugin(BotPlugin):
    id = "ambient_listen"
    name = "群聊主动潜水冒泡"
    version = "1.0.0"
    category = "interaction"
    order = 10
    description = (
        "允许机器人在群里没人点名时被动监听闲聊，看到能接的梗或续聊时机主动冒泡。"
        "关闭后群聊转为纯 @ / 回复 / 点名触发模式，是最立竿见影的省流开关。"
    )
    author = "内置"
    builtin = True
    default_enabled = True
    hot_reload = True
    affects_sandbox_prompt = True
    env_flags = ("GUARD_AMBIENT_LISTEN",)
    related_views = ("config", "workbench", "overview")

    token_impact = {
        "level": "very_high",
        "per_turn_tokens": 110,
        "per_session_tokens": 230,
        "background_llm": False,
        "extra_turns": True,
        "summary": "高活跃群可使 LLM 总调用次数放大 3~10 倍（即使回 NO_REPLY 也消耗整轮上下文）",
    }

    settings_schema = (
        {
            "key": "active_conv_followup",
            "label": "允许刚说话后的短时续聊",
            "type": "bool",
            "default": True,
            "description": "机器人刚在群里回复后，短时间内有人接着聊是否继续跟进（仅本插件开启时生效）",
        },
    )

    def filter_sandbox_gemini_lines(
        self, lines: List[str], enabled: bool, settings: Dict[str, Any]
    ) -> List[str]:
        if enabled:
            return lines
        return strip_markdown_items(
            lines,
            lambda s: (
                "你刚说完话，有人接着聊" in s
                or "没人叫你" in s
                or "群友（没叫你）" in s
                or "群友（普通水群）" in s
                or "群友 A 对群友 B" in s
            ),
        )
