"""品课富文本（题干 / 选项 HTML）处理。

实测形态：题干与选项都是 HTML 片段，图片直接嵌在里面 ——

- 真图：``<img id="rich-img-…" class="rich-img" src="https://cstore-public.seewo.com/…"/>``
- 公式：``<img id="rich-formula-…" class="rich-formula" src="…"/>``

为什么要区分这两类：**它们都是图片**，纯文本模型都读不到，所以都会让题目进人工；
但提示语要说清是哪一种，复核的人才知道自己要看什么。

为什么用标准库 ``html.parser`` 而不是正则去标签：题干里会出现数学表达式与带属性的
标签，正则容易切坏；解析器按标签边界走，稳。
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

#: 这些标签前后要断行，否则 ``</p><p>`` 会把两段文字粘在一起。
_BLOCK_TAGS = frozenset(
    {
        "p",
        "div",
        "br",
        "li",
        "tr",
        "td",
        "th",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "section",
        "article",
        "blockquote",
        "table",
    }
)

_TAG_RE = re.compile(r"<[^>]+>")
_BLANK_LINES_RE = re.compile(r"\n{2,}")
_SPACES_RE = re.compile(r"[ \t]{2,}")

#: 品课富文本里两类图片的 class 标记（实测）。
_IMAGE_CLASS = "rich-img"
_FORMULA_CLASS = "rich-formula"


class _TextExtractor(HTMLParser):
    """把 HTML 抽成纯文本：丢标签，块级标签换行。只取文字，不碰属性。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    @property
    def text(self) -> str:
        return "".join(self._parts)


def strip_html(text: str) -> str:
    """HTML → 纯文本（保留段落换行）。

    纯文本快路径：没有 ``<`` 就直接返回，省掉一次解析。
    """
    if not text:
        return ""
    if "<" not in text:
        return text.replace("\u00a0", " ").strip()

    parser = _TextExtractor()
    try:
        parser.feed(text)
        parser.close()
        raw = parser.text
    except Exception:  # noqa: BLE001 - 解析器出错也不能让整批解析挂掉
        raw = _TAG_RE.sub("", text)

    cleaned = raw.replace("\u00a0", " ")
    cleaned = _BLANK_LINES_RE.sub("\n", cleaned)
    cleaned = _SPACES_RE.sub(" ", cleaned)
    return cleaned.strip()


def image_kind(html: str) -> str:
    """HTML 里的图片形态：``"image"`` / ``"formula"`` / ``""``（没有图）。

    两类图片都算「题干里有程序读不到的东西」，所以调用方对它们一视同仁地转人工；
    返回值只用来决定提示语怎么写。
    """
    if not html or "<img" not in html.lower():
        return ""
    lowered = html.lower()
    if _IMAGE_CLASS in lowered:
        return "image"
    if _FORMULA_CLASS in lowered:
        return "formula"
    return "image"
