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
        for cls in (FakeAdapter, NoSubmitAdapter, SlowAdapter):
            adapters.register(cls.name, cls, override=True)
            self.addCleanup(adapters.unregister, cls.name)
        self.service = ConsoleService(root=self.root)

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
    def test_empty_state_does_not_create_db(self) -> None:
        data = self.service.overview()
        self.assertFalse(data["db_exists"])
        self.assertEqual(data["bank"]["total"], 0)
        self.assertIsNone(data["bank"]["accuracy"])
        self.assertEqual(data["runs"]["total"], 0)
        self.assertFalse((self.root / "api.db").exists(), "只读接口不该建库")

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
        with mock.patch.dict(os.environ, _creds(GOOD), clear=True):
            names = {p["name"]: p for p in self.service.platforms()["platforms"]}
        self.assertTrue(names[GOOD]["has_credentials"])
        self.assertFalse(names[NOSUBMIT]["has_credentials"])


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
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ConsoleError) as cm:
                self.service.start_run(GOOD)
        self.assertEqual(cm.exception.kind, "no_credentials")
        self.assertIn("KEDAIBIAO_APIFAKE_USER", str(cm.exception))

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
        self.assertEqual([a["name"] for a in data["actions"]], ["ping"])
        self.assertTrue(data["actions"][0]["params"][0]["required"])

    def test_actions_for_platform_without_any(self) -> None:
        self.assertEqual(self.service.actions(NOSUBMIT)["actions"], [])

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
        with mock.patch.dict(os.environ, {}, clear=True):
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
