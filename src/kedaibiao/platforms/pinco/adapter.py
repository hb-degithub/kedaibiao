"""希沃品课（Pinco）学生端适配器。

协议事实与证据等级见 :mod:`endpoints`。会话有两条来源：

1. **接管浏览器会话（主路径）**：``PincoAdapter.adopt_session()`` 或
   ``KEDAIBIAO_PINCO_TOKEN``。应用层 RPC 的 csrf 与浏览器上下文绑定，纯 HTTP
   复刻不出来，所以这条路才是真正能打通业务接口的那条；
2. **账号密码走 SSO**（:mod:`sso`，Lead 实测跑通）：只解决身份层，之后仍可能
   因为缺 csrf 而被应用层拒绝。

最要紧的是 :meth:`PincoAdapter.submit`：它默认拒绝执行，直到 :mod:`guard`
里的三条硬规则全部通过，并且提交请求体的结构已经被真实样本确认过
（``submit_payload_confirmed``）。守卫管「该不该交」，那道闸门管「交出去的是不是
对的」—— 交错的答案比不交更糟。
"""

from __future__ import annotations

import logging
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
    NotSupportedError,
    PlatformAdapter,
)
from kedaibiao.platforms.pinco import endpoints as ep
from kedaibiao.platforms.pinco import guard, parsing, sso
from kedaibiao.platforms.pinco.client import PincoClient
from kedaibiao.platforms.pinco.errors import PincoError, PincoNotLoggedIn
from kedaibiao.platforms.pinco.models import (
    PincoClass,
    PincoMaterialDetail,
    PincoSession,
    PincoSubmissionState,
)
from kedaibiao.platforms.pinco.plan import PincoPlan, submission_digest
from kedaibiao.platforms.pinco.solving import PlanBuilder, build_answerer
from kedaibiao.tasks.answerer import Answerer

log = logging.getLogger(__name__)


class PincoAdapter(PlatformAdapter):
    """学生端适配器：抓取 → 作答 → 计划 → （人工复核后）提交。"""

    name = "pinco"
    display_name = "希沃品课"
    capabilities = Capabilities()

    #: 提交请求体的结构是否已由真实样本确认。
    #: TODO(endpoint): Lead 用真实会话验证完提交报文后改成 True（或按需覆盖）。
    #: 在它为 False 时，守卫放行也不会真的发出写请求 —— 字段拼错的提交比不提交更糟。
    submit_payload_confirmed: bool = False

    def __init__(
        self,
        ctx: AdapterContext | None = None,
        *,
        min_confidence: float = 0.75,
        answerer: Answerer | None = None,
        page_size: int = ep.DEFAULT_PAGE_SIZE,
    ) -> None:
        super().__init__(ctx)
        self.min_confidence = min_confidence
        self._answerer = answerer
        #: 翻页大小。**服务端封顶 20**，填大了会让「本页条数 < 请求条数」提前成立，
        #: 于是漏掉后面的页 —— 所以默认就用上限值。
        self.page_size = min(page_size, ep.DEFAULT_PAGE_SIZE)
        self._session: PincoSession | None = None
        self._reviewed: guard.ReviewedPlan | None = None
        #: 本次进程里已经发出过写请求的作业 —— 防重入闸门。
        #: 网络中断导致「看似失败实则已提交」时，就是它挡住自动重试。
        self._attempted: set[str] = set()

    # -- 会话 ---------------------------------------------------------------

    @property
    def session(self) -> PincoSession | None:
        return self._session

    def adopt_session(
        self, *, authorization: str, csrf_token: str = "", username: str = ""
    ) -> PincoSession:
        """接管一份真实会话（cookie / authorization）。

        这是当前唯一可用的「登录」方式：把用户在浏览器里的会话搬过来，
        而不是让程序去复现 SSO 流程。``storage`` 里是凭据，**绝不进日志与计划**。
        """
        storage: dict[str, Any] = {}
        if authorization:
            storage[ep.HEADER_AUTHORIZATION] = authorization
        if csrf_token:
            storage[ep.CSRF_COOKIE] = csrf_token
        self._session = PincoSession(username=username, logged_in=True, storage=storage)
        return self._session

    def client(self) -> PincoClient:
        """构造请求客户端。每次都新建：它只持有引用，没有需要复用的状态。"""
        storage: dict[str, Any] = self._session.storage if self._session else {}
        return PincoClient(
            self.ctx.client(),
            csrf_token=str(storage.get(ep.CSRF_COOKIE, "")),
            authorization=str(storage.get(ep.HEADER_AUTHORIZATION, "")),
        )

    async def login(self, credentials: Credentials) -> bool:
        """登录。两条会话来源，主路径是接管浏览器会话。

        (a) ``credentials.extra["token"]``（``KEDAIBIAO_PINCO_TOKEN``）→ 直接接管，
            应用层可用；
        (b) 账号密码 → 走实测跑通的 SSO 流程（:mod:`sso`）。**但它只解决身份层**：
            应用层 RPC 的 csrf 与浏览器会话上下文绑定，纯 HTTP 拿不到（实测回
            ``403 invalid csrf token``）。因此 (b) 成功后仍可能调不通业务接口，
            这时请用浏览器导出的 cookie 调 :meth:`adopt_session`。
        """
        token = credentials.extra.get("token", "").strip()
        if token:
            self.adopt_session(authorization=token, username=credentials.username)
            self.log.info(
                "已接管品课会话（authorization 注入）：%s", credentials.username or "<无用户名>"
            )
            return True

        if not credentials.username or not credentials.password:
            raise PincoError(
                "品课登录需要账号密码或注入的会话："
                "设置 KEDAIBIAO_PINCO_USER / KEDAIBIAO_PINCO_PASS 走 SSO 登录，"
                "或用 KEDAIBIAO_PINCO_TOKEN 提供浏览器会话的 authorization。"
            )

        result = await sso.login_with_password(
            self.client(), credentials.username, credentials.password
        )
        self._session = PincoSession(
            username=credentials.username,
            logged_in=True,
            # 值是凭据，只进内存；__repr__ 已屏蔽
            storage={ep.SSO_TOKEN_COOKIE: result.token},
        )
        self.log.warning(
            "SSO 登录只解决身份层：应用层 RPC 还需要浏览器会话的 csrf cookie。"
            "若随后出现 403 invalid csrf token，请用浏览器导出的 cookie 调 adopt_session()。"
        )
        return True

    async def is_logged_in(self) -> bool:
        return self._session is not None and self._session.logged_in

    async def health_check(self) -> bool:
        """匿名健康检查 —— 探「网络与协议通不通」，**验证不了登录态**（匿名也能过）。

        排查问题时先跑它，能立刻区分「网络问题」与「会话问题」。
        走的是 REST ``/system/currentTime``（实测 200 + ``{"code":0}``）；
        不再用 ``GetV1Pincofaq`` —— 那个 action 恒回 500，会让这里**恒假**。

        为什么连 ``OSError`` 一起接：健康检查的职责就包含「网络不通」这一种失败，
        而网络层抛的是 ``TransportError``（``OSError`` 子类），不是 ``PincoError``。
        只接 ``PincoError`` 会让「断网」变成向上抛异常，而不是「这里返回 False」。
        """
        try:
            await self.client().health_check()
        except (PincoError, OSError) as exc:
            self.log.warning("品课健康检查失败：%s", exc)
            return False
        return True

    # -- 抓取 ---------------------------------------------------------------

    def _require_session(self) -> None:
        if not (self._session and self._session.logged_in):
            raise PincoNotLoggedIn(
                "尚未接管品课会话：请先 login()（KEDAIBIAO_PINCO_TOKEN）或 adopt_session()。"
            )

    def _page_payload(self, page: int = ep.DEFAULT_PAGE) -> dict[str, Any]:
        """分页参数。实测：字段名 ``page``/``pageSize``，**服务端把 pageSize 封顶 20**。"""
        return {ep.FIELD_PAGE: page, ep.FIELD_PAGE_SIZE: self.page_size}

    async def list_courses(self) -> list[Course]:
        """课程列表（自动翻页）。实测：``GetV1CourseStudentworkbench`` → ``data.content[]``。

        实测坑：服务端 pageSize 封顶 20，课程 total=24 时第一页只回 20 条 ——
        只取第一页会漏课，所以这里翻页取全（并按 courseId 去重防重复页）。
        """
        self._require_session()
        return await self._paged(ep.ACTION_COURSE_WORKBENCH, {}, parsing.parse_courses)

    async def _paged(self, action: str, payload: dict[str, Any], parse: Any) -> list[Any]:
        """把一个列表接口按页取全。

        停止条件有两个（任一满足就停）：本页条数 < 请求条数，或本页没有新条目。
        这样即使服务端忽略分页参数、页页返回同一批，也不会死循环
        （另有 :data:`endpoints.MAX_PAGES` 兜底并告警）。
        """
        collected: list[Any] = []
        seen: set[str] = set()
        for page in range(1, ep.MAX_PAGES + 1):
            data = await self.client().post_action(action, {**payload, **self._page_payload(page)})
            items = parse(data)
            fresh = [item for item in items if item.remote_id not in seen]
            seen.update(item.remote_id for item in fresh)
            collected.extend(fresh)
            if len(items) < self.page_size or not fresh:
                return collected
        self.log.warning(
            "%s 翻到第 %d 页仍未见底，可能分页参数被忽略；先返回已取到的 %d 条",
            action,
            ep.MAX_PAGES,
            len(collected),
        )
        return collected

    async def list_classes(self) -> list[PincoClass]:
        """班级列表。实测：``GetV1StudentClassList {}`` → ``data[]``（裸数组）。

        不属于 ``PlatformAdapter`` 契约的必需钩子，但实测可用，拿来给 CLI/自检
        显示「这个账号在哪些班」。
        """
        self._require_session()
        data = await self.client().post_action(ep.ACTION_CLASS_LIST, {})
        return parsing.parse_classes(data)

    async def list_assignments(self, course: Course) -> list[Assignment]:
        """某门课的作业列表（``TaskKind.HOMEWORK``）。

        实测确认走通的就是这一条：``GetV1StudenthomeworkList {courseId, page,
        pageSize}`` → ``data.content[]``。学习任务是课程材料，在
        :meth:`list_learning_tasks` 里，两条链路分开、互不混入。
        """
        self._require_session()
        # 参数必须带 courseId + page + pageSize —— 缺参数会 401 illegal argument。
        data = await self.client().post_action(
            ep.ACTION_HOMEWORK_LIST,
            {**self._page_payload(), ep.FIELD_COURSE_ID: course.remote_id},
        )
        return parsing.parse_assignments(data, course=course, default_kind=TaskKind.HOMEWORK)

    async def list_learning_tasks(self, course: Course) -> list[Assignment]:
        """学习任务 = **课程材料**（PPT / PDF / 视频）。实测确认。

        ``GetV1CourseByCourseIdStudentMaterialList {courseId}`` →
        ``data.courseMaterials[]``，``learningRatio`` 就是学习进度（0~1）。

        材料不是题：它没有题目、也没有「提交」这个动作，所以这条链路只产出
        「任务 + 进度」；``fetch_questions`` 对它直接抛
        :class:`NotSupportedError`，计划层也会把它排除在可提交集合之外。

        （实测反例：``GetV1StudentChapterTaskList`` 是空壳，任何参数都返回 ``{}``，
        已登记进 :data:`endpoints.DEAD_ACTIONS`，这里不再调用它。）
        """
        self._require_session()
        data = await self.client().post_action(
            ep.ACTION_MATERIAL_LIST, {ep.FIELD_COURSE_ID: course.remote_id}
        )
        return parsing.parse_materials(data, course=course)

    async def fetch_material_detail(
        self, course: Course, material_id: str
    ) -> PincoMaterialDetail:
        """材料详情（只读），用来拿下载地址。

        实测：入参 ``chapterDirectoryMaterialId`` 直接取列表里的 ``materialId`` 值。
        ``downloadUrl`` 带签名/时效 —— 只当次使用，**不要落库**。
        """
        self._require_session()
        data = await self.client().post_action(
            ep.ACTION_MATERIAL_DETAIL,
            {
                ep.FIELD_COURSE_ID: course.remote_id,
                ep.FIELD_CHAPTER_DIRECTORY_MATERIAL_ID: material_id,
            },
        )
        return parsing.parse_material_detail(data, material_id=material_id)

    async def fetch_questions(self, assignment: Assignment) -> list[Question]:
        """作业题目。学习任务（课程材料）没有题目 —— 对它调用会直接报错。

        为什么抛错而不是返回空列表：空列表会被上层读成「这份作业没有题目」，
        而事实是「这类对象根本没有题目这个概念」。两者的处理方式完全不同。
        """
        self._require_session()
        if assignment.kind is TaskKind.COURSE:
            raise NotSupportedError(
                f"{assignment.remote_id!r} 是学习任务（课程材料），没有题目；"
                "请用 fetch_material_detail() 取材料详情/下载地址。"
            )
        data = await self.client().post_action(
            ep.ACTION_HOMEWORK_QUESTIONS,
            {ep.FIELD_STUDENT_HOMEWORK_ID: assignment.remote_id},
        )
        # 题目响应里也带 deadline/expired（实测），顺手补进作业 —— 列表接口没有
        # 这两个字段，为了一个截止时间再打一次详情接口不划算。
        if isinstance(data, dict) and assignment.deadline is None:
            assignment.deadline = parsing.parse_deadline(data)
        return parsing.parse_questions(data)

    # -- 计划 ---------------------------------------------------------------

    def builder(self) -> PlanBuilder:
        """计划生成器。答题器不给就按「题库 → 规则」链式降级。"""
        answerer = self._answerer or build_answerer(
            self.ctx.bank, min_confidence=self.min_confidence
        )
        return PlanBuilder(
            answerer=answerer,
            bus=self.ctx.bus,
            notifier=self.ctx.notifier,
            min_confidence=self.min_confidence,
            platform=self.name,
            display_name=self.display_name,
        )

    async def plan(
        self,
        *,
        max_assignments: int = 0,
        kinds: frozenset[TaskKind] = frozenset(),
    ) -> PincoPlan:
        """抓取作业 + 学习任务，产出可复核计划 —— **全程没有任何写操作**。

        学习任务（课程材料）也会列出来，但只带进度、不带题目：计划层会把它们
        标成跳过，保证不进可提交集合。
        """
        self._require_session()
        items: list[tuple[Assignment, list[Question]]] = []
        for course in await self.list_courses():
            candidates = await self.list_assignments(course)
            candidates.extend(await self.list_learning_tasks(course))
            for assignment in candidates:
                if kinds and assignment.kind not in kinds:
                    continue
                if max_assignments and len(items) >= max_assignments:
                    break
                questions: list[Question] = []
                if assignment.kind is not TaskKind.COURSE:
                    # 已提交/已过期/平台不允许提交的作业不取题：省一次请求，
                    # 也少打扰平台一次（计划里照样会列出它被跳过的原因）。
                    blocked = assignment.finished or bool(
                        parsing.assignment_blocker(assignment.raw)
                    )
                    if not blocked:
                        questions = await self.fetch_questions(assignment)
                items.append((assignment, questions))
            if max_assignments and len(items) >= max_assignments:
                break
        return await self.builder().build(items)

    def confirm_plan(self, plan: PincoPlan) -> guard.ReviewedPlan:
        """登记「人已经复核过这份计划」。

        没有这一步，``submit()`` 一律拒绝执行。返回的凭证也可以交给别的代码
        传递，但凭证本身只在内存里，不会落盘。
        """
        self._reviewed = guard.review(plan)
        self.log.info(
            "已登记复核计划 %s（可提交 %d 个作业）",
            self._reviewed.digest,
            len(self._reviewed.plan.planned),
        )
        return self._reviewed

    # -- 提交 ---------------------------------------------------------------

    async def fetch_submission_state(self, assignment: Assignment) -> PincoSubmissionState:
        """查平台侧「这个作业交了没」。"""
        self._require_session()
        data = await self.client().post_action(
            ep.ACTION_HOMEWORK_DETAIL,
            {ep.FIELD_STUDENT_HOMEWORK_ID: assignment.remote_id},
        )
        return parsing.parse_submission_state(data, assignment_id=assignment.remote_id)

    async def submit(self, assignment: Assignment, answers: list[Answer]) -> SubmissionResult:
        """提交作业 —— 默认拒绝执行。

        依次过三道守卫：计划已复核且内容一致、没有待人工/低置信度题、平台
        状态复查通过。任何一条不过都抛 :class:`guard.SubmissionRefused`，
        请求根本不会发出去。
        """
        self._require_session()
        if assignment.remote_id in self._attempted:
            raise guard.SubmissionRefused(
                guard.REFUSAL_ALREADY_SENT,
                f"作业 {assignment.remote_id!r} 本次进程里已经发过一次提交请求，拒绝再发。"
                "上一次的结果可能未知，请先到平台核实。",
            )

        actual = submission_digest(assignment, answers)
        planned = guard.check_reviewed(
            self._reviewed,
            platform=self.name,
            assignment_id=assignment.remote_id,
            actual_digest=actual,
        )
        guard.check_questions(planned, answers, min_confidence=self.min_confidence)
        state = await self._probe_state(assignment)
        guard.check_platform_state(state, assignment_id=assignment.remote_id)

        if not self.submit_payload_confirmed:
            raise PincoError(
                "提交请求体的结构尚未由真实样本确认（TODO(endpoint)），拒绝发出写请求。"
                "守卫已经放行，但「交出去的东西对不对」还没验证过 —— "
                "交错的答案比不交更糟。"
            )
        # 先记「提交意图」再发请求，顺序不能反：请求发出后进程可能再也没机会
        # 写任何东西（被杀 / 断网）。记下来之后，同一进程内的重试会被上面的
        # 闸门挡住；跨进程则靠提交前的平台状态复查。
        self._attempted.add(assignment.remote_id)
        return await self._post_submit(assignment, planned, answers)

    async def _probe_state(self, assignment: Assignment) -> PincoSubmissionState | None:
        """提交前复查平台状态。

        探测失败返回 ``None``，由守卫按「不知道」拒绝 —— 绝不能把探测失败
        当成「没交过」，那正是重复交卷的成因。
        """
        try:
            return await self.fetch_submission_state(assignment)
        except Exception as exc:  # noqa: BLE001 - 任何失败都必须是「不知道」
            self.log.warning(
                "提交前复查平台状态失败：%s（%s: %s）",
                assignment.remote_id,
                type(exc).__name__,
                exc,
            )
            return None

    async def _post_submit(
        self,
        assignment: Assignment,
        planned: guard.PincoAssignmentPlan,
        answers: list[Answer],
    ) -> SubmissionResult:
        payload = self._submit_payload(assignment, planned, answers)
        try:
            data = await self.client().post_action(
                ep.ACTION_SUBMIT_HOMEWORK, payload, dangerous=True
            )
        except PincoError as exc:
            # 请求已经出去了，失败原因无法区分「服务端没收到」与「收到了但回包丢了」。
            # 保守处理：报失败并说明结果不明，绝不自动重试。
            self.log.error("提交结果不明：%s", exc)
            return SubmissionResult(
                assignment_id=assignment.remote_id,
                ok=False,
                message=f"提交结果不明，请到平台核实后再决定是否重交：{exc}",
            )
        return parsing.parse_submit_result(data, assignment_id=assignment.remote_id)

    def _submit_payload(
        self,
        assignment: Assignment,
        planned: guard.PincoAssignmentPlan,
        answers: list[Answer],
    ) -> dict[str, Any]:
        """拼提交请求体。

        TODO(endpoint): 请求体的确切结构（必填字段、答案编码、是否需要
        ``duration``/``remark``）等真实样本确认。现在只按静态清单拼**最小**
        结构；``submit_payload_confirmed=False`` 时它不会真的被发出去。
        """
        return {
            ep.FIELD_STUDENT_HOMEWORK_ID: assignment.remote_id,
            ep.FIELD_ANSWERS: [
                {
                    ep.FIELD_QUESTION_ID: answer.question.remote_id,
                    ep.FIELD_ANSWER_CONTENT: self._answer_content(answer),
                }
                for answer in answers
            ],
        }

    @staticmethod
    def _answer_content(answer: Answer) -> str:
        """把答案压成 ``answerContent`` 字符串。

        TODO(endpoint): 选项类答案在品课里到底是「选项序号」「选项原文」还是
        别的编码，必须等样本确认。这里先给文本答案，选择题按 1 起的序号拼接
        —— 结构未确认前不会真的发出（见 ``submit_payload_confirmed``）。
        """
        if answer.text.strip():
            return answer.text.strip()
        return ",".join(str(index + 1) for index in answer.choices)
