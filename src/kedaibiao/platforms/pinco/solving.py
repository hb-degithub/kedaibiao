"""抓取之后的作答与计划生成。

三件事必须在同一条链上完成，不能拆散 —— 拆散就会出现「计划里展示的答案」与
「守卫看到的答案」是两份数据的经典事故：

1. **作答**：``BankAnswerer → RuleAnswerer`` 链式降级。每一步的 ``source`` /
   ``confidence`` **原样保留、不修饰** —— 计划里写的来源必须就是答案真实的
   来源，否则守卫按置信度做判断全是假的；
2. **判人工**：图片题（一律不生成答案）、空答案、启发式答案、低于阈值的答案，
   一律标 ``needs_human``；
3. **定状态**：只要有一道题需要人工，整份作业就是 ``NEEDS_HUMAN``。

「答不出就是答不出」在这里落地：启发式答案会保留下来（让人看见程序猜了什么），
但来源标成 ``heuristic``、题目标成 ``needs_human``，于是**永远进不了可提交集合**。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from kedaibiao.core.events import EventBus
from kedaibiao.core.models import Answer, AnswerSource, Assignment, Question, TaskKind
from kedaibiao.notify.base import Notifier
from kedaibiao.platforms.pinco import parsing, question_map
from kedaibiao.platforms.pinco.models import PlanState
from kedaibiao.platforms.pinco.plan import (
    PincoAssignmentPlan,
    PincoPlan,
    PincoQuestionPlan,
)
from kedaibiao.tasks.answerer import Answerer, BankAnswerer, ChainAnswerer, RuleAnswerer

log = logging.getLogger(__name__)

#: 事件名沿用仓库约定 ``<域>.<动作>``（docs/naming.md）。
EVENT_ASSIGNMENT_NEEDS_HUMAN = "assignment.needs_human"
EVENT_QUESTION_NEEDS_HUMAN = "answer.needs_human"


def build_answerer(bank: Any | None, *, min_confidence: float = 0.75) -> Answerer:
    """题库优先、规则兜底的链式答题器。

    顺序不能反：题库命中是「真答案」，规则只是程序的一点直觉。
    ``min_confidence`` 是链式降级的门槛 —— 低于它的答案会被当作没答出来
    交给下一级；最后仍答不出的就是空答案，由计划层标成需人工。
    """
    answerers: list[Answerer] = []
    if bank is not None:
        answerers.append(BankAnswerer(bank))
    answerers.append(RuleAnswerer())
    return ChainAnswerer(answerers, min_confidence=min_confidence)


def failed_course_item(
    *,
    course_id: str,
    course_name: str,
    reason: str,
    platform: str = "pinco",
) -> PincoAssignmentPlan:
    """给「整门课没抓下来」造一条 ``FAILED`` 计划项。

    为什么必须进计划而不是跳过：跳过等于告诉用户「那门课没有作业」。实测坏课程
    （``6569 课程不存在``）就是这样 —— 用户会以为不用管它，而实际上课还在、
    作业也可能在。失败要在复核页面上看得见，``reason`` 要带上平台原话。

    ``kind`` 用 ``HOMEWORK``：这门课的作业与学习任务**都没枚举成功**，无从判断
    它到底有什么；``FAILED`` 项永远不进可提交集合（``PincoPlan.planned`` 只取
    ``PLANNED``），所以这个默认值只影响展示，不会造成误提交。
    """
    name = course_name or course_id
    return PincoAssignmentPlan(
        platform=platform,
        assignment_id=course_id,
        title=name,
        course_name=course_name,
        kind=TaskKind.HOMEWORK,
        state=PlanState.FAILED,
        reason=f"课程「{name}」的作业与学习任务都没取到：{reason}",
    )


@dataclass(slots=True)
class PlanBuilder:
    """把「抓到的一组题」变成「一份可复核的计划」。"""

    answerer: Answerer | None = None
    bus: EventBus | None = None
    notifier: Notifier | None = None
    min_confidence: float = 0.75
    platform: str = "pinco"
    display_name: str = "希沃品课"

    async def build(
        self,
        items: list[tuple[Assignment, list[Question]]],
        *,
        failures: list[PincoAssignmentPlan] | None = None,
    ) -> PincoPlan:
        """生成并盖章一份计划。整个过程**没有任何写操作**。

        ``failures`` 是抓取阶段就坏掉的整门课（见 :func:`failed_course_item`）。
        它们照样进计划、标 ``FAILED``：**计划里少一项，用户就无法知道那里有东西
        没抓到**。门数多时失败项排在成功项之后，不影响状态与哈希。
        """
        plan = PincoPlan(platform=self.platform, display_name=self.display_name)
        for assignment, questions in items:
            plan.assignments.append(await self._build_one(assignment, questions))
        plan.assignments.extend(failures or ())
        return plan.seal()

    # -- 单个作业 -----------------------------------------------------------

    async def _build_one(
        self, assignment: Assignment, questions: list[Question]
    ) -> PincoAssignmentPlan:
        item = PincoAssignmentPlan(
            platform=self.platform,
            assignment_id=assignment.remote_id,
            title=assignment.title,
            course_name=assignment.course_name,
            kind=assignment.kind,
            progress=assignment.progress,
        )
        if assignment.kind is TaskKind.COURSE:
            # 学习任务（课程材料）只跟踪进度：它本身不是题，没有答案可生成，
            # 也不存在「提交」这个动作 —— 结构性排除在可提交集合之外。
            item.state = PlanState.SKIPPED
            item.reason = "学习任务只跟踪学习进度，不生成答案、不提交"
            return item
        # 实测：submitted / expired / canSubmitted 三个标记互相独立，
        # 「没交过」不等于「还能交」。不能交的作业直接跳过，别白抓一遍题目。
        # （先看 blocker 再看 finished：blocker 给出的理由更具体，比如「已过期」。）
        blocker = parsing.assignment_blocker(assignment.raw)
        if blocker:
            item.state = PlanState.SKIPPED
            item.reason = blocker
            return item
        if assignment.finished:
            item.state = PlanState.SKIPPED
            item.reason = "平台已标记完成"
            return item
        if not questions:
            item.state = PlanState.SKIPPED
            item.reason = "没有取到题目"
            return item

        answers = await self._answer(questions)
        item.questions = [
            self._question_plan(index, questions[index], answers[index])
            for index in range(len(questions))
        ]
        human = item.human_questions
        if human:
            item.state = PlanState.NEEDS_HUMAN
            item.reason = f"{len(human)} 道题需要人工处理（读不到题面或没有真答出来）"
            await self._alert(item)
        else:
            item.state = PlanState.PLANNED
        return item

    async def _answer(self, questions: list[Question]) -> list[Answer]:
        if self.answerer is None:
            return [
                Answer(question=question, source=AnswerSource.UNKNOWN)
                for question in questions
            ]
        answers = await self.answerer.answer(questions)
        if len(answers) != len(questions):
            # 答题器返回长度不符属于契约违约：不抛错拖垮整批，但必须补齐，
            # 否则后面的下标对应会错位（把 A 题的答案写到 B 题上）。
            log.warning(
                "答题器返回 %d 条答案，与 %d 道题不符；缺失部分按未作答处理",
                len(answers),
                len(questions),
            )
            padded = list(answers[: len(questions)])
            padded.extend(
                Answer(question=questions[i], source=AnswerSource.UNKNOWN)
                for i in range(len(padded), len(questions))
            )
            return padded
        return answers

    def _question_plan(
        self, index: int, question: Question, answer: Answer
    ) -> PincoQuestionPlan:
        item = PincoQuestionPlan(
            index=index,
            stem=question.stem,
            type=question.type,
            options=list(question.options),
            remote_id=question.remote_id,
            question_raw=dict(question.raw),
        )
        if question_map.is_image_question(question):
            # 需要人工的题一律不生成答案：连题库里的文字答案也不填 ——
            # 题干在图上，或答案本来就要传图，填文字等于交了个错东西。
            # 理由用解析阶段记下的那条（真图/公式/上传/标签各不相同）。
            item.image_required = True
            item.needs_human = True
            item.reason = question_map.human_reason(question)
            # 三类理由码分开带进计划：用户要能分清「必须我自己做」与「只是要看图」
            item.human_reasons = question_map.reason_codes(question)
            item.source = AnswerSource.UNKNOWN
            item.confidence = 0.0
            return item

        item.choices = list(answer.choices)
        item.text = answer.text
        item.source = answer.source
        item.confidence = answer.confidence
        item.answer_raw = dict(answer.raw)

        if answer.is_empty():
            item.needs_human = True
            item.reason = "题库与规则都没答出来，不猜"
        elif answer.source is AnswerSource.HEURISTIC:
            item.needs_human = True
            item.reason = "启发式猜测，不算真答出来"
        elif answer.confidence < self.min_confidence:
            item.needs_human = True
            item.reason = (
                f"置信度 {answer.confidence:.2f} 低于门槛 {self.min_confidence:.2f}"
            )
        return item

    # -- 提醒 ---------------------------------------------------------------

    async def _alert(self, item: PincoAssignmentPlan) -> None:
        """发事件 + 通知。这是「提醒人」的正式出口，不是日志。"""
        human = item.human_questions
        if self.bus is not None:
            for question in human:
                await self.bus.emit(
                    EVENT_QUESTION_NEEDS_HUMAN,
                    platform=self.platform,
                    assignment_id=item.assignment_id,
                    title=item.title,
                    index=question.index,
                    stem=question.stem[:80],
                    reason=question.reason,
                )
            await self.bus.emit(
                EVENT_ASSIGNMENT_NEEDS_HUMAN,
                platform=self.platform,
                assignment_id=item.assignment_id,
                title=item.title,
                reason=item.reason,
                count=len(human),
            )
        if self.notifier is None:
            return
        lines = [
            f"作业：{item.title}（{item.assignment_id}）",
            f"原因：{item.reason}",
            "以下题目不会被自动提交，请到平台上人工处理：",
        ]
        lines.extend(
            f"  {question.index + 1}. {question.stem[:60] or question.remote_id or '<无题干>'}"
            f" —— {question.reason}"
            for question in human
        )
        try:
            await self.notifier.send(
                f"{self.display_name}：有作业需要人工处理", "\n".join(lines), level="warning"
            )
        except Exception:  # noqa: BLE001 - 通知炸了不该让计划生成失败
            log.exception("人工提醒通知发送失败：%s", item.assignment_id)
