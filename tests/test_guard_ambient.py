"""Lurking-path pacing in onebot-ws-guard.py and the shared low-signal filter.

Covers: signal_filter (?, ？, …, emoji, filler words), the guard's inbound
classification (ignore / image_only / fingerprints), AmbientGate (NO_REPLY
back-off, resets, hourly cap, image de-dup + budget, live-conversation pass),
and the UDP done-signal compatibility with older wrappers.

Stdlib only: python3 -m unittest tests/test_guard_ambient.py -v
"""

import importlib.util
import os
import sys
import unittest

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

import signal_filter  # noqa: E402


def _load_guard():
    spec = importlib.util.spec_from_file_location(
        "onebot_ws_guard_under_test", os.path.join(PROJECT_DIR, "onebot-ws-guard.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


guard = _load_guard()
guard.log = lambda msg: None  # keep test output clean


def _group_event(segments, uid="20000001", gid="30000001"):
    return {
        "post_type": "message",
        "message_type": "group",
        "group_id": int(gid),
        "user_id": int(uid),
        "sender": {"nickname": "测试群友", "card": ""},
        "message": segments,
    }


def _text(t):
    return {"type": "text", "data": {"text": t}}


class TestSignalFilter(unittest.TestCase):
    def test_noise_is_low_signal(self):
        for s in ("?", "？？？", "…", "……", "😂😂", "👍🏻", "❤️", "👨‍👩‍👧",
                  "~", "(＾▽＾)", "[表情]", "[动画表情]", "[语音]", "", "  ",
                  "哈哈哈", "草", "6666", "嗯嗯", "ok", "好的"):
            self.assertTrue(signal_filter.is_low_signal_text(s), repr(s))

    def test_content_is_not_low_signal(self):
        for s in ("怎么了", "这是什么", "明天去吃饭吗", "哈哈哈笑死我了这个",
                  "orz", "?? 你说啥", "草 这也行", "1️⃣"):
            self.assertFalse(signal_filter.is_low_signal_text(s), repr(s))


class TestGuardClassification(unittest.TestCase):
    def setUp(self):
        self._saved = (guard.BOT_QQ_ID, list(guard.DIRECT_CALL_NAMES), list(guard.SOFT_CALL_CUES))
        guard.BOT_QQ_ID = "10000009"
        guard.DIRECT_CALL_NAMES[:] = ["测试机器猫"]
        guard.SOFT_CALL_CUES[:] = []

    def tearDown(self):
        guard.BOT_QQ_ID = self._saved[0]
        guard.DIRECT_CALL_NAMES[:] = self._saved[1]
        guard.SOFT_CALL_CUES[:] = self._saved[2]

    def test_question_marks_and_emoji_are_ignored(self):
        for t in ("?", "？", "…", "😂😂😂", "哈哈哈"):
            info = guard.analyze_inbound_group_event(_group_event([_text(t)]), set())
            self.assertTrue(info["ignore"], t)

    def test_real_sentence_passes(self):
        info = guard.analyze_inbound_group_event(_group_event([_text("今天吃什么好呢")]), set())
        self.assertFalse(info["ignore"])
        self.assertFalse(info["image_only"])

    def test_addressed_noise_still_passes(self):
        segs = [{"type": "at", "data": {"qq": "10000009"}}, _text("？")]
        info = guard.analyze_inbound_group_event(_group_event(segs), set())
        self.assertTrue(info["priority"])
        self.assertFalse(info["ignore"])

    def test_image_only_fingerprint(self):
        segs = [{"type": "image", "data": {"file": "ABCDEF0123.jpg", "url": "https://x/download?fileid=zz&rkey=1"}}]
        info = guard.analyze_inbound_group_event(_group_event(segs), set())
        self.assertFalse(info["ignore"])
        self.assertTrue(info["image_only"])
        self.assertEqual(info["image_fps"], ["img:abcdef0123.jpg"])

    def test_image_with_text_is_not_image_only(self):
        segs = [{"type": "image", "data": {"file": "a.jpg"}}, _text("你们看这个离谱不")]
        info = guard.analyze_inbound_group_event(_group_event(segs), set())
        self.assertFalse(info["image_only"])

    def test_fingerprint_from_url_and_mface(self):
        fps = guard.image_fingerprints([
            {"type": "image", "data": {"url": "https://multimedia.example/download?appid=1&fileid=FID9&rkey=abc"}},
            {"type": "mface", "data": {"emoji_id": "e1", "emoji_package_id": "p2"}},
        ])
        self.assertEqual(fps, ["img:FID9", "mface:p2:e1"])


def _gate(**kw):
    opts = dict(base_gap=20, max_gap=300, max_per_hour=0, backoff=True, image_budget=True,
                image_convo_secs=60, image_dedup_secs=3600, image_window_secs=600, image_max=1)
    opts.update(kw)
    return guard.AmbientGate(**opts)


class TestAmbientGate(unittest.TestCase):
    K = "g:1"

    def _silent(self, g):
        g.on_turn_done(self.K, silent=True, woke_model=True, ambient=True)

    def test_gap_doubles_per_silent_turn_and_caps(self):
        g = _gate()
        gaps = []
        for _ in range(6):
            gaps.append(g.current_gap(self.K))
            self._silent(g)
        self.assertEqual(gaps, [20, 40, 80, 160, 300, 300])

    def test_gap_is_enforced(self):
        g = _gate()
        self.assertEqual(g.allow(self.K, 1000.0), (True, ""))
        self._silent(g)  # gap now 40
        self.assertEqual(g.allow(self.K, 1030.0), (False, "gap"))
        self.assertEqual(g.allow(self.K, 1041.0), (True, ""))

    def test_resets(self):
        g = _gate()
        for reset in (
            lambda: g.on_turn_done(self.K, silent=False, woke_model=True, ambient=True),
            lambda: g.on_addressed(self.K),
            lambda: g.on_bot_spoke(self.K, 5.0),
        ):
            self._silent(g)
            self._silent(g)
            self.assertEqual(g.current_gap(self.K), 80)
            reset()
            self.assertEqual(g.current_gap(self.K), 20)

    def test_filtered_or_addressed_silence_does_not_count(self):
        g = _gate()
        g.on_turn_done(self.K, silent=True, woke_model=False, ambient=True)
        g.on_turn_done(self.K, silent=True, woke_model=True, ambient=False)
        self.assertEqual(g.current_gap(self.K), 20)

    def test_backoff_disabled_keeps_fixed_gap(self):
        g = _gate(backoff=False)
        self._silent(g)
        self._silent(g)
        self.assertEqual(g.current_gap(self.K), 20)

    def test_hourly_cap(self):
        g = _gate(base_gap=0, max_per_hour=3)
        for t in (0.0, 1.0, 2.0):
            self.assertTrue(g.allow(self.K, t)[0])
        self.assertEqual(g.allow(self.K, 3.0), (False, "hour_cap"))
        self.assertTrue(g.allow(self.K, 3601.0)[0])

    def test_active_conv_unlimited_bypasses_hourly_cap(self):
        # When active_conv_unlimited=True, live conversation follow-ups bypass hour_cap
        # and do not consume the cold ambient hourly budget.
        g = _gate(base_gap=0, max_per_hour=2, active_conv_secs=60, active_conv_unlimited=True)
        g.on_bot_spoke(self.K, 1000.0)
        for t in (1005.0, 1010.0, 1015.0, 1020.0):
            self.assertEqual(g.allow(self.K, t), (True, ""))
        # After active convo expires (>60s), cold ambient still obeys max_per_hour=2
        self.assertEqual(g.allow(self.K, 1100.0), (True, ""))
        self.assertEqual(g.allow(self.K, 1101.0), (True, ""))
        self.assertEqual(g.allow(self.K, 1102.0), (False, "hour_cap"))

        # With active_conv_unlimited=False (default), active convo is still subject to hour_cap
        g_cap = _gate(base_gap=0, max_per_hour=2, active_conv_secs=60, active_conv_unlimited=False)
        g_cap.on_bot_spoke(self.K, 1000.0)
        self.assertEqual(g_cap.allow(self.K, 1005.0), (True, ""))
        self.assertEqual(g_cap.allow(self.K, 1010.0), (True, ""))
        self.assertEqual(g_cap.allow(self.K, 1015.0), (False, "hour_cap"))

    def test_image_dedup_and_budget(self):
        g = _gate(base_gap=0)
        self.assertEqual(g.allow(self.K, 0.0, image_only=True, fingerprints=["img:a"]), (True, ""))
        # a different picture inside the 10-minute budget window
        self.assertEqual(g.allow(self.K, 100.0, image_only=True, fingerprints=["img:b"]), (False, "image_budget"))
        # same picture again within the hour (budget window already over)
        self.assertEqual(g.allow(self.K, 700.0, image_only=True, fingerprints=["img:a"]), (False, "image_dup"))
        # budget window passed
        self.assertEqual(g.allow(self.K, 800.0, image_only=True, fingerprints=["img:c"]), (True, ""))
        # text messages are never charged against the picture budget
        self.assertEqual(g.allow(self.K, 801.0), (True, ""))

    def test_pictures_pass_during_live_conversation(self):
        g = _gate(base_gap=0)
        g.on_bot_spoke(self.K, 1000.0)
        for t, fp in ((1001.0, "img:a"), (1002.0, "img:a"), (1003.0, "img:b")):
            self.assertEqual(g.allow(self.K, t, image_only=True, fingerprints=[fp]), (True, ""))
        # conversation window (60s) over: budget applies again
        self.assertTrue(g.allow(self.K, 1100.0, image_only=True, fingerprints=["img:z"])[0])
        self.assertEqual(g.allow(self.K, 1101.0, image_only=True, fingerprints=["img:y"]), (False, "image_budget"))

    def test_image_budget_disabled(self):
        g = _gate(base_gap=0, image_budget=False)
        for t in (0.0, 1.0):
            self.assertTrue(g.allow(self.K, t, image_only=True, fingerprints=["img:a"])[0])


class _FakeBridge:
    def __init__(self):
        self.ambient_gate = _gate()
        self.released = []

    def schedule_chat_release(self, chat_key, silent=False):
        self.released.append((chat_key, silent))

    def mark_chat_busy(self, chat_key, actor_role="member"):
        pass


class TestUdpDoneSignal(unittest.TestCase):
    def _send(self, bridge, payload):
        import json
        guard.UDPDoneProtocol(bridge).datagram_received(json.dumps(payload).encode(), ("127.0.0.1", 1))

    def test_new_fields_grow_backoff(self):
        b = _FakeBridge()
        self._send(b, {"done_chat_key": "g:1", "silent": True, "woke_model": True, "ambient": True})
        self.assertEqual(b.ambient_gate.current_gap("g:1"), 40)
        self.assertEqual(b.released, [("g:1", True)])

    def test_old_wrapper_payload_is_compatible(self):
        b = _FakeBridge()
        self._send(b, {"done_chat_key": "g:1", "silent": True})
        self.assertEqual(b.ambient_gate.current_gap("g:1"), 20)
        self.assertEqual(b.released, [("g:1", True)])


class TestBlockedOutboundNotices(unittest.TestCase):
    def setUp(self):
        self._saved_silent_idle = guard.SILENT_IDLE_RESET
        guard.SILENT_IDLE_RESET = True

    def tearDown(self):
        guard.SILENT_IDLE_RESET = self._saved_silent_idle

    def _group_send(self, msg):
        return {"action": "send_group_msg", "params": {"group_id": 1000000001, "message": msg}}

    def test_blocks_idle_reset_and_closing_notices(self):
        for txt in (
            "⏰ 因空闲超过 360 分钟，已自动切换到新会话。",
            "⏰ 因閒置超過 60 分鐘，已自動切換到新會話。",
            "⏰ Session auto-reset after 360 minute(s) of inactivity.",
            "⏳ 正在结束上一个会话（通常几秒钟，最多2分钟）。新会话将自动启动。",
            "⏳ Wrapping up your previous session (usually a few seconds, up to 2 minutes). Your new session will start automatically.",
        ):
            self.assertTrue(guard.is_blocked_outbound(self._group_send(txt)), txt)
            self.assertTrue(guard.is_blocked_outbound(self._group_send([_text(txt)])), txt)

    def test_respects_silent_idle_reset_toggle(self):
        guard.SILENT_IDLE_RESET = False
        notice = "⏰ 因空闲超过 360 分钟，已自动切换到新会话。"
        self.assertFalse(guard.is_blocked_outbound(self._group_send(notice)))

    def test_normal_chat_mentioning_idle_reset_is_not_blocked(self):
        for txt in (
            "刚才那个是空闲超过 360 分钟自动切换到新会话的提示",
            "冷启动之后上下文会清爽一点",
            "✅ 新会话已创建",
        ):
            self.assertFalse(guard.is_blocked_outbound(self._group_send(txt)), txt)


if __name__ == "__main__":
    unittest.main()
