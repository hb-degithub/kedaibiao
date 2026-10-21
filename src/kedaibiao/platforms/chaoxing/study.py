"""学习通刷课引擎：课程 → 章节树 → 任务点 → 视频/文档（页面驱动）。

## 为什么是「页面驱动」而不是打接口

学习通的视频进度由播放器自己按 ``reportTimeInterval``（实测 60 秒）向
``/mooc-ans/multimedia/log/...`` 上报，报文里带 ``enc``、``attDurationEnc``、
``videoFaceCaptureEnc`` 这些**页面生成**的签名。自己拼请求等价于伪造上报，
签名一变就全错。所以这里的做法是：把播放器开在页面上，让它自己上报，
最后**回读**平台记账结果（``mArg.attachments[].isPassed`` + 章节树状态）。

## 实测结论（2026-10-21，真实账号）

- 一个「课件」tab 里可以挂**多个**附件，页面为每个附件渲染一个播放器 iframe；
  只播第一个不算完成 —— 必须逐个 iframe 播完，``isPassed`` 才会变成 ``true``；
- 播放器 iframe 是懒初始化的：不滚动到可视区、不点播放，``<video>`` 的
  ``duration`` 一直是 ``NaN``；
- 倍速白名单最高 2x（设 8x 会被打回 1x），播放位置按真实进度记账；
- 个别视频带「随机人脸采集」（``randomFaceCaptureTimeList``），到点不采集可能
  不计入完成 —— 这种交回人工，不硬闯。

## 流程

1. :meth:`ChaoxingStudy.open_course`：进课程页 → 拿 ``enc``；
2. :meth:`ChaoxingStudy.task_points`：章节树 × ``knowledge/cards`` 的 ``mArg``
   （每个附件一个任务点，带 ``jobid``）；
3. :meth:`ChaoxingStudy.play_video`：定位到 ``jobid`` 对应的 iframe → 滚入可视区
   → 等 ``duration`` 可读 → 播放（2x、静音）→ 轮询到片尾 → 回读 ``isPassed``；
4. :meth:`ChaoxingStudy.read_document`：打开文档附件并停留、翻页；
5. :meth:`ChaoxingStudy.refresh_tree`：回读章节树状态（``icon_yiwanc`` = 已完成）。

## 边界

- 人脸采集/弹题：检测到就返回 ``needs_human``，由控制台提示人工；
- 测验（``work``）任务点只列出来，作答走 quiz 链路；
- 回读是唯一判据：播放器说「播完了」但 ``isPassed`` 没变，如实报 ``partial``。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from kedaibiao.browser.pw import PwError, PwPage
from kedaibiao.platforms.chaoxing import tree as tree_mod

log = logging.getLogger(__name__)

#: 课程页（旧入口，会 302 到 mooc2-ans 的新版学习页）
COURSE_URL = (
    "https://mooc1.chaoxing.com/visit/stucoursemiddle"
    "?courseid={courseid}&clazzid={clazzid}&cpi={cpi}&ismooc2=1&v=2"
)
#: 章节树（服务端渲染，可带 stuenc 直接 GET）
TREE_URL = (
    "https://mooc2-ans.chaoxing.com/mooc2-ans/mycourse/studentcourse"
    "?courseid={courseid}&clazzid={clazzid}&cpi={cpi}&ut=s&t={ts}&stuenc={enc}"
)
#: 课件页（num = tab 序号 - 1；``mArg`` 与播放器 iframe 都从这里来）
CARDS_URL = (
    "https://mooc1.chaoxing.com/mooc-ans/knowledge/cards"
    "?clazzid={clazzid}&courseid={courseid}&knowledgeid={chapter}&num={num}"
    "&ut=s&cpi={cpi}&v=2025-0424-1038-4&mooc2=1&isMicroCourse=false"
    "&editorPreview=0&crossId="
)

#: 播放器 iframe 的 URL 特征
VIDEO_FRAME = "modules/video"
#: 文档阅读器 iframe 的 URL 特征
DOC_FRAME_MARKERS = ("modules/pdf", "pan-yz.chaoxing.com/screen", "modules/doc")
#: 请求页面时带上的 Referer：不带会被当外链请求，返回空壳
_REFERER = {"Referer": "https://mooc2-ans.chaoxing.com/mooc2-ans/mycourse/stu"}

#: 播放器里「需要人处理」的弹层关键词
_HUMAN_MARKERS = ("人脸", "摄像头", "拍照", "采集", "验证是否为本人")
#: 可以自动点掉的「继续」类按钮
_CONTINUE_LABELS = ("继续学习", "继续播放", "知道了", "我知道了", "继续")

#: 视频完成判定：播放位置到片尾多少秒内算「播完」
_VIDEO_TAIL = 2.0
#: 轮询间隔下限（秒）
_MIN_POLL = 0.5
#: 等播放器 iframe 初始化 + 等 isPassed 回读的超时（秒）
_FRAME_WAIT = 60.0
_PASS_WAIT = 60.0

#: 播放器操作脚本（``RATE`` 占位符由调用方替换成 JSON 数字）
_PLAY_JS = (
    "(() => {"
    " const v = document.querySelector('video');"
    " if (!v) return null;"
    " v.muted = true; v.playbackRate = RATE;"
    " v.play().catch(() => {});"
    " return {duration: v.duration, currentTime: v.currentTime, paused: v.paused};"
    "})()"
)
#: 播放器状态轮询脚本
_SNAPSHOT_JS = (
    "(() => {"
    " const rate = RATE;"
    " const v = document.querySelector('video');"
    " if (!v) return {gone: true};"
    " if (!v.paused && Math.abs(v.playbackRate - rate) > 0.01) v.playbackRate = rate;"
    " const pick = '.popup, .mask, [class*=question-pop], [class*=capture],"
    " [class*=face], .ans-video-question';"
    " const overlays = Array.from(document.querySelectorAll(pick)).filter(el =>"
    " (el.offsetWidth || 0) > 40 && (el.offsetHeight || 0) > 20);"
    " return {currentTime: v.currentTime, duration: v.duration, paused: v.paused,"
    " ended: v.ended, rate: v.playbackRate,"
    " overlays: overlays.slice(0, 3).map(el => ({"
    "   cls: (el.className || '').toString().slice(0, 60),"
    "   text: (el.innerText || '').replace(/\\s+/g, ' ').slice(0, 120)}))};"
    "})()"
)


@dataclass(slots=True)
class TaskPoint:
    """一个任务点 = 课件页里的一个附件（视频/文档/测验）。"""

    chapter_id: str
    section_title: str
    index_text: str
    kind: str  # video | document | quiz | image | unknown
    module: str = ""
    name: str = ""
    duration: int = 0
    #: 附件在平台侧的 jobid（播放器 iframe 的 data 属性里也有它）
    jobid: str = ""
    #: 附件级完成标记（``mArg.attachments[].isPassed``），None = 平台没给
    passed: bool | None = None
    #: 章节树里的状态：done / todo / unknown
    state: str = "unknown"
    #: 原始 mArg.attachments 条目（播放器参数都在里面）
    attachment: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.chapter_id}:{self.jobid or self.name}"

    @property
    def is_video(self) -> bool:
        return self.kind == "video"

    @property
    def is_document(self) -> bool:
        return self.kind == "document"

    @property
    def is_quiz(self) -> bool:
        return self.kind == "quiz"

    def label(self) -> str:
        prefix = f"{self.index_text} " if self.index_text else ""
        name = self.name or self.section_title
        if self.duration:
            minutes, seconds = divmod(self.duration, 60)
            return f"{prefix}{name}（{minutes} 分 {seconds} 秒）"
        return f"{prefix}{name}"


@dataclass(slots=True)
class StepResult:
    """一个任务点的执行结果。"""

    task: TaskPoint
    status: str  # done | skipped | failed | needs_human | partial
    detail: str = ""
    seconds: float = 0.0
    #: 播放器里读到的进度（0~1）
    progress: float = 0.0

    def summary(self) -> str:
        return f"[{self.status}] {self.task.label()} {self.detail}".strip()


@dataclass(slots=True)
class StudyReport:
    course_id: str
    steps: list[StepResult] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0

    def add(self, step: StepResult) -> StepResult:
        self.steps.append(step)
        return step

    def count(self, status: str) -> int:
        return sum(1 for step in self.steps if step.status == status)

    @property
    def done(self) -> int:
        return self.count("done")

    def summary(self) -> str:
        elapsed = (self.finished_at or time.time()) - self.started_at
        parts = [f"共 {len(self.steps)} 个任务点"]
        labels = (
            ("done", "完成"),
            ("skipped", "跳过"),
            ("partial", "部分"),
            ("needs_human", "待人工"),
            ("failed", "失败"),
        )
        for status, label in labels:
            value = self.count(status)
            if value:
                parts.append(f"{label} {value}")
        parts.append(f"耗时 {elapsed / 60:.1f} 分钟")
        return "，".join(parts)


def _marg(html: str) -> dict[str, Any] | None:
    """从课件页 HTML 里取 ``mArg``（页面里先有一个 ``mArg = ""`` 的空串，要跳过）。"""
    decoder = json.JSONDecoder()
    for match in re.finditer(r"mArg\s*=\s*", html or ""):
        try:
            value, _ = decoder.raw_decode(html[match.end():])
        except ValueError:
            continue
        if isinstance(value, dict) and isinstance(value.get("attachments"), list):
            return value
    return None


def playback_limits(marg: dict[str, Any] | None) -> dict[str, Any]:
    """从 ``mArg`` 里取播放器配置（时长、上报地址、人脸采集计划等）。"""
    defaults = (marg or {}).get("defaults") or {}
    attachments = (marg or {}).get("attachments") or []
    first = attachments[0] if attachments else {}
    return {
        "duration": int(first.get("attDuration") or 0),
        "report_url": defaults.get("reportUrl", ""),
        "report_interval": defaults.get("reportTimeInterval", 60),
        "face_capture": bool(defaults.get("playingCapture")),
        "face_times": list(first.get("randomFaceCaptureTimeList") or []),
        "is_passed": bool(first.get("isPassed")),
    }


def module_kind(module: str, atype: str) -> str:
    """把学习通的模块名/类型翻译成任务点类型。"""
    module = (module or "").lower()
    atype = (atype or "").lower()
    if "video" in module or atype in ("video", ".mp4"):
        return "video"
    if "doc" in module or atype in (".pdf", "document", "insertdoc"):
        return "document"
    if "work" in module or atype in ("work", "insertwork"):
        return "quiz"
    if "image" in module or atype in ("image", ".jpg", ".png", ".jpeg"):
        return "image"
    return "unknown"


class ChaoxingStudy:
    """一门课的刷课会话。一个实例对应一个页面（同账号串行跑）。"""

    def __init__(
        self,
        page: PwPage,
        *,
        course_id: str,
        clazz_id: str,
        cpi: str = "",
        emit: Callable[[str, str], None] | None = None,
        rate: float = 2.0,
        poll: float = 2.0,
        human_gate: Callable[[str, str], Any] | None = None,
    ) -> None:
        self.page = page
        self.course_id = str(course_id)
        self.clazz_id = str(clazz_id)
        self.cpi = str(cpi or "")
        self.enc = ""
        #: 播放倍速（学习通白名单最高 2x；设更高会被打回 1x）
        self.rate = max(1.0, min(float(rate or 2.0), 2.0))
        self.poll = max(_MIN_POLL, float(poll or 2.0))
        self._emit = emit or (lambda level, message: log.info("%s", message))
        #: 人脸采集/防挂机需要人工认证时的钩子：``await human_gate(kind, message) -> bool``
        self.human_gate = human_gate
        self._tree: tree_mod.CourseTree | None = None
        self._cards_cache: dict[tuple[str, int], dict[str, Any] | None] = {}

    # -- 事件 -------------------------------------------------------------

    def emit(self, level: str, message: str) -> None:
        """向控制台/CLI 发一条事件；回调异常不影响刷课。"""
        try:
            self._emit(level, message)
        except Exception:  # noqa: BLE001 - 回调坏了不能拖垮主流程
            log.debug("emit 回调异常", exc_info=True)

    # -- 课程与章节 -------------------------------------------------------

    async def open_course(self) -> str:
        """打开课程页，返回 ``enc``（后面所有请求都要它）。"""
        url = COURSE_URL.format(
            courseid=self.course_id, clazzid=self.clazz_id, cpi=self.cpi
        )
        await self.page.goto(url, timeout=60000)
        await self.page.wait_for("span.nav_content", timeout=30000)
        match = re.search(r"enc=([0-9a-f]+)", self.page.url)
        if match:
            self.enc = match.group(1)
        if not self.enc:
            raise PwError("课程页没有拿到 enc（会话可能已失效）")
        self.emit("info", f"课程页已打开（enc={self.enc[:8]}…）")
        return self.enc

    def _tree_url(self) -> str:
        return TREE_URL.format(
            courseid=self.course_id,
            clazzid=self.clazz_id,
            cpi=self.cpi,
            ts=int(time.time() * 1000),
            enc=self.enc,
        )

    async def refresh_tree(self) -> tree_mod.CourseTree:
        """重新拉章节树（服务端渲染 HTML）并解析。"""
        html = await self.page.fetch_text(self._tree_url(), headers=_REFERER)
        parsed = tree_mod.parse_chapter_tree(html)
        if not parsed.sections:
            raise PwError("章节树为空：会话可能已失效，或课程页 enc 变了")
        self._tree = parsed
        return parsed

    async def _cards(self, chapter_id: str, num: int) -> dict[str, Any] | None:
        """取课件页并解析 ``mArg``（按会话内缓存，枚举/回读会反复用到）。"""
        key = (chapter_id, num)
        if key in self._cards_cache:
            return self._cards_cache[key]
        html = await self.page.fetch_text(
            self._cards_url(chapter_id, num), headers=_REFERER
        )
        marg = _marg(html)
        self._cards_cache[key] = marg
        return marg

    def _cards_url(self, chapter_id: str, num: int) -> str:
        return CARDS_URL.format(
            clazzid=self.clazz_id,
            courseid=self.course_id,
            chapter=quote(str(chapter_id), safe=""),
            num=num,
            cpi=self.cpi,
        )

    async def task_points(self, *, only_pending: bool = True) -> list[TaskPoint]:
        """枚举任务点：章节树 × 每个小节课件页里的附件。

        - 附件来自 ``num=0``（课件）与 ``num=1``（测验）；一门课里测验通常挂在
          第二个 tab，``property.module == "work"``；
        - 每个附件一个任务点：视频带 ``jobid``/时长，文档带名称，测验带 ``workid``。
        """
        course_tree = self._tree or await self.refresh_tree()
        points: list[TaskPoint] = []
        for section in course_tree.sections:
            if only_pending and section.state == "done":
                continue
            for num in (0, 1):
                marg = await self._cards(section.chapter_id, num)
                for attachment in (marg or {}).get("attachments") or []:
                    prop = attachment.get("property") or {}
                    kind = module_kind(
                        str(prop.get("module", "")), str(prop.get("type", ""))
                    )
                    if kind in ("unknown",):
                        continue
                    points.append(
                        TaskPoint(
                            chapter_id=section.chapter_id,
                            section_title=section.title,
                            index_text=section.index_text,
                            kind=kind,
                            module=str(prop.get("module", "")),
                            name=str(prop.get("name") or prop.get("title") or section.title),
                            duration=int(attachment.get("attDuration") or 0),
                            jobid=str(attachment.get("jobid") or ""),
                            passed=attachment.get("isPassed"),
                            state=section.state,
                            attachment=attachment,
                        )
                    )
        return points

    # -- iframe 定位 ------------------------------------------------------

    async def _attachment_frames(self, kinds: tuple[str, ...]) -> list[dict[str, Any]]:
        """列出当前页面上「带附件元数据」的 iframe（``data`` 属性里有 jobid/name）。"""
        markers = {
            "video": (VIDEO_FRAME,),
            "document": DOC_FRAME_MARKERS,
        }
        wanted = tuple(m for kind in kinds for m in markers.get(kind, ()))
        found: list[dict[str, Any]] = []
        for frame in await self.page.frames():
            if not any(marker in frame.url for marker in wanted):
                continue
            data: dict[str, Any] = {}
            with contextlib.suppress(Exception):
                element = await frame.raw.frame_element()
                raw = await element.get_attribute("data")
                if raw:
                    data = json.loads(raw)
                with contextlib.suppress(Exception):
                    await element.scroll_into_view_if_needed()
            found.append(
                {
                    "frame": frame,
                    "jobid": str(data.get("jobid") or data.get("_jobid") or ""),
                    "name": str(data.get("name") or ""),
                    "kind": (
                        "video"
                        if "modules/video" in frame.url
                        else "document"
                    ),
                }
            )
        return found

    async def _frame_for(self, task: TaskPoint, kind: str) -> Any | None:
        """按 jobid（优先）或文件名找到任务点对应的 iframe。"""
        deadline = time.time() + _FRAME_WAIT
        while time.time() < deadline:
            frames = await self._attachment_frames((kind,))
            if kind == "video":
                candidates = [item for item in frames if item["kind"] == "video"]
            else:
                candidates = [item for item in frames if item["kind"] == "document"]
            for item in candidates:
                if task.jobid and item["jobid"] == task.jobid:
                    return item["frame"]
            if not task.jobid:
                for item in candidates:
                    if task.name and item["name"] == task.name:
                        return item["frame"]
            if candidates and not task.jobid:
                return candidates[0]["frame"]
            await asyncio.sleep(1.5)
        return None

    # -- 视频 -------------------------------------------------------------

    async def _open_cards(self, task: TaskPoint, num: int = 0) -> None:
        await self.page.goto(self._cards_url(task.chapter_id, num), timeout=90000)

    async def play_video(
        self,
        task: TaskPoint,
        *,
        on_question: Callable[[PwPage, Any], None] | None = None,
        max_extra: float = 180.0,
    ) -> StepResult:
        """播放一个视频附件，直到片尾，并回读 ``isPassed``。

        - 一个课件页可能有多个播放器 iframe（每个附件一个），必须按 ``jobid``
          定位，否则会反复播第一个；
        - iframe 懒初始化：先滚入可视区再等 ``duration`` 可读；
        - 倍速会被播放器按白名单重置，每轮轮询重设一次。
        """
        started = time.time()
        await self._open_cards(task, 0)
        frame = await self._frame_for(task, "video")
        if frame is None:
            return StepResult(task, "failed", "没有找到该视频的播放器 iframe")
        rate_literal = json.dumps(self.rate)
        state: Any = None
        wait_deadline = time.time() + _FRAME_WAIT
        while time.time() < wait_deadline:
            state = await frame.eval(_PLAY_JS.replace("RATE", rate_literal))
            duration = float((state or {}).get("duration") or 0)
            if state and duration == duration and duration > 0:
                break
            await asyncio.sleep(1.5)
        if not state:
            return StepResult(task, "failed", "播放器里没有 <video> 元素")
        duration = float(state.get("duration") or task.duration or 0)
        if duration != duration:  # NaN
            duration = float(task.duration or 0)
        limit = (duration / self.rate if duration else 600) + max_extra
        deadline = started + limit
        minutes = duration / self.rate / 60 if duration else 0
        self.emit("info", f"▶ {task.label()}（{self.rate:g}x，预计 {minutes:.1f} 分钟）")

        snapshot_script = _SNAPSHOT_JS.replace("RATE", rate_literal)
        last_report = 0.0
        while time.time() < deadline:
            await asyncio.sleep(self.poll)
            snapshot = await frame.eval(snapshot_script)
            if snapshot.get("gone"):
                return StepResult(
                    task,
                    "failed",
                    "播放器消失了（页面被顶掉/会话失效）",
                    time.time() - started,
                )
            overlays = [
                item for item in snapshot.get("overlays") or [] if item.get("text")
            ]
            human_text = next(
                (
                    item["text"]
                    for item in overlays
                    if any(mark in item["text"] for mark in _HUMAN_MARKERS)
                ),
                "",
            )
            if human_text:
                handled = await self._ask_human(
                    "face",
                    f"播放器要求人工认证：{human_text[:40]}（请在可见窗口完成，然后继续）",
                )
                if handled:
                    self.emit("ok", "人工认证已完成，继续播放")
                    await self._resume(frame)
                    continue
                return StepResult(
                    task,
                    "needs_human",
                    f"播放器要求人工处理：{human_text[:40]}",
                    time.time() - started,
                )
            if overlays:
                handled = await self._handle_overlay(frame, on_question)
                if not handled:
                    return StepResult(
                        task,
                        "needs_human",
                        f"播放器有弹层：{overlays[0].get('text', '')[:40]}",
                        time.time() - started,
                    )
            if snapshot.get("paused") and not snapshot.get("ended"):
                await self._resume(frame)
            position = float(snapshot.get("currentTime") or 0)
            ended = bool(snapshot.get("ended")) or (
                duration > 0 and position >= duration - _VIDEO_TAIL
            )
            if ended:
                return await self._finish_video(task, started, position, duration)
            now = time.time()
            if now - last_report >= 60:
                last_report = now
                ratio = position / duration if duration else 0
                self.emit(
                    "info",
                    f"  {task.name[:18]} {position / 60:.1f}/{duration / 60:.1f} 分钟"
                    f"（{ratio:.0%}）",
                )
        return StepResult(
            task,
            "partial",
            f"到上限 {limit / 60:.0f} 分钟仍未播完",
            time.time() - started,
        )

    async def _finish_video(
        self, task: TaskPoint, started: float, position: float, duration: float
    ) -> StepResult:
        """播放到片尾后等平台记账（``isPassed``），并如实报告结果。"""
        elapsed = time.time() - started
        passed = await self._wait_passed(task)
        if passed is True:
            self.emit("ok", f"✓ {task.label()} 已完成（回读 isPassed=true）")
            return StepResult(task, "done", "播放到片尾且平台已记账", elapsed, 1.0)
        if passed is False:
            self.emit(
                "warn",
                f"  {task.label()} 播完但平台未记账（可能需人脸采集/未达时长）",
            )
            return StepResult(
                task,
                "partial",
                "播放到片尾，但回读 isPassed 仍为 false（可能触发了人脸采集）",
                elapsed,
                1.0,
            )
        self.emit("warn", f"  {task.label()} 播完，平台没给出 isPassed 标记")
        return StepResult(task, "partial", "播放到片尾，平台未返回完成标记", elapsed, 1.0)

    async def _wait_passed(self, task: TaskPoint) -> bool | None:
        """回读该附件的 ``isPassed``：播放器上报有延迟，轮询一会儿。"""
        deadline = time.time() + _PASS_WAIT
        while time.time() < deadline:
            self._cards_cache.pop((task.chapter_id, 0), None)
            marg = await self._cards(task.chapter_id, 0)
            for attachment in (marg or {}).get("attachments") or []:
                jobid = str(attachment.get("jobid") or "")
                if jobid and jobid == task.jobid:
                    passed = attachment.get("isPassed")
                    if passed:
                        return True
                    task.passed = passed
            await asyncio.sleep(5)
        return task.passed

    async def _handle_overlay(
        self, frame: Any, on_question: Callable[[PwPage, Any], None] | None
    ) -> bool:
        if on_question is not None:
            try:
                on_question(self.page, frame)
                return True
            except Exception:  # noqa: BLE001 - 回调失败不打断播放
                log.debug("on_question 回调失败", exc_info=True)
        return await self._click_continue(frame)

    async def _ask_human(self, kind: str, message: str) -> bool:
        """暂停等人工处理（人脸采集/弹题）。没有钩子就返回 False。"""
        if self.human_gate is None:
            return False
        self.emit("warn", message)
        try:
            result = self.human_gate(kind, message)
            if asyncio.iscoroutine(result):
                result = await result
            return bool(result)
        except Exception:  # noqa: BLE001 - 人工钩子失败按「没处理」算
            log.debug("human_gate 失败", exc_info=True)
            return False

    async def _resume(self, frame: Any) -> None:
        with contextlib.suppress(PwError):
            await frame.eval(
                "() => { const v = document.querySelector('video');"
                " if (v) v.play().catch(() => {}); }"
            )

    async def _click_continue(self, frame: Any) -> bool:
        """点掉「继续学习/知道了」；点不到返回 False。"""
        for label in _CONTINUE_LABELS:
            label_literal = json.dumps(label, ensure_ascii=False)
            script = (
                "(() => { const label = " + label_literal + ";"
                " const nodes = Array.from(document.querySelectorAll('a,button,div,span'));"
                " const hit = nodes.find(el => (el.innerText || '').trim() === label"
                " && (el.offsetWidth || 0) > 0 && (el.offsetHeight || 0) > 0);"
                " if (hit) { hit.click(); return true; } return false; })()"
            )
            try:
                if await frame.eval(script):
                    return True
            except PwError:
                continue
        return False

    # -- 文档 -------------------------------------------------------------

    async def read_document(
        self,
        task: TaskPoint,
        *,
        seconds: float = 0.0,
        max_seconds: float = 1800.0,
    ) -> StepResult:
        """「读」一个文档任务点：打开课件页、翻页、停留。

        学习通文档任务点按阅读时长记账（阅读器自己上报），这里不伪造请求，
        只让页面保持打开并向后翻页。真实要求由老师配置。
        """
        started = time.time()
        await self._open_cards(task, 0)
        await asyncio.sleep(3)
        frame = None
        for marker in DOC_FRAME_MARKERS:
            frame = await self.page.frame(marker)
            if frame is not None:
                break
        if frame is None:
            return StepResult(task, "failed", "没有找到文档阅读器 iframe")
        budget = max(30.0, min(seconds or 120.0, max_seconds))
        deadline = started + budget
        pages = 0
        self.emit("info", f"📄 {task.label()}（停留 {budget:.0f} 秒）")
        while time.time() < deadline:
            if await self._flip_page(frame):
                pages += 1
                await asyncio.sleep(max(3.0, budget / 8))
            else:
                await asyncio.sleep(5)
        return StepResult(
            task,
            "done",
            f"停留 {budget:.0f} 秒，翻页 {pages} 次",
            time.time() - started,
        )

    async def _flip_page(self, frame: Any) -> bool:
        script = (
            "(() => {"
            " const labels = ['下一页', '下一张', '下一章'];"
            " const nodes = Array.from(document.querySelectorAll('a,button,div,span'));"
            " for (const label of labels) {"
            "   const hit = nodes.find(el => (el.innerText || '').trim() === label"
            "     && (el.offsetWidth || 0) > 0 && (el.offsetHeight || 0) > 0);"
            "   if (hit) { hit.click(); return true; }"
            " }"
            " const next = document.querySelector('.nextPage, #nextPage, [class*=next-page]');"
            " if (next) { next.click(); return true; }"
            " if (window.scrollBy) { window.scrollBy(0, window.innerHeight * 0.9); return true; }"
            " return false;"
            "})()"
        )
        try:
            return bool(await frame.eval(script))
        except PwError:
            return False

    # -- 总流程 -----------------------------------------------------------

    async def run(
        self,
        *,
        kinds: tuple[str, ...] = ("video", "document"),
        limit: int = 0,
        only_pending: bool = True,
        doc_seconds: float = 0.0,
        on_question: Callable[[PwPage, Any], None] | None = None,
        cancel: Callable[[], bool] | None = None,
    ) -> StudyReport:
        """按任务点顺序跑：视频播放、文档停留；测验任务点只列出不执行。"""
        report = StudyReport(course_id=self.course_id)
        await self.open_course()
        points = await self.task_points(only_pending=only_pending)
        if only_pending:
            points = [point for point in points if point.passed is not True]
        selected = [point for point in points if point.kind in kinds]
        if limit > 0:
            selected = selected[:limit]
        videos = sum(point.is_video for point in selected)
        documents = sum(point.is_document for point in selected)
        self.emit(
            "info",
            f"课程 {self.course_id}：待处理 {len(selected)} 个任务点"
            f"（视频 {videos}，文档 {documents}）",
        )
        for point in selected:
            if cancel is not None and cancel():
                self.emit("warn", "收到停止请求，中断刷课")
                break
            try:
                if point.is_video:
                    step = await self.play_video(point, on_question=on_question)
                elif point.is_document:
                    step = await self.read_document(point, seconds=doc_seconds)
                else:
                    step = StepResult(point, "skipped", f"类型 {point.kind} 不在本次范围")
            except PwError as exc:
                step = StepResult(point, "failed", f"页面操作失败：{exc}")
            except Exception as exc:  # noqa: BLE001 - 单个任务点失败不拖垮整门课
                log.exception("任务点执行异常: %s", point.key)
                step = StepResult(point, "failed", f"未预期错误：{exc}")
            report.add(step)
            self.emit("ok" if step.status == "done" else "warn", step.summary())
        report.finished_at = time.time()
        self.emit("ok", f"■ 刷课结束：{report.summary()}")
        return report


__all__ = [
    "CARDS_URL",
    "ChaoxingStudy",
    "StepResult",
    "StudyReport",
    "TaskPoint",
    "module_kind",
    "playback_limits",
]
