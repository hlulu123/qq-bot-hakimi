#!/usr/bin/env bash
# 停止 QQ 机器人全部后台服务（cc-connect / ws-guard / Web 控制台）
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
  echo "[错误] 未找到 Python 3。"
  read -r -p "按回车键关闭窗口..." _ || true
  exit 1
fi

cd "$PROJECT_DIR"
echo "=========================================="
echo "  停止 QQ 机器人全部服务"
echo "=========================================="
"$PY" "$PROJECT_DIR/bot_runner.py" stop-all || true

# 可选询问是否同时停止 NapCat Docker 容器（默认 N，保持 QQ 登录在线）
COMPOSE_FILE="$PROJECT_DIR/docker-compose.napcat.yml"
if [ -f "$COMPOSE_FILE" ] && command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  running_cid="$(docker compose -f "$COMPOSE_FILE" ps --status running -q 2>/dev/null || true)"
  if [ -n "$running_cid" ]; then
    echo ""
    read -r -p "是否同时停止 NapCat Docker 容器？（默认 N 保持 QQ 在线，下次免重登）[y/N]: " ans || true
    ans="$(printf '%s' "$ans" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')"
    if [ "$ans" = "y" ] || [ "$ans" = "yes" ]; then
      "$PY" "$PROJECT_DIR/bot_runner.py" napcat down || docker compose -f "$COMPOSE_FILE" down || true
    else
      echo "已保持 NapCat 容器运行。"
    fi
  fi
fi

echo ""
read -r -p "按回车键关闭窗口..." _ || true
