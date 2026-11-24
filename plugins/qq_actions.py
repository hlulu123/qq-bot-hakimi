"""Built-in plugin: Interactive QQ actions & group admin directives."""

from typing import Any, Dict, List

from bot_plugins import BotPlugin
from plugins._helpers import strip_markdown_items


class QQActionsPlugin(BotPlugin):
    id = "qq_actions"
    name = "QQ 互动与群管动作"
    version = "1.0.0"
    category = "interaction"
    order = 40
    description = (
        "启用 [[QQ:...]] 动作指令系统（撤回、@群友、@全体、发图/跟表情包、引用回复、戳一戳、"
        "贴表情回应、禁言/踢人/改群名片/设精华/入群审批）。关闭后彻底不注入 [QQ动作权限] 与消息 ID 前缀，"
        "出站网关仍保持裸 CQ 安全拦截。"
    )
    author = "内置"
    builtin = True
    default_enabled = True
    hot_reload = True
    affects_sandbox_prompt = True
    capabilities = (
        "recall_self",
        "recall_others",
        "at_member",
        "at_all",
        "send_image_url",
        "send_image_local",
        "send_sticker_reply",
        "reply_quote",
        "poke",
        "emoji_react",
        "group_ban",
        "group_kick",
        "group_card",
        "group_essence",
        "group_join_approve",
    )
    related_views = ("permissions", "requests")

    token_impact = {
        "level": "very_high",
        "per_turn_tokens": 520,
        "per_session_tokens": 135,
        "background_llm": False,
        "extra_turns": False,
        "summary": "每轮必注约 350~450 tokens 动作说明 + 约 150~250 tokens [#消息ID] 前缀（且随会话累积）",
    }

    settings_schema = (
        {
            "key": "include_denied_list",
            "label": "在提示词中列出「本轮已禁止动作」清单",
            "type": "bool",
            "default": True,
            "description": "关闭后仅注入当前允许的动作指令，每轮可额外再省约 60~100 tokens",
        },
    )

    def estimate_token_usage(
        self, enabled: bool, settings: Dict[str, Any]
    ) -> Dict[str, Any]:
        base = super().estimate_token_usage(enabled, settings)
        if enabled and not settings.get("include_denied_list", True):
            base["per_turn_active"] = max(0, base["per_turn_max"] - 85)
        return base

    def filter_sandbox_gemini_lines(
        self, lines: List[str], enabled: bool, settings: Dict[str, Any]
    ) -> List[str]:
        if enabled:
            return lines
        return strip_markdown_items(
            lines,
            lambda s: "**QQ 动作权限" in s,
        )
