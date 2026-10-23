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


class _Text:
    def __init__(self, text: str) -> None:
        self._text = text

    def text(self) -> str:
        return self._text


class _PollHTTP:
    """假 HTTP：``process`` 一直回「分析中 + sleep 12000ms」。"""

    def __init__(self, states: list[str] | None = None) -> None:
        self.calls = 0
        self.states = list(states or [])

    async def request(self, method: str, path: str, **kwargs: object) -> _Text:
        self.calls += 1
        state = self.states.pop(0) if self.states else "doing"
        if state in ("ok", "done"):
            return _Text('{"s":"ok"}')
        return _Text('{"s":"' + state + '","sleep":12000}')


class TestFastEarlyPolling(unittest.IsolatedAsyncioTestCase):
    """平台建议 12 秒一轮，但头几次探密一点能更早拿到分。"""

    async def test_first_polls_are_capped_then_follow_server(self) -> None:
        sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        # 第 6 次轮询才完成：足够看到「前 3 次提速 + 之后回落到 12s」
        http = _PollHTTP(states=["doing"] * 5 + ["ok"])
        with mock.patch.object(grading.asyncio, "sleep", fake_sleep):
            await grading.poll_analysis(http, base="https://www.pigai.org",
                                        rid="R", key="K", timeout=999)

        self.assertTrue(sleeps, "至少要睡一次")
        fast = sleeps[: grading.FAST_POLLS]
        self.assertTrue(all(s <= grading.FAST_POLL_CAP for s in fast),
                        f"前 {grading.FAST_POLLS} 次该被压到 {grading.FAST_POLL_CAP}s：{fast}")
        if len(sleeps) > grading.FAST_POLLS:
            self.assertEqual(sleeps[grading.FAST_POLLS], 12.0,
                             "之后回到服务端建议的节奏")

    async def test_fast_polls_can_be_disabled(self) -> None:
        sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        http = _PollHTTP(states=["doing", "ok"])
        with mock.patch.object(grading.asyncio, "sleep", fake_sleep), \
             mock.patch.object(grading, "FAST_POLLS", 0):
            await grading.poll_analysis(http, base="", rid="R", key="K", timeout=999)

        self.assertEqual(sleeps[0], 12.0, "关掉提速后完全按服务端节奏")


class TestResumeKeySkipsTrigger(unittest.IsolatedAsyncioTestCase):
    """超时延长时用旧 key 续等，不重复触发分析。"""

    async def test_resume_key_does_not_call_start_analysis(self) -> None:
        http = _PollHTTP(states=["ok"])
        triggered: list[str] = []

        async def fake_start(*a: object, **k: object) -> str:
            triggered.append("start")
            return "K-NEW"

        class _Adapter:
            async def list_finished_essays(self) -> list[object]:
                return []

        async def fake_fetch(*a: object, **k: object) -> object:
            return FakeSnapshot(91.0, True, "已批改")

        with mock.patch.object(grading, "start_analysis", fake_start), \
             mock.patch.object(grading, "fetch_snapshot", fake_fetch):
            snap = await grading.wait_for_grade(
                _Adapter(), http, base="", rid="R", eid="E", timeout=5,
                resume_key="K-OLD",
            )

        self.assertEqual(triggered, [], "续等不该再触发 processStartIcomet")
        self.assertTrue(snap.graded)


class TestAutoExtendAfterTimeout(unittest.IsolatedAsyncioTestCase):
    """等分超时后自动再等一轮；延长期间出分就按正常结果继续。"""

    async def test_extends_once_then_succeeds(self) -> None:
        from kedaibiao.platforms.pigai.grade_loop import GradeLoop
        calls: list[float] = []

        async def wait(*a: object, **k: object) -> object:
            calls.append(float(k.get("timeout") or 0))
            if len(calls) == 1:
                raise grading.AnalysisTimeout("等待分析超时（180s）")
            snap = FakeSnapshot(93.0, True, "已批改")
            snap.raw_state = "已批改"
            return snap

        async def no_score(*a: object, **k: object) -> object:
            return FakeSnapshot(None, False, "分析中")

        async def fetch(*a: object, **k: object) -> object:
            return FakeSnapshot(None, False, "分析中")

        class _Adapter:
            submits = 0

            async def submit(self, assignment: object, answers: object) -> object:
                from types import SimpleNamespace
                self.submits += 1
                return SimpleNamespace(ok=True, message="ok", raw={"eid": "E-OLD"})

            async def list_finished_essays(self) -> list[object]:
                # 没有旧稿：直接进「提交初始正文 → 等分」的分支，
                # 这才是超时延长要覆盖的路径（旧稿取分是另一条路）
                return []

            async def list_pending_assignments(self) -> list[object]:
                from kedaibiao.core.models import Assignment
                return [Assignment(remote_id="R", title="T")]

        async def rewrite(snap: object, essay: str, n: int) -> str:
            return "v1"

        loop = GradeLoop(adapter=_Adapter(), http=object(), base="",
                         target_score=90, max_attempts=1, rewrite=rewrite)
        events: list[dict] = []
        loop.on_event = events.append

        with mock.patch.object(grading, "wait_for_grade", wait), \
             mock.patch.object(grading, "fetch_snapshot", fetch):
            result = await loop.run(rid="R", topic="T", initial_essay="v0")

        kinds = [e["kind"] for e in events]
        self.assertIn("attempt.extending", kinds, "超时后应当自动延长一轮")
        self.assertEqual(result.stop_reason, "reached", "延长期间拿到分就不算超时")
        self.assertEqual(result.best_score, 93.0, f"calls={calls}")


class TestExistingEssayAlsoExtends(unittest.IsolatedAsyncioTestCase):
    """旧稿取分超时也走「抢救 + 自动延长」，不要一超时就整轮结束。"""

    async def test_existing_pending_extends_then_scores(self) -> None:
        from kedaibiao.platforms.pigai.grade_loop import GradeLoop
        from kedaibiao.platforms.pigai.protocol import EssayRow

        calls = {"wait": 0}

        async def wait(*a: object, **k: object) -> object:
            calls["wait"] += 1
            if calls["wait"] == 1:
                raise grading.AnalysisTimeout("等待分析超时（180s）")
            return FakeSnapshot(96.0, True, "已批改")

        async def fetch(*a: object, **k: object) -> object:
            return FakeSnapshot(None, False, "分析中")

        class _Adapter:
            async def submit(self, assignment: object, answers: object) -> object:
                raise AssertionError("拿到分之前不该提交")

            async def list_finished_essays(self) -> list[object]:
                return [EssayRow(rid="R", eid="E-OLD", title="T", score_raw="未截止")]

        loop = GradeLoop(adapter=_Adapter(), http=object(), base="",
                         target_score=90, max_attempts=1, rewrite=None)
        events: list[dict] = []
        loop.on_event = events.append

        with mock.patch.object(grading, "wait_for_grade", wait), \
             mock.patch.object(grading, "fetch_snapshot", fetch):
            result = await loop.run(rid="R", topic="T", initial_essay="v0")

        kinds = [e["kind"] for e in events]
        self.assertIn("attempt.extending", kinds)
        self.assertEqual(result.stop_reason, "reached", "延长后拿到 96 分")
        self.assertEqual(result.best_score, 96.0)
