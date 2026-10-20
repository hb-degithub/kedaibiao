"""东软（neusoft）控制台动作测试 —— 完全离线。

覆盖四块（与 ``test_pinco_actions.py`` 同一套口径）：

1. **注册表**：``resources`` / ``attendance`` 只读，``study`` / ``sign`` 是写操作
   （``mutates=True``）；通用动作仍是 ``builtin``；
2. **信封**：table / detail 的形状与关键字段（控制台只看信封，字段错了没人发现）；
3. **错误边界**：缺参数、课程号找不到 → ``ActionInputError``（服务层 400）；
4. **写操作的闸门**：``study`` 的 ``limit`` 会传到适配器，默认只刷 1 个 ——
   命令行一次误操作不该把整门课刷完。

假适配器只实现动作层用到的那几个方法，不发任何网络请求。
"""

from __future__ import annotations

import unittest

from kedaibiao.api import actions
from kedaibiao.api.actions import ActionInputError, load_builtin_actions
from kedaibiao.core.models import Course
from kedaibiao.platforms.neusoft.adapter import PlaybackReport
from kedaibiao.platforms.neusoft.attendance import Attendance
from kedaibiao.platforms.neusoft.models import Resource

TERM_ID = "900000000000000001"

COURSE = Course(
    remote_id="100000000000000001",
    name="示例课程（一）",
    term="2026 秋",
    raw={"teach_class_id": "100000000000000002", "term_id": TERM_ID},
)

RESOURCES = [
    Resource(res_id="400000000000000001", name="视频一", res_type=1, study_status=0),
    Resource(res_id="400000000000000002", name="文档一", res_type=3, study_status=2),
    Resource(res_id="400000000000000003", name="压缩包", res_type=9, study_status=0),
]

ATTENDANCES = [
    Attendance(attendance_id="a-open", course_name="示例课程（一）", status=1, type=2),
    Attendance(
        attendance_id="a-signed", course_name="示例课程（一）", status=1, type=2, sign_role=4
    ),
    Attendance(
        attendance_id="a-done",
        course_name="示例课程（一）",
        status=2,
        type=1,
        location_type=1,
    ),
]


class FakeAdapter:
    """动作层用到的全部能力，其余一概不实现（越界调用会 AttributeError）。"""

    name = "neusoft"

    def __init__(self) -> None:
        self.play_calls: list[tuple[str, bool, int | None]] = []
        self.sign_calls: list[tuple[str, dict[str, object]]] = []
        self.list_courses_calls = 0

    async def list_courses(self) -> list[Course]:
        self.list_courses_calls += 1
        return [COURSE]

    async def list_course_resources(self, course: Course) -> list[Resource]:
        assert course is COURSE
        return list(RESOURCES)

    async def play_course(
        self, course: Course, *, only_pending: bool = True, limit: int | None = None
    ) -> PlaybackReport:
        self.play_calls.append((course.remote_id, only_pending, limit))
        report = PlaybackReport()
        report.completed = ["400000000000000001"]
        report.failed = ["400000000000000009"]
        return report

    async def list_attendance(self, term_id: str = "") -> list[Attendance]:
        return list(ATTENDANCES)

    async def sign(
        self,
        item: Attendance,
        *,
        latitude: str | None = None,
        longitude: str | None = None,
        address: str = "",
    ) -> bool:
        self.sign_calls.append(
            (item.attendance_id, {"lat": latitude, "lng": longitude, "address": address})
        )
        return True


class ActionRegistryTest(unittest.TestCase):
    def setUp(self) -> None:
        load_builtin_actions()

    def test_registers_readonly_and_mutating(self) -> None:
        specs = {spec.name: spec for spec in actions.register_platform("neusoft")}
        for name in ("resources", "attendance", "study", "sign"):
            self.assertIn(name, specs)
        self.assertFalse(specs["resources"].mutates)
        self.assertFalse(specs["attendance"].mutates)
        self.assertTrue(specs["study"].mutates)
        self.assertTrue(specs["sign"].mutates)

    def test_required_params(self) -> None:
        specs = {spec.name: spec for spec in actions.register_platform("neusoft")}
        required = {p.name for p in specs["resources"].params if p.required}
        self.assertEqual(required, {"course"})
        required = {p.name for p in specs["sign"].params if p.required}
        self.assertEqual(required, {"attendance_id"})


class ResourceActionTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        load_builtin_actions()
        self.adapter = FakeAdapter()

    async def test_table_shape_and_status(self) -> None:
        payload = await actions.get_handler("neusoft", "resources")(
            self.adapter, {"course": COURSE.remote_id}
        )
        self.assertEqual(payload["kind"], "table")
        rows = payload["rows"]
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["status"], "未完成")
        self.assertEqual(rows[1]["status"], "已完成")
        # 不认识的 resType 要显示出来，而不是当作可刷
        self.assertEqual(rows[2]["completable"], "否")
        self.assertIn("待完成 1", payload["title"])

    async def test_lookup_by_course_name(self) -> None:
        payload = await actions.get_handler("neusoft", "resources")(
            self.adapter, {"course": COURSE.name}
        )
        self.assertEqual(payload["kind"], "table")

    async def test_missing_course_raises(self) -> None:
        with self.assertRaises(ActionInputError):
            await actions.get_handler("neusoft", "resources")(self.adapter, {})
        with self.assertRaises(ActionInputError):
            await actions.get_handler("neusoft", "resources")(self.adapter, {"course": "不存在"})


class StudyActionTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        load_builtin_actions()
        self.adapter = FakeAdapter()

    async def test_default_limit_is_one(self) -> None:
        payload = await actions.get_handler("neusoft", "study")(
            self.adapter, {"course": COURSE.remote_id}
        )
        self.assertEqual(self.adapter.play_calls, [(COURSE.remote_id, True, 1)])
        self.assertEqual(payload["kind"], "detail")
        fields = {item["label"]: item["value"] for item in payload["fields"]}
        self.assertEqual(fields["完成"], "1")
        self.assertEqual(fields["失败（回读未完成）"], "1")

    async def test_explicit_limit_is_passed(self) -> None:
        await actions.get_handler("neusoft", "study")(
            self.adapter, {"course": COURSE.remote_id, "limit": "5"}
        )
        self.assertEqual(self.adapter.play_calls, [(COURSE.remote_id, True, 5)])

    async def test_bad_limit_raises(self) -> None:
        for bad in ("abc", "0", "-2"):
            with self.subTest(limit=bad), self.assertRaises(ActionInputError):
                await actions.get_handler("neusoft", "study")(
                    self.adapter, {"course": COURSE.remote_id, "limit": bad}
                )
        self.assertEqual(self.adapter.play_calls, [])


class AttendanceActionTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        load_builtin_actions()
        self.adapter = FakeAdapter()

    async def test_status_column(self) -> None:
        payload = await actions.get_handler("neusoft", "attendance")(self.adapter, {})
        rows = {row["attendance_id"]: row for row in payload["rows"]}
        self.assertEqual(rows["a-open"]["status"], "可签")
        self.assertEqual(rows["a-signed"]["status"], "已签")
        self.assertEqual(rows["a-done"]["status"], "已结束")
        # 扫码场次要标出需定位
        self.assertEqual(rows["a-done"]["location"], "需定位")

    async def test_sign_passes_coordinates(self) -> None:
        payload = await actions.get_handler("neusoft", "sign")(
            self.adapter,
            {
                "attendance_id": "a-open",
                "latitude": "38.9",
                "longitude": "121.6",
                "address": "教学楼",
            },
        )
        self.assertEqual(payload["kind"], "message")
        self.assertEqual(
            self.adapter.sign_calls,
            [("a-open", {"lat": "38.9", "lng": "121.6", "address": "教学楼"})],
        )

    async def test_sign_unknown_id_raises(self) -> None:
        with self.assertRaises(ActionInputError):
            await actions.get_handler("neusoft", "sign")(self.adapter, {"attendance_id": "nope"})
        self.assertEqual(self.adapter.sign_calls, [])

    async def test_sign_missing_id_raises(self) -> None:
        with self.assertRaises(ActionInputError):
            await actions.get_handler("neusoft", "sign")(self.adapter, {})


if __name__ == "__main__":
    unittest.main()
