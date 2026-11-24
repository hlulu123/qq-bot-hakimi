"""Built-in plugin: Few-shot persona dialogue examples in sandbox GEMINI.md."""

from typing import Any, Dict, List

from bot_plugins import BotPlugin
from plugins._helpers import strip_markdown_section


class PersonaFewshotExamplesPlugin(BotPlugin):
    id = "persona_fewshot_examples"
    name = "人设少样本对话示例"
    version = "1.0.0"
    category = "enhancement"
    order = 30
    description = (
        "在群聊与普通私聊的沙盒指令（chat-sandbox/GEMINI.md）中保留「## 八、示例」26 组语气示例。"
        "关闭后自动从沙盒指令中剥离示例段（不会修改 workspace/GEMINI.md 原稿），新会话即刻生效。"
    )
    author = "内置"
    builtin = True
    default_enabled = True
    hot_reload = True
    affects_sandbox_prompt = True
    related_views = ("persona",)

    token_impact = {
        "level": "medium",
        "per_turn_tokens": 0,
        "per_session_tokens": 740,
        "background_llm": False,
        "extra_turns": False,
        "summary": "约 1,117 字（~600~876 tokens）常驻新会话系统指令，关闭后自动从沙盒剥离",
    }

    def filter_sandbox_gemini_lines(
        self, lines: List[str], enabled: bool, settings: Dict[str, Any]
    ) -> List[str]:
        if enabled:
            return lines
        return strip_markdown_section(lines, "## 八、示例")
