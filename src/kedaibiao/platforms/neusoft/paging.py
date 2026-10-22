"""东软列表接口的分页规则。

课程 / 学习任务 / 考勤三个列表都是 App 分页接口（``pageNo`` + ``pageSize``），
但**返回体里带不带 ``total`` 只有考勤是 2026-10-22 只读探测确认过的**；
课程与学习任务按同一套 App 分页风格处理，取不到 ``total`` 就退化成
「短页终止」。

三条终止条件（任何一条成立就停）：

1. **本页没有新条目** —— 服务端可能忽略 ``pageNo``、永远返回同一页，
   继续翻只会死循环，这里提前停并告警；
2. 已取条目覆盖 ``total``；
3. 本页条数小于 ``pageSize``（最后一页）。

另有 :data:`MAX_PAGES` 兜底：服务端一直返回满页、又不认 ``pageNo``、
``total`` 也没有时，不至于无限请求。上限到达会告警，明确告诉调用方
「后面的没读完」，不静默截断。
"""

from __future__ import annotations

from typing import Any

#: 单次翻页上限。正常课程 / 任务 / 考勤都远小于这个数，超了说明协议行为异常。
MAX_PAGES = 50

#: 课程分页：档案 §3 实测 ``pageSize=100``（App 端默认值）。
COURSE_PAGE_SIZE = 100

#: 学习任务分页：档案 §5.2 实测 ``pageSize=200``。
TASK_PAGE_SIZE = 200

#: 考勤分页：接口名里带 ``Page``，但档案里没有实测过分页参数；
#: 先按 100 试，服务端不认就退化成「只读第一页」（与改造前行为一致）。
ATTENDANCE_PAGE_SIZE = 100


def total_of(payload: Any) -> int | None:
    """从响应体里取总数；取不到返回 ``None``（**不猜 0**）。"""
    if not isinstance(payload, dict):
        return None
    for key in ("total", "totalCount"):
        value = payload.get(key)
        if isinstance(value, bool):
            continue
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number >= 0:
            return number
    return None


def should_continue(
    *, collected: int, page_count: int, total: int | None, page_size: int
) -> bool:
    """这一页之后还要不要继续翻。

    ``collected`` / ``page_count`` 都是**原始行数**（不是解析后的条数）：
    解析会按业务规则丢行（课程缺 ID、任务只留作业/测验），而 ``total`` 是
    平台按原始行算的 —— 拿解析结果去比 ``total`` 会永远差一截，然后多打一堆
    重复请求。
    """
    if page_count <= 0:
        return False
    if total is not None:
        return collected < total
    return page_count >= page_size


def reached_page_limit(page_no: int) -> bool:
    return page_no >= MAX_PAGES
