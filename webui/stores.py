"""File-backed stores for QQ Bot「哈基米」Web UI (Python 3.9 stdlib only).

Covers:
- Config store (`bot.env`, `config.toml`, consistency checks, atomic backups, sync)
- Persona store (`workspace/GEMINI.md`, `chat-sandbox/GEMINI.md`, `docs/CHARACTER_CARD.md`, backups & unified diffs)
- User memory store (`data/memory.db` SQLite WAL + FTS5 v2 engine, metadata-preserving edits, sensitive-content guard, prompt previews, group memory, conflicts, recall traces, maintenance)
- Repeat-chain tester (reads live `bot.env` settings & applies `repeat_chain.py` rules)
- Workspace & backup read-only file browser (realpath-confined)
"""

import datetime
import difflib
import hashlib
import inspect
import json
import math
import os
import re
import shutil
import sys
import tempfile
import threading
import time
from typing import Any, Dict, Tuple

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

import bot_env  # noqa: E402,F401
import bot_permissions  # noqa: E402
import bot_plugins  # noqa: E402
from file_lock import atomic_replace, file_lock  # noqa: E402
import repeat_chain  # noqa: E402
import user_memory  # noqa: E402

from .config_schema import (  # noqa: E402,F401
    BUILTIN_PRESETS,
    CONFIG_SCHEMA,
    NotFoundError,
    SensitiveMemoryError,
    StoreError,
    _BUILTIN_PRESET_IDS,
    _ENV_DEFAULTS,
    _MEMORY_MODEL_OPTIONS,
    _SCHEMA_BY_KEY,
    _THOUGHT_OPTIONS,
    _WORKBENCH_TOML_SPEC,
)

BOT_ENV_PATH = os.path.join(PROJECT_DIR, "bot.env")
CONFIG_TOML_PATH = os.path.join(PROJECT_DIR, "config.toml")
WORKSPACE_DIR = os.path.join(PROJECT_DIR, "workspace")
SANDBOX_DIR = os.path.join(PROJECT_DIR, "chat-sandbox")
DOCS_DIR = os.path.join(PROJECT_DIR, "docs")
BACKUP_DIR = os.path.join(PROJECT_DIR, "backup")
DATA_DIR = os.path.join(PROJECT_DIR, "data")

GEMINI_PATH = os.path.join(WORKSPACE_DIR, "GEMINI.md")
SANDBOX_GEMINI_PATH = os.path.join(SANDBOX_DIR, "GEMINI.md")
CHARACTER_CARD_PATH = os.path.join(DOCS_DIR, "CHARACTER_CARD.md")
WEBUI_PRESETS_PATH = os.path.join(DATA_DIR, "webui_presets.json")
_DEFAULT_NAPCAT_CONFIG_DIR = (
    os.environ.get("NAPCAT_CONFIG_DIR", "").strip()
    or os.path.join(PROJECT_DIR, "napcat", "config")
)
NAPCAT_WEBUI_CONFIG_PATH = os.path.join(os.path.expanduser(_DEFAULT_NAPCAT_CONFIG_DIR), "webui.json")

MAX_PREVIEW_BYTES = 200 * 1024  # 200 KB

_ENV_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_PRESET_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,48}$")
_SECRET_KEY_RE = re.compile(r"(?:TOKEN|SECRET|PASSWORD|API_KEY)", re.IGNORECASE)
_presets_lock = threading.Lock()

_PREVIEWABLE_EXTS = {
    ".md", ".txt", ".py", ".json", ".toml", ".env", ".sh", ".command",
    ".bat", ".cmd", ".ps1",
    ".yml", ".yaml", ".log", ".patch", ".js", ".css", ".html", ".svg",
    ".csv", ".ini", ".cfg",
}

_IGNORED_BROWSER_NAMES = {
    ".ds_store", "thumbs.db", "desktop.ini",
    "__pycache__", ".git", "node_modules", ".cc-connect",
}

_REDACT_ENV_RE = re.compile(
    r"^(\s*(?:export\s+)?[A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|API_KEY)[A-Z0-9_]*\s*=\s*)(.+)$",
    re.IGNORECASE,
)
_REDACT_TOML_RE = re.compile(
    r'^(\s*(?:token|secret|password|api_key)\s*=\s*")([^"]*)(".*)$',
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# General helpers
# ---------------------------------------------------------------------------

def _ts_tag():
    return time.strftime("%Y%m%d-%H%M%S")


def _mtime_iso(path):
    try:
        ts = os.path.getmtime(path)
        return datetime.datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")
    except OSError:
        return ""


def _sha256_text(text):
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _mask_secret(value):
    s = str(value or "").strip()
    if not s:
        return ""
    if len(s) <= 8:
        return "•" * len(s)
    return f"{s[:4]}••••{s[-4:]}"


def _is_secret_env_key(key):
    ks = str(key or "").strip()
    if not ks:
        return False
    if _SECRET_KEY_RE.search(ks):
        return True
    sitem = _SCHEMA_BY_KEY.get(ks)
    return bool(sitem and sitem.get("type") == "secret")


def _mask_secret_env(env):
    if not isinstance(env, dict):
        return {}
    return {
        k: (_mask_secret(v) if _is_secret_env_key(k) else v)
        for k, v in env.items()
    }


def _strip_secret_env(env):
    if not isinstance(env, dict):
        return {}
    return {
        k: v
        for k, v in env.items()
        if not _is_secret_env_key(k)
    }


def _posix_relpath(path, start):
    return os.path.relpath(path, start).replace(os.sep, "/").replace("\\", "/")


def _is_confined(target_path, allowed_root):
    try:
        root_real = os.path.normcase(os.path.realpath(allowed_root))
        target_real = os.path.normcase(os.path.realpath(target_path))
        return os.path.commonpath([root_real, target_real]) == root_real
    except (OSError, ValueError):
        return False


def _atomic_write_text(path, content, mode=None):
    shared = getattr(bot_env, "atomic_write_text", None)
    if callable(shared):
        try:
            sig = inspect.signature(shared)
            if "mode" in sig.parameters:
                return shared(path, content, mode=mode)
            if mode is None:
                return shared(path, content)
        except (TypeError, ValueError):
            pass
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".webui_", suffix=".tmp", dir=parent)
    fd_owned = True
    try:
        if mode is not None and hasattr(os, "fchmod"):
            os.fchmod(fd, mode)
        f = os.fdopen(fd, "w", encoding="utf-8", newline="\n")
        fd_owned = False
        with f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        if mode is not None and os.name != "nt":
            os.chmod(tmp_path, mode)
        atomic_replace(tmp_path, path)
        if mode is not None and os.name != "nt":
            os.chmod(path, mode)
    except Exception:
        if fd_owned:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _norm_id_csv(raw):
    cleaned = re.sub(r'[\[\]"\'\s]', "", str(raw or ""))
    items = sorted({x for x in cleaned.split(",") if x})
    return ",".join(items)


def _split_csv(raw):
    return [x.strip() for x in str(raw or "").split(",") if x.strip()]


def _bool_flag(val, default=False):
    if val is None:
        return default
    return str(val).strip().lower() not in ("", "0", "false", "no", "off")


def _get_memory_db_path(env=None):
    if env is None:
        env = parse_bot_env()
    raw = (env.get("MEMORY_DB_PATH") or "").strip() if isinstance(env, dict) else ""
    if not raw:
        raw = (user_memory.get_db_path() or "").strip()
    if not raw:
        return os.path.join(PROJECT_DIR, "data", "memory.db")
    if raw == ":memory:" or raw.startswith("file:"):
        return raw
    expanded = os.path.expanduser(raw)
    if not os.path.isabs(expanded):
        return os.path.abspath(os.path.join(PROJECT_DIR, expanded))
    return expanded


def _env_with_defaults(env=None):
    base = dict(_ENV_DEFAULTS)
    if env is None:
        env = parse_bot_env()
    base.update(env)
    return base


# ---------------------------------------------------------------------------
# Config Store (bot.env & config.toml)
# ---------------------------------------------------------------------------

def _parse_env_line_strict(raw_line):
    parser = getattr(bot_env, "parse_line", None) or getattr(bot_env, "_parse_line", None)
    if callable(parser):
        return parser(raw_line)
    line = raw_line.strip()
    if not line or line.startswith("#") or "=" not in line:
        return None
    if line.startswith("export "):
        line = line[len("export "):].lstrip()
    key, value = line.split("=", 1)
    key = key.strip()
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1]
    if not key or not key.replace("_", "").isalnum():
        return None
    return key, value


def parse_bot_env(path=None):
    """Parse bot.env fresh from disk, preserving exact values without mutating os.environ."""
    if path is None:
        path = BOT_ENV_PATH
    parser = getattr(bot_env, "parse_file", None) or getattr(bot_env, "parse_bot_env", None)
    if callable(parser):
        try:
            res = parser(path)
            if isinstance(res, dict):
                return res
        except Exception:
            pass
    data = {}
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            for line in f:
                parsed = _parse_env_line_strict(line)
                if parsed:
                    data[parsed[0]] = parsed[1]
    except OSError:
        pass
    return data


def _format_env_value(value):
    s = str(value if value is not None else "")
    if "\n" in s or "\r" in s:
        raise StoreError("配置值不能包含换行符")
    formatter = getattr(bot_env, "format_env_value", None) or getattr(bot_env, "format_value", None)
    if callable(formatter):
        try:
            return formatter(s)
        except ValueError as exc:
            raise StoreError(str(exc))
    if s == "":
        return ""
    needs_quote = any(ch in s for ch in (" ", "\t", "#", '"', "'", "$", "`", ";", "&", "|", "(", ")", "<", ">"))
    if not needs_quote:
        return s
    # Prefer double quotes when no ", $, ` are present; else single quotes if no '
    if '"' not in s and "$" not in s and "`" not in s:
        return f'"{s}"'
    if "'" not in s:
        return f"'{s}'"
    escaped = s.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`")
    return f'"{escaped}"'


def _strip_toml_comment(line):
    in_quote = None
    escaped = False
    for idx, ch in enumerate(line):
        if escaped:
            escaped = False
            continue
        if in_quote == '"' and ch == "\\":
            escaped = True
            continue
        if in_quote:
            if ch == in_quote:
                in_quote = None
            continue
        if ch in ('"', "'"):
            in_quote = ch
        elif ch == "#":
            return line[:idx]
    return line


def _parse_toml_scalar(raw):
    s = _strip_toml_comment(raw).strip()
    if not s:
        return ""
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        if s.startswith('"'):
            try:
                return json.loads(s)
            except Exception:
                return s[1:-1]
        return s[1:-1]
    if s.startswith("[") and s.endswith("]"):
        inner = s[1:-1].strip()
        if not inner:
            return []
        try:
            parsed = json.loads(s)
            if isinstance(parsed, list):
                return parsed
        except Exception:
            pass
        parts = []
        for tok in inner.split(","):
            t = tok.strip()
            if (t.startswith('"') and t.endswith('"')) or (t.startswith("'") and t.endswith("'")):
                parts.append(t[1:-1])
            elif t:
                parts.append(t)
        return parts
    low = s.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    if re.fullmatch(r"-?\d+\.\d+", s):
        return float(s)
    return s


def _parse_config_toml_sections_local(path):
    sections = {}
    current_sec = ""
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            for raw_line in f:
                stripped = raw_line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                if stripped.startswith("[[") and stripped.endswith("]]"):
                    current_sec = stripped  # e.g. "[[projects]]"
                    sections.setdefault(current_sec, {})
                    continue
                if stripped.startswith("[") and stripped.endswith("]"):
                    current_sec = stripped  # e.g. "[management]"
                    sections.setdefault(current_sec, {})
                    continue
                if "=" in stripped:
                    key, val = stripped.split("=", 1)
                    key = key.strip()
                    if key:
                        sections.setdefault(current_sec, {})[key] = _parse_toml_scalar(val)
    except OSError:
        pass
    return sections


def parse_config_toml(path=None):
    """Parse key sections of config.toml fresh from disk (Python 3.9 stdlib)."""
    if path is None:
        path = CONFIG_TOML_PATH
    sections = None
    shared_sections = getattr(bot_env, "parse_toml_sections", None)
    shared_toml = getattr(bot_env, "parse_config_toml", None)
    if callable(shared_sections):
        try:
            res = shared_sections(path)
            if isinstance(res, dict):
                sections = res
        except Exception:
            pass
    elif callable(shared_toml):
        try:
            res = shared_toml(path)
            if isinstance(res, dict):
                if all(
                    k in res
                    for k in (
                        "management",
                        "display",
                        "auto_compress",
                        "project",
                        "admin_role_user_ids",
                        "platform_options",
                    )
                ):
                    return res
                if any(str(k).startswith("[") for k in res.keys()):
                    sections = res
        except Exception:
            pass
    if sections is None:
        sections = _parse_config_toml_sections_local(path)

    mgmt = sections.get("[management]", {})
    proj = sections.get("[[projects]]", {})
    admin_role = sections.get("[projects.users.roles.admin]", {})
    plat_opts = sections.get("[projects.platforms.options]", {})
    auto_comp = sections.get("[projects.auto_compress]", {})
    disp = sections.get("[display]", {})

    user_ids = admin_role.get("user_ids", [])
    if isinstance(user_ids, str):
        user_ids = _split_csv(user_ids)
    elif not isinstance(user_ids, list):
        user_ids = []

    return {
        "management": {
            "enabled": bool(mgmt.get("enabled", True)),
            "port": int(mgmt.get("port", 9820) or 9820),
            "token": str(mgmt.get("token", "") or ""),
        },
        "display": {
            "mode": str(disp.get("mode", "compact") or "compact"),
            "thinking_messages": bool(disp.get("thinking_messages", False)),
            "tool_messages": bool(disp.get("tool_messages", False)),
            "reply_footer": bool(disp.get("reply_footer", False)),
        },
        "auto_compress": {
            "enabled": bool(auto_comp.get("enabled", True)),
            "max_tokens": auto_comp.get("max_tokens", 120000),
            "min_gap_mins": auto_comp.get("min_gap_mins"),
            "hard_max_tokens": auto_comp.get("hard_max_tokens"),
            "hard_min_gap_mins": auto_comp.get("hard_min_gap_mins"),
        },
        "project": {
            "name": str(proj.get("name", "qq-gemini-acp") or "qq-gemini-acp"),
            "admin_from": str(proj.get("admin_from", "") or ""),
            "agent_session_idle_timeout_mins": proj.get("agent_session_idle_timeout_mins"),
            "reset_on_idle_mins": proj.get("reset_on_idle_mins"),
            "auto_compress_max_tokens": auto_comp.get("max_tokens"),
        },
        "admin_role_user_ids": [str(x) for x in user_ids],
        "platform_options": {
            "token": str(plat_opts.get("token", "") or ""),
            "ws_url": str(plat_opts.get("ws_url", "") or ""),
            "http_url": str(plat_opts.get("http_url", "") or ""),
            "allow_from": str(plat_opts.get("allow_from", "") or ""),
            "require_mention": plat_opts.get("require_mention"),
        },
    }


def read_workbench_toml(path=None):
    """Read the 9 workbench-controlled config.toml keys with safe typed defaults."""
    if path is None:
        path = CONFIG_TOML_PATH
    toml = parse_config_toml(path)
    plat = toml.get("platform_options") or {}
    proj = toml.get("project") or {}
    comp = toml.get("auto_compress") or {}
    disp = toml.get("display") or {}

    def _safe_int(val, default):
        try:
            if val is None or isinstance(val, bool):
                return default
            return int(val)
        except (ValueError, TypeError):
            return default

    return {
        "require_mention": bool(plat.get("require_mention")) if plat.get("require_mention") is not None else False,
        "agent_session_idle_timeout_mins": _safe_int(proj.get("agent_session_idle_timeout_mins"), 30),
        "reset_on_idle_mins": _safe_int(proj.get("reset_on_idle_mins"), 360),
        "auto_compress_enabled": bool(comp.get("enabled", True)),
        "auto_compress_max_tokens": _safe_int(comp.get("max_tokens"), 120000),
        "auto_compress_min_gap_mins": _safe_int(comp.get("min_gap_mins"), 15),
        "auto_compress_hard_max_tokens": _safe_int(comp.get("hard_max_tokens"), 0),
        "auto_compress_hard_min_gap_mins": _safe_int(comp.get("hard_min_gap_mins"), 3),
        "display_thinking_messages": bool(disp.get("thinking_messages", False)),
        "display_tool_messages": bool(disp.get("tool_messages", False)),
        "display_reply_footer": bool(disp.get("reply_footer", False)),
    }


def _make_config_backup_dir():
    base = f"webui-config-{_ts_tag()}"
    candidate = os.path.join(BACKUP_DIR, base)
    idx = 1
    while os.path.exists(candidate):
        candidate = os.path.join(BACKUP_DIR, f"{base}-{idx}")
        idx += 1
    os.makedirs(candidate, exist_ok=True)
    return candidate


def _backup_file_once(src_path, backup_dir, dest_name):
    if not backup_dir or not os.path.isfile(src_path):
        return
    os.makedirs(backup_dir, exist_ok=True)
    dst = os.path.join(backup_dir, dest_name)
    if not os.path.exists(dst):
        shutil.copy2(src_path, dst)


def check_config_consistency():
    """Mirror start.sh consistency checks between bot.env and config.toml."""
    env = parse_bot_env()
    toml = parse_config_toml()

    bot_qq_id = (env.get("BOT_QQ_ID") or "").strip()
    want_admins = _norm_id_csv(env.get("ADMIN_QQ_IDS", ""))
    cfg_admin_from = _norm_id_csv(toml["project"].get("admin_from", ""))
    cfg_admin_role = _norm_id_csv(",".join(toml.get("admin_role_user_ids") or []))
    env_token = (env.get("NAPCAT_TOKEN") or "").strip()
    cfg_token = (toml["platform_options"].get("token") or "").strip()

    checks = []
    problems = []

    def _add(name, ok, detail, problem_msg=None):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        if not ok:
            problems.append(problem_msg or detail)

    _add(
        "BOT_QQ_ID 非空",
        bool(bot_qq_id),
        f"当前值：{bot_qq_id}" if bot_qq_id else "bot.env 里 BOT_QQ_ID 为空",
        "bot.env 里 BOT_QQ_ID 为空",
    )
    _add(
        "ADMIN_QQ_IDS 非空",
        bool(want_admins),
        f"当前值：{want_admins}" if want_admins else "bot.env 里 ADMIN_QQ_IDS 为空",
        "bot.env 里 ADMIN_QQ_IDS 为空",
    )
    _add(
        "admin_from 一致性",
        cfg_admin_from == want_admins and bool(want_admins),
        f"config.toml=[{cfg_admin_from}] vs bot.env=[{want_admins}]",
        f"config.toml admin_from=[{cfg_admin_from}] 与 bot.env ADMIN_QQ_IDS=[{want_admins}] 不一致",
    )
    _add(
        "roles.admin.user_ids 一致性",
        cfg_admin_role == want_admins and bool(want_admins),
        f"config.toml=[{cfg_admin_role}] vs bot.env=[{want_admins}]",
        f"config.toml users.roles.admin.user_ids=[{cfg_admin_role}] 与 bot.env ADMIN_QQ_IDS=[{want_admins}] 不一致",
    )
    _add(
        "NAPCAT_TOKEN 一致性",
        bool(env_token) and cfg_token == env_token,
        f"config.toml={_mask_secret(cfg_token) or '(空)'} vs bot.env={_mask_secret(env_token) or '(空)'}",
        "config.toml platforms.options.token 与 bot.env NAPCAT_TOKEN 不一致",
    )

    return {
        "valid": len(problems) == 0,
        "problems": problems,
        "checks": checks,
    }


def _rewrite_local_ws_url_port(raw_ws_url, new_port):
    """Replace port in a local ws(s)://127.0.0.1|localhost|[::1] URL; return None for non-local URLs."""
    port_s = str(new_port or "").strip()
    if not port_s.isdigit():
        return None
    port_num = int(port_s)
    if not (1 <= port_num <= 65535):
        return None
    cur = str(raw_ws_url or "").strip()
    if not cur:
        return f"ws://127.0.0.1:{port_num}"
    m = re.match(r"^(wss?://(?:127\.0\.0\.1|localhost|\[::1\])):(\d+)(/.*)?$", cur, re.IGNORECASE)
    if m:
        return f"{m.group(1)}:{port_num}{m.group(3) or ''}"
    m2 = re.match(r"^(wss?://(?:127\.0\.0\.1|localhost|\[::1\]))(/.*)?$", cur, re.IGNORECASE)
    if m2:
        return f"{m2.group(1)}:{port_num}{m2.group(2) or ''}"
    return None


def _rewrite_config_toml_fields(
    admin_ids_list=None,
    napcat_token=None,
    backup_dir=None,
    http_url=None,
    guard_listen_port=None,
):
    """Surgically update synced fields in config.toml (admin_from, user_ids, token, http_url, ws_url)."""
    if not os.path.isfile(CONFIG_TOML_PATH):
        raise NotFoundError("找不到 config.toml")

    with open(CONFIG_TOML_PATH, "r", encoding="utf-8") as f:
        lines = f.readlines()

    _backup_file_once(CONFIG_TOML_PATH, backup_dir, "config.toml")

    sync_admins = admin_ids_list is not None
    sync_token = napcat_token is not None
    http_url_clean = str(http_url or "").strip() if http_url is not None else ""
    sync_http_url = bool(http_url_clean)
    sync_ws_port = guard_listen_port is not None and str(guard_listen_port).strip().isdigit()

    admin_csv = ",".join(admin_ids_list or [])
    admin_arr = "[" + ", ".join(json.dumps(x, ensure_ascii=False) for x in (admin_ids_list or [])) + "]"
    token_quoted = json.dumps(str(napcat_token or ""), ensure_ascii=False)
    admin_from_quoted = json.dumps(admin_csv, ensure_ascii=False)
    http_url_quoted = json.dumps(http_url_clean, ensure_ascii=False)

    current_sec = ""
    out_lines = []
    updated = {
        "admin_from": False,
        "user_ids": False,
        "token": False,
        "http_url": False,
        "ws_url": False,
    }

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[[") and stripped.endswith("]]"):
            current_sec = stripped
            out_lines.append(line)
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            current_sec = stripped
            out_lines.append(line)
            continue

        if sync_admins and current_sec == "[[projects]]" and re.match(r"^\s*admin_from\s*=", line):
            out_lines.append(f"admin_from = {admin_from_quoted}\n")
            updated["admin_from"] = True
        elif sync_admins and current_sec == "[projects.users.roles.admin]" and re.match(r"^\s*user_ids\s*=", line):
            out_lines.append(f"user_ids = {admin_arr}\n")
            updated["user_ids"] = True
        elif sync_token and current_sec == "[projects.platforms.options]" and re.match(r"^\s*token\s*=", line):
            out_lines.append(f"token = {token_quoted}\n")
            updated["token"] = True
        elif sync_http_url and current_sec == "[projects.platforms.options]" and re.match(r"^\s*http_url\s*=", line):
            out_lines.append(f"http_url = {http_url_quoted}\n")
            updated["http_url"] = True
        elif sync_ws_port and current_sec == "[projects.platforms.options]" and re.match(r"^\s*ws_url\s*=", line):
            rhs = line.split("=", 1)[1]
            cur_ws_url = _parse_toml_scalar(rhs)
            new_ws_url = _rewrite_local_ws_url_port(cur_ws_url, guard_listen_port)
            if new_ws_url:
                out_lines.append(f"ws_url = {json.dumps(new_ws_url, ensure_ascii=False)}\n")
                updated["ws_url"] = True
            else:
                out_lines.append(line)
        else:
            out_lines.append(line)

    _atomic_write_text(CONFIG_TOML_PATH, "".join(out_lines))
    return updated


def _coerce_workbench_toml_value(logical_key, raw_val):
    spec = _WORKBENCH_TOML_SPEC.get(logical_key)
    if not spec:
        raise StoreError(f"不支持的 config.toml 配置项：{logical_key}")

    if spec["type"] == "bool":
        if isinstance(raw_val, bool):
            return raw_val
        if isinstance(raw_val, int) and raw_val in (0, 1):
            return bool(raw_val)
        if isinstance(raw_val, str):
            low = raw_val.strip().lower()
            if low in ("true", "1", "yes", "on"):
                return True
            if low in ("false", "0", "no", "off"):
                return False
        raise StoreError(f"{spec['label']} 必须是布尔值")

    if spec["type"] == "int":
        if isinstance(raw_val, bool):
            raise StoreError(f"{spec['label']} 必须是整数")
        try:
            if isinstance(raw_val, float):
                if not raw_val.is_integer():
                    raise ValueError("not integer")
                num = int(raw_val)
            else:
                s = str(raw_val).strip()
                fnum = float(s)
                if not fnum.is_integer():
                    raise ValueError("not integer")
                num = int(fnum)
        except (ValueError, TypeError):
            raise StoreError(f"{spec['label']} 必须是整数")
        if "min" in spec and num < spec["min"]:
            raise StoreError(f"{spec['label']} 不能小于 {spec['min']}")
        if "max" in spec and num > spec["max"]:
            raise StoreError(f"{spec['label']} 不能大于 {spec['max']}")
        return num

    return str(raw_val)


def _validate_workbench_toml_updates(toml_updates, current_toml=None):
    if toml_updates is None:
        return {}, []
    if not isinstance(toml_updates, dict):
        raise StoreError("toml_updates 必须是键值对象")
    if current_toml is None:
        current_toml = read_workbench_toml()
    normalized = {}
    for raw_key, raw_val in toml_updates.items():
        key = str(raw_key or "").strip()
        if not key:
            continue
        normalized[key] = _coerce_workbench_toml_value(key, raw_val)
    changed = [k for k, v in normalized.items() if current_toml.get(k) != v]
    return normalized, changed


def _rewrite_workbench_toml_fields(updates_map, backup_dir=None, path=None):
    """Surgically update workbench-managed config.toml fields preserving comments & formatting."""
    if path is None:
        path = CONFIG_TOML_PATH
    if not updates_map:
        return []
    if not os.path.isfile(path):
        raise NotFoundError("找不到 config.toml")

    with open(path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    _backup_file_once(path, backup_dir, "config.toml")

    remaining_by_sec = {}
    for logical_key, val in updates_map.items():
        spec = _WORKBENCH_TOML_SPEC[logical_key]
        formatted = ("true" if val else "false") if spec["type"] == "bool" else str(int(val))
        remaining_by_sec.setdefault(spec["section"], {})[spec["key"]] = (logical_key, formatted)

    out_lines = []
    current_sec = ""
    last_content_idx = 0
    seen_sections = set()
    replaced_keys = []

    def _flush_missing_for_section(sec_name):
        nonlocal last_content_idx
        rem = remaining_by_sec.pop(sec_name, None)
        if not rem:
            return
        insert_lines = []
        for toml_key, (logical_key, formatted) in rem.items():
            insert_lines.append(f"{toml_key} = {formatted}\n")
            replaced_keys.append(logical_key)
        if insert_lines:
            if last_content_idx > 0 and not out_lines[last_content_idx - 1].endswith("\n"):
                out_lines[last_content_idx - 1] += "\n"
            out_lines[last_content_idx:last_content_idx] = insert_lines

    for line in lines:
        stripped = line.strip()
        if (stripped.startswith("[[") and stripped.endswith("]]")) or (
            stripped.startswith("[") and stripped.endswith("]")
        ):
            if current_sec:
                _flush_missing_for_section(current_sec)
            current_sec = stripped
            seen_sections.add(current_sec)
            out_lines.append(line)
            last_content_idx = len(out_lines)
            continue

        sec_rem = remaining_by_sec.get(current_sec)
        matched_key = None
        if sec_rem and stripped and not stripped.startswith("#") and "=" in stripped:
            for toml_key in list(sec_rem.keys()):
                m = re.match(
                    r"^(\s*)" + re.escape(toml_key) + r"(\s*=\s*)([^#\r\n]*?)(?:([ \t]*#[^\r\n]*))?(\r?\n)?$",
                    line,
                )
                if m:
                    logical_key, formatted = sec_rem.pop(toml_key)
                    indent, eq_part, inline_comment, nl = m.group(1), m.group(2), m.group(4) or "", m.group(5) or "\n"
                    out_lines.append(f"{indent}{toml_key}{eq_part}{formatted}{inline_comment}{nl}")
                    replaced_keys.append(logical_key)
                    matched_key = toml_key
                    last_content_idx = len(out_lines)
                    break

        if matched_key is None:
            out_lines.append(line)
            if stripped and not stripped.startswith("#"):
                last_content_idx = len(out_lines)

    if current_sec:
        _flush_missing_for_section(current_sec)

    # Append any sections that did not exist at all in config.toml
    for sec_name, rem in list(remaining_by_sec.items()):
        if not rem:
            continue
        if out_lines and not out_lines[-1].endswith("\n"):
            out_lines[-1] += "\n"
        if out_lines and out_lines[-1].strip():
            out_lines.append("\n")
        out_lines.append(f"{sec_name}\n")
        for toml_key, (logical_key, formatted) in rem.items():
            out_lines.append(f"{toml_key} = {formatted}\n")
            replaced_keys.append(logical_key)

    _atomic_write_text(path, "".join(out_lines))
    return replaced_keys


def get_config():
    """Return full config payload for GET /api/config."""
    raw_env = parse_bot_env()
    env = _env_with_defaults(raw_env)
    toml = parse_config_toml()
    wb_toml = read_workbench_toml()
    consistency = check_config_consistency()

    env_raw_text, _ = _read_text_optional(BOT_ENV_PATH)
    toml_raw_text, _ = _read_text_optional(CONFIG_TOML_PATH)
    env_raw_safe, _ = _redact_preview_secrets("bot.env", env_raw_text)
    toml_raw_safe, _ = _redact_preview_secrets("config.toml", toml_raw_text)

    # Include any extra keys in bot.env not yet listed in CONFIG_SCHEMA
    known_keys = set(_SCHEMA_BY_KEY.keys())
    extra_keys = [k for k in raw_env.keys() if k not in known_keys]

    file_mode = ""
    try:
        file_mode = oct(os.stat(BOT_ENV_PATH).st_mode & 0o777)
    except OSError:
        pass

    plugin_cfg = bot_plugins.load_config(force=True)
    plugin_flags = {
        p.id: {
            "enabled": bot_plugins.is_enabled(p.id, config=plugin_cfg),
            "name": p.name,
        }
        for p in bot_plugins.list_plugins()
    }

    return {
        "env": _mask_secret_env(env),
        "env_raw": env_raw_safe,
        "toml_raw": toml_raw_safe,
        "schema": CONFIG_SCHEMA,
        "extra_keys": extra_keys,
        "toml": {
            "management_enabled": toml["management"]["enabled"],
            "management_port": toml["management"]["port"],
            "management_token_masked": _mask_secret(toml["management"]["token"]),
            "project_name": toml["project"]["name"],
            "admin_from": toml["project"]["admin_from"],
            "admin_role_user_ids": toml["admin_role_user_ids"],
            "napcat_token_masked": _mask_secret(toml["platform_options"]["token"]),
            "ws_url": toml["platform_options"]["ws_url"],
            "http_url": toml["platform_options"]["http_url"],
            "require_mention": wb_toml["require_mention"],
            "agent_session_idle_timeout_mins": wb_toml["agent_session_idle_timeout_mins"],
            "reset_on_idle_mins": wb_toml["reset_on_idle_mins"],
            "auto_compress_enabled": wb_toml["auto_compress_enabled"],
            "auto_compress_max_tokens": wb_toml["auto_compress_max_tokens"],
            "auto_compress_min_gap_mins": wb_toml["auto_compress_min_gap_mins"],
            "auto_compress_hard_max_tokens": wb_toml["auto_compress_hard_max_tokens"],
            "auto_compress_hard_min_gap_mins": wb_toml["auto_compress_hard_min_gap_mins"],
            "display_thinking_messages": wb_toml["display_thinking_messages"],
            "display_tool_messages": wb_toml["display_tool_messages"],
            "display_reply_footer": wb_toml["display_reply_footer"],
        },
        "consistency": consistency,
        "plugin_flags": plugin_flags,
        "bot_env_mode": file_mode,
        "bot_env_mtime": _mtime_iso(BOT_ENV_PATH),
        "config_toml_mtime": _mtime_iso(CONFIG_TOML_PATH),
    }


def _env_values_equal(cur_val, new_val, schema_item=None):
    cur_s = str(cur_val if cur_val is not None else "").strip()
    new_s = str(new_val if new_val is not None else "").strip()
    if schema_item:
        stype = schema_item.get("type")
        if stype == "boolean":
            return _bool_flag(cur_s) == _bool_flag(new_s)
        if stype == "number" and cur_s != "" and new_s != "":
            try:
                return float(cur_s) == float(new_s)
            except ValueError:
                pass
    return cur_s == new_s


_WS_GUARD_PORT_KEYS = frozenset({"GUARD_LISTEN_PORT", "GUARD_UDP_PORT"})


def _compute_restart_scope(changed_env_keys, changed_toml_keys=None):
    has_env = bool(changed_env_keys)
    has_toml = bool(changed_toml_keys)
    if not has_env and not has_toml:
        return "none"
    if has_toml:
        return "full"
    if any(k in _WS_GUARD_PORT_KEYS for k in changed_env_keys):
        return "full"
    if all(
        k.startswith("GUARD_") or k.startswith("FORWARD_") or k.startswith("GROUP_WHITELIST_")
        for k in changed_env_keys
    ):
        return "ws-guard"
    return "full"


def _validate_env_updates(updates, current_env=None):
    if updates is None:
        return {}, []
    if not isinstance(updates, dict):
        raise StoreError("updates 必须是键值对象")

    if current_env is None:
        current_env = parse_bot_env()
    effective_env = _env_with_defaults(current_env)
    normalized_updates = {}

    for raw_key, raw_val in updates.items():
        key = str(raw_key or "").strip()
        if not _ENV_KEY_RE.match(key):
            raise StoreError(f"非法配置键名：{key}")
        if isinstance(raw_val, bool):
            val = "1" if raw_val else "0"
        elif isinstance(raw_val, float) and raw_val.is_integer():
            val = str(int(raw_val))
        elif isinstance(raw_val, list):
            val = ",".join(str(x).strip() for x in raw_val if str(x).strip())
        elif raw_val is None:
            val = ""
        else:
            val = str(raw_val).strip()

        if "\n" in val or "\r" in val:
            raise StoreError(f"配置项 {key} 不允许包含换行符")

        if _is_secret_env_key(key) and (
            "••••" in val or "****" in val or (bool(val) and set(val) == {"•"})
        ):
            # Never overwrite a secret with a masked placeholder
            continue

        schema_item = _SCHEMA_BY_KEY.get(key)
        if schema_item:
            if key in _WS_GUARD_PORT_KEYS:
                if re.fullmatch(r"[0-9]+", val) is None:
                    raise StoreError(f"{schema_item['label']}（{key}）必须是整数端口")
                val = val.lstrip("0") or "0"
            if schema_item["type"] == "number" and val != "":
                try:
                    num = float(val)
                except ValueError:
                    raise StoreError(f"{schema_item['label']}（{key}）必须是数字")
                if not math.isfinite(num):
                    raise StoreError(f"{schema_item['label']}（{key}）必须是有限数字")
                if "min" in schema_item and num < schema_item["min"]:
                    raise StoreError(f"{schema_item['label']}（{key}）不能小于 {schema_item['min']}")
                if "max" in schema_item and num > schema_item["max"]:
                    raise StoreError(f"{schema_item['label']}（{key}）不能大于 {schema_item['max']}")
            elif schema_item["type"] == "select" and val != "":
                opts = schema_item.get("options") or []
                if opts and val not in opts:
                    raise StoreError(f"{schema_item['label']}（{key}）必须为 {', '.join(opts)} 之一")
            elif schema_item["type"] == "boolean":
                val = "1" if _bool_flag(val) else "0"

        normalized_updates[key] = val

    changed_keys = [
        k for k, v in normalized_updates.items()
        if not _env_values_equal(
            effective_env.get(k, ""), v, None if k in _WS_GUARD_PORT_KEYS else _SCHEMA_BY_KEY.get(k)
        )
    ]
    return normalized_updates, changed_keys


def _write_bot_env_updates(changed_map, backup_dir=None):
    """Write only changed keys into bot.env preserving comments/order & mode 0o600."""
    if not changed_map:
        return
    _backup_file_once(BOT_ENV_PATH, backup_dir, "bot.env")

    lines = []
    if os.path.isfile(BOT_ENV_PATH):
        with open(BOT_ENV_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()

    remaining = dict(changed_map)
    new_lines = []
    for line in lines:
        parsed = _parse_env_line_strict(line)
        if parsed and parsed[0] in remaining:
            key = parsed[0]
            val = remaining.pop(key)
            prefix = "export " if line.lstrip().startswith("export ") else ""
            new_lines.append(f"{prefix}{key}={_format_env_value(val)}\n")
        else:
            new_lines.append(line)

    if remaining:
        if new_lines and not new_lines[-1].endswith("\n"):
            new_lines[-1] += "\n"
        for key, val in remaining.items():
            new_lines.append(f"{key}={_format_env_value(val)}\n")

    _atomic_write_text(BOT_ENV_PATH, "".join(new_lines), mode=0o600)


def update_bot_env(updates, sync_toml=True):
    """Backup and update bot.env in-place preserving comments/order & mode 0o600."""
    current_env = parse_bot_env()
    normalized_updates, changed_keys = _validate_env_updates(updates, current_env=current_env)

    if not changed_keys:
        consistency = check_config_consistency()
        synced = False
        backup_rel = ""
        if sync_toml and not consistency["valid"]:
            sync_res = sync_env_to_toml()
            synced = True
            backup_rel = sync_res.get("backup_dir", "")
            consistency = sync_res["consistency"]
        return {
            "changed_keys": [],
            "restart_scope": "none",
            "synced_toml": synced,
            "backup_dir": backup_rel,
            "consistency": consistency,
            "env": _mask_secret_env(_env_with_defaults(parse_bot_env())),
        }

    backup_path = _make_config_backup_dir()
    _write_bot_env_updates({k: normalized_updates[k] for k in changed_keys}, backup_dir=backup_path)
    fresh_env = parse_bot_env()
    _sync_live_memory_runtime_config(fresh_env)

    synced_toml = False
    sync_admins_token = "ADMIN_QQ_IDS" in changed_keys or "NAPCAT_TOKEN" in changed_keys
    sync_http_url = "NAPCAT_HTTP_URL" in changed_keys
    sync_ws_port = "GUARD_LISTEN_PORT" in changed_keys
    should_sync = bool(sync_toml) and (sync_admins_token or sync_http_url or sync_ws_port)
    if should_sync and (sync_admins_token or os.path.isfile(CONFIG_TOML_PATH)):
        admins = _split_csv(fresh_env.get("ADMIN_QQ_IDS", "")) if sync_admins_token else None
        token = (fresh_env.get("NAPCAT_TOKEN") or "").strip() if sync_admins_token else None
        http_url = (fresh_env.get("NAPCAT_HTTP_URL") or "").strip() if sync_http_url else None
        guard_port = (fresh_env.get("GUARD_LISTEN_PORT") or "").strip() if sync_ws_port else None
        updated_map = _rewrite_config_toml_fields(
            admin_ids_list=admins,
            napcat_token=token,
            backup_dir=backup_path,
            http_url=http_url,
            guard_listen_port=guard_port,
        )
        synced_toml = bool(sync_admins_token or any(updated_map.values()))

    return {
        "changed_keys": changed_keys,
        "restart_scope": _compute_restart_scope(changed_keys),
        "synced_toml": synced_toml,
        "backup_dir": _posix_relpath(backup_path, PROJECT_DIR),
        "consistency": check_config_consistency(),
        "env": _mask_secret_env(_env_with_defaults(fresh_env)),
    }


def sync_env_to_toml():
    """Explicitly sync ADMIN_QQ_IDS, NAPCAT_TOKEN, NAPCAT_HTTP_URL, and GUARD_LISTEN_PORT from bot.env into config.toml."""
    env = parse_bot_env()
    admins = _split_csv(env.get("ADMIN_QQ_IDS", ""))
    token = (env.get("NAPCAT_TOKEN") or "").strip()
    if not admins:
        raise StoreError("bot.env 中的 ADMIN_QQ_IDS 为空，无法同步")
    if not token:
        raise StoreError("bot.env 中的 NAPCAT_TOKEN 为空，无法同步")
    http_url = (env.get("NAPCAT_HTTP_URL") or "").strip() or None
    guard_port = (env.get("GUARD_LISTEN_PORT") or "").strip() or None

    backup_path = _make_config_backup_dir()
    updated = _rewrite_config_toml_fields(
        admins,
        token,
        backup_dir=backup_path,
        http_url=http_url,
        guard_listen_port=guard_port,
    )

    return {
        "updated": updated,
        "backup_dir": _posix_relpath(backup_path, PROJECT_DIR),
        "consistency": check_config_consistency(),
    }


# ---------------------------------------------------------------------------
# Workbench Presets & Unified Workbench State / Updater
# ---------------------------------------------------------------------------

def _sanitize_custom_preset_record(item):
    if not isinstance(item, dict):
        return None
    pid = str(item.get("id") or "").strip()
    name = str(item.get("name") or "").strip()
    if not pid or not name or pid in _BUILTIN_PRESET_IDS:
        return None
    env_clean = {}
    raw_env = item.get("env")
    if isinstance(raw_env, dict):
        for k, v in raw_env.items():
            ks = str(k or "").strip()
            if not _ENV_KEY_RE.match(ks) or _SECRET_KEY_RE.search(ks):
                continue
            schema_item = _SCHEMA_BY_KEY.get(ks)
            if schema_item and schema_item.get("type") == "secret":
                continue
            if isinstance(v, bool):
                env_clean[ks] = "1" if v else "0"
            elif isinstance(v, float) and v.is_integer():
                env_clean[ks] = str(int(v))
            elif v is not None:
                env_clean[ks] = str(v).strip()
    toml_clean = {}
    raw_toml = item.get("toml")
    if isinstance(raw_toml, dict):
        for k, v in raw_toml.items():
            ks = str(k or "").strip()
            if ks not in _WORKBENCH_TOML_SPEC:
                continue
            try:
                toml_clean[ks] = _coerce_workbench_toml_value(ks, v)
            except StoreError:
                continue
    return {
        "id": pid,
        "name": name[:40],
        "tagline": str(item.get("tagline") or "").strip()[:80],
        "badge": str(item.get("badge") or "自定义").strip()[:12] or "自定义",
        "builtin": False,
        "env": env_clean,
        "toml": toml_clean,
        "created_at": str(item.get("created_at") or ""),
        "updated_at": str(item.get("updated_at") or ""),
    }


def list_custom_presets():
    """Load custom workbench presets from data/webui_presets.json safely."""
    with _presets_lock:
        if not os.path.isfile(WEBUI_PRESETS_PATH):
            return []
        try:
            with open(WEBUI_PRESETS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return []

    raw_list = data.get("presets") if isinstance(data, dict) else (data if isinstance(data, list) else [])
    if not isinstance(raw_list, list):
        return []
    out = []
    seen = set()
    for item in raw_list:
        clean = _sanitize_custom_preset_record(item)
        if clean and clean["id"] not in seen:
            seen.add(clean["id"])
            out.append(clean)
    return out


def _write_custom_presets_unlocked(items):
    payload = {
        "version": 1,
        "updated_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "presets": items,
    }
    _atomic_write_text(WEBUI_PRESETS_PATH, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def save_custom_preset(payload):
    """Create or update a custom workbench preset in data/webui_presets.json."""
    if not isinstance(payload, dict):
        raise StoreError("请求体必须是 JSON 对象")

    name = str(payload.get("name") or "").strip()
    if not name:
        raise StoreError("预设名称不能为空")
    if len(name) > 40:
        raise StoreError("预设名称不能超过 40 字")

    tagline = str(payload.get("tagline") or "").strip()[:80]
    badge = str(payload.get("badge") or "自定义").strip()[:12] or "自定义"

    raw_id = str(payload.get("id") or "").strip()
    if raw_id:
        if not _PRESET_ID_RE.match(raw_id):
            raise StoreError("预设 ID 仅支持字母、数字、下划线和连字符（1~48 位）")
        if raw_id in _BUILTIN_PRESET_IDS:
            raise StoreError("不能修改或覆盖内置场景预设")
        pid = raw_id
    else:
        suffix = hashlib.sha1(f"{name}:{time.time()}".encode("utf-8")).hexdigest()[:6]
        pid = f"custom-{_ts_tag()}-{suffix}"

    raw_env = payload.get("env")
    raw_toml = payload.get("toml")
    if raw_env is None:
        raw_env = {}
    if raw_toml is None:
        raw_toml = {}
    if not isinstance(raw_env, dict):
        raise StoreError("env 必须是键值对象")
    if not isinstance(raw_toml, dict):
        raise StoreError("toml 必须是键值对象")

    # Strip secrets before validating/saving
    safe_env_in = {}
    for k, v in raw_env.items():
        ks = str(k or "").strip()
        if not ks:
            continue
        if _SECRET_KEY_RE.search(ks):
            continue
        sitem = _SCHEMA_BY_KEY.get(ks)
        if sitem and sitem.get("type") == "secret":
            continue
        safe_env_in[ks] = v

    norm_env, _ = _validate_env_updates(safe_env_in)
    norm_toml, _ = _validate_workbench_toml_updates(raw_toml)

    if not norm_env and not norm_toml:
        raise StoreError("自定义预设至少需要包含一项 env 或 config.toml 配置")

    now_iso = datetime.datetime.now().astimezone().isoformat(timespec="seconds")

    with _presets_lock:
        existing = []
        if os.path.isfile(WEBUI_PRESETS_PATH):
            try:
                with open(WEBUI_PRESETS_PATH, "r", encoding="utf-8") as f:
                    raw_file = json.load(f)
                raw_items = raw_file.get("presets") if isinstance(raw_file, dict) else (raw_file if isinstance(raw_file, list) else [])
                for it in raw_items or []:
                    clean = _sanitize_custom_preset_record(it)
                    if clean:
                        existing.append(clean)
            except Exception:
                existing = []

        created_at = now_iso
        updated = False
        new_rec = {
            "id": pid,
            "name": name,
            "tagline": tagline,
            "badge": badge,
            "builtin": False,
            "env": norm_env,
            "toml": norm_toml,
            "created_at": created_at,
            "updated_at": now_iso,
        }
        for idx, it in enumerate(existing):
            if it["id"] == pid:
                new_rec["created_at"] = it.get("created_at") or now_iso
                existing[idx] = new_rec
                updated = True
                break
        if not updated:
            existing.append(new_rec)
        _write_custom_presets_unlocked(existing)

    return {
        "preset": new_rec,
        "state": get_workbench_state(),
    }


def delete_custom_preset(preset_id):
    """Delete a custom workbench preset (builtin presets are protected)."""
    pid = str(preset_id or "").strip()
    if not pid:
        raise StoreError("预设 ID 不能为空")
    if pid in _BUILTIN_PRESET_IDS:
        raise StoreError("内置场景预设不可删除")

    with _presets_lock:
        existing = []
        if os.path.isfile(WEBUI_PRESETS_PATH):
            try:
                with open(WEBUI_PRESETS_PATH, "r", encoding="utf-8") as f:
                    raw_file = json.load(f)
                raw_items = raw_file.get("presets") if isinstance(raw_file, dict) else (raw_file if isinstance(raw_file, list) else [])
                for it in raw_items or []:
                    clean = _sanitize_custom_preset_record(it)
                    if clean:
                        existing.append(clean)
            except Exception:
                existing = []

        kept = [it for it in existing if it["id"] != pid]
        if len(kept) == len(existing):
            raise NotFoundError(f"找不到自定义预设：{pid}")
        _write_custom_presets_unlocked(kept)

    return {
        "deleted": True,
        "id": pid,
        "state": get_workbench_state(),
    }


def _detect_active_preset_id(env_map, toml_map, presets):
    matches = []
    for idx, preset in enumerate(presets):
        p_env = preset.get("env") or {}
        p_toml = preset.get("toml") or {}
        if not p_env and not p_toml:
            continue
        ok = True
        for k, want in p_env.items():
            cur = env_map.get(k, _ENV_DEFAULTS.get(k, ""))
            if not _env_values_equal(cur, want, _SCHEMA_BY_KEY.get(k)):
                ok = False
                break
        if not ok:
            continue
        for k, want in p_toml.items():
            spec = _WORKBENCH_TOML_SPEC.get(k)
            if not spec:
                continue
            cur = toml_map.get(k, spec["default"])
            if spec["type"] == "bool":
                if bool(cur) != _bool_flag(want, bool(spec["default"])):
                    ok = False
                    break
            elif spec["type"] == "int":
                try:
                    if int(cur) != int(want):
                        ok = False
                        break
                except (ValueError, TypeError):
                    ok = False
                    break
        if ok:
            score = len(p_env) + len(p_toml)
            is_builtin = 1 if preset.get("builtin") else 0
            matches.append((score, is_builtin, -idx, preset["id"]))
    if not matches:
        return None
    matches.sort(reverse=True)
    return matches[0][3]


def get_workbench_state():
    """Return unified Workbench state (bot.env + config.toml + presets + options)."""
    env = _env_with_defaults(parse_bot_env())
    wb_toml = read_workbench_toml()
    custom_presets = list_custom_presets()
    all_presets = [dict(p) for p in BUILTIN_PRESETS] + custom_presets
    active_preset_id = _detect_active_preset_id(env, wb_toml, all_presets)

    model_opts = list(_MEMORY_MODEL_OPTIONS)
    cur_model = (env.get("MEMORY_MODEL") or "").strip()
    if cur_model and cur_model not in model_opts:
        model_opts.append(cur_model)

    return {
        "env": _strip_secret_env(env),
        "toml": wb_toml,
        "presets": all_presets,
        "active_preset_id": active_preset_id,
        "consistency": check_config_consistency(),
        "thought_options": list(_THOUGHT_OPTIONS),
        "memory_model_options": model_opts,
    }


def update_workbench(env_updates=None, toml_updates=None):
    """Atomically validate, backup, and update bot.env + config.toml from Workbench."""
    if env_updates is None:
        env_updates = {}
    if toml_updates is None:
        toml_updates = {}
    if not isinstance(env_updates, dict):
        raise StoreError("env_updates 必须是键值对象")
    if not isinstance(toml_updates, dict):
        raise StoreError("toml_updates 必须是键值对象")

    current_env = parse_bot_env()
    current_toml = read_workbench_toml()

    # Validate both files completely before mutating anything on disk
    norm_env, changed_env_keys = _validate_env_updates(env_updates, current_env=current_env)
    norm_toml, changed_toml_keys = _validate_workbench_toml_updates(toml_updates, current_toml=current_toml)

    restart_scope = _compute_restart_scope(changed_env_keys, changed_toml_keys)

    if not changed_env_keys and not changed_toml_keys:
        return {
            "changed_env_keys": [],
            "changed_toml_keys": [],
            "changed_keys": [],
            "restart_scope": "none",
            "backup_dir": "",
            "state": get_workbench_state(),
        }

    backup_path = _make_config_backup_dir()

    if changed_env_keys:
        _write_bot_env_updates({k: norm_env[k] for k in changed_env_keys}, backup_dir=backup_path)
        fresh_env = parse_bot_env()
        _sync_live_memory_runtime_config(fresh_env)
        sync_admins_token = "ADMIN_QQ_IDS" in changed_env_keys or "NAPCAT_TOKEN" in changed_env_keys
        sync_http_url = "NAPCAT_HTTP_URL" in changed_env_keys
        sync_ws_port = "GUARD_LISTEN_PORT" in changed_env_keys
        if (sync_admins_token or sync_http_url or sync_ws_port) and os.path.isfile(CONFIG_TOML_PATH):
            admins = _split_csv(fresh_env.get("ADMIN_QQ_IDS", "")) if sync_admins_token else None
            token = (fresh_env.get("NAPCAT_TOKEN") or "").strip() if sync_admins_token else None
            if sync_admins_token and not (admins and token):
                admins = None
                token = None
            http_url = (fresh_env.get("NAPCAT_HTTP_URL") or "").strip() if sync_http_url else None
            guard_port = (fresh_env.get("GUARD_LISTEN_PORT") or "").strip() if sync_ws_port else None
            if any(x is not None for x in (admins, token, http_url, guard_port)):
                _rewrite_config_toml_fields(
                    admin_ids_list=admins,
                    napcat_token=token,
                    backup_dir=backup_path,
                    http_url=http_url,
                    guard_listen_port=guard_port,
                )

    if changed_toml_keys:
        _rewrite_workbench_toml_fields(
            {k: norm_toml[k] for k in changed_toml_keys},
            backup_dir=backup_path,
        )

    return {
        "changed_env_keys": changed_env_keys,
        "changed_toml_keys": changed_toml_keys,
        "changed_keys": changed_env_keys + changed_toml_keys,
        "restart_scope": restart_scope,
        "backup_dir": _posix_relpath(backup_path, PROJECT_DIR),
        "state": get_workbench_state(),
    }


def get_group_whitelist():
    """Read group chat whitelist state from bot.env."""
    env = parse_bot_env()
    enabled = _bool_flag(env.get("GROUP_WHITELIST_ENABLED", "0"), False)
    raw_ids = _split_csv(env.get("GROUP_WHITELIST_IDS", ""))
    clean_ids = []
    seen = set()
    for x in raw_ids:
        sx = str(x).strip()
        if sx and sx not in seen:
            seen.add(sx)
            clean_ids.append(sx)
    return {
        "enabled": enabled,
        "group_ids": clean_ids,
    }


def update_group_whitelist(enabled, group_ids):
    """Persist GROUP_WHITELIST_ENABLED and GROUP_WHITELIST_IDS into bot.env atomically."""
    en_bool = _bool_flag(enabled, False)
    if isinstance(group_ids, str):
        raw_list = _split_csv(group_ids)
    elif isinstance(group_ids, list):
        raw_list = group_ids
    else:
        raise StoreError("group_ids 必须是群号列表")

    clean_ids = []
    seen = set()
    for item in raw_list:
        gid = str(item or "").strip()
        if not gid:
            continue
        if not gid.isdigit():
            raise StoreError(f"群号格式不正确：{gid}（应为纯数字）")
        if gid not in seen:
            seen.add(gid)
            clean_ids.append(gid)

    res = update_bot_env(
        {
            "GROUP_WHITELIST_ENABLED": "1" if en_bool else "0",
            "GROUP_WHITELIST_IDS": ",".join(clean_ids),
        },
        sync_toml=False,
    )
    return {
        "whitelist": get_group_whitelist(),
        "changed_keys": res.get("changed_keys", []),
        "backup_dir": res.get("backup_dir", ""),
    }


def _resolve_napcat_webui_config_path(env=None):
    """Resolve NapCat webui.json path, honoring NAPCAT_CONFIG_DIR env/bot.env override."""
    if env is None:
        env = parse_bot_env()
    cfg_dir = (os.environ.get("NAPCAT_CONFIG_DIR") or env.get("NAPCAT_CONFIG_DIR") or "").strip()
    if cfg_dir:
        expanded = os.path.expanduser(cfg_dir)
        if os.path.isfile(expanded):
            return expanded
        return os.path.join(expanded, "webui.json")
    return NAPCAT_WEBUI_CONFIG_PATH


def get_napcat_webui_meta():
    """Safely read NapCat webui.json (respecting NAPCAT_CONFIG_DIR) for the Embedded Console."""
    env = parse_bot_env()
    cfg_path = _resolve_napcat_webui_config_path(env)
    port = 6099
    token = ""
    disabled = False
    exists = False
    prefix = "/webui/"
    try:
        env_port = (os.environ.get("NAPCAT_WEBUI_PORT") or env.get("NAPCAT_WEBUI_PORT") or "").strip()
        if env_port:
            ep = int(env_port)
            if 1 <= ep <= 65535:
                port = ep
    except (ValueError, TypeError):
        pass
    try:
        if os.path.isfile(cfg_path):
            exists = True
            with open(cfg_path, "r", encoding="utf-8-sig") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                try:
                    p = int(raw.get("port") or port)
                    if 1 <= p <= 65535:
                        port = p
                except (ValueError, TypeError):
                    pass
                token = str(raw.get("token") or "")
                disabled = bool(raw.get("disableWebUI", False))
                raw_prefix = str(raw.get("prefix") or "").strip()
                if raw_prefix:
                    prefix = "/" + raw_prefix.strip("/") + "/"
    except Exception:
        pass
    return {
        "port": port,
        "url": f"http://127.0.0.1:{port}{prefix}",
        "token": token,
        "disabled": disabled,
        "exists": exists,
    }


# ---------------------------------------------------------------------------
# Persona Store (GEMINI.md, chat-sandbox/GEMINI.md, CHARACTER_CARD.md, backups)
# ---------------------------------------------------------------------------

def _read_text_optional(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read(), True
    except OSError:
        return "", False


def list_persona_backups():
    """List subdirectories under backup/ that contain GEMINI.md or CHARACTER_CARD.md."""
    results = []
    if not os.path.isdir(BACKUP_DIR):
        return results

    for name in os.listdir(BACKUP_DIR):
        if name.startswith("."):
            continue
        full = os.path.join(BACKUP_DIR, name)
        if not os.path.isdir(full):
            continue
        gem_direct = os.path.join(full, "GEMINI.md")
        gem_nested = os.path.join(full, "workspace", "GEMINI.md")
        card_direct = os.path.join(full, "CHARACTER_CARD.md")

        gem_path = gem_direct if os.path.isfile(gem_direct) else (gem_nested if os.path.isfile(gem_nested) else None)
        card_path = card_direct if os.path.isfile(card_direct) else None
        if not gem_path and not card_path:
            continue

        ref_path = gem_path or card_path
        try:
            st = os.stat(ref_path)
            mtime_ts = st.st_mtime
            size = st.st_size
        except OSError:
            mtime_ts = 0.0
            size = 0

        try:
            files = sorted(f for f in os.listdir(full) if not f.startswith("."))
        except OSError:
            files = []

        results.append({
            "id": name,
            "name": name,
            "has_gemini": bool(gem_path),
            "has_character_card": bool(card_path),
            "size": size,
            "mtime": datetime.datetime.fromtimestamp(mtime_ts).astimezone().isoformat(timespec="seconds") if mtime_ts else "",
            "mtime_ts": mtime_ts,
            "files": files,
        })

    results.sort(key=lambda x: (x["mtime_ts"], x["name"]), reverse=True)
    return results


def get_persona_sync_status(plugin_config=None):
    """Lightweight persona/sandbox sync check without walking backup/ or reading CHARACTER_CARD.md."""
    gem_text, gem_exists = _read_text_optional(GEMINI_PATH)
    sb_text, sb_exists = _read_text_optional(SANDBOX_GEMINI_PATH)

    sandbox_extra = []
    if os.path.isdir(SANDBOX_DIR):
        for entry in sorted(os.listdir(SANDBOX_DIR)):
            if entry != "GEMINI.md" and entry.lower() not in (".ds_store", "thumbs.db", "desktop.ini"):
                sandbox_extra.append(entry)

    if plugin_config is None:
        plugin_config = bot_plugins.load_config(force=False)
    compiled_text = (
        bot_plugins.render_sandbox_gemini_md(gem_text, config=plugin_config)
        if gem_exists
        else ""
    )
    in_sync = bool(gem_exists and sb_exists and sb_text == compiled_text)
    saved_tokens = (
        max(0, bot_plugins.estimate_tokens(gem_text) - bot_plugins.estimate_tokens(compiled_text))
        if gem_exists
        else 0
    )
    return {
        "gemini_exists": gem_exists,
        "sandbox_exists": sb_exists,
        "in_sync": in_sync,
        "saved_tokens": saved_tokens,
        "sandbox_clean": len(sandbox_extra) == 0,
        "sandbox_extra_files": sandbox_extra,
    }


def get_persona():
    gem_text, gem_exists = _read_text_optional(GEMINI_PATH)
    sb_text, sb_exists = _read_text_optional(SANDBOX_GEMINI_PATH)
    card_text, card_exists = _read_text_optional(CHARACTER_CARD_PATH)
    env = parse_bot_env()

    sandbox_extra = []
    if os.path.isdir(SANDBOX_DIR):
        for entry in sorted(os.listdir(SANDBOX_DIR)):
            if entry != "GEMINI.md" and entry.lower() not in (".ds_store", "thumbs.db", "desktop.ini"):
                sandbox_extra.append(entry)

    plugin_cfg = bot_plugins.load_config(force=True)
    compiled_text = (
        bot_plugins.render_sandbox_gemini_md(gem_text, config=plugin_cfg)
        if gem_exists
        else ""
    )
    in_sync = bool(gem_exists and sb_exists and sb_text == compiled_text)
    workspace_chars = len(gem_text)
    compiled_chars = len(compiled_text)
    workspace_tokens = bot_plugins.estimate_tokens(gem_text)
    compiled_tokens = bot_plugins.estimate_tokens(compiled_text)
    sandbox_prompt = {
        "workspace_chars": workspace_chars,
        "workspace_tokens": workspace_tokens,
        "compiled_chars": compiled_chars,
        "compiled_tokens": compiled_tokens,
        "saved_chars": max(0, workspace_chars - compiled_chars),
        "saved_tokens": max(0, workspace_tokens - compiled_tokens),
        "in_sync": in_sync,
    }
    gem_mtime = _mtime_iso(GEMINI_PATH)
    sb_mtime = _mtime_iso(SANDBOX_GEMINI_PATH)
    card_mtime = _mtime_iso(CHARACTER_CARD_PATH)

    active_rules = {
        "call_names": _split_csv(env.get("BOT_CALL_NAMES", "")),
        "soft_cues": _split_csv(env.get("BOT_SOFT_CALL_CUES", "")),
        "overused_motifs": _split_csv(env.get("BOT_OVERUSED_MOTIFS", "")),
        "overused_lookback": int(env.get("BOT_OVERUSED_LOOKBACK", "8") or 8),
        "casual_max_chars": int(env.get("BOT_CASUAL_MAX_CHARS", "30") or 30),
        "filler_opener_lookback": int(env.get("BOT_FILLER_OPENER_LOOKBACK", "4") or 4),
    }

    return {
        "gemini_md": gem_text,
        "gemini_mtime": gem_mtime,
        "sandbox_gemini_md": sb_text,
        "sandbox_mtime": sb_mtime,
        "synced": in_sync,
        "sandbox_prompt": sandbox_prompt,
        "sandbox_extra_files": sandbox_extra,
        "character_card_md": card_text,
        "character_card_mtime": card_mtime,
        "active_rules": active_rules,
        "gemini": {
            "exists": gem_exists,
            "path": "workspace/GEMINI.md",
            "content": gem_text,
            "chars": len(gem_text),
            "lines": gem_text.count("\n") + (1 if gem_text else 0),
            "bytes": len(gem_text.encode("utf-8")),
            "sha256": _sha256_text(gem_text)[:12],
            "mtime": gem_mtime,
        },
        "sandbox": {
            "exists": sb_exists,
            "path": "chat-sandbox/GEMINI.md",
            "in_sync": in_sync,
            "sha256": _sha256_text(sb_text)[:12] if sb_exists else "",
            "mtime": sb_mtime,
            "extra_files": sandbox_extra,
            "clean": len(sandbox_extra) == 0,
        },
        "character_card": {
            "exists": card_exists,
            "path": "docs/CHARACTER_CARD.md",
            "content": card_text,
            "chars": len(card_text),
            "lines": card_text.count("\n") + (1 if card_text else 0),
            "bytes": len(card_text.encode("utf-8")),
            "mtime": card_mtime,
        },
        "backups": list_persona_backups(),
    }


def _sync_sandbox_with_plugins() -> Tuple[bool, str]:
    """Compile workspace/GEMINI.md via active plugins and write atomically to chat-sandbox/GEMINI.md."""
    gem_text, gem_exists = _read_text_optional(GEMINI_PATH)
    if not gem_exists:
        return False, "找不到 workspace/GEMINI.md"
    try:
        cfg = bot_plugins.load_config(force=True)
        compiled = bot_plugins.render_sandbox_gemini_md(gem_text, config=cfg)
        os.makedirs(SANDBOX_DIR, exist_ok=True)
        _atomic_write_text(SANDBOX_GEMINI_PATH, compiled)
        return True, ""
    except Exception as exc:
        return False, str(exc)


def sync_sandbox():
    """Immediately compile workspace/GEMINI.md (with active plugins) -> chat-sandbox/GEMINI.md atomically."""
    gem_text, gem_exists = _read_text_optional(GEMINI_PATH)
    if not gem_exists:
        raise NotFoundError("找不到 workspace/GEMINI.md")
    cfg = bot_plugins.load_config(force=True)
    compiled = bot_plugins.render_sandbox_gemini_md(gem_text, config=cfg)
    os.makedirs(SANDBOX_DIR, exist_ok=True)
    _atomic_write_text(SANDBOX_GEMINI_PATH, compiled)
    return {
        "in_sync": True,
        "bytes": len(compiled.encode("utf-8")),
        "sha256": _sha256_text(compiled)[:12],
        "mtime": _mtime_iso(SANDBOX_GEMINI_PATH),
        "workspace_chars": len(gem_text),
        "compiled_chars": len(compiled),
        "saved_chars": max(0, len(gem_text) - len(compiled)),
        "saved_tokens": max(0, bot_plugins.estimate_tokens(gem_text) - bot_plugins.estimate_tokens(compiled)),
    }


def save_gemini(content, create_backup=True, sync_to_sandbox=True):
    if not isinstance(content, str) or not content.strip():
        raise StoreError("GEMINI.md 内容不能为空")

    backup_id = ""
    if create_backup and os.path.isfile(GEMINI_PATH):
        backup_id = f"webui-persona-{_ts_tag()}"
        bdir = os.path.join(BACKUP_DIR, backup_id)
        os.makedirs(bdir, exist_ok=True)
        shutil.copy2(GEMINI_PATH, os.path.join(bdir, "GEMINI.md"))

    _atomic_write_text(GEMINI_PATH, content)
    synced = False
    if sync_to_sandbox:
        cfg = bot_plugins.load_config(force=True)
        compiled = bot_plugins.render_sandbox_gemini_md(content, config=cfg)
        os.makedirs(SANDBOX_DIR, exist_ok=True)
        _atomic_write_text(SANDBOX_GEMINI_PATH, compiled)
        synced = True

    data = get_persona()
    data["saved"] = {
        "backup_id": backup_id,
        "synced_sandbox": synced,
    }
    return data


def save_character_card(content, create_backup=True):
    if not isinstance(content, str) or not content.strip():
        raise StoreError("CHARACTER_CARD.md 内容不能为空")

    backup_id = ""
    if create_backup and os.path.isfile(CHARACTER_CARD_PATH):
        backup_id = f"webui-card-{_ts_tag()}"
        bdir = os.path.join(BACKUP_DIR, backup_id)
        os.makedirs(bdir, exist_ok=True)
        shutil.copy2(CHARACTER_CARD_PATH, os.path.join(bdir, "CHARACTER_CARD.md"))

    _atomic_write_text(CHARACTER_CARD_PATH, content)
    data = get_persona()
    data["saved"] = {"backup_id": backup_id}
    return data


def get_persona_backup_diff(backup_id, target="gemini"):
    """Read a backup under backup/<backup_id> and produce structured unified diff against current file."""
    bid = str(backup_id or "").strip()
    if not bid or "/" in bid or "\\" in bid or bid in (".", ".."):
        raise StoreError("无效的备份 ID")

    bdir = os.path.join(BACKUP_DIR, bid)
    if not _is_confined(bdir, BACKUP_DIR) or not os.path.isdir(bdir):
        raise NotFoundError(f"找不到备份：{bid}")

    gem_bfile = os.path.join(bdir, "GEMINI.md")
    if not os.path.isfile(gem_bfile):
        alt = os.path.join(bdir, "workspace", "GEMINI.md")
        gem_bfile = alt if os.path.isfile(alt) else ""
    card_bfile = os.path.join(bdir, "CHARACTER_CARD.md")
    if not os.path.isfile(card_bfile):
        card_bfile = ""

    if target == "character_card":
        bfile = card_bfile
        cur_path = CHARACTER_CARD_PATH
        fname = "CHARACTER_CARD.md"
    else:
        bfile = gem_bfile or card_bfile
        cur_path = GEMINI_PATH if gem_bfile else CHARACTER_CARD_PATH
        fname = "GEMINI.md" if gem_bfile else "CHARACTER_CARD.md"

    if not bfile or not _is_confined(bfile, BACKUP_DIR) or not os.path.isfile(bfile):
        raise NotFoundError(f"备份 {bid} 中不包含 {fname}")

    old_text, _ = _read_text_optional(bfile)
    cur_text, _ = _read_text_optional(cur_path)
    gem_backup_text, gem_ok = _read_text_optional(gem_bfile) if gem_bfile else ("", False)
    card_backup_text, card_ok = _read_text_optional(card_bfile) if card_bfile else ("", False)

    old_lines = old_text.splitlines()
    cur_lines = cur_text.splitlines()
    raw_diff = list(
        difflib.unified_diff(
            old_lines,
            cur_lines,
            fromfile=f"backup/{bid}/{fname}",
            tofile=f"当前/{fname}",
            lineterm="",
            n=3,
        )
    )

    diff_lines = []
    added = 0
    removed = 0
    for line in raw_diff:
        if line.startswith("--- ") or line.startswith("+++ "):
            continue
        if line.startswith("@@"):
            diff_lines.append({"type": "hunk", "text": line})
        elif line.startswith("+"):
            diff_lines.append({"type": "add", "text": line})
            added += 1
        elif line.startswith("-"):
            diff_lines.append({"type": "del", "text": line})
            removed += 1
        else:
            diff_lines.append({"type": "ctx", "text": line})

    return {
        "id": bid,
        "backup_id": bid,
        "target": target,
        "filename": fname,
        "mtime": _mtime_iso(bfile),
        "gemini_md": gem_backup_text if gem_ok else None,
        "character_card_md": card_backup_text if card_ok else "",
        "backup_content": old_text,
        "identical": old_text == cur_text,
        "stats": {
            "added": added,
            "removed": removed,
            "backup_lines": len(old_lines),
            "current_lines": len(cur_lines),
        },
        "diff_lines": diff_lines,
        "diff": diff_lines,
    }


# ---------------------------------------------------------------------------
# User & Group Memory Store (re-exported from webui.memory_store)
# ---------------------------------------------------------------------------

from . import memory_store as _memory_store  # noqa: E402
from .memory_store import (  # noqa: E402,F401
    _DATE_YMD_RE,
    _USER_MEMORY_KINDS,
    _append_mem_history,
    _clean_profile_input,
    _clean_v2_item_input,
    _count_conflict_pairs,
    _diff_and_sync_scope_memories,
    _extract_conflict_info,
    _format_archived_item,
    _format_memory_item,
    _memory_user_map,
    _norm_mem_text,
    _normalize_date_input,
    _normalize_keywords_input,
    _profile_has_any_content,
    _scan_sensitive_group_payload,
    _scan_sensitive_profile,
    _scan_sensitive_texts,
    _sleep_in_progress,
    _sleep_running_lock,
    _sync_live_memory_runtime_config,
    _table_has_column,
    _user_exists_in_db,
    delete_memory,
    get_group_memory,
    get_memory,
    get_memory_summary_stats,
    get_user_memory_traces,
    list_group_memories,
    list_memories,
    list_memory_conflicts,
    resolve_memory_conflict,
    restore_memory_item,
    run_memory_maintenance_flush,
    run_memory_maintenance_sleep,
    trigger_consolidation,
    update_group_memory,
    update_memory,
)

_memory_store._stores_mod = sys.modules[__name__]


# ---------------------------------------------------------------------------
# Repeat Chain Tester (reads live bot.env + repeat_chain.py regexes)
# ---------------------------------------------------------------------------

def test_repeat_text(text):
    raw = str(text if text is not None else "")
    env = parse_bot_env()

    enabled = _bool_flag(env.get("REPEAT_ENABLED", "1"), True)
    min_count = max(2, int(env.get("REPEAT_MIN_COUNT", "2") or 2))
    window_secs = float(env.get("REPEAT_WINDOW_SECS", "180") or 180)
    cooldown_secs = float(env.get("REPEAT_COOLDOWN_SECS", "60") or 60)
    max_chars = int(env.get("REPEAT_MAX_CHARS", "60") or 60)
    photos = _bool_flag(env.get("REPEAT_PHOTOS", "1"), True)
    block_words = [w.lower() for w in _split_csv(env.get("REPEAT_BLOCK_WORDS", ""))]
    call_names = [
        w.lower()
        for w in _split_csv(env.get("BOT_CALL_NAMES", "哈基米,及其猫,机器猫,本猫,紫毛猫,紫发猫"))
    ]
    soft_cues = [
        w.lower()
        for w in _split_csv(
            env.get("BOT_SOFT_CALL_CUES", "猫猫,小猫,小紫,紫毛,紫发,那只猫,某猫,咪咪,哈基,及其,猫娘")
        )
    ]

    settings = {
        "enabled": enabled,
        "min_count": min_count,
        "window_secs": window_secs,
        "cooldown_secs": cooldown_secs,
        "max_chars": max_chars,
        "photos": photos,
        "block_words": block_words,
    }

    def _result(qualifies, reason, key_desc=""):
        return {
            "qualifies": qualifies,
            "reason": reason,
            "key_desc": key_desc,
            "settings": settings,
        }

    if "[CQ:" in raw:
        return _result(False, "文本包含原始 CQ 码（如 @、回复、卡片等）；含 @/回复/文件/语音/卡片的消息不会参与复读。")

    trimmed = repeat_chain._cq_unescape(raw).strip()
    if not trimmed:
        return _result(False, "文本为空或仅含空白字符，无法构成复读队形。")

    if len(trimmed) > max_chars:
        return _result(
            False,
            f"字数超限：当前 {len(trimmed)} 字，超过 REPEAT_MAX_CHARS（{max_chars} 字）上限。",
        )

    if trimmed.startswith("/"):
        return _result(False, "以 `/` 开头的斜杠命令不会参与复读。")

    if repeat_chain._LOW_SIGNAL_RE.match(trimmed):
        return _result(False, "仅包含标点或无意义符号，被低信号规则过滤。")

    unsafe_match = repeat_chain._UNSAFE_RE.search(trimmed)
    if unsafe_match:
        return _result(False, f"包含网址链接或 @ 符号（命中 `{unsafe_match.group(0)}`），安全规则禁止复读。")

    for run in repeat_chain._DIGIT_RUN_RE.findall(trimmed):
        if len(set(run)) > 1:
            return _result(
                False,
                f"包含 6 位以上非单一重复数字串（`{run}`），疑似 QQ 号/手机号/验证码，禁止复读（类似 `666666` 同数字连发则允许）。",
            )

    low = trimmed.lower()
    for w in call_names:
        if w in low:
            return _result(False, f"命中直接点名词「{w}」：属于叫机器人说话，按正常对话处理而非复读。")
    for w in soft_cues:
        if w in low:
            return _result(False, f"命中软点名词「{w}」：疑似在提机器人，按正常对话处理而非复读。")
    for w in block_words:
        if w in low:
            return _result(False, f"命中自定义复读屏蔽词「{w}」（REPEAT_BLOCK_WORDS）。")

    if not enabled:
        return _result(
            False,
            "文本本身通过了全部复读内容规则，但当前 bot.env 中 REPEAT_ENABLED=0（复读功能已关闭）。",
            key_desc=trimmed,
        )

    return _result(
        True,
        f"符合复读条件！当群内至少 2 位不同成员连续发送 {min_count} 条相同内容（相邻间隔 ≤ {window_secs:g} 秒、同群冷却 ≥ {cooldown_secs:g} 秒）时，ws-guard 将把它交给模型决定是否跟风复读。",
        key_desc=trimmed,
    )


# ---------------------------------------------------------------------------
# Workspace & Backup Browser (read-only, realpath-confined)
# ---------------------------------------------------------------------------

_ALLOWED_BROWSER_ROOTS = [
    ("workspace", "管理员工作区 (workspace/)", WORKSPACE_DIR),
    ("chat-sandbox", "群聊安全沙箱 (chat-sandbox/)", SANDBOX_DIR),
    ("docs", "人设与文档 (docs/)", DOCS_DIR),
    ("backup", "历史备份 (backup/)", BACKUP_DIR),
]


def _list_flat_files(root_dir, rel_base=None, max_depth=3, depth=0):
    """List files/dirs inside root_dir with rel_path relative to rel_base (default root_dir)."""
    if rel_base is None:
        rel_base = root_dir
    out = []
    if not os.path.isdir(root_dir) or depth > max_depth:
        return out
    try:
        names = sorted(os.listdir(root_dir))
    except OSError:
        return out

    for name in names:
        if name.lower() in _IGNORED_BROWSER_NAMES:
            continue
        full = os.path.join(root_dir, name)
        rel = _posix_relpath(full, rel_base)
        try:
            st = os.lstat(full)
        except OSError:
            continue
        is_dir = os.path.isdir(full) and not os.path.islink(full)
        mtime = datetime.datetime.fromtimestamp(st.st_mtime).astimezone().isoformat(timespec="seconds")
        if is_dir:
            out.append({
                "name": name,
                "rel_path": rel,
                "path": _posix_relpath(full, PROJECT_DIR),
                "is_dir": True,
                "size": 0,
                "mtime": mtime,
                "previewable": False,
            })
            if depth < max_depth:
                out.extend(_list_flat_files(full, rel_base=rel_base, max_depth=max_depth, depth=depth + 1))
        else:
            ext = os.path.splitext(name)[1].lower()
            previewable = (
                ext in _PREVIEWABLE_EXTS or name in ("bot.env", "GEMINI.md", "CHARACTER_CARD.md")
            ) and st.st_size <= MAX_PREVIEW_BYTES
            out.append({
                "name": name,
                "rel_path": rel,
                "path": _posix_relpath(full, PROJECT_DIR),
                "is_dir": False,
                "size": st.st_size,
                "mtime": mtime,
                "ext": ext,
                "previewable": previewable,
            })
    return out


def list_workspace():
    env = parse_bot_env()
    turbo = _bool_flag(env.get("ADMIN_DM_TURBO", "1"), True)

    workspace_files = _list_flat_files(WORKSPACE_DIR, max_depth=2)
    sandbox_files = _list_flat_files(SANDBOX_DIR, max_depth=2)
    sandbox_extra = [f["rel_path"] for f in sandbox_files if f["rel_path"] != "GEMINI.md"]

    backups = []
    if os.path.isdir(BACKUP_DIR):
        for bname in os.listdir(BACKUP_DIR):
            if bname.startswith("."):
                continue
            bfull = os.path.join(BACKUP_DIR, bname)
            if not os.path.isdir(bfull) or os.path.islink(bfull):
                continue
            bfiles = [
                f for f in _list_flat_files(bfull, rel_base=BACKUP_DIR, max_depth=2)
                if not f["is_dir"]
            ]
            backups.append({
                "name": bname,
                "mtime": _mtime_iso(bfull),
                "file_count": len(bfiles),
                "files": bfiles,
            })
        backups.sort(key=lambda x: (x["mtime"], x["name"]), reverse=True)

    pol = bot_permissions.load_policy(force=True)
    dm_local_ok = bot_permissions.is_allowed("admin_dm_local_tools", actor_role="admin", is_dm=True, policy=pol)
    grp_web_ok = bot_permissions.is_allowed("web_search", actor_role="member", is_dm=False, policy=pol)

    permissions = [
        {
            "scope": "管理员私聊",
            "session_pattern": "qq:<ADMIN_QQ_ID>",
            "workdir": "workspace/",
            "local_tools": (
                "允许（可读写文件、执行命令）"
                if dm_local_ok
                else "已在权限管理中临时关闭（仅聊天）"
            ),
            "web_search": "允许" if bot_permissions.is_allowed("web_search", actor_role="admin", is_dm=True, policy=pol) else "已关闭",
            "approval": (
                ("Turbo 自动放行（ADMIN_DM_TURBO=1）" if turbo else "逐条发送确认（ADMIN_DM_TURBO=0）")
                if dm_local_ok
                else "权限管理总开关已关闭，包装层静默拒绝本地工具"
            ),
            "privileged": dm_local_ok,
            "turbo": turbo if dm_local_ok else None,
        },
        {
            "scope": "群聊（含管理员在群里）",
            "session_pattern": "qq:g:<GROUP_ID>",
            "workdir": "chat-sandbox/",
            "local_tools": (
                "禁止本地工具（仅放行只读联网搜索；调用本地工具立即取消本轮）"
                if grp_web_ok
                else "禁止本地工具与联网搜索（仅纯聊天）"
            ),
            "web_search": "允许" if grp_web_ok else "已关闭",
            "approval": "包装层静默拦截，群里不弹确认",
            "privileged": False,
        },
        {
            "scope": "普通用户私聊",
            "session_pattern": "qq:<USER_ID>",
            "workdir": "chat-sandbox/",
            "local_tools": (
                "禁止本地工具（仅放行只读联网搜索；调用本地工具立即取消本轮）"
                if bot_permissions.is_allowed("web_search", actor_role="member", is_dm=True, policy=pol)
                else "禁止本地工具与联网搜索（仅纯聊天）"
            ),
            "web_search": "允许" if bot_permissions.is_allowed("web_search", actor_role="member", is_dm=True, policy=pol) else "已关闭",
            "approval": "包装层静默拦截，不弹确认",
            "privileged": False,
        },
    ]

    return {
        "workspace_dir": WORKSPACE_DIR,
        "sandbox_dir": SANDBOX_DIR,
        "backup_dir": BACKUP_DIR,
        "workspace_files": workspace_files,
        "sandbox_files": sandbox_files,
        "sandbox_clean": len(sandbox_extra) == 0,
        "sandbox_extra_files": sandbox_extra,
        "backups": backups,
        "permissions": permissions,
        "admin_dm_turbo": turbo,
    }


def _redact_preview_secrets(filename, text):
    low = filename.lower()
    redacted = False
    if not (low.endswith(".env") or low.endswith(".toml") or ".env." in low):
        return text, False

    out = []
    for line in text.splitlines(True):
        m1 = _REDACT_ENV_RE.match(line)
        if m1 and m1.group(2).strip():
            out.append(f"{m1.group(1)}{_mask_secret(m1.group(2).strip())}\n")
            redacted = True
            continue
        m2 = _REDACT_TOML_RE.match(line)
        if m2 and m2.group(2).strip():
            out.append(f"{m2.group(1)}{_mask_secret(m2.group(2).strip())}{m2.group(3)}\n")
            redacted = True
            continue
        out.append(line)
    return "".join(out), redacted


_ROOT_DIR_MAP = {key: path for key, _, path in _ALLOWED_BROWSER_ROOTS}


def read_workspace_file(rel_path, root=""):
    clean = str(rel_path or "").strip().lstrip("/")
    root_key = str(root or "").strip()
    if not clean or ".." in clean.split("/") or "\\" in clean:
        raise StoreError("非法文件路径")

    parts = [p for p in clean.split("/") if p]
    if not parts or any(p in (".", "..") or ":" in p for p in parts):
        raise StoreError("非法文件路径")

    if root_key:
        if root_key not in _ROOT_DIR_MAP:
            raise StoreError("不支持的根目录", status_code=403)
        base_dir = _ROOT_DIR_MAP[root_key]
        # Avoid double prefix if caller passes root="workspace" & path="workspace/GEMINI.md"
        sub_parts = parts[1:] if (len(parts) > 1 and parts[0] == root_key) else parts
        target = os.path.join(base_dir, *sub_parts)
        if not _is_confined(target, base_dir):
            raise StoreError("路径越界，禁止访问", status_code=403)
        display_path = _posix_relpath(target, PROJECT_DIR)
    else:
        target = os.path.join(PROJECT_DIR, *parts)
        allowed = any(_is_confined(target, rpath) for _, _, rpath in _ALLOWED_BROWSER_ROOTS)
        if not allowed:
            raise StoreError("仅允许预览 workspace/、chat-sandbox/、docs/ 与 backup/ 目录下的文件", status_code=403)
        display_path = "/".join(parts)

    if not os.path.isfile(target):
        raise NotFoundError(f"找不到文件：{display_path}")

    size = os.path.getsize(target)
    truncated = size > MAX_PREVIEW_BYTES
    with open(target, "rb") as f:
        raw = f.read(MAX_PREVIEW_BYTES)

    # Detect binary content
    if b"\x00" in raw[:4096]:
        return {
            "path": display_path,
            "name": os.path.basename(target),
            "size": size,
            "mtime": _mtime_iso(target),
            "binary": True,
            "truncated": truncated,
            "redacted": False,
            "content": "",
        }

    text = raw.decode("utf-8", errors="replace")
    text, redacted = _redact_preview_secrets(os.path.basename(target), text)

    return {
        "path": display_path,
        "name": os.path.basename(target),
        "size": size,
        "mtime": _mtime_iso(target),
        "binary": False,
        "truncated": truncated,
        "redacted": redacted,
        "content": text,
    }


# ---------------------------------------------------------------------------
# Bot Permissions Store (`data/bot_permissions.json`, hot-reloaded)
# ---------------------------------------------------------------------------

def get_permissions_state(napcat_groups=None, bot_roles=None):
    """Build complete permissions payload for Web UI (hot-reloaded from disk)."""
    pol = bot_permissions.load_policy(force=True)
    env = _env_with_defaults()
    roles_map = bot_roles if isinstance(bot_roles, dict) else {}

    napcat_by_id = {}
    for g in (napcat_groups or []):
        if isinstance(g, dict) and g.get("group_id"):
            napcat_by_id[str(g["group_id"])] = g

    policy_groups = pol.get("groups") or {}
    all_gids = []
    seen = set()
    for g in (napcat_groups or []):
        gid = str(g.get("group_id") or "").strip()
        if gid and gid not in seen:
            seen.add(gid)
            all_gids.append(gid)
    for gid in sorted(policy_groups.keys()):
        if gid not in seen:
            seen.add(gid)
            all_gids.append(gid)

    groups_out = []
    override_groups_count = 0
    override_rules_count = 0
    for gid in all_gids:
        ng = napcat_by_id.get(gid)
        pg = policy_groups.get(gid) or {}
        overrides = pg.get("overrides") if isinstance(pg.get("overrides"), dict) else {}
        if overrides:
            override_groups_count += 1
            override_rules_count += len(overrides)
        gname = (
            (ng.get("group_name") if ng else "")
            or pg.get("label")
            or f"群 {gid}"
        )
        groups_out.append({
            "group_id": gid,
            "group_name": gname,
            "label": pg.get("label") or "",
            "member_count": int(ng.get("member_count") or 0) if ng else 0,
            "in_napcat": bool(ng),
            "bot_role": roles_map.get(gid, "unknown"),
            "override_count": len(overrides),
            "overrides": overrides,
            "effective": bot_permissions.get_effective_matrix(group_id=gid, policy=pol),
        })

    glob = pol.get("global") or {}
    enabled_global = sum(
        1 for c in bot_permissions.CAPABILITY_CATALOG
        if (glob.get(c["id"]) or {}).get("enabled")
    )
    high_risk_enabled = sum(
        1 for c in bot_permissions.CAPABILITY_CATALOG
        if c["risk"] in ("high", "critical")
        and (glob.get(c["id"]) or {}).get("enabled")
    )

    plugin_cfg = bot_plugins.load_config(force=True)
    plugin_status = {}
    for c in bot_permissions.CAPABILITY_CATALOG:
        cap_id = c["id"]
        owner = bot_plugins.get_capability_plugin(cap_id)
        if owner is not None:
            plugin_status[cap_id] = {
                "plugin_id": owner.id,
                "plugin_name": owner.name,
                "enabled": bot_plugins.is_enabled(owner.id, config=plugin_cfg),
            }

    return {
        "policy": pol,
        "catalog": bot_permissions.CAPABILITY_CATALOG,
        "categories": bot_permissions.CAPABILITY_CATEGORIES,
        "roles": bot_permissions.ROLE_META,
        "limit_specs": bot_permissions.LIMIT_SPECS,
        "templates": bot_permissions.PERMISSION_TEMPLATES,
        "groups": groups_out,
        "plugin_status": plugin_status,
        "env_flags": {
            "BOT_QQ_ID": str(env.get("BOT_QQ_ID") or "").strip(),
            "ADMIN_QQ_IDS": _split_csv(env.get("ADMIN_QQ_IDS", "")),
            "ADMIN_DM_TURBO": _bool_flag(env.get("ADMIN_DM_TURBO", "1"), True),
            "REPEAT_ENABLED": _bool_flag(env.get("REPEAT_ENABLED", "1"), True),
            "REPEAT_PHOTOS": _bool_flag(env.get("REPEAT_PHOTOS", "1"), True),
            "FORWARD_LONG_MSG_GROUP": _bool_flag(env.get("FORWARD_LONG_MSG_GROUP", "1"), True),
            "FORWARD_LONG_MSG_DM": _bool_flag(env.get("FORWARD_LONG_MSG_DM", "0"), False),
        },
        "summary": {
            "total_capabilities": len(bot_permissions.CAPABILITY_CATALOG),
            "enabled_global": enabled_global,
            "high_risk_enabled": high_risk_enabled,
            "override_groups": override_groups_count,
            "override_rules": override_rules_count,
            "updated_at": pol.get("updated_at") or "",
            "hot_reload": True,
        },
        "file_path": _posix_relpath(bot_permissions.PERMISSIONS_FILE, PROJECT_DIR),
    }


def update_permissions(payload):
    if not isinstance(payload, dict):
        raise StoreError("无效的权限更新请求")
    cur = bot_permissions.load_policy(force=True)
    merged = dict(cur)
    if "global" in payload:
        if not isinstance(payload["global"], dict):
            raise StoreError("global 必须是对象")
        new_glob = dict(cur.get("global") or {})
        new_glob.update(payload["global"])
        merged["global"] = new_glob
    if "limits" in payload:
        if not isinstance(payload["limits"], dict):
            raise StoreError("limits 必须是对象")
        new_lim = dict(cur.get("limits") or {})
        new_lim.update(payload["limits"])
        merged["limits"] = new_lim
    if "groups" in payload:
        if not isinstance(payload["groups"], dict):
            raise StoreError("groups 必须是对象")
        merged["groups"] = payload["groups"]
    return bot_permissions.save_policy(merged)


def update_group_permissions(group_id, payload):
    gid = str(group_id or "").strip()
    if not bot_permissions.is_valid_group_id(gid):
        raise StoreError("群号格式无效（需为 5~15 位纯数字）")
    if not isinstance(payload, dict):
        raise StoreError("无效的群权限更新请求")

    cur = bot_permissions.load_policy(force=True)
    groups = dict(cur.get("groups") or {})
    existing = dict(groups.get(gid) or {})

    overrides = payload.get("overrides")
    if overrides is None:
        overrides = existing.get("overrides") or {}
    if not isinstance(overrides, dict):
        raise StoreError("overrides 必须是对象")

    label = str(payload.get("label") if "label" in payload else existing.get("label", "")).strip()
    if overrides or label:
        groups[gid] = {"label": label, "overrides": overrides}
    else:
        groups.pop(gid, None)

    nxt = dict(cur)
    nxt["groups"] = groups
    return bot_permissions.save_policy(nxt)


def delete_group_permissions(group_id):
    gid = str(group_id or "").strip()
    if not bot_permissions.is_valid_group_id(gid):
        raise StoreError("群号格式无效")
    cur = bot_permissions.load_policy(force=True)
    groups = dict(cur.get("groups") or {})
    removed = gid in groups
    groups.pop(gid, None)
    nxt = dict(cur)
    nxt["groups"] = groups
    saved = bot_permissions.save_policy(nxt)
    return {"removed": removed, "group_id": gid, "policy": saved}


def apply_permissions_template(template_id, keep_groups=True, keep_limits=True):
    try:
        return bot_permissions.apply_template(
            template_id,
            keep_groups=bool(keep_groups),
            keep_limits=bool(keep_limits),
        )
    except ValueError as exc:
        raise StoreError(str(exc), status_code=400)


def reset_permissions(keep_groups=False):
    cur = bot_permissions.load_policy(force=True)
    nxt = bot_permissions.default_policy()
    if keep_groups:
        nxt["groups"] = cur.get("groups") or {}
    return bot_permissions.save_policy(nxt)


def evaluate_permission(payload):
    if not isinstance(payload, dict):
        raise StoreError("无效的权限试算请求")
    cap = str(payload.get("capability") or "").strip()
    role = str(payload.get("actor_role") or "member").strip().lower()
    is_dm = bool(payload.get("is_dm", False))
    gid = str(payload.get("group_id") or "").strip() if not is_dm else ""
    if not cap:
        raise StoreError("请指定要试算的能力 ID")
    pol = bot_permissions.load_policy(force=True)
    res = bot_permissions.check_permission(
        cap, actor_role=role, group_id=gid or None, is_dm=is_dm, policy=pol
    )
    res["effective"] = bot_permissions.resolve_capability(cap, group_id=gid or None, policy=pol)
    return res


# ---------------------------------------------------------------------------
# Modular Plugin Management Store (`data/bot_plugins.json`, hot-reloaded)
# ---------------------------------------------------------------------------

def _attach_sandbox_sync_result(
    state: Dict[str, Any], synced: bool, err: str
) -> Dict[str, Any]:
    if err:
        state["sandbox_synced"] = False
        state["sandbox_sync_error"] = err
    else:
        state["sandbox_synced"] = bool(synced and state.get("sandbox_synced", True))
        state["sandbox_sync_error"] = ""
    return state


def get_plugins_state(force: bool = False) -> Dict[str, Any]:
    """Build complete plugin management state for WebUI「插件管理」."""
    raw_env = _env_with_defaults(parse_bot_env())
    env_map = {k: str(v) for k, v in raw_env.items() if not _SECRET_KEY_RE.search(k)}
    gem_text, gem_exists = _read_text_optional(GEMINI_PATH)
    sb_text, sb_exists = _read_text_optional(SANDBOX_GEMINI_PATH)

    state = bot_plugins.build_ui_state(
        env_map=env_map,
        workspace_gemini_text=gem_text,
        force=bool(force),
    )
    compiled = (
        bot_plugins.render_sandbox_gemini_md(gem_text, config=state.get("config"))
        if gem_exists
        else ""
    )
    state["sandbox_synced"] = bool(gem_exists and sb_exists and sb_text == compiled)
    state["sandbox_sync_error"] = ""
    return state


def update_plugins(body: Dict[str, Any]) -> Dict[str, Any]:
    """Bulk-update plugin toggles/settings and re-compile chat-sandbox/GEMINI.md."""
    if not isinstance(body, dict) or not isinstance(body.get("plugins"), dict):
        raise StoreError("无效的插件批量更新请求：缺少 plugins 对象", status_code=400)
    try:
        bot_plugins.bulk_update(body["plugins"])
    except ValueError as exc:
        raise StoreError(str(exc), status_code=400)
    synced, err = _sync_sandbox_with_plugins()
    state = get_plugins_state(force=True)
    return _attach_sandbox_sync_result(state, synced, err)


def update_single_plugin(plugin_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
    """Update a single plugin's enabled flag and/or settings, then sync sandbox."""
    pid = str(plugin_id or "").strip()
    if not pid or bot_plugins.get_plugin(pid) is None:
        raise NotFoundError(f"未知插件：{pid}")
    if not isinstance(body, dict):
        raise StoreError("无效的插件更新请求", status_code=400)
    if (
        "settings" in body
        and body.get("settings") is not None
        and not isinstance(body.get("settings"), dict)
    ):
        raise StoreError("settings 必须是对象", status_code=400)
    try:
        bot_plugins.update_plugin(
            pid,
            enabled=body.get("enabled") if "enabled" in body else None,
            settings=body.get("settings") if isinstance(body.get("settings"), dict) else None,
        )
    except ValueError as exc:
        raise StoreError(str(exc), status_code=400)
    synced, err = _sync_sandbox_with_plugins()
    state = get_plugins_state(force=True)
    return _attach_sandbox_sync_result(state, synced, err)


def apply_plugin_preset(preset_id: str) -> Dict[str, Any]:
    """Apply a built-in plugin preset and re-compile chat-sandbox/GEMINI.md."""
    pid = str(preset_id or "").strip()
    if not pid:
        raise StoreError("请指定要应用的插件预设 preset_id", status_code=400)
    try:
        bot_plugins.apply_preset(pid)
    except ValueError as exc:
        raise StoreError(str(exc), status_code=400)
    synced, err = _sync_sandbox_with_plugins()
    state = get_plugins_state(force=True)
    return _attach_sandbox_sync_result(state, synced, err)


def reset_plugins() -> Dict[str, Any]:
    """Reset all plugins to default config and re-compile chat-sandbox/GEMINI.md."""
    bot_plugins.reset_config()
    synced, err = _sync_sandbox_with_plugins()
    state = get_plugins_state(force=True)
    return _attach_sandbox_sync_result(state, synced, err)


def rescan_plugins() -> Dict[str, Any]:
    """Force re-discovery of plugins/*.py, clear config cache, and re-sync sandbox."""
    bot_plugins.discover_plugins(force=True)
    bot_plugins.invalidate_cache()
    synced, err = _sync_sandbox_with_plugins()
    state = get_plugins_state(force=True)
    return _attach_sandbox_sync_result(state, synced, err)
