#!/usr/bin/env python3
"""Comprehensive tests for Group Member Join Request engine, runtime integration, WebUI API/SSE, and audit fixes."""

import asyncio
import glob
import importlib.util
import json
import os
import re
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from unittest import mock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import bot_permissions  # noqa: E402
import bot_plugins  # noqa: E402
import build_packages  # noqa: E402
import group_requests  # noqa: E402
import onebot_common  # noqa: E402
import qq_actions  # noqa: E402
from webui import config_schema  # noqa: E402
from webui import server as webui_server  # noqa: E402
from webui import services as webui_services  # noqa: E402
from webui import stores as webui_stores  # noqa: E402


def _load_module_from_file(name, rel_path):
    full_path = os.path.join(PROJECT_ROOT, rel_path)
    spec = importlib.util.spec_from_file_location(name, full_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestGroupRequestEngine(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmpdir.name, "group_requests.db")
        self.policy_path = os.path.join(self.tmpdir.name, "group_requests_policy.json")

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_record_lookup_dedup_and_events(self):
        req1, is_new1 = group_requests.record_incoming_request(
            {
                "flag": "9876543210123",
                "sub_type": "add",
                "group_id": "100200",
                "user_id": "200300",
                "comment": "暗号：哈基米",
            },
            db_path=self.db_path,
        )
        self.assertTrue(is_new1)
        self.assertEqual(req1["status"], "pending")
        self.assertEqual(req1["flag"], "9876543210123")

        # Lookup by integer row ID and by numeric string flag both resolve cleanly
        self.assertEqual(group_requests.get_request(req1["id"], db_path=self.db_path)["flag"], "9876543210123")
        self.assertEqual(group_requests.get_request("9876543210123", db_path=self.db_path)["id"], req1["id"])

        # Re-inserting same flag enriches empty fields without duplicating or resetting status
        req2, is_new2 = group_requests.record_incoming_request(
            {
                "flag": "9876543210123",
                "sub_type": "add",
                "group_id": "100200",
                "group_name": "测试交流群",
                "user_id": "200300",
                "user_name": "小明",
                "qq_level": 42,
            },
            db_path=self.db_path,
        )
        self.assertFalse(is_new2)
        self.assertEqual(req2["group_name"], "测试交流群")
        self.assertEqual(req2["user_name"], "小明")
        self.assertEqual(req2["qq_level"], 42)

        events, latest_seq = group_requests.get_events_since(0, db_path=self.db_path)
        self.assertGreaterEqual(latest_seq, 1)
        self.assertTrue(any(ev["event_type"] == "new" for ev in events))

    def test_policy_evaluation_priority_and_group_overrides(self):
        policy = group_requests.default_policy()
        policy["enabled"] = True
        policy["global"].update(
            {
                "mode": "keyword",
                "approve_keywords": ["哈基米", "暗号"],
                "reject_keywords": ["广告", "刷单"],
                "reject_unmatched": False,
                "default_reject_reason": "暗号不对哦",
                "min_qq_level": 16,
                "handle_invite": "auto_reject",
                "auto_delay_secs": 2.5,
            }
        )
        policy["groups"]["100201"] = {
            "label": "开放群",
            "inherit": False,
            "mode": "auto_all",
            "reject_keywords": ["黑产"],
            "min_qq_level": 0,
            "welcome_message": "欢迎 {user}({qq})！",
        }
        saved = group_requests.save_policy(policy, path=self.policy_path, db_path=self.db_path)
        self.assertTrue(saved["enabled"])

        # 1) Reject keyword beats approve keyword
        r_rej = group_requests.evaluate_request_policy(
            {"group_id": "100200", "sub_type": "add", "comment": "哈基米 刷单兼职", "qq_level": 50},
            policy=saved,
        )
        self.assertEqual(r_rej["action"], "reject")
        self.assertEqual(r_rej["source"], "auto_reject_keyword")

        # 2) QQ level below threshold stays manual when reject_unmatched=False
        r_low = group_requests.evaluate_request_policy(
            {"group_id": "100200", "sub_type": "add", "comment": "哈基米", "qq_level": 5},
            policy=saved,
        )
        self.assertEqual(r_low["action"], "manual")

        # 3) Unknown QQ level stays manual when min_qq_level > 0
        r_none_lvl = group_requests.evaluate_request_policy(
            {"group_id": "100200", "sub_type": "add", "comment": "哈基米", "qq_level": None},
            policy=saved,
        )
        self.assertEqual(r_none_lvl["action"], "manual")

        # 4) Keyword + level satisfied -> auto approve
        r_ok = group_requests.evaluate_request_policy(
            {"group_id": "100200", "sub_type": "add", "comment": "我来找哈基米玩", "qq_level": 20},
            policy=saved,
        )
        self.assertEqual(r_ok["action"], "approve")
        self.assertEqual(r_ok["source"], "auto_keyword")
        self.assertEqual(r_ok["delay_secs"], 2.5)

        # 5) Invite follows handle_invite
        r_inv = group_requests.evaluate_request_policy(
            {"group_id": "100200", "sub_type": "invite", "comment": ""},
            policy=saved,
        )
        self.assertEqual(r_inv["action"], "reject")

        # 6) Group override ignores global keyword requirement
        r_grp = group_requests.evaluate_request_policy(
            {"group_id": "100201", "sub_type": "add", "comment": "路过看看"},
            policy=saved,
        )
        self.assertEqual(r_grp["action"], "approve")
        self.assertEqual(r_grp["source"], "auto_all")

        # 7) Master switch off forces manual
        saved["enabled"] = False
        r_off = group_requests.evaluate_request_policy(
            {"group_id": "100201", "sub_type": "add", "comment": "路过看看"},
            policy=saved,
        )
        self.assertEqual(r_off["action"], "manual")
        self.assertEqual(r_off["source"], "disabled")

    def test_execute_decision_fail_retry_welcome_and_external_increase(self):
        policy = group_requests.default_policy()
        policy["global"]["welcome_message"] = "欢迎 {user} ({qq}) 加入！"
        group_requests.save_policy(policy, path=self.policy_path, db_path=self.db_path)

        req, _ = group_requests.record_incoming_request(
            {
                "flag": "flag-retry-1",
                "sub_type": "add",
                "group_id": "100300",
                "user_id": "300400",
                "user_name": "阿橘",
            },
            db_path=self.db_path,
        )

        # First NapCat attempt fails -> status becomes 'failed' (retryable)
        def fail_call(ep, payload, timeout=5.0):
            return False, None, "napcat timeout"

        res_fail = group_requests.execute_decision(
            req["id"],
            approve=True,
            napcat_call=fail_call,
            db_path=self.db_path,
            policy_path=self.policy_path,
        )
        self.assertFalse(res_fail["ok"])
        self.assertEqual(res_fail["request"]["status"], "failed")

        # Retry succeeds and sends welcome message
        calls = []

        def ok_call(ep, payload, timeout=5.0):
            calls.append((ep, payload))
            return True, {"retcode": 0}, ""

        res_ok = group_requests.execute_decision(
            req["id"],
            approve=True,
            napcat_call=ok_call,
            db_path=self.db_path,
            policy_path=self.policy_path,
        )
        self.assertTrue(res_ok["ok"])
        self.assertTrue(res_ok["welcome_sent"])
        self.assertEqual(res_ok["request"]["status"], "approved")
        self.assertEqual(calls[0][0], "set_group_add_request")
        self.assertEqual(calls[1][0], "send_group_msg")
        self.assertIn("阿橘", str(calls[1][1]))

        # Second attempt is guarded by already_handled
        res_dup = group_requests.execute_decision(
            req["id"],
            approve=True,
            napcat_call=ok_call,
            db_path=self.db_path,
            policy_path=self.policy_path,
        )
        self.assertFalse(res_dup["ok"])
        self.assertTrue(res_dup["already_handled"])

        # External group_increase auto-resolves pending requests
        req_ext, _ = group_requests.record_incoming_request(
            {"flag": "flag-ext-2", "sub_type": "add", "group_id": "100300", "user_id": "300401"},
            db_path=self.db_path,
        )
        resolved = group_requests.mark_external_group_increase(
            "100300", "300401", operator_id="999001", db_path=self.db_path
        )
        self.assertEqual(resolved, 1)
        after = group_requests.get_request(req_ext["id"], db_path=self.db_path)
        self.assertEqual(after["status"], "approved")
        self.assertEqual(after["decision_source"], "external")

    def test_sync_from_napcat_and_ws_handler(self):
        def fake_napcat(ep, payload, timeout=5.0):
            if ep == "get_group_system_msg":
                return True, {
                    "join_requests": [
                        {
                            "request_id": "111222",
                            "group_id": 100500,
                            "group_name": "开发群",
                            "requester_uin": 500111,
                            "requester_nick": "新人A",
                            "message": "暗号哈基米",
                            "checked": False,
                        },
                        {
                            "request_id": "111223",
                            "group_id": 100500,
                            "group_name": "开发群",
                            "requester_uin": 500112,
                            "requester_nick": "新人B",
                            "message": "已处理项",
                            "checked": True,
                            "actor": 888001,
                        },
                    ],
                    "invited_requests": [
                        {"request_id": "inv-1", "group_id": 100501, "invitor_uin": 500200, "invitor_nick": "好友"}
                    ],
                    "InvitedRequest": [
                        {"request_id": "inv-1", "group_id": 100501, "invitor_uin": 500200, "invitor_nick": "好友"}
                    ],
                }, ""
            if ep == "get_group_ignore_add_request":
                return True, [
                    {
                        "request_id": "ign-1",
                        "group_id": 100500,
                        "requester_uin": 500999,
                        "requester_nick": "风险号",
                        "message": "进群",
                        "checked": False,
                    }
                ], ""
            if ep == "get_stranger_info":
                return True, {"nickname": "WS新人", "qqLevel": 30}, ""
            if ep == "set_group_add_request":
                return True, {}, ""
            if ep == "send_private_msg":
                return True, {}, ""
            return True, {}, ""

        sync_res = group_requests.sync_from_napcat(napcat_call=fake_napcat, db_path=self.db_path)
        self.assertTrue(sync_res["ok"])
        self.assertEqual(sync_res["synced_new"], 4)  # deduped inv-1 + 2 joins + 1 ignored
        ign = group_requests.get_request("ign-1", db_path=self.db_path)
        self.assertTrue(ign["is_ignored"])
        checked_item = group_requests.get_request("111223", db_path=self.db_path)
        self.assertEqual(checked_item["status"], "approved")

        # End-to-end WS auto-approval + admin DM notification
        policy = group_requests.default_policy()
        policy["enabled"] = True
        policy["global"]["mode"] = "keyword"
        policy["global"]["approve_keywords"] = ["哈基米"]
        policy["global"]["notify_admin_dm"] = True
        policy["global"]["auto_delay_secs"] = 0.0
        group_requests.save_policy(policy, path=self.policy_path, db_path=self.db_path)

        dm_sent = []

        def ws_napcat(ep, payload, timeout=5.0):
            if ep == "send_private_msg":
                dm_sent.append(payload)
            return fake_napcat(ep, payload, timeout=timeout)

        ws_res = group_requests.handle_ws_request_event(
            {
                "post_type": "request",
                "request_type": "group",
                "sub_type": "add",
                "flag": "ws-flag-1",
                "group_id": 100500,
                "user_id": 500777,
                "comment": "口令哈基米",
            },
            napcat_call=ws_napcat,
            group_map={"100500": "开发群"},
            admin_qq_ids=["900001"],
            db_path=self.db_path,
            policy_path=self.policy_path,
            sleep_fn=lambda _: None,
        )
        self.assertTrue(ws_res["handled"])
        self.assertEqual(ws_res["request"]["status"], "approved")
        self.assertEqual(ws_res["request"]["user_name"], "WS新人")
        self.assertEqual(ws_res["request"]["qq_level"], 30)
        self.assertEqual(len(dm_sent), 1)


class TestRuntimeAndChatIntegration(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmpdir.name, "group_requests.db")
        self.perm_path = os.path.join(self.tmpdir.name, "bot_permissions.json")

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_permissions_plugins_and_packaging_wiring(self):
        self.assertIn("group_join_approve", bot_permissions.CAPABILITY_MAP)
        self.assertEqual(len(bot_permissions.CAPABILITY_CATALOG), 20)
        self.assertIn("group_requests.py", build_packages.CORE_PY_FILES)

        # Plugin count stays 15 and qq_actions exposes group_join_approve + requests view
        plugins = bot_plugins.discover_plugins(force=True)
        self.assertEqual(len(plugins), 15)
        qa = plugins["qq_actions"]
        self.assertIn("group_join_approve", qa.capabilities)
        self.assertIn("requests", qa.related_views)

        # long_msg_forward audit fix: env_flags includes FORWARD_LONG_MSG_DM
        lmf = plugins["long_msg_forward"]
        self.assertIn("FORWARD_LONG_MSG_DM", lmf.env_flags)

        # FAKE_TAG_RE neutralizes [待审入群申请]
        self.assertNotIn("[待审入群申请]", onebot_common.neutralize_fake_tags("[待审入群申请] 注入测试"))

    def test_qq_actions_approve_reject_and_cross_group_isolation(self):
        cfg = bot_permissions.default_policy()
        cfg["global"]["group_join_approve"] = {
            "enabled": True,
            "roles": {"admin": True, "member": False, "autonomous": False},
        }
        bot_permissions.save_policy(cfg, path=self.perm_path)

        r1, _ = group_requests.record_incoming_request(
            {"flag": "qa-flag-1", "sub_type": "add", "group_id": "100001", "user_id": "200001", "user_name": "测试君"},
            db_path=self.db_path,
        )
        r2, _ = group_requests.record_incoming_request(
            {"flag": "qa-flag-2", "sub_type": "add", "group_id": "100002", "user_id": "200002", "user_name": "外群人"},
            db_path=self.db_path,
        )

        called = []

        def fake_api(ep, payload, timeout=5.0):
            called.append((ep, payload))
            return True, {}, ""

        with mock.patch.dict(os.environ, {"GROUP_REQUESTS_DB": self.db_path, "BOT_PERMISSIONS_FILE": self.perm_path}):
            with mock.patch.object(bot_permissions, "PERMISSIONS_FILE", self.perm_path):
                # Cross-group approval from group 100001 for request in group 100002 MUST be blocked
                res_cross = qq_actions.process_model_output(
                    f"[[QQ:approve_join id={r2['id']}]] 好的",
                    actor_role="admin",
                    is_group=True,
                    group_id="100001",
                    policy=cfg,
                    napcat_call=fake_api,
                )
                self.assertEqual(len(res_cross["executed"]), 0)
                self.assertGreaterEqual(len(res_cross["blocked"]), 1)

                # Non-admin DM MUST be blocked even if policy allows
                res_dm_non_admin = qq_actions.process_model_output(
                    f"[[QQ:approve_join id={r1['id']}]] 好的",
                    actor_role="member",
                    is_group=False,
                    group_id="",
                    dm_user_id="800001",
                    policy=cfg,
                    napcat_call=fake_api,
                )
                self.assertEqual(len(res_dm_non_admin["executed"]), 0)
                self.assertGreaterEqual(len(res_dm_non_admin["blocked"]), 1)

                # Admin in group 100001 approves r1 by id
                res_ok = qq_actions.process_model_output(
                    f"[[QQ:approve_join id={r1['id']}]] 已同意啦",
                    actor_role="admin",
                    is_group=True,
                    group_id="100001",
                    policy=cfg,
                    napcat_call=fake_api,
                )
                self.assertEqual(len(res_ok["executed"]), 1)
                self.assertEqual(group_requests.get_request(r1["id"], db_path=self.db_path)["status"], "approved")

                # Admin in DM rejects r2 by qq
                res_dm_rej = qq_actions.process_model_output(
                    '[[QQ:reject_join qq=200002 reason="资料不符"]] 已拒绝',
                    actor_role="admin",
                    is_group=False,
                    group_id="",
                    dm_user_id="900001",
                    policy=cfg,
                    napcat_call=fake_api,
                )
                self.assertEqual(len(res_dm_rej["executed"]), 1)
                after_r2 = group_requests.get_request(r2["id"], db_path=self.db_path)
                self.assertEqual(after_r2["status"], "rejected")
                self.assertEqual(after_r2["reject_reason"], "资料不符")


class TestWebUIGroupRequestsAndAuditFixes(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmpdir.name, "group_requests.db")
        self.policy_path = os.path.join(self.tmpdir.name, "group_requests_policy.json")
        self.napcat_calls = []

        def fake_napcat_post(endpoint, payload, timeout=5.0):
            self.napcat_calls.append((endpoint, payload))
            if endpoint == "get_group_system_msg":
                return True, {
                    "join_requests": [
                        {
                            "request_id": "srv-flag-1",
                            "group_id": 666001,
                            "group_name": "盯盘测试群",
                            "requester_uin": 777001,
                            "requester_nick": "小黑",
                            "message": "申请入群测试",
                            "checked": False,
                        }
                    ]
                }, ""
            if endpoint == "get_group_ignore_add_request":
                return True, [], ""
            if endpoint in ("set_group_add_request", "send_group_msg"):
                return True, {"retcode": 0}, ""
            return True, {}, ""

        self._env_patch = mock.patch.dict(
            os.environ,
            {
                "GROUP_REQUESTS_DB": self.db_path,
                "GROUP_REQUESTS_POLICY_FILE": self.policy_path,
            },
        )
        self._env_patch.start()
        self._patches = [
            mock.patch.object(group_requests, "DEFAULT_DB_PATH", self.db_path),
            mock.patch.object(group_requests, "DEFAULT_POLICY_FILE", self.policy_path),
            mock.patch.object(webui_services, "_napcat_call_for_requests", side_effect=fake_napcat_post),
            mock.patch.object(
                webui_services,
                "napcat_get_group_list",
                return_value={
                    "ok": True,
                    "groups": [{"group_id": "666001", "group_name": "盯盘测试群", "member_count": 42}],
                    "group_map": {"666001": "盯盘测试群"},
                },
            ),
            mock.patch.object(
                webui_services,
                "napcat_get_bot_group_roles",
                return_value={"666001": "admin"},
            ),
        ]
        for p in self._patches:
            p.start()

        self.srv = webui_server.HajimiHTTPServer(("127.0.0.1", 0), webui_server.HajimiRequestHandler)
        self.port = self.srv.server_address[1]
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        webui_server._SHUTDOWN_EVENT.set()
        try:
            self.srv.shutdown()
            self.srv.server_close()
        finally:
            webui_server._SHUTDOWN_EVENT.clear()
        for p in reversed(self._patches):
            p.stop()
        self._env_patch.stop()
        self.tmpdir.cleanup()

    def _req(self, method, path, payload=None):
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=data,
            headers=headers,
            method=method,
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            raw = r.read().decode("utf-8")
            return r.status, dict(r.headers.items()), raw

    def test_join_monitor_static_page_and_rest_api_flow(self):
        # 1) Standalone monitor page serves HTML with CSP & zero inline script/event handlers
        status, headers, html = self._req("GET", "/join-monitor")
        self.assertEqual(status, 200)
        self.assertIn("join-monitor.js", html)
        self.assertIn("Content-Security-Policy", headers)
        self.assertNotRegex(html, r"<script(?![^>]*\bsrc\s*=)[^>]*>[\s\S]*?\S")
        self.assertNotRegex(html, r"\bon[a-z]+\s*=\s*[\"']")

        # 2) Sync + list
        status, _, raw = self._req("POST", "/api/group-requests/sync", {})
        self.assertEqual(status, 200)
        sync_body = json.loads(raw)["data"]
        self.assertEqual(sync_body["sync"]["synced_new"], 1)

        status, _, raw = self._req("GET", "/api/group-requests?status=pending")
        self.assertEqual(status, 200)
        list_data = json.loads(raw)["data"]
        self.assertEqual(list_data["counts"]["pending"], 1)
        self.assertEqual(list_data["items"][0]["bot_role"], "admin")
        req_id = list_data["items"][0]["id"]

        # 3) Overview exposes group_requests_summary
        status, _, raw = self._req("GET", "/api/overview?refresh=1")
        self.assertEqual(status, 200)
        ov = json.loads(raw)["data"]
        self.assertEqual(ov["group_requests_summary"]["pending_count"], 1)

        # 4) Policy PUT + read-only test endpoint (including draft policy simulation)
        pol = group_requests.default_policy()
        pol["enabled"] = True
        pol["global"]["mode"] = "keyword"
        pol["global"]["approve_keywords"] = ["测试暗号"]
        status, _, raw = self._req("PUT", "/api/group-requests/policy", pol)
        self.assertEqual(status, 200)

        status, _, raw = self._req(
            "POST",
            "/api/group-requests/policy/test",
            {"group_id": "666001", "comment": "我是测试暗号", "sub_type": "add"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["data"]["action"], "approve")

        # 5) Approve + Clear
        status, _, raw = self._req("POST", f"/api/group-requests/{req_id}/approve", {})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["data"]["request"]["status"], "approved")

        status, _, raw = self._req("POST", "/api/group-requests/clear", {"before_days": 0})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["data"]["deleted"], 1)

    def test_sse_stream_snapshot_and_live_event(self):
        received = []
        ready = threading.Event()

        def reader():
            req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/group-requests/stream?since=0")
            try:
                with urllib.request.urlopen(req, timeout=5) as resp:
                    cur_event = ""
                    for raw_line in resp:
                        line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                        if line.startswith("event:"):
                            cur_event = line.split(":", 1)[1].strip()
                        elif line.startswith("data:"):
                            data = json.loads(line.split(":", 1)[1].strip())
                            received.append((cur_event, data))
                            if cur_event == "snapshot":
                                ready.set()
                            if cur_event == "request_event":
                                break
            except Exception:
                pass

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        self.assertTrue(ready.wait(timeout=3.0), "SSE snapshot event not received")

        group_requests.record_incoming_request(
            {"flag": "sse-live-1", "sub_type": "add", "group_id": "666001", "user_id": "777888", "comment": "实时盯盘"},
            db_path=self.db_path,
        )
        t.join(timeout=3.5)
        events = [e[0] for e in received]
        self.assertIn("snapshot", events)
        self.assertIn("request_event", events)

    def test_audit_fixes_backend_and_static_integrity(self):
        # 1) auto_compress hard_max_tokens & hard_min_gap_mins preserved + min_gap_mins default=15
        toml_path = os.path.join(self.tmpdir.name, "config.toml")
        with open(toml_path, "w", encoding="utf-8") as f:
            f.write(
                """
[display]
mode = "compact"

[[projects]]
name = "qq-gemini-acp"
[projects.auto_compress]
enabled = true
max_tokens = 120000
hard_max_tokens = 200000
hard_min_gap_mins = 5
[[projects.platforms]]
type = "qq"
[projects.platforms.options]
ws_url = "ws://127.0.0.1:3002"
http_url = "http://127.0.0.1:3000"
"""
            )
        wb = webui_stores.read_workbench_toml(toml_path)
        self.assertEqual(wb["auto_compress_hard_max_tokens"], 200000)
        self.assertEqual(wb["auto_compress_hard_min_gap_mins"], 5)
        self.assertEqual(wb["auto_compress_min_gap_mins"], 15)

        # 2) GUARD_UDP_PORT and GUARD_LISTEN_PORT require full restart
        self.assertEqual(webui_stores._compute_restart_scope(["GUARD_UDP_PORT"]), "full")
        self.assertEqual(webui_stores._compute_restart_scope(["GUARD_LISTEN_PORT"]), "full")
        self.assertEqual(webui_stores._compute_restart_scope(["GUARD_BUSY_TTL_SECS"]), "ws-guard")
        schema_by_key = {
            item["key"]: item
            for sec in config_schema.CONFIG_SCHEMA
            for item in sec.get("fields", [])
        }
        self.assertEqual(schema_by_key["GUARD_UDP_PORT"]["restart"], "full")
        self.assertEqual(schema_by_key["GUARD_LISTEN_PORT"]["restart"], "full")

        # 3) Static frontend CSP & NapCat tab regression check
        js_files = glob.glob(os.path.join(PROJECT_ROOT, "webui", "static", "**", "*.js"), recursive=True)
        for fp in js_files:
            with open(fp, encoding="utf-8") as f:
                src = f.read()
            self.assertNotRegex(
                src,
                r"\bon[a-z]+\s*=\s*[\"']",
                f"Inline event handler violates CSP in {fp}",
            )

        with open(os.path.join(PROJECT_ROOT, "webui", "static", "views", "napcat.js"), encoding="utf-8") as f:
            napcat_js = f.read()
        self.assertNotIn("tabsEl.querySelectorAll('.tab-btn')", napcat_js)
        self.assertIn("tabsEl.querySelectorAll('.page-tab')", napcat_js)

        with open(os.path.join(PROJECT_ROOT, "webui", "static", "app.js"), encoding="utf-8") as f:
            app_js = f.read()
        self.assertIn("id: 'requests'", app_js)


if __name__ == "__main__":
    unittest.main()
