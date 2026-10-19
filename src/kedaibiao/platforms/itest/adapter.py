"""iTEST 平台适配器（只读：登录 → 考试列表 → 换答题窗口令牌）。

能力边界（刻意）：
- ``fetch_questions = False``：取卷要进答题窗口（需浏览器设备检测产生的
  ``envTest*`` / ``iTMExam`` cookie），而且**一进窗口就开始计时**；
- ``submit = False``：交卷是不可逆写操作，不在范围内；
- ``course_playback = False``：考试平台没有「刷课」概念。

所以这个适配器提供的是「看得见考试、拿得到入口」，不代人考试。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from kedaibiao.core.models import (
    Assignment,
    Course,
    Credentials,
    Question,
    SubmissionResult,
    TaskKind,
)
from kedaibiao.platforms.base import (
    AdapterContext,
    Capabilities,
    NotSupportedError,
    PlatformAdapter,
)
from kedaibiao.platforms.itest.client import ExamEntry, ItestClient

log = logging.getLogger(__name__)

#: 考试平台没有班级概念（对客户端而言），用一门合成课程承载
SYNTHETIC_COURSE_ID = "itest-exams"
SYNTHETIC_COURSE_NAME = "iTEST 考试"


def _parse_time(value: str) -> datetime | None:
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(value.strip(), fmt)
        except (ValueError, AttributeError):
            continue
    return None


@dataclass(slots=True)
class _ExamMeta:
    entry: ExamEntry


class ItestAdapter(PlatformAdapter):
    name = "itest"
    display_name = "iTEST 智能测评"
    capabilities = Capabilities(
        login=True,
        list_courses=True,
        list_assignments=True,
        # 取题与提交都走「浏览器作答链路」（exam.py），不是通用的 fetch_questions/submit：
        # 试卷结构是 iTEST 专有的（填空填字母 / 选项被打乱 / 翻译写整句），
        # 由 answer.py 生成答案单、exam.py 填表核验后再交。见 native_* 方法。
        fetch_questions=True,
        submit=True,
        course_playback=False,
    )

    #: 告诉服务层：这个平台的取题与提交走原生链路（需要浏览器），
    #: 由 native_plan / native_submit 实现，不走通用的 fetch_questions → submit。
    native_submit_flow = True

    def __init__(self, ctx: AdapterContext | None = None, *, timeout: float | None = None) -> None:
        super().__init__(ctx)
        self._client: ItestClient | None = None
        #: 浏览器求解成功后把 cookie 落在这里（由运行时注入）
        self._cookie_file: str | None = None
        self._timeout = timeout
        self._entry_cache: dict[str, ExamEntry] = {}

    # -- 客户端 -----------------------------------------------------------

    def client(self) -> ItestClient:
        if self._client is None:
            http = self.ctx.client()
            if self._timeout is not None:
                self._client = ItestClient(http, logger=self.log)
            else:
                self._client = ItestClient(http, logger=self.log)
        return self._client

    # -- 生命周期 ---------------------------------------------------------

    async def login(self, credentials: Credentials) -> bool:
        """登录。SSO 要验证码时自动退回浏览器求解。

        分两种情况：
        - 风控没触发 → 纯 HTTP 登录，快；
        - 风控要验证码（code 1506）→ 用浏览器触发 + 求解 + 注入登录，
          成功后把会话 cookie 落盘（与手动登录等价）。
        """
        from kedaibiao.platforms.itest.client import CaptchaRequired

        try:
            return await self.client().login(credentials.username, credentials.password)
        except CaptchaRequired:
            self.log.info("SSO 要求验证码，退回浏览器求解")

        from kedaibiao.platforms.unipus.geetest.browser import login_via_browser

        result = login_via_browser(
            platform="itest",
            username=credentials.username,
            password=credentials.password,
            cookie_file=self._cookie_file,
            on_event=self.log.info,
        )
        if not result.ok:
            self.log.warning("浏览器求解登录失败: %s", result.message)
            return False
        self._adopt_cookies(result.cookies)
        return True

    def _adopt_cookies(self, cookies: list[dict[str, Any]]) -> None:
        """把浏览器拿到的 cookie 灌进当前 HTTP 会话，后续请求立刻可用。"""
        jar = getattr(self.ctx.client(), "transport", None)
        store = getattr(jar, "cookies", None)
        if store is None:
            return
        for item in cookies:
            try:
                store.set(item["name"], item.get("value", ""),
                          domain=item.get("domain", ""), path=item.get("path") or "/")
            except Exception:  # noqa: BLE001 - 单个 cookie 失败不影响其它
                continue

    async def is_logged_in(self) -> bool:
        return await self.client().is_logged_in()

    # -- 数据 -------------------------------------------------------------

    async def list_courses(self) -> list[Course]:
        """合成一门课程承载考试列表 —— 考试平台对学生没有「课程」层。"""
        return [
            Course(
                remote_id=SYNTHETIC_COURSE_ID,
                name=SYNTHETIC_COURSE_NAME,
                teacher="",
                term="",
                raw={"synthetic": True, "reason": "iTEST 对学生没有课程层，考试直接挂在账号下"},
            )
        ]

    async def list_assignments(self, course: Course) -> list[Assignment]:
        exams = await self.client().list_exams()
        self._entry_cache = {e.exam_id: e for e in exams}
        out: list[Assignment] = []
        for exam in exams:
            deadline = _parse_time(exam.end_at)
            raw = exam.raw or {}
            finished = str(raw.get("finish") or "0") in ("1", "True", "true")
            score = raw.get("score")
            out.append(
                Assignment(
                    remote_id=exam.exam_id,
                    title=exam.title or f"考试 {exam.exam_id}",
                    kind=TaskKind.EXAM,
                    course_name=course.name,
                    deadline=deadline,
                    progress=1.0 if finished else 0.0,
                    finished=finished,
                    url="",
                    raw={
                        "finished": finished,
                        "score": score,
                        "exam_code": exam.code,
                        "start_at": exam.start_at,
                        "end_at": exam.end_at,
                        "duration": exam.duration,
                        "kind_source": "exam-list",
                    },
                )
            )
        return out

    async def fetch_questions(self, assignment: Assignment) -> list[Question]:
        """取卷：走浏览器（设备检测的 cookie 只有那边长得出来），一进窗口就开始计时。

        通用契约这里返回 :class:`kedaibiao.core.models.Question` 只是为了占位，
        真正给控制台用的是 :meth:`native_plan` / :meth:`native_submit`。
        """
        raise NotSupportedError(
            "iTEST 的取卷要在作答页里做（会开始计时），请用控制台「进考场取卷」动作"
        )

    async def submit(self, assignment: Assignment, answers: list[Any]) -> SubmissionResult:
        raise NotSupportedError("iTEST 的交卷要走 native_submit（浏览器里填+交）")

    # -- 原生作答链路（控制台「作业」页的提交走这里）------------------------

    def cache_settings(self) -> Any:
        """缓存用的 settings（没有就退回默认路径）。"""
        from types import SimpleNamespace

        return getattr(self.ctx, "settings", None) or SimpleNamespace(
            session_dir="./data/sessions"
        )

    async def native_plan(self, exam_id: str) -> dict[str, Any]:
        """干跑：读缓存试卷 + 答案单，告诉控制台「打算交什么」。**不碰平台。**"""
        import asyncio

        return await asyncio.to_thread(self._native_plan_blocking, exam_id)

    async def native_submit(self, exam_id: str) -> dict[str, Any]:
        """真交：进考场 → 填答案 → 逐项核验 → 交卷 → 回查服务端 finish。

        不可逆。调用方（服务层）只在人工确认后才调到这里。
        """
        import asyncio

        return await asyncio.to_thread(self._native_submit_blocking, exam_id)

    def _native_plan_blocking(self, exam_id: str) -> dict[str, Any]:
        import asyncio

        from kedaibiao.platforms.itest import cache
        from kedaibiao.platforms.itest.answer import build_sheet
        from kedaibiao.platforms.itest.paper import Paper
        from kedaibiao.tasks.llm import LLMAnswerer, LLMConfig

        settings = self.cache_settings()
        paper_raw = cache.read_json(cache.paper_path(settings, exam_id))
        if paper_raw is None:
            raise NotSupportedError(
                f"还没有这场考试的试卷缓存（{cache.paper_path(settings, exam_id)}）。"
                "先在控制台点「进考场取卷」（会开始/继续计时），再来提交。"
            )
        paper = Paper.from_dict(paper_raw)

        sheet_raw = cache.read_json(cache.answers_path(settings, exam_id)) or {}
        answers: dict[str, str] = {
            str(k): str(v) for k, v in (sheet_raw.get("answers") or {}).items()
        }
        skipped = list(sheet_raw.get("skipped") or [])

        # 缓存里缺答案、而现在又装了语音模型 → 重算一次（早先没有转写时生成的缓存
        # 会缺听力，不自愈的话「提交」会一直被拒）
        missing_cached = [q.qid for sec in paper.sections for q in sec.questions
                          if not str(answers.get(q.qid) or "").strip()]
        audio_sections = any(sec.audios for sec in paper.sections)
        from kedaibiao.platforms.itest import stt as _stt

        if answers and missing_cached and audio_sections and _stt.available():
            answers = {}

        if not answers:
            config = LLMConfig.from_env(getattr(self.ctx, "environ", {}) or {})
            if not config.enabled:
                raise NotSupportedError(
                    "还没有答案单，而且大模型没配置 —— 先点「AI 答题」（或在设置页配好大模型）"
                )
            llm = LLMAnswerer(config)
            sheet, transcripts, _note = asyncio.run(build_sheet(
                paper, llm, cache.cache_dir(settings),
                environ=getattr(self.ctx, "environ", {}) or {},
            ))
            answers = sheet.answers
            skipped = sheet.skipped
            cache.write_json(
                cache.answers_path(settings, exam_id),
                {"answers": answers, "skipped": skipped,
                 "transcripts": {str(k): v for k, v in transcripts.items()}},
            )

        rows: list[dict[str, Any]] = []
        missing: list[str] = []
        for section in paper.sections:
            for question in section.questions:
                answer = answers.get(question.qid, "")
                unanswered = not str(answer).strip()
                if unanswered:
                    missing.append(question.qid)
                reason = ""
                if unanswered:
                    reason = next(
                        (str(s.get("reason") or "") for s in skipped
                         if str(s.get("qid")) == question.qid),
                        "没有答案",
                    )
                rows.append({
                    "index": question.number or question.qid,
                    "qid": question.qid,
                    "kind": question.kind,
                    "stem": (question.text or question.qid)[:120],
                    "answer": answer,
                    "confidence": 0.9 if answer else None,
                    "needs_human": unanswered,
                    "reason": reason,
                })

        return {
            "platform": self.name,
            "exam_id": exam_id,
            "title": next((s.part1 for s in paper.sections if s.part1), paper.title),
            "question_count": paper.question_count,
            "slot_count": paper.blank_count,
            "answered": len(answers),
            "missing": missing,
            "dry_run": True,
            "can_submit": not missing,
            "note": (
                "干跑结果，没有向平台提交任何内容。"
                + (f" 还有 {len(missing)} 题没答案（听力需要音频转写或人工补），"
                   "补齐后才会允许交卷。" if missing else " 全部题目都有答案，可以确认交卷。")
            ),
            "answers": rows,
        }

    def _native_submit_blocking(self, exam_id: str) -> dict[str, Any]:
        from kedaibiao.platforms.itest import cache
        from kedaibiao.platforms.itest.exam import ExamRunner, cookies_from_jar

        plan = self._native_plan_blocking(exam_id)
        if plan["missing"]:
            raise NotSupportedError(
                f"还有 {len(plan['missing'])} 题没答案，拒绝交卷（交卷后不能改）。"
                "先补齐答案单再来。"
            )
        answers = {row["qid"]: row["answer"] for row in plan["answers"] if row["answer"]}

        client = self.client()
        jar = getattr(getattr(client, "http", None), "transport", None)
        cookies = cookies_from_jar(getattr(jar, "cookies", None))
        if not cookies:
            raise NotSupportedError("没有可用的登录会话 —— 先到「账号」页重新登录一次")

        settings = self.cache_settings()
        ks_id = str(((self._entry_cache.get(exam_id) or ExamEntry(exam_id="")).raw or {})
                    .get("ksId") or "")
        runner = ExamRunner(cookies)
        with runner:
            entered = runner.enter(exam_id, ks_id=ks_id)
            if not entered.ok:
                raise NotSupportedError(f"没能进入作答页：{entered.message}")
            runner.fill(answers)
            checked = runner.verify(answers)
            if checked.get("total"):
                raise NotSupportedError(
                    f"核验有 {checked['total']} 处不符，已拒绝交卷：{checked.get('bad')}"
                )
            submitted = runner.submit()
            shot = runner.save_screenshot(cache.cache_dir(settings) / f"submitted-{exam_id}.png")

        finish = None
        try:
            import asyncio

            entries = asyncio.run(self.client().list_exams())
            entry = next((e for e in entries if e.exam_id == exam_id), None)
            finish = (entry.raw or {}).get("finish") if entry else None
        except Exception:  # noqa: BLE001 - 复核失败不影响交卷结果本身
            self.log.debug("交卷后复核失败", exc_info=True)

        return {
            "platform": self.name,
            "exam_id": exam_id,
            "submitted": bool(submitted.get("ok")),
            "server_finish": finish,
            "page_text": submitted.get("text", ""),
            "steps": submitted.get("steps", []),
            "screenshot": shot,
            "dry_run": False,
        }

    # -- 扩展方法（不在契约里）--------------------------------------------

    async def answer_window_url(self, exam_id: str, exam_code: str = "") -> str:
        """换答题窗口 URL（**不会开始考试**，调研已核验）。

        这是给人用的：复制到浏览器里自己决定要不要开始。
        """
        answer_url = await self.client().judge_entry(exam_id, exam_code)
        return ItestClient.window_url(answer_url)

    async def exam_meta(self, exam_id: str) -> ExamEntry | None:
        """取某场考试的元信息（标题/起止/时长）。"""
        if not self._entry_cache:
            exams = await self.client().list_exams()
            self._entry_cache = {e.exam_id: e for e in exams}
        return self._entry_cache.get(exam_id)
