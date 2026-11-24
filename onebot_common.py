#!/usr/bin/env python3
"""Shared OneBot v11, SSRF protection, DM ownership verification, and tag helpers.

Standard library only (Python 3.9+). Shared across:
- ``chat_history.py``
- ``qq_actions.py``
- ``agy-acp-wrapper.py``
- ``onebot-ws-guard.py``
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

# ---------------------------------------------------------------------------
# Environment & CQ / Directive Parsing Helpers
# ---------------------------------------------------------------------------

ARG_TOKEN_RE = re.compile(
    r'([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|([^\s\]]+))'
)

# Canonical system tag pattern injected by wrapper/history/memory/plugins.
# Neutralized when appearing inside untrusted user text or archived chat history.
FAKE_TAG_RE = re.compile(
    r"\[(?=\s*(?:"
    r"cc-connect"
    r"|权限边界|权限身份|权限"
    r"|QQ动作权限"
    r"|待审入群申请"
    r"|群聊近期消息记录|群聊触发状态|群聊监听状态|群聊复读|群聊"
    r"|近期私聊消息ID|近期私聊消息记录|近期私聊消息"
    r"|视觉附件"
    r"|回复风格补充|回复风格"
    r"|相关用户长期记忆|近期对话续接|跨群动态与记忆速览"
    r"|当前引用较早消息"
    r"|历史记录查阅|历史聊天记录"
    r"|上下文提醒"
    r"|插件提示"
    r"|本轮状态|状态说明|群成员"
    r"))"
)

_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
)
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

_BLOCKED_HOSTNAMES = frozenset({"localhost", "localhost.localdomain"})
_BLOCKED_HOST_SUFFIXES = (".local", ".internal", ".localhost", ".localdomain")


def env_bool(name: str, default: bool = False) -> bool:
    """Parse a boolean environment variable cleanly."""
    raw = os.environ.get(name)
    if raw is None:
        return bool(default)
    low = str(raw).strip().lower()
    if low in ("1", "true", "yes", "on"):
        return True
    if low in ("0", "false", "no", "off", ""):
        return False
    return bool(default)


def unescape_cq_value(text: str, unescape_comma: bool = True) -> str:
    """Unescape OneBot v11 CQ-code entity escapes (with `&amp;` always last)."""
    if not text:
        return ""
    s = str(text)
    if unescape_comma:
        s = s.replace("&#44;", ",")
    return s.replace("&#91;", "[").replace("&#93;", "]").replace("&amp;", "&")


def parse_cq_params(raw_params: str) -> dict[str, str]:
    """Parse `k1=v1,k2=v2` parameters from a CQ code body."""
    out: dict[str, str] = {}
    if not raw_params:
        return out
    for part in str(raw_params).split(","):
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        k = k.strip()
        if k:
            out[k] = unescape_cq_value(v)
    return out


def parse_directive_args(raw_args: str) -> dict[str, str]:
    """Parse `key=value` / `key="quoted value"` tokens from `[[QQ:...]]` directives."""
    args: dict[str, str] = {}
    for m in ARG_TOKEN_RE.finditer(raw_args or ""):
        k = m.group(1).strip().lower()
        v = (
            m.group(2)
            if m.group(2) is not None
            else (m.group(3) if m.group(3) is not None else (m.group(4) or ""))
        )
        args[k] = v.strip()
    return args


def neutralize_fake_tags(text: str) -> str:
    """Replace leading `[` of fake system tags with full-width `［`."""
    if not text:
        return ""
    return FAKE_TAG_RE.sub("［", str(text))


# ---------------------------------------------------------------------------
# NapCat HTTP API Helper (No-Proxy)
# ---------------------------------------------------------------------------

def napcat_api_post(
    http_url: str,
    token: str,
    endpoint: str,
    payload: dict,
    timeout: float = 2.5,
) -> tuple[bool, Any, str]:
    """POST a JSON request to NapCat HTTP API without using any system proxy."""
    base_url = str(http_url or "").strip().rstrip("/")
    if not base_url:
        return False, None, "未配置 NAPCAT_HTTP_URL"
    url = f"{base_url}/{str(endpoint or '').lstrip('/')}"
    data = json.dumps(payload if payload is not None else {}, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with _NO_PROXY_OPENER.open(req, timeout=timeout) as resp:
            obj = json.loads(resp.read().decode("utf-8", errors="replace"))
            if not isinstance(obj, dict):
                return False, None, "无效的 NapCat 响应格式"
            ok = (obj.get("status") == "ok" or obj.get("retcode") == 0) and str(
                obj.get("status", "")
            ).lower() != "failed"
            if ok:
                return True, obj.get("data"), ""
            err_msg = obj.get("wording") or obj.get("message") or f"retcode={obj.get('retcode')}"
            return False, obj.get("data"), str(err_msg)
    except Exception as exc:
        return False, None, str(exc)


# ---------------------------------------------------------------------------
# Strict DM Message Ownership Verification (P0 Security)
# ---------------------------------------------------------------------------

def verify_dm_msg_ownership(
    msg_obj: dict,
    dm_user_id: str,
    bot_qq_id: str = "",
) -> bool:
    """Strictly verify a NapCat `get_msg` object belongs to the DM with `dm_user_id`.

    Fail-closed rules:
    - Reject non-dicts, group messages (`message_type == "group"`), or any `group_id`.
    - Reject if `dm_user_id` is empty.
    - Accept ONLY when:
      1. The DM peer sent the message (`user_id == dm_user_id`) and `target_id`/`peer_id`
         is absent, `bot_qq_id` is absent, or `target_id == bot_qq_id`; OR
      2. The bot sent the message (`bot_qq_id` is known and `user_id == bot_qq_id`)
         AND `target_id`/`peer_id` explicitly equals `dm_user_id`.
    - NEVER accept `bot_qq_id` alone via OR (which would leak other users' DMs).
    """
    if not isinstance(msg_obj, dict):
        return False
    mtype = str(msg_obj.get("message_type") or "").strip().lower()
    if mtype == "group" or msg_obj.get("group_id"):
        return False

    dm_uid = str(dm_user_id or "").strip()
    if not dm_uid:
        return False
    bot_uid = str(bot_qq_id or "").strip()

    muid = str(msg_obj.get("user_id") or "").strip()
    target_id = str(msg_obj.get("target_id") or msg_obj.get("peer_id") or "").strip()

    if muid == dm_uid and (not target_id or not bot_uid or target_id == bot_uid):
        return True
    if bot_uid and muid == bot_uid and target_id == dm_uid:
        return True
    return False


# ---------------------------------------------------------------------------
# Strict Public IP / URL Validation & Safe Redirect Download (SSRF Protection)
# ---------------------------------------------------------------------------

def _ip_obj_is_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return _ip_obj_is_public(ip.ipv4_mapped)
        if ip.sixtofour is not None and not _ip_obj_is_public(ip.sixtofour):
            return False
        if ip.teredo is not None:
            server_ip, client_ip = ip.teredo
            if not _ip_obj_is_public(server_ip) or not _ip_obj_is_public(client_ip):
                return False
    if (
        not ip.is_global
        or ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        return False
    return True


def is_public_ip(ip_str: str) -> bool:
    """Return True iff `ip_str` parses as a strictly globally routable public IP."""
    if isinstance(ip_str, (ipaddress.IPv4Address, ipaddress.IPv6Address)):
        return _ip_obj_is_public(ip_str)
    if not isinstance(ip_str, str):
        return False
    s = ip_str.strip().strip("[]")
    if not s or "%" in s:
        return False
    try:
        ip = ipaddress.ip_address(s)
    except ValueError:
        return False
    return _ip_obj_is_public(ip)


def is_safe_public_url(url: str, resolve_dns: bool = True) -> bool:
    """Validate that `url` is an http/https URL pointing strictly to a public host/IP."""
    if not isinstance(url, str):
        return False
    u = url.strip()
    if not u or len(u) > 2048:
        return False
    if any(ord(c) < 32 or ord(c) == 127 for c in u):
        return False
    try:
        parsed = urllib.parse.urlparse(u)
        port = parsed.port
    except ValueError:
        return False
    except Exception:
        return False

    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        return False

    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host:
        return False
    if host in _BLOCKED_HOSTNAMES or any(host.endswith(suf) for suf in _BLOCKED_HOST_SUFFIXES):
        return False

    # Reject literal IPs (IPv4, IPv6, IPv4-mapped IPv6) unless strictly public
    try:
        ipaddress.ip_address(host)
        return is_public_ip(host)
    except ValueError:
        pass

    # Catch non-standard IPv4 encodings (decimal 2130706433, hex 0x7f000001, octal, shorthand 127.1)
    try:
        packed = socket.inet_aton(host)
        dotted = socket.inet_ntoa(packed)
        return is_public_ip(dotted)
    except OSError:
        pass

    if resolve_dns:
        try:
            infos = socket.getaddrinfo(
                host,
                port or (443 if scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            )
            if not infos:
                return False
            for info in infos:
                addr = info[4][0]
                if not is_public_ip(addr):
                    return False
        except (socket.gaierror, OSError, ValueError):
            return False

    return True


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """HTTP redirect handler that validates redirect targets BEFORE issuing requests."""

    max_redirections = 5

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        resolved = urllib.parse.urljoin(req.full_url, str(newurl or "").strip())
        if not is_safe_public_url(resolved, resolve_dns=True):
            raise urllib.error.URLError("unsafe redirect blocked")
        return super().redirect_request(req, fp, code, msg, headers, resolved)


_SAFE_PUBLIC_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}),
    SafeRedirectHandler(),
)


def download_safe_public_bytes(
    url: str,
    max_bytes: int,
    timeout: float = 6.0,
    headers: dict | None = None,
) -> tuple[bytes | None, str]:
    """Download bytes from a verified public HTTP(S) URL with no proxy and safe redirects.

    Never falls back to any system proxy opener. Validates initial URL, every redirect
    target before connecting, final URL, and strict streaming byte cap.
    """
    if not is_safe_public_url(url, resolve_dns=True):
        return None, "链接不安全或指向内网/本机地址"

    req_headers = {"User-Agent": _DEFAULT_USER_AGENT}
    if isinstance(headers, dict):
        req_headers.update(headers)

    limit = max(1, int(max_bytes))
    try:
        req = urllib.request.Request(url.strip(), headers=req_headers, method="GET")
        with _SAFE_PUBLIC_OPENER.open(req, timeout=timeout) as resp:
            final_url = resp.geturl() or url
            if not is_safe_public_url(final_url, resolve_dns=True):
                return None, "重定向目标为非公网地址，已拦截"
            buf = bytearray()
            while True:
                remaining = limit + 1 - len(buf)
                chunk = resp.read(min(65536, max(1, remaining)))
                if not chunk:
                    break
                buf.extend(chunk)
                if len(buf) > limit:
                    return None, "下载内容体积超过上限"
            if not buf:
                return None, "下载内容为空"
            return bytes(buf), ""
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", None)
        return None, str(reason or exc)
    except Exception as exc:
        return None, str(exc)
