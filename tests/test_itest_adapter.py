"""iTEST 适配器测试（完全离线，假传输层）。

重点不是「能不能连上平台」，而是：
- 登录链路的两步共用一个 cookie 会话（规格要求）；
- 考试列表解析对字段名差异有容错，但不瞎猜；
- judgeEntry 只签发令牌，不做任何写操作；
- 能力声明如实反映「不取卷、不交卷」。
"""

from __future__ import annotations

import json
import unittest

from kedaibiao.core.models import Credentials
from kedaibiao.net.http import HttpClient, Response
from kedaibiao.platforms.base import AdapterContext, Capabilities
from kedaibiao.platforms.itest.adapter import ItestAdapter
from kedaibiao.platforms.itest.client import (
    ItestAuthError,
    ItestError,
    parse_exam_list,
)


class FakeTransport:
    """按 URL 后缀匹配回包，并记录每次请求。"""

    def __init__(self, routes: dict[str, Response]) -> None:
        self.routes = routes
        self.calls: list[dict] = []
        self.cookies = None  # 触发「无 cookie 罐」分支

    async def request(self, method, url, *, headers=None, body=None, timeout=15.0):
        self.calls.append({"method": method, "url": url, "headers": headers or {},
                           "body": body})
        for suffix, response in self.routes.items():
            if suffix in url:
                return response
        return Response(status=404, url=url, body=b"{}")


def _json(payload, status: int = 200, url: str = "") -> Response:
    return Response(status=status, url=url, body=json.dumps(payload).encode())


def make_adapter(routes: dict[str, Response]) -> tuple[ItestAdapter, FakeTransport]:
    transport = FakeTransport(routes)
    ctx = AdapterContext(http=HttpClient(transport))
    return ItestAdapter(ctx), transport


class TestParseExamList(unittest.TestCase):
    def test_standard_shape(self) -> None:
        payload = {"code": 1, "rs": {"data": [
            {"ksdId": "1001", "examName": "期中测试", "beginTime": "2026-10-20 09:00:00",
             "endTime": "2026-10-20 11:00:00", "limitTime": "120"},
        ]}}
        exams = parse_exam_list(payload)
        self.assertEqual(len(exams), 1)
        self.assertEqual(exams[0].exam_id, "1001")
        self.assertEqual(exams[0].title, "期中测试")
        self.assertEqual(exams[0].duration, "120 分钟", "时长带上单位，别只给个裸数字")

    def test_alternative_field_names(self) -> None:
        """平台版本之间字段名有差异，按已知别名取，取不到留空。"""
        payload = {"rs": {"data": [{"examId": "2", "name": "小测"}]}}
        exam = parse_exam_list(payload)[0]
        self.assertEqual(exam.exam_id, "2")
        self.assertEqual(exam.title, "小测")
        self.assertEqual(exam.start_at, "")

    def test_rows_without_id_skipped(self) -> None:
        payload = {"rs": {"data": [{"examName": "无 id"}, {"ksdId": "3"}]}}
        self.assertEqual([e.exam_id for e in parse_exam_list(payload)], ["3"])

    def test_rs_list_shape(self) -> None:
        payload = {"rs": [{"ksdId": "9"}]}
        self.assertEqual(parse_exam_list(payload)[0].exam_id, "9")

    def test_missing_data_raises(self) -> None:
        with self.assertRaises(ItestError):
            parse_exam_list({"code": 1})

    def test_non_dict_raises(self) -> None:
        with self.assertRaises(ItestError):
            parse_exam_list([])  # type: ignore[arg-type]


class TestCapabilities(unittest.TestCase):
    def test_declares_no_questions_and_no_submit(self) -> None:
        caps = ItestAdapter.capabilities
        self.assertFalse(caps.fetch_questions, "取卷要进答题窗口并开始计时，不做")
        self.assertFalse(caps.submit, "交卷是不可逆写操作，不做")
        self.assertFalse(caps.course_playback, "考试平台没有刷课")
        self.assertTrue(caps.login)

    def test_is_registered_platform(self) -> None:
        self.assertEqual(ItestAdapter.name, "itest")
        self.assertTrue(ItestAdapter.display_name)


class TestLogin(unittest.IsolatedAsyncioTestCase):
    async def test_requires_password(self) -> None:
        adapter, _ = make_adapter({})
        with self.assertRaises(ItestAuthError) as cm:
            await adapter.login(Credentials(username="u", password=""))
        self.assertIn("密码", str(cm.exception))

    async def test_service_missing_from_redirect(self) -> None:
        adapter, _ = make_adapter({
            "/utest/itest/login": Response(status=200, url="https://itestcloud.unipus.cn/utest/itest/login",
                                           body=b"<html></html>"),
        })
        with self.assertRaises(ItestAuthError) as cm:
            await adapter.login(Credentials(username="u", password="p"))
        self.assertIn("SSO", str(cm.exception))

    async def test_sso_rejection_surfaces_code(self) -> None:
        adapter, transport = make_adapter({
            "/utest/itest/login": Response(
                status=200,
                url="https://itestcloud.unipus.cn/utest/itest/login?service=https%3A%2F%2Fssotest",
                body=b"",
            ),
            "/sso/0.1/sso/cip/login": _json({"code": "2101", "msg": "密码错误"}),
        })
        with self.assertRaises(ItestAuthError) as cm:
            await adapter.login(Credentials(username="u", password="p"))
        self.assertIn("2101", str(cm.exception))

        # 关键：用户名与密码必须是加密后提交的（明文不能出现在请求体里）
        sso_call = next(c for c in transport.calls if "cip/login" in c["url"])
        body = json.loads(sso_call["body"].decode())
        self.assertNotIn("u", body["username"])
        self.assertNotIn('"p"', json.dumps(body))
        self.assertIn("service", body)

    async def test_success_flow_uses_same_session(self) -> None:
        adapter, transport = make_adapter({
            "/utest/itest/login": Response(
                status=200,
                url="https://itestcloud.unipus.cn/utest/itest/login?service=SVC",
                body=b"",
            ),
            "/sso/0.1/sso/cip/login": _json({"code": "0", "rs": {"serviceTicket": "T-1"}}),
            "/utest/itest/s/exam": Response(status=200, url="https://itestcloud.unipus.cn/utest/itest/s/exam"),
        })
        # 无 cookie 罐时 is_logged_in 视为 False，但登录流程本身不应该抛错
        ok = await adapter.login(Credentials(username="u", password="p"))
        self.assertFalse(ok, "假传输没有 cookie 罐，登不上是预期")

        # ticket 要带回去
        assert any("ticket=T-1" in c["url"] for c in transport.calls), "必须带 ticket 回登录页"


class TestExamOperations(unittest.IsolatedAsyncioTestCase):
    async def test_list_exams(self) -> None:
        adapter, _ = make_adapter({
            "/student/exam/list": _json({"code": 1, "rs": {"data": [
                {"ksdId": "1001", "examName": "期中测试"},
            ]}}),
        })
        exams = await adapter.client().list_exams()
        self.assertEqual(exams[0].title, "期中测试")

    async def test_list_exams_rejected_when_not_logged_in(self) -> None:
        adapter, _ = make_adapter({
            "/student/exam/list": _json({"code": 401, "msg": "未登录"}),
        })
        with self.assertRaises(ItestAuthError):
            await adapter.client().list_exams()

    async def test_judge_entry_returns_window_url(self) -> None:
        adapter, transport = make_adapter({
            "/exam/judgeEntry": _json({"code": 1, "data": {"url": "https://x/answer?token=JWT"}}),
        })
        url = await adapter.answer_window_url("1001")
        self.assertIn("token=JWT", url)
        self.assertIn("returnUrl=", url, "缺 returnUrl 平台会报没有权限")
        call = next(c for c in transport.calls if "judgeEntry" in c["url"])
        self.assertIn("examId=1001", call["body"].decode())

    async def test_judge_entry_failure(self) -> None:
        adapter, _ = make_adapter({
            "/exam/judgeEntry": _json({"code": 0, "msg": "考试未开始"}),
        })
        with self.assertRaises(ItestError) as cm:
            await adapter.answer_window_url("1001")
        self.assertIn("考试未开始", str(cm.exception))

    async def test_assignments_are_exam_kind_with_deadline(self) -> None:
        adapter, _ = make_adapter({
            "/student/exam/list": _json({"code": 1, "rs": {"data": [
                {"ksdId": "7", "examName": "期末", "endTime": "2026-12-31 17:00:00",
                 "beginTime": "2026-12-31 09:00:00"},
            ]}}),
        })
        from kedaibiao.core.models import TaskKind

        course = (await adapter.list_courses())[0]
        self.assertTrue(course.raw.get("synthetic"))
        items = await adapter.list_assignments(course)
        self.assertEqual(len(items), 1)
        self.assertIs(items[0].kind, TaskKind.EXAM)
        self.assertIsNotNone(items[0].deadline)
        self.assertEqual(items[0].remote_id, "7")

    async def test_fetch_questions_not_supported(self) -> None:
        from kedaibiao.core.models import Assignment
        from kedaibiao.platforms.base import NotSupportedError

        adapter, _ = make_adapter({})
        with self.assertRaises(NotSupportedError):
            await adapter.fetch_questions(Assignment(remote_id="7", title="t"))

    async def test_submit_not_supported(self) -> None:
        from kedaibiao.core.models import Assignment
        from kedaibiao.platforms.base import NotSupportedError

        adapter, _ = make_adapter({})
        with self.assertRaises(NotSupportedError):
            await adapter.submit(Assignment(remote_id="7", title="t"), [])


if __name__ == "__main__":
    unittest.main()


class TestExamListFormatting(unittest.TestCase):
    """考试列表的字段规范化。

    回归背景：平台的 `beginTime` 是 13 位毫秒时间戳，原先原样透传，
    控制台就显示成「开始 1792354500000」—— 对用户毫无意义。
    """

    def test_ms_timestamp_formatted(self) -> None:
        from kedaibiao.platforms.itest.client import _format_ms

        self.assertRegex(_format_ms(1792354500000), r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")

    def test_empty_stays_empty(self) -> None:
        from kedaibiao.platforms.itest.client import _format_ms

        self.assertEqual(_format_ms(None), "")
        self.assertEqual(_format_ms(""), "")

    def test_non_numeric_passthrough(self) -> None:
        from kedaibiao.platforms.itest.client import _format_ms

        self.assertEqual(_format_ms("2026-10-19 04:15"), "2026-10-19 04:15")

    def test_parse_uses_restrict_window_and_finish(self) -> None:
        payload = {"rs": {"data": [{
            "ksdId": "1", "ksName": "卷子",
            "restrictBeginTime": 1792354500000,
            "restrictEndTime": 1794844500000,
            "ksLimit": 999, "finish": 1,
        }]}}
        exam = parse_exam_list(payload)[0]
        self.assertRegex(exam.start_at, r"^\d{4}-\d{2}-\d{2}")
        self.assertRegex(exam.end_at, r"^\d{4}-\d{2}-\d{2}")
        self.assertEqual(exam.duration, "999 分钟")
        self.assertTrue(exam.finished, "finish=1 应识别为已完成")

    def test_unfinished_exam(self) -> None:
        payload = {"rs": {"data": [{"ksdId": "2", "finish": 0}]}}
        self.assertFalse(parse_exam_list(payload)[0].finished)
