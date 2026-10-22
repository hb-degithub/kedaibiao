"""东软分页与图片资源的单测 —— 全部离线。

钉住两件 2026-10-22 修掉的事：

1. **列表翻页**：课程 / 学习任务 / 考勤以前只读第一页（100 / 200 条封顶），
   超出的部分静默消失，表现为「没有这门课 / 老师没布置 / 没有考勤」。
   这里覆盖三条终止条件：覆盖 ``total``、短页、服务端忽略 ``pageNo``
   （重复返回同一页时必须停下并告警，不能死循环）；
2. **图片资源**：``res_type=4`` 计入待完成但没有实测完成链路，刷课时必须
   **跳过、不占 limit、不算失败**，单独进 ``report.unsupported``。
"""

from __future__ import annotations

import json
import unittest
from typing import Any
from urllib.parse import parse_qs, urlparse

from kedaibiao.core.models import Course
from kedaibiao.net.http import Response
from kedaibiao.platforms.base import AdapterContext
from kedaibiao.platforms.neusoft import endpoints as ep
from kedaibiao.platforms.neusoft import paging as paging_rules
from kedaibiao.platforms.neusoft.adapter import NeusoftAdapter
from kedaibiao.platforms.neusoft.homework import list_study_tasks
from kedaibiao.platforms.neusoft.attendance import list_attendance
from kedaibiao.platforms.neusoft.resources import list_courses
from tests.neusoft_support import FakeTransport, RecordedRequest, make_client

TERM_ID = "900000000000000001"
SERIES = "1000000000000000"


class PagedTransport(FakeTransport):
    """按 ``pageNo`` 返回不同页的假传输层。

    真实接口 GET 走 query、POST 走 body；这里两种都解析，测试里不必区分。
    未登记的页返回空数组（等价于「没有下一页」）。
    """

    def __init__(self) -> None:
        super().__init__()
        self.pages: dict[tuple[str, str], dict[int, Any]] = {}

    def page(self, method: str, path_suffix: str, page_no: int, payload: Any) -> None:
        self.pages.setdefault((method.upper(), path_suffix), {})[page_no] = payload

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        timeout: float = 15.0,
    ) -> Response:
        raw_body = body.decode("utf-8") if isinstance(body, (bytes, bytearray)) else None
        self.requests.append(RecordedRequest(method.upper(), url, dict(headers or {}), raw_body))
        request = self.requests[-1]

        page_no = 1
        if method.upper() == "GET":
            page_no = int(parse_qs(urlparse(url).query).get("pageNo", ["1"])[0])
        elif raw_body:
            page_no = int(json.loads(raw_body).get("pageNo", 1))

        for (route_method, suffix), pages in self.pages.items():
            if route_method == method.upper() and request.path.endswith(suffix):
                payload = pages.get(page_no, [])
                return Response(
                    status=200,
                    url=url,
                    body=json.dumps(
                        {"code": 0, "msg": "", "data": payload}, ensure_ascii=False
                    ).encode("utf-8"),
                    headers={"content-type": "application/json"},
                )
        return Response(status=404, url=url, body=b"{}")


def _course_row(index: int) -> dict[str, str]:
    return {
        "teachCourseId": f"{SERIES}{index:04d}",
        "teachClassId": f"{SERIES}{index:04d}c",
        "teachCourseName": f"示例课程 {index}",
    }


def _task_row(index: int) -> dict[str, Any]:
    return {
        "ctlId": f"ctl-{index}",
        "resName": f"作业 {index}",
        "type": ep.TASK_KIND_HOMEWORK,
    }


def _attendance_row(index: int) -> dict[str, Any]:
    return {"attendanceId": f"att-{index}", "status": 1, "type": 2}


class ShouldContinueRuleTest(unittest.TestCase):
    """分页终止条件本身是纯函数，先把三条规则钉死。"""

    def test_stops_on_empty_page(self) -> None:
        self.assertFalse(
            paging_rules.should_continue(collected=0, page_count=0, total=None, page_size=100)
        )

    def test_stops_on_short_page_without_total(self) -> None:
        self.assertFalse(
            paging_rules.should_continue(collected=5, page_count=5, total=None, page_size=100)
        )

    def test_continues_on_full_page_without_total(self) -> None:
        self.assertTrue(
            paging_rules.should_continue(collected=100, page_count=100, total=None, page_size=100)
        )

    def test_total_wins_over_page_length(self) -> None:
        self.assertTrue(
            paging_rules.should_continue(collected=100, page_count=20, total=120, page_size=100)
        )
        self.assertFalse(
            paging_rules.should_continue(collected=120, page_count=20, total=120, page_size=100)
        )

    def test_total_of_ignores_garbage(self) -> None:
        self.assertIsNone(paging_rules.total_of({"list": []}))
        self.assertIsNone(paging_rules.total_of({"total": "abc"}))
        self.assertIsNone(paging_rules.total_of({"total": True}))
        self.assertEqual(paging_rules.total_of({"total": 0}), 0)
        self.assertEqual(paging_rules.total_of({"totalCount": "3"}), 3)


class CoursePagingTest(unittest.IsolatedAsyncioTestCase):
    async def test_reads_all_pages(self) -> None:
        transport = PagedTransport()
        transport.page("GET", ep.COURSE_PAGE, 1, {"list": [_course_row(i) for i in range(100)], "total": 102})
        transport.page("GET", ep.COURSE_PAGE, 2, {"list": [_course_row(100), _course_row(101)], "total": 102})

        courses = await list_courses(make_client(transport), TERM_ID)

        self.assertEqual(len(courses), 102)
        self.assertEqual([r.query["pageNo"] for r in transport.requests], ["1", "2"])
        self.assertEqual(
            [c.teach_course_id for c in courses[:2]],
            [f"{SERIES}0000", f"{SERIES}0001"],
        )

    async def test_stops_when_server_ignores_page_no(self) -> None:
        """服务端永远返回同一页时，必须靠「没有新课程」停下，而不是死循环。"""
        transport = PagedTransport()
        same_page = {"list": [_course_row(i) for i in range(100)], "total": 102}
        transport.page("GET", ep.COURSE_PAGE, 1, same_page)
        transport.page("GET", ep.COURSE_PAGE, 2, same_page)

        courses = await list_courses(make_client(transport), TERM_ID)

        self.assertEqual(len(courses), 100)
        self.assertEqual(len(transport.requests), 2, "第二页无新条目就应停止")

    async def test_single_short_page_does_not_loop(self) -> None:
        transport = PagedTransport()
        transport.page("GET", ep.COURSE_PAGE, 1, {"list": [_course_row(0)]})
        courses = await list_courses(make_client(transport), TERM_ID)
        self.assertEqual(len(courses), 1)
        self.assertEqual(len(transport.requests), 1)


class StudyTaskPagingTest(unittest.IsolatedAsyncioTestCase):
    async def test_reads_all_pages_by_total(self) -> None:
        transport = PagedTransport()
        transport.page(
            "POST", ep.STUDY_TASK_PAGE, 1, {"list": [_task_row(1), _task_row(2)], "total": 3}
        )
        transport.page("POST", ep.STUDY_TASK_PAGE, 2, {"list": [_task_row(3)], "total": 3})

        tasks = await list_study_tasks(make_client(transport), TERM_ID)

        self.assertEqual([t.test_id for t in tasks], ["ctl-1", "ctl-2", "ctl-3"])
        bodies = [r.json() for r in transport.requests]
        self.assertEqual([b["pageNo"] for b in bodies], [1, 2])
        self.assertEqual(bodies[0]["pageSize"], paging_rules.TASK_PAGE_SIZE)
        # 致命坑仍在：body 里不能有 currentClassId
        self.assertNotIn("currentClassId", bodies[0])

    async def test_duplicate_page_stops_without_requiring_total(self) -> None:
        transport = PagedTransport()
        page = {"list": [_task_row(1)] * 200}
        transport.page("POST", ep.STUDY_TASK_PAGE, 1, page)
        transport.page("POST", ep.STUDY_TASK_PAGE, 2, page)
        tasks = await list_study_tasks(make_client(transport), TERM_ID)
        self.assertEqual([t.test_id for t in tasks], ["ctl-1"])
        self.assertEqual(len(transport.requests), 2)


class AttendancePagingTest(unittest.IsolatedAsyncioTestCase):
    async def test_first_request_keeps_measured_body_then_pages(self) -> None:
        transport = PagedTransport()
        transport.page("POST", ep.ATT_PAGE, 1, {"list": [_attendance_row(1)], "total": 2})
        transport.page("POST", ep.ATT_PAGE, 2, {"list": [_attendance_row(2)], "total": 2})

        items = await list_attendance(make_client(transport), "1000000000000000001", TERM_ID)

        self.assertEqual([i.attendance_id for i in items], ["att-1", "att-2"])
        first = transport.requests[0].json()
        self.assertNotIn("pageNo", first, "第一页必须保持档案 §6 的实测报文形态")
        self.assertNotIn("pageSize", first)
        second = transport.requests[1].json()
        self.assertEqual(second["pageNo"], 2)
        self.assertEqual(second["pageSize"], paging_rules.ATTENDANCE_PAGE_SIZE)

    async def test_without_total_stays_single_page(self) -> None:
        """响应没有 total 时无法判断还有没有下一页，保持旧行为（只读第一页）。"""
        transport = PagedTransport()
        transport.page("POST", ep.ATT_PAGE, 1, {"list": [_attendance_row(1)]})

        items = await list_attendance(make_client(transport), "1000000000000000001", TERM_ID)

        self.assertEqual(len(items), 1)
        self.assertEqual(len(transport.requests), 1)

    async def test_ignored_paging_stops_and_warns(self) -> None:
        transport = PagedTransport()
        page = {"list": [_attendance_row(1)], "total": 5}
        transport.page("POST", ep.ATT_PAGE, 1, page)
        transport.page("POST", ep.ATT_PAGE, 2, page)

        with self.assertLogs("kedaibiao.platforms.neusoft.attendance", level="WARNING"):
            items = await list_attendance(make_client(transport), "1000000000000000001", TERM_ID)

        self.assertEqual(len(items), 1)
        self.assertEqual(len(transport.requests), 2)


class ImageResourceTest(unittest.IsolatedAsyncioTestCase):
    """图片：跳过、不占 limit、不算失败，单独记 unsupported。"""

    def _adapter(self) -> NeusoftAdapter:
        transport = FakeTransport()
        transport.json(
            "POST",
            "/listResource",
            {
                "resList": [
                    {"resId": "img-1", "resName": "图片一", "resType": ep.RES_TYPE_IMAGE, "studyStatus": 0},
                    {"resId": "vid-1", "resName": "视频一", "resType": ep.RES_TYPE_VIDEO, "studyStatus": 0},
                ],
                "folderList": [],
            },
        )
        transport.json("POST", "/startStudy", {})
        transport.json("POST", "/studyForAudioOrVideo", {})
        transport.json("POST", "/getRecordInfo", {"studyStatus": 2})
        adapter = NeusoftAdapter(AdapterContext(http=transport, environ={}))
        adapter.client.load_session(make_client(transport).session)
        return adapter

    def _course(self) -> Course:
        return Course(
            remote_id="100000000000000001",
            name="示例课程",
            term="2026 秋",
            raw={
                "teach_course_id": "100000000000000001",
                "teach_class_id": "100000000000000002",
                "term_id": TERM_ID,
            },
        )

    async def test_image_does_not_consume_limit(self) -> None:
        report = await self._adapter().play_course(self._course(), limit=1)

        self.assertEqual(report.completed, ["vid-1"], "limit=1 应该留给真正能完成的视频")
        self.assertEqual(report.unsupported, ["img-1"])
        self.assertEqual(report.failed, [], "图片不是失败")

    async def test_unsupported_is_not_reported_as_failure_age(self) -> None:
        report = await self._adapter().play_course(self._course())
        self.assertTrue(report.ok, "只有 unsupported 时不算失败")
        self.assertEqual(report.unsupported, ["img-1"])


if __name__ == "__main__":
    unittest.main()
