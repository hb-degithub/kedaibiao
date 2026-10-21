"""Playwright 异步页面原语：给平台脚本一层「帧感知」的页面操作。

## 为什么还要这一层

仓库里已有两套浏览器底座：

- :mod:`kedaibiao.browser.cdp`：手写 CDP，同步、只面向主帧；
- :mod:`kedaibiao.browser.driver`：CDP 之上的同步页面动作。

学习通页面有个绕不开的特点：**关键内容大量在 iframe 里**（章节树、课件播放器
常常是 iframe 套 iframe），而 CDP 版 ``PageDriver`` 只在主帧求值。所以这一层
改用 Playwright 的异步 API：

- :class:`PwBrowser` 管进程、context 与登录态；
- :class:`PwPage` 管主页面，:class:`PwFrame` 管 iframe，**两者方法同名同签名**，
  调用方不用关心「这段 JS 该扔给 page 还是 frame」；
- 定位与读取一律走 ``document.querySelector(All)``，选择器只在这一处拼装
  （:func:`kedaibiao.browser.driver.js_str` 负责转义），返回值限定为 JSON
  可序列化的基本类型，不把 Playwright 的句柄漏给调用方；
- 等待是「轮询 + 超时」，不是 ``sleep`` 猜。

## 边界

这里**只做页面动作，不含任何平台业务**：它不知道「学习通」是什么，不认任何
站点的 DOM。平台差异在 ``platforms/chaoxing/``。

## playwright 是可选依赖

模块顶层不 import playwright（核心包要保持零依赖也能导入），真正用到时才由
:func:`_load_async_playwright` 惰性导入；没装则抛 :class:`PwError` 并给出安装命令。

## 测试

单测全部用假 Page/Frame，完全离线：这一层的价值在 JS 拼装、frame 路由与返回
转换的正确性，不在「Chromium 能不能启动」。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .driver import js_str

log = logging.getLogger(__name__)

#: 启动参数：去掉 ``navigator.webdriver`` 这类自动化指纹（与 session.py 保持一致）。
_BROWSER_ARGS = ["--disable-blink-features=AutomationControlled"]

#: 等待元素时的合法状态，语义对齐 Playwright 的 ``wait_for_selector(state=...)``。
_WAIT_STATES = frozenset({"attached", "detached", "visible", "hidden"})

#: 轮询间隔（秒）。测试会把它调小，生产环境不必太密。
_DEFAULT_POLL = 0.1


class PwError(RuntimeError):
    """Playwright 页面层的统一错误。

    覆盖「没装 playwright」「浏览器起不来」「等待超时」「元素不存在」等情况，
    调用方只需 ``except PwError``，不用同时惦记 Playwright 自己的异常类型。
    """


def _load_async_playwright() -> Any:
    """惰性导入 ``async_playwright``；没装时给出可直接复制的安装命令。

    放在函数里而不是模块顶层：本项目的核心依赖是零第三方包，导入
    ``kedaibiao.browser.pw`` 不应该因为环境里没有 playwright 就失败。
    """
    try:
        from playwright.async_api import async_playwright  # noqa: PLC0415
    except ImportError as exc:
        raise PwError(
            "未安装 playwright，浏览器功能不可用。请执行: "
            "pip install playwright && playwright install chromium"
        ) from exc
    return async_playwright


@dataclass(slots=True)
class PwConfig:
    """启动浏览器的一次性配置。

    默认值按「学习通页面自动化」取：中文 locale、1440x900 桌面视口、无头模式。
    ``storage_state`` 可以是登录态文件路径（``str`` / :class:`Path`），也可以是
    Playwright 的 storage_state 字典，便于调用方自己承担持久化。
    """

    headless: bool = True
    locale: str = "zh-CN"
    user_agent: str | None = None
    viewport: tuple[int, int] = (1440, 900)
    storage_state: str | Path | dict[str, Any] | None = None


class _FrameOps:
    """页面与 iframe 共用的操作实现（内部类，不建议外部依赖）。

    要点只有三个：

    1. 所有选择器都经 :func:`js_str` 转义后嵌进 ``querySelector``，
       不把用户给的字符串直接拼进 JS；
    2. 返回 ``querySelector`` 这类轻量值，不返回 DOM 句柄 —— Playwright 的
       ``evaluate`` 只能带回可序列化结果，句柄既传不回来也没有意义；
    3. 点击前 ``scrollIntoView``：学习通的列表很长，元素在视口外时
       ``element.click()`` 在部分页面会点不中或点到遮挡层。
    """

    def __init__(self, raw: Any, *, poll: float = _DEFAULT_POLL) -> None:
        #: 原生 Playwright Page 或 Frame；只作为求值目标，不向外暴露用法。
        self.raw = raw
        #: 轮询间隔（秒）。
        self.poll = poll

    # -- 基础 -------------------------------------------------------------

    @property
    def url(self) -> str:
        """当前页面/帧的 URL；拿不到时返回空串。"""
        return str(getattr(self.raw, "url", "") or "")

    async def eval(self, js: str) -> Any:
        """求值一段 JS，返回 JSON 可序列化的结果。"""
        try:
            return await self.raw.evaluate(js)
        except PwError:
            raise
        except Exception as exc:  # noqa: BLE001 - 统一收敛成 PwError
            raise PwError(f"JS 求值失败: {exc}") from exc

    async def html(self) -> str:
        value = await self.eval(
            "document.documentElement ? document.documentElement.outerHTML : ''"
        )
        return str(value or "")

    # -- 读取 -------------------------------------------------------------

    async def text(self, selector: str = "body") -> str:
        """取节点 ``innerText``（退化为 ``textContent``），去首尾空白。

        ``innerText`` 反映渲染后的换行与空白，比 ``textContent`` 更接近人看到
        的文本；节点不存在返回空串而不是抛错 —— 「这个 tab 下没内容」是常态。
        """
        value = await self.eval(
            "(() => {"
            f" const el = document.querySelector({js_str(selector)});"
            " return el ? (el.innerText ?? el.textContent ?? '') : '';"
            " })()"
        )
        return str(value or "").strip()

    async def count(self, selector: str) -> int:
        """匹配该选择器的元素个数；返回值异常时按 0 处理。"""
        value = await self.eval(f"document.querySelectorAll({js_str(selector)}).length")
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    async def attr(self, selector: str, name: str) -> str | None:
        """取首个匹配元素的属性；元素或属性不存在时返回 ``None``。"""
        value = await self.eval(
            "(() => {"
            f" const el = document.querySelector({js_str(selector)});"
            f" return el ? el.getAttribute({js_str(name)}) : null;"
            " })()"
        )
        return None if value is None else str(value)

    # -- 等待 -------------------------------------------------------------

    async def _poll(self, probe: Any, *, timeout: int, what: str) -> bool:
        """反复执行 ``probe()`` 直到返回真值；超时返回 ``False``。

        页面跳转时执行上下文会被销毁，求值抛错是常态，不能因此中断等待，
        只记 debug。``timeout`` 单位是**毫秒**，与 Playwright 的入参一致。
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(timeout, 0) / 1000
        while True:
            try:
                if await probe():
                    return True
            except Exception:  # noqa: BLE001 - 页面跳转中求值失败，继续等
                log.debug("等待 %s 时求值失败（页面可能在跳转）", what)
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(self.poll)

    def _wait_js(self, selector: str, state: str) -> str:
        """按状态生成等待用的布尔 JS。"""
        sel = js_str(selector)
        if state == "attached":
            return f"(() => {{ return !!document.querySelector({sel}); }})()"
        if state == "detached":
            return f"(() => {{ return !document.querySelector({sel}); }})()"
        check = (
            " const r = el.getBoundingClientRect();"
            " const cs = getComputedStyle(el);"
            " const shown = r.width > 0 && r.height > 0 && cs.visibility !== 'hidden';"
        )
        if state == "visible":
            return (
                "(() => {"
                f" const el = document.querySelector({sel});"
                " if (!el) return false;"
                + check
                + " return shown;"
                " })()"
            )
        # hidden：不存在也算「隐藏」，这样「等遮罩消失」才能立即通过。
        return (
            "(() => {"
            f" const el = document.querySelector({sel});"
            " if (!el) return true;"
            + check
            + " return !shown;"
            " })()"
        )

    async def screenshot_bytes(self, selector: str = "", *, full_page: bool = False) -> bytes:
        """截图为 PNG 字节：给了选择器就只截该元素，否则截整页/整帧。

        学习通的题面走 OCR 时用这个：把「题目 + 选项」那一块截下来交给视觉模型，
        完全绕开字体混淆（伪汉字）和公式图片。
        """
        try:
            if selector:
                locator = self.raw.locator(selector).first
                await locator.scroll_into_view_if_needed(timeout=5000)
                return await locator.screenshot(type="png")
            if full_page:
                return await self.raw.screenshot(type="png", full_page=True)
            return await self.raw.screenshot(type="png")
        except Exception as exc:  # noqa: BLE001
            raise PwError(f"截图失败（{selector or '整页'}）: {exc}") from exc

    async def wait_for(
        self,
        selector: str,
        *,
        timeout: int = 15000,
        state: str = "visible",
    ) -> None:
        """等到选择器满足 ``state``；超时抛 :class:`PwError`。

        ``state`` 取 ``attached`` / ``detached`` / ``visible`` / ``hidden``。
        默认等 ``visible`` 而不是 ``attached``：学习通很多节点先挂在 DOM 上、
        尺寸还是 0，此时点下去等于没点。
        """
        if state not in _WAIT_STATES:
            raise PwError(f"未知的等待状态 {state!r}，可选: {sorted(_WAIT_STATES)}")
        js = self._wait_js(selector, state)
        if not await self._poll(lambda: self.eval(js), timeout=timeout, what=selector):
            raise PwError(f"等待元素超时（{state}，{timeout}ms）: {selector}")

    async def wait_function(self, js: str, *, timeout: int = 15000) -> None:
        """轮询一段返回布尔的 JS 表达式，直到它为真；超时抛 :class:`PwError`。

        用于「不是某个元素，而是一种状态」的等待，例如「加载遮罩已消失」或
        「答题卡已渲染出 N 个格子」。
        """
        if not await self._poll(lambda: self.eval(js), timeout=timeout, what=js):
            raise PwError(f"等待 JS 条件超时（{timeout}ms）: {js[:120]}")

    # -- 交互 -------------------------------------------------------------

    async def click(self, selector: str, *, timeout: int = 15000) -> None:
        """点击首个匹配元素：先滚动到视口中央，再 ``element.click()``。

        这里故意不用 Playwright 的 ``locator.click()``：一来帧内元素要额外传
        ``frame``，二来学习通有些透明遮罩会让可操作性检查一直失败，而真实用户
        点得到、JS 也点得到。需要严格可操作性时由调用方先 ``wait_for``。
        """
        await self.wait_for(selector, timeout=timeout, state="attached")
        ok = await self.eval(
            "(() => {"
            f" const el = document.querySelector({js_str(selector)});"
            " if (!el) return false;"
            " el.scrollIntoView({ block: 'center', inline: 'center' });"
            " el.click();"
            " return true;"
            " })()"
        )
        if not ok:
            raise PwError(f"找不到要点击的元素: {selector}")

    async def fill(self, selector: str, value: str, *, timeout: int = 15000) -> None:
        """给输入框赋值，并补发 ``input`` / ``change`` 事件。

        学习通前端用的是 Vue/jQuery 监听：只改 ``el.value`` 不会触发它们的
        ``v-model`` 或事件回调，必须手动派发事件，否则看起来「填进去了」、
        提交时却是空值。``contenteditable`` 元素没有 ``value``，退化为写文本。
        """
        await self.wait_for(selector, timeout=timeout, state="attached")
        ok = await self.eval(
            "(() => {"
            f" const el = document.querySelector({js_str(selector)});"
            " if (!el) return false;"
            " el.focus();"
            f" const value = {js_str(value)};"
            " if ('value' in el) { el.value = value; } else { el.textContent = value; }"
            " el.dispatchEvent(new Event('input', { bubbles: true }));"
            " el.dispatchEvent(new Event('change', { bubbles: true }));"
            " return true;"
            " })()"
        )
        if not ok:
            raise PwError(f"找不到要填写的元素: {selector}")


class PwFrame(_FrameOps):
    """一个 iframe 的句柄，方法签名与主页面完全一致。

    不直接复用 :class:`PwPage` 实例：``PwPage`` 还有 ``goto`` / ``screenshot``
    这些只对主页面有意义的动作，frame 上暴露它们只会让人误用。
    """


class PwPage:
    """一个 Playwright 页面的人话封装，支持把动作定向到指定 iframe。

    典型用法::

        async with PwBrowser(PwConfig(headless=False)) as browser:
            page = await browser.new_page()
            await page.goto("https://mooc1.chaoxing.com/...")
            frame = await page.frame("knowledge/cards")
            if frame is not None:
                await frame.click("li[cardid]")
    """

    def __init__(self, page: Any, *, poll: float = _DEFAULT_POLL) -> None:
        self._page = page
        # 主页面自身也用 _FrameOps 处理，这样 page 与 frame 的 JS 只有一份实现。
        self._ops = _FrameOps(page, poll=poll)

    @property
    def raw(self) -> Any:
        """原生 Playwright ``Page``。仅用于排障，正常业务请走本类方法。"""
        return self._page

    @property
    def url(self) -> str:
        """当前 URL（不等加载完成）。"""
        return self._ops.url

    def _frame_ops(self, frame: PwFrame | None) -> _FrameOps:
        """把 ``frame=`` 参数解析成求值目标；``None`` 表示主帧。"""
        if frame is None:
            return self._ops
        if isinstance(frame, PwFrame):
            return frame
        raise PwError("frame 参数必须是 PwFrame（用 page.frame() / page.frames() 取得）")

    # -- 导航 -------------------------------------------------------------

    async def goto(self, url: str, *, wait: str = "domcontentloaded", timeout: int = 45000) -> None:
        """打开 URL 并等待到 ``wait`` 状态（默认 DOM 就绪就返回）。

        默认不等到 ``load``：学习通页面里广告/统计脚本很慢，等 ``load`` 常常
        白等十几秒；DOM 就绪后紧跟一个 ``wait_for`` 盯目标元素更可靠。
        """
        try:
            await self._page.goto(url, wait_until=wait, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - 统一收敛成 PwError
            raise PwError(f"打开页面失败（{url}）: {exc}") from exc

    async def title(self) -> str:
        """页面标题；取不到返回空串。"""
        try:
            value = await self._page.title()
        except Exception as exc:  # noqa: BLE001
            raise PwError(f"读取页面标题失败: {exc}") from exc
        return str(value or "")

    # -- 求值与读取 -------------------------------------------------------

    async def eval(self, js: str, *, frame: PwFrame | None = None) -> Any:
        """求值 JS；``frame=None`` 主帧，否则在指定 iframe 内执行。"""
        return await self._frame_ops(frame).eval(js)

    async def text(self, selector: str = "body", *, frame: PwFrame | None = None) -> str:
        """取文本并去首尾空白；元素不存在返回空串。"""
        return await self._frame_ops(frame).text(selector)

    async def html(self, *, frame: PwFrame | None = None) -> str:
        """取 ``documentElement.outerHTML``。"""
        return await self._frame_ops(frame).html()

    async def count(self, selector: str, *, frame: PwFrame | None = None) -> int:
        """匹配元素个数。"""
        return await self._frame_ops(frame).count(selector)

    async def attr(self, selector: str, name: str, *, frame: PwFrame | None = None) -> str | None:
        """取属性值；元素或属性不存在返回 ``None``。"""
        return await self._frame_ops(frame).attr(selector, name)

    # -- 等待 -------------------------------------------------------------

    async def wait_for(
        self,
        selector: str,
        *,
        timeout: int = 15000,
        state: str = "visible",
        frame: PwFrame | None = None,
    ) -> None:
        """等到元素满足 ``state``；超时抛 :class:`PwError`。"""
        await self._frame_ops(frame).wait_for(selector, timeout=timeout, state=state)

    async def wait_function(self, js: str, *, timeout: int = 15000) -> None:
        """轮询 JS 表达式直到为真（在主帧执行）；超时抛 :class:`PwError`。"""
        await self._ops.wait_function(js, timeout=timeout)

    # -- 交互 -------------------------------------------------------------

    async def click(
        self,
        selector: str,
        *,
        timeout: int = 15000,
        frame: PwFrame | None = None,
    ) -> None:
        """点击首个匹配元素（先 ``scrollIntoView``）。"""
        await self._frame_ops(frame).click(selector, timeout=timeout)

    async def fill(
        self,
        selector: str,
        value: str,
        *,
        timeout: int = 15000,
        frame: PwFrame | None = None,
    ) -> None:
        """给输入框/可编辑元素赋值并派发 ``input`` / ``change`` 事件。"""
        await self._frame_ops(frame).fill(selector, value, timeout=timeout)

    async def screenshot(self, path: str | Path, *, full_page: bool = True) -> str:
        """截图到 ``path``；父目录不存在时自动创建，返回实际写入路径。"""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            await self._page.screenshot(path=str(target), full_page=full_page)
        except Exception as exc:  # noqa: BLE001
            raise PwError(f"截图失败（{target}）: {exc}") from exc
        return str(target)

    # -- 帧 ---------------------------------------------------------------

    async def click_text(
        self,
        text: str,
        *,
        timeout: int = 15000,
        exact: bool = False,
        tag: str = "*",
    ) -> None:
        """按可见文本点击元素。

        学习通的导航/弹窗按钮经常只有文本没有稳定 id（「章节」「考试」「进入考试」），
        按文本点比维护一堆选择器稳。``exact=False`` 用包含匹配，优先最短命中
        （避免「考试」点到「考试须知」）。
        """
        deadline = time.time() + timeout / 1000.0
        while time.time() < deadline:
            hit = await self.eval(
                "(() => {"
                " const want = " + js_str(text) + ";"
                " const exact = " + ("true" if exact else "false") + ";"
                " const tag = " + js_str(tag) + ";"
                " const nodes = Array.from(document.querySelectorAll(tag));"
                " const hits = nodes.filter(el => {"
                "   const value = (el.innerText || '').trim();"
                "   if (!value) return false;"
                "   if (exact ? value !== want : value.indexOf(want) < 0) return false;"
                "   return (el.offsetWidth || 0) > 0 && (el.offsetHeight || 0) > 0;"
                " });"
                " if (!hits.length) return false;"
                " hits.sort((a, b) => (a.innerText || '').length - (b.innerText || '').length);"
                " hits[0].click();"
                " return true;"
                "})()"
            )
            if hit:
                return
            await asyncio.sleep(0.25)
        raise PwError(f"找不到可点击的文本：{text!r}")

    async def fetch_text(self, url: str, *, headers: dict[str, str] | None = None,
                        timeout: int = 30000) -> str:
        """用当前 context 的登录态发一个 GET，返回响应文本。

        为什么需要它：学习通的「章节 → 任务点」元数据（``knowledge/cards`` 里的
        ``mArg``）是服务端渲染在 HTML 里的，逐个小节打开页面要 1~2 秒；用同一份
        cookie 直接 GET 能把课程枚举从分钟级压到秒级。请求走 Playwright 的
        ``context.request``，与页面共享 cookie，不额外处理登录态。
        """
        context = getattr(self._page, "context", None)
        if context is None:
            raise PwError("页面没有 context，无法发请求")
        try:
            response = await context.request.get(url, headers=headers or {}, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - 网络错误统一收敛
            raise PwError(f"GET 失败（{url[:120]}）: {exc}") from exc
        if not response.ok:
            raise PwError(f"GET {url[:120]} 返回 {response.status}")
        return await response.text()

    async def frames(self) -> list[PwFrame]:
        """所有 frame（含主帧与嵌套 frame，Playwright 已摊平）。"""
        raw_frames = getattr(self._page, "frames", None) or []
        return [PwFrame(raw, poll=self._ops.poll) for raw in raw_frames]

    async def frame(self, match: str) -> PwFrame | None:
        """按 URL 子串找 frame，找不到再按 name / id 精确匹配。

        优先 URL 子串：学习通同一页面里 ``iframe`` 的 name 经常重复甚至是空串，
        而 ``knowledge/cards``、``ans-video`` 这类 URL 片段稳定得多。匹配不区分
        大小写；都没命中返回 ``None``，由调用方决定是报错还是换路径。
        """
        if not match:
            return None
        needle = str(match).lower()
        frames = await self.frames()
        for candidate in frames:
            if needle in candidate.url.lower():
                return candidate
        for candidate in frames:
            name = str(getattr(candidate.raw, "name", "") or "")
            if name and name.lower() == needle:
                return candidate
        for candidate in frames:
            element_id = await _frame_element_id(candidate.raw)
            if element_id is not None and element_id == match:
                return candidate
        return None


async def _frame_element_id(frame: Any) -> str | None:
    """取 iframe 元素上的 ``id`` 属性；主帧或拿不到时返回 ``None``。

    ``frame.frame_element()`` 对主帧会抛错、对已卸载的 frame 也可能失败，
    这里一律吞掉 —— 它只是 ``frame()`` 匹配的兜底手段。
    """
    get_element = getattr(frame, "frame_element", None)
    if get_element is None:
        return None
    with contextlib.suppress(Exception):
        element = await get_element()
        value = await element.get_attribute("id")
        if value:
            return str(value)
    return None


class PwBrowser:
    """一个 Chromium 实例 + 一个共享 context，负责生命周期与登录态。

    一个 context 就够：刷课/刷题通常同账号串行跑，隔离 context 反而会让
    cookie 不共享、每页都要重新登录。需要多账号时起多个 :class:`PwBrowser`。
    """

    def __init__(self, config: PwConfig | None = None) -> None:
        self.config = config or PwConfig()
        self._playwright: Any | None = None
        self._browser: Any | None = None
        self._context: Any | None = None

    # -- 生命周期 ---------------------------------------------------------

    async def start(self) -> None:
        """启动浏览器并建好 context；重复调用是幂等的。"""
        if self._context is not None:
            return
        async_playwright = _load_async_playwright()
        self._playwright = await async_playwright().start()
        try:
            self._browser = await self._playwright.chromium.launch(
                headless=self.config.headless,
                args=list(_BROWSER_ARGS),
            )
            self._context = await self._browser.new_context(**self._context_kwargs())
        except Exception as exc:  # noqa: BLE001 - 启动失败统一收敛
            await self.close()
            raise PwError(
                f"浏览器启动失败: {exc}。若提示缺少浏览器内核，请执行: "
                "playwright install chromium"
            ) from exc
        log.info("浏览器已启动 (headless=%s)", self.config.headless)

    async def close(self) -> None:
        """关闭 context / 浏览器 / playwright；可重复调用，失败只记日志。"""
        context, self._context = self._context, None
        if context is not None:
            try:
                await context.close()
            except Exception:  # noqa: BLE001 - 关闭阶段不掩盖主流程异常
                log.debug("关闭 context 失败", exc_info=True)
        browser, self._browser = self._browser, None
        if browser is not None:
            try:
                await browser.close()
            except Exception:  # noqa: BLE001
                log.debug("关闭浏览器失败", exc_info=True)
        playwright, self._playwright = self._playwright, None
        if playwright is not None:
            try:
                await playwright.stop()
            except Exception:  # noqa: BLE001
                log.debug("停止 playwright 失败", exc_info=True)

    async def __aenter__(self) -> PwBrowser:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    # -- context / 登录态 -------------------------------------------------

    def _context_kwargs(self) -> dict[str, Any]:
        """把 :class:`PwConfig` 翻译成 ``new_context`` 的参数。"""
        cfg = self.config
        kwargs: dict[str, Any] = {
            "locale": cfg.locale,
            "viewport": {"width": cfg.viewport[0], "height": cfg.viewport[1]},
        }
        if cfg.user_agent is not None:
            kwargs["user_agent"] = cfg.user_agent
        if cfg.storage_state is not None:
            state = cfg.storage_state
            kwargs["storage_state"] = str(state) if isinstance(state, Path) else state
        return kwargs

    async def new_page(self) -> PwPage:
        """新建页面；浏览器未启动时先自动 :meth:`start`。"""
        if self._context is None:
            await self.start()
        context = self._context
        if context is None:  # start() 成功时不可能走到这里，只为类型收窄
            raise PwError("浏览器尚未启动")
        try:
            raw = await context.new_page()
        except Exception as exc:  # noqa: BLE001
            raise PwError(f"新建页面失败: {exc}") from exc
        return PwPage(raw)

    async def save_state(self, path: str | Path) -> Path:
        """把当前 context 的 cookies/localStorage 落盘，供下次 ``storage_state`` 复用。"""
        if self._context is None:
            raise PwError("浏览器尚未启动，无法保存登录态")
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            await self._context.storage_state(path=str(target))
        except Exception as exc:  # noqa: BLE001
            raise PwError(f"保存登录态失败（{target}）: {exc}") from exc
        return target

    async def cookies(self) -> list[dict[str, Any]]:
        """当前 context 的 cookies，已转成纯 ``dict``（JSON 可序列化）。"""
        if self._context is None:
            raise PwError("浏览器尚未启动，无法读取 cookies")
        try:
            raw = await self._context.cookies()
        except Exception as exc:  # noqa: BLE001
            raise PwError(f"读取 cookies 失败: {exc}") from exc
        return [dict(item) for item in raw or [] if isinstance(item, dict)]


__all__ = ["PwBrowser", "PwConfig", "PwError", "PwFrame", "PwPage"]
