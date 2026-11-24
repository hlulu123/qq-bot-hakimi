"""Tests for ACTIVE_CONV_MAX_PER_WINDOW=0 (unlimited active conversation follow-ups).

Covers:
1. WebUI config_schema metadata (`zero_label`, `min=0`) and stores validation/restart scope.
2. End-to-end wrapper pacing: `ACTIVE_CONV_MAX_PER_WINDOW=0` allows unlimited active
   conversation follow-ups (only gated by gap), while `ACTIVE_CONV_MAX_PER_WINDOW=1`
   caps follow-ups after 1 ambient reply in the window.

Stdlib only: python3 -m unittest tests/test_active_conv_unlimited.py -v
"""

import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

from webui import config_schema, stores as webui_stores  # noqa: E402
from tests.test_prompt_diet import BOT, GID, SHIM, _gmsg  # noqa: E402


FAKE_REPLY_AGENT = r'''#!/usr/bin/env python3
import json, os, sys
log = os.environ["FAKE_AGENT_LOG"]
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    mid, method = msg.get("id"), msg.get("method")
    if mid is None:
        continue
    if method == "session/new":
        res = {"sessionId": "sess-1", "configOptions": []}
    elif method == "session/prompt":
        sid = msg["params"]["sessionId"]
        with open(log, "a", encoding="utf-8") as f:
            f.write(json.dumps(msg["params"]["prompt"], ensure_ascii=False) + "\n")
        print(json.dumps({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": sid,
              "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "收到啦 我接着聊"}}}}), flush=True)
        res = {"stopReason": "end_turn"}
    else:
        res = {}
    print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": res}), flush=True)
'''


class TestActiveConvSchemaAndStores(unittest.TestCase):
    def test_schema_exposes_unlimited_zero_label(self):
        item = config_schema._SCHEMA_BY_KEY["ACTIVE_CONV_MAX_PER_WINDOW"]
        self.assertEqual(item["type"], "number")
        self.assertEqual(item["min"], 0)
        self.assertEqual(item.get("zero_label"), "无限制")
        self.assertEqual(item.get("restart"), "full")

    def test_stores_validates_zero_and_bounds(self):
        norm, changed = webui_stores._validate_env_updates(
            {"ACTIVE_CONV_MAX_PER_WINDOW": "0"},
            current_env={"ACTIVE_CONV_MAX_PER_WINDOW": "4"},
        )
        self.assertEqual(norm["ACTIVE_CONV_MAX_PER_WINDOW"], "0")
        self.assertEqual(changed, ["ACTIVE_CONV_MAX_PER_WINDOW"])
        self.assertEqual(webui_stores._compute_restart_scope(changed), "full")

        norm_int, _ = webui_stores._validate_env_updates(
            {"ACTIVE_CONV_MAX_PER_WINDOW": 0},
            current_env={"ACTIVE_CONV_MAX_PER_WINDOW": "4"},
        )
        self.assertEqual(norm_int["ACTIVE_CONV_MAX_PER_WINDOW"], "0")

        with self.assertRaises(webui_stores.StoreError):
            webui_stores._validate_env_updates(
                {"ACTIVE_CONV_MAX_PER_WINDOW": "-1"},
                current_env={},
            )
        with self.assertRaises(webui_stores.StoreError):
            webui_stores._validate_env_updates(
                {"ACTIVE_CONV_MAX_PER_WINDOW": "51"},
                current_env={},
            )


class TestWrapperActiveConvUnlimited(unittest.TestCase):
    def _run_sequence(self, active_conv_max: str, turns: int = 3):
        tmp = tempfile.mkdtemp(prefix="active_conv_e2e_")
        stderr_f = None
        proc = None
        try:
            shim_dir = os.path.join(tmp, "shim")
            os.makedirs(shim_dir)
            with open(os.path.join(shim_dir, "sitecustomize.py"), "w", encoding="utf-8") as f:
                f.write(SHIM)
            napcat_file = os.path.join(tmp, "napcat.json")
            udp_file = os.path.join(tmp, "udp.jsonl")
            agent_log = os.path.join(tmp, "prompts.jsonl")
            agent = os.path.join(tmp, "fake-agy")
            with open(agent, "w", encoding="utf-8") as f:
                f.write(FAKE_REPLY_AGENT)
            os.chmod(agent, 0o755)
            work = os.path.join(tmp, "work")
            os.makedirs(work)
            shutil.copy(os.path.join(PROJECT_DIR, "workspace", "GEMINI.md"), os.path.join(work, "GEMINI.md"))
            with open(os.path.join(tmp, "plugins.json"), "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "plugins": {
                            "ambient_listen": {"enabled": True, "settings": {"active_conv_followup": True}},
                            "qq_actions": {"enabled": True},
                            "chat_history_lookup": {"enabled": False},
                            "style_hygiene": {"enabled": False},
                            "extended_group_context": {"enabled": True},
                            "repeat_chain": {"enabled": False},
                            "memory_recall": {"enabled": False},
                            "memory_extraction": {"enabled": False},
                            "memory_continuity": {"enabled": False},
                            "soft_call_wakeup": {"enabled": False},
                            "multimodal_vision": {"enabled": False},
                            "web_search": {"enabled": False},
                        }
                    },
                    f,
                )
            open(os.path.join(tmp, "empty.env"), "w").close()
            env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG", "TMPDIR") if k in os.environ}
            env.update({
                "BOT_ENV_FILE": os.path.join(tmp, "empty.env"),
                "AGY_BIN": agent,
                "FAKE_AGENT_LOG": agent_log,
                "AGY_WORK_DIR": work,
                "CHAT_SANDBOX_DIR": os.path.join(tmp, "sandbox"),
                "CC_SESSION_KEY": f"qq:g:{GID}",
                "BOT_QQ_ID": str(BOT),
                "ADMIN_QQ_IDS": "10000001",
                "PYTHONPATH": shim_dir,
                "FAKE_PROJECT_DIR": PROJECT_DIR,
                "FAKE_NAPCAT_FILE": napcat_file,
                "FAKE_UDP_FILE": udp_file,
                "NAPCAT_HTTP_URL": "http://127.0.0.1:9",
                "NAPCAT_TOKEN": "t",
                "GUARD_UDP_PORT": "39999",
                "BOT_PLUGINS_FILE": os.path.join(tmp, "plugins.json"),
                "BOT_PERMISSIONS_FILE": os.path.join(tmp, "perms.json"),
                "CHAT_HISTORY_DB": os.path.join(tmp, "chat_history.db"),
                "MEMORY_DB_PATH": os.path.join(tmp, "memory.db"),
                "USER_MEMORY_DIR": os.path.join(tmp, "mem"),
                "USER_MEMORY_ENABLED": "0",
                "TOKEN_USAGE_DB": os.path.join(tmp, "tokens.db"),
                "GROUP_HISTORY_COUNT": "20",
                "AMBIENT_MIN_GAP_SECS": "0",
                "AMBIENT_WINDOW_SECS": "600",
                "AMBIENT_MAX_PER_WINDOW": "1",
                "ACTIVE_CONV_WINDOW_SECS": "60",
                "ACTIVE_CONV_MIN_GAP_SECS": "0",
                "ACTIVE_CONV_MAX_PER_WINDOW": active_conv_max,
            })
            stderr_path = os.path.join(tmp, "wrapper.stderr")
            stderr_f = open(stderr_path, "w", encoding="utf-8")
            proc = subprocess.Popen(
                [sys.executable, os.path.join(PROJECT_DIR, "agy-acp-wrapper.py")],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=stderr_f,
                env=env,
                cwd=tmp,
                text=True,
                encoding="utf-8",
                bufsize=1,
            )
            out = queue.Queue()

            def _pump():
                for line in proc.stdout:
                    line = line.strip()
                    if line:
                        try:
                            out.put(json.loads(line))
                        except ValueError:
                            pass

            threading.Thread(target=_pump, daemon=True).start()
            seq = 0

            def rpc(method, params, timeout=20):
                nonlocal seq
                seq += 1
                rid = seq
                proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}) + "\n")
                proc.stdin.flush()
                deadline = time.time() + timeout
                while time.time() < deadline:
                    try:
                        obj = out.get(timeout=0.5)
                    except queue.Empty:
                        continue
                    if obj.get("id") == rid:
                        return obj
                with open(stderr_path, encoding="utf-8") as f:
                    self.fail(f"no response to {method}; stderr:\n{f.read()[-2000:]}")

            def set_messages(messages):
                with open(napcat_file, "w", encoding="utf-8") as f:
                    json.dump({"gid": GID, "messages": messages}, f, ensure_ascii=False)

            set_messages([])
            rpc("initialize", {"protocolVersion": 1})
            rpc("session/new", {"cwd": tmp, "mcpServers": []})

            now = time.time()
            # Seed an initial @bot turn so the bot speaks and enters active conversation window.
            seed_msg = {
                "message_id": 200,
                "user_id": 20000001,
                "group_id": GID,
                "time": int(now),
                "sender": {"nickname": "甲", "card": ""},
                "message": [
                    {"type": "at", "data": {"qq": str(BOT)}},
                    {"type": "text", "data": {"text": " 今晚吃什么好呀"}},
                ],
            }
            msgs = [seed_msg]
            set_messages(msgs)
            rpc(
                "session/prompt",
                {
                    "sessionId": "sess-1",
                    "prompt": [{"type": "text", "text": f'[cc-connect sender_id=20000001 sender_name="甲" platform=qq chat_id={GID}]\n今晚吃什么好呀'}],
                },
            )

            # Follow-up unmentioned messages during the active conversation window
            for i in range(turns):
                body = f"那第 {i + 1} 个方案怎么样呀 展开说说"
                msgs.append(_gmsg(201 + i, 20000001, "甲", body, now + i + 1))
                set_messages(msgs)
                rpc(
                    "session/prompt",
                    {
                        "sessionId": "sess-1",
                        "prompt": [{"type": "text", "text": f'[cc-connect sender_id=20000001 sender_name="甲" platform=qq chat_id={GID}]\n{body}'}],
                    },
                )

            # Wait briefly for final UDP datagrams to flush
            time.sleep(0.2)
            with open(agent_log, encoding="utf-8") as f:
                prompt_count = len([ln for ln in f if ln.strip()])
            udp_events = []
            if os.path.exists(udp_file):
                with open(udp_file, encoding="utf-8") as f:
                    udp_events = [json.loads(ln) for ln in f if ln.strip()]
            return prompt_count, udp_events
        finally:
            if proc is not None:
                try:
                    proc.stdin.close()
                except Exception:
                    pass
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=2)
                try:
                    proc.stdout.close()
                except Exception:
                    pass
            if stderr_f is not None:
                stderr_f.close()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_zero_allows_unlimited_active_followups(self):
        # 1 seed @bot turn + 3 unmentioned active follow-ups = all 4 wake the model
        prompt_count, udp_events = self._run_sequence("0", turns=3)
        self.assertEqual(prompt_count, 4)
        self.assertTrue(all(not ev.get("silent") for ev in udp_events))

    def test_positive_limit_caps_active_followups(self):
        # 1 seed @bot turn + 1 allowed active follow-up; 2nd & 3rd short-circuited silently
        prompt_count, udp_events = self._run_sequence("1", turns=3)
        self.assertEqual(prompt_count, 2)
        silent_events = [ev for ev in udp_events if ev.get("silent") and not ev.get("woke_model")]
        self.assertEqual(len(silent_events), 2)


if __name__ == "__main__":
    unittest.main()
