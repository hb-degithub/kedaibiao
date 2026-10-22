"""答题器：决定「这道题填什么」。

三种实现，按可靠性排序：
1. ``BankAnswerer``     —— 题库命中，可靠，但覆盖有限；
2. ``RuleAnswerer``     —— 纯启发式兜底，置信度低，只用来避免空题；
3. ``ChainAnswerer``    —— 组合器，逐级降级。

LLM 答题器留了接口位（``answerers`` 注册表），等协议调研与成本评估
之后再补，不在这版骨架里猜实现。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar

from kedaibiao.core.models import Answer, AnswerSource, Question, QuestionType


class Answerer(ABC):
    name: ClassVar[str] = ""

    @abstractmethod
    async def answer(self, questions: list[Question]) -> list[Answer]:
        """对一批题目作答，返回与输入等长的列表（未答出的用空 Answer 占位）。"""

    async def answer_one(self, question: Question) -> Answer:
        return (await self.answer([question]))[0]

    @staticmethod
    def _empty(question: Question) -> Answer:
        return Answer(question=question, source=AnswerSource.UNKNOWN, confidence=0.0)


class BankAnswerer(Answerer):
    """只查题库，不做任何猜测。"""

    name = "bank"

    def __init__(self, bank: object) -> None:
        self.bank = bank

    async def answer(self, questions: list[Question]) -> list[Answer]:
        results: list[Answer] = []
        for q in questions:
            found = self.bank.resolve(q)  # type: ignore[attr-defined]
            results.append(found if found is not None else self._empty(q))
        return results


class RuleAnswerer(Answerer):
    """启发式兜底。置信度刻意压低，好让上层拒绝自动提交。"""

    name = "rule"

    #: 题干含这些词时判断题倾向为「错」
    _NEGATIVE_HINTS = ("不正确", "不属于", "错误", "不是", "不能", "不可以", "无效", "例外")

    BASE_CONFIDENCE = 0.35

    async def answer(self, questions: list[Question]) -> list[Answer]:
        return [self._solve(q) for q in questions]

    def _solve(self, question: Question) -> Answer:
        qtype = question.type
        if qtype is QuestionType.JUDGE:
            is_true = not any(h in question.stem for h in self._NEGATIVE_HINTS)
            return Answer(
                question=question,
                choices=[0 if is_true else 1],
                source=AnswerSource.HEURISTIC,
                confidence=self.BASE_CONFIDENCE,
            )
        if qtype is QuestionType.SINGLE and question.options:
            # 兜底选「最长且含最多限定词」的选项：统计上比随机强
            idx = max(range(len(question.options)), key=lambda i: len(question.options[i]))
            return Answer(
                question=question,
                choices=[idx],
                source=AnswerSource.HEURISTIC,
                confidence=self.BASE_CONFIDENCE,
            )
        if qtype is QuestionType.MULTIPLE and question.options:
            return Answer(
                question=question,
                choices=list(range(len(question.options))),
                source=AnswerSource.HEURISTIC,
                confidence=self.BASE_CONFIDENCE / 2,
            )
        return self._empty(question)


class ChainAnswerer(Answerer):
    """逐级降级：前一个答不出（空答案或低于阈值）才问下一个。

    ## 低于阈值的答案：默认丢弃，按需保留

    默认行为保持「不可靠就当没答」：整条链跑完仍低于 ``min_confidence`` 的题
    返回空占位，让上层（流水线 / 批跑）拒绝自动提交 —— 这是自动链路的正确语义。

    但**人工复核**要的是相反的：控制台要让人看到「模型到底猜了什么、它自己
    有多确信」，再决定改还是确认。那种场景传 ``keep_weak=True``：低于阈值的
    候选答案保留下来（置信度不篡改），只是继续按需人工确认标出来。
    两种语义不能合成一种 —— 丢掉答案就没得复核，保留答案则不能自动提交。
    """

    name = "chain"

    def __init__(self, answerers: list[Answerer], *, min_confidence: float = 0.5) -> None:
        if not answerers:
            raise ValueError("ChainAnswerer 至少需要一个答题器")
        self.answerers = answerers
        self.min_confidence = min_confidence

    @property
    def supports_images(self) -> bool:
        """链里只要有一个能读图，整条链就「能读图」。

        学习通走 OCR 时靠这个判断要不要截图：视觉答题器通常在链首、规则兜底在链尾，
        没有这个属性的话上层会以为整条链只会读文本，截图就白截了。
        """
        return any(getattr(item, "supports_images", False) for item in self.answerers)

    async def answer(
        self, questions: list[Question], *, keep_weak: bool = False
    ) -> list[Answer]:
        remaining = list(range(len(questions)))
        results: dict[int, Answer] = {}
        #: ``keep_weak=True`` 时留下低于阈值但非空的答案（同一题取置信度最高的一条）
        weak: dict[int, Answer] = {}

        for answerer in self.answerers:
            if not remaining:
                break
            batch = [questions[i] for i in remaining]
            produced = await answerer.answer(batch)
            still: list[int] = []
            for i, ans in zip(remaining, produced, strict=True):
                if ans.is_empty():
                    still.append(i)
                    continue
                if ans.confidence >= self.min_confidence:
                    results[i] = ans
                    continue
                if keep_weak:
                    prev = weak.get(i)
                    if prev is None or ans.confidence > prev.confidence:
                        weak[i] = ans
                still.append(i)
            remaining = still

        for i in remaining:
            results[i] = weak[i] if i in weak else self._empty(questions[i])
        return [results[i] for i in range(len(questions))]
