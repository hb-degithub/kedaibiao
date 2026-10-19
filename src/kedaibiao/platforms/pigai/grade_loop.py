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

    def _emit(self, kind: str, **payload: Any) -> None:
        if self.on_event is None:
            return
        try:
            self.on_event({"kind": kind, **payload})
        except Exception:  # noqa: BLE001 - 回调失败不影响主流程
            log.debug("on_event 回调失败", exc_info=True)

    async def run(self, *, rid: str, topic: str, initial_essay: str) -> GradeLoopResult:
        from kedaibiao.platforms.pigai import grading

        result = GradeLoopResult(rid=rid, target_score=self.target_score,
                                 stop_reason=STOP_ERROR)
        essay = initial_essay
        eid = ""

        for attempt in range(1, self.max_attempts + 1):
            self._emit("attempt.start", attempt=attempt, word_count=_word_count(essay))
            record = AttemptRecord(attempt=attempt, essay_text=essay)

            try:
                # 交
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

                # 等分
                snapshot = await grading.wait_for_grade(
                    self.adapter, self.http, base=self.base, rid=rid, eid=eid,
                    timeout=self.analysis_timeout,
                    on_tick=lambda i, s, _t: self._emit("attempt.analysis",
                                                        attempt=attempt, tick=i, state=s),
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

            if record.graded and record.score is not None:
                if result.best_score is None or record.score > result.best_score:
                    result.best_score = record.score
            self._emit("attempt.graded", attempt=attempt, score=record.score,
                       graded=record.graded, raw_state=record.raw_state,
                       feedback_count=len(record.feedback))

            # 判停
            if record.graded and record.score is not None and record.score >= self.target_score:
                result.stop_reason = STOP_REACHED
                result.message = (
                    f"第 {attempt} 轮达到目标分：{record.score:g} >= {self.target_score:g}"
                )
                break
            if not record.graded:
                result.stop_reason = STOP_UNAVAILABLE
                result.message = (
                    f"平台未给出分数（{record.raw_state or '待分析'}）。"
                    "未到截止时间的作业不公布分数 —— 这不是失败，继续交也不会有分。"
                )
                break
            if attempt >= self.max_attempts:
                result.stop_reason = STOP_EXHAUSTED
                result.message = (
                    f"{attempt} 轮用尽，最高 {result.best_score:g}，"
                    f"未达目标 {self.target_score:g}"
                )
                break

            # 改写进入下一轮
            if self.rewrite is None:
                result.stop_reason = STOP_EXHAUSTED
                result.message = "没有配置改写器，无法继续提高分数"
                break
            self._emit("attempt.rewriting", attempt=attempt)
            try:
                essay = await self.rewrite(snapshot, essay, attempt + 1)
            except Exception as exc:  # noqa: BLE001
                result.stop_reason = STOP_ERROR
                result.message = f"改写失败：{type(exc).__name__}: {exc}"
                break
            if not essay or not essay.strip():
                result.stop_reason = STOP_ERROR
                result.message = "改写返回了空正文"
                break

        self._emit("loop.finish", stop_reason=result.stop_reason,
                   best_score=result.best_score, message=result.message)
        return result

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
