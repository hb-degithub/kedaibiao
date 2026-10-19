"""iTEST 智能测评云平台（外研社 Unipus）。

只读链路：登录 → 考试列表 → 换答题窗口令牌。
不取卷、不交卷 —— 见 :mod:`~kedaibiao.platforms.itest.adapter` 里的能力边界。
"""

from kedaibiao.platforms.itest.adapter import ItestAdapter
from kedaibiao.platforms.itest.client import (
    ExamEntry,
    ItestAuthError,
    ItestClient,
    ItestError,
)

__all__ = ["ExamEntry", "ItestAdapter", "ItestAuthError", "ItestClient", "ItestError"]
