#!/usr/bin/env bash
# 在后台启动 QQ 机器人（双击即可，转发至 bot_runner.py start-bg）。
# 日志写到 data/cc-connect.log，停止用 ./stop.sh。
# 用 Terminal.app 启动是有原因的：从 Claude 桌面应用（含它的终端面板）里启动的进程
# 连不上 127.0.0.1，cc-connect 会报 "connect: bad file descriptor"。
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
"$PY" "$PROJECT_DIR/bot_runner.py" start-bg "$@"
echo ""
echo "停止：在项目目录运行 ./stop.sh 。这个窗口可以关掉。"
