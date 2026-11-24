#!/usr/bin/env bash
# 只重启 ws-guard（改了 onebot-ws-guard.py 或 bot.env 里 ws-guard 的设置后用），cc-connect 不用动，会自动重连。
# 双击，或在 Terminal.app 里运行 open restart-ws-guard.command 。
# 不要从 Claude 桌面应用（含它的终端面板）里直接运行：从那里启动的进程连不上 127.0.0.1。
set -euo pipefail

export PATH="${PATH:-}:/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ -x "$PROJECT_DIR/.venv/bin/python3" ]; then
  PY="$PROJECT_DIR/.venv/bin/python3"
elif command -v python3 >/dev/null 2>&1; then
  PY="$(command -v python3)"
elif [ -x "/usr/bin/python3" ]; then
  PY="/usr/bin/python3"
else
  echo "未找到 Python 3" >&2
  exit 1
fi

cd "$PROJECT_DIR"
"$PY" "$PROJECT_DIR/bot_runner.py" restart-ws-guard

sleep 4
LOG_FILE="$PROJECT_DIR/data/ws-guard.log"
if [ -f "$LOG_FILE" ]; then
  echo ""
  echo "ws-guard 日志最后几行："
  tail -n 6 "$LOG_FILE"
fi
echo ""
echo "这个窗口可以关掉。"
