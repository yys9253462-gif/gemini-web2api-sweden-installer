#!/usr/bin/env python3
"""本机回环绑定的 Gemini Web2API 管理面板；仅依赖 Python 标准库。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import http.cookies
import http.server
import json
import os
import re
import secrets
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

CONFIG_PATH = Path(os.environ.get("GEMINI_WEB2API_CONFIG", "/etc/gemini-web2api/config.json"))
PANEL_CONFIG_PATH = Path(os.environ.get("GEMINI_WEB2API_PANEL_CONFIG", "/etc/gemini-web2api/panel.json"))
COOKIE_PATH = Path(os.environ.get("GEMINI_WEB2API_COOKIE_FILE", "/etc/gemini-web2api/cookie.txt"))
STATIC_DIR = Path(os.environ.get("GEMINI_WEB2API_PANEL_STATIC", Path(__file__).parent))
SERVICE_NAME = os.environ.get("GEMINI_WEB2API_SERVICE", "gemini-web2api.service")
BIND_HOST = os.environ.get("GEMINI_PANEL_HOST", "127.0.0.1")
BIND_PORT = int(os.environ.get("GEMINI_PANEL_PORT", "8090"))
TEST_MODE = os.environ.get("GEMINI_PANEL_TEST_MODE") == "1"
SESSION_TTL = 30 * 60
MAX_BODY = 32 * 1024
PBKDF2_ROUNDS = 310_000

_sessions: dict[str, dict] = {}
_session_lock = threading.Lock()
_login_failures: list[float] = []
_login_lock = threading.Lock()


def _read_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        value = json.load(f)
    if not isinstance(value, dict):
        raise ValueError("配置文件格式无效")
    return value


def _atomic_write(path: Path, data: bytes, mode: int = 0o640, group: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp_name, mode)
        if os.name == "posix" and hasattr(os, "geteuid") and os.geteuid() == 0 and group:
            import grp
            os.chown(tmp_name, 0, grp.getgrnam(group).gr_gid)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _hash_password(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS)


def _verify_password(password: str) -> bool:
    panel = _read_json(PANEL_CONFIG_PATH)
    salt = base64.b64decode(panel["salt"])
    expected = base64.b64decode(panel["password_hash"])
    return hmac.compare_digest(_hash_password(password, salt), expected)


def _service_status() -> str:
    if TEST_MODE:
        return "active"
    try:
        p = subprocess.run(["systemctl", "is-active", SERVICE_NAME], capture_output=True, text=True, timeout=5)
        return p.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def _restart_api() -> tuple[bool, str]:
    if TEST_MODE:
        return True, "测试模式：未调用 systemd"
    try:
        p = subprocess.run(["systemctl", "restart", SERVICE_NAME], capture_output=True, text=True, timeout=35)
        if p.returncode != 0:
            return False, (p.stderr or p.stdout or "systemctl restart 失败").strip()[:1500]
        q = subprocess.run(["systemctl", "is-active", SERVICE_NAME], capture_output=True, text=True, timeout=5)
        if q.stdout.strip() != "active":
            return False, f"服务状态：{q.stdout.strip() or '未知'}"
        return True, "服务已重启"
    except Exception as e:
        return False, str(e)


def _safe_config() -> dict:
    cfg = _read_json(CONFIG_PATH)
    cookie_file = cfg.get("cookie_file")
    cookie_configured = bool(cookie_file and Path(cookie_file).is_file())
    proxy_value = cfg.get("proxy") or ""
    proxy_preview = ""
    if proxy_value:
        try:
            parts = urllib.parse.urlsplit(proxy_value)
            host = parts.hostname or ""
            port = f":{parts.port}" if parts.port else ""
            proxy_preview = f"{parts.scheme}://{host}{port}" if host else "已配置（地址已隐藏）"
        except Exception:
            proxy_preview = "已配置（地址已隐藏）"
    key_list = cfg.get("api_keys") or []
    key_preview = ""
    if key_list and isinstance(key_list[0], str):
        key_preview = (key_list[0][:4] + "••••" + key_list[0][-4:]) if len(key_list[0]) > 10 else "已配置"
    return {
        "host": cfg.get("host", "127.0.0.1"),
        "port": cfg.get("port", 8081),
        "default_model": cfg.get("default_model", "gemini-4-argon"),
        "retry_attempts": cfg.get("retry_attempts", 3),
        "retry_delay_sec": cfg.get("retry_delay_sec", 2),
        "request_timeout_sec": cfg.get("request_timeout_sec", 180),
        "temporary_chats": bool(cfg.get("temporary_chats", False)),
        "auth_user": cfg.get("auth_user") or "",
        "xsrf_configured": bool(cfg.get("xsrf_token")),
        "proxy_configured": bool(cfg.get("proxy")),
        "proxy_preview": proxy_preview,
        "cookie_configured": cookie_configured,
        "api_key_count": len(key_list),
        "api_key_preview": key_preview,
    }


def _write_config_and_restart(new_cfg: dict, cookie_bytes: bytes | None = None) -> tuple[bool, str]:
    old_config = CONFIG_PATH.read_bytes()
    old_cookie = COOKIE_PATH.read_bytes() if COOKIE_PATH.exists() else None
    try:
        if cookie_bytes is not None:
            _atomic_write(COOKIE_PATH, cookie_bytes, 0o640, "gemini-web2api")
            new_cfg["cookie_file"] = str(COOKIE_PATH)
        _atomic_write(CONFIG_PATH, (json.dumps(new_cfg, ensure_ascii=False, indent=2) + "\n").encode("utf-8"), 0o640, "gemini-web2api")
        ok, message = _restart_api()
        if ok:
            return True, message
        _atomic_write(CONFIG_PATH, old_config, 0o640, "gemini-web2api")
        if cookie_bytes is not None:
            if old_cookie is None:
                COOKIE_PATH.unlink(missing_ok=True)
            else:
                _atomic_write(COOKIE_PATH, old_cookie, 0o640, "gemini-web2api")
        _restart_api()
        return False, f"重启失败，已回滚配置：{message}"
    except Exception as e:
        try:
            _atomic_write(CONFIG_PATH, old_config, 0o640, "gemini-web2api")
            if cookie_bytes is not None:
                if old_cookie is None:
                    COOKIE_PATH.unlink(missing_ok=True)
                else:
                    _atomic_write(COOKIE_PATH, old_cookie, 0o640, "gemini-web2api")
        except Exception:
            pass
        return False, str(e)


def _normalize_cookie_input(raw: str) -> tuple[str, int]:
    """Accept a Cookie header or a browser-exported cookie JSON array."""
    raw = raw.strip()
    if raw.startswith(("[", "{")):
        try:
            exported = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("Cookie JSON 格式无效") from exc
        if isinstance(exported, dict):
            exported = exported.get("cookies")
        if not isinstance(exported, list):
            raise ValueError("JSON 必须是 Cookie 数组，或包含 cookies 数组的对象")

        now = time.time()
        selected: dict[str, tuple[int, str]] = {}
        for item in exported:
            if not isinstance(item, dict):
                continue
            domain = item.get("domain")
            name = item.get("name")
            value = item.get("value")
            if not all(isinstance(part, str) for part in (domain, name, value)):
                continue
            normalized_domain = domain.lstrip(".").lower()
            if normalized_domain not in {"google.com", "gemini.google.com"}:
                continue
            if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,256}", name):
                continue
            # Cookie header values cannot contain delimiters/control chars. Drop
            # malformed optional cookies (for example analytics entries).
            if not value or any(ord(ch) < 0x21 or ord(ch) > 0x7E or ch in '";,\\' for ch in value):
                continue
            expiry = item.get("expirationDate")
            if expiry is not None:
                try:
                    if float(expiry) <= now:
                        continue
                except (TypeError, ValueError, OverflowError):
                    continue
            priority = len(normalized_domain)
            existing = selected.get(name)
            if existing is None or priority > existing[0]:
                selected[name] = (priority, value)

        if "__Secure-1PSID" not in selected:
            raise ValueError("JSON 中没有有效且未过期的 __Secure-1PSID；请确认导出包含 Gemini 登录 Cookie")
        pairs = [(name, entry[1]) for name, entry in selected.items()]
        return "; ".join(f"{name}={value}" for name, value in pairs), len(pairs)

    parsed = http.cookies.SimpleCookie()
    try:
        parsed.load(raw)
    except Exception as exc:
        raise ValueError("Cookie 字符串格式无法解析") from exc
    if "__Secure-1PSID" not in parsed:
        raise ValueError("Cookie 中缺少 __Secure-1PSID")
    return raw, len(parsed)


class PanelHandler(http.server.BaseHTTPRequestHandler):
    server_version = "GeminiPanel"
    sys_version = ""

    def log_message(self, fmt: str, *args) -> None:
        # 不输出请求体、Cookie 或认证头。
        print(f"[panel] {self.client_address[0]} {fmt % args}", flush=True)

    def _security_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'")

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self._security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, value: dict) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _error(self, status: int, message: str) -> None:
        self._json(status, {"error": message})

    def _body(self) -> dict:
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ValueError("请求长度无效")
        if size < 0 or size > MAX_BODY:
            raise ValueError("请求内容过大")
        raw = self.rfile.read(size) if size else b"{}"
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("请求必须是 JSON 对象")
        return value

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return self.headers.get("Sec-Fetch-Site", "same-origin") != "cross-site"
        parsed = urllib.parse.urlsplit(origin)
        host = self.headers.get("X-Forwarded-Host") or self.headers.get("Host", "")
        return bool(parsed.netloc and hmac.compare_digest(parsed.netloc.lower(), host.lower()))

    def _session(self) -> dict | None:
        cookie = http.cookies.SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
            token = cookie.get("panel_session").value if cookie.get("panel_session") else ""
        except Exception:
            return None
        with _session_lock:
            record = _sessions.get(token)
            if not record:
                return None
            if record["expires"] < time.time():
                _sessions.pop(token, None)
                return None
            record["expires"] = time.time() + SESSION_TTL
            return record

    def _require_session(self) -> dict | None:
        session = self._session()
        if not session:
            self._error(401, "登录已失效，请重新登录")
            return None
        return session

    def _require_csrf(self, session: dict) -> bool:
        if not self._origin_ok():
            self._error(403, "跨站请求已拒绝")
            return False
        supplied = self.headers.get("X-CSRF-Token", "")
        if not supplied or not hmac.compare_digest(supplied, session["csrf"]):
            self._error(403, "请求校验失败，请刷新页面重试")
            return False
        return True

    def _cookie(self, token: str, max_age: int, clear: bool = False) -> str:
        secure = " Secure;" if self.headers.get("X-Forwarded-Proto", "").lower() == "https" else ""
        if clear:
            return f"panel_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict;{secure}"
        return f"panel_session={token}; Path=/; Max-Age={max_age}; HttpOnly; SameSite=Strict;{secure}"

    def do_GET(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        if path in ("/", "/index.html"):
            try:
                self._send(200, (STATIC_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
            except OSError:
                self._error(500, "面板页面文件不存在")
            return
        if path in ("/app.css", "/app.js"):
            name = path.lstrip("/")
            mime = "text/css; charset=utf-8" if name.endswith(".css") else "text/javascript; charset=utf-8"
            try:
                self._send(200, (STATIC_DIR / name).read_bytes(), mime)
            except OSError:
                self._error(404, "静态资源不存在")
            return
        if path == "/api/session":
            session = self._session()
            self._json(200, {"authenticated": bool(session), "csrf": session["csrf"] if session else ""})
            return
        if path.startswith("/api/"):
            session = self._require_session()
            if not session:
                return
            if path == "/api/status":
                self._json(200, self._status())
            elif path == "/api/config":
                try:
                    self._json(200, _safe_config())
                except Exception as e:
                    self._error(500, f"读取配置失败：{e}")
            elif path == "/api/logs":
                self._logs()
            else:
                self._error(404, "接口不存在")
            return
        self._error(404, "页面不存在")

    def _status(self) -> dict:
        config = _safe_config()
        healthy = False
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{config['port']}/", method="GET")
            with urllib.request.urlopen(req, timeout=3) as r:
                healthy = r.status == 200
        except Exception:
            pass
        try:
            panel = _read_json(PANEL_CONFIG_PATH)
        except Exception:
            panel = {}
        domain = panel.get("public_domain", "")
        return {
            "service": _service_status(),
            "api_healthy": healthy,
            "domain": domain,
            "api_url": f"https://{domain}/v1" if domain else f"http://127.0.0.1:{config['port']}/v1",
            "panel_url": f"https://{domain}/panel/" if domain else "http://127.0.0.1:8090/",
            "config": config,
        }

    def _logs(self) -> None:
        try:
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            lines = int(q.get("lines", ["100"])[0])
            lines = min(max(lines, 20), 300)
            if TEST_MODE:
                self._json(200, {"lines": ["测试模式：journalctl 未调用"]})
                return
            p = subprocess.run(["journalctl", "-u", SERVICE_NAME, "-n", str(lines), "--no-pager", "-o", "short-iso"], capture_output=True, text=True, timeout=8)
            self._json(200, {"lines": (p.stdout or p.stderr).splitlines()[-lines:]})
        except Exception as e:
            self._error(500, f"读取日志失败：{e}")

    def do_POST(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        try:
            body = self._body()
        except Exception as e:
            self._error(400, str(e))
            return
        if path == "/api/login":
            self._login(body)
            return
        session = self._require_session()
        if not session or not self._require_csrf(session):
            return
        try:
            if path == "/api/logout":
                token = http.cookies.SimpleCookie(self.headers.get("Cookie", "")).get("panel_session")
                if token:
                    with _session_lock:
                        _sessions.pop(token.value, None)
                self.send_response(200)
                self.send_header("Set-Cookie", self._cookie("", 0, clear=True))
                self._send_headers_json()
                self.wfile.write(b'{"ok":true}')
            elif path == "/api/config":
                self._save_settings(body)
            elif path == "/api/cookie":
                self._save_cookie(body)
            elif path == "/api/api-key/rotate":
                self._rotate_key()
            elif path == "/api/service/restart":
                ok, message = _restart_api()
                self._json(200 if ok else 503, {"ok": ok, "message": message})
            elif path == "/api/test":
                self._test_api(body)
            elif path == "/api/password":
                self._change_password(body)
            else:
                self._error(404, "接口不存在")
        except Exception as e:
            self._error(500, f"操作失败：{e}")

    def _send_headers_json(self) -> None:
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", "11")
        self._security_headers()
        self.end_headers()

    def _login(self, body: dict) -> None:
        if not self._origin_ok():
            self._error(403, "跨站登录请求已拒绝")
            return
        now = time.time()
        with _login_lock:
            _login_failures[:] = [t for t in _login_failures if now - t < 300]
            if len(_login_failures) >= 10:
                self._error(429, "登录尝试过多，请五分钟后再试")
                return
        password = body.get("password", "")
        if not isinstance(password, str) or len(password) > 512:
            password = ""
        try:
            ok = _verify_password(password)
        except Exception:
            ok = False
        if not ok:
            with _login_lock:
                _login_failures.append(now)
            self._error(401, "管理员密码不正确")
            return
        with _login_lock:
            _login_failures.clear()
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(24)
        with _session_lock:
            _sessions[token] = {"csrf": csrf, "expires": time.time() + SESSION_TTL}
        payload = json.dumps({"ok": True, "csrf": csrf}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Set-Cookie", self._cookie(token, SESSION_TTL))
        self._security_headers()
        self.end_headers()
        self.wfile.write(payload)

    def _save_settings(self, body: dict) -> None:
        cfg = _read_json(CONFIG_PATH)
        allowed = {"default_model", "retry_attempts", "retry_delay_sec", "request_timeout_sec", "temporary_chats", "auth_user", "xsrf_token", "proxy"}
        unknown = set(body) - allowed
        if unknown:
            self._error(400, "不支持的设置项：" + ", ".join(sorted(unknown)))
            return
        if "default_model" in body:
            value = body["default_model"]
            if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:@=-]{1,100}", value):
                self._error(400, "模型名称格式无效")
                return
        for key, lo, hi in (("retry_attempts", 1, 10), ("retry_delay_sec", 0, 60), ("request_timeout_sec", 10, 600)):
            if key in body and (type(body[key]) is not int or not lo <= body[key] <= hi):
                self._error(400, f"{key} 超出允许范围")
                return
        if "temporary_chats" in body and type(body["temporary_chats"]) is not bool:
            self._error(400, "temporary_chats 必须是布尔值")
            return
        if "auth_user" in body:
            value = body["auth_user"]
            if value not in (None, "") and (not isinstance(value, str) or not re.fullmatch(r"\d{1,4}", value)):
                self._error(400, "账号序号应为 0-9999 的字符串")
                return
            body["auth_user"] = value or None
        if "xsrf_token" in body and (not isinstance(body["xsrf_token"], str) or len(body["xsrf_token"]) > 4096):
            self._error(400, "XSRF 字段过长或格式无效")
            return
        if "proxy" in body and (not isinstance(body["proxy"], str) or len(body["proxy"]) > 512 or (body["proxy"] and not body["proxy"].startswith(("http://", "https://", "socks5://")))):
            self._error(400, "代理地址仅支持 http、https 或 socks5")
            return
        cfg.update(body)
        ok, message = _write_config_and_restart(cfg)
        self._json(200 if ok else 503, {"ok": ok, "message": message, "config": _safe_config() if ok else None})

    def _save_cookie(self, body: dict) -> None:
        raw_cookie = body.get("cookie", "")
        if not isinstance(raw_cookie, str) or not raw_cookie.strip() or len(raw_cookie) > 24_000:
            self._error(400, "请填写有效 Cookie 字符串或浏览器导出的 JSON（不超过 24 KB）")
            return
        try:
            cookie, cookie_count = _normalize_cookie_input(raw_cookie)
        except ValueError as exc:
            self._error(400, str(exc))
            return
        cfg = _read_json(CONFIG_PATH)
        auth_user = body.get("auth_user")
        if auth_user not in (None, "") and (not isinstance(auth_user, str) or not re.fullmatch(r"\d{1,4}", auth_user)):
            self._error(400, "账号序号应为 0-9999 的字符串")
            return
        xsrf = body.get("xsrf_token", cfg.get("xsrf_token") or "")
        if not isinstance(xsrf, str) or len(xsrf) > 4096:
            self._error(400, "XSRF 字段过长或格式无效")
            return
        cfg["auth_user"] = auth_user or None
        cfg["xsrf_token"] = xsrf or None
        ok, message = _write_config_and_restart(cfg, cookie.encode("utf-8"))
        if ok:
            message = f"已保存 {cookie_count} 项 Cookie 并重启 API。{message}".strip()
        self._json(200 if ok else 503, {"ok": ok, "message": message, "config": _safe_config() if ok else None})

    def _rotate_key(self) -> None:
        cfg = _read_json(CONFIG_PATH)
        new_key = "sk-" + secrets.token_urlsafe(32)
        cfg["api_keys"] = [new_key]
        ok, message = _write_config_and_restart(cfg)
        self._json(200 if ok else 503, {"ok": ok, "message": message, "api_key": new_key if ok else ""})

    def _test_api(self, body: dict) -> None:
        prompt = body.get("prompt", "")
        model = body.get("model", "")
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 4000:
            self._error(400, "测试内容不能为空且不能超过 4000 字符")
            return
        cfg = _read_json(CONFIG_PATH)
        if model:
            if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9_.:@=-]{1,100}", model):
                self._error(400, "模型名称格式无效")
                return
        else:
            model = cfg.get("default_model", "gemini-4-argon")
        keys = cfg.get("api_keys") or []
        if not keys:
            self._error(503, "当前没有配置 API Key")
            return
        request_body = json.dumps({"model": model, "messages": [{"role": "user", "content": prompt}]}).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{int(cfg.get('port', 8081))}/v1/chat/completions",
            data=request_body,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {keys[0]}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=min(int(cfg.get("request_timeout_sec", 180)), 120)) as r:
                result = json.loads(r.read(2 * 1024 * 1024).decode("utf-8"))
            content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
            self._json(200, {"ok": True, "model": model, "content": content})
        except urllib.error.HTTPError as e:
            self._json(502, {"ok": False, "message": e.read(4000).decode("utf-8", "replace")})
        except Exception as e:
            self._json(502, {"ok": False, "message": str(e)[:1500]})

    def _change_password(self, body: dict) -> None:
        password = body.get("password", "")
        if not isinstance(password, str) or len(password) < 14 or len(password) > 256:
            self._error(400, "管理员密码至少 14 位")
            return
        data = _read_json(PANEL_CONFIG_PATH)
        salt = secrets.token_bytes(16)
        data["salt"] = base64.b64encode(salt).decode()
        data["password_hash"] = base64.b64encode(_hash_password(password, salt)).decode()
        _atomic_write(PANEL_CONFIG_PATH, (json.dumps(data, indent=2) + "\n").encode(), 0o600)
        self._json(200, {"ok": True, "message": "管理员密码已更新"})


def main() -> None:
    if not PANEL_CONFIG_PATH.is_file():
        raise SystemExit(f"面板配置不存在：{PANEL_CONFIG_PATH}")
    httpd = http.server.ThreadingHTTPServer((BIND_HOST, BIND_PORT), PanelHandler)
    httpd.daemon_threads = True
    print(f"管理面板仅监听 {BIND_HOST}:{BIND_PORT}", flush=True)
    httpd.serve_forever(poll_interval=0.5)


if __name__ == "__main__":
    main()
