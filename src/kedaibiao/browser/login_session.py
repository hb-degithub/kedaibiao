"""交互式登录会话：先自动过验证码，过不去再把图交给人。

## 为什么需要它

很多平台的登录会弹验证码（图片码 / 短信码 / 滑块）。仓库的纪律是**不硬闯**，
但直接抛「请去浏览器登录」对用户也不好用 —— 尤其他可能根本不在服务器旁边。

这里做的是「一个后台浏览器 + 一个会话」：

1. 打开登录页、填账号密码、点登录；
2. 如果弹验证码：
   - **先自动识别**（图片码走 :mod:`captcha_ocr`，用已配置的多模态模型）；
   - 识别成功就填进去再提交，**最多重试 2 次**（错码会刷新验证码，不循环硬撞）；
   - 仍不成功 → 状态转 ``manual_captcha``，把验证码图片交给控制台，由用户输入；
3. 登录成功 → 状态 ``done``，调用方拿浏览器上下文去存登录态。

## 会话是线程 + 事件循环

Playwright 的异步对象绑定在创建它的事件循环上，不能每次请求 ``asyncio.run``。
所以每个会话一个后台线程（自带 loop），控制台通过线程安全的方法下发命令：
``submit_code`` / ``send_sms`` / ``snapshot``。

## 边界

- 只做**用户本人账号**的登录，不做批量、不做绕过风控；
- 自动识别失败**绝不猜码**：错码会抬高账号风控等级，宁可转人工；
- 会话有 TTL（默认 5 分钟），过期自动关浏览器，避免留下僵尸进程。
"""

from __future__ import annotations

import asyncio
import base64
import logging
import threading
import time
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

#: 会话闲置多久自动关闭（秒）
SESSION_TTL = 300.0

#: 自动识别最多重试几次。错码会让平台刷新验证码，撞多了会加重风控。
AUTO_RETRIES = 2


@dataclass(slots=True)
class LoginSpec:
    """一个平台的登录页描述（**只写已核实的 DOM**，没核实的不猜）。"""

    platform: str
    url: str
    username_selector: str
    password_selector: str
    submit_selector: str
    #: 验证码图片的候选选择器（按顺序试，取第一个有内容的）
    captcha_image_selectors: tuple[str, ...] = ()
    #: 验证码输入框的候选选择器
    captcha_input_selectors: tuple[str, ...] = ()
    #: 短信登录入口 / 获取验证码按钮（按文本点）
    sms_entry_text: tuple[str, ...] = ()
    sms_send_text: tuple[str, ...] = ()
    #: 登录成功的判据（页面正文包含任一且不含登录页标记）
    success_markers: tuple[str, ...] = ()
    #: 登录页特征（出现说明还没登进去）
    login_markers: tuple[str, ...] = ("用户登录", "登录", "密码")


#: 已核实的平台登录页。**只放实测过的选择器**；没有的平台先不进来。
SPECS: dict[str, LoginSpec] = {
    "chaoxing": LoginSpec(
        platform="chaoxing",
        url=(
            "https://passport2.chaoxing.com/login"
            "?fid=&newversion=true&refer=https%3A%2F%2Fi.chaoxing.com"
        ),
        username_selector="#phone",
        password_selector="#pwd",
        submit_selector="#loginBtn",
        # 学习通的图形码是页面上的 <img>，class/id 里带 captcha/vcode；
        # 这里给一组候选，按顺序找第一个能截到图的。
        captcha_image_selectors=(
            "#captcha img",
            "img#captchaImg",
            "img[src*='captcha']",
            "img[src*='vcode']",
            ".captcha img",
        ),
        captcha_input_selectors=(
            "#vcode",
            "input[name='vcode']",
            "input#captcha",
            "input[name='captcha']",
            "input[placeholder*='验证码']",
        ),
        sms_entry_text=("验证码登录", "短信登录"),
        sms_send_text=("获取验证码", "发送验证码"),
        success_markers=("课程", "收件箱", "小组", "云盘"),
    ),
}


class LoginSessionError(RuntimeError):
    def __init__(self, message: str, *, kind: str = "login_failed") -> None:
        super().__init__(message)
        self.kind = kind


@dataclass(slots=True)
class SessionState:
    """给控制台看的只读快照。"""

    session_id: str
    platform: str
    #: starting / auto_captcha / manual_captcha / manual_sms / done / failed / cancelled
    state: str = "starting"
    message: str = ""
    screenshot: bytes = b""
    attempts: int = 0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def public(self, *, include_image: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {
            "session_id": self.session_id,
            "platform": self.platform,
            "state": self.state,
            "message": self.message,
            "attempts": self.attempts,
            "expires_in": max(0, int(SESSION_TTL - (time.time() - self.updated_at))),
        }
        if include_image and self.screenshot:
            data["image_b64"] = base64.b64encode(self.screenshot).decode("ascii")
        return data


def _b64_png(data: bytes) -> str:
    return base64.b64encode(data or b"").decode("ascii")


class InteractiveLogin:
    """一次登录会话（在**一个**事件循环里跑，别跨循环调它）。"""

    def __init__(
        self,
        spec: LoginSpec,
        *,
        username: str,
        password: str,
        page: Any,
        environ: dict[str, str] | None = None,
        ocr: Any = None,
        settle_timeout: float = 8.0,
    ) -> None:
        self.spec = spec
        #: 提交后等「页面稳定/跳转」的上限。测试里调到很小，避免每个用例真等 8 秒。
        self.settle_timeout = settle_timeout
        self.username = username
        self.password = password
        self.page = page
        self.environ = environ or {}
        self._ocr = ocr
        self.state = SessionState(session_id="", platform=spec.platform)

    # -- 内部工具 ---------------------------------------------------------

    async def _first_present(self, selectors: tuple[str, ...]) -> str:
        for selector in selectors:
            try:
                if await self.page.count(selector):
                    return selector
            except Exception:  # noqa: BLE001 - 选择器不合法就试下一个
                continue
        return ""

    async def _logged_in(self) -> bool:
        try:
            body = await self.page.text("body")
        except Exception:  # noqa: BLE001
            return False
        markers = self.spec.success_markers
        if not markers:
            return False
        hit = any(marker in body for marker in markers)
        return hit and "用户登录" not in body

    async def _shot(self, selector: str = "") -> bytes:
        try:
            return await self.page.screenshot_bytes(selector)
        except Exception as exc:  # noqa: BLE001
            log.debug("截图失败（%s）：%s", selector or "整页", exc)
            return b""

    async def _ocr_code(self, image: bytes) -> str:
        if self._ocr is not None:
            try:
                return str(await self._ocr(image, self.environ) or "")
            except Exception as exc:  # noqa: BLE001
                log.debug("自定义 OCR 失败：%s", exc)
                return ""
        from kedaibiao.browser.captcha_ocr import solve_image_captcha

        return await solve_image_captcha(image, environ=self.environ)

    # -- 流程 -------------------------------------------------------------

    async def start(self) -> SessionState:
        """打开登录页、填账号密码、点登录；必要时自动过验证码。"""
        spec = self.spec
        try:
            await self.page.goto(spec.url)
            await self.page.wait_for(spec.username_selector, timeout=30000)
            await self.page.fill(spec.username_selector, self.username)
            await self.page.fill(spec.password_selector, self.password)
            await self.page.click(spec.submit_selector)
        except Exception as exc:  # noqa: BLE001
            return self._fail(f"打开登录页/提交表单失败：{exc}")

        await self._settle()
        if await self._logged_in():
            return self._done("账号密码登录成功（没有遇到验证码）")

        # 有验证码就自动试；识别不出来/码不对就转人工
        for attempt in range(AUTO_RETRIES + 1):
            image_selector = await self._first_present(spec.captcha_image_selectors)
            if not image_selector:
                break
            self.state.state = "auto_captcha"
            self.state.attempts = attempt
            image = await self._shot(image_selector)
            self.state.screenshot = image
            if not image:
                break
            code = await self._ocr_code(image)
            if not code:
                self.state.message = "自动识别验证码失败，已转人工输入"
                self.state.state = "manual_captcha"
                return self.state
            await self._fill_code(code, image_selector)
            self.state.attempts = attempt + 1
            if await self._logged_in():
                return self._done(f"自动识别验证码（{code}）后登录成功")
            self.state.message = f"自动识别的验证码 {code} 未被接受，重试中"

        # 走到这里：需要人工
        image_selector = await self._first_present(spec.captcha_image_selectors)
        self.state.screenshot = await self._shot(image_selector or "")
        self.state.state = "manual_captcha"
        self.state.message = self.state.message or (
            "登录需要验证码：自动识别没成功，请照着图输入"
        )
        return self.state

    async def _fill_code(self, code: str, image_selector: str = "") -> None:
        input_selector = await self._first_present(self.spec.captcha_input_selectors)
        if not input_selector:
            raise LoginSessionError("页面里找不到验证码输入框", kind="no_input")
        await self.page.fill(input_selector, code)
        await self.page.click(self.spec.submit_selector)
        await self._settle()

    async def submit_captcha(self, code: str) -> SessionState:
        """用户手动输入验证码后调用。"""
        code = str(code or "").strip()
        if not code:
            raise LoginSessionError("验证码不能为空", kind="bad_code")
        self.state.state = "auto_captcha"  # 临时态：正在提交
        self.state.message = f"已提交验证码 {code}"
        try:
            await self._fill_code(code)
        except LoginSessionError as exc:
            return self._fail(str(exc))
        except Exception as exc:  # noqa: BLE001
            return self._fail(f"提交验证码失败：{exc}")
        if await self._logged_in():
            return self._done("人工输入验证码后登录成功")
        # 码错了：刷新图片给用户再来一次
        image_selector = await self._first_present(self.spec.captcha_image_selectors)
        self.state.screenshot = await self._shot(image_selector or "")
        self.state.state = "manual_captcha"
        self.state.message = "验证码不对（平台刷新了图片），请重新输入"
        return self.state

    async def send_sms(self) -> SessionState:
        """点「验证码登录 / 获取验证码」，把页面状态交回控制台。"""
        try:
            for text in self.spec.sms_entry_text:
                if await self._click_text(text):
                    break
            for text in self.spec.sms_send_text:
                if await self._click_text(text):
                    break
            await self._settle()
        except Exception as exc:  # noqa: BLE001
            return self._fail(f"触发短信验证码失败：{exc}")
        self.state.state = "manual_sms"
        self.state.screenshot = await self._shot("")
        self.state.message = "已尝试发送短信验证码；收到后填入短信码并提交"
        return self.state

    async def submit_sms(self, code: str) -> SessionState:
        code = str(code or "").strip()
        if not code:
            raise LoginSessionError("短信验证码不能为空", kind="bad_code")
        try:
            input_selector = await self._first_present((
                "input[placeholder*='短信']",
                "input[name='email_code']",
                "#checkCode",
                *self.spec.captcha_input_selectors,
            ))
            if not input_selector:
                raise LoginSessionError("找不到短信验证码输入框", kind="no_input")
            await self.page.fill(input_selector, code)
            await self.page.click(self.spec.submit_selector)
            await self._settle()
        except LoginSessionError as exc:
            return self._fail(str(exc))
        except Exception as exc:  # noqa: BLE001
            return self._fail(f"提交短信验证码失败：{exc}")
        if await self._logged_in():
            return self._done("短信验证码登录成功")
        self.state.screenshot = await self._shot("")
        self.state.state = "manual_sms"
        self.state.message = "短信验证码没通过，请换一个或重新获取"
        return self.state

    async def _click_text(self, text: str) -> bool:
        """按可见文本点元素（找不到返回 False）。"""
        if not text:
            return False
        try:
            script = (
                "(() => { const t = " + _js_str(text) + "; const els = Array.from("
                "document.querySelectorAll('a,button,span,div')); "
                "const hit = els.find(e => (e.textContent||'').trim() === t); "
                "if (!hit) return false; hit.click(); return true; })()"
            )
            found = await self.page.eval(script)
            return bool(found)
        except Exception:  # noqa: BLE001
            return False

    async def _settle(self, *, timeout: float | None = None) -> None:
        """登录/提交后等页面稳定（跳转或渲染），不无限等。"""
        deadline = time.time() + (self.settle_timeout if timeout is None else timeout)
        while time.time() < deadline:
            try:
                if await self._logged_in():
                    return
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(max(0.01, self.settle_timeout / 20))

    def _done(self, message: str) -> SessionState:
        self.state.state = "done"
        self.state.message = message
        self.state.updated_at = time.time()
        self.state.screenshot = b""
        return self.state

    def _fail(self, message: str) -> SessionState:
        self.state.state = "failed"
        self.state.message = message
        self.state.updated_at = time.time()
        return self.state


def _js_str(text: str) -> str:
    from kedaibiao.browser.driver import js_str

    return js_str(text)


class _Holder:
    """一个会话的线程/循环/对象引用（只在 manager 内部用）。"""

    def __init__(self) -> None:
        self.thread: threading.Thread | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.queue: asyncio.Queue | None = None
        self.login: InteractiveLogin | None = None
        self.browser: Any = None
        self.state: SessionState | None = None
        self.cancelled = threading.Event()
        self.finished = threading.Event()
        self.error = ""


class LoginSessionManager:
    """管理若干并发登录会话（每个一个后台线程 + 浏览器）。"""

    def __init__(
        self,
        *,
        environ: dict[str, str] | None = None,
        browser_factory: Any = None,
        ocr: Any = None,
        ttl: float = SESSION_TTL,
        max_sessions: int = 2,
    ) -> None:
        self.environ = environ or {}
        self._browser_factory = browser_factory
        self._ocr = ocr
        self.ttl = ttl
        self.max_sessions = max_sessions
        self._lock = threading.RLock()
        self._holders: dict[str, _Holder] = {}
        self._seq = 0

    # -- 公开 API ---------------------------------------------------------

    def start(self, platform: str, *, username: str, password: str) -> dict[str, Any]:
        spec = SPECS.get(platform)
        if spec is None:
            raise LoginSessionError(
                f"{platform} 还没有人工验证码登录的页面描述（只支持：{'、'.join(sorted(SPECS))}）",
                kind="not_supported",
            )
        if not username or not password:
            raise LoginSessionError("账号和密码都要填", kind="bad_request")
        with self._lock:
            alive = [h for h in self._holders.values() if not h.finished.is_set()]
            if len(alive) >= self.max_sessions:
                raise LoginSessionError(
                    f"同时最多 {self.max_sessions} 个登录会话，先关掉一个再试",
                    kind="busy",
                )
            self._seq += 1
            sid = f"lg-{self._seq}"
            holder = _Holder()
            self._holders[sid] = holder
        holder.thread = threading.Thread(
            target=self._thread_main, args=(sid, spec, username, password),
            name=f"kedaibiao-login-{sid}", daemon=True,
        )
        holder.thread.start()
        return self._wait_state(sid, {"starting"})

    def snapshot(self, sid: str) -> dict[str, Any]:
        holder = self._require(sid)
        state = holder.state
        if state is None:
            return {"session_id": sid, "state": "starting", "message": ""}
        return state.public()

    def submit_code(self, sid: str, code: str) -> dict[str, Any]:
        holder = self._require(sid)
        holder.state.state = "submitting"  # type: ignore[union-attr]
        self._call(holder, lambda login: login.submit_captcha(code))
        return self.snapshot(sid)

    def send_sms(self, sid: str) -> dict[str, Any]:
        holder = self._require(sid)
        self._call(holder, lambda login: login.send_sms())
        return self.snapshot(sid)

    def submit_sms(self, sid: str, code: str) -> dict[str, Any]:
        holder = self._require(sid)
        self._call(holder, lambda login: login.submit_sms(code))
        return self.snapshot(sid)

    def save_state(self, sid: str, path: str) -> bool:
        """把登录态（storage_state）落盘；会话没成功返回 False。"""
        holder = self._require(sid)
        if not holder.state or holder.state.state != "done":
            return False
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._save(holder, path), holder.loop
            )
            return bool(future.result(timeout=30))
        except Exception as exc:  # noqa: BLE001
            log.warning("保存登录态失败：%s", exc)
            return False

    def close(self, sid: str) -> None:
        with self._lock:
            holder = self._holders.pop(sid, None)
        if holder is None:
            return
        holder.cancelled.set()
        self._call(holder, lambda login: login.close() if hasattr(login, "close") else None,
                   allow_finished=True)
        holder.finished.wait(timeout=10)

    def close_all(self) -> None:
        for sid in list(self._holders):
            self.close(sid)

    # -- 内部 -------------------------------------------------------------

    def _require(self, sid: str) -> _Holder:
        with self._lock:
            holder = self._holders.get(sid)
        if holder is None:
            raise LoginSessionError(f"没有这个登录会话：{sid}", kind="not_found")
        return holder

    def _wait_state(self, sid: str, states: set[str], timeout: float = 60.0) -> dict[str, Any]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            snap = self.snapshot(sid)
            if snap.get("state") not in states:
                return snap
            time.sleep(0.2)
        return self.snapshot(sid)

    def _call(self, holder: _Holder, fn: Any, *, allow_finished: bool = False) -> None:
        if holder.loop is None or holder.login is None:
            return
        if holder.finished.is_set() and not allow_finished:
            return
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._apply(holder, fn), holder.loop
            )
            future.result(timeout=120)
        except Exception as exc:  # noqa: BLE001 - 命令失败反映到 state
            log.warning("登录会话命令失败：%s", exc)
            if holder.state is not None:
                holder.state.state = "failed"
                holder.state.message = f"操作失败：{exc}"

    async def _apply(self, holder: _Holder, fn: Any) -> None:
        if holder.login is None:
            return
        result = await fn(holder.login)
        if isinstance(result, SessionState):
            holder.state = result

    async def _save(self, holder: _Holder, path: str) -> bool:
        from pathlib import Path

        context = getattr(getattr(holder.login, "page", None), "raw", None)
        context = getattr(context, "context", None)
        if context is None:
            return False
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        await context.storage_state(path=str(target))
        return True

    def _thread_main(self, sid: str, spec: LoginSpec, username: str, password: str) -> None:
        holder = self._holders[sid]
        loop = asyncio.new_event_loop()
        holder.loop = loop
        asyncio.set_event_loop(loop)
        holder.queue = asyncio.Queue()
        try:
            loop.run_until_complete(self._run_session(holder, spec, username, password))
        except Exception as exc:  # noqa: BLE001
            holder.error = f"{type(exc).__name__}: {exc}"
            if holder.state is not None:
                holder.state.state = "failed"
                holder.state.message = holder.error
        finally:
            holder.finished.set()
            with suppress(Exception):
                loop.close()

    async def _run_session(
        self, holder: _Holder, spec: LoginSpec, username: str, password: str
    ) -> None:
        browser = None
        try:
            if self._browser_factory is not None:
                browser = self._browser_factory()
                page = await browser.new_page()
            else:
                from kedaibiao.browser.pw import PwBrowser, PwConfig

                browser = PwBrowser(PwConfig(headless=True))
                await browser.start()
                page = await browser.new_page()
            holder.browser = browser
            login = InteractiveLogin(
                spec, username=username, password=password,
                page=page, environ=self.environ, ocr=self._ocr,
            )
            holder.login = login
            holder.state = await login.start()
            # 成功后保持浏览器存活一段时间，等控制台来取登录态 / 关会话
            deadline = time.time() + self.ttl
            while time.time() < deadline and not holder.cancelled.is_set():
                if holder.state.state in ("failed", "cancelled"):
                    break
                await asyncio.sleep(0.3)
        finally:
            if browser is not None:
                with suppress(Exception):
                    await browser.close()


__all__ = [
    "AUTO_RETRIES",
    "InteractiveLogin",
    "LoginSessionError",
    "LoginSessionManager",
    "LoginSpec",
    "SESSION_TTL",
    "SPECS",
    "SessionState",
]
