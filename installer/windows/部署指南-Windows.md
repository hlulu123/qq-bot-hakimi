# QQ 机器人「哈基米」Windows 10 / 11 详细部署指南

本指南适用于 **Windows 10 / 11（64 位）**，支持 **Docker Desktop 模式** 与 **Windows 原生 NapCat（免 Docker）模式** 两种运行方案。

---

## 一、部署前准备

1. **一个机器人 QQ 小号**（强烈建议用小号，不要用日常主力大号，手机上提前登录该小号用于首次扫码）。
2. **你自己的管理员 QQ 号**（只有管理员私聊才允许读写 `workspace/` 与执行命令）。
3. **一个可用的 Google 账号**（用于登录 Antigravity CLI `agy`）。
4. **可访问 Google 与 GitHub 的网络环境**（若使用代理软件，建议在安装与运行时开启系统代理或 TUN 模式）。

---

## 二、解压安装包与安全提示放行

### 1. 完整解压压缩包
右键下载好的 `.zip` 压缩包 → 选择 **「全部解压缩」**，解压到一个固定文件夹（例如 `D:\QQ机器人-哈基米` 或 `C:\Users\你的用户名\Desktop\QQ机器人-哈基米`）。

> ⚠️ **切勿在压缩包预览窗口内直接双击 `.bat` 文件！** 那样会导致脚本在临时目录中找不到完整依赖，也无法保存配置。

### 2. 处理 Windows SmartScreen 拦截
首次双击 `1-一键安装与配置.bat` 时，Windows Defender SmartScreen 可能弹出蓝色提示框“Windows 已保护你的电脑”：
1. 点击提示框里的 **「更多信息」**。
2. 点击右下角的 **「仍要运行」** 即可正常启动向导。

---

## 三、一键安装与配置（6 步全流程）

双击 **`1-一键安装与配置.bat`**，脚本会自动调用内置 PowerShell 安装向导，共 6 步：

### 步骤 `[1/6]`：自动检测并安装 Python 3.9+
- 自动查找可用的 Python 3.9+，并**自动排除 Microsoft Store 的假壳占位程序**（`WindowsApps\python.exe`）。
- 如果电脑尚未安装 Python：
  - 优先调用 Windows 内置 `winget` 一键静默安装 Python 3.12（用户级，无需管理员权限）。
  - 若 `winget` 不可用，自动从 python.org 下载官方 64 位安装包静默安装。
  - 安装后自动从注册表刷新当前窗口的 `PATH`，**无需重启电脑**。

### 步骤 `[2/6]`：创建 `.venv` 与安装可选图片组件（Pillow）
- 在项目目录下创建独立虚拟环境 `.venv`，避免污染系统 Python。
- 自动尝试安装可选库 `Pillow`（用于解析 QQ 动图 / GIF 表情包）；默认源超时时自动切换清华大学镜像源；离线环境会自动跳过，不影响机器人主功能。

### 步骤 `[3/6]`：安装 Antigravity CLI (`agy`) 并登录 Google 账号
- 若未安装 `agy`，脚本会提示一键执行官方安装命令：
  ```powershell
  irm https://antigravity.google/cli/install.ps1 | iex
  ```
  默认安装到 `%LOCALAPPDATA%\agy\bin\agy.exe` 并自动刷新 `PATH`。
- 若检测到尚未登录 Google，脚本会直接启动 `agy`：
  1. 浏览器会自动弹出 Google 账号授权登录页面，完成授权。
  2. 回到黑框窗口看到登录成功后，输入 `/exit` 或按 `Ctrl+C` 即可无缝继续安装！

### 步骤 `[4/6]` & `[5/6]`：选择 NapCat 运行方式并完成配置向导
在交互式配置向导（`setup_wizard.py`）中，依次输入：
- **机器人 QQ 号**（小号）
- **管理员 QQ 号**（你的 QQ 号，多个用英文逗号隔开）
- **NapCat 运行模式**（见下方「方案 A」与「方案 B」详解）
- **机器人昵称 / 点名词**（可直接回车保留默认「哈基米」）

向导会自动生成强随机密钥并同步写入 `bot.env`、`config.toml`、`docker-compose.napcat.yml` 与 `napcat\config\` 下的全部配置文件。

### 步骤 `[6/6]`：启动 NapCat、QQ 扫码并启动机器人
根据你选择的 NapCat 方案，按下面对应说明完成扫码后，回车即可一键在后台启动机器人并打开本地 Web 控制台（`http://127.0.0.1:8765`）！

---

## 四、两种 NapCat 运行方案详解

### 方案 A：Docker Desktop 模式（容器隔离）

适合已经装好或打算使用 Docker Desktop 的用户：
1. 确保已安装并启动 [Docker Desktop for Windows](https://www.docker.com/products/docker-desktop/)（需开启 WSL2 后端）。
2. 在配置向导中选择 **Docker 模式**。
3. 安装脚本第 `[6/6]` 步会自动询问是否启动 NapCat 容器（`docker compose -f docker-compose.napcat.yml up -d`）。
4. 容器启动后，窗口会自动打印 **NapCat WebUI Token** 并打开 `http://127.0.0.1:6099/webui/`。
5. 粘贴 Token 登录，用 **机器人 QQ 小号的手机 QQ 扫码登录**，显示在线后回窗口按回车即可。

---

### 方案 B：Windows 原生 NapCat 模式（零 Docker，强烈推荐轻量化用户！）

**不需要安装 WSL2、不需要 Docker Desktop、内存占用极低！**

1. **下载原生 NapCat**：
   前往 NapCat 官方 GitHub Releases 页面：
   ```text
   https://github.com/NapNeko/NapCatQQ/releases
   ```
   下载 Windows 整合包（常见命名为 **`NapCat.Shell.Windows.OneKey.zip`** 或 **`NapCat.Shell.zip`**，推荐带 OneKey 一键包，内置所需 QQ 运行环境），解压到独立文件夹（例如 `D:\NapCat`）。
2. **选择原生模式生成安全配置**：
   在配置向导中选择 **Windows 原生 NapCat 模式**。向导会自动在本项目 `napcat\config\` 下生成：
   - `onebot11_<机器人QQ号>.json`（HTTP 3000 + WS 3001，**仅监听 `127.0.0.1`**，绝不暴露给局域网！）
   - `webui.json`（端口 6099，自动填入随机 Token）
3. **同步配置到 NapCat**：
   - **自动复制（推荐）**：在安装脚本第 `[6/6]` 步提示“是否现在自动复制配置文件过去？”时输入 `y`，直接把你解压好的 NapCat 文件夹拖进窗口按回车，脚本会自动备份原文件并复制 `onebot11*.json` 和 `webui.json` 到 NapCat 的 `config\` 目录！
   - **手动复制**：将本项目 `napcat\config\` 下的 `onebot11*.json` 和 `webui.json` 复制粘贴到你的 NapCat 目录下的 `config\` 目录中。
4. **启动原生 NapCat 并扫码登录**：
   - 双击 NapCat 目录里的启动程序（如 `launcher.bat` / `NapCatWinBootMain.exe`）。
   - 用浏览器打开 `http://127.0.0.1:6099/webui/`，输入安装脚本里显示的 Token（或查看 `webui.json` 中的 `token`），用 **机器人 QQ 小号** 手机扫码登录。
   - 登录后在 WebUI「网络配置」页面确认 `cc-connect-http`（3000）和 `cc-connect-ws`（3001）均已处于启用状态。
5. **启动机器人**：
   回到安装脚本按回车（或双击 **`2-启动机器人(后台).bat`**）即可连接成功！

> 日常开机使用原生 NapCat 时，只需先启动 NapCat 登录 QQ，再双击 **`2-启动机器人(后台).bat`** 即可。

---

## 五、日常使用与全部 `.bat` 脚本说明

| 双击文件 | 作用 |
| :--- | :--- |
| `1-一键安装与配置.bat` | 首次一键安装、补装依赖或重新运行配置向导 |
| `2-启动机器人(后台).bat` | 后台静默启动机器人并自动打开 Web 控制台 |
| `3-打开Web控制台.bat` | 打开本地可视化控制台（`http://127.0.0.1:8765`） |
| `4-重启ws-guard.bat` | 仅重启 ws-guard 代理（不掉线，十几秒内自动重连） |
| `5-停止全部服务.bat` | 停止机器人全部后台进程与 Web 控制台 |
| `6-环境体检(Doctor).bat` | 一键体检：检查 Python、`agy`、NapCat 端口、配置一致性 |
| `前台调试启动.bat` | 在当前黑框前台运行并实时滚动输出日志（`Ctrl+C` 停止，排查问题首选） |

---

## 六、本地 Web 控制台（`http://127.0.0.1:8765`）

无需改代码即可在浏览器里管理机器人（仅本机 `127.0.0.1` 可访问；后台进程守护、启停锁、配置校验与 10MB×3 日志自动轮转由 `bot_runner.py` 统一管理）：
- **实时拓扑、QQ 连接与 24h 统计**：查看 NapCat / ws-guard / cc-connect / WebUI 健康状态、QQ 连接/扫码状态、发言量与 Token 用量统计。
- **QQ 会话、入群监控与历史翻查**：回看群聊/私聊记录、监控与审批入群申请并实时追踪日志；内置 `chat_history.py` 本地归档支持机器人自主翻查前文历史。
- **人设编辑器**：修改 `workspace/GEMINI.md`，支持保存前对比与历史版本回滚。
- **SQLite v2 长期记忆**：由 `memory_engine/` 驱动（`data\memory.db`），分群聊 / 私聊 / 群共享记忆隔离管理（管理员私聊支持跨群回溯各群近期动态，夜间 Sleep 自动整理）。
- **插件管理与 QQ 动作权限（热生效）**：`bot_plugins.py` 一键开关 15 项可选插件与 Token 省流预设；`bot_permissions.py` + `qq_actions.py` 按全局/身份/单群精细管控 20 项 QQ 动作与群管权限。
- **参数配置**：可视化修改 `bot.env` 中的点名词、群白名单、冷却频率、复读、记忆/历史与长消息合并转发设置。

---

## 七、防火墙与杀毒软件说明

- **Windows 防火墙**：如果 Windows 弹出“允许 Python / cc-connect 访问网络”的防火墙提示，允许访问「专用网络」即可；事实上本机器人所有服务端口（`3000`、`3001`、`3002`、`6099`、`8765`、`9820`）**全部仅绑定本机回环地址 `127.0.0.1`**，不会监听局域网或公网，非常安全。
- **杀毒软件误报**：`.tools\cc-connect.exe` 是由 Go 编译的开源项目本地补丁版可执行文件，少数国产杀软（如 360、火绒、电脑管家）可能出现误报隔离。若被拦截，请恢复文件并将整个机器人文件夹加入信任区 / 白名单。

---

## 八、常见问题与排查（FAQ）

### 1. 为什么在命令行打 `python` 会弹出微软应用商店（Microsoft Store）？
Windows 10/11 默认自带一个假的 `WindowsApps\python.exe` 占位别名。本安装包内的 `install.ps1` 与 `find_python.bat` 已专门编写代码**自动跳过它**；如想彻底关闭，可在「系统设置 → 应用 → 高级应用设置 → 应用执行别名」中关闭 `python.exe` 与 `python3.exe`。

### 2. 双击 `1-一键安装与配置.bat` 提示 PowerShell 执行策略受限或闪退？
- `1-一键安装与配置.bat` 已显式附带 `-ExecutionPolicy Bypass` 参数绕过用户级策略限制。
- 如果公司域控电脑通过组策略（GPO）强制锁死了 PowerShell 脚本执行，你可以直接在普通终端里手动运行：`python setup_wizard.py`，再双击 `2-启动机器人(后台).bat`。

### 3. `agy` 安装或调用时连不上 Google？
- 请确认代理软件已开启「系统代理」或「TUN 虚拟网卡模式」。
- 若代理只开了本地 HTTP 端口（例如 `127.0.0.1:7890`），可在系统环境变量或启动终端前设置：
  ```powershell
  $env:HTTP_PROXY="http://127.0.0.1:7890"
  $env:HTTPS_PROXY="http://127.0.0.1:7890"
  ```

### 4. 机器人启动成功了，但 QQ 发消息没反应？
1. 先双击 **`6-环境体检(Doctor).bat`**，查看哪一项报错（最常见是 NapCat 没启动或 QQ 掉线）。
2. 双击 **`前台调试启动.bat`** 观察实时日志输出，一眼就能看到报错信息。
3. 确认用 **管理员 QQ 私聊** 测试（群聊没点名时机器人默认安静潜水）。
4. 若你改过 NapCat 的 Token，请重新双击 `1-一键安装与配置.bat`（或运行 `python setup_wizard.py`）保持三处 Token 完全一致。

### 5. 终端里如果出现中文乱码怎么办？
所有 `.bat` 均内置了 `chcp 65001`，所有 `.ps1` 均采用 UTF-8 BOM 编码；如果你使用的是极旧版 Windows 10 控制台且字体不支持中文，建议将窗口字体改为 **新宋体 / Consolas / Cascadia Mono**，或在 Microsoft Store 安装免费的 **Windows Terminal（终端）** 获得最佳显示效果。
