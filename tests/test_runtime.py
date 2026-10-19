"""运行时装配与 run 命令的端到端测试。

这一层是把所有层拼起来的地方，所以测试也走完整链路：
注册一个假适配器 → 通过 CLI 的 main() 真跑一次 → 校验落库与退出码。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kedaibiao import adapters
from kedaibiao.__main__ import _parse_kinds, main
from kedaibiao.core.models import (
    Answer,
    AnswerSource,
    Assignment,
    Course,
    Credentials,
    Question,
    QuestionType,
    SubmissionResult,
    TaskKind,
)
from kedaibiao.notify.base import ConsoleNotifier, NullNotifier, WebhookNotifier
from kedaibiao.platforms.base import Capabilities, PlatformAdapter
from kedaibiao.runtime import build_notifier, build_runtime
from kedaibiao.config.settings import Settings

PLATFORM = "fakeruntime"


class FakeAdapter(PlatformAdapter):
    name = PLATFORM
    display_name = "假运行时平台"
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
        return [
            Question(stem="已知题", type=QuestionType.SINGLE, options=["甲", "乙"], remote_id="q1")
        ]

    async def submit(self, assignment: Assignment, answers: list[Answer]) -> SubmissionResult:
        return SubmissionResult(assignment_id=assignment.remote_id, ok=True, score=100.0)


class NotifierFactoryTest(unittest.TestCase):
    def test_default_is_console(self) -> None:
        self.assertIsInstance(build_notifier(Settings()), ConsoleNotifier)

    def test_null(self) -> None:
        self.assertIsInstance(build_notifier(Settings(notify="null")), NullNotifier)

    def test_webhook_without_url_degrades_to_console(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsInstance(build_notifier(Settings(notify="webhook")), ConsoleNotifier)

    def test_webhook_with_url(self) -> None:
        env = {"KEDAIBIAO_NOTIFY_WEBHOOK_URL": "https://example.invalid/hook"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertIsInstance(build_notifier(Settings(notify="webhook")), WebhookNotifier)

    def test_unknown_kind_falls_back_to_console(self) -> None:
        self.assertIsInstance(build_notifier(Settings(notify="whatever")), ConsoleNotifier)


class ParseKindsTest(unittest.TestCase):
    def test_empty_means_all(self) -> None:
        self.assertEqual(_parse_kinds(""), frozenset())

    def test_single(self) -> None:
        self.assertEqual(_parse_kinds("exam"), frozenset({TaskKind.EXAM}))

    def test_multiple_with_spaces(self) -> None:
        self.assertEqual(
            _parse_kinds(" homework , exam "), frozenset({TaskKind.HOMEWORK, TaskKind.EXAM})
        )

    def test_invalid_raises(self) -> None:
        with self.assertRaises(ValueError):
            _parse_kinds("bogus")


class RuntimeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.settings = Settings(db_url=f"sqlite:///{self.root}/rt.db",
                                 session_dir=str(self.root / "sessions"))

    def test_unknown_platform_raises_before_credentials(self) -> None:
        runtime = build_runtime(root=self.root, settings=self.settings)
        self.addCleanup(runtime.close)
        with self.assertRaises(KeyError) as cm:
            runtime.make_adapter("does-not-exist")
        self.assertIn("does-not-exist", str(cm.exception))

    def test_make_adapter_creates_independent_context(self) -> None:
        adapters.register(PLATFORM, FakeAdapter, override=True)
        self.addCleanup(adapters.unregister, PLATFORM)
        runtime = build_runtime(root=self.root, settings=self.settings)
        self.addCleanup(runtime.close)

        a1 = runtime.make_adapter(PLATFORM)
        a2 = runtime.make_adapter(PLATFORM)
        self.assertIsInstance(a1, FakeAdapter)
        self.assertIsNot(a1.ctx, a2.ctx)
        self.assertIsNot(a1.ctx.http, a2.ctx.http, "cookie 不能跨适配器实例共享")

    def test_run_passes_credentials_through(self) -> None:
        import asyncio

        adapters.register(PLATFORM, FakeAdapter, override=True)
        self.addCleanup(adapters.unregister, PLATFORM)
        runtime = build_runtime(root=self.root, settings=self.settings)
        self.addCleanup(runtime.close)

        report = asyncio.run(runtime.run(PLATFORM, credentials=Credentials("u", "p")))
        self.assertEqual(len(report.records), 1)


class CliRunIntegrationTest(unittest.TestCase):
    """通过 main() 走完整的命令行链路（干跑，不产生写操作）。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "config").mkdir()
        (self.root / "config" / "local.toml").write_text(
            "[kedaibiao]\n"
            f'db_url = "sqlite:///{self.root}/cli.db"\n'
            f'session_dir = "{self.root}/sessions"\n'
            "dry_run = true\n"
            "notify = \"null\"\n",
            encoding="utf-8",
        )
        # 题库里预置一道答案，让流水线能走到「干跑」这一步而不是「一题都没答出来」
        runtime = build_runtime(root=self.root)
        q = Question(stem="已知题", options=["甲", "乙"], remote_id="q1")
        runtime.bank.upsert(
            q, Answer(question=q, choices=[0], source=AnswerSource.BANK_HIT, confidence=1.0)
        )
        runtime.close()

        adapters.register(PLATFORM, FakeAdapter, override=True)
        self.addCleanup(adapters.unregister, PLATFORM)

        # 凭据只来自账号库（不再读 .env）：给这个假平台绑一个号
        from kedaibiao.security.accounts import AccountStore

        store = AccountStore(self.root / "cli.db")
        try:
            store.add(0, PLATFORM, label="测试号", username="stu")
        finally:
            store.close()

    def _run(self, argv: list[str], env: dict[str, str]) -> int:
        with mock.patch.dict(os.environ, env, clear=True):
            return main(argv)

    def test_dry_run_skips_and_exits_zero(self) -> None:
        code = self._run(["run", PLATFORM, "--root", str(self.root)], {})
        self.assertEqual(code, 0)

    # 「没有凭据时退出码 2」已由 test_cli_fails_when_platform_has_no_account 覆盖

    def test_unknown_platform_exits_two(self) -> None:
        code = self._run(
            ["run", "ghost", "--root", str(self.root)],
            {"KEDAIBIAO_GHOST_USER": "u"},
        )
        self.assertEqual(code, 2)

    def test_invalid_kinds_exits_two(self) -> None:
        code = self._run(
            ["run", PLATFORM, "--root", str(self.root), "--kinds", "bogus"],
            {f"KEDAIBIAO_{PLATFORM.upper()}_USER": "u"},
        )
        self.assertEqual(code, 2)

    def test_limit_is_honoured(self) -> None:
        code = self._run(
            ["run", PLATFORM, "--root", str(self.root), "--limit", "0"], {}
        )
        self.assertEqual(code, 0)

    def test_live_flag_is_required_to_disable_dry_run(self) -> None:
        """--live 必须真的把 dry_run 关掉，否则「全自动」就是假的。"""
        import asyncio

        from kedaibiao.tasks.pipeline import PipelineConfig

        settings = Settings(dry_run=False, db_url=f"sqlite:///{self.root}/live.db",
                            session_dir=str(self.root / "s"))
        runtime = build_runtime(
            root=self.root,
            settings=settings,
            config=PipelineConfig(dry_run=False, min_confidence=0.75),
        )
        self.addCleanup(runtime.close)
        q = Question(stem="已知题", options=["甲", "乙"], remote_id="q1")
        runtime.bank.upsert(
            q, Answer(question=q, choices=[0], source=AnswerSource.BANK_HIT, confidence=1.0)
        )
        report = asyncio.run(runtime.run(PLATFORM, credentials=Credentials("u", "p")))
        self.assertEqual(report.succeeded, 1)

    def test_credentials_come_from_account_store(self) -> None:
        """CLI 的凭据来自账号库，而不是 .env。

        即使 .env 里写了平台账号密码，也不该被使用 —— 凭据只有一个来源。
        """
        (self.root / ".env").write_text(
            f"KEDAIBIAO_{PLATFORM.upper()}_USER=should-not-be-used\n"
            f"KEDAIBIAO_{PLATFORM.upper()}_PASS=should-not-be-used\n",
            encoding="utf-8",
        )
        code = self._run(["run", PLATFORM, "--root", str(self.root)], {})
        self.assertEqual(code, 0)
        # 库里的账号是唯一来源（用户名来自账号库那条）
        from kedaibiao.security.accounts import AccountStore

        store = AccountStore(self.root / "cli.db")
        try:
            accounts = store.list_for(None, PLATFORM)
        finally:
            store.close()
        self.assertEqual(accounts[0].username, "stu", "用的必须是账号库里的号")

    def test_cli_fails_when_platform_has_no_account(self) -> None:
        from kedaibiao.security.accounts import AccountStore

        store = AccountStore(self.root / "cli.db")
        try:
            for acc in store.list_for(None, PLATFORM):
                store.delete(acc.id)
        finally:
            store.close()
        code = self._run(["run", PLATFORM, "--root", str(self.root)], {})
        self.assertEqual(code, 2, "没有绑定账号应当明确失败，而不是偷偷去读 .env")

    def test_dotenv_does_not_leak_into_os_environ(self) -> None:
        key = f"KEDAIBIAO_{PLATFORM.upper()}_USER"
        (self.root / ".env").write_text(f"{key}=fromfile\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {}, clear=True):
            self._run(["run", PLATFORM, "--root", str(self.root)], {})
            self.assertNotIn(key, os.environ, "不该有全局副作用")

    def test_resolve_environ_explicit_wins_over_dotenv(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            runtime = build_runtime(
                root=self.root,
                environ={f"KEDAIBIAO_{PLATFORM.upper()}_USER": "explicit"},
            )
        self.addCleanup(runtime.close)
        self.assertEqual(runtime.environ[f"KEDAIBIAO_{PLATFORM.upper()}_USER"], "explicit")


if __name__ == "__main__":
    unittest.main()
