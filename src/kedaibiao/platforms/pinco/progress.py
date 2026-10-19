"""品课「刷课」：学习时长上报（**写操作**）。

为什么单独一个模块而不是塞进 adapter：它是一段有节奏的循环（每 5 秒一次、
几十到几百次），和「一次请求一个结果」的抓取方法气质完全不同；单独放也让
「哪些代码会写平台」一眼可见。

## 已确认的事实（证据见 ``docs/verification/pinco-progress.md``）

官方学生端 bundle（``material.js``）里，材料页在详情返回
``countLearningProgress === true`` 时启动 ``setInterval(5000)``：

```
POST /student/api.json?actionName=PostV1CourseByCourseIdMaterialStudentLearningDurationReport
{"courseId": <courseId>, "materialId": <列表里的 materialId>, "learningDuration": 5}
```

真实会话直接调用返回 ``{"statusCode":0,"message":"ok"}``。

## **尚未确认**的部分（不要当成已解决）

实测按上面的报文发了 60 秒、以及一次性发 1200 秒，材料的 ``learningRatio``
始终是 0、``viewed`` 始终是 ``false``（部分已学材料也停在 12 不动）。可见
「接口收下了」不等于「进度累加了」—— 可能还有页面/App 上下文、学习会话或
服务端批处理的门槛。

因此这里的默认行为是 **dry-run（只算计划、不发请求）**，真正发送必须显式
``dry_run=False``；并且带三道熔断：总时长上限、次数上限、**连续若干次刷新
进度都没动就停** —— 在语义没确认之前，宁可停下来说「没动」，也不要把同一发
请求刷几百次。

## 安全边界

- 只发这一个 action，绝不碰作业提交（那个走 adapter.submit + guard）；
- 只对 ``countLearningProgress`` 为真的材料动手；
- 间隔不得低于页面实测的 5 秒（比页面更密只会更像机器人）；
- 不读、不写任何凭据；日志里只有 materialId 与秒数。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from kedaibiao.platforms.base import NotSupportedError
from kedaibiao.platforms.pinco import endpoints as ep
from kedaibiao.platforms.pinco.models import PincoMaterialDetail

log = logging.getLogger(__name__)

#: 页面实测的节奏：每 5 秒上报一次、每次算 5 秒。改小没有依据，只会更像脚本。
PAGE_TICK_SECONDS = 5.0
PAGE_LEARNING_DURATION = 5

#: 允许的最小间隔。页面就是 5 秒；比它更密不属于「模拟正常学习」。
MIN_TICK_SECONDS = PAGE_TICK_SECONDS

#: 进度比较的容差：``learningRatio`` 是百分比整数/归一化小数，抖动小于 1% 不算涨。
RATIO_EPSILON = 0.005

#: 停下来的原因（固定字符串，测试与调用方都按它分支）。
STOP_DRY_RUN = "dry_run"
STOP_TARGET_REACHED = "target_reached"
STOP_MAX_TICKS = "max_ticks"
STOP_MAX_SECONDS = "max_total_seconds"
STOP_RATIO_STALLED = "ratio_stalled"


@dataclass(frozen=True, slots=True)
class ProgressPolicy:
    """上报节奏与熔断阈值。默认值就是「按页面节奏，最多一小时，涨不动就停」。

    ``max_ticks`` 与 ``max_total_seconds`` 任一先到都会停：前者防次数失控，
    后者是给用户的总时长承诺（默认 1 小时）。
    """

    tick_seconds: float = PAGE_TICK_SECONDS
    learning_duration: int = PAGE_LEARNING_DURATION
    #: 每多少次上报刷新一次材料详情看进度（页面不刷新，但我们要知道有没有涨）。
    refresh_every: int = 6
    max_ticks: int = 720
    max_total_seconds: int = 3600
    #: 连续多少次刷新都看不到进度上涨就停 —— 语义未确认前的熔断。
    ratio_stall_checks: int = 3

    def __post_init__(self) -> None:
        if self.tick_seconds < MIN_TICK_SECONDS:
            raise ValueError(
                f"tick_seconds 不能小于 {MIN_TICK_SECONDS:g} 秒（页面实测节奏）"
            )
        if self.learning_duration < 1:
            raise ValueError("learning_duration 必须是正整数秒")
        if self.refresh_every < 1:
            raise ValueError("refresh_every 至少为 1")
        if self.max_ticks < 1:
            raise ValueError("max_ticks 至少为 1")
        if self.max_total_seconds < self.learning_duration:
            raise ValueError("max_total_seconds 至少要容纳一次上报")
        if self.ratio_stall_checks < 1:
            raise ValueError("ratio_stall_checks 至少为 1")


@dataclass(slots=True)
class ProgressTick:
    """一次上报的记录。``ratio`` 只有轮到刷新详情的那次才有值。"""

    index: int
    sent_seconds: int
    total_seconds: int
    ratio: float | None = None

    def as_line(self) -> str:
        ratio = "—" if self.ratio is None else f"{self.ratio * 100:.0f}%"
        return f"#{self.index} 累计 {self.total_seconds}s 进度 {ratio}"


@dataclass(slots=True)
class ProgressReport:
    """一次「刷课」的结果 —— 发没发、发了多少、进度动没动。"""

    course_id: str
    material_id: str
    dry_run: bool
    ratio_before: float
    ratio_after: float
    stopped: str
    #: dry-run 时算出来的计划时长（真实发送时为 0）。
    planned_seconds: int = 0
    sent_seconds: int = 0
    ticks: list[ProgressTick] = field(default_factory=list)

    @property
    def ratio_delta(self) -> float:
        return round(self.ratio_after - self.ratio_before, 4)

    def summary(self) -> str:
        before = f"{self.ratio_before * 100:.0f}%"
        after = f"{self.ratio_after * 100:.0f}%"
        if self.dry_run:
            return (
                f"[dry-run] 材料 {self.material_id}：进度 {before}，"
                f"计划上报 {self.planned_seconds} 秒（约 "
                f"{self.planned_seconds // max(PAGE_LEARNING_DURATION, 1)} 次 / "
                f"{self.planned_seconds} 秒墙钟），未发送任何请求。"
            )
        return (
            f"材料 {self.material_id}：进度 {before} → {after}"
            f"（{self.ratio_delta:+.2%}），已上报 {self.sent_seconds} 秒 / "
            f"{len(self.ticks)} 次，停止原因 {self.stopped}。"
        )


DetailFetcher = Callable[[], Awaitable[PincoMaterialDetail]]


class LearningReporter:
    """按页面节奏上报学习时长，并盯着进度有没有真的动。

    只依赖两个东西：一个能发 action 的 client（真实实现是
    :class:`~kedaibiao.platforms.pinco.client.PincoClient`），和一个能重取材料
    详情的函数。这样单测可以完全不联网、不 sleep（注入假的 ``sleep``）。
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

    def _payload(self, course_id: str, material_id: str) -> dict[str, Any]:
        return {
            ep.FIELD_COURSE_ID: course_id,
            ep.FIELD_MATERIAL_ID: material_id,
            ep.FIELD_LEARNING_DURATION: self.policy.learning_duration,
        }

    def _planned_seconds(self) -> int:
        """dry-run 时的计划总量：次数与总时长两个上限取小。"""
        ticks = min(
            self.policy.max_ticks,
            self.policy.max_total_seconds // self.policy.learning_duration,
        )
        return ticks * self.policy.learning_duration

    @staticmethod
    def _counts_progress(initial: PincoMaterialDetail) -> bool:
        raw = initial.raw if isinstance(initial.raw, dict) else {}
        return bool(raw.get(ep.FIELD_COUNT_LEARNING_PROGRESS))

    # -- 主流程 -------------------------------------------------------------

    async def run(
        self,
        *,
        course_id: str,
        material_id: str,
        initial: PincoMaterialDetail,
        fetch_detail: DetailFetcher,
        dry_run: bool = True,
        target_ratio: float = 1.0,
        on_tick: Callable[[ProgressTick], None] | None = None,
    ) -> ProgressReport:
        """上报学习时长。

        ``dry_run=True``（默认）只算计划；``dry_run=False`` 才会真的发请求。
        ``target_ratio`` 默认 1.0（学满）；已达标就一次都不发。
        """
        if not self._counts_progress(initial):
            raise NotSupportedError(
                f"材料 {material_id} 的 countLearningProgress 不为真 —— "
                "平台不统计它的学习进度，上报时长没有意义。"
            )

        ratio_before = self._clamp(initial.progress)
        report = ProgressReport(
            course_id=course_id,
            material_id=material_id,
            dry_run=dry_run,
            ratio_before=ratio_before,
            ratio_after=ratio_before,
            stopped=STOP_DRY_RUN,
        )

        if ratio_before >= target_ratio:
            report.stopped = STOP_TARGET_REACHED
            log.info(
                "材料 %s 进度已是 %.0f%%，达到目标，不发上报",
                material_id,
                ratio_before * 100,
            )
            return report

        if dry_run:
            report.planned_seconds = self._planned_seconds()
            log.info(
                "材料 %s dry-run：计划上报 %s 秒（未发送）",
                material_id,
                report.planned_seconds,
            )
            return report

        return await self._run_live(
            report=report,
            course_id=course_id,
            material_id=material_id,
            fetch_detail=fetch_detail,
            target_ratio=target_ratio,
            on_tick=on_tick,
        )

    async def _run_live(
        self,
        *,
        report: ProgressReport,
        course_id: str,
        material_id: str,
        fetch_detail: DetailFetcher,
        target_ratio: float,
        on_tick: Callable[[ProgressTick], None] | None,
    ) -> ProgressReport:
        policy = self.policy
        payload = self._payload(course_id, material_id)
        ratio = report.ratio_before
        last_checked = ratio
        stalled = 0
        sent = 0
        index = 0

        while True:
            if index >= policy.max_ticks:
                report.stopped = STOP_MAX_TICKS
                break
            if sent + policy.learning_duration > policy.max_total_seconds:
                report.stopped = STOP_MAX_SECONDS
                break

            index += 1
            # dangerous=True：进度上报是写操作，客户端闸门要求显式放行。
            await self._client.post_action(
                ep.ACTION_LEARNING_DURATION_REPORT, payload, dangerous=True
            )
            sent += policy.learning_duration
            tick = ProgressTick(
                index=index, sent_seconds=policy.learning_duration, total_seconds=sent
            )

            last_possible = sent + policy.learning_duration > policy.max_total_seconds
            check_now = index % policy.refresh_every == 0 or last_possible
            if check_now:
                detail = await fetch_detail()
                ratio = self._clamp(detail.progress)
                tick.ratio = ratio
                report.ratio_after = ratio
            report.ticks.append(tick)
            if on_tick is not None:
                on_tick(tick)

            if check_now:
                if ratio >= target_ratio:
                    report.stopped = STOP_TARGET_REACHED
                    break
                if ratio > last_checked + RATIO_EPSILON:
                    stalled = 0
                    last_checked = ratio
                    log.info("材料 %s 进度涨到 %.0f%%", material_id, ratio * 100)
                else:
                    stalled += 1
                    if stalled >= policy.ratio_stall_checks:
                        report.stopped = STOP_RATIO_STALLED
                        break

            if index >= policy.max_ticks or last_possible:
                continue
            await self._sleep(policy.tick_seconds)

        report.sent_seconds = sent
        report.ratio_after = self._clamp(report.ratio_after)
        log.info(
            "材料 %s 上报结束：%s 秒 / %s 次，进度 %.0f%% → %.0f%%，停止原因 %s",
            material_id,
            sent,
            len(report.ticks),
            report.ratio_before * 100,
            report.ratio_after * 100,
            report.stopped,
        )
        return report

    @staticmethod
    def _clamp(value: float) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return 0.0
        return min(max(number, 0.0), 1.0)
