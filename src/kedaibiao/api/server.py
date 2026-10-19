"""Web 控制台的 HTTP 外壳 —— 标准库 ``http.server``。

**为什么不是 FastAPI**：本机 ``python3`` 没有 pip，``python3 -m venv`` 缺
``ensurepip`` 装不起来，FastAPI / uvicorn 都装不上。控制台是本地单人用，
标准库足够。

关键是**逻辑不在这里** —— 全部在 ``service.py``。这个文件只做三件事：
路由、JSON 编解码、安全检查。将来换 FastAPI，本文件整个删掉即可。

安全约定（详见 docs/web-console-api.md 第 5 节）：

- 默认只绑定 ``127.0.0.1``；
- 校验 ``Host`` 头，挡 DNS rebinding；
- POST 要求 ``Content-Type: application/json`` 且拒绝跨站 ``Origin`` ——
  浏览器对跨站 JSON 请求会先发预检，而本服务不返回任何 CORS 头，预检必然失败。
"""

from __future__ import annotations

import json
import logging
import socket
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from kedaibiao.api.service import ConsoleError, ConsoleService

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".png": "image/png",
}
MAX_BODY = 64 * 1024


class ConsoleHandler(BaseHTTPRequestHandler):
    server_version = "kedaibiao-console"
    protocol_version = "HTTP/1.1"  # 始终带 Content-Length，可保持长连接

    #: 由 ``make_server`` 注入
    service: ConsoleService
    allowed_hosts: set[str] = set()

    # -- 基础设施 ---------------------------------------------------------

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        log.debug("%s - %s", self.address_string(), fmt % args)

    def _send(self, status: int, body: bytes, content_type: str, **extra: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in extra.items():
            self.send_header(key.replace("_", "-"), value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _error(self, status: int, message: str, kind: str = "bad_request") -> None:
        self._json(status, {"error": message, "kind": kind})

    def _host_ok(self) -> bool:
        """挡 DNS rebinding：Host 头必须是本机可达的名字之一。"""
        if not self.allowed_hosts:
            return True
        host = (self.headers.get("Host") or "").strip().lower()
        if not host:
            return False
        bare = host.rsplit(":", 1)[0].strip("[]")
        return host in self.allowed_hosts or bare in self.allowed_hosts

    def _origin_ok(self) -> bool:
        """拒绝跨站 Origin。同源或没有 Origin（命令行工具）都放行。"""
        origin = self.headers.get("Origin")
        if not origin:
            return True
        if origin == "null":
            return False
        try:
            parsed = urlparse(origin)
        except ValueError:
            return False
        if not self.allowed_hosts:
            return True
        host = (parsed.netloc or "").lower()
        bare = host.rsplit(":", 1)[0].strip("[]")
        return host in self.allowed_hosts or bare in self.allowed_hosts

    def _guard(self) -> bool:
        if not self._host_ok():
            self._error(HTTPStatus.FORBIDDEN, "Host 头不被允许", "forbidden")
            return False
        if not self._origin_ok():
            self._error(HTTPStatus.FORBIDDEN, "跨站请求被拒绝", "forbidden")
            return False
        return True

    def _query(self) -> dict[str, list[str]]:
        return parse_qs(urlparse(self.path).query)

    def _body_json(self) -> Any:
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            raise ConsoleError(
                "POST 必须使用 Content-Type: application/json", kind="bad_request", status=415
            )
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_BODY:
            raise ConsoleError("请求体过大", kind="bad_request", status=413)
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ConsoleError(f"请求体不是合法 JSON: {exc}", kind="bad_request", status=400) from exc

    # -- 路由 -------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        self._dispatch("GET")

    def do_HEAD(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        if not self._guard():
            return
        path = urlparse(self.path).path.rstrip("/") or "/"
        try:
            if method == "GET":
                self._route_get(path)
            else:
                self._route_post(path)
        except ConsoleError as exc:
            self._error(exc.status, str(exc), exc.kind)
        except Exception as exc:  # noqa: BLE001 - 兜底，别让服务线程静默死掉
            log.exception("处理 %s %s 失败", method, path)
            self._error(500, f"{type(exc).__name__}: {exc}", "internal")

    def _route_get(self, path: str) -> None:
        svc = self.service
        if path in ("/", "/index.html"):
            return self._static("index.html")
        if path.startswith("/static/"):
            return self._static(path[len("/static/") :])
        if path == "/api/overview":
            return self._json(200, svc.overview())
        if path == "/api/platforms":
            return self._json(200, svc.platforms())
        if path == "/api/runs":
            return self._json(200, svc.runs(self._int_param("limit", 50)))
        if path == "/api/submissions":
            return self._json(200, svc.submissions(self._int_param("limit", 50)))
        if path == "/api/bank":
            return self._json(200, svc.bank())
        if path == "/api/events":
            return self._json(200, svc.events(self._int_param("limit", 100)))
        if path == "/api/jobs":
            return self._json(200, svc.jobs())
        if path.startswith("/api/jobs/"):
            return self._json(200, svc.job(path[len("/api/jobs/") :]))
        self._error(404, f"没有这个路由: {path}", "not_found")

    def _route_post(self, path: str) -> None:
        if path != "/api/run":
            return self._error(404, f"没有这个路由: {path}", "not_found")

        payload = self._body_json()
        if not isinstance(payload, dict):
            raise ConsoleError("请求体必须是 JSON 对象", kind="bad_request", status=400)

        platform = payload.get("platform")
        if not isinstance(platform, str) or not platform.strip():
            raise ConsoleError("缺少 platform 字段", kind="bad_request", status=400)

        kinds = payload.get("kinds") or []
        if not isinstance(kinds, list):
            raise ConsoleError("kinds 必须是数组", kind="bad_request", status=400)

        try:
            limit = int(payload.get("limit") or 0)
        except (TypeError, ValueError) as exc:
            raise ConsoleError("limit 必须是整数", kind="bad_request", status=400) from exc

        job = self.service.start_run(
            platform.strip(),
            live=bool(payload.get("live", False)),
            limit=limit,
            kinds=[str(k) for k in kinds],
        )
        self._json(HTTPStatus.ACCEPTED, job)

    def _int_param(self, name: str, default: int) -> int:
        raw = self._query().get(name, [None])[0]
        if raw is None:
            return default
        try:
            return int(raw)
        except ValueError:
            raise ConsoleError(f"{name} 必须是整数", kind="bad_request", status=400) from None

    def _static(self, relative: str) -> None:
        base = STATIC_DIR.resolve()
        target = (base / relative).resolve()
        # 防目录穿越：解析后必须仍在 static/ 之内
        if base not in target.parents and target != base:
            return self._error(403, "越界访问", "forbidden")
        if not target.is_file():
            return self._error(404, f"静态资源不存在: {relative}", "not_found")
        ctype = STATIC_TYPES.get(target.suffix.lower(), "application/octet-stream")
        self._send(200, target.read_bytes(), ctype)


LOOPBACK_NAMES = ("127.0.0.1", "localhost", "::1")


def build_allowed_hosts(
    bind_host: str, port: int, allow_hosts: list[str] | None = None
) -> set[str]:
    """算出可接受的 ``Host`` 头集合。空集表示不做校验。

    - 绑回环 → 只认回环名字；
    - 绑具体非回环地址 → 认该地址 + 额外名字 + 回环名字。这样用主机名访问也能过，
      同时仍然挡得住 DNS rebinding；
    - 绑 ``0.0.0.0`` / ``::``（通配）→ 无法枚举合法名字，只认显式给的额外名字。
    """
    bind_host = bind_host or "127.0.0.1"
    extra = list(allow_hosts or [])

    def expand(name: str) -> set[str]:
        return {name, f"{name}:{port}"}

    if bind_host in LOOPBACK_NAMES:
        allowed: set[str] = set()
        for name in LOOPBACK_NAMES:
            allowed |= expand(name)
        return allowed

    allowed = set()
    for name in extra:
        allowed |= expand(name)
    if bind_host not in ("0.0.0.0", "::", ""):
        allowed |= expand(bind_host)
        for name in LOOPBACK_NAMES:
            allowed |= expand(name)
    return allowed


def make_server(
    service: ConsoleService,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    allow_hosts: list[str] | None = None,
) -> ThreadingHTTPServer:
    """建好但**不启动**服务，便于测试里用 ``port=0`` 拿随机端口。

    ``Host`` 白名单见 :func:`build_allowed_hosts`。
    """
    handler = type("BoundConsoleHandler", (ConsoleHandler,), {"service": service})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    handler.allowed_hosts = build_allowed_hosts(
        host, httpd.server_address[1], allow_hosts
    )
    return httpd


def serve(
    service: ConsoleService,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    allow_hosts: list[str] | None = None,
    banner: bool = True,
) -> None:
    """启动控制台并阻塞。``Ctrl-C`` 退出。"""
    try:
        httpd = make_server(service, host=host, port=port, allow_hosts=allow_hosts)
    except OSError as exc:
        if exc.errno in (48, 98):  # EADDRINUSE
            raise SystemExit(f"端口 {port} 已被占用；换一个 --port 再试。") from exc
        raise
    actual = httpd.server_address[1]
    shown = "127.0.0.1" if host in ("", "0.0.0.0", "::") else host
    url = f"http://{shown}:{actual}/"

    if banner:
        print(f"课代表控制台已启动： {url}")
        print(f"  绑定 {host}:{actual}   数据目录 {service.root.resolve()}")
        if host not in ("127.0.0.1", "localhost", "::1", ""):
            print(f"  ⚠️  绑定的是 {host}，不是回环地址。")
            print("     同网络内能访问到这个地址的设备都可以操作本控制台，")
            print("     包括触发跑批。请确认这是你要的。")
        print("  Ctrl-C 退出")
        sys.stdout.flush()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.shutdown()
        httpd.server_close()


def pick_free_port(host: str = "127.0.0.1") -> int:
    """要一个当前空闲的端口号（拿完就释放，存在极小的竞态，够本地用）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])
