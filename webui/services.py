"""Runtime service integrations, analytics, log parsers, and control actions
for QQ Bot「哈基米」Web UI (Python 3.9 stdlib only).

Integrates with:
- NapCat OneBot v11 HTTP API (short 2s timeouts, 15s group list cache, Docker & Native support)
- cc-connect Management HTTP API (2.5s timeout, disk fallback to data/sessions/*.json)
- Local process table (`ps` on POSIX / `Get-CimInstance Win32_Process` on Windows) cross-checked with `data/run/*.pid`
- Structured log parsers for `data/cc-connect.log` (Go slog logfmt) and `data/ws-guard.log`
- Service controls via `bot_runner.py`
"""

import concurrent.futures
import copy
import datetime
import hashlib
import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import napcat_api, stores
import bot_env  # noqa: E402
import file_lock  # noqa: E402
import onebot_common  # noqa: E402
import token_usage  # noqa: E402

try:
    import group_requests  # noqa: E402
except ImportError:
    group_requests = None

PROJECT_DIR = stores.PROJECT_DIR
DATA_DIR = stores.DATA_DIR
RUN_DIR = os.path.join(DATA_DIR, "run")
SESSIONS_DIR = os.path.join(DATA_DIR, "sessions")
CC_LOG_PATH = os.path.join(DATA_DIR, "cc-connect.log")
WS_GUARD_LOG_PATH = os.path.join(DATA_DIR, "ws-guard.log")
GUARD_SUP_PID_FILE = os.path.join(RUN_DIR, "ws-guard-supervisor.pid")
GUARD_WORKER_PID_FILE = os.path.join(RUN_DIR, "ws-guard-worker.pid")
CC_CONNECT_PID_FILE = os.path.join(RUN_DIR, "cc-connect.pid")
START_PID_FILE = os.path.join(RUN_DIR, "start.pid")
WEBUI_PID_FILE = os.path.join(RUN_DIR, "webui.pid")
GUARD_SCRIPT = os.path.join(PROJECT_DIR, "onebot-ws-guard.py")
BOT_RUNNER_SCRIPT = os.path.join(PROJECT_DIR, "bot_runner.py")

# 15-second thread-safe cache for NapCat group list
_group_cache_lock = threading.Lock()
_group_cache = {"ts": 0.0, "groups": [], "group_map": {}, "ok": False, "error": ""}
_GROUP_CACHE_TTL = 15.0

# 15-second thread-safe cache for NapCat friend list
_friend_cache_lock = threading.Lock()
_friend_cache = {"ts": 0.0, "friends": [], "ok": False, "error": ""}
_FRIEND_CACHE_TTL = 15.0

# 60-second thread-safe cache for bot's own role in each QQ group
_bot_role_cache_lock = threading.Lock()
_bot_role_cache = {"ts": 0.0, "roles": {}}
_BOT_ROLE_CACHE_TTL = 60.0

# 10-second thread-safe cache for Docker inspect
_docker_cache_lock = threading.Lock()
_docker_cache = {"ts": 0.0, "data": None}
_DOCKER_CACHE_TTL = 10.0

# 2.5-second thread-safe cache for Windows PowerShell process queries
_win_proc_cache_lock = threading.Lock()
_win_proc_cache = {"ts": 0.0, "procs": {}}
_WIN_PROC_CACHE_TTL = 2.5

# 3-second thread-safe cache for inspect_processes()
_proc_cache_lock = threading.Lock()
_proc_cache = {"ts": 0.0, "data": None}
_PROC_CACHE_TTL = 3.0

# 3-second thread-safe cache for get_overview()
_overview_cache_lock = threading.Lock()
_overview_cache = {"ts": 0.0, "data": None}
_OVERVIEW_CACHE_TTL = 3.0

# 15-second thread-safe cache for get_analytics() keyed by log file mtime/size + local hour
_analytics_cache_lock = threading.Lock()
_analytics_cache = {"ts": 0.0, "key": None, "data": None}
_ANALYTICS_CACHE_TTL = 15.0
_WS_ANALYTICS_MAX_BYTES = 2 * 1024 * 1024

_ANSI_ESCAPE_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])")
_QR_BLOCK_RE = re.compile(r"[▄▀█░▓]{6,}")
_QR_TEXT_RE = re.compile(r"(二维码|扫码|请扫描|等待扫码|qrcode|qr\s*code|qlogin)", re.IGNORECASE)
_LOGIN_HINT_RE = re.compile(
    r"(二维码|扫码|请扫描|等待扫码|qrcode|qr\s*code|qlogin|登录|login|掉线|offline|expired|webui|token)",
    re.IGNORECASE,
)

_PS_LINE_RE = re.compile(
    r"^\s*(\d+)\s+(\d+)\s+([\d.]+)\s+(\d+)\s+"
    r"([A-Z][a-z]{2}\s+[A-Z][a-z]{2}\s+\d+\s+\d{2}:\d{2}:\d{2}\s+\d{4})\s+(.*)$"
)

_SENDER_HEADER_RE = re.compile(
    r'^\[cc-connect\s+sender_id=(\S+)(?:\s+sender_name="([^"]*)")?\s+platform=(\S+)\s+chat_id=([^\]]+)\]\s*',
    re.DOTALL,
)

_NO_REPLY_RE = re.compile(
    r'^[\s`"\'\[\(\*（【]*NO_REPLY[\s`"\'\]\)\*）】\.。!！~～]*$',
    re.IGNORECASE,
)

_LOGFMT_TOKEN_RE = re.compile(r'([A-Za-z0-9_.-]+)=("(?:\\.|[^"\\])*"|\S+)')
_WS_GUARD_LINE_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\]\s+\[ws-guard\]\s+(.*)$")
_ISO_FRAC_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:?\d{2})?$")
_GO_DUR_RE = re.compile(r"(\d+(?:\.\d+)?)(ns|us|µs|μs|ms|s|m|h)")


class ServiceError(stores.StoreError):
    pass


# ---------------------------------------------------------------------------
# Time & formatting helpers (Python 3.9 safe)
# ---------------------------------------------------------------------------

def _now_iso():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _parse_iso_dt(ts_str):
    """Python 3.9-safe ISO 8601 parser handling arbitrary fractional second lengths and Z."""
    s = str(ts_str or "").strip()
    if not s:
        return None
    m = _ISO_FRAC_RE.match(s)
    if not m:
        return None
    base, frac, tz = m.group(1).replace(" ", "T"), m.group(2), m.group(3)
    if frac:
        frac6 = (frac + "000000")[:6]
        iso = f"{base}.{frac6}"
    else:
        iso = base
    if tz:
        if tz == "Z":
            tz = "+00:00"
        elif len(tz) == 5 and ":" not in tz:
            tz = tz[:3] + ":" + tz[3:]
        iso += tz
    try:
        return datetime.datetime.fromisoformat(iso)
    except ValueError:
        return None


def _parse_go_duration(raw):
    """Parse Go duration strings like '10.907514167s', '1m12.3s', '450ms' into float seconds."""
    s = str(raw or "").strip()
    if not s:
        return None
    matches = _GO_DUR_RE.findall(s)
    if not matches:
        try:
            return float(s)
        except ValueError:
            return None
    unit_scale = {
        "ns": 1e-9,
        "us": 1e-6,
        "µs": 1e-6,
        "μs": 1e-6,
        "ms": 1e-3,
        "s": 1.0,
        "m": 60.0,
        "h": 3600.0,
    }
    total = 0.0
    for val_s, unit in matches:
        total += float(val_s) * unit_scale.get(unit, 1.0)
    return total


def _http_json(url, method="GET", headers=None, payload=None, timeout=2.5):
    hdrs = {"Accept": "application/json"}
    if headers:
        hdrs.update(headers)
    body = None
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return True, (json.loads(raw) if raw.strip() else {}), resp.status
    except urllib.error.HTTPError as e:
        err_body = ""
        try:
            err_body = e.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            pass
        try:
            parsed = json.loads(err_body)
            msg = parsed.get("error") or parsed.get("message") or f"HTTP {e.code}"
        except Exception:
            msg = f"HTTP {e.code}: {err_body or e.reason}"
        return False, msg, e.code
    except Exception as e:
        return False, str(e), 0


def _pid_alive(pid):
    return file_lock.is_pid_alive(pid)


def _extract_url_port(url, default_port):
    try:
        parsed = urllib.parse.urlsplit(str(url or "").strip())
        if parsed.port:
            return int(parsed.port)
    except Exception:
        pass
    return int(default_port)


def _invalidate_proc_cache():
    with _win_proc_cache_lock:
        _win_proc_cache["ts"] = 0.0
        _win_proc_cache["procs"] = {}
    with _proc_cache_lock:
        _proc_cache["ts"] = 0.0
        _proc_cache["data"] = None


def invalidate_overview_cache():
    """Clear the short-lived server-side /api/overview TTL cache."""
    with _overview_cache_lock:
        _overview_cache["ts"] = 0.0
        _overview_cache["data"] = None


def _read_pid_file(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            val = int(f.read().strip())
            return val if val > 0 else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# NapCat OneBot v11 Client & Docker Inspector
# ---------------------------------------------------------------------------

def _napcat_cfg():
    env = stores.parse_bot_env()
    base = (env.get("NAPCAT_HTTP_URL") or "http://127.0.0.1:3000").strip().rstrip("/")
    token = (env.get("NAPCAT_TOKEN") or "").strip()
    bot_qq = (env.get("BOT_QQ_ID") or "").strip()
    return base, token, bot_qq


def _napcat_call(endpoint, payload=None, timeout=2.0):
    base, token, _ = _napcat_cfg()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    method = "POST" if payload is not None else "GET"
    ok, res, _ = _http_json(f"{base}{endpoint}", method=method, headers=headers, payload=payload, timeout=timeout)
    if not ok:
        return False, res
    if isinstance(res, dict):
        status = str(res.get("status") or "").lower()
        retcode = res.get("retcode", 0)
        if status == "failed" or (isinstance(retcode, int) and retcode != 0):
            return False, res.get("message") or res.get("wording") or f"NapCat retcode={retcode}"
        return True, res.get("data")
    return True, res


def napcat_get_login_info():
    ok, data = _napcat_call("/get_login_info", timeout=2.0)
    if not ok or not isinstance(data, dict):
        return {"ok": False, "user_id": "", "nickname": "", "error": str(data)}
    return {
        "ok": True,
        "user_id": str(data.get("user_id") or ""),
        "nickname": str(data.get("nickname") or ""),
        "error": "",
    }


def napcat_get_status():
    ok, data = _napcat_call("/get_status", timeout=2.0)
    if not ok or not isinstance(data, dict):
        return {"ok": False, "online": False, "good": False, "stat": {}, "error": str(data)}
    stat = data.get("stat") if isinstance(data.get("stat"), dict) else {}
    return {
        "ok": True,
        "online": bool(data.get("online")),
        "good": bool(data.get("good")),
        "stat": stat,
        "error": "",
    }


def napcat_get_version_info():
    ok, data = _napcat_call("/get_version_info", timeout=1.5)
    if not ok or not isinstance(data, dict):
        return {"ok": False, "app_name": "", "app_version": "", "protocol_version": "", "error": str(data)}
    return {
        "ok": True,
        "app_name": str(data.get("app_name") or ""),
        "app_version": str(data.get("app_version") or ""),
        "protocol_version": str(data.get("protocol_version") or ""),
        "error": "",
    }


def napcat_get_online_clients():
    ok, data = _napcat_call("/get_online_clients", timeout=1.5)
    if not ok or not isinstance(data, list):
        return []
    return [c for c in data if isinstance(c, dict)]


def napcat_get_group_list(force=False):
    now = time.time()
    with _group_cache_lock:
        if not force and (now - _group_cache["ts"]) < _GROUP_CACHE_TTL and _group_cache["ts"] > 0:
            return {
                "ok": _group_cache["ok"],
                "groups": list(_group_cache["groups"]),
                "group_map": dict(_group_cache["group_map"]),
                "cached": True,
                "error": _group_cache["error"],
            }

    ok, data = _napcat_call("/get_group_list", timeout=2.0)
    groups = []
    gmap = {}
    err = ""
    if ok and isinstance(data, list):
        for g in data:
            if not isinstance(g, dict):
                continue
            gid = str(g.get("group_id") or "").strip()
            gname = str(g.get("group_name") or "").strip()
            if gid:
                gmap[gid] = gname or f"群 {gid}"
                groups.append({
                    "group_id": gid,
                    "group_name": gname or f"群 {gid}",
                    "member_count": int(g.get("member_count") or 0),
                    "max_member_count": int(g.get("max_member_count") or 0),
                })
    else:
        err = str(data)

    with _group_cache_lock:
        _group_cache.update({
            "ts": now,
            "groups": groups,
            "group_map": gmap,
            "ok": ok,
            "error": err,
        })

    return {"ok": ok, "groups": groups, "group_map": gmap, "cached": False, "error": err}


def napcat_get_friend_list(force=False):
    """Fetch NapCat friend list with 15s thread-safe cache; returns list of {user_id, nickname, remark}."""
    now = time.time()
    with _friend_cache_lock:
        if not force and (now - _friend_cache["ts"]) < _FRIEND_CACHE_TTL and _friend_cache["ts"] > 0:
            return list(_friend_cache["friends"])

    ok, data = _napcat_call("/get_friend_list", timeout=2.0)
    friends = []
    err = ""
    if ok and isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            uid = str(item.get("user_id") or "").strip()
            if not uid:
                continue
            friends.append({
                "user_id": uid,
                "nickname": str(item.get("nickname") or "").strip(),
                "remark": str(item.get("remark") or "").strip(),
            })
    else:
        err = str(data)

    with _friend_cache_lock:
        _friend_cache.update({
            "ts": now,
            "friends": friends,
            "ok": ok,
            "error": err,
        })

    return list(friends)


def napcat_get_bot_group_roles(group_ids, force=False):
    """Query NapCat for the bot's own QQ role ('owner'|'admin'|'member') per group."""
    now = time.time()
    gids = [str(g).strip() for g in (group_ids or []) if str(g).strip().isdigit()]
    if not gids:
        return {}
    with _bot_role_cache_lock:
        if (
            not force
            and (now - _bot_role_cache["ts"]) < _BOT_ROLE_CACHE_TTL
            and all(g in _bot_role_cache["roles"] for g in gids)
        ):
            return {g: _bot_role_cache["roles"][g] for g in gids}

    env = stores.parse_bot_env()
    bot_qq = str(env.get("BOT_QQ_ID") or "").strip()
    if not bot_qq.isdigit():
        return {g: "unknown" for g in gids}

    def _fetch_role(gid):
        ok, data = _napcat_call(
            "/get_group_member_info",
            payload={"group_id": int(gid), "user_id": int(bot_qq), "no_cache": False},
            timeout=1.2,
        )
        if ok and isinstance(data, dict):
            role = str(data.get("role") or "").strip().lower()
            return gid, (role if role in ("owner", "admin", "member") else "unknown")
        return gid, "unknown"

    target_gids = gids[:40]
    roles = {}
    if len(target_gids) == 1:
        gid, role = _fetch_role(target_gids[0])
        roles[gid] = role
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, len(target_gids))) as pool:
            for gid, role in pool.map(_fetch_role, target_gids):
                roles[gid] = role

    with _bot_role_cache_lock:
        _bot_role_cache["ts"] = now
        _bot_role_cache["roles"].update(roles)
    return roles


def get_permissions_overview(force_groups=False):
    """Combine bot_permissions store with live NapCat group list & bot roles."""
    glist = napcat_get_group_list(force=force_groups)
    napcat_groups = glist.get("groups") or []
    bot_roles = (
        napcat_get_bot_group_roles([g["group_id"] for g in napcat_groups], force=force_groups)
        if glist.get("ok")
        else {}
    )
    data = stores.get_permissions_state(napcat_groups=napcat_groups, bot_roles=bot_roles)
    data["napcat_online"] = bool(glist.get("ok"))
    return data


def _invalidate_napcat_caches():
    with _docker_cache_lock:
        _docker_cache["ts"] = 0.0
        _docker_cache["data"] = None
    with _group_cache_lock:
        _group_cache["ts"] = 0.0
    with _friend_cache_lock:
        _friend_cache["ts"] = 0.0
    with _bot_role_cache_lock:
        _bot_role_cache["ts"] = 0.0
        _bot_role_cache["roles"].clear()
    napcat_api.invalidate_caches()
    invalidate_overview_cache()


def _format_onebot_segments(segments, raw_message=""):
    if not isinstance(segments, list) or not segments:
        return str(raw_message or "")
    parts = []
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        st = str(seg.get("type") or "")
        dt = seg.get("data") or {}
        if st == "text":
            parts.append(str(dt.get("text") or ""))
        elif st == "at":
            qq = str(dt.get("qq") or "")
            name = str(dt.get("name") or "")
            parts.append("@全体成员" if qq == "all" else f"@{name or qq}")
        elif st == "reply":
            parts.append(f"[回复 #{dt.get('id', '')}]")
        elif st == "face":
            parts.append("[QQ表情]")
        elif st == "mface":
            parts.append(str(dt.get("summary") or "[商城表情]"))
        elif st == "image":
            summary = str(dt.get("summary") or "").strip()
            is_sticker = str(dt.get("sub_type", "")) == "1" or "表情" in summary
            parts.append(summary or ("[表情包]" if is_sticker else "[图片]"))
        elif st == "forward":
            parts.append("[合并转发]")
        elif st == "record":
            parts.append("[语音]")
        elif st == "video":
            parts.append("[视频]")
        elif st == "file":
            parts.append(f"[文件: {dt.get('name') or dt.get('file') or ''}]")
        else:
            parts.append(f"[{st}]")
    return "".join(parts).strip()


def get_napcat_group_history(group_id, count=30):
    gid_str = str(group_id or "").strip()
    if not gid_str or not gid_str.isdigit():
        raise ServiceError("group_id 必须是有效的 QQ 群号")
    cnt = min(max(int(count or 30), 1), 100)

    ok, data = _napcat_call("/get_group_msg_history", payload={"group_id": int(gid_str), "count": cnt}, timeout=2.5)
    if not ok:
        raise ServiceError(f"NapCat 获取群聊历史失败：{data}", status_code=502)

    raw_msgs = (data or {}).get("messages") if isinstance(data, dict) else []
    if not isinstance(raw_msgs, list):
        raw_msgs = []

    _, _, bot_qq = _napcat_cfg()
    gmap = napcat_get_group_list().get("group_map", {})

    formatted = []
    for m in raw_msgs:
        if not isinstance(m, dict):
            continue
        uid = str(m.get("user_id") or "")
        sender = m.get("sender") if isinstance(m.get("sender"), dict) else {}
        sender_name = str(sender.get("card") or sender.get("nickname") or uid)
        ts = m.get("time")
        ts_iso = ""
        if isinstance(ts, (int, float)) and ts > 0:
            ts_iso = datetime.datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")
        segs = m.get("message") if isinstance(m.get("message"), list) else []
        text = _format_onebot_segments(segs, m.get("raw_message", ""))

        formatted.append({
            "message_id": m.get("message_id"),
            "time": ts,
            "timestamp": ts_iso,
            "user_id": uid,
            "sender_id": uid,
            "sender_name": sender_name,
            "sender": {
                "user_id": uid,
                "nickname": str(sender.get("nickname") or ""),
                "card": str(sender.get("card") or ""),
                "role": str(sender.get("role") or ""),
            },
            "is_bot": bool(bot_qq and uid == bot_qq),
            "role": "assistant" if (bot_qq and uid == bot_qq) else "user",
            "text": text,
            "content": text,
            "raw_message": str(m.get("raw_message") or ""),
            "message": segs,
        })

    return {
        "group_id": gid_str,
        "group_name": gmap.get(gid_str) or f"QQ 群 {gid_str}",
        "count": len(formatted),
        "messages": formatted,
    }


def napcat_send_group_msg(group_id, message):
    gid = str(group_id or "").strip()
    if not gid.isdigit():
        raise ServiceError("无效的群号")
    ok, data = _napcat_call("/send_group_msg", payload={"group_id": int(gid), "message": message}, timeout=3.0)
    if not ok:
        raise ServiceError(f"NapCat 群消息发送失败：{data}", status_code=502)
    return data


def napcat_send_private_msg(user_id, message):
    uid = str(user_id or "").strip()
    if not uid.isdigit():
        raise ServiceError("无效的用户 QQ 号")
    ok, data = _napcat_call("/send_private_msg", payload={"user_id": int(uid), "message": message}, timeout=3.0)
    if not ok:
        raise ServiceError(f"NapCat 私聊发送失败：{data}", status_code=502)
    return data


def get_napcat_docker_status(force=False):
    now = time.time()
    with _docker_cache_lock:
        if not force and _docker_cache["data"] is not None and (now - _docker_cache["ts"]) < _DOCKER_CACHE_TTL:
            return dict(_docker_cache["data"])

    env = bot_env.build_subprocess_env()
    result = {
        "container_name": "napcat",
        "available": False,
        "running": False,
        "status": "unknown",
        "image": "",
        "started_at": "",
        "uptime_seconds": None,
    }
    try:
        proc = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Status}}\t{{.State.StartedAt}}\t{{.Config.Image}}", "napcat"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=2.0,
            env=env,
            creationflags=bot_env.subprocess_creationflags(),
        )
        if proc.returncode == 0 and proc.stdout.strip():
            parts = proc.stdout.strip().split("\t")
            status = parts[0].strip() if len(parts) > 0 else "unknown"
            started_at = parts[1].strip() if len(parts) > 1 else ""
            image = parts[2].strip() if len(parts) > 2 else ""
            uptime_sec = None
            dt = _parse_iso_dt(started_at)
            if dt and status == "running":
                uptime_sec = max(0.0, (datetime.datetime.now(datetime.timezone.utc) - dt.astimezone(datetime.timezone.utc)).total_seconds())
            result = {
                "container_name": "napcat",
                "available": True,
                "running": status == "running",
                "status": status,
                "image": image,
                "started_at": started_at,
                "uptime_seconds": round(uptime_sec, 1) if uptime_sec is not None else None,
            }
    except Exception:
        pass

    with _docker_cache_lock:
        _docker_cache["ts"] = now
        _docker_cache["data"] = result
    return dict(result)


def is_ws_guard_code_stale():
    """Return True if the running ws-guard worker started before onebot-ws-guard.py's mtime."""
    try:
        script_mtime = os.path.getmtime(GUARD_SCRIPT)
    except OSError:
        return False
    procs = inspect_processes()
    worker = procs.get("ws_guard_worker")
    if not worker:
        return False
    uptime_s = worker.get("uptime_seconds")
    if uptime_s is None:
        return False
    started_epoch = time.time() - float(uptime_s)
    # Allow 3s tolerance for clock/ps rounding
    return (started_epoch + 3.0) < script_mtime


def update_group_whitelist_action(enabled, group_ids, restart_ws_guard=False):
    res = stores.update_group_whitelist(enabled, group_ids)
    control_result = None
    if restart_ws_guard:
        control_result = run_control("restart-ws-guard")
    invalidate_overview_cache()
    return {
        "whitelist": res["whitelist"],
        "changed_keys": res.get("changed_keys", []),
        "backup_dir": res.get("backup_dir", ""),
        "ws_guard_needs_restart": is_ws_guard_code_stale(),
        "control_result": control_result,
    }


def _probe_http_reachable(url, timeout=1.2):
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 500
    except urllib.error.HTTPError as e:
        return 200 <= e.code < 500
    except Exception:
        return False


def get_napcat_info(force=False):
    """Aggregate NapCat WebUI API dashboard, Docker/Native state, OneBot status/login/groups/friends."""
    env = stores.parse_bot_env()
    webui_meta = stores.get_napcat_webui_meta()
    bot_qq_env = (env.get("BOT_QQ_ID") or "").strip()

    docker_info = get_napcat_docker_status(force=force)
    status_info = napcat_get_status()
    login_info = napcat_get_login_info()
    version_info = napcat_get_version_info()
    group_res = napcat_get_group_list(force=force)
    friends = napcat_get_friend_list(force=force)
    online_clients = napcat_get_online_clients() if status_info.get("ok") else []
    groups = group_res.get("groups") or []

    # Direct authenticated NapCat WebUI API snapshot (versions, login_state/QR, OB11 config, core config, plugins)
    dashboard = napcat_api.get_dashboard_snapshot(bot_qq=bot_qq_env, include_sys=False)
    webui_api_status = dashboard.get("webui_api") or {}
    nc_versions = dashboard.get("versions") or {}
    login_state = dashboard.get("login_state") or {}

    http_url = (env.get("NAPCAT_HTTP_URL") or "http://127.0.0.1:3000").strip()
    ws_url = (env.get("NAPCAT_WS_URL") or "ws://127.0.0.1:3001").strip()
    http_port = _extract_url_port(http_url, 3000)
    ws_port = _extract_url_port(ws_url, 3001)
    guard_port = int(env.get("GUARD_LISTEN_PORT", "3002") or 3002)
    _, _, _, mgmt_port = _cc_cfg()

    napcat_online = bool(
        (status_info.get("ok") and status_info.get("online"))
        or (login_state.get("ok") and login_state.get("is_login"))
    )
    napcat_good = bool(status_info.get("ok") and status_info.get("good")) if status_info.get("ok") else napcat_online
    onebot_reachable = bool(status_info.get("ok") or login_info.get("ok"))

    webui_reachable = bool(webui_api_status.get("ok"))
    if not webui_reachable and not webui_meta.get("disabled"):
        webui_reachable = _probe_http_reachable(webui_meta["url"], timeout=1.2)

    if docker_info["available"]:
        napcat_mode = "docker"
    elif webui_reachable or onebot_reachable:
        napcat_mode = "native"
    else:
        napcat_mode = "unconfigured"

    raw_webui_token = str(webui_meta.get("token") or "")
    webui_out = {k: v for k, v in webui_meta.items() if k != "token"}
    webui_out["has_token"] = bool(raw_webui_token)
    webui_out["token_masked"] = stores._mask_secret(raw_webui_token)
    webui_out["reachable"] = webui_reachable
    webui_out["api_authenticated"] = bool(webui_api_status.get("authenticated"))
    webui_out["require_2fa"] = bool(webui_api_status.get("require_2fa"))
    webui_out["api_error"] = str(webui_api_status.get("error") or "")

    container_obj = {
        "name": "napcat",
        "available": docker_info["available"],
        "running": docker_info["running"],
        "status": docker_info["status"],
        "image": docker_info.get("image", ""),
        "started_at": docker_info["started_at"],
        "uptime_seconds": docker_info["uptime_seconds"],
    }

    uid_resolved = login_info.get("user_id") or login_state.get("uin") or bot_qq_env
    nick_resolved = login_info.get("nickname") or login_state.get("nick") or ""
    uptime_s = docker_info["uptime_seconds"]
    if docker_info["running"] and uptime_s:
        hrs = int(uptime_s // 3600)
        mins = int((uptime_s % 3600) // 60)
        container_status_str = f"Up {hrs}h {mins}m" if hrs > 0 else f"Up {mins}m"
    elif napcat_mode == "native":
        container_status_str = "native"
    else:
        container_status_str = docker_info["status"] or "unknown"

    versions_out = {
        "napcat": nc_versions.get("napcat") or version_info.get("app_version") or "",
        "qq": nc_versions.get("qq") or "",
        "protocol": version_info.get("protocol_version") or "v11",
        "app_name": version_info.get("app_name") or "NapCat.Onebot",
        "platform": nc_versions.get("platform") or "",
    }

    return {
        "timestamp": _now_iso(),
        "mode": napcat_mode,
        "online": napcat_online,
        "good": napcat_good,
        "docker_status": docker_info["status"],
        "container_status": container_status_str,
        "image": docker_info.get("image", ""),
        "uptime_seconds": uptime_s,
        "user_id": uid_resolved,
        "nickname": nick_resolved,
        "webui": webui_out,
        "webui_url": webui_out["url"],
        "webui_api": webui_api_status,
        "versions": versions_out,
        "login_state": login_state,
        "ob11_config": dashboard.get("ob11_config") or {},
        "napcat_config": dashboard.get("napcat_config") or {},
        "webui_config": dashboard.get("webui_config") or {},
        "plugins": dashboard.get("plugins") or {},
        "log_files": dashboard.get("log_files") or [],
        "sys_status": dashboard.get("sys_status"),
        "online_clients": online_clients,
        "container": container_obj,
        "docker": dict(container_obj),
        "status": {
            "ok": bool(status_info.get("ok")),
            "online": napcat_online,
            "good": napcat_good,
            "stat": status_info.get("stat") or {},
            "error": status_info.get("error", ""),
        },
        "login_info": {
            "ok": bool(login_info.get("ok") or login_state.get("is_login")),
            "user_id": uid_resolved,
            "nickname": nick_resolved,
            "uid": login_state.get("uid") or "",
            "avatar_url": login_state.get("avatar_url") or "",
            "error": login_info.get("error", ""),
        },
        "groups": groups,
        "group_count": len(groups),
        "friends": friends,
        "friend_count": len(friends),
        "group_whitelist": stores.get_group_whitelist(),
        "ws_guard_needs_restart": is_ws_guard_code_stale(),
        "ports": {
            "http": http_port,
            "ws": ws_port,
            "webui": webui_out["port"],
            "ws_guard": guard_port,
            "cc_connect": mgmt_port,
        },
        "endpoints": {
            "http_url": http_url,
            "ws_url": ws_url,
            "webui_url": webui_out["url"],
            "webui_port": webui_out["port"],
        },
        "http_url": http_url,
        "ws_url": ws_url,
    }


def get_napcat_qrcode():
    """Fetch current NapCat QQ login state + scannable SVG QR code."""
    return napcat_api.get_login_state(include_qr_svg=True)


def refresh_napcat_qrcode():
    """Ask NapCat to refresh QQ login QR code and return updated SVG + login state."""
    return napcat_api.refresh_qrcode()


def napcat_quick_login(uin):
    res = napcat_api.quick_login(uin)
    _invalidate_napcat_caches()
    return res


def napcat_set_auto_login_qq(uin):
    return napcat_api.set_auto_login_qq(uin)


def update_napcat_ob11_config(config_obj):
    res = napcat_api.set_ob11_config(config_obj)
    _invalidate_napcat_caches()
    return res


def update_napcat_core_config(updates):
    return napcat_api.set_core_config(updates)


def set_napcat_plugin_status(plugin_id, enable):
    return napcat_api.set_plugin_status(plugin_id, enable)


def get_napcat_log_files():
    return napcat_api.get_log_files()


def get_napcat_log_file(log_id):
    return napcat_api.get_log_file_content(log_id)


def get_napcat_debug_schemas():
    return napcat_api.get_debug_schemas()


_SAFE_ACTION_RE = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")


def call_napcat_debug_api(action, params=None):
    """Execute an OneBot v11 action via OneBot HTTP API (or NapCat WebUI Debug fallback)."""
    act = str(action or "").strip().lstrip("/")
    if not act or ".." in act or not _SAFE_ACTION_RE.match(act):
        raise ServiceError("无效的 OneBot 接口名（仅允许字母、数字、下划线、点、连字符）", status_code=400)
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise ServiceError("接口参数 params 必须是 JSON 对象", status_code=400)

    t0 = time.monotonic()
    base, token, _ = _napcat_cfg()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    ok, raw_resp, http_status = _http_json(
        f"{base}/{act}",
        method="POST",
        headers=headers,
        payload=params,
        timeout=8.0,
    )
    elapsed_ms = round((time.monotonic() - t0) * 1000.0, 1)

    if ok:
        retcode = raw_resp.get("retcode") if isinstance(raw_resp, dict) else 0
        status_str = str(raw_resp.get("status") or "ok") if isinstance(raw_resp, dict) else "ok"
        return {
            "ok": status_str.lower() != "failed" and (retcode in (0, None)),
            "via": "onebot_http",
            "action": act,
            "http_status": http_status,
            "status": status_str,
            "retcode": retcode,
            "elapsed_ms": elapsed_ms,
            "response": raw_resp,
            "error": "" if (status_str.lower() != "failed" and retcode in (0, None)) else (
                (raw_resp.get("message") or raw_resp.get("wording") or f"retcode={retcode}")
                if isinstance(raw_resp, dict) else ""
            ),
        }

    # Fallback to NapCat WebUI Debug endpoint if OneBot HTTP adapter is stopped/unreachable
    t1 = time.monotonic()
    ok2, data2, err2 = napcat_api.api_request(
        "POST",
        "/api/Debug/call",
        payload={"action": act, "params": params},
        timeout=8.0,
    )
    elapsed2 = round((time.monotonic() - t1) * 1000.0, 1)
    if ok2:
        return {
            "ok": True,
            "via": "napcat_webui_debug",
            "action": act,
            "http_status": 200,
            "status": "ok",
            "retcode": 0,
            "elapsed_ms": elapsed2,
            "response": data2,
            "error": "",
        }

    return {
        "ok": False,
        "via": "onebot_http",
        "action": act,
        "http_status": http_status,
        "status": "failed",
        "retcode": -1,
        "elapsed_ms": elapsed_ms,
        "response": None,
        "error": f"OneBot HTTP 调用失败：{raw_resp}；NapCat Debug 兜底失败：{err2}",
    }


def restart_napcat_process():
    res = napcat_api.restart_napcat_process()
    _invalidate_napcat_caches()
    return res


def get_napcat_logs(lines=120):
    """Fetch recent `docker logs` for napcat, stripping ANSI escapes & detecting QR/login hints."""
    try:
        n = int(lines)
    except (ValueError, TypeError):
        n = 120
    n = min(max(n, 20), 500)

    env = bot_env.build_subprocess_env()

    try:
        proc = subprocess.run(
            ["docker", "logs", "--tail", str(n), "--timestamps", "napcat"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=4.0,
            env=env,
            creationflags=bot_env.subprocess_creationflags(),
        )
    except FileNotFoundError:
        return {
            "ok": False,
            "lines": [],
            "requested_lines": n,
            "count": 0,
            "clean_lines": [],
            "clean_text": "",
            "raw": "",
            "has_qr_hint": False,
            "has_login_hint": False,
            "login_hints": [],
            "error": "未检测到 Docker 命令（如使用本机原生 NapCat，请直接在 NapCat WebUI 或本地终端窗口查看日志与扫码二维码）",
        }
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "lines": [],
            "requested_lines": n,
            "count": 0,
            "clean_lines": [],
            "clean_text": "",
            "raw": "",
            "has_qr_hint": False,
            "has_login_hint": False,
            "login_hints": [],
            "error": "读取 NapCat 容器日志超时（>4s）",
        }
    except Exception as e:
        return {
            "ok": False,
            "lines": [],
            "requested_lines": n,
            "count": 0,
            "clean_lines": [],
            "clean_text": "",
            "raw": "",
            "has_qr_hint": False,
            "has_login_hint": False,
            "login_hints": [],
            "error": f"执行 docker logs 失败：{e}",
        }

    raw_text = proc.stdout or ""
    raw_lines = raw_text.splitlines()
    clean_lines = [_ANSI_ESCAPE_RE.sub("", line).rstrip("\r") for line in raw_lines]

    has_qr_hint = False
    login_hints = []
    for cl in clean_lines:
        if _QR_BLOCK_RE.search(cl) or _QR_TEXT_RE.search(cl):
            has_qr_hint = True
        if _LOGIN_HINT_RE.search(cl) and not _QR_BLOCK_RE.search(cl):
            login_hints.append(cl)

    return {
        "ok": proc.returncode == 0,
        "lines": clean_lines,
        "requested_lines": n,
        "count": len(clean_lines),
        "clean_lines": clean_lines,
        "clean_text": "\n".join(clean_lines),
        "raw": raw_text,
        "has_qr_hint": has_qr_hint,
        "has_login_hint": bool(login_hints) or has_qr_hint,
        "login_hints": login_hints[-12:],
        "error": "" if proc.returncode == 0 else (clean_lines[-1] if clean_lines else f"docker logs 返回码 {proc.returncode}"),
    }


def control_napcat_container(action):
    """Start, stop, or restart the local `napcat` Docker container safely."""
    act = str(action or "").strip().lower()
    if act not in ("start", "stop", "restart"):
        raise ServiceError(f"不支持的 NapCat 容器操作：{action}", status_code=400)

    env = bot_env.build_subprocess_env()
    cflags = bot_env.subprocess_creationflags()

    try:
        if act == "start":
            proc = subprocess.run(
                ["docker", "start", "napcat"],
                cwd=PROJECT_DIR,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=20.0,
                env=env,
                creationflags=cflags,
            )
            output = _ANSI_ESCAPE_RE.sub("", (proc.stdout or "")).strip()
            if proc.returncode == 0:
                _invalidate_napcat_caches()
                return {
                    "action": act,
                    "ok": True,
                    "message": "NapCat 容器已启动，请稍候几秒等待 QQ 连接就绪。",
                    "output": output,
                }
            compose_path = os.path.join(PROJECT_DIR, "docker-compose.napcat.yml")
            if not os.path.isfile(compose_path):
                raise ServiceError(f"docker start 失败且未找到 docker-compose.napcat.yml：{output}", status_code=500)
            cproc = subprocess.run(
                ["docker", "compose", "-f", compose_path, "up", "-d"],
                cwd=PROJECT_DIR,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=45.0,
                env=env,
                creationflags=cflags,
            )
            coutput = _ANSI_ESCAPE_RE.sub("", (cproc.stdout or "")).strip()
            if cproc.returncode != 0:
                raise ServiceError(f"启动 NapCat 容器失败：{coutput or output}", status_code=500)
            _invalidate_napcat_caches()
            return {
                "action": act,
                "ok": True,
                "message": "已通过 docker-compose.napcat.yml 启动 NapCat 容器。",
                "output": coutput,
            }

        if act == "stop":
            proc = subprocess.run(
                ["docker", "stop", "napcat"],
                cwd=PROJECT_DIR,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=25.0,
                env=env,
                creationflags=cflags,
            )
            output = _ANSI_ESCAPE_RE.sub("", (proc.stdout or "")).strip()
            if proc.returncode != 0:
                raise ServiceError(f"停止 NapCat 容器失败：{output}", status_code=500)
            _invalidate_napcat_caches()
            return {
                "action": act,
                "ok": True,
                "message": "NapCat 容器已停止。",
                "output": output,
            }

        # act == "restart"
        proc = subprocess.run(
            ["docker", "restart", "napcat"],
            cwd=PROJECT_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30.0,
            env=env,
            creationflags=cflags,
        )
        output = _ANSI_ESCAPE_RE.sub("", (proc.stdout or "")).strip()
        if proc.returncode != 0:
            raise ServiceError(f"重启 NapCat 容器失败：{output}", status_code=500)
        _invalidate_napcat_caches()
        return {
            "action": act,
            "ok": True,
            "message": "NapCat 容器已重启，请稍候几秒刷新在线状态。",
            "output": output,
        }
    except ServiceError:
        raise
    except subprocess.TimeoutExpired:
        raise ServiceError(f"NapCat 容器操作（{act}）执行超时", status_code=504)
    except Exception as e:
        raise ServiceError(f"NapCat 容器操作（{act}）失败：{e}", status_code=500)


# ---------------------------------------------------------------------------
# cc-connect Management API Client + Disk Session Fallback
# ---------------------------------------------------------------------------

def _cc_cfg():
    toml = stores.parse_config_toml()
    port = toml["management"]["port"]
    token = toml["management"]["token"]
    project = toml["project"]["name"] or "qq-gemini-acp"
    return f"http://127.0.0.1:{port}", token, project, port


def _cc_call(path, method="GET", payload=None, timeout=2.5):
    base, token, _, _ = _cc_cfg()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    ok, res, status = _http_json(f"{base}{path}", method=method, headers=headers, payload=payload, timeout=timeout)
    if not ok:
        return False, res, status
    if isinstance(res, dict) and "ok" in res:
        if not res["ok"]:
            return False, res.get("error") or "cc-connect API 返回失败", status
        return True, res.get("data"), status
    return True, res, status


def cc_get_status():
    ok, data, _ = _cc_call("/api/v1/status")
    return ok, data


def _load_disk_session_store():
    """Read latest data/sessions/*.json as fallback when cc-connect is stopped."""
    if not os.path.isdir(SESSIONS_DIR):
        return None
    candidates = [
        os.path.join(SESSIONS_DIR, f)
        for f in os.listdir(SESSIONS_DIR)
        if f.endswith(".json") and not f.startswith(".")
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    try:
        with open(candidates[0], "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _build_sid_to_key_map():
    store = _load_disk_session_store()
    if not isinstance(store, dict):
        return {}, {}
    sid_to_key = {}
    user_meta = store.get("user_meta") or {}
    for skey, sids in (store.get("user_sessions") or {}).items():
        if isinstance(sids, list):
            for sid in sids:
                sid_to_key[str(sid)] = str(skey)
    for skey, sid in (store.get("active_session") or {}).items():
        if sid:
            sid_to_key[str(sid)] = str(skey)
    return sid_to_key, user_meta


def _parse_session_key(session_key, admin_ids):
    sk = str(session_key or "")
    if sk.startswith("qq:g:"):
        gid = sk[len("qq:g:"):]
        return True, gid, False
    if sk.startswith("qq:"):
        uid = sk[len("qq:"):]
        return False, uid, (uid in admin_ids)
    return False, sk, False


def _enrich_session_item(raw, group_map, mem_names, admin_ids):
    sid = str(raw.get("id") or "")
    skey = str(raw.get("session_key") or "")
    is_group, target_id, is_admin_dm = _parse_session_key(skey, admin_ids)

    raw_chat_name = str(raw.get("chat_name") or "").strip()
    raw_user_name = str(raw.get("user_name") or "").strip()
    raw_name = str(raw.get("name") or "").strip()

    if is_group:
        resolved_name = group_map.get(target_id) or raw_chat_name or raw_name or f"QQ 群 {target_id}"
    else:
        resolved_name = raw_user_name or mem_names.get(target_id) or raw_name or f"QQ 用户 {target_id}"

    last_msg = raw.get("last_message")
    last_msg_clean = None
    if isinstance(last_msg, dict):
        lm_content = str(last_msg.get("content") or "")
        lm_content = _SENDER_HEADER_RE.sub("", lm_content).strip()
        last_msg_clean = {
            "role": str(last_msg.get("role") or ""),
            "content": lm_content,
            "is_silent": bool(last_msg.get("role") == "assistant" and _NO_REPLY_RE.match(lm_content)),
            "timestamp": str(last_msg.get("timestamp") or ""),
        }

    return {
        "id": sid,
        "session_key": skey,
        "platform": str(raw.get("platform") or "qq"),
        "agent_type": str(raw.get("agent_type") or "acp"),
        "is_group": is_group,
        "type": "group" if is_group else "private",
        "target_id": target_id,
        "group_id": target_id if is_group else "",
        "user_id": "" if is_group else target_id,
        "is_admin_dm": is_admin_dm,
        "work_dir_mode": "workspace" if is_admin_dm else "chat-sandbox",
        "name": resolved_name,
        "display_name": resolved_name,
        "chat_name": raw_chat_name or (resolved_name if is_group else ""),
        "user_name": raw_user_name or ("" if is_group else resolved_name),
        "live": bool(raw.get("live", False)),
        "active": bool(raw.get("active", False)),
        "history_count": int(raw.get("history_count") or 0),
        "message_count": int(raw.get("history_count") or 0),
        "created_at": str(raw.get("created_at") or ""),
        "updated_at": str(raw.get("updated_at") or ""),
        "last_user_activity": str(raw.get("last_user_activity") or raw.get("updated_at") or ""),
        "last_message": last_msg_clean,
    }


def list_sessions():
    env = stores.parse_bot_env()
    admin_ids = set(stores._split_csv(env.get("ADMIN_QQ_IDS", "")))
    group_map = napcat_get_group_list().get("group_map", {})
    mem_names = stores._memory_user_map()

    _, _, proj, _ = _cc_cfg()
    ok, data, _ = _cc_call(f"/api/v1/projects/{urllib.parse.quote(proj, safe='')}/sessions")
    source = "cc-connect"
    raw_list = []

    if ok and isinstance(data, dict) and isinstance(data.get("sessions"), list):
        raw_list = data["sessions"]
    else:
        source = "disk-fallback"
        disk = _load_disk_session_store()
        if isinstance(disk, dict):
            sessions_map = disk.get("sessions") or {}
            active_map = disk.get("active_session") or {}
            user_meta = disk.get("user_meta") or {}
            sid_to_key = {}
            for skey, sids in (disk.get("user_sessions") or {}).items():
                if isinstance(sids, list):
                    for sid in sids:
                        sid_to_key[str(sid)] = str(skey)
            for sid, sobj in sessions_map.items():
                if not isinstance(sobj, dict):
                    continue
                skey = sid_to_key.get(sid, "")
                hist = sobj.get("history") or []
                meta = user_meta.get(skey) or {}
                raw_list.append({
                    "id": sid,
                    "name": sobj.get("name", ""),
                    "session_key": skey,
                    "platform": "qq",
                    "agent_type": sobj.get("agent_type", "acp"),
                    "active": active_map.get(skey) == sid,
                    "live": False,
                    "history_count": len(hist),
                    "created_at": sobj.get("created_at", ""),
                    "updated_at": sobj.get("updated_at", ""),
                    "last_user_activity": sobj.get("last_user_activity", ""),
                    "user_name": meta.get("user_name", ""),
                    "chat_name": meta.get("chat_name", ""),
                    "last_message": hist[-1] if hist else None,
                })

    enriched = [_enrich_session_item(item, group_map, mem_names, admin_ids) for item in raw_list]
    enriched.sort(
        key=lambda s: (1 if s["live"] else 0, 1 if s["active"] else 0, s["updated_at"]),
        reverse=True,
    )

    live_count = sum(1 for s in enriched if s["live"])
    active_count = sum(1 for s in enriched if s["active"])

    return {
        "sessions": enriched,
        "source": source,
        "total": len(enriched),
        "live_count": live_count,
        "active_count": active_count,
    }


def parse_session_messages(history, limit=120, hide_silent=False, query=""):
    """Clean cc-connect sender prefixes & tag NO_REPLY / FOLLOW_REPEAT messages."""
    q_low = (query or "").strip().lower()
    silent_count = 0
    replied_count = 0
    follow_repeat_count = 0
    parsed_all = []

    for idx, msg in enumerate(history or []):
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "user")
        raw_content = str(msg.get("content") or "")
        ts = str(msg.get("timestamp") or "")

        sender_id = ""
        sender_name = ""
        platform = "qq"
        chat_id = ""
        content = raw_content

        m = _SENDER_HEADER_RE.match(raw_content)
        if m:
            sender_id = m.group(1) or ""
            sender_name = m.group(2) or ""
            platform = m.group(3) or "qq"
            chat_id = m.group(4) or ""
            content = raw_content[m.end():]

        stripped = content.strip()
        is_silent = bool(role == "assistant" and _NO_REPLY_RE.match(stripped))
        is_follow_repeat = bool(role == "assistant" and stripped.upper() == "FOLLOW_REPEAT")
        media_only = bool(role == "user" and stripped == "")

        if role == "assistant":
            if is_silent:
                silent_count += 1
            else:
                replied_count += 1
            if is_follow_repeat:
                follow_repeat_count += 1

        if hide_silent and is_silent:
            continue
        if q_low:
            hay = f"{content} {sender_name} {sender_id}".lower()
            if q_low not in hay:
                continue

        parsed_all.append({
            "index": idx,
            "role": role,
            "sender_id": sender_id,
            "sender_name": sender_name,
            "platform": platform,
            "chat_id": chat_id,
            "content": content,
            "media_only": media_only,
            "is_silent": is_silent,
            "is_follow_repeat": is_follow_repeat,
            "timestamp": ts,
        })

    if limit and limit > 0 and len(parsed_all) > limit:
        parsed_all = parsed_all[-limit:]

    return {
        "messages": parsed_all,
        "total_history": len(history or []),
        "returned": len(parsed_all),
        "silent_count": silent_count,
        "replied_count": replied_count,
        "follow_repeat_count": follow_repeat_count,
    }


def get_session_detail(session_id, limit=120, hide_silent=False, query=""):
    sid = str(session_id or "").strip()
    if not sid or "/" in sid or ".." in sid:
        raise ServiceError("无效的会话 ID")

    lim = min(max(int(limit or 120), 1), 1000)
    # Fetch extra history if filtering so caller still gets up to `limit` items
    fetch_lim = min(lim * 3, 1500) if (hide_silent or query) else lim

    env = stores.parse_bot_env()
    admin_ids = set(stores._split_csv(env.get("ADMIN_QQ_IDS", "")))
    group_map = napcat_get_group_list().get("group_map", {})
    mem_names = stores._memory_user_map()

    _, _, proj, _ = _cc_cfg()
    ok, data, status = _cc_call(
        f"/api/v1/projects/{urllib.parse.quote(proj, safe='')}/sessions/{urllib.parse.quote(sid, safe='')}?history_limit={fetch_lim}"
    )
    source = "cc-connect"
    raw_session = None

    if ok and isinstance(data, dict):
        raw_session = data
    elif status == 404:
        raise stores.NotFoundError(f"找不到会话：{sid}")
    else:
        source = "disk-fallback"
        disk = _load_disk_session_store()
        if isinstance(disk, dict) and sid in (disk.get("sessions") or {}):
            sobj = disk["sessions"][sid]
            sid_to_key, user_meta = _build_sid_to_key_map()
            skey = sid_to_key.get(sid, "")
            hist = sobj.get("history") or []
            meta = user_meta.get(skey) or {}
            raw_session = {
                "id": sid,
                "name": sobj.get("name", ""),
                "session_key": skey,
                "agent_session_id": sobj.get("agent_session_id", ""),
                "agent_type": sobj.get("agent_type", "acp"),
                "platform": "qq",
                "active": (disk.get("active_session") or {}).get(skey) == sid,
                "live": False,
                "history_count": len(hist),
                "history": hist[-fetch_lim:],
                "created_at": sobj.get("created_at", ""),
                "updated_at": sobj.get("updated_at", ""),
                "user_name": meta.get("user_name", ""),
                "chat_name": meta.get("chat_name", ""),
            }
        else:
            raise stores.NotFoundError(f"找不到会话：{sid}")

    session_meta = _enrich_session_item(raw_session, group_map, mem_names, admin_ids)
    session_meta["agent_session_id"] = str(raw_session.get("agent_session_id") or "")

    parsed = parse_session_messages(
        raw_session.get("history") or [],
        limit=lim,
        hide_silent=hide_silent,
        query=query,
    )

    return {
        "session": session_meta,
        "id": session_meta["id"],
        "session_key": session_meta["session_key"],
        "agent_session_id": session_meta["agent_session_id"],
        "source": source,
        "messages": parsed["messages"],
        "total_history": parsed["total_history"],
        "history_count": session_meta["history_count"],
        "silent_count": parsed["silent_count"],
        "replied_count": parsed["replied_count"],
        "follow_repeat_count": parsed["follow_repeat_count"],
    }


def delete_session(session_id):
    sid = str(session_id or "").strip()
    if not sid or "/" in sid or ".." in sid:
        raise ServiceError("无效的会话 ID")
    _, _, proj, _ = _cc_cfg()
    ok, data, status = _cc_call(
        f"/api/v1/projects/{urllib.parse.quote(proj, safe='')}/sessions/{urllib.parse.quote(sid, safe='')}",
        method="DELETE",
        timeout=3.0,
    )
    if not ok:
        raise ServiceError(f"删除会话失败：{data}", status_code=status or 502)
    return {"deleted": True, "session_id": sid, "detail": data}


def send_session_message(payload):
    if not isinstance(payload, dict):
        raise ServiceError("请求体必须是 JSON 对象")

    message = str(payload.get("message") or payload.get("text") or "").strip()
    if not message:
        raise ServiceError("发送消息内容不能为空")

    session_key = str(payload.get("session_key") or "").strip()
    chat_type = str(payload.get("chat_type") or payload.get("target_type") or "").strip().lower()
    target_id = str(payload.get("target_id") or payload.get("group_id") or payload.get("user_id") or "").strip()
    channel = str(payload.get("channel") or payload.get("via") or payload.get("mode") or "cc-connect").strip().lower()

    if not session_key and target_id:
        session_key = f"qq:g:{target_id}" if chat_type == "group" else f"qq:{target_id}"

    if not target_id and session_key:
        is_grp, tid, _ = _parse_session_key(session_key, set())
        target_id = tid
        if not chat_type:
            chat_type = "group" if is_grp else "private"

    if channel == "napcat":
        if not target_id:
            raise ServiceError("缺少目标群号或 QQ 号")
        if chat_type == "group" or session_key.startswith("qq:g:"):
            res = napcat_send_group_msg(target_id, message)
        else:
            res = napcat_send_private_msg(target_id, message)
        return {"sent": True, "channel": "napcat", "target_id": target_id, "result": res}

    if not session_key:
        raise ServiceError("缺少 session_key")

    _, _, proj, _ = _cc_cfg()
    ok, data, status = _cc_call(
        f"/api/v1/projects/{urllib.parse.quote(proj, safe='')}/send",
        method="POST",
        payload={"session_key": session_key, "message": message},
        timeout=5.0,
    )
    if not ok:
        raise ServiceError(f"cc-connect 发送失败：{data}", status_code=status or 502)
    return {"sent": True, "channel": "cc-connect", "session_key": session_key, "result": data}


# ---------------------------------------------------------------------------
# Process Inspector (POSIX `ps` / Windows `Get-CimInstance Win32_Process`)
# ---------------------------------------------------------------------------

_WIN_DATE_MS_RE = re.compile(r"/Date\((-?\d+)")
_WIN_DMTF_RE = re.compile(r"^(\d{14})\.")


def _parse_ps_lstart(lstart_str):
    try:
        dt = datetime.datetime.strptime(lstart_str.strip(), "%a %b %d %H:%M:%S %Y")
        return max(0.0, (datetime.datetime.now() - dt).total_seconds())
    except Exception:
        return None


def _parse_win_creation_date(raw):
    """Parse PowerShell ConvertTo-Json CreationDate into (started_str, uptime_seconds)."""
    if not raw:
        return "", None
    s = str(raw).strip()
    if not s:
        return "", None

    # Format 1: Windows PowerShell 5.1 JSON DateTime: "/Date(1700000000123)/"
    m_ms = _WIN_DATE_MS_RE.search(s)
    if m_ms:
        try:
            epoch = int(m_ms.group(1)) / 1000.0
            dt = datetime.datetime.fromtimestamp(epoch).astimezone()
            return dt.isoformat(timespec="seconds"), max(0.0, time.time() - epoch)
        except Exception:
            return s, None

    # Format 2: ISO 8601 (PowerShell 7+)
    dt = _parse_iso_dt(s)
    if dt is not None:
        try:
            if dt.tzinfo is not None:
                now = datetime.datetime.now(datetime.timezone.utc)
                uptime = max(0.0, (now - dt.astimezone(datetime.timezone.utc)).total_seconds())
            else:
                uptime = max(0.0, (datetime.datetime.now() - dt).total_seconds())
            return dt.isoformat(timespec="seconds"), uptime
        except Exception:
            return s, None

    # Format 3: WMI DMTF "YYYYMMDDHHMMSS.ffffff+ZZZ"
    m_dmtf = _WIN_DMTF_RE.match(s)
    if m_dmtf:
        try:
            dt_naive = datetime.datetime.strptime(m_dmtf.group(1), "%Y%m%d%H%M%S")
            uptime = max(0.0, (datetime.datetime.now() - dt_naive).total_seconds())
            return dt_naive.isoformat(timespec="seconds"), uptime
        except Exception:
            return s, None

    return s, None


def _query_processes_posix():
    procs = {}
    try:
        out = subprocess.check_output(
            ["ps", "-axo", "pid,ppid,pcpu,rss,lstart,command"],
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=2.5,
            env=bot_env.build_subprocess_env({"LC_ALL": "C"}),
            creationflags=bot_env.subprocess_creationflags(),
        )
        for line in out.splitlines()[1:]:
            m = _PS_LINE_RE.match(line)
            if not m:
                continue
            pid = int(m.group(1))
            ppid = int(m.group(2))
            cpu = float(m.group(3))
            rss_kb = int(m.group(4))
            lstart = m.group(5)
            cmd = m.group(6)
            uptime_s = _parse_ps_lstart(lstart)
            procs[pid] = {
                "pid": pid,
                "ppid": ppid,
                "cpu": cpu,
                "rss_kb": rss_kb,
                "rss_mb": round(rss_kb / 1024.0, 1),
                "started": lstart,
                "started_at": lstart,
                "uptime_seconds": round(uptime_s, 1) if uptime_s is not None else None,
                "command": cmd,
            }
    except Exception:
        pass
    return procs


def _query_processes_windows():
    now = time.monotonic()
    with _win_proc_cache_lock:
        if _win_proc_cache["ts"] > 0 and (now - _win_proc_cache["ts"]) < _WIN_PROC_CACHE_TTL:
            return {pid: dict(info) for pid, info in _win_proc_cache["procs"].items()}

    procs = {}
    ps_cmd = (
        "Get-CimInstance Win32_Process | "
        "Select-Object ProcessId,ParentProcessId,WorkingSetSize,CreationDate,CommandLine | "
        "ConvertTo-Json -Compress"
    )
    try:
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5.0,
            env=bot_env.build_subprocess_env(),
            creationflags=bot_env.subprocess_creationflags(),
        )
        raw = (proc.stdout or "").strip()
        if proc.returncode == 0 and raw:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                parsed = [parsed]
            if isinstance(parsed, list):
                for item in parsed:
                    if not isinstance(item, dict):
                        continue
                    try:
                        pid = int(item.get("ProcessId") or 0)
                    except (ValueError, TypeError):
                        continue
                    if pid <= 0:
                        continue
                    try:
                        ppid = int(item.get("ParentProcessId") or 0)
                    except (ValueError, TypeError):
                        ppid = 0
                    try:
                        ws_bytes = int(item.get("WorkingSetSize") or 0)
                    except (ValueError, TypeError):
                        ws_bytes = 0
                    rss_kb = max(0, ws_bytes // 1024)
                    started_str, uptime_s = _parse_win_creation_date(item.get("CreationDate"))
                    cmd = str(item.get("CommandLine") or "")
                    procs[pid] = {
                        "pid": pid,
                        "ppid": ppid,
                        "cpu": 0.0,
                        "rss_kb": rss_kb,
                        "rss_mb": round(rss_kb / 1024.0, 1),
                        "started": started_str,
                        "started_at": started_str,
                        "uptime_seconds": round(uptime_s, 1) if uptime_s is not None else None,
                        "command": cmd,
                    }
    except Exception:
        pass

    with _win_proc_cache_lock:
        _win_proc_cache["ts"] = time.monotonic()
        _win_proc_cache["procs"] = procs
    return {pid: dict(info) for pid, info in procs.items()}


def _synthetic_proc(pid):
    return {
        "pid": int(pid),
        "ppid": 0,
        "cpu": 0.0,
        "rss_kb": 0,
        "rss_mb": 0.0,
        "started": "",
        "started_at": "",
        "uptime_seconds": None,
        "command": "",
    }


def _cmd_lower(cmd):
    return str(cmd or "").replace("\\", "/").lower()


def inspect_processes(force=False):
    """Inspect local bot processes cross-checked against data/run/*.pid (3s TTL cache)."""
    now_mono = time.monotonic()
    with _proc_cache_lock:
        if (
            not force
            and _proc_cache["data"] is not None
            and (now_mono - _proc_cache["ts"]) < _PROC_CACHE_TTL
        ):
            return copy.deepcopy(_proc_cache["data"])

    sup_pid_file = _read_pid_file(GUARD_SUP_PID_FILE)
    worker_pid_file = _read_pid_file(GUARD_WORKER_PID_FILE)
    cc_pid_file = _read_pid_file(CC_CONNECT_PID_FILE)
    start_pid_file = _read_pid_file(START_PID_FILE)

    procs = _query_processes_windows() if os.name == "nt" else _query_processes_posix()

    def _is_ws_supervisor_cmd(cmd_low):
        if "bot_runner.py" in cmd_low and "--supervise-ws-guard" in cmd_low:
            return True
        if "onebot-ws-guard.py" in cmd_low and ("while true" in cmd_low or "bash -c" in cmd_low):
            return True
        return False

    def _is_ws_worker_cmd(cmd_low):
        return (
            "onebot-ws-guard.py" in cmd_low
            and "while true" not in cmd_low
            and "bash -c" not in cmd_low
            and "bot_runner.py" not in cmd_low
            and "--supervise-ws-guard" not in cmd_low
        )

    def _is_cc_supervisor_cmd(cmd_low):
        return "bot_runner.py" in cmd_low and "--supervise-cc" in cmd_low

    def _is_cc_connect_cmd(cmd_low):
        return (
            ("cc-connect" in cmd_low or "cc-connect.exe" in cmd_low)
            and "--config" in cmd_low
            and "config.toml" in cmd_low
            and "bot_runner.py" not in cmd_low
            and "--supervise-cc" not in cmd_low
        )

    ws_sup = None
    if sup_pid_file and sup_pid_file in procs:
        cand = procs[sup_pid_file]
        clow = _cmd_lower(cand["command"])
        if not clow or _is_ws_supervisor_cmd(clow) or "onebot-ws-guard.py" in clow or "bot_runner.py" in clow:
            ws_sup = cand
    if ws_sup is None:
        for p in procs.values():
            if _is_ws_supervisor_cmd(_cmd_lower(p["command"])):
                ws_sup = p
                break
    if ws_sup is None and sup_pid_file and _pid_alive(sup_pid_file):
        ws_sup = _synthetic_proc(sup_pid_file)

    ws_worker = None
    if worker_pid_file and worker_pid_file in procs:
        cand = procs[worker_pid_file]
        clow = _cmd_lower(cand["command"])
        if not clow or _is_ws_worker_cmd(clow):
            ws_worker = cand
    if ws_worker is None:
        for p in procs.values():
            if _is_ws_worker_cmd(_cmd_lower(p["command"])):
                if ws_sup and p["ppid"] == ws_sup["pid"]:
                    ws_worker = p
                    break
                if ws_worker is None:
                    ws_worker = p
    if ws_worker is None and worker_pid_file and _pid_alive(worker_pid_file):
        ws_worker = procs.get(worker_pid_file) or _synthetic_proc(worker_pid_file)

    cc_proc = None
    if cc_pid_file and cc_pid_file in procs:
        cand = procs[cc_pid_file]
        clow = _cmd_lower(cand["command"])
        if not clow or _is_cc_connect_cmd(clow):
            cc_proc = cand
    if cc_proc is None:
        for p in procs.values():
            if _is_cc_connect_cmd(_cmd_lower(p["command"])):
                cc_proc = p
                break
    if cc_proc is None and cc_pid_file and _pid_alive(cc_pid_file):
        cand = procs.get(cc_pid_file)
        if cand is None or not _is_cc_supervisor_cmd(_cmd_lower(cand["command"])):
            cc_proc = cand or _synthetic_proc(cc_pid_file)

    my_pid = os.getpid()
    wrappers = []
    for p in procs.values():
        if p["pid"] == my_pid:
            continue
        clow = _cmd_lower(p["command"])
        if (
            "agy-acp-wrapper.py" in clow
            and "bot_runner.py" not in clow
            and "bash -c" not in clow
            and "sh -c" not in clow
        ):
            wrappers.append({
                "pid": p["pid"],
                "ppid": p["ppid"],
                "cpu": p["cpu"],
                "rss_mb": p["rss_mb"],
                "started": p["started"],
                "started_at": p["started_at"],
                "uptime_seconds": p["uptime_seconds"],
            })
    wrappers.sort(key=lambda w: w["pid"])

    start_loop_pid = None
    if start_pid_file and _pid_alive(start_pid_file):
        start_loop_pid = start_pid_file
    else:
        for p in procs.values():
            if _is_cc_supervisor_cmd(_cmd_lower(p["command"])):
                start_loop_pid = p["pid"]
                break

    result = {
        "start_loop_pid": start_loop_pid,
        "ws_guard_supervisor": ws_sup,
        "ws_guard_worker": ws_worker,
        "cc_connect": cc_proc,
        "acp_wrappers": wrappers,
        "acp_total_rss_mb": round(sum(w["rss_mb"] for w in wrappers), 1),
    }
    with _proc_cache_lock:
        _proc_cache["ts"] = time.monotonic()
        _proc_cache["data"] = copy.deepcopy(result)
    return result


# ---------------------------------------------------------------------------
# Log Parsers (cc-connect slog logfmt + ws-guard)
# ---------------------------------------------------------------------------

def _unescape_logfmt_val(val):
    if len(val) >= 2 and val[0] == '"' and val[-1] == '"':
        try:
            return json.loads(val)
        except Exception:
            return val[1:-1]
    return val


def _make_log_id(source, offset, raw_line):
    digest = hashlib.sha1(raw_line.encode("utf-8", errors="replace")).hexdigest()[:8]
    return f"{source}:{offset}:{digest}"


def parse_cc_log_line(raw_line, offset=0):
    line = raw_line.rstrip("\r\n")
    entry_id = _make_log_id("cc-connect", offset, line)

    if line.startswith("time=") and " level=" in line:
        pairs = _LOGFMT_TOKEN_RE.findall(line)
        fields = {}
        ts = ""
        level = "INFO"
        msg = ""
        for k, v in pairs:
            uv = _unescape_logfmt_val(v)
            if k == "time":
                ts = str(uv)
            elif k == "level":
                level = str(uv).upper()
            elif k == "msg":
                msg = str(uv)
            else:
                fields[k] = uv
        return {
            "id": entry_id,
            "source": "cc-connect",
            "time": ts,
            "ts": ts,
            "level": level,
            "msg": msg or line,
            "fields": fields,
            "raw": line,
        }

    # Banner / shell line in cc-connect.log
    level = "INFO"
    low = line.lower()
    if "error" in low or "失败" in line or "panic" in low:
        level = "ERROR"
    elif "warn" in low or "异常退出" in line:
        level = "WARN"
    return {
        "id": entry_id,
        "source": "cc-connect",
        "time": "",
        "ts": "",
        "level": level,
        "msg": line,
        "fields": {},
        "raw": line,
    }


def parse_ws_guard_log_line(raw_line, offset=0):
    line = raw_line.rstrip("\r\n")
    entry_id = _make_log_id("ws-guard", offset, line)

    m = _WS_GUARD_LINE_RE.match(line)
    if m:
        ts = m.group(1)
        msg = m.group(2)
    else:
        ts = ""
        msg = line

    low = msg.lower()
    if any(k in low for k in ("error", "traceback", "exception", "failed")):
        level = "ERROR"
    elif any(k in low for k in ("exited", "closed", "timeout", "terminated", "rejected", "retry", "fallback", "busy ttl")):
        level = "WARN"
    else:
        level = "INFO"

    fields = {}
    gm = re.search(r"\b(g:\d+|u:\d+)\b", msg)
    if gm:
        fields["chat"] = gm.group(1)

    return {
        "id": entry_id,
        "source": "ws-guard",
        "time": ts,
        "ts": ts,
        "level": level,
        "msg": msg,
        "fields": fields,
        "raw": line,
    }


def _read_tail_lines_with_offsets(path, max_lines=200, max_bytes=512 * 1024):
    """Read last `max_lines` of a file along with each line's starting byte offset."""
    if not os.path.isfile(path):
        return []
    try:
        size = os.path.getsize(path)
        start = max(0, size - max_bytes)
        with open(path, "rb") as f:
            f.seek(start)
            chunk = f.read()
    except OSError:
        return []

    items = []
    pos = 0
    if start > 0:
        nl = chunk.find(b"\n")
        if nl != -1:
            pos = nl + 1

    while pos < len(chunk):
        nl = chunk.find(b"\n", pos)
        if nl == -1:
            raw = chunk[pos:]
            line_off = start + pos
            pos = len(chunk)
        else:
            raw = chunk[pos:nl]
            line_off = start + pos
            pos = nl + 1
        text = raw.decode("utf-8", errors="replace").rstrip("\r")
        if text:
            items.append((line_off, text))

    return items[-max_lines:]


def _entry_sort_ts(entry):
    ts = entry.get("time") or ""
    dt = _parse_iso_dt(ts)
    if dt:
        if dt.tzinfo is None:
            return dt.timestamp()
        return dt.timestamp()
    return 0.0


def tail_logs(source="all", lines=150, level="", query="", max_bytes=512 * 1024):
    src = str(source or "all").strip().lower()
    if src not in ("all", "cc-connect", "ws-guard"):
        src = "all"
    lim = min(max(int(lines or 150), 1), 2000)
    lvl_filter = str(level or "").strip().upper()
    q_low = str(query or "").strip().lower()
    read_bytes = max(1024, int(max_bytes or (512 * 1024)))

    entries = []
    if src in ("all", "cc-connect"):
        for off, line in _read_tail_lines_with_offsets(CC_LOG_PATH, max_lines=lim, max_bytes=read_bytes):
            entries.append(parse_cc_log_line(line, offset=off))
    if src in ("all", "ws-guard"):
        for off, line in _read_tail_lines_with_offsets(WS_GUARD_LOG_PATH, max_lines=lim, max_bytes=read_bytes):
            entries.append(parse_ws_guard_log_line(line, offset=off))

    if src == "all":
        entries.sort(key=_entry_sort_ts)

    if lvl_filter and lvl_filter != "ALL":
        entries = [e for e in entries if e["level"] == lvl_filter]
    if q_low:
        entries = [e for e in entries if q_low in e["raw"].lower() or q_low in e["msg"].lower()]

    if len(entries) > lim:
        entries = entries[-lim:]

    return {
        "source": src,
        "count": len(entries),
        "entries": entries,
    }


# ---------------------------------------------------------------------------
# Analytics Aggregator
# ---------------------------------------------------------------------------

def _percentile(sorted_vals, pct):
    if not sorted_vals:
        return 0.0
    k = (len(sorted_vals) - 1) * (pct / 100.0)
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    if f == c:
        return sorted_vals[f]
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


def _file_stat_sig(path):
    try:
        st = os.stat(path)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return (0, 0)


def get_analytics(force=False):
    now_local = datetime.datetime.now().astimezone().replace(minute=0, second=0, microsecond=0)
    cc_sig = _file_stat_sig(CC_LOG_PATH)
    ws_sig = _file_stat_sig(WS_GUARD_LOG_PATH)
    cache_key = (cc_sig[0], cc_sig[1], ws_sig[0], ws_sig[1], now_local.strftime("%Y-%m-%dT%H"))
    now_mono = time.monotonic()

    with _analytics_cache_lock:
        if (
            not force
            and _analytics_cache["data"] is not None
            and _analytics_cache["key"] == cache_key
            and (now_mono - _analytics_cache["ts"]) < _ANALYTICS_CACHE_TTL
        ):
            return copy.deepcopy(_analytics_cache["data"])

    sid_to_key, user_meta = _build_sid_to_key_map()
    group_map = napcat_get_group_list().get("group_map", {})
    mem_names = stores._memory_user_map()

    def _chat_display(session_key):
        sk = str(session_key or "")
        is_grp, tid, _ = _parse_session_key(sk, set())
        if is_grp:
            return group_map.get(tid) or f"QQ 群 {tid}", True
        if sk.startswith("qq:"):
            uname = (user_meta.get(sk) or {}).get("user_name") or mem_names.get(tid) or f"QQ {tid}"
            return uname, False
        return sk or "未知会话", False

    # Prepare last 24 local hourly buckets
    hour_keys = []
    hourly_buckets = {}
    for offset in range(23, -1, -1):
        hdt = now_local - datetime.timedelta(hours=offset)
        hkey = hdt.strftime("%Y-%m-%dT%H:00")
        hour_keys.append(hkey)
        hourly_buckets[hkey] = {
            "hour": hdt.strftime("%H:00"),
            "label": hdt.strftime("%H:00"),
            "hour_iso": hkey,
            "total": 0,
            "replied": 0,
            "silent": 0,
            "dur_sum": 0.0,
            "dur_cnt": 0,
        }

    msg_meta = {}  # msg_id -> {"session_key": ..., "user": ...}
    turns = []
    durations = []
    chat_stats = {}  # session_key -> {"total", "replied", "silent"}

    # Scan up to last 4 MB of cc-connect.log
    if os.path.isfile(CC_LOG_PATH):
        try:
            size = os.path.getsize(CC_LOG_PATH)
            start = max(0, size - 4 * 1024 * 1024)
            with open(CC_LOG_PATH, "rb") as f:
                f.seek(start)
                if start > 0:
                    f.readline()
                for raw_bytes in f:
                    line = raw_bytes.decode("utf-8", errors="replace")
                    if 'msg="message received"' in line:
                        pairs = dict(_LOGFMT_TOKEN_RE.findall(line))
                        mid = _unescape_logfmt_val(pairs.get("msg_id", ""))
                        if mid:
                            msg_meta[str(mid)] = {
                                "session_key": str(_unescape_logfmt_val(pairs.get("session", ""))),
                                "user": str(_unescape_logfmt_val(pairs.get("user", ""))),
                            }
                    elif 'msg="turn complete"' in line:
                        pairs = dict(_LOGFMT_TOKEN_RE.findall(line))
                        ts_s = str(_unescape_logfmt_val(pairs.get("time", "")))
                        sid = str(_unescape_logfmt_val(pairs.get("session", "")))
                        mid = str(_unescape_logfmt_val(pairs.get("msg_id", "")))
                        dur = _parse_go_duration(_unescape_logfmt_val(pairs.get("turn_duration", "")))
                        resp_len = 0
                        try:
                            resp_len = int(_unescape_logfmt_val(pairs.get("response_len", "0")))
                        except ValueError:
                            pass
                        silent = str(_unescape_logfmt_val(pairs.get("silent", "false"))).lower() == "true"

                        meta = msg_meta.get(mid) or {}
                        skey = meta.get("session_key") or sid_to_key.get(sid) or sid
                        user = meta.get("user") or ""

                        if dur is not None and dur >= 0:
                            durations.append(dur)

                        dt = _parse_iso_dt(ts_s)
                        if dt:
                            dt_local = dt.astimezone()
                            hkey = dt_local.strftime("%Y-%m-%dT%H:00")
                            if hkey in hourly_buckets:
                                hb = hourly_buckets[hkey]
                                hb["total"] += 1
                                if silent:
                                    hb["silent"] += 1
                                else:
                                    hb["replied"] += 1
                                if dur is not None and dur >= 0:
                                    hb["dur_sum"] += dur
                                    hb["dur_cnt"] += 1
                            time_short = dt_local.strftime("%H:%M:%S")
                        else:
                            time_short = ts_s

                        cs = chat_stats.setdefault(skey, {"total": 0, "replied": 0, "silent": 0, "dur_sum": 0.0, "dur_cnt": 0})
                        cs["total"] += 1
                        if silent:
                            cs["silent"] += 1
                        else:
                            cs["replied"] += 1
                        if dur is not None and dur >= 0:
                            cs["dur_sum"] += dur
                            cs["dur_cnt"] += 1

                        turns.append({
                            "time": ts_s,
                            "time_short": time_short,
                            "session_id": sid,
                            "session": skey,
                            "user": user,
                            "duration_sec": round(dur, 2) if dur is not None else 0.0,
                            "response_len": resp_len,
                            "silent": silent,
                        })
        except OSError:
            pass

    # Count repeat joins & forward cards from last 2 MB of ws-guard.log
    repeat_joins = 0
    repeat_offers = 0
    forward_cards = 0
    if os.path.isfile(WS_GUARD_LOG_PATH):
        try:
            ws_size = os.path.getsize(WS_GUARD_LOG_PATH)
            ws_start = max(0, ws_size - _WS_ANALYTICS_MAX_BYTES)
            with open(WS_GUARD_LOG_PATH, "rb") as f:
                f.seek(ws_start)
                if ws_start > 0:
                    f.readline()
                for raw_bytes in f:
                    line = raw_bytes.decode("utf-8", errors="replace")
                    if "Joining repeat chain" in line or "Following repeat chain" in line:
                        repeat_joins += 1
                    elif "asking the model whether to join" in line:
                        repeat_offers += 1
                    elif "send_group_forward_msg" in line or "send_private_forward_msg" in line:
                        forward_cards += 1
        except OSError:
            pass

    total_turns = len(turns)
    silent_turns = sum(1 for t in turns if t["silent"])
    replied_turns = total_turns - silent_turns
    reply_rate = round((replied_turns / total_turns) * 100.0, 1) if total_turns > 0 else 0.0

    durations_sorted = sorted(durations)
    avg_dur = round(sum(durations_sorted) / len(durations_sorted), 2) if durations_sorted else 0.0
    p50_dur = round(_percentile(durations_sorted, 50), 2)
    p95_dur = round(_percentile(durations_sorted, 95), 2)

    hourly = []
    for hkey in hour_keys:
        hb = hourly_buckets[hkey]
        hourly.append({
            "hour": hb["hour"],
            "label": hb["label"],
            "hour_iso": hb["hour_iso"],
            "total": hb["total"],
            "replied": hb["replied"],
            "silent": hb["silent"],
            "avg_sec": round(hb["dur_sum"] / hb["dur_cnt"], 2) if hb["dur_cnt"] > 0 else 0.0,
        })

    top_chats = []
    for skey, cs in chat_stats.items():
        name, is_grp = _chat_display(skey)
        tot = cs["total"]
        rep = cs["replied"]
        top_chats.append({
            "session_key": skey,
            "name": name,
            "is_group": is_grp,
            "total": tot,
            "replied": rep,
            "silent": cs["silent"],
            "reply_rate": round((rep / tot) * 100.0, 1) if tot > 0 else 0.0,
            "avg_duration_s": round(cs["dur_sum"] / cs["dur_cnt"], 2) if cs["dur_cnt"] > 0 else None,
        })
    top_chats.sort(key=lambda c: (c["total"], c["replied"]), reverse=True)

    recent_turns = []
    for t in reversed(turns[-25:]):
        name, is_grp = _chat_display(t["session"])
        item = dict(t)
        item["name"] = name
        item["is_group"] = is_grp
        recent_turns.append(item)

    result = {
        "summary": {
            "total_turns": total_turns,
            "replied_turns": replied_turns,
            "silent_turns": silent_turns,
            "reply_rate": reply_rate,
            "avg_duration_sec": avg_dur,
            "p50_duration_sec": p50_dur,
            "p95_duration_sec": p95_dur,
            "repeat_joins": repeat_joins,
            "repeat_turns": repeat_joins,
            "repeat_offers": repeat_offers,
            "forward_cards": forward_cards,
        },
        "hourly": hourly,
        "top_chats": top_chats[:12],
        "recent_turns": recent_turns,
    }
    with _analytics_cache_lock:
        _analytics_cache["ts"] = time.monotonic()
        _analytics_cache["key"] = cache_key
        _analytics_cache["data"] = copy.deepcopy(result)
    return result


# ---------------------------------------------------------------------------
# Token Usage Analytics (SQLite Ledger + Incremental cc-connect.log Backfill)
# ---------------------------------------------------------------------------

def get_token_usage(range_key="24h", chat_filter="all", query="", sort_by="total_tokens"):
    _, user_meta = _build_sid_to_key_map()
    group_map = napcat_get_group_list().get("group_map", {})
    mem_names = stores._memory_user_map()
    return token_usage.get_usage_dashboard(
        range_key=range_key,
        chat_filter=chat_filter,
        query=query,
        sort_by=sort_by,
        group_map=group_map,
        user_meta=user_meta,
        mem_names=mem_names,
        cc_log_path=CC_LOG_PATH,
        sessions_dir=SESSIONS_DIR,
    )


def get_token_chat_detail(session_key, range_key="24h", limit=50):
    skey = str(session_key or "").strip()
    if not skey or "/" in skey or ".." in skey:
        raise ServiceError("无效的会话标识")
    _, user_meta = _build_sid_to_key_map()
    group_map = napcat_get_group_list().get("group_map", {})
    mem_names = stores._memory_user_map()
    return token_usage.get_chat_detail(
        skey,
        range_key=range_key,
        limit=limit,
        group_map=group_map,
        user_meta=user_meta,
        mem_names=mem_names,
        cc_log_path=CC_LOG_PATH,
        sessions_dir=SESSIONS_DIR,
    )


def sync_token_usage_logs(force=False):
    return token_usage.sync_from_cc_log(
        cc_log_path=CC_LOG_PATH,
        sessions_dir=SESSIONS_DIR,
        force=bool(force),
    )


# ---------------------------------------------------------------------------
# Group Member Join Requests (Monitoring, Approval & Auto-Approval Policy)
# ---------------------------------------------------------------------------

def _get_group_requests_mod():
    global group_requests
    if group_requests is None:
        try:
            import group_requests as _gr
            group_requests = _gr
        except ImportError as exc:
            raise ServiceError(f"入群申请模块不可用：{exc}", status_code=503)
    return group_requests


def _napcat_call_for_requests(endpoint, payload=None, timeout=5.0):
    """Bridge OneBot v11 POST calls for group_requests -> (ok, data, err)."""
    base, token, _ = _napcat_cfg()
    return onebot_common.napcat_api_post(
        base,
        token,
        endpoint,
        payload if isinstance(payload, dict) else {},
        timeout=float(timeout or 5.0),
    )


def _build_group_requests_context(force_groups=False):
    """Fetch NapCat online state, group list, bot group roles, and ws-guard status (fail-soft)."""
    napcat_online = False
    groups_list = []
    group_map = {}
    role_map = {}
    ws_guard_running = False

    try:
        glist = napcat_get_group_list(force=force_groups)
        napcat_online = bool(glist.get("ok"))
        groups_list = list(glist.get("groups") or [])
        group_map = dict(glist.get("group_map") or {})
    except Exception:
        pass

    if napcat_online and groups_list:
        try:
            gids = [str(g.get("group_id") or "").strip() for g in groups_list if g.get("group_id")]
            role_map = napcat_get_bot_group_roles(gids, force=force_groups) or {}
        except Exception:
            role_map = {}

    try:
        procs = inspect_processes()
        ws_guard_running = bool(procs.get("ws_guard_worker") is not None)
    except Exception:
        ws_guard_running = False

    return {
        "napcat_online": napcat_online,
        "groups_list": groups_list,
        "group_map": group_map,
        "role_map": role_map,
        "ws_guard_running": ws_guard_running,
    }


def _enrich_group_request_item(item, group_map=None, role_map=None):
    if not isinstance(item, dict):
        return item
    enriched = dict(item)
    gid = str(enriched.get("group_id") or "").strip()
    gmap = group_map or {}
    rmap = role_map or {}
    if not str(enriched.get("group_name") or "").strip() and gid and gid in gmap:
        enriched["group_name"] = gmap[gid]
    enriched["bot_role"] = rmap.get(gid, "")
    return enriched


def _build_group_requests_groups(gr, policy, ctx, extra_items=None):
    """Build merged group directory with bot_role, pending_count, and effective_policy."""
    groups_list = ctx.get("groups_list") or []
    group_map = ctx.get("group_map") or {}
    role_map = ctx.get("role_map") or {}

    pending_counts = {}
    pending_gids = set()
    extra_names = {}
    try:
        p_res = gr.list_requests(status="pending", limit=1000, offset=0)
        for it in (p_res.get("items") or []):
            if not isinstance(it, dict):
                continue
            gid = str(it.get("group_id") or "").strip()
            if not gid:
                continue
            pending_counts[gid] = pending_counts.get(gid, 0) + 1
            pending_gids.add(gid)
            gn = str(it.get("group_name") or "").strip()
            if gn and gid not in extra_names:
                extra_names[gid] = gn
    except Exception:
        pass

    napcat_by_id = {}
    ordered_ids = []
    seen = set()

    for g in groups_list:
        gid = str(g.get("group_id") or "").strip()
        if not gid or gid in seen:
            continue
        seen.add(gid)
        ordered_ids.append(gid)
        napcat_by_id[gid] = g

    for it in (extra_items or []):
        if not isinstance(it, dict):
            continue
        gid = str(it.get("group_id") or "").strip()
        if gid:
            gn = str(it.get("group_name") or "").strip()
            if gn and gid not in extra_names:
                extra_names[gid] = gn
            if gid not in seen:
                seen.add(gid)
                ordered_ids.append(gid)

    for gid in sorted(pending_gids):
        if gid not in seen:
            seen.add(gid)
            ordered_ids.append(gid)

    pol_groups = (
        policy.get("groups")
        if isinstance(policy, dict) and isinstance(policy.get("groups"), dict)
        else {}
    )
    for raw_gid, gcfg in pol_groups.items():
        gid = str(raw_gid or "").strip()
        if gid and gid not in seen:
            seen.add(gid)
            ordered_ids.append(gid)
        if gid and isinstance(gcfg, dict):
            lbl = str(gcfg.get("label") or gcfg.get("group_name") or "").strip()
            if lbl and gid not in extra_names:
                extra_names[gid] = lbl

    groups_out = []
    for gid in ordered_ids:
        ng = napcat_by_id.get(gid)
        gname = (
            (str(ng.get("group_name") or "").strip() if ng else "")
            or group_map.get(gid, "")
            or extra_names.get(gid, "")
            or f"群 {gid}"
        )
        try:
            eff_pol = gr.resolve_group_policy(gid, policy=policy)
        except Exception:
            eff_pol = {}
        groups_out.append({
            "group_id": gid,
            "group_name": gname,
            "member_count": int(ng.get("member_count") or 0) if ng else 0,
            "bot_role": role_map.get(gid, ""),
            "pending_count": int(pending_counts.get(gid, 0)),
            "effective_policy": eff_pol,
        })

    role_rank = {"owner": 2, "admin": 1}
    groups_out.sort(
        key=lambda x: (
            -x["pending_count"],
            -role_rank.get(x["bot_role"], 0),
            -x["member_count"],
            x["group_id"],
        )
    )
    return groups_out


def list_group_requests(
    status="all",
    group_id="",
    sub_type="",
    query="",
    limit=100,
    offset=0,
    sync=False,
):
    gr = _get_group_requests_mod()
    ctx = _build_group_requests_context()
    if sync:
        try:
            gr.sync_from_napcat(
                napcat_call=_napcat_call_for_requests,
                group_map=ctx["group_map"],
                count=50,
            )
        except Exception:
            pass

    lim = min(max(int(limit or 100), 1), 500)
    off = max(int(offset or 0), 0)
    res = gr.list_requests(
        status=str(status or "all"),
        group_id=str(group_id or ""),
        sub_type=str(sub_type or ""),
        query=str(query or ""),
        limit=lim,
        offset=off,
    )
    policy = gr.load_policy(force=True)
    items = [
        _enrich_group_request_item(it, ctx["group_map"], ctx["role_map"])
        for it in (res.get("items") or [])
    ]
    groups = _build_group_requests_groups(gr, policy, ctx, extra_items=items)

    return {
        "items": items,
        "counts": res.get("counts") or {
            "pending": 0,
            "approved": 0,
            "rejected": 0,
            "failed": 0,
            "total": 0,
        },
        "policy": policy,
        "groups": groups,
        "napcat_online": ctx["napcat_online"],
        "ws_guard_running": ctx["ws_guard_running"],
        "latest_seq": int(res.get("latest_seq") or 0),
    }


def sync_group_requests(count=50):
    gr = _get_group_requests_mod()
    try:
        cnt = min(max(int(count or 50), 1), 200)
    except (TypeError, ValueError):
        cnt = 50
    ctx = _build_group_requests_context(force_groups=True)
    raw_sync = gr.sync_from_napcat(
        napcat_call=_napcat_call_for_requests,
        group_map=ctx["group_map"],
        count=cnt,
    )
    sync_out = dict(raw_sync) if isinstance(raw_sync, dict) else {"ok": False, "error": "同步失败"}
    # Keep canonical group_requests keys and provide friendly aliases for frontend toasts
    sync_out.setdefault("inserted", int(sync_out.get("synced_new") or 0))
    sync_out.setdefault("fetched", int(sync_out.get("total_fetched") or 0))

    res = gr.list_requests(status="all", limit=100, offset=0)
    items = [
        _enrich_group_request_item(it, ctx["group_map"], ctx["role_map"])
        for it in (res.get("items") or [])
    ]
    return {
        "sync": sync_out,
        "counts": res.get("counts") or {
            "pending": 0,
            "approved": 0,
            "rejected": 0,
            "failed": 0,
            "total": 0,
        },
        "items": items,
        "latest_seq": int(res.get("latest_seq") or 0),
    }


def _execute_single_group_request(req_id, approve, reason="", send_welcome=True):
    gr = _get_group_requests_mod()
    rid = str(req_id or "").strip()
    if not rid:
        raise stores.StoreError("缺少入群申请 ID", status_code=400)
    try:
        res = gr.execute_decision(
            rid,
            approve=bool(approve),
            reason=str(reason or "").strip(),
            decision_source="webui",
            operator_id="webui",
            napcat_call=_napcat_call_for_requests,
            send_welcome=bool(send_welcome) if approve else False,
        )
    except ValueError as exc:
        raise stores.StoreError(str(exc), status_code=404)

    ctx = _build_group_requests_context()
    req_obj = res.get("request")
    if req_obj is None:
        req_obj = gr.get_request(rid)
    enriched_req = _enrich_group_request_item(req_obj, ctx["group_map"], ctx["role_map"])
    counts = (gr.list_requests(status="all", limit=1, offset=0) or {}).get("counts") or {
        "pending": 0,
        "approved": 0,
        "rejected": 0,
        "failed": 0,
        "total": 0,
    }

    if not res.get("ok"):
        already = bool(res.get("already_handled"))
        err_msg = str(
            res.get("error")
            or ("该入群申请已被处理" if already else ("审批执行失败" if approve else "拒绝执行失败"))
        )
        raise stores.StoreError(
            err_msg,
            status_code=409 if already else 502,
            extra={
                "already_handled": already,
                "request": enriched_req,
                "counts": counts,
            },
        )

    return {
        "ok": True,
        "already_handled": bool(res.get("already_handled")),
        "request": enriched_req,
        "error": str(res.get("error") or ""),
        "welcome_sent": bool(res.get("welcome_sent")),
        "counts": counts,
    }


def approve_group_request(req_id, send_welcome=True):
    return _execute_single_group_request(req_id, approve=True, reason="", send_welcome=send_welcome)


def reject_group_request(req_id, reason=""):
    return _execute_single_group_request(req_id, approve=False, reason=reason, send_welcome=False)


def batch_group_requests(ids, action="approve", reason=""):
    gr = _get_group_requests_mod()
    act = str(action or "approve").strip().lower()
    if act not in ("approve", "reject"):
        raise stores.StoreError("批量操作 action 必须为 approve 或 reject", status_code=400)
    if not isinstance(ids, list) or not ids:
        raise stores.StoreError("ids 必须是非空列表", status_code=400)

    clean_ids = []
    seen = set()
    for raw in ids:
        rid = str(raw if raw is not None else "").strip()
        if rid and rid not in seen:
            seen.add(rid)
            clean_ids.append(rid)

    if not clean_ids:
        raise stores.StoreError("ids 必须包含至少一个有效的申请 ID", status_code=400)
    if len(clean_ids) > 50:
        raise stores.StoreError("单次批量处理最多支持 50 条申请", status_code=400)

    approve = act == "approve"
    reason_str = str(reason or "").strip()
    ctx = _build_group_requests_context()

    results = []
    succeeded = 0
    failed = 0
    for rid in clean_ids:
        try:
            res = gr.execute_decision(
                rid,
                approve=approve,
                reason=reason_str,
                decision_source="webui",
                operator_id="webui",
                napcat_call=_napcat_call_for_requests,
                send_welcome=approve,
            )
            ok = bool(res.get("ok"))
            if ok:
                succeeded += 1
            else:
                failed += 1
            req_obj = res.get("request")
            if req_obj is None:
                req_obj = gr.get_request(rid)
            results.append({
                "id": rid,
                "ok": ok,
                "already_handled": bool(res.get("already_handled")),
                "error": str(res.get("error") or ""),
                "welcome_sent": bool(res.get("welcome_sent")),
                "request": _enrich_group_request_item(req_obj, ctx["group_map"], ctx["role_map"]),
            })
        except Exception as exc:
            failed += 1
            results.append({
                "id": rid,
                "ok": False,
                "already_handled": False,
                "error": str(exc),
                "welcome_sent": False,
                "request": None,
            })

    counts = (gr.list_requests(status="all", limit=1, offset=0) or {}).get("counts") or {
        "pending": 0,
        "approved": 0,
        "rejected": 0,
        "failed": 0,
        "total": 0,
    }
    return {
        "processed": len(clean_ids),
        "succeeded": succeeded,
        "failed": failed,
        "results": results,
        "counts": counts,
    }


def get_group_requests_policy():
    gr = _get_group_requests_mod()
    policy = gr.load_policy(force=True)
    ctx = _build_group_requests_context()
    extra_items = []
    try:
        extra_items = (gr.list_requests(status="all", limit=500, offset=0) or {}).get("items") or []
    except Exception:
        pass
    groups = _build_group_requests_groups(gr, policy, ctx, extra_items=extra_items)
    return {
        "policy": policy,
        "groups": groups,
    }


def update_group_requests_policy(payload):
    gr = _get_group_requests_mod()
    if not isinstance(payload, dict):
        raise stores.StoreError("策略更新请求体必须是 JSON 对象", status_code=400)

    cur = gr.load_policy(force=True)
    merged = copy.deepcopy(cur)

    if "enabled" in payload:
        merged["enabled"] = bool(payload["enabled"])
    if "global" in payload:
        if not isinstance(payload["global"], dict):
            raise stores.StoreError("global 必须是对象", status_code=400)
        new_glob = dict(merged.get("global") or {})
        new_glob.update(payload["global"])
        merged["global"] = new_glob
    if "groups" in payload:
        if not isinstance(payload["groups"], dict):
            raise stores.StoreError("groups 必须是对象", status_code=400)
        merged["groups"] = {
            str(k).strip(): v
            for k, v in payload["groups"].items()
            if str(k or "").strip() and v is not None
        }

    try:
        saved = gr.save_policy(merged)
    except ValueError as exc:
        raise stores.StoreError(str(exc), status_code=400)

    ctx = _build_group_requests_context()
    extra_items = []
    try:
        extra_items = (gr.list_requests(status="all", limit=500, offset=0) or {}).get("items") or []
    except Exception:
        pass
    groups = _build_group_requests_groups(gr, saved, ctx, extra_items=extra_items)
    return {
        "policy": saved,
        "groups": groups,
    }


def test_group_requests_policy(payload):
    gr = _get_group_requests_mod()
    if not isinstance(payload, dict):
        raise stores.StoreError("策略试算请求体必须是 JSON 对象", status_code=400)

    draft_policy = payload.get("policy")
    if draft_policy is not None and not isinstance(draft_policy, dict):
        raise stores.StoreError("policy 必须是对象", status_code=400)

    if isinstance(draft_policy, dict):
        cur = gr.load_policy(force=True)
        policy = copy.deepcopy(cur)
        if "enabled" in draft_policy:
            policy["enabled"] = bool(draft_policy["enabled"])
        if isinstance(draft_policy.get("global"), dict):
            policy.setdefault("global", {}).update(draft_policy["global"])
        if isinstance(draft_policy.get("groups"), dict):
            policy["groups"] = draft_policy["groups"]
    else:
        policy = gr.load_policy(force=True)

    if isinstance(payload.get("request"), dict):
        req = dict(payload["request"])
    else:
        req = {k: v for k, v in payload.items() if k != "policy"}
    req.setdefault("sub_type", "add")
    req.setdefault("group_id", "")
    req.setdefault("user_id", "")
    req.setdefault("comment", "")

    try:
        return gr.evaluate_request_policy(req, policy=policy)
    except ValueError as exc:
        raise stores.StoreError(str(exc), status_code=400)


def clear_group_requests(before_days=0):
    gr = _get_group_requests_mod()
    try:
        days = int(before_days or 0)
    except (TypeError, ValueError):
        raise stores.StoreError("before_days 必须是整数", status_code=400)
    if days < 0:
        raise stores.StoreError("before_days 不能为负数", status_code=400)
    deleted = int(gr.clear_handled_requests(before_days=days) or 0)
    counts = (gr.list_requests(status="all", limit=1, offset=0) or {}).get("counts") or {
        "pending": 0,
        "approved": 0,
        "rejected": 0,
        "failed": 0,
        "total": 0,
    }
    return {
        "deleted": deleted,
        "counts": counts,
    }


# ---------------------------------------------------------------------------
# Overview Aggregator
# ---------------------------------------------------------------------------

def get_overview(force=False):
    now_mono = time.monotonic()
    with _overview_cache_lock:
        if (
            not force
            and _overview_cache["data"] is not None
            and (now_mono - _overview_cache["ts"]) < _OVERVIEW_CACHE_TTL
        ):
            return copy.deepcopy(_overview_cache["data"])

    env = stores.parse_bot_env()
    _, _, _, mgmt_port = _cc_cfg()
    webui_meta = stores.get_napcat_webui_meta()

    procs = inspect_processes()
    login_info = napcat_get_login_info()
    nc_status = napcat_get_status()
    nc_groups = napcat_get_group_list()
    docker_info = get_napcat_docker_status()

    cc_ok, cc_status_data = cc_get_status()
    sess_data = list_sessions()
    consistency = stores.check_config_consistency()
    plugins_state = stores.get_plugins_state()
    persona_sync = stores.get_persona_sync_status(plugin_config=plugins_state.get("config"))
    mem_data = stores.get_memory_summary_stats()
    mem_stats = mem_data["stats"]

    ws_sup = procs["ws_guard_supervisor"]
    ws_worker = procs["ws_guard_worker"]
    cc_proc = procs["cc_connect"]
    wrappers = procs["acp_wrappers"]

    ws_tail = tail_logs(source="ws-guard", lines=5, max_bytes=16384).get("entries") or []
    ws_last_event = ws_tail[-1]["msg"] if ws_tail else ""

    http_url = (env.get("NAPCAT_HTTP_URL") or "http://127.0.0.1:3000").strip()
    ws_url = (env.get("NAPCAT_WS_URL") or "ws://127.0.0.1:3001").strip()
    http_port = _extract_url_port(http_url, 3000)
    ws_port = _extract_url_port(ws_url, 3001)

    napcat_ok = bool(login_info["ok"] and nc_status["ok"] and nc_status.get("online"))
    onebot_reachable = bool(login_info["ok"] or nc_status["ok"])
    native_webui_reachable = False
    if not napcat_ok and not docker_info["running"] and not docker_info["available"] and not webui_meta.get("disabled"):
        native_webui_reachable = _probe_http_reachable(webui_meta["url"], timeout=0.6)

    if docker_info["available"]:
        napcat_mode = "docker"
    elif napcat_ok or onebot_reachable or native_webui_reachable:
        napcat_mode = "native"
    else:
        napcat_mode = "unconfigured"

    napcat_running = napcat_ok or docker_info["running"] or native_webui_reachable
    nc_vers = napcat_api.get_versions() if napcat_running else {"napcat": "", "qq": "", "platform": ""}
    nc_friends = napcat_get_friend_list() if napcat_ok else []
    napcat_service = {
        "ok": napcat_ok,
        "running": napcat_running,
        "mode": napcat_mode,
        "online": bool(nc_status.get("online")),
        "good": bool(nc_status.get("good")),
        "status": "online" if napcat_ok else ("degraded" if napcat_running else "offline"),
        "docker_status": docker_info["status"],
        "container_status": "native" if napcat_mode == "native" else docker_info["status"],
        "docker_started_at": docker_info["started_at"],
        "uptime_seconds": docker_info["uptime_seconds"],
        "napcat_version": nc_vers.get("napcat", ""),
        "qq_version": nc_vers.get("qq", ""),
        "user_id": login_info.get("user_id") or env.get("BOT_QQ_ID", ""),
        "qq": login_info.get("user_id") or env.get("BOT_QQ_ID", ""),
        "nickname": login_info.get("nickname") or (stores._split_csv(env.get("BOT_CALL_NAMES", "哈基米"))[0:1] or ["哈基米"])[0],
        "group_count": len(nc_groups.get("groups") or []),
        "groups_count": len(nc_groups.get("groups") or []),
        "friend_count": len(nc_friends),
        "friends_count": len(nc_friends),
        "http_url": http_url,
        "ws_url": ws_url,
        "http_port": http_port,
        "ws_port": ws_port,
        "webui_url": webui_meta["url"],
        "webui_port": webui_meta["port"],
        "error": login_info.get("error") or nc_status.get("error") or "",
    }

    ws_ok = bool(ws_worker is not None)
    ws_service = {
        "ok": ws_ok,
        "running": ws_ok,
        "status": "running" if ws_ok else "stopped",
        "pid": ws_worker["pid"] if ws_worker else None,
        "worker_pid": ws_worker["pid"] if ws_worker else None,
        "worker_alive": ws_ok,
        "supervisor_pid": ws_sup["pid"] if ws_sup else None,
        "supervisor_alive": bool(ws_sup is not None),
        "rss_mb": ws_worker["rss_mb"] if ws_worker else 0.0,
        "started": ws_worker["started"] if ws_worker else "",
        "uptime_seconds": ws_worker["uptime_seconds"] if ws_worker else None,
        "listen_port": int(env.get("GUARD_LISTEN_PORT", "3002") or 3002),
        "udp_port": int(env.get("GUARD_UDP_PORT", "3003") or 3003),
        "last_event": ws_last_event,
    }

    cc_running = bool(cc_ok or cc_proc is not None)
    cc_version = ""
    cc_uptime = cc_proc["uptime_seconds"] if cc_proc else None
    cc_platforms = []
    cc_projects_count = 0
    if cc_ok and isinstance(cc_status_data, dict):
        cc_version = str(cc_status_data.get("version") or "")
        if cc_status_data.get("uptime_seconds") is not None:
            cc_uptime = cc_status_data.get("uptime_seconds")
        cc_platforms = cc_status_data.get("connected_platforms") or []
        cc_projects_count = int(cc_status_data.get("projects_count") or 0)

    cc_service = {
        "ok": bool(cc_ok),
        "running": cc_running,
        "status": "running" if cc_ok else ("degraded" if cc_running else "stopped"),
        "pid": cc_proc["pid"] if cc_proc else None,
        "rss_mb": cc_proc["rss_mb"] if cc_proc else 0.0,
        "started": cc_proc["started"] if cc_proc else "",
        "version": cc_version,
        "uptime_seconds": cc_uptime,
        "connected_platforms": cc_platforms,
        "projects_count": cc_projects_count,
        "sessions_count": sess_data["total"],
        "active_sessions": sess_data["live_count"],
        "live_sessions": sess_data["live_count"],
        "acp_processes": len(wrappers),
        "management_port": mgmt_port,
        "start_loop_pid": procs["start_loop_pid"],
        "error": "" if cc_ok else str(cc_status_data),
    }

    acp_service = {
        "ok": len(wrappers) > 0 or cc_ok,
        "running": len(wrappers) > 0,
        "count": len(wrappers),
        "process_count": len(wrappers),
        "active_count": len(wrappers),
        "rss_mb": procs["acp_total_rss_mb"],
        "total_rss_mb": procs["acp_total_rss_mb"],
        "workers": wrappers,
        "processes": wrappers,
    }

    up_count = sum(1 for x in (napcat_ok, ws_ok, cc_ok) if x)
    overall = "healthy" if up_count == 3 and consistency["valid"] else ("stopped" if up_count == 0 else "degraded")

    group_sess_cnt = sum(1 for s in sess_data["sessions"] if s["is_group"])
    dm_sess_cnt = sess_data["total"] - group_sess_cnt

    services_obj = {
        "napcat": napcat_service,
        "ws_guard": ws_service,
        "cc_connect": cc_service,
        "acp": acp_service,
        "acp_wrappers": acp_service,
    }

    call_names = stores._split_csv(env.get("BOT_CALL_NAMES", "哈基米"))
    admin_ids = stores._split_csv(env.get("ADMIN_QQ_IDS", ""))

    runtime = {
        "thought_level": env.get("AGY_THOUGHT_LEVEL", "medium"),
        "lurk_thought_level": env.get("AGY_LURK_THOUGHT_LEVEL", "low"),
        "task_thought_level": env.get("AGY_TASK_THOUGHT_LEVEL", "xhigh"),
        "admin_dm_turbo": stores._bool_flag(env.get("ADMIN_DM_TURBO", "1"), True),
        "repeat_enabled": stores._bool_flag(env.get("REPEAT_ENABLED", "1"), True),
        "forward_group": stores._bool_flag(env.get("FORWARD_LONG_MSG_GROUP", "1"), True),
        "forward_dm": stores._bool_flag(env.get("FORWARD_LONG_MSG_DM", "0"), False),
        "memory_enabled": stores._bool_flag(env.get("USER_MEMORY_ENABLED", "1"), True),
        "memory_model": env.get("MEMORY_MODEL", "gemini-4-neon"),
        "ambient_min_gap_secs": int(float(env.get("AMBIENT_MIN_GAP_SECS", "45") or 45)),
        "ambient_max_per_window": int(float(env.get("AMBIENT_MAX_PER_WINDOW", "4") or 4)),
        "ambient_listen_enabled": stores._bool_flag(env.get("GUARD_AMBIENT_LISTEN", "1"), True),
        "guard_ambient_gap_secs": float(env.get("GUARD_AMBIENT_GAP_SECS", "20") or 20),
    }

    p_sum = plugins_state.get("summary") or {}
    plugins_summary = {
        "enabled_count": int(p_sum.get("enabled_count") or 0),
        "disabled_count": int(p_sum.get("disabled_count") or 0),
        "total_count": int(p_sum.get("total_count") or 0),
        "active_preset": str(plugins_state.get("active_preset") or "custom"),
        "per_turn_active_tokens": int(p_sum.get("per_turn_active_tokens") or 0),
        "per_turn_max_tokens": int(p_sum.get("per_turn_max_tokens") or 0),
        "per_turn_saved_tokens": int(p_sum.get("per_turn_saved_tokens") or 0),
        "per_turn_save_pct": int(p_sum.get("per_turn_save_pct") or 0),
        "sandbox_gemini_saved_tokens": int(p_sum.get("sandbox_gemini_saved_tokens") or 0),
        "background_llm_active_count": int(p_sum.get("background_llm_active_count") or 0),
        "extra_turns_active_count": int(p_sum.get("extra_turns_active_count") or 0),
    }

    sandbox_in_sync = bool(persona_sync.get("in_sync"))
    sb_saved_tokens = int(persona_sync.get("saved_tokens") or 0)
    sandbox_clean = bool(persona_sync.get("sandbox_clean"))
    sandbox_extra_files = list(persona_sync.get("sandbox_extra_files") or [])
    if sandbox_in_sync:
        sync_detail = (
            f"chat-sandbox/GEMINI.md 已按当前插件配置同步（已精简约 {sb_saved_tokens} tokens）"
            if sb_saved_tokens > 0
            else "workspace/GEMINI.md 与 chat-sandbox/GEMINI.md 内容一致"
        )
    else:
        sync_detail = "chat-sandbox/GEMINI.md 与当前人设或插件编译结果不一致，建议一键同步"

    config_health = {
        "ok": consistency["valid"] and sandbox_in_sync and sandbox_clean,
        "valid": consistency["valid"],
        "problems": consistency["problems"],
        "checks": consistency["checks"] + [
            {
                "name": "chat-sandbox 人设同步",
                "ok": sandbox_in_sync,
                "detail": sync_detail,
            },
            {
                "name": "chat-sandbox 目录洁净",
                "ok": sandbox_clean,
                "detail": "仅含 GEMINI.md"
                if sandbox_clean
                else f"发现多余文件：{', '.join(sandbox_extra_files)}",
            },
        ],
        "persona_in_sync": sandbox_in_sync,
        "sandbox_clean": sandbox_clean,
        "sandbox_extra_files": sandbox_extra_files,
    }

    memory_summary = {
        "total_users": mem_stats["total_users"],
        "with_public": mem_stats["with_public"],
        "public_users": mem_stats["with_public"],
        "with_dm": mem_stats["with_dm"],
        "dm_users": mem_stats["with_dm"],
        "total_facts": mem_stats["total_facts"],
        "total_jokes": mem_stats["total_jokes"],
        "active_memories": mem_stats.get("active_memories", mem_stats["total_facts"] + mem_stats["total_jokes"]),
        "group_memories_count": mem_stats.get("group_memories_count", 0),
        "groups_count": mem_stats.get("groups_count", 0),
        "conflicts_count": mem_stats.get("conflicts_count", 0),
        "conflict_count": mem_stats.get("conflicts_count", 0),
        "pending_queue": mem_stats["pending_queue"],
        "memory_enabled": runtime["memory_enabled"],
        "plugin_flags": mem_data.get("plugin_flags") or {},
    }

    sessions_summary = {
        "total_count": sess_data["total"],
        "live_count": sess_data["live_count"],
        "active_count": sess_data["active_count"],
        "group_count": group_sess_cnt,
        "dm_count": dm_sess_cnt,
        "source": sess_data["source"],
    }

    _, ov_user_meta = _build_sid_to_key_map()
    ov_mem_names = stores._memory_user_map()
    token_summary = token_usage.get_overview_summary(
        group_map=nc_groups.get("group_map"),
        user_meta=ov_user_meta,
        mem_names=ov_mem_names,
        cc_log_path=CC_LOG_PATH,
        sessions_dir=SESSIONS_DIR,
    )

    group_requests_summary = {}
    try:
        gr_mod = _get_group_requests_mod()
        group_requests_summary = gr_mod.get_summary() or {}
    except Exception:
        group_requests_summary = {}

    result = {
        "timestamp": _now_iso(),
        "overall": overall,
        "bot": {
            "qq": napcat_service["qq"],
            "qq_id": napcat_service["qq"],
            "nickname": napcat_service["nickname"],
            "call_names": call_names,
            "admin_qq_ids": admin_ids,
            "admin_dm_turbo": runtime["admin_dm_turbo"],
            "repeat_enabled": runtime["repeat_enabled"],
            "memory_enabled": runtime["memory_enabled"],
            "thought_levels": {
                "chat": runtime["thought_level"],
                "lurk": runtime["lurk_thought_level"],
                "task": runtime["task_thought_level"],
            },
        },
        "services": services_obj,
        "components": services_obj,
        "sessions_summary": sessions_summary,
        "memory_summary": memory_summary,
        "plugins_summary": plugins_summary,
        "token_summary": token_summary,
        "group_requests_summary": group_requests_summary,
        "config_health": config_health,
        "runtime": runtime,
        "ports": {
            "napcat_http": http_port,
            "napcat_ws": ws_port,
            "napcat_webui": webui_meta["port"],
            "ws_guard": ws_service["listen_port"],
            "cc_connect": mgmt_port,
        },
        "counts": {
            "groups": napcat_service["groups_count"],
            "sessions": sess_data["total"],
            "live_sessions": sess_data["live_count"],
            "memory_users": mem_stats["total_users"],
            "memory_facts": mem_stats["total_facts"],
        },
        "recent_events": ws_tail,
    }
    with _overview_cache_lock:
        _overview_cache["ts"] = time.monotonic()
        _overview_cache["data"] = copy.deepcopy(result)
    return result


# ---------------------------------------------------------------------------
# Service Control Actions (bot_runner.py cross-platform)
# ---------------------------------------------------------------------------

def _run_bot_runner(subcommand, timeout=30.0):
    if not os.path.isfile(BOT_RUNNER_SCRIPT):
        raise ServiceError("找不到 bot_runner.py，无法执行服务控制操作", status_code=500)
    py_bin = bot_env.resolve_python_bin(PROJECT_DIR)
    try:
        proc = subprocess.run(
            [py_bin, BOT_RUNNER_SCRIPT, subcommand],
            cwd=PROJECT_DIR,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=bot_env.build_subprocess_env(),
            creationflags=bot_env.subprocess_creationflags(),
        )
    except subprocess.TimeoutExpired:
        raise ServiceError(f"执行服务控制（{subcommand}）超时（>{int(timeout)}s）", status_code=504)
    except Exception as e:
        raise ServiceError(f"执行 bot_runner.py {subcommand} 失败：{e}", status_code=500)
    finally:
        _invalidate_proc_cache()
        invalidate_overview_cache()

    output = _ANSI_ESCAPE_RE.sub("", ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip())
    return proc.returncode, output


def run_control(action):
    act = str(action or "").strip().lower()

    if act == "sync-sandbox":
        res = stores.sync_sandbox()
        invalidate_overview_cache()
        saved_tok = int(res.get("saved_tokens") or 0)
        msg = (
            f"已按当前插件配置编译并同步 workspace/GEMINI.md 到 chat-sandbox/GEMINI.md（精简约 {saved_tok} tokens）"
            if saved_tok > 0
            else "已将 workspace/GEMINI.md 同步到 chat-sandbox/GEMINI.md"
        )
        return {
            "action": act,
            "ok": True,
            "message": msg,
            "detail": res,
        }

    if act == "reload-cc-connect":
        ok, data, status = _cc_call("/api/v1/reload", method="POST", payload={}, timeout=5.0)
        invalidate_overview_cache()
        if not ok:
            raise ServiceError(f"cc-connect 重载失败：{data}", status_code=status or 502)
        return {
            "action": act,
            "ok": True,
            "message": "cc-connect 配置已热重载",
            "detail": data,
        }

    if act == "restart-ws-guard":
        os.makedirs(RUN_DIR, exist_ok=True)
        rc, output = _run_bot_runner("restart-ws-guard", timeout=25.0)
        if rc != 0:
            raise ServiceError(f"重启 ws-guard 失败：{output or f'退出码 {rc}'}", status_code=500)
        sup_pid = _read_pid_file(GUARD_SUP_PID_FILE)
        return {
            "action": act,
            "ok": True,
            "mode": "bot-runner",
            "supervisor_pid": sup_pid,
            "output": output,
            "message": "已触发 ws-guard 无感重启，数秒内将自动用最新配置恢复运行，cc-connect 会自动重连。",
        }

    if act == "stop-bot":
        rc, output = _run_bot_runner("stop", timeout=30.0)
        return {
            "action": act,
            "ok": rc == 0,
            "returncode": rc,
            "output": output,
            "message": "已停止机器人服务" if rc == 0 else f"停止机器人服务返回异常（退出码 {rc}）",
        }

    if act == "start-bot":
        consistency = stores.check_config_consistency()
        if not consistency["valid"]:
            raise ServiceError(
                "配置校验未通过，拒绝启动：" + "；".join(consistency["problems"]),
                status_code=400,
                extra={"problems": consistency["problems"]},
            )
        procs = inspect_processes(force=True)
        if procs["cc_connect"] is not None:
            return {
                "action": act,
                "ok": True,
                "already_running": True,
                "pid": procs["cc_connect"]["pid"],
                "message": f"cc-connect 已在运行中（PID {procs['cc_connect']['pid']}），无需重复启动。",
            }

        rc, output = _run_bot_runner("start-bg", timeout=30.0)
        time.sleep(0.5)
        after = inspect_processes(force=True)
        pid = (after.get("cc_connect") or {}).get("pid") or after.get("start_loop_pid")
        ok = rc == 0
        return {
            "action": act,
            "ok": ok,
            "started": ok,
            "returncode": rc,
            "pid": pid,
            "output": output,
            "message": f"机器人服务已在后台启动{f'（PID {pid}）' if pid else ''}，请稍候几秒刷新状态。"
            if ok
            else f"机器人服务启动失败：{output or f'退出码 {rc}'}",
        }

    if act == "restart-bot":
        consistency = stores.check_config_consistency()
        if not consistency["valid"]:
            raise ServiceError(
                "配置校验未通过，拒绝重启（避免停机后无法启动）：" + "；".join(consistency["problems"]),
                status_code=400,
                extra={"problems": consistency["problems"]},
            )
        rc, output = _run_bot_runner("restart", timeout=45.0)
        time.sleep(0.5)
        after = inspect_processes(force=True)
        pid = (after.get("cc_connect") or {}).get("pid") or after.get("start_loop_pid")
        ok = rc == 0
        return {
            "action": act,
            "ok": ok,
            "returncode": rc,
            "pid": pid,
            "output": output,
            "message": "机器人服务已完成完整重启，请稍候几秒刷新状态。"
            if ok
            else f"重启未完全成功：{output or f'退出码 {rc}'}",
        }

    raise ServiceError(f"不支持的控制操作：{action}", status_code=400)
