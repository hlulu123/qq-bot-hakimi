#!/usr/bin/env bash
# 仅重启 ws-guard 守护代理（cc-connect 保持在线并在十几秒内自动重连）
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
      printf '%s\n' "$cand"
      return 0
    fi
  done
  return 1
}

PY="$(find_python || true)"
if [ -z "$PY" ]; then
  echo "[错误] 未找到 Python 3，请先运行「1-一键安装与配置.command」。"
  read -r -p "按回车键关闭窗口..." _ || true
  exit 1
fi

cd "$PROJECT_DIR"
echo "=========================================="
echo "  重启 onebot-ws-guard 守护代理"
echo "=========================================="
"$PY" "$PROJECT_DIR/bot_runner.py" restart-ws-guard || true

LOG_FILE="$PROJECT_DIR/data/ws-guard.log"
if [ -f "$LOG_FILE" ]; then
  echo ""
  echo "---------- 最近 ws-guard 日志（最后 20 行）----------"
  tail -n 20 "$LOG_FILE" || true
  echo "----------------------------------------------------"
fi

echo ""
read -r -p "按回车键关闭窗口..." _ || true
