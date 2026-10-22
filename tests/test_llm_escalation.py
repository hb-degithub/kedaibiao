"""低置信度升级 + OCR 独立配置的单测（全部离线，不联网、不花钱）。

钉住四件事：

1. **配置解析**：OCR 用 ``OCR_MODEL``（兼容旧 ``VISION_MODEL``）、可独立网关；
   升级需要「阈值 > 0 + 配了第二个模型」，第二个模型与主模型相同则视为未配置
   （同一个模型重问一遍没有意义）；
2. **升级时机**：只对「没答出来」或「置信度低于阈值」的题重问，其余题不动；
3. **取信规则**：第二个模型置信度更高才替换，且替换后标记来源；
4. **失败隔离**：第二个模型报错只记日志、保留主模型答案，不抛给上层。
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from kedaibiao.core.models import Answer, AnswerSource, Question, QuestionType
from kedaibiao.tasks.answerer import Answerer
from kedaibiao.tasks.llm import (
    EscalatingAnswerer,
    LLMConfig,
    build_llm_answerer,
)

ENV = {
    "KEDAIBIAO_LLM_BASE_URL": "https://api.example.com/v1",
    "KEDAIBIAO_LLM_API_KEY": "sk-test",
    "KEDAIBIAO_LLM_MODEL": "cheap-model",
}


def q(stem: str) -> Question:
    return Question(stem=stem, type=QuestionType.SINGLE, options=["A", "B", "C", "D"])


class FakeAnswerer(Answerer):
    """按题面回固定答案的假答题器；``boom`` 用来模拟第二个模型失败。"""

    name = "fake"

    def __init__(
        self,
        table: dict[str, tuple[list[int], float]],
        *,
        boom: bool = False,
        model: str = "fake-model",
    ) -> None:
        self.table = table
        self.boom = boom
        self.calls: list[list[str]] = []
        # EscalatingAnswerer 会在替换答案时记录「升级到哪个模型」，所以假对象
        # 也要有 config.model；真实实现里这是 LLMConfig。
        self.config = SimpleNamespace(model=model)

    async def answer(self, questions: list[Question]) -> list[Answer]:
        self.calls.append([item.stem for item in questions])
        if self.boom:
            raise RuntimeError("第二个模型挂了")
        out: list[Answer] = []
        for item in questions:
            choices, confidence = self.table.get(item.stem, ([], 0.0))
            out.append(
                Answer(
                    question=item,
                    choices=list(choices),
                    confidence=confidence,
                    source=AnswerSource.LLM if choices else AnswerSource.UNKNOWN,
                )
            )
        return out


class ConfigParsingTest(unittest.TestCase):
    def test_ocr_model_alias(self) -> None:
        cfg = LLMConfig.from_env({**ENV, "KEDAIBIAO_LLM_OCR_MODEL": "qwen-vl"})
        self.assertEqual(cfg.vision_model, "qwen-vl")
        self.assertEqual(cfg.vision_config().model, "qwen-vl")

    def test_legacy_vision_model_still_works(self) -> None:
        cfg = LLMConfig.from_env({**ENV, "KEDAIBIAO_LLM_VISION_MODEL": "old-vl"})
        self.assertEqual(cfg.vision_model, "old-vl")

    def test_ocr_gateway_override(self) -> None:
        cfg = LLMConfig.from_env({
            **ENV,
            "KEDAIBIAO_LLM_OCR_MODEL": "vl",
            "KEDAIBIAO_LLM_OCR_BASE_URL": "https://ocr.example.com/v1",
            "KEDAIBIAO_LLM_OCR_API_KEY": "ocr-key",
        })
        vision = cfg.vision_config()
        self.assertEqual(vision.base_url, "https://ocr.example.com/v1")
        self.assertEqual(vision.api_key, "ocr-key")
        # 主配置不能被动到
        self.assertEqual(cfg.base_url, "https://api.example.com/v1")
        self.assertEqual(cfg.api_key, "sk-test")

    def test_escalation_needs_threshold_and_model(self) -> None:
        self.assertFalse(LLMConfig.from_env(ENV).escalate_enabled)
        self.assertFalse(
            LLMConfig.from_env({**ENV, "KEDAIBIAO_LLM_FALLBACK_MODEL": "big"}).escalate_enabled,
            "没给阈值不能默认升钱",
        )
        self.assertFalse(
            LLMConfig.from_env({**ENV, "KEDAIBIAO_LLM_ESCALATE_THRESHOLD": "0.8"}).escalate_enabled,
            "没给第二个模型不能升级",
        )
        cfg = LLMConfig.from_env({
            **ENV,
            "KEDAIBIAO_LLM_FALLBACK_MODEL": "big",
            "KEDAIBIAO_LLM_ESCALATE_THRESHOLD": "0.8",
        })
        self.assertTrue(cfg.escalate_enabled)
        fallback = cfg.fallback_config()
        assert fallback is not None
        self.assertEqual(fallback.model, "big")
        self.assertFalse(fallback.escalate_enabled, "第二个模型不应再升级（防无限套娃）")

    def test_same_model_means_no_escalation(self) -> None:
        cfg = LLMConfig.from_env({
            **ENV,
            "KEDAIBIAO_LLM_FALLBACK_MODEL": "cheap-model",
            "KEDAIBIAO_LLM_ESCALATE_THRESHOLD": "0.8",
        })
        self.assertIsNone(cfg.fallback_config(), "同一个模型重问一遍只是白花钱")

    def test_bad_threshold_falls_back_to_disabled(self) -> None:
        cfg = LLMConfig.from_env({
            **ENV,
            "KEDAIBIAO_LLM_FALLBACK_MODEL": "big",
            "KEDAIBIAO_LLM_ESCALATE_THRESHOLD": "abc",
        })
        self.assertEqual(cfg.escalate_threshold, 0.0)

    def test_build_answerer_wraps_only_when_configured(self) -> None:
        self.assertEqual(type(build_llm_answerer(ENV)).__name__, "VisionAnswerer")
        wrapped = build_llm_answerer({
            **ENV,
            "KEDAIBIAO_LLM_FALLBACK_MODEL": "big",
            "KEDAIBIAO_LLM_ESCALATE_THRESHOLD": "0.8",
        })
        self.assertIsInstance(wrapped, EscalatingAnswerer)


class EscalationBehaviorTest(unittest.IsolatedAsyncioTestCase):
    def _subject(
        self,
        primary: FakeAnswerer,
        fallback: FakeAnswerer,
        threshold: float = 0.8,
    ) -> EscalatingAnswerer:
        return EscalatingAnswerer(primary, fallback, threshold=threshold)  # type: ignore[arg-type]

    async def test_escalates_only_low_confidence(self) -> None:
        primary = FakeAnswerer({"Q1": ([0], 0.95), "Q2": ([0], 0.3)})
        fallback = FakeAnswerer({"Q2": ([1], 0.9)})
        answerer = self._subject(primary, fallback)

        out = await answerer.answer([q("Q1"), q("Q2")])

        self.assertEqual(primary.calls[0], ["Q1", "Q2"], "主模型仍然是整份作答")
        self.assertEqual(fallback.calls, [["Q2"]], "只把低置信度的题拿去升级")
        self.assertEqual(out[0].choices, [0], "高置信度题不动")
        self.assertEqual(out[1].choices, [1], "低置信度题用升级后的答案")
        self.assertEqual(answerer.escalations, 1)

    async def test_keeps_primary_when_fallback_is_less_confident(self) -> None:
        primary = FakeAnswerer({"Q1": ([0], 0.5)})
        fallback = FakeAnswerer({"Q1": ([2], 0.4)})
        answerer = self._subject(primary, fallback)

        out = await answerer.answer([q("Q1")])
        self.assertEqual(out[0].choices, [0])
        self.assertEqual(answerer.escalations, 0)

    async def test_empty_answer_is_escalated(self) -> None:
        primary = FakeAnswerer({})  # 完全没答出来
        fallback = FakeAnswerer({"Q1": ([3], 0.2)})
        answerer = self._subject(primary, fallback)

        out = await answerer.answer([q("Q1")])
        self.assertEqual(out[0].choices, [3], "没答出来的题也值得再问一次")

    async def test_marks_source_of_escalated_answers(self) -> None:
        primary = FakeAnswerer({"Q1": ([0], 0.2)})
        fallback = FakeAnswerer({"Q1": ([1], 0.9)})
        answerer = self._subject(primary, fallback)

        out = await answerer.answer([q("Q1")])
        self.assertEqual(out[0].raw["escalated_model"], "fake-model")
        self.assertIn("escalated_from", out[0].raw)
        self.assertEqual(out[0].raw["escalated_from"]["threshold"], 0.8)
        self.assertEqual(out[0].source, AnswerSource.LLM)

    async def test_threshold_zero_disables_everything(self) -> None:
        primary = FakeAnswerer({"Q1": ([0], 0.1)})
        fallback = FakeAnswerer({"Q1": ([1], 0.99)})
        answerer = self._subject(primary, fallback, threshold=0.0)

        out = await answerer.answer([q("Q1")])
        self.assertEqual(fallback.calls, [], "阈值 0 = 关闭，不该多花任何请求")
        self.assertEqual(out[0].choices, [0])

    async def test_fallback_failure_keeps_primary_answer(self) -> None:
        primary = FakeAnswerer({"Q1": ([0], 0.3)})
        fallback = FakeAnswerer({}, boom=True)
        answerer = self._subject(primary, fallback)

        with self.assertLogs("kedaibiao.tasks.llm", level="WARNING"):
            out = await answerer.answer([q("Q1")])

        self.assertEqual(out[0].choices, [0], "升级失败不能丢主模型的答案")
        self.assertIn("第二个模型挂了", answerer.last_error)


if __name__ == "__main__":
    unittest.main()
