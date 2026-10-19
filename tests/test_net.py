"""网络传输层测试。

分两段：
1. 纯假传输层 —— 验证 HttpClient 的行为，完全离线；
2. 本地回环 HTTP 服务 —— 验证 UrllibTransport 真的能跑通，且不依赖外网。
"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from kedaibiao.net.http import (
    HttpClient,
    HttpStatusError,
    Response,
    ResponseNotJson,
    TransportError,
    UrllibTransport,
)


class FakeTransport:
    """记录调用并按脚本返回响应的假传输层。"""

    def __init__(self, *responses: Response) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    async def request(self, method, url, *, headers=None, body=None, timeout=15.0):
        self.calls.append(
            {"method": method, "url": url, "headers": headers or {}, "body": body, "timeout": timeout}
        )
        if not self.responses:
            raise AssertionError("FakeTransport 没有更多预设响应")
        return self.responses.pop(0)


def _json_response(payload, status: int = 200, url: str = "http://x/") -> Response:
    return Response(status=status, url=url, body=json.dumps(payload).encode())


class TestResponse(unittest.TestCase):
    def test_text_and_ok(self) -> None:
        r = Response(status=200, url="u", body="你好".encode())
        self.assertEqual(r.text(), "你好")
        self.assertTrue(r.ok)

    def test_not_ok(self) -> None:
        self.assertFalse(Response(status=404, url="u").ok)
        self.assertFalse(Response(status=500, url="u").ok)

    def test_json(self) -> None:
        self.assertEqual(Response(status=200, url="u", body=b'{"a":1}').json(), {"a": 1})

    def test_non_json_raises_with_preview(self) -> None:
        r = Response(status=200, url="http://x/y", body=b"<html>login</html>")
        with self.assertRaises(ResponseNotJson) as cm:
            r.json()
        self.assertIn("http://x/y", str(cm.exception))
        self.assertIn("login", str(cm.exception))

    def test_gbk_encoding(self) -> None:
        r = Response(status=200, url="u", body="中文".encode("gbk"), encoding="gbk")
        self.assertEqual(r.text(), "中文")


class TestErrorHierarchy(unittest.TestCase):
    def test_transport_error_is_oserror_so_scheduler_retries(self) -> None:
        from kedaibiao.core.scheduler import RETRYABLE

        self.assertTrue(issubclass(TransportError, OSError))
        self.assertTrue(any(issubclass(TransportError, t) for t in RETRYABLE))

    def test_status_error_carries_response(self) -> None:
        resp = Response(status=503, url="http://x/", body=b"busy")
        err = HttpStatusError(resp)
        self.assertEqual(err.status, 503)
        self.assertIs(err.response, resp)
        self.assertIn("503", str(err))


class TestHttpClientWithFakeTransport(unittest.IsolatedAsyncioTestCase):
    async def test_get_merges_base_headers(self) -> None:
        fake = FakeTransport(_json_response({"ok": True}))
        client = HttpClient(fake, base_headers={"X-App": "kedaibiao"})
        resp = await client.get("http://x/a", headers={"X-Extra": "1"})
        headers = fake.calls[0]["headers"]
        self.assertEqual(headers["X-App"], "kedaibiao")
        self.assertEqual(headers["X-Extra"], "1")
        self.assertIn("Accept-Language", headers)
        self.assertEqual(resp.status, 200)

    async def test_per_request_header_overrides_base(self) -> None:
        fake = FakeTransport(_json_response({}))
        client = HttpClient(fake, base_headers={"Accept": "application/json"})
        await client.get("http://x/", headers={"Accept": "text/html"})
        self.assertEqual(fake.calls[0]["headers"]["Accept"], "text/html")

    async def test_post_json_sets_content_type_and_body(self) -> None:
        fake = FakeTransport(_json_response({"ok": 1}))
        client = HttpClient(fake)
        await client.post_json("http://x/", {"中文": "值"})
        call = fake.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["headers"]["Content-Type"], "application/json")
        self.assertEqual(json.loads(call["body"].decode()), {"中文": "值"})

    async def test_post_form_urlencodes(self) -> None:
        fake = FakeTransport(_json_response({"ok": 1}))
        client = HttpClient(fake)
        await client.post_form("http://x/", {"a": "1 2", "b": "值"})
        call = fake.calls[0]
        self.assertEqual(call["headers"]["Content-Type"], "application/x-www-form-urlencoded")
        self.assertIn("a=1+2", call["body"].decode())

    async def test_get_json_raises_on_error_status(self) -> None:
        fake = FakeTransport(_json_response({"err": 1}, status=500))
        client = HttpClient(fake)
        with self.assertRaises(HttpStatusError):
            await client.get_json("http://x/")

    async def test_expect_error_suppresses_status_error(self) -> None:
        fake = FakeTransport(_json_response({"err": "业务错误"}, status=400))
        client = HttpClient(fake)
        self.assertEqual(await client.get_json("http://x/", expect_error=True), {"err": "业务错误"})

    async def test_post_json_data_happy_path(self) -> None:
        fake = FakeTransport(_json_response({"code": 1}))
        client = HttpClient(fake)
        self.assertEqual(await client.post_json_data("http://x/", {"q": 1}), {"code": 1})

    async def test_timeout_override(self) -> None:
        fake = FakeTransport(_json_response({}))
        client = HttpClient(fake, timeout=3.0)
        await client.get("http://x/", timeout=99.0)
        self.assertEqual(fake.calls[0]["timeout"], 99.0)

    async def test_join(self) -> None:
        client = HttpClient(FakeTransport())
        self.assertEqual(client.join("https://a.cn/x/", "../y"), "https://a.cn/y")


class TestCookiePersistence(unittest.TestCase):
    def _transport_with_cookie(self) -> UrllibTransport:
        import http.cookiejar

        t = UrllibTransport()
        t.cookies.set_cookie(
            http.cookiejar.Cookie(
                version=0, name="sid", value="abc123", port=None, port_specified=False,
                domain=".unipus.cn", domain_specified=True, domain_initial_dot=True,
                path="/", path_specified=True, secure=True, expires=1893456000,
                discard=False, comment=None, comment_url=None, rest={},
            )
        )
        return t

    def test_save_load_roundtrip(self) -> None:
        t = self._transport_with_cookie()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cookies.json"
            self.assertEqual(t.save_cookies(path), 1)

            restored = UrllibTransport()
            self.assertEqual(restored.load_cookies(path), 1)
            cookies = list(restored.cookies)
            self.assertEqual(len(cookies), 1)
            self.assertEqual(cookies[0].name, "sid")
            self.assertEqual(cookies[0].value, "abc123")
            self.assertEqual(cookies[0].domain, ".unipus.cn")
            self.assertTrue(cookies[0].secure)

    def test_load_missing_file_is_noop(self) -> None:
        self.assertEqual(UrllibTransport().load_cookies("/nonexistent/x.json"), 0)

    def test_cookie_header(self) -> None:
        t = self._transport_with_cookie()
        self.assertEqual(t.cookie_header(), "sid=abc123")

    def test_saved_file_is_gitignored_path(self) -> None:
        """cookie 是凭据 —— 确认 .gitignore 覆盖了常见落盘位置。"""
        root = Path(__file__).resolve().parent.parent
        ignore = (root / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("*.cookies.json", ignore)
        self.assertIn("storage_state", ignore)


# --------------------------------------------------------------------------
# 本地回环服务：验证 UrllibTransport 真能跑
# --------------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *args) -> None:  # noqa: ANN002 - 静音测试日志
        pass

    def _send(self, status: int, body: bytes, **headers: str) -> None:
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        for key, value in headers.items():
            self.send_header(key.replace("_", "-"), value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        if self.path == "/json":
            self._send(200, b'{"ok":true}', Content_Type="application/json; charset=utf-8")
        elif self.path == "/setcookie":
            self._send(200, b"{}", Set_Cookie="sid=loopback; Path=/")
        elif self.path == "/echo-cookie":
            self._send(200, json.dumps({"cookie": self.headers.get("Cookie", "")}).encode(),
                       Content_Type="application/json")
        elif self.path == "/boom":
            self._send(500, b'{"error":"boom"}', Content_Type="application/json")
        else:
            self._send(404, b"nope")

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        data = self.rfile.read(length)
        if self.path == "/echo":
            self._send(200, data, Content_Type=self.headers.get("Content-Type", "text/plain"))
        else:
            self._send(404, b"nope")


class TestUrllibTransportLoopback(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    async def test_get_json_real_request(self) -> None:
        client = HttpClient(UrllibTransport())
        self.assertEqual(await client.get_json(f"{self.base}/json"), {"ok": True})

    async def test_post_json_roundtrip(self) -> None:
        client = HttpClient(UrllibTransport())
        payload = {"题目": "答案", "n": 1}
        resp = await client.post_json(f"{self.base}/echo", payload)
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.json(), payload)

    async def test_cookies_are_stored_and_replayed(self) -> None:
        transport = UrllibTransport()
        client = HttpClient(transport)
        await client.get(f"{self.base}/setcookie")
        self.assertEqual(len(list(transport.cookies)), 1)

        echoed = await client.get_json(f"{self.base}/echo-cookie")
        self.assertIn("sid=loopback", echoed["cookie"])

    async def test_http_error_becomes_response_not_exception(self) -> None:
        client = HttpClient(UrllibTransport())
        resp = await client.get(f"{self.base}/boom")
        self.assertEqual(resp.status, 500)
        self.assertEqual(resp.json(), {"error": "boom"})

    async def test_status_error_raised_by_get_json(self) -> None:
        client = HttpClient(UrllibTransport())
        with self.assertRaises(HttpStatusError):
            await client.get_json(f"{self.base}/boom")

    async def test_connection_failure_is_transport_error(self) -> None:
        # 端口 1 上不会有服务
        client = HttpClient(UrllibTransport())
        with self.assertRaises(TransportError):
            await client.get("http://127.0.0.1:1/x", timeout=1.0)


if __name__ == "__main__":
    unittest.main()
