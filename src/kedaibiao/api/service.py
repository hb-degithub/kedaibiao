"""Web 控制台的服务层 —— 与 HTTP 框架无关。

分成两层的理由：本机装不上 FastAPI（没有 pip），所以 HTTP 外壳用标准库写；
但**数据与动作的逻辑不该和 HTTP 绑死**。将来换 FastAPI / 加 CLI 子命令，
这里一行不用改。

本模块位于应用层，可以依赖 ``runtime`` 及以下所有层。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import re
import threading
import time
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime
from enum import Enum
from importlib.util import find_spec
from pathlib import Path
from typing import Any

from kedaibiao import __version__, adapters
from kedaibiao.api.actions import (
    PLATFORM_ERROR,
    ActionInputError,
    column,
    ensure_generic_actions,
    get_handler,
    has_action,
    load_builtin_actions,
    register_platform,
    table,
)
from kedaibiao.bank.store import ProgressStore, QuestionBank
from kedaibiao.config.overrides import OverrideStore
from kedaibiao.config.settings import (
    ENV_PREFIX,
    Settings,
    load_settings,
    resolve_environ,
)
from kedaibiao.core.models import Answer, AnswerSource, Credentials, TaskKind
from kedaibiao.platforms import load_builtin_adapters
from kedaibiao.platforms.base import NotSupportedError
from kedaibiao.runtime import (
    build_runtime,
    persist_http_cookies,
    restore_adapter_session,
    save_adapter_session,
    session_path,
)
from kedaibiao.security.accounts import AccountError, AccountStore, PlatformAccount
from kedaibiao.security.auth import (
    ROLE_USER,
    AuthError,
    AuthStore,
    Session,
    User,
)
from kedaibiao.security.credentials import env_key
from kedaibiao.tasks.llm import LLMConfig
from kedaibiao.tasks.pipeline import PipelineConfig

log = logging.getLogger(__name__)

JOB_HISTORY = 50
EVENT_HISTORY = 500
#: 每个平台保留多少行「详细日志」
PLATFORM_LOG_HISTORY = 400
OPTIONAL_DEPS = ("playwright", "fastapi", "uvicorn", "faster_whisper")

#: 品课的适配器名。这条链路是平台专属的（计划里带逐题哈希与人工标记），
#: 通用动作层装不下，所以单独放在服务层。
PINCO = "pinco"


class ConsoleError(Exception):
    """带机器可读 ``kind`` 与 HTTP 状态码的受控错误。"""

    def __init__(self, message: str, *, kind: str = "bad_request", status: int = 400) -> None:
        super().__init__(message)
        self.kind = kind
        self.status = status


@dataclass(frozen=True, slots=True)
class Viewer:
    """当前请求是谁在看。

    ``user_id=None`` 表示**未登录**，只允许内部/测试使用；HTTP 层在进入任何
    数据方法之前就会拦掉未登录请求（见 ``api/server.py`` 的鉴权门），
    所以生产路径上 viewer 一定是真实用户。
    """

    user_id: int | None = None
    is_superadmin: bool = False
    username: str = ""

    @property
    def authenticated(self) -> bool:
        return self.user_id is not None

    def owner_filter(self, requested: int | None = None) -> int | None:
        """把请求里的 ``?owner_id=`` 解析成实际用于过滤的归属。

        - 普通用户：只能是自己。指定别人 → 403（**不是**静默当成自己）；
        - 超级管理员：指定谁就是谁；不指定 = ``None`` = 不过滤（全量视图）；
        - 未登录：不过滤（内部/测试路径）。
        """
        if not self.authenticated:
            return requested
        if self.is_superadmin:
            return requested
        if requested is not None and int(requested) != self.user_id:
            raise ConsoleError(
                "无权查看其他用户的数据", kind="forbidden", status=403
            )
        return self.user_id

    def require_superadmin(self) -> None:
        if self.authenticated and not self.is_superadmin:
            raise ConsoleError("需要超级管理员权限", kind="forbidden", status=403)


def _jsonable(value: Any) -> Any:
    """把领域对象递归转成可 JSON 序列化的结构。

    事件载荷里混着 datetime / Enum / dataclass，``json.dumps`` 会直接炸。
    统一在这里收口，免得每个调用点各写一遍。
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(v) for v in value]
    if is_dataclass(value) and not isinstance(value, type):
        return _jsonable(asdict(value))
    return str(value)


def _friendly_platform_error(platform: str, exc: BaseException) -> str:
    """把平台异常翻成用户能照做的中文，并指出环境变量那条路已经不存在了。

    背景：适配器里的报错文案还停留在「设置 KEDAIBIAO_<平台>_USER/_PASS」，
    但凭据现在只从控制台账号库来 —— 照那句话做是不会生效的。
    """
    message = str(exc).strip() or type(exc).__name__
    if "KEDAIBIAO_" in message:
        message += (
            "\n注意：`.env` 里的平台账号已不再被读取。"
            "请在控制台「账号」页绑定该平台的账号。"
        )
    if "csrf" in message.lower() or "会话" in message:
        message += (
            "\n该平台的业务接口要求**浏览器会话 cookie**（csrf 与浏览器上下文绑定，"
            "密码登录只解决身份层）。需要用浏览器导出 cookie 的方式接入。"
        )
    # 「code=500 系统异常」是**平台业务模块**在报错，跟登录态无关。
    # 不说清楚的话用户会反复去重新登录 —— 实际重登一百次也没用。
    if "系统异常" in message or "code=500" in message:
        message += (
            "\n这是**平台侧该模块报错**（服务端 500），不是登录失效、也不是本地配置问题。"
            "\n可以先在东软网页版 / App 里点一下同样的功能确认：那边也报错就是平台在修，等一会儿再试；"
            "那边正常说明是接口变了，需要重新抓包对齐。"
        )
    return message


def _clean_essay(text: str) -> str:
    """去掉模型爱加的围栏/引号/标题行，只留正文。"""
    body = (text or "").strip()
    fence = re.search(r"```(?:[a-zA-Z]*)\s*(.+?)\s*```", body, re.S)
    if fence:
        body = fence.group(1).strip()
    lines = [ln for ln in body.splitlines() if not ln.strip().startswith("#")]
    body = "\n".join(lines).strip().strip('"').strip("“”").strip()
    return body


def _truthy(value: Any) -> bool:
    """把界面传来的确认串解释成布尔：``true/1/yes/确认`` 都算真。"""
    return str(value or "").strip().lower() in {
        "1", "true", "yes", "y", "on", "确认", "我确认",
    }


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# -- 品课计划：内容哈希与展示用的常量 --------------------------------------

#: 计划状态 → 给人看的中文。机器用的值（planned / needs_human / ...）另外留一份，
#: 前端要按状态分支，所以两列都得有。
_PINCO_STATE_LABELS = {
    "planned": "可提交",
    "needs_human": "需要人工",
    "skipped": "跳过",
    "failed": "抓取失败",
}


def _pinco_short(digest: str) -> str:
    """内容哈希的短码 —— 表格里只放前 8 位，完整值在信封顶层。"""
    return (digest or "")[:8] or "—"


def _pinco_content_digest(plan: Any) -> str:
    """整份计划的**内容**哈希（不含生成时间）。

    为什么不用 ``plan.digest``：``PincoPlan.compute_digest()`` 把 ``created_at``
    一起哈希了，而「复核 → 提交」中间必然隔了一段时间，重新 plan 出来的时间戳
    一定不同 —— 拿它比对会让每一次提交都报「计划已变化」。复核要锁定的东西是
    **内容**（作业、题目、答案），不是这份计划是哪一秒生成的。所以这里只哈希
    内容部分，算法与仓库其它哈希一致（sha256 + 排序键 + 紧凑分隔符，取前 32 位）。

    计划里的内容字段（作业/题目/答案）都是确定的，不含时间戳 —— 同一份内容
    无论生成几次，这个值都稳定。
    """
    blob = json.dumps(
        {
            "platform": plan.platform,
            "assignments": [item.to_dict() for item in plan.assignments],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


def _pinco_submission_payload(item: Any) -> tuple[Any, list[Any]]:
    """从复核过的计划项还原「要提交的作业 + 逐题答案」。

    **为什么不重新跑一遍答题链**：品课的提交守卫比对的是「本次要提交的内容」
    与「复核过的计划」的规范化哈希（``plan.submission_digest()`` 对
    ``PincoAssignmentPlan.content_digest()``）。重新作答会引入题库之外的
    模型/规则差异，哈希必然对不上，守卫会以 ``plan_hash_mismatch`` 拒绝 ——
    用户看到的就是「复核过的计划提交不了」。

    计划项本身就带着逐题的 ``choices`` / ``text`` / ``source`` / ``confidence``
    与平台原始载荷（``question_raw`` / ``answer_raw``），那正是用户刚刚复核过的
    答案。提交前那一步「重新 plan」已经保证了它是最新的，所以答案只能从这里
    还原，不能另起一套。
    """
    from kedaibiao.core.models import Answer, Assignment, Question

    assignment = Assignment(
        remote_id=item.assignment_id,
        title=item.title,
        kind=item.kind,
        course_name=item.course_name,
        progress=item.progress,
    )
    answers: list[Any] = []
    for planned in item.questions:
        question = Question(
            stem=planned.stem,
            type=planned.type,
            options=list(planned.options),
            remote_id=planned.remote_id,
            raw=dict(planned.question_raw),
        )
        answers.append(
            Answer(
                question=question,
                choices=list(planned.choices),
                text=planned.text,
                source=planned.source,
                confidence=planned.confidence,
                raw=dict(planned.answer_raw),
            )
        )
    return assignment, answers


# --------------------------------------------------------------------------
# 提交结果的展示信封
# --------------------------------------------------------------------------
#
# 为什么是「另挂」而不是直接返回信封：``/api/assignments/submit`` 的干跑结果
# 里 ``answers`` / ``can_submit`` / ``note`` 这些**机器字段**同时在被 CLI、
# 流水线与测试消费（契约见 docs/web-console-api.md）。信封键
# （``kind`` / ``fields`` / ``sections``）与机器字段互不冲突，叠加即可 ——
# 与 unipus 适配器里的 ``_plan_envelope`` 是同一套约定。


def _text_or_empty(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _short_text(value: Any, limit: int = 140) -> str:
    text = _text_or_empty(value)
    if not text:
        return "—"
    return text if len(text) <= limit else text[:limit] + "…"


def answer_letters(choices: Any, options: Any = None) -> str:
    """把选项下标翻译成人能读的答案：``[0]`` → ``A. 甲``。

    计划里选择题的答案机器字段必须保留下标（平台写回、题库指纹都靠它），
    但给人看的任何地方都不该出现裸 ``0``：控制台的「计划答案」列、CLI 摘要、
    老控制台都走这里。有选项原文就带上，没有就只给字母。
    """
    if not isinstance(choices, (list, tuple)) or not choices:
        return ""
    texts = list(options) if isinstance(options, (list, tuple)) else []
    parts: list[str] = []
    for choice in choices:
        if isinstance(choice, bool) or not isinstance(choice, int):
            parts.append(str(choice))
            continue
        letter = chr(ord("A") + choice) if 0 <= choice < 26 else str(choice)
        text = str(texts[choice]).strip() if 0 <= choice < len(texts) else ""
        parts.append(f"{letter}. {text}" if text else letter)
    return " / ".join(parts)


def _planned_answer(item: dict[str, Any]) -> str:
    """一行计划里「打算交的答案」：文本答案 → 选项字母（带原文）→ ``—``。"""
    for key in ("text", "answer", "answer_text", "planned_answer", "value"):
        text = _text_or_empty(item.get(key))
        if text:
            return text
    choices = item.get("choices")
    if isinstance(choices, (list, tuple)) and choices:
        return answer_letters(choices, item.get("options")) or "—"
    return "—"


def _confidence_text(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "—"
    return f"{float(value):.3f}"


#: 单题人工答案的文本上限。控制台是手输的，超过这个只可能是粘错东西。
MAX_OVERRIDE_TEXT = 8000


def _normalise_answer_overrides(raw: Any) -> list[dict[str, Any]] | None:
    """校验控制台传来的人工答案覆盖，形状不对直接 400。

    为什么在入口就把形状定死：等提交到一半才发现 payload 有问题，平台侧
    可能已经进考场、扣次数了。这里只做纯数据校验，不碰平台。
    """
    if raw is None or raw == []:
        return None
    if not isinstance(raw, list):
        raise ConsoleError("answers 必须是数组", kind="bad_request", status=400)
    cleaned: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ConsoleError("answers 的每一项必须是对象", kind="bad_request", status=400)
        try:
            index = int(item.get("index"))
        except (TypeError, ValueError):
            raise ConsoleError(
                "answers 里每项需要整数 index（题号从 1 开始）", kind="bad_request", status=400
            ) from None
        entry: dict[str, Any] = {"index": index}
        if "choices" in item:
            raw_choices = item.get("choices")
            if raw_choices is None:
                raw_choices = []
            if not isinstance(raw_choices, list):
                raise ConsoleError(
                    f"answers 第 {index} 项的 choices 必须是数组", kind="bad_request", status=400
                )
            try:
                choices = [int(choice) for choice in raw_choices]
            except (TypeError, ValueError):
                raise ConsoleError(
                    f"answers 第 {index} 项的 choices 必须是整数下标",
                    kind="bad_request",
                    status=400,
                ) from None
            if any(choice < 0 for choice in choices):
                raise ConsoleError(
                    f"answers 第 {index} 项的 choices 不能是负数", kind="bad_request", status=400
                )
            entry["choices"] = choices
        if "text" in item:
            text = item.get("text")
            if not isinstance(text, str):
                raise ConsoleError(
                    f"answers 第 {index} 项的 text 必须是字符串", kind="bad_request", status=400
                )
            if len(text) > MAX_OVERRIDE_TEXT:
                raise ConsoleError(
                    f"answers 第 {index} 项的 text 太长（上限 {MAX_OVERRIDE_TEXT} 字）",
                    kind="bad_request",
                    status=400,
                )
            entry["text"] = text
        if "choices" not in entry and "text" not in entry:
            raise ConsoleError(
                f"answers 第 {index} 项既没有 choices 也没有 text", kind="bad_request", status=400
            )
        cleaned.append(entry)
    return cleaned or None


async def _ask_for_review(answerer: Any, questions: list[Any]) -> list[Any]:
    """控制台的作答：保留低于阈值的候选答案，供人复核。

    ``ChainAnswerer`` 默认把低于 ``min_confidence`` 的答案丢掉（自动批跑要的
    是「不确定就别交」）。控制台正相反 —— 人要看模型到底猜了什么才能改，
    所以这里显式要 ``keep_weak=True``。其它答题器原样调用。
    """
    from kedaibiao.tasks.answerer import ChainAnswerer  # noqa: PLC0415 - 避免顶层耦合

    if isinstance(answerer, ChainAnswerer):
        return await answerer.answer(questions, keep_weak=True)
    return await answerer.answer(questions)


def _session_expired_error(exc: BaseException) -> ConsoleError | None:
    """把「平台说没登录」翻成一句用户能照做的话。

    为什么单独认这一类：有的平台 ``is_logged_in`` 的探测接口对过期 token
    仍然回正常，真正报 401 是在后面拉数据时 —— 那时已经不是「登录失败」那句
    文案覆盖的路径了，会漏成 500 内部错误。
    """
    name = type(exc).__name__.lower()
    text = str(exc)
    lowered = text.lower()
    if not (
        "notloggedin" in name
        or "未登录" in text
        or "no accesstoken" in lowered
        or "没有 accesstoken" in lowered
    ):
        return None
    return ConsoleError(
        f"平台登录态已失效（{text}）。请到「账号」页重新登录这个账号，再回来提交。",
        kind=PLATFORM_ERROR,
        status=502,
    )


def _apply_answer_overrides(
    answers: list[Any], overrides: list[dict[str, Any]]
) -> None:
    """把人工作答写回本次作答列表（原地改）。

    改过的题标记 ``manual``、置信度按是否仍为空记 1.0 / 0.0：人已经确认过，
    不应再被 ``min_confidence`` 阈值拦；清空的题仍然算「没答」，两条硬门槛
    继续生效。
    """
    total = len(answers)
    for entry in overrides:
        index = entry["index"]
        if not 1 <= index <= total:
            raise ConsoleError(
                f"answers 的 index {index} 超出范围（本次共 {total} 题）",
                kind="bad_request",
                status=400,
            )
        answer = answers[index - 1]
        if "choices" in entry:
            limit = len(answer.question.options)
            bad = [choice for choice in entry["choices"] if limit and choice >= limit]
            if bad:
                raise ConsoleError(
                    f"第 {index} 题的选项下标 {bad} 超出范围（共 {limit} 个选项）",
                    kind="bad_request",
                    status=400,
                )
            answer.choices = list(entry["choices"])
        if "text" in entry:
            answer.text = entry["text"].strip()
        answer.source = AnswerSource.MANUAL
        answer.confidence = 0.0 if answer.is_empty() else 1.0


#: 复核过的计划能复用多久。太短会把「看了十分钟才点提交」判成过期，
#: 太长又可能撞上平台改了题 —— 30 分钟够人慢慢核对 50 道题。
SUBMIT_PLAN_TTL_SECONDS = 1800
#: 比对计划与当前试卷时的题干前缀长度（防串题，不需要整段比对）。
_PLAN_STEM_KEY = 60


def _plan_stem_key(stem: Any) -> str:
    return _text_or_empty(stem)[:_PLAN_STEM_KEY]


def _answer_rows_from_plan(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把缓存的答案单压成「可重建 Answer」的最小结构。"""
    rows: list[dict[str, Any]] = []
    for entry in entries:
        rows.append(
            {
                "index": entry.get("index"),
                "choices": list(entry.get("choices") or []),
                "text": str(entry.get("text") or ""),
                "source": str(entry.get("source") or "unknown"),
                "confidence": float(entry.get("confidence") or 0.0),
                "stem_key": _plan_stem_key(entry.get("stem")),
            }
        )
    return rows


def _submit_plan_envelope(
    plan: dict[str, Any], assignment_id: str
) -> dict[str, Any]:
    """干跑计划的展示信封：结论字段 + 逐题「打算怎么答」。

    兼容两种计划形状：通用链路（``stem`` / ``text`` / ``choices``）与原生
    链路（iTEST：``stem`` / ``answer`` / ``reason``）—— 逐题列表都叫
    ``answers``，所以这里只按字段名兜底，不认平台。
    """
    items = [
        item
        for item in (plan.get("answers") or plan.get("questions") or [])
        if isinstance(item, dict)
    ]
    threshold = plan.get("threshold")
    rows: list[dict[str, Any]] = []
    humans = 0
    for index, item in enumerate(items, start=1):
        confidence = item.get("confidence")
        needs_human = item.get("needs_human") is True
        too_low = (
            isinstance(threshold, (int, float))
            and not isinstance(threshold, bool)
            and isinstance(confidence, (int, float))
            and not isinstance(confidence, bool)
            and confidence < threshold
        )
        if needs_human:
            humans += 1
        mark = _text_or_empty(item.get("reason"))
        if not mark:
            mark = "需人工确认" if needs_human else ("低于阈值" if too_low else "—")
        rows.append(
            {
                "index": item.get("index") or item.get("no") or index,
                "kind": _text_or_empty(item.get("kind")) or "—",
                "stem": _short_text(
                    item.get("stem") or item.get("question") or item.get("title")
                ),
                "answer": _planned_answer(item),
                "confidence": _confidence_text(confidence),
                "source": _text_or_empty(item.get("source")) or "—",
                "mark": mark,
            }
        )

    count = plan.get("question_count")
    answered = plan.get("answered")
    fields: list[dict[str, Any]] = [
        {"label": "任务号", "value": assignment_id},
        {"label": "题目数", "value": count if count is not None else len(items)},
        {"label": "已答", "value": answered if answered is not None else "—"},
        {"label": "需人工", "value": humans},
        {"label": "最低置信度", "value": _confidence_text(plan.get("min_confidence"))},
        {"label": "阈值 min_confidence", "value": _confidence_text(threshold)},
    ]
    if plan.get("can_submit") is not None:
        fields.append(
            {
                "label": "可交卷",
                "value": "是" if plan.get("can_submit") else "否（还有题没答案）",
            }
        )
    missing = plan.get("missing")
    if isinstance(missing, (list, tuple)) and missing:
        fields.append({"label": "缺答案", "value": f"{len(missing)} 题"})

    if rows:
        section: dict[str, Any] = {
            "label": f"逐题计划（{len(rows)} 题）",
            "kind": "table",
            "columns": [
                column("index", "#"),
                column("kind", "题型"),
                column("stem", "题干"),
                column("answer", "计划答案"),
                column("confidence", "置信度", mono=True),
                column("source", "来源"),
                column("mark", "标记"),
            ],
            "rows": rows,
        }
    else:
        section = {
            "label": "逐题计划",
            "kind": "text",
            "text": "这份计划没有逐题内容（后端只返回了结论字段）。",
        }
    return {
        "kind": "detail",
        "action": "submit",
        "label": "干跑",
        "fields": fields,
        "sections": [section],
    }


def _submit_result_envelope(
    result: dict[str, Any], assignment_id: str
) -> dict[str, Any]:
    """提交结果的展示信封。

    三个链路的结果形状不一样：原生链路（iTEST）给交卷步骤与页面回显、U校园给
    ``server_finish`` 与回读、通用链路给 ``ok``/``message``。信封统一收口，
    免得没有 ``message`` 的平台又掉回「这个动作没有返回结构化内容」。
    """
    submitted = bool(result.get("submitted"))
    accepted = result.get("ok")
    fields: list[dict[str, Any]] = [
        {"label": "任务号", "value": assignment_id},
        {"label": "已交卷", "value": "是" if submitted else "否"},
    ]
    if accepted is not None:
        fields.append({"label": "平台受理", "value": "是" if accepted else "否"})
    finish = result.get("server_finish")
    fields.append(
        {
            "label": "平台回读 finish",
            "value": "—" if finish is None else ("是" if finish else "否"),
        }
    )
    if result.get("score") is not None:
        fields.append({"label": "得分", "value": result.get("score")})
    screenshot = result.get("screenshot")
    if screenshot:
        fields.append({"label": "交卷截图", "value": str(screenshot)})

    sections: list[dict[str, Any]] = []
    message = _text_or_empty(result.get("message"))
    if message:
        sections.append({"label": "平台回执", "kind": "text", "text": message})
    page_text = _text_or_empty(result.get("page_text"))
    if page_text:
        sections.append({"label": "交卷页面回显", "kind": "text", "text": page_text})
    steps = result.get("steps") or []
    step_rows: list[dict[str, Any]] = []
    for step in steps:
        if isinstance(step, dict):
            step_rows.append(
                {
                    "name": _text_or_empty(step.get("name")) or "—",
                    "ok": "是" if step.get("ok") else "否",
                    "detail": _text_or_empty(step.get("detail")) or "—",
                }
            )
        else:
            step_rows.append({"name": _short_text(step), "ok": "—", "detail": "—"})
    if step_rows:
        sections.append(
            {
                "label": f"交卷步骤（{len(step_rows)} 步）",
                "kind": "table",
                "columns": [
                    column("name", "步骤"),
                    column("ok", "成功"),
                    column("detail", "细节"),
                ],
                "rows": step_rows,
            }
        )
    # 标签只用来说明「到底交没交」—— 这是用户最容易被误导的一处。
    label = "已交卷" if submitted and accepted is not False else "未交卷"
    return {
        "kind": "detail",
        "action": "submit",
        "label": label,
        "fields": fields,
        "sections": sections,
    }


class ConsoleService:
    """控制台的全部数据与动作。线程安全（Web 服务是多线程的）。"""

    def __init__(
        self, *, root: str | Path = ".", settings: Settings | None = None,
        environ: dict[str, str] | None = None,
    ) -> None:
        self.root = Path(root)
        self._settings = settings  # 懒加载；每次跑批都会重新读配置
        #: 显式注入的环境变量（测试用）。``None`` 表示每次从 .env + os.environ 现算。
        self._environ: dict[str, str] | None = dict(environ) if environ is not None else None
        self._lock = threading.RLock()
        self._jobs: list[dict[str, Any]] = []
        self._jobs_by_id: dict[str, dict[str, Any]] = {}
        self._events: list[dict[str, Any]] = []
        #: 按平台分开的执行日志（终端式面板用）。每个平台独立一份，互不串味。
        self._platform_logs: dict[str, list[dict[str, Any]]] = {}
        self._seq = 0
        self._active_job_id: str | None = None
        #: 用户点了「停止」的任务 id（后台线程每个循环点检查一次）
        self._cancel_requested: set[str] = set()
        #: 学习通任务的前台画面：job_id → {page, loop, frame, at}
        self._chaoxing_live: dict[str, dict[str, Any]] = {}
        #: 等人工过滑块/人脸时的事件：job_id → threading.Event
        self._human_events: dict[str, threading.Event] = {}
        #: 前台截图失败计数（只用来限日志，不影响任务）
        self._live_errors: dict[str, int] = {}
        #: 前台截图任务（持有引用）
        self._live_tasks: dict[str, Any] = {}
        #: 复核过的提交计划：``owner|platform|assignment`` → 那次干跑算出的答案单。
        #: 为什么要缓存：干跑和真提交是两次独立请求，中间隔着人看计划的时间。
        #: 每次都重跑答题链的话，模型可能给出**另一套**答案 —— 人确认过的东西
        #: 就作废了，而且低置信度题的集合还可能变，门槛会莫名其妙地又亮一次。
        self._submit_plans: dict[str, dict[str, Any]] = {}
        #: 滑块自动登录会话（unipus / itest）
        self._auto_logins: dict[str, dict[str, Any]] = {}
        #: 定时跑批器（``tasks/schedule.py``）。只有 web 进程里才会启动，
        #: 其它入口（CLI/测试）保持为 None，``schedule_status`` 也能照常读配置。
        self._schedule_runner: Any = None
        # 动作与适配器注册表都是进程级的，重复登记会覆盖，这里调用是幂等的。
        # 必须显式调用：程序化使用 ConsoleService 时不会经过 CLI 的 main()，
        # 否则会出现「平台数 0」这种看起来像坏了的结果。
        load_builtin_adapters()
        load_builtin_actions()

    # -- 配置与存储 -------------------------------------------------------

    @property
    def settings(self) -> Settings:
        if self._settings is None:
            self._settings = load_settings(root=self.root, environ=self.environ)
        return self._settings

    @property
    def environ(self) -> dict[str, str]:
        """本次运行认的环境变量（``.env`` + 真实环境变量）。

        凭据与平台扫描都要走这一份，不能各自去读 ``os.environ`` ——
        否则 ``.env`` 里配的凭据在控制台上会显示成「未配置」。

        **每次现算**（除非构造时显式注入过）：用户改了 ``.env`` 之后刷新页面
        就该生效，不该要求重启服务。解析一个几百字节的文件，开销可以忽略。
        """
        if self._environ is not None:
            return self._environ
        return resolve_environ(self.root)

    def _db_path(self) -> Path:
        return self.settings.db_path

    def _check_platform(self, platform: str | None) -> str | None:
        """校验 ``?platform=`` 参数，返回规范化后的作用域。

        ``None`` / 空串 = 全部平台。传了没注册的名字**必须报错**，不能静默
        当成空结果 —— 否则用户会以为「这个平台没数据」。
        """
        if platform is None or not str(platform).strip():
            return None
        name = str(platform).strip()
        if name not in adapters:
            known = ", ".join(adapters.names()) or "<未注册任何适配器>"
            raise ConsoleError(
                f"未注册的平台: {name!r}；已注册: {known}",
                kind="unknown_platform", status=404,
            )
        return name

    def _read(self, fn: Any) -> Any:
        """开一个只读用的连接跑 ``fn``，用完即关。

        sqlite 连接不能跨线程共享，而 Web 服务是多线程的 —— 每次调用开一个
        最省心，本地文件开销可以忽略。
        """

        class _Both:
            """同时持有题库与留痕，共用一个数据库文件。"""

            def __init__(self, path: Path) -> None:
                self.bank = QuestionBank(path)
                self.store = ProgressStore(path)

            def close(self) -> None:
                self.bank.close()
                self.store.close()

        handle = _Both(self._db_path())
        try:
            return fn(handle)
        finally:
            handle.close()

    # -- 大模型状态 -------------------------------------------------------

    #: 大模型字段 → 环境变量名（控制台覆盖值与 .env 走同一套键）
    LLM_FIELDS: dict[str, str] = {
        "base_url": "KEDAIBIAO_LLM_BASE_URL",
        "api_key": "KEDAIBIAO_LLM_API_KEY",
        "model": "KEDAIBIAO_LLM_MODEL",
        "timeout": "KEDAIBIAO_LLM_TIMEOUT",
        "temperature": "KEDAIBIAO_LLM_TEMPERATURE",
        "user_agent": "KEDAIBIAO_LLM_USER_AGENT",
        # 读图（OCR）单独一套：模型与网关/密钥都可以和主模型不同
        "ocr_model": "KEDAIBIAO_LLM_OCR_MODEL",
        "ocr_base_url": "KEDAIBIAO_LLM_OCR_BASE_URL",
        "ocr_api_key": "KEDAIBIAO_LLM_OCR_API_KEY",
        # 低置信度升级：低于阈值改问第二个模型
        "escalate_threshold": "KEDAIBIAO_LLM_ESCALATE_THRESHOLD",
        "fallback_model": "KEDAIBIAO_LLM_FALLBACK_MODEL",
        "fallback_base_url": "KEDAIBIAO_LLM_FALLBACK_BASE_URL",
        "fallback_api_key": "KEDAIBIAO_LLM_FALLBACK_API_KEY",
    }

    def _llm_overrides(self) -> dict[str, str]:
        """控制台里保存的大模型覆盖值（键形如 ``llm.model``）。"""
        try:
            return OverrideStore(self._db_path()).all("llm.")
        except Exception:  # noqa: BLE001 - 读不到就当没覆盖
            log.debug("读取大模型覆盖配置失败", exc_info=True)
            return {}

    def _llm_environ(self) -> dict[str, str]:
        """把控制台覆盖值叠到环境变量上，得到**本次实际生效**的那一份。"""
        merged = dict(self.environ)
        for name, env_name in self.LLM_FIELDS.items():
            value = self._llm_overrides().get(f"llm.{name}")
            if value is not None and value != "":
                merged[env_name] = value
        return merged

    def llm_status(self) -> dict[str, Any]:
        """大模型答题的配置状态。**绝不返回 api_key 的值**，只说有没有设。"""
        cfg = LLMConfig.from_env(self._llm_environ())
        overrides = self._llm_overrides()
        from_console = sorted(k.split(".", 1)[1] for k in overrides if k.startswith("llm."))
        return {
            "enabled": cfg.enabled,
            "configured": cfg.enabled,
            "provider": "openai-compatible",
            "model": cfg.model,
            "api_base": cfg.base_url,
            # 只报告「有没有设」，不返回内容
            "api_key_set": bool(cfg.api_key),
            "timeout": cfg.timeout,
            "temperature": cfg.temperature,
            # 读图（OCR）配置：模型名可公开，密钥只说有没有设
            "ocr_model": cfg.vision_model,
            "ocr_api_base": cfg.vision_base_url or cfg.base_url,
            "ocr_api_key_set": bool(cfg.vision_api_key or cfg.api_key),
            # 低置信度升级
            "escalate_threshold": cfg.escalate_threshold,
            "escalate_enabled": cfg.escalate_enabled,
            "fallback_model": cfg.fallback_model,
            "fallback_api_base": cfg.fallback_base_url or cfg.base_url,
            "fallback_api_key_set": bool(cfg.fallback_api_key or cfg.api_key),
            # 控制台可改：这些字段被界面覆盖了（其余取 .env）
            "editable": True,
            "override_fields": from_console,
            "source": "console" if from_console else ("env" if cfg.enabled else "none"),
            "configured_via": (
                "控制台「大模型」面板" if from_console
                else ("环境变量 KEDAIBIAO_LLM_*" if cfg.enabled else "")
            ),
        }

    def update_llm_config(self, viewer: Viewer | None, payload: dict[str, Any]) -> dict[str, Any]:
        """保存控制台里改的大模型配置。只有超管能改。

        只写 ``app_settings``，**不回写 .env**；字段留空 = 删掉覆盖项、回落到 .env。
        ``api_key`` 只进不出：界面永远拿不到明文，只看到「已设置 / 未设置」。
        """
        if not (viewer and viewer.is_superadmin):
            raise ConsoleError("只有超级管理员能改大模型配置", kind="forbidden", status=403)
        if not isinstance(payload, dict):
            raise ConsoleError("请求体必须是 JSON 对象", kind="bad_request", status=400)

        store = OverrideStore(self._db_path())
        updates: dict[str, str] = {}
        removals: list[str] = []

        base_url = str(payload.get("base_url") or "").strip()
        if "base_url" in payload:
            if base_url and not re.match(r"^https?://", base_url):
                raise ConsoleError(
                    "接口地址必须 http:// 或 https:// 开头",
                    kind="bad_request", status=400,
                )
            if base_url:
                updates["llm.base_url"] = base_url.rstrip("/")
            else:
                removals.append("llm.base_url")

        model = str(payload.get("model") or "").strip()
        if "model" in payload:
            if model:
                updates["llm.model"] = model
            else:
                removals.append("llm.model")

        if "api_key" in payload:
            key = str(payload.get("api_key") or "").strip()
            if key:
                updates["llm.api_key"] = key
        if payload.get("clear_api_key"):
            removals.append("llm.api_key")

        for name in ("ocr_model", "fallback_model"):
            if name in payload:
                value = str(payload.get(name) or "").strip()
                if value:
                    updates[f"llm.{name}"] = value
                else:
                    removals.append(f"llm.{name}")

        for name in ("ocr_base_url", "fallback_base_url"):
            if name not in payload:
                continue
            value = str(payload.get(name) or "").strip()
            if value and not re.match(r"^https?://", value):
                raise ConsoleError(
                    f"{name} 必须 http:// 或 https:// 开头", kind="bad_request", status=400
                )
            if value:
                updates[f"llm.{name}"] = value.rstrip("/")
            else:
                removals.append(f"llm.{name}")

        for name in ("ocr_api_key", "fallback_api_key"):
            if name not in payload:
                continue
            value = str(payload.get(name) or "").strip()
            if value:
                updates[f"llm.{name}"] = value
        for flag, name in (
            ("clear_ocr_api_key", "ocr_api_key"),
            ("clear_fallback_api_key", "fallback_api_key"),
        ):
            if payload.get(flag):
                removals.append(f"llm.{name}")

        # 阈值：0 = 关闭升级；上限 1（置信度本身就在 0~1）
        if "escalate_threshold" in payload:
            raw = payload.get("escalate_threshold")
            try:
                threshold = float(raw)
            except (TypeError, ValueError):
                raise ConsoleError(
                    "escalate_threshold 必须是数字", kind="bad_request", status=400
                ) from None
            if not (0.0 <= threshold <= 1.0):
                raise ConsoleError(
                    "escalate_threshold 要在 0~1 之间（0 = 关闭升级）",
                    kind="bad_request", status=400,
                )
            if threshold <= 0:
                removals.append("llm.escalate_threshold")
            else:
                updates["llm.escalate_threshold"] = f"{threshold:g}"

        for name, low, high in (("timeout", 1.0, 600.0), ("temperature", 0.0, 2.0)):
            if name not in payload:
                continue
            raw = payload.get(name)
            try:
                value = float(raw)
            except (TypeError, ValueError):
                raise ConsoleError(f"{name} 必须是数字", kind="bad_request", status=400) from None
            if not (low <= value <= high):
                raise ConsoleError(
                    f"{name} 要在 {low:g}~{high:g} 之间", kind="bad_request", status=400
                )
            updates[f"llm.{name}"] = f"{value:g}"

        if "user_agent" in payload:
            ua = str(payload.get("user_agent") or "").strip()
            if ua:
                updates["llm.user_agent"] = ua
            else:
                removals.append("llm.user_agent")

        try:
            store.put_many(updates)
            store.delete([k for k in removals if k not in updates])
        except Exception as exc:  # noqa: BLE001
            raise ConsoleError(f"保存失败：{exc}", kind="internal", status=500) from exc
        return self.llm_status()

    def test_llm_connection(self, viewer: Viewer | None) -> dict[str, Any]:
        """用当前生效的配置真发一次最小请求，验证 key / 地址 / 模型是否可用。"""
        from kedaibiao.tasks.llm import LLMAnswerer, LLMError

        cfg = LLMConfig.from_env(self._llm_environ())
        if not cfg.enabled:
            raise ConsoleError(
                "大模型还没配好：至少要填接口地址和模型名", kind="bad_request", status=400
            )
        llm = LLMAnswerer(cfg)
        started = time.monotonic()
        try:
            reply = asyncio.run(llm.complete(
                "你是连通性测试助手。", "只回复两个字母：OK", max_tokens=64
            ))
        except LLMError as exc:
            raise ConsoleError(f"调用失败：{exc}", kind="platform_error", status=502) from exc
        except Exception as exc:  # noqa: BLE001
            raise ConsoleError(f"调用失败：{type(exc).__name__}: {exc}",
                               kind="platform_error", status=502) from exc
        return {
            "ok": True,
            "model": cfg.model,
            "api_base": cfg.base_url,
            "reply": (reply or "").strip()[:200],
            "latency_ms": int((time.monotonic() - started) * 1000),
        }

    # -- 只读接口 ---------------------------------------------------------

    def overview(
        self, platform: str | None = None, *, owner_id: int | None = None,
        viewer: Viewer | None = None,
    ) -> dict[str, Any]:
        scope = self._check_platform(platform)
        owner = (viewer or Viewer()).owner_filter(owner_id)
        settings = self.settings
        db = self._db_path()
        exists = db.is_file()

        bank_stats: dict[str, Any] = {
            "total": 0, "graded": 0, "correct": 0, "accuracy": None,
            "ungraded": 0, "by_source": {}, "by_platform": {},
        }
        run_counts: dict[str, int] = {
            "total": 0, "succeeded": 0, "failed": 0, "skipped": 0, "running": 0,
        }

        if exists:
            def collect(handle: Any) -> tuple[dict[str, Any], dict[str, int]]:
                return (
                    handle.bank.stats(scope, owner_id=owner),
                    handle.store.status_counts(platform=scope, owner_id=owner),
                )

            bank_stats, run_counts = self._read(collect)
            for key in ("total", "succeeded", "failed", "skipped", "running"):
                run_counts.setdefault(key, 0)

        bound = self._with_accounts(
            lambda st: {a.platform for a in st.list_for(owner)}
        )
        credentialed = [name for name in adapters.names() if name in bound]

        return {
            "version": __version__,
            "python": ".".join(str(p) for p in __import__("sys").version_info[:3]),
            "repo_root": str(self.root.resolve()),
            "platform": scope,
            "env": settings.env,
            "dry_run": settings.dry_run,
            "headless": settings.headless,
            "max_concurrency": settings.max_concurrency,
            "min_confidence": settings.min_confidence,
            "db_path": str(db),
            "db_exists": exists,
            "platform_count": len(adapters.names()),
            "credentialed_platforms": credentialed,
            "optional_deps": {name: find_spec(name) is not None for name in OPTIONAL_DEPS},
            "llm": self.llm_status(),
            "bank": bank_stats,
            "runs": run_counts,
        }

    def platforms(self) -> dict[str, Any]:
        bound = self._with_accounts(lambda st: {a.platform for a in st.list_for()})
        items = []
        for name in adapters.names():
            cls = adapters.get(name)
            caps = getattr(cls, "capabilities", None)
            items.append({
                "name": name,
                "display_name": getattr(cls, "display_name", "") or name,
                "capabilities": _jsonable(caps) if caps is not None else {},
                # 有没有原生批量提交链路（batch-submit 只对有它的平台开放）。
                # 前端据此隐藏「一键全部提交」，避免点了必然 501。
                "native_submit_flow": bool(getattr(cls, "native_submit_flow", False)),
                "has_credentials": name in bound,
            })
        return {"platforms": items}

    def runs(
        self, limit: int = 50, platform: str | None = None, *,
        owner_id: int | None = None, viewer: Viewer | None = None,
    ) -> dict[str, Any]:
        scope = self._check_platform(platform)
        owner = (viewer or Viewer()).owner_filter(owner_id)
        if not self._db_path().is_file():
            return {"platform": scope, "runs": []}
        rows = self._read(
            lambda h: h.store.recent_runs(_clamp(limit, 1, 500), platform=scope, owner_id=owner)
        )
        return {"platform": scope, "runs": [_jsonable(r) for r in rows]}

    def submissions(
        self, limit: int = 50, platform: str | None = None, *,
        owner_id: int | None = None, viewer: Viewer | None = None,
    ) -> dict[str, Any]:
        scope = self._check_platform(platform)
        owner = (viewer or Viewer()).owner_filter(owner_id)
        if not self._db_path().is_file():
            return {"platform": scope, "submissions": []}
        rows = self._read(
            lambda h: h.store.recent_submissions(_clamp(limit, 1, 500), platform=scope, owner_id=owner)
        )
        return {"platform": scope, "submissions": [_jsonable(r) for r in rows]}

    def bank(
        self, platform: str | None = None, *, owner_id: int | None = None,
        viewer: Viewer | None = None,
    ) -> dict[str, Any]:
        scope = self._check_platform(platform)
        owner = (viewer or Viewer()).owner_filter(owner_id)
        empty: dict[str, Any] = {
            "total": 0, "graded": 0, "correct": 0, "accuracy": None,
            "ungraded": 0, "by_source": {}, "by_platform": {},
        }
        if not self._db_path().is_file():
            return {**empty, "platform": scope, "db_path": str(self._db_path())}
        stats = self._read(lambda h: h.bank.stats(scope, owner_id=owner))
        return {**stats, "platform": scope, "db_path": str(self._db_path())}

    def statistics(
        self, *, owner_id: int | None = None, viewer: Viewer | None = None
    ) -> dict[str, Any]:
        """跨平台对比。**刻意不接受 platform 参数** —— 它本身就是横向视图。

        归属仍然隔离：普通用户只统计自己的数据。
        """
        owner = (viewer or Viewer()).owner_filter(owner_id)
        registered = adapters.names()
        empty_bank: dict[str, Any] = {
            "total": 0, "graded": 0, "correct": 0, "accuracy": None,
            "ungraded": 0, "by_source": {}, "by_platform": {},
        }
        if not self._db_path().is_file():
            return {
                "bank": empty_bank,
                "platforms": [_blank_platform_row(name) for name in registered],
            }

        def collect(handle: Any) -> tuple[Any, Any, Any]:
            return (
                handle.bank.stats(owner_id=owner),
                handle.bank.platform_stats(owner_id=owner),
                handle.store.platform_stats(owner_id=owner),
            )

        bank, question_rows, run_rows = self._read(collect)

        questions = {row["platform"]: row for row in question_rows}
        runs = {row["platform"]: row for row in run_rows}

        # 先按注册顺序铺一遍，保证「没有数据的平台」也出现在对比表里
        order = list(registered)
        for name in list(runs) + list(questions):
            if name and name not in order:
                order.append(name)

        platforms = []
        for name in order:
            row = _blank_platform_row(name)
            row.update({k: v for k, v in runs.get(name, {}).items() if k != "platform"})
            q = questions.get(name)
            if q:
                row["questions"] = q["questions"]
                row["accuracy"] = q["accuracy"]
            platforms.append(row)

        return {"bank": bank, "platforms": platforms}

    # -- 定时跑批 ---------------------------------------------------------

    def start_schedule(self) -> dict[str, Any]:
        """按配置启动后台跑批线程（配置未启用时什么都不做）。

        返回值给启动脚本用：``{enabled, started, jobs, tick, error}``。
        配置写错不抛 —— 控制台必须能起来，错误放进 ``error`` 让人看见。
        """
        from kedaibiao.tasks.schedule import (
            ScheduleConfigError,
            ScheduleRunner,
            execute_job,
            load_plan,
        )

        with self._lock:
            if self._schedule_runner is not None:
                return {**self._schedule_runner.status(), "started": True}
        try:
            plan = load_plan(self.root)
        except ScheduleConfigError as exc:
            self._schedule_error = str(exc)
            return {"enabled": False, "started": False, "jobs": [], "error": str(exc)}

        if not plan.enabled or not plan.jobs:
            return {
                "enabled": plan.enabled,
                "started": False,
                "tick": plan.tick,
                "jobs": [],
                "error": "",
            }

        runner = ScheduleRunner(plan, lambda job: execute_job(self.root, job))
        runner.start()
        with self._lock:
            self._schedule_runner = runner
        return {**runner.status(), "started": True, "error": ""}

    def stop_schedule(self) -> None:
        with self._lock:
            runner = self._schedule_runner
            self._schedule_runner = None
        if runner is not None:
            runner.stop()

    def schedule_status(self) -> dict[str, Any]:
        """定时跑批的只读快照。没启动时也返回配置本身（``running=False``）。"""
        with self._lock:
            runner = self._schedule_runner
        if runner is not None:
            return runner.status()

        from kedaibiao.tasks.schedule import ScheduleConfigError, load_plan

        try:
            plan = load_plan(self.root)
        except ScheduleConfigError as exc:
            return {
                "enabled": False, "running": False, "tick": 0.0, "triggered": 0,
                "jobs": [], "error": str(exc),
            }
        return {
            "enabled": plan.enabled,
            "running": False,
            "tick": plan.tick,
            "triggered": 0,
            "jobs": [
                {
                    "name": job.name,
                    "platform": job.platform,
                    "every_seconds": job.every_seconds,
                    "live": job.live,
                    "limit": job.limit,
                    "kinds": sorted(k.value for k in job.kinds),
                    "enabled": job.enabled,
                    "last_run_at": None,
                    "last_error": "",
                }
                for job in plan.jobs
            ],
            "error": "",
        }

    def settings_view(self) -> dict[str, Any]:
        """设置页数据。**只列环境变量的键名，绝不返回值。**"""
        settings = self.settings
        db = self._db_path()
        platforms = []
        for name in adapters.names():
            cls = adapters.get(name)
            caps = getattr(cls, "capabilities", None)
            platforms.append({
                "name": name,
                "display_name": getattr(cls, "display_name", "") or name,
                "has_credentials": self._with_accounts(
                    lambda st, n=name: len(st.list_for(None, n)) > 0
                ),
                "credentials_hint": (
                    f"{env_key(name, 'user')} / {env_key(name, 'pass')}"
                    f"（或 {env_key(name, 'token')}）"
                ),
                "capabilities": _jsonable(caps) if caps is not None else {},
            })

        return {
            "env": settings.env,
            "dry_run": settings.dry_run,
            "headless": settings.headless,
            "max_concurrency": settings.max_concurrency,
            "min_interval": settings.min_interval,
            "max_attempts": settings.max_attempts,
            "min_confidence": settings.min_confidence,
            "notify": settings.notify,
            "db_path": str(db),
            "db_exists": db.is_file(),
            "session_dir": settings.session_dir,
            "dotenv_present": (self.root / ".env").is_file(),
            "env_keys_seen": sorted(k for k in self.environ if k.startswith(ENV_PREFIX)),
            "optional_deps": {name: find_spec(name) is not None for name in OPTIONAL_DEPS},
            "llm": self.llm_status(),
            "platforms": platforms,
        }

    def _plog(self, platform: str, level: str, text: str) -> None:
        """追加一行「某平台的执行日志」。

        和 job 的 logs 分开存：job 属于**一次**任务，这里属于**一个平台**的历史。
        控制台的「详细日志」面板看的是这一份（切平台就换一份，互不干扰）。
        """
        name = str(platform or "")
        if not name:
            return
        with self._lock:
            lines = self._platform_logs.setdefault(name, [])
            lines.append({"at": _now(), "level": str(level), "text": str(text)[:600]})
            del lines[:-PLATFORM_LOG_HISTORY]

    def _plog_long(self, platform: str, level: str, text: str, *, max_chars: int = 6000) -> None:
        """把可能很长的内容（提示词 / 作文正文 / 批注）分行写进平台日志。

        终端面板里要能直接读到「我让它怎么改」「它改成了什么」，
        所以这里按行切、每行限长，而不是整段截断成一行。
        """
        body = str(text or "")[:max_chars]
        for raw in body.splitlines() or [""]:
            line = raw.rstrip()
            while len(line) > 300:
                self._plog(platform, level, line[:300])
                line = "    " + line[300:]
            self._plog(platform, level, line)

    def platform_logs(self, platform: str, limit: int = 200) -> dict[str, Any]:
        scope = self._check_platform(platform)
        with self._lock:
            lines = list(self._platform_logs.get(scope or "", []))
        return {
            "platform": scope,
            "logs": lines[-_clamp(limit, 1, 400):],
            "total": len(lines),
        }

    def events(self, limit: int = 100) -> dict[str, Any]:
        with self._lock:
            return {"events": list(self._events[-_clamp(limit, 1, EVENT_HISTORY):])}

    def jobs(
        self, platform: str | None = None, *, viewer: Viewer | None = None
    ) -> dict[str, Any]:
        scope = self._check_platform(platform)
        viewer = viewer or Viewer()
        with self._lock:
            items = [_jsonable(j) for j in reversed(self._jobs)]
        if scope:
            items = [j for j in items if j.get("platform") == scope]
        if viewer.authenticated and not viewer.is_superadmin:
            items = [j for j in items if j.get("owner_id") == viewer.user_id]
        return {"platform": scope, "jobs": items}

    def job(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            found = self._jobs_by_id.get(job_id)
        if found is None:
            raise ConsoleError(f"没有这个任务: {job_id}", kind="not_found", status=404)
        return _jsonable(found)

    # -- 平台只读动作 -----------------------------------------------------

    def actions(self, platform: str) -> dict[str, Any]:
        scope = self._check_platform(platform)
        assert scope is not None
        ensure_generic_actions(scope)  # 课程/作业是所有平台都有的通用动作
        return {
            "platform": scope,
            "actions": [spec.to_dict() for spec in register_platform(scope)],
        }

    def run_action(
        self, platform: str, action: str, params: dict[str, Any] | None = None,
        *, viewer: Viewer | None = None,
    ) -> dict[str, Any]:
        scope = self._check_platform(platform)
        assert scope is not None
        ensure_generic_actions(scope)
        if not has_action(scope, action):
            available = ", ".join(s.name for s in register_platform(scope)) or "<无>"
            raise ConsoleError(
                f"{scope} 没有这个动作: {action!r}；可用: {available}",
                kind="unknown_action", status=404,
            )
        spec = next(s for s in register_platform(scope) if s.name == action)
        cleaned = self._validate_params(spec, params or {})
        detail = json.dumps(cleaned, ensure_ascii=False) if cleaned else ""
        self._plog(scope, "info", f"$ 动作 {action} {detail}".rstrip())
        try:
            result = asyncio.run(self._execute_action(scope, action, cleaned, viewer))
        except ConsoleError as exc:
            self._plog(scope, "error", f"✗ {action}：{exc}")
            raise
        except Exception as exc:  # noqa: BLE001
            self._plog(scope, "error", f"✗ {action}：{type(exc).__name__}: {exc}")
            raise
        envelope_kind = (result or {}).get("kind")
        title = (result or {}).get("title") or (result or {}).get("label") or ""
        self._plog(scope, "ok", f"→ {action} {envelope_kind or ''} {title}".rstrip())
        return result

    @staticmethod
    def _validate_params(spec: Any, params: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(params, dict):
            raise ConsoleError("参数必须是 JSON 对象", kind="bad_request", status=400)
        out: dict[str, Any] = {}
        for param in spec.params:
            raw = params.get(param.name)
            blank = raw is None or (isinstance(raw, str) and not raw.strip())
            if blank:
                if param.required:
                    raise ConsoleError(
                        f"缺少必填参数：{param.label}（{param.name}）",
                        kind="bad_request", status=400,
                    )
                continue
            if param.type == "int":
                try:
                    out[param.name] = int(raw)
                except (TypeError, ValueError):
                    raise ConsoleError(
                        f"参数 {param.label} 必须是整数",
                        kind="bad_request", status=400,
                    ) from None
            else:
                out[param.name] = str(raw).strip()
        return out

    async def _execute_action(
        self, platform: str, action: str, params: dict[str, Any],
        viewer: Viewer | None = None,
    ) -> dict[str, Any]:
        """跑一个动作。每次都新建 Runtime —— 它们各自持有 sqlite 连接。

        ``local`` 动作（只读本地缓存 / 调外部模型）不连平台，跳过登录；
        其余动作登录用「当前账号」的 cookie 文件。
        """
        spec = next((s for s in register_platform(platform) if s.name == action), None)
        local = bool(getattr(spec, "local", False))
        creds, cookie_file = (None, None) if local else self._resolve_login(platform, viewer)

        runtime = build_runtime(
            # 大模型这类「控制台可改」的配置要叠进来，动作层才能看到界面上的值
            root=self.root, environ=self._llm_environ(), cookie_file=cookie_file,
            owner_id=viewer.user_id if (viewer and viewer.authenticated) else 0,
        )
        adapter: Any = None
        try:
            adapter = runtime.make_adapter(platform)

            if not local:
                # 有落盘的会话就先恢复：账号库不存密码，没有会话就只能要求重新登录
                restore_adapter_session(adapter, cookie_file)
                try:
                    logged_in = await adapter.refresh_session(creds)
                except Exception as exc:  # noqa: BLE001 - 平台异常一律翻成 502
                    raise ConsoleError(
                        f"{platform} 登录失败：{_friendly_platform_error(platform, exc)}",
                        kind=PLATFORM_ERROR, status=502,
                    ) from exc
                if not logged_in:
                    # 平台自己知道为什么失效（例如东软：刷新令牌也过期）——
                    # 带上原因，用户才知道该去「账号」页重登，而不是去改密码。
                    note = str(getattr(adapter, "last_session_error", "") or "").strip()
                    raise ConsoleError(
                        f"{platform} 登录失败"
                        + (f"：{note}" if note else "（凭据可能不对，或会话已失效）")
                        + "；请到「账号」页给这个平台重新登录一次",
                        kind=PLATFORM_ERROR, status=502,
                    )

            try:
                return await get_handler(platform, action)(adapter, params)
            except ConsoleError:
                raise
            except ActionInputError as exc:
                # 参数不合法是 400，不是 502 —— 别把用户的输入错误说成平台故障
                raise ConsoleError(str(exc), kind="bad_request", status=400) from exc
            except NotSupportedError as exc:
                # 平台明确说「这个能力我没有」→ 501，不是 502。
                # 502 会让用户以为是平台故障/网络问题，实际是入口不该存在：
                # 例如 iTEST 的通用 detail 要进考场取卷（会开始计时），它自己就拒绝了。
                raise ConsoleError(
                    f"{platform} 不支持这个操作：{exc}"
                    f"（这是平台能力限制，不是故障；换用该平台自己的动作）",
                    kind="not_supported", status=501,
                ) from exc
            except Exception as exc:  # noqa: BLE001
                log.exception("平台动作失败: %s/%s", platform, action)
                raise ConsoleError(
                    f"{platform} 接口调用失败：{_friendly_platform_error(platform, exc)}",
                    kind=PLATFORM_ERROR, status=502,
                ) from exc
        finally:
            if adapter is not None and not local:
                # 落盘 cookie：下次点「课程/作业」时 refresh_session 能直接跳过登录，
                # 不必每次都重新登录（对平台礼貌，也快得多）
                try:
                    persist_http_cookies(adapter, runtime.settings)
                except Exception:  # noqa: BLE001 - 落盘失败不该影响已拿到的结果
                    log.debug("cookie 落盘失败", exc_info=True)
                await adapter.close()
            runtime.close()

    # -- 认证与用户 -------------------------------------------------------

    def _with_auth(self, fn: Any) -> Any:
        store = AuthStore(self._db_path())
        try:
            return fn(store)
        finally:
            store.close()

    def _with_accounts(self, fn: Any) -> Any:
        store = AccountStore(self._db_path())
        try:
            return fn(store)
        finally:
            store.close()

    def bootstrap_admin(self) -> tuple[dict[str, Any] | None, str | None]:
        """首次启动创建超级管理员，并把「无归属」的历史数据认领给它。

        认领这一步很关键：升级前所有数据 ``owner_id=0``，如果不认领，
        登录后就"什么都看不见"，用户会以为数据丢了。
        """
        user, generated = self._with_auth(
            lambda st: st.ensure_bootstrap_admin()
        )
        if user is None:
            return None, None
        # 历史数据认领给首个超管
        def claim(handle: Any) -> None:
            handle.bank.claim_ownerless(user.id)

        if self._db_path().is_file():
            self._read(claim)
        return user.public(), generated

    def login(self, username: str, password: str) -> tuple[dict[str, Any], Session]:
        user = self._with_auth(lambda st: st.verify_login(username, password))
        if user is None:
            raise ConsoleError("账号或密码不正确", kind="unauthorized", status=401)
        if user.disabled:
            raise ConsoleError("该账号已被停用", kind="unauthorized", status=401)
        session = self._with_auth(lambda st: st.create_session(user.id))
        return user.public(), session

    def logout(self, token: str) -> None:
        if token:
            self._with_auth(lambda st: st.delete_session(token))

    def user_for_token(self, token: str) -> dict[str, Any] | None:
        session = self._with_auth(lambda st: st.get_session(token))
        if session is None:
            return None
        user = self._with_auth(lambda st: st.get_user_by_id(session.user_id))
        if user is None or user.disabled:
            return None
        return user.public()

    # -- 用户管理（仅超管）------------------------------------------------

    def users(self, viewer: Viewer) -> dict[str, Any]:
        viewer.require_superadmin()

        users = self._with_auth(lambda st: st.list_users())
        counts = self._with_accounts(lambda st: st.counts_by_owner())
        out = []
        for user in users:
            row = user.public()
            row["account_count"] = counts.get(user.id, 0)
            out.append(row)
        return {"users": out, "account_counts": counts}

    def create_user(
        self, viewer: Viewer, username: str, password: str, *,
        role: str = ROLE_USER, note: str = "",
    ) -> dict[str, Any]:
        viewer.require_superadmin()
        try:
            user = self._with_auth(
                lambda st: st.create_user(username, password, role=role, note=note)
            )
        except AuthError as exc:
            raise ConsoleError(str(exc), kind=exc.kind, status=exc.status) from exc
        return user.public()

    def update_user(self, viewer: Viewer, user_id: int, **fields: Any) -> dict[str, Any]:
        viewer.require_superadmin()

        def apply(st: AuthStore) -> User:
            if st.get_user_by_id(user_id) is None:
                raise ConsoleError("用户不存在", kind="not_found", status=404)
            if fields.get("password"):
                st.set_password(user_id, fields["password"])
            if fields.get("username"):
                st.rename_user(user_id, fields["username"])
            if fields.get("role"):
                st.set_role(user_id, fields["role"])
            if fields.get("disabled") is not None:
                # 不允许把自己停用（会把管理入口锁死）
                if int(user_id) == viewer.user_id and fields["disabled"]:
                    raise ConsoleError(
                        "不能停用当前登录的账号", kind="bad_request", status=400
                    )
                st.set_disabled(user_id, bool(fields["disabled"]))
            user = st.get_user_by_id(user_id)
            assert user is not None
            return user

        try:
            user = self._with_auth(apply)
        except AuthError as exc:
            raise ConsoleError(str(exc), kind=exc.kind, status=exc.status) from exc
        return user.public()

    def delete_user(self, viewer: Viewer, user_id: int) -> None:
        viewer.require_superadmin()
        if int(user_id) == viewer.user_id:
            raise ConsoleError("不能删除当前登录的账号", kind="bad_request", status=400)

        def apply(st: AuthStore) -> None:
            if st.get_user_by_id(user_id) is None:
                raise ConsoleError("用户不存在", kind="not_found", status=404)
            st.delete_user(user_id)

        try:
            self._with_auth(apply)
        except AuthError as exc:
            raise ConsoleError(str(exc), kind=exc.kind, status=exc.status) from exc
        # 连带清掉他的平台账号（数据行本身留着，便于超管追溯）
        self._with_accounts(lambda st: st.delete_for_owner(user_id))

    # -- 平台账号（多号切换）----------------------------------------------

    def _require_account(self, viewer: Viewer, account_id: int) -> PlatformAccount:
        account = self._with_accounts(lambda st: st.get(account_id))
        if account is None:
            raise ConsoleError("账号不存在", kind="not_found", status=404)
        if (
            viewer.authenticated
            and not viewer.is_superadmin
            and account.owner_id != viewer.user_id
        ):
            raise ConsoleError("无权操作其他用户的账号", kind="forbidden", status=403)
        return account

    def accounts(
        self, viewer: Viewer, *, platform: str | None = None,
        owner_id: int | None = None,
    ) -> dict[str, Any]:
        """账号列表。普通用户只看自己；超管可用 ``owner_id`` 指定某人。"""
        if platform:
            self._check_platform(platform)
        if not viewer.authenticated or viewer.is_superadmin:
            target = owner_id
        else:
            if owner_id is not None and int(owner_id) != viewer.user_id:
                raise ConsoleError("无权查看其他用户的账号", kind="forbidden", status=403)
            target = viewer.user_id
        rows = self._with_accounts(lambda st: st.list_for(target, platform))
        return {
            "platform": platform,
            "owner_id": target,
            "accounts": [a.public() for a in rows],
        }

    # -- 交互式登录（验证码先自动过，不行再人工） -------------------------

    def _login_sessions(self) -> Any:
        """懒建登录会话管理器：只用一次的过程，不必绑在服务构造里。"""
        with self._lock:
            manager = getattr(self, "_interactive_logins", None)
            if manager is None:
                from kedaibiao.browser.login_session import LoginSessionManager

                manager = LoginSessionManager(
                    environ=self.environ, ttl=300.0, max_sessions=2
                )
                self._interactive_logins = manager
            return manager

    #: 登录验证码是**极验滑块**、且已内置自动求解的平台。
    #: 复用 ``unipus/geetest``（U校园与 iTEST 共用，已实测可用），不需要人工看图。
    AUTO_SLIDER_PLATFORMS = ("unipus", "itest")

    def auto_captcha_platforms(self) -> list[dict[str, Any]]:
        """告诉前端：哪些平台能「自动过验证码」，各是哪种机制。"""
        items = [
            {
                "platform": p,
                "kind": "slider",
                "label": "极验滑块（自动求解，复用 geetest v3 求解器）",
            }
            for p in self.AUTO_SLIDER_PLATFORMS
        ]
        from kedaibiao.browser.login_session import SPECS

        for name in SPECS:
            items.append({
                "platform": name,
                "kind": "image",
                "label": "图片验证码（先自动识别，失败转人工输入）+ 短信码",
            })
        return items

    def start_auto_login(
        self, viewer: Viewer, platform: str, *, username: str, password: str
    ) -> dict[str, Any]:
        """滑块类平台：直接走平台既有的「浏览器 + 极验求解」登录，后台跑、可轮询。"""
        self._check_platform(platform)
        if not viewer.authenticated:
            raise ConsoleError("未登录", kind="unauthorized", status=401)
        if platform not in self.AUTO_SLIDER_PLATFORMS:
            raise ConsoleError(
                f"{platform} 不是滑块自动求解平台；请用「验证码登录」（图片/短信）"
                "或普通密码登录",
                kind="not_supported", status=501,
            )
        if not username.strip() or not password:
            raise ConsoleError("账号和密码都要填", kind="bad_request", status=400)

        account = self._with_accounts(
            lambda st: st.find(viewer.user_id, platform, username.strip())
        )
        if account is None:
            account = self._with_accounts(
                lambda st: st.add(viewer.user_id, platform, username=username.strip())
            )

        with self._lock:
            self._seq += 1
            sid = f"al-{self._seq}"
            entry: dict[str, Any] = {
                "session_id": sid,
                "platform": platform,
                "state": "running",
                "message": "正在打开登录页并自动求解滑块…",
                "account_id": account.id,
                "started_at": _now(),
            }
            self._auto_logins[sid] = entry

        def run() -> None:
            try:
                ok, note = self._login_account(account, password)
            except Exception as exc:  # noqa: BLE001 - 线程边界：异常翻成失败状态
                ok, note = False, f"{type(exc).__name__}: {exc}"
            try:
                self._with_accounts(
                    lambda st: st.record_login(account.id, ok=ok, note=note)
                )
            except Exception:  # noqa: BLE001
                log.debug("记录登录结果失败", exc_info=True)
            with self._lock:
                entry["state"] = "done" if ok else "failed"
                entry["message"] = (
                    f"登录成功（{note}）" if ok else f"登录失败：{note}"
                )
                entry["finished_at"] = _now()

        threading.Thread(
            target=run, name=f"kedaibiao-auto-login-{sid}", daemon=True
        ).start()
        return dict(entry)

    def auto_login_state(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            entry = self._auto_logins.get(session_id)
        if entry is None:
            raise ConsoleError(
                f"没有这个自动登录会话：{session_id}", kind="not_found", status=404
            )
        return dict(entry)

    def start_interactive_login(
        self, viewer: Viewer, platform: str, *, username: str, password: str
    ) -> dict[str, Any]:
        """开一个登录会话：先自动过验证码，过不去就等人工输入。"""
        self._check_platform(platform)
        if not viewer.authenticated:
            raise ConsoleError("未登录", kind="unauthorized", status=401)
        from kedaibiao.browser.login_session import LoginSessionError

        try:
            return self._login_sessions().start(
                platform, username=username, password=password
            )
        except LoginSessionError as exc:
            kind = "not_supported" if exc.kind == "not_supported" else "bad_request"
            status = 501 if exc.kind == "not_supported" else 400
            if exc.kind == "busy":
                status = 409
            raise ConsoleError(str(exc), kind=kind, status=status) from exc

    def login_session_state(self, session_id: str) -> dict[str, Any]:
        from kedaibiao.browser.login_session import LoginSessionError

        try:
            return self._login_sessions().snapshot(session_id)
        except LoginSessionError as exc:
            raise ConsoleError(str(exc), kind="not_found", status=404) from exc

    def login_session_code(self, session_id: str, code: str) -> dict[str, Any]:
        from kedaibiao.browser.login_session import LoginSessionError

        try:
            return self._login_sessions().submit_code(session_id, code)
        except LoginSessionError as exc:
            raise ConsoleError(str(exc), kind="bad_request", status=400) from exc

    def login_session_sms(
        self, session_id: str, *, action: str = "send", code: str = ""
    ) -> dict[str, Any]:
        from kedaibiao.browser.login_session import LoginSessionError

        manager = self._login_sessions()
        try:
            if action == "code":
                return manager.submit_sms(session_id, code)
            return manager.send_sms(session_id)
        except LoginSessionError as exc:
            raise ConsoleError(str(exc), kind="bad_request", status=400) from exc

    def finish_interactive_login(
        self, viewer: Viewer, session_id: str, *, account_id: int | None = None
    ) -> dict[str, Any]:
        """登录成功后把浏览器登录态落到该账号的会话文件，并记一次成功。"""
        from kedaibiao.browser.login_session import LoginSessionError

        manager = self._login_sessions()
        try:
            state = manager.snapshot(session_id)
        except LoginSessionError as exc:
            raise ConsoleError(str(exc), kind="not_found", status=404) from exc
        if state.get("state") != "done":
            raise ConsoleError(
                f"登录还没完成（当前 {state.get('state')}）", kind="bad_request", status=400
            )
        account = (
            self._require_account(viewer, account_id)
            if account_id
            else self._with_accounts(
                lambda st: st.active_for(
                    viewer.user_id if viewer.authenticated else 0, state["platform"]
                )
            )
        )
        if account is None:
            raise ConsoleError(
                "这个平台还没有绑定账号：先在账号页添加一个（登录态才能落到它名下）",
                kind="no_credentials", status=400,
            )
        settings = self.settings
        target = account.cookie_path(settings.session_dir)
        ok = manager.save_state(session_id, str(target))
        self._with_accounts(
            lambda st: st.record_login(
                account.id, ok=bool(ok),
                note="浏览器交互登录（自动过验证码 / 人工输入）" if ok else "保存登录态失败",
            )
        )
        manager.close(session_id)
        return {"ok": bool(ok), "account": account.public(), "path": str(target)}

    def cancel_interactive_login(self, session_id: str) -> dict[str, Any]:
        try:
            self._login_sessions().close(session_id)
        except Exception:  # noqa: BLE001 - 取消失败不算错
            log.debug("关闭登录会话失败", exc_info=True)
        return {"ok": True}

    def add_account(
        self, viewer: Viewer, platform: str, *,
        label: str = "", username: str = "", password: str = "",
        login_now: bool = True, owner_id: int | None = None,
    ) -> dict[str, Any]:
        self._check_platform(platform)
        if not viewer.authenticated:
            raise ConsoleError("未登录", kind="unauthorized", status=401)
        if owner_id is not None and not viewer.is_superadmin and int(owner_id) != viewer.user_id:
            raise ConsoleError("无权操作其他用户的账号", kind="forbidden", status=403)
        target = owner_id if (viewer.is_superadmin and owner_id is not None) else viewer.user_id
        if not username.strip():
            raise ConsoleError("请填写平台用户名", kind="bad_request", status=400)

        account = self._with_accounts(
            lambda st: st.add(target, platform, label=label, username=username)
        )
        if login_now and password:
            ok, note = self._login_account(account, password)
            self._with_accounts(lambda st: st.record_login(account.id, ok=ok, note=note))
            if not ok:
                raise ConsoleError(
                    f"{platform} 登录失败：{note}", kind=PLATFORM_ERROR, status=502
                )
        fresh = self._with_accounts(lambda st: st.get(account.id))
        return (fresh or account).public()

    def activate_account(self, viewer: Viewer, account_id: int) -> dict[str, Any]:
        account = self._require_account(viewer, account_id)
        self._with_accounts(
            lambda st: st.set_active(account.owner_id, account.platform, account.id)
        )
        fresh = self._with_accounts(lambda st: st.get(account.id))
        return (fresh or account).public()

    def login_account(self, viewer: Viewer, account_id: int, password: str) -> dict[str, Any]:
        account = self._require_account(viewer, account_id)
        ok, note = self._login_account(account, password)
        self._with_accounts(lambda st: st.record_login(account.id, ok=ok, note=note))
        if not ok:
            raise ConsoleError(
                f"{account.platform} 登录失败：{note}", kind=PLATFORM_ERROR, status=502
            )
        fresh = self._with_accounts(lambda st: st.get(account.id))
        return (fresh or account).public()

    def update_account(
        self, viewer: Viewer, account_id: int, *,
        label: str | None = None, username: str | None = None,
    ) -> dict[str, Any]:
        account = self._require_account(viewer, account_id)
        try:
            updated = self._with_accounts(
                lambda st: st.update(account.id, label=label, username=username)
            )
        except AccountError as exc:
            raise ConsoleError(str(exc), kind=exc.kind, status=exc.status) from exc
        return updated.public()

    def delete_account(self, viewer: Viewer, account_id: int) -> None:
        account = self._require_account(viewer, account_id)
        self._with_accounts(lambda st: st.delete(account.id))
        try:
            settings = load_settings(root=self.root, environ=self.environ)
            cookie = account.cookie_path(settings.session_dir)
            cookie.unlink(missing_ok=True)
            # 会话快照也是凭据，一并删掉
            session_path(cookie).unlink(missing_ok=True)
        except OSError:
            log.debug("删除会话文件失败", exc_info=True)

    def _login_account(self, account: PlatformAccount, password: str) -> tuple[bool, str]:
        """用给定密码登录该账号，成功则把 cookie 落到**它自己的文件**里。

        密码只在这里用一次，不落盘。
        """
        settings = load_settings(root=self.root, environ=self.environ)
        cookie_file = account.cookie_path(settings.session_dir)
        runtime = build_runtime(
            root=self.root, environ=self.environ, cookie_file=str(cookie_file)
        )
        adapter: Any = None
        try:
            adapter = runtime.make_adapter(account.platform)
            creds = Credentials(username=account.username, password=password)
            ok = asyncio.run(adapter.login(creds))
            if ok:
                persist_http_cookies(adapter, settings)
                # 有些平台的会话不只在 cookie 里（bearer token 等），
                # 不把会话本身存下来，下次调用就只剩空密码 → 报「需要账号密码」
                save_adapter_session(adapter, cookie_file)
                return True, ""
            return False, "平台拒绝了这次登录（账号或密码不对？）"
        except Exception as exc:  # noqa: BLE001 - 平台异常统一成失败原因
            log.warning("账号 %s 登录异常: %s", account.id, exc)
            return False, f"{type(exc).__name__}: {exc}"
        finally:
            if adapter is not None:
                try:
                    asyncio.run(adapter.close())
                except Exception:  # noqa: BLE001
                    log.debug("关闭适配器失败", exc_info=True)
            runtime.close()

    def _resolve_login(
        self, platform: str, viewer: Viewer | None
    ) -> tuple[Credentials, str]:
        """这次请求用哪个号、哪个 cookie 文件。

        **只认账号库里绑定过的账号**，不再回退到 ``.env``。

        为什么去掉 .env 回退：两套来源并存时，用户永远搞不清「底部用的是哪个
        号」，而且 .env 里是明文密码。现在只有一条路 —— 在控制台「账号」页绑定，
        密码只在登录那一刻用一次，之后靠该账号自己的 cookie 文件。
        """
        settings = load_settings(root=self.root, environ=self.environ)
        owner = viewer.user_id if (viewer and viewer.authenticated) else None
        try:
            try:
                return self._with_accounts(
                    lambda st: st.resolve_credentials(
                        platform, owner_id=owner, session_dir=settings.session_dir
                    )
                )
            except AccountError:
                # 超级管理员没给自己绑号时，退回「全量视角」：命令行/早期绑定过的
                # 账号 owner_id=0，管理员在控制台里也应该能用（否则页面上看得到
                # 账号，点动作却报「还没有绑定账号」）。
                if not (viewer and viewer.is_superadmin):
                    raise
                return self._with_accounts(
                    lambda st: st.resolve_credentials(
                        platform, owner_id=None, session_dir=settings.session_dir
                    )
                )
        except AccountError as exc:
            raise ConsoleError(str(exc), kind=exc.kind, status=exc.status) from exc

    # -- 作文：评分循环 / 手动取分 / 历史 ---------------------------------

    def _essay_http(self, platform: str, viewer: Viewer | None) -> tuple[Any, Any, Any]:
        """返回 (adapter, http, cookie_file)，并已恢复会话。"""
        creds, cookie_file = self._resolve_login(platform, viewer)
        settings = load_settings(root=self.root, environ=self.environ)
        runtime = build_runtime(root=self.root, environ=self.environ,
                                cookie_file=cookie_file)
        adapter = runtime.make_adapter(platform)
        restore_adapter_session(adapter, cookie_file)
        return adapter, runtime, cookie_file

    def start_grade_loop(
        self,
        platform: str,
        rid: str,
        *,
        target_score: float,
        max_attempts: int = 3,
        initial_essay: str = "",
        viewer: Viewer | None = None,
        analysis_timeout: float = 180.0,
        resubmit_without_score: bool = False,
    ) -> dict[str, Any]:
        """启动「写→交→看批注→改写→再交」的后台任务。

        ``analysis_timeout`` 是**等批改出分**的上限（秒）。超时不算失败：稿子已经交了，
        任务会以 ``stop_reason=timeout`` 停下，前端提供「继续等待 N 秒 / 手动获取」。
        """
        scope = self._check_platform(platform)
        assert scope is not None
        if scope != "pigai":
            raise ConsoleError(
                "评分循环目前只实现了 pigai（其他平台的提交还没落地）",
                kind="not_supported", status=501,
            )
        if not rid.strip():
            raise ConsoleError("缺少作业号 rid", kind="bad_request", status=400)
        if not (0 < float(target_score) <= 100):
            raise ConsoleError("目标分数要在 0~100 之间", kind="bad_request", status=400)
        if int(max_attempts) < 1:
            raise ConsoleError("轮次至少 1 轮", kind="bad_request", status=400)
        analysis_timeout = float(analysis_timeout or 180.0)
        if not (20.0 <= analysis_timeout <= 1800.0):
            raise ConsoleError("等分上限要在 20~1800 秒之间", kind="bad_request", status=400)

        with self._lock:
            self._seq += 1
            job_id = f"g-{self._seq}"
            job: dict[str, Any] = {
                "job_id": job_id, "platform": scope, "rid": rid.strip(),
                "target_score": float(target_score), "max_attempts": int(max_attempts),
                "analysis_timeout": analysis_timeout,
                "resubmit_without_score": bool(resubmit_without_score),
                "status": "running", "started_at": _now(), "finished_at": None,
                "events": [], "attempts": [], "stop_reason": "", "message": "",
                "best_score": None, "initial_essay": initial_essay,
                "owner_id": viewer.user_id if (viewer and viewer.authenticated) else 0,
            }
            self._jobs.append(job)
            self._jobs_by_id[job_id] = job
            del self._jobs[:-JOB_HISTORY]
            for stale in list(self._jobs_by_id):
                if stale not in {j["job_id"] for j in self._jobs}:
                    del self._jobs_by_id[stale]

        threading.Thread(
            target=self._run_grade_loop, args=(job_id, scope, rid.strip(),
                                               float(target_score), int(max_attempts),
                                               initial_essay, viewer, analysis_timeout,
                                               bool(resubmit_without_score)),
            name=f"kedaibiao-grade-{job_id}", daemon=True,
        ).start()
        return _jsonable(job)

    # -- 学习通：作业页里就地跑「做考试 / 章节测验 / 刷课」的后台任务 ----------

    def chaoxing_meta(
        self, params: dict[str, Any], *, viewer: Viewer | None = None
    ) -> dict[str, Any]:
        """考试 / 测验的元信息（时间窗口、时长、剩余提交次数）—— 只读，不进答题页。"""
        action = str(params.get("action") or "").strip().lower()
        if action not in {"exam", "quiz"}:
            raise ConsoleError("action 只能是 exam / quiz", kind="bad_request", status=400)
        course_ref = str(params.get("course") or "").strip()
        if not course_ref:
            raise ConsoleError("缺少课程（course）", kind="bad_request", status=400)
        runtime: Any = None
        adapter: Any = None
        try:
            creds, cookie_file = self._resolve_login("chaoxing", viewer)
            runtime = build_runtime(
                root=self.root,
                environ=self._llm_environ(),
                cookie_file=cookie_file,
                owner_id=viewer.user_id if (viewer and viewer.authenticated) else 0,
            )
            adapter = runtime.make_adapter("chaoxing")
            restore_adapter_session(adapter, cookie_file)
            if not asyncio.run(adapter.refresh_session(creds)):
                raise RuntimeError("学习通登录失败（会话可能已失效，请重新登录）")
            course = self._chaoxing_pick_course(
                asyncio.run(adapter.list_courses()), course_ref
            )
            if course is None:
                raise RuntimeError(f"没有找到课程 {course_ref!r}")
            result = asyncio.run(
                adapter.automation.meta(
                    course,
                    action=action,
                    exam_id=str(params.get("exam_id") or ""),
                    chapter=str(params.get("chapter") or ""),
                    relation_id=str(params.get("relation_id") or ""),
                )
            )
            result["course"] = course.name
            return _jsonable(result)
        except ConsoleError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("读取学习通考试元信息失败")
            raise ConsoleError(
                f"读取考试信息失败：{type(exc).__name__}: {exc}",
                kind=PLATFORM_ERROR,
                status=502,
            ) from exc
        finally:
            if adapter is not None:
                with contextlib.suppress(Exception):
                    asyncio.run(adapter.close())
            if runtime is not None:
                with contextlib.suppress(Exception):
                    runtime.close()

    def chaoxing_meta_batch(
        self, params: dict[str, Any], *, viewer: Viewer | None = None
    ) -> dict[str, Any]:
        """批量取考试/测验元信息（作业列表「补齐信息」按钮）—— 只读。"""
        course_ref = str(params.get("course") or "").strip()
        items = params.get("items")
        if not course_ref:
            raise ConsoleError("缺少课程（course）", kind="bad_request", status=400)
        if not isinstance(items, list) or not items:
            raise ConsoleError("items 不能为空", kind="bad_request", status=400)
        if len(items) > 40:
            raise ConsoleError("一次最多补 40 项", kind="bad_request", status=400)
        runtime: Any = None
        adapter: Any = None
        try:
            creds, cookie_file = self._resolve_login("chaoxing", viewer)
            runtime = build_runtime(
                root=self.root,
                environ=self._llm_environ(),
                cookie_file=cookie_file,
                owner_id=viewer.user_id if (viewer and viewer.authenticated) else 0,
            )
            adapter = runtime.make_adapter("chaoxing")
            restore_adapter_session(adapter, cookie_file)
            if not asyncio.run(adapter.refresh_session(creds)):
                raise RuntimeError("学习通登录失败（会话可能已失效，请重新登录）")
            course = self._chaoxing_pick_course(
                asyncio.run(adapter.list_courses()), course_ref
            )
            if course is None:
                raise RuntimeError(f"没有找到课程 {course_ref!r}")
            metas = asyncio.run(adapter.automation.meta_batch(course, items))
            return {"course": course.name, "metas": _jsonable(metas)}
        except ConsoleError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("批量读取学习通考试信息失败")
            raise ConsoleError(
                f"批量读取考试信息失败：{type(exc).__name__}: {exc}",
                kind=PLATFORM_ERROR,
                status=502,
            ) from exc
        finally:
            if adapter is not None:
                with contextlib.suppress(Exception):
                    asyncio.run(adapter.close())
            if runtime is not None:
                with contextlib.suppress(Exception):
                    runtime.close()

    def start_chaoxing_job(
        self, action: str, params: dict[str, Any], *, viewer: Viewer | None = None
    ) -> dict[str, Any]:
        """启一个学习通后台任务，返回 job（logs/items 供控制台实时渲染）。

        为什么做成后台任务而不是同步动作：做一场考试要几十秒到几分钟，HTTP
        同步等待会被浏览器超时掐断，而且用户看不到过程。这里沿用刷课那套
        ``/api/jobs/{id}`` 轮询，作业页里就能看到逐题结果与日志。
        """
        scope = self._check_platform("chaoxing")
        assert scope is not None
        action = str(action or "").strip().lower()
        if action not in {"exam", "quiz", "study"}:
            raise ConsoleError("action 只能是 exam / quiz / study", kind="bad_request", status=400)
        course = str(params.get("course") or "").strip()
        if not course:
            raise ConsoleError("缺少课程（course）", kind="bad_request", status=400)
        if action == "exam" and not str(params.get("exam_id") or "").strip():
            if not str(params.get("assignment_id") or "").strip():
                raise ConsoleError("缺少考试号（exam_id）", kind="bad_request", status=400)
        if action == "quiz" and not str(params.get("chapter") or "").strip():
            raise ConsoleError("缺少小节 id（chapter）", kind="bad_request", status=400)

        with self._lock:
            self._seq += 1
            job_id = f"x-{self._seq}"
            job: dict[str, Any] = {
                "job_id": job_id,
                "kind": f"chaoxing-{action}",
                "platform": scope,
                "action": action,
                "params": dict(params),
                "status": "running",
                "started_at": _now(),
                "finished_at": None,
                "logs": [],
                "items": [],
                "score": None,
                "score_text": "",
                "submitted": False,
                "summary": "",
                "error": "",
                "owner_id": viewer.user_id if (viewer and viewer.authenticated) else 0,
            }
            self._jobs.append(job)
            self._jobs_by_id[job_id] = job
            del self._jobs[:-JOB_HISTORY]
            for stale in list(self._jobs_by_id):
                if stale not in {j["job_id"] for j in self._jobs}:
                    del self._jobs_by_id[stale]

        self._plog(scope, "info", f"$ 学习通任务 {job_id}（{action}，course={course}）")
        threading.Thread(
            target=self._run_chaoxing_job,
            args=(job_id, action, dict(params), viewer),
            name=f"kedaibiao-chaoxing-{job_id}",
            daemon=True,
        ).start()
        return _jsonable(job)

    @staticmethod
    def _chaoxing_pick_course(courses: list[Any], ref: str) -> Any:
        ref = str(ref or "").strip()
        for course in courses:
            if course.remote_id == ref:
                return course
        for course in courses:
            if course.remote_id.split(":")[0] == ref or ref in course.remote_id:
                return course
        for course in courses:
            if ref and ref in (course.name or ""):
                return course
        return None

    def _run_chaoxing_job(
        self, job_id: str, action: str, params: dict[str, Any], viewer: Viewer | None
    ) -> None:
        runtime: Any = None
        adapter: Any = None
        try:
            creds, cookie_file = self._resolve_login("chaoxing", viewer)
            runtime = build_runtime(
                root=self.root,
                environ=self._llm_environ(),
                cookie_file=cookie_file,
                owner_id=viewer.user_id if (viewer and viewer.authenticated) else 0,
            )
            adapter = runtime.make_adapter("chaoxing")
            restore_adapter_session(adapter, cookie_file)
            if not asyncio.run(adapter.refresh_session(creds)):
                raise RuntimeError(
                    "学习通登录失败（会话可能已失效，请到「账号」页重新登录一次）"
                )
            courses = asyncio.run(adapter.list_courses())
            course = self._chaoxing_pick_course(courses, params.get("course", ""))
            if course is None:
                raise RuntimeError(f"没有找到课程 {params.get('course')!r}")
            self._job_log(job_id, "info", f"课程：{course.name}（{course.remote_id}）")

            def emit(level: str, message: str) -> None:
                self._job_log(job_id, level if level in {"ok", "warn", "error"} else "info", message)

            def on_page(page: Any) -> None:
                """拿到页面后开一个后台截图任务，控制台就能「前台显示」。"""
                loop = asyncio.get_running_loop()
                self._chaoxing_live[job_id] = {
                    "page": page,
                    "loop": loop,
                    "frame": b"",
                    "at": 0.0,
                }
                self._job_update(job_id, has_live=True)
                self._job_log(job_id, "info", "前台显示已开启（可实时看画面）")

            async def human_gate(kind: str, message: str) -> bool:
                """暂停等人在控制台上过滑块/人脸。"""
                return await self._chaoxing_wait_human(job_id, kind, message)

            confirm = _truthy(params.get("confirm"))
            force = _truthy(params.get("force"))
            captcha_mode = str(params.get("captcha_mode") or "").strip().lower()
            if captcha_mode not in {"", "auto", "human"}:
                raise RuntimeError("captcha_mode 只能是 auto / human")
            if action == "study":
                kinds = tuple(
                    item.strip()
                    for item in str(params.get("kinds") or "video,document").split(",")
                    if item.strip()
                )
                result = asyncio.run(
                    adapter.automation.play_course(
                        course,
                        only_pending=True,
                        limit=int(params.get("limit") or 0),
                        kinds=kinds,
                        rate=float(params.get("rate") or 2.0),
                        doc_seconds=float(params.get("doc_seconds") or 0),
                        emit=emit,
                        cancel=lambda: self._cancelled(job_id),
                        on_page=on_page,
                    )
                )
                self._job_update(job_id, steps=result.get("steps") or [], summary=result.get("summary", ""))
                self._job_log(job_id, "ok", f"■ {result.get('summary', '')}")
            elif action == "exam":
                result = asyncio.run(
                    adapter.automation.run_exam(
                        course,
                        exam_id=str(params.get("exam_id") or ""),
                        title=str(params.get("title") or ""),
                        relation_id=str(params.get("relation_id") or ""),
                        confirm=confirm,
                        emit=emit,
                        on_items=lambda items: self._job_update(job_id, items=items),
                        force=force,
                        retake=_truthy(params.get("retake")),
                        captcha_mode=captcha_mode,
                        human_gate=human_gate,
                        on_page=on_page,
                        cancel=lambda: self._cancelled(job_id),
                    )
                )
                self._chaoxing_finish(job_id, result, confirm=confirm)
            else:
                result = asyncio.run(
                    adapter.automation.run_section_quiz(
                        course,
                        chapter_id=str(params.get("chapter") or ""),
                        confirm=confirm,
                        emit=emit,
                        on_items=lambda items: self._job_update(job_id, items=items),
                        force=force,
                        captcha_mode=captcha_mode,
                        human_gate=human_gate,
                        on_page=on_page,
                    )
                )
                self._chaoxing_finish(job_id, result, confirm=confirm)
            self._finish_batch_job(job_id, "done", summary=self._chaoxing_summary(job_id))
        except Exception as exc:  # noqa: BLE001 - 失败要落在任务里，而不是吞掉
            message = f"{type(exc).__name__}: {exc}"
            self._job_log(job_id, "error", f"✗ {message}")
            self._plog("chaoxing", "error", f"✗ 学习通任务 {job_id}：{message}")
            log.exception("学习通任务失败: %s", job_id)
            self._finish_batch_job(job_id, "failed", error=message)
        finally:
            self._chaoxing_live.pop(job_id, None)
            self._human_events.pop(job_id, None)
            self._live_tasks.pop(job_id, None)
            self._job_update(job_id, has_live=False, human="")
            if adapter is not None:
                with contextlib.suppress(Exception):
                    persist_http_cookies(adapter, runtime.settings if runtime else None)
                with contextlib.suppress(Exception):
                    asyncio.run(adapter.close())
            if runtime is not None:
                with contextlib.suppress(Exception):
                    runtime.close()

    def _chaoxing_finish(self, job_id: str, result: dict[str, Any], *, confirm: bool) -> None:
        """把作答结果写进 job：逐题信息、分数、是否交卷。"""
        items = result.get("items") or []
        self._job_update(
            job_id,
            items=items,
            score=result.get("score"),
            score_text=result.get("score_text") or "",
            submitted=bool(result.get("submitted")),
            summary=result.get("summary", ""),
            submit_note=result.get("submit_note") or "",
        )
        for item in items:
            mark = "ok" if item.get("answer") else "warn"
            self._job_log(
                job_id,
                mark,
                f"第 {item.get('number')} 题 {item.get('type')} → "
                f"{item.get('answer') or '（未答）'}"
                f"（置信度 {item.get('confidence', 0):.2f}，来源 {item.get('source')}）",
            )
        if result.get("submitted"):
            score_text = result.get("score_text") or "平台未回显分数"
            self._job_log(job_id, "ok", f"✓ 已交卷：{score_text}")
        elif confirm:
            self._job_log(job_id, "warn", f"交卷未完成：{result.get('submit_note') or '未知原因'}")
        else:
            self._job_log(job_id, "info", "干跑：已填卷并核验，未交卷")

    async def _chaoxing_live_loop(self, job_id: str, page: Any) -> None:
        """任务运行期间每 1.5 秒截一帧（JPEG），控制台拿来「前台显示」。"""
        self._job_log(job_id, "info", "前台截图任务已启动")
        while True:
            with self._lock:
                job = self._jobs_by_id.get(job_id)
                running = job is not None and job.get("status") == "running"
            if not running:
                return
            try:
                frame = await page.raw.screenshot(
                    type="jpeg", quality=60, timeout=5000
                )
            except Exception as exc:  # noqa: BLE001 - 截图失败不影响任务
                self._live_errors[job_id] = self._live_errors.get(job_id, 0) + 1
                if self._live_errors[job_id] in (1, 5):
                    self._job_log(
                        job_id, "warn", f"前台截图失败（第 {self._live_errors[job_id]} 次）：{exc}"
                    )
                frame = b""
            info = self._chaoxing_live.get(job_id)
            if info is not None:
                info["ticks"] = info.get("ticks", 0) + 1
            if info is not None and frame:
                if not info.get("frame"):
                    log.info("学习通前台首帧已就绪（job=%s，%d 字节）", job_id, len(frame))
                info["frame"] = frame
                info["at"] = time.time()
            await asyncio.sleep(1.5)

    def chaoxing_live(self, job_id: str) -> bytes | None:
        """按需截一帧当前画面（前端每隔一两秒取一次）。

        为什么按需截而不是后台循环：Playwright 的页面属于任务自己的事件循环，
        从监听线程里切一帧最稳；后台任务在 OCR 密集时反而容易饿死。
        """
        info = self._chaoxing_live.get(job_id)
        if not info or info.get("loop") is None:
            return None
        loop = info["loop"]
        page = info["page"]
        try:
            future = asyncio.run_coroutine_threadsafe(
                page.raw.screenshot(type="jpeg", quality=60, timeout=5000), loop
            )
            frame = future.result(timeout=8)
        except Exception as exc:  # noqa: BLE001 - 截图失败就当这次没有
            self._live_errors[job_id] = self._live_errors.get(job_id, 0) + 1
            if self._live_errors[job_id] in (1, 10):
                self._job_log(job_id, "warn", f"前台截图失败：{exc}")
            return None
        if frame:
            info["frame"] = frame
            info["at"] = time.time()
        return frame or None

    def chaoxing_captcha(self, job_id: str) -> dict[str, Any] | None:
        """把当前页面上的滑块验证码「拆出来」：背景图/拼图链接 + 缺口位置。

        验证码可能在主页面，也可能在 iframe 里（超星的 captcha 有时是独立 frame），
        所以逐个 frame 找；找到后用 frame 的位置把坐标换算回整页视图。
        """
        info = self._chaoxing_live.get(job_id)
        if not info or info.get("loop") is None:
            return None
        loop = info["loop"]
        page = info["page"]

        async def find_in_frame(frame: Any) -> dict[str, Any] | None:
            return await frame.evaluate(
                """(() => {
                    const bg = document.getElementById('cx_imgBg');
                    const small = document.querySelector('.cx_imgBtn img');
                    const handle = document.querySelector('.cx_rightBtn');
                    if (!bg || !small || !handle) return null;
                    const raw = bg.style.backgroundImage || '';
                    let url = '';
                    if (raw.indexOf('url(') === 0) {
                        url = raw.slice(4);
                        if (url.endsWith(')')) { url = url.slice(0, -1); }
                        url = url.replace(/^["']|["']$/g, '');
                    }
                    const box = (el) => {
                        const b = el.getBoundingClientRect();
                        return {x: b.x, y: b.y, w: b.width, h: b.height};
                    };
                    return {
                        bgUrl: url,
                        smallUrl: small.src || '',
                        bg: box(bg), handle: box(handle),
                    };
                })()"""
            )

        async def collect() -> dict[str, Any] | None:
            from kedaibiao.platforms.chaoxing import captcha as captcha_mod  # noqa: PLC0415

            for _ in range(4):
                data = None
                offset_x = 0.0
                offset_y = 0.0
                frames = [page.raw.main_frame] + [
                    raw for raw in page.raw.frames if raw is not page.raw.main_frame
                ]
                for raw_frame in frames:
                    try:
                        found = await find_in_frame(raw_frame)
                    except Exception:  # noqa: BLE001 - frame 正在跳转
                        continue
                    if not found or not found.get("bgUrl"):
                        continue
                    data = found
                    if raw_frame is not page.raw.main_frame:
                        try:
                            element = await raw_frame.frame_element()
                            box = await element.bounding_box()
                            if box:
                                offset_x = float(box.get("x") or 0.0)
                                offset_y = float(box.get("y") or 0.0)
                        except Exception:  # noqa: BLE001
                            offset_x = offset_y = 0.0
                    break
                if data:
                    break
                await asyncio.sleep(1.0)
            if not data:
                return None
            context = page.raw.context
            big = await (await context.request.get(data["bgUrl"], timeout=20000)).body()
            small = await (await context.request.get(data["smallUrl"], timeout=20000)).body()
            try:
                gap = captcha_mod.find_gap_offset(big, small)
            except Exception:  # noqa: BLE001 - 算不出来就只给图
                gap = 0
            bg_w = float(data["bg"].get("w") or 320) or 320
            viewport = getattr(page.raw, "viewport_size", None) or {
                "width": 1440, "height": 900,
            }
            vw = float(viewport.get("width") or 1440)
            vh = float(viewport.get("height") or 900)
            handle = data["handle"]
            return {
                "bg_url": data["bgUrl"],
                "piece_url": data["smallUrl"],
                "gap_px": gap,
                "bg_width": bg_w,
                "bg_image": "data:image/jpeg;base64," + base64.b64encode(big).decode(),
                "piece_image": "data:image/png;base64," + base64.b64encode(small).decode(),
                "handle_nx": (offset_x + handle["x"] + handle["w"] / 2) / vw,
                "handle_ny": (offset_y + handle["y"] + handle["h"] / 2) / vh,
                "gap_ratio": gap / bg_w,
            }

        try:
            future = asyncio.run_coroutine_threadsafe(collect(), loop)
            return future.result(timeout=25)
        except Exception as exc:  # noqa: BLE001
            log.debug("读取滑块验证码失败: %s", exc)
            return None

    def chaoxing_pointer(
        self, job_id: str, kind: str, nx: float, ny: float
    ) -> dict[str, Any]:
        """把控制台上的拖动/点击映射成页面里的鼠标事件（人工过滑块用）。

        ``nx``/``ny`` 是 0~1 的归一化坐标（前端按图片尺寸算），这里换算成页面
        CSS 像素。所有 Playwright 调用都回到任务自己的事件循环里执行。
        """
        info = self._chaoxing_live.get(job_id)
        if not info or info.get("loop") is None:
            return {"ok": False, "error": "这个任务没有可操作的页面"}
        loop = info["loop"]
        page = info["page"]
        viewport = getattr(page.raw, "viewport_size", None) or {
            "width": 1440,
            "height": 900,
        }
        x = max(0.0, min(1.0, float(nx))) * float(viewport.get("width", 1440))
        y = max(0.0, min(1.0, float(ny))) * float(viewport.get("height", 900))
        mouse = page.raw.mouse
        if kind == "move":
            coro = mouse.move(x, y)
        elif kind == "down":
            coro = mouse.down()
        elif kind == "up":
            coro = mouse.up()
        else:
            return {"ok": False, "error": f"未知的鼠标动作 {kind!r}"}
        try:
            future = asyncio.run_coroutine_threadsafe(coro, loop)
            future.result(timeout=5)
        except Exception as exc:  # noqa: BLE001 - 页面可能已关闭
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return {"ok": True}

    async def _chaoxing_wait_human(self, job_id: str, kind: str, message: str) -> bool:
        """暂停任务等人工（滑块/人脸），控制台点「继续」后返回 True。"""
        event = threading.Event()
        self._human_events[job_id] = event
        self._job_update(job_id, human=message)
        self._job_log(job_id, "warn", message + "（在画面上操作，完成后点「继续」）")
        handled = False
        try:
            for _ in range(120):
                with self._lock:
                    job = self._jobs_by_id.get(job_id)
                if job is None or job.get("status") != "running" or self._cancelled(job_id):
                    break
                if await asyncio.to_thread(event.wait, 5):
                    handled = True
                    break
        finally:
            self._human_events.pop(job_id, None)
            self._job_update(job_id, human="")
        if handled:
            self._job_log(job_id, "ok", "已收到「完成」，继续执行")
        return handled

    def mark_human(self, job_id: str) -> bool:
        """控制台按钮：告诉正在等待的任务「人已经处理完了」。"""
        event = self._human_events.get(job_id)
        if event is None:
            return False
        event.set()
        return True

    def _chaoxing_summary(self, job_id: str) -> str:
        with self._lock:
            job = self._jobs_by_id.get(job_id)
            if job is None:
                return ""
            items = job.get("items") or []
            answered = sum(1 for item in items if item.get("answer"))
            parts = [f"共 {len(items)} 题", f"作答 {answered}"]
            if job.get("submitted"):
                parts.append("已交卷")
                if job.get("score_text"):
                    parts.append(f"分数 {job['score_text']}")
            else:
                parts.append("未交卷")
            if job.get("steps"):
                parts.insert(0, f"任务点 {len(job['steps'])} 个")
            return "，".join(parts)

    def _job_update(self, job_id: str, **fields: Any) -> None:
        with self._lock:
            job = self._jobs_by_id.get(job_id)
            if job is not None:
                job.update(fields)

    # -- iTEST：一键自动化（取卷 → 转写答题 → 填 → 核验 → 复核 → 待人工/交卷）--

    def start_itest_auto(
        self, exam_id: str, *, viewer: Viewer | None = None,
        autosave_wait: float | None = None,
    ) -> dict[str, Any]:
        """启动「一键自动化」后台任务，返回 job（内含 logs 供控制台实时滚动）。"""
        scope = self._check_platform("itest")
        assert scope is not None
        exam_id = str(exam_id or "").strip()
        if not exam_id:
            raise ConsoleError("缺少考试号（exam_id）", kind="bad_request", status=400)

        with self._lock:
            self._seq += 1
            job_id = f"a-{self._seq}"
            job: dict[str, Any] = {
                "job_id": job_id, "kind": "itest-auto", "platform": "itest",
                "exam_id": exam_id, "status": "running",
                "started_at": _now(), "finished_at": None,
                "logs": [], "steps": [], "verify": None, "manual": [],
                "paper": None, "error": "", "owner_id":
                    viewer.user_id if (viewer and viewer.authenticated) else 0,
            }
            self._jobs.append(job)
            self._jobs_by_id[job_id] = job
            del self._jobs[:-JOB_HISTORY]
            for stale in list(self._jobs_by_id):
                if stale not in {j["job_id"] for j in self._jobs}:
                    del self._jobs_by_id[stale]

        threading.Thread(
            target=self._run_itest_auto,
            args=(job_id, exam_id, viewer, autosave_wait),
            name=f"kedaibiao-itest-auto-{job_id}", daemon=True,
        ).start()
        return _jsonable(job)

    def _job_log(self, job_id: str, level: str, text: str) -> None:
        """追加一行日志（控制台按行滚动显示，格式像命令输出）。"""
        with self._lock:
            job = self._jobs_by_id.get(job_id)
            if job is None:
                return
            job["logs"].append({"at": _now(), "level": level, "text": str(text)})
            del job["logs"][:-800]
            # 同一行也进「平台日志」—— 控制台切到该平台就能翻到这次任务的输出
            name = str(job.get("platform") or "")
            if name:
                lines = self._platform_logs.setdefault(name, [])
                lines.append({"at": _now(), "level": str(level), "text": str(text)[:600]})
                del lines[:-PLATFORM_LOG_HISTORY]

    def _job_step(self, job_id: str, name: str, ok: bool, detail: str = "") -> None:
        with self._lock:
            job = self._jobs_by_id.get(job_id)
            if job is None:
                return
            job["steps"].append({
                "name": name, "ok": bool(ok), "detail": str(detail),
                "at": _now(),
            })

    def _run_itest_auto(
        self, job_id: str, exam_id: str, viewer: Viewer | None,
        autosave_wait: float | None,
    ) -> None:
        import time as _time

        from kedaibiao.platforms.itest import cache as itest_cache
        from kedaibiao.platforms.itest.answer import build_sheet
        from kedaibiao.platforms.itest.exam import ExamRunner, cookies_from_jar
        from kedaibiao.platforms.itest.paper import KIND_RADIO
        from kedaibiao.tasks.llm import LLMAnswerer, LLMConfig

        runtime = None
        adapter = None
        try:
            # 实测：iTEST 的自动保存只认页面里「真实交互」产生的答案模型，
            # 我们用脚本设值 + 派发事件填进去的值**不会**被它缓存（重进页面读回是空）。
            # 所以默认不等自动保存；需要观察持久化的可以显式传 autosave_wait。
            wait = autosave_wait
            if wait is None:
                try:
                    wait = float(self.environ.get("KEDAIBIAO_ITEST_AUTOSAVE_WAIT", "0"))
                except (TypeError, ValueError):
                    wait = 0.0

            self._job_log(job_id, "info", f"$ kedaibiao itest auto --exam {exam_id}")
            creds, cookie_file = self._resolve_login("itest", viewer)
            runtime = build_runtime(
                root=self.root, environ=self._llm_environ(), cookie_file=cookie_file,
                owner_id=viewer.user_id if (viewer and viewer.authenticated) else 0,
            )
            adapter = runtime.make_adapter("itest")
            restore_adapter_session(adapter, cookie_file)
            self._job_log(job_id, "info", "[1/6] 登录 iTEST…")
            if not asyncio.run(adapter.refresh_session(creds)):
                raise ConsoleError("登录失败（会话已失效，请到「账号」页重新登录）",
                                   kind=PLATFORM_ERROR, status=502)
            self._job_log(job_id, "ok", "[1/6] 登录成功")
            self._job_step(job_id, "登录", True)

            client = adapter.client()
            jar = getattr(getattr(client, "http", None), "transport", None)
            cookies = cookies_from_jar(getattr(jar, "cookies", None))
            if not cookies:
                raise ConsoleError("没有可用的会话 cookie", kind=PLATFORM_ERROR, status=502)

            entry = asyncio.run(adapter.exam_meta(exam_id))
            ks_id = str(((getattr(entry, "raw", None) or {}).get("ksId")) or "")
            settings = getattr(runtime, "settings", None)
            cache_dir = itest_cache.cache_dir(settings)

            # ① 取卷（会开始/继续计时）
            self._job_log(job_id, "info", "[2/6] 进入考场取卷（★ 计时开始）…")
            # with 里才会启动浏览器并注入 cookie
            with ExamRunner(cookies) as runner:
                entered = runner.enter(exam_id, ks_id=ks_id)
                for step in entered.steps:
                    self._job_log(job_id, "ok" if step.ok else "warn",
                                  f"       · {step.name} {step.detail}".rstrip())
                if not entered.ok:
                    raise ConsoleError(f"没能进入作答页：{entered.message}",
                                       kind=PLATFORM_ERROR, status=502)
                paper = runner.read_paper()
            itest_cache.write_json(
                itest_cache.paper_path(settings, exam_id), paper.to_dict()
            )
            self._job_log(
                job_id, "ok",
                f"[2/6] 取卷完成：{len(paper.sections)} 大题 / {paper.question_count} 题 / "
                f"{paper.blank_count} 槽位",
            )
            self._job_step(job_id, "取卷", True, f"{paper.question_count} 题")

            # ② 转写 + AI 答题
            self._job_log(job_id, "info", "[3/6] 音频转写 + 大模型答题…")
            config = LLMConfig.from_env(self._llm_environ())
            if not config.enabled:
                raise ConsoleError("大模型未配置 —— 先在设置页填接口地址/模型/Key",
                                   kind="bad_request", status=400)
            self._job_log(job_id, "info", f"       模型：{config.model}")
            sheet, transcripts, stt_note = asyncio.run(build_sheet(
                paper, LLMAnswerer(config), cache_dir,
                environ=self._llm_environ(),
            ))
            if stt_note:
                self._job_log(job_id, "info", f"       {stt_note}")
            self._job_log(
                job_id, "ok",
                f"[3/6] 答题完成：{sheet.answered} 题有答案 / 跳过 {len(sheet.skipped)} 题",
            )
            for item in sheet.skipped:
                self._job_log(job_id, "warn",
                              f"       · 跳过 {item.get('qid')}（{item.get('kind')}）："
                              f"{item.get('reason', '')[:60]}")
            itest_cache.write_json(
                itest_cache.answers_path(settings, exam_id),
                {"answers": sheet.answers, "skipped": sheet.skipped,
                 "transcripts": {str(k): v for k, v in transcripts.items()}},
            )
            self._job_step(job_id, "AI 答题", True,
                           f"{sheet.answered} 题，跳过 {len(sheet.skipped)}")

            answers = dict(sheet.answers)

            # ③ 填答 + 同会话核验
            missing = [q.qid for s in paper.sections for q in s.questions
                       if not str(answers.get(q.qid) or "").strip()]
            filled: dict[str, Any] = {}
            checked: dict[str, Any] = {}
            with ExamRunner(cookies) as runner:
                self._job_log(job_id, "info", "[4/6] 进入作答页填答案…")
                entered = runner.enter(exam_id, ks_id=ks_id)
                if not entered.ok:
                    raise ConsoleError(f"没能进入作答页：{entered.message}",
                                       kind=PLATFORM_ERROR, status=502)
                if answers:
                    filled = runner.fill(answers)
                    checked = runner.verify(answers)
                self._job_log(
                    job_id, "ok",
                    f"[4/6] 填表：填空 {filled.get('blank', 0)} / 单选 {filled.get('radio', 0)} / "
                    f"翻译 {filled.get('textarea', 0)}；核验不符 {checked.get('total', 0)} 处",
                )
                self._job_step(
                    job_id, "填答案", True, json.dumps(filled, ensure_ascii=False)
                )
                self._job_step(job_id, "逐项核验", not checked.get("total"),
                               f"不符 {checked.get('total', 0)} 处")
                for bad in (checked.get("bad") or [])[:10]:
                    self._job_log(job_id, "warn", f"       · 核验不符：{bad}")
                if wait > 0:
                    self._job_log(
                        job_id, "info",
                        f"[5/6] 等平台自动保存（{wait:.0f}s，iTEST 每分钟存一次）…",
                    )
                    _time.sleep(wait)

            # ④ 重新抓一遍复核（新会话，验证真的落到服务端）
            self._job_log(job_id, "info", "[5/6] 复核：重新进考场抓一遍页面…")
            with ExamRunner(cookies) as runner:
                entered = runner.enter(exam_id, ks_id=ks_id)
                if not entered.ok:
                    raise ConsoleError(f"复核时进不去作答页：{entered.message}",
                                       kind=PLATFORM_ERROR, status=502)
                current = runner.read_answers()

            matched = sum(
                1 for qid, want in answers.items()
                if str(current.get(qid) or "").strip() == str(want).strip()
            )
            mismatched = [
                {"qid": qid, "want": want, "got": current.get(qid, "")}
                for qid, want in answers.items()
                if str(current.get(qid) or "").strip() != str(want).strip()
            ]
            persisted = matched > 0
            if mismatched and not persisted:
                # 全是空 → 平台没把脚本填的值缓存下来（实测如此），不是填错
                self._job_log(
                    job_id, "info",
                    f"[5/6] 重进考场读回：页面是空的（{matched}/{len(answers)}）—— "
                    "iTEST 的自动保存不认脚本填值；交卷时会重新填写并核验，不影响结果。",
                )
                self._job_step(job_id, "复核", True,
                               "服务端未落库（交卷时重填并核验）")
            else:
                self._job_log(
                    job_id, "ok" if not mismatched else "warn",
                    f"[5/6] 复核完成：{matched}/{len(answers)} 题与答案单一致"
                    + ("" if not mismatched else f"，{len(mismatched)} 题不一致"),
                )
                self._job_step(job_id, "复核", not mismatched,
                               f"{matched}/{len(answers)} 一致")

            # ⑤ 需要人工的题
            manual_rows: list[dict[str, Any]] = []
            for section in paper.sections:
                for question in section.questions:
                    if str(answers.get(question.qid) or "").strip():
                        continue
                    manual_rows.append({
                        "qid": question.qid,
                        "kind": question.kind,
                        "kind_label": {"blank": "填空", "radio": "单选",
                                       "write": "翻译"}.get(question.kind, question.kind),
                        "stem": (question.text or "")[:160],
                        "sentences": question.sentences[:6],
                        "slots": question.slot_count,
                        "options": (
                            [{"letter": c.letter, "text": c.text, "sub": c.sub}
                             for c in question.choices]
                            if question.kind == KIND_RADIO else []
                        ),
                        "answer": "",
                    })
            with self._lock:
                job = self._jobs_by_id.get(job_id)
                if job is not None:
                    job["manual"] = manual_rows
                    job["verify"] = {
                        "total": len(answers), "matched": matched,
                        "mismatched": mismatched, "missing": missing,
                        "filled": filled, "checked": checked,
                        "persisted": persisted,
                    }
                    job["paper"] = {
                        "title": next((s.part1 for s in paper.sections if s.part1), paper.title),
                        "question_count": paper.question_count,
                        "slot_count": paper.blank_count,
                    }
            if manual_rows:
                self._job_log(
                    job_id, "warn",
                    f"[6/6] 还有 {len(manual_rows)} 题没有答案 —— "
                    "请在下方手动选择后点「保存并复核」",
                )
            else:
                self._job_log(job_id, "ok", "[6/6] 全部题目都有答案，可以确认交卷")

            self._job_step(job_id, "完成", True)
            self._job_log(job_id, "ok", "自动化流程结束（未交卷）。")
            with self._lock:
                job = self._jobs_by_id.get(job_id)
                if job is not None:
                    job["status"] = "succeeded"
        except ConsoleError as exc:
            self._job_log(job_id, "error", f"✗ {exc}")
            with self._lock:
                job = self._jobs_by_id.get(job_id)
                if job is not None:
                    job["status"] = "failed"
                    job["error"] = str(exc)
        except Exception as exc:  # noqa: BLE001 - 后台任务必须自己收尾
            log.exception("iTEST 自动化失败")
            self._job_log(job_id, "error", f"✗ {type(exc).__name__}: {exc}")
            with self._lock:
                job = self._jobs_by_id.get(job_id)
                if job is not None:
                    job["status"] = "failed"
                    job["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            if adapter is not None:
                with contextlib.suppress(Exception):
                    asyncio.run(adapter.close())
            if runtime is not None:
                with contextlib.suppress(Exception):
                    runtime.close()
            with self._lock:
                job = self._jobs_by_id.get(job_id)
                if job is not None:
                    job["finished_at"] = _now()

    def save_itest_manual(
        self, exam_id: str, answers: dict[str, str], *, viewer: Viewer | None = None
    ) -> dict[str, Any]:
        """把手动补的答案并进答案单缓存，返回最新的缺项清单。"""
        from kedaibiao.platforms.itest import cache as itest_cache
        from kedaibiao.platforms.itest.paper import Paper

        settings = self.settings
        exam_id = str(exam_id or "").strip()
        if not exam_id:
            raise ConsoleError("缺少考试号（exam_id）", kind="bad_request", status=400)
        if not isinstance(answers, dict) or not answers:
            raise ConsoleError("没有要保存的答案", kind="bad_request", status=400)

        paper_raw = itest_cache.read_json(itest_cache.paper_path(settings, exam_id))
        if paper_raw is None:
            raise ConsoleError("还没有这场考试的试卷缓存，先跑一次「一键自动化」",
                               kind="bad_request", status=400)
        paper = Paper.from_dict(paper_raw)
        sheet = itest_cache.read_json(itest_cache.answers_path(settings, exam_id)) or {}
        merged = {str(k): str(v) for k, v in (sheet.get("answers") or {}).items()}
        for qid, value in answers.items():
            text = str(value or "").strip()
            if text:
                merged[str(qid)] = text
        skipped = [s for s in (sheet.get("skipped") or [])
                   if str(s.get("qid")) not in merged]
        itest_cache.write_json(
            itest_cache.answers_path(settings, exam_id),
            {"answers": merged, "skipped": skipped,
             "transcripts": sheet.get("transcripts") or {}},
        )
        missing = [q.qid for s in paper.sections for q in s.questions
                   if not str(merged.get(q.qid) or "").strip()]
        return {
            "exam_id": exam_id,
            "answered": len(merged),
            "missing": missing,
            "can_submit": not missing,
            "answers": merged,
        }

    def grade_job(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            found = self._jobs_by_id.get(job_id)
        if found is None:
            raise ConsoleError(f"没有这个任务: {job_id}", kind="not_found", status=404)
        return _jsonable(found)

    def _run_grade_loop(self, job_id: str, platform: str, rid: str,
                        target: float, attempts: int, initial_essay: str,
                        viewer: Viewer | None, analysis_timeout: float = 180.0,
                        resubmit_without_score: bool = False) -> None:
        from kedaibiao.platforms.pigai.grade_loop import GradeLoop
        from kedaibiao.tasks.llm import LLMAnswerer, LLMConfig

        runtime = None
        adapter = None
        try:
            adapter, runtime, cookie_file = self._essay_http(platform, viewer)
            settings = runtime.settings

            page = None
            try:
                page = asyncio.run(adapter.fetch_assignment_by_rid(rid))
            except Exception:  # noqa: BLE001 - 拿不到题目信息也能继续
                page = None
            topic = (page.title if page else "") or ""
            word_range = (page.word_range if page else "") or ""

            essay = initial_essay.strip()
            if not essay:
                essay = self._write_initial_essay(settings, topic, word_range, page)
            if not essay.strip():
                raise ConsoleError(
                    "没有初始正文，且大模型未配置无法起草。"
                    "请配置 KEDAIBIAO_LLM_* 或在请求里带上 initial_essay。",
                    kind="bad_request", status=400,
                )

            rewrite = None
            cfg = LLMConfig.from_env(runtime.environ)
            if cfg.enabled:
                llm = LLMAnswerer(cfg)
                from kedaibiao.platforms.pigai.grade_loop import (
                    REWRITE_SYSTEM,
                    build_rewrite_prompt,
                )

                async def rewrite(snapshot, current, next_attempt):  # noqa: ANN001
                    prompt = build_rewrite_prompt(
                        topic=topic, word_range=word_range, essay=current,
                        snapshot=snapshot, attempt=next_attempt,
                    )
                    # 提示词、改后正文都进日志 —— 用户要能在这里直接看到「让它怎么改」和「改成了什么」
                    self._plog(platform, "info",
                               f"— 第 {next_attempt} 轮改写提示词（{len(prompt)} 字）—")
                    self._plog_long(platform, "info", prompt, max_chars=6000)
                    text = await llm.complete(REWRITE_SYSTEM, prompt)
                    cleaned = _clean_essay(text)
                    self._plog(platform, "ok",
                               f"— 第 {next_attempt} 轮改写完成：{len(cleaned.split())} 词 —")
                    self._plog_long(platform, "ok", cleaned, max_chars=6000)
                    return cleaned

            def on_event(event: dict[str, Any]) -> None:
                with self._lock:
                    job = self._jobs_by_id.get(job_id)
                    if job is None:
                        return
                    job["events"].append({"at": _now(), **_jsonable(event)})
                    del job["events"][:-400]
                    kind = str(event.get("kind") or "")
                    if kind:
                        extra = ""
                        if event.get("score") is not None:
                            extra = f" {event.get('score')} 分"
                        elif event.get("message"):
                            extra = f" {event.get('message')}"
                        lines = self._platform_logs.setdefault("pigai", [])
                        lines.append({
                            "at": _now(),
                            "level": "error" if "fail" in kind else "info",
                            "text": f"· {kind}{extra}"[:600],
                        })
                        del lines[:-PLATFORM_LOG_HISTORY]
                    if event.get("kind") == "attempt.graded":
                        score = event.get("score")
                        if isinstance(score, (int, float)):
                            current = job.get("best_score")
                            job["best_score"] = (
                                score if not isinstance(current, (int, float))
                                else max(current, score)
                            )

            loop = GradeLoop(
                adapter=adapter,
                http=adapter.ctx.client(),
                base="https://www.pigai.org",
                target_score=target, max_attempts=attempts,
                on_event=on_event, rewrite=rewrite,
                word_range=word_range,
                analysis_timeout=analysis_timeout,
                resubmit_without_score=resubmit_without_score,
            )
            result = asyncio.run(loop.run(rid=rid, topic=topic, initial_essay=essay))

            # 落库每一轮
            if runtime.store is not None:
                owner = viewer.user_id if (viewer and viewer.authenticated) else 0
                for rec in result.attempts:
                    try:
                        runtime.store.record_attempt(
                            owner_id=owner, platform=platform, rid=rid,
                            eid=rec.eid, attempt=rec.attempt, snapshot=rec,
                        )
                    except Exception:  # noqa: BLE001
                        log.debug("记录尝试失败", exc_info=True)

            with self._lock:
                job = self._jobs_by_id[job_id]
                job["status"] = "succeeded" if result.stop_reason == "reached" else "failed"
                job["stop_reason"] = result.stop_reason
                job["message"] = result.message
                job["attempts"] = [a.to_dict() for a in result.attempts]
                job["best_score"] = result.best_score
        except Exception as exc:  # noqa: BLE001
            log.exception("评分循环异常")
            with self._lock:
                job = self._jobs_by_id.get(job_id)
                if job is not None:
                    job["status"] = "failed"
                    job["message"] = f"{type(exc).__name__}: {exc}"
        finally:
            if adapter is not None:
                try:
                    asyncio.run(adapter.close())
                except Exception:  # noqa: BLE001
                    pass
            if runtime is not None:
                runtime.close()
            with self._lock:
                job = self._jobs_by_id.get(job_id)
                if job is not None:
                    job["finished_at"] = _now()

    def _write_initial_essay(self, settings: Any, topic: str, word_range: str,
                             page: Any) -> str:
        """没给初始正文时，用大模型按题目要求起草。"""
        from kedaibiao.tasks.llm import LLMAnswerer, LLMConfig

        cfg = LLMConfig.from_env(self._llm_environ())
        if not cfg.enabled:
            return ""
        requirement = getattr(page, "requirement", "") or topic
        prompt = (
            f"题目/要求：{requirement}\n"
            f"字数要求：{word_range or '120~150'}\n"
            "请写一篇符合要求的英语作文，只输出正文。"
        )
        try:
            return _clean_essay(asyncio.run(LLMAnswerer(cfg).complete(
                "你是英语写作助手，只输出英文正文，不要标题与解释。", prompt)))
        except Exception:  # noqa: BLE001
            log.warning("起草失败", exc_info=True)
            return ""

    @staticmethod
    def _latest_eid(adapter: Any, rid: str) -> str:
        """按作业号找最新一篇的稿件号。

        控制台「只取分」按钮只知道 rid（作业号），不知道 eid（稿件号）；
        拿空 eid 去请求会得到「没找到作文」。所以缺 eid 时就地查一次列表。
        """
        import asyncio

        try:
            rows = asyncio.run(adapter.list_finished_essays())
        except Exception:  # noqa: BLE001 - 查不到就交给调用方报错
            return ""
        for row in rows:
            if str(getattr(row, "rid", "")) == str(rid):
                return str(getattr(row, "eid", "") or "")
        return ""

    def _log_snapshot(self, platform: str, rid: str, eid: str, snap: Any) -> None:
        """把「分数 + 四维 + 总评 + 逐条批注 + 正文」写进平台日志。

        控制台的执行日志面板要能直接看到平台给了什么评价、作文长什么样 ——
        这是判断「按什么改写」的依据，不该只留在界面卡片里。
        """
        data = snap.to_dict() if hasattr(snap, "to_dict") else dict(snap or {})
        score = data.get("score")
        if score is None:
            self._plog(platform, "warn",
                       f"· 取分 {rid}/{eid}：暂无分数（{data.get('raw_state') or '待分析'}）")
            return
        self._plog(platform, "ok",
                   f"· 取分 {rid}/{eid}：{score} / {data.get('full_marks') or '—'} 分"
                   f"（{data.get('raw_state') or '—'}）")
        dims = data.get("dimensions") or []
        if dims:
            self._plog(platform, "info", "  维度：" + "，".join(
                f"{d.get('name')} {round(float(d.get('ratio') or 0) * 100)}%"
                for d in dims
            ))
        overall = str(data.get("overall_comment") or "").strip()
        if overall:
            self._plog_long(platform, "info", "  总评：" + overall)
        feedback = data.get("feedback_lines") or []
        if feedback:
            self._plog(platform, "info", f"  批注 {len(feedback)} 条：")
            for item in feedback[:20]:
                self._plog_long(platform, "info", "    - " + str(item), max_chars=600)
        essay = str(data.get("essay_text") or "").strip()
        if essay:
            self._plog(platform, "info",
                       f"  作文正文（{len(essay.split())} 词）：")
            self._plog_long(platform, "info", essay, max_chars=4000)

    def fetch_essay_grade(self, platform: str, rid: str, eid: str, *,
                          viewer: Viewer | None = None,
                          trigger_analysis: bool = True) -> dict[str, Any]:
        """手动取一次分数与评价（不提交任何东西）。"""
        scope = self._check_platform(platform)
        assert scope is not None
        if scope != "pigai":
            raise ConsoleError("取分目前只实现了 pigai", kind="not_supported", status=501)
        adapter, runtime, _ = self._essay_http(scope, viewer)
        try:
            from kedaibiao.platforms.pigai import grading

            eid = str(eid or "").strip()
            if not eid:
                eid = self._latest_eid(adapter, rid)
                if eid:
                    self._plog(scope, "info", f"· 未给稿件号，自动取最新：{eid}")
            http = adapter.ctx.client()
            if trigger_analysis:
                snap = asyncio.run(grading.wait_for_grade(
                    adapter, http, base="https://www.pigai.org", rid=rid, eid=eid))
            else:
                snap = asyncio.run(grading.fetch_snapshot(adapter, rid=rid, eid=eid))
            self._log_snapshot(scope, rid, eid, snap)
            return snap.to_dict()
        finally:
            asyncio.run(adapter.close())
            runtime.close()

    def wait_essay_grade(
        self, rid: str, eid: str = "", *, platform: str = "pigai",
        timeout: float = 180.0, viewer: Viewer | None = None,
    ) -> dict[str, Any]:
        """**再等一段时间的批改结果**（不重新提交）。

        评分循环以 ``stop_reason=timeout`` 停下后，用户可以带着稿件号再等 N 秒，
        或者直接取一次分。超时**不抛 502** —— 返回 ``timed_out=True`` 让前端继续问用户。
        """
        scope = self._check_platform(platform)
        assert scope is not None
        if scope != "pigai":
            raise ConsoleError("等分目前只实现了 pigai", kind="not_supported", status=501)
        rid = str(rid or "").strip()
        if not rid:
            raise ConsoleError("缺少作业号 rid", kind="bad_request", status=400)
        timeout = float(timeout or 180.0)
        if not (5.0 <= timeout <= 1800.0):
            raise ConsoleError("等待时长要在 5~1800 秒之间", kind="bad_request", status=400)

        from kedaibiao.platforms.pigai import grading

        adapter, runtime, _ = self._essay_http(scope, viewer)
        try:
            eid = str(eid or "").strip()
            if not eid:
                eid = self._latest_eid(adapter, rid)
            http = adapter.ctx.client()
            self._plog(scope, "info", f"$ 等批改 {rid}/{eid or '—'} 最多 {timeout:.0f}s")
            try:
                snap = asyncio.run(grading.wait_for_grade(
                    adapter, http, base="https://www.pigai.org",
                    rid=rid, eid=eid, timeout=timeout,
                ))
            except grading.AnalysisTimeout as exc:
                self._plog(scope, "warn", f"· 仍未出分（{exc}），可以继续等或手动获取")
                return {
                    "rid": rid, "eid": eid, "graded": False, "timed_out": True,
                    "message": f"{exc}。稿子已经交了，只是还没批完；可以再等一段或稍后手动获取。",
                }
            out = snap.to_dict()
            out.update({"timed_out": False})
            self._log_snapshot(scope, rid, eid, snap)
            self._plog(scope, "ok",
                       f"→ 出分 {out.get('score') if out.get('graded') else '未出分'}"
                       f"（{out.get('raw_state') or '—'}）")
            return out
        finally:
            with contextlib.suppress(Exception):
                asyncio.run(adapter.close())
            runtime.close()

    def essay_history(self, rid: str, *, platform: str = "pigai",
                      viewer: Viewer | None = None) -> dict[str, Any]:
        """某作业的历史提交记录（每一轮的分数与批注）。"""
        owner = (viewer or Viewer()).owner_filter(None)
        if not self._db_path().is_file():
            return {"rid": rid, "attempts": []}
        rows = self._read(lambda h: h.store.attempts_for(
            rid, owner_id=owner, platform=platform))
        return {"rid": rid, "platform": platform, "attempts": rows}

    # -- 单个作业：详情与提交 ---------------------------------------------

    def assignment_detail(
        self, platform: str, assignment_id: str, *, viewer: Viewer | None = None
    ) -> dict[str, Any]:
        """作业详情（控制台双击作业行时调用）。走通用动作，返回展示信封。"""
        if not str(assignment_id or "").strip():
            raise ConsoleError("缺少任务号", kind="bad_request", status=400)
        return self.run_action(
            platform, "detail", {"assignment_id": str(assignment_id).strip()}, viewer=viewer
        )

    def submit_assignment(
        self,
        platform: str,
        assignment_id: str,
        *,
        viewer: Viewer | None = None,
        confirm: bool = False,
        overrides: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """针对**单个作业**作答并提交。

        ``confirm=False``（默认）= 只规划不提交，返回「打算交什么」。
        ``confirm=True`` 才会真的调平台的写接口。

        ``overrides`` 是人工在控制台上改过的答案（只对 ``confirm=True`` 生效）：
        ``[{"index": 1, "choices": [2]}, {"index": 3, "text": "……"}]``，
        ``index`` 是干跑计划里的题号（1 起）。改过的题按 ``manual`` 处理、
        置信度记 1.0 —— 人已经亲眼看过了，不再由阈值拦；没改的题照旧受
        两条硬门槛约束。

        平台没实现提交时**明确拒绝**（501 not_supported），而不是假装成功 ——
        现在 pigai 仍是 ``capabilities.submit = False``；unipus 已实现但不能真交
        （默认干跑，真发要 ``submit_live``）。
        """
        scope = self._check_platform(platform)
        assert scope is not None
        overrides = _normalise_answer_overrides(overrides)
        cls: Any = adapters.get(scope)
        caps = getattr(cls, "capabilities", None)
        display = getattr(cls, "display_name", None) or scope
        if caps is not None and not getattr(caps, "submit", False):
            raise ConsoleError(
                f"{display} 尚未实现提交（capabilities.submit = False）。"
                f"当前平台只能读数据；提交需要先按真实抓包实现该平台的写接口。",
                kind="not_supported", status=501,
            )

        assignment_id = str(assignment_id or "").strip()
        if not assignment_id:
            raise ConsoleError("缺少任务号", kind="bad_request", status=400)

        edited = f"，人工改过 {len(overrides)} 题" if overrides else ""
        self._plog(
            scope,
            "info",
            f"$ 提交 {assignment_id}（{'真交，不可逆' if confirm else '干跑，不提交'}{edited}）",
        )
        try:
            result = asyncio.run(
                self._submit_one(
                    scope, assignment_id, viewer=viewer, confirm=confirm, overrides=overrides
                )
            )
        except ConsoleError as exc:
            self._plog(scope, "error", f"✗ 提交 {assignment_id}：{exc}")
            raise
        except Exception as exc:  # noqa: BLE001
            self._plog(scope, "error",
                       f"✗ 提交 {assignment_id}：{type(exc).__name__}: {exc}")
            expired = _session_expired_error(exc)
            if expired is not None:
                raise expired from exc
            raise
        if isinstance(result, dict):
            if result.get("dry_run", True) is False or result.get("submitted") is not None:
                self._plog(scope, "ok",
                           f"→ 提交完成 submitted={result.get('submitted')} "
                           f"finish={result.get('server_finish')}")
            else:
                self._plog(scope, "ok",
                           f"→ 干跑 {result.get('question_count', '?')} 题，"
                           f"有答案 {result.get('answered', '?')} 题")
        return result

    # -- 复核过的提交计划 -------------------------------------------------

    @staticmethod
    def _plan_key(owner_id: int, platform: str, assignment_id: str) -> str:
        return f"{owner_id}|{platform}|{assignment_id}"

    def _remember_submit_plan(self, key: str, plan: dict[str, Any]) -> None:
        """把干跑算出的答案单存下来，供随后的「确认真提交」原样复用。"""
        rows = _answer_rows_from_plan(list(plan.get("answers") or []))
        with self._lock:
            self._submit_plans[key] = {"at": time.time(), "rows": rows}

    def _cached_answers(
        self, key: str, questions: list[Any]
    ) -> list[Answer] | None:
        """取回人看过的那份答案单；题目对不上就作废。

        为什么要校验题干前缀：计划是按**题序**匹配的，如果这中间平台换了题
        （或者用户切了作业），按下标硬套就是「把答案填到别的题上」。宁可重算。
        """
        with self._lock:
            entry = self._submit_plans.get(key)
            if entry is None:
                return None
            if time.time() - float(entry.get("at") or 0.0) > SUBMIT_PLAN_TTL_SECONDS:
                self._submit_plans.pop(key, None)
                return None
            rows = list(entry.get("rows") or [])
        if len(rows) != len(questions):
            return None
        answers: list[Answer] = []
        for index, (row, question) in enumerate(zip(rows, questions, strict=True), start=1):
            if row.get("index") != index:
                return None
            stem_key = str(row.get("stem_key") or "")
            if stem_key and _plan_stem_key(question.stem) != stem_key:
                return None
            try:
                source = AnswerSource(str(row.get("source") or "unknown"))
            except ValueError:
                source = AnswerSource.UNKNOWN
            answers.append(
                Answer(
                    question=question,
                    choices=list(row.get("choices") or []),
                    text=str(row.get("text") or ""),
                    source=source,
                    confidence=float(row.get("confidence") or 0.0),
                )
            )
        return answers

    async def _missing_assignment_hint(
        self,
        adapter: Any,
        platform: str,
        assignment_id: str,
        skipped_courses: list[str],
    ) -> str:
        """作业在列表里定位不到时，尽量说清「为什么」。

        典型场景（品课实测 2026-10-26）：作业所属课程已结束/归档，列表接口对
        那门课回 6569，作业本身却是**过期**状态 —— 用户看到「找不到作业」会
        以为是程序问题。这里再探一次详情，把过期/已提交讲清楚。
        """
        probe = getattr(adapter, "fetch_submission_state", None)
        if probe is not None:
            try:
                from kedaibiao.core.models import Assignment as _A

                state = await probe(_A(remote_id=assignment_id, title=""))
                if getattr(state, "expired", False):
                    return (
                        f"作业 {assignment_id} 已过期（平台 expired=true），交不上去。"
                        "过期的作业仍在列表里显示，但平台不再接受作答。"
                    )
                if getattr(state, "submitted", False):
                    return f"作业 {assignment_id} 平台显示已提交（submitted=true），不需要再交。"
            except Exception:  # noqa: BLE001 - 探测失败就退回通用文案
                log.debug("探测作业状态失败", exc_info=True)
        if skipped_courses:
            return (
                f"在 {platform} 里找不到作业 {assignment_id!r}；"
                f"有 {len(skipped_courses)} 门课程读取失败（例：{skipped_courses[0]}），"
                "作业可能属于已结束/归档的课程。"
            )
        return ""

    async def _submit_one(
        self,
        platform: str,
        assignment_id: str,
        *,
        viewer: Viewer | None,
        confirm: bool,
        overrides: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        creds, cookie_file = self._resolve_login(platform, viewer)
        settings = load_settings(root=self.root, environ=self.environ)
        owner_id = viewer.user_id if (viewer and viewer.authenticated) else 0
        runtime = build_runtime(
            # 大模型这类「控制台可改」的配置要叠进来，动作层才能看到界面上的值
            root=self.root, environ=self._llm_environ(), cookie_file=cookie_file,
            owner_id=owner_id,
        )
        adapter: Any = None
        try:
            adapter = runtime.make_adapter(platform)
            restore_adapter_session(adapter, cookie_file)
            if not await adapter.refresh_session(creds):
                detail = _text_or_empty(getattr(adapter, "last_session_error", ""))
                raise ConsoleError(
                    f"{platform} 登录失败（会话可能已失效，请到「账号」页重新登录）"
                    + (f"：{detail}" if detail else ""),
                    kind=PLATFORM_ERROR, status=502,
                )

            target = None
            skipped_courses: list[str] = []
            for course in await adapter.list_courses():
                try:
                    items = await adapter.list_assignments(course)
                except Exception as exc:  # noqa: BLE001 - 单门课失败不该让整次定位失败
                    allowed = getattr(adapter, "skippable_course_errors", ())
                    if allowed and isinstance(exc, allowed):
                        # 品课 6569「课程不存在」就属于这种：课程已归档/换班，
                        # 其它课程里的作业仍要能定位到。
                        skipped_courses.append(f"{course.name or course.remote_id}（{exc}）")
                        self._plog(platform, "warn",
                                   f"! 课程读取失败已跳过：{course.name or course.remote_id}")
                        continue
                    raise
                for item in items:
                    if item.remote_id == assignment_id:
                        target = item
                        break
                if target is not None:
                    break
            if target is None:
                hint = await self._missing_assignment_hint(
                    adapter, platform, assignment_id, skipped_courses
                )
                raise ConsoleError(
                    hint or f"在 {platform} 里找不到作业 {assignment_id!r}",
                    kind="not_found", status=404,
                )

            # 平台侧已过期就别再花时间出计划：过期作业在列表里仍会显示，
            # 但平台不接受作答（品课实测：expired=true 与 submitted 独立）。
            state_probe = getattr(adapter, "fetch_submission_state", None)
            if state_probe is not None and target is not None:
                try:
                    state = await state_probe(target)
                except Exception:  # noqa: BLE001 - 探测失败不拦（提交时还有守卫）
                    state = None
                if getattr(state, "expired", False):
                    deadline_text = _text_or_empty(str(getattr(target, "deadline", "") or ""))
                    raise ConsoleError(
                        f"作业 {assignment_id!r} 已过期，平台不再接受作答"
                        + (f"（截止 {deadline_text}）" if deadline_text else "")
                        + "。它仍会出现在列表里，但已经交不上去。",
                        kind="bad_request", status=400,
                    )

            # 原生作答链路的平台（iTEST）：取题/交卷都在浏览器里做，
            # 不走通用的 fetch_questions → answerer → submit 三段式。
            if getattr(adapter, "native_submit_flow", False):
                if overrides:
                    raise ConsoleError(
                        "这个平台走原生提交链路（取题/交卷都在平台页面里完成），"
                        "不支持在控制台逐题改答案；请到平台页面里改。",
                        kind="bad_request",
                        status=400,
                    )
                if not confirm:
                    plan = await adapter.native_plan(assignment_id)
                    plan.setdefault("title", target.title)
                    # 原生链路里 unipus 自己挂了信封；iTEST 没有，这里补齐。
                    if "kind" not in plan:
                        plan.update(_submit_plan_envelope(plan, assignment_id))
                    return plan
                result = await adapter.native_submit(assignment_id)
                result.setdefault("title", target.title)
                if "kind" not in result:
                    result.update(_submit_result_envelope(result, assignment_id))
                return result

            questions = list(target.questions)
            if not questions and getattr(adapter.capabilities, "fetch_questions", False):
                questions = await adapter.fetch_questions(target)
            if not questions:
                raise ConsoleError(
                    (
                        "批改网没有取题目接口（fetch_questions=False），通用提交拿不到题。"
                        "它的提交走「作业页 → 该行『评分循环』」：读稿件 / 交正文 /"
                        " 等批改 / 改写重交。"
                        if platform == "pigai"
                        else "没有取到题目，无法作答"
                    ), kind="bad_request", status=400
                )

            # 控制台是**人工复核**，不是自动批跑：低于阈值的候选答案不能丢，
            # 否则人看到的是「没答出来」，实际模型可能给了 0.7 的猜测。
            plan_key = self._plan_key(owner_id, platform, assignment_id)
            answers: list[Any] | None = None
            if confirm:
                # 真提交优先复用「人刚才看过的那份计划」：重跑答题链会让答案
                # 换一套，甚至冒出新的低置信度题，门槛看起来像在随机闪。
                answers = self._cached_answers(plan_key, questions)
                if answers is not None:
                    self._plog(platform, "info", "复用复核时的那份计划（答案未被重新生成）")
            if answers is None:
                answers = await _ask_for_review(runtime.pipeline.answerer, questions)
            if confirm and overrides:
                # 人工在控制台上改过答案：写回这份作答列表，再走同一套硬门槛。
                _apply_answer_overrides(answers, overrides)
            answerable = [a for a in answers if not a.is_empty()]
            weakest = min((a.confidence for a in answerable), default=0.0)
            plan = {
                "platform": platform,
                "assignment_id": assignment_id,
                "title": target.title,
                "question_count": len(questions),
                "answered": len(answerable),
                "min_confidence": round(weakest, 4),
                "threshold": runtime.settings.min_confidence,
                "dry_run": not confirm,
                # 控制台据此把干跑计划渲染成「可以逐题改答案」的编辑器。
                # 原生链路（iTEST / U校园）的计划形状不同，不置这个标志。
                "editable": True,
                "answers": [
                    {
                        "index": i + 1,
                        "stem": (a.question.stem or "")[:120],
                        "type": a.question.type.value,
                        "options": list(a.question.options),
                        "choices": a.choices,
                        "text": a.text,
                        # 给人看的答案（"A. 甲" / "A. 甲 / C. 丙"）：机器字段
                        # choices 仍是下标，但 CLI、日志、回退表格都不该出现裸 0。
                        "answer_text": answer_letters(
                            a.choices, a.question.options
                        ) or a.text.strip(),
                        "source": a.source.value,
                        "confidence": a.confidence,
                        "needs_human": a.confidence < runtime.settings.min_confidence,
                    }
                    for i, a in enumerate(answers)
                ],
            }

            if not confirm:
                plan["submitted"] = False
                note = "这是干跑结果，没有向平台提交任何内容。确认无误后带 confirm=true 再调用。"
                gate = ""
                if getattr(adapter, "allow_submit", True) is False:
                    # 只有这个平台「真的会被闸门拦住」时才提示，否则说明是错的。
                    gate = _text_or_empty(getattr(adapter, "submit_gate_hint", ""))
                if gate:
                    # 这个平台「真提交」只会存草稿：必须在点击之前说清楚，
                    # 否则用户会以为按钮没生效。
                    note += f"\n注意：{gate}"
                    plan["submit_gate"] = gate
                plan["note"] = note
                # 存下这份答案单：下一步「确认真提交」原样复用它。
                self._remember_submit_plan(plan_key, plan)
                plan.update(_submit_plan_envelope(plan, assignment_id))
                return plan

            if not answerable:
                raise ConsoleError("一题都没答出来，拒绝提交", kind="bad_request", status=400)
            if weakest < runtime.settings.min_confidence:
                raise ConsoleError(
                    f"最低置信度 {weakest:.2f} 低于阈值 {runtime.settings.min_confidence:.2f}，"
                    f"拒绝提交（宁可交白卷也不交错答案）。"
                    f"回到「提交计划」里把标了「需人工确认」的题核对一遍 —— "
                    f"改过或勾了「我确认这题」的题会以人工答案提交，不受这条门槛约束。",
                    kind="bad_request", status=400,
                )

            result = await adapter.submit(target, answers)
            if runtime.store is not None:
                runtime.store.record_submission(
                    platform, result, owner_id=owner_id
                )
            plan["submitted"] = True
            plan["ok"] = result.ok
            plan["score"] = result.score
            plan["message"] = result.message
            # 顶层也带一份：前端据此把列表里的行标成「已完成」。
            plan["server_finish"] = (result.raw or {}).get("server_finish")
            # 结果也走展示信封：没有 message 的平台、或只交了草稿的情况，
            # 用户都要能在抽屉里一眼看清「到底交没交」。
            plan.update(
                _submit_result_envelope(
                    {
                        "submitted": result.ok,
                        "ok": result.ok,
                        "score": result.score,
                        "message": result.message,
                        "server_finish": (result.raw or {}).get("server_finish"),
                    },
                    assignment_id,
                )
            )
            return plan
        finally:
            if adapter is not None:
                try:
                    persist_http_cookies(adapter, settings)
                    save_adapter_session(adapter, cookie_file)
                except Exception:  # noqa: BLE001
                    log.debug("保存会话失败", exc_info=True)
                await adapter.close()
            runtime.close()

    # -- 品课：计划 → 人工复核 → 提交 -------------------------------------

    def pinco_plan(
        self, viewer: Viewer | None = None, *, course: str = "", limit: int = 0
    ) -> dict[str, Any]:
        """把品课「要交什么」整理成一份可复核的计划（**全程只读**）。

        返回 ``kind=table`` 的信封，一行一个作业/学习任务（状态、阻断原因、
        题目数、该作业的内容哈希短码）；信封顶层的 ``digest`` 是整份计划的
        **内容哈希**，人工复核无误后必须原样带回 :meth:`pinco_submit_reviewed`。

        为什么分成「计划」与「提交」两步：品课的 ``submit()`` 里钉着一条守卫 ——
        没有登记过人工复核的计划一律拒绝执行。这条链路就是那条守卫要求的
        正确用法：先看计划，再确认，最后才发写请求。
        """
        try:
            return asyncio.run(
                self._pinco_plan_async(viewer=viewer, course=course, limit=limit)
            )
        except ConsoleError as exc:
            self._plog(PINCO, "error", f"✗ 生成品课提交计划：{exc}")
            raise

    async def _pinco_plan_async(
        self, *, viewer: Viewer | None, course: str, limit: int
    ) -> dict[str, Any]:
        self._plog(
            PINCO, "info", f"$ 生成品课提交计划（course={course!r}，limit={limit}）"
        )
        adapter, runtime, cookie_file, _owner_id, settings = await self._pinco_open(
            viewer, "生成计划"
        )
        try:
            plan = await self._pinco_replan(adapter, course=course, limit=limit)
        finally:
            await self._pinco_close(adapter, runtime, cookie_file, settings)
        out = self._pinco_envelope(plan, course=course, limit=limit)
        self._plog(PINCO, "ok", f"→ {plan.summary()}（digest={out['digest']}）")
        return out

    def pinco_submit_reviewed(
        self,
        viewer: Viewer | None = None,
        assignment_id: str = "",
        plan_digest: str = "",
        *,
        course: str = "",
        limit: int = 0,
    ) -> dict[str, Any]:
        """提交**人工复核过**的那个作业。

        ``plan_digest`` 必须是 :meth:`pinco_plan` 信封里那个完整 ``digest``。
        这里会重新抓一遍计划、比对内容哈希：

        - 一致 → ``confirm_plan(plan)`` 登记复核凭证，再从计划里还原答案并提交；
        - 不一致 → 400「计划已变化，请重新生成并复核」，**不会**按新内容偷偷交。

        存在的意义：品课提交报文的结构还没有真实样本确认（``submit_payload_confirmed
        = False``），所以现在这条路走到最后会被那道闸门挡住 —— 但挡住的方式是
        一条清楚的中文错误（``not_supported``），而不是把原始异常糊到用户脸上。
        """
        assignment_id = str(assignment_id or "").strip()
        plan_digest = str(plan_digest or "").strip()
        if not assignment_id:
            raise ConsoleError("缺少任务号", kind="bad_request", status=400)
        if not plan_digest:
            raise ConsoleError(
                "缺少计划 digest：先调 /api/platforms/pinco/plan 生成计划并复核，"
                "再把它信封里的 digest 原样带回。",
                kind="bad_request",
                status=400,
            )
        self._plog(
            PINCO,
            "info",
            f"$ 提交 {assignment_id}（已复核计划 digest={plan_digest}）",
        )
        try:
            out = asyncio.run(
                self._pinco_submit_reviewed_async(
                    viewer=viewer,
                    assignment_id=assignment_id,
                    plan_digest=plan_digest,
                    course=course,
                    limit=limit,
                )
            )
        except ConsoleError as exc:
            self._plog(PINCO, "error", f"✗ 提交 {assignment_id}：{exc}")
            raise
        if out.get("ok"):
            self._plog(PINCO, "ok", f"→ 提交 {assignment_id} 完成 score={out.get('score')}")
        else:
            self._plog(
                PINCO, "error", f"✗ 提交 {assignment_id} 结果不明：{out.get('message')}"
            )
        return out

    async def _pinco_submit_reviewed_async(
        self,
        *,
        viewer: Viewer | None,
        assignment_id: str,
        plan_digest: str,
        course: str,
        limit: int,
    ) -> dict[str, Any]:
        """复核 → 提交的异步主体（同步入口用 ``asyncio.run`` 包一层）。"""
        from kedaibiao.platforms.pinco.guard import SubmissionRefused
        from kedaibiao.platforms.pinco.models import PlanState

        adapter, runtime, cookie_file, owner_id, settings = await self._pinco_open(
            viewer, "提交前重新生成计划"
        )
        try:
            # 1) 重新 plan：这一遍既是「取最新内容」，也是 digest 的比对基准。
            plan = await self._pinco_replan(adapter, course=course, limit=limit)
            current = _pinco_content_digest(plan)
            if current != plan_digest:
                raise ConsoleError(
                    "计划已变化，请重新生成并复核"
                    f"（复核时 digest={plan_digest}，现在 digest={current}）。"
                    "复核之后题目或答案变过，旧的那份不能再用来提交。",
                    kind="bad_request",
                    status=400,
                )

            # 2) 登记复核凭证 —— 不登记的话 adapter.submit() 里第一道守卫就会拒绝。
            try:
                adapter.confirm_plan(plan)
            except SubmissionRefused as exc:
                raise self._pinco_error(exc, "登记复核计划") from exc

            item = plan.find(assignment_id)
            if item is None:
                raise ConsoleError(
                    f"作业 {assignment_id!r} 不在复核过的计划里，请重新生成计划并复核",
                    kind="bad_request",
                    status=400,
                )
            if item.state is PlanState.NEEDS_HUMAN:
                detail = "；".join(
                    f"{(q.stem.strip() or q.remote_id or '<无题干>')[:30]}（{q.reason}）"
                    for q in item.human_questions[:5]
                )
                raise ConsoleError(
                    f"作业 {assignment_id!r} 里有需要人工处理的题，整份作业不能自动提交："
                    f"{detail or item.reason or '有题目需要人工'}",
                    kind="bad_request",
                    status=400,
                )
            if item.state is not PlanState.PLANNED:
                raise ConsoleError(
                    f"作业 {assignment_id!r} 在计划里的状态是 {item.state.value}"
                    f"（{item.reason or '没有说明'}），只有 planned 的作业才允许提交",
                    kind="bad_request",
                    status=400,
                )

            # 3) 从复核过的计划项还原「作业 + 答案」，再交给平台适配器。
            assignment, answers = _pinco_submission_payload(item)
            try:
                result = await adapter.submit(assignment, answers)
            except SubmissionRefused as exc:
                raise self._pinco_error(exc, "提交") from exc
            except ConsoleError:
                raise
            except Exception as exc:  # noqa: BLE001 - 全部翻成受控错误再上抛
                raise self._pinco_submit_error(adapter, exc) from exc

            store = getattr(runtime, "store", None)
            if store is not None:
                try:
                    store.record_submission(PINCO, result, owner_id=owner_id)
                except Exception:  # noqa: BLE001 - 留痕失败不该把「已提交」变成 500
                    log.warning("品课提交留痕失败：%s", assignment_id, exc_info=True)
            answered = sum(1 for answer in answers if not answer.is_empty())
            return {
                "platform": PINCO,
                "action": "pinco-submit-reviewed",
                "assignment_id": assignment_id,
                "title": item.title,
                "course": item.course_name,
                "digest": plan_digest,
                "state": item.state.value,
                "submitted": True,
                "ok": bool(result.ok),
                "score": result.score,
                "message": result.message,
                "question_count": len(answers),
                "answered": answered,
            }
        finally:
            await self._pinco_close(adapter, runtime, cookie_file, settings)

    # -- 品课：内部辅助 ---------------------------------------------------

    async def _pinco_open(
        self, viewer: Viewer | None, what: str
    ) -> tuple[Any, Any, str, int, Any]:
        """建一个**已登录**的品课适配器，返回 (adapter, runtime, cookie_file,
        owner_id, settings)。

        每次都新建：品课的复核凭证 ``ReviewedPlan`` 只活在适配器实例里 ——
        「计划」和「提交」必须是两次独立调用、各自重新 plan，这样「复核之后
        计划变了」才可能被 digest 比对发现。共用一个长生命周期适配器反而
        会把两次请求之间的变化藏起来。
        """
        if PINCO not in adapters:
            raise ConsoleError(
                "品课（pinco）适配器没有加载：无法生成提交计划。",
                kind="not_supported",
                status=501,
            )
        cls: Any = adapters.get(PINCO)
        if not hasattr(cls, "plan") or not hasattr(cls, "confirm_plan"):
            raise ConsoleError(
                "当前品课适配器没有实现 plan()/confirm_plan()，这条链路不可用。",
                kind="not_supported",
                status=501,
            )
        creds, cookie_file = self._resolve_login(PINCO, viewer)
        settings = load_settings(root=self.root, environ=self.environ)
        owner_id = viewer.user_id if (viewer and viewer.authenticated) else 0
        runtime = build_runtime(
            root=self.root,
            environ=self._llm_environ(),
            cookie_file=cookie_file,
            owner_id=owner_id,
        )
        adapter: Any = None
        try:
            adapter = runtime.make_adapter(PINCO)
            restore_adapter_session(adapter, cookie_file)
            if not await adapter.refresh_session(creds):
                raise ConsoleError(
                    f"品课登录失败：{what}前需要先到「账号」页重新登录一次"
                    "（会话可能已失效，密码登录只解决身份层，业务接口还要求浏览器会话）。",
                    kind=PLATFORM_ERROR,
                    status=502,
                )
            return adapter, runtime, cookie_file, owner_id, settings
        except BaseException:
            if adapter is not None:
                with contextlib.suppress(Exception):
                    await adapter.close()
            with contextlib.suppress(Exception):
                runtime.close()
            raise

    async def _pinco_close(
        self, adapter: Any, runtime: Any, cookie_file: str, settings: Any
    ) -> None:
        """收尾：先落盘会话，再关适配器与运行时。落盘失败不影响本次结果。"""
        if adapter is not None:
            try:
                persist_http_cookies(adapter, settings)
                save_adapter_session(adapter, cookie_file)
            except Exception:  # noqa: BLE001
                log.debug("保存品课会话失败", exc_info=True)
            with contextlib.suppress(Exception):
                await adapter.close()
        if runtime is not None:
            with contextlib.suppress(Exception):
                runtime.close()

    async def _pinco_replan(self, adapter: Any, *, course: str, limit: int) -> Any:
        """抓一遍并产出计划（只读），再按课程过滤。两次入口共用这一段。

        ``limit`` 是**任务数上限**（0 = 不限制），直接交给平台的 ``plan()`` ——
        过滤必须发生在抓取阶段，否则「只看前 5 个」在复核时看到的和在提交时
        抓到的会是两份不同的东西。
        """
        try:
            plan = await adapter.plan(max_assignments=max(0, int(limit or 0)))
        except ConsoleError:
            raise
        except Exception as exc:  # noqa: BLE001 - 平台异常统一翻成受控错误
            raise self._pinco_error(exc, "生成计划") from exc
        return self._pinco_filter_plan(plan, course)

    @staticmethod
    def _pinco_filter_plan(plan: Any, course: str) -> Any:
        """按课程过滤计划，并**重新盖章**。

        为什么要重新 ``seal()``：计划的 digest 描述的是「用户复核过的那部分
        内容」。过滤掉别的课程之后还带着旧哈希，提交端的 digest 比对就会
        对不上 —— 那不是安全，只是把用户卡死。
        """
        needle = (course or "").strip().lower()
        if not needle:
            return plan
        from kedaibiao.platforms.pinco.models import PlanState

        def matches(item: Any) -> bool:
            fields = [item.course_name or "", item.assignment_id or ""]
            if item.state is PlanState.FAILED:
                # 整门课都没抓下来时 course_name 就是课名，title 也是课名
                fields.append(item.title or "")
            return any(needle in str(value).lower() for value in fields)

        plan.assignments = [item for item in plan.assignments if matches(item)]
        if not plan.assignments:
            raise ConsoleError(
                f"没有匹配的课程（course={course!r}）", kind="bad_request", status=400
            )
        return plan.seal()

    @staticmethod
    def _pinco_error(exc: BaseException, action: str) -> ConsoleError:
        """把品课异常翻成控制台错误（守住守卫的原因码）。"""
        from kedaibiao.platforms.pinco.guard import SubmissionRefused

        if isinstance(exc, SubmissionRefused):
            return ConsoleError(
                f"{action}被提交守卫拦下：[{exc.reason}] {exc.message}",
                kind="submission_refused",
                status=409,
            )
        if isinstance(exc, ConsoleError):
            return exc
        return ConsoleError(
            f"{action}失败：{_friendly_platform_error(PINCO, exc)}",
            kind=PLATFORM_ERROR,
            status=502,
        )

    @staticmethod
    def _pinco_submit_error(adapter: Any, exc: BaseException) -> ConsoleError:
        """提交阶段的异常 → 控制台错误。

        「提交报文结构还没被真实样本确认」是**已知且故意的**闸门，不是故障：
        它必须变 ``not_supported`` 的清楚中文，而不是一个 ``pinco error``。
        """
        if not getattr(adapter, "submit_payload_confirmed", True):
            return ConsoleError(
                "品课提交未执行：提交报文结构尚未由真实样本确认"
                "（submit_payload_confirmed=False），程序拒绝发出写请求。"
                "计划已复核、守卫已放行，但「交出去的东西对不对」还没验证过 —— "
                "未确认的报文不发。请先用真实抓包核对提交字段，再打开这道闸门。"
                f"（原始信息：{exc}）",
                kind="not_supported",
                status=501,
            )
        return ConsoleService._pinco_error(exc, "提交")

    @staticmethod
    def _pinco_envelope(plan: Any, *, course: str, limit: int) -> dict[str, Any]:
        """把计划渲染成控制台表格信封。

        行里放「能扫一眼做判断」的东西：状态、阻断原因、题目数、该作业的
        内容哈希短码；完整 digest 放顶层（提交时要原样带回）。
        """
        rows: list[dict[str, Any]] = []
        for item in plan.assignments:
            digest = item.content_digest() if item.state.value == "planned" else ""
            rows.append(
                {
                    "state": item.state.value,
                    "state_label": _PINCO_STATE_LABELS.get(
                        item.state.value, item.state.value
                    ),
                    "title": item.title,
                    "assignment_id": item.assignment_id,
                    "course": item.course_name or "—",
                    "kind": item.kind.value,
                    "question_count": len(item.questions),
                    "human_count": len(item.human_questions),
                    "digest": _pinco_short(digest),
                    "reason": item.reason or "—",
                }
            )
        digest = _pinco_content_digest(plan)
        out = table(
            platform=PINCO,
            action="pinco-plan",
            label="品课提交计划",
            title=f"品课提交计划（{len(rows)} 项，可提交 {len(plan.planned)}）",
            columns=[
                column("state_label", "状态"),
                column("title", "作业名"),
                column("assignment_id", "任务号", mono=True),
                column("course", "课程"),
                column("question_count", "题目数"),
                column("human_count", "待人工"),
                column("digest", "内容哈希", mono=True),
                column("reason", "说明"),
            ],
            rows=rows,
            note=(
                "本接口只读，没有向平台发出任何写请求。复核这张表无误后，"
                "带下面这个 digest 调 POST /api/platforms/pinco/submit-reviewed "
                "才会提交；提交前会重新抓一遍计划，内容对不上就整份拒绝。"
            ),
            empty_hint="没有抓到需要处理的作业或学习任务。",
        )
        out.update(
            {
                # 前端要原样带回的那个值（内容哈希，不含生成时间）
                "digest": digest,
                # 计划自身的盖章哈希：含 created_at，只用于排查，别拿来做比对
                "sealed_digest": plan.digest,
                "summary": plan.summary(),
                "counts": {
                    "total": len(plan.assignments),
                    "planned": len(plan.planned),
                    "needs_human": len(plan.needs_human),
                    "skipped": len(plan.skipped),
                    "failed": len(plan.failed),
                },
                "human_reasons": plan.human_reason_counts(),
                "created_at": plan.created_at.isoformat(timespec="seconds"),
                "course": course,
                "limit": limit,
            }
        )
        return out

    # -- 触发跑批 ---------------------------------------------------------

    def start_run(
        self,
        platform: str,
        *,
        live: bool = False,
        limit: int = 0,
        kinds: list[str] | None = None,
        viewer: Viewer | None = None,
    ) -> dict[str, Any]:
        """校验并启动一次跑批。校验全部前置，好让 HTTP 层返回准确的状态码。"""
        if platform not in adapters:
            known = ", ".join(adapters.names()) or "<未注册任何适配器>"
            raise ConsoleError(
                f"未注册的平台: {platform!r}；已注册: {known}",
                kind="unknown_platform", status=404,
            )

        cls: Any = adapters.get(platform)
        caps = getattr(cls, "capabilities", None)

        if live:
            if caps is not None and not getattr(caps, "submit", False):
                raise ConsoleError(
                    f"{getattr(cls, 'display_name', platform)} 本阶段不支持提交"
                    f"（capabilities.submit = False），不能以 live 模式运行。",
                    kind="bad_request", status=400,
                )

        # 干跑也要登录（要拉课程才能规划），所以凭据一律前置校验 ——
        # 放在这里比让后台线程抛异常再标 failed 更容易排查。
        creds, cookie_file = self._resolve_login(platform, viewer)
        owner_id = viewer.user_id if (viewer and viewer.authenticated) else 0

        parsed_kinds = _parse_kinds(kinds or [])
        limit = _clamp(int(limit), 0, 10_000)

        with self._lock:
            if self._active_job_id is not None:
                raise ConsoleError(
                    f"已有任务在跑（{self._active_job_id}），同一时刻只允许一个。",
                    kind="busy", status=409,
                )
            self._seq += 1
            job_id = f"j-{self._seq}"
            job: dict[str, Any] = {
                "job_id": job_id,
                "platform": platform,
                "live": bool(live),
                "status": "running",
                "started_at": _now(),
                "finished_at": None,
                "summary": "",
                "error": "",
                "records": [],
                "live_requested": bool(live),
                "limit": limit,
                "kinds": sorted(k.value for k in parsed_kinds),
                "owner_id": owner_id,
            }
            self._jobs.append(job)
            self._jobs_by_id[job_id] = job
            del self._jobs[:-JOB_HISTORY]
            for stale in list(self._jobs_by_id):
                if stale not in {j["job_id"] for j in self._jobs}:
                    del self._jobs_by_id[stale]
            self._active_job_id = job_id

        self._plog(platform, "info",
                   f"$ 跑批 {job_id}（{'live 真跑' if live else '干跑'}，"
                   f"kinds={sorted(k.value for k in parsed_kinds) or '全部'}，"
                   f"limit={limit or '不限'}）")
        thread = threading.Thread(
            target=self._execute,
            args=(job_id, platform, bool(live), limit, parsed_kinds, owner_id,
                  cookie_file, creds),
            name=f"kedaibiao-job-{job_id}",
            daemon=True,
        )
        thread.start()
        return _jsonable(job)

    # -- 刷课（按教材组织 + 任务模式时长 + 未完成任务提交）------------------

    def course_tree(
        self, platform: str, course_remote_id: str, *, viewer: Viewer | None = None,
        with_state: bool = True, state_scope: str = "required",
    ) -> dict[str, Any]:
        """一本书的单元/任务树（只读）。控制台「刷课」页点进教材时调用。"""
        scope = self._check_platform(platform)
        assert scope is not None
        course_remote_id = str(course_remote_id or "").strip()
        if not course_remote_id:
            raise ConsoleError("缺少课程号（course）", kind="bad_request", status=400)
        creds, cookie_file = self._resolve_login(scope, viewer)
        runtime = build_runtime(
            root=self.root, environ=self._llm_environ(), cookie_file=cookie_file,
            owner_id=viewer.user_id if (viewer and viewer.authenticated) else 0,
        )
        adapter: Any = None
        try:
            adapter = runtime.make_adapter(scope)
            restore_adapter_session(adapter, cookie_file)
            if not asyncio.run(adapter.refresh_session(creds)):
                raise ConsoleError(
                    f"{scope} 登录失败（会话可能已失效，请到「账号」页重新登录）",
                    kind=PLATFORM_ERROR, status=502,
                )
            if not hasattr(adapter, "native_course_tree"):
                raise ConsoleError(
                    f"{scope} 没有教材树能力（native_course_tree）",
                    kind="not_supported", status=501,
                )
            target = None
            for course in asyncio.run(adapter.list_courses()):
                if str(course.remote_id) == course_remote_id:
                    target = course
                    break
            if target is None:
                raise ConsoleError(
                    f"在 {scope} 里找不到课程 {course_remote_id!r}",
                    kind="not_found", status=404,
                )
            tree = asyncio.run(
                adapter.native_course_tree(
                    target, with_state=bool(with_state), state_scope=state_scope
                )
            )
            return _jsonable(tree)
        finally:
            if adapter is not None:
                with contextlib.suppress(Exception):
                    asyncio.run(adapter.close())
            runtime.close()

    def start_brush(
        self,
        platform: str,
        *,
        course: str,
        minutes: float = 10.0,
        confirm: bool = False,
        submit_pending: bool = False,
        only_required: bool = True,
        interval: float = 20.0,
        max_groups: int = 60,
        subjective_placeholder: str = "",
        llm_subjective: bool = False,
        resubmit_incomplete: bool = False,
        rooms: int = 6,
        mode: str = "",
        viewer: Viewer | None = None,
    ) -> dict[str, Any]:
        """刷课：一本书 →（可选）提交未完成任务 + 任务模式挂时长。

        ``mode`` 明确只做哪一半：``"submit"``（只提交未完成）、``"duration"``
        （只挂时长）、``"both"``；留空时按 ``submit_pending`` 推断。

        安全默认：``confirm=False`` 只出计划（不提交、不建连接）。
        ``submit_pending=True`` 才会尝试提交「没做过」的任务组；
        已做过的组**不会重交**（平台留存首条记录，实测重交不改分）。
        """
        scope = self._check_platform(platform)
        assert scope is not None
        course = str(course or "").strip()
        if not course:
            raise ConsoleError("缺少课程号（course）", kind="bad_request", status=400)
        cls: Any = adapters.get(scope)
        if not getattr(cls, "native_submit_flow", False):
            raise ConsoleError(
                f"{getattr(cls, 'display_name', scope)} 没有原生刷课链路",
                kind="not_supported", status=501,
            )
        try:
            minutes = float(minutes)
        except (TypeError, ValueError) as exc:
            raise ConsoleError("minutes 必须是数字", kind="bad_request", status=400) from exc
        if not (0.5 <= minutes <= 240):
            raise ConsoleError("minutes 要在 0.5~240 之间", kind="bad_request", status=400)
        interval = max(5.0, min(float(interval or 20.0), 120.0))
        try:
            max_groups = int(max_groups or 60)
        except (TypeError, ValueError):
            max_groups = 60
        max_groups = max(1, min(max_groups, 500))
        mode = str(mode or "").strip().lower()
        if mode and mode not in ("submit", "duration", "both"):
            raise ConsoleError("mode 只能是 submit / duration / both", kind="bad_request", status=400)
        if not mode:
            mode = "both" if submit_pending else "duration"
        submit_pending = mode in ("submit", "both")
        do_duration = mode in ("duration", "both")

        creds, cookie_file = self._resolve_login(scope, viewer)
        owner_id = viewer.user_id if (viewer and viewer.authenticated) else 0

        with self._lock:
            if self._active_job_id is not None:
                raise ConsoleError(
                    f"已有任务在跑（{self._active_job_id}），同一时刻只允许一个。",
                    kind="busy", status=409,
                )
            self._seq += 1
            job_id = f"r-{self._seq}"
            job: dict[str, Any] = {
                "job_id": job_id, "kind": "brush", "platform": scope,
                "course": course, "minutes": minutes, "confirm": bool(confirm),
                "submit_pending": bool(submit_pending), "only_required": bool(only_required),
                "mode": mode, "max_groups": max_groups,
                "status": "running", "started_at": _now(), "finished_at": None,
                "summary": "", "error": "", "logs": [],
                "counts": {"submitted": 0, "skipped": 0, "failed": 0,
                           "targets": 0, "credited_seconds": 0},
                "owner_id": owner_id,
            }
            self._jobs.append(job)
            self._jobs_by_id[job_id] = job
            del self._jobs[:-JOB_HISTORY]
            for stale in list(self._jobs_by_id):
                if stale not in {j["job_id"] for j in self._jobs}:
                    del self._jobs_by_id[stale]
            self._active_job_id = job_id

        self._plog(
            scope, "info",
            f"$ 刷课 {job_id}（{'真跑' if confirm else '干跑'}；{minutes:g} 分钟；"
            f"course={course}；提交未完成={submit_pending}）",
        )
        threading.Thread(
            target=self._run_brush,
            args=(job_id, scope, course, minutes, bool(confirm), bool(submit_pending),
                  bool(only_required), interval, owner_id, cookie_file, creds,
                  do_duration, mode, max_groups, str(subjective_placeholder or ""),
                  bool(llm_subjective), bool(resubmit_incomplete),
                  max(2, min(int(rooms or 6), 60))),
            name=f"kedaibiao-brush-{job_id}", daemon=True,
        ).start()
        return _jsonable(job)

    async def _llm_fill_group(
        self, job_id: str, adapter: Any, group: dict[str, Any]
    ) -> dict[str, list[str]]:
        """对「服务端没有标准答案」的题组用大模型生成内容。

        只处理翻译/写作/主观类（``needs_answer``），返回
        ``{instanceId: [子题文本...]}`` 供提交时覆盖。
        生成内容会写进日志（截断）便于人工复查。
        """
        from kedaibiao.tasks.llm import LLMAnswerer, LLMConfig

        config = LLMConfig.from_env(self._llm_environ())
        if not config.enabled:
            raise ConsoleError(
                "大模型未配置（控制台「设置」页填 BASE_URL 与 MODEL 后再试）",
                kind="bad_request", status=400,
            )
        plan = await adapter.native_plan(group["assignment_id"])
        rows = plan.get("questions") or []
        answerer = LLMAnswerer(config)
        overrides: dict[str, list[str]] = {}
        for row in rows:
            # 服务端有标准答案就不用模型：标准答案更准（翻译/阅读常带答案）
            if row.get("has_standard_answer"):
                continue
            if not (row.get("needs_answer") or row.get("wants_text")):
                continue
            instance_id = str(row.get("instanceId") or "")
            stem = str(row.get("stem") or "").strip()
            if not instance_id or not stem:
                continue
            kind = str(row.get("kind") or "")
            children = max(int(row.get("child_count") or 1), 1)
            # 判断题面语言：含中文才走「翻译」，英文题（阅读理解/简答）走「作答」。
            # 之前按题型标签一律当翻译，英文题会被喂「翻译助手」的提示，
            # 网关对此常返回空内容（实测）。
            has_cjk = any("\u4e00" <= ch <= "\u9fff" for ch in stem)
            if (kind == "translation" or "翻译" in str(group.get("type_label") or "")) and has_cjk:
                system = (
                    "你是英语翻译助手。把用户给的汉语逐句翻译成自然、准确的英语；"
                    "只输出译文，不要解释。"
                )
                if children > 1:
                    user = (
                        f"下面共有 {children} 句中文。请逐句翻译，**每句一行、共 {children} 行**，"
                        f"行首不要加编号或符号：\n{stem}"
                    )
                else:
                    user = f"请把下面的内容翻译成英语：\n{stem}"
            elif kind == "writing" or "写作" in str(group.get("type_label") or ""):
                system = (
                    "你是大学英语写作助手。按题目要求写一篇结构完整、"
                    "语言自然的英语短文，只输出正文。"
                )
                user = f"题目要求：\n{stem}"
            else:
                system = (
                    "你是大学英语答题助手。用英语简明作答，只输出答案正文。"
                )
                user = f"题目：\n{stem}"
            text = ""
            last_error = ""
            for attempt in range(1, 4):     # 网关会偶发返回空串/超时，重试 2 次
                try:
                    text = (await answerer.complete(system, user, max_tokens=800) or "").strip()
                except Exception as exc:  # noqa: BLE001
                    last_error = f"{type(exc).__name__}: {exc}"
                    text = ""
                if text:
                    break
                last_error = last_error or "网关返回空内容"
                self._job_log(
                    job_id, "warn",
                    f"  · 生成失败第 {attempt}/3 次（{last_error[:60]}）：{stem[:36]}…",
                )
                await asyncio.sleep(2.0 * attempt)
            if not text:
                self._job_log(
                    job_id, "error",
                    f"  ✗ 大模型未生成内容，该题跳过（原因：{last_error or '空响应'}）",
                )
                continue
            text = (text or "").strip()
            if not text:
                continue
            import re as _re

            parts = [
                _re.sub(r"^\s*\d+[.、)]\s*", "", part).strip()
                for part in text.split("\n")
                if part.strip()
            ]
            if children == 1:
                values = [text]
            elif len(parts) >= children:
                values = parts[:children]
            else:
                values = parts + [""] * (children - len(parts))
            overrides[instance_id] = values
            self._job_log(
                job_id, "info",
                f"  · 大模型作答（{instance_id}）：{text[:80].replace(chr(10), ' ')}…",
            )
        return overrides

    def _run_brush(
        self, job_id: str, platform: str, course_id: str, minutes: float,
        confirm: bool, submit_pending: bool, only_required: bool, interval: float,
        owner_id: int, cookie_file: str | None, credentials: Any,
        do_duration: bool = True, mode: str = "both", max_groups: int = 60,
        subjective_placeholder: str = "",
        llm_subjective: bool = False,
        resubmit_incomplete: bool = False,
        rooms: int = 6,
    ) -> None:
        from kedaibiao.platforms.unipus import duration as duration_mod

        runtime = None
        adapter: Any = None
        counts = {"submitted": 0, "skipped": 0, "failed": 0, "targets": 0,
                  "credited_seconds": 0}
        try:
            runtime = build_runtime(
                root=self.root, environ=self._llm_environ(),
                cookie_file=cookie_file, owner_id=owner_id,
            )
            adapter = runtime.make_adapter(platform)
            if subjective_placeholder and hasattr(adapter, "subjective_placeholder"):
                adapter.subjective_placeholder = str(subjective_placeholder)
                self._job_log(
                    job_id, "warn",
                    f"· 翻译/写作将用占位文本「{subjective_placeholder}」提交"
                    f"（服务端没有标准答案；能得完成分但内容不是人写的）",
                )
            restore_adapter_session(adapter, cookie_file)
            self._job_log(job_id, "info", "$ 恢复登录会话…")
            if not asyncio.run(adapter.refresh_session(credentials)):
                raise RuntimeError("登录失败：请到「账号」页重新登录一次")
            self._job_log(job_id, "ok", "✓ 会话就绪")

            course = None
            for item in asyncio.run(adapter.list_courses()):
                if str(item.remote_id) == course_id:
                    course = item
                    break
            if course is None:
                raise RuntimeError(f"找不到课程 {course_id}")
            self._job_log(job_id, "info", f"→ {course.name}")
            tree = asyncio.run(adapter.native_course_tree(course, with_state=True))
            groups = [g for unit in tree["units"] for g in unit["groups"]]
            required = [g for g in groups if g["required"]]
            scope_groups = [
                g for g in groups if (g["required"] or not only_required)
            ]
            done = [g for g in scope_groups if g.get("finished")]
            full = [g for g in done if g.get("full_marks")]
            incomplete = [g for g in done if not g.get("full_marks")]
            pending = [g for g in scope_groups if not g.get("finished")]
            if resubmit_incomplete:
                # 已完成但没全对的也重交（实测不改分，仅供排查/死马当活马医）
                pending = incomplete + pending
            self._job_log(
                job_id, "info",
                f"  {tree['group_count']} 个任务组，必修 {tree['required_count']}；"
                f"范围（{'仅必修' if only_required else '全部'}）{len(scope_groups)} 组："
                f"未做 {len(scope_groups) - len(done)}，已完成 {len(done)}"
                f"（其中满分 {len(full)}、未满分 {len(incomplete)}）；本次待提交 {len(pending)}",
            )
            if resubmit_incomplete and incomplete:
                self._job_log(
                    job_id, "warn",
                    f"· 已开启「已完成未满分也重交」：{len(incomplete)} 组会再交一次。"
                    f"注意：平台留存首条记录，实测重交**不改分**，只为排查",
                )

            stopped_any = False
            if len(pending) > max_groups:
                self._job_log(
                    job_id, "warn",
                    f"· 本轮最多提交 {max_groups} 组（剩余 {len(pending) - max_groups} 组未处理，"
                    f"可再跑一次；勾选「只处理必修」能少交很多）",
                )
                pending = pending[:max_groups]
            if not only_required:
                non_required = [g for g in pending if not g.get("required")]
                if non_required:
                    self._job_log(
                        job_id, "warn",
                        f"· 未勾选「只处理必修」：这 {len(non_required)} 组不在班级必修里，"
                        f"提交不会计入门户进度",
                    )
            if submit_pending:
                rate_limited = 0
                permission_denied = 0
                aborted = False
                stopped = False
                estimated_minutes = (len(pending) * (interval + 1.5)) / 60 if confirm else 0
                if confirm and pending:
                    self._job_log(
                        job_id, "info",
                        f"· 预计耗时约 {estimated_minutes:.0f} 分钟"
                        f"（{len(pending)} 组 × 约 {interval + 1.5:.0f}s）；"
                        f"随时可点「停止任务」",
                    )
                for index, group in enumerate(pending):
                    if self._cancelled(job_id):
                        stopped = True
                        stopped_any = True
                    if stopped or aborted:
                        counts["skipped"] += 1
                        continue
                    if not confirm:
                        counts["skipped"] += 1
                        self._job_log(
                            job_id, "info",
                            f"· 干跑：将提交 {group['name']}（{group['unit_name']}）",
                        )
                        continue
                    overrides: dict[str, list[str]] = {}
                    if llm_subjective and group.get("needs_answer"):
                        try:
                            overrides = asyncio.run(
                                self._llm_fill_group(job_id, adapter, group)
                            )
                        except Exception as exc:  # noqa: BLE001 - 生成失败就退回无覆盖
                            self._job_log(
                                job_id, "warn",
                                f"  · 大模型作答失败（{type(exc).__name__}: {exc}），"
                                f"该组退回占位/空答案",
                            )
                    if group.get("needs_answer") and not overrides and not subjective_placeholder:
                        counts["failed"] += 1
                        self._job_log(
                            job_id, "error",
                            f"✗ {group['name']} 跳过提交：服务端无标准答案，"
                            f"且大模型没有生成内容（空提交会记 0 分，而平台留存首条记录、"
                            f"之后改不了）",
                        )
                        continue
                    try:
                        result = asyncio.run(
                            adapter.native_submit(
                                group["assignment_id"], answer_overrides=overrides or None
                            )
                        )
                        counts["submitted"] += 1
                        rate_limited = 0
                        self._job_log(
                            job_id, "ok",
                            f"✓ 已提交 {group['name']}：code={result.get('code')!r}",
                        )
                        if index < len(pending) - 1:
                            if self._sleep_interruptible(job_id, interval):
                                stopped = True
                                stopped_any = True
                    except Exception as exc:  # noqa: BLE001
                        counts["failed"] += 1
                        code = getattr(exc, "code", None)
                        is_limit = type(exc).__name__ == "UnipusRateLimited"
                        if is_limit:
                            rate_limited += 1
                        elif code == 100002:
                            permission_denied += 1
                        hint = ""
                        if code == 100002:
                            activated = tree.get("activated")
                            state_text = {True: "已激活", False: "**未激活**"}.get(
                                activated, "激活状态未知"
                            )
                            hint = (
                                f"（没有权限；教材{state_text}：未激活的教材无法提交，"
                                "需要先在网页端输入激活码；也可能是任务在本班还没打开过）"
                            )
                        self._job_log(
                            job_id, "error",
                            f"✗ {group['name']} 提交失败：{type(exc).__name__}: {exc}{hint}",
                        )
                        if is_limit:
                            # 平台明确说「休息几分钟」：继续打只会把号打进更长的冷却
                            wait = float(getattr(exc, "retry_after", 0) or 0)
                            self._job_log(
                                job_id, "warn",
                                f"  ⚠️ 平台限流（{code}）：停止本轮提交，剩余 "
                                f"{len(pending) - index - 1} 个未尝试。建议等 "
                                f"{max(wait, 180):.0f} 秒以上再跑。",
                            )
                            aborted = True
                        elif permission_denied >= 2:
                            self._job_log(
                                job_id, "warn",
                                "  ⚠️ 连续 2 个任务「没有权限」（100002）：停止本轮提交。"
                                "常见原因是这本教材还没激活（网页端需输入激活码）"
                                "或任务在本班还没打开过。",
                            )
                            aborted = True
                        elif rate_limited >= 2:
                            aborted = True
            else:
                self._job_log(job_id, "info", "· 未勾选「提交未完成后任务」，只刷时长")

            if not do_duration:
                counts["targets"] = 0
                summary = (
                    f"完成：提交 {counts['submitted']}，跳过 {counts['skipped']}，"
                    f"失败 {counts['failed']}（未刷时长）"
                )
                with self._lock:
                    job = self._jobs_by_id.get(job_id)
                    if job is not None:
                        job["counts"] = counts
                self._finish_batch_job(job_id, "done", summary=summary)
                self._job_log(job_id, "ok", f"■ {summary}")
                self._plog(platform, "ok", f"■ 刷课 {job_id} {summary}")
                return

            # 多房间刷时长（实测：单房间不记账，6 房间 150s 记 509s）
            from kedaibiao.platforms.unipus import endpoints as unipus_endpoints

            token = adapter.client().auth_headers(
                unipus_endpoints.UnipusApp.UCONTENT
            )[unipus_endpoints.UCONTENT_AUTH_HEADER]
            counts["targets"] = 0
            pool_report = duration_mod.run_duration_pool(
                adapter.client().session.open_id,
                token,
                seconds=minutes * 60,
                rooms=int(rooms or 6),
                confirm=confirm,
            )
            counts["rooms"] = len(pool_report.plan.rooms)
            self._job_log(
                job_id, "info",
                f"· 并发数（房间）：{len(pool_report.plan.rooms)}"
                + ("（超过安全区 20，可能握手被拒）" if len(pool_report.plan.rooms) > 20 else ""),
            )
            counts["credited_seconds"] = int(pool_report.credited_seconds)
            counts["r_set"] = pool_report.r_set_updates
            for line in pool_report.plan.warnings:
                self._job_log(job_id, "warn", f"⚠️ {line}")
            if not confirm:
                self._job_log(job_id, "info", f"· 干跑计划：{pool_report.plan.summary()}")
                self._job_log(
                    job_id, "info",
                    f"· 将连 {len(pool_report.plan.rooms)} 条连接："
                    + "、".join(f"{m}/{g}" for m, g in pool_report.plan.rooms),
                )
            else:
                self._job_log(
                    job_id, "info",
                    f"· 房间池：连接 {pool_report.rooms_connected} 个 / kick {pool_report.rooms_kicked} 个；"
                    f"记账 {pool_report.credited_seconds:.0f} 秒（r_set {pool_report.r_set_updates} 条）",
                )
                for name, credited in list(pool_report.per_room.items())[:6]:
                    self._job_log(job_id, "info", f"    {name}: {credited:.0f}s")
                for err in pool_report.errors[:3]:
                    self._job_log(job_id, "warn", f"    {err}")
            counts["targets"] = len(pool_report.plan.rooms)
            report = pool_report
            prefix = "已停止" if stopped_any else "完成"
            summary = (
                f"{prefix}：提交 {counts['submitted']}，跳过 {counts['skipped']}，"
                f"失败 {counts['failed']}，时长目标 {counts['targets']} 个，"
                f"记账 {counts['credited_seconds']} 秒"
            )
            with self._lock:
                job = self._jobs_by_id.get(job_id)
                if job is not None:
                    job["counts"] = counts
            self._finish_batch_job(job_id, "done", summary=summary)
            self._job_log(job_id, "ok", f"■ {summary}")
            self._plog(platform, "ok", f"■ 刷课 {job_id} {summary}")
        except Exception as exc:  # noqa: BLE001
            message = f"{type(exc).__name__}: {exc}"
            self._finish_batch_job(job_id, "failed", error=message)
            self._job_log(job_id, "error", f"✗ {message}")
            self._plog(platform, "error", f"✗ 刷课 {job_id}：{message}")
            log.exception("刷课失败: %s", job_id)
        finally:
            if adapter is not None:
                with contextlib.suppress(Exception):
                    asyncio.run(adapter.close())
            if runtime is not None:
                with contextlib.suppress(Exception):
                    runtime.close()
            with self._lock:
                if self._active_job_id == job_id:
                    self._active_job_id = None

    # -- 批量提交（原生提交链路平台，例如 U校园）---------------------------

    def start_batch_submit(
        self,
        platform: str,
        *,
        course: str = "",
        confirm: bool = False,
        limit: int = 0,
        interval: float = 8.0,
        skip_done: bool = True,
        viewer: Viewer | None = None,
    ) -> dict[str, Any]:
        """一键全部提交：后台任务，逐组「读状态 → 干跑计划 →（确认后）真交」。

        - ``confirm=False``（默认）= **只规划不提交**，回报「哪些会交、哪些跳过/被拦」；
        - ``confirm=True`` 才逐组真的写平台（不可逆），组间默认停 ``interval`` 秒；
        - ``skip_done=True``（默认）跳过**所有已完成**（``first_pass`` 有值）的组。
          为什么连"没全对"也跳过：2026-10-20 实测，平台的留存策略
          ``record_every_submit=false`` —— 一组只保留首条记录，重交**不会改分**
          （提交前后 ``state.score`` 完全一致）。想覆盖旧记录只能人工在网页端重做；
        - ``course`` 可填课程号或课程名片段，留空 = 全部课程；
        - ``limit`` 最多处理多少个任务（0 = 不限），给首次试跑限流用。

        任务进度写进 ``job.logs``（同时进平台日志），前端按 ``job_id`` 轮询
        ``GET /api/jobs/<id>``。同一时刻只允许一个任务（与跑批共用闸门）。
        """
        scope = self._check_platform(platform)
        assert scope is not None
        cls: Any = adapters.get(scope)
        caps = getattr(cls, "capabilities", None)
        if caps is not None and not getattr(caps, "submit", False):
            raise ConsoleError(
                f"{getattr(cls, 'display_name', scope)} 尚未实现提交"
                f"（capabilities.submit = False），不能批量提交。",
                kind="not_supported", status=501,
            )
        if not getattr(cls, "native_submit_flow", False):
            raise ConsoleError(
                f"{getattr(cls, 'display_name', scope)} 没有原生提交链路"
                f"（native_plan/native_submit），暂不支持一键批量提交。",
                kind="not_supported", status=501,
            )

        course = str(course or "").strip()
        limit = _clamp(int(limit or 0), 0, 10_000)
        try:
            interval = float(interval)
        except (TypeError, ValueError):
            interval = 8.0
        interval = max(0.5, min(interval, 120.0))

        # 先校验账号存在（批量任务里再登录），这样 HTTP 层能立刻回 400/404
        creds, cookie_file = self._resolve_login(scope, viewer)
        owner_id = viewer.user_id if (viewer and viewer.authenticated) else 0

        with self._lock:
            if self._active_job_id is not None:
                raise ConsoleError(
                    f"已有任务在跑（{self._active_job_id}），同一时刻只允许一个。",
                    kind="busy", status=409,
                )
            self._seq += 1
            job_id = f"b-{self._seq}"
            job: dict[str, Any] = {
                "job_id": job_id,
                "kind": "batch-submit",
                "platform": scope,
                "course": course,
                "confirm": bool(confirm),
                "limit": limit,
                "interval": interval,
                "skip_done": bool(skip_done),
                "status": "running",
                "started_at": _now(),
                "finished_at": None,
                "summary": "",
                "error": "",
                "logs": [],
                "items": [],
                "counts": {
                    "planned": 0, "submitted": 0, "skipped": 0,
                    "blocked": 0, "failed": 0,
                },
                "owner_id": owner_id,
            }
            self._jobs.append(job)
            self._jobs_by_id[job_id] = job
            del self._jobs[:-JOB_HISTORY]
            for stale in list(self._jobs_by_id):
                if stale not in {j["job_id"] for j in self._jobs}:
                    del self._jobs_by_id[stale]
            self._active_job_id = job_id

        self._plog(
            scope, "info",
            f"$ 批量提交 {job_id}（{'真交，不可逆' if confirm else '干跑，不提交'}；"
            f"course={course or '全部课程'}，limit={limit or '不限'}）",
        )
        threading.Thread(
            target=self._run_batch_submit,
            args=(job_id, scope, course, bool(confirm), limit, interval,
                  bool(skip_done), owner_id, cookie_file, creds),
            name=f"kedaibiao-batch-{job_id}",
            daemon=True,
        ).start()
        return _jsonable(job)

    def _run_batch_submit(
        self,
        job_id: str,
        platform: str,
        course: str,
        confirm: bool,
        limit: int,
        interval: float,
        skip_done: bool,
        owner_id: int,
        cookie_file: str | None,
        credentials: Any,
    ) -> None:
        """批量提交的后台线程：逐课程 → 逐任务，单组失败不拖垮整批。"""
        runtime = None
        adapter: Any = None
        counts: dict[str, int] = {}
        try:
            runtime = build_runtime(
                root=self.root, environ=self._llm_environ(),
                cookie_file=cookie_file, owner_id=owner_id,
            )
            adapter = runtime.make_adapter(platform)
            restore_adapter_session(adapter, cookie_file)
            self._job_log(job_id, "info", "$ 恢复登录会话…")
            if not asyncio.run(adapter.refresh_session(credentials)):
                raise RuntimeError(
                    "登录失败：会话无效且无法自动登录，请到「账号」页重新登录一次"
                )
            self._job_log(job_id, "ok", "✓ 会话就绪")

            courses = asyncio.run(adapter.list_courses())
            if course:
                needle = course.lower()
                courses = [
                    c for c in courses
                    if str(c.remote_id) == course
                    or needle in (c.name or "").lower()
                    or needle in str(c.remote_id).lower()
                ]
            if not courses:
                raise RuntimeError(f"没有匹配的课程（course={course!r}）")
            self._job_log(job_id, "info", f"· 课程 {len(courses)} 门")

            processed = 0
            for target in courses:
                if limit and processed >= limit:
                    break
                self._job_log(
                    job_id, "info", f"→ {target.name}（{target.remote_id}）"
                )
                assignments = asyncio.run(adapter.list_assignments(target))
                self._job_log(job_id, "info", f"  {len(assignments)} 个任务")
                for assignment in assignments:
                    if limit and processed >= limit:
                        break
                    processed += 1
                    self._batch_one(
                        job_id, adapter, assignment,
                        confirm=confirm, skip_done=skip_done, interval=interval,
                    )
                    if limit and processed >= limit:
                        break
            counts = self._batch_counts(job_id)
            summary = (
                f"完成：可提交 {counts['planned']}，真交 {counts['submitted']}，"
                f"跳过 {counts['skipped']}，被拦 {counts['blocked']}，"
                f"失败 {counts['failed']}"
            )
            self._finish_batch_job(job_id, "done", summary=summary)
            self._job_log(job_id, "ok", f"■ {summary}")
            self._plog(platform, "ok", f"■ 批量提交 {job_id} {summary}")
        except Exception as exc:  # noqa: BLE001 - 后台任务必须落成失败状态
            counts = self._batch_counts(job_id)
            message = f"{type(exc).__name__}: {exc}"
            self._finish_batch_job(job_id, "failed", error=message)
            self._job_log(job_id, "error", f"✗ {message}")
            self._plog(platform, "error", f"✗ 批量提交 {job_id}：{message}")
            log.exception("批量提交失败: %s", job_id)
        finally:
            if adapter is not None:
                with contextlib.suppress(Exception):
                    asyncio.run(adapter.close())
            if runtime is not None:
                with contextlib.suppress(Exception):
                    runtime.close()
            with self._lock:
                if self._active_job_id == job_id:
                    self._active_job_id = None

    def _batch_one(
        self,
        job_id: str,
        adapter: Any,
        assignment: Any,
        *,
        confirm: bool,
        skip_done: bool,
        interval: float,
    ) -> None:
        """处理一个任务组：读状态 → 干跑计划 →（确认后）真交。"""
        assignment_id = str(getattr(assignment, "remote_id", "") or "")
        title = str(getattr(assignment, "title", "") or assignment_id)
        if assignment_id:
            # 同名的任务很多（每单元都有 Exercise 1），日志里带上分组号便于对照
            title = f"{title}·{assignment_id.rsplit('/', 1)[-1][:8]}"
        if not assignment_id:
            self._bump(job_id, "failed")
            self._job_log(job_id, "warn", "· 跳过：任务号为空")
            return

        state: dict[str, Any] = {}
        if hasattr(adapter, "native_assignment_state"):
            try:
                state = asyncio.run(adapter.native_assignment_state(assignment_id)) or {}
            except Exception as exc:  # noqa: BLE001 - 状态读不到就当作未知，不拦
                self._job_log(job_id, "warn", f"· {title}：状态读取失败（{type(exc).__name__}），按未完成处理")

        # 「要不要跳过」只看**逐项得分**：平台的 score_pct 不是正确率
        # （实测：score=[1,1,1,1,1,0,0,0,0,0] 也回 score_pct=1）。
        if skip_done and state.get("finished"):
            total_items = int(state.get("total_items") or 0)
            correct_items = int(state.get("correct_items") or 0)
            if not total_items:
                reason = "已完成（无计分点：纯学习/内容组）"
            elif correct_items == total_items:
                reason = f"已满分（{correct_items}/{total_items} 项全对）"
            else:
                # 没全对也跳过：平台留存首条记录，重交不改分（实测）
                reason = (f"已完成但未全对（{correct_items}/{total_items} 项）；"
                          f"平台留存首条记录，重交不改分")
            self._bump(job_id, "skipped")
            self._job_log(job_id, "info", f"· 跳过 {title}（{reason}）")
            return

        try:
            plan = asyncio.run(adapter.native_plan(assignment_id))
        except Exception as exc:  # noqa: BLE001
            self._bump(job_id, "failed")
            self._job_log(job_id, "error", f"✗ {title}：取计划失败 {type(exc).__name__}: {exc}")
            return

        blockers = [str(item) for item in (plan.get("blockers") or [])]
        if not plan.get("can_submit", True):
            self._bump(job_id, "blocked")
            self._job_log(
                job_id, "warn",
                f"· 被拦 {title}：" + ("；".join(blockers) or "本地校验未通过"),
            )
            return

        self._bump(job_id, "planned")
        detail = (
            f"题目 {plan.get('question_count')} · 计分点 {plan.get('item_count')}"
            f" · submitType {plan.get('submit_type')}"
        )
        if not confirm:
            self._job_log(job_id, "ok", f"✓ 可提交 {title}（{detail}）")
            return

        try:
            result = asyncio.run(adapter.native_submit(assignment_id))
        except Exception as exc:  # noqa: BLE001
            retry_after = getattr(exc, "retry_after", None)
            self._bump(job_id, "failed")
            self._job_log(job_id, "error", f"✗ {title}：提交失败 {type(exc).__name__}: {exc}")
            if isinstance(retry_after, (int, float)) and retry_after > 0:
                wait = min(float(retry_after), 120.0)
                self._job_log(job_id, "warn", f"   撞上限流，等待 {wait:.0f}s 再继续")
                time.sleep(wait)
            return

        self._bump(job_id, "submitted")
        self._job_log(
            job_id, "ok",
            f"✓ 已提交 {title}：code={result.get('code')!r} "
            f"得分率={result.get('score_pct')} "
            f"finish={result.get('server_finish')}",
        )
        if interval:
            time.sleep(interval)

    def _batch_counts(self, job_id: str) -> dict[str, int]:
        with self._lock:
            job = self._jobs_by_id.get(job_id)
            return dict(job.get("counts") or {}) if job else {}

    def _bump(self, job_id: str, key: str) -> None:
        with self._lock:
            job = self._jobs_by_id.get(job_id)
            if job is None:
                return
            counts = job.setdefault("counts", {})
            counts[key] = int(counts.get(key) or 0) + 1

    def _finish_batch_job(
        self,
        job_id: str,
        status: str,
        *,
        summary: str = "",
        error: str = "",
    ) -> None:
        with self._lock:
            job = self._jobs_by_id.get(job_id)
            if job is None:
                return
            job["status"] = status
            job["finished_at"] = _now()
            if summary:
                job["summary"] = summary
            if error:
                job["error"] = error

    def cancel_job(self, job_id: str) -> dict[str, Any]:
        """请求停止一个后台任务（刷课 / 批量提交）。

        只置标记，真正的停在后台线程的下一个检查点生效 —— 正在飞的那一个
        请求会照常完成，不会留下半提交状态。
        """
        job_id = str(job_id or "").strip()
        with self._lock:
            job = self._jobs_by_id.get(job_id)
            if job is None:
                raise ConsoleError(
                    f"任务 {job_id!r} 不存在或已被清理", kind="not_found", status=404
                )
            if job.get("status") != "running":
                return _jsonable(job)
            self._cancel_requested.add(job_id)
            job["cancel_requested"] = True
        self._job_log(job_id, "warn", "· 收到停止请求：处理完当前这一组就停")
        return _jsonable(self._jobs_by_id.get(job_id) or job)

    def _cancelled(self, job_id: str) -> bool:
        with self._lock:
            return job_id in self._cancel_requested

    def _sleep_interruptible(self, job_id: str, seconds: float) -> bool:
        """分片睡；返回 True 表示期间收到了停止请求。"""
        remaining = float(seconds)
        while remaining > 0:
            if self._cancelled(job_id):
                return True
            step = min(1.0, remaining)
            time.sleep(step)
            remaining -= step
        return self._cancelled(job_id)

    def wait_for_idle(self, timeout: float = 30.0) -> bool:
        """等当前任务结束。给测试与优雅关闭用。"""
        deadline = threading.Event()
        waited = 0.0
        while waited < timeout:
            with self._lock:
                if self._active_job_id is None:
                    return True
            deadline.wait(0.05)
            waited += 0.05
        return False

    # -- 内部 -------------------------------------------------------------

    def _execute(
        self,
        job_id: str,
        platform: str,
        live: bool,
        limit: int,
        kinds: frozenset[TaskKind],
        owner_id: int = 0,
        cookie_file: str | None = None,
        credentials: Any = None,
    ) -> None:
        """在后台线程里跑一次流水线。

        每个任务单独装配一个 Runtime：它持有自己的 sqlite 连接，而 sqlite
        连接不能跨线程共享。跑完必须 close，否则连接会泄漏。
        """
        runtime = None
        try:
            settings = load_settings(root=self.root).merged({"dry_run": not live})
            runtime = build_runtime(
                root=self.root,
                settings=settings,
                config=PipelineConfig(
                    dry_run=not live,
                    min_confidence=settings.min_confidence,
                    kinds=kinds,
                    max_assignments=limit,
                ),
                owner_id=owner_id,
                cookie_file=cookie_file,
            )
            runtime.bus.on("*", self._record_event)
            if cookie_file:
                # 跑批同样使用「恢复会话」这条路，避免明明登录过却报缺凭据
                pass

            report = asyncio.run(runtime.run(platform, credentials=credentials))

            self._plog(platform, "ok" if report.failed == 0 else "warn",
                       f"→ 跑批 {job_id} 结束：{report.summary()}")
            with self._lock:
                job = self._jobs_by_id[job_id]
                job["summary"] = report.summary()
                job["status"] = "succeeded" if report.failed == 0 else "failed"
                job["records"] = [
                    {
                        "assignment_id": r.assignment_id,
                        "status": r.status.value,
                        "note": r.error or str(r.detail.get("reason", "")),
                    }
                    for r in report.records
                ]
        except BaseException as exc:  # noqa: BLE001 - 后台线程里必须兜住，否则静默死掉
            log.exception("跑批任务 %s 异常结束", job_id)
            self._plog(platform, "error", f"✗ 跑批 {job_id}：{type(exc).__name__}: {exc}")
            with self._lock:
                job = self._jobs_by_id[job_id]
                job["status"] = "failed"
                job["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            if runtime is not None:
                runtime.close()
            with self._lock:
                job = self._jobs_by_id[job_id]
                job["finished_at"] = _now()
                if self._active_job_id == job_id:
                    self._active_job_id = None

    def _record_event(self, event: Any) -> None:
        entry = {
            "at": _jsonable(getattr(event, "at", datetime.now())),
            "name": getattr(event, "name", "?"),
            "payload": _jsonable(getattr(event, "payload", {})),
        }
        with self._lock:
            self._events.append(entry)
            del self._events[:-EVENT_HISTORY]


def _blank_platform_row(name: str) -> dict[str, Any]:
    """「统计」页对比表的一行空白模板。

    没有数据的平台也要出现 —— 缺行会让人以为那个平台不存在。
    """
    return {
        "platform": name,
        "total": 0,
        "succeeded": 0,
        "failed": 0,
        "skipped": 0,
        "submissions": 0,
        "submit_ok": 0,
        "questions": 0,
        "accuracy": None,
    }


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def _parse_kinds(kinds: list[str]) -> frozenset[TaskKind]:
    parsed = set()
    for raw in kinds:
        name = str(raw).strip()
        if not name:
            continue
        try:
            parsed.add(TaskKind(name))
        except ValueError as exc:
            valid = ", ".join(k.value for k in TaskKind)
            raise ConsoleError(
                f"未知的任务类型: {name!r}；可选 {valid}", kind="bad_request", status=400
            ) from exc
    return frozenset(parsed)
