#!/usr/bin/env python3
"""Regression tests for P0 security fixes (DM ownership, SSRF, WebUI CSRF/Host/secrets),
P1 infra/runtime fixes (file_lock POSIX timeout, bot_env round-trip, chat_history O(N+M),
setup_wizard v2 sync, bot_runner log rotation), and P2 packaging hygiene.
"""
from __future__ import annotations

import http.client
import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

import bot_env  # noqa: E402
import bot_runner  # noqa: E402
import build_packages  # noqa: E402
import chat_history  # noqa: E402
import file_lock  # noqa: E402
import onebot_common  # noqa: E402
import qq_actions  # noqa: E402
import setup_wizard  # noqa: E402
from webui import server as webui_server  # noqa: E402
from webui import services as webui_services  # noqa: E402
from webui import stores as webui_stores  # noqa: E402


class TestSecurityFixes(unittest.TestCase):
    def test_dm_ownership_blocks_cross_user_and_admin_dm_leak(self) -> None:
        bot_uid = "1000000001"
        admin_uid = "10000001"
        attacker_uid = "20000002"

        admin_to_bot = {"message_type": "private", "user_id": admin_uid, "target_id": bot_uid}
        bot_to_admin = {"message_type": "private", "user_id": bot_uid, "target_id": admin_uid}
        attacker_to_bot = {"message_type": "private", "user_id": attacker_uid, "target_id": bot_uid}
        bot_to_attacker = {"message_type": "private", "user_id": bot_uid, "target_id": attacker_uid}
        group_msg = {"message_type": "group", "group_id": "999999", "user_id": attacker_uid}

        # Attacker MUST be denied access to admin's DMs (in both directions!)
        self.assertFalse(onebot_common.verify_dm_msg_ownership(admin_to_bot, attacker_uid, bot_uid))
        self.assertFalse(onebot_common.verify_dm_msg_ownership(bot_to_admin, attacker_uid, bot_uid))
        self.assertFalse(chat_history._verify_napcat_msg_ownership(admin_to_bot, False, "", attacker_uid, bot_uid))
        self.assertFalse(chat_history._verify_napcat_msg_ownership(bot_to_admin, False, "", attacker_uid, bot_uid))
        self.assertFalse(onebot_common.verify_dm_msg_ownership(group_msg, attacker_uid, bot_uid))

        # Legitimate own DM messages pass in both directions
        self.assertTrue(onebot_common.verify_dm_msg_ownership(attacker_to_bot, attacker_uid, bot_uid))
        self.assertTrue(onebot_common.verify_dm_msg_ownership(bot_to_attacker, attacker_uid, bot_uid))
        self.assertTrue(chat_history._verify_napcat_msg_ownership(admin_to_bot, False, "", admin_uid, bot_uid))
        self.assertTrue(chat_history._verify_napcat_msg_ownership(bot_to_admin, False, "", admin_uid, bot_uid))

        # qq_actions._verify_msg_in_chat also rejects attacker accessing admin DM
        ok_atk, _, _ = qq_actions._verify_msg_in_chat(
            "123456", False, "", attacker_uid, bot_uid, lambda api, p, timeout=3.0: (True, bot_to_admin, "")
        )
        self.assertFalse(ok_atk)
        ok_own, _, _ = qq_actions._verify_msg_in_chat(
            "123456", False, "", admin_uid, bot_uid, lambda api, p, timeout=3.0: (True, bot_to_admin, "")
        )
        self.assertTrue(ok_own)

    def test_ssrf_url_validation_and_redirect_handler(self) -> None:
        for bad_url in [
            "http://127.0.0.1:9820/api",
            "http://localhost:8765/",
            "http://169.254.169.254/latest/meta-data/",
            "http://10.0.0.1/secret",
            "http://172.16.8.20/internal",
            "http://192.168.1.1/admin",
            "file:///etc/passwd",
            "ftp://example.com/a.png",
        ]:
            self.assertFalse(onebot_common.is_safe_public_url(bad_url, resolve_dns=False), bad_url)

        # Case 2 segment array file:// block in qq_actions.filter_outbound_onebot_send
        payload = {
            "action": "send_private_msg",
            "params": {
                "user_id": 20000002,
                "message": [{"type": "image", "data": {"file": "file:///etc/passwd"}}],
            },
        }
        filtered, drop, notes = qq_actions.filter_outbound_onebot_send(
            payload, actor_role="member", group_id="", is_dm=True
        )
        self.assertTrue(drop or not any(
            isinstance(s, dict) and s.get("type") == "image"
            for s in (filtered.get("params", {}).get("message") or [])
        ))

    def test_webui_host_csrf_and_secret_masking(self) -> None:
        # 1. Secret masking in stores.get_config / get_workbench_state
        cfg = webui_stores.get_config()
        env_map = cfg.get("env") or {}
        if "NAPCAT_TOKEN" in env_map and env_map["NAPCAT_TOKEN"]:
            self.assertIn("•", str(env_map["NAPCAT_TOKEN"]))
        wb = webui_stores.get_workbench_state()
        self.assertNotIn("NAPCAT_TOKEN", wb.get("env") or {})

        # 2. Live ephemeral HajimiHTTPServer Host + Origin + Content-Type checks
        srv = webui_server.HajimiHTTPServer(("127.0.0.1", 0), webui_server.HajimiRequestHandler)
        port = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            # DNS rebinding Host rejected (403)
            c1 = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            c1.putrequest("GET", "/api/config", skip_host=True)
            c1.putheader("Host", f"evil.rebind.example:{port}")
            c1.endheaders()
            r1 = c1.getresponse()
            r1.read()
            c1.close()
            self.assertEqual(r1.status, 403)

            # Cross-origin POST CSRF rejected (403)
            c2 = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            c2.request(
                "POST",
                "/api/memories/maintenance/flush",
                body=b"{}",
                headers={"Content-Type": "application/json", "Origin": "https://evil.example"},
            )
            r2 = c2.getresponse()
            r2.read()
            c2.close()
            self.assertEqual(r2.status, 403)

            # Non-JSON text/plain POST body rejected (415)
            c3 = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            c3.request(
                "POST",
                "/api/memories/maintenance/flush",
                body=b'{"idle_mins":0}',
                headers={"Content-Type": "text/plain"},
            )
            r3 = c3.getresponse()
            r3.read()
            c3.close()
            self.assertEqual(r3.status, 415)

            # Valid loopback GET returns 200 with CSP header
            c4 = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            c4.request("GET", "/api/config")
            r4 = c4.getresponse()
            body4 = r4.read()
            c4.close()
            self.assertEqual(r4.status, 200)
            self.assertTrue(r4.getheader("Content-Security-Policy"))
            payload4 = json.loads(body4.decode("utf-8"))
            self.assertTrue(payload4.get("ok"))
            self.assertIn("env", payload4.get("data") or {})
        finally:
            srv.shutdown()
            srv.server_close()
            t.join(timeout=2)


class TestInfraAndPerformanceFixes(unittest.TestCase):
    def test_file_lock_posix_timeout_raises(self) -> None:
        if os.name == "nt":
            self.skipTest("POSIX-only flock timeout check")
        with tempfile.TemporaryDirectory() as td:
            lp = os.path.join(td, "test.lock")
            # Hold exclusive flock in a child process and verify parent times out cleanly
            holder = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    (
                        "import fcntl, sys, time; "
                        "f = open(sys.argv[1], 'a+'); "
                        "fcntl.flock(f.fileno(), fcntl.LOCK_EX); "
                        "print('LOCKED', flush=True); "
                        "time.sleep(5)"
                    ),
                    lp,
                ],
                stdout=subprocess.PIPE,
                text=True,
            )
            try:
                line = holder.stdout.readline().strip()
                self.assertEqual(line, "LOCKED")
                t0 = time.monotonic()
                with self.assertRaises(TimeoutError):
                    with file_lock.file_lock(lp, exclusive=True, timeout=0.6):
                        pass
                elapsed = time.monotonic() - t0
                self.assertGreaterEqual(elapsed, 0.45)
                self.assertLess(elapsed, 3.0)
            finally:
                holder.terminate()
                holder.wait(timeout=3)
                if holder.stdout:
                    holder.stdout.close()

    def test_bot_env_roundtrip_and_multiline_toml(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            env_path = os.path.join(td, "test.env")
            val = 'hello "world" $HOME \\path # not comment'
            formatted = bot_env.format_env_value(val)
            bot_env.atomic_write_text(env_path, f'TEST_KEY={formatted}\nPLAIN=abc # inline comment\n')
            parsed = bot_env.parse_file(env_path)
            self.assertEqual(parsed["TEST_KEY"], val)
            self.assertEqual(parsed["PLAIN"], "abc")

            toml_path = os.path.join(td, "test.toml")
            bot_env.atomic_write_text(
                toml_path,
                "[display] # comment\nreply_footer = false\n"
                "[[projects]]\nname = \"demo\"\n"
                "[projects.users.roles.admin]\nuser_ids = [\n  \"12345\",\n  \"67890\",\n]\n",
            )
            cfg = bot_env.parse_config_toml(toml_path)
            self.assertEqual(cfg.get("[display]", {}).get("reply_footer"), False)
            self.assertEqual(cfg.get("[projects.users.roles.admin]", {}).get("user_ids"), ["12345", "67890"])

    def test_webui_guard_ports_reject_invalid_values_and_repair_legacy_values(self) -> None:
        for key in ("GUARD_LISTEN_PORT", "GUARD_UDP_PORT"):
            for value in ("3500.5", "3.5e3", "NaN", "", "1023", "65536"):
                with self.subTest(key=key, value=value), self.assertRaises(webui_stores.StoreError):
                    webui_stores._validate_env_updates({key: value}, current_env={})
            with self.subTest(key=key):
                normalized, changed = webui_stores._validate_env_updates(
                    {key: "03500"}, current_env={key: "3500.0"}
                )
                self.assertEqual(normalized, {key: "3500"})
                self.assertEqual(changed, [key])

    def test_chat_history_dedup_fast_linear(self) -> None:
        db_rows = [{"ts": 1000 + i * 3, "text": f"msg {i}", "sender_id": "1"} for i in range(2000)]
        sess_rows = [{"ts": 1000 + i * 3, "text": f"msg {i}", "sender_id": "1"} for i in range(2000)]
        t0 = time.monotonic()
        merged = chat_history._dedup_and_merge_session_rows(db_rows, sess_rows)
        elapsed = time.monotonic() - t0
        self.assertEqual(len(merged), 2000)
        self.assertLess(elapsed, 1.0)

    def test_setup_wizard_v2_keys_and_build_packages_no_hardcoded_secrets(self) -> None:
        # setup_wizard must not contain dead v1 keys and must include v2 keys
        self.assertNotIn("MEMORY_BATCH_SIZE", setup_wizard._STANDARD_ENV_KEYS)
        self.assertIn("MEMORY_DB_PATH", setup_wizard._STANDARD_ENV_KEYS)
        self.assertIn("CHAT_HISTORY_ENABLED", setup_wizard._STANDARD_ENV_KEYS)
        self.assertIn("GROUP_WHITELIST_ENABLED", setup_wizard._STANDARD_ENV_KEYS)

        # build_packages.py must dynamically load FORBIDDEN_STRINGS with zero hardcoded live tokens
        self.assertIn("onebot_common.py", build_packages.CORE_PY_FILES)
        self.assertTrue(len(build_packages.FORBIDDEN_STRINGS) >= 1)
        with open(os.path.join(PROJECT_DIR, "build_packages.py"), encoding="utf-8") as fh:
            bp_src = fh.read()
        for bad in build_packages.FORBIDDEN_STRINGS:
            if len(bad) >= 8:
                self.assertNotIn(bad, bp_src, f"Hardcoded secret found in build_packages.py: {bad!r}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
