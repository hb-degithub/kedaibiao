"""通知渠道抽象。

跑批是无人值守的，出错必须能把人叫醒，否则「全自动」等于「静默失败」。
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.request
from abc import ABC, abstractmethod
from typing import Any, ClassVar

log = logging.getLogger(__name__)


class Notifier(ABC):
    name: ClassVar[str] = ""

    @abstractmethod
    async def send(self, title: str, body: str, *, level: str = "info") -> bool:
        """发送一条通知，返回是否成功。实现方不得抛异常。"""


class ConsoleNotifier(Notifier):
    name = "console"

    def __init__(self) -> None:
        self.log = logging.getLogger("kedaibiao.notify")

    async def send(self, title: str, body: str, *, level: str = "info") -> bool:
        emit = {
            "error": self.log.error,
            "warning": self.log.warning,
            "success": self.log.info,
        }.get(level, self.log.info)
        emit("[%s] %s\n%s", level.upper(), title, body)
        return True


class NullNotifier(Notifier):
    """测试与 ``--quiet`` 用。"""

    name = "null"

    async def send(self, title: str, body: str, *, level: str = "info") -> bool:
        return True


class WebhookNotifier(Notifier):
    """通用 Webhook（Server 酱 / 钉钉 / 飞书 / Bark 都能套）。"""

    name = "webhook"

    def __init__(self, url: str, *, timeout: float = 10.0, extra: dict[str, Any] | None = None) -> None:
        if not url:
            raise ValueError("WebhookNotifier 需要 url")
        self.url = url
        self.timeout = timeout
        self.extra = extra or {}

    async def send(self, title: str, body: str, *, level: str = "info") -> bool:
        payload = {"title": title, "body": body, "level": level, **self.extra}
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(  # noqa: S310 - 用户显式提供 URL
            self.url, data=data, headers={"Content-Type": "application/json"}
        )
        try:
            # urllib 是阻塞的，丢到线程里避免卡住事件循环
            await asyncio.to_thread(
                urllib.request.urlopen, req, timeout=self.timeout  # noqa: S310
            )
        except Exception:  # noqa: BLE001 - 通知失败不能影响主流程
            log.exception("Webhook 通知失败: %s", self.url)
            return False
        return True
