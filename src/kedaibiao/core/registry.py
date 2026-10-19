"""通用注册表：平台适配器、通知渠道、答题器都靠它做插件发现。

刻意写成泛型而不是绑定 ``PlatformAdapter``，避免 ``core`` 反向依赖
``platforms`` 造成循环导入。
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from importlib.metadata import entry_points
from typing import Generic, TypeVar

T = TypeVar("T")


class AlreadyRegisteredError(KeyError):
    pass


class NotFoundError(KeyError):
    pass


class Registry(Generic[T]):
    """名字 -> 对象 的注册表，带 entry point 自动发现。"""

    def __init__(self, kind: str) -> None:
        self._kind = kind
        self._items: dict[str, T] = {}

    # -- 基本操作 ---------------------------------------------------------

    def register(self, name: str, item: T, *, override: bool = False) -> None:
        if not name:
            raise ValueError("注册名不能为空")
        if name in self._items and not override:
            raise AlreadyRegisteredError(f"{self._kind} 已注册: {name}")
        self._items[name] = item

    def unregister(self, name: str) -> None:
        self._items.pop(name, None)

    def get(self, name: str) -> T:
        try:
            return self._items[name]
        except KeyError:
            known = ", ".join(sorted(self._items)) or "<空>"
            raise NotFoundError(f"未找到 {self._kind}: {name!r}，已注册: {known}") from None

    def names(self) -> list[str]:
        return sorted(self._items)

    def __contains__(self, name: object) -> bool:
        return name in self._items

    def __iter__(self) -> Iterator[str]:
        return iter(self.names())

    def __len__(self) -> int:
        return len(self._items)

    # -- 插件发现 ---------------------------------------------------------

    def load_entry_points(self, group: str) -> int:
        """从已安装分发包的 entry point 装载插件，返回成功装载数。

        ``pyproject.toml`` 里的 ``[project.entry-points."kedaibiao.adapters"]``
        就是走这里。单个插件炸掉不应该拖垮整个装载流程。
        """
        loaded = 0
        for ep in self._iter_entry_points(group):
            try:
                obj = ep.load()
            except Exception:  # noqa: BLE001 - 插件质量问题不该阻断核心启动
                continue
            self.register(ep.name, obj, override=True)
            loaded += 1
        return loaded

    @staticmethod
    def _iter_entry_points(group: str) -> Iterable[object]:
        try:
            return entry_points(group=group)
        except TypeError:  # Python < 3.10 的旧签名
            return entry_points().get(group, [])  # type: ignore[attr-defined]
