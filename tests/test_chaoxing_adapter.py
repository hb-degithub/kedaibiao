"""超星/学习通适配器测试（离线：假传输层，不联网）。"""

from __future__ import annotations

import asyncio
import http.cookiejar
import os
import unittest
from typing import Any

from kedaibiao.core.models import Answer, Credentials
from kedaibiao.net.http import HttpClient, Response
from kedaibiao.platforms.base import AdapterContext, NotSupportedError
from kedaibiao.platforms.chaoxing import (
    ChaoxingAdapter,
    ChaoxingLoginError,
    endpoints,
    parsing,
)

COURSES_HTML = (
    '<div><a href="/mycourse/stu?courseid=111&clazzid=222&enc=e&cpi=333">'
    "<span>线性代数</span></a>"
    '<a href="/mycourse/stu?courseid=444&clazzid=555&enc=f&cpi=666">'
    "<span>大学英语</span></a></div>"
)
WORKS_HTML = (
    "<ul><li><div><a href=\"https://x/work/doHomeWorkNew?workId=9\">第一章作业</a></div>"
    "<p>2026-12-01 23:59</p><p>未交</p></li>"
    "<li><div><a href=\"https://x/exam/test?examId=3\">期中考试</a></div>"
    "<p>2026-10-20 14:00</p></li></ul>"
)
LOGIN_HTML = "<html><head><title>用户登录</title></head><body>请输入密码</body></html>"


class FakeTransport:
    """按 URL 子串匹配返回固定响应；记录请求方法。"""

    def __init__(self, routes: dict[str, tuple[int, str]] | None = None) -> None:
        self.routes = dict(routes or {})
        self.requests: list[tuple[str, str]] = []
        self.cookies = http.cookiejar.CookieJar()

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        timeout: float = 15.0,
    ) -> Response:
        self.requests.append((method, url))
        for key, (status, text) in self.routes.items():
            if key in url:
                return Response(status=status, url=url, body=text.encode("utf-8"))
        return Response(status=404, url=url, body=b"not found")


class SequencedTransport(FakeTransport):
    """同一个 URL 依次返回不同响应（模拟「探会话时还有效、取列表时已过期」）。"""

    def __init__(self, sequence: list[tuple[int, str]]) -> None:
        super().__init__()
        self.sequence = list(sequence)

    async def request(self, method: str, url: str, **kw: Any) -> Response:
        self.requests.append((method, url))
        status, text = self.sequence.pop(0) if self.sequence else (404, "no more")
        return Response(status=status, url=url, body=text.encode("utf-8"))


def make_adapter(transport: FakeTransport) -> ChaoxingAdapter:
    ctx = AdapterContext(http=HttpClient(transport))
    return ChaoxingAdapter(ctx)


class TestCapabilities(unittest.TestCase):
    def test_read_only_flags(self) -> None:
        caps = ChaoxingAdapter.capabilities
        self.assertTrue(caps.login)
        self.assertTrue(caps.list_courses)
        self.assertTrue(caps.list_assignments)
        self.assertFalse(caps.fetch_questions)
        self.assertFalse(caps.submit)
        self.assertFalse(caps.course_playback)

    def test_name_and_display(self) -> None:
        self.assertEqual(ChaoxingAdapter.name, "chaoxing")
        self.assertEqual(ChaoxingAdapter.display_name, "超星学习通")

    def test_registered_by_auto_discovery(self) -> None:
        from kedaibiao import adapters
        from kedaibiao.platforms import load_builtin_adapters

        load_builtin_adapters()
        self.assertIn("chaoxing", adapters.names())

    def test_generic_console_actions_exist(self) -> None:
        from kedaibiao.api import actions

        actions.ensure_generic_actions("chaoxing")
        self.assertTrue(actions.has_action("chaoxing", "courses"))
        self.assertTrue(actions.has_action("chaoxing", "assignments"))


class TestLogin(unittest.TestCase):
    def setUp(self) -> None:
        os.environ.pop("KEDAIBIAO_CHAOXING_COOKIE", None)

    def tearDown(self) -> None:
        os.environ.pop("KEDAIBIAO_CHAOXING_COOKIE", None)

    def test_cookie_in_password_is_adopted(self) -> None:
        adapter = make_adapter(FakeTransport({endpoints.COURSE_LIST_URLS[0]: (200, COURSES_HTML)}))

        ok = asyncio.run(adapter.login(Credentials(username="s1", password="fid=1; _uid=2")))

        self.assertTrue(ok)
        self.assertTrue(asyncio.run(adapter.is_logged_in()))

    def test_cookie_in_extra(self) -> None:
        adapter = make_adapter(FakeTransport({endpoints.COURSE_LIST_URLS[0]: (200, COURSES_HTML)}))
        creds = Credentials(username="s1", extra={"cookie": "fid=9; _uid=8"})

        self.assertTrue(asyncio.run(adapter.login(creds)))

    def test_cookie_from_env(self) -> None:
        os.environ["KEDAIBIAO_CHAOXING_COOKIE"] = "fid=7; _uid=6"
        adapter = make_adapter(FakeTransport({endpoints.COURSE_LIST_URLS[0]: (200, COURSES_HTML)}))

        self.assertTrue(asyncio.run(adapter.login(Credentials(username="s1"))))

    def test_plain_password_goes_through_browser_login(self) -> None:
        """账密登录：不再是「必须贴 Cookie」，而是走浏览器的手机号+密码登录。

        这里不联网：把 ``_login_with_password`` 换掉，只验证分支走对了。
        """
        adapter = make_adapter(FakeTransport())
        calls: list[tuple[str, str]] = []

        async def fake_login(username: str, password: str) -> bool:
            calls.append((username, password))
            return True

        adapter._login_with_password = fake_login  # type: ignore[method-assign]
        ok = asyncio.run(adapter.login(Credentials(username="s1", password="hunter2")))
        self.assertTrue(ok)
        self.assertEqual(calls, [("s1", "hunter2")])

    def test_no_credentials_still_asks_for_cookie(self) -> None:
        adapter = make_adapter(FakeTransport())
        with self.assertRaises(ChaoxingLoginError) as ctx:
            asyncio.run(adapter.login(Credentials(username="s1", password="")))
        self.assertIn("Cookie", str(ctx.exception))

    def test_login_page_means_false(self) -> None:
        adapter = make_adapter(FakeTransport({endpoints.COURSE_LIST_URLS[0]: (200, LOGIN_HTML)}))

        ok = asyncio.run(adapter.login(Credentials(username="s1", password="fid=1; _uid=2")))

        self.assertFalse(ok)
        self.assertFalse(asyncio.run(adapter.is_logged_in()))

    def test_network_failure_is_explicit(self) -> None:
        class DownTransport(FakeTransport):
            async def request(self, method: str, url: str, **kw: Any) -> Response:
                raise OSError("connection refused")

        adapter = make_adapter(DownTransport())
        with self.assertRaises(ChaoxingLoginError) as ctx:
            asyncio.run(adapter.login(Credentials(username="s1", password="fid=1; _uid=2")))
        self.assertIn("连不上", str(ctx.exception))

    def test_http_error_means_not_logged_in(self) -> None:
        adapter = make_adapter(FakeTransport({endpoints.COURSE_LIST_URLS[0]: (403, "forbidden")}))

        ok = asyncio.run(adapter.login(Credentials(username="s1", password="fid=1; _uid=2")))

        self.assertFalse(ok)


class TestReadOnlyData(unittest.TestCase):
    def setUp(self) -> None:
        os.environ.pop("KEDAIBIAO_CHAOXING_COOKIE", None)

    def test_list_courses(self) -> None:
        transport = FakeTransport({endpoints.COURSE_LIST_URLS[0]: (200, COURSES_HTML)})
        adapter = make_adapter(transport)
        asyncio.run(adapter.login(Credentials(username="s1", password="fid=1; _uid=2")))

        courses = asyncio.run(adapter.list_courses())

        self.assertEqual([c.name for c in courses], ["线性代数", "大学英语"])
        self.assertTrue(all(method == "GET" for method, _ in transport.requests))

    def test_list_assignments(self) -> None:
        transport = FakeTransport({
            endpoints.COURSE_LIST_URLS[0]: (200, COURSES_HTML),
            "work/list": (200, WORKS_HTML),
        })
        adapter = make_adapter(transport)
        asyncio.run(adapter.login(Credentials(username="s1", password="fid=1; _uid=2")))
        course = parsing.parse_courses(COURSES_HTML)[0]

        works = asyncio.run(adapter.list_assignments(course))

        self.assertEqual([w.title for w in works], ["第一章作业", "期中考试"])
        self.assertTrue(all(method == "GET" for method, _ in transport.requests))

    def test_expired_session_raises_clear_error(self) -> None:
        transport = SequencedTransport([(200, COURSES_HTML), (200, LOGIN_HTML)])
        adapter = make_adapter(transport)
        asyncio.run(adapter.login(Credentials(username="s1", password="fid=1; _uid=2")))

        with self.assertRaises(ChaoxingLoginError) as ctx:
            asyncio.run(adapter.list_courses())
        self.assertIn("失效", str(ctx.exception))

    def test_submit_is_not_supported(self) -> None:
        adapter = make_adapter(FakeTransport())
        with self.assertRaises(NotSupportedError):
            asyncio.run(adapter.submit(None, [Answer(question=None)]))  # type: ignore[arg-type]

    def test_no_write_requests_are_made(self) -> None:
        """整条链路只应有 GET —— 这是只读承诺的机械检查。"""
        transport = FakeTransport({
            endpoints.COURSE_LIST_URLS[0]: (200, COURSES_HTML),
            "work/list": (200, WORKS_HTML),
        })
        adapter = make_adapter(transport)
        asyncio.run(adapter.login(Credentials(username="s1", password="fid=1; _uid=2")))
        asyncio.run(adapter.list_courses())
        course = parsing.parse_courses(COURSES_HTML)[0]
        asyncio.run(adapter.list_assignments(course))

        self.assertTrue(transport.requests)
        self.assertEqual({method for method, _ in transport.requests}, {"GET"})


class TestSessionPersistence(unittest.TestCase):
    def test_export_import_roundtrip(self) -> None:
        transport = FakeTransport({endpoints.COURSE_LIST_URLS[0]: (200, COURSES_HTML)})
        adapter = make_adapter(transport)
        asyncio.run(adapter.login(Credentials(username="s1", password="fid=1; _uid=2")))

        exported = adapter.export_session()
        self.assertIsNotNone(exported)
        self.assertIn("fid=1", exported["cookie_header"])
        self.assertEqual(exported["username"], "s1")

        second = make_adapter(transport)
        self.assertTrue(second.import_session(exported))
        self.assertIsNotNone(second.export_session())

    def test_import_empty_returns_false(self) -> None:
        adapter = make_adapter(FakeTransport())
        self.assertFalse(adapter.import_session({}))
        self.assertIsNone(adapter.export_session())


class TestHelpers(unittest.TestCase):
    def test_looks_like_cookie_header(self) -> None:
        from kedaibiao.platforms.chaoxing.adapter import looks_like_cookie_header

        self.assertTrue(looks_like_cookie_header("fid=1; _uid=2"))
        self.assertTrue(looks_like_cookie_header("_uid=2"))
        self.assertFalse(looks_like_cookie_header("hunter2"))
        self.assertFalse(looks_like_cookie_header(""))


if __name__ == "__main__":
    unittest.main()
