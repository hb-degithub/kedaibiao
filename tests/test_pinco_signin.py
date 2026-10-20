"""品课「课堂签到」测试 —— 完全离线。

覆盖三层：

1. **解析**：整条签到链接 / 含链接的文本 / 裸码 → ``attendanceCode``；
   空值、缺参数、带空白、超长都抛 ``ValueError``；
2. **请求**：默认干跑不发请求；``dry_run=False`` 时发
   ``PostV1TeachingAttendanceAttend {attendanceKey: <码>}`` 且显式
   ``dangerous=True``；平台业务拒绝（7207/7208/10419）作为结果返回而不是异常，
   鉴权错误照常上抛；
3. **控制台动作**：``signin`` 已登记为写操作，``code`` 必填、``confirm`` 默认
   干跑；结果信封与错误边界。

证据边界：字段名与 7207 是真实会话实测（空 body → 401「attendanceKey 不能为null」，
随便填码 → 7207），成功路径来自学生端 H5 静态代码，这里只验证我们自己代码的
行为，不声称线上已签到成功。
"""

from __future__ import annotations

import unittest
from typing import Any

from kedaibiao.api import actions
from kedaibiao.api.actions import ActionInputError, load_builtin_actions
from kedaibiao.platforms.pinco import attendance
from kedaibiao.platforms.pinco import endpoints as ep
from kedaibiao.platforms.pinco.errors import PincoApiError, PincoNotLoggedIn

CODE = "abc123def456"


def _run(action: str, adapter: Any, params: dict[str, Any] | None = None) -> Any:
    return actions.get_handler("pinco", action)(adapter, params or {})


def _fields(envelope: dict[str, Any]) -> dict[str, str]:
    return {item["label"]: item["value"] for item in envelope["fields"]}


def _sections(envelope: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["label"]: item for item in envelope["sections"]}


class FakeClient:
    """记录请求的假客户端；``error`` 非空时按平台错误抛出。"""

    def __init__(self, error: BaseException | None = None) -> None:
        self.calls: list[tuple[str, Any, bool]] = []
        self.error = error

    async def post_action(
        self, action: str, payload: dict[str, Any] | None = None, *, dangerous: bool = False
    ) -> Any:
        self.calls.append((action, payload, dangerous))
        if self.error is not None:
            raise self.error
        return {"ok": True}


class FakeSignInAdapter:
    """假适配器：只实现 ``sign_in``，内部走真实的 attendance 模块。"""

    name = "pinco"
    display_name = "希沃品课"

    def __init__(self, error: BaseException | None = None) -> None:
        self.client = FakeClient(error=error)
        self.calls: list[tuple[str, bool]] = []

    async def sign_in(
        self, raw_code: str, *, dry_run: bool = True
    ) -> attendance.SignInOutcome:
        self.calls.append((raw_code, dry_run))
        return await attendance.sign_in(self.client, raw_code, dry_run=dry_run)


class TestExtractCode(unittest.TestCase):
    def test_raw_code(self) -> None:
        self.assertEqual(attendance.extract_attendance_code(CODE), CODE)

    def test_full_signin_url(self) -> None:
        url = f"https://pinco.seewo.com/student/signin?attendanceCode={CODE}"
        self.assertEqual(attendance.extract_attendance_code(url), CODE)

    def test_url_with_extra_params_and_fragment(self) -> None:
        url = (
            "https://pinco.seewo.com/student/signin?from=app"
            f"&attendanceCode={CODE}&x=1#frag"
        )
        self.assertEqual(attendance.extract_attendance_code(url), CODE)

    def test_pasted_text_around_link(self) -> None:
        text = f"点这里签到 https://pinco.seewo.com/student/signin?attendanceCode={CODE} 谢谢"
        self.assertEqual(attendance.extract_attendance_code(text), CODE)

    def test_percent_encoded_value_is_decoded(self) -> None:
        url = "https://pinco.seewo.com/student/signin?attendanceCode=a%2Bb%3Dc"
        self.assertEqual(attendance.extract_attendance_code(url), "a+b=c")

    def test_empty_is_error(self) -> None:
        for raw in ("", "   ", None):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                attendance.extract_attendance_code(raw)  # type: ignore[arg-type]

    def test_url_without_param_is_error(self) -> None:
        with self.assertRaises(ValueError):
            attendance.extract_attendance_code("https://pinco.seewo.com/student/workbench")

    def test_blank_param_is_error(self) -> None:
        with self.assertRaises(ValueError):
            attendance.extract_attendance_code(
                "https://pinco.seewo.com/student/signin?attendanceCode="
            )

    def test_whitespace_inside_code_is_error(self) -> None:
        with self.assertRaises(ValueError):
            attendance.extract_attendance_code("abc 123")

    def test_overlong_code_is_error(self) -> None:
        with self.assertRaises(ValueError):
            attendance.extract_attendance_code("x" * (attendance.MAX_CODE_LENGTH + 1))


class TestMask(unittest.TestCase):
    def test_short_code_is_all_stars(self) -> None:
        self.assertEqual(attendance.mask_attendance_code("ab"), "**")

    def test_long_code_keeps_edges(self) -> None:
        masked = attendance.mask_attendance_code(CODE)
        self.assertTrue(masked.startswith("ab"))
        self.assertTrue(masked.endswith("56"))
        self.assertNotIn(CODE, masked)


class TestSignInRequest(unittest.IsolatedAsyncioTestCase):
    async def test_dry_run_sends_nothing(self) -> None:
        client = FakeClient()
        outcome = await attendance.sign_in(client, f"https://x/student/signin?attendanceCode={CODE}")
        self.assertTrue(outcome.dry_run)
        self.assertFalse(outcome.sent)
        self.assertFalse(outcome.signed)
        self.assertEqual(client.calls, [])
        self.assertIn("没有发送请求", outcome.summary())

    async def test_live_sends_attendance_key_and_dangerous_flag(self) -> None:
        client = FakeClient()
        outcome = await attendance.sign_in(client, CODE, dry_run=False)
        self.assertTrue(outcome.sent)
        self.assertTrue(outcome.signed)
        self.assertEqual(outcome.message, "签到成功")
        self.assertEqual(
            client.calls,
            [(ep.ACTION_ATTENDANCE_ATTEND, {ep.FIELD_ATTENDANCE_KEY: CODE}, True)],
        )

    async def test_business_rejection_is_result_not_exception(self) -> None:
        error = PincoApiError(
            "PostV1TeachingAttendanceAttend 返回 statusCode=7207：考勤签到码无效", code=7207
        )
        client = FakeClient(error=error)
        outcome = await attendance.sign_in(client, CODE, dry_run=False)
        self.assertTrue(outcome.sent)
        self.assertFalse(outcome.signed)
        self.assertEqual(outcome.error_code, 7207)
        self.assertIn("无效", outcome.message)
        self.assertIn("签到失败", outcome.summary())

    async def test_overdue_and_not_match_have_hints(self) -> None:
        for code, hint in (
            (attendance.ERROR_CODE_OVERDUE, "已结束"),
            (attendance.ERROR_CODE_NOT_MATCH, "不匹配"),
        ):
            with self.subTest(code=code):
                client = FakeClient(error=PincoApiError("拒绝", code=code))
                outcome = await attendance.sign_in(client, CODE, dry_run=False)
                self.assertFalse(outcome.signed)
                self.assertIn(hint, outcome.message)

    async def test_auth_error_still_raises(self) -> None:
        client = FakeClient(error=PincoNotLoggedIn("未登录"))
        with self.assertRaises(PincoNotLoggedIn):
            await attendance.sign_in(client, CODE, dry_run=False)


class TestSignInAction(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        load_builtin_actions()

    def test_registered_as_write_action_with_required_code(self) -> None:
        specs = {spec.name: spec for spec in actions.register_platform("pinco")}
        spec = specs["signin"]
        self.assertTrue(spec.mutates)
        self.assertFalse(spec.builtin)
        self.assertFalse(spec.local)
        params = {param.name: param for param in spec.params}
        self.assertEqual(set(params), {"code", "confirm"})
        self.assertTrue(params["code"].required)
        self.assertFalse(params["confirm"].required)


class TestSignInActionResult(unittest.IsolatedAsyncioTestCase):
    async def test_dry_run_renders_without_sending(self) -> None:
        adapter = FakeSignInAdapter()
        url = f"https://pinco.seewo.com/student/signin?attendanceCode={CODE}"
        out = await _run("signin", adapter, {"code": url})
        self.assertEqual(out["kind"], "detail")
        fields = _fields(out)
        self.assertEqual(fields["模式"], "干跑")
        self.assertEqual(fields["结果"], "未发送（干跑）")
        self.assertIn("没有发送任何请求", out["note"])
        self.assertIn(attendance.mask_attendance_code(CODE), fields["签到码"])
        self.assertEqual(adapter.client.calls, [])
        self.assertEqual(adapter.calls, [(url, True)])

    async def test_confirm_true_sends_and_reports_success(self) -> None:
        adapter = FakeSignInAdapter()
        out = await _run("signin", adapter, {"code": CODE, "confirm": "true"})
        fields = _fields(out)
        self.assertEqual(fields["模式"], "写入")
        self.assertEqual(fields["结果"], "已发送，签到成功")
        self.assertIn("写操作", out["note"])
        self.assertEqual(
            adapter.client.calls,
            [(ep.ACTION_ATTENDANCE_ATTEND, {ep.FIELD_ATTENDANCE_KEY: CODE}, True)],
        )
        self.assertEqual(adapter.calls, [(CODE, False)])

    async def test_confirm_true_with_rejection_shows_reason(self) -> None:
        adapter = FakeSignInAdapter(error=PincoApiError("拒绝", code=7208))
        out = await _run("signin", adapter, {"code": CODE, "confirm": "1"})
        fields = _fields(out)
        self.assertEqual(fields["结果"], "已发送，平台拒绝")
        self.assertEqual(fields["平台错误码"], "7208")
        self.assertIn("已结束", fields["失败原因"])
        self.assertIn("平台拒绝", out["note"])

    async def test_missing_code_is_action_input_error(self) -> None:
        with self.assertRaises(ActionInputError):
            await _run("signin", FakeSignInAdapter(), {})

    async def test_bad_link_is_action_input_error(self) -> None:
        with self.assertRaises(ActionInputError):
            await _run(
                "signin",
                FakeSignInAdapter(),
                {"code": "https://pinco.seewo.com/student/workbench"},
            )

    async def test_mechanism_section_mentions_endpoint_and_boundary(self) -> None:
        out = await _run("signin", FakeSignInAdapter(), {"code": CODE})
        text = _sections(out)["机制与证据边界"]["text"]
        self.assertIn("attendanceCode", text)
        self.assertIn("attendanceKey", text)
        self.assertIn("7207", text)
        self.assertIn("尚未用有效签到码实测", text)
