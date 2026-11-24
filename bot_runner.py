#!/usr/bin/env python3
"""Cross-platform runner, supervisor, and diagnostics CLI for the QQ Bot.

Zero external dependencies (Python 3.9+ stdlib only; macOS, Linux, Windows).

Subcommands:
    start             Run cc-connect in foreground (with ws-guard supervised in background)
    start-bg          Start ws-guard + cc-connect completely in background
    stop              Stop cc-connect and ws-guard (and their supervisors)
    stop-all          Stop bot services and Web UI
    restart           Stop and start-bg (validates config before stopping)
    restart-ws-guard  Restart only ws-guard without touching cc-connect
    status [--json]   Show process states, PIDs, and local port reachability
    webui <action>    Manage local Web UI (start|stop|restart|status|fg|open)
    napcat <action>   Manage NapCat container / check native reachability (up|down|status|logs)
    sync-sandbox      Atomically copy workspace/GEMINI.md -> chat-sandbox/GEMINI.md
    doctor            Run full environment, config, NapCat, and service diagnostics

Internal supervisor modes:
    --supervise-ws-guard   Daemon loop supervising onebot-ws-guard.py
    --supervise-cc         Daemon loop supervising cc-connect
"""

import argparse
import datetime
import json
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import webbrowser


def _configure_stdio_utf8():
    """Ensure UTF-8 stdout/stderr across Windows consoles and redirected logs."""
    for stream in (sys.stdout, sys.stderr):
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
    """Inspect CLI argv for --project-dir before importing bot_env so BOT_ENV_FILE is isolated."""
    for idx, tok in enumerate(argv):
        if tok == "--project-dir" and idx + 1 < len(argv):
            return os.path.abspath(os.path.expanduser(argv[idx + 1])), True
        if tok.startswith("--project-dir="):
            val = tok.split("=", 1)[1]
            if val:
                return os.path.abspath(os.path.expanduser(val)), True
    return _SCRIPT_DIR, False


if __name__ == "__main__":
    _cli_project_dir, _cli_project_explicit = _peek_project_dir_from_argv(sys.argv[1:])
    if _cli_project_explicit:
        os.environ["BOT_ENV_FILE"] = os.path.join(_cli_project_dir, "bot.env")
    else:
        os.environ.setdefault("BOT_ENV_FILE", os.path.join(_cli_project_dir, "bot.env"))

import bot_env  # noqa: E402
import file_lock  # noqa: E402


# ---------------------------------------------------------------------------
# Path & PID file helpers
# ---------------------------------------------------------------------------

def _paths(project_dir):
    root = os.path.abspath(project_dir)
    data_dir = os.path.join(root, "data")
    run_dir = os.path.join(data_dir, "run")
    return {
        "root": root,
        "env": os.path.join(root, "bot.env"),
        "config": os.path.join(root, "config.toml"),
        "compose": os.path.join(root, "docker-compose.napcat.yml"),
        "data_dir": data_dir,
        "run_dir": run_dir,
        "workspace_dir": os.path.join(root, "workspace"),
        "sandbox_dir": os.path.join(root, "chat-sandbox"),
        "workspace_gemini": os.path.join(root, "workspace", "GEMINI.md"),
        "sandbox_gemini": os.path.join(root, "chat-sandbox", "GEMINI.md"),
        "napcat_webui_json": os.path.join(root, "napcat", "config", "webui.json"),
        "guard_script": os.path.join(root, "onebot-ws-guard.py"),
        "ws_guard_log": os.path.join(data_dir, "ws-guard.log"),
        "cc_log": os.path.join(data_dir, "cc-connect.log"),
        "webui_log": os.path.join(data_dir, "webui.log"),
        "pid_ws_sup": os.path.join(run_dir, "ws-guard-supervisor.pid"),
        "pid_ws_worker": os.path.join(run_dir, "ws-guard-worker.pid"),
        "pid_start": os.path.join(run_dir, "start.pid"),
        "pid_cc": os.path.join(run_dir, "cc-connect.pid"),
        "pid_webui": os.path.join(run_dir, "webui.pid"),
    }


def _now_str():
    return time.strftime("%Y-%m-%d %H:%M:%S")


_atomic_write_bytes = bot_env.atomic_write_bytes
_is_executable = bot_env._is_executable_file


def _read_pid(path):
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            raw = f.read().strip()
        val = int(raw)
        return val if val > 0 else None
    except Exception:
        return None


def _write_pid(path, pid):
    _atomic_write_bytes(path, f"{int(pid)}\n".encode("utf-8"))


def _remove_pid(path, expected_pid=None):
    try:
        if expected_pid is not None:
            cur = _read_pid(path)
            if cur is not None and cur != int(expected_pid):
                return
        if os.path.exists(path):
            os.unlink(path)
    except OSError:
        pass


def _rotate_log_if_needed(log_path, max_bytes=10 * 1024 * 1024, backups=3):
    """Rotate ``log_path`` -> ``log_path.1``..``log_path.<backups>`` if size >= ``max_bytes``."""
    if not log_path or max_bytes <= 0 or backups <= 0:
        return False
    try:
        if not os.path.isfile(log_path) or os.path.getsize(log_path) < max_bytes:
            return False
    except OSError:
        return False

    rotated = False
    for idx in range(backups, 0, -1):
        src = f"{log_path}.{idx - 1}" if idx > 1 else log_path
        dst = f"{log_path}.{idx}"
        if not os.path.exists(src):
            continue
        try:
            file_lock.atomic_replace(src, dst)
            if idx == 1:
                rotated = True
        except OSError:
            pass
    return rotated


def _read_tail_lines(path, count=20):
    """Read the last ``count`` lines of ``path`` by seeking backward in chunks from EOF."""
    if count <= 0 or not os.path.isfile(path):
        return []
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            file_size = f.tell()
            if file_size == 0:
                return []
            chunk_size = 8192
            pos = file_size
            buf = bytearray()
            nl_count = 0
            while pos > 0 and nl_count <= count:
                read_len = min(chunk_size, pos)
                pos -= read_len
                f.seek(pos, os.SEEK_SET)
                chunk = f.read(read_len)
                nl_count += chunk.count(b"\n")
                buf[:0] = chunk
        text = bytes(buf).decode("utf-8", errors="replace")
        return text.splitlines()[-count:]
    except OSError:
        return []


def _append_log_line(path, text):
    try:
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "a", encoding="utf-8", errors="replace") as f:
            f.write(text)
            if not text.endswith("\n"):
                f.write("\n")
            f.flush()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Config parsers (bot.env & config.toml) + validation (shared via bot_env)
# ---------------------------------------------------------------------------

def parse_bot_env(env_path):
    """Parse bot.env fresh from disk (UTF-8 with optional BOM)."""
    return bot_env.parse_file(env_path)


def parse_config_toml(config_path):
    """Parse sections and key/value pairs from config.toml (stdlib only, Python 3.9+)."""
    return bot_env.parse_config_toml(config_path)


def _mask_secret(val):
    s = str(val or "").strip()
    if not s:
        return "(空)"
    if len(s) <= 8:
        return "*" * len(s)
    return f"{s[:4]}****{s[-4:]}"


def validate_config(project_dir, check_cc_bin=True):
    """Validate bot.env, config.toml consistency and cc-connect binary.

    Returns: (ok: bool, problems: list[str], info: dict)
    """
    p = _paths(project_dir)
    problems = []
    info = {
        "env_exists": os.path.isfile(p["env"]),
        "config_exists": os.path.isfile(p["config"]),
        "env": {},
        "toml": {},
        "cc_bin": "",
        "cc_bin_ok": False,
    }

    if not info["env_exists"]:
        problems.append(f"找不到 {p['env']}（请先运行 setup_wizard.py 生成配置）")
    if not info["config_exists"]:
        problems.append(f"找不到 {p['config']}（请先运行 setup_wizard.py 生成配置）")

    if info["env_exists"] and info["config_exists"]:
        env = parse_bot_env(p["env"])
        toml = parse_config_toml(p["config"])
        info["env"] = env
        info["toml"] = toml
        problems.extend(bot_env.check_config_consistency(env, toml))

    cc_bin = bot_env.resolve_cc_connect_bin(p["root"])
    cc_ok = _is_executable(cc_bin)
    info["cc_bin"] = cc_bin
    info["cc_bin_ok"] = cc_ok
    if check_cc_bin and not cc_ok:
        problems.append(f"找不到可执行的 cc-connect 二进制：{cc_bin}")

    return (len(problems) == 0), problems, info


def _build_project_env(project_dir, env_dict=None, extra=None):
    """Build subprocess environment with project's bot.env overlaid cleanly."""
    p = _paths(project_dir)
    parsed = env_dict if env_dict is not None else parse_bot_env(p["env"])
    overlay = dict(parsed)
    overlay["BOT_ENV_FILE"] = p["env"]
    env = bot_env.build_subprocess_env(extra=overlay)

    # Prepend local .tools/node_modules/.bin if present (matching start.sh)
    node_bin = os.path.join(p["root"], ".tools", "node_modules", ".bin")
    if os.path.isdir(node_bin):
        cur_path = env.get("PATH", "")
        parts = [x for x in cur_path.split(os.pathsep) if x]
        norm_set = {os.path.normcase(os.path.normpath(x)) for x in parts}
        if os.path.normcase(os.path.normpath(node_bin)) not in norm_set:
            env["PATH"] = node_bin + os.pathsep + cur_path

    if extra:
        env.update(extra)
    return env


# ---------------------------------------------------------------------------
# Cross-platform process management
# ---------------------------------------------------------------------------

def _posix_child_pids(parent_pid):
    """Return direct and descendant child PIDs of parent_pid on POSIX."""
    if os.name == "nt":
        return []
    try:
        proc = subprocess.run(
            ["ps", "-eo", "pid=,ppid="],
            capture_output=True,
            text=True,
            timeout=3.0,
        )
        if proc.returncode != 0:
            return []
        children_map = {}
        for line in proc.stdout.splitlines():
            parts = line.split()
            if len(parts) != 2:
                continue
            try:
                pid_i = int(parts[0])
                ppid_i = int(parts[1])
            except ValueError:
                continue
            children_map.setdefault(ppid_i, []).append(pid_i)
        found = []
        queue = list(children_map.get(int(parent_pid), []))
        seen = set()
        while queue:
            cur = queue.pop(0)
            if cur in seen or cur == os.getpid():
                continue
            seen.add(cur)
            found.append(cur)
            queue.extend(children_map.get(cur, []))
        return found
    except Exception:
        return []


def _is_alive_reaping(pid, proc=None):
    """Check if pid is alive, reaping zombie direct children on POSIX so wait loops exit immediately."""
    if proc is not None:
        if proc.poll() is not None:
            return False
    elif os.name != "nt":
        try:
            reaped_pid, _ = os.waitpid(int(pid), os.WNOHANG)
            if reaped_pid == int(pid):
                return False
        except ChildProcessError:
            pass
        except OSError:
            pass
    return file_lock.is_pid_alive(pid)


def terminate_pid(pid, timeout=15.0, tree=False, proc=None):
    """Cross-platform graceful -> forceful process termination.

    Returns True if the process is no longer alive when finished.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return True
    if pid <= 0 or pid == os.getpid():
        return True
    if not _is_alive_reaping(pid, proc=proc):
        return True

    timeout = max(0.5, float(timeout))

    if os.name != "nt":
        targets = [pid]
        if tree:
            # Terminate children too (bottom-up + parent)
            children = _posix_child_pids(pid)
            targets = list(reversed(children)) + [pid]

        # Check if pid leads its own process group (different from ours)
        pgid = None
        if tree:
            try:
                cand_pgid = os.getpgid(pid)
                if cand_pgid == pid and cand_pgid != os.getpgrp():
                    pgid = cand_pgid
            except OSError:
                pgid = None

        if pgid is not None:
            try:
                os.killpg(pgid, signal.SIGTERM)
            except OSError:
                pass
        for tpid in targets:
            try:
                os.kill(tpid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except OSError:
                pass

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not _is_alive_reaping(pid, proc=proc):
                if not tree or not any(file_lock.is_pid_alive(c) for c in targets if c != pid):
                    return True
            time.sleep(0.15)

        # Force kill remaining processes
        if pgid is not None:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except OSError:
                pass
        for tpid in targets:
            if _is_alive_reaping(tpid, proc=proc if tpid == pid else None):
                try:
                    os.kill(tpid, signal.SIGKILL)
                except OSError:
                    pass

        kill_deadline = time.monotonic() + 2.0
        while time.monotonic() < kill_deadline:
            if not _is_alive_reaping(pid, proc=proc):
                return True
            time.sleep(0.1)
        return not _is_alive_reaping(pid, proc=proc)

    # Windows: taskkill /PID <pid> [/T] -> wait -> taskkill /F /PID <pid> [/T]
    flags = bot_env.subprocess_creationflags(detached=False)
    base_cmd = ["taskkill", "/PID", str(pid)]
    if tree:
        base_cmd.append("/T")

    graceful_rc = 1
    graceful_err = ""
    try:
        res = subprocess.run(
            base_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10.0,
            creationflags=flags,
        )
        graceful_rc = res.returncode
        graceful_err = (res.stdout or "") + "\n" + (res.stderr or "")
    except Exception:
        pass

    if not _is_alive_reaping(pid, proc=proc):
        return True

    # Console apps on Windows without a window often reject non-forced taskkill immediately;
    # avoid stalling the full timeout when taskkill explicitly demands /F.
    if graceful_rc == 0:
        wait_secs = timeout
    elif "/F" in graceful_err or "cannot be terminated" in graceful_err.lower():
        wait_secs = 0.5
    else:
        wait_secs = min(timeout, 2.5)

    deadline = time.monotonic() + wait_secs
    while time.monotonic() < deadline:
        if not _is_alive_reaping(pid, proc=proc):
            return True
        time.sleep(0.15)

    if _is_alive_reaping(pid, proc=proc):
        force_cmd = ["taskkill", "/F", "/PID", str(pid)]
        if tree:
            force_cmd.append("/T")
        try:
            subprocess.run(
                force_cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10.0,
                creationflags=flags,
            )
        except Exception:
            pass
        kill_deadline = time.monotonic() + 3.0
        while time.monotonic() < kill_deadline:
            if not _is_alive_reaping(pid, proc=proc):
                return True
            time.sleep(0.15)

    return not _is_alive_reaping(pid, proc=proc)


def _find_matching_pids(required_substrings, exclude_substrings=()):
    """Discover running PIDs whose command line matches all required_substrings (project-scoped)."""
    me = os.getpid()
    req_norm = [os.path.normcase(str(s)) for s in required_substrings if s]
    exc_norm = [os.path.normcase(str(s)) for s in exclude_substrings if s]
    if not req_norm:
        return []

    found = []
    if os.name != "nt":
        for ps_col in ("command=", "args="):
            try:
                proc = subprocess.run(
                    ["ps", "-ww", "-eo", f"pid=,{ps_col}"],
                    capture_output=True,
                    text=True,
                    timeout=3.0,
                )
                if proc.returncode != 0:
                    continue
                for line in proc.stdout.splitlines():
                    m = re.match(r"^\s*(\d+)\s+(.*)$", line)
                    if not m:
                        continue
                    pid_i = int(m.group(1))
                    if pid_i == me:
                        continue
                    cmd = os.path.normcase(m.group(2))
                    if all(sub in cmd for sub in req_norm) and not any(ex in cmd for ex in exc_norm):
                        found.append(pid_i)
                return found
            except Exception:
                continue
        return found

    # Windows: best-effort PowerShell CIM query
    try:
        ps_cmd = (
            "Get-CimInstance Win32_Process | "
            "ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }"
        )
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
            capture_output=True,
            text=True,
            timeout=5.0,
            creationflags=bot_env.subprocess_creationflags(detached=False),
        )
        if proc.returncode == 0:
            for line in proc.stdout.splitlines():
                if "\t" not in line:
                    continue
                pid_s, cmd_s = line.split("\t", 1)
                try:
                    pid_i = int(pid_s.strip())
                except ValueError:
                    continue
                if pid_i == me:
                    continue
                cmd = os.path.normcase(cmd_s)
                if all(sub in cmd for sub in req_norm) and not any(ex in cmd for ex in exc_norm):
                    found.append(pid_i)
    except Exception:
        pass
    return found


def _find_orphan_cc_pids(project_dir):
    p = _paths(project_dir)
    return _find_matching_pids(
        required_substrings=["cc-connect", p["config"]],
        exclude_substrings=["--supervise-cc", "bot_runner.py"],
    )


def _find_orphan_ws_guard_pids(project_dir):
    p = _paths(project_dir)
    return _find_matching_pids(
        required_substrings=[p["guard_script"]],
        exclude_substrings=["--supervise-ws-guard", "bot_runner.py", "bash -c"],
    )


def _find_orphan_webui_pids(project_dir, port=None):
    """Find running webui.server processes belonging to this project/port."""
    p = _paths(project_dir)
    found = []
    seen = set()

    def _collect(req):
        for pid in _find_matching_pids(
            required_substrings=req,
            exclude_substrings=["bot_runner.py"],
        ):
            if pid not in seen:
                seen.add(pid)
                found.append(pid)

    _collect(["webui.server", p["root"]])
    if port is not None:
        port_s = str(int(port))
        _collect(["webui.server", f"--port {port_s}"])
        _collect(["webui.server", f"--port={port_s}"])
    return found


def _tcp_port_open(host, port, timeout=0.5):
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def _port_from_url(url, default_port):
    try:
        parsed = urllib.parse.urlsplit(str(url or "").strip())
        if parsed.port:
            return int(parsed.port)
    except Exception:
        pass
    return int(default_port)


def _spawn_detached(cmd, cwd, env, log_path=None):
    """Spawn a detached background process across macOS, Linux, and Windows."""
    kwargs = {
        "cwd": cwd,
        "env": env,
        "stdin": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = bot_env.subprocess_creationflags(detached=True)
    else:
        kwargs["start_new_session"] = True

    logf = None
    try:
        if log_path:
            parent = os.path.dirname(os.path.abspath(log_path))
            if parent:
                os.makedirs(parent, exist_ok=True)
            logf = open(log_path, "a", encoding="utf-8", errors="replace")
            kwargs["stdout"] = logf
            kwargs["stderr"] = subprocess.STDOUT
        else:
            kwargs["stdout"] = subprocess.DEVNULL
            kwargs["stderr"] = subprocess.DEVNULL
        return subprocess.Popen(cmd, **kwargs)
    finally:
        if logf is not None:
            try:
                logf.close()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Sandbox sync
# ---------------------------------------------------------------------------

def _compile_sandbox_gemini_bytes(project_dir, src_path):
    """Compile effective chat-sandbox/GEMINI.md bytes from workspace/GEMINI.md via bot_plugins."""
    with open(src_path, "r", encoding="utf-8-sig") as f:
        src_text = f.read()
    try:
        import bot_plugins
        proj_root = os.path.abspath(project_dir)
        if (
            not os.environ.get("BOT_PLUGINS_FILE", "").strip()
            and os.path.normcase(proj_root) != os.path.normcase(os.path.abspath(_SCRIPT_DIR))
        ):
            other_cfg_path = os.path.join(proj_root, "data", "bot_plugins.json")
            if os.path.isfile(other_cfg_path):
                with open(other_cfg_path, "r", encoding="utf-8-sig") as cf:
                    cfg_override = bot_plugins.normalize_config(json.load(cf))
            else:
                cfg_override = bot_plugins.default_config()
        else:
            cfg_override = bot_plugins.load_config(force=True)
        compiled = bot_plugins.render_sandbox_gemini_md(src_text, config=cfg_override)
    except Exception:
        compiled = src_text
    return compiled.encode("utf-8")


def sync_sandbox(project_dir, quiet=False):
    """Atomically compile and sync workspace/GEMINI.md -> chat-sandbox/GEMINI.md."""
    p = _paths(project_dir)
    src = p["workspace_gemini"]
    dst = p["sandbox_gemini"]

    if not os.path.isfile(src):
        if not quiet:
            print(f"提示：找不到 {src}，跳过人设沙箱同步。", file=sys.stderr)
        return False

    os.makedirs(p["sandbox_dir"], exist_ok=True)
    want_bytes = _compile_sandbox_gemini_bytes(p["root"], src)

    if os.path.isfile(dst):
        try:
            with open(dst, "rb") as f:
                if f.read() == want_bytes:
                    if not quiet:
                        print("chat-sandbox/GEMINI.md 已与当前人设及插件配置一致，无需更新。")
                    return True
        except OSError:
            pass

    _atomic_write_bytes(dst, want_bytes)
    if not quiet:
        print("已将 workspace/GEMINI.md 按当前插件配置同步到 chat-sandbox/GEMINI.md")
    return True


# ---------------------------------------------------------------------------
# Internal supervisor loops (--supervise-ws-guard / --supervise-cc)
# ---------------------------------------------------------------------------

def _install_stop_signals(stop_event):
    def _handler(signum, frame):
        stop_event.set()

    for sig_name in ("SIGTERM", "SIGINT", "SIGBREAK"):
        sig = getattr(signal, sig_name, None)
        if sig is not None:
            try:
                signal.signal(sig, _handler)
            except (OSError, ValueError):
                pass


def run_supervise_ws_guard(project_dir):
    """Daemon loop supervising onebot-ws-guard.py (restarts 3s after abnormal exit)."""
    p = _paths(project_dir)
    os.makedirs(p["run_dir"], exist_ok=True)
    os.makedirs(p["data_dir"], exist_ok=True)

    my_pid = os.getpid()
    _write_pid(p["pid_ws_sup"], my_pid)
    stop_event = threading.Event()
    _install_stop_signals(stop_event)

    worker_proc = None
    try:
        while not stop_event.is_set():
            py_bin = bot_env.resolve_python_bin(p["root"])
            env = _build_project_env(p["root"])

            if not os.path.isfile(p["guard_script"]):
                _append_log_line(
                    p["ws_guard_log"],
                    f"[supervisor] 找不到 {p['guard_script']}，退出守护循环",
                )
                return 1

            _rotate_log_if_needed(p["ws_guard_log"])
            with open(p["ws_guard_log"], "a", encoding="utf-8", errors="replace") as logf:
                logf.write(f"\n===== START {_now_str()} =====\n")
                logf.flush()
                worker_proc = subprocess.Popen(
                    [py_bin, p["guard_script"]],
                    cwd=p["root"],
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=logf,
                    stderr=subprocess.STDOUT,
                    creationflags=bot_env.subprocess_creationflags(detached=False),
                )
            _write_pid(p["pid_ws_worker"], worker_proc.pid)

            try:
                while worker_proc.poll() is None:
                    if stop_event.is_set():
                        terminate_pid(worker_proc.pid, timeout=10.0, tree=True, proc=worker_proc)
                        break
                    time.sleep(0.25)
            finally:
                rc = worker_proc.poll()
                if rc is None:
                    terminate_pid(worker_proc.pid, timeout=5.0, tree=True, proc=worker_proc)
                    rc = worker_proc.poll()
                _remove_pid(p["pid_ws_worker"], expected_pid=worker_proc.pid)
                worker_proc = None

            if stop_event.is_set():
                _append_log_line(
                    p["ws_guard_log"],
                    f"[supervisor] ws-guard stopped (code {rc})",
                )
                break

            _append_log_line(
                p["ws_guard_log"],
                f"[supervisor] ws-guard exited (code {rc}), restarting in 3s",
            )
            stop_event.wait(3.0)
        return 0
    except Exception:
        _append_log_line(p["ws_guard_log"], "[supervisor] fatal error:\n" + traceback.format_exc())
        return 1
    finally:
        if worker_proc is not None and worker_proc.poll() is None:
            terminate_pid(worker_proc.pid, timeout=5.0, tree=True, proc=worker_proc)
            _remove_pid(p["pid_ws_worker"], expected_pid=worker_proc.pid)
        _remove_pid(p["pid_ws_sup"], expected_pid=my_pid)


def run_supervise_cc(project_dir, extra_args=None):
    """Daemon loop supervising cc-connect (restarts 5s after abnormal exit)."""
    p = _paths(project_dir)
    os.makedirs(p["run_dir"], exist_ok=True)
    os.makedirs(p["data_dir"], exist_ok=True)

    my_pid = os.getpid()
    _write_pid(p["pid_start"], my_pid)
    stop_event = threading.Event()
    _install_stop_signals(stop_event)

    extra_args = list(extra_args or [])
    cc_proc = None
    try:
        while not stop_event.is_set():
            cc_bin = bot_env.resolve_cc_connect_bin(p["root"])
            if not _is_executable(cc_bin):
                _append_log_line(p["cc_log"], f"[supervisor] 找不到可执行的 cc-connect：{cc_bin}")
                return 1

            env = _build_project_env(p["root"])
            sync_sandbox(p["root"], quiet=True)

            _rotate_log_if_needed(p["cc_log"])
            with open(p["cc_log"], "a", encoding="utf-8", errors="replace") as logf:
                logf.write(f"\n===== START CC-CONNECT {_now_str()} =====\n")
                logf.flush()
                cc_proc = subprocess.Popen(
                    [cc_bin, "--config", p["config"]] + extra_args,
                    cwd=p["root"],
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=logf,
                    stderr=subprocess.STDOUT,
                    creationflags=bot_env.subprocess_creationflags(detached=False),
                )
            _write_pid(p["pid_cc"], cc_proc.pid)

            try:
                while cc_proc.poll() is None:
                    if stop_event.is_set():
                        terminate_pid(cc_proc.pid, timeout=15.0, tree=True, proc=cc_proc)
                        break
                    time.sleep(0.25)
            finally:
                rc = cc_proc.poll()
                if rc is None:
                    terminate_pid(cc_proc.pid, timeout=5.0, tree=True, proc=cc_proc)
                    rc = cc_proc.poll()
                _remove_pid(p["pid_cc"], expected_pid=cc_proc.pid)
                cc_proc = None

            if stop_event.is_set() or rc == 0:
                _append_log_line(
                    p["cc_log"],
                    f"[supervisor] cc-connect exited cleanly (code {rc})",
                )
                break

            _append_log_line(
                p["cc_log"],
                f"[supervisor] cc-connect exited (code {rc}), restarting in 5s",
            )
            stop_event.wait(5.0)
        return 0
    except Exception:
        _append_log_line(p["cc_log"], "[supervisor] fatal error:\n" + traceback.format_exc())
        return 1
    finally:
        if cc_proc is not None and cc_proc.poll() is None:
            terminate_pid(cc_proc.pid, timeout=10.0, tree=True, proc=cc_proc)
            _remove_pid(p["pid_cc"], expected_pid=cc_proc.pid)
        _remove_pid(p["pid_start"], expected_pid=my_pid)


def _ensure_ws_guard_running(project_dir):
    """Ensure detached ws-guard supervisor is running; start it if not."""
    p = _paths(project_dir)
    os.makedirs(p["run_dir"], exist_ok=True)
    os.makedirs(p["data_dir"], exist_ok=True)

    sup_pid = _read_pid(p["pid_ws_sup"])
    if sup_pid and file_lock.is_pid_alive(sup_pid):
        worker_pid = _read_pid(p["pid_ws_worker"])
        worker_info = (
            f"，worker PID {worker_pid}"
            if (worker_pid and file_lock.is_pid_alive(worker_pid))
            else ""
        )
        print(f"ws-guard 守护进程已在运行（守护 PID {sup_pid}{worker_info}）")
        return True, sup_pid

    # Clean up any orphaned ws-guard worker before starting a new supervisor
    old_worker = _read_pid(p["pid_ws_worker"])
    if old_worker and file_lock.is_pid_alive(old_worker):
        terminate_pid(old_worker, timeout=5.0, tree=True)
    _remove_pid(p["pid_ws_worker"])
    for orphan_pid in _find_orphan_ws_guard_pids(p["root"]):
        terminate_pid(orphan_pid, timeout=5.0, tree=True)

    py_bin = bot_env.resolve_python_bin(p["root"])
    runner_script = os.path.abspath(__file__)
    env = _build_project_env(p["root"])
    proc = _spawn_detached(
        [py_bin, runner_script, "--project-dir", p["root"], "--supervise-ws-guard"],
        cwd=p["root"],
        env=env,
        log_path=None,
    )

    deadline = time.monotonic() + 3.0
    worker_pid = None
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            print(f"ws-guard 守护进程启动失败，请查看日志：{p['ws_guard_log']}", file=sys.stderr)
            return False, None
        w = _read_pid(p["pid_ws_worker"])
        if w and file_lock.is_pid_alive(w):
            worker_pid = w
            break
        time.sleep(0.2)

    w_text = f"，worker PID {worker_pid}" if worker_pid else ""
    print(f"ws-guard 守护进程已启动（守护 PID {proc.pid}{w_text}，日志 {p['ws_guard_log']}）")
    return True, proc.pid


# ---------------------------------------------------------------------------
# Subcommands: start / start-bg / stop / stop-all / restart / restart-ws-guard
# ---------------------------------------------------------------------------

def _print_validation_errors(problems):
    print("配置校验失败，未启动：", file=sys.stderr)
    for item in problems:
        print(f"  - {item}", file=sys.stderr)


def _print_startup_banner(project_dir, cc_bin, env_cfg, toml_cfg):
    p = _paths(project_dir)
    guard_port = env_cfg.get("GUARD_LISTEN_PORT", "3002") or "3002"
    napcat_ws = env_cfg.get("NAPCAT_WS_URL", "ws://127.0.0.1:3001") or "ws://127.0.0.1:3001"
    mgmt_port = (toml_cfg.get("[management]") or {}).get("port", 9820) or 9820
    print("==========================================")
    print("  QQ-ACP (cc-connect + Antigravity ACP)")
    print("==========================================")
    print(f"程序路径: {cc_bin}")
    print(f"配置文件: {p['config']} + {p['env']}")
    print(f"WS 守护 : ws://127.0.0.1:{guard_port} -> {napcat_ws}")
    print(f"管理 API : http://127.0.0.1:{mgmt_port}（仅本机）")
    print("==========================================")
    print("")


def _detect_running_cc(project_dir):
    """Return (start_sup_pid_or_None, cc_pid_or_None) for currently alive processes."""
    p = _paths(project_dir)
    start_pid = _read_pid(p["pid_start"])
    if start_pid and not file_lock.is_pid_alive(start_pid):
        start_pid = None

    cc_pid = _read_pid(p["pid_cc"])
    if cc_pid and not file_lock.is_pid_alive(cc_pid):
        cc_pid = None
    if cc_pid is None:
        orphans = _find_orphan_cc_pids(p["root"])
        if orphans:
            cc_pid = orphans[0]
    return start_pid, cc_pid


def cmd_start(project_dir, extra_args=None):
    """Run cc-connect in foreground with restart loop & tee to data/cc-connect.log."""
    p = _paths(project_dir)
    ok, problems, info = validate_config(p["root"], check_cc_bin=True)
    if not ok:
        _print_validation_errors(problems)
        return 1

    start_pid, cc_pid = _detect_running_cc(p["root"])
    if cc_pid or start_pid:
        active = cc_pid or start_pid
        print(
            f"机器人已在运行中（PID {active}），请先执行 stop 再前台启动。",
            file=sys.stderr,
        )
        return 1

    sync_sandbox(p["root"], quiet=False)
    _print_startup_banner(p["root"], info["cc_bin"], info["env"], info["toml"])

    ws_ok, _ = _ensure_ws_guard_running(p["root"])
    if not ws_ok:
        return 1

    os.makedirs(p["run_dir"], exist_ok=True)
    os.makedirs(p["data_dir"], exist_ok=True)

    my_pid = os.getpid()
    _write_pid(p["pid_start"], my_pid)
    stop_event = threading.Event()
    _install_stop_signals(stop_event)

    extra_args = list(extra_args or [])
    cc_proc = None
    try:
        while not stop_event.is_set():
            env = _build_project_env(p["root"])
            sync_sandbox(p["root"], quiet=True)
            _rotate_log_if_needed(p["cc_log"])
            logf = open(p["cc_log"], "a", encoding="utf-8", errors="replace")
            log_lock = threading.Lock()
            log_closed = [False]
            tee_t = None
            try:
                logf.write(f"\n===== START CC-CONNECT {_now_str()} (foreground) =====\n")
                logf.flush()
                cc_proc = subprocess.Popen(
                    [info["cc_bin"], "--config", p["config"]] + extra_args,
                    cwd=p["root"],
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    creationflags=bot_env.subprocess_creationflags(detached=False),
                )
                _write_pid(p["pid_cc"], cc_proc.pid)

                def _tee_worker(pipe, file_obj, lock, closed_flag):
                    try:
                        while True:
                            chunk = pipe.readline()
                            if not chunk:
                                break
                            text = chunk.decode("utf-8", errors="replace")
                            with lock:
                                if not closed_flag[0]:
                                    try:
                                        file_obj.write(text)
                                        file_obj.flush()
                                    except (OSError, ValueError):
                                        pass
                            try:
                                sys.stdout.write(text)
                                sys.stdout.flush()
                            except (OSError, ValueError):
                                pass
                    except Exception:
                        pass

                tee_t = threading.Thread(
                    target=_tee_worker,
                    args=(cc_proc.stdout, logf, log_lock, log_closed),
                    daemon=True,
                )
                tee_t.start()

                while cc_proc.poll() is None:
                    if stop_event.is_set():
                        terminate_pid(cc_proc.pid, timeout=15.0, tree=True, proc=cc_proc)
                        break
                    time.sleep(0.2)

                rc = cc_proc.poll()
                if rc is None:
                    terminate_pid(cc_proc.pid, timeout=5.0, tree=True, proc=cc_proc)
                    rc = cc_proc.poll()
                if tee_t is not None:
                    tee_t.join(timeout=3.0)
                if cc_proc.stdout is not None:
                    try:
                        cc_proc.stdout.close()
                    except OSError:
                        pass
                _remove_pid(p["pid_cc"], expected_pid=cc_proc.pid)
                cc_proc = None
            finally:
                if tee_t is not None and tee_t.is_alive():
                    tee_t.join(timeout=1.0)
                with log_lock:
                    log_closed[0] = True
                    try:
                        logf.close()
                    except OSError:
                        pass

            if stop_event.is_set() or rc == 0:
                print(
                    f"cc-connect 已退出（code={rc}）。ws-guard 仍在后台，全部停止请运行："
                    f" python bot_runner.py stop"
                )
                return 0

            print(f"cc-connect 异常退出（code={rc}），5 秒后自动重启……（Ctrl+C 停止）")
            if stop_event.wait(5.0):
                return 0
        return 0
    finally:
        if cc_proc is not None and cc_proc.poll() is None:
            terminate_pid(cc_proc.pid, timeout=10.0, tree=True, proc=cc_proc)
            _remove_pid(p["pid_cc"], expected_pid=cc_proc.pid)
        _remove_pid(p["pid_start"], expected_pid=my_pid)


def _cmd_start_bg_locked(project_dir):
    """Start ws-guard and cc-connect detached in the background (caller holds lifecycle.lock)."""
    p = _paths(project_dir)
    ok, problems, info = validate_config(p["root"], check_cc_bin=True)
    if not ok:
        _print_validation_errors(problems)
        return 1

    sync_sandbox(p["root"], quiet=False)
    ws_ok, ws_sup_pid = _ensure_ws_guard_running(p["root"])
    if not ws_ok:
        return 1

    start_pid, cc_pid = _detect_running_cc(p["root"])
    if cc_pid or start_pid:
        details = []
        if cc_pid:
            details.append(f"cc-connect PID {cc_pid}")
        if start_pid:
            details.append(f"守护 PID {start_pid}")
        print(f"机器人已在后台运行中（{'，'.join(details)}），无需重复启动。")
        return 0

    _rotate_log_if_needed(p["cc_log"])
    _append_log_line(p["cc_log"], f"\n===== RESTART {_now_str()} (start-bg) =====")

    py_bin = bot_env.resolve_python_bin(p["root"])
    runner_script = os.path.abspath(__file__)
    env = _build_project_env(p["root"], env_dict=info["env"])
    sup_proc = _spawn_detached(
        [py_bin, runner_script, "--project-dir", p["root"], "--supervise-cc"],
        cwd=p["root"],
        env=env,
        log_path=None,
    )

    def _cleanup_failed_supervisor(live_cc_pid=None):
        if sup_proc.poll() is None:
            terminate_pid(sup_proc.pid, timeout=8.0, tree=True, proc=sup_proc)
        _remove_pid(p["pid_start"], expected_pid=sup_proc.pid)
        if live_cc_pid and file_lock.is_pid_alive(live_cc_pid):
            terminate_pid(live_cc_pid, timeout=5.0, tree=True)
            _remove_pid(p["pid_cc"], expected_pid=live_cc_pid)

    # Wait up to 5s to verify supervisor + cc-connect come up and stay alive
    deadline = time.monotonic() + 5.0
    cc_seen_at = None
    cc_live_pid = None

    while time.monotonic() < deadline:
        if sup_proc.poll() is not None:
            print(f"后台守护进程启动后立即退出（code={sup_proc.returncode}），请查看日志：{p['cc_log']}", file=sys.stderr)
            tail = _read_tail_lines(p["cc_log"], count=15)
            if tail:
                print("--- 最近日志 ---", file=sys.stderr)
                for line in tail:
                    print(f"  {line}", file=sys.stderr)
            _cleanup_failed_supervisor(cc_live_pid)
            return 1

        cand_cc = _read_pid(p["pid_cc"])
        if cand_cc and file_lock.is_pid_alive(cand_cc):
            if cc_live_pid != cand_cc:
                cc_live_pid = cand_cc
                cc_seen_at = time.monotonic()
            elif cc_seen_at is not None and (time.monotonic() - cc_seen_at) >= 1.5:
                break
        elif cc_live_pid is not None and not file_lock.is_pid_alive(cc_live_pid):
            # cc-connect started then died immediately
            print(f"cc-connect 启动后立即异常退出，请查看日志：{p['cc_log']}", file=sys.stderr)
            tail = _read_tail_lines(p["cc_log"], count=15)
            if tail:
                print("--- 最近日志 ---", file=sys.stderr)
                for line in tail:
                    print(f"  {line}", file=sys.stderr)
            _cleanup_failed_supervisor(cc_live_pid)
            return 1
        time.sleep(0.25)

    final_cc = _read_pid(p["pid_cc"])
    if sup_proc.poll() is not None or not (final_cc and file_lock.is_pid_alive(final_cc)):
        print(f"cc-connect 未能在 5 秒内保持存活，请查看日志：{p['cc_log']}", file=sys.stderr)
        tail = _read_tail_lines(p["cc_log"], count=15)
        if tail:
            print("--- 最近日志 ---", file=sys.stderr)
            for line in tail:
                print(f"  {line}", file=sys.stderr)
        _cleanup_failed_supervisor(final_cc or cc_live_pid)
        return 1

    print("机器人已在后台启动！")
    print(f"  cc-connect : PID {final_cc}（守护 PID {sup_proc.pid}）")
    print(f"  ws-guard   : 守护 PID {ws_sup_pid}")
    print(f"  日志文件   : {p['cc_log']}")
    print(f"  停止命令   : python bot_runner.py stop")
    return 0


def cmd_start_bg(project_dir):
    """Start ws-guard and cc-connect detached in the background (serialized by lifecycle.lock)."""
    p = _paths(project_dir)
    lock_path = os.path.join(p["run_dir"], "lifecycle.lock")
    try:
        with file_lock.file_lock(lock_path, exclusive=True, timeout=15.0):
            return _cmd_start_bg_locked(p["root"])
    except TimeoutError:
        print("另一个启动/重启操作正在进行中，等待锁超时，请稍后重试。", file=sys.stderr)
        return 1


def cmd_stop(project_dir):
    """Cleanly stop start supervisor, cc-connect (+ orphans), ws-guard supervisor & worker."""
    p = _paths(project_dir)
    stopped_any = False

    # 1. Stop cc-connect supervisor first (wait longer than its 15s inner child stop)
    start_pid = _read_pid(p["pid_start"])
    if start_pid and file_lock.is_pid_alive(start_pid):
        terminate_pid(start_pid, timeout=18.0, tree=False)
        stopped_any = True
    _remove_pid(p["pid_start"])

    # 2. Stop cc-connect by PID file
    cc_pid = _read_pid(p["pid_cc"])
    cc_stopped = False
    if cc_pid and file_lock.is_pid_alive(cc_pid):
        terminate_pid(cc_pid, timeout=15.0, tree=True)
        cc_stopped = True
        stopped_any = True
    _remove_pid(p["pid_cc"])

    # 3. Stop any orphaned cc-connect for this project
    for orphan in _find_orphan_cc_pids(p["root"]):
        if file_lock.is_pid_alive(orphan):
            terminate_pid(orphan, timeout=15.0, tree=True)
            cc_stopped = True
            stopped_any = True

    if cc_stopped or start_pid:
        print("cc-connect 已停止")
    else:
        print("cc-connect 未在运行")

    # 4. Stop ws-guard supervisor FIRST (wait longer than its 10s inner child stop)
    ws_sup_pid = _read_pid(p["pid_ws_sup"])
    ws_stopped = False
    if ws_sup_pid and file_lock.is_pid_alive(ws_sup_pid):
        terminate_pid(ws_sup_pid, timeout=14.0, tree=False)
        ws_stopped = True
        stopped_any = True
    _remove_pid(p["pid_ws_sup"])

    # 5. Stop ws-guard worker by PID file
    ws_worker_pid = _read_pid(p["pid_ws_worker"])
    if ws_worker_pid and file_lock.is_pid_alive(ws_worker_pid):
        terminate_pid(ws_worker_pid, timeout=8.0, tree=True)
        ws_stopped = True
        stopped_any = True
    _remove_pid(p["pid_ws_worker"])

    # 6. Stop any orphaned ws-guard worker for this project
    for orphan in _find_orphan_ws_guard_pids(p["root"]):
        if file_lock.is_pid_alive(orphan):
            terminate_pid(orphan, timeout=5.0, tree=True)
            ws_stopped = True
            stopped_any = True

    if ws_stopped:
        print("ws-guard 已停止")
    else:
        print("ws-guard 未在运行")

    if not stopped_any:
        print("（没有检测到需要停止的机器人后台进程）")
    return 0


def cmd_stop_all(project_dir):
    rc1 = cmd_stop(project_dir)
    rc2 = cmd_webui(project_dir, "stop")
    return 0 if (rc1 == 0 and rc2 == 0) else 1


def cmd_restart(project_dir):
    p = _paths(project_dir)
    lock_path = os.path.join(p["run_dir"], "lifecycle.lock")
    try:
        with file_lock.file_lock(lock_path, exclusive=True, timeout=15.0):
            ok, problems, _ = validate_config(p["root"], check_cc_bin=True)
            if not ok:
                print("配置校验未通过，拒绝重启（避免停机后无法启动）：", file=sys.stderr)
                for item in problems:
                    print(f"  - {item}", file=sys.stderr)
                return 1
            cmd_stop(p["root"])
            time.sleep(1.0)
            return _cmd_start_bg_locked(p["root"])
    except TimeoutError:
        print("另一个启动/重启操作正在进行中，等待锁超时，请稍后重试。", file=sys.stderr)
        return 1


def cmd_restart_ws_guard(project_dir):
    """Restart ONLY ws-guard worker; leave cc-connect completely untouched."""
    p = _paths(project_dir)
    os.makedirs(p["run_dir"], exist_ok=True)

    sup_pid = _read_pid(p["pid_ws_sup"])
    if sup_pid and file_lock.is_pid_alive(sup_pid):
        worker_pid = _read_pid(p["pid_ws_worker"])
        killed = False
        if worker_pid and file_lock.is_pid_alive(worker_pid):
            terminate_pid(worker_pid, timeout=8.0, tree=True)
            _remove_pid(p["pid_ws_worker"], expected_pid=worker_pid)
            killed = True
        elif os.name != "nt":
            # Legacy bash supervisor without worker pid file: kill ONLY its direct children
            for child in _posix_child_pids(sup_pid):
                terminate_pid(child, timeout=8.0, tree=True)
                killed = True
        if killed:
            print(
                f"已结束旧的 ws-guard（守护进程 PID {sup_pid} 仍在运行），"
                "3 秒后将自动用最新配置拉起，cc-connect 会自动重连。"
            )
        else:
            print(
                f"ws-guard 守护进程（PID {sup_pid}）正在运行，将自动拉起 ws-guard。"
            )
        return 0

    # Supervisor not running: clear stale files/orphans and spawn detached supervisor
    _remove_pid(p["pid_ws_sup"])
    worker_pid = _read_pid(p["pid_ws_worker"])
    if worker_pid and file_lock.is_pid_alive(worker_pid):
        terminate_pid(worker_pid, timeout=5.0, tree=True)
    _remove_pid(p["pid_ws_worker"])
    for orphan in _find_orphan_ws_guard_pids(p["root"]):
        terminate_pid(orphan, timeout=5.0, tree=True)
    time.sleep(0.5)

    ok, _ = _ensure_ws_guard_running(p["root"])
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# Subcommand: webui {start|stop|restart|status|fg|open}
# ---------------------------------------------------------------------------

def _webui_settings(project_dir):
    p = _paths(project_dir)
    env_cfg = parse_bot_env(p["env"])
    host = (os.environ.get("WEBUI_HOST") or env_cfg.get("WEBUI_HOST") or "127.0.0.1").strip()
    raw_port = (os.environ.get("WEBUI_PORT") or env_cfg.get("WEBUI_PORT") or "8765").strip()
    try:
        port = int(raw_port)
    except ValueError:
        port = 8765
    display_host = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    url = f"http://{display_host}:{port}"
    return host, port, display_host, url


def _webui_env(project_dir):
    p = _paths(project_dir)
    env = _build_project_env(p["root"])
    # Allow running webui.server even when --project-dir points elsewhere
    if not os.path.isdir(os.path.join(p["root"], "webui")):
        cur_pp = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = _SCRIPT_DIR + (os.pathsep + cur_pp if cur_pp else "")
    return env


def cmd_webui(project_dir, action):
    p = _paths(project_dir)
    os.makedirs(p["run_dir"], exist_ok=True)
    os.makedirs(p["data_dir"], exist_ok=True)

    host, port, display_host, url = _webui_settings(p["root"])
    pid_file = p["pid_webui"]
    log_file = p["webui_log"]

    def _running_pid():
        pid = _read_pid(pid_file)
        if pid and file_lock.is_pid_alive(pid):
            return pid
        return None

    act = (action or "status").strip().lower()

    if act == "status":
        pid = _running_pid()
        if not pid:
            orphans = _find_orphan_webui_pids(p["root"], port=port)
            if orphans:
                pid = orphans[0]
        if pid:
            reachable = _tcp_port_open(display_host, port, timeout=0.5)
            extra = "" if reachable else "（端口暂未就绪）"
            print(f"Web UI 运行中（PID {pid}）：{url}{extra}")
            return 0
        print("Web UI 未运行")
        return 1

    if act == "stop":
        pid = _running_pid()
        stopped_pids = []
        if pid:
            terminate_pid(pid, timeout=6.0, tree=True)
            stopped_pids.append(pid)
        _remove_pid(pid_file)
        for orphan in _find_orphan_webui_pids(p["root"], port=port):
            if orphan not in stopped_pids and file_lock.is_pid_alive(orphan):
                terminate_pid(orphan, timeout=6.0, tree=True)
                stopped_pids.append(orphan)
        if not stopped_pids:
            print("Web UI 未在运行")
            return 0
        pid_desc = ", ".join(str(x) for x in stopped_pids)
        print(f"Web UI 已停止（原 PID {pid_desc}）")
        return 0

    if act == "start":
        pid = _running_pid()
        if pid:
            print(f"Web UI 已在运行（PID {pid}）：{url}")
            return 0
        if _tcp_port_open(display_host, port, timeout=0.5):
            orphans = _find_orphan_webui_pids(p["root"], port=port)
            if orphans:
                print(f"Web UI 已在运行（PID {orphans[0]}）：{url}")
            else:
                print(f"端口 {display_host}:{port} 已在监听中（Web UI 可能已在运行，未重复启动）：{url}")
            return 0

        _rotate_log_if_needed(log_file)
        _append_log_line(log_file, f"\n===== START WEBUI {_now_str()} =====")
        py_bin = bot_env.resolve_python_bin(p["root"])
        proc = _spawn_detached(
            [py_bin, "-m", "webui.server", "--host", host, "--port", str(port)],
            cwd=p["root"],
            env=_webui_env(p["root"]),
            log_path=log_file,
        )
        _write_pid(pid_file, proc.pid)

        for _ in range(20):
            if proc.poll() is not None:
                print(f"Web UI 启动失败，请查看日志：{log_file}", file=sys.stderr)
                for line in _read_tail_lines(log_file, count=20):
                    print(f"  {line}", file=sys.stderr)
                _remove_pid(pid_file, expected_pid=proc.pid)
                return 1
            if _tcp_port_open(display_host, port, timeout=0.3):
                print(f"Web UI 已启动（PID {proc.pid}）：{url}")
                print(f"日志文件：{log_file}")
                return 0
            time.sleep(0.25)

        print(f"Web UI 进程已启动（PID {proc.pid}），地址：{url}")
        return 0

    if act == "restart":
        cmd_webui(p["root"], "stop")
        return cmd_webui(p["root"], "start")

    if act == "fg":
        pid = _running_pid()
        if not pid:
            orphans = _find_orphan_webui_pids(p["root"], port=port)
            if orphans:
                pid = orphans[0]
        if pid:
            print(f"后台 Web UI 已在运行（PID {pid}），请先执行 webui stop", file=sys.stderr)
            return 1
        if _tcp_port_open(display_host, port, timeout=0.5):
            print(f"端口 {display_host}:{port} 已被占用，无法前台启动 Web UI", file=sys.stderr)
            return 1
        py_bin = bot_env.resolve_python_bin(p["root"])
        print(f"前台运行 Web UI：{url}（Ctrl+C 退出）")
        proc = subprocess.Popen(
            [py_bin, "-m", "webui.server", "--host", host, "--port", str(port)],
            cwd=p["root"],
            env=_webui_env(p["root"]),
        )
        try:
            return proc.wait()
        except KeyboardInterrupt:
            terminate_pid(proc.pid, timeout=5.0, tree=True, proc=proc)
            return 0

    if act == "open":
        if not _running_pid() and not _tcp_port_open(display_host, port, timeout=0.5):
            rc = cmd_webui(p["root"], "start")
            if rc != 0:
                return rc
        else:
            print(f"Web UI 已在运行：{url}")
        try:
            webbrowser.open(url)
            print(f"已在浏览器打开：{url}")
        except Exception as exc:
            print(f"无法自动打开浏览器（{exc}），请手动访问：{url}")
        return 0

    print(f"不支持的 webui 操作：{action}（支持：start|stop|restart|status|fg|open）", file=sys.stderr)
    return 1


# ---------------------------------------------------------------------------
# Subcommand: napcat {up|down|status|logs}
# ---------------------------------------------------------------------------

def _napcat_ports(project_dir, env_cfg=None):
    p = _paths(project_dir)
    if env_cfg is None:
        env_cfg = parse_bot_env(p["env"])
    http_port = _port_from_url(env_cfg.get("NAPCAT_HTTP_URL"), 3000)
    ws_port = _port_from_url(env_cfg.get("NAPCAT_WS_URL"), 3001)
    webui_port = 6099
    if os.path.isfile(p["napcat_webui_json"]):
        try:
            with open(p["napcat_webui_json"], "r", encoding="utf-8-sig") as f:
                wj = json.load(f)
            if isinstance(wj, dict) and wj.get("port"):
                webui_port = int(wj["port"])
        except Exception:
            pass
    return http_port, ws_port, webui_port


def _detect_docker_compose(env):
    """Return compose command prefix (['docker', 'compose'] or ['docker-compose']) or None."""
    docker_bin = shutil.which("docker", path=env.get("PATH", ""))
    flags = bot_env.subprocess_creationflags(detached=False)
    if docker_bin:
        try:
            res = subprocess.run(
                [docker_bin, "compose", "version"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3.0,
                env=env,
                creationflags=flags,
            )
            if res.returncode == 0:
                return [docker_bin, "compose"]
        except Exception:
            pass
    dc_bin = shutil.which("docker-compose", path=env.get("PATH", ""))
    if dc_bin:
        return [dc_bin]
    return None


def _inspect_napcat_docker(env):
    """Inspect docker availability and napcat container state."""
    docker_bin = shutil.which("docker", path=env.get("PATH", ""))
    info = {
        "docker_installed": bool(docker_bin),
        "docker_running": False,
        "container_exists": False,
        "container_running": False,
        "status": "not_installed" if not docker_bin else "unknown",
        "image": "",
        "error": "",
    }
    if not docker_bin:
        return info

    flags = bot_env.subprocess_creationflags(detached=False)
    try:
        res = subprocess.run(
            [docker_bin, "inspect", "--format", "{{.State.Status}}\t{{.Config.Image}}", "napcat"],
            capture_output=True,
            text=True,
            timeout=3.0,
            env=env,
            creationflags=flags,
        )
        if res.returncode == 0 and res.stdout.strip():
            parts = res.stdout.strip().split("\t")
            st = parts[0].strip()
            img = parts[1].strip() if len(parts) > 1 else ""
            info.update({
                "docker_running": True,
                "container_exists": True,
                "container_running": st == "running",
                "status": st,
                "image": img,
            })
            return info
        err = (res.stderr or "").strip()
        if "No such object" in err or "Error: No such" in err:
            info.update({
                "docker_running": True,
                "container_exists": False,
                "status": "not_created",
            })
        else:
            info["status"] = "daemon_unavailable"
            info["error"] = err
    except subprocess.TimeoutExpired:
        info["status"] = "timeout"
        info["error"] = "docker inspect 超时"
    except Exception as exc:
        info["status"] = "error"
        info["error"] = str(exc)
    return info


def cmd_napcat(project_dir, action, tail=80, follow=False):
    p = _paths(project_dir)
    env_cfg = parse_bot_env(p["env"])
    env = _build_project_env(p["root"], env_dict=env_cfg)
    http_port, ws_port, webui_port = _napcat_ports(p["root"], env_cfg)

    act = (action or "status").strip().lower()
    compose_cmd = _detect_docker_compose(env)
    has_compose_file = os.path.isfile(p["compose"])

    def _print_native_ports():
        h_ok = _tcp_port_open("127.0.0.1", http_port)
        w_ok = _tcp_port_open("127.0.0.1", ws_port)
        u_ok = _tcp_port_open("127.0.0.1", webui_port)
        print("本机端口连通性：")
        print(f"  HTTP  (127.0.0.1:{http_port}) : {'已连通' if h_ok else '未连通'}")
        print(f"  WS    (127.0.0.1:{ws_port}) : {'已连通' if w_ok else '未连通'}")
        print(f"  WebUI (127.0.0.1:{webui_port}) : {'已连通' if u_ok else '未连通'}  (http://127.0.0.1:{webui_port}/webui/)")
        return h_ok, w_ok, u_ok

    if not compose_cmd or not has_compose_file:
        reason = "未检测到 Docker / docker compose" if not compose_cmd else f"找不到 {p['compose']}"
        print(f"提示：{reason}（若使用本机原生 NapCat，无需 Docker）。")
        h_ok, w_ok, u_ok = _print_native_ports()
        if act == "status":
            if h_ok or w_ok:
                print("检测到本机原生 NapCat 正在运行。")
                return 0
            print("当前未检测到运行中的 NapCat。请启动原生 NapCat，或安装 OrbStack / Docker Desktop 后运行 napcat up。")
            return 1
        print("无法通过 Docker 执行该操作；请直接管理本机原生 NapCat，或安装 Docker 后重试。", file=sys.stderr)
        return 1

    base = compose_cmd + ["-f", p["compose"]]

    if act == "up":
        print("正在通过 Docker 启动 NapCat 容器……")
        res = subprocess.run(base + ["up", "-d"], cwd=p["root"], env=env)
        if res.returncode == 0:
            print(f"NapCat 容器已启动。WebUI 地址：http://127.0.0.1:{webui_port}/webui/")
        return res.returncode

    if act == "down":
        print("正在停止并移除 NapCat 容器（登录态和配置保留在 napcat/ 目录）……")
        res = subprocess.run(base + ["down"], cwd=p["root"], env=env)
        return res.returncode

    if act == "status":
        dinfo = _inspect_napcat_docker(env)
        if dinfo["docker_running"]:
            print(f"Docker 状态：正常 | 容器 napcat：{dinfo['status']}" + (f" ({dinfo['image']})" if dinfo["image"] else ""))
        else:
            print(f"Docker 引擎未运行或不可访问：{dinfo['error'] or dinfo['status']}")
        h_ok, w_ok, _ = _print_native_ports()
        return 0 if (dinfo["container_running"] or (h_ok and w_ok)) else 1

    if act == "logs":
        cmd = base + ["logs", "--tail", str(max(1, int(tail or 80)))]
        if follow:
            cmd.append("-f")
        try:
            res = subprocess.run(cmd, cwd=p["root"], env=env)
            return res.returncode
        except KeyboardInterrupt:
            return 0

    print(f"不支持的 napcat 操作：{action}（支持：up|down|status|logs）", file=sys.stderr)
    return 1


# ---------------------------------------------------------------------------
# Subcommand: status [--json]
# ---------------------------------------------------------------------------

def collect_status(project_dir):
    """Gather structured runtime status for processes and local ports."""
    p = _paths(project_dir)
    env_cfg = parse_bot_env(p["env"])
    toml_cfg = parse_config_toml(p["config"])

    ws_sup_pid = _read_pid(p["pid_ws_sup"])
    ws_sup_alive = bool(ws_sup_pid and file_lock.is_pid_alive(ws_sup_pid))
    ws_worker_pid = _read_pid(p["pid_ws_worker"])
    ws_worker_alive = bool(ws_worker_pid and file_lock.is_pid_alive(ws_worker_pid))
    if not ws_worker_alive:
        orphans = _find_orphan_ws_guard_pids(p["root"])
        if orphans:
            ws_worker_pid = orphans[0]
            ws_worker_alive = True

    start_pid, cc_pid = _detect_running_cc(p["root"])
    webui_host, webui_port, webui_disp_host, webui_url = _webui_settings(p["root"])
    webui_pid = _read_pid(p["pid_webui"])
    webui_alive = bool(webui_pid and file_lock.is_pid_alive(webui_pid))
    if not webui_alive:
        webui_orphans = _find_orphan_webui_pids(p["root"], port=webui_port)
        if webui_orphans:
            webui_pid = webui_orphans[0]
            webui_alive = True

    napcat_http_port, napcat_ws_port, napcat_webui_port = _napcat_ports(p["root"], env_cfg)
    try:
        ws_guard_port = int(env_cfg.get("GUARD_LISTEN_PORT", "3002") or 3002)
    except ValueError:
        ws_guard_port = 3002
    try:
        cc_mgmt_port = int((toml_cfg.get("[management]") or {}).get("port", 9820) or 9820)
    except ValueError:
        cc_mgmt_port = 9820

    ports = {
        "napcat_http": {"port": napcat_http_port, "reachable": _tcp_port_open("127.0.0.1", napcat_http_port)},
        "napcat_ws": {"port": napcat_ws_port, "reachable": _tcp_port_open("127.0.0.1", napcat_ws_port)},
        "napcat_webui": {"port": napcat_webui_port, "reachable": _tcp_port_open("127.0.0.1", napcat_webui_port)},
        "ws_guard": {"port": ws_guard_port, "reachable": _tcp_port_open("127.0.0.1", ws_guard_port)},
        "cc_management": {"port": cc_mgmt_port, "reachable": _tcp_port_open("127.0.0.1", cc_mgmt_port)},
        "webui": {"port": webui_port, "reachable": _tcp_port_open(webui_disp_host, webui_port)},
    }

    return {
        "timestamp": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "project_dir": p["root"],
        "processes": {
            "ws_guard": {
                "running": bool(ws_worker_alive or ws_sup_alive),
                "supervisor_running": ws_sup_alive,
                "supervisor_pid": ws_sup_pid if ws_sup_alive else None,
                "worker_running": ws_worker_alive,
                "worker_pid": ws_worker_pid if ws_worker_alive else None,
            },
            "cc_connect": {
                "running": bool(cc_pid is not None),
                "pid": cc_pid,
                "supervisor_running": bool(start_pid is not None),
                "supervisor_pid": start_pid,
            },
            "webui": {
                "running": webui_alive,
                "pid": webui_pid if webui_alive else None,
                "host": webui_host,
                "port": webui_port,
                "url": webui_url,
            },
        },
        "ports": ports,
    }


def cmd_status(project_dir, as_json=False):
    st = collect_status(project_dir)
    if as_json:
        print(json.dumps(st, ensure_ascii=False, indent=2))
        return 0

    procs = st["processes"]
    ports = st["ports"]

    ws = procs["ws_guard"]
    cc = procs["cc_connect"]
    wu = procs["webui"]

    def _badge(ok_flag):
        return "[运行中]" if ok_flag else "[未运行]"

    def _port_badge(ok_flag):
        return "已连通" if ok_flag else "未连通"

    print("==========================================")
    print("  QQ 机器人运行状态")
    print("==========================================")
    print(f"项目目录: {st['project_dir']}")
    print("")
    print("【核心进程】")
    print(
        f"  ws-guard   {_badge(ws['running'])}  "
        f"守护 PID: {ws['supervisor_pid'] or '-'}  |  worker PID: {ws['worker_pid'] or '-'}"
    )
    print(
        f"  cc-connect {_badge(cc['running'])}  "
        f"进程 PID: {cc['pid'] or '-'}  |  守护 PID: {cc['supervisor_pid'] or '-'}"
    )
    print(
        f"  Web UI     {_badge(wu['running'])}  "
        f"进程 PID: {wu['pid'] or '-'}  |  地址: {wu['url']}"
    )
    print("")
    print("【本地端口连通性】")
    port_rows = [
        ("NapCat HTTP API", "napcat_http"),
        ("NapCat WebSocket", "napcat_ws"),
        ("NapCat WebUI", "napcat_webui"),
        ("ws-guard 代理", "ws_guard"),
        ("cc-connect 管理", "cc_management"),
        ("机器人 Web UI", "webui"),
    ]
    for label, key in port_rows:
        item = ports[key]
        print(f"  {label:<16} 127.0.0.1:{item['port']:<5}  {_port_badge(item['reachable'])}")
    print("==========================================")
    return 0


# ---------------------------------------------------------------------------
# Subcommand: doctor
# ---------------------------------------------------------------------------

def _probe_version(cmd, env, timeout=5.0):
    try:
        res = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            creationflags=bot_env.subprocess_creationflags(detached=False),
        )
        out = (res.stdout or res.stderr or "").strip()
        first_line = out.splitlines()[0].strip() if out else ""
        return res.returncode == 0, first_line
    except Exception as exc:
        return False, str(exc)


def _check_pillow(py_bin, env):
    same_py = os.path.normcase(os.path.realpath(py_bin)) == os.path.normcase(os.path.realpath(sys.executable))
    if same_py:
        try:
            import PIL  # noqa: F401
            return True, getattr(PIL, "__version__", "已安装")
        except Exception:
            return False, ""
    ok, out = _probe_version(
        [py_bin, "-c", "import PIL; print(getattr(PIL, '__version__', 'ok'))"],
        env=env,
        timeout=4.0,
    )
    return ok, out if ok else ""


def _probe_python_version(py_bin, env):
    same_py = os.path.normcase(os.path.realpath(py_bin)) == os.path.normcase(os.path.realpath(sys.executable))
    if same_py:
        v = sys.version_info
        return (v.major, v.minor, v.micro), f"{v.major}.{v.minor}.{v.micro}"
    ok, out = _probe_version(
        [py_bin, "-c", "import sys; v=sys.version_info; print(f'{v.major}.{v.minor}.{v.micro}')"],
        env=env,
        timeout=4.0,
    )
    if ok and out:
        m = re.match(r"^(\d+)\.(\d+)\.(\d+)", out.strip())
        if m:
            tup = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
            return tup, f"{tup[0]}.{tup[1]}.{tup[2]}"
    return None, out or "未知"


def cmd_doctor(project_dir):
    """Run friendly, comprehensive Chinese diagnostics across environment & runtime."""
    p = _paths(project_dir)
    env_cfg = parse_bot_env(p["env"])
    env = _build_project_env(p["root"], env_dict=env_cfg)

    py_bin = bot_env.resolve_python_bin(p["root"])
    venv_dir = os.path.join(p["root"], ".venv")
    venv_active = os.path.normcase(os.path.abspath(py_bin)).startswith(
        os.path.normcase(os.path.abspath(venv_dir))
    )
    py_ver_tup, py_ver_str = _probe_python_version(py_bin, env)
    py_ok = bool(py_ver_tup is not None and py_ver_tup[:2] >= (3, 9))

    next_steps = []
    core_ok = True

    def _line(tag, title, detail):
        print(f"  [{tag}] {title}: {detail}")

    print("==========================================")
    print("  QQ 机器人环境与配置自检 (doctor)")
    print("==========================================")
    print(f"项目目录: {p['root']}")
    print("")

    # 1. OS & Python
    os_name = f"{platform.system()} {platform.release()} ({platform.machine()})"
    venv_note = "已启用项目 .venv" if venv_active else ("存在 .venv 但当前未激活" if os.path.isdir(venv_dir) else "使用系统 Python（未创建 .venv）")
    _line("OK" if py_ok else "FAIL", "1. 系统与 Python", f"{os_name} | Python {py_ver_str} | {venv_note}")
    print(f"       解释器路径: {py_bin}")
    if not py_ok:
        core_ok = False
        next_steps.append("请安装 Python 3.9 及以上版本后重试。")

    # 2. Optional Pillow
    pil_ok, pil_ver = _check_pillow(py_bin, env)
    if pil_ok:
        _line("OK", "2. 可选依赖 Pillow", f"已安装 (v{pil_ver})，支持 GIF 动图表情包抽帧识别")
    else:
        _line("WARN", "2. 可选依赖 Pillow", "未安装（不影响核心聊天，但无法识别 GIF 动图表情包）")
        next_steps.append(f"如需识别 GIF 动图表情包，可安装 Pillow：\"{py_bin}\" -m pip install Pillow")

    # 3. agy CLI
    explicit_agy = env_cfg.get("AGY_BIN", "").strip()
    agy_bin = bot_env.resolve_agy_bin(explicit=explicit_agy)
    agy_found = _is_executable(agy_bin)
    if not agy_found:
        which_agy = shutil.which(agy_bin, path=env.get("PATH", ""))
        if which_agy and _is_executable(which_agy):
            agy_bin = which_agy
            agy_found = True
    if agy_found:
        ver_ok, ver_str = _probe_version([agy_bin, "--version"], env=env, timeout=5.0)
        ver_note = ver_str if ver_ok and ver_str else "已找到可执行文件"
        _line("OK", "3. Antigravity CLI (agy)", f"{agy_bin} ({ver_note})")
    else:
        core_ok = False
        _line("FAIL", "3. Antigravity CLI (agy)", f"未找到可执行文件（当前解析：{agy_bin}）")
        next_steps.append("请先安装 Antigravity CLI (agy) 并完成登录，然后检查 bot.env 中的 AGY_BIN。")

    # 4. cc-connect binary
    cc_bin = bot_env.resolve_cc_connect_bin(p["root"])
    cc_found = _is_executable(cc_bin)
    if cc_found:
        ver_ok, ver_str = _probe_version([cc_bin, "--version"], env=env, timeout=5.0)
        ver_note = ver_str if ver_ok and ver_str else "已找到可执行文件"
        _line("OK", "4. cc-connect 二进制", f"{cc_bin} ({ver_note})")
    else:
        core_ok = False
        _line("FAIL", "4. cc-connect 二进制", f"未找到可执行文件（期望位置：{cc_bin}）")
        next_steps.append("请运行安装脚本下载/编译 patched cc-connect 到 .tools/ 目录，或确保 cc-connect 在 PATH 中。")

    # 5. bot.env + config.toml consistency
    cfg_ok, cfg_problems, _ = validate_config(p["root"], check_cc_bin=False)
    if cfg_ok:
        _line(
            "OK",
            "5. 配置文件一致性",
            f"bot.env 与 config.toml 校验通过（机器人 QQ {env_cfg.get('BOT_QQ_ID', '-')}，Token {_mask_secret(env_cfg.get('NAPCAT_TOKEN'))}）",
        )
    else:
        core_ok = False
        _line("FAIL", "5. 配置文件一致性", f"发现 {len(cfg_problems)} 项问题：")
        for prob in cfg_problems:
            print(f"       - {prob}")
        next_steps.append("运行配置向导生成或修复配置：python setup_wizard.py")

    # 6. Persona & chat-sandbox sync
    ws_md = p["workspace_gemini"]
    sb_md = p["sandbox_gemini"]
    if not os.path.isfile(ws_md):
        _line("WARN", "6. 人设与沙箱同步", f"缺少 {ws_md}（将无法加载自定义人设）")
    elif not os.path.isfile(sb_md):
        _line("WARN", "6. 人设与沙箱同步", "缺少 chat-sandbox/GEMINI.md（尚未同步）")
        next_steps.append("同步人设到沙箱目录：python bot_runner.py sync-sandbox")
    else:
        try:
            want_bytes = _compile_sandbox_gemini_bytes(p["root"], ws_md)
            with open(sb_md, "rb") as f2:
                synced = want_bytes == f2.read()
        except OSError:
            synced = False
        extra_files = []
        try:
            extra_files = [
                name for name in os.listdir(p["sandbox_dir"])
                if name != "GEMINI.md" and not name.startswith(".")
            ]
        except OSError:
            pass
        if synced and not extra_files:
            _line("OK", "6. 人设与沙箱同步", "chat-sandbox/GEMINI.md 已与人设原稿及当前插件配置同步，沙箱洁净")
        else:
            msgs = []
            if not synced:
                msgs.append("内容不一致")
                next_steps.append("同步人设到沙箱目录：python bot_runner.py sync-sandbox")
            if extra_files:
                msgs.append(f"沙箱存在多余文件：{', '.join(extra_files[:5])}")
            _line("WARN", "6. 人设与沙箱同步", "；".join(msgs))

    # 7. Docker & NapCat reachability
    dinfo = _inspect_napcat_docker(env)
    http_port, ws_port, webui_port = _napcat_ports(p["root"], env_cfg)
    h_ok = _tcp_port_open("127.0.0.1", http_port)
    w_ok = _tcp_port_open("127.0.0.1", ws_port)
    u_ok = _tcp_port_open("127.0.0.1", webui_port)

    if dinfo["container_running"]:
        docker_desc = f"Docker 容器 napcat 运行中"
    elif dinfo["docker_running"]:
        docker_desc = f"Docker 已就绪（napcat 容器状态：{dinfo['status']}）"
    elif dinfo["docker_installed"]:
        docker_desc = "已安装 Docker，但后台服务未启动"
    else:
        docker_desc = "未安装 Docker（若使用本机原生 NapCat 属正常）"

    port_desc = (
        f"HTTP({http_port}):{'通' if h_ok else '断'} / "
        f"WS({ws_port}):{'通' if w_ok else '断'} / "
        f"WebUI({webui_port}):{'通' if u_ok else '断'}"
    )
    napcat_tag = "OK" if (h_ok and w_ok) else "WARN"
    _line(napcat_tag, "7. Docker 与 NapCat", f"{docker_desc} | {port_desc}")
    if not (h_ok and w_ok):
        if dinfo["docker_installed"] and os.path.isfile(p["compose"]):
            next_steps.append("启动 NapCat 容器：python bot_runner.py napcat up（然后访问 http://127.0.0.1:6099/webui/ 扫码登录）")
        else:
            next_steps.append("请启动本机原生 NapCat（或安装 Docker 后运行 python bot_runner.py napcat up），并确认 3000/3001 端口已开启。")

    # 8. Running status
    st = collect_status(p["root"])
    ws_st = st["processes"]["ws_guard"]
    cc_st = st["processes"]["cc_connect"]
    wu_st = st["processes"]["webui"]
    ws_port_ok = st["ports"]["ws_guard"]["reachable"]
    cc_port_ok = st["ports"]["cc_management"]["reachable"]
    wu_port_ok = st["ports"]["webui"]["reachable"]

    run_summary = (
        f"ws-guard:{'运行中' if ws_st['running'] else '未运行'}(端口{'通' if ws_port_ok else '断'}) | "
        f"cc-connect:{'运行中' if cc_st['running'] else '未运行'}(管理口{'通' if cc_port_ok else '断'}) | "
        f"WebUI:{'运行中' if wu_st['running'] else '未运行'}(端口{'通' if wu_port_ok else '断'})"
    )
    _line("OK" if (ws_st["running"] and cc_st["running"]) else "INFO", "8. 机器人服务状态", run_summary)
    if core_ok and not cc_st["running"]:
        next_steps.append("启动机器人：python bot_runner.py start-bg（后台）或 python bot_runner.py start（前台）")
    if not wu_st["running"]:
        next_steps.append("打开可视化控制面板：python bot_runner.py webui open")

    print("")
    print("==========================================")
    if core_ok:
        print("检查结果：核心运行前置条件已全部通过！")
    else:
        print("检查结果：存在核心前置条件未就绪（标记为 [FAIL] 的项）。")
    if next_steps:
        print("")
        print("建议下一步：")
        for idx, step in enumerate(next_steps, 1):
            print(f"  {idx}. {step}")
    print("==========================================")
    return 0 if core_ok else 1


# ---------------------------------------------------------------------------
# CLI argument parsing
# ---------------------------------------------------------------------------

def _build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--project-dir",
        default=argparse.SUPPRESS,
        help="项目根目录路径（默认为 bot_runner.py 所在目录）",
    )

    parser = argparse.ArgumentParser(
        prog="bot_runner.py",
        description="QQ 机器人跨平台启动、守护与诊断工具（macOS / Linux / Windows）",
    )
    parser.add_argument(
        "--project-dir",
        default=_SCRIPT_DIR,
        help="项目根目录路径（默认为 bot_runner.py 所在目录）",
    )
    parser.add_argument(
        "--supervise-ws-guard",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--supervise-cc",
        action="store_true",
        help=argparse.SUPPRESS,
    )

    sub = parser.add_subparsers(dest="command")

    p_start = sub.add_parser("start", parents=[common], help="前台运行 cc-connect（后台守护 ws-guard）")
    p_start.add_argument("cc_args", nargs=argparse.REMAINDER, help="透传给 cc-connect 的额外参数")

    sub.add_parser("start-bg", parents=[common], help="完全在后台启动 ws-guard 与 cc-connect")
    sub.add_parser("stop", parents=[common], help="停止 cc-connect 与 ws-guard")
    sub.add_parser("stop-all", parents=[common], help="停止机器人服务与 Web UI")
    sub.add_parser("restart", parents=[common], help="重启机器人服务（先校验再停启）")
    sub.add_parser("restart-ws-guard", parents=[common], help="仅重启 ws-guard，不影响 cc-connect")

    p_status = sub.add_parser("status", parents=[common], help="查看各进程与本地端口状态")
    p_status.add_argument("--json", action="store_true", dest="as_json", help="以 JSON 格式输出")

    p_webui = sub.add_parser("webui", parents=[common], help="管理本地可视化 Web UI")
    p_webui.add_argument(
        "action",
        choices=["start", "stop", "restart", "status", "fg", "open"],
        help="Web UI 操作",
    )

    p_napcat = sub.add_parser("napcat", parents=[common], help="管理 NapCat 容器或检查原生 NapCat")
    p_napcat.add_argument(
        "action",
        choices=["up", "down", "status", "logs"],
        help="NapCat 操作",
    )
    p_napcat.add_argument("-n", "--tail", type=int, default=80, help="logs 显示行数（默认 80）")
    p_napcat.add_argument("-f", "--follow", action="store_true", help="logs 持续跟踪输出")

    sub.add_parser("sync-sandbox", parents=[common], help="同步 workspace/GEMINI.md 到 chat-sandbox/GEMINI.md")
    sub.add_parser("doctor", parents=[common], help="执行完整环境与配置自检")

    return parser


def main(argv=None):
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    _, project_explicit = _peek_project_dir_from_argv(raw_argv)
    parser = _build_parser()
    args = parser.parse_args(raw_argv)
    project_dir = os.path.abspath(os.path.expanduser(getattr(args, "project_dir", _SCRIPT_DIR)))
    if project_explicit:
        env_path = os.path.join(project_dir, "bot.env")
        os.environ["BOT_ENV_FILE"] = env_path
        bot_env.ENV_FILE = env_path

    if args.supervise_ws_guard:
        return run_supervise_ws_guard(project_dir)
    if args.supervise_cc:
        return run_supervise_cc(project_dir)

    cmd = args.command
    if not cmd:
        parser.print_help()
        return 1

    if cmd == "start":
        extra = list(args.cc_args or [])
        if extra and extra[0] == "--":
            extra = extra[1:]
        return cmd_start(project_dir, extra_args=extra)
    if cmd == "start-bg":
        return cmd_start_bg(project_dir)
    if cmd == "stop":
        return cmd_stop(project_dir)
    if cmd == "stop-all":
        return cmd_stop_all(project_dir)
    if cmd == "restart":
        return cmd_restart(project_dir)
    if cmd == "restart-ws-guard":
        return cmd_restart_ws_guard(project_dir)
    if cmd == "status":
        return cmd_status(project_dir, as_json=bool(args.as_json))
    if cmd == "webui":
        return cmd_webui(project_dir, args.action)
    if cmd == "napcat":
        return cmd_napcat(project_dir, args.action, tail=args.tail, follow=args.follow)
    if cmd == "sync-sandbox":
        return 0 if sync_sandbox(project_dir, quiet=False) else 1
    if cmd == "doctor":
        return cmd_doctor(project_dir)

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
