"""U校园请求编排：只依赖 ``kedaibiao.net.http.HttpClient``。

本层负责四件事：

1. **会话连续性**：登录三步共用同一个 ``HttpClient``（也就是同一个带 cookie
   罐的会话）。规格第 2.1 节把"①与②必须同会话"列为硬约束 —— 用一次性请求
   的客户端会在 ``validate`` 一步失败，且失败形态可能是 HTML，极难定位。
   适配器全程只持有 ``ctx.client()`` 返回的那**一个**对象。
2. **鉴权头按应用选择**：门户发 ``Authorization``，ucontent 发
   ``x-annotator-auth-token``；两者不可互换（规格第 2.3 节）。
3. **成功码按应用判定**：每个调用点显式声明属于哪套判定（规格第 5.1 节）。
4. **异常响应上抛**：HTML 登录页 / 非 JSON / 业务错误码 / 限流码各有专用异常，
   绝不静默返回空结果。

⚠️ 本层**不 sleep、不重试**：限流只抛 :class:`UnipusRateLimited`（带
``retry_after``），退避与重排由 ``core.scheduler`` 决定。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from kedaibiao.net.http import HttpClient, Response, ResponseNotJson
from kedaibiao.platforms.unipus import codes, endpoints, parsing
from kedaibiao.platforms.unipus.codes import CIPHER_SHELL_FAMILY
from kedaibiao.platforms.unipus.crypto import build_annotator_token, encrypt_login_field
from kedaibiao.platforms.unipus.endpoints import UnipusApp
from kedaibiao.platforms.unipus.errors import (
    UnipusApiError,
    UnipusAuthError,
    UnipusError,
    UnipusHttpError,
    UnipusNotLoggedIn,
    UnipusProtocolError,
    UnipusRateLimited,
)
from kedaibiao.platforms.unipus.parsing import (
    Catalog,
    ContentQuestion,
    CourseBinding,
    GroupState,
    RequiredSet,
    SessionInfo,
    StandardAnswer,
)

#: 规格第 9 节：常规接口超时 30 秒。
#: HttpClient 自带的默认值是 15 秒，但那是框架通用值；这里按平台规格取 30。
REQUEST_TIMEOUT_SECONDS = 30.0

#: 非 2xx 时正文摘要的长度上限
BODY_PREVIEW_CHARS = 200


def _system_clock_ms() -> int:
    """默认时间源：Unix epoch 毫秒（annotator token 的 ``exp`` 基准）。"""
    return int(time.time() * 1000)


@dataclass(slots=True)
class UnipusSession:
    """会话里要存的东西（规格第 2.2 节）。

    ⚠️ 只存内存，**不落盘**：本适配器不写任何含令牌的文件。
    """

    portal_token: str = ""
    open_id: str = ""
    display_name: str = ""
    school: str = ""
    app_user_id: str = ""

    @property
    def has_portal_token(self) -> bool:
        return bool(self.portal_token)

    @property
    def ready(self) -> bool:
        return bool(self.portal_token and self.open_id)

    def clear(self) -> None:
        self.portal_token = ""
        self.open_id = ""
        self.display_name = ""
        self.school = ""
        self.app_user_id = ""

    def describe(self) -> str:
        """可安全打日志的描述：**只有标识，没有令牌**。"""
        return (
            f"open_id={self.open_id or '-'} school={self.school or '-'} "
            f"display_name={self.display_name or '-'} "
            f"portal_token={'已持有' if self.portal_token else '无'}"
        )


@dataclass(slots=True)
class UnipusClient:
    """协议编排器。构造时注入 ``HttpClient``（测试注入假 Transport 即可离线）。"""

    http: HttpClient
    logger: logging.Logger = field(default_factory=lambda: logging.getLogger("kedaibiao.unipus"))
    timeout: float = REQUEST_TIMEOUT_SECONDS
    #: 毫秒时间源，注入后 annotator token 可复现（纯函数化的最后一环）
    clock: Any = field(default=_system_clock_ms)
    session: UnipusSession = field(default_factory=UnipusSession)

    # -- 请求层 -----------------------------------------------------------

    def portal_host_headers(self) -> dict[str, str]:
        """门户"微应用宿主"四头（考核/任务树类接口必需）。

        缺任何一项都会被当成"认不出是哪个应用"，返回 ``6011 feature denied``
        （规格第 3.4 节）。
        """
        if not self.session.open_id:
            raise UnipusNotLoggedIn("宿主四头需要 open_id（ssoId），当前会话为空")
        return {
            endpoints.HOST_HEADER_APP_ID: endpoints.HOST_APP_ID,
            endpoints.HOST_HEADER_PLATFORM: endpoints.HOST_PLATFORM_WEB,
            endpoints.HOST_HEADER_SCHOOL: self.session.school,
            endpoints.HOST_HEADER_OPENID: self.session.open_id,
        }

    def auth_headers(self, app: UnipusApp, *, host_headers: bool = False) -> dict[str, str]:
        """按应用取鉴权头。**不可自由组合**：调用方只能选应用，不能传 token。"""
        if app is UnipusApp.PORTAL:
            if not self.session.portal_token:
                raise UnipusNotLoggedIn("门户接口需要门户 JWT，当前会话没有；请先 login()")
            headers = {endpoints.PORTAL_AUTH_HEADER: self.session.portal_token}
            if host_headers:
                headers.update(self.portal_host_headers())
            return headers
        if app is UnipusApp.UCONTENT:
            # 每次用前现铸，不落盘（规格第 2.3 节）
            token = build_annotator_token(self.session.open_id, now_ms=int(self.clock()))
            return {endpoints.UCONTENT_AUTH_HEADER: token}
        return {}

    async def request(
        self,
        app: UnipusApp,
        method: str,
        path: str,
        *,
        family: str,
        path_params: dict[str, object] | None = None,
        query: dict[str, object] | None = None,
        payload: dict[str, Any] | None = None,
        host_headers: bool = False,
    ) -> Any:
        """发一次请求并按 ``family`` 的成功码判定。

        ``family`` 必填：SSO / 门户 / ucontent 三套码不统一，调用点必须自己
        声明（规格第 5.1 节）。
        """
        url = endpoints.url_for(app, path, **(path_params or {}))
        if query:
            url = endpoints.query_url(url, **query)
        headers = self.auth_headers(app, host_headers=host_headers)
        body: bytes | None = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"

        # 域名纪律：发请求前再断言一次（规格第 1 节）
        endpoints.assert_url_for_app(app, url)
        response = await self.http.request(
            method.upper(), url, headers=headers, body=body, timeout=self.timeout
        )
        document = self._decode(response, url)
        if not codes.success_check(family, document):
            raise self._business_error(app, url, document)
        return document

    # -- 响应处理 ---------------------------------------------------------

    def _decode(self, response: Response, url: str) -> Any:
        """状态码与正文形态检查：**异常响应不静默**。"""
        text = response.text()
        if not response.ok:
            if response.status in (401, 403) or codes.looks_like_html(text):
                raise UnipusAuthError(codes.login_page_message(url))
            raise UnipusHttpError(
                status=response.status, url=url, body_preview=text[:BODY_PREVIEW_CHARS]
            )
        if codes.looks_like_html(text):
            # 200 也可能是登录页（网关改过状态码的形态）
            raise UnipusAuthError(codes.login_page_message(url))
        if not text.strip():
            raise UnipusProtocolError(f"{url} 返回空正文，期望 JSON")
        try:
            return response.json()
        except ResponseNotJson as exc:
            raise UnipusProtocolError(
                f"{url} 返回的不是 JSON: {text[:BODY_PREVIEW_CHARS]!r}"
            ) from exc

    def _business_error(self, app: UnipusApp, url: str, document: Any) -> UnipusApiError:
        message = codes.extract_message(document)
        code = codes.result_code(document)
        if codes.is_rate_limited(document):
            return UnipusRateLimited(
                app=app.value, url=url, message=message, code=code, payload=document
            )
        return UnipusApiError(
            app=app.value, url=url, message=message, code=code, payload=document
        )

    # -- 登录（规格第 2.1 节） -------------------------------------------

    async def login(self, username: str, password: str) -> bool:
        """密码登录三步。

        返回 ``False`` 仅表示**登录端点以业务码拒绝了这次尝试**（凭据问题）；
        其余失败（validate 失败、用户信息结构不对、HTML 登录页）一律上抛，
        避免把"链路坏了"伪装成"密码错了"。
        """
        self.session.clear()
        try:
            first = await self.request(
                UnipusApp.SSO,
                "POST",
                endpoints.SSO_PASSWORD_LOGIN,
                family=UnipusApp.SSO.value,
                payload={
                    "service": endpoints.PORTAL_SERVICE,
                    "username": encrypt_login_field(username),
                    "password": encrypt_login_field(password),
                },
            )
        except UnipusRateLimited:
            # 限流不是"凭据被拒"：上抛给调度层退避重试
            raise
        except UnipusApiError as exc:
            # 不打印凭据，只打印平台的业务消息
            self.logger.warning("U校园登录被拒: %s", exc)
            return False

        ticket = parsing.as_text(
            parsing.dig(first, "rs", "serviceTicket"), field_name="rs.serviceTicket"
        )
        if not ticket:
            raise UnipusProtocolError("登录成功响应里没有 rs.serviceTicket")

        # ② 必须复用同一个 HttpClient（cookie 连续性）
        second = await self.request(
            UnipusApp.SSO,
            "GET",
            endpoints.SSO_TICKET_VALIDATE,
            family=UnipusApp.SSO.value,
            query={"ticket": ticket, "service": endpoints.PORTAL_SERVICE},
        )
        jwt = parsing.as_text(parsing.dig(second, "rs", "jwt"), field_name="rs.jwt")
        if not jwt:
            raise UnipusProtocolError("ticket 换 JWT 的响应里没有 rs.jwt")

        # ③ 用门户 JWT 取用户信息，组装会话
        self.session.portal_token = jwt
        try:
            info = await self.fetch_user_info()
        except UnipusError:
            self.session.clear()
            raise
        self.apply_user_info(info)
        self.logger.info("U校园登录成功: %s", self.session.describe())
        return True

    def apply_user_info(self, info: SessionInfo) -> None:
        self.session.open_id = info.open_id
        self.session.display_name = info.display_name
        self.session.school = info.school
        self.session.app_user_id = info.app_user_id

    async def fetch_user_info(self) -> SessionInfo:
        body = await self.request(
            UnipusApp.PORTAL, "GET", endpoints.PORTAL_USER_INFO, family=UnipusApp.PORTAL.value
        )
        return parsing.parse_session(body)

    async def is_logged_in(self) -> bool:
        """用门户用户信息探活。

        目录/内容接口**不校验鉴权**（发错头也返回成功），所以探活必须打受保护
        的门户接口，否则会给出"鉴权没问题"的错觉（规格第 2.3、11.2 节）。
        网络层错误照常上抛（交给调度层重试）。
        """
        if not self.session.has_portal_token:
            return False
        try:
            info = await self.fetch_user_info()
        except UnipusRateLimited:
            # 限流是"稍后再试"，不是"会话失效"：上抛给调度层重试
            raise
        except (UnipusAuthError, UnipusApiError):
            return False
        self.apply_user_info(info)
        return True

    # -- 只读数据 ---------------------------------------------------------

    async def fetch_course_bindings(self) -> tuple[CourseBinding, ...]:
        """班级×教材绑定（含真实进度）。"""
        body = await self.request(
            UnipusApp.PORTAL,
            "GET",
            endpoints.PORTAL_COURSE_LIST,
            family=UnipusApp.PORTAL.value,
        )
        return parsing.parse_course_list(body)

    async def fetch_bookshelf(self) -> tuple[parsing.BookshelfBook, ...]:
        """门户书架（不含进度；用来补激活标记与资源 ID）。"""
        body = await self.request(
            UnipusApp.PORTAL,
            "GET",
            endpoints.PORTAL_BOOKSHELF,
            family=UnipusApp.PORTAL.value,
        )
        return parsing.parse_bookshelf(body)

    async def fetch_catalog(self, course_instance_id: str) -> Catalog:
        """教材目录（单元 → 分组）。路径里的冒号/加号**不做 URL 编码**。"""
        if not course_instance_id:
            raise UnipusProtocolError("拉目录需要 courseId（实例 ID）")
        body = await self.request(
            UnipusApp.UCONTENT,
            "GET",
            endpoints.UCONTENT_CATALOG,
            family=UnipusApp.UCONTENT.value,
            path_params={"course_id": course_instance_id},
        )
        return parsing.parse_catalog(body)

    async def fetch_content(
        self, course_instance_id: str, group_id: str
    ) -> tuple[ContentQuestion, ...]:
        """分组题目内容（加密；**不校验鉴权**，所以不能用来验证 token）。"""
        body = await self.request(
            UnipusApp.UCONTENT,
            "GET",
            endpoints.UCONTENT_CONTENT,
            family=CIPHER_SHELL_FAMILY,
            path_params={"course_id": course_instance_id, "group_id": group_id},
        )
        return parsing.parse_content(body)

    async def fetch_answers(
        self, course_instance_id: str, group_id: str
    ) -> tuple[StandardAnswer, ...]:
        """标准答案（密文字段名是 ``data``，与内容接口不同）。

        这条链**校验鉴权**，是联调时验证 annotator token 的推荐探针
        （规格第 2.3、11.2 节）。
        """
        body = await self.request(
            UnipusApp.UCONTENT,
            "GET",
            endpoints.UCONTENT_ANSWER,
            family=CIPHER_SHELL_FAMILY,
            path_params={"course_id": course_instance_id, "group_id": group_id},
        )
        return parsing.parse_answers(body)

    async def fetch_group_state(self, course_instance_id: str, group_id: str) -> GroupState:
        """单任务状态（真源）。"""
        body = await self.request(
            UnipusApp.UCONTENT,
            "GET",
            endpoints.UCONTENT_GROUP_PROGRESS,
            family=UnipusApp.UCONTENT.value,
            path_params={"course_id": course_instance_id, "group_id": group_id},
        )
        return parsing.parse_group_state(body)

    async def fetch_required_leafs(
        self, course_instance_id: str, group_ids: tuple[str, ...]
    ) -> RequiredSet:
        """分组级必修集。

        ⚠️ ``tasks`` 参数**必须**逗号分隔，且必须显式带上目录里收集到的全部
        分组 ID；``leafs`` 为空表示**信息缺失**（规格第 4.3、6.3 节）。
        ⚠️ 这个口径按 (实例, openId) 记账、**不随班级变化**，同教材多班时会给出
        合并口径（规格第 6.3 节）。
        """
        body = await self.request(
            UnipusApp.UCONTENT,
            "GET",
            endpoints.UCONTENT_TASK_PROGRESS,
            family=UnipusApp.UCONTENT.value,
            path_params={
                "course_id": course_instance_id,
                "open_id": self.session.open_id,
            },
            query={"tasks": endpoints.comma_join(group_ids)},
        )
        return parsing.parse_required_leafs(body)
