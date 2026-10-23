#!/usr/bin/env python3
"""后端接口矩阵自检：路由 × 安全 × keep-alive × 并发 × 静态资源。

设计约束（重要）：

- **离线**：只连自己刚起的服务（``make_server(port=0)`` 的随机端口），
  绝不连接本机正在使用的 8765 控制台；
- **不碰真实数据**：临时目录 + 临时 sqlite，用完即删，不读 ``data/``；
- **不启动真实平台**：进程内注册一个假平台 ``httpfake``，所有平台调用都在本地完成；
- **只读**：不改 ``src/``、``tests/``，唯一产物就是本文件。

覆盖内容见任务 task-9：路由矩阵、安全（Host/Origin/穿越/401/403/415/413/响应头）、
keep-alive 回归（带 body 的 POST 之后接 GET）、错误分支、8 线程并发、静态资源区分。

用法::

    python3 scripts/api_matrix_check.py

退出码：0 = 全绿；1 = 有失败（每项 PASS/FAIL，末尾汇总 N/M 并列出失败项）。
"""

from __future__ import annotations

import argparse
import contextlib
import http.client
import json
import re
import sys
import tempfile
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from kedaibiao import adapters  # noqa: E402
from kedaibiao.api.server import make_server  # noqa: E402
from kedaibiao.api.service import ConsoleService  # noqa: E402
from kedaibiao.core.models import (  # noqa: E402
    Assignment,
    Course,
    Credentials,
    Question,
    QuestionType,
    SubmissionResult,
)
from kedaibiao.platforms.base import Capabilities, PlatformAdapter  # noqa: E402
from kedaibiao.security.accounts import AccountStore  # noqa: E402
from kedaibiao.security.auth import AuthStore  # noqa: E402

#: 用户正在使用的控制台端口 —— 本脚本永远不连它。
LIVE_PORT = 8765
FAKE = "httpfake"
ADMIN_PW = "adminpass123"
NORMAL_PW = "normalpass123"
MAX_BODY = 64 * 1024


# ---------------------------------------------------------------------------
# 进程内的假平台（离线，所有方法立即返回）
# ---------------------------------------------------------------------------


class FakeAdapter(PlatformAdapter):
    name = FAKE
    display_name = "矩阵自检假平台"
    capabilities = Capabilities()

    async def login(self, credentials: Credentials) -> bool:
        return True

    async def is_logged_in(self) -> bool:
        return True

    async def list_courses(self) -> list[Course]:
        return [Course(remote_id="c1", name="课程一"), Course(remote_id="c2", name="课程二")]

    async def list_assignments(self, course: Course) -> list[Assignment]:
        return [Assignment(remote_id="a1", title="作业一")]

    async def fetch_questions(self, assignment: Assignment) -> list[Question]:
        return [
            Question(stem="已知题", options=["甲", "乙"], remote_id="q1",
                     type=QuestionType.SINGLE)
        ]

    async def submit(self, assignment: Assignment, answers) -> SubmissionResult:  # noqa: ANN001
        return SubmissionResult(assignment_id=assignment.remote_id, ok=True, score=80.0)

    async def native_course_tree(  # noqa: ANN001
        self, course: Course, *, with_state: bool = True, state_scope: str = "required"
    ) -> dict[str, Any]:
        return {
            "course": course.remote_id,
            "name": course.name,
            "with_state": bool(with_state),
            "state_scope": state_scope,
            "units": [{"id": "u1", "name": "单元一",
                       "tasks": [{"id": "t1", "name": "任务一"}]}],
        }


# ---------------------------------------------------------------------------
# HTTP 小工具
# ---------------------------------------------------------------------------


@dataclass
class Resp:
    status: int
    headers: dict[str, str]
    body: bytes

    @property
    def json(self) -> Any:
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None

    @property
    def kind(self) -> str:
        data = self.json
        return str(data.get("kind") or "") if isinstance(data, dict) else ""

    def snippet(self, limit: int = 260) -> str:
        text = self.body.decode("utf-8", "replace")
        return text[:limit].replace("\n", " ")


class Harness:
    """临时控制台：临时库 + 随机端口 + 假平台 + 两个用户。"""

    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="kdb-matrix-")
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "api.db"
        (self.root / "config").mkdir()
        (self.root / "config" / "local.toml").write_text(
            "[kedaibiao]\n"
            f'db_url = "sqlite:///{self.db_path}"\n'
            f'session_dir = "{self.root}/sessions"\n'
            'notify = "null"\n'
            "dry_run = true\n",
            encoding="utf-8",
        )

        adapters.register(FakeAdapter.name, FakeAdapter, override=True)
        # environ={}：显式清空环境变量，保证 .env 里的 LLM key 不会让「测连通性」出网
        self.service = ConsoleService(root=self.root, environ={})

        created, generated = self.service.bootstrap_admin()
        if created is not None and generated:
            admin_name, admin_pw = created["username"], generated
        else:  # 理论上不会发生（全新临时库）；保底造一个已知密码的超管
            store = AuthStore(self.db_path)
            try:
                store.create_user("admin2", ADMIN_PW, role="superadmin")
            finally:
                store.close()
            admin_name, admin_pw = "admin2", ADMIN_PW
        user, session = self.service.login(admin_name, admin_pw)
        self.user_id = int(user["id"])
        self.token_admin = session.token

        store = AuthStore(self.db_path)
        try:
            store.create_user("normal", NORMAL_PW, role="user")
        finally:
            store.close()
        _, normal_session = self.service.login("normal", NORMAL_PW)
        self.token_normal = normal_session.token
        #: 一次性会话：专门给 /api/auth/logout 用，别把主令牌注销掉
        self.token_disposable = self.service.login("normal", NORMAL_PW)[1].token

        store = AccountStore(self.db_path)
        try:
            # 给假平台绑一个号：动作/教材树/course-tree 才会走到平台调用这一层
            store.add(self.user_id, FAKE, label="自检号", username="stu")
        finally:
            store.close()

        self.httpd = make_server(self.service, host="127.0.0.1", port=0)
        self.port = int(self.httpd.server_address[1])
        if self.port == LIVE_PORT:
            raise RuntimeError(f"随机端口撞上了正在使用的 {LIVE_PORT}，拒绝继续")
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        self.thread.start()
        self._lock = threading.Lock()
        self._checks = 0

    # -- 生命周期 ---------------------------------------------------------

    def close(self) -> None:
        try:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.thread.join(timeout=5)
        finally:
            # 退出清理，失败不影响已经产出的结果
            with contextlib.suppress(Exception):
                adapters.unregister(FAKE)
            self.tmp.cleanup()

    # -- 请求 -------------------------------------------------------------

    def call(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        token: str | None = None,
        host: str | None = None,
        origin: str | None = None,
        conn: http.client.HTTPConnection | None = None,
        timeout: float = 15.0,
    ) -> Resp:
        hdrs = dict(headers or {})
        if json_body is not None:
            body = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json")
        if token:
            hdrs.setdefault("Cookie", f"kdb_session={token}")
        if origin is not None:
            hdrs["Origin"] = origin

        own = conn is None
        if own:
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        try:
            conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            conn.putheader("Host", host or f"127.0.0.1:{self.port}")
            for key, value in hdrs.items():
                conn.putheader(key, value)
            if body is not None:
                conn.putheader("Content-Length", str(len(body)))
            elif method in ("POST", "PUT", "PATCH"):
                conn.putheader("Content-Length", "0")
            conn.endheaders()
            if body:
                conn.send(body)
            resp = conn.getresponse()
            data = resp.read()
            return Resp(
                resp.status,
                {k.lower(): v for k, v in resp.getheaders()},
                data,
            )
        finally:
            if own:
                conn.close()

    def request(
        self, path: str, *, method: str = "GET", token: str | None = None, **kwargs: Any
    ) -> Resp:
        token = self.token_admin if token is None else token
        return self.call(method, path, token=token or None, **kwargs)


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------


class Report:
    def __init__(self) -> None:
        self.results: list[tuple[str, bool, str]] = []
        self.infos: list[str] = []

    def check(self, name: str, fn: Callable[[], str | None]) -> None:
        try:
            note = fn() or ""
        except AssertionError as exc:
            self.results.append((name, False, str(exc)))
            print(f"FAIL  {name}  — {exc}")
        except Exception as exc:  # noqa: BLE001 - 任何异常都算失败，但别中断整套
            detail = f"{type(exc).__name__}: {exc}"
            self.results.append((name, False, detail))
            print(f"FAIL  {name}  — {detail}")
        else:
            self.results.append((name, True, note))
            print(f"PASS  {name}" + (f"  ({note})" if note else ""))

    def info(self, text: str) -> None:
        self.infos.append(text)
        print(f"INFO  {text}")

    def summary(self) -> int:
        total = len(self.results)
        failed = [(n, d) for n, ok, d in self.results if not ok]
        print()
        if self.infos:
            print("观察项（不计入通过率）：")
            for line in self.infos:
                print(f"  · {line}")
            print()
        print(f"汇总: {total - len(failed)}/{total} 通过")
        if failed:
            print("失败清单：")
            for name, detail in failed:
                print(f"  - {name}: {detail}")
            return 1
        print("全部通过。")
        return 0


def expect(
    resp: Resp,
    statuses: int | set[int],
    *,
    kind: str | None = None,
    route_known: bool = True,
    note: str | None = None,
) -> str:
    """校验一个响应：状态码 ∈ statuses，可选校验 kind，错误体必须是契约 JSON。"""
    allowed = {statuses} if isinstance(statuses, int) else set(statuses)
    if resp.status not in allowed:
        raise AssertionError(
            f"期望 {sorted(allowed)}，实际 {resp.status} kind={resp.kind or '-'} "
            f"body={resp.snippet()}"
        )
    data = resp.json
    if route_known and resp.status == 404:
        msg = str((data or {}).get("error") or "") if isinstance(data, dict) else ""
        if "没有这个路由" in msg:
            raise AssertionError(f"路由没有匹配上（{msg}）；路径可能写错或已下线")
    if kind is not None and resp.kind != kind:
        raise AssertionError(f"期望 kind={kind}，实际 kind={resp.kind!r} body={resp.snippet()}")
    if resp.status >= 400 and (
        not isinstance(data, dict) or "error" not in data or "kind" not in data
    ):
        # 例外：二进制/纯文本路由（如学习通前台画面 /live 的 JPEG 帧）没有 JSON 契约，
        # 它的 404 只回 ``no frame``。仅放行这类 media 类型的错误响应，别把契约放宽到 JSON 路由。
        ctype = resp.headers.get("content-type", "")
        if not ("text/plain" in ctype or ctype.startswith("image/")):
            raise AssertionError(f"错误响应不符合契约（缺 error/kind）: {resp.snippet()}")
    # 静态资源（HTML/JS/CSS）没有 JSON body；API 的 2xx 必须是 JSON
    if (resp.status < 400 and not isinstance(data, (dict, list))
            and "json" in resp.headers.get("content-type", "")):
        raise AssertionError(f"2xx 但 body 不是 JSON: {resp.snippet()}")
    return note or f"{resp.status}" + (f" {resp.kind}" if resp.kind else "")


# ---------------------------------------------------------------------------
# 1) 路由矩阵
# ---------------------------------------------------------------------------
#
# 每条：(方法, 路径, JSON body, 允许状态码集合, 期望 kind, 路由必须命中)
# GET 成功路径与错误分支都放在同一张表里，顺序 = server.py 的读取顺序。

GET_CASES: list[tuple[str, set[int], str | None, bool]] = [
    # 静态页面与资源
    ("/", {200}, None, True),
    ("/index.html", {200}, None, True),
    ("/classic", {200}, None, True),
    ("/classic/index.html", {200}, None, True),
    ("/next", {200}, None, True),
    ("/static/next/app.js", {200}, None, True),
    ("/static/next/app.css", {200}, None, True),
    ("/static/style.css", {200}, None, True),
    ("/static/v1.html", {200}, None, True),
    ("/static/next/not-here.js", {404}, "not_found", True),
    # 认证 / 用户 / 账号
    ("/api/auth/me", {200}, None, True),
    ("/api/users", {200}, None, True),
    ("/api/accounts", {200}, None, True),
    ("/api/accounts/login/lg-404", {404}, "not_found", True),
    ("/api/users/999", {404}, "not_found", False),  # 已知死分支：恒 404
    # 平台级只读
    ("/api/platforms", {200}, None, True),
    ("/api/platforms/" + FAKE + "/actions", {200}, None, True),
    ("/api/platforms/" + FAKE + "/logs", {200}, None, True),
    ("/api/platforms/" + FAKE + "/course-tree?course=c1", {200}, None, True),
    ("/api/platforms/ghost/actions", {404}, "unknown_platform", True),
    ("/api/platforms/ghost/logs", {404}, "unknown_platform", True),
    ("/api/platforms/ghost/course-tree?course=x", {404}, "unknown_platform", True),
    # 学习通前台画面 / 滑块验证码：任务态路由，没有这个任务时不碰浏览器。
    # /live 的 404 是纯文本 ``no frame``（图片路由没有 JSON 契约，见 expect 的例外）。
    ("/api/platforms/chaoxing/live?job=nope", {404}, None, True),
    ("/api/platforms/chaoxing/captcha?job=nope", {200}, None, True),
    # 聚合只读
    ("/api/overview", {200}, None, True),
    ("/api/overview?platform=" + FAKE, {200}, None, True),
    ("/api/statistics", {200}, None, True),
    ("/api/settings", {200}, None, True),
    ("/api/settings/llm", {200}, None, True),
    ("/api/schedule", {200}, None, True),
    ("/api/runs", {200}, None, True),
    ("/api/runs?platform=" + FAKE, {200}, None, True),
    ("/api/submissions", {200}, None, True),
    ("/api/bank", {200}, None, True),
    ("/api/events", {200}, None, True),
    ("/api/jobs", {200}, None, True),
    # 任务
    ("/api/jobs/j-404", {404}, "not_found", True),
    ("/api/essays/jobs/j-404", {404}, "not_found", True),
    # 未知
    ("/api/definitely-not-a-route", {404}, "not_found", False),
]

POST_CASES: list[tuple[str, dict[str, Any], set[int], str | None, bool]] = [
    # 认证（login 用错密码验证「公开路径 + 401」；logout 只注销一次性会话）
    ("/api/auth/login", {"username": "no-such-user", "password": "x"},
     {401}, "unauthorized", True),
    ("/api/auth/logout", {}, {200}, None, True),
    # 配置
    ("/api/settings/llm", {}, {200}, None, True),
    ("/api/settings/llm/test", {}, {400}, "bad_request", True),
    ("/api/settings/llm", {"base_url": "ftp://x"}, {400}, "bad_request", True),
    # 用户
    ("/api/users", {}, {400}, "bad_request", True),
    ("/api/users/999/update", {}, {404}, "not_found", True),
    ("/api/users/999/delete", {}, {404}, "not_found", True),
    # 账号
    ("/api/accounts", {}, {400}, "bad_request", True),
    ("/api/accounts", {"platform": "ghost", "username": "u"}, {404}, "unknown_platform", True),
    ("/api/accounts/999/activate", {}, {404}, "not_found", True),
    ("/api/accounts/999/login", {"password": "x"}, {404}, "not_found", True),
    # 交互式登录：平台不在已核实清单里 → 501 not_supported（不是 500）
    ("/api/accounts/login/start",
     {"platform": FAKE, "username": "u", "password": "p"},
     {501}, "not_supported", True),
    ("/api/accounts/999/update", {}, {404}, "not_found", True),
    ("/api/accounts/999/delete", {}, {404}, "not_found", True),
    # iTEST
    ("/api/platforms/itest/auto", {}, {400}, "bad_request", True),
    ("/api/platforms/itest/manual", {}, {400}, "bad_request", True),
    # 品课
    ("/api/platforms/pinco/plan", {}, {400}, "no_credentials", True),
    ("/api/platforms/pinco/submit-reviewed", {}, {400}, "bad_request", True),
    # 学习通人工过滑块/点击：没有对应的前台任务时服务层直接回 ok=false，不触达浏览器
    ("/api/platforms/chaoxing/pointer", {}, {200}, None, True),
    # 任务控制
    ("/api/jobs/j-404/cancel", {}, {404}, "not_found", True),
    # 刷课（闸门在平台能力上，不会真的起后台任务）
    ("/api/platforms/ghost/brush", {}, {404}, "unknown_platform", True),
    ("/api/platforms/" + FAKE + "/brush", {"course": "c1"}, {501}, "not_supported", True),
    # 一键全部提交
    ("/api/platforms/ghost/batch-submit", {}, {404}, "unknown_platform", True),
    ("/api/platforms/" + FAKE + "/batch-submit", {}, {501}, "not_supported", True),
    # 平台动作
    ("/api/platforms/" + FAKE + "/actions/courses", {}, {200}, None, True),
    ("/api/platforms/" + FAKE + "/actions/assignments", {"with_state": "0"}, {200}, None, True),
    ("/api/platforms/" + FAKE + "/actions/nope", {}, {404}, "unknown_action", True),
    ("/api/platforms/ghost/actions/courses", {}, {404}, "unknown_platform", True),
    # 作业详情 / 提交
    ("/api/assignments/detail", {"platform": "ghost", "assignment_id": "a1"},
     {404}, "unknown_platform", True),
    ("/api/assignments/detail", {"platform": FAKE, "assignment_id": "a1"},
     {200}, None, True),
    ("/api/assignments/submit", {"platform": "ghost", "assignment_id": "a1"},
     {404}, "unknown_platform", True),
    ("/api/assignments/submit", {"platform": FAKE}, {400}, "bad_request", True),
    # 作文（pigai 专用 / 本地历史）
    ("/api/essays/grade-loop", {"platform": "pigai"}, {400}, "bad_request", True),
    ("/api/essays/wait", {"platform": "pigai"}, {400}, "bad_request", True),
    ("/api/essays/fetch", {"platform": "pigai", "assignment_id": "r1"},
     {400}, "no_credentials", True),
    ("/api/essays/history", {"assignment_id": "r1", "platform": "pigai"}, {200}, None, True),
    # 跑批
    ("/api/run", {}, {400}, "bad_request", True),
    ("/api/run", {"platform": "ghost"}, {404}, "unknown_platform", True),
]


def section_routes(h: Harness, rep: Report) -> None:
    print("\n== 1. 路由矩阵（登录态：超管） ==")
    for path, allowed, kind, known in GET_CASES:
        def run(path: str = path, allowed: set[int] = allowed,
                kind: str | None = kind, known: bool = known) -> str:
            return expect(h.request(path), allowed, kind=kind, route_known=known)
        rep.check(f"GET  {path}", run)

    for path, payload, allowed, kind, known in POST_CASES:
        def run(path: str = path, payload: dict[str, Any] = payload,
                allowed: set[int] = allowed, kind: str | None = kind,
                known: bool = known) -> str:
            token = h.token_disposable if path == "/api/auth/logout" else h.token_admin
            note = expect(
                h.call("POST", path, json_body=payload, token=token),
                allowed, kind=kind, route_known=known,
            )
            if path == "/api/auth/logout":
                # 注销后补一张新的一次性会话，避免「注销一次就永久失效」
                h.token_disposable = h.service.login("normal", NORMAL_PW)[1].token
            return note
        label = path + (f"  {json.dumps(payload, ensure_ascii=False)}" if payload else "")
        rep.check(f"POST {label}", run)

    def head_root() -> str:
        resp = h.call("HEAD", "/", token=h.token_admin)
        if resp.status != 200:
            raise AssertionError(f"HEAD / 期望 200，实际 {resp.status}")
        if resp.body:
            raise AssertionError(f"HEAD 不应有响应体，实际 {len(resp.body)} 字节")
        return "200 且无 body"

    def head_api() -> str:
        resp = h.call("HEAD", "/api/auth/me", token=h.token_admin)
        if resp.status != 200:
            raise AssertionError(f"HEAD /api/auth/me 期望 200，实际 {resp.status}")
        return "200"

    rep.check("HEAD / → 200（且无响应体）", head_root)
    rep.check("HEAD /api/auth/me → 200", head_api)


# ---------------------------------------------------------------------------
# 2) 路由表与 server.py 同步（防止脚本慢慢过期）
# ---------------------------------------------------------------------------


def _discover_routes() -> tuple[set[str], set[str]]:
    """从 server.py 的 ``if path ...`` 条件里抽出字面路由和前缀模板。"""
    server = (ROOT / "src" / "kedaibiao" / "api" / "server.py").read_text(encoding="utf-8")
    literals: set[str] = set()
    prefixes: set[str] = set()
    for line in server.splitlines():
        if "path" not in line or not re.search(r"\bpath\s*(==|!=|\.startswith|\bin\b)", line):
            continue
        for match in re.finditer(r'"(/[^"]*)"', line):
            value = match.group(1)
            if not (value.startswith("/api/") or value.startswith("/static/")
                    or value in {"/", "/index.html", "/next", "/classic",
                                 "/classic/index.html"}):
                continue
            if "startswith" in line and value.endswith("/"):
                prefixes.add(value)
            else:
                literals.add(value)
    return literals, prefixes


def section_route_sync(h: Harness, rep: Report) -> None:
    print("\n== 2. 矩阵与 server.py 路由表同步 ==")
    literals, prefixes = _discover_routes()
    tested = {path.split("?", 1)[0] for path, *_ in GET_CASES}
    tested |= {path.split("?", 1)[0] for path, *_ in POST_CASES}
    #: 故意打不存在的路径（未知路由/死分支探测）不参与「脚本是否过期」检查
    known_paths = {path.split("?", 1)[0] for path, _a, _k, known in GET_CASES if known}
    known_paths |= {path.split("?", 1)[0] for path, _p, _a, _k, known in POST_CASES if known}

    def literal_covered() -> str:
        missing = sorted(p for p in literals if p not in tested)
        if missing:
            raise AssertionError(f"server.py 里有、矩阵没测: {missing}")
        return f"{len(literals)} 条字面路由全部覆盖"

    def prefix_covered() -> str:
        missing = sorted(p for p in prefixes if not any(t.startswith(p) for t in tested))
        if missing:
            raise AssertionError(f"这些前缀模板没有探测路径: {missing}")
        return f"{len(prefixes)} 个前缀模板全部覆盖"

    def no_stale() -> str:
        stale = []
        for path in known_paths:
            if path in literals or path.startswith("/static/") or path == "/":
                continue
            if not any(path.startswith(p) for p in prefixes):
                stale.append(path)
        if stale:
            raise AssertionError(f"矩阵里有 server.py 不认的路径（脚本过期）: {stale}")
        return f"{len(known_paths)} 条探测路径都在路由表内"

    rep.check("路由矩阵覆盖 server.py 全部字面路由", literal_covered)
    rep.check("路由矩阵覆盖 server.py 全部前缀模板", prefix_covered)
    rep.check("矩阵无过期路径", no_stale)


# ---------------------------------------------------------------------------
# 3) 安全
# ---------------------------------------------------------------------------


def section_security(h: Harness, rep: Report) -> None:
    print("\n== 3. 安全 ==")

    def host_forged() -> str:
        resp = h.call("GET", "/api/auth/me", host="evil.com", token=h.token_admin)
        return expect(resp, {403}, kind="forbidden")

    def host_ok() -> str:
        # Host 带端口是浏览器默认写法，必须放行（正对照）
        return expect(h.call("GET", "/api/auth/me"), {200})

    def origin_cross() -> str:
        resp = h.call("GET", "/api/overview", token=h.token_admin,
                      origin="http://evil.example")
        return expect(resp, {403}, kind="forbidden")

    def origin_null() -> str:
        resp = h.call("GET", "/api/overview", token=h.token_admin, origin="null")
        return expect(resp, {403}, kind="forbidden")

    def origin_same() -> str:
        resp = h.call("GET", "/api/overview", token=h.token_admin,
                      origin=f"http://127.0.0.1:{h.port}")
        return expect(resp, {200})

    def traversal_raw() -> str:
        resp = h.call("GET", "/static/../../etc/passwd", token=h.token_admin)
        expect(resp, {403, 404}, route_known=False)
        if b"root:" in resp.body:
            raise AssertionError("路径穿越读到了 /etc/passwd")
        return f"{resp.status}（未泄露文件）"

    def traversal_encoded() -> str:
        resp = h.call("GET", "/static/%2e%2e/%2e%2e/etc/passwd", token=h.token_admin)
        expect(resp, {403, 404}, route_known=False)
        if b"root:" in resp.body:
            raise AssertionError("编码后的路径穿越读到了 /etc/passwd")
        return f"{resp.status}"

    def traversal_parent() -> str:
        resp = h.call("GET", "/static/../api/server.py", token=h.token_admin)
        expect(resp, {403, 404}, route_known=False)
        if b"ConsoleService" in resp.body or b"def _dispatch" in resp.body:
            raise AssertionError("路径穿越读到了服务端源码")
        return f"{resp.status}"

    def unauth_overview() -> str:
        resp = h.call("GET", "/api/overview", token="")
        return expect(resp, {401}, kind="unauthorized")

    def unauth_users() -> str:
        resp = h.call("GET", "/api/users", token="")
        return expect(resp, {401}, kind="unauthorized")

    def unauth_post() -> str:
        resp = h.call("POST", "/api/settings/llm", json_body={}, token="")
        return expect(resp, {401}, kind="unauthorized")

    def public_login_page() -> str:
        # 登录页必须公开，否则用户连登录框都看不到
        return expect(h.call("GET", "/", token=""), {200})

    def non_admin_users() -> str:
        resp = h.call("GET", "/api/users", token=h.token_normal)
        return expect(resp, {403}, kind="forbidden")

    def non_admin_llm() -> str:
        resp = h.call("POST", "/api/settings/llm", json_body={}, token=h.token_normal)
        return expect(resp, {403}, kind="forbidden")

    def normal_user_overview() -> str:
        # 普通用户自己的数据必须能看（不是一刀切 403）
        return expect(h.call("GET", "/api/overview", token=h.token_normal), {200})

    def headers_ok() -> str:
        resp = h.call("GET", "/api/overview", token=h.token_admin)
        expect(resp, {200})
        wanted = {
            "x-content-type-options": "nosniff",
            "x-frame-options": "DENY",
            "referrer-policy": "no-referrer",
        }
        for name, value in wanted.items():
            got = resp.headers.get(name, "")
            if got != value:
                raise AssertionError(f"响应头 {name}={got!r}，期望 {value!r}")
        csp = resp.headers.get("content-security-policy", "")
        if "default-src 'none'" not in csp:
            raise AssertionError(f"CSP 缺 default-src 'none': {csp!r}")
        if resp.headers.get("cache-control") != "no-store":
            raise AssertionError(
                f"Cache-Control={resp.headers.get('cache-control')!r}，期望 no-store"
            )
        return "nosniff/DENY/CSP/no-store 齐备"

    def ctype_required() -> str:
        resp = h.call("POST", "/api/settings/llm", body=b"{}",
                      headers={"Content-Type": "text/plain"}, token=h.token_admin)
        return expect(resp, {415})

    def body_too_large() -> str:
        big = b'{"pad":"' + b"x" * (MAX_BODY + 4096) + b'"}'
        resp = h.call("POST", "/api/settings/llm", body=big,
                      headers={"Content-Type": "application/json"}, token=h.token_admin)
        return expect(resp, {413})

    def bad_json() -> str:
        resp = h.call("POST", "/api/settings/llm", body=b"{not json",
                      headers={"Content-Type": "application/json"}, token=h.token_admin)
        return expect(resp, {400}, kind="bad_request")

    def login_bad_password() -> str:
        resp = h.call("POST", "/api/auth/login", token="",
                      json_body={"username": "normal", "password": "wrong-password"})
        return expect(resp, {401}, kind="unauthorized")

    def login_cookie_flags() -> str:
        resp = h.call("POST", "/api/auth/login", token="",
                      json_body={"username": "normal", "password": NORMAL_PW})
        expect(resp, {200})
        cookie = resp.headers.get("set-cookie", "")
        for flag in ("kdb_session=", "HttpOnly", "SameSite=Strict"):
            if flag not in cookie:
                raise AssertionError(f"Set-Cookie 缺 {flag!r}: {cookie!r}")
        return "HttpOnly + SameSite=Strict"

    def origin_userinfo() -> str:
        """Origin 里塞 userinfo，试图让 host 解析退化成 127.0.0.1。"""
        origin = f"http://127.0.0.1:{h.port}@evil.example"
        resp = h.call("GET", "/api/overview", token=h.token_admin, origin=origin)
        try:
            return expect(resp, {403}, kind="forbidden")
        except AssertionError as exc:
            # 期望 403（跨站被拒）。实际 200：_origin_ok 用
            # netloc.rsplit(":", 1)[0] 取 host，userinfo 形式会退化成
            # "127.0.0.1" 而命中白名单。浏览器不会发这种 Origin，故实际影响低。
            raise AssertionError(
                f"请求 GET /api/overview，Origin: {origin}；期望 403，实际 {resp.status}；"
                "根因：server.py:_origin_ok 用 netloc.rsplit(':',1)[0] 取 host，"
                "userinfo 会退化成 127.0.0.1 命中白名单（建议改用 urlsplit(origin).hostname）"
            ) from exc

    def forged_cookie() -> str:
        resp = h.call("GET", "/api/overview", token="garbage-token-value")
        return expect(resp, {401}, kind="unauthorized")

    def owner_escalation() -> str:
        # 普通用户带 ?owner_id=<超管 id> 想看他人的数据 → 403
        resp = h.call("GET", f"/api/overview?owner_id={h.user_id}", token=h.token_normal)
        return expect(resp, {403}, kind="forbidden")

    def disabled_user() -> str:
        # 停用后旧 cookie 必须立刻失效
        from kedaibiao.security.auth import AuthStore

        store = AuthStore(h.db_path)
        try:
            user = store.get_user_by_name("normal")
            assert user is not None
            store.set_disabled(user.id, True)
        finally:
            store.close()
        resp = h.call("GET", "/api/overview", token=h.token_normal)
        return expect(resp, {401}, kind="unauthorized")

    for name, fn in [
        ("Host 伪造 evil.com → 403", host_forged),
        ("Host 正常（含端口）→ 200", host_ok),
        ("跨站 Origin → 403", origin_cross),
        ("Origin: null → 403", origin_null),
        ("同源 Origin → 200（正对照）", origin_same),
        ("路径穿越 /static/../../etc/passwd", traversal_raw),
        ("编码路径穿越 %2e%2e", traversal_encoded),
        ("路径穿越 /static/../api/server.py", traversal_parent),
        ("未登录 GET /api/overview → 401", unauth_overview),
        ("未登录 GET /api/users → 401（不是 403）", unauth_users),
        ("未登录 POST → 401", unauth_post),
        ("未登录也能打开登录页 /", public_login_page),
        ("普通用户 GET /api/users → 403", non_admin_users),
        ("普通用户 POST /api/settings/llm → 403", non_admin_llm),
        ("普通用户 GET /api/overview → 200", normal_user_overview),
        ("安全响应头齐备", headers_ok),
        ("POST 非 JSON Content-Type → 415", ctype_required),
        ("超大 body → 413", body_too_large),
        ("非法 JSON → 400", bad_json),
        ("错误密码 → 401", login_bad_password),
        ("登录 Cookie 标志", login_cookie_flags),
        ("伪造 Cookie → 401", forged_cookie),
        ("普通用户 ?owner_id=超管 → 403", owner_escalation),
        ("Origin 带 userinfo 的跨站值 → 403（已知守卫缺陷）", origin_userinfo),
        ("停用用户后旧 cookie → 401", disabled_user),
    ]:
        rep.check(name, fn)


# ---------------------------------------------------------------------------
# 4) keep-alive 回归
# ---------------------------------------------------------------------------


def section_keepalive(h: Harness, rep: Report) -> None:
    print("\n== 4. keep-alive 回归 ==")

    def post_with_body_then_get() -> str:
        """POST 的 body 必须被读走，否则下一请求会变成 '{}GET' → 501。"""
        conn = http.client.HTTPConnection("127.0.0.1", h.port, timeout=15)
        try:
            first = h.call(
                "POST", "/api/jobs/j-none/cancel",
                json_body={"ignored": True}, token=h.token_admin, conn=conn,
            )
            if first.status != 404:
                raise AssertionError(f"前置 POST 期望 404，实际 {first.status}")
            sock_after_first = conn.sock
            if sock_after_first is None:
                raise AssertionError("POST 之后连接被关闭，keep-alive 没生效")

            second = h.call("GET", "/api/overview", token=h.token_admin, conn=conn)
            if conn.sock is not sock_after_first:
                raise AssertionError("GET 用了新连接（socket 变了），本次没有验证到 keep-alive")
            expect(second, {200})
            return f"同一连接上 POST→GET：{first.status}→{second.status}"
        finally:
            conn.close()

    def post_to_body_route_then_get() -> str:
        """会解析 body 的 POST 之后，同一连接上也必须能继续。"""
        conn = http.client.HTTPConnection("127.0.0.1", h.port, timeout=15)
        try:
            first = h.call(
                "POST", "/api/settings/llm", json_body={}, token=h.token_admin, conn=conn,
            )
            expect(first, {200})
            sock = conn.sock
            second = h.call("GET", "/api/jobs", token=h.token_admin, conn=conn)
            if conn.sock is not sock:
                raise AssertionError("GET 用了新连接（socket 变了），本次没有验证到 keep-alive")
            expect(second, {200})
            return f"同一连接上 POST→GET：{first.status}→{second.status}"
        finally:
            conn.close()

    def oversize_then_get() -> str:
        """超大 body 必须被完整读掉，否则残留字节会污染同一连接上的下一个请求。"""
        conn = http.client.HTTPConnection("127.0.0.1", h.port, timeout=15)
        try:
            big = b'{"pad":"' + b"x" * (MAX_BODY + 4096) + b'"}'
            first = h.call(
                "POST", "/api/settings/llm", body=big,
                headers={"Content-Type": "application/json"},
                token=h.token_admin, conn=conn,
            )
            if first.status != 413:
                raise AssertionError(f"前置 POST 期望 413，实际 {first.status}")
            sock = conn.sock
            if sock is None:
                raise AssertionError("413 之后连接被关闭")
            second = h.call("GET", "/api/overview", token=h.token_admin, conn=conn)
            if conn.sock is not sock:
                raise AssertionError("GET 用了新连接（socket 变了）")
            expect(second, {200})
            return f"413 后同一连接 GET：{second.status}"
        finally:
            conn.close()

    def two_gets_one_connection() -> str:
        conn = http.client.HTTPConnection("127.0.0.1", h.port, timeout=15)
        try:
            first = h.call("GET", "/api/auth/me", token=h.token_admin, conn=conn)
            expect(first, {200})
            sock = conn.sock
            second = h.call("GET", "/api/platforms", token=h.token_admin, conn=conn)
            expect(second, {200})
            if conn.sock is not sock:
                raise AssertionError("第二个 GET 用了新连接")
            return "两次 GET 复用同一 socket"
        finally:
            conn.close()

    rep.check("带 body 的 POST（不解析 body）后接 GET", post_with_body_then_get)
    rep.check("带 body 的 POST（解析 body）后接 GET", post_to_body_route_then_get)
    rep.check("超大 body 413 后同连接 GET", oversize_then_get)
    rep.check("同一连接连续两次 GET", two_gets_one_connection)


# ---------------------------------------------------------------------------
# 5) 错误分支
# ---------------------------------------------------------------------------


def section_errors(h: Harness, rep: Report) -> None:
    print("\n== 5. 错误分支（正确 4xx，不是 500） ==")

    def limited() -> str:
        return expect(h.request("/api/runs?limit=abc"), {400}, kind="bad_request")

    def owner_bad() -> str:
        return expect(h.request("/api/overview?owner_id=abc"), {400}, kind="bad_request")

    def platform_param_ghost() -> str:
        return expect(h.request("/api/runs?platform=ghost"), {404}, kind="unknown_platform")

    def unknown_action() -> str:
        resp = h.request("/api/platforms/" + FAKE + "/actions/nope",
                         method="POST", json_body={})
        return expect(resp, {404}, kind="unknown_action")

    def missing_action_param() -> str:
        # essay 动作必填 rid：缺参数必须是 400 bad_request（不是 500/502）
        resp = h.request("/api/platforms/pigai/actions/essay",
                         method="POST", json_body={})
        return expect(resp, {400}, kind="bad_request")

    def job_missing() -> str:
        return expect(h.request("/api/jobs/no-such-job"), {404}, kind="not_found")

    def job_cancel_missing() -> str:
        resp = h.request("/api/jobs/no-such-job/cancel", method="POST", json_body={})
        return expect(resp, {404}, kind="not_found")

    def method_not_allowed() -> str:
        # 只 POST 的路由用 GET 打：当前实现是 404（不是 500），记录并锁住行为
        return expect(h.request("/api/run"), {404}, kind="not_found", route_known=False)

    def unknown_route_post() -> str:
        resp = h.request("/api/nope", method="POST", json_body={})
        return expect(resp, {404}, kind="not_found", route_known=False)

    for name, fn in [
        ("GET /api/runs?limit=abc → 400", limited),
        ("GET /api/overview?owner_id=abc → 400", owner_bad),
        ("?platform=ghost → 404 unknown_platform", platform_param_ghost),
        ("未知动作 → 404 unknown_action", unknown_action),
        ("动作缺必填参数 → 400", missing_action_param),
        ("不存在的 job → 404 not_found", job_missing),
        ("取消不存在的 job → 404 not_found", job_cancel_missing),
        ("GET 只 POST 的路由 → 404（不是 500）", method_not_allowed),
        ("POST 未知路由 → 404", unknown_route_post),
    ]:
        rep.check(name, fn)


# ---------------------------------------------------------------------------
# 6) 并发
# ---------------------------------------------------------------------------


def section_concurrency(h: Harness, rep: Report) -> None:
    print("\n== 6. 并发（8 线程） ==")

    def run_parallel(paths: list[str], workers: int = 8) -> str:
        results: list[tuple[str, int, str]] = []
        lock = threading.Lock()

        def one(path: str) -> None:
            try:
                resp = h.request(path)
                item = (path, resp.status, resp.kind)
            except Exception as exc:  # noqa: BLE001 - 线程里任何异常都要带回主线程
                item = (path, -1, f"{type(exc).__name__}: {exc}")
            with lock:
                results.append(item)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(one, paths * workers))
        bad = [r for r in results if r[1] != 200]
        if bad:
            raise AssertionError(f"{len(bad)}/{len(results)} 个请求不是 200：{bad[:4]}")
        return f"{len(results)} 个请求全部 200，无 500"

    def overview_parallel() -> str:
        return run_parallel(["/api/overview"])

    def jobs_parallel() -> str:
        return run_parallel(["/api/jobs"])

    def mixed_parallel() -> str:
        return run_parallel(["/api/overview", "/api/platforms", "/api/jobs", "/api/bank"])

    rep.check("8 线程 × /api/overview", overview_parallel)
    rep.check("8 线程 × /api/jobs", jobs_parallel)
    rep.check("8 线程混合只读接口", mixed_parallel)


# ---------------------------------------------------------------------------
# 7) 静态资源
# ---------------------------------------------------------------------------


def section_static(h: Harness, rep: Report) -> None:
    print("\n== 7. 静态资源（v2 在 /，v1 在 /classic） ==")

    def content_type(path: str, needle: str) -> tuple[Resp, str]:
        resp = h.request(path)
        expect(resp, {200})
        ctype = resp.headers.get("content-type", "")
        if needle not in ctype:
            raise AssertionError(f"{path} Content-Type={ctype!r}，期望含 {needle!r}")
        return resp, ctype

    def root_is_v2() -> str:
        resp, ctype = content_type("/", "text/html")
        if b"/static/next/app.js" not in resp.body:
            raise AssertionError("/ 没有引用 /static/next/app.js（看起来不是 v2）")
        return "v2 外壳"

    def classic_is_v1() -> str:
        resp, ctype = content_type("/classic", "text/html")
        if b'id="page-overview"' not in resp.body:
            raise AssertionError("/classic 里没有 v1 的 id=\"page-overview\"")
        if b"/static/app.js" not in resp.body:
            raise AssertionError("/classic 没有引用 /static/app.js（看起来不是 v1）")
        return "v1 外壳"

    def next_js() -> str:
        _, ctype = content_type("/static/next/app.js", "javascript")
        return ctype

    def next_css() -> str:
        _, ctype = content_type("/static/next/app.css", "text/css")
        return ctype

    def v1_js() -> str:
        _, ctype = content_type("/static/app.js", "javascript")
        return ctype

    def v1_html_direct() -> str:
        _, ctype = content_type("/static/v1.html", "text/html")
        return ctype

    def static_404() -> str:
        resp = h.request("/static/next/missing-file.js")
        return expect(resp, {404}, kind="not_found")

    for name, fn in [
        ("GET / → v2 外壳", root_is_v2),
        ("GET /classic → v1 外壳", classic_is_v1),
        ("GET /static/next/app.js → JS", next_js),
        ("GET /static/next/app.css → CSS", next_css),
        ("GET /static/app.js → JS（v1）", v1_js),
        ("GET /static/v1.html → HTML", v1_html_direct),
        ("GET /static/next/missing-file.js → 404", static_404),
    ]:
        rep.check(name, fn)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(description="后端接口矩阵自检（离线、临时库）").parse_args(argv)

    print("启动临时控制台（随机端口，临时库）…")
    harness = Harness()
    print(f"  root={harness.root}")
    print(f"  http://127.0.0.1:{harness.port}  （不是 {LIVE_PORT}，绝不连接正在用的控制台）")
    report = Report()
    try:
        section_routes(harness, report)
        section_route_sync(harness, report)
        section_security(harness, report)
        section_keepalive(harness, report)
        section_errors(harness, report)
        section_concurrency(harness, report)
        section_static(harness, report)

        # 观察项：已知不一致，不计入通过率
        ghost_history = harness.request(
            "/api/essays/history", method="POST",
            json_body={"assignment_id": "r1", "platform": "ghost"},
        )
        if ghost_history.status == 200:
            report.info("POST /api/essays/history 不校验 platform：传 ghost 也返回 200")

        odd_methods = []
        for method in ("DELETE", "PUT", "PATCH", "OPTIONS"):
            resp = harness.call(method, "/api/overview", token=harness.token_admin)
            if resp.status >= 500:
                odd_methods.append(f"{method}→{resp.status}")
        if odd_methods:
            report.info(
                "非 GET/POST 方法没有被路由处理（" + "、".join(odd_methods) + "，"
                "http.server 默认的 HTML 501）：不走 {error,kind} 契约、也不带安全响应头；"
                "如需严格对齐契约，建议在 ConsoleHandler 里统一改成 405 JSON"
            )
    finally:
        harness.close()

    return report.summary()


if __name__ == "__main__":
    sys.exit(main())
