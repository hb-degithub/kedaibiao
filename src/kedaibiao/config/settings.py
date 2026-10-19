"""配置加载。

优先级（低 -> 高）：内置默认值 < ``config/default.toml`` < ``config/local.toml``
< 环境变量（``KEDAIBIAO_`` 前缀）。

只读标准库 ``tomllib``（Python 3.11+），不引入 pydantic，保持核心零依赖。
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

from kedaibiao.config.dotenv import parse_dotenv

ENV_PREFIX = "KEDAIBIAO_"

_BOOL_TRUE = {"1", "true", "yes", "on"}


@dataclass(slots=True)
class Settings:
    env: str = "dev"
    log_level: str = "INFO"

    # 存储
    db_url: str = "sqlite:///./data/kedaibiao.db"

    # 浏览器
    headless: bool = True
    session_dir: str = "./data/sessions"

    # 调度
    max_concurrency: int = 3
    min_interval: float = 0.0
    max_attempts: int = 3

    # 安全：默认干跑，必须显式关闭才会真正提交
    dry_run: bool = True

    notify: str = "console"

    #: 低于该置信度的答案不自动提交
    min_confidence: float = 0.75

    @property
    def db_path(self) -> Path:
        """把 ``sqlite:///x`` 转成文件路径；非 sqlite 原样返回。"""
        prefix = "sqlite:///"
        if self.db_url.startswith(prefix):
            return Path(self.db_url[len(prefix) :])
        return Path(self.db_url)

    def with_env(self, environ: dict[str, str] | None = None) -> Settings:
        """在当前值之上叠加环境变量，返回新实例。

        注意这里必须是实例方法：``from_env`` 若直接 ``cls(**kwargs)``，
        会把调用方此前从 TOML 合并来的值整个丢掉。
        """
        env = environ if environ is not None else dict(os.environ)
        data = self.to_dict()
        for f in fields(self):
            raw = env.get(f"{ENV_PREFIX}{f.name.upper()}")
            if raw is not None:
                data[f.name] = _coerce(f.type, raw)
        return Settings(**data)

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> Settings:
        """仅从环境变量构造（内置默认值 + 环境变量）。"""
        return cls().with_env(environ)

    def merged(self, overrides: dict[str, Any]) -> Settings:
        """用 TOML 里的值覆盖当前设置（未识别的键直接忽略并报错）。"""
        known = {f.name for f in fields(self)}
        unknown = set(overrides) - known
        if unknown:
            raise ValueError(f"未知配置项: {', '.join(sorted(unknown))}")
        data = {f.name: getattr(self, f.name) for f in fields(self)}
        data.update(overrides)
        return Settings(**data)

    def to_dict(self) -> dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self)}


def _coerce(declared: Any, raw: str) -> Any:
    """按字段声明的类型转换环境变量字符串。"""
    name = declared if isinstance(declared, str) else getattr(declared, "__name__", str(declared))
    if name == "bool":
        return raw.strip().lower() in _BOOL_TRUE
    if name == "int":
        return int(raw)
    if name == "float":
        return float(raw)
    return raw


def resolve_environ(
    root: str | Path = ".",
    *,
    environ: dict[str, str] | None = None,
    use_dotenv: bool = True,
) -> dict[str, str]:
    """算出「本次运行到底认哪些环境变量」。

    优先级：``.env`` 文件 < 真实环境变量（CI/容器里临时导出的值永远压过文件）。

    为什么要有这个函数：``load_settings`` 只把合并结果用于自身的配置字段，
    与 ``doctor`` 的配置扫描都要读同一份环境。早期版本让它们各自读
    ``os.environ``，结果是 **``.env`` 里的值根本到不了使用方**。

    ⚠️ 注意：**平台凭据已经不走环境变量了**（见 ``security/accounts.py``）。
    这里只影响配置项（并发、headless 之类）。

    这里刻意**不回写 ``os.environ``**：全局副作用会让测试之间互相污染，
    也让「这次到底读了哪些环境变量」变得不可追踪。显式传下去更清楚。
    """
    if environ is not None:
        return dict(environ)
    file_env = parse_dotenv(Path(root) / ".env") if use_dotenv else {}
    return {**file_env, **os.environ}


def load_settings(
    *,
    root: str | Path = ".",
    environ: dict[str, str] | None = None,
    use_dotenv: bool = True,
) -> Settings:
    """按优先级装配配置。缺失的 TOML / .env 文件静默跳过。

    ``environ`` 为 ``None`` 时，会额外读取 ``<root>/.env``，但**真实环境变量
    优先** —— 这样 CI/容器里临时导出的值永远压过仓库里的文件。

    显式传入 ``environ`` 时完全不读 ``.env``，便于测试。

    ⚠️ **平台凭据不走这里**。请在控制台「账号」页绑定平台账号
    （``security.accounts``），凭据只从账号库来。
    """
    root = Path(root)
    resolved = resolve_environ(root, environ=environ, use_dotenv=use_dotenv)

    settings = Settings()
    for rel in ("config/default.toml", "config/local.toml"):
        path = root / rel
        if not path.is_file():
            continue
        with path.open("rb") as fh:
            data = tomllib.load(fh)
        # 允许 [kedaibiao] 包裹，也允许平铺
        section = data.get("kedaibiao", data)
        settings = settings.merged({k: v for k, v in section.items() if not isinstance(v, dict)})
    return settings.with_env(resolved)
