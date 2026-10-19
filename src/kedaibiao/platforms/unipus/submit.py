"""U校园提交载荷的构造与响应解析（纯函数，可离线测）。

## 依据

规格 §8.1（`docs/research/unipus-protocol.md`）给了完整的**形状**：
键名、嵌套结构、每个字段的形状与来源行号。

## 但有两处**取值语义未确认**

1. `isCompleted`：规格只说「长度 = 各题贡献项数之和」，**没说值怎么定**。
2. `thirdPartyJudges`：规格只说「字符串化的 JSON 数组，长度必须与 isCompleted
   严格相等」，**没说元素内容**。

这两个字段是提交能否被平台接受的关键，而**猜错了不会报错、只会静默判错**——
交错的答案比不交更糟。所以：

- 本模块把它们做成**显式参数**，默认给最保守的值；
- 适配器侧用 ``submit_payload_confirmed=False`` 闸门挡住真实发送；
- 需要用户提供**一次真实提交请求**来确认这两个字段，确认后把闸门打开即可。

## 答案从哪来

规格 §7.3：答案是**服务端标准答案原样转发**，不是我们自己作答。所以
``build_ques_data`` 接收 ``StandardAnswer.children``（每子题的答案字符串数组），
原样填进 ``children[i].value``。我们自己算出来的答案在这里**用不上**。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence


class SubmitPayloadError(ValueError):
    pass


@dataclass(slots=True)
class SubmitPlan:
    """一次提交要发送的东西（构造完就能给人看，发送与否另说）。"""

    payload: dict[str, Any]
    group_id: str
    submit_type: int
    item_count: int
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"groupId={self.group_id} submitType={self.submit_type} "
            f"题目数={len(self.payload.get('quesDatas', []))} 计分点={self.item_count}"
        )


def _stringify(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def build_answer_object(
    *,
    children: Sequence[Sequence[str]],
    media_indexes: Iterable[int] = (),
    reply: bool = False,
) -> dict[str, Any]:
    """构造 ``answer``（会被字符串化后放进 ``quesDatas[].answer``）。

    ``progress`` 的键是**媒体项在 contents 里的下标**（字符串），值恒 true；
    主观题额外加 ``progress.reply = true``（规格 §8.1）。
    """
    progress: dict[str, Any] = {str(i): True for i in media_indexes}
    if reply:
        progress["reply"] = True
    return {
        "value": [],
        "children": [{"value": list(child), "isDone": True} for child in children],
        "progress": progress,
        "record": {"url": ""},
    }


def build_ques_data(
    *,
    questions: Sequence[Any],
    standard_answers: Sequence[Any] | None = None,
    answer_version: int = 1,
) -> list[dict[str, Any]]:
    """按题序拼 ``quesDatas``。

    ``questions`` 是 ``parsing.ContentQuestion``（或任何有 ``instance_id`` /
    ``media_types`` 的对象）；``standard_answers`` 是同序的 ``StandardAnswer``。
    """
    answers = list(standard_answers or [])
    if answers and len(answers) != len(questions):
        raise SubmitPayloadError(
            f"答案条数（{len(answers)}）与题目条数（{len(questions)}）不一致 —— "
            f"规格第 11.2 节明确要求实现者自己做这项校验，不一致就不该提交。"
        )

    out: list[dict[str, Any]] = []
    for index, question in enumerate(questions):
        standard = answers[index] if answers else None
        children = list(getattr(standard, "children", ()) or ())
        if not children:
            # 没有标准答案就交一个空的子题数组：结构仍然合法，但大概率判错。
            # 调用方应当在提交前用 plan.warnings 拦住这种情况。
            children = [()] * max(1, int(getattr(question, "child_count", 1) or 1))
        answer_obj = build_answer_object(
            children=children,
            media_indexes=tuple(getattr(question, "media_types", ()) or ()),
        )
        out.append({
            "instanceId": str(getattr(question, "instance_id", "")),
            "answer": _stringify(answer_obj),
            "context": _stringify({"state": "submitted"}),
            "contextVersion": 1,
            "answerVersion": answer_version,
        })
    return out


def build_submit_plan(
    *,
    course_id: str,
    group_id: str,
    open_id: str,
    questions: Sequence[Any],
    standard_answers: Sequence[Any] | None = None,
    is_completed: Sequence[bool] | None = None,
    third_party_judges: Sequence[Any] | None = None,
    answer_version: int = 1,
) -> SubmitPlan:
    """拼出完整提交体。

    ``is_completed`` 与 ``third_party_judges`` 的取值语义**未确认**（见模块
    docstring），默认给：全 ``True`` / 全 ``None``，长度按「各题贡献项数之和」。
    """
    if not course_id:
        raise SubmitPayloadError("缺少 courseId")
    if not group_id:
        raise SubmitPayloadError("缺少 groupId")
    if not open_id:
        raise SubmitPayloadError("缺少 openId（规格：必须显式传入且不能为空）")
    if not questions:
        raise SubmitPayloadError("没有题目，不提交")

    ques_data = build_ques_data(
        questions=questions, standard_answers=standard_answers,
        answer_version=answer_version,
    )

    item_count = sum(int(getattr(q, "item_count", 0) or 0) for q in questions)
    if item_count <= 0:
        raise SubmitPayloadError("算不出计分点数（item_count 全为 0），拒绝构造提交体")

    if is_completed is None:
        completed = [True] * item_count
    else:
        completed = list(is_completed)
        if len(completed) != item_count:
            raise SubmitPayloadError(
                f"isCompleted 长度 {len(completed)} 与计分点数 {item_count} 不一致"
            )

    if third_party_judges is None:
        judges: list[Any] = [None] * item_count
    else:
        judges = list(third_party_judges)
        if len(judges) != len(completed):
            raise SubmitPayloadError(
                "thirdPartyJudges 长度必须与 isCompleted 严格相等"
                f"（{len(judges)} != {len(completed)}）"
            )

    # 纯学习题组 submitType=2（规格 §8.1），否则 1
    all_learning = all(_is_learning(q) for q in questions)
    submit_type = 2 if all_learning else 1

    warnings: list[str] = []
    if standard_answers is None:
        warnings.append("没有取到标准答案，子题为空数组 —— 大概率判错，不建议提交")
    elif any(not getattr(a, "children", ()) for a in standard_answers):
        warnings.append("部分题目没有标准答案，对应子题为空数组")
    if not all_learning:
        warnings.append(
            "isCompleted / thirdPartyJudges 的取值语义未经真实抓包确认"
            "（规格只给了长度要求）；真实发送被 submit_payload_confirmed 闸门挡住"
        )

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
        payload=payload, group_id=group_id, submit_type=submit_type,
        item_count=item_count, warnings=warnings,
    )


def _is_learning(question: Any) -> bool:
    """``category == 0`` 是学习题（规格 §7.1）。拿不到就当不是。"""
    try:
        return int(getattr(question, "category", None)) == 0
    except (TypeError, ValueError):
        return False


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
            return "服务端未接受（code != 0）"
        pct = f"{self.score_pct:.0%}" if self.score_pct is not None else "—"
        return f"服务端已接受；得分率 {pct}；得分项 {self.score}"


def parse_submit_result(document: dict[str, Any]) -> SubmitResult:
    """解析提交响应（规格 §8.2）。"""
    data = document.get("data") or {}
    state = data.get("state") or {}
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

    accepted = str(document.get("code")) == "0"
    return SubmitResult(
        accepted=accepted,
        state=state_value,
        score=score_list,
        score_pct=score_pct,
        version=str(data.get("version") or ""),
        raw=document,
    )
