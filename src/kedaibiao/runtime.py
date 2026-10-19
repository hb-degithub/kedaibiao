"""运行时装配：把配置、存储、传输、调度、流水线拼成一个可直接跑的对象。

为什么要单独一层：CLI（``__main__``）和将来的 Web 控制面（``api/``）需要
**完全相同**的装配逻辑。如果各拼各的，两边行为会悄悄分叉 —— 最典型的
后果是 Web 面板上显示的成功率与 CLI 跑出来的对不上。

本模块位于应用层，可以依赖下面所有层。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kedaibiao import adapters
from kedaibiao.bank.store import ProgressStore, QuestionBank
from kedaibiao.config.settings import Settings, load_settings
from kedaibiao.core.events import EventBus
from kedaibiao.core.scheduler import Scheduler
from kedaibiao.net.http import HttpClient, UrllibTransport
from kedaibiao.notify.base import ConsoleNotifier, Notifier, NullNotifier, WebhookNotifier
from kedaibiao.platforms.base import AdapterContext, PlatformAdapter
from kedaibiao.security.credentials import load_credentials
from kedaibiao.tasks.answerer import BankAnswerer, ChainAnswerer, RuleAnswerer
from kedaibiao.tasks.pipeline import Pipeline, PipelineConfig, PipelineReport

log = logging.getLogger(__name__)

NOTIFY_WEBHOOK_ENV = "KEDAIBIAO_NOTIFY_WEBHOOK_URL"


def build_notifier(settings: Settings) -> Notifier:
    kind = (settings.notify or "console").strip().lower()
    if kind == "null":
        return NullNotifier()
    if kind == "webhook":
        url = os.environ.get(NOTIFY_WEBHOOK_ENV, "")
        if not url:
            log.warning("%s 未设置，webhook 通知降级为控制台", NOTIFY_WEBHOOK_ENV)
            return ConsoleNotifier()
        return WebhookNotifier(url)
    return ConsoleNotifier()


def build_http_client(settings: Settings) -> HttpClient:
    """装配协议型平台用的 HTTP 客户端，并把 cookie 落盘以复用登录态。"""
    transport = UrllibTransport()
    cookies = Path(settings.session_dir) / "http_cookies.json"
    restored = transport.load_cookies(cookies)
    if restored:
        log.info("已恢复 %d 条 cookie: %s", restored, cookies)
    return HttpClient(transport)


def build_context(
    settings: Settings,
    *,
    bus: EventBus | None = None,
    bank: QuestionBank | None = None,
    store: ProgressStore | None = None,
    notifier: Notifier | None = None,
) -> AdapterContext:
    return AdapterContext(
        bus=bus or EventBus(),
        http=build_http_client(settings),
        bank=bank,
        store=store,
        notifier=notifier,
        settings=settings,
    )


@dataclass(slots=True)
class Runtime:
    """一次性装配好的运行环境。用完记得 ``close()``。"""

    settings: Settings
    bank: QuestionBank
    store: ProgressStore
    bus: EventBus
    pipeline: Pipeline
    notifier: Notifier

    def make_adapter(self, platform: str) -> PlatformAdapter:
        if platform not in adapters:
            known = ", ".join(adapters.names()) or "<未注册任何适配器>"
            raise KeyError(f"未注册的平台: {platform!r}；已注册: {known}")
        cls: Any = adapters.get(platform)
        # 每个适配器一份独立的 http（cookie 不能跨平台串）
        ctx = build_context(
            self.settings,
            bus=self.bus,
            bank=self.bank,
            store=self.store,
            notifier=self.notifier,
        )
        return cls(ctx)

    async def run(self, platform: str, *, credentials: Any = None) -> PipelineReport:
        # 先确认平台存在，再要凭据 —— 否则没注册的平台会先报「缺凭据」，误导排查方向
        adapter = self.make_adapter(platform)
        creds = credentials if credentials is not None else load_credentials(platform, require=True)
        try:
            return await self.pipeline.run(adapter, creds)
        finally:
            await adapter.close()
            self._persist_cookies(adapter)

    def _persist_cookies(self, adapter: PlatformAdapter) -> None:
        transport = getattr(getattr(adapter.ctx, "http", None), "transport", None)
        if isinstance(transport, UrllibTransport):
            path = Path(self.settings.session_dir) / "http_cookies.json"
            transport.save_cookies(path)

    def close(self) -> None:
        self.bank.close()
        self.store.close()


def build_runtime(
    *,
    root: str | Path = ".",
    settings: Settings | None = None,
    config: PipelineConfig | None = None,
) -> Runtime:
    settings = settings or load_settings(root=root)

    bus = EventBus()
    bank = QuestionBank(settings.db_path)
    store = ProgressStore(settings.db_path)
    notifier = build_notifier(settings)

    answerer = ChainAnswerer(
        [BankAnswerer(bank), RuleAnswerer()],
        min_confidence=settings.min_confidence,
    )
    pipeline = Pipeline(
        answerer=answerer,
        bank=bank,
        store=store,
        bus=bus,
        scheduler=Scheduler(settings.max_concurrency, settings.min_interval),
        notifier=notifier,
        config=config
        or PipelineConfig(
            dry_run=settings.dry_run,
            min_confidence=settings.min_confidence,
        ),
    )
    return Runtime(
        settings=settings,
        bank=bank,
        store=store,
        bus=bus,
        pipeline=pipeline,
        notifier=notifier,
    )
