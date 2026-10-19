"""品课适配器测试：全部离线，靠假 Transport 喂样本。

覆盖五块：

1. 协议层：信封分流（code!=0 / 401 / 403 / missing csrf）、action URL 与请求头、
   危险动作闸门、双层判错、Web/REST 协议选择；
2. 解析层：课程 / 作业与学习任务 / 题目 / 提交状态；
3. 计划层：题型映射、图片题识别、逐题题干答案置信度来源、内容哈希防篡改；
4. **提交守卫**：三条硬规则的每一种拒绝路径，以及「全部通过」的正路；
5. SSO 登录：DER 取模数（含前导 0x00 的坑）、PKCS#1 v1.5、表单字段、
   token 不进日志。

样本是**按 Lead 实测结论与静态字段清单手写的**，不是真实抓包。真实 HAR 到手后，
样本常量要换成抓包内容（用例名与断言意图保持不变）。
"""

from __future__ import annotations

import base64
import hashlib
import pathlib
import json
import os
import unittest
import unittest.mock
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kedaibiao.bank.store import QuestionBank
from kedaibiao.core.events import EventBus
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
from kedaibiao.net.http import HttpClient, Response, UrllibTransport
from kedaibiao.notify.base import Notifier
from kedaibiao.platforms.base import AdapterContext, NotSupportedError
from kedaibiao.platforms.pinco import crypto
from kedaibiao.platforms.pinco import endpoints as ep
from kedaibiao.platforms.pinco import (
    guard,
    parsing,
    plan as plan_mod,
    question_map,
    richtext,
    solving,
)
from kedaibiao.platforms.pinco.adapter import PincoAdapter
from kedaibiao.platforms.pinco import adapter as adapter_mod
from kedaibiao.platforms.pinco.errors import (
    PincoApiError,
    PincoEndpointUnavailable,
    PincoError,
    PincoNotLoggedIn,
    PincoProtocolError,
)
from kedaibiao.platforms.pinco.models import PlanState

# --------------------------------------------------------------------------
# 样本：真实响应（已脱敏）从 tests/fixtures/pinco/ 读，结构不手写
# --------------------------------------------------------------------------
#
# fixture 来自 Lead 用真实浏览器会话只读抓取的响应原文（``data/recon/pinco/
# samples/``），只把课程名、标题、教师名换成示例值，ID / 结构 / HTML 形态保留。
# 手写样本只用于补充实测没覆盖到的分支（例如没有图片的作业）。

FIXTURES = Path(__file__).parent / "fixtures" / "pinco"


def load_fixture(name: str) -> dict[str, Any]:
    """读一个 fixture（真实信封原文）。"""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def fixture_data(name: str) -> Any:
    """取 fixture 的 ``data`` 段（已剥信封）。"""
    return load_fixture(name)["data"]


COURSE_ITEMS = fixture_data("course_list.json")["content"]
CLASS_ITEMS = fixture_data("class_list.json")
HOMEWORK_ITEMS = fixture_data("homework_list.json")["content"]
HOMEWORK_DETAIL = fixture_data("homework_detail.json")
IMAGE_QUESTIONS = fixture_data("homework_questions.json")
PLAIN_QUESTIONS = fixture_data("homework_questions_plain.json")
MATERIAL_ITEMS = fixture_data("material_list.json")["courseMaterials"]
MATERIAL_DETAIL = fixture_data("material_detail.json")

#: 各作业在 fixture 里的 ID（按列表顺序：已提交 / 可作答 / 已过期）
SUBMITTED_HW = HOMEWORK_ITEMS[0]["studentHomeworkId"]
OPEN_HW = HOMEWORK_ITEMS[1]["studentHomeworkId"]
EXPIRED_HW = HOMEWORK_ITEMS[2]["studentHomeworkId"]
#: 材料（学习任务）ID
MATERIAL_ID = MATERIAL_ITEMS[0]["materialId"]

DEFAULT_ACTIONS: dict[str, Any] = {
    ep.ACTION_COURSE_WORKBENCH: {"content": COURSE_ITEMS[:1]},
    ep.ACTION_CLASS_LIST: fixture_data("class_list.json"),
    ep.ACTION_HOMEWORK_LIST: fixture_data("homework_list.json"),
    ep.ACTION_MATERIAL_LIST: fixture_data("material_list.json"),
    ep.ACTION_MATERIAL_DETAIL: MATERIAL_DETAIL,
    # 默认喂「没有图片、能答出来」的题目，方便测正常路径；
    # 图片题的用例各自覆盖这条路由。
    ep.ACTION_HOMEWORK_QUESTIONS: PLAIN_QUESTIONS,
    # 默认给一个「未提交、未过期、允许提交」的状态（守卫生效需要它）
    ep.ACTION_HOMEWORK_DETAIL: {
        "submitted": False,
        "expired": False,
        "canSubmitted": True,
    },
    ep.ACTION_SUBMIT_HOMEWORK: {"success": True, "score": 95.0, "message": "交卷成功"},
}

SUBMIT_ACTION_MARKER = "PostV1"


def envelope(data: Any, *, status: int = 0, message: str = "ok") -> dict[str, Any]:
    """应用层**真实成功信封**：``{"statusCode": 0, "data": …, "message": "ok"}``。

    注意这里**没有 ``code`` 字段** —— 实测就是这样；只认 ``code`` 的实现会把
    每一次成功都判成「未通过鉴权」。
    """
    return {"statusCode": status, "data": data, "message": message}


def api_error(code: int, msg: str = "boom") -> dict[str, Any]:
    """REST 家族的错误信封形态：``{"code": 403, "msg": "access forbidden"}``。"""
    return {"code": code, "msg": msg}


def auth_message(text: str = "missing csrf token") -> dict[str, Any]:
    """未登录形态：HTTP 200 + 只有 ``message``（实测）。"""
    return {"message": text}


@dataclass(slots=True)
class Replies:
    """按顺序应答（翻页测试用）：第 n 次调用吐第 n 个，之后重复最后一个。

    必须用独立类型包一层：**裸列表是合法的真实响应**（班级接口就是 ``data[]``），
    不能拿它当「序列」的意思。
    """

    items: list[Any]


@dataclass(slots=True)
class FakeReply:
    """需要指定 HTTP 状态 / 非 JSON 正文时的应答。"""

    body: dict[str, Any] = field(default_factory=dict)
    status: int = 200
    #: 直接给原始字节（用来造「响应是登录页 HTML」这种非 JSON 情况）
    raw: bytes | None = None


@dataclass(slots=True)
class RecordedCall:
    method: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes | None = None

    @property
    def action(self) -> str:
        query = urllib.parse.urlparse(self.url).query
        return urllib.parse.parse_qs(query).get("actionName", [""])[0]


class FakeTransport:
    """假传输层：按 action 路由应答，**绝不联网**，并且**校验请求契约**。

    只实现 ``net.http.Transport`` 协议里的 ``request``，这正是集成指南第 6 节
    要求的离线验证方式；打不通的 URL 直接抛 ``ConnectionError``（真联网之前
    就会先炸，不会静悄悄出去）。

    ⚠️ 为什么要在这里校验请求头（真实教训）：曾经 161 个离线用例全绿，却因为
    **少一个 ``x-server: main`` 头**在线上全线 500 —— 假 Transport 只看响应、
    不看请求，「线上必需的头/参数」这类约束就锁不住。所以这里对每个打到品课网关
    的请求做最低限度校验：缺头当场失败，而不是留给真实账号去发现。
    """

    #: 打到品课网关的请求必须带的头（缺一个线上就 500 / 鉴权失败）
    REQUIRED_HEADERS = (ep.HEADER_SERVER, ep.HEADER_TRACE_ID, ep.HEADER_LANGUAGE)

    def __init__(self, actions: dict[str, Any] | None = None) -> None:
        self.actions: dict[str, Any] = dict(DEFAULT_ACTIONS)
        self.actions.update(actions or {})
        self.calls: list[RecordedCall] = []
        #: 序列路由的游标（测翻页用）
        self.page_cursor: dict[str, int] = {}

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        timeout: float = 15.0,
    ) -> Response:
        call = RecordedCall(method=method, url=url, headers=dict(headers or {}), body=body)
        self.calls.append(call)
        parsed = urllib.parse.urlparse(url)
        if ep.BASE_URL in url:
            for header in self.REQUIRED_HEADERS:
                if not call.headers.get(header):
                    raise AssertionError(
                        f"请求缺少必需头 {header!r}（{method} {url}）—— 线上会直接 500，"
                        "别把这类约束留给真实账号去发现"
                    )
            if call.headers[ep.HEADER_SERVER] != ep.SERVER_NAME:
                raise AssertionError(
                    f"{ep.HEADER_SERVER} 必须是 {ep.SERVER_NAME!r}，"
                    f"实际是 {call.headers[ep.HEADER_SERVER]!r}"
                )
        action = urllib.parse.parse_qs(parsed.query).get("actionName", [""])[0]
        key = action or parsed.path
        if key not in self.actions:
            # REST 路径带前缀，按「最长后缀匹配」兜一下（最长优先，避免前缀撞车）
            candidates = [
                name
                for name in self.actions
                if name and (key.endswith(name) or name in url)
            ]
            if candidates:
                key = max(candidates, key=len)
        if key not in self.actions:
            raise ConnectionError(f"假传输层没有配置这条路由: {method} {url}")
        reply = self.actions[key]
        if isinstance(reply, Replies):
            cursor = self.page_cursor.get(key, 0)
            reply = reply.items[cursor] if cursor < len(reply.items) else reply.items[-1]
            self.page_cursor[key] = cursor + 1
        if isinstance(reply, FakeReply):
            status = reply.status
            raw = (
                reply.raw
                if reply.raw is not None
                else json.dumps(reply.body, ensure_ascii=False).encode("utf-8")
            )
        else:
            status = 200
            raw = json.dumps(envelope(reply), ensure_ascii=False).encode("utf-8")
        return Response(
            status=status,
            url=url,
            body=raw,
            headers={"Content-Type": "application/json"},
        )

    def action_names(self) -> list[str]:
        return [call.action for call in self.calls if call.action]

    def wrote_anything(self) -> bool:
        """有没有碰到过写操作（提交类 action）。"""
        return any(SUBMIT_ACTION_MARKER in name for name in self.action_names())


class RecordingNotifier(Notifier):
    name = "recording"

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    async def send(self, title: str, body: str, *, level: str = "info") -> bool:
        self.sent.append((title, body, level))
        return True


def make_adapter(
    actions: dict[str, Any] | None = None,
    *,
    bank: QuestionBank | None = None,
    answerer: Any = None,
    bus: EventBus | None = None,
    notifier: RecordingNotifier | None = None,
    min_confidence: float = 0.75,
    login: bool = True,
) -> tuple[PincoAdapter, FakeTransport, RecordingNotifier, EventBus]:
    transport = FakeTransport(actions)
    ctx = AdapterContext(
        http=HttpClient(transport=transport),
        bank=bank,
        bus=bus or EventBus(),
        notifier=notifier or RecordingNotifier(),
    )
    adapter = PincoAdapter(ctx, min_confidence=min_confidence, answerer=answerer)
    if login:
        adapter.adopt_session(authorization="Bearer test-token", csrf_token="csrf-test")
    return adapter, transport, ctx.notifier, ctx.bus  # type: ignore[return-value]


def seed_bank(question: Question, *, confidence: float = 1.0) -> QuestionBank:
    bank = QuestionBank(":memory:")
    bank.upsert(
        question,
        Answer(
            question=question,
            choices=[0],
            source=AnswerSource.BANK_HIT,
            confidence=confidence,
        ),
    )
    return bank


# --------------------------------------------------------------------------
# 1. 题型映射与图片题识别
# --------------------------------------------------------------------------


class TestQuestionMap(unittest.TestCase):
    def test_type_aliases(self) -> None:
        self.assertIs(question_map.map_question_type("single"), QuestionType.SINGLE)
        self.assertIs(question_map.map_question_type("radio"), QuestionType.SINGLE)
        self.assertIs(question_map.map_question_type("多选"), QuestionType.MULTIPLE)
        self.assertIs(question_map.map_question_type("judge"), QuestionType.JUDGE)
        self.assertIs(question_map.map_question_type("fill_blank"), QuestionType.FILL)
        self.assertIs(question_map.map_question_type("essay"), QuestionType.ESSAY)

    def test_unknown_type_is_not_guessed(self) -> None:
        """认不出来的题型就是 UNKNOWN —— 不能靠选项数量猜，猜错会让启发式自信答错。"""
        self.assertIs(question_map.map_question_type("weird-new-type"), QuestionType.UNKNOWN)
        self.assertIs(question_map.map_question_type(None), QuestionType.UNKNOWN)

    def test_image_type_name(self) -> None:
        self.assertIs(question_map.map_question_type("image"), question_map.IMAGE_TYPE)
        self.assertTrue(question_map.is_image_type_name("拍照"))

    def test_image_required_by_flag_field(self) -> None:
        self.assertTrue(question_map.image_required({"requiresImage": True}))
        self.assertFalse(question_map.image_required({"requiresImage": False}))

    def test_image_required_by_type_name(self) -> None:
        self.assertTrue(question_map.image_required({"questionType": "handwrite"}))

    def test_attachment_on_non_choice_is_image(self) -> None:
        self.assertTrue(
            question_map.image_required({"attachments": [1]}, has_options=False)
        )

    def test_attachment_on_choice_is_not_image(self) -> None:
        """选择题里的 attachments 多半是题干配图，不该把整份作业拖进待人工。"""
        self.assertFalse(
            question_map.image_required({"attachments": [1]}, has_options=True)
        )

    def test_mark_and_detect_roundtrip(self) -> None:
        question = Question(stem="上传图", remote_id="q9")
        question_map.mark_image(question)
        self.assertTrue(question_map.is_image_question(question))
        self.assertTrue(question.raw[question_map.IMAGE_FLAG])

    def test_human_reason(self) -> None:
        image = question_map.mark_image(Question(stem="上传图"))
        plain = Question(stem="普通题")
        self.assertIn("图片", question_map.human_reason(image))
        self.assertEqual(question_map.human_reason(plain), "")


class TestEndpointsFacts(unittest.TestCase):
    """端点常量模块的两条纪律，用测试钉住。"""

    def test_tracked_constants_have_evidence_level(self) -> None:
        """新增 action / 字段 / 请求头常量必须登记证据等级。

        没有证据等级的常量意味着「有人凭印象加了一条端点事实」—— 这是本项目
        最不能接受的一类改动，所以让它直接测试失败。
        """
        missing = [
            name
            for name in dir(ep)
            if name.startswith(("ACTION_", "FIELD_", "HEADER_"))
            and isinstance(getattr(ep, name), str)
            and ep.evidence_of(name) is None
        ]
        self.assertEqual(missing, [], "这些常量没有登记证据等级")

    def test_dangerous_actions_are_marked(self) -> None:
        self.assertIn(ep.ACTION_SUBMIT_HOMEWORK, ep.DANGEROUS_ACTIONS)
        self.assertIn(ep.ACTION_SUBMIT_CHAPTER_TASK, ep.DANGEROUS_ACTIONS)
        self.assertIn(ep.ACTION_SUBMIT_COURSE_GROUP_TASK, ep.DANGEROUS_ACTIONS)
        self.assertNotIn(ep.ACTION_HOMEWORK_QUESTIONS, ep.DANGEROUS_ACTIONS)

    def test_parsing_never_uses_absent_student_fields(self) -> None:
        """学生端没有 homeworkId / pageNum / answerList，解析器里不许出现。

        用了它们的后果是静默取到 None，然后看起来像「这个平台没有作业」。
        """
        from pathlib import Path

        source = Path(parsing.__file__).read_text(encoding="utf-8")
        for name in ep.NOT_IN_STUDENT_API:
            self.assertNotIn(f'"{name}"', source, f"{name} 不在学生端接口里")


# --------------------------------------------------------------------------
# 2. 信封与解析
# --------------------------------------------------------------------------


class TestEnvelope(unittest.TestCase):
    """信封：实测有三种形态，混了就会把成功判成失败。"""

    def test_real_success_envelope_has_no_code(self) -> None:
        """应用层成功是 ``{"statusCode": 0, "data": …, "message": "ok"}``，没有 code。"""
        payload = envelope({"content": []})
        self.assertNotIn("code", payload)
        self.assertEqual(parsing.unwrap_envelope(payload, action="X"), {"content": []})

    def test_missing_data_is_empty_dict(self) -> None:
        self.assertEqual(
            parsing.unwrap_envelope({"statusCode": 0, "message": "ok"}), {}
        )

    def test_401_is_not_logged_in(self) -> None:
        with self.assertRaises(PincoNotLoggedIn):
            parsing.unwrap_envelope({"statusCode": 401, "message": "unauthorized"}, action="X")

    def test_403_is_not_logged_in(self) -> None:
        payload = {"code": 403, "msg": "access forbidden"}
        with self.assertRaises(PincoNotLoggedIn):
            parsing.unwrap_envelope(payload, action="X")

    def test_http_401_without_status_code(self) -> None:
        with self.assertRaises(PincoNotLoggedIn):
            parsing.unwrap_envelope({"message": "nope"}, action="X", http_status=401)

    def test_business_status_code_raises_api_error(self) -> None:
        """实测形态：``{"statusCode": 6571, "message": "没有该课程权限"}``。"""
        payload = {"statusCode": 6571, "message": "没有该课程权限"}
        with self.assertRaises(PincoApiError) as cm:
            parsing.unwrap_envelope(payload, action="X")
        self.assertIn("没有该课程权限", str(cm.exception))
        self.assertEqual(cm.exception.code, 6571)

    def test_illegal_argument_is_protocol_error_not_auth(self) -> None:
        """实测：401 也可能是「参数不对」，不能当成会话失效去重登。"""
        payload = {"statusCode": 401, "message": "illegal argument"}
        with self.assertRaises(PincoProtocolError) as cm:
            parsing.unwrap_envelope(payload, action="X")
        self.assertIn("illegal argument", str(cm.exception))

    def test_err_assertion_names_unknown_action(self) -> None:
        """实测：action 名不存在 → ``{"code":"ERR_ASSERTION","message":"Internal Server Error"}``。"""
        payload = {"code": "ERR_ASSERTION", "message": "Internal Server Error"}
        with self.assertRaises(PincoApiError) as cm:
            parsing.unwrap_envelope(payload, action="TotallyWrongName")
        self.assertIn("action 名写错", str(cm.exception))

    def test_empty_object_is_not_empty_data(self) -> None:
        """空壳 action 返回 ``{}`` —— 不是合法信封，必须报错而不是当成空数据。"""
        with self.assertRaises(PincoProtocolError):
            parsing.unwrap_envelope({}, action="GetV1StudentChapterTaskList")

    def test_missing_csrf_message_is_not_logged_in(self) -> None:
        """实测形态：HTTP 200 + {"message":"missing csrf token"}。"""
        with self.assertRaises(PincoNotLoggedIn) as cm:
            parsing.unwrap_envelope({"message": "missing csrf token"}, action="X")
        self.assertIn("csrf", str(cm.exception))

    def test_non_dict_is_protocol_error(self) -> None:
        with self.assertRaises(PincoProtocolError):
            parsing.unwrap_envelope("<html>login</html>", action="X")


class TestParseBusinessData(unittest.TestCase):
    """解析层：字段名与结构都按 fixture 里的真实响应核对。"""

    def test_parse_courses_from_real_sample(self) -> None:
        courses = parsing.parse_courses({"content": COURSE_ITEMS})
        self.assertEqual(
            [c.remote_id for c in courses],
            ["1326193414086569984", "1321128857631563776"],
        )
        self.assertEqual(courses[0].name, "示例课程 A")
        self.assertEqual(courses[0].teacher, "教师甲")

    def test_parse_courses_raises_when_no_item_has_id(self) -> None:
        """整批课程都没有 ID = 字段名不符，必须炸，不能安静返回空列表。"""
        with self.assertRaises(PincoProtocolError):
            parsing.parse_courses({"content": [{"name": "无 id 的课"}]})

    def test_parse_courses_skips_single_bad_item(self) -> None:
        courses = parsing.parse_courses({"content": [{"name": "无 id"}, *COURSE_ITEMS[:1]]})
        self.assertEqual([c.remote_id for c in courses], ["1326193414086569984"])

    def test_parse_classes_from_real_sample(self) -> None:
        classes = parsing.parse_classes(CLASS_ITEMS)
        self.assertEqual([c.class_id for c in classes], ["2001", "2002"])
        self.assertEqual(classes[0].name, "示例班级 A")
        self.assertEqual(classes[0].student_count, 42)

    def test_parse_assignments_from_real_sample(self) -> None:
        items = parsing.parse_assignments({"content": HOMEWORK_ITEMS})
        self.assertEqual(
            [a.remote_id for a in items], [SUBMITTED_HW, OPEN_HW, EXPIRED_HW]
        )
        self.assertEqual(items[1].title, "示例作业-可作答")
        self.assertIs(items[1].kind, TaskKind.HOMEWORK)
        self.assertTrue(items[0].finished, "submitted=true 的作业算已完成")
        self.assertFalse(items[1].finished)
        # 列表接口没有 deadline（实测只有详情与题目接口有）
        self.assertIsNone(items[1].deadline)

    def test_assignment_blocker_separates_the_three_flags(self) -> None:
        """实测：submitted / expired / canSubmitted 互相独立，必须分开判。"""
        self.assertIn("已提交", parsing.assignment_blocker(HOMEWORK_ITEMS[0]))
        self.assertEqual(parsing.assignment_blocker(HOMEWORK_ITEMS[1]), "")
        self.assertIn("已过期", parsing.assignment_blocker(HOMEWORK_ITEMS[2]))

    def test_assignment_blocker_can_submitted_false(self) -> None:
        item = {"studentHomeworkId": "x", "submitted": False, "expired": False,
                "canSubmitted": False}
        self.assertIn("不可提交", parsing.assignment_blocker(item))

    def test_parse_assignments_default_kind_for_learning_tasks(self) -> None:
        items = parsing.parse_assignments(
            {"content": [{"chapterTaskId": "ct-1", "title": "第1章 视频"}]},
            default_kind=TaskKind.COURSE,
        )
        self.assertEqual(items[0].remote_id, "ct-1")
        self.assertIs(items[0].kind, TaskKind.COURSE)

    def test_parse_assignments_raises_when_no_item_has_id(self) -> None:
        """整批都没有 ID = 字段名不符，必须炸，不能安静返回空列表。"""
        with self.assertRaises(PincoProtocolError):
            parsing.parse_assignments({"content": [{"title": "没有 id"}]})

    def test_parse_assignments_skips_single_bad_item(self) -> None:
        items = parsing.parse_assignments(
            {"content": [{"title": "坏条目"}, HOMEWORK_ITEMS[1]]}
        )
        self.assertEqual([a.remote_id for a in items], [OPEN_HW])

    # -- 题目 ---------------------------------------------------------------

    def test_parse_questions_from_real_sample(self) -> None:
        """真实样本：3 道主观题（含 1 道图片题、1 道上传题）+ 2 道单选，题干是 HTML。"""
        questions = parsing.parse_questions(IMAGE_QUESTIONS)
        self.assertEqual(len(questions), 5)
        self.assertIs(questions[0].type, QuestionType.ESSAY)
        self.assertIs(questions[3].type, QuestionType.SINGLE)
        self.assertEqual(questions[0].remote_id, "1328382733296533512")
        self.assertIn("节点电压法", questions[0].stem)
        self.assertNotIn("<p>", questions[0].stem, "题干要抽成纯文本")
        self.assertEqual(questions[0].score, 10.0)

    def test_parse_questions_reads_option_letters_in_order(self) -> None:
        questions = parsing.parse_questions(IMAGE_QUESTIONS)
        choice = questions[3]
        self.assertEqual(choice.options[:2], ["i1(t)滞后i2(t)60°", "i1(t)超前i2(t)60°"])

    def test_image_only_option_is_labelled(self) -> None:
        """选项本身是图片（实测 rich-formula 选项）时不能渲染成空字符串。"""
        questions = parsing.parse_questions(IMAGE_QUESTIONS)
        self.assertEqual(questions[4].options, ["(图片选项)", "(图片选项)"])

    def test_parse_questions_does_not_fabricate_unknown_groups(self) -> None:
        """未知分组按空处理（并告警），不能凭空造题。"""
        questions = parsing.parse_questions({"judges": [{"questionId": "q9"}]})
        self.assertEqual(questions, [])

    def test_parse_questions_skips_item_without_stem_and_id(self) -> None:
        data = {"shortAnswers": [{"standardScore": 10}, *IMAGE_QUESTIONS["shortAnswers"]]}
        questions = parsing.parse_questions(data)
        self.assertEqual(len(questions), len(IMAGE_QUESTIONS["shortAnswers"]))

    def test_parse_questions_requires_object(self) -> None:
        with self.assertRaises(PincoProtocolError):
            parsing.parse_questions("nope")

    def test_parse_deadline_from_epoch_millis(self) -> None:
        self.assertIsNotNone(parsing.parse_deadline(IMAGE_QUESTIONS))
        self.assertIsNone(parsing.parse_deadline({}))

    # -- 提交状态 / 结果 -----------------------------------------------------

    def test_parse_submission_state_from_real_detail(self) -> None:
        state = parsing.parse_submission_state(HOMEWORK_DETAIL, assignment_id=EXPIRED_HW)
        self.assertFalse(state.submitted)
        self.assertTrue(state.expired)
        self.assertFalse(state.can_submit)

    def test_parse_submission_state_without_flag_raises(self) -> None:
        """状态字段缺失必须抛错：不能兜底成「没交过」。"""
        with self.assertRaises(PincoProtocolError):
            parsing.parse_submission_state({"foo": 1}, assignment_id="hw-1")

    def test_parse_submit_result_ok_from_envelope(self) -> None:
        result = parsing.parse_submit_result({"score": 88}, assignment_id="hw-1")
        self.assertTrue(result.ok)
        self.assertEqual(result.score, 88.0)

    def test_parse_submit_result_explicit_failure(self) -> None:
        result = parsing.parse_submit_result(
            {"success": False, "message": "已过截止时间"}, assignment_id="hw-1"
        )
        self.assertFalse(result.ok)
        self.assertIn("已过截止时间", result.message)


# --------------------------------------------------------------------------
# 3. 客户端：URL / 请求头 / 危险动作闸门
# --------------------------------------------------------------------------


class TestClient(unittest.IsolatedAsyncioTestCase):
    async def test_url_and_headers(self) -> None:
        adapter, transport, _, _ = make_adapter()
        await adapter.list_courses()

        call = transport.calls[0]
        self.assertEqual(call.method, "POST")
        self.assertEqual(call.action, ep.ACTION_COURSE_WORKBENCH)
        self.assertIn(ep.STUDENT_API_PATH, call.url)
        self.assertEqual(call.headers[ep.HEADER_CSRF], "csrf-test")
        self.assertEqual(call.headers[ep.HEADER_AUTHORIZATION], "Bearer test-token")
        self.assertTrue(call.headers[ep.HEADER_TRACE_ID])
        self.assertEqual(call.headers[ep.HEADER_LANGUAGE], ep.DEFAULT_LANGUAGE)
        # 实测必需的网关路由头：缺了它服务端回 500 ERR_ASSERTION，形状与
        # 「action 名写错」相同。这条断言原先写反了（断言它「不能出现」），
        # 是端到端实测抓出来的 —— 离线单测的假 Transport 不校验请求头，锁不住这类错误。
        self.assertEqual(call.headers[ep.HEADER_SERVER], ep.SERVER_NAME)

    async def test_csrf_read_from_cookie_jar_when_not_injected(self) -> None:
        class JarCookie:
            name = ep.CSRF_COOKIE
            value = "csrf-from-jar"

        adapter, transport, _, _ = make_adapter()
        adapter.adopt_session(authorization="Bearer t")
        adapter.ctx.http.transport.cookies = [JarCookie()]  # type: ignore[attr-defined]
        await adapter.list_courses()
        self.assertEqual(transport.calls[0].headers[ep.HEADER_CSRF], "csrf-from-jar")

    async def test_dangerous_action_refused_without_flag(self) -> None:
        adapter, transport, _, _ = make_adapter()
        with self.assertRaises(PincoProtocolError):
            await adapter.client().post_action(ep.ACTION_SUBMIT_HOMEWORK, {})
        self.assertEqual(transport.calls, [], "被拦下的动作不该发出任何请求")

    async def test_dangerous_action_allowed_with_flag(self) -> None:
        adapter, transport, _, _ = make_adapter(
            {ep.ACTION_SUBMIT_HOMEWORK: {"success": True}}
        )
        await adapter.client().post_action(
            ep.ACTION_SUBMIT_HOMEWORK, {}, dangerous=True
        )
        self.assertEqual(transport.action_names(), [ep.ACTION_SUBMIT_HOMEWORK])

    async def test_health_check_uses_rest_current_time(self) -> None:
        """健康检查走实测可用的 REST 接口，不碰那个恒 500 的死 action。"""
        adapter, transport, _, _ = make_adapter(
            {ep.REST_PATH_CURRENT_TIME: {"currentTime": 1760000000000}}
        )
        self.assertTrue(await adapter.health_check())
        self.assertEqual(transport.calls[0].method, "GET")
        self.assertIn(ep.REST_API_PREFIX, transport.calls[0].url)
        self.assertNotIn(ep.ACTION_FAQ, transport.action_names())

    async def test_health_check_false_on_api_error(self) -> None:
        adapter, _, _, _ = make_adapter(
            {
                ep.REST_PATH_CURRENT_TIME: FakeReply(
                    {"code": 403, "msg": "access forbidden"}, status=403
                )
            }
        )
        self.assertFalse(await adapter.health_check())

    async def test_health_check_false_when_network_is_down(self) -> None:
        """断网抛的是 TransportError（OSError 子类）：必须返回 False，不能上抛。"""
        adapter, transport, _, _ = make_adapter()

        async def refuse(*args: Any, **kwargs: Any) -> Response:
            raise ConnectionError("网络断了")

        transport.request = refuse  # type: ignore[method-assign]
        self.assertFalse(await adapter.health_check())

    async def test_health_check_false_on_non_json_body(self) -> None:
        adapter, _, _, _ = make_adapter(
            {ep.REST_PATH_CURRENT_TIME: FakeReply(raw=b"<html>502</html>", status=200)}
        )
        self.assertFalse(await adapter.health_check())


# --------------------------------------------------------------------------
# 4. 计划生成：逐题内容 + 图片题人工回路
# --------------------------------------------------------------------------


class TestPlanBuilder(unittest.IsolatedAsyncioTestCase):
    async def _plan_for(self, question: Question, answer: Answer | None, **kw: Any):
        assignment = Assignment(remote_id="hw-1", title="第一章作业")
        answerer = _FixedAnswerer(answer)
        builder = solving.PlanBuilder(
            answerer=answerer,
            bus=kw.get("bus"),
            notifier=kw.get("notifier"),
            min_confidence=kw.get("min_confidence", 0.75),
        )
        return await builder.build([(assignment, [question])])

    async def test_bank_hit_is_planned(self) -> None:
        question = Question(stem="题", options=["甲"], remote_id="q1")
        answer = Answer(
            question=question, choices=[0], source=AnswerSource.BANK_HIT, confidence=1.0
        )
        plan = await self._plan_for(question, answer)
        item = plan.assignments[0]
        self.assertIs(item.state, PlanState.PLANNED)
        row = item.questions[0]
        self.assertFalse(row.needs_human)
        self.assertEqual(row.source, AnswerSource.BANK_HIT)
        self.assertEqual(row.confidence, 1.0)
        self.assertEqual(row.answer_label(), "甲")

    async def test_heuristic_guess_is_needs_human(self) -> None:
        question = Question(stem="题", type=QuestionType.JUDGE, remote_id="q1")
        answer = Answer(
            question=question, choices=[0], source=AnswerSource.HEURISTIC, confidence=0.35
        )
        plan = await self._plan_for(question, answer)
        row = plan.assignments[0].questions[0]
        self.assertTrue(row.needs_human)
        self.assertIn("启发式", row.reason)
        self.assertIs(plan.assignments[0].state, PlanState.NEEDS_HUMAN)

    async def test_empty_answer_is_needs_human(self) -> None:
        question = Question(stem="没答出来", remote_id="q1")
        plan = await self._plan_for(question, None)
        row = plan.assignments[0].questions[0]
        self.assertTrue(row.needs_human)
        self.assertIn("没答出来", row.reason)

    async def test_low_confidence_is_needs_human(self) -> None:
        question = Question(stem="题", options=["甲"], remote_id="q1")
        answer = Answer(
            question=question, choices=[0], source=AnswerSource.MANUAL, confidence=0.4
        )
        plan = await self._plan_for(question, answer, min_confidence=0.75)
        self.assertTrue(plan.assignments[0].questions[0].needs_human)

    async def test_image_question_never_gets_an_answer(self) -> None:
        """图片题即使答题器给了答案也不采用 —— 它要的是一张图。"""
        question = question_map.mark_image(Question(stem="请上传设计图", remote_id="q2"))
        answer = Answer(
            question=question, text="随便写点", source=AnswerSource.MANUAL, confidence=1.0
        )
        plan = await self._plan_for(question, answer)
        row = plan.assignments[0].questions[0]
        self.assertTrue(row.image_required)
        self.assertTrue(row.needs_human)
        self.assertEqual(row.text, "")
        self.assertEqual(row.choices, [])
        self.assertFalse(row.has_answer)
        self.assertIs(plan.assignments[0].state, PlanState.NEEDS_HUMAN)

    async def test_needs_human_emits_events_and_notification(self) -> None:
        bus = EventBus()
        notifier = RecordingNotifier()
        seen: list[str] = []
        bus.on("*", lambda event: seen.append(event.name))

        question = question_map.mark_image(Question(stem="上传图", remote_id="q2"))
        await self._plan_for(question, None, bus=bus, notifier=notifier)

        self.assertIn(solving.EVENT_QUESTION_NEEDS_HUMAN, seen)
        self.assertIn(solving.EVENT_ASSIGNMENT_NEEDS_HUMAN, seen)
        self.assertEqual(len(notifier.sent), 1)
        self.assertEqual(notifier.sent[0][2], "warning")
        self.assertIn("人工", notifier.sent[0][0])

    async def test_planned_assignment_emits_nothing(self) -> None:
        bus = EventBus()
        notifier = RecordingNotifier()
        question = Question(stem="题", options=["甲"], remote_id="q1")
        answer = Answer(
            question=question, choices=[0], source=AnswerSource.BANK_HIT, confidence=1.0
        )
        await self._plan_for(question, answer, bus=bus, notifier=notifier)
        self.assertEqual(notifier.sent, [])

    async def test_finished_assignment_is_skipped_without_questions(self) -> None:
        builder = solving.PlanBuilder(answerer=_FixedAnswerer(None))
        plan = await builder.build(
            [(Assignment(remote_id="a", title="已完成", finished=True), [])]
        )
        self.assertIs(plan.assignments[0].state, PlanState.SKIPPED)
        self.assertIn("平台已标记完成", plan.assignments[0].reason)

    async def test_no_questions_is_skipped(self) -> None:
        builder = solving.PlanBuilder(answerer=_FixedAnswerer(None))
        plan = await builder.build([(Assignment(remote_id="a", title="空作业"), [])])
        self.assertIs(plan.assignments[0].state, PlanState.SKIPPED)
        self.assertIn("没有取到题目", plan.assignments[0].reason)

    async def test_answerer_length_mismatch_is_padded(self) -> None:
        """答题器违约返回条数不符时补齐，避免答案串题。"""
        builder = solving.PlanBuilder(answerer=_ShortAnswerer())
        questions = [Question(stem="题1", remote_id="q1"), Question(stem="题2", remote_id="q2")]
        plan = await builder.build([(Assignment(remote_id="a", title="t"), questions)])
        self.assertEqual(len(plan.assignments[0].questions), 2)
        self.assertTrue(plan.assignments[0].questions[1].needs_human)


class TestPlanArtifact(unittest.IsolatedAsyncioTestCase):
    async def _sample_plan(self):
        question = Question(
            stem="下列哪项属于结构化数据",
            type=QuestionType.SINGLE,
            options=["关系表", "图片", "音频"],
            remote_id="q1",
        )
        answer = Answer(
            question=question,
            choices=[0],
            source=AnswerSource.BANK_HIT,
            confidence=1.0,
        )
        builder = solving.PlanBuilder(answerer=_FixedAnswerer(answer))
        plan = await builder.build(
            [(Assignment(remote_id="hw-1", title="第一章作业"), [question])]
        )
        return plan, question, answer

    async def test_render_text_has_everything_a_reviewer_needs(self) -> None:
        plan, _, _ = await self._sample_plan()
        text = plan.render_text()
        self.assertIn("下列哪项属于结构化数据", text)
        self.assertIn("关系表", text)
        self.assertIn("bank_hit", text)
        self.assertIn("1.00", text)
        self.assertIn("plan", text.lower())

    async def test_render_text_lists_human_questions(self) -> None:
        question = question_map.mark_image(Question(stem="上传图", remote_id="q2"))
        builder = solving.PlanBuilder(answerer=_FixedAnswerer(None))
        plan = await builder.build(
            [(Assignment(remote_id="hw-2", title="图片作业"), [question])]
        )
        text = plan.render_text()
        self.assertIn("需要人工", text)
        self.assertIn("上传图", text)

    async def test_seal_and_verify(self) -> None:
        plan, _, _ = await self._sample_plan()
        self.assertTrue(plan.digest)
        plan.verify()  # 不抛

    async def test_tampered_plan_is_rejected(self) -> None:
        plan, _, _ = await self._sample_plan()
        plan.assignments[0].questions[0].choices = [1]  # 复核之后被人改了答案
        with self.assertRaises(plan_mod.PlanTamperedError):
            plan.verify()

    async def test_unsealed_plan_is_rejected(self) -> None:
        empty = plan_mod.PincoPlan(
            assignments=[
                plan_mod.PincoAssignmentPlan(
                    platform="pinco", assignment_id="a1", title="t", questions=[]
                )
            ]
        )
        with self.assertRaises(plan_mod.PlanTamperedError):
            empty.verify()

    async def test_json_roundtrip_keeps_digest(self) -> None:
        plan, _, _ = await self._sample_plan()
        restored = plan_mod.PincoPlan.from_dict(json.loads(plan.json_text()))
        self.assertEqual(restored.digest, plan.digest)
        self.assertEqual(
            restored.assignments[0].content_digest(),
            plan.assignments[0].content_digest(),
        )

    async def test_plan_digest_equals_submission_digest(self) -> None:
        """计划里的哈希与「真提交时算的哈希」必须一致，否则守卫毫无意义。"""
        plan, _, answer = await self._sample_plan()
        assignment = Assignment(remote_id="hw-1", title="第一章作业")
        self.assertEqual(
            plan.assignments[0].content_digest(),
            plan_mod.submission_digest(assignment, [answer]),
        )


# --------------------------------------------------------------------------
# 5. 提交守卫
# --------------------------------------------------------------------------


class _FixedAnswerer:
    """固定返回同一份答案（按题数复制），用于把计划摆成想要的样子。"""

    def __init__(self, answer: Answer | None) -> None:
        self._answer = answer

    async def answer(self, questions: list[Question]) -> list[Answer]:
        if self._answer is None:
            return [Answer(question=q, source=AnswerSource.UNKNOWN) for q in questions]
        return [
            Answer(
                question=question,
                choices=list(self._answer.choices),
                text=self._answer.text,
                source=self._answer.source,
                confidence=self._answer.confidence,
                raw=dict(self._answer.raw),
            )
            for question in questions
        ]


class _ShortAnswerer:
    """故意少返一条，验证计划层补齐而不是错位。"""

    async def answer(self, questions: list[Question]) -> list[Answer]:
        first = questions[0]
        return [
            Answer(
                question=first, choices=[0], source=AnswerSource.BANK_HIT, confidence=1.0
            )
        ]


IMAGE_ERRORS = (PincoError, guard.SubmissionRefused)


class TestSubmitGuard(unittest.IsolatedAsyncioTestCase):
    """三条硬规则 + 「全部通过」的正路。每条拒绝路径都有独立用例锁住。"""

    async def _setup(self, *, image: bool = False, confidence: float = 1.0):
        question = (
            question_map.mark_image(Question(stem="请上传设计图", remote_id="q2"))
            if image
            else Question(
                stem="下列哪项属于结构化数据",
                type=QuestionType.SINGLE,
                options=["关系表", "图片", "音频"],
                remote_id="q1",
            )
        )
        source = AnswerSource.UNKNOWN if image else AnswerSource.BANK_HIT
        answer = Answer(
            question=question,
            choices=[] if image else [0],
            source=source,
            confidence=0.0 if image else confidence,
        )
        adapter, transport, _, _ = make_adapter()
        adapter.submit_payload_confirmed = True
        plan = await solving.PlanBuilder(answerer=_FixedAnswerer(answer)).build(
            [(Assignment(remote_id="hw-1", title="第一章作业"), [question])]
        )
        return adapter, transport, plan, answer, question

    async def test_submit_refused_without_reviewed_plan(self) -> None:
        adapter, transport, plan, answer, _ = await self._setup()
        with self.assertRaises(guard.SubmissionRefused) as cm:
            await adapter.submit(Assignment(remote_id="hw-1", title="t"), [answer])
        self.assertEqual(cm.exception.reason, guard.REFUSAL_NO_REVIEWED_PLAN)
        self.assertFalse(transport.wrote_anything(), "没有复核计划就绝不能发写请求")

    async def test_submit_refused_when_content_hash_mismatch(self) -> None:
        adapter, transport, plan, answer, _ = await self._setup()
        adapter.confirm_plan(plan)
        # 复核之后偷偷改了答案：内容哈希与计划不符
        tampered = Answer(
            question=answer.question,
            choices=[2],
            source=AnswerSource.BANK_HIT,
            confidence=1.0,
        )
        with self.assertRaises(guard.SubmissionRefused) as cm:
            await adapter.submit(Assignment(remote_id="hw-1", title="第一章作业"), [tampered])
        self.assertEqual(cm.exception.reason, guard.REFUSAL_HASH_MISMATCH)
        self.assertFalse(transport.wrote_anything())

    async def test_submit_refused_when_plan_tampered_after_review(self) -> None:
        """计划在复核之后被改过 —— 即使提交内容与「改后的计划」一致也要拒。"""
        adapter, transport, plan, answer, _ = await self._setup()
        reviewed = adapter.confirm_plan(plan)
        reviewed.plan.assignments[0].questions[0].choices = [1]
        with self.assertRaises(guard.SubmissionRefused) as cm:
            await adapter.submit(Assignment(remote_id="hw-1", title="第一章作业"), [answer])
        self.assertEqual(cm.exception.reason, guard.REFUSAL_HASH_MISMATCH)
        self.assertFalse(transport.wrote_anything())

    async def test_submit_refused_for_assignment_not_in_plan(self) -> None:
        adapter, transport, plan, answer, _ = await self._setup()
        adapter.confirm_plan(plan)
        other = Assignment(remote_id="hw-other", title="别的作业")
        with self.assertRaises(guard.SubmissionRefused) as cm:
            await adapter.submit(other, [answer])
        self.assertEqual(cm.exception.reason, guard.REFUSAL_ASSIGNMENT_NOT_IN_PLAN)
        self.assertFalse(transport.wrote_anything())

    async def test_submit_refused_when_image_question_unanswered(self) -> None:
        adapter, transport, plan, answer, _ = await self._setup(image=True)
        adapter.confirm_plan(plan)
        with self.assertRaises(guard.SubmissionRefused) as cm:
            await adapter.submit(Assignment(remote_id="hw-1", title="第一章作业"), [answer])
        self.assertEqual(cm.exception.reason, guard.REFUSAL_HUMAN_QUESTIONS)
        self.assertFalse(transport.wrote_anything())

    async def test_submit_refused_when_low_confidence_answer(self) -> None:
        adapter, transport, plan, answer, _ = await self._setup(confidence=0.4)
        adapter.submit_payload_confirmed = True
        adapter.confirm_plan(plan)
        with self.assertRaises(guard.SubmissionRefused) as cm:
            await adapter.submit(Assignment(remote_id="hw-1", title="第一章作业"), [answer])
        self.assertEqual(cm.exception.reason, guard.REFUSAL_HUMAN_QUESTIONS)
        self.assertFalse(transport.wrote_anything())

    async def test_submit_refused_when_platform_already_submitted(self) -> None:
        adapter, transport, plan, answer, _ = await self._setup()
        adapter.confirm_plan(plan)
        adapter.ctx.http.transport.actions[ep.ACTION_HOMEWORK_DETAIL] = {"submitted": True}
        with self.assertRaises(guard.SubmissionRefused) as cm:
            await adapter.submit(Assignment(remote_id="hw-1", title="第一章作业"), [answer])
        self.assertEqual(cm.exception.reason, guard.REFUSAL_ALREADY_SUBMITTED)
        self.assertFalse(transport.wrote_anything())

    async def test_submit_refused_when_platform_says_expired(self) -> None:
        """实测组合：submitted=false 但 expired=true —— 交不上去，必须拒。"""
        adapter, transport, plan, answer, _ = await self._setup()
        adapter.confirm_plan(plan)
        adapter.ctx.http.transport.actions[ep.ACTION_HOMEWORK_DETAIL] = {
            "submitted": False,
            "expired": True,
            "canSubmitted": False,
        }
        with self.assertRaises(guard.SubmissionRefused) as cm:
            await adapter.submit(Assignment(remote_id="hw-1", title="第一章作业"), [answer])
        self.assertEqual(cm.exception.reason, guard.REFUSAL_NOT_SUBMITTABLE)
        self.assertFalse(transport.wrote_anything())

    async def test_submit_refused_when_platform_disallows_submit(self) -> None:
        adapter, transport, plan, answer, _ = await self._setup()
        adapter.confirm_plan(plan)
        adapter.ctx.http.transport.actions[ep.ACTION_HOMEWORK_DETAIL] = {
            "submitted": False,
            "expired": False,
            "canSubmitted": False,
        }
        with self.assertRaises(guard.SubmissionRefused) as cm:
            await adapter.submit(Assignment(remote_id="hw-1", title="第一章作业"), [answer])
        self.assertEqual(cm.exception.reason, guard.REFUSAL_NOT_SUBMITTABLE)
        self.assertFalse(transport.wrote_anything())

    async def test_submit_refused_when_state_probe_fails(self) -> None:
        """状态查不到就不许交 —— 「不知道」不能当成「没交过」。"""
        adapter, transport, plan, answer, _ = await self._setup()
        adapter.confirm_plan(plan)
        adapter.ctx.http.transport.actions[ep.ACTION_HOMEWORK_DETAIL] = FakeReply(
            {"statusCode": 500, "message": "boom"}, status=500
        )
        with self.assertRaises(guard.SubmissionRefused) as cm:
            await adapter.submit(Assignment(remote_id="hw-1", title="第一章作业"), [answer])
        self.assertEqual(cm.exception.reason, guard.REFUSAL_STATE_UNKNOWN)
        self.assertFalse(transport.wrote_anything())

    async def test_submit_refused_when_state_flag_missing(self) -> None:
        adapter, transport, plan, answer, _ = await self._setup()
        adapter.confirm_plan(plan)
        adapter.ctx.http.transport.actions[ep.ACTION_HOMEWORK_DETAIL] = {"foo": 1}
        with self.assertRaises(guard.SubmissionRefused) as cm:
            await adapter.submit(Assignment(remote_id="hw-1", title="第一章作业"), [answer])
        self.assertEqual(cm.exception.reason, guard.REFUSAL_STATE_UNKNOWN)
        self.assertFalse(transport.wrote_anything())

    async def test_submit_succeeds_when_all_guards_pass(self) -> None:
        adapter, transport, plan, answer, _ = await self._setup()
        adapter.confirm_plan(plan)
        result = await adapter.submit(
            Assignment(remote_id="hw-1", title="第一章作业"), [answer]
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.score, 95.0)
        writes = [name for name in transport.action_names() if SUBMIT_ACTION_MARKER in name]
        self.assertEqual(writes, [ep.ACTION_SUBMIT_HOMEWORK])

    async def test_submit_refused_when_payload_shape_unconfirmed(self) -> None:
        """结构没验证过就不发写请求 —— 交错的答案比不交更糟。"""
        adapter, transport, plan, answer, _ = await self._setup()
        adapter.submit_payload_confirmed = False
        adapter.confirm_plan(plan)
        with self.assertRaises(PincoError) as cm:
            await adapter.submit(Assignment(remote_id="hw-1", title="第一章作业"), [answer])
        self.assertIn("尚未由真实样本确认", str(cm.exception))
        self.assertFalse(transport.wrote_anything())

    async def test_second_submit_in_same_session_is_refused(self) -> None:
        adapter, transport, plan, answer, _ = await self._setup()
        adapter.confirm_plan(plan)
        assignment = Assignment(remote_id="hw-1", title="第一章作业")
        await adapter.submit(assignment, [answer])
        with self.assertRaises(guard.SubmissionRefused) as cm:
            await adapter.submit(assignment, [answer])
        self.assertEqual(cm.exception.reason, guard.REFUSAL_ALREADY_SENT)
        writes = [name for name in transport.action_names() if SUBMIT_ACTION_MARKER in name]
        self.assertEqual(len(writes), 1, "第二次提交绝不能真的发出去")

    async def test_network_failure_reports_unknown_and_blocks_retry(self) -> None:
        adapter, transport, plan, answer, _ = await self._setup()
        adapter.confirm_plan(plan)
        adapter.ctx.http.transport.actions[ep.ACTION_SUBMIT_HOMEWORK] = FakeReply(
            {"statusCode": 500, "message": "服务器开小差了"}, status=500
        )
        assignment = Assignment(remote_id="hw-1", title="第一章作业")
        result = await adapter.submit(assignment, [answer])
        self.assertFalse(result.ok)
        self.assertIn("结果不明", result.message)
        with self.assertRaises(guard.SubmissionRefused) as cm:
            await adapter.submit(assignment, [answer])
        self.assertEqual(cm.exception.reason, guard.REFUSAL_ALREADY_SENT)


class TestQuestionGuardRules(unittest.TestCase):
    """规则二的逐条单测：直接打在 ``guard.check_questions`` 上。

    为什么单测这层：``PlanBuilder`` 会把这类题目整份作业标成待人工，于是
    ``check_reviewed`` 先一步拒绝；``check_questions`` 是**第二道**防线 ——
    万一有别的代码路径造出一份「声称可提交但内容有问题」的计划，它必须拦住。
    """

    def _planned(self, question: Question) -> plan_mod.PincoAssignmentPlan:
        return plan_mod.PincoAssignmentPlan(
            platform="pinco",
            assignment_id="hw-1",
            title="t",
            state=PlanState.PLANNED,
            questions=[
                plan_mod.PincoQuestionPlan(
                    index=0,
                    stem=question.stem,
                    type=question.type,
                    options=list(question.options),
                    remote_id=question.remote_id,
                    image_required=question_map.is_image_question(question),
                )
            ],
        )

    def test_image_question_without_answer_is_refused(self) -> None:
        question = question_map.mark_image(Question(stem="上传图", remote_id="q1"))
        with self.assertRaises(guard.SubmissionRefused) as cm:
            guard.check_questions(
                self._planned(question),
                [Answer(question=question, source=AnswerSource.UNKNOWN)],
                min_confidence=0.75,
            )
        self.assertEqual(cm.exception.reason, guard.REFUSAL_HUMAN_QUESTIONS)

    def test_image_question_answered_by_text_is_refused(self) -> None:
        """有人给图片题填了文字答案 —— 守卫必须拦住这种「糊弄」。"""
        question = question_map.mark_image(Question(stem="上传图", remote_id="q1"))
        guessed = Answer(
            question=question, text="我的作答", source=AnswerSource.MANUAL, confidence=1.0
        )
        with self.assertRaises(guard.SubmissionRefused) as cm:
            guard.check_questions(
                self._planned(question), [guessed], min_confidence=0.75
            )
        self.assertEqual(cm.exception.reason, guard.REFUSAL_IMAGE_GUESSED)

    def test_low_confidence_answer_is_refused(self) -> None:
        question = Question(stem="题", options=["甲"], remote_id="q1")
        weak = Answer(
            question=question, choices=[0], source=AnswerSource.MANUAL, confidence=0.4
        )
        with self.assertRaises(guard.SubmissionRefused) as cm:
            guard.check_questions(self._planned(question), [weak], min_confidence=0.75)
        self.assertEqual(cm.exception.reason, guard.REFUSAL_LOW_CONFIDENCE)

    def test_heuristic_answer_is_refused_even_with_high_confidence(self) -> None:
        """启发式答案不算「真答出来」，置信度再高也不许自动提交。"""
        question = Question(stem="题", options=["甲"], remote_id="q1")
        guess = Answer(
            question=question, choices=[0], source=AnswerSource.HEURISTIC, confidence=0.99
        )
        with self.assertRaises(guard.SubmissionRefused) as cm:
            guard.check_questions(self._planned(question), [guess], min_confidence=0.75)
        self.assertEqual(cm.exception.reason, guard.REFUSAL_LOW_CONFIDENCE)
        self.assertIn("启发式", cm.exception.message)

    def test_missing_answer_is_refused(self) -> None:
        question = Question(stem="题", options=["甲"], remote_id="q1")
        with self.assertRaises(guard.SubmissionRefused) as cm:
            guard.check_questions(self._planned(question), [], min_confidence=0.75)
        self.assertEqual(cm.exception.reason, guard.REFUSAL_HUMAN_QUESTIONS)

    def test_clean_plan_passes(self) -> None:
        question = Question(stem="题", options=["甲"], remote_id="q1")
        good = Answer(
            question=question, choices=[0], source=AnswerSource.BANK_HIT, confidence=1.0
        )
        guard.check_questions(self._planned(question), [good], min_confidence=0.75)


# --------------------------------------------------------------------------
# 6. 适配器离线端到端
# --------------------------------------------------------------------------


class TestAdapterOffline(unittest.IsolatedAsyncioTestCase):
    async def test_read_requires_session(self) -> None:
        adapter, _, _, _ = make_adapter(login=False)
        with self.assertRaises(PincoNotLoggedIn):
            await adapter.list_courses()

    async def test_login_without_credentials_raises(self) -> None:
        adapter, _, _, _ = make_adapter(login=False)
        with self.assertRaises(PincoError) as cm:
            await adapter.login(Credentials(username="u"))
        message = str(cm.exception)
        self.assertIn("KEDAIBIAO_PINCO_USER", message)
        self.assertIn("KEDAIBIAO_PINCO_TOKEN", message)

    async def test_login_adopts_session(self) -> None:
        adapter, _, _, _ = make_adapter(login=False)
        self.assertFalse(await adapter.is_logged_in())
        self.assertTrue(
            await adapter.login(Credentials(username="u", extra={"token": "Bearer x"}))
        )
        self.assertTrue(await adapter.is_logged_in())
        self.assertNotIn("Bearer x", repr(adapter.session), "令牌不能出现在 repr 里")

    async def test_list_courses_and_assignments(self) -> None:
        adapter, transport, _, _ = make_adapter()
        courses = await adapter.list_courses()
        assignments = await adapter.list_assignments(courses[0])
        self.assertEqual([c.remote_id for c in courses], ["1326193414086569984"])
        self.assertEqual(
            [a.remote_id for a in assignments], [SUBMITTED_HW, OPEN_HW, EXPIRED_HW]
        )
        self.assertFalse(transport.wrote_anything())

    async def test_list_courses_paginates(self) -> None:
        """实测：服务端 pageSize 封顶 20，第二页要接着取，否则漏课。"""
        page1 = [dict(COURSE_ITEMS[0], courseId=f"c-{i}") for i in range(20)]
        page2 = [dict(COURSE_ITEMS[1], courseId="c-tail")]
        adapter, transport, _, _ = make_adapter(
            {ep.ACTION_COURSE_WORKBENCH: Replies([{"content": page1}, {"content": page2}])}
        )
        courses = await adapter.list_courses()
        self.assertEqual(len(courses), 21)
        self.assertEqual(
            transport.action_names().count(ep.ACTION_COURSE_WORKBENCH), 2, "应该请求两页"
        )
        self.assertTrue(all("actionName" in call.url for call in transport.calls))

    async def test_list_courses_stops_when_pagination_ignored(self) -> None:
        """服务端若忽略分页参数、页页返回同一批，去重 + 兜底必须让它停下来。"""
        page = [dict(COURSE_ITEMS[0], courseId=f"c-{i}") for i in range(20)]
        adapter, transport, _, _ = make_adapter(
            {ep.ACTION_COURSE_WORKBENCH: Replies([{"content": page}])}
        )
        courses = await adapter.list_courses()
        self.assertEqual(len(courses), 20)
        self.assertEqual(len(transport.calls), 2, "第二页没有新条目就应当停下")

    async def test_list_classes_uses_measured_action(self) -> None:
        adapter, transport, _, _ = make_adapter()
        classes = await adapter.list_classes()
        self.assertEqual([c.class_id for c in classes], ["2001", "2002"])
        self.assertEqual(transport.action_names(), [ep.ACTION_CLASS_LIST])

    async def test_list_learning_tasks_maps_learning_ratio_to_progress(self) -> None:
        """学习任务 = 课程材料，``learningRatio`` 直接映射成 progress。"""
        adapter, transport, _, _ = make_adapter()
        tasks = await adapter.list_learning_tasks(Course(remote_id="c-1", name="示例课程 B"))
        self.assertEqual([t.remote_id for t in tasks], [m["materialId"] for m in MATERIAL_ITEMS])
        self.assertTrue(all(t.kind is TaskKind.COURSE for t in tasks))
        self.assertEqual([t.progress for t in tasks], [0.0, 0.5, 1.0])
        self.assertFalse(tasks[1].finished)
        self.assertTrue(tasks[2].finished, "学到 100% 视为完成")
        self.assertEqual(tasks[0].course_name, "示例课程 B")
        self.assertIn(ep.ACTION_MATERIAL_LIST, transport.action_names())
        self.assertFalse(transport.wrote_anything())

    async def test_fetch_material_detail_returns_download_url(self) -> None:
        adapter, transport, _, _ = make_adapter()
        detail = await adapter.fetch_material_detail(
            Course(remote_id="c-1", name="示例课程 B"), MATERIAL_ID
        )
        self.assertEqual(detail.material_id, MATERIAL_ID)
        self.assertTrue(detail.downloadable)
        self.assertIn("/server-main/api/resource/", detail.download_url)
        self.assertIn(ep.ACTION_MATERIAL_DETAIL, transport.action_names())

    async def test_fetch_questions_for_homework_only(self) -> None:
        adapter, transport, _, _ = make_adapter()
        homework = Assignment(remote_id=OPEN_HW, title="作业", kind=TaskKind.HOMEWORK)
        self.assertEqual(len(await adapter.fetch_questions(homework)), 2)
        self.assertIn(ep.ACTION_HOMEWORK_QUESTIONS, transport.action_names())

    async def test_fetch_questions_on_material_raises(self) -> None:
        """材料不是题：报错而不是返回空列表（空列表会被读成「没有题目」）。"""
        adapter, transport, _, _ = make_adapter()
        material = Assignment(remote_id=MATERIAL_ID, title="课件", kind=TaskKind.COURSE)
        with self.assertRaises(NotSupportedError) as cm:
            await adapter.fetch_questions(material)
        self.assertIn("学习任务", str(cm.exception))
        self.assertEqual(transport.calls, [], "不该为材料发任何请求")

    async def test_fetch_questions_backfills_deadline(self) -> None:
        """列表接口没有 deadline，题目响应里有 —— 顺手补上，省一次详情请求。"""
        adapter, _, _, _ = make_adapter()
        homework = Assignment(remote_id=OPEN_HW, title="作业", kind=TaskKind.HOMEWORK)
        self.assertIsNone(homework.deadline)
        await adapter.fetch_questions(homework)
        self.assertIsNotNone(homework.deadline)

    async def test_plan_never_touches_submit_actions(self) -> None:
        """核心承诺：抓取到计划这一步，一个写操作都不许发生。"""
        # 题库里预置真实样本里的两道题（题干与 remote_id 逐字一致），
        # 这样「可作答」的那份作业才会走到 PLANNED。
        bank = QuestionBank(":memory:")
        self.addCleanup(bank.close)
        for question in parsing.parse_questions(PLAIN_QUESTIONS):
            bank.upsert(
                question,
                Answer(
                    question=question,
                    choices=[0] if question.options else [],
                    text="" if question.options else "示例作答",
                    source=AnswerSource.BANK_HIT,
                    confidence=1.0,
                ),
            )
        adapter, transport, _, _ = make_adapter(bank=bank)

        plan = await adapter.plan()

        self.assertFalse(transport.wrote_anything())
        self.assertTrue(all(SUBMIT_ACTION_MARKER not in c.url for c in transport.calls))
        self.assertTrue(plan.digest)
        self.assertEqual(plan.platform, "pinco")
        states = {a.assignment_id: a.state for a in plan.assignments}
        self.assertIs(states[OPEN_HW], PlanState.PLANNED)
        self.assertIs(states[SUBMITTED_HW], PlanState.SKIPPED)
        self.assertIs(states[EXPIRED_HW], PlanState.SKIPPED)
        # 学习任务也在计划里，但只列进度、不进可提交集合
        self.assertIs(states[MATERIAL_ID], PlanState.SKIPPED)
        self.assertIn("学习进度", plan.render_text())

    async def test_plan_marks_image_homework_needs_human_end_to_end(self) -> None:
        adapter, transport, _, _ = make_adapter(
            {ep.ACTION_HOMEWORK_QUESTIONS: IMAGE_QUESTIONS}
        )
        plan = await adapter.plan()
        item = plan.find(OPEN_HW)
        self.assertIsNotNone(item)
        assert item is not None
        self.assertIs(item.state, PlanState.NEEDS_HUMAN)
        self.assertTrue(item.questions[0].image_required)
        self.assertIn("题干含真图片", item.questions[0].reason)
        self.assertFalse(transport.wrote_anything())
        self.assertIn("需要人工", plan.render_text())

    async def test_plan_skips_finished_homework_without_fetching(self) -> None:
        adapter, transport, _, _ = make_adapter(
            {ep.ACTION_HOMEWORK_LIST: {"content": [HOMEWORK_ITEMS[0]]}}
        )
        plan = await adapter.plan()
        item = plan.find(SUBMITTED_HW)
        assert item is not None
        self.assertIs(item.state, PlanState.SKIPPED)
        self.assertIn("已提交", item.reason)
        self.assertNotIn(ep.ACTION_HOMEWORK_QUESTIONS, transport.action_names())

    async def test_plan_skips_expired_homework(self) -> None:
        """实测组合：submitted=false 但 expired=true / canSubmitted=false。"""
        adapter, transport, _, _ = make_adapter(
            {ep.ACTION_HOMEWORK_LIST: {"content": [HOMEWORK_ITEMS[2]]}}
        )
        plan = await adapter.plan()
        item = plan.find(EXPIRED_HW)
        assert item is not None
        self.assertIs(item.state, PlanState.SKIPPED)
        self.assertIn("过期", item.reason)
        self.assertNotIn(ep.ACTION_HOMEWORK_QUESTIONS, transport.action_names())

    async def test_plan_respects_kind_filter(self) -> None:
        adapter, _, _, _ = make_adapter()
        plan = await adapter.plan(kinds=frozenset({TaskKind.COURSE}))
        self.assertEqual(
            [a.assignment_id for a in plan.assignments],
            [m["materialId"] for m in MATERIAL_ITEMS],
        )

    async def test_plan_never_marks_material_submittable(self) -> None:
        """学习任务永远不可提交：没有题目、没有票据、也拿不到提交入口。"""
        adapter, _, _, _ = make_adapter()
        plan = await adapter.plan()
        for material in MATERIAL_ITEMS:
            item = plan.find(material["materialId"])
            assert item is not None
            self.assertEqual(item.questions, [], "材料不产生题目，也就不产生答案")
            self.assertIs(item.state, PlanState.SKIPPED)
        reviewed = adapter.confirm_plan(plan)
        for material in MATERIAL_ITEMS:
            self.assertIsNone(reviewed.digest_for(material["materialId"]))

    async def test_plan_respects_max_assignments(self) -> None:
        adapter, _, _, _ = make_adapter()
        plan = await adapter.plan(max_assignments=1)
        self.assertEqual(len(plan.assignments), 1)

    async def test_confirm_plan_returns_reviewed_token(self) -> None:
        adapter, _, _, _ = make_adapter()
        plan = await adapter.plan()
        reviewed = adapter.confirm_plan(plan)
        self.assertEqual(reviewed.digest, plan.digest)
        self.assertEqual(reviewed.platform, "pinco")

    async def test_confirm_plan_rejects_unsealed_plan(self) -> None:
        adapter, _, _, _ = make_adapter()
        with self.assertRaises(guard.SubmissionRefused) as cm:
            adapter.confirm_plan(plan_mod.PincoPlan())
        self.assertEqual(cm.exception.reason, guard.REFUSAL_HASH_MISMATCH)


# --------------------------------------------------------------------------
# 7. SSO 密码登录（Lead 实测跑通的算法）
# --------------------------------------------------------------------------

#: 测试用 RSA 密钥（512 bit，e=65537）。私钥只在断言里用来解密，
#: 不参与任何真实链路 —— 别把它当成产品里的密钥。
TEST_RSA_N = int(
    "7327164187276590722698478793193672957510002838877242962674759228"
    "9668416297206750757981902889017067531080456706068992912572997202"
    "41111830994257707971935293"
)
TEST_RSA_D = int(
    "4015814127024243742749998623262028151438146268056696064456943051"
    "1811646016438161412247087447464947069669625204858245576585086026"
    "74660247857851214962010693"
)
TEST_RSA_E = 65537
TEST_KEY_BYTES = 64


def _der_length(length: int) -> bytes:
    if length < 0x80:
        return bytes([length])
    raw = length.to_bytes((length.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(raw)]) + raw


def _der_tlv(tag: int, value: bytes) -> bytes:
    return bytes([tag]) + _der_length(len(value)) + value


def _der_integer(value: int) -> bytes:
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    if raw[0] >= 0x80:  # DER 正整数：高位置 1 时必须补前导 0x00
        raw = b"\x00" + raw
    return _der_tlv(0x02, raw)


def spki_der(n: int = TEST_RSA_N, e: int = TEST_RSA_E) -> bytes:
    """按真实结构拼一个 RSA SPKI（用来喂给适配器的 DER 解析器）。"""
    rsa_key = _der_tlv(0x30, _der_integer(n) + _der_integer(e))
    bit_string = _der_tlv(0x03, b"\x00" + rsa_key)
    algorithm = _der_tlv(
        0x30, _der_tlv(0x06, bytes.fromhex("2a864886f70d010101")) + _der_tlv(0x05, b"")
    )
    return _der_tlv(0x30, algorithm + bit_string)


def rsa_decrypt_em(cipher: bytes) -> bytes:
    """用测试私钥解密，返回完整的 PKCS#1 v1.5 编码块（EM）。"""
    return pow(int.from_bytes(cipher, "big"), TEST_RSA_D, TEST_RSA_N).to_bytes(
        TEST_KEY_BYTES, "big"
    )


def rsa_decrypt(cipher: bytes) -> bytes:
    """解密并去掉 PKCS#1 v1.5 填充，返回明文。"""
    em = rsa_decrypt_em(cipher)
    assert em[:2] == b"\x00\x02"
    return em[em.index(b"\x00", 2) + 1 :]


SSO_PUBLIC_KEY_B64 = base64.b64encode(spki_der()).decode("ascii")
SSO_TOKEN = "sso-token-1"
SSO_SUCCESS: dict[str, Any] = {
    "data": {
        "statusCode": 200,
        "data": {"user": {"nickName": "小明"}, "token": SSO_TOKEN},
    }
}


def sso_routes(**overrides: Any) -> dict[str, Any]:
    routes: dict[str, Any] = {
        ep.SSO_PUBLIC_KEY_PATH: FakeReply({"data": SSO_PUBLIC_KEY_B64}),
        ep.SSO_LOGIN_PATH: FakeReply(SSO_SUCCESS),
        ep.SSO_SET_TOKEN_PATH: FakeReply({"message": "设置token成功"}),
    }
    routes.update(overrides)
    return routes


def form_of(call: RecordedCall) -> dict[str, str]:
    """把记录下来的表单请求体还原成字典（保留空值，``captcha=""`` 也要在）。"""
    assert call.body is not None
    return {
        key: values[0]
        for key, values in urllib.parse.parse_qs(
            call.body.decode("utf-8"), keep_blank_values=True
        ).items()
    }


class TestCrypto(unittest.TestCase):
    def test_der_integer_with_leading_zero(self) -> None:
        """样本必须覆盖「DER 里 n 带前导 0x00」这个坑，否则测不到那条回归。"""
        raw = TEST_RSA_N.to_bytes(TEST_KEY_BYTES, "big")
        self.assertGreaterEqual(raw[0], 0x80, "测试密钥的最高位必须是 1")
        modulus, exponent = crypto.parse_rsa_public_key(spki_der())
        self.assertEqual((modulus, exponent), (TEST_RSA_N, TEST_RSA_E))

    def test_key_size_uses_bit_length_not_der_byte_length(self) -> None:
        """按 DER 字节长度算会多一个字节，服务端回 500403（Lead 实测踩过）。"""
        der_integer_bytes = (TEST_RSA_N.bit_length() + 8) // 8
        self.assertEqual(der_integer_bytes, TEST_KEY_BYTES + 1)
        self.assertEqual(crypto.public_key_size_bytes(TEST_RSA_N), TEST_KEY_BYTES)

    def test_decode_base64_public_key(self) -> None:
        modulus, exponent = crypto.decode_public_key(SSO_PUBLIC_KEY_B64)
        self.assertEqual((modulus, exponent), (TEST_RSA_N, TEST_RSA_E))

    def test_pkcs1v15_structure(self) -> None:
        message = b"hello-pinco"
        cipher = crypto.encrypt_pkcs1v15(message, TEST_RSA_N, TEST_RSA_E)
        self.assertEqual(len(cipher), TEST_KEY_BYTES)
        em = rsa_decrypt_em(cipher)
        self.assertEqual(em[:2], b"\x00\x02")
        separator = em.index(b"\x00", 2)
        self.assertGreaterEqual(separator - 2, 8, "PS 至少 8 字节")
        self.assertTrue(all(byte != 0 for byte in em[2:separator]), "PS 必须全非零")
        self.assertEqual(em[separator + 1 :], message)

    def test_padding_is_randomized(self) -> None:
        first = crypto.encrypt_pkcs1v15(b"same", TEST_RSA_N, TEST_RSA_E)
        second = crypto.encrypt_pkcs1v15(b"same", TEST_RSA_N, TEST_RSA_E)
        self.assertNotEqual(first, second, "PS 随机，否则相同密码的密文可被直接比对")

    def test_credential_fields_match_measured_protocol(self) -> None:
        """username 加密明文；password 加密 MD5 十六进制摘要。"""
        encrypted_username, encrypted_password = crypto.encrypt_credentials(
            "stu", "pw123", TEST_RSA_N, TEST_RSA_E
        )
        self.assertEqual(rsa_decrypt(base64.b64decode(encrypted_username)), b"stu")
        self.assertEqual(
            rsa_decrypt(base64.b64decode(encrypted_password)),
            hashlib.md5(b"pw123").hexdigest().encode(),
        )

    def test_oversized_message_rejected(self) -> None:
        with self.assertRaises(PincoProtocolError):
            crypto.encrypt_pkcs1v15(b"x" * 60, TEST_RSA_N, TEST_RSA_E)

    def test_der_without_integers_rejected(self) -> None:
        with self.assertRaises(PincoProtocolError):
            crypto.parse_rsa_public_key(_der_tlv(0x30, _der_tlv(0x06, b"\x2a")))

    def test_bad_base64_rejected(self) -> None:
        with self.assertRaises(PincoProtocolError):
            crypto.decode_public_key("!!!not-base64!!!")


class TestSsoLogin(unittest.IsolatedAsyncioTestCase):
    async def test_full_flow_matches_measured_form(self) -> None:
        adapter, transport, _, _ = make_adapter(sso_routes(), login=False)
        self.assertTrue(
            await adapter.login(Credentials(username="stu", password="pw123"))
        )
        self.assertTrue(await adapter.is_logged_in())
        paths = [urllib.parse.urlparse(call.url).path for call in transport.calls]
        self.assertEqual(
            paths,
            [ep.SSO_PUBLIC_KEY_PATH, ep.SSO_LOGIN_PATH, ep.SSO_SET_TOKEN_PATH],
        )

        key_form = form_of(transport.calls[0])
        self.assertTrue(key_form[ep.FIELD_SSO_DATA_ID])
        self.assertEqual(key_form[ep.FIELD_SSO_IS_FORCE_SYSTEM], "true")
        self.assertEqual(key_form[ep.FIELD_SSO_IS_IFRAME], "1")
        self.assertEqual(key_form[ep.FIELD_SSO_SYSTEM], ep.SSO_SYSTEM)

        login_form = form_of(transport.calls[1])
        self.assertEqual(login_form[ep.FIELD_SSO_DATA_ID], key_form[ep.FIELD_SSO_DATA_ID])
        self.assertEqual(login_form[ep.FIELD_SSO_CAPTCHA], "")
        self.assertEqual(login_form[ep.FIELD_SSO_CLIENT_TYPE], "pc")
        self.assertEqual(login_form[ep.FIELD_SSO_PHONE_COUNTRY_CODE], "86")
        self.assertLessEqual(
            int(login_form[ep.FIELD_SSO_RST]), int(login_form[ep.FIELD_SSO_LST])
        )
        self.assertEqual(
            rsa_decrypt(base64.b64decode(login_form[ep.FIELD_SSO_USERNAME])), b"stu"
        )
        self.assertEqual(
            rsa_decrypt(base64.b64decode(login_form[ep.FIELD_SSO_PASSWORD])),
            hashlib.md5(b"pw123").hexdigest().encode(),
        )
        self.assertNotIn(b"pw123", transport.calls[1].body or b"", "明文密码绝不能上网")

        self.assertEqual(form_of(transport.calls[2])[ep.FIELD_SSO_TOKEN], SSO_TOKEN)
        self.assertFalse(transport.wrote_anything(), "SSO 登录不该碰应用层提交接口")

    async def test_sso_token_stays_out_of_repr(self) -> None:
        adapter, _, _, _ = make_adapter(sso_routes(), login=False)
        await adapter.login(Credentials(username="stu", password="pw123"))
        self.assertNotIn(SSO_TOKEN, repr(adapter.session))

    async def test_business_failure_reports_platform_code(self) -> None:
        routes = sso_routes(
            **{
                ep.SSO_LOGIN_PATH: FakeReply(
                    {"data": {"statusCode": 500403, "message": "登录加解密验证失败"}}
                )
            }
        )
        adapter, _, _, _ = make_adapter(routes, login=False)
        with self.assertRaises(PincoApiError) as cm:
            await adapter.login(Credentials(username="stu", password="pw123"))
        self.assertEqual(cm.exception.code, 500403)
        self.assertIn("加解密", str(cm.exception))

    async def test_response_without_token_raises_protocol_error(self) -> None:
        routes = sso_routes(
            **{
                ep.SSO_LOGIN_PATH: FakeReply(
                    {"data": {"statusCode": 200, "data": {"user": {"nickName": "x"}}}}
                )
            }
        )
        adapter, _, _, _ = make_adapter(routes, login=False)
        with self.assertRaises(PincoProtocolError):
            await adapter.login(Credentials(username="stu", password="pw123"))

    async def test_missing_public_key_raises(self) -> None:
        routes = sso_routes(**{ep.SSO_PUBLIC_KEY_PATH: FakeReply({"data": {}})})
        adapter, _, _, _ = make_adapter(routes, login=False)
        with self.assertRaises(PincoProtocolError):
            await adapter.login(Credentials(username="stu", password="pw123"))


# --------------------------------------------------------------------------
# 8. 协议选择与双层判错
# --------------------------------------------------------------------------


class TestProtocolSelection(unittest.IsolatedAsyncioTestCase):
    async def test_rest_protocol_requires_verified_mapping(self) -> None:
        """没有核对过的 Web→REST 映射时必须报错 —— 猜路径会打到别的接口。"""
        adapter, transport, _, _ = make_adapter()
        with self.assertRaises(PincoEndpointUnavailable):
            await adapter.client().call(
                ep.ACTION_HOMEWORK_LIST_WEB, {}, protocol=ep.Protocol.REST
            )
        self.assertEqual(transport.calls, [], "没有映射就不该发出任何请求")

    async def test_rest_protocol_uses_mapping_once_verified(self) -> None:
        rest_path = "/student/homework/list/web"
        adapter, transport, _, _ = make_adapter(
            {f"{ep.REST_API_PREFIX}{rest_path}": {"list": []}}
        )
        ep.REST_ACTION_PATHS[ep.ACTION_HOMEWORK_LIST_WEB] = rest_path
        self.addCleanup(ep.REST_ACTION_PATHS.pop, ep.ACTION_HOMEWORK_LIST_WEB, None)

        data = await adapter.client().call(
            ep.ACTION_HOMEWORK_LIST_WEB, {}, protocol=ep.Protocol.REST
        )
        self.assertEqual(data, {"list": []})
        self.assertIn(f"{ep.REST_API_PREFIX}{rest_path}", transport.calls[0].url)
        self.assertNotIn("actionName", transport.calls[0].url)

    async def test_anonymous_current_time_over_rest(self) -> None:
        adapter, transport, _, _ = make_adapter(
            {ep.REST_PATH_CURRENT_TIME: {"currentTime": 1760000000000}}
        )
        data = await adapter.client().current_time()
        self.assertEqual(data["currentTime"], 1760000000000)
        self.assertEqual(transport.calls[0].method, "GET")
        self.assertIn(ep.REST_API_PREFIX, transport.calls[0].url)

    async def test_csrf_header_uses_cookie_when_jar_has_it(self) -> None:
        class JarCookie:
            name = ep.CSRF_COOKIE
            value = "from-jar"

        adapter, transport, _, _ = make_adapter()
        adapter.adopt_session(authorization="Bearer t")
        adapter.ctx.http.transport.cookies = [JarCookie()]  # type: ignore[attr-defined]
        await adapter.client().post_action(ep.ACTION_COURSE_WORKBENCH, {})
        self.assertEqual(transport.calls[0].headers[ep.HEADER_CSRF], "from-jar")


class TestDoubleLayerErrors(unittest.IsolatedAsyncioTestCase):
    """HTTP 200 里可以套业务 403（实测形态）—— 只看 Response.ok 会漏掉。"""

    async def test_business_403_inside_http_200(self) -> None:
        adapter, _, _, _ = make_adapter(
            {
                ep.ACTION_COURSE_WORKBENCH: FakeReply(
                    {"code": 403, "msg": "access forbidden", "statusCode": 403}
                )
            }
        )
        with self.assertRaises(PincoNotLoggedIn):
            await adapter.client().post_action(ep.ACTION_COURSE_WORKBENCH, {})

    async def test_rest_style_code_403_is_not_logged_in(self) -> None:
        """REST 家族的 ``{"code":403,"msg":"access forbidden"}``（无 statusCode）。"""
        adapter, _, _, _ = make_adapter(
            {ep.ACTION_COURSE_WORKBENCH: FakeReply({"code": 403, "msg": "access forbidden"})}
        )
        with self.assertRaises(PincoNotLoggedIn):
            await adapter.client().post_action(ep.ACTION_COURSE_WORKBENCH, {})

    async def test_http_500_with_json_body(self) -> None:
        adapter, _, _, _ = make_adapter(
            {ep.ACTION_COURSE_WORKBENCH: FakeReply({"code": 500, "msg": "内部错误"}, status=500)}
        )
        with self.assertRaises(PincoApiError):
            await adapter.client().post_action(ep.ACTION_COURSE_WORKBENCH, {})

    async def test_non_json_body_is_protocol_error(self) -> None:
        adapter, _, _, _ = make_adapter(
            {ep.ACTION_COURSE_WORKBENCH: FakeReply(raw=b"<html>login</html>", status=200)}
        )
        with self.assertRaises(PincoProtocolError) as cm:
            await adapter.client().post_action(ep.ACTION_COURSE_WORKBENCH, {})
        self.assertIn("不是 JSON", str(cm.exception))


# --------------------------------------------------------------------------
# 9. 富文本与图片题识别（实测 HTML 形态）
# --------------------------------------------------------------------------


class TestRichtext(unittest.TestCase):
    def test_strip_html_keeps_text_and_breaks_paragraphs(self) -> None:
        text = richtext.strip_html("<p>第一段</p><p>第二段<br/>换行</p>")
        self.assertIn("第一段", text)
        self.assertIn("第二段", text)
        self.assertNotIn("<", text)

    def test_strip_html_handles_entities_and_nbsp(self) -> None:
        self.assertEqual(richtext.strip_html("<p>a&nbsp;b &amp; c</p>"), "a b & c")

    def test_strip_html_passes_plain_text_through(self) -> None:
        self.assertEqual(richtext.strip_html("  纯文本题干  "), "纯文本题干")

    def test_strip_html_survives_broken_markup(self) -> None:
        self.assertIn("未闭合", richtext.strip_html("<p>未闭合<div><span>"))

    def test_image_kind(self) -> None:
        self.assertEqual(richtext.image_kind('<p><img class="rich-img" src="x"/></p>'), "image")
        self.assertEqual(
            richtext.image_kind('<p><img class="rich-formula" src="x"/></p>'), "formula"
        )
        self.assertEqual(richtext.image_kind("<p>没有图</p>"), "")
        self.assertEqual(richtext.image_kind(""), "")


class TestImageDetectionFromRealSample(unittest.TestCase):
    """图片判定按真实样本的四种形态各锁一条。

    这些题目**必须**进人工：题干在图上、公式在图上、答案要传附件。
    """

    def setUp(self) -> None:
        self.questions = parsing.parse_questions(IMAGE_QUESTIONS)

    def test_stem_rich_image_is_human(self) -> None:
        circuit = self.questions[0]
        self.assertTrue(question_map.is_image_question(circuit))
        self.assertIn("题干含真图片", question_map.human_reason(circuit))
        self.assertIn("节点电压法", circuit.stem, "题干文字仍要抽出来给人看")

    def test_stem_rich_formula_is_human(self) -> None:
        formula = self.questions[1]
        self.assertTrue(question_map.is_image_question(formula))
        self.assertIn("公式", question_map.human_reason(formula))

    def test_answer_attachment_slot_is_human(self) -> None:
        upload = self.questions[2]
        self.assertTrue(question_map.is_image_question(upload))
        self.assertIn("上传图片", question_map.human_reason(upload))

    def test_choice_question_with_stem_image_is_human(self) -> None:
        """选择题题干里有真图也要转人工 —— 选项文本救不了看不到的图。"""
        choice = self.questions[4]
        self.assertTrue(choice.options, "它确实是选择题")
        self.assertTrue(question_map.is_image_question(choice))

    def test_plain_questions_are_not_human(self) -> None:
        for question in parsing.parse_questions(PLAIN_QUESTIONS):
            self.assertFalse(question_map.is_image_question(question))


class TestHumanReasonCodes(unittest.TestCase):
    """三类「需要人工」的理由码：分开、互不吞并。

    | 码 | 含义 | 谁来处理 |
    | --- | --- | --- |
    | ``answer_requires_image`` | 答案本身要人上传/拍照/作图 | **用户必须亲自做** |
    | ``stem_has_image`` | 题干含真图片（rich-img） | 需要看图 |
    | ``stem_has_formula_image`` | 题干含公式图（rich-formula） | 需要看图 |
    """

    def setUp(self) -> None:
        self.questions = parsing.parse_questions(IMAGE_QUESTIONS)

    def codes_of(self, index: int) -> list[str]:
        return question_map.reason_codes(self.questions[index])

    def test_answer_requires_image_is_its_own_code(self) -> None:
        """实测「请上传你的实验接线照片」—— 这才是用户说的「图片输入题」。"""
        upload = self.questions[2]
        self.assertEqual(self.codes_of(2), [question_map.REASON_ANSWER_REQUIRES_IMAGE])
        self.assertIn("上传", question_map.REASON_TEXTS[question_map.REASON_ANSWER_REQUIRES_IMAGE])
        self.assertFalse(upload.options, "它不是选择题，是主观题")

    def test_stem_has_image_is_its_own_code(self) -> None:
        self.assertEqual(self.codes_of(0), [question_map.REASON_STEM_HAS_IMAGE])

    def test_stem_has_formula_image_is_its_own_code(self) -> None:
        self.assertEqual(self.codes_of(1), [question_map.REASON_STEM_HAS_FORMULA_IMAGE])

    def test_choice_question_stem_image_uses_the_same_stem_code(self) -> None:
        self.assertEqual(self.codes_of(4), [question_map.REASON_STEM_HAS_IMAGE])

    def test_codes_do_not_swallow_each_other(self) -> None:
        """题干既有真图、答案又要人上传时必须两条都报出来。"""
        item = {
            "questionId": "q-mixed",
            "content": '<p>请上传你的实验接线照片</p><p><img class="rich-img" src="x"/></p>',
            "answer": {"attachments": []},
        }
        codes = [reason.code for reason in question_map.human_reasons(item)]
        self.assertEqual(
            codes,
            [
                question_map.REASON_ANSWER_REQUIRES_IMAGE,
                question_map.REASON_STEM_HAS_IMAGE,
            ],
        )

    def test_formula_is_not_reported_as_stem_image(self) -> None:
        """公式图不能被当成真图片吞掉 —— 用户看的是两类不同的东西。"""
        codes = self.codes_of(1)
        self.assertNotIn(question_map.REASON_STEM_HAS_IMAGE, codes)

    def test_answer_side_judgement_needs_text_or_shape_not_just_attachments(self) -> None:
        """答案槽有附件不等于「这题要你传图」：那可能是学生自己已经传过。

        所以只认「题干文案/题型说要作图上传」；选择题里的附件一律不当信号。
        """
        choice_with_attachment = {
            "questionId": "q1",
            "content": "<p>下列哪项正确？</p>",
            "optionItems": [{"option": "A", "content": "甲"}, {"option": "B", "content": "乙"}],
            "answer": {"attachments": [{"fileName": "student-uploaded.png"}]},
        }
        self.assertFalse(question_map.image_required(choice_with_attachment, has_options=True))

    def test_answer_hint_words_are_explicit(self) -> None:
        """题干文案里的「上传/拍照/作图/手写」才算答案侧信号。"""
        for hint in ("请上传", "拍照提交", "作图说明", "手写作答"):
            item = {"questionId": "q", "content": f"<p>{hint}你的答案</p>"}
            self.assertEqual(
                [r.code for r in question_map.human_reasons(item)],
                [question_map.REASON_ANSWER_REQUIRES_IMAGE],
                hint,
            )

    def test_bare_picture_word_is_not_an_answer_signal(self) -> None:
        """光秃秃的「图片」不算：'下列哪项是图片格式' 不该被拖进人工。"""
        item = {"questionId": "q", "content": "<p>下列哪项属于图片格式？</p>",
                "optionItems": [{"option": "A", "content": "PNG"}]}
        self.assertFalse(question_map.image_required(item, has_options=True))

    def test_mark_and_reason_roundtrip(self) -> None:
        question = question_map.mark_image(Question(stem="上传图", remote_id="q9"))
        self.assertEqual(
            question_map.reason_codes(question), [question_map.REASON_ANSWER_REQUIRES_IMAGE]
        )
        self.assertIn("图片", question_map.human_reason(question))

    def test_legacy_marker_without_codes_falls_back_conservatively(self) -> None:
        """只有布尔位、没有码的老标记：按最保守的一类兜底。"""
        question = Question(stem="老标记", remote_id="q-old")
        question.raw[question_map.IMAGE_FLAG] = True
        self.assertEqual(
            question_map.reason_codes(question), [question_map.REASON_ANSWER_REQUIRES_IMAGE]
        )


class TestPlanListsHumanReasonsSeparately(unittest.IsolatedAsyncioTestCase):
    """计划产物必须把三类分开列，用户一眼能分清「必须我做」与「要我看图」。"""

    async def _plan(self):
        assignment = Assignment(remote_id="hw-img", title="电路阶段性测试")
        questions = parsing.parse_questions(IMAGE_QUESTIONS)
        builder = solving.PlanBuilder(answerer=_FixedAnswerer(None))
        return await builder.build([(assignment, questions)])

    async def test_counts_are_split_by_code(self) -> None:
        plan = await self._plan()
        self.assertEqual(
            plan.human_reason_counts(),
            {
                question_map.REASON_ANSWER_REQUIRES_IMAGE: 1,
                question_map.REASON_STEM_HAS_IMAGE: 2,
                question_map.REASON_STEM_HAS_FORMULA_IMAGE: 1,
            },
        )

    async def test_render_text_lists_the_three_categories(self) -> None:
        plan = await self._plan()
        text = plan.render_text()
        for code in (
            question_map.REASON_ANSWER_REQUIRES_IMAGE,
            question_map.REASON_STEM_HAS_IMAGE,
            question_map.REASON_STEM_HAS_FORMULA_IMAGE,
        ):
            self.assertIn(code, text)
        self.assertIn("必须你亲自做", text)
        self.assertIn("需要看图", text)
        # 三类分开列，不是笼统一句「有图片题」
        self.assertIn(f"（{question_map.REASON_STEM_HAS_IMAGE}）：2 道", text)

    async def test_question_carries_codes_and_survives_json_roundtrip(self) -> None:
        plan = await self._plan()
        upload = plan.assignments[0].questions[2]
        self.assertEqual(upload.human_reasons, [question_map.REASON_ANSWER_REQUIRES_IMAGE])
        restored = plan_mod.PincoPlan.from_dict(json.loads(plan.json_text()))
        self.assertEqual(
            restored.assignments[0].questions[2].human_reasons,
            [question_map.REASON_ANSWER_REQUIRES_IMAGE],
        )
        self.assertEqual(restored.digest, plan.digest)

    async def test_formula_image_question_is_never_answered(self) -> None:
        """锁住「不收窄」的裁决：公式图题依然不生成答案、依然进人工。

        没有多模态答题器时，收窄判定只会让文本答题器在没看到题面的情况下硬答 = 猜。
        """
        plan = await self._plan()
        formula = plan.assignments[0].questions[1]
        self.assertTrue(formula.needs_human)
        self.assertFalse(formula.has_answer)
        self.assertIn("公式", formula.reason)


class TestDeadEndpoints(unittest.TestCase):
    """实测的死接口：登记为 DEAD，且解析路径里不许出现。"""

    def test_out_of_scope_is_not_confused_with_dead(self) -> None:
        """教师端是「范围外」，不是「死接口」—— 两类必须分开，各有原因。"""
        self.assertFalse(
            ep.DEAD_ACTIONS & ep.OUT_OF_SCOPE_ACTIONS,
            "同一个 action 不能既算死接口又算范围外",
        )
        self.assertIn(ep.ACTION_TEACHER_WORKBENCH, ep.OUT_OF_SCOPE_ACTIONS)
        self.assertNotIn(ep.ACTION_TEACHER_WORKBENCH, ep.DEAD_ACTIONS)
        for name in (
            "ACTION_TEACHER_WORKBENCH",
            "ACTION_CHAPTER_DIRECTORY_NODES",
        ):
            self.assertIs(ep.evidence_of(name), ep.Evidence.OUT_OF_SCOPE, name)

    def test_every_registered_bad_action_has_a_reason(self) -> None:
        """只留一个集合看不出去原因，所以每条都要有一句话说明。"""
        for name in dir(ep):
            value = getattr(ep, name)
            if not name.startswith("ACTION_") or not isinstance(value, str):
                continue
            if ep.evidence_of(name) in (ep.Evidence.DEAD, ep.Evidence.OUT_OF_SCOPE):
                self.assertTrue(ep.note_of(name), f"{name} 没有登记原因")
        self.assertIn("500", ep.note_of("ACTION_FAQ"))
        self.assertIn("教师端", ep.note_of("ACTION_TEACHER_WORKBENCH"))

    def test_platform_package_never_calls_out_of_scope_or_dead_actions(self) -> None:
        """范围锁定：包内代码（除常量表本身）不许引用教师端 / 死接口常量。"""
        package = pathlib.Path(parsing.__file__).parent
        offenders: list[str] = []
        for path in sorted(package.glob("*.py")):
            if path.name in {"endpoints.py", "__init__.py"}:
                continue
            source = path.read_text(encoding="utf-8")
            for name in dir(ep):
                value = getattr(ep, name)
                if not name.startswith("ACTION_") or not isinstance(value, str):
                    continue
                if value not in (ep.DEAD_ACTIONS | ep.OUT_OF_SCOPE_ACTIONS):
                    continue
                # 只看代码引用（``ep.ACTION_X``）；注释里提到名字不算调用。
                if f"ep.{name}" in source:
                    offenders.append(f"{path.name} 引用了 {name!r}")
        self.assertEqual(offenders, [], "包内代码不该调用死接口或教师端接口")

    def test_dead_actions_are_registered_as_dead(self) -> None:
        """死接口名单与证据等级必须一致（互相漏登记就等于没登记）。"""
        self.assertIs(ep.evidence_of("ACTION_CHAPTER_TASK_LIST"), ep.Evidence.DEAD)
        self.assertIs(ep.evidence_of("ACTION_FAQ"), ep.Evidence.DEAD)
        dead_names = {
            name
            for name in dir(ep)
            if name.startswith("ACTION_")
            and isinstance(getattr(ep, name), str)
            and ep.evidence_of(name) is ep.Evidence.DEAD
        }
        self.assertIn("ACTION_FAQ", dead_names)
        for name in dead_names:
            self.assertIn(
                getattr(ep, name),
                ep.DEAD_ACTIONS,
                f"{name} 标了 DEAD 但不在 DEAD_ACTIONS 里",
            )
        # 反向也要成立：写进 DEAD_ACTIONS 的 action 常量必须也标成 DEAD，
        # 否则「名单」和「证据等级」两处会各说各话。
        action_values = {
            getattr(ep, name)
            for name in dir(ep)
            if name.startswith("ACTION_") and isinstance(getattr(ep, name), str)
        }
        for action in ep.DEAD_ACTIONS & action_values:
            names = [
                name
                for name in dir(ep)
                if name.startswith("ACTION_") and getattr(ep, name) == action
            ]
            for name in names:
                self.assertIs(
                    ep.evidence_of(name), ep.Evidence.DEAD, f"{name} 在名单里却没标 DEAD"
                )

    def test_measured_read_actions_are_marked_measured(self) -> None:
        for name in (
            "ACTION_COURSE_WORKBENCH",
            "ACTION_HOMEWORK_LIST",
            "ACTION_HOMEWORK_DETAIL",
            "ACTION_HOMEWORK_QUESTIONS",
            "ACTION_CLASS_LIST",
            "ACTION_MATERIAL_LIST",
            "ACTION_MATERIAL_DETAIL",
        ):
            self.assertIs(ep.evidence_of(name), ep.Evidence.MEASURED, name)

    def test_submit_payload_still_unconfirmed(self) -> None:
        """提交报文结构仍未验证 —— 这道闸门不能因为读路径打通就放开。"""
        adapter, _, _, _ = make_adapter()
        self.assertFalse(PincoAdapter.submit_payload_confirmed)

    def test_page_size_uses_server_cap(self) -> None:
        self.assertEqual(ep.DEFAULT_PAGE_SIZE, 20, "服务端 pageSize 封顶 20")

    def test_adapter_never_calls_dead_actions(self) -> None:
        import asyncio

        async def scenario() -> list[str]:
            adapter, transport, _, _ = make_adapter()
            await adapter.list_courses()
            await adapter.list_assignments(Course(remote_id="c-1", name="示例课程 A"))
            await adapter.list_learning_tasks(Course(remote_id="c-1", name="示例课程 A"))
            await adapter.plan()
            return transport.action_names()

        called = asyncio.run(scenario())
        self.assertFalse(
            [name for name in called if name in ep.DEAD_ACTIONS],
            "死接口一个都不该被调用",
        )


class ReplayTransport:
    """把真实抓包的响应**原样回放**的传输层（录制回放）。

    与 :class:`FakeTransport` 的区别在于它更接近线上：

    1. 响应体直接读 fixture 文件的**原始字节**（不重新序列化），信封、字段、
       HTML 都与抓包一致；
    2. 每个请求都校验线上必需的请求头（缺 ``x-server`` 当场失败）；
    3. 记录请求的 (method, 路径, action, 关键参数)，供用例断言「到底打了哪些接口」；
    4. 没录到的接口直接抛错 —— 回放测试里出现「没录到的请求」本身就是发现，
       说明适配器打了计划外的接口。
    """

    def __init__(self, recordings: dict[str, str]) -> None:
        #: action（或 REST 路径）→ fixture 文件名
        self.recordings = recordings
        self.calls: list[RecordedCall] = []

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        timeout: float = 15.0,
    ) -> Response:
        call = RecordedCall(method=method, url=url, headers=dict(headers or {}), body=body)
        self.calls.append(call)
        if ep.BASE_URL in url:
            for header in (ep.HEADER_SERVER, ep.HEADER_TRACE_ID, ep.HEADER_LANGUAGE):
                if not call.headers.get(header):
                    raise AssertionError(f"回放请求缺少必需头 {header!r}：{url}")
            if call.headers[ep.HEADER_SERVER] != ep.SERVER_NAME:
                raise AssertionError(f"{ep.HEADER_SERVER} 值不对：{call.headers}")

        parsed = urllib.parse.urlparse(url)
        action = urllib.parse.parse_qs(parsed.query).get("actionName", [""])[0]
        key = action or parsed.path
        fixture = self.recordings.get(key)
        if fixture is None:
            raise AssertionError(f"回放里没有录制这条请求：{method} {url}")
        raw = (FIXTURES / fixture).read_bytes()
        return Response(
            status=200,
            url=url,
            body=raw,
            headers={"Content-Type": "application/json"},
        )

    def actions(self) -> list[str]:
        return [call.action for call in self.calls if call.action]

    def write_actions(self) -> list[str]:
        return [name for name in self.actions() if SUBMIT_ACTION_MARKER in name]


class TestReplayEndToEnd(unittest.IsolatedAsyncioTestCase):
    """录制回放：用真实抓包响应跑完整 plan()，覆盖请求头 / URL / action 这些层。

    这一层存在的理由就是那次线上事故：请求头错了，parser 单测照样全绿。
    回放比单测更接近真实链路 —— 请求从哪里出去、带什么头、打了哪些接口，都在这里断言。
    """

    RECORDINGS = {
        ep.ACTION_COURSE_WORKBENCH: "course_list.json",
        ep.ACTION_HOMEWORK_LIST: "homework_list.json",
        ep.ACTION_MATERIAL_LIST: "material_list.json",
        ep.ACTION_HOMEWORK_QUESTIONS: "homework_questions_plain.json",
    }

    def _adapter(self, recordings: dict[str, str] | None = None):
        transport = ReplayTransport(recordings or dict(self.RECORDINGS))
        ctx = AdapterContext(
            http=HttpClient(transport=transport),
            bank=None,
            bus=EventBus(),
            notifier=RecordingNotifier(),
        )
        adapter = PincoAdapter(ctx)
        adapter.adopt_session(authorization="Bearer replay", csrf_token="csrf-replay")
        return adapter, transport

    async def test_plan_replays_real_responses_end_to_end(self) -> None:
        adapter, transport = self._adapter()

        plan = await adapter.plan()

        # 计划跑完了，而且落到了预期的状态（fixture 里三份作业：已提交/可作答/已过期）
        states = {a.assignment_id: a.state for a in plan.assignments}
        self.assertIs(states[SUBMITTED_HW], PlanState.SKIPPED)
        self.assertIs(states[EXPIRED_HW], PlanState.SKIPPED)
        self.assertIs(states[MATERIAL_ID], PlanState.SKIPPED)
        # 可作答那份：题库空 + 规则答题 → 待人工（不猜）
        self.assertIs(states[OPEN_HW], PlanState.NEEDS_HUMAN)
        self.assertTrue(plan.digest)

        # 请求层面：只打了录制过的只读接口，且一个写接口都没有
        self.assertEqual(transport.write_actions(), [])
        self.assertEqual(
            set(transport.actions()),
            {
                ep.ACTION_COURSE_WORKBENCH,
                ep.ACTION_HOMEWORK_LIST,
                ep.ACTION_MATERIAL_LIST,
                ep.ACTION_HOMEWORK_QUESTIONS,
            },
        )
        # 每个请求都带了 x-server / trace-id / language（ReplayTransport 已校验，
        # 这里再显式断一次，免得有人把校验删了）
        for call in transport.calls:
            self.assertEqual(call.headers[ep.HEADER_SERVER], ep.SERVER_NAME)
            self.assertTrue(call.headers[ep.HEADER_TRACE_ID])

    async def test_replay_reports_unrecorded_request(self) -> None:
        """没录到的接口必须当场失败 —— 那是「适配器打了计划外的接口」的信号。"""
        adapter, _ = self._adapter({ep.ACTION_COURSE_WORKBENCH: "course_list.json"})
        with self.assertRaises(AssertionError) as cm:
            await adapter.plan()
        self.assertIn("回放里没有录制这条请求", str(cm.exception))

    async def test_replay_health_check(self) -> None:
        """健康检查也走回放：REST currentTime 真实响应是 ``{"code":0,...}``。"""
        adapter, transport = self._adapter(
            {f"{ep.REST_API_PREFIX}{ep.REST_PATH_CURRENT_TIME}": "system_current_time.json"}
        )
        self.assertTrue(await adapter.health_check())
        self.assertEqual(transport.calls[0].method, "GET")


# --------------------------------------------------------------------------
# 13. 家族判成功 + fixture 溯源
# --------------------------------------------------------------------------


class TestFamilySuccess(unittest.IsolatedAsyncioTestCase):
    """RPC 与 REST 的「成功」判据不同 —— 混用会把 REST 成功判成失败。

    真实事故：``health_check`` 走 REST ``/system/currentTime``，真实响应是
    ``{"code":0,…,"statusCode":200,…}``；代码当时用 RPC 的规则（``statusCode == 0``）
    去判，于是**每次成功都被判成失败**，health_check 恒假 —— 把「网络没问题」
    报成「网络有问题」，正好毁掉它存在的意义。

    单测当时之所以是绿的：fixture 被手写成了 ``{"code":0,"data":{…}}``（缺
    ``statusCode``），字段缺失走了兜底分支。**回放框架对、回放内容假**。
    """

    FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "pinco"

    def test_same_envelope_is_success_in_rest_and_failure_in_rpc(self) -> None:
        """同一个 JSON 在两族里判定相反 —— 「按家族判成功」的核心不变量。"""
        payload = {
            "code": 0,
            "msg": "ok",
            "data": {"currentTime": 1},
            "statusCode": 200,
            "message": "ok",
        }
        self.assertEqual(
            parsing.unwrap_envelope(payload, action="REST", family=ep.Protocol.REST),
            {"currentTime": 1},
        )
        with self.assertRaises(PincoApiError):
            parsing.unwrap_envelope(payload, action="RPC", family=ep.Protocol.WEB)

    def test_rpc_success_is_status_code_zero(self) -> None:
        payload = {"statusCode": 0, "data": {"total": 1}, "message": "ok"}
        self.assertEqual(
            parsing.unwrap_envelope(payload, action="RPC", family=ep.Protocol.WEB),
            {"total": 1},
        )

    async def test_health_check_true_on_real_captured_envelope(self) -> None:
        """回归锁：拿**真实抓包字节**（含 ``statusCode:200``）跑，必须返回 True。"""
        raw = (self.FIXTURES / "system_current_time.json").read_bytes()
        self.assertIn(b'"statusCode":200', raw, "这条用例必须用带 statusCode 的真实字节，别换成手写样本")
        adapter, _, _, _ = make_adapter({ep.REST_PATH_CURRENT_TIME: FakeReply(raw=raw)})
        self.assertTrue(await adapter.health_check())


class TestFixtureProvenance(unittest.TestCase):
    """样本本身要可溯源 —— 三次「离线全绿、线上不通」全出在样本与真实响应不一致。

    这里检查的不是解析逻辑，而是**样本的形状**：漏登记来源、该标合成的没标、
    真实字段被「顺手精简」。规矩见 ``tests/fixtures/pinco/README.md``。
    """

    FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "pinco"
    #: README 里声明为「合成」的样本：必须带 ``_fixture`` 标记，其余文件不许带。
    SYNTHETIC = frozenset({"homework_questions_plain.json"})

    def _fixtures(self) -> list[pathlib.Path]:
        return sorted(self.FIXTURES.glob("*.json"))

    def test_every_fixture_is_registered_in_readme(self) -> None:
        readme = (self.FIXTURES / "README.md").read_text(encoding="utf-8")
        missing = [p.name for p in self._fixtures() if p.name not in readme]
        self.assertEqual(missing, [], f"这些样本没在 README 清单里登记来源: {missing}")

    def test_synthetic_fixtures_are_marked_and_others_are_not(self) -> None:
        for path in self._fixtures():
            data = json.loads(path.read_text(encoding="utf-8"))
            marked = isinstance(data, dict) and data.get("_fixture") == "synthetic"
            if path.name in self.SYNTHETIC:
                self.assertTrue(marked, f"{path.name} 声明是合成样本，必须在顶层标 _fixture=synthetic")
            else:
                self.assertFalse(marked, f"{path.name} 不是合成样本，不该带 _fixture 标记")

    def test_real_rest_envelope_keeps_deciding_fields(self) -> None:
        """真实抓包的 REST 信封不许被精简 —— 缺了 ``statusCode`` 就会重演恒假事故。"""
        data = json.loads((self.FIXTURES / "system_current_time.json").read_text(encoding="utf-8"))
        for field in ("code", "msg", "data", "statusCode", "message"):
            self.assertIn(field, data, f"真实抓包的 REST 信封必须保留 {field!r}，不要顺手精简")
        self.assertEqual(data["code"], 0)
        self.assertEqual(data["statusCode"], 200)


class TestSessionInjection(unittest.IsolatedAsyncioTestCase):
    """会话注入 —— 这是 CLI / 控制台**唯一走得通**的那条路。

    背景：凭据层只给得了 authorization（``KEDAIBIAO_<平台>_TOKEN``），而应用层
    RPC 要的是 ``csrfToken``。实测只带 authorization 调
    ``GetV1CourseStudentworkbench`` 回 ``403 missing csrf token`` —— 也就是
    适配器「谁也调不动」，``kedaibiao action pinco courses`` 直接失败。
    所以必须支持把浏览器导出的一整串 cookie 搬进来。
    """

    HEADER = "csrfToken=csrf-123; x-token=tok-456; x-username=u%40x; acw_tc=abc"

    def test_parse_cookie_header(self) -> None:
        parsed = adapter_mod.parse_cookie_header(self.HEADER)
        self.assertEqual(parsed["csrfToken"], "csrf-123")
        self.assertEqual(parsed["x-token"], "tok-456")
        self.assertEqual(parsed["acw_tc"], "abc")
        self.assertEqual(adapter_mod.parse_cookie_header(""), {})
        self.assertEqual(adapter_mod.parse_cookie_header("garbage-without-equals"), {})

    def test_cookie_header_is_distinguished_from_authorization(self) -> None:
        """authorization 值（含 base64 填充的 ``=``）不能被误判成 cookie 串。"""
        self.assertTrue(adapter_mod.looks_like_cookie_header(self.HEADER))
        self.assertTrue(adapter_mod.looks_like_cookie_header("csrfToken=only"))
        self.assertFalse(
            adapter_mod.looks_like_cookie_header("Bearer eyJhbGciOiJIUzI1NiJ9.abc==")
        )
        self.assertFalse(adapter_mod.looks_like_cookie_header("YWJjZGVmZ2hpamtsbW5vcA=="))

    async def test_adopt_cookies_requires_csrf(self) -> None:
        adapter, _, _, _ = make_adapter(login=False)
        with self.assertRaises(PincoError) as cm:
            adapter.adopt_cookies("x-token=only-token")
        self.assertIn(ep.CSRF_COOKIE, str(cm.exception))

    async def test_adopt_cookies_installs_into_transport_jar_with_right_domains(self) -> None:
        """装错域浏览器就不会发 —— 根域 cookie 必须挂 ``.seewo.com``。"""
        transport = UrllibTransport()
        adapter = PincoAdapter(AdapterContext(http=HttpClient(transport=transport)))
        adapter.adopt_cookies(self.HEADER)

        pairs = {(c.domain, c.name) for c in transport.cookies}
        self.assertIn(("pinco.seewo.com", ep.CSRF_COOKIE), pairs)
        self.assertIn((".seewo.com", "x-token"), pairs)
        self.assertIn((".seewo.com", "x-username"), pairs)
        # csrf 同时进了会话存储：客户端会把它放进 x-csrf-token 头
        self.assertEqual(adapter.session.storage[ep.CSRF_COOKIE], "csrf-123")
        self.assertNotIn(ep.HEADER_AUTHORIZATION, adapter.session.storage)

    async def test_login_accepts_cookie_string_in_token(self) -> None:
        """cookie 串塞进 ``KEDAIBIAO_PINCO_TOKEN`` 也能用 —— ``.env`` 今天就生效。"""
        adapter, _, _, _ = make_adapter(login=False)
        creds = Credentials(username="stu", password="", extra={"token": self.HEADER})
        self.assertTrue(await adapter.login(creds))
        self.assertEqual(adapter.session.storage[ep.CSRF_COOKIE], "csrf-123")

    async def test_login_reads_cookie_from_dedicated_env(self) -> None:
        adapter, _, _, _ = make_adapter(login=False)
        creds = Credentials(username="stu", password="", extra={})
        with unittest.mock.patch.dict(
            os.environ, {adapter_mod.COOKIE_ENV: self.HEADER}, clear=False
        ):
            self.assertTrue(await adapter.login(creds))
        self.assertEqual(adapter.session.storage[ep.CSRF_COOKIE], "csrf-123")

    async def test_cookie_env_beats_plain_token(self) -> None:
        """两处都给时，cookie 必须赢 —— 否则又会退回那条走不通的路。"""
        adapter, _, _, _ = make_adapter(login=False)
        creds = Credentials(
            username="stu", password="", extra={"token": "Bearer plain-token"}
        )
        with unittest.mock.patch.dict(
            os.environ, {adapter_mod.COOKIE_ENV: self.HEADER}, clear=False
        ):
            self.assertTrue(await adapter.login(creds))
        self.assertEqual(adapter.session.storage[ep.CSRF_COOKIE], "csrf-123")

    async def test_plain_authorization_still_accepted_with_warning(self) -> None:
        """普通 authorization 仍然接受（兼容），但要留下「这条路调不通」的告警。"""
        adapter, _, _, _ = make_adapter(login=False)
        creds = Credentials(username="stu", password="", extra={"token": "Bearer plain-token"})
        with self.assertLogs("kedaibiao.pinco", level="WARNING") as logs:
            self.assertTrue(await adapter.login(creds))
        self.assertIn("csrf", " ".join(logs.output).lower())
        self.assertEqual(
            adapter.session.storage[ep.HEADER_AUTHORIZATION], "Bearer plain-token"
        )


if __name__ == "__main__":
    unittest.main()
