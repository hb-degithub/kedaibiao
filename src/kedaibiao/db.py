"""SQLite 存储底座。

认证、题库、执行留痕都用它，避免每个模块各写一遍「开库 + WAL + 锁 +
幂等迁移」这套样板。迁移机制是重点：``CREATE TABLE IF NOT EXISTS`` 只能建新库，
已有库缺列不会自动补，所以每个 store 都要能声明自己的 ``_migrate``。
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any


class SqliteStore:
    """带线程锁的连接包装。

    为什么要有锁：Web 服务是多线程的（每个请求一个线程），而 sqlite 连接
    默认不允许跨线程。这里用 ``check_same_thread=False`` + 显式锁串行化，
    比每次开连接省事，也比「每个线程一个连接」好管。
    """

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(self.schema())
        self.migrate()
        self._conn.commit()

    # -- 子类覆写 ---------------------------------------------------------

    def schema(self) -> str:
        """建表语句。只有建新库时会用到。"""
        return ""

    def migrate(self) -> None:
        """把旧库补到当前 schema。每次开库都会跑，必须幂等。"""

    # -- 基础设施 ---------------------------------------------------------

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def cursor(self) -> Iterator[sqlite3.Cursor]:
        with self._lock:
            cur = self._conn.cursor()
            try:
                yield cur
                self._conn.commit()
            finally:
                cur.close()

    # -- 迁移辅助 ---------------------------------------------------------

    def columns(self, table: str) -> set[str]:
        rows = self._conn.execute(f"PRAGMA table_info({table})").fetchall()
        return {row["name"] for row in rows}

    def table_exists(self, table: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        return row is not None

    def add_column_if_missing(self, table: str, column: str, ddl: str) -> bool:
        """``ddl`` 是列定义（不含列名），例如 ``TEXT NOT NULL DEFAULT ''``。"""
        if not self.table_exists(table) or column in self.columns(table):
            return False
        self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        return True

    def index(self, name: str, table: str, columns: Sequence[str]) -> None:
        cols = ", ".join(columns)
        self._conn.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {table}({cols})")

    # -- 便捷查询 ---------------------------------------------------------

    def fetchall(self, sql: str, args: Sequence[Any] = ()) -> list[dict[str, Any]]:
        with self.cursor() as cur:
            return [dict(r) for r in cur.execute(sql, tuple(args)).fetchall()]

    def fetchone(self, sql: str, args: Sequence[Any] = ()) -> dict[str, Any] | None:
        with self.cursor() as cur:
            row = cur.execute(sql, tuple(args)).fetchone()
        return dict(row) if row is not None else None

    def scalar(self, sql: str, args: Sequence[Any] = ()) -> Any:
        with self.cursor() as cur:
            row = cur.execute(sql, tuple(args)).fetchone()
        return row[0] if row is not None else None

    def execute(self, sql: str, args: Sequence[Any] = ()) -> int:
        with self.cursor() as cur:
            cur.execute(sql, tuple(args))
            return int(cur.lastrowid or 0)
