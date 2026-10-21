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
    ) -> None:
        self.page = page
        self.course_id = str(course_id)
        self.clazz_id = str(clazz_id)
        self.cpi = str(cpi or "")
        self.answerer = answerer
        self._emit = emit or (lambda level, message: log.info("%s", message))
        #: 需要人工认证（人脸/验证码）时的钩子：``await human_gate(kind, message) -> bool``
        self.human_gate = human_gate
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

    async def _page_has_face_capture(self) -> bool:
        """考试须知/答题页是否出现人脸识别层。"""
        html = ""
        with contextlib.suppress(PwError):
            html = await self.page.html()
        return any(
            marker in html
            for marker in ("人脸比对", "reFaceRecognition", "face_Box", "人脸识别")
        )

    async def open_exam_item(self, item: quiz_mod.ExamItem) -> PwPage:
        """打开考试/作业：须知页 → 勾选同意 → 进入考试 → 返回答题页。

        - 「只允许手机 APP 考试」直接拒绝（网页端没有这条路）；
        - 考试人脸识别不代过：检测到就调 ``human_gate`` 等人工完成，再重试进入；
        - 答题页可能开在新标签里，按 URL/标题特征挑出来。
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
            with contextlib.suppress(PwError):
                await self.page.click(".face_agreement", timeout=8000)
            await asyncio.sleep(0.5)
            with contextlib.suppress(PwError):
                await self.page.click("#startBtn", timeout=8000)
            await asyncio.sleep(4)
            if await self._page_has_face_capture():
                handled = await self._wait_human(
                    "face",
                    "考试要求人脸识别：请在可见的浏览器窗口里完成认证后再继续",
                )
                if not handled:
                    raise PwError("考试需要人脸识别，未完成认证（可用 --show 打开窗口人工认证）")
                continue
            answer_page = await self._pick_answer_page()
            if answer_page is not None:
                return answer_page
        raise PwError("进入考试后没有找到答题页（可能需要人脸识别/被风控）")

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

    async def run_exam(
        self,
        item: quiz_mod.ExamItem,
        *,
        confirm: bool = False,
        max_seconds: float = 0.0,
    ) -> QuizReport:
        """作业/考试全链路：进入 → 解析 → 作答 → 填卷 → 核验 →（可选）交卷 → 回读分数。"""
        report = QuizReport(title=item.title)
        page: Any = None
        try:
            page = await self.open_exam_item(item)
            self.emit("info", f"已进入答题页：{page.url[:120]}")
            html = await page.html()
            work = quiz_mod.parse_work(html)
            if not work.questions:
                # 题目可能在 iframe 里
                for frame in await page.frames():
                    with contextlib.suppress(PwError):
                        work = quiz_mod.parse_work(await frame.html())
                    if work.questions:
                        page = frame
                        break
            await self.set_questions(work)
            report.title = work.title or item.title
            self.emit(
                "info",
                f"「{report.title}」：{len(work.questions)} 题"
                f"（字体解码{'已启用' if work.deobfuscated else '未启用'}）",
            )
            report.items = await self.plan(work)
            filled = await self.fill(page, report.items)
            await self.verify(page, report.items)
            if confirm and filled == 0:
                report.submit_note = "一题都没填上，拒绝交卷（避免交白卷）"
                self.emit("warn", report.submit_note)
                confirm = False
            if confirm:
                result = await self._exam_submit(page, confirm=True)
                report.submitted = bool(result.get("submitted"))
                if report.submitted:
                    outcome = await self.wait_result(timeout=max(max_seconds, 90.0))
                    report.score = outcome.get("score")
                    report.score_text = str(outcome.get("score_text") or "")
                    report.submit_note = str(outcome.get("submit_note") or "")
            else:
                await self._exam_submit(page, confirm=False)
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

    async def plan(self, work: quiz_mod.Work) -> list[PlanItem]:
        """每题调用作答器，得到字母/文本；不碰页面。"""
        items: list[PlanItem] = []
        answerable = [q for q in work.questions if not q.unsupported]
        answers: dict[str, Answer] = {}
        if self.answerer is not None and answerable:
            produced = await self.answerer.answer(
                [self._core_question(question) for question in answerable]
            )
            for question, answer in zip(answerable, produced, strict=True):
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
            answer = answers.get(question.qid)
            letters, text_answer, confidence, source = self._answer_to_letters(question, answer)
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
        report.items = await self.plan(work)
        filled = await self.fill(frame, report.items)
        await self.verify(frame, report.items)
        if confirm and filled == 0:
            report.submit_note = "一题都没填上，拒绝交卷（避免交白卷）"
            self.emit("warn", report.submit_note)
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
