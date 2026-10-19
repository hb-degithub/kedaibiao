"""平台执行日志测试（离线）。

控制台每个平台各有一份「详细日志」：动作调用、跑批、评分循环、一键自动化都写进去。
这里锁：按平台隔离、条数上限、失败也留痕、未知平台报错。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kedaibiao.api.actions import ActionSpec, message, register
from kedaibiao.api.service import (
    PLATFORM_LOG_HISTORY,
    ConsoleError,
    ConsoleService,
    Viewer,
)

SUPER = Viewer(user_id=1, is_superadmin=True, username="admin")


def make_service(root: Path) -> ConsoleService:
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "config" / "local.toml").write_text(
        "[kedaibiao]\n"
        f'db_url = "sqlite:///{root}/logs.db"\n'
        f'session_dir = "{root}/sessions"\n'
        'notify = "null"\n',
        encoding="utf-8",
    )
    return ConsoleService(root=root)


class TestPlatformLogStore(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.service = make_service(self.root)

    def test_isolated_per_platform(self) -> None:
        self.service._plog("itest", "info", "itest 的一行")
        self.service._plog("pigai", "ok", "pigai 的一行")
        itest = self.service.platform_logs("itest")
        pigai = self.service.platform_logs("pigai")
        self.assertEqual([x["text"] for x in itest["logs"]], ["itest 的一行"])
        self.assertEqual([x["text"] for x in pigai["logs"]], ["pigai 的一行"])
        self.assertEqual(self.service.platform_logs("pinco")["logs"], [])

    def test_history_is_capped(self) -> None:
        for i in range(PLATFORM_LOG_HISTORY + 20):
            self.service._plog("itest", "info", f"第 {i} 行")
        out = self.service.platform_logs("itest", limit=400)
        self.assertEqual(len(out["logs"]), PLATFORM_LOG_HISTORY)
        self.assertEqual(out["logs"][-1]["text"], f"第 {PLATFORM_LOG_HISTORY + 19} 行")

    def test_limit_returns_tail(self) -> None:
        for i in range(10):
            self.service._plog("itest", "info", f"行 {i}")
        out = self.service.platform_logs("itest", limit=3)
        self.assertEqual([x["text"] for x in out["logs"]], ["行 7", "行 8", "行 9"])
        self.assertEqual(out["total"], 10)

    def test_unknown_platform_rejected(self) -> None:
        with self.assertRaises(ConsoleError) as ctx:
            self.service.platform_logs("nope")
        self.assertEqual(ctx.exception.status, 404)

    def test_blank_platform_ignored(self) -> None:
        self.service._plog("", "info", "无主")
        self.assertEqual(self.service.platform_logs("itest")["logs"], [])


class TestActionLogging(unittest.TestCase):
    """动作层每次调用都应留下「开始 + 结果」，失败也要留痕。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.service = make_service(self.root)

        async def ok_handler(adapter, params):
            return message(platform="itest", action="probe", label="探针",
                           message="pong")

        async def boom_handler(adapter, params):
            raise ConsoleError("平台说不行", kind="platform_error", status=502)

        register(
            ActionSpec(platform="itest", name="probe", label="探针",
                       params=(), local=True),
            ok_handler,
        )
        register(
            ActionSpec(platform="itest", name="boom", label="会炸", params=(), local=True),
            boom_handler,
        )

    def test_success_writes_two_lines(self) -> None:
        self.service.run_action("itest", "probe", {}, viewer=SUPER)
        texts = [x["text"] for x in self.service.platform_logs("itest")["logs"]]
        self.assertTrue(any(t.startswith("$ 动作 probe") for t in texts), texts)
        self.assertTrue(any(t.startswith("→ probe") for t in texts), texts)

    def test_failure_is_logged_with_error_level(self) -> None:
        with self.assertRaises(ConsoleError):
            self.service.run_action("itest", "boom", {}, viewer=SUPER)
        logs = self.service.platform_logs("itest")["logs"]
        self.assertTrue(any(x["level"] == "error" and "平台说不行" in x["text"]
                            for x in logs), logs)

    def test_unknown_action_does_not_pollute_log(self) -> None:
        with self.assertRaises(ConsoleError):
            self.service.run_action("itest", "does-not-exist", {}, viewer=SUPER)
        self.assertEqual(self.service.platform_logs("itest")["logs"], [])


class TestAutoJobLogsMirror(unittest.TestCase):
    """一键自动化的 job logs 也进平台日志（切平台后仍能翻到）。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.service = make_service(self.root)

    def test_job_logs_are_mirrored(self) -> None:
        with mock.patch.object(ConsoleService, "_run_itest_auto", autospec=True) as runner:
            def fake(self_, job_id, exam_id, viewer, wait):
                self_._job_log(job_id, "info", "自动化第一行")
                with self_._lock:
                    self_._jobs_by_id[job_id]["status"] = "succeeded"
            runner.side_effect = fake
            self.service.start_itest_auto("1099", viewer=SUPER)
        texts = [x["text"] for x in self.service.platform_logs("itest")["logs"]]
        self.assertIn("自动化第一行", texts)


if __name__ == "__main__":
    unittest.main()
