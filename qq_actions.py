"""QQ OneBot v11 action directives, NapCat HTTP executor, and outbound CQ/segment guard.

Works together with ``bot_permissions.py`` to provide defense-in-depth:
1. ``build_action_prompt_block(...)`` tells the model ONLY the QQ actions currently
   permitted for this turn (actor role + group override) and their exact syntax.
2. ``process_model_output(...)`` parses ``[[QQ:...]]`` directives from the model's
   reply, enforces permissions, limits, target isolation, SSRF/path safety, and
   executes side-effect actions via NapCat HTTP API while converting approved
   inline directives (``reply`` / ``at``) into clean CQ tokens.
3. ``filter_outbound_onebot_send(...)`` runs inside ``onebot-ws-guard.py`` as a
   hard gateway barrier on EVERY outbound ``send_*_msg`` request, blocking any
   unauthorized ``at`` / ``at_all`` / ``reply`` / ``image`` / injected CQ codes
   and converting validated CQ tokens into canonical OneBot v11 segment arrays.
"""

import base64
import os
import re
import sys
import threading
import time
import urllib.parse

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

import bot_env  # noqa: E402,F401
import bot_permissions  # noqa: E402
import onebot_common  # noqa: E402

try:
    import bot_plugins  # noqa: E402
except Exception:
    bot_plugins = None

try:
    import group_requests  # noqa: E402
except Exception:
    group_requests = None


def _plugin_enabled(plugin_id, default=True):
    if bot_plugins is None:
        return default
    try:
        return bool(bot_plugins.is_enabled(plugin_id))
    except Exception:
        return default


def _plugin_setting(plugin_id, key, default=None):
    if bot_plugins is None:
        return default
    try:
        return bot_plugins.get_setting(plugin_id, key, default)
    except Exception:
        return default

MAX_IMAGE_BYTES = int(os.environ.get("MAX_RAW_IMAGE_BYTES", str(12 * 1024 * 1024)))
ALLOWED_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}

# Matches [[QQ:action key=value key2="quoted value"]]
DIRECTIVE_RE = re.compile(r"\[\[\s*QQ\s*:\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*([^\]]*)\]\]")
ARG_TOKEN_RE = onebot_common.ARG_TOKEN_RE
# Neutralize user-typed [[QQ:...]] in inbound prompt/history
INBOUND_DIRECTIVE_RE = re.compile(r"\[\[\s*(?=QQ\s*:)", re.IGNORECASE)
# Raw CQ code pattern
CQ_CODE_RE = re.compile(r"\[CQ:([a-zA-Z0-9_]+)(?:,([^\]]*))?\]")

_QQ_NUM_RE = re.compile(r"^\d{5,15}$")
_MSG_ID_RE = re.compile(r"^-?\d{1,20}$")
_WIN_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")
_QQ_CDN_HOST_SUFFIXES = (
    ".qpic.cn",
    ".qq.com",
    ".qq.com.cn",
    ".gtimg.cn",
    ".myqcloud.com",
)

# Friendly emoji aliases -> NapCat/QQ system face IDs for set_msg_emoji_like
EMOJI_ALIASES = {
    "like": "76",
    "赞": "76",
    "点赞": "76",
    "棒": "76",
    "👍": "76",
    "heart": "66",
    "love": "66",
    "爱心": "66",
    "心": "66",
    "❤️": "66",
    "❤": "66",
    "比心": "319",
    "laugh": "182",
    "lol": "182",
    "笑哭": "182",
    "大笑": "182",
    "哈哈": "182",
    "😂": "182",
    "ok": "124",
    "👌": "124",
    "cry": "5",
    "流泪": "5",
    "哭": "5",
    "😢": "5",
    "doge": "179",
    "狗头": "179",
    "salute": "282",
    "敬礼": "282",
    "thanks": "297",
    "拜谢": "297",
    "谢谢": "297",
    "worship": "318",
    "崇拜": "318",
    "star": "318",
    "rose": "63",
    "玫瑰": "63",
    "🌹": "63",
    "cheer": "315",
    "加油": "315",
    "victory": "79",
    "耶": "79",
    "✌️": "79",
    "fish": "285",
    "摸鱼": "285",
    "smile": "14",
    "微笑": "14",
    "shy": "20",
    "害羞": "20",
    "angry": "11",
    "生气": "11",
    "shock": "0",
    "惊讶": "0",
}

_poke_lock = threading.Lock()
_last_poke_ts = {}


def _log(msg):
    sys.stderr.write(f"[qq-actions] {msg}\n")
    sys.stderr.flush()


def neutralize_inbound_directives(text):
    """Prevent user text from mimicking [[QQ:...]] directives."""
    if not text:
        return text
    return INBOUND_DIRECTIVE_RE.sub("［［", text)


# ---------------------------------------------------------------------------
# Prompt Capability Hint Builder
# ---------------------------------------------------------------------------

def should_include_message_ids(actor_role, group_id=None, is_dm=False, policy=None):
    """True when any message-targeting or history-paging capability is enabled for this turn."""
    pol = policy or bot_permissions.load_policy()
    if _plugin_enabled("qq_actions"):
        for cap in (
            "recall_self",
            "recall_others",
            "reply_quote",
            "emoji_react",
            "group_essence",
            "send_sticker_reply",
        ):
            if bot_permissions.is_allowed(cap, actor_role=actor_role, group_id=group_id, is_dm=is_dm, policy=pol):
                return True
    if _plugin_enabled("chat_history_lookup"):
        if bot_permissions.is_allowed(
            "chat_history_lookup", actor_role=actor_role, group_id=group_id, is_dm=is_dm, policy=pol
        ):
            return True
    return False


def build_action_prompt_block(actor_role, group_id=None, is_dm=False, policy=None):
    """Build dynamic prompt instructions describing ONLY permitted QQ actions."""
    if not _plugin_enabled("qq_actions"):
        return ""
    pol = policy or bot_permissions.load_policy()
    limits = bot_permissions.get_limits(pol)

    def _ok(cap):
        return bot_permissions.is_allowed(
            cap, actor_role=actor_role, group_id=group_id, is_dm=is_dm, policy=pol
        )

    allowed_specs = []
    denied_names = []

    if _ok("recall_self") or _ok("recall_others"):
        if _ok("recall_self") and _ok("recall_others"):
            allowed_specs.append("撤回消息：`[[QQ:recall target=last]]`（撤回自己刚发的一条）或 `[[QQ:recall msg_id=消息ID]]`（可撤回自己或群友消息）")
        elif _ok("recall_self"):
            allowed_specs.append("撤回自己消息：`[[QQ:recall target=last]]` 或 `[[QQ:recall msg_id=自己的消息ID]]`（仅限撤回你自己的消息）")
            denied_names.append("撤回他人消息")
        else:
            allowed_specs.append("撤回他人消息：`[[QQ:recall msg_id=消息ID]]`")
            denied_names.append("撤回自己消息")
    else:
        denied_names.append("撤回消息")

    if not is_dm:
        if _ok("at_member"):
            allowed_specs.append(f"@指定群友：`[[QQ:at qq=QQ号]]`（写在回复正文里对应位置，单条最多 {limits['max_at_per_msg']} 人；平时不必刻意 @，仅在点名提醒或需要明确对象时用）")
        else:
            denied_names.append("@指定群友")

        if _ok("at_all"):
            allowed_specs.append("@全体成员：`[[QQ:at qq=all]]`（仅管理员明确要求时使用）")
        else:
            denied_names.append("@全体成员")

    if _ok("reply_quote"):
        allowed_specs.append("引用回复：`[[QQ:reply msg_id=消息ID]]`（放在回复最前面，仅在上下文交错需要指明回复哪条时使用）")
    else:
        denied_names.append("引用回复")

    img_modes = []
    if _ok("send_image_url"):
        img_modes.append('网络图片 `[[QQ:image url="公网图片URL"]]`')
    else:
        denied_names.append("发送网络图片")
    if _ok("send_image_local"):
        dirs_hint = "/".join(limits.get("allowed_local_image_dirs") or ["workspace"])
        img_modes.append(f'本地图片 `[[QQ:image file="相对路径"]]`（仅限 {dirs_hint} 目录内真实图片）')
    else:
        denied_names.append("发送本地图片")
    if _ok("send_sticker_reply"):
        img_modes.append("转发当前聊天里的表情包/图片 `[[QQ:sticker msg_id=消息ID]]`")
    else:
        denied_names.append("发表情包/跟图")
    if img_modes:
        allowed_specs.append(f"发图（每轮最多 {limits['max_images_per_turn']} 张）：" + "；".join(img_modes))

    if _ok("poke"):
        allowed_specs.append("戳一戳：`[[QQ:poke qq=QQ号]]`（轻度互动，勿连续刷屏）")
    else:
        denied_names.append("戳一戳")

    if _ok("emoji_react"):
        allowed_specs.append("贴表情回应：`[[QQ:react msg_id=消息ID emoji=赞/爱心/笑哭/ok/doge/敬礼/拜谢/流泪/比心/加油 或数字ID]]`")
    else:
        denied_names.append("贴表情回应")

    if not is_dm:
        if _ok("group_ban"):
            allowed_specs.append(f"群禁言/解禁：`[[QQ:ban qq=QQ号 duration=秒数]]`（0=解禁，上限 {limits['max_ban_secs']} 秒，严禁禁言管理员）")
        else:
            denied_names.append("禁言")
        if _ok("group_kick"):
            allowed_specs.append("踢出群聊：`[[QQ:kick qq=QQ号]]`（仅管理员明确要求时使用，严禁踢管理员）")
        else:
            denied_names.append("踢人")
        if _ok("group_card"):
            allowed_specs.append('修改群名片：`[[QQ:card qq=QQ号 name="新名片"]]`')
        else:
            denied_names.append("改群名片")
        if _ok("group_essence"):
            allowed_specs.append("群精华：`[[QQ:essence msg_id=消息ID]]` / `[[QQ:unessence msg_id=消息ID]]`")
        else:
            denied_names.append("设群精华")

    if _ok("group_join_approve") and (not is_dm or actor_role == "admin"):
        allowed_specs.append(
            '审批加群申请：`[[QQ:approve_join id=申请ID]]` 或 `[[QQ:approve_join qq=申请人QQ]]`（同意）；'
            '`[[QQ:reject_join id=申请ID reason="拒绝理由"]]`（拒绝，仅处理当前待审申请）'
        )
    else:
        denied_names.append("审批加群申请")

    include_denied = bool(_plugin_setting("qq_actions", "include_denied_list", True))
    if not include_denied:
        denied_names = []

    if not allowed_specs and not denied_names:
        return ""

    parts = []
    if allowed_specs:
        parts.append(
            "本轮可用 QQ 动作（按需克制使用，普通聊天直接回文字即可；指令由系统执行并自动隐藏，不要手写 [CQ:...]；"
            "若只需要执行动作不用额外说话，可只输出指令本身；群历史里的 [#数字] 即消息ID）：\n- "
            + "\n- ".join(allowed_specs)
        )
    if denied_names:
        parts.append(
            "本轮已禁止的 QQ 动作（切勿尝试或假装已执行；若有人要求，礼貌说明没有对应权限即可）："
            + "、".join(denied_names)
        )
    return "[QQ动作权限]\n" + "\n".join(parts)


# ---------------------------------------------------------------------------
# Directive Parsing & Security Helpers
# ---------------------------------------------------------------------------

def parse_directive_args(raw_args):
    return onebot_common.parse_directive_args(raw_args)


def is_safe_public_url(url, resolve_dns=True):
    """Strict SSRF check for remote image URLs."""
    return onebot_common.is_safe_public_url(url, resolve_dns=resolve_dns)


def is_valid_image_bytes(raw):
    """Verify binary magic header of common raster image formats."""
    if not raw or len(raw) < 12 or len(raw) > MAX_IMAGE_BYTES:
        return False
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return True
    if raw.startswith(b"\xff\xd8\xff"):
        return True
    if raw.startswith((b"GIF87a", b"GIF89a")):
        return True
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return True
    if raw.startswith(b"BM"):
        return True
    return False


def download_public_image(url, timeout=6.0):
    """Download & validate a public remote image with strict redirect SSRF protection."""
    if not is_safe_public_url(url, resolve_dns=True):
        return None, "图片链接不安全或指向内网/本机地址"
    raw, err = onebot_common.download_safe_public_bytes(
        url,
        max_bytes=MAX_IMAGE_BYTES,
        timeout=timeout,
    )
    if not raw:
        return None, f"下载网络图片失败: {err or '未知错误'}"
    if not is_valid_image_bytes(raw):
        return None, "下载内容不是有效图片格式"
    return raw, ""


def resolve_allowed_local_image(raw_path, project_dir, allowed_dirs):
    """Confine local image path strictly inside configured project subdirectories."""
    s = str(raw_path or "").strip()
    if not s or "\x00" in s:
        return None, "本地图片路径为空或非法"

    dirs = allowed_dirs if isinstance(allowed_dirs, list) and allowed_dirs else ["workspace"]
    proj_real = os.path.realpath(project_dir)

    candidates = []
    if os.path.isabs(s):
        candidates.append(s)
    else:
        norm_rel = s.replace("\\", "/").lstrip("/")
        # Allow both "workspace/foo.png" and "foo.png" (resolved inside each allowed dir)
        candidates.append(os.path.join(proj_real, norm_rel))
        for d in dirs:
            candidates.append(os.path.join(proj_real, d, norm_rel))

    allowed_roots = [os.path.realpath(os.path.join(proj_real, d)) for d in dirs]

    for cand in candidates:
        real_cand = os.path.realpath(cand)
        confined = False
        for root in allowed_roots:
            try:
                if os.path.commonpath([os.path.normcase(root), os.path.normcase(real_cand)]) == os.path.normcase(root):
                    confined = True
                    break
            except ValueError:
                continue
        if not confined:
            continue
        if not os.path.isfile(real_cand):
            continue
        ext = os.path.splitext(real_cand)[1].lower()
        if ext not in ALLOWED_IMAGE_EXTS:
            return None, f"不支持的本地图片格式: {ext or '未知'}"
        try:
            size = os.path.getsize(real_cand)
            if size <= 0 or size > MAX_IMAGE_BYTES:
                return None, "本地图片为空或超过体积上限"
            with open(real_cand, "rb") as f:
                data = f.read(MAX_IMAGE_BYTES + 1)
            if not is_valid_image_bytes(data):
                return None, "文件内容不是有效图片"
            return data, ""
        except OSError as exc:
            return None, f"读取本地图片失败: {exc}"

    return None, "本地图片不存在或超出允许目录范围"


def _default_napcat_post(endpoint, payload, timeout=4.0):
    base_url = os.environ.get("NAPCAT_HTTP_URL", "http://127.0.0.1:3000").rstrip("/")
    token = os.environ.get("NAPCAT_TOKEN", "")
    return onebot_common.napcat_api_post(base_url, token, endpoint, payload, timeout=timeout)


def _clean_qq(val):
    return str(val or "").strip().lstrip("@").strip()


def _clean_msg_id(val):
    return str(val or "").strip().lstrip("#").strip()


def _verify_msg_in_chat(msg_id_str, is_group, group_id, dm_user_id, bot_qq_id, call_api):
    """Fetch message via NapCat get_msg and verify it belongs to the current chat."""
    msg_id_str = _clean_msg_id(msg_id_str)
    if not _MSG_ID_RE.match(msg_id_str):
        return False, None, "无效的消息 ID"
    mid = int(msg_id_str)
    ok, data, err = call_api("get_msg", {"message_id": mid}, timeout=3.0)
    if not ok or not isinstance(data, dict):
        return False, None, f"查询消息失败({err or '未找到'})"

    mtype = str(data.get("message_type") or "").strip().lower()
    if is_group:
        gid = str(group_id or "").strip()
        mgid = str(data.get("group_id") or "").strip()
        if not gid or mgid != gid:
            return False, None, "目标消息不属于当前群聊，已拒绝跨群操作"
        return True, data, ""
    else:
        if mtype == "group" or data.get("group_id"):
            return False, None, "目标消息属于群聊，不能在私聊中操作"
        if not onebot_common.verify_dm_msg_ownership(data, dm_user_id, bot_qq_id):
            return False, None, "目标消息不属于当前私聊，已拒绝跨会话操作"
        return True, data, ""


def _find_last_bot_msg_id(is_group, group_id, dm_user_id, bot_qq_id, call_api):
    if not bot_qq_id:
        return None
    if is_group and group_id:
        ok, data, _ = call_api(
            "get_group_msg_history", {"group_id": int(group_id), "count": 25}, timeout=3.0
        )
    elif dm_user_id:
        ok, data, _ = call_api(
            "get_friend_msg_history", {"user_id": int(dm_user_id), "count": 20}, timeout=3.0
        )
    else:
        return None
    if not ok or not isinstance(data, dict):
        return None
    msgs = data.get("messages")
    if not isinstance(msgs, list):
        return None
    for m in reversed(msgs):
        if str(m.get("user_id") or "") == str(bot_qq_id):
            mid = str(m.get("message_id") or "").strip()
            if _MSG_ID_RE.match(mid):
                return mid
    return None


def _extract_first_image_url(msg_obj):
    segs = msg_obj.get("message") if isinstance(msg_obj, dict) else None
    if not isinstance(segs, list):
        return ""
    for seg in segs:
        if not isinstance(seg, dict):
            continue
        st = seg.get("type", "")
        dt = seg.get("data") or {}
        if st in ("image", "mface"):
            url = str(dt.get("url") or "").strip()
            if url:
                return url
    return ""


def _resolve_emoji_id(raw_val):
    s = str(raw_val or "").strip()
    if not s:
        return ""
    if s.isdigit():
        return s
    low = s.lower()
    if low in EMOJI_ALIASES:
        return EMOJI_ALIASES[low]
    if s in EMOJI_ALIASES:
        return EMOJI_ALIASES[s]
    # Single Unicode emoji character -> decimal codepoint (supported by NapCat)
    if len(s) == 1 and ord(s) > 127:
        return str(ord(s))
    return ""


def _send_image_bytes(img_bytes, is_group, group_id, dm_user_id, call_api):
    b64 = base64.b64encode(img_bytes).decode("ascii")
    segs = [{"type": "image", "data": {"file": "base64://" + b64}}]
    if is_group and group_id:
        return call_api("send_group_msg", {"group_id": int(group_id), "message": segs}, timeout=12.0)
    if dm_user_id:
        return call_api("send_private_msg", {"user_id": int(dm_user_id), "message": segs}, timeout=12.0)
    return False, None, "缺少发送目标"


def _strip_raw_cq_codes(text):
    """Remove any unapproved raw [CQ:...] written directly by the model."""
    if not text or "[CQ:" not in text:
        return text
    return CQ_CODE_RE.sub("", text)


def _collapse_extra_blank_lines(text):
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ---------------------------------------------------------------------------
# Main Model-Output Directive Processor (runs in agy-acp-wrapper.py)
# ---------------------------------------------------------------------------

def process_model_output(
    text,
    actor_role,
    is_group,
    group_id="",
    dm_user_id="",
    bot_qq_id="",
    admin_qq_ids=None,
    project_dir=_SCRIPT_DIR,
    policy=None,
    napcat_call=None,
    text_postprocessor=None,
):
    """Parse, authorize, and execute ``[[QQ:...]]`` directives in a bot reply.

    Args:
        text: Raw model output text.
        actor_role: ``"admin"`` | ``"member"`` | ``"autonomous"``.
        is_group: Whether this turn is in a QQ group.
        group_id: Group ID string (when ``is_group`` is True).
        dm_user_id: User ID string (when ``is_group`` is False).
        bot_qq_id: Bot's own QQ ID string.
        admin_qq_ids: Iterable/set of admin QQ ID strings.
        project_dir: Root directory of the bot project.
        policy: Optional policy dict (hot-loaded from disk when None).
        napcat_call: Optional callable ``(endpoint, payload, timeout=...) -> (ok, data, err)``.
        text_postprocessor: Optional callable ``(plain_text) -> str`` applied ONLY to
            the non-directive prose (never corrupting validated CQ tokens).

    Returns dict with keys:
        final_text, executed, blocked, failed, any_action_executed
    """
    pol = policy if isinstance(policy, dict) else bot_permissions.load_policy()
    limits = bot_permissions.get_limits(pol)
    call_api = napcat_call or _default_napcat_post
    admins = {str(x).strip() for x in (admin_qq_ids or ()) if str(x).strip()}
    bot_qq = str(bot_qq_id or "").strip()
    gid = str(group_id or "").strip() if is_group else ""
    uid = str(dm_user_id or "").strip()
    is_dm = not is_group

    executed = []
    blocked = []
    failed = []

    raw_text = str(text or "")

    # Strip raw model-emitted [CQ:...] BEFORE we inject our own validated CQ tokens
    cleaned_input = _strip_raw_cq_codes(raw_text)

    if not _plugin_enabled("qq_actions"):
        prose = _collapse_extra_blank_lines(DIRECTIVE_RE.sub("", cleaned_input))
        out_text = text_postprocessor(prose) if (prose and text_postprocessor) else prose.strip()
        return {
            "final_text": out_text,
            "executed": executed,
            "blocked": blocked,
            "failed": failed,
            "any_action_executed": False,
        }

    has_directives = bool(DIRECTIVE_RE.search(raw_text))

    if not has_directives:
        out_text = text_postprocessor(cleaned_input) if text_postprocessor else cleaned_input.strip()
        return {
            "final_text": out_text,
            "executed": executed,
            "blocked": blocked,
            "failed": failed,
            "any_action_executed": False,
        }

    max_actions = int(limits.get("max_actions_per_turn", 3))
    max_at = int(limits.get("max_at_per_msg", 3))
    max_images = int(limits.get("max_images_per_turn", 2))
    max_ban_secs = int(limits.get("max_ban_secs", 86400))
    poke_cd = int(limits.get("poke_cooldown_secs", 15))
    allowed_dirs = limits.get("allowed_local_image_dirs") or ["workspace"]

    side_actions_used = 0
    images_sent = 0
    at_count = 0
    reply_cq = ""
    cq_placeholders = []
    side_effect_queue = []

    def _perm(cap_id):
        return bot_permissions.check_permission(
            cap_id,
            actor_role=actor_role,
            group_id=gid or None,
            is_dm=is_dm,
            policy=pol,
        )

    def _replace_directive(match):
        nonlocal at_count, reply_cq
        action = match.group(1).strip().lower()
        args = parse_directive_args(match.group(2))

        if action == "at":
            target_qq = _clean_qq(args.get("qq") or args.get("user_id") or args.get("target") or "").lower()
            if target_qq == "all":
                res = _perm("at_all")
                if not res["allowed"]:
                    blocked.append({"action": "at_all", "reason": res["reason"]})
                    _log(f"BLOCKED at_all ({actor_role}@{gid or 'dm'}): {res['reason']}")
                    return ""
                token = "[CQ:at,qq=all]"
                idx = len(cq_placeholders)
                cq_placeholders.append(token)
                executed.append({"action": "at_all", "target": "all"})
                return f"\u0000QQCQ{idx}\u0000"
            if not _QQ_NUM_RE.match(target_qq):
                failed.append({"action": "at", "reason": f"无效的 QQ 号: {target_qq}"})
                return ""
            res = _perm("at_member")
            if not res["allowed"]:
                blocked.append({"action": "at_member", "target": target_qq, "reason": res["reason"]})
                _log(f"BLOCKED at_member qq={target_qq} ({actor_role}@{gid or 'dm'}): {res['reason']}")
                return ""
            if at_count >= max_at:
                blocked.append({"action": "at_member", "target": target_qq, "reason": f"超过单条消息 @ 上限 ({max_at})"})
                _log(f"BLOCKED at_member qq={target_qq}: exceeded max_at_per_msg={max_at}")
                return ""
            at_count += 1
            token = f"[CQ:at,qq={target_qq}]"
            idx = len(cq_placeholders)
            cq_placeholders.append(token)
            executed.append({"action": "at_member", "target": target_qq})
            return f"\u0000QQCQ{idx}\u0000"

        if action == "reply":
            mid = _clean_msg_id(args.get("msg_id") or args.get("id") or "")
            res = _perm("reply_quote")
            if not res["allowed"]:
                blocked.append({"action": "reply_quote", "msg_id": mid, "reason": res["reason"]})
                _log(f"BLOCKED reply_quote ({actor_role}@{gid or 'dm'}): {res['reason']}")
                return ""
            if not _MSG_ID_RE.match(mid):
                failed.append({"action": "reply_quote", "reason": f"无效的消息 ID: {mid}"})
                return ""
            if not reply_cq:
                ok_msg, _mobj, err = _verify_msg_in_chat(mid, is_group, gid, uid, bot_qq, call_api)
                if not ok_msg:
                    failed.append({"action": "reply_quote", "msg_id": mid, "reason": err})
                    _log(f"FAILED reply_quote msg_id={mid}: {err}")
                    return ""
                reply_cq = f"[CQ:reply,id={mid}]"
                executed.append({"action": "reply_quote", "msg_id": mid})
            return ""

        if action == "history":
            # Handled internally by agy-acp-wrapper's history lookup loop; silently strip
            # any residual directive so it never leaks to QQ or triggers a false permission block.
            return ""

        # All other directives are side-effect actions executed via NapCat HTTP API
        side_effect_queue.append((action, args))
        return ""

    prose = DIRECTIVE_RE.sub(_replace_directive, cleaned_input)
    prose = _collapse_extra_blank_lines(prose)

    # Execute side-effect actions in order
    chat_key = f"g:{gid}" if is_group else f"dm:{uid}"
    for action, args in side_effect_queue:
        if side_actions_used >= max_actions:
            reason = f"超过单轮最大 QQ 动作数上限 ({max_actions})"
            blocked.append({"action": action, "reason": reason})
            _log(f"BLOCKED {action}: {reason}")
            continue

        if action == "recall":
            target = str(args.get("target") or "").strip().lower()
            mid = _clean_msg_id(args.get("msg_id") or args.get("id") or "")
            if target == "last" or not mid:
                res = _perm("recall_self")
                if not res["allowed"]:
                    blocked.append({"action": "recall_self", "reason": res["reason"]})
                    _log(f"BLOCKED recall_self ({actor_role}@{chat_key}): {res['reason']}")
                    continue
                mid = _find_last_bot_msg_id(is_group, gid, uid, bot_qq, call_api)
                if not mid:
                    failed.append({"action": "recall_self", "reason": "未找到机器人近期发出的消息"})
                    continue
                ok, _, err = call_api("delete_msg", {"message_id": int(mid)}, timeout=4.0)
                if ok:
                    side_actions_used += 1
                    executed.append({"action": "recall_self", "msg_id": mid})
                    _log(f"EXECUTED recall_self msg_id={mid} ({actor_role}@{chat_key})")
                else:
                    failed.append({"action": "recall_self", "msg_id": mid, "reason": err})
                    _log(f"FAILED recall_self msg_id={mid}: {err}")
            else:
                ok_msg, mobj, err = _verify_msg_in_chat(mid, is_group, gid, uid, bot_qq, call_api)
                if not ok_msg:
                    failed.append({"action": "recall", "msg_id": mid, "reason": err})
                    _log(f"FAILED recall msg_id={mid}: {err}")
                    continue
                msg_author = str(mobj.get("user_id") or "").strip()
                is_own = bool(bot_qq and msg_author == bot_qq)
                cap_id = "recall_self" if is_own else "recall_others"
                res = _perm(cap_id)
                if not res["allowed"]:
                    blocked.append({"action": cap_id, "msg_id": mid, "reason": res["reason"]})
                    _log(f"BLOCKED {cap_id} msg_id={mid} ({actor_role}@{chat_key}): {res['reason']}")
                    continue
                if not is_own and actor_role != "admin" and msg_author in admins:
                    reason = "非管理员不可撤回管理员消息"
                    blocked.append({"action": "recall_others", "msg_id": mid, "reason": reason})
                    _log(f"BLOCKED recall_others msg_id={mid}: {reason}")
                    continue
                ok, _, err = call_api("delete_msg", {"message_id": int(mid)}, timeout=4.0)
                if ok:
                    side_actions_used += 1
                    executed.append({"action": cap_id, "msg_id": mid})
                    _log(f"EXECUTED {cap_id} msg_id={mid} ({actor_role}@{chat_key})")
                else:
                    failed.append({"action": cap_id, "msg_id": mid, "reason": err})
                    _log(f"FAILED {cap_id} msg_id={mid}: {err}")

        elif action == "poke":
            res = _perm("poke")
            if not res["allowed"]:
                blocked.append({"action": "poke", "reason": res["reason"]})
                _log(f"BLOCKED poke ({actor_role}@{chat_key}): {res['reason']}")
                continue
            target_qq = _clean_qq(args.get("qq") or args.get("user_id") or (uid if is_dm else ""))
            if not _QQ_NUM_RE.match(target_qq):
                failed.append({"action": "poke", "reason": f"无效的目标 QQ: {target_qq}"})
                continue
            if is_dm and target_qq != uid:
                blocked.append({"action": "poke", "reason": "私聊仅允许戳当前对话对象"})
                continue
            now_mono = time.monotonic()
            with _poke_lock:
                last_ts = _last_poke_ts.get(chat_key, -1e9)
                if poke_cd > 0 and (now_mono - last_ts) < poke_cd:
                    blocked.append({"action": "poke", "reason": f"戳一戳冷却中（{poke_cd}秒）"})
                    _log(f"BLOCKED poke ({chat_key}): cooldown {poke_cd}s")
                    continue
                _last_poke_ts[chat_key] = now_mono
            if is_group:
                ok, _, err = call_api("group_poke", {"group_id": int(gid), "user_id": int(target_qq)}, timeout=4.0)
                if not ok:
                    ok, _, err = call_api("send_poke", {"group_id": int(gid), "user_id": int(target_qq)}, timeout=4.0)
            else:
                ok, _, err = call_api("friend_poke", {"user_id": int(target_qq)}, timeout=4.0)
                if not ok:
                    ok, _, err = call_api("send_poke", {"user_id": int(target_qq)}, timeout=4.0)
            if ok:
                side_actions_used += 1
                executed.append({"action": "poke", "target": target_qq})
                _log(f"EXECUTED poke target={target_qq} ({actor_role}@{chat_key})")
            else:
                failed.append({"action": "poke", "target": target_qq, "reason": err})
                _log(f"FAILED poke target={target_qq}: {err}")

        elif action == "react":
            res = _perm("emoji_react")
            if not res["allowed"]:
                blocked.append({"action": "emoji_react", "reason": res["reason"]})
                _log(f"BLOCKED emoji_react ({actor_role}@{chat_key}): {res['reason']}")
                continue
            mid = _clean_msg_id(args.get("msg_id") or args.get("id") or "")
            eid = _resolve_emoji_id(args.get("emoji") or args.get("emoji_id") or "")
            if not eid:
                failed.append({"action": "emoji_react", "reason": "未识别的表情 ID"})
                continue
            ok_msg, _, err = _verify_msg_in_chat(mid, is_group, gid, uid, bot_qq, call_api)
            if not ok_msg:
                failed.append({"action": "emoji_react", "msg_id": mid, "reason": err})
                continue
            ok, _, err = call_api(
                "set_msg_emoji_like",
                {"message_id": int(mid), "emoji_id": str(eid), "set": True},
                timeout=4.0,
            )
            if ok:
                side_actions_used += 1
                executed.append({"action": "emoji_react", "msg_id": mid, "emoji_id": eid})
                _log(f"EXECUTED emoji_react msg_id={mid} emoji={eid} ({actor_role}@{chat_key})")
            else:
                failed.append({"action": "emoji_react", "msg_id": mid, "reason": err})
                _log(f"FAILED emoji_react msg_id={mid}: {err}")

        elif action == "image":
            if images_sent >= max_images:
                blocked.append({"action": "image", "reason": f"超过单轮发图上限 ({max_images} 张)"})
                continue
            img_url = str(args.get("url") or "").strip()
            img_file = str(args.get("file") or args.get("path") or "").strip()
            if img_url:
                res = _perm("send_image_url")
                if not res["allowed"]:
                    blocked.append({"action": "send_image_url", "reason": res["reason"]})
                    _log(f"BLOCKED send_image_url ({actor_role}@{chat_key}): {res['reason']}")
                    continue
                img_bytes, err = download_public_image(img_url)
                if not img_bytes:
                    failed.append({"action": "send_image_url", "reason": err})
                    _log(f"FAILED send_image_url: {err}")
                    continue
                ok, _, err = _send_image_bytes(img_bytes, is_group, gid, uid, call_api)
                if ok:
                    side_actions_used += 1
                    images_sent += 1
                    executed.append({"action": "send_image_url", "url": img_url})
                    _log(f"EXECUTED send_image_url ({actor_role}@{chat_key})")
                else:
                    failed.append({"action": "send_image_url", "reason": err})
                    _log(f"FAILED send_image_url send: {err}")
            elif img_file:
                res = _perm("send_image_local")
                if not res["allowed"]:
                    blocked.append({"action": "send_image_local", "reason": res["reason"]})
                    _log(f"BLOCKED send_image_local ({actor_role}@{chat_key}): {res['reason']}")
                    continue
                img_bytes, err = resolve_allowed_local_image(img_file, project_dir, allowed_dirs)
                if not img_bytes:
                    failed.append({"action": "send_image_local", "reason": err})
                    _log(f"FAILED send_image_local ({img_file}): {err}")
                    continue
                ok, _, err = _send_image_bytes(img_bytes, is_group, gid, uid, call_api)
                if ok:
                    side_actions_used += 1
                    images_sent += 1
                    executed.append({"action": "send_image_local", "file": img_file})
                    _log(f"EXECUTED send_image_local file={img_file} ({actor_role}@{chat_key})")
                else:
                    failed.append({"action": "send_image_local", "reason": err})
                    _log(f"FAILED send_image_local send: {err}")
            else:
                failed.append({"action": "image", "reason": "缺少 url 或 file 参数"})

        elif action == "sticker":
            if images_sent >= max_images:
                blocked.append({"action": "send_sticker_reply", "reason": f"超过单轮发图上限 ({max_images} 张)"})
                continue
            res = _perm("send_sticker_reply")
            if not res["allowed"]:
                blocked.append({"action": "send_sticker_reply", "reason": res["reason"]})
                _log(f"BLOCKED send_sticker_reply ({actor_role}@{chat_key}): {res['reason']}")
                continue
            mid = _clean_msg_id(args.get("msg_id") or args.get("id") or "")
            ok_msg, mobj, err = _verify_msg_in_chat(mid, is_group, gid, uid, bot_qq, call_api)
            if not ok_msg:
                failed.append({"action": "send_sticker_reply", "msg_id": mid, "reason": err})
                continue
            s_url = _extract_first_image_url(mobj)
            if not s_url:
                failed.append({"action": "send_sticker_reply", "msg_id": mid, "reason": "目标消息不含图片或表情包"})
                continue
            img_bytes, err = download_public_image(s_url)
            if not img_bytes:
                failed.append({"action": "send_sticker_reply", "msg_id": mid, "reason": err})
                continue
            ok, _, err = _send_image_bytes(img_bytes, is_group, gid, uid, call_api)
            if ok:
                side_actions_used += 1
                images_sent += 1
                executed.append({"action": "send_sticker_reply", "msg_id": mid})
                _log(f"EXECUTED send_sticker_reply msg_id={mid} ({actor_role}@{chat_key})")
            else:
                failed.append({"action": "send_sticker_reply", "msg_id": mid, "reason": err})

        elif action == "ban":
            res = _perm("group_ban")
            if not res["allowed"]:
                blocked.append({"action": "group_ban", "reason": res["reason"]})
                _log(f"BLOCKED group_ban ({actor_role}@{chat_key}): {res['reason']}")
                continue
            target_qq = _clean_qq(args.get("qq") or args.get("user_id") or "")
            if not _QQ_NUM_RE.match(target_qq):
                failed.append({"action": "group_ban", "reason": f"无效的目标 QQ: {target_qq}"})
                continue
            if target_qq == bot_qq or target_qq in admins:
                blocked.append({"action": "group_ban", "target": target_qq, "reason": "禁止对管理员或机器人自身执行禁言"})
                _log(f"BLOCKED group_ban target={target_qq}: protected user")
                continue
            try:
                dur = int(float(args.get("duration", "600")))
            except (TypeError, ValueError):
                dur = 600
            dur = max(0, min(max_ban_secs, dur))
            ok, _, err = call_api(
                "set_group_ban",
                {"group_id": int(gid), "user_id": int(target_qq), "duration": dur},
                timeout=4.0,
            )
            if ok:
                side_actions_used += 1
                executed.append({"action": "group_ban", "target": target_qq, "duration": dur})
                _log(f"EXECUTED group_ban target={target_qq} dur={dur}s ({actor_role}@{chat_key})")
            else:
                failed.append({"action": "group_ban", "target": target_qq, "reason": err})
                _log(f"FAILED group_ban target={target_qq}: {err}")

        elif action == "kick":
            res = _perm("group_kick")
            if not res["allowed"]:
                blocked.append({"action": "group_kick", "reason": res["reason"]})
                _log(f"BLOCKED group_kick ({actor_role}@{chat_key}): {res['reason']}")
                continue
            target_qq = _clean_qq(args.get("qq") or args.get("user_id") or "")
            if not _QQ_NUM_RE.match(target_qq):
                failed.append({"action": "group_kick", "reason": f"无效的目标 QQ: {target_qq}"})
                continue
            if target_qq == bot_qq or target_qq in admins:
                blocked.append({"action": "group_kick", "target": target_qq, "reason": "禁止踢出管理员或机器人自身"})
                _log(f"BLOCKED group_kick target={target_qq}: protected user")
                continue
            reject_add = str(args.get("reject", "0")).strip().lower() in ("1", "true", "yes")
            ok, _, err = call_api(
                "set_group_kick",
                {"group_id": int(gid), "user_id": int(target_qq), "reject_add_request": reject_add},
                timeout=4.0,
            )
            if ok:
                side_actions_used += 1
                executed.append({"action": "group_kick", "target": target_qq})
                _log(f"EXECUTED group_kick target={target_qq} ({actor_role}@{chat_key})")
            else:
                failed.append({"action": "group_kick", "target": target_qq, "reason": err})
                _log(f"FAILED group_kick target={target_qq}: {err}")

        elif action == "card":
            res = _perm("group_card")
            if not res["allowed"]:
                blocked.append({"action": "group_card", "reason": res["reason"]})
                _log(f"BLOCKED group_card ({actor_role}@{chat_key}): {res['reason']}")
                continue
            target_qq = _clean_qq(args.get("qq") or args.get("user_id") or "")
            card_name = str(args.get("name") or args.get("card") or "").strip()[:60]
            if not _QQ_NUM_RE.match(target_qq):
                failed.append({"action": "group_card", "reason": f"无效的目标 QQ: {target_qq}"})
                continue
            if target_qq == bot_qq or target_qq in admins:
                blocked.append({"action": "group_card", "target": target_qq, "reason": "禁止修改管理员或机器人自身的群名片"})
                _log(f"BLOCKED group_card target={target_qq}: protected user")
                continue
            ok, _, err = call_api(
                "set_group_card",
                {"group_id": int(gid), "user_id": int(target_qq), "card": card_name},
                timeout=4.0,
            )
            if ok:
                side_actions_used += 1
                executed.append({"action": "group_card", "target": target_qq, "card": card_name})
                _log(f"EXECUTED group_card target={target_qq} card={card_name!r} ({actor_role}@{chat_key})")
            else:
                failed.append({"action": "group_card", "target": target_qq, "reason": err})

        elif action in ("essence", "unessence"):
            res = _perm("group_essence")
            if not res["allowed"]:
                blocked.append({"action": "group_essence", "reason": res["reason"]})
                _log(f"BLOCKED group_essence ({actor_role}@{chat_key}): {res['reason']}")
                continue
            mid = _clean_msg_id(args.get("msg_id") or args.get("id") or "")
            ok_msg, _, err = _verify_msg_in_chat(mid, is_group, gid, uid, bot_qq, call_api)
            if not ok_msg:
                failed.append({"action": "group_essence", "msg_id": mid, "reason": err})
                continue
            ep = "set_essence_msg" if action == "essence" else "delete_essence_msg"
            ok, _, err = call_api(ep, {"message_id": int(mid)}, timeout=4.0)
            if ok:
                side_actions_used += 1
                executed.append({"action": action, "msg_id": mid})
                _log(f"EXECUTED {ep} msg_id={mid} ({actor_role}@{chat_key})")
            else:
                failed.append({"action": action, "msg_id": mid, "reason": err})

        elif action in ("approve_join", "reject_join"):
            res = _perm("group_join_approve")
            if not res["allowed"]:
                blocked.append({"action": action, "reason": res["reason"]})
                _log(f"BLOCKED {action} ({actor_role}@{chat_key}): {res['reason']}")
                continue
            if is_dm and actor_role != "admin":
                reason = "私聊仅允许管理员审批入群申请"
                blocked.append({"action": action, "reason": reason})
                _log(f"BLOCKED {action} ({actor_role}@{chat_key}): {reason}")
                continue
            if group_requests is None:
                failed.append({"action": action, "reason": "入群申请模块未加载"})
                _log(f"FAILED {action}: group_requests module unavailable")
                continue

            req_ref = str(args.get("id") or args.get("request_id") or args.get("flag") or "").strip()
            if req_ref.startswith("#") and req_ref[1:].isdigit():
                req_ref = req_ref[1:]
            target_qq = _clean_qq(args.get("qq") or args.get("user_id") or "")
            target_gid = str(args.get("group_id") or args.get("group") or "").strip()
            req = None

            if req_ref:
                try:
                    req = group_requests.get_request(req_ref)
                except Exception as exc:
                    failed.append({"action": action, "target": req_ref, "reason": f"查询入群申请失败: {exc}"})
                    _log(f"FAILED {action} ref={req_ref}: {exc}")
                    continue
                if not isinstance(req, dict):
                    failed.append({"action": action, "target": req_ref, "reason": f"未找到对应的入群申请: {req_ref}"})
                    _log(f"FAILED {action} ref={req_ref}: not found")
                    continue
            elif target_qq:
                if not _QQ_NUM_RE.match(target_qq):
                    failed.append({"action": action, "reason": f"无效的申请人 QQ: {target_qq}"})
                    continue
                search_gid = gid if is_group else target_gid
                try:
                    listing = group_requests.list_requests(
                        status="pending",
                        group_id=search_gid,
                        limit=50,
                    )
                except Exception as exc:
                    failed.append({"action": action, "target": target_qq, "reason": f"查询待审入群申请失败: {exc}"})
                    _log(f"FAILED {action} qq={target_qq}: {exc}")
                    continue
                items = (listing.get("items") or []) if isinstance(listing, dict) else []
                matches = [
                    it
                    for it in items
                    if isinstance(it, dict) and str(it.get("user_id") or "").strip() == target_qq
                ]
                if not matches:
                    failed.append({"action": action, "target": target_qq, "reason": f"未找到 QQ {target_qq} 的待审入群申请"})
                    _log(f"FAILED {action} qq={target_qq}: no pending request")
                    continue

                def _req_recency_key(item):
                    seq_v = item.get("seq")
                    seq_i = int(seq_v) if isinstance(seq_v, int) else (int(seq_v) if str(seq_v or "").isdigit() else 0)
                    ts_v = item.get("created_at") or item.get("updated_at") or item.get("ts") or item.get("time") or ""
                    ts_s = f"{ts_v:020.3f}" if isinstance(ts_v, (int, float)) else str(ts_v)
                    rid_v = item.get("id")
                    rid_i = int(rid_v) if isinstance(rid_v, int) else (int(rid_v) if str(rid_v or "").isdigit() else 0)
                    return (ts_s, seq_i, rid_i)

                req = max(matches, key=_req_recency_key)
            else:
                failed.append({"action": action, "reason": "缺少申请 id 或申请人 qq 参数"})
                continue

            req_gid = str(req.get("group_id") or "").strip()
            if is_group and (not gid or req_gid != gid):
                reason = "目标入群申请不属于当前群聊，已拒绝跨群审批"
                blocked.append({"action": action, "request_id": str(req.get("id") or req_ref), "reason": reason})
                _log(f"BLOCKED {action} id={req.get('id') or req_ref}: cross-group ({req_gid} != {gid})")
                continue

            req_status = str(req.get("status") or "").strip().lower()
            if req_status in ("approved", "rejected"):
                failed.append(
                    {
                        "action": action,
                        "request_id": str(req.get("id") or req_ref),
                        "reason": f"该入群申请当前状态为 {req_status}，已处理完毕",
                    }
                )
                _log(f"FAILED {action} id={req.get('id') or req_ref}: status={req_status}")
                continue

            req_id = req.get("id") if req.get("id") is not None else (req.get("flag") or req_ref)
            reason_arg = str(args.get("reason") or "").strip()[:120]
            op_id = f"{actor_role}:{uid or gid}"
            try:
                dec = group_requests.execute_decision(
                    req_id,
                    approve=(action == "approve_join"),
                    reason=reason_arg,
                    decision_source="bot_chat",
                    operator_id=op_id,
                    napcat_call=call_api,
                )
            except Exception as exc:
                dec = {"ok": False, "error": str(exc)}

            if isinstance(dec, dict) and dec.get("ok"):
                side_actions_used += 1
                executed.append(
                    {
                        "action": action,
                        "request_id": str(req_id),
                        "group_id": req_gid,
                        "user_id": str(req.get("user_id") or ""),
                    }
                )
                _log(
                    f"EXECUTED {action} id={req_id} g:{req_gid} u:{req.get('user_id', '')} "
                    f"({actor_role}@{chat_key})"
                )
            else:
                err_msg = (dec.get("error") if isinstance(dec, dict) else "") or "执行入群审批失败"
                failed.append({"action": action, "request_id": str(req_id), "reason": str(err_msg)})
                _log(f"FAILED {action} id={req_id}: {err_msg}")

        else:
            blocked.append({"action": action, "reason": f"不支持的 QQ 动作: {action}"})

    # Post-process visible prose ONLY (preserving placeholders intact)
    if prose and text_postprocessor:
        prose = text_postprocessor(prose)

    # Restore inline CQ placeholders
    for idx, tok in enumerate(cq_placeholders):
        prose = prose.replace(f"\u0000QQCQ{idx}\u0000", tok)

    # Hoist reply CQ tag to the very beginning if present
    if reply_cq:
        if prose:
            final_text = f"{reply_cq}{prose}"
        else:
            # A reply quote without body text is invalid in QQ; drop bare reply
            final_text = ""
    else:
        final_text = prose.strip()

    return {
        "final_text": final_text,
        "executed": executed,
        "blocked": blocked,
        "failed": failed,
        "any_action_executed": bool(executed),
    }


# ---------------------------------------------------------------------------
# Hard Gateway Enforcement Filter (runs in onebot-ws-guard.py)
# ---------------------------------------------------------------------------

def _parse_cq_params(param_str):
    return onebot_common.parse_cq_params(param_str)


def _is_qq_cdn_url(url):
    try:
        host = (urllib.parse.urlparse(str(url or "").strip()).hostname or "").strip().lower().rstrip(".")
    except Exception:
        return False
    return bool(host and any(host == suf.lstrip(".") or host.endswith(suf) for suf in _QQ_CDN_HOST_SUFFIXES))


def _parse_local_image_ref(raw_val):
    """Detect file:// URIs or local filesystem paths. Returns (is_local, fs_path)."""
    s = str(raw_val or "").strip()
    if not s:
        return False, ""
    low = s.lower()
    if low.startswith("file:"):
        try:
            parsed = urllib.parse.urlparse(s)
        except Exception:
            return True, ""
        # Reject remote/UNC file:// hosts (only empty or localhost allowed)
        if parsed.netloc and parsed.netloc.lower() not in ("", "localhost"):
            return True, ""
        fs_path = urllib.parse.unquote(parsed.path or "")
        if re.match(r"^/[A-Za-z]:[\\/]", fs_path):
            fs_path = fs_path[1:]
        return True, fs_path
    if low.startswith(("http://", "https://", "base64://")):
        return False, ""
    if s.startswith(("/", "\\", "~/", "~\\")) or s == "~" or _WIN_DRIVE_RE.match(s):
        return True, (os.path.expanduser(s) if s.startswith("~") else s)
    if "://" not in s and ("/" in s or "\\" in s or ".." in s):
        return True, s
    return False, ""


def _is_confined_local_image_path(fs_path):
    """Strictly verify a local filesystem path stays inside AGY_WORK_DIR with an image extension."""
    if not fs_path or "\x00" in fs_path:
        return False
    if os.name != "nt" and (_WIN_DRIVE_RE.match(fs_path) or fs_path.startswith("\\")):
        return False
    work_dir = os.path.realpath(
        os.environ.get("AGY_WORK_DIR", "").strip()
        or os.path.join(_SCRIPT_DIR, "workspace")
    )
    cand = fs_path if os.path.isabs(fs_path) else os.path.join(work_dir, fs_path)
    try:
        real_p = os.path.realpath(cand)
        if os.path.commonpath([os.path.normcase(work_dir), os.path.normcase(real_p)]) != os.path.normcase(work_dir):
            return False
    except (OSError, ValueError):
        return False
    if os.path.normcase(real_p) == os.path.normcase(work_dir):
        return False
    ext = os.path.splitext(real_p)[1].lower()
    if ext not in ALLOWED_IMAGE_EXTS:
        return False
    return True


def filter_outbound_onebot_send(
    obj,
    actor_role="member",
    group_id="",
    is_dm=False,
    is_repeat_follow=False,
    policy=None,
):
    """Enforce bot_permissions on EVERY outbound OneBot send_*_msg request.

    - Converts authorized ``[CQ:reply,...]`` and ``[CQ:at,...]`` in string messages
      into canonical OneBot v11 segment arrays (so NapCat executes ONLY validated
      segments and treats all other text literally without CQ injection).
    - Blocks unauthorized ``at_all``, ``at_member``, ``reply_quote``, and ``image``
      segments/CQ codes.
    - Never permits raw ``file://`` local file CQ injection or out-of-workspace local image segments.

    Returns ``(new_obj, drop_entirely, audit_notes)``.
    """
    pol = policy if isinstance(policy, dict) else bot_permissions.load_policy()
    limits = bot_permissions.get_limits(pol)
    max_at = int(limits.get("max_at_per_msg", 3))
    gid = "" if is_dm else str(group_id or "").strip()
    notes = []

    def _ok(cap):
        return bot_permissions.is_allowed(
            cap, actor_role=actor_role, group_id=gid or None, is_dm=is_dm, policy=pol
        )

    params = obj.get("params") or {}
    msg = params.get("message")

    # Case 1: String message (e.g. normal reply from cc-connect)
    if isinstance(msg, str):
        if "[CQ:" not in msg:
            return obj, False, notes

        segments = []
        reply_seg = None
        at_used = 0
        pos = 0
        for m in CQ_CODE_RE.finditer(msg):
            if m.start() > pos:
                chunk = msg[pos : m.start()]
                if chunk:
                    segments.append({"type": "text", "data": {"text": chunk}})
            pos = m.end()

            ctype = m.group(1).strip().lower()
            cdata = _parse_cq_params(m.group(2) or "")

            if ctype == "at":
                qq = str(cdata.get("qq") or "").strip().lower()
                if qq == "all":
                    if _ok("at_all"):
                        segments.append({"type": "at", "data": {"qq": "all"}})
                    else:
                        notes.append("blocked:at_all")
                elif _QQ_NUM_RE.match(qq):
                    if _ok("at_member") and at_used < max_at:
                        at_used += 1
                        segments.append({"type": "at", "data": {"qq": qq}})
                    else:
                        notes.append(f"blocked:at_member:{qq}")
                else:
                    notes.append("blocked:at_invalid")

            elif ctype == "reply":
                rid = str(cdata.get("id") or "").strip()
                if _MSG_ID_RE.match(rid) and _ok("reply_quote"):
                    if reply_seg is None:
                        reply_seg = {"type": "reply", "data": {"id": rid}}
                else:
                    notes.append("blocked:reply_quote")

            elif ctype == "image":
                fval = str(cdata.get("file") or cdata.get("url") or "").strip()
                # Never allow local file:// injection via raw CQ strings
                is_loc, _ = _parse_local_image_ref(fval)
                if is_loc:
                    notes.append("blocked:cq_local_file_image")
                elif fval.lower().startswith(("http://", "https://")):
                    url_allowed = _ok("send_image_url") or (
                        _is_qq_cdn_url(fval) and _ok("send_sticker_reply")
                    )
                    if url_allowed and is_safe_public_url(fval, resolve_dns=False):
                        segments.append({"type": "image", "data": {"file": fval}})
                    else:
                        notes.append("blocked:send_image_url")
                elif fval.lower().startswith("base64://"):
                    if _ok("send_image_local") or _ok("send_image_url") or _ok("send_sticker_reply"):
                        segments.append({"type": "image", "data": {"file": fval}})
                    else:
                        notes.append("blocked:send_image_b64")
                else:
                    notes.append("blocked:cq_image_unknown")
            else:
                # Any other raw CQ code is stripped for security
                notes.append(f"blocked:cq_{ctype}")

        if pos < len(msg):
            tail = msg[pos:]
            if tail:
                segments.append({"type": "text", "data": {"text": tail}})

        if reply_seg is not None:
            segments.insert(0, reply_seg)

        # Ensure non-empty meaningful content remains
        has_content = any(
            (s.get("type") != "text" and s.get("type") != "reply")
            or (s.get("type") == "text" and str((s.get("data") or {}).get("text", "")).strip())
            for s in segments
        )
        if not has_content:
            return obj, True, notes

        new_obj = dict(obj)
        new_params = {k: v for k, v in params.items() if k != "auto_escape"}
        new_params["message"] = segments
        new_obj["params"] = new_params
        return new_obj, False, notes

    # Case 2: Segment array (e.g. cc-connect SendImage or FOLLOW_REPEAT)
    if isinstance(msg, list):
        filtered = []
        at_used = 0
        changed = False
        for seg in msg:
            if not isinstance(seg, dict):
                continue
            st = str(seg.get("type") or "").strip().lower()
            dt = seg.get("data") or {}
            if st == "at":
                qq = str(dt.get("qq") or "").strip().lower()
                if qq == "all":
                    if _ok("at_all"):
                        filtered.append(seg)
                    else:
                        changed = True
                        notes.append("blocked:at_all")
                else:
                    if _ok("at_member") and at_used < max_at:
                        at_used += 1
                        filtered.append(seg)
                    else:
                        changed = True
                        notes.append(f"blocked:at_member:{qq}")
            elif st == "reply":
                if _ok("reply_quote"):
                    filtered.append(seg)
                else:
                    changed = True
                    notes.append("blocked:reply_quote")
            elif st == "image":
                fval = str(dt.get("file") or "").strip()
                uval = str(dt.get("url") or "").strip()
                vals = [v for v in (fval, uval) if v]
                target_val = fval or uval
                is_local_any = any(_parse_local_image_ref(v)[0] for v in vals)

                if is_local_any:
                    if is_repeat_follow or not _ok("send_image_local"):
                        changed = True
                        notes.append("blocked:send_image_local")
                    else:
                        all_ok = True
                        for v in vals:
                            is_loc, fs_p = _parse_local_image_ref(v)
                            if is_loc:
                                if not _is_confined_local_image_path(fs_p):
                                    all_ok = False
                                    break
                            elif v.lower().startswith(("http://", "https://")):
                                if not is_safe_public_url(v, resolve_dns=False):
                                    all_ok = False
                                    break
                        if all_ok:
                            filtered.append(seg)
                        else:
                            changed = True
                            notes.append("blocked:local_file_image")
                elif is_repeat_follow:
                    url_ok = all(
                        is_safe_public_url(v, resolve_dns=False)
                        for v in vals
                        if v.lower().startswith(("http://", "https://"))
                    )
                    if _ok("send_sticker_reply") and url_ok:
                        filtered.append(seg)
                    else:
                        changed = True
                        notes.append("blocked:send_sticker_reply")
                elif any(v.lower().startswith(("http://", "https://")) for v in vals):
                    http_vals = [v for v in vals if v.lower().startswith(("http://", "https://"))]
                    all_public = all(is_safe_public_url(v, resolve_dns=False) for v in http_vals)
                    all_allowed = all(
                        _ok("send_image_url") or (_is_qq_cdn_url(v) and _ok("send_sticker_reply"))
                        for v in http_vals
                    )
                    if all_public and all_allowed:
                        filtered.append(seg)
                    else:
                        changed = True
                        notes.append("blocked:send_image_url")
                elif target_val.lower().startswith("base64://"):
                    if _ok("send_image_local") or _ok("send_image_url") or _ok("send_sticker_reply"):
                        filtered.append(seg)
                    else:
                        changed = True
                        notes.append("blocked:send_image_b64")
                else:
                    if (
                        target_val
                        and "://" not in target_val
                        and (_ok("send_image_local") or _ok("send_image_url") or _ok("send_sticker_reply"))
                    ):
                        filtered.append(seg)
                    else:
                        changed = True
                        notes.append("blocked:send_image")
            else:
                filtered.append(seg)

        if not changed:
            return obj, False, notes
        if not filtered:
            return obj, True, notes
        new_obj = dict(obj)
        new_params = dict(params)
        new_params["message"] = filtered
        new_obj["params"] = new_params
        return new_obj, False, notes

    return obj, False, notes
