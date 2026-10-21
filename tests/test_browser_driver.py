"""页面操作原语测试。

全部用假的求值器跑，不启浏览器、不联网 —— 这一层的价值在**拼接**与
**等待**的正确性，不在「Chrome 能不能开起来」。
"""

from __future__ import annotations

import json
import unittest
from typing import Any

from kedaibiao.browser.driver import DriverError, Link, PageDriver, js_str


class FakeBrowser:
    """只实现 ``navigate`` / ``evaluate`` 的假浏览器。"""

    def __init__(self, handler=None) -> None:
        self.navigated: list[str] = []
        self.expressions: list[str] = []
        self.handler = handler

    def navigate(self, url: str) -> None:
        self.navigated.append(url)

    def evaluate(self, expression: str, *, await_promise: bool = True) -> Any:
        self.expressions.append(expression)
        assert await_promise is True
        if self.handler is not None:
            return self.handler(expression)
        return None


class TestJsStr(unittest.TestCase):
    def test_is_valid_js_string_literal(self) -> None:
        """拼出来的必须是合法 JSON/JS 字符串字面量，且能原样还原。"""
        for value in ['a"b', "a'b", "a\\b", "第一行\n第二行", "</script>", "课程 A"]:
            literal = js_str(value)
            self.assertEqual(json.loads(literal), value)

    def test_escapes_quote_and_newline(self) -> None:
        self.assertEqual(js_str('x"y'), '"x\\"y"')
        self.assertEqual(js_str("x\ny"), '"x\\ny"')

    def test_non_ascii_is_escaped(self) -> None:
        self.assertEqual(js_str("课"), '"\\u8bfe"')


class TestNavigation(unittest.TestCase):
    def test_goto_navigates_and_waits(self) -> None:
        browser = FakeBrowser(lambda expr: "complete" if "readyState" in expr else None)
        page = PageDriver(browser, poll=0.001, timeout=1)

        page.goto("https://example.com/a")

        self.assertEqual(browser.navigated, ["https://example.com/a"])
        self.assertTrue(any("readyState" in e for e in browser.expressions))

    def test_wait_ready_times_out(self) -> None:
        browser = FakeBrowser(lambda expr: "loading")
        page = PageDriver(browser, poll=0.001, timeout=0.02)

        with self.assertRaises(DriverError):
            page.wait_ready()

    def test_wait_ready_returns_when_complete(self) -> None:
        states = iter(["loading", "interactive", "complete"])
        browser = FakeBrowser(lambda expr: next(states))
        page = PageDriver(browser, poll=0.001, timeout=1)

        page.wait_ready()  # 不抛异常即通过


class TestWaiting(unittest.TestCase):
    def test_wait_for_returns_true_when_found(self) -> None:
        browser = FakeBrowser(lambda expr: True)
        page = PageDriver(browser, poll=0.001, timeout=1)

        self.assertTrue(page.wait_for(".course-card"))

    def test_wait_for_returns_false_on_timeout(self) -> None:
        browser = FakeBrowser(lambda expr: False)
        page = PageDriver(browser, poll=0.001, timeout=0.02)

        self.assertFalse(page.wait_for(".nope"))

    def test_wait_for_survives_eval_errors(self) -> None:
        """页面跳转中求值会抛错，等待应继续而不是直接炸。"""
        def handler(expr: str) -> Any:
            raise RuntimeError("Execution context was destroyed")

        page = PageDriver(FakeBrowser(handler), poll=0.001, timeout=0.02)
        self.assertFalse(page.wait_for(".x"))

    def test_visible_wait_checks_geometry(self) -> None:
        browser = FakeBrowser(lambda expr: True)
        page = PageDriver(browser, poll=0.001, timeout=1)

        page.wait_for(".x", visible=True)

        expr = browser.expressions[0]
        self.assertIn("getBoundingClientRect", expr)
        self.assertIn("visibility !== 'hidden'", expr)

    def test_plain_wait_only_checks_attachment(self) -> None:
        browser = FakeBrowser(lambda expr: True)
        page = PageDriver(browser, poll=0.001, timeout=1)

        page.wait_for(".x")

        self.assertNotIn("getBoundingClientRect", browser.expressions[0])

    def test_wait_for_function(self) -> None:
        browser = FakeBrowser(lambda expr: True)
        page = PageDriver(browser, poll=0.001, timeout=1)

        self.assertTrue(page.wait_for_function("document.querySelectorAll('.c').length > 3"))
        self.assertIn("querySelectorAll('.c').length > 3", browser.expressions[0])

    def test_wait_for_function_times_out(self) -> None:
        page = PageDriver(FakeBrowser(lambda expr: False), poll=0.001, timeout=0.02)
        self.assertFalse(page.wait_for_function("false"))


class TestActionability(unittest.TestCase):
    def test_checks_all_four_properties(self) -> None:
        browser = FakeBrowser(lambda expr: True)
        page = PageDriver(browser, poll=0.001, timeout=1)

        self.assertTrue(page.wait_actionable(".btn"))

        expr = browser.expressions[0]
        self.assertIn("isConnected", expr)
        self.assertIn("getBoundingClientRect", expr)
        self.assertIn("requestAnimationFrame", expr)
        self.assertIn("el.disabled === true", expr)

    def test_require_enabled_can_be_disabled(self) -> None:
        browser = FakeBrowser(lambda expr: True)
        page = PageDriver(browser, poll=0.001, timeout=1)

        page.wait_actionable(".btn", require_enabled=False)

        self.assertIn("false && el.disabled === true", browser.expressions[0])

    def test_timeout_returns_false(self) -> None:
        page = PageDriver(FakeBrowser(lambda expr: False), poll=0.001, timeout=0.02)
        self.assertFalse(page.wait_actionable(".btn"))


class TestReading(unittest.TestCase):
    def test_links_normalizes_and_filters(self) -> None:
        browser = FakeBrowser(lambda expr: [
            {"text": "  课程 A  ", "href": "https://x/a"},
            {"text": "空链接", "href": "   "},
            {"text": "", "href": "https://x/b"},
            "not-a-dict",
        ])
        page = PageDriver(browser)

        links = page.links("a.item")

        self.assertEqual(links, [
            Link(text="课程 A", href="https://x/a"),
            Link(text="", href="https://x/b"),
        ])

    def test_links_returns_empty_on_bad_payload(self) -> None:
        page = PageDriver(FakeBrowser(lambda expr: None))
        self.assertEqual(page.links(), [])

    def test_text_missing_returns_empty(self) -> None:
        page = PageDriver(FakeBrowser(lambda expr: None))
        self.assertEqual(page.text(".absent"), "")

    def test_text_strips(self) -> None:
        page = PageDriver(FakeBrowser(lambda expr: "  作业一  "))
        self.assertEqual(page.text(".title"), "作业一")

    def test_count_tolerates_non_int(self) -> None:
        page = PageDriver(FakeBrowser(lambda expr: "not a number"))
        self.assertEqual(page.count(".x"), 0)

    def test_attr_missing_returns_none(self) -> None:
        page = PageDriver(FakeBrowser(lambda expr: None))
        self.assertIsNone(page.attr(".x", "href"))

    def test_attr_returns_string(self) -> None:
        page = PageDriver(FakeBrowser(lambda expr: "/detail/1"))
        self.assertEqual(page.attr(".x", "href"), "/detail/1")

    def test_selector_is_escaped_into_expression(self) -> None:
        browser = FakeBrowser(lambda expr: True)
        page = PageDriver(browser, poll=0.001, timeout=1)

        page.exists('a[href="x"]')

        expr = browser.expressions[0]
        self.assertIn('a[href=\\"x\\"]', expr)


class TestInteraction(unittest.TestCase):
    def test_click_success(self) -> None:
        page = PageDriver(FakeBrowser(lambda expr: True), poll=0.001, timeout=1)
        self.assertIsNone(page.click(".btn"))

    def test_click_missing_raises(self) -> None:
        page = PageDriver(FakeBrowser(lambda expr: False), poll=0.001, timeout=0.02)
        with self.assertRaises(DriverError):
            page.click(".btn")

    def test_click_waits_for_actionability_by_default(self) -> None:
        browser = FakeBrowser(lambda expr: True)
        page = PageDriver(browser, poll=0.001, timeout=1)

        page.click(".btn")

        # 第一次求值是可操作性轮询，第二次才是点击
        self.assertIn("requestAnimationFrame", browser.expressions[0])
        self.assertIn("el.click()", browser.expressions[1])

    def test_click_can_skip_wait(self) -> None:
        browser = FakeBrowser(lambda expr: True)
        page = PageDriver(browser, poll=0.001, timeout=1)

        page.click(".btn", wait=False)

        self.assertEqual(len(browser.expressions), 1)
        self.assertIn("el.click()", browser.expressions[0])

    def test_click_text_returns_bool(self) -> None:
        page = PageDriver(FakeBrowser(lambda expr: True))
        self.assertTrue(page.click_text("作业一"))

    def test_click_text_exact_vs_contains(self) -> None:
        browser = FakeBrowser(lambda expr: True)
        page = PageDriver(browser)

        page.click_text("作业一", exact=True)
        page.click_text("作业一", exact=False)

        exact_expr, contains_expr = browser.expressions
        # 常量走 js_str 转义（非 ASCII 变成 \uXXXX），断言的是拼接方式而不是字面量
        self.assertIn("t === " + js_str("作业一"), exact_expr)
        self.assertIn("t.includes(" + js_str("作业一") + ")", contains_expr)

    def test_eval_passthrough(self) -> None:
        page = PageDriver(FakeBrowser(lambda expr: 42))
        self.assertEqual(page.eval("1+1"), 42)


if __name__ == "__main__":
    unittest.main()
