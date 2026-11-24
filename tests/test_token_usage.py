#!/usr/bin/env python3
"""Unit & integration tests for token_usage.py, memory_engine usage capture, and WebUI /api/tokens."""

import datetime
import json
import os
import sys
import tempfile
import time
import unittest
import urllib.parse
import urllib.request
from unittest import mock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import token_usage  # noqa: E402
from memory_engine import consolidate  # noqa: E402
from webui import server as webui_server  # noqa: E402
from webui import services as webui_services  # noqa: E402


class TestTokenUsageEngine(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmpdir.name, "token_usage.db")
        self.cc_log = os.path.join(self.tmpdir.name, "cc-connect.log")
        self.sessions_dir = os.path.join(self.tmpdir.name, "sessions")
        os.makedirs(self.sessions_dir, exist_ok=True)
        token_usage._INITIALIZED_DBS.discard(os.path.abspath(self.db_path))
        token_usage._LAST_AUTO_SYNC_BY_DB.pop(os.path.abspath(self.db_path), None)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_normalize_identity_and_extract_usage(self):
        self.assertEqual(
            token_usage.normalize_chat_identity(chat_key="g:100200"),
            ("qq:g:100200", "group", "100200"),
        )
        self.assertEqual(
            token_usage.normalize_chat_identity(session_key="qq:g:100200"),
            ("qq:g:100200", "group", "100200"),
        )
        self.assertEqual(
            token_usage.normalize_chat_identity(chat_key="dm:556677"),
            ("qq:556677", "dm", ""),
        )
        self.assertEqual(
            token_usage.normalize_chat_identity(source="memory_sleep"),
            ("system:memory_sleep", "system", ""),
        )

        # ACP camelCase
        acp_u = token_usage.extract_usage_fields(
            {
                "inputTokens": 2183,
                "cachedReadTokens": 10384,
                "cachedWriteTokens": 0,
                "outputTokens": 12,
                "thoughtTokens": 10,
                "totalTokens": 12579,
            }
        )
        self.assertEqual(acp_u["input_tokens"], 2183)
        self.assertEqual(acp_u["cached_read_tokens"], 10384)
        self.assertEqual(acp_u["output_tokens"], 12)
        self.assertEqual(acp_u["thought_tokens"], 10)
        self.assertEqual(acp_u["total_tokens"], 12579)

        # stream-json snake_case
        sj_u = token_usage.extract_usage_fields(
            {
                "input_tokens": 9921,
                "cache_read_tokens": 2000,
                "output_tokens": 22,
                "thinking_tokens": 20,
                "total_tokens": 11943,
            }
        )
        self.assertEqual(sj_u["input_tokens"], 9921)
        self.assertEqual(sj_u["cached_read_tokens"], 2000)
        self.assertEqual(sj_u["thought_tokens"], 20)
        self.assertEqual(sj_u["total_tokens"], 11943)

    def test_record_and_group_aggregation_and_detail(self):
        now = time.time()
        # Group A: 1 replied turn + 1 silent ambient turn + 1 background memory extract (using g:111111)
        ok1 = token_usage.record_usage(
            usage={
                "inputTokens": 2000,
                "cachedReadTokens": 18000,
                "outputTokens": 120,
                "thoughtTokens": 80,
                "totalTokens": 20120,
            },
            session_key="qq:g:111111",
            is_group=True,
            group_id="111111",
            sender_id="9001",
            sender_name="Alice",
            agent_session_id="asid-1",
            source="chat",
            model="gemini-4-neon",
            thought_level="medium",
            context_used=20120,
            is_silent=False,
            duration_ms=4200,
            response_len=65,
            ts=now - 120,
            db_path=self.db_path,
        )
        self.assertTrue(ok1)

        ok2 = token_usage.record_usage(
            usage={
                "inputTokens": 1500,
                "cachedReadTokens": 19500,
                "outputTokens": 15,
                "thoughtTokens": 12,
                "totalTokens": 21015,
            },
            session_key="qq:g:111111",
            is_group=True,
            group_id="111111",
            sender_id="9002",
            sender_name="Bob",
            agent_session_id="asid-1",
            source="chat",
            model="gemini-4-neon",
            thought_level="low",
            context_used=21015,
            is_silent=True,
            is_ambient=True,
            duration_ms=2600,
            ts=now - 60,
            db_path=self.db_path,
        )
        self.assertTrue(ok2)

        ok3 = token_usage.record_usage(
            usage={
                "input_tokens": 5000,
                "cache_read_tokens": 0,
                "output_tokens": 300,
                "thinking_tokens": 100,
                "total_tokens": 5300,
            },
            chat_key="g:111111",
            is_group=True,
            group_id="111111",
            source="memory_extract",
            model="gemini-4-neon",
            thought_level="medium",
            duration_ms=3100,
            ts=now - 30,
            db_path=self.db_path,
        )
        self.assertTrue(ok3)

        # Group B: smaller spend
        token_usage.record_usage(
            usage={
                "inputTokens": 4000,
                "cachedReadTokens": 6000,
                "outputTokens": 100,
                "thoughtTokens": 50,
                "totalTokens": 10100,
            },
            session_key="qq:g:222222",
            is_group=True,
            group_id="222222",
            sender_id="9003",
            sender_name="Carol",
            source="chat",
            model="gemini-4-neon",
            thought_level="medium",
            context_used=10100,
            is_silent=False,
            ts=now - 90,
            db_path=self.db_path,
        )

        # DM spend
        token_usage.record_usage(
            usage={
                "inputTokens": 3000,
                "cachedReadTokens": 0,
                "outputTokens": 200,
                "thoughtTokens": 150,
                "totalTokens": 3200,
            },
            session_key="qq:88888",
            is_group=False,
            dm_user_id="88888",
            sender_id="88888",
            sender_name="Admin",
            source="chat",
            model="gemini-4-neon",
            thought_level="xhigh",
            context_used=3200,
            ts=now - 45,
            db_path=self.db_path,
        )

        gmap = {"111111": "水群一号", "222222": "测试二群"}
        dash = token_usage.get_usage_dashboard(
            range_key="24h",
            group_map=gmap,
            db_path=self.db_path,
            cc_log_path=self.cc_log,
            sessions_dir=self.sessions_dir,
            now_ts=now,
        )
        s = dash["summary"]
        expected_total = 20120 + 21015 + 5300 + 10100 + 3200
        self.assertEqual(s["total_tokens"], expected_total)
        self.assertEqual(s["total_calls"], 5)
        self.assertEqual(s["group_tokens"], 20120 + 21015 + 5300 + 10100)
        self.assertEqual(s["dm_tokens"], 3200)
        self.assertEqual(s["silent_tokens"], 21015)
        self.assertEqual(s["memory_tokens"], 5300)
        self.assertEqual(s["exact_calls"], 5)
        self.assertEqual(s["top_group"]["group_id"], "111111")
        self.assertEqual(s["top_group"]["name"], "水群一号")
        self.assertIsNotNone(s["cache_hit_rate"])

        # Group filter returns only groups, sorted by total_tokens desc
        dash_grp = token_usage.get_usage_dashboard(
            range_key="24h",
            chat_filter="group",
            group_map=gmap,
            db_path=self.db_path,
            cc_log_path=self.cc_log,
            sessions_dir=self.sessions_dir,
            now_ts=now,
        )
        self.assertEqual([c["group_id"] for c in dash_grp["chats"]], ["111111", "222222"])
        self.assertEqual(dash_grp["chats"][0]["total_tokens"], 20120 + 21015 + 5300)
        self.assertEqual(dash_grp["chats"][0]["memory_tokens"], 5300)
        self.assertEqual(dash_grp["chats"][0]["memory_calls"], 1)

        # Single-group detail drill-down
        det = token_usage.get_chat_detail(
            "qq:g:111111",
            range_key="24h",
            group_map=gmap,
            db_path=self.db_path,
            cc_log_path=self.cc_log,
            sessions_dir=self.sessions_dir,
            now_ts=now,
        )
        self.assertEqual(det["chat"]["name"], "水群一号")
        self.assertEqual(det["chat"]["calls"], 3)
        self.assertEqual(len(det["recent_events"]), 3)
        member_names = [m["sender_name"] for m in det["top_members"]]
        self.assertIn("Bob", member_names)
        self.assertIn("Alice", member_names)

    def test_cc_log_backfill_skips_fast_silent_and_deduplicates_exact(self):
        base_dt = datetime.datetime.now().astimezone().replace(microsecond=0)
        t1 = (base_dt - datetime.timedelta(minutes=5)).isoformat()
        t2 = (base_dt - datetime.timedelta(minutes=4)).isoformat()
        t3 = (base_dt - datetime.timedelta(minutes=3)).isoformat()
        t4_ts = (base_dt - datetime.timedelta(minutes=2)).timestamp()
        t4 = datetime.datetime.fromtimestamp(t4_ts).astimezone().isoformat()

        # Pre-record turn 4 as an exact live record from agy-acp-wrapper.py
        token_usage.record_usage(
            usage={"inputTokens": 2000, "cachedReadTokens": 48000, "outputTokens": 100, "totalTokens": 50100},
            session_key="qq:g:333333",
            is_group=True,
            group_id="333333",
            sender_name="LiveUser",
            agent_session_id="asid-live",
            source="chat",
            ts=t4_ts - 0.5,
            db_path=self.db_path,
        )

        # Write realistic cc-connect.log lines:
        # Turn 1: real reply (20,000 tok)
        # Turn 2: fast-silent wrapper short-circuit (0.06s, same 20,000 est -> MUST BE SKIPPED)
        # Turn 3: real silent ambient LLM turn (23,500 tok, 5.2s -> MUST BE IMPORTED)
        # Turn 4: already recorded exact by live wrapper (50,100 tok -> MUST BE DEDUPED)
        log_lines = [
            f'time={t1} level=INFO msg="message received" platform=qq msg_id=m1 session=qq:g:333333 user=UserA content_len=12',
            f'time={t1} level=INFO msg="auto-compress: decision" estimate=20000 estimate_source=exact usage_source="" heuristic_estimate=500 max_tokens=120000 triggered=false',
            f'time={t1} level=INFO msg="turn complete" session=s1 agent_session=asid-live msg_id=m1 tools=0 response_len=45 turn_duration=6.12s input_tokens=0 output_tokens=0 silent=false',
            f'time={t2} level=INFO msg="message received" platform=qq msg_id=m2 session=qq:g:333333 user=UserB content_len=3',
            f'time={t2} level=INFO msg="auto-compress: decision" estimate=20000 estimate_source=exact usage_source="" heuristic_estimate=502 max_tokens=120000 triggered=false',
            f'time={t2} level=INFO msg="turn complete" session=s1 agent_session=asid-live msg_id=m2 tools=0 response_len=8 turn_duration=62.5ms input_tokens=0 output_tokens=0 silent=true',
            f'time={t3} level=INFO msg="message received" platform=qq msg_id=m3 session=qq:g:333333 user=UserC content_len=20',
            f'time={t3} level=INFO msg="auto-compress: decision" estimate=23500 estimate_source=exact usage_source="" heuristic_estimate=520 max_tokens=120000 triggered=false',
            f'time={t3} level=INFO msg="turn complete" session=s1 agent_session=asid-live msg_id=m3 tools=0 response_len=8 turn_duration=5.21s input_tokens=0 output_tokens=0 silent=true',
            f'time={t4} level=INFO msg="message received" platform=qq msg_id=m4 session=qq:g:333333 user=LiveUser content_len=15',
            f'time={t4} level=INFO msg="auto-compress: decision" estimate=50100 estimate_source=exact usage_source="" heuristic_estimate=600 max_tokens=120000 triggered=false',
            f'time={t4} level=INFO msg="turn complete" session=s1 agent_session=asid-live msg_id=m4 tools=0 response_len=50 turn_duration=4.8s input_tokens=0 output_tokens=0 silent=false',
        ]
        with open(self.cc_log, "w", encoding="utf-8") as f:
            f.write("\n".join(log_lines) + "\n")

        res = token_usage.sync_from_cc_log(
            cc_log_path=self.cc_log,
            sessions_dir=self.sessions_dir,
            db_path=self.db_path,
        )
        self.assertTrue(res["ok"])
        self.assertEqual(res["inserted"], 2)  # m1 + m3
        self.assertEqual(res["skipped_fast_silent"], 1)  # m2
        self.assertEqual(res["skipped_exact_dup"], 1)  # m4

        # Second incremental sync with no new bytes is a no-op
        res2 = token_usage.sync_from_cc_log(
            cc_log_path=self.cc_log,
            sessions_dir=self.sessions_dir,
            db_path=self.db_path,
        )
        self.assertEqual(res2["inserted"], 0)
        self.assertTrue(res2.get("unchanged"))

        dash = token_usage.get_usage_dashboard(
            range_key="24h",
            db_path=self.db_path,
            cc_log_path=self.cc_log,
            sessions_dir=self.sessions_dir,
        )
        # Total = 50,100 (live exact) + 20,000 (m1) + 23,500 (m3) = 93,600
        self.assertEqual(dash["summary"]["total_tokens"], 93600)
        self.assertEqual(dash["summary"]["total_calls"], 3)
        self.assertEqual(dash["summary"]["exact_calls"], 1)
        self.assertEqual(dash["summary"]["log_backfill_calls"], 2)

    def test_extract_agy_usage_from_stream_json(self):
        stdout = "\n".join(
            [
                json.dumps({"event": "step_update", "step_update": {"step_index": 1}}),
                json.dumps(
                    {
                        "event": "result",
                        "result": {
                            "status": "SUCCESS",
                            "duration_seconds": 3.25,
                            "usage": {
                                "input_tokens": 8000,
                                "cache_read_tokens": 1200,
                                "output_tokens": 150,
                                "thinking_tokens": 90,
                                "total_tokens": 9350,
                            },
                        },
                    }
                ),
            ]
        )
        usage, dur_ms = consolidate.extract_agy_usage(stdout)
        self.assertEqual(usage["total_tokens"], 9350)
        self.assertEqual(dur_ms, 3250)


class TestWebUITokensAPI(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmpdir.name, "token_usage.db")
        self.cc_log = os.path.join(self.tmpdir.name, "cc-connect.log")
        self.sessions_dir = os.path.join(self.tmpdir.name, "sessions")
        os.makedirs(self.sessions_dir, exist_ok=True)
        token_usage._INITIALIZED_DBS.discard(os.path.abspath(self.db_path))

        token_usage.record_usage(
            usage={"inputTokens": 1200, "cachedReadTokens": 8800, "outputTokens": 80, "totalTokens": 10080},
            session_key="qq:g:555001",
            is_group=True,
            group_id="555001",
            sender_id="1001",
            sender_name="Tester",
            source="chat",
            model="gemini-4-neon",
            thought_level="medium",
            db_path=self.db_path,
        )

        self._patches = [
            mock.patch.object(token_usage, "TOKEN_USAGE_DB_PATH", self.db_path),
            mock.patch.object(webui_services, "CC_LOG_PATH", self.cc_log),
            mock.patch.object(webui_services, "SESSIONS_DIR", self.sessions_dir),
            mock.patch.object(
                webui_services,
                "napcat_get_group_list",
                return_value={"ok": True, "groups": [], "group_map": {"555001": "联调测试群"}},
            ),
        ]
        for p in self._patches:
            p.start()

        self.srv = webui_server.HajimiHTTPServer(("127.0.0.1", 0), webui_server.HajimiRequestHandler)
        self.port = self.srv.server_address[1]
        import threading
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        for p in reversed(self._patches):
            p.stop()
        self.tmpdir.cleanup()

    def _get(self, path):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5) as r:
            return r.status, json.loads(r.read().decode("utf-8"))

    def _post(self, path, payload=None):
        body = json.dumps(payload or {}).encode("utf-8")
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode("utf-8"))

    def test_tokens_endpoints(self):
        code, body = self._get("/api/tokens?range=24h")
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        data = body["data"]
        self.assertEqual(data["summary"]["total_tokens"], 10080)
        self.assertEqual(len(data["chats"]), 1)
        self.assertEqual(data["chats"][0]["name"], "联调测试群")

        # Single chat detail endpoint
        enc = urllib.parse.quote("qq:g:555001", safe="")
        code2, body2 = self._get(f"/api/tokens/chat/{enc}?range=24h")
        self.assertEqual(code2, 200)
        self.assertTrue(body2["ok"])
        self.assertEqual(body2["data"]["chat"]["name"], "联调测试群")
        self.assertEqual(body2["data"]["chat"]["total_tokens"], 10080)

        # Sync endpoint
        code3, body3 = self._post("/api/tokens/sync", {"force": False})
        self.assertEqual(code3, 200)
        self.assertTrue(body3["ok"])


if __name__ == "__main__":
    unittest.main()
