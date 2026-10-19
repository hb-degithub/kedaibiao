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

from kedaibiao.api.service import ConsoleError, ConsoleService, Viewer
from kedaibiao.security.auth import SESSION_TTL

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

SESSION_COOKIE = "kdb_session"
#: 不需要登录就能访问的路径。
#:
#: **登录页本身必须公开** —— 它就在 index.html 里，被登录门拦住的话
#: 浏览器只会看到一个 401，用户连输入账号密码的地方都没有。
PUBLIC_PATHS = {"/", "/index.html", "/api/auth/login", "/api/auth/me"}
PUBLIC_PREFIXES = ("/static/",)
CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
    "img-src 'self' data:; font-src 'self'; form-action 'none'; base-uri 'none'; "
    "frame-ancestors 'none'"
)


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
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        pending = getattr(self, "_session_cookie", None)
        if pending:
            self.send_header("Set-Cookie", pending)
            self._session_cookie = None
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

    # -- 会话 -------------------------------------------------------------

    def _token(self) -> str:
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            name, _, value = part.strip().partition("=")
            if name == SESSION_COOKIE:
                return value.strip()
        return ""

    def _viewer(self) -> Viewer:
        token = self._token()
        if not token:
            return Viewer()
        user = self.service.user_for_token(token)
        if user is None:
            return Viewer()
        return Viewer(
            user_id=int(user["id"]),
            is_superadmin=user.get("role") == "superadmin",
            username=user.get("username", ""),
        )

    def _require_auth(self, path: str, viewer: Viewer) -> bool:
        """返回 True 表示放行。未登录时已经写好 401，调用方直接返回。"""
        if path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES):
            return True
        if viewer.authenticated:
            return True
        self._error(HTTPStatus.UNAUTHORIZED, "请先登录", "unauthorized")
        return False

    def _set_session_cookie(self, token: str, max_age: int) -> None:
        self._session_cookie = (
            f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age}"
        )

    def _clear_session_cookie(self) -> None:
        self._session_cookie = (
            f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"
        )

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
            viewer = self._viewer()
            if not self._require_auth(path, viewer):
                return
            if method == "GET":
                self._route_get(path, viewer)
            else:
                self._route_post(path, viewer)
        except ConsoleError as exc:
            self._error(exc.status, str(exc), exc.kind)
        except Exception as exc:  # noqa: BLE001 - 兜底，别让服务线程静默死掉
            log.exception("处理 %s %s 失败", method, path)
            self._error(500, f"{type(exc).__name__}: {exc}", "internal")

    def _route_get(self, path: str, viewer: Viewer) -> None:
        svc = self.service
        if path in ("/", "/index.html"):
            return self._static("index.html")
        if path.startswith("/static/"):
            return self._static(path[len("/static/") :])

        # -- 认证与用户 --
        if path == "/api/auth/me":
            if not viewer.authenticated:
                # 明确返回 null，别给一个 id=None 的假用户 —— 前端会误判成已登录
                return self._json(200, {"user": None, "is_superadmin": False})
            return self._json(200, {
                "user": {
                    "id": viewer.user_id,
                    "username": viewer.username,
                    "role": "superadmin" if viewer.is_superadmin else "user",
                },
                "is_superadmin": viewer.is_superadmin,
            })
        if path == "/api/users":
            return self._json(200, svc.users(viewer))
        if path.startswith("/api/users/"):
            return self._error(404, f"没有这个路由: {path}", "not_found")
        if path == "/api/accounts":
            return self._json(200, svc.accounts(
                viewer,
                platform=self._query().get("platform", [None])[0],
                owner_id=self._owner_param(),
            ))
        if path == "/api/questions":
            return self._json(200, {
                "questions": svc.recent_questions(
                    self._int_param("limit", 50),
                    platform=self._query().get("platform", [None])[0],
                    owner_id=viewer.owner_filter(self._owner_param()),
                )
            })

        # 注意顺序：/api/platforms/<name>/actions 必须先于 /api/platforms 判断
        if path.startswith("/api/platforms/"):
            parts = path[len("/api/platforms/") :].split("/")
            if len(parts) == 2 and parts[1] == "logs" and parts[0]:
                return self._json(200, svc.platform_logs(
                    parts[0], limit=self._int_param("limit", 200)
                ))
            if len(parts) == 2 and parts[1] == "actions" and parts[0]:
                return self._json(200, svc.actions(parts[0]))
            return self._error(404, f"没有这个路由: {path}", "not_found")

        scope = self._platform_param()
        if path == "/api/overview":
            return self._json(200, svc.overview(scope, owner_id=self._owner_param(), viewer=viewer))
        if path == "/api/statistics":
            return self._json(200, svc.statistics(owner_id=self._owner_param(), viewer=viewer))
        if path == "/api/settings":
            return self._json(200, svc.settings_view())
        if path == "/api/platforms":
            return self._json(200, svc.platforms())
        if path == "/api/runs":
            return self._json(200, svc.runs(self._int_param("limit", 50), scope, owner_id=self._owner_param(), viewer=viewer))
        if path == "/api/submissions":
            return self._json(200, svc.submissions(self._int_param("limit", 50), scope, owner_id=self._owner_param(), viewer=viewer))
        if path == "/api/bank":
            return self._json(200, svc.bank(scope, owner_id=self._owner_param(), viewer=viewer))
        if path == "/api/events":
            return self._json(200, svc.events(self._int_param("limit", 100)))
        if path == "/api/jobs":
            return self._json(200, svc.jobs(scope, viewer=viewer))
        if path.startswith("/api/essays/jobs/"):
            return self._json(200, svc.grade_job(path[len("/api/essays/jobs/") :]))
        if path.startswith("/api/jobs/"):
            return self._json(200, svc.job(path[len("/api/jobs/") :]))
        self._error(404, f"没有这个路由: {path}", "not_found")

    def _route_post(self, path: str, viewer: Viewer) -> None:
        # -- 认证 --
        if path == "/api/auth/login":
            payload = self._body_json()
            if not isinstance(payload, dict):
                raise ConsoleError("请求体必须是 JSON 对象", kind="bad_request", status=400)
            user, session = self.service.login(
                str(payload.get("username") or ""), str(payload.get("password") or "")
            )
            self._set_session_cookie(session.token, int(SESSION_TTL.total_seconds()))
            return self._json(200, {"user": user})

        if path == "/api/auth/logout":
            self.service.logout(self._token())
            self._clear_session_cookie()
            return self._json(200, {"ok": True})

        # -- 大模型配置（控制台可改，只有超管能写）--
        if path == "/api/settings/llm":
            payload = self._body_json()
            if not isinstance(payload, dict):
                raise ConsoleError("请求体必须是 JSON 对象", kind="bad_request", status=400)
            return self._json(200, self.service.update_llm_config(viewer, payload))
        if path == "/api/settings/llm/test":
            self._body_json()  # 统一要求 JSON Content-Type，body 可为空
            return self._json(200, self.service.test_llm_connection(viewer))

        # -- 用户管理 --
        if path == "/api/users":
            return self._json(200, self._create_user(viewer))
        if path.startswith("/api/users/"):
            parts = path[len("/api/users/") :].split("/")
            if len(parts) == 2 and parts[0].isdigit():
                user_id = int(parts[0])
                if parts[1] == "delete":
                    self.service.delete_user(viewer, user_id)
                    return self._json(200, {"ok": True})
                if parts[1] == "update":
                    payload = self._body_json()
                    if not isinstance(payload, dict):
                        raise ConsoleError("请求体必须是 JSON 对象", kind="bad_request", status=400)
                    return self._json(200, self.service.update_user(
                        viewer, user_id,
                        username=payload.get("username"),
                        password=payload.get("password"),
                        role=payload.get("role"),
                        disabled=payload.get("disabled"),
                        note=payload.get("note"),
                    ))
            return self._error(404, f"没有这个路由: {path}", "not_found")

        # -- 平台账号 --
        if path == "/api/accounts":
            payload = self._body_json()
            if not isinstance(payload, dict):
                raise ConsoleError("请求体必须是 JSON 对象", kind="bad_request", status=400)
            return self._json(201, self.service.add_account(
                viewer,
                str(payload.get("platform") or ""),
                label=str(payload.get("label") or ""),
                username=str(payload.get("username") or ""),
                password=str(payload.get("password") or ""),
                login_now=bool(payload.get("login", True)),
                owner_id=payload.get("owner_id"),
            ))
        if path.startswith("/api/accounts/"):
            parts = path[len("/api/accounts/") :].split("/")
            if len(parts) == 2 and parts[0].isdigit():
                account_id = int(parts[0])
                if parts[1] == "activate":
                    return self._json(200, self.service.activate_account(viewer, account_id))
                if parts[1] == "delete":
                    self.service.delete_account(viewer, account_id)
                    return self._json(200, {"ok": True})
                if parts[1] == "login":
                    payload = self._body_json()
                    if not isinstance(payload, dict):
                        raise ConsoleError("请求体必须是 JSON 对象", kind="bad_request", status=400)
                    return self._json(200, self.service.login_account(
                        viewer, account_id, str(payload.get("password") or "")
                    ))
                if parts[1] == "update":
                    payload = self._body_json()
                    if not isinstance(payload, dict):
                        raise ConsoleError("请求体必须是 JSON 对象", kind="bad_request", status=400)
                    return self._json(200, self.service.update_account(
                        viewer, account_id,
                        label=payload.get("label"), username=payload.get("username"),
                    ))
            return self._error(404, f"没有这个路由: {path}", "not_found")

        # -- iTEST 一键自动化（后台任务 + 流式日志）--
        if path == "/api/platforms/itest/auto":
            payload = self._body_json()
            if not isinstance(payload, dict):
                raise ConsoleError("请求体必须是 JSON 对象", kind="bad_request", status=400)
            wait = payload.get("autosave_wait")
            job = self.service.start_itest_auto(
                str(payload.get("exam_id") or ""),
                viewer=viewer,
                autosave_wait=None if wait is None else float(wait),
            )
            return self._json(202, job)
        if path == "/api/platforms/itest/manual":
            payload = self._body_json()
            if not isinstance(payload, dict):
                raise ConsoleError("请求体必须是 JSON 对象", kind="bad_request", status=400)
            answers = payload.get("answers")
            if not isinstance(answers, dict):
                raise ConsoleError("answers 必须是对象", kind="bad_request", status=400)
            return self._json(200, self.service.save_itest_manual(
                str(payload.get("exam_id") or ""),
                {str(k): str(v) for k, v in answers.items()},
                viewer=viewer,
            ))

        if path.startswith("/api/platforms/"):
            parts = path[len("/api/platforms/") :].split("/")
            if len(parts) == 3 and parts[1] == "actions" and parts[0] and parts[2]:
                payload = self._body_json()
                if not isinstance(payload, dict):
                    raise ConsoleError(
                        "请求体必须是 JSON 对象", kind="bad_request", status=400
                    )
                return self._json(200, self.service.run_action(
                    parts[0], parts[2], payload, viewer=viewer
                ))
            return self._error(404, f"没有这个路由: {path}", "not_found")

        if path == "/api/essays/grade-loop":
            payload = self._body_json()
            if not isinstance(payload, dict):
                raise ConsoleError("请求体必须是 JSON 对象", kind="bad_request", status=400)
            return self._json(202, self.service.start_grade_loop(
                str(payload.get("platform") or ""),
                str(payload.get("assignment_id") or payload.get("rid") or ""),
                target_score=float(payload.get("target_score") or 90),
                max_attempts=int(payload.get("max_attempts") or 3),
                initial_essay=str(payload.get("initial_essay") or ""),
                viewer=viewer,
            ))

        if path == "/api/essays/fetch":
            payload = self._body_json()
            if not isinstance(payload, dict):
                raise ConsoleError("请求体必须是 JSON 对象", kind="bad_request", status=400)
            return self._json(200, self.service.fetch_essay_grade(
                str(payload.get("platform") or "pigai"),
                str(payload.get("assignment_id") or payload.get("rid") or ""),
                str(payload.get("eid") or ""),
                viewer=viewer,
                trigger_analysis=bool(payload.get("trigger_analysis", True)),
            ))

        if path == "/api/assignments/detail":
            payload = self._body_json()
            if not isinstance(payload, dict):
                raise ConsoleError("请求体必须是 JSON 对象", kind="bad_request", status=400)
            return self._json(200, self.service.assignment_detail(
                str(payload.get("platform") or ""),
                str(payload.get("assignment_id") or ""),
                viewer=viewer,
            ))

        if path == "/api/essays/history":
            payload = self._body_json()
            if not isinstance(payload, dict):
                raise ConsoleError("请求体必须是 JSON 对象", kind="bad_request", status=400)
            return self._json(200, self.service.essay_history(
                str(payload.get("assignment_id") or payload.get("rid") or ""),
                platform=str(payload.get("platform") or "pigai"),
                viewer=viewer,
            ))

        if path == "/api/assignments/submit":
            payload = self._body_json()
            if not isinstance(payload, dict):
                raise ConsoleError("请求体必须是 JSON 对象", kind="bad_request", status=400)
            return self._json(200, self.service.submit_assignment(
                str(payload.get("platform") or ""),
                str(payload.get("assignment_id") or ""),
                viewer=viewer,
                confirm=bool(payload.get("confirm", False)),
            ))

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
            viewer=viewer,
        )
        self._json(HTTPStatus.ACCEPTED, job)

    def _owner_param(self) -> int | None:
        """超管用 ``?owner_id=`` 指定看谁的数据。普通用户传别人会被 403。"""
        raw = self._query().get("owner_id", [None])[0]
        if raw in (None, ""):
            return None
        try:
            return int(raw)
        except ValueError:
            raise ConsoleError("owner_id 必须是整数", kind="bad_request", status=400) from None

    def _create_user(self, viewer: Viewer) -> dict[str, Any]:
        payload = self._body_json()
        if not isinstance(payload, dict):
            raise ConsoleError("请求体必须是 JSON 对象", kind="bad_request", status=400)
        return self.service.create_user(
            viewer,
            str(payload.get("username") or ""),
            str(payload.get("password") or ""),
            role=str(payload.get("role") or "user"),
            note=str(payload.get("note") or ""),
        )

    def _platform_param(self) -> str | None:
        """``?platform=`` 原样返回给服务层校验（不在这里判存在性）。"""
        raw = self._query().get("platform", [None])[0]
        return raw or None

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
    # 首次启动创建超管（密码只在这一刻打印一次）
    created, generated = service.bootstrap_admin()
    if created is not None and generated:
        print("=" * 60)
        print("已创建超级管理员账号：")
        print(f"  用户名: {created['username']}")
        print(f"  密码  : {generated}")
        print("这串密码只显示这一次，请立刻保存（库里只存哈希，无法找回）。")
        print("=" * 60)
        sys.stdout.flush()

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
