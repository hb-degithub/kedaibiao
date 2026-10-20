"""U校园提交载荷的构造与响应解析（纯函数，可离线测）。

## 依据

- **形状**（键名、嵌套、字符串化 JSON）：``docs/research/unipus-protocol.md`` §8.1；
- **取值语义**（本次实现的主要依据）：``docs/research/uai-core-submit-facts.md``
  §1–§4（对参考实现 ``Yunlk/uai-core@4628e44`` 的只读源码取证）。已确认的三条：

  1. ``isCompleted`` 的元素**恒为 JSON 布尔 ``true``**（源码里只 push ``true``，
     没有任何分支写 ``false``）；长度 = 该组**非学习题**的贡献项数之和：
     学习题 0 项、``role-play`` / ``oral-state`` 恒 1 项、其余
     ``max(child_count, 1)`` 项。
  2. ``thirdPartyJudges`` 是**字符串化的 JSON 对象数组**（元素是对象，不是
     ``null``/``""``），长度与 ``isCompleted`` **严格相等**，元素形状按题型分派
     （见 :func:`build_judge_object`）。
  3. ``submitType`` 是**纯派生值**：该组全部是学习题 → ``2``，否则 ``1``；
     纯学习题组（``submitType=2``，两个数组为空）**也必须提交** —— 它的进度
     （``answer.progress`` / ``children[].isDone``）就是靠这次写请求记录的。

## 答案从哪来

规格 §7.3：答案是**服务端标准答案原样转发**，不是我们自己作答。所以
:func:`build_ques_data` 接收 ``StandardAnswer.children``（每子题的答案字符串
数组），原样填进 ``children[i].value``；我们算出来的答案在这里**用不上**。

## 两处「按源码事实实现、但服务端是否照收仍未实测」的地方（默认保守）

- 口语 / 角色扮演类的**满分 record** 是「客户端自报分数」（facts §2、§9.1-7）：
  是否照搬属产品与合规决策。因此 ``self_report_scores`` 默认 ``False``
  —— 只交完成度、不报分，对应题型不发 ``specific_scores``，``payloads`` /
  ``record`` 用只有 ``url: ""`` 的空记录（长度仍按题型要求给足）。
- 主观题**没有服务端标准答案**：``children[i].value`` 必须是字符串
  （多个值用 ``\\n`` 连接）。默认发空串 ``""``；参考实现用占位文本
  ``[已提交]``，本项目做成显式参数 ``subjective_placeholder``，因为那句
  「不是学生写的」，必须在 UI 上明示。

## 本地校验（服务端不会告诉你哪里错了）

1. ``len(isCompleted) == len(thirdPartyJudges)``（facts §1-5）；
2. 解析不出任何题目项时**明确失败**，绝不发空 ``quesDatas``（规格 U7）；
3. **客观题族**的内容侧 ``child_count`` 与答案侧 ``children`` 数量不一致 →
   该组标**不可提交**并给出原因（规格 §11.2-2；参考实现没做这条校验）。
   非客观题（主观题 / 口语 / 学习题 / 讨论题 / 上传）在答案接口处**本来就可能
   没有标准答案**（``null`` 或空字符串，2026-10-20 真实样本见下），这类题按
   内容侧 ``child_count`` 补齐 children，**不 block**：

   > 2026-10-20 真实样本：讨论题组 ``…/7302fcb43004729`` 的 ``replyType`` 是
   > ``discussion``，答案接口返回 ``{"answer": ""}``（空字符串）。按旧规则会
   > 被误判成「内容侧 1 项，答案侧 0 项」而拦住。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

#: 判分元素的 ``versions`` 默认值（facts §2-3）：参考实现称服务端不强校验这组值。
#: ``course`` 本是「书目版本」，本项目拿不到 → 空串（源码默认也是空串）。
JUDGE_VERSIONS: dict[str, Any] = {
    "course": "",
    "group": 1,
    "template": 1,
    "answer": 3,
    "content": "0",
}

#: 口语 / 角色扮演的满分维度（facts §2-3、§3-4；量纲 0~100）
FULL_MARKS: dict[str, int] = {
    "total": 100,
    "accuracy": 100,
    "integrity": 100,
    "fluency": 100,
}

#: 录音类题型（``record`` 要给「满分 record」的那批）—— facts §3-4
SCORED_TYPES: frozenset[str] = frozenset({"oral", "oral-state", "role-play"})

#: 需要服务端标准答案才有意义的题型（答案接口只对客观题有内容，facts §5.2）。
OBJECTIVE_TYPES: frozenset[str] = frozenset(
    {
        "single-choice",
        "multi-choice",
        "dropdown",
        "fill-blank",
        "banked-cloze",
        "sequence",
        "objective",
    }
)

#: 平台题型名（``parsing.classify_question`` 的产物）；缺 ``unipus_type`` 时的兜底
_KIND_STUDY = "study"
_KIND_OBJECTIVE = "objective"
#: 需要「自拟内容」的题型（服务端不给标准答案）—— 翻译/写作/主观/讨论
SUBJECTIVE_KINDS: frozenset[str] = frozenset(
    {"subjective", "discussion", "translation", "writing", "text-area", "essay"}
)


class SubmitPayloadError(ValueError):
    """本地校验不通过 / 构造不出合法提交体（**不猜、不发**）。"""


def _stringify(value: Any) -> str:
    """紧凑序列化（facts §2-5：无空格、非 ASCII 不转义）。"""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def question_kind(question: Any) -> str:
    """取平台题型名；拿不到时按 ``category == 0`` 兜底判学习题。"""
    kind = str(getattr(question, "unipus_type", "") or "")
    if kind:
        return kind
    try:
        if int(getattr(question, "category", None)) == 0:
            return _KIND_STUDY
    except (TypeError, ValueError):
        pass
    return _KIND_OBJECTIVE


def child_slot_count(question: Any) -> int:
    """``answer.children`` 的长度：**内容侧** ``child_count``，至少 1（facts §3-2）。"""
    try:
        child_count = int(getattr(question, "child_count", 0) or 0)
    except (TypeError, ValueError):
        child_count = 0
    return max(child_count, 1)


def judge_item_count(question: Any) -> int:
    """该题贡献的判分项数（facts §1-2 的长度规则，逐字照做）。"""
    kind = question_kind(question)
    if kind == _KIND_STUDY:
        return 0
    if kind in ("role-play", "oral-state"):
        return 1
    return child_slot_count(question)


def _media_indexes(question: Any) -> tuple[int, ...]:
    """``answer.progress`` 的键：媒体项在 ``contents`` 里的**下标**。

    兼容旧字段（``media_types`` 存的是 type 取值，**不是下标**）：那个字段只在
    拿不到 ``media_indexes`` 时用于「有一项媒体」的粗略兜底；正常路径必须用
    ``media_indexes``（踩过的坑，facts §9.1-5）。
    """
    value = getattr(question, "media_indexes", None)
    if value is None:
        # 老对象没有 media_indexes：宁可少打点也不要用 type 值当键发出去
        return ()
    return tuple(int(index) for index in value)


def _answers_of_child(standard: Any, child_index: int) -> tuple[str, ...]:
    children = tuple(getattr(standard, "children", ()) or ())
    if 0 <= child_index < len(children):
        return tuple(children[child_index])
    return ()


def _all_answers(standard: Any) -> tuple[str, ...]:
    """该题所有子题答案的扁平列表（摘要/判空用）。"""
    out: list[str] = []
    for group in tuple(getattr(standard, "children", ()) or ()):
        out.extend(group)
    return tuple(out)


def _comma_join(values: Sequence[str]) -> str:
    """``thirdPartyJudges[].value``：多值**逗号连接**（facts §2-6、§7.3）。"""
    return ",".join(str(value) for value in values)


def _empty_record() -> dict[str, Any]:
    """保守的 ``record``：只留 url，不自报分数。"""
    return {"url": ""}


def full_record() -> dict[str, Any]:
    """满分 record（facts §3-4）。

    ⚠️ 分数由客户端本地赋值、服务端照存 —— 这是「自报分数」，属产品/合规决策。
    只有 ``self_report_scores=True`` 时才会用到。
    """
    return {
        "url": "",
        "specific_scores": dict(FULL_MARKS),
        "recordDetail": {
            "score": 100,
            "smooth": 100,
            "completed": 100,
            "correctness": 100,
            "audioUrl": "",
            "details": [],
            "relevance": 100,
        },
    }


def build_answer_object(
    *,
    children: Sequence[Sequence[str]],
    media_indexes: Iterable[int] = (),
    reply: bool = False,
    subjective: bool = False,
    self_report_scores: bool = False,
    subjective_placeholder: str = "",
) -> dict[str, Any]:
    """构造 ``answer``（会被字符串化后放进 ``quesDatas[].answer``）。

    逐键依据 facts §3：

    - ``value``：题级恒空数组；
    - ``children``：长度 = ``max(child_count, 1)``（**调用方按内容侧给**），每项
      ``{"value": [...], "isDone": true}``；**主观题例外：``value`` 是字符串**
      （多个值用 ``\\n`` 连接），空作答为空串而不是 ``null``；
    - ``progress``：键是**媒体项在 contents 里的下标**（字符串），值恒 ``true``；
      主观题额外 ``progress.reply = true``；
    - ``record``：录音类给满 record（仅 ``self_report_scores=True``），其余
      ``{"url": ""}``。
    """
    progress: dict[str, Any] = {str(int(index)): True for index in media_indexes}
    if reply:
        progress["reply"] = True

    built_children: list[dict[str, Any]] = []
    for values in children:
        if subjective:
            text = "\n".join(str(value) for value in values)
            if not text and subjective_placeholder:
                text = subjective_placeholder
            built_children.append({"value": text, "isDone": True})
        else:
            built_children.append({"value": [str(value) for value in values], "isDone": True})

    record = full_record() if self_report_scores else _empty_record()
    return {
        "value": [],
        "children": built_children,
        "progress": progress,
        "record": record,
    }


def build_judge_object(
    *,
    question: Any,
    standard: Any = None,
    child_index: int = 0,
    self_report_scores: bool = False,
) -> dict[str, Any]:
    """构造 ``thirdPartyJudges`` 的一个元素（facts §2 的题型分派表）。

    元素字段（``question_type`` / ``reply_type`` 原样转发内容侧的
    ``type`` / ``replyType``；服务端是否逐键校验未实测）：

    ==========================================  ====================================
    题型                                        元素形状
    ==========================================  ====================================
    客观题族（single/multi/dropdown/fill-blank/  ``value`` + ``question_type`` +
    banked-cloze/sequence/兜底 objective）      ``reply_type`` + ``versions`` +
                                                ``payloads: []``
    ``subjective``                              与客观题族完全相同（5 键）
    ``upload``                                  ``question_type`` + ``reply_type``
                                                + ``payloads: []`` + ``versions``
                                                + ``desc: ""``（**没有 value**）
    ``oral``                                    ``value`` + ``payloads``（1 个 record）
                                                + ``versions`` + ``question_type``
                                                + ``reply_type``
    ``oral-state``                              整题 1 项；``value`` 是**映射**
                                                （前 n-1 个子题）+ ``payloads``
                                                （1 个 record）
    ``role-play``                               ``role: ""`` + ``payloads``
                                                （长度 = 子题数）+ ``versions``
                                                + ``question_type`` +
                                                ``reply_type``（**没有 value**）
    ==========================================  ====================================

    ``self_report_scores=True`` 时，录音类额外带 ``specific_scores``，``payloads``
    / ``answer.record`` 用满分 record；默认保守：不带分数、record 只留 url。
    """
    kind = question_kind(question)
    versions = dict(JUDGE_VERSIONS)
    base: dict[str, Any] = {
        "question_type": str(getattr(question, "question_type", "") or ""),
        "reply_type": str(getattr(question, "reply_type", "") or ""),
        "versions": versions,
    }
    record = full_record() if self_report_scores else _empty_record()
    scores = dict(FULL_MARKS) if self_report_scores else None
    slots = child_slot_count(question)

    if kind == "upload":
        return {
            "question_type": base["question_type"],
            "reply_type": base["reply_type"],
            "payloads": [],
            "versions": versions,
            "desc": "",
        }

    if kind == "role-play":
        # 唯一「内外长度故意不一致」的题型：外层只 1 项，payloads 按子题展开
        element = {
            "role": "",
            "payloads": [dict(record) for _ in range(slots)],
            "versions": versions,
            "question_type": base["question_type"],
            "reply_type": base["reply_type"],
        }
        if scores is not None:
            element["specific_scores"] = scores
        return element

    if kind == "oral-state":
        # 前 n-1 个子题按下标收进 value（单值给字符串、多值给数组），最后一个是真作答
        value_map: dict[str, Any] = {}
        for index in range(slots - 1):
            values = _answers_of_child(standard, index)
            if len(values) > 1:
                value_map[str(index)] = [str(item) for item in values]
            elif values:
                value_map[str(index)] = str(values[0])
            else:
                value_map[str(index)] = ""
        element = {
            "value": value_map,
            "payloads": [dict(record)],
            "versions": versions,
            "question_type": base["question_type"],
            "reply_type": base["reply_type"],
        }
        if scores is not None:
            element["specific_scores"] = scores
        return element

    if kind == "oral":
        values = _answers_of_child(standard, child_index)
        element = {
            "value": _comma_join(values),
            "payloads": [dict(record)],
            "versions": versions,
            "question_type": base["question_type"],
            "reply_type": base["reply_type"],
        }
        if scores is not None:
            element["specific_scores"] = scores
        return element

    # 客观题族 + subjective：5 个键完全相同（facts §2 表）
    values = _answers_of_child(standard, child_index)
    return {
        "value": _comma_join(values),
        "question_type": base["question_type"],
        "reply_type": base["reply_type"],
        "versions": versions,
        "payloads": [],
    }


def build_ques_data(
    *,
    questions: Sequence[Any],
    standard_answers: Sequence[Any] | None = None,
    answer_version: int = 1,
    self_report_scores: bool = False,
    subjective_placeholder: str = "",
    answer_overrides: dict[str, list[str]] | None = None,
    blockers: list[str] | None = None,
) -> list[dict[str, Any]]:
    """按题序拼 ``quesDatas``。

    ``questions`` 是 ``parsing.ContentQuestion``（或任何有 ``instance_id`` /
    ``unipus_type`` / ``child_count`` / ``media_indexes`` 的对象）；
    ``standard_answers`` 是同序的 ``StandardAnswer``。

    ``children`` 长度**由内容侧 ``child_count`` 决定**（facts §9.1-3），答案侧
    缺项就填空数组。只有**客观题族**的数量不一致才算问题：``blockers`` 传一个
    列表进来时记进列表（该组标为不可提交），不传则直接抛
    :class:`SubmitPayloadError`；主观题 / 口语 / 学习题（含 ``discussion``）/
    上传类的答案侧为空或不一致**不 block**，按内容侧补齐。
    """
    answers = list(standard_answers or [])
    if answers and len(answers) != len(questions):
        raise SubmitPayloadError(
            f"答案条数（{len(answers)}）与题目条数（{len(questions)}）不一致 —— "
            "外层是按下标对齐的（规格 §7.3），错位提交比不提交更糟，拒绝构造。"
        )

    out: list[dict[str, Any]] = []
    for index, question in enumerate(questions):
        standard = answers[index] if answers else None
        kind = question_kind(question)
        child_count = int(getattr(question, "child_count", 0) or 0)
        slots = max(child_count, 1)

        if standard is not None:
            answer_children = tuple(getattr(standard, "children", ()) or ())
            if len(answer_children) != child_count:
                # 只有**客观题族**才把「答案侧数量对不上」当 blocker：
                # 主观题/口语/学习题（discussion 等）在答案接口处本来就可能
                # 是 null / ""（2026-10-20 真实样本：replyType=discussion 的
                # 题组答案侧 0 项），这类题按内容侧 child_count 补齐即可。
                if question_kind(question) in OBJECTIVE_TYPES:
                    reason = (
                        f"第 {index} 题（instanceId={_instance_id(question)}）子题数不一致："
                        f"内容侧 {child_count} 项，答案侧 {len(answer_children)} 项"
                    )
                    if blockers is None:
                        raise SubmitPayloadError(
                            reason + "；规格 §11.2-2 要求这种组判为不可提交"
                        )
                    blockers.append(reason + "；该组不可提交")

        children = tuple(_answers_of_child(standard, slot) for slot in range(slots))
        # 自拟内容（翻译/写作）：按 instanceId 覆盖逐子题文本。
        # 只在服务端没有标准答案、且调用方给了文本时生效。
        override = (answer_overrides or {}).get(_instance_id(question))
        if override:
            texts = [str(item) for item in override if str(item).strip()]
            if texts and not any(children):
                if kind in SUBJECTIVE_KINDS:
                    children = tuple(
                        [texts[slot]] if slot < len(texts) else [""]
                        for slot in range(slots)
                    )
                else:
                    children = tuple(
                        [texts[slot]] if slot < len(texts) else []
                        for slot in range(slots)
                    )
        answer_obj = build_answer_object(
            children=children,
            media_indexes=_media_indexes(question),
            reply=(kind == "subjective"),
            subjective=(kind == "subjective"),
            self_report_scores=self_report_scores,
            subjective_placeholder=subjective_placeholder,
        )
        out.append(
            {
                "instanceId": _instance_id(question),
                "answer": _stringify(answer_obj),
                "context": _stringify({"state": "submitted"}),
                "contextVersion": 1,
                "answerVersion": answer_version,
            }
        )
    return out


def _instance_id(question: Any) -> str:
    """19 位题目 ID 全程字符串（绝不能过 ``float``，规格 §6.2）。"""
    return str(getattr(question, "instance_id", "") or "")


@dataclass(slots=True)
class SubmitPlan:
    """一次提交要发送的东西（构造完就能给人看，发送与否另说）。"""

    payload: dict[str, Any]
    group_id: str
    submit_type: int
    item_count: int
    question_count: int = 0
    #: 逐题摘要（控制台干跑展示用）
    rows: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    #: 本地校验未通过的原因；非空 = **不可提交**（绝不发请求）
    blockers: list[str] = field(default_factory=list)

    @property
    def submittable(self) -> bool:
        return not self.blockers

    def summary(self) -> str:
        return (
            f"groupId={self.group_id} submitType={self.submit_type} "
            f"题目数={self.question_count} 计分点={self.item_count}"
            f"（{self._verdict()}）"
        )

    def _verdict(self) -> str:
        return "可提交" if self.submittable else f"不可提交：{len(self.blockers)} 处问题"

    def as_dict(self) -> dict[str, Any]:
        """给控制台/干跑摘要用的可序列化视图（**不含完整 payload**）。"""
        return {
            "group_id": self.group_id,
            "course_id": str(self.payload.get("courseId") or ""),
            "submit_type": self.submit_type,
            "item_count": self.item_count,
            "question_count": self.question_count,
            "is_completed_len": len(self.payload.get("isCompleted") or []),
            "third_party_judges_len": len(_parse_judges(self.payload)) or 0,
            "submittable": self.submittable,
            "verdict": self._verdict(),
            "warnings": list(self.warnings),
            "blockers": list(self.blockers),
            "questions": [dict(row) for row in self.rows],
        }


def _parse_judges(payload: dict[str, Any]) -> list[Any]:
    raw = payload.get("thirdPartyJudges")
    if not isinstance(raw, str):
        return list(raw or [])
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:  # pragma: no cover - 构造侧不会发出坏 JSON
        return []
    return list(parsed) if isinstance(parsed, list) else []


def build_submit_plan(
    *,
    course_id: str,
    group_id: str,
    open_id: str,
    questions: Sequence[Any],
    standard_answers: Sequence[Any] | None = None,
    answer_version: int = 1,
    self_report_scores: bool = False,
    subjective_placeholder: str = "",
    answer_overrides: dict[str, list[str]] | None = None,
    is_completed: Sequence[bool] | None = None,
    third_party_judges: Sequence[Any] | None = None,
) -> SubmitPlan:
    """拼出完整提交体（facts §1–§4 + 规格 §8.1）。

    ``is_completed`` / ``third_party_judges`` 默认**按事实生成**（不再是
    ``[None] * n``）；显式传入时只校验长度，方便离线/对照实验。
    """
    if not course_id:
        raise SubmitPayloadError("缺少 courseId（形如 course-v2:…）")
    if not group_id:
        raise SubmitPayloadError("缺少 groupId")
    if not open_id:
        raise SubmitPayloadError("缺少 openId（规格：必须显式传入且不能为空）")
    if not questions:
        # 规格 U7：quesDatas 为空是否被接受**未验证**；参考实现选择明确报错
        raise SubmitPayloadError(
            "没有解析出任何题目项，拒绝构造空 quesDatas（规格 §11.2-3 / U7）。"
            "这通常意味着内容接口返回了空数组或题型解析失败。"
        )

    answers = list(standard_answers or [])
    if answers and len(answers) != len(questions):
        raise SubmitPayloadError(
            f"答案条数（{len(answers)}）与题目条数（{len(questions)}）不一致："
            "外层按下标对齐（规格 §7.3），这种组不可提交。"
        )

    blockers: list[str] = []
    warnings: list[str] = []

    ques_data = build_ques_data(
        questions=questions,
        standard_answers=standard_answers,
        answer_version=answer_version,
        self_report_scores=self_report_scores,
        answer_overrides=answer_overrides,
        subjective_placeholder=subjective_placeholder,
        blockers=blockers,
    )

    rows: list[dict[str, Any]] = []
    generated_judges: list[dict[str, Any]] = []
    item_count = 0
    all_study = True
    has_scored = False
    has_subjective_without_answer = False
    for index, question in enumerate(questions):
        kind = question_kind(question)
        standard = answers[index] if answers else None
        row_warnings: list[str] = []
        contributed = judge_item_count(question)
        item_count += contributed
        all_study = all_study and kind == _KIND_STUDY
        has_scored = has_scored or kind in SCORED_TYPES
        child_count = int(getattr(question, "child_count", 0) or 0)
        slots = max(child_count, 1)

        provided = int(getattr(question, "item_count", 0) or 0)
        if provided and provided != contributed:
            message = (
                f"第 {index} 题贡献项数按事实算得 {contributed}，"
                f"内容解析给的 item_count={provided}（以内容侧 child_count 为准）"
            )
            warnings.append(message)
            row_warnings.append(message)

        if kind in OBJECTIVE_TYPES:
            if standard is None or not _all_answers(standard):
                blockers.append(
                    f"第 {index} 题（{kind}）没有服务端标准答案；"
                    "客观题必须原样转发标准答案（规格 §7.3），该组不可提交"
                )
            else:
                answer_children = tuple(getattr(standard, "children", ()) or ())
                missing = [
                    slot
                    for slot in range(min(slots, len(answer_children)))
                    if not _answers_of_child(standard, slot)
                ]
                if missing:
                    message = (
                        f"第 {index} 题有 {len(missing)} 个子题答案为空"
                        f"（下标 {missing}），这些子题大概率判 0"
                    )
                    warnings.append(message)
                    row_warnings.append(message)
        elif standard is not None:
            # 非客观题（subjective / oral / study（含 discussion）/ upload …）：
            # 答案侧为空或数量不同是**正常现象**，按内容侧 child_count 补齐即可。
            answer_children = tuple(getattr(standard, "children", ()) or ())
            if len(answer_children) != child_count:
                message = (
                    f"第 {index} 题（{kind}）答案侧 {len(answer_children)} 项与内容侧 "
                    f"child_count={child_count} 不一致；非客观题按内容侧补齐"
                    "（空作答/占位，不视为不可提交）"
                )
                warnings.append(message)
                row_warnings.append(message)
        if kind == "subjective" and not subjective_placeholder:
            if standard is None or not _all_answers(standard):
                has_subjective_without_answer = True

        if third_party_judges is None:
            if kind in ("role-play", "oral-state"):
                generated_judges.append(
                    build_judge_object(
                        question=question,
                        standard=standard,
                        child_index=0,
                        self_report_scores=self_report_scores,
                    )
                )
            else:
                for slot in range(contributed):
                    generated_judges.append(
                        build_judge_object(
                            question=question,
                            standard=standard,
                            child_index=slot,
                            self_report_scores=self_report_scores,
                        )
                    )

        rows.append(
            {
                "index": index,
                "instanceId": _instance_id(question),
                "kind": kind,
                "child_count": child_count,
                "item_count": contributed,
                "media_indexes": list(_media_indexes(question)),
                # 给控制台干跑视图用的可读题面（表格里显示「这题是什么」）
                "stem": (getattr(question, "stem", "") or "")[:200],
                "answer": _comma_join(_all_answers(standard) if standard else ()),
                "has_standard_answer": bool(standard and _all_answers(standard)),
                "needs_answer": kind in SUBJECTIVE_KINDS,
                "wants_text": kind in SUBJECTIVE_KINDS,
                "warnings": row_warnings,
            }
        )

    # 纯学习题组 submitType=2（两个数组为空，但**仍要提交**：进度靠它记录）
    submit_type = 2 if all_study else 1
    if item_count == 0 and not all_study:  # pragma: no cover - 防御：非学习题必 >=1 项
        blockers.append("算不出任何计分项，但组里并非全是学习题；拒绝提交")

    if is_completed is None:
        completed = [True] * item_count
    else:
        completed = list(is_completed)
        if len(completed) != item_count:
            raise SubmitPayloadError(
                f"isCompleted 长度 {len(completed)} 与计分点数 {item_count} 不一致"
            )
        if any(value is not True for value in completed):
            warnings.append("isCompleted 里出现了非 true 的元素（参考实现从不发 false）")

    if third_party_judges is not None:
        judges = list(third_party_judges)
        if len(judges) != len(completed):
            raise SubmitPayloadError(
                "thirdPartyJudges 长度必须与 isCompleted 严格相等"
                f"（{len(judges)} != {len(completed)}）"
            )
    else:
        judges = generated_judges

    if len(judges) != len(completed):  # pragma: no cover - 上面按同一计数生成
        raise SubmitPayloadError(
            "thirdPartyJudges 长度必须与 isCompleted 严格相等"
            f"（{len(judges)} != {len(completed)}）"
        )

    if has_scored and not self_report_scores:
        warnings.append(
            "组内有口语/角色扮演类题目：默认不自报分数（self_report_scores=False），"
            "这些题只能拿完成度、拿不到分数；需要时显式打开（合规风险自担）"
        )
    if has_subjective_without_answer and not answer_overrides and not subjective_placeholder:
        warnings.append(
            "组内有主观题且没有服务端标准答案：children[].value 发空串，"
            "完成度可能为 0；可开「大模型作答」或显式传 subjective_placeholder"
            "（参考实现用 [已提交]，但那不是学生写的，必须对外明示）"
        )
    if blockers:
        warnings.append("本地校验未通过（见 blockers），真实提交会被拒绝")

    payload = {
        "courseId": course_id,
        "openId": open_id,
        "version": "default",
        "quesDatas": ques_data,
        "groupId": group_id,
        "isCompleted": completed,
        "thirdPartyJudges": _stringify(judges),
        "submitType": submit_type,
        "associationGroupId": "",
        "associationUnitId": "",
    }
    return SubmitPlan(
        payload=payload,
        group_id=group_id,
        submit_type=submit_type,
        item_count=item_count,
        question_count=len(questions),
        rows=rows,
        warnings=warnings,
        blockers=blockers,
    )


# --------------------------------------------------------------------------
# 响应解析
# --------------------------------------------------------------------------


@dataclass(slots=True)
class SubmitResult:
    accepted: bool
    state: int | None = None
    score: list[Any] = field(default_factory=list)
    score_pct: float | None = None
    version: str = ""
    raw: dict[str, Any] = field(default_factory=dict)
    #: 响应里的 ``code`` 原值与原类型 —— 三套成功码的真实 JSON 类型未实测
    #: （facts §7「仍未确认」），记下来供一次真实响应后固化。
    code: Any = None
    code_type: str = ""

    def per_child_verdicts(self) -> list[bool | None]:
        """把有序得分列表翻成「每项是否得分」。

        规格 §8.2：``score`` 是有序得分列表；非判分题为空列表。
        粒度（按子题还是按题）未确认（U6），所以这里只做「0 分 / 非 0 分」的
        粗判定，且**不据此回填题库** —— 除非调用方明确知道粒度。
        """
        out: list[bool | None] = []
        for value in self.score:
            try:
                out.append(float(value) > 0)
            except (TypeError, ValueError):
                out.append(None)
        return out

    def summary(self) -> str:
        if not self.accepted:
            return (
                f"服务端未接受（code={self.code!r}，类型 {self.code_type or '缺失'}）；"
                "提交成功要求整数 code == 0（facts §7）"
            )
        pct = f"{self.score_pct:.0%}" if self.score_pct is not None else "—"
        return f"服务端已接受；得分率 {pct}；得分项 {self.score}"


def parse_submit_result(document: dict[str, Any]) -> SubmitResult:
    """解析提交响应（规格 §8.2 + facts §7）。

    成功判定：**优先整数比较** ``code == 0``；``code`` 是字符串时退回
    ``"0"`` 比较（参考实现只认整数，但源码调研建议先整数再字符串，并把实际
    类型记进日志，等一次真实响应后固化 —— facts §9.1-10）。
    """
    if not isinstance(document, dict):
        raise SubmitPayloadError(f"提交响应期望对象，实际 {type(document).__name__}")

    code = document.get("code")
    code_type = type(code).__name__
    if isinstance(code, bool):
        accepted = False
    elif isinstance(code, int):
        accepted = code == 0
    elif isinstance(code, str):
        accepted = code.strip() == "0"
    else:
        accepted = False

    data = document.get("data") or {}
    data = data if isinstance(data, dict) else {}
    state = data.get("state") or {}
    state = state if isinstance(state, dict) else {}
    raw_state = state.get("state")
    try:
        state_value = int(raw_state)
    except (TypeError, ValueError):
        state_value = None

    score = state.get("score")
    score_list = list(score) if isinstance(score, list) else []

    pct = state.get("score_pct")
    try:
        score_pct = float(pct) if pct is not None else None
    except (TypeError, ValueError):
        score_pct = None

    return SubmitResult(
        accepted=accepted,
        state=state_value,
        score=score_list,
        score_pct=score_pct,
        version=str(data.get("version") or ""),
        raw=document,
        code=code,
        code_type=code_type,
    )
