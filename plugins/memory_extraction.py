"""Built-in plugin: Background LLM memory extraction & idle window flush."""

from bot_plugins import BotPlugin


class MemoryExtractionPlugin(BotPlugin):
    id = "memory_extraction"
    name = "后台记忆自动提炼"
    version = "1.0.0"
    category = "memory"
    order = 30
    description = (
        "在聊天窗口满阈值、命中高信号线索词或会话空闲 10 分钟时，自动在后台拉起无头 agy 子进程"
        "提炼结构化长期记忆。关闭后彻底停止所有实时与空闲后台 LLM 提炼调用（仍可在 WebUI 手动触发）。"
    )
    author = "内置"
    builtin = True
    default_enabled = True
    hot_reload = True
    affects_sandbox_prompt = False
    env_flags = ("USER_MEMORY_ENABLED",)
    related_views = ("memory", "config")

    token_impact = {
        "level": "very_high",
        "per_turn_tokens": 0,
        "per_session_tokens": 0,
        "background_llm": True,
        "extra_turns": False,
        "summary": "最大后台 Token 消耗源：每次提炼消耗约 2,500~8,000 输入 + medium 思考 Token",
    }
