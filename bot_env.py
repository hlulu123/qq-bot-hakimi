"""Load the shared bot settings file (bot.env) and provide cross-platform helpers.

bot.env is the single source of truth for settings shared by
agy-acp-wrapper.py, onebot-ws-guard.py, user_memory.py, bot_runner.py,
setup_wizard.py, webui, and helper scripts. Values already present in the
environment win over the file, so a caller can still override one setting
for a single run.

Import this module before reading any settings from os.environ.
"""

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = _SCRIPT_DIR
ENV_FILE = os.environ.get("BOT_ENV_FILE", "").strip() or os.path.join(_SCRIPT_DIR, "bot.env")

if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

import file_lock  # noqa: E402

_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_EXPORT_PREFIX_RE = re.compile(r"^export\s+")
_WIN_EXEC_EXTS = (".exe", ".cmd", ".bat", ".com")
_TRUTHY_STRS = frozenset({"1", "true", "yes", "on", "y"})
_FALSY_STRS = frozenset({"0", "false", "no", "off", "n"})


# ---------------------------------------------------------------------------
# bot.env parsing, formatting, and typed getters
# ---------------------------------------------------------------------------

def _parse_double_quoted(raw):
    """Parse a double-quoted env value with POSIX-compatible backslash unescaping."""
    out = []
    i = 1
    n = len(raw)
    closed = False
    while i < n:
        ch = raw[i]
        if ch == "\\" and i + 1 < n:
            nxt = raw[i + 1]
            if nxt in ('"', "$", "`", "\\"):
                out.append(nxt)
                i += 2
                continue
            # Preserve unknown escapes (e.g., Windows paths like "C:\Users\...") literally.
            out.append(ch)
            i += 1
            continue
        if ch == '"':
            closed = True
            i += 1
            break
        out.append(ch)
        i += 1
    if not closed:
        return raw
    rest = raw[i:].lstrip()
    if rest and not rest.startswith("#"):
        if raw.endswith('"') and len(raw) >= 2:
            return raw[1:-1]
        return raw
    return "".join(out)


def _parse_single_quoted(raw):
    """Parse a single-quoted literal env value (no backslash escaping)."""
    end = raw.find("'", 1)
    if end == -1:
        return raw
    rest = raw[end + 1:].lstrip()
    if rest and not rest.startswith("#"):
        if raw.endswith("'") and len(raw) >= 2:
            return raw[1:-1]
        return raw
    return raw[1:end]


def _strip_unquoted_inline_comment(raw):
    """Strip an unquoted inline `# comment` only when `#` is preceded by whitespace."""
    for idx, ch in enumerate(raw):
        if ch == "#" and idx > 0 and raw[idx - 1] in (" ", "\t"):
            return raw[:idx].rstrip()
    return raw


def _parse_line(line):
    if not isinstance(line, str):
        return None
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return None
    if _EXPORT_PREFIX_RE.match(stripped):
        stripped = _EXPORT_PREFIX_RE.sub("", stripped, count=1)
    key, raw_val = stripped.split("=", 1)
    key = key.strip()
    if not _ENV_KEY_RE.match(key):
        return None
    val_l = raw_val.lstrip()
    if not val_l:
        return key, ""
    if val_l[0] == "#":
        if raw_val and raw_val[0] in (" ", "\t"):
            return key, ""
        return key, val_l.rstrip()
    val = val_l.rstrip()
    if val[0] == '"':
        return key, _parse_double_quoted(val)
    if val[0] == "'":
        return key, _parse_single_quoted(val)
    return key, _strip_unquoted_inline_comment(val)


def parse_file(path=None):
    """Parse a bot.env file fresh from disk without mutating os.environ."""
    target = ENV_FILE if path is None else path
    data = {}
    if not target or not os.path.isfile(target):
        return data
    try:
        with open(target, "r", encoding="utf-8-sig") as f:
            for line in f:
                parsed = _parse_line(line)
                if parsed:
                    data[parsed[0]] = parsed[1]
    except OSError:
        pass
    return data


def format_env_value(value, force_quote=False):
    """Format a bot.env value cleanly so it round-trips with ``_parse_line`` and POSIX shells."""
    s = str("" if value is None else value).replace("\r", "").replace("\n", "")
    if s == "":
        return ""
    needs_quote = force_quote or any(
        ch in s for ch in (" ", "\t", "#", '"', "'", "$", "`", ";", "&", "|", "(", ")", "<", ">")
    )
    if not needs_quote:
        return s
    # Clean double quotes when no characters need escaping
    if not any(ch in s for ch in ('"', "$", "`", "\\")):
        return f'"{s}"'
    # Clean single quotes for literal values (including Windows paths with spaces & backslashes)
    if "'" not in s:
        return f"'{s}'"
    # Fallback: double-quoted with backslash escaping matching _parse_line
    escaped = (
        s.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("$", "\\$")
        .replace("`", "\\`")
    )
    return f'"{escaped}"'


def env_bool(name, default=False, env=None):
    """Read a boolean flag from ``env`` (default ``os.environ``)."""
    source = os.environ if env is None else env
    raw = source.get(name)
    if raw is None:
        return bool(default)
    low = str(raw).strip().lower()
    if not low:
        return bool(default)
    if low in _TRUTHY_STRS:
        return True
    if low in _FALSY_STRS:
        return False
    return bool(default)


def load(path=None):
    target = ENV_FILE if path is None else path
    try:
        with open(target, "r", encoding="utf-8-sig") as f:
            lines = f.readlines()
    except OSError:
        return False
    for line in lines:
        parsed = _parse_line(line)
        if parsed:
            os.environ.setdefault(parsed[0], parsed[1])
    return True


# ---------------------------------------------------------------------------
# Atomic file writes (preserving permissions on POSIX)
# ---------------------------------------------------------------------------

def atomic_write_bytes(path, data, mode=None):
    """Atomically write ``data`` bytes to ``path``.

    On POSIX, if ``mode`` is ``None``, preserves the existing file's mode when
    overwriting or defaults to ``0o644`` for newly created non-secret files.
    """
    abs_path = os.path.abspath(path)
    parent = os.path.dirname(abs_path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    target_mode = mode
    if target_mode is None and os.name != "nt":
        try:
            target_mode = stat.S_IMODE(os.stat(abs_path).st_mode)
        except OSError:
            target_mode = 0o644

    fd, tmp_path = tempfile.mkstemp(prefix=".botenv_", suffix=".tmp", dir=parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        if target_mode is not None and os.name != "nt":
            try:
                os.chmod(tmp_path, target_mode)
            except OSError:
                pass
        file_lock.atomic_replace(tmp_path, abs_path)
        if target_mode is not None and os.name != "nt":
            try:
                os.chmod(abs_path, target_mode)
            except OSError:
                pass
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def atomic_write_text(path, content, mode=None):
    """Atomically write UTF-8 text to ``path`` (preserving mode or defaulting to 0o644)."""
    data = str(content).encode("utf-8")
    atomic_write_bytes(path, data, mode=mode)


# ---------------------------------------------------------------------------
# Lightweight stdlib config.toml parser & consistency checker
# ---------------------------------------------------------------------------

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


def _toml_bracket_balance(text):
    depth = 0
    in_quote = None
    escaped = False
    for ch in text:
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
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
    return depth


def _parse_toml_scalar(raw):
    s = _strip_toml_comment(raw).strip()
    if not s:
        return ""
    if s.startswith('"') and s.endswith('"') and len(s) >= 2:
        try:
            return json.loads(s)
        except Exception:
            return s[1:-1]
    if s.startswith("'") and s.endswith("'") and len(s) >= 2:
        return s[1:-1]
    if s.startswith("[") and s.endswith("]"):
        inner = s[1:-1].strip()
        if not inner:
            return []
        try:
            cleaned = re.sub(r",\s*\]$", "]", s)
            parsed = json.loads(cleaned)
            if isinstance(parsed, list):
                return parsed
        except Exception:
            pass
        items = []
        for tok in inner.split(","):
            t = tok.strip()
            if not t:
                continue
            if t.startswith('"') and t.endswith('"') and len(t) >= 2:
                try:
                    items.append(json.loads(t))
                except Exception:
                    items.append(t[1:-1])
            elif t.startswith("'") and t.endswith("'") and len(t) >= 2:
                items.append(t[1:-1])
            else:
                items.append(t)
        return items
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


def parse_config_toml(path):
    """Parse sections and key/value pairs from config.toml (stdlib only, Python 3.9+).

    Supports multiline arrays and trailing comments on ``[section]`` headers.
    Returns a dict mapping section headers (e.g. '', '[management]', '[[projects]]',
    '[projects.users.roles.admin]', '[projects.platforms.options]') to key->value dicts.
    """
    sections = {"": {}}
    if not path or not os.path.isfile(path):
        return sections
    current_sec = ""
    pending_key = None
    pending_val = ""
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            for raw_line in f:
                no_comment = _strip_toml_comment(raw_line).strip()
                if pending_key is not None:
                    if no_comment:
                        pending_val += " " + no_comment
                    if _toml_bracket_balance(pending_val) <= 0:
                        sections.setdefault(current_sec, {})[pending_key] = _parse_toml_scalar(pending_val)
                        pending_key = None
                        pending_val = ""
                    continue
                if not no_comment:
                    continue
                if no_comment.startswith("[[") and no_comment.endswith("]]"):
                    current_sec = no_comment
                    sections.setdefault(current_sec, {})
                    continue
                if no_comment.startswith("[") and no_comment.endswith("]"):
                    current_sec = no_comment
                    sections.setdefault(current_sec, {})
                    continue
                if "=" in no_comment:
                    key, val = no_comment.split("=", 1)
                    key = key.strip()
                    val_s = val.strip()
                    if not key:
                        continue
                    if val_s.startswith("[") and _toml_bracket_balance(val_s) > 0:
                        pending_key = key
                        pending_val = val_s
                    else:
                        sections.setdefault(current_sec, {})[key] = _parse_toml_scalar(val_s)
    except OSError:
        pass
    return sections


def _norm_id_csv(raw):
    if isinstance(raw, (list, tuple, set)):
        raw_str = ",".join(str(x) for x in raw)
    else:
        raw_str = str(raw or "")
    cleaned = re.sub(r'[\[\]"\'\s]', "", raw_str)
    items = sorted({x for x in cleaned.split(",") if x})
    return ",".join(items)


def check_config_consistency(env, toml_cfg):
    """Validate consistency between parsed ``bot.env`` and ``config.toml`` dicts.

    Returns a list of human-readable error strings (empty if valid).
    """
    env = env or {}
    toml_cfg = toml_cfg or {}
    problems = []

    bot_qq = str(env.get("BOT_QQ_ID") or "").strip()
    if not bot_qq:
        problems.append("bot.env 里 BOT_QQ_ID 为空")
    elif not bot_qq.isdigit():
        problems.append(f"bot.env 里 BOT_QQ_ID=[{bot_qq}] 必须是纯数字 QQ 号")

    want_admins = _norm_id_csv(env.get("ADMIN_QQ_IDS", ""))
    if not want_admins:
        problems.append("bot.env 里 ADMIN_QQ_IDS 为空")
    else:
        bad_admins = [x for x in want_admins.split(",") if not x.isdigit()]
        if bad_admins:
            problems.append(f"bot.env 里 ADMIN_QQ_IDS 包含非数字 QQ 号：{','.join(bad_admins)}")

    proj = toml_cfg.get("[[projects]]", {}) or {}
    admin_role = toml_cfg.get("[projects.users.roles.admin]", {}) or {}
    plat_opts = toml_cfg.get("[projects.platforms.options]", {}) or {}

    cfg_admin_from = _norm_id_csv(proj.get("admin_from", ""))
    cfg_admin_role = _norm_id_csv(admin_role.get("user_ids", []))
    cfg_token = str(plat_opts.get("token", "") or "").strip()
    env_token = str(env.get("NAPCAT_TOKEN") or "").strip()

    if cfg_admin_from != want_admins:
        problems.append(
            f"config.toml admin_from=[{cfg_admin_from}] 与 bot.env ADMIN_QQ_IDS=[{want_admins}] 不一致"
        )
    if cfg_admin_role != want_admins:
        problems.append(
            f"config.toml users.roles.admin.user_ids=[{cfg_admin_role}] 与 bot.env ADMIN_QQ_IDS=[{want_admins}] 不一致"
        )
    if not env_token:
        problems.append("bot.env 里 NAPCAT_TOKEN 为空")
    elif cfg_token != env_token:
        problems.append("config.toml platforms.options.token 与 bot.env NAPCAT_TOKEN 不一致")

    return problems


# ---------------------------------------------------------------------------
# Executable & subprocess helpers
# ---------------------------------------------------------------------------

def _is_executable_file(path):
    if not path or not os.path.isfile(path):
        return False
    if os.name == "nt":
        return os.path.splitext(path)[1].lower() in _WIN_EXEC_EXTS
    return os.access(path, os.X_OK)


def build_subprocess_env(extra=None):
    """Return a copy of os.environ with sensible cross-platform PATH additions.

    Missing system directories are appended so active virtualenvs (e.g. .venv/bin)
    keep precedence in PATH.
    """
    env = os.environ.copy()
    path_parts = [p for p in env.get("PATH", "").split(os.pathsep) if p]
    seen = {os.path.normcase(os.path.normpath(p)) for p in path_parts}

    def _add(entry, prepend=False):
        if not entry:
            return
        expanded = os.path.expanduser(entry)
        norm = os.path.normcase(os.path.normpath(expanded))
        if norm in seen:
            return
        if os.path.isdir(expanded):
            if prepend:
                path_parts.insert(0, expanded)
            else:
                path_parts.append(expanded)
            seen.add(norm)

    if os.name == "nt":
        local_app = env.get("LOCALAPPDATA", "")
        program_files = env.get("ProgramFiles", r"C:\Program Files")
        if local_app:
            _add(os.path.join(local_app, "agy", "bin"))
            _add(os.path.join(local_app, "Programs", "antigravity"))
        _add(os.path.join(os.path.expanduser("~"), ".local", "bin"))
        _add(os.path.join(program_files, "Docker", "Docker", "resources", "bin"))
    else:
        for d in (
            os.path.join(os.path.expanduser("~"), ".local", "bin"),
            "/opt/homebrew/bin",
            "/usr/local/bin",
            "/usr/bin",
            "/bin",
            "/usr/sbin",
            "/sbin",
        ):
            _add(d, prepend=False)

    env["PATH"] = os.pathsep.join(path_parts)
    if os.name == "nt":
        env.setdefault("PYTHONUTF8", "1")
        env.setdefault("PYTHONIOENCODING", "utf-8")
    if extra:
        env.update(extra)
    return env


def subprocess_creationflags(detached=False):
    """Return Windows subprocess creationflags (0 on POSIX)."""
    if os.name != "nt":
        return 0
    no_win = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    if not detached:
        return no_win
    new_group = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
    detached_flag = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
    return new_group | detached_flag | no_win


def hidden_popen_kwargs(detached=False):
    """Return Popen kwargs to suppress console windows on Windows ({} on POSIX)."""
    flags = subprocess_creationflags(detached=detached)
    return {"creationflags": flags} if flags else {}


def resolve_agy_bin(explicit=""):
    """Locate the Antigravity CLI (`agy`) binary across macOS, Linux, and Windows."""
    raw = (explicit or os.environ.get("AGY_BIN", "")).strip()
    env_path = build_subprocess_env().get("PATH", "")

    if raw:
        expanded = os.path.expanduser(raw)
        if _is_executable_file(expanded):
            return expanded
        found = shutil.which(raw, path=env_path)
        if found:
            return found

    for name in ("agy", "antigravity"):
        found = shutil.which(name, path=env_path)
        if found:
            return found

    candidates = []
    if os.name == "nt":
        local_app = os.environ.get("LOCALAPPDATA", "")
        user_home = os.path.expanduser("~")
        if local_app:
            candidates.extend([
                os.path.join(local_app, "agy", "bin", "agy.exe"),
                os.path.join(local_app, "agy", "bin", "agy.cmd"),
                os.path.join(local_app, "Programs", "antigravity", "agy.exe"),
            ])
        candidates.extend([
            os.path.join(user_home, ".local", "bin", "agy.exe"),
            os.path.join(user_home, ".local", "bin", "agy.cmd"),
        ])
    else:
        candidates.extend([
            os.path.expanduser("~/.local/bin/agy"),
            "/opt/homebrew/bin/agy",
            "/usr/local/bin/agy",
            "/usr/bin/agy",
        ])

    for cand in candidates:
        if _is_executable_file(cand):
            return cand

    return raw or ("agy.exe" if os.name == "nt" else "agy")


def resolve_python_bin(project_dir=_SCRIPT_DIR):
    """Prefer the project's local .venv interpreter if available; else sys.executable."""
    if os.name == "nt":
        venv_py = os.path.join(project_dir, ".venv", "Scripts", "python.exe")
    else:
        venv_py = os.path.join(project_dir, ".venv", "bin", "python3")
    if _is_executable_file(venv_py):
        return venv_py
    return sys.executable or ("python" if os.name == "nt" else "python3")


def resolve_cc_connect_bin(project_dir=_SCRIPT_DIR):
    """Locate the local patched cc-connect binary across macOS, Linux, and Windows."""
    candidates = []
    if os.name == "nt":
        candidates.append(os.path.join(project_dir, ".tools", "cc-connect.exe"))
    else:
        candidates.append(os.path.join(project_dir, ".tools", "cc-connect"))
    for cand in candidates:
        if _is_executable_file(cand):
            return cand
    found = shutil.which("cc-connect", path=build_subprocess_env().get("PATH", ""))
    if found:
        return found
    if os.name != "nt" and _is_executable_file("/opt/homebrew/bin/cc-connect"):
        return "/opt/homebrew/bin/cc-connect"
    return candidates[0]


LOADED = load()
