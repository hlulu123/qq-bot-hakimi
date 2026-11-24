"""Unit tests for zero-login NapCat WebUI API integration and QR SVG encoder."""

import hashlib
import json
import os
import tempfile
import unittest
from unittest import mock

from webui import napcat_api, qrcode_svg, services, stores


class TestQRCodeSVG(unittest.TestCase):
    def test_generate_svg_typical_qq_url(self):
        url = "https://txz.qq.com/p?k=EXAMPLE_QR_TOKEN_0123456789abcdef&f=1000000000"
        svg = qrcode_svg.generate_qr_svg(url, border=3, size_px=220)
        self.assertTrue(svg.startswith("<svg "))
        self.assertTrue(svg.endswith("</svg>"))
        self.assertIn('width="220"', svg)
        self.assertIn("<path d=", svg)
        self.assertNotIn("<script", svg.lower())

    def test_versions_and_level_l_fallback(self):
        # Short string -> V1 (21x21)
        m1 = qrcode_svg.encode_qr_matrix("hello")
        self.assertEqual(len(m1), 21)
        # Check top-left 7x7 finder outer border is dark
        for i in range(7):
            self.assertEqual(m1[0][i], 1)
            self.assertEqual(m1[6][i], 1)
            self.assertEqual(m1[i][0], 1)
            self.assertEqual(m1[i][6], 1)

        # Longer string > 213 bytes -> auto-falls back to Level L (V10 = 57x57)
        m10 = qrcode_svg.encode_qr_matrix("A" * 240)
        self.assertEqual(len(m10), 57)

    def test_invalid_inputs(self):
        with self.assertRaises(ValueError):
            qrcode_svg.encode_qr_matrix("")
        with self.assertRaises(ValueError):
            qrcode_svg.encode_qr_matrix("X" * 300)


class TestNapCatApi(unittest.TestCase):
    def setUp(self):
        napcat_api.invalidate_caches()

    def tearDown(self):
        napcat_api.invalidate_caches()

    def test_token_hash_matches_napcat_spec(self):
        expected = hashlib.sha256(b"mysecret.napcat").hexdigest()
        self.assertEqual(napcat_api._token_hash("mysecret"), expected)

    def test_normalize_sys_status(self):
        raw = {
            "cpu": {"core": 10, "model": "Apple M3", "speed": "2.40", "usage": {"system": "12.5", "qq": "1.25"}},
            "memory": {"total": "8000.0", "usage": {"system": "2000.0", "qq": "80.0"}},
            "arch": "linux arm64",
        }
        norm = napcat_api._normalize_sys_status(raw)
        self.assertEqual(norm["arch"], "linux arm64")
        self.assertEqual(norm["cpu"]["cores"], 10)
        self.assertEqual(norm["cpu"]["system_pct"], 12.5)
        self.assertEqual(norm["cpu"]["qq_pct"], 1.25)
        self.assertEqual(norm["memory"]["total_mb"], 8000.0)
        self.assertEqual(norm["memory"]["system_pct"], 25.0)
        self.assertEqual(norm["memory"]["qq_pct"], 1.0)

    def test_auto_login_and_retry_on_unauthorized(self):
        fake_meta = {"port": 6099, "token": "tok123", "disabled": False, "exists": True}
        calls = []

        def fake_http(url, method="GET", headers=None, payload=None, timeout=2.5):
            calls.append((method, url, headers, payload))
            if url.endswith("/api/auth/login"):
                return True, {"code": 0, "data": {"Credential": "cred_" + str(len(calls))}, "message": "success"}, 200
            # First API call rejects with Unauthorized, second succeeds
            if len(calls) == 2:
                return True, {"code": -1, "message": "Unauthorized"}, 200
            return True, {"code": 0, "data": {"version": "4.18.42"}, "message": "success"}, 200

        with mock.patch.object(napcat_api, "_get_meta", return_value=fake_meta), \
             mock.patch.object(napcat_api, "_raw_http_json", side_effect=fake_http):
            ok, data, err = napcat_api.api_request("GET", "/api/base/GetNapCatVersion")
            self.assertTrue(ok)
            self.assertEqual(data, {"version": "4.18.42"})
            self.assertEqual(err, "")
            # Expect: login -> call (Unauthorized) -> re-login -> call (success)
            self.assertEqual(len(calls), 4)

    def test_2fa_handled_gracefully(self):
        fake_meta = {"port": 6099, "token": "tok2fa", "disabled": False, "exists": True}

        def fake_http(url, method="GET", headers=None, payload=None, timeout=2.5):
            return True, {"code": 0, "data": {"require2FA": True}, "message": "success"}, 200

        with mock.patch.object(napcat_api, "_get_meta", return_value=fake_meta), \
             mock.patch.object(napcat_api, "_raw_http_json", side_effect=fake_http):
            cred, meta = napcat_api.get_credential(force=True)
            self.assertIsNone(cred)
            self.assertTrue(meta["require_2fa"])
            self.assertFalse(meta["authenticated"])

    def test_ob11_config_disk_fallback_and_summary(self):
        with tempfile.TemporaryDirectory() as td:
            sample = {
                "network": {
                    "httpServers": [{"name": "h1", "enable": True, "port": 3000}],
                    "httpSseServers": [],
                    "httpClients": [],
                    "websocketServers": [
                        {"name": "w1", "enable": True, "port": 3001},
                        {"name": "w2", "enable": False, "port": 3009},
                    ],
                    "websocketClients": [],
                }
            }
            with open(os.path.join(td, "onebot11_12345.json"), "w", encoding="utf-8") as f:
                json.dump(sample, f)

            with mock.patch.object(napcat_api, "api_request", return_value=(False, None, "offline")), \
                 mock.patch.object(napcat_api, "_napcat_config_dir", return_value=td):
                res = napcat_api.get_ob11_config(bot_qq="12345")
                self.assertTrue(res["available"])
                self.assertEqual(res["source"], "disk")
                self.assertEqual(res["summary"]["total"], 3)
                self.assertEqual(res["summary"]["enabled"], 2)

    def test_set_ob11_config_serializes_json_string(self):
        captured = {}

        def fake_api(method, path, payload=None, query=None, timeout=2.5):
            if path == "/api/OB11Config/SetConfig":
                captured["payload"] = payload
                return True, None, ""
            return True, {"network": {"httpServers": []}}, ""

        with mock.patch.object(napcat_api, "api_request", side_effect=fake_api):
            napcat_api.set_ob11_config({"network": {"httpServers": []}})
            self.assertIn("config", captured["payload"])
            self.assertIsInstance(captured["payload"]["config"], str)
            self.assertEqual(json.loads(captured["payload"]["config"]), {"network": {"httpServers": []}})

    def test_validation_guards(self):
        with self.assertRaises(stores.StoreError):
            napcat_api.get_log_file_content("../secret.log")
        with self.assertRaises(stores.StoreError):
            napcat_api.get_log_file_content("sub/file.log")
        with self.assertRaises(stores.StoreError):
            napcat_api.set_core_config({"consoleLogLevel": "invalid_level"})
        with self.assertRaises(stores.StoreError):
            napcat_api.quick_login("abc")
        with self.assertRaises(stores.StoreError):
            services.call_napcat_debug_api("../etc/passwd", {})
        with self.assertRaises(stores.StoreError):
            services.call_napcat_debug_api("get_status", ["not_a_dict"])


if __name__ == "__main__":
    unittest.main()
