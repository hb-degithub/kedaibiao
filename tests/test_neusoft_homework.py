"""东软作业模块离线单测。

全程离线：``FakeTransport`` 按路径后缀路由，断言的是**请求长什么样**
（body / params / 到底有没有发），而不是平台真会怎么回。

重点盯三处：

1. 两个致命坑 —— 学习任务必须带 ``termId``、不能带 ``currentClassId``；
   列表必须按 ``type`` 过滤；
2. ``ctlId`` 就是作答用的 ``testId``，只留一个真名；
3. 交卷不可逆 —— 没有 ``confirm=True`` 时**一个请求都不发**。
"""

from __future__ import annotations

from datetime import datetime

import logging
import unittest
from typing import Any

from kedaibiao.core.models import QuestionType
from kedaibiao.platforms.neusoft import endpoints as ep
from kedaibiao.platforms.neusoft.errors import NeusoftError, NeusoftProtocolError
from kedaibiao.platforms.neusoft.homework import (
    Entered,
    core_question_type,
    enter_task,
    get_paper,
    list_study_tasks,
    parse_paper,
    parse_study_tasks,
    save_answers,
    submit_paper,
    to_core_questions,
)
from tests.neusoft_support import FakeTransport, make_client


def setUpModule() -> None:
    """测试里不打印预期中的「跳过」警告，保持断言输出干净。"""
    logging.getLogger("kedaibiao.platforms.neusoft.homework").setLevel(logging.ERROR)


#: 一份学习任务分页：两行作业（type 3，一行数字一行字符串）+ 一行视频（type 1）
#: + 一行单元测试（type 2，走同一套取卷链路）+ 一行缺 ``ctlId`` 的作业。
TASK_PAGE: dict[str, Any] = {
    "total": 5,
    "list": [
        {
            "resId": "res-1",
            "ctlId": "ctl-301",
            "type": 3,
            "resName": "第三章作业",
            "teachCourseName": "高等数学",
            "studyStatus": 0,
            "openTime": "2026-09-01 08:00:00",
        },
        {
            "resId": "res-9",
            "ctlId": "ctl-401",
            "type": 2,
            "resName": "单元测试一",
            "teachCourseName": "高等数学",
            "studyStatus": 0,
            "openTime": "2026-09-10 08:00:00",
        },
        {
            "resId": "res-2",
            "ctlId": "ctl-video",
            "type": 1,
            "resName": "第三章视频",
            "teachCourseName": "高等数学",
            "studyStatus": 0,
        },
        {
            "resId": "res-3",
            "ctlId": "ctl-302",
            "type": "3",
            "resName": "随堂小测验",
            "teachCourseName": "线性代数",
            "studyStatus": "2",
        },
        {
            "resId": "res-4",
            "type": 3,
            "resName": "缺 ctlId 的作业",
            "teachCourseName": "高等数学",
            "studyStatus": 0,
        },
    ],
}


def _paper_payload() -> dict[str, Any]:
    """一份试卷：两个 part、四道题（其中一条 ``paperQuestion`` 缺失用于跳过）。"""
    return {
        "testId": "ctl-301",
        "paperParts": [
            {
                "partId": "part-1",
                "paperQuestionList": [
                    {
                        "paperQuestion": {
                            "paperId": "pq-1",
                            "questionNo": "1",
                            "questionType": "4",
                            "stemText": "请填空：水的化学式是____。",
                            "stem": "<p>这份 HTML 题干不应被使用</p>",
                            "choices": [],
                            "candidateAnswer": ["H2O", "水"],
                            "standardAnswer": "H2O,水",
                        }
                    },
                    {
                        "paperQuestion": {
                            "paperId": "pq-2",
                            "questionNo": 2,
                            "questionType": 4,
                            "stem": "<p>速度&amp;加速度</p>",
                            "choices": [
                                {"choiceId": 1, "choiceContent": "选项甲"},
                                {"choiceId": "2", "choiceContent": "选项乙"},
                            ],
                            "candidateAnswer": None,
                            "standardAnswer": None,
                        }
                    },
                    {
                        "paperQuestion": {
                            "id": "pq-3",
                            "questionNo": 3,
                            "questionType": "8",
                            "stemText": "",
                            "stem": "",
                            "candidateAnswer": "",
                            "standardAnswer": [],
                        }
                    },
                    {"paperQuestion": None},
                ],
            },
            {
                "partId": "part-2",
                "paperQuestionList": [
                    {
                        "paperQuestion": {
                            "paperId": "pq-4",
                            "questionNo": "1",
                            "questionType": "10",
                            "stemText": "组合题父题",
                        }
                    }
                ],
            },
        ],
    }


def _entered() -> Entered:
    return Entered(test_id="ctl-301", candidate_id="cand-1", history_id="hist-1")


class StudyTaskParseTest(unittest.TestCase):
    """列表解析：过滤 + ctlId ↔ testId。"""

    def test_filters_out_video_rows_but_keeps_tests(self) -> None:
        tasks = parse_study_tasks(TASK_PAGE)
        # type=1 的视频被丢弃、缺 ctlId 的行跳过；type=2 的单元测试**必须保留**
        # （曾经只认 type=3，把「单元测试一/二/三」整批过滤掉了）
        self.assertEqual([task.test_id for task in tasks], ["ctl-301", "ctl-401", "ctl-302"])

    def test_task_type_is_kept(self) -> None:
        tasks = {task.test_id: task for task in parse_study_tasks(TASK_PAGE)}
        self.assertEqual(tasks["ctl-301"].task_type, ep.TASK_KIND_HOMEWORK)
        self.assertEqual(tasks["ctl-401"].task_type, ep.TASK_KIND_TEST)

    def test_ctl_id_is_the_test_id(self) -> None:
        task = parse_study_tasks(TASK_PAGE)[0]
        self.assertEqual(task.test_id, "ctl-301")
        self.assertEqual(task.ctl_id, task.test_id)
        self.assertEqual(task.name, "第三章作业")
        self.assertEqual(task.course_name, "高等数学")

    def test_string_status_and_type_are_accepted(self) -> None:
        by_id = {task.test_id: task for task in parse_study_tasks(TASK_PAGE)}
        # type 是字符串 "3" 也能认出是作业；studyStatus 是字符串 "2" 也能认出已完成
        self.assertEqual(by_id["ctl-302"].task_type, ep.TASK_KIND_HOMEWORK)
        self.assertEqual(by_id["ctl-302"].study_status, ep.STUDY_STATUS_DONE)
        self.assertTrue(by_id["ctl-302"].is_completed)

    def test_tolerates_non_list_payload(self) -> None:
        self.assertEqual(parse_study_tasks(None), [])
        self.assertEqual(parse_study_tasks({"list": None}), [])


class StudyTaskRequestTest(unittest.IsolatedAsyncioTestCase):
    """列表请求：必须带 termId，绝不能带 currentClassId。"""

    async def test_body_has_term_id_and_no_current_class_id(self) -> None:
        transport = FakeTransport()
        transport.json("POST", "/study-task/page", TASK_PAGE)
        client = make_client(transport)

        tasks = await list_study_tasks(client, "term-2026-1")

        self.assertEqual(
            [task.test_id for task in tasks], ["ctl-301", "ctl-401", "ctl-302"]
        )
        self.assertEqual(transport.paths(), [ep.STUDY_TASK_PAGE])
        body = transport.body_of("/study-task/page")
        self.assertEqual(body["termId"], "term-2026-1")
        self.assertEqual(body["pageNo"], 1)
        self.assertEqual(body["pageSize"], 200)
        # 致命坑 1：带上课程上下文会 403，所以 body 里必须没有这个键
        self.assertNotIn("currentClassId", body)

    async def test_missing_term_id_refuses_without_request(self) -> None:
        transport = FakeTransport()
        client = make_client(transport)
        with self.assertRaises(NeusoftError):
            await list_study_tasks(client, "")
        self.assertEqual(transport.requests, [])


class StudyTaskDeadlineTest(unittest.TestCase):
    """学习任务的截止时间来自 ``closeTime``（毫秒时间戳）。

    之前完全没解析 deadline，控制台「截止」列永远是「—」，用户没法判断
    哪些作业快到期了。
    """

    def test_close_time_becomes_deadline(self) -> None:
        tasks = parse_study_tasks({"list": [{
            "ctlId": "ctl-1", "resName": "作业1", "type": 3,
            "closeTime": 1789919999000, "openTime": 1788331198000,
        }]})
        self.assertEqual(len(tasks), 1)
        self.assertIsNotNone(tasks[0].deadline)
        assert tasks[0].deadline is not None
        self.assertEqual(tasks[0].deadline.strftime("%Y-%m-%d %H:%M"), "2026-09-20 23:59")
        self.assertIsNotNone(tasks[0].available_from)

    def test_missing_or_zero_close_time_is_none(self) -> None:
        for row in (
            {"ctlId": "a", "type": 3},
            {"ctlId": "b", "type": 3, "closeTime": 0},
            {"ctlId": "c", "type": 3, "closeTime": None},
            {"ctlId": "d", "type": 3, "closeTime": "abc"},
            {"ctlId": "e", "type": 3, "closeTime": True},
        ):
            with self.subTest(row=row):
                tasks = parse_study_tasks({"list": [row]})
                self.assertIsNone(tasks[0].deadline, "缺失/0/非法一律当没有，不猜时间")

    def test_assignment_carries_deadline(self) -> None:
        from kedaibiao.platforms.neusoft.adapter import _to_core_assignment
        from kedaibiao.platforms.neusoft.homework import StudyTask

        task = StudyTask(
            test_id="ctl-1", name="作业1", task_type=3, deadline=datetime(2026, 9, 20, 23, 59)
        )
        assignment = _to_core_assignment(task)
        self.assertEqual(assignment.deadline, datetime(2026, 9, 20, 23, 59))
        self.assertEqual(assignment.title, "作业1")


class PaperParseTest(unittest.TestCase):
    """试卷解析：层级拍平、三态答案、题干退化、选项。"""

    def test_flattens_parts_and_skips_broken_rows(self) -> None:
        paper = parse_paper(_paper_payload())
        self.assertEqual(paper.test_id, "ctl-301")
        # part-1 三道有效题 + part-2 一道；paperQuestion 为 None 的被跳过
        self.assertEqual(paper.question_count, 4)
        self.assertEqual([q.question_no for q in paper.questions], ["1", "2", "3", "1"])

    def test_three_state_answers_are_normalized(self) -> None:
        paper = parse_paper(_paper_payload())
        first = paper.questions[0]
        self.assertEqual(first.candidate_answer, ("H2O", "水"))  # 数组
        self.assertEqual(first.standard_answer, ("H2O", "水"))  # 逗号串
        second = paper.questions[1]
        self.assertEqual(second.candidate_answer, ())  # null
        self.assertEqual(second.standard_answer, ())
        third = paper.questions[2]
        self.assertEqual(third.candidate_answer, ())  # 空串
        self.assertEqual(third.standard_answer, ())  # 空数组

    def test_question_no_and_type_accept_numbers(self) -> None:
        paper = parse_paper(_paper_payload())
        second = paper.questions[1]
        self.assertEqual(second.question_no, "2")  # 数字 → 字符串
        self.assertEqual(second.question_type, "4")
        self.assertEqual(paper.questions[2].question_no, "3")
        self.assertEqual(paper.questions[2].question_type, "8")

    def test_stem_prefers_stem_text_and_strips_html(self) -> None:
        paper = parse_paper(_paper_payload())
        # stemText 优先：stem 里那份 HTML 题干不应被使用
        self.assertEqual(paper.questions[0].stem, "请填空：水的化学式是____。")
        # stemText 缺失时退化为 stem，但要去标签、解实体
        self.assertEqual(paper.questions[1].stem, "速度&加速度")

    def test_reads_choices(self) -> None:
        choices = parse_paper(_paper_payload()).questions[1].choices
        self.assertEqual([choice.content for choice in choices], ["选项甲", "选项乙"])
        self.assertEqual([choice.choice_id for choice in choices], ["1", "2"])

    def test_paper_id_falls_back_to_id_and_raw_is_kept(self) -> None:
        paper = parse_paper(_paper_payload())
        third = paper.questions[2]
        self.assertEqual(third.paper_id, "pq-3")
        self.assertEqual(paper.questions[0].raw["questionNo"], "1")
        self.assertEqual(paper.questions[0].raw["candidateAnswer"], ["H2O", "水"])

    def test_rejects_non_object_payload(self) -> None:
        with self.assertRaises(NeusoftProtocolError):
            parse_paper([{"paperParts": []}])


class PaperRequestTest(unittest.IsolatedAsyncioTestCase):
    """取卷请求：GET + ``testId`` query。"""

    async def test_get_paper_sends_test_id_query(self) -> None:
        transport = FakeTransport()
        transport.json("GET", "/student/paper", _paper_payload())
        client = make_client(transport)

        paper = await get_paper(client, "ctl-301")

        self.assertEqual(paper.test_id, "ctl-301")
        self.assertEqual(transport.paths(), [ep.EXAM_PAPER])
        self.assertEqual(transport.requests[-1].method, "GET")
        self.assertEqual(transport.requests[-1].query, {"testId": "ctl-301"})
        self.assertIsNone(transport.body_of("/student/paper"))


class EnterTaskTest(unittest.IsolatedAsyncioTestCase):
    """进入作答：取 candidateId / historyId。"""

    async def test_enter_reads_candidate_and_history(self) -> None:
        transport = FakeTransport()
        transport.json(
            "GET",
            "/student/enter",
            {"testId": "ctl-301", "candidateId": "cand-1", "historyId": "hist-1"},
        )
        client = make_client(transport)

        entered = await enter_task(client, "ctl-301")

        self.assertEqual(entered.test_id, "ctl-301")
        self.assertEqual(entered.candidate_id, "cand-1")
        self.assertEqual(entered.history_id, "hist-1")
        self.assertTrue(entered.is_ready)
        self.assertEqual(transport.paths(), [ep.EXAM_ENTER])
        self.assertEqual(transport.requests[-1].query, {"testId": "ctl-301"})


class AnswerSaveTest(unittest.IsolatedAsyncioTestCase):
    """保存草稿：body 结构 + 四字段齐全。"""

    async def test_answers_carry_all_four_fields(self) -> None:
        transport = FakeTransport()
        transport.json("POST", "/answer/save", {"isSubmitPaper": None})
        client = make_client(transport)
        answers = [
            {"paperId": "pq-1", "questionNo": "1", "questionType": "4", "answer": ["H2O", "水"]},
            {"paperId": "pq-2", "questionNo": 2, "questionType": 4, "answer": "A"},
        ]

        result = await save_answers(client, _entered(), answers)

        self.assertEqual(result, {"isSubmitPaper": None})
        self.assertEqual(transport.paths(), [ep.EXAM_ANSWER_SAVE])
        body = transport.body_of("/answer/save")
        self.assertEqual(body["candidateId"], "cand-1")
        self.assertEqual(body["testId"], "ctl-301")
        self.assertEqual(body["historyId"], "hist-1")
        rows = body["answers"]
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(
                sorted(row), ["answer", "paperId", "questionNo", "questionType"]
            )
        self.assertEqual(rows[0]["answer"], "H2O,水")  # 数组 → 逗号串
        self.assertEqual(rows[1]["questionNo"], "2")  # 数字 → 字符串
        self.assertEqual(rows[1]["answer"], "A")

    async def test_missing_field_refuses_without_request(self) -> None:
        transport = FakeTransport()
        client = make_client(transport)
        answers = [{"paperId": "pq-1", "questionNo": "1", "answer": "A"}]
        with self.assertRaises(NeusoftProtocolError):
            await save_answers(client, _entered(), answers)
        self.assertEqual(transport.requests, [])

    async def test_requires_entered_ids(self) -> None:
        transport = FakeTransport()
        client = make_client(transport)
        with self.assertRaises(NeusoftError):
            await save_answers(client, Entered(test_id="ctl-301"), [])
        self.assertEqual(transport.requests, [])


class SubmitPaperTest(unittest.IsolatedAsyncioTestCase):
    """交卷：默认拒绝、不可逆、只有 confirm=True 才落请求。"""

    async def test_without_confirm_refuses_and_sends_nothing(self) -> None:
        transport = FakeTransport()
        client = make_client(transport)
        with self.assertRaises(NeusoftError) as ctx:
            await submit_paper(client, "hist-1")
        self.assertIn("不可逆", str(ctx.exception))
        self.assertEqual(transport.requests, [])

    async def test_confirm_sends_put_with_history_id(self) -> None:
        transport = FakeTransport()
        transport.json("PUT", "/student/submit", None)
        client = make_client(transport)

        ok = await submit_paper(client, "hist-1", confirm=True)

        self.assertTrue(ok)
        self.assertEqual(transport.paths(), [ep.EXAM_SUBMIT])
        self.assertEqual(transport.requests[-1].method, "PUT")
        self.assertEqual(transport.requests[-1].query, {"historyId": "hist-1"})
        # 交卷只有 query，没有 body
        self.assertIsNone(transport.body_of("/student/submit"))

    async def test_confirm_without_history_id_sends_nothing(self) -> None:
        transport = FakeTransport()
        client = make_client(transport)
        with self.assertRaises(NeusoftError):
            await submit_paper(client, "", confirm=True)
        self.assertEqual(transport.requests, [])


class CoreQuestionTest(unittest.TestCase):
    """题型映射与 core 转换。"""

    def test_only_documented_question_types_are_mapped(self) -> None:
        self.assertIs(core_question_type("4"), QuestionType.FILL)
        self.assertIs(core_question_type(4), QuestionType.FILL)
        # 2026-10-22 真实试卷实测：1 = 四选一单选，3 = 正/误判断题
        self.assertIs(core_question_type("1"), QuestionType.SINGLE)
        self.assertIs(core_question_type("3"), QuestionType.JUDGE)
        # 组合题（8/10）在 core 里没有对应枚举；其余未登记取值同样归 UNKNOWN
        for raw in ("8", "10", "", None):
            with self.subTest(question_type=raw):
                self.assertIs(core_question_type(raw), QuestionType.UNKNOWN)

    def test_to_core_questions_keeps_options_raw_and_identity(self) -> None:
        questions = to_core_questions(parse_paper(_paper_payload()))
        self.assertEqual(len(questions), 4)
        first = questions[0]
        self.assertIs(first.type, QuestionType.FILL)
        self.assertEqual(first.stem, "请填空：水的化学式是____。")
        self.assertEqual(first.options, [])
        self.assertEqual(first.remote_id, "pq-1")
        self.assertEqual(first.raw["candidateAnswer"], ["H2O", "水"])
        self.assertTrue(first.is_identifiable)
        self.assertEqual(questions[1].options, ["选项甲", "选项乙"])
        self.assertIs(questions[2].type, QuestionType.UNKNOWN)
        self.assertIs(questions[3].type, QuestionType.UNKNOWN)


def _real_shape_payload() -> dict[str, Any]:
    """**真实响应形状**（2026-10-22 实测，见 ``data/recon/neusoft-paper-*.json``）。

    和旧假设的差别全在这里，也是「交卷时一条答案都映射不上」的根因：

    - ``paperId`` / ``id`` 在**外层**（``paperQuestionList[]``），内层没有；
    - ``paperId`` 整份试卷共用一个值（等于顶层 ``id``），逐题唯一的是外层 ``id``；
    - ``choices[].choiceId`` 是字母且**顺序打乱**（不是 A、B、C、D）；
    - 选项文本可能被二次转义（``"\\t实用性"``）。
    """
    return {
        "id": "paper-1",
        "name": "期末测试",
        "paperParts": [
            {
                "partId": "part-1",
                "paperQuestionList": [
                    {
                        "id": "pqrow-1",
                        "paperId": "paper-1",
                        "partId": "part-1",
                        "questionType": "1",
                        "paperQuestion": {
                            "questionNo": 17,
                            "questionType": "1",
                            "stemText": "下列描述错误的一项是？",
                            "choices": [
                                {"choiceId": "B", "choiceContent": "开放性"},
                                {"choiceId": "D", "choiceContent": "从众性"},
                            ],
                        },
                    },
                    {
                        "id": "pqrow-2",
                        "paperId": "paper-1",
                        "partId": "part-1",
                        "questionType": "3",
                        "paperQuestion": {
                            "questionNo": 38,
                            "questionType": "3",
                            "stemText": "发散思维和聚合思维往往要结合使用。",
                            "choices": [
                                {"choiceId": "A", "choiceContent": "\\t正确"},
                                {"choiceId": "B", "choiceContent": "\\t错误"},
                            ],
                        },
                    },
                ],
            }
        ],
    }


class RealShapePaperTest(unittest.TestCase):
    """真实响应形状：paperId 在外层、逐题唯一的是外层 id、选项字母是打乱的。"""

    def test_paper_id_comes_from_outer_row(self) -> None:
        paper = parse_paper(_real_shape_payload())
        self.assertEqual(len(paper.questions), 2)
        for item in paper.questions:
            self.assertEqual(item.paper_id, "paper-1")
        # 迁移前这里恒为空 —— 于是 answer/save 的行全被丢掉
        self.assertEqual(
            [item.question_no for item in paper.questions], ["17", "38"]
        )

    def test_question_id_is_the_unique_key(self) -> None:
        paper = parse_paper(_real_shape_payload())
        self.assertEqual([item.question_id for item in paper.questions], ["pqrow-1", "pqrow-2"])
        keys = [item.key for item in paper.questions]
        self.assertEqual(keys, ["pqrow-1", "pqrow-2"])
        self.assertEqual(len(set(keys)), len(keys), "paperId 共用，绝不能用它当主键")

    def test_escaped_choice_text_is_decoded(self) -> None:
        paper = parse_paper(_real_shape_payload())
        judge = paper.questions[1]
        self.assertEqual([choice.content for choice in judge.choices], ["正确", "错误"])

    def test_to_core_questions_use_unique_remote_id(self) -> None:
        questions = to_core_questions(parse_paper(_real_shape_payload()))
        self.assertEqual([q.remote_id for q in questions], ["pqrow-1", "pqrow-2"])
        self.assertIs(questions[0].type, QuestionType.SINGLE)
        self.assertIs(questions[1].type, QuestionType.JUDGE)


if __name__ == "__main__":
    unittest.main()
