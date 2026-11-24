#!/usr/bin/env python3
"""极验滑块「在浏览器里直接拖」的实验脚本（闭环定位 + 逐步取证）。

## 为什么有这个脚本

`platforms/unipus/geetest/` 原来的做法是：抓 `get.php` 素材 → 本地算 `w`
（AES+RSA，靠 vendored `jiyan.js`）→ 自己 POST `ajax.php`。2026-11-24 实测
这套在**新版极验**上过不去：素材与缺口都对（`distance` 与肉眼一致），
但 `ajax.php` 回 `{"message":"fail"}` —— 说明 `w` 的构造已经不是
`jiyan.js` 那一代了。

本脚本走另一条路：**不自己算 w**，而是用 CDP 的真实鼠标事件把滑块拖到位，
让页面自己的 JS 去完成校验。这样绕开了加密细节。

## 现在到哪一步（2026-11-24 实测）

- ✅ 图片还原 + 缺口定位正确（`unscramble` + `find_gap`）
- ✅ 闭环拖拽有效：拖完读 `.geetest_canvas_slice` 画布像素，拼图块左沿与
  缺口目标**逐像素吻合**（`piece_x == gap`），不再出现「请正确拼合图像」
- ❌ 但极验自己的校验请求仍会失败/超时（面板停在原地，偶发「网络不给力」），
  拿不到 `validate`，因此随后的 `cip/login` 依旧回 `code=1506`
- 结论：**差的不是图像或轨迹，是 `w`/validate 那一环**。下一步应当改用页面
  自己那份 `gct.js`（`gct_path`）在 Node 里算 `w`，而不是继续用旧 `jiyan.js`

这个脚本留作继续排查的起点：它把每一步（抓素材、算缺口、拖拽、读画布位置、
读面板状态、截图）都打印出来了。

用法::

    KDB_UP_PW='<平台密码>' PYTHONPATH=src .venv/bin/python scripts/geetest_drag_probe.py
    # 截图落在 /tmp/gt_probe_*.png
"""

from __future__ import annotations

import base64
import io
import json
import os
import sys
import time

from kedaibiao.platforms.unipus.geetest import browser as gt_browser
from kedaibiao.platforms.unipus.geetest import v3


def _pad_left(slice_bytes: bytes) -> int:
    """拼图块图片左侧的透明内边距（拖拽时要减掉）。"""
    from PIL import Image

    image = Image.open(io.BytesIO(slice_bytes)).convert("RGBA")
    width, height = image.size
    pixels = image.load()
    for x in range(width):
        for y in range(height):
            if pixels[x, y][3] > 40:
                return x
    return 0


def _piece_x(cdp) -> int | None:
    """读 `.geetest_canvas_slice` 画布像素，返回拼图块最左沿（画布坐标）。"""
    from PIL import Image

    url = cdp.evaluate(
        "(()=>{const c=document.querySelector('.geetest_canvas_slice');"
        "return c?c.toDataURL('image/png'):'';})()"
    )
    if not url or "," not in str(url):
        return None
    raw = base64.b64decode(str(url).split(",", 1)[1])
    image = Image.open(io.BytesIO(raw)).convert("RGBA")
    width, height = image.size
    pixels = image.load()
    for x in range(width):
        for y in range(height):
            if pixels[x, y][3] > 40:
                return x
    return None


def _panel_state(cdp) -> dict:
    return cdp.evaluate(
        "(() => {const t=document.body.innerText;"
        "const p=document.querySelector('.geetest_panel');"
        "return {wrong:t.includes('请正确拼合图像'),net:t.includes('网络不给力'),"
        "shown: !!p && getComputedStyle(p).display!=='none',"
        "url: location.href};})()"
    )


def _geometry(cdp) -> dict:
    return cdp.evaluate(
        "(()=>{const r=e=>{if(!e) return null;const b=e.getBoundingClientRect();"
        "return [b.x,b.y,b.width,b.height];};"
        "const bgd=document.querySelector('.geetest_canvas_bg');"
        "return {bg:r(bgd),btn:r(document.querySelector('.geetest_slider_button')),"
        "bgW:bgd?bgd.width:0};})()"
    )


def _drag_to(cdp, start: tuple[float, float], dx: int) -> None:
    """带节拍的拖拽：**必须有人味**，否则极验回「怪兽吃了拼图」。

    2026-11-24 实测：位置拖准（拼图块左沿 == 缺口）后仍失败，报「怪兽吃了拼图，
    请重试」—— 那是轨迹/速度判定，不是位置判定。``Browser.drag`` 默认零延迟，
    整段几毫秒就发完，等于告诉平台「这不是人拖的」。

    这里按缓出曲线分步，并在每步之间 sleep；末段放慢、松手前停顿一下。
    """
    import random

    x0, y0 = start
    cdp.mouse("pressed", x0, y0)
    steps = max(24, abs(dx) // 3)
    total_ms = random.randint(650, 1100)
    elapsed = 0.0
    for i in range(1, steps + 1):
        ratio = i / steps
        # 缓出：起步快、末段慢（人手特征）
        eased = 1 - (1 - ratio) ** 2.2
        x = x0 + dx * eased
        y = y0 + random.uniform(-1.2, 1.2)
        # 节拍逐渐变慢
        delay = (total_ms / steps) * (0.6 + ratio) / 1000.0
        time.sleep(delay)
        elapsed += delay
        cdp.mouse("moved", x, y)
    # 松手前停一下，再补两帧微调（人总会抖一下）
    time.sleep(random.uniform(0.12, 0.25))
    for _ in range(2):
        cdp.mouse("moved", x0 + dx + random.uniform(-0.8, 0.8), y0)
        time.sleep(random.uniform(0.03, 0.07))
    cdp.mouse("released", x0 + dx, y0)


def main() -> int:
    try:
        from kedaibiao.browser.cdp import Browser, find_browser
        from kedaibiao.net.http import HttpClient, UrllibTransport
    except ImportError as exc:  # pragma: no cover
        print(f"缺少依赖：{exc}")
        return 2

    password = os.environ.get("KDB_UP_PW", "")
    # 用户名/密码都只从环境变量取，**脚本里不留任何真实账号**
    username = os.environ.get("KDB_UP_USER", "")
    if not username or not password:
        print("请用 KDB_UP_USER / KDB_UP_PW 环境变量提供账号密码（都不会写盘）")
        return 2

    fill = gt_browser._FILL_AND_SUBMIT % (
        json.dumps(username),
        json.dumps(password),
    )

    cdp = Browser(executable=find_browser(), timeout=40)
    cdp.start()
    cdp.enable_network()
    cdp.navigate(gt_browser.PLATFORM_ENTRIES["unipus"])
    time.sleep(4)

    first = cdp.capture_response("cip/login", trigger_script=fill, timeout=12, settle=2.0)
    print("① 首次登录响应:", (first.get("body") or "")[:160])

    captured = cdp.capture_response(gt_browser.GET_PHP_NEXT_MARK, trigger_script="1",
                                    timeout=8, settle=1.5)
    try:
        data = gt_browser._parse_jsonp(captured.get("body") or "")
        payload = data.get("data") if isinstance(data.get("data"), dict) else data
    except Exception:  # noqa: BLE001
        payload = None
    if not payload or not payload.get("slice"):
        print("没抓到素材（可能这次没弹验证码，或被限流）")
        cdp.close()
        return 1

    import asyncio

    async def download() -> tuple[bytes, bytes]:
        client = HttpClient(UrllibTransport(), timeout=45)
        headers = {"Referer": gt_browser.SSO_LOGIN}
        slice_bytes = (
            await client.get(f"https://static.geetest.com/{payload['slice']}", headers=headers)
        ).body
        bg_bytes = (
            await client.get(f"https://static.geetest.com/{payload['bg']}", headers=headers)
        ).body
        return slice_bytes, bg_bytes

    slice_bytes, bg_bytes = asyncio.run(download())
    gap = v3.find_gap(slice_bytes, v3.unscramble(bg_bytes))
    pad = _pad_left(slice_bytes)
    print(f"② 缺口={gap}px 左内边距={pad}px  challenge={str(payload.get('challenge'))[:16]}…")

    geo = _geometry(cdp)
    if not geo.get("btn"):
        print("找不到滑块按钮，页面结构可能变了")
        cdp.close()
        return 1
    scale = (geo["bg"][2] / float(geo["bgW"])) if geo.get("bgW") else 1.0
    start = (
        geo["btn"][0] + geo["btn"][2] / 2,
        geo["btn"][1] + geo["btn"][3] / 2,
    )
    print(f"③ 显示缩放={scale:.3f}  起点=({start[0]:.0f},{start[1]:.0f})")

    target = gap
    moved = 0
    for round_no in range(1, 4):
        current = _piece_x(cdp) or 0
        error = target - current
        delta = round((gap - pad) * scale) if round_no == 1 else round(error * scale)
        if abs(delta) < 2:
            break
        _drag_to(cdp, (start[0] + moved, start[1]), delta)
        moved += delta
        time.sleep(0.8)
        state = _panel_state(cdp)
        print(
            f"   第{round_no}轮：拖 {delta}px → 块x={_piece_x(cdp)} 目标={target} "
            f"wrong={state.get('wrong')} net={state.get('net')} 面板={state.get('shown')}"
        )
        if not state.get("wrong") and not state.get("net") and not state.get("shown"):
            break

    try:
        png = cdp.call("Page.captureScreenshot", {"format": "png"})
        with open("/tmp/gt_probe_after.png", "wb") as handle:
            handle.write(base64.b64decode(png["data"]))
        print("④ 截图：/tmp/gt_probe_after.png")
    except Exception as exc:  # noqa: BLE001
        print("截图失败:", exc)

    state = _panel_state(cdp)
    print("⑤ 面板状态:", state)
    print("   cookies:", sorted({c.name for c in cdp.cookies()}))
    cdp.close()
    return 0 if not state.get("shown") else 1


if __name__ == "__main__":
    sys.exit(main())
