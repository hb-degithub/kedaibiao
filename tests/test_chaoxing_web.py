"""只读看板 Web 服务测试。

起一个真的 ``ThreadingHTTPServer``（绑 127.0.0.1 随机端口），但把取数层换成
固定 HTML —— 验证**路由、错误分支、渲染入口**是通的，不碰真实平台。
"""

from __future__ import annotations

import importlib.util
import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

from kedaibiao.platforms.chaoxing import client, endpoints

ROOT = Path(__file__).resolve().parents[1]

COURSES_HTML = (
    '<div><a href="/mycourse/stu?courseid=111&clazzid=222&enc=e&cpi=333">'
    "<span>线性代数</span></a></div>"
)
WORKS_HTML = (
    "<ul><li><div><a href=\"https://x/work/doHomeWorkNew?workId=9\">第一章作业</a></div>"
    "<p>2026-12-01 23:59</p><p>未交</p></li></ul>"
)


def _load_web_module():
    spec = importlib.util.spec_from_file_location(
        "chaoxing_web", ROOT / "scripts" / "chaoxing_web.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["chaoxing_web"] = module
    spec.loader.exec_module(module)
    return module


class TestWebServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = _load_web_module()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), cls.mod.Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self) -> None:
        self.mod.Handler.default_cookie = ""
        self.mod.Handler.dump_dir = None

    def _get(self, path: str) -> tuple[int, str]:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5) as r:
                return r.status, r.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8")

    def _post(self, path: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def _patch_fetch(self):
        urls_seen: list[str] = []

        def fake_fetch(url: str, cookie: str, timeout: float):
            urls_seen.append(url)
            if url in endpoints.COURSE_LIST_URLS:
                return 200, COURSES_HTML
            return 200, WORKS_HTML

        original = client.fetch
        client.fetch = fake_fetch
        self.addCleanup(lambda: setattr(client, "fetch", original))
        return urls_seen

    def test_index_serves_page(self) -> None:
        status, body = self._get("/")
        self.assertEqual(status, 200)
        self.assertIn("只读看板", body)
        self.assertIn("/api/data", body)

    def test_health(self) -> None:
        status, body = self._get("/api/health")
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(body)["default_cookie"])

    def test_unknown_path_404(self) -> None:
        status, _ = self._get("/nope")
        self.assertEqual(status, 404)

    def test_missing_cookie_is_rejected(self) -> None:
        status, payload = self._post("/api/data", {})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "no_cookie")

    def test_data_with_cookie(self) -> None:
        url_of_courses = endpoints.COURSE_LIST_URLS[0]
        self._patch_fetch()
        status, payload = self._post("/api/data", {"cookie": "fid=1"})

        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["courses"][0]["name"], "线性代数")
        self.assertEqual(payload["works"][0]["items"][0]["title"], "第一章作业")
        self.assertEqual(payload["works"][0]["items"][0]["kind"], "homework")
        self.assertIsNotNone(url_of_courses)

    def test_default_cookie_is_used_as_fallback(self) -> None:
        self.mod.Handler.default_cookie = "fid=server"
        self._patch_fetch()

        status, payload = self._post("/api/data", {})

        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])

    def test_bad_json_body(self) -> None:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/data",
            data=b"not json",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=5):
                self.fail("应当返回 400")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)
            self.assertIn("JSON", json.loads(exc.read())["error"])

    def test_failure_payload_is_reported_not_raised(self) -> None:
        def boom(url: str, cookie: str, timeout: float):
            raise RuntimeError("socket exploded")

        original = client.fetch
        client.fetch = boom
        self.addCleanup(lambda: setattr(client, "fetch", original))

        status, payload = self._post("/api/data", {"cookie": "fid=1"})
        self.assertEqual(status, 500)
        self.assertIn("socket exploded", payload["error"])


if __name__ == "__main__":
    unittest.main()
