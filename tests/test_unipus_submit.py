"""U校园提交载荷构造与响应解析测试（完全离线）。

规格 §8.1 给了完整形状，但 isCompleted / thirdPartyJudges 的**取值语义**没给，
所以这里锁的是「形状正确 + 缺口被显式暴露」，而不是「猜对值」。
"""

from __future__ import annotations

import json
import unittest
from dataclasses import dataclass

from kedaibiao.platforms.unipus.submit import (
    SubmitPayloadError,
    build_answer_object,
    build_ques_data,
    build_submit_plan,
    parse_submit_result,
)


@dataclass
class FakeQuestion:
    instance_id: str
    item_count: int = 1
    child_count: int = 1
    media_types: tuple[int, ...] = ()
    category: int | None = 1


@dataclass
class FakeStandard:
    children: tuple[tuple[str, ...], ...]


def q(qid: str = "q1", **kw) -> FakeQuestion:
    return FakeQuestion(instance_id=qid, **kw)


class TestAnswerObject(unittest.TestCase):
    def test_shape(self) -> None:
        obj = build_answer_object(children=[("A",), ("B", "C")])
        self.assertEqual(obj["value"], [])
        self.assertEqual(obj["children"], [
            {"value": ["A"], "isDone": True},
            {"value": ["B", "C"], "isDone": True},
        ])
        self.assertEqual(obj["record"], {"url": ""})

    def test_progress_keys_are_media_indexes(self) -> None:
        obj = build_answer_object(children=[()], media_indexes=(0, 2))
        self.assertEqual(obj["progress"], {"0": True, "2": True})

    def test_reply_flag_for_subjective(self) -> None:
        obj = build_answer_object(children=[()], media_indexes=(0,), reply=True)
        self.assertTrue(obj["progress"]["reply"])


class TestQuesData(unittest.TestCase):
    def test_forward_standard_answers(self) -> None:
        """规格 §7.3：答案是服务端标准答案原样转发。"""
        data = build_ques_data(
            questions=[q("a")],
            standard_answers=[FakeStandard(children=(("X",),))],
        )
        answer = json.loads(data[0]["answer"])
        self.assertEqual(answer["children"][0]["value"], ["X"])
        self.assertEqual(data[0]["instanceId"], "a")
        self.assertEqual(json.loads(data[0]["context"]), {"state": "submitted"})
        self.assertEqual(data[0]["contextVersion"], 1)
        self.assertEqual(data[0]["answerVersion"], 1)

    def test_mismatched_answer_count_rejected(self) -> None:
        with self.assertRaises(SubmitPayloadError) as cm:
            build_ques_data(
                questions=[q("a"), q("b")],
                standard_answers=[FakeStandard(children=(("X",),))],
            )
        self.assertIn("不一致", str(cm.exception))

    def test_without_answers_uses_empty_children(self) -> None:
        data = build_ques_data(questions=[q("a", child_count=2)])
        answer = json.loads(data[0]["answer"])
        self.assertEqual(len(answer["children"]), 2)
        self.assertEqual(answer["children"][0]["value"], [])


class TestSubmitPlan(unittest.TestCase):
    def _plan(self, **kw):
        questions = kw.pop("questions", None) or [q("a"), q("b")]
        base = dict(
            course_id="course-v2:inst-1",
            group_id="u1g1",
            open_id="open-1",
            questions=questions,
            # 答案条数必须跟题目数一致，否则会被一致性校验挡住（这本身就是被测行为）
            standard_answers=kw.pop(
                "standard_answers",
                [FakeStandard(children=(("A",),)) for _ in questions],
            ),
        )
        base.update(kw)
        return build_submit_plan(**base)

    def test_all_spec_keys_present(self) -> None:
        plan = self._plan()
        for key in ("courseId", "openId", "version", "quesDatas", "groupId",
                    "isCompleted", "thirdPartyJudges", "submitType",
                    "associationGroupId", "associationUnitId"):
            self.assertIn(key, plan.payload, key)
        self.assertEqual(plan.payload["version"], "default")
        self.assertEqual(plan.payload["associationGroupId"], "")
        self.assertEqual(plan.payload["associationUnitId"], "")

    def test_item_count_and_lengths(self) -> None:
        plan = self._plan(questions=[q("a", item_count=2), q("b", item_count=3)])
        self.assertEqual(plan.item_count, 5)
        self.assertEqual(len(plan.payload["isCompleted"]), 5)
        judges = json.loads(plan.payload["thirdPartyJudges"])
        self.assertEqual(len(judges), 5, "thirdPartyJudges 长度必须与 isCompleted 相等")

    def test_submit_type_objective(self) -> None:
        plan = self._plan(questions=[q("a", category=1)])
        self.assertEqual(plan.submit_type, 1)

    def test_submit_type_pure_learning(self) -> None:
        plan = self._plan(questions=[q("a", category=0), q("b", category=0)])
        self.assertEqual(plan.submit_type, 2, "纯学习题组是 2（规格 §8.1）")

    def test_mixed_is_not_pure_learning(self) -> None:
        plan = self._plan(questions=[q("a", category=0), q("b", category=1)])
        self.assertEqual(plan.submit_type, 1)

    def test_missing_open_id_rejected(self) -> None:
        with self.assertRaises(SubmitPayloadError):
            self._plan(open_id="")

    def test_zero_item_count_rejected(self) -> None:
        with self.assertRaises(SubmitPayloadError) as cm:
            self._plan(questions=[q("a", item_count=0)])
        self.assertIn("计分点", str(cm.exception))

    def test_bad_is_completed_length_rejected(self) -> None:
        with self.assertRaises(SubmitPayloadError):
            self._plan(is_completed=[True])

    def test_bad_judges_length_rejected(self) -> None:
        with self.assertRaises(SubmitPayloadError) as cm:
            self._plan(third_party_judges=[1])
        self.assertIn("严格相等", str(cm.exception))

    def test_warns_about_unconfirmed_semantics(self) -> None:
        plan = self._plan()
        joined = "；".join(plan.warnings)
        self.assertIn("thirdPartyJudges", joined, "必须显式暴露未确认的字段")
        self.assertIn("submit_payload_confirmed", joined)

    def test_warns_when_no_standard_answers(self) -> None:
        plan = self._plan(standard_answers=None)
        self.assertTrue(any("标准答案" in w for w in plan.warnings))

    def test_summary_readable(self) -> None:
        self.assertIn("u1g1", self._plan().summary())


class TestParseResult(unittest.TestCase):
    def test_accepted_with_scores(self) -> None:
        result = parse_submit_result({
            "code": 0,
            "data": {"state": {"state": 1, "score": [1, 1, 0], "score_pct": 0.667},
                     "version": "v3"},
        })
        self.assertTrue(result.accepted)
        self.assertEqual(result.state, 1)
        self.assertEqual(result.score, [1, 1, 0])
        self.assertAlmostEqual(result.score_pct, 0.667)
        self.assertEqual(result.version, "v3")
        self.assertEqual(result.per_child_verdicts(), [True, True, False])

    def test_rejected(self) -> None:
        result = parse_submit_result({"code": 1, "data": {}})
        self.assertFalse(result.accepted)
        self.assertIn("未接受", result.summary())

    def test_empty_score_for_non_graded(self) -> None:
        result = parse_submit_result({"code": 0, "data": {"state": {"state": 1, "score": []}}})
        self.assertEqual(result.per_child_verdicts(), [])

    def test_malformed_numbers_tolerated(self) -> None:
        result = parse_submit_result({
            "code": 0, "data": {"state": {"state": "x", "score": [1, "y"], "score_pct": "z"}},
        })
        self.assertIsNone(result.state)
        self.assertIsNone(result.score_pct)
        self.assertEqual(result.per_child_verdicts(), [True, None])


class TestAdapterGate(unittest.IsolatedAsyncioTestCase):
    """闸门必须挡住真实发送，并把缺口讲清楚。"""

    async def test_gate_blocks_submit(self) -> None:
        from kedaibiao.core.models import Assignment
        from kedaibiao.platforms.base import NotSupportedError
        from kedaibiao.platforms.unipus.adapter import UnipusAdapter

        adapter = UnipusAdapter()  # 没有 http，闸门仍应直接拒绝
        with self.assertRaises(NotSupportedError) as cm:
            await adapter.submit(Assignment(remote_id="course-v2:x/u1g1", title="t"), [])
        message = str(cm.exception)
        self.assertIn("未由真实抓包确认", message)
        self.assertIn("submit_payload_confirmed", message)

    def test_capability_stays_false(self) -> None:
        from kedaibiao.platforms.unipus.adapter import UnipusAdapter

        self.assertFalse(UnipusAdapter.capabilities.submit,
                         "载荷未确认前，能力声明保持 False —— 流水线不会尝试提交")


if __name__ == "__main__":
    unittest.main()
