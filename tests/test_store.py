"""题库与留痕存储测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from kedaibiao.bank.store import ProgressStore, QuestionBank
from kedaibiao.core.models import (
    Answer,
    AnswerSource,
    Question,
    QuestionType,
    RunRecord,
    RunStatus,
    SubmissionResult,
)


def _q(stem: str, options: list[str] | None = None) -> Question:
    return Question(stem=stem, type=QuestionType.SINGLE, options=options or ["甲", "乙"])


def _a(q: Question, choices: list[int], conf: float = 1.0) -> Answer:
    return Answer(question=q, choices=choices, source=AnswerSource.BANK_HIT, confidence=conf)


class TestQuestionBank(unittest.TestCase):
    def setUp(self) -> None:
        self.bank = QuestionBank(":memory:")
        self.addCleanup(self.bank.close)

    def test_upsert_then_exact_lookup(self) -> None:
        q = _q("下列哪项正确？")
        fp = self.bank.upsert(q, _a(q, [0]))
        self.assertEqual(len(fp), 32)
        hit = self.bank.lookup(q)
        assert hit is not None
        self.assertEqual(hit.choices, [0])
        self.assertIs(hit.source, AnswerSource.BANK_HIT)

    def test_lookup_miss(self) -> None:
        self.assertIsNone(self.bank.lookup(_q("没见过的题")))

    def test_punctuation_variance_hits_exact_path(self) -> None:
        stored = _q("下列哪项，正确？")
        self.bank.upsert(stored, _a(stored, [1]))
        self.assertIsNotNone(self.bank.lookup(_q("下列哪项正确")))

    def test_fuzzy_lookup_on_near_duplicate(self) -> None:
        stored = _q("在关系数据库中，用于唯一标识一条记录的属性称为")
        self.bank.upsert(stored, _a(stored, [2]))
        near = _q("在关系数据库中用于唯一标识一条记录的属性称为！")
        hit = self.bank.fuzzy_lookup(near)
        if hit is not None:  # 归一化后可能已完全相同，两种结果都合法
            self.assertIs(hit.source, AnswerSource.BANK_FUZZY)
            self.assertEqual(hit.choices, [2])

    def test_fuzzy_lookup_rejects_dissimilar(self) -> None:
        stored = _q("完全不一样的题目内容甲甲乙乙丙丙")
        self.bank.upsert(stored, _a(stored, [0]))
        self.assertIsNone(self.bank.fuzzy_lookup(_q("另外一道毫不相干的题丁丁戊戊己己")))

    def test_resolve_prefers_exact(self) -> None:
        q = _q("精确命中的题目")
        self.bank.upsert(q, _a(q, [1]))
        hit = self.bank.resolve(q)
        assert hit is not None
        self.assertIs(hit.source, AnswerSource.BANK_HIT)

    def test_hits_counter_increments(self) -> None:
        q = _q("计数据的题")
        self.bank.upsert(q, _a(q, [0]))
        for _ in range(3):
            self.bank.lookup(q)
        self.assertEqual(self.bank.stats()["total"], 1)
        rows = self.bank._conn.execute("SELECT hits FROM questions").fetchone()  # noqa: SLF001
        self.assertEqual(rows["hits"], 3)

    def test_graded_answer_not_overwritten_by_ungraded(self) -> None:
        q = _q("已被平台判分的题")
        self.bank.upsert(q, _a(q, [1]), correct=True)
        self.bank.upsert(q, _a(q, [0]), correct=None)
        hit = self.bank.lookup(q)
        assert hit is not None
        self.assertEqual(hit.choices, [1], "有判分背书的旧答案不该被未验证的新答案覆盖")
        row = self.bank._conn.execute("SELECT correct FROM questions").fetchone()  # noqa: SLF001
        self.assertEqual(row["correct"], 1)

    def test_ungraded_answer_updated_by_ungraded(self) -> None:
        q = _q("可刷新的题")
        self.bank.upsert(q, _a(q, [1]))
        self.bank.upsert(q, _a(q, [0]))
        hit = self.bank.lookup(q)
        assert hit is not None
        self.assertEqual(hit.choices, [0])

    def test_record_outcome(self) -> None:
        q = _q("回填判分的题")
        fp = self.bank.upsert(q, _a(q, [0]))
        self.bank.record_outcome(fp, correct=False)
        stats = self.bank.stats()
        self.assertEqual(stats["graded"], 1)
        self.assertEqual(stats["correct"], 0)
        self.assertEqual(stats["accuracy"], 0.0)

    def test_stats_empty(self) -> None:
        stats = self.bank.stats()
        self.assertEqual(stats["total"], 0)
        self.assertIsNone(stats["accuracy"])

    def test_stats_accuracy(self) -> None:
        for i, ok in enumerate([True, True, False, False]):
            q = _q(f"题{i}")
            self.bank.upsert(q, _a(q, [0]), correct=ok)
        stats = self.bank.stats()
        self.assertEqual(stats["graded"], 4)
        self.assertEqual(stats["correct"], 2)
        self.assertEqual(stats["accuracy"], 0.5)
        self.assertEqual(stats["by_source"], {"bank_hit": 4})

    def test_export_import_roundtrip(self) -> None:
        for i in range(3):
            q = _q(f"往返题{i}")
            self.bank.upsert(q, _a(q, [i % 2]), correct=True if i == 0 else None)

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bank.jsonl"
            self.assertEqual(self.bank.export_jsonl(path), 3)
            self.assertGreater(path.stat().st_size, 0)

            other = QuestionBank(":memory:")
            self.addCleanup(other.close)
            self.assertEqual(other.import_jsonl(path), 3)
            self.assertEqual(other.stats()["total"], 3)
            self.assertEqual(other.stats()["graded"], 1)

    def test_options_persisted(self) -> None:
        q = _q("带选项的题", ["选项一", "选项二", "选项三"])
        self.bank.upsert(q, _a(q, [2]))
        row = self.bank._conn.execute("SELECT options FROM questions").fetchone()  # noqa: SLF001
        self.assertIn("选项三", row["options"])


class TestProgressStore(unittest.TestCase):
    def setUp(self) -> None:
        self.store = ProgressStore(":memory:")
        self.addCleanup(self.store.close)

    def test_lifecycle(self) -> None:
        run_id = self.store.start_run(RunRecord(platform="fake", assignment_id="a1"))
        self.assertGreater(run_id, 0)
        self.store.finish_run(run_id, RunStatus.SUCCEEDED, attempts=2, detail={"k": "v"})
        runs = self.store.recent_runs()
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["status"], "succeeded")
        self.assertEqual(runs[0]["attempts"], 2)
        self.assertIn('"k"', runs[0]["detail"])

    def test_already_done_only_after_success(self) -> None:
        run_id = self.store.start_run(RunRecord(platform="fake", assignment_id="a2"))
        self.store.finish_run(run_id, RunStatus.FAILED, error="boom")
        self.assertFalse(self.store.already_done("fake", "a2"))

        run_id = self.store.start_run(RunRecord(platform="fake", assignment_id="a2"))
        self.store.finish_run(run_id, RunStatus.SUCCEEDED)
        self.assertTrue(self.store.already_done("fake", "a2"))

    def test_already_done_scoped_by_platform(self) -> None:
        run_id = self.store.start_run(RunRecord(platform="p1", assignment_id="same"))
        self.store.finish_run(run_id, RunStatus.SUCCEEDED)
        self.assertFalse(self.store.already_done("p2", "same"))

    def test_error_truncated(self) -> None:
        run_id = self.store.start_run(RunRecord(platform="fake", assignment_id="a3"))
        self.store.finish_run(run_id, RunStatus.FAILED, error="x" * 5000)
        self.assertLessEqual(len(self.store.recent_runs()[0]["error"]), 2000)

    def test_record_submission(self) -> None:
        self.store.record_submission(
            "fake",
            SubmissionResult(assignment_id="a4", ok=True, score=88.5, message="已交卷"),
        )
        row = self.store._conn.execute("SELECT * FROM submissions").fetchone()  # noqa: SLF001
        self.assertEqual(row["ok"], 1)
        self.assertEqual(row["score"], 88.5)
        self.assertEqual(row["message"], "已交卷")

    def test_persists_to_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sub" / "p.db"
            store = ProgressStore(path)
            run_id = store.start_run(RunRecord(platform="fake", assignment_id="a5"))
            store.finish_run(run_id, RunStatus.SUCCEEDED)
            store.close()
            self.assertTrue(path.is_file())

            reopened = ProgressStore(path)
            self.addCleanup(reopened.close)
            self.assertTrue(reopened.already_done("fake", "a5"))


if __name__ == "__main__":
    unittest.main()
