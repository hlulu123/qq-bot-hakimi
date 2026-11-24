# QQ 机器人「哈基米」一键部署包

> 基于 **NapCatQQ（OneBot v11）+ cc-connect（本地优化版）+ Antigravity CLI（ACP）** 的高拟真 QQ 群聊与个人助手机器人，自带本地可视化 Web 控制台与一键懒人启动器，支持 **macOS（Apple Silicon / Intel）** 与 **Windows 10/11**。

---

## 核心特性

- **高拟真自然聊天**：内置「哈基米」人设，默认安静潜水、被点名及时响应、自然顺着接梗、口语化断句与口癖去重，拒绝客服腔和括号戏。
- **SQLite v2 分层长期记忆 & 历史自主翻查**：由 `memory_engine/` 驱动（`data/memory.db`），支持 L0 印象 / L1 档案 / L2 事实与事件三通道混合召回、群聊共享记忆、夜间 Sleep 自动整理，**群聊与私聊记忆严格隔离**（管理员私聊可按需跨群回溯各群近期动态）；配合 `chat_history.py` 本地归档与 NapCat 分页回溯，遗忘前文时可自主翻查历史聊天记录。
- **模块化插件与细粒度 QQ 权限（热生效）**：`bot_plugins.py` 管理 15 项可选功能插件与 Token 省流预设；`bot_permissions.py` + `qq_actions.py` 提供 20 项 QQ 动作权限（撤回、@人、发图、戳一戳、贴表情、群管、入群审批、历史翻查等）按身份与按群精细控制，并在网关层硬拦截越权出站动作。
- **智能复读与长消息防刷屏**：识别群友连续复读（含表情包与图片）并由模型决定是否原样跟风；长回复自动转为“合并转发聊天记录”卡片发出。
- **严格的双层沙箱安全模型**：管理员私聊可读写工作目录、写代码、跑命令；群聊与普通人私聊全程隔离在 `chat-sandbox/`，仅允许只读联网搜索，越权即刻拦截。
- **本地可视化 Web 控制台**：内置纯 Python WebUI（`http://127.0.0.1:8765`），一站式管理架构拓扑、QQ 连接与扫码状态、24 小时回复与 Token 统计、QQ 会话、入群申请监控与审批、人设编辑与版本回滚、长期记忆、插件开关、QQ 动作权限、行为配置及实时日志流。
- **全本地回环监听**：NapCat、ws-guard、cc-connect 管理 API、Web 控制台全部只监听 `127.0.0.1`，同一 Wi-Fi / 局域网内其他设备无法访问。

---

## 系统架构

```text
QQ ⇄ NapCat（Docker 容器或 Windows 原生版；仅绑 127.0.0.1: 3000 HTTP / 3001 WS / 6099 WebUI）
        ⇅ OneBot v11 WebSocket
     onebot-ws-guard.py（127.0.0.1:3002）
        ├─ 单群防并发死锁、群白名单/潜水过滤、过滤刷屏提示与 Q群管家
        ├─ 连续复读检测（文字 / 表情包 / 图片）与出站 CQ 权限硬拦截
        └─ 长回复自动转合并转发卡片
        ⇅
     cc-connect（内置已打补丁的 .tools/cc-connect；管理 API 仅监听 127.0.0.1:9820）
        ⇅ ACP（每个会话独立进程，空闲自动回收）
     agy-acp-wrapper.py ──▶ Antigravity CLI (`agy acp`)
        ├─ user_memory.py + memory_engine/ ──▶ SQLite v2 分层长期记忆 & 夜间 Sleep & 管理员跨群近况回溯
        ├─ chat_history.py ──▶ 本地聊天归档 & 自主历史翻查
        ├─ bot_permissions.py + qq_actions.py ──▶ 细粒度 QQ 动作权限与执行器
        └─ bot_plugins.py + plugins/ ──▶ 模块化插件与 Token 省流热重载

Web 控制台：webui/（仅监听 127.0.0.1:8765）
统一管理入口：bot_runner.py（跨平台进程守护 / 启停锁 / 配置校验 / 日志轮转 / 体检）
```

---

## 一键安装与启动

### macOS（Apple Silicon / Intel）

> 详细步骤请参阅同目录的 **[部署指南-macOS.md](./部署指南-macOS.md)** 与 **[首次运行必读.txt](./首次运行必读.txt)**。

1. **拖出镜像**：如果你是从 `.dmg` 打开的，请先把整个文件夹复制到桌面（例如 `~/Desktop/QQ机器人-哈基米`），不要在 DMG 里直接运行。
2. **首次放行并安装**：右键点击 **`1-一键安装与配置.command`** → 选择「打开」（或在终端执行 `xattr -dr com.apple.quarantine <本文件夹路径>`）。脚本会自动完成：
   - 创建 `.venv` 虚拟环境并安装可选 `Pillow`（支持动图表情包识别）
   - 检测/一键安装 Antigravity CLI (`agy`) 并引导完成 Google 账号登录
   - 检测/引导安装 OrbStack（轻量级 Docker）
   - 运行交互式配置向导，自动生成 `bot.env`、`config.toml` 和随机密钥
   - 启动 NapCat 并自动打开 `http://127.0.0.1:6099/webui/` 供手机 QQ 扫码登录
   - 一键后台启动机器人并打开本地 Web 控制台
3. **日常使用**：以后只需双击 **`2-启动机器人(后台).command`** 启动，双击 **`3-打开Web控制台.command`** 查看状态。

### Windows 10 / 11

> 详细步骤请参阅同目录的 **[部署指南-Windows.md](./部署指南-Windows.md)** 与 **[首次运行必读.txt](./首次运行必读.txt)**。

1. **完整解压**：右键 ZIP 压缩包 →「全部解压缩」到本地目录（例如 `D:\QQ机器人-哈基米`），**不要在压缩包预览界面里直接双击脚本**。
2. **一键安装**：双击 **`1-一键安装与配置.bat`**（若弹出 Windows SmartScreen 蓝色提示，点「更多信息」→「仍要运行」）。脚本会自动完成：
   - 检测并一键安装 Python 3.12（自动跳过 Microsoft Store 假壳，免重启刷新 PATH）
   - 创建 `.venv` 并安装可选 `Pillow`（支持动图/GIF 表情包）
   - 检测/一键安装 Antigravity CLI (`agy`) 并引导 Google 登录
   - 支持 **Docker Desktop 模式** 或 **Windows 原生 NapCat（免 Docker）模式** 任选其一
   - 运行交互式配置向导，并自动引导 QQ 扫码与一键启动
3. **日常使用**：以后只需双击 **`2-启动机器人(后台).bat`** 启动，双击 **`3-打开Web控制台.bat`** 查看状态。

---

## 日常管理速查

### 懒人双击启动器

| 功能 | macOS 双击文件 | Windows 双击文件 |
| :--- | :--- | :--- |
| 首次一键安装与向导配置 | `1-一键安装与配置.command` | `1-一键安装与配置.bat` |
| 后台启动机器人并打开 WebUI | `2-启动机器人(后台).command` | `2-启动机器人(后台).bat` |
| 打开本地 Web 控制台 | `3-打开Web控制台.command` | `3-打开Web控制台.bat` |
| 仅重启 ws-guard（不掉线） | `4-重启ws-guard.command` | `4-重启ws-guard.bat` |
| 停止全部机器人服务 | `5-停止全部服务.command` | `5-停止全部服务.bat` |
| 一键环境体检（Doctor） | `6-环境体检(Doctor).command` | `6-环境体检(Doctor).bat` |
| 前台运行查看实时日志 | `./start.sh` | `前台调试启动.bat` |

### 命令行管理（CLI 高级用户）

统一通过 `bot_runner.py` 管理（建议优先使用项目 `.venv` 内的 Python）：

```bash
# 查看全链路运行状态
python3 bot_runner.py status

# 后台启动 / 前台启动 / 重启 / 停止全部
python3 bot_runner.py start-bg
python3 bot_runner.py start
python3 bot_runner.py restart
python3 bot_runner.py stop-all

# 仅重启 ws-guard（改长消息转发或过滤规则后无需全量重启）
python3 bot_runner.py restart-ws-guard

# Web 控制台管理
python3 bot_runner.py webui start|stop|restart|status|fg|open

# NapCat Docker 容器管理
python3 bot_runner.py napcat up|down|status|logs

# 全链路环境体检 & 同步沙箱人设
python3 bot_runner.py doctor
python3 bot_runner.py sync-sandbox
```

macOS 下同时保留了 `./start.sh`、`./stop.sh`、`./webui.sh` 快捷命令。

---

## 本地 Web 控制台（`http://127.0.0.1:8765`）

启动后在浏览器访问 `http://127.0.0.1:8765` 即可可视化管理：

- **运行总览与 QQ 连接**：实时展示 NapCat、ws-guard、cc-connect、WebUI 连接状态、QQ 扫码/在线状态、24 小时回复与 Token 用量统计。
- **QQ 会话、入群监控与日志**：浏览群聊/私聊记录、审批/监控入群申请，实时追踪 `cc-connect.log` 与 `ws-guard.log`（支持 10MB 自动轮转）。
- **人设编辑器**：在线修改 `workspace/GEMINI.md`，支持保存前 diff 对比与历史版本回滚。
- **SQLite v2 长期记忆管理**：分群聊 / 私聊 / 群共享记忆视图查看、检索与编辑记忆，查看夜间 Sleep 维护记录。
- **插件与权限管理（热生效）**：一键切换 15 项可选插件与 Token 省流预设；按全局 / 身份 / 群精细管理 20 项 QQ 动作权限。
- **行为配置与测试**：可视化修改 `bot.env`（自动校验并提示生效方式），内置复读判定测试。

---

## 安全与权限模型

| 对话来源 | 工作目录 | 本地文件 / 命令行权限 | 权限确认方式 |
| :--- | :--- | :--- | :--- |
| **管理员私聊** | `workspace/` | 允许 | 默认 Turbo 自动放行（可在 `bot.env` 设 `ADMIN_DM_TURBO=0` 改为逐条私聊确认） |
| **群聊（含管理员在群内）** | `chat-sandbox/` | **完全禁止**（碰本地工具立刻取消本轮） | 仅静默放行只读联网搜索，其余全部拒绝 |
| **普通用户私聊** | `chat-sandbox/` | **完全禁止**（同上） | 同上 |

- **管理员想让机器人干本地活（写代码、改文件、跑脚本）时，请务必私聊机器人**，在群里发命令会被安全策略统一拦截。
- 所有监听端口（`3000` / `3001` / `3002` / `6099` / `8765` / `9820`）默认仅绑定 `127.0.0.1`。
- `bot.env`、`config.toml` 与 `napcat/config/` 包含你的账号与随机 Token，**切勿上传到公开仓库或发给他人**。

---

## 常见问题（FAQ）

1. **怎么排查“机器人启动了但不回消息”？**
   - 先双击 **`6-环境体检(Doctor)`**，逐项检查 Python、`agy` 登录态、NapCat 端口连通性、`bot.env` 与 `config.toml` 一致性。
   - 确认 NapCat WebUI（`http://127.0.0.1:6099/webui/`）里机器人 QQ 处于**在线状态**，且 OneBot11 HTTP (3000) / WS (3001) 服务已启用。
   - 用管理员 QQ **私聊**机器人发一句“在吗”，检查是否正常响应（群聊未点名时默认潜水）。
2. **macOS 双击 `.command` 提示“无法验证开发者”？**
   - 右键文件选「打开」，或在终端运行 `xattr -dr com.apple.quarantine <本文件夹路径>` 一次性清除隔离属性。
3. **macOS 用户提醒：请勿在 Claude 桌面应用内置终端里启动机器人**
   - Claude 桌面应用内置终端存在沙箱网络限制，会导致 `cc-connect` 连不上 `127.0.0.1`。请使用系统自带「终端 (Terminal.app)」或直接双击 `.command` 启动。
4. **Windows 上找不到 `agy` 或登录 Google 失败？**
   - 请确认本机网络可访问 Google；如果使用系统代理，请在启动终端前设置代理环境变量（如 `HTTPS_PROXY=http://127.0.0.1:7890`，按你的实际代理端口替换）或在代理软件中开启 TUN / 系统代理模式。
5. **如何修改机器人名字、人设或说话频率？**
   - 改人设：直接编辑 `workspace/GEMINI.md`（或通过 Web 控制台「人设」页面），详细设计说明见 [docs/CHARACTER_CARD.md](./docs/CHARACTER_CARD.md)。
   - 改名字/频率/复读/合并转发：在 Web 控制台「行为配置」页面或直接编辑 `bot.env` 后重启。
6. **需要重新配置 QQ 号或重置 Token？**
   - 随时重新运行 **`1-一键安装与配置`**（或命令行运行 `python setup_wizard.py`），向导会自动保留你的现有习惯并重新同步全部配置文件。

---

## 目录结构

| 路径 | 作用 |
| :--- | :--- |
| `bot_runner.py` | 跨平台进程守护、启停锁、日志轮转与 CLI 统一管理入口 |
| `setup_wizard.py` | 交互式首次安装与配置同步向导（重跑保留自定义 `config.toml` 项） |
| `bot.env` | 共享配置唯一来源（安装向导生成，请勿泄露） |
| `config.toml` | `cc-connect` 主配置（安装向导生成，请勿泄露） |
| `workspace/GEMINI.md` | 线上生效的机器人人设与行为规范 |
| `chat-sandbox/` | 群聊与普通用户私聊隔离目录（自动同步生成，勿手动放私密文件） |
| `agy-acp-wrapper.py` | ACP 包装层（权限隔离、思考强度调度、口语微调、记忆与历史注入） |
| `onebot-ws-guard.py` & `onebot_common.py` | OneBot WebSocket 守护代理与协议工具（防死锁、白名单、复读、长回复合并转发、出站拦截） |
| `repeat_chain.py` & `signal_filter.py` & `prompt_diet.py` | 连续复读检测、低信号过滤与提示词瘦身/增量上下文注入 |
| `memory_engine/` & `user_memory.py` | SQLite v2 分层长期记忆引擎（`data/memory.db`、三通道召回、Sleep 维护、管理员跨群近况回溯） |
| `chat_history.py` | 本地聊天记录归档与机器人自主历史翻查模块 |
| `group_requests.py` & `token_usage.py` | 入群申请监控审批引擎与 Token 用量统计模块 |
| `bot_permissions.py` & `qq_actions.py` | 细粒度 QQ 动作权限策略引擎（20 项能力）与 NapCat 动作执行器 |
| `bot_plugins.py` & `plugins/` | 模块化功能插件管理器、15 项内置插件与扩展模板 |
| `webui/` | 本地可视化 Web 控制台（`127.0.0.1:8765`） |
| `.tools/` | 内置已打补丁的 `cc-connect` 可执行文件 |
| `napcat/` | NapCat 配置与 QQ 登录态持久化目录 |
| `data/` | 运行日志、`memory.db`、会话归档与运行态 PID/锁文件 |
| `docs/CHARACTER_CARD.md` | 机器人角色设计理念与调教经验文档 |
