"""超星滑块验证码求解的单测：只测缺口定位（不联网、不启浏览器）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from kedaibiao.platforms.chaoxing import captcha

FIXTURES = Path(__file__).parent / "fixtures" / "chaoxing"


def _images() -> tuple[bytes, bytes]:
    return (
        (FIXTURES / "slide_big.jpg").read_bytes(),
        (FIXTURES / "slide_small.jpg").read_bytes(),
    )


def test_find_gap_offset_golden() -> None:
    # 真实抓取的一对图：肉眼缺口在 x≈271，轮廓法必须落在同一位置
    big, small = _images()
    assert captcha.find_gap_offset(big, small) == 271


def test_find_gap_offset_is_stable() -> None:
    big, small = _images()
    assert captcha.find_gap_offset(big, small) == captcha.find_gap_offset(big, small)


def test_find_gap_offset_rejects_garbage() -> None:
    with pytest.raises(captcha.CaptchaError):
        captcha.find_gap_offset(b"not an image", b"neither")
