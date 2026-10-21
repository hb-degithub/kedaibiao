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
        else:
            parts.append("未提交（干跑）")
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
    ) -> None:
        self.page = page
        self.course_id = str(course_id)
        self.clazz_id = str(clazz_id)
        self.cpi = str(cpi or "")
        self.answerer = answerer
        self._emit = emit or (lambda level, message: log.info("%s", message))
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

    async def open_exam_item(self, item: quiz_mod.ExamItem) -> PwPage:
        """打开考试/作业：须知页 → 勾选同意 → 进入考试 → 返回答题页。"""
        url = EXAM_NOTES_URL.format(
            courseid=self.course_id,
            clazzid=self.clazz_id,
            exam=item.exam_id,
            cpi=self.cpi,
        )
        await self.page.goto(url, timeout=90000)
        self.emit("info", f"考试须知：{item.title}（{item.status}）")
        # 勾「我已阅读并同意」，再点「进入考试」
        for _ in range(3):
            with contextlib.suppress(PwError):
                await self.page.click(".face_agreement", timeout=8000)
            with contextlib.suppress(PwError):
                await self.page.click("#startBtn", timeout=8000)
            await asyncio.sleep(3)
            if "examnotes" not in self.page.url:
                break
        # 答题页可能开在新标签里
        context = getattr(self.page.raw, "context", None)
        deadline = time.time() + 60
        while time.time() < deadline:
            pages = list(getattr(context, "pages", []) or [])
            for candidate in pages:
                if "reVersionTestStartNew" in candidate.url or "/test/" in candidate.url:
                    return PwPage(candidate)
            if "examnotes" not in self.page.url:
                return self.page
            await asyncio.sleep(2)
        raise PwError("进入考试后没有找到答题页（可能被风控/需要人脸）")

    # -- 解析与作答计划 ---------------------------------------------------

    async def parse_work(self, frame: Any) -> quiz_mod.Work:
        html = await frame.html()
        return quiz_mod.parse_work(html)

    def _core_question(self, question: quiz_mod.Question) -> CoreQuestion:
        qtype = _QTYPE_MAP.get(int(question.qtype), QuestionType.UNKNOWN)
        images = [f"[图片{i + 1}]" for i in range(len(question.images))]
        stem = (question.text or "").strip()
        if images and not stem:
            stem = "（题面是图片）"
        return CoreQuestion(
            stem=stem,
            type=qtype,
            options=[opt.text or " ".join(images) for opt in question.options],
            remote_id=question.qid,
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
        """交卷。``confirm=False`` 只定位提交按钮（干跑），不点击。"""
        script = quiz_mod.submit_script(dry_run=not confirm)
        result = await frame.eval(script)
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
        await asyncio.sleep(3)
        result["page_text"] = (await self.page.text("body"))[:300]
        result["submitted"] = True
        return result

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
        await self.fill(frame, report.items)
        await self.verify(frame, report.items)
        if confirm:
            result = await self.submit(frame, confirm=True)
            report.submitted = bool(result.get("submitted"))
            report.submit_detail = json.dumps(result, ensure_ascii=False)[:300]
        else:
            await self.submit(frame, confirm=False)
        report.finished_at = time.time()
        return report


__all__ = ["ChaoxingQuiz", "PlanItem", "QuizReport"]
