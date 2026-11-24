"""Built-in plugin: Auto-convert long replies into QQ merged forward cards."""

from bot_plugins import BotPlugin


class LongMsgForwardPlugin(BotPlugin):
    id = "long_msg_forward"
    name = "长回复合并转发卡片"
    version = "1.0.0"
    category = "enhancement"
    order = 40
    description = (
        "机器人单条回复超过阈值字数或行数时，在 ws-guard 网关层自动改写为 QQ 合并转发聊天记录卡片发出，"
        "避免刷屏；发送失败时自动回退普通消息。纯本地排版，不消耗任何模型 Token。"
    )
    author = "内置"
    builtin = True
    default_enabled = True
    hot_reload = True
    affects_sandbox_prompt = False
    capabilities = ("forward_long_msg",)
    env_flags = ("FORWARD_LONG_MSG_GROUP", "FORWARD_LONG_MSG_DM")
    related_views = ("permissions", "config", "workbench")

    token_impact = {
        "level": "none",
        "per_turn_tokens": 0,
        "per_session_tokens": 0,
        "background_llm": False,
        "extra_turns": False,
        "summary": "纯本地出站格式转换，零 LLM Token 开销",
    }
