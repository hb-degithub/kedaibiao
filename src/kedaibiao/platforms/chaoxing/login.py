"""学习通网页登录（手机号 / 超星号 + 密码）。

## 为什么要自己登

控制台里的学习通账号以前只能贴 Cookie：登录要过图形验证码/滑块，框架不代过。
但**账号密码登录的整条链路在浏览器里是可以走的** —— 密码校验本身没有验证码，
只有风控命中时才会弹出图形验证码或滑块。所以这里的策略是：

1. 优先复用落盘的 ``storage_state``（一次登录，长期有效，直到会话过期）；
2. 没有/失效时才用账号密码登录；
3. 真弹出验证码时**不硬闯**，抛 :class:`ChaoxingLoginError` 并带上截图路径，
   交给人工在控制台里处理 —— 与仓库其它平台「不代过验证码」的纪律一致。

## 会话落盘

``storage_state`` 是 Playwright 的格式（cookie + localStorage），一个账号一份，
放在 ``data/sessions/chaoxing.storage_state.json``（已在 .gitignore 里）。
密码只在登录那一次用，不落盘、不进日志。
"""

from __future__ import annotations

import contextlib
import logging
import re
from pathlib import Path

from kedaibiao.browser.pw import PwError, PwPage

log = logging.getLogger(__name__)

#: 登录页（``newversion=true`` 是能在无头浏览器里稳定渲染的手机号+密码表单）
LOGIN_URL = (
    "https://passport2.chaoxing.com/login"
    "?fid=&newversion=true&refer=https%3A%2F%2Fi.chaoxing.com"
)
#: 登录成功后落地页；用它判断「会话还有效吗」
HOME_URL = "https://i.chaoxing.com/base"

#: 登录页表单选择器（真实 DOM，已核实）
PHONE_INPUT = "#phone"
PASSWORD_INPUT = "#pwd"
LOGIN_BUTTON = "#loginBtn"

#: 页面上的错误提示容器
_ERROR_SELECTORS = ("#err-txt", "#phoneMsg", "#pwdMsg", ".err-tip", ".err-txt")

#: 「登录成功」的特征：个人空间里会出现的导航词
_LOGGED_IN_MARKERS = ("课程", "收件箱", "小组", "云盘")
#: 「登录失败」的常见文案（不是验证码）
_ERROR_MARKERS = ("密码错误", "账号不存在", "账号或密码错误", "请输入正确的手机号", "手机号格式")


class ChaoxingLoginError(RuntimeError):
    """登录失败。``kind`` 用来让上层区分「要人工过验证码」「密码错」「网络错」。"""

    def __init__(self, message: str, *, kind: str = "login_failed") -> None:
        super().__init__(message)
        self.kind = kind


def looks_logged_in(text: str) -> bool:
    """从页面正文判断是否已经登录（登录页正文没有这几个词）。"""
    body = text or ""
    return any(marker in body for marker in _LOGGED_IN_MARKERS) and "用户登录" not in body


def login_error_message(text: str) -> str:
    """从正文里捞出登录错误文案；没有错误返回空串。"""
    body = text or ""
    for marker in _ERROR_MARKERS:
        if marker in body:
            return marker
    pattern = r"(验证码[^\s，。]{0,10}|账号[^\s，。]{0,8}错误|密码[^\s，。]{0,8}错误)"
    match = re.search(pattern, body)
    return match.group(1) if match else ""


#: 个人空间是 SPA：readyState=complete 时导航栏往往还没渲染，要等关键词出现
_LOGGED_IN_PROBE = (
    "!!document.body && ['课程', '收件箱', '小组', '云盘']"
    ".some(k => document.body.innerText.includes(k))"
)


async def _session_cookie_names(page: PwPage) -> set[str]:
    """读当前 context 的 cookie 名（判断粗略的登录态兜底用）。"""
    context = getattr(getattr(page, "raw", None), "context", None)
    if context is None:
        return set()
    try:
        cookies = await context.cookies()
    except Exception:  # noqa: BLE001 - 读 cookie 失败就当没有
        return set()
    return {str(item.get("name", "")) for item in cookies or []}


async def is_logged_in(page: PwPage, *, timeout: int = 30000) -> bool:
    """打开个人空间判断会话是否还有效。

    个人空间是重 JS 的 SPA：``readyState=complete`` 时导航栏经常还没渲染，
    所以按「关键词出现」轮询；再不行就看 cookie 里有没有 ``_uid``/``uf``
    （有 cookie 但页面是登录页，仍然算没登录 —— 会话过期时 cookie 还在）。
    """
    await page.goto(HOME_URL, timeout=timeout)
    with contextlib.suppress(PwError):
        await page.wait_function(_LOGGED_IN_PROBE, timeout=timeout)
    body = await page.text("body")
    if looks_logged_in(body):
        return True
    names = await _session_cookie_names(page)
    return bool({"_uid", "uf"} <= names) and "用户登录" not in body


async def login_with_password(
    page: PwPage,
    username: str,
    password: str,
    *,
    timeout: int = 45000,
) -> None:
    """用手机号/超星号 + 密码登录。成功返回，失败抛 :class:`ChaoxingLoginError`。"""
    await page.goto(LOGIN_URL, timeout=timeout)
    await page.wait_for(PHONE_INPUT, timeout=timeout, state="visible")
    await page.fill(PHONE_INPUT, username)
    await page.fill(PASSWORD_INPUT, password)
    await page.click(LOGIN_BUTTON)
    # 跳转要么成功到个人空间，要么停在登录页并出现错误文案/验证码
    await page.wait_function(
        "location.hostname !== 'passport2.chaoxing.com'", timeout=timeout
    )
    await page.wait_function("document.readyState === 'complete'", timeout=timeout)
    body = await page.text("body")
    if looks_logged_in(body):
        return
    error = login_error_message(body)
    if error:
        raise ChaoxingLoginError(f"学习通登录失败：{error}", kind="bad_credentials")
    raise ChaoxingLoginError(
        "学习通登录没有跳转成功（可能需要图形验证码/滑块，或账号被风控）。"
        "请在控制台里用有头模式人工登录一次，或改用 Cookie 方式。",
        kind="needs_human",
    )


async def ensure_login(
    page: PwPage,
    *,
    username: str = "",
    password: str = "",
    state_path: str | Path | None = None,
    force: bool = False,
) -> None:
    """确保当前页面处于已登录状态：

    1. 先试现有会话（个人空间能打开、正文有「课程/收件箱」）；
    2. 不行且有账密 → 走密码登录；
    3. 登录成功后把 storage_state 落到 ``state_path``。
    """
    if not force:
        try:
            if await is_logged_in(page):
                log.info("学习通会话仍有效，跳过登录")
                return
        except PwError as exc:  # noqa: BLE001 - 网络抖动就当会话不可用
            log.debug("检查会话失败: %s", exc)
    if not username or not password:
        raise ChaoxingLoginError(
            "学习通会话已失效，且没有提供账号密码（可用 storage_state / Cookie 兜底）",
            kind="no_credentials",
        )
    await login_with_password(page, username, password)
    if state_path is not None:
        target = Path(state_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        raw = getattr(page, "raw", None)
        context = getattr(raw, "context", None)
        if context is not None:
            await context.storage_state(path=str(target))
            log.info("学习通登录态已保存: %s", target)


__all__ = [
    "ChaoxingLoginError",
    "HOME_URL",
    "LOGIN_URL",
    "ensure_login",
    "is_logged_in",
    "login_error_message",
    "login_with_password",
    "looks_logged_in",
]
