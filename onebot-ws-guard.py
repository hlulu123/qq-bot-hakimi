#!/usr/bin/env python3
"""Local OneBot v11 WebSocket Guard Proxy between cc-connect and NapCat.

Solves cc-connect v1.5.3 busy-queue deadlock & group spam when `require_mention = false`:
1. Per-group in-flight gate: ensures at most 1 message per group is active in
   cc-connect at a time, completely preventing `message queued for busy session`
   and the resulting 15s `send_group_msg` readLoop deadlock!
2. Outbound spam blocker: strips/acks cc-connect's hardcoded busy notice
   ("📬 消息已收到，将在当前任务完成后处理。") so it NEVER leaks into QQ groups.
3. Smart pre-filtering before waking cc-connect:
   - Drops messages from self (`BOT_QQ_ID`) and official/ignored bots (`Q群管家`, etc.).
   - Drops ambient group messages that explicitly `@` someone else without `@`ing us
     (while still letting NapCat keep full group history & recording user memory).
   - While a group is busy: preserves priority queue for direct @/reply/name-calls
     and coalesces rapid-fire ambient chatter to the latest single message.
4. Receives instant turn-completion UDP notifications from `agy-acp-wrapper.py`
   (plus safety TTL fallback) to immediately release per-group busy gates.
5. Long outbound replies are rewritten into a merged-forward "chat record" card
   (send_group_forward_msg / send_private_forward_msg) so they don't flood the
   chat. The echo is kept, so cc-connect sees a normal reply; if NapCat rejects
   the forward card, the original plain message is sent instead.
6. Repeat chains (复读, see repeat_chain.py): once at least two people have
   posted the same message REPEAT_MIN_COUNT times in a row, that message goes to
   the model even while lurking is paced, and the model decides whether to join.
   Its answer FOLLOW_REPEAT is swapped here for an exact copy of the message.
   Other messages in the chain aren't dispatched at all.
"""

import asyncio
import base64
import collections
import copy
import hashlib
import json
import os
import queue
import re
import socket
import struct
import sys
import threading
import time
from urllib.parse import urlparse

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

import bot_env  # noqa: E402,F401  (loads bot.env before any setting is read)
import bot_permissions  # noqa: E402
import chat_history  # noqa: E402
import onebot_common  # noqa: E402
import qq_actions  # noqa: E402
import user_memory  # noqa: E402
import repeat_chain  # noqa: E402
import signal_filter  # noqa: E402

try:
    import bot_plugins  # noqa: E402
except Exception:
    bot_plugins = None

try:
    import group_requests  # noqa: E402
except Exception:
    group_requests = None


def _plugin_enabled(pid: str, default: bool = True) -> bool:
    """Fail-open hot-reloaded plugin toggle query."""
    if bot_plugins is None:
        return default
    try:
        return bool(bot_plugins.is_enabled(pid))
    except Exception:
        return default


def _plugin_setting(pid: str, key: str, default=None):
    """Fail-open hot-reloaded plugin setting query."""
    if bot_plugins is None:
        return default
    try:
        return bot_plugins.get_setting(pid, key, default)
    except Exception:
        return default


def _ambient_listen_enabled() -> bool:
    return bool(AMBIENT_LISTEN_ENABLED) and _plugin_enabled("ambient_listen")


def _sticker_vision_enabled() -> bool:
    return _plugin_enabled("multimodal_vision") and bool(
        _plugin_setting("multimodal_vision", "sticker_vision", True)
    )


def _history_archive_enabled() -> bool:
    return _plugin_enabled("chat_history_lookup") or bool(
        _plugin_setting("chat_history_lookup", "passive_archive", True)
    )


def _has_active_ws_hook(hook_name: str) -> bool:
    if bot_plugins is None:
        return False
    try:
        base_fn = getattr(bot_plugins.BotPlugin, hook_name, None)
        for inst in bot_plugins.list_plugins():
            if getattr(type(inst), hook_name, None) is not base_fn and bot_plugins.is_enabled(inst.id):
                return True
    except Exception:
        return True
    return False


def _apply_ws_inbound_hooks(obj: dict, raw: str, ctx: dict):
    """Run custom plugin WS inbound hooks (error-isolated). Returns (obj, raw, should_drop)."""
    if bot_plugins is None:
        return obj, raw, False
    snapshot = copy.deepcopy(obj) if _has_active_ws_hook("on_ws_inbound") else None
    try:
        new_obj, should_drop = bot_plugins.run_ws_inbound_hooks(obj, ctx)
    except Exception as exc:
        log(f"Plugin inbound hook error: {exc}")
        return obj, raw, False
    if should_drop:
        return obj, raw, True
    if not isinstance(new_obj, dict):
        new_obj = obj
    if new_obj is not obj or (snapshot is not None and new_obj != snapshot):
        return new_obj, json.dumps(new_obj, ensure_ascii=False), False
    return obj, raw, False


def _apply_ws_outbound_hooks(obj: dict, raw: str, ctx: dict):
    """Run custom plugin WS outbound hooks (error-isolated). Returns (obj, raw, should_drop)."""
    if bot_plugins is None:
        return obj, raw, False
    snapshot = copy.deepcopy(obj) if _has_active_ws_hook("on_ws_outbound") else None
    try:
        new_obj, should_drop = bot_plugins.run_ws_outbound_hooks(obj, ctx)
    except Exception as exc:
        log(f"Plugin outbound hook error: {exc}")
        return obj, raw, False
    if should_drop:
        return obj, raw, True
    if not isinstance(new_obj, dict):
        new_obj = obj
    if new_obj is not obj or (snapshot is not None and new_obj != snapshot):
        return new_obj, json.dumps(new_obj, ensure_ascii=False), False
    return obj, raw, False

# All identity/token settings come from bot.env (no hard-coded fallbacks)
LISTEN_HOST = os.environ.get("GUARD_LISTEN_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("GUARD_LISTEN_PORT", "3002"))
UDP_NOTIFY_PORT = int(os.environ.get("GUARD_UDP_PORT", "3003"))
UPSTREAM_WS_URL = os.environ.get("NAPCAT_WS_URL", "ws://127.0.0.1:3001")
NAPCAT_HTTP_URL = os.environ.get("NAPCAT_HTTP_URL", "http://127.0.0.1:3000").rstrip("/")
NAPCAT_TOKEN = os.environ.get("NAPCAT_TOKEN", "")


def _napcat_http_post(endpoint: str, payload: dict, timeout: float = 4.0):
    return onebot_common.napcat_api_post(
        NAPCAT_HTTP_URL, NAPCAT_TOKEN, endpoint, payload, timeout=timeout
    )

BOT_QQ_ID = os.environ.get("BOT_QQ_ID", "").strip()
ADMIN_QQ_IDS = {
    x.strip() for x in os.environ.get("ADMIN_QQ_IDS", "").split(",") if x.strip()
}
IGNORED_BOT_QQ_IDS = {
    x.strip()
    for x in os.environ.get("IGNORED_BOT_QQ_IDS", "2854196310").split(",")
    if x.strip()
}
IGNORED_BOT_NAMES = {
    x.strip().lower()
    for x in os.environ.get("IGNORED_BOT_NAMES", "q群管家").split(",")
    if x.strip()
}

DIRECT_CALL_NAMES = [
    x.strip().lower()
    for x in os.environ.get(
        "BOT_CALL_NAMES", "哈基米,及其猫,机器猫,本猫,紫毛猫,紫发猫"
    ).split(",")
    if x.strip()
]
SOFT_CALL_CUES = [
    x.strip().lower()
    for x in os.environ.get(
        "BOT_SOFT_CALL_CUES",
        "猫猫,小猫,小紫,紫毛,紫发,那只猫,某猫,咪咪,哈基,及其,猫娘",
    ).split(",")
    if x.strip()
]

# Safety TTL in case a turn hangs without sending UDP done signal.
# DMs get a longer one: the admin's DM is where long tasks run.
BUSY_TTL_SECS = float(os.environ.get("GUARD_BUSY_TTL_SECS", "120"))
DM_BUSY_TTL_SECS = float(os.environ.get("GUARD_DM_BUSY_TTL_SECS", "900"))
# Brief pause after silent NO_REPLY turn before draining next message
SILENT_DONE_DELAY_SECS = float(os.environ.get("GUARD_SILENT_DONE_DELAY_SECS", "0.25"))
# Settle pause after NapCat confirms outbound send_group_msg echo (catches multi-chunk + unlock)
POST_SEND_SETTLE_SECS = float(os.environ.get("GUARD_POST_SEND_SETTLE_SECS", "0.35"))
# Max time to wait for outbound send echo after agent turn completes
SEND_ECHO_WAIT_CAP_SECS = float(os.environ.get("GUARD_SEND_ECHO_WAIT_CAP_SECS", "6.0"))


def _env_flag(name, default):
    default_bool = str(default).strip().lower() not in ("", "0", "false", "no", "off")
    return onebot_common.env_bool(name, default=default_bool)


# Whether to dispatch unmentioned ambient group chatter (0 = only @bot/reply/name-calls/soft-cues/repeat)
AMBIENT_LISTEN_ENABLED = _env_flag("GUARD_AMBIENT_LISTEN", "1")
# Group chat whitelist: when enabled (1), only group_ids in GROUP_WHITELIST_IDS are forwarded
GROUP_WHITELIST_ENABLED = _env_flag("GROUP_WHITELIST_ENABLED", "0")
GROUP_WHITELIST_IDS = {
    x.strip()
    for x in os.environ.get("GROUP_WHITELIST_IDS", "").split(",")
    if x.strip()
}
# Minimum gap between dispatching unmentioned ambient turns per group
AMBIENT_DISPATCH_MIN_GAP_SECS = float(os.environ.get("GUARD_AMBIENT_GAP_SECS", "20.0"))
# Lurking back-off: every consecutive ambient turn the model answers with NO_REPLY
# doubles the gap above (20 -> 40 -> 80 -> 160 -> cap); the bot speaking, or
# anyone @/replying/calling it, resets it. 0 keeps the fixed gap.
AMBIENT_BACKOFF_ENABLED = _env_flag("GUARD_AMBIENT_BACKOFF", "1")
AMBIENT_MAX_GAP_SECS = float(os.environ.get("GUARD_AMBIENT_MAX_GAP_SECS", "300"))
def _int_env(name, default):
    raw = os.environ.get(name, "")
    if raw is None or not str(raw).strip():
        return default
    try:
        return int(float(str(raw).strip()))
    except ValueError:
        return default


def _float_env(name, default):
    raw = os.environ.get(name, "")
    if raw is None or not str(raw).strip():
        return default
    try:
        return float(str(raw).strip())
    except ValueError:
        return default


# Hard cap on ambient (unaddressed) model wake-ups per group per rolling hour (0 = no cap)
AMBIENT_MAX_PER_HOUR = _int_env("GUARD_AMBIENT_MAX_PER_HOUR", 20)
# Active conversation window & unlimited flag shared with wrapper (0 = no count cap during active convo)
ACTIVE_CONV_WINDOW_SECS = _float_env("ACTIVE_CONV_WINDOW_SECS", 60.0)
ACTIVE_CONV_MAX_PER_WINDOW = _int_env("ACTIVE_CONV_MAX_PER_WINDOW", 1)
ACTIVE_CONV_UNLIMITED = ACTIVE_CONV_MAX_PER_WINDOW <= 0
# Unaddressed image-only / sticker-only messages (vision calls are expensive):
# outside a conversation the bot just took part in, drop repeats of the same
# picture and allow at most IMAGE_AMBIENT_MAX per IMAGE_AMBIENT_WINDOW_SECS.
IMAGE_BUDGET_ENABLED = _env_flag("GUARD_IMAGE_BUDGET", "1")
IMAGE_CONVO_SECS = float(
    os.environ.get("GUARD_IMAGE_CONVO_SECS", "").strip()
    or os.environ.get("ACTIVE_CONV_WINDOW_SECS", "").strip()
    or "60"
)
IMAGE_DEDUP_SECS = float(os.environ.get("GUARD_IMAGE_DEDUP_SECS", "3600"))
IMAGE_AMBIENT_WINDOW_SECS = float(os.environ.get("GUARD_IMAGE_AMBIENT_WINDOW_SECS", "600"))
IMAGE_AMBIENT_MAX = int(os.environ.get("GUARD_IMAGE_AMBIENT_MAX", "1"))

_LIVE_ENV_LAST_CHECK = -10.0
_LIVE_ENV_MTIME = None


def _maybe_reload_live_guard_flags():
    """Hot-reload live group whitelist, ambient listen & silent notice flags when bot.env changes."""
    global AMBIENT_LISTEN_ENABLED, GROUP_WHITELIST_ENABLED, GROUP_WHITELIST_IDS, SILENT_IDLE_RESET
    global _LIVE_ENV_LAST_CHECK, _LIVE_ENV_MTIME
    now = time.monotonic()
    if now - _LIVE_ENV_LAST_CHECK < 1.0:
        return
    _LIVE_ENV_LAST_CHECK = now
    try:
        st = os.stat(bot_env.ENV_FILE)
    except OSError:
        return
    if _LIVE_ENV_MTIME is not None and st.st_mtime == _LIVE_ENV_MTIME:
        return
    first_load = _LIVE_ENV_MTIME is None
    _LIVE_ENV_MTIME = st.st_mtime
    if first_load:
        return
    parsed = {}
    try:
        with open(bot_env.ENV_FILE, "r", encoding="utf-8-sig") as f:
            for line in f:
                item = bot_env._parse_line(line)
                if item:
                    parsed[item[0]] = item[1]
    except OSError:
        return

    def _to_flag(val, default):
        raw = parsed.get(val, default)
        return str(raw).strip().lower() not in ("", "0", "false", "no", "off")

    new_ambient = _to_flag("GUARD_AMBIENT_LISTEN", "1")
    new_wl_enabled = _to_flag("GROUP_WHITELIST_ENABLED", "0")
    new_wl_ids = {
        x.strip()
        for x in str(parsed.get("GROUP_WHITELIST_IDS", "")).split(",")
        if x.strip()
    }
    new_silent_idle = _to_flag("CC_SILENT_IDLE_RESET", "1")
    if (
        new_ambient != AMBIENT_LISTEN_ENABLED
        or new_wl_enabled != GROUP_WHITELIST_ENABLED
        or new_wl_ids != GROUP_WHITELIST_IDS
        or new_silent_idle != SILENT_IDLE_RESET
    ):
        AMBIENT_LISTEN_ENABLED = new_ambient
        GROUP_WHITELIST_ENABLED = new_wl_enabled
        GROUP_WHITELIST_IDS = new_wl_ids
        SILENT_IDLE_RESET = new_silent_idle
        log(
            f"Hot-reloaded live flags from bot.env: "
            f"ambient_listen={AMBIENT_LISTEN_ENABLED}, "
            f"group_whitelist={GROUP_WHITELIST_ENABLED} ({len(GROUP_WHITELIST_IDS)} groups), "
            f"silent_idle_reset={SILENT_IDLE_RESET}"
        )


# Long replies -> merged-forward chat record card (see build_forward_request)
FORWARD_LONG_MSG_GROUP = _env_flag("FORWARD_LONG_MSG_GROUP", "1")
FORWARD_LONG_MSG_DM = _env_flag("FORWARD_LONG_MSG_DM", "0")
# "Long" = more than this many characters, or more than this many lines
FORWARD_MIN_CHARS = int(os.environ.get("FORWARD_MIN_CHARS", "300"))
FORWARD_MIN_LINES = int(os.environ.get("FORWARD_MIN_LINES", "12"))
# Each bubble inside the card holds at most this many characters
FORWARD_NODE_MAX_CHARS = max(100, int(os.environ.get("FORWARD_NODE_MAX_CHARS", "800")))
FORWARD_NICKNAME = (
    os.environ.get("FORWARD_NICKNAME", "").strip()
    or next(
        (x.strip() for x in os.environ.get("BOT_CALL_NAMES", "").split(",") if x.strip()),
        "",
    )
    or "哈基米"
)

BLOCKED_OUTBOUND_PATTERNS = (
    "消息已收到，将在当前任务完成后处理",
    "訊息已收到，將在目前任務完成後處理",
    "Message received — will process",
    "Message received, will process",
    "消息队列已满",
    "訊息佇列已滿",
    "Message queue is full",
    "上一个请求仍在处理中",
    "上一個請求仍在處理中",
    "Previous request still processing",
)

# Belt-and-suspenders filter for cc-connect's idle auto-reset notices
# (MsgSessionAutoResetIdle / MsgSessionClosingGraceful) even if an unpatched
# cc-connect binary is used. Respects CC_SILENT_IDLE_RESET (default 1).
SILENT_IDLE_RESET = _env_flag("CC_SILENT_IDLE_RESET", "1")
_IDLE_RESET_OUTBOUND_RE = re.compile(
    r"^(?:"
    r"⏰\s*因空闲超过\s*\d+\s*分钟，已自动切换到新会话。"
    r"|⏰\s*因閒置超過\s*\d+\s*分鐘，已自動切換到新會話。"
    r"|⏰\s*Session auto-reset after\s+\d+\s+minute\(s\) of inactivity\."
    r"|⏰\s*\d+\s*分以上操作がなかったため、新しいセッションに自動切り替えました。"
    r"|⏰\s*La sesión se reinició automáticamente tras\s+\d+\s+minuto\(s\) de inactividad\."
    r"|⏳\s*正在结束上一个会话（通常几秒钟，最多2分钟）。新会话将自动启动。"
    r"|⏳\s*正在結束上一個會話（通常幾秒鐘，最多2分鐘）。新會話將自動啟動。"
    r"|⏳\s*Wrapping up your previous session \(usually a few seconds, up to 2 minutes\)\. Your new session will start automatically\."
    r"|⏳\s*前のセッションを終了中です（通常は数秒、最大2分）。新しいセッションは自動的に開始されます。"
    r"|⏳\s*Cerrando la sesión anterior \(normalmente unos segundos, hasta 2 minutos\)\. La nueva sesión se iniciará automáticamente\."
    r")$"
)

# cc-connect's permission prompt and its "still waiting" hint (core/i18n.go
# MsgPermissionPrompt / MsgPermissionHint) both end with this choice line.
# While one is outstanding the turn is blocked on the user's reply, so that
# reply must skip the busy gate (cc-connect answers it before the session lock).
PERMISSION_PROMPT_MARKERS = (
    "**允许** / **拒绝** / **允许所有**",
    "**允許** / **拒絕** / **允許所有**",
    "**allow** / **deny** / **allow all**",
)

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [ws-guard] {msg}", flush=True)


_SKIPPED_MEM_QUEUE = queue.Queue(maxsize=512)
_SKIPPED_MEM_WORKER_LOCK = threading.Lock()
_SKIPPED_MEM_WORKER_STARTED = False


def _skipped_memory_worker():
    """Dedicated background thread persisting skipped group messages to SQLite."""
    while True:
        item = _SKIPPED_MEM_QUEUE.get()
        try:
            if not isinstance(item, dict):
                continue
            user_memory.record_chat_message(
                chat_key=f"g:{item['group_id']}",
                role="user",
                text=item["plain_text"],
                sender_uid=item["user_id"],
                sender_name=item["sender_name"],
                is_group=True,
                group_id=item["group_id"],
                auto_consolidate=bool(item.get("auto_consolidate", True)),
            )
        except Exception as exc:
            log(f"Async record_skipped memory error: {exc}")
        finally:
            _SKIPPED_MEM_QUEUE.task_done()


def _ensure_skipped_memory_worker():
    global _SKIPPED_MEM_WORKER_STARTED
    if _SKIPPED_MEM_WORKER_STARTED:
        return
    with _SKIPPED_MEM_WORKER_LOCK:
        if _SKIPPED_MEM_WORKER_STARTED:
            return
        worker = threading.Thread(
            target=_skipped_memory_worker,
            daemon=True,
            name="ws-guard-mem-writer",
        )
        worker.start()
        _SKIPPED_MEM_WORKER_STARTED = True


# ---------------------------------------------------------------------------
# Minimal RFC 6455 Async WebSocket framing (stdlib only, zero dependencies)
# ---------------------------------------------------------------------------

class WSClosed(Exception):
    pass


class AsyncWS:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, is_client: bool):
        self.reader = reader
        self.writer = writer
        self.is_client = is_client
        self._write_lock = asyncio.Lock()
        self.closed = False

    async def send_frame(self, opcode: int, payload: bytes):
        if self.closed:
            raise WSClosed()
        async with self._write_lock:
            if self.closed:
                raise WSClosed()
            fin_op = 0x80 | (opcode & 0x0F)
            length = len(payload)
            mask_bit = 0x80 if self.is_client else 0x00
            header = bytearray([fin_op])
            if length < 126:
                header.append(mask_bit | length)
            elif length < 65536:
                header.append(mask_bit | 126)
                header.extend(struct.pack("!H", length))
            else:
                header.append(mask_bit | 127)
                header.extend(struct.pack("!Q", length))
            if self.is_client:
                mask = os.urandom(4)
                header.extend(mask)
                masked = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
                self.writer.write(bytes(header) + masked)
            else:
                self.writer.write(bytes(header) + payload)
            await self.writer.drain()

    async def send_text(self, text: str):
        await self.send_frame(0x1, text.encode("utf-8"))

    async def send_pong(self, payload: bytes = b""):
        await self.send_frame(0xA, payload)

    async def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.writer.close()
            await self.writer.wait_closed()
        except Exception:
            pass

    async def recv_text(self) -> str:
        fragments = []
        while True:
            try:
                hdr = await self.reader.readexactly(2)
            except (asyncio.IncompleteReadError, ConnectionError, OSError):
                raise WSClosed()
            b1, b2 = hdr[0], hdr[1]
            fin = bool(b1 & 0x80)
            opcode = b1 & 0x0F
            masked = bool(b2 & 0x80)
            length = b2 & 0x7F
            try:
                if length == 126:
                    length = struct.unpack("!H", await self.reader.readexactly(2))[0]
                elif length == 127:
                    length = struct.unpack("!Q", await self.reader.readexactly(8))[0]
                mask = await self.reader.readexactly(4) if masked else None
                payload = await self.reader.readexactly(length) if length else b""
            except (asyncio.IncompleteReadError, ConnectionError, OSError):
                raise WSClosed()

            if masked and mask:
                payload = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))

            if opcode == 0x8:  # close
                raise WSClosed()
            if opcode == 0x9:  # ping
                try:
                    await self.send_pong(payload)
                except Exception:
                    pass
                continue
            if opcode == 0xA:  # pong
                continue
            if opcode in (0x1, 0x2, 0x0):
                fragments.append(payload)
                if fin:
                    return b"".join(fragments).decode("utf-8", errors="replace")


async def ws_client_connect(url: str, token: str = "") -> AsyncWS:
    parsed = urlparse(url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "wss" else 80)
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query

    reader, writer = await asyncio.open_connection(host, port)
    try:
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        headers = [
            f"GET {path} HTTP/1.1",
            f"Host: {host}:{port}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
        ]
        if token:
            headers.append(f"Authorization: Bearer {token}")
        req = ("\r\n".join(headers) + "\r\n\r\n").encode("utf-8")
        writer.write(req)
        await writer.drain()

        status_line = await reader.readline()
        if b"101" not in status_line:
            raise ConnectionError(f"Upstream WS upgrade failed: {status_line!r}")
        while True:
            line = await reader.readline()
            if not line or line in (b"\r\n", b"\n"):
                break
        return AsyncWS(reader, writer, is_client=True)
    except BaseException:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass
        raise


async def ws_server_handshake(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> AsyncWS:
    req_line = await reader.readline()
    if not req_line:
        raise WSClosed()
    headers = {}
    while True:
        line = await reader.readline()
        if not line or line in (b"\r\n", b"\n"):
            break
        if b":" in line:
            k, v = line.decode("latin1").split(":", 1)
            headers[k.strip().lower()] = v.strip()

    ws_key = headers.get("sec-websocket-key", "")
    if not ws_key:
        writer.write(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
        await writer.drain()
        raise WSClosed()

    accept = base64.b64encode(
        hashlib.sha1((ws_key + WS_GUID).encode("ascii")).digest()
    ).decode("ascii")
    resp = (
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {accept}\r\n"
        "\r\n"
    ).encode("ascii")
    writer.write(resp)
    await writer.drain()
    return AsyncWS(reader, writer, is_client=False)


# ---------------------------------------------------------------------------
# OneBot v11 message classification & guard state
# ---------------------------------------------------------------------------

def extract_outbound_text(params: dict) -> str:
    msg = params.get("message")
    if isinstance(msg, str):
        return msg
    if isinstance(msg, list):
        parts = []
        for seg in msg:
            if isinstance(seg, dict) and seg.get("type") == "text":
                parts.append(str((seg.get("data") or {}).get("text", "")))
        return "".join(parts)
    return ""


def is_blocked_outbound(obj: dict) -> bool:
    action = obj.get("action")
    if action not in ("send_group_msg", "send_private_msg", "send_msg"):
        return False
    params = obj.get("params") or {}
    txt = extract_outbound_text(params)
    if any(pat in txt for pat in BLOCKED_OUTBOUND_PATTERNS):
        return True
    if SILENT_IDLE_RESET and _IDLE_RESET_OUTBOUND_RE.match(txt.strip()):
        return True
    return False


def is_permission_prompt(params: dict) -> bool:
    txt = extract_outbound_text(params)
    return any(m in txt for m in PERMISSION_PROMPT_MARKERS)


SEND_ACTIONS = ("send_group_msg", "send_private_msg", "send_msg")


def _cq_unescape(text: str) -> str:
    # Same unescaping NapCat applies to the text parts of a CQ-code string message
    return onebot_common.unescape_cq_value(text, unescape_comma=False)


def outbound_pure_text(params: dict):
    """Text of an outbound message that is nothing but text, else None.

    Anything with a CQ code or a non-text segment (image, file, @, reply ...) is
    left alone: those can't be carried into a forward card unchanged.
    """
    msg = params.get("message")
    if isinstance(msg, str):
        if "[CQ:" in msg:
            return None
        auto_escape = params.get("auto_escape")
        if auto_escape is True or str(auto_escape).lower() == "true":
            return msg
        return _cq_unescape(msg)
    if isinstance(msg, list) and msg:
        parts = []
        for seg in msg:
            if not isinstance(seg, dict) or seg.get("type") != "text":
                return None
            parts.append(str((seg.get("data") or {}).get("text", "")))
        return "".join(parts)
    return None


def is_long_text(text: str) -> bool:
    text = text.strip()
    return len(text) > FORWARD_MIN_CHARS or text.count("\n") + 1 > FORWARD_MIN_LINES


def _hard_split(text: str, max_chars: int):
    """Split by lines, then cut lines that alone exceed max_chars."""
    out, cur = [], ""
    for line in text.split("\n"):
        while len(line) > max_chars:
            if cur:
                out.append(cur)
                cur = ""
            out.append(line[:max_chars])
            line = line[max_chars:]
        if cur and len(cur) + 1 + len(line) > max_chars:
            out.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        out.append(cur)
    return out


def split_forward_nodes(text: str, max_chars: int = None):
    """Split a long reply into chat-bubble sized pieces.

    Paragraph (blank line) boundaries are preferred, a ``` code block is never
    split at its inner blank lines, and small paragraphs are packed together.
    """
    max_chars = max_chars or FORWARD_NODE_MAX_CHARS
    paras, cur, in_code = [], [], False
    for line in text.strip().split("\n"):
        if line.strip().startswith("```"):
            in_code = not in_code
        if not line.strip() and not in_code:
            if cur:
                paras.append("\n".join(cur).strip("\n"))
                cur = []
            continue
        cur.append(line)
    if cur:
        paras.append("\n".join(cur).strip("\n"))

    nodes, buf = [], ""
    for para in paras:
        pieces = [para] if len(para) <= max_chars else _hard_split(para, max_chars)
        for piece in pieces:
            if buf and len(buf) + 2 + len(piece) > max_chars:
                nodes.append(buf)
                buf = piece
            else:
                buf = f"{buf}\n\n{piece}" if buf else piece
    if buf:
        nodes.append(buf)
    return [n for n in nodes if n.strip()] or [text]


def build_forward_request(obj: dict, actor_role: str = "member"):
    """Rewrite a long pure-text send_*_msg into a merged-forward request.

    Returns the new request dict (same echo), or None to send the original.
    """
    if not _plugin_enabled("long_msg_forward"):
        return None
    action = obj.get("action")
    if action not in SEND_ACTIONS:
        return None
    params = obj.get("params") or {}
    group_id = params.get("group_id")
    user_id = params.get("user_id")
    msg_type = params.get("message_type")
    if action == "send_group_msg" or (
        action == "send_msg" and (msg_type == "group" or (not msg_type and group_id))
    ):
        if not group_id:
            return None
        gid_str = str(group_id).strip()
        eff = bot_permissions.resolve_capability("forward_long_msg", group_id=gid_str)
        perm_ok = bot_permissions.is_allowed(
            "forward_long_msg", actor_role=actor_role, group_id=gid_str, is_dm=False
        )
        if not perm_ok:
            return None
        if not FORWARD_LONG_MSG_GROUP and not eff.get("overridden"):
            return None
        new_action, target = "send_group_forward_msg", {"group_id": group_id}
    else:
        if not user_id or not FORWARD_LONG_MSG_DM:
            return None
        if not bot_permissions.is_allowed(
            "forward_long_msg", actor_role=actor_role, group_id=None, is_dm=True
        ):
            return None
        new_action, target = "send_private_forward_msg", {"user_id": user_id}

    text = outbound_pure_text(params)
    if text is None or not is_long_text(text):
        return None

    sender = {"user_id": BOT_QQ_ID, "nickname": FORWARD_NICKNAME} if BOT_QQ_ID else {
        "nickname": FORWARD_NICKNAME
    }
    nodes = [
        {"type": "node", "data": dict(sender, content=[{"type": "text", "data": {"text": piece}}])}
        for piece in split_forward_nodes(text)
    ]
    req = {"action": new_action, "params": dict(target, messages=nodes)}
    if "echo" in obj:
        req["echo"] = obj["echo"]
    return req


def api_response_ok(obj: dict) -> bool:
    try:
        retcode = int(obj.get("retcode", 0) or 0)
    except (TypeError, ValueError):
        retcode = -1
    return retcode == 0 and str(obj.get("status", "ok")).lower() != "failed"


def normalize_inbound_mface(obj: dict, raw: str) -> str:
    """Convert OneBot `mface` (QQ market sticker) segments with URLs into `image`
    segments so cc-connect downloads them just like regular images/stickers.
    Returns `raw` unchanged when no `mface` segment is present or sticker vision is disabled.
    """
    if not _sticker_vision_enabled():
        return raw
    msg = obj.get("message")
    if not isinstance(msg, list):
        return raw
    changed = False
    new_segs = []
    for seg in msg:
        if isinstance(seg, dict) and seg.get("type") == "mface":
            dt = seg.get("data") or {}
            url = str(dt.get("url") or "").strip()
            summary = str(dt.get("summary") or "").strip()
            if url:
                new_segs.append(
                    {
                        "type": "image",
                        "data": {
                            "url": url,
                            "summary": summary or "[商城表情]",
                            "sub_type": 1,
                        },
                    }
                )
                changed = True
                continue
            elif summary:
                new_segs.append({"type": "text", "data": {"text": summary}})
                changed = True
                continue
        new_segs.append(seg)
    if not changed:
        return raw
    patched = dict(obj)
    patched["message"] = new_segs
    return json.dumps(patched, ensure_ascii=False)


_URL_FILEID_RE = re.compile(r"[?&]fileid=([^&]+)")


def image_fingerprints(segments) -> list:
    """Stable ids for the pictures in a message (same picture -> same id).

    image: NapCat's `file` (an md5-style name) / file_unique / file_id, else the
    url's fileid parameter (rkey changes per fetch, so never the whole url);
    mface: package + emoji id.
    """
    fps = []
    if not isinstance(segments, list):
        return fps
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        st = seg.get("type", "")
        dt = seg.get("data") or {}
        fp = ""
        if st == "image":
            for k in ("file_unique", "file", "file_id"):
                v = str(dt.get(k) or "").strip()
                if v and not v.startswith(("http://", "https://", "base64://")):
                    fp = "img:" + v.lower()
                    break
            if not fp:
                m = _URL_FILEID_RE.search(str(dt.get("url") or ""))
                if m:
                    fp = "img:" + m.group(1)
        elif st == "mface":
            eid = str(dt.get("emoji_id") or "").strip()
            if eid:
                fp = f"mface:{str(dt.get('emoji_package_id') or '').strip()}:{eid}"
        if fp and fp not in fps:
            fps.append(fp)
    return fps


class AmbientGate:
    """Per-group pacing of unaddressed (lurking) model turns. Pure state, no I/O.

    - gap: base gap doubled per consecutive ambient NO_REPLY turn, capped
    - hourly cap on ambient dispatches
    - image-only messages outside a live conversation: de-dup + small budget
    """

    MAX_STREAK = 10

    def __init__(
        self,
        base_gap=AMBIENT_DISPATCH_MIN_GAP_SECS,
        max_gap=AMBIENT_MAX_GAP_SECS,
        max_per_hour=AMBIENT_MAX_PER_HOUR,
        backoff=AMBIENT_BACKOFF_ENABLED,
        image_budget=IMAGE_BUDGET_ENABLED,
        image_convo_secs=IMAGE_CONVO_SECS,
        image_dedup_secs=IMAGE_DEDUP_SECS,
        image_window_secs=IMAGE_AMBIENT_WINDOW_SECS,
        image_max=IMAGE_AMBIENT_MAX,
        active_conv_secs=None,
        active_conv_unlimited=False,
    ):
        self.base_gap = max(0.0, float(base_gap))
        self.max_gap = max(self.base_gap, float(max_gap))
        self.max_per_hour = int(max_per_hour)
        self.backoff = bool(backoff)
        self.image_budget = bool(image_budget)
        self.image_convo_secs = float(image_convo_secs)
        self.active_conv_secs = (
            self.image_convo_secs if active_conv_secs is None else float(active_conv_secs)
        )
        self.active_conv_unlimited = bool(active_conv_unlimited)
        self.image_dedup_secs = float(image_dedup_secs)
        self.image_window_secs = float(image_window_secs)
        self.image_max = int(image_max)
        self.last_dispatch = {}
        self.silent_streak = {}
        self.dispatch_hist = collections.defaultdict(collections.deque)
        self.image_hist = collections.defaultdict(collections.deque)
        self.image_seen = collections.defaultdict(collections.OrderedDict)
        self.last_bot_spoke = {}
        self._cap_logged_at = {}

    def current_gap(self, chat_key: str) -> float:
        streak = self.silent_streak.get(chat_key, 0) if self.backoff else 0
        return min(self.base_gap * (2 ** streak), self.max_gap) if streak else self.base_gap

    def in_convo(self, chat_key: str, now: float) -> bool:
        ts = self.last_bot_spoke.get(chat_key, 0.0)
        return ts > 0 and (now - ts) < self.image_convo_secs

    def in_active_convo(self, chat_key: str, now: float) -> bool:
        ts = self.last_bot_spoke.get(chat_key, 0.0)
        return self.active_conv_secs > 0 and ts > 0 and (now - ts) < self.active_conv_secs

    @staticmethod
    def _trim(dq, now, window):
        while dq and now - dq[0] >= window:
            dq.popleft()

    def _image_check(self, chat_key, now, fingerprints):
        """(ok, reason). Records every sighting so a picture is judged once per dedup window."""
        seen = self.image_seen[chat_key]
        for fp, ts in list(seen.items()):
            if now - ts < self.image_dedup_secs:
                break
            seen.pop(fp, None)
        dup = any(
            fp in seen and now - seen[fp] < self.image_dedup_secs for fp in fingerprints
        )
        for fp in fingerprints:
            seen.pop(fp, None)
            seen[fp] = now
        while len(seen) > 300:
            seen.popitem(last=False)
        if dup:
            return False, "image_dup"
        hist = self.image_hist[chat_key]
        self._trim(hist, now, self.image_window_secs)
        if self.image_max <= 0 or len(hist) >= self.image_max:
            return False, "image_budget"
        return True, ""

    def allow(self, chat_key: str, now: float, image_only=False, fingerprints=()) -> tuple:
        """Decide (and on success record) one ambient dispatch. Returns (ok, reason)."""
        counts_image = bool(
            image_only and self.image_budget and not self.in_convo(chat_key, now)
        )
        if counts_image:
            ok, reason = self._image_check(chat_key, now, list(fingerprints or ()))
            if not ok:
                return False, reason
        last = self.last_dispatch.get(chat_key, 0.0)
        if last > 0 and (now - last) < self.current_gap(chat_key):
            return False, "gap"
        bypass_hour_cap = bool(self.active_conv_unlimited and self.in_active_convo(chat_key, now))
        hist = self.dispatch_hist[chat_key]
        self._trim(hist, now, 3600.0)
        if not bypass_hour_cap and self.max_per_hour > 0 and len(hist) >= self.max_per_hour:
            if now - self._cap_logged_at.get(chat_key, 0.0) > 600:
                self._cap_logged_at[chat_key] = now
                log(f"Ambient hourly cap reached in {chat_key} ({self.max_per_hour}/h); lurking quietly")
            return False, "hour_cap"
        self.last_dispatch[chat_key] = now
        if not bypass_hour_cap:
            hist.append(now)
        if counts_image:
            self.image_hist[chat_key].append(now)
        return True, ""

    def _set_streak(self, chat_key: str, value: int):
        old = self.silent_streak.get(chat_key, 0)
        value = max(0, min(int(value), self.MAX_STREAK))
        if value == old:
            return
        old_gap = self.current_gap(chat_key)
        if value:
            self.silent_streak[chat_key] = value
        else:
            self.silent_streak.pop(chat_key, None)
        new_gap = self.current_gap(chat_key)
        if self.backoff and new_gap != old_gap:
            log(f"Ambient back-off {chat_key}: streak {old} -> {value}, gap {old_gap:.0f}s -> {new_gap:.0f}s")

    def on_turn_done(self, chat_key: str, silent: bool, woke_model: bool, ambient: bool):
        if not silent:
            self._set_streak(chat_key, 0)
        elif woke_model and ambient:
            self._set_streak(chat_key, self.silent_streak.get(chat_key, 0) + 1)

    def on_addressed(self, chat_key: str):
        self._set_streak(chat_key, 0)

    def on_bot_spoke(self, chat_key: str, now: float):
        self.last_bot_spoke[chat_key] = now
        self._set_streak(chat_key, 0)


def analyze_inbound_group_event(obj: dict, bot_msg_ids: set):
    """Classify a OneBot group message event.

    Returns dict:
      ignore: bool (drop completely)
      priority: bool (explicit @bot, reply to bot, direct name, or admin slash cmd)
      soft_call: bool (sounds like calling bot)
      mentioned_other_only: bool
      group_id: str
      user_id: str
      sender_name: str
      plain_text: str
    """
    uid = str(obj.get("user_id", ""))
    gid = str(obj.get("group_id", ""))
    sender = obj.get("sender") or {}
    sender_name = (sender.get("card") or sender.get("nickname") or uid).strip()

    res = {
        "ignore": False,
        "priority": False,
        "soft_call": False,
        "mentioned_other_only": False,
        "group_id": gid,
        "user_id": uid,
        "sender_name": sender_name,
        "plain_text": "",
        # Picture(s) with no meaningful text, and stable ids of those pictures
        "image_only": False,
        "image_fps": [],
    }

    if (BOT_QQ_ID and uid == BOT_QQ_ID) or uid in IGNORED_BOT_QQ_IDS:
        res["ignore"] = True
        return res
    if sender_name.lower() in IGNORED_BOT_NAMES:
        res["ignore"] = True
        return res

    msg = obj.get("message")
    text_parts = []
    has_image = False
    has_media = False
    mentioned_bot = False
    mentioned_other = False
    replied_to_bot = False
    sticker_vision_on = _sticker_vision_enabled()

    if isinstance(msg, list):
        for seg in msg:
            if not isinstance(seg, dict):
                continue
            st = seg.get("type", "")
            dt = seg.get("data") or {}
            if st == "text":
                text_parts.append(str(dt.get("text", "")))
            elif st == "at":
                qq = str(dt.get("qq", ""))
                if BOT_QQ_ID and qq == BOT_QQ_ID:
                    mentioned_bot = True
                elif qq and qq != "all":
                    mentioned_other = True
            elif st == "reply":
                rid = str(dt.get("id", ""))
                if rid and rid in bot_msg_ids:
                    replied_to_bot = True
            elif st == "image":
                has_image = True
                has_media = True
            elif st == "mface":
                if sticker_vision_on:
                    if dt.get("url"):
                        has_image = True
                        has_media = True
                    elif dt.get("summary"):
                        text_parts.append(str(dt.get("summary")))
            elif st in ("record", "file"):
                has_media = True
    elif isinstance(msg, str):
        text_parts.append(msg)
        if BOT_QQ_ID and f"[CQ:at,qq={BOT_QQ_ID}]" in msg:
            mentioned_bot = True
        elif "[CQ:at,qq=" in msg and "[CQ:at,qq=all]" not in msg:
            mentioned_other = True

    plain = re.sub(r"\[CQ:[^\]]+\]", "", "".join(text_parts)).strip()
    res["plain_text"] = plain
    low = plain.lower()

    # cc-connect's parseMessage ignores messages with empty text and no image/file/record
    # (e.g. bare @bot with no text, or QQ face/forward/json-only messages). Never mark a
    # group busy for something cc-connect drops silently without invoking agy-acp-wrapper.
    if not plain and not has_media:
        res["ignore"] = True
        return res

    direct_name = any(n in low for n in DIRECT_CALL_NAMES)
    soft_cue = (
        _plugin_enabled("soft_call_wakeup")
        and (not direct_name)
        and any(c in low for c in SOFT_CALL_CUES)
    )
    is_admin_cmd = uid in ADMIN_QQ_IDS and plain.startswith("/")

    res["mentioned_other_only"] = bool(
        mentioned_other and not mentioned_bot and not replied_to_bot and not direct_name
    )
    res["priority"] = bool(mentioned_bot or replied_to_bot or direct_name or is_admin_cmd)
    res["soft_call"] = bool(soft_cue)

    low_signal_text = signal_filter.is_low_signal_text(plain)
    if has_image and low_signal_text:
        res["image_only"] = True
        res["image_fps"] = image_fingerprints(msg)

    # Drop purely media/empty noise (?, …, emoji, 哈哈哈) when not directly calling us
    if not res["priority"] and not res["soft_call"]:
        if res["mentioned_other_only"]:
            res["ignore"] = True
        elif low_signal_text and not has_image:
            res["ignore"] = True

    return res


def is_bot_sender(obj: dict) -> bool:
    """Our own account, or a bot on the ignore lists (they never join a repeat chain)."""
    uid = str(obj.get("user_id", ""))
    sender = obj.get("sender") or {}
    name = (sender.get("card") or sender.get("nickname") or uid).strip().lower()
    return bool(
        (BOT_QQ_ID and uid == BOT_QQ_ID) or uid in IGNORED_BOT_QQ_IDS or name in IGNORED_BOT_NAMES
    )


def outbound_group_key(action, params: dict) -> str:
    """g:<id> for a group send, else ""."""
    gid = str(params.get("group_id") or "").strip()
    if not gid:
        return ""
    if action == "send_group_msg" or params.get("message_type") == "group" or not params.get("user_id"):
        return f"g:{gid}"
    return ""


class GuardBridge:
    def __init__(self):
        self.client_ws = None
        self.upstream_ws = None
        # Track recent bot message IDs so we detect replies to bot
        self.bot_msg_ids = collections.deque(maxlen=400)
        self.bot_msg_id_set = set()
        # Track echo -> (chat_key, sent_ts) for capturing bot's sent message_ids and
        # syncing busy-gate release with actual NapCat send completion
        self.pending_send_echoes = {}
        # Track echo -> (chat_key, text, reply_to_id, sent_ts) to archive bot replies
        # with their real OneBot message_id once NapCat confirms delivery
        self.pending_outbound_archive = {}
        self.active_send_counts = collections.defaultdict(int)
        # echo -> (original plain send request, sent_ts) for long replies rewritten
        # into a forward card; resent as-is if NapCat rejects the card
        self.forward_fallbacks = {}

        # Per-chat busy state: chat_key -> started_ts
        self.busy_chats = {}
        # Active turn actor role per chat_key ("admin" | "member" | "autonomous")
        self.active_turn_role = {}
        # Priority queues: chat_key -> deque[(raw_json_str, actor_role)]
        self.priority_queues = collections.defaultdict(lambda: collections.deque(maxlen=10))
        # Latest coalesced ambient message per group: chat_key -> raw_json_str
        self.latest_ambient = {}
        # Ambient (lurking) pacing: gap + NO_REPLY back-off + hourly cap + image budget
        self.ambient_gate = AmbientGate(
            active_conv_secs=ACTIVE_CONV_WINDOW_SECS,
            active_conv_unlimited=ACTIVE_CONV_UNLIMITED,
        )
        # Delayed release tasks
        self.release_tasks = {}
        # DM chats whose current turn is blocked on a permission prompt:
        # chat_key -> prompt_ts. The next message from that chat skips the gate.
        self.awaiting_permission = {}
        # Repeat chain per group:
        # chat_key -> {key, segs, count, users, last_ts, offered_ts, followed_ts}
        self.repeat_chains = {}
        # The chain last handed to the model per group (its FOLLOW_REPEAT answer
        # may arrive after the chat has moved on)
        self.repeat_offers = {}
        self.last_repeat_offer = {}
        # Fire-and-forget background tasks (kept referenced until completion)
        self._bg_tasks = set()

    def _spawn_bg(self, coro):
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        return task

    async def _on_group_request(self, obj: dict):
        if group_requests is None:
            return
        gid = str(obj.get("group_id") or "").strip()
        uid = str(obj.get("user_id") or "").strip()
        sub_type = str(obj.get("sub_type") or "add").strip()
        try:
            res = await asyncio.to_thread(
                group_requests.handle_ws_request_event,
                obj,
                napcat_call=_napcat_http_post,
                admin_qq_ids=ADMIN_QQ_IDS,
            )
            if isinstance(res, dict):
                req_obj = res.get("request") if isinstance(res.get("request"), dict) else {}
                eval_obj = res.get("evaluation") if isinstance(res.get("evaluation"), dict) else {}
                dec_obj = res.get("decision") if isinstance(res.get("decision"), dict) else {}
                status = (
                    req_obj.get("status")
                    or eval_obj.get("action")
                    or ("handled" if res.get("handled") else "pending")
                )
                detail = dec_obj.get("error") or eval_obj.get("reason") or ""
                suffix = f" ({detail})" if detail else ""
                log(f"Group join request g:{gid} u:{uid} ({sub_type}) -> {status}{suffix}")
            else:
                log(f"Group join request g:{gid} u:{uid} ({sub_type}) -> processed")
        except Exception as exc:
            log(f"Group join request g:{gid} u:{uid} ({sub_type}) error: {exc}")

    async def _on_group_increase_notice(self, obj: dict):
        if group_requests is None:
            return
        gid = str(obj.get("group_id") or "").strip()
        uid = str(obj.get("user_id") or "").strip()
        op = str(obj.get("operator_id") or "").strip()
        if not gid or not uid:
            return
        try:
            updated = await asyncio.to_thread(
                group_requests.mark_external_group_increase,
                gid,
                uid,
                operator_id=op,
            )
            if updated:
                log(
                    f"Group increase notice g:{gid} u:{uid} (op:{op or '?'}) "
                    f"-> marked {updated} pending request(s)"
                )
        except Exception as exc:
            log(f"Group increase notice g:{gid} u:{uid} error: {exc}")

    def remember_bot_msg_id(self, mid):
        mid_s = str(mid or "").strip()
        if not mid_s or mid_s == "0" or mid_s in self.bot_msg_id_set:
            return
        if len(self.bot_msg_ids) == self.bot_msg_ids.maxlen:
            old = self.bot_msg_ids.popleft()
            self.bot_msg_id_set.discard(old)
        self.bot_msg_ids.append(mid_s)
        self.bot_msg_id_set.add(mid_s)

    def _prune_stale_send_echoes(self, max_age=25.0):
        cutoff = time.time() - max_age
        stale = [k for k, (_, ts) in self.pending_send_echoes.items() if ts < cutoff]
        for k in stale:
            chat_key, _ = self.pending_send_echoes.pop(k)
            if chat_key and self.active_send_counts.get(chat_key, 0) > 0:
                self.active_send_counts[chat_key] -= 1
            self.pending_outbound_archive.pop(k, None)
        stale_arch = [k for k, item in self.pending_outbound_archive.items() if item[3] < cutoff]
        for k in stale_arch:
            self.pending_outbound_archive.pop(k, None)

    def is_chat_busy(self, chat_key: str) -> bool:
        ts = self.busy_chats.get(chat_key)
        if ts is None:
            return False
        ttl = DM_BUSY_TTL_SECS if chat_key.startswith("dm:") else BUSY_TTL_SECS
        if time.time() - ts > ttl:
            self.busy_chats.pop(chat_key, None)
            return False
        return True

    def _cleanup_send_echo(self, echo):
        if echo is None:
            return None
        ekey = str(echo)
        entry = self.pending_send_echoes.pop(ekey, None)
        if entry is not None:
            send_chat_key, _ = entry
            if send_chat_key and self.active_send_counts.get(send_chat_key, 0) > 0:
                self.active_send_counts[send_chat_key] -= 1
        self.pending_outbound_archive.pop(ekey, None)
        return entry

    def mark_chat_busy(self, chat_key: str, actor_role: str = "member"):
        prev_rel = self.release_tasks.pop(chat_key, None)
        if prev_rel and not prev_rel.done():
            prev_rel.cancel()
        self.busy_chats[chat_key] = time.time()
        self.active_turn_role[chat_key] = actor_role
        # A new turn starts: any prompt left from an earlier turn is stale
        self.awaiting_permission.pop(chat_key, None)

    def track_repeat(self, chat_key: str, obj: dict, info: dict) -> str:
        """Update the group's repeat chain with this human message.

        Returns "offer" when the model should now decide whether to join the
        chain, "skip" when the message only continues a chain (it isn't
        dispatched), else "" (handle the message as usual).
        """
        if not _plugin_enabled("repeat_chain"):
            return ""
        sig = repeat_chain.signature(obj.get("message"))
        info["repeat_key"] = sig[0] if sig else None
        now = time.time()
        chain = self.repeat_chains.get(chat_key)
        if sig and chain and chain["key"] == sig[0] and now - chain["last_ts"] <= repeat_chain.WINDOW_SECS:
            chain["count"] += 1
            chain["users"].add(info["user_id"])
            chain["last_ts"] = now
        else:
            chain = {
                "key": sig[0] if sig else None,
                "segs": sig[1] if sig else None,
                "count": 1,
                "users": {info["user_id"]},
                "last_ts": now,
                "offered_ts": 0.0,
                "followed_ts": 0.0,
            }
            self.repeat_chains[chat_key] = chain
        if not chain["key"] or chain["count"] < 2 or info["priority"] or info["soft_call"]:
            return ""
        if info["ignore"]:
            # e.g. a bare QQ face: cc-connect drops it without a turn, so it can't
            # be offered to the model (the group would stay busy until the TTL)
            return ""
        if chain["offered_ts"] or chain["followed_ts"]:
            return "skip"
        if chain["count"] < repeat_chain.MIN_COUNT or len(chain["users"]) < 2:
            return "skip"
        if now - self.last_repeat_offer.get(chat_key, 0.0) < repeat_chain.COOLDOWN_SECS:
            return "skip"
        chain["offered_ts"] = now
        self.last_repeat_offer[chat_key] = now
        self.repeat_offers[chat_key] = chain
        return "offer"

    def take_follow_token(self, chat_key: str):
        """Segments to send for the model's FOLLOW_REPEAT answer, or None to drop it."""
        chain = self.repeat_offers.get(chat_key) if chat_key else None
        if not chain or chain["followed_ts"] or time.time() - chain["offered_ts"] > 300.0:
            return None
        chain["followed_ts"] = time.time()
        return chain["segs"]

    def outbound_repeats_chain(self, chat_key: str, params: dict) -> bool:
        """True if a model reply would say again what the bot already repeated.

        The first time a reply matches the current chain it is let through and
        the chain counts as followed, so the bot never joins the same chain twice.
        """
        chain = self.repeat_chains.get(chat_key)
        if not chain or not chain["key"]:
            return False
        sig = repeat_chain.signature(params.get("message"))
        if not sig or sig[0] != chain["key"]:
            return False
        if not chain["followed_ts"]:
            chain["followed_ts"] = time.time()
            return False
        return True

    async def ack_dropped_send(self, echo):
        if self.client_ws and not self.client_ws.closed:
            ack = {"status": "ok", "retcode": 0, "data": {"message_id": 0}}
            if echo is not None:
                ack["echo"] = echo
            await self.client_ws.send_text(json.dumps(ack, ensure_ascii=False))

    def ambient_allow(self, chat_key: str, info) -> bool:
        """Single gate for every unaddressed (lurking) dispatch path."""
        info = info or {}
        ok, reason = self.ambient_gate.allow(
            chat_key,
            time.time(),
            image_only=bool(info.get("image_only")),
            fingerprints=info.get("image_fps") or (),
        )
        if not ok and reason.startswith("image"):
            log(f"Skipped unaddressed picture in {chat_key} ({reason})")
        return ok

    @staticmethod
    def record_skipped(info: dict):
        """Record a message that will never reach the wrapper into user memory.

        Messages that ARE dispatched are recorded by agy-acp-wrapper.py, so this
        is only called for dropped / superseded ones (no double recording).
        Offloaded to a bounded background worker thread so SQLite write locks
        never freeze the asyncio OneBot WebSocket event loop.
        """
        if not info or not info.get("plain_text"):
            return
        extraction_on = _plugin_enabled("memory_extraction")
        if not (
            extraction_on
            or _plugin_enabled("memory_continuity")
            or _plugin_enabled("memory_recall")
        ):
            return
        payload = {
            "group_id": str(info.get("group_id") or ""),
            "plain_text": str(info.get("plain_text") or ""),
            "user_id": str(info.get("user_id") or ""),
            "sender_name": str(info.get("sender_name") or ""),
            "auto_consolidate": bool(extraction_on),
        }
        _ensure_skipped_memory_worker()
        try:
            _SKIPPED_MEM_QUEUE.put_nowait(payload)
        except queue.Full:
            try:
                _SKIPPED_MEM_QUEUE.get_nowait()
                _SKIPPED_MEM_QUEUE.task_done()
            except queue.Empty:
                pass
            try:
                _SKIPPED_MEM_QUEUE.put_nowait(payload)
            except queue.Full:
                log("Skipped-memory queue full; dropped message")

    def schedule_chat_release(self, chat_key: str, silent: bool = False):
        prev = self.release_tasks.get(chat_key)
        if prev and not prev.done():
            prev.cancel()
        self.release_tasks[chat_key] = asyncio.create_task(
            self._release_after_delay(chat_key, silent=silent)
        )

    async def _release_after_delay(self, chat_key: str, silent: bool = False):
        try:
            if silent:
                await asyncio.sleep(SILENT_DONE_DELAY_SECS)
            else:
                # Give cc-connect a brief moment to emit send_group_msg / send_private_msg
                await asyncio.sleep(0.25)
                deadline = time.monotonic() + SEND_ECHO_WAIT_CAP_SECS
                while time.monotonic() < deadline:
                    self._prune_stale_send_echoes()
                    while (
                        self.active_send_counts.get(chat_key, 0) > 0
                        and time.monotonic() < deadline
                    ):
                        await asyncio.sleep(0.05)
                    # Settle window so multi-chunk replies and cc-connect session.Unlock finish
                    await asyncio.sleep(POST_SEND_SETTLE_SECS)
                    if self.active_send_counts.get(chat_key, 0) == 0:
                        break
            self.busy_chats.pop(chat_key, None)
            self.active_turn_role.pop(chat_key, None)
            self.awaiting_permission.pop(chat_key, None)
            await self.drain_next(chat_key)
        except asyncio.CancelledError:
            pass

    async def drain_next(self, chat_key: str):
        if not self.client_ws or self.client_ws.closed:
            return
        if self.is_chat_busy(chat_key):
            return

        # 1. Priority queue first (explicit @bot / reply / name call / DM)
        pq = self.priority_queues.get(chat_key)
        while pq:
            item = pq.popleft()
            is_soft_queued = False
            soft_info = None
            if isinstance(item, tuple):
                if len(item) >= 4:
                    raw, role, is_soft_queued, soft_info = (
                        item[0],
                        item[1],
                        bool(item[2]),
                        item[3],
                    )
                else:
                    raw, role = item[0], item[1]
            else:
                raw, role = item, "member"
            if is_soft_queued and not _plugin_enabled("soft_call_wakeup"):
                _maybe_reload_live_guard_flags()
                if not _ambient_listen_enabled() or not self.ambient_allow(chat_key, soft_info):
                    self.record_skipped(soft_info)
                    continue
                role = "autonomous"
            self.mark_chat_busy(chat_key, actor_role=role)
            try:
                await self.client_ws.send_text(raw)
            except Exception:
                self.busy_chats.pop(chat_key, None)
            return

        # 2. Otherwise latest coalesced ambient message (respecting gentle gap)
        amb = self.latest_ambient.pop(chat_key, None)
        if amb:
            _maybe_reload_live_guard_flags()
            raw, is_soft, enqueued_ts, info = amb
            if is_soft and not _plugin_enabled("soft_call_wakeup"):
                is_soft = False
            if not _ambient_listen_enabled() and not is_soft:
                self.record_skipped(info)
                return
            # Drop stale ambient messages older than 25s
            if time.time() - enqueued_ts > 25.0 and not is_soft:
                self.record_skipped(info)
                return
            if not is_soft and not self.ambient_allow(chat_key, info):
                self.record_skipped(info)
                return
            amb_role = (
                ("admin" if info.get("user_id") in ADMIN_QQ_IDS else "member")
                if is_soft
                else "autonomous"
            )
            self.mark_chat_busy(chat_key, actor_role=amb_role)
            try:
                await self.client_ws.send_text(raw)
            except Exception:
                self.busy_chats.pop(chat_key, None)

    async def handle_upstream_message(self, raw: str):
        if not self.client_ws or self.client_ws.closed:
            return
        try:
            obj = json.loads(raw)
        except Exception:
            await self.client_ws.send_text(raw)
            return

        # Capture message_id from NapCat API response to our bot's sends
        echo = obj.get("echo")
        if echo is not None and str(echo) in self.forward_fallbacks:
            original_raw, _ = self.forward_fallbacks.pop(str(echo))
            if not api_response_ok(obj) and self.upstream_ws and not self.upstream_ws.closed:
                # Forward card rejected: send the original plain message with the
                # same echo; its response is what cc-connect will get
                log(
                    f"Forward card failed (retcode={obj.get('retcode')}, "
                    f"msg={obj.get('message') or obj.get('wording')}), "
                    f"falling back to plain message (echo={echo})"
                )
                try:
                    await self.upstream_ws.send_text(original_raw)
                    return
                except Exception as e:
                    log(f"Fallback plain send failed: {e}")
                    self._cleanup_send_echo(echo)
                except BaseException:
                    self._cleanup_send_echo(echo)
                    raise
        if echo is not None and str(echo) in self.pending_send_echoes:
            send_chat_key, _ = self.pending_send_echoes.pop(str(echo))
            if send_chat_key and self.active_send_counts.get(send_chat_key, 0) > 0:
                self.active_send_counts[send_chat_key] -= 1
            data = obj.get("data")
            sent_mid = ""
            if isinstance(data, dict) and data.get("message_id"):
                sent_mid = str(data["message_id"])
                self.remember_bot_msg_id(sent_mid)
            arch = self.pending_outbound_archive.pop(str(echo), None)
            if arch and api_response_ok(obj) and _history_archive_enabled():
                arch_key, arch_text, arch_reply_id, arch_ts = arch
                chat_history.record_outbound_bot_message_async(
                    chat_key=arch_key,
                    text=arch_text,
                    message_id=sent_mid,
                    bot_qq_id=BOT_QQ_ID,
                    bot_name=FORWARD_NICKNAME,
                    reply_to_id=arch_reply_id,
                    ts=arch_ts,
                )

        post_type = obj.get("post_type")
        if post_type == "request" and obj.get("request_type") == "group" and group_requests is not None:
            self._spawn_bg(self._on_group_request(obj))
        elif post_type == "notice" and obj.get("notice_type") == "group_increase" and group_requests is not None:
            self._spawn_bg(self._on_group_increase_notice(obj))
        if post_type != "message":
            await self.client_ws.send_text(raw)
            return

        msg_type = obj.get("message_type")

        # Private DM: gate per user_id so DM never hits busy queue either
        if msg_type == "private":
            uid = str(obj.get("user_id", ""))
            if BOT_QQ_ID and uid == BOT_QQ_ID:
                return
            in_ctx = {
                "is_group": False,
                "is_dm": True,
                "group_id": "",
                "user_id": uid,
                "chat_key": f"dm:{uid}",
                "is_admin": uid in ADMIN_QQ_IDS,
                "bot_qq_id": BOT_QQ_ID,
            }
            obj, raw, should_drop = _apply_ws_inbound_hooks(obj, raw, in_ctx)
            if should_drop:
                return
            if _history_archive_enabled():
                chat_history.record_onebot_event_async(
                    obj, bot_qq_id=BOT_QQ_ID, ignored_bot_ids=IGNORED_BOT_QQ_IDS
                )
            raw = normalize_inbound_mface(obj, raw)
            plain = extract_outbound_text(obj).strip()
            msg_segs = obj.get("message")
            sticker_vision_on = _sticker_vision_enabled()
            has_media = isinstance(msg_segs, list) and any(
                isinstance(s, dict)
                and (
                    s.get("type") in ("image", "record", "file")
                    or (
                        sticker_vision_on
                        and s.get("type") == "mface"
                        and (s.get("data") or {}).get("url")
                    )
                )
                for s in msg_segs
            )
            if not plain and not has_media:
                return
            if plain.startswith("/"):
                cmd_word = plain.lower().split(None, 1)[0]
                if cmd_word in ("/compact", "/compress"):
                    dm_role = "admin" if uid in ADMIN_QQ_IDS else "member"
                    self.mark_chat_busy(f"dm:{uid}", actor_role=dm_role)
                await self.client_ws.send_text(raw)
                return
            chat_key = f"dm:{uid}"
            dm_role = "admin" if uid in ADMIN_QQ_IDS else "member"
            if self.is_chat_busy(chat_key):
                if self.awaiting_permission.pop(chat_key, None) is not None:
                    # The turn is waiting for exactly this reply (allow / deny /
                    # allow all). Queuing it would deadlock until the busy TTL.
                    # If cc-connect doesn't take it as an answer it re-sends the
                    # hint, which re-arms awaiting_permission.
                    log(f"Permission reply from {chat_key} passed through busy gate")
                    await self.client_ws.send_text(raw)
                    return
                self.priority_queues[chat_key].append((raw, dm_role))
                return
            self.mark_chat_busy(chat_key, actor_role=dm_role)
            await self.client_ws.send_text(raw)
            return

        # Group message: smart pre-filter + per-group in-flight gate
        if msg_type == "group":
            _maybe_reload_live_guard_flags()
            raw_gid = str(obj.get("group_id", "")).strip()
            if GROUP_WHITELIST_ENABLED and raw_gid not in GROUP_WHITELIST_IDS:
                return
            if is_bot_sender(obj):
                return
            raw_uid = str(obj.get("user_id", "")).strip()
            in_ctx = {
                "is_group": True,
                "is_dm": False,
                "group_id": raw_gid,
                "user_id": raw_uid,
                "chat_key": f"g:{raw_gid}",
                "is_admin": raw_uid in ADMIN_QQ_IDS,
                "bot_qq_id": BOT_QQ_ID,
            }
            obj, raw, should_drop = _apply_ws_inbound_hooks(obj, raw, in_ctx)
            if should_drop:
                return
            if _history_archive_enabled():
                chat_history.record_onebot_event_async(
                    obj, bot_qq_id=BOT_QQ_ID, ignored_bot_ids=IGNORED_BOT_QQ_IDS
                )
            info = analyze_inbound_group_event(obj, self.bot_msg_id_set)
            chat_key = f"g:{info['group_id']}"
            repeat = (
                self.track_repeat(chat_key, obj, info)
                if _plugin_enabled("repeat_chain")
                else ""
            )
            raw = normalize_inbound_mface(obj, raw)

            if info["plain_text"].startswith("/"):
                # Slash commands are handled directly by cc-connect without agy-acp-wrapper,
                # except /compact & /compress which trigger an agent compaction turn.
                cmd_word = info["plain_text"].lower().split(None, 1)[0]
                if cmd_word in ("/compact", "/compress"):
                    cmd_role = "admin" if info["user_id"] in ADMIN_QQ_IDS else "member"
                    self.mark_chat_busy(chat_key, actor_role=cmd_role)
                await self.client_ws.send_text(raw)
                return

            if repeat == "skip":
                # Just another copy of a chain: nothing new for the model to answer
                self.record_skipped(info)
                return
            if repeat == "offer":
                chain = self.repeat_chains[chat_key]
                repeat_perm_ok = bot_permissions.is_allowed(
                    "repeat_follow", actor_role="autonomous", group_id=raw_gid, is_dm=False
                )
                if repeat_perm_ok and isinstance(chain.get("segs"), list):
                    if any(
                        isinstance(s, dict) and s.get("type") in ("image", "mface")
                        for s in chain["segs"]
                    ):
                        repeat_perm_ok = bot_permissions.is_allowed(
                            "send_sticker_reply",
                            actor_role="autonomous",
                            group_id=raw_gid,
                            is_dm=False,
                        )
                if not repeat_perm_ok:
                    self.record_skipped(info)
                    return
                log(
                    f"Repeat chain in {chat_key} ({chain['count']} in a row): "
                    f"asking the model whether to join: {repeat_chain.describe(chain['key'])[:40]}"
                )
                # The chain's first message may still be waiting as ambient chatter
                prev = self.latest_ambient.get(chat_key)
                if prev and prev[3].get("repeat_key") == chain["key"]:
                    self.latest_ambient.pop(chat_key, None)
                    self.record_skipped(prev[3])
                # Not paced like ambient chatter: a chain is over in seconds
                if self.is_chat_busy(chat_key):
                    self.priority_queues[chat_key].append((raw, "autonomous"))
                    return
                self.mark_chat_busy(chat_key, actor_role="autonomous")
                await self.client_ws.send_text(raw)
                return

            if info["ignore"]:
                # Even when ignoring `@other` chatter from real humans, still record
                # meaningful text into per-user long-term memory!
                if info["mentioned_other_only"] and info["user_id"] not in IGNORED_BOT_QQ_IDS:
                    self.record_skipped(info)
                return

            sender_role = "admin" if info["user_id"] in ADMIN_QQ_IDS else "member"

            if info["priority"] or info["soft_call"]:
                # Someone is talking to the bot: lurking back-off no longer applies
                self.ambient_gate.on_addressed(chat_key)

            if info["priority"]:
                if self.is_chat_busy(chat_key):
                    self.priority_queues[chat_key].append((raw, sender_role))
                    return
                self.mark_chat_busy(chat_key, actor_role=sender_role)
                await self.client_ws.send_text(raw)
                return

            # Ambient or soft-call group message
            if not _ambient_listen_enabled() and not info["soft_call"]:
                self.record_skipped(info)
                return

            if self.is_chat_busy(chat_key):
                if info["soft_call"]:
                    self.priority_queues[chat_key].append((raw, sender_role, True, info))
                else:
                    # Keep only the latest ambient message per busy group; the one
                    # it replaces will never be dispatched, so record that one now
                    prev = self.latest_ambient.get(chat_key)
                    if prev:
                        self.record_skipped(prev[3])
                    self.latest_ambient[chat_key] = (raw, False, time.time(), info)
                return

            if not info["soft_call"] and not self.ambient_allow(chat_key, info):
                self.record_skipped(info)
                return

            self.mark_chat_busy(
                chat_key, actor_role=sender_role if info["soft_call"] else "autonomous"
            )
            await self.client_ws.send_text(raw)
            return

        await self.client_ws.send_text(raw)

    async def handle_client_message(self, raw: str):
        try:
            obj = json.loads(raw)
        except Exception:
            if self.upstream_ws and not self.upstream_ws.closed:
                await self.upstream_ws.send_text(raw)
            return

        # Belt-and-suspenders: NEVER let hardcoded busy-queue or idle-reset notices hit QQ!
        if is_blocked_outbound(obj):
            echo = obj.get("echo")
            log(f"Blocked hardcoded cc-connect system notice (echo={echo})")
            if self.client_ws and not self.client_ws.closed:
                ack = {
                    "status": "ok",
                    "retcode": 0,
                    "data": {"message_id": 0},
                }
                if echo is not None:
                    ack["echo"] = echo
                await self.client_ws.send_text(json.dumps(ack, ensure_ascii=False))
            return

        action = obj.get("action")
        echo = obj.get("echo")
        was_repeat_follow = False
        if action in SEND_ACTIONS and _plugin_enabled("repeat_chain"):
            p = obj.get("params") or {}
            group_key = outbound_group_key(action, p)
            if extract_outbound_text(p).strip() == repeat_chain.FOLLOW_TOKEN:
                gid_for_repeat = group_key[2:] if group_key.startswith("g:") else ""
                if not gid_for_repeat or not bot_permissions.is_allowed(
                    "repeat_follow", actor_role="autonomous", group_id=gid_for_repeat, is_dm=False
                ):
                    log(f"Dropped {repeat_chain.FOLLOW_TOKEN} blocked by repeat_follow permission in {group_key or 'DM'}")
                    await self.ack_dropped_send(echo)
                    return
                # The model chose to join a repeat chain: send an exact copy
                segs = self.take_follow_token(group_key)
                if segs is None:
                    log(f"Dropped stray {repeat_chain.FOLLOW_TOKEN} (to {group_key or 'a DM'})")
                    await self.ack_dropped_send(echo)
                    return
                obj = dict(obj)
                obj["params"] = {k: v for k, v in p.items() if k != "auto_escape"}
                obj["params"]["message"] = segs
                raw = json.dumps(obj, ensure_ascii=False)
                was_repeat_follow = True
                log(f"Joining repeat chain in {group_key}")
            elif group_key and self.outbound_repeats_chain(group_key, p):
                log(f"Dropped model reply that repeats a chain already joined in {group_key}")
                await self.ack_dropped_send(echo)
                return
        send_chat_key = ""
        outbound_gid = ""
        outbound_is_dm = False
        outbound_role = "member"
        if action in SEND_ACTIONS:
            params = obj.get("params") or {}
            if action == "send_group_msg" or params.get("group_id"):
                gid = str(params.get("group_id") or "").strip()
                if gid:
                    send_chat_key = f"g:{gid}"
                    outbound_gid = gid
            elif params.get("user_id"):
                uid = str(params.get("user_id") or "").strip()
                if uid:
                    send_chat_key = f"dm:{uid}"
                    outbound_is_dm = True
            outbound_role = self.active_turn_role.get(
                send_chat_key,
                "admin" if (outbound_is_dm and send_chat_key[3:] in ADMIN_QQ_IDS) else "member",
            )
            if send_chat_key.startswith("dm:") and is_permission_prompt(params):
                self.awaiting_permission[send_chat_key] = time.time()
                log(f"Permission prompt sent to {send_chat_key}; next reply skips busy gate")

            # Hard gateway enforcement on outbound @ / @all / reply / image / CQ codes
            filtered_obj, drop_send, guard_notes = qq_actions.filter_outbound_onebot_send(
                obj,
                actor_role=outbound_role,
                group_id=outbound_gid,
                is_dm=outbound_is_dm,
                is_repeat_follow=was_repeat_follow,
            )
            if guard_notes:
                log(
                    f"Permission guard on {send_chat_key or action} ({outbound_role}): "
                    f"{', '.join(guard_notes)}"
                )
            if drop_send:
                log(f"Dropped outbound message after permission guard ({send_chat_key or action})")
                await self.ack_dropped_send(echo)
                return
            if send_chat_key.startswith("g:"):
                self.ambient_gate.on_bot_spoke(send_chat_key, time.time())
            if filtered_obj is not obj:
                obj = filtered_obj
                raw = json.dumps(obj, ensure_ascii=False)

            out_ctx = {
                "action": action,
                "is_group": (not outbound_is_dm) and bool(outbound_gid),
                "is_dm": outbound_is_dm,
                "group_id": outbound_gid,
                "user_id": send_chat_key[3:] if outbound_is_dm else "",
                "chat_key": send_chat_key,
                "actor_role": outbound_role,
                "is_repeat_follow": was_repeat_follow,
                "bot_qq_id": BOT_QQ_ID,
            }
            obj, raw, drop_by_plugin = _apply_ws_outbound_hooks(obj, raw, out_ctx)
            if drop_by_plugin:
                log(f"Dropped outbound message by plugin hook ({send_chat_key or action})")
                await self.ack_dropped_send(echo)
                return
        if action in SEND_ACTIONS and send_chat_key and _history_archive_enabled():
            post_params = obj.get("params") or {}
            if not is_permission_prompt(post_params):
                rendered_out = chat_history.render_onebot_message(
                    post_params.get("message"), bot_qq_id=BOT_QQ_ID
                )
                if rendered_out["text"]:
                    if echo is not None:
                        self.pending_outbound_archive[str(echo)] = (
                            send_chat_key,
                            rendered_out["text"],
                            rendered_out["reply_to_id"],
                            int(time.time()),
                        )
                    else:
                        chat_history.record_outbound_bot_message_async(
                            chat_key=send_chat_key,
                            text=rendered_out["text"],
                            bot_qq_id=BOT_QQ_ID,
                            bot_name=FORWARD_NICKNAME,
                            reply_to_id=rendered_out["reply_to_id"],
                        )
        if action in SEND_ACTIONS and echo is not None:
            self.pending_send_echoes[str(echo)] = (send_chat_key, time.time())
            if send_chat_key:
                self.active_send_counts[send_chat_key] += 1
            if len(self.pending_send_echoes) > 200:
                self._prune_stale_send_echoes(max_age=60.0)

        fwd = (
            build_forward_request(obj, actor_role=outbound_role)
            if _plugin_enabled("long_msg_forward")
            else None
        )
        if fwd is not None:
            if echo is not None:
                if len(self.forward_fallbacks) > 50:
                    cutoff = time.time() - 60.0
                    for k in [k for k, (_, ts) in self.forward_fallbacks.items() if ts < cutoff]:
                        self.forward_fallbacks.pop(k, None)
                self.forward_fallbacks[str(echo)] = (raw, time.time())
            nodes = fwd["params"]["messages"]
            log(
                f"Long reply ({sum(len(n['data']['content'][0]['data']['text']) for n in nodes)} chars) "
                f"-> {fwd['action']} with {len(nodes)} node(s) (echo={echo})"
            )
            raw = json.dumps(fwd, ensure_ascii=False)

        if self.upstream_ws and not self.upstream_ws.closed:
            try:
                await self.upstream_ws.send_text(raw)
            except BaseException:
                self._cleanup_send_echo(echo)
                if echo is not None:
                    self.forward_fallbacks.pop(str(echo), None)
                raise
        else:
            self._cleanup_send_echo(echo)
            if echo is not None:
                self.forward_fallbacks.pop(str(echo), None)

    async def handle_client_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            cws = await asyncio.wait_for(ws_server_handshake(reader, writer), timeout=15.0)
        except Exception as e:
            log(f"Client WS handshake failed: {e}")
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            return

        try:
            uws = await asyncio.wait_for(
                ws_client_connect(UPSTREAM_WS_URL, token=NAPCAT_TOKEN), timeout=15.0
            )
        except Exception as e:
            log(f"Failed to connect upstream NapCat WS ({UPSTREAM_WS_URL}): {e}")
            await cws.close()
            return

        log("cc-connect connected to ws-guard")
        # Close any previous stale connection cleanly ONLY after both handshakes succeeded
        old_cws = self.client_ws
        old_uws = self.upstream_ws
        self.client_ws = cws
        self.upstream_ws = uws
        if old_cws and not old_cws.closed:
            await old_cws.close()
        if old_uws and not old_uws.closed:
            await old_uws.close()

        for t in self.release_tasks.values():
            if t and not t.done():
                t.cancel()
        self.release_tasks.clear()
        self.busy_chats.clear()
        self.active_turn_role.clear()
        self.awaiting_permission.clear()
        self.priority_queues.clear()
        self.latest_ambient.clear()
        self.pending_send_echoes.clear()
        self.pending_outbound_archive.clear()
        self.active_send_counts.clear()
        self.forward_fallbacks.clear()
        log(f"Connected upstream to NapCat WS ({UPSTREAM_WS_URL})")

        async def pump_upstream():
            try:
                while True:
                    msg = await uws.recv_text()
                    await self.handle_upstream_message(msg)
            except WSClosed:
                pass
            except Exception as e:
                log(f"Upstream pump error: {e}")

        async def pump_client():
            try:
                while True:
                    msg = await cws.recv_text()
                    await self.handle_client_message(msg)
            except WSClosed:
                pass
            except Exception as e:
                log(f"Client pump error: {e}")

        t1 = asyncio.create_task(pump_upstream())
        t2 = asyncio.create_task(pump_client())
        done, pending = await asyncio.wait({t1, t2}, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        await cws.close()
        await uws.close()
        if self.client_ws is cws:
            self.client_ws = None
        if self.upstream_ws is uws:
            self.upstream_ws = None
        log("Bridge session closed")


class UDPDoneProtocol(asyncio.DatagramProtocol):
    def __init__(self, bridge: GuardBridge):
        self.bridge = bridge

    def datagram_received(self, data: bytes, addr):
        try:
            obj = json.loads(data.decode("utf-8", errors="ignore"))
            busy_key = obj.get("busy_chat_key")
            if busy_key:
                self.bridge.mark_chat_busy(str(busy_key), actor_role="system")
            chat_key = obj.get("done_chat_key")
            if chat_key:
                silent = bool(obj.get("silent", False))
                # woke_model / ambient are newer fields; an older wrapper omits them,
                # which counts as "no model call" and never grows the back-off.
                self.bridge.ambient_gate.on_turn_done(
                    str(chat_key),
                    silent=silent,
                    woke_model=bool(obj.get("woke_model", False)),
                    ambient=bool(obj.get("ambient", False)),
                )
                self.bridge.schedule_chat_release(str(chat_key), silent=silent)
        except Exception:
            pass

    def error_received(self, exc):
        # Ignore transient Windows IOCP WSAECONNRESET errors on UDP sockets
        pass


async def _memory_idle_flush_loop():
    """Periodically flush idle chat windows with undistilled raw_messages."""
    try:
        # Gentle startup catch-up so pre-restart pending messages flush soon after reboot
        await asyncio.sleep(30.0)
        cycle = 0
        while True:
            cycle += 1
            try:
                if _plugin_enabled("memory_extraction"):
                    res = await asyncio.to_thread(user_memory.run_idle_flush)
                    if isinstance(res, dict) and res.get("windows_flushed", 0) > 0:
                        log(
                            f"Memory idle flush: flushed {res.get('windows_flushed', 0)}/"
                            f"{res.get('windows_checked', 0)} window(s)"
                        )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log(f"Memory idle flush error: {exc}")

            if cycle % 10 == 0:
                try:
                    ckpt = await asyncio.to_thread(user_memory.checkpoint_wal, mode="PASSIVE")
                    if isinstance(ckpt, dict) and ckpt.get("log_frames", 0) > 0:
                        log(
                            "Memory WAL PASSIVE checkpoint: "
                            f"checkpointed={ckpt.get('checkpointed_frames', 0)}/"
                            f"{ckpt.get('log_frames', 0)} frames"
                        )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log(f"Memory WAL checkpoint error: {exc}")

            await asyncio.sleep(180.0)
    except asyncio.CancelledError:
        return


async def _memory_sleep_loop():
    """Periodically check and run the overnight Sleep memory maintenance cycle."""
    try:
        while True:
            await asyncio.sleep(1800.0)
            try:
                sleep_on = _plugin_enabled("memory_sleep")
                keep_cleanup = bool(
                    _plugin_setting("memory_sleep", "keep_rule_cleanup", True)
                )
                if not sleep_on and not keep_cleanup:
                    continue
                ready, reason = await asyncio.to_thread(user_memory.should_run_sleep)
                if ready:
                    if sleep_on:
                        log(f"Starting memory sleep cycle ({reason})")
                        report = await asyncio.to_thread(user_memory.run_sleep_cycle)
                    else:
                        log(
                            f"Starting memory sleep rule-only cleanup ({reason}, "
                            "memory_sleep plugin disabled, llm_fn=False)"
                        )
                        report = await asyncio.to_thread(
                            user_memory.run_sleep_cycle, llm_fn=False
                        )
                    if isinstance(report, dict) and report.get("ran"):
                        log(
                            "Memory sleep cycle finished: "
                            f"merged={report.get('merged_duplicates', 0)}, "
                            f"conflicts_resolved={report.get('conflicts_resolved', 0)}, "
                            f"conflicts_flagged={report.get('conflicts_flagged', 0)}, "
                            f"profiles={report.get('profiles_refreshed', 0)}, "
                            f"archived={report.get('archived_count', 0)}, "
                            f"purged_raw={report.get('purged_raw_count', 0)}, "
                            f"embeddings={report.get('embeddings_backfilled', 0)}, "
                            f"interrupted={report.get('interrupted', False)}"
                        )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log(f"Memory sleep cycle error: {exc}")
    except asyncio.CancelledError:
        return


def _extract_sync_count(res: dict, *keys) -> int:
    for k in keys:
        v = res.get(k)
        if isinstance(v, bool):
            continue
        if isinstance(v, int):
            return v
        if isinstance(v, list):
            return len(v)
    return 0


async def _group_request_sync_loop(bridge: GuardBridge):
    """Periodically sync group join requests from NapCat HTTP API as a fail-safe."""
    if group_requests is None:
        return
    try:
        await asyncio.sleep(4.0)
        while True:
            try:
                res = await asyncio.to_thread(
                    group_requests.sync_from_napcat,
                    napcat_call=_napcat_http_post,
                )
                if isinstance(res, dict):
                    added = _extract_sync_count(
                        res, "synced_new", "added", "new", "new_count", "inserted"
                    )
                    updated = _extract_sync_count(
                        res, "synced_updated", "updated", "updated_count"
                    )
                    if added > 0 or updated > 0:
                        log(f"Group request sync: added={added}, updated={updated}")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log(f"Group request sync error: {exc}")

            disconnected = bridge.upstream_ws is None or bridge.upstream_ws.closed
            await asyncio.sleep(30.0 if disconnected else 120.0)
    except asyncio.CancelledError:
        return


async def async_main():
    bridge = GuardBridge()
    loop = asyncio.get_running_loop()

    transport, _ = await loop.create_datagram_endpoint(
        lambda: UDPDoneProtocol(bridge),
        local_addr=(LISTEN_HOST, UDP_NOTIFY_PORT),
    )
    server = await asyncio.start_server(
        bridge.handle_client_connection, LISTEN_HOST, LISTEN_PORT
    )
    log(
        f"Listening WS on ws://{LISTEN_HOST}:{LISTEN_PORT} -> {UPSTREAM_WS_URL} "
        f"(UDP done notify on {LISTEN_HOST}:{UDP_NOTIFY_PORT})"
    )
    bg_tasks = [
        asyncio.create_task(_memory_idle_flush_loop()),
        asyncio.create_task(_memory_sleep_loop()),
        asyncio.create_task(_group_request_sync_loop(bridge)),
    ]
    try:
        async with server:
            await server.serve_forever()
    finally:
        for task in bg_tasks:
            task.cancel()
        if bg_tasks:
            await asyncio.gather(*bg_tasks, return_exceptions=True)
        transport.close()


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        if _stream is not None and hasattr(_stream, "reconfigure"):
            try:
                _stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    missing = [k for k, v in (("BOT_QQ_ID", BOT_QQ_ID), ("NAPCAT_TOKEN", NAPCAT_TOKEN), ("ADMIN_QQ_IDS", ADMIN_QQ_IDS)) if not v]
    if missing:
        log(f"Missing settings {missing}: check bot.env ({bot_env.ENV_FILE})")
        sys.exit(2)
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        pass
