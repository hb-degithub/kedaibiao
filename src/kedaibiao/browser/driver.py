"""页面操作原语：在 CDP ``Browser`` 之上做一层「人话」封装。

## 为什么要有这一层

``cdp.Browser`` 只提供 ``navigate`` / ``evaluate`` / ``click`` 这类**协议级**
动作。脚本里直接用它，每做一件小事都要手写一段 ``Runtime.evaluate``：

```python
browser.evaluate("Array.from(document.querySelectorAll('a[href]'), a => a.href)")
```

同一段 JS 在脚本之间复制粘贴，转义一错就是「页面没问题、结果就是空的」。
这一层把常用动作收敛成方法，JS 只在**这一处**拼装：

- 字符串一律走 :func:`js_str`（JSON 转义），不手写引号；
- 等待统一用轮询 + 超时，不靠 ``sleep`` 猜；
- 返回值统一成 Python 类型（``str`` / ``bool`` / ``list[Link]``）。

## 边界

**只做页面操作，不含任何平台业务。** 这里不知道「学习通」「超星」是什么，
不认任何站点的 DOM 结构，也不替调用方决定点哪里 —— 选择器由调用方给。
平台差异在 ``platforms/`` 里，本层只回答「页面上现在有什么」。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Protocol

log = logging.getLogger(__name__)


class DriverError(RuntimeError):
    """页面操作失败（元素不存在、等待超时等）。"""


class _Evaluator(Protocol):
    """驱动只需要浏览器这两件事：导航与求值。

    用 Protocol 而不是直接依赖 ``Browser``：单测可以塞一个假的求值器，
    完全离线跑，也不需要装 Chrome。
    """

    def navigate(self, url: str) -> None: ...

    def evaluate(self, expression: str, *, await_promise: bool = True) -> Any: ...


@dataclass(slots=True, frozen=True)
class Link:
    """页面上的一个链接。``text`` 已去首尾空白。"""

    text: str
    href: str


def js_str(value: str) -> str:
    """把一个 Python 字符串安全地嵌进 JS 表达式。

    用 ``json.dumps`` 而不是自己加引号：引号、反斜杠、换行、``</script>``
    这些都在 JSON 的转义规则里，而 JSON 字符串字面量恰好是合法的 JS
    字符串字面量。非 ASCII 字符会被转成 ``\\uXXXX``，JS 引擎照样认得。
    """
    return json.dumps(value, ensure_ascii=True)


class PageDriver:
    """在一个浏览器页面上做定位、等待、读取、点击。

    典型用法::

        with Browser(user_data_dir=profile) as browser:
            page = PageDriver(browser)
            page.goto("https://example.com/list")
            for link in page.links("a.item"):
                print(link.text, link.href)
    """

    def __init__(
        self,
        browser: _Evaluator,
        *,
        poll: float = 0.25,
        timeout: float = 15.0,
    ) -> None:
        self.browser = browser
        #: 轮询间隔（秒）。等 DOM 用，不要拿它当「加载完成」的保证。
        self.poll = poll
        #: 默认超时（秒）。
        self.timeout = timeout

    # -- 求值 -------------------------------------------------------------

    def eval(self, expression: str) -> Any:
        """跑一段 JS 并取回值（``returnByValue``）。"""
        return self.browser.evaluate(expression, await_promise=True)

    # -- 导航与等待 -------------------------------------------------------

    def goto(self, url: str, *, timeout: float | None = None) -> None:
        """导航到 ``url``，并等到 ``document.readyState == "complete"``。

        注意：``complete`` 只说明资源加载完，**不代表**前端框架把列表渲染好了。
        列表页请再跟一个 :meth:`wait_for` 盯住目标元素。
        """
        self.browser.navigate(url)
        self.wait_ready(timeout=timeout)

    def ready_state(self) -> str:
        value = self.eval("document.readyState")
        return str(value or "")

    def wait_ready(self, *, timeout: float | None = None) -> None:
        """等到 ``readyState`` 变成 ``complete``；超时抛 :class:`DriverError`。"""
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        while True:
            if self.ready_state() == "complete":
                return
            if time.monotonic() >= deadline:
                raise DriverError("等待页面加载完成超时（document.readyState 未到 complete）")
            time.sleep(self.poll)

    def _poll(self, probe, *, timeout: float | None, what: str) -> bool:
        """轮询 ``probe()`` 直到返回真值；超时返回 ``False``。

        求值失败（页面正在跳转、执行上下文被销毁）当作「还没好」继续等，
        只记 debug —— 这类错误在导航过程中是常态，不该打断等待。
        """
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        while True:
            try:
                if probe():
                    return True
            except Exception:  # noqa: BLE001 - 页面跳转中求值失败，继续等
                log.debug("等待 %s 时求值失败（页面可能在跳转）", what)
            if time.monotonic() >= deadline:
                return False
            time.sleep(self.poll)

    def wait_for(
        self,
        selector: str,
        *,
        timeout: float | None = None,
        visible: bool = False,
    ) -> bool:
        """轮询直到选择器命中（``visible=True`` 时还要求元素有实际尺寸且可见）。

        返回 ``True`` 表示命中；超时返回 ``False``（**不抛异常** —— 「等不到」
        往往是正常分支，例如「这个页面没有待办作业」）。
        """
        js = f"(() => {{ const el = document.querySelector({js_str(selector)});"
        if not visible:
            return self._poll(
                lambda: bool(self.eval(js + " return !!el; })()")),
                timeout=timeout,
                what=selector,
            )
        return self._poll(
            lambda: bool(self.eval(
                js
                + " if (!el) return false;"
                " const r = el.getBoundingClientRect();"
                " const cs = getComputedStyle(el);"
                " return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden';"
                " })()"
            )),
            timeout=timeout,
            what=selector,
        )

    def wait_for_function(
        self,
        expression: str,
        *,
        timeout: float | None = None,
    ) -> bool:
        """轮询一段返回布尔的 JS 表达式，直到它为真。

        用于「不是某个元素，而是一种状态」的等待，例如「课程卡片数量 > 0」
        或「加载遮罩已经消失」::

            page.wait_for_function("document.querySelectorAll('.card').length > 3")
        """
        return self._poll(lambda: bool(self.eval(expression)), timeout=timeout, what=expression)

    def wait_actionable(
        self,
        selector: str,
        *,
        timeout: float | None = None,
        require_enabled: bool = True,
    ) -> bool:
        """等到元素「可交互」再返回，而不是等它「存在」。

        照 Playwright 的可操作性检查做四件事（见
        https://playwright.dev/docs/actionability ）：

        - **attached**：元素在文档里（``isConnected``）；
        - **visible**：有非空包围盒，且不是 ``visibility:hidden``；
        - **stable**：包围盒跨两个 ``requestAnimationFrame`` 不变（挡掉入场动画）；
        - **enabled**：``disabled`` 属性为假（表单控件被禁用时点了也白点）。

        这四项在 SPA 页面上缺一不可：元素存在但正在淡入时点下去会点在旧位置，
        按钮禁用时点下去毫无反应 —— 两种都会表现为「脚本跑了但什么也没发生」。
        """
        js = (
            "(async () => {"
            f" const el = document.querySelector({js_str(selector)});"
            " if (!el || !el.isConnected) return false;"
            " const cs = getComputedStyle(el);"
            " const r = el.getBoundingClientRect();"
            " if (!(r.width > 0 && r.height > 0) || cs.visibility === 'hidden') return false;"
            " if (REQUIRE_ENABLED && el.disabled === true) return false;"
            " const a = el.getBoundingClientRect();"
            " await new Promise(res => requestAnimationFrame(() => requestAnimationFrame(res)));"
            " const b = el.getBoundingClientRect();"
            " return a.x === b.x && a.y === b.y && a.width === b.width && a.height === b.height;"
            " })()"
        ).replace("REQUIRE_ENABLED", "true" if require_enabled else "false")
        return self._poll(lambda: bool(self.eval(js)), timeout=timeout, what=selector)

    # -- 读取 -------------------------------------------------------------

    def title(self) -> str:
        return str(self.eval("document.title") or "")

    def url(self) -> str:
        return str(self.eval("location.href") or "")

    def html(self) -> str:
        return str(self.eval("document.documentElement.outerHTML") or "")

    def text(self, selector: str = "body") -> str:
        """取节点的 ``innerText``（已渲染、去多余空白）；节点不存在返回空串。"""
        value = self.eval(
            f"(() => {{ const el = document.querySelector({js_str(selector)});"
            " return el ? el.innerText : ''; })()"
        )
        return str(value or "").strip()

    def count(self, selector: str) -> int:
        """匹配该选择器的元素个数。"""
        value = self.eval(
            f"document.querySelectorAll({js_str(selector)}).length"
        )
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    def exists(self, selector: str) -> bool:
        return self.count(selector) > 0

    def attr(self, selector: str, name: str) -> str | None:
        """取属性；元素或属性不存在时返回 ``None``。"""
        value = self.eval(
            f"(() => {{ const el = document.querySelector({js_str(selector)});"
            f" return el ? el.getAttribute({js_str(name)}) : null; }})()"
        )
        return None if value is None else str(value)

    def links(self, selector: str = "a[href]") -> list[Link]:
        """把匹配的 ``<a>`` 收成 :class:`Link` 列表。

        文本取 ``textContent``（不是 ``innerText``）—— 列表里常有被 CSS 隐藏的
        角标，``innerText`` 会丢掉它们，但课程名一般不受影响；``textContent``
        对未渲染的卡片更稳。``href`` 取解析后的绝对地址（``a.href``）。
        """
        raw = self.eval(
            "Array.from(document.querySelectorAll(" + js_str(selector) + "),"
            " a => ({ text: (a.textContent || '').replace(/\\s+/g, ' ').trim(),"
            " href: a.href || a.getAttribute('href') || '' }))"
        )
        if not isinstance(raw, list):
            return []
        out: list[Link] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            href = str(item.get("href") or "").strip()
            if not href:
                continue
            out.append(Link(text=str(item.get("text") or "").strip(), href=href))
        return out

    # -- 交互 -------------------------------------------------------------

    def click(self, selector: str, *, timeout: float | None = None, wait: bool = True) -> None:
        """点击首个匹配元素。

        默认先 :meth:`wait_actionable` —— 元素存在不等于能点（可能还在淡入、
        可能被禁用）。``wait=False`` 用于「元素本来就已经就绪、不想再花一轮
        轮询」的场景。元素缺失或不可点则抛 :class:`DriverError`。
        """
        if wait and not self.wait_actionable(selector, timeout=timeout):
            raise DriverError(f"元素在超时内未变为可交互: {selector}")
        ok = self.eval(
            f"(() => {{ const el = document.querySelector({js_str(selector)});"
            " if (!el) return false; el.click(); return true; })()"
        )
        if not ok:
            raise DriverError(f"找不到要点击的元素: {selector}")

    def click_text(
        self,
        pattern: str,
        *,
        selector: str = "a,button",
        exact: bool = False,
    ) -> bool:
        """点击文本匹配的首个元素，返回是否点到。

        ``exact=False`` 用「包含」匹配（课程名后面常跟状态角标）。
        """
        matcher = (
            f"t === {js_str(pattern)}"
            if exact
            else f"t.includes({js_str(pattern)})"
        )
        ok = self.eval(
            "(() => {"
            f" const nodes = Array.from(document.querySelectorAll({js_str(selector)}));"
            " const hit = nodes.find(el => {"
            "   const t = (el.textContent || '').replace(/\\s+/g, ' ').trim();"
            f"   return {matcher};"
            " });"
            " if (!hit) return false; hit.click(); return true;"
            " })()"
        )
        return bool(ok)
