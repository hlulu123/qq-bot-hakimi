#!/usr/bin/env python3
"""Interactive and non-interactive setup wizard for the QQ Bot.

Zero external dependencies (Python 3.9+ stdlib only; macOS, Linux, Windows).

Generates and keeps in sync:
  - bot.env (chmod 0600 on POSIX)
  - config.toml (properly escaped TOML strings for POSIX & Windows paths)
  - docker-compose.napcat.yml (loopback-only port bindings)
  - napcat/config/onebot11.json & napcat/config/onebot11_<BOT_QQ>.json
  - napcat/config/webui.json
  - Required runtime directories + workspace/GEMINI.md -> chat-sandbox/GEMINI.md sync

Usage:
  Interactive:
      python setup_wizard.py [--project-dir <dir>]

  Non-interactive:
      python setup_wizard.py --non-interactive --bot-qq <qq> --admin-qq <qq1,qq2>
          [--bot-name <name>] [--napcat-mode docker|native]
          [--agy-bin <path>] [--project-dir <dir>] [--reset-tokens]
"""

import argparse
import json
import os
import re
import secrets
import shutil
import subprocess
import sys


def _configure_stdio_utf8():
    """Ensure UTF-8 stdio across Windows terminals and redirected streams."""
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


_configure_stdio_utf8()

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)


def _peek_project_dir_from_argv(argv):
    for idx, tok in enumerate(argv):
        if tok == "--project-dir" and idx + 1 < len(argv):
            return os.path.abspath(os.path.expanduser(argv[idx + 1]))
        if tok.startswith("--project-dir="):
            val = tok.split("=", 1)[1]
            if val:
                return os.path.abspath(os.path.expanduser(val))
    return _SCRIPT_DIR


if __name__ == "__main__":
    _cli_project_dir = _peek_project_dir_from_argv(sys.argv[1:])
    os.environ["BOT_ENV_FILE"] = os.path.join(_cli_project_dir, "bot.env")

import bot_env  # noqa: E402


DEFAULT_BOT_NAME = "哈基米"
DEFAULT_BOT_CALL_NAMES = ["哈基米", "及其猫", "机器猫", "本猫", "紫毛猫", "紫发猫"]
DEFAULT_SOFT_CALL_CUES = "猫猫,小猫,小紫,紫毛,紫发,那只猫,某猫,咪咪,哈基,及其,猫娘"
DEFAULT_OVERUSED_MOTIFS = "猫条,冻干,本猫,好家伙,属于是,电费,插电,赛博,喵,草"
DEFAULT_UPSTREAM_ERROR_REPLY = "这个我接不住 换个话题吧"

_QQ_RE = re.compile(r"^\d{5,15}$")
_PLACEHOLDER_RE = re.compile(
    r"^(?:change[-_]?me|replace[-_]?me|your[-_]?.*(?:token|secret|key).*|"
    r"napcat|token|secret|placeholder|example|todo|xxx+|test[-_]?token)$",
    re.IGNORECASE,
)

# Deprecated v1 memory keys dropped automatically when re-rendering bot.env
_DEPRECATED_ENV_KEYS = frozenset({
    "MEMORY_BATCH_SIZE",
    "MEMORY_MIN_INTERVAL_SECS",
    "MEMORY_IMMEDIATE_DEBOUNCE_SECS",
})

# Known keys managed directly in the rendered bot.env template
_STANDARD_ENV_KEYS = {
    "BOT_QQ_ID",
    "ADMIN_QQ_IDS",
    "IGNORED_BOT_QQ_IDS",
    "IGNORED_BOT_NAMES",
    "NAPCAT_HTTP_URL",
    "NAPCAT_WS_URL",
    "NAPCAT_TOKEN",
    "GUARD_LISTEN_PORT",
    "GUARD_UDP_PORT",
    "GUARD_BUSY_TTL_SECS",
    "GUARD_DM_BUSY_TTL_SECS",
    "GUARD_AMBIENT_LISTEN",
    "GUARD_AMBIENT_GAP_SECS",
    "GUARD_AMBIENT_BACKOFF",
    "GUARD_AMBIENT_MAX_GAP_SECS",
    "GUARD_AMBIENT_MAX_PER_HOUR",
    "GUARD_IMAGE_BUDGET",
    "GUARD_IMAGE_DEDUP_SECS",
    "GUARD_IMAGE_AMBIENT_WINDOW_SECS",
    "GUARD_IMAGE_AMBIENT_MAX",
    "LURK_IMAGE_MAX_DIM",
    "LOW_SIGNAL_FILLER",
    "GROUP_WHITELIST_ENABLED",
    "GROUP_WHITELIST_IDS",
    "REPEAT_ENABLED",
    "REPEAT_MIN_COUNT",
    "REPEAT_WINDOW_SECS",
    "REPEAT_COOLDOWN_SECS",
    "REPEAT_MAX_CHARS",
    "REPEAT_PHOTOS",
    "REPEAT_BLOCK_WORDS",
    "BOT_CALL_NAMES",
    "BOT_SOFT_CALL_CUES",
    "AGY_BIN",
    "AGY_THOUGHT_LEVEL",
    "AGY_LURK_THOUGHT_LEVEL",
    "AGY_TASK_THOUGHT_LEVEL",
    "AGY_WORK_DIR",
    "ADMIN_DM_TURBO",
    "CHAT_SANDBOX_DIR",
    "GROUP_HISTORY_COUNT",
    "BOT_HISTORY_DELTA",
    "BOT_PROMPT_MODE",
    "BOT_PROMPT_REFRESH_TURNS",
    "AMBIENT_MIN_GAP_SECS",
    "AMBIENT_WINDOW_SECS",
    "AMBIENT_MAX_PER_WINDOW",
    "ACTIVE_CONV_WINDOW_SECS",
    "ACTIVE_CONV_MIN_GAP_SECS",
    "ACTIVE_CONV_MAX_PER_WINDOW",
    "BOT_OVERUSED_MOTIFS",
    "BOT_OVERUSED_LOOKBACK",
    "BOT_CASUAL_MAX_CHARS",
    "BOT_FILLER_OPENER_LOOKBACK",
    "UPSTREAM_ERROR_REPLY",
    "USER_MEMORY_ENABLED",
    "MEMORY_DB_PATH",
    "USER_MEMORY_DIR",
    "MEMORY_EXTRACTOR_CWD",
    "MEMORY_MODEL",
    "MEMORY_EFFORT",
    "MEMORY_MAX_CONCURRENT_JOBS",
    "MEMORY_GROUP_MEMORY_ENABLED",
    "MEMORY_GROUP_WINDOW_SIZE",
    "MEMORY_DM_WINDOW_SIZE",
    "MEMORY_IDLE_FLUSH_MINS",
    "MEMORY_RECALL_MAX_ITEMS",
    "MEMORY_RECALL_MAX_CHARS",
    "MEMORY_REPEAT_WINDOW_TURNS",
    "MEMORY_L0_REFRESH_TURNS",
    "MEMORY_EPISODIC_HALFLIFE_DAYS",
    "MEMORY_CONTINUITY_DM_TURNS",
    "MEMORY_SLEEP_ENABLED",
    "MEMORY_SLEEP_START_HOUR",
    "MEMORY_SLEEP_END_HOUR",
    "MEMORY_EPISODIC_ARCHIVE_DAYS",
    "MEMORY_RAW_RETENTION_DAYS",
    "MEMORY_EMBED_URL",
    "MEMORY_EMBED_MODEL",
    "MEMORY_EMBED_API_KEY",
    "CHAT_HISTORY_ENABLED",
    "CHAT_HISTORY_MAX_ROUNDS",
    "CHAT_HISTORY_NAPCAT_MAX_PAGES",
    "CHAT_HISTORY_RETENTION_DAYS",
    "CC_SILENT_QUEUE_NOTICE",
    "CC_SILENT_AUTO_COMPRESS",
    "CC_SILENT_IDLE_RESET",
    "FORWARD_LONG_MSG_GROUP",
    "FORWARD_LONG_MSG_DM",
    "FORWARD_MIN_CHARS",
    "FORWARD_MIN_LINES",
    "FORWARD_NODE_MAX_CHARS",
    "FORWARD_NICKNAME",
}


# ---------------------------------------------------------------------------
# Helpers: atomic I/O, parsing, validation, escaping (shared via bot_env)
# ---------------------------------------------------------------------------

_atomic_write_text = bot_env.atomic_write_text
_format_env_value = bot_env.format_env_value
_parse_existing_bot_env = bot_env.parse_file
_is_executable = bot_env._is_executable_file


def _atomic_copy_bytes(src, dst):
    with open(src, "rb") as f:
        data = f.read()
    if os.path.isfile(dst):
        try:
            with open(dst, "rb") as f:
                if f.read() == data:
                    return False
        except OSError:
            pass
    bot_env.atomic_write_bytes(dst, data)
    return True


def _toml_str(value):
    """Format a Python string as a valid TOML 1.0 basic string (safe for Windows backslashes)."""
    return json.dumps(str(value if value is not None else ""), ensure_ascii=False)


def _is_placeholder_token(token):
    s = str(token or "").strip()
    if len(s) < 8:
        return True
    if any(ch in s for ch in ("<", ">", "{", "}", " ")):
        return True
    if _PLACEHOLDER_RE.match(s):
        return True
    return False


def _validate_single_qq(raw):
    val = str(raw or "").strip()
    if _QQ_RE.match(val):
        return val
    return None


def _parse_admin_qqs(raw):
    """Normalize and validate comma/space-separated admin QQ IDs.

    Returns (valid_list, invalid_list).
    """
    text = str(raw or "").replace("，", ",").replace(";", ",").replace("；", ",")
    parts = [tok.strip() for tok in re.split(r"[,\s]+", text) if tok.strip()]
    valid = []
    invalid = []
    seen = set()
    for tok in parts:
        if _QQ_RE.match(tok):
            if tok not in seen:
                seen.add(tok)
                valid.append(tok)
        else:
            invalid.append(tok)
    return valid, invalid


def _read_existing_mgmt_token(config_toml_path):
    toml_cfg = bot_env.parse_config_toml(config_toml_path)
    mgmt = toml_cfg.get("[management]", {}) or {}
    return str(mgmt.get("token", "") or "").strip()


def _read_existing_json(path):
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            obj = json.load(f)
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def _detect_existing_napcat_mode(project_dir, bot_qq=""):
    """Inspect existing onebot11*.json to infer previous docker/native mode, or None."""
    cfg_dir = os.path.join(project_dir, "napcat", "config")
    candidates = []
    if bot_qq:
        candidates.append(os.path.join(cfg_dir, f"onebot11_{bot_qq}.json"))
    candidates.append(os.path.join(cfg_dir, "onebot11.json"))
    for path in candidates:
        obj = _read_existing_json(path)
        if not obj:
            continue
        net = obj.get("network")
        if not isinstance(net, dict):
            continue
        for key in ("httpServers", "websocketServers"):
            servers = net.get(key)
            if isinstance(servers, list) and servers and isinstance(servers[0], dict):
                host = str(servers[0].get("host") or "").strip()
                if host == "127.0.0.1":
                    return "native"
                if host in ("0.0.0.0", "::"):
                    return "docker"
    return None


def _default_napcat_mode(project_dir, existing_bot_qq=""):
    prev = _detect_existing_napcat_mode(project_dir, existing_bot_qq)
    if prev in ("docker", "native"):
        return prev
    if os.name != "nt":
        return "docker"
    env_path = bot_env.build_subprocess_env().get("PATH", "")
    return "docker" if shutil.which("docker", path=env_path) else "native"


def _detect_agy_binary(explicit="", env_agy=""):
    """Detect agy binary without leaking unrelated workspace settings."""
    env_path = bot_env.build_subprocess_env().get("PATH", "")
    # 1. Explicit CLI argument or existing target bot.env value
    for candidate in (explicit, env_agy):
        raw = str(candidate or "").strip()
        if not raw:
            continue
        expanded = os.path.expanduser(raw)
        if _is_executable(expanded):
            return expanded, True
        found = shutil.which(raw, path=env_path)
        if found and _is_executable(found):
            return found, True

    # 2. Standard cross-platform detection via bot_env.resolve_agy_bin
    resolved = bot_env.resolve_agy_bin(explicit=explicit or env_agy)
    if _is_executable(resolved):
        return resolved, True
    found = shutil.which(resolved, path=env_path)
    if found and _is_executable(found):
        return found, True
    return resolved, False


def _probe_agy_version(agy_path):
    try:
        res = subprocess.run(
            [agy_path, "--version"],
            capture_output=True,
            text=True,
            timeout=5.0,
            env=bot_env.build_subprocess_env(),
            creationflags=bot_env.subprocess_creationflags(detached=False),
        )
        out = (res.stdout or res.stderr or "").strip()
        return out.splitlines()[0].strip() if (res.returncode == 0 and out) else ""
    except Exception:
        return ""


def _build_call_names(bot_name, existing_call_names_csv=""):
    clean_name = re.sub(r'[,，"\'\r\n]+', "", str(bot_name or "")).strip()
    if existing_call_names_csv:
        base = [x.strip() for x in existing_call_names_csv.split(",") if x.strip()]
    else:
        base = list(DEFAULT_BOT_CALL_NAMES)
    if not base:
        base = list(DEFAULT_BOT_CALL_NAMES)

    if not clean_name or clean_name == DEFAULT_BOT_NAME:
        if not existing_call_names_csv:
            return list(DEFAULT_BOT_CALL_NAMES)
        if DEFAULT_BOT_NAME not in base:
            return [DEFAULT_BOT_NAME] + base
        return base

    # Put the custom name first and deduplicate preserving order
    merged = [clean_name] + [x for x in base if x != clean_name]
    return merged


# ---------------------------------------------------------------------------
# Template renderers
# ---------------------------------------------------------------------------

def render_bot_env(
    project_dir,
    bot_qq,
    admin_qqs,
    napcat_token,
    call_names,
    agy_bin,
    existing_env=None,
):
    prev = dict(existing_env or {})

    def _get(key, default):
        val = prev.get(key)
        return default if val is None else val

    admin_csv = ",".join(admin_qqs)
    call_names_csv = ",".join(call_names)
    work_dir = os.path.join(project_dir, "workspace")
    sandbox_dir = os.path.join(project_dir, "chat-sandbox")
    memory_db_path = os.path.join(project_dir, "data", "memory.db")
    memory_dir = os.path.join(project_dir, "data", "user_memories")
    extractor_cwd = os.path.join(project_dir, "data", "memory_extractor_cwd")

    upstream_err = _get("UPSTREAM_ERROR_REPLY", DEFAULT_UPSTREAM_ERROR_REPLY)

    lines = [
        "# =============================================================================",
        "# 机器人共享配置（唯一来源，由 setup_wizard.py 生成）",
        "# agy-acp-wrapper.py / onebot-ws-guard.py / user_memory.py / bot_runner.py 都从这里读。",
        "# cc-connect 自己的 config.toml 里还各有一份 NapCat token 和管理员 QQ（admin_from、",
        "# users.roles.admin），启动时会校验它们和这里一致，不一致就拒绝启动。",
        "# 格式：KEY=VALUE，一行一个。改完后重启机器人生效。",
        "# =============================================================================",
        "",
        "# ---- 身份 ----",
        f"BOT_QQ_ID={_format_env_value(bot_qq)}",
        "# 管理员 QQ，多个用英文逗号分隔",
        f"ADMIN_QQ_IDS={_format_env_value(admin_csv)}",
        "# 不理会的其他机器人（默认 Q群管家）",
        f"IGNORED_BOT_QQ_IDS={_format_env_value(_get('IGNORED_BOT_QQ_IDS', '2854196310'))}",
        f"IGNORED_BOT_NAMES={_format_env_value(_get('IGNORED_BOT_NAMES', 'q群管家'))}",
        "",
        "# ---- NapCat ----",
        f"NAPCAT_HTTP_URL={_format_env_value(_get('NAPCAT_HTTP_URL', 'http://127.0.0.1:3000'))}",
        f"NAPCAT_WS_URL={_format_env_value(_get('NAPCAT_WS_URL', 'ws://127.0.0.1:3001'))}",
        f"NAPCAT_TOKEN={_format_env_value(napcat_token)}",
        "",
        "# ---- ws-guard ----",
        f"GUARD_LISTEN_PORT={_format_env_value(_get('GUARD_LISTEN_PORT', '3002'))}",
        f"GUARD_UDP_PORT={_format_env_value(_get('GUARD_UDP_PORT', '3003'))}",
        "# 群聊单轮最长占用时间（秒），超时自动放行下一条",
        f"GUARD_BUSY_TTL_SECS={_format_env_value(_get('GUARD_BUSY_TTL_SECS', '120'))}",
        "# 私聊单轮最长占用时间（秒），管理员私聊干活可能很久",
        f"GUARD_DM_BUSY_TTL_SECS={_format_env_value(_get('GUARD_DM_BUSY_TTL_SECS', '900'))}",
        "# 是否开启群聊环境潜水监听（1 = 监听群内消息按策略决定是否插嘴；0 = 仅响应 @ 或点名）",
        f"GUARD_AMBIENT_LISTEN={_format_env_value(_get('GUARD_AMBIENT_LISTEN', '1'))}",
        "# 群聊潜水消息送给模型判断的最小间隔（秒），减少低价值消息并发",
        f"GUARD_AMBIENT_GAP_SECS={_format_env_value(_get('GUARD_AMBIENT_GAP_SECS', '20'))}",
        "# 潜水退避：模型连续输出 NO_REPLY 时上面的间隔逐次翻倍（20→40→80→160→封顶），1 = 开启；0 = 固定间隔",
        f"GUARD_AMBIENT_BACKOFF={_format_env_value(_get('GUARD_AMBIENT_BACKOFF', '1'))}",
        f"GUARD_AMBIENT_MAX_GAP_SECS={_format_env_value(_get('GUARD_AMBIENT_MAX_GAP_SECS', '300'))}",
        "# 每群每小时最多为潜水消息唤醒模型几次（0 = 不限）",
        f"GUARD_AMBIENT_MAX_PER_HOUR={_format_env_value(_get('GUARD_AMBIENT_MAX_PER_HOUR', '20'))}",
        "# 没人点名的纯图/纯表情包视觉预算与去重窗口（秒）",
        f"GUARD_IMAGE_BUDGET={_format_env_value(_get('GUARD_IMAGE_BUDGET', '1'))}",
        f"GUARD_IMAGE_DEDUP_SECS={_format_env_value(_get('GUARD_IMAGE_DEDUP_SECS', '3600'))}",
        f"GUARD_IMAGE_AMBIENT_WINDOW_SECS={_format_env_value(_get('GUARD_IMAGE_AMBIENT_WINDOW_SECS', '600'))}",
        f"GUARD_IMAGE_AMBIENT_MAX={_format_env_value(_get('GUARD_IMAGE_AMBIENT_MAX', '1'))}",
        "# 潜水时送去看的图片缩到的最长边（像素，768 = 视觉模型的一个图块）",
        f"LURK_IMAGE_MAX_DIM={_format_env_value(_get('LURK_IMAGE_MAX_DIM', '768'))}",
        "# 低信号过滤：纯“哈哈哈/草/666/嗯”这类叠词也算噪声，不唤醒模型（0 = 只过滤标点和 Emoji）",
        f"LOW_SIGNAL_FILLER={_format_env_value(_get('LOW_SIGNAL_FILLER', '1'))}",
        "# 群聊白名单开关（1 = 仅响应下方 GROUP_WHITELIST_IDS 中的群，其他群静默忽略；0 = 不限制群）",
        f"GROUP_WHITELIST_ENABLED={_format_env_value(_get('GROUP_WHITELIST_ENABLED', '0'))}",
        "# 群聊白名单 QQ 群号列表，英文逗号分隔（GROUP_WHITELIST_ENABLED=1 时生效）",
        f"GROUP_WHITELIST_IDS={_format_env_value(_get('GROUP_WHITELIST_IDS', ''))}",
        "",
        "# ---- 复读（ws-guard 和 wrapper 都读）----",
        "# 群里同一句话或同一个表情包连着出现 REPEAT_MIN_COUNT 条（至少 2 个人发的），就把这条交给模型决定跟不跟。1 开 / 0 关",
        f"REPEAT_ENABLED={_format_env_value(_get('REPEAT_ENABLED', '1'))}",
        f"REPEAT_MIN_COUNT={_format_env_value(_get('REPEAT_MIN_COUNT', '2'))}",
        "# 两条一样的消息隔了超过这么多秒就不算连着",
        f"REPEAT_WINDOW_SECS={_format_env_value(_get('REPEAT_WINDOW_SECS', '180'))}",
        "# 同一个群两次问模型要不要跟，至少隔多少秒",
        f"REPEAT_COOLDOWN_SECS={_format_env_value(_get('REPEAT_COOLDOWN_SECS', '60'))}",
        "# 文字超过这么多字就不算复读",
        f"REPEAT_MAX_CHARS={_format_env_value(_get('REPEAT_MAX_CHARS', '60'))}",
        "# 普通照片的复读也交给模型判断（模型看得到图）。1 开 / 0 关",
        f"REPEAT_PHOTOS={_format_env_value(_get('REPEAT_PHOTOS', '1'))}",
        "# 含这些词就不交给模型，逗号分隔（链接、长串数字、@、点名机器人、/命令 本来就不算）",
        f"REPEAT_BLOCK_WORDS={_format_env_value(_get('REPEAT_BLOCK_WORDS', ''))}",
        "",
        "# ---- 点名 ----",
        f"BOT_CALL_NAMES={_format_env_value(call_names_csv)}",
        f"BOT_SOFT_CALL_CUES={_format_env_value(_get('BOT_SOFT_CALL_CUES', DEFAULT_SOFT_CALL_CUES))}",
        "",
        "# ---- Antigravity / wrapper ----",
        f"AGY_BIN={_format_env_value(agy_bin)}",
        f"AGY_THOUGHT_LEVEL={_format_env_value(_get('AGY_THOUGHT_LEVEL', 'medium'))}",
        f"AGY_LURK_THOUGHT_LEVEL={_format_env_value(_get('AGY_LURK_THOUGHT_LEVEL', 'low'))}",
        f"AGY_TASK_THOUGHT_LEVEL={_format_env_value(_get('AGY_TASK_THOUGHT_LEVEL', 'xhigh'))}",
        "# 管理员私聊会话的工作目录（可读写文件、跑命令）",
        f"AGY_WORK_DIR={_format_env_value(work_dir)}",
        "# 管理员私聊 Turbo 模式：1 = 改文件、跑命令等一律自动放行；0 = 每次发确认给管理员。只影响管理员私聊",
        f"ADMIN_DM_TURBO={_format_env_value(_get('ADMIN_DM_TURBO', '1'))}",
        "# 群聊和普通用户私聊的工作目录：只放一份 GEMINI.md，别的什么都没有",
        f"CHAT_SANDBOX_DIR={_format_env_value(sandbox_dir)}",
        f"GROUP_HISTORY_COUNT={_format_env_value(_get('GROUP_HISTORY_COUNT', '30'))}",
        "# 群聊历史增量注入：首轮/压缩后给完整窗口，之后每轮只给上一轮之后新增的消息（0 = 每轮全量）",
        f"BOT_HISTORY_DELTA={_format_env_value(_get('BOT_HISTORY_DELTA', '1'))}",
        "# 提示词模式：lean = 规则块仅首轮或变化时完整注入；full = 每轮全量注入",
        f"BOT_PROMPT_MODE={_format_env_value(_get('BOT_PROMPT_MODE', 'lean'))}",
        "# lean 模式下每隔多少轮重新完整注入一次规则块（0 = 不定期刷新）",
        f"BOT_PROMPT_REFRESH_TURNS={_format_env_value(_get('BOT_PROMPT_REFRESH_TURNS', '30'))}",
        "# 没人叫时主动接梗的频率：两次至少隔 AMBIENT_MIN_GAP_SECS 秒，每 AMBIENT_WINDOW_SECS 秒最多 AMBIENT_MAX_PER_WINDOW 次（0 = 不限次数）",
        "# 刚说完话 ACTIVE_CONV_WINDOW_SECS 秒内，群友接话时可以更快跟一句（间隔 ACTIVE_CONV_MIN_GAP_SECS 秒；ACTIVE_CONV_MAX_PER_WINDOW=0 表示活跃接话不限次数）",
        f"AMBIENT_MIN_GAP_SECS={_format_env_value(_get('AMBIENT_MIN_GAP_SECS', '45'))}",
        f"AMBIENT_WINDOW_SECS={_format_env_value(_get('AMBIENT_WINDOW_SECS', '600'))}",
        f"AMBIENT_MAX_PER_WINDOW={_format_env_value(_get('AMBIENT_MAX_PER_WINDOW', '4'))}",
        f"ACTIVE_CONV_WINDOW_SECS={_format_env_value(_get('ACTIVE_CONV_WINDOW_SECS', '60'))}",
        f"ACTIVE_CONV_MIN_GAP_SECS={_format_env_value(_get('ACTIVE_CONV_MIN_GAP_SECS', '10'))}",
        f"ACTIVE_CONV_MAX_PER_WINDOW={_format_env_value(_get('ACTIVE_CONV_MAX_PER_WINDOW', '4'))}",
        "# 容易被用烂的词（逗号分隔）：最近 BOT_OVERUSED_LOOKBACK 条回复里出现过，下一轮就提醒模型别再用",
        f"BOT_OVERUSED_MOTIFS={_format_env_value(_get('BOT_OVERUSED_MOTIFS', DEFAULT_OVERUSED_MOTIFS))}",
        f"BOT_OVERUSED_LOOKBACK={_format_env_value(_get('BOT_OVERUSED_LOOKBACK', '8'))}",
        "# 不超过这么多字的单行闲聊回复，发送前把中文逗号换成空格、去掉句尾句号（0 关闭）",
        f"BOT_CASUAL_MAX_CHARS={_format_env_value(_get('BOT_CASUAL_MAX_CHARS', '30'))}",
        "# 最近这么多条回复里有一条先垫语气词再说话，下一轮提醒开头直接说内容（0 关闭）",
        f"BOT_FILLER_OPENER_LOOKBACK={_format_env_value(_get('BOT_FILLER_OPENER_LOOKBACK', '4'))}",
        "# 模型把上游报错原文当回复吐出来时：被叫到就换成这句，潜水时直接不说话；管理员私聊照原样显示",
        f"UPSTREAM_ERROR_REPLY={_format_env_value(upstream_err, force_quote=True)}",
        "",
        "# ---- 每用户长期记忆（SQLite v2 记忆引擎：群聊与私聊严格隔离）----",
        f"USER_MEMORY_ENABLED={_format_env_value(_get('USER_MEMORY_ENABLED', '1'))}",
        "# v2 SQLite 记忆数据库路径",
        f"MEMORY_DB_PATH={_format_env_value(memory_db_path)}",
        "# 保留 v1 JSON 目录路径供只读归档参考/迁移",
        f"USER_MEMORY_DIR={_format_env_value(memory_dir)}",
        f"MEMORY_EXTRACTOR_CWD={_format_env_value(extractor_cwd)}",
        f"MEMORY_MODEL={_format_env_value(_get('MEMORY_MODEL', 'gemini-4-neon'))}",
        f"MEMORY_EFFORT={_format_env_value(_get('MEMORY_EFFORT', 'low'))}",
        f"MEMORY_MAX_CONCURRENT_JOBS={_format_env_value(_get('MEMORY_MAX_CONCURRENT_JOBS', '2'))}",
        "# 是否启用群聊共享群记忆：1 开 / 0 关",
        f"MEMORY_GROUP_MEMORY_ENABLED={_format_env_value(_get('MEMORY_GROUP_MEMORY_ENABLED', '1'))}",
        "# 窗口提炼阈值：群聊/私聊累计有效消息条数与静默自动整理分钟数",
        f"MEMORY_GROUP_WINDOW_SIZE={_format_env_value(_get('MEMORY_GROUP_WINDOW_SIZE', '12'))}",
        f"MEMORY_DM_WINDOW_SIZE={_format_env_value(_get('MEMORY_DM_WINDOW_SIZE', '6'))}",
        f"MEMORY_IDLE_FLUSH_MINS={_format_env_value(_get('MEMORY_IDLE_FLUSH_MINS', '10'))}",
        "# 混合召回预算与去重（L2 最大条数、总字数上限、不重复轮数、L0 刷新轮数、事件半衰期天数）",
        f"MEMORY_RECALL_MAX_ITEMS={_format_env_value(_get('MEMORY_RECALL_MAX_ITEMS', '4'))}",
        f"MEMORY_RECALL_MAX_CHARS={_format_env_value(_get('MEMORY_RECALL_MAX_CHARS', '600'))}",
        f"MEMORY_REPEAT_WINDOW_TURNS={_format_env_value(_get('MEMORY_REPEAT_WINDOW_TURNS', '10'))}",
        f"MEMORY_L0_REFRESH_TURNS={_format_env_value(_get('MEMORY_L0_REFRESH_TURNS', '12'))}",
        f"MEMORY_EPISODIC_HALFLIFE_DAYS={_format_env_value(_get('MEMORY_EPISODIC_HALFLIFE_DAYS', '21'))}",
        "# 新会话/重启后首轮续接最多回放多少轮私聊原始对话",
        f"MEMORY_CONTINUITY_DM_TURNS={_format_env_value(_get('MEMORY_CONTINUITY_DM_TURNS', '12'))}",
        "# 夜间 Sleep 维护周期（自动去重合并、冲突检测、档案重整与冷数据归档）",
        f"MEMORY_SLEEP_ENABLED={_format_env_value(_get('MEMORY_SLEEP_ENABLED', '1'))}",
        f"MEMORY_SLEEP_START_HOUR={_format_env_value(_get('MEMORY_SLEEP_START_HOUR', '3'))}",
        f"MEMORY_SLEEP_END_HOUR={_format_env_value(_get('MEMORY_SLEEP_END_HOUR', '6'))}",
        f"MEMORY_EPISODIC_ARCHIVE_DAYS={_format_env_value(_get('MEMORY_EPISODIC_ARCHIVE_DAYS', '60'))}",
        f"MEMORY_RAW_RETENTION_DAYS={_format_env_value(_get('MEMORY_RAW_RETENTION_DAYS', '60'))}",
        "# 可选：外接 HTTP Embedding 服务（留空则纯本地三通道召回，零外部依赖）",
        f"MEMORY_EMBED_URL={_format_env_value(_get('MEMORY_EMBED_URL', ''))}",
        f"MEMORY_EMBED_MODEL={_format_env_value(_get('MEMORY_EMBED_MODEL', ''))}",
        f"MEMORY_EMBED_API_KEY={_format_env_value(_get('MEMORY_EMBED_API_KEY', ''))}",
        "",
        "# ---- 历史聊天记录自主翻查（本地归档 + NapCat 分页回溯 + 会话记录兜底）----",
        f"CHAT_HISTORY_ENABLED={_format_env_value(_get('CHAT_HISTORY_ENABLED', '1'))}",
        f"CHAT_HISTORY_MAX_ROUNDS={_format_env_value(_get('CHAT_HISTORY_MAX_ROUNDS', '3'))}",
        f"CHAT_HISTORY_NAPCAT_MAX_PAGES={_format_env_value(_get('CHAT_HISTORY_NAPCAT_MAX_PAGES', '6'))}",
        f"CHAT_HISTORY_RETENTION_DAYS={_format_env_value(_get('CHAT_HISTORY_RETENTION_DAYS', '30'))}",
        "",
        "# ---- cc-connect ----",
        "# 不往群里发“消息已收到，将在当前任务完成后处理”（本地补丁里加的开关）",
        f"CC_SILENT_QUEUE_NOTICE={_format_env_value(_get('CC_SILENT_QUEUE_NOTICE', '1'))}",
        "# 自动压缩上下文时不往群里发“正在压缩上下文...”（本地补丁开关；手动 /compress 仍提示）",
        f"CC_SILENT_AUTO_COMPRESS={_format_env_value(_get('CC_SILENT_AUTO_COMPRESS', '1'))}",
        "# 冷场空闲自动开启新会话时不往群里发“因空闲超过 X 分钟，已自动切换到新会话”（本地补丁开关；手动 /new 仍提示）",
        f"CC_SILENT_IDLE_RESET={_format_env_value(_get('CC_SILENT_IDLE_RESET', '1'))}",
        "",
        "# ---- 长消息合并转发（ws-guard）----",
        "# 一条回复超过 FORWARD_MIN_CHARS 个字或 FORWARD_MIN_LINES 行时，改成“合并转发的聊天记录”卡片发出，防止刷屏",
        "# 群聊默认开；私聊默认关（管理员私聊看任务结果不用每次点开卡片）。1 开 / 0 关",
        f"FORWARD_LONG_MSG_GROUP={_format_env_value(_get('FORWARD_LONG_MSG_GROUP', '1'))}",
        f"FORWARD_LONG_MSG_DM={_format_env_value(_get('FORWARD_LONG_MSG_DM', '0'))}",
        f"FORWARD_MIN_CHARS={_format_env_value(_get('FORWARD_MIN_CHARS', '300'))}",
        f"FORWARD_MIN_LINES={_format_env_value(_get('FORWARD_MIN_LINES', '12'))}",
        "# 卡片里每个气泡最多多少字，长文优先按空行分段拆成多个气泡",
        f"FORWARD_NODE_MAX_CHARS={_format_env_value(_get('FORWARD_NODE_MAX_CHARS', '800'))}",
        "# 卡片里显示的发送者昵称，留空则用 BOT_CALL_NAMES 的第一个",
        f"FORWARD_NICKNAME={_format_env_value(_get('FORWARD_NICKNAME', ''))}",
    ]

    # Preserve any additional custom keys from an existing bot.env (dropping deprecated v1 keys)
    extras = [
        (k, v)
        for k, v in prev.items()
        if k not in _STANDARD_ENV_KEYS and k not in _DEPRECATED_ENV_KEYS
    ]
    if extras:
        lines.append("")
        lines.append("# ---- 其他自定义项（保留自原配置）----")
        for k, v in extras:
            lines.append(f"{k}={_format_env_value(v)}")

    return "\n".join(lines) + "\n"


def render_config_toml(project_dir, admin_qqs, napcat_token, management_token, existing_env=None):
    prev = dict(existing_env or {})
    data_dir = os.path.join(project_dir, "data")
    work_dir = os.path.join(project_dir, "workspace")
    python_bin = bot_env.resolve_python_bin(project_dir)
    wrapper_script = os.path.join(project_dir, "agy-acp-wrapper.py")

    admin_csv = ",".join(admin_qqs)
    admin_arr = "[" + ", ".join(_toml_str(q) for q in admin_qqs) + "]"
    guard_port = (prev.get("GUARD_LISTEN_PORT") or "3002").strip() or "3002"
    http_url = (prev.get("NAPCAT_HTTP_URL") or "http://127.0.0.1:3000").strip() or "http://127.0.0.1:3000"
    ws_url = f"ws://127.0.0.1:{guard_port}"

    lines = [
        "# =============================================================================",
        "# QQ-ACP 配置文件（cc-connect + NapCatQQ + Antigravity CLI ACP）",
        "# 由 setup_wizard.py 自动生成",
        "# =============================================================================",
        "",
        'language = "zh"',
        f"data_dir = {_toml_str(data_dir)}",
        "",
        "[log]",
        'level = "info"',
        "",
        "# QQ 聊天窗适配：隐藏思考过程、工具刷屏及底部模型/Token/路径尾巴",
        "[display]",
        'mode = "compact"',
        "thinking_messages = false",
        "tool_messages = false",
        "reply_footer = false",
        "show_context_indicator = false",
        "hide_agent_footer = true",
        "",
        "# 关闭全局机械限流提示，避免全群监听水群时弹出“请慢一点”刷屏",
        "# 发言节流与防刷屏由 agy-acp-wrapper.py 的静默冷却机制控制",
        "[rate_limit]",
        "max_messages = 0",
        "window_secs = 60",
        "",
        "# 本地管理 API：http://127.0.0.1:9820（仅本机监听）",
        "[management]",
        "enabled = true",
        "port = 9820",
        f"token = {_toml_str(management_token)}",
        "",
        "# =============================================================================",
        "# 主项目：Antigravity CLI (ACP) <-> QQ (NapCat OneBot v11)",
        "# =============================================================================",
        "[[projects]]",
        'name = "qq-gemini-acp"',
        "",
        "# 多群资源与会话生命周期管理：",
        "# 1. 某群空闲 30 分钟自动回收对应的 live agent 子进程释放内存（保留会话ID，下次发消息无感恢复）",
        "agent_session_idle_timeout_mins = 30",
        "# 2. 某群冷场超过 6 小时后自动开启清爽新会话（配合每用户长期记忆 + 群聊上下文，既防漂移又不失忆）",
        "reset_on_idle_mins = 360",
        "",
        "# 关闭回复底部的工作目录尾巴（reply_footer / show_context_indicator 统一在上方 [display] 配置）",
        "show_workdir_indicator = false",
        "",
        "# 将发言人 QQ 号、群名片/昵称与群号注入到每条消息前，便于区分谁对谁说话与权限判断",
        "inject_sender = true",
        "",
        "# 管理员 QQ 白名单（仅管理员可执行特权命令与任务）",
        f"admin_from = {_toml_str(admin_csv)}",
        "",
        "# 上下文自动压缩：超过 120K Token 后压缩；两次压缩至少间隔 15 分钟。",
        "# 硬上限（本地补丁新增）：超过 200K 时不必等满 15 分钟，距上次压缩满 3 分钟即可再压",
        "[projects.auto_compress]",
        "enabled = true",
        "max_tokens = 120000",
        "min_gap_mins = 15",
        "hard_max_tokens = 200000",
        "hard_min_gap_mins = 3",
        "allow_heuristic = false",
        "",
        "# 分角色权限控制：管理员放开全部命令；普通群友/其他人禁用所有斜杠命令，仅可纯文本聊天",
        "[projects.users]",
        'default_role = "member"',
        "",
        "[projects.users.roles.admin]",
        f"user_ids = {admin_arr}",
        "disabled_commands = []",
        "rate_limit = { max_messages = 0, window_secs = 60 }",
        "",
        "[projects.users.roles.member]",
        'user_ids = ["*"]',
        'disabled_commands = ["*"]',
        "rate_limit = { max_messages = 0, window_secs = 60 }",
        "",
        "[projects.agent]",
        'type = "acp"',
        "",
        "[projects.agent.options]",
        f"work_dir = {_toml_str(work_dir)}",
        "# 通过 ACP 包装脚本启动 agy acp（每个会话一个进程）：",
        "# 1. 会话隔离：仅管理员私聊在 workspace/ 运行；群聊与非管理员私聊在 chat-sandbox/ 运行",
        "# 2. 动态切换思考强度：群聊监听 low / 普通闲聊 medium / 管理员任务 xhigh",
        "# 3. 低频监听群聊 + 每用户长期记忆 + NapCat 近期群聊上下文",
        f"cmd = {_toml_str(python_bin)}",
        f"args = [{_toml_str(wrapper_script)}]",
        'mode = "default"',
        'display_name = "Antigravity ACP"',
        "",
        "# 包装脚本、ws-guard、记忆模块的环境变量全部在 bot.env 里（唯一来源）。",
        "# 注意：NapCat token 和管理员 QQ 在本文件里还各有一份（platforms.options.token、",
        "# admin_from、users.roles.admin.user_ids），启动时会校验它们和 bot.env 一致。",
        "",
        "[[projects.platforms]]",
        'type = "qq"',
        "",
        "[projects.platforms.options]",
        "# 连接本地 OneBot WS 守护代理（127.0.0.1:3002 -> NapCat 127.0.0.1:3001）",
        f"ws_url = {_toml_str(ws_url)}",
        "# NapCat Access Token（已与 bot.env 和 NapCat OneBot11 配置同步）",
        f"token = {_toml_str(napcat_token)}",
        "# NapCat OneBot HTTP API 地址（用于文件/附件收发及群聊历史上下文读取）",
        f"http_url = {_toml_str(http_url)}",
        "# 允许所有人触发对话（具体执行权限由 admin_from / users 角色与 ACP 包装层严格限制为仅管理员）",
        'allow_from = "*"',
        "# 持续监听群聊所有消息；被点名/感觉在喊它/有意思时冒泡，没意思时输出 NO_REPLY 安静潜水",
        "require_mention = false",
        "# 群聊内所有人共享同一个会话上下文，配合 inject_sender 与群聊历史能看清所有人的消息并分清谁对谁",
        "share_session_in_channel = true",
    ]
    return "\n".join(lines) + "\n"


def patch_existing_config_toml(
    existing_text,
    project_dir,
    admin_qqs,
    napcat_token,
    management_token,
    existing_env=None,
):
    """Patch only wizard-managed fields in an existing config.toml.

    Preserves user customizations such as require_mention, auto_compress,
    display settings, idle timeouts, comments, and formatting. Falls back to
    ``render_config_toml`` if required core sections are absent.
    """
    if not existing_text or not existing_text.strip():
        return render_config_toml(
            project_dir=project_dir,
            admin_qqs=admin_qqs,
            napcat_token=napcat_token,
            management_token=management_token,
            existing_env=existing_env,
        )

    lines = existing_text.splitlines()
    present_sections = {""}
    for line in lines:
        nc = bot_env._strip_toml_comment(line).strip()
        if (nc.startswith("[[") and nc.endswith("]]")) or (nc.startswith("[") and nc.endswith("]")):
            present_sections.add(nc)

    required_sections = {
        "[[projects]]",
        "[projects.users.roles.admin]",
        "[projects.platforms.options]",
    }
    if not required_sections.issubset(present_sections):
        return render_config_toml(
            project_dir=project_dir,
            admin_qqs=admin_qqs,
            napcat_token=napcat_token,
            management_token=management_token,
            existing_env=existing_env,
        )

    prev = dict(existing_env or {})
    data_dir = os.path.join(project_dir, "data")
    work_dir = os.path.join(project_dir, "workspace")
    python_bin = bot_env.resolve_python_bin(project_dir)
    wrapper_script = os.path.join(project_dir, "agy-acp-wrapper.py")

    admin_csv = ",".join(admin_qqs)
    admin_arr = "[" + ", ".join(_toml_str(q) for q in admin_qqs) + "]"
    guard_port = (prev.get("GUARD_LISTEN_PORT") or "3002").strip() or "3002"
    ws_url = f"ws://127.0.0.1:{guard_port}"

    section_targets = {
        "": [
            ("data_dir", _toml_str(data_dir)),
        ],
        "[management]": [
            ("token", _toml_str(management_token)),
        ],
        "[[projects]]": [
            ("admin_from", _toml_str(admin_csv)),
        ],
        "[projects.users.roles.admin]": [
            ("user_ids", admin_arr),
        ],
        "[projects.agent.options]": [
            ("work_dir", _toml_str(work_dir)),
            ("cmd", _toml_str(python_bin)),
            ("args", f"[{_toml_str(wrapper_script)}]"),
        ],
        "[projects.platforms.options]": [
            ("token", _toml_str(napcat_token)),
            ("ws_url", _toml_str(ws_url)),
        ],
    }
    target_lookup = {
        (sec, key): val
        for sec, pairs in section_targets.items()
        for key, val in pairs
    }

    key_re = re.compile(r"^(\s*)([A-Za-z0-9_.-]+)(\s*=\s*)(.*)$")

    def _should_patch_ws_url(raw_rhs):
        cur = str(bot_env._parse_toml_scalar(raw_rhs) or "").strip()
        if not cur or "<" in cur or ">" in cur:
            return True
        low = cur.lower()
        return low.startswith("ws://127.0.0.1:") or low.startswith("ws://localhost:")

    def _insert_missing_for_section(out_lines, sec, seen_keys):
        missing = [
            (k, v)
            for k, v in section_targets.get(sec, ())
            if (sec, k) not in seen_keys
        ]
        if not missing:
            return
        # Insert before trailing blank lines so formatting stays neat
        insert_at = len(out_lines)
        while insert_at > 0 and out_lines[insert_at - 1].strip() == "":
            insert_at -= 1
        additions = [f"{k} = {v}" for k, v in missing]
        out_lines[insert_at:insert_at] = additions
        for k, _ in missing:
            seen_keys.add((sec, k))

    out = []
    current_sec = ""
    seen = set()
    skipping_ml = False
    ml_buf = ""

    for raw_line in lines:
        no_comment = bot_env._strip_toml_comment(raw_line).strip()
        if skipping_ml:
            if no_comment:
                ml_buf += " " + no_comment
            if bot_env._toml_bracket_balance(ml_buf) <= 0:
                skipping_ml = False
                ml_buf = ""
            continue

        if (no_comment.startswith("[[") and no_comment.endswith("]]")) or (
            no_comment.startswith("[") and no_comment.endswith("]")
        ):
            _insert_missing_for_section(out, current_sec, seen)
            current_sec = no_comment
            out.append(raw_line)
            continue

        m = key_re.match(raw_line)
        if m and not raw_line.lstrip().startswith("#"):
            indent, key, _, rhs = m.groups()
            pair = (current_sec, key)
            if pair in target_lookup and pair not in seen:
                rhs_nc = bot_env._strip_toml_comment(rhs).strip()
                if pair == ("[projects.platforms.options]", "ws_url") and not _should_patch_ws_url(rhs):
                    seen.add(pair)
                    out.append(raw_line)
                    continue
                out.append(f"{indent}{key} = {target_lookup[pair]}")
                seen.add(pair)
                if rhs_nc.startswith("[") and bot_env._toml_bracket_balance(rhs_nc) > 0:
                    skipping_ml = True
                    ml_buf = rhs_nc
                continue

        out.append(raw_line)

    _insert_missing_for_section(out, current_sec, seen)
    return "\n".join(out).rstrip("\n") + "\n"


def render_docker_compose(bot_qq):
    lines = [
        "services:",
        "  napcat:",
        "    image: ghcr.io/napneko/nodenapcat:latest",
        "    container_name: napcat",
        "    restart: unless-stopped",
        "    # 只绑本机回环地址：同一局域网/公共 Wi-Fi 里的其他设备访问不到",
        "    ports:",
        '      - "127.0.0.1:3000:3000"   # OneBot HTTP API',
        '      - "127.0.0.1:3001:3001"   # OneBot 正向 WebSocket（ws-guard 连接）',
        '      - "127.0.0.1:6099:6099"   # NapCat WebUI 管理面板',
        "    environment:",
        "      - TZ=Asia/Shanghai",
        "      - NAPCAT_GID=0",
        "      - NAPCAT_UID=0",
        "      # 用 napcat/QQ 里保存的登录态快速登录机器人 QQ，重建容器后不用重新扫码",
        f"      - NAPCAT_QUICK_ACCOUNT=${{BOT_QQ_ID:-{bot_qq}}}",
        "    volumes:",
        "      - ./napcat/config:/app/napcat/config",
        "      - ./napcat/QQ:/root/.config/QQ",
    ]
    return "\n".join(lines) + "\n"


def build_onebot11_config(napcat_token, napcat_mode):
    # CRITICAL for LAN security:
    # - Docker mode binds 0.0.0.0 inside container (docker-compose binds host side to 127.0.0.1 only)
    # - Native mode binds 127.0.0.1 directly so LAN devices cannot reach OneBot endpoints
    bind_host = "0.0.0.0" if napcat_mode == "docker" else "127.0.0.1"
    return {
        "network": {
            "httpServers": [
                {
                    "name": "cc-connect-http",
                    "enable": True,
                    "host": bind_host,
                    "port": 3000,
                    "enableCors": True,
                    "enableWebsocket": False,
                    "messagePostFormat": "array",
                    "token": napcat_token,
                    "debug": False,
                }
            ],
            "httpSseServers": [],
            "httpClients": [],
            "websocketServers": [
                {
                    "name": "cc-connect-ws",
                    "enable": True,
                    "host": bind_host,
                    "port": 3001,
                    "messagePostFormat": "array",
                    "reportSelfMessage": False,
                    "token": napcat_token,
                    "enableForcePushEvent": True,
                    "debug": False,
                    "heartInterval": 30000,
                }
            ],
            "websocketClients": [],
            "plugins": [],
        },
        "musicSignUrl": "",
        "enableLocalFile2Url": False,
        "parseMultMsg": False,
        "imageDownloadProxy": "",
        "timeout": {
            "baseTimeout": 10000,
            "uploadSpeedKBps": 256,
            "downloadSpeedKBps": 256,
            "maxTimeout": 1800000,
        },
    }


def build_napcat_webui_config(webui_token, napcat_mode, existing_webui=None):
    bind_host = "::" if napcat_mode == "docker" else "127.0.0.1"
    if isinstance(existing_webui, dict):
        data = dict(existing_webui)
        data["host"] = bind_host
        data["port"] = int(data.get("port") or 6099)
        data["token"] = webui_token
        data.setdefault("loginRate", 10)
        data.setdefault("autoLoginAccount", "")
        data.setdefault("disableWebUI", False)
        return data
    return {
        "host": bind_host,
        "port": 6099,
        "token": webui_token,
        "loginRate": 10,
        "autoLoginAccount": "",
        "disableWebUI": False,
        "accessControlMode": "none",
        "ipWhitelist": [],
        "ipBlacklist": [],
        "enableXForwardedFor": False,
        "enable2FA": False,
        "totpSecret": "",
    }


# ---------------------------------------------------------------------------
# Interactive prompts
# ---------------------------------------------------------------------------

def _prompt_line(prompt_text):
    return input(prompt_text)


def _interactive_collect(project_dir, args, existing_env):
    print("==========================================================")
    print("  欢迎使用 QQ 机器人一键配置向导")
    print("==========================================================")
    print(f"项目目录：{project_dir}")
    if existing_env:
        print("检测到已有配置：重新运行向导将自动保留现有安全 Token（除非指定 --reset-tokens）。")
    print("")

    # Step 1: BOT_QQ_ID
    default_bot_qq = (args.bot_qq or existing_env.get("BOT_QQ_ID") or "").strip()
    print("【第 1 步 / 共 5 步】填写机器人 QQ 号")
    print("  这是用来登录 NapCat、在群里聊天的机器人 QQ 号（建议使用小号）。")
    while True:
        hint = f" [回车保留: {default_bot_qq}]" if default_bot_qq else ""
        raw = _prompt_line(f"  请输入机器人 QQ 号（5~15 位纯数字）{hint}: ").strip()
        candidate = raw or default_bot_qq
        valid = _validate_single_qq(candidate)
        if valid:
            bot_qq = valid
            break
        print("  × QQ 号格式不正确，请输入 5~15 位纯数字。")
    print("")

    # Step 2: ADMIN_QQ_IDS
    default_admin_raw = (args.admin_qq or existing_env.get("ADMIN_QQ_IDS") or "").strip()
    print("【第 2 步 / 共 5 步】填写管理员 QQ 号")
    print("  管理员可以使用斜杠命令，并通过私聊让机器人在 workspace/ 中读写文件、执行任务。")
    print("  支持多个管理员，用英文逗号分隔（例如：10001,10002）。")
    while True:
        hint = f" [回车保留: {default_admin_raw}]" if default_admin_raw else ""
        raw = _prompt_line(f"  请输入管理员 QQ 号{hint}: ").strip()
        candidate = raw or default_admin_raw
        valid_admins, bad_admins = _parse_admin_qqs(candidate)
        if valid_admins and not bad_admins:
            admin_qqs = valid_admins
            break
        if bad_admins:
            print(f"  × 发现无效的 QQ 号（需为 5~15 位纯数字）：{', '.join(bad_admins)}")
        else:
            print("  × 请至少输入一个有效的管理员 QQ 号。")
    if bot_qq in admin_qqs:
        print("  ! 提示：管理员 QQ 与机器人 QQ 相同，通常建议使用你的个人大号作为管理员。")
    print("")

    # Step 3: Bot Name
    existing_calls = [
        x.strip() for x in (existing_env.get("BOT_CALL_NAMES") or "").split(",") if x.strip()
    ]
    default_name = (args.bot_name or (existing_calls[0] if existing_calls else DEFAULT_BOT_NAME)).strip() or DEFAULT_BOT_NAME
    print("【第 3 步 / 共 5 步】给机器人起个名字")
    print(f"  直接按回车默认使用「{default_name}」（保持与原作者完全一致的人设与称呼）；")
    print("  若输入新名字，会自动加入直接点名词列表（BOT_CALL_NAMES）最前面。")
    print("  提示：机器人详细人设（workspace/GEMINI.md）之后可在 WebUI 中随时可视化修改。")
    raw_name = _prompt_line(f"  机器人名字 [默认: {default_name}]: ").strip()
    bot_name = raw_name or default_name
    if bot_name != DEFAULT_BOT_NAME:
        print(f"  ✓ 已将「{bot_name}」设为首选称呼，随时可在 WebUI「人设」页面同步调整人设描述。")
    print("")

    # Step 4: NapCat Mode
    default_mode = args.napcat_mode or _default_napcat_mode(project_dir, bot_qq)
    default_choice = "1" if default_mode == "docker" else "2"
    print("【第 4 步 / 共 5 步】选择 NapCat（QQ 协议端）运行方式")
    print("  1 = Docker 容器（推荐：macOS 用 OrbStack / Docker Desktop，Windows 用 Docker Desktop，一键拉起且完全隔离）")
    print("  2 = 本机原生 NapCat（Windows 懒人免 Docker 方案，或已有正在运行的本机 NapCat）")
    while True:
        raw_mode = _prompt_line(f"  请选择 1 或 2 [默认: {default_choice}]: ").strip()
        choice = raw_mode or default_choice
        if choice in ("1", "docker", "Docker"):
            napcat_mode = "docker"
            break
        if choice in ("2", "native", "Native"):
            napcat_mode = "native"
            break
        print("  × 请输入 1 或 2。")
    print("")

    # Step 5: Detect agy CLI
    print("【第 5 步 / 共 5 步】自动检测 Antigravity CLI (agy)")
    agy_bin, agy_found = _detect_agy_binary(
        explicit=args.agy_bin or "",
        env_agy=existing_env.get("AGY_BIN", ""),
    )
    if agy_found:
        ver = _probe_agy_version(agy_bin)
        ver_text = f" ({ver})" if ver else ""
        print(f"  ✓ 已检测到 agy：{agy_bin}{ver_text}")
    else:
        print(f"  ! 暂未自动检测到 agy 可执行文件（默认将先写入：{agy_bin}）。")
        print("    安装提示：请先安装 Antigravity CLI 并完成 Google 账号登录；")
        print("    也可以现在继续完成配置，稍后安装完 agy 再在 bot.env 或重新运行向导更新 AGY_BIN。")
        custom_agy = _prompt_line("  如果 agy 已安装在自定义路径，可在此粘贴（直接回车跳过）: ").strip()
        if custom_agy:
            agy_bin, agy_found = _detect_agy_binary(explicit=custom_agy)
            if agy_found:
                print(f"  ✓ 已验证自定义路径：{agy_bin}")
            else:
                agy_bin = os.path.expanduser(custom_agy)
                print(f"  ! 未在该路径检测到可执行文件，仍按输入写入：{agy_bin}")
    print("")

    return {
        "bot_qq": bot_qq,
        "admin_qqs": admin_qqs,
        "bot_name": bot_name,
        "napcat_mode": napcat_mode,
        "agy_bin": agy_bin,
        "agy_found": agy_found,
    }


def _noninteractive_collect(project_dir, args, existing_env):
    raw_bot_qq = (args.bot_qq or existing_env.get("BOT_QQ_ID") or "").strip()
    bot_qq = _validate_single_qq(raw_bot_qq)
    if not bot_qq:
        raise ValueError("非交互模式下必须通过 --bot-qq 提供有效的 5~15 位纯数字机器人 QQ 号")

    raw_admins = (args.admin_qq or existing_env.get("ADMIN_QQ_IDS") or "").strip()
    admin_qqs, bad_admins = _parse_admin_qqs(raw_admins)
    if not admin_qqs or bad_admins:
        detail = f"（无效项：{','.join(bad_admins)}）" if bad_admins else ""
        raise ValueError(f"非交互模式下必须通过 --admin-qq 提供有效的管理员 QQ 号{detail}")

    existing_calls = [
        x.strip() for x in (existing_env.get("BOT_CALL_NAMES") or "").split(",") if x.strip()
    ]
    bot_name = (
        args.bot_name
        or (existing_calls[0] if existing_calls else DEFAULT_BOT_NAME)
        or DEFAULT_BOT_NAME
    ).strip()

    napcat_mode = args.napcat_mode or _default_napcat_mode(project_dir, bot_qq)
    agy_bin, agy_found = _detect_agy_binary(
        explicit=args.agy_bin or "",
        env_agy=existing_env.get("AGY_BIN", ""),
    )
    if args.agy_bin and not agy_found:
        agy_bin = os.path.expanduser(args.agy_bin.strip())

    return {
        "bot_qq": bot_qq,
        "admin_qqs": admin_qqs,
        "bot_name": bot_name,
        "napcat_mode": napcat_mode,
        "agy_bin": agy_bin,
        "agy_found": agy_found,
    }


# ---------------------------------------------------------------------------
# Core generation + summary
# ---------------------------------------------------------------------------

def apply_setup(project_dir, choices, existing_env, reset_tokens=False):
    root = os.path.abspath(project_dir)
    bot_env_path = os.path.join(root, "bot.env")
    config_toml_path = os.path.join(root, "config.toml")
    compose_path = os.path.join(root, "docker-compose.napcat.yml")
    napcat_cfg_dir = os.path.join(root, "napcat", "config")
    onebot_default_path = os.path.join(napcat_cfg_dir, "onebot11.json")
    onebot_qq_path = os.path.join(napcat_cfg_dir, f"onebot11_{choices['bot_qq']}.json")
    webui_json_path = os.path.join(napcat_cfg_dir, "webui.json")

    # 1. Create required directories
    req_dirs = [
        os.path.join(root, "data", "run"),
        os.path.join(root, "data", "user_memories"),
        os.path.join(root, "data", "memory_extractor_cwd"),
        os.path.join(root, "data", "sessions"),
        os.path.join(root, "napcat", "config"),
        os.path.join(root, "napcat", "QQ"),
        os.path.join(root, "workspace"),
        os.path.join(root, "chat-sandbox"),
    ]
    for d in req_dirs:
        os.makedirs(d, exist_ok=True)

    # 2. Resolve / generate cryptographically strong tokens
    existing_napcat_token = (existing_env.get("NAPCAT_TOKEN") or "").strip()
    if reset_tokens or _is_placeholder_token(existing_napcat_token):
        napcat_token = secrets.token_hex(16)  # 32 hex chars
        napcat_token_state = "新生成"
    else:
        napcat_token = existing_napcat_token
        napcat_token_state = "已沿用"

    existing_mgmt_token = _read_existing_mgmt_token(config_toml_path)
    if reset_tokens or _is_placeholder_token(existing_mgmt_token):
        management_token = secrets.token_hex(16)  # 32 hex chars
        mgmt_token_state = "新生成"
    else:
        management_token = existing_mgmt_token
        mgmt_token_state = "已沿用"

    existing_webui_json = _read_existing_json(webui_json_path)
    existing_webui_token = (
        str(existing_webui_json.get("token") or "").strip()
        if isinstance(existing_webui_json, dict)
        else ""
    )
    if reset_tokens or existing_webui_json is None or _is_placeholder_token(existing_webui_token):
        napcat_webui_token = secrets.token_hex(8)  # 16 hex chars
        webui_token_state = "新生成"
    else:
        napcat_webui_token = existing_webui_token
        webui_token_state = "已沿用"

    # 3. Build BOT_CALL_NAMES
    call_names = _build_call_names(
        choices["bot_name"],
        existing_call_names_csv=existing_env.get("BOT_CALL_NAMES", ""),
    )

    # 4. Write bot.env (chmod 0600 on POSIX)
    bot_env_content = render_bot_env(
        project_dir=root,
        bot_qq=choices["bot_qq"],
        admin_qqs=choices["admin_qqs"],
        napcat_token=napcat_token,
        call_names=call_names,
        agy_bin=choices["agy_bin"],
        existing_env=existing_env,
    )
    _atomic_write_text(bot_env_path, bot_env_content, mode=0o600)

    # 5. Write config.toml (preserve user customizations when file already exists)
    if os.path.isfile(config_toml_path):
        try:
            with open(config_toml_path, "r", encoding="utf-8-sig") as f:
                existing_toml_text = f.read()
        except OSError:
            existing_toml_text = ""
        config_toml_content = patch_existing_config_toml(
            existing_toml_text,
            project_dir=root,
            admin_qqs=choices["admin_qqs"],
            napcat_token=napcat_token,
            management_token=management_token,
            existing_env=existing_env,
        )
    else:
        config_toml_content = render_config_toml(
            project_dir=root,
            admin_qqs=choices["admin_qqs"],
            napcat_token=napcat_token,
            management_token=management_token,
            existing_env=existing_env,
        )
    _atomic_write_text(config_toml_path, config_toml_content, mode=0o600)

    # 6. Write docker-compose.napcat.yml
    compose_content = render_docker_compose(choices["bot_qq"])
    _atomic_write_text(compose_path, compose_content)

    # 7. Write napcat/config/onebot11.json & onebot11_<BOT_QQ>.json
    onebot_obj = build_onebot11_config(napcat_token, choices["napcat_mode"])
    onebot_json_str = json.dumps(onebot_obj, ensure_ascii=False, indent=2) + "\n"
    _atomic_write_text(onebot_default_path, onebot_json_str)
    _atomic_write_text(onebot_qq_path, onebot_json_str)

    # 8. Write napcat/config/webui.json
    webui_obj = build_napcat_webui_config(
        napcat_webui_token,
        choices["napcat_mode"],
        existing_webui=existing_webui_json,
    )
    webui_json_str = json.dumps(webui_obj, ensure_ascii=False, indent=4) + "\n"
    _atomic_write_text(webui_json_path, webui_json_str)

    # 9. Sync workspace/GEMINI.md -> chat-sandbox/GEMINI.md if present
    ws_gemini = os.path.join(root, "workspace", "GEMINI.md")
    sb_gemini = os.path.join(root, "chat-sandbox", "GEMINI.md")
    sandbox_synced = False
    if os.path.isfile(ws_gemini):
        _atomic_copy_bytes(ws_gemini, sb_gemini)
        sandbox_synced = True

    webui_host = (os.environ.get("WEBUI_HOST") or existing_env.get("WEBUI_HOST") or "127.0.0.1").strip()
    raw_webui_port = (os.environ.get("WEBUI_PORT") or existing_env.get("WEBUI_PORT") or "8765").strip()
    try:
        webui_port = int(raw_webui_port)
    except ValueError:
        webui_port = 8765
    display_webui_host = "127.0.0.1" if webui_host in ("0.0.0.0", "::", "") else webui_host
    bot_webui_url = f"http://{display_webui_host}:{webui_port}"

    return {
        "project_dir": root,
        "bot_qq": choices["bot_qq"],
        "admin_qqs": choices["admin_qqs"],
        "bot_name": call_names[0],
        "call_names": call_names,
        "napcat_mode": choices["napcat_mode"],
        "agy_bin": choices["agy_bin"],
        "agy_found": choices["agy_found"],
        "napcat_token": napcat_token,
        "napcat_token_state": napcat_token_state,
        "management_token": management_token,
        "mgmt_token_state": mgmt_token_state,
        "napcat_webui_token": napcat_webui_token,
        "webui_token_state": webui_token_state,
        "napcat_webui_port": int(webui_obj.get("port") or 6099),
        "bot_webui_url": bot_webui_url,
        "sandbox_synced": sandbox_synced,
        "written_files": [
            bot_env_path,
            config_toml_path,
            compose_path,
            onebot_default_path,
            onebot_qq_path,
            webui_json_path,
        ],
    }


def _print_summary(result):
    mode_label = (
        "Docker 容器（容器内 0.0.0.0，宿主机仅 127.0.0.1 回环映射）"
        if result["napcat_mode"] == "docker"
        else "本机原生 NapCat（仅监听 127.0.0.1，防止局域网暴露）"
    )
    py_cmd = "python" if os.name == "nt" else "python3"
    napcat_webui_url = f"http://127.0.0.1:{result['napcat_webui_port']}/webui/"
    bot_webui_url = result.get("bot_webui_url") or "http://127.0.0.1:8765"

    print("==========================================================")
    print("  配置生成完成！")
    print("==========================================================")
    print(f"  机器人 QQ      : {result['bot_qq']}")
    print(f"  管理员 QQ      : {', '.join(result['admin_qqs'])}")
    print(f"  机器人称呼     : {result['bot_name']}（点名词：{','.join(result['call_names'])}）")
    print(f"  NapCat 模式    : {mode_label}")
    print(f"  Antigravity    : {result['agy_bin']}" + ("" if result["agy_found"] else "  [暂未检测到可执行文件]"))
    print(f"  沙箱人设同步   : {'已同步 workspace/GEMINI.md -> chat-sandbox/GEMINI.md' if result['sandbox_synced'] else '未发现 workspace/GEMINI.md（后续启动时会自动同步）'}")
    print("----------------------------------------------------------")
    print(f"  NapCat WebUI   : {napcat_webui_url}")
    print(f"  NapCat 登录Token: {result['napcat_webui_token']}  ({result['webui_token_state']})")
    print(f"  OneBot Token   : {result['napcat_token'][:6]}...{result['napcat_token'][-4:]}  ({result['napcat_token_state']}，已四处同步)")
    print(f"  机器人 WebUI   : {bot_webui_url}")
    print("----------------------------------------------------------")
    print("  已写入配置文件：")
    for fpath in result["written_files"]:
        rel = os.path.relpath(fpath, result["project_dir"])
        print(f"    - {rel}")
    print("----------------------------------------------------------")
    print("  下一步操作：")
    if result["napcat_mode"] == "docker":
        print(f"    1. 启动 NapCat 容器：{py_cmd} bot_runner.py napcat up")
        print(f"    2. 浏览器打开 {napcat_webui_url}，输入上方登录 Token 并扫码登录 QQ {result['bot_qq']}")
    else:
        print(f"    1. 将 napcat/config/ 下生成的配置文件导入本机原生 NapCat 并启动登录 QQ {result['bot_qq']}")
        print(f"    2. 确认本机 NapCat 的 HTTP(3000) 与正向 WS(3001) 已启用且 Token 一致")
    print(f"    3. 执行自检确认环境就绪：{py_cmd} bot_runner.py doctor")
    print(f"    4. 启动机器人：{py_cmd} bot_runner.py start-bg  （或前台：{py_cmd} bot_runner.py start）")
    print(f"    5. 打开可视化管理面板：{py_cmd} bot_runner.py webui open")
    if not result["agy_found"]:
        print("    ! 注意：启动前请确认已安装 Antigravity CLI (agy) 并完成登录。")
    print("==========================================================")


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

def _build_parser():
    parser = argparse.ArgumentParser(
        prog="setup_wizard.py",
        description="QQ 机器人交互式 / 非交互式配置向导（macOS / Linux / Windows）",
    )
    parser.add_argument(
        "--project-dir",
        default=_SCRIPT_DIR,
        help="目标项目根目录（默认为 setup_wizard.py 所在目录）",
    )
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="非交互模式（适合脚本与自动化测试，需提供 --bot-qq 和 --admin-qq）",
    )
    parser.add_argument("--bot-qq", default="", help="机器人 QQ 号（5~15 位纯数字）")
    parser.add_argument("--admin-qq", default="", help="管理员 QQ 号，多个用英文逗号分隔")
    parser.add_argument("--bot-name", default="", help=f"机器人名字（默认：{DEFAULT_BOT_NAME}）")
    parser.add_argument(
        "--napcat-mode",
        choices=["docker", "native"],
        default="",
        help="NapCat 运行模式：docker 或 native",
    )
    parser.add_argument("--agy-bin", default="", help="显式指定 agy 可执行文件路径")
    parser.add_argument(
        "--reset-tokens",
        action="store_true",
        help="强制重新生成 NAPCAT_TOKEN、MANAGEMENT_TOKEN 与 NapCat WebUI Token",
    )
    return parser


def main(argv=None):
    parser = _build_parser()
    args = parser.parse_args(argv)
    project_dir = os.path.abspath(os.path.expanduser(args.project_dir))
    os.makedirs(project_dir, exist_ok=True)

    existing_env = _parse_existing_bot_env(os.path.join(project_dir, "bot.env"))

    try:
        if args.non_interactive:
            choices = _noninteractive_collect(project_dir, args, existing_env)
        else:
            choices = _interactive_collect(project_dir, args, existing_env)
    except KeyboardInterrupt:
        print("\n已取消配置，未修改任何文件。", file=sys.stderr)
        return 130
    except EOFError:
        print("\n标准输入已结束 (EOF)，已取消配置，未修改任何文件。", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"参数校验失败：{exc}", file=sys.stderr)
        return 2

    result = apply_setup(
        project_dir=project_dir,
        choices=choices,
        existing_env=existing_env,
        reset_tokens=bool(args.reset_tokens),
    )
    _print_summary(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
