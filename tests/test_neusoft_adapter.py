"""适配器总装测试：登录、课程→作业、刷课、交卷闸门、签到编排。

这一层测的是**编排**而不是协议细节（协议由各模块自己的测试钉）。
重点在四道闸门：

1. 交卷默认不开闸 —— 只能存草稿，且要如实告诉调用方「没交」；
2. 刷课以回读为准 —— 接口说成功但回读没完成，必须算失败；
3. 签到遇到「不在该教学班」（明细 id 为空）要跳过而不是硬签；
4. 会话能导出/导入 —— 否则重启就要重新绑定账号（账号库不存密码）。
"""

from __future__ import annotations

import unittest

from kedaibiao.core.models import (
    Answer,
    AnswerSource,
    Assignment,
    Course,
    Credentials,
    Question,
    QuestionType,
    TaskKind,
)
from kedaibiao.platforms.base import AdapterContext
from kedaibiao.platforms.neusoft import endpoints as ep
from kedaibiao.platforms.neusoft.adapter import (
    SUBMIT_BLOCKED_MESSAGE,
    NeusoftAdapter,
    build_answer_rows,
)
from kedaibiao.platforms.neusoft.homework import Choice, Paper, PaperQuestion
from tests.neusoft_support import FakeTransport, make_client

TERM_ID = "900000000000000001"
COURSE_ID = "100000000000000001"
CLASS_ID = "100000000000000002"
TEST_ID = "300000000000000001"
RES_ID = "400000000000000001"


def make_adapter(
    transport: FakeTransport, *, environ: dict[str, str] | None = None
) -> NeusoftAdapter:
    ctx = AdapterContext(http=transport, environ=dict(environ or {}))
    adapter = NeusoftAdapter(ctx)
    # 直接注入已登录会话，省掉每个用例都走一遍登录
    adapter.client.load_session(make_client(transport).session)
    return adapter


def course() -> Course:
    return Course(
        remote_id=COURSE_ID,
        name="示例课程（一）",
        term="2026 秋",
        raw={"teach_course_id": COURSE_ID, "teach_class_id": CLASS_ID, "term_id": TERM_ID},
    )


class LoginTest(unittest.IsolatedAsyncioTestCase):
    async def test_login_success(self) -> None:
        transport = FakeTransport()
        transport.json("GET", "/tenant/app/option", [{"tenantId": "t1", "name": "东软（外网）"}])
        transport.json(
            "POST", "/auth/app/login",
            {"userId": "u1", "tenantId": "t1", "accessToken": "tok", "refreshToken": "r"},
        )
        adapter = NeusoftAdapter(AdapterContext(http=transport))
        self.assertTrue(await adapter.login(Credentials("alice", "secret")))
        self.assertEqual(adapter.client.session.access_token, "tok")

    async def test_business_login_error_returns_false(self) -> None:
        transport = FakeTransport()
        transport.json("GET", "/tenant/app/option", [])
        transport.api_error("POST", "/auth/app/login", 1002001, "用户名或密码错误")
        adapter = NeusoftAdapter(AdapterContext(http=transport))
        self.assertFalse(await adapter.login(Credentials("alice", "wrong")))


class SessionTest(unittest.IsolatedAsyncioTestCase):
    async def test_export_import_round_trip(self) -> None:
        transport = FakeTransport()
        adapter = make_adapter(transport)
        data = adapter.export_session()
        assert data is not None
        self.assertEqual(data["access_token"], "fake-token")

        other = NeusoftAdapter(AdapterContext(http=FakeTransport()))
        self.assertTrue(other.import_session(data))
        self.assertEqual(other.client.session.access_token, "fake-token")

    async def test_import_rejects_other_domain(self) -> None:
        """换学校（换域名）时旧 token 必须作废，否则请求全打到一个错的网关。"""
        transport = FakeTransport()
        adapter = make_adapter(transport)
        data = adapter.export_session() or {}
        data["base_url"] = "https://another-school.test"
        other = NeusoftAdapter(AdapterContext(http=FakeTransport()))
        self.assertFalse(other.import_session(data))

    async def test_import_rejects_empty_token(self) -> None:
        adapter = NeusoftAdapter(AdapterContext(http=FakeTransport()))
        self.assertFalse(adapter.import_session({"access_token": ""}))
        self.assertIsNone(adapter.export_session())


class CourseTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.transport = FakeTransport()
        self.transport.json("GET", "/getTeachTermDropDown", [{"id": TERM_ID, "name": "2026 秋"}])
        self.transport.json(
            "GET",
            "/teach-arrangement-stu/page",
            {
                "list": [
                    {
                        "teachCourseId": COURSE_ID,
                        "teachClassId": CLASS_ID,
                        "teachCourseName": "示例课程（一）",
                    }
                ]
            },
        )

    async def test_maps_course_and_keeps_keys_in_raw(self) -> None:
        adapter = make_adapter(self.transport)
        courses = await adapter.list_courses()
        self.assertEqual(len(courses), 1)
        item = courses[0]
        self.assertEqual(item.remote_id, COURSE_ID)
        self.assertEqual(item.name, "示例课程（一）")
        # 拉资源要 teach_class_id，拉作业要 term_id —— 都藏在 raw 里
        self.assertEqual(item.raw["teach_class_id"], CLASS_ID)
        self.assertEqual(item.raw["term_id"], TERM_ID)

    async def test_pinned_term_limits_probing(self) -> None:
        self.transport.json("GET", "/getTeachTermDropDown", [
            {"id": TERM_ID, "name": "2026 秋"},
            {"id": "900000000000000002", "name": "2026 春"},
        ])
        adapter = make_adapter(
            self.transport, environ={"KEDAIBIAO_NEUSOFT_TERM_ID": "900000000000000002"}
        )
        await adapter.list_courses()
        page_calls = [
            q for q in self.transport.requests if q.path.endswith("/teach-arrangement-stu/page")
        ]
        self.assertEqual(len(page_calls), 1)
        self.assertEqual(page_calls[0].query["termId"], "900000000000000002")


class AssignmentTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.transport = FakeTransport()
        self.transport.json("POST", "/study-task/page", {
            "list": [
                {"ctlId": TEST_ID, "resName": "作业一", "teachCourseName": "示例课程（一）",
                 "teachCourseId": COURSE_ID, "type": 3, "studyStatus": 0},
                {"ctlId": "300000000000000002", "resName": "视频资源",
                 "teachCourseName": "示例课程（一）", "teachCourseId": COURSE_ID,
                 "type": 1, "studyStatus": 0},
                {"ctlId": "300000000000000003", "resName": "别的课的作业",
                 "teachCourseName": "另一门课", "teachCourseId": "100000000000000009",
                 "type": 3, "studyStatus": 1},
                {"ctlId": "300000000000000004", "resName": "单元测试一",
                 "teachCourseName": "示例课程（一）", "teachCourseId": COURSE_ID,
                 "type": 2, "studyStatus": 0},
            ]
        })

    async def test_filters_by_course_and_kind(self) -> None:
        adapter = make_adapter(self.transport)
        items = await adapter.list_assignments(course())
        # 作业（type 3）与单元测试（type 2）都属于可作答任务；视频与别的课要滤掉
        self.assertEqual([item.remote_id for item in items], [TEST_ID, "300000000000000004"])
        self.assertEqual(items[0].kind, TaskKind.HOMEWORK)
        self.assertEqual(items[1].kind, TaskKind.EXAM)
        self.assertFalse(items[0].finished)

    async def test_term_tasks_are_cached(self) -> None:
        adapter = make_adapter(self.transport)
        await adapter.list_assignments(course())
        await adapter.list_assignments(course())
        calls = [q for q in self.transport.requests if q.path.endswith("/study-task/page")]
        self.assertEqual(len(calls), 1, "同一学期只该拉一次任务列表")


class QuestionTest(unittest.IsolatedAsyncioTestCase):
    async def test_paper_maps_to_core_questions(self) -> None:
        transport = FakeTransport()
        transport.json("GET", "/paper", {
            "paperParts": [{
                "partName": "单选题",
                "paperQuestionList": [
                    {"paperQuestion": {
                        "paperId": "p1", "questionNo": "1", "questionType": "4",
                        "stemText": "填空题干", "stem": "<p>填空题干</p>",
                        "choices": [{"choiceId": "c1", "choiceContent": "选项一"}],
                    }},
                    {"paperQuestion": {
                        "paperId": "p2", "questionNo": "2", "questionType": "99",
                        "stem": "<div>未知题型</div>", "choices": [],
                    }},
                ],
            }]
        })
        adapter = make_adapter(transport)
        questions = await adapter.fetch_questions(Assignment(remote_id=TEST_ID, title="作业一"))
        self.assertEqual(len(questions), 2)
        self.assertEqual(questions[0].type, QuestionType.FILL)
        self.assertEqual(questions[0].stem, "填空题干")
        self.assertEqual(questions[0].options, ["选项一"])
        # 枚举不全时归 UNKNOWN，不许凭直觉当单选题
        self.assertEqual(questions[1].type, QuestionType.UNKNOWN)


class AnswerRowTest(unittest.TestCase):
    def _paper(self) -> Paper:
        return Paper(test_id=TEST_ID, questions=[
            PaperQuestion(
                paper_id="p1", question_no="1", question_type="4",
                choices=(Choice("c1", "北京"), Choice("c2", "上海")),
            ),
            PaperQuestion(
                paper_id="p2", question_no="2", question_type="1",
                choices=(Choice("c3", "正确"), Choice("c4", "错误")),
            ),
        ])

    def test_choice_index_maps_to_choice_id(self) -> None:
        """平台答案是逗号拼接的 choiceId，不是字母、不是文案。"""
        answer = Answer(
            question=Question(stem="q", remote_id="p2"),
            choices=[1],
            source=AnswerSource.BANK_HIT,
        )
        rows = build_answer_rows(self._paper(), [answer])
        self.assertEqual(
            rows,
            [{"paperId": "p2", "questionNo": "2", "questionType": "1", "answer": "c4"}],
        )

    def test_multiple_choices_are_comma_joined(self) -> None:
        answer = Answer(question=Question(stem="q", remote_id="p1"), choices=[0, 1])
        rows = build_answer_rows(self._paper(), [answer])
        self.assertEqual(rows[0]["answer"], "c1,c2")

    def test_fill_uses_text(self) -> None:
        answer = Answer(question=Question(stem="q", remote_id="p1"), text="  答案  ")
        rows = build_answer_rows(self._paper(), [answer])
        self.assertEqual(rows[0]["answer"], "答案")

    def test_unknown_question_is_dropped(self) -> None:
        answer = Answer(question=Question(stem="q", remote_id="不存在的题"), text="x")
        self.assertEqual(build_answer_rows(self._paper(), [answer]), [])

    def test_empty_answer_is_dropped(self) -> None:
        answer = Answer(question=Question(stem="q", remote_id="p1"))
        self.assertEqual(build_answer_rows(self._paper(), [answer]), [])

    def test_real_shape_shared_paper_id_maps_every_question(self) -> None:
        """回归：``paperId`` 整份试卷共用一个值，靠它建索引会把所有题压成一条。

        真实响应里逐题唯一的是外层 ``id``；选项字母还是打乱的。
        """
        paper = Paper(test_id=TEST_ID, questions=[
            PaperQuestion(
                paper_id="paper-1", question_id="row-1", question_no="17",
                question_type="1",
                choices=(Choice("C", "实用性"), Choice("A", "颠覆性")),
            ),
            PaperQuestion(
                paper_id="paper-1", question_id="row-2", question_no="38",
                question_type="3",
                choices=(Choice("A", "正确"), Choice("B", "错误")),
            ),
        ])
        answers = [
            Answer(question=Question(stem="q1", remote_id="row-1"), choices=[1]),
            Answer(question=Question(stem="q2", remote_id="row-2"), choices=[1]),
        ]
        rows = build_answer_rows(paper, answers)
        self.assertEqual(len(rows), 2, "两道题都要有行（共用 paperId 不能互相覆盖）")
        self.assertEqual(rows[0]["paperId"], "paper-1")
        self.assertEqual(rows[0]["questionNo"], "17")
        self.assertEqual(rows[0]["answer"], "A", "下标 1 → 该位置的 choiceId（打乱的字母）")
        self.assertEqual(rows[1]["questionNo"], "38")
        self.assertEqual(rows[1]["answer"], "B")


class SubmitGateTest(unittest.IsolatedAsyncioTestCase):
    def _transport(self) -> FakeTransport:
        transport = FakeTransport()
        transport.json("POST", "/answer/save", {"isSubmitPaper": None})
        transport.json("PUT", "/student/submit", {"ok": True})
        transport.json(
            "GET",
            "/paper",
            {
                "paperParts": [
                    {
                        "paperQuestionList": [
                            {
                                "paperQuestion": {
                                    "paperId": "p1",
                                    "questionNo": "1",
                                    "questionType": "1",
                                    "stemText": "题",
                                    "choices": [{"choiceId": "c1", "choiceContent": "A"}],
                                }
                            }
                        ]
                    }
                ]
            },
        )
        transport.json(
            "GET", "/enter", {"candidateId": "cand1", "historyId": "hist1", "testId": TEST_ID}
        )
        return transport

    async def test_default_saves_draft_and_never_submits(self) -> None:
        transport = self._transport()
        adapter = make_adapter(transport)
        result = await adapter.submit(
            Assignment(remote_id=TEST_ID, title="作业一"),
            [Answer(question=Question(stem="题", remote_id="p1"), choices=[0])],
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.message, SUBMIT_BLOCKED_MESSAGE)
        paths = transport.paths()
        self.assertIn(ep.EXAM_ANSWER_SAVE, paths)
        self.assertFalse(any(path.endswith("/submit") for path in paths), "默认绝不该交卷")

    async def test_allow_submit_env_opens_the_gate(self) -> None:
        transport = self._transport()
        adapter = make_adapter(transport, environ={"KEDAIBIAO_NEUSOFT_ALLOW_SUBMIT": "1"})
        result = await adapter.submit(
            Assignment(remote_id=TEST_ID, title="作业一"),
            [Answer(question=Question(stem="题", remote_id="p1"), choices=[0])],
        )
        self.assertTrue(result.ok)
        self.assertTrue(any(path.endswith(ep.EXAM_SUBMIT) for path in transport.paths()))

    async def test_save_that_already_submitted_does_not_submit_twice(self) -> None:
        """协议档案 §5.3：``isSubmitPaper`` 非空 = 服务端顺手交了卷，不能再 PUT 一次。"""
        transport = self._transport()
        transport.json("POST", "/answer/save", {"isSubmitPaper": "1"})
        adapter = make_adapter(transport, environ={"KEDAIBIAO_NEUSOFT_ALLOW_SUBMIT": "1"})
        result = await adapter.submit(
            Assignment(remote_id=TEST_ID, title="作业一"),
            [Answer(question=Question(stem="题", remote_id="p1"), choices=[0])],
        )
        self.assertTrue(result.ok)
        self.assertIn("isSubmitPaper", result.message)
        self.assertFalse(
            any(path.endswith(ep.EXAM_SUBMIT) for path in transport.paths()),
            "保存已经交卷了，不该再发一次 submit",
        )

    async def test_no_answers_returns_failure_without_requests(self) -> None:
        transport = self._transport()
        adapter = make_adapter(transport)
        result = await adapter.submit(Assignment(remote_id=TEST_ID, title="x"), [])
        self.assertFalse(result.ok)
        self.assertEqual(transport.requests, [])


class ScoreReadbackTest(unittest.IsolatedAsyncioTestCase):
    """成绩回读：交卷后每题的 candidateScore 累加，没批改时如实说「还没出分」。"""

    def _transport(self, *, graded: bool) -> FakeTransport:
        transport = FakeTransport()
        transport.json("POST", "/answer/save", {"isSubmitPaper": None})
        transport.json("PUT", "/student/submit", {"ok": True})
        judge_choices = [
            {"choiceId": "A", "choiceContent": "正确"},
            {"choiceId": "B", "choiceContent": "错误"},
        ]
        transport.json(
            "GET",
            "/paper",
            {
                "id": "paper-1",
                "name": "期末测试",
                "paperParts": [
                    {
                        "paperQuestionList": [
                            {
                                "id": "row-1",
                                "paperId": "paper-1",
                                "questionType": "3",
                                "paperQuestion": {
                                    "questionNo": 1,
                                    "questionType": "3",
                                    "stemText": "判断题一",
                                    "choices": judge_choices,
                                    "candidateScore": 10 if graded else None,
                                    "questionScore": 10,
                                },
                            },
                            {
                                "id": "row-2",
                                "paperId": "paper-1",
                                "questionType": "1",
                                "paperQuestion": {
                                    "questionNo": 2,
                                    "questionType": "1",
                                    "stemText": "单选题二",
                                    "choices": [
                                        {"choiceId": "A", "choiceContent": "甲"},
                                        {"choiceId": "B", "choiceContent": "乙"},
                                    ],
                                    "candidateScore": 0 if graded else None,
                                    "questionScore": 10,
                                    "candidateAnswer": "A",
                                },
                            },
                        ]
                    }
                ],
            },
        )
        transport.json(
            "GET", "/enter", {"candidateId": "cand1", "historyId": "hist1", "testId": TEST_ID}
        )
        return transport

    async def test_fetch_score_sums_graded_questions(self) -> None:
        adapter = make_adapter(self._transport(graded=True))
        out = await adapter.fetch_score(TEST_ID)
        self.assertTrue(out["graded"])
        self.assertEqual(out["earned"], 10.0)
        self.assertEqual(out["full"], 20.0)
        self.assertEqual(out["score_pct"], 50.0)
        self.assertEqual(out["scored_questions"], 2)
        self.assertEqual(out["name"], "期末测试")
        self.assertEqual([row["no"] for row in out["rows"]], ["1", "2"])

    async def test_fetch_score_reports_not_graded_without_faking_zero(self) -> None:
        adapter = make_adapter(self._transport(graded=False))
        out = await adapter.fetch_score(TEST_ID)
        self.assertFalse(out["graded"])
        self.assertIsNone(out["score_pct"], "没批改不能报 0 分")
        self.assertEqual(out["scored_questions"], 0)

    async def test_submit_message_carries_score_when_graded(self) -> None:
        adapter = make_adapter(
            self._transport(graded=True), environ={"KEDAIBIAO_NEUSOFT_ALLOW_SUBMIT": "1"}
        )
        result = await adapter.submit(
            Assignment(remote_id=TEST_ID, title="作业一"),
            [Answer(question=Question(stem="判断题一", remote_id="row-1"), choices=[0])],
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.score, 50.0)
        self.assertIn("回读得分 10.0/20.0", result.message)

    async def test_submit_survives_score_readback_failure(self) -> None:
        """成绩读不到不该把成功的交卷报成失败。"""
        transport = self._transport(graded=True)

        async def boom(*_args, **_kwargs):
            raise RuntimeError("还没出分")

        adapter = make_adapter(transport, environ={"KEDAIBIAO_NEUSOFT_ALLOW_SUBMIT": "1"})
        adapter.fetch_score = boom  # type: ignore[method-assign]
        result = await adapter.submit(
            Assignment(remote_id=TEST_ID, title="作业一"),
            [Answer(question=Question(stem="判断题一", remote_id="row-1"), choices=[0])],
        )
        self.assertTrue(result.ok)
        self.assertIn("没回读到", result.message)


class PlaybackTest(unittest.IsolatedAsyncioTestCase):
    def _transport(self, *, status: int) -> FakeTransport:
        transport = FakeTransport()
        transport.json("POST", "/listResource", {"resList": [
            {"resId": RES_ID, "resName": "视频一", "resType": 1, "studyStatus": 0},
            {"resId": "400000000000000002", "resName": "文档一", "resType": 3, "studyStatus": 2},
        ], "folderList": []})
        transport.json("POST", "/startStudy", {})
        transport.json("POST", "/studyForAudioOrVideo", {})
        transport.json("POST", "/startStudyForDocPicMd", {})
        transport.json("POST", "/endStudyForDocPicMd", {})
        transport.json("POST", "/getRecordInfo", {"studyStatus": status})
        return transport

    async def test_completes_pending_and_skips_done(self) -> None:
        transport = self._transport(status=2)
        adapter = make_adapter(transport)
        report = await adapter.play_course(course())
        self.assertEqual(report.completed, [RES_ID])
        self.assertEqual(report.skipped, ["400000000000000002"])
        self.assertTrue(report.ok)

    async def test_api_success_but_not_completed_counts_as_failure(self) -> None:
        """接口全 code=0，回读仍是未完成 —— 这就是本平台最大的坑。"""
        transport = self._transport(status=0)
        adapter = make_adapter(transport)
        report = await adapter.play_course(course())
        self.assertEqual(report.completed, [])
        self.assertEqual(report.failed, [RES_ID])
        self.assertFalse(report.ok)

    async def test_limit_stops_early(self) -> None:
        transport = self._transport(status=2)
        adapter = make_adapter(transport)
        report = await adapter.play_course(course(), limit=1)
        self.assertEqual(len(report.completed), 1)


class AttendanceTest(unittest.IsolatedAsyncioTestCase):
    async def test_sign_skips_when_not_in_class(self) -> None:
        transport = FakeTransport()
        transport.json("GET", "/getAttendanceDetailId/" + "a1" + "/" + "1000000000000000001", "")
        adapter = make_adapter(transport)
        from kedaibiao.platforms.neusoft.attendance import Attendance

        item = Attendance(attendance_id="a1", status=1, type=2, sign_role=None)
        self.assertFalse(await adapter.sign(item))
        self.assertFalse(any(path.endswith("/update") for path in transport.paths()))

    async def test_sign_submits_with_detail_id(self) -> None:
        transport = FakeTransport()
        transport.json("GET", "/getAttendanceDetailId/a1/1000000000000000001", "detail-9")
        transport.json("PUT", "/update", {"ok": True})
        adapter = make_adapter(transport)
        from kedaibiao.platforms.neusoft.attendance import Attendance

        item = Attendance(attendance_id="a1", status=1, type=2, sign_role=None)
        self.assertTrue(await adapter.sign(item))
        body = transport.body_of("/update")
        self.assertEqual(body["id"], "detail-9")
        self.assertEqual(body["status"], 1)


if __name__ == "__main__":
    unittest.main()
