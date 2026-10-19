"""平台适配器契约。

新增一个平台的完整流程：
1. 新建 ``kedaibiao/platforms/<平台>/``；
2. 继承 ``PlatformAdapter``，实现需要的钩子；
3. 在 ``pyproject.toml`` 的 entry point 里注册，或在 ``platforms/__init__.py``
   里手工 ``registry.register(...)``。

任何平台私有概念都不允许泄漏到 ``core`` —— 全部翻译成 ``core.models``。
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, ClassVar

from kedaibiao.core.events import EventBus
from kedaibiao.core.models import (
    Answer,
    Assignment,
    Course,
    Credentials,
    Question,
    SubmissionResult,
)


class NotSupportedError(NotImplementedError):
    """适配器不支持该能力（先看 ``capabilities`` 再调用）。"""


@dataclass(slots=True, frozen=True)
class Capabilities:
    """能力声明。调度器据此决定哪些阶段可以跳过。"""

    login: bool = True
    list_courses: bool = True
    list_assignments: bool = True
    fetch_questions: bool = True
    submit: bool = True
    course_playback: bool = False


@dataclass(slots=True)
class AdapterContext:
    """适配器拿到的全部外部依赖。

    用 ``Any`` 声明可选依赖（playwright / httpx），这样没装浏览器依赖时
    核心依然可以导入并跑单测。
    """

    bus: EventBus = field(default_factory=EventBus)
    browser: Any | None = None  # kedaibiao.browser.session.BrowserSession
    bank: Any | None = None  # kedaibiao.bank.store.QuestionBank
    notifier: Any | None = None
    store: Any | None = None  # kedaibiao.bank.store.ProgressStore
    settings: Any | None = None
    logger: logging.Logger = field(default_factory=lambda: logging.getLogger("kedaibiao"))


class PlatformAdapter(ABC):
    #: 机器名，全小写，用作 registry key 与 DB 字段
    name: ClassVar[str] = ""
    #: 展示名
    display_name: ClassVar[str] = ""
    #: 能力声明
    capabilities: ClassVar[Capabilities] = Capabilities()

    def __init__(self, ctx: AdapterContext | None = None) -> None:
        self.ctx = ctx or AdapterContext()
        self.log = self.ctx.logger.getChild(self.name or type(self).__name__)

    # -- 生命周期 ---------------------------------------------------------

    @abstractmethod
    async def login(self, credentials: Credentials) -> bool:
        """登录并持久化会话。返回是否成功。"""

    async def is_logged_in(self) -> bool:
        """会话是否仍然有效。默认保守返回 False，触发重新登录。"""
        return False

    async def close(self) -> None:
        """释放资源。默认无操作。"""

    async def __aenter__(self) -> PlatformAdapter:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    # -- 数据获取 ---------------------------------------------------------

    async def list_courses(self) -> list[Course]:
        if not self.capabilities.list_courses:
            raise NotSupportedError(f"{self.name} 不支持 list_courses")
        return []

    async def list_assignments(self, course: Course) -> list[Assignment]:
        if not self.capabilities.list_assignments:
            raise NotSupportedError(f"{self.name} 不支持 list_assignments")
        return []

    async def fetch_questions(self, assignment: Assignment) -> list[Question]:
        if not self.capabilities.fetch_questions:
            raise NotSupportedError(f"{self.name} 不支持 fetch_questions")
        return []

    # -- 写操作 -----------------------------------------------------------

    async def submit(
        self, assignment: Assignment, answers: list[Answer]
    ) -> SubmissionResult:
        if not self.capabilities.submit:
            raise NotSupportedError(f"{self.name} 不支持 submit")
        raise NotSupportedError(f"{self.name} 未实现 submit")

    # -- 辅助 -------------------------------------------------------------

    async def refresh_session(self, credentials: Credentials) -> bool:
        """会话失效时的统一入口：先探测，再决定是否重登。"""
        if await self.is_logged_in():
            return True
        self.log.info("会话失效，重新登录: %s", self.name)
        return await self.login(credentials)
