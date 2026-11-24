"""Built-in plugin: Multimodal image & sticker vision understanding."""

from typing import Any, Dict, List

from bot_plugins import BotPlugin
from plugins._helpers import strip_markdown_items


class MultimodalVisionPlugin(BotPlugin):
    id = "multimodal_vision"
    name = "图片与表情包视觉理解"
    version = "1.0.0"
    category = "capabilities"
    order = 10
    description = (
        "将当前消息附带的图片、引用图片、近期上下文图片、动图 3 帧拼图以及 QQ 商城表情包"
        "编码为多模态视觉块传给模型。支持独立开关「上下文自动拉图」与「商城表情转图」以精细控耗。"
    )
    author = "内置"
    builtin = True
    default_enabled = True
    hot_reload = True
    affects_sandbox_prompt = True
    related_views = ("config",)

    token_impact = {
        "level": "high",
        "per_turn_tokens": 350,
        "per_session_tokens": 85,
        "background_llm": False,
        "extra_turns": False,
        "summary": "单张图片约 258~1,000+ 视觉 Token，单轮默认最多 4 张（含引用/近期/表情包）",
    }

    settings_schema = (
        {
            "key": "direct_images",
            "label": "理解当前消息直接附带的图片",
            "type": "bool",
            "default": True,
            "description": "关闭后连用户直接发来的图片也不会编码上传",
        },
        {
            "key": "context_images",
            "label": "自动拉取引用消息与近期上下文图片",
            "type": "bool",
            "default": True,
            "description": "关闭后仅看当前消息直发图，不再自动把群友刚发过的图或被引用图塞进视觉上下文",
        },
        {
            "key": "sticker_vision",
            "label": "将 QQ 商城大表情（mface）转为图片识图",
            "type": "bool",
            "default": True,
            "description": "关闭后跳过商城表情下载与识图，大幅减少纯表情包水群的视觉 Token 消耗",
        },
        {
            "key": "max_images_per_turn",
            "label": "单轮最多附带图片张数",
            "type": "int",
            "default": 4,
            "min": 1,
            "max": 4,
            "description": "热生效限制单轮最多编码几张图（调为 1~2 可明显节省多图场景 Token）",
        },
    )

    def estimate_token_usage(
        self, enabled: bool, settings: Dict[str, Any]
    ) -> Dict[str, Any]:
        base = super().estimate_token_usage(enabled, settings)
        if not enabled:
            return base
        direct = bool(settings.get("direct_images", True))
        ctx_img = bool(settings.get("context_images", True))
        sticker = bool(settings.get("sticker_vision", True))
        max_n = max(1, min(4, int(settings.get("max_images_per_turn") or 4)))
        if not direct and not ctx_img:
            base["per_turn_active"] = 0
            return base
        factor = 0.0
        if direct:
            factor += 0.45
        if ctx_img:
            factor += 0.35
        if sticker:
            factor += 0.20
        factor *= (0.45 + 0.55 * (float(max_n) / 4.0))
        base["per_turn_active"] = int(round(base["per_turn_max"] * min(1.0, factor)))
        return base

    def filter_sandbox_gemini_lines(
        self, lines: List[str], enabled: bool, settings: Dict[str, Any]
    ) -> List[str]:
        if enabled:
            return lines
        return strip_markdown_items(
            lines,
            lambda s: (
                "看图实事求是" in s
                or "发了张美颜开得很重的自拍" in s
            ),
        )
