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

import http.cookiejar
import os
from typing import Any

from kedaibiao.browser.capture import parse_cookie_header
from kedaibiao.core.models import Assignment, Course, Credentials
from kedaibiao.platforms.base import AdapterContext, Capabilities, PlatformAdapter
from kedaibiao.platforms.chaoxing import endpoints, parsing

#: 会话 cookie 的环境变量名
COOKIE_ENV = "KEDAIBIAO_CHAOXING_COOKIE"

#: cookie 挂在根域上：mooc1 / mooc2-ans / i / passport2 都是它的子域
COOKIE_DOMAIN = ".chaoxing.com"

#: 用来区分「整串 cookie」和「一个普通字符串」的特征项
_COOKIE_MARKERS = ("fid=", "_uid=", "UID=", "uf=", "ws=", "p_auth_token=")

_HTML_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    "Referer": "https://i.chaoxing.com/",
}


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
            raise ChaoxingLoginError(
                "学习通没有账密登录路径（扫码/验证码由人完成）。请在控制台「账号」页"
                "添加账号后，把浏览器里的 Cookie 整串贴到密码栏登录一次，"
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
        return []
