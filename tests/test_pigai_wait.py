"""批改网「等分出超时」的补救逻辑（离线）。

用户要求：取不到分数时，能选「手动获取」或「继续等待 N 秒」。
这里锁后端的两半：
- 评分循环把 AnalysisTimeout 单独标成 stop_reason=timeout（不是 error，也不是失败）；
- wait_essay_grade 可重复等待：超时返回 timed_out=True（HTTP 200），出分返回快照。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kedaibiao.api.service import ConsoleError, ConsoleService, Viewer
from kedaibiao.platforms.pigai import grading
from kedaibiao.platforms.pigai.grade_loop import STOP_TIMEOUT

SUPER = Viewer(user_id=1, is_superadmin=True, username="admin")


def make_service(root: Path) -> ConsoleService:
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "config" / "local.toml").write_text(
        "[kedaibiao]\n"
        f'db_url = "sqlite:///{root}/w.db"\n'
        f'session_dir = "{root}/sessions"\n'
        'notify = "null"\n',
        encoding="utf-8",
    )
    return ConsoleService(root=root)


class FakeSnapshot:
    def __init__(self, score: float | None, graded: bool, state: str = "") -> None:
        self._score = score
        self.graded = graded
        self.raw_state = state

    def to_dict(self) -> dict:
        return {
            "score": self._score, "graded": self.graded,
            "raw_state": self.raw_state, "feedback_lines": [],
        }


class TestWaitEssayGrade(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = make_service(Path(self.tmp.name))
        # 绕开真实登录：_essay_http 返回假 adapter/runtime
        from types import SimpleNamespace

        adapter = SimpleNamespace(
            ctx=SimpleNamespace(client=lambda: object()),
            close=mock.AsyncMock(),
        )
        runtime = SimpleNamespace(close=lambda: None)
        patcher = mock.patch.object(
            ConsoleService, "_essay_http", lambda *a, **k: (adapter, runtime, None)
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_timeout_returns_timed_out_not_error(self) -> None:
        with mock.patch.object(
            grading, "wait_for_grade",
            side_effect=grading.AnalysisTimeout("等待分析超时（120s）"),
        ):
            out = self.service.wait_essay_grade("1000001", "9000001", timeout=120,
                                                viewer=SUPER)
        self.assertTrue(out["timed_out"])
        self.assertFalse(out["graded"])
        self.assertIn("等待分析超时", out["message"])
        self.assertIn("可以再等", out["message"])

    def test_success_returns_snapshot(self) -> None:
        async def fake_wait(*a, **k):
            return FakeSnapshot(92.0, True, "已批改")

        with mock.patch.object(grading, "wait_for_grade", fake_wait):
            out = self.service.wait_essay_grade("1000001", "9000001", timeout=60,
                                                viewer=SUPER)
        self.assertFalse(out["timed_out"])
        self.assertEqual(out["score"], 92.0)
        self.assertTrue(out["graded"])

    def test_not_graded_is_not_an_error(self) -> None:
        async def fake_wait(*a, **k):
            return FakeSnapshot(None, False, "分析中")

        with mock.patch.object(grading, "wait_for_grade", fake_wait):
            out = self.service.wait_essay_grade("1000001", "", timeout=30, viewer=SUPER)
        self.assertFalse(out["timed_out"])
        self.assertFalse(out["graded"])
        self.assertEqual(out["raw_state"], "分析中")

    def test_bounds_and_required(self) -> None:
        with self.assertRaises(ConsoleError):
            self.service.wait_essay_grade("", "", viewer=SUPER)
        with self.assertRaises(ConsoleError):
            self.service.wait_essay_grade("1", "", timeout=1, viewer=SUPER)
        with self.assertRaises(ConsoleError):
            self.service.wait_essay_grade("1", "", timeout=99999, viewer=SUPER)

    def test_only_pigai_supported(self) -> None:
        with self.assertRaises(ConsoleError) as ctx:
            self.service.wait_essay_grade("1", platform="itest", viewer=SUPER)
        self.assertEqual(ctx.exception.status, 501)


class TestGradeLoopTimeout(unittest.TestCase):
    def test_timeout_reason_is_distinct(self) -> None:
        """AnalysisTimeout 必须落到 STOP_TIMEOUT，而不是 STOP_ERROR。"""

        from kedaibiao.platforms.pigai import grade_loop as gl

        self.assertEqual(STOP_TIMEOUT, "timeout")
        self.assertNotEqual(gl.STOP_TIMEOUT, gl.STOP_ERROR)


class TestStartGradeLoopTimeoutParam(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = make_service(Path(self.tmp.name))

    def test_bad_timeout_rejected(self) -> None:
        with self.assertRaises(ConsoleError) as ctx:
            self.service.start_grade_loop(
                "pigai", "1000001", target_score=90, analysis_timeout=5, viewer=SUPER
            )
        self.assertEqual(ctx.exception.status, 400)

    def test_job_records_timeout(self) -> None:
        with mock.patch.object(ConsoleService, "_resolve_login",
                               lambda *a, **k: (None, None)), \
             mock.patch.object(ConsoleService, "_run_grade_loop", autospec=True):
            job = self.service.start_grade_loop(
                "pigai", "1000001", target_score=90, analysis_timeout=300, viewer=SUPER
            )
        self.assertEqual(job["analysis_timeout"], 300.0)


if __name__ == "__main__":
    unittest.main()
