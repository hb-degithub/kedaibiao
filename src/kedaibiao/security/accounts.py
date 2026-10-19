"""平台账号：一个用户可以给同一个平台绑多个号，随时切换。

## 安全模型：**不落盘平台密码**

账号表只存「平台 + 标签 + 用户名 + 这个号自己的 cookie 文件路径 + 最近登录结果」。

密码只在**登录那一刻**用一次，用来换回会话 cookie；cookie 存进该账号专属的
文件。这样做的好处：

- 数据库里没有可解密的密码 —— 拖库也拿不到平台凭据；
- 与现状对比：``.env`` 里现在是**明文**存平台密码，这个模型比它更安全；
- 代价：平台会话过期后需要重新输一次密码。控制台会把「上次登录结果」显示出来，
  失效时明确提示，而不是默默失败。

（若将来确实要「自动重登」，需要引入主密钥 + 可逆加密，那是另一个明确的决定，
不在当前实现里。）
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from kedaibiao.db import SqliteStore

log = logging.getLogger(__name__)


class AccountError(Exception):
    def __init__(self, message: str, *, kind: str = "bad_request", status: int = 400) -> None:
        super().__init__(message)
        self.kind = kind
        self.status = status


def _iso_now() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass(slots=True)
class PlatformAccount:
    id: int
    owner_id: int
    platform: str
    label: str = ""
    username: str = ""
    is_active: bool = False
    created_at: str = ""
    last_login_at: str | None = None
    last_login_ok: bool | None = None
    last_login_note: str = ""

    @property
    def display(self) -> str:
        return self.label or self.username or f"#{self.id}"

    def cookie_path(self, session_dir: str | Path) -> Path:
        """这个号专属的 cookie 文件。

        用 ``accounts/`` 子目录把账号会话与旧的全局 ``http_cookies.json``
        分开，避免迁移期间互相踩。
        """
        return (
            Path(session_dir)
            / "accounts"
            / f"{self.owner_id}-{self.platform}-{self.id}.cookies.json"
        )

    def public(self) -> dict[str, Any]:
        """对外表示。**不含密码**（本来也不存），也不含 cookie 内容。"""
        return {
            "id": self.id,
            "owner_id": self.owner_id,
            "platform": self.platform,
            "label": self.label,
            "display": self.display,
            "username": self.username,
            "is_active": self.is_active,
            "created_at": self.created_at,
            "last_login_at": self.last_login_at,
            "last_login_ok": self.last_login_ok,
            "last_login_note": self.last_login_note,
        }


class AccountStore(SqliteStore):
    SCHEMA = """
    CREATE TABLE IF NOT EXISTS platform_accounts (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        owner_id        INTEGER NOT NULL,
        platform        TEXT NOT NULL,
        label           TEXT NOT NULL DEFAULT '',
        username        TEXT NOT NULL DEFAULT '',
        is_active       INTEGER NOT NULL DEFAULT 0,
        created_at      TEXT NOT NULL,
        last_login_at   TEXT,
        last_login_ok   INTEGER,
        last_login_note TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX IF NOT EXISTS idx_accounts_owner ON platform_accounts(owner_id, platform);
    CREATE TABLE IF NOT EXISTS active_accounts (
        owner_id   INTEGER NOT NULL,
        platform   TEXT NOT NULL,
        account_id INTEGER NOT NULL,
        PRIMARY KEY (owner_id, platform)
    );
    """

    def schema(self) -> str:
        return self.SCHEMA

    # -- 查询 -------------------------------------------------------------

    def list_for(
        self, owner_id: int | None = None, platform: str | None = None
    ) -> list[PlatformAccount]:
        """``owner_id=None`` 表示不限归属（超级管理员视角）。"""
        where, args = [], []
        if owner_id is not None:
            where.append("owner_id=?")
            args.append(owner_id)
        if platform:
            where.append("platform=?")
            args.append(platform)
        sql = "SELECT * FROM platform_accounts"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY platform, id"
        return [self._row(r) for r in self.fetchall(sql, args)]  # type: ignore[misc]

    def get(self, account_id: int) -> PlatformAccount | None:
        row = self.fetchone("SELECT * FROM platform_accounts WHERE id=?", (account_id,))
        return self._row(row) if row else None

    def active_any(self, platform: str, owner_id: int | None = None) -> PlatformAccount | None:
        """取该平台的一个可用账号。

        ``owner_id`` 给了就只看那个人；没给（CLI / 内部路径）就按 id 取最早的一个。
        这条路径存在是因为**命令行没有登录态**，但它仍然必须用账号库里
        绑定过的号，而不是环境变量里的明文密码。
        """
        rows = self.list_for(owner_id, platform)
        if not rows:
            return None
        for row in rows:
            if row.is_active:
                return row
        return rows[0]

    def active_for(self, owner_id: int, platform: str) -> PlatformAccount | None:
        row = self.fetchone(
            """SELECT a.* FROM platform_accounts a
               JOIN active_accounts x ON x.account_id = a.id
               WHERE x.owner_id=? AND x.platform=?""",
            (owner_id, platform),
        )
        if row is not None:
            return self._row(row)
        # 没有显式选过就退回该平台下最早添加的那个
        rows = self.list_for(owner_id, platform)
        return rows[0] if rows else None

    # -- 变更 -------------------------------------------------------------

    def add(
        self,
        owner_id: int,
        platform: str,
        *,
        label: str = "",
        username: str = "",
    ) -> PlatformAccount:
        platform = (platform or "").strip()
        if not platform:
            raise AccountError("缺少平台名")
        username = (username or "").strip()
        account_id = self.execute(
            """INSERT INTO platform_accounts
               (owner_id, platform, label, username, is_active, created_at, last_login_note)
               VALUES (?,?,?,?,0,?,'')""",
            (owner_id, platform, label.strip(), username, _iso_now()),
        )
        # 第一个号自动成为当前号，省得用户还要手动点一下
        if len(self.list_for(owner_id, platform)) == 1:
            self.set_active(owner_id, platform, account_id)
        account = self.get(account_id)
        assert account is not None
        return account

    def update(
        self,
        account_id: int,
        *,
        label: str | None = None,
        username: str | None = None,
    ) -> PlatformAccount:
        sets, args = [], []
        if label is not None:
            sets.append("label=?")
            args.append(label.strip())
        if username is not None:
            sets.append("username=?")
            args.append(username.strip())
        if sets:
            args.append(account_id)
            self.execute(
                f"UPDATE platform_accounts SET {', '.join(sets)} WHERE id=?", args
            )
        account = self.get(account_id)
        if account is None:
            raise AccountError("账号不存在", kind="not_found", status=404)
        return account

    def set_active(self, owner_id: int, platform: str, account_id: int) -> None:
        account = self.get(account_id)
        if account is None or account.owner_id != owner_id or account.platform != platform:
            raise AccountError("账号不属于该用户或该平台", kind="not_found", status=404)
        with self.cursor() as cur:
            cur.execute(
                """INSERT INTO active_accounts (owner_id, platform, account_id)
                   VALUES (?,?,?)
                   ON CONFLICT(owner_id, platform) DO UPDATE SET account_id=excluded.account_id""",
                (owner_id, platform, account_id),
            )
            cur.execute(
                "UPDATE platform_accounts SET is_active=0 WHERE owner_id=? AND platform=?",
                (owner_id, platform),
            )
            cur.execute(
                "UPDATE platform_accounts SET is_active=1 WHERE id=?", (account_id,)
            )

    def resolve_credentials(
        self, platform: str, *, owner_id: int | None = None, session_dir: str | Path = "."
    ) -> tuple[Any, str]:
        """返回 ``(Credentials, cookie_file)``；没有绑定账号则抛 ``AccountError``。

        密码留空 —— 这个号登录过之后，会话就在它自己的 cookie 文件里，
        ``refresh_session`` 会先探 ``is_logged_in()``，有效就不用再登录。
        真失效了会让用户重新输一次密码（``login_account``）。
        """
        from kedaibiao.core.models import Credentials

        account = self.active_any(platform, owner_id)
        if account is None:
            raise AccountError(
                f"{platform} 还没有绑定账号。请先在控制台「账号」页添加一个 "
                f"{platform} 账号（会真登录一次），之后命令行也会用它。",
                kind="no_credentials", status=400,
            )
        return (
            Credentials(username=account.username or "", password=""),
            str(account.cookie_path(session_dir)),
        )

    def record_login(self, account_id: int, *, ok: bool, note: str = "") -> None:
        self.execute(
            """UPDATE platform_accounts
               SET last_login_at=?, last_login_ok=?, last_login_note=?
               WHERE id=?""",
            (_iso_now(), int(ok), note[:500], account_id),
        )

    def delete(self, account_id: int) -> None:
        self.execute("DELETE FROM active_accounts WHERE account_id=?", (account_id,))
        self.execute("DELETE FROM platform_accounts WHERE id=?", (account_id,))

    def delete_for_owner(self, owner_id: int) -> None:
        self.execute(
            "DELETE FROM active_accounts WHERE account_id IN "
            "(SELECT id FROM platform_accounts WHERE owner_id=?)",
            (owner_id,),
        )
        self.execute("DELETE FROM platform_accounts WHERE owner_id=?", (owner_id,))

    def counts_by_owner(self) -> dict[int, int]:
        rows = self.fetchall(
            "SELECT owner_id, COUNT(*) AS c FROM platform_accounts GROUP BY owner_id"
        )
        return {int(r["owner_id"]): int(r["c"]) for r in rows}

    # -- 内部 -------------------------------------------------------------

    @staticmethod
    def _row(row: Any) -> PlatformAccount | None:
        if row is None:
            return None
        ok = row["last_login_ok"]
        return PlatformAccount(
            id=int(row["id"]),
            owner_id=int(row["owner_id"]),
            platform=row["platform"],
            label=row["label"] or "",
            username=row["username"] or "",
            is_active=bool(row["is_active"]),
            created_at=row["created_at"],
            last_login_at=row["last_login_at"],
            last_login_ok=None if ok is None else bool(ok),
            last_login_note=row["last_login_note"] or "",
        )
