#!/usr/bin/env bash
# =============================================================================
# QQ 机器人「哈基米」macOS 一键安装与配置向导
# 支持 Apple Silicon (M1/M2/M3/M4) 与 Intel Mac
# =============================================================================
set -u

export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 终端彩色输出（非 TTY 时自动降级）
if [ -t 1 ]; then
  C_RESET=$'\033[0m'
  C_BOLD=$'\033[1m'
  C_CYAN=$'\033[36m'
  C_GREEN=$'\033[32m'
  C_YELLOW=$'\033[33m'
  C_RED=$'\033[31m'
else
  C_RESET=""; C_BOLD=""; C_CYAN=""; C_GREEN=""; C_YELLOW=""; C_RED=""
fi

info() { printf '%s[信息]%s %s\n' "$C_CYAN" "$C_RESET" "$1"; }
ok()   { printf '%s[完成]%s %s\n' "$C_GREEN" "$C_RESET" "$1"; }
warn() { printf '%s[提示]%s %s\n' "$C_YELLOW" "$C_RESET" "$1"; }
err()  { printf '%s[错误]%s %s\n' "$C_RED" "$C_RESET" "$1" >&2; }

pause_and_exit() {
  local code="${1:-0}"
  echo ""
  read -r -p "按回车键关闭窗口..." _ || true
  exit "$code"
}

ask_yes_no() {
  # $1 = 提示文案, $2 = 默认值 (Y 或 N)
  local prompt="$1"
  local def="${2:-Y}"
  local hint="[Y/n]"
  [ "$def" = "N" ] && hint="[y/N]"
  local ans=""
  read -r -p "${prompt} ${hint}: " ans || true
  ans="$(printf '%s' "$ans" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')"
  if [ -z "$ans" ]; then
    [ "$def" = "Y" ] && return 0 || return 1
  fi
  case "$ans" in
    y|yes|是|1) return 0 ;;
    *)          return 1 ;;
  esac
}

echo ""
printf '%s========================================================%s\n' "$C_CYAN$C_BOLD" "$C_RESET"
printf '%s     QQ 机器人「哈基米」macOS 一键安装与配置向导%s\n' "$C_CYAN$C_BOLD" "$C_RESET"
printf '%s========================================================%s\n' "$C_CYAN$C_BOLD" "$C_RESET"
echo "当前目录: $PROJECT_DIR"
echo ""

# -----------------------------------------------------------------------------
# 步骤 0：检测是否在只读 DMG 镜像 (/Volumes/...) 或只读目录中运行
# -----------------------------------------------------------------------------
is_writable=0
if touch "$PROJECT_DIR/.write_probe_$$" 2>/dev/null; then
  rm -f "$PROJECT_DIR/.write_probe_$$" 2>/dev/null || true
  is_writable=1
fi

is_volumes=0
case "$PROJECT_DIR" in
  /Volumes/*) is_volumes=1 ;;
esac

if [ "$is_writable" -eq 0 ] || [ "$is_volumes" -eq 1 ]; then
  if [ "$is_writable" -eq 0 ]; then
    warn "检测到当前位于只读磁盘镜像（DMG）或无写入权限的目录中："
    echo "    $PROJECT_DIR"
    echo "机器人需要保存配置、聊天记录与 QQ 登录态，无法在只读镜像内直接运行。"
  else
    warn "检测到当前位于外接卷 / 磁盘镜像路径下（/Volumes/...）："
    echo "    $PROJECT_DIR"
    echo "如果你是从 DMG 镜像打开的，强烈建议先把整个文件夹复制到本机目录再安装。"
  fi
  echo ""
  echo "请选择要自动复制到的位置："
  echo "  [1] 复制到桌面：~/Desktop/QQ机器人-哈基米  （推荐，直接回车）"
  echo "  [2] 复制到主目录：~/QQ机器人-哈基米"
  if [ "$is_writable" -eq 1 ]; then
    echo "  [3] 我确认当前是可写硬盘，继续在当前目录安装"
  fi
  echo "  [0] 退出，我自己手动复制"
  echo ""
  read -r -p "请输入选项 [1]: " copy_choice || true
  copy_choice="${copy_choice:-1}"

  target_dir=""
  case "$copy_choice" in
    1) target_dir="$HOME/Desktop/QQ机器人-哈基米" ;;
    2) target_dir="$HOME/QQ机器人-哈基米" ;;
    3)
      if [ "$is_writable" -eq 1 ]; then
        info "继续在当前目录执行安装..."
      else
        err "当前目录不可写，无法继续。"
        pause_and_exit 1
      fi
      ;;
    *)
      info "已取消。请手动复制整个文件夹到桌面后，再双击「1-一键安装与配置.command」。"
      pause_and_exit 0
      ;;
  esac

  if [ -n "$target_dir" ]; then
    if [ -e "$target_dir" ]; then
      ts="$(date '+%Y%m%d-%H%M%S')"
      warn "目标目录已存在：$target_dir"
      target_dir="${target_dir}-${ts}"
      info "为避免覆盖已有数据，将复制到新目录：$target_dir"
    fi
    info "正在复制文件到：$target_dir ..."
    if ! cp -R "$PROJECT_DIR" "$target_dir"; then
      err "复制失败，请检查磁盘空间或权限后手动复制。"
      pause_and_exit 1
    fi
    xattr -dr com.apple.quarantine "$target_dir" 2>/dev/null || true
    chmod +x "$target_dir"/*.command "$target_dir"/*.sh 2>/dev/null || true
    [ -f "$target_dir/.tools/cc-connect" ] && chmod +x "$target_dir/.tools/cc-connect" 2>/dev/null || true
    ok "已成功复制到：$target_dir"
    echo ""
    if ask_yes_no "是否立即切换到新目录继续安装？" "Y"; then
      exec bash "$target_dir/1-一键安装与配置.command"
    else
      info "稍后请打开 $target_dir，双击其中的「1-一键安装与配置.command」即可。"
      open "$target_dir" 2>/dev/null || true
      pause_and_exit 0
    fi
  fi
fi

# -----------------------------------------------------------------------------
# 步骤 1：清除 macOS Gatekeeper 隔离标记并补齐可执行权限
# -----------------------------------------------------------------------------
printf '%s[1/7] 清理 macOS 安全隔离标记并检查执行权限...%s\n' "$C_BOLD" "$C_RESET"
xattr -dr com.apple.quarantine "$PROJECT_DIR" 2>/dev/null || true
chmod +x "$PROJECT_DIR"/*.command "$PROJECT_DIR"/*.sh 2>/dev/null || true
for bin_path in \
  "$PROJECT_DIR/.tools/cc-connect" \
  "$PROJECT_DIR/bot_runner.py" \
  "$PROJECT_DIR/setup_wizard.py" \
  "$PROJECT_DIR/agy-acp-wrapper.py" \
  "$PROJECT_DIR/onebot-ws-guard.py"; do
  [ -f "$bin_path" ] && chmod +x "$bin_path" 2>/dev/null || true
done
ok "权限与隔离标记处理完成"
echo ""

# -----------------------------------------------------------------------------
# 步骤 2：检测 Python 3.9+ 并准备 .venv 与可选依赖 Pillow
# -----------------------------------------------------------------------------
printf '%s[2/7] 检测 Python 3.9+ 与初始化虚拟环境 (.venv)...%s\n' "$C_BOLD" "$C_RESET"

check_py_ge_39() {
  local cand="$1"
  [ -n "$cand" ] && [ -x "$cand" ] || return 1
  "$cand" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1
}

find_system_python() {
  local cand=""
  for cand in \
    "/opt/homebrew/bin/python3" \
    "/opt/homebrew/bin/python3.12" \
    "/opt/homebrew/bin/python3.11" \
    "/opt/homebrew/bin/python3.10" \
    "/usr/local/bin/python3" \
    "$(command -v python3 2>/dev/null || true)" \
    "/usr/bin/python3"; do
    if check_py_ge_39 "$cand"; then
      printf '%s\n' "$cand"
      return 0
    fi
  done
  return 1
}

SYS_PY="$(find_system_python || true)"
if [ -z "$SYS_PY" ]; then
  err "未检测到 Python 3.9 或更高版本！"
  if command -v brew >/dev/null 2>&1; then
    if ask_yes_no "检测到已安装 Homebrew，是否立即运行 brew install python@3.12？" "Y"; then
      brew install python@3.12 || true
      export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
      SYS_PY="$(find_system_python || true)"
    fi
  fi
fi

if [ -z "$SYS_PY" ]; then
  echo "请先安装 Python 3.9+（任选一种方式）："
  echo "  1) 访问官网下载安装包：https://www.python.org/downloads/macos/"
  echo "  2) 或在终端执行：xcode-select --install"
  echo "安装完成后重新双击本脚本即可。"
  pause_and_exit 1
fi

PY_VER="$("$SYS_PY" -c 'import sys; print(".".join(map(str, sys.version_info[:3])))' 2>/dev/null || echo "3.x")"
ok "检测到系统 Python ${PY_VER}（$SYS_PY）"

VENV_PY="$PROJECT_DIR/.venv/bin/python3"
if ! check_py_ge_39 "$VENV_PY"; then
  info "正在创建项目虚拟环境 .venv ..."
  if "$SYS_PY" -m venv "$PROJECT_DIR/.venv" 2>/dev/null && check_py_ge_39 "$VENV_PY"; then
    ok "虚拟环境创建成功：$VENV_PY"
  else
    warn "创建 .venv 失败，将直接使用系统 Python：$SYS_PY"
    VENV_PY="$SYS_PY"
  fi
else
  ok "已复用现有虚拟环境：$VENV_PY"
fi
PY="$VENV_PY"

# 可选安装 Pillow（用于支持 GIF 动图表情包）
if "$PY" -c "import PIL" >/dev/null 2>&1; then
  ok "已检测到 Pillow（支持动图/GIF 表情包识别）"
else
  info "正在安装可选组件 Pillow（用于识别 QQ 动图/GIF 表情包）..."
  if "$PY" -m pip install --disable-pip-version-check --default-timeout=15 --quiet Pillow >/dev/null 2>&1; then
    ok "Pillow 安装完成"
  else
    warn "默认 PyPI 源较慢或不可达，尝试切换清华大学镜像源安装 Pillow..."
    if "$PY" -m pip install --disable-pip-version-check --default-timeout=15 --quiet \
      -i https://pypi.tuna.tsinghua.edu.cn/simple --trusted-host pypi.tuna.tsinghua.edu.cn Pillow >/dev/null 2>&1; then
      ok "Pillow（清华镜像源）安装完成"
    else
      warn "跳过 Pillow 安装（可能处于离线环境）。这不影响机器人正常运行，仅暂不支持 GIF 动图拆帧，联网后可随时重新运行本脚本补装。"
    fi
  fi
fi
echo ""

# -----------------------------------------------------------------------------
# 步骤 3：检测并安装 Antigravity CLI (agy) 与 Google 账号登录
# -----------------------------------------------------------------------------
printf '%s[3/7] 检测 Antigravity CLI (agy) 与 Google 登录态...%s\n' "$C_BOLD" "$C_RESET"

find_agy() {
  local cand=""
  for cand in \
    "$(command -v agy 2>/dev/null || true)" \
    "$HOME/.local/bin/agy" \
    "/opt/homebrew/bin/agy" \
    "/usr/local/bin/agy"; do
    if [ -n "$cand" ] && [ -x "$cand" ]; then
      printf '%s\n' "$cand"
      return 0
    fi
  done
  return 1
}

AGY_PATH="$(find_agy || true)"
if [ -z "$AGY_PATH" ]; then
  warn "未检测到 Antigravity CLI (agy)。机器人需要通过 agy 调用大模型。"
  if ask_yes_no "是否立即一键安装官方 Antigravity CLI (agy)？" "Y"; then
    info "正在执行官方安装脚本：curl -fsSL https://antigravity.google/cli/install.sh | bash"
    if curl -fsSL https://antigravity.google/cli/install.sh | bash; then
      export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
      AGY_PATH="$(find_agy || true)"
    else
      warn "官方安装脚本执行失败，请检查网络连接（需要可访问 Google 的网络环境）。"
    fi
  fi
fi

if [ -n "$AGY_PATH" ]; then
  ok "检测到 agy：$AGY_PATH"
  agy_logged_in=0
  if [ -d "$HOME/.gemini/antigravity-cli" ] && [ -n "$(ls -A "$HOME/.gemini/antigravity-cli" 2>/dev/null)" ]; then
    agy_logged_in=1
  fi
  if [ "$agy_logged_in" -eq 1 ]; then
    ok "检测到本地存在 agy 登录目录（~/.gemini/antigravity-cli）"
    if ask_yes_no "是否仍要打开 agy 确认或切换 Google 账号登录状态？" "N"; then
      info "即将启动 agy。完成登录或确认后，输入 /exit 或按 Ctrl+C 即可返回安装向导。"
      "$AGY_PATH" || true
    fi
  else
    warn "尚未检测到 ~/.gemini/antigravity-cli 登录凭证。"
    if ask_yes_no "是否现在启动 agy 完成 Google 账号登录？" "Y"; then
      echo ""
      info "即将启动 agy：浏览器会自动打开 Google 登录页；登录成功回到终端后，输入 /exit 或按 Ctrl+C 即可继续安装！"
      echo ""
      "$AGY_PATH" || true
    else
      warn "请记得在启动机器人前于终端运行一次 agy 完成 Google 登录。"
    fi
  fi
else
  warn "当前未安装 agy。你可以先完成后续配置，之后在终端运行以下命令补装："
  echo "    curl -fsSL https://antigravity.google/cli/install.sh | bash"
fi
echo ""

# -----------------------------------------------------------------------------
# 步骤 4：检测 Docker / OrbStack（用于运行 NapCat QQ 协议端）
# -----------------------------------------------------------------------------
printf '%s[4/7] 检测 Docker / OrbStack 环境（用于运行 NapCat）...%s\n' "$C_BOLD" "$C_RESET"

docker_ready=0
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  docker_ready=1
  ok "Docker 引擎运行正常"
elif command -v docker >/dev/null 2>&1; then
  warn "检测到已安装 docker 命令，但引擎尚未启动。"
  if [ -d "/Applications/OrbStack.app" ]; then
    if ask_yes_no "检测到已安装 OrbStack，是否现在启动 OrbStack？" "Y"; then
      open -a OrbStack 2>/dev/null || true
      info "正在等待 OrbStack 引擎就绪（最多 20 秒）..."
      for _ in $(seq 1 20); do
        if docker info >/dev/null 2>&1; then
          docker_ready=1
          break
        fi
        sleep 1
      done
    fi
  elif [ -d "/Applications/Docker.app" ]; then
    if ask_yes_no "检测到已安装 Docker Desktop，是否现在启动？" "Y"; then
      open -a Docker 2>/dev/null || true
      info "正在等待 Docker Desktop 启动（最多 25 秒）..."
      for _ in $(seq 1 25); do
        if docker info >/dev/null 2>&1; then
          docker_ready=1
          break
        fi
        sleep 1
      done
    fi
  fi
  if [ "$docker_ready" -eq 1 ]; then
    ok "Docker 引擎已就绪"
  else
    warn "Docker 引擎暂未就绪，稍后请手动打开 OrbStack 或 Docker Desktop 后再拉起 NapCat。"
  fi
else
  warn "未检测到 Docker 或 OrbStack。"
  echo "macOS 强烈推荐安装轻量高速的 OrbStack（内存占用远低于 Docker Desktop，开箱即用）："
  echo "  官网：https://orbstack.dev"
  echo ""
  if command -v brew >/dev/null 2>&1; then
    if ask_yes_no "检测到已安装 Homebrew，是否立即执行 brew install --cask orbstack？" "Y"; then
      if brew install --cask orbstack; then
        open -a OrbStack 2>/dev/null || true
        info "已安装并打开 OrbStack，请在弹出的窗口完成初始化后按回车继续..."
        read -r -p "OrbStack 初始化完成后按回车继续（或直接回车稍后再配）..." _ || true
        if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
          docker_ready=1
          ok "OrbStack / Docker 引擎已就绪！"
        fi
      else
        warn "Homebrew 安装失败，请手动访问 https://orbstack.dev 下载安装。"
      fi
    fi
  else
    if ask_yes_no "是否在浏览器中打开 OrbStack 官网（https://orbstack.dev）下载安装？" "Y"; then
      open "https://orbstack.dev" 2>/dev/null || true
      echo "下载并安装打开 OrbStack 后，回来按回车继续（也可以先完成下方配置向导）。"
      read -r -p "按回车键继续..." _ || true
      export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.orbstack/bin:$PATH"
      if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
        docker_ready=1
        ok "OrbStack / Docker 引擎已就绪！"
      fi
    fi
  fi
fi
echo ""

# -----------------------------------------------------------------------------
# 步骤 5：运行交互式配置向导 setup_wizard.py
# -----------------------------------------------------------------------------
printf '%s[5/7] 运行交互式配置向导（设置 QQ 号、管理员与安全密钥）...%s\n' "$C_BOLD" "$C_RESET"
if [ ! -f "$PROJECT_DIR/setup_wizard.py" ]; then
  err "未找到 setup_wizard.py，请确认安装包文件是否完整。"
  pause_and_exit 1
fi

cd "$PROJECT_DIR"
if ! "$PY" "$PROJECT_DIR/setup_wizard.py"; then
  err "配置向导未完成或发生错误，请检查上方输出后重试。"
  pause_and_exit 1
fi
ok "配置向导已完成"
echo ""

# -----------------------------------------------------------------------------
# 步骤 6：拉起 NapCat Docker 容器并打开 WebUI 扫码登录 QQ
# -----------------------------------------------------------------------------
printf '%s[6/7] 启动 NapCat 并扫码登录机器人 QQ...%s\n' "$C_BOLD" "$C_RESET"

# 重新探测 Docker 是否已就绪
if [ "$docker_ready" -eq 0 ] && command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  docker_ready=1
fi

COMPOSE_FILE="$PROJECT_DIR/docker-compose.napcat.yml"
if [ "$docker_ready" -eq 1 ] && [ -f "$COMPOSE_FILE" ]; then
  # 判断用户是否选择了 Docker 模式（若 onebot11*.json 绑定了 127.0.0.1 则可能选了外部 NapCat）
  default_napcat_up="Y"
  if "$PY" -c '
import glob, json, sys
files = glob.glob("napcat/config/onebot11*.json")
if files:
    data = json.load(open(files[0], encoding="utf-8"))
    hosts = [s.get("host", "") for s in data.get("network", {}).get("websocketServers", [])]
    if hosts and all(h == "127.0.0.1" for h in hosts):
        sys.exit(2)
' 2>/dev/null; then
    default_napcat_up="Y"
  else
    rc=$?
    if [ "$rc" -eq 2 ]; then
      default_napcat_up="N"
    fi
  fi

  if ask_yes_no "是否现在启动 NapCat Docker 容器并打开扫码页面？" "$default_napcat_up"; then
    info "正在启动 NapCat 容器（首次运行会自动拉取镜像，可能需要 1~3 分钟）..."
    if docker compose -f "$COMPOSE_FILE" up -d; then
      ok "NapCat 容器已启动，正在等待 WebUI 就绪..."
      for _ in $(seq 1 15); do
        if "$PY" -c 'import socket; s=socket.create_connection(("127.0.0.1", 6099), 0.4); s.close()' 2>/dev/null; then
          break
        fi
        sleep 1
      done

      WEBUI_TOKEN="$("$PY" -c '
import json, os
p = os.path.join("napcat", "config", "webui.json")
if os.path.isfile(p):
    try:
        print(json.load(open(p, encoding="utf-8")).get("token", ""))
    except Exception:
        pass
' 2>/dev/null || true)"

      echo ""
      printf '%s--------------------------------------------------------%s\n' "$C_GREEN" "$C_RESET"
      echo "  NapCat WebUI 地址：http://127.0.0.1:6099/webui/"
      if [ -n "$WEBUI_TOKEN" ]; then
        printf '  NapCat WebUI Token：%s%s%s\n' "$C_BOLD$C_YELLOW" "$WEBUI_TOKEN" "$C_RESET"
      else
        echo "  NapCat WebUI Token：请查看 napcat/config/webui.json 或运行 docker logs napcat"
      fi
      printf '%s--------------------------------------------------------%s\n' "$C_GREEN" "$C_RESET"
      echo "请在浏览器中输入上方 Token 登录，并用【机器人 QQ 小号】手机扫码登录。"
      echo ""
      open "http://127.0.0.1:6099/webui/" 2>/dev/null || true
      read -r -p "手机 QQ 扫码登录成功后，回到这里按回车继续..." _ || true
    else
      warn "启动 NapCat 容器失败，请检查网络或 Docker 状态后重试：docker compose -f docker-compose.napcat.yml up -d"
    fi
  fi
else
  warn "跳过自动启动 NapCat Docker 容器（未检测到运行中的 Docker 或无 docker-compose.napcat.yml）。"
  echo "请确保 OrbStack/Docker 启动后执行：docker compose -f docker-compose.napcat.yml up -d"
  echo "然后访问 http://127.0.0.1:6099/webui/ 完成机器人 QQ 扫码登录。"
fi
echo ""

# -----------------------------------------------------------------------------
# 步骤 7：一键后台启动机器人与本地 Web 控制台
# -----------------------------------------------------------------------------
printf '%s[7/7] 启动 QQ 机器人与本地 Web 控制台...%s\n' "$C_BOLD" "$C_RESET"
if [ -f "$PROJECT_DIR/bot_runner.py" ] && [ -f "$PROJECT_DIR/bot.env" ]; then
  if ask_yes_no "是否立即在后台启动机器人并打开本地 Web 控制台？" "Y"; then
    if "$PY" "$PROJECT_DIR/bot_runner.py" start-bg; then
      ok "机器人已在后台启动！"
      "$PY" "$PROJECT_DIR/bot_runner.py" webui open || true
    else
      warn "后台启动未完全成功，建议双击「6-环境体检(Doctor).command」查看详细诊断。"
    fi
  fi
fi

echo ""
printf '%s========================================================%s\n' "$C_GREEN$C_BOLD" "$C_RESET"
printf '%s  全部安装配置步骤已完成！日常使用快捷方式：%s\n' "$C_GREEN$C_BOLD" "$C_RESET"
printf '%s========================================================%s\n' "$C_GREEN$C_BOLD" "$C_RESET"
echo "  - 启动机器人：双击「2-启动机器人(后台).command」"
echo "  - 打开控制台：双击「3-打开Web控制台.command」（http://127.0.0.1:8765）"
echo "  - 停止全部：  双击「5-停止全部服务.command」"
echo "  - 环境体检：  双击「6-环境体检(Doctor).command」"
echo ""
pause_and_exit 0
