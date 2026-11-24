# QQ 机器人「哈基米」插件开发规范

所有**非必要功能**（当前内置可选能力 + 后续新增扩展功能）都统一以插件形式放在 `plugins/` 目录下，由 [bot_plugins.py](../bot_plugins.py) 自动发现、热加载（≤0.8 秒生效）并在本地 WebUI「插件管理」中一键开关，关闭后实现**零提示词注入 / 零后台模型调用 / 零多余轮次**。

## 快速新增一个插件

1. 复制 `plugins/_template.py.example` 为 `plugins/<你的插件名>.py`（文件名不能以 `_` 开头）。
2. 继承 `bot_plugins.BotPlugin`，填写元数据（`id`、`name`、`description`、`token_impact`、`settings_schema`）。
3. 按需实现以下生命周期钩子（**仅插件启用时才会被调用**，且有异常隔离，不会拖垮主进程）：
   - `get_system_prompt_fragment(settings)`：返回系统级指令片段，会自动编译进 `chat-sandbox/GEMINI.md`（不修改 `workspace/GEMINI.md`）。
   - `on_prompt_sections(ctx, settings)`：在 `agy-acp-wrapper.py` 每轮发给模型前按需注入提示词（建议做关键词/场景按需触发以进一步省 Token）。
   - `on_response_postprocess(text, ctx, settings)`：在回复发往 QQ 前做轻量后处理。
   - `on_ws_inbound(event, ctx, settings)` / `on_ws_outbound(payload, ctx, settings)`：在 `onebot-ws-guard.py` 网关层拦截或改写 OneBot 入站/出站事件。
4. 在 WebUI「插件管理」点击「重新扫描插件目录」即可看到新插件开关与可视化参数表单。

## 工程约束

- 仅使用 **Python 3.9+ 标准库**，不引入第三方依赖。
- 不要在插件源码里硬编码 QQ 号、Token 或本机路径（运行时开关与参数持久化在 `data/bot_plugins.json`）。
- 若插件会触发额外 LLM 轮次或注入大量上下文，请如实填写 `token_impact`，方便在 WebUI 中直观评估 Token 消耗。
