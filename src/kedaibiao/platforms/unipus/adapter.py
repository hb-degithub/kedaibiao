"""U校园（Unipus）适配器：把平台数据翻译成 ``core.models``。

本阶段**只做只读链路**：登录 → 拉课程 → 拉任务 → 拉题目。
``capabilities.submit`` 为 ``False``，调用 ``submit`` 会走基类抛
``NotSupportedError``。

两条必须记住的约定（会进幂等键，上线后不能改）：

1. ``Assignment.remote_id`` = ``f"{课程实例 ID}/{分组 ID}"``，
   由 :func:`~kedaibiao.platforms.unipus.parsing.pack_assignment_id` 生成 ——
   这个值同时是 ``RunRecord.assignment_id`` 与 ``ProgressStore.already_done``
   的键；
2. ``Course.remote_id`` = 门户「班级教材 ID」（``courseResourceId``），
   缺失时退到课堂实例 ID。

规格第 11.2 节点名的两个坑在本层有对应处理：19 位题目 ID 全程 ``str``
（``parsing.precision_id``），登录/请求链共用同一个 ``HttpClient``（``client``）。
"""

from __future__ import annotations

import http.cookiejar
from collections.abc import Sequence
from dataclasses import replace
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
from kedaibiao.platforms.unipus import parsing, submit as submit_mod
from kedaibiao.platforms.unipus import endpoints
from kedaibiao.platforms.unipus.client import UnipusClient
from kedaibiao.platforms.unipus.errors import (
    UnipusCaptchaRequired,
    UnipusError,
    UnipusProtocolError,
)
from kedaibiao.platforms.unipus.parsing import (
    SCOPE_UCONTENT_LEAFS,
    BookshelfBook,
    CourseBinding,
    GroupState,
    RequiredSet,
    StandardAnswer,
    catalog_group_ids,
    pack_assignment_id,
    split_assignment_id,
)

#: ``Question.raw`` 里的标记：题面在协议层不可得（规格 U1）
STEM_UNAVAILABLE_KEY = "stem_unavailable"

#: ``Assignment.raw`` 里的标记：任务类别判断不出（目录节点没有该信息）
KIND_UNCONFIRMED_KEY = "kind_unconfirmed"

#: ``Assignment.raw`` 里的标记：完成度/进度未经 ``progress/v2`` 核实
STATE_UNCONFIRMED_KEY = "state_unconfirmed"


class UnipusAdapter(PlatformAdapter):
    """U校园适配器（只读）。"""

    name = "unipus"
    display_name = "U校园（Unipus）"
    capabilities = Capabilities(
        login=True,
        list_courses=True,
        list_assignments=True,
        fetch_questions=True,
        submit=False,
        course_playback=False,
    )

    #: 提交载荷是否已由真实抓包确认。
    #:
    #: 规格 §8.1 给了完整的形状，但 ``isCompleted`` / ``thirdPartyJudges`` 的
    #: **取值语义**没给。这两个字段猜错不会报错、只会静默判错，所以默认关闭。
    #: 拿到一次真实提交请求、核对完这两个字段后，把它置 True 即可启用。
    submit_payload_confirmed: bool = False

    def __init__(
        self,
        ctx: AdapterContext | None = None,
        *,
        timeout: float | None = None,
        clock: Any | None = None,
        submit_payload_confirmed: bool | None = None,
    ) -> None:
        super().__init__(ctx)
        if submit_payload_confirmed is not None:
            self.submit_payload_confirmed = submit_payload_confirmed
        self._client: UnipusClient | None = None
        self._timeout = timeout
        self._clock = clock
        #: cookie 落盘位置（由 ``runtime.make_adapter`` 注入）——浏览器求解
        #: 验证码后要把 cookie 写回同一个文件，下次才能免登录。
        self._cookie_file: str | None = None

    # -- 客户端 -----------------------------------------------------------

    def client(self) -> UnipusClient:
        """惰性构造编排器：网络客户端一律来自 ``AdapterContext.http``。"""
        if self._client is None:
            options: dict[str, Any] = {"logger": self.log}
            if self._timeout is not None:
                options["timeout"] = self._timeout
            if self._clock is not None:
                options["clock"] = self._clock
            self._client = UnipusClient(self.ctx.client(), **options)
        return self._client

    @property
    def session(self) -> Any:
        """当前会话（只读用途；不含任何写操作）。"""
        return self.client().session if self._client is not None else None

    # -- 生命周期 ---------------------------------------------------------

    async def login(self, credentials: Credentials) -> bool:
        """登录。SSO 要验证码时**自动退回浏览器求解**。

        两条路径：

        - 风控没触发 → 纯 HTTP 三步登录（快）；
        - 风控要验证码（业务码 1506）→ 浏览器打开 SSO、解极验滑块、注入登录，
          成功后把会话 cookie 灌进当前 HttpClient，再用 cookie 里的 ``jwt``
          补齐门户会话。``login_via_browser`` 与 iTEST 共用，已实测可用。
        """
        if not credentials.username:
            raise UnipusProtocolError("U校园登录需要非空用户名")
        try:
            return await self.client().login(credentials.username, credentials.password)
        except UnipusCaptchaRequired:
            self.log.info("SSO 要求验证码（1506），退回浏览器求解")
        return await self._login_via_browser(credentials)

    async def _login_via_browser(self, credentials: Credentials) -> bool:
        """浏览器触发 + 极验求解 + 注入登录；成功后补齐门户会话。"""
        from kedaibiao.platforms.unipus.geetest.browser import login_via_browser

        try:
            result = login_via_browser(
                platform="unipus",
                username=credentials.username,
                password=credentials.password,
                cookie_file=self._cookie_file,
                on_event=self.log.info,
            )
        except Exception as exc:  # noqa: BLE001 - 浏览器/驱动异常一律翻成登录失败
            self.log.warning("浏览器求解登录异常: %s: %s", type(exc).__name__, exc)
            return False
        if not result.ok:
            self.log.warning("浏览器求解登录失败: %s", result.message or "未返回原因")
            return False
        self._adopt_cookies(result.cookies)
        # 浏览器会话里的 jwt cookie 就是门户 JWT，直接用它补齐会话
        if await self._bootstrap_session_from_cookies():
            self.log.info("浏览器登录成功，门户会话已补齐: %s", self.client().session.describe())
            return True
        # 兜底：TGC 已建立，重走纯 HTTP 登录（通常不再弹验证码）
        try:
            return await self.client().login(credentials.username, credentials.password)
        except UnipusCaptchaRequired:
            self.log.warning("浏览器登录后重试仍要求验证码")
            return False

    def _cookie_value(self, name: str) -> str:
        """从当前 cookie 罐里取一个 cookie 值；取不到返回空串。"""
        transport = getattr(self.ctx.client(), "transport", None)
        jar = getattr(transport, "cookies", None)
        if jar is None:
            return ""
        try:
            items = list(jar)
        except TypeError:
            return ""
        for cookie in items:
            if getattr(cookie, "name", "") == name:
                value = getattr(cookie, "value", "")
                if value:
                    return str(value)
        return ""

    def _adopt_cookies(self, cookies: Sequence[dict[str, Any]]) -> int:
        """把浏览器拿到的 cookie 灌进当前 HttpClient 的 cookie 罐。

        注意 ``urllib`` 的 ``CookieJar`` **没有** ``set()``（iTEST 那份同款代码会
        静默跳过、等于没灌），所以这里手工造 ``http.cookiejar.Cookie``。
        返回成功灌入的条数，方便排查。
        """
        transport = getattr(self.ctx.client(), "transport", None)
        jar = getattr(transport, "cookies", None)
        if jar is None or not cookies:
            return 0
        adopted = 0
        for item in cookies:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "")
            if not name:
                continue
            domain = str(item.get("domain") or ".unipus.cn")
            try:
                jar.set_cookie(
                    http.cookiejar.Cookie(
                        version=0,
                        name=name,
                        value=str(item.get("value") or ""),
                        port=None,
                        port_specified=False,
                        domain=domain,
                        domain_specified=bool(item.get("domain")),
                        domain_initial_dot=domain.startswith("."),
                        path=str(item.get("path") or "/"),
                        path_specified=True,
                        secure=bool(item.get("secure")),
                        expires=item.get("expires"),
                        discard=False,
                        comment=None,
                        comment_url=None,
                        rest={},
                    )
                )
                adopted += 1
            except Exception:  # noqa: BLE001 - 单条 cookie 坏掉不影响其它
                self.log.debug("灌入 cookie 失败: %s", name, exc_info=True)
        return adopted

    async def _bootstrap_session_from_cookies(self) -> bool:
        """用 cookie 罐里的 ``jwt`` 恢复门户会话。

        为什么需要：账号库不存密码、会话快照也可能没落盘；但 SSO 会话里
        ``.unipus.cn`` 的 ``jwt`` cookie 本身就是门户 JWT。拿它打一次用户信息
        接口就能把 ``open_id``/``school`` 补齐 —— 这是「只有 cookie」时的兜底。
        """
        jwt = self._cookie_value("jwt")
        if not jwt:
            return False
        client = self.client()
        client.session.portal_token = jwt
        try:
            info = await client.fetch_user_info()
        except UnipusError as exc:
            self.log.info("cookie 里的 jwt 不被门户接受（%s），需要重新登录", exc)
            client.session.clear()
            return False
        client.apply_user_info(info)
        return True

    async def is_logged_in(self) -> bool:
        """会话是否有效；没有内存会话时先尝试用 cookie 里的 jwt 恢复。"""
        if self._client is None:
            client = self.client()
            if not client.session.has_portal_token and not await self._bootstrap_session_from_cookies():
                return False
            return await client.is_logged_in()
        if not self._client.session.has_portal_token:
            await self._bootstrap_session_from_cookies()
        return await self._client.is_logged_in()

    # -- 会话持久化 --------------------------------------------------------
    #
    # 原先这里的约定是「只存内存、不写任何含令牌的文件」。但账号库**刻意不存
    # 密码**，于是每次请求都会新建适配器 → 内存会话丢失 → 只剩空密码 →
    # 报「登录失败」。所以必须把会话本身存下来。
    #
    # 存的是**会话**（令牌），不是密码；落盘位置与 cookie 文件同目录、
    # 权限 0600，生命周期与「登录态」一致，登出/删账号时一并删除。

    def export_session(self) -> dict[str, Any] | None:
        session = self.session
        if session is None or not getattr(session, "has_portal_token", False):
            return None
        return {
            "portal_token": session.portal_token,
            "open_id": session.open_id,
            "display_name": session.display_name,
            "school": session.school,
            "app_user_id": session.app_user_id,
        }

    def import_session(self, data: dict[str, Any]) -> bool:
        portal_token = str(data.get("portal_token") or "")
        open_id = str(data.get("open_id") or "")
        if not portal_token or not open_id:
            return False
        session = self.client().session
        session.portal_token = portal_token
        session.open_id = open_id
        session.display_name = str(data.get("display_name") or "")
        session.school = str(data.get("school") or "")
        session.app_user_id = str(data.get("app_user_id") or "")
        return True

    # -- 课程 -------------------------------------------------------------

    async def list_courses(self, *, with_bookshelf: bool = True) -> list[Course]:
        """班级×教材绑定列表。

        ``with_bookshelf=True``（默认）会额外打一次门户书架，只为补
        ``activation`` 激活标记与缺失的 ``resourceId``（规格第 6.4 节）。
        书架失败会照常上抛 —— 不做静默降级。
        """
        client = self.client()
        bindings = list(await client.fetch_course_bindings())
        if with_bookshelf:
            books = await client.fetch_bookshelf()
            bindings = _merge_bookshelf(bindings, books)
        return [_to_course(binding) for binding in bindings]

    # -- 任务 -------------------------------------------------------------

    async def list_assignments(
        self, course: Course, *, with_state: bool = False
    ) -> list[Assignment]:
        """目录里的分组 → ``Assignment`` 列表。

        ``with_state=True`` 会为**每个**分组各打一次 ``progress/v2``（N 次请求，
        顺序执行，不走并发）来填 ``progress``/``finished``；默认不填，并在
        ``raw`` 里标 ``state_unconfirmed=True``——目录接口本身没有完成度。
        """
        catalog = await self.client().fetch_catalog(_course_instance_id(course))
        assignments: list[Assignment] = []
        for unit in catalog.units:
            for group in unit.groups:
                assignments.append(
                    _to_assignment(course, unit, group, resource_id=catalog.resource_id)
                )
        if with_state:
            assignments = [await self._with_state(item) for item in assignments]
        return assignments

    async def _with_state(self, assignment: Assignment) -> Assignment:
        state = await self.fetch_group_state(assignment)
        assignment.raw.pop(STATE_UNCONFIRMED_KEY, None)
        assignment.raw["group_state"] = state.raw
        assignment.raw["first_pass_datetime"] = state.first_pass_datetime
        assignment.progress = state.score_pct if state.score_pct is not None else 0.0
        assignment.finished = state.finished
        return assignment

    async def fetch_group_state(self, assignment: Assignment) -> GroupState:
        """单任务状态（真源）。

        ⚠️ 只有 ``first_pass.datetime`` 能判断"做过没有"；``state.state``/
        ``score``/``pass`` 都不是班级完成度（规格第 6.4、11.2 节）。
        """
        course_instance_id, group_id = split_assignment_id(assignment.remote_id)
        return await self.client().fetch_group_state(course_instance_id, group_id)

    async def fetch_required_groups(self, course: Course) -> RequiredSet:
        """必修集（``leafs`` 口径）。

        ⚠️ 这个口径按 (实例, openId) 记账、**不随班级变化**；要班级口径需要用
        门户的 ``/api/tla/*``（本阶段未接线，见交付说明）。
        ``missing=True`` 表示 ``leafs`` 为空 —— 那是**信息缺失**，不是"没有必修"。
        """
        client = self.client()
        catalog = await client.fetch_catalog(_course_instance_id(course))
        group_ids = catalog_group_ids(catalog)
        if not group_ids:
            return RequiredSet(scope=SCOPE_UCONTENT_LEAFS, missing=True, raw={})
        return await client.fetch_required_leafs(_course_instance_id(course), group_ids)

    # -- 题目 -------------------------------------------------------------

    async def fetch_questions(self, assignment: Assignment) -> list[Question]:
        """拉题目内容。

        规格 U1 明确：解密后的**子题内部结构**（题干、选项表示）在协议规格里
        不可得。因此这里 ``stem=""``、``options=[]``，把原始 JSON 原样放进
        ``Question.raw``（含 ``inner``），并标 ``stem_unavailable=True``。
        ``remote_id`` 一定填平台题目 ID —— 无题面的题目靠它做指纹（否则入不了库）。
        """
        course_instance_id, group_id = split_assignment_id(assignment.remote_id)
        content = await self.client().fetch_content(course_instance_id, group_id)
        return [_to_question(item) for item in content]

    async def submit(
        self, assignment: Assignment, answers: list[Answer]
    ) -> SubmissionResult:
        """提交一个任务组。

        ⚠️ **默认被闸门挡住**（``submit_payload_confirmed = False``）—— 见类属性
        注释。闸门打开后，流程是：

        1. 取内容（题目结构，用来算计分点数与子题数组）；
        2. 取标准答案（规格 §7.3：答案是**服务端标准答案原样转发**，
           本方法不用 ``answers`` 参数里的内容）；
        3. 构造提交体（:mod:`kedaibiao.platforms.unipus.submit`）；
        4. POST 提交端点；
        5. 回读一次 ``progress/v2`` 确认（规格 §8.3：服务端接受不等于记下了成绩）。
        """
        instance_id, group_id = split_assignment_id(assignment.remote_id)

        if not self.submit_payload_confirmed:
            message = (
                "提交载荷的取值语义尚未由真实抓包确认（isCompleted / "
                "thirdPartyJudges），拒绝发出写请求。"
            )
            # 有网络客户端的话，顺手把「按规格能构造出的载荷」也演示出来 ——
            # 让用户看到到底还差什么，比一句「不支持」有用。
            if getattr(self.ctx, "http", None) is not None:
                try:
                    content = await self.client().fetch_content(instance_id, group_id)
                    plan = submit_mod.build_submit_plan(
                        course_id=self._course_v2(instance_id),
                        group_id=group_id,
                        open_id=self.session.open_id if self.session else "",
                        questions=content,
                    )
                    message += (
                        f"\n已按规格 §8.1 构造出载荷：{plan.summary()}"
                        f"\n待确认项：{'；'.join(plan.warnings) or '无'}"
                    )
                except Exception as exc:  # noqa: BLE001 - 只是演示，失败不影响结论
                    message += f"\n（顺带构造载荷时失败：{type(exc).__name__}）"
            message += (
                "\n拿到一次真实提交请求后，把 UnipusAdapter.submit_payload_confirmed "
                "置 True 即可启用。"
            )
            raise NotSupportedError(message)

        content = await self.client().fetch_content(instance_id, group_id)
        standard = await self.client().fetch_answers(instance_id, group_id)
        plan = submit_mod.build_submit_plan(
            course_id=self._course_v2(instance_id),
            group_id=group_id,
            open_id=self.session.open_id if self.session else "",
            questions=content,
            standard_answers=standard,
        )
        if plan.warnings:
            self.log.warning("提交前提示：%s", "；".join(plan.warnings))

        document = await self.client().request(
            endpoints.UnipusApp.UCONTENT,
            "POST",
            endpoints.UCONTENT_SUBMIT,
            family="ucontent",
            payload=plan.payload,
        )
        result = submit_mod.parse_submit_result(document)

        # 回读一次，确认成绩真的记下了（规格 §8.3）
        try:
            state = await self.client().fetch_group_state(instance_id, group_id)
            self.log.info(
                "提交后回读 group state：first_pass=%s", getattr(state, "first_pass", None)
            )
        except Exception:  # noqa: BLE001 - 回读失败不影响「提交请求已被接受」的事实
            self.log.warning("提交后回读状态失败（不影响提交结果）", exc_info=True)

        return SubmissionResult(
            assignment_id=assignment.remote_id,
            ok=result.accepted,
            score=result.score_pct,
            message=result.summary(),
            raw={"state": result.state, "score": result.score,
                 "version": result.version, "submitType": plan.submit_type},
        )

    def _course_v2(self, instance_id: str) -> str:
        """实例 ID → ``course-v2:...`` 形态（规格 §6.1）。"""
        return ("course-v2:" + instance_id) if not instance_id.startswith("course-v2:") else instance_id

    async def fetch_standard_answers(
        self,
        assignment: Assignment,
        *,
        expected_count: int | None = None,
        expected_child_counts: Sequence[int] | None = None,
    ) -> list[StandardAnswer]:
        """**适配器扩展方法**（不在 ``PlatformAdapter`` 契约里）。

        标准答案由服务端下发，字符串语义未确认（规格 U2），所以**不合成**
        ``core.Answer``：只按内容题序原样返回，供后续 M2 提交时塞进
        ``Answer.raw``。

        两个可选的一致性校验参数（规格第 11.2 节第 2 条要求实现者自己加）：

        - ``expected_count``：题目条数（通常 ``len(await fetch_questions(...))``）；
        - ``expected_child_counts``：**逐题**的子题数（从 ``fetch_questions`` 的
          ``Question.raw["child_count"]`` 取）。

        提交载荷里 ``isCompleted`` 的长度由**内容侧**子题数决定、
        ``thirdPartyJudges`` 由**答案侧**子题数决定，两者不等就会被平台拒
        （``300100`` / 越界）。所以不一致的组必须在这里被判为"不可提交"。
        """
        course_instance_id, group_id = split_assignment_id(assignment.remote_id)
        answers = await self.client().fetch_answers(course_instance_id, group_id)
        if expected_count is not None and len(answers) != expected_count:
            raise UnipusProtocolError(
                f"答案数与内容题数不一致：内容 {expected_count} 题，答案 {len(answers)} 条"
                f"（{assignment.remote_id}）；这组不可提交"
            )
        if expected_child_counts is not None:
            counts = list(expected_child_counts)
            if len(counts) != len(answers):
                raise UnipusProtocolError(
                    f"逐题子题数校验需要 {len(answers)} 个期望值，实际给了 {len(counts)} 个"
                )
            for index, (answer, expected) in enumerate(zip(answers, counts)):
                if len(answer.children) != expected:
                    raise UnipusProtocolError(
                        f"第 {index} 题子题数不一致：内容侧 {expected}，答案侧 "
                        f"{len(answer.children)}（{assignment.remote_id}）；这组不可提交"
                    )
        return list(answers)


# --------------------------------------------------------------------------
# 翻译辅助
# --------------------------------------------------------------------------


def _merge_bookshelf(
    bindings: list[CourseBinding], books: tuple[BookshelfBook, ...]
) -> list[CourseBinding]:
    """用书架条目补 ``activated`` 与缺失的 ``resourceId``。

    匹配键优先 ``course_instance_id``，退到 ``resource_id``（规格第 6.4 节把
    「有 instanceId 或 resourceId」当作教材节点判据）。
    """
    by_instance = {book.course_instance_id: book for book in books if book.course_instance_id}
    by_resource = {book.resource_id: book for book in books if book.resource_id}
    merged: list[CourseBinding] = []
    for binding in bindings:
        book = by_instance.get(binding.course_instance_id) or by_resource.get(binding.resource_id)
        if book is None:
            merged.append(binding)
            continue
        merged.append(
            replace(
                binding,
                activated=book.activated,
                resource_id=binding.resource_id or book.resource_id,
            )
        )
    return merged


def _course_instance_id(course: Course) -> str:
    instance_id = course.raw.get("course_instance_id") if isinstance(course.raw, dict) else None
    if not instance_id:
        raise UnipusProtocolError(
            f"课程 {course.remote_id!r} 的 raw 里没有 course_instance_id，无法定位实例"
        )
    return str(instance_id)


def _to_course(binding: CourseBinding) -> Course:
    parts = [part for part in (binding.class_name, binding.book_name) if part]
    return Course(
        remote_id=binding.remote_id,
        name=" · ".join(parts) or binding.remote_id,
        # 规格里没有教师与学期字段，留空而不是猜
        teacher="",
        term="",
        raw={
            "class_id": binding.class_id,
            "class_name": binding.class_name,
            "class_numeric_id": binding.class_numeric_id,
            "course_instance_id": binding.course_instance_id,
            "course_resource_id": binding.course_resource_id,
            "resource_id": binding.resource_id,
            "strategy_id": binding.strategy_id,
            "book_name": binding.book_name,
            "finish_point_num": binding.finish_points,
            "total_point_num": binding.total_points,
            "activated": binding.activated,
            "book": binding.raw,
        },
    )


def _to_assignment(
    course: Course, unit: parsing.CatalogUnit, group: parsing.CatalogGroup, *, resource_id: str
) -> Assignment:
    return Assignment(
        remote_id=pack_assignment_id(_course_instance_id(course), group.group_id),
        title=group.name or group.group_id,
        # 目录节点只有 base/name/role/tab_type/url，判断不出任务类别：
        # 保持默认 HOMEWORK 并在 raw 里标记（lead 已裁决不推断）
        kind=TaskKind.HOMEWORK,
        course_name=course.name,
        deadline=None,  # 规格未提供截止时间
        progress=0.0,
        finished=False,
        questions=[],
        url="",  # 规格未提供前端页面 URL
        raw={
            "course_instance_id": _course_instance_id(course),
            "unit_id": unit.unit_id,
            "unit_name": unit.name,
            "group_id": group.group_id,
            "base": group.base,
            "tab_type": group.tab_type,
            "resource_id": resource_id,
            KIND_UNCONFIRMED_KEY: True,
            STATE_UNCONFIRMED_KEY: True,
            "group": group.raw,
        },
    )


def _to_question(item: parsing.ContentQuestion) -> Question:
    #: 旧版规格（U1）说题面不可得；2026-10-20 用真实内容样本联调后已能取到：
    #: ``children[].quesText`` 是子题题干（HTML），``children[].options[]`` 是选项。
    #: 取不到时才退化成「题干不可得」，指纹回落到平台题目 ID。
    stem_unavailable = not item.stem and not item.options
    return Question(
        stem=item.stem,
        type=item.core_type,
        options=list(item.options),
        remote_id=item.instance_id,
        score=None,  # 规格没有题目分值字段
        raw={
            STEM_UNAVAILABLE_KEY: stem_unavailable,
            "unipus_type": item.unipus_type,
            "item_count": item.item_count,
            "child_count": item.child_count,
            "reply_type": item.reply_type,
            "question_type": item.question_type,
            "category": item.category,
            "media_types": list(item.media_types),
            # 提交 progress 的键必须用 contents 下标，不是 type（踩过的坑）
            "media_indexes": list(item.media_indexes),
            "children": [dict(child) for child in item.children],
            "item": item.item,
            "inner": item.inner,
        },
    )
