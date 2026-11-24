"""Shared QQ bot capability & permission policy engine (Python 3.9 stdlib only).

Single source of truth for granular QQ action & capability permissions shared by:
- ``agy-acp-wrapper.py`` (prompt capability hints, action execution, local/web tool gating)
- ``onebot-ws-guard.py`` (hard gateway enforcement on outbound @/reply/image/forward/repeat)
- ``qq_actions.py`` (runtime permission evaluation before calling NapCat APIs)
- ``webui/`` (visual permission management console with instant hot-reload)

Storage: ``data/bot_permissions.json`` (cross-process locked via ``file_lock.py`` and
atomically replaced; consumers hot-reload automatically when the file's mtime changes).
"""

import copy
import datetime
import json
import os
import re
import sys
import tempfile
import threading
import time

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

import bot_env  # noqa: E402,F401
from file_lock import atomic_replace, file_lock  # noqa: E402

PERMISSIONS_FILE = (
    os.environ.get("BOT_PERMISSIONS_FILE", "").strip()
    or os.path.join(_SCRIPT_DIR, "data", "bot_permissions.json")
)

VALID_ROLES = ("admin", "member", "autonomous")

ROLE_META = [
    {
        "id": "admin",
        "label": "管理员",
        "desc": "管理员私聊，或在群聊中由管理员明确 @ / 回复 / 点名触发",
    },
    {
        "id": "member",
        "label": "普通群友",
        "desc": "非管理员私聊，或在群聊中由普通群友明确 @ / 回复 / 点名触发",
    },
    {
        "id": "autonomous",
        "label": "机器人自主",
        "desc": "群聊无人点名时的被动监听主动冒泡、续聊或跟风复读",
    },
]

CAPABILITY_CATEGORIES = [
    {
        "id": "recall",
        "title": "消息撤回",
        "desc": "控制机器人撤回自己刚发的消息，或作为群管撤回他人消息的权限",
        "icon": "rotate",
    },
    {
        "id": "mention",
        "title": "@提及他人",
        "desc": "控制机器人发送真实 QQ @ 提及（单人 @ 与 @全体成员独立管控）",
        "icon": "users",
    },
    {
        "id": "media",
        "title": "图片与表情包",
        "desc": "控制机器人发送网络图、工作区本地图以及复用群内表情包/跟图的能力",
        "icon": "sparkles",
    },
    {
        "id": "interaction",
        "title": "QQ 互动动作",
        "desc": "引用回复指定消息、戳一戳（拍一拍）与消息贴表情回应",
        "icon": "chat",
    },
    {
        "id": "group_admin",
        "title": "群管理高危操作",
        "desc": "禁言、踢人、改群名片、设群精华等高风险操作（默认全部关闭，需机器人为群管）",
        "icon": "shield",
    },
    {
        "id": "builtin",
        "title": "已有能力统一收口",
        "desc": "统一管控跟风复读、长回复合并转发、只读联网搜索与管理员私聊本地工具",
        "icon": "sliders",
    },
]

# Full declarative catalog of all 20 managed capabilities across 6 categories.
CAPABILITY_CATALOG = [
    # --- 1. Recall ---
    {
        "id": "recall_self",
        "category": "recall",
        "label": "撤回机器人自己的消息",
        "desc": "允许机器人撤回自己在当前聊天中发出的消息（例如发错字、说错话或管理员让撤回）。",
        "risk": "low",
        "group_only": False,
        "requires_group_admin": False,
        "role_locks": {},
        "default": {
            "enabled": True,
            "roles": {"admin": True, "member": False, "autonomous": True},
        },
    },
    {
        "id": "recall_others",
        "category": "recall",
        "label": "撤回他人消息（群管）",
        "desc": "允许机器人在群聊中撤回其他群成员发出的消息（需机器人拥有群管理员/群主身份）。",
        "risk": "high",
        "group_only": True,
        "requires_group_admin": True,
        "role_locks": {},
        "default": {
            "enabled": False,
            "roles": {"admin": True, "member": False, "autonomous": False},
        },
    },
    # --- 2. Mention ---
    {
        "id": "at_member",
        "category": "mention",
        "label": "@指定群友",
        "desc": "允许机器人在群聊回复中发送真实 QQ @ 提及指定成员（受单条上限保护，防刷屏）。",
        "risk": "low",
        "group_only": True,
        "requires_group_admin": False,
        "role_locks": {},
        "default": {
            "enabled": True,
            "roles": {"admin": True, "member": True, "autonomous": True},
        },
    },
    {
        "id": "at_all",
        "category": "mention",
        "label": "@全体成员",
        "desc": "允许机器人发送 @全体成员（极高扰民风险，需机器人为群管/群主；未放行时网关层强力拦截）。",
        "risk": "critical",
        "group_only": True,
        "requires_group_admin": True,
        "role_locks": {},
        "default": {
            "enabled": False,
            "roles": {"admin": True, "member": False, "autonomous": False},
        },
    },
    # --- 3. Media ---
    {
        "id": "send_image_url",
        "category": "media",
        "label": "发送网络图片",
        "desc": "允许机器人下载并发送公网 HTTP/HTTPS 图片（内置 SSRF 防护与图片格式校验）。",
        "risk": "medium",
        "group_only": False,
        "requires_group_admin": False,
        "role_locks": {},
        "default": {
            "enabled": True,
            "roles": {"admin": True, "member": True, "autonomous": False},
        },
    },
    {
        "id": "send_image_local",
        "category": "media",
        "label": "发送工作区本地图片",
        "desc": "允许机器人发送白名单本地目录（默认 workspace/）内的图片文件，严禁越界读取其他路径。",
        "risk": "high",
        "group_only": False,
        "requires_group_admin": False,
        "role_locks": {},
        "default": {
            "enabled": True,
            "roles": {"admin": True, "member": False, "autonomous": False},
        },
    },
    {
        "id": "send_sticker_reply",
        "category": "media",
        "label": "发表情包 / 群内跟图",
        "desc": "允许机器人复用当前聊天近期出现过的表情包或图片回复，以及允许图片复读。",
        "risk": "low",
        "group_only": False,
        "requires_group_admin": False,
        "role_locks": {},
        "default": {
            "enabled": True,
            "roles": {"admin": True, "member": True, "autonomous": True},
        },
    },
    # --- 4. Interaction ---
    {
        "id": "reply_quote",
        "category": "interaction",
        "label": "引用回复指定消息",
        "desc": "允许机器人在回复时附带 QQ 原生引用回复气泡，精准指向某一条历史消息。",
        "risk": "low",
        "group_only": False,
        "requires_group_admin": False,
        "role_locks": {},
        "default": {
            "enabled": True,
            "roles": {"admin": True, "member": True, "autonomous": True},
        },
    },
    {
        "id": "poke",
        "category": "interaction",
        "label": "戳一戳（拍一拍）",
        "desc": "允许机器人调用群聊/私聊戳一戳动作（受内置冷却时间限制，防止连戳刷屏）。",
        "risk": "low",
        "group_only": False,
        "requires_group_admin": False,
        "role_locks": {},
        "default": {
            "enabled": True,
            "roles": {"admin": True, "member": True, "autonomous": True},
        },
    },
    {
        "id": "emoji_react",
        "category": "interaction",
        "label": "消息贴表情回应",
        "desc": "允许机器人给当前聊天中的指定消息贴上 QQ 表情回应（点赞、爱心、大笑等）。",
        "risk": "low",
        "group_only": False,
        "requires_group_admin": False,
        "role_locks": {},
        "default": {
            "enabled": True,
            "roles": {"admin": True, "member": True, "autonomous": True},
        },
    },
    # --- 5. Group Admin ---
    {
        "id": "group_ban",
        "category": "group_admin",
        "label": "群成员禁言 / 解禁",
        "desc": "允许机器人禁言或解除禁言群成员（永不可禁言管理员和机器人自身，时长受上限保护）。",
        "risk": "critical",
        "group_only": True,
        "requires_group_admin": True,
        "role_locks": {},
        "default": {
            "enabled": False,
            "roles": {"admin": True, "member": False, "autonomous": False},
        },
    },
    {
        "id": "group_kick",
        "category": "group_admin",
        "label": "踢出群成员",
        "desc": "允许机器人将指定成员移出群聊（极高危操作；永不可踢管理员和机器人自身）。",
        "risk": "critical",
        "group_only": True,
        "requires_group_admin": True,
        "role_locks": {},
        "default": {
            "enabled": False,
            "roles": {"admin": True, "member": False, "autonomous": False},
        },
    },
    {
        "id": "group_card",
        "category": "group_admin",
        "label": "修改群名片",
        "desc": "允许机器人修改指定群成员的群昵称/群名片（需机器人为群管，或仅改自己名片）。",
        "risk": "high",
        "group_only": True,
        "requires_group_admin": True,
        "role_locks": {},
        "default": {
            "enabled": False,
            "roles": {"admin": True, "member": False, "autonomous": False},
        },
    },
    {
        "id": "group_essence",
        "category": "group_admin",
        "label": "设为 / 移出群精华消息",
        "desc": "允许机器人将群消息设为精华或移出群精华（需机器人为群管/群主）。",
        "risk": "high",
        "group_only": True,
        "requires_group_admin": True,
        "role_locks": {},
        "default": {
            "enabled": False,
            "roles": {"admin": True, "member": False, "autonomous": False},
        },
    },
    {
        "id": "group_join_approve",
        "category": "group_admin",
        "label": "审批群成员入群申请",
        "desc": "允许机器人在对话中同意或拒绝群成员的加群申请（需机器人为群管/群主；支持在群聊或管理员私聊中审批）。",
        "risk": "high",
        "group_only": False,
        "requires_group_admin": True,
        "role_locks": {},
        "default": {
            "enabled": False,
            "roles": {"admin": True, "member": False, "autonomous": False},
        },
    },
    # --- 6. Builtin capabilities convergence ---
    {
        "id": "repeat_follow",
        "category": "builtin",
        "label": "群聊跟风复读",
        "desc": "收口管控复读能力（与 bot.env 的 REPEAT_ENABLED 协同，支持按单个群独立开关复读）。",
        "risk": "low",
        "group_only": True,
        "requires_group_admin": False,
        "role_locks": {},
        "default": {
            "enabled": True,
            "roles": {"admin": True, "member": True, "autonomous": True},
        },
    },
    {
        "id": "forward_long_msg",
        "category": "builtin",
        "label": "长回复自动合并转发",
        "desc": "收口管控超长消息转聊天记录卡片（与 FORWARD_LONG_MSG_* 协同，支持按群独立开关）。",
        "risk": "low",
        "group_only": False,
        "requires_group_admin": False,
        "role_locks": {},
        "default": {
            "enabled": True,
            "roles": {"admin": True, "member": True, "autonomous": True},
        },
    },
    {
        "id": "web_search",
        "category": "builtin",
        "label": "联网搜索（只读）",
        "desc": "控制群聊与普通私聊是否放行只读联网搜索（search_web），支持按群独立关闭。",
        "risk": "low",
        "group_only": False,
        "requires_group_admin": False,
        "role_locks": {},
        "default": {
            "enabled": True,
            "roles": {"admin": True, "member": True, "autonomous": True},
        },
    },
    {
        "id": "chat_history_lookup",
        "category": "builtin",
        "label": "自主翻查聊天记录",
        "desc": "当上下文被压缩、会话重置或超出近期窗口时，允许机器人自主翻阅和搜索当前群聊/私聊的原始历史记录（严格隔离在当前会话，绝不跨群或串私聊）。",
        "risk": "low",
        "group_only": False,
        "requires_group_admin": False,
        "role_locks": {},
        "default": {
            "enabled": True,
            "roles": {"admin": True, "member": True, "autonomous": True},
        },
    },
    {
        "id": "admin_dm_local_tools",
        "category": "builtin",
        "label": "管理员私聊本地文件与命令",
        "desc": "总控管理员私聊中的本地读写文件与终端命令执行（仅限管理员私聊；普通群友与自主触发永久锁定禁用）。",
        "risk": "critical",
        "group_only": False,
        "dm_admin_only": True,
        "requires_group_admin": False,
        "role_locks": {"member": False, "autonomous": False},
        "default": {
            "enabled": True,
            "roles": {"admin": True, "member": False, "autonomous": False},
        },
    },
]

CAPABILITY_MAP = {c["id"]: c for c in CAPABILITY_CATALOG}

DEFAULT_LIMITS = {
    "max_actions_per_turn": 3,
    "max_at_per_msg": 3,
    "max_images_per_turn": 2,
    "max_ban_secs": 86400,
    "poke_cooldown_secs": 15,
    "allowed_local_image_dirs": ["workspace"],
}

LIMIT_SPECS = [
    {
        "key": "max_actions_per_turn",
        "label": "单轮最大 QQ 动作数",
        "type": "int",
        "min": 1,
        "max": 10,
        "unit": "次",
        "desc": "机器人单次回合中最多可执行的 QQ 动作（撤回/戳一戳/发图/贴表情/群管等）总次数。",
    },
    {
        "key": "max_at_per_msg",
        "label": "单条消息最大 @ 人数",
        "type": "int",
        "min": 1,
        "max": 10,
        "unit": "人",
        "desc": "单条群聊回复中最多允许 @ 多少位成员，超出部分自动转为纯文本防刷屏。",
    },
    {
        "key": "max_images_per_turn",
        "label": "单轮最多发送图片数",
        "type": "int",
        "min": 1,
        "max": 5,
        "unit": "张",
        "desc": "单次回合中机器人最多主动发送的图片/表情包张数。",
    },
    {
        "key": "max_ban_secs",
        "label": "单次禁言时长上限",
        "type": "int",
        "min": 60,
        "max": 2592000,
        "unit": "秒",
        "desc": "开启群禁言权限时，单次允许禁言的最长秒数（默认 86400 秒 = 1 天，最大 30 天）。",
    },
    {
        "key": "poke_cooldown_secs",
        "label": "同会话戳一戳冷却",
        "type": "int",
        "min": 0,
        "max": 600,
        "unit": "秒",
        "desc": "同一群聊/私聊中两次戳一戳之间的最小间隔秒数，防止连戳扰民。",
    },
    {
        "key": "allowed_local_image_dirs",
        "label": "本地发图允许目录（项目相对路径）",
        "type": "tags",
        "desc": "仅允许从这些项目子目录发送本地图片（严禁 .. 或绝对路径，默认 workspace）。",
    },
]

PERMISSION_TEMPLATES = [
    {
        "id": "safe_default",
        "name": "保守安全（默认）",
        "badge": "推荐",
        "desc": "开启基础互动（@人、引用、戳一戳、贴表情、撤回自己、复读、合并转发、联网搜索）；关闭全部群管高危操作与 @全体成员。",
        "global": {c["id"]: copy.deepcopy(c["default"]) for c in CAPABILITY_CATALOG},
    },
    {
        "id": "lively_interactive",
        "name": "活跃互动",
        "badge": "互动",
        "desc": "放开更多群友互动：允许普通群友让机器人发网络图、复用表情包、戳一戳、贴表情、引用回复和 @人，仍关闭全部群管高危操作。",
        "global": {
            "recall_self": {"enabled": True, "roles": {"admin": True, "member": True, "autonomous": True}},
            "recall_others": {"enabled": False, "roles": {"admin": True, "member": False, "autonomous": False}},
            "at_member": {"enabled": True, "roles": {"admin": True, "member": True, "autonomous": True}},
            "at_all": {"enabled": False, "roles": {"admin": True, "member": False, "autonomous": False}},
            "send_image_url": {"enabled": True, "roles": {"admin": True, "member": True, "autonomous": True}},
            "send_image_local": {"enabled": True, "roles": {"admin": True, "member": False, "autonomous": False}},
            "send_sticker_reply": {"enabled": True, "roles": {"admin": True, "member": True, "autonomous": True}},
            "reply_quote": {"enabled": True, "roles": {"admin": True, "member": True, "autonomous": True}},
            "poke": {"enabled": True, "roles": {"admin": True, "member": True, "autonomous": True}},
            "emoji_react": {"enabled": True, "roles": {"admin": True, "member": True, "autonomous": True}},
            "group_ban": {"enabled": False, "roles": {"admin": True, "member": False, "autonomous": False}},
            "group_kick": {"enabled": False, "roles": {"admin": True, "member": False, "autonomous": False}},
            "group_card": {"enabled": False, "roles": {"admin": True, "member": False, "autonomous": False}},
            "group_essence": {"enabled": False, "roles": {"admin": True, "member": False, "autonomous": False}},
            "group_join_approve": {"enabled": False, "roles": {"admin": True, "member": False, "autonomous": False}},
            "repeat_follow": {"enabled": True, "roles": {"admin": True, "member": True, "autonomous": True}},
            "forward_long_msg": {"enabled": True, "roles": {"admin": True, "member": True, "autonomous": True}},
            "web_search": {"enabled": True, "roles": {"admin": True, "member": True, "autonomous": True}},
            "chat_history_lookup": {"enabled": True, "roles": {"admin": True, "member": True, "autonomous": True}},
            "admin_dm_local_tools": {"enabled": True, "roles": {"admin": True, "member": False, "autonomous": False}},
        },
    },
    {
        "id": "group_moderator",
        "name": "管理员群管助手",
        "badge": "群管",
        "desc": "为管理员开放撤回他人消息、禁言、改群名片、设群精华、审批入群申请与 @全体成员（均严格限定仅管理员可触发，绝不对普通群友或自主触发开放）。",
        "global": {
            "recall_self": {"enabled": True, "roles": {"admin": True, "member": False, "autonomous": True}},
            "recall_others": {"enabled": True, "roles": {"admin": True, "member": False, "autonomous": False}},
            "at_member": {"enabled": True, "roles": {"admin": True, "member": True, "autonomous": True}},
            "at_all": {"enabled": True, "roles": {"admin": True, "member": False, "autonomous": False}},
            "send_image_url": {"enabled": True, "roles": {"admin": True, "member": True, "autonomous": False}},
            "send_image_local": {"enabled": True, "roles": {"admin": True, "member": False, "autonomous": False}},
            "send_sticker_reply": {"enabled": True, "roles": {"admin": True, "member": True, "autonomous": True}},
            "reply_quote": {"enabled": True, "roles": {"admin": True, "member": True, "autonomous": True}},
            "poke": {"enabled": True, "roles": {"admin": True, "member": True, "autonomous": True}},
            "emoji_react": {"enabled": True, "roles": {"admin": True, "member": True, "autonomous": True}},
            "group_ban": {"enabled": True, "roles": {"admin": True, "member": False, "autonomous": False}},
            "group_kick": {"enabled": False, "roles": {"admin": True, "member": False, "autonomous": False}},
            "group_card": {"enabled": True, "roles": {"admin": True, "member": False, "autonomous": False}},
            "group_essence": {"enabled": True, "roles": {"admin": True, "member": False, "autonomous": False}},
            "group_join_approve": {"enabled": True, "roles": {"admin": True, "member": False, "autonomous": False}},
            "repeat_follow": {"enabled": True, "roles": {"admin": True, "member": True, "autonomous": True}},
            "forward_long_msg": {"enabled": True, "roles": {"admin": True, "member": True, "autonomous": True}},
            "web_search": {"enabled": True, "roles": {"admin": True, "member": True, "autonomous": True}},
            "chat_history_lookup": {"enabled": True, "roles": {"admin": True, "member": True, "autonomous": True}},
            "admin_dm_local_tools": {"enabled": True, "roles": {"admin": True, "member": False, "autonomous": False}},
        },
    },
    {
        "id": "minimal_text",
        "name": "极简纯文字",
        "badge": "极简",
        "desc": "仅保留纯文本聊天、联网搜索、合并转发与管理员私聊工具；关闭所有撤回、@人、发图、戳一戳、贴表情、复读与群管操作。",
        "global": {
            "recall_self": {"enabled": False, "roles": {"admin": True, "member": False, "autonomous": False}},
            "recall_others": {"enabled": False, "roles": {"admin": True, "member": False, "autonomous": False}},
            "at_member": {"enabled": False, "roles": {"admin": True, "member": False, "autonomous": False}},
            "at_all": {"enabled": False, "roles": {"admin": True, "member": False, "autonomous": False}},
            "send_image_url": {"enabled": False, "roles": {"admin": True, "member": False, "autonomous": False}},
            "send_image_local": {"enabled": False, "roles": {"admin": True, "member": False, "autonomous": False}},
            "send_sticker_reply": {"enabled": False, "roles": {"admin": True, "member": False, "autonomous": False}},
            "reply_quote": {"enabled": False, "roles": {"admin": True, "member": False, "autonomous": False}},
            "poke": {"enabled": False, "roles": {"admin": True, "member": False, "autonomous": False}},
            "emoji_react": {"enabled": False, "roles": {"admin": True, "member": False, "autonomous": False}},
            "group_ban": {"enabled": False, "roles": {"admin": True, "member": False, "autonomous": False}},
            "group_kick": {"enabled": False, "roles": {"admin": True, "member": False, "autonomous": False}},
            "group_card": {"enabled": False, "roles": {"admin": True, "member": False, "autonomous": False}},
            "group_essence": {"enabled": False, "roles": {"admin": True, "member": False, "autonomous": False}},
            "group_join_approve": {"enabled": False, "roles": {"admin": True, "member": False, "autonomous": False}},
            "repeat_follow": {"enabled": False, "roles": {"admin": True, "member": False, "autonomous": False}},
            "forward_long_msg": {"enabled": True, "roles": {"admin": True, "member": True, "autonomous": True}},
            "web_search": {"enabled": True, "roles": {"admin": True, "member": True, "autonomous": True}},
            "chat_history_lookup": {"enabled": True, "roles": {"admin": True, "member": True, "autonomous": True}},
            "admin_dm_local_tools": {"enabled": True, "roles": {"admin": True, "member": False, "autonomous": False}},
        },
    },
]

_TEMPLATE_BY_ID = {t["id"]: t for t in PERMISSION_TEMPLATES}

_GROUP_ID_RE = re.compile(r"^\d{5,15}$")
_SAFE_REL_DIR_RE = re.compile(r"^[A-Za-z0-9_.\-\u4e00-\u9fff/]+$")
_FORBIDDEN_LOCAL_DIRS = {"data", "napcat", "backup", ".tools", ".git", "webui", "installer", "plugins", "memory_engine"}

_cache_lock = threading.Lock()
_cached_policy = None
_cached_mtime = None
_cached_path = None
_last_stat_check = -10.0


def _now_iso():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _to_bool(val, default=False):
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return bool(val)
    if isinstance(val, str):
        low = val.strip().lower()
        if low in ("1", "true", "yes", "on"):
            return True
        if low in ("0", "false", "no", "off", ""):
            return False
    return default


def _sanitize_local_dirs(raw):
    if isinstance(raw, str):
        items = [x.strip() for x in raw.split(",") if x.strip()]
    elif isinstance(raw, list):
        items = [str(x).strip() for x in raw if str(x).strip()]
    else:
        return list(DEFAULT_LIMITS["allowed_local_image_dirs"])

    cleaned = []
    seen = set()
    for item in items:
        norm = item.replace("\\", "/").strip("/")
        if not norm or ".." in norm.split("/") or norm.startswith("/") or ":" in norm:
            continue
        if not _SAFE_REL_DIR_RE.match(norm):
            continue
        top = norm.split("/", 1)[0].lower()
        if top in _FORBIDDEN_LOCAL_DIRS:
            continue
        if norm not in seen:
            seen.add(norm)
            cleaned.append(norm)
    return cleaned or list(DEFAULT_LIMITS["allowed_local_image_dirs"])


def _sanitize_limits(raw_limits):
    src = raw_limits if isinstance(raw_limits, dict) else {}
    out = copy.deepcopy(DEFAULT_LIMITS)
    for spec in LIMIT_SPECS:
        k = spec["key"]
        if k not in src:
            continue
        if spec["type"] == "int":
            try:
                v = int(float(src[k]))
            except (TypeError, ValueError):
                continue
            v = max(spec["min"], min(spec["max"], v))
            out[k] = v
        elif spec["type"] == "tags":
            out[k] = _sanitize_local_dirs(src[k])
    return out


def _apply_role_locks(cap_id, roles_dict):
    spec = CAPABILITY_MAP.get(cap_id)
    if not spec:
        return roles_dict
    locks = spec.get("role_locks") or {}
    for role, forced in locks.items():
        if role in VALID_ROLES:
            roles_dict[role] = bool(forced)
    return roles_dict


def _sanitize_cap_entry(cap_id, raw_entry, fallback):
    res = {
        "enabled": bool(fallback["enabled"]),
        "roles": {r: bool(fallback["roles"].get(r, False)) for r in VALID_ROLES},
    }
    if isinstance(raw_entry, dict):
        if "enabled" in raw_entry and raw_entry["enabled"] is not None:
            res["enabled"] = _to_bool(raw_entry["enabled"], res["enabled"])
        raw_roles = raw_entry.get("roles")
        if isinstance(raw_roles, dict):
            for r in VALID_ROLES:
                if r in raw_roles and raw_roles[r] is not None:
                    res["roles"][r] = _to_bool(raw_roles[r], res["roles"][r])
    _apply_role_locks(cap_id, res["roles"])
    return res


def _sanitize_group_override(cap_id, raw_override):
    """Sanitize a partial per-group override; returns {} if empty."""
    if not isinstance(raw_override, dict):
        return {}
    spec = CAPABILITY_MAP.get(cap_id)
    if not spec:
        return {}
    locks = spec.get("role_locks") or {}
    out = {}
    if "enabled" in raw_override and raw_override["enabled"] is not None:
        out["enabled"] = _to_bool(raw_override["enabled"], False)
    raw_roles = raw_override.get("roles")
    if isinstance(raw_roles, dict):
        roles_out = {}
        for r in VALID_ROLES:
            if r in raw_roles and raw_roles[r] is not None:
                val = _to_bool(raw_roles[r], False)
                if r in locks:
                    val = bool(locks[r])
                roles_out[r] = val
        if roles_out:
            out["roles"] = roles_out
    return out


def default_policy():
    return {
        "version": 1,
        "updated_at": "",
        "limits": copy.deepcopy(DEFAULT_LIMITS),
        "global": {
            c["id"]: copy.deepcopy(c["default"])
            for c in CAPABILITY_CATALOG
        },
        "groups": {},
    }


def normalize_policy(raw):
    """Normalize arbitrary JSON dict into a clean, schema-valid policy dict."""
    base = default_policy()
    if not isinstance(raw, dict):
        return base

    base["version"] = 1
    if isinstance(raw.get("updated_at"), str):
        base["updated_at"] = raw["updated_at"].strip()
    base["limits"] = _sanitize_limits(raw.get("limits"))

    raw_global = raw.get("global") if isinstance(raw.get("global"), dict) else {}
    for c in CAPABILITY_CATALOG:
        cid = c["id"]
        base["global"][cid] = _sanitize_cap_entry(cid, raw_global.get(cid), c["default"])

    raw_groups = raw.get("groups") if isinstance(raw.get("groups"), dict) else {}
    clean_groups = {}
    for raw_gid, gdata in raw_groups.items():
        gid = str(raw_gid or "").strip()
        if not _GROUP_ID_RE.match(gid) or not isinstance(gdata, dict):
            continue
        label = str(gdata.get("label") or "").strip()[:80]
        raw_ov = gdata.get("overrides") if isinstance(gdata.get("overrides"), dict) else {}
        clean_ov = {}
        for cid, ov_val in raw_ov.items():
            if cid not in CAPABILITY_MAP:
                continue
            if CAPABILITY_MAP[cid].get("dm_admin_only"):
                continue
            sanitized = _sanitize_group_override(cid, ov_val)
            if sanitized:
                clean_ov[cid] = sanitized
        # Keep group entry if it has overrides or an explicit label
        if clean_ov or label:
            clean_groups[gid] = {
                "label": label,
                "overrides": clean_ov,
            }
    base["groups"] = clean_groups
    return base


def _lock_path_for(path):
    return path + ".lock"


def load_policy(path=None, force=False):
    """Load policy from disk with fast mtime-based hot-reload caching."""
    global _cached_policy, _cached_mtime, _cached_path, _last_stat_check
    target = path or PERMISSIONS_FILE
    now = time.monotonic()

    with _cache_lock:
        if (
            not force
            and _cached_policy is not None
            and _cached_path == target
            and (now - _last_stat_check) < 0.8
        ):
            return _cached_policy

        _last_stat_check = now
        try:
            st = os.stat(target)
            mtime = (st.st_mtime_ns, st.st_size)
        except OSError:
            mtime = None

        if (
            not force
            and _cached_policy is not None
            and _cached_path == target
            and _cached_mtime == mtime
        ):
            return _cached_policy

    if mtime is None:
        pol = default_policy()
    else:
        raw = None
        try:
            with file_lock(_lock_path_for(target), exclusive=False):
                with open(target, "r", encoding="utf-8-sig") as f:
                    raw = json.load(f)
        except Exception:
            raw = None
        pol = normalize_policy(raw)

    with _cache_lock:
        _cached_policy = pol
        _cached_mtime = mtime
        _cached_path = target
    return pol


def save_policy(policy, path=None):
    """Validate, lock, and atomically write policy to disk."""
    global _cached_policy, _cached_mtime, _cached_path, _last_stat_check
    target = path or PERMISSIONS_FILE
    clean = normalize_policy(policy)
    clean["updated_at"] = _now_iso()

    parent = os.path.dirname(os.path.abspath(target))
    os.makedirs(parent, exist_ok=True)
    text = json.dumps(clean, ensure_ascii=False, indent=2) + "\n"

    with file_lock(_lock_path_for(target), exclusive=True):
        fd, tmp = tempfile.mkstemp(prefix=".bot_perm_", suffix=".tmp", dir=parent)
        fd_owned = True
        try:
            f = os.fdopen(fd, "w", encoding="utf-8", newline="\n")
            fd_owned = False
            with f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            atomic_replace(tmp, target)
        except Exception:
            if fd_owned:
                try:
                    os.close(fd)
                except OSError:
                    pass
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    try:
        st = os.stat(target)
        mtime = (st.st_mtime_ns, st.st_size)
    except OSError:
        mtime = None
    with _cache_lock:
        _cached_policy = clean
        _cached_mtime = mtime
        _cached_path = target
        _last_stat_check = time.monotonic()
    return copy.deepcopy(clean)


def apply_template(template_id, keep_groups=True, keep_limits=True, path=None):
    """Apply a built-in permission template to global rules and persist."""
    tpl = _TEMPLATE_BY_ID.get(str(template_id or "").strip())
    if not tpl:
        raise ValueError(f"未知的权限模板: {template_id}")
    cur = copy.deepcopy(load_policy(path=path, force=True))
    nxt = default_policy()
    nxt["global"] = copy.deepcopy(tpl["global"])
    if keep_groups:
        nxt["groups"] = cur.get("groups") or {}
    if keep_limits:
        nxt["limits"] = cur.get("limits") or copy.deepcopy(DEFAULT_LIMITS)
    return save_policy(nxt, path=path)


def resolve_capability(capability_id, group_id=None, policy=None):
    """Return effective rule for ``capability_id`` in ``group_id`` (or global/DM)."""
    spec = CAPABILITY_MAP.get(capability_id)
    if not spec:
        return {
            "capability": capability_id,
            "known": False,
            "enabled": False,
            "roles": {r: False for r in VALID_ROLES},
            "source": "unknown",
            "overridden": False,
        }

    pol = policy if isinstance(policy, dict) else load_policy()
    glob = (pol.get("global") or {}).get(capability_id) or spec["default"]
    eff_enabled = bool(glob.get("enabled", False))
    eff_roles = {
        r: bool((glob.get("roles") or {}).get(r, False))
        for r in VALID_ROLES
    }
    source = "global"
    overridden = False

    gid = str(group_id or "").strip()
    if gid and not spec.get("dm_admin_only"):
        g_entry = (pol.get("groups") or {}).get(gid)
        if isinstance(g_entry, dict):
            ov = (g_entry.get("overrides") or {}).get(capability_id)
            if isinstance(ov, dict) and ov:
                overridden = True
                source = f"group:{gid}"
                if "enabled" in ov and ov["enabled"] is not None:
                    eff_enabled = bool(ov["enabled"])
                if isinstance(ov.get("roles"), dict):
                    for r in VALID_ROLES:
                        if r in ov["roles"] and ov["roles"][r] is not None:
                            eff_roles[r] = bool(ov["roles"][r])

    _apply_role_locks(capability_id, eff_roles)
    return {
        "capability": capability_id,
        "known": True,
        "enabled": eff_enabled,
        "roles": eff_roles,
        "source": source,
        "overridden": overridden,
    }


def check_permission(capability_id, actor_role="member", group_id=None, is_dm=False, policy=None):
    """Evaluate whether ``actor_role`` may invoke ``capability_id`` in this chat.

    Returns dict:
        {
          "allowed": bool,
          "capability": str,
          "label": str,
          "actor_role": str,
          "group_id": str,
          "source": str,        # "global" | "group:<gid>" | "plugin_disabled" | "hard_rule" | "unknown"
          "overridden": bool,
          "reason": str,        # Human-readable Chinese reason
        }
    """
    role = str(actor_role or "member").strip().lower()
    if role not in VALID_ROLES:
        role = "member"
    gid = "" if is_dm else str(group_id or "").strip()
    spec = CAPABILITY_MAP.get(capability_id)
    if not spec:
        return {
            "allowed": False,
            "capability": str(capability_id),
            "label": str(capability_id),
            "actor_role": role,
            "group_id": gid,
            "source": "unknown",
            "overridden": False,
            "reason": f"未知能力「{capability_id}」，按默认拒绝拦截",
        }

    label = spec["label"]
    role_label = {"admin": "管理员", "member": "普通群友", "autonomous": "机器人自主"}.get(role, role)

    # Scope safety checks
    if is_dm and spec.get("group_only"):
        return {
            "allowed": False,
            "capability": capability_id,
            "label": label,
            "actor_role": role,
            "group_id": "",
            "source": "hard_rule",
            "overridden": False,
            "reason": f"「{label}」仅支持在 QQ 群聊中使用",
        }
    if spec.get("dm_admin_only") and (not is_dm or role != "admin"):
        return {
            "allowed": False,
            "capability": capability_id,
            "label": label,
            "actor_role": role,
            "group_id": gid,
            "source": "hard_rule",
            "overridden": False,
            "reason": f"「{label}」永久锁定仅限管理员私聊使用",
        }

    # Master plugin switch check (fail-open if bot_plugins is unavailable)
    try:
        import bot_plugins
        plug_res = bot_plugins.check_capability_plugin(capability_id)
    except Exception:
        plug_res = None
    if isinstance(plug_res, dict) and not plug_res.get("allowed", True):
        return {
            "allowed": False,
            "capability": capability_id,
            "label": label,
            "actor_role": role,
            "group_id": gid,
            "source": "plugin_disabled",
            "overridden": False,
            "reason": plug_res.get("reason") or f"所属插件已关闭，无法使用「{label}」",
            "plugin_id": plug_res.get("plugin_id", ""),
            "plugin_name": plug_res.get("plugin_name", ""),
        }

    eff = resolve_capability(capability_id, group_id=gid or None, policy=policy)
    scope_desc = f"群 {gid} 独立覆写" if eff["overridden"] else "全局策略"

    if not eff["enabled"]:
        return {
            "allowed": False,
            "capability": capability_id,
            "label": label,
            "actor_role": role,
            "group_id": gid,
            "source": eff["source"],
            "overridden": eff["overridden"],
            "reason": f"{scope_desc}已关闭「{label}」总开关",
        }

    if not eff["roles"].get(role, False):
        return {
            "allowed": False,
            "capability": capability_id,
            "label": label,
            "actor_role": role,
            "group_id": gid,
            "source": eff["source"],
            "overridden": eff["overridden"],
            "reason": f"{scope_desc}未向「{role_label}」开放「{label}」权限",
        }

    return {
        "allowed": True,
        "capability": capability_id,
        "label": label,
        "actor_role": role,
        "group_id": gid,
        "source": eff["source"],
        "overridden": eff["overridden"],
        "reason": f"已按{scope_desc}向「{role_label}」放行「{label}」",
    }


def is_allowed(capability_id, actor_role="member", group_id=None, is_dm=False, policy=None):
    return bool(
        check_permission(
            capability_id,
            actor_role=actor_role,
            group_id=group_id,
            is_dm=is_dm,
            policy=policy,
        )["allowed"]
    )


def get_limits(policy=None):
    pol = policy if isinstance(policy, dict) else load_policy()
    return copy.deepcopy(pol.get("limits") or DEFAULT_LIMITS)


def get_effective_matrix(group_id=None, policy=None):
    """Return full capability resolution map for UI display."""
    pol = policy if isinstance(policy, dict) else load_policy()
    out = {}
    for c in CAPABILITY_CATALOG:
        cid = c["id"]
        out[cid] = resolve_capability(cid, group_id=group_id, policy=pol)
    return out


def infer_actor_role(is_admin, is_group, directly_addressed=True, soft_calling=False):
    """Canonical mapping from turn metadata to permission role."""
    if not is_group:
        return "admin" if is_admin else "member"
    if directly_addressed or soft_calling:
        return "admin" if is_admin else "member"
    return "autonomous"


def is_valid_group_id(group_id):
    return bool(_GROUP_ID_RE.match(str(group_id or "").strip()))
