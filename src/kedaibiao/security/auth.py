"""控制台的账号体系：用户、角色、会话。

## 角色

- ``superadmin`` 超级管理员：能看所有人的数据，能管理用户；
- ``user`` 普通用户：只看得到自己的数据。

**数据隔离靠 owner_id**，不靠界面隐藏 —— 界面隐藏只是体验，越权必须由
存储层和服务层挡住（见 ``bank/store.py`` 与 ``api/service.py`` 的 scoping）。

## 密码

用 ``hashlib.scrypt``（标准库，无需第三方依赖），每个用户独立随机盐。
存储格式：``scrypt$n$r$p$<salt_hex>$<hash_hex>`` —— 参数写在里面，
将来调参不会让老密码失效。
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from kedaibiao.db import SqliteStore

log = logging.getLogger(__name__)

ROLE_SUPERADMIN = "superadmin"
ROLE_USER = "user"
ROLES = (ROLE_SUPERADMIN, ROLE_USER)

#: 会话默认有效期
SESSION_TTL = timedelta(days=7)

#: 密码长度下限 —— 这个控制台能触发跑批，太短的密码等于没设
MIN_PASSWORD_LENGTH = 8

_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32


class AuthError(Exception):
    """认证/授权失败。``kind`` 供 HTTP 层映射状态码。"""

    def __init__(self, message: str, *, kind: str = "unauthorized", status: int = 401) -> None:
        super().__init__(message)
        self.kind = kind
        self.status = status


# --------------------------------------------------------------------------
# 密码
# --------------------------------------------------------------------------


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    if not password:
        raise ValueError("密码不能为空")
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """恒定时间比较。任何解析失败都返回 False，不抛异常。"""
    if not password or not stored:
        return False
    try:
        algo, n, r, p, salt_hex, hash_hex = stored.split("$")
        if algo != "scrypt":
            return False
        expected = bytes.fromhex(hash_hex)
        dk = hashlib.scrypt(
            password.encode("utf-8"),
            salt=bytes.fromhex(salt_hex),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk, expected)


def validate_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise AuthError(
            f"密码至少 {MIN_PASSWORD_LENGTH} 位", kind="bad_request", status=400
        )


# --------------------------------------------------------------------------
# 模型
# --------------------------------------------------------------------------


@dataclass(slots=True)
class User:
    id: int
    username: str
    role: str = ROLE_USER
    created_at: str = ""
    disabled: bool = False
    note: str = ""

    @property
    def is_superadmin(self) -> bool:
        return self.role == ROLE_SUPERADMIN

    def public(self) -> dict[str, Any]:
        """对外表示。**绝不含密码哈希。**"""
        return {
            "id": self.id,
            "username": self.username,
            "role": self.role,
            "created_at": self.created_at,
            "disabled": self.disabled,
            "note": self.note,
        }


@dataclass(slots=True)
class Session:
    token: str
    user_id: int
    created_at: str
    expires_at: str


def _now() -> datetime:
    return datetime.now()


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# 存储
# --------------------------------------------------------------------------


class AuthStore(SqliteStore):
    SCHEMA = """
    CREATE TABLE IF NOT EXISTS users (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        username      TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        role          TEXT NOT NULL DEFAULT 'user',
        disabled      INTEGER NOT NULL DEFAULT 0,
        note          TEXT NOT NULL DEFAULT '',
        created_at    TEXT NOT NULL,
        updated_at    TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS sessions (
        token      TEXT PRIMARY KEY,
        user_id    INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
    );
    CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
    CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);
    """

    def schema(self) -> str:
        return self.SCHEMA

    def migrate(self) -> None:
        # 第一版就是完整 schema；保留钩子给将来的列变更
        self.add_column_if_missing("users", "note", "TEXT NOT NULL DEFAULT ''")
        self.add_column_if_missing("users", "disabled", "INTEGER NOT NULL DEFAULT 0")

    # -- 用户 -------------------------------------------------------------

    def count_users(self) -> int:
        return int(self.scalar("SELECT COUNT(*) FROM users") or 0)

    def get_user_by_name(self, username: str, *, with_hash: bool = False) -> User | None:
        row = self.fetchone("SELECT * FROM users WHERE username=?", (username,))
        return self._row_to_user(row, with_hash=with_hash) if row else None

    def get_user_by_id(self, user_id: int) -> User | None:
        row = self.fetchone("SELECT * FROM users WHERE id=?", (user_id,))
        return self._row_to_user(row) if row else None

    def list_users(self) -> list[User]:
        rows = self.fetchall("SELECT * FROM users ORDER BY id")
        return [self._row_to_user(r) for r in rows]  # type: ignore[misc]

    def create_user(
        self, username: str, password: str, *, role: str = ROLE_USER, note: str = ""
    ) -> User:
        username = (username or "").strip()
        if not username:
            raise AuthError("用户名不能为空", kind="bad_request", status=400)
        if role not in ROLES:
            raise AuthError(f"未知角色: {role}", kind="bad_request", status=400)
        validate_password(password)
        if self.get_user_by_name(username) is not None:
            raise AuthError(f"用户名已存在: {username}", kind="bad_request", status=409)

        now = _iso(_now())
        with self.cursor() as cur:
            cur.execute(
                """INSERT INTO users (username, password_hash, role, disabled, note,
                                      created_at, updated_at)
                   VALUES (?,?,?,0,?,?,?)""",
                (username, hash_password(password), role, note, now, now),
            )
            user_id = int(cur.lastrowid or 0)
        user = self.get_user_by_id(user_id)
        assert user is not None
        return user

    def verify_login(self, username: str, password: str) -> User | None:
        """校验账号密码。失败一律返回 ``None``，不区分「用户不存在」与「密码错」。"""
        row = self.fetchone("SELECT * FROM users WHERE username=?", ((username or "").strip(),))
        if row is None:
            # 仍然跑一次哈希，避免用响应时间探测用户名是否存在
            verify_password(password, hash_password("dummy-password"))
            return None
        if not verify_password(password, row["password_hash"]):
            return None
        user = self._row_to_user(row)
        assert user is not None
        if user.disabled:
            return None
        return user

    def set_password(self, user_id: int, password: str) -> None:
        validate_password(password)
        self.execute(
            "UPDATE users SET password_hash=?, updated_at=? WHERE id=?",
            (hash_password(password), _iso(_now()), user_id),
        )
        # 改密码后踢掉所有会话
        self.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))

    def set_disabled(self, user_id: int, disabled: bool) -> None:
        self.execute(
            "UPDATE users SET disabled=?, updated_at=? WHERE id=?",
            (int(disabled), _iso(_now()), user_id),
        )
        if disabled:
            self.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))

    def set_role(self, user_id: int, role: str) -> None:
        if role not in ROLES:
            raise AuthError(f"未知角色: {role}", kind="bad_request", status=400)
        self.execute(
            "UPDATE users SET role=?, updated_at=? WHERE id=?",
            (role, _iso(_now()), user_id),
        )

    def rename_user(self, user_id: int, username: str) -> None:
        username = (username or "").strip()
        if not username:
            raise AuthError("用户名不能为空", kind="bad_request", status=400)
        existing = self.get_user_by_name(username)
        if existing is not None and existing.id != user_id:
            raise AuthError(f"用户名已存在: {username}", kind="bad_request", status=409)
        self.execute(
            "UPDATE users SET username=?, updated_at=? WHERE id=?",
            (username, _iso(_now()), user_id),
        )

    def delete_user(self, user_id: int) -> None:
        if self.count_superadmins() <= 1:
            target = self.get_user_by_id(user_id)
            if target is not None and target.is_superadmin:
                raise AuthError(
                    "至少要保留一个超级管理员", kind="bad_request", status=400
                )
        self.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
        self.execute("DELETE FROM users WHERE id=?", (user_id,))

    def count_superadmins(self) -> int:
        return int(
            self.scalar("SELECT COUNT(*) FROM users WHERE role=?", (ROLE_SUPERADMIN,)) or 0
        )

    # -- 会话 -------------------------------------------------------------

    def create_session(self, user_id: int, *, ttl: timedelta = SESSION_TTL) -> Session:
        token = secrets.token_urlsafe(32)
        now = _now()
        session = Session(
            token=token, user_id=user_id, created_at=_iso(now), expires_at=_iso(now + ttl)
        )
        self.execute(
            "INSERT INTO sessions (token, user_id, created_at, expires_at) VALUES (?,?,?,?)",
            (session.token, session.user_id, session.created_at, session.expires_at),
        )
        return session

    def get_session(self, token: str) -> Session | None:
        if not token:
            return None
        row = self.fetchone("SELECT * FROM sessions WHERE token=?", (token,))
        if row is None:
            return None
        if row["expires_at"] <= _iso(_now()):
            self.delete_session(token)
            return None
        return Session(
            token=row["token"],
            user_id=row["user_id"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
        )

    def delete_session(self, token: str) -> None:
        self.execute("DELETE FROM sessions WHERE token=?", (token,))

    def delete_sessions_for(self, user_id: int) -> None:
        self.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))

    def purge_expired_sessions(self) -> int:
        with self.cursor() as cur:
            cur.execute("DELETE FROM sessions WHERE expires_at <= ?", (_iso(_now()),))
            return cur.rowcount

    def session_count(self) -> int:
        return int(self.scalar("SELECT COUNT(*) FROM sessions") or 0)

    # -- 首次启动 ---------------------------------------------------------

    def ensure_bootstrap_admin(
        self, *, username: str = "admin", password: str | None = None
    ) -> tuple[User | None, str | None]:
        """没有用户时创建一个超管。

        返回 ``(用户, 自动生成的密码)``；密码只在**自动生成**时返回一次，
        调用方必须立刻打印给用户，之后再也拿不到（库里只有哈希）。
        """
        if self.count_users() > 0:
            return None, None

        generated: str | None = None
        if password:
            validate_password(password)
        else:
            generated = secrets.token_urlsafe(12)
            password = generated

        user = self.create_user(username, password, role=ROLE_SUPERADMIN, note="首次启动自动创建")
        return user, generated

    # -- 内部 -------------------------------------------------------------

    @staticmethod
    def _row_to_user(row: Any, *, with_hash: bool = False) -> User | None:
        if row is None:
            return None
        return User(
            id=int(row["id"]),
            username=row["username"],
            role=row["role"],
            created_at=row["created_at"],
            disabled=bool(row["disabled"]),
            note=row["note"] or "",
        )
