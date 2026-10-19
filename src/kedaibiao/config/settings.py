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


def load_settings(
    *,
    root: str | Path = ".",
    environ: dict[str, str] | None = None,
) -> Settings:
    """按优先级装配配置。缺失的 TOML 文件静默跳过。"""
    root = Path(root)
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
    return settings.with_env(environ)
