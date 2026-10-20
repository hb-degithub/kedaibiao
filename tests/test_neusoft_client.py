"""请求层单测：公共头、外壳解包、401 自动重登、登录链路。

全部离线（注入假传输层）。这里钉住的四条都是「错了会静默失败」的东西：

1. ``Id-Code`` 必须每个请求都带，且只覆盖相对路径；
2. HTTP 200 里可以套业务错误码，只看状态码会漏；
3. token 会被服务端提前吊销 —— 401 必须能自动重登重试；
4. 登录返回 ``code=0`` 但没 ``accessToken`` 要判失败，不能当成功。
"""

from __future__ import annotations

import json
import unittest

from kedaibiao.net.http import Response
from kedaibiao.platforms.neusoft import endpoints as ep
from kedaibiao.platforms.neusoft.client import NeusoftClient
from kedaibiao.platforms.neusoft.errors import (
    NeusoftApiError,
    NeusoftError,
    NeusoftNotLoggedIn,
    NeusoftProtocolError,
)
from kedaibiao.platforms.neusoft.models import Session
from tests.neusoft_support import (
    FakeTransport,
    RecordedRequest,
    make_anonymous_client,
    make_client,
)


class _FlakyTransport(FakeTransport):
    """第一次打某路径返回 401，之后正常 —— 用来测自动重登。"""

    def __init__(self) -> None:
        super().__init__()
        self.flaky_paths: dict[str, int] = {}

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        timeout: float = 15.0,
    ) -> Response:
        raw = url.split("://", 1)[-1]
        path = "/" + raw[raw.find("/") + 1 :] if raw.find("/") >= 0 else "/"
        path = path.split("?", 1)[0]
        remaining = self.flaky_paths.get(path, 0)
        if remaining > 0:
            self.flaky_paths[path] = remaining - 1
            self.requests.append(
                RecordedRequest(method.upper(), url, dict(headers or {}), None)
            )
            return Response(status=401, url=url, body=b'{"code":401,"msg":"unauthorized"}')
        return await super().request(
            method, url, headers=headers, body=body, timeout=timeout
        )


class HeaderTest(unittest.IsolatedAsyncioTestCase):
    """公共请求头。"""

    async def asyncSetUp(self) -> None:
        self.transport = FakeTransport()
        self.client = make_client(self.transport)

    async def test_sends_id_code_tenant_and_bearer(self) -> None:
        self.transport.json("POST", "/listResource", {"resList": []})
        await self.client.request("POST", ep.RESOURCE_LIST, body={"a": 1})

        headers = self.transport.headers_of("/listResource")
        self.assertTrue(headers.get(ep.HEADER_ID_CODE), "必须带 Id-Code")
        self.assertEqual(headers.get(ep.HEADER_TENANT), "0000000000000000000")
        self.assertEqual(headers.get(ep.HEADER_AUTHORIZATION), "Bearer fake-token")
        # 登录接口对 UA 敏感：必须是 App/uni-app 形态，不能是桌面浏览器
        self.assertIn("uni-app", headers.get("User-Agent", ""))

    async def test_id_code_ignores_query_string(self) -> None:
        """签名只覆盖相对路径：带 query 与不带 query 的 Id-Code 必须一致。"""
        self.transport.json("GET", "/paper", {"ok": True})
        await self.client.request("GET", ep.EXAM_PAPER, params={"testId": "123"})
        await self.client.request("GET", ep.EXAM_PAPER)
        first, second = [
            item.headers.get(ep.HEADER_ID_CODE) for item in self.transport.requests
        ]
        self.assertEqual(first, second)

    async def test_json_body_is_serialized(self) -> None:
        self.transport.json("POST", "/startStudy", {"ok": True})
        await self.client.request("POST", ep.START_STUDY, body={"teachCourseId": "1"})
        self.assertEqual(self.transport.body_of("/startStudy"), {"teachCourseId": "1"})

    async def test_query_params_are_sent(self) -> None:
        self.transport.json("GET", "/enter", {"historyId": "h1"})
        await self.client.request("GET", ep.EXAM_ENTER, params={"testId": 42})
        self.assertEqual(self.transport.requests[-1].query, {"testId": "42"})


class EnvelopeTest(unittest.IsolatedAsyncioTestCase):
    """响应外壳 ``{code, msg, data}``。"""

    async def test_returns_data_field(self) -> None:
        transport = FakeTransport()
        transport.json("GET", "/profile/get", {"userId": "u1"})
        client = make_client(transport)
        self.assertEqual(
            await client.request("GET", ep.USER_PROFILE), {"userId": "u1"}
        )

    async def test_business_error_in_http_200_is_raised(self) -> None:
        transport = FakeTransport()
        transport.api_error("POST", "/study-task/page", 403, "您权限不足或者非法访问")
        client = make_client(transport)
        with self.assertRaises(NeusoftApiError) as caught:
            await client.request("POST", ep.STUDY_TASK_PAGE, body={"termId": "1"})
        self.assertEqual(caught.exception.code, 403)
        self.assertIn("权限不足", caught.exception.message)

    async def test_http_error_status_is_raised(self) -> None:
        transport = FakeTransport()
        transport.raw("GET", "/missing", "gateway down", status=502)
        client = make_client(transport)
        with self.assertRaises(NeusoftApiError) as caught:
            await client.request("GET", "/missing")
        self.assertEqual(caught.exception.code, 502)

    async def test_missing_code_is_protocol_error(self) -> None:
        """拿到登录页 HTML / 网关错误页时，必须报「协议不符」而不是静默当成功。"""
        transport = FakeTransport()
        transport.raw("GET", "/weird", '{"data": {}}')
        client = make_client(transport)
        with self.assertRaises(NeusoftProtocolError):
            await client.request("GET", "/weird")

    async def test_non_json_is_protocol_error(self) -> None:
        transport = FakeTransport()
        transport.raw("GET", "/html", "<html>login</html>")
        client = make_client(transport)
        with self.assertRaises(NeusoftProtocolError):
            await client.request("GET", "/html")


class ReauthTest(unittest.IsolatedAsyncioTestCase):
    """401 自动重登重试 —— 这个平台的 token 会被服务端提前吊销。"""

    async def test_401_triggers_one_relogin_and_retry(self) -> None:
        transport = _FlakyTransport()
        transport.json("POST", "/auth/app/login", {"accessToken": "new-token", "userId": "u9"})
        transport.json("POST", "/startStudy", {"ok": True})
        transport.flaky_paths[ep.START_STUDY] = 1
        client = make_client(transport)
        client.remember_credentials("alice", "secret")

        result = await client.request("POST", ep.START_STUDY, body={"x": 1})

        self.assertEqual(result, {"ok": True})
        paths = transport.paths()
        self.assertEqual(paths.count(ep.APP_LOGIN), 1, "应当只重登一次")
        self.assertEqual(transport.requests[-1].path, ep.START_STUDY)
        self.assertEqual(client.session.access_token, "new-token")

    async def test_no_credentials_means_no_relogin(self) -> None:
        transport = _FlakyTransport()
        transport.json("POST", "/startStudy", {"ok": True})
        transport.flaky_paths[ep.START_STUDY] = 1
        client = make_client(transport)  # 没有 remember_credentials

        with self.assertRaises(NeusoftApiError):
            await client.request("POST", ep.START_STUDY, body={"x": 1})
        self.assertNotIn(ep.APP_LOGIN, transport.paths())

    async def test_missing_token_without_credentials_raises(self) -> None:
        transport = FakeTransport()
        client = NeusoftClient(transport, session=Session())
        with self.assertRaises(NeusoftNotLoggedIn):
            await client.request("POST", ep.START_STUDY, body={})


class LoginTest(unittest.IsolatedAsyncioTestCase):
    """登录链路：先解析租户，再登录。"""

    async def test_resolves_external_tenant_and_uses_login_response(self) -> None:
        transport = FakeTransport()
        transport.json(
            "GET",
            "/tenant/app/option",
            [
                {"tenantId": "inner-1", "name": "东软（内网）"},
                {"tenantId": "outer-1", "name": "东软（外网）"},
            ],
        )
        transport.json(
            "POST",
            "/auth/app/login",
            {
                "userId": "u1",
                "tenantId": "tenant-from-login",
                "accessToken": "tok",
                "refreshToken": "ref",
                "expiresTime": 1,
            },
        )
        client = make_anonymous_client(transport)

        session = await client.login("alice", "secret")

        self.assertEqual(session.access_token, "tok")
        # 登录返回的 tenantId 才是权威值
        self.assertEqual(client.tenant_id, "tenant-from-login")
        login_request = [
            item for item in transport.requests if item.path.endswith("/auth/app/login")
        ][0]
        self.assertEqual(login_request.json(), {"username": "alice", "password": "secret"})
        # 登录请求就带上了租户（先解析再登录）
        self.assertEqual(login_request.headers.get(ep.HEADER_TENANT), "outer-1")
        # 登录请求本身也要带 Id-Code（用匿名 userId 签）
        self.assertTrue(login_request.headers.get(ep.HEADER_ID_CODE))
        self.assertNotIn(ep.HEADER_AUTHORIZATION, login_request.headers)

    async def test_internal_tenant_is_never_chosen(self) -> None:
        transport = FakeTransport()
        transport.json(
            "GET", "/tenant/app/option", [{"tenantId": "inner-1", "name": "东软（内网）"}]
        )
        client = make_anonymous_client(transport)
        self.assertEqual(await client.resolve_tenant_id(), ep.FALLBACK_TENANT_ID)

    async def test_tenant_website_fallback(self) -> None:
        transport = FakeTransport()
        transport.json(
            "GET",
            "/tenant/app/option",
            [{"tenantId": "by-host", "name": "某校", "website": "https://neusoft.test/x"}],
        )
        client = make_anonymous_client(transport)
        self.assertEqual(await client.resolve_tenant_id(), "by-host")

    async def test_missing_access_token_is_failure(self) -> None:
        transport = FakeTransport()
        transport.json("GET", "/tenant/app/option", [])
        transport.json("POST", "/auth/app/login", {"userId": "u1"})
        client = make_anonymous_client(transport)
        with self.assertRaises(NeusoftProtocolError):
            await client.login("alice", "secret")

    async def test_empty_username_rejected_before_network(self) -> None:
        transport = FakeTransport()
        client = make_anonymous_client(transport)
        with self.assertRaises(NeusoftError):
            await client.login("", "secret")
        self.assertEqual(transport.requests, [])


class SessionTest(unittest.IsolatedAsyncioTestCase):
    def test_session_round_trip(self) -> None:
        session = Session(
            user_id="u1",
            tenant_id="t1",
            access_token="a",
            refresh_token="r",
            expires_time=123,
        )
        restored = Session.from_dict(json.loads(json.dumps(session.to_dict())))
        self.assertEqual(restored.to_dict(), session.to_dict())

    async def test_is_logged_in_probes_profile(self) -> None:
        transport = FakeTransport()
        transport.json("GET", "/profile/get", {"userId": "u1"})
        client = make_client(transport)
        self.assertTrue(await client.is_logged_in())

    async def test_is_logged_in_false_on_error(self) -> None:
        transport = FakeTransport()
        transport.api_error("GET", "/profile/get", 401, "unauthorized")
        client = make_client(transport)
        self.assertFalse(await client.is_logged_in())


if __name__ == "__main__":
    unittest.main()
