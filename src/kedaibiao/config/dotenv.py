"""极简 ``.env`` 解析（标准库，不引入 python-dotenv）。

为什么需要：仓库里有 ``.env.example``，但 ``Settings`` 只读 ``os.environ``，
实际没有任何代码把 ``.env`` 加载进去 —— 照着示例建了 ``.env`` 也不会生效。

约定：
- 只做 ``KEY=VALUE``，不执行任何命令、不做变量展开（避免安全面）；
- ``#`` 开头是注释，行内 `` #`` 之后也当注释（除非值被引号包住）；
- 支持单/双引号包裹，支持 ``export `` 前缀；
- **真实环境变量优先于 ``.env`` 文件**（与 docker/CI 的直觉一致）。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

_LINE_RE = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")


class DotEnvSyntaxError(ValueError):
    pass


def parse_dotenv(path: str | Path) -> dict[str, str]:
    """解析 ``.env`` 文件，返回键值对。文件不存在返回空字典。"""
    src = Path(path)
    if not src.is_file():
        return {}

    result: dict[str, str] = {}
    for lineno, raw in enumerate(src.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _LINE_RE.match(line)
        if match is None:
            raise DotEnvSyntaxError(f"{src}:{lineno} 无法解析: {raw!r}")
        key, value = match.group(1), match.group(2).strip()
        result[key] = _unquote(value)
    return result


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        inner = value[1:-1]
        if value[0] == '"':
            # 双引号内才处理转义
            return inner.replace("\\n", "\n").replace("\\t", "\t").replace('\\"', '"')
        return inner
    # 未加引号：`` #`` 之后视为行内注释
    hash_at = value.find(" #")
    if hash_at != -1:
        value = value[:hash_at]
    return value.strip()


def load_dotenv(path: str | Path, *, override: bool = False) -> int:
    """把 ``.env`` 写进 ``os.environ``，返回写入条目数。

    ``override=False``（默认）时不覆盖已存在的真实环境变量 —— 这样 CI 里
    临时导出的变量永远压过仓库里的 ``.env``。
    """
    values = parse_dotenv(path)
    written = 0
    for key, value in values.items():
        if not override and key in os.environ:
            continue
        os.environ[key] = value
        written += 1
    return written
