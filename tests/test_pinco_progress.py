"""品课「刷课」上报器测试 —— 完全离线，不联网、不真 sleep。

覆盖三块：

1. **策略**：节奏不得快于页面实测（5 秒），各种上限/阈值的取值校验；
2. **上报器**：dry-run 不发请求、按页面报文发送、达标即停、进度不动就停、
   总时长/次数上限、``countLearningProgress`` 为假时拒绝；
3. **接线**：适配器方法默认 dry-run，且真的走
   ``PostV1CourseByCourseIdMaterialStudentLearningDurationReport``（假 Transport 校验报文）。

为什么单独盯熔断：进度是否真的累加**尚未确认**（见
``docs/verification/pinco-progress.md``）。语义没确认之前，最危险的不是「没刷上」，
而是「刷了几百次还以为在涨」—— 所以「进度不动就停」必须有测试。
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
from kedaibiao.platforms.pinco.models import PincoMaterialDetail
from kedaibiao.platforms.pinco.progress import (
    PAGE_LEARNING_DURATION,
    PAGE_TICK_SECONDS,
    RATIO_EPSILON,
    STOP_DRY_RUN,
    STOP_MAX_SECONDS,
    STOP_MAX_TICKS,
    STOP_RATIO_STALLED,
    STOP_TARGET_REACHED,
    LearningReporter,
    ProgressPolicy,
    ProgressReport,
)

COURSE = Course(remote_id="course-1", name="示例课程")
MATERIAL_ID = "1322029806386077696"

#: 真实材料详情样本（字段形态与线上一致；``countLearningProgress`` 为真）。
MATERIAL_DETAIL: dict[str, object] = {
    "chapterDirectoryMaterialId": MATERIAL_ID,
    "materialId": "1322029806344134656",
    "materialName": "示例材料-01.pptx",
    "learningRatio": 0,
    "viewed": False,
    "countLearningProgress": True,
}


def material(ratio: float, *, counts: bool = True) -> PincoMaterialDetail:
    """构造一条材料详情；``ratio`` 是 0~1 的归一化进度。"""
    return PincoMaterialDetail(
        material_id=MATERIAL_ID,
        name="示例材料-01.pptx",
        progress=ratio,
        raw={
            ep.FIELD_COUNT_LEARNING_PROGRESS: counts,
            ep.FIELD_LEARNING_RATIO: ratio * 100,
        },
    )


class StubClient:
    """只记录 ``post_action`` 的假客户端。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def post_action(
        self, action: str, payload: dict[str, object] | None = None, *, dangerous: bool = False
    ) -> dict[str, object]:
        assert dangerous is True, "学习时长上报是写操作，必须显式 dangerous=True"
        self.calls.append((action, dict(payload or {})))
        return {}


class DetailSequence:
    """按顺序吐详情；用完后一直重复最后一个（模拟「进度不再动」）。"""

    def __init__(self, details: list[PincoMaterialDetail]) -> None:
        assert details
        self.details = details
        self.index = 0

    async def __call__(self) -> PincoMaterialDetail:
        item = self.details[min(self.index, len(self.details) - 1)]
        self.index += 1
        return item


class PolicyTests(unittest.TestCase):
    def test_defaults_match_page_rhythm(self) -> None:
        policy = ProgressPolicy()
        self.assertEqual(policy.tick_seconds, PAGE_TICK_SECONDS)
        self.assertEqual(policy.learning_duration, PAGE_LEARNING_DURATION)
        self.assertEqual(policy.learning_duration, 5)
        self.assertEqual(policy.tick_seconds, 5.0)

    def test_rejects_ticks_faster_than_page(self) -> None:
        with self.assertRaises(ValueError):
            ProgressPolicy(tick_seconds=2.0)

    def test_rejects_bad_limits(self) -> None:
        cases = (
            {"learning_duration": 0},
            {"refresh_every": 0},
            {"max_ticks": 0},
            {"max_total_seconds": 3},
            {"ratio_stall_checks": 0},
        )
        for case in cases:
            with self.subTest(case=case), self.assertRaises(ValueError):
                ProgressPolicy(**case)


class ReporterTests(unittest.TestCase):
    def report(
        self,
        *,
        policy: ProgressPolicy | None = None,
        details: list[PincoMaterialDetail],
        initial: PincoMaterialDetail | None = None,
        dry_run: bool = False,
        target_ratio: float = 1.0,
    ) -> tuple[ProgressReport, StubClient, list[float], DetailSequence]:
        client = StubClient()
        sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        reporter = LearningReporter(client, policy=policy, sleep=fake_sleep)
        sequence = DetailSequence(details)

        async def _run() -> ProgressReport:
            return await reporter.run(
                course_id=COURSE.remote_id,
                material_id=MATERIAL_ID,
                initial=initial or material(0.0),
                fetch_detail=sequence,
                dry_run=dry_run,
                target_ratio=target_ratio,
            )

        return asyncio.run(_run()), client, sleeps, sequence

    def test_dry_run_never_touches_the_platform(self) -> None:
        report, client, sleeps, sequence = self.report(
            policy=ProgressPolicy(max_ticks=12, max_total_seconds=60),
            details=[material(0.0)],
            dry_run=True,
        )
        self.assertTrue(report.dry_run)
        self.assertEqual(report.stopped, STOP_DRY_RUN)
        self.assertEqual(report.planned_seconds, 60)
        self.assertEqual(report.sent_seconds, 0)
        self.assertEqual(client.calls, [])
        self.assertEqual(sleeps, [])
        self.assertEqual(sequence.index, 0)
        self.assertIn("dry-run", report.summary())

    def test_dry_run_plan_is_capped_by_total_seconds(self) -> None:
        report, _, _, _ = self.report(
            policy=ProgressPolicy(max_ticks=100, max_total_seconds=30),
            details=[material(0.0)],
            dry_run=True,
        )
        self.assertEqual(report.planned_seconds, 30)

    def test_already_at_target_sends_nothing(self) -> None:
        report, client, sleeps, _ = self.report(
            details=[material(1.0)],
            initial=material(1.0),
        )
        self.assertEqual(report.stopped, STOP_TARGET_REACHED)
        self.assertEqual(client.calls, [])
        self.assertEqual(sleeps, [])
        self.assertEqual(report.sent_seconds, 0)
        self.assertEqual(report.ratio_delta, 0)

    def test_live_sends_page_payload_and_stops_at_target(self) -> None:
        report, client, sleeps, _ = self.report(
            policy=ProgressPolicy(refresh_every=2, max_ticks=10),
            details=[material(0.5), material(1.0)],
        )
        self.assertEqual(report.stopped, STOP_TARGET_REACHED)
        self.assertEqual(report.sent_seconds, 20)
        self.assertEqual(len(report.ticks), 4)
        self.assertAlmostEqual(report.ratio_delta, 1.0)
        self.assertEqual(sleeps, [PAGE_TICK_SECONDS] * 3)

        self.assertEqual(len(client.calls), 4)
        for action, payload in client.calls:
            self.assertEqual(action, ep.ACTION_LEARNING_DURATION_REPORT)
            self.assertEqual(
                payload,
                {
                    ep.FIELD_COURSE_ID: COURSE.remote_id,
                    ep.FIELD_MATERIAL_ID: MATERIAL_ID,
                    ep.FIELD_LEARNING_DURATION: PAGE_LEARNING_DURATION,
                },
            )

    def test_ratio_stall_stops_early(self) -> None:
        report, client, _, _ = self.report(
            policy=ProgressPolicy(
                refresh_every=2, ratio_stall_checks=2, max_ticks=100, max_total_seconds=3600
            ),
            details=[material(0.0)],
        )
        self.assertEqual(report.stopped, STOP_RATIO_STALLED)
        self.assertEqual(report.sent_seconds, 20)
        self.assertEqual(len(client.calls), 4)
        self.assertEqual(report.ratio_delta, 0)

    def test_max_total_seconds_cap(self) -> None:
        report, _, _, _ = self.report(
            policy=ProgressPolicy(
                refresh_every=1000, ratio_stall_checks=3, max_ticks=100, max_total_seconds=12
            ),
            details=[material(0.0)],
        )
        self.assertEqual(report.stopped, STOP_MAX_SECONDS)
        self.assertEqual(report.sent_seconds, 10)

    def test_max_ticks_cap(self) -> None:
        report, _, _, _ = self.report(
            policy=ProgressPolicy(
                refresh_every=1000, max_ticks=3, max_total_seconds=3600
            ),
            details=[material(0.0)],
        )
        self.assertEqual(report.stopped, STOP_MAX_TICKS)
        self.assertEqual(report.sent_seconds, 15)
        self.assertEqual(len(report.ticks), 3)

    def test_counts_progress_false_is_refused(self) -> None:
        client = StubClient()

        async def fake_sleep(seconds: float) -> None:
            raise AssertionError("不该 sleep")

        reporter = LearningReporter(client, sleep=fake_sleep)
        sequence = DetailSequence([material(0.0)])
        with self.assertRaises(NotSupportedError):
            asyncio.run(
                reporter.run(
                    course_id=COURSE.remote_id,
                    material_id=MATERIAL_ID,
                    initial=material(0.0, counts=False),
                    fetch_detail=sequence,
                    dry_run=False,
                )
            )
        self.assertEqual(client.calls, [])

    def test_tolerance_is_smaller_than_one_percent(self) -> None:
        self.assertLess(RATIO_EPSILON, 0.01)


class _RecordingTransport:
    """最小 Transport：按 action 回信封，并记录请求体。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

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
        elif action == ep.ACTION_LEARNING_DURATION_REPORT:
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

    def test_report_learning_defaults_to_dry_run(self) -> None:
        default = inspect.signature(PincoAdapter.report_learning).parameters["dry_run"].default
        self.assertIs(default, True)

    async def test_report_learning_dry_run_reads_only_detail(self) -> None:
        adapter, transport = self.make_adapter()
        report = await adapter.report_learning(COURSE, MATERIAL_ID)
        self.assertTrue(report.dry_run)
        self.assertEqual(
            [call["action"] for call in transport.calls], [ep.ACTION_MATERIAL_DETAIL]
        )
        self.assertEqual(transport.calls[0]["payload"][ep.FIELD_COURSE_ID], COURSE.remote_id)

    async def test_report_learning_live_sends_duration_report(self) -> None:
        adapter, transport = self.make_adapter()
        policy = ProgressPolicy(
            refresh_every=1, ratio_stall_checks=1, max_ticks=2, max_total_seconds=60
        )
        report = await adapter.report_learning(
            COURSE, MATERIAL_ID, dry_run=False, policy=policy
        )
        self.assertEqual(report.stopped, STOP_RATIO_STALLED)
        actions = [call["action"] for call in transport.calls]
        self.assertEqual(
            actions,
            [
                ep.ACTION_MATERIAL_DETAIL,
                ep.ACTION_LEARNING_DURATION_REPORT,
                ep.ACTION_MATERIAL_DETAIL,
            ],
        )
        sent = transport.calls[1]["payload"]
        self.assertEqual(sent[ep.FIELD_COURSE_ID], COURSE.remote_id)
        self.assertEqual(sent[ep.FIELD_MATERIAL_ID], MATERIAL_ID)
        self.assertEqual(sent[ep.FIELD_LEARNING_DURATION], PAGE_LEARNING_DURATION)
