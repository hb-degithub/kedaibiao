"""品课「刷课」翻页上报器测试 —— 完全离线，不联网、不真 sleep。

覆盖三块：

1. **策略**：翻页间隔下限、各种上限/阈值的取值校验；
2. **上报器**：dry-run 不发请求、逐页发 ``maxLook``、达标即停、进度不动就停、
   页数/总时长上限、目标比例换算；
3. **接线**：适配器方法默认 dry-run；live 时先开课件拿页数，再逐页翻，
   报文形如 ``{"coursewareId": <materialId>, "maxPageIndex": N}``。

机制依据（2026-10-20 实测，见 ``docs/verification/pinco-progress.md``）：
材料进度 = ``(maxPageIndex + 1) / 总页数``；翻一页上报一次 ``maxPageIndex``。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import unittest
import urllib.parse

from kedaibiao.core.models import Course
from kedaibiao.net.http import HttpClient, Response
from kedaibiao.platforms.base import AdapterContext, NotSupportedError
from kedaibiao.platforms.pinco import endpoints as ep
from kedaibiao.platforms.pinco.adapter import PincoAdapter
from kedaibiao.platforms.pinco.progress import (
    MIN_PAGE_INTERVAL_SECONDS,
    PAGE_INTERVAL_SECONDS,
    RATIO_EPSILON,
    STOP_DRY_RUN,
    STOP_MAX_PAGES,
    STOP_MAX_SECONDS,
    STOP_RATIO_STALLED,
    STOP_TARGET_REACHED,
    CoursewareProgressReporter,
    ProgressPolicy,
    ProgressReport,
)

COURSE = Course(remote_id="course-1", name="示例课程")
MATERIAL_ID = "1322029806386077696"
RES_ID = "dc8a8e2608804510abe0c5882ea08810"
TOTAL_PAGES = 10

#: 真实材料详情样本（字段形态与线上一致）。
MATERIAL_DETAIL: dict[str, object] = {
    "chapterDirectoryMaterialId": MATERIAL_ID,
    "materialId": "1322029806344134656",
    "materialName": "示例材料-01.pptx",
    "learningRatio": 0,
    "viewed": False,
    "countLearningProgress": True,
    "media": {
        "mediaId": MATERIAL_ID,
        "resourceId": "1322029806344134656",
        "resId": RES_ID,
        "type": 4,
        "fileName": "示例材料-01.pptx",
        "fileSize": 22267856,
    },
}


def ratio_for(page_index: int, total: int = TOTAL_PAGES) -> float:
    """``maxPageIndex`` → 归一化进度；页下标 0 起。"""
    return min((page_index + 1) / total, 1.0)


class StubClient:
    """只记录 ``post_action`` 的假客户端。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def post_action(
        self, action: str, payload: dict[str, object] | None = None, *, dangerous: bool = False
    ) -> dict[str, object]:
        assert dangerous is True, "翻页上报是写操作，必须显式 dangerous=True"
        self.calls.append((action, dict(payload or {})))
        return {}


class RatioSequence:
    """按顺序吐进度；用完后一直重复最后一个（模拟「进度不再动」）。"""

    def __init__(self, ratios: list[float]) -> None:
        assert ratios
        self.ratios = ratios
        self.index = 0

    async def __call__(self) -> float:
        value = self.ratios[min(self.index, len(self.ratios) - 1)]
        self.index += 1
        return value


class PolicyTests(unittest.TestCase):
    def test_defaults_are_one_second_and_bounded(self) -> None:
        policy = ProgressPolicy()
        self.assertEqual(policy.page_interval_seconds, PAGE_INTERVAL_SECONDS)
        self.assertEqual(policy.page_interval_seconds, 1.0)
        self.assertEqual(MIN_PAGE_INTERVAL_SECONDS, 0.2)
        self.assertGreaterEqual(policy.max_pages, 1)
        self.assertGreaterEqual(policy.max_total_seconds, 60)

    def test_rejects_pages_faster_than_human_swipe(self) -> None:
        with self.assertRaises(ValueError):
            ProgressPolicy(page_interval_seconds=0.1)

    def test_rejects_bad_limits(self) -> None:
        cases = (
            {"max_pages": 0},
            {"max_total_seconds": 0.1},
            {"refresh_every_pages": 0},
            {"ratio_stall_checks": 0},
        )
        for case in cases:
            with self.subTest(case=case), self.assertRaises(ValueError):
                ProgressPolicy(**case)

    def test_accepts_custom_interval(self) -> None:
        policy = ProgressPolicy(page_interval_seconds=2.5)
        self.assertEqual(policy.page_interval_seconds, 2.5)


class ReporterTests(unittest.TestCase):
    def report(
        self,
        *,
        policy: ProgressPolicy | None = None,
        ratios: list[float],
        dry_run: bool = False,
        target_ratio: float = 1.0,
        total_pages: int = TOTAL_PAGES,
        start_page_index: int = -1,
        ratio_before: float = 0.0,
    ) -> tuple[ProgressReport, StubClient, list[float], RatioSequence]:
        client = StubClient()
        sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        reporter = CoursewareProgressReporter(client, policy=policy, sleep=fake_sleep)
        sequence = RatioSequence(ratios)

        async def _run() -> ProgressReport:
            return await reporter.run(
                course_id=COURSE.remote_id,
                material_id=MATERIAL_ID,
                total_pages=total_pages,
                start_page_index=start_page_index,
                ratio_before=ratio_before,
                fetch_ratio=sequence,
                dry_run=dry_run,
                target_ratio=target_ratio,
            )

        return asyncio.run(_run()), client, sleeps, sequence

    def test_dry_run_never_touches_the_platform(self) -> None:
        report, client, sleeps, sequence = self.report(
            ratios=[ratio_for(0)], dry_run=True
        )
        self.assertTrue(report.dry_run)
        self.assertEqual(report.stopped, STOP_DRY_RUN)
        self.assertEqual(report.planned_pages, TOTAL_PAGES)
        self.assertEqual(report.sent_pages, 0)
        self.assertEqual(client.calls, [])
        self.assertEqual(sleeps, [])
        self.assertEqual(sequence.index, 0)
        self.assertIn("dry-run", report.summary())
        self.assertIn("计划翻 10 页", report.summary())

    def test_dry_run_with_unknown_pages_does_not_invent_a_plan(self) -> None:
        report, client, _, _ = self.report(
            ratios=[0.0], dry_run=True, total_pages=0
        )
        self.assertEqual(report.stopped, STOP_DRY_RUN)
        self.assertEqual(report.planned_pages, 0)
        self.assertIn("页数未知", report.summary())
        self.assertEqual(client.calls, [])

    def test_already_at_target_sends_nothing(self) -> None:
        report, client, sleeps, _ = self.report(
            ratios=[1.0], start_page_index=9, ratio_before=1.0
        )
        self.assertEqual(report.stopped, STOP_TARGET_REACHED)
        self.assertEqual(client.calls, [])
        self.assertEqual(sleeps, [])
        self.assertEqual(report.sent_pages, 0)

    def test_live_steps_every_page_and_stops_at_target(self) -> None:
        report, client, sleeps, _ = self.report(
            policy=ProgressPolicy(refresh_every_pages=10, max_pages=20),
            ratios=[ratio_for(9)],
        )
        self.assertEqual(report.stopped, STOP_TARGET_REACHED)
        self.assertEqual(report.sent_pages, TOTAL_PAGES)
        self.assertEqual(len(report.ticks), TOTAL_PAGES)
        self.assertAlmostEqual(report.ratio_delta, 1.0)
        self.assertEqual(sleeps, [PAGE_INTERVAL_SECONDS] * (TOTAL_PAGES - 1))

        self.assertEqual(len(client.calls), TOTAL_PAGES)
        for index, (action, payload) in enumerate(client.calls):
            self.assertEqual(action, ep.ACTION_COURSEWARE_MAX_LOOK)
            self.assertEqual(
                payload,
                {ep.FIELD_COURSEWARE_ID: MATERIAL_ID, ep.FIELD_MAX_PAGE_INDEX: index},
            )

    def test_target_ratio_stops_at_the_right_page(self) -> None:
        report, client, _, _ = self.report(
            policy=ProgressPolicy(refresh_every_pages=1),
            ratios=[0.1, 0.2, 0.3, 0.4, 0.5],
            target_ratio=0.5,
        )
        self.assertEqual(report.stopped, STOP_TARGET_REACHED)
        self.assertEqual(report.sent_pages, 5)
        self.assertEqual(len(client.calls), 5)
        self.assertAlmostEqual(report.ratio_after, 0.5)

    def test_zero_target_sends_nothing(self) -> None:
        report, client, _, _ = self.report(ratios=[0.0], target_ratio=0.0)
        self.assertEqual(report.stopped, STOP_TARGET_REACHED)
        self.assertEqual(report.sent_pages, 0)
        self.assertEqual(client.calls, [])

    def test_ratio_stall_stops_early(self) -> None:
        report, client, _, _ = self.report(
            policy=ProgressPolicy(
                refresh_every_pages=2, ratio_stall_checks=2, max_pages=100
            ),
            ratios=[0.0],
        )
        self.assertEqual(report.stopped, STOP_RATIO_STALLED)
        self.assertEqual(report.sent_pages, 4)
        self.assertEqual(len(client.calls), 4)
        self.assertEqual(report.ratio_delta, 0)

    def test_max_pages_cap(self) -> None:
        report, client, _, _ = self.report(
            policy=ProgressPolicy(
                refresh_every_pages=1000, ratio_stall_checks=3, max_pages=3
            ),
            ratios=[0.0],
        )
        self.assertEqual(report.stopped, STOP_MAX_PAGES)
        self.assertEqual(report.sent_pages, 3)
        self.assertEqual(len(client.calls), 3)

    def test_max_total_seconds_cap(self) -> None:
        report, _, _, _ = self.report(
            policy=ProgressPolicy(
                refresh_every_pages=1000,
                ratio_stall_checks=3,
                max_pages=100,
                page_interval_seconds=1.0,
                max_total_seconds=3.0,
            ),
            ratios=[0.0],
        )
        self.assertEqual(report.stopped, STOP_MAX_SECONDS)
        self.assertEqual(report.sent_pages, 3)

    def test_resumes_from_max_page_index(self) -> None:
        report, client, _, _ = self.report(
            policy=ProgressPolicy(refresh_every_pages=100),
            ratios=[ratio_for(9)],
            start_page_index=7,
            ratio_before=ratio_for(7),
        )
        self.assertEqual(report.stopped, STOP_TARGET_REACHED)
        self.assertEqual([p[1][ep.FIELD_MAX_PAGE_INDEX] for p in client.calls], [8, 9])

    def test_tolerance_is_smaller_than_one_percent(self) -> None:
        self.assertLess(RATIO_EPSILON, 0.01)


class _RecordingTransport:
    """最小 Transport：按 action 回信封，并记录请求体。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.max_index = -1

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        timeout: float = 15.0,
    ) -> Response:
        parsed = urllib.parse.urlparse(url)
        action = urllib.parse.parse_qs(parsed.query).get("actionName", [""])[0]
        payload = json.loads(body.decode("utf-8")) if body else {}
        self.calls.append({"action": action, "payload": payload, "headers": dict(headers or {})})
        if action == ep.ACTION_MATERIAL_DETAIL:
            data: dict[str, object] = dict(MATERIAL_DETAIL)
            data[ep.FIELD_LEARNING_RATIO] = round(ratio_for(self.max_index) * 100)
        elif action == ep.ACTION_COURSEWARE_OPEN:
            data = {
                ep.FIELD_MAX_PAGE_INDEX: self.max_index,
                ep.FIELD_PAGES: [
                    {ep.FIELD_PAGE_INDEX: index + 1, "coursewarePageId": f"p{index}"}
                    for index in range(TOTAL_PAGES)
                ],
            }
        elif action == ep.ACTION_COURSEWARE_MAX_LOOK:
            self.max_index = int(payload[ep.FIELD_MAX_PAGE_INDEX])
            data = {}
        else:  # pragma: no cover - 走到这里说明接线接错了
            raise AssertionError(f"意外 action：{action}")
        envelope = {"statusCode": 0, "data": data, "message": "ok"}
        return Response(
            status=200,
            url=url,
            body=json.dumps(envelope, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )


class AdapterWiringTests(unittest.IsolatedAsyncioTestCase):
    def make_adapter(self) -> tuple[PincoAdapter, _RecordingTransport]:
        transport = _RecordingTransport()
        ctx = AdapterContext(http=HttpClient(transport=transport))  # type: ignore[arg-type]
        adapter = PincoAdapter(ctx)
        adapter.adopt_session(authorization="Bearer test", csrf_token="csrf-test")
        return adapter, transport

    def test_advance_learning_defaults_to_dry_run(self) -> None:
        default = inspect.signature(PincoAdapter.advance_learning).parameters["dry_run"].default
        self.assertIs(default, True)

    async def test_advance_learning_dry_run_reads_only_detail(self) -> None:
        adapter, transport = self.make_adapter()
        report = await adapter.advance_learning(COURSE, MATERIAL_ID)
        self.assertTrue(report.dry_run)
        self.assertEqual(
            [call["action"] for call in transport.calls], [ep.ACTION_MATERIAL_DETAIL]
        )
        self.assertEqual(transport.calls[0]["payload"][ep.FIELD_COURSE_ID], COURSE.remote_id)

    async def test_advance_learning_live_opens_then_turns_pages(self) -> None:
        adapter, transport = self.make_adapter()
        policy = ProgressPolicy(refresh_every_pages=1000, max_pages=100)
        report = await adapter.advance_learning(
            COURSE, MATERIAL_ID, dry_run=False, policy=policy
        )
        self.assertEqual(report.stopped, STOP_TARGET_REACHED)
        self.assertEqual(report.total_pages, TOTAL_PAGES)
        actions = [call["action"] for call in transport.calls]
        self.assertEqual(actions[0], ep.ACTION_MATERIAL_DETAIL)
        self.assertEqual(actions.count(ep.ACTION_COURSEWARE_OPEN), 1)
        self.assertLess(actions.index(ep.ACTION_COURSEWARE_OPEN), actions.index(
            ep.ACTION_COURSEWARE_MAX_LOOK
        ))
        self.assertEqual(actions[-1], ep.ACTION_MATERIAL_DETAIL)
        max_looks = [
            call["payload"]
            for call in transport.calls
            if call["action"] == ep.ACTION_COURSEWARE_MAX_LOOK
        ]
        self.assertEqual(len(max_looks), TOTAL_PAGES)
        self.assertEqual(max_looks[0][ep.FIELD_COURSEWARE_ID], MATERIAL_ID)
        self.assertEqual(
            [payload[ep.FIELD_MAX_PAGE_INDEX] for payload in max_looks],
            list(range(TOTAL_PAGES)),
        )

    async def test_open_courseware_requires_res_id(self) -> None:
        class _DetailOnly:
            async def request(self, method, url, *, headers=None, body=None, timeout=15.0):  # type: ignore[no-untyped-def]
                envelope = {
                    "statusCode": 0,
                    "data": {"chapterDirectoryMaterialId": MATERIAL_ID},
                    "message": "ok",
                }
                return Response(
                    status=200,
                    url=url,
                    body=json.dumps(envelope).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                )

        ctx = AdapterContext(http=HttpClient(transport=_DetailOnly()))  # type: ignore[arg-type]
        adapter = PincoAdapter(ctx)
        adapter.adopt_session(authorization="Bearer test", csrf_token="csrf-test")
        with self.assertRaises(NotSupportedError):
            await adapter.open_courseware(COURSE, MATERIAL_ID)
