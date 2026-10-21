"""超星 / 学习通适配器（**只读**）。

## 能力声明就是边界

``Capabilities`` 里 ``fetch_questions`` / ``submit`` / ``course_playback`` 全是
``False``：流水线看到这三个没开，取题、作答、提交、刷课四个阶段直接跳过。
不是「跑起来才拒绝」，而是**契约层面就没有这条路**。

## 会话从哪来

学习通的登录要扫码/验证码，框架不代过。会话只从「已经登录过的浏览器」搬：

1. ``credentials.extra["cookie"]``；
2. ``credentials.password`` **且它看起来像 cookie 串** —— 控制台的「账号」页
   没有 cookie 输入框，把整串 cookie 贴在密码栏即可（密码只用一次、不落盘）；
3. 环境变量 ``KEDAIBIAO_CHAOXING_COOKIE``；
4. 上次落盘的会话（``import_session`` 或 cookie 罐恢复）。

搬过来的 cookie 进传输层的 cookie 罐，随后 ``persist_http_cookies`` 落到该账号
自己的 cookie 文件，下次控制台请求就不用再贴。
"""

from __future__ import annotations

import asyncio
import http.cookiejar
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from kedaibiao.browser.capture import parse_cookie_header
from kedaibiao.core.models import Assignment, Course, Credentials, TaskKind
from kedaibiao.platforms.base import AdapterContext, Capabilities, PlatformAdapter
from kedaibiao.platforms.chaoxing import endpoints, parsing

#: 会话 cookie 的环境变量名
COOKIE_ENV = "KEDAIBIAO_CHAOXING_COOKIE"

#: cookie 挂在根域上：mooc1 / mooc2-ans / i / passport2 都是它的子域
COOKIE_DOMAIN = ".chaoxing.com"

#: 用来区分「整串 cookie」和「一个普通字符串」的特征项
_COOKIE_MARKERS = ("fid=", "_uid=", "UID=", "uf=", "ws=", "p_auth_token=")

#: 浏览器 UA —— 学习通前置的 tengine 会直接 403 掉非浏览器 UA（实测 httpx/urllib 默认
#: UA 一律 403，带上 Chrome UA 就 200），所以只读请求也必须伪装成浏览器。
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

_HTML_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    "Referer": "https://i.chaoxing.com/",
    "User-Agent": BROWSER_UA,
}


def _parse_deadline(text: str) -> Any | None:
    """把「2026-10-25 22:00:00.0」这类截止时间文本解析成 datetime。"""
    raw = (text or "").strip()
    if not raw or "剩余" in raw or "已过期" in raw:
        return None
    match = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})(?:[ T](\d{1,2}):(\d{2}))?", raw)
    if not match:
        return None
    year, month, day = int(match.group(1)), int(match.group(2)), int(match.group(3))
    hour = int(match.group(4) or 0)
    minute = int(match.group(5) or 0)
    try:
        return datetime(year, month, day, hour, minute)
    except ValueError:
        return None


def _deadline_from_onclick(raw: str) -> Any | None:
    """从 ``goTest(...,'2026-10-25 22:00:00.0',...)`` 里兜底取截止时间。"""
    if not raw:
        return None
    match = re.search(r"'(\d{4}-\d{1,2}-\d{1,2}[^']*)'", raw)
    return _parse_deadline(match.group(1)) if match else None


class ChaoxingError(RuntimeError):
    """学习通侧的可预期失败。"""


class ChaoxingLoginError(ChaoxingError):
    """没有可用会话，或会话已失效。"""


def looks_like_cookie_header(value: str) -> bool:
    """判断一个字符串是不是「整串 cookie」。

    判据是「含 ``=`` 且含已知 cookie 名」—— 只判 ``=`` 会把普通密码误判进来，
    只判 ``;`` 又会漏掉单条 cookie。
    """
    text = value or ""
    return "=" in text and any(marker in text for marker in _COOKIE_MARKERS)


def _jar_of(ctx: AdapterContext) -> Any | None:
    return getattr(getattr(getattr(ctx, "http", None), "transport", None), "cookies", None)


def install_cookies(ctx: AdapterContext, cookies: dict[str, str]) -> int:
    """把 cookie 装进传输层 cookie 罐，返回装了几条。

    罐子不存在（单测里的假传输层）时返回 0 —— 此时请求不会带 cookie，
    但单测靠假传输层按 URL 直接给响应，照样能验逻辑。
    """
    jar = _jar_of(ctx)
    if jar is None:
        return 0
    installed = 0
    for name, value in cookies.items():
        jar.set_cookie(
            http.cookiejar.Cookie(
                version=0,
                name=name,
                value=value,
                port=None,
                port_specified=False,
                domain=COOKIE_DOMAIN,
                domain_specified=True,
                domain_initial_dot=True,
                path="/",
                path_specified=True,
                secure=True,
                expires=None,
                discard=False,
                comment=None,
                comment_url=None,
                rest={},
            )
        )
        installed += 1
    return installed


class ChaoxingAdapter(PlatformAdapter):
    """只读：课程 → 作业/考试。没有提交，没有进度上报。"""

    name = "chaoxing"
    display_name = "超星学习通"
    capabilities = Capabilities(
        login=True,
        list_courses=True,
        list_assignments=True,
        fetch_questions=False,
        submit=False,
        course_playback=False,
    )
    #: 课程级失败一律上抛：cookie 失效与「这门课没作业」必须区分开
    skippable_course_errors: tuple[type[BaseException], ...] = ()

    def __init__(self, ctx: AdapterContext | None = None) -> None:
        super().__init__(ctx)
        self._logged_in = False
        self._username = ""
        #: 探测缓存。``None`` = 还没探过；每来一次控制台请求就是一个新适配器，
        #: 所以这里的缓存只在一次请求内有效，不会把失效会话当成有效。
        self._probe_ok: bool | None = None

    # -- 会话 -------------------------------------------------------------

    def adopt_cookies(self, cookie_header: str, *, username: str = "") -> int:
        """接管浏览器导出的整串 cookie，返回装了几条。"""
        cookies = parse_cookie_header(cookie_header)
        if not cookies:
            raise ChaoxingLoginError(
                "cookie 串解析为空。需要形如 `fid=…; _uid=…` 的内容："
                "浏览器 DevTools → Network → 任选一个请求 → 复制 Cookie 请求头整段。"
            )
        installed = install_cookies(self.ctx, cookies)
        self._logged_in = True
        self._username = username
        self._probe_ok = None
        self.log.info(
            "已接管学习通浏览器会话：装 %d 条 cookie，用户 %s",
            installed,
            username or "<未提供>",
        )
        return installed

    async def _get(self, url: str) -> tuple[int, str]:
        """发一个只读 GET，返回 ``(status, text)``；网络失败 ``status=0``。"""
        try:
            response = await self.ctx.client().get(url, headers=_HTML_HEADERS)
        except Exception as exc:  # noqa: BLE001 - 网络层异常统一成 status=0
            return 0, f"{type(exc).__name__}: {exc}"
        return response.status, response.text()

    def _jar_header(self) -> str:
        jar = _jar_of(self.ctx)
        if jar is None:
            return ""
        pairs: dict[str, str] = {}
        for cookie in jar:
            if cookie.name and cookie.value is not None:
                pairs[cookie.name] = cookie.value
        return "; ".join(f"{k}={v}" for k, v in pairs.items())

    async def _probe(self) -> bool:
        """真探一次：能拿到非登录页就当作会话有效。"""
        url = endpoints.COURSE_LIST_URLS[0]
        status, body = await self._get(url)
        if status == 0:
            raise ChaoxingLoginError(f"连不上学习通：{body}")
        if status >= 400:
            # 403/404 同样是「这个会话不被接受」，不能当成有效
            return False
        return not parsing.looks_like_login(body, endpoints.LOGIN_MARKERS)

    async def is_logged_in(self) -> bool:
        if self._probe_ok is None:
            self._probe_ok = await self._probe()
        return self._probe_ok

    async def login(self, credentials: Credentials) -> bool:
        # 环境变量优先从 ctx.environ 取（.env + 真实环境的快照），
        # 兜底才读进程环境 —— 与框架里其它凭据的取法保持一致。
        env_cookie = self.ctx.environ.get(COOKIE_ENV) or os.environ.get(COOKIE_ENV, "")
        raw = (
            str(credentials.extra.get("cookie", "") or "")
            or (credentials.password if looks_like_cookie_header(credentials.password) else "")
            or env_cookie
        ).strip()
        if not raw:
            username = str(credentials.username or "").strip()
            password = str(credentials.password or "")
            if username and password:
                ok = await self._login_with_password(username, password)
                if not ok:
                    raise ChaoxingLoginError("学习通账密登录没有成功（可能被风控要求验证码）")
                return True
            raise ChaoxingLoginError(
                "学习通需要账号密码，或把浏览器里的 Cookie 整串贴到密码栏登录一次，"
                f"或设置环境变量 {COOKIE_ENV}。"
            )
        self.adopt_cookies(raw, username=credentials.username)
        ok = await self._probe()
        self._probe_ok = ok
        self._logged_in = ok
        if not ok:
            self.log.warning("学习通会话无效（返回登录页）—— cookie 可能已过期")
        return ok

    def export_session(self) -> dict[str, Any] | None:
        if not self._logged_in:
            return None
        return {"username": self._username, "cookie_header": self._jar_header()}

    def import_session(self, data: dict[str, Any]) -> bool:
        header = str(data.get("cookie_header") or "")
        if not header:
            return False
        self.adopt_cookies(header, username=str(data.get("username") or ""))
        return True

    # -- 数据 -------------------------------------------------------------

    # -- 浏览器自动化入口 -------------------------------------------------

    def saved_state_path(self) -> Path:
        """浏览器登录态的落盘位置（账密登录一次，之后刷课/考试都复用它）。"""
        base = self.ctx.environ.get("KEDAIBIAO_SESSION_DIR") or "data/sessions"
        return Path(base) / "chaoxing.storage_state.json"

    def answerer(self) -> Any:
        """作答器：优先大模型（能读公式截图），兜底规则答题器。"""
        from kedaibiao.tasks.answerer import ChainAnswerer, RuleAnswerer  # noqa: PLC0415
        from kedaibiao.tasks.llm import build_llm_answerer  # noqa: PLC0415

        chain: list[Any] = []
        llm = build_llm_answerer(dict(self.ctx.environ or {}))
        if llm is not None:
            chain.append(llm)
        chain.append(RuleAnswerer())
        return ChainAnswerer(chain, min_confidence=0.0)

    @property
    def automation(self) -> ChaoxingAutomation:
        """刷课 / 刷题 / 考试的门面（每次都新建，无状态）。"""
        return ChaoxingAutomation(self)

    async def _login_with_password(self, username: str, password: str) -> bool:
        """账密登录：起无头浏览器走登录页，成功后把 storage_state 落盘。"""
        from kedaibiao.browser.pw import PwBrowser, PwConfig  # noqa: PLC0415
        from kedaibiao.platforms.chaoxing import login as login_mod  # noqa: PLC0415

        state_path = self.saved_state_path()
        async with PwBrowser(PwConfig(headless=True)) as browser:
            page = await browser.new_page()
            await login_mod.login_with_password(page, username, password)
            await browser.save_state(state_path)
            cookies = await browser.cookies()
        pairs = {
            str(item.get("name", "")): str(item.get("value", ""))
            for item in cookies
            if item.get("name")
        }
        installed = install_cookies(self.ctx, pairs)
        self._logged_in = True
        self._username = username
        self._probe_ok = None
        self.log.info(
            "学习通账密登录成功：装 %d 条 cookie，会话已保存到 %s",
            installed,
            state_path,
        )
        return True

    async def list_courses(self) -> list[Course]:
        if not await self.is_logged_in():
            raise ChaoxingLoginError(
                "学习通会话已失效。请在控制台「账号」页重新贴一次 Cookie。"
            )
        notes: list[str] = []
        for index, url in enumerate(endpoints.COURSE_LIST_URLS, start=1):
            status, body = await self._get(url)
            if status == 0:
                notes.append(f"[{index}] 网络失败: {body}")
                continue
            if parsing.looks_like_login(body, endpoints.LOGIN_MARKERS):
                self._probe_ok = False
                raise ChaoxingLoginError(
                    f"学习通返回了登录页（入口 {index}）—— 会话已失效，请重新绑定 Cookie。"
                )
            courses = parsing.parse_courses(body)
            if courses:
                self.log.info("学习通课程：%d 门（入口 %d）", len(courses), index)
                return courses
            notes.append(f"[{index}] HTTP {status}，解析出 0 门课")
        self.log.warning("学习通没有解析出课程：%s", "；".join(notes))
        return []

    async def list_assignments(self, course: Course) -> list[Assignment]:
        raw = course.raw
        params = {
            "courseid": raw.get("courseid", ""),
            "clazzid": raw.get("clazzid", ""),
            "cpi": raw.get("cpi", ""),
            "enc": raw.get("enc", ""),
        }
        if not params["courseid"]:
            raise ChaoxingError(f"课程 {course.name!r} 缺少 courseid，无法定位作业列表")

        for index, template in enumerate(endpoints.WORK_LIST_URL_TEMPLATES, start=1):
            status, body = await self._get(template.format(**params))
            if status == 0:
                continue
            if parsing.looks_like_login(body, endpoints.LOGIN_MARKERS):
                self._probe_ok = False
                raise ChaoxingLoginError(
                    f"拉取 {course.name!r} 的作业列表时返回登录页 —— 会话已失效。"
                )
            works = parsing.parse_works(body, course_name=course.name)
            if works:
                return works
            self.log.debug("作业入口 %d 未解析出条目（HTTP %s）", index, status)
        # 学习通的「作业」实际挂在课程页的**考试** tab（exam-ans/exam-list）里，
        # 老的 work/list 接口对它基本恒返回「暂无作业」。所以 HTTP 侧没解析到，
        # 就用浏览器读一次考试列表 —— 这样控制台「作业」页才不是空的。
        return await self._assignments_from_exam_list(course)

    async def _assignments_from_exam_list(self, course: Course) -> list[Assignment]:
        """把课程页「考试」tab 里的作业/测验/考试映射成 ``Assignment``。"""
        items = await self.automation.list_exams(course)
        out: list[Assignment] = []
        for item in items:
            if not item.title:
                continue
            title = item.title
            kind = (
                TaskKind.EXAM
                if any(word in title for word in ("测验", "测试", "考试", "阶段测验"))
                else TaskKind.HOMEWORK
            )
            deadline = _parse_deadline(item.end_time_text) or _deadline_from_onclick(
                item.raw_onclick
            )
            finished = item.status == "done"
            out.append(
                Assignment(
                    remote_id=item.exam_id or item.title,
                    title=title,
                    kind=kind,
                    course_name=course.name,
                    deadline=deadline,
                    progress=1.0 if finished else 0.0,
                    finished=finished,
                    raw={
                        "status": item.status,
                        "mobile_only": item.mobile_only,
                        "score": item.score,
                        "score_text": item.score_text,
                        "end_time_text": item.end_time_text,
                        "action": item.action,
                        "exam_id": item.exam_id,
                        "relation_id": item.relation_id,
                    },
                )
            )
        self.log.info("学习通考试列表：%s 共 %d 条", course.name, len(out))
        out.extend(await self._assignments_from_section_quizzes(course))
        return out

    async def _assignments_from_section_quizzes(self, course: Course) -> list[Assignment]:
        """把「章节测验」任务点也列进作业页，控制台里就能直接作答。

        学习通的章节测验挂在每个小节的第二个 tab（课件页 ``num=1``），和课程级
        考试不是一回事；但用户眼里它们都是「要做的测验」，所以并排显示。
        """
        try:
            points = await self.automation.task_points(course, only_pending=False)
        except Exception as exc:  # noqa: BLE001 - 章节探测失败不影响考试列表
            self.log.warning("学习通章节测验枚举失败：%s", exc)
            return []
        out: list[Assignment] = []
        for point in points:
            if point.kind != "quiz":
                continue
            out.append(
                Assignment(
                    remote_id=f"quiz:{point.chapter_id}",
                    title=f"{point.index_text} {point.section_title}（章节测验）".strip(),
                    kind=TaskKind.PRACTICE,
                    course_name=course.name,
                    finished=False,
                    raw={"status": "quiz", "chapter_id": point.chapter_id},
                )
            )
        return out


# ---------------------------------------------------------------------------
# 浏览器自动化（刷课 / 刷题 / 考试）—— 控制台「刷课」与「考试」入口都走这里
# ---------------------------------------------------------------------------


def _storage_state_from_header(header: str, *, domain: str = COOKIE_DOMAIN) -> dict[str, Any]:
    """把整串 cookie 变成 Playwright ``storage_state``（浏览器直接复用登录态）。"""
    cookies = parse_cookie_header(header)
    return {
        "cookies": [
            {
                "name": name,
                "value": value,
                "domain": domain,
                "path": "/",
                "expires": -1,
                "httpOnly": False,
                "secure": True,
                "sameSite": "Lax",
            }
            for name, value in cookies.items()
        ],
        "origins": [],
    }


class ChaoxingAutomation:
    """学习通页面自动化的门面：刷课、章节测验、考试。

    单独一个类而不是塞进适配器：这些动作都要起浏览器、跑几分钟，与「读课程
    列表」那种一次 GET 的语义不同，放在一起会让适配器越来越难读。
    """

    def __init__(self, adapter: ChaoxingAdapter) -> None:
        self.adapter = adapter

    def _config(self, *, storage_state: dict[str, Any] | str | None = None) -> Any:
        from kedaibiao.browser.pw import PwConfig  # noqa: PLC0415 - 可选依赖

        state = storage_state
        if state is None:
            saved = self.adapter.saved_state_path()
            if saved and saved.is_file():
                state = str(saved)
            else:
                state = _storage_state_from_header(self.adapter._jar_header())
        return PwConfig(headless=True, storage_state=state)

    async def _page(self, browser: Any) -> Any:
        page = await browser.new_page()
        from kedaibiao.platforms.chaoxing import login as login_mod  # noqa: PLC0415

        await login_mod.ensure_login(page, state_path=self.adapter.saved_state_path())
        return page

    def _ids(self, course: Course) -> tuple[str, str, str]:
        raw = course.raw or {}
        return (
            str(raw.get("courseid", "")),
            str(raw.get("clazzid", "")),
            str(raw.get("cpi", "")),
        )

    async def task_points(self, course: Course, *, only_pending: bool = True) -> list[Any]:
        from kedaibiao.browser.pw import PwBrowser  # noqa: PLC0415
        from kedaibiao.platforms.chaoxing.study import ChaoxingStudy  # noqa: PLC0415

        course_id, clazz_id, cpi = self._ids(course)
        async with PwBrowser(self._config()) as browser:
            page = await self._page(browser)
            study = ChaoxingStudy(
                page, course_id=course_id, clazz_id=clazz_id, cpi=cpi
            )
            await study.open_course()
            return await study.task_points(only_pending=only_pending)

    async def play_course(
        self,
        course: Course,
        *,
        only_pending: bool = True,
        limit: int = 0,
        kinds: tuple[str, ...] = ("video", "document"),
        rate: float = 2.0,
        doc_seconds: float = 0.0,
        emit: Any | None = None,
        cancel: Any | None = None,
    ) -> dict[str, Any]:
        from kedaibiao.browser.pw import PwBrowser  # noqa: PLC0415
        from kedaibiao.platforms.chaoxing.study import ChaoxingStudy  # noqa: PLC0415

        course_id, clazz_id, cpi = self._ids(course)
        async with PwBrowser(self._config()) as browser:
            page = await self._page(browser)
            study = ChaoxingStudy(
                page,
                course_id=course_id,
                clazz_id=clazz_id,
                cpi=cpi,
                emit=emit,
                rate=rate,
            )
            report = await study.run(
                kinds=kinds,
                limit=limit,
                only_pending=only_pending,
                doc_seconds=doc_seconds,
                cancel=cancel,
            )
        return {
            "course": course.name,
            "summary": report.summary(),
            "steps": [
                {
                    "task": step.task.name,
                    "kind": step.task.kind,
                    "section": step.task.index_text,
                    "status": step.status,
                    "detail": step.detail,
                    "seconds": round(step.seconds, 1),
                }
                for step in report.steps
            ],
        }

    async def meta(
        self,
        course: Course,
        *,
        action: str,
        exam_id: str = "",
        chapter: str = "",
    ) -> dict[str, Any]:
        """考试 / 测验的元信息：时间窗口、时长、剩余提交次数。

        只在**不开始作答**的页面上取：考试读「考试须知」，章节测验读课件页的
        ``doHomeWorkNew`` 表单（隐藏域里有上限/已用次数）。进考试就会开始计时，
        所以这里绝不进答题页。
        """
        from kedaibiao.browser.pw import PwBrowser  # noqa: PLC0415
        from kedaibiao.platforms.chaoxing import quiz as quiz_mod  # noqa: PLC0415
        from kedaibiao.platforms.chaoxing.study import CARDS_URL  # noqa: PLC0415

        course_id, clazz_id, cpi = self._ids(course)
        async with PwBrowser(self._config()) as browser:
            page = await self._page(browser)
            if action == "exam":
                if not exam_id:
                    raise ValueError("exam 需要 exam_id")
                url = (
                    "https://mooc1.chaoxing.com/exam-ans/exam/test/examcode/examnotes"
                    f"?courseId={course_id}&classId={clazz_id}&examId={exam_id}&cpi={cpi}"
                )
                await page.goto(url, timeout=60000)
                await asyncio.sleep(2.5)
                data = quiz_mod.parse_exam_notes(await page.html())
                data["kind"] = "exam"
                return data
            if not chapter:
                raise ValueError("quiz 需要 chapter（小节 id）")
            await page.goto(
                CARDS_URL.format(
                    clazzid=clazz_id,
                    courseid=course_id,
                    chapter=chapter,
                    num=1,
                    cpi=cpi,
                ),
                timeout=90000,
            )
            deadline = 60.0
            waited = 0.0
            frame = None
            while waited < deadline:
                frame = await page.frame("doHomeWorkNew")
                if frame is not None:
                    break
                await asyncio.sleep(1.5)
                waited += 1.5
            if frame is None:
                return {"kind": "quiz", "attempts_limit": None, "attempts_remaining": None}
            data = quiz_mod.parse_work_meta(await frame.html())
            data["kind"] = "quiz"
            return data

    async def meta_batch(
        self, course: Course, items: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """一次浏览器会话里批量取元信息（作业列表「补齐信息」用）。

        每个考试/测验要开一个页面读它自己的须知/表单，单个约 2~5 秒；共用同一个
        Chrome 实例能省掉反复启动的开销。单项失败只记错误，不拖累其它项。
        """
        from kedaibiao.browser.pw import PwBrowser  # noqa: PLC0415
        from kedaibiao.platforms.chaoxing import quiz as quiz_mod  # noqa: PLC0415
        from kedaibiao.platforms.chaoxing.study import CARDS_URL  # noqa: PLC0415

        course_id, clazz_id, cpi = self._ids(course)
        results: list[dict[str, Any]] = []
        async with PwBrowser(self._config()) as browser:
            page = await self._page(browser)
            for item in items:
                action = str(item.get("action") or "")
                try:
                    if action == "exam":
                        exam_id = str(item.get("exam_id") or "")
                        url = (
                            "https://mooc1.chaoxing.com/exam-ans/exam/test/examcode/examnotes"
                            f"?courseId={course_id}&classId={clazz_id}"
                            f"&examId={exam_id}&cpi={cpi}"
                        )
                        await page.goto(url, timeout=60000)
                        await asyncio.sleep(1.8)
                        data = quiz_mod.parse_exam_notes(await page.html())
                    else:
                        chapter = str(item.get("chapter") or "")
                        await page.goto(
                            CARDS_URL.format(
                                clazzid=clazz_id,
                                courseid=course_id,
                                chapter=chapter,
                                num=1,
                                cpi=cpi,
                            ),
                            timeout=90000,
                        )
                        frame = None
                        for _ in range(20):
                            frame = await page.frame("doHomeWorkNew")
                            if frame is not None:
                                break
                            await asyncio.sleep(1.2)
                        data = (
                            quiz_mod.parse_work_meta(await frame.html())
                            if frame is not None
                            else {"attempts_note": "", "attempts_limit": None}
                        )
                    data["ok"] = True
                except Exception as exc:  # noqa: BLE001 - 单项失败不影响其它
                    data = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
                results.append(data)
        return results

    async def list_exams(self, course: Course) -> list[Any]:
        from kedaibiao.browser.pw import PwBrowser  # noqa: PLC0415
        from kedaibiao.platforms.chaoxing.quizrun import ChaoxingQuiz  # noqa: PLC0415

        course_id, clazz_id, cpi = self._ids(course)
        async with PwBrowser(self._config()) as browser:
            page = await self._page(browser)
            runner = ChaoxingQuiz(
                page, course_id=course_id, clazz_id=clazz_id, cpi=cpi
            )
            return await runner.list_exams()

    async def run_exam(
        self,
        course: Course,
        *,
        exam_id: str,
        title: str = "",
        relation_id: str = "",
        confirm: bool = False,
        emit: Any | None = None,
        on_items: Any | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        from kedaibiao.browser.pw import PwBrowser  # noqa: PLC0415
        from kedaibiao.platforms.chaoxing.quiz import ExamItem  # noqa: PLC0415
        from kedaibiao.platforms.chaoxing.quizrun import ChaoxingQuiz  # noqa: PLC0415

        course_id, clazz_id, cpi = self._ids(course)
        item = ExamItem(
            title=title or exam_id,
            status="todo",
            exam_id=exam_id,
            relation_id=relation_id,
            action="goTest",
        )
        async with PwBrowser(self._config()) as browser:
            page = await self._page(browser)
            runner = ChaoxingQuiz(
                page,
                course_id=course_id,
                clazz_id=clazz_id,
                cpi=cpi,
                answerer=self.adapter.answerer(),
                emit=emit,
            )
            report = await runner.run_exam(
                item, confirm=confirm, on_items=on_items, force=force
            )
        return _report_dict(report)

    async def run_section_quiz(
        self,
        course: Course,
        *,
        chapter_id: str,
        confirm: bool = False,
        emit: Any | None = None,
        on_items: Any | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        from kedaibiao.browser.pw import PwBrowser  # noqa: PLC0415
        from kedaibiao.platforms.chaoxing.quizrun import ChaoxingQuiz  # noqa: PLC0415

        course_id, clazz_id, cpi = self._ids(course)
        async with PwBrowser(self._config()) as browser:
            page = await self._page(browser)
            runner = ChaoxingQuiz(
                page,
                course_id=course_id,
                clazz_id=clazz_id,
                cpi=cpi,
                answerer=self.adapter.answerer(),
                emit=emit,
            )
            report = await runner.run_section_quiz(
                chapter_id, confirm=confirm, on_items=on_items, force=force
            )
        return _report_dict(report)


def _report_dict(report: Any) -> dict[str, Any]:
    """把 QuizReport 转成控制台能直接渲染的纯数据。"""
    return {
        "title": report.title,
        "summary": report.summary(),
        "submitted": report.submitted,
        "score": report.score,
        "score_text": report.score_text,
        "submit_note": report.submit_note,
        "items": [
            {
                "number": item.number,
                "type": item.type_name,
                "answer": item.answer,
                "confidence": round(item.confidence, 2),
                "source": item.source,
                "verified": item.verified,
                "unsupported": item.unsupported,
                "text": item.text[:120],
            }
            for item in report.items
        ],
    }
