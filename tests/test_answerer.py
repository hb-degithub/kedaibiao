"""答题器测试。"""

from __future__ import annotations

import unittest

from kedaibiao.bank.store import QuestionBank
from kedaibiao.core.models import Answer, AnswerSource, Question, QuestionType
from kedaibiao.tasks.answerer import Answerer, BankAnswerer, ChainAnswerer, RuleAnswerer


class TestBankAnswerer(unittest.IsolatedAsyncioTestCase):
    async def test_hit(self) -> None:
        bank = QuestionBank(":memory:")
        self.addCleanup(bank.close)
        q = Question(stem="已入库的题", type=QuestionType.SINGLE)
        bank.upsert(
            q,
            Answer(question=q, choices=[1], source=AnswerSource.BANK_HIT, confidence=1.0),
        )
        results = await BankAnswerer(bank).answer([q])
        self.assertEqual(results[0].choices, [1])
        self.assertIs(results[0].source, AnswerSource.BANK_HIT)

    async def test_miss_returns_empty_placeholder(self) -> None:
        bank = QuestionBank(":memory:")
        self.addCleanup(bank.close)
        results = await BankAnswerer(bank).answer([Question(stem="没入库")])
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].is_empty())
        self.assertIs(results[0].source, AnswerSource.UNKNOWN)

    async def test_length_matches_input(self) -> None:
        bank = QuestionBank(":memory:")
        self.addCleanup(bank.close)
        questions = [Question(stem=f"题{i}") for i in range(5)]
        self.assertEqual(len(await BankAnswerer(bank).answer(questions)), 5)


class TestRuleAnswerer(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.rule = RuleAnswerer()

    async def test_judge_default_true(self) -> None:
        q = Question(stem="数据库是一门课程", type=QuestionType.JUDGE)
        self.assertEqual((await self.rule.answer([q]))[0].choices, [0])

    async def test_judge_negative_hint_false(self) -> None:
        for stem in ["下列说法不正确", "该选项不属于范畴", "这是错误的", "他不是学生"]:
            q = Question(stem=stem, type=QuestionType.JUDGE)
            self.assertEqual((await self.rule.answer([q]))[0].choices, [1], stem)

    async def test_single_picks_longest_option(self) -> None:
        q = Question(
            stem="选择",
            type=QuestionType.SINGLE,
            options=["短", "中等长度", "这是明显最长的一个选项"],
        )
        self.assertEqual((await self.rule.answer([q]))[0].choices, [2])

    async def test_multiple_confidence_is_discounted(self) -> None:
        q = Question(stem="多选", type=QuestionType.MULTIPLE, options=["甲", "乙", "丙"])
        ans = (await self.rule.answer([q]))[0]
        self.assertEqual(ans.choices, [0, 1, 2])
        self.assertLess(ans.confidence, RuleAnswerer.BASE_CONFIDENCE)

    async def test_confidence_below_auto_submit_threshold(self) -> None:
        q = Question(stem="判断", type=QuestionType.JUDGE)
        ans = (await self.rule.answer([q]))[0]
        self.assertLess(ans.confidence, 0.75, "启发式答案绝不该达到自动提交阈值")

    async def test_fill_and_essay_left_empty(self) -> None:
        for qtype in (QuestionType.FILL, QuestionType.ESSAY, QuestionType.UNKNOWN):
            q = Question(stem="填空题", type=qtype)
            self.assertTrue((await self.rule.answer([q]))[0].is_empty(), qtype)

    async def test_single_without_options_is_empty(self) -> None:
        q = Question(stem="没有选项", type=QuestionType.SINGLE, options=[])
        self.assertTrue((await self.rule.answer([q]))[0].is_empty())


class _StubAnswerer(Answerer):
    def __init__(self, name: str, choices: list[int], confidence: float) -> None:
        self.name = name
        self._choices = choices
        self._confidence = confidence
        self.calls = 0

    async def answer(self, questions: list[Question]) -> list[Answer]:
        self.calls += 1
        return [
            Answer(
                question=q,
                choices=list(self._choices),
                source=AnswerSource.HEURISTIC,
                confidence=self._confidence,
            )
            for q in questions
        ]


class TestChainAnswerer(unittest.IsolatedAsyncioTestCase):
    async def test_first_confident_answerer_wins(self) -> None:
        first = _StubAnswerer("first", [0], 0.9)
        second = _StubAnswerer("second", [1], 0.9)
        chain = ChainAnswerer([first, second], min_confidence=0.5)
        result = await chain.answer([Question(stem="题")])
        self.assertEqual(result[0].choices, [0])
        self.assertEqual(second.calls, 0, "前一个已答出就不该再问下一个")

    async def test_falls_through_on_low_confidence(self) -> None:
        weak = _StubAnswerer("weak", [0], 0.2)
        strong = _StubAnswerer("strong", [1], 0.9)
        chain = ChainAnswerer([weak, strong], min_confidence=0.5)
        result = await chain.answer([Question(stem="题")])
        self.assertEqual(result[0].choices, [1])
        self.assertEqual(strong.calls, 1)

    async def test_partial_fallthrough_only_sends_unanswered(self) -> None:
        weak = _StubAnswerer("weak", [0], 0.9)
        strong = _StubAnswerer("strong", [1], 0.9)
        weak.answer = _mixed  # type: ignore[method-assign]
        chain = ChainAnswerer([weak, strong], min_confidence=0.5)
        result = await chain.answer([Question(stem="有答案的题"), Question(stem="没答案的题")])
        self.assertEqual(result[0].choices, [0])
        self.assertEqual(result[1].choices, [1])
        self.assertEqual(strong.calls, 1)

    async def test_empty_answerer_list_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ChainAnswerer([])

    async def test_unanswered_gets_empty_placeholder(self) -> None:
        chain = ChainAnswerer([_StubAnswerer("weak", [0], 0.1)], min_confidence=0.5)
        result = await chain.answer([Question(stem="题")])
        self.assertTrue(result[0].is_empty())


async def _mixed(questions: list[Question]) -> list[Answer]:
    out: list[Answer] = []
    for q in questions:
        if "没答案" in q.stem:
            out.append(Answer(question=q, source=AnswerSource.UNKNOWN, confidence=0.0))
        else:
            out.append(
                Answer(question=q, choices=[0], source=AnswerSource.HEURISTIC, confidence=0.9)
            )
    return out


if __name__ == "__main__":
    unittest.main()
