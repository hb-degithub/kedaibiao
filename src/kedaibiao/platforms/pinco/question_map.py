"""题型翻译 + 「需人工」判定。

两件事必须放在平台包里：

1. 平台题型名 → 核心 :class:`QuestionType`（平台概念只在这一层出现）；
2. 识别「程序读不到题面 / 答案要传图」的题 —— 这类题**永远不生成答案**。

图片判定刻意**不依赖共享区**：核心 ``core/models.py`` 由另一个会话维护，本包必须
能在核心没有 ``QuestionType.IMAGE`` 时照常工作。做法是把判定结果与**理由码**写进
``Question.raw`` 里本包自己的标记位（:data:`IMAGE_FLAG` / :data:`REASONS_FLAG`），
后续所有判断都以那两个标记为准；核心枚举里若有 ``IMAGE`` 就顺带写上。

实测形态（Lead 真实会话只读抓取）：题干是 HTML，真图是
``<img class="rich-img" …>``，公式是 ``<img class="rich-formula" …>``。

**三类理由码分开，互不吞并**（用户要能一眼分清「哪几道必须我自己做」和
「哪几道只是要我看图」）：

- :data:`REASON_ANSWER_REQUIRES_IMAGE`：题干要求上传/拍照/作图，或题型/标签说
  要作图上传 —— **用户必须亲自做**，这正是用户原话「图片输入题」的字面含义；
- :data:`REASON_STEM_HAS_IMAGE`：题干含真图片（rich-img），题面信息在图上；
- :data:`REASON_STEM_HAS_FORMULA_IMAGE`：题干含公式图（rich-formula）。

⚠️ **不要把判定收窄成只认 ``rich-img``**（也不许为它加「允许自动答公式图」的
开关）：当前答题器是 ``BankAnswerer → RuleAnswerer``，**没有多模态**，收窄之后那
道公式题照样答不出来；所以收窄的收益是 0，代价却是把「信息不足」伪装成「有信息」，
让文本答题器在没看到题面的情况下硬答 —— 那就成了猜。启发式答案本来就被提交守卫
拒绝（``test_heuristic_answer_is_refused_even_with_high_confidence``），收窄不会多
答出任何一道题。等真接入多模态答题器时再一起做。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from kedaibiao.core.models import Question, QuestionType
from kedaibiao.platforms.pinco import endpoints as ep
from kedaibiao.platforms.pinco import richtext

#: 核心题型枚举里的「图片题」；核心若还没这个成员就退回 ``UNKNOWN``。
IMAGE_TYPE: QuestionType = getattr(QuestionType, "IMAGE", QuestionType.UNKNOWN)

#: 本包写在 ``Question.raw`` 里的图片题标记键。
#: 带 ``pinco_`` 前缀，避免与核心或别的平台包的标记位撞名。
IMAGE_FLAG = "pinco_requires_image"
#: 需人工的**理由码列表**与主理由文本也存进 raw：计划里要写清楚为什么叫人，
#: 不能只给一个布尔位。
REASONS_FLAG = "pinco_needs_human_reasons"
REASON_FLAG = "pinco_needs_human_reason"

# -- 三类理由码（稳定字符串，计划产物与测试都靠它，不要改字面量）-----------

#: 答案必须由人上传图片/附件（拍照、作图、手写）。**用户必须亲自做**。
REASON_ANSWER_REQUIRES_IMAGE = "answer_requires_image"
#: 题干含真图片（``rich-img``），题面信息可能在图上。
REASON_STEM_HAS_IMAGE = "stem_has_image"
#: 题干含公式图（``rich-formula``），文本模型读不到。
REASON_STEM_HAS_FORMULA_IMAGE = "stem_has_formula_image"

#: 理由码 → 给复核的人看的解释（写「为什么」，不是复述「做了什么」）。
REASON_TEXTS: dict[str, str] = {
    REASON_ANSWER_REQUIRES_IMAGE: (
        "这道题要你亲自上传图片/拍照/作图作答，程序不能代做，请人工处理"
    ),
    REASON_STEM_HAS_IMAGE: "题干含真图片，题面信息可能在图上，程序读不到，请人工处理",
    REASON_STEM_HAS_FORMULA_IMAGE: "题干含公式图片，文本模型读不到，请人工处理",
}

#: 理由码 → 计划里的短标签（分三类列的时候用）。
REASON_LABELS: dict[str, str] = {
    REASON_ANSWER_REQUIRES_IMAGE: "必须你亲自做（上传/拍照/作图）",
    REASON_STEM_HAS_IMAGE: "需要看图（题干含真图片）",
    REASON_STEM_HAS_FORMULA_IMAGE: "需要看图（题干含公式图片）",
}

#: 优先级：答案侧排在题干侧前面 —— 那是用户必须亲自动手的那一类，
#: 一道题同时命中多条时，主理由报它。
REASON_PRIORITY: tuple[str, ...] = (
    REASON_ANSWER_REQUIRES_IMAGE,
    REASON_STEM_HAS_IMAGE,
    REASON_STEM_HAS_FORMULA_IMAGE,
)

#: 兜底提示语（调用方没给理由码时用）。
IMAGE_REASON = REASON_TEXTS[REASON_ANSWER_REQUIRES_IMAGE]

#: 题干文案里出现这些词，说明**答案**要人上传/画/拍（不是「题干里有图」）。
#: 刻意不含光秃秃的「图片」「照片」：像「下列哪项是图片格式」这种题干会被误判成
#: 要上传图片，把能自动作答的题也拖进人工。
_ANSWER_IMAGE_HINTS = (
    "上传",
    "拍照",
    "拍摄",
    "作图",
    "画图",
    "画出",
    "绘画",
    "手写",
    "手绘",
    "截图",
    "附上",
    "附件",
)

#: 平台题型名 → 核心题型。比对前先做 :func:`normalize_type_name`。
_TYPE_ALIASES: dict[str, QuestionType] = {
    # 单选
    "single": QuestionType.SINGLE,
    "singlechoice": QuestionType.SINGLE,
    "radio": QuestionType.SINGLE,
    "choice": QuestionType.SINGLE,
    "单选": QuestionType.SINGLE,
    "单选题": QuestionType.SINGLE,
    # 多选
    "multiple": QuestionType.MULTIPLE,
    "multichoice": QuestionType.MULTIPLE,
    "checkbox": QuestionType.MULTIPLE,
    "multi": QuestionType.MULTIPLE,
    "多选": QuestionType.MULTIPLE,
    "多选题": QuestionType.MULTIPLE,
    # 判断
    "judge": QuestionType.JUDGE,
    "truefalse": QuestionType.JUDGE,
    "bool": QuestionType.JUDGE,
    "boolean": QuestionType.JUDGE,
    "判断": QuestionType.JUDGE,
    "判断题": QuestionType.JUDGE,
    # 填空
    "fill": QuestionType.FILL,
    "blank": QuestionType.FILL,
    "fillblank": QuestionType.FILL,
    "填空": QuestionType.FILL,
    "填空题": QuestionType.FILL,
    # 简答 / 论述
    "essay": QuestionType.ESSAY,
    "shortanswer": QuestionType.ESSAY,
    "subjective": QuestionType.ESSAY,
    "简答": QuestionType.ESSAY,
    "论述": QuestionType.ESSAY,
    "问答": QuestionType.ESSAY,
}

#: 会被判定为「图片作答题」的题型名（TODO(endpoint): 待样本确认后再增删）。
_IMAGE_TYPE_NAMES = frozenset(
    {
        "image",
        "img",
        "picture",
        "photo",
        "upload",
        "uploadimage",
        "handwrite",
        "handwriting",
        "draw",
        "drawing",
        "图片",
        "图片题",
        "拍照",
        "上传",
        "上传图片",
        "手写",
        "作图",
    }
)

#: 可能承载「这题要图片」语义的字段名。
#: TODO(endpoint): 样本到手后收窄。多认一个键只是多叫一次人工，
#: 少认一个键就可能拿文字答案去糊弄图片题 —— 所以宁可多认。
_IMAGE_FIELD_KEYS = (
    "requires_image",
    "requiresImage",
    "image_required",
    "imageRequired",
    "need_image",
    "needImage",
    "need_upload",
    "needUpload",
    "upload_image",
    "uploadImage",
    "photo_answer",
    "photoAnswer",
)

#: 「哪些题型字段」可能携带题型名。
_TYPE_FIELD_KEYS = (
    "type",
    "questionType",
    "question_type",
    "answerType",
    "answer_type",
    "qtype",
    "category",
)

#: 会被 :func:`_as_flag` 认作 True 的字面量。
_TRUE_LITERALS = frozenset({"1", "true", "yes", "on", "y", "image", "是", "需要"})


def normalize_type_name(raw: Any) -> str:
    """题型名归一化：取字符串、转小写、去掉空格/下划线/连字符。"""
    if raw is None:
        return ""
    text = raw if isinstance(raw, str) else str(raw)
    for ch in (" ", "\t", "_", "-", "\u3000"):
        text = text.replace(ch, "")
    return text.strip().lower()


#: 归一化后的图片题型名集合（先算一次，避免每次判定都重建集合）。
_NORMALIZED_IMAGE_NAMES = frozenset(normalize_type_name(name) for name in _IMAGE_TYPE_NAMES)


def _as_flag(value: Any) -> bool:
    """把平台原始字段解释成布尔。认不出来一律 False。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in _TRUE_LITERALS
    return False


def is_image_type_name(raw: Any) -> bool:
    return normalize_type_name(raw) in _NORMALIZED_IMAGE_NAMES


def question_type_name(item: dict[str, Any]) -> Any:
    """从平台原始题目载荷里取出题型名（可能为空）。

    单独放一个函数是为了让解析层不必知道候选字段名 —— 字段一览表只在本模块
    出现一次，样本到手后收窄也只改一处。
    """
    for key in _TYPE_FIELD_KEYS:
        if item.get(key) is not None:
            return item[key]
    return None


def map_question_type(raw: Any) -> QuestionType:
    """平台题型名 → 核心题型。

    **认不出来就 UNKNOWN，不根据选项数量猜。** 猜错题型的后果不是「少答
    一题」，而是让启发式答题器自信地按错误题型作答，这条路必须堵死。
    """
    name = normalize_type_name(raw)
    if not name:
        return QuestionType.UNKNOWN
    if name in _NORMALIZED_IMAGE_NAMES:
        return IMAGE_TYPE
    return _TYPE_ALIASES.get(name, QuestionType.UNKNOWN)


def _has_attachment(value: Any) -> bool:
    """附件槽里有没有东西（空列表 / None / 空串都算没有）。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (list, dict, str)):
        return bool(value)
    return value is not None


@dataclass(slots=True, frozen=True)
class HumanReason:
    """一条「必须人工」的理由：稳定的码 + 给人看的解释 + 计划里的短标签。"""

    code: str
    text: str
    label: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "text": self.text, "label": self.label}


def _reason(code: str) -> HumanReason:
    return HumanReason(code=code, text=REASON_TEXTS[code], label=REASON_LABELS[code])


def _answer_requires_image(
    item: dict[str, Any], stem_text: str, *, has_options: bool = False
) -> bool:
    """这道题是不是「要人亲自上传/拍照/作图」。

    判据按证据强度：

    1. **题干文案**要求上传/拍照/作图/手写 —— 最直接的证据
       （实测样例：「请上传你的实验接线照片，并说明测量步骤」）；
    2. 题型名或 ``labels`` 标记为作图/上传；
    3. 平台明确的「要图片」布尔位；
    4. 答案槽已经有附件（**仅非选择题**）。

    ⚠️ 第 4 条只能兜底：``answer.attachments`` 非空也可能是学生自己已经传过答案，
    所以它排在前三条之后，而且**选择题一律不看附件槽**（那里的附件通常是题干配图）。
    只看 attachments 就判定「这题要传图」，是这份判定的头号误判来源。
    """
    if any(hint in stem_text for hint in _ANSWER_IMAGE_HINTS):
        return True
    for key in _TYPE_FIELD_KEYS:
        if key in item and is_image_type_name(item.get(key)):
            return True
    labels = item.get(ep.FIELD_LABELS)
    if isinstance(labels, list) and any(is_image_type_name(label) for label in labels):
        return True
    for key in _IMAGE_FIELD_KEYS:
        if key in item and _as_flag(item.get(key)):
            return True
    # 「选择题」既可能由调用方告诉我们（解析时已抽好选项），也可能能从原始载荷看出来。
    # 选择题一律不再看附件槽：那里的附件通常是题干配图或学生随手传的东西。
    is_choice = has_options or bool(item.get(ep.FIELD_OPTION_ITEMS))
    if is_choice:
        return False
    answer = item.get(ep.FIELD_ANSWER_BLOCK)
    if isinstance(answer, dict) and _has_attachment(answer.get(ep.FIELD_ATTACHMENTS)):
        return True
    return _has_attachment(item.get(ep.FIELD_ATTACHMENTS))


def human_reasons(
    item: dict[str, Any], *, has_options: bool = False, stem_text: str = ""
) -> list[HumanReason]:
    """这道题有哪些「必须人工」的理由，按 :data:`REASON_PRIORITY` 排序。

    **三类互不吞并**：题干里同时有真图和公式图、或题干有图而答案又要人做，
    就把命中的码都带回来；主理由（列表第一项）按优先级取答案侧。
    返回空列表表示可以自动作答。
    """
    raw_html = item.get(ep.FIELD_CONTENT)
    text = stem_text or (richtext.strip_html(raw_html) if isinstance(raw_html, str) else "")
    codes: list[str] = []

    if _answer_requires_image(item, text, has_options=has_options):
        codes.append(REASON_ANSWER_REQUIRES_IMAGE)

    html = item.get(ep.FIELD_CONTENT)
    if isinstance(html, str):
        kind = richtext.image_kind(html)
        if kind == "image":
            codes.append(REASON_STEM_HAS_IMAGE)
        elif kind == "formula":
            codes.append(REASON_STEM_HAS_FORMULA_IMAGE)

    ordered = [code for code in REASON_PRIORITY if code in codes]
    return [_reason(code) for code in ordered]


def image_required(item: dict[str, Any], *, has_options: bool = False) -> bool:
    """``human_reasons`` 的布尔版（调用方只关心「要不要人工」时用它）。"""
    return bool(human_reasons(item, has_options=has_options))


def mark_image(
    question: Question, *, reasons: list[HumanReason] | None = None
) -> Question:
    """给需人工的题盖上本包标记位与理由码，并把题型写成核心的 IMAGE（若核心有）。

    理由存进 ``raw`` 而不是只留一个布尔位：计划与通知里要写清楚**为什么**叫人，
    而且要能分清「必须你自己做」与「只是要看图」。

    不传 ``reasons`` 时按最保守的一类兜底（「答案要你亲自做」）—— 方向宁可多叫人。
    """
    resolved = reasons or [_reason(REASON_ANSWER_REQUIRES_IMAGE)]
    question.raw[IMAGE_FLAG] = True
    question.raw[REASONS_FLAG] = [item.code for item in resolved]
    question.raw[REASON_FLAG] = resolved[0].text
    if IMAGE_TYPE is not QuestionType.UNKNOWN:
        question.type = IMAGE_TYPE
    return question


def is_image_question(question: Question) -> bool:
    """这题是否需要人工（图片/公式/附件作答）。

    三个来源取或，任一命中即算：本包标记位、核心题型 IMAGE、核心若提供的
    ``needs_image`` 属性。
    """
    if _as_flag(question.raw.get(IMAGE_FLAG)):
        return True
    if IMAGE_TYPE is not QuestionType.UNKNOWN and question.type is IMAGE_TYPE:
        return True
    needs = getattr(question, "needs_image", None)
    return needs if isinstance(needs, bool) else False


def reason_codes(question: Question) -> list[str]:
    """这道题命中的理由码（按优先级）。

    老标记（只有布尔位、没有码）按最保守的一类兜底：``answer_requires_image``。
    """
    if not is_image_question(question):
        return []
    stored = question.raw.get(REASONS_FLAG)
    if isinstance(stored, list):
        codes = [code for code in stored if isinstance(code, str) and code in REASON_TEXTS]
        if codes:
            return [code for code in REASON_PRIORITY if code in codes]
    return [REASON_ANSWER_REQUIRES_IMAGE]


def reason_objects(question: Question) -> list[HumanReason]:
    """这道题命中的理由对象（码 + 解释 + 标签），按优先级。"""
    return [_reason(code) for code in reason_codes(question)]


def human_reason(question: Question) -> str:
    """这题为什么必须人工（主理由文本）；返回空串表示可以自动作答。"""
    if not is_image_question(question):
        return ""
    stored = question.raw.get(REASON_FLAG)
    if isinstance(stored, str) and stored:
        return stored
    codes = reason_codes(question)
    return REASON_TEXTS[codes[0]] if codes else IMAGE_REASON
