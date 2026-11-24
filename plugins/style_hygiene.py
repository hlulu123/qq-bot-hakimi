"""Built-in plugin: Per-turn style reinforcement & motif/opener anti-repetition."""

from typing import Any, Dict

from bot_plugins import BotPlugin


class StyleHygienePlugin(BotPlugin):
    id = "style_hygiene"
    name = "每轮风格强化与口癖防重"
    version = "1.0.0"
    category = "enhancement"
    order = 10
    description = (
        "每轮非任务消息自动注入约 328 字的 [回复风格] 复述提醒，以及口癖词与语气垫词防重警告。"
        "因 GEMINI.md 已包含完整说话风格，关闭本插件可直接省去每轮重复注入的风格提示词。"
    )
    author = "内置"
    builtin = True
    default_enabled = True
    hot_reload = True
    affects_sandbox_prompt = False
    related_views = ("persona", "config")

    token_impact = {
        "level": "high",
        "per_turn_tokens": 285,
        "per_session_tokens": 0,
        "background_llm": False,
        "extra_turns": False,
        "summary": "每轮非任务对话必注 328~416 字风格与防重提醒（且随多轮会话累积计费）",
    }

    settings_schema = (
        {
            "key": "inject_style_reminder",
            "label": "每轮注入 [回复风格] 提醒（328 字）",
            "type": "bool",
            "default": True,
            "description": "关闭此项即可省下绝大部分 Token（约 240 tokens/轮），仅靠 GEMINI.md 保持语气",
        },
        {
            "key": "inject_motif_guard",
            "label": "注入高频口癖与开头垫词防重提示",
            "type": "bool",
            "default": True,
            "description": "检测到最近重复用过「喵/冻干/嗯」等词时追加约 45~85 字提醒",
        },
        {
            "key": "casual_postprocess",
            "label": "本地短句口语化后处理（零 Token）",
            "type": "bool",
            "default": True,
            "description": "纯本地正则：将短回复逗号转空格并去掉句尾句号，不消耗任何 Token",
        },
    )

    def estimate_token_usage(
        self, enabled: bool, settings: Dict[str, Any]
    ) -> Dict[str, Any]:
        base = super().estimate_token_usage(enabled, settings)
        if not enabled:
            return base
        active = 0
        if settings.get("inject_style_reminder", True):
            active += 245
        if settings.get("inject_motif_guard", True):
            active += 40
        base["per_turn_active"] = active
        return base
