"""按目标分数自动「写 → 交 → 看批注 → 改写 → 再交」的闭环。

## 一轮做什么

1. 拿到（或生成）一篇正文；
2. 提交（首交 ``eid`` 为空，改稿带上一次稿号）；
3. 触发分析并轮询到出分；
4. 记录这一轮的分数、批注、维度；
5. 没到目标分就**按批注改写**，进入下一轮；
6. 到目标分 / 用尽轮次 / 分数不可得（未截止）时停止。

## 停止的三类原因（必须区分）

- ``reached``：达到目标分，成功；
- ``exhausted``：轮次用完还没到；
- ``unavailable``：平台不给分（**未截止**）或分析超时 —— 这不是失败，
  继续重试只会白交作文。

## 为什么不在提交后立刻再提交

同一篇作业连续提交会覆盖/新增稿次，而平台对「重复提交」的策略未完全确认
（规格 §4.7）。所以每轮之间都要**真的拿到上一轮分数**，否则不进入下一轮。

## 触发方式

控制台里跑在后台线程（分析每轮要 1–2 分钟，多轮可能十几分钟），
进度通过 ``on_event`` 回调实时推给界面。
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable

log = logging.getLogger(__name__)

#: 停止原因
STOP_REACHED = "reached"
STOP_EXHAUSTED = "exhausted"
STOP_UNAVAILABLE = "unavailable"
STOP_ERROR = "error"
#: 等批改出分超时 —— 不是写作失败：作文已经交上去了，只是平台的批改队列还没出分。
#: 用户可以「继续等待 N 秒」或「手动获取一次」，不必重新提交。
STOP_TIMEOUT = "timeout"

REWRITE_SYSTEM = """你是一位英语写作导师。你会收到一篇英语作文、它的机器评分与批注。
请按照批注**改写**这篇作文，使其更符合评分标准。

要求：
- 保留原主题与主要观点，不要跑题；
- 逐条修掉批注里指出的问题（语法、搭配、词汇、句子结构）；
- 提升词汇丰富度与句式多样性，但不要堆砌生僻词；
- 保持原来的字数区间（会明确给出）；
- 只输出改写后的英文正文，不要任何解释、标题、markdown 或引号。"""


@dataclass(slots=True)
class AttemptRecord:
    """一轮的记录。"""

    attempt: int
    eid: str = ""
    score: float | None = None
    full_marks: float | None = None
    graded: bool = False
    raw_state: str = ""
    essay_text: str = ""
    feedback: list[str] = field(default_factory=list)
    dimensions: list[dict[str, Any]] = field(default_factory=list)
    overall: str = ""
    note: str = ""

    @property
    def score_label(self) -> str:
        if self.graded and self.score is not None:
            return f"{self.score:g}"
        return self.raw_state or "未出分"

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempt": self.attempt,
            "eid": self.eid,
            "score": self.score,
            "full_marks": self.full_marks,
            "graded": self.graded,
            "raw_state": self.raw_state,
            "score_label": self.score_label,
            "essay_text": self.essay_text,
            "word_count": _word_count(self.essay_text),
            "feedback": list(self.feedback),
            "dimensions": list(self.dimensions),
            "overall": self.overall,
            "note": self.note,
        }


@dataclass(slots=True)
class GradeLoopResult:
    rid: str
    target_score: float
    stop_reason: str
    best_score: float | None = None
    final_eid: str = ""
    attempts: list[AttemptRecord] = field(default_factory=list)
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "rid": self.rid,
            "target_score": self.target_score,
            "stop_reason": self.stop_reason,
            "best_score": self.best_score,
            "final_eid": self.final_eid,
            "message": self.message,
            "attempts": [a.to_dict() for a in self.attempts],
        }


def _word_count(text: str) -> int:
    return len(re.findall(r"[A-Za-z][A-Za-z'\-]*", text or ""))


def build_rewrite_prompt(
    *,
    topic: str,
    word_range: str,
    essay: str,
    snapshot: Any,
    attempt: int,
) -> str:
    """把「上一轮的结果 + 批注」拼成改写指令。"""
    data = snapshot.to_dict() if hasattr(snapshot, "to_dict") else dict(snapshot)
    lines = [
        f"第 {attempt} 轮改写。",
        f"题目/主题：{topic or '(未提供)'}",
        f"字数要求：{word_range or '(未提供)'}",
        f"当前字数：{_word_count(essay)}",
        f"当前得分：{data.get('score')} / {data.get('full_marks')}"
        f"（得分率 {data.get('score_pct')}）",
    ]
    dims = data.get("dimensions") or []
    if dims:
        lines.append("各维度得分率：" + "，".join(
            f"{d.get('name')} {d.get('ratio')}" for d in dims
        ))
    weak = data.get("weak_dimensions") or []
    if weak:
        lines.append("最需要提升的维度：" + "、".join(str(x) for x in weak))
    overall = data.get("overall_comment") or ""
    if overall:
        lines.append(f"总评：{overall}")
    feedback = data.get("feedback_lines") or []
    if feedback:
        lines.append("批注（逐条修掉）：")
        lines.extend(f"- {item}" for item in feedback)
    lines.extend(["", "原文：", essay])
    return "\n".join(lines)


class GradeLoop:
    """跑一轮到多轮。所有平台交互都通过注入的 adapter / http，便于离线测试。"""

    def __init__(
        self,
        *,
        adapter: Any,
        http: Any,
        base: str,
        target_score: float,
        max_attempts: int = 3,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        rewrite: Callable[[Any, str, int], Any] | None = None,
        word_range: str = "",
        analysis_timeout: float = 180.0,
        resubmit_without_score: bool = False,
    ) -> None:
        self.adapter = adapter
        self.http = http
        self.base = base
        self.target_score = float(target_score)
        self.max_attempts = max(1, int(max_attempts))
        self.on_event = on_event
        self.rewrite = rewrite
        self.word_range = word_range
        self.analysis_timeout = analysis_timeout
        #: 拿不到分数时也允许「改写后重交」。
        #: 批改网有些作业老师开了提前批改：列表写「未截止」但详情页早就有分，
        #: 而且允许反复提交修改 —— 也有真的还没出分、但学生想继续改的情况。
        self.resubmit_without_score = bool(resubmit_without_score)

    def _emit(self, kind: str, **payload: Any) -> None:
        if self.on_event is None:
            return
        try:
            self.on_event({"kind": kind, **payload})
        except Exception:  # noqa: BLE001 - 回调失败不影响主流程
            log.debug("on_event 回调失败", exc_info=True)

    async def run(self, *, rid: str, topic: str, initial_essay: str) -> GradeLoopResult:
        """跑一轮「取分 → 看分 → 决定是否改写重交」的循环。

        **顺序很重要**：先读已有稿件的分数并展示，分够了就停、绝不重交；
        只有「确实低于目标分」且「拿到了批注」时，才会改写后再提交。
        """
        from kedaibiao.platforms.pigai import grading

        result = GradeLoopResult(rid=rid, target_score=self.target_score,
                                 stop_reason=STOP_ERROR)
        essay = initial_essay
        eid = ""
        current: Any = None

        # ① 先看有没有已经交过的稿子（有就先取分，不提交）
        existing = await self._probe_existing(rid)
        if existing is not None:
            eid = str(getattr(existing, "eid", "") or "")
            state = str(getattr(existing, "score_raw", "") or "")
            self._emit("attempt.existing", eid=eid, raw_state=state,
                       submit_count=getattr(existing, "submit_count", 0))
            try:
                current = await grading.fetch_snapshot(self.adapter, rid=rid, eid=eid)
                if not current.graded and current.raw_state in grading.PENDING_STATES:
                    # 交过但还没出分：再等等看（超时不算失败，交给用户决定）
                    current = await grading.wait_for_grade(
                        self.adapter, self.http, base=self.base, rid=rid, eid=eid,
                        timeout=self.analysis_timeout,
                        on_tick=lambda i, st, _t: self._emit(
                            "attempt.analysis", attempt=0, tick=i, state=st),
                    )
            except grading.AnalysisTimeout as exc:
                result.final_eid = eid
                result.stop_reason = STOP_TIMEOUT
                result.message = (
                    f"已有稿件（{eid}）还没出分：{exc}。"
                    "可以「继续等待 N 秒」或「手动获取一次」，不需要重新提交。"
                )
                self._emit("attempt.timeout", attempt=0, eid=eid, error=str(exc))
                self._emit("loop.finish", stop_reason=result.stop_reason,
                           best_score=None, message=result.message)
                return result
            except grading.AnalysisFailed as exc:
                # 平台拒绝触发分析（例如这篇早就批过了）—— 不是失败，按「暂时没分」处理
                self._emit("attempt.error", attempt=0, error=str(exc))
                if current is not None:
                    current.analysis_error = str(exc)
            except Exception as exc:  # noqa: BLE001 - 读不到分就按未出分处理
                self._emit("attempt.error", attempt=0,
                           error=f"{type(exc).__name__}: {exc}")
                current = None

            if current is not None and current.graded:
                record = self._record_from_snapshot(0, current, eid)
                result.attempts.append(record)
                result.final_eid = eid
                result.best_score = record.score
                self._emit("attempt.graded", attempt=0, existing=True,
                           score=record.score, graded=True,
                           raw_state=record.raw_state,
                           feedback_count=len(record.feedback))
                if record.score is not None and record.score >= self.target_score:
                    result.stop_reason = STOP_REACHED
                    result.message = (
                        f"已有稿件 {record.score:g} 分，已达目标 {self.target_score:g} —— "
                        "不需要重新提交。"
                    )
                    self._emit("loop.finish", stop_reason=result.stop_reason,
                               best_score=result.best_score, message=result.message)
                    return result
                # 低于目标：基于原稿改写（拿得到正文就用原稿）
                if current.essay_text.strip():
                    essay = current.essay_text
            elif current is not None and not self.resubmit_without_score:
                result.stop_reason = STOP_UNAVAILABLE
                result.message = (
                    f"已有稿件（{eid}）平台还没给出分数（{current.raw_state or '待分析'}）。"
                    "可以先「手动获取一次」或「继续等待 N 秒」；"
                    "如果你确定这篇可以继续改，点「直接改写重交」。"
                )
                self._emit("loop.finish", stop_reason=result.stop_reason,
                           best_score=None, message=result.message)
                return result
            elif current is not None:
                # 用户明确要求：没分也继续改 → 用现有正文/批注往下走
                self._emit("attempt.resubmit_without_score", eid=eid,
                           raw_state=current.raw_state)

        for attempt in range(1, self.max_attempts + 1):
            # ② 分已经看到了：够了就停，不够才改写重交
            if current is not None and (current.graded or self.resubmit_without_score):
                if self.rewrite is None:
                    result.stop_reason = STOP_EXHAUSTED
                    result.message = (
                        f"当前 {current.score:g} 分，未达目标 {self.target_score:g}；"
                        "没有配置改写器，无法继续提高分数"
                    )
                    break
                self._emit("attempt.rewriting", attempt=attempt,
                           score=current.score if current.graded else None,
                           raw_state=getattr(current, "raw_state", ""))
                try:
                    essay = await self.rewrite(current, essay, attempt)
                except Exception as exc:  # noqa: BLE001
                    result.stop_reason = STOP_ERROR
                    result.message = f"改写失败：{type(exc).__name__}: {exc}"
                    break
                if not essay or not essay.strip():
                    result.stop_reason = STOP_ERROR
                    result.message = "改写返回了空正文"
                    break

            self._emit("attempt.start", attempt=attempt, word_count=_word_count(essay))
            record = AttemptRecord(attempt=attempt, essay_text=essay)

            try:
                from kedaibiao.core.models import Answer, AnswerSource

                assignment = await self._assignment(rid)
                submitted = await self.adapter.submit(
                    assignment,
                    [Answer(question=None, text=essay,
                            source=AnswerSource.LLM, confidence=0.9)],  # type: ignore[arg-type]
                )
                eid = str((submitted.raw or {}).get("eid") or "")
                record.eid = eid
                record.note = submitted.message
                self._emit("attempt.submitted", attempt=attempt, eid=eid,
                           ok=submitted.ok, message=submitted.message)
                if not submitted.ok:
                    record.note = submitted.message or "提交被拒"
                    result.attempts.append(record)
                    result.stop_reason = STOP_ERROR
                    result.message = record.note
                    break

                snapshot = await grading.wait_for_grade(
                    self.adapter, self.http, base=self.base, rid=rid, eid=eid,
                    timeout=self.analysis_timeout,
                    on_tick=lambda i, s2, _t: self._emit("attempt.analysis",
                                                         attempt=attempt, tick=i,
                                                         state=s2),
                )
            except grading.AnalysisTimeout as exc:
                record.note = f"等批改超时：{exc}"
                result.attempts.append(record)
                self._emit("attempt.timeout", attempt=attempt, eid=eid,
                           error=record.note)
                result.stop_reason = STOP_TIMEOUT
                result.final_eid = eid
                result.message = (
                    f"{record.note}。稿件号 {eid} 已提交，"
                    "可以「继续等待 N 秒」或「手动获取一次」，不需要重新提交。"
                )
                break
            except grading.AnalysisFailed as exc:
                # 平台说「没找到作文」之类：不算崩，按「还没出分」处理
                record.note = f"平台拒绝分析：{exc}"
                self._emit("attempt.error", attempt=attempt, error=record.note)
                snapshot = grading.EssaySnapshot(
                    rid=rid, eid=eid, raw_state="分析被拒",
                    analysis_error=str(exc),
                )
            except Exception as exc:  # noqa: BLE001 - 单轮失败不该炸掉整个循环
                record.note = f"{type(exc).__name__}: {exc}"
                result.attempts.append(record)
                self._emit("attempt.error", attempt=attempt, error=record.note)
                result.stop_reason = STOP_ERROR
                result.message = record.note
                break

            data = snapshot.to_dict()
            record.score = data.get("score")
            record.full_marks = data.get("full_marks")
            record.graded = bool(data.get("graded"))
            record.raw_state = str(data.get("raw_state") or "")
            record.feedback = list(data.get("feedback_lines") or [])
            record.dimensions = list(data.get("dimensions") or [])
            record.overall = str(data.get("overall_comment") or "")
            result.attempts.append(record)
            result.final_eid = eid
            current = snapshot

            if record.graded and record.score is not None:
                if result.best_score is None or record.score > result.best_score:
                    result.best_score = record.score
            self._emit("attempt.graded", attempt=attempt, score=record.score,
                       graded=record.graded, raw_state=record.raw_state,
                       feedback_count=len(record.feedback))

            # ③ 看到分之后再决定停 / 继续
            if record.graded and record.score is not None and record.score >= self.target_score:
                result.stop_reason = STOP_REACHED
                result.message = (
                    f"第 {attempt} 轮达到目标分：{record.score:g} >= {self.target_score:g}"
                )
                break
            if not record.graded:
                if self.resubmit_without_score and attempt < self.max_attempts \
                        and self.rewrite is not None:
                    self._emit("attempt.resubmit_without_score", attempt=attempt,
                               raw_state=record.raw_state)
                    continue
                result.stop_reason = STOP_UNAVAILABLE
                reason = getattr(snapshot, "analysis_error", "") or record.raw_state or "待分析"
                result.message = (
                    f"平台还没给出分数（{reason}）。"
                    "可以先「手动获取一次」或「继续等待 N 秒」；"
                    "要接着改就点「直接改写重交」。"
                )
                break
            if attempt >= self.max_attempts:
                result.stop_reason = STOP_EXHAUSTED
                result.message = (
                    f"{attempt} 轮用尽，最高 {result.best_score:g}，"
                    f"未达目标 {self.target_score:g}"
                )
                break

        self._emit("loop.finish", stop_reason=result.stop_reason,
                   best_score=result.best_score, message=result.message)
        return result

    async def _probe_existing(self, rid: str) -> Any:
        """已提交作文列表里属于这个 rid 的最新一行（没有返回 None）。"""
        try:
            rows = await self.adapter.list_finished_essays()
        except Exception:  # noqa: BLE001 - 探测失败就当作没有旧稿
            return None
        for row in rows:
            if str(getattr(row, "rid", "")) == str(rid):
                return row
        return None

    @staticmethod
    def _record_from_snapshot(attempt: int, snapshot: Any, eid: str) -> AttemptRecord:
        data = snapshot.to_dict()
        return AttemptRecord(
            attempt=attempt,
            eid=eid,
            score=data.get("score"),
            full_marks=data.get("full_marks"),
            graded=bool(data.get("graded")),
            raw_state=str(data.get("raw_state") or ""),
            essay_text=str(getattr(snapshot, "essay_text", "") or ""),
            feedback=list(data.get("feedback_lines") or []),
            dimensions=list(data.get("dimensions") or []),
            overall=str(data.get("overall_comment") or ""),
        )

    async def _assignment(self, rid: str) -> Any:
        """把 rid 还原成 Assignment（提交只需要 remote_id 与 title）。"""
        from kedaibiao.core.models import Assignment, TaskKind

        for item in await self.adapter.list_pending_assignments():
            if item.remote_id == rid:
                return item
        for row in await self.adapter.list_finished_essays():
            if row.rid == rid:
                return Assignment(remote_id=rid, title=row.title, kind=TaskKind.HOMEWORK)
        return Assignment(remote_id=rid, title="")
