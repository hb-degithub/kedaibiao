"""通知渠道抽象。

跑批是无人值守的，出错必须能把人叫醒，否则「全自动」等于「静默失败」。
"""

from __future__ import annotations

import asyncio
import json
import logging
import smtplib
import ssl
import urllib.request
from abc import ABC, abstractmethod
from contextlib import suppress
from email.message import EmailMessage
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

    def __init__(
        self, url: str, *, timeout: float = 10.0, extra: dict[str, Any] | None = None
    ) -> None:
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


class EmailNotifier(Notifier):
    """SMTP 邮件通知（标准库实现，不引第三方依赖）。

    为什么需要它：``webhook`` 需要你先有一个可用的推送服务；无人值守跑批
    出问题时，邮件是最低门槛、最不容易失效的兜底渠道。

    配置全部走环境变量（``.env`` 同样生效）：

    | 变量 | 必填 | 说明 |
    | --- | --- | --- |
    | ``KEDAIBIAO_SMTP_HOST`` | ✅ | SMTP 服务器 |
    | ``KEDAIBIAO_SMTP_TO`` | ✅ | 收件人，逗号分隔 |
    | ``KEDAIBIAO_SMTP_PORT`` | — | 默认 587 |
    | ``KEDAIBIAO_SMTP_USER`` | — | 登录用户名（为空则不登录） |
    | ``KEDAIBIAO_SMTP_PASSWORD`` | — | 登录密码 / 授权码 |
    | ``KEDAIBIAO_SMTP_FROM`` | — | 发件人，默认取 user |
    | ``KEDAIBIAO_SMTP_SECURITY`` | — | ``starttls``（默认）/ ``ssl`` / ``none`` |
    | ``KEDAIBIAO_SMTP_TIMEOUT`` | — | 秒，默认 15 |

    失败只记日志、返回 ``False``，绝不抛异常 —— 通知失败不能把跑批结果带崩。
    日志与异常文案里都**不出现密码**。
    """

    name = "email"

    def __init__(
        self,
        *,
        host: str,
        recipients: list[str],
        port: int = 587,
        user: str = "",
        password: str = "",
        sender: str = "",
        security: str = "starttls",
        timeout: float = 15.0,
    ) -> None:
        if not host:
            raise ValueError("EmailNotifier 需要 SMTP host")
        recipients = [item.strip() for item in recipients if item and item.strip()]
        if not recipients:
            raise ValueError("EmailNotifier 需要至少一个收件人")
        self.host = host
        self.port = port
        self.user = user
        self._password = password
        self.sender = sender or user
        self.recipients = recipients
        self.security = security.strip().lower()
        self.timeout = timeout

    def __repr__(self) -> str:  # pragma: no cover - 仅用于排查
        return f"EmailNotifier(host={self.host!r}, to={len(self.recipients)} 人)"

    @classmethod
    def from_environ(cls, environ: dict[str, str]) -> EmailNotifier | None:
        """从环境变量构造；必填项缺失返回 ``None``（由调用方降级）。"""
        host = (environ.get("KEDAIBIAO_SMTP_HOST") or "").strip()
        raw_to = (environ.get("KEDAIBIAO_SMTP_TO") or "").strip()
        if not host or not raw_to:
            return None
        try:
            port = int(environ.get("KEDAIBIAO_SMTP_PORT") or 587)
        except ValueError:
            port = 587
        try:
            timeout = float(environ.get("KEDAIBIAO_SMTP_TIMEOUT") or 15.0)
        except ValueError:
            timeout = 15.0
        return cls(
            host=host,
            port=port,
            recipients=raw_to.split(","),
            user=(environ.get("KEDAIBIAO_SMTP_USER") or "").strip(),
            password=environ.get("KEDAIBIAO_SMTP_PASSWORD") or "",
            sender=(environ.get("KEDAIBIAO_SMTP_FROM") or "").strip(),
            security=(environ.get("KEDAIBIAO_SMTP_SECURITY") or "starttls"),
            timeout=timeout,
        )

    async def send(self, title: str, body: str, *, level: str = "info") -> bool:
        message = EmailMessage()
        message["Subject"] = f"[课代表][{level.upper()}] {title}"
        message["From"] = self.sender or "kedaibiao@localhost"
        message["To"] = ", ".join(self.recipients)
        message.set_content(body)
        try:
            await asyncio.to_thread(self._send_sync, message)
        except Exception as exc:  # noqa: BLE001 - 通知失败不能影响主流程
            log.error("邮件通知失败（host=%s）：%s", self.host, type(exc).__name__)
            return False
        return True

    def _send_sync(self, message: EmailMessage) -> None:
        if self.security == "ssl":
            client: smtplib.SMTP = smtplib.SMTP_SSL(
                self.host, self.port, timeout=self.timeout,
                context=ssl.create_default_context(),
            )
        else:
            client = smtplib.SMTP(self.host, self.port, timeout=self.timeout)
        try:
            if self.security == "starttls":
                client.ehlo()
                client.starttls(context=ssl.create_default_context())
                client.ehlo()
            if self.user:
                client.login(self.user, self._password)
            client.send_message(message)
        finally:
            # 服务端先断开时 quit 会抛，这里不关心；不能盖住前面的真实错误
            with suppress(Exception):
                client.quit()
