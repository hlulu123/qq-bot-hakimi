#!/usr/bin/env bash
# QQ 机器人「哈基米」本地 Web 控制台管理脚本（CLI 快捷入口，转发至 bot_runner.py）
# 用法：./webui.sh [start|stop|restart|status|fg|open]
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
if [ "$#" -eq 0 ]; then
  exec "$PY" "$PROJECT_DIR/bot_runner.py" webui start
else
  exec "$PY" "$PROJECT_DIR/bot_runner.py" webui "$@"
fi
