"""学习通题面字体混淆（``font-cxsecret``）的解码。

## 问题长什么样

作业 / 章节测验 / 考试的题面 HTML 里，部分汉字被替换成一串**伪字符**
（``厙厠厦厩庬…``），同时页面内联一个子集字体：

```html
<style>@font-face{font-family:'font-cxsecret';
  src:url('data:application/font-ttf;charset=utf-8;base64,AAEAAAAMAIAAAwBA…')}</style>
```

伪码位 → 字形的映射就写在字体的 ``cmap`` 里，而字形的**轮廓是真题面用的真字**
（思源黑体）。所以 ``innerText`` 读到的是 ``设厙示庲个事件``，人眼看页面却是
``设表示两个事件`` —— 直接拿文本去问大模型，问到的是一堆生僻字。

## 解法：字形比对，不靠 OCR、不联网

1. 从页面里把内联 base64 TTF 抠出来（:func:`extract_font`）；
2. ``fontTools`` 读 ``cmap`` 拿到「伪码位 → 字形」，``Pillow`` 把每个字形渲染成位图；
3. 用参考字体（思源黑体 = ``Noto Sans CJK SC``）把 GB2312 常用字（6763 个）也渲染一遍，
   逐字比对：位图先按**墨迹包围盒**裁切再缩放到同一网格，抵消字号/基线差异；
4. 距离最近且足够小（默认 ``<= 0.12``）的候选即真字。

2026-10-21 对真实章节测验页实测：34 个伪字全部命中，最佳距离 0.02~0.07，
次优候选通常 > 0.2，判别度足够。

## 边界

- 只处理「内联 data URI 字体」这种形态；页面改成外链字体时 ``extract_font`` 返回
  ``None``，调用方应当把「题目文本不可信」如实报出来，不要静默当成正常文本；
- ``fontTools`` / ``Pillow`` 是可选依赖（``pip install 'kedaibiao[chaoxing]'``），
  没装时抛 :class:`FontDecodeError` 并说明装法，核心包仍然可以导入；
- 本模块不判断「哪些文本被混淆」—— 映射之外的字符原样返回，所以整段 decode 是安全的。
"""

from __future__ import annotations

import base64
import hashlib
import io
import logging
import re
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: 页面里内联字体的 data URI。超星用 ``application/font-ttf``，也见过 ``font/ttf``。
_FONT_RE = re.compile(
    r"data:application/(?:font-ttf|x-font-ttf|font-sfnt|octet-stream);charset=utf-8;base64,"
    r"([A-Za-z0-9+/=\s]+)",
)
_FONT_RE_LOOSE = re.compile(
    r"data:(?:application|font)/(?:[a-z-]*ttf|sfnt|octet-stream)[^,]*;base64,([A-Za-z0-9+/=\s]+)",
)

#: 参考字体候选（按优先级）。Linux 常见路径；也可用环境变量覆盖。
REFERENCE_CANDIDATES: tuple[str, ...] = (
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
    "/usr/share/fonts/truetype/arphic/uming.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simsun.ttc",
)

#: 参考字体环境变量
REFERENCE_ENV = "KEDAIBIAO_CHAXING_FONT"

#: 渲染边长（像素）：字形先画到这么大的画布上再裁切
_RENDER_SIZE = 128
#: 比对网格
_FEATURE_SIZE = 40
#: 最佳距离超过它就不认（宁可漏字，不可错字）
MAX_DISTANCE = 0.12


class FontDecodeError(RuntimeError):
    """字体解码不可用：缺依赖、缺参考字体，或字体本身解析失败。"""


def _require(name: str) -> Any:
    """惰性导入可选依赖；点号模块用 importlib 才能拿到子模块本身。"""
    import importlib  # noqa: PLC0415

    try:
        return importlib.import_module(name)
    except ImportError as exc:  # pragma: no cover - 取决于运行环境
        raise FontDecodeError(
            f"字体解码需要 {name}，请执行: pip install 'kedaibiao[chaoxing]'"
        ) from exc


def extract_font(html: str) -> bytes | None:
    """从题目页 HTML 里抠出内联的混淆字体；没有就返回 ``None``。

    只认 base64 data URI：超星把子集字体直接内联在 ``<style>`` 里，不额外发请求。
    先按 ``charset=utf-8`` 的严格写法匹配，落空再放宽到 ``font/ttf`` 一类。
    """
    for pattern in (_FONT_RE, _FONT_RE_LOOSE):
        match = pattern.search(html or "")
        if match:
            raw = re.sub(r"\s+", "", match.group(1))
            try:
                return base64.b64decode(raw)
            except (ValueError, TypeError):  # pragma: no cover - 页面残缺
                continue
    return None


def _font_has_glyphs(font_bytes: bytes) -> bool:
    _require("fontTools")
    from fontTools.ttLib import TTFont  # noqa: PLC0415

    try:
        font = TTFont(io.BytesIO(font_bytes), lazy=True)
        return bool(font.getBestCmap())
    except Exception as exc:  # noqa: BLE001 - 各种解析失败都按「没字形」处理
        log.debug("字体解析失败: %s", exc)
        return False


def find_reference_font(explicit: str | Path | None = None) -> Path:
    """找参考字体（思源黑体一系）。找不到就抛错 —— 用别的字体比对会错字。"""
    import os  # noqa: PLC0415

    candidates: list[str] = []
    if explicit:
        candidates.append(str(explicit))
    env = os.environ.get(REFERENCE_ENV)
    if env:
        candidates.append(env)
    candidates.extend(REFERENCE_CANDIDATES)
    for item in candidates:
        path = Path(item)
        if path.is_file():
            return path
    raise FontDecodeError(
        "找不到参考字体（思源黑体 / Noto Sans CJK SC）。"
        "请安装 fonts-noto-cjk，或用 " + REFERENCE_ENV + " 指定一个 ttf/ttc 路径。"
    )


def _open_font(path: Path, *, index: int = 0) -> Any:
    """打开参考字体；``.ttc`` 里挑简体中文那一面。"""
    pillow = _require("PIL.ImageFont")
    if path.suffix.lower() == ".ttc":
        # Noto 的 TTC 里 JP/KR/SC/TC 四面共存，SC 通常在第 3 面。
        for idx in (index, 2, 0, 1, 3):
            try:
                font = pillow.truetype(str(path), _RENDER_SIZE, index=idx)
            except Exception:  # noqa: BLE001 - 越界就换下一面
                continue
            name = " ".join(getattr(font, "getname", lambda: ("", ""))())
            if idx == index or "SC" in name or "CN" in name:
                return font
        raise FontDecodeError(f"字体里找不到可用的简体中文面: {path}")
    return pillow.truetype(str(path), _RENDER_SIZE)


def _feature(font: Any, char: str, *, image_module: Any, image_draw: Any, array: Any) -> Any:
    """把一个字形渲染成「裁到墨迹包围盒、缩放到固定网格」的特征位图。"""
    size = _RENDER_SIZE
    canvas = image_module.new("L", (size * 2, size * 2), 0)
    image_draw.Draw(canvas).text((size // 2, size // 2), char, fill=255, font=font)
    bbox = canvas.getbbox()
    if not bbox:
        return array.zeros((_FEATURE_SIZE, _FEATURE_SIZE), dtype="uint8")
    cropped = canvas.crop(bbox).resize((_FEATURE_SIZE, _FEATURE_SIZE), image_module.BILINEAR)
    return array.asarray(cropped, dtype="uint8")


def _gb2312_charset() -> list[str]:
    """GB2312 一级+二级汉字（6763 个）—— 覆盖日常题面的绝大多数字。"""
    chars: list[str] = []
    for high in range(0xB0, 0xF8):
        for low in range(0xA1, 0xFF):
            try:
                chars.append(bytes((high, low)).decode("gb2312"))
            except UnicodeDecodeError:
                continue
    return chars


def _reference_features(path: Path) -> tuple[Any, list[tuple[str, Any]]]:
    """渲染参考字体里 GB2312 的每个字，返回 (字体对象, [(字, 特征)])。"""
    image_module = _require("PIL.Image")
    image_draw = _require("PIL.ImageDraw")
    array = _require("numpy")
    font = _open_font(path)
    features = []
    for char in _gb2312_charset():
        try:
            features.append((char, _feature(font, char, image_module=image_module,
                                            image_draw=image_draw, array=array)))
        except Exception:  # noqa: BLE001 - 个别字渲染失败就跳过
            continue
    return font, features


class Deobfuscator:
    """一次解码上下文：字体 → 映射，缓存起来给同一页面的多段文本复用。

    典型用法::

        dec = Deobfuscator.from_html(题面_html)
        print(dec.decode("设厙示庲个事件"))   # 设表示两个事件
    """

    def __init__(self, mapping: dict[str, str], *, font_sha256: str = "",
                 unmatched: tuple[str, ...] = ()) -> None:
        self.mapping = dict(mapping)
        #: 字体指纹，方便日志里区分「同一份混淆字体」
        self.font_sha256 = font_sha256
        #: 没认出来的伪字（调用方应当据此降低答案置信度，而不是当正常文本）
        self.unmatched = tuple(unmatched)

    def __bool__(self) -> bool:
        return bool(self.mapping)

    @classmethod
    def from_html(cls, html: str, *, reference: str | Path | None = None,
                  max_distance: float = MAX_DISTANCE) -> Deobfuscator:
        """从题目页 HTML 建解码器；页面里没有内联字体时返回空映射（不抛错）。"""
        font_bytes = extract_font(html)
        if not font_bytes:
            log.debug("页面里没有内联混淆字体，按明文处理")
            return cls({})
        return cls.from_font(font_bytes, reference=reference, max_distance=max_distance)

    @classmethod
    def from_font(cls, font_bytes: bytes, *, reference: str | Path | None = None,
                  max_distance: float = MAX_DISTANCE) -> Deobfuscator:
        if not _font_has_glyphs(font_bytes):
            return cls({})
        route = find_reference_font(reference)
        return cls(build_map(font_bytes, reference=route, max_distance=max_distance),
                   font_sha256=hashlib.sha256(font_bytes).hexdigest())

    def decode(self, text: str) -> str:
        """把伪字符换成真字；映射之外的字符原样保留。"""
        if not self.mapping or not text:
            return text or ""
        return "".join(self.mapping.get(char, char) for char in text)


def build_map(font_bytes: bytes, *, reference: str | Path | None = None,
              max_distance: float = MAX_DISTANCE) -> dict[str, str]:
    """建立「伪字符 → 真字」映射；距离超阈值的伪字不收录。

    参考字体只渲染一次（6763 个常用字），同一进程里多页复用：超星的混淆字体
    每页重新生成，但参考侧不变。
    """
    from fontTools.ttLib import TTFont  # noqa: PLC0415

    image_module = _require("PIL.Image")
    image_draw = _require("PIL.ImageDraw")
    array = _require("numpy")

    try:
        font = TTFont(io.BytesIO(font_bytes), lazy=True)
        cmap = font.getBestCmap()
    except Exception as exc:  # noqa: BLE001
        raise FontDecodeError(f"混淆字体解析失败: {exc}") from exc
    if not cmap:
        return {}

    # 同一份字体只解码一次：按 sha256 缓存映射
    digest = hashlib.sha256(font_bytes).hexdigest()
    cached = _MAP_CACHE.get(digest)
    if cached is not None:
        return dict(cached)

    ref_path = find_reference_font(reference)
    ref_font, ref_features = _reference_features(ref_path)
    ob_font = _font_from_bytes(font_bytes)

    mapping: dict[str, str] = {}
    for codepoint in cmap:
        pseudo = chr(codepoint)
        try:
            probe = _feature(ob_font, pseudo, image_module=image_module,
                             image_draw=image_draw, array=array)
        except Exception:  # noqa: BLE001
            continue
        best_char, best_distance = "", max_distance + 1.0
        for char, feature in ref_features:
            # 位图是 0~255，距离归一化到 0~1 才好和阈值比较
            distance = float(
                array.abs(probe.astype("int16") - feature.astype("int16")).mean()
            ) / 255.0
            if distance < best_distance:
                best_char, best_distance = char, distance
        if best_char and best_distance <= max_distance:
            mapping[pseudo] = best_char
        else:
            log.debug("伪字 %r 未命中（最近 %r 距离 %.3f）", pseudo, best_char, best_distance)

    _MAP_CACHE[digest] = dict(mapping)
    return mapping


def _font_from_bytes(font_bytes: bytes) -> Any:
    """把内存里的字体字节交给 Pillow（它只收路径或文件对象）。"""
    pillow = _require("PIL.ImageFont")
    return pillow.truetype(io.BytesIO(font_bytes), _RENDER_SIZE)


#: 进程级映射缓存：字体 sha256 → 映射
_MAP_CACHE: dict[str, dict[str, str]] = {}


def decode(text: str, mapping: dict[str, str]) -> str:
    """便捷函数：按给定映射解码一段文本。"""
    if not mapping or not text:
        return text or ""
    return "".join(mapping.get(char, char) for char in text)
