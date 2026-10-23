"""批改网评分循环：**先取分、看分，再决定要不要重交**（离线）。

用户要求：必须先拿到并展示分数，才判断要不要重新提交。
这些用例锁的就是顺序与「不必要就绝不提交」：

- 已有稿件且已达目标 → 一次都不提交；
- 已有稿件低于目标 → 改写后再提交，拿到新分再判；
- 已有稿件但平台还没给分（未截止/待分析）→ 不提交，交还给用户决定；
- 完全没有旧稿 → 才提交初始稿。
"""

from __future__ import annotations

import unittest
from unittest import mock

from kedaibiao.platforms.pigai.grade_loop import GradeLoop
from kedaibiao.platforms.pigai.protocol import EssayRow


class Snap:
    def __init__(self, score=None, *, state="graded", essay_text="原稿正文"):
        self.score = score
        self.raw_state = state
        self.essay_text = essay_text

    @property
    def graded(self) -> bool:
        return self.score is not None

    def to_dict(self):
        return {
            "score": self.score, "full_marks": 100.0, "graded": self.graded,
            "score_pct": (self.score or 0) / 100, "raw_state": self.raw_state,
            "dimensions": [{"name": "词汇", "ratio": 0.8}],
            "weak_dimensions": ["词汇"], "overall_comment": "ok",
            "feedback_lines": ["[w] 连词"], "essay_text": self.essay_text,
        }


class Submit:
    ok = True
    message = "ok"
    raw = {"eid": "E-NEW"}


class Adapter:
    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.submits = 0

    async def submit(self, assignment, answers):
        self.submits += 1
        return Submit()

    async def list_finished_essays(self):
        return self.rows

    async def list_pending_assignments(self):
        from kedaibiao.core.models import Assignment
        return [Assignment(remote_id="R", title="T")]


def old_row(score_raw="80", eid="E-OLD"):
    return EssayRow(rid="R", eid=eid, title="T", score_raw=score_raw)


class Base(unittest.IsolatedAsyncioTestCase):
    def make_loop(self, adapter, *, target=90, max_attempts=3, rewrite=True):
        rewrites: list[int] = []

        async def _rewrite(snap, essay, n):
            rewrites.append(n)
            return f"v{n}"

        loop = GradeLoop(
            adapter=adapter, http=object(), base="", target_score=target,
            max_attempts=max_attempts, rewrite=_rewrite if rewrite else None,
        )
        return loop, rewrites

    def patch_grading(self, *, fetch=None, wait=None):
        import kedaibiao.platforms.pigai.grading as grading

        if fetch is not None:
            self._p1 = mock.patch.object(grading, "fetch_snapshot", fetch)
            self._p1.start()
            self.addCleanup(self._p1.stop)
        if wait is not None:
            self._p2 = mock.patch.object(grading, "wait_for_grade", wait)
            self._p2.start()
            self.addCleanup(self._p2.stop)


class TestScoreFirst(Base):
    async def test_existing_above_target_never_submits(self) -> None:
        adapter = Adapter([old_row("95")])

        async def fetch(*a, **k):
            return Snap(95.0)

        self.patch_grading(fetch=fetch)
        loop, rewrites = self.make_loop(adapter)
        result = await loop.run(rid="R", topic="T", initial_essay="v1")

        self.assertEqual(result.stop_reason, "reached")
        self.assertEqual(adapter.submits, 0, "已达目标就不该再交一次")
        self.assertEqual(rewrites, [])
        self.assertIn("不需要重新提交", result.message)
        self.assertEqual(result.attempts[0].attempt, 0, "旧稿记为第 0 轮")

    async def test_existing_below_target_rewrites_then_submits(self) -> None:
        adapter = Adapter([old_row("70")])
        calls = {"fetch": 0, "wait": 0}

        async def fetch(*a, **k):
            calls["fetch"] += 1
            return Snap(70.0)

        async def wait(*a, **k):
            calls["wait"] += 1
            return Snap(93.0)

        self.patch_grading(fetch=fetch, wait=wait)
        loop, rewrites = self.make_loop(adapter)
        result = await loop.run(rid="R", topic="T", initial_essay="v1")

        self.assertEqual(calls["fetch"], 1, "先取分")
        self.assertEqual(rewrites, [1], "看到 70 分后才改写")
        self.assertEqual(adapter.submits, 1)
        self.assertEqual(result.stop_reason, "reached")
        self.assertEqual(result.best_score, 93.0)

    async def test_existing_not_graded_does_not_submit(self) -> None:
        adapter = Adapter([old_row("未截止")])

        async def fetch(*a, **k):
            return Snap(None, state="未截止")

        async def wait(*a, **k):
            return Snap(None, state="未截止")

        self.patch_grading(fetch=fetch, wait=wait)
        loop, _ = self.make_loop(adapter)
        result = await loop.run(rid="R", topic="T", initial_essay="v1")

        self.assertEqual(result.stop_reason, "unavailable")
        self.assertEqual(adapter.submits, 0, "默认没分就不重交")
        self.assertIn("直接改写重交", result.message, "要把「可以强制重交」的出口告诉用户")

    async def test_existing_not_graded_but_forced_resubmit_submits(self) -> None:
        """用户明确点「直接改写重交」时才允许没分也交。"""
        adapter = Adapter([old_row("待分析")])

        async def fetch(*a, **k):
            return Snap(None, state="待分析")

        async def wait(*a, **k):
            return Snap(None, state="待分析")

        self.patch_grading(fetch=fetch, wait=wait)
        rewrites: list[int] = []

        async def _rewrite(snap, essay, n):
            rewrites.append(n)
            return f"v{n}"

        loop = GradeLoop(adapter=adapter, http=object(), base="", target_score=90,
                         max_attempts=1, rewrite=_rewrite,
                         resubmit_without_score=True)
        result = await loop.run(rid="R", topic="T", initial_essay="v1")

        self.assertEqual(adapter.submits, 1, "开了开关就该交")
        self.assertEqual(rewrites, [1], "先改写再交")
        self.assertEqual(result.stop_reason, "unavailable")

    async def test_no_existing_submits_initial_essay(self) -> None:
        adapter = Adapter([])

        async def wait(*a, **k):
            return Snap(88.0)

        self.patch_grading(wait=wait)
        loop, _ = self.make_loop(adapter, max_attempts=1)
        result = await loop.run(rid="R", topic="T", initial_essay="v1")

        self.assertEqual(adapter.submits, 1)
        self.assertEqual(result.stop_reason, "exhausted")
        self.assertEqual(result.best_score, 88.0)

    async def test_existing_pending_but_wait_times_out(self) -> None:
        import kedaibiao.platforms.pigai.grading as grading

        adapter = Adapter([old_row("待分析")])

        async def fetch(*a, **k):
            return Snap(None, state="待分析")

        async def wait(*a, **k):
            raise grading.AnalysisTimeout("等待分析超时（180s）")

        self.patch_grading(fetch=fetch, wait=wait)
        loop, _ = self.make_loop(adapter)
        result = await loop.run(rid="R", topic="T", initial_essay="v1")

        self.assertEqual(result.stop_reason, "timeout")
        self.assertEqual(adapter.submits, 0, "没拿到分就不重交")
        self.assertIn("不需要重新提交", result.message)

    async def test_probe_failure_falls_back_to_submit(self) -> None:
        adapter = Adapter([old_row("80")])

        async def fetch(*a, **k):
            raise RuntimeError("详情页打不开")

        async def wait(*a, **k):
            return Snap(91.0)

        self.patch_grading(fetch=fetch, wait=wait)
        loop, _ = self.make_loop(adapter)
        await loop.run(rid="R", topic="T", initial_essay="v1")
        self.assertEqual(adapter.submits, 1, "读不到旧分就只能按新稿走")

    async def test_existing_below_target_without_rewriter_stops(self) -> None:
        adapter = Adapter([old_row("70")])

        async def fetch(*a, **k):
            return Snap(70.0)

        self.patch_grading(fetch=fetch)
        loop, _ = self.make_loop(adapter, rewrite=False)
        result = await loop.run(rid="R", topic="T", initial_essay="v1")

        self.assertEqual(result.stop_reason, "exhausted")
        self.assertEqual(adapter.submits, 0, "没有改写器就不该盲目重交")
        self.assertIn("没有配置改写器", result.message)


if __name__ == "__main__":
    unittest.main()


class TestRewritePrompt(unittest.IsolatedAsyncioTestCase):
    """改写提示词必须带上「分数 + 评价」，否则改写了也没依据。"""

    class _Snap:
        def __init__(self, **extra):
            self.extra = extra

        def to_dict(self):
            data = {
                "score": 83.0, "full_marks": 100.0, "score_pct": 0.83,
                "dimensions": [{"name": "词汇", "ratio": 0.9}],
                "weak_dimensions": ["句子"],
                "overall_comment": "可适当增加从句的使用",
                "feedback_lines": ["[confusion] 易混词汇", "[i_suggest.word] 换词"],
            }
            data.update(self.extra)
            return data

    def _prompt(self, snapshot):
        from kedaibiao.platforms.pigai.grade_loop import build_rewrite_prompt
        return build_rewrite_prompt(
            topic="T", word_range="120-180", essay="原文正文", snapshot=snapshot, attempt=1
        )

    async def test_prompt_has_score_evaluation_and_feedback(self) -> None:
        prompt = self._prompt(self._Snap())
        self.assertIn("83.0", prompt)
        self.assertIn("总评：可适当增加从句", prompt)
        self.assertIn("易混词汇", prompt)
        self.assertIn("最需要提升的维度：句子", prompt)
        self.assertIn("原文正文", prompt)
        self.assertIn("重新", prompt.replace("不要整篇重写", "重新"))

    async def test_prompt_mentions_limits_when_only_score(self) -> None:
        prompt = self._prompt(
            self._Snap(overall_comment="", feedback_lines=[], dimensions=[],
                       weak_dimensions=[])
        )
        self.assertIn("没有总评和批注", prompt)
        self.assertIn("常见提分点", prompt)


class TestTimeoutRescue(Base):
    """等分超时后先免费补读一次：刚好出分就按正常结果走，不要报 timeout。"""

    async def test_timeout_but_score_just_appeared(self) -> None:
        adapter = Adapter([old_row("70")])
        calls = {"fetch": 0}

        async def fetch(*a, **k):
            calls["fetch"] += 1
            # 第一次：旧稿分数；第二次（超时后补读）：新稿 93 分
            return Snap(70.0) if calls["fetch"] == 1 else Snap(93.0)

        async def wait(*a, **k):
            import kedaibiao.platforms.pigai.grading as grading
            raise grading.AnalysisTimeout("等待分析超时（180s）")

        self.patch_grading(fetch=fetch, wait=wait)
        loop, _ = self.make_loop(adapter, max_attempts=1)
        events: list[dict] = []
        loop.on_event = events.append
        result = await loop.run(rid="R", topic="T", initial_essay="v1")

        self.assertEqual(result.stop_reason, "reached", "抢救到分数就不是 timeout 了")
        self.assertEqual(result.best_score, 93.0)
        self.assertEqual(adapter.submits, 1)
        kinds = [e["kind"] for e in events]
        self.assertIn("attempt.rescued", kinds)
        self.assertNotIn("attempt.timeout", kinds)

    async def test_timeout_without_score_still_reports_timeout(self) -> None:
        adapter = Adapter([])

        async def fetch(*a, **k):
            return Snap(None, state="分析中")

        async def wait(*a, **k):
            import kedaibiao.platforms.pigai.grading as grading
            raise grading.AnalysisTimeout("等待分析超时（180s）")

        self.patch_grading(fetch=fetch, wait=wait)
        loop, _ = self.make_loop(adapter, max_attempts=1)
        result = await loop.run(rid="R", topic="T", initial_essay="v1")

        self.assertEqual(result.stop_reason, "timeout")
        self.assertIn("不需要重新提交", result.message)
        self.assertIn("已经交上去了", result.message)


class TestEventsCarryWordCounts(Base):
    async def test_rewrite_events_carry_before_and_after_counts(self) -> None:
        adapter = Adapter([old_row("70")])

        async def fetch(*a, **k):
            # 已有稿件的正文来自快照 —— 改写前的词数就是它的词数
            return Snap(70.0, essay_text="one two three four")

        async def wait(*a, **k):
            return Snap(95.0)

        self.patch_grading(fetch=fetch, wait=wait)
        loop, _ = self.make_loop(adapter, max_attempts=1)
        events: list[dict] = []
        loop.on_event = events.append
        await loop.run(rid="R", topic="T", initial_essay="v0")

        rewriting = next(e for e in events if e["kind"] == "attempt.rewriting")
        rewritten = next(e for e in events if e["kind"] == "attempt.rewritten")
        self.assertEqual(rewriting["word_count"], 4, "改写前词数 = 现有稿件词数")
        self.assertEqual(rewritten["word_count"], 1, "改写后词数（fake 改写器返回 v1）")


class TestReusedEidNotice(Base):
    async def test_submit_without_new_eid_is_flagged(self) -> None:
        """平台没回新稿号时要说清楚：多半是覆盖提交，不是新稿次。"""
        adapter = Adapter([old_row("70")])
        adapter.submit = lambda assignment, answers: _submit_with_eid("E-OLD")

        async def fetch(*a, **k):
            return Snap(70.0)

        async def wait(*a, **k):
            return Snap(92.0)

        self.patch_grading(fetch=fetch, wait=wait)
        loop, _ = self.make_loop(adapter, max_attempts=1)
        events: list[dict] = []
        loop.on_event = events.append
        await loop.run(rid="R", topic="T", initial_essay="v1")

        submitted = next(e for e in events if e["kind"] == "attempt.submitted")
        self.assertTrue(submitted.get("reused"), "沿用旧稿号要标出来")


async def _submit_with_eid(eid: str):
    class _R:
        ok = True
        message = "ok"
        raw = {"eid": eid}

    return _R()
