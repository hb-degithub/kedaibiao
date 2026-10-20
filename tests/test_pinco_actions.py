"""品课（pinco）控制台动作测试 —— 完全离线。

覆盖四块：

1. **注册表**：learning / material / answers / connection 四个只读动作，外加
   ``progress``（写操作）已登记；只读动作 ``mutates=False``、``progress``
   ``mutates=True``，参数形态与任务约定一致；``ConsoleService.actions("pinco")``
   能列出它们（且通用动作仍是 builtin）；
2. **信封**：四个动作各自的 table / detail 形状与关键字段；
3. **错误边界**：缺参数 / 找不到目标抛 ``ActionInputError``（服务层 400），
   平台异常原样上抛（由服务层统一翻成 502），不吞、不静默；
4. **只读**：假适配器的 ``submit`` 一旦被调用就直接 AssertionError。

样本直接读 ``tests/fixtures/pinco/`` 的响应原文，再用**真实解析器**转成核心
模型，所以 ``Question.raw`` / ``Assignment.raw`` 的字段形态与线上一致。
假适配器只实现这几个只读方法，不发任何网络请求、不 sleep。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from kedaibiao.api import actions
from kedaibiao.api.actions import ActionInputError, load_builtin_actions
from kedaibiao.api.service import ConsoleService
from kedaibiao.platforms.base import NotSupportedError
from kedaibiao.platforms.pinco import parsing
from kedaibiao.platforms.pinco.errors import PincoApiError, PincoError, PincoNotLoggedIn
from kedaibiao.platforms.pinco.progress import ProgressReport

FIXTURES = Path(__file__).parent / "fixtures" / "pinco"


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _fixture_data(name: str) -> Any:
    return _fixture(name)["data"]


COURSES = parsing.parse_courses(_fixture_data("course_list.json"))
COURSE_A, COURSE_B = COURSES[0], COURSES[1]
ASSIGNMENTS = parsing.parse_assignments(_fixture_data("homework_list.json"), course=COURSE_A)
MATERIALS = parsing.parse_materials(_fixture_data("material_list.json"), course=COURSE_A)
MATERIAL_DETAIL = parsing.parse_material_detail(
    _fixture_data("material_detail.json"), material_id=MATERIALS[0].remote_id
)
QUESTIONS = parsing.parse_questions(_fixture_data("homework_questions.json"))

#: 假适配器的默认会话（模块级单例，避免在参数默认值里现造对象）。
_DEFAULT_SESSION = SimpleNamespace(username="示例同学")


def setUpModule() -> None:
    load_builtin_actions()
    actions.ensure_generic_actions("pinco")


class FakePincoAdapter:
    """离线假适配器：只实现本任务用到的只读方法。

    ``submit`` 故意保留并直接失败 —— 一旦哪个动作不小心走了写链路，
    测试会当场红，而不是等真实账号上才发现。
    """

    name = "pinco"
    display_name = "希沃品课"
    #: 和真实适配器一致：品课的业务错误（如 6569 课程不存在）只跳过该课。
    skippable_course_errors = (PincoApiError,)

    def __init__(
        self,
        *,
        courses: list[Any] | None = None,
        materials: dict[str, list[Any]] | None = None,
        assignments: dict[str, list[Any]] | None = None,
        questions: list[Any] | None = None,
        material_detail: Any = None,
        logged_in: bool = True,
        healthy: Any = True,
        session: Any = _DEFAULT_SESSION,
    ) -> None:
        self.courses = list(COURSES if courses is None else courses)
        if materials is None:
            materials = {COURSE_A.remote_id: list(MATERIALS)}
        self.materials = dict(materials)
        if assignments is None:
            assignments = {COURSE_A.remote_id: list(ASSIGNMENTS)}
        self.assignments = dict(assignments)
        self.questions = list(QUESTIONS if questions is None else questions)
        self.material_detail = MATERIAL_DETAIL if material_detail is None else material_detail
        self.logged_in = logged_in
        self.healthy = healthy
        self.session = session
        self.calls: list[tuple[Any, ...]] = []
        #: 让测试制造平台异常：设了就在对应方法里抛。
        self.list_courses_error: Exception | None = None
        self.health_error: Exception | None = None
        self.questions_error: Exception | None = None
        self.material_error: Exception | None = None
        self.submitted: list[Any] = []
        #: 记录 progress 动作对适配器的调用：(material_id, dry_run, target)。
        self.advance_calls: list[tuple[str, bool, float]] = []
        #: 某门课要抛的课程级异常（模拟品课 6569 课程不存在）。
        self.course_errors: dict[str, Exception] = {}

    # -- 只读方法 --------------------------------------------------------

    async def is_logged_in(self) -> bool:
        self.calls.append(("is_logged_in",))
        return self.logged_in

    async def health_check(self) -> bool:
        self.calls.append(("health_check",))
        if self.health_error is not None:
            raise self.health_error
        return bool(self.healthy)

    async def list_courses(self) -> list[Any]:
        self.calls.append(("list_courses",))
        if self.list_courses_error is not None:
            raise self.list_courses_error
        return list(self.courses)

    async def list_learning_tasks(self, course: Any) -> list[Any]:
        self.calls.append(("list_learning_tasks", course.remote_id))
        error = self.course_errors.get(course.remote_id)
        if error is not None:
            raise error
        return list(self.materials.get(course.remote_id, []))

    async def fetch_material_detail(self, course: Any, material_id: str) -> Any:
        self.calls.append(("fetch_material_detail", course.remote_id, material_id))
        if self.material_error is not None:
            raise self.material_error
        return self.material_detail

    async def list_assignments(self, course: Any) -> list[Any]:
        self.calls.append(("list_assignments", course.remote_id))
        error = self.course_errors.get(course.remote_id)
        if error is not None:
            raise error
        return list(self.assignments.get(course.remote_id, []))

    async def fetch_questions(self, assignment: Any) -> list[Any]:
        self.calls.append(("fetch_questions", assignment.remote_id))
        if self.questions_error is not None:
            raise self.questions_error
        return list(self.questions)

    async def advance_learning(
        self,
        course: Any,
        material_id: str,
        *,
        dry_run: bool = True,
        policy: Any = None,
        target_ratio: float = 1.0,
        on_page: Any = None,
    ) -> ProgressReport:
        """模拟「按页推进」：不发网络、不 sleep，只记录参数并返回报告。"""
        self.advance_calls.append((material_id, dry_run, target_ratio))
        item = next(
            (m for m in self.materials.get(course.remote_id, []) if m.remote_id == material_id),
            None,
        )
        before = float(getattr(item, "progress", 0.0) or 0.0)
        after = before if dry_run else max(before, target_ratio)
        return ProgressReport(
            course_id=course.remote_id,
            material_id=material_id,
            dry_run=dry_run,
            ratio_before=before,
            ratio_after=after,
            stopped="dry_run" if dry_run else "target_reached",
            total_pages=10,
            start_page_index=-1,
            planned_pages=10 if dry_run else 0,
            sent_pages=0 if dry_run else 5,
        )

    # -- 写方法（不该被调用）--------------------------------------------

    async def submit(self, *args: Any, **kwargs: Any) -> Any:
        self.submitted.append((args, kwargs))
        raise AssertionError("只读动作不允许调用 submit")


async def _run(action: str, adapter: Any, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """直接跑某个 pinco 动作的 handler（不经过登录/运行时装配）。"""
    return await actions.get_handler("pinco", action)(adapter, params or {})


def _fields(envelope: dict[str, Any]) -> dict[str, str]:
    return {item["label"]: item["value"] for item in envelope["fields"]}


def _sections(envelope: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["label"]: item for item in envelope["sections"]}


def _rows_by(section: dict[str, Any], key: str) -> dict[str, dict[str, Any]]:
    return {row[key]: row for row in section["rows"]}


class TestRegistry(unittest.TestCase):
    """四个动作的声明必须符合任务约定（只读、平台专属、参数正确）。"""

    def test_four_platform_specific_readonly_actions(self) -> None:
        specs = {spec.name: spec for spec in actions.register_platform("pinco")}
        for name in ("learning", "material", "answers", "connection"):
            with self.subTest(name=name):
                self.assertIn(name, specs)
                spec = specs[name]
                self.assertEqual(spec.platform, "pinco")
                self.assertFalse(spec.builtin, "平台专属动作不该被工具页过滤掉")
                self.assertFalse(spec.mutates, "只读动作不允许标成写操作")
                self.assertFalse(spec.local, "这几个动作都要连平台")

    def test_learning_course_param_is_optional(self) -> None:
        spec = next(s for s in actions.register_platform("pinco") if s.name == "learning")
        self.assertEqual([p.name for p in spec.params], ["course"])
        self.assertFalse(spec.params[0].required)

    def test_material_params_are_required(self) -> None:
        spec = next(s for s in actions.register_platform("pinco") if s.name == "material")
        params = {p.name: p for p in spec.params}
        self.assertEqual(set(params), {"course", "material_id"})
        self.assertTrue(params["course"].required)
        self.assertTrue(params["material_id"].required)

    def test_answers_takes_assignment_id(self) -> None:
        spec = next(s for s in actions.register_platform("pinco") if s.name == "answers")
        self.assertEqual([p.name for p in spec.params], ["assignment_id"])
        self.assertTrue(spec.params[0].required)

    def test_connection_takes_no_params(self) -> None:
        spec = next(s for s in actions.register_platform("pinco") if s.name == "connection")
        self.assertEqual(spec.params, ())

    def test_progress_is_explicitly_a_write_action(self) -> None:
        spec = next(s for s in actions.register_platform("pinco") if s.name == "progress")
        self.assertFalse(spec.builtin)
        self.assertTrue(spec.mutates, "推进学习进度会改平台状态，必须标成写操作")
        params = {p.name: p for p in spec.params}
        self.assertEqual(
            set(params),
            {
                "course",
                "material_id",
                "target",
                "confirm",
                "max_seconds",
                "max_materials",
                "page_interval",
            },
        )
        self.assertTrue(params["course"].required)
        self.assertFalse(params["material_id"].required, "留空应表示整课未达标材料")
        self.assertFalse(params["confirm"].required, "默认干跑，confirm 不是必填")


class TestConsoleServiceListing(unittest.TestCase):
    """控制台服务能列出这四个动作，且标记为平台专属（builtin=False）。"""

    def test_actions_listed_for_pinco(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config").mkdir()
            (root / "config" / "local.toml").write_text(
                "[kedaibiao]\n"
                f'db_url = "sqlite:///{root}/api.db"\n'
                f'session_dir = "{root}/sessions"\n'
                'notify = "null"\n',
                encoding="utf-8",
            )
            data = ConsoleService(root=root).actions("pinco")

        self.assertEqual(data["platform"], "pinco")
        listed = {item["name"]: item for item in data["actions"]}
        for name in ("learning", "material", "answers", "connection"):
            with self.subTest(name=name):
                self.assertIn(name, listed)
                self.assertFalse(listed[name]["builtin"])
                self.assertFalse(listed[name]["mutates"])
        # 通用动作（课程/作业/详情）仍应是 builtin，工具页据此过滤
        self.assertTrue(listed["courses"]["builtin"])
        self.assertTrue(listed["assignments"]["builtin"])


class TestLearningAction(unittest.IsolatedAsyncioTestCase):
    async def test_table_envelope_and_rows(self) -> None:
        adapter = FakePincoAdapter()
        out = await _run("learning", adapter)

        self.assertEqual(out["platform"], "pinco")
        self.assertEqual(out["action"], "learning")
        self.assertEqual(out["kind"], "table")
        self.assertEqual(
            [c["label"] for c in out["columns"]],
            ["课程", "材料号", "名称", "大小(MB)", "学习进度(%)", "来源"],
        )
        self.assertEqual(len(out["rows"]), len(MATERIALS))
        first = out["rows"][0]
        self.assertEqual(first["course"], COURSE_A.name)
        self.assertEqual(first["material_id"], MATERIALS[0].remote_id)
        self.assertEqual(first["name"], MATERIALS[0].title)
        self.assertEqual(first["size_mb"], "21.2")  # 22267856 字节
        self.assertEqual(first["progress"], "0%")
        self.assertEqual(first["source"], "materialSource=0")
        self.assertEqual([row["progress"] for row in out["rows"]], ["0%", "50%", "100%"])
        self.assertEqual(adapter.submitted, [])

    async def test_course_filter_only_reads_that_course(self) -> None:
        adapter = FakePincoAdapter(
            materials={COURSE_A.remote_id: list(MATERIALS), COURSE_B.remote_id: []}
        )
        out = await _run("learning", adapter, {"course": COURSE_B.remote_id})

        self.assertEqual(out["rows"], [])
        self.assertNotIn(("list_learning_tasks", COURSE_A.remote_id), adapter.calls)
        self.assertIn(("list_learning_tasks", COURSE_B.remote_id), adapter.calls)
        self.assertIn(COURSE_B.remote_id, out["title"])

    async def test_blank_course_means_all_courses(self) -> None:
        adapter = FakePincoAdapter(materials={COURSE_A.remote_id: [], COURSE_B.remote_id: []})
        out = await _run("learning", adapter, {"course": "   "})

        self.assertEqual(out["rows"], [])
        self.assertIn(("list_learning_tasks", COURSE_A.remote_id), adapter.calls)
        self.assertIn(("list_learning_tasks", COURSE_B.remote_id), adapter.calls)
        self.assertIn("全部课程", out["title"])

    async def test_unknown_course_is_action_input_error(self) -> None:
        adapter = FakePincoAdapter()
        with self.assertRaises(ActionInputError) as cm:
            await _run("learning", adapter, {"course": "999"})
        self.assertIn("999", str(cm.exception))
        self.assertFalse([call for call in adapter.calls if call[0] == "list_learning_tasks"])

    async def test_empty_result_has_hint(self) -> None:
        adapter = FakePincoAdapter(materials={COURSE_A.remote_id: []})
        out = await _run("learning", adapter)
        self.assertEqual(out["rows"], [])
        self.assertTrue(out["empty_hint"])


class TestMaterialAction(unittest.IsolatedAsyncioTestCase):
    def _params(self) -> dict[str, str]:
        return {
            "course": COURSE_A.remote_id,
            "material_id": MATERIALS[0].remote_id,
        }

    async def test_detail_envelope_and_download_url(self) -> None:
        adapter = FakePincoAdapter()
        out = await _run("material", adapter, self._params())

        self.assertEqual(out["platform"], "pinco")
        self.assertEqual(out["action"], "material")
        self.assertEqual(out["kind"], "detail")
        fields = _fields(out)
        self.assertEqual(fields["材料名"], MATERIAL_DETAIL.name)
        self.assertEqual(fields["课程"], COURSE_A.name)
        self.assertEqual(fields["大小(MB)"], "21.2")
        self.assertEqual(fields["学习进度"], "0%")
        self.assertEqual(fields["可下载"], "可下载")
        self.assertIn("server-main/api/resource", fields["下载地址"])
        self.assertIn(
            ("fetch_material_detail", COURSE_A.remote_id, MATERIALS[0].remote_id), adapter.calls
        )
        self.assertEqual(adapter.submitted, [])

    async def test_raw_summary_hides_signed_url(self) -> None:
        out = await _run("material", FakePincoAdapter(), self._params())
        sections = _sections(out)
        summary = next(item for label, item in sections.items() if label.startswith("原始字段摘要"))
        self.assertEqual(summary["kind"], "text")
        self.assertIn("（已隐藏：含签名或凭据）", summary["text"])
        self.assertNotIn("server-main/api/resource", summary["text"])
        # 非敏感字段照常展示，摘要才有排查价值
        self.assertIn("resourceTypeDownloadable", summary["text"])

    async def test_missing_course_is_action_input_error(self) -> None:
        with self.assertRaises(ActionInputError) as cm:
            await _run("material", FakePincoAdapter(), {"material_id": "x"})
        self.assertIn("course", str(cm.exception))

    async def test_missing_material_id_is_action_input_error(self) -> None:
        with self.assertRaises(ActionInputError) as cm:
            await _run("material", FakePincoAdapter(), {"course": COURSE_A.remote_id})
        self.assertIn("material_id", str(cm.exception))

    async def test_unknown_course_is_action_input_error(self) -> None:
        adapter = FakePincoAdapter()
        with self.assertRaises(ActionInputError) as cm:
            await _run(
                "material",
                adapter,
                {"course": "999", "material_id": MATERIALS[0].remote_id},
            )
        self.assertIn("999", str(cm.exception))
        self.assertNotIn("fetch_material_detail", [c[0] for c in adapter.calls])

    async def test_platform_error_propagates(self) -> None:
        """平台异常不在这里吞掉 —— 服务层统一翻成 502 platform_error。"""
        adapter = FakePincoAdapter()
        adapter.material_error = PincoNotLoggedIn("登录态失效")
        with self.assertRaises(PincoNotLoggedIn):
            await _run("material", adapter, self._params())


class TestAnswersAction(unittest.IsolatedAsyncioTestCase):
    async def test_detail_lists_questions_and_answer_slots(self) -> None:
        adapter = FakePincoAdapter()
        out = await _run("answers", adapter, {"assignment_id": ASSIGNMENTS[0].remote_id})

        self.assertEqual(out["platform"], "pinco")
        self.assertEqual(out["action"], "answers")
        self.assertEqual(out["kind"], "detail")
        fields = _fields(out)
        self.assertEqual(fields["作业号"], ASSIGNMENTS[0].remote_id)
        self.assertEqual(fields["名称"], ASSIGNMENTS[0].title)
        self.assertEqual(fields["课程"], COURSE_A.name)
        self.assertEqual(fields["题目数"], str(len(QUESTIONS)))

        section = _sections(out)["题目与作答槽"]
        self.assertEqual(section["kind"], "table")
        self.assertEqual(
            [c["label"] for c in section["columns"]],
            ["#", "题目号", "题型", "题干", "选项", "score", "review", "submitted"],
        )
        self.assertEqual(len(section["rows"]), len(QUESTIONS))
        rows = _rows_by(section, "qid")

        essay = rows[QUESTIONS[0].remote_id]
        self.assertEqual(essay["type"], "简答")
        self.assertEqual(essay["score"], "未评分")
        self.assertEqual(essay["review"], "—")
        self.assertEqual(essay["submitted"], "未提交")
        self.assertTrue(essay["stem"])

        # fixture 第 3 道简答题的作答槽是 submitted=true
        self.assertEqual(rows[QUESTIONS[2].remote_id]["submitted"], "已提交")

        single = rows[QUESTIONS[3].remote_id]
        self.assertEqual(single["type"], "单选")
        self.assertIn("A.", single["options"])
        self.assertIn("B.", single["options"])
        self.assertEqual(adapter.submitted, [])

    async def test_missing_assignment_id_is_action_input_error(self) -> None:
        with self.assertRaises(ActionInputError) as cm:
            await _run("answers", FakePincoAdapter(), {})
        self.assertIn("assignment_id", str(cm.exception))

    async def test_unknown_assignment_is_action_input_error(self) -> None:
        adapter = FakePincoAdapter(assignments={})
        with self.assertRaises(ActionInputError) as cm:
            await _run("answers", adapter, {"assignment_id": "404"})
        self.assertIn("找不到", str(cm.exception))
        self.assertIn("404", str(cm.exception))
        self.assertNotIn("fetch_questions", [c[0] for c in adapter.calls])

    async def test_learning_task_is_bad_request_not_platform_error(self) -> None:
        """学习任务（课程材料）没有题目：这是选错对象，不是平台故障。"""
        adapter = FakePincoAdapter()
        adapter.questions_error = NotSupportedError("课程材料没有题目")
        with self.assertRaises(ActionInputError) as cm:
            await _run("answers", adapter, {"assignment_id": ASSIGNMENTS[0].remote_id})
        self.assertIn("没有题目", str(cm.exception))

    async def test_platform_error_propagates(self) -> None:
        adapter = FakePincoAdapter()
        adapter.questions_error = PincoNotLoggedIn("登录态失效")
        with self.assertRaises(PincoNotLoggedIn):
            await _run("answers", adapter, {"assignment_id": ASSIGNMENTS[0].remote_id})


class TestConnectionAction(unittest.IsolatedAsyncioTestCase):
    async def test_detail_reports_all_probes(self) -> None:
        adapter = FakePincoAdapter(courses=[COURSE_A, COURSE_B])
        out = await _run("connection", adapter)

        self.assertEqual(out["platform"], "pinco")
        self.assertEqual(out["action"], "connection")
        self.assertEqual(out["kind"], "detail")
        fields = _fields(out)
        self.assertIn("希沃品课", fields["平台"])
        self.assertIn("pinco", fields["平台"])
        self.assertEqual(fields["会话"], "已接管")
        self.assertEqual(fields["账号"], "示例同学")
        self.assertEqual(fields["is_logged_in"], "有效")
        self.assertEqual(fields["health_check"], "通过")
        self.assertEqual(fields["课程数量"], "2")
        self.assertIn(("health_check",), adapter.calls)
        self.assertIn(("list_courses",), adapter.calls)
        self.assertEqual(adapter.submitted, [])

    async def test_not_logged_in_reports_without_reading_courses(self) -> None:
        adapter = FakePincoAdapter(logged_in=False, session=None)
        out = await _run("connection", adapter)

        fields = _fields(out)
        self.assertEqual(fields["会话"], "没有会话")
        self.assertEqual(fields["账号"], "—")
        self.assertEqual(fields["is_logged_in"], "无效或未登录")
        self.assertEqual(fields["课程数量"], "未读取（未登录）")
        self.assertNotIn(("list_courses",), adapter.calls)
        # 健康检查是匿名接口，未登录也可以探
        self.assertIn(("health_check",), adapter.calls)

    async def test_health_failure_is_reported(self) -> None:
        adapter = FakePincoAdapter(healthy=False)
        out = await _run("connection", adapter)
        self.assertEqual(_fields(out)["health_check"], "失败")
        self.assertEqual(_fields(out)["课程数量"], "2")

    async def test_health_exception_is_reported_not_raised(self) -> None:
        adapter = FakePincoAdapter()
        adapter.health_error = RuntimeError("网络炸了")
        out = await _run("connection", adapter)
        field = _fields(out)["health_check"]
        self.assertTrue(field.startswith("失败（RuntimeError"))
        self.assertIn("网络炸了", field)
        # 自检不该因为一个探测项失败就整条 502，课程数量还要照常尝试
        self.assertEqual(_fields(out)["课程数量"], "2")

    async def test_course_list_failure_is_reported_not_raised(self) -> None:
        adapter = FakePincoAdapter()
        adapter.list_courses_error = PincoError("课程接口 403")
        out = await _run("connection", adapter)
        field = _fields(out)["课程数量"]
        self.assertIn("读取失败", field)
        self.assertIn("403", field)
        self.assertEqual(_fields(out)["is_logged_in"], "有效")


class TestProgressAction(unittest.IsolatedAsyncioTestCase):
    """progress（刷课）动作：默认干跑、confirm 才写、可留空处理整课未达标材料。"""

    async def test_default_is_dry_run_and_sends_nothing(self) -> None:
        adapter = FakePincoAdapter()
        out = await _run(
            "progress",
            adapter,
            {"course": COURSE_A.remote_id, "material_id": MATERIALS[0].remote_id},
        )
        self.assertEqual(out["kind"], "detail")
        self.assertEqual(_fields(out)["模式"], "干跑")
        self.assertIn("没有发送任何写请求", out["note"])
        self.assertEqual(
            adapter.advance_calls, [(MATERIALS[0].remote_id, True, 1.0)]
        )
        row = _sections(out)["处理结果"]["rows"][0]
        self.assertEqual(row["before"], "0%")
        self.assertEqual(row["after"], "0%")
        self.assertEqual(row["stopped"], "干跑")

    async def test_confirm_true_actually_writes(self) -> None:
        adapter = FakePincoAdapter()
        out = await _run(
            "progress",
            adapter,
            {
                "course": COURSE_A.remote_id,
                "material_id": MATERIALS[0].remote_id,
                "confirm": "true",
            },
        )
        self.assertEqual(_fields(out)["模式"], "写入")
        self.assertEqual(_fields(out)["目标进度"], "100%")
        self.assertEqual(
            adapter.advance_calls, [(MATERIALS[0].remote_id, False, 1.0)]
        )
        row = _sections(out)["处理结果"]["rows"][0]
        self.assertEqual(row["before"], "0%")
        self.assertEqual(row["after"], "100%")
        self.assertEqual(row["pages"], "5")
        self.assertEqual(row["stopped"], "已达标")

    async def test_blank_material_picks_unfinished_up_to_limit(self) -> None:
        adapter = FakePincoAdapter()
        out = await _run(
            "progress",
            adapter,
            {"course": COURSE_A.remote_id, "max_materials": "1"},
        )
        # 样本里 3 份材料：0%、50%、100%；目标 100% → 未达标 2 份，本次只处理 1 份
        self.assertEqual(len(adapter.advance_calls), 1)
        self.assertEqual(_fields(out)["本次材料"], "1 份")
        self.assertEqual(_fields(out)["剩余未处理"], "1 份（可再执行一次继续）")

    async def test_blank_material_with_no_unfinished_is_empty(self) -> None:
        adapter = FakePincoAdapter()
        out = await _run(
            "progress",
            adapter,
            {"course": COURSE_A.remote_id, "target": "0.0"},
        )
        self.assertEqual(adapter.advance_calls, [])
        self.assertEqual(_sections(out)["处理结果"]["rows"], [])

    async def test_unknown_material_is_action_input_error(self) -> None:
        adapter = FakePincoAdapter()
        with self.assertRaises(ActionInputError):
            await _run(
                "progress",
                adapter,
                {"course": COURSE_A.remote_id, "material_id": "no-such"},
            )

    async def test_bad_target_and_interval_are_action_input_errors(self) -> None:
        adapter = FakePincoAdapter()
        for params in (
            {"course": COURSE_A.remote_id, "target": "1.5"},
            {"course": COURSE_A.remote_id, "target": "abc"},
            {"course": COURSE_A.remote_id, "page_interval": "0.1"},
            {"course": COURSE_A.remote_id, "max_seconds": "1"},
        ):
            with self.subTest(params=params), self.assertRaises(ActionInputError):
                await _run("progress", adapter, params)


class TestAllCoursesTolerance(unittest.IsolatedAsyncioTestCase):
    """整课抓取：一门课回「课程不存在」不能拖垮其余 20 多门课。"""

    async def test_learning_skips_course_scoped_error(self) -> None:
        adapter = FakePincoAdapter()
        adapter.course_errors[COURSE_B.remote_id] = PincoApiError(
            "GetV1CourseByCourseIdStudentMaterialList 返回 statusCode=6569：课程不存在",
            code=6569,
        )
        out = await _run("learning", adapter, {})
        self.assertEqual(len(out["rows"]), len(MATERIALS))
        self.assertIn("已跳过", out["note"])
        self.assertIn(COURSE_B.name, out["note"])

    async def test_assignments_skips_course_scoped_error(self) -> None:
        adapter = FakePincoAdapter()
        adapter.course_errors[COURSE_B.remote_id] = PincoApiError(
            "GetV1StudenthomeworkList 返回 statusCode=6569：课程不存在", code=6569
        )
        out = await _run("assignments", adapter, {})
        self.assertEqual(len(out["rows"]), len(ASSIGNMENTS))
        self.assertIn("已跳过", out["note"])

    async def test_specific_course_error_is_not_swallowed(self) -> None:
        adapter = FakePincoAdapter()
        adapter.course_errors[COURSE_B.remote_id] = PincoApiError("课程不存在", code=6569)
        with self.assertRaises(PincoApiError):
            await _run("assignments", adapter, {"course": COURSE_B.remote_id})

    async def test_unlisted_error_is_not_swallowed(self) -> None:
        adapter = FakePincoAdapter()
        adapter.course_errors[COURSE_B.remote_id] = ValueError("不是课程级错误")
        with self.assertRaises(ValueError):
            await _run("assignments", adapter, {})


if __name__ == "__main__":
    unittest.main()
