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
from kedaibiao.platforms.base import AdapterContext, Capabilities, PlatformAdapter
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
        fetch_questions=False,
        submit=False,
        course_playback=False,
    )

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
            out.append(
                Assignment(
                    remote_id=exam.exam_id,
                    title=exam.title or f"考试 {exam.exam_id}",
                    kind=TaskKind.EXAM,
                    course_name=course.name,
                    deadline=deadline,
                    progress=0.0,
                    finished=False,
                    url="",
                    raw={
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
        # 刻意不实现：取卷需要浏览器设备检测的 cookie，且一进窗口就开始计时
        return await super().fetch_questions(assignment)

    async def submit(self, assignment: Assignment, answers: list[Any]) -> SubmissionResult:
        # 刻意不实现：交卷是不可逆写操作
        return await super().submit(assignment, answers)

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
