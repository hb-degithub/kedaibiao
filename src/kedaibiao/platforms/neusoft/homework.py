"""东软：学习任务（作业 / 测验）—— 拉列表、取卷、进入作答、存草稿、交卷。

## 为什么作业的入口不是课程资源

作业混在「学习任务」列表里，和视频共用一个接口，**必须按 ``type`` 过滤**：
1=视频/资料、3=作业/测验。不过滤就会拿视频的 ``resId`` 去当 ``testId`` 取卷。
另一个致命坑是**只传 ``termId``、不传 ``currentClassId``** —— 带上课程上下文
服务端一律回 ``403 您权限不足或者非法访问``（见 ``docs/research/neusoft.md`` §5.1）。

## 读路径与写路径的置信度不同

``study-task/page``、``paper``、``enter`` 有真实账号实测证据（``[A]``）；
``answer/save`` 与 ``submit`` 只有 APK 静态分析证据（``[B]``），本仓库没有真实
样本复测过。因此：

- 保存草稿**逐条校验四个必填字段** —— 宁可一条不发，也不发半条让服务端静默丢题；
- 交卷**默认拒绝**：不可逆、没有撤销接口，只认调用方的显式 ``confirm=True``。

题型映射同样只登记档案证实过的取值，见 :data:`QUESTION_TYPE_MAP`。
"""

from __future__ import annotations

import html
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from kedaibiao.core.models import Question, QuestionType
from kedaibiao.platforms.neusoft import endpoints as ep
from kedaibiao.platforms.neusoft import paging as paging_rules
from kedaibiao.platforms.neusoft.client import NeusoftClient
from kedaibiao.platforms.neusoft.errors import NeusoftError, NeusoftProtocolError
from kedaibiao.platforms.neusoft.models import as_float, as_int, as_list, as_str

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# 解析产物（只服务本模块，所以不放 models.py —— 那里是跨模块冲突热点）
# --------------------------------------------------------------------------


@dataclass(slots=True)
class StudyTask:
    """学习任务列表里的一行作业。

    ``test_id`` 就是列表里的 ``ctlId``：平台在作答接口里把它叫 ``testId``，
    同一个值有两个名字。这里只留 ``test_id`` 作为真名，``ctl_id`` 保留成只读
    别名，方便对着协议档案读代码。
    """

    test_id: str
    name: str = ""
    course_name: str = ""
    study_status: int = 0
    open_time: str = ""
    #: 平台原始类型：2 = 单元测试 / 测验，3 = 作业（两者都用 testId 取卷）
    task_type: int = 0
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def ctl_id(self) -> str:
        """``ctlId`` 的别名 —— 档案 §5.2：``ctlId == resCtl.id == testId``。"""
        return self.test_id

    @property
    def is_completed(self) -> bool:
        return self.study_status == ep.STUDY_STATUS_DONE


@dataclass(slots=True, frozen=True)
class Choice:
    """一个选项。``choice_id`` 用于回填，``content`` 用于题库匹配。"""

    choice_id: str
    content: str


@dataclass(slots=True)
class PaperQuestion:
    """试卷里的一道题。

    字段名沿用平台原样（``paperId`` → ``paper_id``），因为保存草稿时
    ``paperId`` / ``questionNo`` / ``questionType`` 三项要原样回传（§5.3）。

    ⚠️ 真实响应里这三项**不在同一层**（2026-10-22 实测，见
    ``data/recon/neusoft-paper-*.json``）::

        paperQuestionList[]: {id, paperId, partId, questionType,
                              paperQuestion: {questionNo, questionType, stemText, choices, …}}

    - ``paperId`` 在外层，**整份试卷共用一个值**（等于响应顶层的 ``id``），
      不能拿它当「一道题」的主键；
    - ``id`` 在外层且逐题唯一，才是题目行的主键（:attr:`question_id`）；
    - ``questionNo`` / ``stemText`` / ``choices`` 在内层。
    """

    paper_id: str = ""
    #: 外层 ``id``：逐题唯一的题目行主键。``questionNo`` 才是人看的题号。
    question_id: str = ""
    question_no: str = ""
    question_type: str = ""
    stem: str = ""
    choices: tuple[Choice, ...] = ()
    #: 三态字段（数组 / 逗号串 / null）统一成元组
    candidate_answer: tuple[str, ...] = ()
    standard_answer: tuple[str, ...] = ()
    #: 批改后才有的分数（未批改时平台给 null）
    candidate_score: float | None = None
    question_score: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        """作答匹配用的主键：逐题 ``id`` 优先，退化到 ``paperId``。

        两种已知形状各自唯一：真实形状靠外层 ``id``（逐题唯一），旧假设的形状里
        ``paperId`` 本身就是逐题的值。两者都缺的行没有可匹配的键，返回空串 ——
        调用方会跳过它，绝不拿 ``questionNo`` 去猜（题型分组后题号会重复）。
        """
        return self.question_id or self.paper_id


@dataclass(slots=True)
class Paper:
    """一份试卷：``paperParts`` 里的题目按原顺序拍平到 :attr:`questions`。"""

    test_id: str
    questions: list[PaperQuestion] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def question_count(self) -> int:
        return len(self.questions)


@dataclass(slots=True)
class Entered:
    """进入作答的结果。

    ``enter`` 幂等：连调多次返回同一个 ``historyId``，不消耗作答次数。
    """

    test_id: str
    candidate_id: str = ""
    history_id: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_ready(self) -> bool:
        """能否保存草稿：这两个 ID 缺一个，save 和 submit 都用不了。"""
        return bool(self.test_id and self.candidate_id and self.history_id)


# --------------------------------------------------------------------------
# 题型映射
# --------------------------------------------------------------------------

#: 平台 ``questionType`` → ``core.QuestionType``。
#:
#: **只登记档案证实过的取值**（``docs/research/neusoft.md`` §5.4）：``"4"`` 是多空填空。
#: 组合题 ``"8"`` / ``"10"`` 是「父题 + 子题」，core 里没有对应枚举，也归 ``UNKNOWN``。
#:
#: 其余取值一律 ``UNKNOWN`` —— 平台枚举不全（§8 待补）。按「1 单选 / 2 多选 /
#: 3 判断」这种直觉补表，会把判断题当单选回填，答案判错且没有任何报错，比不知道更糟。
QUESTION_TYPE_MAP: dict[str, QuestionType] = {
    ep.QUESTION_TYPE_SINGLE: QuestionType.SINGLE,
    ep.QUESTION_TYPE_JUDGE: QuestionType.JUDGE,
    ep.QUESTION_TYPE_FILL_MULTI: QuestionType.FILL,
}


def core_question_type(platform_type: Any) -> QuestionType:
    """平台题型 → ``core.QuestionType``。

    平台同一个字段时而字符串、时而数字（``"4"`` / ``4``），所以先统一成 ``str``
    再查表；查不到归 ``UNKNOWN``，原始取值留在 ``raw`` 里等档案补全。
    """
    return QUESTION_TYPE_MAP.get(as_str(platform_type).strip(), QuestionType.UNKNOWN)


# --------------------------------------------------------------------------
# 学习任务列表
# --------------------------------------------------------------------------


def parse_study_tasks(payload: Any) -> list[StudyTask]:
    """解析学习任务分页，**留作业（3）与单元测试 / 测验（2）**。

    列表里混着视频与资料（``type == 1``），不过滤就会把视频当试卷；
    缺 ``ctlId`` 的行没有可作答的 ``testId``，跳过并留日志，免得后面取卷时
    只得到一句「试卷不存在」。

    ⚠️ 以前只认 ``type == 3``，把「单元测试一/二/三」这类 **type=2** 的任务
    整批过滤掉了 —— 用户看到的就是「东软的考试怎么没做」。两种类型走的是同一套
    smartexam 取卷 / 保存 / 交卷链路，只有类型值不同。
    """
    tasks: list[StudyTask] = []
    # 未识别的 type 不再静默丢弃：按类型聚合计数 + 一条样例名，打完一次 warning。
    # 上一次事故就是 type=2 被 `continue` 吃掉，界面看起来像「考试功能没做」。
    skipped: dict[int, list[str]] = {}
    for row in _rows(payload, key="list"):
        task_type = as_int(row.get("type"))
        if task_type not in (ep.TASK_KIND_TEST, ep.TASK_KIND_HOMEWORK):
            skipped.setdefault(task_type, []).append(as_str(row.get("resName")))
            continue
        test_id = as_str(row.get("ctlId"))
        if not test_id:
            log.warning("学习任务缺少 ctlId，已跳过: %s", as_str(row.get("resName")))
            continue
        tasks.append(
            StudyTask(
                test_id=test_id,
                name=as_str(row.get("resName")),
                course_name=as_str(row.get("teachCourseName")),
                study_status=as_int(row.get("studyStatus")),
                open_time=as_str(row.get("openTime")),
                task_type=task_type,
                raw=dict(row),
            )
        )
    if skipped:
        detail = "；".join(
            f"type={kind} ×{len(names)}（例：{names[0][:24]}）"
            for kind, names in sorted(skipped.items())
        )
        log.warning("学习任务里有 %d 行类型未识别，已跳过：%s", 
                    sum(len(v) for v in skipped.values()), detail)
    return tasks


async def list_study_tasks(client: NeusoftClient, term_id: str) -> list[StudyTask]:
    """按学期拉学习任务。

    body **只放学期维度**（``termId`` + 分页）：带 ``currentClassId`` 会 403，
    而且不是「某门课没作业」那种空结果，是整批请求失败。
    """
    if not term_id:
        raise NeusoftError("list_study_tasks 需要 termId：本接口按学期拉，缺学期无法定位任务")

    # 翻页规则见 :mod:`..paging`。以前只取第 1 页（200 条封顶），一学期任务
    # 超过 200 条时后面的作业/测验静默消失，表现为「老师没布置」。
    tasks: list[StudyTask] = []
    seen: set[str] = set()
    raw_collected = 0
    page_size = paging_rules.TASK_PAGE_SIZE

    for page_no in range(1, paging_rules.MAX_PAGES + 1):
        payload = await client.request(
            "POST",
            ep.STUDY_TASK_PAGE,
            body={"pageNo": page_no, "pageSize": page_size, "termId": term_id},
        )
        rows = _rows(payload, key="list")
        raw_collected += len(rows)
        fresh = 0
        for task in parse_study_tasks(payload):
            if task.test_id in seen:
                continue
            seen.add(task.test_id)
            tasks.append(task)
            fresh += 1
        if fresh == 0:
            if page_no > 1:
                log.warning(
                    "学习任务分页第 %d 页没有新任务，提前停止（服务端可能忽略了 pageNo）",
                    page_no,
                )
            break
        if not paging_rules.should_continue(
            collected=raw_collected,
            page_count=len(rows),
            total=paging_rules.total_of(payload),
            page_size=page_size,
        ):
            break
    else:
        log.warning("学习任务超过 %d 页仍未读完，后续任务未拉取", paging_rules.MAX_PAGES)

    return tasks


# --------------------------------------------------------------------------
# 试卷
# --------------------------------------------------------------------------


def parse_paper(payload: Any, *, test_id: str = "") -> Paper:
    """解析试卷结构。

    层级是 ``paperParts[].paperQuestionList[].paperQuestion``（§5.3），拍平后
    保留每个内层原始 dict 到 ``raw``，排查时不用再翻平台响应。
    题干优先 ``stemText``：``stem`` 是富文本，带标签和实体，直接进题库会让
    同一道题换个排版就匹配不上。
    """
    if not isinstance(payload, dict):
        raise NeusoftProtocolError(f"试卷响应不是对象: {type(payload).__name__}")
    paper_fallback_id = as_str(payload.get("id"))
    questions: list[PaperQuestion] = []
    for part in as_list(payload.get("paperParts")):
        if not isinstance(part, dict):
            continue
        for item in as_list(part.get("paperQuestionList")):
            if not isinstance(item, dict):
                continue
            inner = item.get("paperQuestion")
            if not isinstance(inner, dict):
                log.warning("试卷题目缺少 paperQuestion 内层，已跳过: %r", item)
                continue
            question = _paper_question(inner, item)
            # 外层与内层都没给 paperId 时，用响应顶层的 id 兜底（同一份试卷同一个值）。
            if not question.paper_id:
                question.paper_id = paper_fallback_id
            questions.append(question)
    return Paper(
        test_id=as_str(payload.get("testId")) or test_id,
        questions=questions,
        raw=dict(payload),
    )


async def get_paper(client: NeusoftClient, test_id: str) -> Paper:
    """取试卷结构（**不需要先 enter**，§5.3）。"""
    if not test_id:
        raise NeusoftError("get_paper 需要 testId（学习任务里的 ctlId）")
    payload = await client.request("GET", ep.EXAM_PAPER, params={"testId": test_id})
    return parse_paper(payload, test_id=test_id)


def parse_entered(payload: Any, *, test_id: str = "") -> Entered:
    """解析 enter 的返回。

    只取保存/交卷必需的 ``candidateId`` 与 ``historyId``。返回体里同时带完整
    试卷，但那份结构没有二次确认；这里不解析，要题目请单独调 :func:`get_paper`
    —— 解析失败会把「进入作答成功」误报成失败。
    """
    if not isinstance(payload, dict):
        raise NeusoftProtocolError(f"enter 响应不是对象: {type(payload).__name__}")
    return Entered(
        test_id=as_str(payload.get("testId")) or test_id,
        candidate_id=as_str(payload.get("candidateId")),
        history_id=as_str(payload.get("historyId")),
        raw=dict(payload),
    )


async def enter_task(client: NeusoftClient, test_id: str) -> Entered:
    """进入作答（幂等）。

    重试安全：连调多次返回同一个 ``historyId``。「剩余作答次数不足」
    「作答时间已截止」都是业务提示，会以 :class:`NeusoftApiError` 上抛。
    """
    if not test_id:
        raise NeusoftError("enter_task 需要 testId（学习任务里的 ctlId）")
    payload = await client.request("GET", ep.EXAM_ENTER, params={"testId": test_id})
    return parse_entered(payload, test_id=test_id)


# --------------------------------------------------------------------------
# 保存草稿 / 交卷
# --------------------------------------------------------------------------

#: 保存草稿时每条答案的必填字段，顺序与协议档案一致（§5.3）。
_ANSWER_FIELDS: tuple[str, ...] = ("paperId", "questionNo", "questionType", "answer")


async def save_answers(
    client: NeusoftClient,
    entered: Entered,
    answers: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """保存作答草稿（**只存不交**，写路径 ``[B]``，本仓库未实测）。

    为什么逐条严格校验：``answers`` 少一个 ``paperId`` 时服务端可能整批拒收
    或静默丢题，``code`` 却仍是 0；本地先拦住，问题才定位得到。

    返回体里的 ``isSubmitPaper`` 非空表示服务端**顺手交了卷**（§5.3），
    调用方要据此判断，不能假定「保存 = 安全」。
    """
    if not entered.test_id or not entered.candidate_id or not entered.history_id:
        raise NeusoftError("保存草稿缺少 testId/candidateId/historyId：先调 enter_task 拿到")
    body: dict[str, Any] = {
        "candidateId": entered.candidate_id,
        "testId": entered.test_id,
        "historyId": entered.history_id,
        "answers": [_answer_row(item) for item in answers],
    }
    payload = await client.request("POST", ep.EXAM_ANSWER_SAVE, body=body)
    return payload if isinstance(payload, dict) else {}


async def submit_paper(
    client: NeusoftClient,
    history_id: str,
    *,
    confirm: bool = False,
) -> bool:
    """交卷 —— **不可逆**，默认拒绝执行。

    为什么默认拒绝：交卷后不能改答案、作答次数照扣，平台侧没有撤销接口，
    误调一次就毁掉一次机会。所以 ``confirm=False`` 时**一个请求都不发**，
    直接抛 :class:`NeusoftError`；只有显式传 ``confirm=True`` 才落请求。

    写路径 ``[B]``：已有 APK 静态分析证据，但本仓库没有真实样本实测过，
    接入真实账号时应先人工确认交卷时机。成功返回 ``True``（``code == 0``
    仅表示受理，成绩要另行查看）。
    """
    if not confirm:
        raise NeusoftError(
            "submit_paper 不可逆：交卷后不能改答案、作答次数照扣。"
            "确认要交请显式传 confirm=True"
        )
    if not history_id:
        raise NeusoftError("submit_paper 缺少 historyId（来自 enter_task）")
    await client.request("PUT", ep.EXAM_SUBMIT, params={"historyId": history_id})
    return True


# --------------------------------------------------------------------------
# 转 core 模型
# --------------------------------------------------------------------------


def to_core_questions(paper: Paper) -> list[Question]:
    """试卷 → ``core.Question`` 列表，供题库匹配与作答器使用。

    ``remote_id`` 用 :attr:`PaperQuestion.key`（逐题唯一的 ``id``）：
    保存草稿时要靠它把答案匹配回具体题目行。``paperId`` 整份试卷共用一个值，
    拿它当 ``remote_id`` 会让所有题互相同化（曾经就是这样，导致交卷时
    「没有一条答案能映射到平台题目」）。``questionNo`` 与平台原始字段都留在
    ``raw`` 里，回填答案不需要再翻响应。
    题型只做 :func:`core_question_type` 的保守映射，映射不出来就是 ``UNKNOWN``。
    """
    return [
        Question(
            stem=item.stem,
            type=core_question_type(item.question_type),
            options=[choice.content for choice in item.choices],
            remote_id=item.key or None,
            raw=dict(item.raw),
        )
        for item in paper.questions
    ]


# --------------------------------------------------------------------------
# 内部解析
# --------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]*>")
#: 换行类标签要先换成换行符：直接删标签会把两段粘成一段。
_BLOCK_TAG_RE = re.compile(r"(?is)<\s*(?:br|/p|/div|/li|/tr)\s*/?\s*>")


def _plain_text(markup: str) -> str:
    """HTML → 纯文本（去标签、解实体、按行去空白）。

    只用于 ``stem`` 的退化路径：``stemText`` 缺失时才需要自己清洗。
    """
    if not markup:
        return ""
    text = _BLOCK_TAG_RE.sub("\n", markup)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


#: 平台在文本字段里会把制表符/换行**二次转义**成字面量 ``\t`` ``\n``
#: （实测 ``choiceContent`` 是 ``"\\t实用性"``，原样显示就是「\实用性」）。
_ESCAPE_RE = re.compile(r"\\([trn])")
_ESCAPE_MAP = {"t": "\t", "r": "\n", "n": "\n"}


def _clean_text(value: Any) -> str:
    """解掉二次转义、统一空白。

    只处理 ``\\t`` / ``\\n`` / ``\\r`` 三种 —— 题干里的反斜杠（数学式、路径）
    不该被这里当转义吃掉。
    """
    text = as_str(value)
    if not text:
        return ""
    if "\\" in text:
        text = _ESCAPE_RE.sub(lambda m: _ESCAPE_MAP[m.group(1)], text)
    return text.replace("\u3000", " ").strip()


def _paper_question(
    inner: Mapping[str, Any], wrapper: Mapping[str, Any] | None = None
) -> PaperQuestion:
    """解析一道题：**内层 ``paperQuestion`` + 外层行**。

    为什么两层都要读：真实响应里 ``paperId``/``id`` 在外层、``questionNo``/
    ``questionType``/``stemText``/``choices`` 在内层（见 :class:`PaperQuestion`
    的类注释）。只读内层会得到 ``paper_id=""`` —— 保存草稿时整批答案都定位
    不到题目，交卷回一句「没有一条答案能映射到平台题目」。
    """
    outer = wrapper or {}
    # ``paperId`` 原样回传（§5.3）：外层优先，内层是旧假设下的位置，保留兜底。
    paper_id = (
        as_str(outer.get("paperId"))
        or as_str(inner.get("paperId"))
        # 曾经把内层 ``id`` 当 paperId 兜底；真实响应里内层没有 id，
        # 有 id 的是外层（逐题唯一），所以这里退到外层的 questionNo 组合。
        or as_str(inner.get("id"))
    )
    question_id = as_str(outer.get("id")) or as_str(inner.get("id"))
    choices: list[Choice] = []
    for item in as_list(inner.get("choices")):
        if isinstance(item, dict):
            choices.append(
                Choice(
                    choice_id=_clean_text(item.get("choiceId")),
                    content=_clean_text(item.get("choiceContent")),
                )
            )
    return PaperQuestion(
        paper_id=paper_id,
        question_id=question_id,
        question_no=as_str(inner.get("questionNo")) or as_str(outer.get("questionNo")),
        question_type=as_str(inner.get("questionType"))
        or as_str(outer.get("questionType")),
        stem=(
            _clean_text(inner.get("stemText"))
            or _plain_text(as_str(inner.get("stem")))
        ),
        choices=tuple(choices),
        candidate_answer=_answer_values(inner.get("candidateAnswer")),
        standard_answer=_answer_values(inner.get("standardAnswer")),
        candidate_score=as_float(inner.get("candidateScore")),
        question_score=as_float(inner.get("questionScore")),
        raw={**dict(inner), "_outer": dict(outer)},
    )


def _answer_values(value: Any) -> tuple[str, ...]:
    """三态答案（数组 / 逗号串 / null）统一成非空字符串元组。"""
    values: list[str] = []
    for item in as_list(value):
        text = as_str(item)
        if text:
            values.append(text)
    return tuple(values)


def _answer_row(item: Mapping[str, Any]) -> dict[str, str]:
    """校验并规整一条待保存的答案。

    四个字段缺一不可（§5.3）：``paperId``/``questionNo``/``questionType`` 原样
    转字符串，``answer`` 走 :func:`_answer_text` 收敛成逗号串。
    """
    missing = [key for key in _ANSWER_FIELDS if key not in item]
    if missing:
        raise NeusoftProtocolError(f"答案缺字段 {missing}，拒绝发送: {sorted(item)}")
    return {
        "paperId": as_str(item["paperId"]),
        "questionNo": as_str(item["questionNo"]),
        "questionType": as_str(item["questionType"]),
        "answer": _answer_text(item["answer"]),
    }


def _answer_text(value: Any) -> str:
    """把答案值收敛成单个字符串。

    平台接受逗号串（``candidateAnswer`` 就是这个形状，§5.4），所以多选/多空
    按 ``A,B`` 拼；调用方传数组也照同一规则处理，避免各自拼一套。
    """
    if isinstance(value, (list, tuple)):
        return ",".join(as_str(part) for part in value if as_str(part))
    return as_str(value)


def _rows(payload: Any, *, key: str) -> list[dict[str, Any]]:
    """从分页 ``data`` 里取出行，顺手丢掉非对象元素。

    ``data`` 偶尔直接给数组（学期下拉就有这种双形状），两种都收。
    """
    if isinstance(payload, dict):
        raw = payload.get(key)
    elif isinstance(payload, list):
        raw = payload
    else:
        raw = None
    return [item for item in as_list(raw) if isinstance(item, dict)]
