"""NapCat WebUI REST & SSE API client (Python 3.9 stdlib only).

Authenticates automatically using the locally configured NapCat WebUI token
from `napcat/config/webui.json` (via `stores.get_napcat_webui_meta()`), caches
the 1-hour NapCat session Credential server-side, and proxies NapCat's dashboard
data, QR/quick-login state, live CPU/RAM telemetry, OneBot 11 network config,
NapCat core config, plugins, real-time logs, log files, and API debugger so
the user never needs to manually log into NapCat WebUI.
"""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple
import urllib.error
import urllib.parse
import urllib.request

from . import qrcode_svg, stores

_ANSI_ESCAPE_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])")
_CREDENTIAL_TTL = 45 * 60.0  # NapCat credentials are valid for 60 minutes; refresh at 45m
_VERSIONS_TTL = 30.0
_SYS_SNAPSHOT_TTL = 5.0
_SCHEMAS_TTL = 120.0

_auth_lock = threading.Lock()
_auth_state: Dict[str, Any] = {
    "port": 0,
    "token_hash": "",
    "credential": "",
    "acquired_at": 0.0,
    "require_2fa": False,
    "last_error": "",
    "last_fail_at": 0.0,
}

_versions_lock = threading.Lock()
_versions_cache: Dict[str, Any] = {"ts": 0.0, "data": None}

_sys_lock = threading.Lock()
_sys_cache: Dict[str, Any] = {"ts": 0.0, "data": None}

_schemas_lock = threading.Lock()
_schemas_cache: Dict[str, Any] = {"ts": 0.0, "data": None}

_CORE_CONFIG_ALLOWED_KEYS = {
    "fileLog",
    "consoleLog",
    "fileLogLevel",
    "consoleLogLevel",
    "packetBackend",
    "packetServer",
    "o3HookMode",
    "autoTimeSync",
}

_VALID_LOG_LEVELS = {"debug", "info", "warn", "error", "fatal", "off"}

_OB11_NETWORK_SECTIONS = (
    "httpServers",
    "httpSseServers",
    "httpClients",
    "websocketServers",
    "websocketClients",
)


def invalidate_caches() -> None:
    """Clear cached credentials and metadata (e.g. after container restart)."""
    with _auth_lock:
        _auth_state.update({
            "credential": "",
            "acquired_at": 0.0,
            "require_2fa": False,
            "last_error": "",
            "last_fail_at": 0.0,
        })
    with _versions_lock:
        _versions_cache["ts"] = 0.0
        _versions_cache["data"] = None
    with _sys_lock:
        _sys_cache["ts"] = 0.0
        _sys_cache["data"] = None
    with _schemas_lock:
        _schemas_cache["ts"] = 0.0
        _schemas_cache["data"] = None


def update_sys_status_cache(sample: Dict[str, Any]) -> None:
    """Update the shared latest system status snapshot (e.g. from SSE stream)."""
    if not isinstance(sample, dict):
        return
    with _sys_lock:
        _sys_cache["ts"] = time.time()
        _sys_cache["data"] = _normalize_sys_status(sample)


def _get_meta() -> Dict[str, Any]:
    return stores.get_napcat_webui_meta()


def _base_url(meta: Optional[Dict[str, Any]] = None) -> str:
    m = meta or _get_meta()
    port = int(m.get("port") or 6099)
    return f"http://127.0.0.1:{port}"


def _token_hash(token: str) -> str:
    return hashlib.sha256((str(token) + ".napcat").encode("utf-8")).hexdigest()


def _raw_http_json(
    url: str,
    method: str = "GET",
    headers: Optional[Dict[str, str]] = None,
    payload: Optional[Any] = None,
    timeout: float = 2.5,
) -> Tuple[bool, Any, int]:
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
            msg = parsed.get("message") or parsed.get("error") or f"HTTP {e.code}"
        except Exception:
            msg = f"HTTP {e.code}: {err_body or e.reason}"
        return False, msg, e.code
    except Exception as e:
        return False, str(e), 0


def get_credential(force: bool = False, timeout: float = 2.5) -> Tuple[Optional[str], Dict[str, Any]]:
    """Obtain or return cached NapCat WebUI Bearer Credential.

    Returns `(credential_or_None, status_meta)`.
    """
    meta = _get_meta()
    port = int(meta.get("port") or 6099)
    token = str(meta.get("token") or "")
    disabled = bool(meta.get("disabled"))

    if disabled:
        return None, {
            "ok": False,
            "authenticated": False,
            "require_2fa": False,
            "disabled": True,
            "error": "NapCat WebUI 在配置中已禁用",
        }
    if not token:
        return None, {
            "ok": False,
            "authenticated": False,
            "require_2fa": False,
            "disabled": False,
            "error": "未在 napcat/config/webui.json 中读取到 WebUI Token",
        }

    thash = _token_hash(token)
    now = time.time()

    with _auth_lock:
        if (
            not force
            and _auth_state["credential"]
            and _auth_state["port"] == port
            and _auth_state["token_hash"] == thash
            and (now - _auth_state["acquired_at"]) < _CREDENTIAL_TTL
        ):
            return _auth_state["credential"], {
                "ok": True,
                "authenticated": True,
                "require_2fa": False,
                "disabled": False,
                "error": "",
            }
        # Avoid hammering login rate-limit if we just failed within 4 seconds
        if (
            not force
            and _auth_state["last_error"]
            and _auth_state["port"] == port
            and _auth_state["token_hash"] == thash
            and (now - _auth_state["last_fail_at"]) < 4.0
        ):
            return None, {
                "ok": False,
                "authenticated": False,
                "require_2fa": bool(_auth_state["require_2fa"]),
                "disabled": False,
                "error": _auth_state["last_error"],
            }

    url = f"http://127.0.0.1:{port}/api/auth/login"
    ok, res, _ = _raw_http_json(url, method="POST", payload={"hash": thash}, timeout=timeout)

    if not ok or not isinstance(res, dict):
        err = str(res or "连接 NapCat WebUI 失败")
        with _auth_lock:
            _auth_state.update({
                "port": port,
                "token_hash": thash,
                "credential": "",
                "acquired_at": 0.0,
                "require_2fa": False,
                "last_error": err,
                "last_fail_at": time.time(),
            })
        return None, {
            "ok": False,
            "authenticated": False,
            "require_2fa": False,
            "disabled": False,
            "error": err,
        }

    code = res.get("code")
    data = res.get("data") if isinstance(res.get("data"), dict) else {}

    if code == 0 and data.get("require2FA"):
        err = "NapCat WebUI 已开启两步验证 (2FA)，无法自动免登直连部分管理接口"
        with _auth_lock:
            _auth_state.update({
                "port": port,
                "token_hash": thash,
                "credential": "",
                "acquired_at": 0.0,
                "require_2fa": True,
                "last_error": err,
                "last_fail_at": time.time(),
            })
        return None, {
            "ok": False,
            "authenticated": False,
            "require_2fa": True,
            "disabled": False,
            "error": err,
        }

    cred = str(data.get("Credential") or "")
    if code == 0 and cred:
        with _auth_lock:
            _auth_state.update({
                "port": port,
                "token_hash": thash,
                "credential": cred,
                "acquired_at": time.time(),
                "require_2fa": False,
                "last_error": "",
                "last_fail_at": 0.0,
            })
        return cred, {
            "ok": True,
            "authenticated": True,
            "require_2fa": False,
            "disabled": False,
            "error": "",
        }

    err = str(res.get("message") or f"NapCat WebUI 登录失败 (code={code})")
    with _auth_lock:
        _auth_state.update({
            "port": port,
            "token_hash": thash,
            "credential": "",
            "acquired_at": 0.0,
            "require_2fa": False,
            "last_error": err,
            "last_fail_at": time.time(),
        })
    return None, {
        "ok": False,
        "authenticated": False,
        "require_2fa": False,
        "disabled": False,
        "error": err,
    }


def api_request(
    method: str,
    path: str,
    payload: Optional[Any] = None,
    query: Optional[Dict[str, str]] = None,
    timeout: float = 2.5,
) -> Tuple[bool, Any, str]:
    """Call an authenticated NapCat WebUI API endpoint with automatic 1x re-auth retry."""
    meta = _get_meta()
    base = _base_url(meta)
    full_url = f"{base}{path}"
    if query:
        full_url = f"{full_url}?{urllib.parse.urlencode(query)}"

    for attempt in range(2):
        cred, auth_meta = get_credential(force=(attempt == 1), timeout=timeout)
        if not cred:
            return False, None, auth_meta.get("error") or "NapCat WebUI 鉴权失败"

        ok, res, status = _raw_http_json(
            full_url,
            method=method,
            headers={"Authorization": f"Bearer {cred}"},
            payload=payload,
            timeout=timeout,
        )
        if not ok:
            if status == 401 and attempt == 0:
                continue
            return False, None, str(res)

        if isinstance(res, dict):
            code = res.get("code")
            msg = str(res.get("message") or "")
            if code == 0:
                return True, res.get("data"), ""
            if "unauthorized" in msg.lower() and attempt == 0:
                continue
            return False, None, msg or f"NapCat API 返回 code={code}"
        return True, res, ""

    return False, None, "NapCat WebUI 鉴权重试失败"


def open_sse(path: str, timeout: float = 15.0):
    """Open an authenticated upstream SSE response stream from NapCat WebUI."""
    meta = _get_meta()
    base = _base_url(meta)
    cred, auth_meta = get_credential(force=False, timeout=2.5)
    if not cred:
        raise RuntimeError(auth_meta.get("error") or "NapCat WebUI 未鉴权")

    url = f"{base}{path}"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {cred}",
            "Accept": "text/event-stream",
            "Cache-Control": "no-cache",
        },
        method="GET",
    )
    return urllib.request.urlopen(req, timeout=timeout)


# ---------------------------------------------------------------------------
# Normalization & Disk Fallbacks
# ---------------------------------------------------------------------------

def _napcat_config_dir() -> str:
    env = stores.parse_bot_env()
    cfg_path = stores._resolve_napcat_webui_config_path(env)
    return os.path.dirname(cfg_path)


def _read_json_file(path: str) -> Optional[Dict[str, Any]]:
    try:
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
    except Exception:
        pass
    return None


def _normalize_sys_status(raw: Dict[str, Any]) -> Dict[str, Any]:
    cpu = raw.get("cpu") if isinstance(raw.get("cpu"), dict) else {}
    cpu_usage = cpu.get("usage") if isinstance(cpu.get("usage"), dict) else {}
    mem = raw.get("memory") if isinstance(raw.get("memory"), dict) else {}
    mem_usage = mem.get("usage") if isinstance(mem.get("usage"), dict) else {}

    def _f(v: Any, default: float = 0.0) -> float:
        try:
            return round(float(v), 2)
        except (TypeError, ValueError):
            return default

    total_mb = _f(mem.get("total"))
    sys_mb = _f(mem_usage.get("system"))
    qq_mb = _f(mem_usage.get("qq"))
    mem_pct = round((sys_mb / total_mb) * 100.0, 1) if total_mb > 0 else 0.0
    qq_mem_pct = round((qq_mb / total_mb) * 100.0, 2) if total_mb > 0 else 0.0

    return {
        "ts": time.time(),
        "arch": str(raw.get("arch") or ""),
        "cpu": {
            "cores": int(cpu.get("core") or 0),
            "model": str(cpu.get("model") or ""),
            "speed_ghz": _f(cpu.get("speed")),
            "system_pct": _f(cpu_usage.get("system")),
            "qq_pct": _f(cpu_usage.get("qq")),
        },
        "memory": {
            "total_mb": total_mb,
            "system_mb": sys_mb,
            "qq_mb": qq_mb,
            "system_pct": mem_pct,
            "qq_pct": qq_mem_pct,
        },
    }


def get_cached_sys_status() -> Optional[Dict[str, Any]]:
    """Return the most recently cached system CPU/RAM snapshot without blocking."""
    with _sys_lock:
        return dict(_sys_cache["data"]) if _sys_cache["data"] is not None else None


def sample_sys_status(force: bool = False, timeout: float = 4.5) -> Optional[Dict[str, Any]]:
    """Return latest system CPU/RAM snapshot, reading 1 SSE event if stale."""
    now = time.time()
    with _sys_lock:
        if not force and _sys_cache["data"] is not None and (now - _sys_cache["ts"]) < _SYS_SNAPSHOT_TTL:
            return dict(_sys_cache["data"])

    try:
        resp = open_sse("/api/base/GetSysStatusRealTime", timeout=timeout)
        try:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                raw_line = resp.readline()
                if not raw_line:
                    break
                line = raw_line.decode("utf-8", errors="replace").strip()
                if line.startswith("data:"):
                    payload_str = line[5:].strip()
                    if payload_str:
                        parsed = json.loads(payload_str)
                        if isinstance(parsed, dict):
                            norm = _normalize_sys_status(parsed)
                            with _sys_lock:
                                _sys_cache["ts"] = time.time()
                                _sys_cache["data"] = norm
                            return dict(norm)
        finally:
            try:
                resp.close()
            except Exception:
                pass
    except Exception:
        pass

    with _sys_lock:
        return dict(_sys_cache["data"]) if _sys_cache["data"] is not None else None


def _summarize_ob11_config(cfg: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(cfg, dict):
        return {
            "available": False,
            "source": "none",
            "config": {},
            "summary": {"total": 0, "enabled": 0, "by_type": {}},
        }
    network = cfg.get("network") if isinstance(cfg.get("network"), dict) else {}
    by_type: Dict[str, Dict[str, int]] = {}
    total = 0
    enabled = 0
    for sec in _OB11_NETWORK_SECTIONS:
        items = network.get(sec) if isinstance(network.get(sec), list) else []
        en = sum(1 for item in items if isinstance(item, dict) and item.get("enable"))
        by_type[sec] = {"total": len(items), "enabled": en}
        total += len(items)
        enabled += en
    return {
        "available": True,
        "config": cfg,
        "summary": {
            "total": total,
            "enabled": enabled,
            "by_type": by_type,
        },
    }


def get_versions(force: bool = False) -> Dict[str, Any]:
    now = time.time()
    with _versions_lock:
        if not force and _versions_cache["data"] is not None and (now - _versions_cache["ts"]) < _VERSIONS_TTL:
            return dict(_versions_cache["data"])

    nc_ok, nc_data, _ = api_request("GET", "/api/base/GetNapCatVersion", timeout=2.0)
    qq_ok, qq_data, _ = api_request("GET", "/api/base/QQVersion", timeout=2.0)
    pf_ok, pf_data, _ = api_request("POST", "/api/QQLogin/GetPlatformInfo", payload={}, timeout=2.0)

    napcat_ver = ""
    if nc_ok:
        if isinstance(nc_data, dict):
            napcat_ver = str(nc_data.get("version") or "")
        elif isinstance(nc_data, str):
            napcat_ver = nc_data

    qq_ver = str(qq_data or "") if qq_ok else ""
    platform = str(pf_data.get("platform") or "") if (pf_ok and isinstance(pf_data, dict)) else ""

    result = {
        "napcat": napcat_ver,
        "qq": qq_ver,
        "platform": platform,
    }
    if napcat_ver or qq_ver or platform:
        with _versions_lock:
            _versions_cache["ts"] = now
            _versions_cache["data"] = result
    return result


def get_login_state(include_qr_svg: bool = False) -> Dict[str, Any]:
    """Fetch NapCat QQ login status, login info, auto-login QQ, quick login list & optional QR SVG."""
    st_ok, st_data, st_err = api_request("POST", "/api/QQLogin/CheckLoginStatus", payload={}, timeout=2.0)
    info_ok, info_data, _ = api_request("POST", "/api/QQLogin/GetQQLoginInfo", payload={}, timeout=2.0)
    auto_ok, auto_data, _ = api_request("POST", "/api/QQLogin/GetQuickLoginQQ", payload={}, timeout=2.0)

    st = st_data if (st_ok and isinstance(st_data, dict)) else {}
    info = info_data if (info_ok and isinstance(info_data, dict)) else {}

    is_login = bool(st.get("isLogin"))
    is_offline = bool(st.get("isOffline"))
    login_phase = str(st.get("loginPhase") or "")
    qr_accepted = bool(st.get("qrLoginAccepted"))
    core_ready = bool(st.get("coreReady"))
    qrcode_url = str(st.get("qrcodeurl") or "")
    login_error = str(st.get("loginError") or "")

    # Only query quick-login list when not logged in or explicitly requested (it's empty when logged in)
    quick_list: List[Dict[str, Any]] = []
    if st_ok and (not is_login or include_qr_svg):
        ql_ok, ql_data, _ = api_request("POST", "/api/QQLogin/GetQuickLoginListNew", payload={}, timeout=2.0)
        if ql_ok and isinstance(ql_data, list):
            for item in ql_data:
                if isinstance(item, dict):
                    quick_list.append({
                        "uin": str(item.get("uin") or ""),
                        "nick": str(item.get("nickName") or item.get("nick") or ""),
                        "face_url": str(item.get("faceUrl") or ""),
                        "is_quick_login": bool(item.get("isQuickLogin", True)),
                        "is_auto_login": bool(item.get("isAutoLogin", False)),
                    })
                elif isinstance(item, (str, int)):
                    quick_list.append({"uin": str(item), "nick": "", "face_url": "", "is_quick_login": True, "is_auto_login": False})

    # If waiting for login and qrcode_url empty, ask GetQQLoginQrcode directly
    if st_ok and not is_login and not qrcode_url:
        qr_ok, qr_data, _ = api_request("POST", "/api/QQLogin/GetQQLoginQrcode", payload={}, timeout=2.0)
        if qr_ok and isinstance(qr_data, dict):
            qrcode_url = str(qr_data.get("qrcode") or qr_data.get("qrcodeurl") or "")

    qr_svg = ""
    if qrcode_url and (include_qr_svg or not is_login):
        try:
            qr_svg = qrcode_svg.generate_qr_svg(qrcode_url, border=3, size_px=220)
        except Exception:
            qr_svg = ""

    return {
        "ok": st_ok,
        "error": "" if st_ok else st_err,
        "is_login": is_login,
        "is_offline": is_offline,
        "login_phase": login_phase,
        "qr_login_accepted": qr_accepted,
        "core_ready": core_ready,
        "qrcode_url": qrcode_url,
        "qrcode_svg": qr_svg,
        "login_error": login_error,
        "uid": str(info.get("uid") or ""),
        "uin": str(info.get("uin") or ""),
        "nick": str(info.get("nick") or ""),
        "online": info.get("online") if "online" in info else is_login,
        "avatar_url": str(info.get("avatarUrl") or ""),
        "auto_login_qq": str(auto_data or "") if auto_ok else "",
        "quick_login_list": quick_list,
    }


def refresh_qrcode() -> Dict[str, Any]:
    """Ask NapCat to refresh its QQ login QR code and return updated login state + SVG."""
    ok, data, err = api_request("POST", "/api/QQLogin/RefreshQRcode", payload={}, timeout=4.0)
    if not ok:
        raise stores.StoreError(f"刷新二维码失败：{err}", status_code=502)
    state = get_login_state(include_qr_svg=True)
    if isinstance(data, dict):
        if data.get("restarting"):
            state["restarting"] = True
        new_url = str(data.get("qrcodeurl") or "")
        if new_url and new_url != state.get("qrcode_url"):
            state["qrcode_url"] = new_url
            try:
                state["qrcode_svg"] = qrcode_svg.generate_qr_svg(new_url, border=3, size_px=220)
            except Exception:
                pass
    return state


def quick_login(uin: str) -> Dict[str, Any]:
    uin_s = str(uin or "").strip()
    if not uin_s.isdigit():
        raise stores.StoreError("QQ 号必须是纯数字", status_code=400)
    ok, _, err = api_request("POST", "/api/QQLogin/SetQuickLogin", payload={"uin": uin_s}, timeout=6.0)
    if not ok:
        raise stores.StoreError(f"快捷登录失败：{err}", status_code=502)
    return {"ok": True, "uin": uin_s, "message": f"已向 NapCat 发送快捷登录请求（QQ {uin_s}）"}


def set_auto_login_qq(uin: str) -> Dict[str, Any]:
    uin_s = str(uin or "").strip()
    if uin_s and not uin_s.isdigit():
        raise stores.StoreError("自动登录 QQ 号必须是纯数字或留空", status_code=400)
    ok, _, err = api_request("POST", "/api/QQLogin/SetQuickLoginQQ", payload={"uin": uin_s}, timeout=3.0)
    if not ok:
        raise stores.StoreError(f"设置自动登录账号失败：{err}", status_code=502)
    msg = f"已将自动登录 QQ 设为 {uin_s}" if uin_s else "已清空自动登录 QQ 设置"
    return {"ok": True, "auto_login_qq": uin_s, "message": msg}


def get_ob11_config(bot_qq: str = "") -> Dict[str, Any]:
    """Fetch OneBot 11 network & global config via NapCat API, with disk fallback."""
    ok, data, err = api_request("POST", "/api/OB11Config/GetConfig", payload={}, timeout=2.5)
    if ok and isinstance(data, dict):
        out = _summarize_ob11_config(data)
        out["source"] = "api"
        out["error"] = ""
        return out

    # Fallback to local disk config file
    cfg_dir = _napcat_config_dir()
    candidates = []
    if bot_qq and str(bot_qq).strip().isdigit():
        candidates.append(os.path.join(cfg_dir, f"onebot11_{str(bot_qq).strip()}.json"))
    candidates.append(os.path.join(cfg_dir, "onebot11.json"))
    if os.path.isdir(cfg_dir):
        for fname in sorted(os.listdir(cfg_dir)):
            if fname.startswith("onebot11_") and fname.endswith(".json"):
                p = os.path.join(cfg_dir, fname)
                if p not in candidates:
                    candidates.append(p)
    for path in candidates:
        disk_cfg = _read_json_file(path)
        if disk_cfg is not None:
            out = _summarize_ob11_config(disk_cfg)
            out["source"] = "disk"
            out["file"] = os.path.basename(path)
            out["error"] = err
            return out

    out = _summarize_ob11_config(None)
    out["error"] = err
    return out


def set_ob11_config(config_obj: Dict[str, Any]) -> Dict[str, Any]:
    """Save OneBot 11 config via NapCat API (`OB11Config/SetConfig`)."""
    if not isinstance(config_obj, dict):
        raise stores.StoreError("OneBot 配置必须是 JSON 对象", status_code=400)
    network = config_obj.get("network")
    if not isinstance(network, dict):
        raise stores.StoreError("OneBot 配置缺少 network 字段", status_code=400)
    for sec in _OB11_NETWORK_SECTIONS:
        if sec in network and not isinstance(network[sec], list):
            raise stores.StoreError(f"network.{sec} 必须是列表", status_code=400)

    payload_str = json.dumps(config_obj, ensure_ascii=False)
    ok, _, err = api_request("POST", "/api/OB11Config/SetConfig", payload={"config": payload_str}, timeout=4.0)
    if not ok:
        raise stores.StoreError(f"保存 OneBot 网络配置失败：{err}", status_code=502)
    updated = get_ob11_config()
    updated["message"] = "OneBot 11 网络配置已保存并热生效"
    return updated


def get_core_config(bot_qq: str = "") -> Dict[str, Any]:
    """Fetch NapCat per-account / global core config via API with disk fallback."""
    ok, data, err = api_request("GET", "/api/NapCatConfig/GetUinConfig", timeout=2.0)
    scope = "uin"
    if not ok or not isinstance(data, dict):
        ok2, data2, err2 = api_request("GET", "/api/NapCatConfig/GetConfig", timeout=2.0)
        if ok2 and isinstance(data2, dict):
            ok, data, err, scope = True, data2, "", "global"
        else:
            err = err2 or err

    if ok and isinstance(data, dict):
        return {"available": True, "source": "api", "scope": scope, "config": data, "error": ""}

    cfg_dir = _napcat_config_dir()
    candidates = []
    if bot_qq and str(bot_qq).strip().isdigit():
        candidates.append(os.path.join(cfg_dir, f"napcat_{str(bot_qq).strip()}.json"))
    candidates.append(os.path.join(cfg_dir, "napcat.json"))
    for path in candidates:
        disk_cfg = _read_json_file(path)
        if disk_cfg is not None:
            return {"available": True, "source": "disk", "scope": "disk", "config": disk_cfg, "error": err}

    return {"available": False, "source": "none", "scope": "none", "config": {}, "error": err}


def set_core_config(updates: Dict[str, Any]) -> Dict[str, Any]:
    """Update NapCat core/account settings (`fileLog`, `consoleLog`, levels, `autoTimeSync`, etc.)."""
    if not isinstance(updates, dict) or not updates:
        raise stores.StoreError("配置更新不能为空", status_code=400)

    clean: Dict[str, Any] = {}
    for k, v in updates.items():
        if k not in _CORE_CONFIG_ALLOWED_KEYS:
            continue
        if k in ("fileLog", "consoleLog", "autoTimeSync"):
            clean[k] = bool(v)
        elif k in ("fileLogLevel", "consoleLogLevel"):
            lv = str(v or "").strip().lower()
            if lv not in _VALID_LOG_LEVELS:
                raise stores.StoreError(f"无效的日志级别 {k}={v}", status_code=400)
            clean[k] = lv
        elif k == "o3HookMode":
            try:
                clean[k] = int(v)
            except (TypeError, ValueError):
                raise stores.StoreError("o3HookMode 必须是整数", status_code=400)
        else:
            clean[k] = str(v or "").strip()

    if not clean:
        raise stores.StoreError("没有可保存的有效配置项", status_code=400)

    ok, _, err = api_request("POST", "/api/NapCatConfig/SetUinConfig", payload=clean, timeout=3.0)
    if not ok and "not login" in err.lower():
        ok, _, err = api_request("POST", "/api/NapCatConfig/SetConfig", payload=clean, timeout=3.0)
    if not ok:
        raise stores.StoreError(f"保存 NapCat 核心配置失败：{err}", status_code=502)

    refreshed = get_core_config()
    refreshed["message"] = "NapCat 核心配置已保存"
    return refreshed


def get_webui_config() -> Dict[str, Any]:
    """Fetch NapCat WebUI server config & 2FA status."""
    ok, data, err = api_request("GET", "/api/WebUIConfig/GetConfig", timeout=2.0)
    tfa_ok, tfa_data, _ = api_request("GET", "/api/auth/2fa/status", timeout=2.0)

    cfg = data if (ok and isinstance(data, dict)) else {}
    if not cfg:
        disk = _read_json_file(stores._resolve_napcat_webui_config_path())
        if isinstance(disk, dict):
            cfg = {
                "host": disk.get("host", "::"),
                "port": disk.get("port", 6099),
                "loginRate": disk.get("loginRate", 10),
                "disableWebUI": bool(disk.get("disableWebUI", False)),
                "accessControlMode": disk.get("accessControlMode", "none"),
                "ipWhitelist": disk.get("ipWhitelist", []),
                "ipBlacklist": disk.get("ipBlacklist", []),
                "enableXForwardedFor": bool(disk.get("enableXForwardedFor", False)),
            }

    enable_2fa = False
    if tfa_ok and isinstance(tfa_data, dict):
        enable_2fa = bool(tfa_data.get("enable2FA"))
    else:
        disk = _read_json_file(stores._resolve_napcat_webui_config_path())
        if isinstance(disk, dict):
            enable_2fa = bool(disk.get("enable2FA", False))

    return {
        "ok": ok,
        "error": "" if ok else err,
        "host": str(cfg.get("host") or "::"),
        "port": int(cfg.get("port") or 6099),
        "login_rate": int(cfg.get("loginRate") or 10),
        "disable_webui": bool(cfg.get("disableWebUI", False)),
        "access_control_mode": str(cfg.get("accessControlMode") or "none"),
        "ip_whitelist": cfg.get("ipWhitelist") if isinstance(cfg.get("ipWhitelist"), list) else [],
        "ip_blacklist": cfg.get("ipBlacklist") if isinstance(cfg.get("ipBlacklist"), list) else [],
        "enable_x_forwarded_for": bool(cfg.get("enableXForwardedFor", False)),
        "enable_2fa": enable_2fa,
    }


def get_plugins() -> Dict[str, Any]:
    ok, data, err = api_request("GET", "/api/Plugin/List", timeout=2.0)
    if not ok or not isinstance(data, dict):
        return {
            "ok": False,
            "items": [],
            "plugin_manager_not_found": True,
            "extension_pages": [],
            "error": err,
        }
    return {
        "ok": True,
        "items": data.get("plugins") if isinstance(data.get("plugins"), list) else [],
        "plugin_manager_not_found": bool(data.get("pluginManagerNotFound")),
        "extension_pages": data.get("extensionPages") if isinstance(data.get("extensionPages"), list) else [],
        "error": "",
    }


def set_plugin_status(plugin_id: str, enable: bool) -> Dict[str, Any]:
    pid = str(plugin_id or "").strip()
    if not pid:
        raise stores.StoreError("插件 ID 不能为空", status_code=400)
    ok, data, err = api_request(
        "POST",
        "/api/Plugin/SetStatus",
        payload={"id": pid, "enable": bool(enable)},
        timeout=5.0,
    )
    if not ok:
        raise stores.StoreError(f"切换插件状态失败：{err}", status_code=502)
    return {
        "ok": True,
        "id": pid,
        "enable": bool(enable),
        "message": (data.get("message") if isinstance(data, dict) else None) or ("插件已启用" if enable else "插件已禁用"),
        "plugins": get_plugins(),
    }


def get_log_files() -> Dict[str, Any]:
    ok, data, err = api_request("GET", "/api/Log/GetLogList", timeout=2.5)
    if not ok:
        return {"ok": False, "files": [], "error": err}
    files = data if isinstance(data, list) else []
    return {"ok": True, "files": [str(x) for x in files], "error": ""}


def get_log_file_content(log_id: str) -> Dict[str, Any]:
    lid = str(log_id or "").strip()
    if not lid or ".." in lid or "/" in lid or "\\" in lid:
        raise stores.StoreError("无效的日志文件名", status_code=400)
    ok, data, err = api_request("GET", "/api/Log/GetLog", query={"id": lid}, timeout=4.0)
    if not ok:
        raise stores.StoreError(f"读取 NapCat 日志文件失败：{err}", status_code=502)
    raw = str(data or "")
    clean = _ANSI_ESCAPE_RE.sub("", raw)
    lines = clean.splitlines()
    return {
        "ok": True,
        "id": lid,
        "count": len(lines),
        "lines": lines,
        "text": clean,
    }


def get_debug_schemas(force: bool = False) -> Dict[str, Any]:
    """Fetch compact OneBot action catalog from NapCat `/api/Debug/schemas`."""
    now = time.time()
    with _schemas_lock:
        if not force and _schemas_cache["data"] is not None and (now - _schemas_cache["ts"]) < _SCHEMAS_TTL:
            return dict(_schemas_cache["data"])

    ok, data, err = api_request("GET", "/api/Debug/schemas", timeout=4.0)
    if not ok or not isinstance(data, dict):
        return {"ok": False, "actions": [], "count": 0, "error": err}

    actions = []
    for name, meta in sorted(data.items()):
        if not isinstance(meta, dict):
            continue
        payload_schema = meta.get("payload") if isinstance(meta.get("payload"), dict) else {}
        props = payload_schema.get("properties") if isinstance(payload_schema.get("properties"), dict) else {}
        required = payload_schema.get("required") if isinstance(payload_schema.get("required"), list) else []
        params_info = []
        for pname, pmeta in props.items():
            if not isinstance(pmeta, dict):
                continue
            params_info.append({
                "name": str(pname),
                "type": str(pmeta.get("type") or "any"),
                "description": str(pmeta.get("description") or ""),
                "required": pname in required,
            })
        actions.append({
            "action": str(name),
            "description": str(meta.get("description") or ""),
            "tags": meta.get("tags") if isinstance(meta.get("tags"), list) else [],
            "params": params_info,
        })

    result = {"ok": True, "actions": actions, "count": len(actions), "error": ""}
    with _schemas_lock:
        _schemas_cache["ts"] = now
        _schemas_cache["data"] = result
    return dict(result)


def restart_napcat_process() -> Dict[str, Any]:
    ok, data, err = api_request("POST", "/api/QQLogin/RestartNapCat", payload={}, timeout=5.0)
    if not ok:
        raise stores.StoreError(f"重启 NapCat 进程失败：{err}", status_code=502)
    invalidate_caches()
    msg = (data.get("message") if isinstance(data, dict) else None) or "已向 NapCat 发送软重启指令"
    return {"ok": True, "message": msg}


def get_dashboard_snapshot(bot_qq: str = "", include_sys: bool = False) -> Dict[str, Any]:
    """Parallel-fetch NapCat WebUI API dashboard data (completes in ~80-150ms)."""
    cred, auth_meta = get_credential(force=False, timeout=2.0)

    if not cred:
        # Still provide disk-backed OB11/Core/WebUI config fallbacks!
        return {
            "webui_api": auth_meta,
            "versions": {"napcat": "", "qq": "", "platform": ""},
            "login_state": {
                "ok": False,
                "error": auth_meta.get("error", ""),
                "is_login": False,
                "is_offline": False,
                "login_phase": "",
                "qr_login_accepted": False,
                "core_ready": False,
                "qrcode_url": "",
                "qrcode_svg": "",
                "login_error": "",
                "uid": "",
                "uin": "",
                "nick": "",
                "online": False,
                "avatar_url": "",
                "auto_login_qq": "",
                "quick_login_list": [],
            },
            "ob11_config": get_ob11_config(bot_qq=bot_qq),
            "napcat_config": get_core_config(bot_qq=bot_qq),
            "webui_config": get_webui_config(),
            "plugins": {"ok": False, "items": [], "plugin_manager_not_found": True, "extension_pages": [], "error": auth_meta.get("error", "")},
            "log_files": [],
            "sys_status": get_cached_sys_status(),
        }

    tasks = {
        "versions": lambda: get_versions(),
        "login_state": lambda: get_login_state(include_qr_svg=False),
        "ob11_config": lambda: get_ob11_config(bot_qq=bot_qq),
        "napcat_config": lambda: get_core_config(bot_qq=bot_qq),
        "webui_config": lambda: get_webui_config(),
        "plugins": lambda: get_plugins(),
        "log_files": lambda: get_log_files(),
    }
    if include_sys:
        tasks["sys_status"] = lambda: sample_sys_status(force=False, timeout=4.5)

    results: Dict[str, Any] = {}
    with ThreadPoolExecutor(max_workers=len(tasks)) as pool:
        futs = {k: pool.submit(fn) for k, fn in tasks.items()}
        for k, fut in futs.items():
            try:
                results[k] = fut.result(timeout=4.0)
            except Exception as e:
                results[k] = {"ok": False, "error": str(e)} if k != "sys_status" else None

    lf = results.get("log_files")
    log_files_list = lf.get("files", []) if isinstance(lf, dict) else []

    return {
        "webui_api": auth_meta,
        "versions": results.get("versions") or {"napcat": "", "qq": "", "platform": ""},
        "login_state": results.get("login_state") or {},
        "ob11_config": results.get("ob11_config") or _summarize_ob11_config(None),
        "napcat_config": results.get("napcat_config") or {"available": False, "config": {}},
        "webui_config": results.get("webui_config") or {},
        "plugins": results.get("plugins") or {"ok": False, "items": [], "plugin_manager_not_found": True, "extension_pages": []},
        "log_files": log_files_list,
        "sys_status": results.get("sys_status") if include_sys else get_cached_sys_status(),
    }
