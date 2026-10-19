"""凭据层（**已弃用**，应用不再使用）。

> 平台凭据现在**只**来自控制台的账号管理（``security/accounts.py``：
> 绑定时真登录一次，之后只存该账号自己的会话 cookie，不落盘密码）。
> 这个模块保留下来给独立脚本用（它们没有控制台登录态），但
> **``Runtime`` / ``ConsoleService`` / CLI 的主流程都不再调用它**。
>
> 为什么去掉这条路径：两套来源并存时，用户永远搞不清「实际用的是哪个号」，
> 而且 ``.env`` 里是明文密码。

原始说明：把「某个平台的账号密码从哪来」这件事收敛到一处。

设计约束：

1. **绝不进版本库**：只从环境变量 / ``.env``（已被 .gitignore 排除）读取；
2. **绝不进日志**：返回值是 ``core.models.Credentials``，其 ``__repr__``
   已屏蔽密码；本模块另外提供 ``describe()`` 供 CLI/日志安全打印；
3. **命名可预测**：``KEDAIBIAO_<平台>_USER`` / ``_PASS`` / ``_TOKEN``，
   平台名大写（``unipus`` → ``KEDAIBIAO_UNIPUS_USER``）。

用扁平环境变量而不是配置文件，是为了让 CI/容器注入凭据的路径保持统一，
也避免在磁盘上多留一份明文密码。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from kedaibiao.core.models import Credentials

ENV_PREFIX = "KEDAIBIAO_"

#: 平台级额外字段的已知后缀（除 USER/PASS 外的小写键名）
KNOWN_EXTRA_FIELDS = ("token", "school", "tenant", "openid")


class CredentialError(RuntimeError):
    """凭据缺失或不可用。"""


def env_key(platform: str, field_name: str) -> str:
    """``('unipus', 'user')`` → ``'KEDAIBIAO_UNIPUS_USER'``。"""
    return f"{ENV_PREFIX}{platform.upper()}_{field_name.upper()}"


@dataclass(slots=True)
class CredentialSource:
    """记录凭据「从哪个键来的」（值本身不落日志），便于排查配置问题。"""

    platform: str
    keys: list[str] = field(default_factory=list)

    def describe(self) -> str:
        return f"{self.platform}: {', '.join(self.keys) if self.keys else '<无>'}"


def load_credentials(
    platform: str,
    *,
    environ: dict[str, str] | None = None,
    require: bool = True,
) -> Credentials | None:
    """按约定从环境变量装配某平台的凭据。

    ``require=True``（默认）时，连用户名/令牌都没有就抛 ``CredentialError``；
    否则返回 ``None``，交由调用方决定是否跳过该平台。
    """
    env = environ if environ is not None else dict(os.environ)
    username = env.get(env_key(platform, "user"), "").strip()
    password = env.get(env_key(platform, "pass"), "")
    extra: dict[str, str] = {}
    used: list[str] = []

    if username:
        used.append(env_key(platform, "user"))
    if password:
        used.append(env_key(platform, "pass"))

    for name in KNOWN_EXTRA_FIELDS:
        value = env.get(env_key(platform, name), "").strip()
        if value:
            extra[name] = value
            used.append(env_key(platform, name))

    if not username and not extra:
        if require:
            raise CredentialError(
                f"未找到 {platform} 的凭据。请在 .env 或环境变量中设置 "
                f"{env_key(platform, 'user')}（密码用 {env_key(platform, 'pass')}，"
                f"若平台用令牌则设 {env_key(platform, 'token')}）。"
                f".env.example 有模板；.env 已被 .gitignore 排除，不会入库。"
            )
        return None

    return Credentials(username=username, password=password, extra=extra)


def describe(credentials: Credentials) -> str:
    """安全打印：只暴露用户名与「有哪些额外字段」，绝不输出密码/令牌值。"""
    extra_names = ", ".join(sorted(credentials.extra)) or "无"
    return (
        f"username={credentials.username!r} "
        f"password={'已设置' if credentials.password else '未设置'} "
        f"额外字段=[{extra_names}]"
    )


def available_platforms(environ: dict[str, str] | None = None) -> list[str]:
    """扫描环境变量，列出配置了凭据的平台名（小写）。

    用于 CLI 自检：不打印任何值，只说「哪几个平台有凭据」。
    """
    env = environ if environ is not None else dict(os.environ)
    found: set[str] = set()
    for key in env:
        if not key.startswith(ENV_PREFIX) or not key.endswith("_USER"):
            continue
        middle = key[len(ENV_PREFIX) : -len("_USER")]
        if middle:
            found.add(middle.lower())
    return sorted(found)
