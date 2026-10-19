"""极验 Geetest v3 滑块求解（仅用于用户本人账号的自动化登录）。

- :mod:`v3` —— 取图 / 还原背景 / 缺口 / 轨迹 / w / 提交
- ``vendor/`` —— MIT 许可的 jiyan.js（原样 vendor）+ 本项目的 Node 驱动
- 版权与许可见 ``vendor/NOTICE``
"""

from kedaibiao.platforms.unipus.geetest.browser import (
    CaptchaSolution,
    CaptchaSolutionError,
    solve_login_captcha,
    solution_from_capture,
    trigger_captcha,
)
from kedaibiao.platforms.unipus.geetest.v3 import (
    Challenge,
    GeetestError,
    GeetestUnavailable,
    SolveResult,
    build_trace,
    call_driver,
    compute_w,
    fetch_challenge,
    find_gap,
    node_available,
    solve,
    unscramble,
)

__all__ = [
    "CaptchaSolution",
    "CaptchaSolutionError",
    "Challenge", "GeetestError", "GeetestUnavailable", "SolveResult",
    "build_trace", "call_driver", "compute_w", "fetch_challenge", "find_gap",
    "node_available",
    "solve", "solution_from_capture", "solve_login_captcha",
    "trigger_captcha", "unscramble",
]
