"""超星 / 学习通页面解析（纯函数，可离线测试）。

解析只依赖标准库的 :mod:`html.parser`，不引第三方 —— 与核心层「零依赖」
的约定一致，也保证单测不需要网络。

## 为什么这么「宽容」

超星不同端、不同灰度版本给的是不同的 HTML 片段：卡片可能叫 ``course``、
可能是 ``course-item``；作业行可能是 ``<li>``、可能是 ``<tr>``。这里不写死
具体 class，而是按**语义特征**认：

- 课程 = 链接里带 ``courseid`` 的锚点；
- 作业行 = 含作业/考试关键词、或链接里带 ``work``/``exam``、且有日期的行。

宁可多认一两个，也不要因为改版就整页解析成空 —— 空列表在这里是危险信号
（会被误读成「没有作业」），所以 :func:`parse_works` 会在什么都没认出来时
保留原始文本摘要，供 ``--dump`` 对照。
"""

from __future__ import annotations

import contextlib
import hashlib
import html as html_lib
import re
from datetime import datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, urlparse

from kedaibiao.core.models import Assignment, Course, TaskKind

_WS = re.compile(r"[ \t\r\n\u00a0]+")
_DATE = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})(?:[ T](\d{1,2}):(\d{2}))?")
_LI = re.compile(r"<li\b[^>]*>(.*?)</li>", re.S | re.I)
_SCRIPT = re.compile(r"<(script|style)\b.*?</\1>", re.S | re.I)
_TAG = re.compile(r"<[^>]+>")

#: 作业/考试行的语义特征
_WORK_WORDS = ("作业", "考试", "测验", "未交", "待交", "已完成", "已交")
_EXAM_WORDS = ("考试", "测验")


def clean(text: str) -> str:
    """去标签残留的空白、反转义实体、压缩空白。"""
    return _WS.sub(" ", html_lib.unescape(text or "")).strip()


def strip_tags(block: str) -> str:
    return clean(_TAG.sub(" ", _SCRIPT.sub(" ", block)))


class _AnchorCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.anchors: list[dict[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self._href = dict(attrs).get("href") or ""
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href is not None:
            self.anchors.append({"href": self._href, "text": clean(" ".join(self._text))})
            self._href = None


def anchors(html: str) -> list[dict[str, str]]:
    """抽出所有 ``<a>`` 的 href 与可见文本。"""
    collector = _AnchorCollector()
    # 半截 HTML 也能拿到已解析的部分；解析器不该因为一个坏标签整页报废
    with contextlib.suppress(Exception):
        collector.feed(html or "")
    return collector.anchors


def _first(query: dict[str, list[str]], name: str) -> str:
    values = query.get(name) or [""]
    return (values[0] or "").strip()


def parse_courses(html: str) -> list[Course]:
    """从课程列表片段里抽出 :class:`Course`。

    识别规则：锚点链接的 query 里带 ``courseid``。同一门课在卡片里可能出现
    两次（封面 + 标题各一个 ``<a>``），按 ``(courseid, clazzid)`` 去重。
    """
    out: list[Course] = []
    seen: set[tuple[str, str]] = set()
    for anchor in anchors(html):
        query = parse_qs(urlparse(anchor["href"]).query)
        courseid = _first(query, "courseid") or _first(query, "courseId")
        if not courseid:
            continue
        clazzid = _first(query, "clazzid") or _first(query, "classId")
        key = (courseid, clazzid)
        if key in seen:
            continue
        name = anchor["text"]
        if not name:
            continue
        seen.add(key)
        out.append(
            Course(
                remote_id=f"{courseid}:{clazzid}",
                name=name,
                raw={
                    "courseid": courseid,
                    "clazzid": clazzid,
                    "enc": _first(query, "enc"),
                    "cpi": _first(query, "cpi"),
                    "href": anchor["href"],
                },
            )
        )
    return out


def _parse_deadline(text: str) -> datetime | None:
    matches = list(_DATE.finditer(text))
    if not matches:
        return None
    # 截止时间通常是行里最后一个日期
    match = matches[-1]
    year, month, day, hour, minute = match.groups()
    try:
        return datetime(
            int(year), int(month), int(day), int(hour or 0), int(minute or 0)
        )
    except ValueError:
        return None


def _is_work_row(block_text: str, href: str) -> bool:
    if any(word in block_text for word in _WORK_WORDS):
        return True
    lowered = href.lower()
    return "work" in lowered or "exam" in lowered


def _guess_kind(title: str, href: str) -> TaskKind:
    if any(word in title for word in _EXAM_WORDS):
        return TaskKind.EXAM
    return TaskKind.EXAM if "exam" in href.lower() else TaskKind.HOMEWORK


def _work_id(href: str, title: str, deadline: datetime | None) -> str:
    query = parse_qs(urlparse(href).query)
    for key in ("workId", "workid", "examId", "examid", "taskId", "id"):
        value = _first(query, key)
        if value:
            return value
    digest = hashlib.sha256(
        f"{title}|{deadline.isoformat() if deadline else ''}".encode()
    ).hexdigest()
    return "h" + digest[:15]


def parse_works(html: str, *, course_name: str = "") -> list[Assignment]:
    """从作业/考试列表片段里抽出 :class:`Assignment`。

    行按 ``<li>`` 切分（超星两种版本都用 ``li`` 承载一行）；每行取第一个
    锚点当标题与链接，末尾日期当截止时间。认不出任何行时返回空列表 ——
    调用方应当同时保留 ``--dump`` 的原始页面，因为「真没作业」和「改版了」
    在这里长得一模一样。
    """
    out: list[Assignment] = []
    seen: set[str] = set()
    for block in _LI.findall(html or ""):
        text = strip_tags(block)
        row_anchors = anchors(block)
        href = row_anchors[0]["href"] if row_anchors else ""
        title = (row_anchors[0]["text"] if row_anchors else "") or text[:40]
        if not title or not _is_work_row(text, href):
            continue
        deadline = _parse_deadline(text)
        remote_id = _work_id(href, title, deadline)
        if remote_id in seen:
            continue
        seen.add(remote_id)
        finished = "已完成" in text or "已交" in text
        out.append(
            Assignment(
                remote_id=remote_id,
                title=title,
                kind=_guess_kind(title, href),
                course_name=course_name,
                deadline=deadline,
                finished=finished,
                url=href,
                raw={"text": text[:200]},
            )
        )
    return out


def looks_like_login(html: str, markers: tuple[str, ...]) -> bool:
    """判断拿到的页面是不是登录页。

    为什么需要：登录态失效时返回的登录页同样解析出 0 条课程，和「这个账号
    确实没有课」无法区分。两者处理方式相反 —— 前者要重新绑定账号，后者是
    正常结果 —— 所以必须单独判一次。
    """
    lowered = (html or "").lower()
    return any(marker.lower() in lowered for marker in markers)


def summarise(html: str, limit: int = 300) -> dict[str, Any]:
    """解析失败时给人看的摘要：标题 + 可见文本前 N 字。"""
    title = ""
    match = re.search(r"<title[^>]*>(.*?)</title>", html or "", re.S | re.I)
    if match:
        title = clean(match.group(1))
    return {"title": title, "text": strip_tags(html)[:limit]}
