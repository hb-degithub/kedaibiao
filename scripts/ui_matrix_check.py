#!/usr/bin/env python3
"""控制台 v2 的「页面 × 平台」矩阵 + 三态 + 响应式回归检查。

为什么单开一个脚本而不改 ui_smoke_next.py：那个脚本按业务流走（刷课 → 树 → 干跑 →
日志），覆盖面集中在两条主动线上；这里要的是**横向扫全表**：12 个页面 × 6 个平台
（全部平台 + 5 个适配器）共 72 个组合，每个组合都要求「不炸、标题对、左轨高亮对、
不发 4xx/5xx」。两者关注点不同，互相 import 只会让改一个脚本时另一个跟着红。

铁律：
  * **只读**。脚本不点任何写按钮（提交 / 刷课 / 签到 / 删号 / 一键自动化），
    平台调用类 POST 一律由 Playwright 路由拦截成 mock，不会真的打到平台。
  * **不自己起服务**。--base 收地址；连不上就提示先起一个临时实例，退出码 2。
  * 建议对着一份**临时 root** 跑，别对着你日常用的那个控制台：
        mkdir -p /tmp/kdb-ui-matrix && cp -r config /tmp/kdb-ui-matrix/
        cd /tmp/kdb-ui-matrix && PYTHONPATH=/path/to/src \\
            python -m kedaibiao web --root /tmp/kdb-ui-matrix --port 8811
        # 首次启动会打印一次 admin 密码
        python3 scripts/ui_matrix_check.py --base http://127.0.0.1:8811 \\
            --username admin --password '……'

覆盖：
  1. 72 个「页面 × 平台」组合：无 pageerror / console error / 4xx/5xx（favicon 除外）、
     标题正确、左轨 aria-current 高亮正确、能力不足的页面按约定退回 overview。
  2. 三态：7 个数据页「接口 500 → 错误块且不崩」；assignments / attendance「空数据 →
     空状态而不是空白表」。
  3. 交互：⌘K 命令面板（开 / 过滤 / 回车跳转 / Esc 关）、检查器（行点开 / Esc / backdrop）、
     顶栏账号 chip（下拉 / 详情进检查器）、主题切换、左轨在 ≤1080px 折叠成图标条。
  4. 响应式：1440 / 1280 / 1024 / 768 / 414 五档 + 无横向溢出断言。
  5. 未登录：登录门可见、/api/overview 返回 401。

用法：
    python3 scripts/ui_matrix_check.py --base http://127.0.0.1:8811 \\
        --username admin --password '……'            # 或 --token <kdb_session>
    python3 scripts/ui_matrix_check.py --base ... --token ... --shots /tmp/kdb-matrix-shots

退出码：0 = 全绿；1 = 有失败项；2 = 环境问题（连不上 / 登录失败 / 缺 playwright）。
"""

from __future__ import annotations

import argparse
import json
import sys
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from urllib.parse import urlparse

# ---------------------------------------------------------------- 常量

#: 页面顺序与左轨/标题上显示的中文名（必须和 next/app.js 的 PAGES 一致）。
PAGES: list[tuple[str, str]] = [
    ("overview", "概览"),
    ("tasks", "任务"),
    ("courses", "课程"),
    ("assignments", "作业"),
    ("attendance", "考勤"),
    ("bank", "题库"),
    ("logs", "运行日志"),
    ("stats", "统计"),
    ("accounts", "账号"),
    ("tools", "工具"),
    ("roadmap", "计划"),
    ("users", "用户"),
]
PAGE_LABEL = dict(PAGES)

#: None = 全部平台（hash 里不写平台段）。
PLATFORMS: list[str | None] = [None, "itest", "neusoft", "pigai", "pinco", "unipus"]
PLATFORM_LABEL = {
    None: "全部平台",
    "itest": "iTEST",
    "neusoft": "东软",
    "pigai": "批改网",
    "pinco": "品课",
    "unipus": "U校园",
}

#: 页面可用性规则 —— 直接对应 next/app.js 的 pageAvailable()：
#:   requiresCapability → 平台 capabilities 里该键为真；「全部平台」下照常显示（给 gate）
#:   requiresAnyAction → 平台动作清单里出现任一动作；「全部平台」下不可用（退回概览）
#:   admin            → 仅超管
PAGE_RULES: dict[str, dict] = {
    "courses": {"capability": "list_courses"},
    "assignments": {"capability": "list_assignments"},
    "attendance": {"any_action": ("attendance", "signin")},
    "users": {"admin": True},
}

#: 约定好的「应该退回概览」组合。平台注册表变了、这份常量对不上时，脚本会在
#: 启动自检阶段直接报错，而不是悄悄放宽断言。
KNOWN_FALLBACKS = {
    ("attendance", None),
    ("attendance", "itest"),
    ("attendance", "pigai"),
    ("attendance", "unipus"),
}

#: 三态测试：页面 → (平台, 要被注入 500 的路径片段, 该页主接口)。
STATE_500_CASES: list[tuple[str, str, str]] = [
    ("courses", "unipus", "/actions/courses"),
    ("assignments", "unipus", "/actions/assignments"),
    ("attendance", "neusoft", "/actions/attendance"),
    ("bank", "unipus", "/api/bank"),
    ("tools", "unipus", "/api/settings/llm"),
    ("logs", "unipus", "/api/runs"),
    ("stats", "unipus", "/api/statistics"),
]

#: 空数据用例：(页面, 平台, 期望在空状态里出现的文案)
STATE_EMPTY_CASES: list[tuple[str, str, str]] = [
    ("assignments", "unipus", "没有数据"),
    ("attendance", "neusoft", "这个学期没有考勤场次"),
]

VIEWPORTS = [1440, 1280, 1024, 768, 414]

#: 平台动作 mock 里默认给的空信封（0 行也算合法返回，页面会走空状态）。
EMPTY_TABLE = {
    "kind": "table",
    "title": "mock",
    "columns": [{"key": "title", "label": "名称"}, {"key": "status", "label": "状态"}],
    "rows": [],
}

#: 交互测试用的「有数据」信封。
RICH_ROWS = [
    {"id": "a1", "title": "作业 A · 单元测试", "status": "待处理", "course": "c1"},
    {"id": "a2", "title": "作业 B · 写作", "status": "succeeded", "course": "c1"},
]

RICH_DETAIL = {
    "platform": "unipus",
    "kind": "detail",
    "title": "作业详情",
    "fields": [
        {"label": "作业号", "value": "a1"},
        {"label": "状态", "value": "待处理"},
    ],
    "sections": [],
}

ACCOUNT_FIXTURE = [
    {
        "id": 7,
        "platform": "unipus",
        "label": "测试号",
        "username": "u_test",
        "is_active": True,
        "last_login_ok": True,
        "last_login_at": "2026-10-20T10:00:00",
        "last_login_note": "",
        "created_at": "2026-10-19T09:00:00",
    }
]


#: 已知噪声：浏览器自己产生的、跟页面代码无关的 console error。
#: `frame-ancestors` 放在 <meta> 的 CSP 里按规范会被忽略，Chrome 每次都提醒一句 ——
#: 这条不是回归，但会在「无 console error」这条里误报，所以按子串忽略。
NOISE_SUBSTRINGS = (
    "frame-ancestors",
)


def is_noise(text: str) -> bool:
    return any(frag in text for frag in NOISE_SUBSTRINGS)


# ---------------------------------------------------------------- 小工具


def display_width(text: str) -> int:
    """终端列宽（东亚宽字符算 2 列），让矩阵表在中文下也对齐。"""
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in text)


def pad(text: str, width: int) -> str:
    return text + " " * max(0, width - display_width(text))


def http_json(base: str, path: str, *, method: str = "GET",
              body: dict | None = None, token: str | None = None
              ) -> tuple[int, dict | None]:
    """极简 JSON 请求。只用于「服务可达性 / 登录 / 401 断言」，不参与页面渲染。"""
    req = urllib.request.Request(base + path, method=method)
    if token:
        req.add_header("Cookie", "kdb_session=" + token)
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data=data, timeout=8) as resp:
            raw = resp.read()
            try:
                return resp.status, json.loads(raw or b"null")
            except ValueError:
                return resp.status, None
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            parsed = json.loads(raw or b"null")
        except ValueError:
            parsed = None
        return exc.code, parsed


@dataclass
class Result:
    """一条检查结论。fail 记录同时留存「期望 / 实际 / 控制台原文」。"""

    name: str
    ok: bool
    detail: str = ""

    def fmt(self) -> str:
        if self.ok:
            return f"  PASS  {self.name}"
        return f"  FAIL  {self.name}  —— {self.detail}"


@dataclass
class Recorder:
    """把一次页面访问期间的所有噪声收集起来，供组合判定。"""

    pageerrors: list[str] = field(default_factory=list)
    console: list[str] = field(default_factory=list)
    bad: list[str] = field(default_factory=list)

    def reset(self) -> None:
        self.pageerrors.clear()
        self.console.clear()
        self.bad.clear()

    def summary(self) -> str:
        parts = []
        if self.pageerrors:
            parts.append("pageerror: " + self.pageerrors[0])
        if self.console:
            parts.append("console: " + self.console[0])
        if self.bad:
            parts.append("HTTP: " + ", ".join(self.bad[:3]))
        return " / ".join(parts)


class MockLayer:
    """平台调用类接口的 mock 层。

    只挂一个 ``**/api/**`` 路由，其余请求 ``route.fallback()`` 交给真服务。
    这样既能 mock 平台动作，又能临时注入 500 / 账号样本，而不用给每个接口写一遍。
    """

    def __init__(self) -> None:
        self.fail_substrings: list[str] = []
        self.accounts: list[dict] | None = None
        self.action_rows: list[dict] = []
        self.action_body: dict | None = None
        self.detail_body: dict | None = None

    # -- 测试期开关 -----------------------------------------------------

    def reset(self) -> None:
        self.fail_substrings = []
        self.accounts = None
        self.action_rows = []
        self.action_body = None
        self.detail_body = None

    def install(self, page) -> None:  # noqa: ANN001 - playwright Page
        page.route("**/api/**", self._handle)

    # -- 路由处理 -------------------------------------------------------

    def _handle(self, route) -> None:  # noqa: ANN001 - playwright Route
        request = route.request
        path = urlparse(request.url).path

        if any(frag in path for frag in self.fail_substrings):
            return route.fulfill(
                status=500,
                content_type="application/json",
                body=json.dumps({
                    "error": "这是 ui_matrix_check 注入的 500（接口故障演练）",
                    "kind": "internal",
                    "detail": "mocked by scripts/ui_matrix_check.py",
                }),
            )

        if path == "/api/accounts" and self.accounts is not None:
            return route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"platform": None, "owner_id": None,
                                 "accounts": self.accounts}),
            )

        if path == "/api/assignments/detail" and self.detail_body is not None:
            return route.fulfill(status=200, content_type="application/json",
                                 body=json.dumps(self.detail_body))

        # POST /api/platforms/<name>/actions/<action> —— 平台调用，一律 mock。
        # 注意别误伤 GET /api/platforms/<name>/actions（动作清单）：那一条是只读的、
        # 真接口，页面用它决定「考勤页在这个平台存不存在」。
        if request.method == "POST" and "/actions/" in path:
            platform = path.split("/")[3] if path.count("/") >= 5 else ""
            payload = self.action_body or dict(EMPTY_TABLE, platform=platform,
                                               rows=self.action_rows)
            return route.fulfill(status=200, content_type="application/json",
                                 body=json.dumps(payload))

        return route.fallback()


# ---------------------------------------------------------------- 期望值


def expected_page(name: str, platform: str | None, platforms: dict[str, dict],
                  actions: dict[str, list[str]], is_super: bool) -> str:
    """按「后端真实能力」推算这一页在该平台下会不会退回概览。

    刻意不复刻前端的实现细节，而是从 ``/api/platforms`` + ``/api/platforms/<n>/actions``
    的真实返回推：页面 X 在平台 P 可用 ⟺ P 声明了 X 需要的能力/动作。
    """
    rule = PAGE_RULES.get(name)
    if not rule:
        return name
    if rule.get("admin") and not is_super:
        return "overview"
    cap = rule.get("capability")
    if cap:
        if platform and not (platforms.get(platform, {}).get("capabilities") or {}).get(cap):
            return "overview"
        return name
    wanted = rule.get("any_action")
    if wanted:
        if not platform:
            return "overview"
        have = actions.get(platform) or []
        return name if any(w in have for w in wanted) else "overview"
    return name


# ---------------------------------------------------------------- 环境


def preflight(base: str, args) -> tuple[str, dict, dict, bool]:
    """确认服务可达 + 拿到登录态 + 读平台/动作清单。任何一步失败都退出码 2。"""
    try:
        status, body = http_json(base, "/api/auth/me")
    except (urllib.error.URLError, OSError) as exc:
        print(f"✗ 连不上 {base}：{exc}")
        print("  先起一个临时实例（不要用你日常那个）：")
        print("    mkdir -p /tmp/kdb-ui-matrix && cp -r config /tmp/kdb-ui-matrix/")
        print("    cd /tmp/kdb-ui-matrix && PYTHONPATH=<仓库>/src \\")
        print("        python3 -m kedaibiao web --root /tmp/kdb-ui-matrix --port 8811")
        raise SystemExit(2)
    if status != 200:
        print(f"✗ {base}/api/auth/me 返回 {status}，这不像控制台服务。")
        raise SystemExit(2)

    token = args.token or ""
    if not token:
        if not args.username or not args.password:
            print("✗ 需要 --token，或 --username + --password 登录一次拿会话。")
            raise SystemExit(2)
        # 登录响应体只回 user，会话在 Set-Cookie 里 —— urllib 拿不到，用 http.client。
        token, login_error = login_cookie(base, args.username, args.password)
        if not token:
            print(f"✗ 登录失败：{login_error}")
            raise SystemExit(2)

    status, me = http_json(base, "/api/auth/me", token=token)
    if status != 200 or not (me or {}).get("user"):
        print("✗ 会话无效（cookie kdb_session 可能已过期），换 --token 或重新登录。")
        raise SystemExit(2)
    is_super = bool((me or {}).get("is_superadmin"))

    status, body = http_json(base, "/api/platforms", token=token)
    if status != 200 or not isinstance(body, dict):
        print(f"✗ /api/platforms 返回 {status}")
        raise SystemExit(2)
    platforms = {p["name"]: p for p in body.get("platforms") or []}

    actions: dict[str, list[str]] = {}
    for name in platforms:
        status, body = http_json(base, f"/api/platforms/{name}/actions", token=token)
        actions[name] = [a["name"] for a in (body or {}).get("actions") or []] \
            if status == 200 else []

    return token, platforms, actions, is_super


def login_cookie(base: str, username: str, password: str) -> tuple[str, str]:
    """登录并取出 Set-Cookie 里的 kdb_session。

    urllib 不会把响应头的 cookie 交出来，所以这里直接用 ``http.client`` ——
    返回 ``(token, error)``，token 为空时 error 是要打印的原因。
    """
    import http.client

    parsed = urlparse(base)
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port or 80, timeout=8)
    try:
        body = json.dumps({"username": username, "password": password})
        conn.request("POST", "/api/auth/login", body=body,
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        raw = resp.read()
        if resp.status != 200:
            try:
                reason = json.loads(raw or b"{}").get("error", "")
            except ValueError:
                reason = ""
            return "", f"HTTP {resp.status} {reason}".strip()
        for header, value in resp.getheaders():
            if header.lower() == "set-cookie" and value.startswith("kdb_session="):
                return value.split(";", 1)[0].split("=", 1)[1], ""
        return "", "登录返回 200 但没有下发 kdb_session cookie"
    except OSError as exc:
        return "", f"{type(exc).__name__}: {exc}"
    finally:
        conn.close()


# ---------------------------------------------------------------- 浏览器侧


class Matrix:
    def __init__(self, base: str, page, mock: MockLayer, rec: Recorder,
                 platforms: dict, actions: dict, is_super: bool, timeout: int,
                 shots: str | None, fallbacks: set) -> None:
        self.base = base.rstrip("/")
        self.fallbacks = fallbacks
        self.page = page
        self.mock = mock
        self.rec = rec
        self.platforms = platforms
        self.actions = actions
        self.is_super = is_super
        self.timeout = timeout
        self.shots = shots
        self.results: list[Result] = []

    # -- 基础 -----------------------------------------------------------

    def url_for(self, name: str, platform: str | None) -> str:
        return f"{self.base}/#/{name}" + (f"/{platform}" if platform else "")

    def goto(self, name: str, platform: str | None) -> None:
        self.rec.reset()
        self.page.goto(self.url_for(name, platform), wait_until="domcontentloaded")

    def goto_fresh(self, name: str, platform: str | None) -> None:
        """强制真的重载一次文档。

        只改 hash 是 same-document 导航，``boot()/enterApp()`` 不会再跑 ——
        而「左轨在 ≤1080px 折叠」正是 enterApp 里按窗口宽度加 class 的，
        不重载就会永远量到上一次的宽度，测不出东西。
        """
        self.goto(name, platform)
        self.page.reload(wait_until="domcontentloaded")

    def ensure_closed(self) -> None:
        """把可能残留的浮层关掉：上一条用例失败时最容易留下一个开着的检查器，
        它的 backdrop 会盖住整页，让后面所有点击都超时（假失败）。"""
        try:
            self.page.keyboard.press("Escape")
        except Exception:  # noqa: BLE001 - 关不上不影响后续判定
            pass
        self.page.evaluate(
            """() => {
                 const ins = document.getElementById('inspector');
                 const pal = document.getElementById('palette');
                 const menu = document.getElementById('acct-menu');
                 if (ins) ins.hidden = true;
                 if (pal) pal.hidden = true;
                 if (menu) menu.hidden = true;
               }"""
        )

    def wait_page(self, label: str) -> None:
        """等到「标题 = 课代表 · <label>」且左轨该项 aria-current —— 两个都由 renderPage 写。"""
        self.page.wait_for_function(
            """(lbl) => {
                 if (document.title !== '课代表 · ' + lbl) return false;
                 const cur = document.querySelector('#rail [aria-current="page"]');
                 if (!cur) return false;
                 const text = cur.querySelector('.lbl');
                 return !!(text && text.textContent.trim() === lbl);
               }""",
            arg=label,
            timeout=self.timeout,
        )

    def actual_state(self) -> str:
        state = self.page.evaluate(
            """() => {
                 const cur = document.querySelector('#rail [aria-current="page"] .lbl');
                 return {
                   title: document.title,
                   hash: location.hash,
                   rail: cur ? cur.textContent.trim() : '',
                 };
               }"""
        )
        return (f"title={state['title']!r} hash={state['hash']!r} "
                f"rail={state['rail']!r}")

    # -- 1. 矩阵 ---------------------------------------------------------

    def run_matrix(self) -> None:
        print("\n=== 1. 页面 × 平台矩阵（12 × 6 = 72）===")
        rows: list[tuple[str, str, str]] = []
        for name, _label in PAGES:
            for platform in PLATFORMS:
                want = expected_page(name, platform, self.platforms,
                                     self.actions, self.is_super)
                name_tag = f"{name}/{platform or '全部平台'}"
                self.goto(name, platform)
                reason = ""
                ok = True
                try:
                    self.wait_page(PAGE_LABEL[want])
                    # 再稳一拍：让页面里第二跳请求（列表 / 状态回读）有机会出错
                    self.page.wait_for_timeout(220)
                except Exception as exc:  # noqa: BLE001 - 超时 = 页面没到预期
                    ok = False
                    reason = (f"期望 {want}（{PAGE_LABEL[want]}），实际 "
                              f"{self.actual_state()}；等待失败：{type(exc).__name__}")
                if ok:
                    noise = self.rec.summary()
                    if noise:
                        ok = False
                        reason = noise
                if ok and want != name:
                    # 退回概览必须是「约定好的」那几组，不能是意外
                    if (name, platform) not in self.fallbacks:
                        ok = False
                        reason = f"意外退回 overview：{name}/{platform} 不在 KNOWN_FALLBACKS"
                if ok and want == name and (name, platform) in self.fallbacks:
                    ok = False
                    reason = "该退回 overview 的组合没有退回"
                self.results.append(Result(name_tag, ok, reason))
                rows.append((name, platform or "全部平台", "PASS" if ok else "FAIL"))

        w0 = max(display_width(r[0]) for r in rows)
        w1 = max(display_width(r[1]) for r in rows)
        for a, b, c in rows:
            print(f"  {pad(a, w0)}  {pad(b, w1)}  {c}")

    # -- 2. 三态 ---------------------------------------------------------

    def run_states(self) -> None:
        print("\n=== 2. 三态（接口 500 / 空数据）===")

        for name, platform, frag in STATE_500_CASES:
            self.mock.reset()
            self.mock.fail_substrings = [frag]
            self.goto(name, platform)
            ok, reason = True, ""
            try:
                self.page.wait_for_selector("#main .page", timeout=self.timeout)
                # 失败要落成可见的错误块，而不是骨架屏一直转 / 白屏
                self.page.wait_for_selector("#main .state.state-error",
                                            timeout=self.timeout)
                self.page.wait_for_timeout(150)
                text = self.page.inner_text("#main")
                if "mocked" not in text and "没取到数据" not in text:
                    ok, reason = False, f"错误块文案不对：{text[:120]!r}"
                if self.page.locator("#main .skel").count():
                    ok, reason = False, "错误块旁边还留着骨架屏（可能有一次请求没被 catch）"
            except Exception as exc:  # noqa: BLE001
                ok, reason = False, (f"{type(exc).__name__}: 没有出现 .state.state-error；"
                                     f"实际 {self.actual_state()}")
            # 关键：接口挂了，框架本身不能崩 —— 左轨还在、页面还在
            if ok and not self.page.locator("#rail .nav-item").count():
                ok, reason = False, "左轨为空：一次接口 500 把整个壳子打挂了"
            self.mock.reset()
            self.results.append(Result(f"500/{name}@{platform}", ok, reason))

        for name, platform, expect_text in STATE_EMPTY_CASES:
            self.mock.reset()
            self.mock.action_rows = []
            self.goto(name, platform)
            ok, reason = True, ""
            try:
                self.page.wait_for_selector("#main .page", timeout=self.timeout)
                self.page.wait_for_selector("#main .state", timeout=self.timeout)
                self.page.wait_for_timeout(150)
                text = self.page.inner_text("#main")
                if expect_text not in text:
                    ok, reason = False, f"空状态文案里没有 {expect_text!r}：{text[:120]!r}"
                if self.page.locator("#main table.tbl tbody tr").count():
                    ok, reason = False, "空数据却渲染出了数据行"
            except Exception as exc:  # noqa: BLE001
                ok, reason = False, f"{type(exc).__name__}: 没有空状态；实际 {self.actual_state()}"
            self.mock.reset()
            self.results.append(Result(f"空态/{name}@{platform}", ok, reason))

    # -- 3. 交互 ---------------------------------------------------------

    def run_interactions(self) -> None:
        print("\n=== 3. 交互（命令面板 / 检查器 / 账号 chip / 主题 / 左轨折叠）===")
        self._interaction_palette()
        self._interaction_inspector()
        self._interaction_account_chip()
        self._interaction_theme()
        self._interaction_rail_collapse()

    def _add(self, name: str, ok: bool, reason: str = "") -> None:
        self.results.append(Result(name, ok, reason))

    def _interaction_palette(self) -> None:
        self.mock.reset()
        self.rec.reset()
        self.ensure_closed()
        self.goto("overview", "unipus")
        ok, reason = True, ""
        try:
            self.wait_page("概览")
            self.page.keyboard.press("Control+k")
            self.page.wait_for_selector("#palette:not([hidden])", timeout=self.timeout)
            self.page.fill("#palette-input", "工具")
            self.page.wait_for_timeout(120)
            items = self.page.locator("#palette-list .palette-item, "
                                      "#palette-list button, #palette-list .menu-row")
            labels = [t.strip() for t in items.all_inner_texts()]
            if not any("工具" in t for t in labels):
                ok, reason = False, f"输入「工具」后没过滤出该项：{labels[:5]}"
            if ok:
                self.page.keyboard.press("Enter")
                self.wait_page("工具")
                if "#/tools" not in self.page.evaluate("() => location.hash"):
                    ok, reason = False, f"回车没跳到工具页：{self.actual_state()}"
            if ok:
                self.page.keyboard.press("Control+k")
                self.page.wait_for_selector("#palette:not([hidden])", timeout=self.timeout)
                self.page.keyboard.press("Escape")
                self.page.wait_for_timeout(120)
                if not self.page.evaluate("() => document.getElementById('palette').hidden"):
                    ok, reason = False, "Esc 没有关掉命令面板"
        except Exception as exc:  # noqa: BLE001
            ok, reason = False, f"{type(exc).__name__}: {self.actual_state()}"
        if ok and self.rec.summary():
            ok, reason = False, self.rec.summary()
        self._add("交互/命令面板（⌘K · 过滤 · 回车 · Esc）", ok, reason)

    def _interaction_inspector(self) -> None:
        self.mock.reset()
        self.mock.action_rows = list(RICH_ROWS)
        self.mock.detail_body = RICH_DETAIL
        self.rec.reset()
        self.ensure_closed()
        self.goto("assignments", "unipus")
        ok, reason = True, ""
        try:
            self.wait_page("作业")
            row = self.page.locator("#main table.tbl tbody tr.row-click").first
            row.wait_for(timeout=self.timeout)
            row.click()
            self.page.wait_for_selector("#inspector:not([hidden])", timeout=self.timeout)
            # 检查器先开在「行标题」上，详情请求回来后才换成 env.title —— 等后者，
            # 否则量到的是中间态。
            self.page.wait_for_function(
                "() => document.getElementById('inspector-title')"
                ".textContent.indexOf('详情') >= 0", timeout=self.timeout)
            title = self.page.inner_text("#inspector-title")
            if "详情" not in title:
                ok, reason = False, f"检查器标题不对：{title!r}"
            if ok:
                self.page.keyboard.press("Escape")
                self.page.wait_for_timeout(120)
                if not self.page.evaluate("() => document.getElementById('inspector').hidden"):
                    ok, reason = False, "Esc 没有关掉检查器"
            if ok:
                row.click()
                self.page.wait_for_selector("#inspector:not([hidden])", timeout=self.timeout)
                # backdrop 铺满全屏、抽屉压在右侧（width 720）：默认点「元素中心」会正好
                # 落在抽屉左边缘上，所以按相对坐标点左侧空白区，模拟真实用户点遮罩。
                self.page.locator(".inspector-backdrop").click(
                    position={"x": 40, "y": 300})
                self.page.wait_for_timeout(120)
                if not self.page.evaluate("() => document.getElementById('inspector').hidden"):
                    ok, reason = False, "点 backdrop 没有关掉检查器"
        except Exception as exc:  # noqa: BLE001
            ok, reason = False, f"{type(exc).__name__}: {self.actual_state()}"
        if ok and self.rec.summary():
            ok, reason = False, self.rec.summary()
        self.mock.reset()
        self.ensure_closed()
        self._add("交互/检查器（行点开 · Esc · backdrop）", ok, reason)

    def _interaction_account_chip(self) -> None:
        self.mock.reset()
        self.mock.accounts = list(ACCOUNT_FIXTURE)
        self.rec.reset()
        self.ensure_closed()
        self.goto("accounts", "unipus")
        ok, reason = True, ""
        try:
            self.wait_page("账号")
            chip = self.page.locator("#acct-info")
            chip.wait_for(state="visible", timeout=self.timeout)
            chip.click()
            self.page.wait_for_selector("#acct-menu:not([hidden])", timeout=self.timeout)
            self.page.click("#acct-menu button:has-text('详情')")
            self.page.wait_for_selector("#inspector:not([hidden])", timeout=self.timeout)
            title = self.page.inner_text("#inspector-title")
            if "账号" not in title:
                ok, reason = False, f"账号详情没进检查器：{title!r}"
            if ok:
                self.page.keyboard.press("Escape")
                self.page.wait_for_timeout(120)
        except Exception as exc:  # noqa: BLE001
            ok, reason = False, f"{type(exc).__name__}: {self.actual_state()}"
        if ok and self.rec.summary():
            ok, reason = False, self.rec.summary()
        self.mock.reset()
        self.ensure_closed()
        self._add("交互/顶栏账号 chip（下拉 · 详情进检查器）", ok, reason)

    def _interaction_theme(self) -> None:
        self.mock.reset()
        self.rec.reset()
        self.ensure_closed()
        self.goto("overview", "unipus")
        ok, reason = True, ""
        try:
            self.wait_page("概览")
            before = self.page.evaluate(
                "() => document.documentElement.getAttribute('data-theme')")
            self.page.click("#theme-btn")
            self.page.wait_for_timeout(120)
            after = self.page.evaluate(
                "() => document.documentElement.getAttribute('data-theme')")
            if before == after:
                ok, reason = False, f"点主题按钮后 data-theme 没变（{before!r}）"
            if ok and after not in ("dark", "light"):
                ok, reason = False, f"data-theme 取值非法：{after!r}"
            if ok:
                self.page.click("#theme-btn")
                self.page.wait_for_timeout(120)
                back = self.page.evaluate(
                    "() => document.documentElement.getAttribute('data-theme')")
                if back != before:
                    ok, reason = False, f"再点一次没切回 {before!r}（得到 {back!r}）"
        except Exception as exc:  # noqa: BLE001
            ok, reason = False, f"{type(exc).__name__}: {self.actual_state()}"
        if ok and self.rec.summary():
            ok, reason = False, self.rec.summary()
        self._add("交互/主题切换（dark ↔ light）", ok, reason)

    def _interaction_rail_collapse(self) -> None:
        self.mock.reset()
        self.rec.reset()
        ok, reason = True, ""
        try:
            self.ensure_closed()
            self.page.set_viewport_size({"width": 1024, "height": 900})
            self.goto_fresh("overview", "unipus")
            self.wait_page("概览")
            narrow = self.page.evaluate(
                "() => document.getElementById('app').classList.contains('rail-min')")
            self.page.set_viewport_size({"width": 1440, "height": 900})
            self.goto_fresh("overview", "unipus")
            self.wait_page("概览")
            wide = self.page.evaluate(
                "() => document.getElementById('app').classList.contains('rail-min')")
            if not narrow:
                ok, reason = False, "1024px 下 #app 没有 rail-min（左轨没折叠成图标条）"
            elif wide:
                ok, reason = False, "1440px 下 #app 仍带 rail-min（不该折叠）"
        except Exception as exc:  # noqa: BLE001
            ok, reason = False, f"{type(exc).__name__}"
        if ok and self.rec.summary():
            ok, reason = False, self.rec.summary()
        self._add("交互/左轨折叠（≤1080px → rail-min）", ok, reason)

    # -- 3.5 手机端适配 ---------------------------------------------------

    def run_mobile(self) -> None:
        """手机端专项：底部导航要有文字、顶栏收进「更多」、表格不被压扁。"""
        print("\n=== 3.5 手机端适配 ===")
        self.mock.reset()
        self.rec.reset()
        self.ensure_closed()
        self.page.set_viewport_size({"width": 390, "height": 844})
        self.goto_fresh("overview", None)
        self.page.wait_for_timeout(600)

        labels = [t.strip() for t in self.page.locator(".nav-item .lbl").all_inner_texts()]
        visible = [t for t in labels if t]
        self._add("手机端/底部导航显示文字标签", len(visible) >= 8, f"可见 {len(visible)} 个")

        ok = self.page.is_visible("#more-btn")
        self._add("手机端/顶栏有「更多」按钮", ok, "" if ok else "不可见")
        if ok:
            self.page.click("#more-btn")
            self.page.wait_for_timeout(350)
            n = self.page.locator("#more-menu .rowx-title").count()
            self._add("手机端/「更多」菜单有主题·刷新·退出", n >= 3, f"{n} 项")
            self.page.keyboard.press("Escape")

        # 表格不能被压成「每列一个字」：容器要能横滑、首列要有合理宽度。
        # 必须给「有数据」的 mock —— 空表时页面走空状态，根本没有 .tbl 可量。
        self.mock.reset()
        self.mock.action_rows = list(RICH_ROWS)
        self.rec.reset()
        self.goto_fresh("assignments", "unipus")
        self.page.wait_for_timeout(1500)
        metrics = self.page.evaluate(
            """() => {
                 const w = document.querySelector('main .tbl-wrap');
                 const td = document.querySelector('main .tbl tbody td');
                 return {
                   canScroll: w ? w.scrollWidth > w.clientWidth : null,
                   cellW: td ? Math.round(td.getBoundingClientRect().width) : 0,
                   overflow: document.documentElement.scrollWidth - window.innerWidth,
                 };
               }"""
        )
        self._add("手机端/宽表格可横滑", metrics["canScroll"] is True, str(metrics))
        self._add("手机端/表格首列不塌陷（≥120px）", metrics["cellW"] >= 120,
                  f"首列 {metrics['cellW']}px")
        self._add("手机端/页面无横向溢出", metrics["overflow"] <= 1, f"{metrics['overflow']}px")

        self.page.set_viewport_size({"width": 1440, "height": 1000})

    # -- 4. 响应式 -------------------------------------------------------

    def run_responsive(self) -> None:
        print("\n=== 4. 响应式（无横向溢出）===")
        for width in VIEWPORTS:
            self.mock.reset()
            self.rec.reset()
            self.ensure_closed()
            self.page.set_viewport_size({"width": width, "height": 900})
            # 必须真重载：只改 hash 不会重跑 boot()/enterApp()，
            # 左轨折叠状态会沿用上一档宽度，量出来的就是假的。
            self.goto_fresh("overview", None)
            ok, reason = True, ""
            try:
                self.wait_page("概览")
                self.page.wait_for_timeout(180)
                metrics = self.page.evaluate(
                    """() => ({
                         scroll: document.documentElement.scrollWidth,
                         inner: window.innerWidth,
                         body: document.body.scrollWidth,
                       })""")
                if metrics["scroll"] > metrics["inner"] + 1:
                    ok, reason = (f"横向溢出：scrollWidth={metrics['scroll']} > "
                                  f"innerWidth={metrics['inner']}（body={metrics['body']}）")
                if self.shots:
                    self.page.screenshot(path=f"{self.shots}/overview-{width}.png",
                                         full_page=False)
            except Exception as exc:  # noqa: BLE001
                ok, reason = False, f"{type(exc).__name__}"
            if ok and self.rec.summary():
                ok, reason = False, self.rec.summary()
            self._add(f"响应式/{width}px", ok, reason)
        self.page.set_viewport_size({"width": 1440, "height": 900})

    # -- 5. 未登录 -------------------------------------------------------

    def run_anonymous(self, browser) -> None:
        print("\n=== 5. 未登录 ===")
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        ok, reason = True, ""
        try:
            page.goto(self.base + "/", wait_until="domcontentloaded")
            page.wait_for_selector("#login:not([hidden])", timeout=self.timeout)
            page.wait_for_timeout(200)
            if not page.evaluate("() => document.getElementById('app').hidden"):
                ok, reason = False, "未登录却显示了 #app（登录门没拦住）"
            if ok and not page.locator("#login-form").count():
                ok, reason = False, "登录门里没有 #login-form"
        except Exception as exc:  # noqa: BLE001
            ok, reason = False, f"{type(exc).__name__}: 没有出现登录门"
        ctx.close()
        self._add("未登录/显示登录门", ok, reason)

        ok, reason = True, ""
        try:
            status, body = http_json(self.base, "/api/overview")
            if status != 401:
                ok, reason = False, f"/api/overview 期望 401，实际 {status}"
            elif not isinstance(body, dict) or not body.get("error"):
                ok, reason = False, f"401 响应里没有中文 error：{body}"
        except Exception as exc:  # noqa: BLE001
            ok, reason = False, f"{type(exc).__name__}: {exc}"
        self._add("未登录//api/overview → 401", ok, reason)


# ---------------------------------------------------------------- 主流程


def main() -> int:
    parser = argparse.ArgumentParser(
        description="控制台 v2：页面 × 平台矩阵 + 三态 + 响应式检查（只读）")
    parser.add_argument("--base", default="http://127.0.0.1:8765",
                        help="已在运行的控制台地址（脚本不会自己起服务）")
    parser.add_argument("--token", default="", help="已有效的 kdb_session 值")
    parser.add_argument("--username", default="", help="登录用户名（没有 --token 时用）")
    parser.add_argument("--password", default="", help="登录密码（没有 --token 时用）")
    parser.add_argument("--timeout", type=int, default=8000, help="单步等待超时（毫秒）")
    parser.add_argument("--shots", default="", help="截图目录（可选，五档响应式各一张）")
    args = parser.parse_args()

    base = args.base.rstrip("/")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("✗ 缺 playwright：pip install playwright && playwright install chromium")
        return 2

    token, platforms, actions, is_super = preflight(base, args)

    # 自检：平台注册表推算出来的「该退回概览」组合必须和 KNOWN_FALLBACKS 一致，
    # 否则说明平台能力/动作变了，矩阵断言需要人更新 —— 直接报错比悄悄放水好。
    fallbacks = set(KNOWN_FALLBACKS)
    if not is_super:
        # 非超管时 users 页对每个平台都不可用（pageAvailable 的 admin 判断）
        fallbacks |= {("users", plat) for plat in PLATFORMS}
    computed = {
        (name, plat)
        for name, _ in PAGES
        for plat in PLATFORMS
        if expected_page(name, plat, platforms, actions, is_super) != name
    }
    if computed != fallbacks:
        print("✗ 平台能力/动作清单与脚本内置的 KNOWN_FALLBACKS 不一致：")
        print(f"    多出来：{sorted(computed - fallbacks)}")
        print(f"    少掉了：{sorted(fallbacks - computed)}")
        print("  请先核对 next/app.js 的 pageAvailable() 与各适配器的 capabilities / 动作注册。")
        return 2

    shots = args.shots or None
    if shots:
        from pathlib import Path
        Path(shots).mkdir(parents=True, exist_ok=True)

    mock = MockLayer()
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1440, "height": 900},
                                  locale="zh-CN")
        ctx.add_cookies([{"name": "kdb_session", "value": token, "url": base}])
        page = ctx.new_page()

        rec = Recorder()

        def on_pageerror(exc) -> None:  # noqa: ANN001
            rec.pageerrors.append(str(exc))

        def on_console(msg) -> None:  # noqa: ANN001
            if msg.type != "error":
                return
            text = msg.text
            # favicon 的 404 是浏览器自己发的，和页面无关
            if "favicon" in text.lower():
                return
            if is_noise(text):
                return
            rec.console.append(text)

        def on_response(resp) -> None:  # noqa: ANN001
            url = resp.url
            if "/favicon.ico" in url:
                return
            if resp.status >= 400:
                rec.bad.append(f"{resp.status} {urlparse(url).path}")

        page.on("pageerror", on_pageerror)
        page.on("console", on_console)
        page.on("response", on_response)
        mock.install(page)

        matrix = Matrix(base, page, mock, rec, platforms, actions, is_super,
                        args.timeout, shots, fallbacks)
        matrix.run_matrix()
        matrix.run_states()
        matrix.run_interactions()
        matrix.run_mobile()
        matrix.run_responsive()
        matrix.run_anonymous(browser)

        browser.close()

    # -- 汇总 -----------------------------------------------------------
    total = len(matrix.results)
    failed = [r for r in matrix.results if not r.ok]
    print("\n=== 汇总 ===")
    print(f"  检查项 {total} 个 · 通过 {total - len(failed)} · 失败 {len(failed)}")
    matrix_n = 12 * 6
    matrix_pass = sum(1 for r in matrix.results[:matrix_n] if r.ok)
    print(f"  组合矩阵 {matrix_n} 个 · 通过 {matrix_pass} · 失败 {matrix_n - matrix_pass}")
    for r in matrix.results[matrix_n:]:
        print(("  ✓ " if r.ok else "  ✗ ") + r.name + ("" if r.ok else f"：{r.detail}"))

    if failed:
        print("\n=== 失败清单 ===")
        for r in failed:
            print(f"  · {r.name}\n      原因：{r.detail}")
        print("\n退出码 1")
        return 1
    print("\n退出码 0（全绿）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
