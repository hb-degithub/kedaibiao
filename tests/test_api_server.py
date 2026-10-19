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
        self.httpd = make_server(self.service, host="127.0.0.1", port=0)
        self.port = self.httpd.server_address[1]
        # poll_interval 默认 0.5s，会让每个用例的 shutdown 白等半秒
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        self.thread.start()
        self.addCleanup(self._stop)

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
             raw: bytes | None = None, headers: dict[str, str] | None = None):
        url = f"http://127.0.0.1:{self.port}{path}"
        hdrs = dict(headers or {})
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
        with mock.patch.dict(os.environ, {}, clear=True):
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
        self.assertEqual([a["name"] for a in data["actions"]], ["echo"])

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
        with mock.patch.dict(os.environ, {}, clear=True):
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


if __name__ == "__main__":
    unittest.main()
