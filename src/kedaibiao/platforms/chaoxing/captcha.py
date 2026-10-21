"""超星滑块安全验证（captcha.chaoxing.com）的自动求解。

## 什么时候会遇到

进入考试/作业时，超星风控可能弹一个图片滑块：背景图里挖了一个拼图形状的
缺口，右侧滑块拖动拼图去填。DOM 长这样：

```html
<div id="cx_imgBg" style="background:url('.../slide/big/<id>.jpg')">
  <div class="cx_imgBtn"><img src=".../slide/small/<id>.jpg"></div>  <!-- 拼图，RGBA 透明底 -->
</div>
<div class="cx_rightBtn">…</div>   <!-- 拖拽手柄 -->
```

## 怎么求缺口位置

不能拿拼图的像素去和背景做模板匹配：缺口处的背景已经被挖掉/糊掉了。
可靠的是**轮廓**：拼图 alpha 蒙版的边缘形状，与背景里缺口那圈描边重合时，
边缘梯度响应最强。所以对每个候选 x（竖直位置由拼图在竖条里的位置决定）：

1. 取拼图 alpha 蒙版的轮廓点（腐蚀前后取差）；
2. 把轮廓平移到候选位置，累加背景图的梯度幅值；
3. 再减去「蒙版内部方差」的惩罚（缺口内部是平坦的）；
4. 分数最高的 x 就是缺口位置。

实测（2026-10-21）：同一张 320×160 的题，轮廓法给出的 x 与肉眼缺口一致，
按该距离拖拽后验证码通过并进入答题页。

## 边界

- 只处理「图片滑块」这一种；出现其它形态（点选、短信验证）就返回 False，
  由上层交人工；
- 拖拽用 Playwright 的鼠标事件逐帧移动（带缓动），不是一次性瞬移 ——
  超星会校验轨迹；
- 依赖 numpy/Pillow（与 fontdec 同一组可选依赖）。
"""

from __future__ import annotations

import asyncio
import io
import logging
from typing import Any

log = logging.getLogger(__name__)

#: 拖拽步数与总耗时（秒）——太短会被判定为机器
DRAG_STEPS = 34
DRAG_DELAY = 0.014

#: 缺口检测的候选扫描步长（1 = 逐像素）
_SCAN_STEP = 1


class CaptchaError(RuntimeError):
    """滑块验证无法完成（不是滑块、图片拿不到、拖拽后仍未通过）。"""


def _require(name: str) -> Any:
    import importlib  # noqa: PLC0415

    try:
        return importlib.import_module(name)
    except ImportError as exc:  # pragma: no cover - 取决于运行环境
        raise CaptchaError(
            f"滑块求解需要 {name}，请执行: pip install 'kedaibiao[chaoxing]'"
        ) from exc


def _erode(mask: Any) -> Any:
    """3×3 腐蚀（纯 numpy，避免引入 scipy）。"""
    np = _require("numpy")
    padded = np.pad(mask, 1, constant_values=False)
    return (
        padded[1:-1, 1:-1]
        & padded[:-2, 1:-1]
        & padded[2:, 1:-1]
        & padded[1:-1, :-2]
        & padded[1:-1, 2:]
    )


def find_gap_offset(big_bytes: bytes, small_bytes: bytes) -> int:
    """求拼图要向右拖多少像素（自然像素）。

    ``big_bytes`` 是背景图，``small_bytes`` 是带 alpha 的拼图竖条。返回缺口
    左边缘的 x。图片解析失败抛 :class:`CaptchaError`。
    """
    np = _require("numpy")
    image = _require("PIL.Image")
    try:
        big_image = image.open(io.BytesIO(big_bytes)).convert("RGB")
        small_image = image.open(io.BytesIO(small_bytes)).convert("RGBA")
    except Exception as exc:  # noqa: BLE001
        raise CaptchaError(f"验证码图片解析失败: {exc}") from exc
    big = np.asarray(big_image).astype("float32")
    small = np.asarray(small_image)
    mask = small[:, :, 3] > 128
    ys, xs = np.where(mask)
    if len(xs) == 0:
        raise CaptchaError("拼图是空白的（alpha 全透明）")
    x0, x1 = int(xs.min()), int(xs.max()) + 1
    y0, y1 = int(ys.min()), int(ys.max()) + 1
    piece_mask = mask[y0:y1, x0:x1]
    height, width = piece_mask.shape

    gray = big.mean(axis=2)
    gy, gx = np.gradient(gray)
    edge = np.hypot(gx, gy)
    outline = piece_mask & ~_erode(piece_mask)
    points = np.argwhere(outline)
    big_h, big_w = edge.shape
    best_score, best_x = -1e9, 0
    for x in range(0, big_w - width + 1, _SCAN_STEP):
        yy = points[:, 0] + y0
        xx = points[:, 1] + x
        valid = (yy < big_h) & (xx < big_w)
        if not valid.any():
            continue
        score = float(edge[yy[valid], xx[valid]].mean())
        patch = big[y0:y0 + height, x:x + width]
        score -= float(patch[piece_mask].std()) * 0.8
        if score > best_score:
            best_score, best_x = score, x
    return best_x


async def captcha_visible(page: Any) -> bool:
    """当前页面上有没有可见的超星滑块。"""
    script = """(() => {
        const eject = document.getElementById('eject');
        if (eject && (eject.offsetWidth || 0) > 0
            && window.getComputedStyle(eject).display !== 'none') return true;
        return false;
    })()"""
    try:
        return bool(await page.eval(script))
    except Exception:  # noqa: BLE001 - 页面正在跳转时求值可能失败
        return False


async def solve_slide_captcha(page: Any, *, attempts: int = 3) -> bool:
    """自动解滑块：找缺口 → 按距离拖动 → 校验是否通过。最多试 ``attempts`` 次。"""
    for attempt in range(1, attempts + 1):
        if not await captcha_visible(page):
            return True
        info = await page.eval(
            """(() => {
                const bg = document.getElementById('cx_imgBg');
                const small = document.querySelector('.cx_imgBtn img');
                const handle = document.querySelector('.cx_rightBtn');
                if (!bg || !small || !handle) return null;
                const m = /url\\("?([^")]+)"?\\)/.exec(bg.style.backgroundImage || '');
                const box = (el) => {
                    const b = el.getBoundingClientRect();
                    return {x: b.x, y: b.y, w: b.width, h: b.height};
                };
                return {
                    bgUrl: m ? m[1] : '', smallUrl: small.src || '',
                    big: box(bg), handle: box(handle),
                };
            })()"""
        )
        if not info or not info.get("bgUrl") or not info.get("smallUrl"):
            log.warning("滑块结构不认识，交人工")
            return False
        context = getattr(getattr(page, "raw", None), "context", None)
        if context is None:
            return False
        big = await (await context.request.get(info["bgUrl"], timeout=20000)).body()
        small = await (await context.request.get(info["smallUrl"], timeout=20000)).body()
        offset = find_gap_offset(big, small)
        scale = float(info["big"]["w"]) / 320.0 if info["big"]["w"] else 1.0
        distance = offset * scale
        log.info("滑块第 %d 次：缺口 x=%d，拖动 %.0f px", attempt, offset, distance)
        handle = info["handle"]
        start_x = handle["x"] + handle["w"] / 2
        start_y = handle["y"] + handle["h"] / 2
        mouse = page.raw.mouse
        await mouse.move(start_x, start_y)
        await asyncio.sleep(0.1)
        await mouse.down()
        for step in range(1, DRAG_STEPS + 1):
            ratio = step / DRAG_STEPS
            eased = 1 - (1 - ratio) ** 2.2
            await mouse.move(
                start_x + distance * eased,
                start_y + (0.8 if step % 2 else -0.5),
            )
            await asyncio.sleep(DRAG_DELAY)
        await asyncio.sleep(0.2)
        await mouse.up()
        await asyncio.sleep(3)
        if not await captcha_visible(page):
            log.info("滑块验证通过")
            return True
        log.warning("滑块第 %d 次未通过", attempt)
        await asyncio.sleep(1)
    return False


__all__ = [
    "CaptchaError",
    "captcha_visible",
    "find_gap_offset",
    "solve_slide_captcha",
]
