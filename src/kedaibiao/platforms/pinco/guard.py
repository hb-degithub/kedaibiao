"""提交守卫 —— 三条硬规则，全部钉在 ``submit()`` 入口上。

为什么守卫写在平台包里：``submit()`` 是这一层唯一能造成不可撤销后果的调用。
流水线（共享区）可能是别人写的、可能有 bug、也可能被绕过；把「没有人工复核过
的计划就不许提交」钉在真正发出写请求的地方，是最后一道闸门 —— 就算上层全线
失守，写操作也不会自己发出去。

三条硬规则（原因码是稳定字符串，测试与日志都靠它，不要改字面量）：

- **计划已复核且内容一致** —— ``no_reviewed_plan`` / ``assignment_not_in_plan`` /
  ``plan_hash_mismatch``：拦「没复核就提交」「只复核了一部分」「复核后内容被改」；
- **没有待人工或低置信度题** —— ``human_questions_pending`` /
  ``image_answer_guessed`` / ``low_confidence_answer``：拦「图片题没作答」
  「拿文字答案糊弄图片题」「拿启发式答案硬凑」；
- **平台状态复查** —— ``already_submitted`` / ``not_submittable`` /
  ``submission_state_unknown``：拦「重复交卷」「已过期/平台不允许交」，
  以及「状态查不到还想交」。

原因码是稳定字符串，测试与日志都靠它，不要改字面量。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import NoReturn

from kedaibiao.core.models import Answer, AnswerSource
from kedaibiao.platforms.pinco.errors import PincoError
from kedaibiao.platforms.pinco.models import PlanState, PincoSubmissionState
from kedaibiao.platforms.pinco.plan import (
    PincoAssignmentPlan,
    PincoPlan,
    PincoQuestionPlan,
    PlanTamperedError,
)

REFUSAL_NO_REVIEWED_PLAN = "no_reviewed_plan"
REFUSAL_PLATFORM_MISMATCH = "plan_platform_mismatch"
REFUSAL_ASSIGNMENT_NOT_IN_PLAN = "assignment_not_in_plan"
REFUSAL_HASH_MISMATCH = "plan_hash_mismatch"
REFUSAL_HUMAN_QUESTIONS = "human_questions_pending"
REFUSAL_IMAGE_GUESSED = "image_answer_guessed"
REFUSAL_LOW_CONFIDENCE = "low_confidence_answer"
REFUSAL_ALREADY_SUBMITTED = "already_submitted"
REFUSAL_NOT_SUBMITTABLE = "not_submittable"
REFUSAL_STATE_UNKNOWN = "submission_state_unknown"
REFUSAL_ALREADY_SENT = "already_submitted_in_session"


class SubmissionRefused(PincoError):
    """提交被守卫拦下。

    ``reason`` 是机器可读的原因码；``message`` 是给人看的解释。
    ``str(exc)`` 带码，便于日志与测试一眼定位。
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(f"[{reason}] {message}")
        self.reason = reason
        self.message = message


def _refuse(reason: str, message: str) -> NoReturn:
    raise SubmissionRefused(reason, message)


@dataclass(slots=True, frozen=True)
class ReviewedPlan:
    """用户复核过的计划 —— 提交的唯一凭证。

    为什么带整份计划而不是只带哈希：内容一致只是第一道；第二道要看**计划里
    逐题的判定**（图片题、置信度），那必须能拿到题目列表本身。
    ``plan.digest`` 在 :func:`review` 时已校验过，之后任何改动都会被
    :func:`check_reviewed` 重新发现。
    """

    platform: str
    plan: PincoPlan
    digest: str
    reviewed_at: datetime = field(default_factory=datetime.now)

    def digest_for(self, assignment_id: str) -> str | None:
        return self.plan.assignment_digests().get(assignment_id)


def review(plan: PincoPlan) -> ReviewedPlan:
    """登记「人已经复核过这份计划」。

    先 ``plan.verify()``：没盖章或被改过的计划根本不该进入复核环节 ——
    在入口就报错，比等到提交时才失败清楚得多。
    """
    try:
        plan.verify()
    except PlanTamperedError as exc:
        _refuse(REFUSAL_HASH_MISMATCH, str(exc))
    return ReviewedPlan(platform=plan.platform, plan=plan, digest=plan.digest)


# --------------------------------------------------------------------------
# 规则一：计划已复核且内容一致
# --------------------------------------------------------------------------


def check_reviewed(
    reviewed: ReviewedPlan | None,
    *,
    platform: str,
    assignment_id: str,
    actual_digest: str,
) -> PincoAssignmentPlan:
    """校验提交凭证；通过则返回计划里对应的作业条目。"""
    if reviewed is None:
        _refuse(
            REFUSAL_NO_REVIEWED_PLAN,
            "提交被拒绝：没有人工复核过的计划。请先产出计划、确认无误后 "
            "调用 confirm_plan()，再提交。",
        )
    assert reviewed is not None  # 上面的分支已经返回，这里只是给类型检查器看
    if reviewed.platform != platform:
        _refuse(
            REFUSAL_PLATFORM_MISMATCH,
            f"提交被拒绝：复核的计划属于 {reviewed.platform!r}，当前平台是 {platform!r}。",
        )
    try:
        reviewed.plan.verify()
    except PlanTamperedError as exc:
        _refuse(REFUSAL_HASH_MISMATCH, f"提交被拒绝：{exc}")

    item = reviewed.plan.find(assignment_id)
    if item is None:
        _refuse(
            REFUSAL_ASSIGNMENT_NOT_IN_PLAN,
            f"提交被拒绝：作业 {assignment_id!r} 不在复核过的计划里。"
            "请重新生成计划并复核。",
        )
    assert item is not None
    if item.state is PlanState.NEEDS_HUMAN:
        detail = "；".join(f"{_label(q)}（{q.reason}）" for q in item.human_questions)
        _refuse(
            REFUSAL_HUMAN_QUESTIONS,
            f"提交被拒绝：作业 {assignment_id!r} 里有需要人工处理的题，"
            f"整份作业处于待人工状态：{detail or item.reason or '有题目需要人工'}",
        )
    if item.state is not PlanState.PLANNED:
        _refuse(
            REFUSAL_ASSIGNMENT_NOT_IN_PLAN,
            f"提交被拒绝：作业 {assignment_id!r} 在计划里的状态是 {item.state.value}，"
            "只有 planned 的作业才允许提交。",
        )
    expected = item.content_digest()
    if expected != actual_digest:
        _refuse(
            REFUSAL_HASH_MISMATCH,
            "提交被拒绝：本次要提交的内容与复核过的计划不一致"
            f"（计划 {expected}，本次 {actual_digest}）。请重新生成计划并复核。",
        )
    return item


# --------------------------------------------------------------------------
# 规则二：没有待人工 / 低置信度题
# --------------------------------------------------------------------------


def _label(question: PincoQuestionPlan) -> str:
    stem = question.stem.strip() or question.remote_id or f"第 {question.index + 1} 题"
    return stem[:40]


def _answer_for(
    index: int, question: PincoQuestionPlan, answers: list[Answer]
) -> Answer | None:
    """把计划里的题对应到本次要提交的答案。

    先按位置对（流水线按计划顺序重建答案），位置对不上再按平台题目 ID 找。
    两条都对不上就返回 ``None``，由调用方按「没作答」处理 —— 宁可拒绝，
    也不要把别的题的答案安到这道题上。
    """
    if index < len(answers):
        candidate = answers[index]
        if question.remote_id is None or candidate.question.remote_id == question.remote_id:
            return candidate
    if question.remote_id is not None:
        for candidate in answers:
            if candidate.question.remote_id == question.remote_id:
                return candidate
    return None


def check_questions(
    planned: PincoAssignmentPlan,
    answers: list[Answer],
    *,
    min_confidence: float,
) -> None:
    """逐题检查：图片题不许被文字答案顶替、低置信度题不许自动提交。"""
    pending: list[str] = []
    guessed: list[str] = []
    low: list[str] = []

    for index, question in enumerate(planned.questions):
        answer = _answer_for(index, question, answers)
        empty = answer is None or answer.is_empty()
        if question.image_required:
            # 图片题的答案只能是一张图，文字/选项答案一律视为「糊弄」
            if empty:
                pending.append(_label(question))
            else:
                guessed.append(_label(question))
            continue
        if empty:
            pending.append(_label(question))
            continue
        assert answer is not None
        if answer.source is AnswerSource.HEURISTIC:
            low.append(f"{_label(question)}（启发式猜测）")
        elif answer.confidence < min_confidence:
            low.append(f"{_label(question)}（置信度 {answer.confidence:.2f}）")

    if guessed:
        _refuse(
            REFUSAL_IMAGE_GUESSED,
            "提交被拒绝：以下图片作答题出现了非图片答案（程序不能替人作答）："
            + "；".join(guessed),
        )
    if pending:
        _refuse(
            REFUSAL_HUMAN_QUESTIONS,
            "提交被拒绝：以下题目还没有真正作答，需要人工处理：" + "；".join(pending),
        )
    if low:
        _refuse(
            REFUSAL_LOW_CONFIDENCE,
            f"提交被拒绝：以下答案达不到自动提交的置信度门槛（{min_confidence:.2f}）："
            + "；".join(low),
        )


# --------------------------------------------------------------------------
# 规则三：平台状态复查
# --------------------------------------------------------------------------


def check_platform_state(state: PincoSubmissionState | None, *, assignment_id: str) -> None:
    """提交前复查平台状态。

    ``None`` 表示「探测不到」—— 探测不到就**不能提交**。把不知道当成没交过，
    正是「看似失败实则已提交」的成因。

    除了「已提交」，还要看实测出来的另外两个独立标记：``expired`` 与
    ``canSubmitted``。未提交但已过期的作业在平台上根本交不上去，硬交只会
    换来一个业务错误，甚至可能被记一次异常提交。
    """
    if state is None:
        _refuse(
            REFUSAL_STATE_UNKNOWN,
            f"提交被拒绝：无法确认平台上作业 {assignment_id!r} 的提交状态。"
            "状态查不到时不允许提交（可能是网络问题，也可能是响应结构变了）。",
        )
    assert state is not None
    if state.submitted:
        _refuse(
            REFUSAL_ALREADY_SUBMITTED,
            f"提交被拒绝：平台上作业 {assignment_id!r} 已经提交过了，不能重复交卷。",
        )
    if state.expired:
        _refuse(
            REFUSAL_NOT_SUBMITTABLE,
            f"提交被拒绝：平台上作业 {assignment_id!r} 已过期（expired=true），交不上去。",
        )
    if state.can_submit is False:
        _refuse(
            REFUSAL_NOT_SUBMITTABLE,
            f"提交被拒绝：平台显示作业 {assignment_id!r} 当前不可提交"
            "（canSubmitted=false），需人工确认原因。",
        )


def describe_rules(min_confidence: float) -> list[str]:
    """给 CLI / 日志用的一句话规则说明（便于排查「为什么不让交」）。"""
    return [
        "1) 必须存在人工复核过、且内容哈希与本次提交一致的已盖章计划；",
        "2) 计划里不得有未作答的图片题、也不得有低于 "
        f"{min_confidence:.2f} 的启发式/低置信度答案；",
        "3) 提交前必须能查到平台状态，且平台显示该作业尚未提交、未过期、允许提交。",
    ]
