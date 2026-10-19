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


class TestSnapshotAndRewriteLogging(unittest.TestCase):
    """分数、评价、正文、改写提示词与结果都要能落到平台日志里（用户要能看见）。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = make_service(Path(self.tmp.name))

    class _Snap:
        def to_dict(self):
            return {
                "score": 83.5, "full_marks": 100.0, "raw_state": "未截止",
                "dimensions": [{"name": "词汇", "ratio": 0.94},
                               {"name": "内容相关", "ratio": 0.53}],
                "overall_comment": "行文稍欠流畅，应适当增加连接词的使用。",
                "feedback_lines": ["[confusion] 易混词汇", "[i_suggest.word] 换词"],
                "essay_text": "This is the essay body. It has two sentences.",
            }

    def test_snapshot_details_logged(self) -> None:
        self.service._log_snapshot("pigai", "1000001", "9000001", self._Snap())
        text = "\n".join(x["text"] for x in self.service.platform_logs("pigai")["logs"])
        self.assertIn("83.5", text)
        self.assertIn("维度：", text)
        self.assertIn("词汇 94%", text)
        self.assertIn("总评：", text)
        self.assertIn("批注 2 条", text)
        self.assertIn("易混词汇", text)
        self.assertIn("作文正文", text)
        self.assertIn("This is the essay body", text)

    def test_ungraded_snapshot_logs_state_only(self) -> None:
        class Snap:
            def to_dict(self):
                return {"score": None, "raw_state": "待分析"}

        self.service._log_snapshot("pigai", "1", "2", Snap())
        logs = self.service.platform_logs("pigai")["logs"]
        self.assertTrue(any(x["level"] == "warn" and "暂无分数" in x["text"] for x in logs))

    def test_plog_long_splits_and_keeps_content(self) -> None:
        body = "\n".join(f"第 {i} 行内容" for i in range(5))
        self.service._plog_long("pigai", "info", body)
        logs = self.service.platform_logs("pigai")["logs"]
        self.assertEqual(len(logs), 5)
        self.assertIn("第 4 行内容", logs[-1]["text"])

    def test_plog_long_wraps_very_long_line(self) -> None:
        self.service._plog_long("pigai", "info", "长" * 700, max_chars=1000)
        logs = self.service.platform_logs("pigai")["logs"]
        self.assertGreaterEqual(len(logs), 3, "超长行要拆成多行，不能整段截断")
        self.assertTrue(all(len(x["text"]) <= 300 for x in logs))
