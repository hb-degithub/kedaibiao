"""批改网（pigai.org）SSR HTML 解析层：**纯标准库、完全离线**。

* 规格出处：``docs/research/pigai.md`` §6（数据面规格；每个函数的 docstring 里
  逐条标注对应的 §6.x 小节）。
* 契约出处：``kedaibiao.platforms.pigai.protocol``（**冻结**，本模块只读不改）。
  本模块产出的对象类型全部来自 ``protocol``，不自定义跨模块类型。

为什么不引入 bs4 / lxml
    ``#essayList`` 的表头 ``ul.reItemM`` 里嵌了一个
    ``<div style="clear:both"></div>``（非法 HTML，pigai.md §6.2）。严格 DOM
    解析器会「修正」这层嵌套、把行结构挪位；标准库 ``html.parser`` 是容错的事件
    流解析器，正好吃下这种页面。故本模块全程只用 ``html.parser``。

三条纪律（task-3 约束，逐条落实）
    1. 先校验表头（``protocol.DONE_HEADERS`` / ``PENDING_HEADERS``）再按列序取值；
       表头不一致一律抛 :class:`HeaderMismatchError` —— **错位的解析结果比崩溃更危险**。
    2. 单行/单条解析失败只跳过该条（并发一条 warning 日志），不影响其余条目；
       只有「整页识别不出来」才抛 :class:`PageShapeError`。
    3. ``Annotation.level`` / ``Annotation.cate`` 遇到未知取值**原样保留字符串**。

task-6 增量（正文字段 / 写作页 / 修两个解析缺陷）
    * :func:`parse_essay_detail` 增加 ``essay_text``（容器 ``#contents``）；
    * 新增 :func:`parse_write_page`（``Endpoints.lookup_by_rid`` 的「按作业号定位」页）；
    * 列表行**不做跨列回落**（标题只从标题格、URL 只从操作格）；
    * 四维宽度守卫用 ``math.isfinite``，并保证 ``ratio`` 永不为 ``nan``/``inf``。

本模块不做任何 I/O：不联网、不读文件、不 ``print``、不 ``sleep``
（``docs/integration-guide.md`` §6.1 / §8）。
"""

from __future__ import annotations

import json
import logging
import math
import re
from collections.abc import Iterator
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Final
from urllib.parse import parse_qs, urlsplit

from . import protocol

logger = logging.getLogger(__name__)

__all__ = [
    "FbAnnotation",
    "HeaderMismatchError",
    "PageShapeError",
    "PigaiParseError",
    "parse_assignment_rows",
    "parse_class_rows",
    "parse_essay_detail",
    "parse_essay_rows",
    "parse_fb_js",
    "parse_write_page",
]


# --------------------------------------------------------------------------
# 异常
# --------------------------------------------------------------------------


class PigaiParseError(ValueError):
    """批改网解析层的基类异常（``ValueError`` 子类，便于调用方一次性捕获）。"""


class HeaderMismatchError(PigaiParseError):
    """表头与 ``protocol`` 里的表头常量不一致（或根本找不到表头）。

    抛出即代表**拒绝按列序取值**：宁可整页失败，也不给出错位的行数据。
    """


class PageShapeError(PigaiParseError):
    """整页一个已知锚点都没有（典型场景：会话过期后拿到登录页）。

    只用于「完全认不出这是不是目标页面」；页面部分字段缺失**不**抛此异常。
    """


# --------------------------------------------------------------------------
# 极简「近 DOM」树（容错，够用即可）
# --------------------------------------------------------------------------

#: HTML 空元素：不入栈，否则会把后续文本吞成它的子节点。
_VOID_TAGS: Final = frozenset(
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

#: 闭合标签可省略的元素：同标签连续出现时自动闭合前一个（页面本身就有未闭合标签）。
_AUTO_CLOSE_TAGS: Final = frozenset({"li", "p", "td", "th", "tr", "option", "dt", "dd"})

_WS_RE: Final = re.compile(r"\s+")
_INT_RE: Final = re.compile(r"[+-]?\d+")
_FLOAT_RE: Final = re.compile(r"[+-]?(?:\d+\.?\d*|\.\d+)")
_META_SPLIT_RE: Final = re.compile(r"[:：]")


class _Node:
    """元素节点。``children`` 里既有 ``_Node`` 也有 ``str``（文本片段）。"""

    __slots__ = ("attrs", "children", "parent", "tag")

    def __init__(self, tag: str, attrs: dict[str, str | None], parent: _Node | None) -> None:
        self.tag = tag
        self.attrs = attrs
        self.parent = parent
        self.children: list[_Node | str] = []


class _TreeBuilder(HTMLParser):
    """把容错的 HTML 事件流堆成一棵树。

    与浏览器行为的差异（都是刻意为之）：
      * 注释直接丢弃 —— 行内注释里藏着「查看/删除」入口（pigai.md §6.2），
        丢弃后操作列只剩可见文案。
      * ``<br>``/``<img>`` 等空元素不入栈。
      * ``<li>``/``<p>``/``<td>`` 等按 HTML 规则自动闭合前一个同名元素。
      * 找不到匹配的闭合标签就忽略（页面本身有非法嵌套）。
      * ``convert_charrefs=True``：文本与**属性值**里的实体都已被还原
        （``short_msg="&lt;b&gt;"`` 取出来就是 ``<b>``），不要再 unescape 一次。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("#document", {}, None)
        self._stack: list[_Node] = [self.root]

    def _attach(self, tag: str, attrs: list[tuple[str, str | None]]) -> _Node:
        node = _Node(tag, dict(attrs), self._stack[-1])
        self._stack[-1].children.append(node)
        return node

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _AUTO_CLOSE_TAGS and self._stack[-1].tag == tag:
            self._stack.pop()
        node = self._attach(tag, attrs)
        if tag not in _VOID_TAGS:
            self._stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # 例如 <a href='...'/> 这种自闭合写法（页面里真有，见 §6.5 的 snt_info）
        self._attach(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        for i in range(len(self._stack) - 1, 0, -1):
            if self._stack[i].tag == tag:
                del self._stack[i:]
                return
        # 孤立的闭合标签：忽略

    def handle_data(self, data: str) -> None:
        self._stack[-1].children.append(data)

    def handle_comment(self, data: str) -> None:  # noqa: ARG002 - 刻意丢弃注释
        return


def _parse_tree(html: str) -> _Node:
    builder = _TreeBuilder()
    builder.feed(html or "")
    builder.close()
    return builder.root


# --------------------------------------------------------------------------
# 树查询小工具
# --------------------------------------------------------------------------


def _text(node: _Node) -> str:
    """节点及其全部后代的文本（不含注释，注释没进树）。"""
    parts: list[str] = []
    stack: list[_Node | str] = [node]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            parts.append(item)
        else:
            stack.extend(reversed(item.children))
    return "".join(parts)


def _norm(text: str) -> str:
    """折叠全部空白（含 ``&nbsp;`` → ``\\xa0``）并 strip。"""
    return _WS_RE.sub(" ", text or "").strip()


def _child_elements(node: _Node) -> list[_Node]:
    return [child for child in node.children if isinstance(child, _Node)]


def _iter_elements(node: _Node, *, include_self: bool = True) -> Iterator[_Node]:
    """先序遍历元素（跳过文本节点）。"""
    stack: list[_Node] = [node] if include_self else list(reversed(_child_elements(node)))
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(_child_elements(current)))


def _classes(node: _Node) -> list[str]:
    return (node.attrs.get("class") or "").split()


def _has_class(node: _Node, name: str) -> bool:
    return name in _classes(node)


def _find_by_id(root: _Node, value: str) -> _Node | None:
    """按 ``id`` 找第一个元素，**大小写不敏感**。

    为什么要不敏感：页面里的标签大小写并不稳定（正文容器在详情页是
    ``<div id="contents">``、在写作页是大写 ``<TEXTAREA id="contents">``，
    见 ``protocol.DETAIL_BODY_ID`` / ``DETAIL_BODY_TAG`` / ``WRITE_BODY_TAG``）。
    统一按小写比较，避免「时好时坏」。
    """
    wanted = value.strip().lower()
    for node in _iter_elements(root):
        if (node.attrs.get("id") or "").strip().lower() == wanted:
            return node
    return None


def _find_by_class(root: _Node, name: str, *, tag: str | None = None) -> _Node | None:
    for node in _iter_elements(root):
        if (tag is None or node.tag == tag) and _has_class(node, name):
            return node
    return None


def _find_all_by_class(root: _Node, name: str, *, tag: str | None = None) -> list[_Node]:
    return [
        node
        for node in _iter_elements(root)
        if (tag is None or node.tag == tag) and _has_class(node, name)
    ]


def _li_children(node: _Node) -> list[_Node]:
    return [child for child in _child_elements(node) if child.tag == "li"]


def _first_anchor(node: _Node) -> _Node | None:
    if node.tag == "a":
        return node
    for element in _iter_elements(node, include_self=False):
        if element.tag == "a":
            return element
    return None


def _style_px(style: str | None, prop: str = "width") -> float | None:
    """从内联 ``style`` 里取 ``prop: 123.45px`` 的数值（取不到就 None）。"""
    match = re.search(rf"(?:^|;)\s*{prop}\s*:\s*([0-9]*\.?[0-9]+)\s*px\b", style or "")
    return float(match.group(1)) if match else None


def _to_int(text: str) -> int | None:
    match = _INT_RE.fullmatch((text or "").strip())
    return int(match.group()) if match else None


def _to_float(text: str) -> float | None:
    match = _FLOAT_RE.fullmatch((text or "").strip())
    return float(match.group()) if match else None


def _raw_href(href: str | None) -> str:
    """行对象的 ``url`` 一律**原样保留** DOM 里的 href（不做绝对化、不发明新 URL）。

    两种形态都真实存在：已完成列表是 ``/index.php?c=v2&a=view&eid=..&rid=..``；
    未完成列表是 ``?c=v2&a=write&rid=1000001``（无前导 ``/``、**不含 eid**，且指向
    **写作页**）。调用方需要绝对地址时自行 ``urljoin(protocol.BASE_URL, url)``。
    """
    return (href or "").strip()


def _href_params(href: str | None) -> dict[str, str]:
    """取 href 查询串的第一个值（``eid`` 只存在于 href 里，pigai.md §6.2）。"""
    query = parse_qs(urlsplit(href or "").query)
    return {key: values[0] for key, values in query.items() if values}


# --------------------------------------------------------------------------
# 选择器：protocol.py **未收录**，暂以 module-private 常量落地
# --------------------------------------------------------------------------
# ⚠ 契约纪律要求「选择器唯一出处是 protocol.py」。以下选择器在 protocol.py 里
#   没有对应常量（pigai.md §6.3/§6.5/§6.8 只给了部分锚点），按 task-3「不得改
#   protocol.py」的约束，这里以 private 常量兜底，并已报给 lead 待上收。
#   上收后应删除本节、改引用 protocol 常量。

_DIM_TRACK_WIDTH_PROP: Final = "width"  #: 进度条容器/进度的宽度属性（两个宽度都从 DOM 读）
_DIM_SCORE_TAG: Final = "td"  #: 权威分数（protocol.DETAIL_DIM_SCORE_ATTR）挂在这个标签上
#: protocol.Dimension.source 的取值。⚠ 与 protocol.DETAIL_DIM_SCORE_ATTR 同字面量但**不同概念**：
#: 那是 DOM 属性名，这是返回值里的来源标记，故不复用同一个常量。
_DIM_SOURCE_TITLE: Final = "title"
_DIM_SOURCE_WIDTH: Final = "width"
_TOPIC_WRAPPER_CLASS: Final = "yaoqiu_info"  #: §6.3 题目容器
_REQ_ITEM_CLASS: Final = "yq_itme"  #: §6.3 #yaoqiuW 内的条目
_REQ_MORE_ID: Final = "yq_more"  #: §6.3 「更多」链接条目，不算要求正文
_ANNOTATION_SID_ATTR: Final = "sid"  #: §6.5 按句点评条目判据
_ANNOTATION_SNT_ATTR: Final = "snt"
_ANNOTATION_KEY_ATTR: Final = "content"
_ANNOTATION_CATE_ATTR: Final = "cate"
_ANNOTATION_MSG_ATTR: Final = "short_msg"
_JS_EID_VAR: Final = "_eid"  #: §6.3 详情页内嵌变量
_JS_RID_VAR: Final = "_request_id"
_CLASS_BLOCK_CLASS: Final = "baseinfo"  #: §6.8 班级作业条目容器（未解剖完整 DOM）
_CLASS_DETAIL_UL_CLASS: Final = "detail"
_CLASS_STATUS_CLASS: Final = "m-score"
_CLASS_LABEL_ID: Final = "ID"
_CLASS_LABEL_SUBMIT: Final = "提交人数"
_CLASS_LABEL_DEADLINE: Final = "截止日期"
_TOPIC_LABEL_RE: Final = re.compile(r"^\s*题目\s*[:：]\s*")

# ── 写作页 /「按作业号定位」页（``Endpoints.lookup_by_rid``）的局部选择器 ──
#: 「题目： [rid] 标题」所在容器（实测 id="timu"）。
_WRITE_TOPIC_ID: Final = "timu"
#: 题目要求正文所在容器（实测 ``<div id="request_y"><p>…</p></div>``）。
_WRITE_REQUIREMENT_ID: Final = "request_y"
_WRITE_JS_RID_VAR: Final = "_rid"
_WRITE_JS_EID_VAR: Final = "_eid"
#: 前端开关名（红线：只读、不利用）。值必须仍在 protocol 的开关清单里，
#: 否则 import 期立刻失败 —— 避免「契约漂移后静默读到 False」。
_NO_PASTE_VAR: Final = "_no_paste"
if _NO_PASTE_VAR not in protocol.FRONTEND_SWITCH_VARS:  # pragma: no cover - 契约漂移护栏
    raise RuntimeError(
        f"{_NO_PASTE_VAR!r} 不在 protocol.FRONTEND_SWITCH_VARS 里；"
        "契约已漂移，必须同步 parse.py 后再启动"
    )

#: ``题目：[1000001] 示例课程（三）- Unit 6`` → 抓 ``(rid, 标题)``；方括号可缺省。
_TIMU_RE: Final = re.compile(r"^\s*题目\s*[:：]\s*(?:\[\s*(\d+)\s*\]\s*)?(.*)$", re.S)
#: ``字数:120~150`` —— 用「区间」形态自我区分：页内另一处 ``字数:123 词``（``#init_wc``）不会被匹配。
_WORD_RANGE_RE: Final = re.compile(r"字数\s*[:：]\s*(\d+\s*[~～\-—]\s*\d+)")
_FULL_MARKS_RE: Final = re.compile(r"满分\s*[:：]\s*(\d+(?:\.\d+)?)")
#: ``截止时间：2026-11-16 23:59`` —— 只认完整日期时间，避开被注释掉的「答题时间」。
_DEADLINE_RE: Final = re.compile(r"截止时间\s*[:：]\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2})")
_SERVER_TIME_RE: Final = re.compile(
    r"当前时间\s*[:：]\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2}(?::\d{2})?)"
)
#: ``var _no_paste = "1";`` 这类开关的真值判定（只读取，不利用）。
_NO_PASTE_TRUE_VALUES: Final = frozenset({"1", "true", "yes", "on"})


# --------------------------------------------------------------------------
# 页面级工具
# --------------------------------------------------------------------------


def _js_string_var(html: str, name: str) -> str | None:
    """取内嵌 JS 里的 ``var <name> = '...';``。

    :returns: 变量存在时返回其字符串值（**可能是空串**，例如未写过时的
        ``var _eid = ''``）；变量不存在时返回 ``None``。
        调用方靠这个区分「页内明确说是空」与「页内没有这个变量」——
        两者的回落策略不同（见 :func:`parse_write_page`）。
    """
    match = re.search(rf"\b{re.escape(name)}\s*=\s*['\"]([^'\"]*)['\"]", html or "")
    return match.group(1) if match else None


def _body_text(tree: _Node) -> str:
    """正文：``protocol.DETAIL_BODY_ID``（``#contents``）容器的文本。

    ⚠ **只按 id 选，不限标签**：同一个 id 在两种页面上是两种标签（契约
    ``DETAIL_BODY_ID`` 的 docstring 已写明「只按 id 匹配，不要按标签」）——

    * 详情页：``<div id="contents"><div id="parahtml">…``
      → 对应 ``protocol.DETAIL_BODY_TAG``（``div``）；
    * 写作页 / 定位页：``<TEXTAREA NAME="contents" id="contents">…``
      → 对应 ``protocol.WRITE_BODY_TAG``（``TEXTAREA``）。

    因此**不能**拿任一标签常量做过滤：按 ``DETAIL_BODY_TAG`` 过滤会丢掉写作页，
    按旧版（误写成 ``TEXTAREA`` 的 ``DETAIL_BODY_TAG``）过滤会把详情页读成空串
    —— 后者正是「正文时好时坏」的另一半成因（2026-10-19 已勘误）。
    标签大小写问题在树里天然不存在：``html.parser`` 已把标签名与属性名小写化
    （属性值除外，故 id 比较也做了大小写归一）。

    同一页还有 3 个 ``class="section-content draft-top|main|tail"`` 的 textarea
    （作文开头/主体/结尾**常见表达模板**）与 ``#beizhu`` —— 都不是 ``#contents``，
    按 id 选取天然不会误取。

    除 ``strip()`` 外**不做任何归一化**（用户要求正文原样可读/可回写），
    换行与段间空行都保留。
    """
    node = _find_by_id(tree, protocol.DETAIL_BODY_ID)
    if node is None:
        return ""
    known_tags = {protocol.DETAIL_BODY_TAG.lower(), protocol.WRITE_BODY_TAG.lower()}
    if node.tag not in known_tags:
        # 刻意不按标签过滤（契约也写明「只按 id 匹配，不要按标签」）。留一条 debug
        # 便于定位「正文时有时无」的历史问题（lead 曾被它坑过两次）。
        logger.debug(
            "正文容器 #%s 的实际标签是 <%s>；已知形态为详情页 <%s> / 写作页 <%s>",
            protocol.DETAIL_BODY_ID,
            node.tag,
            protocol.DETAIL_BODY_TAG,
            protocol.WRITE_BODY_TAG,
        )
    return _text(node).strip()


def _balanced_object(text: str, start: int) -> str | None:
    """``text[start]`` 是 ``{``：按 JSON 字符串规则扫描出配平的 ``{...}``。"""
    if start >= len(text) or text[start] != "{":
        return None
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        char = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _join_labels(text: str) -> tuple[str, str] | None:
    """把 ``"截止日期：2026-09-19 23:59"`` 拆成 ``("截止日期", "2026-09-19 23:59")``。"""
    parts = _META_SPLIT_RE.split(_norm(text), maxsplit=1)
    if len(parts) != 2:
        return None
    key, value = parts[0].strip(), parts[1].strip()
    return (key, value) if key else None


# --------------------------------------------------------------------------
# 列表页：先校验表头，再按列序取值
# --------------------------------------------------------------------------


def _validated_rows(
    html: str, expected_headers: tuple[str, ...], what: str
) -> tuple[list[list[_Node]], list[str]]:
    """定位 ``#essayList``，校验表头文案与列序，返回 ``(数据行的 li 列表, 表头文案)``。

    表头缺失或与 ``expected_headers`` 不一致 → :class:`HeaderMismatchError`。
    """
    tree = _parse_tree(html)
    container = _find_by_id(tree, protocol.LIST_CONTAINER_ID)
    if container is None:
        raise HeaderMismatchError(
            f"{what}：页面里找不到 #{protocol.LIST_CONTAINER_ID} 容器，拒绝解析"
        )

    header = _find_by_class(container, protocol.LIST_HEADER_CLASS, tag="ul")
    if header is None:
        raise HeaderMismatchError(
            f"{what}：找不到表头 ul.{protocol.LIST_HEADER_CLASS}，拒绝解析"
        )

    actual = tuple(_norm(_text(li)) for li in _li_children(header))
    if actual != expected_headers:
        raise HeaderMismatchError(
            f"{what}：表头与 protocol 常量不一致（列序可能已变），拒绝按列序取值。\n"
            f"  实际={actual}\n  期望={expected_headers}"
        )

    rows: list[list[_Node]] = []
    for row in _find_all_by_class(container, protocol.LIST_ROW_CLASS, tag="ul"):
        cells = _li_children(row)
        if len(cells) != len(expected_headers):
            # 列数对不上 → 跳过该行，绝不猜列位（§6.2「宁可少取不可错位」）
            logger.warning(
                "%s：跳过 1 行 —— 单元格 %d 个，期望 %d 个（表头 %s）",
                what,
                len(cells),
                len(expected_headers),
                protocol.LIST_HEADER_CLASS,
            )
            continue
        rows.append(cells)
    return rows, list(actual)


def parse_essay_rows(html: str) -> list[protocol.EssayRow]:
    """解析**已完成**作文列表 ``GET /index.php?c=write``（pigai.md §6.2，逐字核验）。

    八个 ``<li>`` 的顺序即 ``protocol.DONE_HEADERS`` 的列序：

    ``0 作文ID(=rid) | 1 作文标题(锚文本, href 里才有 eid) | 2 体裁 | 3 提交时间 |
    4 截止日期 | 5 成绩 | 6 提交次数 | 7 操作``

    成绩列在截止日期前是文字（``未截止``，§6.2 的 ⚠）→ 原样存进
    :attr:`protocol.EssayRow.score_raw`，由 ``.score`` 属性返回 ``None``。

    ``EssayRow.url`` 是**原样**的 href（``/index.php?c=v2&a=view&eid=..&rid=..``），
    需要绝对地址时调用方自行 ``urljoin(protocol.BASE_URL, url)``。

    :raises HeaderMismatchError: 表头不是 ``protocol.DONE_HEADERS``（含空页/错页）。
    """
    cells_rows, _ = _validated_rows(html, protocol.DONE_HEADERS, "已完成列表")
    out: list[protocol.EssayRow] = []
    for cells in cells_rows:
        try:
            rid = _norm(_text(cells[0]))
            if not rid:
                raise ValueError("第 0 列（作文ID）为空")
            anchor = _first_anchor(cells[1])
            href = (anchor.attrs.get("href") if anchor is not None else "") or ""
            params = _href_params(href)
            title = _norm(_text(anchor)) if anchor is not None else _norm(_text(cells[1]))
            row = protocol.EssayRow(
                rid=params.get("rid") or rid,
                eid=params.get("eid", ""),
                title=title,
                genre=_norm(_text(cells[2])),
                submitted_at=_norm(_text(cells[3])),
                deadline=_norm(_text(cells[4])),
                score_raw=_norm(_text(cells[5])),
                submit_count=_to_int(_norm(_text(cells[6]))) or 0,
                url=_raw_href(href),
            )
        except (ValueError, IndexError, TypeError) as exc:  # 单行失败只跳过该行
            logger.warning("已完成列表：跳过 1 行（%s）", exc)
            continue
        out.append(row)
    return out


def parse_assignment_rows(html: str) -> list[protocol.AssignmentRow]:
    """解析**未完成**作业列表 ``GET /index.php?c=write&a=waitComplete``。

    七列列序即 ``protocol.PENDING_HEADERS``：
    ``0 作业号(=rid) | 1 作业标题 | 2 类型 | 3 出题人 | 4 指定班级 | 5 截止时间 | 6 操作``。

    ⚠ **证据强度（逐条区分，请勿混为一谈）**
      * **实测**：表头 7 条文案与列序 —— ``waitComplete`` 页的 ``ul.reItemM``
        与 ``protocol.PENDING_HEADERS`` 逐字一致。
      * **实测**：行级模板 ``#essayList > ul.reItemS``（7 个 ``li``）—— 2026-10-19
        重取的 ``data/recon/pigai/raw/b_index_php_c_write_a_waitComplete.html``
        里**确有 1 条真实数据行**（作业号 1000001），列序已逐字核对。
        **注意**：这与 ``pigai.md`` §6.1 / 8-U15「本轮只有表头、0 行」的旧结论
        不一致（样本是后来重取的），证据以上述样本为准。
      * **推断**：分页/多行时的行为；``操作`` 列的完整取值集合；第 6 列之外是否
        还有隐藏列；字段语义（``kind``/``author``/``classes``/``deadline`` 与
        第 2/3/4/5 列的对应关系）。
      * **保底（实测要求）**：``raw_cells`` 一定填充 7 列原始单元格文本，
        样本扩大后可据此校正列位。

    两个已实测的细节：

      * 标题单元格的 ``<a title="查看作文:">`` 是**空标题**，不可当作标题用 →
        标题取**锚文本**（实测为作业名），``title`` 属性忽略。
      * ``url`` 存**原样** href：``?c=v2&a=write&rid=1000001``（无前导 ``/``、
        **不含 eid**，且指向**写作页**而非只读详情页）。第 4 列（指定班级）的
        单元格文本与它的 ``title`` 属性内容相同，本实现取**文本**。

    ⚠ **禁止跨列回落**（task-6 修正验证者「需知会-1」）：标题**只**从第 1 列取
    （该格没有锚点就退化为该格纯文本），``url`` **只**从第 6 列（操作格）取锚点。
    两者不得互相回落 —— 否则「标题格无锚点」时会拿操作格的「查看」当标题、
    把操作格的 href 串成 url，正是「错位取值」。第 0 列为空时 ``rid`` 的兜底也
    只认**标题格**的 href。

    :raises HeaderMismatchError: 表头不是 ``protocol.PENDING_HEADERS``。
    """
    cells_rows, _ = _validated_rows(html, protocol.PENDING_HEADERS, "未完成列表")
    out: list[protocol.AssignmentRow] = []
    for cells in cells_rows:
        try:
            raw_cells = tuple(_norm(_text(li)) for li in cells)
            title_anchor = _first_anchor(cells[1])  # 标题：只认第 1 列
            action_anchor = _first_anchor(cells[6])  # URL：只认操作列
            title_href = (title_anchor.attrs.get("href") if title_anchor else "") or ""
            action_href = (action_anchor.attrs.get("href") if action_anchor else "") or ""
            rid = _norm(_text(cells[0])) or _href_params(title_href).get("rid", "")
            if not rid:
                raise ValueError("第 0 列（作业号）为空且标题格 href 里也没有 rid")
            row = protocol.AssignmentRow(
                rid=rid,
                title=_norm(_text(title_anchor))
                if title_anchor is not None
                else _norm(_text(cells[1])),
                kind=_norm(_text(cells[2])),
                author=_norm(_text(cells[3])),
                classes=_norm(_text(cells[4])),
                deadline=_norm(_text(cells[5])),
                url=_raw_href(action_href),
                raw_cells=raw_cells,
            )
        except (ValueError, IndexError, TypeError) as exc:  # 单行失败只跳过该行
            logger.warning("未完成列表：跳过 1 行（%s）", exc)
            continue
        out.append(row)
    return out


# --------------------------------------------------------------------------
# 评分详情页
# --------------------------------------------------------------------------


def _annotation_items(tree: _Node) -> list[_Node]:
    """按句点评条目：带 ``sid`` 属性的 ``<li>``（§6.5）。

    页内另有 ``<li class="tdianping">``（教师点评/段评，无 ``sid``）→ 天然排除。
    """
    return [
        node
        for node in _iter_elements(tree)
        if node.tag == "li" and node.attrs.get(_ANNOTATION_SID_ATTR) is not None
    ]


def _parse_dimensions(tree: _Node) -> tuple[protocol.Dimension, ...]:
    """四维分数（§6.4）。取值优先级见 ``protocol.Dimension``：**优先 title，兜底宽度比**。

    DOM 形状（实测）::

        <div class="tbL">词汇: </div>
        <div style="float:left;width:154px;...">            <!-- 容器：读它的宽 -->
          <table style="width:138.26512px;...">              <!-- 进度条：读它的宽 -->
            <td ... width: 134.26512px; ... title="0.8951008"></td>   <!-- 权威分数 -->
            <td ... width: 2px; ...><img ...></td>

    1. **首选** 进度条 ``td`` 的 ``protocol.DETAIL_DIM_SCORE_ATTR``（``title``）——
       实测 8 位有效数字的权威分数，``source="title"``；
    2. **兜底** ``条宽 / 容器宽``，``source="width"``；两个宽度**都从 DOM 读**。

    §6.4 里的 ``154px`` 只是**那一个样本**的容器宽，故此处绝不硬编码。
    实测两条路径系统性相差 0.003~0.006（§6.4 的渲染近似），差异原样保留、不校正。

    宽度守卫（task-6 修「建议-6/-7」；task-10 按 lead 裁定调整优先级）：

    * **``title`` 是否可用，与宽度是否可用无关** —— 只要 ``title`` 是**有限数**就用它，
      哪怕宽度是 ``inf``（或干脆读不到）。理由（lead 裁定）：``title`` 是权威值、
      宽度比只是它的渲染近似，两者来源独立；兜底路径坏掉不代表这一维度坏掉，
      丢掉一个独立的权威读数会把「渲染坏了」误判成「分数没了」。
      此时宽度字段**原样保留**读到的值（读不到记 ``0.0``），消费方看 ``source`` 即可。
    * ``title`` **自身**非有限（``title="9"*400`` → ``inf``）→ 视为拿不到 title，走兜底。
    * 走兜底（宽度比）时宽度**必须可用**，否则跳过，且措辞区分三种情形：
      缺失 → 「宽度读不到」；非有限 → 「不是有限值」；``容器宽 == 0``（真实读数）
      → 「容器宽为 0，无法计算宽度比」。
    * 除法结果再过一次 ``isfinite`` 兜底 —— 保证 **``ratio`` 永不为 ``nan``/``inf``**。
    """
    dims: list[protocol.Dimension] = []
    for label in _find_all_by_class(tree, protocol.DETAIL_DIM_LABEL_CLASS):
        name = _norm(_text(label)).rstrip(":：").strip()
        if not name:
            continue
        siblings = _child_elements(label.parent) if label.parent is not None else []
        try:
            position = siblings.index(label)
        except ValueError:  # pragma: no cover - 理论上不会发生
            continue
        container = next(
            (
                node
                for node in siblings[position + 1 :]
                if _style_px(node.attrs.get("style"), _DIM_TRACK_WIDTH_PROP) is not None
            ),
            None,
        )
        if container is None:
            logger.warning("评分详情：维度「%s」找不到宽度容器，跳过该维度", name)
            continue
        container_width = _style_px(container.attrs.get("style"), _DIM_TRACK_WIDTH_PROP)
        bar = container if container.tag == "table" else None
        if bar is None:
            bar = next(
                (node for node in _iter_elements(container) if node.tag == "table"), None
            )
        if bar is None:
            logger.warning("评分详情：维度「%s」找不到进度条 table，跳过该维度", name)
            continue
        bar_width = _style_px(bar.attrs.get("style"), _DIM_TRACK_WIDTH_PROP)

        # 1) 权威分数：进度条内的 td title —— **与宽度是否可用无关**。
        #    lead 裁定（task-10）：title 是权威值、宽度比只是它的渲染近似，
        #    两者来源独立；兜底路径坏掉不代表这一维度坏掉，不能把「渲染坏了」
        #    误判成「分数没了」。
        score: float | None = None
        for cell in _iter_elements(bar):
            if cell.tag != _DIM_SCORE_TAG:
                continue
            candidate = _to_float(cell.attrs.get(protocol.DETAIL_DIM_SCORE_ATTR) or "")
            if candidate is not None and math.isfinite(candidate):
                score = candidate
                break
        widths_usable = (
            container_width is not None
            and bar_width is not None
            and math.isfinite(container_width)
            and math.isfinite(bar_width)
        )
        if score is not None:
            if not widths_usable:
                # 不静默：宽度异常要留痕，但**不影响**权威分数的采用
                logger.warning(
                    "评分详情：维度「%s」宽度不可用（容器=%r 条=%r）→ 采用 title 权威值 %r",
                    name,
                    container_width,
                    bar_width,
                    score,
                )
            # 宽度字段原样保留读到的值；读不到时记 0.0（此时请看 source，ratio 来自 title）
            dims.append(
                protocol.Dimension(
                    name=name,
                    ratio=score,
                    bar_width=bar_width if bar_width is not None else 0.0,
                    container_width=container_width if container_width is not None else 0.0,
                    source=_DIM_SOURCE_TITLE,
                )
            )
            continue

        # 2) 兜底：宽度比（title 缺失或 title 本身不可用时才走这里）→ 宽度必须可用
        if container_width is None or bar_width is None:
            logger.warning(
                "评分详情：维度「%s」宽度读不到（容器=%r 条=%r）且缺少可用 title，跳过该维度",
                name,
                container_width,
                bar_width,
            )
            continue
        # 非有限值（如 float("9"*400) → inf）：绝不让它流进 ratio，否则会得到 inf/nan
        if not (math.isfinite(container_width) and math.isfinite(bar_width)):
            logger.warning(
                "评分详情：维度「%s」宽度不是有限值（容器=%r 条=%r）且缺少可用 title，"
                "跳过该维度",
                name,
                container_width,
                bar_width,
            )
            continue
        if container_width == 0:
            # 不是「读不到」，是读到了 0 —— 措辞要说清楚，别把 0 说成缺失
            logger.warning("评分详情：维度「%s」容器宽为 0，无法计算宽度比，跳过该维度", name)
            continue
        logger.warning("评分详情：维度「%s」缺少可用 title，回退到宽度比", name)
        ratio = bar_width / container_width
        if not math.isfinite(ratio):
            logger.warning(
                "评分详情：维度「%s」宽度比不是有限值（%r/%r），跳过该维度",
                name,
                bar_width,
                container_width,
            )
            continue
        if ratio > 1.0:
            logger.warning(
                "评分详情：维度「%s」比值 > 1（%s/%s），原样返回", name, bar_width, container_width
            )
        dims.append(
            protocol.Dimension(
                name=name,
                ratio=ratio,
                bar_width=bar_width,
                container_width=container_width,
                source=_DIM_SOURCE_WIDTH,
            )
        )
    return tuple(dims)


def _parse_annotations(tree: _Node) -> tuple[protocol.Annotation, ...]:
    """按句点评（§6.5）。未知 ``class``/``cate`` 一律原样保留字符串。"""
    out: list[protocol.Annotation] = []
    for item in _annotation_items(tree):
        try:
            sid = _to_int(item.attrs.get(_ANNOTATION_SID_ATTR) or "")
            if sid is None:
                raise ValueError("sid 不是整数")
            tokens = (item.attrs.get("class") or "").split()
            level = next((t for t in tokens if t in protocol.ANNOTATION_LEVELS), None)
            if level is None:  # 未知等级：原样保留（不映射成枚举失败）
                level = tokens[0] if tokens else ""
            out.append(
                protocol.Annotation(
                    sid=sid,
                    sentence=_norm(item.attrs.get(_ANNOTATION_SNT_ATTR) or ""),
                    level=level,
                    cate=(item.attrs.get(_ANNOTATION_CATE_ATTR) or "").strip(),
                    keyword=(item.attrs.get(_ANNOTATION_KEY_ATTR) or "").strip(),
                    message=_norm(item.attrs.get(_ANNOTATION_MSG_ATTR) or ""),
                )
            )
        except (ValueError, TypeError) as exc:
            logger.warning("评分详情：跳过 1 条按句点评（%s）", exc)
            continue
    return tuple(out)


def _parse_meta(tree: _Node) -> dict[str, str]:
    """作业元信息 ``ul.stu_info_item``（§6.3）→ ``{"开始时间": "...", "满分": "100", ...}``。"""
    meta: dict[str, str] = {}
    info = _find_by_class(tree, protocol.DETAIL_META_CLASS, tag="ul")
    if info is None:
        return meta
    for li in _li_children(info):
        pair = _join_labels(_text(li))
        if pair is not None:
            meta[pair[0]] = pair[1]
    return meta


def _parse_topic(tree: _Node) -> str:
    """题目：优先 ``.yaoqiu_info > h3``，取不到就取页内第一个 ``h3``（§6.3）。"""
    wrapper = _find_by_class(tree, _TOPIC_WRAPPER_CLASS)
    scope = wrapper if wrapper is not None else tree
    for node in _iter_elements(scope):
        if node.tag == protocol.DETAIL_TOPIC_TAG:
            return _TOPIC_LABEL_RE.sub("", _norm(_text(node))).strip()
    return ""


def _parse_requirements(tree: _Node) -> str:
    """要求：``#yaoqiuW`` 内的 ``.yq_itme`` 条目文本（排除「更多」），§6.3。"""
    holder = _find_by_id(tree, protocol.DETAIL_REQUIREMENTS_ID)
    if holder is None:
        return ""
    items = [
        _norm(_text(node))
        for node in _iter_elements(holder)
        if _has_class(node, _REQ_ITEM_CLASS) and node.attrs.get("id") != _REQ_MORE_ID
    ]
    items = [item for item in items if item]
    return "\n".join(items) if items else _norm(_text(holder))


def parse_essay_detail(
    html: str, *, rid: str = "", eid: str = ""
) -> protocol.EssayDetail:
    """解析评分详情页 ``GET /index.php?c=v2&a=view&eid=&rid=``（§6.3~§6.6）。

    * ``score`` ← ``#rescoreTxt`` 的**文本**；``full_marks`` ← 它的 ``content`` 属性
      （§6.3，两者同源不同属性）。
    * ``dimensions`` ← 进度条宽 / 容器宽，两个宽度都从 DOM 读（§6.4）。
    * ``annotations`` ← 带 ``sid`` 的 ``<li>``（§6.5）；``overall_comment`` ←
      ``#allpingyu_txt``；``meta`` ← ``ul.stu_info_item``；``topic``/``requirements``
      ← ``.yaoqiu_info > h3`` / ``#yaoqiuW``。
    * ``essay_text`` ← ``#contents``（``protocol.DETAIL_BODY_ID``）。⚠ **实测该容器在
      详情页是 ``<div>`` 而不是 ``<TEXTAREA>``**（正文在其中的 ``#parahtml`` 里），
      故选取只按 id、不限标签，详见 :func:`_body_text`。除 ``strip()`` 外不归一化。
    * ``rid``/``eid``：显式传入的优先；传空则回落到页内内嵌变量
      ``_request_id`` / ``_eid``（实测存在，§6.3 的脚本区）。
    * ``fb_js`` **不在本返回值里**（``protocol.EssayDetail`` 没有该字段，契约冻结）→
      单独用 :func:`parse_fb_js` 取。

    :raises PageShapeError: 页面里一个已知锚点都没有（典型场景：登录页/会话过期）。
        **``#contents`` 不算锚点** —— 写作页也有同一个 id，若把它计入锚点，
        写作页会被静默解析成「几乎全空」的详情对象。
    """
    tree = _parse_tree(html)
    score_el = _find_by_id(tree, protocol.DETAIL_SCORE_ID)
    comment_el = _find_by_id(tree, protocol.DETAIL_COMMENT_ID)
    requirement_el = _find_by_id(tree, protocol.DETAIL_REQUIREMENTS_ID)
    meta_el = _find_by_class(tree, protocol.DETAIL_META_CLASS, tag="ul")
    dim_labels = _find_all_by_class(tree, protocol.DETAIL_DIM_LABEL_CLASS)
    annotation_items = _annotation_items(tree)

    if not any((score_el, comment_el, requirement_el, meta_el, dim_labels, annotation_items)):
        raise PageShapeError(
            "评分详情页：找不到任何已知锚点（#rescoreTxt / #allpingyu_txt / "
            f"#{protocol.DETAIL_REQUIREMENTS_ID} / ul.{protocol.DETAIL_META_CLASS} / "
            f".{protocol.DETAIL_DIM_LABEL_CLASS} / li[sid]），可能是登录页或非详情页"
        )

    dimensions = _parse_dimensions(tree)
    if dimensions and tuple(d.name for d in dimensions) != protocol.DETAIL_DIM_NAMES:
        logger.warning(
            "评分详情：维度名与 protocol.DETAIL_DIM_NAMES 不一致：%s",
            tuple(d.name for d in dimensions),
        )

    return protocol.EssayDetail(
        rid=rid or _js_string_var(html, _JS_RID_VAR) or "",
        eid=eid or _js_string_var(html, _JS_EID_VAR) or "",
        score=_to_float(_text(score_el)) if score_el is not None else None,
        full_marks=_to_float(score_el.attrs.get("content") or "") if score_el is not None else None,
        dimensions=dimensions,
        overall_comment=_norm(_text(comment_el)) if comment_el is not None else "",
        annotations=_parse_annotations(tree),
        meta=_parse_meta(tree),
        topic=_parse_topic(tree),
        requirements=_parse_requirements(tree),
        essay_text=_body_text(tree),
    )


@dataclass(frozen=True, slots=True)
class FbAnnotation:
    """``fb_js`` 字典的一条（§6.6）。

    这是 **parse.py 本地类型**：``protocol`` 里只有变量名常量
    ``DETAIL_FB_JS_VAR``，没有对应的数据结构（契约冻结，未擅自新增到 protocol）。
    """

    keyword: str
    kp: str
    sids: tuple[int, ...]
    message: str
    category: str


# 变量名不重复定义：直接取契约里的 protocol.DETAIL_FB_JS_VAR
_FB_JS_ASSIGN_RE: Final = re.compile(
    rf"\b{re.escape(protocol.DETAIL_FB_JS_VAR)}\s*=\s*(\{{)"
)


def parse_fb_js(html: str) -> dict[str, FbAnnotation]:
    """解析内嵌批注字典 ``var fb_js = {...}``（§6.6，已核验）。

    ⚠ **这不是 ``protocol`` 契约的一部分**（契约里只有变量名常量
    ``DETAIL_FB_JS_VAR``，没有对应数据结构）。它是解析层的**加分项 API**：
    ``protocol.EssayDetail`` 已由 ``annotations`` 覆盖只读需求，故本函数的存在
    不改变 adapter 的契约。返回值类型 :class:`FbAnnotation` 同为 parse-local。

    页面里该变量被赋值两次（先 ``""``，后真正的对象），按 JS 语义**后者胜**。
    值是 unicode 转义的 JSON 串，先配平花括号再 ``json.loads``；解析失败只记
    warning 并返回已成功的部分（字典是辅助数据，不拖垮整页解析）。
    """
    out: dict[str, FbAnnotation] = {}
    for match in _FB_JS_ASSIGN_RE.finditer(html or ""):
        raw = _balanced_object(html, match.start(1))
        if raw is None:
            continue
        try:
            data = json.loads(raw)
        except ValueError as exc:
            logger.warning("评分详情：fb_js 不是合法 JSON，跳过（%s）", exc)
            continue
        if not isinstance(data, dict):
            continue
        for keyword, entry in data.items():
            if not isinstance(entry, dict):
                continue
            sids: list[int] = []
            for value in entry.get("sid") or ():
                sid = _to_int(str(value)) if not isinstance(value, bool) else None
                if sid is not None:
                    sids.append(sid)
            out[str(keyword)] = FbAnnotation(
                keyword=str(keyword),
                kp=str(entry.get("kp") or ""),
                sids=tuple(sids),
                message=str(entry.get("txt") or ""),
                category=str(entry.get("category") or ""),
            )
    return out


# --------------------------------------------------------------------------
# 写作页 /「按作业号定位」页（Endpoints.lookup_by_rid）
# --------------------------------------------------------------------------


def _js_flag(html: str, name: str) -> bool:
    """读内嵌 JS 布尔开关（``var _no_paste = "1";``）。**只读取，不利用。**"""
    raw = _js_string_var(html, name)
    return bool(raw) and raw.strip().lower() in _NO_PASTE_TRUE_VALUES


def parse_write_page(html: str, *, rid: str = "", eid: str = "") -> protocol.WritePage:
    """解析写作页 /「按作业号定位」页（``Endpoints.lookup_by_rid``，用户缺口 B/C）。

    该页与写作页**功能等价**：``#contents`` 里能读回已有稿件正文。适配器只 GET，
    不 POST（GET 无副作用已由 lead 实测，见 ``protocol.Endpoints.lookup_by_rid``）。

    字段来源（全部实测，两种样本各验一遍）：

    ==================  ==========================================================
    ``rid``/``eid``     内嵌变量 ``_rid`` / ``_eid`` **优先**；页面里没有这两个
                        变量时才回落到传入参数。⚠ 二者语义不同：``_eid`` **存在但为空**
                        （``var _eid = ''``）是「这个作业还没写过」这一**事实**，
                        此时**不能**回落成调用方传进来的 eid。
    ``title``           ``#timu`` 的「题目： [rid] 标题」→ 取标题部分（去掉编号）。
    ``requirement``     ``#request_y`` 内文本（题目要求正文）。
    ``word_range``      ``字数:120~150`` → ``"120~150"``（原文，不做数字解释）。
    ``full_marks``      ``满分:100`` → ``100.0``。
    ``deadline``        ``截止时间：2026-11-16 23:59`` → **原文**，不做时区解释。
    ``server_time``     ``当前时间：2026-10-19 11:16:06`` → 原文（服务端时钟）。
    ``essay_text``      ``#contents``；未写过为空串。除 ``strip()`` 外不归一化。
    ``no_paste``        前端开关 ``_no_paste``（红线：只读取、不利用）。
    ==================  ==========================================================

    两个实测坑（都会让「时好时坏」重现，故写在这里）：

    1. ``#contents`` 在**写作页是大写 ``<TEXTAREA>``**、在**详情页是 ``<div>``**；
       本实现只按 id 选、不限标签，且 id 比较大小写不敏感（见 :func:`_body_text`）。
    2. 同页有 3 个 ``class="section-content draft-top|main|tail"`` 的 textarea
       （作文开头/主体/结尾**常见表达模板**）与 ``#beizhu``：**都不是正文**。
       ``字数`` 也有两处（``#v2_request_titlex`` 的区间、``#init_wc`` 的实时字数），
       故只认「区间」形态。

    :raises PageShapeError: 一个写作页锚点都没有（典型场景：登录页/会话过期）。
        ⚠ 判据**刻意不含** ``#contents`` 与 ``var _eid``：这两样在**详情页也有**
        （详情页同样有 ``#contents`` 与 ``_eid``），只有 ``#timu`` / ``#request_y`` /
        ``var _rid`` 才是写作页独有的。这样详情页被误喂进来会**明确报错**，
        而不是静默返回一个「字段几乎全空」的写作页对象。
    """
    tree = _parse_tree(html)
    contents = _find_by_id(tree, protocol.DETAIL_BODY_ID)
    topic_el = _find_by_id(tree, _WRITE_TOPIC_ID)
    requirement_el = _find_by_id(tree, _WRITE_REQUIREMENT_ID)
    js_rid = _js_string_var(html, _WRITE_JS_RID_VAR)
    js_eid = _js_string_var(html, _WRITE_JS_EID_VAR)
    if not any((topic_el, requirement_el, js_rid is not None)):
        raise PageShapeError(
            f"写作页：找不到任何写作页独有的锚点（#{_WRITE_TOPIC_ID} / "
            f"#{_WRITE_REQUIREMENT_ID} / var {_WRITE_JS_RID_VAR}），可能是登录页、"
            f"详情页或非写作页（本页 #contents={contents is not None}）"
        )

    page_text = _norm(_text(tree))

    title = ""
    if topic_el is not None:
        match = _TIMU_RE.match(_norm(_text(topic_el)))
        if match is not None:
            title = (match.group(2) or "").strip()

    word_range_match = _WORD_RANGE_RE.search(page_text)
    full_marks_match = _FULL_MARKS_RE.search(page_text)
    deadline_match = _DEADLINE_RE.search(page_text)
    server_time_match = _SERVER_TIME_RE.search(page_text)

    return protocol.WritePage(
        # 页内变量优先；变量「存在但为空」也算页面说了话（见 docstring 的 ⚠）
        rid=js_rid if js_rid is not None else rid,
        eid=js_eid if js_eid is not None else eid,
        title=title,
        requirement=_norm(_text(requirement_el)) if requirement_el is not None else "",
        word_range=_norm(word_range_match.group(1)) if word_range_match else "",
        full_marks=_to_float(full_marks_match.group(1)) if full_marks_match else None,
        deadline=deadline_match.group(1) if deadline_match else "",
        server_time=server_time_match.group(1) if server_time_match else "",
        essay_text=_body_text(tree),
        no_paste=_js_flag(html, _NO_PASTE_VAR),
    )


# --------------------------------------------------------------------------
# 班级页
# --------------------------------------------------------------------------


def parse_class_rows(html: str) -> list[protocol.ClassRow]:
    """解析班级页 ``GET /index.php?c=write&a=myclass`` 的作业条目（§6.8）。

    实测正文形如::

        ID：1000003   提交人数： 43   截止日期：2026-09-19 23:59   已截止

    条目容器 ``.baseinfo`` + ``ul.detail`` + 状态 ``.m-score`` 的选择器在
    ``protocol.py`` 里**没有常量**（§6.8 注明「未解剖完整 DOM」）→ 见文件上方
    「选择器」一节的说明，待 lead 上收。

    ``提交人数`` 是**班级维度的聚合值**，与「我提交了几次」（§6.2 第 6 列）不是
    一回事。空班级页返回 ``[]``（不是异常）。
    """
    tree = _parse_tree(html)
    blocks = _find_all_by_class(tree, _CLASS_BLOCK_CLASS)
    if not blocks:
        logger.warning(
            "班级页：找不到 .%s 作业条目（页面可能为空，也可能不是班级页）", _CLASS_BLOCK_CLASS
        )
        return []

    out: list[protocol.ClassRow] = []
    for block in blocks:
        try:
            detail = _find_by_class(block, _CLASS_DETAIL_UL_CLASS, tag="ul")
            if detail is None:
                raise ValueError(f"条目里找不到 ul.{_CLASS_DETAIL_UL_CLASS}")
            rid = ""
            submit_count: int | None = None
            deadline = ""
            for li in _li_children(detail):
                pair = _join_labels(_text(li))
                if pair is None:
                    continue
                key, value = pair
                if key == _CLASS_LABEL_ID:
                    rid = value
                elif key == _CLASS_LABEL_SUBMIT:
                    submit_count = _to_int(value)
                elif key == _CLASS_LABEL_DEADLINE:
                    deadline = value
            if not rid:  # 回落：条目内形如 <div class="flex" id="1000002"> 的数字 id
                for node in _iter_elements(block):
                    node_id = node.attrs.get("id") or ""
                    if node_id.isdigit():
                        rid = node_id
                        break
            if not rid:
                raise ValueError("条目里取不到作业 ID")
            status_el = _find_by_class(block, _CLASS_STATUS_CLASS)
            out.append(
                protocol.ClassRow(
                    rid=rid,
                    submit_count=submit_count,
                    deadline=deadline,
                    status=_norm(_text(status_el)) if status_el is not None else "",
                )
            )
        except (ValueError, TypeError) as exc:  # 单条失败只跳过该条
            logger.warning("班级页：跳过 1 个条目（%s）", exc)
            continue
    return out
