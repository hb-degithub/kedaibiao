"""学习通「考试 / 作业 / 章节测验」页面解析 + 填卷/提交 JS 生成（纯函数）。

## 这个模块做什么

把真实抓到的答题页 / 考试列表页 HTML 解析成题目模型，并按页面**自己的**
交互方式生成可 ``eval`` 的 JS 字符串：

- :func:`parse_work` —— 答题页 → :class:`Work`（题面、题型、选项、图片、可作答性）；
- :func:`parse_exam_list` —— 考试列表页 → :class:`ExamItem`；
- :func:`fill_script` / :func:`verify_script` / :func:`submit_script` —— 填、核验、提交。

本模块不联网、不启浏览器，也不碰 Playwright：真正驱动页面由 runner 用
:mod:`kedaibiao.browser.pw` 执行这些 JS。纯函数的好处是**全部可离线回归**。

## 真实 DOM（2026-10-21 对 ``data/chaoxing-probe/`` 抓包逐行核对）

答题页（``work_do_home.html``）：

```html
<div class="singleQuesId" id="question210547688" data="210547688">
  <div class="TiMu newTiMu">
    <div class="Zy_TItle clearfix">
      <i class="fl">1</i>
      <div class="clearfix font-cxsecret fontLabel">
        <span class="newZy_TItle">【单选题】</span>设<img src="...">厙示庲个事件…
      </div>
    </div>
    <ul class="Zy_ulTop w-top fl">
      <li class="font-cxsecret before-after" onclick="addChoice(this);" qid="210547688" qtype="0">
        <label class="fl before"><span class="num_option choice210547688" data="A">A</span></label>
        <a class="fl after"> … 选项正文（可含 &lt;img&gt;） … </a>
      </li>
    </ul>
    <input type="hidden" name="answer210547688" id="answer210547688" value="">
    <input type="hidden" id="answertype210547688" name="answertype210547688" value="0">
  </div>
</div>
```

- 单选 / 判断题点 ``addChoice(li)``，把字母写进 ``#answer<qid>``；
- 多选题点 ``addMultipleChoice(li)``（选择态 class 是 ``check_answer_dx``）；
- 提交按钮是 **``a.btnSubmit``**（``onclick="btnBlueSubmit();"``），点了之后页面先
  AJAX 校验，再弹 **``#workpop``** 确认层（``#popok`` = 提交、``#popno`` = 取消），
  确认后走 ``submitCheckTimes()``；``#submitBack`` 是「未达到及格线，请重做」的提示层，
  ``#verifyCodeWin`` 是提交次数超限后的图形验证码层。
- 题面汉字的字体混淆（``font-cxsecret``）交给 :mod:`.fontdec`，本模块不重复实现。

考试列表页（``examlist_*.html``）：``div.bottomList ul li``，行内
``onclick="goTest('课程id',关系id,考试id,'截止时间',…)"`` 或
``viewExamAnswer('关系id','考试id')``（已核对：``tId=关系id``、``id=考试id``），
状态在 ``p.status``，手机限制是 ``div.span_tip[title="只允许手机APP考试"]``。

## 边界

- 只自动作答**单选 / 多选 / 判断 / 简单填空（可见 input）**；简答、连线、排序、
  完型、阅读、程序题、富文本填空等一律在 :attr:`Question.unsupported` 里写明原因，
  绝不假装能做 —— 调用方按这个字段跳过并交人工；
- 题面里的行内公式是 ``<img>``，解析时用 ``【图】`` 占位并单独给出图片 URL 列表，
  方便上层把图片喂给多模态模型；
- 填空答案用 ``|`` 分隔多个空（与 :func:`fill_script` 的约定一致）；
- 生成的选择器全部基于 ``id=question<qid>`` / ``#answer<qid>`` / ``qid`` 属性精确定位，
  并在 JS 内断言「每个字母只命中 1 个选项」，不靠 class 顺序猜。
"""

from __future__ import annotations

import html as html_lib
import io
import json
import logging
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

from kedaibiao.platforms.chaoxing import fontdec

log = logging.getLogger(__name__)

#: 题面里行内公式 / 图片的占位符
IMAGE_MARK = "【图】"

#: 学习通 ``qtype`` → 题型名。0/1/2/3/11/12/13/14/15/16/17/19/20/22/24/26 来自答题页
#: 自带的 ``toadd()`` 校验脚本逐条核对；5~8 是历史上常见的简答类，仅用于展示/兜底。
QTYPE_NAMES: dict[int, str] = {
    0: "单选题",
    1: "多选题",
    2: "填空题",
    3: "判断题",
    4: "简答题",
    5: "名词解释",
    6: "论述题",
    7: "计算题",
    8: "其他题",
    9: "分录题",
    10: "资料题",
    11: "连线题",
    12: "投票题",
    13: "排序题",
    14: "完型填空",
    15: "阅读理解",
    16: "综合题",
    17: "程序题",
    18: "简答题",
    19: "听力题",
    20: "共用选项题",
    22: "口语测评题",
    24: "选词填空",
    26: "写作题",
}

#: 可以用「点字母」作答的题型
CHOICE_QTYPES = frozenset({0, 1, 3})
#: 填空（可见 input）
FILL_QTYPE = 2

#: 判断题「对」的常见写法
_JUDGE_TRUE_TEXTS = ("对", "正确", "是", "√", "T", "TRUE")
#: 判断题「错」的常见写法
_JUDGE_FALSE_TEXTS = ("错", "错误", "否", "×", "X", "F", "FALSE")

#: 状态词 → 规范状态。顺序敏感：先匹配到的优先。
_STATUS_WORDS: tuple[tuple[str, str], ...] = (
    ("待做", "todo"),
    ("未做", "todo"),
    ("未完成", "todo"),
    ("未交", "todo"),
    ("进行中", "todo"),
    ("已完成", "done"),
    ("已交", "done"),
    ("已提交", "done"),
    ("已过期", "expired"),
    ("已结束", "expired"),
    ("已截止", "expired"),
    ("已关闭", "expired"),
)

#: 已知动作 → 规范动作名
_KNOWN_ACTIONS = frozenset({"goTest", "viewExamAnswer"})

_QID_ID_RE = re.compile(r"^question(\d+)$")
_CALL_RE = re.compile(r"^\s*([A-Za-z_$][\w$]*)\s*\((.*)\)\s*;?\s*$", re.S)
_DIGITS_RE = re.compile(r"\d+")
_WS_RE = re.compile(r"[\s\u00a0]+")
_LETTER_RE = re.compile(r"[A-Z]")
#: ``div.time`` 里算「截止时间」的文本特征（排除「重考」「100分」这类）
_END_TIME_HINT_RE = re.compile(r"(剩余|已过期|截止|\d{4}-\d{1,2}-\d{1,2})")
#: goTest 第 4 个参数是截止时间
_DATE_HINT_RE = re.compile(r"\d{4}-\d{1,2}-\d{1,2}|\d{1,2}:\d{2}")


# ---------------------------------------------------------------------------
# 迷你 DOM：标准库 HTMLParser 建树，只为了「按块限定作用域」取文本/属性，
# 不用正则切 div（真实页面里 div 嵌套很深，正则切不干净）。
# ---------------------------------------------------------------------------


class _Node:
    """极简 DOM 节点：标签、属性、子节点（``_Node`` 或文本片段）。"""

    __slots__ = ("tag", "attrs", "children")

    def __init__(self, tag: str, attrs: dict[str, str]) -> None:
        self.tag = tag
        self.attrs = attrs
        self.children: list[_Node | str] = []

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<_Node {self.tag} {self.attrs.get('class', '')!r}>"


#: HTML 空元素：不进栈
_VOID_TAGS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)


class _TreeBuilder(HTMLParser):
    """把 HTML 建成 :class:`_Node` 树；容错：标签不闭合/乱闭合都不抛异常。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("#document", {})
        self._stack: list[_Node] = [self.root]
        self._skip = 0  # script/style 里的文本不要

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = _Node(tag, {key: (value or "") for key, value in attrs})
        self._stack[-1].children.append(node)
        if tag in _VOID_TAGS:
            return
        if tag in ("script", "style"):
            self._skip += 1
        self._stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._stack[-1].children.append(_Node(tag, {key: (value or "") for key, value in attrs}))

    def handle_endtag(self, tag: str) -> None:
        if tag in _VOID_TAGS:
            return
        for index in range(len(self._stack) - 1, 0, -1):
            if self._stack[index].tag == tag:
                if tag in ("script", "style") and self._skip:
                    self._skip -= 1
                del self._stack[index:]
                return

    def handle_data(self, data: str) -> None:
        if self._skip or not data:
            return
        self._stack[-1].children.append(data)


def _parse_html(html: str) -> _Node:
    builder = _TreeBuilder()
    builder.feed(html or "")
    builder.close()
    return builder.root


def _walk(node: _Node):
    """深度优先遍历所有 ``_Node`` 后代（不含自身）。"""
    for child in node.children:
        if isinstance(child, _Node):
            yield child
            yield from _walk(child)


def _classes(node: _Node) -> list[str]:
    return node.attrs.get("class", "").split()


def _has_class(node: _Node, name: str) -> bool:
    return name in _classes(node)


def _iter_nodes(root: _Node, *, tag: str | None = None, cls: str | None = None):
    for node in _walk(root):
        if tag is not None and node.tag != tag:
            continue
        if cls is not None and not _has_class(node, cls):
            continue
        yield node


def _first_node(root: _Node, *, tag: str | None = None, cls: str | None = None) -> _Node | None:
    return next(_iter_nodes(root, tag=tag, cls=cls), None)


def _by_id(root: _Node, node_id: str) -> _Node | None:
    for node in _walk(root):
        if node.attrs.get("id", "") == node_id:
            return node
    return None


def _image_url(node: _Node) -> str:
    """取图片的真实地址：优先懒加载的 ``data-original``，跳过 data URI 占位图。"""
    for key in ("data-original", "data-src", "src"):
        url = (node.attrs.get(key) or "").strip()
        if url and not url.startswith("data:"):
            return url
    return ""


def _node_text(node: _Node | None, *, exclude: tuple[_Node, ...] = ()) -> str:
    """节点纯文本（不含图片占位、不做解码）。"""
    if node is None:
        return ""
    parts: list[str] = []

    def walk(current: _Node | str) -> None:
        if isinstance(current, str):
            parts.append(current)
            return
        if current in exclude or current.tag in ("script", "style"):
            return
        if current.tag == "img":
            return
        if current.tag in ("br", "p", "div", "li", "tr", "td", "th", "h1", "h2", "h3"):
            parts.append(" ")
        for child in current.children:
            walk(child)

    walk(node)
    return _WS_RE.sub(" ", html_lib.unescape("".join(parts))).strip()


def _content_text(
    node: _Node | None,
    *,
    images: list[str] | None = None,
    exclude: tuple[_Node, ...] = (),
) -> str:
    """节点内容文本：``<img>`` 换成 :data:`IMAGE_MARK`，图片地址按出现顺序收进 ``images``。"""
    if node is None:
        return ""
    parts: list[str] = []

    def walk(current: _Node | str) -> None:
        if isinstance(current, str):
            parts.append(current)
            return
        if current in exclude or current.tag in ("script", "style"):
            return
        if current.tag == "img":
            url = _image_url(current)
            if images is not None and url:
                images.append(url)
                parts.append(IMAGE_MARK)
            elif url:
                parts.append(IMAGE_MARK)
            return
        if current.tag in ("br", "p", "div", "li", "tr", "td", "th", "h1", "h2", "h3"):
            parts.append(" ")
        for child in current.children:
            walk(child)

    walk(node)
    return _WS_RE.sub(" ", html_lib.unescape("".join(parts))).strip()


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ExamItem:
    """考试列表页的一行（考试 / 单元测验 / 课后作业共用这套列表）。"""

    title: str
    status: str = "unknown"  # todo | done | expired | unknown
    exam_id: str = ""
    relation_id: str = ""
    end_time_text: str = ""
    mobile_only: bool = False
    action: str = "unknown"  # goTest | viewExamAnswer | unknown
    raw_onclick: str = ""
    #: 已完成条目列表里回显的分数（「重考 100 分」里的 100）
    score: float | None = None
    score_text: str = ""


@dataclass(slots=True)
class Option:
    """一个选项。``letter`` 是页面给的字母（A/B/C…），``text`` 已解码。"""

    letter: str
    text: str = ""
    images: list[str] = field(default_factory=list)
    selected_hint: bool = False


@dataclass(slots=True)
class Question:
    """一道题。``unsupported`` 为空串表示可以自动作答。"""

    qid: str
    number: int
    qtype: int
    type_name: str
    text: str = ""
    images: list[str] = field(default_factory=list)
    options: list[Option] = field(default_factory=list)
    fill_inputs: int = 0
    unsupported: str = ""

    def option_text(self, letter: str) -> str:
        """按字母取选项正文；没有就返回空串。"""
        for option in self.options:
            if option.letter == letter.upper():
                return option.text
        return ""


@dataclass(slots=True)
class Work:
    """一份答题页解析结果。"""

    title: str = ""
    questions: list[Question] = field(default_factory=list)
    #: 是否成功套用了字体混淆解码（页面没有混淆字体时为 False，文本按原样）
    deobfuscated: bool = False
    #: 混淆字体里有、但没解码出来的伪字（调用方应据此降低答案置信度）
    font_unmatched: tuple[str, ...] = ()

    def by_type(self) -> dict[str, list[Question]]:
        """按题型名分组（``{"单选题": [...], "多选题": [...]}``）。"""
        grouped: dict[str, list[Question]] = {}
        for question in self.questions:
            grouped.setdefault(question.type_name or f"题型{question.qtype}", []).append(question)
        return grouped


# ---------------------------------------------------------------------------
# 答题页解析
# ---------------------------------------------------------------------------


def parse_work(html: str) -> Work:
    """解析答题页 HTML → :class:`Work`。空 HTML / 残缺 HTML 返回空 Work，不抛异常。"""
    source = html or ""
    if not source.strip():
        return Work()
    decoder = _decoder(source)
    raw_texts: list[str] = []

    def decode(value: str) -> str:
        if not value:
            return ""
        raw_texts.append(value)
        return decoder.decode(value) if decoder else value

    root = _parse_html(source)
    blocks = [
        node
        for node in _walk(root)
        if node.tag == "div" and _QID_ID_RE.match(node.attrs.get("id", ""))
    ]
    if not blocks:
        blocks = [node for node in _walk(root) if _has_class(node, "singleQuesId")]
    if not blocks:
        blocks = [node for node in _walk(root) if _has_class(node, "TiMu")]

    questions = [
        _build_question(root, block, index + 1, decode)
        for index, block in enumerate(blocks)
    ]
    title = _work_title(root, decode)
    return Work(
        title=title,
        questions=questions,
        deobfuscated=bool(decoder and decoder.mapping),
        font_unmatched=_font_unmatched(source, raw_texts, decoder),
    )


def _decoder(source: str) -> fontdec.Deobfuscator | None:
    """建字体解码器；字体/参考字体不可用时如实返回 ``None``（文本保持原样）。"""
    try:
        return fontdec.Deobfuscator.from_html(source)
    except fontdec.FontDecodeError as exc:
        log.warning("字体混淆解码不可用，题面按原样返回：%s", exc)
        return None
    except Exception as exc:  # noqa: BLE001 - 解码是可选增强，不能拖垮解析
        log.warning("字体混淆解码异常，题面按原样返回：%s", exc)
        return None


def _font_unmatched(
    source: str,
    raw_texts: list[str],
    decoder: fontdec.Deobfuscator | None,
) -> tuple[str, ...]:
    """找出「混淆字体里有这个码位、但映射里没有」的伪字。"""
    if not decoder or not decoder.mapping:
        return ()
    font_chars = _font_codepoints(source)
    if not font_chars:
        return ()
    used = {char for text in raw_texts for char in text}
    return tuple(sorted((used & font_chars) - set(decoder.mapping)))


def _font_codepoints(source: str) -> set[str]:
    """内联混淆字体的 cmap 码位（只用于识别漏解的伪字，不参与解码）。"""
    font_bytes = fontdec.extract_font(source)
    if not font_bytes:
        return set()
    try:
        from fontTools.ttLib import TTFont  # noqa: PLC0415 - 可选依赖，与 fontdec 一致

        font = TTFont(io.BytesIO(font_bytes), lazy=True)
        return {chr(codepoint) for codepoint in (font.getBestCmap() or {})}
    except Exception:  # noqa: BLE001 - 认不出就当作没有漏字
        log.debug("读取混淆字体 cmap 失败", exc_info=True)
        return set()


def _work_title(root: _Node, decode) -> str:
    node = _first_node(root, tag="div", cls="TestTitle_name")
    title = decode(_node_text(node))
    if title:
        return title
    node = _first_node(root, tag="title")
    return decode(_node_text(node))


def _build_question(root: _Node, block: _Node, fallback_number: int, decode) -> Question:
    match = _QID_ID_RE.match(block.attrs.get("id", ""))
    qid = match.group(1) if match else (
        block.attrs.get("data", "").strip() or block.attrs.get("qid", "").strip()
    )

    title_node = _first_node(block, tag="div", cls="Zy_TItle") or _first_node(block, cls="Zy_TItle")
    type_node = _first_node(title_node, cls="newZy_TItle") if title_node else None
    type_name = _clean_type_name(decode(_node_text(type_node)))

    number_node = _first_node(title_node, tag="i", cls="fl") if title_node else None
    number_text = _node_text(number_node)
    number_match = _DIGITS_RE.search(number_text)
    number = int(number_match.group()) if number_match else fallback_number

    label_node = (
        _first_node(title_node, tag="div", cls="fontLabel") if title_node else None
    ) or title_node
    text_images: list[str] = []
    text = decode(
        _content_text(label_node, images=text_images, exclude=(type_node,) if type_node else ())
    )
    if not text:
        text_images = []
        text = decode(_content_text(title_node, images=text_images))

    options = _build_options(block, decode)
    fill_inputs, rich_editor = _fill_inputs(block)
    qtype = _resolve_qtype(root, block, qid, type_name)
    if type_name in ("", "未知") and qtype in QTYPE_NAMES:
        type_name = QTYPE_NAMES[qtype]
    if not type_name:
        type_name = QTYPE_NAMES.get(qtype, f"题型{qtype}" if qtype >= 0 else "未知题型")

    return Question(
        qid=qid,
        number=number,
        qtype=qtype,
        type_name=type_name,
        text=text,
        images=text_images,
        options=options,
        fill_inputs=fill_inputs,
        unsupported=_unsupported_reason(qtype, type_name, options, fill_inputs, rich_editor),
    )


def _clean_type_name(value: str) -> str:
    """``【单选题】`` → ``单选题``。"""
    return re.sub(r"[\s【】\[\]（）()]+", "", value or "")


def _resolve_qtype(root: _Node, block: _Node, qid: str, type_name: str) -> int:
    """qtype 解析顺序：选项 li 的 ``qtype`` → 隐藏 input ``answertype<qid>`` → 题型名反查。"""
    for node in _walk(block):
        if node.tag == "li" and _DIGITS_RE.fullmatch(node.attrs.get("qtype", "").strip()):
            return int(node.attrs["qtype"])
    if qid:
        hidden = _by_id(root, f"answertype{qid}")
        if hidden is not None and _DIGITS_RE.fullmatch(hidden.attrs.get("value", "").strip()):
            return int(hidden.attrs["value"])
    for qtype, name in QTYPE_NAMES.items():
        if name == type_name:
            return qtype
    return -1


def _option_list(block: _Node) -> _Node | None:
    """找选项列表：优先 ``ul.Zy_ulTop``，退而求其次找「含选项 li」的 ul/ol。"""
    lists = [
        node
        for node in _walk(block)
        if node.tag in ("ul", "ol") and _has_class(node, "Zy_ulTop")
    ]
    if lists:
        return lists[0]
    for node in _walk(block):
        if node.tag not in ("ul", "ol"):
            continue
        for child in _walk(node):
            if child.tag != "li":
                continue
            if child.attrs.get("qid") or child.attrs.get("qtype") or _is_choice_li(child):
                return node
    return None


def _is_choice_li(node: _Node) -> bool:
    onclick = node.attrs.get("onclick", "")
    return "addchoice" in onclick.lower() or "addmultiplechoice" in onclick.lower()


def _build_options(block: _Node, decode) -> list[Option]:
    holder = _option_list(block)
    if holder is None:
        return []
    options: list[Option] = []
    for index, li in enumerate(node for node in _walk(holder) if node.tag == "li"):
        letter_node = _first_node(li, cls="num_option") or _first_node(li, cls="num_option_dx")
        letter = ""
        if letter_node is not None:
            letter = (letter_node.attrs.get("data") or _node_text(letter_node)).strip().upper()
            if not _LETTER_RE.fullmatch(letter):
                found = _LETTER_RE.search(letter)
                letter = found.group() if found else ""
        if not letter:
            letter = chr(ord("A") + index) if index < 26 else ""

        content_node = _first_node(li, cls="after")
        if content_node is None:
            content_node = _first_node(li, tag="a")
        if content_node is None:
            content_node = _first_node(li, tag="label", cls="before")
        label_node = _first_node(li, tag="label", cls="before")
        exclude = (label_node,) if label_node is not None and content_node is not label_node else ()
        images: list[str] = []
        text = decode(_content_text(content_node, images=images, exclude=exclude))

        selected = any(
            _has_class(node, "check_answer") or _has_class(node, "check_answer_dx")
            for node in _walk(li)
        ) or li.attrs.get("aria-checked", "").strip().lower() == "true"
        options.append(
            Option(letter=letter, text=text, images=images, selected_hint=selected)
        )
    return options


def _fill_inputs(block: _Node) -> tuple[int, bool]:
    """返回 (可见输入框数量, 是否富文本编辑器)。

    隐藏域（``answer<qid>`` / ``answertype<qid>``）、单选/复选控件都不算「填空输入框」；
    出现 ``textarea`` / ``div.InpDIV`` / ``#answerEditor<qid>`` 说明是 UEditor 富文本，
    自动填写不可靠。
    """
    count = 0
    rich = False
    for node in _walk(block):
        if node.tag == "textarea":
            count += 1
            rich = True
        elif node.tag == "input":
            input_type = (node.attrs.get("type") or "text").strip().lower()
            if input_type in ("hidden", "checkbox", "radio", "submit", "button", "file", "image"):
                continue
            count += 1
        elif (node.tag == "div" and _has_class(node, "InpDIV")) or (
            node.attrs.get("id", "").startswith("answerEditor")
        ):
            rich = True
    return count, rich


def _unsupported_reason(
    qtype: int,
    type_name: str,
    options: list[Option],
    fill_inputs: int,
    rich_editor: bool,
) -> str:
    if qtype in CHOICE_QTYPES:
        if not options:
            return "页面里没有解析到选项"
        return ""
    if qtype == FILL_QTYPE:
        if rich_editor:
            return "填空题使用富文本编辑器（textarea/UEditor），暂不支持自动填写"
        if fill_inputs <= 0:
            return "填空题没有找到可见输入框"
        return ""
    if qtype < 0:
        return "无法识别题型（缺 qtype 与题型名）"
    return f"暂不支持自动作答：{type_name or f'题型{qtype}'}"


# ---------------------------------------------------------------------------
# 考试列表解析
# ---------------------------------------------------------------------------


def parse_exam_list(html: str) -> list[ExamItem]:
    """解析考试 / 测验列表页 → :class:`ExamItem` 列表；空页或「暂无作业」返回空列表。"""
    source = html or ""
    if not source.strip():
        return []
    root = _parse_html(source)
    rows: list[_Node] = []
    for container in _iter_nodes(root, tag="div", cls="bottomList"):
        rows.extend(node for node in _walk(container) if node.tag == "li")
    if not rows:
        rows = [
            node
            for node in _walk(root)
            if node.tag == "li"
            and _first_node(node, tag="p", cls="status") is not None
            and _first_node(node, tag="p", cls="overHidden2") is not None
        ]
    items = [_exam_item(row) for row in rows]
    return [item for item in items if item is not None]


def _parse_call(raw: str) -> tuple[str, list[str]]:
    match = _CALL_RE.match(raw or "")
    if not match:
        return "", []
    return match.group(1), _split_js_args(match.group(2))


def _split_js_args(text: str) -> list[str]:
    """按顶层逗号切 JS 实参（引号内的逗号不算），返回值去掉字符串引号。"""
    args: list[str] = []
    buffer: list[str] = []
    quote = ""
    depth = 0
    for char in text:
        if quote:
            if char == quote:
                quote = ""
            else:
                buffer.append(char)
            continue
        if char in ("'", '"'):
            quote = char
            continue
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth = max(0, depth - 1)
        if char == "," and depth == 0:
            args.append("".join(buffer).strip())
            buffer = []
            continue
        buffer.append(char)
    tail = "".join(buffer).strip()
    if tail or args:
        args.append(tail)
    return args


def _status_of(text: str) -> str:
    compact = (text or "").replace(" ", "")
    for word, status in _STATUS_WORDS:
        if word in compact:
            return status
    return "unknown"


def _exam_item(li: _Node) -> ExamItem | None:
    raw_onclick = ""
    for node in _walk(li):
        onclick = node.attrs.get("onclick", "").strip()
        if onclick:
            raw_onclick = onclick
            break
    status_text = _node_text(_first_node(li, tag="p", cls="status"))
    title = _node_text(_first_node(li, tag="p", cls="overHidden2"))
    if not raw_onclick and not status_text:
        return None

    action_name, args = _parse_call(raw_onclick)
    action = action_name if action_name in _KNOWN_ACTIONS else "unknown"

    exam_id = ""
    relation_id = ""
    end_time_text = ""
    # 实测（2026-10-21）：examnotes?examId=<args[1]> 能打开该场考试；
    # args[2] 是作答记录 id（须知页 startBtn 的 tId=<args[1]>&id=<args[2]>）。
    # viewExamAnswer(<examId>,<answerId>) 同理。
    if action == "goTest":
        exam_id = args[1] if len(args) > 1 else ""
        relation_id = args[2] if len(args) > 2 else ""
        end_time_text = args[3] if len(args) > 3 else ""
    elif action == "viewExamAnswer":
        exam_id = args[0] if args else ""
        relation_id = args[1] if len(args) > 1 else ""
    if not end_time_text:
        end_time_text = _end_time_text(li)

    status = _status_of(status_text)
    if status == "unknown":
        if action == "goTest":
            status = "todo"
        elif action == "viewExamAnswer":
            status = "done"

    score, score_text = _exam_score(li)
    return ExamItem(
        title=title,
        status=status,
        exam_id=exam_id.strip(),
        relation_id=relation_id.strip(),
        end_time_text=end_time_text.strip(),
        mobile_only=_is_mobile_only(li),
        action=action,
        raw_onclick=raw_onclick,
        score=score,
        score_text=score_text,
    )


_EXAM_SCORE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*分(?!钟)")


def _exam_score(li: _Node) -> tuple[float | None, str]:
    """从考试行里取回显分数。

    已完成行会写成「（重考）100 分  剩余19小时…」；「满分」字样在列表里没有，
    所以只认「数字 + 分」。取不到就返回 ``(None, "")``，不猜。
    """
    text = _node_text(li)
    match = _EXAM_SCORE_RE.search(text)
    if not match:
        return None, ""
    try:
        return float(match.group(1)), match.group(0).strip()
    except ValueError:  # pragma: no cover - 正则已限制数字
        return None, ""


def _end_time_text(li: _Node) -> str:
    """行内截止时间文本。

    ``div.time`` 里可能塞着「重考」按钮或逆袭分数，不能见 time 就取；只认带
    「剩余 / 已过期 / 截止 / 日期」的文本。已完成的行如果没有显式时间，就看行内
    是否还挂着「重考」的 ``goTest(...)``，从它的第 4 个参数拿截止时间。
    """
    for node in _walk(li):
        if node.tag == "div" and _has_class(node, "time"):
            text = _node_text(node)
            match = _END_TIME_HINT_RE.search(text)
            if match:
                # 同一个 div 里可能先有「100分」再有「剩余19小时28分钟」，从特征词切起
                return text[match.start() :].strip()
    for node in _walk(li):
        name, args = _parse_call(node.attrs.get("onclick", ""))
        if name == "goTest" and len(args) > 3 and _DATE_HINT_RE.search(args[3]):
            return args[3]
    return ""


def _is_mobile_only(li: _Node) -> bool:
    text = _node_text(li).replace(" ", "")
    if "只允许手机" in text or "手机APP" in text:
        return True
    for node in _walk(li):
        title = (node.attrs.get("title") or "").replace(" ", "")
        if "只允许手机" in title or "手机APP" in title:
            return True
    return False


# ---------------------------------------------------------------------------
# 答案归一化
# ---------------------------------------------------------------------------


def _compact(text: str) -> str:
    return _WS_RE.sub("", text or "").strip("。．.，,；;、：:")


def _judge_aliases(question: Question) -> dict[str, str]:
    """判断题答案别名 → 选项字母（只对判断题有内容）。

    学习通有的版式把判断题答案存成 ``true`` / ``false``（阅读理解的分支里能看到
    ``itemAnswer == "true" ? true : itemAnswer``），有的存字母。这里两种都认，并且
    以**选项文字**判断「对」到底对应 A 还是 B —— 不假设 A 一定是对。
    """
    if question.qtype != 3:
        return {}
    true_letter = ""
    false_letter = ""
    for option in question.options:
        key = _compact(option.text).upper()
        if key in _JUDGE_TRUE_TEXTS and not true_letter:
            true_letter = option.letter
        elif key in _JUDGE_FALSE_TEXTS and not false_letter:
            false_letter = option.letter
    # 选项写成「对/错」以外的样子时，按最常见约定兜底
    true_letter = true_letter or "A"
    false_letter = false_letter or "B"
    aliases: dict[str, str] = {}
    for token in _JUDGE_TRUE_TEXTS:
        aliases[token] = true_letter
    for token in _JUDGE_FALSE_TEXTS:
        aliases[token] = false_letter
    return aliases


def _answer_letters(question: Question, answer: str) -> str:
    """把答案归一成「排序去重的大写字母串」，如 ``"b,a"`` / ``"BA"`` → ``"AB"``。

    支持：字母（``A``）、字母串（``ACD`` / ``A,C,D``）、选项原文、判断题的「对/错」。
    """
    raw = (answer or "").strip()
    if not raw:
        raise ValueError(f"第 {question.number} 题（{question.qid}）的答案为空")
    for option in question.options:
        if option.text and _compact(option.text) == _compact(raw):
            return option.letter
    # 判断题先认「对/错/true/false」这类写法：只有字母的答案（如 "true"）走别的分支会误判
    letters = _judge_aliases(question).get(_compact(raw).upper(), "")
    if not letters:
        letters = "".join(sorted({char.upper() for char in re.findall(r"[A-Za-z]", raw)}))
    if not letters:
        raise ValueError(
            f"第 {question.number} 题（{question.qid}）的答案 {answer!r} 不是字母，"
            "也不是任何选项的原文"
        )
    available = {option.letter for option in question.options}
    unknown = sorted(set(letters) - available)
    if unknown:
        raise ValueError(
            f"第 {question.number} 题（{question.qid}）没有选项 {''.join(unknown)}，"
            f"现有选项为 {''.join(sorted(available)) or '无'}"
        )
    if question.qtype != 1 and len(letters) > 1:
        raise ValueError(
            f"第 {question.number} 题（{question.qid}）是{question.type_name}，"
            f"答案 {letters} 多于一个选项"
        )
    return letters


def _fill_values(answer: str) -> list[str]:
    values = [part.strip() for part in (answer or "").split("|")]
    if not any(values):
        raise ValueError("填空题的答案为空")
    return values


# ---------------------------------------------------------------------------
# JS 生成
# ---------------------------------------------------------------------------


def _js_literal(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _js_string_list(values: list[str]) -> str:
    return json.dumps(values, ensure_ascii=False)


def _js_aliases(question: Question) -> str:
    """判断题答案别名（``true`` / 「对」 → 选项字母）；其它题型给空对象。"""
    return json.dumps(_judge_aliases(question), ensure_ascii=False)


def _require_answerable(question: Question) -> None:
    if question.unsupported:
        raise ValueError(
            f"第 {question.number} 题（{question.qid}）无法自动作答：{question.unsupported}"
        )
    if not question.qid:
        raise ValueError(f"第 {question.number} 题缺少 qid，无法精确定位")


#: 精确定位题块（真实页面 id=question<qid>）
_JS_BLOCK = (
    '  var qid = {qid};\n'
    '  var block = document.getElementById("question" + qid);\n'
    '  if (!block) {{ throw new Error("找不到题块 #question" + qid); }}\n'
)


def fill_script(question: Question, answer: str) -> str:
    """生成「填这一题」的 JS（IIFE，``eval`` 后返回 JSON 可序列化的对象）。

    - 单选 / 判断：按字母点选（页面有 ``addChoice`` 就调它，否则对 ``li`` 派发 click）；
    - 多选：按字母集合逐个点选，多退少补（``addMultipleChoice`` 优先）；
    - 填空：给可见 input/textarea 赋值并派发 ``input`` / ``change``。

    选择器按 ``#question<qid>`` + 选项字母 ``data`` 属性定位，并在 JS 里断言
    「每个字母只命中 1 个选项」，命中 0 个或多个直接抛错，绝不按顺序猜。
    无法作答的题（见 :attr:`Question.unsupported`）抛 :class:`ValueError`。
    """
    _require_answerable(question)
    if question.qtype == FILL_QTYPE:
        return _fill_script_fill(question, answer)
    return _fill_script_choice(question, answer)


def _fill_script_choice(question: Question, answer: str) -> str:
    letters = _answer_letters(question, answer)
    wanted = list(letters)
    multi = question.qtype == 1
    qid = _js_literal(question.qid)
    head = _JS_BLOCK.format(qid=qid)
    return (
        "(function () {\n"
        + head
        + '  var wanted = ' + _js_string_list(wanted) + ";\n"
        + "  var multi = " + ("true" if multi else "false") + ";\n"
        + "  var aliases = " + _js_aliases(question) + ";\n"
        + "  var all = block.getElementsByTagName('li');\n"
        + "  var marks = [];\n"
        + "  for (var i = 0; i < all.length; i++) {\n"
        + "    var mark = all[i].querySelector('.num_option, .num_option_dx');\n"
        + "    if (!mark) { continue; }\n"
        + "    var letter = String(mark.getAttribute('data') || mark.textContent || '')\n"
        + "      .replace(/\\s+/g, '').toUpperCase();\n"
        + "    marks.push({ li: all[i], letter: letter });\n"
        + "  }\n"
        + "  if (!marks.length) { throw new Error('第 "
        + str(question.number)
        + " 题 #question' + qid + ' 没找到选项'); }\n"
        + "  for (var j = 0; j < wanted.length; j++) {\n"
        + "    var hits = [];\n"
        + "    for (var k = 0; k < marks.length; k++) {\n"
        + "      if (marks[k].letter === wanted[j]) { hits.push(marks[k]); }\n"
        + "    }\n"
        + "    if (hits.length !== 1) {\n"
        + "      throw new Error('选项 ' + wanted[j] + ' 命中 ' + hits.length + ' 个，拒绝操作');\n"
        + "    }\n"
        + "  }\n"
        + "  function isOn(li) {\n"
        + "    if (li.querySelector('.check_answer, .check_answer_dx')) { return true; }\n"
        + "    if (String(li.getAttribute('aria-checked') || '').toLowerCase() === 'true') { "
        + "return true; }\n"
        + "    var input = li.querySelector('input[type=radio], input[type=checkbox]');\n"
        + "    return !!(input && input.checked);\n"
        + "  }\n"
        + "  function fire(li) {\n"
        + "    if (multi && typeof window.addMultipleChoice === 'function') { "
        + "window.addMultipleChoice(li); return; }\n"
        + "    if (typeof window.addChoice === 'function') { window.addChoice(li); return; }\n"
        + "    li.click();\n"
        + "  }\n"
        + "  var clicked = [];\n"
        + "  for (var m = 0; m < marks.length; m++) {\n"
        + "    var item = marks[m];\n"
        + "    var want = wanted.indexOf(item.letter) >= 0;\n"
        + "    if (want === isOn(item.li)) { continue; }\n"
        + "    fire(item.li);\n"
        + "    clicked.push(item.letter);\n"
        + "  }\n"
        + "  var actual = '';\n"
        + "  var hidden = document.getElementById('answer' + qid);\n"
        + "  if (hidden && typeof hidden.value === 'string') { actual = hidden.value; }\n"
        + "  var alias = aliases[actual.replace(/\\s+/g, '').toUpperCase()];\n"
        + "  if (alias) { actual = alias; }\n"
        + "  if (!actual) {\n"
        + "    for (var n = 0; n < marks.length; n++) {\n"
        + "      if (isOn(marks[n].li)) { actual += marks[n].letter; }\n"
        + "    }\n"
        + "  }\n"
        + "  actual = actual.replace(/\\s+/g, '').toUpperCase().split('').sort().join('');\n"
        + "  var result = {\n"
        + "    ok: true, qid: qid, mode: multi ? 'multi' : 'single',\n"
        + "    expected: " + _js_literal(letters) + ", actual: actual, clicked: clicked\n"
        + "  };\n"
        + "  if (actual !== result.expected) {\n"
        + "    result.ok = false;\n"
        + "    result.error = '页面回读的答案与预期不一致';\n"
        + "  }\n"
        + "  return result;\n"
        + "})()\n"
    )


def _fill_script_fill(question: Question, answer: str) -> str:
    values = _fill_values(answer)
    head = _JS_BLOCK.format(qid=_js_literal(question.qid))
    return (
        "(function () {\n"
        + head
        + "  var values = " + _js_string_list(values) + ";\n"
        + "  var raw = block.querySelectorAll('input, textarea');\n"
        + "  var fields = [];\n"
        + "  for (var i = 0; i < raw.length; i++) {\n"
        + "    var node = raw[i];\n"
        + "    var tag = String(node.tagName || '').toLowerCase();\n"
        + "    var type = String(node.getAttribute('type') || 'text').toLowerCase();\n"
        + "    if (tag === 'input' && ['hidden', 'checkbox', 'radio', 'submit', 'button', "
        + "'file', 'image'].indexOf(type) >= 0) { continue; }\n"
        + "    fields.push(node);\n"
        + "  }\n"
        + "  if (fields.length !== values.length) {\n"
        + "    throw new Error('第 "
        + str(question.number)
        + " 题填空数量不匹配：页面上 ' + fields.length + ' 个，答案 ' + values.length + ' 个');\n"
        + "  }\n"
        + "  for (var j = 0; j < fields.length; j++) {\n"
        + "    var field = fields[j];\n"
        + "    field.value = values[j];\n"
        + "    try {\n"
        + "      field.dispatchEvent(new Event('input', { bubbles: true }));\n"
        + "      field.dispatchEvent(new Event('change', { bubbles: true }));\n"
        + "    } catch (err) {\n"
        + "      if (field.fireEvent) { field.fireEvent('onchange'); }\n"
        + "    }\n"
        + "  }\n"
        + "  return { ok: true, qid: qid, mode: 'fill', expected: values.join('|'), "
        + "actual: values.join('|'), filled: fields.length };\n"
        + "})()\n"
    )


def verify_script(question: Question, answer: str) -> str:
    """生成「核验这一题」的 JS **布尔表达式**：读回页面当前选择/填写值并与答案比对。

    返回值是表达式而不是语句，``eval`` 结果就是 ``true`` / ``false``，适合
    ``page.eval(verify_script(...))`` 直接判断。无法作答的题抛 :class:`ValueError`。
    """
    _require_answerable(question)
    if question.qtype == FILL_QTYPE:
        return _verify_script_fill(question, answer)
    return _verify_script_choice(question, answer)


def _verify_script_choice(question: Question, answer: str) -> str:
    letters = _answer_letters(question, answer)
    head = _JS_BLOCK.format(qid=_js_literal(question.qid))
    return (
        "(function () {\n"
        + head
        + "  var aliases = " + _js_aliases(question) + ";\n"
        + "  var actual = '';\n"
        + "  var hidden = document.getElementById('answer' + qid);\n"
        + "  if (hidden && typeof hidden.value === 'string') { actual = hidden.value; }\n"
        + "  actual = actual.replace(/\\s+/g, '').toUpperCase();\n"
        + "  if (aliases[actual]) { actual = aliases[actual]; }\n"
        + "  if (!actual) {\n"
        + "    var all = block.getElementsByTagName('li');\n"
        + "    for (var i = 0; i < all.length; i++) {\n"
        + "      var li = all[i];\n"
        + "      var on = !!li.querySelector('.check_answer, .check_answer_dx')\n"
        + "        || String(li.getAttribute('aria-checked') || '').toLowerCase() === 'true';\n"
        + "      if (!on) {\n"
        + "        var input = li.querySelector('input[type=radio], input[type=checkbox]');\n"
        + "        on = !!(input && input.checked);\n"
        + "      }\n"
        + "      if (!on) { continue; }\n"
        + "      var mark = li.querySelector('.num_option, .num_option_dx');\n"
        + "      if (mark) {\n"
        + "        actual += String(mark.getAttribute('data') || mark.textContent || '')\n"
        + "          .replace(/\\s+/g, '').toUpperCase();\n"
        + "      }\n"
        + "    }\n"
        + "  }\n"
        + "  actual = actual.split('').sort().join('');\n"
        + "  return actual === " + _js_literal(letters) + ";\n"
        + "})()\n"
    )


def _verify_script_fill(question: Question, answer: str) -> str:
    values = _fill_values(answer)
    head = _JS_BLOCK.format(qid=_js_literal(question.qid))
    return (
        "(function () {\n"
        + head
        + "  var raw = block.querySelectorAll('input, textarea');\n"
        + "  var fields = [];\n"
        + "  for (var i = 0; i < raw.length; i++) {\n"
        + "    var node = raw[i];\n"
        + "    var tag = String(node.tagName || '').toLowerCase();\n"
        + "    var type = String(node.getAttribute('type') || 'text').toLowerCase();\n"
        + "    if (tag === 'input' && ['hidden', 'checkbox', 'radio', 'submit', 'button', "
        + "'file', 'image'].indexOf(type) >= 0) { continue; }\n"
        + "    fields.push(node);\n"
        + "  }\n"
        + "  var actual = [];\n"
        + "  for (var j = 0; j < fields.length; j++) {\n"
        + "    actual.push(String(fields[j].value || '').trim());\n"
        + "  }\n"
        + "  return actual.join('|') === " + _js_literal("|".join(values)) + ";\n"
        + "})()\n"
    )


def submit_script(dry_run: bool) -> str:
    """生成提交 JS。

    ``dry_run=True``：**只定位**提交按钮并返回按钮文字（不点击），用于「先确认页面就绪」；
    ``dry_run=False``：点 ``a.btnSubmit`` → 等 ``#workpop`` 确认层（``#popok`` 提交）或
    旧版 ``#confirmSubWin``（``submitCheckTimes()`` 确定）并点掉 → 立刻返回。

    提交结果由调用方回读页面判断（成功时页面会跳转，JS 上下文随之销毁，所以这里
    不在确认后继续等待）：``#submitBack`` 出现表示「未达到及格线，请重做」，
    ``#verifyCodeWin`` 出现表示提交次数超限、需要图形验证码（必须人工）。
    """
    if dry_run:
        return _submit_script_dry()
    return _submit_script_real()


def _submit_script_dry() -> str:
    return (
        "(function () {\n"
        "  var selectors = ['a.btnSubmit', '#ZyBottom a.btnSubmit',\n"
        "    'a[onclick*=\"btnBlueSubmit\"]', 'a[onclick*=\"submitCheckTimes\"]'];\n"
        "  var button = null;\n"
        "  var used = '';\n"
        "  for (var i = 0; i < selectors.length; i++) {\n"
        "    button = document.querySelector(selectors[i]);\n"
        "    if (button) { used = selectors[i]; break; }\n"
        "  }\n"
        "  if (!button) { return { ok: false, dry_run: true, error: '找不到提交按钮' }; }\n"
        "  var text = String(button.innerText || button.textContent || '')"
        ".replace(/\\s+/g, ' ').trim();\n"
        "  return {\n"
        "    ok: true, dry_run: true, selector: used, text: text,\n"
        "    onclick: button.getAttribute('onclick') || ''\n"
        "  };\n"
        "})()\n"
    )


def _submit_script_real() -> str:
    return (
        "(async function () {\n"
        "  function docs() {\n"
        "    var list = [document];\n"
        "    try {\n"
        "      if (window.top && window.top !== window && window.top.document) { "
        "list.push(window.top.document); }\n"
        "    } catch (err) { }\n"
        "    return list;\n"
        "  }\n"
        "  function find(selector) {\n"
        "    var list = docs();\n"
        "    for (var i = 0; i < list.length; i++) {\n"
        "      var node = list[i].querySelector(selector);\n"
        "      if (node) { return node; }\n"
        "    }\n"
        "    return null;\n"
        "  }\n"
        "  function visible(node) {\n"
        "    if (!node) { return false; }\n"
        "    var win = (node.ownerDocument && node.ownerDocument.defaultView) || window;\n"
        "    var style = win.getComputedStyle(node);\n"
        "    if (style.display === 'none' || style.visibility === 'hidden'"
        " || style.opacity === '0') { return false; }\n"
        "    return node.getClientRects().length > 0;\n"
        "  }\n"
        "  function sleep(ms) { "
        "return new Promise(function (resolve) { setTimeout(resolve, ms); }); }\n"
        "  function tipText(selector) {\n"
        "    var node = find(selector);\n"
        "    return node ? String(node.innerText || node.textContent || '')"
        ".replace(/\\s+/g, ' ').trim() : '';\n"
        "  }\n"
        "  var button = find('a.btnSubmit') || find('#ZyBottom a.btnSubmit')\n"
        "    || find('a[onclick*=\"btnBlueSubmit\"]')"
        " || find('a[onclick*=\"submitCheckTimes\"]');\n"
        "  if (!button) { return { ok: false, error: '找不到提交按钮' }; }\n"
        "  var button_text = String(button.innerText || button.textContent || '')"
        ".replace(/\\s+/g, ' ').trim();\n"
        "  button.click();\n"
        "  var callbacks = [\n"
        "    ['#workpop', '#popok'],\n"
        "    ['#confirmSubWin', '#confirmSubWin a[onclick*=\"submitCheckTimes\"]']\n"
        "  ];\n"
        "  var confirmed = '';\n"
        "  var deadline = Date.now() + 15000;\n"
        "  while (Date.now() < deadline) {\n"
        "    if (visible(find('#verifyCodeWin'))) {\n"
        "      return { ok: false, clicked_submit: true, need_verify_code: true,\n"
        "        error: '提交次数超限，页面要求图形验证码，必须人工处理' };\n"
        "    }\n"
        "    if (visible(find('#submitBack'))) {\n"
        "      return { ok: false, clicked_submit: true, rejected: true,\n"
        "        error: tipText('#submitBack #tipContent') || '页面拒绝提交（未达到及格线）' };\n"
        "    }\n"
        "    for (var i = 0; i < callbacks.length; i++) {\n"
        "      var layer = find(callbacks[i][0]);\n"
        "      if (!visible(layer)) { continue; }\n"
        "      var confirm_button = find(callbacks[i][1]);\n"
        "      if (!confirm_button) { continue; }\n"
        "      confirm_button.click();\n"
        "      confirmed = callbacks[i][0] + ' ' + callbacks[i][1];\n"
        "      break;\n"
        "    }\n"
        "    if (confirmed) { break; }\n"
        "    await sleep(250);\n"
        "  }\n"
        "  if (!confirmed) {\n"
        "    return { ok: false, clicked_submit: true, error: '点击提交后没有等到确认弹层' };\n"
        "  }\n"
        "  return { ok: true, submitted: true, clicked_submit: true,\n"
        "    confirm_selector: confirmed, submit_text: button_text };\n"
        "})()\n"
    )
