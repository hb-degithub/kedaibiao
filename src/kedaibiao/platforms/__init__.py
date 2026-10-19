"""平台适配器层。每个平台一个子包，只依赖 ``core``、``net``、``browser``。

## 注册

``load_builtin_adapters()`` 自动发现 ``platforms/`` 下的子包：

- 逐个导入子包（导入失败只记日志并跳过）；
- 在模块里找出 ``PlatformAdapter`` 的具体子类，用类上的 ``name`` 注册；
- 再补上已安装分发包的 entry point（``kedaibiao.adapters`` 组）。

**为什么是自动发现而不是手工列表**：并行开发时手工列表意味着每加一个平台
都要改这个共享文件，多窗口必然冲突。自动发现让「新增平台」变成纯新增文件，
共享区零改动。

同时保留 entry point：装成包发布时用得上；源码直跑（``PYTHONPATH=src``）
时靠自动发现，两条路都通。
"""

from __future__ import annotations

import importlib
import inspect
import logging
import pkgutil
from typing import Any

from kedaibiao import adapters
from kedaibiao.platforms.base import (
    AdapterContext,
    Capabilities,
    NotSupportedError,
    PlatformAdapter,
)

log = logging.getLogger(__name__)

#: entry point 组名，与 pyproject.toml 保持一致
ADAPTER_ENTRY_POINT_GROUP = "kedaibiao.adapters"


def _iter_adapter_classes(module: Any) -> list[type[PlatformAdapter]]:
    """找出模块里所有「可用的」适配器类。

    过滤条件：是 ``PlatformAdapter`` 的具体子类（抽象基类与中间基类不算），
    且 ``name`` 非空 —— ``name`` 是注册键，空的等于没登记。
    按类对象去重，避免同一个类被 re-export 多次。
    """
    found: list[type[PlatformAdapter]] = []
    seen: set[int] = set()
    for value in vars(module).values():
        if not inspect.isclass(value) or id(value) in seen:
            continue
        if not issubclass(value, PlatformAdapter) or value is PlatformAdapter:
            continue
        if inspect.isabstract(value) or not getattr(value, "name", ""):
            continue
        seen.add(id(value))
        found.append(value)
    return found


def load_builtin_adapters() -> int:
    """自动发现并注册所有可用适配器，返回成功装载的数量。

    **单个子包导入失败只记日志并跳过。** 两个理由：

    - 一个平台的问题不该让整个框架起不来（``kedaibiao platforms`` 还得能跑）；
    - 并行开发时，别的窗口正在写的半成品不该拖垮当前窗口。

    代价是「适配器写了但没注册上」会被静默吞掉，所以用 ``debug`` 级别记录
    异常 —— 需要排查时把日志级别调低就能看到原因。
    """
    loaded = 0
    package = importlib.import_module("kedaibiao.platforms")

    for info in pkgutil.iter_modules(package.__path__):
        if not info.ispkg:
            continue
        module_path = f"{package.__name__}.{info.name}"
        try:
            module = importlib.import_module(module_path)
        except Exception:  # noqa: BLE001 - 见 docstring：容错是有意的
            log.debug("平台子包 %s 导入失败，跳过", module_path, exc_info=True)
            continue

        for cls in _iter_adapter_classes(module):
            adapters.register(cls.name, cls, override=True)
            loaded += 1
            log.debug("已注册平台适配器 %s -> %s", cls.name, cls.__name__)

    # 已安装分发包声明的 entry point（与自动发现重复时以后者为准，类相同）
    loaded += adapters.load_entry_points(ADAPTER_ENTRY_POINT_GROUP)
    return loaded


__all__ = [
    "ADAPTER_ENTRY_POINT_GROUP",
    "AdapterContext",
    "Capabilities",
    "NotSupportedError",
    "PlatformAdapter",
    "load_builtin_adapters",
]
