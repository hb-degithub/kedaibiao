"""极简异步事件总线。

用途：任务状态流转、答题结果、异常都要能被 Web 控制台、日志、通知
同时消费，但又不想让核心去 import 这些消费者 —— 所以用事件解耦。
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

log = logging.getLogger(__name__)

Handler = Callable[["Event"], Awaitable[None] | None]


@dataclass(slots=True)
class Event:
    name: str
    payload: dict[str, Any] = field(default_factory=dict)
    at: datetime = field(default_factory=datetime.now)


class EventBus:
    def __init__(self) -> None:
        self._handlers: dict[str, list[Handler]] = defaultdict(list)
        self._wildcard: list[Handler] = []

    def on(self, name: str, handler: Handler) -> None:
        """订阅具体事件名；``"*"`` 表示订阅全部。"""
        if name == "*":
            self._wildcard.append(handler)
        else:
            self._handlers[name].append(handler)

    def off(self, name: str, handler: Handler) -> None:
        bucket = self._wildcard if name == "*" else self._handlers.get(name, [])
        if handler in bucket:
            bucket.remove(handler)

    async def emit(self, name: str, **payload: Any) -> None:
        """广播事件。单个订阅者抛错只记日志，不影响其他订阅者与主流程。"""
        event = Event(name=name, payload=payload)
        for handler in (*self._handlers.get(name, ()), *self._wildcard):
            try:
                result = handler(event)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:  # noqa: BLE001 - 事件消费者不应拖垮生产者
                log.exception("事件处理器失败: event=%s handler=%r", name, handler)
