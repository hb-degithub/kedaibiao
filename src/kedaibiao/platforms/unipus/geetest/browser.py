"""在浏览器里触发验证码、在外面解掉、再注入登录。

## 为什么是这个架构

极验 v3 的 challenge 是**一次性**的：页面自己用它取过图（``get.php``）之后，
再用它取图会报 ``error_02 old challenge``。而新 challenge 只能由站点后端签发。

所以自己注册一个 challenge 走不通（会 ``error_21 not proof``）。正确做法是
**蹭页面那一次**：

```
1. 浏览器打开 SSO 登录页 → 填账号密码 → 点登录 → 极验弹出
2. 抓页面自己那次 get.php(is_next=true) 的响应 → 拿到 challenge / s / 图片地址
3. 用我们的求解器算距离 + 轨迹 + w（纯 HTTP，不依赖浏览器）
4. 打 ajax.php → geetest_validate
5. 把 {challenge, validate, seccode} 注入 SSO 登录请求 → serviceTicket
```

## 实测记录（2026-10-19）

- 抓包要点：极验跑在**跨域 iframe** 里，必须 ``Target.setAutoAttach(flatten)``，
  否则请求不出现在当前会话上
- 求解结果：缺口 135 / w 1132 字符 / ajax.php 返回
  ``{"success":1,"validate":"57492d…","score":"1"}``
- 登录结果：``cip/login`` 返回 ``{"code":"0","rs":{"serviceTicket":"ST-…"}}``
  —— **裸字段与 geetest_ 前缀两种命名都能通过**

## 复用

:func:`solve_login_captcha` 是入口：给一个已启动的 :class:`Browser` 与登录信息，
返回 :class:`CaptchaSolution`。它不负责把 ticket 换成会话 cookie —— 那是
:mod:`kedaibiao.platforms.unipus.client` 的事。
"""

from __future__ import annotations

import json
import logging
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from kedaibiao.browser.cdp import Browser, CDPError
from kedaibiao.platforms.unipus.geetest import v3

log = logging.getLogger(__name__)

#: 页面自己那次取图请求（含图片素材的那一次）
GET_PHP_MARK = "get.php"

#: Unipus SSO 登录页（各平台跳到这里）
SSO_LOGIN = "https://sso.unipus.cn/sso/login"


class CaptchaSolutionError(RuntimeError):
    pass


@dataclass(slots=True)
class CaptchaSolution:
    """一次验证码求解的产物 —— 可直接塞进 ``cip/login``。"""

    gt: str
    challenge: str
    validate: str
    distance: int = 0
    elapsed: float = 0.0

    @property
    def seccode(self) -> str:
        """极验 v3 的 seccode 约定：``validate|jordan``。"""
        return f"{self.validate}|jordan"

    def login_fields(self, *, prefixed: bool = False) -> dict[str, str]:
        """转成登录请求要带的字段。

        ``prefixed=True`` 用 ``geetest_*`` 命名 —— 实测两种都能通过，
        保留开关是为了别的站点可能只认其中一种。
        """
        if prefixed:
            return {
                "geetest_challenge": self.challenge,
                "geetest_validate": self.validate,
                "geetest_seccode": self.seccode,
            }
        return {
            "challenge": self.challenge,
            "validate": self.validate,
            "seccode": self.seccode,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "gt": self.gt, "challenge": self.challenge,
            "validate": self.validate, "seccode": self.seccode,
            "distance": self.distance, "elapsed": round(self.elapsed, 2),
        }


def _parse_jsonp(text: str) -> dict[str, Any]:
    body = (text or "").strip()
    if "(" in body and body.endswith(")"):
        body = body[body.find("(") + 1: body.rfind(")")]
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise CaptchaSolutionError(f"响应不是 JSON: {text[:160]!r}") from exc
    return data if isinstance(data, dict) else {}


#: 在登录页里填表并点「登录」的脚本。
#: 用页面自己的按钮触发（而不是 form.submit()）—— 否则会绕过页面的加密与验证码逻辑。
_FILL_AND_SUBMIT = """
(() => {
  document.querySelectorAll('input[type=checkbox]').forEach(b => { if (!b.checked) b.click(); });
  const set = (el, v) => {
    el.focus(); el.value = v;
    el.dispatchEvent(new Event('input',  { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
  };
  const u = document.querySelector('input[name=username], #username');
  const p = document.querySelector('input[name=password], #password');
  if (!u || !p) return 'no-form';
  set(u, %s); set(p, %s);
  const btn = [...document.querySelectorAll('button,div,a')]
      .find(x => x.offsetParent && x.textContent.trim() === '登录');
  if (!btn) return 'no-button';
  btn.click();
  return 'clicked';
})()
"""


def trigger_captcha(
    browser: Browser,
    *,
    login_url: str,
    username: str,
    password: str,
    wait: float = 9.0,
) -> dict[str, str]:
    """打开登录页 → 填表点登录 → 抓回含图片素材的那次 ``get.php`` 响应。

    返回 ``{"url": ..., "body": ...}``；抓不到就是空 dict。
    """
    browser.enable_network()
    browser.navigate(login_url)
    time.sleep(4.0)

    script = _FILL_AND_SUBMIT % (json.dumps(username), json.dumps(password))
    try:
        outcome = browser.evaluate(script)
    except CDPError as exc:
        raise CaptchaSolutionError(f"填表失败：{exc}") from exc
    if outcome != "clicked":
        raise CaptchaSolutionError(f"登录页结构不符（{outcome}）—— 可能已改版")

    # 点完之后才开始等：极验会在稍后发出 is_next 那次请求
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        captured = browser.capture_response(
            GET_PHP_MARK, timeout=max(1.0, deadline - time.monotonic()), settle=2.0,
        )
        if captured.get("body") and '"slice"' in captured["body"]:
            return captured
    return {}


def solution_from_capture(captured: dict[str, str], *, referer: str = SSO_LOGIN,
                          http: Any = None) -> CaptchaSolution:
    """把抓到的素材交给求解器，拿 validate。"""
    import asyncio

    from kedaibiao.net.http import HttpClient, UrllibTransport

    body = captured.get("body") or ""
    if not body:
        raise CaptchaSolutionError("没有素材（get.php 响应为空）")
    data = _parse_jsonp(body)
    payload = data.get("data") if isinstance(data.get("data"), dict) else data
    if not payload.get("slice") or not payload.get("bg"):
        raise CaptchaSolutionError("素材里没有图片地址 —— 可能抓错了那一次 get.php")

    own_http = http is None
    client = http or HttpClient(UrllibTransport(), timeout=45)
    gt = str(payload.get("gt") or "")
    challenge = str(payload.get("challenge") or "")
    s = str(payload.get("s") or "")
    headers = {"Referer": referer}

    def normalize(host: str, fallback: str) -> str:
        value = host or fallback
        return value.split("://", 1)[-1].strip("/")

    server = normalize(
        (payload.get("static_servers") or ["static.geetest.com"])[0], "static.geetest.com"
    )
    api = normalize(str(payload.get("api_server") or ""), "api.geevisit.com")

    async def run() -> v3.SolveResult:
        slice_bytes = (await client.get(f"https://{server}/{payload['slice']}",
                                        headers=headers)).body
        bg_bytes = (await client.get(f"https://{server}/{payload['bg']}",
                                     headers=headers)).body
        distance = v3.find_gap(slice_bytes, v3.unscramble(bg_bytes))
        slide_length = max(0, distance - 5)      # 与原实现一致的 5px 容差
        trace = v3.build_trace(slide_length)
        w = v3.compute_w(
            slide_length=slide_length, trace=trace, s=s, challenge=challenge,
            passtime=trace[-1][2], imgload=random.randint(100, 200), gt=gt,
        )
        query = (f"gt={gt}&challenge={challenge}&lang=zh-cn&pt=0&w={w}"
                 f"&callback=geetest_{int(time.time() * 1000)}")
        response = await client.get(f"https://{api}/ajax.php?{query}", headers=headers)
        parsed = _parse_jsonp(response.text())
        ok = bool(parsed.get("success")) or str(parsed.get("status") or "") in ("success", "ok")
        return v3.SolveResult(
            ok=ok, distance=distance,
            validate=str(parsed.get("validate") or ""),
            seccode=str(parsed.get("geetest_seccode") or ""),
            message="" if ok else str(parsed.get("message") or response.text()[:120]),
            raw=parsed,
        )

    try:
        started = time.time()
        result = asyncio.run(run())
    finally:
        if own_http:
            del client

    if not result.ok:
        raise CaptchaSolutionError(f"极验校验未通过：{result.message}")
    return CaptchaSolution(
        gt=gt, challenge=challenge, validate=result.validate,
        distance=result.distance, elapsed=time.time() - started,
    )


def solve_login_captcha(
    browser: Browser,
    *,
    login_url: str,
    username: str,
    password: str,
    on_event: Callable[[str], None] | None = None,
) -> CaptchaSolution:
    """端到端：触发 → 抓素材 → 求解。**不负责登录**（那是调用方的事）。

    调用方拿到 :class:`CaptchaSolution` 后，用
    ``solution.login_fields()`` 拼进 ``cip/login`` 的请求体即可。
    """
    def emit(message: str) -> None:
        if on_event is not None:
            try:
                on_event(message)
            except Exception:  # noqa: BLE001
                pass

    emit("打开登录页并触发验证码…")
    captured = trigger_captcha(browser, login_url=login_url,
                               username=username, password=password)
    if not captured:
        raise CaptchaSolutionError(
            "没抓到极验素材。常见原因：页面结构变了、等待太短、"
            "或者没开 Target.setAutoAttach（跨域 iframe 里的请求看不到）。"
        )
    emit("已拿到素材，正在求解…")
    solution = solution_from_capture(captured)
    emit(f"求解成功：缺口 {solution.distance}px，耗时 {solution.elapsed:.2f}s")
    return solution


def save_session(session: CaptchaSolution, cookies: list[dict[str, Any]],
                 cookie_file: str | Path) -> Path:
    """把浏览器里的会话 cookie 落盘成账号的 cookie 文件（0600）。

    求解成功后浏览器里就是已登录态，取它的 cookie 等价于手动登录拿到的会话。
    """
    import stat
    from pathlib import Path as _Path

    out = _Path(cookie_file)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(cookies, ensure_ascii=False), encoding="utf-8")
    try:
        out.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    return out
