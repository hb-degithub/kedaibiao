"""东软适配器单测的公共夹具。

三个模块（刷课 / 作业 / 考勤）的测试共用这一份假传输层，避免各写一套
——同时保证「离线可跑」这条硬约定：整个适配器的测试都不联网。

用法::

    transport = FakeTransport()
    transport.json("POST", "/web-api/teachmanager/.../startStudy", {"x": 1})
    client = make_client(transport)
    assert transport.paths() == ["/web-api/..."]
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from kedaibiao.net.http import Response
from kedaibiao.platforms.neusoft.client import NeusoftClient
from kedaibiao.platforms.neusoft.models import Session

#: 测试里统一用的假域名，别用真实域名（避免把测试当成联调）。
TEST_BASE = "https://neusoft.test"


@dataclass(slots=True)
class RecordedRequest:
    method: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    body: Any = None

    @property
    def path(self) -> str:
        """去掉域名与 query 的路径 —— 平台签名与路由都只看这一段。"""
        after = self.url.split("://", 1)[-1]
        slash = after.find("/")
        raw = after[slash:] if slash >= 0 else "/"
        return raw.split("?", 1)[0]

    @property
    def query(self) -> dict[str, str]:
        from urllib.parse import parse_qs, urlparse

        return {k: v[0] for k, v in parse_qs(urlparse(self.url).query).items()}

    def json(self) -> Any:
        return json.loads(self.body) if self.body else None


@dataclass(slots=True)
class _Route:
    method: str
    path_suffix: str
    status: int
    body: bytes


class FakeTransport:
    """按「方法 + 路径后缀」路由的假传输层。

    路径用**后缀**匹配，测试里可以只写 ``/startStudy``，不必把整条前缀抄一遍。
    未命中的请求返回 404，并在请求记录里留痕 —— 测试里能一眼看出打错了端点。
    """

    def __init__(self) -> None:
        self.routes: list[_Route] = []
        self.requests: list[RecordedRequest] = []
        #: 命不中路由时的默认响应；给 ``None`` 表示用 404。
        self.default_status = 404

    # -- 配置 --

    def json(self, method: str, path_suffix: str, payload: Any, *, status: int = 200) -> None:
        """登记一个返回 ``{"code": 0, "msg": "", "data": payload}`` 的路由。"""
        self.raw(
            method,
            path_suffix,
            json.dumps({"code": 0, "msg": "", "data": payload}, ensure_ascii=False),
            status=status,
        )

    def api_error(
        self, method: str, path_suffix: str, code: int, msg: str = "", *, status: int = 200
    ) -> None:
        """登记一个业务错误路由（HTTP 200 里套非 0 的 code）。"""
        self.raw(
            method,
            path_suffix,
            json.dumps({"code": code, "msg": msg, "data": None}, ensure_ascii=False),
            status=status,
        )

    def raw(self, method: str, path_suffix: str, text: str, *, status: int = 200) -> None:
        self.routes.append(_Route(method.upper(), path_suffix, status, text.encode("utf-8")))

    # -- 断言辅助 --

    def paths(self) -> list[str]:
        return [item.path for item in self.requests]

    def body_of(self, path_suffix: str) -> Any:
        """取最后一次打到某端点的请求体（JSON 解码后）。"""
        for item in reversed(self.requests):
            if item.path.endswith(path_suffix):
                return item.json()
        raise AssertionError(f"没有打到 {path_suffix} 的请求，实际: {self.paths()}")

    def headers_of(self, path_suffix: str) -> dict[str, str]:
        for item in reversed(self.requests):
            if item.path.endswith(path_suffix):
                return item.headers
        raise AssertionError(f"没有打到 {path_suffix} 的请求，实际: {self.paths()}")

    # -- 传输层契约 --

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        timeout: float = 15.0,
    ) -> Response:
        raw_body = body.decode("utf-8") if isinstance(body, (bytes, bytearray)) else None
        self.requests.append(
            RecordedRequest(method.upper(), url, dict(headers or {}), raw_body)
        )
        path = self.requests[-1].path
        for route in reversed(self.routes):
            if route.method == method.upper() and path.endswith(route.path_suffix):
                return Response(
                    status=route.status,
                    url=url,
                    body=route.body,
                    headers={"content-type": "application/json"},
                )
        return Response(status=self.default_status, url=url, body=b"{}")


def make_client(
    transport: FakeTransport,
    *,
    user_id: str = "1000000000000000001",
    tenant_id: str = "0000000000000000000",
    access_token: str = "fake-token",
    uuid_factory: Any = None,
) -> NeusoftClient:
    """造一个已登录的客户端（签名 uuid 默认固定，保证断言可复现）。"""
    session = Session(
        user_id=user_id,
        tenant_id=tenant_id,
        access_token=access_token,
        refresh_token="fake-refresh",
        expires_time=0,
    )
    return NeusoftClient(
        transport,
        base_url=TEST_BASE,
        tenant_id=tenant_id,
        session=session,
        uuid_factory=uuid_factory or (lambda: "fixed-uuid"),
    )


def make_anonymous_client(transport: FakeTransport) -> NeusoftClient:
    """未登录的客户端（测登录链路用）。"""
    return NeusoftClient(
        transport, base_url=TEST_BASE, uuid_factory=lambda: "fixed-uuid"
    )
