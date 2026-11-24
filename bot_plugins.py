"""Modular plugin framework & hot-reload registry for QQ Bot「哈基米」(Python 3.9 stdlib only).

Purpose
-------
Turn all current and future non-essential capabilities into independently toggleable
plugins so users can disable unused features from the WebUI (「插件管理」) to cut
LLM prompt tokens, multimodal vision tokens, unnecessary extra turns, and background
model calls — with <=0.8s hot reload and zero edits to core runtime files when adding
new plugins.

Shared by:
- ``agy-acp-wrapper.py`` (per-turn prompt sections, sandbox GEMINI.md projection, response hooks)
- ``onebot-ws-guard.py`` (ambient/soft-call/repeat/sticker/forward gating, background memory loops, WS hooks)
- ``bot_permissions.py`` (master plugin switch precedence over granular capability policies)
- ``qq_actions.py`` / ``user_memory.py`` / ``chat_history.py`` (zero-token short-circuiting)
- ``bot_runner.py`` / ``webui/`` (sandbox sync, WebUI「插件管理」console & token budget dashboard)

Storage: ``data/bot_plugins.json`` (cross-process locked via ``file_lock.py`` and atomically replaced).
Plugins directory: ``plugins/*.py`` (auto-discovered; files starting with ``_`` are ignored).
"""

import copy
import datetime
import importlib.util
import inspect
import json
import os
import re
import sys
import tempfile
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

import bot_env  # noqa: E402,F401
from file_lock import atomic_replace, file_lock  # noqa: E402

DEFAULT_PLUGINS_FILE = os.path.join(_SCRIPT_DIR, "data", "bot_plugins.json")
DEFAULT_PLUGINS_DIR = os.path.join(_SCRIPT_DIR, "plugins")

# Keep module-level aliases for convenience/introspection
PLUGINS_FILE = os.environ.get("BOT_PLUGINS_FILE", "").strip() or DEFAULT_PLUGINS_FILE
PLUGINS_DIR = os.environ.get("BOT_PLUGINS_DIR", "").strip() or DEFAULT_PLUGINS_DIR

PLUGIN_CATEGORIES = [
    {
        "id": "interaction",
        "title": "群聊触发与互动",
        "desc": "控制主动潜水冒泡、模糊唤醒、跟风复读与 QQ 动作指令；关闭后可大幅减少群聊调用次数与每轮提示词体积",
        "icon": "chat",
        "order": 10,
    },
    {
        "id": "memory",
        "title": "记忆与历史检索",
        "desc": "控制长期记忆检索、跨会话续接、后台提炼、夜间整理与历史记录自助翻查，是后台模型与上下文 Token 的主要来源",
        "icon": "brain",
        "order": 20,
    },
    {
        "id": "capabilities",
        "title": "多模态与联网能力",
        "desc": "控制图片/表情包视觉理解与只读联网搜索，按需开启以节省视觉 Token 与搜索工具调用开销",
        "icon": "sparkles",
        "order": 30,
    },
    {
        "id": "enhancement",
        "title": "提示词与体验增强",
        "desc": "控制每轮风格提醒、扩展群聊上下文长度、少样本对话示例与长回复合并转发卡片",
        "icon": "sliders",
        "order": 40,
    },
    {
        "id": "custom",
        "title": "自定义扩展插件",
        "desc": "放入 plugins/ 目录的第三方或后续自研插件会自动发现并展示在此处",
        "icon": "puzzle",
        "order": 50,
    },
]

_CATEGORY_ORDER = {c["id"]: c["order"] for c in PLUGIN_CATEGORIES}

TOKEN_LEVEL_META = {
    "none": {"label": "零 Token 开销", "rank": 0},
    "low": {"label": "低 Token 开销", "rank": 1},
    "medium": {"label": "中 Token 开销", "rank": 2},
    "high": {"label": "高 Token 开销", "rank": 3},
    "very_high": {"label": "极高 Token 开销", "rank": 4},
}

PLUGIN_PRESETS = [
    {
        "id": "ultra_lean",
        "name": "极简省流模式",
        "badge": "省流约 80%+",
        "desc": "仅保留核心点名/@/私聊问答与极简上下文，关闭所有被动触发、后台记忆提炼、视觉与冗余提示词，最低 Token 消耗。",
        "plugins": {
            "ambient_listen": {"enabled": False},
            "soft_call_wakeup": {"enabled": False},
            "repeat_chain": {"enabled": False},
            "qq_actions": {"enabled": False},
            "memory_recall": {"enabled": False},
            "memory_continuity": {"enabled": False},
            "memory_extraction": {"enabled": False},
            "memory_sleep": {"enabled": False, "settings": {"keep_rule_cleanup": True}},
            "chat_history_lookup": {"enabled": False, "settings": {"passive_archive": True}},
            "multimodal_vision": {"enabled": False},
            "web_search": {"enabled": False},
            "style_hygiene": {"enabled": False},
            "extended_group_context": {"enabled": False, "settings": {"lean_count": 8}},
            "persona_fewshot_examples": {"enabled": False},
            "long_msg_forward": {"enabled": True},
        },
    },
    {
        "id": "token_saver",
        "name": "日常省流均衡",
        "badge": "推荐省流",
        "desc": "仅在被 @/回复/点名时回复，保留直发看图、联网搜索、已有记忆召回与长回复合并转发；关闭主动冒泡、后台自动蒸馏、历史翻查与冗余风格提醒。",
        "plugins": {
            "ambient_listen": {"enabled": False},
            "soft_call_wakeup": {"enabled": False},
            "repeat_chain": {"enabled": False},
            "qq_actions": {"enabled": False},
            "memory_recall": {"enabled": True},
            "memory_continuity": {"enabled": False},
            "memory_extraction": {"enabled": False},
            "memory_sleep": {"enabled": False, "settings": {"keep_rule_cleanup": True}},
            "chat_history_lookup": {"enabled": False, "settings": {"passive_archive": True}},
            "multimodal_vision": {
                "enabled": True,
                "settings": {
                    "direct_images": True,
                    "context_images": False,
                    "sticker_vision": False,
                    "max_images_per_turn": 2,
                },
            },
            "web_search": {"enabled": True},
            "style_hygiene": {"enabled": False},
            "extended_group_context": {"enabled": False, "settings": {"lean_count": 10}},
            "persona_fewshot_examples": {"enabled": False},
            "long_msg_forward": {"enabled": True},
        },
    },
    {
        "id": "balanced_group",
        "name": "活跃群聊均衡",
        "badge": "互动 + 控耗",
        "desc": "保留主动冒泡、复读、QQ 互动、完整记忆提炼与直发看图；关闭夜间睡眠调用、历史多轮翻查、表情包视觉与每轮风格重复提醒以控制账单。",
        "plugins": {
            "ambient_listen": {"enabled": True},
            "soft_call_wakeup": {"enabled": True},
            "repeat_chain": {"enabled": True},
            "qq_actions": {"enabled": True, "settings": {"include_denied_list": False}},
            "memory_recall": {"enabled": True},
            "memory_continuity": {"enabled": True},
            "memory_extraction": {"enabled": True},
            "memory_sleep": {"enabled": False, "settings": {"keep_rule_cleanup": True}},
            "chat_history_lookup": {"enabled": False, "settings": {"passive_archive": True}},
            "multimodal_vision": {
                "enabled": True,
                "settings": {
                    "direct_images": True,
                    "context_images": True,
                    "sticker_vision": False,
                    "max_images_per_turn": 2,
                },
            },
            "web_search": {"enabled": True},
            "style_hygiene": {"enabled": False},
            "extended_group_context": {"enabled": False, "settings": {"lean_count": 14}},
            "persona_fewshot_examples": {"enabled": False},
            "long_msg_forward": {"enabled": True},
        },
    },
    {
        "id": "full_featured",
        "name": "全功能完整模式",
        "badge": "全部开启",
        "desc": "开启所有 15 项内置插件与默认参数，获得最完整的记忆、历史翻查、群聊互动、视觉与人设体验。",
        "plugins": {
            "ambient_listen": {"enabled": True},
            "soft_call_wakeup": {"enabled": True},
            "repeat_chain": {"enabled": True},
            "qq_actions": {"enabled": True, "settings": {"include_denied_list": True}},
            "memory_recall": {"enabled": True},
            "memory_continuity": {"enabled": True},
            "memory_extraction": {"enabled": True},
            "memory_sleep": {"enabled": True, "settings": {"keep_rule_cleanup": True}},
            "chat_history_lookup": {"enabled": True, "settings": {"passive_archive": True}},
            "multimodal_vision": {
                "enabled": True,
                "settings": {
                    "direct_images": True,
                    "context_images": True,
                    "sticker_vision": True,
                    "max_images_per_turn": 4,
                },
            },
            "web_search": {"enabled": True},
            "style_hygiene": {
                "enabled": True,
                "settings": {
                    "inject_style_reminder": True,
                    "inject_motif_guard": True,
                    "casual_postprocess": True,
                },
            },
            "extended_group_context": {"enabled": True, "settings": {"lean_count": 8}},
            "persona_fewshot_examples": {"enabled": True},
            "long_msg_forward": {"enabled": True},
        },
    },
]

_PRESET_MAP = {p["id"]: p for p in PLUGIN_PRESETS}


class BotPlugin:
    """Base class for all built-in and future custom QQ bot plugins.

    To add a new non-essential feature as a plugin, drop a ``.py`` file into
    ``plugins/`` defining a subclass of ``BotPlugin`` with a unique ``id``.
    The plugin manager will automatically discover it, render its toggle and
    settings in WebUI「插件管理」, hot-reload its state, and execute its hooks
    only while enabled.
    """

    id: str = ""
    name: str = ""
    version: str = "1.0.0"
    category: str = "custom"
    order: int = 100
    description: str = ""
    author: str = "内置"
    builtin: bool = False
    default_enabled: bool = True
    hot_reload: bool = True
    affects_sandbox_prompt: bool = False
    capabilities: Tuple[str, ...] = ()
    requires: Tuple[str, ...] = ()
    env_flags: Tuple[str, ...] = ()
    related_views: Tuple[str, ...] = ()
    token_impact: Dict[str, Any] = {
        "level": "none",
        "per_turn_tokens": 0,
        "per_session_tokens": 0,
        "background_llm": False,
        "extra_turns": False,
        "summary": "无额外 Token 开销",
    }
    settings_schema: Tuple[Dict[str, Any], ...] = ()

    # Populated automatically during discovery
    source_file: str = ""

    def default_settings(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for spec in self.settings_schema or ():
            key = str(spec.get("key") or "").strip()
            if key:
                out[key] = copy.deepcopy(spec.get("default"))
        return out

    def normalize_settings(self, raw: Any) -> Dict[str, Any]:
        src = raw if isinstance(raw, dict) else {}
        out: Dict[str, Any] = {}
        for spec in self.settings_schema or ():
            key = str(spec.get("key") or "").strip()
            if not key:
                continue
            default_val = copy.deepcopy(spec.get("default"))
            if key not in src:
                out[key] = default_val
                continue
            val = src.get(key)
            stype = str(spec.get("type") or "str").lower()
            if stype == "bool":
                out[key] = _coerce_bool(val, bool(default_val))
            elif stype == "int":
                out[key] = _coerce_int(
                    val,
                    int(default_val) if default_val is not None else 0,
                    spec.get("min"),
                    spec.get("max"),
                )
            elif stype == "float":
                out[key] = _coerce_float(
                    val,
                    float(default_val) if default_val is not None else 0.0,
                    spec.get("min"),
                    spec.get("max"),
                )
            elif stype == "select":
                options = spec.get("options") or []
                allowed = [
                    str(o["value"] if isinstance(o, dict) else o) for o in options
                ]
                sval = str(val) if val is not None else str(default_val)
                out[key] = sval if (not allowed or sval in allowed) else str(default_val)
            else:
                out[key] = str(val) if val is not None else str(default_val or "")
        return out

    def estimate_token_usage(
        self, enabled: bool, settings: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Return active vs max token impact for the WebUI dashboard.

        Subclasses with granular settings (e.g. ``multimodal_vision``, ``style_hygiene``,
        ``qq_actions``) may override this to reflect partial savings accurately.
        """
        impact = self.token_impact if isinstance(self.token_impact, dict) else {}
        max_turn = max(0, int(impact.get("per_turn_tokens") or 0))
        max_sess = max(0, int(impact.get("per_session_tokens") or 0))
        bg = bool(impact.get("background_llm"))
        extra = bool(impact.get("extra_turns"))
        return {
            "per_turn_active": max_turn if enabled else 0,
            "per_turn_max": max_turn,
            "per_session_active": max_sess if enabled else 0,
            "per_session_max": max_sess,
            "background_llm_active": bg and enabled,
            "background_llm_max": bg,
            "extra_turns_active": extra and enabled,
            "extra_turns_max": extra,
        }

    # --- Sandbox GEMINI.md projection hooks ---
    def get_system_prompt_fragment(self, settings: Dict[str, Any]) -> str:
        """Optional markdown fragment appended to chat-sandbox/GEMINI.md when enabled."""
        return ""

    def filter_sandbox_gemini_lines(
        self, lines: List[str], enabled: bool, settings: Dict[str, Any]
    ) -> List[str]:
        """Optional hook to strip built-in feature lines from chat-sandbox/GEMINI.md when disabled."""
        return lines

    # --- Wrapper runtime lifecycle hooks (agy-acp-wrapper.py) ---
    def on_prompt_sections(
        self, ctx: Dict[str, Any], settings: Dict[str, Any]
    ) -> List[str]:
        """Return extra prompt text blocks to inject into the current turn."""
        return []

    def on_response_postprocess(
        self, text: str, ctx: Dict[str, Any], settings: Dict[str, Any]
    ) -> str:
        """Inspect or transform final assistant text before sending to QQ."""
        return text

    # --- Gateway runtime lifecycle hooks (onebot-ws-guard.py) ---
    def on_ws_inbound(
        self, event: Dict[str, Any], ctx: Dict[str, Any], settings: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """Optional hook for inbound OneBot events.

        Return ``{"drop": True}`` to silently drop the event, ``{"event": new_event}``
        to pass a modified event dict, or ``None`` to pass through unchanged.
        """
        return None

    def on_ws_outbound(
        self, payload: Dict[str, Any], ctx: Dict[str, Any], settings: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """Optional hook for outbound OneBot API payloads.

        Return ``{"drop": True}`` to suppress the send (with synthetic OK ACK),
        ``{"payload": new_payload}`` to replace the payload, or ``None`` unchanged.
        """
        return None


# ---------------------------------------------------------------------------
# Coercion & text helpers
# ---------------------------------------------------------------------------

def _coerce_bool(val: Any, default: bool = False) -> bool:
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return bool(val)
    if isinstance(val, str):
        low = val.strip().lower()
        if low in ("1", "true", "yes", "on", "y"):
            return True
        if low in ("0", "false", "no", "off", "n", ""):
            return False
    return default


def _coerce_int(
    val: Any, default: int, min_v: Optional[int], max_v: Optional[int]
) -> int:
    try:
        n = int(val)
    except (TypeError, ValueError):
        n = default
    if min_v is not None:
        n = max(int(min_v), n)
    if max_v is not None:
        n = min(int(max_v), n)
    return n


def _coerce_float(
    val: Any, default: float, min_v: Optional[float], max_v: Optional[float]
) -> float:
    try:
        n = float(val)
    except (TypeError, ValueError):
        n = default
    if min_v is not None:
        n = max(float(min_v), n)
    if max_v is not None:
        n = min(float(max_v), n)
    return n


def _now_iso() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


_CJK_RE = re.compile(r"[\u3400-\u9fff\uf900-\ufaff\u3000-\u303f\uff00-\uffef]")


def estimate_tokens(text: str) -> int:
    """Calibrated token estimator for mixed Chinese + ASCII text (stdlib only)."""
    if not text:
        return 0
    cjk = len(_CJK_RE.findall(text))
    non_cjk = max(0, len(text) - cjk)
    return int(round(cjk * 0.8 + non_cjk * 0.32))


def get_plugins_file() -> str:
    env_p = os.environ.get("BOT_PLUGINS_FILE", "").strip()
    return env_p if env_p else DEFAULT_PLUGINS_FILE


def get_plugins_dir() -> str:
    env_p = os.environ.get("BOT_PLUGINS_DIR", "").strip()
    return env_p if env_p else DEFAULT_PLUGINS_DIR


# ---------------------------------------------------------------------------
# Dynamic plugin auto-discovery
# ---------------------------------------------------------------------------

_registry_lock = threading.Lock()
_registry_dir: Optional[str] = None
_registry_plugins: Dict[str, BotPlugin] = {}
_registry_cap_map: Dict[str, BotPlugin] = {}
_registry_load_errors: List[Dict[str, str]] = []
_registry_loaded = False

_PLUGIN_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,48}$")


def _extract_plugin_instances(mod: Any, file_path: str) -> List[BotPlugin]:
    found: List[BotPlugin] = []
    # Pattern 1: explicit create_plugin() factory or PLUGIN instance
    factory = getattr(mod, "create_plugin", None)
    if callable(factory):
        obj = factory()
        if isinstance(obj, BotPlugin):
            found.append(obj)
    elif isinstance(getattr(mod, "PLUGIN", None), BotPlugin):
        found.append(getattr(mod, "PLUGIN"))
    else:
        # Pattern 2: discover BotPlugin subclasses defined in this module
        for _, cls in inspect.getmembers(mod, inspect.isclass):
            if (
                issubclass(cls, BotPlugin)
                and cls is not BotPlugin
                and cls.__module__ == mod.__name__
            ):
                found.append(cls())
    for inst in found:
        inst.source_file = file_path
    return found


def discover_plugins(force: bool = False) -> Dict[str, BotPlugin]:
    """Auto-discover and instantiate all plugins under ``plugins/*.py``."""
    global _registry_dir, _registry_plugins, _registry_cap_map
    global _registry_load_errors, _registry_loaded

    pdir = os.path.abspath(get_plugins_dir())
    with _registry_lock:
        if _registry_loaded and not force and _registry_dir == pdir:
            return dict(_registry_plugins)

        loaded: Dict[str, BotPlugin] = {}
        cap_map: Dict[str, BotPlugin] = {}
        errors: List[Dict[str, str]] = []

        if os.path.isdir(pdir):
            entries = sorted(os.listdir(pdir))
            for fname in entries:
                if not fname.endswith(".py") or fname.startswith("_"):
                    continue
                fpath = os.path.join(pdir, fname)
                if not os.path.isfile(fpath):
                    continue
                stem = fname[:-3]
                mod_name = "_hajimi_plugin_{}_{}".format(
                    re.sub(r"[^0-9a-zA-Z_]", "_", stem),
                    abs(hash(fpath)) % 1000000,
                )
                try:
                    spec = importlib.util.spec_from_file_location(mod_name, fpath)
                    if spec is None or spec.loader is None:
                        errors.append(
                            {"file": fname, "error": "无法创建模块加载规范 (spec_from_file_location)"}
                        )
                        continue
                    mod = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(mod)
                    instances = _extract_plugin_instances(mod, fpath)
                    if not instances:
                        errors.append(
                            {
                                "file": fname,
                                "error": "未找到 BotPlugin 子类、PLUGIN 实例或 create_plugin()",
                            }
                        )
                        continue
                    for inst in instances:
                        pid = str(getattr(inst, "id", "") or "").strip()
                        if not _PLUGIN_ID_RE.match(pid):
                            errors.append(
                                {
                                    "file": fname,
                                    "error": "非法插件 ID {!r}（需为小写字母开头的字母/数字/下划线）".format(pid),
                                }
                            )
                            continue
                        if pid in loaded:
                            errors.append(
                                {
                                    "file": fname,
                                    "error": "重复插件 ID {!r}，已被 {} 注册".format(
                                        pid, os.path.basename(loaded[pid].source_file)
                                    ),
                                }
                            )
                            continue
                        if not inst.name:
                            inst.name = pid
                        if inst.category not in _CATEGORY_ORDER:
                            inst.category = "custom"
                        loaded[pid] = inst
                        for cap_id in inst.capabilities or ():
                            cap_map[str(cap_id)] = inst
                except Exception as exc:
                    errors.append({"file": fname, "error": str(exc)})

        _registry_dir = pdir
        _registry_plugins = loaded
        _registry_cap_map = cap_map
        _registry_load_errors = errors
        _registry_loaded = True
        return dict(_registry_plugins)


def list_plugins(force_discover: bool = False) -> List[BotPlugin]:
    plugins = discover_plugins(force=force_discover)
    return sorted(
        plugins.values(),
        key=lambda p: (_CATEGORY_ORDER.get(p.category, 99), int(getattr(p, "order", 100)), p.id),
    )


def get_plugin(plugin_id: str) -> Optional[BotPlugin]:
    return discover_plugins().get(str(plugin_id or "").strip())


def get_load_errors() -> List[Dict[str, str]]:
    discover_plugins()
    with _registry_lock:
        return list(_registry_load_errors)


def get_capability_plugin(cap_id: str) -> Optional[BotPlugin]:
    discover_plugins()
    with _registry_lock:
        return _registry_cap_map.get(str(cap_id or "").strip())


# ---------------------------------------------------------------------------
# Configuration normalization, atomic persistence & <=0.8s hot-reload cache
# ---------------------------------------------------------------------------

_cache_lock = threading.Lock()
_cached_path: Optional[str] = None
_cached_config: Optional[Dict[str, Any]] = None
_cached_sig: Optional[Tuple[int, int]] = None
_last_stat_check: float = 0.0
_STAT_CHECK_INTERVAL = 0.8


def default_config() -> Dict[str, Any]:
    plugins = discover_plugins()
    entries: Dict[str, Dict[str, Any]] = {}
    for pid, inst in plugins.items():
        entries[pid] = {
            "enabled": bool(inst.default_enabled),
            "settings": inst.default_settings(),
        }
    return {
        "version": 1,
        "updated_at": "",
        "plugins": entries,
    }


def normalize_config(raw: Any) -> Dict[str, Any]:
    plugins = discover_plugins()
    src = raw if isinstance(raw, dict) else {}
    src_plugins = src.get("plugins") if isinstance(src.get("plugins"), dict) else {}

    normalized_plugins: Dict[str, Dict[str, Any]] = {}
    for pid, inst in plugins.items():
        entry = src_plugins.get(pid)
        if isinstance(entry, bool):
            entry = {"enabled": entry}
        elif not isinstance(entry, dict):
            entry = {}
        enabled = _coerce_bool(entry.get("enabled"), bool(inst.default_enabled))
        settings = inst.normalize_settings(entry.get("settings"))
        normalized_plugins[pid] = {
            "enabled": enabled,
            "settings": settings,
        }

    # Preserve entries for temporarily removed custom plugins so config isn't lost
    for pid, entry in src_plugins.items():
        if pid in normalized_plugins:
            continue
        if isinstance(pid, str) and _PLUGIN_ID_RE.match(pid) and isinstance(entry, dict):
            normalized_plugins[pid] = {
                "enabled": _coerce_bool(entry.get("enabled"), True),
                "settings": copy.deepcopy(entry.get("settings"))
                if isinstance(entry.get("settings"), dict)
                else {},
            }

    return {
        "version": 1,
        "updated_at": str(src.get("updated_at") or ""),
        "plugins": normalized_plugins,
    }


def invalidate_cache() -> None:
    global _cached_config, _cached_sig, _last_stat_check
    with _cache_lock:
        _cached_config = None
        _cached_sig = None
        _last_stat_check = 0.0


def load_config(force: bool = False) -> Dict[str, Any]:
    """Hot-reloadable plugin config loader (caches mtime_ns + size, checks every 0.8s)."""
    global _cached_path, _cached_config, _cached_sig, _last_stat_check

    path = os.path.abspath(get_plugins_file())
    now = time.monotonic()
    with _cache_lock:
        if (
            not force
            and _cached_config is not None
            and _cached_path == path
            and (now - _last_stat_check) < _STAT_CHECK_INTERVAL
        ):
            return _cached_config

        _last_stat_check = now
        try:
            st = os.stat(path)
            sig: Optional[Tuple[int, int]] = (st.st_mtime_ns, st.st_size)
        except OSError:
            sig = None

        if not force and _cached_config is not None and _cached_path == path and sig == _cached_sig:
            return _cached_config

        raw: Any = None
        if sig is not None:
            lock_path = path + ".lock"
            try:
                with file_lock(lock_path, exclusive=False):
                    with open(path, "r", encoding="utf-8-sig") as f:
                        raw = json.load(f)
            except Exception:
                raw = None

        cfg = normalize_config(raw)
        _cached_path = path
        _cached_config = cfg
        _cached_sig = sig
        return cfg


def save_config(raw: Any) -> Dict[str, Any]:
    """Normalize and atomically persist ``data/bot_plugins.json``."""
    global _cached_path, _cached_config, _cached_sig, _last_stat_check

    cfg = normalize_config(raw)
    cfg["updated_at"] = _now_iso()

    path = os.path.abspath(get_plugins_file())
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    lock_path = path + ".lock"
    payload = json.dumps(cfg, ensure_ascii=False, indent=2) + "\n"

    with _cache_lock:
        with file_lock(lock_path, exclusive=True):
            fd, tmp_path = tempfile.mkstemp(
                prefix=".bot_plugins.", suffix=".tmp", dir=parent or "."
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                    f.write(payload)
                    f.flush()
                    os.fsync(f.fileno())
                atomic_replace(tmp_path, path)
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
            try:
                st = os.stat(path)
                sig: Optional[Tuple[int, int]] = (st.st_mtime_ns, st.st_size)
            except OSError:
                sig = None
        _cached_path = path
        _cached_config = cfg
        _cached_sig = sig
        _last_stat_check = time.monotonic()

    return copy.deepcopy(cfg)


def update_plugin(
    plugin_id: str,
    enabled: Optional[bool] = None,
    settings: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    pid = str(plugin_id or "").strip()
    if not get_plugin(pid):
        raise ValueError("未知插件：{}".format(pid))
    cfg = copy.deepcopy(load_config(force=True))
    entry = cfg["plugins"].setdefault(pid, {"enabled": True, "settings": {}})
    if enabled is not None:
        entry["enabled"] = _coerce_bool(enabled, bool(entry.get("enabled", True)))
    if isinstance(settings, dict):
        merged = dict(entry.get("settings") or {})
        merged.update(settings)
        entry["settings"] = merged
    return save_config(cfg)


def bulk_update(plugins_patch: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(plugins_patch, dict):
        raise ValueError("plugins 字段必须是对象")
    cfg = copy.deepcopy(load_config(force=True))
    discovered = discover_plugins()
    for pid, patch in plugins_patch.items():
        if pid not in discovered:
            continue
        if isinstance(patch, bool):
            patch = {"enabled": patch}
        if not isinstance(patch, dict):
            continue
        entry = cfg["plugins"].setdefault(
            pid,
            {
                "enabled": bool(discovered[pid].default_enabled),
                "settings": discovered[pid].default_settings(),
            },
        )
        if "enabled" in patch:
            entry["enabled"] = _coerce_bool(patch["enabled"], bool(entry["enabled"]))
        if isinstance(patch.get("settings"), dict):
            merged = dict(entry.get("settings") or {})
            merged.update(patch["settings"])
            entry["settings"] = merged
    return save_config(cfg)


def apply_preset(preset_id: str) -> Dict[str, Any]:
    preset = _PRESET_MAP.get(str(preset_id or "").strip())
    if not preset:
        raise ValueError("未知插件预设：{}".format(preset_id))
    cfg = copy.deepcopy(load_config(force=True))
    discovered = discover_plugins()
    for pid, spec in (preset.get("plugins") or {}).items():
        if pid not in discovered:
            continue
        entry = cfg["plugins"].setdefault(
            pid,
            {
                "enabled": bool(discovered[pid].default_enabled),
                "settings": discovered[pid].default_settings(),
            },
        )
        if "enabled" in spec:
            entry["enabled"] = bool(spec["enabled"])
        if isinstance(spec.get("settings"), dict):
            merged = discovered[pid].default_settings()
            merged.update(spec["settings"])
            entry["settings"] = merged
        else:
            # Reset to defaults when preset doesn't override settings? Keep clean defaults on full_featured
            if preset_id == "full_featured":
                entry["settings"] = discovered[pid].default_settings()
    return save_config(cfg)


def reset_config() -> Dict[str, Any]:
    return save_config(default_config())


# ---------------------------------------------------------------------------
# Fast hot-reloaded runtime queries
# ---------------------------------------------------------------------------

def is_enabled(
    plugin_id: str,
    config: Optional[Dict[str, Any]] = None,
    _seen: Optional[set] = None,
) -> bool:
    """Return True if ``plugin_id`` (and any declared dependencies) are enabled."""
    pid = str(plugin_id or "").strip()
    if not pid:
        return False
    cfg = config if isinstance(config, dict) else load_config()
    inst = get_plugin(pid)
    entry = (cfg.get("plugins") or {}).get(pid)
    if isinstance(entry, dict) and "enabled" in entry:
        flag = bool(entry["enabled"])
    elif inst is not None:
        flag = bool(inst.default_enabled)
    else:
        return False
    if not flag:
        return False
    if inst is not None and inst.requires:
        seen = _seen if _seen is not None else set()
        if pid in seen:
            return True
        seen.add(pid)
        for req_id in inst.requires:
            if not is_enabled(req_id, config=cfg, _seen=seen):
                return False
    return True


def get_settings(
    plugin_id: str, config: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    pid = str(plugin_id or "").strip()
    cfg = config if isinstance(config, dict) else load_config()
    entry = (cfg.get("plugins") or {}).get(pid)
    if isinstance(entry, dict) and isinstance(entry.get("settings"), dict):
        return dict(entry["settings"])
    inst = get_plugin(pid)
    return inst.default_settings() if inst is not None else {}


def get_setting(
    plugin_id: str,
    key: str,
    default: Any = None,
    config: Optional[Dict[str, Any]] = None,
) -> Any:
    settings = get_settings(plugin_id, config=config)
    if key in settings:
        return settings[key]
    return default


def check_capability_plugin(
    cap_id: str, config: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Check whether the parent plugin owning ``cap_id`` is currently enabled."""
    plugin = get_capability_plugin(cap_id)
    if plugin is None:
        return {"allowed": True, "plugin_id": "", "plugin_name": "", "reason": ""}
    if is_enabled(plugin.id, config=config):
        return {
            "allowed": True,
            "plugin_id": plugin.id,
            "plugin_name": plugin.name,
            "reason": "",
        }
    return {
        "allowed": False,
        "plugin_id": plugin.id,
        "plugin_name": plugin.name,
        "reason": "所属插件「{}」已在插件管理中关闭".format(plugin.name),
    }


# ---------------------------------------------------------------------------
# Sandbox GEMINI.md dynamic compiler (non-destructive projection!)
# ---------------------------------------------------------------------------

_PLUGIN_PROMPT_BEGIN = "<!-- HAJIMI_PLUGIN_PROMPTS_BEGIN -->"
_PLUGIN_PROMPT_END = "<!-- HAJIMI_PLUGIN_PROMPTS_END -->"


def render_sandbox_gemini_md(
    workspace_text: str, config: Optional[Dict[str, Any]] = None
) -> str:
    """Compile effective ``chat-sandbox/GEMINI.md`` from ``workspace/GEMINI.md``.

    - Never modifies ``workspace/GEMINI.md`` (the user's editable source of truth).
    - When all built-in plugins are enabled and no custom plugins contribute system
      prompt fragments, returns ``workspace_text`` byte-for-byte unchanged.
    - When built-in plugins are disabled, invokes each plugin's
      ``filter_sandbox_gemini_lines()`` to strip unused feature instructions/examples.
    - When enabled plugins (especially future custom plugins) define
      ``get_system_prompt_fragment()``, appends them cleanly in a delimited section.
    """
    if not workspace_text:
        return ""

    cfg = config if isinstance(config, dict) else load_config()
    plugins = list_plugins()

    has_trailing_nl = workspace_text.endswith("\n")
    lines = workspace_text.splitlines()
    modified = False

    for inst in plugins:
        enabled = is_enabled(inst.id, config=cfg)
        settings = get_settings(inst.id, config=cfg)
        try:
            new_lines = inst.filter_sandbox_gemini_lines(list(lines), enabled, settings)
            if isinstance(new_lines, list) and new_lines != lines:
                lines = new_lines
                modified = True
        except Exception:
            pass

    fragments: List[Tuple[str, str]] = []
    for inst in plugins:
        if not is_enabled(inst.id, config=cfg):
            continue
        settings = get_settings(inst.id, config=cfg)
        try:
            frag = str(inst.get_system_prompt_fragment(settings) or "").strip()
            if frag:
                fragments.append((inst.name, frag))
        except Exception:
            pass

    if not modified and not fragments:
        return workspace_text

    # Collapse 3+ consecutive blank lines into at most 1 blank line
    cleaned: List[str] = []
    blank_run = 0
    for line in lines:
        if line.strip() == "":
            blank_run += 1
            if blank_run <= 1:
                cleaned.append("")
        else:
            blank_run = 0
            cleaned.append(line)
    while cleaned and cleaned[-1] == "":
        cleaned.pop()

    text = "\n".join(cleaned)
    if fragments:
        parts = [
            "",
            _PLUGIN_PROMPT_BEGIN,
            "## 扩展插件指令",
            "",
        ]
        for pname, frag in fragments:
            parts.append("### {}".format(pname))
            parts.append(frag)
            parts.append("")
        parts.append(_PLUGIN_PROMPT_END)
        text = text.rstrip("\n") + "\n" + "\n".join(parts)

    if has_trailing_nl or fragments:
        text += "\n"
    return text


# ---------------------------------------------------------------------------
# Error-isolated custom plugin lifecycle hook runners
# ---------------------------------------------------------------------------

def _log_hook_error(plugin_id: str, hook_name: str, exc: Exception) -> None:
    try:
        sys.stderr.write(
            "[bot_plugins] plugin={!r} hook={!r} error: {}\n".format(
                plugin_id, hook_name, exc
            )
        )
        sys.stderr.flush()
    except Exception:
        pass


def run_prompt_hooks(
    ctx: Dict[str, Any], config: Optional[Dict[str, Any]] = None
) -> List[str]:
    """Run ``on_prompt_sections`` across all enabled plugins (error-isolated)."""
    cfg = config if isinstance(config, dict) else load_config()
    out: List[str] = []
    for inst in list_plugins():
        if not is_enabled(inst.id, config=cfg):
            continue
        # Fast-path skip if plugin uses base no-op implementation
        if type(inst).on_prompt_sections is BotPlugin.on_prompt_sections:
            continue
        settings = get_settings(inst.id, config=cfg)
        try:
            sections = inst.on_prompt_sections(ctx, settings)
            if isinstance(sections, str) and sections.strip():
                out.append(sections.strip())
            elif isinstance(sections, (list, tuple)):
                for s in sections:
                    if isinstance(s, str) and s.strip():
                        out.append(s.strip())
        except Exception as exc:
            _log_hook_error(inst.id, "on_prompt_sections", exc)
    return out


def run_response_hooks(
    text: str, ctx: Dict[str, Any], config: Optional[Dict[str, Any]] = None
) -> str:
    """Run ``on_response_postprocess`` across all enabled plugins (error-isolated)."""
    cfg = config if isinstance(config, dict) else load_config()
    current = text
    for inst in list_plugins():
        if not is_enabled(inst.id, config=cfg):
            continue
        if type(inst).on_response_postprocess is BotPlugin.on_response_postprocess:
            continue
        settings = get_settings(inst.id, config=cfg)
        try:
            res = inst.on_response_postprocess(current, ctx, settings)
            if isinstance(res, str):
                current = res
        except Exception as exc:
            _log_hook_error(inst.id, "on_response_postprocess", exc)
    return current


def run_ws_inbound_hooks(
    event: Dict[str, Any],
    ctx: Dict[str, Any],
    config: Optional[Dict[str, Any]] = None,
) -> Tuple[Dict[str, Any], bool]:
    """Run ``on_ws_inbound`` across all enabled plugins. Returns ``(event, drop)``."""
    cfg = config if isinstance(config, dict) else load_config()
    cur = event
    for inst in list_plugins():
        if not is_enabled(inst.id, config=cfg):
            continue
        if type(inst).on_ws_inbound is BotPlugin.on_ws_inbound:
            continue
        settings = get_settings(inst.id, config=cfg)
        try:
            res = inst.on_ws_inbound(cur, ctx, settings)
            if isinstance(res, dict):
                if res.get("drop"):
                    return cur, True
                if isinstance(res.get("event"), dict):
                    cur = res["event"]
        except Exception as exc:
            _log_hook_error(inst.id, "on_ws_inbound", exc)
    return cur, False


def run_ws_outbound_hooks(
    payload: Dict[str, Any],
    ctx: Dict[str, Any],
    config: Optional[Dict[str, Any]] = None,
) -> Tuple[Dict[str, Any], bool]:
    """Run ``on_ws_outbound`` across all enabled plugins. Returns ``(payload, drop)``."""
    cfg = config if isinstance(config, dict) else load_config()
    cur = payload
    for inst in list_plugins():
        if not is_enabled(inst.id, config=cfg):
            continue
        if type(inst).on_ws_outbound is BotPlugin.on_ws_outbound:
            continue
        settings = get_settings(inst.id, config=cfg)
        try:
            res = inst.on_ws_outbound(cur, ctx, settings)
            if isinstance(res, dict):
                if res.get("drop"):
                    return cur, True
                if isinstance(res.get("payload"), dict):
                    cur = res["payload"]
        except Exception as exc:
            _log_hook_error(inst.id, "on_ws_outbound", exc)
    return cur, False


# ---------------------------------------------------------------------------
# Token budget calculation & WebUI full state snapshot
# ---------------------------------------------------------------------------

def _read_workspace_gemini_text() -> str:
    work_dir = os.environ.get("AGY_WORK_DIR", "").strip() or os.path.join(
        _SCRIPT_DIR, "workspace"
    )
    path = os.path.join(work_dir, "GEMINI.md")
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            return f.read()
    except OSError:
        return ""


def compute_token_budget(
    config: Optional[Dict[str, Any]] = None,
    workspace_gemini_text: Optional[str] = None,
) -> Dict[str, Any]:
    """Calculate live active vs saved Token estimates across all plugins & GEMINI.md."""
    cfg = config if isinstance(config, dict) else load_config()
    plugins = list_plugins()

    enabled_count = 0
    builtin_count = 0
    custom_count = 0
    per_turn_active = 0
    per_turn_max = 0
    per_session_active = 0
    per_session_max = 0
    bg_active = 0
    bg_max = 0
    extra_active = 0
    extra_max = 0
    per_plugin: Dict[str, Dict[str, Any]] = {}

    for inst in plugins:
        if inst.builtin:
            builtin_count += 1
        else:
            custom_count += 1
        enabled = is_enabled(inst.id, config=cfg)
        if enabled:
            enabled_count += 1
        settings = get_settings(inst.id, config=cfg)
        est = inst.estimate_token_usage(enabled, settings)
        per_plugin[inst.id] = est

        per_turn_active += int(est.get("per_turn_active") or 0)
        per_turn_max += int(est.get("per_turn_max") or 0)
        per_session_active += int(est.get("per_session_active") or 0)
        per_session_max += int(est.get("per_session_max") or 0)
        if est.get("background_llm_max"):
            bg_max += 1
            if est.get("background_llm_active"):
                bg_active += 1
        if est.get("extra_turns_max"):
            extra_max += 1
            if est.get("extra_turns_active"):
                extra_active += 1

    raw_gemini = (
        workspace_gemini_text
        if workspace_gemini_text is not None
        else _read_workspace_gemini_text()
    )
    compiled_gemini = render_sandbox_gemini_md(raw_gemini, config=cfg) if raw_gemini else ""
    raw_gemini_chars = len(raw_gemini)
    compiled_gemini_chars = len(compiled_gemini)
    saved_gemini_chars = max(0, raw_gemini_chars - compiled_gemini_chars)
    raw_gemini_tokens = estimate_tokens(raw_gemini)
    compiled_gemini_tokens = estimate_tokens(compiled_gemini)
    saved_gemini_tokens = max(0, raw_gemini_tokens - compiled_gemini_tokens)

    per_turn_saved = max(0, per_turn_max - per_turn_active)
    per_turn_save_pct = (
        int(round((per_turn_saved / float(per_turn_max)) * 100))
        if per_turn_max > 0
        else 0
    )
    per_session_saved = max(0, per_session_max - per_session_active)
    per_session_save_pct = (
        int(round((per_session_saved / float(per_session_max)) * 100))
        if per_session_max > 0
        else 0
    )

    return {
        "enabled_count": enabled_count,
        "disabled_count": max(0, len(plugins) - enabled_count),
        "total_count": len(plugins),
        "builtin_count": builtin_count,
        "custom_count": custom_count,
        "per_turn_active_tokens": per_turn_active,
        "per_turn_max_tokens": per_turn_max,
        "per_turn_saved_tokens": per_turn_saved,
        "per_turn_save_pct": per_turn_save_pct,
        "per_session_active_tokens": per_session_active,
        "per_session_max_tokens": per_session_max,
        "per_session_saved_tokens": per_session_saved,
        "per_session_save_pct": per_session_save_pct,
        "sandbox_gemini_raw_chars": raw_gemini_chars,
        "sandbox_gemini_compiled_chars": compiled_gemini_chars,
        "sandbox_gemini_saved_chars": saved_gemini_chars,
        "sandbox_gemini_raw_tokens": raw_gemini_tokens,
        "sandbox_gemini_compiled_tokens": compiled_gemini_tokens,
        "sandbox_gemini_saved_tokens": saved_gemini_tokens,
        "background_llm_active_count": bg_active,
        "background_llm_max_count": bg_max,
        "background_llm_disabled_count": max(0, bg_max - bg_active),
        "extra_turns_active_count": extra_active,
        "extra_turns_max_count": extra_max,
        "extra_turns_disabled_count": max(0, extra_max - extra_active),
        "per_plugin": per_plugin,
    }


def _detect_matching_preset(cfg: Dict[str, Any]) -> str:
    plugins_cfg = cfg.get("plugins") or {}
    for preset in PLUGIN_PRESETS:
        matched = True
        for pid, spec in (preset.get("plugins") or {}).items():
            cur = plugins_cfg.get(pid)
            if not isinstance(cur, dict):
                matched = False
                break
            if "enabled" in spec and bool(cur.get("enabled")) != bool(spec["enabled"]):
                matched = False
                break
        if matched:
            return preset["id"]
    return "custom"


def build_ui_state(
    env_map: Optional[Dict[str, str]] = None,
    workspace_gemini_text: Optional[str] = None,
    force: bool = False,
) -> Dict[str, Any]:
    """Build complete declarative payload for WebUI「插件管理」."""
    if force:
        discover_plugins(force=True)
    cfg = load_config(force=force)
    plugins = list_plugins()
    budget = compute_token_budget(config=cfg, workspace_gemini_text=workspace_gemini_text)
    env_src = env_map if isinstance(env_map, dict) else {}

    catalog_items: List[Dict[str, Any]] = []
    for inst in plugins:
        entry = (cfg.get("plugins") or {}).get(inst.id) or {}
        enabled_raw = bool(entry.get("enabled", inst.default_enabled))
        effective_enabled = is_enabled(inst.id, config=cfg)
        settings = get_settings(inst.id, config=cfg)
        est = budget["per_plugin"].get(inst.id) or inst.estimate_token_usage(
            effective_enabled, settings
        )

        # Cross-check underlying bot.env toggles so WebUI can warn if env disables it
        env_disabled_keys: List[str] = []
        for ek in inst.env_flags or ():
            val = env_src.get(ek)
            if val is None:
                val = os.environ.get(ek)
            if val is not None and str(val).strip().lower() in ("0", "false", "no", "off"):
                env_disabled_keys.append(ek)

        missing_deps = [
            req_id for req_id in (inst.requires or ()) if not is_enabled(req_id, config=cfg)
        ]

        impact = dict(inst.token_impact) if isinstance(inst.token_impact, dict) else {}
        lvl = str(impact.get("level") or "none")
        impact["level_label"] = TOKEN_LEVEL_META.get(lvl, TOKEN_LEVEL_META["none"])["label"]

        rel_source = ""
        if inst.source_file:
            try:
                rel_source = os.path.relpath(inst.source_file, _SCRIPT_DIR).replace("\\", "/")
            except ValueError:
                rel_source = inst.source_file

        catalog_items.append(
            {
                "id": inst.id,
                "name": inst.name,
                "version": inst.version,
                "category": inst.category,
                "order": int(getattr(inst, "order", 100)),
                "description": inst.description,
                "author": inst.author,
                "builtin": bool(inst.builtin),
                "default_enabled": bool(inst.default_enabled),
                "hot_reload": bool(inst.hot_reload),
                "affects_sandbox_prompt": bool(inst.affects_sandbox_prompt),
                "capabilities": list(inst.capabilities or ()),
                "requires": list(inst.requires or ()),
                "missing_requires": missing_deps,
                "env_flags": list(inst.env_flags or ()),
                "env_disabled_keys": env_disabled_keys,
                "related_views": list(inst.related_views or ()),
                "token_impact": impact,
                "token_estimate": est,
                "settings_schema": list(inst.settings_schema or ()),
                "enabled": enabled_raw,
                "effective_enabled": effective_enabled,
                "settings": settings,
                "source_file": rel_source,
            }
        )

    return {
        "version": cfg.get("version", 1),
        "updated_at": cfg.get("updated_at", ""),
        "file_path": os.path.relpath(os.path.abspath(get_plugins_file()), _SCRIPT_DIR).replace("\\", "/")
        if os.path.abspath(get_plugins_file()).startswith(_SCRIPT_DIR)
        else os.path.abspath(get_plugins_file()),
        "plugins_dir": os.path.relpath(os.path.abspath(get_plugins_dir()), _SCRIPT_DIR).replace("\\", "/")
        if os.path.abspath(get_plugins_dir()).startswith(_SCRIPT_DIR)
        else os.path.abspath(get_plugins_dir()),
        "active_preset": _detect_matching_preset(cfg),
        "categories": PLUGIN_CATEGORIES,
        "presets": [
            {
                "id": p["id"],
                "name": p["name"],
                "badge": p["badge"],
                "desc": p["desc"],
                "enabled_count": sum(
                    1 for v in (p.get("plugins") or {}).values() if v.get("enabled")
                ),
            }
            for p in PLUGIN_PRESETS
        ],
        "plugins": catalog_items,
        "config": cfg,
        "summary": {k: v for k, v in budget.items() if k != "per_plugin"},
        "load_errors": get_load_errors(),
    }
