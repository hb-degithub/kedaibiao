"""学习通题面字体混淆解码（fontdec）的单测：全部离线。"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from kedaibiao.platforms.chaoxing import fontdec

FIXTURES = Path(__file__).parent / "fixtures" / "chaoxing"
WORK_PAGE = FIXTURES / "quiz_work_do_home.html"
FONT_FILE = FIXTURES / "cxsecret.ttf"


def _inline_page(font_bytes: bytes, text: str = "设厙示庲个事件") -> str:
    encoded = base64.b64encode(font_bytes).decode()
    head = (
        "<html><head><style>"
        "@font-face{font-family:'font-cxsecret';src:url("
        "'data:application/font-ttf;charset=utf-8;base64," + encoded + "')}"
        "</style></head>"
    )
    return head + "<body><div class='font-cxsecret'>" + text + "</div></body></html>"


def test_extract_font_from_real_page() -> None:
    html = WORK_PAGE.read_text(encoding="utf-8", errors="ignore")
    data = fontdec.extract_font(html)
    assert data is not None
    assert data[:4] == b"\x00\x01\x00\x00"  # TrueType 头


def test_extract_font_from_inline_and_absent() -> None:
    font_bytes = FONT_FILE.read_bytes()
    data = fontdec.extract_font(_inline_page(font_bytes))
    assert data == font_bytes
    assert fontdec.extract_font("<html><body>普通页面</body></html>") is None
    assert fontdec.extract_font("") is None


def test_deobfuscator_from_real_page() -> None:
    html = WORK_PAGE.read_text(encoding="utf-8", errors="ignore")
    dec = fontdec.Deobfuscator.from_html(html)
    assert dec
    assert len(dec.mapping) >= 30
    # 真实题面里的伪字串，解码后必须是通顺中文
    assert dec.decode("设厙示庲个事件") == "设表示三个事件"
    assert dec.decode("下庱厙庰庮正庬庯是") == "下列表述不正确的是"


def test_deobfuscator_handles_unknown_chars() -> None:
    html = WORK_PAGE.read_text(encoding="utf-8", errors="ignore")
    dec = fontdec.Deobfuscator.from_html(html)
    # 映射之外的字符原样保留（不能把正常汉字搞坏）
    assert dec.decode("ABC 123 正常汉字") == "ABC 123 正常汉字"
    assert dec.decode("") == ""


def test_empty_mapping_for_plain_page() -> None:
    dec = fontdec.Deobfuscator.from_html("<html><body>没有混淆字体</body></html>")
    assert not dec
    assert dec.decode("原文") == "原文"


def test_decode_helper() -> None:
    mapping = {"厙": "表", "庲": "三"}
    assert fontdec.decode("厙庲个事件", mapping) == "表三个事件"
    assert fontdec.decode("原样", {}) == "原样"


@pytest.mark.skipif(
    not Path(fontdec.REFERENCE_CANDIDATES[0]).is_file()
    and not Path("/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf").is_file(),
    reason="没有参考字体，跳过字形比对",
)
def test_build_map_matches_known_glyph() -> None:
    mapping = fontdec.build_map(FONT_FILE.read_bytes())
    assert len(mapping) >= 30
    assert mapping["厙"] == "表"
    assert mapping["庲"] == "三"
