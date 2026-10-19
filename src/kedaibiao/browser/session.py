"""Playwright 会话管理。

为什么要自己管会话：
- 登录态要落盘复用，否则每次跑批都要重新过人机验证；
- 多平台要隔离 context（cookie 绝不能串）；
- 无头模式撞上验证码时需要能「抬起」窗口交给人处理。

playwright 是可选依赖，本模块用惰性 import，没装也不影响核心导入。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Protocol

log = logging.getLogger(__name__)


class BrowserUnavailableError(RuntimeError):
    """未安装 playwright 或浏览器内核缺失。"""


class CaptchaHandler(Protocol):
    """人机验证处理钩子。返回 True 表示已放行，False 表示放弃本次任务。"""

    async def handle(self, page: Any, platform: str) -> bool: ...


class AbortOnCaptcha:
    """默认策略：直接放弃，记录现场截图供排查。"""

    def __init__(self, screenshot_dir: str | Path = "./data/captcha") -> None:
        self.screenshot_dir = Path(screenshot_dir)

    async def handle(self, page: Any, platform: str) -> bool:
        self.screenshot_dir.mkdir(parents=True, exist_ok=True)
        shot = self.screenshot_dir / f"{platform}-captcha.png"
        try:
            await page.screenshot(path=str(shot), full_page=True)
            log.error("遇到人机验证，已截图: %s", shot)
        except Exception:  # noqa: BLE001
            log.exception("截图失败")
        return False


class WaitForHuman:
    """有头模式下等待人工过验证码。仅当 ``headless=False`` 时有意义。"""

    def __init__(self, timeout: float = 180.0) -> None:
        self.timeout = timeout

    async def handle(self, page: Any, platform: str) -> bool:
        log.warning("[%s] 需要人工完成验证，请在 %.0f 秒内处理浏览器窗口", platform, self.timeout)
        try:
            await page.bring_to_front()
        except Exception:  # noqa: BLE001
            pass
        try:
            await page.wait_for_timeout(self.timeout * 1000)
        except Exception:  # noqa: BLE001
            return False
        return True


class BrowserSession:
    """一个浏览器实例 + 每个平台一个持久化 context。"""

    def __init__(
        self,
        *,
        headless: bool = True,
        session_dir: str | Path = "./data/sessions",
        locale: str = "zh-CN",
        user_agent: str | None = None,
        captcha_handler: CaptchaHandler | None = None,
    ) -> None:
        self.headless = headless
        self.session_dir = Path(session_dir)
        self.locale = locale
        self.user_agent = user_agent
        self.captcha_handler: CaptchaHandler = captcha_handler or (
            AbortOnCaptcha() if headless else WaitForHuman()
        )
        self._playwright: Any | None = None
        self._browser: Any | None = None
        self._contexts: dict[str, Any] = {}

    # -- 生命周期 ---------------------------------------------------------

    async def start(self) -> None:
        if self._browser is not None:
            return
        try:
            from playwright.async_api import async_playwright  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover - 取决于运行环境
            raise BrowserUnavailableError(
                "未安装 playwright，请执行: pip install 'kedaibiao[browser]' && playwright install chromium"
            ) from exc

        self.session_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self.headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        log.info("浏览器已启动 (headless=%s)", self.headless)

    async def close(self) -> None:
        for platform, ctx in list(self._contexts.items()):
            await self._save_state(platform, ctx)
            try:
                await ctx.close()
            except Exception:  # noqa: BLE001
                log.debug("关闭 context 失败: %s", platform, exc_info=True)
        self._contexts.clear()
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None

    async def __aenter__(self) -> BrowserSession:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    # -- context ----------------------------------------------------------

    def state_path(self, platform: str) -> Path:
        return self.session_dir / f"{platform}.storage_state.json"

    async def context(self, platform: str) -> Any:
        """取该平台的 context；已存在则复用（登录态天然共享）。"""
        if platform in self._contexts:
            return self._contexts[platform]
        if self._browser is None:
            await self.start()

        state = self.state_path(platform)
        kwargs: dict[str, Any] = {
            "locale": self.locale,
            "viewport": {"width": 1440, "height": 900},
            "storage_state": str(state) if state.is_file() else None,
        }
        if self.user_agent:
            kwargs["user_agent"] = self.user_agent

        ctx = await self._browser.new_context(**kwargs)  # type: ignore[union-attr]
        self._contexts[platform] = ctx
        log.info("已创建 context: %s (复用登录态=%s)", platform, state.is_file())
        return ctx

    async def page(self, platform: str) -> Any:
        ctx = await self.context(platform)
        return await ctx.new_page()

    async def save_state(self, platform: str) -> Path:
        ctx = self._contexts.get(platform)
        if ctx is None:
            raise KeyError(f"未创建 context: {platform}")
        return await self._save_state(platform, ctx)

    async def _save_state(self, platform: str, ctx: Any) -> Path:
        path = self.state_path(platform)
        path.parent.mkdir(parents=True, exist_ok=True)
        await ctx.storage_state(path=str(path))
        return path

    # -- 反爬辅助 ---------------------------------------------------------

    async def human_pause(self, low: float = 0.4, high: float = 1.2) -> None:
        """随机停顿，压低固定节奏带来的风控命中率。"""
        import random  # noqa: PLC0415

        await asyncio.sleep(random.uniform(low, high))
