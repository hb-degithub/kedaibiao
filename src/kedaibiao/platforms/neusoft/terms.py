"""学期列表。

学期 ID 是作业/考勤/课程三套接口的共同入口：课程按学期分页、学习任务按学期
分页、考勤按学期分页。所以这个模块虽然只有一个函数，却是所有业务的起点。
"""

from __future__ import annotations

from typing import Any

from kedaibiao.platforms.neusoft import endpoints as ep
from kedaibiao.platforms.neusoft.client import NeusoftClient
from kedaibiao.platforms.neusoft.models import Term, as_list, as_str


def parse_terms(payload: Any) -> list[Term]:
    """解析学期下拉。

    平台两种形状都出现过：直接给数组，或包一层 ``{list: [...]}`` —— 两种都收，
    否则换一个学期就整批空。
    """
    rows: list[Any]
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        rows = as_list(payload.get("list"))
    else:
        rows = []
    terms: list[Term] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        term_id = as_str(row.get("id") or row.get("termId"))
        if not term_id:
            continue
        terms.append(Term(id=term_id, name=as_str(row.get("name") or row.get("termName"))))
    return terms


async def list_terms(client: NeusoftClient) -> list[Term]:
    """拉全部学期，按平台返回顺序（通常新→旧）。"""
    payload = await client.request("GET", ep.TERM_DROPDOWN)
    return parse_terms(payload)
