"""品课「交作业」动作：完整链路（计划 → 保存答案 → 校验 → 提交 → 回读）。

这条链路是 2026-10-28 实测跑通的：
- 干跑只出计划，绝不写；
- 有需人工/低置信度题 → 拒绝交；
- confirm=true 时：保存答案 → 回读校验 → 提交（协议见 endpoints.ACTION_ANSWER_SAVE）。

用真实 ``PincoAdapter`` + 假 Transport 跑，所以保存/校验/提交的顺序是真的被
执行到的代码路径，不是 mock 出来的假顺序。
"""

from __future__ import annotations

import unittest

from kedaibiao.api import actions
from kedaibiao.api.actions import ActionInputError, load_builtin_actions
from kedaibiao.core.models import (
    Answer,
    AnswerSource,
    Assignment,
    Question,
    QuestionType,
)
from kedaibiao.platforms.pinco import endpoints as ep
from kedaibiao.platforms.pinco import solving
from tests.test_pinco import Replies, make_adapter

ASSIGNMENT_ID = "hw-1"


class _FixedAnswerer:
    def __init__(self, answers: list[Answer]) -> None:
        self._answers = answers

    async def answer(self, questions: list[Question]) -> list[Answer]:
        return list(self._answers)


def _question() -> Question:
    return Question(
        stem="下列哪项属于结构化数据",
        type=QuestionType.SINGLE,
        options=["关系表", "图片", "音频"],
        remote_id="q1",
    )


def setUpModule() -> None:
    load_builtin_actions()
    actions.ensure_generic_actions("pinco")


class PincoSubmitActionTest(unittest.IsolatedAsyncioTestCase):
    async def _adapter(self, *, confidence: float = 1.0):
        question = _question()
        answer = Answer(
            question=question,
            choices=[0],
            source=AnswerSource.BANK_HIT,
            confidence=confidence,
        )
        adapter, transport, _, _ = make_adapter()
        adapter.submit_payload_confirmed = True
        transport.actions[ep.ACTION_ANSWER_SAVE] = {}
        transport.actions[ep.ACTION_HOMEWORK_QUESTIONS] = Replies([
            {"singleChoices": [{"questionId": "q1", "answer": {"solution": "A"}}]},
        ])
        plan = await solving.PlanBuilder(answerer=_FixedAnswerer([answer])).build(
            [(Assignment(remote_id=ASSIGNMENT_ID, title="第一章作业"), [question])]
        )
        adapter.plan = _plan_returning(plan)  # type: ignore[method-assign]
        return adapter, transport, plan

    async def test_dry_run_only_builds_plan(self) -> None:
        adapter, transport, _ = await self._adapter()
        out = await actions.get_handler("pinco", "submit")(
            adapter, {"assignment_id": ASSIGNMENT_ID}
        )
        self.assertEqual(out["kind"], "table")
        self.assertTrue(out["meta"]["dry_run"])
        self.assertFalse(transport.wrote_anything(), "干跑绝不能写")

    async def test_confirm_refuses_when_question_needs_human(self) -> None:
        adapter, transport, _ = await self._adapter(confidence=0.2)
        with self.assertRaises(ActionInputError) as ctx:
            await actions.get_handler("pinco", "submit")(
                adapter, {"assignment_id": ASSIGNMENT_ID, "confirm": "true"}
            )
        self.assertIn("人工", str(ctx.exception))
        self.assertFalse(transport.wrote_anything())

    async def test_confirm_submits_after_saving_and_verifying(self) -> None:
        adapter, transport, _ = await self._adapter()
        out = await actions.get_handler("pinco", "submit")(
            adapter, {"assignment_id": ASSIGNMENT_ID, "confirm": "true"}
        )
        names = transport.action_names()
        self.assertIn(ep.ACTION_ANSWER_SAVE, names, "提交前必须先保存答案")
        self.assertIn(ep.ACTION_SUBMIT_HOMEWORK, names)
        self.assertLess(
            names.index(ep.ACTION_ANSWER_SAVE), names.index(ep.ACTION_SUBMIT_HOMEWORK)
        )
        self.assertEqual(out["kind"], "table")
        self.assertFalse(out["meta"]["dry_run"])
        self.assertEqual(out["meta"]["answered"], 1)

    async def test_unknown_assignment_is_input_error(self) -> None:
        adapter, _, _ = await self._adapter()
        with self.assertRaises(ActionInputError):
            await actions.get_handler("pinco", "submit")(
                adapter, {"assignment_id": "nope"}
            )

    async def test_action_is_registered_as_write(self) -> None:
        specs = {s.name: s for s in actions.register_platform("pinco")}
        self.assertIn("submit", specs)
        self.assertTrue(specs["submit"].mutates, "交作业是写操作，必须标 mutates")


def _plan_returning(plan):
    async def _plan(**kwargs):  # noqa: ANN001, ANN202
        return plan

    return _plan
