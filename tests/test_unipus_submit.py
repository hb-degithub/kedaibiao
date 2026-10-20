"""U校园提交链路测试（**完全离线**，不访问任何真实站点）。

依据：

- 形状：``docs/research/unipus-protocol.md`` §8.1；
- 取值语义：``docs/research/uai-core-submit-facts.md`` §1–§5
  （``isCompleted`` 恒 true / ``thirdPartyJudges`` 对象数组且严格等长 /
  ``answer.children`` 按内容侧 child_count / ``submitType`` 纯派生）。

所有网络交互都注入假 ``Transport``；内容/答案响应现场加密（与真实样本
``data/recon/unipus/sample-*.json`` 的**形状**一致，不读那些文件）。
"""

from __future__ import annotations

import json
import unittest
from dataclasses import dataclass
from typing import Any

from kedaibiao.core.models import (
    Answer,
    AnswerSource,
    Assignment,
    Question,
)
from kedaibiao.net.http import HttpClient, Response
from kedaibiao.platforms.base import AdapterContext, NotSupportedError
from kedaibiao.platforms.unipus import crypto, endpoints
from kedaibiao.platforms.unipus.adapter import UnipusAdapter
from kedaibiao.platforms.unipus.errors import UnipusRateLimited
from kedaibiao.platforms.unipus.submit import (
    FULL_MARKS,
    JUDGE_VERSIONS,
    SubmitPayloadError,
    build_answer_object,
    build_judge_object,
    build_ques_data,
    build_submit_plan,
    judge_item_count,
    parse_submit_result,
)

# --------------------------------------------------------------------------
# 固定样本（形状取自 2026-10-20 真实联调样本）
# --------------------------------------------------------------------------

K = "20260921"
NOW_MS = 1_700_000_000_000
#: 19 位题目 ID（绝不能过 float —— 规格 §6.2）
BIG_ID = "1474574379963842561"
INSTANCE_ID = "80001"
GROUP_ID = "u1g1"
REMOTE_ID = f"{INSTANCE_ID}/{GROUP_ID}"


class FakeTransport:
    """按脚本返回响应并记录调用的假传输层。"""

    def __init__(self, *responses: Any) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def request(self, method, url, *, headers=None, body=None, timeout=15.0):
        self.calls.append(
            {"method": method, "url": url, "headers": dict(headers or {}),
             "body": body, "timeout": timeout}
        )
        if not self.responses:
            raise AssertionError(f"FakeTransport 没有更多预设响应，但收到了 {method} {url}")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    @property
    def methods(self) -> list[str]:
        return [call["method"] for call in self.calls]

    def posts(self) -> list[dict[str, Any]]:
        return [call for call in self.calls if call["method"] == "POST"]


def json_response(payload: Any, status: int = 200) -> Response:
    return Response(status=status, url="https://x/", body=json.dumps(payload, ensure_ascii=False).encode())


def ecb_encrypt(key: bytes, data: bytes) -> bytes:
    """ZeroPadding + AES-128-ECB 单块串联（与真实密文外壳同算法）。"""
    padded = data + b"\x00" * ((-len(data)) % 16) or b"\x00" * 16
    return b"".join(
        crypto.aes128_encrypt_block(key, padded[offset: offset + 16])
        for offset in range(0, len(padded), 16)
    )


def wrap_cipher(text: str, k: str = K) -> str:
    key = ("1a2b3c4d" + k).encode("ascii")
    return "unipus." + ecb_encrypt(key, text.encode("utf-8")).hex()


def content_item(
    *,
    item_id: str = BIG_ID,
    reply_type: str = "singlechoice",
    qtype: str = "basic",
    category: int = 1,
    child_count: int = 4,
    content_types: tuple[int, ...] = (4, 2),
) -> dict[str, Any]:
    """一条内容项：``{id, content:"<JSON字符串>"}``（真实形状）。"""
    children = [
        {
            "replyType": reply_type,
            "type": qtype,
            "category": category,
            "quesText": f"<p>{index + 1}. question</p>",
            "options": [{"name": "A", "text": "<p>甲</p>", "value": "A"}],
            "children": [],
            "contents": [],
        }
        for index in range(child_count)
    ]
    inner = {
        "replyType": reply_type,
        "type": qtype,
        "category": category,
        "children": children,
        "contents": [{"type": entry_type, "path": "https://cdn/x"} for entry_type in content_types],
    }
    return {"id": item_id, "content": json.dumps(inner, ensure_ascii=False)}


def answer_item(
    *,
    item_id: str = BIG_ID,
    groups: list[list[str]] | None = None,
    with_answer: bool = True,
    empty_string: bool = False,
) -> dict[str, Any]:
    """一条答案项：``answer`` 是字符串化 JSON；口语/主观/讨论题可以没有 ``answers``。

    ``empty_string=True`` 复刻 2026-10-20 真实样本：``{"answer": ""}``。
    """
    item: dict[str, Any] = {"id": item_id, "quesId": "1400025095004880896"}
    if empty_string:
        item["answer"] = ""
        return item
    if with_answer:
        item["answer"] = json.dumps(
            {"children": [{"answers": group} for group in (groups or [])]},
            ensure_ascii=False,
        )
    return item


def content_response(items: list[dict[str, Any]]) -> Response:
    return json_response({"code": 0, "k": K, "content": wrap_cipher(json.dumps(items, ensure_ascii=False))})


def answer_response(items: list[dict[str, Any]]) -> Response:
    return json_response({"code": 0, "k": K, "data": wrap_cipher(json.dumps(items, ensure_ascii=False))})


def submit_response(*, code: Any = 0, score=(1, 1, 1, 1), score_pct: float = 1.0) -> Response:
    return json_response(
        {
            "code": code,
            "data": {
                "state": {"state": 1, "score": list(score), "score_pct": score_pct},
                "version": "v3",
            },
        }
    )


def progress_response(*, first_pass: str | None = "2026-10-20 10:00:00") -> Response:
    return json_response(
        {
            "code": 0,
            "data": {
                "first_pass": {"datetime": first_pass},
                "state": {"state": 1, "lastSubmit": "9", "score": [1, 1], "score_pct": 1.0},
                "flowStrategy": {"required": True},
                "strategy": {"task_mini_score_pct": 0.6},
            },
        }
    )


def make_adapter(*responses: Any, **kwargs: Any) -> tuple[UnipusAdapter, FakeTransport]:
    fake = FakeTransport(*responses)
    adapter = UnipusAdapter(AdapterContext(http=HttpClient(fake)), clock=lambda: NOW_MS, **kwargs)
    session = adapter.client().session
    session.portal_token = "PORTAL-JWT"
    session.open_id = "900001"
    session.school = "1000"
    return adapter, fake


def asked_answers(count: int = 2) -> list[Answer]:
    """通用 answerer 产出的答案（提交时**不参与载荷**，只做数量对照）。"""
    return [
        Answer(
            question=Question(stem=f"我们自己的答案 {index}", remote_id=f"x{index}"),
            text="不该被发出去",
            source=AnswerSource.LLM,
            confidence=0.9,
        )
        for index in range(count)
    ]


# --------------------------------------------------------------------------
# 纯函数：answer 对象
# --------------------------------------------------------------------------


class TestAnswerObject(unittest.TestCase):
    def test_shape_default_is_array_per_child(self) -> None:
        obj = build_answer_object(children=[("A",), ("B", "C")])
        self.assertEqual(obj["value"], [])
        self.assertEqual(
            obj["children"],
            [{"value": ["A"], "isDone": True}, {"value": ["B", "C"], "isDone": True}],
        )
        self.assertEqual(obj["record"], {"url": ""})

    def test_progress_keys_are_contents_indexes_not_type_values(self) -> None:
        # 真实样本：contents = [type 4 正文, type 2 视频] → 媒体下标是 1，不是 2
        obj = build_answer_object(children=[()], media_indexes=(1,))
        self.assertEqual(obj["progress"], {"1": True})

    def test_subjective_value_is_string_and_reply_flag(self) -> None:
        obj = build_answer_object(
            children=[("第一行", "第二行"), ()], media_indexes=(0,), reply=True, subjective=True
        )
        self.assertEqual(obj["children"][0]["value"], "第一行\n第二行")
        self.assertEqual(obj["children"][1]["value"], "")
        self.assertTrue(obj["progress"]["reply"])

    def test_subjective_placeholder_is_explicit(self) -> None:
        obj = build_answer_object(
            children=[()], subjective=True, subjective_placeholder="[已提交]"
        )
        self.assertEqual(obj["children"][0]["value"], "[已提交]")

    def test_full_record_only_when_self_reporting(self) -> None:
        conservative = build_answer_object(children=[()])
        self.assertEqual(conservative["record"], {"url": ""})
        full = build_answer_object(children=[()], self_report_scores=True)
        self.assertEqual(full["record"]["specific_scores"], FULL_MARKS)
        self.assertEqual(full["record"]["recordDetail"]["score"], 100)


# --------------------------------------------------------------------------
# 纯函数：thirdPartyJudges 元素
# --------------------------------------------------------------------------


@dataclass
class FakeQuestion:
    instance_id: str = BIG_ID
    unipus_type: str = "single-choice"
    child_count: int = 1
    item_count: int = 0
    question_type: str = "basic"
    reply_type: str = "singlechoice"
    category: int | None = 1
    media_indexes: tuple[int, ...] = ()


@dataclass
class FakeStandard:
    children: tuple[tuple[str, ...], ...] = ()


class TestJudgeObject(unittest.TestCase):
    def test_objective_family_has_exactly_five_keys(self) -> None:
        element = build_judge_object(
            question=FakeQuestion(unipus_type="multi-choice", child_count=2),
            standard=FakeStandard((("A", "B", "D"), ("C",))),
            child_index=0,
        )
        self.assertEqual(
            set(element),
            {"value", "question_type", "reply_type", "versions", "payloads"},
        )
        self.assertEqual(element["value"], "A,B,D")
        self.assertEqual(element["payloads"], [])
        self.assertEqual(element["versions"], JUDGE_VERSIONS)
        self.assertEqual(element["question_type"], "basic")
        self.assertEqual(element["reply_type"], "singlechoice")

    def test_subjective_has_the_same_five_keys(self) -> None:
        element = build_judge_object(
            question=FakeQuestion(unipus_type="subjective", reply_type="text-area"),
            standard=FakeStandard((("你好",),)),
        )
        self.assertEqual(
            set(element),
            {"value", "question_type", "reply_type", "versions", "payloads"},
        )
        self.assertEqual(element["value"], "你好")

    def test_upload_has_no_value(self) -> None:
        element = build_judge_object(
            question=FakeQuestion(unipus_type="upload", reply_type="multiFileUpload"),
        )
        self.assertNotIn("value", element)
        self.assertEqual(element["desc"], "")
        self.assertEqual(element["payloads"], [])
        self.assertIn("versions", element)

    def test_oral_conservative_by_default(self) -> None:
        element = build_judge_object(question=FakeQuestion(unipus_type="oral", reply_type="record"))
        self.assertEqual(element["payloads"], [{"url": ""}])
        self.assertNotIn("specific_scores", element, "默认不自报分数")
        self.assertEqual(element["value"], "")

    def test_oral_full_record_when_opted_in(self) -> None:
        element = build_judge_object(
            question=FakeQuestion(unipus_type="oral", reply_type="record"),
            self_report_scores=True,
        )
        self.assertEqual(element["specific_scores"], FULL_MARKS)
        self.assertEqual(element["payloads"][0]["recordDetail"]["score"], 100)

    def test_oral_state_value_is_mapping_of_leading_children(self) -> None:
        element = build_judge_object(
            question=FakeQuestion(unipus_type="oral-state", child_count=3, reply_type="record"),
            standard=FakeStandard((("A",), ("B", "C"), ("真正的作答",))),
        )
        self.assertEqual(element["value"], {"0": "A", "1": ["B", "C"]})
        self.assertEqual(len(element["payloads"]), 1, "oral-state 整题只算 1 项")

    def test_role_play_payloads_expand_by_child_count(self) -> None:
        element = build_judge_object(
            question=FakeQuestion(unipus_type="role-play", child_count=3, reply_type="record"),
        )
        self.assertEqual(element["role"], "")
        self.assertEqual(len(element["payloads"]), 3)
        self.assertNotIn("value", element, "role-play 没有 value 字段")

    def test_item_counts_follow_facts(self) -> None:
        self.assertEqual(judge_item_count(FakeQuestion(unipus_type="study", child_count=5)), 0)
        self.assertEqual(judge_item_count(FakeQuestion(unipus_type="role-play", child_count=5)), 1)
        self.assertEqual(judge_item_count(FakeQuestion(unipus_type="oral-state", child_count=5)), 1)
        self.assertEqual(judge_item_count(FakeQuestion(unipus_type="single-choice", child_count=5)), 5)
        self.assertEqual(judge_item_count(FakeQuestion(unipus_type="single-choice", child_count=0)), 1)


# --------------------------------------------------------------------------
# 纯函数：quesDatas
# --------------------------------------------------------------------------


class TestQuesData(unittest.TestCase):
    def test_forward_standard_answers(self) -> None:
        data = build_ques_data(
            questions=[FakeQuestion(unipus_type="single-choice", child_count=2)],
            standard_answers=[FakeStandard((("X",), ("Y",)))],
        )
        answer = json.loads(data[0]["answer"])
        self.assertEqual(answer["children"][0]["value"], ["X"])
        self.assertEqual(data[0]["instanceId"], BIG_ID)
        self.assertEqual(data[0]["instanceId"], str(data[0]["instanceId"]),
                         "19 位 ID 必须是字符串")
        self.assertEqual(json.loads(data[0]["context"]), {"state": "submitted"})
        self.assertEqual(data[0]["contextVersion"], 1)
        self.assertEqual(data[0]["answerVersion"], 1)

    def test_children_length_follows_content_child_count(self) -> None:
        """facts §9.1-3：children 长度按内容侧，缺项填空数组。"""
        data = build_ques_data(
            questions=[FakeQuestion(unipus_type="single-choice", child_count=4)],
            standard_answers=[FakeStandard((("A",), ("B",)))],
            blockers=[],
        )
        answer = json.loads(data[0]["answer"])
        self.assertEqual(len(answer["children"]), 4)
        self.assertEqual(answer["children"][3]["value"], [])

    def test_child_count_mismatch_is_blocked(self) -> None:
        blockers: list[str] = []
        build_ques_data(
            questions=[FakeQuestion(unipus_type="single-choice", child_count=4)],
            standard_answers=[FakeStandard((("A",), ("B",)))],
            blockers=blockers,
        )
        self.assertEqual(len(blockers), 1)
        self.assertIn("子题数不一致", blockers[0])
        self.assertIn("不可提交", blockers[0])

    def test_child_count_mismatch_raises_without_blocker_sink(self) -> None:
        with self.assertRaises(SubmitPayloadError) as cm:
            build_ques_data(
                questions=[FakeQuestion(unipus_type="single-choice", child_count=4)],
                standard_answers=[FakeStandard((("A",), ("B",)))],
            )
        self.assertIn("子题数不一致", str(cm.exception))

    def test_mismatched_answer_count_rejected(self) -> None:
        with self.assertRaises(SubmitPayloadError) as cm:
            build_ques_data(
                questions=[FakeQuestion(instance_id="a"), FakeQuestion(instance_id="b")],
                standard_answers=[FakeStandard((("X",),))],
            )
        self.assertIn("不一致", str(cm.exception))

    def test_non_objective_empty_answer_is_padded_not_rejected(self) -> None:
        """2026-10-20 真实样本：discussion/text-area 的答案侧是空字符串/0 项。

        这类题**不是**客观题，答案侧为空是正常的：按内容侧 child_count 补齐，
        不抛错、也不 block。
        """
        questions = [
            FakeQuestion(instance_id="d", unipus_type="study", child_count=1),
            FakeQuestion(instance_id="s", unipus_type="subjective",
                         reply_type="text-area", child_count=5),
            FakeQuestion(instance_id="o", unipus_type="oral", reply_type="record",
                         child_count=2),
        ]
        data = build_ques_data(
            questions=questions,
            standard_answers=[FakeStandard(()), FakeStandard(()), FakeStandard(())],
        )
        self.assertEqual(len(data), 3)
        self.assertEqual(len(json.loads(data[0]["answer"])["children"]), 1)
        self.assertEqual(len(json.loads(data[1]["answer"])["children"]), 5)
        self.assertEqual(len(json.loads(data[2]["answer"])["children"]), 2)

    def test_objective_mismatch_still_raises(self) -> None:
        with self.assertRaises(SubmitPayloadError):
            build_ques_data(
                questions=[FakeQuestion(unipus_type="single-choice", child_count=4)],
                standard_answers=[FakeStandard((("A",), ("B",)))],
            )


# --------------------------------------------------------------------------
# 纯函数：提交计划
# --------------------------------------------------------------------------


class TestSubmitPlan(unittest.TestCase):
    def _plan(self, **kwargs: Any):
        questions = kwargs.pop("questions", None)
        if questions is None:
            questions = [
                FakeQuestion(instance_id="a", unipus_type="single-choice", child_count=2),
                FakeQuestion(instance_id="b", unipus_type="single-choice", child_count=2),
            ]
        standards = kwargs.pop(
            "standard_answers",
            [FakeStandard((("A",), ("B",))) for _ in questions],
        )
        base = dict(
            course_id=f"course-v2:{INSTANCE_ID}",
            group_id=GROUP_ID,
            open_id="900001",
            questions=questions,
            standard_answers=standards,
        )
        base.update(kwargs)
        return build_submit_plan(**base)

    def test_all_spec_keys_present(self) -> None:
        payload = self._plan().payload
        for key in (
            "courseId", "openId", "version", "quesDatas", "groupId",
            "isCompleted", "thirdPartyJudges", "submitType",
            "associationGroupId", "associationUnitId",
        ):
            self.assertIn(key, payload, key)
        self.assertEqual(payload["version"], "default")
        self.assertEqual(payload["courseId"], f"course-v2:{INSTANCE_ID}")
        self.assertEqual(payload["associationGroupId"], "")
        self.assertEqual(payload["associationUnitId"], "")

    def test_is_completed_is_all_true_and_judges_are_equal_length(self) -> None:
        plan = self._plan()
        self.assertEqual(plan.item_count, 4)
        self.assertEqual(plan.payload["isCompleted"], [True] * 4)
        self.assertTrue(all(value is True for value in plan.payload["isCompleted"]))
        judges = json.loads(plan.payload["thirdPartyJudges"])
        self.assertIsInstance(judges, list)
        self.assertEqual(len(judges), 4, "thirdPartyJudges 长度必须与 isCompleted 严格相等")
        for element in judges:
            self.assertIsInstance(element, dict, "元素必须是对象，不能是 null/空串")
        self.assertTrue(plan.submittable)

    def test_submit_type_is_pure_derivation(self) -> None:
        mixed = self._plan(
            questions=[
                FakeQuestion(unipus_type="study", child_count=1),
                FakeQuestion(unipus_type="single-choice", child_count=1),
            ],
            standard_answers=[FakeStandard(()), FakeStandard((("A",),))],
        )
        self.assertEqual(mixed.submit_type, 1)
        self.assertEqual(mixed.item_count, 1)

        pure_study = self._plan(
            questions=[FakeQuestion(unipus_type="study", child_count=0)],
            standard_answers=[FakeStandard(())],
        )
        self.assertEqual(pure_study.submit_type, 2, "整组学习题 → submitType=2")
        self.assertEqual(pure_study.item_count, 0)
        self.assertEqual(pure_study.payload["isCompleted"], [])
        self.assertEqual(pure_study.payload["thirdPartyJudges"], "[]")
        self.assertTrue(pure_study.submittable, "纯学习题组也要提交（进度靠写请求记录）")

    def test_content_children_drive_both_arrays(self) -> None:
        plan = self._plan(
            questions=[
                FakeQuestion(instance_id="a", unipus_type="role-play", child_count=3),
                FakeQuestion(instance_id="b", unipus_type="oral-state", child_count=2),
                FakeQuestion(instance_id="c", unipus_type="study", child_count=4),
                FakeQuestion(instance_id="d", unipus_type="subjective", child_count=2),
            ],
            standard_answers=[FakeStandard(()), FakeStandard(()), FakeStandard(()), FakeStandard(((), ()))],
        )
        self.assertEqual(plan.item_count, 1 + 1 + 0 + 2)
        self.assertEqual(len(plan.payload["isCompleted"]), plan.item_count)
        self.assertEqual(len(json.loads(plan.payload["thirdPartyJudges"])), plan.item_count)

    def test_empty_questions_refused(self) -> None:
        with self.assertRaises(SubmitPayloadError) as cm:
            self._plan(questions=[], standard_answers=[])
        self.assertIn("quesDatas", str(cm.exception))

    def test_missing_ids_refused(self) -> None:
        for key in ("course_id", "group_id", "open_id"):
            with self.assertRaises(SubmitPayloadError, msg=key):
                self._plan(**{key: ""})

    def test_answer_count_mismatch_refused(self) -> None:
        with self.assertRaises(SubmitPayloadError):
            self._plan(standard_answers=[FakeStandard((("A",),))])

    def test_child_count_mismatch_marks_not_submittable(self) -> None:
        plan = self._plan(
            questions=[FakeQuestion(unipus_type="single-choice", child_count=4)],
            standard_answers=[FakeStandard((("A",), ("B",)))],
        )
        self.assertFalse(plan.submittable)
        self.assertEqual(len(plan.blockers), 1)
        self.assertIn("子题数不一致", plan.blockers[0])
        self.assertTrue(any("本地校验未通过" in warning for warning in plan.warnings))
        self.assertFalse(plan.as_dict()["submittable"])

    def test_discussion_with_empty_answer_is_submittable(self) -> None:
        """真实样本（…/7302fcb43004729）：discussion + answer="" 不能被误拦。"""
        plan = self._plan(
            questions=[
                FakeQuestion(instance_id="d", unipus_type="study", child_count=1)
            ],
            standard_answers=[FakeStandard(())],
        )
        self.assertTrue(plan.submittable, plan.blockers)
        self.assertEqual(plan.blockers, [])
        self.assertEqual(plan.submit_type, 2, "整组只有学习题 → submitType=2")
        self.assertEqual(plan.item_count, 0)
        self.assertEqual(plan.payload["isCompleted"], [])
        self.assertEqual(plan.payload["thirdPartyJudges"], "[]")
        answer = json.loads(plan.payload["quesDatas"][0]["answer"])
        self.assertEqual(len(answer["children"]), 1, "按内容侧 child_count 补齐")

    def test_subjective_with_empty_answer_is_submittable(self) -> None:
        plan = self._plan(
            questions=[
                FakeQuestion(instance_id="s", unipus_type="subjective",
                             reply_type="text-area", child_count=5)
            ],
            standard_answers=[FakeStandard(())],
        )
        self.assertTrue(plan.submittable, plan.blockers)
        answer = json.loads(plan.payload["quesDatas"][0]["answer"])
        self.assertEqual(len(answer["children"]), 5)
        self.assertTrue(all(child["value"] == "" for child in answer["children"]))

    def test_missing_standard_answer_blocks_objective(self) -> None:
        plan = self._plan(
            questions=[FakeQuestion(unipus_type="single-choice", child_count=2)],
            standard_answers=[FakeStandard(())],
        )
        self.assertFalse(plan.submittable)
        self.assertTrue(
            any("标准答案" in blocker for blocker in plan.blockers),
            plan.blockers,
        )

    def test_warns_about_scored_and_subjective_without_answers(self) -> None:
        plan = self._plan(
            questions=[
                FakeQuestion(instance_id="a", unipus_type="oral", child_count=1),
                FakeQuestion(instance_id="b", unipus_type="subjective", child_count=1),
            ],
            standard_answers=[FakeStandard(()), FakeStandard(((),))],
        )
        joined = "；".join(plan.warnings)
        self.assertIn("self_report_scores", joined, "必须暴露「默认不报分」")
        self.assertIn("主观题", joined)

    def test_explicit_arrays_validated(self) -> None:
        plan = self._plan(is_completed=[True] * 4, third_party_judges=[{} for _ in range(4)])
        self.assertEqual(len(plan.payload["isCompleted"]), 4)
        with self.assertRaises(SubmitPayloadError):
            self._plan(is_completed=[True])
        with self.assertRaises(SubmitPayloadError) as cm:
            self._plan(third_party_judges=[{}])
        self.assertIn("严格相等", str(cm.exception))

    def test_summary_readable(self) -> None:
        plan = self._plan()
        summary = plan.summary()
        self.assertIn(GROUP_ID, summary)
        self.assertIn("submitType=1", summary)
        self.assertIn("可提交", summary)


# --------------------------------------------------------------------------
# 响应解析
# --------------------------------------------------------------------------


class TestParseResult(unittest.TestCase):
    def test_accepted_with_scores(self) -> None:
        result = parse_submit_result(
            {
                "code": 0,
                "data": {"state": {"state": 1, "score": [1, 1, 0], "score_pct": 0.667},
                         "version": "v3"},
            }
        )
        self.assertTrue(result.accepted)
        self.assertEqual(result.state, 1)
        self.assertEqual(result.score, [1, 1, 0])
        self.assertAlmostEqual(result.score_pct, 0.667)
        self.assertEqual(result.version, "v3")
        self.assertEqual(result.code_type, "int")
        self.assertEqual(result.per_child_verdicts(), [True, True, False])

    def test_string_zero_accepted_with_type_recorded(self) -> None:
        """facts §9.1-10：先整数比较，字符串再退回；实际类型要记下来。"""
        result = parse_submit_result({"code": "0", "data": {}})
        self.assertTrue(result.accepted)
        self.assertEqual(result.code_type, "str")
        self.assertEqual(result.code, "0")

    def test_rejected_codes(self) -> None:
        for code in (1, "1", None, False, True):
            result = parse_submit_result({"code": code, "data": {}})
            self.assertFalse(result.accepted, code)
            self.assertIn("未接受", result.summary())

    def test_empty_score_for_non_graded(self) -> None:
        result = parse_submit_result({"code": 0, "data": {"state": {"state": 1, "score": []}}})
        self.assertEqual(result.per_child_verdicts(), [])

    def test_malformed_numbers_tolerated(self) -> None:
        result = parse_submit_result(
            {"code": 0, "data": {"state": {"state": "x", "score": [1, "y"], "score_pct": "z"}}}
        )
        self.assertIsNone(result.state)
        self.assertIsNone(result.score_pct)
        self.assertEqual(result.per_child_verdicts(), [True, None])


# --------------------------------------------------------------------------
# 适配器：干跑（绝不发写请求）
# --------------------------------------------------------------------------


class TestAdapterDryRun(unittest.IsolatedAsyncioTestCase):
    def _adapter(self) -> tuple[UnipusAdapter, FakeTransport]:
        adapter, fake = make_adapter(
            content_response([content_item(child_count=4)]),
            answer_response([answer_item(groups=[["A"], ["B"], ["A"], ["B"]])]),
        )
        return adapter, fake

    async def test_default_is_dry_run_and_sends_no_post(self) -> None:
        adapter, fake = self._adapter()
        result = await adapter.submit(Assignment(remote_id=REMOTE_ID, title="1.1"), asked_answers(2))

        self.assertFalse(result.ok)
        self.assertIn("干跑", result.message)
        self.assertTrue(result.raw["dry_run"])
        self.assertEqual(fake.methods, ["GET", "GET"], "干跑只能有只读 GET，绝不能发 POST")
        self.assertEqual(fake.posts(), [])

    async def test_answers_argument_only_used_for_comparison(self) -> None:
        adapter, _fake = self._adapter()
        result = await adapter.submit(Assignment(remote_id=REMOTE_ID, title="1.1"), asked_answers(2))

        self.assertFalse(result.raw["answers_argument_used"])
        self.assertEqual(result.raw["answers_argument_count"], 2)
        rows = result.raw["questions"]
        self.assertEqual(rows[0]["answer"], "A,B,A,B", "干跑摘要必须是服务端标准答案")
        self.assertNotIn("不该被发出去", json.dumps(result.raw, ensure_ascii=False))

    async def test_plan_summary_has_shape_fields(self) -> None:
        adapter, _fake = self._adapter()
        result = await adapter.submit(Assignment(remote_id=REMOTE_ID, title="1.1"), [])
        self.assertEqual(result.raw["submit_type"], 1)
        self.assertEqual(result.raw["item_count"], 4)
        self.assertEqual(result.raw["question_count"], 1)
        self.assertEqual(result.raw["is_completed_len"], 4)
        self.assertEqual(result.raw["third_party_judges_len"], 4)
        self.assertTrue(result.raw["submittable"])

    async def test_no_http_returns_explanatory_dry_run(self) -> None:
        adapter = UnipusAdapter()
        result = await adapter.submit(Assignment(remote_id=REMOTE_ID, title="1.1"), [])
        self.assertFalse(result.ok)
        self.assertIn("没有注入 HTTP 客户端", result.message)
        self.assertEqual(result.raw["reason"], "no-http")


# --------------------------------------------------------------------------
# 适配器：真发
# --------------------------------------------------------------------------


class TestAdapterLive(unittest.IsolatedAsyncioTestCase):
    async def test_live_post_payload_shape_and_readback(self) -> None:
        adapter, fake = make_adapter(
            content_response([content_item(child_count=4)]),
            answer_response([answer_item(groups=[["A"], ["B"], ["A"], ["B"]])]),
            submit_response(score=(1, 1, 1, 1), score_pct=1.0),
            progress_response(),
            submit_live=True,
        )
        result = await adapter.submit(Assignment(remote_id=REMOTE_ID, title="1.1"), asked_answers(2))

        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.score, 1.0)
        self.assertIn("已接受", result.message)
        self.assertIn("已记录首做", result.message)
        self.assertFalse(result.raw["dry_run"])
        self.assertEqual(result.raw["submit_type"], 1)
        self.assertTrue(result.raw["readback"]["finished"])
        self.assertEqual(result.raw["code_type"], "int")

        posts = fake.posts()
        self.assertEqual(len(posts), 1, "真发只能有 1 次 POST（不重试）")
        self.assertTrue(posts[0]["url"].endswith(endpoints.UCONTENT_SUBMIT))
        self.assertTrue(posts[0]["url"].startswith("https://ucontent.unipus.cn"))
        body = json.loads(posts[0]["body"])
        self.assertEqual(body["courseId"], f"course-v2:{INSTANCE_ID}")
        self.assertEqual(body["groupId"], GROUP_ID)
        self.assertEqual(body["openId"], "900001")
        self.assertEqual(body["version"], "default")
        self.assertEqual(body["isCompleted"], [True] * 4)
        judges = json.loads(body["thirdPartyJudges"])
        self.assertEqual(len(judges), 4)
        self.assertTrue(all(isinstance(element, dict) for element in judges))
        self.assertEqual(body["quesDatas"][0]["instanceId"], BIG_ID)
        answer = json.loads(body["quesDatas"][0]["answer"])
        self.assertEqual([child["value"] for child in answer["children"]],
                         [["A"], ["B"], ["A"], ["B"]])
        self.assertEqual(answer["progress"], {"1": True},
                         "progress 的键是 contents 下标（type=2 的项在下标 1）")

    async def test_live_refuses_when_child_counts_mismatch(self) -> None:
        adapter, fake = make_adapter(
            content_response([content_item(child_count=4)]),
            answer_response([answer_item(groups=[["A"], ["B"]])]),
            submit_live=True,
        )
        result = await adapter.submit(Assignment(remote_id=REMOTE_ID, title="1.1"), [])

        self.assertFalse(result.ok)
        self.assertIn("本地校验未通过", result.message)
        self.assertIn("子题数不一致", result.message)
        self.assertEqual(fake.posts(), [], "校验不通过时绝不能发 POST")
        self.assertEqual(fake.methods, ["GET", "GET"])

    async def test_live_rejected_code_raises_api_error(self) -> None:
        """ucontent 的提交判定是**严格整数 code==0**（facts §7）：

        非 0 的响应在请求层就变成 ``UnipusApiError``（异常不静默），
        不会走到 ``parse_submit_result`` 的「未接受」分支，也不会回读。
        """
        from kedaibiao.platforms.unipus.errors import UnipusApiError

        adapter, fake = make_adapter(
            content_response([content_item(child_count=1)]),
            answer_response([answer_item(groups=[["A"]])]),
            submit_response(code=1),
            submit_live=True,
        )
        with self.assertRaises(UnipusApiError):
            await adapter.submit(Assignment(remote_id=REMOTE_ID, title="1.1"), [])
        self.assertEqual(len(fake.posts()), 1)
        self.assertEqual(fake.methods, ["GET", "GET", "POST"], "拒绝时不回读")

    async def test_rate_limit_propagates_without_retry(self) -> None:
        adapter, fake = make_adapter(
            content_response([content_item(child_count=1)]),
            answer_response([answer_item(groups=[["A"]])]),
            submit_response(code=600001),
            submit_live=True,
        )
        with self.assertRaises(UnipusRateLimited):
            await adapter.submit(Assignment(remote_id=REMOTE_ID, title="1.1"), [])
        self.assertEqual(len(fake.posts()), 1, "限流不 sleep、不重试，直接交给调度层")
        self.assertEqual(fake.methods, ["GET", "GET", "POST"])

    async def test_readback_failure_soft_lands(self) -> None:
        adapter, fake = make_adapter(
            content_response([content_item(child_count=1)]),
            answer_response([answer_item(groups=[["A"]])]),
            submit_response(),
            Response(status=500, url="https://x/", body=b"boom"),
            submit_live=True,
        )
        result = await adapter.submit(Assignment(remote_id=REMOTE_ID, title="1.1"), [])
        self.assertTrue(result.ok, "回读失败不影响「提交请求已被接受」")
        self.assertIsNone(result.raw["readback"])
        self.assertTrue(result.raw["readback_error"])
        self.assertIn("回读", result.message)
        self.assertEqual(len(fake.posts()), 1)

    async def test_readback_rate_limit_is_not_swallowed(self) -> None:
        adapter, _fake = make_adapter(
            content_response([content_item(child_count=1)]),
            answer_response([answer_item(groups=[["A"]])]),
            submit_response(),
            submit_response(code=600002),
            submit_live=True,
        )
        with self.assertRaises(UnipusRateLimited):
            await adapter.submit(Assignment(remote_id=REMOTE_ID, title="1.1"), [])

    async def test_empty_content_refused_before_post(self) -> None:
        adapter, fake = make_adapter(
            content_response([]),
            answer_response([]),
            submit_live=True,
        )
        with self.assertRaises(SubmitPayloadError):
            await adapter.submit(Assignment(remote_id=REMOTE_ID, title="1.1"), [])
        self.assertEqual(fake.posts(), [])


# --------------------------------------------------------------------------
# 控制台原生链路：native_plan / native_submit
# --------------------------------------------------------------------------


class TestNativeHooks(unittest.IsolatedAsyncioTestCase):
    def test_capabilities_and_flow_marker(self) -> None:
        self.assertTrue(UnipusAdapter.capabilities.submit, "提交链路已实现，能力要打开")
        self.assertTrue(getattr(UnipusAdapter, "native_submit_flow", False))
        self.assertFalse(UnipusAdapter.submit_live, "类默认必须干跑")

    async def test_native_plan_is_dry_run(self) -> None:
        adapter, fake = make_adapter(
            content_response([content_item(child_count=4)]),
            answer_response([answer_item(groups=[["A"], ["B"], ["A"], ["B"]])]),
        )
        plan = await adapter.native_plan(REMOTE_ID)

        self.assertTrue(plan["dry_run"])
        self.assertTrue(plan["can_submit"])
        self.assertEqual(plan["platform"], "unipus")
        self.assertEqual(plan["assignment_id"], REMOTE_ID)
        self.assertEqual(plan["question_count"], 1)
        self.assertEqual(plan["item_count"], 4)
        self.assertEqual(plan["answered"], 1)
        self.assertIn("没有向平台提交任何内容", plan["note"])
        self.assertEqual(fake.posts(), [])
        self.assertEqual(fake.methods, ["GET", "GET"])

    async def test_native_plan_accepts_discussion_with_empty_answer(self) -> None:
        """真实样本形状：内容侧 1 项，答案侧 ``{"answer": ""}`` —— 不该被 block。"""
        adapter, fake = make_adapter(
            content_response(
                [content_item(reply_type="discussion", qtype="discussion",
                              category=0, child_count=1)]
            ),
            answer_response([answer_item(empty_string=True)]),
        )
        plan = await adapter.native_plan(REMOTE_ID)
        self.assertTrue(plan["can_submit"], plan["blockers"])
        self.assertEqual(plan["blockers"], [])
        self.assertEqual(plan["submit_type"], 2)
        self.assertEqual(plan["item_count"], 0)
        self.assertEqual(fake.posts(), [])

    async def test_native_plan_reports_blockers(self) -> None:
        adapter, fake = make_adapter(
            content_response([content_item(child_count=4)]),
            answer_response([answer_item(groups=[["A"], ["B"]])]),
        )
        plan = await adapter.native_plan(REMOTE_ID)
        self.assertFalse(plan["can_submit"])
        self.assertTrue(plan["blockers"])
        self.assertIn("子题数不一致", plan["blockers"][0])
        self.assertEqual(fake.posts(), [])

    async def test_native_submit_posts_and_reads_back(self) -> None:
        adapter, fake = make_adapter(
            content_response([content_item(child_count=2)]),
            answer_response([answer_item(groups=[["A"], ["B"]])]),
            submit_response(score=(1, 1), score_pct=1.0),
            progress_response(),
        )
        result = await adapter.native_submit(REMOTE_ID)

        self.assertFalse(result["dry_run"])
        self.assertTrue(result["submitted"])
        self.assertTrue(result["server_finish"])
        self.assertEqual(result["submit_type"], 1)
        self.assertEqual(result["question_count"], 1)
        self.assertEqual(len(fake.posts()), 1)
        self.assertTrue(fake.posts()[0]["url"].endswith(endpoints.UCONTENT_SUBMIT))

    async def test_native_submit_refuses_blocked_plan(self) -> None:
        adapter, fake = make_adapter(
            content_response([content_item(child_count=4)]),
            answer_response([answer_item(groups=[["A"]])]),
        )
        with self.assertRaises(NotSupportedError) as cm:
            await adapter.native_submit(REMOTE_ID)
        self.assertIn("拒绝提交", str(cm.exception))
        self.assertEqual(fake.posts(), [])

    async def test_native_submit_empty_content_fails_clearly(self) -> None:
        adapter, fake = make_adapter(content_response([]), answer_response([]))
        with self.assertRaises(SubmitPayloadError):
            await adapter.native_submit(REMOTE_ID)
        self.assertEqual(fake.posts(), [])


if __name__ == "__main__":
    unittest.main()
