"""课程列表与课程资源列表。

## 为什么「课程 → 资源」放同一个模块

课程分页返回的 ``teachClassId`` 就是资源接口要的 ``currentClassId``（档案 §3，
两个接口叫法不同，值相同）。它是刷课的唯一上下文入口：资源接口必须带
``teachCourseId`` + ``currentClassId`` 这一对，否则 403。把两者放在一处，
刷课模块就只面对 :class:`~kedaibiao.platforms.neusoft.models.Resource`，
不必再操心课程上下文从哪来。

## 为什么解析与 IO 分开

响应形状没法靠离线测试凭空造出来 —— 但「给一段 payload，产出哪些对象」必须
能单独断言。所以 ``parse_*`` 一律是纯函数，``list_*`` 只负责发请求与递归。

## 两条取舍

1. 缺 ``teachCourseId`` / ``teachClassId`` 的课程**直接丢**：留着它也进不了
   资源接口，只会在刷课时炸在 403 上；
2. 目录递归用 ``seen`` 集合防环：``folderList`` 的 ``id`` 会被当作下一层的
   ``parentId``，目录数据一旦出现自环就会无限请求（客户端不重试，但这里死循环）。
"""

from __future__ import annotations

import logging
from typing import Any

from kedaibiao.platforms.neusoft import endpoints as ep
from kedaibiao.platforms.neusoft.client import NeusoftClient
from kedaibiao.platforms.neusoft.models import (
    NeusoftCourse,
    Resource,
    as_int,
    as_str,
)

log = logging.getLogger(__name__)

#: 课程分页参数。实测一次 100 条足够覆盖单个学期（档案 §3 给的示例就是 1/100），
#: 这里不翻页：多数学期课程数远小于 100，翻页只会多打请求。
COURSE_PAGE_NO = 1
COURSE_PAGE_SIZE = 100

#: 资源列表根层的 ``parentId``，[A]（档案 §3 的示例 body 就是这个值）。
ROOT_PARENT_ID = "0"

#: 课程名 / 教师名在这份响应里的字段名**没有实测样本**（档案 §3 只记了
#: ``teachCourseId`` / ``teachClassId``），按同平台其它列表的命名习惯给候选，
#: 取不到就留空 —— 名字缺失不影响刷课，但不能把它当成事实。
_COURSE_NAME_KEYS = ("teachCourseName", "courseName", "name")
_COURSE_TEACHER_KEYS = ("teacherName", "teacherNames", "teacher")

#: 资源名字段候选：``resName`` 有证据（档案 §5 的学习任务行里就是它），其余是兜底。
_RES_NAME_KEYS = ("resName", "name", "title")


# --------------------------------------------------------------------------
# 解析（纯函数）
# --------------------------------------------------------------------------


def _rows(payload: Any, key: str) -> list[Any]:
    """从已剥外壳的 ``data`` 里取列表。

    认两种形状：直接是数组（本平台有的下拉接口这样）；或 ``{key: [...]}``。
    都不认识就返回空列表，由调用方决定空意味着什么 —— 这里不抛，因为
    「这门课没有资源」和「字段名变了」在刷课层面处理方式相同（跳过）。
    """
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        value = payload.get(key)
        if isinstance(value, list):
            return value
    return []


def _first_str(row: dict[str, Any], keys: tuple[str, ...]) -> str:
    """按声明顺序取第一个非空字符串字段。"""
    for key in keys:
        text = as_str(row.get(key))
        if text:
            return text
    return ""


def parse_courses(payload: Any, *, term_id: str = "") -> list[NeusoftCourse]:
    """解析课程分页的 ``data``（``data.list[]``）。

    ``term_id`` 是入参而非响应字段：课程行里有没有它未经证实，而调用方本来
    就知道自己按哪个学期查的 —— 直接用调用方的值，比猜字段名可靠。

    缺 ``teachCourseId`` 或 ``teachClassId`` 的行会被丢掉并告警：两者缺一，
    资源接口就调不通，早点丢掉比后面 403 好排查。
    """
    courses: list[NeusoftCourse] = []
    for row in _rows(payload, "list"):
        if not isinstance(row, dict):
            continue
        teach_course_id = as_str(row.get("teachCourseId"))
        teach_class_id = as_str(row.get("teachClassId"))
        if not teach_course_id or not teach_class_id:
            log.warning(
                "东软课程行缺少 teachCourseId/teachClassId，已跳过（字段：%s）",
                sorted(row)[:8],
            )
            continue
        courses.append(
            NeusoftCourse(
                teach_course_id=teach_course_id,
                teach_class_id=teach_class_id,
                name=_first_str(row, _COURSE_NAME_KEYS),
                term_id=term_id or as_str(row.get("termId")),
                teacher=_first_str(row, _COURSE_TEACHER_KEYS),
            )
        )
    return courses


def parse_resources(payload: Any, *, parent_id: str = ROOT_PARENT_ID) -> list[Resource]:
    """解析一层资源列表的 ``data.resList``。

    ``parent_id`` 由调用方传本层的 ``parentId`` —— 响应里的资源行不带层级信息，
    刷课时真正要用的是 ``res_id``，这里留下 ``parent_id`` 只为排查目录层级。

    缺 ``resId`` 的行丢掉并告警：没有 ID 就无法回读学习记录，也无法上报。
    """
    resources: list[Resource] = []
    for row in _rows(payload, "resList"):
        if not isinstance(row, dict):
            continue
        res_id = as_str(row.get("resId") or row.get("id"))
        if not res_id:
            log.warning("东软资源行缺少 resId，已跳过（字段：%s）", sorted(row)[:8])
            continue
        resources.append(
            Resource(
                res_id=res_id,
                name=_first_str(row, _RES_NAME_KEYS),
                res_type=as_int(row.get("resType")),
                study_status=as_int(row.get("studyStatus")),
                parent_id=parent_id,
                raw=row,
            )
        )
    return resources


def parse_folders(payload: Any) -> list[str]:
    """解析一层目录的 ``data.folderList``，只取 ``id``。

    子目录的请求体里，本层 ``id`` 会原样作为下一层的 ``parentId``（档案 §3）。
    没有 ``id`` 的目录行没法继续下钻，丢掉并告警。
    """
    folders: list[str] = []
    for row in _rows(payload, "folderList"):
        if not isinstance(row, dict):
            continue
        folder_id = as_str(row.get("id") or row.get("folderId"))
        if not folder_id:
            log.warning("东软目录行缺少 id，已跳过（字段：%s）", sorted(row)[:8])
            continue
        folders.append(folder_id)
    return folders


def build_resource_body(course: NeusoftCourse, parent_id: str) -> dict[str, Any]:
    """资源列表请求体。

    为什么空值也要显式写出来：``resType`` / ``studyStatus`` / ``isAssess`` 是筛选项，
    空串表示「不筛」。整段省略和给空串在服务端是不是同一语义没有证据，而实测
    跑通的形态就是全带 —— 照实测写，省得踩「少了字段就只回一部分资源」。
    """
    return {
        "teachCourseId": course.teach_course_id,
        "currentClassId": course.current_class_id,
        "name": "",
        "resType": "",
        "studyStatus": "",
        "sortType": 1,
        "isAssess": "",
        "parentId": parent_id,
    }


# --------------------------------------------------------------------------
# IO
# --------------------------------------------------------------------------


async def list_courses(client: NeusoftClient, term_id: str) -> list[NeusoftCourse]:
    """列一个学期下的课程。

    参数按档案 §3 的实测值固定：``pageNo=1`` / ``pageSize=100``。
    """
    payload = await client.request(
        "GET",
        ep.COURSE_PAGE,
        params={
            "termId": term_id,
            "pageNo": COURSE_PAGE_NO,
            "pageSize": COURSE_PAGE_SIZE,
        },
    )
    return parse_courses(payload, term_id=term_id)


async def list_resources(client: NeusoftClient, course: NeusoftCourse) -> list[Resource]:
    """列一门课的全部资源，按目录层级递归。

    广度优先逐层请求：根层 ``parentId="0"``，拿到的 ``folderList`` 里每个 ``id``
    作为下一层的 ``parentId``。``seen_folders`` 从根值开始预置，任何重复出现
    的目录 id（包括自环 ``folderList`` 里挂自己）都被跳过并告警 —— 不跳过就是
    无限请求。``seen_resources`` 顺带去重，防止同一资源被多个目录引用时重复计入。
    """
    resources: list[Resource] = []
    seen_resources: set[str] = set()
    seen_folders: set[str] = {ROOT_PARENT_ID}
    pending: list[str] = [ROOT_PARENT_ID]

    while pending:
        parent_id = pending.pop(0)
        payload = await client.request(
            "POST", ep.RESOURCE_LIST, body=build_resource_body(course, parent_id)
        )
        for resource in parse_resources(payload, parent_id=parent_id):
            if resource.res_id in seen_resources:
                continue
            seen_resources.add(resource.res_id)
            resources.append(resource)
        for folder_id in parse_folders(payload):
            if folder_id in seen_folders:
                log.warning(
                    "资源目录出现重复/环路，已跳过：folderId=%s parentId=%s",
                    folder_id,
                    parent_id,
                )
                continue
            seen_folders.add(folder_id)
            pending.append(folder_id)
    return resources
