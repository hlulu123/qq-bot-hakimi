#!/usr/bin/env python3
"""Local HTTP + SSE Server for QQ Bot「哈基米」Web UI (Python 3.9 stdlib only).

Usage:
    python3 -m webui.server
    python3 webui/server.py --host 127.0.0.1 --port 8765
"""

import argparse
import base64
import hashlib
import json
import mimetypes
import os
import re
import signal
import socket
import sys
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

if __package__ in (None, ""):
    _PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _PROJECT_ROOT not in sys.path:
        sys.path.insert(0, _PROJECT_ROOT)
    from webui import napcat_api, services, stores  # noqa: E402
else:
    from . import napcat_api, services, stores  # noqa: E402

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
MAX_BODY_BYTES = 5 * 1024 * 1024  # 5 MB safety limit

_MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".mjs": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".txt": "text/plain; charset=utf-8",
}

_SHUTDOWN_EVENT = threading.Event()
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]"})
_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_READONLY_POST_PATHS = frozenset({
    "/api/tools/test-repeat",
    "/api/permissions/evaluate",
    "/api/napcat/debug/call",
    "/api/group-requests/policy/test",
})


def _build_csp_header():
    """Build a practical CSP allowing 'self' + static HTML inline theme script hashes."""
    script_src = "'self'"
    hashes = []
    seen_hashes = set()
    for fname in ("index.html", "join-monitor.html"):
        html_path = os.path.join(STATIC_DIR, fname)
        try:
            with open(html_path, "r", encoding="utf-8") as f:
                html = f.read()
            for m in re.finditer(r"<script(?:\s+[^>]*)?>([\s\S]*?)</script>", html, re.IGNORECASE):
                tag_open_end = html.find(">", m.start())
                tag_open = html[m.start():tag_open_end + 1] if tag_open_end != -1 else ""
                if re.search(r"\bsrc\s*=", tag_open, re.IGNORECASE):
                    continue
                body = m.group(1)
                if body and body.strip():
                    digest = base64.b64encode(hashlib.sha256(body.encode("utf-8")).digest()).decode("ascii")
                    token = f"'sha256-{digest}'"
                    if token not in seen_hashes:
                        seen_hashes.add(token)
                        hashes.append(token)
        except OSError:
            pass
    if hashes:
        script_src = "'self' " + " ".join(hashes)
    return (
        f"default-src 'self'; "
        f"script-src {script_src}; "
        f"style-src 'self' 'unsafe-inline'; "
        f"img-src 'self' data: blob: https: http:; "
        f"font-src 'self' data:; "
        f"connect-src 'self'; "
        f"frame-src 'self' http://127.0.0.1:* http://localhost:*; "
        f"object-src 'none'; "
        f"base-uri 'self'; "
        f"frame-ancestors 'self'"
    )


_CSP_HEADER = _build_csp_header()


def _parse_host_header(raw_host):
    """Parse HTTP Host header into (normalized_host, port_or_None); return (None, None) if invalid."""
    h = str(raw_host or "").strip().lower()
    if not h or any(c in h for c in (" ", "\t", "\r", "\n", "/", "\\", "@", "?", "#")):
        return None, None
    if h.startswith("["):
        end = h.find("]")
        if end == -1:
            return None, None
        host = h[: end + 1]
        rest = h[end + 1 :]
        if not rest:
            return host, None
        if not rest.startswith(":"):
            return None, None
        port_s = rest[1:]
        if not port_s.isdigit():
            return None, None
        port = int(port_s)
        return (host, port) if 1 <= port <= 65535 else (None, None)
    if ":" in h:
        host, port_s = h.rsplit(":", 1)
        if not host or ":" in host or not port_s.isdigit():
            return None, None
        port = int(port_s)
        return (host, port) if 1 <= port <= 65535 else (None, None)
    return h, None


def _parse_origin_url(raw_url):
    """Parse Origin or Referer URL into (scheme, normalized_host, port_or_None) or None."""
    s = str(raw_url or "").strip()
    if not s or s.lower() == "null":
        return None
    try:
        parsed = urllib.parse.urlsplit(s)
        scheme = (parsed.scheme or "").lower()
        if scheme not in ("http", "https"):
            return None
        hostname = (parsed.hostname or "").strip().lower()
        if not hostname:
            return None
        if ":" in hostname and not hostname.startswith("["):
            hostname = f"[{hostname}]"
        port = parsed.port
        return scheme, hostname, port
    except ValueError:
        return None


def _parse_bool_param(val, default=False):
    if val is None or val == "":
        return default
    return str(val).strip().lower() in ("1", "true", "yes", "on")


def _parse_int_param(val, default, min_val=None, max_val=None):
    try:
        num = int(str(val).strip())
    except (ValueError, TypeError):
        num = default
    if min_val is not None:
        num = max(min_val, num)
    if max_val is not None:
        num = min(max_val, num)
    return num


class HajimiHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = (os.name != "nt")

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError, ConnectionAbortedError)):
            return
        super().handle_error(request, client_address)


class HajimiRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def handle_one_request(self):
        try:
            return super().handle_one_request()
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            self.close_connection = True

    def finish(self):
        try:
            super().finish()
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError, OSError):
            pass

    def log_message(self, format, *args):
        # Keep console output concise; log non-2xx or mutating API calls
        try:
            status = str(args[1]) if len(args) > 1 else ""
            req_line = str(args[0]) if args else ""
        except Exception:
            status = ""
            req_line = ""
        if status.startswith(("4", "5")) or any(req_line.startswith(m) for m in ("POST ", "PUT ", "PATCH ", "DELETE ")):
            ts = time.strftime("%H:%M:%S")
            sys.stderr.write(f"[{ts}] [webui] {req_line} -> {status}\n")
            sys.stderr.flush()

    # ------------------------------------------------------------------
    # Localhost security guards (DNS-rebinding + CSRF)
    # ------------------------------------------------------------------

    def _allowed_hosts_and_port(self):
        allowed = set(_LOOPBACK_HOSTS)
        srv_addr = getattr(self.server, "server_address", None)
        srv_host = str(srv_addr[0]).strip().lower() if srv_addr and len(srv_addr) >= 1 else ""
        srv_port = int(srv_addr[1]) if srv_addr and len(srv_addr) >= 2 and srv_addr[1] else None
        if srv_host and srv_host not in ("0.0.0.0", "::", ""):
            if ":" in srv_host and not srv_host.startswith("["):
                allowed.add(f"[{srv_host}]")
            else:
                allowed.add(srv_host)
        return allowed, srv_port

    def _validate_host(self):
        """Reject DNS-rebinding or mismatched Host headers with 403 Forbidden."""
        raw_host = self.headers.get("Host")
        host, port = _parse_host_header(raw_host)
        allowed, srv_port = self._allowed_hosts_and_port()
        if not host or host not in allowed:
            self._send_error_json("拒绝访问：非法的 Host 请求头", status_code=HTTPStatus.FORBIDDEN)
            return None
        if port is not None and srv_port is not None and port != srv_port:
            self._send_error_json("拒绝访问：Host 端口不匹配", status_code=HTTPStatus.FORBIDDEN)
            return None
        return host, port, allowed, srv_port

    def _validate_csrf(self, method, path, host_info):
        """Protect mutating /api/* endpoints from cross-origin & simple-request CSRF."""
        if method not in _MUTATING_METHODS or not path.startswith("/api/"):
            return True

        req_host, _req_port, allowed_hosts, srv_port = host_info
        origin = self.headers.get("Origin")
        referer = self.headers.get("Referer")

        if origin is not None:
            parsed = _parse_origin_url(origin)
            if not parsed:
                self._send_error_json("拒绝跨站请求（非法 Origin）", status_code=HTTPStatus.FORBIDDEN)
                return False
            _, orig_host, orig_port = parsed
            if orig_host not in allowed_hosts and orig_host != req_host:
                self._send_error_json("拒绝跨站请求（Origin 不在允许列表）", status_code=HTTPStatus.FORBIDDEN)
                return False
            if orig_port is not None and srv_port is not None and orig_port != srv_port:
                self._send_error_json("拒绝跨站请求（Origin 端口不匹配）", status_code=HTTPStatus.FORBIDDEN)
                return False
        elif referer is not None and referer.strip():
            parsed = _parse_origin_url(referer)
            if not parsed:
                self._send_error_json("拒绝跨站请求（非法 Referer）", status_code=HTTPStatus.FORBIDDEN)
                return False
            _, ref_host, ref_port = parsed
            if ref_host not in allowed_hosts and ref_host != req_host:
                self._send_error_json("拒绝跨站请求（Referer 不在允许列表）", status_code=HTTPStatus.FORBIDDEN)
                return False
            if ref_port is not None and srv_port is not None and ref_port != srv_port:
                self._send_error_json("拒绝跨站请求（Referer 端口不匹配）", status_code=HTTPStatus.FORBIDDEN)
                return False

        # Require application/json Content-Type whenever a mutating /api/* request carries a body
        has_body = False
        if (self.headers.get("Transfer-Encoding") or "").strip():
            has_body = True
        else:
            raw_len = self.headers.get("Content-Length")
            if raw_len is not None and raw_len.strip() != "":
                try:
                    if int(raw_len.strip()) > 0:
                        has_body = True
                except ValueError:
                    self._send_error_json("无效的 Content-Length", status_code=HTTPStatus.BAD_REQUEST)
                    return False
        if has_body:
            ctype = (self.headers.get("Content-Type") or "").strip().lower()
            if not ctype.startswith("application/json"):
                self._send_error_json(
                    "拒绝请求：写入接口仅接受 application/json 请求体",
                    status_code=HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                )
                return False

        return True

    # ------------------------------------------------------------------
    # Response helpers
    # ------------------------------------------------------------------

    def _send_security_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", _CSP_HEADER)

    def _send_json(self, status_code, payload, close=None):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        should_close = bool(close) if close is not None else int(status_code) >= 400
        if should_close:
            self.close_connection = True
        try:
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            if should_close:
                self.send_header("Connection", "close")
            self._send_security_headers()
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _send_ok(self, data, status_code=HTTPStatus.OK):
        if getattr(self, "_invalidate_overview_before_ok", False):
            services.invalidate_overview_cache()
            self._invalidate_overview_before_ok = False
        self._send_json(status_code, {"ok": True, "data": data})

    def _send_error_json(self, message, status_code=HTTPStatus.BAD_REQUEST, extra=None):
        payload = {"ok": False, "error": str(message or "请求失败")}
        if isinstance(extra, dict):
            payload.update(extra)
        self._send_json(status_code, payload)

    def _read_json_body(self, allow_empty=False):
        raw_len = self.headers.get("Content-Length")
        if raw_len is None or raw_len.strip() == "":
            if allow_empty:
                return {}
            return {}
        try:
            length = int(raw_len)
        except ValueError:
            raise stores.StoreError("无效的 Content-Length", status_code=400)
        if length < 0 or length > MAX_BODY_BYTES:
            raise stores.StoreError(f"请求体过大（上限 {MAX_BODY_BYTES // (1024 * 1024)} MB）", status_code=413)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        if not raw.strip():
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            raise stores.StoreError("无效的 JSON 请求体", status_code=400)

    def _serve_static(self, rel_path):
        clean = urllib.parse.unquote(rel_path).lstrip("/")
        if not clean:
            clean = "index.html"
        if ".." in clean.split("/") or "\\" in clean:
            self._send_error_json("非法静态资源路径", status_code=403)
            return

        parts = [p for p in clean.split("/") if p]
        if not parts or any(p in (".", "..") or ":" in p for p in parts):
            self._send_error_json("非法静态资源路径", status_code=403)
            return

        target = os.path.join(STATIC_DIR, *parts)
        if not stores._is_confined(target, STATIC_DIR) or not os.path.isfile(target):
            self._send_error_json("静态文件不存在", status_code=404)
            return

        ext = os.path.splitext(target)[1].lower()
        ctype = _MIME_TYPES.get(ext) or mimetypes.guess_type(target)[0] or "application/octet-stream"
        try:
            with open(target, "rb") as f:
                data = f.read()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-cache")
            self._send_security_headers()
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    # ------------------------------------------------------------------
    # SSE Live Log Stream (`GET /api/logs/stream`)
    # ------------------------------------------------------------------

    def _handle_log_stream(self, qs):
        source = (qs.get("source", ["all"])[0] or "all").strip().lower()
        if source not in ("all", "cc-connect", "ws-guard"):
            source = "all"
        initial = _parse_int_param(qs.get("initial", ["20"])[0], 20, min_val=0, max_val=500)

        files = []
        if source in ("all", "cc-connect"):
            files.append(("cc-connect", services.CC_LOG_PATH, services.parse_cc_log_line))
        if source in ("all", "ws-guard"):
            files.append(("ws-guard", services.WS_GUARD_LOG_PATH, services.parse_ws_guard_log_line))

        try:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self._send_security_headers()
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

        def _write_sse(raw_str):
            self.wfile.write(raw_str.encode("utf-8"))
            self.wfile.flush()

        def _send_entry(entry):
            payload = json.dumps(entry, ensure_ascii=False)
            _write_sse(f"data: {payload}\n\n")

        # Initialize per-file byte offsets & buffered partial bytes
        states = {}
        initial_entries = []

        for src_name, fpath, parser in files:
            cur_size = 0
            if os.path.isfile(fpath):
                try:
                    cur_size = os.path.getsize(fpath)
                except OSError:
                    cur_size = 0
                if initial > 0:
                    for off, line in services._read_tail_lines_with_offsets(fpath, max_lines=initial):
                        initial_entries.append(parser(line, offset=off))
            states[src_name] = {
                "path": fpath,
                "parser": parser,
                "offset": cur_size,
                "partial": b"",
                "partial_offset": cur_size,
            }

        try:
            if initial_entries:
                if source == "all":
                    initial_entries.sort(key=services._entry_sort_ts)
                    initial_entries = initial_entries[-initial:]
                for entry in initial_entries:
                    _send_entry(entry)
            else:
                _write_sse(": connected\n\n")

            last_ping = time.monotonic()

            while not _SHUTDOWN_EVENT.is_set():
                sent_any = False
                for src_name, st in states.items():
                    fpath = st["path"]
                    if not os.path.isfile(fpath):
                        st["offset"] = 0
                        st["partial"] = b""
                        continue
                    try:
                        size = os.path.getsize(fpath)
                    except OSError:
                        continue
                    if size < st["offset"]:
                        # File truncated or rotated
                        st["offset"] = 0
                        st["partial"] = b""
                        st["partial_offset"] = 0
                    if size > st["offset"]:
                        try:
                            with open(fpath, "rb") as f:
                                f.seek(st["offset"])
                                chunk = f.read(min(size - st["offset"], 128 * 1024))
                        except OSError:
                            continue
                        if not chunk:
                            continue

                        if not st["partial"]:
                            st["partial_offset"] = st["offset"]
                        st["offset"] += len(chunk)
                        buf = st["partial"] + chunk
                        base_off = st["partial_offset"]

                        pos = 0
                        while True:
                            nl = buf.find(b"\n", pos)
                            if nl == -1:
                                st["partial"] = buf[pos:]
                                st["partial_offset"] = base_off + pos
                                break
                            line_bytes = buf[pos:nl]
                            line_off = base_off + pos
                            pos = nl + 1
                            text = line_bytes.decode("utf-8", errors="replace").rstrip("\r")
                            if text:
                                _send_entry(st["parser"](text, offset=line_off))
                                sent_any = True
                        if len(st["partial"]) > 64 * 1024:
                            # Guard against unbounded partial buffer on non-newline binary output
                            st["partial"] = b""

                now = time.monotonic()
                if sent_any:
                    last_ping = now
                elif (now - last_ping) >= 15.0:
                    _write_sse(": ping\n\n")
                    last_ping = now

                time.sleep(0.4)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    # ------------------------------------------------------------------
    # NapCat SSE Proxies (`GET /api/napcat/sys-stream`, `GET /api/napcat/log-stream`)
    # ------------------------------------------------------------------

    def _begin_sse(self):
        try:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self._send_security_headers()
            self.end_headers()
            return True
        except (BrokenPipeError, ConnectionResetError, OSError):
            return False

    def _write_sse_raw(self, text):
        self.wfile.write(text.encode("utf-8"))
        self.wfile.flush()

    def _handle_napcat_sys_stream(self):
        if not self._begin_sse():
            return
        try:
            self._write_sse_raw(": connected\n\n")
            while not _SHUTDOWN_EVENT.is_set():
                upstream = None
                try:
                    upstream = napcat_api.open_sse("/api/base/GetSysStatusRealTime", timeout=15.0)
                    while not _SHUTDOWN_EVENT.is_set():
                        try:
                            raw_line = upstream.readline()
                        except socket.timeout:
                            self._write_sse_raw(": ping\n\n")
                            continue
                        if not raw_line:
                            break
                        line = raw_line.decode("utf-8", errors="replace").strip()
                        if not line.startswith("data:"):
                            continue
                        payload_str = line[5:].strip()
                        if not payload_str:
                            continue
                        try:
                            parsed = json.loads(payload_str)
                            if isinstance(parsed, dict):
                                norm = napcat_api._normalize_sys_status(parsed)
                                napcat_api.update_sys_status_cache(parsed)
                                self._write_sse_raw(f"data: {json.dumps(norm, ensure_ascii=False)}\n\n")
                        except Exception:
                            continue
                except (BrokenPipeError, ConnectionResetError):
                    return
                except Exception as e:
                    err_payload = json.dumps({"error": str(e)}, ensure_ascii=False)
                    self._write_sse_raw(f"event: status_error\ndata: {err_payload}\n\n")
                    # Wait briefly before retry, checking shutdown
                    for _ in range(10):
                        if _SHUTDOWN_EVENT.is_set():
                            return
                        time.sleep(0.3)
                finally:
                    if upstream is not None:
                        try:
                            upstream.close()
                        except Exception:
                            pass
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    def _handle_napcat_log_stream(self):
        if not self._begin_sse():
            return
        ansi_re = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])")
        lvl_re = re.compile(r"\[(info|warn|warning|error|debug|fatal)\]", re.IGNORECASE)
        try:
            self._write_sse_raw(": connected\n\n")
            while not _SHUTDOWN_EVENT.is_set():
                upstream = None
                try:
                    upstream = napcat_api.open_sse("/api/Log/GetLogRealTime", timeout=15.0)
                    while not _SHUTDOWN_EVENT.is_set():
                        try:
                            raw_line = upstream.readline()
                        except socket.timeout:
                            self._write_sse_raw(": ping\n\n")
                            continue
                        if not raw_line:
                            break
                        line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                        if not line.startswith("data:"):
                            continue
                        raw_msg = line[5:]
                        if raw_msg.startswith(" "):
                            raw_msg = raw_msg[1:]
                        clean_msg = ansi_re.sub("", raw_msg).strip()
                        if not clean_msg:
                            continue
                        m_lvl = lvl_re.search(clean_msg)
                        level = (m_lvl.group(1).upper() if m_lvl else "INFO")
                        if level == "WARNING":
                            level = "WARN"
                        entry = {
                            "ts": time.strftime("%H:%M:%S"),
                            "level": level,
                            "text": clean_msg,
                        }
                        self._write_sse_raw(f"data: {json.dumps(entry, ensure_ascii=False)}\n\n")
                except (BrokenPipeError, ConnectionResetError):
                    return
                except Exception as e:
                    err_payload = json.dumps({"error": str(e)}, ensure_ascii=False)
                    self._write_sse_raw(f"event: log_error\ndata: {err_payload}\n\n")
                    for _ in range(10):
                        if _SHUTDOWN_EVENT.is_set():
                            return
                        time.sleep(0.3)
                finally:
                    if upstream is not None:
                        try:
                            upstream.close()
                        except Exception:
                            pass
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    # ------------------------------------------------------------------
    # Group Join Requests Live SSE Stream (`GET /api/group-requests/stream`)
    # ------------------------------------------------------------------

    def _handle_group_requests_stream(self, qs):
        gr = services._get_group_requests_mod()
        self.close_connection = True

        raw_since = (qs.get("since", [""])[0] or "").strip()
        if not raw_since:
            raw_since = (self.headers.get("Last-Event-ID") or "").strip()
        since_seq = None
        if raw_since:
            try:
                since_seq = max(0, int(raw_since))
            except ValueError:
                since_seq = None

        if not self._begin_sse():
            return

        def _parse_event_payload(ev):
            val = ev.get("payload")
            if val is None and "payload_json" in ev:
                val = ev.get("payload_json")
            if isinstance(val, dict):
                return val
            if isinstance(val, str) and val.strip():
                try:
                    parsed_p = json.loads(val)
                    return parsed_p if isinstance(parsed_p, dict) else {}
                except Exception:
                    return {}
            return {}

        try:
            ctx = services._build_group_requests_context()
            group_map = dict(ctx.get("group_map") or {})
            role_map = dict(ctx.get("role_map") or {})
            last_ctx_refresh = time.monotonic()

            try:
                list_res = gr.list_requests(status="all", limit=1, offset=0) or {}
            except Exception:
                list_res = {}
            try:
                summary = gr.get_summary() or {}
            except Exception:
                summary = {}

            counts = list_res.get("counts") or {
                "pending": 0,
                "approved": 0,
                "rejected": 0,
                "failed": 0,
                "total": 0,
            }
            if list_res.get("latest_seq") is not None:
                latest_seq = int(list_res.get("latest_seq") or 0)
            else:
                try:
                    latest_seq = int(gr.get_latest_seq() or 0)
                except Exception:
                    latest_seq = 0

            snapshot = {
                "counts": counts,
                "latest_seq": latest_seq,
                "napcat_online": bool(ctx.get("napcat_online")),
                "ws_guard_running": bool(ctx.get("ws_guard_running")),
                "policy_enabled": bool(summary.get("policy_enabled", True)),
            }
            self._write_sse_raw(
                f"event: snapshot\ndata: {json.dumps(snapshot, ensure_ascii=False)}\n\n"
            )

            last_seq = since_seq if since_seq is not None else latest_seq
            last_ping = time.monotonic()

            while not _SHUTDOWN_EVENT.is_set():
                now = time.monotonic()
                if (now - last_ctx_refresh) >= 30.0:
                    try:
                        ctx = services._build_group_requests_context()
                        group_map = dict(ctx.get("group_map") or {})
                        role_map = dict(ctx.get("role_map") or {})
                    except Exception:
                        pass
                    last_ctx_refresh = now

                sent_any = False
                try:
                    events, new_seq = gr.get_events_since(last_seq, limit=50)
                except Exception:
                    events, new_seq = [], last_seq

                if events:
                    try:
                        cur_counts = (
                            (gr.list_requests(status="all", limit=1, offset=0) or {}).get("counts")
                            or counts
                        )
                    except Exception:
                        cur_counts = counts
                    counts = cur_counts

                    for ev in events:
                        if not isinstance(ev, dict):
                            continue
                        ev_seq = int(ev.get("seq") or 0)
                        req_obj = None
                        req_lookup = ev.get("request_id")
                        if req_lookup is None or str(req_lookup).strip() == "":
                            req_lookup = ev.get("flag")
                        if req_lookup is not None and str(req_lookup).strip() != "":
                            try:
                                req_obj = gr.get_request(req_lookup)
                            except Exception:
                                req_obj = None
                        if isinstance(req_obj, dict):
                            req_obj = services._enrich_group_request_item(req_obj, group_map, role_map)

                        ev_out = {
                            "seq": ev_seq,
                            "event_type": str(ev.get("event_type") or ""),
                            "request_id": ev.get("request_id"),
                            "flag": str(ev.get("flag") or ""),
                            "group_id": str(ev.get("group_id") or ""),
                            "payload": _parse_event_payload(ev),
                            "created_at": ev.get("created_at"),
                            "request": req_obj,
                            "counts": counts,
                        }
                        self._write_sse_raw(
                            f"id: {ev_seq}\nevent: request_event\ndata: {json.dumps(ev_out, ensure_ascii=False)}\n\n"
                        )
                        if ev_seq > last_seq:
                            last_seq = ev_seq
                        sent_any = True

                    if new_seq is not None and int(new_seq) > last_seq:
                        last_seq = int(new_seq)
                elif new_seq is not None and int(new_seq) < last_seq:
                    # DB reset or truncated: realign cursor so future events are not missed
                    last_seq = int(new_seq)

                now = time.monotonic()
                if sent_any:
                    last_ping = now
                elif (now - last_ping) >= 15.0:
                    self._write_sse_raw(": ping\n\n")
                    last_ping = now

                _SHUTDOWN_EVENT.wait(0.4)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            return

    # ------------------------------------------------------------------
    # Route dispatch
    # ------------------------------------------------------------------

    def _dispatch(self, method):
        self._invalidate_overview_before_ok = False
        host_info = self._validate_host()
        if host_info is None:
            return

        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path or "/"
        qs = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)

        # Static routes (GET / HEAD)
        if method == "GET":
            if path == "/" or path == "/index.html":
                return self._serve_static("index.html")
            if path == "/join-monitor" or path == "/join-monitor/":
                return self._serve_static("join-monitor.html")
            if path.startswith("/static/"):
                return self._serve_static(path[len("/static/"):])
            if path == "/favicon.ico":
                ico = os.path.join(STATIC_DIR, "favicon.ico")
                if os.path.isfile(ico):
                    return self._serve_static("favicon.ico")
                self.send_response(HTTPStatus.NO_CONTENT)
                self.send_header("Content-Length", "0")
                self._send_security_headers()
                self.end_headers()
                return

        if not path.startswith("/api/"):
            return self._send_error_json("接口路径不存在", status_code=404)

        if not self._validate_csrf(method, path, host_info):
            return

        self._invalidate_overview_before_ok = (
            method in _MUTATING_METHODS and path not in _READONLY_POST_PATHS
        )

        try:
            # --- Overview & Analytics ---
            if method == "GET" and path == "/api/overview":
                force = _parse_bool_param(qs.get("refresh", [""])[0], False)
                return self._send_ok(services.get_overview(force=force))

            if method == "GET" and path == "/api/analytics":
                return self._send_ok(services.get_analytics())

            # --- Token Usage Analytics ---
            if method == "GET" and path == "/api/tokens":
                rng = qs.get("range", ["24h"])[0] or "24h"
                flt = qs.get("filter", ["all"])[0] or "all"
                q = qs.get("q", [""])[0]
                sort_by = qs.get("sort", ["total_tokens"])[0] or "total_tokens"
                return self._send_ok(
                    services.get_token_usage(
                        range_key=rng,
                        chat_filter=flt,
                        query=q,
                        sort_by=sort_by,
                    )
                )

            if method == "POST" and path == "/api/tokens/sync":
                body = self._read_json_body(allow_empty=True)
                force = bool(body.get("force", False)) if isinstance(body, dict) else False
                return self._send_ok(services.sync_token_usage_logs(force=force))

            if method == "GET" and path.startswith("/api/tokens/chat/"):
                skey = urllib.parse.unquote(path[len("/api/tokens/chat/"):]).strip("/")
                rng = qs.get("range", ["24h"])[0] or "24h"
                limit = _parse_int_param(qs.get("limit", ["50"])[0], 50, min_val=1, max_val=200)
                return self._send_ok(
                    services.get_token_chat_detail(skey, range_key=rng, limit=limit)
                )

            # --- Service Control ---
            if method == "POST" and path.startswith("/api/control/"):
                action = urllib.parse.unquote(path[len("/api/control/"):]).strip("/")
                self._read_json_body(allow_empty=True)
                return self._send_ok(services.run_control(action))

            # --- Sessions & NapCat History ---
            if method == "GET" and path == "/api/sessions":
                return self._send_ok(services.list_sessions())

            if method == "POST" and path == "/api/sessions/send":
                body = self._read_json_body()
                return self._send_ok(services.send_session_message(body))

            if path.startswith("/api/sessions/"):
                sid = urllib.parse.unquote(path[len("/api/sessions/"):]).strip("/")
                if method == "GET":
                    limit = _parse_int_param(qs.get("limit", ["120"])[0], 120, min_val=1, max_val=1000)
                    hide_silent = _parse_bool_param(qs.get("hide_silent", [""])[0], False)
                    q = qs.get("q", [""])[0]
                    return self._send_ok(services.get_session_detail(sid, limit=limit, hide_silent=hide_silent, query=q))
                if method == "DELETE":
                    return self._send_ok(services.delete_session(sid))

            if method == "GET" and path == "/api/napcat/group-history":
                gid = qs.get("group_id", [""])[0]
                count = _parse_int_param(qs.get("count", ["30"])[0], 30, min_val=1, max_val=100)
                return self._send_ok(services.get_napcat_group_history(gid, count=count))

            if method == "GET" and path == "/api/napcat/info":
                force = _parse_bool_param(qs.get("refresh", [""])[0], False)
                return self._send_ok(services.get_napcat_info(force=force))

            if method == "GET" and path == "/api/napcat/sys-stream":
                return self._handle_napcat_sys_stream()

            if method == "GET" and path == "/api/napcat/log-stream":
                return self._handle_napcat_log_stream()

            if method == "GET" and path == "/api/napcat/qrcode":
                return self._send_ok(services.get_napcat_qrcode())

            if method == "POST" and path == "/api/napcat/login/refresh-qr":
                self._read_json_body(allow_empty=True)
                return self._send_ok(services.refresh_napcat_qrcode())

            if method == "POST" and path == "/api/napcat/login/quick":
                body = self._read_json_body()
                if not isinstance(body, dict):
                    raise stores.StoreError("无效的快捷登录请求")
                return self._send_ok(services.napcat_quick_login(body.get("uin", "")))

            if method == "POST" and path == "/api/napcat/login/auto-account":
                body = self._read_json_body()
                if not isinstance(body, dict):
                    raise stores.StoreError("无效的自动登录设置请求")
                return self._send_ok(services.napcat_set_auto_login_qq(body.get("uin", "")))

            if method == "PUT" and path == "/api/napcat/ob11-config":
                body = self._read_json_body()
                return self._send_ok(services.update_napcat_ob11_config(body))

            if method == "PUT" and path == "/api/napcat/core-config":
                body = self._read_json_body()
                return self._send_ok(services.update_napcat_core_config(body))

            if method == "POST" and path == "/api/napcat/plugins/status":
                body = self._read_json_body()
                if not isinstance(body, dict):
                    raise stores.StoreError("无效的插件状态更新请求")
                return self._send_ok(
                    services.set_napcat_plugin_status(body.get("id", ""), bool(body.get("enable", False)))
                )

            if method == "GET" and path == "/api/napcat/log-files":
                return self._send_ok(services.get_napcat_log_files())

            if method == "GET" and path == "/api/napcat/log-file":
                log_id = qs.get("id", [""])[0]
                return self._send_ok(services.get_napcat_log_file(log_id))

            if method == "GET" and path == "/api/napcat/debug/schemas":
                return self._send_ok(services.get_napcat_debug_schemas())

            if method == "POST" and path == "/api/napcat/debug/call":
                body = self._read_json_body()
                if not isinstance(body, dict):
                    raise stores.StoreError("无效的接口调试请求")
                return self._send_ok(services.call_napcat_debug_api(body.get("action", ""), body.get("params")))

            if method == "POST" and path == "/api/napcat/process/restart":
                self._read_json_body(allow_empty=True)
                return self._send_ok(services.restart_napcat_process())

            if method == "PUT" and path == "/api/napcat/whitelist":
                body = self._read_json_body()
                if not isinstance(body, dict):
                    raise stores.StoreError("无效的群聊白名单更新请求")
                enabled = body.get("enabled", False)
                group_ids = body.get("group_ids", [])
                restart_ws = bool(body.get("restart_ws_guard", False))
                return self._send_ok(
                    services.update_group_whitelist_action(
                        enabled=enabled,
                        group_ids=group_ids,
                        restart_ws_guard=restart_ws,
                    )
                )

            if method == "GET" and path == "/api/napcat/logs":
                lines = _parse_int_param(qs.get("lines", ["120"])[0], 120, min_val=20, max_val=500)
                return self._send_ok(services.get_napcat_logs(lines=lines))

            if method == "POST" and path.startswith("/api/napcat/container/"):
                action = urllib.parse.unquote(path[len("/api/napcat/container/"):]).strip("/")
                self._read_json_body(allow_empty=True)
                return self._send_ok(services.control_napcat_container(action))

            # --- Categorized Control Workbench & Presets ---
            if method == "GET" and path == "/api/workbench":
                return self._send_ok(stores.get_workbench_state())

            if method == "PUT" and path == "/api/workbench":
                body = self._read_json_body()
                if not isinstance(body, dict):
                    raise stores.StoreError("无效的工作台更新请求")
                env_updates = body.get("env_updates") if "env_updates" in body else body.get("env", {})
                toml_updates = body.get("toml_updates") if "toml_updates" in body else body.get("toml", {})
                apply_restart = str(body.get("apply_restart") or "").strip().lower()

                res = stores.update_workbench(env_updates=env_updates, toml_updates=toml_updates)
                control_action = ""
                if apply_restart == "auto":
                    if res.get("restart_scope") == "ws-guard":
                        control_action = "restart-ws-guard"
                    elif res.get("restart_scope") == "full":
                        control_action = "restart-bot"
                elif apply_restart in ("ws-guard", "restart-ws-guard"):
                    control_action = "restart-ws-guard"
                elif apply_restart in ("reload", "reload-cc-connect"):
                    control_action = "reload-cc-connect"
                elif apply_restart in ("full", "restart-bot"):
                    control_action = "restart-bot"

                control_result = None
                if control_action:
                    try:
                        control_result = services.run_control(control_action)
                    except stores.StoreError as ce:
                        control_result = {
                            "action": control_action,
                            "ok": False,
                            "error": ce.message,
                            "message": ce.message,
                        }
                res["control_result"] = control_result
                return self._send_ok(res)

            if method == "POST" and path == "/api/workbench/presets":
                body = self._read_json_body()
                return self._send_ok(stores.save_custom_preset(body))

            if method == "DELETE" and path.startswith("/api/workbench/presets/"):
                preset_id = urllib.parse.unquote(path[len("/api/workbench/presets/"):]).strip("/")
                return self._send_ok(stores.delete_custom_preset(preset_id))

            # --- Group Member Join Requests (Live Monitor & Approval) ---
            if method == "GET" and path == "/api/group-requests/stream":
                return self._handle_group_requests_stream(qs)

            if method == "GET" and path == "/api/group-requests":
                status = qs.get("status", ["all"])[0] or "all"
                group_id = qs.get("group_id", [""])[0]
                sub_type = qs.get("sub_type", [""])[0]
                q = qs.get("q", [""])[0]
                limit = _parse_int_param(qs.get("limit", ["100"])[0], 100, min_val=1, max_val=500)
                offset = _parse_int_param(qs.get("offset", ["0"])[0], 0, min_val=0)
                sync_flag = _parse_bool_param(qs.get("sync", [""])[0], False)
                return self._send_ok(
                    services.list_group_requests(
                        status=status,
                        group_id=group_id,
                        sub_type=sub_type,
                        query=q,
                        limit=limit,
                        offset=offset,
                        sync=sync_flag,
                    )
                )

            if method == "GET" and path == "/api/group-requests/policy":
                return self._send_ok(services.get_group_requests_policy())

            if method == "PUT" and path == "/api/group-requests/policy":
                body = self._read_json_body()
                return self._send_ok(services.update_group_requests_policy(body))

            if method == "POST" and path == "/api/group-requests/policy/test":
                body = self._read_json_body()
                return self._send_ok(services.test_group_requests_policy(body))

            if method == "POST" and path == "/api/group-requests/sync":
                body = self._read_json_body(allow_empty=True)
                count = body.get("count", 50) if isinstance(body, dict) else 50
                return self._send_ok(services.sync_group_requests(count=count))

            if method == "POST" and path == "/api/group-requests/batch":
                body = self._read_json_body()
                if not isinstance(body, dict):
                    raise stores.StoreError("无效的批量审批请求")
                return self._send_ok(
                    services.batch_group_requests(
                        body.get("ids"),
                        action=body.get("action", "approve"),
                        reason=body.get("reason", ""),
                    )
                )

            if method == "POST" and path == "/api/group-requests/clear":
                body = self._read_json_body(allow_empty=True)
                before_days = body.get("before_days", 0) if isinstance(body, dict) else 0
                return self._send_ok(services.clear_group_requests(before_days=before_days))

            if method == "POST" and path.startswith("/api/group-requests/"):
                rest = path[len("/api/group-requests/"):].strip("/")
                parts = [urllib.parse.unquote(p) for p in rest.split("/") if p]
                if len(parts) == 2 and parts[0] not in ("policy", "sync", "batch", "clear", "stream"):
                    req_id, action = parts[0], parts[1]
                    if action == "approve":
                        body = self._read_json_body(allow_empty=True)
                        send_welcome = bool(body.get("send_welcome", True)) if isinstance(body, dict) else True
                        return self._send_ok(
                            services.approve_group_request(req_id, send_welcome=send_welcome)
                        )
                    if action == "reject":
                        body = self._read_json_body(allow_empty=True)
                        reason = body.get("reason", "") if isinstance(body, dict) else ""
                        return self._send_ok(
                            services.reject_group_request(req_id, reason=reason)
                        )

            # --- Bot Permissions (hot-reloaded) ---
            if method == "GET" and path == "/api/permissions":
                force = _parse_bool_param(qs.get("refresh", [""])[0], False)
                return self._send_ok(services.get_permissions_overview(force_groups=force))

            if method == "PUT" and path == "/api/permissions":
                body = self._read_json_body()
                stores.update_permissions(body)
                return self._send_ok(services.get_permissions_overview())

            if method == "POST" and path == "/api/permissions/template":
                body = self._read_json_body()
                if not isinstance(body, dict):
                    raise stores.StoreError("无效的模板请求")
                stores.apply_permissions_template(
                    template_id=body.get("template_id"),
                    keep_groups=bool(body.get("keep_groups", True)),
                    keep_limits=bool(body.get("keep_limits", True)),
                )
                return self._send_ok(services.get_permissions_overview())

            if method == "POST" and path == "/api/permissions/reset":
                body = self._read_json_body(allow_empty=True)
                keep_groups = bool(body.get("keep_groups", False)) if isinstance(body, dict) else False
                stores.reset_permissions(keep_groups=keep_groups)
                return self._send_ok(services.get_permissions_overview())

            if method == "POST" and path == "/api/permissions/evaluate":
                body = self._read_json_body()
                return self._send_ok(stores.evaluate_permission(body))

            if path.startswith("/api/permissions/groups/"):
                gid = urllib.parse.unquote(path[len("/api/permissions/groups/"):]).strip("/")
                if method == "PUT":
                    body = self._read_json_body()
                    stores.update_group_permissions(gid, body)
                    return self._send_ok(services.get_permissions_overview())
                if method == "DELETE":
                    stores.delete_group_permissions(gid)
                    return self._send_ok(services.get_permissions_overview())

            # --- Modular Plugins (hot-reloaded) ---
            if method == "GET" and path == "/api/plugins":
                refresh = _parse_bool_param(qs.get("refresh", [""])[0], False)
                return self._send_ok(stores.get_plugins_state(force=refresh))

            if method == "PUT" and path == "/api/plugins":
                body = self._read_json_body()
                return self._send_ok(stores.update_plugins(body))

            if method == "POST" and path == "/api/plugins/preset":
                body = self._read_json_body()
                if not isinstance(body, dict):
                    raise stores.StoreError("无效的插件预设请求")
                return self._send_ok(stores.apply_plugin_preset(body.get("preset_id", "")))

            if method == "POST" and path == "/api/plugins/reset":
                self._read_json_body(allow_empty=True)
                return self._send_ok(stores.reset_plugins())

            if method == "POST" and path == "/api/plugins/rescan":
                self._read_json_body(allow_empty=True)
                return self._send_ok(stores.rescan_plugins())

            if method == "PUT" and path.startswith("/api/plugins/"):
                plugin_id = urllib.parse.unquote(path[len("/api/plugins/"):]).strip("/")
                if "/" not in plugin_id and plugin_id not in ("", "preset", "reset", "rescan"):
                    body = self._read_json_body()
                    return self._send_ok(stores.update_single_plugin(plugin_id, body))

            # --- Persona ---
            if method == "GET" and path == "/api/persona":
                return self._send_ok(stores.get_persona())

            if method == "PUT" and path == "/api/persona/gemini":
                body = self._read_json_body()
                content = body.get("content")
                create_backup = bool(body.get("create_backup", body.get("backup", True)))
                sync_sandbox = bool(body.get("sync_sandbox", True))
                return self._send_ok(
                    stores.save_gemini(content, create_backup=create_backup, sync_to_sandbox=sync_sandbox)
                )

            if method == "PUT" and path == "/api/persona/character-card":
                body = self._read_json_body()
                content = body.get("content")
                create_backup = bool(body.get("create_backup", body.get("backup", True)))
                return self._send_ok(stores.save_character_card(content, create_backup=create_backup))

            if method == "GET" and path.startswith("/api/persona/backup/"):
                bid = urllib.parse.unquote(path[len("/api/persona/backup/"):]).strip("/")
                target = qs.get("target", ["gemini"])[0] or "gemini"
                return self._send_ok(stores.get_persona_backup_diff(bid, target=target))

            # --- User & Group Memories (v2 SQLite engine) ---
            if method == "GET" and path == "/api/memories":
                q = qs.get("q", [""])[0]
                flt = qs.get("filter", ["all"])[0] or "all"
                gid = qs.get("group_id", [""])[0]
                sort_by = qs.get("sort", ["last_seen"])[0] or "last_seen"
                gmap = services.napcat_get_group_list().get("group_map", {})
                return self._send_ok(
                    stores.list_memories(q=q, filter_mode=flt, group_id=gid, sort_by=sort_by, group_map=gmap)
                )

            if path.startswith("/api/memories/"):
                rest = path[len("/api/memories/"):].strip("/")
                parts = [urllib.parse.unquote(p) for p in rest.split("/") if p]

                # Exact fixed-prefix routes MUST take precedence over <uid>
                if parts == ["groups"] and method == "GET":
                    gmap = services.napcat_get_group_list().get("group_map", {})
                    return self._send_ok(stores.list_group_memories(group_map=gmap))

                if len(parts) == 2 and parts[0] == "groups":
                    gid = parts[1]
                    if method == "GET":
                        gmap = services.napcat_get_group_list().get("group_map", {})
                        return self._send_ok(stores.get_group_memory(gid, group_map=gmap))
                    if method == "PUT":
                        body = self._read_json_body()
                        gmap = services.napcat_get_group_list().get("group_map", {})
                        return self._send_ok(stores.update_group_memory(gid, body, group_map=gmap))

                if parts == ["conflicts"] and method == "GET":
                    return self._send_ok(stores.list_memory_conflicts())

                if len(parts) == 3 and parts[0] == "conflicts" and parts[2] == "resolve" and method == "POST":
                    mem_id = parts[1]
                    body = self._read_json_body()
                    if not isinstance(body, dict):
                        raise stores.StoreError("请求体必须是 JSON 对象")
                    return self._send_ok(
                        stores.resolve_memory_conflict(
                            mem_id,
                            body.get("action", ""),
                            partner_id=body.get("partner_id", ""),
                        )
                    )

                if parts == ["maintenance", "flush"] and method == "POST":
                    body = self._read_json_body(allow_empty=True)
                    if not isinstance(body, dict):
                        body = {}
                    try:
                        idle_mins = float(body.get("idle_mins", 0.0))
                    except (TypeError, ValueError):
                        raise stores.StoreError("idle_mins 必须是数字")
                    return self._send_ok(
                        stores.run_memory_maintenance_flush(
                            async_run=bool(body.get("async", True)),
                            idle_mins=idle_mins,
                        )
                    )

                if parts == ["maintenance", "sleep"] and method == "POST":
                    body = self._read_json_body(allow_empty=True)
                    if not isinstance(body, dict):
                        body = {}
                    return self._send_ok(
                        stores.run_memory_maintenance_sleep(
                            async_run=bool(body.get("async", True)),
                            force=bool(body.get("force", True)),
                            skip_llm=bool(body.get("skip_llm", False)),
                        )
                    )

                if len(parts) == 3 and parts[0] == "items" and parts[2] == "restore" and method == "POST":
                    mem_id = parts[1]
                    self._read_json_body(allow_empty=True)
                    return self._send_ok(stores.restore_memory_item(mem_id))

                if len(parts) == 2 and parts[1] == "traces" and method == "GET":
                    uid = parts[0]
                    limit = _parse_int_param(qs.get("limit", ["20"])[0], 20, min_val=1, max_val=100)
                    return self._send_ok(stores.get_user_memory_traces(uid, limit=limit))

                if len(parts) == 2 and parts[1] == "consolidate" and method == "POST":
                    uid = parts[0]
                    body = self._read_json_body(allow_empty=True)
                    scope = body.get("scope", "public") if isinstance(body, dict) else "public"
                    return self._send_ok(stores.trigger_consolidation(uid, scope=scope))

                if len(parts) == 1 and parts[0] not in ("groups", "conflicts", "maintenance", "items"):
                    uid = parts[0]
                    if method == "GET":
                        gmap = services.napcat_get_group_list().get("group_map", {})
                        return self._send_ok(stores.get_memory(uid, group_map=gmap))
                    if method == "PUT":
                        body = self._read_json_body()
                        gmap = services.napcat_get_group_list().get("group_map", {})
                        return self._send_ok(stores.update_memory(uid, body, group_map=gmap))
                    if method == "DELETE":
                        return self._send_ok(stores.delete_memory(uid))

            # --- Config & Tools ---
            if method == "GET" and path == "/api/config":
                return self._send_ok(stores.get_config())

            if method == "PUT" and path == "/api/config/env":
                body = self._read_json_body()
                if isinstance(body, dict) and "updates" in body and isinstance(body["updates"], dict):
                    updates = body["updates"]
                    sync_toml = bool(body.get("sync_toml", True))
                elif isinstance(body, dict):
                    sync_toml = bool(body.get("sync_toml", True))
                    updates = {k: v for k, v in body.items() if k != "sync_toml"}
                else:
                    raise stores.StoreError("无效的配置更新请求")
                return self._send_ok(stores.update_bot_env(updates, sync_toml=sync_toml))

            if method == "POST" and path == "/api/config/sync-toml":
                self._read_json_body(allow_empty=True)
                return self._send_ok(stores.sync_env_to_toml())

            if method == "POST" and path == "/api/tools/test-repeat":
                body = self._read_json_body()
                text = body.get("text", "") if isinstance(body, dict) else ""
                return self._send_ok(stores.test_repeat_text(text))

            # --- Logs ---
            if method == "GET" and path == "/api/logs/tail":
                source = qs.get("source", ["all"])[0] or "all"
                lines = _parse_int_param(qs.get("lines", ["150"])[0], 150, min_val=1, max_val=2000)
                level = qs.get("level", [""])[0]
                q = qs.get("q", [""])[0]
                return self._send_ok(services.tail_logs(source=source, lines=lines, level=level, query=q))

            if method == "GET" and path == "/api/logs/stream":
                return self._handle_log_stream(qs)

            # --- Workspace & Backups ---
            if method == "GET" and path == "/api/workspace":
                return self._send_ok(stores.list_workspace())

            if method == "GET" and path == "/api/workspace/file":
                rel_path = qs.get("path", [""])[0]
                root = qs.get("root", [""])[0]
                return self._send_ok(stores.read_workspace_file(rel_path, root=root))

            return self._send_error_json(f"未找到路由：{method} {path}", status_code=404)

        except stores.SensitiveMemoryError as e:
            matched_preview = "、".join(sorted({m["matched"] for m in e.matches})[:5])
            msg = f"{e.message}（命中：{matched_preview}）" if matched_preview else e.message
            return self._send_error_json(msg, status_code=e.status_code, extra={"matches": e.matches})
        except stores.StoreError as e:
            return self._send_error_json(e.message, status_code=e.status_code, extra=e.extra)
        except Exception as e:
            return self._send_error_json(f"服务器内部错误：{e}", status_code=500)

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_PATCH(self):
        self._dispatch("PATCH")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def do_OPTIONS(self):
        if self._validate_host() is None:
            return
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Allow", "GET, POST, PUT, PATCH, DELETE, OPTIONS")
        self.send_header("Content-Length", "0")
        self._send_security_headers()
        self.end_headers()


def run_server(host=None, port=None):
    bind_host = (host or os.environ.get("WEBUI_HOST") or "127.0.0.1").strip() or "127.0.0.1"
    bind_port = int(port if port is not None else (os.environ.get("WEBUI_PORT") or 8765))

    server = HajimiHTTPServer((bind_host, bind_port), HajimiRequestHandler)

    def _handle_signal(signum, _frame):
        _SHUTDOWN_EVENT.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGINT, _handle_signal)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, _handle_signal)
        if hasattr(signal, "SIGBREAK"):
            signal.signal(signal.SIGBREAK, _handle_signal)

    print("==========================================", flush=True)
    print("  QQ 机器人「哈基米」本地 Web 控制台", flush=True)
    print(f"  访问地址: http://{bind_host}:{bind_port}", flush=True)
    print(f"  项目目录: {stores.PROJECT_DIR}", flush=True)
    print("  仅监听本机回环地址，按 Ctrl+C 停止", flush=True)
    print("==========================================", flush=True)

    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        _SHUTDOWN_EVENT.set()
        server.server_close()


def _reconfigure_stdio():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def main():
    _reconfigure_stdio()
    parser = argparse.ArgumentParser(description="QQ Bot「哈基米」本地 Web UI 后端")
    parser.add_argument(
        "--host",
        default=os.environ.get("WEBUI_HOST", "127.0.0.1"),
        help="监听地址（默认 127.0.0.1）",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("WEBUI_PORT", "8765")),
        help="监听端口（默认 8765）",
    )
    args = parser.parse_args()
    run_server(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
