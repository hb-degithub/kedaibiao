"""平台只读动作注册表。

## 为什么要有这一层

各平台除框架契约（课程/作业/题目/提交）之外都有自己的只读能力，例如批改网的
「待办作业 / 历史作文 / 作文详情 / 作业号查询」。

如果没有这一层，控制台就得为每个平台写专属页面 —— 每接一个平台改一次前端。
改成让**后端把动作结果翻译成展示信封**（table / detail / message），前端只看
信封的 ``kind`` 渲染，于是新增平台 = 后端加一个注册函数，前端零改动。

## 分层

- 动作的**声明**（有哪些动作、要什么参数）在这里；
- 动作的**实现**调用适配器的公开方法，不碰适配器内部；
- 平台异常一律翻成 ``ConsoleError(kind="platform_error")``，
  让 HTTP 层统一成 502，而不是漏一个 500 堆栈给用户。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from kedaibiao.core.models import Assignment, TaskKind

log = logging.getLogger(__name__)

PLATFORM_ERROR = "platform_error"


@dataclass(frozen=True, slots=True)
class ActionParam:
    name: str
    label: str
    type: str = "string"  # string | int
    required: bool = False
    hint: str = ""

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": self.name,
            "label": self.label,
            "type": self.type,
            "required": self.required,
        }
        if self.hint:
            out["hint"] = self.hint
        return out


@dataclass(frozen=True, slots=True)
class ActionSpec:
    platform: str
    name: str
    label: str
    description: str = ""
    params: tuple[ActionParam, ...] = ()
    #: True = 框架契约里本来就有的通用能力（courses / assignments），它们有专属
    #: 功能页。控制台的「工具」页应过滤掉这些，避免与专属页重复。
    builtin: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "description": self.description,
            "params": [p.to_dict() for p in self.params],
            "builtin": self.builtin,
        }


class ActionInputError(ValueError):
    """动作参数不合法 —— 由服务层翻成 400 ``bad_request``。

    和 ``platform_error`` 区分开：那是「平台那边出问题」，这是「你给的东西不对」。
    """


#: 处理器签名：(适配器实例, 参数 dict) -> 展示信封
Handler = Callable[[Any, dict[str, Any]], Awaitable[dict[str, Any]]]

_SPECS: dict[str, dict[str, ActionSpec]] = {}
_HANDLERS: dict[tuple[str, str], Handler] = {}


def register(spec: ActionSpec, handler: Handler) -> None:
    """登记一个只读动作。同名重复登记会覆盖（便于热改）。"""
    _SPECS.setdefault(spec.platform, {})[spec.name] = spec
    _HANDLERS[(spec.platform, spec.name)] = handler


def register_platform(platform: str) -> list[ActionSpec]:
    """返回某平台已登记的动作（按登记顺序）。"""
    return list(_SPECS.get(platform, {}).values())


def has_action(platform: str, name: str) -> bool:
    return (platform, name) in _HANDLERS


def get_handler(platform: str, name: str) -> Handler:
    return _HANDLERS[(platform, name)]


# --------------------------------------------------------------------------
# 展示信封
# --------------------------------------------------------------------------


def column(key: str, label: str, mono: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {"key": key, "label": label}
    if mono:
        out["mono"] = True
    return out


def envelope(
    *,
    platform: str,
    action: str,
    label: str,
    kind: str,
    title: str = "",
    note: str = "",
    **payload: Any,
) -> dict[str, Any]:
    return {
        "platform": platform,
        "action": action,
        "label": label,
        "kind": kind,
        "title": title or label,
        "note": note,
        **payload,
    }


def table(
    *,
    platform: str,
    action: str,
    label: str,
    columns: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    title: str = "",
    note: str = "",
    empty_hint: str = "没有数据",
) -> dict[str, Any]:
    return envelope(
        platform=platform,
        action=action,
        label=label,
        kind="table",
        title=title or f"{label}（{len(rows)} 项）",
        note=note,
        columns=columns,
        rows=rows,
        empty_hint=empty_hint,
    )


def detail(
    *,
    platform: str,
    action: str,
    label: str,
    fields: list[dict[str, Any]],
    sections: list[dict[str, Any]],
    title: str = "",
    note: str = "",
) -> dict[str, Any]:
    return envelope(
        platform=platform,
        action=action,
        label=label,
        kind="detail",
        title=title or label,
        note=note,
        fields=fields,
        sections=sections,
    )


def message(
    *, platform: str, action: str, label: str, message: str, title: str = ""
) -> dict[str, Any]:
    return envelope(
        platform=platform,
        action=action,
        label=label,
        kind="message",
        title=title or label,
        message=message,
    )


def _field(label: str, value: Any) -> dict[str, Any]:
    return {"label": label, "value": "" if value is None else str(value)}


def _text_section(label: str, text: str) -> dict[str, Any]:
    return {"label": label, "kind": "text", "text": text or ""}


def _table_section(
    label: str, columns: list[dict[str, Any]], rows: list[dict[str, Any]]
) -> dict[str, Any]:
    return {"label": label, "kind": "table", "columns": columns, "rows": rows}


def _fmt_deadline(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    return str(value)


# --------------------------------------------------------------------------
# 批改网（pigai）
# --------------------------------------------------------------------------

_KIND_LABEL = {
    TaskKind.COURSE: "课程",
    TaskKind.HOMEWORK: "作业",
    TaskKind.EXAM: "考试",
    TaskKind.PRACTICE: "练习",
}


async def _pigai_pending(adapter: Any, params: dict[str, Any]) -> dict[str, Any]:
    rows: list[Assignment] = await adapter.list_pending_assignments()
    data = [
        {
            "rid": a.remote_id,
            "title": a.title,
            "kind": _KIND_LABEL.get(a.kind, a.kind.value),
            "deadline": _fmt_deadline(a.deadline),
            "url": a.url or "",
        }
        for a in rows
    ]
    return table(
        platform=getattr(adapter, "name", "pigai"),
        action="pending",
        label="待办作业",
        title=f"待办作业（{len(data)} 项）",
        columns=[
            column("rid", "作文号", mono=True),
            column("title", "题目"),
            column("kind", "类型"),
            column("deadline", "截止时间"),
        ],
        rows=data,
        empty_hint="当前没有待办作业。",
    )


async def _pigai_finished(adapter: Any, params: dict[str, Any]) -> dict[str, Any]:
    rows = await adapter.list_finished_essays()
    data = [
        {
            "rid": r.rid,
            "eid": r.eid,
            "title": r.title,
            "submitted_at": r.submitted_at,
            "deadline": r.deadline,
            "score": r.score_raw,
            "submit_count": r.submit_count,
        }
        for r in rows
    ]
    return table(
        platform=getattr(adapter, "name", "pigai"),
        action="finished",
        label="历史作文",
        title=f"历史作文（{len(data)} 篇）",
        columns=[
            column("rid", "作文号", mono=True),
            column("eid", "稿件号", mono=True),
            column("title", "题目"),
            column("submitted_at", "提交时间"),
            column("deadline", "截止时间"),
            column("score", "分数"),
            column("submit_count", "提交次数"),
        ],
        rows=data,
        empty_hint="还没有已提交的作文。",
    )


async def _pigai_essay(adapter: Any, params: dict[str, Any]) -> dict[str, Any]:
    rid = str(params["rid"])
    eid = str(params["eid"])
    item = await adapter.fetch_essay_detail(rid, eid)

    score_text = ""
    if item.score is not None:
        score_text = str(item.score)
        if item.full_marks is not None:
            score_text = f"{item.score} / {item.full_marks}"

    fields = [_field("总分", score_text or "—")]
    if item.topic:
        fields.append(_field("题目", item.topic))
    for key, value in (item.meta or {}).items():
        fields.append(_field(str(key), value))

    sections: list[dict[str, Any]] = []
    if item.requirements:
        sections.append(_text_section("题目要求", item.requirements))
    if item.dimensions:
        sections.append(
            _table_section(
                "分项评分",
                [column("name", "维度"), column("ratio", "得分率"), column("source", "来源")],
                [
                    {"name": d.name, "ratio": f"{d.ratio:.0%}", "source": d.source}
                    for d in item.dimensions
                ],
            )
        )
    if item.essay_text:
        sections.append(_text_section("正文", item.essay_text))
    if item.overall_comment:
        sections.append(_text_section("总评", item.overall_comment))
    if item.annotations:
        sections.append(
            _table_section(
                "逐句批注",
                [
                    column("sentence", "句子"),
                    column("level", "级别"),
                    column("cate", "类型"),
                    column("message", "批注"),
                ],
                [
                    {
                        "sentence": a.sentence,
                        "level": a.level,
                        "cate": a.cate,
                        "message": a.message,
                    }
                    for a in item.annotations
                ],
            )
        )

    return detail(
        platform=getattr(adapter, "name", "pigai"),
        action="essay",
        label="作文详情",
        title=f"作文详情 · {rid} / {eid}",
        note="正文与批注属于个人内容，只在本地查看。",
        fields=fields,
        sections=sections,
    )


async def _pigai_lookup(adapter: Any, params: dict[str, Any]) -> dict[str, Any]:
    rid = str(params["rid"])
    page = await adapter.fetch_assignment_by_rid(rid)

    fields = [
        _field("作文号", page.rid or rid),
        _field("稿件号", page.eid or "—"),
        _field("标题", page.title or "—"),
        _field("字数要求", page.word_range or "—"),
        _field("满分", page.full_marks if page.full_marks is not None else "—"),
        _field("截止时间", page.deadline or "—"),
        _field("服务器时间", page.server_time or "—"),
    ]

    note = ""
    if page.no_paste:
        note = "该写作页在前端禁用了粘贴。本工具不利用这一点绕过限制，只如实展示。"

    sections: list[dict[str, Any]] = []
    if page.requirement:
        sections.append(_text_section("题目要求", page.requirement))
    if page.essay_text:
        sections.append(_text_section("已有正文", page.essay_text))

    return detail(
        platform=getattr(adapter, "name", "pigai"),
        action="lookup",
        label="按作业号查询",
        title=f"写作页 · {page.rid or rid}",
        note=note,
        fields=fields,
        sections=sections,
    )


def _register_pigai() -> None:
    register(
        ActionSpec(
            platform="pigai",
            name="pending",
            label="待办作业",
            description="未完成 / 未截止的作文任务",
        ),
        _pigai_pending,
    )
    register(
        ActionSpec(
            platform="pigai",
            name="finished",
            label="历史作文",
            description="已提交的作文列表",
        ),
        _pigai_finished,
    )
    register(
        ActionSpec(
            platform="pigai",
            name="essay",
            label="作文详情",
            description="按作文号 + 稿件号取分数、分项、正文与逐句批注",
            params=(
                ActionParam("rid", "作文号", required=True, hint="例如 200000004"),
                ActionParam("eid", "稿件号", required=True, hint="例如 1000006"),
            ),
        ),
        _pigai_essay,
    )
    register(
        ActionSpec(
            platform="pigai",
            name="lookup",
            label="按作业号查询",
            description="用作文号直接打开写作页",
            params=(ActionParam("rid", "作文号", required=True),),
        ),
        _pigai_lookup,
    )


def load_builtin_actions() -> int:
    """登记所有内置动作。新增平台在这里加一行注册函数即可。"""
    _register_pigai()
    return len(_HANDLERS)


# --------------------------------------------------------------------------
# 通用动作：框架契约里本来就有的读能力，所有平台都有
# --------------------------------------------------------------------------


def _caps(adapter: Any) -> Any:
    return getattr(adapter, "capabilities", None)


async def _generic_courses(adapter: Any, params: dict[str, Any]) -> dict[str, Any]:
    name = getattr(adapter, "name", "?")
    caps = _caps(adapter)
    if caps is not None and not getattr(caps, "list_courses", False):
        raise ActionInputError(f"{name} 不支持读取课程列表")

    courses = await adapter.list_courses()
    rows = [
        {
            "id": c.remote_id,
            "name": c.name,
            "teacher": c.teacher or "—",
            "term": c.term or "—",
        }
        for c in courses
    ]
    return table(
        platform=name,
        action="courses",
        label="课程",
        title=f"课程列表（{len(rows)} 门）",
        columns=[
            column("id", "课程号", mono=True),
            column("name", "名称"),
            column("teacher", "教师"),
            column("term", "学期"),
        ],
        rows=rows,
        empty_hint="该平台没有返回任何课程。",
    )


async def _generic_assignments(adapter: Any, params: dict[str, Any]) -> dict[str, Any]:
    name = getattr(adapter, "name", "?")
    caps = _caps(adapter)
    for capability in ("list_courses", "list_assignments"):
        if caps is not None and not getattr(caps, capability, False):
            raise ActionInputError(f"{name} 不支持读取作业列表")

    courses = list(await adapter.list_courses())
    only = str(params.get("course") or "").strip()
    if only:
        courses = [c for c in courses if c.remote_id == only]
        if not courses:
            raise ActionInputError(f"没有找到课程号 {only!r}")

    rows: list[dict[str, Any]] = []
    for course in courses:
        for item in await adapter.list_assignments(course):
            rows.append(
                {
                    "course": course.name or course.remote_id,
                    "id": item.remote_id,
                    "title": item.title,
                    "kind": _KIND_LABEL.get(item.kind, item.kind.value),
                    "deadline": _fmt_deadline(item.deadline) or "—",
                    "state": "已完成" if item.finished else "未完成",
                }
            )

    scope = f"（课程 {only}）" if only else "（全部课程）"
    return table(
        platform=name,
        action="assignments",
        label="作业",
        title=f"作业 / 任务{scope} —— {len(rows)} 项",
        note="只读数据，不会提交任何内容。",
        columns=[
            column("course", "课程"),
            column("id", "任务号", mono=True),
            column("title", "名称"),
            column("kind", "类型"),
            column("deadline", "截止"),
            column("state", "状态"),
        ],
        rows=rows,
        empty_hint="这些课程下没有作业 / 任务。",
    )


def ensure_generic_actions(platform: str) -> None:
    """给某个平台补上通用动作。

    在 ``actions()`` / ``run_action()`` 里按需调用，而不是在
    ``load_builtin_actions()`` 里一次性遍历 —— 平台是自动发现的，
    注册时机可能晚于本模块的加载。
    """
    if not has_action(platform, "courses"):
        register(
            ActionSpec(
                platform=platform,
                name="courses",
                label="课程列表",
                description="该平台的课程 / 班级列表（只读，会真的访问平台）",
                builtin=True,
            ),
            _generic_courses,
        )
    if not has_action(platform, "assignments"):
        register(
            ActionSpec(
                platform=platform,
                name="assignments",
                label="作业列表",
                description="各课程下的作业 / 任务（只读，会真的访问平台）",
                builtin=True,
                params=(
                    ActionParam("course", "课程号", hint="留空 = 全部课程"),
                ),
            ),
            _generic_assignments,
        )
