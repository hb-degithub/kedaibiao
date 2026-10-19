"""极验 Geetest v3 滑块求解（unipus / pinco / itest 共用 Unipus SSO）。

## 用途与边界

仅用于**用户本人账号**在无人值守时的登录。极验是商业反机器人产品，
本模块只为个人自用工具服务，不得用于批量注册 / 撞库 / 薅羊毛。

## 流程

```
register  → gt, challenge
get.php   → challenge, s, bg / fullbg / slice
还原背景图（v3 特有：52 块置换）
缺口定位（滑块图 vs 还原后的背景图）
轨迹生成（ease-out-expo + 抖动）
w = vendor 的 jiyan.js 计算（AES + RSA）
ajax.php  → geetest_validate / geetest_seccode
```

## 为什么 w 交给 Node 算

`jiyan.js`（MIT，已 vendor）经过控制流平坦化 + 字符串表混淆，逐行翻成 Python
的正确性无法保证。所以原样 vendor，用 subprocess 调 node。**登录时才用到**，
不影响不登录的场景。

## 依赖

- **Node**（算 w 必需；缺了会抛 :class:`GeetestUnavailable`）
- **Pillow**（缺口定位；缺了退化为纯 Python 的粗定位）
"""

from __future__ import annotations

import json
import logging
import math
import random
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

VENDOR_DIR = Path(__file__).resolve().parent / "vendor"
DRIVER = VENDOR_DIR / "driver.js"

#: 极验 v3 背景图置换表 —— **v3 的指纹**，v4 没有这一步。
#: 背景被切成 52 块（上 26 + 下 26，每块 10×80）打乱，按此表拼回。
UNSCRAMBLE_TABLE = [
    39, 38, 48, 49, 41, 40, 46, 47, 35, 34, 50, 51, 33, 32, 28, 29, 27, 26,
    36, 37, 31, 30, 44, 45, 43, 42, 12, 13, 23, 22, 14, 15, 21, 20, 8, 9,
    25, 24, 6, 7, 3, 2, 0, 1, 11, 10, 4, 5, 19, 18, 16, 17,
]

BLOCK_W = 10
BLOCK_H = 80
CANVAS_W = 260
CANVAS_H = 160

#: 官方 demo 的注册接口（阶段 1–4 用它做匿名验证，不碰用户账号）
DEMO_REGISTER = "https://www.geetest.com/demo/gt/register-slide-official"


class GeetestError(RuntimeError):
    pass


class GeetestUnavailable(GeetestError):
    """环境缺依赖（例如没有 Node）。"""


@dataclass(slots=True)
class Challenge:
    """一次挑战的素材。"""

    gt: str
    challenge: str
    s: str = ""
    bg_url: str = ""
    fullbg_url: str = ""
    slice_url: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------
# 背景图还原（v3 特有）
# --------------------------------------------------------------------------


def unscramble(image_bytes: bytes, *, canvas: tuple[int, int] = (CANVAS_W, CANVAS_H)) -> bytes:
    """把极验 v3 打乱的背景图拼回正常顺序。

    这一步**不能省** —— 不还原的话缺口位置算出来是错的。
    """
    try:
        from io import BytesIO

        from PIL import Image
    except ImportError as exc:  # pragma: no cover - 环境相关
        raise GeetestUnavailable("需要 Pillow 才能还原极验背景图") from exc

    source = Image.open(BytesIO(image_bytes))
    out = Image.new("RGBA", canvas)
    half = canvas[1] // 2
    for index, slot in enumerate(UNSCRAMBLE_TABLE):
        src_x = slot % 26 * 12 + 1
        src_y = half if slot > 25 else 0
        piece = source.crop((src_x, src_y, src_x + BLOCK_W, src_y + BLOCK_H))
        dst_x = index % 26 * BLOCK_W
        dst_y = half if index > 25 else 0
        out.paste(piece, (dst_x, dst_y))

    buffer = BytesIO()
    out.convert("RGB").save(buffer, format="PNG")
    return buffer.getvalue()


# --------------------------------------------------------------------------
# 缺口定位
# --------------------------------------------------------------------------


def find_gap(slice_bytes: bytes, bg_bytes: bytes, *, search: tuple[int, int] = (5, 250)) -> int:
    """返回滑块应该在的 x 偏移（缺口左边缘）。

    做法：两张图都取边缘，然后在背景图上滑动匹配滑块图的边缘模板。
    用 Pillow 实现，不引 OpenCV。
    """
    try:
        from io import BytesIO

        from PIL import Image, ImageFilter
    except ImportError as exc:  # pragma: no cover
        raise GeetestUnavailable("需要 Pillow 才能定位缺口") from exc

    def edges(data: bytes) -> Any:
        img = Image.open(BytesIO(data)).convert("L")
        return img.filter(ImageFilter.FIND_EDGES)

    piece = _trim_blank(edges(slice_bytes))
    background = edges(bg_bytes)
    pw, ph = piece.size
    if pw == 0 or ph == 0:
        raise GeetestError("滑块图是空的，无法定位缺口")

    pdata = list(piece.getdata())
    bdata = list(background.getdata())
    bw, bh = background.size

    best_x, best_score = 0, float("inf")
    # 只在左上角一段竖向范围里找：缺口总是位于背景上半部
    for x in range(search[0], min(search[1], bw - pw)):
        for y in range(0, max(1, bh - ph), 4):
            score = 0
            step = 3
            for py in range(0, ph, step):
                row = py * pw
                brow = (y + py) * bw + x
                for px in range(0, pw, step):
                    diff = pdata[row + px] - bdata[brow + px]
                    score += diff * diff
            if score < best_score:
                best_score, best_x = score, x
    return best_x


def _trim_blank(image: Any) -> Any:
    """裁掉滑块图四周的纯色空白，只留滑块本体。"""
    width, height = image.size
    data = image.load()
    min_x, min_y, max_x, max_y = width, height, 0, 0
    for y in range(height):
        for x in range(width):
            if data[x, y] > 24:
                min_x, max_x = min(min_x, x), max(max_x, x)
                min_y, max_y = min(min_y, y), max(max_y, y)
    if max_x <= min_x or max_y <= min_y:
        return image
    return image.crop((min_x, min_y, max_x + 1, max_y + 1))


# --------------------------------------------------------------------------
# 轨迹生成
# --------------------------------------------------------------------------


def build_trace(distance: int, *, rng: random.Random | None = None) -> list[list[int]]:
    """生成拟人滑动轨迹 ``[[x, y, t], ...]``（x=累计位移，t=毫秒）。

    缓动用 ease-out-expo：起步快、末段慢，符合人手。
    """
    random_ = rng or random
    if distance < 0:
        raise GeetestError("距离不能为负")

    def ease(sep: float) -> float:
        return 1.0 if sep >= 1 else 1 - pow(2, -10 * sep)

    trace: list[list[int]] = [
        [random_.randint(-50, -10), random_.randint(-50, -10), 0],
        [0, 0, 0],
    ]
    count = 30 + int(distance / 2)
    elapsed = random_.randint(50, 100)
    last_x = 0
    for index in range(count):
        x = round(ease(index / count) * distance)
        elapsed += random_.randint(10, 20)
        if x == last_x:
            continue
        trace.append([x, 0, elapsed])
        last_x = x
    trace.append(list(trace[-1]))
    return trace


# --------------------------------------------------------------------------
# Node 驱动
# --------------------------------------------------------------------------


def node_available() -> bool:
    return shutil.which("node") is not None


def call_driver(payload: dict[str, Any], *, timeout: float = 30.0) -> dict[str, Any]:
    """调 vendor/driver.js 算 ``w`` 或编码轨迹。"""
    if not node_available():
        raise GeetestUnavailable(
            "需要 Node 才能计算极验 w 参数（jiyan.js 是混淆过的，不翻译成 Python）。"
            "装一个 node 即可，登录之外的功能不受影响。"
        )
    if not DRIVER.is_file():
        raise GeetestUnavailable(f"缺少 {DRIVER}")

    proc = subprocess.run(  # noqa: S603 - 固定脚本路径，输入走 stdin
        ["node", str(DRIVER)],
        input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
    )
    if proc.returncode != 0:
        raise GeetestError(f"node 退出码 {proc.returncode}: {proc.stderr.decode()[:300]}")
    try:
        out = json.loads(proc.stdout.decode("utf-8") or "{}")
    except json.JSONDecodeError as exc:
        raise GeetestError(f"driver 输出不是 JSON: {proc.stdout[:200]!r}") from exc
    if not out.get("ok"):
        raise GeetestError(f"driver 报错: {out.get('error')}")
    return out


def encode_trace(trace: list[list[int]], s: str) -> str:
    return str(call_driver({"op": "encode_trace", "trace": trace, "s": s})["value"])


def compute_w(
    *,
    slide_length: int,
    trace: list[list[int]],
    s: str,
    challenge: str,
    passtime: int,
    imgload: int,
    gt: str,
) -> str:
    """算最终的 ``w``（AES(参数) 拼 RSA(AES 密钥)）。"""
    return str(call_driver({
        "op": "w",
        "slide_length": slide_length,
        "trace": trace,
        "s": s,
        "challenge": challenge,
        "passtime": passtime,
        "imgload": imgload,
        "gt": gt,
    })["value"])


# --------------------------------------------------------------------------
# 编排
# --------------------------------------------------------------------------


@dataclass(slots=True)
class SolveResult:
    ok: bool
    validate: str = ""
    seccode: str = ""
    distance: int = 0
    elapsed: float = 0.0
    message: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok, "validate": self.validate, "seccode": self.seccode,
            "distance": self.distance, "elapsed": round(self.elapsed, 2),
            "message": self.message,
        }


async def fetch_challenge(
    http: Any, *, gt: str, challenge: str,
    api_server: str = "api.geetest.com",
    referer: str = "https://www.geetest.com/",
) -> Challenge:
    """取一次挑战的素材（图片地址、``s``、挑战号）。

    ⚠️ **不要带 ``is_next=true``** —— 带了只返回主题元信息，没有 ``bg``/``slice``。
    正确顺序是先用 ``ajax.php`` 占位，再 ``get.php``（实测于 2026-10-19）。
    """
    stamp = int(time.time() * 1000)
    await http.get(
        f"https://{api_server}/ajax.php?gt={gt}&challenge={challenge}"
        f"&lang=zh-cn&pt=0&w=&callback=geetest_{stamp}",
        headers={"Referer": referer},
    )
    query = (
        f"gt={gt}&challenge={challenge}&lang=zh-cn&https=true"
        f"&protocol=https%3A%2F%2F&offline=false&product=embed"
        f"&api_server={api_server}&isPC=true&autoReset=true&width=100%25"
        f"&callback=geetest_{int(time.time() * 1000)}"
    )
    resp = await http.get(f"https://{api_server}/get.php?{query}",
                          headers={"Referer": referer})
    data = _parse_jsonp(resp.text())
    payload = data.get("data") if isinstance(data.get("data"), dict) else data
    if not payload.get("bg") or not payload.get("slice"):
        raise GeetestError(f"get.php 没返回图片素材：{resp.text()[:200]}")
    server = (payload.get("static_servers") or ["static.geetest.com"])[0]
    return Challenge(
        gt=gt,
        challenge=str(payload.get("challenge") or challenge),
        s=str(payload.get("s") or ""),
        bg_url=f"https://{server}/{payload['bg']}",
        fullbg_url=f"https://{server}/{payload.get('fullbg', '')}",
        slice_url=f"https://{server}/{payload['slice']}",
        raw=payload,
    )


async def solve(
    http: Any,
    *,
    challenge: Challenge,
    referer: str = "https://www.geetest.com/",
    verify_url: str = "https://api.geetest.com/ajax.php",
) -> SolveResult:
    """完整解一次：取图 → 还原背景 → 缺口 → 轨迹 → ``w`` → 提交。

    注册（拿 gt/challenge）由调用方负责 —— 各站点入口不同。
    """
    started = time.time()
    headers = {"Referer": referer}
    slice_bytes = (await http.get(challenge.slice_url, headers=headers)).body
    bg_bytes = (await http.get(challenge.bg_url, headers=headers)).body

    distance = find_gap(slice_bytes, unscramble(bg_bytes))
    # 与原实现一致：留 5px 容差
    slide_length = max(0, distance - 5)
    trace = build_trace(slide_length)
    w = compute_w(
        slide_length=slide_length, trace=trace, s=challenge.s,
        challenge=challenge.challenge, passtime=trace[-1][2],
        imgload=random.randint(100, 200), gt=challenge.gt,
    )

    query = (
        f"gt={challenge.gt}&challenge={challenge.challenge}"
        f"&lang=zh-cn&pt=0&w={w}&callback=geetest_{int(time.time() * 1000)}"
    )
    resp = await http.get(f"{verify_url}?{query}", headers=headers)
    data = _parse_jsonp(resp.text())
    ok = bool(data.get("success")) or str(data.get("status") or "") in ("success", "ok")
    result = SolveResult(
        ok=ok, distance=distance, elapsed=time.time() - started, raw=data,
        message="" if ok else str(data.get("message") or data.get("msg") or resp.text()[:120]),
    )
    if ok:
        result.validate = str(data.get("validate") or data.get("geetest_validate") or "")
        result.seccode = str(data.get("geetest_seccode") or "")
    return result


def _parse_jsonp(text: str) -> dict[str, Any]:
    body = (text or "").strip()
    if body.startswith("(") or body.endswith(")"):
        start, end = body.find("("), body.rfind(")")
        if start >= 0 and end > start:
            body = body[start + 1:end]
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        raise GeetestError(f"验证响应不是 JSON: {text[:200]!r}") from None
    return parsed if isinstance(parsed, dict) else {"raw": parsed}
