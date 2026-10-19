"""登录中继：把「要人机验证的登录」搬到控制台里做。

## 为什么需要它

Unipus SSO（unipus / pinco / itest 三个平台共用）在风控触发时会弹**滑块拼图
验证码**。这个验证码是给人看的，脚本解不了 —— 也不该去解。

但用户不在服务器跟前（无显示器），所以设计成：

```
后端启动真实浏览器 → 打开 SSO 登录页 → 填账号密码 → 点登录
        ↓ 出现验证码
把浏览器画面截图发给前端 → 用户在控制台里拖滑块
        ↓ 前端把鼠标轨迹发回来
后端把轨迹**原样回放**给浏览器（CDP Input.dispatchMouseEvent）
        ↓ 验证通过
读取 cookie → 存进该账号 → 关闭浏览器
```

## 三条纪律

1. **不伪造轨迹**：回放的是用户真实拖拽的折线，不是程序生成的「像人」的曲线。
   这一步的意义是「让人来过验证」，不是「让机器骗过验证」。
2. **浏览器只在服务端开**：绑定回环以外的调试端口不开；用完立刻关。
3. **会话即凭据**：成功后取到的 cookie 与手动登录拿到的完全等价，
   按账号的 cookie 文件落盘（0600），密码不落盘。

## 状态机

``starting`` → ``need_login``（要填/点）→ ``captcha``（等人拖）→ ``success`` / ``failed``
"""

from __future__ import annotations

import base64
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kedaibiao.browser.cdp import Browser, CDPError, find_browser

log = logging.getLogger(__name__)

#: 各平台的 SSO 登录入口（都跳到 sso.unipus.cn）
LOGIN_ENTRIES = {
    "unipus": "https://ucontent.unipus.cn/",
    "pinco": "https://pinco.seewo.com/",
    "itest": "https://itestcloud.unipus.cn/utest/itest/login",
}

STAGE_STARTING = "starting"
STAGE_NEED_LOGIN = "need_login"
STAGE_CAPTCHA = "captcha"
STAGE_SUCCESS = "success"
STAGE_FAILED = "failed"


class RelayError(RuntimeError):
    pass


@dataclass(slots=True)
class LoginRelay:
    """一次登录中继。由服务层持有，按 id 存活若干分钟。"""

    platform: str
    username: str = ""
    password: str = ""
    entry_url: str = ""
    stage: str = STAGE_STARTING
    note: str = ""
    created_at: float = field(default_factory=time.time)
    #: 成功后的 cookie 列表（[{name,value,domain,path,...}]）
    cookies: list[dict[str, Any]] = field(default_factory=list)
    _browser: Browser | None = None

    # -- 生命周期 ---------------------------------------------------------

    def start(self, *, width: int = 1280, height: int = 900, timeout: float = 30.0) -> None:
        executable = find_browser()
        if not executable:
            raise RelayError("找不到 Chrome/Chromium，无法启动登录中继")
        browser = Browser(executable=executable, timeout=timeout)
        try:
            browser.start()
            # 设定视口，保证前端拿到的坐标与浏览器一致
            browser.call("Emulation.setDeviceMetricsOverride", {
                "width": width, "height": height, "deviceScaleFactor": 1, "mobile": False,
            })
            browser.navigate(self.entry_url or LOGIN_ENTRIES.get(self.platform, ""))
            time.sleep(2.5)
            self._browser = browser
        except Exception:
            browser.close()
            raise
        self.stage = STAGE_NEED_LOGIN

    def close(self) -> None:
        if self._browser is not None:
            try:
                self._browser.close()
            except Exception:  # noqa: BLE001
                log.debug("关闭中继浏览器失败", exc_info=True)
            self._browser = None

    @property
    def alive(self) -> bool:
        return self._browser is not None

    # -- 观察 -------------------------------------------------------------

    def snapshot(self, *, with_image: bool = True) -> dict[str, Any]:
        """当前状态 + 一张截图（前端据此显示）。"""
        data: dict[str, Any] = {
            "platform": self.platform,
            "stage": self.stage,
            "note": self.note,
            "alive": self.alive,
            "url": "",
            "image": "",
            "cookies_ready": bool(self.cookies),
        }
        if self._browser is None:
            return data
        try:
            data["url"] = self._browser.call("Runtime.evaluate", {
                "expression": "location.href", "returnByValue": True,
            }).get("result", {}).get("value", "")
        except CDPError:
            pass
        if with_image:
            try:
                png = self._browser.screenshot()
                data["image"] = base64.b64encode(png).decode("ascii")
            except CDPError as exc:
                data["note"] = f"截图失败：{exc}"
        self._refresh_stage(data.get("url", ""))
        data["stage"] = self.stage
        return data

    def _refresh_stage(self, url: str) -> None:
        """根据落地 URL 粗判阶段（仅用于界面提示，不参与判定）。"""
        if self.stage in (STAGE_SUCCESS, STAGE_FAILED):
            return
        if not url:
            return
        if "sso.unipus.cn/sso/login" in url:
            self.stage = STAGE_NEED_LOGIN
        elif "sso.unipus.cn" not in url:
            # 已经离开 SSO → 大概率登录成功
            self.stage = STAGE_SUCCESS

    # -- 输入 -------------------------------------------------------------

    def apply_actions(self, actions: list[dict[str, Any]]) -> dict[str, Any]:
        """回放前端传来的用户操作。

        支持：
        - ``{"type":"click","x":..,"y":..}``
        - ``{"type":"drag","points":[[x,y],...]}``  —— 滑块的真实轨迹
        - ``{"type":"text","text":"..."}``
        - ``{"type":"key","key":"Enter"}``
        """
        if self._browser is None:
            raise RelayError("中继浏览器已关闭")
        for action in actions or []:
            kind = str(action.get("type") or "")
            try:
                if kind == "click":
                    self._browser.click(float(action["x"]), float(action["y"]))
                elif kind == "drag":
                    points = [(float(p[0]), float(p[1])) for p in action.get("points") or []]
                    if len(points) >= 2:
                        self._browser.drag(points)
                elif kind == "text":
                    self._browser.insert_text(str(action.get("text") or ""))
                elif kind == "key":
                    self._browser.press_key(str(action.get("key") or ""))
            except (CDPError, KeyError, TypeError, ValueError) as exc:
                raise RelayError(f"回放 {kind} 失败：{exc}") from exc
        time.sleep(0.6)
        return self.snapshot()

    # -- 收尾 -------------------------------------------------------------

    def capture_cookies(self) -> list[dict[str, Any]]:
        """把浏览器里的 cookie 全部取出来（等价于手动登录拿到的会话）。"""
        if self._browser is None:
            raise RelayError("中继浏览器已关闭")
        cookies = self._browser.cookies()
        self.cookies = [
            {"name": c.name, "value": c.value, "domain": c.domain,
             "path": c.path, "secure": c.secure, "http_only": c.http_only}
            for c in cookies
        ]
        return self.cookies

    def as_cookie_file(self) -> list[dict[str, Any]]:
        """转换成 :class:`UrllibTransport` 能吃的 cookie 文件格式。"""
        return [
            {
                "domain": c["domain"], "path": c.get("path") or "/",
                "name": c["name"], "value": c["value"],
                "secure": bool(c.get("secure")), "expires": None,
            }
            for c in self.cookies
        ]

    def save_cookie_file(self, path: str | Path) -> Path:
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(self.as_cookie_file(), ensure_ascii=False), encoding="utf-8")
        try:
            out.chmod(0o600)
        except OSError:
            pass
        return out
