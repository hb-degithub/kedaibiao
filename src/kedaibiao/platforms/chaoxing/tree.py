"""学习通 / 超星 课程目录树解析：目录页 HTML → 结构化小节与任务点。

## 为什么必须盯着真实 DOM 写

目录页有两套完全不同的渲染，字段名也完全不同：

* **目录页**（本模块的主战场，``div.fanyaChapter`` 那一坨）::

      <div class="chapter_item">
         <div class="catalog_title chapter_Thats_bnt"> ... 章名在 span[title] ...
      </div>
      <div class="catalog_level"><ul><li>
         <div class="chapter_item" id="cur177855067"
              onclick="toOld('205643380', '177855067', '151631873',0)"
              title="1.1 一维，多维运动的描述">
             ... <span class="catalog_sbar">1.1</span> ...
             <div class="catalog_state icon_yiwanc catalog_tishi56">已完成</div>
      </li></ul></div>

* **学习页**（``frame_0.html`` 这类，标题是「学生学习页面」）用的是
  ``div.posCatalog_select`` / ``span.icon_Completed``，是另一套 DOM。

只解析目录页，理由很实际：目录页是「有哪些小节」的唯一权威来源；把学习页的
``posCatalog_*`` 也硬塞进同一个函数，只会让「解析不到」和「确实没有」混为一谈。
学习页目前只需要 tab 条，那是 :func:`parse_tabs` 的活。

## tab 为什么单独一个入口

真实抓包里目录页的 ``li[cardid]`` / ``changeDisplayContent`` 计数是 **0**：
课件 / 测验 / 扩展资源这组 tab 是「点开某个小节之后」在学习页里单独渲染的，
一份 HTML 只带当前小节的那一组::

    <li title="测验" cardid="415843881"
        onclick="changeDisplayContent(2,3,'461423614','219456814','151567344','');">
      <span class="spanText">测验</span></li>

所以 :func:`parse_chapter_tree` 负责目录树（如果同一份 HTML 里恰好也带了 tab
条，会按 ``changeDisplayContent`` 的第三个小节 id 挂回对应小节），
:func:`parse_tabs` 负责单独的小节 tab 条。

## 为什么用 HTMLParser 而不是正则

真实页面里 ``title`` / ``onclick`` 两种引号混用、属性顺序不固定、章节层级可变
（例如「10.3 波函数」下面还有「10.3.1 新建目录」这种三级小节）。用正则跨标签
匹配，引号一换就崩；HTMLParser 只做词法切分，层级和上下文由我们按栈维护。

## 完成状态怎么判

超星把状态放在 ``.catalog_state`` 的 class 上：``icon_yiwanc`` = 已完成，
``icon_weiwanc`` = 未完成。没有 ``.catalog_state`` 的小节会在 ``.catalog_task``
里放待完成任务点提示（``catalog_jindu`` / ``catalog_points_yi`` /
``bntHoverTips``），这些一律算 ``todo``；连提示都没有的小节才是 ``unknown``。
注意 ``catalog_points_yi`` 显示 ``0`` 时仍然是待完成样式（真实数据里概率论整门课
都是 0/0 任务点），这里不把它擅自升级成 ``done``。
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from html.parser import HTMLParser

__all__ = ["CourseTree", "Section", "Tab", "parse_chapter_tree", "parse_tabs"]

#: tab 标题 → kind。没见过的标题保留 "unknown"，让上层自己决定要不要处理。
_TAB_KIND_BY_TITLE = {"课件": "courseware", "测验": "quiz", "扩展资源": "extra"}

#: ``id="cur<chapterId>"``（转小写后再匹配，超星偶尔写 ``cur`` / ``CUR`` 混着来）
_CUR_ID_RE = re.compile(r"^cur(\d+)$", re.I | re.ASCII)

#: ``toOld('<courseid>','<chapterId>','<clazzid>',0)``，只取第二个参数；
#: 单双引号混用、以及外层属性用单引号包双引号的情况都要认。
_TO_OLD_RE = re.compile(
    r"toOld\(\s*['\"]([^'\"]*)['\"]\s*,\s*['\"]([^'\"]*)['\"]"
)

#: ``changeDisplayContent(<num>,<count>,'<chapterId>',...)``，单双引号都接受
_DISPLAY_RE = re.compile(
    r"changeDisplayContent\(\s*(\d+)\s*,\s*(\d+)\s*,\s*['\"]([^'\"]*)['\"]"
)

#: 「1个待完成任务点」「4个待完成任务点」
_PENDING_TIP_RE = re.compile(r"(\d+)\s*个待完成任务点")

#: html.parser 不会自动闭合的空元素：不压栈，否则后面每个 ``</div>`` 都会错位
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

#: 段首的编号，如 "1.1"、"10.3.1"，用于没有 .catalog_sbar 时兜底
_INDEX_PREFIX_RE = re.compile(r"^(\d+(?:\.\d+)*)\s+")


def _clean(text: str | None) -> str:
    """把空白折叠成单个空格并去首尾 —— 超星标题里全角/半角空格混着来。"""
    return re.sub(r"\s+", " ", text or "").strip()


def _has_class(element_class: str, name: str) -> bool:
    return name in (element_class or "").split()


def _strip_index_prefix(text: str) -> str:
    """去掉标题开头的「1.1」编号 —— 只在没有别的地方取编号时兜底用。"""
    return _INDEX_PREFIX_RE.sub("", text or "", count=1).strip()


@dataclass(slots=True)
class Tab:
    """一个小节下面的 tab（课件 / 测验 / 扩展资源）。

    ``index`` 取 ``changeDisplayContent`` 的第一个参数（真实数据从 1 开始）；
    没有这个参数时按同一父节点里的出现次序从 1 编号。
    """

    kind: str
    index: int
    card_id: str
    title: str


@dataclass(slots=True)
class Section:
    """目录树里的一个小节（可能嵌套，如 10.3 下面还有 10.3.1）。"""

    chapter_id: str
    title: str
    index_text: str
    state: str
    tabs: list[Tab] = field(default_factory=list)
    chapter_title: str = ""


class CourseTree:
    """一份课程目录的解析结果。

    ``chapters`` 是解析时按出现顺序记录的章名（含没有小节的章），
    ``sections`` 是按文档顺序展开的小节（含嵌套小节）。
    """

    __slots__ = ("sections", "chapters")

    def __init__(
        self,
        sections: list[Section] | None = None,
        chapters: list[str] | None = None,
    ) -> None:
        self.sections: list[Section] = list(sections or [])
        self.chapters: list[str] = list(chapters or [])

    def chapter_names(self) -> list[str]:
        """按出现顺序返回去重后的章名；章名缺失时退回小节上的章名。"""
        names = list(dict.fromkeys(name for name in self.chapters if name))
        for section in self.sections:
            if section.chapter_title and section.chapter_title not in names:
                names.append(section.chapter_title)
        return names

    def by_id(self, chapter_id: str) -> Section | None:
        """按小节 id 查小节；找不到返回 ``None``（不是抛异常）。"""
        for section in self.sections:
            if section.chapter_id == chapter_id:
                return section
        return None

    def iter_task_points(self) -> Iterator[Section]:
        """只产出带 tab 的小节 —— 没有 tab 的小节没有可刷的任务点。"""
        for section in self.sections:
            if section.tabs:
                yield section

    def __len__(self) -> int:
        return len(self.sections)

    def __iter__(self) -> Iterator[Section]:
        return iter(self.sections)


class _Element:
    """开标签栈里的一条记录：只留解析要用的字段。"""

    __slots__ = ("tag", "attrs", "element_class", "depth", "kind", "captures", "tab_count")

    def __init__(self, tag: str, attrs: dict[str, str], depth: int) -> None:
        self.tag = tag
        self.attrs = attrs
        self.element_class = attrs.get("class", "")
        self.depth = depth
        #: "chapter_unit" / "chapter_header" / "section" / "tab"，普通元素为空串
        self.kind = ""
        self.captures: list[_Capture] = []
        self.tab_count = 0


class _Capture:
    """一段正在收集文本的节点；``owner`` 是最终要写进去的草稿/元素。"""

    __slots__ = ("kind", "buffer", "owner")

    def __init__(self, kind: str, owner: object) -> None:
        self.kind = kind
        self.buffer: list[str] = []
        self.owner = owner

    def text(self) -> str:
        return _clean("".join(self.buffer))


class _SectionDraft:
    """正在解析的小节：Section 可能还没收齐标题/状态，先攒在草稿里。"""

    __slots__ = ("section", "depth", "state_classes", "pending", "has_pending_marker", "name_text")

    def __init__(self, section: Section, depth: int) -> None:
        self.section = section
        self.depth = depth
        self.state_classes: list[str] = []
        self.pending = 0
        self.has_pending_marker = False
        self.name_text = ""


class _TabDraft:
    """正在解析的 tab：``span.spanText`` 的文本要等标签闭合才知道。"""

    __slots__ = ("tab", "chapter_id", "depth", "span_text", "title_text")

    def __init__(self, tab: Tab, chapter_id: str, depth: int) -> None:
        self.tab = tab
        self.chapter_id = chapter_id
        self.depth = depth
        #: ``span.spanText`` 的文本：比整段文本可靠（整段会带上 span.num 的序号）
        self.span_text = ""
        self.title_text: list[str] = []


class _TreeParser(HTMLParser):
    """把目录页 HTML 扫成一串 Section / Tab 草稿。

    解析状态全部挂在栈上：``_sections`` 是被打开的 section 栈（支持 10.3 →
    10.3.1 这种嵌套），``_tabs`` 是打开的 tab 栈，``_captures`` 决定当前文本
    该写进哪个字段。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.sections: list[Section] = []
        self.chapters: list[str] = []
        #: ``(chapterId, Tab)``，按文档顺序；章节 id 为空时退化为空串
        self.tabs: list[tuple[str, Tab]] = []
        self._stack: list[_Element] = []
        self._sections: list[_SectionDraft] = []
        self._tabs: list[_TabDraft] = []
        self._captures: list[_Capture] = []
        self._chapter_header_depth: int | None = None
        self._chapter_title = ""
        self._chapter_name_text = ""

    # ---------------------------------------------------------------- 入口
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attr_map = {name.lower(): value or "" for name, value in attrs}
        depth = len(self._stack)

        if tag in _VOID_TAGS:
            # 空元素不压栈，否则后面的 </div> 会把栈弹错位。
            return

        element = _Element(tag, attr_map, depth)

        if _has_class(element.element_class, "chapter_unit"):
            element.kind = "chapter_unit"
            self._chapter_title = ""
            self._chapter_name_text = ""
            self._chapter_header_depth = None
        elif tag == "div" and _has_class(element.element_class, "chapter_item"):
            # 有 id=cur<n> 的是小节；没有 id 但 onclick 是 toOld(...) 的也是小节
            # （合成/降级页面里 id 可能被去掉）；章标题那一行两者都没有。
            if _CUR_ID_RE.match(attr_map.get("id", "")) or "toOld(" in attr_map.get(
                "onclick", ""
            ):
                element.kind = "section"
                self._start_section(element)
            else:
                element.kind = "chapter_header"
                self._chapter_header_depth = depth
        elif tag == "li" and (
            "cardid" in attr_map or "changeDisplayContent" in attr_map.get("onclick", "")
        ):
            element.kind = "tab"
            self._start_tab(element)

        self._stack.append(element)

        if element.kind == "tab":
            return

        if _has_class(element.element_class, "catalog_sbar") and self._sections:
            self._push_capture(element, "section_index", self._sections[-1])
        elif _has_class(element.element_class, "catalog_name"):
            if self._sections:
                self._push_capture(element, "section_name", self._sections[-1])
            elif self._chapter_header_depth is not None and depth > self._chapter_header_depth:
                self._push_capture(element, "chapter_name", None)
        elif _has_class(element.element_class, "spanText") and self._tabs:
            self._push_capture(element, "tab_title", self._tabs[-1])
        elif (
            tag == "span"
            and _has_class(element.element_class, "catalog_points_yi")
            and self._sections
        ):
            self._sections[-1].has_pending_marker = True
            self._push_capture(element, "pending_count", self._sections[-1])
        elif tag == "span" and _has_class(element.element_class, "bntHoverTips") and self._sections:
            self._sections[-1].has_pending_marker = True
            self._push_capture(element, "pending_tip", self._sections[-1])

        if _has_class(element.element_class, "catalog_jindu") and self._sections:
            self._sections[-1].has_pending_marker = True
        if (
            _has_class(element.element_class, "catalog_state")
            or _has_class(element.element_class, "icon_yiwanc")
            or _has_class(element.element_class, "icon_weiwanc")
        ) and self._sections:
            self._sections[-1].state_classes.append(element.element_class)

        if (
            tag == "span"
            and "title" in attr_map
            and self._chapter_header_depth is not None
            and depth > self._chapter_header_depth
            and not self._sections
        ):
            self._chapter_title = _clean(attr_map["title"])

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # XHTML 里 <input ... /> 会走这里；空元素照旧不压栈。
        if tag.lower() not in _VOID_TAGS:
            self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _VOID_TAGS:
            return
        # 容忍未闭合标签：一直弹到同名元素为止；弹不到就忽略。
        while self._stack:
            element = self._stack.pop()
            self._finish_element(element)
            if element.tag == tag:
                return

    def handle_data(self, data: str) -> None:
        if not data:
            return
        if self._captures:
            self._captures[-1].buffer.append(data)
        elif self._tabs:
            self._tabs[-1].title_text.append(data)

    def finish(self) -> None:
        """收尾：正常文档走不到，畸形 HTML 里未闭合的小节/tab 也不能丢。"""
        while self._tabs:
            self._finish_tab()
        while self._sections:
            self._finish_section(self._sections.pop())

    # ------------------------------------------------------------ 小节/章
    def _start_section(self, element: _Element) -> None:
        attrs = element.attrs
        chapter_id = ""
        match = _CUR_ID_RE.match(attrs.get("id", ""))
        if match:
            chapter_id = match.group(1)
        if not chapter_id:
            match = _TO_OLD_RE.search(attrs.get("onclick", ""))
            if match:
                chapter_id = match.group(2)
        section = Section(
            chapter_id=chapter_id,
            title=_clean(attrs.get("title", "")),
            index_text="",
            state="unknown",
            chapter_title=self._chapter_title,
        )
        self.sections.append(section)
        self._sections.append(_SectionDraft(section, element.depth))

    def _finish_section(self, draft: _SectionDraft) -> None:
        section = draft.section
        if not section.title:
            section.title = _strip_index_prefix(_clean(draft.name_text))
        if not section.index_text:
            match = _INDEX_PREFIX_RE.match(section.title)
            if match:
                section.index_text = match.group(1)
        classes = " ".join(draft.state_classes)
        if "icon_yiwanc" in classes:
            section.state = "done"
        elif "icon_weiwanc" in classes or draft.has_pending_marker or draft.pending > 0:
            section.state = "todo"
        else:
            section.state = "unknown"

    def _finish_chapter_header(self) -> None:
        if not self._chapter_title:
            self._chapter_title = _strip_index_prefix(_clean(self._chapter_name_text))
        if self._chapter_title and self._chapter_title not in self.chapters:
            self.chapters.append(self._chapter_title)
        self._chapter_header_depth = None
        self._chapter_name_text = ""

    # ----------------------------------------------------------------- tab
    def _start_tab(self, element: _Element) -> None:
        attrs = element.attrs
        match = _DISPLAY_RE.search(attrs.get("onclick", ""))
        chapter_id = match.group(3) if match else ""
        if match:
            index = int(match.group(1))
        else:
            parent = self._stack[-1] if self._stack else None
            parent.tab_count += 1
            index = parent.tab_count
        if not chapter_id and self._sections:
            chapter_id = self._sections[-1].section.chapter_id
        tab = Tab(
            kind="unknown",
            index=index,
            card_id=attrs.get("cardid", ""),
            title=_clean(attrs.get("title", "")),
        )
        self._tabs.append(_TabDraft(tab, chapter_id, element.depth))

    def _finish_tab(self) -> None:
        draft = self._tabs.pop()
        tab = draft.tab
        if not tab.title:
            tab.title = draft.span_text or _clean("".join(draft.title_text))
        tab.kind = _TAB_KIND_BY_TITLE.get(tab.title, "unknown")
        self.tabs.append((draft.chapter_id, tab))

    # -------------------------------------------------------------- 捕获
    def _push_capture(self, element: _Element, kind: str, owner: object) -> None:
        capture = _Capture(kind, owner)
        element.captures.append(capture)
        self._captures.append(capture)

    def _finish_captures(self, element: _Element) -> None:
        for capture in element.captures:
            if capture in self._captures:
                self._captures.remove(capture)
            self._assign_capture(capture)

    def _assign_capture(self, capture: _Capture) -> None:
        text = capture.text()
        owner = capture.owner
        if capture.kind == "section_index":
            if isinstance(owner, _SectionDraft) and not owner.section.index_text:
                owner.section.index_text = text
        elif capture.kind == "section_name":
            if isinstance(owner, _SectionDraft):
                owner.name_text = text
        elif capture.kind == "chapter_name":
            # 章标题兜底：span[title] 缺失时才用（有 title 时直接写 _chapter_title）
            self._chapter_name_text = text
        elif capture.kind == "pending_count":
            if isinstance(owner, _SectionDraft) and text.isdigit():
                owner.pending = max(owner.pending, int(text))
        elif capture.kind == "pending_tip":
            match = _PENDING_TIP_RE.search(text)
            if isinstance(owner, _SectionDraft) and match:
                owner.pending = max(owner.pending, int(match.group(1)))
        elif capture.kind == "tab_title" and isinstance(owner, _TabDraft):
            owner.span_text = text

    # ------------------------------------------------------------- 收尾
    def _finish_element(self, element: _Element) -> None:
        self._finish_captures(element)
        kind = element.kind
        if kind == "section" and self._sections:
            self._finish_section(self._sections.pop())
        elif kind == "chapter_header":
            self._finish_chapter_header()
        elif kind == "tab" and self._tabs:
            self._finish_tab()


def _attach_tabs(sections: list[Section], tabs: list[tuple[str, Tab]]) -> None:
    by_id = {section.chapter_id: section for section in sections if section.chapter_id}
    for chapter_id, tab in tabs:
        section = by_id.get(chapter_id)
        if section is not None:
            section.tabs.append(tab)


def parse_chapter_tree(html: str) -> CourseTree:
    """解析目录页 HTML，返回 :class:`CourseTree`。

    纯函数：不联网、不依赖浏览器，畸形 HTML 也不抛异常（没解析到的部分为空）。
    同一份 HTML 里如果带小节 tab 条（``li[cardid]``），会按
    ``changeDisplayContent`` 的第三个小节 id 挂到对应小节的 ``tabs`` 上。
    """
    parser = _TreeParser()
    parser.feed(html or "")
    parser.close()
    parser.finish()
    _attach_tabs(parser.sections, parser.tabs)
    return CourseTree(parser.sections, parser.chapters)


def parse_tabs(html: str) -> list[Tab]:
    """解析小节 tab 条 HTML 片段，按文档顺序返回 :class:`Tab` 列表。

    用于学习页里单独渲染的 ``div#prev_tab > ul > li[cardid]``：那里没有目录树，
    只有当前小节的课件/测验/扩展资源。
    """
    parser = _TreeParser()
    parser.feed(html or "")
    parser.close()
    parser.finish()
    return [tab for _chapter_id, tab in parser.tabs]
