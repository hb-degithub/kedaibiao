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

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from kedaibiao.core.models import Assignment, QuestionType, TaskKind
from kedaibiao.platforms.base import NotSupportedError

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
    #: True = 这个动作会改平台侧状态（交卷、填答案、开考计时）。控制台据此把
    #: 「只读」徽标换成「写操作」，避免用户把它当查询点。
    mutates: bool = False
    #: True = 这个动作**不需要连平台**（只读本地缓存 / 调外部模型）。
    #: 动作层据此跳过登录：否则会话一过期，「AI 答题」这种纯本地动作也会报登录失败。
    local: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "description": self.description,
            "params": [p.to_dict() for p in self.params],
            "builtin": self.builtin,
            "mutates": self.mutates,
            "local": self.local,
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
                ActionParam("rid", "作文号", required=True, hint="例如 200000001"),
                ActionParam("eid", "稿件号", required=True, hint="例如 1000002"),
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




# --------------------------------------------------------------------------
# iTEST（考试平台）
# --------------------------------------------------------------------------


async def _itest_exams(adapter: Any, params: dict[str, Any]) -> dict[str, Any]:
    """考试列表（比通用 assignments 多带起止/时长/考试码）。"""
    name = getattr(adapter, "name", "itest")
    exams = await adapter.client().list_exams()
    rows = [
        {
            "exam_id": e.exam_id,
            "title": e.title or "—",
            "status": "已交卷" if e.finished else "未完成",
            "start": e.start_at or "—",
            "end": e.end_at or "—",
            "duration": e.duration or "—",
        }
        for e in exams
    ]
    return table(
        platform=name,
        action="exams",
        label="考试",
        title=f"考试列表（{len(rows)} 场）",
        note="只读。取卷需要进答题窗口（会开始计时），本工具不代为进入。",
        columns=[
            column("exam_id", "考试号", mono=True),
            column("title", "名称"),
            column("status", "状态"),
            column("start", "开始"),
            column("end", "结束"),
            column("duration", "时长"),
        ],
        rows=rows,
        empty_hint="当前没有可参加的考试。",
    )


async def _itest_entry(adapter: Any, params: dict[str, Any]) -> dict[str, Any]:
    """换答题窗口 URL。

    这个调用**只签发令牌，不会开始考试**（调研已核验）。给出 URL 由人决定
    要不要打开 —— 一旦打开就开始计时，属于不可逆操作。
    """
    name = getattr(adapter, "name", "itest")
    exam_id = str(params.get("exam_id") or "").strip()
    if not exam_id:
        raise ActionInputError("缺少考试号（exam_id）")
    entry = await adapter.exam_meta(exam_id)
    url = await adapter.answer_window_url(exam_id, entry.code if entry else "")
    return detail(
        platform=name,
        action="entry",
        label="答题入口",
        title=f"答题窗口 · {entry.title if entry else exam_id}",
        note=(
            "这个链接**只签发令牌，不会开始考试**。打开它并点「开始考试」后开始计时，"
            "属于不可逆操作 —— 本工具只提供入口，不替你进入。"
        ),
        fields=[
            {"label": "考试号", "value": exam_id},
            {"label": "名称", "value": entry.title if entry else "—"},
            {"label": "开始", "value": entry.start_at if entry else "—"},
            {"label": "结束", "value": entry.end_at if entry else "—"},
            {"label": "时长", "value": entry.duration if entry else "—"},
        ],
        sections=[
            {"label": "答题窗口 URL（点击打开前请确认）", "kind": "text", "text": url},
        ],
    )


def _register_itest() -> None:
    register(
        ActionSpec(
            platform="itest",
            name="exams",
            label="考试列表",
            description="当前账号可参加的考试（只读）",
        ),
        _itest_exams,
    )
    register(
        ActionSpec(
            platform="itest",
            name="entry",
            label="答题入口",
            description="换答题窗口 URL —— 只签发令牌，不会开始考试",
            params=(ActionParam("exam_id", "考试号", required=True),),
        ),
        _itest_entry,
    )
    register(
        ActionSpec(
            platform="itest",
            name="paper",
            label="进考场取卷",
            description="进答题页把试卷读出来（★ 会开始计时，不可逆；只读题目，不改答案）",
            params=(ActionParam("exam_id", "考试号", required=True),),
            mutates=True,
        ),
        _itest_paper,
    )
    register(
        ActionSpec(
            platform="itest",
            name="answers",
            label="AI 答题",
            description="用大模型按缓存的试卷生成答案单（填空+翻译；听力不代答）",
            params=(ActionParam("exam_id", "考试号", required=True),),
            local=True,
        ),
        _itest_answers,
    )
    register(
        ActionSpec(
            platform="itest",
            name="fill",
            label="填答案",
            description="按答案单填进作答页并逐项核验（不进服务端；不会交卷）",
            params=(
                ActionParam("exam_id", "考试号", required=True),
                ActionParam(
                    "answers", "答案单（JSON，可留空用 AI 答题的缓存）", type="text",
                    hint='{"qid": "字母串或整句译文"}；留空则读 data/itest/answers-<考试号>.json',
                ),
            ),
            mutates=True,
        ),
        _itest_fill,
    )
    register(
        ActionSpec(
            platform="itest",
            name="submit",
            label="交卷",
            description="填答案 → 核验 → 交卷（不可逆，必须把确认串写成「交卷」）",
            params=(
                ActionParam("exam_id", "考试号", required=True),
                ActionParam("answers", "答案单（JSON，可留空用 AI 答题的缓存）", type="text"),
                ActionParam(
                    "confirm", "确认串", required=True,
                    hint="必须输入「交卷」—— 交卷后不能再次作答",
                ),
            ),
            mutates=True,
        ),
        _itest_submit,
    )


# --------------------------------------------------------------------------
# iTEST 进考场作答：取卷 / 填答 / 交卷
# --------------------------------------------------------------------------
#
# 这一组动作把 exam.py 的浏览器链路接到控制台。三条纪律写在最前面：
#   1. 进考场就开始计时 —— 取卷动作本身也是不可逆的（但只读，不改答案）；
#   2. 填答案只操作界面，不拼 ansData —— 页面自己组装载荷；
#   3. 交卷必须带人工确认串，且核验有错就**拒绝交卷**。

_ITEST_KIND_LABEL = {"blank": "填空", "radio": "单选", "write": "翻译"}


def _itest_require(params: dict[str, Any], key: str, label: str) -> str:
    value = str(params.get(key) or "").strip()
    if not value:
        raise ActionInputError(f"缺少{label}（{key}）")
    return value


def _itest_exam_cookies(adapter: Any) -> list[dict[str, Any]]:
    """从适配器已登录的 HTTP 会话取 cookie，灌进浏览器（不落盘、不打印）。"""
    from kedaibiao.platforms.itest.exam import cookies_from_jar

    client = adapter.client()
    http = getattr(client, "http", None)
    # cookie 挂在传输层的 jar 上（UrllibTransport.cookies），HttpClient 本身没有
    jar = getattr(getattr(http, "transport", None), "cookies", None)
    if jar is None:
        jar = getattr(http, "cookies", None)
    cookies = cookies_from_jar(jar) if jar is not None else []
    if not cookies:
        raise ActionInputError(
            "当前账号没有可用的登录会话 —— 先在「账号」页绑定该平台并登录一次"
        )
    return cookies


def _itest_runner(adapter: Any) -> Any:
    from kedaibiao.platforms.itest.exam import ExamRunner

    return ExamRunner(_itest_exam_cookies(adapter))


def _itest_cache(adapter: Any, kind: str, exam_id: str) -> Path:
    from kedaibiao.platforms.itest import cache

    settings = getattr(adapter.ctx, "settings", None)
    if kind == "paper":
        return cache.paper_path(settings, exam_id)
    if kind == "answers":
        return cache.answers_path(settings, exam_id)
    return cache.cache_dir(settings) / f"{kind}-{exam_id}.json"


def _itest_cached_answers(adapter: Any, exam_id: str) -> dict[str, str]:
    """读 AI 答题落盘的答案单（``answers-<考试号>.json``）。"""
    path = _itest_cache(adapter, "answers", exam_id)
    if not path.is_file():
        raise ActionInputError(
            f"没有现成答案单：{path} 不存在 —— 先点「AI 答题」，或在参数里直接贴 JSON"
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ActionInputError(f"答案单文件坏了：{exc}") from exc
    answers = data.get("answers") if isinstance(data, dict) else None
    if not isinstance(answers, dict) or not answers:
        raise ActionInputError("答案单文件里没有 answers")
    return {str(k): str(v) for k, v in answers.items() if str(v).strip()}


def _itest_shot_dir(adapter: Any) -> Path:
    from kedaibiao.platforms.itest import cache

    return cache.cache_dir(getattr(adapter.ctx, "settings", None))


def _itest_parse_answers(raw: Any) -> dict[str, str]:
    """答案单必须是 ``{qid: "答案"}`` 的 JSON 对象。"""
    text = str(raw or "").strip()
    if not text:
        raise ActionInputError("答案单是空的")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ActionInputError(f"答案单不是合法 JSON：{exc}") from exc
    if not isinstance(data, dict) or not data:
        raise ActionInputError('答案单必须是对象，例如 {"1001353740": "F I G C B E J D A H"}')
    out: dict[str, str] = {}
    for key, value in data.items():
        if not isinstance(value, (str, int, float)):
            raise ActionInputError(f"答案 {key!r} 的值必须是字符串（填字母串或整句译文）")
        out[str(key)] = str(value).strip()
    return out


def _itest_paper_name(paper: Any) -> str:
    """给控制台看的卷名。

    页面 ``<title>`` 是「试题作答 - iTEST…」这类壳标题，真正的作业名在第一个大题的
    ``part1`` 里（实测 B2U5/B2U6 都是「26-27-1大学英语I（三）单元作业-新标准B2U5」）。
    """
    for section in getattr(paper, "sections", []) or []:
        if section.part1:
            return str(section.part1)
    return str(getattr(paper, "title", "") or "")


def _itest_ks_id(entry: Any) -> str:
    """考试卡片上印的是 ksId（不是 ksdId）—— 点卡片进考场要用它。"""
    return str(((getattr(entry, "raw", None) or {}).get("ksId")) or "")


async def _itest_exam_entry(adapter: Any, exam_id: str) -> tuple[str, str]:
    """取这场考试的 (标题, ksId)；拿不到元信息就让 ksId 留空（走兜底路径）。"""
    entry = await adapter.exam_meta(exam_id)
    return (entry.title if entry else ""), _itest_ks_id(entry)


def _itest_paper_blocking(
    adapter: Any, exam_id: str, ks_id: str = ""
) -> dict[str, Any]:
    runner = _itest_runner(adapter)
    with runner:
        entered = runner.enter(exam_id, ks_id=ks_id)
        if not entered.ok:
            raise ActionInputError(
                f"没能进入作答页：{entered.message}（步骤：{_steps_text(entered.steps)}）"
            )
        paper = runner.read_paper()
        current = runner.read_answers()
        shot = runner.save_screenshot(_itest_shot_dir(adapter) / f"paper-{exam_id}.png")
    return {"entered": entered, "paper": paper, "current": current, "shot": shot}


def _itest_fill_blocking(
    adapter: Any, exam_id: str, answers: dict[str, str], *, submit: bool,
    ks_id: str = "",
) -> dict[str, Any]:
    runner = _itest_runner(adapter)
    with runner:
        entered = runner.enter(exam_id, ks_id=ks_id)
        if not entered.ok:
            raise ActionInputError(
                f"没能进入作答页：{entered.message}（步骤：{_steps_text(entered.steps)}）"
            )
        paper = runner.read_paper()
        applied = paper.apply_answers(answers)
        filled = runner.fill(answers)
        checked = runner.verify(answers)
        shot = runner.save_screenshot(_itest_shot_dir(adapter) / f"filled-{exam_id}.png")
        result: dict[str, Any] = {
            "entered": entered, "paper": paper, "current": runner.read_answers(),
            "applied": applied, "filled": filled, "checked": checked, "shot": shot,
            "submitted": None,
        }
        if submit:
            result["submitted"] = runner.submit()
            result["shot_after"] = runner.save_screenshot(
                _itest_shot_dir(adapter) / f"submitted-{exam_id}.png"
            )
        return result


def _steps_text(steps: list[Any]) -> str:
    return "；".join(f"{s.name}{'✓' if s.ok else '✗'}" for s in steps) or "无"


async def _itest_paper(adapter: Any, params: dict[str, Any]) -> dict[str, Any]:
    """进考场把试卷读出来（会开始计时，但只读题目、不改答案）。"""
    name = getattr(adapter, "name", "itest")
    exam_id = _itest_require(params, "exam_id", "考试号")
    _title, ks_id = await _itest_exam_entry(adapter, exam_id)
    data = await asyncio.to_thread(_itest_paper_blocking, adapter, exam_id, ks_id)
    paper = data["paper"]
    current: dict[str, str] = data["current"]
    cache = _itest_cache(adapter, "paper", exam_id)
    try:
        cache.write_text(json.dumps(paper.to_dict(), ensure_ascii=False), encoding="utf-8")
    except OSError:  # noqa: BLE001 - 缓存失败不该让取卷白跑
        log.debug("试卷缓存写失败", exc_info=True)

    rows: list[dict[str, Any]] = []
    for section in paper.sections:
        for question in section.questions:
            rows.append({
                "section": f"{section.index}. {(section.part2 or section.part1)[:14]}",
                "qid": question.qid,
                "kind": _ITEST_KIND_LABEL.get(question.kind, question.kind),
                "number": question.number,
                "text": (question.text or "")[:46],
                "slots": question.slot_count,
                "filled": (current.get(question.qid) or "—")[:24],
            })
    return table(
        platform=name,
        action="paper",
        label="试卷",
        title=f"取卷 · {_itest_paper_name(paper)[:40] or exam_id}",
        note=(
            f"已进入作答页（★ 计时已开始/继续）。{len(paper.sections)} 大题 / "
            f"{paper.question_count} 题 / {paper.blank_count} 槽位；"
            f"「已填」读的是页面当前值（服务端自动保存的也在里面）。"
            f"截图：{data['shot']}"
        ),
        columns=[
            column("section", "大题"),
            column("number", "序号", mono=True),
            column("kind", "题型"),
            column("qid", "qid", mono=True),
            column("text", "题干/首句"),
            column("slots", "槽位", mono=True),
            column("filled", "已填"),
        ],
        rows=rows,
        empty_hint="这场的作答页没解析出题目 —— 可能在设备检测页，或页面结构变了。",
    )


async def _itest_answers(adapter: Any, params: dict[str, Any]) -> dict[str, Any]:
    """用大模型按「取卷」缓存生成答案单，并落盘给「填答案 / 交卷」用。

    不碰平台：只读缓存试卷 + 调模型。听力题不代答（见 answer.py 的说明）。
    """
    from kedaibiao.platforms.itest.answer import AnswerError, build_sheet
    from kedaibiao.platforms.itest.paper import Paper
    from kedaibiao.tasks.llm import LLMAnswerer, LLMConfig

    name = getattr(adapter, "name", "itest")
    exam_id = _itest_require(params, "exam_id", "考试号")
    cache = _itest_cache(adapter, "paper", exam_id)
    if not cache.is_file():
        raise ActionInputError(
            f"还没有试卷缓存（{cache}）—— 先点「进考场取卷」，再让 AI 答题"
        )
    try:
        paper = Paper.from_dict(json.loads(cache.read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:
        raise ActionInputError(f"试卷缓存读不出来：{exc}") from exc

    config = LLMConfig.from_env(getattr(adapter.ctx, "environ", {}) or {})
    if not config.enabled:
        raise ActionInputError(
            "大模型没配置 —— 在 .env 里设置 KEDAIBIAO_LLM_BASE_URL / _API_KEY / _MODEL"
        )
    llm = LLMAnswerer(config)
    try:
        sheet, transcripts, stt_note = await build_sheet(
            paper, llm, _itest_shot_dir(adapter),
            environ=getattr(adapter.ctx, "environ", {}) or {},
        )
    except AnswerError as exc:
        raise ActionInputError(f"AI 答案不合法：{exc}") from exc

    out = _itest_cache(adapter, "answers", exam_id)
    try:
        out.write_text(
            json.dumps({
                "answers": sheet.answers, "skipped": sheet.skipped,
                "transcripts": {str(k): v for k, v in transcripts.items()},
            }, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
    except OSError:  # noqa: BLE001
        log.debug("答案单落盘失败", exc_info=True)

    rows = [
        {
            "qid": item["qid"],
            "kind": _ITEST_KIND_LABEL.get(item["kind"], item["kind"]),
            "answer": str(item["answer"])[:48],
        }
        for item in sheet.details
    ]
    for item in sheet.skipped:
        rows.append({
            "qid": item["qid"],
            "kind": _ITEST_KIND_LABEL.get(item["kind"], item["kind"]),
            "answer": "（跳过）" + item["reason"][:40],
        })
    note = (
        f"模型 {config.model}；答出 {sheet.answered} 题、跳过 {len(sheet.skipped)} 题。"
        + (f" {stt_note}" if stt_note else "")
        + " 答案已存 " + str(out) + "，「填答案 / 交卷」留空 answers 就会用它。"
    )
    if sheet.skipped:
        note += " 跳过的题需要人工补（或见上一条说明）。"
    return table(
        platform=name,
        action="answers",
        label="AI 答案单",
        title=f"AI 答题 · {_itest_paper_name(paper)[:32] or exam_id}",
        note=note,
        columns=[
            column("qid", "qid", mono=True),
            column("kind", "题型"),
            column("answer", "答案"),
        ],
        rows=rows,
        empty_hint="这次一题都没答出来。",
    )


async def _itest_fill(adapter: Any, params: dict[str, Any]) -> dict[str, Any]:
    """把答案填进作答页并逐项读回核验（不交卷）。"""
    name = getattr(adapter, "name", "itest")
    exam_id = _itest_require(params, "exam_id", "考试号")
    raw_answers = str(params.get("answers") or "").strip()
    answers = (_itest_parse_answers(raw_answers) if raw_answers
               else _itest_cached_answers(adapter, exam_id))
    _title, ks_id = await _itest_exam_entry(adapter, exam_id)
    data = await asyncio.to_thread(
        _itest_fill_blocking, adapter, exam_id, answers, submit=False, ks_id=ks_id
    )

    checked = data["checked"]
    bad = {str(item).split("#", 1)[0] for item in checked.get("bad", [])}
    paper = data["paper"]
    rows = [
        {
            "qid": question.qid,
            "kind": _ITEST_KIND_LABEL.get(question.kind, question.kind),
            "want": (question.answer or "")[:40],
            "state": "✗ 核验不符" if question.qid in bad else "✓",
        }
        for section in paper.sections for question in section.questions
    ]
    filled = data["filled"]
    return table(
        platform=name,
        action="fill",
        label="填答案",
        title=f"填答案 · {exam_id}（核验 {checked.get('total', 0)} 处不符）",
        note=(
            f"填空 {filled.get('blank', 0)} / 单选 {filled.get('radio', 0)} / "
            f"翻译 {filled.get('textarea', 0)}；"
            f"未匹配 {len(filled.get('missing', []))}；"
            f"核验不符 {checked.get('total', 0)}。"
            "只改了页面，**没有交卷**；动作结束浏览器就关了，平台每分钟一次的自动保存"
            "可能还没跑 —— 要真正留存，请用「交卷」（同一个会话里填完直接交）。截图已存 "
            + str(data["shot"])
        ),
        columns=[
            column("qid", "qid", mono=True),
            column("kind", "题型"),
            column("want", "应填"),
            column("state", "核验"),
        ],
        rows=rows,
        empty_hint="没有可填的题。",
    )


async def _itest_submit(adapter: Any, params: dict[str, Any]) -> dict[str, Any]:
    """填答案 → 核验 → 交卷（不可逆，必须人工确认）。"""
    name = getattr(adapter, "name", "itest")
    exam_id = _itest_require(params, "exam_id", "考试号")
    if str(params.get("confirm") or "").strip() != "交卷":
        raise ActionInputError("交卷是不可逆操作 —— 请在「确认串」里输入「交卷」")
    raw_answers = str(params.get("answers") or "").strip()
    answers = (_itest_parse_answers(raw_answers) if raw_answers
               else _itest_cached_answers(adapter, exam_id))
    _title, ks_id = await _itest_exam_entry(adapter, exam_id)
    data = await asyncio.to_thread(
        _itest_fill_blocking, adapter, exam_id, answers, submit=True, ks_id=ks_id
    )

    checked = data["checked"]
    submitted = data["submitted"] or {}
    if checked.get("total"):
        # 核验有错就不该交 —— 这一层比「相信调用方」更可靠
        raise ActionInputError(
            f"核验有 {checked['total']} 处不符，已拒绝交卷（答案没填对）。详见 fill 动作。"
        )

    finish = None
    try:
        exams = await adapter.client().list_exams()
        entry = next((e for e in exams if e.exam_id == exam_id), None)
        finish = (entry.raw or {}).get("finish") if entry else None
    except Exception:  # noqa: BLE001 - 查询失败不影响交卷结果本身
        log.debug("交卷后复核考试列表失败", exc_info=True)

    server_ok = finish in (1, "1")
    title = "已交卷" if server_ok else (
        "已交卷（服务端状态还没刷新）" if submitted.get("ok") else "交卷未确认"
    )
    fields = [
        {"label": "考试号", "value": exam_id},
        {"label": "服务端 finish", "value": "已交卷（finish=1）" if server_ok else f"{finish}"},
        {"label": "页面证据", "value": submitted.get("text", "")[:120] or "（没有识别到成功提示）"},
        {"label": "填表", "value": json.dumps(data["filled"], ensure_ascii=False)[:120]},
        {"label": "核验", "value": f"不符 {checked.get('total', 0)} 处"},
        {"label": "交卷后截图", "value": str(data.get("shot_after") or data["shot"])},
    ]
    steps = submitted.get("steps", [])
    return detail(
        platform=name,
        action="submit",
        label="交卷结果",
        title=f"{title} · {exam_id}",
        note=(
            "交卷是不可逆操作。页面出现成功提示且服务端 finish 变成 1 才算真的交上；"
            "两者不一致时以服务端为准，并人工看一眼考试列表。"
        ),
        fields=fields,
        sections=[
            _text_section("页面提示", submitted.get("text", "")),
            _table_section(
                "交卷步骤",
                [column("name", "步骤"), column("detail", "点中的文字")],
                [{"name": s.get("name", ""), "detail": s.get("detail", "")} for s in steps],
            ),
        ],
    )



# --------------------------------------------------------------------------
# 希沃品课（pinco）
# --------------------------------------------------------------------------
#
# 四个动作全部只读：学习任务（课程材料）进度、材料详情、作业题目/作答槽、
# 连接自检。品课的写接口（提交）有自己独立的守卫，这一层**不碰**任何写接口。
#
# 材料详情里的 ``downloadUrl`` 带签名/时效：只在本次响应里展示，不进日志、
# 不落库；「原始字段摘要」里也会把签名地址与凭据的值替换掉。

#: 品课材料列表/详情里的字段名（实测确认，见 platforms/pinco/endpoints.py）。
_PINCO_MATERIAL_SIZE = "materialSize"
_PINCO_MATERIAL_SOURCE = "materialSource"
_PINCO_MEDIA = "media"
_PINCO_MEDIA_FILE_SIZE = "fileSize"
_PINCO_MEDIA_TYPE = "type"

#: 原始字段摘要里要隐藏「值」的键：签名下载地址 / 令牌 / cookie 等。
#: 键名保留、值替换 —— 既看得见平台确实给了这个字段，又不会把敏感值带出去。
_PINCO_SECRET_HINTS = (
    "download", "token", "cookie", "authorization", "csrf", "sign", "secret", "password",
)
_PINCO_SECRET_KEYS = frozenset({"url", "thumbnailurl"})

_QUESTION_TYPE_LABEL = {
    QuestionType.SINGLE: "单选",
    QuestionType.MULTIPLE: "多选",
    QuestionType.JUDGE: "判断",
    QuestionType.FILL: "填空",
    QuestionType.ESSAY: "简答",
    QuestionType.UNKNOWN: "未知",
}


def _pinco_platform(adapter: Any) -> str:
    return getattr(adapter, "name", "pinco")


def _pinco_require(params: dict[str, Any], key: str, label: str) -> str:
    """取必填参数；缺了抛 ``ActionInputError``（服务层翻成 400）。

    服务层在调 handler 前已经校验过一遍，这里再校验一次是为了让 handler
    被单独调用时也给出同样的错误，而不是一个 KeyError 堆栈。
    """
    value = str(params.get(key) or "").strip()
    if not value:
        raise ActionInputError(f"缺少{label}（{key}）")
    return value


def _pinco_size_mb(raw: Any) -> str:
    """材料大小（MB）。列表里是 ``materialSize``，详情里在 ``media.fileSize``。"""
    data = raw if isinstance(raw, dict) else {}
    size = data.get(_PINCO_MATERIAL_SIZE)
    if size is None:
        media = data.get(_PINCO_MEDIA)
        if isinstance(media, dict):
            size = media.get(_PINCO_MEDIA_FILE_SIZE)
    if size is None:
        return "—"
    try:
        number = float(size)
    except (TypeError, ValueError):
        return "—"
    if number <= 0:
        return "—"
    return f"{number / (1024 * 1024):.1f}"


def _pinco_percent(progress: Any) -> str:
    try:
        value = float(progress)
    except (TypeError, ValueError):
        return "—"
    return f"{value * 100:.0f}%"


def _pinco_number(
    params: dict[str, Any],
    key: str,
    label: str,
    default: float,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    """取数值参数；留空用默认值，非法/越界抛 ``ActionInputError``。"""
    raw = params.get(key)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError) as error:
        raise ActionInputError(f"{label}（{key}）必须是数字") from error
    if minimum is not None and value < minimum:
        raise ActionInputError(f"{label}（{key}）不能小于 {minimum:g}")
    if maximum is not None and value > maximum:
        raise ActionInputError(f"{label}（{key}）不能大于 {maximum:g}")
    return value


def _pinco_stop_text(stopped: str) -> str:
    return {
        "dry_run": "干跑",
        "target_reached": "已达标",
        "max_pages": "页数上限",
        "max_total_seconds": "时长上限",
        "ratio_stalled": "进度不涨已停",
    }.get(stopped, stopped or "—")


def _pinco_material_source(raw: Any) -> str:
    """材料来源列：平台原始码，语义未确认，所以不编中文映射，只如实展示。"""
    data = raw if isinstance(raw, dict) else {}
    source = data.get(_PINCO_MATERIAL_SOURCE)
    if source is not None:
        return f"materialSource={source}"
    media = data.get(_PINCO_MEDIA)
    if isinstance(media, dict) and media.get(_PINCO_MEDIA_TYPE) is not None:
        return f"media.type={media[_PINCO_MEDIA_TYPE]}"
    return "—"


def _pinco_redact(value: Any, depth: int = 0) -> Any:
    """递归隐藏原始载荷里的签名地址与凭据值（键名保留）。"""
    if depth > 6:
        return "…"
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            lower = name.lower()
            if lower in _PINCO_SECRET_KEYS or any(
                hint in lower for hint in _PINCO_SECRET_HINTS
            ):
                out[name] = "（已隐藏：含签名或凭据）"
            else:
                out[name] = _pinco_redact(item, depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [_pinco_redact(item, depth + 1) for item in list(value)[:50]]
    if isinstance(value, str) and len(value) > 300:
        return value[:300] + "…"
    return value


def _pinco_raw_summary(raw: Any) -> str:
    try:
        text = json.dumps(
            _pinco_redact(raw or {}), ensure_ascii=False, indent=2, default=str
        )
    except (TypeError, ValueError):
        return "（原始字段无法序列化）"
    return text[:4000]


def _pinco_brief(exc: BaseException) -> str:
    return (str(exc).strip() or type(exc).__name__)[:200]


async def _pinco_find_course(adapter: Any, course_id: str) -> Any:
    name = _pinco_platform(adapter)
    for course in await adapter.list_courses():
        if course.remote_id == course_id:
            return course
    raise ActionInputError(f"在 {name} 里找不到课程号 {course_id!r}")


async def _pinco_locate_assignment(
    adapter: Any, assignment_id: str
) -> tuple[Assignment, Any]:
    """在课程/作业列表里找到目标作业（返回作业与所属课程）。

    品课没有「按作业号取作业」的只读方法，所以只能扫一遍列表。控制台的
    「作业列表」也是按这个口径列的，从那里复制的作业号一定能找到。
    """
    name = _pinco_platform(adapter)
    for course in await adapter.list_courses():
        for item in await adapter.list_assignments(course):
            if item.remote_id == assignment_id:
                return item, course
    raise ActionInputError(
        f"在 {name} 的作业列表里找不到作业 {assignment_id!r} —— "
        "请从「作业列表」复制作业号"
    )


async def _pinco_learning(adapter: Any, params: dict[str, Any]) -> dict[str, Any]:
    """学习任务 = 课程材料：列材料、大小、学习进度（只读）。"""
    name = _pinco_platform(adapter)
    only = str(params.get("course") or "").strip()
    courses = list(await adapter.list_courses())
    if only:
        courses = [course for course in courses if course.remote_id == only]
        if not courses:
            raise ActionInputError(f"没有找到课程号 {only!r}")

    rows: list[dict[str, Any]] = []
    failures: list[str] = []
    for course in courses:
        try:
            items = list(await adapter.list_learning_tasks(course))
        except Exception as error:  # noqa: BLE001 - 只跳平台声明的课程级错误
            if only or not _course_scoped_failure(adapter, error):
                raise
            failures.append(f"{course.name or course.remote_id}（{error}）")
            continue
        for item in items:
            rows.append(
                {
                    "course": course.name or course.remote_id,
                    # 补课程号：material 动作要的是 course 号，只有课程名的话
                    # 用户还得回「课程列表」对一遍才能查材料详情
                    "course_id": course.remote_id,
                    "material_id": item.remote_id,
                    "name": item.title,
                    "size_mb": _pinco_size_mb(item.raw),
                    "progress": _pinco_percent(item.progress),
                    "source": _pinco_material_source(item.raw),
                }
            )

    scope = f"（课程 {only}）" if only else "（全部课程）"
    return table(
        platform=name,
        action="learning",
        label="学习任务 / 材料进度",
        title=f"学习任务 / 材料{scope} —— {len(rows)} 项",
        note=(
            "只读。品课的「学习任务」就是课程材料（PPT/PDF/视频）：没有题目，"
            "也不会提交任何内容。「来源」列是平台原始 materialSource/media.type 码，"
            "语义未确认，只如实展示。"
            + (
                "学习通的作业/测验/考试来自课程页「考试」tab（含状态与已回显分数）。"
                if name == "chaoxing" else ""
            )
            + _skipped_note(failures, len(courses))
        ),
        columns=[
            column("course", "课程"),
            column("course_id", "课程号", mono=True),
            column("material_id", "材料号", mono=True),
            column("name", "名称"),
            column("size_mb", "大小(MB)"),
            column("progress", "学习进度(%)"),
            column("source", "来源"),
        ],
        rows=rows,
        empty_hint="这些课程下没有课程材料（或平台没有返回）。",
    )


async def _pinco_material(adapter: Any, params: dict[str, Any]) -> dict[str, Any]:
    """材料详情：大小、进度、下载地址与原始字段摘要（只读）。"""
    name = _pinco_platform(adapter)
    course_id = _pinco_require(params, "course", "课程号")
    material_id = _pinco_require(params, "material_id", "材料号")
    course = await _pinco_find_course(adapter, course_id)
    item = await adapter.fetch_material_detail(course, material_id)

    if item.downloadable is None:
        downloadable = "—"
    else:
        downloadable = "可下载" if item.downloadable else "不可下载"

    return detail(
        platform=name,
        action="material",
        label="材料详情",
        title=f"材料详情 · {item.name or material_id}",
        note=(
            "只读。下载地址带签名/时效，只在本次响应里展示 —— 不要保存、转发"
            "或写进文档；下面的原始字段摘要已把签名地址与凭据的值替换掉。"
        ),
        fields=[
            _field("材料号", item.material_id or material_id),
            _field("材料名", item.name or "—"),
            _field("课程", course.name or course.remote_id),
            _field("大小(MB)", _pinco_size_mb(item.raw)),
            _field("学习进度", _pinco_percent(item.progress)),
            _field("可下载", downloadable),
            _field("下载地址", item.download_url or "（平台未提供下载地址）"),
        ],
        sections=[
            _text_section(
                "原始字段摘要（签名地址与凭据已隐藏）", _pinco_raw_summary(item.raw)
            ),
        ],
    )


def _pinco_answer_slot(raw: Any) -> tuple[Any, Any, Any]:
    """从题目原始载荷里取已有作答槽的 (score, review, submitted)。

    实测形态：题目项里带 ``answer: {score, review, submitted, …}``；
    没有这个对象时按「平台没给」处理（返回 None / 空串），不猜。
    """
    data = raw if isinstance(raw, dict) else {}
    block = data.get("answer")
    if not isinstance(block, dict):
        block = {}
    score = block.get("score", data.get("score"))
    review = block.get("review", data.get("review", ""))
    submitted = block.get("submitted", data.get("submitted"))
    return score, review, submitted


def _pinco_score_text(score: Any) -> str:
    if score is None:
        return "—"
    try:
        number = float(score)
    except (TypeError, ValueError):
        return str(score)
    if number < 0:  # 实测：未评分为 -1
        return "未评分"
    return f"{number:g}"


def _pinco_options_text(question: Any) -> str:
    options = list(getattr(question, "options", None) or [])
    if not options:
        return "—"
    text = " / ".join(
        f"{chr(ord('A') + index)}. {option}" for index, option in enumerate(options)
    )
    return text[:200]


async def _pinco_answers(adapter: Any, params: dict[str, Any]) -> dict[str, Any]:
    """作业题目与作答槽：逐题列题型/题干/选项 + 已有作答状态（只读）。"""
    name = _pinco_platform(adapter)
    assignment_id = _pinco_require(params, "assignment_id", "作业号")
    target, course = await _pinco_locate_assignment(adapter, assignment_id)

    try:
        questions = list(await adapter.fetch_questions(target))
    except NotSupportedError as exc:
        # 材料（学习任务）没有题目 —— 这是「选错了」，不是平台故障。
        raise ActionInputError(str(exc)) from exc

    rows: list[dict[str, Any]] = []
    for index, question in enumerate(questions):
        score, review, submitted = _pinco_answer_slot(question.raw)
        rows.append(
            {
                "index": index + 1,
                "qid": question.remote_id or "",
                "type": _QUESTION_TYPE_LABEL.get(question.type, question.type.value),
                "stem": question.stem or "（平台未提供文字题干，题面可能在图片里）",
                "options": _pinco_options_text(question),
                "score": _pinco_score_text(score),
                "review": str(review or "").strip()[:80] or "—",
                "submitted": (
                    "—" if submitted is None else ("已提交" if submitted else "未提交")
                ),
            }
        )

    return detail(
        platform=name,
        action="answers",
        label="作业题目与作答槽",
        title=f"{target.title or assignment_id} · 作答槽",
        note=(
            "只读：这里只列出题目与平台已有的作答槽状态，不拼提交报文，"
            "也不调用任何写接口。score 为 -1 表示平台尚未评分。"
        ),
        fields=[
            _field("作业号", target.remote_id),
            _field("名称", target.title or "—"),
            _field("课程", target.course_name or course.name or "—"),
            _field("类型", _KIND_LABEL.get(target.kind, target.kind.value)),
            _field("截止", _fmt_deadline(target.deadline) or "—"),
            _field("状态", "已完成" if target.finished else "未完成"),
            _field("题目数", len(questions)),
        ],
        sections=[
            _table_section(
                "题目与作答槽",
                [
                    column("index", "#"),
                    column("qid", "题目号", mono=True),
                    column("type", "题型"),
                    column("stem", "题干"),
                    column("options", "选项"),
                    column("score", "score"),
                    column("review", "review"),
                    column("submitted", "submitted"),
                ],
                rows,
            )
        ],
    )


async def _pinco_connection(adapter: Any, params: dict[str, Any]) -> dict[str, Any]:
    """连接自检：会话 / is_logged_in / health_check / 课程数量。

    与别的动作不同，这里把「健康检查失败」「课程数量读不出来」当成**结果**展示，
    而不是往上抛异常 —— 自检的价值就在于把失败点说清楚，而不是变成一条 502。
    """
    name = _pinco_platform(adapter)
    display = getattr(adapter, "display_name", "") or name
    session = getattr(adapter, "session", None)
    logged_in = bool(await adapter.is_logged_in())

    try:
        healthy = await adapter.health_check()
        health_text = "通过" if healthy else "失败"
    except Exception as exc:  # noqa: BLE001 - 自检要把失败原因显示出来
        log.debug("品课连接自检：health_check 抛异常", exc_info=True)
        health_text = f"失败（{type(exc).__name__}: {_pinco_brief(exc)}）"

    if not logged_in:
        course_text = "未读取（未登录）"
    else:
        try:
            course_text = str(len(await adapter.list_courses()))
        except Exception as exc:  # noqa: BLE001 - 同上，失败原因比异常栈有用
            log.debug("品课连接自检：课程列表读取失败", exc_info=True)
            course_text = f"读取失败（{type(exc).__name__}: {_pinco_brief(exc)}）"

    fields = [
        _field("平台", f"{display}（{name}）"),
        _field("会话", "已接管" if session is not None else "没有会话"),
        _field("账号", getattr(session, "username", "") or "—"),
        _field("is_logged_in", "有效" if logged_in else "无效或未登录"),
        _field("health_check", health_text),
        _field("课程数量", course_text),
    ]
    expires_at = getattr(session, "expires_at", None)
    if expires_at:
        fields.append(_field("会话到期", _fmt_deadline(expires_at)))

    return detail(
        platform=name,
        action="connection",
        label="连接自检",
        title=f"连接自检 · {display}",
        note="这是一次真实访问平台的只读自检，不会提交任何内容。",
        fields=fields,
        sections=[
            _text_section(
                "自检项说明",
                "is_logged_in：本地会话状态（不访问平台）；"
                "health_check：匿名 GET /system/currentTime，只说明网络/协议通；"
                "课程数量：GetV1CourseStudentworkbench，能读出来才说明登录态与业务"
                "接口都可用。健康检查通过但课程数量读取失败时，问题多半在会话/csrf，"
                "而不是网络。",
            )
        ],
    )


async def _pinco_progress(adapter: Any, params: dict[str, Any]) -> dict[str, Any]:
    """按页推进材料学习进度（「刷课」）—— **写操作**，``confirm=true`` 才真写。"""
    # 平台专属策略对象：延迟导入，避免 API 层在模块导入期就依赖 pinco 包。
    from kedaibiao.platforms.pinco.progress import ProgressPolicy

    name = _pinco_platform(adapter)
    course_id = _pinco_require(params, "course", "课程号")
    material_id = str(params.get("material_id") or "").strip()
    confirm = str(params.get("confirm") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "是",
    }
    target = _pinco_number(params, "target", "目标进度", 1.0, minimum=0.0, maximum=1.0)
    max_seconds = _pinco_number(params, "max_seconds", "每份时间上限", 60.0, minimum=5.0)
    page_interval = _pinco_number(
        params, "page_interval", "翻页间隔", 1.0, minimum=0.2
    )
    max_materials = int(
        _pinco_number(params, "max_materials", "本次最多处理几份", 3, minimum=1)
    )

    course = await _pinco_find_course(adapter, course_id)
    materials = list(await adapter.list_learning_tasks(course))
    remaining = 0
    if material_id:
        targets = [item for item in materials if item.remote_id == material_id]
        if not targets:
            raise ActionInputError(f"课程 {course_id} 里没有材料号 {material_id!r}")
    else:
        unfinished = [item for item in materials if item.progress + 1e-9 < target]
        targets = unfinished[:max_materials]
        remaining = max(0, len(unfinished) - len(targets))

    policy = ProgressPolicy(
        page_interval_seconds=page_interval,
        max_total_seconds=max_seconds,
        refresh_every_pages=10,
        ratio_stall_checks=2,
    )
    rows: list[dict[str, Any]] = []
    for item in targets:
        report = await adapter.advance_learning(
            course,
            item.remote_id,
            dry_run=not confirm,
            policy=policy,
            target_ratio=target,
        )
        rows.append(
            {
                "material_id": item.remote_id,
                "name": item.title,
                "before": _pinco_percent(report.ratio_before),
                "after": _pinco_percent(report.ratio_after),
                "pages": (
                    f"{report.planned_pages}（计划）"
                    if report.dry_run and report.total_pages
                    else ("—" if report.dry_run else str(report.sent_pages))
                ),
                "stopped": _pinco_stop_text(report.stopped),
            }
        )

    mode = "写入" if confirm else "干跑"
    fields = [
        _field("课程", course.name or course_id),
        _field("模式", mode),
        _field("目标进度", _pinco_percent(target)),
        _field("本次材料", f"{len(targets)} 份"),
    ]
    if remaining:
        fields.append(_field("剩余未处理", f"{remaining} 份（可再执行一次继续）"))

    note = (
        "写操作：会按页推进平台上的学习进度（不可撤销，建议一次少推几份）。"
        if confirm
        else (
            "干跑：只读材料/进度，**没有发送任何写请求**。"
            "确认无误后把 confirm 填 true 再执行一次才会真的翻页。"
        )
    )
    if confirm:
        stalled = [row for row in rows if row["stopped"] == "进度不涨已停"]
        if stalled:
            note += " 有材料的进度连续回读都没涨，已按熔断停止。"

    return detail(
        platform=name,
        action="progress",
        label="推进学习进度（写操作）",
        title=f"推进学习进度 · {course.name or course_id}（{mode}）",
        note=note,
        fields=fields,
        sections=[
            {
                "label": "处理结果",
                "kind": "table",
                "columns": [
                    column("material_id", "材料号", mono=True),
                    column("name", "名称"),
                    column("before", "进度前"),
                    column("after", "进度后"),
                    column("pages", "翻页"),
                    column("stopped", "停止原因"),
                ],
                "rows": rows,
            }
        ],
    )


async def _pinco_signin(adapter: Any, params: dict[str, Any]) -> dict[str, Any]:
    """课堂签到（写操作）—— 把签到链接里的 ``attendanceCode`` 提交给考勤接口。

    ``confirm=true`` 才真的发；留空只解析签到码（干跑）。平台拒绝（码无效 /
    已结束 / 不匹配）作为结果展示，不变成 502 —— 那也是一次签到的真实结果。
    """
    name = _pinco_platform(adapter)
    raw = _pinco_require(params, "code", "签到码 / 签到链接")
    confirm = str(params.get("confirm") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "是",
    }
    try:
        outcome = await adapter.sign_in(raw, dry_run=not confirm)
    except ValueError as exc:
        raise ActionInputError(str(exc)) from exc

    mode = "写入" if confirm else "干跑"
    if outcome.dry_run:
        result_text = "未发送（干跑）"
    elif outcome.signed:
        result_text = "已发送，签到成功"
    else:
        result_text = "已发送，平台拒绝"

    fields = [
        _field("平台", getattr(adapter, "display_name", "") or name),
        _field("模式", mode),
        _field("签到码", f"{outcome.masked_key}（{len(outcome.attendance_key)} 位）"),
        _field("结果", result_text),
    ]
    if outcome.error_code is not None:
        fields.append(_field("平台错误码", outcome.error_code))
    if not outcome.dry_run and not outcome.signed:
        fields.append(_field("失败原因", outcome.message))

    if outcome.dry_run:
        note = (
            "干跑：只解析签到码，**没有发送任何请求**。"
            "确认链接/码无误后把 confirm 填 true 再执行一次才会真的签到。"
        )
    elif outcome.signed:
        note = (
            "写操作：已调用 PostV1TeachingAttendanceAttend，平台接受了这次签到。"
            "签到码是滚动码，不能复用；考勤统计以平台为准。"
        )
    else:
        note = (
            "写操作：请求已发出，但平台拒绝了这次签到（见失败原因）。"
            "签到码是滚动码，过期/换码后需要重新取当次的链接再试。"
        )

    return detail(
        platform=name,
        action="signin",
        label="课堂签到（写操作）",
        title=f"课堂签到 · {mode}",
        note=note,
        fields=fields,
        sections=[
            _text_section(
                "机制与证据边界",
                "学生端 H5 /student/signin?attendanceCode=... 读链接里的 attendanceCode，"
                "再发 PostV1TeachingAttendanceAttend {attendanceKey: <码>}。"
                "字段名与失败码（7207 码无效 / 7208 已结束 / 10419 不匹配）已实测；"
                "成功路径来自学生端 H5 静态代码，尚未用有效签到码实测。"
                "老师端二维码约 10 秒一换，请粘贴当次链接，不要复用旧码。",
            )
        ],
    )


def _register_pinco() -> None:
    register(
        ActionSpec(
            platform="pinco",
            name="learning",
            label="学习任务 / 材料进度",
            description="课程材料与学习进度（只读；课程号留空 = 全部课程）",
            params=(ActionParam("course", "课程号", hint="留空 = 全部课程"),),
        ),
        _pinco_learning,
    )
    register(
        ActionSpec(
            platform="pinco",
            name="material",
            label="材料详情",
            description=(
                "按课程号 + 材料号取材料详情与下载地址"
                "（只读；下载地址带签名，只展示不落库）"
            ),
            params=(
                ActionParam("course", "课程号", required=True),
                ActionParam("material_id", "材料号", required=True),
            ),
        ),
        _pinco_material,
    )
    register(
        ActionSpec(
            platform="pinco",
            name="answers",
            label="作业题目与作答槽",
            description="逐题列出题型/题干/选项与已有作答槽状态（只读，不提交）",
            params=(ActionParam("assignment_id", "作业号", required=True),),
        ),
        _pinco_answers,
    )
    register(
        ActionSpec(
            platform="pinco",
            name="connection",
            label="连接自检",
            description="检查品课会话、健康检查与课程数量（只读，会真的访问平台）",
        ),
        _pinco_connection,
    )
    register(
        ActionSpec(
            platform="pinco",
            name="progress",
            label="推进学习进度（写操作）",
            description=(
                "按页推进材料学习进度：先打开课件拿总页数，再逐页上报 maxPageIndex。"
                "默认干跑（只算不写），confirm=true 才真的翻页。"
            ),
            params=(
                ActionParam("course", "课程号", required=True),
                ActionParam(
                    "material_id",
                    "材料号",
                    hint="留空 = 该课程下未达标的材料（最多 max_materials 份）",
                ),
                ActionParam("target", "目标进度 0~1", hint="默认 1.0"),
                ActionParam(
                    "confirm",
                    "确认写入",
                    hint="填 true 才会真的翻页；留空 = 干跑（默认）",
                ),
                ActionParam("max_seconds", "每份时间上限（秒）", hint="默认 60"),
                ActionParam(
                    "max_materials",
                    "本次最多处理几份",
                    hint="默认 3；只对 material_id 留空时生效",
                ),
                ActionParam("page_interval", "翻页间隔（秒）", hint="默认 1.0，不得小于 0.2"),
            ),
            mutates=True,
        ),
        _pinco_progress,
    )
    register(
        ActionSpec(
            platform="pinco",
            name="signin",
            label="课堂签到（写操作）",
            description=(
                "把老师发的签到链接/签到码提交给考勤接口"
                "（PostV1TeachingAttendanceAttend）。默认干跑，confirm=true 才真发。"
            ),
            params=(
                ActionParam(
                    "code",
                    "签到码 / 签到链接",
                    required=True,
                    hint="支持 /student/signin?attendanceCode=... 整条链接，或直接填码",
                ),
                ActionParam(
                    "confirm",
                    "确认写入",
                    hint="填 true 才会真的签到；留空 = 干跑（默认）",
                ),
            ),
            mutates=True,
        ),
        _pinco_signin,
    )


# --------------------------------------------------------------------------
# U校园（unipus）
# --------------------------------------------------------------------------


def _unipus_platform(adapter: Any) -> str:
    return getattr(adapter, "name", "unipus") or "unipus"


def _unipus_require(params: dict[str, Any], key: str, label: str) -> str:
    value = str(params.get(key) or "").strip()
    if not value:
        raise ActionInputError(f"缺少{label}（{key}）")
    return value


def _unipus_flag(params: dict[str, Any], key: str) -> bool:
    raw = params.get(key)
    if isinstance(raw, bool):
        return raw
    return str(raw or "").strip().lower() in ("1", "true", "yes", "y", "on", "是")


def _unipus_short(text: str, limit: int = 120) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _unipus_answers_text(answers: Any) -> str:
    """把标准答案的逐子题答案摊成一行：``1:A 2:B``。"""
    if answers is None or not getattr(answers, "children", ()):
        return "—"
    parts: list[str] = []
    for index, group in enumerate(answers.children, start=1):
        values = [str(item) for item in (group or ()) if str(item)]
        if values:
            parts.append(f"{index}:{'/'.join(values)}")
    return " ".join(parts) or "—"


def _unipus_options_text(question: Any) -> str:
    options = list(getattr(question, "options", ()) or [])
    if not options:
        return "—"
    return " / ".join(_unipus_short(option, 40) for option in options)


async def _unipus_answers(adapter: Any, params: dict[str, Any]) -> dict[str, Any]:
    """列出某道任务的内容题目 + 服务端标准答案（只读）。"""
    from kedaibiao.platforms.unipus import parsing as unipus_parsing

    name = _unipus_platform(adapter)
    assignment_id = _unipus_require(params, "assignment_id", "任务号")
    try:
        course_instance_id, group_id = unipus_parsing.split_assignment_id(assignment_id)
    except Exception as exc:  # noqa: BLE001 - 输入格式错误 → 400
        raise ActionInputError(str(exc)) from exc

    client = adapter.client()
    try:
        questions = list(await client.fetch_content(course_instance_id, group_id))
        answers = list(await client.fetch_answers(course_instance_id, group_id))
    except Exception:  # noqa: BLE001 - 平台异常由服务层翻成 502
        raise

    rows: list[dict[str, Any]] = []
    for index, item in enumerate(questions):
        answer = answers[index] if index < len(answers) else None
        rows.append(
            {
                "index": index + 1,
                "qid": item.instance_id,
                "type": item.unipus_type or "—",
                "children": item.child_count,
                "stem": _unipus_short(item.stem, 160) or "（无文字题干）",
                "options": _unipus_options_text(item),
                "answers": _unipus_answers_text(answer),
                "points": item.item_count,
            }
        )

    return detail(
        platform=name,
        action="answers",
        label="题目与标准答案",
        title=f"{group_id} · 题目与标准答案",
        note=(
            "只读：内容与答案都来自平台（答案由服务端下发，适配器原样转发）。"
            "「计分点」= 提交时该题贡献的判分项数量。"
        ),
        fields=[
            _field("任务号", assignment_id),
            _field("课程实例", course_instance_id),
            _field("分组", group_id),
            _field("题目数", len(rows)),
            _field("计分点合计", sum(int(row["points"] or 0) for row in rows)),
        ],
        sections=[
            _table_section(
                "题目",
                [
                    {"key": "index", "label": "#"},
                    {"key": "type", "label": "平台题型"},
                    {"key": "children", "label": "子题"},
                    {"key": "stem", "label": "题干"},
                    {"key": "options", "label": "选项"},
                    {"key": "answers", "label": "标准答案"},
                    {"key": "points", "label": "计分点"},
                ],
                rows,
            )
        ],
    )


async def _unipus_duration(adapter: Any, params: dict[str, Any]) -> dict[str, Any]:
    """学习时长上报：默认干跑，confirm=true 才真的建连接（写操作）。"""
    from kedaibiao.platforms.unipus import duration as duration_mod
    from kedaibiao.platforms.unipus import endpoints as unipus_endpoints

    name = _unipus_platform(adapter)
    seconds = float(params.get("seconds") or 60)
    if seconds <= 0:
        raise ActionInputError("seconds 必须是正数")
    confirm = _unipus_flag(params, "confirm")
    kick = _unipus_flag(params, "kick")
    module = str(params.get("module") or "").strip()
    module_group = str(params.get("module_group") or "").strip()

    client = adapter.client()
    if not client.session.open_id:
        raise ActionInputError("会话里没有 open_id（先到「账号」页登录一次）")
    token = client.auth_headers(unipus_endpoints.UnipusApp.UCONTENT).get(
        unipus_endpoints.UCONTENT_AUTH_HEADER, ""
    )
    if not token:
        raise ActionInputError("取不到 annotator token（会话不完整）")

    report = await asyncio.to_thread(
        duration_mod.report_duration,
        client.session.open_id,
        token,
        seconds,
        confirm=confirm,
        enable_kick=kick,
        module=module,
        module_group=module_group,
    )
    plan = report.plan
    lines = [
        f"干跑：{'否（已真实建立连接）' if confirm else '是（未建立任何连接）'}",
        f"计划时长：{plan.requested_seconds:g} 秒；心跳 {plan.planned_pings} 次"
        f"（间隔 {plan.ping_interval:g}s）",
    ]
    if kick:
        lines.append(f"kick-v2：已启用（module={module or '-'} / group={module_group or '-'}）")
    else:
        lines.append("kick-v2：未启用（源码事实：不 kick 不记账，是否替用户做由你决定）")
    lines.append(
        f"连接={report.connected} 入房={report.namespace_acked} "
        f"ping={report.pings_sent} pong={report.pongs_received} "
        f"观测记账秒数={report.credited_seconds:g}"
    )
    if report.error:
        lines.append(f"错误：{report.error}")
    for warning in report.warnings:
        lines.append(f"⚠️ {warning}")
    return message(
        platform=name,
        action="duration",
        label="学习时长上报",
        title="学习时长上报（写操作）",
        message="\n".join(lines),
    )


async def _unipus_submit(adapter: Any, params: dict[str, Any]) -> dict[str, Any]:
    """提交一个任务组：``confirm=false`` 只出计划，``true`` 才真发（不可逆）。"""
    name = _unipus_platform(adapter)
    assignment_id = _unipus_require(params, "assignment_id", "任务号")
    confirm = _unipus_flag(params, "confirm")
    if not getattr(adapter, "native_submit_flow", False):
        raise ActionInputError("该适配器没有原生提交链路（native_plan/native_submit）")

    if not confirm:
        plan = await adapter.native_plan(assignment_id)
        rows: list[dict[str, Any]] = []
        for item in plan.get("questions") or []:
            rows.append(
                {
                    "index": int(item.get("index") or 0) + 1,
                    "kind": item.get("kind") or "—",
                    "children": item.get("child_count"),
                    "stem": _unipus_short(item.get("stem") or "", 140) or "（无文字题干）",
                    "answer": item.get("answer") or "—",
                    "points": item.get("item_count"),
                }
            )
        blockers = [str(item) for item in (plan.get("blockers") or [])]
        warnings = [str(item) for item in (plan.get("warnings") or [])]
        return detail(
            platform=name,
            action="submit",
            label="提交计划（干跑）",
            title=f"{plan.get('title') or assignment_id} · 提交计划",
            note=(
                "干跑：只读内容 + 标准答案并做本地校验，**没有向平台提交任何内容**。"
                "确认无误后加 confirm=true 才会真发（不可逆）。"
            ),
            fields=[
                _field("任务号", assignment_id),
                _field("分组", plan.get("group_id") or "—"),
                _field("提交类型", plan.get("submit_type")),
                _field("题目数", plan.get("question_count")),
                _field("计分点", plan.get("item_count")),
                _field("可提交", "是" if plan.get("can_submit") else "否"),
                _field("结论", plan.get("verdict") or "—"),
                _field("阻塞项", "；".join(blockers) or "无"),
            ],
            sections=[
                _table_section(
                    "题目与将要提交的答案",
                    [
                        {"key": "index", "label": "#"},
                        {"key": "kind", "label": "平台题型"},
                        {"key": "children", "label": "子题"},
                        {"key": "stem", "label": "题干"},
                        {"key": "answer", "label": "将提交的答案"},
                        {"key": "points", "label": "计分点"},
                    ],
                    rows,
                ),
                _text_section("提示", "\n".join(warnings) or "无"),
            ],
        )

    try:
        result = await adapter.native_submit(assignment_id)
    except NotSupportedError as exc:
        raise ActionInputError(str(exc)) from exc
    readback = result.get("readback") or {}
    lines = [
        f"服务端已接受：{'是' if result.get('ok') else '否'}",
        f"code={result.get('code')!r}（类型 {result.get('code_type')}）",
        f"得分率：{result.get('score_pct')}",
        f"回读 progress/v2：server_finish={result.get('server_finish')} "
        f"first_pass={result.get('first_pass')}",
    ]
    if result.get("readback_error"):
        lines.append(f"回读失败：{result['readback_error']}")
    if readback:
        lines.append(f"回读详情：{json.dumps(readback, ensure_ascii=False)}")
    for warning in result.get("warnings") or []:
        lines.append(f"⚠️ {warning}")
    return message(
        platform=name,
        action="submit",
        label="提交结果",
        title=f"{result.get('assignment_id') or assignment_id} · 提交结果",
        message="\n".join(lines),
    )


def _register_unipus() -> None:
    register(
        ActionSpec(
            platform="unipus",
            name="answers",
            label="题目与标准答案",
            description=(
                "按任务号取内容题目与服务端标准答案（只读）。"
                "U校园的作答是转发服务端答案，这里能看到将要提交什么。"
            ),
            params=(ActionParam("assignment_id", "任务号", required=True),),
        ),
        _unipus_answers,
    )
    register(
        ActionSpec(
            platform="unipus",
            name="submit",
            label="提交任务组（写操作）",
            description=(
                "U校园的作答 = 转发服务端标准答案。默认干跑（只出计划），"
                "confirm=true 才真的提交，提交后回读 progress/v2。"
            ),
            params=(
                ActionParam("assignment_id", "任务号", required=True),
                ActionParam(
                    "confirm",
                    "确认提交",
                    hint="填 true 才会真的提交（不可逆）；留空 = 干跑（默认）",
                ),
            ),
            mutates=True,
        ),
        _unipus_submit,
    )
    register(
        ActionSpec(
            platform="unipus",
            name="duration",
            label="学习时长上报（写操作）",
            description=(
                "按 engine.io v3 协议维持学习时长会话。默认干跑（不建连接），"
                "confirm=true 才真连；kick 需要平台记账时再显式打开。"
            ),
            params=(
                ActionParam("seconds", "上报时长（秒）", type="int", hint="默认 60"),
                ActionParam(
                    "confirm",
                    "确认真连",
                    hint="填 true 才会建立连接；留空 = 干跑（默认）",
                ),
                ActionParam(
                    "kick",
                    "发送 kick-v2",
                    hint="填 true 才发；报文形状未实测，且是账号级记账",
                ),
                ActionParam("module", "module", hint="仅 kick 需要"),
                ActionParam("module_group", "moduleGroup", hint="仅 kick 需要"),
            ),
            mutates=True,
        ),
        _unipus_duration,
    )


# --------------------------------------------------------------------------
# 东软智慧教育（neusoft）
# --------------------------------------------------------------------------
#
# 这个平台有两块通用契约覆盖不到的能力：**刷课**（视频/文档，以回读判定完成）
# 和**考勤签到**。所以在这里给它们各配一个动作，控制台「工具」页与 CLI 都能直接用。


async def _neusoft_find_course(adapter: Any, params: dict[str, Any]) -> Any:
    """按课程号或课程名定位一门课。

    允许按名字匹配是因为命令行人肉输入时手里通常只有课程名；仍然要求**精确相等**，
    不做模糊匹配 —— 刷课是写操作，认错课比报错更糟。
    """
    key = str(params.get("course") or "").strip()
    if not key:
        raise ActionInputError("缺少课程号（course）")
    courses = await adapter.list_courses()
    for item in courses:
        if item.remote_id == key:
            return item
    for item in courses:
        if item.name == key:
            return item
    raise ActionInputError(f"没有找到课程 {key}（用 `action neusoft courses` 看课程号）")


async def _neusoft_resources(adapter: Any, params: dict[str, Any]) -> dict[str, Any]:
    """某门课的资源清单与完成度（只读）。"""
    course = await _neusoft_find_course(adapter, params)
    items = await adapter.list_course_resources(course)
    type_names = {1: "视频", 3: "文档", 4: "图片"}
    rows = [
        {
            "res_id": item.res_id,
            "name": item.name or "—",
            "type": type_names.get(item.res_type, f"其他({item.res_type})"),
            "status": "已完成" if item.is_completed else "未完成",
            "completable": "是" if item.is_completable else "否",
        }
        for item in items
    ]
    pending = sum(1 for item in items if item.is_completable and not item.is_completed)
    return table(
        platform=adapter.name,
        action="resources",
        label="课程资源",
        title=f"{course.name} · 资源 {len(items)} 个（待完成 {pending}）",
        note="只读。刷课请用 study 动作 —— 它按回读的学习状态判定，不是看接口返回。",
        columns=[
            column("res_id", "资源号", mono=True),
            column("name", "名称"),
            column("type", "类型"),
            column("status", "状态"),
            column("completable", "可刷"),
        ],
        rows=rows,
        empty_hint="这门课没有资源。",
    )


async def _neusoft_study(adapter: Any, params: dict[str, Any]) -> dict[str, Any]:
    """刷课（写操作）。默认只刷 1 个，用于先验证账号状态。"""
    course = await _neusoft_find_course(adapter, params)
    raw_limit = str(params.get("limit") or "").strip()
    try:
        limit = int(raw_limit) if raw_limit else 1
    except ValueError as exc:
        raise ActionInputError(f"limit 必须是整数，收到 {raw_limit!r}") from exc
    if limit <= 0:
        raise ActionInputError("limit 必须大于 0")
    report = await adapter.play_course(course, only_pending=True, limit=limit)
    return detail(
        platform=adapter.name,
        action="study",
        label="刷课",
        title=f"{course.name} · 本次处理 {limit} 个资源",
        note=(
            "完成状态以**回读** studyStatus==2 为准：接口返回 code=0 但回读没完成，"
            "会记在失败里，不会算成功。"
        ),
        fields=[
            _field("完成", len(report.completed)),
            _field("跳过（已完成）", len(report.skipped)),
            _field("失败（回读未完成）", len(report.failed)),
        ],
        sections=[
            _text_section("完成的资源号", "\n".join(report.completed) or "—"),
            _text_section("失败的资源号", "\n".join(report.failed) or "—"),
        ],
    )


async def _neusoft_attendance(adapter: Any, params: dict[str, Any]) -> dict[str, Any]:
    """考勤场次列表（只读）。"""
    # 延迟导入：动作层不该在模块加载期就把某个平台的包拉起来
    from kedaibiao.platforms.neusoft.attendance import is_open, is_signed, needs_location

    term_id = str(params.get("term") or "").strip()
    items = await adapter.list_attendance(term_id)
    rows = []
    for item in items:
        signed = is_signed(item)
        rows.append(
            {
                "attendance_id": item.attendance_id,
                "course": item.course_name or "—",
                "status": "已签" if signed else ("可签" if is_open(item) else "已结束"),
                "type": str(item.type or "—"),
                "location": "需定位" if needs_location(item) else "—",
            }
        )
    return table(
        platform=adapter.name,
        action="attendance",
        label="考勤",
        title=f"考勤场次（{len(rows)} 场）",
        note="只读。签到请用 sign 动作；已签 / 已结束 / 需定位缺坐标的会被拦在请求之前。",
        columns=[
            column("attendance_id", "场次号", mono=True),
            column("course", "课程"),
            column("status", "状态"),
            column("type", "类型"),
            column("location", "定位"),
        ],
        rows=rows,
        empty_hint="这个学期没有考勤记录。",
    )


async def _neusoft_score(adapter: Any, params: dict[str, Any]) -> dict[str, Any]:
    """回读某次作答的成绩（只读）。

    数据就是那张试卷的批改结果：交卷后平台把每题 ``candidateScore`` 写回，
    累加即得分。所以这里不碰任何写接口，**也不拿 0 分冒充成绩** ——
    还没批改时如实说「还没出分」。
    """
    assignment_id = str(params.get("assignment_id") or "").strip()
    if not assignment_id:
        raise ActionInputError("需要任务号（作业列表里那一列）")

    data = await adapter.fetch_score(assignment_id)
    graded = bool(data.get("graded"))
    earned = data.get("earned")
    full = data.get("full")
    pct = data.get("score_pct")
    fields = [
        _field("任务号", assignment_id),
        _field(
            "成绩",
            f"{earned} / {full}（{pct}%）" if graded else "平台还没批改，暂无分数",
        ),
        _field("已批改题数", f"{data.get('scored_questions')} / {data.get('question_count')}"),
    ]
    rows = [
        {
            "no": row.get("no"),
            "type": row.get("type"),
            "stem": row.get("stem"),
            "answer": row.get("answer") or "—",
            "score": "—" if row.get("score") is None else row.get("score"),
            "full": "—" if row.get("full") is None else row.get("full"),
        }
        for row in data.get("rows") or []
    ]
    return detail(
        platform=adapter.name,
        action="score",
        label="成绩",
        title=f"{data.get('name') or assignment_id} · 成绩",
        note=(
            "只读：这份成绩是从试卷结构里回读的（每题 candidateScore 之和）。"
            if graded
            else "只读。平台还没批改这份卷子，所以现在没有分数 —— 过一会儿再看。"
        ),
        fields=fields,
        sections=[
            _table_section(
                "逐题得分",
                [
                    column("no", "题号", mono=True),
                    column("type", "题型"),
                    column("stem", "题干"),
                    column("answer", "提交的答案"),
                    column("score", "得分", mono=True),
                    column("full", "满分", mono=True),
                ],
                rows,
            )
        ],
    )


async def _neusoft_sign(adapter: Any, params: dict[str, Any]) -> dict[str, Any]:
    """提交签到（写操作，不可逆）。"""
    attendance_id = str(params.get("attendance_id") or "").strip()
    if not attendance_id:
        raise ActionInputError("缺少场次号（attendance_id）")
    items = await adapter.list_attendance(str(params.get("term") or "").strip())
    target = next((item for item in items if item.attendance_id == attendance_id), None)
    if target is None:
        raise ActionInputError(
            f"没有找到场次 {attendance_id}（用 `action neusoft attendance` 看场次号）"
        )
    ok = await adapter.sign(
        target,
        latitude=str(params.get("latitude") or "").strip() or None,
        longitude=str(params.get("longitude") or "").strip() or None,
        address=str(params.get("address") or "").strip(),
    )
    if not ok:
        return message(
            platform=adapter.name,
            action="sign",
            label="签到",
            title=f"场次 {attendance_id} 未提交",
            message="没有提交：该场次找不到本人明细（可能不在这个教学班），或已被前置条件拦下。",
        )
    return message(
        platform=adapter.name,
        action="sign",
        label="签到",
        title=f"场次 {attendance_id} 已提交",
        message=(
            "签到请求已受理。**code=0 不等于签到落库** —— 请再跑一次 attendance 动作，"
            "看到状态变成「已签」才算真的签上。"
        ),
    )


def _register_neusoft() -> None:
    register(
        ActionSpec(
            platform="neusoft",
            name="resources",
            label="课程资源",
            description="某门课的资源清单与完成度（只读）",
            params=(ActionParam("course", "课程号或课程名", required=True),),
        ),
        _neusoft_resources,
    )
    register(
        ActionSpec(
            platform="neusoft",
            name="study",
            label="刷课",
            description="刷完指定数量未完成资源（★ 写操作；以回读状态判定完成）",
            params=(
                ActionParam("course", "课程号或课程名", required=True),
                ActionParam("limit", "本次刷几个", type="int", hint="默认 1"),
            ),
            mutates=True,
        ),
        _neusoft_study,
    )
    register(
        ActionSpec(
            platform="neusoft",
            name="attendance",
            label="考勤",
            description="本学期考勤场次与签到状态（只读）",
            params=(ActionParam("term", "学期号", hint="留空 = 最新学期"),),
        ),
        _neusoft_attendance,
    )
    register(
        ActionSpec(
            platform="neusoft",
            name="score",
            label="成绩",
            description="回读某次作答的成绩（只读；没批改时会明说）",
            params=(ActionParam("assignment_id", "任务号", required=True),),
        ),
        _neusoft_score,
    )
    register(
        ActionSpec(
            platform="neusoft",
            name="sign",
            label="签到",
            description="提交签到（★ 写操作，不可逆；需定位的场次要给坐标）",
            params=(
                ActionParam("attendance_id", "场次号", required=True),
                ActionParam("term", "学期号"),
                ActionParam("latitude", "纬度", hint="需定位场次必填"),
                ActionParam("longitude", "经度", hint="需定位场次必填"),
                ActionParam("address", "地址描述"),
            ),
            mutates=True,
        ),
        _neusoft_sign,
    )


def load_builtin_actions() -> int:
    """登记所有内置动作。新增平台在这里加一行注册函数即可。"""
    _register_pigai()
    _register_itest()
    _register_pinco()
    _register_unipus()
    _register_neusoft()
    _register_chaoxing()
    return len(_HANDLERS)


# --------------------------------------------------------------------------
# 通用动作：框架契约里本来就有的读能力，所有平台都有
# --------------------------------------------------------------------------


def _course_scoped_failure(adapter: Any, error: Exception) -> bool:
    """这门课坏了，是否允许「跳过它、继续抓别的课」。

    平台通过 ``skippable_course_errors`` 声明哪些异常是课程级业务错误
    （品课：``6569 课程不存在``）。没声明的平台保持严格 —— 一门课出错就上抛。
    登录失效 / 网络错误**永远不跳过**，否则会被误读成「这门课没有作业」。
    """
    types = tuple(getattr(adapter, "skippable_course_errors", ()) or ())
    return bool(types) and isinstance(error, types)


def _skipped_note(failures: list[str], total: int) -> str:
    if not failures:
        return ""

    def trim(text: str) -> str:
        return text if len(text) <= 40 else text[:37] + "…"

    shown = "、".join(trim(item) for item in failures[:3])
    more = f" 等 {len(failures)} 门" if len(failures) > 3 else ""
    return f"另有 {len(failures)}/{total} 门课读取失败已跳过（课程级错误）：{shown}{more}。"



def _caps(adapter: Any) -> Any:
    return getattr(adapter, "capabilities", None)


async def _generic_courses(adapter: Any, params: dict[str, Any]) -> dict[str, Any]:
    name = getattr(adapter, "name", "?")
    caps = _caps(adapter)
    if caps is not None and not getattr(caps, "list_courses", False):
        raise ActionInputError(f"{name} 不支持读取课程列表")

    courses = await adapter.list_courses()
    rows = []
    has_progress = False
    for c in courses:
        raw = c.raw if isinstance(c.raw, dict) else {}
        finish = raw.get("finish_point_num")
        total = raw.get("total_point_num")
        progress = "—"
        pct = None
        if isinstance(finish, (int, float)) and isinstance(total, (int, float)) and total:
            progress = f"{int(finish)}/{int(total)}"
            pct = round(float(finish) / float(total), 3)
            has_progress = True
        elif isinstance(finish, (int, float)) and isinstance(total, (int, float)):
            progress = f"{int(finish)}/{int(total)}"
            has_progress = True
        rows.append(
            {
                "id": c.remote_id,
                "name": c.name,
                "teacher": c.teacher or "—",
                "term": c.term or "—",
                "progress": progress,
                "progress_pct": pct,
                "activated": (
                    "已激活" if raw.get("activated") is True
                    else ("未激活" if raw.get("activated") is False else "—")
                ),
                "activated_raw": raw.get("activated"),
                "in_bookshelf": raw.get("in_bookshelf"),
            }
        )
    columns = [
        column("id", "课程号", mono=True),
        column("name", "名称"),
    ]
    if has_progress:
        columns.append(column("progress", "完成进度", mono=True))
    if any(row.get("activated") is not None for row in rows):
        columns.append(column("activated", "激活", mono=True))
    columns.extend([column("teacher", "教师"), column("term", "学期")])
    return table(
        platform=name,
        action="courses",
        label="课程",
        title=f"课程列表（{len(rows)} 门）",
        columns=columns,
        rows=rows,
        empty_hint="该平台没有返回任何课程。",
    )


async def _list_assignments_maybe_with_state(adapter: Any, course: Any) -> list[Any]:
    """读作业列表；适配器支持 ``with_state`` 就顺便回读完成度。

    U校园的目录接口本身没有完成度，``finished`` 必须逐组回读 ``progress/v2``
    （N 次请求，顺序执行）。所以默认不读，只有显式要求时才付这个代价。
    """
    import inspect

    try:
        supported = "with_state" in inspect.signature(adapter.list_assignments).parameters
    except (TypeError, ValueError):  # C 扩展/装饰器包裹时拿不到签名
        supported = False
    if supported:
        return list(await adapter.list_assignments(course, with_state=True))
    return list(await adapter.list_assignments(course))


async def _generic_assignments(adapter: Any, params: dict[str, Any]) -> dict[str, Any]:
    name = getattr(adapter, "name", "?")
    caps = _caps(adapter)
    for capability in ("list_courses", "list_assignments"):
        if caps is not None and not getattr(caps, capability, False):
            raise ActionInputError(f"{name} 不支持读取作业列表")

    # 状态列默认不读：平台目录本身没有完成度，回读要逐组打接口（很慢）。
    # 读不到就显示「—」，绝不默认写成「未完成」—— 那会把已提交的作业报成没做。
    with_state = _unipus_flag(params, "with_state")

    courses = list(await adapter.list_courses())
    only = str(params.get("course") or "").strip()
    if only:
        # 课程号可能带班级后缀（学习通是 ``courseid:clazzid``），两种写法都认
        courses = [
            c for c in courses
            if c.remote_id == only or c.remote_id.split(":")[0] == only
        ]
        if not courses:
            raise ActionInputError(f"没有找到课程号 {only!r}")

    rows: list[dict[str, Any]] = []
    failures: list[str] = []
    for course in courses:
        try:
            items = (
                await _list_assignments_maybe_with_state(adapter, course)
                if with_state else list(await adapter.list_assignments(course))
            )
        except Exception as error:  # noqa: BLE001 - 只跳平台声明的课程级错误
            if only or not _course_scoped_failure(adapter, error):
                raise
            failures.append(f"{course.name or course.remote_id}（{error}）")
            continue
        for item in items:
            raw = item.raw or {}
            rows.append(
                {
                    "course": course.name or course.remote_id,
                    "id": item.remote_id,
                    # 前端双击详情 / 提交都按 assignment_id 调接口；
                    # 这里显式给一份，免得前端去猜列名映射。
                    "assignment_id": item.remote_id,
                    "title": item.title,
                    "kind": _KIND_LABEL.get(item.kind, item.kind.value),
                    "deadline": _fmt_deadline(item.deadline) or "—",
                    "state": _assignment_state(item, with_state=with_state),
                    "score": str(raw.get("score_text") or ""),
                    # 让前端知道这行能不能重考 / 还剩几次（不额外开页面）
                    "retake": bool(raw.get("retake")),
                    "retake_exam_id": str(raw.get("retake_exam_id") or ""),
                    "retake_relation_id": str(raw.get("retake_relation_id") or ""),
                    "answer_id": str(raw.get("relation_id") or ""),
                    "attempts_text": str(raw.get("attempts_text") or ""),
                }
            )

    scope = f"（课程 {only}）" if only else "（全部课程）"
    return table(
        platform=name,
        action="assignments",
        label="作业",
        title=f"作业 / 任务{scope} —— {len(rows)} 项",
        note=(
            "只读数据，不会提交任何内容。"
            + (
                "学习通的作业/测验/考试来自课程页「考试」tab，"
                "状态与分数是平台自己回的（不需要回读）。"
                if name == "chaoxing" else
                (
                    "状态列按逐组回读的 progress/v2 判定（已完成 = 平台已记账）。"
                    if with_state else
                    "状态列默认不读：平台目录本身没有完成度，这里显示「—」；"
                    "要看真实状态请传 with_state=1（逐组回读，慢但准）。"
                )
            )
            + _skipped_note(failures, len(courses))
        ),
        columns=[
            column("course", "课程"),
            column("id", "任务号", mono=True),
            column("title", "名称"),
            column("kind", "类型"),
            column("deadline", "截止"),
            column("state", "状态"),
        ]
        + ([column("score", "分数")] if any(row.get("score") for row in rows) else []),
        rows=rows,
        empty_hint="这些课程下没有作业 / 任务。",
    )




async def _generic_assignment_detail(adapter: Any, params: dict[str, Any]) -> dict[str, Any]:
    """作业详情（双击作业行时调用）。

    只读：把该作业的题目、题型、选项列出来，并把平台原始载荷放进 sections，
    方便排查「为什么这题没答出来」。
    """
    name = getattr(adapter, "name", "?")
    assignment_id = str(params.get("assignment_id") or "").strip()
    if not assignment_id:
        raise ActionInputError("缺少 assignment_id")

    # 先在所有课程里把这个作业找出来
    target = None
    course_name = ""
    for course in await adapter.list_courses():
        for item in await adapter.list_assignments(course):
            if item.remote_id == assignment_id:
                target, course_name = item, course.name
                break
        if target is not None:
            break
    if target is None:
        raise ActionInputError(f"在 {name} 里找不到作业 {assignment_id!r}")

    questions = list(target.questions)
    if not questions and getattr(adapter.capabilities, "fetch_questions", False):
        questions = await adapter.fetch_questions(target)

    fields = [
        {"label": "任务号", "value": target.remote_id},
        {"label": "名称", "value": target.title or "—"},
        {"label": "课程", "value": course_name or "—"},
        {"label": "类型", "value": _KIND_LABEL.get(target.kind, target.kind.value)},
        {"label": "截止", "value": _fmt_deadline(target.deadline) or "—"},
        # 目录接口没回读完成度时（raw.state_unconfirmed）不能写成「未完成」，
        # 那会把已提交的作业报成没做；显示「未核实」，让用户去勾「读取完成状态」。
        {"label": "状态", "value": (
            "未核实"
            if (target.raw or {}).get("state_unconfirmed")
            else ("已完成" if target.finished else "未完成")
        )},
        {"label": "进度", "value": f"{target.progress:.0%}"},
        {"label": "题目数", "value": len(questions)},
    ]
    # 平台自己的题型（目前 U校园提供：听力/词汇/阅读/翻译/写作/单元测验/口语/上传）
    platform_type = (target.raw or {}).get("type_label") if isinstance(target.raw, dict) else None
    if platform_type:
        fields.insert(4, {"label": "任务题型", "value": str(platform_type)})

    sections: list[dict[str, Any]] = []
    if questions:
        rows = []
        for i, q in enumerate(questions):
            raw_q = q.raw if isinstance(q.raw, dict) else {}
            # 平台题型（U校园有 unipus_type；其它平台回落到核心题型）
            type_text = str(raw_q.get("unipus_type") or q.type.value)
            if raw_q.get("unipus_type") and raw_q.get("reply_type"):
                type_text = f"{raw_q['unipus_type']}（{raw_q['reply_type']}）"
            rows.append({
                "index": i + 1,
                "type": type_text,
                "stem": q.stem or "（平台未提供题干）",
                "options": " / ".join(
                    f"{n}. {o}" for n, o in enumerate(q.options)
                ) or "—",
            })
        sections.append(
            _table_section(
                "题目",
                [
                    column("index", "#"),
                    column("type", "题型"),
                    column("stem", "题干"),
                    column("options", "选项"),
                ],
                rows,
            )
        )

    # 平台原始载荷：排查用，默认收起
    if target.raw:
        sections.append({
            "label": "平台原始数据",
            "kind": "text",
            "text": json.dumps(target.raw, ensure_ascii=False, indent=2)[:4000],
        })

    return detail(
        platform=name,
        action="detail",
        label="作业详情",
        title=f"{target.title or target.remote_id}",
        note="只读数据，不会提交任何内容。" if not questions else "",
        fields=fields,
        sections=sections,
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
    if not has_action(platform, "detail"):
        register(
            ActionSpec(
                platform=platform,
                name="detail",
                label="作业详情",
                description="查看单个作业的题目与平台原始数据（只读）",
                builtin=True,
                params=(ActionParam("assignment_id", "任务号", required=True),),
            ),
            _generic_assignment_detail,
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
                    ActionParam(
                        "with_state", "读取完成状态",
                        hint="1 = 逐组回读 progress/v2（慢，但状态是真实的）；"
                             "0 = 不读，状态列显示 —",
                    ),
                ),
            ),
            _generic_assignments,
        )


# ---------------------------------------------------------------------------
# 超星 / 学习通：任务点、刷课、考试、章节测验
# ---------------------------------------------------------------------------


def _assignment_state(item: Any, *, with_state: bool) -> str:
    """作业行的状态文案：能确定就说确定，不能就显示「—」。"""
    raw = item.raw or {}
    status = str(raw.get("status") or "").strip()
    if status:
        # 平台自己给了状态（学习通：考试 tab 的待做/已完成/已过期）
        return {
            "done": "已完成",
            "expired": "已过期",
            "todo": "未完成",
            "quiz": "章节测验",
        }.get(
            status, "未完成" if not item.finished else "已完成"
        )
    if with_state:
        return "已完成" if item.finished else "未完成"
    return "—"


def _chaoxing_confirm(params: dict[str, Any]) -> bool:
    """写操作闸门：``confirm`` 填 true/1/yes/确认 才真写。"""
    return str(params.get("confirm") or "").strip().lower() in {
        "1", "true", "yes", "y", "on", "确认", "我确认",
    }


async def _chaoxing_course(adapter: Any, params: dict[str, Any]) -> Any:
    """按课程号或课程名找课（找不到就报清楚，不做模糊猜测）。"""
    ref = str(params.get("course") or "").strip()
    courses = list(await adapter.list_courses())
    if not courses:
        raise ActionInputError("学习通没有返回任何课程（会话可能已失效，请重新登录）")
    if not ref:
        if len(courses) == 1:
            return courses[0]
        names = "、".join(f"{c.name}（{c.remote_id}）" for c in courses[:8])
        raise ActionInputError(f"请指定课程号或课程名，可选：{names}")
    for course in courses:
        if ref == course.remote_id or ref in course.remote_id:
            return course
    for course in courses:
        if ref in (course.name or ""):
            return course
    options = "、".join(f"{course.name}（{course.remote_id}）" for course in courses[:8])
    raise ActionInputError(f"没有找到课程 {ref!r}；可选：{options}")


async def _chaoxing_tasks(adapter: Any, params: dict[str, Any]) -> dict[str, Any]:
    """任务点清单（只读）：视频/文档/测验，标出完成状态与时长。"""
    course = await _chaoxing_course(adapter, params)
    points = await adapter.automation.task_points(course, only_pending=False)
    rows = [
        {
            "kind": point.kind,
            "section": point.index_text,
            "name": point.name,
            "minutes": round(point.duration / 60) if point.duration else "—",
            "status": "已完成" if point.passed is True else (
                "待完成" if point.kind != "quiz" else "测验"
            ),
            "chapter_id": point.chapter_id,
        }
        for point in points
    ]
    pending = sum(1 for point in points if point.passed is not True and point.kind != "quiz")
    return table(
        platform=adapter.name,
        action="tasks",
        label="任务点",
        title=f"{course.name} · 任务点 {len(points)} 个（待完成 {pending}）",
        note="只读。刷课用 study 动作；章节测验用 quiz 动作（chapter_id 就是这里的值）。",
        columns=[
            column("kind", "类型"),
            column("section", "小节"),
            column("name", "名称"),
            column("minutes", "时长(分)", mono=True),
            column("status", "状态"),
            column("chapter_id", "小节号", mono=True),
        ],
        rows=rows,
        empty_hint="这门课没有可识别的任务点。",
    )


async def _chaoxing_study(adapter: Any, params: dict[str, Any]) -> dict[str, Any]:
    """刷课（写操作）。默认干跑只列计划，``confirm=true`` 才真播。"""
    course = await _chaoxing_course(adapter, params)
    limit = _int_param(params, "limit", default=1)
    kinds = tuple(
        item.strip() for item in str(params.get("kinds") or "video,document").split(",")
        if item.strip()
    )
    rate = float(params.get("rate") or 2.0)
    points = await adapter.automation.task_points(course, only_pending=True)
    targets = [point for point in points if point.kind in kinds]
    planned = targets[:limit] if limit > 0 else targets
    rows = [
        {
            "kind": point.kind,
            "section": point.index_text,
            "name": point.name,
            "minutes": round(point.duration / 60) if point.duration else "—",
        }
        for point in planned
    ]
    if not _chaoxing_confirm(params):
        return table(
            platform=adapter.name,
            action="study",
            label="刷课",
            title=f"{course.name} · 计划刷 {len(planned)} 个任务点（干跑）",
            note=(
                "这是写操作的计划。核对无误后把 confirm 填 true 再执行一次才会真的播放。"
                "视频按 2 倍速播放、进度由播放器自己上报，完成判定回读平台记录。"
            ),
            columns=[
                column("kind", "类型"),
                column("section", "小节"),
                column("name", "名称"),
                column("minutes", "时长(分)", mono=True),
            ],
            rows=rows,
            empty_hint="没有待完成的任务点。",
        )
    result = await adapter.automation.play_course(
        course,
        only_pending=True,
        limit=limit,
        kinds=kinds,
        rate=rate,
        emit=lambda level, message: log.info("[chaoxing] %s %s", level, message),
    )
    return detail(
        platform=adapter.name,
        action="study",
        label="刷课",
        title=f"{course.name} · {result['summary']}",
        note="完成判定以回读 isPassed / 章节树为准；播完但平台没记账会标「部分」。",
        fields=[_field("处理任务点", len(result["steps"]))],
        sections=[
            _text_section(
                "明细",
                "\n".join(
                    f"[{step['status']}] {step['section']} {step['task']} "
                    f"{step['detail']}（{step['seconds']:.0f}s）"
                    for step in result["steps"]
                ) or "—",
            )
        ],
    )


async def _chaoxing_exams(adapter: Any, params: dict[str, Any]) -> dict[str, Any]:
    """考试 / 作业列表（只读），含状态、截止时间与已回显的分数。"""
    course = await _chaoxing_course(adapter, params)
    items = await adapter.automation.list_exams(course)
    rows = [
        {
            "title": item.title,
            "status": {"todo": "待做", "done": "已完成", "expired": "已过期"}.get(
                item.status, item.status
            ),
            "end_time": item.end_time_text or "—",
            "score": item.score_text or "—",
            "mobile": "只允许手机APP" if item.mobile_only else "",
            "exam_id": item.exam_id,
        }
        for item in items
    ]
    return table(
        platform=adapter.name,
        action="exams",
        label="考试 / 作业",
        title=f"{course.name} · {len(items)} 场",
        note="只读。做考试用 exam 动作（exam_id 就是这里的值，默认干跑）。",
        columns=[
            column("title", "名称"),
            column("status", "状态"),
            column("end_time", "截止"),
            column("score", "分数"),
            column("mobile", "限制"),
            column("exam_id", "考试号", mono=True),
        ],
        rows=rows,
        empty_hint="这门课没有考试/作业。",
    )


async def _chaoxing_exam(adapter: Any, params: dict[str, Any]) -> dict[str, Any]:
    """做一场考试（写操作）：默认干跑只作答不交卷，confirm=true 才交卷。"""
    course = await _chaoxing_course(adapter, params)
    exam_id = str(params.get("exam_id") or "").strip()
    if not exam_id:
        raise ActionInputError("需要 exam_id（先用 exams 动作列出考试号）")
    confirm = _chaoxing_confirm(params)
    result = await adapter.automation.run_exam(
        course,
        exam_id=exam_id,
        title=str(params.get("title") or ""),
        relation_id=str(params.get("relation_id") or ""),
        confirm=confirm,
        emit=lambda level, message: log.info("[chaoxing] %s %s", level, message),
    )
    return _exam_detail(adapter.name, result, confirm=confirm)


async def _chaoxing_quiz(adapter: Any, params: dict[str, Any]) -> dict[str, Any]:
    """做章节测验（写操作）：默认干跑填卷+核验，confirm=true 才交卷。"""
    course = await _chaoxing_course(adapter, params)
    chapter_id = str(params.get("chapter") or "").strip()
    if not chapter_id:
        raise ActionInputError("需要 chapter（章节测验的小节 id，先用 tasks 动作查看）")
    confirm = _chaoxing_confirm(params)
    result = await adapter.automation.run_section_quiz(
        course, chapter_id=chapter_id, confirm=confirm,
        emit=lambda level, message: log.info("[chaoxing] %s %s", level, message),
    )
    return _exam_detail(adapter.name, result, confirm=confirm)


def _exam_detail(platform: str, result: dict[str, Any], *, confirm: bool) -> dict[str, Any]:
    fields = [
        _field("题数", len(result.get("items") or [])),
        _field(
            "作答",
            sum(1 for item in result.get("items") or [] if item.get("answer")),
        ),
        _field("交卷", "已交卷" if result.get("submitted") else "未交卷（干跑）"),
    ]
    if result.get("score_text"):
        fields.append(_field("分数", result["score_text"]))
    elif result.get("submit_note"):
        fields.append(_field("备注", result["submit_note"]))
    rows = "\n".join(
        f"{item['number']}. {item['type']} → {item['answer'] or '（未答）'}"
        f"（{item['confidence']:.2f} {item['source']}）"
        + (f"  ⚠ {item['unsupported']}" if item.get("unsupported") else "")
        for item in result.get("items") or []
    )
    note = (
        "已交卷，分数来自平台回显。" if result.get("submitted")
        else "干跑：已填卷并核验，但**没有交卷**。确认无误后把 confirm 填 true 再执行一次。"
        if not confirm else "交卷流程已执行，详见备注。"
    )
    return detail(
        platform=platform,
        action="exam" if confirm else "exam",
        label="考试 / 测验",
        title=f"{result.get('title') or '考试'} · {result.get('summary', '')}",
        note=note,
        fields=fields,
        sections=[_text_section("逐题结果", rows or "—")],
    )


def _int_param(params: dict[str, Any], name: str, *, default: int = 0) -> int:
    raw = str(params.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ActionInputError(f"{name} 必须是整数，收到 {raw!r}") from exc


def _register_chaoxing() -> None:
    register(
        ActionSpec(
            platform="chaoxing",
            name="tasks",
            label="任务点",
            description="某门课的视频/文档/测验任务点与完成状态（只读）",
            params=(ActionParam("course", "课程号或课程名", required=True),),
        ),
        _chaoxing_tasks,
    )
    register(
        ActionSpec(
            platform="chaoxing",
            name="study",
            label="刷课",
            description="播放视频/阅读文档（★ 写操作；默认干跑，confirm=true 才真播）",
            params=(
                ActionParam("course", "课程号或课程名", required=True),
                ActionParam("limit", "本次处理几个", type="int", hint="默认 1"),
                ActionParam("kinds", "类型", hint="默认 video,document"),
                ActionParam("rate", "倍速", hint="默认 2（平台上限）"),
                ActionParam("confirm", "确认", hint="true 才真的播放"),
            ),
            mutates=True,
        ),
        _chaoxing_study,
    )
    register(
        ActionSpec(
            platform="chaoxing",
            name="exams",
            label="考试/作业",
            description="某门课的考试与作业列表（只读，含分数与截止时间）",
            params=(ActionParam("course", "课程号或课程名", required=True),),
        ),
        _chaoxing_exams,
    )
    register(
        ActionSpec(
            platform="chaoxing",
            name="exam",
            label="做考试",
            description="作答并交卷（★ 写操作；默认干跑，confirm=true 才交卷）",
            params=(
                ActionParam("course", "课程号或课程名", required=True),
                ActionParam("exam_id", "考试号", required=True),
                ActionParam("title", "名称", hint="只用于显示"),
                ActionParam("relation_id", "作答记录号"),
                ActionParam("confirm", "确认", hint="true 才交卷（不可逆）"),
            ),
            mutates=True,
        ),
        _chaoxing_exam,
    )
    register(
        ActionSpec(
            platform="chaoxing",
            name="quiz",
            label="章节测验",
            description="做章节测验（★ 写操作；默认干跑，confirm=true 才交卷）",
            params=(
                ActionParam("course", "课程号或课程名", required=True),
                ActionParam("chapter", "小节 id", required=True),
                ActionParam("confirm", "确认", hint="true 才交卷（不可逆）"),
            ),
            mutates=True,
        ),
        _chaoxing_quiz,
    )
