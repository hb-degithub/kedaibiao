"""东软平台请求层：统一请求头、外壳解包、**以及自动重登重试**。

## 为什么重登是必需品而不是可选糖

这个平台的 token 会被**服务端提前吊销**，而本地 ``expiresTime`` 还显示有效。
症状是「同一个 client 上，前一个请求能过、后一个 401」。所以这层刻意做成：
凭据交给 :class:`NeusoftClient`，:meth:`NeusoftClient.request` 自己重登重试 ——
调用方没机会忘记包重试。

## 四个约定

1. **所有请求都走 :meth:`request`**，包括登录（登录只是 ``auth=False``）；
2. **不 sleep、不重试网络错误**：``OSError`` 原样上抛，退避交给 ``core.scheduler``；
3. **双层判错**：HTTP 非 2xx 与响应体 ``code != 0`` 都要判，只看一层会漏；
4. **``code == 0`` 不等于业务完成** —— 刷课必须以回读 ``studyStatus`` 为准。
"""

from __future__ import annotations

import json
import logging
import urllib.parse
import uuid as uuid_module
from collections.abc import Callable, Mapping
from typing import Any

from kedaibiao.net.http import ResponseNotJson
from kedaibiao.platforms.neusoft import endpoints as ep
from kedaibiao.platforms.neusoft.errors import (
    NeusoftApiError,
    NeusoftError,
    NeusoftNotLoggedIn,
    NeusoftProtocolError,
)
from kedaibiao.platforms.neusoft.models import Session, as_str
from kedaibiao.platforms.neusoft.signing import build_id_code_with_uuid

log = logging.getLogger(__name__)

#: 缺 session 时的统一异常文案。
_NEED_LOGIN = "尚未登录：没有 accessToken"


class NeusoftClient:
    """请求 + 签名 + 会话。业务方法按模块拆在 ``terms`` / ``resources`` / ``study`` /
    ``homework`` / ``attendance`` 里，都以本类为唯一出口。"""

    def __init__(
        self,
        http: Any,
        *,
        base_url: str = ep.DEFAULT_BASE_URL,
        tenant_id: str = "",
        session: Session | None = None,
        uuid_factory: Callable[[], str] | None = None,
    ) -> None:
        #: 真实实现是 ``net.http.HttpClient``；测试里注入假 Transport 的同一个类。
        self._http = http
        self.base_url = base_url.rstrip("/")
        self.tenant_id = tenant_id
        self.session = session or Session()
        #: 签名里的 uuid 来源。测试注入固定值以取得可复现的 ``Id-Code``。
        self._uuid_factory = uuid_factory or (lambda: str(uuid_module.uuid4()))
        #: 自动重登用的凭据。账号库不存密码，所以这里通常只在本次进程内有效。
        self._username = ""
        self._password = ""
        #: 最近一次 refresh 失败的原因（给上层拼错误文案用）。
        #: 平台对失效的 refresh_token 回的是 401「账号未登录」—— 这跟凭据写错
        #: 是两回事，文案上要能区分开，否则用户会去改密码。
        self.refresh_error = ""

    # -- 会话 -------------------------------------------------------------

    def remember_credentials(self, username: str, password: str) -> None:
        """记下凭据，供 401 自动重登。只在内存里，不落盘、不进日志。"""
        self._username = username
        self._password = password

    def load_session(self, session: Session) -> None:
        """恢复已保存的会话（来自 ``import_session``）。"""
        self.session = session
        if session.tenant_id:
            self.tenant_id = session.tenant_id

    @property
    def user_id(self) -> str:
        return self.session.user_id

    # -- 请求 -------------------------------------------------------------

    def headers(self, path: str, *, auth: bool, method: str = "POST") -> dict[str, str]:
        """组装公共请求头。

        ``Id-Code`` 只覆盖**相对路径**（无域名、无 query），所以这里再规范一次 ——
        调用方传了带 query 的路径也不会把签名带错。
        """
        headers = {
            ep.HEADER_CONTENT_TYPE: ep.CONTENT_TYPE_JSON,
            "Accept": ep.ACCEPT_JSON,
            ep.HEADER_USER_AGENT: ep.APP_USER_AGENT,
            ep.HEADER_ID_CODE: build_id_code_with_uuid(
                self.session.user_id, path, self._uuid_factory()
            ),
        }
        if self.tenant_id:
            headers[ep.HEADER_TENANT] = self.tenant_id
        if auth and self.session.access_token:
            headers[ep.HEADER_AUTHORIZATION] = f"Bearer {self.session.access_token}"
        return headers

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        body: Any | None = None,
        auth: bool = True,
        allow_reauth: bool = True,
        timeout: float | None = None,
    ) -> Any:
        """发一次请求并解包 ``data``。

        401（HTTP 状态或业务码）会自动重登一次再重试；重登也失败才抛
        :class:`NeusoftNotLoggedIn`。
        """
        if auth and not self.session.access_token:
            if not allow_reauth or not self._username:
                raise NeusoftNotLoggedIn(_NEED_LOGIN)
            await self.login(self._username, self._password)
        try:
            return await self._send(
                method, path, params=params, body=body, auth=auth, timeout=timeout
            )
        except NeusoftApiError as exc:
            if not (exc.is_auth_error and auth and allow_reauth):
                raise
            # 优先用 refresh_token 续期（不需要密码，进程重启后也有效）
            if await self.refresh():
                return await self._send(
                    method, path, params=params, body=body, auth=auth,
                    allow_reauth=False, timeout=timeout,
                )
            if not self._username:
                raise NeusoftNotLoggedIn(_NEED_LOGIN) from exc
            log.info("会话被吊销且刷新失败，重新登录后重试一次: %s", path)
            await self.login(self._username, self._password)
            return await self._send(
                method,
                path,
                params=params,
                body=body,
                auth=auth,
                allow_reauth=False,
                timeout=timeout,
            )

    async def _send(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        body: Any | None = None,
        auth: bool,
        allow_reauth: bool = True,
        timeout: float | None = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        query = {k: as_str(v) for k, v in (params or {}).items() if v is not None}
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        headers = self.headers(path, auth=auth, method=method)
        payload_bytes = (
            json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        )
        kwargs: dict[str, Any] = {"headers": headers, "body": payload_bytes}
        if timeout is not None:
            kwargs["timeout"] = timeout

        response = await self._http.request(method, url, **kwargs)
        text = response.text()
        # 401 是「登录态失效」，要走重登；403 是权限问题，重登没用。
        if response.status == 401:
            raise NeusoftApiError(401, "HTTP 401 登录态失效", path)
        if not response.ok:
            raise NeusoftApiError(response.status, text[:200], path)
        try:
            payload = response.json()
        except ResponseNotJson as exc:
            raise NeusoftProtocolError(f"{path} 返回的不是 JSON: {text[:200]!r}") from exc
        return _unwrap(payload, path)

    # -- 鉴权 -------------------------------------------------------------

    async def resolve_tenant_id(self, hint: str = ep.DEFAULT_TENANT_HINT) -> str:
        """从租户列表挑生产租户。

        规则（每条都是踩过的）：

        - 名称含「内网」的**一律跳过** —— 内网租户登录必失败；
        - 优先名称匹配 ``hint``（学校名）的条目；
        - 再退化为「业务域名出现在 website 字段里」；
        - 都失败就回退常量 :data:`endpoints.FALLBACK_TENANT_ID`。
        """
        try:
            data = await self._send(
                "GET", ep.TENANT_OPTION, params=None, body=None, auth=False, allow_reauth=False
            )
        except NeusoftError as exc:
            log.warning("租户列表获取失败，回退默认租户: %s", exc)
            return ep.FALLBACK_TENANT_ID

        tenants = [item for item in (data or []) if isinstance(item, dict)]
        external = [
            item
            for item in tenants
            if ep.INTERNAL_TENANT_MARKER not in as_str(item.get("name"))
        ]
        for item in external:
            if hint and hint in as_str(item.get("name")):
                found = as_str(item.get("tenantId") or item.get("id"))
                if found:
                    return found
        host = self.base_url.split("://", 1)[-1].split("/", 1)[0]
        for item in external:
            if host and host in as_str(item.get("website")):
                found = as_str(item.get("tenantId") or item.get("id"))
                if found:
                    return found
        log.warning("租户列表里没有匹配「%s」的外网租户，回退默认值", hint)
        return ep.FALLBACK_TENANT_ID

    async def login(self, username: str, password: str) -> Session:
        """App 登录（免图形验证码）。

        顺序要紧：**先解析租户，再登录** —— 登录请求自己也要带 ``Tenant-Id``，
        租户不对会直接回「租户不存在」。
        """
        if not username:
            raise NeusoftError("登录用户名不能为空")
        if not self.tenant_id:
            self.tenant_id = await self.resolve_tenant_id()
        payload = await self._send(
            "POST",
            ep.APP_LOGIN,
            params=None,
            body={"username": username, "password": password},
            auth=False,
            allow_reauth=False,
        )
        session = Session.from_payload(payload)
        if not session.access_token:
            raise NeusoftProtocolError("登录返回 code=0 但未取到 accessToken")
        # 登录返回的 tenantId 才是权威值
        if session.tenant_id:
            self.tenant_id = session.tenant_id
        session.tenant_id = self.tenant_id
        self.session = session
        self.remember_credentials(username, password)
        log.info("已登录东软平台: userId=%s", session.user_id)
        return session

    async def refresh(self) -> bool:
        """用 ``refresh_token`` 换新 access_token。

        为什么重要：账号库**故意不存平台密码**，所以进程重启后从磁盘恢复的会话
        没有 ``_username``/``_password``；一旦服务端吊销 token，旧实现只会抛
        「登录失败（凭据可能不对，或会话已失效）」，用户必须回账号页重输密码。

        这条路径不需要密码，正好补上那个洞。``APP_REFRESH`` 目前是 **[C] 级证据**
        （未实测过报文），所以：
        - 失败一律**静默**返回 False，绝不把原本能用的路径弄坏；
        - 不清掉旧 ``refresh_token``，可能只是临时故障；
        - 调用方拿到 False 后照旧走密码重登（有凭据时）或直接报失效。
        """
        token = self.session.refresh_token
        if not token:
            return False
        try:
            payload = await self._send(
                "POST",
                ep.APP_REFRESH,
                params=None,
                body={"refreshToken": token},
                auth=False,
                allow_reauth=False,
            )
        except NeusoftError as exc:
            self.refresh_error = str(exc)
            log.info("刷新令牌失败，交回调用方处理: %s", exc)
            return False
        renewed = Session.from_payload(payload)
        if not renewed.access_token:
            self.refresh_error = "刷新令牌返回里没有 accessToken"
            log.info(self.refresh_error)
            return False
        self.refresh_error = ""
        # 平台可能只回 accessToken，其它字段沿用旧会话
        renewed.user_id = renewed.user_id or self.session.user_id
        renewed.refresh_token = renewed.refresh_token or self.session.refresh_token
        renewed.expires_time = renewed.expires_time or 0
        renewed.tenant_id = renewed.tenant_id or self.tenant_id
        self.session = renewed
        if renewed.tenant_id:
            self.tenant_id = renewed.tenant_id
        log.info("已用 refresh_token 续期东软会话")
        return True

    async def is_logged_in(self) -> bool:
        """会话是否还有效。探测失败一律按「无效」处理，交给调用方重新登录。"""
        if not self.session.access_token:
            return False
        try:
            await self._send("GET", ep.USER_PROFILE, auth=True, allow_reauth=False)
        except NeusoftError:
            return False
        return True


def _unwrap(payload: Any, path: str) -> Any:
    """解 ``{code, msg, data}`` 外壳。

    平台偶尔在 HTTP 200 里套业务错误码，所以必须看 ``code``；
    缺 ``code`` 说明不是本平台的响应（多半是网关或登录页 HTML），也是协议错误。
    """
    if not isinstance(payload, dict):
        raise NeusoftProtocolError(f"{path} 响应不是对象: {type(payload).__name__}")
    if "code" not in payload:
        raise NeusoftProtocolError(f"{path} 响应缺少 code 字段")
    code = payload.get("code")
    if _as_code(code) != 0:
        raise NeusoftApiError(code, as_str(payload.get("msg")), path)
    return payload.get("data")


def _as_code(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1
