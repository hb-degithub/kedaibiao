"""响应解析：真实信封 + 课程 / 班级 / 作业 / 题目 / 提交状态。

字段名与结构来自 Lead 的真实会话只读实测（样本见 ``tests/fixtures/pinco/``），
证据等级记在 :mod:`endpoints` 的 ``EVIDENCE`` 里。

**信封（实测，最容易踩的一个坑）**：应用层成功响应是

    {"statusCode": 0, "data": {...}, "message": "ok"}

—— **没有 ``code`` 字段**。只认 ``code`` 会把每一次成功都判成「未通过鉴权」。
另外两种形态也要认：REST 家族的 ``{"code": 403, "msg": "access forbidden"}``，
以及未登录的 ``{"message": "missing csrf token"}``（HTTP 200）。

三条纪律：

1. 结构不符抛 :class:`PincoProtocolError`，**绝不静默返回空列表**；
2. 没有 ID 的条目丢掉并告警（没有 ID 就无法幂等）；整批都没 ID 就抛错；
3. 未知的题目分组按空处理**并告警** —— 新分组出现时要看得见，而不是当成
   「这份作业没有题目」。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from kedaibiao.core.models import (
    Assignment,
    Course,
    Question,
    QuestionType,
    SubmissionResult,
    TaskKind,
)
from kedaibiao.platforms.pinco import endpoints as ep
from kedaibiao.platforms.pinco import question_map, richtext
from kedaibiao.platforms.pinco.errors import PincoApiError, PincoNotLoggedIn, PincoProtocolError
from kedaibiao.platforms.pinco.models import (
    PincoClass,
    PincoMaterialDetail,
    PincoSubmissionState,
)

log = logging.getLogger(__name__)

#: 分页列表的元素数组可能在哪些键下面。``content`` 是实测（课程与作业列表都用它，
#: 且**没有 total 字段**，只有 content/size）。
_LIST_KEYS = (
    ep.FIELD_CONTENT,
    "list",
    "records",
    "rows",
    "items",
    "homeworks",
    "homeworkList",
    "courses",
    "courseList",
    "classes",
    "classList",
)

#: 题目分组 → 核心题型。**只列实测响应里出现过的两个**；别的分组名不猜。
_GROUP_TYPES: dict[str, QuestionType] = {
    ep.FIELD_SHORT_ANSWERS: QuestionType.ESSAY,
    ep.FIELD_SINGLE_CHOICES: QuestionType.SINGLE,
}

#: 视作「已完成」的字面量。
_FINISHED_LITERALS = frozenset(
    {"1", "true", "yes", "on", "finished", "done", "completed", "submitted", "已提交", "已完成"}
)


# --------------------------------------------------------------------------
# 信封
# --------------------------------------------------------------------------


def _success(
    family: ep.Protocol, status: Any, code: Any
) -> tuple[bool | None, str]:
    """按家族判定「这次算不算成功」，返回 (结论, 依据字段名)。

    ``None`` 表示两个字段都没有、判不了（调用方按协议错误处理 —— 绝不能兜底成成功）。

    - **RPC 家族**：``statusCode`` 是权威，必须恰好 0；它缺失时才退回 ``code``
      （网关偶尔只回 ``code``）。
    - **REST 家族**：``code`` 是权威，必须 0；``statusCode`` 在这里是 **HTTP 镜像
      200**，拿它当应用层码就会把每次成功都判成失败（health_check 恒假就是这么来的）。
      ``code`` 缺失时才退回 ``statusCode``。
    """
    if family is ep.Protocol.REST:
        if code is not None:
            return code in ep.SUCCESS_CODES, ep.FIELD_CODE
        if status is not None:
            return status in ep.SUCCESS_CODES, ep.FIELD_STATUS_CODE
        return None, ""
    if status is not None:
        return status == ep.SUCCESS_CODE, ep.FIELD_STATUS_CODE
    if code is not None:
        return code in ep.SUCCESS_CODES, ep.FIELD_CODE
    return None, ""


def unwrap_envelope(
    payload: Any,
    *,
    action: str = "",
    http_status: int = 0,
    family: ep.Protocol = ep.Protocol.WEB,
) -> Any:
    """校验品课信封并取出 ``data``。``family`` 决定用哪个字段判成功。

    判定顺序（每条都对应实测过的真实形态）：

    0. ``401 illegal argument`` / ``code == "ERR_ASSERTION"`` → 各自专用异常；
    1. ``statusCode`` / ``code`` 是 401/403，或 HTTP 状态是 401/403 → 登录态问题
       （**这一步与家族无关**：两族的鉴权失败码一致）；
    2. 按家族判定成功（见 :func:`_success`）；
    3. 两个字段都没有、只有 ``message`` → 实测 ``missing csrf token`` 形态按登录态
       处理，其余按协议错误（结构不认识）；
    4. 判失败 → :class:`PincoApiError`，报错里带**依据字段名与家族**。

    **绝不把失败当成空数据，也绝不把「判不了」当成成功。**
    """
    if not isinstance(payload, dict):
        raise PincoProtocolError(
            f"{action or '品课'} 的响应不是对象（{type(payload).__name__}），无法解析信封。"
        )

    status = payload.get(ep.FIELD_STATUS_CODE)
    code = payload.get(ep.FIELD_CODE)
    message = _as_str(payload.get(ep.FIELD_MSG)) or _as_str(payload.get(ep.FIELD_MESSAGE))
    http_auth = http_status in ep.AUTH_STATUS_CODES

    # 实测：401 也可能是「参数不对」（``401 illegal argument``）而不是登录态问题。
    # 这类是我们自己的调用错误，必须报成协议错误 —— 否则会被当成会话失效去重登，
    # 排查方向直接跑偏。
    if "illegal argument" in message.lower():
        raise PincoProtocolError(
            f"{action or '品课'} 参数不合法：{message}"
            "（缺必填参数？实测报错文本常直接点出参数名）。"
        )
    # 实测：action 名不存在时回 ``{"code":"ERR_ASSERTION","message":"Internal Server Error"}``，
    # 与「缺必需请求头（如 x-server）」形状相同 —— 两种原因都要靠上下文区分。
    if code == "ERR_ASSERTION":
        raise PincoApiError(
            f"{action or '品课'} 服务端断言失败（ERR_ASSERTION）："
            f"{message or 'Internal Server Error'}。"
            "实测经验：action 名写错、或缺必需请求头（x-server 等）都是这个形状。",
            code=code,
        )

    # 鉴权失败两族一致：statusCode / code 里出现 401/403，或 HTTP 状态是 401/403。
    for label, value in (("statusCode", status), ("code", code)):
        if value in ep.AUTH_STATUS_CODES:
            raise PincoNotLoggedIn(
                f"{action or '品课'} 返回 {label}={value}：{message or '权限不足'}。"
                f"请确认登录态（{ep.HEADER_AUTHORIZATION} / {ep.HEADER_CSRF}）是否有效。"
            )
    if http_auth:
        raise PincoNotLoggedIn(
            f"{action or '品课'} 返回 HTTP {http_status}：{message or '权限不足'}。"
            f"请确认登录态（{ep.HEADER_AUTHORIZATION} / {ep.HEADER_CSRF}）是否有效。"
        )

    ok, field = _success(family, status, code)
    if ok is None:
        if any(hint in message.lower() for hint in ep.AUTH_MESSAGE_HINTS):
            raise PincoNotLoggedIn(
                f"{action or '品课'} 未通过鉴权：{message}"
                f"（请确认 cookie {ep.CSRF_COOKIE} 已带上，值放进 {ep.HEADER_CSRF} 头。"
                "若刚做过密码登录：SSO 成功不等于应用层可用 —— csrf 与浏览器会话"
                "上下文绑定，纯 HTTP 拿不到，请用浏览器导出的 cookie 接管会话）。"
            )
        raise PincoProtocolError(
            f"{action or '品课'} 的响应里既没有 statusCode 也没有 code，"
            f"无法判断结果。字段：{sorted(payload)[:8]}"
        )

    if not ok:
        decided = status if field == ep.FIELD_STATUS_CODE else code
        raise PincoApiError(
            f"{action or '品课'} 返回 {field}={decided!r}：{message or '未知错误'}"
            f"（{family.value} 家族按 {field} 判成功）",
            code=decided,
        )

    data = payload.get(ep.FIELD_DATA)
    return {} if data is None else data


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------


def _first(item: dict[str, Any], keys: tuple[str, ...]) -> Any:
    """按声明顺序取第一个存在的非 None 字段值。

    ``False`` 与 ``0`` 是合法取值，不能被当成缺失 —— 否则 ``submitted: false``
    会被误读成「没有这个字段」。
    """
    for key in keys:
        if key in item and item[key] is not None:
            return item[key]
    return None


def _as_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value).strip()


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = _as_str(value).rstrip("%")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _as_bool(value: Any, *, true_literals: frozenset[str] = _FINISHED_LITERALS) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value == 1
    return _as_str(value).lower() in true_literals


def _as_time(value: Any) -> datetime | None:
    """时间字段 → ``datetime``；认不出来返回 None。

    实测：``deadline`` / ``sendTime`` / ``createTime`` 都是 **epoch 毫秒**。
    认不出来返回 None 而不是抛错：截止时间缺失不该让整份作业作废。
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        seconds = float(value)
        if seconds > 1e11:  # 毫秒时间戳（当前 epoch 秒约 1.7e9）
            seconds /= 1000.0
        try:
            return datetime.fromtimestamp(seconds)
        except (OverflowError, OSError, ValueError):
            return None
    text = _as_str(value)
    if not text:
        return None
    normalised = text.replace("/", "-")
    if normalised.endswith("Z"):
        normalised = normalised[:-1]
    try:
        return datetime.fromisoformat(normalised)
    except ValueError:
        return None


def _nested(item: dict[str, Any], *path: str) -> Any:
    """按路径取嵌套字段，中途不是 dict 就返回 None。"""
    current: Any = item
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def parse_deadline(data: Any) -> datetime | None:
    """从作业详情 / 题目响应里取截止时间（实测是 epoch 毫秒）。取不到返回 None。"""
    if not isinstance(data, dict):
        return None
    return _as_time(_first(data, (ep.FIELD_DEADLINE, ep.FIELD_END_TIME)))


def _as_progress(value: Any) -> float:
    """进度归一化到 0.0~1.0；大于 1 的一律按百分比处理。"""
    number = _as_float(value)
    if number is None:
        return 0.0
    if number > 1.0:
        number /= 100.0
    return min(max(number, 0.0), 1.0)


def iter_items(data: Any, *keys: str) -> list[dict[str, Any]]:
    """从已剥信封的数据里取出「对象列表」。

    先看调用方给的候选键与 :data:`_LIST_KEYS`，再兜底找唯一一个列表字段；
    找不到就抛错 —— 例如响应其实是登录页被解析成对象的情况。
    """
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if not isinstance(data, dict):
        raise PincoProtocolError(f"预期列表或对象，实际是 {type(data).__name__}。")

    for key in (*keys, *_LIST_KEYS):
        value = data.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    lists = [value for value in data.values() if isinstance(value, list)]
    if len(lists) == 1:
        return [item for item in lists[0] if isinstance(item, dict)]
    if not lists:
        raise PincoProtocolError(
            "响应里没有任何列表字段，无法确定数据结构"
            f"（候选键：{', '.join((*keys, *_LIST_KEYS))}）。"
        )
    raise PincoProtocolError(
        f"响应里有 {len(lists)} 个列表字段，无法确定用哪一个；"
        f"请在解析器里补上真实字段名（候选：{', '.join(keys)}）。"
    )


# --------------------------------------------------------------------------
# 课程 / 班级 / 作业
# --------------------------------------------------------------------------


def parse_courses(data: Any) -> list[Course]:
    """解析课程列表。实测：``data.content[]``，字段 ``courseName``/``courseId``。"""
    items = iter_items(data, "courses", "courseList")
    courses: list[Course] = []
    for item in items:
        remote_id = _as_str(_first(item, (ep.FIELD_COURSE_ID, ep.FIELD_CLASS_ID, "id")))
        if not remote_id:
            log.warning("品课课程缺少 ID，已跳过：%s", sorted(item)[:8])
            continue
        courses.append(
            Course(
                remote_id=remote_id,
                name=_as_str(_first(item, (ep.FIELD_COURSE_NAME, "name", ep.FIELD_TITLE))),
                teacher=_as_str(
                    _first(item, (ep.FIELD_CREATOR_NAME,))
                    or _nested(item, ep.FIELD_CREATOR_USER, ep.FIELD_CREATOR_NAME)
                ),
                raw=item,
            )
        )
    if items and not courses:
        raise PincoProtocolError(
            "拿到了课程列表，但没有一条含可用 ID —— 多半是字段名与样本不符"
            f"（已核对：{ep.FIELD_COURSE_ID}/{ep.FIELD_COURSE_NAME}）。"
        )
    return courses


def parse_classes(data: Any) -> list[PincoClass]:
    """解析班级列表。实测：``data[]`` 是**裸数组**，字段 ``classId``/``className``。"""
    items = iter_items(data, "classes", "classList")
    classes: list[PincoClass] = []
    for item in items:
        class_id = _as_str(_first(item, (ep.FIELD_CLASS_ID, "id")))
        if not class_id:
            log.warning("品课班级缺少 ID，已跳过：%s", sorted(item)[:8])
            continue
        student_count = _as_float(_first(item, (ep.FIELD_STUDENT_COUNT,)))
        classes.append(
            PincoClass(
                class_id=class_id,
                name=_as_str(_first(item, (ep.FIELD_CLASS_NAME, "name"))),
                teacher=_as_str(_first(item, (ep.FIELD_CREATOR_NAME,))),
                student_count=int(student_count) if student_count is not None else None,
                raw=item,
            )
        )
    if items and not classes:
        raise PincoProtocolError(
            "拿到了班级列表，但没有一条含可用 ID —— 多半是字段名与样本不符"
            f"（已核对：{ep.FIELD_CLASS_ID}/{ep.FIELD_CLASS_NAME}）。"
        )
    return classes


def assignment_blocker(item: dict[str, Any]) -> str:
    """这个作业当前能不能提交；返回空串表示可以。

    实测事实：``submitted`` / ``expired`` / ``canSubmitted`` **三个标记互相独立**，
    存在「submitted=false 但 expired=true、canSubmitted=false」的作业。只看
    ``submitted`` 会把它当成「可以作答」，于是白抓一遍题目、还可能踩到提交被拒。
    """
    if _as_bool(item.get(ep.FIELD_SUBMITTED)):
        return "平台显示已提交"
    if _as_bool(item.get(ep.FIELD_EXPIRED)):
        return "平台显示已过期"
    can_submit = item.get(ep.FIELD_CAN_SUBMITTED)
    if can_submit is not None and not _as_bool(can_submit):
        return f"平台显示当前不可提交（{ep.FIELD_CAN_SUBMITTED}=false）"
    if _as_bool(item.get(ep.FIELD_REJECTED)):
        return "平台显示该作业已被驳回，需人工处理"
    return ""


def parse_assignments(
    data: Any, *, course: Course | None = None, default_kind: TaskKind = TaskKind.HOMEWORK
) -> list[Assignment]:
    """解析作业列表。实测：``data.content[]``，主键 ``studentHomeworkId``。

    ``remote_id`` 同时是幂等键，缺了会导致重复提交，所以没有 ID 的条目丢掉并告警；
    整批都没 ID 说明字段名不符，直接抛错。

    列表里**没有** ``deadline``（详情与题目接口才有），所以这里留空；
    :meth:`PincoAdapter.fetch_questions` 会顺手从题目响应里补上。
    """
    items = iter_items(data, "homeworks", "homeworkList")
    assignments: list[Assignment] = []
    for item in items:
        remote_id = _as_str(
            _first(
                item,
                (
                    ep.FIELD_STUDENT_HOMEWORK_ID,
                    ep.FIELD_CHAPTER_TASK_ID,
                    ep.FIELD_COURSE_GROUP_TASK_ID,
                ),
            )
        )
        if not remote_id:
            log.warning("品课作业/学习任务缺少 ID，已跳过：%s", sorted(item)[:8])
            continue
        assignments.append(
            Assignment(
                remote_id=remote_id,
                title=_as_str(_first(item, (ep.FIELD_TITLE, "name"))) or remote_id,
                kind=default_kind,
                course_name=(
                    _as_str(
                        _first(item, (ep.FIELD_COURSE_NAME,))
                        or _nested(item, ep.FIELD_COURSE, ep.FIELD_COURSE_NAME)
                    )
                    or (course.name if course else "")
                ),
                deadline=_as_time(_first(item, (ep.FIELD_DEADLINE,))),
                progress=0.0,
                finished=_as_bool(_first(item, (ep.FIELD_SUBMITTED,))),
                raw=item,
            )
        )
    if items and not assignments:
        raise PincoProtocolError(
            "拿到了作业列表，但没有一条含可用 ID —— 多半是字段名与样本不符"
            f"（已核对：{ep.FIELD_STUDENT_HOMEWORK_ID}）。"
        )
    return assignments


# --------------------------------------------------------------------------
# 题目
# --------------------------------------------------------------------------


def _parse_options(item: dict[str, Any]) -> list[str]:
    """选项统一成字符串列表，**保持原始顺序** —— 顺序就是选项序号。

    实测形态：``optionItems: [{"option": "A", "content": "<p>…</p>"}, …]``。
    按 ``option`` 字母排序，避免平台乱序时把选项序号对错。
    """
    option_items = item.get(ep.FIELD_OPTION_ITEMS)
    if isinstance(option_items, list):
        pairs: list[tuple[str, str]] = []
        for entry in option_items:
            if isinstance(entry, dict):
                letter = _as_str(entry.get(ep.FIELD_OPTION))
                raw = _as_str(entry.get(ep.FIELD_CONTENT))
            else:
                letter, raw = "", _as_str(entry)
            text = richtext.strip_html(raw)
            if not text and richtext.image_kind(raw):
                # 选项本身就是一张图（实测：选项里只有 rich-formula 图片）：
                # 给个明确标注，别在计划里渲染成空字符串让人以为数据坏了。
                text = "(图片选项)"
            pairs.append((letter, text))
        if any(letter for letter, _ in pairs):
            pairs.sort(key=lambda pair: pair[0])
        return [text for _, text in pairs]

    # 静态清单里的备选形态（语义未验证，留着以防别的分组用）
    raw_options = item.get(ep.FIELD_OPTIONS)
    if isinstance(raw_options, list):
        return [
            richtext.strip_html(_as_str(entry.get(ep.FIELD_CONTENT)))
            if isinstance(entry, dict)
            else richtext.strip_html(_as_str(entry))
            for entry in raw_options
        ]
    return []


def parse_questions(data: Any) -> list[Question]:
    """解析题目。实测：分组在 ``data.shortAnswers[]`` 与 ``data.singleChoices[]``。

    - 题干 ``content`` 是 HTML，这里抽成纯文本存进 ``Question.stem``，原始 HTML
      留在 ``Question.raw``；
    - 图片判定交给 :mod:`question_map`（题干里的 ``rich-img`` / ``rich-formula``
      都算「程序读不到」，转人工）；
    - **未知分组按空处理并告警**：新分组出现时要看得见，不能悄悄当成「没有题目」。
    """
    if not isinstance(data, dict):
        raise PincoProtocolError(
            f"题目响应不是对象（{type(data).__name__}），无法解析题目。"
        )

    questions: list[Question] = []
    groups_seen: list[str] = []
    for group_key, group_type in _GROUP_TYPES.items():
        group = data.get(group_key)
        if not isinstance(group, list):
            continue
        groups_seen.append(group_key)
        for item in group:
            if not isinstance(item, dict):
                continue
            question = _build_question(item, group_type)
            if question is not None:
                questions.append(question)

    if not groups_seen and data:
        log.warning(
            "题目响应里没有已知分组（已知：%s；实际键：%s）—— 可能出现了新分组，"
            "先按「没有题目」处理，但请核对字段名。",
            "/".join(_GROUP_TYPES),
            ", ".join(sorted(data)[:10]),
        )
    return questions


def _build_question(item: dict[str, Any], group_type: QuestionType) -> Question | None:
    """把分组里的一个元素翻成核心 ``Question``；不可标识的返回 None。"""
    remote_id = _as_str(_first(item, (ep.FIELD_QUESTION_ID, "id"))) or None
    html = _as_str(_first(item, (ep.FIELD_CONTENT,)))
    stem = richtext.strip_html(html)
    if not stem and not remote_id:
        log.warning("品课题目既无题干也无 ID，已跳过：%s", sorted(item)[:8])
        return None

    options = _parse_options(item)
    question = Question(
        stem=stem,
        type=_question_type(group_type, item),
        options=options,
        remote_id=remote_id,
        score=_as_float(_first(item, (ep.FIELD_STANDARD_SCORE, ep.FIELD_SCORE))),
        raw=item,
    )
    reasons = question_map.human_reasons(item, has_options=bool(options), stem_text=stem)
    if reasons:
        question_map.mark_image(question, reasons=reasons)
    return question


def _question_type(group_type: QuestionType, item: dict[str, Any]) -> QuestionType:
    """题型：分组本身已经说明（shortAnswers=主观题、singleChoices=单选），
    只有 ``raw`` 里另有更明确的题型字段时才覆盖它。"""
    mapped = question_map.map_question_type(question_map.question_type_name(item))
    return mapped if mapped is not QuestionType.UNKNOWN else group_type


# --------------------------------------------------------------------------
# 学习任务（课程材料）
# --------------------------------------------------------------------------


def parse_materials(data: Any, *, course: Course | None = None) -> list[Assignment]:
    """解析课程材料 —— 品课的「学习任务」就是它。实测：``data.courseMaterials[]``。

    ``learningRatio`` 就是学习进度（0~1），直接映射到 ``Assignment.progress``；
    学到 1.0 视为完成。

    **材料不是题**：这里产出的 ``Assignment.questions`` 永远是空的。这条链路只做
    「列任务 + 看进度 + 取详情下载地址」，不生成答案、不进可提交集合。
    """
    items = iter_items(data, ep.FIELD_COURSE_MATERIALS)
    materials: list[Assignment] = []
    for item in items:
        remote_id = _as_str(_first(item, (ep.FIELD_MATERIAL_ID, ep.FIELD_MEDIA_ID)))
        if not remote_id:
            log.warning("品课材料缺少 ID，已跳过：%s", sorted(item)[:8])
            continue
        progress = _as_progress(_first(item, (ep.FIELD_LEARNING_RATIO,)))
        materials.append(
            Assignment(
                remote_id=remote_id,
                title=(
                    _as_str(_first(item, (ep.FIELD_MATERIAL_NAME, ep.FIELD_TITLE)))
                    or remote_id
                ),
                kind=TaskKind.COURSE,
                course_name=course.name if course else "",
                progress=progress,
                finished=progress >= 1.0,
                raw=item,
            )
        )
    if items and not materials:
        raise PincoProtocolError(
            "拿到了材料列表，但没有一条含可用 ID —— 多半是字段名与样本不符"
            f"（已核对：{ep.FIELD_MATERIAL_ID}）。"
        )
    return materials


def parse_material_detail(data: Any, *, material_id: str) -> PincoMaterialDetail:
    """解析材料详情。实测：``data.media.downloadUrl`` + ``resourceTypeDownloadable``。

    ``downloadUrl`` **带签名/时效**（形如 ``/server-main/api/resource/<id>/url?``），
    只当次使用，别落库当永久链接。
    """
    if not isinstance(data, dict):
        raise PincoProtocolError(
            f"材料详情响应不是对象（{type(data).__name__}），无法解析。"
        )
    media = data.get(ep.FIELD_MEDIA)
    if not isinstance(media, dict):
        media = {}
    download_url = _as_str(
        _first(media, (ep.FIELD_DOWNLOAD_URL,)) or _first(data, (ep.FIELD_DOWNLOAD_URL,))
    )
    downloadable = data.get(ep.FIELD_RESOURCE_TYPE_DOWNLOADABLE)
    return PincoMaterialDetail(
        material_id=(
            _as_str(
                _first(data, (ep.FIELD_CHAPTER_DIRECTORY_MATERIAL_ID, ep.FIELD_MATERIAL_ID))
            )
            or material_id
        ),
        name=_as_str(_first(data, (ep.FIELD_MATERIAL_NAME, ep.FIELD_TITLE))),
        download_url=download_url,
        downloadable=None if downloadable is None else _as_bool(downloadable),
        progress=_as_progress(_first(data, (ep.FIELD_LEARNING_RATIO,))),
        raw=data,
    )


# --------------------------------------------------------------------------
# 提交相关
# --------------------------------------------------------------------------


def parse_submission_state(data: Any, *, assignment_id: str) -> PincoSubmissionState:
    """解析「这个作业交了没 / 还能不能交」。

    实测字段：``submitted`` / ``expired`` / ``canSubmitted``。找不到任何状态字段
    就抛错 —— 调用方必须把「不知道」与「确定没交」分开，不能兜底成
    ``submitted=False``（那正是重复交卷的成因）。
    """
    if not isinstance(data, dict):
        raise PincoProtocolError(
            f"提交状态响应不是对象（{type(data).__name__}），无法判断是否已提交。"
        )
    flag = _first(data, (ep.FIELD_SUBMITTED,))
    if flag is None:
        raise PincoProtocolError(
            "提交状态响应里没有可识别的状态字段，无法判断是否已提交"
            f"（已核对：{ep.FIELD_SUBMITTED}）。字段：{sorted(data)[:8]}"
        )
    can_submit = data.get(ep.FIELD_CAN_SUBMITTED)
    return PincoSubmissionState(
        assignment_id=assignment_id,
        submitted=_as_bool(flag),
        expired=_as_bool(data.get(ep.FIELD_EXPIRED)),
        can_submit=None if can_submit is None else _as_bool(can_submit),
        raw=data,
    )


def parse_submit_result(data: Any, *, assignment_id: str) -> SubmissionResult:
    """解析提交结果。

    走到这里说明信封是成功的（``statusCode == 0``）；若 ``data`` 里另给了更细的
    成功标记，以它为准。**不假定成功** —— 只有明确写了成功才算成功。

    TODO(endpoint): 提交响应体结构未验证（不会为了验证去真提交），
    第一次真实提交后按样本回填。
    """
    if not isinstance(data, dict):
        raise PincoProtocolError(f"提交响应不是对象（{type(data).__name__}），无法判定成败。")
    flag = _first(data, ("success", "ok", "isSuccess"))
    if flag is None:
        ok = True  # 信封已经成功，这里没有更细的标记
    else:
        ok = _as_bool(flag, true_literals=frozenset({"1", "true", "yes", "success", "ok"}))
    message = _as_str(_first(data, (ep.FIELD_MSG, ep.FIELD_MESSAGE, "message")))
    return SubmissionResult(
        assignment_id=assignment_id,
        ok=ok,
        score=_as_float(_first(data, (ep.FIELD_SCORE, "myScore", "totalScore"))),
        message=message or ("提交成功" if ok else "提交被平台拒绝"),
        raw=data,
    )
