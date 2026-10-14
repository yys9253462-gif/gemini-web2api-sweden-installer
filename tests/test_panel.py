"""Panel HTTP smoke tests; run with: python -m unittest discover -s tests"""
import base64
import hashlib
import http.cookiejar
import json
import os
import socket
import secrets
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PANEL_SERVER = ROOT / "panel" / "server.py"


class PanelHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="gemini-panel-test-")
        cls.tmp = Path(cls.temp.name)
        cls.config_path = cls.tmp / "config.json"
        cls.panel_config_path = cls.tmp / "panel.json"
        cls.cookie_path = cls.tmp / "cookie.txt"
        api_key = "sk-" + secrets.token_urlsafe(24)
        cls.proxy_password = secrets.token_urlsafe(12)
        cls.config_path.write_text(json.dumps({
            "host": "127.0.0.1", "port": 8081, "default_model": "gemini-3.6-flash",
            "retry_attempts": 3, "retry_delay_sec": 2, "request_timeout_sec": 180,
            "temporary_chats": True, "auth_user": None, "xsrf_token": None,
            "proxy": "http://user:" + cls.proxy_password + "@proxy.example:8080", "cookie_file": None, "api_keys": [api_key],
        }), encoding="utf-8")
        salt = os.urandom(16)
        password = "test-" + secrets.token_urlsafe(24)
        password_hash = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 310_000)
        cls.panel_config_path.write_text(json.dumps({
            "salt": base64.b64encode(salt).decode(),
            "password_hash": base64.b64encode(password_hash).decode(),
            "public_domain": "test.example.com",
        }), encoding="utf-8")
        cls.password = password
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            cls.port = sock.getsockname()[1]
        env = os.environ.copy()
        env.update({
            "GEMINI_WEB2API_CONFIG": str(cls.config_path),
            "GEMINI_WEB2API_PANEL_CONFIG": str(cls.panel_config_path),
            "GEMINI_WEB2API_COOKIE_FILE": str(cls.cookie_path),
            "GEMINI_WEB2API_PANEL_STATIC": str(PANEL_SERVER.parent),
            "GEMINI_WEB2API_SERVICE": "gemini-web2api-test.service",
            "GEMINI_PANEL_HOST": "127.0.0.1",
            "GEMINI_PANEL_PORT": str(cls.port),
            "GEMINI_PANEL_TEST_MODE": "1",
        })
        cls.process = subprocess.Popen([sys.executable, str(PANEL_SERVER)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.cookies = http.cookiejar.CookieJar()
        cls.client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cls.cookies))
        for _ in range(50):
            try:
                cls.client.open(cls.base + "/", timeout=0.2)
                break
            except Exception:
                time.sleep(0.1)
        else:
            raise RuntimeError("panel test server did not start")

    @classmethod
    def tearDownClass(cls):
        cls.process.terminate()
        try:
            cls.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            cls.process.kill()
        cls.temp.cleanup()

    def request(self, path, method="GET", payload=None, csrf=True):
        data = json.dumps(payload).encode() if payload is not None else None
        headers = {"Origin": self.base}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if csrf and hasattr(self, "csrf") and method != "GET" and path != "/api/login":
            headers["X-CSRF-Token"] = self.csrf
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with self.client.open(req, timeout=5) as response:
                return response.status, json.loads(response.read().decode())
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read().decode())
            finally:
                e.close()

    def login(self):
        status, data = self.request("/api/login", "POST", {"password": self.password}, csrf=False)
        self.assertEqual(status, 200)
        self.csrf = data["csrf"]

    def test_login_config_cookie_key_and_logout(self):
        status, session = self.request("/api/session")
        self.assertEqual(status, 200)
        self.assertFalse(session["authenticated"])
        self.login()

        status, config = self.request("/api/config")
        self.assertEqual(status, 200)
        self.assertNotIn("api_keys", config)
        self.assertNotIn("proxy", config)
        self.assertNotIn(self.proxy_password, json.dumps(config))
        self.assertEqual(config["proxy_preview"], "http://proxy.example:8080")
        self.assertFalse(config["cookie_configured"])

        status, result = self.request("/api/config", "POST", {
            "default_model": "gemini-3.6-flash", "retry_attempts": 4,
            "retry_delay_sec": 2, "request_timeout_sec": 180,
            "temporary_chats": True, "proxy": "",
        })
        self.assertEqual(status, 200)
        self.assertTrue(result["ok"])

        cookie = "SID=sid-value; SAPISID=sapisid-value; __Secure-1PSID=g.session-value"
        status, result = self.request("/api/cookie", "POST", {
            "cookie": cookie, "auth_user": "1", "xsrf_token": "xsrf-test",
        })
        self.assertEqual(status, 200)
        self.assertTrue(result["ok"])
        self.assertTrue(self.cookie_path.is_file())
        self.assertEqual(self.cookie_path.read_text(encoding="utf-8"), cookie)
        saved = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["auth_user"], "1")
        self.assertEqual(saved["xsrf_token"], "xsrf-test")

        status, result = self.request("/api/api-key/rotate", "POST", {})
        self.assertEqual(status, 200)
        self.assertTrue(result["api_key"].startswith("sk-"))
        saved = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["api_keys"], [result["api_key"]])

        status, result = self.request("/api/logout", "POST", {})
        self.assertEqual(status, 200)
        status, session = self.request("/api/session")
        self.assertFalse(session["authenticated"])

    def test_mutation_requires_csrf(self):
        self.login()
        status, _ = self.request("/api/service/restart", "POST", {}, csrf=False)
        self.assertEqual(status, 403)

    def test_login_page_and_static_assets_are_public(self):
        self.assertEqual(self.client.open(self.base + "/", timeout=5).status, 200)
        self.assertEqual(self.client.open(self.base + "/app.css", timeout=5).status, 200)
        self.assertEqual(self.client.open(self.base + "/app.js", timeout=5).status, 200)


if __name__ == "__main__":
    unittest.main()
