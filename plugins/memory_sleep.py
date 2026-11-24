"""Built-in plugin: Overnight Sleep maintenance (LLM conflict arbitration & profile refresh)."""

from bot_plugins import BotPlugin


class MemorySleepPlugin(BotPlugin):
    id = "memory_sleep"
    name = "夜间记忆整理（Sleep）"
    version = "1.0.0"
    category = "memory"
    order = 40
    description = (
        "凌晨 03:00–06:00 空闲时段自动执行记忆冲突 LLM 仲裁（最多 15 组）与热画像摘要刷新（最多 10 人）。"
        "关闭后彻底停止夜间后台模型调用，同时可保留零 Token 的本地数据库归档清理。"
    )
    author = "内置"
    builtin = True
    default_enabled = True
    hot_reload = True
    affects_sandbox_prompt = False
    env_flags = ("USER_MEMORY_ENABLED", "MEMORY_SLEEP_ENABLED")
    related_views = ("memory", "config")

    token_impact = {
        "level": "medium",
        "per_turn_tokens": 0,
        "per_session_tokens": 0,
        "background_llm": True,
        "extra_turns": False,
        "summary": "每晚最多执行 25 次后台无头 LLM 调用（冲突仲裁 + 画像重写）",
    }

    settings_schema = (
        {
            "key": "keep_rule_cleanup",
            "label": "关闭时仍保留零 Token 数据库清理（Phase 1）",
            "type": "bool",
            "default": True,
            "description": "开启后即使关掉本插件，夜间仍仅用纯本地规则清理过期日志与归档（不调用任何大模型）",
        },
    )
