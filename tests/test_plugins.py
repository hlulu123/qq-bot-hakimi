"""Comprehensive unit & integration test suite for the modular Plugin System.

Runs on Python 3.9+ stdlib only (`python3 -m unittest tests/test_plugins.py -v`).
Fully isolated via temporary directories and env vars — never mutates live `data/`.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

import bot_permissions  # noqa: E402
import bot_plugins  # noqa: E402
import build_packages  # noqa: E402
import chat_history  # noqa: E402
import qq_actions  # noqa: E402
import user_memory  # noqa: E402
from webui import services as webui_services  # noqa: E402
from webui import stores as webui_stores  # noqa: E402


class _PluginTestBase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hajimi_plugin_test_")
        self._orig_env = {
            k: os.environ.get(k)
            for k in (
                "BOT_PLUGINS_FILE",
                "BOT_PLUGINS_DIR",
                "BOT_PERMISSIONS_FILE",
                "MEMORY_DB_PATH",
                "USER_MEMORY_ENABLED",
            )
        }
        self._orig_mem_enabled = user_memory.MEMORY_ENABLED
        self._orig_mem_db_path = user_memory.MEMORY_DB_PATH

        self.plugins_file = os.path.join(self._tmpdir, "bot_plugins.json")
        self.perms_file = os.path.join(self._tmpdir, "bot_permissions.json")
        self.mem_db = os.path.join(self._tmpdir, "memory.db")

        os.environ["BOT_PLUGINS_FILE"] = self.plugins_file
        os.environ.pop("BOT_PLUGINS_DIR", None)
        os.environ["BOT_PERMISSIONS_FILE"] = self.perms_file
        os.environ["MEMORY_DB_PATH"] = self.mem_db
        os.environ["USER_MEMORY_ENABLED"] = "1"
        user_memory.MEMORY_ENABLED = True
        user_memory.MEMORY_DB_PATH = self.mem_db

        bot_plugins.discover_plugins(force=True)
        bot_plugins.invalidate_cache()

    def tearDown(self):
        for k, v in self._orig_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        user_memory.MEMORY_ENABLED = self._orig_mem_enabled
        user_memory.MEMORY_DB_PATH = self._orig_mem_db_path
        bot_plugins.discover_plugins(force=True)
        bot_plugins.invalidate_cache()
        shutil.rmtree(self._tmpdir, ignore_errors=True)


class TestPluginDiscoveryAndPersistence(_PluginTestBase):
    def test_builtin_plugins_discovered_cleanly(self):
        plugins = bot_plugins.list_plugins(force_discover=True)
        errors = bot_plugins.get_load_errors()
        self.assertEqual(errors, [])
        self.assertEqual(len(plugins), 15)
        ids = {p.id for p in plugins}
        for expected in (
            "ambient_listen",
            "soft_call_wakeup",
            "repeat_chain",
            "qq_actions",
            "memory_recall",
            "memory_continuity",
            "memory_extraction",
            "memory_sleep",
            "chat_history_lookup",
            "multimodal_vision",
            "web_search",
            "style_hygiene",
            "extended_group_context",
            "persona_fewshot_examples",
            "long_msg_forward",
        ):
            self.assertIn(expected, ids)

    def test_hot_reload_persistence_and_presets(self):
        # Missing file falls back cleanly to defaults (all enabled)
        self.assertTrue(bot_plugins.is_enabled("qq_actions"))
        self.assertFalse(os.path.exists(self.plugins_file))

        # Apply ultra_lean preset
        cfg = bot_plugins.apply_preset("ultra_lean")
        self.assertTrue(os.path.exists(self.plugins_file))
        self.assertFalse(bot_plugins.is_enabled("qq_actions"))
        self.assertFalse(bot_plugins.is_enabled("ambient_listen"))
        self.assertFalse(bot_plugins.is_enabled("memory_extraction"))
        self.assertTrue(bot_plugins.is_enabled("long_msg_forward"))
        self.assertEqual(cfg["plugins"]["extended_group_context"]["settings"]["lean_count"], 8)

        # Simulate external process edit & verify hot-reload after invalidation/mtime change
        with open(self.plugins_file, "r", encoding="utf-8") as f:
            disk = json.load(f)
        disk["plugins"]["web_search"]["enabled"] = True
        with open(self.plugins_file, "w", encoding="utf-8") as f:
            json.dump(disk, f)
        bot_plugins.invalidate_cache()
        self.assertTrue(bot_plugins.is_enabled("web_search"))

        # Reset restores all built-ins enabled
        bot_plugins.reset_config()
        self.assertTrue(bot_plugins.is_enabled("qq_actions"))
        self.assertTrue(bot_plugins.is_enabled("ambient_listen"))

    def test_custom_dropin_plugin_hooks_and_error_isolation(self):
        custom_dir = os.path.join(self._tmpdir, "custom_plugins")
        os.makedirs(custom_dir, exist_ok=True)
        # Copy one built-in + add a custom plugin + add a broken plugin
        with open(os.path.join(custom_dir, "hello_ext.py"), "w", encoding="utf-8") as f:
            f.write(
                """
from bot_plugins import BotPlugin

class HelloExtPlugin(BotPlugin):
    id = "hello_ext"
    name = "测试自定义插件"
    category = "custom"
    default_enabled = True
    token_impact = {"level": "low", "per_turn_tokens": 50, "per_session_tokens": 20, "background_llm": False, "extra_turns": False, "summary": "测试"}
    settings_schema = ({"key": "tag", "label": "标签", "type": "str", "default": "HELLO"},)

    def get_system_prompt_fragment(self, settings):
        return "自定义系统规则：" + str(settings.get("tag", ""))

    def on_prompt_sections(self, ctx, settings):
        return ["[EXT:" + str(settings.get("tag", "")) + "]"]

    def on_response_postprocess(self, text, ctx, settings):
        return text + "!"

    def on_ws_inbound(self, event, ctx, settings):
        if event.get("raw_message") == "DROP_ME":
            return {"drop": True}
        return None

    def on_ws_outbound(self, payload, ctx, settings):
        if payload.get("params", {}).get("message") == "BLOCK_OUT":
            return {"drop": True}
        return None
"""
            )
        with open(os.path.join(custom_dir, "broken_ext.py"), "w", encoding="utf-8") as f:
            f.write("raise RuntimeError('intentional broken plugin')\n")
        with open(os.path.join(custom_dir, "crash_hook.py"), "w", encoding="utf-8") as f:
            f.write(
                """
from bot_plugins import BotPlugin

class CrashHookPlugin(BotPlugin):
    id = "crash_hook"
    name = "异常钩子插件"
    default_enabled = True
    def on_prompt_sections(self, ctx, settings):
        raise ValueError("boom")
    def on_response_postprocess(self, text, ctx, settings):
        raise ValueError("boom2")
"""
            )

        os.environ["BOT_PLUGINS_DIR"] = custom_dir
        discovered = bot_plugins.discover_plugins(force=True)
        self.assertIn("hello_ext", discovered)
        self.assertIn("crash_hook", discovered)
        errs = bot_plugins.get_load_errors()
        self.assertEqual(len(errs), 1)
        self.assertEqual(errs[0]["file"], "broken_ext.py")

        # Hooks run when enabled (and crash_hook error is isolated!)
        sections = bot_plugins.run_prompt_hooks({"user_text": "hi"})
        self.assertEqual(sections, ["[EXT:HELLO]"])
        self.assertEqual(bot_plugins.run_response_hooks("ok", {}), "ok!")
        _, dropped_in = bot_plugins.run_ws_inbound_hooks({"raw_message": "DROP_ME"}, {})
        self.assertTrue(dropped_in)
        _, dropped_out = bot_plugins.run_ws_outbound_hooks({"params": {"message": "BLOCK_OUT"}}, {})
        self.assertTrue(dropped_out)

        # Sandbox GEMINI.md includes fragment ONLY while enabled
        compiled = bot_plugins.render_sandbox_gemini_md("# Core\n")
        self.assertIn("自定义系统规则：HELLO", compiled)

        # Turn OFF custom plugin -> zero hooks, zero system fragment!
        bot_plugins.update_plugin("hello_ext", enabled=False)
        self.assertEqual(bot_plugins.run_prompt_hooks({"user_text": "hi"}), [])
        self.assertEqual(bot_plugins.run_response_hooks("ok", {}), "ok")
        _, dropped_in2 = bot_plugins.run_ws_inbound_hooks({"raw_message": "DROP_ME"}, {})
        self.assertFalse(dropped_in2)
        compiled_off = bot_plugins.render_sandbox_gemini_md("# Core\n")
        self.assertNotIn("自定义系统规则", compiled_off)


class TestSandboxGeminiAndTokenBudget(_PluginTestBase):
    def test_sandbox_gemini_parity_and_ultra_lean_trimming(self):
        gemini_path = os.path.join(PROJECT_DIR, "workspace", "GEMINI.md")
        with open(gemini_path, "r", encoding="utf-8") as f:
            raw = f.read()

        # Default config -> exact byte-for-byte parity
        self.assertEqual(
            bot_plugins.render_sandbox_gemini_md(raw, bot_plugins.default_config()),
            raw,
        )

        # Ultra-lean -> strips optional feature instructions & examples, preserves core safety
        bot_plugins.apply_preset("ultra_lean")
        trimmed = bot_plugins.render_sandbox_gemini_md(raw)
        self.assertLess(len(trimmed), len(raw) - 1800)
        # Core security & multi-speaker lines MUST remain intact!
        self.assertIn("**群聊和普通用户私聊**", trimmed)
        self.assertIn("不读写本地文件、不执行命令", trimmed)
        self.assertIn("结合 `[群聊近期消息记录", trimmed)
        # Disabled plugin sections MUST be removed!
        self.assertNotIn("FOLLOW_REPEAT", trimmed)
        self.assertNotIn("[[QQ:history", trimmed)
        self.assertNotIn("## 八、示例", trimmed)
        self.assertNotIn("**QQ 动作权限", trimmed)

        # Token budget reflects substantial savings
        budget = bot_plugins.compute_token_budget(workspace_gemini_text=raw)
        self.assertGreater(budget["per_turn_saved_tokens"], 2000)
        self.assertEqual(budget["per_turn_save_pct"], 100)
        self.assertGreater(budget["sandbox_gemini_saved_tokens"], 1200)
        self.assertEqual(budget["background_llm_active_count"], 0)
        self.assertEqual(budget["extra_turns_active_count"], 0)


class TestRuntimeTokenGating(_PluginTestBase):
    def test_permissions_and_qq_actions_zero_token_when_disabled(self):
        # Enabled: prompt block is non-empty and msg IDs enabled
        prompt_on = qq_actions.build_action_prompt_block(actor_role="member", group_id="1001", is_dm=False)
        self.assertGreater(len(prompt_on), 400)
        self.assertTrue(qq_actions.should_include_message_ids(actor_role="member", group_id="1001", is_dm=False))

        # Setting include_denied_list=False trims denied section while plugin ON
        bot_plugins.update_plugin("qq_actions", enabled=True, settings={"include_denied_list": False})
        prompt_no_denied = qq_actions.build_action_prompt_block(actor_role="member", group_id="1001", is_dm=False)
        self.assertNotIn("本轮已禁止的 QQ 动作", prompt_no_denied)
        self.assertLess(len(prompt_no_denied), len(prompt_on))

        # Disable qq_actions -> 0 chars prompt, permissions return plugin_disabled, directives stripped
        bot_plugins.update_plugin("qq_actions", enabled=False)
        self.assertEqual(
            qq_actions.build_action_prompt_block(actor_role="admin", group_id="1001", is_dm=False),
            "",
        )
        res = bot_permissions.check_permission("poke", actor_role="admin", group_id="1001", is_dm=False)
        self.assertFalse(res["allowed"])
        self.assertEqual(res["source"], "plugin_disabled")
        self.assertEqual(res["plugin_id"], "qq_actions")

        out = qq_actions.process_model_output(
            "你好 [[QQ:poke qq=12345]] [CQ:at,qq=12345] 世界",
            actor_role="admin",
            is_group=True,
            group_id="1001",
            dm_user_id="",
        )
        self.assertNotIn("[[QQ:", out["final_text"])
        self.assertNotIn("[CQ:", out["final_text"])
        self.assertEqual(out["executed"], [])
        self.assertFalse(out["any_action_executed"])

        # Also disable chat_history_lookup -> should_include_message_ids becomes False!
        bot_plugins.update_plugin("chat_history_lookup", enabled=False)
        self.assertFalse(
            qq_actions.should_include_message_ids(actor_role="admin", group_id="1001", is_dm=False)
        )
        self.assertEqual(chat_history.build_history_prompt_block(is_group=True), "")

    def test_memory_granular_plugin_gating(self):
        # Seed a memory message first while plugins are enabled
        user_memory.db.init_db(self.mem_db)
        r_on = user_memory.record_chat_message(
            chat_key="dm:90001",
            sender_uid="90001",
            sender_name="Tester",
            role="user",
            text="我喜欢喝冰美式咖啡，每天早上都喝",
            is_group=False,
            auto_consolidate=False,
            db_path=self.mem_db,
        )
        self.assertTrue(r_on.get("recorded"))

        # Turn off memory_extraction -> run_idle_flush(respect_plugin=True) skips cleanly
        bot_plugins.update_plugin("memory_extraction", enabled=False)
        flush_res = user_memory.run_idle_flush(db_path=self.mem_db, respect_plugin=True)
        self.assertTrue(flush_res.get("skipped"))
        self.assertEqual(flush_res.get("reason"), "plugin_disabled")

        # Turn off memory_recall & memory_continuity -> prompt blocks are completely empty
        bot_plugins.update_plugin("memory_recall", enabled=False)
        bot_plugins.update_plugin("memory_continuity", enabled=False)
        blocks = user_memory.build_prompt_memory_blocks(
            sender_id="90001",
            sender_name="Tester",
            query="咖啡",
            is_group=False,
            is_first_turn=True,
            db_path=self.mem_db,
        )
        self.assertEqual(blocks.get("continuity_block", ""), "")
        self.assertEqual(blocks.get("memory_block", ""), "")

        # With all 3 memory runtime plugins OFF -> record_chat_message short-circuits
        r_off = user_memory.record_chat_message(
            chat_key="dm:90001",
            sender_uid="90001",
            sender_name="Tester",
            role="user",
            text="不应写入",
            is_group=False,
            auto_consolidate=True,
            db_path=self.mem_db,
        )
        self.assertFalse(r_off.get("recorded"))
        self.assertEqual(r_off.get("reason"), "plugin_disabled")


class TestWebUIBackendAndPackaging(_PluginTestBase):
    def test_webui_store_and_cross_endpoint_fields(self):
        state = webui_stores.get_plugins_state(force=True)
        self.assertEqual(state["summary"]["total_count"], 15)
        self.assertEqual(len(state["presets"]), 4)
        self.assertEqual(state["active_preset"], "full_featured")

        # Permissions state exposes plugin_status
        perms = webui_stores.get_permissions_state()
        self.assertIn("plugin_status", perms)
        self.assertIn("poke", perms["plugin_status"])
        self.assertEqual(perms["plugin_status"]["poke"]["plugin_id"], "qq_actions")

        # Memories & persona & config expose plugin cross-fields
        mem = webui_stores.list_memories()
        self.assertIn("plugin_flags", mem)
        self.assertIn("memory_recall", mem["plugin_flags"])

        persona = webui_stores.get_persona()
        self.assertIn("sandbox_prompt", persona)
        self.assertGreater(persona["sandbox_prompt"]["workspace_tokens"], 1000)

        cfg = webui_stores.get_config()
        self.assertIn("plugin_flags", cfg)
        self.assertIn("repeat_chain", cfg["plugin_flags"])

        ov = webui_services.get_overview()
        self.assertIn("plugins_summary", ov)
        self.assertEqual(ov["plugins_summary"]["total_count"], 15)

    def test_packaging_allowlist_and_secret_audit(self):
        self.assertIn("bot_plugins.py", build_packages.CORE_PY_FILES)
        # Ensure zero forbidden personal strings in bot_plugins.py or plugins/*.py
        files_to_check = [os.path.join(PROJECT_DIR, "bot_plugins.py")]
        pdir = os.path.join(PROJECT_DIR, "plugins")
        for fn in os.listdir(pdir):
            if fn.endswith(".py") or fn.endswith(".md") or fn.endswith(".example"):
                files_to_check.append(os.path.join(pdir, fn))
        for fp in files_to_check:
            with open(fp, "r", encoding="utf-8") as f:
                content = f.read()
            for bad in build_packages.FORBIDDEN_STRINGS:
                self.assertNotIn(bad, content, "Forbidden string {!r} found in {}".format(bad, fp))


if __name__ == "__main__":
    unittest.main()
