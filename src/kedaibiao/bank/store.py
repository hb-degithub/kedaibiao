"""题库与执行留痕存储（SQLite，标准库 ``sqlite3``，零额外依赖）。

## 数据隔离

**所有数据都属于某个用户**（``owner_id``）。这一层是隔离的**唯一**保证 ——
界面隐藏只是体验，越权必须在这里挡住。

- ``owner_id=None`` 表示**不限归属**，只有超级管理员的服务层才会这样调用；
- 题库主键是 ``(owner_id, fingerprint)``：A 缓存的答案不能被 B 命中，
  否则「换号」就变成数据串台。

## 迁移

``questions`` 早期版本主键只有 ``fingerprint``，加归属必须**重建表**
（SQLite 不能直接改主键）。``runs`` / ``submissions`` 只需要补列。
两条迁移都幂等，每次开库都会跑。
"""

from __future__ import annotations

import json
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
from kedaibiao.db import SqliteStore

#: 「不限归属」的显式写法。普通用户的所有查询都必须带上自己的 id。
ANY_OWNER: None = None

QUESTIONS_COLUMNS = """
    owner_id    INTEGER NOT NULL DEFAULT 0,
    fingerprint TEXT NOT NULL,
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
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (owner_id, fingerprint)
"""

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS questions ({QUESTIONS_COLUMNS});

CREATE TABLE IF NOT EXISTS runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id      INTEGER NOT NULL DEFAULT 0,
    platform      TEXT NOT NULL,
    assignment_id TEXT NOT NULL,
    status        TEXT NOT NULL,
    attempts      INTEGER NOT NULL DEFAULT 0,
    error         TEXT NOT NULL DEFAULT '',
    detail        TEXT NOT NULL DEFAULT '{{}}',
    started_at    TEXT,
    finished_at   TEXT
);

CREATE TABLE IF NOT EXISTS essay_attempts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id      INTEGER NOT NULL DEFAULT 0,
    platform      TEXT NOT NULL DEFAULT '',
    rid           TEXT NOT NULL,
    eid           TEXT NOT NULL DEFAULT '',
    attempt       INTEGER NOT NULL DEFAULT 1,
    score         REAL,
    full_marks    REAL,
    graded        INTEGER NOT NULL DEFAULT 0,
    raw_state     TEXT NOT NULL DEFAULT '',
    essay_text    TEXT NOT NULL DEFAULT '',
    feedback      TEXT NOT NULL DEFAULT '[]',
    dimensions    TEXT NOT NULL DEFAULT '[]',
    overall       TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS submissions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id      INTEGER NOT NULL DEFAULT 0,
    platform      TEXT NOT NULL,
    assignment_id TEXT NOT NULL,
    ok            INTEGER NOT NULL,
    score         REAL,
    message       TEXT NOT NULL DEFAULT '',
    raw           TEXT NOT NULL DEFAULT '{{}}',
    created_at    TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _filters(
    owner_id: int | None = ANY_OWNER, platform: str | None = None
) -> tuple[str, tuple[Any, ...]]:
    """统一的过滤片段。

    **总是以 ``WHERE 1=1`` 开头** —— 这样后续无论拼多少个 ``AND`` 都不会出
    「FROM questions AND ...」这种语法错误。归属为 ``None`` 表示不过滤
    （只有超级管理员的全量视图会这样调用）。
    """
    where = " WHERE 1=1"
    args: list[Any] = []
    if owner_id is not None:
        where += " AND owner_id=?"
        args.append(owner_id)
    if platform:
        where += " AND platform=?"
        args.append(platform)
    return where, tuple(args)


class UnidentifiableQuestionError(ValueError):
    """题目既无题干也无平台 ID —— 无法唯一标识，入库会导致互相覆盖。"""


class QuestionBank(SqliteStore):
    """题目指纹 -> 答案 的索引（按用户隔离）。"""

    def schema(self) -> str:
        return SCHEMA

    def migrate(self) -> None:
        # 1) questions：老版本主键只有 fingerprint，必须重建表才能带 owner_id
        cols = self.columns("questions")
        if cols and "owner_id" not in cols:
            self._rebuild_questions()
            cols = self.columns("questions")
        if cols:
            self.index("idx_questions_norm", "questions", ["owner_id", "norm_stem"])
            self.index("idx_questions_platform", "questions", ["owner_id", "platform"])

        # 2) runs / submissions：补列即可（主键没变）
        if self.table_exists("essay_attempts"):
            self.index("idx_attempts_rid", "essay_attempts", ["owner_id", "platform", "rid"])
        self.add_column_if_missing("runs", "owner_id", "INTEGER NOT NULL DEFAULT 0")
        self.add_column_if_missing("submissions", "owner_id", "INTEGER NOT NULL DEFAULT 0")
        if self.table_exists("runs"):
            self.index("idx_runs_owner", "runs", ["owner_id", "platform"])
        if self.table_exists("submissions"):
            self.index("idx_submissions_owner", "submissions", ["owner_id", "platform"])

    def _rebuild_questions(self) -> None:
        """把老 questions 表重建为带 ``owner_id`` 的复合主键表。

        老数据归属 0（历史遗留 / 迁移前的数据），启动时会由服务层认领给
        首个超级管理员，避免「升级后数据全不见了」。
        """
        old_cols = self.columns("questions")
        carry = [
            c
            for c in (
                "fingerprint", "norm_stem", "stem", "qtype", "options", "choices",
                "text", "source", "confidence", "hits", "correct", "created_at",
                "updated_at",
            )
            if c in old_cols
        ]
        if "platform" in old_cols:
            carry.append("platform")
        cols_sql = ", ".join(carry)

        self._conn.execute("ALTER TABLE questions RENAME TO questions_legacy")
        self._conn.execute(f"CREATE TABLE questions ({QUESTIONS_COLUMNS})")
        self._conn.execute(
            f"INSERT INTO questions (owner_id, {cols_sql}) "
            f"SELECT 0, {cols_sql} FROM questions_legacy"
        )
        self._conn.execute("DROP TABLE questions_legacy")

    # -- 写入 -------------------------------------------------------------

    def upsert(
        self,
        question: Question,
        answer: Answer,
        *,
        correct: bool | None = None,
        platform: str = "",
        owner_id: int = 0,
    ) -> str:
        """写入或更新一条题目答案，返回 fingerprint。

        已存在时做合并而非覆盖：旧答案有平台判分背书（``correct`` 非空）
        而新答案没有时，保留旧答案。

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
        with self.cursor() as cur:
            row = cur.execute(
                "SELECT choices, text, source, confidence, correct, platform "
                "FROM questions WHERE owner_id=? AND fingerprint=?",
                (owner_id, fp),
            ).fetchone()

            if row is None:
                cur.execute(
                    """INSERT INTO questions
                       (owner_id, fingerprint, norm_stem, stem, qtype, options, choices,
                        text, source, confidence, hits, correct, platform,
                        created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,0,?,?,?,?)""",
                    (
                        owner_id,
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
                keep_platform = row["platform"] or platform or ""
                if keep_old:
                    cur.execute(
                        "UPDATE questions SET platform=?, updated_at=? "
                        "WHERE owner_id=? AND fingerprint=?",
                        (keep_platform, now, owner_id, fp),
                    )
                else:
                    cur.execute(
                        """UPDATE questions SET choices=?, text=?, source=?, confidence=?,
                           correct=?, platform=?, updated_at=?
                           WHERE owner_id=? AND fingerprint=?""",
                        (
                            json.dumps(answer.choices),
                            answer.text,
                            answer.source.value,
                            answer.confidence,
                            None if correct is None else int(correct),
                            keep_platform,
                            now,
                            owner_id,
                            fp,
                        ),
                    )
        return fp

    def record_outcome(self, fingerprint: str, *, correct: bool, owner_id: int = 0) -> None:
        """平台判分回填。这是题库质量唯一的真相来源。"""
        self.execute(
            "UPDATE questions SET correct=?, updated_at=? WHERE owner_id=? AND fingerprint=?",
            (int(correct), _now(), owner_id, fingerprint),
        )

    def claim_ownerless(self, owner_id: int) -> int:
        """把归属 0 的历史数据认领给某个用户（升级后用）。

        只动 ``owner_id=0`` 的行，不会抢别人已经明确归属的数据。
        """
        with self.cursor() as cur:
            cur.execute("UPDATE questions SET owner_id=? WHERE owner_id=0", (owner_id,))
            questions = cur.rowcount
            cur.execute("UPDATE runs SET owner_id=? WHERE owner_id=0", (owner_id,))
            cur.execute("UPDATE submissions SET owner_id=? WHERE owner_id=0", (owner_id,))
        return int(questions)

    def ownerless_count(self) -> int:
        return int(
            self.scalar(
                "SELECT (SELECT COUNT(*) FROM questions WHERE owner_id=0)"
                " + (SELECT COUNT(*) FROM runs WHERE owner_id=0)"
                " + (SELECT COUNT(*) FROM submissions WHERE owner_id=0)"
            )
            or 0
        )

    # -- 读取 -------------------------------------------------------------

    def lookup(self, question: Question, *, owner_id: int = 0) -> Answer | None:
        """精确命中，命中即累加 hits。无法标识的题目直接返回 None。"""
        if not question.is_identifiable:
            return None
        fp = question.fingerprint
        with self.cursor() as cur:
            row = cur.execute(
                "SELECT * FROM questions WHERE owner_id=? AND fingerprint=?",
                (owner_id, fp),
            ).fetchone()
            if row is None:
                return None
            cur.execute(
                "UPDATE questions SET hits=hits+1 WHERE owner_id=? AND fingerprint=?",
                (owner_id, fp),
            )
        return self._row_to_answer(question, row)

    def fuzzy_lookup(
        self,
        question: Question,
        *,
        threshold: float = 0.92,
        limit: int = 200,
        owner_id: int = 0,
    ) -> Answer | None:
        """模糊命中：题干近似即可（平台换字号/加空格/改标点很常见）。

        ``threshold`` 默认 0.92 偏严 —— 宁可漏答也不要错答。
        """
        target = normalize_question(question.stem)
        if not target:
            return None
        prefix = target[:8]
        rows = self.fetchall(
            "SELECT * FROM questions WHERE owner_id=? AND norm_stem LIKE ? LIMIT ?",
            (owner_id, f"{prefix}%", limit),
        )
        best: dict[str, Any] | None = None
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

    def resolve(self, question: Question, *, owner_id: int = 0) -> Answer | None:
        """精确优先，其次模糊。"""
        return self.lookup(question, owner_id=owner_id) or self.fuzzy_lookup(
            question, owner_id=owner_id
        )

    def stats(
        self, platform: str | None = None, *, owner_id: int | None = ANY_OWNER
    ) -> dict[str, Any]:
        """题库统计。``platform`` 非空时只统计该平台；``owner_id=None`` 不过滤归属。

        额外返回 ``by_platform``（各平台题目数，**始终全量**），供「统计」页对比。
        """
        where, args = _filters(owner_id, platform)

        def scoped(extra: str = "") -> str:
            return f"SELECT COUNT(*) FROM questions{where}{extra}"

        total = int(self.scalar(scoped(), args) or 0)
        graded = int(self.scalar(scoped(" AND correct IS NOT NULL"), args) or 0)
        right = int(self.scalar(scoped(" AND correct=1"), args) or 0)

        by_source = self.fetchall(
            f"SELECT source, COUNT(*) c FROM questions{where} GROUP BY source ORDER BY c DESC",
            args,
        )
        own_where, own_args = _filters(owner_id)
        by_platform = self.fetchall(
            f"SELECT platform, COUNT(*) c FROM questions{own_where} "
            "GROUP BY platform ORDER BY c DESC",
            own_args,
        )
        return {
            "total": total,
            "graded": graded,
            "correct": right,
            "accuracy": round(right / graded, 4) if graded else None,
            "ungraded": total - graded,
            "by_source": {r["source"]: r["c"] for r in by_source},
            "by_platform": {(r["platform"] or "<未知>"): r["c"] for r in by_platform},
        }

    def platform_stats(self, *, owner_id: int | None = ANY_OWNER) -> list[dict[str, Any]]:
        """各平台的题目数与判分准确率，供「统计」页做对比。"""
        where, args = _filters(owner_id)
        rows = self.fetchall(
            f"""SELECT platform,
                       COUNT(*) AS total,
                       SUM(CASE WHEN correct IS NOT NULL THEN 1 ELSE 0 END) AS graded,
                       SUM(CASE WHEN correct=1 THEN 1 ELSE 0 END) AS correct
                FROM questions{where} GROUP BY platform ORDER BY total DESC""",
            args,
        )
        return [
            {
                "platform": r["platform"] or "",
                "questions": r["total"],
                "graded": r["graded"] or 0,
                "correct": r["correct"] or 0,
                "accuracy": (round((r["correct"] or 0) / r["graded"], 4) if r["graded"] else None),
            }
            for r in rows
        ]

    def recent_questions(
        self, limit: int = 50, *, platform: str | None = None,
        owner_id: int | None = ANY_OWNER,
    ) -> list[dict[str, Any]]:
        """最近的题目（「详细作答信息」用）。"""
        where, args = _filters(owner_id, platform)
        rows = self.fetchall(
            f"SELECT owner_id, fingerprint, stem, qtype, choices, text, source,"
            f" confidence, correct, platform, updated_at FROM questions{where}"
            f" ORDER BY updated_at DESC LIMIT ?",
            (*args, max(1, min(limit, 500))),
        )
        out = []
        for r in rows:
            item = dict(r)
            item["choices"] = json.loads(item["choices"] or "[]")
            out.append(item)
        return out

    # -- 导入导出 ---------------------------------------------------------

    def export_jsonl(self, path: str | Path, *, owner_id: int | None = ANY_OWNER) -> int:
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        where, args = _filters(owner_id)
        rows = self.fetchall(f"SELECT * FROM questions{where}", args)
        with out.open("w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        return len(rows)

    def import_jsonl(self, path: str | Path, *, owner_id: int = 0) -> int:
        count = 0
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
                self.upsert(
                    q, a, correct=None if correct is None else bool(correct),
                    platform=data.get("platform", ""), owner_id=owner_id,
                )
                count += 1
        return count

    @staticmethod
    def _row_to_answer(question: Question, row: Any) -> Answer:
        return Answer(
            question=question,
            choices=json.loads(row["choices"] or "[]"),
            text=row["text"] or "",
            source=AnswerSource(row["source"]),
            confidence=float(row["confidence"] or 0),
        )


class ProgressStore(SqliteStore):
    """执行留痕：跑过什么、成功没有、失败原因（按用户隔离）。"""

    def schema(self) -> str:
        return SCHEMA

    def migrate(self) -> None:
        cols = self.columns("questions")
        if cols and "owner_id" not in cols:
            self._rebuild_questions()
        if self.columns("questions"):
            self.index("idx_questions_norm", "questions", ["owner_id", "norm_stem"])
            self.index("idx_questions_platform", "questions", ["owner_id", "platform"])
        if self.table_exists("essay_attempts"):
            self.index("idx_attempts_rid", "essay_attempts", ["owner_id", "platform", "rid"])
        self.add_column_if_missing("runs", "owner_id", "INTEGER NOT NULL DEFAULT 0")
        self.add_column_if_missing("submissions", "owner_id", "INTEGER NOT NULL DEFAULT 0")
        if self.table_exists("runs"):
            self.index("idx_runs_owner", "runs", ["owner_id", "platform"])
        if self.table_exists("submissions"):
            self.index("idx_submissions_owner", "submissions", ["owner_id", "platform"])

    #: 与 QuestionBank 共用同一套重建逻辑
    _rebuild_questions = QuestionBank._rebuild_questions

    def start_run(self, record: RunRecord, *, owner_id: int = 0) -> int:
        return self.execute(
            """INSERT INTO runs
               (owner_id, platform, assignment_id, status, attempts, error, detail, started_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                owner_id,
                record.platform,
                record.assignment_id,
                RunStatus.RUNNING.value,
                record.attempts,
                "",
                json.dumps(record.detail, ensure_ascii=False),
                _now(),
            ),
        )

    def finish_run(
        self,
        run_id: int,
        status: RunStatus,
        *,
        error: str = "",
        attempts: int = 0,
        detail: dict[str, Any] | None = None,
    ) -> None:
        self.execute(
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

    # -- 作文尝试记录 -----------------------------------------------------

    def record_attempt(self, *, owner_id: int, platform: str, rid: str, eid: str,
                       attempt: int, snapshot: Any) -> int:
        """落一条「这次提交得了多少分、批注是什么」。"""
        data = snapshot.to_dict() if hasattr(snapshot, "to_dict") else dict(snapshot)
        return self.execute(
            """INSERT INTO essay_attempts
               (owner_id, platform, rid, eid, attempt, score, full_marks, graded,
                raw_state, essay_text, feedback, dimensions, overall, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                owner_id, platform, rid, eid, attempt,
                data.get("score"), data.get("full_marks"),
                int(bool(data.get("graded"))), str(data.get("raw_state") or ""),
                str(data.get("essay_text") or "")[:20000],
                json.dumps(data.get("feedback_lines") or [], ensure_ascii=False),
                json.dumps(data.get("dimensions") or [], ensure_ascii=False),
                str(data.get("overall_comment") or "")[:4000],
                _now(),
            ),
        )

    def attempts_for(self, rid: str, *, owner_id: int | None = ANY_OWNER,
                     platform: str = "pigai") -> list[dict[str, Any]]:
        where, args = _filters(owner_id)
        where += " AND platform=? AND rid=?"
        rows = self.fetchall(
            f"SELECT * FROM essay_attempts{where} ORDER BY attempt ASC, id ASC",
            (*args, platform, rid),
        )
        out = []
        for r in rows:
            item = dict(r)
            item["feedback"] = json.loads(item["feedback"] or "[]")
            item["dimensions"] = json.loads(item["dimensions"] or "[]")
            out.append(item)
        return out

    def record_submission(
        self, platform: str, result: SubmissionResult, *, owner_id: int = 0
    ) -> None:
        self.execute(
            """INSERT INTO submissions
               (owner_id, platform, assignment_id, ok, score, message, raw, created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                owner_id,
                platform,
                result.assignment_id,
                int(result.ok),
                result.score,
                result.message[:2000],
                json.dumps(result.raw, ensure_ascii=False),
                _now(),
            ),
        )

    def already_done(self, platform: str, assignment_id: str, *, owner_id: int = 0) -> bool:
        """幂等保护：同一作业已成功提交过就不再重复跑（只看这个用户自己的记录）。"""
        row = self.fetchone(
            """SELECT 1 FROM runs WHERE owner_id=? AND platform=? AND assignment_id=?
               AND status=? LIMIT 1""",
            (owner_id, platform, assignment_id, RunStatus.SUCCEEDED.value),
        )
        return row is not None

    def recent_runs(
        self, limit: int = 50, *, platform: str | None = None,
        owner_id: int | None = ANY_OWNER,
    ) -> list[dict[str, Any]]:
        where, args = _filters(owner_id, platform)
        return self.fetchall(
            f"SELECT * FROM runs{where} ORDER BY id DESC LIMIT ?",
            (*args, max(1, min(limit, 500))),
        )

    def status_counts(
        self, *, platform: str | None = None, owner_id: int | None = ANY_OWNER
    ) -> dict[str, int]:
        where, args = _filters(owner_id, platform)
        rows = self.fetchall(
            f"SELECT status, COUNT(*) AS c FROM runs{where} GROUP BY status", args
        )
        counts = {row["status"]: row["c"] for row in rows}
        counts["total"] = sum(counts.values())
        return counts

    def platform_stats(self, *, owner_id: int | None = ANY_OWNER) -> list[dict[str, Any]]:
        """各平台的成败对比，供「统计」页用。"""
        where, args = _filters(owner_id)
        runs = self.fetchall(
            f"""SELECT platform,
                       COUNT(*) AS total,
                       SUM(CASE WHEN status='succeeded' THEN 1 ELSE 0 END) AS succeeded,
                       SUM(CASE WHEN status='failed'    THEN 1 ELSE 0 END) AS failed,
                       SUM(CASE WHEN status='skipped'   THEN 1 ELSE 0 END) AS skipped
                FROM runs{where} GROUP BY platform ORDER BY total DESC""",
            args,
        )
        sub_where, sub_args = _filters(owner_id)
        subs = self.fetchall(
            f"""SELECT platform, COUNT(*) AS total,
                       SUM(CASE WHEN ok=1 THEN 1 ELSE 0 END) AS ok
                FROM submissions{sub_where} GROUP BY platform""",
            sub_args,
        )
        sub_map = {
            r["platform"]: {"submissions": r["total"], "submit_ok": r["ok"]} for r in subs
        }
        return [
            {
                "platform": r["platform"],
                "total": r["total"],
                "succeeded": r["succeeded"] or 0,
                "failed": r["failed"] or 0,
                "skipped": r["skipped"] or 0,
                **sub_map.get(r["platform"], {"submissions": 0, "submit_ok": 0}),
            }
            for r in runs
        ]

    def recent_submissions(
        self, limit: int = 50, *, platform: str | None = None,
        owner_id: int | None = ANY_OWNER,
    ) -> list[dict[str, Any]]:
        where, args = _filters(owner_id, platform)
        return self.fetchall(
            f"SELECT * FROM submissions{where} ORDER BY id DESC LIMIT ?",
            (*args, max(1, min(limit, 500))),
        )


def record_to_dict(record: RunRecord) -> dict[str, Any]:
    """给日志/事件用的扁平化。"""
    data = asdict(record)
    data["status"] = record.status.value
    return data
