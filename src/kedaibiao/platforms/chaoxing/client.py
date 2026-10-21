"""超星 / 学习通 只读客户端：入口探测 → 拉取 → 解析。

## 为什么单独一层

CLI（``scripts/chaoxing_peek.py``）和网页看板（``scripts/chaoxing_web.py``）
必须走**同一条**取数路径。各写一遍的结果是「命令行读得到、页面读不到」，
而这类问题最难查 —— 两边都「没报错」。

## 只读

本模块只有 GET。没有 POST、没有提交、没有进度上报。要加写操作，
那是另一个模块的事，不该混进「看看有什么作业」这条链路里。
"""

from __future__ import annotations

import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from kedaibiao.core.models import Assignment, Course
from kedaibiao.platforms.chaoxing import endpoints, parsing

#: 桌面版 UA。超星对 UA 有一定敏感性，用默认的 ``Python-urllib`` 容易拿到
#: 精简页甚至被拦 —— 这里不是伪装，只是别让客户端标识本身成为异常信号。
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


class FetchError(RuntimeError):
    """网络层失败（连不上 / 超时 / 非 HTTP）。"""


def fetch(url: str, cookie: str, timeout: float = 20.0) -> tuple[int, str]:
    """发一个只读 GET，返回 ``(status, body)``；网络失败返回 ``(0, 说明)``。"""
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Cookie": cookie,
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": "https://i.chaoxing.com/",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            body = response.read()
            charset = response.headers.get_content_charset() or "utf-8"
            return response.status, body.decode(charset, "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return 0, f"<network-error>{exc}</network-error>"


def _dump(directory: Path, name: str, body: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(body, encoding="utf-8")


def load_courses(
    cookie: str,
    timeout: float = 20.0,
    dump_dir: Path | None = None,
) -> tuple[list[Course], list[str]]:
    """按候选顺序试课程列表入口，返回 ``(课程, 每条入口的结果说明)``。"""
    notes: list[str] = []
    for index, url in enumerate(endpoints.COURSE_LIST_URLS, start=1):
        status, body = fetch(url, cookie, timeout)
        if dump_dir is not None:
            _dump(dump_dir, f"courses-{index}.html", body)
        if status == 0:
            notes.append(f"[{index}] 网络失败: {body}")
            continue
        if parsing.looks_like_login(body, endpoints.LOGIN_MARKERS):
            notes.append(f"[{index}] 登录态无效（返回登录页）")
            continue
        courses = parsing.parse_courses(body)
        if courses:
            notes.append(f"[{index}] 解析出 {len(courses)} 门课")
            return courses, notes
        notes.append(f"[{index}] HTTP {status}，解析出 0 门课")
    return [], notes


def load_works(
    course: Course,
    cookie: str,
    timeout: float = 20.0,
    dump_dir: Path | None = None,
) -> tuple[list[Assignment], str]:
    """拉一门课的作业/考试列表，返回 ``(条目, 失败说明)``。"""
    raw = course.raw
    params: dict[str, Any] = {
        "courseid": raw.get("courseid", ""),
        "clazzid": raw.get("clazzid", ""),
        "cpi": raw.get("cpi", ""),
        "enc": raw.get("enc", ""),
    }
    if not params["courseid"]:
        return [], "缺少 courseid"
    note = ""
    safe = parsing.clean(course.name).replace("/", "_")[:40]
    for index, template in enumerate(endpoints.WORK_LIST_URL_TEMPLATES, start=1):
        url = template.format(**params)
        status, body = fetch(url, cookie, timeout)
        if dump_dir is not None:
            _dump(dump_dir, f"works-{safe}-{index}.html", body)
        if status == 0:
            note = "网络失败"
            continue
        works = parsing.parse_works(body, course_name=course.name)
        if works:
            return works, ""
        if parsing.looks_like_login(body, endpoints.LOGIN_MARKERS):
            note = "登录态无效"
            continue
        note = f"HTTP {status}，解析出 0 条"
    return [], note


def collect(
    cookie: str,
    *,
    timeout: float = 20.0,
    dump_dir: Path | None = None,
    include_works: bool = True,
) -> dict[str, Any]:
    """一次取全：课程 + 每门课的作业/考试。返回可直接 JSON 化的结构。"""
    courses, notes = load_courses(cookie, timeout, dump_dir)
    payload: dict[str, Any] = {
        "ok": bool(courses),
        "notes": notes,
        "courses": [
            {
                "name": course.name,
                "courseid": course.raw.get("courseid"),
                "clazzid": course.raw.get("clazzid"),
            }
            for course in courses
        ],
        "works": [],
    }
    if not courses or not include_works:
        return payload

    for course in courses:
        works, note = load_works(course, cookie, timeout, dump_dir)
        payload["works"].append(
            {
                "course": course.name,
                "note": note,
                "items": [
                    {
                        "id": work.remote_id,
                        "title": work.title,
                        "kind": work.kind.value,
                        "deadline": work.deadline.isoformat() if work.deadline else None,
                        "finished": work.finished,
                        "url": work.url,
                    }
                    for work in works
                ],
            }
        )
    return payload
