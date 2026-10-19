"""批改网协议客户端：注入式 ``HttpClient`` + 标准库 RSA 登录。

职责边界（``docs/integration-guide.md`` §2/§3.3）：

* 本模块只管**登录与会话判据**；HTML 解析在 ``parse.py``，适配器语义在 ``adapter.py``；
* 传输一律走注入的 ``HttpClient``（``http.get`` / ``http.post_form``）——
  本模块**不 import urllib / requests / socket**，也**不管 cookie 文件**
  （载入与落盘由 ``runtime.build_http_client()`` / ``Runtime._persist_cookies()`` 负责）；
* 端点、表单字段名、cookie 名一律取自 ``protocol.py``（唯一出处）；
* **不限速**：节流是 ``core.scheduler.Scheduler`` 的职责，本模块没有
  ``min_interval``、不 sleep；
* 凭据由调用方传入（正常路径是控制台绑定的平台账号，见 ``security.accounts``），
  本模块**不读 os.environ / .env**。

**隐私红线**：密码、密文、cookie 值绝不进日志或异常。日志里只出现 cookie 的
**名字**；异常里只有 HTTP 方法、路径与状态码，绝不含表单体。

只读纪律：除登录那一次 POST 外只发 GET；本模块不声明任何写入端点。
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from kedaibiao.platforms.pigai import protocol, rsa_login

if TYPE_CHECKING:  # 仅用于类型标注；运行期不需要，也避免与共享层强耦合
    from kedaibiao.net.http import HttpClient, Response

__all__ = ["PigaiClient", "PigaiError", "PigaiResponseError"]

#: 登录成功后 cookie jar 里必然出现的 cookie 名（判据之一）
_SUCCESS_COOKIE: str = protocol.LOGIN_SUCCESS_COOKIE
#: 登录表单的**严格**标识（带属性名）。**``login()`` 的判据用它**：
#: 裸串 ``lg_from_w`` 会被脚本文案误命中 —— 本站自己的 ``2020.js`` 里就有
#: ``$('#lg_from_w').submit();``，用裸串会把正常页面判成「未登录」。
#: （离线核对：两份匿名首页样本里 ``lg_from_w`` 各只出现 1 次，且正是 ``id="lg_from_w"``。）
#: 注意 ``is_logged_in()`` 已改用**正面**标记 :data:`_LIST_MARKER`，见其 docstring。
_STRICT_MARKER: str = protocol.LOGIN_FORM_MARKER_STRICT
#: ``is_logged_in()`` 的**正面**标记：列表容器 ``id="essayList"``。
#: 由契约常量 ``protocol.LIST_CONTAINER_ID`` 拼出属性形态 —— 同样避开裸串误命中
#: （列表页里 ``#essayList`` 还会出现在内联 CSS 里）。
#: 离线核对（``data/recon/pigai/raw/``）：已登录 ``?c=write`` 有该属性；匿名失效页
#: ``b_anon_write.html`` 既无该属性也无登录表单标识 → 否定判据没有鉴别力。
_LIST_MARKER: str = f'id="{protocol.LIST_CONTAINER_ID}"'
#: cookie 头里 ``name=value`` 的探测模式（仅用于 duck-typing 的 transport）
_COOKIE_HEADER_RE: re.Pattern[str] = re.compile(
    r"(?:^|;\s*)" + re.escape(_SUCCESS_COOKIE) + r"=([^;]+)"
)
#: 每个请求都带的头。``UrllibTransport`` 自己不设 UA（默认会是 ``Python-urllib/3.x``），
#: 而 ``protocol.USER_AGENT`` 是冻结契约里真机探针用过的浏览器 UA —— 由本层显式带上。
_BASE_HEADERS: dict[str, str] = {
    "User-Agent": protocol.USER_AGENT,
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


class PigaiError(Exception):
    """``PigaiClient`` 的异常基类。消息里不含密码、密文、表单体或 cookie 值。"""


class PigaiResponseError(PigaiError):
    """服务端返回了非成功状态码。刻意不携带响应体（正文可能含他人数据）。"""

    def __init__(self, method: str, path: str, status: int) -> None:
        super().__init__(f"{method} {path} 返回 HTTP {status}")
        self.method = method
        self.path = path
        self.status = status


def _check_read_only_path(path: str) -> str:
    """只接受站内只读路径（``protocol.Endpoints.*``）。挡掉绝对 URL，避免被指向别处。"""
    if not isinstance(path, str) or not path:
        raise ValueError("path 不能为空")
    if not path.startswith("/") or path.startswith("//") or "://" in path:
        raise ValueError("path 必须是以单个 / 开头的站内路径（取自 protocol.Endpoints）")
    return path


class PigaiClient:
    """批改网学生端协议客户端（只读）。

    典型用法（``adapter.py`` 里）：

    >>> client = PigaiClient(ctx.client(), logger=self.log)
    >>> if not await client.is_logged_in():
    ...     await client.login(creds.username, creds.password)
    >>> html = await client.get(protocol.Endpoints.ASSIGNMENTS_PENDING)

    注入的 ``HttpClient`` **由调用方持有**：``aclose()`` 只标记本对象失效，
    不会关闭 ``HttpClient``（它通常被 runtime 复用）。
    """

    def __init__(self, http: HttpClient, *, logger: logging.Logger | None = None) -> None:
        self.http = http
        self.log = logger or logging.getLogger("kedaibiao.platforms.pigai")
        self._closed = False

    # -- 生命周期 ---------------------------------------------------------

    async def login(self, username: str, password: str) -> bool:
        """登录并返回是否成功。**唯一一次 POST**，只建立会话，不改业务状态。

        流程（``docs/research/pigai.md`` §4.4 实测）：

        1. ``GET /`` 取服务端下发的 ``old`` cookie；
        2. ``POST /index.php?a=login``，表单字段
           ``username`` / ``password=""`` / ``remember=1`` / ``checkhash=""`` /
           ``password_encrypt=<RSA密文>``，带 ``Referer`` 与 ``Origin``；
        3. 成功判据 = 响应页**不含**严格登录表单标识
           （``LOGIN_FORM_MARKER_STRICT``）**且** cookie jar 里有
           ``LOGIN_SUCCESS_COOKIE``。

        失败返回 ``False``（凭据被拒 / 仍是登录页）；HTTP 非成功状态抛
        ``PigaiResponseError``；网络层失败由 ``HttpClient`` 的 ``TransportError``
        （继承 ``OSError``，可被调度层重试）向上抛。

        ⚠ 与 ``is_logged_in()`` 的判据**有意不对称**（这是设计，不是缺陷，请勿「统一」）：

        * ``login()`` 回答「**凭据交换成功了吗**」—— cookie 是这次交换的直接产物，
          所以判据里有 ``_JUKU_USER``；
        * ``is_logged_in()`` 回答「**我还能读到受保护数据吗**」—— 页面内容才是直接证据，
          所以看正面标记、完全不看 cookie（陈旧 cookie 会说谎）。

        统一两者只会多一层耦合：例如登录成功后若被引到「完善资料」页，
        正面判据会说「未登录」而凭据交换其实已成功。

        明文密码只在此方法的内存里短暂存在：不写日志、不进异常、不落盘。
        """
        self._ensure_open()
        if not isinstance(username, str) or not username.strip():
            raise ValueError("username 不能为空")
        if not isinstance(password, str) or not password:
            raise ValueError("password 不能为空")

        # 1) 先取首页下发的 cookie（真机流程的第一步）
        home = await self._get_response(protocol.Endpoints.HOME)
        self.log.debug(
            "登录前首页 %s -> HTTP %s %dB", protocol.Endpoints.HOME, home.status, len(home.body)
        )

        # 2) 加密（密码 UTF-8 → PKCS#1 v1.5 → base64）；明文到此为止
        form = {
            protocol.LOGIN_FIELD_USER: username.strip(),
            protocol.LOGIN_FIELD_PASSWORD: "",
            protocol.LOGIN_FIELD_REMEMBER: "1",
            protocol.LOGIN_FIELD_CHECKHASH: "",
            protocol.LOGIN_FIELD_PASSWORD_ENCRYPT: rsa_login.encrypt_password(password),
        }

        # 3) 全程唯一一次 POST
        resp: Response = await self.http.post_form(
            protocol.BASE_URL + protocol.LOGIN_PATH,
            form,
            headers={
                **_BASE_HEADERS,
                "Referer": protocol.BASE_URL + protocol.Endpoints.HOME,
                "Origin": protocol.BASE_URL,
            },
        )
        if not resp.ok:
            raise PigaiResponseError("POST", protocol.LOGIN_PATH, resp.status)

        page = resp.text()
        if _STRICT_MARKER in page:
            self.log.warning(
                "批改网登录失败：响应页仍是登录表单（HTTP %s，%dB）", resp.status, len(page)
            )
            return False
        if not self._has_login_cookie():
            self.log.warning(
                "批改网登录失败：响应页已不是登录表单，但 cookie 里没有 %s", _SUCCESS_COOKIE
            )
            return False

        # 只报 cookie 的**名字**，值绝不进日志
        self.log.info(
            "批改网登录成功（HTTP %s，落地页 %dB，登录态 cookie=%s）",
            resp.status,
            len(page),
            _SUCCESS_COOKIE,
        )
        return True

    async def is_logged_in(self) -> bool:
        """会话是否仍然有效。一次廉价探针：``GET /index.php?c=write``。

        **判据 = 探针页里出现正面标记** ``id="essayList"``
        （:data:`_LIST_MARKER`，由 ``protocol.LIST_CONTAINER_ID`` 拼出）。

        为什么不用否定判据（task-7 的旧方案「页面不含登录表单 + jar 里有 cookie」，**已废弃**）：
        会话失效时服务端返回的是 **200 的引导页**（「使用此功能，请先登录」+ JS 跳
        ``?a=loginForm``），它**既不含登录表单标识、也不是合法页** ——
        离线样本 ``b_anon_write.html`` 实测两者皆无。所以「不含登录表单」在会话失效时
        **恒成立**，毫无鉴别力；再配一条陈旧 ``_JUKU_USER`` 就会双双通过 → 误判已登录。

        正面判据的两个好处：

        1. **完全不看 cookie jar** → 陈旧 cookie 的窗口直接关闭（比补第二条 cookie 判据彻底）；
        2. 合法列表页**哪怕是空的**也含该容器（只有表头时同样有 ``<div id="essayList">``）。

        两条判据的分工（**有意不对称**，不是缺陷，请勿「统一」）：

        * HTTP 非 2xx（业务判断：服务端说这次探针不成功）→ 返回 **False**，走重登；
        * 网络层异常（``TransportError``，连不上/超时）→ **原样抛出**，交给
          ``core.scheduler`` 的退避重试。**不要**在这里吞成 False：``refresh_session()``
          位于 Scheduler 之外，吞掉异常换不来任何重试，只会紧接着去发一次登录 POST ——
          站点故障时反复发登录请求正是要避免的风控面。

        与 ``login()`` 的判据也**有意不对称**：``login()`` 回答「凭据交换成功了吗」
        （cookie 是这次交换的直接产物）；本方法回答「我还能读到受保护数据吗」
        （页面内容才是直接证据）。这也是它完全不看 cookie jar 的原因。

        刻意**不缓存**结果，也**不额外开请求**：探测本身就是一次只读请求。
        """
        self._ensure_open()
        try:
            resp = await self._get_response(protocol.Endpoints.ESSAYS_DONE)
        except PigaiError as exc:
            # 服务端这次探针没给成功响应 → 业务上按未登录处理（走重登）。
            # 注意只捕获 PigaiError；TransportError(OSError) 必须继续向上抛，
            # 否则站点故障时会退化成「不重试 + 紧接着发一次登录 POST」。
            self.log.warning("会话探针拿到非成功响应（%s）→ 保守判未登录", exc)
            return False
        page = resp.text()
        alive = _LIST_MARKER in page
        self.log.debug(
            "会话探针 %s -> HTTP %s %dB，正面标记 %s = %s",
            protocol.Endpoints.ESSAYS_DONE,
            resp.status,
            len(resp.body),
            _LIST_MARKER,
            alive,
        )
        return alive

    async def get(self, path: str) -> str:
        """取一个只读页面的 HTML 原文。

        ``path`` 取自 ``protocol.Endpoints.*``。
        **只读保证由调用方提供**：本方法只校验「站内路径」（拒绝绝对 URL 与
        ``//host``），**不做 ``a=`` 动词白名单** —— 它拦不住
        ``/index.php?c=v2&a=write`` 这类写入口。请只传 ``protocol.Endpoints``
        里声明的只读端点；本模块不声明也不调用任何写入端点。
        """
        self._ensure_open()
        resp = await self._get_response(path)
        self.log.debug("GET %s -> HTTP %s %dB", path, resp.status, len(resp.body))
        return resp.text()

    async def aclose(self) -> None:
        """标记本客户端失效。**不关闭注入的 ``HttpClient``**（那是调用方的资源）。"""
        if self._closed:
            return
        self._closed = True
        self.log.debug("批改网客户端已关闭（注入的 HttpClient 仍归调用方所有）")

    async def __aenter__(self) -> PigaiClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    # -- 内部 -------------------------------------------------------------

    async def _get_response(self, path: str) -> Response:
        """GET 一个只读路径；非成功状态码统一转成 ``PigaiResponseError``。"""
        resp: Response = await self.http.get(
            protocol.BASE_URL + _check_read_only_path(path), headers=dict(_BASE_HEADERS)
        )
        if not resp.ok:
            raise PigaiResponseError("GET", path, resp.status)
        return resp

    def _has_login_cookie(self) -> bool:
        """cookie jar 里是否有主登录态 cookie。

        现在只被 ``login()`` 当作**成功判据之一**使用；``is_logged_in()`` 已改为
        看正面标记（陈旧 cookie 不可信，见其 docstring）。

        用 **duck-typing** 适配 transport（``Transport`` 协议里并没有 cookie API）：

        1. ``transport.cookies``（``UrllibTransport`` 的 ``CookieJar``）→ 看名字与非空值；
        2. 退一步用 ``transport.cookie_header()`` 的字符串。

        两者都拿不到时**保守判否**：宁可让上层重登一次，也不误判成已登录。
        只看 cookie 的**名字**，值绝不进日志。
        """
        transport = getattr(self.http, "transport", None)
        if transport is None:
            return False
        jar = getattr(transport, "cookies", None)
        if jar is not None:
            try:
                return any(
                    getattr(cookie, "name", None) == _SUCCESS_COOKIE
                    and bool(getattr(cookie, "value", ""))
                    for cookie in jar
                )
            except TypeError:  # pragma: no cover - 不是可迭代的 jar
                return False
        header = getattr(transport, "cookie_header", None)
        if callable(header):
            try:
                raw = str(header() or "")
            except Exception:  # noqa: BLE001 - 拿不到 cookie 头就当没有
                return False
            return bool(_COOKIE_HEADER_RE.search(raw))
        return False

    def _ensure_open(self) -> None:
        if self._closed:
            raise PigaiError("PigaiClient 已关闭（aclose 之后请新建实例；HttpClient 归调用方）")
