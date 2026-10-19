"""运行时装配：把配置、存储、传输、调度、流水线拼成一个可直接跑的对象。

为什么要单独一层：CLI（``__main__``）和将来的 Web 控制面（``api/``）需要
**完全相同**的装配逻辑。如果各拼各的，两边行为会悄悄分叉 —— 最典型的
后果是 Web 面板上显示的成功率与 CLI 跑出来的对不上。

本模块位于应用层，可以依赖下面所有层。
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kedaibiao import adapters
from kedaibiao.bank.store import ProgressStore, QuestionBank
from kedaibiao.config.settings import Settings, load_settings, resolve_environ
from kedaibiao.core.events import EventBus
from kedaibiao.core.scheduler import Scheduler
from kedaibiao.net.http import HttpClient, UrllibTransport
from kedaibiao.notify.base import ConsoleNotifier, Notifier, NullNotifier, WebhookNotifier
from kedaibiao.platforms.base import AdapterContext, PlatformAdapter
from kedaibiao.tasks.answerer import BankAnswerer, ChainAnswerer, RuleAnswerer
from kedaibiao.tasks.llm import build_llm_answerer
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


def build_http_client(settings: Settings, *, cookie_file: str | Path | None = None) -> HttpClient:
    """装配协议型平台用的 HTTP 客户端，并把 cookie 落盘以复用登录态。

    ``cookie_file`` 指定 cookie 落盘位置。**多账号时必须按账号传不同的文件** ——
    否则切换账号就变成"共用同一个会话"，等于没切。
    省略时回退到旧的全局文件（``http_cookies.json``），保持单账号场景兼容。
    """
    transport = UrllibTransport()
    cookies = Path(cookie_file) if cookie_file else Path(settings.session_dir) / "http_cookies.json"
    restored = transport.load_cookies(cookies)
    if restored:
        log.info("已恢复 %d 条 cookie: %s", restored, cookies)
    client = HttpClient(transport)
    client.cookie_file = cookies  # 供落盘时使用
    return client


def build_context(
    settings: Settings,
    *,
    bus: EventBus | None = None,
    bank: QuestionBank | None = None,
    store: ProgressStore | None = None,
    notifier: Notifier | None = None,
    cookie_file: str | Path | None = None,
    environ: dict[str, str] | None = None,
) -> AdapterContext:
    return AdapterContext(
        bus=bus or EventBus(),
        http=build_http_client(settings, cookie_file=cookie_file),
        bank=bank,
        store=store,
        notifier=notifier,
        settings=settings,
        environ=dict(environ or {}),
    )



def session_path(cookie_file: str | Path) -> Path:
    """会话快照文件路径 —— 与 cookie 文件同目录、同保护级别。"""
    cookie = Path(cookie_file)
    return cookie.with_suffix(cookie.suffix + ".session.json")


def save_adapter_session(adapter: PlatformAdapter, cookie_file: str | Path) -> bool:
    """把适配器的会话导出来落盘（没有可导出的就返回 False）。

    有些平台的会话不只在 cookie 里（bearer token / 平台自己的登录态载荷），
    只存 cookie 的话进程重启就得重新登录 —— 而账号库刻意不存密码，
    于是这类平台会卡在「需要账号密码」上。
    """
    export = getattr(adapter, "export_session", None)
    if export is None:
        return False
    data = export()
    if not data:
        return False
    path = session_path(cookie_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return True


def restore_adapter_session(adapter: PlatformAdapter, cookie_file: str | Path) -> bool:
    """从磁盘恢复会话；文件不存在或恢复失败返回 False（调用方照常走登录）。"""
    path = session_path(cookie_file)
    if not path.is_file():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    import_session = getattr(adapter, "import_session", None)
    if import_session is None:
        return False
    try:
        return bool(import_session(data))
    except Exception:  # noqa: BLE001 - 恢复失败就当没有会话
        log.debug("恢复会话失败: %s", path, exc_info=True)
        return False


def persist_http_cookies(adapter: PlatformAdapter, settings: Settings) -> bool:
    """把协议型适配器的 cookie 落盘，返回是否真的写了。

    为什么重要：控制台的「课程 / 作业 / 工具」每次调用都会新建适配器。
    如果不把 cookie 存下来，每次点一下就是一次完整登录 —— 对平台不礼貌
    （可能触发风控），对用户也慢。存下来之后 ``refresh_session`` 会先探
    ``is_logged_in()``，会话还有效就直接跳过登录。
    """
    client = getattr(adapter.ctx, "http", None)
    transport = getattr(client, "transport", None)
    if not isinstance(transport, UrllibTransport):
        return False
    path = getattr(client, "cookie_file", None) or (
        Path(settings.session_dir) / "http_cookies.json"
    )
    transport.save_cookies(path)
    return True


@dataclass(slots=True)
class Runtime:
    """一次性装配好的运行环境。用完记得 ``close()``。"""

    settings: Settings
    bank: QuestionBank
    store: ProgressStore
    bus: EventBus
    pipeline: Pipeline
    notifier: Notifier
    #: 本次运行认的环境变量快照（``.env`` + 真实环境变量）。
    #: 凭据必须走这一份，不能各自去读 ``os.environ`` —— 否则 ``.env`` 里的
    #: 凭据到不了凭据层。
    environ: dict[str, str] = field(default_factory=dict)
    #: 适配器用的 cookie 文件。多账号场景下每个账号一个，切换账号 = 换会话。
    cookie_file: str | None = None
    #: 这次运行产生的数据归谁
    owner_id: int = 0

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
            cookie_file=self.cookie_file,
            environ=self.environ,
        )
        adapter = cls(ctx)
        # 支持「浏览器求解验证码」的适配器需要知道 cookie 落盘位置
        if hasattr(adapter, "_cookie_file"):
            adapter._cookie_file = str(self.cookie_file) if self.cookie_file else None
        return adapter

    async def run(self, platform: str, *, credentials: Any = None) -> PipelineReport:
        # 先确认平台存在，再要凭据 —— 否则没注册的平台会先报「缺凭据」，误导排查方向
        adapter = self.make_adapter(platform)
        if credentials is None:
            # 刻意不回退到 .env：凭据只有一个来源（控制台绑定的平台账号），
            # 调用方必须先把它解析好再传进来。
            raise RuntimeError(
                f"未提供 {platform} 的凭据。请在控制台「账号」页绑定该平台账号，"
                f"或调用方先解析出凭据再传入。"
            )
        creds = credentials
        if self.cookie_file:
            # 有落盘的会话就先恢复（账号库不存密码，没有会话就只能重新登录）
            restore_adapter_session(adapter, self.cookie_file)
        try:
            return await self.pipeline.run(adapter, creds)
        finally:
            await adapter.close()
            self._persist_cookies(adapter)

    def _persist_cookies(self, adapter: PlatformAdapter) -> None:
        persist_http_cookies(adapter, self.settings)

    def close(self) -> None:
        self.bank.close()
        self.store.close()


def build_runtime(
    *,
    root: str | Path = ".",
    settings: Settings | None = None,
    config: PipelineConfig | None = None,
    environ: dict[str, str] | None = None,
    cookie_file: str | None = None,
    owner_id: int = 0,
) -> Runtime:
    # 先算一次「本次认哪些环境变量」，配置与凭据共用同一份
    resolved_env = resolve_environ(root, environ=environ)
    settings = settings or load_settings(root=root, environ=resolved_env)

    bus = EventBus()
    bank = QuestionBank(settings.db_path)
    store = ProgressStore(settings.db_path)
    notifier = build_notifier(settings)

    # 答题链顺序：题库（最可靠）→ 大模型（会用外部 API，未配置则跳过）→ 规则兜底。
    # 规则放最后：它只会瞎猜，置信度刻意压低，用来避免「整份一题都不答」。
    chain: list[Any] = [BankAnswerer(bank)]
    llm = build_llm_answerer(resolved_env)
    if llm is not None:
        chain.append(llm)
        log.info("大模型答题器已启用: %s", llm.config.describe())
    chain.append(RuleAnswerer())
    answerer = ChainAnswerer(chain, min_confidence=settings.min_confidence)
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
        owner_id=owner_id,
    )
    return Runtime(
        settings=settings,
        bank=bank,
        store=store,
        bus=bus,
        pipeline=pipeline,
        notifier=notifier,
        environ=resolved_env,
        cookie_file=cookie_file,
        owner_id=owner_id,
    )
