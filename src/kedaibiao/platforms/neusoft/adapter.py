"""东软智慧教育平台适配器。

协议事实见 ``docs/research/neusoft.md``。这个适配器的形状由平台决定，不是设计偏好：

- **登录走 App 接口**（``/system/auth/app/login``），没有图形验证码。Web 端那套
  滑块（AJ-Captcha）完全不需要碰 —— 也就不存在「验证码对抗」这类合规问题。
- **课程按学期组织**：学期 → 课程 → 资源。作业却是**跨课程的学期任务列表**
  （``study-task/page`` 要 ``termId``），所以 ``list_assignments(course)`` 必须
  先按学期拉一次任务，再按课程归属过滤。
- **刷课与作业是两条完全不同的链路**：刷课调学习记录接口并以回读判定完成；
  作业走 ``smartexam`` 的 paper/enter/save/submit。

三条安全闸门：

1. 刷课的完成判定只看**回读** ``studyStatus == 2``，接口 ``code == 0`` 不算数；
2. 交卷不可逆，且写路径只有 APK 静态证据 —— 默认**只存草稿不交卷**，
   要交卷必须显式配 ``KEDAIBIAO_NEUSOFT_ALLOW_SUBMIT=1``；
3. 签到是不可逆写动作，已签 / 已结束 / 需定位却无坐标一律拦截在请求之前。

## 环境变量

| 变量 | 作用 |
| --- | --- |
| ``KEDAIBIAO_NEUSOFT_BASE_URL`` | 业务域名（默认 ``https://study.neusoft.edu.cn``） |
| ``KEDAIBIAO_NEUSOFT_TENANT_ID`` | 强制指定租户，跳过自动解析 |
| ``KEDAIBIAO_NEUSOFT_TERM_ID`` | 只处理指定学期（省掉逐学期探测） |
| ``KEDAIBIAO_NEUSOFT_ALLOW_SUBMIT`` | ``1`` 才允许真正交卷（默认只存草稿） |
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from kedaibiao.core.models import (
    Answer,
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
    PlatformAdapter,
)
from kedaibiao.platforms.neusoft import attendance as att
from kedaibiao.platforms.neusoft import endpoints as ep
from kedaibiao.platforms.neusoft import homework, resources, study, terms
from kedaibiao.platforms.neusoft.client import NeusoftClient
from kedaibiao.platforms.neusoft.errors import NeusoftApiError, NeusoftError
from kedaibiao.platforms.neusoft.models import NeusoftCourse, Resource, Session, Term

log = logging.getLogger(__name__)

#: 业务域名覆盖。生产域名随学校不同，所以必须可配。
ENV_BASE_URL = "KEDAIBIAO_NEUSOFT_BASE_URL"
#: 强制租户（跳过 ``tenant/app/option`` 解析）。
ENV_TENANT_ID = "KEDAIBIAO_NEUSOFT_TENANT_ID"
#: 只处理指定学期。
ENV_TERM_ID = "KEDAIBIAO_NEUSOFT_TERM_ID"
#: 允许真正交卷。默认关闭 —— 写路径只有 APK 静态证据。
ENV_ALLOW_SUBMIT = "KEDAIBIAO_NEUSOFT_ALLOW_SUBMIT"

#: 交卷闸门的提示语，测试与文档共用一份，避免两处措辞漂移。
SUBMIT_BLOCKED_MESSAGE = (
    "已保存作答草稿，但未交卷：交卷写路径只有 APK 静态分析证据（[B]），"
    f"确认要交请设置 {ENV_ALLOW_SUBMIT}=1 后重跑"
)


@dataclass(slots=True)
class PlaybackReport:
    """一次刷课的结果。**以回读为准**，不以「请求成功」为准。"""

    completed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed


class NeusoftAdapter(PlatformAdapter):
    """东软智慧教育（NEUMOOC）适配器。"""

    name = "neusoft"
    display_name = "东软智慧教育"
    capabilities = Capabilities(
        login=True,
        list_courses=True,
        list_assignments=True,
        fetch_questions=True,
        submit=True,
        course_playback=True,
    )
    #: 这个平台没有「某门课不存在」这类可跳过的课程级业务错误，
    #: 任何失败都应当上抛 —— 否则会把会话过期伪装成「这门课没作业」。
    skippable_course_errors = ()
    #: 提交闸门说明：控制台把这句话放在干跑计划里，用户点「确认真提交」之前
    #: 就该知道这个平台默认**只存草稿不交卷**（写路径只有 APK 静态证据）。
    submit_gate_hint = SUBMIT_BLOCKED_MESSAGE

    def __init__(self, ctx: AdapterContext | None = None) -> None:
        super().__init__(ctx)
        environ = self.ctx.environ or {}
        self.allow_submit = _env_flag(environ.get(ENV_ALLOW_SUBMIT, ""))
        self._pinned_term_id = (environ.get(ENV_TERM_ID) or "").strip()
        self.client = NeusoftClient(
            self.ctx.client(),
            base_url=(environ.get(ENV_BASE_URL) or ep.DEFAULT_BASE_URL).strip(),
            tenant_id=(environ.get(ENV_TENANT_ID) or "").strip(),
        )
        #: 一次运行内缓存，避免同一份学期任务列表被反复拉取。
        self._terms: list[Term] | None = None
        self._tasks_by_term: dict[str, list[homework.StudyTask]] = {}
        self._papers: dict[str, homework.Paper] = {}

    # -- 会话 -------------------------------------------------------------

    async def login(self, credentials: Credentials) -> bool:
        """App 登录（免验证码）。

        只把「凭据错」当失败返回 False；网络与协议错误照常上抛 ——
        前者重试没用，后者让调度器按退避重试才是对的。
        """
        try:
            await self.client.login(credentials.username, credentials.password)
        except NeusoftApiError as exc:
            self.log.warning("东软登录失败: %s", exc.message or exc)
            return False
        self.log.info("东软登录成功: userId=%s", self.client.user_id)
        return True

    async def is_logged_in(self) -> bool:
        return await self.client.is_logged_in()

    #: 上一次 :meth:`refresh_session` 失败的原因（服务层据此给出可操作的文案）
    last_session_error: str = ""

    async def refresh_session(self, credentials: Credentials) -> bool:
        """会话失效时的统一入口：探测 → refresh_token 续期 → 密码重登。

        比基类多一步「用 refresh_token 续期」：账号库不存密码，从磁盘恢复的
        会话没有凭据，光靠密码重登会把「会话过期」变成「必须回账号页重绑」。

        两个坑（2026-10-22 实测）：

        - ``is_logged_in`` 探的是 ``USER_PROFILE``，平台在 token 过期后**仍可能
          回它正常**，等到拉课程才 401 —— 所以「探测通过」不等于「能干活」，
          上层必须把后续 401 翻译成「重新登录」；
        - 账号库解析出来的 ``Credentials.password`` 是空串，拿它去 ``login``
          只会得到一个没有意义的失败。没有密码就不要试，直接说明要人工重登。
        """
        if await self.is_logged_in():
            return True
        renewed = await self.client.refresh()
        if renewed:
            # 续期成功后要再确认一次：拿到 token ≠ 真能用
            if await self.is_logged_in():
                self.last_session_error = ""
                return True
            self.last_session_error = "刷新令牌换到了 token，但仍无法访问"
        else:
            self.last_session_error = (
                f"刷新令牌也失效（{self.client.refresh_error or '平台拒绝刷新'}）"
            )
        self.log.info("东软会话已过期：%s —— 需要重新登录一次", self.last_session_error)
        if not (credentials.password or "").strip():
            # 账号库不存密码，这里必定失败；别把「该人工重登」伪装成「登录失败」。
            self.last_session_error += "；账号库里没有密码，请到「账号」页重新登录一次"
            return False
        ok = await self.login(credentials)
        if ok and not await self.is_logged_in():
            self.last_session_error += "；密码登录返回成功但会话仍不可用"
            return False
        return ok

    def export_session(self) -> dict[str, Any] | None:
        """导出会话。

        这个平台的会话是 bearer token，不在 cookie 里；账号库又不存密码，
        所以不落这份快照的话，进程一重启就只能重新绑定账号。
        """
        data = self.client.session.to_dict()
        if not data.get("access_token"):
            return None
        data["base_url"] = self.client.base_url
        return data

    def import_session(self, data: dict[str, Any]) -> bool:
        session = Session.from_dict(data)
        if not session.access_token:
            return False
        base_url = str(data.get("base_url") or "").strip()
        # 换学校（换域名）时旧 token 不能用，宁可当作没有会话
        if base_url and base_url.rstrip("/") != self.client.base_url:
            self.log.info("会话域名与当前配置不一致，忽略旧会话")
            return False
        self.client.load_session(session)
        return True

    async def close(self) -> None:
        """无长连接需要释放（urllib 每次请求自建连接）。"""

    # -- 课程 -------------------------------------------------------------

    async def list_terms(self) -> list[Term]:
        """学期列表（带缓存）。"""
        if self._terms is None:
            self._terms = await terms.list_terms(self.client)
        return self._terms

    async def list_courses(self) -> list[Course]:
        """列全部学期的课程，翻译成 ``core.Course``。

        ``raw`` 里带上 ``teach_class_id`` / ``term_id``：拉资源和拉学期任务都要
        这两把钥匙，而 ``core.Course`` 没有它们的位置。
        """
        all_terms = await self.list_terms()
        if self._pinned_term_id:
            all_terms = [t for t in all_terms if t.id == self._pinned_term_id]
            if not all_terms:
                self.log.warning("配置的学期 %s 不在学期列表里", self._pinned_term_id)
        courses: list[Course] = []
        for term in all_terms:
            for item in await resources.list_courses(self.client, term.id):
                courses.append(_to_core_course(item, term))
        self.log.info("东软：%d 个学期，共 %d 门课", len(all_terms), len(courses))
        return courses

    async def list_course_resources(self, course: Course) -> list[Resource]:
        """列某门课的资源（视频 / 文档 / 图片）。"""
        return await resources.list_resources(self.client, _to_platform_course(course))

    # -- 作业 -------------------------------------------------------------

    async def list_assignments(self, course: Course) -> list[Assignment]:
        """列某门课的作业。

        平台的学习任务是**跨课程的学期列表**，所以这里拉一次学期任务再按课程过滤。
        过滤优先看 ``teachCourseId``，平台没给时退化为课程名匹配。
        """
        time = course.raw or {}
        term_id = str(time.get("term_id") or "")
        if not term_id:
            self.log.warning("课程 %s 缺 term_id，跳过作业抓取", course.name)
            return []
        tasks = await self._tasks_for_term(term_id)
        matched = [task for task in tasks if _task_belongs(task, course)]
        self.log.debug("东软：%s 命中 %d 个作业", course.name, len(matched))
        return [_to_core_assignment(task) for task in matched]

    async def _tasks_for_term(self, term_id: str) -> list[homework.StudyTask]:
        cached = self._tasks_by_term.get(term_id)
        if cached is None:
            cached = await homework.list_study_tasks(self.client, term_id)
            self._tasks_by_term[term_id] = cached
        return cached

    async def fetch_questions(self, assignment: Assignment) -> list[Question]:
        """取题目正文。``remote_id`` 就是 ``ctlId``（= ``testId``）。"""
        paper = await self._paper(assignment.remote_id)
        return homework.to_core_questions(paper)

    async def _paper(self, test_id: str) -> homework.Paper:
        paper = self._papers.get(test_id)
        if paper is None:
            paper = await homework.get_paper(self.client, test_id)
            self._papers[test_id] = paper
        return paper

    async def submit(
        self, assignment: Assignment, answers: list[Answer]
    ) -> SubmissionResult:
        """保存作答；只有显式放行时才真正交卷。

        为什么默认不交卷：交卷不可逆（不能改答案、次数照扣、平台无撤销接口），
        而这条写路径目前只有 APK 静态分析证据，没有真实样本复跑。所以默认
        **存草稿**并如实告诉调用方「没交」—— 让用户自己决定何时开闸。
        """
        if not answers:
            return SubmissionResult(
                assignment_id=assignment.remote_id, ok=False, message="没有可提交的答案"
            )
        paper = await self._paper(assignment.remote_id)
        entered = await homework.enter_task(self.client, assignment.remote_id)
        if not entered.is_ready:
            return SubmissionResult(
                assignment_id=assignment.remote_id,
                ok=False,
                message="enter 未返回 candidateId/historyId，无法作答",
                raw=entered.raw,
            )
        rows = build_answer_rows(paper, answers)
        if not rows:
            return SubmissionResult(
                assignment_id=assignment.remote_id,
                ok=False,
                message="没有一条答案能映射到平台题目（题干/选项对不上）",
            )
        saved = await homework.save_answers(self.client, entered, rows)
        # 协议档案 §5.3：保存返回体里 ``isSubmitPaper`` 非空 = 服务端**顺手交了卷**。
        # 那种情况下再 PUT 一次 submit 就是重复交卷，可能被平台记成异常；
        # 这里如实按「已交卷」返回，不再补一刀。
        if saved.get("isSubmitPaper"):
            return SubmissionResult(
                assignment_id=assignment.remote_id,
                ok=True,
                message=f"已交卷（保存草稿时平台同时受理了提交：isSubmitPaper={saved.get('isSubmitPaper')}）",
                raw={**saved, "server_finish": True},
            )
        if not self.allow_submit:
            self.log.warning(SUBMIT_BLOCKED_MESSAGE)
            return SubmissionResult(
                assignment_id=assignment.remote_id,
                ok=False,
                message=SUBMIT_BLOCKED_MESSAGE,
                raw=saved,
            )
        await homework.submit_paper(self.client, entered.history_id, confirm=True)
        return SubmissionResult(
            assignment_id=assignment.remote_id,
            ok=True,
            message="已交卷（写路径 [B]，建议到平台确认成绩）",
            raw={**saved, "server_finish": True},
        )

    # -- 刷课 -------------------------------------------------------------

    async def play_course(
        self,
        course: Course,
        *,
        only_pending: bool = True,
        limit: int | None = None,
    ) -> PlaybackReport:
        """刷完一门课的可完成资源。

        每一条都以**回读 ``studyStatus == 2``** 判定成功；``limit`` 用于灰度
        试跑（先刷 1 个验证账号状态，再放量）。
        """
        platform_course = _to_platform_course(course)
        items = await resources.list_resources(self.client, platform_course)
        report = PlaybackReport()
        done = 0
        for item in items:
            if not item.is_completable:
                continue
            if only_pending and item.is_completed:
                report.skipped.append(item.res_id)
                continue
            if limit is not None and done >= limit:
                break
            done += 1
            ok = await study.complete_resource(self.client, platform_course, item)
            (report.completed if ok else report.failed).append(item.res_id)
        self.log.info(
            "东软刷课：完成 %d，跳过 %d，失败 %d",
            len(report.completed),
            len(report.skipped),
            len(report.failed),
        )
        return report

    # -- 考勤 -------------------------------------------------------------

    async def list_attendance(self, term_id: str = "") -> list[att.Attendance]:
        """列某学期的考勤场次。默认取当前登录用户的 ``studentId``。"""
        resolved = term_id or self._pinned_term_id or await self._current_term_id()
        return await att.list_attendance(self.client, self.client.user_id, resolved)

    async def _current_term_id(self) -> str:
        """没指定学期时取学期列表的第一个（平台按新→旧返回）。"""
        all_terms = await self.list_terms()
        if not all_terms:
            raise NeusoftError("学期列表为空，无法查考勤")
        return all_terms[0].id

    async def sign(
        self,
        item: att.Attendance,
        *,
        latitude: str | None = None,
        longitude: str | None = None,
        address: str = "",
    ) -> bool:
        """提交签到。三条守卫在 :func:`attendance.submit_sign` 里，被拦时不发请求。"""
        if not item.attendance_id:
            self.log.warning("考勤记录缺 attendanceId，跳过")
            return False
        detail = await att.detail_id(self.client, item.attendance_id, self.client.user_id)
        if not detail:
            # 空 id = 不在该教学班，这是业务状态而不是错误
            self.log.warning("考勤 %s 找不到本人明细（可能不在该教学班），跳过", item.attendance_id)
            return False
        await att.submit_sign(
            self.client,
            item,
            detail_id=detail,
            latitude=latitude,
            longitude=longitude,
            address=address,
        )
        return True


# --------------------------------------------------------------------------
# 模型换算
# --------------------------------------------------------------------------


def _env_flag(raw: str) -> bool:
    return (raw or "").strip().lower() in {"1", "true", "yes", "on"}


def _to_core_course(item: NeusoftCourse, term: Term) -> Course:
    return Course(
        remote_id=item.teach_course_id,
        name=item.name or item.teach_course_id,
        teacher=item.teacher,
        term=term.name,
        raw={
            "teach_class_id": item.teach_class_id,
            "term_id": term.id,
            "teach_course_id": item.teach_course_id,
        },
    )


def _to_platform_course(course: Course) -> NeusoftCourse:
    raw = course.raw or {}
    return NeusoftCourse(
        teach_course_id=str(raw.get("teach_course_id") or course.remote_id),
        teach_class_id=str(raw.get("teach_class_id") or ""),
        name=course.name,
        term_id=str(raw.get("term_id") or ""),
        teacher=course.teacher,
    )


def _task_belongs(task: homework.StudyTask, course: Course) -> bool:
    """判断一个学习任务是否属于这门课。

    平台把 ``teachCourseId`` 放在任务行里，但档案里没有把它摘出来（``[C]``），
    所以先用它、再退化为课程名精确匹配。两者都没有就判不属 —— 宁可漏，
    也不要把别的课的作业算到这门课上（算错课程归属会让「这门课没作业」变成假阴性）。
    """
    raw = task.raw or {}
    task_course_id = str(raw.get("teachCourseId") or raw.get("teachCourseID") or "")
    if task_course_id:
        return task_course_id == course.remote_id
    return bool(task.course_name) and task.course_name == course.name


def _to_core_assignment(task: homework.StudyTask) -> Assignment:
    #: type=2 是单元测试 / 测验，type=3 是作业；两者同一套交卷链路，只是种类不同
    kind = (
        TaskKind.EXAM
        if task.task_type == ep.TASK_KIND_TEST
        else TaskKind.HOMEWORK
    )
    return Assignment(
        remote_id=task.test_id,
        title=task.name or task.test_id,
        kind=kind,
        course_name=task.course_name,
        finished=task.is_completed,
        progress=1.0 if task.is_completed else 0.0,
        raw=dict(task.raw),
    )


def build_answer_rows(
    paper: homework.Paper, answers: list[Answer]
) -> list[dict[str, str]]:
    """把 ``core.Answer`` 翻译成平台 ``answer/save`` 要的行。

    两个关键换算：

    - **题目定位**：``paperId`` + ``questionNo`` + ``questionType`` 三项要原样回传
      （§5.3）。索引必须用**逐题唯一**的键（:attr:`PaperQuestion.key`）：
      ``paperId`` 整份试卷共用一个值，拿它建索引会把所有题压成一条，
      交卷时就报「没有一条答案能映射到平台题目」；
    - **选项答案**：平台的 ``standardAnswer`` 是**逗号拼接的 ``choiceId``** 形状
      （§5.4 的三态字段），所以选择索引要换成 ``choice_id``，不是字母也不是文案。
      ⚠️ 真实响应里 ``choiceId`` 是 ``A``/``B``/``C``/``D`` 且**顺序是打乱的**
      （选项数组不是 A、B、C、D 的顺序），所以只能按数组下标取 ``choice_id``，
      不能拿位置当字母用。
    """
    by_key = {item.key: item for item in paper.questions if item.key}
    rows: list[dict[str, str]] = []
    for answer in answers:
        remote_id = answer.question.remote_id or ""
        item = by_key.get(remote_id)
        if item is None:
            continue
        text = _answer_text(answer, item)
        if not text:
            continue
        rows.append(
            {
                "paperId": item.paper_id,
                "questionNo": item.question_no,
                "questionType": item.question_type,
                "answer": text,
            }
        )
    return rows


def _answer_text(answer: Answer, item: homework.PaperQuestion) -> str:
    """把一条答案序列化成平台要的字符串。"""
    choices = [index for index in answer.choices if 0 <= index < len(item.choices)]
    if choices:
        return ",".join(item.choices[index].choice_id for index in choices)
    return answer.text.strip()


__all__ = [
    "NeusoftAdapter",
    "PlaybackReport",
    "SUBMIT_BLOCKED_MESSAGE",
    "build_answer_rows",
]
