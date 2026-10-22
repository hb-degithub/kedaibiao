"""控制台 HTTP 外壳测试。

全部在 127.0.0.1 回环上跑，不访问外部网络。重点覆盖两件事：

1. 契约里的每个路由与错误分支；
2. 安全检查 —— Host 校验（防 DNS rebinding）、跨站 Origin 拒绝、
   目录穿越、POST 必须 JSON。
"""

from __future__ import annotations

import http.client
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from unittest import mock

from kedaibiao import adapters
from kedaibiao.api.server import STATIC_DIR, build_allowed_hosts, make_server
from kedaibiao.api.service import ConsoleService
from kedaibiao.core.models import (
    Answer,
    AnswerSource,
    Assignment,
    Course,
    Credentials,
    Question,
    QuestionType,
    SubmissionResult,
)
from kedaibiao.platforms.base import Capabilities, PlatformAdapter

PLATFORM = "httpfake"
NOSUBMIT = "httpnosubmit"


class FakeAdapter(PlatformAdapter):
    name = PLATFORM
    display_name = "HTTP 假平台"
    capabilities = Capabilities()

    async def login(self, credentials: Credentials) -> bool:
        return True

    async def is_logged_in(self) -> bool:
        return True

    async def list_courses(self) -> list[Course]:
        return [Course(remote_id="c1", name="课程")]

    async def list_assignments(self, course: Course) -> list[Assignment]:
        return [Assignment(remote_id="a1", title="作业")]

    async def fetch_questions(self, assignment: Assignment) -> list[Question]:
        return [Question(stem="已知题", options=["甲", "乙"], remote_id="q1",
                         type=QuestionType.SINGLE)]

    async def submit(self, assignment: Assignment, answers: list[Answer]) -> SubmissionResult:
        return SubmissionResult(assignment_id=assignment.remote_id, ok=True, score=80.0)


class NoSubmitAdapter(FakeAdapter):
    name = NOSUBMIT
    capabilities = Capabilities(submit=False)


def _creds(platform: str) -> dict[str, str]:
    up = platform.upper()
    return {f"KEDAIBIAO_{up}_USER": "stu", f"KEDAIBIAO_{up}_PASS": "pw"}


class ServerTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "config").mkdir()
        (self.root / "config" / "local.toml").write_text(
            "[kedaibiao]\n"
            f'db_url = "sqlite:///{self.root}/api.db"\n'
            f'session_dir = "{self.root}/sessions"\n'
            'notify = "null"\n',
            encoding="utf-8",
        )
        for cls in (FakeAdapter, NoSubmitAdapter):
            adapters.register(cls.name, cls, override=True)
            self.addCleanup(adapters.unregister, cls.name)

        self.service = ConsoleService(root=self.root)
        self._start_server()   # 先登录，拿到 user_id
        # 凭据只来自账号库：给假平台绑到当前用户名下
        for cls in (FakeAdapter, NoSubmitAdapter):
            self.bind_account(cls.name)

    def bind_account(self, platform: str, username: str = "stu", owner_id: int | None = None) -> int:
        """给某平台绑定一个账号 —— 现在凭据**只**来自账号库，不再读 .env。

        ``owner_id`` 必须与请求的登录用户一致，否则服务层按归属查不到。
        """
        from kedaibiao.security.accounts import AccountStore

        owner = getattr(self, "user_id", 0) if owner_id is None else owner_id
        store = AccountStore(self.root / ("api.db"))
        try:
            account = store.add(owner, platform, label="测试号", username=username)
            return account.id
        finally:
            store.close()

    def unbind_all(self, platform: str) -> None:
        from kedaibiao.security.accounts import AccountStore

        store = AccountStore(self.root / ("api.db"))
        try:
            for acc in store.list_for(None, platform):
                store.delete(acc.id)
        finally:
            store.close()

    def _start_server(self) -> None:
        self.httpd = make_server(self.service, host="127.0.0.1", port=0)
        self.port = self.httpd.server_address[1]
        # poll_interval 默认 0.5s，会让每个用例的 shutdown 白等半秒
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        self.thread.start()
        self.addCleanup(self._stop)
        self.token = self._bootstrap_login()

    def _bootstrap_login(self, username: str = "admin", password: str = "adminpass123") -> str:
        """建一个超管并登录，返回会话令牌。

        控制台现在有登录门，所有数据路由都要求会话 —— 所以测试基类统一登录一次。
        """
        from kedaibiao.security.auth import AuthStore

        store = AuthStore(self.root / "api.db")
        try:
            existing = store.get_user_by_name(username)
            if existing is None:
                store.create_user(username, password, role="superadmin")
            else:
                # 已存在（例如 bootstrap 建过）就把密码设成已知值
                store.set_password(existing.id, password)
        finally:
            store.close()
        user, session = self.service.login(username, password)
        self.user_id = user["id"]
        return session.token

    def _stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)

    def seed_bank(self) -> None:
        from kedaibiao.bank.store import QuestionBank

        bank = QuestionBank(self.root / "api.db")
        q = Question(stem="已知题", options=["甲", "乙"], remote_id="q1")
        bank.upsert(q, Answer(question=q, choices=[0], source=AnswerSource.BANK_HIT,
                              confidence=1.0))
        bank.close()

    def call(self, path: str, *, method: str = "GET", json_body=None,
             raw: bytes | None = None, headers: dict[str, str] | None = None,
             auth: bool = True):
        url = f"http://127.0.0.1:{self.port}{path}"
        hdrs = dict(headers or {})
        if auth and getattr(self, "token", None):
            hdrs.setdefault("Cookie", f"kdb_session={self.token}")
        data = raw
        if json_body is not None:
            data = json.dumps(json_body).encode()
            hdrs.setdefault("Content-Type", "application/json")
        req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, resp.read(), dict(resp.headers)
        except urllib.error.HTTPError as exc:
            # HTTPError 持有连接，必须关；否则漏 ResourceWarning
            try:
                return exc.code, exc.read(), dict(exc.headers or {})
            finally:
                exc.close()

    def as_json(self, response) -> dict:
        status, body, _ = response
        self.assertTrue(body, f"HTTP {status} 没有响应体")
        return json.loads(body)

    def raw_call(self, path: str, *, host: str, method: str = "GET"):
        """绕过 urllib 的 URL 规范化与 Host 自动填充。"""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            conn.putheader("Host", host)
            if getattr(self, "token", None):
                conn.putheader("Cookie", f"kdb_session={self.token}")
            conn.endheaders()
            resp = conn.getresponse()
            return resp.status, resp.read()
        finally:
            conn.close()


class TestReadRoutes(ServerTestBase):
    def test_overview(self) -> None:
        status, body, headers = self.call("/api/overview")
        self.assertEqual(status, 200)
        self.assertIn("application/json", headers.get("Content-Type", ""))
        data = json.loads(body)
        self.assertIn("dry_run", data)
        self.assertIn("bank", data)
        self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")

    def test_platforms(self) -> None:
        data = self.as_json(self.call("/api/platforms"))
        names = {p["name"] for p in data["platforms"]}
        self.assertIn(PLATFORM, names)
        self.assertIn(NOSUBMIT, names)

    def test_bank(self) -> None:
        data = self.as_json(self.call("/api/bank"))
        self.assertIn("db_path", data)
        self.assertNotIn("bank", data)

    def test_history_routes(self) -> None:
        for path in ("/api/runs", "/api/submissions", "/api/jobs", "/api/events"):
            with self.subTest(path=path):
                data = self.as_json(self.call(path))
                self.assertIsInstance(data, dict)

    def test_limit_must_be_integer(self) -> None:
        status, body, _ = self.call("/api/runs?limit=abc")
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["kind"], "bad_request")

    def test_unknown_job_is_404(self) -> None:
        status, body, _ = self.call("/api/jobs/j-999")
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body)["kind"], "not_found")

    def test_unknown_route_is_404(self) -> None:
        status, body, _ = self.call("/api/nope")
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body)["kind"], "not_found")

    def test_head_has_no_body(self) -> None:
        status, body, _ = self.call("/api/overview", method="HEAD")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")


class TestHardening(ServerTestBase):
    """守卫自身不能被「看起来像白名单」的畸形值绕过。"""

    def test_origin_with_userinfo_is_rejected(self) -> None:
        """``http://127.0.0.1:PORT@evil.example`` 的 netloc 以白名单名字开头。

        不显式拒绝 userinfo 的话，``netloc.rsplit(":")`` 会退化成 ``127.0.0.1``
        从而命中白名单 —— 等于放行了跨站请求。浏览器不会这么发 Origin，
        但守卫必须严格，不能依赖「浏览器不会这么发」。
        """
        status, body, _ = self.call(
            "/api/overview",
            headers={"Origin": f"http://127.0.0.1:{self.port}@evil.example"},
        )
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body)["kind"], "forbidden")

    def test_host_with_userinfo_is_rejected(self) -> None:
        status, _ = self.raw_call("/api/overview", host=f"user@127.0.0.1:{self.port}")
        self.assertEqual(status, 403)

    def test_unsupported_method_returns_json_405(self) -> None:
        """非 GET/HEAD/POST 要给 JSON 405，而不是 http.server 默认的 HTML 501。"""
        for method in ("PUT", "DELETE", "PATCH", "OPTIONS"):
            with self.subTest(method=method):
                status, body, headers = self.call("/api/overview", method=method)
                self.assertEqual(status, 405)
                self.assertIn("application/json", headers.get("Content-Type", ""))
                payload = json.loads(body)
                self.assertEqual(payload["kind"], "method_not_allowed")
                self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")


class TestKeepAliveBody(ServerTestBase):
    """keep-alive 下的请求体处理。

    回归用例：``POST /api/accounts/<id>/activate`` 不解析 body，如果服务端不把
    ``{}`` 读掉，它会留在 socket 里被当成下一个请求行（实测 501
    ``Unsupported method ('{}GET')``）。切换平台账号就走这条路径。
    """

    def test_body_on_ignored_route_does_not_break_next_request(self) -> None:
        account_id = self.bind_account(PLATFORM)
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            body = b"{}"
            headers = {
                "Content-Type": "application/json",
                "Cookie": f"kdb_session={self.token}",
                "Content-Length": str(len(body)),
            }
            conn.request("POST", f"/api/accounts/{account_id}/activate", body=body,
                         headers=headers)
            first = conn.getresponse()
            first.read()
            self.assertEqual(first.status, 200)

            conn.request("GET", "/api/overview",
                         headers={"Cookie": f"kdb_session={self.token}"})
            second = conn.getresponse()
            payload = second.read()
            self.assertEqual(second.status, 200, payload[:200])
            self.assertIn(b"dry_run", payload)
        finally:
            conn.close()

    def test_oversized_body_is_413(self) -> None:
        from kedaibiao.api.server import MAX_BODY

        raw = b"{" + b" " * (MAX_BODY + 10) + b"}"
        status, body, _ = self.call(
            "/api/run", method="POST", raw=raw,
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 413)
        self.assertEqual(json.loads(body)["kind"], "bad_request")


class TestStatic(ServerTestBase):
    def test_index_served_if_present(self) -> None:
        status, body, headers = self.call("/")
        if (STATIC_DIR / "index.html").is_file():
            self.assertEqual(status, 200)
            self.assertIn("text/html", headers.get("Content-Type", ""))
            self.assertIn(b"<", body)
        else:
            # 前端还没落地时不该炸，只是 404
            self.assertEqual(status, 404)

    def test_missing_asset_is_404(self) -> None:
        status, _, _ = self.call("/static/definitely-not-here.js")
        self.assertEqual(status, 404)

    def test_console_is_public(self) -> None:
        """新版控制台就是 /，和登录页一样公开（登录门在页面里，接口才要会话）。"""
        for path in ("/", "/next"):
            with self.subTest(path=path):
                status, body, headers = self.call(path, auth=False)
                self.assertEqual(status, 200)
                self.assertIn("text/html", headers.get("Content-Type", ""))
                self.assertIn(b"/static/next/app.js", body)

    def test_classic_console_served(self) -> None:
        """旧版控制台留在 /classic；/next 只是别名，两版共用同一套接口。"""
        status, body, headers = self.call("/classic", auth=False)
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers.get("Content-Type", ""))
        # v1 的方案页 id 是它独有的标记（v2 的动态页面里没有）
        self.assertIn(b'id="page-overview"', body)

    def test_next_assets_served(self) -> None:
        for path, kind in (
            ("/static/next/app.js", "javascript"),
            ("/static/next/app.css", "text/css"),
        ):
            with self.subTest(path=path):
                status, body, headers = self.call(path, auth=False)
                self.assertEqual(status, 200)
                self.assertIn(kind, headers.get("Content-Type", ""))
                self.assertGreater(len(body), 1000)

    def test_traversal_blocked(self) -> None:
        status, _ = self.raw_call("/static/../../etc/passwd", host=f"127.0.0.1:{self.port}")
        self.assertIn(status, (403, 404))
        self.assertNotEqual(status, 200)


class TestPostRun(ServerTestBase):
    def test_requires_json_content_type(self) -> None:
        status, body, _ = self.call(
            "/api/run", method="POST", raw=b"platform=x",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        self.assertEqual(status, 415)
        self.assertEqual(json.loads(body)["kind"], "bad_request")

    def test_bad_json(self) -> None:
        status, body, _ = self.call(
            "/api/run", method="POST", raw=b"{not json",
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 400)

    def test_body_must_be_object(self) -> None:
        status, _, _ = self.call("/api/run", method="POST", json_body=[1, 2])
        self.assertEqual(status, 400)

    def test_missing_platform(self) -> None:
        status, body, _ = self.call("/api/run", method="POST", json_body={})
        self.assertEqual(status, 400)
        self.assertIn("platform", json.loads(body)["error"])

    def test_unknown_platform(self) -> None:
        status, body, _ = self.call("/api/run", method="POST", json_body={"platform": "ghost"})
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body)["kind"], "unknown_platform")

    def test_no_credentials(self) -> None:
        self.unbind_all(PLATFORM)  # 凭据只来自账号库
        status, body, _ = self.call(
            "/api/run", method="POST", json_body={"platform": PLATFORM}
        )
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["kind"], "no_credentials")

    def test_live_on_platform_without_submit(self) -> None:
        env = _creds(NOSUBMIT)
        with mock.patch.dict(os.environ, env, clear=True):
            status, body, _ = self.call(
                "/api/run", method="POST", json_body={"platform": NOSUBMIT, "live": True}
            )
        self.assertEqual(status, 400)
        self.assertIn("不支持提交", json.loads(body)["error"])

    def test_dry_run_accepted_and_completes(self) -> None:
        self.seed_bank()
        with mock.patch.dict(os.environ, _creds(PLATFORM), clear=True):
            status, body, _ = self.call(
                "/api/run", method="POST", json_body={"platform": PLATFORM}
            )
            self.assertEqual(status, 202)
            job = json.loads(body)
            self.assertFalse(job["live"])
            self.assertEqual(job["status"], "running")
            self.assertTrue(self.service.wait_for_idle(timeout=20))

        final = self.as_json(self.call(f"/api/jobs/{job['job_id']}"))
        self.assertEqual(final["status"], "succeeded")
        self.assertFalse(final["live"], "干跑任务必须标记 live=false")
        self.assertEqual(final["records"][0]["status"], "skipped")
        self.assertIn("dry_run", final["records"][0]["note"])

    def test_bad_kind_rejected(self) -> None:
        with mock.patch.dict(os.environ, _creds(PLATFORM), clear=True):
            status, body, _ = self.call(
                "/api/run", method="POST", json_body={"platform": PLATFORM, "kinds": ["zzz"]}
            )
        self.assertEqual(status, 400)
        self.assertIn("未知的任务类型", json.loads(body)["error"])

    def test_bad_limit_rejected(self) -> None:
        status, _, _ = self.call(
            "/api/run", method="POST", json_body={"platform": PLATFORM, "limit": "many"}
        )
        self.assertEqual(status, 400)

    def test_kinds_must_be_array(self) -> None:
        status, _, _ = self.call(
            "/api/run", method="POST", json_body={"platform": PLATFORM, "kinds": "exam"}
        )
        self.assertEqual(status, 400)


class TestAllowedHosts(unittest.TestCase):
    """Host 白名单的组成规则（纯函数，不绑端口）。"""

    def test_loopback_only(self) -> None:
        allowed = build_allowed_hosts("127.0.0.1", 8765)
        self.assertEqual(allowed, {
            "127.0.0.1", "127.0.0.1:8765",
            "localhost", "localhost:8765",
            "::1", "::1:8765",
        })
        self.assertNotIn("100.112.224.1", allowed)

    def test_specific_ip_includes_hostname_and_loopback(self) -> None:
        allowed = build_allowed_hosts("100.112.224.1", 8765, ["hb-mini.tailf4741f.ts.net"])
        self.assertIn("100.112.224.1", allowed)
        self.assertIn("100.112.224.1:8765", allowed)
        self.assertIn("hb-mini.tailf4741f.ts.net", allowed)
        self.assertIn("hb-mini.tailf4741f.ts.net:8765", allowed)
        self.assertIn("127.0.0.1:8765", allowed, "本机访问也要能用")

    def test_wildcard_only_trusts_explicit_names(self) -> None:
        self.assertEqual(build_allowed_hosts("0.0.0.0", 8765), set())
        allowed = build_allowed_hosts("0.0.0.0", 8765, ["box.local"])
        self.assertEqual(allowed, {"box.local", "box.local:8765"})

    def test_empty_host_treated_as_loopback(self) -> None:
        self.assertIn("127.0.0.1:1", build_allowed_hosts("", 1))


class TestPlatformScopedRoutes(ServerTestBase):
    def test_overview_accepts_platform(self) -> None:
        data = self.as_json(self.call(f"/api/overview?platform={PLATFORM}"))
        self.assertEqual(data["platform"], PLATFORM)
        self.assertIsNone(self.as_json(self.call("/api/overview"))["platform"])

    def test_unknown_platform_is_404(self) -> None:
        status, body, _ = self.call("/api/runs?platform=ghost")
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body)["kind"], "unknown_platform")

    def test_bank_accepts_platform(self) -> None:
        data = self.as_json(self.call(f"/api/bank?platform={PLATFORM}"))
        self.assertEqual(data["platform"], PLATFORM)
        self.assertIn("by_platform", data)

    def test_statistics_route(self) -> None:
        data = self.as_json(self.call("/api/statistics"))
        self.assertIn("bank", data)
        self.assertIn("platforms", data)
        self.assertTrue(data["platforms"])

    def test_settings_route(self) -> None:
        data = self.as_json(self.call("/api/settings"))
        self.assertIn("dotenv_present", data)
        self.assertIn("env_keys_seen", data)
        self.assertIn("platforms", data)

    def test_jobs_accepts_platform(self) -> None:
        data = self.as_json(self.call(f"/api/jobs?platform={PLATFORM}"))
        self.assertEqual(data["platform"], PLATFORM)


class TestActionRoutes(ServerTestBase):
    def setUp(self) -> None:
        super().setUp()
        from kedaibiao.api.actions import ActionParam, ActionSpec, message, register

        async def _echo(adapter: Any, params: dict) -> dict:
            if params.get("who") == "explode":
                raise RuntimeError("平台炸了")
            return message(platform=PLATFORM, action="echo", label="回声",
                           message=f"hi {params.get('who', '')}")

        register(
            ActionSpec(platform=PLATFORM, name="echo", label="回声",
                       params=(ActionParam("who", "对象", required=True),)),
            _echo,
        )

    def test_list_actions(self) -> None:
        data = self.as_json(self.call(f"/api/platforms/{PLATFORM}/actions"))
        self.assertEqual(data["platform"], PLATFORM)
        names = [a["name"] for a in data["actions"]]
        self.assertIn("echo", names)
        self.assertIn("courses", names)

    def test_list_actions_unknown_platform(self) -> None:
        status, body, _ = self.call("/api/platforms/ghost/actions")
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body)["kind"], "unknown_platform")

    def test_action_requires_json_content_type(self) -> None:
        status, body, _ = self.call(
            f"/api/platforms/{PLATFORM}/actions/echo", method="POST", raw=b"who=x",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        self.assertEqual(status, 415)

    def test_unknown_action_is_404(self) -> None:
        status, body, _ = self.call(
            f"/api/platforms/{PLATFORM}/actions/nope", method="POST", json_body={}
        )
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body)["kind"], "unknown_action")

    def test_missing_param(self) -> None:
        with mock.patch.dict(os.environ, _creds(PLATFORM), clear=True):
            status, body, _ = self.call(
                f"/api/platforms/{PLATFORM}/actions/echo", method="POST", json_body={}
            )
        self.assertEqual(status, 400)
        self.assertIn("对象", json.loads(body)["error"])

    def test_no_credentials(self) -> None:
        self.unbind_all(PLATFORM)
        status, body, _ = self.call(
            f"/api/platforms/{PLATFORM}/actions/echo",
            method="POST", json_body={"who": "x"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["kind"], "no_credentials")

    def test_success(self) -> None:
        with mock.patch.dict(os.environ, _creds(PLATFORM), clear=True):
            status, body, _ = self.call(
                f"/api/platforms/{PLATFORM}/actions/echo",
                method="POST", json_body={"who": "world"},
            )
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(data["kind"], "message")
        self.assertEqual(data["message"], "hi world")

    def test_platform_error_is_502(self) -> None:
        with mock.patch.dict(os.environ, _creds(PLATFORM), clear=True):
            status, body, _ = self.call(
                f"/api/platforms/{PLATFORM}/actions/echo",
                method="POST", json_body={"who": "explode"},
            )
        self.assertEqual(status, 502)
        data = json.loads(body)
        self.assertEqual(data["kind"], "platform_error")
        self.assertIn("平台炸了", data["error"])


class TestSecurity(ServerTestBase):
    def test_bad_host_rejected(self) -> None:
        status, body = self.raw_call("/api/overview", host="evil.example")
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body)["kind"], "forbidden")

    def test_loopback_host_accepted(self) -> None:
        status, _ = self.raw_call("/api/overview", host=f"127.0.0.1:{self.port}")
        self.assertEqual(status, 200)
        status, _ = self.raw_call("/api/overview", host=f"localhost:{self.port}")
        self.assertEqual(status, 200)

    def test_cross_origin_rejected(self) -> None:
        status, body, _ = self.call(
            "/api/overview", headers={"Origin": "http://evil.example"}
        )
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body)["kind"], "forbidden")

    def test_same_origin_accepted(self) -> None:
        status, _, _ = self.call(
            "/api/overview", headers={"Origin": f"http://127.0.0.1:{self.port}"}
        )
        self.assertEqual(status, 200)

    def test_null_origin_rejected(self) -> None:
        status, _, _ = self.call("/api/overview", headers={"Origin": "null"})
        self.assertEqual(status, 403)

    def test_no_cors_headers_ever(self) -> None:
        """不返回 CORS 头 —— 跨站预检必然失败，这是 CSRF 防线的一半。"""
        _, _, headers = self.call("/api/overview")
        for key in headers:
            self.assertNotIn("Access-Control", key)


class TestSubmitRoute(ServerTestBase):
    """`/api/assignments/submit` 只锁「HTTP 路由 → 服务层」的接线。

    提交链路的业务（覆盖答案、置信度门槛、平台写接口）在
    tests/test_api_service.py 的 TestSubmitPipeline 里用假适配器覆盖。
    """

    def test_route_passes_human_answers_to_service(self) -> None:
        answers = [{"index": 1, "choices": [1]}, {"index": 2, "text": "人工答案"}]
        with mock.patch.object(
            self.service, "submit_assignment", return_value={"submitted": True}
        ) as handler:
            status, body, _ = self.call(
                "/api/assignments/submit",
                method="POST",
                json_body={
                    "platform": PLATFORM,
                    "assignment_id": "a1",
                    "confirm": True,
                    "answers": answers,
                },
            )
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["submitted"])
        kwargs = handler.call_args.kwargs
        self.assertTrue(kwargs["confirm"])
        self.assertEqual(kwargs["overrides"], answers)

    def test_route_without_answers_passes_none(self) -> None:
        with mock.patch.object(
            self.service, "submit_assignment", return_value={"dry_run": True}
        ) as handler:
            status, _, _ = self.call(
                "/api/assignments/submit",
                method="POST",
                json_body={"platform": PLATFORM, "assignment_id": "a1"},
            )
        self.assertEqual(status, 200)
        self.assertIsNone(handler.call_args.kwargs["overrides"])


class TestPincoReviewRoutes(ServerTestBase):
    """品课两条路由：只锁「HTTP 路由 → 服务层」的接线。

    业务（digest 比对、守卫、未确认闸门）在 tests/test_api_service.py 的
    TestPincoReviewChain 里用假适配器覆盖，这里不重复。
    """

    def test_plan_route_passes_course_and_limit(self) -> None:
        payload = {"kind": "table", "platform": "pinco", "digest": "d" * 32, "rows": []}
        with mock.patch.object(
            self.service, "pinco_plan", return_value=payload
        ) as handler:
            status, body, _ = self.call(
                "/api/platforms/pinco/plan",
                method="POST",
                json_body={"course": "数据结构", "limit": "3"},
            )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["digest"], "d" * 32)
        self.assertEqual(handler.call_args.kwargs["course"], "数据结构")
        self.assertEqual(handler.call_args.kwargs["limit"], 3)

    def test_plan_route_defaults_and_bad_limit(self) -> None:
        with mock.patch.object(
            self.service, "pinco_plan", return_value={"kind": "table", "rows": []}
        ) as handler:
            status, _, _ = self.call(
                "/api/platforms/pinco/plan", method="POST", json_body={}
            )
        self.assertEqual(status, 200)
        self.assertEqual(handler.call_args.kwargs["course"], "")
        self.assertEqual(handler.call_args.kwargs["limit"], 0)

        status, body, _ = self.call(
            "/api/platforms/pinco/plan", method="POST", json_body={"limit": "many"}
        )
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["kind"], "bad_request")

    def test_submit_reviewed_route_passes_digest(self) -> None:
        payload = {"platform": "pinco", "assignment_id": "a1", "submitted": True,
                   "ok": True, "score": 95.0, "digest": "x" * 32}
        with mock.patch.object(
            self.service, "pinco_submit_reviewed", return_value=payload
        ) as handler:
            status, body, _ = self.call(
                "/api/platforms/pinco/submit-reviewed",
                method="POST",
                json_body={"assignment_id": "a1", "digest": "x" * 32, "limit": 2},
            )
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["submitted"])
        args = handler.call_args.args
        self.assertEqual(args[1], "a1", "第二个位置参数是任务号")
        self.assertEqual(args[2], "x" * 32, "第三个位置参数是计划 digest")
        self.assertEqual(handler.call_args.kwargs["limit"], 2)

    def test_submit_reviewed_accepts_plan_digest_alias(self) -> None:
        with mock.patch.object(
            self.service, "pinco_submit_reviewed", return_value={"ok": True}
        ) as handler:
            self.call(
                "/api/platforms/pinco/submit-reviewed",
                method="POST",
                json_body={"assignment_id": "a1", "plan_digest": "p" * 32},
            )
        self.assertEqual(handler.call_args.args[2], "p" * 32)

    def test_unconfirmed_payload_maps_to_501(self) -> None:
        from kedaibiao.api.service import ConsoleError

        with mock.patch.object(
            self.service,
            "pinco_submit_reviewed",
            side_effect=ConsoleError("提交未执行：报文未确认", kind="not_supported", status=501),
        ):
            status, body, _ = self.call(
                "/api/platforms/pinco/submit-reviewed",
                method="POST",
                json_body={"assignment_id": "a1", "digest": "x" * 32},
            )
        self.assertEqual(status, 501)
        data = json.loads(body)
        self.assertEqual(data["kind"], "not_supported")
        self.assertIn("未确认", data["error"])

    def test_body_must_be_json_object(self) -> None:
        for path in ("/api/platforms/pinco/plan", "/api/platforms/pinco/submit-reviewed"):
            with self.subTest(path=path):
                status, body, _ = self.call(path, method="POST", json_body=["nope"])
                self.assertEqual(status, 400)
                self.assertEqual(json.loads(body)["kind"], "bad_request")


class TestPincoReviewChainOverHttp(ServerTestBase):
    """端到端：真路由 → 真服务层 → 假品课适配器（不碰网络）。

    与 ``TestPincoReviewRoutes`` 互补：那条锁字段名，这条锁「参数位置与服务层
    签名真的对得上」—— 路由把 viewer 放第一个位置参数，压 mock 是发现不了
    位置错位的。
    """

    def setUp(self) -> None:
        super().setUp()
        from kedaibiao.api import service as service_mod
        from tests.test_api_service import FakePincoAdapter, _FakePincoRuntime

        adapters.register(FakePincoAdapter.name, FakePincoAdapter, override=True)
        self.addCleanup(adapters.unregister, FakePincoAdapter.name)
        self.bind_account("pinco")
        FakePincoAdapter.plan_calls = 0
        FakePincoAdapter.drift = False
        FakePincoAdapter.needs_human_item = False
        FakePincoAdapter.submit_payload_confirmed = False
        self.runtime = _FakePincoRuntime()
        patcher = mock.patch.object(
            service_mod, "build_runtime", return_value=self.runtime
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_plan_then_submit_stops_at_unconfirmed_gate(self) -> None:
        status, body, _ = self.call(
            "/api/platforms/pinco/plan", method="POST", json_body={}
        )
        self.assertEqual(status, 200)
        plan = json.loads(body)
        self.assertEqual(plan["kind"], "table")
        self.assertEqual(len(plan["digest"]), 32)
        self.assertEqual(plan["rows"][0]["assignment_id"], "a1")

        status, body, _ = self.call(
            "/api/platforms/pinco/submit-reviewed",
            method="POST",
            json_body={"assignment_id": "a1", "digest": plan["digest"]},
        )
        self.assertEqual(status, 501)
        data = json.loads(body)
        self.assertEqual(data["kind"], "not_supported")
        self.assertIn("未确认", data["error"])
        self.assertEqual(self.runtime.adapters[-1].submitted, [])

    def test_stale_digest_is_400(self) -> None:
        status, body, _ = self.call(
            "/api/platforms/pinco/submit-reviewed",
            method="POST",
            json_body={"assignment_id": "a1", "digest": "0" * 32},
        )
        self.assertEqual(status, 400)
        self.assertIn("计划已变化", json.loads(body)["error"])


if __name__ == "__main__":
    unittest.main()
