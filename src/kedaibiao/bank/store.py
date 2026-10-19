"""题库与执行留痕存储（SQLite，标准库 ``sqlite3``，零额外依赖）。

表设计要点：
- ``questions.fingerprint`` 是主键，来自 ``models.question_fingerprint``；
  题干归一化算法一旦变更就必须写迁移，否则全库失配。
- ``correct`` 三态：1 答对 / 0 答错 / NULL 尚未由平台判分回填。
  只有被平台确认过的答案才允许高置信度复用。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from kedaibiao.core.models import (
    Answer,
    AnswerSource,
    Question,
    QuestionType,
    RunRecord,
    RunStatus,
    SubmissionResult,
    normalize_question,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS questions (
    fingerprint TEXT PRIMARY KEY,
    norm_stem   TEXT NOT NULL,
    stem        TEXT NOT NULL,
    qtype       TEXT NOT NULL,
    options     TEXT NOT NULL,
    choices     TEXT NOT NULL,
    text        TEXT NOT NULL DEFAULT '',
    source      TEXT NOT NULL,
    confidence  REAL NOT NULL DEFAULT 0,
    hits        INTEGER NOT NULL DEFAULT 0,
    correct     INTEGER,
    platform    TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_questions_norm ON questions(norm_stem);

CREATE TABLE IF NOT EXISTS runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    platform      TEXT NOT NULL,
    assignment_id TEXT NOT NULL,
    status        TEXT NOT NULL,
    attempts      INTEGER NOT NULL DEFAULT 0,
    error         TEXT NOT NULL DEFAULT '',
    detail        TEXT NOT NULL DEFAULT '{}',
    started_at    TEXT,
    finished_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_assignment ON runs(platform, assignment_id);

CREATE TABLE IF NOT EXISTS submissions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    platform      TEXT NOT NULL,
    assignment_id TEXT NOT NULL,
    ok            INTEGER NOT NULL,
    score         REAL,
    message       TEXT NOT NULL DEFAULT '',
    raw           TEXT NOT NULL DEFAULT '{}',
    created_at    TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class UnidentifiableQuestionError(ValueError):
    """题目既无题干也无平台 ID —— 无法唯一标识，入库会导致互相覆盖。"""


class _SqliteBase:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """把旧库补到当前 schema。

        用 ``CREATE TABLE IF NOT EXISTS`` 只能建新库，已有库缺列不会自动补 ——
        所以这里显式检查并按需 ``ALTER TABLE``。迁移必须幂等：每次开库都会跑。

        索引也在这里建：旧库在补列之前建 ``idx_questions_platform`` 会直接报
        「no such column」，所以它不能留在 ``SCHEMA`` 里。
        """
        cols = {row["name"] for row in self._conn.execute("PRAGMA table_info(questions)")}
        if cols and "platform" not in cols:
            self._conn.execute(
                "ALTER TABLE questions ADD COLUMN platform TEXT NOT NULL DEFAULT ''"
            )
        if cols:
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_questions_platform ON questions(platform)"
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def _cursor(self) -> Iterator[sqlite3.Cursor]:
        with self._lock:
            cur = self._conn.cursor()
            try:
                yield cur
                self._conn.commit()
            finally:
                cur.close()


class QuestionBank(_SqliteBase):
    """题目指纹 -> 答案 的索引。"""

    # -- 写入 -------------------------------------------------------------

    def upsert(
        self,
        question: Question,
        answer: Answer,
        *,
        correct: bool | None = None,
        platform: str = "",
    ) -> str:
        """写入或更新一条题目答案，返回 fingerprint。

        已存在时做合并而非覆盖：旧答案有平台判分背书（``correct`` 非空）
        而新答案没有时，保留旧答案。

        ``platform`` 用于按平台分维度统计（控制台的「统计」页）。旧库里已有
        的题目 ``platform`` 为空串，合并时若新值非空会补上。

        题目无法唯一标识时抛 ``UnidentifiableQuestionError`` —— 这类题目
        （无题干且无平台 ID）的指纹会退化成同一个哨兵值，入库即互相覆盖。
        """
        if not question.is_identifiable:
            raise UnidentifiableQuestionError(
                "题目既无题干也无平台 ID，无法唯一标识。请先补上 remote_id，"
                "或在调用前用 Question.is_identifiable 过滤。"
            )
        fp = question.fingerprint
        now = _now()
        with self._cursor() as cur:
            row = cur.execute(
                "SELECT choices, text, source, confidence, correct, platform "
                "FROM questions WHERE fingerprint=?",
                (fp,),
            ).fetchone()

            if row is None:
                cur.execute(
                    """INSERT INTO questions
                       (fingerprint, norm_stem, stem, qtype, options, choices, text,
                        source, confidence, hits, correct, platform, created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,0,?,?,?,?)""",
                    (
                        fp,
                        normalize_question(question.stem),
                        question.stem,
                        question.type.value,
                        json.dumps(question.options, ensure_ascii=False),
                        json.dumps(answer.choices),
                        answer.text,
                        answer.source.value,
                        answer.confidence,
                        None if correct is None else int(correct),
                        platform or "",
                        now,
                        now,
                    ),
                )
            else:
                keep_old = row["correct"] is not None and correct is None
                # 已有平台归属就别被空值冲掉：一道题可能是先从别的平台进来的
                keep_platform = row["platform"] or platform or ""
                if keep_old:
                    cur.execute(
                        "UPDATE questions SET platform=?, updated_at=? WHERE fingerprint=?",
                        (keep_platform, now, fp),
                    )
                else:
                    cur.execute(
                        """UPDATE questions SET choices=?, text=?, source=?, confidence=?,
                           correct=?, platform=?, updated_at=? WHERE fingerprint=?""",
                        (
                            json.dumps(answer.choices),
                            answer.text,
                            answer.source.value,
                            answer.confidence,
                            None if correct is None else int(correct),
                            keep_platform,
                            now,
                            fp,
                        ),
                    )
        return fp

    def record_outcome(self, fingerprint: str, *, correct: bool) -> None:
        """平台判分回填。这是题库质量唯一的真相来源。"""
        with self._cursor() as cur:
            cur.execute(
                "UPDATE questions SET correct=?, updated_at=? WHERE fingerprint=?",
                (int(correct), _now(), fingerprint),
            )

    # -- 读取 -------------------------------------------------------------

    def lookup(self, question: Question) -> Answer | None:
        """精确命中，命中即累加 hits。无法标识的题目直接返回 None。"""
        if not question.is_identifiable:
            return None
        with self._cursor() as cur:
            row = cur.execute(
                "SELECT * FROM questions WHERE fingerprint=?", (question.fingerprint,)
            ).fetchone()
            if row is None:
                return None
            cur.execute("UPDATE questions SET hits=hits+1 WHERE fingerprint=?", (row["fingerprint"],))
        return self._row_to_answer(question, row)

    def fuzzy_lookup(self, question: Question, *, threshold: float = 0.92, limit: int = 200) -> Answer | None:
        """模糊命中：题干近似即可（平台换字号/加空格/改标点很常见）。

        ``threshold`` 默认 0.92 偏严 —— 宁可漏答也不要错答。
        """
        target = normalize_question(question.stem)
        if not target:
            return None
        prefix = target[:8]
        with self._cursor() as cur:
            rows = cur.execute(
                "SELECT * FROM questions WHERE norm_stem LIKE ? LIMIT ?",
                (f"{prefix}%", limit),
            ).fetchall()
        best: sqlite3.Row | None = None
        best_ratio = 0.0
        for row in rows:
            ratio = SequenceMatcher(None, target, row["norm_stem"]).ratio()
            if ratio > best_ratio:
                best, best_ratio = row, ratio
        if best is None or best_ratio < threshold:
            return None
        answer = self._row_to_answer(question, best)
        answer.source = AnswerSource.BANK_FUZZY
        answer.confidence = round(best_ratio, 4)
        return answer

    def resolve(self, question: Question) -> Answer | None:
        """精确优先，其次模糊。"""
        return self.lookup(question) or self.fuzzy_lookup(question)

    def stats(self, platform: str | None = None) -> dict[str, Any]:
        """题库统计。``platform`` 非空时只统计该平台。

        额外返回 ``by_platform``（各平台题目数），供控制台的「统计」页做对比；
        即使只看单个平台也会带上，方便前端一处渲染。
        """
        where = " WHERE platform=?" if platform else ""
        args: tuple[Any, ...] = (platform,) if platform else ()
        with self._cursor() as cur:
            total = cur.execute(
                f"SELECT COUNT(*) FROM questions{where}", args
            ).fetchone()[0]
            graded = cur.execute(
                f"SELECT COUNT(*) FROM questions{where}{' AND' if where else ' WHERE'}"
                " correct IS NOT NULL",
                args,
            ).fetchone()[0]
            right = cur.execute(
                f"SELECT COUNT(*) FROM questions{where}{' AND' if where else ' WHERE'}"
                " correct=1",
                args,
            ).fetchone()[0]
            by_source = cur.execute(
                f"SELECT source, COUNT(*) c FROM questions{where}"
                " GROUP BY source ORDER BY c DESC",
                args,
            ).fetchall()
            by_platform = cur.execute(
                "SELECT platform, COUNT(*) c FROM questions GROUP BY platform"
                " ORDER BY c DESC"
            ).fetchall()
        return {
            "total": total,
            "graded": graded,
            "correct": right,
            "accuracy": round(right / graded, 4) if graded else None,
            "ungraded": total - graded,
            "by_source": {r["source"]: r["c"] for r in by_source},
            "by_platform": {(r["platform"] or "<未知>"): r["c"] for r in by_platform},
        }

    # -- 导入导出 ---------------------------------------------------------

    def platform_stats(self) -> list[dict[str, Any]]:
        """各平台的题目数与判分准确率，供「统计」页做对比。"""
        with self._cursor() as cur:
            rows = cur.execute(
                """SELECT platform,
                          COUNT(*) AS total,
                          SUM(CASE WHEN correct IS NOT NULL THEN 1 ELSE 0 END) AS graded,
                          SUM(CASE WHEN correct=1 THEN 1 ELSE 0 END) AS correct
                   FROM questions GROUP BY platform ORDER BY total DESC"""
            ).fetchall()
        return [
            {
                "platform": r["platform"] or "",
                "questions": r["total"],
                "graded": r["graded"] or 0,
                "correct": r["correct"] or 0,
                "accuracy": (
                    round((r["correct"] or 0) / r["graded"], 4) if r["graded"] else None
                ),
            }
            for r in rows
        ]

    def export_jsonl(self, path: str | Path) -> int:
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        n = 0
        with self._cursor() as cur:
            rows = cur.execute("SELECT * FROM questions").fetchall()
        with out.open("w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(dict(row), ensure_ascii=False) + "\n")
                n += 1
        return n

    def import_jsonl(self, path: str | Path) -> int:
        n = 0
        with Path(path).open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                data = json.loads(line)
                q = Question(
                    stem=data["stem"],
                    type=QuestionType(data.get("qtype", "unknown")),
                    options=json.loads(data.get("options") or "[]"),
                )
                a = Answer(
                    question=q,
                    choices=json.loads(data.get("choices") or "[]"),
                    text=data.get("text", ""),
                    source=AnswerSource(data.get("source", "unknown")),
                    confidence=float(data.get("confidence") or 0),
                )
                correct = data.get("correct")
                self.upsert(q, a, correct=None if correct is None else bool(correct))
                n += 1
        return n

    @staticmethod
    def _row_to_answer(question: Question, row: sqlite3.Row) -> Answer:
        return Answer(
            question=question,
            choices=json.loads(row["choices"] or "[]"),
            text=row["text"] or "",
            source=AnswerSource(row["source"]),
            confidence=float(row["confidence"] or 0),
        )


class ProgressStore(_SqliteBase):
    """执行留痕：跑过什么、成功没有、失败原因。"""

    def start_run(self, record: RunRecord) -> int:
        with self._cursor() as cur:
            cur.execute(
                """INSERT INTO runs (platform, assignment_id, status, attempts, error, detail, started_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    record.platform,
                    record.assignment_id,
                    RunStatus.RUNNING.value,
                    record.attempts,
                    "",
                    json.dumps(record.detail, ensure_ascii=False),
                    _now(),
                ),
            )
            return int(cur.lastrowid or 0)

    def finish_run(
        self,
        run_id: int,
        status: RunStatus,
        *,
        error: str = "",
        attempts: int = 0,
        detail: dict[str, Any] | None = None,
    ) -> None:
        with self._cursor() as cur:
            cur.execute(
                """UPDATE runs SET status=?, error=?, attempts=?, detail=?, finished_at=?
                   WHERE id=?""",
                (
                    status.value,
                    error[:2000],
                    attempts,
                    json.dumps(detail or {}, ensure_ascii=False),
                    _now(),
                    run_id,
                ),
            )

    def record_submission(self, platform: str, result: SubmissionResult) -> None:
        with self._cursor() as cur:
            cur.execute(
                """INSERT INTO submissions
                   (platform, assignment_id, ok, score, message, raw, created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    platform,
                    result.assignment_id,
                    int(result.ok),
                    result.score,
                    result.message[:2000],
                    json.dumps(result.raw, ensure_ascii=False),
                    _now(),
                ),
            )

    def already_done(self, platform: str, assignment_id: str) -> bool:
        """幂等保护：同一作业已成功提交过就不再重复跑。"""
        with self._cursor() as cur:
            row = cur.execute(
                """SELECT 1 FROM runs WHERE platform=? AND assignment_id=? AND status=?
                   LIMIT 1""",
                (platform, assignment_id, RunStatus.SUCCEEDED.value),
            ).fetchone()
        return row is not None

    def recent_runs(self, limit: int = 50, *, platform: str | None = None) -> list[dict[str, Any]]:
        where = " WHERE platform=?" if platform else ""
        args: tuple[Any, ...] = ((platform, limit) if platform else (limit,))
        with self._cursor() as cur:
            rows = cur.execute(
                f"SELECT * FROM runs{where} ORDER BY id DESC LIMIT ?", args
            ).fetchall()
        return [dict(r) for r in rows]

    def status_counts(self, *, platform: str | None = None) -> dict[str, int]:
        """按状态汇总执行记录。

        给控制台/CLI 用：``{"total": 40, "succeeded": 30, ...}``。
        未知状态不丢，照原样计数 —— 免得将来加了新状态这里悄悄漏掉。
        """
        where = " WHERE platform=?" if platform else ""
        args: tuple[Any, ...] = (platform,) if platform else ()
        with self._cursor() as cur:
            rows = cur.execute(
                f"SELECT status, COUNT(*) AS c FROM runs{where} GROUP BY status", args
            ).fetchall()
        counts = {row["status"]: row["c"] for row in rows}
        counts["total"] = sum(counts.values())
        return counts

    def platform_stats(self) -> list[dict[str, Any]]:
        """各平台的成败对比，供「统计」页用。"""
        with self._cursor() as cur:
            rows = cur.execute(
                """SELECT platform,
                          COUNT(*) AS total,
                          SUM(CASE WHEN status='succeeded' THEN 1 ELSE 0 END) AS succeeded,
                          SUM(CASE WHEN status='failed'    THEN 1 ELSE 0 END) AS failed,
                          SUM(CASE WHEN status='skipped'   THEN 1 ELSE 0 END) AS skipped
                   FROM runs GROUP BY platform ORDER BY total DESC"""
            ).fetchall()
            subs = cur.execute(
                """SELECT platform,
                          COUNT(*) AS total,
                          SUM(CASE WHEN ok=1 THEN 1 ELSE 0 END) AS ok
                   FROM submissions GROUP BY platform"""
            ).fetchall()
        sub_map = {r["platform"]: {"submissions": r["total"], "submit_ok": r["ok"]} for r in subs}
        return [
            {
                "platform": r["platform"],
                "total": r["total"],
                "succeeded": r["succeeded"] or 0,
                "failed": r["failed"] or 0,
                "skipped": r["skipped"] or 0,
                **sub_map.get(r["platform"], {"submissions": 0, "submit_ok": 0}),
            }
            for r in rows
        ]

    def recent_submissions(
        self, limit: int = 50, *, platform: str | None = None
    ) -> list[dict[str, Any]]:
        where = " WHERE platform=?" if platform else ""
        args: tuple[Any, ...] = ((platform, limit) if platform else (limit,))
        with self._cursor() as cur:
            rows = cur.execute(
                f"SELECT * FROM submissions{where} ORDER BY id DESC LIMIT ?", args
            ).fetchall()
        return [dict(r) for r in rows]


def record_to_dict(record: RunRecord) -> dict[str, Any]:
    """给日志/事件用的扁平化。"""
    data = asdict(record)
    data["status"] = record.status.value
    return data
