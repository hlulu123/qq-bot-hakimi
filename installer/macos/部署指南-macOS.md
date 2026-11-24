# QQ 机器人「哈基米」macOS 详细部署指南

本指南适用于 **macOS 12 及以上版本**，全面支持 **Apple Silicon（M1 / M2 / M3 / M4）** 与 **Intel Mac**（内置通用二进制 `cc-connect`）。

---

## 一、部署前准备

1. **一个机器人 QQ 小号**（强烈建议用小号，不要用日常主力大号，并确保手机上已登录该小号用于首次扫码）。
2. **你自己的管理员 QQ 号**（仅管理员私聊才拥有本地工作目录与高级命令权限）。
3. **一个可用的 Google 账号**（用于登录 Antigravity CLI `agy`）。
4. **可访问 Google 与 GitHub 的网络环境**（若使用代理软件，建议在安装与运行时开启系统代理或 TUN 模式）。

---

## 二、复制文件夹并放行 macOS 安全限制

### 1. 复制到本机可写目录

- 如果你是下载的 `.dmg` 镜像：打开 DMG 后，将整个机器人文件夹拖拽到 **桌面**（如 `~/Desktop/QQ机器人-哈基米`）或个人目录（如 `~/QQ机器人-哈基米`），然后再退出 DMG 镜像。
- 如果你是下载的 `.zip` 压缩包：双击解压后放到固定目录即可。

> 若你不小心直接在 DMG 里双击了安装脚本，脚本会自动检测只读目录，并一键帮你复制到桌面！

### 2. 处理 macOS“无法验证开发者”（Gatekeeper）

首次运行从网络下载的 `.command` 脚本时，macOS 会拦截提示“无法打开，因为无法验证开发者”。可通过以下任一方式永久放行本目录：

- **方式 A（推荐，一次性彻底放行）**：
  打开系统自带的「终端（Terminal）」，输入 `xattr -dr com.apple.quarantine `（注意末尾有一个空格），把机器人文件夹拖进终端窗口，按回车执行。
- **方式 B（图形界面右键打开）**：
  右键点击 **`1-一键安装与配置.command`** → 选择「打开」→ 点击「打开」。
  - 在 macOS Sequoia (15+) 上若首次仍被拦截：请前往 **系统设置 → 隐私与安全性**，滑到底部点击 **「仍要打开」**，之后再双击一次即可。

---

## 三、一键安装与初始化（全流程说明）

双击 **`1-一键安装与配置.command`**，脚本会分 7 步带你自动完成全部配置：

### 步骤 1：自动修复权限与清理隔离标记
脚本会自动对当前目录执行 `xattr -dr com.apple.quarantine` 并为所有 `.command`、`.sh` 及 `.tools/cc-connect` 补齐可执行权限。

### 步骤 2：检测 Python 3.9+ 并初始化 `.venv`
- 自动检测系统已安装的 Python 3.9+（依次查找 Homebrew Python、系统 Python）。
- 如果未安装，检测到 Homebrew 时可直接一键 `brew install python@3.12`，或按提示从 [python.org](https://www.python.org/downloads/macos/) 安装。
- 在项目目录自动创建独立的 `.venv` 虚拟环境，并尝试安装可选库 `Pillow`（用于让机器人看懂 QQ 动图 / GIF 表情包；默认源失败时会自动切换清华镜像源，离线时自动跳过不影响主功能）。

### 步骤 3：安装 Antigravity CLI (`agy`) 并登录 Google 账号
- 若未安装 `agy`，脚本会询问是否执行官方一键安装命令：
  ```bash
  curl -fsSL https://antigravity.google/cli/install.sh | bash
  ```
  默认安装至 `~/.local/bin/agy`（如果你此前用 Homebrew 安装在 `/opt/homebrew/bin/agy`，脚本也会自动识别）。
- 若未检测到 `~/.gemini/antigravity-cli` 登录态，脚本会直接启动 `agy`：
  1. 浏览器会自动弹出 Google 账号授权登录页面，点击同意授权。
  2. 回到终端看到登录成功提示后，输入 `/exit` 或按 `Ctrl+C` 即可无缝回到安装向导。

### 步骤 4：安装并启动 OrbStack（推荐的轻量级 Docker）
macOS 下运行 NapCat 容器强烈推荐使用 **OrbStack**（比 Docker Desktop 更省内存、启动只要 2 秒，同时兼容 M 系列和 Intel 芯片）：
- 如果装了 Homebrew，脚本可一键执行 `brew install --cask orbstack`。
- 未装 Homebrew 时，脚本可直接帮你打开官网 [https://orbstack.dev](https://orbstack.dev) 下载 `.dmg` 安装，安装后双击打开 OrbStack 等待右上角菜单栏小图标就绪即可。
- 如果你已经安装了 Docker Desktop，同样完全兼容，打开 Docker Desktop 即可。

### 步骤 5：交互式配置向导（`setup_wizard.py`）
按屏幕提示输入：
- **机器人 QQ 号**（准备给哈基米登录的 QQ 小号）
- **管理员 QQ 号**（你的 QQ 号，多个用英文逗号隔开）
- **机器人昵称 / 点名词**（可直接回车沿用默认「哈基米」）
- 向导会自动生成强随机 `NAPCAT_TOKEN`、`WEBUI_TOKEN` 和管理 API Token，并自动写入并同步 `bot.env`、`config.toml`、`docker-compose.napcat.yml` 及 `napcat/config/` 下的配置文件。

### 步骤 6：启动 NapCat 容器并扫码登录 QQ
- 确认后脚本会执行 `docker compose -f docker-compose.napcat.yml up -d` 拉起容器（首次拉取镜像约 1~3 分钟）。
- 容器就绪后，终端会直接打印 **NapCat WebUI Token**，并自动打开浏览器访问：
  ```text
  http://127.0.0.1:6099/webui/
  ```
- 在网页里粘贴 Token 登录，切到「QQ 登录」页面，用 **机器人 QQ 小号的手机 QQ 扫描二维码**。
- 手机确认登录、网页显示已在线后，回到终端按回车继续。

> 登录后，QQ 登录态会自动持久化保存在 `./napcat/QQ` 目录，且 `docker-compose.napcat.yml` 已配置 `NAPCAT_QUICK_ACCOUNT`，今后重启电脑或重建容器都会自动快速登录，无需反复扫码。

### 步骤 7：一键启动机器人与本地 Web 控制台
按回车确认后，脚本会立刻在后台拉起 `ws-guard`、`cc-connect` 与本地 Web 控制台，并自动打开：
```text
http://127.0.0.1:8765
```

此时，用你的 **管理员 QQ 私聊机器人** 发一句“在吗”，收到回复即代表部署大功告成！

---

## 四、日常使用与全部快捷脚本

| 双击文件 / 命令 | 说明 |
| :--- | :--- |
| `1-一键安装与配置.command` | 首次安装、补装依赖或重新运行配置向导 |
| `2-启动机器人(后台).command` | 自动确保 NapCat 运行 → 后台启动机器人 → 打开 Web 控制台 |
| `3-打开Web控制台.command` | 直接打开 `http://127.0.0.1:8765` 本地管理面板 |
| `4-重启ws-guard.command` | 仅重启 ws-guard（改长回复合并转发/过滤参数时用，不中断 QQ 会话） |
| `5-停止全部服务.command` | 停止机器人与 Web 控制台（可选是否连同 NapCat 一起停，默认保留 QQ 在线） |
| `6-环境体检(Doctor).command` | 一键诊断全链路状态、配置一致性与端口连通性 |
| `./start.sh` | 终端前台运行（实时查看日志，`Ctrl+C` 退出） |
| `./stop.sh` | 终端停止全部机器人服务 |
| `./webui.sh [start\|stop\|restart\|status\|fg\|open]` | 终端单独管理 Web 控制台 |

---

## 五、本地 Web 控制台使用简介

打开 `http://127.0.0.1:8765`（仅本机可访问），你可以直接在网页里完成几乎全部日常运维（后台所有启停、配置一致性校验与 10MB×3 日志自动轮转均由 `bot_runner.py` 统一管理）：

- **实时拓扑、QQ 连接与统计**：一眼看清 NapCat / ws-guard / cc-connect 是否在线、查看 QQ 连接/扫码状态，以及近 24 小时回复量与 Token 用量统计。
- **会话、入群监控与历史翻查**：查看群聊/私聊消息与机器人的回复记录、监控与审批入群申请；内置 `chat_history.py` 本地归档支持机器人自主翻查前文历史。
- **人设编辑器**：可视化修改 `workspace/GEMINI.md`，支持保存前 diff 对比与历史版本回滚。
- **SQLite v2 长期记忆管理**：由 `memory_engine/` 驱动（`data/memory.db`），分别管理群聊个人记忆、私聊记忆与群共享记忆（群聊与私聊严格隔离，管理员私聊支持跨群回溯各群近期动态，夜间 Sleep 自动整理）。
- **插件管理与 QQ 动作权限（热生效）**：`bot_plugins.py` 一键开关 15 项可选功能插件与 Token 省流预设；`bot_permissions.py` + `qq_actions.py` 按全局/身份/单群精细管控 20 项 QQ 动作与群管权限。
- **配置与日志**：在线修改 `bot.env` 参数（含群白名单、记忆与历史参数）、测试群聊复读判定、实时追踪服务日志。

---

## 六、常见问题与故障排查

### 1. 千万不要在 Claude 桌面应用内置终端里启动机器人
Claude 桌面应用的内置终端运行在沙箱网络中，会导致 `cc-connect` 无法访问 `127.0.0.1` 并反复报 `connect: bad file descriptor`。请始终使用 **Finder 双击 `.command`** 或系统自带的 **终端（Terminal.app）/ iTerm2**。

### 2. 机器人显示已启动，但发消息完全没反应
1. 先双击 **`6-环境体检(Doctor).command`**，按红/黄色提示逐项排查。
2. 打开 `http://127.0.0.1:6099/webui/` 检查机器人 QQ 是否在线，以及「网络配置」里的 `cc-connect-http` (3000) 和 `cc-connect-ws` (3001) 是否为开启状态。
3. 在群聊里未点名机器人时，哈基米默认安静潜水（`NO_REPLY`）；测试时请直接 **@机器人**、喊一声 **“哈基米”**，或用 **管理员 QQ 私聊**测试。
4. 查看 `data/cc-connect.log` 和 `data/ws-guard.log`（或直接在 WebUI「日志」页查看）。

### 3. `agy` 安装失败或调用超时
- 确认当前网络可以正常访问 Google 服务。
- 如果你使用代理软件但只开了 HTTP 代理没开 TUN 模式，可以在启动前于终端设置代理或开启代理软件的「增强模式 / TUN 模式」。
- 若终端提示 `command not found: agy`，请确认 `~/.local/bin` 已在 PATH 中，或在 `bot.env` 中显式填写 `AGY_BIN=/Users/你的用户名/.local/bin/agy`（留空时机器人也会自动搜索该路径）。

### 4. OrbStack 拉取 NapCat 镜像很慢
- 首次运行 `docker compose -f docker-compose.napcat.yml up -d` 需要从 `ghcr.io` 拉取镜像，请保持网络通畅（或在 OrbStack 设置中检查代理是否自动跟随系统代理）。

### 5. 电脑重启后如何快速恢复？
1. 确保 OrbStack（或 Docker Desktop）已启动（NapCat 容器配置了 `restart: unless-stopped`，会自动启动并快速重连 QQ）。
2. 双击 **`2-启动机器人(后台).command`** 即可恢复全部服务。
