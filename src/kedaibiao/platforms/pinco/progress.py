"""品课「刷课」：按页推进课件学习进度（**写操作**）。

## 机制（2026-10-20 实测确认，证据见 ``docs/verification/pinco-progress.md``）

材料进度不是按时长算的 —— 是**按看过的最大页**算的：

1. 把材料当成一份课件打开::

       POST /student/api.json?actionName=PostV1CoursewareStudentOpen
       {"resId": <media.resId>, "coursewareId": <材料列表里的 materialId>}

   返回 ``data.pages[]``（总页数）与 ``data.maxPageIndex``（已看到的最大页，
   **0 起**）。实测：10 页的 PPT、``maxPageIndex=3`` 时材料
   ``learningRatio=40`` —— ``(3+1)/10 = 40%``，对得上。

2. 每「翻一页」上报一次::

       POST /student/api.json?actionName=PostV1CoursewareStudentMaxLook
       {"coursewareId": <materialId>, "maxPageIndex": <新页号>}

   实测把它从 3 推到 4，材料的 ``learningRatio`` 立刻从 40 变成 50。

PPT（``media.type=4``）和 PDF（``type=8``）都走同一条路（PDF 实测 45 / 387 页）。

## 为什么不是「学习时长上报」

学生端 H5 材料页里确实有 ``PostV1CourseByCourseIdMaterialStudentLearningDurationReport``
（每 5 秒一次、每次 5 秒），但**实测发了 60s / 1200s 进度都不动** —— 接口收下
不等于累加。真正让 ``learningRatio`` 变化的是上面的 ``maxLook``。时长上报的
常量仍留在 ``endpoints.py`` 里作为阴性证据，不再用于刷课。

## 安全边界

- 默认 **dry-run**：只读材料、算要翻多少页，不发任何写请求；
- 真发必须显式 ``dry_run=False``，且 ``maxLook``/``open`` 都在
  ``DANGEROUS_ACTIONS`` 里，客户端要求显式放行；
- 翻页之间有间隔（默认 1 秒，不低于 0.2 秒），带页数/总时长双上限；
- 每 N 页回读一次材料详情核对进度；连续几次都没涨就停（``ratio_stalled``）；
- 只碰这一门课的材料进度，绝不碰作业提交。

页码语义提醒：``maxPageIndex`` 是 **0 起**的页下标，``maxPageIndex=0`` 表示
第 1 页已看。所以 100% 对应 ``maxPageIndex = 总页数 - 1``。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from kedaibiao.platforms.pinco import endpoints as ep

log = logging.getLogger(__name__)

#: 翻页间隔：真实滑动一页大约 1~2 秒。比 0.2 秒更密没有依据，只会更像脚本。
PAGE_INTERVAL_SECONDS = 1.0
MIN_PAGE_INTERVAL_SECONDS = 0.2

#: 进度比较的容差：``learningRatio`` 是百分比整数，抖动小于 0.5% 不算涨。
RATIO_EPSILON = 0.005

#: 停下来的原因（固定字符串，测试与调用方都按它分支）。
STOP_DRY_RUN = "dry_run"
STOP_TARGET_REACHED = "target_reached"
STOP_MAX_PAGES = "max_pages"
STOP_MAX_SECONDS = "max_total_seconds"
STOP_RATIO_STALLED = "ratio_stalled"


@dataclass(frozen=True, slots=True)
class ProgressPolicy:
    """翻页节奏与熔断阈值。默认值 = 「一秒一页、一次最多 900 秒、涨不动就停」。"""

    page_interval_seconds: float = PAGE_INTERVAL_SECONDS
    #: 每次运行最多翻多少页（含跳页之后剩下的页数）。
    max_pages: int = 400
    #: 每次运行的时间上限（秒）—— 给用户的承诺，不是功能限制。
    max_total_seconds: float = 900.0
    #: 每翻多少页回读一次材料详情核对进度。
    refresh_every_pages: int = 10
    #: 连续几次回读都没看到进度上涨就停。
    ratio_stall_checks: int = 2

    def __post_init__(self) -> None:
        if self.page_interval_seconds < MIN_PAGE_INTERVAL_SECONDS:
            raise ValueError(
                f"page_interval_seconds 不能小于 {MIN_PAGE_INTERVAL_SECONDS:g} 秒"
            )
        if self.max_pages < 1:
            raise ValueError("max_pages 至少为 1")
        if self.max_total_seconds < self.page_interval_seconds:
            raise ValueError("max_total_seconds 至少要容纳一次翻页")
        if self.refresh_every_pages < 1:
            raise ValueError("refresh_every_pages 至少为 1")
        if self.ratio_stall_checks < 1:
            raise ValueError("ratio_stall_checks 至少为 1")


@dataclass(slots=True)
class ProgressTick:
    """翻一页的记录。``ratio`` 只有轮到回读详情的那次才有值。"""

    page_index: int
    total_pages: int
    ratio: float | None = None

    @property
    def position(self) -> str:
        return f"{self.page_index + 1}/{self.total_pages}"

    def as_line(self) -> str:
        ratio = "—" if self.ratio is None else f"{self.ratio * 100:.0f}%"
        return f"翻到第 {self.position} 页，进度 {ratio}"


@dataclass(slots=True)
class ProgressReport:
    """一次「刷课」的结果 —— 翻没翻、翻到哪、进度动没动。"""

    course_id: str
    material_id: str
    dry_run: bool
    ratio_before: float
    ratio_after: float
    stopped: str
    total_pages: int = 0
    #: 开始时已看到的最大页下标（0 起；``-1`` 表示一页没看过）。
    start_page_index: int = -1
    #: dry-run 时算出来的计划翻页数（真实发送时为实际发送数）。
    planned_pages: int = 0
    sent_pages: int = 0
    ticks: list[ProgressTick] = field(default_factory=list)

    @property
    def ratio_delta(self) -> float:
        return round(self.ratio_after - self.ratio_before, 4)

    def summary(self) -> str:
        before = f"{self.ratio_before * 100:.0f}%"
        after = f"{self.ratio_after * 100:.0f}%"
        pages = f"（共 {self.total_pages} 页）" if self.total_pages else ""
        if self.dry_run:
            if not self.total_pages:
                return (
                    f"[dry-run] 材料 {self.material_id}：进度 {before}，"
                    "页数未知（dry-run 不打开课件 —— 打开是写接口）；"
                    "未发送任何请求。"
                )
            return (
                f"[dry-run] 材料 {self.material_id}：进度 {before}{pages}，"
                f"计划翻 {self.planned_pages} 页；未发送任何请求。"
            )
        return (
            f"材料 {self.material_id}：进度 {before} → {after}"
            f"（{self.ratio_delta:+.2%}），翻页 {self.sent_pages} 次"
            f"{pages}，停止原因 {self.stopped}。"
        )


DetailFetcher = Callable[[], Awaitable[Any]]
PageHook = Callable[[ProgressTick], None]


class CoursewareProgressReporter:
    """按页推进课件进度（``PostV1CoursewareStudentMaxLook``）。

    只依赖一个能发 action 的 client 和一个能回读材料进度的函数，单测因此
    可以不联网、不 sleep（注入假的 ``sleep``）。
    """

    def __init__(
        self,
        client: Any,
        *,
        policy: ProgressPolicy | None = None,
        sleep: Callable[[float], Awaitable[Any]] | None = None,
    ) -> None:
        self._client = client
        self.policy = policy or ProgressPolicy()
        self._sleep = sleep or asyncio.sleep

    # -- 小工具 -------------------------------------------------------------

    def _payload(self, material_id: str, page_index: int) -> dict[str, Any]:
        return {
            ep.FIELD_COURSEWARE_ID: material_id,
            ep.FIELD_MAX_PAGE_INDEX: page_index,
        }

    @staticmethod
    def _target_page_index(total_pages: int, target_ratio: float) -> int:
        """按目标比例算出「最后一页」的下标（0 起，含）。"""
        target = min(max(target_ratio, 0.0), 1.0)
        if target <= 0:
            return -1
        pages = max(1, round(total_pages * target))
        return min(total_pages, pages) - 1

    @staticmethod
    def _clamp(value: float) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return 0.0
        return min(max(number, 0.0), 1.0)

    # -- 主流程 -------------------------------------------------------------

    async def run(
        self,
        *,
        course_id: str,
        material_id: str,
        total_pages: int,
        start_page_index: int,
        ratio_before: float,
        fetch_ratio: DetailFetcher,
        dry_run: bool = True,
        target_ratio: float = 1.0,
        on_page: PageHook | None = None,
    ) -> ProgressReport:
        """把材料从 ``start_page_index`` 推到 ``target_ratio``。

        ``start_page_index`` 是课件接口给的 ``maxPageIndex``（0 起，-1 = 没看过）。
        ``dry_run=True``（默认）只算计划、不发请求。
        """
        if total_pages < 0:
            raise ValueError("total_pages 不能为负")
        if start_page_index < -1:
            raise ValueError("start_page_index 不能小于 -1")
        if not dry_run and total_pages < 1:
            raise ValueError("真实翻页需要 total_pages >= 1")

        target_index = self._target_page_index(total_pages, target_ratio)
        report = ProgressReport(
            course_id=course_id,
            material_id=material_id,
            dry_run=dry_run,
            ratio_before=self._clamp(ratio_before),
            ratio_after=self._clamp(ratio_before),
            stopped=STOP_DRY_RUN,
            total_pages=total_pages,
            start_page_index=start_page_index,
        )
        planned = max(0, target_index - start_page_index) if total_pages else 0
        if dry_run:
            # dry-run 不打开课件（打开是写接口），所以页数可能未知；未知就不编计划。
            report.planned_pages = planned
            if total_pages and planned == 0:
                report.stopped = STOP_TARGET_REACHED
            log.info("材料 %s dry-run：计划翻 %s 页（未发送）", material_id, planned)
            return report

        if planned == 0:
            report.stopped = STOP_TARGET_REACHED
            log.info(
                "材料 %s 已到目标（maxPageIndex=%s / %s 页）",
                material_id,
                start_page_index,
                total_pages,
            )
            return report

        return await self._run_live(
            report=report,
            material_id=material_id,
            target_index=target_index,
            fetch_ratio=fetch_ratio,
            target_ratio=target_ratio,
            on_page=on_page,
        )

    async def _run_live(
        self,
        *,
        report: ProgressReport,
        material_id: str,
        target_index: int,
        fetch_ratio: DetailFetcher,
        target_ratio: float,
        on_page: PageHook | None,
    ) -> ProgressReport:
        policy = self.policy
        page = report.start_page_index
        elapsed = 0.0
        last_seen = report.ratio_before
        stalled = 0
        index = 0

        while page < target_index:
            if index >= policy.max_pages:
                report.stopped = STOP_MAX_PAGES
                break
            if elapsed + policy.page_interval_seconds > policy.max_total_seconds:
                report.stopped = STOP_MAX_SECONDS
                break

            page += 1
            index += 1
            # dangerous=True：翻页上报是写操作，客户端闸门要求显式放行。
            await self._client.post_action(
                ep.ACTION_COURSEWARE_MAX_LOOK,
                self._payload(material_id, page),
                dangerous=True,
            )
            report.sent_pages += 1
            tick = ProgressTick(page_index=page, total_pages=report.total_pages)

            last_page = page >= target_index
            check_now = index % policy.refresh_every_pages == 0 or last_page
            if check_now:
                ratio = self._clamp(await fetch_ratio())
                tick.ratio = ratio
                report.ratio_after = ratio
            report.ticks.append(tick)
            if on_page is not None:
                on_page(tick)

            if check_now:
                if report.ratio_after + RATIO_EPSILON >= target_ratio:
                    report.stopped = STOP_TARGET_REACHED
                    break
                if report.ratio_after > last_seen + RATIO_EPSILON:
                    stalled = 0
                    last_seen = report.ratio_after
                    log.info(
                        "材料 %s 进度涨到 %.0f%%（第 %s 页）",
                        material_id,
                        report.ratio_after * 100,
                        page + 1,
                    )
                else:
                    stalled += 1
                    if stalled >= policy.ratio_stall_checks:
                        report.stopped = STOP_RATIO_STALLED
                        break

            elapsed += policy.page_interval_seconds
            if page < target_index:
                await self._sleep(policy.page_interval_seconds)

        log.info(
            "材料 %s 翻页结束：%s 次，进度 %.0f%% → %.0f%%，停止原因 %s",
            material_id,
            report.sent_pages,
            report.ratio_before * 100,
            report.ratio_after * 100,
            report.stopped,
        )
        return report
