"""Incremental group history + rule-block de-duplication (prompt_diet.py), and an
end-to-end run of agy-acp-wrapper.py against a fake NapCat and a fake ACP agent.

Stdlib only: python3 -m unittest tests/test_prompt_diet.py -v
The end-to-end test runs entirely in a temp dir (never touches live data/).
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

import prompt_diet  # noqa: E402


def E(key, uid, name, text, t="12:00", tag=""):
    return prompt_diet.make_entry(key=key, uid=uid, name=name, text=text, time_str=t, tag=tag)


class TestGroupHistory(unittest.TestCase):
    def setUp(self):
        self.c = prompt_diet.GroupHistoryCursor()
        self.base = [
            E("1", "111", "甲", "早上好", "09:00"),
            E("2", "222", "乙", "今天吃什么", "09:00"),
            E("3", "999", "机器猫", "火锅？", "09:01", tag="self"),
            E("4", "111", "甲", "好啊", "09:02"),  # current message
        ]

    def test_first_turn_is_full_with_roster_and_without_current(self):
        text, pending = prompt_diet.render_group_history(self.base, self.c, "测试群(1)", current_key="4")
        self.assertIn("[群聊近期消息记录", text)
        self.assertIn("[群成员: 甲=111，乙=222]", text)
        self.assertIn("- [09:00] 甲: 早上好", text)
        self.assertIn("- 乙: 今天吃什么", text)  # same minute: no repeated time
        self.assertIn("- [09:01] 你: 火锅？", text)
        self.assertNotIn("好啊", text)
        self.assertNotIn("111)", text)  # no name(QQ) on every line
        self.assertTrue(pending["full"])

    def test_delta_only_new_messages_and_new_people(self):
        _, p = prompt_diet.render_group_history(self.base, self.c, "g", current_key="4")
        self.c.commit(p)
        more = self.base + [E("5", "222", "乙", "走起", "09:03"), E("6", "333", "丙", "带我一个", "09:03"),
                            E("7", "111", "甲", "那就这样", "09:04")]
        text, p2 = prompt_diet.render_group_history(more, self.c, "g", current_key="7")
        self.assertTrue(text.startswith("[群聊新消息 — 你上一轮之后新增的 2 条]"))
        self.assertIn("[群成员新增: 丙=333]", text)
        self.assertIn("- [09:03] 乙: 走起", text)
        self.assertIn("- 丙: 带我一个", text)
        for old in ("早上好", "今天吃什么", "火锅？", "好啊", "那就这样"):
            self.assertNotIn(old, text)
        self.assertFalse(p2["full"])

    def test_nothing_new_renders_nothing(self):
        _, p = prompt_diet.render_group_history(self.base, self.c, "g", current_key="4")
        self.c.commit(p)
        text, _ = prompt_diet.render_group_history(self.base, self.c, "g", current_key="4")
        self.assertEqual(text, "")

    def test_uncommitted_turn_is_resent(self):
        prompt_diet.render_group_history(self.base, self.c, "g", current_key="4")  # never committed
        text, _ = prompt_diet.render_group_history(self.base, self.c, "g", current_key="4")
        self.assertIn("[群聊近期消息记录", text)

    def test_gap_note_when_window_has_no_overlap(self):
        _, p = prompt_diet.render_group_history(self.base, self.c, "g", current_key="4")
        self.c.commit(p)
        fresh = [E(str(k), "222", "乙", f"消息{k}", "10:00") for k in range(10, 15)]
        text, _ = prompt_diet.render_group_history(fresh, self.c, "g")
        self.assertIn("更早还有没显示的消息", text)
        self.assertNotIn("[群成员新增", text)  # 乙 already known

    def test_delta_is_capped(self):
        _, p = prompt_diet.render_group_history(self.base, self.c, "g", current_key="4", full_limit=3)
        self.c.commit(p)
        more = self.base + [E(str(k), "222", "乙", f"m{k}", "10:00") for k in range(10, 20)]
        text, _ = prompt_diet.render_group_history(more, self.c, "g", full_limit=3)
        self.assertEqual(text.count("\n- "), 3)
        self.assertIn("更早还有没显示的消息", text)

    def test_same_name_gets_suffix(self):
        entries = [E("1", "11110001", "小明", "a"), E("2", "22220002", "小明", "b")]
        text, _ = prompt_diet.render_group_history(entries, self.c, "g")
        self.assertIn("小明#0001=11110001", text)
        self.assertIn("小明#0002=22220002", text)

    def test_reset_makes_next_turn_full(self):
        _, p = prompt_diet.render_group_history(self.base, self.c, "g", current_key="4")
        self.c.commit(p)
        self.c.reset()
        text, _ = prompt_diet.render_group_history(self.base, self.c, "g", current_key="4")
        self.assertIn("[群聊近期消息记录", text)
        self.assertIn("[群成员:", text)

    def test_delta_disabled_is_always_full(self):
        _, p = prompt_diet.render_group_history(self.base, self.c, "g", current_key="4", delta=False)
        self.c.commit(p)
        text, _ = prompt_diet.render_group_history(self.base, self.c, "g", current_key="4", delta=False)
        self.assertIn("[群聊近期消息记录", text)

    def test_admin_tag_in_roster(self):
        entries = [E("1", "555", "管", "hi", tag="admin")]
        text, _ = prompt_diet.render_group_history(entries, self.c, "g")
        self.assertIn("管=555(管理员)", text)


class TestStaticBlocks(unittest.TestCase):
    def test_send_once_then_only_dynamic(self):
        s = prompt_diet.StaticBlockDedup()
        items = [(None, "动态"), ("style", "风格"), ("perm:member", "权限")]
        texts, p = s.select(items)
        self.assertEqual(texts, ["动态", "风格", "权限"])
        s.commit(p)
        texts, p = s.select(items)
        self.assertEqual(texts, ["动态"])

    def test_changed_text_is_resent(self):
        s = prompt_diet.StaticBlockDedup()
        _, p = s.select([("perm:member", "权限 v1")])
        s.commit(p)
        texts, _ = s.select([("perm:member", "权限 v2")])
        self.assertEqual(texts, ["权限 v2"])

    def test_uncommitted_is_resent(self):
        s = prompt_diet.StaticBlockDedup()
        s.select([("style", "风格")])
        texts, _ = s.select([("style", "风格")])
        self.assertEqual(texts, ["风格"])

    def test_periodic_refresh(self):
        s = prompt_diet.StaticBlockDedup()
        _, p = s.select([("style", "风格")])
        s.commit(p)
        for _ in range(2):
            self.assertFalse(s.due_refresh(3))
            _, p = s.select([("style", "风格")])
            s.commit(p)
        self.assertTrue(s.due_refresh(3))
        texts, p = s.select([("style", "风格")], force=True)
        self.assertEqual(texts, ["风格"])
        s.commit(p)
        self.assertFalse(s.due_refresh(3))

    def test_reset(self):
        d = prompt_diet.SessionDiet()
        _, p = d.static.select([("style", "风格")])
        d.commit({"static": p, "history": None})
        d.reset()
        texts, _ = d.static.select([("style", "风格")])
        self.assertEqual(texts, ["风格"])


# ---------------------------------------------------------------------------
# End-to-end: real wrapper process, fake NapCat HTTP, fake ACP agent, UDP sink
# ---------------------------------------------------------------------------

GID = 30000001
BOT = 10000009
FAKE_AGENT = r'''#!/usr/bin/env python3
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
              "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "NO_REPLY"}}}}), flush=True)
        res = {"stopReason": "end_turn"}
    else:
        res = {}
    print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": res}), flush=True)
'''


# Loaded into the wrapper process via PYTHONPATH: NapCat HTTP calls read a JSON
# file and guard UDP datagrams are appended to a file, so the test needs no
# localhost sockets (some sandboxes forbid them).
SHIM = r'''
import json, os, socket, sys
if os.environ.get("FAKE_NAPCAT_FILE"):
    sys.path.insert(0, os.environ["FAKE_PROJECT_DIR"])
    import onebot_common

    def _fake_post(http_url, token, endpoint, payload, timeout=2.5):
        with open(os.environ["FAKE_NAPCAT_FILE"], encoding="utf-8") as f:
            state = json.load(f)
        ep = str(endpoint).strip("/")
        if ep == "get_group_msg_history":
            return True, {"messages": state["messages"]}, ""
        if ep == "get_group_member_list":
            return True, [], ""
        if ep == "get_group_list":
            return True, [{"group_id": state["gid"], "group_name": "测试群"}], ""
        return False, None, "unsupported in test"

    onebot_common.napcat_api_post = _fake_post
if os.environ.get("FAKE_UDP_FILE"):
    _orig_sendto = socket.socket.sendto

    def _sendto(self, data, *args):
        addr = args[-1]
        if isinstance(addr, tuple) and addr[0] == "127.0.0.1":
            with open(os.environ["FAKE_UDP_FILE"], "a", encoding="utf-8") as f:
                f.write(data.decode("utf-8") + "\n")
            return len(data)
        return _orig_sendto(self, data, *args)

    socket.socket.sendto = _sendto
'''


def _gmsg(mid, uid, name, text, ts):
    return {"message_id": mid, "user_id": uid, "group_id": GID, "time": int(ts),
            "sender": {"nickname": name, "card": ""}, "message": [{"type": "text", "data": {"text": text}}]}


class TestWrapperEndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="prompt_diet_e2e_")
        shim_dir = os.path.join(self.tmp, "shim")
        os.makedirs(shim_dir)
        with open(os.path.join(shim_dir, "sitecustomize.py"), "w", encoding="utf-8") as f:
            f.write(SHIM)
        self.napcat_file = os.path.join(self.tmp, "napcat.json")
        self.udp_file = os.path.join(self.tmp, "udp.jsonl")
        self.udp_seen = 0
        self.set_messages([])

        agent = os.path.join(self.tmp, "fake-agy")
        with open(agent, "w") as f:
            f.write(FAKE_AGENT)
        os.chmod(agent, 0o755)
        work = os.path.join(self.tmp, "work")
        os.makedirs(work)
        shutil.copy(os.path.join(PROJECT_DIR, "workspace", "GEMINI.md"), os.path.join(work, "GEMINI.md"))
        with open(os.path.join(self.tmp, "plugins.json"), "w") as f:
            json.dump({"plugins": {k: {"enabled": True} for k in (
                "ambient_listen", "qq_actions", "chat_history_lookup", "style_hygiene", "extended_group_context")}
                | {k: {"enabled": False} for k in (
                    "repeat_chain", "memory_recall", "memory_extraction", "memory_continuity",
                    "soft_call_wakeup", "multimodal_vision", "web_search")}}, f)
        open(os.path.join(self.tmp, "empty.env"), "w").close()
        self.agent_log = os.path.join(self.tmp, "prompts.jsonl")
        env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG", "TMPDIR") if k in os.environ}
        env.update({
            "BOT_ENV_FILE": os.path.join(self.tmp, "empty.env"),
            "AGY_BIN": agent,
            "FAKE_AGENT_LOG": self.agent_log,
            "AGY_WORK_DIR": work,
            "CHAT_SANDBOX_DIR": os.path.join(self.tmp, "sandbox"),
            "CC_SESSION_KEY": f"qq:g:{GID}",
            "BOT_QQ_ID": str(BOT),
            "ADMIN_QQ_IDS": "10000001",
            "PYTHONPATH": shim_dir,
            "FAKE_PROJECT_DIR": PROJECT_DIR,
            "FAKE_NAPCAT_FILE": self.napcat_file,
            "FAKE_UDP_FILE": self.udp_file,
            "NAPCAT_HTTP_URL": "http://127.0.0.1:9",
            "NAPCAT_TOKEN": "t",
            "GUARD_UDP_PORT": "39999",
            "BOT_PLUGINS_FILE": os.path.join(self.tmp, "plugins.json"),
            "BOT_PERMISSIONS_FILE": os.path.join(self.tmp, "perms.json"),
            "CHAT_HISTORY_DB": os.path.join(self.tmp, "chat_history.db"),
            "MEMORY_DB_PATH": os.path.join(self.tmp, "memory.db"),
            "USER_MEMORY_DIR": os.path.join(self.tmp, "mem"),
            "USER_MEMORY_ENABLED": "0",
            "TOKEN_USAGE_DB": os.path.join(self.tmp, "tokens.db"),
            "GROUP_HISTORY_COUNT": "30",
            "BOT_PROMPT_MODE": "lean",
            "BOT_HISTORY_DELTA": "1",
        })
        self.stderr_path = os.path.join(self.tmp, "wrapper.stderr")
        self.stderr_f = open(self.stderr_path, "w")
        self.proc = subprocess.Popen(
            [sys.executable, os.path.join(PROJECT_DIR, "agy-acp-wrapper.py")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.stderr_f,
            env=env, cwd=self.tmp, text=True, encoding="utf-8", bufsize=1,
        )
        self.out = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()
        self.seq = 0

    def _pump(self):
        for line in self.proc.stdout:
            line = line.strip()
            if line:
                try:
                    self.out.put(json.loads(line))
                except ValueError:
                    pass

    def tearDown(self):
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.stderr_f.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def rpc(self, method, params, timeout=20):
        self.seq += 1
        rid = self.seq
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}) + "\n")
        self.proc.stdin.flush()
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                obj = self.out.get(timeout=0.5)
            except queue.Empty:
                continue
            if obj.get("id") == rid:
                return obj
        with open(self.stderr_path) as f:
            self.fail(f"no response to {method}; wrapper stderr:\n{f.read()[-3000:]}")

    def prompt(self, sender, name, body):
        text = f'[cc-connect sender_id={sender} sender_name="{name}" platform=qq chat_id={GID}]\n{body}'
        return self.rpc("session/prompt", {"sessionId": "sess-1", "prompt": [{"type": "text", "text": text}]})

    def last_prompt(self):
        with open(self.agent_log, encoding="utf-8") as f:
            lines = [ln for ln in f if ln.strip()]
        return json.loads(lines[-1])[0]["text"], len(lines)

    def set_messages(self, messages):
        with open(self.napcat_file, "w", encoding="utf-8") as f:
            json.dump({"gid": GID, "messages": messages}, f, ensure_ascii=False)

    def udp_done(self, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if os.path.exists(self.udp_file):
                with open(self.udp_file, encoding="utf-8") as f:
                    lines = [ln for ln in f if ln.strip()]
                if len(lines) > self.udp_seen:
                    self.udp_seen += 1
                    return json.loads(lines[self.udp_seen - 1])
            time.sleep(0.05)
        self.fail("no done datagram from the wrapper")

    def test_incremental_history_and_rule_dedup(self):
        now = time.time()
        self.rpc("initialize", {"protocolVersion": 1})
        self.assertEqual(self.rpc("session/new", {"cwd": self.tmp, "mcpServers": []})["result"]["sessionId"], "sess-1")

        # Turn 1: full window + every rule block once
        msgs = [
            _gmsg(101, 20000002, "乙", "早上好呀", now - 120),
            _gmsg(102, 20000003, "丙", "今天天气真不错", now - 90),
            _gmsg(103, 20000001, "甲", "今天中午吃什么呢大家", now),
        ]
        self.set_messages(msgs)
        self.assertEqual(self.prompt(20000001, "甲", "今天中午吃什么呢大家")["result"]["stopReason"], "end_turn")
        t1, n = self.last_prompt()
        self.assertEqual(n, 1)
        for must in ("[本轮状态: 群聊·潜水", "[状态说明", "[回复风格", "[群聊监听状态·潜水",
                     "[群聊近期消息记录", "[群成员:", "乙=20000002", "早上好呀", "今天天气真不错"):
            self.assertIn(must, t1)
        self.assertEqual(t1.count("今天中午吃什么呢大家"), 1)  # only as the message body
        done = self.udp_done()
        self.assertEqual(done["done_chat_key"], f"g:{GID}")
        self.assertTrue(done["silent"] and done["woke_model"] and done["ambient"])

        # Turn 2: only what is new, no rule blocks again
        msgs += [
            _gmsg(104, 20000002, "乙", "火锅吧我请客", now + 1),
            _gmsg(105, 20000001, "甲", "火锅可以 谁请客来着", now + 2),
        ]
        self.set_messages(msgs)
        self.prompt(20000001, "甲", "火锅可以 谁请客来着")
        t2, _ = self.last_prompt()
        self.assertIn("[本轮状态: 群聊·潜水", t2)
        self.assertIn("[群聊新消息 — 你上一轮之后新增的 1 条]", t2)
        self.assertIn("乙: 火锅吧我请客", t2)
        for gone in ("[状态说明", "[回复风格", "[群聊监听状态", "[群成员", "早上好呀", "[群聊近期消息记录"):
            self.assertNotIn(gone, t2)
        self.assertLess(len(t2), len(t1) / 3)
        self.udp_done()

        # Compaction resets the diet: next turn is full again
        self.rpc("session/prompt", {"sessionId": "sess-1", "prompt": [{"type": "text", "text": "/compact"}]})
        msgs += [
            _gmsg(106, 20000004, "丁", "我也要去", now + 3),
            _gmsg(107, 20000001, "甲", "那就这么定了啊", now + 4),
        ]
        self.set_messages(msgs)
        self.prompt(20000001, "甲", "那就这么定了啊")
        t3, _ = self.last_prompt()
        for must in ("[状态说明", "[回复风格", "[群聊近期消息记录", "[群成员:", "丁=20000004", "早上好呀"):
            self.assertIn(must, t3)


if __name__ == "__main__":
    unittest.main()
