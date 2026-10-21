"""学习通刷题引擎：章节测验 / 作业 / 考试（页面驱动）。

## 链路

1. 打开答题页（章节测验：课件页 ``num=1`` 里的 ``doHomeWorkNew`` iframe；
   作业/考试：考试列表 → 考试须知 → 答题页）；
2. ``quiz.parse_work`` 解析题目（含 ``fontdec`` 字体混淆解码）；
3. 交给仓库既有的作答器（``LLMAnswerer`` / ``BankAnswerer`` / ``RuleAnswerer``）
   得到选项字母或填空文本；
4. ``quiz.fill_script`` 逐题填、``quiz.verify_script`` 逐题回读核验；
5. **默认干跑不交卷**：``submit(confirm=True)`` 才点「提交」，并处理二次确认层。

## 纪律

- 题面带图片（公式截图）时，文本模型看不到图 —— 这类题标 ``low_confidence``，
  报告里如实列出，不假装答得对；
- 字体解码没覆盖到的伪字（``Work.font_unmatched``）同样降置信度；
- 交卷不可逆：只有调用方显式 ``confirm=True`` 才执行 ``submit_script(dry_run=False)``。
"""

from __future__ import annotations

import asyncio
import base64
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
from kedaibiao.core.models import Answer, QuestionType
from kedaibiao.core.models import Question as CoreQuestion
from kedaibiao.platforms.chaoxing import captcha as captcha_mod
from kedaibiao.platforms.chaoxing import quiz as quiz_mod

log = logging.getLogger(__name__)

CARDS_URL = (
    "https://mooc1.chaoxing.com/mooc-ans/knowledge/cards"
    "?clazzid={clazzid}&courseid={courseid}&knowledgeid={chapter}&num={num}"
    "&ut=s&cpi={cpi}&v=2025-0424-1038-4&mooc2=1&isMicroCourse=false"
    "&editorPreview=0&crossId="
)
COURSE_URL = (
    "https://mooc1.chaoxing.com/visit/stucoursemiddle"
    "?courseid={courseid}&clazzid={clazzid}&cpi={cpi}&ismooc2=1&v=2"
)
EXAM_NOTES_URL = (
    "https://mooc1.chaoxing.com/exam-ans/exam/test/examcode/examnotes"
    "?courseId={courseid}&classId={clazzid}&examId={exam}&cpi={cpi}"
)
WORK_FRAME = "doHomeWorkNew"
_REFERER = {"Referer": "https://mooc2-ans.chaoxing.com/mooc2-ans/mycourse/stu"}

#: 学习通 qtype → 统一题型
_QTYPE_MAP = {
    0: QuestionType.SINGLE,
    1: QuestionType.MULTIPLE,
    2: QuestionType.FILL,
    3: QuestionType.JUDGE,
}


#: 交卷后页面上的分数格式（实测：「本次成绩85.0分」「最终成绩85.0分」）
_SCORE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"本次成绩\s*[:：]?\s*(\d+(?:\.\d+)?)\s*分?"), "本次成绩"),
    (re.compile(r"最终成绩\s*[:：]?\s*(\d+(?:\.\d+)?)\s*分?"), "最终成绩"),
    (re.compile(r"得分\s*[:：]?\s*(\d+(?:\.\d+)?)"), "得分"),
    (re.compile(r"成绩\s*[:：]?\s*(\d+(?:\.\d+)?)\s*分"), "成绩"),
)


def parse_score_text(text: str) -> tuple[float | None, str]:
    """从交卷结果页正文里抠分数：返回 ``(分数, 原文片段)``；没有就 ``(None, "")``。

    - 只认「本次成绩/最终成绩/得分/成绩X分」这些明确措辞，避免把「满分 100.0」
      误当成得分；
    - 分数原文片段直接给界面展示（例如「本次成绩 85 分」）。
    """
    body = text or ""
    for pattern, label in _SCORE_PATTERNS:
        match = pattern.search(body)
        if not match:
            continue
        try:
            score = float(match.group(1))
        except ValueError:  # pragma: no cover - 正则已限制为数字
            continue
        snippet = match.group(0).strip()
        if "分" not in snippet:
            snippet = f"{label} {score:g} 分"
        return score, snippet
    return None, ""


#: 交卷保险的默认阈值：题数这么少、或置信度低于这个值，就先停在人工确认
SAFE_MIN_QUESTIONS = 3
SAFE_MIN_CONFIDENCE = 0.6


def submit_block_reason(
    items: list[Any],
    *,
    force: bool = False,
    min_questions: int = SAFE_MIN_QUESTIONS,
    min_confidence: float = SAFE_MIN_CONFIDENCE,
) -> str:
    """要不要拦住这次「提交」：返回原因（空串 = 可以交）。

    教训来自实测：2 题各 50 分的小测，模型两题都答得很自信（0.83）但错了一题，
    直接交就是 50 分。题目少、或存在低置信度题时，先停在「已填好、待确认」，
    让人扫一眼再决定 —— 交卷不可逆，慢一步比错一步好。
    """
    if force:
        return ""
    answered = [item for item in items if getattr(item, "answer", "")]
    if not answered:
        return "一题都没答上，拒绝交卷（避免交白卷）"
    if len(items) <= min_questions:
        return (
            f"共 {len(items)} 题（≤{min_questions} 题的小测不自动交），"
            "已填好，请人工确认后点「强制交卷」"
        )
    low = [item for item in answered if item.confidence < min_confidence]
    if low:
        numbers = "、".join(str(item.number) for item in low)
        return (
            f"有 {len(low)} 题置信度低于 {min_confidence:.0%}（第 {numbers} 题），"
            "已填好，请人工确认后点「强制交卷」"
        )
    return ""


@dataclass(slots=True)
class PlanItem:
    """一道题的作答计划（填卷前先算好、可干跑预览）。"""

    number: int
    qid: str
    type_name: str
    text: str
    options: list[str]
    images: int
    #: 选项字母（多选是 "AB" 这种）或填空文本
    answer: str
    confidence: float
    source: str
    unsupported: str = ""
    #: 填卷后的回读核验结果
    verified: bool | None = None

    def label(self) -> str:
        return f"{self.number}. {self.type_name}{self.text[:40]}"


@dataclass(slots=True)
class QuizReport:
    title: str = ""
    items: list[PlanItem] = field(default_factory=list)
    submitted: bool = False
    submit_detail: str = ""
    #: 交卷后平台回显的分数（没回显就是 None，别编一个）
    score: float | None = None
    #: 分数原文（「本次成绩 85.0 分」这种），给界面展示用
    score_text: str = ""
    #: 交卷后需要人工处理的原因（图形验证码/未及格重做等）
    submit_note: str = ""
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0

    @property
    def filled(self) -> int:
        return sum(1 for item in self.items if item.answer and not item.unsupported)

    @property
    def verified(self) -> int:
        return sum(1 for item in self.items if item.verified)

    def summary(self) -> str:
        elapsed = (self.finished_at or time.time()) - self.started_at
        parts = [
            f"共 {len(self.items)} 题",
            f"作答 {self.filled}",
            f"核验通过 {self.verified}",
        ]
        if self.submitted:
            parts.append("已提交")
            if self.score is not None:
                parts.append(f"得分 {self.score:g}")
            elif self.score_text:
                parts.append(self.score_text)
        else:
            parts.append("未提交（干跑）")
        if self.submit_note:
            parts.append(self.submit_note)
        parts.append(f"耗时 {elapsed:.1f} 秒")
        return "，".join(parts)


class ChaoxingQuiz:
    """一份答题页的作答会话。"""

    def __init__(
        self,
        page: PwPage,
        *,
        course_id: str,
        clazz_id: str,
        cpi: str = "",
        answerer: Any | None = None,
        emit: Callable[[str, str], None] | None = None,
        human_gate: Callable[[str, str], Any] | None = None,
        use_ocr: bool | None = None,
    ) -> None:
        self.page = page
        self.course_id = str(course_id)
        self.clazz_id = str(clazz_id)
        self.cpi = str(cpi or "")
        self.answerer = answerer
        self._emit = emit or (lambda level, message: log.info("%s", message))
        #: 需要人工认证（人脸/验证码）时的钩子：``await human_gate(kind, message) -> bool``
        self.human_gate = human_gate
        #: 题面走 OCR（页面截图 + 视觉模型）：学习通的字体混淆/公式图靠它最稳。
        #: 默认开；``KEDAIBIAO_CHAXING_OCR=0`` 可关掉退回纯 DOM 解析。
        if use_ocr is None:
            import os  # noqa: PLC0415

            use_ocr = os.environ.get("KEDAIBIAO_CHAXING_OCR", "1") not in {
                "0", "false", "no", "off",
            }
        self.use_ocr = bool(use_ocr)
        self._questions: dict[str, quiz_mod.Question] = {}

    def emit(self, level: str, message: str) -> None:
        try:
            self._emit(level, message)
        except Exception:  # noqa: BLE001
            log.debug("emit 回调异常", exc_info=True)

    # -- 打开答题页 -------------------------------------------------------

    def _cards_url(self, chapter_id: str, num: int) -> str:
        return CARDS_URL.format(
            clazzid=self.clazz_id,
            courseid=self.course_id,
            chapter=quote(str(chapter_id), safe=""),
            num=num,
            cpi=self.cpi,
        )

    async def open_section_quiz(self, chapter_id: str, *, num: int = 1) -> Any:
        """打开章节测验：课件页 ``num=1`` 里的 ``doHomeWorkNew`` iframe。"""
        await self.page.goto(self._cards_url(chapter_id, num), timeout=90000)
        deadline = time.time() + 60
        while time.time() < deadline:
            frame = await self.page.frame(WORK_FRAME)
            if frame is not None:
                return frame
            await asyncio.sleep(1.5)
        raise PwError("没有找到章节测验 iframe（doHomeWorkNew）")

    async def list_exams(self) -> list[quiz_mod.ExamItem]:
        """打开课程页 → 「考试」tab → 解析考试/作业/测验列表（只读）。"""
        await self.page.goto(
            COURSE_URL.format(
                courseid=self.course_id, clazzid=self.clazz_id, cpi=self.cpi
            ),
            timeout=60000,
        )
        await self.page.wait_for("span.nav_content", timeout=30000)
        await self.page.click_text("考试", timeout=15000)
        deadline = time.time() + 30
        while time.time() < deadline:
            frame = await self.page.frame("exam-list")
            if frame is not None:
                html = await frame.html()
                items = quiz_mod.parse_exam_list(html)
                if items:
                    return items
            await asyncio.sleep(1.5)
        raise PwError("考试列表为空：可能没有考试，或会话已失效")

    async def _wait_human(self, kind: str, message: str) -> bool:
        """暂停等人工处理（人脸采集/图形验证码）。没有钩子就返回 False。"""
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

    #: 须知/答题页判断「是否真的要求人脸认证」的脚本。
    #:
    #: 别扫 HTML 文本：考试须知模板里**永远**带着人脸比对的 DOM（`face_Box`、
    #: 「人脸比对中…」之类），只是隐藏着。真正的开关是隐藏域
    #: ``#needFaceRecognition`` / ``#webNeedFaceRecognition``（实测值 0 = 不需要），
    #: 其次才是「人脸弹层是否可见」。
    _FACE_JS = """(() => {
        const flagOn = (id) => {
            const el = document.getElementById(id);
            return !!el && String(el.value || '').trim() === '1';
        };
        if (flagOn('needFaceRecognition') || flagOn('webNeedFaceRecognition')) return true;
        const pop = document.getElementById('faceRecognitionComparePop');
        if (pop) {
            const style = window.getComputedStyle(pop);
            if (style.display !== 'none' && style.visibility !== 'hidden'
                && (pop.offsetWidth || pop.offsetHeight)) return true;
        }
        const boxes = Array.from(document.querySelectorAll(
            '.face_Box, .face_video_btn, .face_recognition_pop'));
        return boxes.some(el => (el.offsetWidth || 0) > 50 && (el.offsetHeight || 0) > 20);
    })()"""

    #: 进入考试时可能弹出超星自己的「安全验证」滑块（captcha.chaoxing.com）。
    #: 这是风控触发，不是人脸；按仓库纪律**不代过**，交给人工在可见窗口里拖。
    _CAPTCHA_JS = """(() => {
        const captcha = document.getElementById('captcha');
        if (captcha && captcha.style.display !== 'none'
            && (captcha.offsetWidth || 0) > 0) return true;
        const eject = document.getElementById('eject');
        return !!eject && (eject.offsetWidth || 0) > 0
            && window.getComputedStyle(eject).display !== 'none';
    })()"""

    async def _page_has_captcha(self) -> bool:
        with contextlib.suppress(PwError):
            if await self.page.eval(self._CAPTCHA_JS):
                return True
        for frame in await self.page.frames():
            with contextlib.suppress(PwError):
                if await frame.eval(self._CAPTCHA_JS):
                    return True
        return False

    async def _wait_captcha_done(self, *, timeout: float = 120.0) -> bool:
        """等人拖完滑块：验证码消失即视为完成。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not await self._page_has_captcha():
                return True
            await asyncio.sleep(2)
        return False

    async def _page_has_face_capture(self) -> bool:
        """当前页面**真的**要求人脸认证吗（隐藏模板 DOM 不算）。"""
        with contextlib.suppress(PwError):
            if await self.page.eval(self._FACE_JS):
                return True
        for frame in await self.page.frames():
            with contextlib.suppress(PwError):
                if await frame.eval(self._FACE_JS):
                    return True
        return False

    async def open_exam_item(self, item: quiz_mod.ExamItem) -> PwPage:
        """打开考试/作业：须知页 → 勾选同意 → 进入考试 →（二次确认）→ 答题页。

        实测到的坑：

        - 「我已阅读并同意」是**开关**：连点两次会取消勾选，`进入考试` 随之变灰；
        - 点「进入考试」不会直接跳转，而是弹出 `#confirmEnterWin` 提示层
          （本次考试答题时长…），要点 `#tabIntoexam2` 才真正进卷；
        - 答题页可能开在新标签里，也可能当前页跳转，两种都要认；
        - 「只允许手机 APP 考试」直接拒绝。
        """
        if item.mobile_only:
            raise PwError(f"「{item.title}」只允许手机 APP 作答，网页端做不了")
        url = EXAM_NOTES_URL.format(
            courseid=self.course_id,
            clazzid=self.clazz_id,
            exam=item.exam_id,
            cpi=self.cpi,
        )
        await self.page.goto(url, timeout=90000)
        self.emit("info", f"考试须知：{item.title}（{item.status}）")
        for _attempt in range(3):
            await self._ensure_agreement()
            with contextlib.suppress(PwError):
                await self.page.click("#startBtn", timeout=8000)
            await asyncio.sleep(2)
            if await self._page_has_face_capture():
                handled = await self._wait_human(
                    "face",
                    "考试要求人脸识别：请完成认证后再继续",
                )
                if not handled:
                    raise PwError(
                        "考试要求人脸识别，未完成认证（可用 --show 在有摄像头的机器上跑）"
                    )
                continue
            await self._confirm_enter_dialog()
            if await self._page_has_captcha():
                self.emit("info", "检测到「安全验证」滑块，尝试自动求解…")
                solved = False
                with contextlib.suppress(Exception):
                    solved = await captcha_mod.solve_slide_captcha(self.page)
                if solved:
                    self.emit("ok", "滑块验证已自动通过")
                else:
                    handled = await self._wait_human(
                        "captcha",
                        "自动解滑块失败：请在可见的浏览器窗口里手动拖动完成后继续",
                    )
                    if not handled:
                        raise PwError(
                            "进入考试需要滑块安全验证（自动求解失败），"
                            "可用 --show 打开窗口人工拖动"
                        )
                    if not await self._wait_captcha_done():
                        raise PwError("滑块安全验证没有通过，请重试")
                await asyncio.sleep(2)
            answer_page = await self._pick_answer_page()
            if answer_page is not None:
                return answer_page
        raise PwError("进入考试后没有找到答题页（可能被风控/需要人脸）")

    async def _ensure_agreement(self) -> None:
        """勾选「我已阅读并同意」——已经勾上时不要再点（再点会取消）。"""
        disabled = await self.page.eval(
            "(() => { const b = document.getElementById('startBtn');"
            " return !!b && /disble|opa40/.test(b.className); })()"
        )
        if disabled:
            with contextlib.suppress(PwError):
                await self.page.click(".face_agreement", timeout=8000)
            await asyncio.sleep(0.5)

    async def _confirm_enter_dialog(self) -> None:
        """点掉「本次考试答题时长…」确认层里的「进入考试/继续考试」。"""
        script = (
            "(() => {"
            " const ids = ['tabIntoexam2'];"
            " for (const id of ids) {"
            "   const el = document.getElementById(id);"
            "   if (el && (el.offsetWidth || 0) > 0) { el.click(); return true; }"
            " }"
            " const nodes = Array.from(document.querySelectorAll('a,button'));"
            " const labels = ['进入考试', '继续考试'];"
            " for (const label of labels) {"
            "   const hit = nodes.find(el => (el.innerText || '').trim() === label"
            "     && (el.offsetWidth || 0) > 0 && (el.offsetHeight || 0) > 0);"
            "   if (hit) { hit.click(); return true; }"
            " }"
            " return false;"
            "})()"
        )
        for _ in range(6):
            with contextlib.suppress(PwError):
                if await self.page.eval(script):
                    self.emit("info", "已确认进入考试，等待答题页…")
                    return
            await asyncio.sleep(1.5)

    async def _pick_answer_page(self) -> PwPage | None:
        """在已打开的页面里找答题页（新标签或当前页跳转两种都可能）。"""
        context = getattr(self.page.raw, "context", None)
        pages = list(getattr(context, "pages", []) or [])
        for raw_page in pages:
            url = str(getattr(raw_page, "url", "") or "")
            if "examnotes" in url:
                continue
            if "reVersionTestStartNew" in url or "/test/" in url:
                return PwPage(raw_page)
        deadline = time.time() + 30
        while time.time() < deadline:
            pages = list(getattr(context, "pages", []) or [])
            for raw_page in pages:
                url = str(getattr(raw_page, "url", "") or "")
                if "examnotes" in url:
                    continue
                candidate = PwPage(raw_page)
                with contextlib.suppress(PwError):
                    html = await candidate.html()
                    if 'id="question' in html or "TiMu" in html:
                        return candidate
            await asyncio.sleep(1.5)
        return None

    #: 考试答题页是「一题一屏」：读当前题 → 填 → 下一题。
    #: 题干/选项里的公式都是图片，一并交给视觉作答器。
    _EXAM_READ_JS = """(() => {
        const q = document.querySelector('.questionLi');
        if (!q) return {done: true};
        const head = q.querySelector('h3.mark_name');
        const titleNode = head || q;
        const text = (titleNode.innerText || '').replace(/\\s+/g, ' ').trim();
        const images = Array.from(titleNode.querySelectorAll('img'))
            .map(i => i.getAttribute('data-original') || i.src).filter(Boolean);
        const options = Array.from(q.querySelectorAll('.answerBg')).map((el, index) => {
            const span = el.querySelector('span.num_option');
            const label = span ? (span.innerText || '').trim() : String(index);
            const data = span ? (span.getAttribute('data') || '').trim() : '';
            const text = (el.innerText || '').replace(/\\s+/g, ' ').trim();
            const imgs = Array.from(el.querySelectorAll('img'))
                .map(i => i.getAttribute('data-original') || i.src).filter(Boolean);
            const cls = (el.className || '').toString();
            const on = /(^|\\s)(check_answer|check_answer_dx|cur)(\\s|$)/.test(cls);
            return {label: label, data: data, text: text, images: imgs, on: on};
        });
        const typeEl = q.querySelector('input[type=hidden][id^=typeName]');
        const inputs = Array.from(q.querySelectorAll('input[type=text], textarea'))
            .map(el => ({id: el.id, name: el.name}));
        return {
            qid: q.getAttribute('data') || '',
            type: typeEl ? typeEl.value : '',
            text: text,
            images: images,
            options: options,
            inputs: inputs,
            number: (text.match(/^(\\d+)\\./) || [])[1] || '',
        };
    })()"""

    async def _exam_read(self, page: Any) -> dict[str, Any]:
        info = await page.eval(self._EXAM_READ_JS)
        return info if isinstance(info, dict) else {"done": True}

    async def _exam_next(self, page: Any) -> str:
        """点「下一题/下一步」；没有下一题返回空串。"""
        label = await page.eval(
            """(() => {
                const nodes = Array.from(document.querySelectorAll('a,button'));
                const hit = nodes.find(el =>
                    ['下一题', '下一步'].includes((el.innerText || '').trim())
                    && (el.offsetWidth || 0) > 0);
                if (hit) { hit.click(); return hit.innerText.trim(); }
                return '';
            })()"""
        )
        return str(label or "")

    @staticmethod
    def items_payload(items: list[PlanItem]) -> list[dict[str, Any]]:
        """把题目计划转成纯数据（控制台「题目信息」表直接渲染）。"""
        return [
            {
                "number": item.number,
                "type": item.type_name,
                "answer": item.answer,
                "confidence": round(item.confidence, 2),
                "source": item.source,
                "verified": item.verified,
                "unsupported": item.unsupported,
                "text": (item.text or "")[:160],
            }
            for item in items
        ]

    @staticmethod
    def _png_data_url(png: bytes) -> str:
        return "data:image/png;base64," + base64.b64encode(png).decode()

    def _ocr_supported(self) -> bool:
        """作答器能不能读图（能读图才值得截图）。"""
        return bool(self.use_ocr and getattr(self.answerer, "supports_images", False))

    async def _ocr_core_question(
        self,
        page: Any,
        selector: str,
        *,
        type_name: str,
        number: int = 0,
        letters: list[str] | None = None,
        option_texts: list[str] | None = None,
    ) -> CoreQuestion | None:
        """把一道题**截图**成一张图，作为视觉模型的题面来源（OCR 方案）。

        为什么是 OCR 而不是解析 DOM：学习通题面用子集字体把常用字换成伪汉字，
        公式还是图片；直接读文本要靠字形比对解码，遇到字体一变就得重来。
        截图交给视觉模型则一次覆盖「字体混淆 + 公式图 + 排版」三件事。

        选项字母仍取自 DOM（点击要按字母点），文本只作为辅助提示；图里有完整题面。
        """
        try:
            png = await page.screenshot_bytes(selector)
        except PwError as exc:
            log.debug("题目截图失败（%s）: %s", selector, exc)
            return None
        if not png:
            return None
        options: list[str] = []
        for index, letter in enumerate(letters or []):
            text = (option_texts or [""] * len(letters))[index] if option_texts else ""
            options.append(f"{letter}. {text}".strip())
        stem = (
            f"下面这张截图是第 {number} 题（{type_name or '选择题'}）。"
            "请先识别图片里的题干与选项，再给出正确答案。"
            if number else
            "下面这张截图是一道题。请先识别图片里的题干与选项，再给出正确答案。"
        )
        return CoreQuestion(
            stem=stem,
            type=_QTYPE_MAP.get(
                {"单选题": 0, "多选题": 1, "填空题": 2, "判断题": 3}.get(type_name, -1),
                QuestionType.UNKNOWN,
            ),
            options=options,
            remote_id=f"ocr-{number or selector}",
            raw={"images": [self._png_data_url(png)]},
        )

    async def _exam_answer(self, page: Any, info: dict[str, Any]) -> PlanItem:
        """答当前这一题并填进页面，返回计划项。"""
        qtype_name = str(info.get("type") or "")
        qid = str(info.get("qid") or "")
        options = info.get("options") or []
        number = int(info["number"]) if str(info.get("number") or "").isdigit() else 0
        plan = PlanItem(
            number=number or len(self._questions) + 1,
            qid=qid,
            type_name=qtype_name,
            text=str(info.get("text") or ""),
            options=[str(opt.get("label") or "") for opt in options],
            images=len(info.get("images") or []),
            answer="",
            confidence=0.0,
            source="",
        )
        type_map = {
            "单选题": QuestionType.SINGLE,
            "多选题": QuestionType.MULTIPLE,
            "判断题": QuestionType.JUDGE,
            "填空题": QuestionType.FILL,
        }
        question_type = type_map.get(qtype_name)
        if question_type is None:
            plan.unsupported = f"暂不支持的考试题型：{qtype_name or '未知'}"
            return plan
        core_question: CoreQuestion | None = None
        if self._ocr_supported() and options:
            # OCR 方案：整道题截一张图，题干/公式/选项都交给视觉模型识别
            core_question = await self._ocr_core_question(
                page,
                ".questionLi",
                type_name=qtype_name,
                number=plan.number,
                letters=[str(opt.get("label") or "") for opt in options],
                option_texts=[str(opt.get("text") or "") for opt in options],
            )
        if core_question is None:
            raw: dict[str, Any] = {"images": list(info.get("images") or [])}
            option_images = {
                index: opt.get("images") or []
                for index, opt in enumerate(options)
                if opt.get("images")
            }
            if option_images:
                raw["option_images"] = option_images
            core_question = CoreQuestion(
                stem=plan.text or "（题面是图片）",
                type=question_type,
                options=[(opt.get("text") or opt.get("label") or "") for opt in options],
                remote_id=qid,
                raw=raw,
            )
        answer: Answer | None = None
        if self.answerer is not None:
            produced = await self.answerer.answer([core_question])
            answer = produced[0] if produced else None
        if question_type is QuestionType.FILL:
            text_answer = (answer.text if answer else "") or ""
            plan.answer = text_answer
            plan.confidence = float(getattr(answer, "confidence", 0.0) or 0.0)
            plan.source = getattr(getattr(answer, "source", None), "value", "unknown")
            if text_answer:
                result = await page.eval(self._exam_fill_call([], [text_answer]))
                plan.verified = bool((result or {}).get("filled"))
            return plan
        choices = list(answer.choices) if answer and answer.choices else []
        letters = [
            str(options[index].get("label") or "")
            for index in choices
            if 0 <= index < len(options)
        ]
        plan.answer = "".join(letters)
        plan.confidence = float(getattr(answer, "confidence", 0.0) or 0.0)
        plan.source = getattr(getattr(answer, "source", None), "value", "unknown")
        if choices:
            await page.eval(self._exam_fill_call(choices, []))
            plan.verified = bool(letters)
        return plan

    @staticmethod
    def _exam_fill_call(choices: list[int], texts: list[str]) -> str:
        return (
            "((choices, texts) => {"
            " const q = document.querySelector('.questionLi');"
            " const out = {clicked: [], filled: 0};"
            " if (!q) return out;"
            " if (choices && choices.length) {"
            "   const boxes = Array.from(q.querySelectorAll('.answerBg'));"
            "   for (let index = 0; index < boxes.length; index++) {"
            "     if (!choices.includes(index)) continue;"
            "     const el = boxes[index];"
            "     const cls = (el.className || '').toString();"
            "     const on = /(^|\\s)(check_answer|check_answer_dx|cur)(\\s|$)/.test(cls);"
            "     if (!on) { el.click(); out.clicked.push(index); }"
            "   }"
            " }"
            " if (texts && texts.length) {"
            "   const fields = Array.from(q.querySelectorAll('input[type=text], textarea'));"
            "   for (let i = 0; i < fields.length && i < texts.length; i++) {"
            "     const el = fields[i]; el.value = texts[i];"
            "     el.dispatchEvent(new Event('input', {bubbles: true}));"
            "     el.dispatchEvent(new Event('change', {bubbles: true}));"
            "     el.dispatchEvent(new Event('blur', {bubbles: true}));"
            "     out.filled += 1;"
            "   }"
            " }"
            " return out;"
            "})(" + json.dumps(choices) + "," + json.dumps(texts) + ")"
        )

    async def _exam_submit_form(self, page: Any) -> dict[str, Any]:
        """提交试卷：直接提交页面自带的 ``#submitTest`` 表单（页面自己的载荷）。"""
        result = await page.eval(
            """(() => {
                const form = document.getElementById('submitTest');
                if (!form) return {ok: false, error: '找不到交卷表单 #submitTest'};
                form.submit();
                return {ok: true};
            })()"""
        )
        if not isinstance(result, dict) or not result.get("ok"):
            return result if isinstance(result, dict) else {"ok": False}
        await asyncio.sleep(5)
        body = ""
        with contextlib.suppress(PwError):
            body = await page.text("body")
        try:
            parsed = json.loads(body)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            result["response"] = parsed
            result["ok"] = parsed.get("status") == "success" or "成功" in str(
                parsed.get("msg", "")
            )
        return result

    async def _read_exam_score(self, exam_id: str) -> tuple[float | None, str]:
        """交卷后从考试列表回读分数（已完成行会回显「xx 分」）。"""
        try:
            items = await self.list_exams()
        except PwError as exc:
            log.debug("回读考试列表失败: %s", exc)
            return None, ""
        for item in items:
            if item.exam_id == exam_id and item.score is not None:
                return item.score, item.score_text
        return None, ""

    async def run_exam(
        self,
        item: quiz_mod.ExamItem,
        *,
        confirm: bool = False,
        max_questions: int = 60,
        on_items: Callable[[list[dict[str, Any]]], None] | None = None,
        force: bool = False,
    ) -> QuizReport:
        """作业/考试全链路：进入（含自动解滑块）→ 逐题作答 → 交卷 → 回读分数。"""
        report = QuizReport(title=item.title)
        page: Any = None
        try:
            page = await self.open_exam_item(item)
            self.emit("info", f"已进入答题页：{page.url[:120]}")
            seen: set[str] = set()
            for _step in range(max_questions):
                info = await self._exam_read(page)
                if info.get("done"):
                    break
                # 「下一题」在最后一题后可能绕回第一题：按 qid 去重，别把已答的再答一遍
                qid = str(info.get("qid") or "")
                if qid and qid in seen:
                    break
                if qid:
                    seen.add(qid)
                plan_item = await self._exam_answer(page, info)
                report.items.append(plan_item)
                if on_items is not None:
                    on_items(self.items_payload(report.items))
                mark = "ok" if plan_item.answer else "warn"
                self.emit(
                    mark,
                    f"第 {plan_item.number} 题 {plan_item.type_name} → "
                    f"{plan_item.answer or '（未答）'}"
                    f"（{plan_item.confidence:.2f} {plan_item.source}）",
                )
                if not await self._exam_next(page):
                    break
                await asyncio.sleep(1.2)
            answered = sum(1 for item in report.items if item.answer)
            self.emit("info", f"共 {len(report.items)} 题，作答 {answered} 题")
            if not confirm:
                self.emit("info", "干跑：未交卷（注意：进入考试后计时不会停）")
                return report
            blocked = submit_block_reason(report.items, force=force)
            if blocked:
                report.submit_note = blocked
                self.emit("warn", f"暂不交卷：{blocked}")
                return report
            self.emit("info", "提交试卷…")
            result = await self._exam_submit_form(page)
            report.submitted = bool(result.get("ok"))
            report.submit_detail = json.dumps(
                {k: v for k, v in result.items() if k != "page_text"},
                ensure_ascii=False,
            )[:300]
            if not report.submitted:
                report.submit_note = f"交卷失败：{result.get('error') or result.get('response')}"
                return report
            await asyncio.sleep(5)
            score, score_text = await self._read_exam_score(item.exam_id)
            report.score = score
            report.score_text = score_text
            if score is not None:
                self.emit("ok", f"交卷成功，平台回显：{score_text}")
            else:
                self.emit("warn", "交卷成功，但列表里还没看到分数（可能延迟）")
        finally:
            if page is not None and page is not self.page:
                with contextlib.suppress(Exception):
                    await page.raw.close()
        report.finished_at = time.time()
        return report

    async def _exam_submit(self, page: Any, *, confirm: bool) -> dict[str, Any]:
        """考试交卷：定位「交卷」按钮；确认后点两次（页面自身有二次确认层）。"""
        script = (
            "(() => {"
            " const labels = ['交卷', '提交试卷', '确认交卷'];"
            " const nodes = Array.from(document.querySelectorAll('a,button,div,span'));"
            " for (const label of labels) {"
            "   const hit = nodes.find(el => (el.innerText || '').trim() === label"
            "     && (el.offsetWidth || 0) > 0 && (el.offsetHeight || 0) > 0);"
            "   if (hit) { if (!"
            + ("false" if confirm else "true")
            + ") hit.click(); return {ok: true, text: label, selector: 'text'}; }"
            " }"
            " return {ok: false, error: '找不到交卷按钮'};"
            "})()"
        )
        result = await page.eval(script)
        if not isinstance(result, dict):
            return {"ok": False, "error": "交卷脚本返回异常"}
        if not confirm:
            self.emit(
                "info",
                f"干跑：找到交卷按钮「{result.get('text', '?')}」，未点击"
                if result.get("ok")
                else f"干跑：{result.get('error', '找不到交卷按钮')}",
            )
            return result
        # 页面自身的二次确认层
        await asyncio.sleep(2)
        confirm_script = (
            "(() => {"
            " const labels = ['确定', '确认', '确认交卷', '继续交卷'];"
            " const nodes = Array.from(document.querySelectorAll('a,button,div,span'));"
            " for (const label of labels) {"
            "   const hit = nodes.find(el => (el.innerText || '').trim() === label"
            "     && (el.offsetWidth || 0) > 0 && (el.offsetHeight || 0) > 0);"
            "   if (hit) { hit.click(); return true; }"
            " }"
            " return false;"
            "})()"
        )
        with contextlib.suppress(PwError):
            await page.eval(confirm_script)
        result["submitted"] = True
        return result

    # -- 解析与作答计划 ---------------------------------------------------

    async def parse_work(self, frame: Any) -> quiz_mod.Work:
        html = await frame.html()
        return quiz_mod.parse_work(html)

    def _core_question(self, question: quiz_mod.Question) -> CoreQuestion:
        """把学习通题目转成统一 Question，**把图片 URL 也带上**。

        带图题（公式截图）交给 ``VisionAnswerer`` 走视觉；纯文本题行为不变。
        选项文本为空但有图的，先放占位符，避免选项列表错位。
        """
        qtype = _QTYPE_MAP.get(int(question.qtype), QuestionType.UNKNOWN)
        stem = (question.text or "").strip()
        if not stem and question.images:
            stem = "（题面是图片）"
        options = []
        for opt in question.options:
            text = (opt.text or "").strip()
            if not text and opt.images:
                text = f"（选项{opt.letter}是图片）"
            options.append(text or f"选项{opt.letter}")
        raw: dict[str, Any] = {"images": list(question.images)}
        option_images = {
            opt.letter: list(opt.images) for opt in question.options if opt.images
        }
        if option_images:
            raw["option_images"] = option_images
        return CoreQuestion(
            stem=stem,
            type=qtype,
            options=options,
            remote_id=question.qid,
            raw=raw,
        )

    async def plan(
        self,
        work: quiz_mod.Work,
        *,
        frame: Any | None = None,
        on_items: Callable[[list[dict[str, Any]]], None] | None = None,
    ) -> list[PlanItem]:
        """逐题调用作答器，得到字母/文本。

        开了 OCR 且给了 ``frame`` 时**一题一题来**：截图 → 视觉作答 → 立刻通过
        ``on_items`` 推给界面，用户能看着题目信息一条条出现，而不是等 90 秒。
        没开 OCR 就退回原来的批量链路。
        """
        items: list[PlanItem] = []
        sequential = frame is not None and self._ocr_supported()
        if sequential:
            self.emit("info", "题面走 OCR：逐题截图识别（字体混淆/公式图都按图片读）")

        def remember(question: quiz_mod.Question, answer: Answer | None) -> None:
            letters, text_answer, confidence, source = self._answer_to_letters(
                question, answer
            )
            items.append(
                PlanItem(
                    number=question.number,
                    qid=question.qid,
                    type_name=question.type_name,
                    text=question.text,
                    options=[opt.letter for opt in question.options],
                    images=len(question.images),
                    answer=letters or text_answer,
                    confidence=confidence,
                    source=source,
                )
            )

        if sequential:
            for question in work.questions:
                if question.unsupported:
                    items.append(
                        PlanItem(
                            number=question.number,
                            qid=question.qid,
                            type_name=question.type_name,
                            text=question.text,
                            options=[opt.letter for opt in question.options],
                            images=len(question.images),
                            answer="",
                            confidence=0.0,
                            source="unsupported",
                            unsupported=question.unsupported,
                        )
                    )
                else:
                    core: CoreQuestion | None = None
                    if question.qid:
                        core = await self._ocr_core_question(
                            frame,
                            f"#question{question.qid}",
                            type_name=question.type_name,
                            number=question.number,
                            letters=[opt.letter for opt in question.options],
                            option_texts=[opt.text for opt in question.options],
                        )
                    core = core or self._core_question(question)
                    answer: Answer | None = None
                    if self.answerer is not None:
                        produced = await self.answerer.answer([core])
                        answer = produced[0] if produced else None
                    remember(question, answer)
                if on_items is not None:
                    on_items(self.items_payload(items))
            return items

        answerable = [q for q in work.questions if not q.unsupported]
        answers: dict[str, Answer] = {}
        if self.answerer is not None and answerable:
            produced_answers = await self.answerer.answer(
                [self._core_question(question) for question in answerable]
            )
            for question, answer in zip(answerable, produced_answers, strict=True):
                answers[question.qid] = answer
        for question in work.questions:
            if question.unsupported:
                items.append(
                    PlanItem(
                        number=question.number,
                        qid=question.qid,
                        type_name=question.type_name,
                        text=question.text,
                        options=[opt.letter for opt in question.options],
                        images=len(question.images),
                        answer="",
                        confidence=0.0,
                        source="unsupported",
                        unsupported=question.unsupported,
                    )
                )
                continue
            remember(question, answers.get(question.qid))
        if on_items is not None:
            on_items(self.items_payload(items))
        return items

    @staticmethod
    def _answer_to_letters(
        question: quiz_mod.Question, answer: Answer | None
    ) -> tuple[str, str, float, str]:
        """把统一 Answer 转成学习通的字母串（多选 "AB"）或填空文本。"""
        if answer is None or answer.is_empty():
            return "", "", 0.0, "empty"
        confidence = float(answer.confidence or 0.0)
        source = getattr(answer.source, "value", str(answer.source))
        if question.qtype == quiz_mod.FILL_QTYPE:
            return "", answer.text or "", confidence, source
        letters = []
        for index in answer.choices:
            if 0 <= index < len(question.options):
                letters.append(question.options[index].letter.upper())
        return "".join(letters), "", confidence, source

    # -- 填卷与核验 -------------------------------------------------------

    async def fill(self, frame: Any, items: list[PlanItem]) -> int:
        """逐题填卷；返回成功填写的题数。"""
        filled = 0
        for item in items:
            if item.unsupported or not item.answer:
                continue
            question = self._question_by_qid(frame, item.qid)
            if question is None:
                item.unsupported = "填卷时找不到题目"
                continue
            try:
                script = quiz_mod.fill_script(question, item.answer)
                await frame.eval(script)
                filled += 1
                self.emit("ok", f"填好 {item.number}. {item.type_name} → {item.answer}")
            except ValueError as exc:
                item.unsupported = str(exc)
            except PwError as exc:
                item.unsupported = f"填卷失败：{exc}"
        return filled

    def _question_by_qid(
        self, frame: Any, qid: str
    ) -> quiz_mod.Question | None:  # pragma: no cover - 由测试替身覆盖
        return self._questions.get(qid)

    async def set_questions(self, work: quiz_mod.Work) -> None:
        self._questions = {question.qid: question for question in work.questions}

    async def verify(self, frame: Any, items: list[PlanItem]) -> int:
        """逐题回读核验，返回通过数。"""
        passed = 0
        for item in items:
            if item.unsupported or not item.answer:
                continue
            question = self._questions.get(item.qid)
            if question is None:
                continue
            try:
                ok = bool(await frame.eval(quiz_mod.verify_script(question, item.answer)))
            except PwError:
                ok = False
            item.verified = ok
            passed += 1 if ok else 0
            if not ok:
                self.emit("warn", f"核验不一致：第 {item.number} 题（{item.answer}）")
        return passed

    async def submit(self, frame: Any, *, confirm: bool = False) -> dict[str, Any]:
        """交卷。``confirm=False`` 只定位提交按钮（干跑），不点击。

        真交卷后**回读结果**：平台一般会把「本次成绩/最终成绩」打回页面，
        这里轮询页面与各 iframe 的正文，把分数原文与数值一起带回来。
        """
        script = quiz_mod.submit_script(dry_run=not confirm)
        try:
            result = await frame.eval(script)
        except PwError as exc:
            return {"ok": False, "error": f"提交脚本执行失败：{exc}"}
        if not isinstance(result, dict):
            result = {"ok": False, "error": f"提交脚本返回 {type(result).__name__}"}
        if not confirm:
            self.emit(
                "info",
                f"干跑：找到提交按钮「{result.get('text', '?')}」，未点击"
                if result.get("ok")
                else f"干跑：{result.get('error', '找不到提交按钮')}",
            )
            return result
        result["submitted"] = True
        # 交卷后页面可能跳转/弹层，最多等 90 秒把结果捞出来
        outcome = await self.wait_result()
        result.update(outcome)
        return result

    async def _collect_text(self) -> str:
        """页面 + 所有 iframe 的可见正文（交卷结果可能在任一层）。"""
        chunks: list[str] = []
        with contextlib.suppress(PwError):
            chunks.append(await self.page.text("body"))
        for frame in await self.page.frames():
            with contextlib.suppress(PwError):
                text = await frame.eval("document.body ? document.body.innerText : ''")
                if text:
                    chunks.append(str(text))
        return "\n".join(chunks)

    async def wait_result(self, *, timeout: float = 90.0) -> dict[str, Any]:
        """等交卷结果：返回 ``{score, score_text, submit_note, page_text}``。

        只有平台真的回显分数才填 ``score``；找不到就如实返回 ``None``，
        并给出「平台没有回显分数」的说明，不猜一个数字。
        """
        deadline = time.time() + timeout
        finished_markers = (
            "提交成功", "交卷成功", "已交卷", "测评结果", "查看答案", "答题结果",
        )
        last_text = ""
        while time.time() < deadline:
            text = last_text = await self._collect_text()
            if "请输入验证码" in text or "验证码" in text and "提交" in text:
                return {
                    "score": None,
                    "score_text": "",
                    "submit_note": "需要图形验证码，已停止（请人工提交一次）",
                    "page_text": text[:500],
                }
            if "未达到及格线" in text or "未达到通过标准" in text:
                return {
                    "score": None,
                    "score_text": "",
                    "submit_note": "平台提示未达到及格线/通过标准，需要重做",
                    "page_text": text[:500],
                }
            if "答题次数" in text and "已用完" in text:
                return {
                    "score": None,
                    "score_text": "",
                    "submit_note": "答题次数已用完",
                    "page_text": text[:500],
                }
            score, snippet = parse_score_text(text)
            if score is not None:
                self.emit("ok", f"交卷成功，平台回显：{snippet}")
                return {
                    "score": score,
                    "score_text": snippet,
                    "submit_note": "",
                    "page_text": text[:500],
                }
            if any(marker in text for marker in finished_markers):
                # 页面显示已交卷但没给分（老师设置了考后不公布/不显示分数）
                await asyncio.sleep(3)
                text = await self._collect_text()
                if any(marker in text for marker in finished_markers):
                    return {
                        "score": None,
                        "score_text": "",
                        "submit_note": "平台已收卷，但没有回显分数（老师可能关闭了成绩显示）",
                        "page_text": text[:500],
                    }
            await asyncio.sleep(2)
        return {
            "score": None,
            "score_text": "",
            "submit_note": "交卷后 90 秒内没有读到结果",
            "page_text": last_text[:500],
        }

    async def run_section_quiz(
        self,
        chapter_id: str,
        *,
        confirm: bool = False,
        num: int = 1,
        on_items: Callable[[list[dict[str, Any]]], None] | None = None,
        force: bool = False,
    ) -> QuizReport:
        """章节测验全链路：打开 → 解析 → 作答计划 → 填 → 核验 →（可选）交卷。"""
        report = QuizReport()
        frame = await self.open_section_quiz(chapter_id, num=num)
        work = await self.parse_work(frame)
        await self.set_questions(work)
        report.title = work.title
        self.emit(
            "info",
            f"章节测验「{work.title or chapter_id}」：{len(work.questions)} 题"
            f"（字体解码{'已启用' if work.deobfuscated else '未启用'}）",
        )
        report.items = await self.plan(work, frame=frame, on_items=on_items)
        if on_items is not None and not (frame is not None and self._ocr_supported()):
            on_items(self.items_payload(report.items))
        await self.fill(frame, report.items)
        await self.verify(frame, report.items)
        if on_items is not None:
            on_items(self.items_payload(report.items))
        if confirm:
            blocked = submit_block_reason(report.items, force=force)
            if blocked:
                report.submit_note = blocked
                self.emit("warn", f"暂不交卷：{blocked}")
                confirm = False
        if confirm:
            result = await self.submit(frame, confirm=True)
            report.submitted = bool(result.get("submitted"))
            report.score = result.get("score")
            report.score_text = str(result.get("score_text") or "")
            report.submit_note = str(result.get("submit_note") or "")
            report.submit_detail = json.dumps(
                {k: v for k, v in result.items() if k != "page_text"},
                ensure_ascii=False,
            )[:300]
        else:
            await self.submit(frame, confirm=False)
        report.finished_at = time.time()
        return report


__all__ = ["ChaoxingQuiz", "PlanItem", "QuizReport", "parse_score_text"]
