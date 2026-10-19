"""控制台可改的运行时配置（目前只有大模型），存 sqlite，不写 ``.env``。

## 为什么要能在控制台里改

``.env`` 是部署配置：改了要重启服务，而且服务自己回写 ``.env`` 容易出现
「谁最后写的算」。但大模型这种配置是**使用期**的东西 —— 换网关、换模型、换 key
都该在界面上点一下就生效。所以放应用自己的库里。

## 优先级

```
控制台里保存的值  >  .env / 环境变量  >  内置默认值
```

键名用 ``<分组>.<字段>``（例如 ``llm.model``），值一律存字符串；清空某个键
等于「删掉覆盖项」，回落到 ``.env``。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

TABLE = "app_settings"


class OverrideStore:
    """``app_settings`` 表的极简读写（与主库同一个 sqlite 文件）。"""

    def __init__(self, db_path: str | Path) -> None:
        self.path = Path(db_path)

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.path)
        con.execute(
            f"CREATE TABLE IF NOT EXISTS {TABLE} ("
            "  key TEXT PRIMARY KEY,"
            "  value TEXT NOT NULL,"
            "  updated_at TEXT NOT NULL"
            ")"
        )
        return con

    def all(self, prefix: str = "") -> dict[str, str]:
        try:
            with self._connect() as con:
                if prefix:
                    rows = con.execute(
                        f"SELECT key, value FROM {TABLE} WHERE key LIKE ? ORDER BY key",
                        (prefix + "%",),
                    ).fetchall()
                else:
                    rows = con.execute(
                        f"SELECT key, value FROM {TABLE} ORDER BY key"
                    ).fetchall()
        except sqlite3.Error:
            return {}
        return {str(k): str(v) for k, v in rows}

    def put_many(self, values: dict[str, str]) -> int:
        now = datetime.now().isoformat(timespec="seconds")
        rows = [(str(k), str(v), now) for k, v in values.items()]
        if not rows:
            return 0
        with self._connect() as con:
            con.executemany(
                f"INSERT INTO {TABLE} (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                "updated_at=excluded.updated_at",
                rows,
            )
        return len(rows)

    def delete(self, keys: list[str]) -> int:
        if not keys:
            return 0
        with self._connect() as con:
            con.executemany(f"DELETE FROM {TABLE} WHERE key=?", [(k,) for k in keys])
        return len(keys)
