"""图片题（视觉）作答测试 —— 完全离线，绝不联网。

用假 Transport 同时扮演两个角色：
- ``/chat/completions``：按脚本回模型输出；
- 图片 URL：回预设字节/状态码。

这样「下载 → base64 → 多模态消息 → 解析 → Answer」整条链路都能在内存里跑完，
既断言了协议形状，也断言了失败降级。
"""

from __future__ import annotations

import base64
import json
import unittest
from typing import Any

from kedaibiao.core.models import AnswerSource, Question, QuestionType
from kedaibiao.net.http import HttpClient, Response
from kedaibiao.tasks.llm import (
    CONFIDENCE_DISCOUNT,
    DEFAULT_USER_AGENT,
    MAX_IMAGE_BYTES,
    MAX_IMAGES,
    LLMAnswerer,
    LLMConfig,
    VisionAnswerer,
    build_llm_answerer,
    has_images,
    image_urls,
)

#: 1x1 透明 PNG。足够验证「下载 → base64 → data URL」无损往返。
PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

STEM_IMG = "https://p.ananas.chaoxing.com/star3/origin/stem.png"
OPT_A_IMG = "https://p.ananas.chaoxing.com/star3/origin/opt-a.png"
OPT_B_IMG = "https://p.ananas.chaoxing.com/star3/origin/opt-b.png"
DEFAULT_IMAGES = {
    STEM_IMG: (200, PNG_1PX, "image/png"),
    OPT_A_IMG: (200, PNG_1PX, "image/png"),
    OPT_B_IMG: (200, PNG_1PX, "image/png"),
}


class FakeTransport:
    """记录请求、按脚本回包；图片 URL 走预设字节。"""

    def __init__(
        self,
        *replies: str,
        images: dict[str, tuple[int, bytes, str]] | None = None,
        errors: dict[str, Exception] | None = None,
    ) -> None:
        self.replies = list(replies)
        self.images = DEFAULT_IMAGES if images is None else images
        self.errors = errors or {}
        self.chat_status = 200
        self.calls: list[dict[str, Any]] = []

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        timeout: float = 15.0,
    ) -> Response:
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": headers or {},
                "body": body,
                "timeout": timeout,
            }
        )
        if url.endswith("/chat/completions"):
            if self.chat_status != 200:
                return Response(self.chat_status, url, b'{"error":"boom"}')
            if not self.replies:
                raise AssertionError("没有更多预设回复")
            payload = {"choices": [{"message": {"content": self.replies.pop(0)}}]}
            return Response(200, url, json.dumps(payload).encode())
        if url in self.errors:
            raise self.errors[url]
        if url in self.images:
            status, data, content_type = self.images[url]
            headers = {"Content-Type": content_type} if content_type else {}
            return Response(status, url, data, headers=headers)
        return Response(404, url, b"<html>not found</html>", headers={"Content-Type": "text/html"})


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------


def make_answerer(*replies: str, **kwargs: Any) -> tuple[VisionAnswerer, FakeTransport]:
    images = kwargs.pop("images", None)
    errors = kwargs.pop("errors", None)
    transport = FakeTransport(*replies, images=images, errors=errors)
    config = LLMConfig(
        base_url="https://api.example.com/v1",
        api_key="sk-test",
        model="text-model",
        **kwargs,
    )
    return VisionAnswerer(config, client=HttpClient(transport)), transport


def q(
    stem: str = "如图所示，物体的加速度是",
    options: tuple[str, ...] = ("甲", "乙", "丙"),
    qtype: QuestionType = QuestionType.SINGLE,
    images: tuple[str, ...] = (),
    option_images: Any = None,
    remote_id: str = "q1",
) -> Question:
    raw: dict[str, Any] = {}
    if images:
        raw["images"] = list(images)
    if option_images is not None:
        raw["option_images"] = option_images
    return Question(
        stem=stem,
        type=qtype,
        options=list(options),
        remote_id=remote_id,
        raw=raw,
    )


def image_question(**kwargs: Any) -> Question:
    kwargs.setdefault("images", (STEM_IMG,))
    return q(**kwargs)


def chat_calls(transport: FakeTransport) -> list[dict[str, Any]]:
    return [call for call in transport.calls if call["url"].endswith("/chat/completions")]


def image_calls(transport: FakeTransport) -> list[dict[str, Any]]:
    return [call for call in transport.calls if not call["url"].endswith("/chat/completions")]


def payload_of(call: dict[str, Any]) -> dict[str, Any]:
    return json.loads(call["body"].decode())


def user_content(call: dict[str, Any]) -> Any:
    return payload_of(call)["messages"][1]["content"]


def image_parts(call: dict[str, Any]) -> list[dict[str, Any]]:
    content = user_content(call)
    assert isinstance(content, list), "视觉请求的 user content 必须是 parts 数组"
    return [part for part in content if part.get("type") == "image_url"]


def text_part(call: dict[str, Any]) -> str:
    content = user_content(call)
    assert isinstance(content, list)
    return next(part["text"] for part in content if part.get("type") == "text")


def data_url_of(part: dict[str, Any]) -> str:
    return part["image_url"]["url"]


def vision_reply(
    choices: Any = ("A",), confidence: float = 0.9, reason: str = "看图可知", text: str = ""
) -> str:
    return json.dumps(
        {"choices": list(choices), "confidence": confidence, "text": text, "reason": reason},
        ensure_ascii=False,
    )


# --------------------------------------------------------------------------
# 图片提取
# --------------------------------------------------------------------------


class TestImageExtraction(unittest.TestCase):
    def test_stem_and_option_images_in_send_order(self) -> None:
        question = q(images=(STEM_IMG,), option_images={0: [OPT_A_IMG], "B": [OPT_B_IMG]})
        self.assertTrue(has_images(question))
        self.assertEqual(image_urls(question), [STEM_IMG, OPT_A_IMG, OPT_B_IMG])

    def test_no_images(self) -> None:
        self.assertFalse(has_images(q()))
        self.assertEqual(image_urls(q()), [])

    def test_option_key_forms(self) -> None:
        question = q(
            options=("甲", "乙", "丙", "丁"),
            option_images={0: [OPT_A_IMG], "1": [OPT_B_IMG], "c": ["https://x/c.png"]},
        )
        self.assertEqual(image_urls(question), [OPT_A_IMG, OPT_B_IMG, "https://x/c.png"])

    def test_option_images_list_form(self) -> None:
        question = q(options=("甲", "乙"), option_images=[[OPT_A_IMG], [OPT_B_IMG]])
        self.assertEqual(image_urls(question), [OPT_A_IMG, OPT_B_IMG])

    def test_stem_images_alias(self) -> None:
        question = Question(stem="题", options=["甲"], raw={"stem_images": [STEM_IMG]})
        self.assertEqual(image_urls(question), [STEM_IMG])

    def test_duplicate_url_sent_once(self) -> None:
        question = q(images=(STEM_IMG,), option_images={0: [STEM_IMG]})
        self.assertEqual(image_urls(question), [STEM_IMG])

    def test_non_http_urls_ignored(self) -> None:
        question = q(
            images=(
                "data:image/png;base64,AAAA",
                "/relative/x.png",
                "ftp://x/y.png",
                STEM_IMG,
            )
        )
        self.assertEqual(image_urls(question), [STEM_IMG])

    def test_malformed_raw_tolerated(self) -> None:
        broken = Question(stem="题", raw={"images": 123, "option_images": "junk"})
        self.assertEqual(image_urls(broken), [])
        non_dict = Question(stem="题", raw=None)  # type: ignore[arg-type]
        self.assertEqual(image_urls(non_dict), [])
        self.assertFalse(has_images(non_dict))


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------


class TestVisionConfig(unittest.TestCase):
    def test_vision_model_falls_back_to_main_model(self) -> None:
        self.assertEqual(LLMConfig(model="m").vision_model_name, "m")
        self.assertEqual(LLMConfig(model="m", vision_model="vl").vision_model_name, "vl")

    def test_defaults_match_contract(self) -> None:
        config = LLMConfig()
        self.assertEqual(config.image_timeout, 15.0)
        self.assertEqual(config.max_images, MAX_IMAGES)
        self.assertEqual(config.max_image_bytes, MAX_IMAGE_BYTES)
        self.assertEqual(config.user_agent, DEFAULT_USER_AGENT)
        self.assertIn("Mozilla/5.0", config.user_agent)
        self.assertIn("Chrome/131", config.user_agent)

    def test_from_env(self) -> None:
        config = LLMConfig.from_env(
            {
                "KEDAIBIAO_LLM_BASE_URL": "https://api.example.com/v1/",
                "KEDAIBIAO_LLM_API_KEY": "sk-1",
                "KEDAIBIAO_LLM_MODEL": "text-model",
                "KEDAIBIAO_LLM_VISION_MODEL": "vision-model",
                "KEDAIBIAO_LLM_IMAGE_TIMEOUT": "8",
                "KEDAIBIAO_LLM_MAX_IMAGES": "2",
                "KEDAIBIAO_LLM_MAX_IMAGE_BYTES": "1000",
            }
        )
        self.assertEqual(config.base_url, "https://api.example.com/v1")
        self.assertEqual(config.vision_model_name, "vision-model")
        self.assertEqual(config.image_timeout, 8.0)
        self.assertEqual(config.max_images, 2)
        self.assertEqual(config.max_image_bytes, 1000)

    def test_user_agent_override(self) -> None:
        config = LLMConfig.from_env({"KEDAIBIAO_LLM_USER_AGENT": "MyUA/1.0"})
        self.assertEqual(config.user_agent, "MyUA/1.0")

    def test_describe_mentions_vision_but_never_key(self) -> None:
        config = LLMConfig(
            base_url="https://x/v1", api_key="sk-secret-value", model="m", vision_model="vl"
        )
        text = config.describe()
        self.assertIn("vl", text)
        self.assertNotIn("sk-secret-value", text)


# --------------------------------------------------------------------------
# 视觉作答
# --------------------------------------------------------------------------


class TestVisionAnswering(unittest.IsolatedAsyncioTestCase):
    async def test_multimodal_payload_shape(self) -> None:
        answerer, transport = make_answerer(vision_reply())
        question = image_question(option_images={0: [OPT_A_IMG]})
        result = await answerer.answer([question])

        # 结果
        self.assertEqual(result[0].choices, [0])
        self.assertIs(result[0].source, AnswerSource.LLM)
        self.assertAlmostEqual(
            result[0].confidence, round(0.9 * CONFIDENCE_DISCOUNT, 4)
        )
        self.assertEqual(result[0].raw["reason"], "看图可知")
        self.assertEqual(result[0].raw["vision_images"], 2)

        # 请求形状
        call = chat_calls(transport)[0]
        self.assertIn("/chat/completions", call["url"])
        payload = payload_of(call)
        self.assertEqual(payload["model"], "text-model")
        self.assertEqual(payload["messages"][0]["role"], "system")
        self.assertIn("JSON", payload["messages"][0]["content"])

        parts = image_parts(call)
        self.assertEqual(len(parts), 2, "题干图 + 选项图各一个 image_url part")
        for part in parts:
            data_url = data_url_of(part)
            prefix, encoded = data_url.split(",", 1)
            self.assertEqual(prefix, "data:image/png;base64")
            self.assertEqual(base64.b64decode(encoded), PNG_1PX)

        body = text_part(call)
        self.assertIn("题干图片", body)
        self.assertIn("选项A的图片", body)
        self.assertIn("图1", body)
        self.assertIn("图2", body)
        self.assertIn("题干: 如图所示", body)

    async def test_letters_mapped_to_indices(self) -> None:
        answerer, _ = make_answerer(vision_reply(choices=("C",)))
        result = await answerer.answer([image_question()])
        self.assertEqual(result[0].choices, [2], "字母 C 应映射为下标 2")

    async def test_multiple_choice_letters(self) -> None:
        answerer, _ = make_answerer(vision_reply(choices=("C", "A")))
        result = await answerer.answer([image_question(qtype=QuestionType.MULTIPLE)])
        self.assertEqual(result[0].choices, [0, 2], "多选字母应升序去重")

    async def test_numeric_choices_tolerated(self) -> None:
        answerer, _ = make_answerer(vision_reply(choices=(1,)))
        result = await answerer.answer([image_question()])
        self.assertEqual(result[0].choices, [1], "模型回了下标也认")

    async def test_fenced_json_parsed(self) -> None:
        answerer, _ = make_answerer(f"好的：\n```json\n{vision_reply()}\n```")
        result = await answerer.answer([image_question()])
        self.assertEqual(result[0].choices, [0])

    async def test_answers_array_shape_tolerated(self) -> None:
        reply = json.dumps(
            {"answers": [{"choices": ["B"], "confidence": 0.8}]}, ensure_ascii=False
        )
        answerer, _ = make_answerer(reply)
        result = await answerer.answer([image_question()])
        self.assertEqual(result[0].choices, [1])

    async def test_fill_question_uses_text(self) -> None:
        answerer, _ = make_answerer(vision_reply(choices=(), text="42"))
        result = await answerer.answer([image_question(qtype=QuestionType.FILL)])
        self.assertEqual(result[0].text, "42")
        self.assertEqual(result[0].choices, [])

    async def test_image_count_capped(self) -> None:
        urls = [
            f"https://p.ananas.chaoxing.com/star3/origin/img{i}.png"
            for i in range(MAX_IMAGES + 2)
        ]
        images = {url: (200, PNG_1PX, "image/png") for url in urls}
        answerer, transport = make_answerer(vision_reply(), images=images)
        await answerer.answer([image_question(images=tuple(urls))])

        self.assertEqual(len(image_calls(transport)), MAX_IMAGES, "超出的图根本不该下载")
        call = chat_calls(transport)[0]
        self.assertEqual(len(image_parts(call)), MAX_IMAGES)
        self.assertNotIn("注意：另有", text_part(call), "被上限裁掉的不算下载失败")

    async def test_zero_image_budget_degrades(self) -> None:
        answerer, transport = make_answerer(vision_reply(), max_images=0)
        result = await answerer.answer([image_question()])
        self.assertTrue(result[0].is_empty())
        self.assertEqual(transport.calls, [], "预算为 0 时不该下载也不该请求")
        self.assertIn("MAX_IMAGES", answerer.last_error)

    async def test_oversized_image_skipped(self) -> None:
        huge = b"\x89PNG\r\n\x1a\n" + b"x" * MAX_IMAGE_BYTES
        answerer, transport = make_answerer(
            vision_reply(), images={STEM_IMG: (200, huge, "image/png")}
        )
        result = await answerer.answer([image_question()])

        self.assertTrue(result[0].is_empty(), "唯一一张图超限 → 空答案")
        self.assertIs(result[0].source, AnswerSource.UNKNOWN)
        self.assertEqual(chat_calls(transport), [], "没图可用就不该发请求")
        self.assertIn("图片", answerer.last_error)

    async def test_all_downloads_fail_degrades_to_empty(self) -> None:
        answerer, transport = make_answerer(
            vision_reply(),
            errors={STEM_IMG: ConnectionError("连不上 CDN")},
        )
        question = image_question(option_images={1: ["https://p.ananas.chaoxing.com/missing.png"]})
        result = await answerer.answer([question])

        self.assertEqual(len(result), 1)
        self.assertTrue(result[0].is_empty())
        self.assertIs(result[0].source, AnswerSource.UNKNOWN)
        self.assertEqual(result[0].confidence, 0.0)
        self.assertEqual(chat_calls(transport), [])
        self.assertIn("图片", answerer.last_error)

    async def test_partial_download_keeps_going(self) -> None:
        answerer, transport = make_answerer(vision_reply())
        question = image_question(
            images=(STEM_IMG,), option_images={0: [OPT_A_IMG]}
        )
        errors = {STEM_IMG: ConnectionError("第一张挂了")}
        transport.errors = errors

        result = await answerer.answer([question])
        self.assertEqual(result[0].choices, [0], "一张图失败不影响整题")
        call = chat_calls(transport)[0]
        self.assertEqual(len(image_parts(call)), 1)
        self.assertIn("另有 1 张图片下载失败", text_part(call))

    async def test_non_image_response_skipped(self) -> None:
        answerer, transport = make_answerer(
            vision_reply(),
            images={STEM_IMG: (200, b"<html>login</html>", "text/html")},
        )
        result = await answerer.answer([image_question()])
        self.assertTrue(result[0].is_empty())
        self.assertEqual(chat_calls(transport), [])

    async def test_magic_bytes_sniffed_without_content_type(self) -> None:
        answerer, transport = make_answerer(vision_reply(), images={STEM_IMG: (200, PNG_1PX, "")})
        result = await answerer.answer([image_question()])
        self.assertEqual(result[0].choices, [0])
        part = image_parts(chat_calls(transport)[0])[0]
        self.assertEqual(
            data_url_of(part),
            "data:image/png;base64," + base64.b64encode(PNG_1PX).decode("ascii"),
        )

    async def test_model_garbage_degrades(self) -> None:
        answerer, _ = make_answerer("我拒绝输出 JSON")
        result = await answerer.answer([image_question()])
        self.assertTrue(result[0].is_empty())
        self.assertIn("JSON", answerer.last_error)

    async def test_http_error_degrades(self) -> None:
        answerer, transport = make_answerer(vision_reply())
        transport.chat_status = 500
        result = await answerer.answer([image_question()])
        self.assertTrue(result[0].is_empty())
        self.assertIn("500", answerer.last_error)

    async def test_download_headers_and_timeout(self) -> None:
        answerer, transport = make_answerer(vision_reply())
        await answerer.answer([image_question()])

        call = image_calls(transport)[0]
        self.assertEqual(call["method"], "GET")
        self.assertEqual(call["headers"]["User-Agent"], DEFAULT_USER_AGENT)
        self.assertIn("chaoxing.com", call["headers"]["Referer"])
        self.assertEqual(call["timeout"], 15.0, "图片下载超时必须是 15s")
        self.assertIn("Chrome/131", chat_calls(transport)[0]["headers"]["User-Agent"])
        self.assertEqual(
            chat_calls(transport)[0]["headers"]["Authorization"], "Bearer sk-test"
        )

    async def test_custom_user_agent_used(self) -> None:
        answerer, transport = make_answerer(vision_reply(), user_agent="UA/2.0")
        await answerer.answer([image_question()])
        self.assertEqual(image_calls(transport)[0]["headers"]["User-Agent"], "UA/2.0")
        self.assertEqual(chat_calls(transport)[0]["headers"]["User-Agent"], "UA/2.0")

    async def test_empty_list_makes_no_request(self) -> None:
        answerer, transport = make_answerer()
        self.assertEqual(await answerer.answer([]), [])
        self.assertEqual(transport.calls, [])

    async def test_disabled_makes_no_request(self) -> None:
        answerer = VisionAnswerer(LLMConfig())
        result = await answerer.answer([image_question()])
        self.assertTrue(result[0].is_empty())
        self.assertIs(result[0].source, AnswerSource.UNKNOWN)

    async def test_raw_payload_not_mutated(self) -> None:
        answerer, _ = make_answerer(vision_reply())
        question = image_question()
        before = json.dumps(question.raw, ensure_ascii=False)
        await answerer.answer([question])
        self.assertEqual(json.dumps(question.raw, ensure_ascii=False), before)



# --------------------------------------------------------------------------
# 与文本链路混合
# --------------------------------------------------------------------------


class TestMixedBatch(unittest.IsolatedAsyncioTestCase):
    async def test_text_questions_keep_batch_path(self) -> None:
        reply = json.dumps(
            {"answers": [
                {"index": 0, "choices": [1], "confidence": 0.9},
                {"index": 1, "choices": [2], "confidence": 0.9},
            ]}
        )
        answerer, transport = make_answerer(reply)
        result = await answerer.answer([q(stem="题一"), q(stem="题二")])

        self.assertEqual([item.choices for item in result], [[1], [2]])
        calls = chat_calls(transport)
        self.assertEqual(len(calls), 1, "纯文本题仍然批量一问")
        content = user_content(calls[0])
        self.assertIsInstance(content, str, "文本链路 content 必须是字符串")
        self.assertIn("题一", content)
        self.assertIn("题二", content)

    async def test_mixed_order_preserved(self) -> None:
        text_reply = json.dumps({"answers": [{"index": 0, "choices": [1], "confidence": 0.9}]})
        answerer, transport = make_answerer(text_reply, vision_reply(choices=("C",)))
        questions = [q(stem="纯文本题"), image_question(stem="图片题")]
        result = await answerer.answer(questions)

        self.assertEqual(result[0].choices, [1])
        self.assertEqual(result[1].choices, [2])
        self.assertFalse(result[0].raw.get("vision_images"))
        self.assertEqual(result[1].raw["vision_images"], 1)

        calls = chat_calls(transport)
        self.assertEqual(len(calls), 2)
        self.assertIsInstance(user_content(calls[0]), str)
        self.assertIsInstance(user_content(calls[1]), list)

    async def test_vision_model_override_used(self) -> None:
        text_reply = json.dumps({"answers": [{"index": 0, "choices": [0], "confidence": 0.9}]})
        answerer, transport = make_answerer(text_reply, vision_reply(), vision_model="vl-1")
        await answerer.answer([q(stem="纯文本"), image_question()])

        models = [payload_of(call)["model"] for call in chat_calls(transport)]
        self.assertEqual(models, ["text-model", "vl-1"], "文本用主模型，视觉用 VISION_MODEL")


# --------------------------------------------------------------------------
# build_llm_answerer / 兼容性
# --------------------------------------------------------------------------


class TestBuild(unittest.IsolatedAsyncioTestCase):
    ENV = {
        "KEDAIBIAO_LLM_BASE_URL": "https://api.example.com/v1",
        "KEDAIBIAO_LLM_API_KEY": "sk-test",
        "KEDAIBIAO_LLM_MODEL": "text-model",
    }

    async def test_build_returns_vision_answerer(self) -> None:
        answerer = build_llm_answerer(dict(self.ENV))
        self.assertIsNotNone(answerer)
        assert answerer is not None
        self.assertIsInstance(answerer, VisionAnswerer)
        self.assertIsInstance(answerer, LLMAnswerer, "既有 isinstance 判断必须仍然成立")
        self.assertTrue(answerer.supports_images)

    async def test_build_none_when_unconfigured(self) -> None:
        self.assertIsNone(build_llm_answerer({}))
        self.assertIsNone(build_llm_answerer({"KEDAIBIAO_LLM_MODEL": "m"}))

    async def test_built_answerer_still_answers_text(self) -> None:
        transport = FakeTransport(json.dumps(
            {"answers": [{"index": 0, "choices": [1], "confidence": 0.9}]}
        ))
        answerer = build_llm_answerer(dict(self.ENV), client=HttpClient(transport))
        assert answerer is not None
        result = await answerer.answer([q(stem="纯文本题")])
        self.assertEqual(result[0].choices, [1])

    async def test_built_answerer_answers_image_question(self) -> None:
        transport = FakeTransport(vision_reply())
        answerer = build_llm_answerer(dict(self.ENV), client=HttpClient(transport))
        assert answerer is not None
        result = await answerer.answer([image_question()])
        self.assertEqual(result[0].choices, [0])
        self.assertEqual(len(image_parts(chat_calls(transport)[0])), 1)

    async def test_built_answerer_honours_env_vision_model(self) -> None:
        env = {**self.ENV, "KEDAIBIAO_LLM_VISION_MODEL": "vl-from-env"}
        transport = FakeTransport(vision_reply())
        answerer = build_llm_answerer(env, client=HttpClient(transport))
        assert answerer is not None
        await answerer.answer([image_question()])
        self.assertEqual(payload_of(chat_calls(transport)[0])["model"], "vl-from-env")


if __name__ == "__main__":
    unittest.main()
