"""可复核的提交计划：逐题题干 / 答案 / 置信度 / 来源 + 内容哈希。

计划是本包的安全核心，承担三件事：

1. **给人看**（:meth:`PincoPlan.render_text`）—— 用户复核的就是这一段；
2. **给机器校验**（``digest`` / ``submission_digest``）—— 提交内容的规范化
   哈希。复核之后计划被改过、或提交时传入的内容与计划不同，哈希就对不上，
   提交一律拒绝；
3. **区分「能交」与「要人」** —— 图片题、没真答出来的题进 ``NEEDS_HUMAN``，
   并且**不会**出现在可提交集合里（票据只发给 ``PLANNED``）。

``submission_digest(assignment, answers)`` 与 ``PincoAssignmentPlan`` 的哈希
用同一套规范化算法，所以「复核过的那份」与「真要交的那份」能逐字节比对。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from kedaibiao.core.models import (
    Answer,
    AnswerSource,
    Assignment,
    QuestionType,
    TaskKind,
)
from kedaibiao.platforms.pinco import question_map
from kedaibiao.platforms.pinco.errors import PincoError
from kedaibiao.platforms.pinco.models import PlanState


class PlanTamperedError(PincoError):
    """计划内容与自身哈希不一致 —— 复核之后被改过，拒绝提交。"""


# --------------------------------------------------------------------------
# 规范化哈希
# --------------------------------------------------------------------------


def _question_payload(
    *,
    remote_id: str | None,
    stem: str,
    type_name: str,
    options: list[str],
    choices: list[int],
    text: str,
    question_raw: dict[str, Any],
    answer_raw: dict[str, Any],
) -> dict[str, Any]:
    """一道题参与哈希的部分。

    ``question_raw`` / ``answer_raw`` 也进哈希：有些平台提交时要把题目或答案
    的原始载荷原样回传（例如加密封装），那部分同样是「要交的内容」，
    只哈希题干会让它们成为绕过复核的后门。
    """
    return {
        "remote_id": remote_id,
        "stem": stem,
        "type": type_name,
        "options": list(options),
        "choices": list(choices),
        "text": text,
        "question_raw": question_raw,
        "answer_raw": answer_raw,
    }


def _digest(assignment_id: str, title: str, kind: str, questions: list[dict[str, Any]]) -> str:
    blob = json.dumps(
        {
            "assignment_id": assignment_id,
            "title": title,
            "kind": kind,
            "questions": questions,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


def submission_digest(assignment: Assignment, answers: list[Answer]) -> str:
    """「这次真要提交的内容」的规范化哈希。复核与提交共用同一个算法。"""
    return _digest(
        assignment.remote_id,
        assignment.title,
        assignment.kind.value,
        [
            _question_payload(
                remote_id=answer.question.remote_id,
                stem=answer.question.stem,
                type_name=answer.question.type.value,
                options=list(answer.question.options),
                choices=list(answer.choices),
                text=answer.text,
                question_raw=dict(answer.question.raw),
                answer_raw=dict(answer.raw),
            )
            for answer in answers
        ],
    )


# --------------------------------------------------------------------------
# 计划结构
# --------------------------------------------------------------------------


@dataclass(slots=True)
class PincoQuestionPlan:
    """计划里的一道题。"""

    index: int
    stem: str
    type: QuestionType = QuestionType.UNKNOWN
    options: list[str] = field(default_factory=list)
    remote_id: str | None = None
    choices: list[int] = field(default_factory=list)
    text: str = ""
    source: AnswerSource = AnswerSource.UNKNOWN
    confidence: float = 0.0
    needs_human: bool = False
    reason: str = ""
    #: 「为什么需要人工」的稳定理由码（answer_requires_image / stem_has_image /
    #: stem_has_formula_image），按优先级排列。计划里要按这三类分开列。
    human_reasons: list[str] = field(default_factory=list)
    image_required: bool = False
    question_raw: dict[str, Any] = field(default_factory=dict)
    answer_raw: dict[str, Any] = field(default_factory=dict)

    @property
    def has_answer(self) -> bool:
        return bool(self.choices) or bool(self.text.strip()) or bool(self.answer_raw)

    def answer_label(self) -> str:
        """人类可读答案：能翻成选项文字就翻，翻不了退回序号。"""
        if self.choices:
            picked = [self.options[i] for i in self.choices if 0 <= i < len(self.options)]
            if len(picked) == len(self.choices):
                return " / ".join(picked)
            return "选项 " + ", ".join(str(i + 1) for i in self.choices)
        if self.text.strip():
            return self.text.strip()
        if self.answer_raw:
            return "(平台原始答案载荷，见计划 JSON)"
        return "(未作答)"

    def as_payload(self) -> dict[str, Any]:
        return _question_payload(
            remote_id=self.remote_id,
            stem=self.stem,
            type_name=self.type.value,
            options=self.options,
            choices=self.choices,
            text=self.text,
            question_raw=self.question_raw,
            answer_raw=self.answer_raw,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "stem": self.stem,
            "type": self.type.value,
            "options": list(self.options),
            "remote_id": self.remote_id,
            "choices": list(self.choices),
            "text": self.text,
            "source": self.source.value,
            "confidence": self.confidence,
            "needs_human": self.needs_human,
            "reason": self.reason,
            "human_reasons": list(self.human_reasons),
            "image_required": self.image_required,
            "question_raw": self.question_raw,
            "answer_raw": self.answer_raw,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PincoQuestionPlan:
        return cls(
            index=int(data.get("index", 0)),
            stem=str(data.get("stem", "")),
            type=_enum_or(QuestionType, data.get("type"), QuestionType.UNKNOWN),
            options=[str(o) for o in data.get("options") or []],
            remote_id=data.get("remote_id"),
            choices=[int(c) for c in data.get("choices") or []],
            text=str(data.get("text", "")),
            source=_enum_or(AnswerSource, data.get("source"), AnswerSource.UNKNOWN),
            confidence=float(data.get("confidence") or 0.0),
            needs_human=bool(data.get("needs_human")),
            reason=str(data.get("reason", "")),
            human_reasons=[str(code) for code in data.get("human_reasons") or []],
            image_required=bool(data.get("image_required")),
            question_raw=dict(data.get("question_raw") or {}),
            answer_raw=dict(data.get("answer_raw") or {}),
        )


@dataclass(slots=True)
class PincoAssignmentPlan:
    """计划里的一个作业（或学习任务）。"""

    platform: str
    assignment_id: str
    title: str
    course_name: str = ""
    kind: TaskKind = TaskKind.HOMEWORK
    state: PlanState = PlanState.PLANNED
    reason: str = ""
    #: 学习进度（0.0~1.0）。只有学习任务（课程材料）会填。
    progress: float = 0.0
    questions: list[PincoQuestionPlan] = field(default_factory=list)
    attempts: int = 0

    @property
    def needs_human(self) -> bool:
        return self.state is PlanState.NEEDS_HUMAN

    @property
    def human_questions(self) -> list[PincoQuestionPlan]:
        return [q for q in self.questions if q.needs_human]

    def content_digest(self) -> str:
        """本作业的提交内容哈希。"""
        return _digest(
            self.assignment_id,
            self.title,
            self.kind.value,
            [q.as_payload() for q in self.questions],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "assignment_id": self.assignment_id,
            "title": self.title,
            "course_name": self.course_name,
            "kind": self.kind.value,
            "state": self.state.value,
            "reason": self.reason,
            "progress": self.progress,
            "attempts": self.attempts,
            "questions": [q.to_dict() for q in self.questions],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PincoAssignmentPlan:
        return cls(
            platform=str(data.get("platform", "")),
            assignment_id=str(data.get("assignment_id", "")),
            title=str(data.get("title", "")),
            course_name=str(data.get("course_name", "")),
            kind=_enum_or(TaskKind, data.get("kind"), TaskKind.HOMEWORK),
            state=_enum_or(PlanState, data.get("state"), PlanState.FAILED),
            reason=str(data.get("reason", "")),
            progress=float(data.get("progress") or 0.0),
            questions=[PincoQuestionPlan.from_dict(q) for q in data.get("questions") or []],
            attempts=int(data.get("attempts") or 0),
        )


@dataclass(slots=True)
class PincoPlan:
    """一份完整的、可落盘、可校验的提交计划。"""

    platform: str = "pinco"
    display_name: str = ""
    created_at: datetime = field(default_factory=datetime.now)
    assignments: list[PincoAssignmentPlan] = field(default_factory=list)
    #: 计划整体哈希（不含本字段自身）。``seal()`` 写入，``verify()`` 校验。
    digest: str = ""

    # -- 查询 ---------------------------------------------------------------

    @property
    def label(self) -> str:
        return self.display_name or self.platform

    @property
    def planned(self) -> list[PincoAssignmentPlan]:
        return [a for a in self.assignments if a.state is PlanState.PLANNED]

    @property
    def needs_human(self) -> list[PincoAssignmentPlan]:
        return [a for a in self.assignments if a.state is PlanState.NEEDS_HUMAN]

    @property
    def skipped(self) -> list[PincoAssignmentPlan]:
        return [a for a in self.assignments if a.state is PlanState.SKIPPED]

    @property
    def failed(self) -> list[PincoAssignmentPlan]:
        return [a for a in self.assignments if a.state is PlanState.FAILED]

    def find(self, assignment_id: str) -> PincoAssignmentPlan | None:
        for item in self.assignments:
            if item.assignment_id == assignment_id:
                return item
        return None

    def human_reason_counts(self) -> dict[str, int]:
        """按「为什么需要人工」的理由码统计题目数（三类分开，互不吞并）。

        顺序固定为 ``question_map.REASON_PRIORITY``，渲染时不必再排。
        """
        counts: dict[str, int] = {}
        for assignment in self.assignments:
            for question in assignment.questions:
                for code in question.human_reasons:
                    counts[code] = counts.get(code, 0) + 1
        return {code: counts[code] for code in question_map.REASON_PRIORITY if code in counts}

    def assignment_digests(self) -> dict[str, str]:
        """作业 ID → 内容哈希。提交守卫比对的就是这张表。"""
        return {a.assignment_id: a.content_digest() for a in self.planned}

    def summary(self) -> str:
        return (
            f"[{self.label}] 计划共 {len(self.assignments)} 项："
            f"可提交 {len(self.planned)}，待人工 {len(self.needs_human)}，"
            f"跳过 {len(self.skipped)}，失败 {len(self.failed)}"
        )

    # -- 完整性 -------------------------------------------------------------

    def compute_digest(self) -> str:
        payload = self.to_dict()
        payload.pop("digest", None)
        blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]

    def seal(self) -> PincoPlan:
        """计算并写入整体哈希。计划落盘、交给用户复核之前必须盖章。"""
        self.digest = self.compute_digest()
        return self

    def verify(self) -> None:
        """校验整体哈希；不一致抛 :class:`PlanTamperedError`。"""
        if not self.digest:
            raise PlanTamperedError("计划尚未盖章（没有内容哈希），拒绝提交。请重新生成计划。")
        if self.compute_digest() != self.digest:
            raise PlanTamperedError(
                "计划内容与内容哈希不一致 —— 复核之后被修改过，拒绝提交。"
                "请重新生成计划并复核。"
            )

    # -- 序列化 -------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "display_name": self.display_name,
            "created_at": self.created_at.isoformat(timespec="seconds"),
            "digest": self.digest,
            "assignments": [a.to_dict() for a in self.assignments],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, verify: bool = True) -> PincoPlan:
        created_raw = data.get("created_at")
        plan = cls(
            platform=str(data.get("platform", "pinco")),
            display_name=str(data.get("display_name", "")),
            created_at=(
                datetime.fromisoformat(str(created_raw)) if created_raw else datetime.now()
            ),
            assignments=[PincoAssignmentPlan.from_dict(a) for a in data.get("assignments") or []],
            digest=str(data.get("digest", "")),
        )
        if verify:
            plan.verify()
        return plan

    def json_text(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n"

    def render_text(self) -> str:
        """人类可读的计划 —— 复核时看的就是这一段。"""
        lines = [
            f"【品课提交计划】{self.label}",
            f"生成时间：{self.created_at.isoformat(timespec='seconds')}",
            f"内容哈希：{self.digest or '<未盖章>'}",
            self.summary(),
            "",
        ]
        if not self.assignments:
            lines.append("（没有抓取到需要处理的作业/学习任务）")
            return "\n".join(lines) + "\n"

        for item in self.assignments:
            lines.append(f"── {item.title}  [{item.assignment_id}]  状态={item.state.value}")
            meta = [f"类型={item.kind.value}"]
            if item.course_name:
                meta.append(f"课程={item.course_name}")
            if item.kind is TaskKind.COURSE or item.progress:
                meta.append(f"学习进度={item.progress:.0%}")
            lines.append("   " + "  ".join(meta))
            if item.reason:
                lines.append(f"   说明：{item.reason}")
            for question in item.questions:
                stem = question.stem.strip() or "(平台未提供题干)"
                lines.append(f"   {question.index + 1}. [{question.type.value}] {stem}")
                if question.options:
                    lines.append("      选项：" + " | ".join(question.options))
                if question.needs_human:
                    codes = "/".join(question.human_reasons) or "其他原因"
                    lines.append(
                        f"      ⚠ 需要人工[{codes}]：{question.reason or '需要人工处理'}"
                    )
                else:
                    lines.append(
                        f"      答案：{question.answer_label()}"
                        f"    （来源={question.source.value}，置信度={question.confidence:.2f}）"
                    )
            lines.append("")

        human = [q for a in self.assignments for q in a.human_questions]
        if human:
            lines.append(f"需要人工处理的题目共 {len(human)} 道（不会被自动提交）：")
            counts = self.human_reason_counts()
            for code in question_map.REASON_PRIORITY:
                if code not in counts:
                    continue
                lines.append(
                    f"  - {question_map.REASON_LABELS[code]}（{code}）：{counts[code]} 道"
                )
            others = len(human) - sum(counts.values())
            if others:
                # 没答出来 / 置信度不足这类「非图片」原因也要有名字，
                # 否则用户会以为剩下的全是图片题。
                lines.append(f"  - 其他原因（没真答出来/置信度不足）：{others} 道")
            lines.append("")
            for question in human:
                codes = "/".join(question.human_reasons) or "其他原因"
                lines.append(
                    f"  · [{codes}] "
                    f"{question.stem.strip() or question.remote_id or '<无题干>'}"
                )
            lines.append("")
        return "\n".join(lines) + "\n"


def _enum_or(enum_cls: type[Any], value: Any, default: Any) -> Any:
    """把 JSON 里的字符串还原成枚举成员；认不出来退回默认值。

    计划文件可能被人手改过，这里不该因为一个坏枚举值整体崩掉 ——
    把守的是 :meth:`PincoPlan.verify` 的内容哈希。
    """
    if isinstance(value, enum_cls):
        return value
    try:
        return enum_cls(value)
    except (ValueError, TypeError):
        return default
