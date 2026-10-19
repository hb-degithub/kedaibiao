"""从浏览器用户目录里抓取某个站点的 cookie。

## 为什么是「用户目录」而不是「让用户点」

这台机器没有显示器（``DISPLAY`` 为空），也没装 playwright，开不出可交互窗口。
但 Chrome 自带调试端口，而**登录态本来就存在 profile 目录里** —— 只要用户
在那个目录里登录过一次，headless 启动后打开页面，``Network.getAllCookies``
就能把包括 HttpOnly 在内的 cookie 全部读出来。

好处：
- 不接触密码（不落盘、不输入）；
- 不碰验证码/扫码/二次验证 —— 那些用户在自己浏览器里已经过了；
- 读得到 HttpOnly（``document.cookie`` 读不到，这也是为什么不能靠前端脚本）。

## 前提与失败形态

- 前提：``--profile`` 指向一个**登录过目标站点**的 Chrome 用户目录；
- 若那个 Chrome 正在运行，profile 会被锁 → 明确报错（不要冒险复制一份正在写的库）；
- 抓不到目标 cookie 时，报错会写清楚「先在那个 profile 里登录一次再试」。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kedaibiao.browser.cdp import Browser, CDPError, Cookie, cookie_header, find_browser

log = logging.getLogger(__name__)


class CaptureError(RuntimeError):
    def __init__(self, message: str, *, kind: str = "capture_failed", status: int = 400) -> None:
        super().__init__(message)
        self.kind = kind
        self.status = status


@dataclass(slots=True)
class CaptureResult:
    cookie_header: str
    cookies: list[Cookie]
    url: str

    def names(self) -> list[str]:
        return sorted({c.name for c in self.cookies})

    def summary(self, *, required: tuple[str, ...] = ()) -> str:
        missing = [name for name in required if name not in self.names()]
        parts = [f"抓到 {len(self.cookies)} 条 cookie"]
        if required:
            parts.append("关键项齐全" if not missing else f"缺少关键项: {', '.join(missing)}")
        return "；".join(parts)


def capture_cookies(
    *,
    url: str,
    profile: str | Path | None = None,
    domain_hint: str = "",
    wait_seconds: float = 4.0,
    required: tuple[str, ...] = (),
    browser_path: str | None = None,
    timeout: float = 30.0,
) -> CaptureResult:
    """用浏览器打开 ``url``，把该站点的 cookie 抓出来。

    ``profile`` 省略时用一个全新的临时目录 —— 那里面当然没有登录态，
    只在测试/自检时有用。
    """
    executable = browser_path or find_browser()
    if not executable:
        raise CaptureError(
            "找不到 Chrome/Chromium。装一个 Chrome，或用 --browser 指定路径。",
            kind="no_browser", status=400,
        )

    try:
        browser = Browser(
            executable=executable,
            user_data_dir=profile,
            timeout=timeout,
        )
    except CDPError as exc:
        raise CaptureError(str(exc), kind="profile_locked", status=409) from exc

    try:
        with browser:
            try:
                browser.navigate(url)
            except CDPError as exc:
                raise CaptureError(f"打开页面失败：{exc}") from exc
            # 给页面一点时间把会话相关 cookie 种下来（有的站点会二次跳转）
            import time

            time.sleep(max(0.0, wait_seconds))
            cookies = browser.cookies()
    except CDPError as exc:
        raise CaptureError(f"浏览器会话出错：{exc}") from exc

    header = cookie_header(cookies, domain_hint=domain_hint)
    if not header:
        raise CaptureError(
            f"在 {url} 下没抓到任何 cookie。"
            f"通常是因为这个用户目录没有登录过该站点 —— 先用它打开 {url} 登录一次，再重试。",
            kind="no_cookies", status=400,
        )

    result = CaptureResult(cookie_header=header, cookies=cookies, url=url)
    if required:
        missing = [name for name in required if name not in result.names()]
        if missing:
            raise CaptureError(
                f"抓到了 cookie 但缺少关键项 {', '.join(missing)}。"
                f"请确认已经真正登录（不是只打开了登录页），并稍等几秒后重试。",
                kind="missing_cookie", status=400,
            )
    return result


def save_session_cookie(session_file: str | Path, header: str) -> Path:
    """把 cookie 串写进账号的会话快照文件（0600）。

    与账号的其他会话文件放在一起，删账号时会被一起清掉。
    """
    if not header.strip():
        raise CaptureError("cookie 串为空")
    path = Path(session_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"cookie_header": header}, ensure_ascii=False), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


def parse_cookie_header(header: str) -> dict[str, str]:
    """解析 ``a=1; b=2`` —— 供 CLI 校验手贴的串。"""
    out: dict[str, str] = {}
    for part in (header or "").split(";"):
        name, sep, value = part.strip().partition("=")
        if sep and name:
            out[name.strip()] = value.strip()
    return out


def describe_cookies(cookies: list[Cookie]) -> list[dict[str, Any]]:
    """给界面/日志用的摘要 —— **值一律截断**，避免把令牌写进日志。"""
    return [
        {
            "name": c.name,
            "domain": c.domain,
            "http_only": c.http_only,
            "secure": c.secure,
            "length": len(c.value),
        }
        for c in cookies
    ]
