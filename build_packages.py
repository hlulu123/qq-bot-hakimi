#!/usr/bin/env python3
"""Build sanitized, ready-to-share macOS and Windows lazy-dog installer packages.

Outputs in ./dist/:
  - QQ机器人-哈基米-macOS-通用版.zip   (Apple Silicon + Intel Universal)
  - QQ机器人-哈基米-macOS-通用版.dmg   (macOS drag-and-drop disk image)
  - QQ机器人-哈基米-Windows-x64懒人包.zip (Windows 10/11 x64 one-click bundle)
  - SHA256SUMS.txt

Can be re-run anytime after editing the bot to regenerate fresh packages.
"""

import argparse
import getpass
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import urllib.parse
import zipfile

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

import bot_env  # noqa: E402

INSTALLER_DIR = os.path.join(PROJECT_DIR, "installer")
BIN_DIR = os.path.join(INSTALLER_DIR, "binaries")
DIST_DIR = os.path.join(PROJECT_DIR, "dist")

PKG_FOLDER_NAME = "QQ机器人-哈基米"
MAC_ZIP_NAME = "QQ机器人-哈基米-macOS-通用版.zip"
MAC_DMG_NAME = "QQ机器人-哈基米-macOS-通用版.dmg"
WIN_ZIP_NAME = "QQ机器人-哈基米-Windows-x64懒人包.zip"

CORE_PY_FILES = [
    "agy-acp-wrapper.py",
    "onebot-ws-guard.py",
    "onebot_common.py",
    "bot_permissions.py",
    "bot_plugins.py",
    "token_usage.py",
    "qq_actions.py",
    "group_requests.py",
    "chat_history.py",
    "user_memory.py",
    "repeat_chain.py",
    "signal_filter.py",
    "prompt_diet.py",
    "bot_env.py",
    "file_lock.py",
    "bot_runner.py",
    "setup_wizard.py",
]

_TEMPLATE_DUMMY_VALUES = frozenset({
    "1000000001",
    "1000000002",
    "100000001",
    "1234567890",
    "2854196310",
    "root",
})

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "0.0.0.0", ""})


def _is_non_loopback_url(val):
    s = str(val or "").strip()
    if not s or "://" not in s:
        return False
    try:
        host = (urllib.parse.urlparse(s).hostname or "").strip().lower()
    except Exception:
        return True
    return host not in _LOOPBACK_HOSTS


def _load_forbidden_strings():
    """Dynamically gather local secrets, IDs, and paths that must never leak into packages."""
    raw_items = []

    def _add(val):
        if val is None:
            return
        s = str(val).strip()
        if len(s) < 3:
            return
        if s.startswith("<") and s.endswith(">"):
            return
        if s in _TEMPLATE_DUMMY_VALUES or s == "*":
            return
        raw_items.append(s)

    def _add_csv(val):
        if isinstance(val, (list, tuple, set)):
            for item in val:
                _add(item)
            return
        for tok in re.split(r"[,，\s]+", str(val or "")):
            _add(tok)

    def _add_url_if_non_loopback(val):
        if _is_non_loopback_url(val):
            _add(val)

    def _walk_json_secrets(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                kl = str(k).lower()
                if kl in ("token", "accesstoken", "access_token", "totpsecret", "packetserver", "musicsignurl", "imagedownloadproxy"):
                    if isinstance(v, str):
                        _add(v)
                elif kl == "url" and isinstance(v, str):
                    _add_url_if_non_loopback(v)
                else:
                    _walk_json_secrets(v)
        elif isinstance(obj, list):
            for item in obj:
                _walk_json_secrets(item)

    # 1. Local bot.env (tokens, bot/admin QQ IDs, private keys, group whitelist, local paths)
    env_data = bot_env.parse_file(os.path.join(PROJECT_DIR, "bot.env"))
    _add(env_data.get("BOT_QQ_ID"))
    _add_csv(env_data.get("ADMIN_QQ_IDS"))
    _add_csv(env_data.get("IGNORED_BOT_QQ_IDS"))
    _add(env_data.get("NAPCAT_TOKEN"))
    _add(env_data.get("MEMORY_EMBED_API_KEY"))
    _add(env_data.get("MEMORY_EMBED_URL"))
    _add_csv(env_data.get("GROUP_WHITELIST_IDS"))
    _add(env_data.get("FORWARD_NICKNAME"))
    _add_url_if_non_loopback(env_data.get("NAPCAT_HTTP_URL"))
    _add_url_if_non_loopback(env_data.get("NAPCAT_WS_URL"))
    for path_key in (
        "AGY_WORK_DIR",
        "CHAT_SANDBOX_DIR",
        "MEMORY_DB_PATH",
        "USER_MEMORY_DIR",
        "MEMORY_EXTRACTOR_CWD",
    ):
        _add(env_data.get(path_key))

    # 2. Local config.toml (management token, admin_from, admin user_ids, platform token, paths)
    toml_path = os.path.join(PROJECT_DIR, "config.toml")
    toml_data = bot_env.parse_config_toml(toml_path)
    _add((toml_data.get("") or {}).get("data_dir"))
    _add((toml_data.get("[management]") or {}).get("token"))
    _add_csv((toml_data.get("[[projects]]") or {}).get("admin_from"))
    _add_csv((toml_data.get("[projects.users.roles.admin]") or {}).get("user_ids"))
    _add((toml_data.get("[projects.agent.options]") or {}).get("work_dir"))
    plat_opts = toml_data.get("[projects.platforms.options]") or {}
    _add(plat_opts.get("token"))
    _add_csv(plat_opts.get("allow_from"))
    _add_url_if_non_loopback(plat_opts.get("ws_url"))
    _add_url_if_non_loopback(plat_opts.get("http_url"))
    # Regex fallback across multi-section config.toml
    if os.path.isfile(toml_path):
        try:
            with open(toml_path, "r", encoding="utf-8-sig") as f:
                toml_raw = f.read()
            for m in re.finditer(r'^\s*(?:token|admin_from)\s*=\s*"([^"]+)"', toml_raw, re.MULTILINE):
                _add_csv(m.group(1))
        except OSError:
            pass

    # 3. Local napcat/config/* (WebUI login token, OneBot tokens, embedded QQ IDs in filenames)
    napcat_cfg_dir = os.path.join(PROJECT_DIR, "napcat", "config")
    if os.path.isdir(napcat_cfg_dir):
        for fname in sorted(os.listdir(napcat_cfg_dir)):
            fpath = os.path.join(napcat_cfg_dir, fname)
            if not os.path.isfile(fpath) or not fname.endswith(".json"):
                continue
            m_qq = re.search(r"_(\d{6,})\.json$", fname)
            if m_qq:
                _add(m_qq.group(1))
            try:
                with open(fpath, "r", encoding="utf-8-sig") as f:
                    obj = json.load(f)
            except Exception:
                continue
            if fname == "webui.json" and isinstance(obj, dict):
                _add(obj.get("token"))
                _add(obj.get("autoLoginAccount"))
                _add(obj.get("totpSecret"))
                ip_wl = obj.get("ipWhitelist")
                if isinstance(ip_wl, list):
                    for ip in ip_wl:
                        if str(ip).strip() not in _LOOPBACK_HOSTS:
                            _add(ip)
                elif isinstance(ip_wl, str) and ip_wl.strip() not in _LOOPBACK_HOSTS:
                    _add(ip_wl)
            else:
                _walk_json_secrets(obj)

    # 4. Local runtime data/ (real group IDs & QQ UIDs >= 7 digits from sessions & memory.db)
    sessions_dir = os.path.join(PROJECT_DIR, "data", "sessions")
    if os.path.isdir(sessions_dir):
        for fname in sorted(os.listdir(sessions_dir)):
            for m in re.finditer(r"(?:^|[^0-9])([1-9]\d{6,11})(?:[^0-9]|$)", fname):
                _add(m.group(1))
            if fname.endswith(".json"):
                fpath = os.path.join(sessions_dir, fname)
                try:
                    with open(fpath, "r", encoding="utf-8-sig") as f:
                        head = f.read(4096)
                    for m in re.finditer(r"qq:[gu]:([1-9]\d{6,11})", head):
                        _add(m.group(1))
                except OSError:
                    pass

    mem_db = os.path.join(PROJECT_DIR, "data", "memory.db")
    if os.path.isfile(mem_db):
        conn = None
        try:
            conn = sqlite3.connect(f"file:{mem_db}?mode=ro", uri=True)
            for sql in (
                "SELECT DISTINCT group_id FROM groups",
                "SELECT DISTINCT entity_id FROM profiles",
                "SELECT DISTINCT owner_uid FROM memories",
                "SELECT DISTINCT scope FROM memories",
            ):
                try:
                    for (val,) in conn.execute(sql).fetchall():
                        for m in re.finditer(r"([1-9]\d{6,11})", str(val or "")):
                            _add(m.group(1))
                except sqlite3.Error:
                    pass
        except Exception:
            pass
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    # 5. Local project path, user home path & username
    _add(PROJECT_DIR)
    home_dir = os.path.expanduser("~")
    if home_dir and home_dir != "~":
        _add(home_dir)
        _add(os.path.basename(home_dir.rstrip("/\\")))
    try:
        _add(getpass.getuser())
    except Exception:
        pass

    # 6. Optional local gitignored .build_secrets.local (one secret/nickname per line)
    local_secrets_file = os.path.join(PROJECT_DIR, ".build_secrets.local")
    if os.path.isfile(local_secrets_file):
        try:
            with open(local_secrets_file, "r", encoding="utf-8-sig") as f:
                for line in f:
                    s = line.strip()
                    if not s or s.startswith("#"):
                        continue
                    _add(s)
        except OSError:
            pass

    seen = set()
    result = []
    for item in raw_items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _load_binary_forbidden_bytes(forbidden_strings):
    """High-confidence byte needles for scanning compiled Go binaries (no short false positives)."""
    needles = []
    seen = set()

    def _add_str(s):
        if not s:
            return
        b = s.encode("utf-8")
        if b not in seen:
            seen.add(b)
            needles.append((s, b))

    _add_str(PROJECT_DIR)
    home_dir = os.path.expanduser("~")
    if home_dir and home_dir != "~":
        _add_str(home_dir)
    usernames = set()
    if home_dir and home_dir != "~":
        usernames.add(os.path.basename(home_dir.rstrip("/\\")))
    try:
        usernames.add(getpass.getuser())
    except Exception:
        pass
    for u in sorted(usernames):
        if u and u not in _TEMPLATE_DUMMY_VALUES:
            for pat in (
                f"/{u}/",
                f"\\{u}\\",
                f"/Users/{u}",
                f"/home/{u}",
                f"C:\\Users\\{u}",
            ):
                _add_str(pat)

    for item in forbidden_strings:
        if len(item) >= 8:
            _add_str(item)
    return needles


# Strings that MUST NEVER appear in any distributed package (loaded dynamically from local state).
FORBIDDEN_STRINGS = _load_forbidden_strings()
BINARY_FORBIDDEN_BYTES = _load_binary_forbidden_bytes(FORBIDDEN_STRINGS)

FORBIDDEN_EXACT_NAMES = {
    "bot.env",
    "config.toml",
    "webui.json",
    "passkey.json",
    ".build_secrets.local",
    ".DS_Store",
    "Thumbs.db",
    "desktop.ini",
}

FORBIDDEN_SUFFIXES = (
    ".log",
    ".pid",
    ".bak",
    ".pptx",
    ".pyc",
    ".pyo",
    ".sock",
    ".db",
    ".db-wal",
    ".db-shm",
    ".sqlite",
    ".sqlite3",
)

BINARY_EXTS = {
    ".exe", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp",
    ".woff", ".woff2", ".ttf", ".zip", ".dmg", ".bin",
}


def _copy_file(src, dst, mode=None):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(src, dst)
    if mode is not None and os.name != "nt":
        os.chmod(dst, mode)


def _copy_tree_filtered(src_root, dst_root):
    for root, dirs, files in os.walk(src_root):
        dirs[:] = [
            d for d in sorted(dirs)
            if d not in ("__pycache__", ".git", ".pytest_cache", "node_modules")
        ]
        rel = os.path.relpath(root, src_root)
        target_dir = dst_root if rel == "." else os.path.join(dst_root, rel)
        os.makedirs(target_dir, exist_ok=True)
        for name in sorted(files):
            if name in (".DS_Store", "Thumbs.db", "desktop.ini") or name.endswith((".pyc", ".pyo")):
                continue
            _copy_file(os.path.join(root, name), os.path.join(target_dir, name))


def _touch_gitkeep(path):
    os.makedirs(path, exist_ok=True)
    keep = os.path.join(path, ".gitkeep")
    if not os.path.exists(keep):
        with open(keep, "w", encoding="utf-8") as f:
            f.write("")


def _ensure_cc_connect_binaries(rebuild=False):
    mac_bin = os.path.join(BIN_DIR, "cc-connect-darwin-universal")
    win_bin = os.path.join(BIN_DIR, "cc-connect-windows-amd64.exe")
    patch_file = os.path.join(PROJECT_DIR, "patches", "cc-connect-local.patch")
    if not rebuild and os.path.isfile(mac_bin) and os.path.isfile(win_bin):
        if os.path.isfile(patch_file):
            patch_mtime = os.path.getmtime(patch_file)
            if patch_mtime > min(os.path.getmtime(mac_bin), os.path.getmtime(win_bin)):
                print("[build] 检测到本地补丁更新于预编译二进制，自动重新交叉编译 cc-connect...")
                rebuild = True
    if not rebuild and os.path.isfile(mac_bin) and os.path.isfile(win_bin):
        print(f"[build] 复用已编译的 cc-connect 二进制: {BIN_DIR}")
        return mac_bin, win_bin

    cc_src = os.path.join(PROJECT_DIR, ".tools", "cc-connect-src")
    if not os.path.isdir(cc_src):
        raise RuntimeError(f"找不到 cc-connect 源码目录: {cc_src}")

    os.makedirs(BIN_DIR, exist_ok=True)
    ldflags = "-s -w -X main.version=1.5.3-qqfix -X main.commit=8f442161-patched"
    arm64_out = os.path.join(BIN_DIR, "cc-connect-darwin-arm64")
    amd64_out = os.path.join(BIN_DIR, "cc-connect-darwin-amd64")

    targets = [
        ("darwin", "arm64", arm64_out),
        ("darwin", "amd64", amd64_out),
        ("windows", "amd64", win_bin),
    ]
    for goos, goarch, out_path in targets:
        print(f"[build] 正在交叉编译 cc-connect ({goos}/{goarch})...")
        env = os.environ.copy()
        env["CGO_ENABLED"] = "0"
        env["GOOS"] = goos
        env["GOARCH"] = goarch
        subprocess.run(
            [
                "go", "build",
                "-trimpath",
                "-buildvcs=false",
                "-tags", "no_web",
                "-ldflags", ldflags,
                "-o", out_path,
                "./cmd/cc-connect",
            ],
            cwd=cc_src,
            env=env,
            check=True,
        )

    print("[build] 合并 macOS Universal Binary (arm64 + x86_64)...")
    subprocess.run(
        ["lipo", "-create", "-output", mac_bin, arm64_out, amd64_out],
        check=True,
    )
    os.chmod(mac_bin, 0o755)
    for tmp in (arm64_out, amd64_out):
        if os.path.exists(tmp):
            os.remove(tmp)
    return mac_bin, win_bin


def _stage_common(stage_root):
    # 1. Core Python scripts & memory_engine package
    for fname in CORE_PY_FILES:
        src = os.path.join(PROJECT_DIR, fname)
        if not os.path.isfile(src):
            raise FileNotFoundError(f"缺少核心脚本: {src}")
        _copy_file(src, os.path.join(stage_root, fname))
    _copy_tree_filtered(
        os.path.join(PROJECT_DIR, "memory_engine"),
        os.path.join(stage_root, "memory_engine"),
    )
    _copy_tree_filtered(
        os.path.join(PROJECT_DIR, "plugins"),
        os.path.join(stage_root, "plugins"),
    )

    # 2. WebUI
    _copy_tree_filtered(
        os.path.join(PROJECT_DIR, "webui"),
        os.path.join(stage_root, "webui"),
    )

    # 3. Persona (GEMINI.md ONLY — never copy personal files from workspace/)
    gemini_md = os.path.join(PROJECT_DIR, "workspace", "GEMINI.md")
    _copy_file(gemini_md, os.path.join(stage_root, "workspace", "GEMINI.md"))
    _copy_file(gemini_md, os.path.join(stage_root, "chat-sandbox", "GEMINI.md"))

    # 4. Sanitized docs & patches
    tpl_dir = os.path.join(INSTALLER_DIR, "templates")
    _copy_file(
        os.path.join(tpl_dir, "CHARACTER_CARD_PUBLIC.md"),
        os.path.join(stage_root, "docs", "CHARACTER_CARD.md"),
    )
    _copy_file(
        os.path.join(PROJECT_DIR, "patches", "cc-connect-local.patch"),
        os.path.join(stage_root, "patches", "cc-connect-local.patch"),
    )
    _copy_file(
        os.path.join(tpl_dir, "README_PACKAGE.md"),
        os.path.join(stage_root, "README.md"),
    )

    # 5. Clean config templates
    _copy_file(
        os.path.join(tpl_dir, "bot.env.example"),
        os.path.join(stage_root, "bot.env.example"),
    )
    _copy_file(
        os.path.join(tpl_dir, "config.toml.example"),
        os.path.join(stage_root, "config.toml.example"),
    )
    _copy_file(
        os.path.join(tpl_dir, "docker-compose.napcat.yml.example"),
        os.path.join(stage_root, "docker-compose.napcat.yml"),
    )
    _copy_file(
        os.path.join(tpl_dir, "napcat", "onebot11.json.example"),
        os.path.join(stage_root, "napcat", "config", "onebot11.json.example"),
    )
    _copy_file(
        os.path.join(tpl_dir, "napcat", "webui.json.example"),
        os.path.join(stage_root, "napcat", "config", "webui.json.example"),
    )

    # 6. Empty runtime directories
    for rel_dir in (
        os.path.join("data", "run"),
        os.path.join("data", "sessions"),
        os.path.join("data", "user_memories"),
        os.path.join("data", "memory_extractor_cwd"),
        os.path.join("napcat", "QQ"),
    ):
        _touch_gitkeep(os.path.join(stage_root, rel_dir))


def _stage_macos(stage_root, mac_bin):
    _stage_common(stage_root)
    _copy_file(mac_bin, os.path.join(stage_root, ".tools", "cc-connect"), mode=0o755)

    mac_src = os.path.join(INSTALLER_DIR, "macos")
    for name in sorted(os.listdir(mac_src)):
        src = os.path.join(mac_src, name)
        if not os.path.isfile(src) or name == ".DS_Store":
            continue
        mode = 0o755 if name.endswith((".command", ".sh")) else 0o644
        _copy_file(src, os.path.join(stage_root, name), mode=mode)


def _stage_windows(stage_root, win_bin):
    _stage_common(stage_root)
    _copy_file(win_bin, os.path.join(stage_root, ".tools", "cc-connect.exe"))

    win_src = os.path.join(INSTALLER_DIR, "windows")
    for name in sorted(os.listdir(win_src)):
        src = os.path.join(win_src, name)
        if name == ".DS_Store":
            continue
        if os.path.isfile(src):
            _copy_file(src, os.path.join(stage_root, name))

    scripts_src = os.path.join(win_src, "scripts")
    if os.path.isdir(scripts_src):
        for name in sorted(os.listdir(scripts_src)):
            src = os.path.join(scripts_src, name)
            if os.path.isfile(src) and name != ".DS_Store":
                _copy_file(src, os.path.join(stage_root, "installer", name))


def audit_staged_package(stage_root, platform_name):
    """Fail build immediately if any secret, personal artifact, or encoding bug is found."""
    errors = []

    for root, dirs, files in os.walk(stage_root):
        for name in files:
            fpath = os.path.join(root, name)
            rel = os.path.relpath(fpath, stage_root)

            ext = os.path.splitext(name)[1].lower()
            rel_norm = rel.replace("\\", "/")

            if name in FORBIDDEN_EXACT_NAMES:
                errors.append(f"[{platform_name}] 禁止打包敏感/运行态文件: {rel}")
            if name.endswith(FORBIDDEN_SUFFIXES):
                errors.append(f"[{platform_name}] 禁止打包临时/日志/个人后缀文件: {rel}")
            if name.startswith(("onebot11_", "nt_qq_")) or (
                name.startswith("napcat_") and (ext != ".py" or rel_norm.startswith("napcat/"))
            ):
                errors.append(f"[{platform_name}] 禁止打包个人账号配置: {rel}")

            is_go_bin = rel_norm in (".tools/cc-connect", ".tools/cc-connect.exe")
            is_bin = ext in BINARY_EXTS or is_go_bin
            if is_go_bin:
                with open(fpath, "rb") as f:
                    raw_bin = f.read()
                for label, needle_bytes in BINARY_FORBIDDEN_BYTES:
                    if needle_bytes in raw_bin:
                        errors.append(f"[{platform_name}] 二进制检测到本机路径/敏感信息 '{label}' 于: {rel}")
                continue
            if is_bin:
                continue

            with open(fpath, "rb") as f:
                raw = f.read()

            # Check Windows script encodings & line endings
            if platform_name == "windows":
                if ext == ".bat":
                    if raw.startswith(b"\xef\xbb\xbf"):
                        errors.append(f"[windows] .bat 文件不能带 UTF-8 BOM: {rel}")
                    if re.search(rb"(?<!\r)\n", raw):
                        errors.append(f"[windows] .bat 文件必须使用 CRLF 换行: {rel}")
                elif ext == ".ps1":
                    if not raw.startswith(b"\xef\xbb\xbf"):
                        errors.append(f"[windows] .ps1 文件必须带 UTF-8 BOM（兼容 PowerShell 5.1 中文）: {rel}")
                    if re.search(rb"(?<!\r)\n", raw):
                        errors.append(f"[windows] .ps1 文件必须使用 CRLF 换行: {rel}")

            # Check macOS script encodings & executable bits
            if platform_name == "macos" and ext in (".command", ".sh"):
                if b"\r\n" in raw:
                    errors.append(f"[macos] shell 脚本不能包含 CRLF: {rel}")
                st_mode = os.stat(fpath).st_mode
                if not (st_mode & stat.S_IXUSR):
                    errors.append(f"[macos] 脚本缺少可执行权限 +x: {rel}")

            # Scan text for forbidden personal strings
            try:
                text = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                continue

            for needle in FORBIDDEN_STRINGS:
                if needle in text:
                    errors.append(f"[{platform_name}] 检测到隐私/个人敏感词 '{needle}' 于: {rel}")

    if errors:
        msg = "\n".join("  - " + e for e in errors)
        raise RuntimeError(f"打包安全与格式审计未通过：\n{msg}")
    print(f"[audit] {platform_name} 包安全脱敏 & 编码格式校验全部通过！")


def create_zip_archive(stage_parent, folder_name, out_zip, preserve_unix_perms=False):
    """Create a UTF-8 safe ZIP archive (works seamlessly on Windows Explorer & macOS)."""
    os.makedirs(os.path.dirname(out_zip), exist_ok=True)
    if os.path.exists(out_zip):
        os.remove(out_zip)

    base = os.path.join(stage_parent, folder_name)
    with zipfile.ZipFile(out_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for root, dirs, files in os.walk(base):
            dirs.sort()
            files.sort()
            for fname in files:
                abs_path = os.path.join(root, fname)
                arcname = os.path.relpath(abs_path, stage_parent).replace(os.sep, "/")
                st = os.stat(abs_path)
                mtime = time.localtime(st.st_mtime)
                date_time = (
                    max(1980, mtime.tm_year),
                    mtime.tm_mon,
                    mtime.tm_mday,
                    mtime.tm_hour,
                    mtime.tm_min,
                    mtime.tm_sec,
                )
                zi = zipfile.ZipInfo(arcname, date_time=date_time)
                zi.compress_type = zipfile.ZIP_DEFLATED
                # Force UTF-8 filename flag (bit 11) for non-ASCII paths so Windows Explorer decodes Chinese cleanly.
                if any(ord(c) > 127 for c in arcname):
                    zi.flag_bits |= 0x800
                if preserve_unix_perms:
                    zi.create_system = 3  # Unix
                    zi.external_attr = (st.st_mode & 0xFFFF) << 16
                else:
                    zi.create_system = 0  # DOS/Windows
                    zi.external_attr = 0x20  # Archive attribute
                with open(abs_path, "rb") as fp:
                    zf.writestr(zi, fp.read())

    size_mb = os.path.getsize(out_zip) / (1024 * 1024)
    print(f"[pack] 已生成: {out_zip} ({size_mb:.1f} MB)")


def create_macos_dmg(stage_parent, folder_name, out_dmg):
    """Create a compressed macOS .dmg disk image containing the ready-to-drag bot folder."""
    if sys.platform != "darwin":
        print("[pack] 非 macOS 环境，跳过 .dmg 构建")
        return
    os.makedirs(os.path.dirname(out_dmg), exist_ok=True)
    if os.path.exists(out_dmg):
        os.remove(out_dmg)

    with tempfile.TemporaryDirectory(prefix="qqbot-dmg-") as dmg_src:
        shutil.copytree(
            os.path.join(stage_parent, folder_name),
            os.path.join(dmg_src, folder_name),
            symlinks=True,
        )
        # Put a top-level read-first file right next to the folder inside the DMG
        readme_first = os.path.join(stage_parent, folder_name, "首次运行必读.txt")
        if os.path.isfile(readme_first):
            shutil.copy2(readme_first, os.path.join(dmg_src, "首次运行必读.txt"))

        subprocess.run(
            [
                "hdiutil", "create",
                "-volname", PKG_FOLDER_NAME,
                "-srcfolder", dmg_src,
                "-ov",
                "-format", "UDZO",
                "-imagekey", "zlib-level=6",
                out_dmg,
            ],
            check=True,
        )
    size_mb = os.path.getsize(out_dmg) / (1024 * 1024)
    print(f"[pack] 已生成: {out_dmg} ({size_mb:.1f} MB)")


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    parser = argparse.ArgumentParser(description="构建 QQ 机器人「哈基米」macOS 与 Windows 懒人安装包")
    parser.add_argument("--rebuild-go", action="store_true", help="强制重新交叉编译 cc-connect 二进制")
    parser.add_argument("--no-dmg", action="store_true", help="跳过 macOS .dmg 生成，仅生成 .zip")
    args = parser.parse_args()

    mac_bin, win_bin = _ensure_cc_connect_binaries(rebuild=args.rebuild_go)
    os.makedirs(DIST_DIR, exist_ok=True)

    artifacts = []

    with tempfile.TemporaryDirectory(prefix="qqbot-stage-") as tmp_root:
        # ---- macOS ----
        mac_stage_parent = os.path.join(tmp_root, "mac")
        mac_stage = os.path.join(mac_stage_parent, PKG_FOLDER_NAME)
        os.makedirs(mac_stage, exist_ok=True)
        print("[stage] 正在组装 macOS 通用版 (Apple Silicon + Intel)...")
        _stage_macos(mac_stage, mac_bin)
        audit_staged_package(mac_stage, "macos")

        mac_zip = os.path.join(DIST_DIR, MAC_ZIP_NAME)
        create_zip_archive(mac_stage_parent, PKG_FOLDER_NAME, mac_zip, preserve_unix_perms=True)
        artifacts.append(mac_zip)

        if not args.no_dmg:
            mac_dmg = os.path.join(DIST_DIR, MAC_DMG_NAME)
            create_macos_dmg(mac_stage_parent, PKG_FOLDER_NAME, mac_dmg)
            if os.path.isfile(mac_dmg):
                artifacts.append(mac_dmg)

        # ---- Windows ----
        win_stage_parent = os.path.join(tmp_root, "win")
        win_stage = os.path.join(win_stage_parent, PKG_FOLDER_NAME)
        os.makedirs(win_stage, exist_ok=True)
        print("[stage] 正在组装 Windows x64 懒人包...")
        _stage_windows(win_stage, win_bin)
        audit_staged_package(win_stage, "windows")

        win_zip = os.path.join(DIST_DIR, WIN_ZIP_NAME)
        create_zip_archive(win_stage_parent, PKG_FOLDER_NAME, win_zip, preserve_unix_perms=False)
        artifacts.append(win_zip)

    # Write SHA256SUMS.txt
    sums_path = os.path.join(DIST_DIR, "SHA256SUMS.txt")
    lines = [f"{_sha256_file(p)}  {os.path.basename(p)}" for p in artifacts]
    with open(sums_path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    print(f"[pack] 已写入校验文件: {sums_path}")
    for line in lines:
        print(f"  {line}")


if __name__ == "__main__":
    main()
