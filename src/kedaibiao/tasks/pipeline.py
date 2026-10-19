"""任务流水线：把「登录 -> 拉课 -> 拉作业 -> 作答 -> 提交 -> 回填题库」串起来。

安全默认：``PipelineConfig.dry_run`` 默认 True。干跑只打印打算提交什么，
不产生任何写操作。要真提交必须显式 ``dry_run=False``。

幂等：同一 ``(platform, assignment_id)`` 已成功过就不再重复提交，
避免重复交卷被判作弊。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from kedaibiao.bank.store import ProgressStore, QuestionBank, record_to_dict
from kedaibiao.core.events import EventBus
from kedaibiao.core.models import (
    Answer,
    Assignment,
    Course,
    Credentials,
    Question,
    RunRecord,
    RunStatus,
    TaskKind,
)
from kedaibiao.core.scheduler import Scheduler
from kedaibiao.notify.base import Notifier, NullNotifier
from kedaibiao.platforms.base import NotSupportedError, PlatformAdapter
from kedaibiao.tasks.answerer import Answerer

log = logging.getLogger(__name__)

# 事件名常量，避免各处硬编码字符串
EVENT_RUN_START = "run.start"
EVENT_RUN_FINISH = "run.finish"
EVENT_ASSIGNMENT_DONE = "assignment.done"
EVENT_ANSWER_LOW_CONFIDENCE = "answer.low_confidence"


class AbortedBeforeSubmit(RuntimeError):
    """在写操作前主动放弃（干跑、置信度不足、已有记录）。"""


@dataclass(slots=True)
class PipelineConfig:
    #: 干跑：只规划不提交
    dry_run: bool = True
    #: 只处理这些类型；空集表示全部
    kinds: frozenset[TaskKind] = frozenset()
    #: 低于该置信度的答案不自动提交
    min_confidence: float = 0.75
    #: 已成功过的作业是否跳过
    skip_finished: bool = True
    #: 作答后是否把答案回写题库（未判分，标记为待验证）
    write_back: bool = True
    #: 单个平台最多处理多少个作业，0 表示不限。用于首次试跑限流。
    max_assignments: int = 0


@dataclass(slots=True)
class PipelineReport:
    platform: str = ""
    #: 展示名，优先用于人类可读的输出
    display_name: str = ""
    records: list[RunRecord] = field(default_factory=list)

    @property
    def label(self) -> str:
        return self.display_name or self.platform

    @property
    def succeeded(self) -> int:
        return sum(1 for r in self.records if r.status is RunStatus.SUCCEEDED)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.records if r.status is RunStatus.FAILED)

    @property
    def skipped(self) -> int:
        return sum(1 for r in self.records if r.status is RunStatus.SKIPPED)

    def summary(self) -> str:
        return (
            f"[{self.label}] 共 {len(self.records)} 项："
            f"成功 {self.succeeded}，失败 {self.failed}，跳过 {self.skipped}"
        )


class Pipeline:
    def __init__(
        self,
        *,
        answerer: Answerer,
        bank: QuestionBank | None = None,
        store: ProgressStore | None = None,
        bus: EventBus | None = None,
        scheduler: Scheduler | None = None,
        notifier: Notifier | None = None,
        config: PipelineConfig | None = None,
        owner_id: int = 0,
    ) -> None:
        self.answerer = answerer
        self.bank = bank
        self.store = store
        self.bus = bus or EventBus()
        self.scheduler = scheduler or Scheduler()
        self.notifier = notifier or NullNotifier()
        self.config = config or PipelineConfig()
        #: 这次跑批产生的数据归谁。0 = 未登录/内部路径。
        self.owner_id = owner_id

    # -- 平台级 -----------------------------------------------------------

    async def run(self, adapter: PlatformAdapter, credentials: Credentials) -> PipelineReport:
        report = PipelineReport(platform=adapter.name, display_name=adapter.display_name)
        adapter.ctx.bus = self.bus  # 让适配器也能发事件

        if not await adapter.refresh_session(credentials):
            report.records.append(
                RunRecord(
                    platform=adapter.name,
                    assignment_id="<login>",
                    status=RunStatus.FAILED,
                    error="登录失败",
                )
            )
            await self.notifier.send(
                f"{adapter.display_name or adapter.name} 登录失败",
                "请检查凭据或验证码状态。",
                level="error",
            )
            return report

        assignments = await self._collect(adapter)
        if self.config.max_assignments:
            assignments = assignments[: self.config.max_assignments]

        results = await self.scheduler.map(
            assignments,
            lambda a: self._process(adapter, a),
            key=adapter.name,
        )
        for item in results:
            if isinstance(item, BaseException):
                report.records.append(
                    RunRecord(
                        platform=adapter.name,
                        assignment_id="<unknown>",
                        status=RunStatus.FAILED,
                        error=f"{type(item).__name__}: {item}",
                    )
                )
            else:
                report.records.append(item)  # type: ignore[arg-type]

        await self.notifier.send(
            f"{adapter.display_name or adapter.name} 跑批结束",
            report.summary(),
            level="error" if report.failed else "success",
        )
        return report

    async def _collect(self, adapter: PlatformAdapter) -> list[Assignment]:
        """把该平台下所有课程的所有作业摊平。"""
        assignments: list[Assignment] = []
        try:
            courses: list[Course] = await adapter.list_courses()
        except NotSupportedError:
            return assignments

        for course in courses:
            try:
                items = await adapter.list_assignments(course)
            except NotSupportedError:
                continue
            for item in items:
                if not item.course_name:
                    item.course_name = course.name
                assignments.append(item)

        if self.config.kinds:
            assignments = [a for a in assignments if a.kind in self.config.kinds]
        return assignments

    # -- 作业级 -----------------------------------------------------------

    async def _process(self, adapter: PlatformAdapter, assignment: Assignment) -> RunRecord:
        record = RunRecord(platform=adapter.name, assignment_id=assignment.remote_id)

        if self.config.skip_finished and assignment.finished:
            record.status = RunStatus.SKIPPED
            record.detail["reason"] = "平台已标记完成"
            return record
        if (
            self.config.skip_finished
            and self.store is not None
            and self.store.already_done(adapter.name, assignment.remote_id)
        ):
            record.status = RunStatus.SKIPPED
            record.detail["reason"] = "本地已成功记录"
            return record

        record.status = RunStatus.RUNNING
        record.started_at = datetime.now()
        run_id = self.store.start_run(record, owner_id=self.owner_id) if self.store else 0
        await self.bus.emit(
            EVENT_RUN_START, platform=adapter.name, assignment=assignment.title
        )

        try:
            await self._solve_and_submit(adapter, assignment, record)
            record.status = RunStatus.SUCCEEDED
        except AbortedBeforeSubmit as exc:
            record.status = RunStatus.SKIPPED
            record.detail["reason"] = str(exc)
        except Exception as exc:  # noqa: BLE001 - 单题失败不拖垮整批
            record.status = RunStatus.FAILED
            record.error = f"{type(exc).__name__}: {exc}"
            adapter.log.exception("作业处理失败: %s", assignment.title)
        finally:
            record.finished_at = datetime.now()
            if self.store and run_id:
                self.store.finish_run(
                    run_id,
                    record.status,
                    error=record.error,
                    attempts=record.attempts,
                    detail=record.detail,
                )
            await self.bus.emit(EVENT_ASSIGNMENT_DONE, **record_to_dict(record))

        return record

    async def _solve_and_submit(
        self, adapter: PlatformAdapter, assignment: Assignment, record: RunRecord
    ) -> None:
        questions: list[Question] = list(assignment.questions)
        if not questions and adapter.capabilities.fetch_questions:
            questions = await adapter.fetch_questions(assignment)
            record.attempts += 1
        record.detail["question_count"] = len(questions)

        if not questions:
            raise AbortedBeforeSubmit("没有取到题目")

        answers: list[Answer] = await self.answerer.answer(questions)
        answerable = [a for a in answers if not a.is_empty()]
        record.detail["answered"] = len(answerable)
        record.detail["avg_confidence"] = (
            round(sum(a.confidence for a in answerable) / len(answerable), 4) if answerable else 0.0
        )
        record.detail["sources"] = _count_sources(answers)

        if not answerable:
            raise AbortedBeforeSubmit("一题都没答出来")

        weakest = min(a.confidence for a in answerable)
        if weakest < self.config.min_confidence:
            await self.bus.emit(
                EVENT_ANSWER_LOW_CONFIDENCE,
                platform=adapter.name,
                assignment=assignment.title,
                weakest=weakest,
            )
            raise AbortedBeforeSubmit(
                f"最低置信度 {weakest:.2f} < 阈值 {self.config.min_confidence:.2f}"
            )

        if self.config.dry_run:
            record.detail["dry_run"] = True
            record.detail["plan"] = [
                {"q": q.stem[:60], "choices": a.choices, "text": a.text, "conf": a.confidence}
                for q, a in zip(questions, answers, strict=True)
            ]
            raise AbortedBeforeSubmit("dry_run=True，未提交")

        result = await adapter.submit(assignment, answers)
        record.detail["submit"] = {"ok": result.ok, "score": result.score, "msg": result.message}
        if self.store:
            self.store.record_submission(adapter.name, result, owner_id=self.owner_id)
        if not result.ok:
            raise RuntimeError(f"提交被拒: {result.message}")

        if self.config.write_back and self.bank is not None:
            self._write_back(questions, answers, result, platform=adapter.name)

    def _write_back(
        self,
        questions: list[Question],
        answers: list[Answer],
        result: Any,
        *,
        platform: str = "",
    ) -> None:
        """把这次作答写进题库。

        只有当平台在返回值里明确给了每题对错时，才标记 ``correct``；
        否则一律留空（待验证），避免污染题库。
        """
        assert self.bank is not None
        verdicts = _extract_verdicts(result)
        skipped = 0
        for q, a in zip(questions, answers, strict=True):
            if a.is_empty():
                continue
            if not q.is_identifiable:
                # 无题干又无平台 ID 的题入库会互相覆盖，宁可丢掉这一条
                skipped += 1
                continue
            correct = verdicts.get(q.remote_id) if q.remote_id else None
            self.bank.upsert(
                q, a, correct=correct, platform=platform, owner_id=self.owner_id
            )
        if skipped:
            log.warning(
                "题库回写跳过 %d 道无法唯一标识的题目（无题干且无平台 ID）", skipped
            )


def _count_sources(answers: list[Answer]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for a in answers:
        counts[a.source.value] = counts.get(a.source.value, 0) + 1
    return counts


def _extract_verdicts(result: Any) -> dict[str, bool]:
    """从平台返回值里抽取「题目 ID -> 是否正确」。

    各平台格式不同，这里只认一种约定格式：``raw["questions"]`` 是
    ``[{id, correct}]`` 列表。不匹配就返回空字典 —— 宁可不回填。
    """
    raw = getattr(result, "raw", None) or {}
    items = raw.get("questions")
    if not isinstance(items, list):
        return {}
    verdicts: dict[str, bool] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        qid = item.get("id") or item.get("questionId")
        if qid is None or "correct" not in item:
            continue
        verdicts[str(qid)] = bool(item["correct"])
    return verdicts
