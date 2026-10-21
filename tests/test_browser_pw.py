"""Playwright 页面原语测试。

全部用假 Page/Frame 跑：不联网、不启动浏览器。这一层的价值在 JS 拼装、
frame 路由与返回转换的正确性，不在「Chromium 能不能启动」。

用 ``unittest.IsolatedAsyncioTestCase``（标准库）而不是 pytest-asyncio：
仓库的 dev 依赖里虽有它，但当前 venv 没装，测试不该依赖插件才能跑。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest import mock

from kedaibiao.browser import pw
from kedaibiao.browser.pw import PwBrowser, PwConfig, PwError, PwFrame, PwPage


class FakeElement:
    """假 iframe 元素，只为 ``get_attribute``。"""

    def __init__(self, attrs: dict[str, str]) -> None:
        self.attrs = attrs

    async def get_attribute(self, name: str) -> str | None:
        return self.attrs.get(name)


class FakeFrame:
    """假 Frame：记录求值表达式，按 handler 返回预设值。"""

    def __init__(
        self,
        *,
        url: str = "",
        name: str = "",
        handler: Callable[[str], Any] | None = None,
        attrs: dict[str, str] | None = None,
    ) -> None:
        self.url = url
        self.name = name
        self.handler = handler
        self._attrs = attrs
        self.expressions: list[str] = []

    async def evaluate(self, expression: str) -> Any:
        self.expressions.append(expression)
        if self.handler is None:
            return None
        return self.handler(expression)

    async def frame_element(self) -> FakeElement:
        if self._attrs is None:
            raise RuntimeError("主帧没有 iframe 元素")
        return FakeElement(self._attrs)


class FakePage:
    """假 Page：记录导航/求值/截图调用，按 handler 返回预设值。"""

    def __init__(
        self,
        handler: Callable[[str], Any] | None = None,
        *,
        url: str = "https://example.com/page",
        title: str = "假页面",
        frames: list[FakeFrame] | None = None,
    ) -> None:
        self.handler = handler
        self.url = url
        self._title = title
        self.frames = list(frames or [])
        self.expressions: list[str] = []
        self.goto_calls: list[dict[str, Any]] = []
        self.screenshot_calls: list[dict[str, Any]] = []
        self.title_calls = 0

    async def evaluate(self, expression: str) -> Any:
        self.expressions.append(expression)
        if self.handler is None:
            return None
        return self.handler(expression)

    async def goto(self, url: str, *, wait_until: str, timeout: int) -> None:
        self.goto_calls.append({"url": url, "wait_until": wait_until, "timeout": timeout})

    async def title(self) -> str:
        self.title_calls += 1
        return self._title

    async def screenshot(self, path: str, *, full_page: bool) -> None:
        self.screenshot_calls.append({"path": path, "full_page": full_page})


class FakeContext:
    """假 BrowserContext：记录登录态读写与页面创建。"""

    def __init__(
        self,
        page: FakePage | None = None,
        cookies: list[dict[str, Any]] | None = None,
    ) -> None:
        self.page = page or FakePage()
        self.cookies_value = cookies or []
        self.new_page_calls = 0
        self.storage_calls: list[str | None] = []
        self.closed = False

    async def new_page(self) -> FakePage:
        self.new_page_calls += 1
        return self.page

    async def storage_state(self, *, path: str | None = None) -> None:
        self.storage_calls.append(path)

    async def cookies(self) -> list[dict[str, Any]]:
        return self.cookies_value

    async def close(self) -> None:
        self.closed = True


class FakeBrowserRuntime:
    """假的 Playwright Browser，只实现 new_context / close。"""

    def __init__(self, context: FakeContext) -> None:
        self.context = context
        self.context_kwargs: dict[str, Any] | None = None
        self.closed = False

    async def new_context(self, **kwargs: Any) -> FakeContext:
        self.context_kwargs = kwargs
        return self.context

    async def close(self) -> None:
        self.closed = True


class FakeChromium:
    """假的 chromium 启动器，记录 launch 参数。"""

    def __init__(self, browser: FakeBrowserRuntime) -> None:
        self.browser = browser
        self.launch_kwargs: dict[str, Any] | None = None

    async def launch(self, **kwargs: Any) -> FakeBrowserRuntime:
        self.launch_kwargs = kwargs
        return self.browser


class FakePlaywright:
    """假的 Playwright 运行时，只实现 stop。"""

    def __init__(self, chromium: FakeChromium) -> None:
        self.chromium = chromium
        self.stopped = False

    async def stop(self) -> None:
        self.stopped = True


class FakeStarter:
    """假的 ``async_playwright()`` 返回值。"""

    def __init__(self, runtime: FakePlaywright) -> None:
        self.runtime = runtime
        self.start_count = 0
        self.started = False

    async def start(self) -> FakePlaywright:
        self.start_count += 1
        self.started = True
        return self.runtime


def make_page(
    handler: Callable[[str], Any] | None = None,
    **kwargs: Any,
) -> tuple[PwPage, FakePage]:
    """构造快轮询的 PwPage，超时用例不必真等 15 秒。"""
    fake = FakePage(handler, **kwargs)
    return PwPage(fake, poll=0.001), fake


class TestConfig(unittest.TestCase):
    """纯同步用例：配置、异常类型、惰性导入。"""

    def test_pw_error_is_runtime_error(self) -> None:
        self.assertTrue(issubclass(PwError, RuntimeError))

    def test_config_defaults(self) -> None:
        config = PwConfig()
        self.assertTrue(config.headless)
        self.assertEqual(config.locale, "zh-CN")
        self.assertIsNone(config.user_agent)
        self.assertEqual(config.viewport, (1440, 900))
        self.assertIsNone(config.storage_state)

    def test_config_uses_slots(self) -> None:
        self.assertFalse(hasattr(PwConfig(), "__dict__"))

    def test_config_accepts_path_and_dict_storage_state(self) -> None:
        self.assertEqual(
            PwConfig(storage_state={"cookies": []}).storage_state, {"cookies": []}
        )
        self.assertEqual(
            PwConfig(storage_state=Path("state.json")).storage_state, Path("state.json")
        )

    def test_missing_playwright_raises_pw_error(self) -> None:
        """模拟没装 playwright：导入不失败，真正用到时才抛 PwError 并给安装命令。"""
        names = ("playwright", "playwright.async_api")
        saved = {name: sys.modules.get(name) for name in names}
        for name in names:
            sys.modules.pop(name, None)
        sys.modules["playwright"] = None  # type: ignore[assignment]
        try:
            with self.assertRaises(PwError) as ctx:
                pw._load_async_playwright()
        finally:
            for name, module in saved.items():
                if module is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = module
        self.assertIn("pip install playwright && playwright install chromium", str(ctx.exception))


class PwAsyncTestCase(unittest.IsolatedAsyncioTestCase):
    """异步用例基类：提供临时目录与假 Playwright 注入。"""

    def setUp(self) -> None:
        super().setUp()
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def install_fake_playwright(
        self,
        *,
        page: FakePage | None = None,
        cookies: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """把惰性导入替换成假实现，返回所有可断言的假对象。"""
        context = FakeContext(page=page, cookies=cookies)
        browser = FakeBrowserRuntime(context)
        chromium = FakeChromium(browser)
        runtime = FakePlaywright(chromium)
        starter = FakeStarter(runtime)

        def factory() -> Callable[[], FakeStarter]:
            def make_starter() -> FakeStarter:
                return starter

            return make_starter

        patcher = mock.patch.object(pw, "_load_async_playwright", factory)
        patcher.start()
        self.addCleanup(patcher.stop)
        return {
            "context": context,
            "browser": browser,
            "chromium": chromium,
            "playwright": runtime,
            "starter": starter,
        }


class TestPageBasics(PwAsyncTestCase):
    """导航、求值、读取与截图。"""

    async def test_eval_runs_on_main_frame(self) -> None:
        page, fake = make_page(lambda expr: 42)
        self.assertEqual(await page.eval("1 + 1"), 42)
        self.assertEqual(fake.expressions, ["1 + 1"])

    async def test_eval_can_target_frame(self) -> None:
        child = FakeFrame(url="https://x/child", handler=lambda expr: "inner")
        page, fake = make_page(frames=[child])
        frame = await page.frame("child")
        self.assertIsNotNone(frame)

        self.assertEqual(await page.eval("document.title", frame=frame), "inner")
        self.assertEqual(child.expressions, ["document.title"])
        self.assertEqual(fake.expressions, [])

    async def test_eval_rejects_non_pwframe(self) -> None:
        page, _ = make_page()
        with self.assertRaises(PwError):
            await page.eval("1", frame=object())  # type: ignore[arg-type]

    async def test_goto_forwards_defaults(self) -> None:
        page, fake = make_page()
        await page.goto("https://mooc1.chaoxing.com/course")
        self.assertEqual(
            fake.goto_calls,
            [
                {
                    "url": "https://mooc1.chaoxing.com/course",
                    "wait_until": "domcontentloaded",
                    "timeout": 45000,
                }
            ],
        )

    async def test_goto_forwards_custom_wait_and_timeout(self) -> None:
        page, fake = make_page()
        await page.goto("https://x/y", wait="load", timeout=1234)
        self.assertEqual(fake.goto_calls[0]["wait_until"], "load")
        self.assertEqual(fake.goto_calls[0]["timeout"], 1234)

    async def test_goto_wraps_playwright_errors(self) -> None:
        page, fake = make_page()

        async def boom(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("net::ERR_TIMED_OUT")

        with (
            mock.patch.object(fake, "goto", boom),
            self.assertRaises(PwError) as ctx,
        ):
            await page.goto("https://x/y")
        self.assertIn("打开页面失败", str(ctx.exception))

    async def test_url_and_title(self) -> None:
        page, fake = make_page(url="https://x/y", title="课程")
        self.assertEqual(page.url, "https://x/y")
        self.assertEqual(await page.title(), "课程")
        self.assertEqual(fake.title_calls, 1)

    async def test_html_reads_document_element(self) -> None:
        page, fake = make_page(lambda expr: "<html>ok</html>")
        self.assertEqual(await page.html(), "<html>ok</html>")
        self.assertIn("outerHTML", fake.expressions[0])

    async def test_text_strips_and_uses_query_selector(self) -> None:
        page, fake = make_page(lambda expr: "  作业一  ")
        self.assertEqual(await page.text(".title"), "作业一")
        expression = fake.expressions[0]
        self.assertIn("document.querySelector(", expression)
        self.assertIn("innerText", expression)

    async def test_text_missing_returns_empty(self) -> None:
        page, _ = make_page(lambda expr: None)
        self.assertEqual(await page.text(".absent"), "")

    async def test_selector_is_json_escaped(self) -> None:
        page, fake = make_page(lambda expr: "")
        selector = 'a[title="课程"]'
        await page.text(selector)
        self.assertIn(json.dumps(selector), fake.expressions[0])

    async def test_count_returns_int(self) -> None:
        page, fake = make_page(lambda expr: 3)
        self.assertEqual(await page.count(".item"), 3)
        self.assertIn("document.querySelectorAll(", fake.expressions[0])

    async def test_count_tolerates_bad_payload(self) -> None:
        page, _ = make_page(lambda expr: "not-a-number")
        self.assertEqual(await page.count(".item"), 0)

    async def test_attr_returns_string_or_none(self) -> None:
        page, fake = make_page(lambda expr: "card-1")
        self.assertEqual(await page.attr("li.task", "cardid"), "card-1")
        self.assertIn(json.dumps("cardid"), fake.expressions[0])

        missing, _ = make_page(lambda expr: None)
        self.assertIsNone(await missing.attr("li.task", "cardid"))

    async def test_screenshot_creates_parent_and_forwards(self) -> None:
        page, fake = make_page()
        target = self.tmp / "shots" / "page.png"

        result = await page.screenshot(target)

        self.assertEqual(result, str(target))
        self.assertTrue(target.parent.is_dir())
        self.assertEqual(fake.screenshot_calls, [{"path": str(target), "full_page": True}])

    async def test_screenshot_can_disable_full_page(self) -> None:
        page, fake = make_page()
        await page.screenshot(self.tmp / "page.png", full_page=False)
        self.assertFalse(fake.screenshot_calls[0]["full_page"])


class TestWaiting(PwAsyncTestCase):
    """等待语义与超时。"""

    async def test_wait_for_defaults_to_visible(self) -> None:
        page, fake = make_page(lambda expr: True)
        await page.wait_for(".card")
        expression = fake.expressions[0]
        self.assertIn("getBoundingClientRect", expression)
        self.assertIn("visibility", expression)

    async def test_wait_for_attached_only_checks_existence(self) -> None:
        page, fake = make_page(lambda expr: True)
        await page.wait_for(".card", state="attached")
        expression = fake.expressions[0]
        self.assertIn("!!document.querySelector", expression)
        self.assertNotIn("getBoundingClientRect", expression)

    async def test_wait_for_hidden_checks_inverse_visibility(self) -> None:
        page, fake = make_page(lambda expr: True)
        await page.wait_for(".mask", state="hidden")
        self.assertIn("return !shown", fake.expressions[0])

    async def test_wait_for_detached_checks_absence(self) -> None:
        page, fake = make_page(lambda expr: True)
        await page.wait_for(".mask", state="detached")
        self.assertIn("!document.querySelector", fake.expressions[0])

    async def test_wait_for_retries_until_true(self) -> None:
        values = iter([False, False, True])
        page, fake = make_page(lambda expr: next(values))
        await page.wait_for(".card", timeout=100)
        self.assertEqual(len(fake.expressions), 3)

    async def test_wait_for_timeout_raises(self) -> None:
        page, _ = make_page(lambda expr: False)
        with self.assertRaises(PwError) as ctx:
            await page.wait_for(".card", timeout=10)
        self.assertIn("等待元素超时", str(ctx.exception))

    async def test_wait_for_rejects_unknown_state(self) -> None:
        page, fake = make_page(lambda expr: True)
        with self.assertRaises(PwError):
            await page.wait_for(".card", state="ready")
        self.assertEqual(fake.expressions, [])

    async def test_wait_for_runs_inside_frame(self) -> None:
        child = FakeFrame(url="https://x/frame", handler=lambda expr: True)
        page, fake = make_page(frames=[child])
        frame = await page.frame("x/frame")
        self.assertIsNotNone(frame)

        await page.wait_for(".card", frame=frame)

        self.assertEqual(fake.expressions, [])
        self.assertEqual(len(child.expressions), 1)

    async def test_wait_function_polls_until_truthy(self) -> None:
        values = iter([False, None, "ok"])
        page, fake = make_page(lambda expr: next(values))
        await page.wait_function("window.__ready === true", timeout=100)
        self.assertEqual(len(fake.expressions), 3)
        self.assertTrue(all(expr == "window.__ready === true" for expr in fake.expressions))

    async def test_wait_function_timeout_raises(self) -> None:
        page, _ = make_page(lambda expr: False)
        with self.assertRaises(PwError) as ctx:
            await page.wait_function("false", timeout=10)
        self.assertIn("等待 JS 条件超时", str(ctx.exception))


class TestInteraction(PwAsyncTestCase):
    """点击与填写。"""

    async def test_click_scrolls_then_clicks(self) -> None:
        page, fake = make_page(lambda expr: True)
        await page.click(".task", timeout=100)

        self.assertEqual(len(fake.expressions), 2)
        self.assertIn("!!document.querySelector", fake.expressions[0])
        click_js = fake.expressions[1]
        self.assertIn("scrollIntoView", click_js)
        self.assertIn("el.click()", click_js)
        self.assertLess(click_js.index("scrollIntoView"), click_js.index("el.click()"))

    async def test_click_missing_element_raises(self) -> None:
        page, _ = make_page(lambda expr: False)
        with self.assertRaises(PwError) as ctx:
            await page.click(".absent", timeout=10)
        self.assertIn("等待元素超时", str(ctx.exception))

    async def test_click_element_vanishes_between_wait_and_click(self) -> None:
        def handler(expression: str) -> Any:
            return "scrollIntoView" not in expression

        page, _ = make_page(handler)
        with self.assertRaises(PwError) as ctx:
            await page.click(".gone", timeout=100)
        self.assertIn("找不到要点击的元素", str(ctx.exception))

    async def test_fill_sets_value_and_dispatches_events(self) -> None:
        page, fake = make_page(lambda expr: True)
        value = 'a"b\n课'

        await page.fill(".input", value, timeout=100)

        self.assertEqual(len(fake.expressions), 2)
        fill_js = fake.expressions[1]
        self.assertIn("el.focus()", fill_js)
        self.assertIn(json.dumps(value), fill_js)
        self.assertIn("dispatchEvent", fill_js)
        self.assertIn("'input'", fill_js)
        self.assertIn("'change'", fill_js)

    async def test_fill_missing_element_raises(self) -> None:
        page, _ = make_page(lambda expr: False)
        with self.assertRaises(PwError) as ctx:
            await page.fill(".absent", "x", timeout=10)
        self.assertIn("等待元素超时", str(ctx.exception))


class TestFrames(PwAsyncTestCase):
    """frame 枚举与匹配。"""

    async def test_frames_wraps_all_frames(self) -> None:
        page, _ = make_page(
            frames=[
                FakeFrame(url="https://x/main"),
                FakeFrame(url="https://x/child"),
                FakeFrame(url="https://x/nested"),
            ]
        )

        wrapped = await page.frames()

        self.assertEqual(
            [item.url for item in wrapped],
            ["https://x/main", "https://x/child", "https://x/nested"],
        )
        self.assertTrue(all(isinstance(item, PwFrame) for item in wrapped))

    async def test_frame_matches_url_substring_case_insensitive(self) -> None:
        page, _ = make_page(
            frames=[
                FakeFrame(url="https://mooc1.chaoxing.com/main"),
                FakeFrame(url="https://mooc1.chaoxing.com/knowledge/cards?x=1"),
            ]
        )

        found = await page.frame("KNOWLEDGE/CARDS")

        self.assertIsNotNone(found)
        self.assertTrue(found.url.endswith("?x=1"))

    async def test_frame_matches_name_when_url_misses(self) -> None:
        page, _ = make_page(frames=[FakeFrame(url="https://x/plain", name="courseFrame")])

        found = await page.frame("courseFrame")

        self.assertIsNotNone(found)
        self.assertEqual(found.url, "https://x/plain")

    async def test_frame_matches_iframe_id(self) -> None:
        framed = FakeFrame(url="https://x/plain", attrs={"id": "frame-7"})
        page, _ = make_page(frames=[FakeFrame(url="https://x/main"), framed])

        found = await page.frame("frame-7")

        self.assertIsNotNone(found)
        self.assertIs(found.raw, framed)

    async def test_frame_prefers_url_match_over_name(self) -> None:
        page, _ = make_page(
            frames=[
                FakeFrame(url="https://x/one", name="target"),
                FakeFrame(url="https://x/target/page"),
            ]
        )

        found = await page.frame("target")

        self.assertIsNotNone(found)
        self.assertEqual(found.url, "https://x/target/page")

    async def test_frame_returns_none_when_no_match(self) -> None:
        page, _ = make_page(frames=[FakeFrame(url="https://x/a")])
        self.assertIsNone(await page.frame("missing"))
        self.assertIsNone(await page.frame(""))

    async def test_pwframe_operations_run_in_frame(self) -> None:
        def handler(expression: str) -> Any:
            if "querySelectorAll" in expression:
                return 2
            if "getAttribute" in expression:
                return "v"
            if "innerText" in expression:
                return "  frame-text  "
            return True

        child = FakeFrame(url="https://x/child", handler=handler)
        frame = PwFrame(child, poll=0.001)

        self.assertEqual(frame.url, "https://x/child")
        self.assertIs(await frame.eval("1 + 1"), True)
        self.assertEqual(await frame.text(".t"), "frame-text")
        self.assertEqual(await frame.count(".i"), 2)
        self.assertEqual(await frame.attr(".i", "data-x"), "v")

        await frame.wait_for(".i", state="attached", timeout=100)
        await frame.click(".i", timeout=100)
        await frame.fill(".i", "值", timeout=100)

        # 帧内动作只落在 frame 上，不会漏到 page。
        self.assertGreaterEqual(len(child.expressions), 7)
        self.assertTrue(any("scrollIntoView" in item for item in child.expressions))


class TestBrowser(PwAsyncTestCase):
    """浏览器生命周期、context 参数与登录态。"""

    async def test_new_page_autostarts_and_wraps_page(self) -> None:
        page_obj = FakePage()
        bundle = self.install_fake_playwright(page=page_obj)
        browser = PwBrowser()
        try:
            page = await browser.new_page()

            self.assertIsInstance(page, PwPage)
            self.assertIs(page.raw, page_obj)
            self.assertTrue(bundle["starter"].started)
            self.assertEqual(bundle["context"].new_page_calls, 1)
        finally:
            await browser.close()

    async def test_start_builds_context_from_config(self) -> None:
        bundle = self.install_fake_playwright()
        state = self.tmp / "state.json"
        config = PwConfig(
            headless=False,
            locale="en-US",
            user_agent="UA/1.0",
            viewport=(800, 600),
            storage_state=state,
        )
        browser = PwBrowser(config)

        await browser.start()
        try:
            self.assertEqual(
                bundle["chromium"].launch_kwargs,
                {
                    "headless": False,
                    "args": ["--disable-blink-features=AutomationControlled"],
                },
            )
            self.assertEqual(
                bundle["browser"].context_kwargs,
                {
                    "locale": "en-US",
                    "viewport": {"width": 800, "height": 600},
                    "user_agent": "UA/1.0",
                    "storage_state": str(state),
                },
            )
        finally:
            await browser.close()

    async def test_default_config_omits_optional_context_args(self) -> None:
        bundle = self.install_fake_playwright()
        browser = PwBrowser()

        await browser.start()
        try:
            kwargs = bundle["browser"].context_kwargs
            self.assertIsNotNone(kwargs)
            self.assertNotIn("user_agent", kwargs)
            self.assertNotIn("storage_state", kwargs)
            self.assertEqual(kwargs["locale"], "zh-CN")
            self.assertEqual(kwargs["viewport"], {"width": 1440, "height": 900})
            self.assertTrue(bundle["chromium"].launch_kwargs["headless"])
        finally:
            await browser.close()

    async def test_storage_state_dict_is_passed_through(self) -> None:
        bundle = self.install_fake_playwright()
        state: dict[str, Any] = {"cookies": [{"name": "a", "value": "1"}]}
        browser = PwBrowser(PwConfig(storage_state=state))

        await browser.start()
        try:
            self.assertEqual(bundle["browser"].context_kwargs["storage_state"], state)
        finally:
            await browser.close()

    async def test_start_is_idempotent(self) -> None:
        bundle = self.install_fake_playwright()
        browser = PwBrowser()

        await browser.start()
        await browser.start()
        try:
            self.assertEqual(bundle["starter"].start_count, 1)
        finally:
            await browser.close()

    async def test_context_manager_closes_everything(self) -> None:
        bundle = self.install_fake_playwright()

        with self.assertRaises(RuntimeError):
            async with PwBrowser():
                raise RuntimeError("boom")

        self.assertTrue(bundle["context"].closed)
        self.assertTrue(bundle["browser"].closed)
        self.assertTrue(bundle["playwright"].stopped)

    async def test_close_is_idempotent(self) -> None:
        self.install_fake_playwright()
        browser = PwBrowser()
        await browser.start()

        await browser.close()
        await browser.close()  # 重复关闭不应抛异常

    async def test_launch_failure_is_wrapped(self) -> None:
        bundle = self.install_fake_playwright()

        async def boom(**kwargs: Any) -> Any:
            raise RuntimeError("Executable doesn't exist")

        with mock.patch.object(bundle["chromium"], "launch", boom):
            browser = PwBrowser()
            with self.assertRaises(PwError) as ctx:
                await browser.start()

        self.assertIn("playwright install chromium", str(ctx.exception))
        # 启动失败必须把已经起来的 playwright 收掉，避免泄漏进程。
        self.assertIsNone(browser._playwright)
        self.assertIsNone(browser._browser)

    async def test_save_state_creates_parent_and_forwards(self) -> None:
        bundle = self.install_fake_playwright()
        browser = PwBrowser()
        await browser.start()
        try:
            target = self.tmp / "profiles" / "chaoxing.json"

            saved = await browser.save_state(target)

            self.assertEqual(saved, target)
            self.assertTrue(target.parent.is_dir())
            self.assertEqual(bundle["context"].storage_calls, [str(target)])
        finally:
            await browser.close()

    async def test_save_state_wraps_errors(self) -> None:
        bundle = self.install_fake_playwright()
        browser = PwBrowser()
        await browser.start()
        try:

            async def boom(*, path: str | None = None) -> None:
                raise RuntimeError("disk full")

            with (
                mock.patch.object(bundle["context"], "storage_state", boom),
                self.assertRaises(PwError) as ctx,
            ):
                await browser.save_state(self.tmp / "state.json")
            self.assertIn("保存登录态失败", str(ctx.exception))
        finally:
            await browser.close()

    async def test_cookies_returns_json_serializable_dicts(self) -> None:
        cookies = [{"name": "a", "value": "1", "domain": ".chaoxing.com"}]
        self.install_fake_playwright(cookies=cookies)
        browser = PwBrowser()
        await browser.start()
        try:
            result = await browser.cookies()

            self.assertEqual(result, cookies)
            self.assertTrue(all(isinstance(item, dict) for item in result))
            json.dumps(result)  # 契约要求：返回值必须 JSON 可序列化
        finally:
            await browser.close()

    async def test_state_methods_require_start(self) -> None:
        browser = PwBrowser()

        with self.assertRaises(PwError):
            await browser.save_state("state.json")
        with self.assertRaises(PwError):
            await browser.cookies()
