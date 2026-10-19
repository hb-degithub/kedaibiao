"""批改网「平台拒绝分析」要软着陆（离线）。

现象：点「只取分」或评分循环里触发分析时，平台回 `msg:没找到作文！`
（常见于这篇早就批过了，或 eid 对不上分析队列）。
原来会当成致命错误往上抛 → 界面「失败 AnalysisFailed」。现在：

- `wait_for_grade` 不抛，返回当前快照 + ``analysis_error``；
- `fetch_essay_grade` 返回 200（graded=False + analysis_error），前端显示可读原因；
- `grade_loop` 把这种情况当作「暂时没分」：停成 unavailable，而不是 error。
"""

from __future__ import annotations

import unittest
from unittest import mock

from kedaibiao.platforms.pigai import grading
from kedaibiao.platforms.pigai.grade_loop import GradeLoop
from kedaibiao.platforms.pigai.protocol import EssayRow


class TestWaitForGradeSoft(unittest.IsolatedAsyncioTestCase):
    async def test_rejection_returns_snapshot_with_error(self) -> None:
        async def fake_snapshot(*a, **k):
            return grading.EssaySnapshot(rid="R", eid="E", raw_state="待分析")

        async def fake_start(*a, **k):
            raise grading.AnalysisFailed("平台拒绝分析：msg:没找到作文！")

        with mock.patch.object(grading, "fetch_snapshot", fake_snapshot), \
             mock.patch.object(grading, "start_analysis", fake_start):
            snap = await grading.wait_for_grade(
                object(), object(), base="https://x", rid="R", eid="E", timeout=5
            )
        self.assertFalse(snap.graded)
        self.assertIn("没找到作文", snap.analysis_error)
        self.assertEqual(snap.to_dict()["analysis_error"], snap.analysis_error)


class _Adapter:
    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.submits = 0

    async def list_finished_essays(self):
        return self.rows

    async def submit(self, assignment, answers):
        self.submits += 1

        class R:
            ok = True
            message = "ok"
            raw = {"eid": "E-NEW"}
        return R()

    async def list_pending_assignments(self):
        from kedaibiao.core.models import Assignment
        return [Assignment(remote_id="R", title="T")]


class TestGradeLoopSoft(unittest.IsolatedAsyncioTestCase):
    async def test_probe_rejection_is_unavailable_not_error(self) -> None:
        adapter = _Adapter([EssayRow(rid="R", eid="E", title="T", score_raw="待分析")])

        async def fake_snapshot(*a, **k):
            return grading.EssaySnapshot(rid="R", eid="E", raw_state="待分析")

        async def fake_start(*a, **k):
            raise grading.AnalysisFailed("平台拒绝分析：msg:没找到作文！")

        async def _rewrite(snap, essay, n):
            return f"v{n}"

        loop = GradeLoop(adapter=adapter, http=object(), base="", target_score=90,
                         max_attempts=1, rewrite=_rewrite)
        with mock.patch.object(grading, "fetch_snapshot", fake_snapshot), \
             mock.patch.object(grading, "start_analysis", fake_start):
            result = await loop.run(rid="R", topic="T", initial_essay="v1")

        self.assertEqual(result.stop_reason, "unavailable")
        self.assertEqual(adapter.submits, 0, "分析被拒时不能重交")
        self.assertNotEqual(result.stop_reason, "error")

    async def test_analysis_rejected_after_submit_is_unavailable(self) -> None:
        adapter = _Adapter([])

        async def fake_start(*a, **k):
            raise grading.AnalysisFailed("平台拒绝分析：msg:没找到作文！")

        async def fake_snapshot(*a, **k):
            return grading.EssaySnapshot(rid="R", eid="E", raw_state="待分析")

        loop = GradeLoop(adapter=adapter, http=object(), base="", target_score=90,
                         max_attempts=1, rewrite=None)
        with mock.patch.object(grading, "fetch_snapshot", fake_snapshot), \
             mock.patch.object(grading, "start_analysis", fake_start):
            result = await loop.run(rid="R", topic="T", initial_essay="v1")

        self.assertEqual(adapter.submits, 1, "没有旧稿时先交一次")
        self.assertEqual(result.stop_reason, "unavailable")
        self.assertIn("没找到作文", result.message)


if __name__ == "__main__":
    unittest.main()


class TestListScoreFallback(unittest.IsolatedAsyncioTestCase):
    """老作文详情页读不到时，分数要回落到「已提交列表」，不能装作没分。"""

    async def test_parse_error_uses_list_score(self) -> None:
        from kedaibiao.platforms.pigai import grading as g

        class Adapter:
            async def list_finished_essays(self):
                from kedaibiao.platforms.pigai.protocol import EssayRow
                return [EssayRow(rid="R", eid="E", title="T", score_raw="87.5")]

            async def fetch_essay_detail(self, rid, eid):
                raise RuntimeError("找不到任何已知锚点")

        snap = await g.fetch_snapshot(Adapter(), rid="R", eid="E")
        self.assertTrue(snap.graded)
        self.assertEqual(snap.score, 87.5)
        self.assertIn("列表", snap.detail_error)

    async def test_detail_without_score_uses_list_score(self) -> None:
        from kedaibiao.platforms.pigai import grading as g

        class Detail:
            score = None
            full_marks = None
            topic = ""
            essay_text = "正文"
            dimensions = []
            overall_comment = ""
            annotations = []

        class Adapter:
            async def list_finished_essays(self):
                from kedaibiao.platforms.pigai.protocol import EssayRow
                return [EssayRow(rid="R", eid="E", title="T", score_raw="75")]

            async def fetch_essay_detail(self, rid, eid):
                return Detail()

        snap = await g.fetch_snapshot(Adapter(), rid="R", eid="E")
        self.assertEqual(snap.score, 75.0)
        self.assertEqual(snap.essay_text, "正文")
        self.assertIn("正文", snap.detail_error)

    async def test_real_detail_score_wins(self) -> None:
        from kedaibiao.platforms.pigai import grading as g

        class Detail:
            score = 83.0
            full_marks = 100.0
            topic = "t"
            essay_text = "正文"
            dimensions = []
            overall_comment = "ok"
            annotations = []

        class Adapter:
            async def list_finished_essays(self):
                from kedaibiao.platforms.pigai.protocol import EssayRow
                return [EssayRow(rid="R", eid="E", title="T", score_raw="未截止")]

            async def fetch_essay_detail(self, rid, eid):
                return Detail()

        snap = await g.fetch_snapshot(Adapter(), rid="R", eid="E")
        self.assertEqual(snap.score, 83.0)
        self.assertEqual(snap.detail_error, "", "详情页有分就不该标 detail_error")
