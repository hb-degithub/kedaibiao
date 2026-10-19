"""浏览器 cookie 抓取测试（不需要真浏览器，验证逻辑与协议帧）。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kedaibiao.browser.capture import (
    CaptureError,
    capture_cookies,
    describe_cookies,
    parse_cookie_header,
    save_session_cookie,
)
from kedaibiao.browser.cdp import Cookie, cookie_header, find_browser


class TestCookieHeader(unittest.TestCase):
    def _c(self, name: str, value: str, domain: str) -> Cookie:
        return Cookie(name=name, value=value, domain=domain)

    def test_joins(self) -> None:
        header = cookie_header([self._c("a", "1", "x.com"), self._c("b", "2", "x.com")])
        self.assertEqual(header, "a=1; b=2")

    def test_domain_filter(self) -> None:
        header = cookie_header(
            [self._c("a", "1", ".seewo.com"), self._c("z", "9", "other.cn")],
            domain_hint="seewo.com",
        )
        self.assertEqual(header, "a=1")

    def test_more_specific_domain_wins(self) -> None:
        """`.seewo.com` 与 `pinco.seewo.com` 同名 cookie 时取更具体的。"""
        header = cookie_header([
            self._c("csrfToken", "root", ".seewo.com"),
            self._c("csrfToken", "pinco", "pinco.seewo.com"),
        ])
        self.assertEqual(header, "csrfToken=pinco")

    def test_empty(self) -> None:
        self.assertEqual(cookie_header([]), "")


class TestParseCookieHeader(unittest.TestCase):
    def test_basic(self) -> None:
        self.assertEqual(
            parse_cookie_header("csrfToken=abc; x-token=xyz"),
            {"csrfToken": "abc", "x-token": "xyz"},
        )

    def test_values_with_equals(self) -> None:
        self.assertEqual(parse_cookie_header("a=b=c"), {"a": "b=c"})

    def test_ignores_junk(self) -> None:
        self.assertEqual(parse_cookie_header("; a=1 ; junk ; b=2"), {"a": "1", "b": "2"})

    def test_empty(self) -> None:
        self.assertEqual(parse_cookie_header(""), {})


class TestSaveSessionCookie(unittest.TestCase):
    def test_writes_0600_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = save_session_cookie(Path(tmp) / "sub" / "x.session.json", "a=1; b=2")
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["cookie_header"], "a=1; b=2")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_empty_rejected(self) -> None:
        with self.assertRaises(CaptureError):
            save_session_cookie("/tmp/x.json", "   ")


class TestDescribe(unittest.TestCase):
    def test_no_values_leaked(self) -> None:
        """摘要里只给长度，不给值 —— 令牌不该出现在日志里。"""
        items = describe_cookies([Cookie(name="csrfToken", value="SECRET-VALUE",
                                         domain="x.com")])
        blob = json.dumps(items)
        self.assertNotIn("SECRET-VALUE", blob)
        self.assertEqual(items[0]["length"], len("SECRET-VALUE"))
        self.assertEqual(items[0]["name"], "csrfToken")


class TestCaptureErrors(unittest.TestCase):
    def test_no_browser(self) -> None:
        with mock.patch("kedaibiao.browser.capture.find_browser", return_value=None):
            with self.assertRaises(CaptureError) as cm:
                capture_cookies(url="https://x.com/", profile=None)
        self.assertEqual(cm.exception.kind, "no_browser")

    def test_empty_cookies_is_error(self) -> None:
        fake = mock.MagicMock()
        fake.port = 1
        fake.cookies.return_value = []
        fake.__enter__ = lambda self: self
        fake.__exit__ = lambda *args: None
        with mock.patch("kedaibiao.browser.capture.find_browser", return_value="/x/chrome"), \
             mock.patch("kedaibiao.browser.capture.Browser", return_value=fake):
            with self.assertRaises(CaptureError) as cm:
                capture_cookies(url="https://x.com/", profile="/tmp/p", wait_seconds=0)
        self.assertEqual(cm.exception.kind, "no_cookies")
        self.assertIn("没有登录过", str(cm.exception))

    def test_missing_required_cookie_is_error(self) -> None:
        fake = mock.MagicMock()
        fake.port = 1
        fake.cookies.return_value = [Cookie(name="other", value="1", domain="x.com")]
        fake.__enter__ = lambda self: self
        fake.__exit__ = lambda *args: None
        with mock.patch("kedaibiao.browser.capture.find_browser", return_value="/x/chrome"), \
             mock.patch("kedaibiao.browser.capture.Browser", return_value=fake):
            with self.assertRaises(CaptureError) as cm:
                capture_cookies(url="https://x.com/", profile="/tmp/p", wait_seconds=0,
                                required=("csrfToken",))
        self.assertEqual(cm.exception.kind, "missing_cookie")

    def test_success_returns_header(self) -> None:
        fake = mock.MagicMock()
        fake.port = 1
        fake.cookies.return_value = [
            Cookie(name="csrfToken", value="T", domain=".x.com"),
            Cookie(name="x-token", value="U", domain=".x.com"),
        ]
        fake.__enter__ = lambda self: self
        fake.__exit__ = lambda *args: None
        with mock.patch("kedaibiao.browser.capture.find_browser", return_value="/x/chrome"), \
             mock.patch("kedaibiao.browser.capture.Browser", return_value=fake):
            result = capture_cookies(url="https://x.com/", profile="/tmp/p",
                                     wait_seconds=0, required=("csrfToken",))
        self.assertIn("csrfToken=T", result.cookie_header)
        self.assertIn("x-token=U", result.cookie_header)
        self.assertIn("关键项齐全", result.summary(required=("csrfToken",)))


class TestFindBrowser(unittest.TestCase):
    def test_finds_something_or_none(self) -> None:
        found = find_browser()
        self.assertTrue(found is None or Path(found).exists(), found)


class TestPincoSessionImport(unittest.TestCase):
    """pinco 必须能优先用整串 cookie 恢复会话（业务接口唯一走得通的路）。"""

    def _adapter(self):
        from kedaibiao.net.http import HttpClient, UrllibTransport
        from kedaibiao.platforms.base import AdapterContext
        from kedaibiao.platforms.pinco.adapter import PincoAdapter

        return PincoAdapter(AdapterContext(http=HttpClient(UrllibTransport())))

    def test_import_from_cookie_header(self) -> None:
        adapter = self._adapter()
        ok = adapter.import_session({
            "username": "stu",
            "cookie_header": "csrfToken=T; x-token=U",
        })
        self.assertTrue(ok)
        self.assertTrue(adapter._session.logged_in)  # noqa: SLF001
        self.assertEqual(adapter._session.storage.get("csrfToken"), "T")  # noqa: SLF001

    def test_import_rejects_header_without_csrf(self) -> None:
        adapter = self._adapter()
        # 没有 csrfToken 的 cookie 串会被 adopt_cookies 拒绝，然后退回 storage 路径
        ok = adapter.import_session({"cookie_header": "foo=bar"})
        self.assertFalse(ok)

    def test_export_includes_cookie_header(self) -> None:
        adapter = self._adapter()
        adapter.import_session({"username": "stu", "cookie_header": "csrfToken=T; x-token=U"})
        exported = adapter.export_session()
        assert exported is not None
        self.assertIn("csrfToken=T", exported["cookie_header"])
        self.assertIn("x-token=U", exported["cookie_header"])


if __name__ == "__main__":
    unittest.main()
