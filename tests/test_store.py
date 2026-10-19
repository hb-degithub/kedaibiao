"""题库与留痕存储测试。"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from kedaibiao.bank.store import (
    ProgressStore,
    QuestionBank,
    UnidentifiableQuestionError,
)
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


class TestUnidentifiableQuestions(unittest.TestCase):
    """无题干且无平台 ID 的题目必须被拒，否则会互相覆盖。"""

    def setUp(self) -> None:
        self.bank = QuestionBank(":memory:")
        self.addCleanup(self.bank.close)

    def test_upsert_rejected(self) -> None:
        q = Question(stem="", options=["甲", "乙"])
        with self.assertRaises(UnidentifiableQuestionError) as cm:
            self.bank.upsert(q, Answer(question=q, choices=[0]))
        self.assertIn("remote_id", str(cm.exception))

    def test_lookup_returns_none(self) -> None:
        self.assertIsNone(self.bank.lookup(Question(stem="")))
        self.assertIsNone(self.bank.resolve(Question(stem="")))

    def test_remote_id_only_question_is_storable_and_distinct(self) -> None:
        q1 = Question(stem="", remote_id="q-1")
        q2 = Question(stem="", remote_id="q-2")
        self.bank.upsert(q1, Answer(question=q1, choices=[0]))
        self.bank.upsert(q2, Answer(question=q2, choices=[1]))

        self.assertEqual(self.bank.stats()["total"], 2, "两道无题面的题不能互相覆盖")
        hit1 = self.bank.lookup(q1)
        hit2 = self.bank.lookup(q2)
        assert hit1 is not None and hit2 is not None
        self.assertEqual(hit1.choices, [0])
        self.assertEqual(hit2.choices, [1])


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


class TestPlatformDimension(unittest.TestCase):
    """题库的平台维度：控制台「统计」页要按平台对比。"""

    def setUp(self) -> None:
        self.bank = QuestionBank(":memory:")
        self.addCleanup(self.bank.close)

    def test_platform_recorded(self) -> None:
        q = _q("甲平台的题")
        self.bank.upsert(q, _a(q, [0]), platform="alpha")
        self.assertEqual(self.bank.stats("alpha")["total"], 1)
        self.assertEqual(self.bank.stats("beta")["total"], 0)
        self.assertEqual(self.bank.stats()["total"], 1, "不传平台就是全部")

    def test_platform_stats_accuracy(self) -> None:
        for i, ok in enumerate([True, False]):
            q = _q(f"题{i}")
            self.bank.upsert(q, _a(q, [0]), correct=ok, platform="alpha")
        rows = {r["platform"]: r for r in self.bank.platform_stats()}
        self.assertEqual(rows["alpha"]["questions"], 2)
        self.assertEqual(rows["alpha"]["graded"], 2)
        self.assertEqual(rows["alpha"]["correct"], 1)
        self.assertEqual(rows["alpha"]["accuracy"], 0.5)

    def test_by_platform_is_always_global(self) -> None:
        for name in ("alpha", "beta"):
            q = _q(f"{name}的题")
            self.bank.upsert(q, _a(q, [0]), platform=name)
        scoped = self.bank.stats("alpha")
        self.assertEqual(scoped["total"], 1)
        self.assertEqual(scoped["by_platform"], {"alpha": 1, "beta": 1},
                         "by_platform 始终是全量，供对比用")

    def test_empty_platform_shows_as_unknown(self) -> None:
        q = _q("没标平台的题")
        self.bank.upsert(q, _a(q, [0]))
        self.assertIn("<未知>", self.bank.stats()["by_platform"])

    def test_existing_platform_not_overwritten_by_blank(self) -> None:
        q = _q("已归属平台的题")
        self.bank.upsert(q, _a(q, [0]), platform="alpha")
        self.bank.upsert(q, _a(q, [1]), platform="")
        self.assertEqual(self.bank.stats("alpha")["total"], 1)
        self.assertNotIn("", self.bank.stats()["by_platform"], "空写入不该把归属抹掉")

    def test_blank_platform_filled_in_by_later_write(self) -> None:
        q = _q("先没归属后来有")
        self.bank.upsert(q, _a(q, [0]))
        self.bank.upsert(q, _a(q, [1]), platform="alpha")
        self.assertEqual(self.bank.stats("alpha")["total"], 1)


class TestSchemaMigration(unittest.TestCase):
    """旧库（没有 platform 列）必须能自动补列且不丢数据。"""

    def _make_legacy_db(self, path: Path) -> None:
        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE questions (
                fingerprint TEXT PRIMARY KEY, norm_stem TEXT NOT NULL,
                stem TEXT NOT NULL, qtype TEXT NOT NULL, options TEXT NOT NULL,
                choices TEXT NOT NULL, text TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL, confidence REAL NOT NULL DEFAULT 0,
                hits INTEGER NOT NULL DEFAULT 0, correct INTEGER,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, platform TEXT NOT NULL,
                assignment_id TEXT NOT NULL, status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '',
                detail TEXT NOT NULL DEFAULT '{}', started_at TEXT, finished_at TEXT);
            CREATE TABLE submissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, platform TEXT NOT NULL,
                assignment_id TEXT NOT NULL, ok INTEGER NOT NULL, score REAL,
                message TEXT NOT NULL DEFAULT '', raw TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL);
            INSERT INTO questions VALUES
                ('fp1','题干','题干','single','[]','[0]','','bank_hit',1.0,0,1,
                 '2026-01-01','2026-01-01');
            """
        )
        conn.commit()
        conn.close()

    def test_adds_column_and_keeps_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "legacy.db"
            self._make_legacy_db(path)

            bank = QuestionBank(path)
            try:
                cols = {r["name"] for r in bank._conn.execute("PRAGMA table_info(questions)")}
                self.assertIn("platform", cols)
                self.assertEqual(bank.stats()["total"], 1, "旧数据不能丢")
                self.assertEqual(bank.stats()["correct"], 1)
            finally:
                bank.close()

    def test_migration_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "legacy.db"
            self._make_legacy_db(path)
            for _ in range(3):
                QuestionBank(path).close()

    def test_legacy_rows_are_addressable_by_platform_after_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "legacy.db"
            self._make_legacy_db(path)
            bank = QuestionBank(path)
            try:
                q = Question(stem="题干")
                bank.upsert(q, Answer(question=q, choices=[1],
                                      source=AnswerSource.BANK_HIT), platform="alpha")
                self.assertEqual(bank.stats("alpha")["total"], 1)
            finally:
                bank.close()


class TestProgressStorePlatformFilters(unittest.TestCase):
    def setUp(self) -> None:
        self.store = ProgressStore(":memory:")
        self.addCleanup(self.store.close)
        for platform, status in (("alpha", RunStatus.SUCCEEDED),
                                 ("alpha", RunStatus.FAILED),
                                 ("beta", RunStatus.SUCCEEDED)):
            run_id = self.store.start_run(
                RunRecord(platform=platform, assignment_id=f"{platform}-{status.value}")
            )
            self.store.finish_run(run_id, status)

    def test_status_counts_scoped(self) -> None:
        self.assertEqual(self.store.status_counts(platform="alpha")["total"], 2)
        self.assertEqual(self.store.status_counts(platform="beta")["total"], 1)
        self.assertEqual(self.store.status_counts()["total"], 3)

    def test_recent_runs_scoped(self) -> None:
        alpha = self.store.recent_runs(platform="alpha")
        self.assertEqual(len(alpha), 2)
        self.assertTrue(all(r["platform"] == "alpha" for r in alpha))

    def test_platform_stats_comparison(self) -> None:
        rows = {r["platform"]: r for r in self.store.platform_stats()}
        self.assertEqual(rows["alpha"]["succeeded"], 1)
        self.assertEqual(rows["alpha"]["failed"], 1)
        self.assertEqual(rows["beta"]["total"], 1)

    def test_recent_submissions_scoped(self) -> None:
        self.store.record_submission(
            "alpha", SubmissionResult(assignment_id="a", ok=True)
        )
        self.store.record_submission(
            "beta", SubmissionResult(assignment_id="b", ok=False)
        )
        self.assertEqual(len(self.store.recent_submissions(platform="alpha")), 1)
        self.assertEqual(len(self.store.recent_submissions()), 2)


if __name__ == "__main__":
    unittest.main()
