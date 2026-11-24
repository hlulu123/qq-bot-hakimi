#!/usr/bin/env bash
# QQ 机器人「哈基米」后台启动器（macOS）
set -u

export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

find_python() {
  local cand=""
  for cand in \
    "$PROJECT_DIR/.venv/bin/python3" \
    "/opt/homebrew/bin/python3" \
    "/usr/local/bin/python3" \
    "$(command -v python3 2>/dev/null || true)" \
    "/usr/bin/python3"; do
    if [ -n "$cand" ] && [ -x "$cand" ]; then
      if "$cand" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then
        printf '%s\n' "$cand"
        return 0
      fi
    fi
  done
  return 1
}

echo "=========================================="
echo "  QQ 机器人「哈基米」后台启动"
echo "=========================================="

if [ ! -f "$PROJECT_DIR/bot.env" ]; then
  echo "[提示] 未找到配置文件 bot.env！"
  echo "请先双击运行「1-一键安装与配置.command」完成初始化配置。"
  echo ""
  read -r -p "按回车键关闭窗口..." _ || true
  exit 1
fi

PY="$(find_python || true)"
if [ -z "$PY" ]; then
  echo "[错误] 未找到可用的 Python 3.9+，请先运行「1-一键安装与配置.command」。"
  echo ""
  read -r -p "按回车键关闭窗口..." _ || true
  exit 1
fi

cd "$PROJECT_DIR"

# 如果存在 docker-compose.napcat.yml 且 Docker 正在运行，但 napcat 容器尚未启动，则自动拉起
COMPOSE_FILE="$PROJECT_DIR/docker-compose.napcat.yml"
if [ -f "$COMPOSE_FILE" ] && command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  running_cid="$(docker compose -f "$COMPOSE_FILE" ps --status running -q 2>/dev/null || true)"
  if [ -z "$running_cid" ]; then
    echo "[信息] 检测到 NapCat 容器未运行，正在自动启动..."
    docker compose -f "$COMPOSE_FILE" up -d || true
    sleep 2
  fi
fi

if ! "$PY" "$PROJECT_DIR/bot_runner.py" start-bg; then
  echo ""
  echo "[错误] 机器人启动失败！请检查上方提示，或双击「6-环境体检(Doctor).command」排查。"
  echo ""
  read -r -p "按回车键关闭窗口..." _ || true
  exit 1
fi

echo ""
"$PY" "$PROJECT_DIR/bot_runner.py" webui open || true

echo ""
echo "提示：停止机器人请双击「5-停止全部服务.command」。"
read -r -p "按回车键关闭窗口..." _ || true
