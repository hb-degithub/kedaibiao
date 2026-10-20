"""东软考勤与签到单测 —— **完全离线**（注入假传输层，不联网）。

这里钉住的都是「错了会静默出事」的点：

1. **``status=null`` 算进行中** —— 新发布的场次服务端就这么返回；只判 ``== 1``
   会整批漏签，而这条在真实列表里不容易一眼看出来；
2. **已签有两个独立判据** —— ``signRole ∈ {3,4}`` **或** ``signTime`` 非空，
   只看一个会重复提交；
3. **三条守卫挡在发请求之前** —— 已签 / 已结束 / 需定位却缺坐标时，假传输层上
   必须一个请求都没有；签到是不可逆写动作，发出去就收不回来；
4. **报文形状** —— 必填字段、定位三字段、扫码 ``refreshSeed`` 的拼接位置。

证据边界：字段名、禁止重复提交、``status=null`` 来自协议档案 §6 的实测记录；
成功签到的真实回执本仓库没有样本，所以这里只验证「我们代码发出的报文与拦截
行为」，不声称线上签到成功。``refreshSeed`` 的确切取值来源仍是 ``[C]``。
"""

from __future__ import annotations

import unittest
from typing import Any

from kedaibiao.platforms.neusoft import attendance
from kedaibiao.platforms.neusoft import endpoints as ep
from kedaibiao.platforms.neusoft.errors import NeusoftError
from tests.neusoft_support import FakeTransport, make_client

#: 测试里统一的学生 ID（与 ``make_client`` 的默认 user_id 一致）。
STUDENT_ID = "1000000000000000001"
ATTENDANCE_ID = "1900000000000000001"
DETAIL_ID = "601"

#: 一份真实形状的考勤行：数字型的 ``id``、``null`` 的 ``status`` 与 ``signRole``、
#: 字符串型 ``openTime`` / 数字型 ``finishTime``、以及定位所需的字段。
_SAMPLE_ROW: dict[str, Any] = {
    "attendanceId": ATTENDANCE_ID,
    "id": 601,
    "title": "二维码考勤 2026-09-23",
    "courseName": "大学英语Ⅰ（三）",
    "type": 1,
    "status": None,
    "openTime": "1790210019000",
    "finishTime": 1790210319000,
    "signRole": None,
    "signTime": None,
    "signUserId": None,
    "attendanceLocationType": 1,
    "longitude": 121.5426,
    "latitude": "38.8977",
    "attendanceRadius": 300,
    "addressName": "辽宁省大连市甘井子区东软A6栋",
}


def _make_att(**overrides: Any) -> attendance.Attendance:
    """造一个默认「进行中、未签、不需要定位」的场次，按需覆盖字段。"""
    fields: dict[str, Any] = {
        "attendance_id": ATTENDANCE_ID,
        "detail_id": DETAIL_ID,
        "status": None,
        "type": 0,
        "sign_role": 0,
        "sign_time": "",
        "location_type": 0,
    }
    fields.update(overrides)
    return attendance.Attendance(**fields)


class ParseAttendanceTest(unittest.IsolatedAsyncioTestCase):
    """列表解析：字段名多、类型脏，解析层要一次收干净。"""

    def test_parses_sample_row(self) -> None:
        items = attendance.parse_attendance({"list": [_SAMPLE_ROW]})

        self.assertEqual(len(items), 1)
        att = items[0]
        self.assertEqual(att.attendance_id, ATTENDANCE_ID)
        self.assertEqual(att.detail_id, DETAIL_ID)  # 数字明细 ID 转成字符串
        self.assertIsNone(att.status, "null 必须保留成 None，不能压成 0")
        self.assertEqual(att.type, 1)
        self.assertEqual(att.location_type, 1)
        self.assertEqual(att.start_time, 1790210019000)
        self.assertEqual(att.end_time, 1790210319000)
        self.assertEqual(att.course_name, "大学英语Ⅰ（三）")
        self.assertEqual(att.sign_role, 0)
        self.assertEqual(att.sign_time, "")
        self.assertEqual(att.raw["title"], "二维码考勤 2026-09-23")

    def test_skips_row_without_attendance_id(self) -> None:
        """没有场次 ID 的行没法签到也没法去重，直接丢掉而不是留个空壳。"""
        rows = [{"id": 1, "courseName": "缺场次 ID"}, {"attendanceId": "2", "id": 2}]
        items = attendance.parse_attendance({"list": rows})
        self.assertEqual([item.attendance_id for item in items], ["2"])

    def test_tolerates_array_and_empty_payloads(self) -> None:
        self.assertEqual(
            [item.attendance_id for item in attendance.parse_attendance([_SAMPLE_ROW])],
            [ATTENDANCE_ID],
        )
        for payload in (None, {}, {"list": None}, {"list": []}, "不是列表"):
            with self.subTest(payload=payload):
                self.assertEqual(attendance.parse_attendance(payload), [])

    def test_zero_finish_time_is_treated_as_missing(self) -> None:
        """``finishTime=0`` 更可能是「没给」，当成有效时间会把场次全判成已结束。"""
        att = attendance.parse_attendance(
            {"list": [{"attendanceId": "3", "finishTime": 0, "openTime": ""}]}
        )[0]
        self.assertIsNone(att.end_time)
        self.assertIsNone(att.start_time)


class ListAttendanceTest(unittest.IsolatedAsyncioTestCase):
    """列表请求体：三个 ``null`` 过滤器是在表达「不过滤」，不能省。"""

    async def test_posts_null_filters_and_parses_list(self) -> None:
        transport = FakeTransport()
        transport.json(
            "POST", "/getAppStuAttendancePage", {"list": [_SAMPLE_ROW], "total": 1}
        )
        client = make_client(transport)

        items = await attendance.list_attendance(client, STUDENT_ID, "2026-2027-1")

        self.assertEqual(len(items), 1)
        self.assertEqual(transport.paths(), [ep.ATT_PAGE])
        self.assertEqual(
            transport.body_of("/getAppStuAttendancePage"),
            {
                "studentId": STUDENT_ID,
                "termId": "2026-2027-1",
                "courseId": None,
                "attendanceStatus": None,
                "status": None,
            },
        )


class IsOpenRuleTest(unittest.IsolatedAsyncioTestCase):
    """§6.1 第 1 条：``status=null`` 也算进行中，然后才看结束时间。"""

    def test_null_status_is_open(self) -> None:
        self.assertTrue(attendance.is_open(_make_att(status=None), now_ms=1_000))

    def test_status_one_is_open(self) -> None:
        self.assertTrue(attendance.is_open(_make_att(status=1), now_ms=1_000))

    def test_other_statuses_are_closed(self) -> None:
        for status in (0, 2, 3):
            with self.subTest(status=status):
                self.assertFalse(attendance.is_open(_make_att(status=status), now_ms=1_000))

    def test_finish_time_in_past_closes_even_when_status_is_one(self) -> None:
        """服务端有时只关窗口不改 status，所以时间检查必须独立生效。"""
        att = _make_att(status=1, end_time=1_500)
        self.assertFalse(attendance.is_open(att, now_ms=1_500))
        self.assertTrue(attendance.is_open(att, now_ms=1_499))

    def test_null_status_still_respects_finish_time(self) -> None:
        att = _make_att(status=None, end_time=1_500)
        self.assertFalse(attendance.is_open(att, now_ms=9_999))


class IsSignedRuleTest(unittest.IsolatedAsyncioTestCase):
    """§6.1 第 2 条：两个判据缺一不可。"""

    def test_signed_by_role(self) -> None:
        for role in (3, 4):
            with self.subTest(role=role):
                self.assertTrue(attendance.is_signed(_make_att(sign_role=role)))

    def test_signed_by_sign_time_without_role(self) -> None:
        """教师那边留下的记录可能 signRole 为空但 signTime 有值。"""
        self.assertTrue(
            attendance.is_signed(_make_att(sign_role=0, sign_time="1790210024000"))
        )

    def test_unsigned_when_both_missing(self) -> None:
        self.assertFalse(attendance.is_signed(_make_att(sign_role=0, sign_time="")))


class DetailIdTest(unittest.IsolatedAsyncioTestCase):
    """明细 ID：数字要转字符串，空串表示「不在该教学班」。"""

    async def test_numeric_detail_id_becomes_string(self) -> None:
        transport = FakeTransport()
        transport.json(
            "GET", f"/getAttendanceDetailId/{ATTENDANCE_ID}/{STUDENT_ID}", 601
        )
        client = make_client(transport)

        found = await attendance.detail_id(client, ATTENDANCE_ID, STUDENT_ID)

        self.assertEqual(found, "601")
        self.assertIsInstance(found, str)
        self.assertEqual(
            transport.paths(),
            [f"{ep.ATT_DETAIL_ID}/{ATTENDANCE_ID}/{STUDENT_ID}"],
        )

    async def test_empty_detail_id_means_not_in_class(self) -> None:
        transport = FakeTransport()
        transport.json(
            "GET", f"/getAttendanceDetailId/{ATTENDANCE_ID}/{STUDENT_ID}", ""
        )
        client = make_client(transport)

        self.assertEqual(await attendance.detail_id(client, ATTENDANCE_ID, STUDENT_ID), "")


class SubmitSignTest(unittest.IsolatedAsyncioTestCase):
    """提交签到：报文形状 + 三条守卫必须挡在请求之前。"""

    async def asyncSetUp(self) -> None:
        self.transport = FakeTransport()
        self.transport.json("PUT", "/update", {"ok": True})
        self.client = make_client(self.transport)

    async def test_body_has_required_fields_and_location_triplet(self) -> None:
        att = _make_att(type=2, location_type=1)
        result = await attendance.submit_sign(
            self.client,
            att,
            detail_id=DETAIL_ID,
            latitude="38.8977",
            longitude="121.5426",
            address="辽宁省大连市甘井子区东软A6栋",
        )

        self.assertEqual(result, {"ok": True})
        self.assertEqual(transport_last_method(self.transport), "PUT")
        self.assertEqual(transport_last_path(self.transport), ep.ATT_UPDATE)
        self.assertEqual(
            self.transport.body_of("/update"),
            {
                "attendanceId": ATTENDANCE_ID,
                "id": DETAIL_ID,
                "status": ep.ATT_STATUS_PRESENT,
                "type": 2,
                "signRole": attendance.SIGN_ROLE_STUDENT,
                "signUserId": STUDENT_ID,
                "signLongitude": "121.5426",
                "signLatitude": "38.8977",
                "signAddressName": "辽宁省大连市甘井子区东软A6栋",
            },
        )

    async def test_coordinates_are_sent_as_strings(self) -> None:
        """平台要求坐标是字符串形态；浮点直接塞进去长度与格式都不稳定。"""
        att = _make_att(location_type=1)
        await attendance.submit_sign(
            self.client, att, detail_id=DETAIL_ID, latitude=38.8977, longitude=121.5426
        )
        body = self.transport.body_of("/update")
        self.assertEqual(body["signLatitude"], "38.8977")
        self.assertEqual(body["signLongitude"], "121.5426")

    async def test_no_location_fields_when_not_required(self) -> None:
        att = _make_att(location_type=0)
        await attendance.submit_sign(
            self.client,
            att,
            detail_id=DETAIL_ID,
            latitude="38.8977",
            longitude="121.5426",
        )
        body = self.transport.body_of("/update")
        self.assertNotIn("signLongitude", body)
        self.assertNotIn("signLatitude", body)
        self.assertNotIn("signAddressName", body)
        self.assertNotIn("refreshSeed", body)

    async def test_scan_type_adds_refresh_seed(self) -> None:
        att = _make_att(type=ep.ATT_TYPE_SCAN)

        await attendance.submit_sign(
            self.client, att, detail_id=DETAIL_ID, refresh_seed="from-qr-code"
        )
        self.assertEqual(
            self.transport.body_of("/update")["refreshSeed"], "from-qr-code"
        )

        # 调用方没给 seed 时用兜底值，保证扫码分支字段齐全。
        await attendance.submit_sign(self.client, att, detail_id=DETAIL_ID)
        self.assertEqual(
            self.transport.body_of("/update")["refreshSeed"],
            attendance.DEFAULT_REFRESH_SEED,
        )

    async def test_signed_guard_blocks_before_request(self) -> None:
        for att in (
            _make_att(sign_role=4),
            _make_att(sign_role=3),
            _make_att(sign_role=0, sign_time="1790210024000"),
        ):
            with self.subTest(att=att):
                self.transport.requests.clear()
                with self.assertRaises(attendance.AttendanceGuardError):
                    await attendance.submit_sign(self.client, att, detail_id=DETAIL_ID)
                self.assertEqual(
                    self.transport.requests, [], "已签时不得发出任何请求"
                )

    async def test_ended_guard_blocks_before_request(self) -> None:
        cases = (
            _make_att(status=2),  # 已结束
            _make_att(status=None, end_time=1),  # 只有结束时间过了
            _make_att(status=1, end_time=1),  # status 没更新但窗口关了
        )
        for att in cases:
            with self.subTest(att=att):
                self.transport.requests.clear()
                with self.assertRaises(attendance.AttendanceGuardError):
                    await attendance.submit_sign(self.client, att, detail_id=DETAIL_ID)
                self.assertEqual(
                    self.transport.requests, [], "已结束时不得发出任何请求"
                )

    async def test_location_guard_blocks_before_request(self) -> None:
        att = _make_att(location_type=1)
        for latitude, longitude in ((None, None), ("", ""), ("38.8977", None)):
            with self.subTest(latitude=latitude, longitude=longitude):
                self.transport.requests.clear()
                with self.assertRaises(attendance.AttendanceGuardError):
                    await attendance.submit_sign(
                        self.client,
                        att,
                        detail_id=DETAIL_ID,
                        latitude=latitude,
                        longitude=longitude,
                    )
                self.assertEqual(
                    self.transport.requests, [], "缺定位坐标时不得发出任何请求"
                )

    async def test_guard_error_message_names_the_reason(self) -> None:
        """被拦住时日志里要能看出「为什么没签」，否则线上只能干瞪眼。"""
        with self.assertRaises(attendance.AttendanceGuardError) as ctx:
            await attendance.submit_sign(
                self.client, _make_att(sign_role=4), detail_id=DETAIL_ID
            )
        self.assertIn("已经签过", str(ctx.exception))
        self.assertTrue(issubclass(attendance.AttendanceGuardError, NeusoftError))


def transport_last_method(transport: FakeTransport) -> str:
    """最后一次请求的方法（断言守卫放行后确实走了 PUT）。"""
    return transport.requests[-1].method


def transport_last_path(transport: FakeTransport) -> str:
    """最后一次请求的路径（不含域名与 query）。"""
    return transport.requests[-1].path
