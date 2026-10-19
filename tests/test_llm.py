"""大模型答题器测试（完全离线，用假传输层）。"""

from __future__ import annotations

import json
import unittest

from kedaibiao.core.models import AnswerSource, Question, QuestionType
from kedaibiao.net.http import HttpClient, Response
from kedaibiao.tasks.llm import (
    CONFIDENCE_DISCOUNT,
    LLMAnswerer,
    LLMConfig,
    LLMError,
    _extract_json,
    build_llm_answerer,
)


class FakeTransport:
    """记录请求、按脚本回包。"""

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.calls: list[dict] = []

    async def request(self, method, url, *, headers=None, body=None, timeout=15.0):
        self.calls.append({"method": method, "url": url, "headers": headers or {},
                           "body": body, "timeout": timeout})
        if not self.replies:
            raise AssertionError("没有更多预设回复")
        content = self.replies.pop(0)
        payload = {"choices": [{"message": {"content": content}}]}
        return Response(status=200, url=url, body=json.dumps(payload).encode())


def make_answerer(*replies: str, **cfg) -> tuple[LLMAnswerer, FakeTransport]:
    transport = FakeTransport(*replies)
    config = LLMConfig(base_url="https://api.example.com/v1", api_key="sk-test",
                       model="test-model", **cfg)
    return LLMAnswerer(config, client=HttpClient(transport)), transport


def q(stem: str, options: list[str] | None = None,
      qtype: QuestionType = QuestionType.SINGLE) -> Question:
    return Question(stem=stem, type=qtype, options=options or ["甲", "乙", "丙"], remote_id="q1")


class TestConfig(unittest.TestCase):
    def test_disabled_without_model_or_url(self) -> None:
        self.assertFalse(LLMConfig().enabled)
        self.assertFalse(LLMConfig(base_url="x").enabled)
        self.assertFalse(LLMConfig(model="m").enabled)
        self.assertTrue(LLMConfig(base_url="x", model="m").enabled)

    def test_from_env(self) -> None:
        cfg = LLMConfig.from_env({
            "KEDAIBIAO_LLM_BASE_URL": "https://api.deepseek.com/v1/",
            "KEDAIBIAO_LLM_API_KEY": "sk-1",
            "KEDAIBIAO_LLM_MODEL": "deepseek-chat",
            "KEDAIBIAO_LLM_TIMEOUT": "30",
            "KEDAIBIAO_LLM_TEMPERATURE": "0.2",
        })
        self.assertEqual(cfg.base_url, "https://api.deepseek.com/v1", "结尾斜杠要归一")
        self.assertEqual(cfg.model, "deepseek-chat")
        self.assertEqual(cfg.timeout, 30.0)
        self.assertEqual(cfg.temperature, 0.2)

    def test_describe_never_leaks_key(self) -> None:
        cfg = LLMConfig(base_url="https://x/v1", api_key="sk-secret-value", model="m")
        text = cfg.describe()
        self.assertIn("m", text)
        self.assertIn("已设置", text)
        self.assertNotIn("sk-secret-value", text)

    def test_build_returns_none_when_unconfigured(self) -> None:
        self.assertIsNone(build_llm_answerer({}))
        self.assertIsNotNone(build_llm_answerer(
            {"KEDAIBIAO_LLM_BASE_URL": "https://x/v1", "KEDAIBIAO_LLM_MODEL": "m"}
        ))


class TestExtractJson(unittest.TestCase):
    def test_plain(self) -> None:
        self.assertEqual(_extract_json('{"a":1}'), {"a": 1})

    def test_fenced(self) -> None:
        self.assertEqual(_extract_json('```json\n{"a":2}\n```'), {"a": 2})

    def test_prose_around(self) -> None:
        self.assertEqual(_extract_json('好的，答案如下：\n{"a":3}\n希望有帮助'), {"a": 3})

    def test_nested_braces(self) -> None:
        self.assertEqual(_extract_json('前言 {"a":{"b":4}} 后语'), {"a": {"b": 4}})

    def test_empty_raises(self) -> None:
        with self.assertRaises(LLMError):
            _extract_json("")

    def test_garbage_raises(self) -> None:
        with self.assertRaises(LLMError):
            _extract_json("这不是 JSON")


class TestAnswering(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_returns_empty(self) -> None:
        answerer = LLMAnswerer(LLMConfig())
        result = await answerer.answer([q("题")])
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0].is_empty())

    async def test_single_choice(self) -> None:
        reply = json.dumps({"answers": [{"index": 0, "choices": [1],
                                         "confidence": 0.9, "reason": "因为乙"}]})
        answerer, transport = make_answerer(reply)
        result = await answerer.answer([q("题")])
        self.assertEqual(result[0].choices, [1])
        self.assertIs(result[0].source, AnswerSource.LLM)
        self.assertAlmostEqual(result[0].confidence, round(0.9 * CONFIDENCE_DISCOUNT, 4))
        self.assertIn("因为乙", result[0].raw["reason"])

        # 请求形状：OpenAI 兼容
        call = transport.calls[0]
        self.assertIn("/chat/completions", call["url"])
        self.assertEqual(call["headers"]["Authorization"], "Bearer sk-test")
        body = json.loads(call["body"].decode())
        self.assertEqual(body["model"], "test-model")
        self.assertEqual(body["messages"][0]["role"], "system")

    async def test_multiple_choice(self) -> None:
        reply = json.dumps({"answers": [{"index": 0, "choices": [2, 0], "confidence": 0.8}]})
        answerer, _ = make_answerer(reply)
        result = await answerer.answer([q("多选", qtype=QuestionType.MULTIPLE)])
        self.assertEqual(result[0].choices, [0, 2], "应升序去重")

    async def test_fill_uses_text(self) -> None:
        reply = json.dumps({"answers": [{"index": 0, "choices": [], "text": "答案文本",
                                         "confidence": 0.7}]})
        answerer, _ = make_answerer(reply)
        result = await answerer.answer([q("填空", qtype=QuestionType.FILL)])
        self.assertEqual(result[0].text, "答案文本")
        self.assertEqual(result[0].choices, [])

    async def test_out_of_range_choices_discarded(self) -> None:
        """模型给了越界选项 —— 宁可丢掉也不能猜。"""
        reply = json.dumps({"answers": [{"index": 0, "choices": [99], "confidence": 0.9}]})
        answerer, _ = make_answerer(reply)
        result = await answerer.answer([q("题", options=["甲", "乙"])])
        self.assertTrue(result[0].is_empty(), "越界选项应被丢弃")

    async def test_choice_question_without_choices_discarded(self) -> None:
        reply = json.dumps({"answers": [{"index": 0, "choices": [], "text": "随便",
                                         "confidence": 0.9}]})
        answerer, _ = make_answerer(reply)
        result = await answerer.answer([q("题")])
        self.assertTrue(result[0].is_empty(), "选择题没有合法选项就算没答")

    async def test_single_choice_keeps_first_of_many(self) -> None:
        reply = json.dumps({"answers": [{"index": 0, "choices": [0, 1], "confidence": 0.9}]})
        answerer, _ = make_answerer(reply)
        result = await answerer.answer([q("单选")])
        self.assertEqual(result[0].choices, [0])

    async def test_confidence_clamped(self) -> None:
        reply = json.dumps({"answers": [{"index": 0, "choices": [0], "confidence": 5}]})
        answerer, _ = make_answerer(reply)
        result = await answerer.answer([q("题")])
        self.assertLessEqual(result[0].confidence, 1.0)

    async def test_missing_question_degrades(self) -> None:
        """模型漏了某题 —— 那题保持空，不影响其它题。"""
        reply = json.dumps({"answers": [{"index": 0, "choices": [0], "confidence": 0.9}]})
        answerer, _ = make_answerer(reply)
        questions = [q("题一"), q("题二")]
        result = await answerer.answer(questions)
        self.assertFalse(result[0].is_empty())
        self.assertTrue(result[1].is_empty())

    async def test_batching(self) -> None:
        batch1 = json.dumps({"answers": [
            {"index": 0, "choices": [0], "confidence": 0.9},
            {"index": 1, "choices": [1], "confidence": 0.9},
        ]})
        batch2 = json.dumps({"answers": [{"index": 0, "choices": [2], "confidence": 0.9}]})
        answerer, transport = make_answerer(batch1, batch2, max_batch=2)
        result = await answerer.answer([q("1"), q("2"), q("3")])
        self.assertEqual(len(transport.calls), 2, "应分两批请求")
        self.assertEqual([r.choices for r in result], [[0], [1], [2]])

    async def test_api_failure_does_not_crash_batch(self) -> None:
        """模型报错时保持空答案交给下一级，而不是抛出去炸掉整份。"""
        transport = FakeTransport()
        transport.replies = []
        answerer = LLMAnswerer(
            LLMConfig(base_url="https://x/v1", model="m"),
            client=HttpClient(transport),
        )

        async def boom(*args, **kwargs):
            raise ConnectionError("模型挂了")

        transport.request = boom  # type: ignore[method-assign]
        result = await answerer.answer([q("题")])
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0].is_empty())
        self.assertIn("模型挂了", answerer.last_error)

    async def test_malformed_model_output_degrades(self) -> None:
        answerer, _ = make_answerer("我拒绝输出 JSON")
        result = await answerer.answer([q("题")])
        self.assertTrue(result[0].is_empty())
        self.assertIn("JSON", answerer.last_error or "")

    async def test_empty_question_list(self) -> None:
        answerer, transport = make_answerer()
        self.assertEqual(await answerer.answer([]), [])
        self.assertEqual(transport.calls, [], "没题目就不该发请求")


class TestChainIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_bank_beats_llm(self) -> None:
        """题库命中就不该花大模型的钱。"""
        from kedaibiao.bank.store import QuestionBank
        from kedaibiao.core.models import Answer
        from kedaibiao.tasks.answerer import BankAnswerer, ChainAnswerer

        bank = QuestionBank(":memory:")
        self.addCleanup(bank.close)
        question = q("已入库的题")
        bank.upsert(question, Answer(question=question, choices=[2],
                                     source=AnswerSource.BANK_HIT, confidence=1.0))

        reply = json.dumps({"answers": [{"index": 0, "choices": [0], "confidence": 0.9}]})
        llm, transport = make_answerer(reply)
        chain = ChainAnswerer([BankAnswerer(bank), llm], min_confidence=0.5)
        result = await chain.answer([question])
        self.assertEqual(result[0].choices, [2])
        self.assertEqual(transport.calls, [], "题库命中时不该调用大模型")

    async def test_llm_fills_what_bank_misses(self) -> None:
        from kedaibiao.bank.store import QuestionBank
        from kedaibiao.tasks.answerer import BankAnswerer, ChainAnswerer

        bank = QuestionBank(":memory:")
        self.addCleanup(bank.close)
        reply = json.dumps({"answers": [{"index": 0, "choices": [1], "confidence": 0.9}]})
        llm, transport = make_answerer(reply)
        chain = ChainAnswerer([BankAnswerer(bank), llm], min_confidence=0.5)
        result = await chain.answer([q("题库没有的题")])
        self.assertEqual(result[0].choices, [1])
        self.assertEqual(len(transport.calls), 1)


if __name__ == "__main__":
    unittest.main()
