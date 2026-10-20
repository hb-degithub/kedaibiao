"""控制台服务层测试（完全离线）。

服务层是与 HTTP 框架无关的那一半，所以这里不碰 socket —— HTTP 外壳的测试
在 tests/test_api_server.py。
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest import mock

from kedaibiao import adapters
from kedaibiao.api.service import ConsoleError, ConsoleService, _jsonable, _parse_kinds
from kedaibiao.core.models import (
    Answer,
    AnswerSource,
    Assignment,
    Course,
    Credentials,
    Question,
    QuestionType,
    RunStatus,
    SubmissionResult,
    TaskKind,
)
from kedaibiao.platforms.base import Capabilities, PlatformAdapter

GOOD = "apifake"
NOSUBMIT = "apinosubmit"
SLOW = "apislow"
NOCAPS = "apinocaps"
NATIVE = "apinative"


class FakeAdapter(PlatformAdapter):
    name = GOOD
    display_name = "接口假平台"
    capabilities = Capabilities()

    async def login(self, credentials: Credentials) -> bool:
        return True

    async def is_logged_in(self) -> bool:
        return True

    async def list_courses(self) -> list[Course]:
        return [Course(remote_id="c1", name="课程")]

    async def list_assignments(self, course: Course) -> list[Assignment]:
        return [Assignment(remote_id="a1", title="作业", kind=TaskKind.HOMEWORK)]

    async def fetch_questions(self, assignment: Assignment) -> list[Question]:
        return [Question(stem="已知题", type=QuestionType.SINGLE,
                         options=["甲", "乙"], remote_id="q1")]

    async def submit(self, assignment: Assignment, answers: list[Answer]) -> SubmissionResult:
        return SubmissionResult(assignment_id=assignment.remote_id, ok=True, score=90.0)


class NoSubmitAdapter(FakeAdapter):
    name = NOSUBMIT
    display_name = "不能提交的平台"
    capabilities = Capabilities(submit=False)


class SlowAdapter(FakeAdapter):
    name = SLOW
    display_name = "慢平台"

    async def list_courses(self) -> list[Course]:
        await asyncio.sleep(0.8)
        return await super().list_courses()


def _creds(platform: str) -> dict[str, str]:
    up = platform.upper()
    return {f"KEDAIBIAO_{up}_USER": "stu", f"KEDAIBIAO_{up}_PASS": "pw"}


class NativeBatchAdapter(FakeAdapter):
    """带「原生提交链路」的假平台：用来离线验证一键批量提交的后台任务。

    三个任务分别覆盖三种分支：a1 已满分（跳过）、a2 可提交、a3 本地校验被拦。
    """

    name = NATIVE
    display_name = "原生批量假平台"
    capabilities = Capabilities(submit=True)
    native_submit_flow = True
    submitted: list[str] = []

    async def list_assignments(self, course: Course) -> list[Assignment]:
        return [
            Assignment(remote_id=f"a{i}", title=f"任务{i}", kind=TaskKind.HOMEWORK)
            for i in (1, 2, 3)
        ]

    async def native_assignment_state(self, assignment_id: str) -> dict[str, Any]:
        # a1：逐项全对 → 跳过；a2：做过但没全对 → 重交；a3：被拦
        if assignment_id == "a1":
            return {
                "finished": True, "score_pct": 1.0,
                "total_items": 2, "correct_items": 2, "full_marks": True,
            }
        if assignment_id == "a2":
            return {
                "finished": True, "score_pct": 1.0,   # 平台 score_pct 不可信（只对一半）
                "total_items": 2, "correct_items": 1, "full_marks": False,
            }
        return {"finished": False, "score_pct": None,
                "total_items": 0, "correct_items": 0, "full_marks": False}

    async def native_plan(self, assignment_id: str) -> dict[str, Any]:
        if assignment_id == "a3":
            return {
                "can_submit": False,
                "blockers": ["子题数不一致"],
                "question_count": 1,
                "item_count": 1,
                "submit_type": 1,
            }
        return {
            "can_submit": True,
            "question_count": 1,
            "item_count": 2,
            "submit_type": 1,
            "blockers": [],
        }

    async def native_submit(self, assignment_id: str) -> dict[str, Any]:
        type(self).submitted.append(assignment_id)
        return {
            "ok": True,
            "code": 0,
            "score_pct": 1.0,
            "server_finish": True,
        }


class NoCapsAdapter(FakeAdapter):
    """连课程都读不了的平台 —— 验证「能力缺失 -> 400 而不是 502」。"""

    name = NOCAPS
    display_name = "啥都不支持的平台"
    capabilities = Capabilities(list_courses=False, list_assignments=False)


class ServiceTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "config").mkdir()
        (self.root / "config" / "local.toml").write_text(
            "[kedaibiao]\n"
            f'db_url = "sqlite:///{self.root}/api.db"\n'
            f'session_dir = "{self.root}/sessions"\n'
            'notify = "null"\n',
            encoding="utf-8",
        )
        for cls in (FakeAdapter, NoSubmitAdapter, SlowAdapter, NoCapsAdapter,
                    NativeBatchAdapter):
            adapters.register(cls.name, cls, override=True)
            self.addCleanup(adapters.unregister, cls.name)
        self.service = ConsoleService(root=self.root)
        # 凭据只来自账号库：给每个假平台绑一个号
        for cls in (FakeAdapter, NoSubmitAdapter, SlowAdapter, NoCapsAdapter,
                    NativeBatchAdapter):
            self.bind_account(cls.name)

    def bind_account(self, platform: str, username: str = "stu") -> int:
        """给某平台绑定一个账号 —— 现在凭据**只**来自账号库，不再读 .env。"""
        from kedaibiao.security.accounts import AccountStore

        store = AccountStore(self.root / ("api.db"))
        try:
            account = store.add(0, platform, label="测试号", username=username)
            return account.id
        finally:
            store.close()

    def unbind_all(self, platform: str) -> None:
        from kedaibiao.security.accounts import AccountStore

        store = AccountStore(self.root / ("api.db"))
        try:
            for acc in store.list_for(None, platform):
                store.delete(acc.id)
        finally:
            store.close()


    def seed_bank(self) -> None:
        """往题库塞一条答案，好让流水线走到「干跑」而不是「一题都没答出来」。"""
        from kedaibiao.bank.store import QuestionBank

        bank = QuestionBank(self.root / "api.db")
        q = Question(stem="已知题", options=["甲", "乙"], remote_id="q1")
        bank.upsert(q, Answer(question=q, choices=[0],
                              source=AnswerSource.BANK_HIT, confidence=1.0))
        bank.close()

    def wait(self) -> None:
        self.assertTrue(self.service.wait_for_idle(timeout=20), "任务没有在超时内结束")


class TestOverview(ServiceTestBase):
    def test_empty_state_reports_zeros(self) -> None:
        # 注意：setUp 会为假平台绑定账号，所以库文件本来就存在；
        # 这里断言的是「没有任何题库与跑批数据」。
        data = self.service.overview()
        self.assertEqual(data["bank"]["total"], 0)
        self.assertIsNone(data["bank"]["accuracy"])
        self.assertEqual(data["runs"]["total"], 0)

    def test_reports_env_and_settings(self) -> None:
        data = self.service.overview()
        self.assertEqual(data["env"], "dev")
        self.assertTrue(data["dry_run"])
        self.assertIn("version", data)
        self.assertIn("python", data)
        self.assertIn(data["platform_count"], range(0, 50))

    def test_optional_deps_are_booleans(self) -> None:
        for value in self.service.overview()["optional_deps"].values():
            self.assertIsInstance(value, bool)

    def test_no_credentials_leaked(self) -> None:
        blob = repr(self.service.overview())
        self.assertNotIn("pw", blob)

    def test_after_a_run_counts_appear(self) -> None:
        self.seed_bank()
        with mock.patch.dict(os.environ, _creds(GOOD), clear=True):
            self.service.start_run(GOOD)
            self.wait()
        data = self.service.overview()
        self.assertTrue(data["db_exists"])
        self.assertGreaterEqual(data["runs"]["total"], 1)


class TestPlatforms(ServiceTestBase):
    def test_lists_registered_adapters_with_capabilities(self) -> None:
        names = {p["name"]: p for p in self.service.platforms()["platforms"]}
        self.assertIn(GOOD, names)
        self.assertEqual(names[GOOD]["display_name"], "接口假平台")
        self.assertTrue(names[GOOD]["capabilities"]["submit"])
        self.assertFalse(names[NOSUBMIT]["capabilities"]["submit"])

    def test_has_credentials_flag(self) -> None:
        """「有凭据」= 账号库里绑了该平台的号（不再看 .env）。"""
        self.unbind_all(NOSUBMIT)
        names = {p["name"]: p for p in self.service.platforms()["platforms"]}
        self.assertTrue(names[GOOD]["has_credentials"])
        self.assertFalse(names[NOSUBMIT]["has_credentials"], "没绑账号就不算有凭据")


class TestBankAndHistory(ServiceTestBase):
    def test_bank_is_flat_with_db_path(self) -> None:
        data = self.service.bank()
        for key in ("total", "graded", "correct", "accuracy", "ungraded", "by_source", "db_path"):
            self.assertIn(key, data)
        self.assertNotIn("bank", data)

    def test_runs_and_submissions_empty(self) -> None:
        self.assertEqual(self.service.runs()["runs"], [])
        self.assertEqual(self.service.submissions()["submissions"], [])

    def test_submissions_recorded_after_live_run(self) -> None:
        self.seed_bank()
        with mock.patch.dict(os.environ, _creds(GOOD), clear=True):
            self.service.start_run(GOOD, live=True)
            self.wait()

        subs = self.service.submissions()["submissions"]
        self.assertEqual(len(subs), 1)
        self.assertTrue(subs[0]["ok"])
        self.assertEqual(subs[0]["score"], 90.0)

    def test_runs_status_vocabulary(self) -> None:
        self.seed_bank()
        with mock.patch.dict(os.environ, _creds(GOOD), clear=True):
            self.service.start_run(GOOD)
            self.wait()
        runs = self.service.runs()["runs"]
        self.assertTrue(runs)
        self.assertEqual(runs[0]["status"], RunStatus.SKIPPED.value)


class TestStartRunValidation(ServiceTestBase):
    def test_unknown_platform(self) -> None:
        with self.assertRaises(ConsoleError) as cm:
            self.service.start_run("ghost")
        self.assertEqual(cm.exception.kind, "unknown_platform")
        self.assertEqual(cm.exception.status, 404)

    def test_live_on_platform_without_submit(self) -> None:
        with mock.patch.dict(os.environ, _creds(NOSUBMIT), clear=True):
            with self.assertRaises(ConsoleError) as cm:
                self.service.start_run(NOSUBMIT, live=True)
        self.assertEqual(cm.exception.kind, "bad_request")
        self.assertIn("不支持提交", str(cm.exception))

    def test_missing_credentials_blocks_even_dry_run(self) -> None:
        self.unbind_all(GOOD)  # 凭据只来自账号库
        with self.assertRaises(ConsoleError) as cm:
            self.service.start_run(GOOD)
        self.assertEqual(cm.exception.kind, "no_credentials")
        self.assertIn("账号", str(cm.exception))

    def test_bad_kind(self) -> None:
        with mock.patch.dict(os.environ, _creds(GOOD), clear=True):
            with self.assertRaises(ConsoleError) as cm:
                self.service.start_run(GOOD, kinds=["bogus"])
        self.assertEqual(cm.exception.kind, "bad_request")

    def test_job_not_found(self) -> None:
        with self.assertRaises(ConsoleError) as cm:
            self.service.job("j-999")
        self.assertEqual(cm.exception.kind, "not_found")
        self.assertEqual(cm.exception.status, 404)


class TestRunLifecycle(ServiceTestBase):
    def test_dry_run_job_skips_without_writing(self) -> None:
        self.seed_bank()
        with mock.patch.dict(os.environ, _creds(GOOD), clear=True):
            started = self.service.start_run(GOOD)
            self.assertEqual(started["status"], "running")
            self.assertFalse(started["live"])
            self.wait()

        job = self.service.job(started["job_id"])
        self.assertEqual(job["status"], "succeeded")
        self.assertTrue(job["finished_at"])
        self.assertEqual(job["records"][0]["status"], "skipped")
        self.assertIn("dry_run", job["records"][0]["note"])
        self.assertEqual(self.service.submissions()["submissions"], [])

    def test_job_status_has_no_aborted(self) -> None:
        self.seed_bank()
        with mock.patch.dict(os.environ, _creds(GOOD), clear=True):
            self.service.start_run(GOOD)
            self.wait()
        self.assertIn(self.service.jobs()["jobs"][0]["status"],
                      {"running", "succeeded", "failed"})

    def test_busy(self) -> None:
        with mock.patch.dict(os.environ, _creds(SLOW), clear=True):
            self.service.start_run(SLOW)
            with self.assertRaises(ConsoleError) as cm:
                self.service.start_run(SLOW)
            self.assertEqual(cm.exception.kind, "busy")
            self.assertEqual(cm.exception.status, 409)
            self.wait()

    def test_jobs_newest_first(self) -> None:
        self.seed_bank()
        with mock.patch.dict(os.environ, _creds(GOOD), clear=True):
            first = self.service.start_run(GOOD)
            self.wait()
            second = self.service.start_run(GOOD)
            self.wait()
        ids = [j["job_id"] for j in self.service.jobs()["jobs"]]
        self.assertEqual(ids[0], second["job_id"])
        self.assertIn(first["job_id"], ids)

    def test_events_are_recorded(self) -> None:
        self.seed_bank()
        with mock.patch.dict(os.environ, _creds(GOOD), clear=True):
            self.service.start_run(GOOD)
            self.wait()
        events = self.service.events()["events"]
        names = {e["name"] for e in events}
        self.assertIn("assignment.done", names)
        # 事件载荷里是 datetime/Enum，必须已被转成可 JSON 化的值
        for event in events:
            self.assertIsInstance(event["at"], str)
            self.assertIsInstance(event["payload"], dict)

    def test_failure_is_captured_not_swallowed(self) -> None:
        """适配器抛错时，任务必须变成 failed 并带上原因，不能静默死掉。"""
        class Boom(FakeAdapter):
            name = "apiboom"

            async def list_courses(self) -> list[Course]:
                raise RuntimeError("接口炸了")

        adapters.register(Boom.name, Boom, override=True)
        self.addCleanup(adapters.unregister, Boom.name)
        self.bind_account(Boom.name)
        with mock.patch.dict(os.environ, _creds(Boom.name), clear=True):
            started = self.service.start_run(Boom.name)
            self.wait()
        job = self.service.job(started["job_id"])
        self.assertEqual(job["status"], "failed")
        self.assertIn("接口炸了", job["error"])

    def test_events_ring_buffer_is_capped(self) -> None:
        from kedaibiao.api import service as svc

        original = svc.EVENT_HISTORY
        svc.EVENT_HISTORY = 5
        try:
            for i in range(20):
                self.service._record_event(  # noqa: SLF001
                    type("E", (), {"at": datetime.now(), "name": f"e{i}", "payload": {}})()
                )
            self.assertEqual(len(self.service.events(limit=100)["events"]), 5)
        finally:
            svc.EVENT_HISTORY = original


class TestJsonable(unittest.TestCase):
    def test_datetime_and_enum(self) -> None:
        self.assertEqual(_jsonable(datetime(2026, 10, 19, 11, 20)), "2026-10-19T11:20:00")
        self.assertEqual(_jsonable(TaskKind.EXAM), "exam")

    def test_nested(self) -> None:
        out = _jsonable({"a": [TaskKind.COURSE], "b": {"c": datetime(2026, 1, 2, 3, 4, 5)}})
        self.assertEqual(out, {"a": ["course"], "b": {"c": "2026-01-02T03:04:05"}})

    def test_dataclass(self) -> None:
        out = _jsonable(SubmissionResult(assignment_id="a", ok=True))
        self.assertEqual(out["assignment_id"], "a")
        self.assertTrue(out["ok"])

    def test_scalars_pass_through(self) -> None:
        for value in (None, True, 1, 1.5, "x"):
            self.assertEqual(_jsonable(value), value)

    def test_unknown_becomes_str(self) -> None:
        self.assertIsInstance(_jsonable(object()), str)


class TestParseKinds(unittest.TestCase):
    def test_empty(self) -> None:
        self.assertEqual(_parse_kinds([]), frozenset())

    def test_valid(self) -> None:
        self.assertEqual(_parse_kinds(["exam", "homework"]),
                         frozenset({TaskKind.EXAM, TaskKind.HOMEWORK}))

    def test_invalid(self) -> None:
        with self.assertRaises(ConsoleError):
            _parse_kinds(["nope"])


class TestPlatformScoping(ServiceTestBase):
    """`?platform=` 的过滤语义。"""

    def _seed_runs(self) -> None:
        from kedaibiao.bank.store import ProgressStore
        from kedaibiao.core.models import RunRecord, RunStatus

        store = ProgressStore(self.root / "api.db")
        for platform, status in ((GOOD, RunStatus.SUCCEEDED), (GOOD, RunStatus.FAILED),
                                 (NOSUBMIT, RunStatus.SUCCEEDED)):
            run_id = store.start_run(
                RunRecord(platform=platform, assignment_id=f"{platform}-x")
            )
            store.finish_run(run_id, status)
        store.close()

    def test_overview_scoped(self) -> None:
        self._seed_runs()
        allp = self.service.overview()
        scoped = self.service.overview(GOOD)
        self.assertIsNone(allp["platform"])
        self.assertEqual(scoped["platform"], GOOD)
        self.assertEqual(allp["runs"]["total"], 3)
        self.assertEqual(scoped["runs"]["total"], 2)
        self.assertEqual(scoped["runs"]["failed"], 1)

    def test_runs_scoped(self) -> None:
        self._seed_runs()
        rows = self.service.runs(platform=GOOD)["runs"]
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r["platform"] == GOOD for r in rows))
        self.assertEqual(len(self.service.runs()["runs"]), 3)

    def test_bank_scoped(self) -> None:
        from kedaibiao.bank.store import QuestionBank

        bank = QuestionBank(self.root / "api.db")
        q = Question(stem="题", options=["甲"], remote_id="q1")
        bank.upsert(q, Answer(question=q, choices=[0], source=AnswerSource.BANK_HIT),
                    platform=GOOD)
        bank.close()

        self.assertEqual(self.service.bank(GOOD)["total"], 1)
        self.assertEqual(self.service.bank(NOSUBMIT)["total"], 0)
        self.assertEqual(self.service.bank()["total"], 1)

    def test_unknown_platform_is_404_not_empty(self) -> None:
        for call in (
            lambda: self.service.overview("ghost"),
            lambda: self.service.runs(platform="ghost"),
            lambda: self.service.bank("ghost"),
            lambda: self.service.jobs("ghost"),
        ):
            with self.subTest(call=call):
                with self.assertRaises(ConsoleError) as cm:
                    call()
                self.assertEqual(cm.exception.kind, "unknown_platform")
                self.assertEqual(cm.exception.status, 404)

    def test_blank_platform_means_all(self) -> None:
        self.assertIsNone(self.service.overview("")["platform"])
        self.assertIsNone(self.service.overview("   ")["platform"])

    def test_jobs_scoped(self) -> None:
        self.seed_bank()
        with mock.patch.dict(os.environ, _creds(GOOD), clear=True):
            self.service.start_run(GOOD)
            self.wait()
        self.assertEqual(len(self.service.jobs(GOOD)["jobs"]), 1)
        self.assertEqual(len(self.service.jobs(NOSUBMIT)["jobs"]), 0)


class TestStatistics(ServiceTestBase):
    def test_empty_state_lists_all_registered_platforms(self) -> None:
        data = self.service.statistics()
        names = {r["platform"] for r in data["platforms"]}
        self.assertIn(GOOD, names)
        self.assertIn(NOSUBMIT, names, "没有数据的平台也要出现，否则对比表缺行")
        self.assertEqual(data["platforms"][0]["questions"], 0)
        self.assertIsNone(data["platforms"][0]["accuracy"])

    def test_merges_runs_and_questions(self) -> None:
        from kedaibiao.bank.store import ProgressStore, QuestionBank
        from kedaibiao.core.models import RunRecord, RunStatus

        bank = QuestionBank(self.root / "api.db")
        q = Question(stem="题", options=["甲"], remote_id="q1")
        bank.upsert(q, Answer(question=q, choices=[0], source=AnswerSource.BANK_HIT),
                    correct=True, platform=GOOD)
        bank.close()

        store = ProgressStore(self.root / "api.db")
        run_id = store.start_run(RunRecord(platform=GOOD, assignment_id="a"))
        store.finish_run(run_id, RunStatus.SUCCEEDED)
        store.record_submission(GOOD, SubmissionResult(assignment_id="a", ok=True))
        store.close()

        rows = {r["platform"]: r for r in self.service.statistics()["platforms"]}
        self.assertEqual(rows[GOOD]["succeeded"], 1)
        self.assertEqual(rows[GOOD]["questions"], 1)
        self.assertEqual(rows[GOOD]["accuracy"], 1.0)
        self.assertEqual(rows[GOOD]["submissions"], 1)


class TestSettingsView(ServiceTestBase):
    def test_shape_and_no_secrets(self) -> None:
        with mock.patch.dict(
            os.environ,
            {f"KEDAIBIAO_{GOOD.upper()}_USER": "stu",
             f"KEDAIBIAO_{GOOD.upper()}_PASS": "topsecret"},
            clear=True,
        ):
            data = self.service.settings_view()

        for key in ("dry_run", "max_concurrency", "db_path", "dotenv_present",
                    "env_keys_seen", "optional_deps", "platforms"):
            self.assertIn(key, data)

        blob = repr(data)
        self.assertNotIn("topsecret", blob, "设置页绝不能带出凭据值")
        self.assertNotIn("stu", blob, "用户名也不该带出")

    def test_env_keys_seen_lists_names_only(self) -> None:
        with mock.patch.dict(
            os.environ, {f"KEDAIBIAO_{GOOD.upper()}_PASS": "secret"}, clear=True
        ):
            keys = self.service.settings_view()["env_keys_seen"]
        self.assertIn(f"KEDAIBIAO_{GOOD.upper()}_PASS", keys)

    def test_credentials_hint_shows_expected_keys(self) -> None:
        rows = {p["name"]: p for p in self.service.settings_view()["platforms"]}
        self.assertIn(f"KEDAIBIAO_{GOOD.upper()}_USER", rows[GOOD]["credentials_hint"])

    def test_dotenv_present_flag(self) -> None:
        self.assertFalse(self.service.settings_view()["dotenv_present"])
        (self.root / ".env").write_text("X=1\n", encoding="utf-8")
        service = ConsoleService(root=self.root)
        self.assertTrue(service.settings_view()["dotenv_present"])


class TestActions(ServiceTestBase):
    def setUp(self) -> None:
        super().setUp()
        from kedaibiao.api.actions import ActionParam, ActionSpec, message, register

        async def _ping(adapter: Any, params: dict) -> dict:
            return message(platform=GOOD, action="ping", label="测试动作",
                           message=f"pong:{params.get('who', '')}")

        register(
            ActionSpec(platform=GOOD, name="ping", label="测试动作",
                       params=(ActionParam("who", "对象", required=True),)),
            _ping,
        )

    def test_list_actions(self) -> None:
        data = self.service.actions(GOOD)
        self.assertEqual(data["platform"], GOOD)
        names = [a["name"] for a in data["actions"]]
        self.assertIn("ping", names)
        ping = next(a for a in data["actions"] if a["name"] == "ping")
        self.assertTrue(ping["params"][0]["required"])

    def test_every_platform_gets_generic_actions(self) -> None:
        """课程/作业是框架契约里就有的能力，所有平台都该有。"""
        for name in (GOOD, NOSUBMIT):
            names = {a["name"] for a in self.service.actions(name)["actions"]}
            self.assertLessEqual({"courses", "assignments"}, names, name)

    def test_courses_action_returns_table(self) -> None:
        with mock.patch.dict(os.environ, _creds(GOOD), clear=True):
            out = self.service.run_action(GOOD, "courses", {})
        self.assertEqual(out["kind"], "table")
        self.assertEqual(out["rows"][0]["name"], "课程")
        self.assertEqual(out["platform"], GOOD)

    def test_assignments_action_returns_table(self) -> None:
        with mock.patch.dict(os.environ, _creds(GOOD), clear=True):
            out = self.service.run_action(GOOD, "assignments", {})
        self.assertEqual(out["kind"], "table")
        self.assertEqual(out["rows"][0]["id"], "a1")
        self.assertEqual(out["rows"][0]["state"], "未完成")

    def test_assignments_unknown_course_is_bad_request(self) -> None:
        with mock.patch.dict(os.environ, _creds(GOOD), clear=True):
            with self.assertRaises(ConsoleError) as cm:
                self.service.run_action(GOOD, "assignments", {"course": "nope"})
        self.assertEqual(cm.exception.kind, "bad_request")
        self.assertIn("nope", str(cm.exception))

    def test_unsupported_capability_is_bad_request_not_502(self) -> None:
        """平台不支持某项读能力 -> 400（用户选错了），不是 502（平台故障）。"""
        with mock.patch.dict(os.environ, _creds(NOCAPS), clear=True):
            with self.assertRaises(ConsoleError) as cm:
                self.service.run_action(NOCAPS, "courses", {})
        self.assertEqual(cm.exception.kind, "bad_request")
        self.assertEqual(cm.exception.status, 400)

    def test_actions_unknown_platform(self) -> None:
        with self.assertRaises(ConsoleError) as cm:
            self.service.actions("ghost")
        self.assertEqual(cm.exception.kind, "unknown_platform")

    def test_unknown_action(self) -> None:
        with self.assertRaises(ConsoleError) as cm:
            self.service.run_action(GOOD, "nope", {})
        self.assertEqual(cm.exception.kind, "unknown_action")
        self.assertEqual(cm.exception.status, 404)

    def test_missing_required_param(self) -> None:
        with self.assertRaises(ConsoleError) as cm:
            self.service.run_action(GOOD, "ping", {})
        self.assertEqual(cm.exception.kind, "bad_request")
        self.assertIn("对象", str(cm.exception))

    def test_missing_credentials(self) -> None:
        self.unbind_all(GOOD)
        with self.assertRaises(ConsoleError) as cm:
            self.service.run_action(GOOD, "ping", {"who": "x"})
        self.assertEqual(cm.exception.kind, "no_credentials")

    def test_success(self) -> None:
        with mock.patch.dict(os.environ, _creds(GOOD), clear=True):
            out = self.service.run_action(GOOD, "ping", {"who": "world"})
        self.assertEqual(out["kind"], "message")
        self.assertEqual(out["message"], "pong:world")

    def test_platform_exception_becomes_platform_error(self) -> None:
        from kedaibiao.api.actions import ActionSpec, register

        async def _boom(adapter: Any, params: dict) -> dict:
            raise RuntimeError("平台炸了")

        register(ActionSpec(platform=GOOD, name="boom", label="炸"), _boom)
        with mock.patch.dict(os.environ, _creds(GOOD), clear=True):
            with self.assertRaises(ConsoleError) as cm:
                self.service.run_action(GOOD, "boom", {})
        self.assertEqual(cm.exception.kind, "platform_error")
        self.assertEqual(cm.exception.status, 502)
        self.assertIn("平台炸了", str(cm.exception))


class TestAssignmentDetailAndSubmit(ServiceTestBase):
    """单个作业：详情（双击）与提交（目前所有平台都未实现，必须明确拒绝）。"""

    def test_detail_returns_envelope(self) -> None:
        out = self.service.assignment_detail(GOOD, "a1")
        self.assertEqual(out["kind"], "detail")
        self.assertEqual(out["action"], "detail")
        labels = [f["label"] for f in out["fields"]]
        self.assertIn("任务号", labels)
        self.assertIn("题目数", labels)

    def test_detail_unknown_assignment(self) -> None:
        with self.assertRaises(ConsoleError) as cm:
            self.service.assignment_detail(GOOD, "nope")
        self.assertEqual(cm.exception.kind, "bad_request")
        self.assertIn("找不到", str(cm.exception))

    def test_detail_requires_id(self) -> None:
        with self.assertRaises(ConsoleError) as cm:
            self.service.assignment_detail(GOOD, "  ")
        self.assertEqual(cm.exception.kind, "bad_request")

    def test_submit_refused_when_platform_lacks_capability(self) -> None:
        """pigai / unipus 都是 submit=False —— 必须 501 明确拒绝，不能假装成功。"""
        with self.assertRaises(ConsoleError) as cm:
            self.service.submit_assignment(NOSUBMIT, "a1")
        self.assertEqual(cm.exception.kind, "not_supported")
        self.assertEqual(cm.exception.status, 501)
        self.assertIn("尚未实现提交", str(cm.exception))


class SubmitCapableAdapter(FakeAdapter):
    """假想一个「已实现提交」的平台，用来验证提交链路的接线是否正确。"""

    name = "apisubmit"
    display_name = "能提交的假平台"

    def __init__(self, ctx=None, **kw) -> None:
        super().__init__(ctx, **kw)
        self.submitted: list[tuple] = []

    async def submit(self, assignment, answers):
        self.submitted.append((assignment.remote_id, list(answers)))
        return SubmissionResult(assignment_id=assignment.remote_id, ok=True,
                                score=88.0, message="交卷成功")


class TestSubmitPipeline(unittest.IsolatedAsyncioTestCase):
    """提交链路的接线测试：用「能提交的假平台」跑通，证明平台一实现就能用。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "config").mkdir()
        (self.root / "config" / "local.toml").write_text(
            "[kedaibiao]\n"
            f'db_url = "sqlite:///{self.root}/x.db"\n'
            f'session_dir = "{self.root}/s"\n'
            'min_confidence = 0.5\n',
            encoding="utf-8",
        )
        adapters.register(SubmitCapableAdapter.name, SubmitCapableAdapter, override=True)
        self.addCleanup(adapters.unregister, SubmitCapableAdapter.name)

        from kedaibiao.security.accounts import AccountStore

        store = AccountStore(self.root / "x.db")
        self.account = store.add(0, SubmitCapableAdapter.name, username="stu")
        store.close()

        # 题库里预置答案，让作答链有货
        from kedaibiao.bank.store import QuestionBank
        bank = QuestionBank(self.root / "x.db")
        q = Question(stem="已知题", options=["甲", "乙"], remote_id="q1")
        bank.upsert(q, Answer(question=q, choices=[0],
                              source=AnswerSource.BANK_HIT, confidence=1.0))
        bank.close()
        self.service = ConsoleService(root=self.root)

    def test_dry_run_does_not_submit(self) -> None:
        out = self.service.submit_assignment(SubmitCapableAdapter.name, "a1")
        self.assertFalse(out["submitted"])
        self.assertTrue(out["dry_run"])
        self.assertEqual(out["answered"], 1)
        self.assertIn("干跑", out["note"])

    def test_confirm_submits(self) -> None:
        out = self.service.submit_assignment(
            SubmitCapableAdapter.name, "a1", confirm=True
        )
        self.assertTrue(out["submitted"])
        self.assertTrue(out["ok"])
        self.assertEqual(out["score"], 88.0)

    def test_low_confidence_blocks_confirm(self) -> None:
        """置信度不够时，即使 confirm=true 也拒绝提交。"""
        from kedaibiao.bank.store import QuestionBank

        bank = QuestionBank(self.root / "x.db")
        for row in bank.fetchall("SELECT owner_id, fingerprint FROM questions"):
            bank.execute("DELETE FROM questions WHERE fingerprint=?",
                         (row["fingerprint"],))
        bank.close()
        out = self.service.submit_assignment(
            SubmitCapableAdapter.name, "a1", confirm=True
        ) if False else None  # noqa: F841 - 下面用异常断言
        with self.assertRaises(ConsoleError) as cm:
            self.service.submit_assignment(
                SubmitCapableAdapter.name, "a1", confirm=True
            )
        self.assertEqual(cm.exception.status, 400)


class TestParamValidation(unittest.TestCase):
    def _spec(self):
        from kedaibiao.api.actions import ActionParam, ActionSpec

        return ActionSpec(
            platform="x", name="a", label="A",
            params=(
                ActionParam("rid", "编号", required=True),
                ActionParam("n", "数量", type="int"),
            ),
        )

    def test_int_coercion(self) -> None:
        out = ConsoleService._validate_params(self._spec(), {"rid": "1", "n": "7"})
        self.assertEqual(out, {"rid": "1", "n": 7})

    def test_bad_int(self) -> None:
        with self.assertRaises(ConsoleError):
            ConsoleService._validate_params(self._spec(), {"rid": "1", "n": "abc"})

    def test_optional_param_skipped_when_blank(self) -> None:
        out = ConsoleService._validate_params(self._spec(), {"rid": "1", "n": "  "})
        self.assertEqual(out, {"rid": "1"})

    def test_required_whitespace_rejected(self) -> None:
        with self.assertRaises(ConsoleError):
            ConsoleService._validate_params(self._spec(), {"rid": "   "})

    def test_non_dict_rejected(self) -> None:
        with self.assertRaises(ConsoleError):
            ConsoleService._validate_params(self._spec(), ["nope"])  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()


class TestBatchSubmit(ServiceTestBase):
    """一键全部提交（原生提交链路平台）的后台任务。"""

    def _wait(self, job: dict[str, Any]) -> dict[str, Any]:
        self.assertTrue(
            self.service.wait_for_idle(timeout=30), "批量任务没有在 30s 内结束"
        )
        return self.service.job(job["job_id"])

    def test_dry_run_counts_and_never_submits(self) -> None:
        NativeBatchAdapter.submitted = []
        job = self.service.start_batch_submit(NATIVE, confirm=False, interval=0.5)
        self.assertEqual(job["kind"], "batch-submit")
        done = self._wait(job)
        self.assertEqual(done["status"], "done", done)
        self.assertEqual(
            done["counts"],
            {"planned": 0, "submitted": 0, "skipped": 2, "blocked": 1, "failed": 0},
        )
        self.assertEqual(NativeBatchAdapter.submitted, [], "干跑绝不能真交")
        self.assertIn("可提交 0", done["summary"])

    def test_live_submits_only_not_started(self) -> None:
        """已完成（哪怕没全对）默认跳过：平台留存首条记录，重交不改分。"""
        NativeBatchAdapter.submitted = []
        job = self.service.start_batch_submit(NATIVE, confirm=True, interval=0.5)
        done = self._wait(job)
        self.assertEqual(done["status"], "done", done)
        self.assertEqual(done["counts"]["submitted"], 0)
        self.assertEqual(done["counts"]["skipped"], 2)
        self.assertEqual(done["counts"]["blocked"], 1)
        self.assertEqual(NativeBatchAdapter.submitted, [])

    def test_skip_done_false_resubmits(self) -> None:
        """显式关掉 skip_done 才会重交已完成但没全对的组。"""
        NativeBatchAdapter.submitted = []
        job = self.service.start_batch_submit(
            NATIVE, confirm=True, interval=0.5, skip_done=False
        )
        done = self._wait(job)
        self.assertEqual(done["status"], "done")
        self.assertEqual(done["counts"]["submitted"], 2)
        self.assertEqual(sorted(NativeBatchAdapter.submitted), ["a1", "a2"])

    def test_course_filter_mismatch_fails_softly(self) -> None:
        job = self.service.start_batch_submit(NATIVE, course="不存在的课", interval=0.5)
        done = self._wait(job)
        self.assertEqual(done["status"], "failed")
        self.assertIn("没有匹配的课程", done["error"])

    def test_limit_caps_processed_tasks(self) -> None:
        NativeBatchAdapter.submitted = []
        job = self.service.start_batch_submit(NATIVE, limit=1, interval=0.5)
        done = self._wait(job)
        self.assertEqual(done["status"], "done")
        # limit=1 → 只处理第一个任务（a1 已满分 → 跳过）
        self.assertEqual(done["counts"]["skipped"], 1)
        self.assertEqual(done["counts"]["planned"], 0)
        self.assertEqual(NativeBatchAdapter.submitted, [])

    def test_score_pct_one_but_not_all_correct_is_still_skipped(self) -> None:
        """平台 score_pct=1 不代表全对（实测语义不可靠）→ 未全对时日志要写明。"""
        NativeBatchAdapter.submitted = []
        job = self.service.start_batch_submit(NATIVE, confirm=False, interval=0.5)
        done = self._wait(job)
        texts = " ".join(line["text"] for line in done["logs"])
        self.assertIn("已完成但未全对", texts)

    def test_platform_without_native_flow_is_501(self) -> None:
        with self.assertRaises(ConsoleError) as ctx:
            self.service.start_batch_submit(GOOD)
        self.assertEqual(ctx.exception.status, 501)
        self.assertEqual(ctx.exception.kind, "not_supported")

    def test_platform_without_submit_is_501(self) -> None:
        with self.assertRaises(ConsoleError) as ctx:
            self.service.start_batch_submit(NOSUBMIT)
        self.assertEqual(ctx.exception.status, 501)

    def test_unknown_platform_is_404(self) -> None:
        with self.assertRaises(ConsoleError) as ctx:
            self.service.start_batch_submit("ghost")
        self.assertEqual(ctx.exception.status, 404)
