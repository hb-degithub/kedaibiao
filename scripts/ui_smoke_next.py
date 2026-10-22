#!/usr/bin/env python3
"""控制台 v2（/next）的 UI 冒烟测试。

为什么要它：v2 是零构建的原生 JS，改排版时最怕「CSS 没坏但交互断了」。
这个脚本用 Playwright 起真浏览器，**对会访问平台的接口注入 mock**，
所以不需要任何平台账号，也不会真的登录平台。

覆盖：登录门、平台药丸、命令面板、行式列表、检查器（含上一条/下一条）、
刷课工作区（教材 → 树 → 干跑 → 日志）、iTEST 一键自动化（步骤/核验/人工补答）。

前置：

    PYTHONPATH=src python3 -m kedaibiao web --port 8765

用法：

    python3 scripts/ui_smoke_next.py --base http://127.0.0.1:8765
    python3 scripts/ui_smoke_next.py --base http://127.0.0.1:8765 \\
        --username admin --password '……' --shots /tmp/kdb-shots

不给 --username/--password 时，脚本自己找一个能登录的账号会被卡住 ——
所以也支持直接给会话 cookie：``--token <kdb_session 的值>``。

退出码：0 = 全部通过；1 = 有失败项（会逐条打印 FAIL 原因）。
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------- 固定测试数据

COURSES = {
    "platform": "unipus", "kind": "table", "title": "课程 / 班级（4 项）", "columns": [],
    "rows": [
        {"id": "c1", "name": "新视野大学英语（第三版）读写 2", "teacher": "李静", "term": "2026 秋"},
        {"id": "c2", "name": "大学英语视听说 2", "teacher": "王敏", "term": "2026 秋"},
        {"id": "c3", "name": "学术英语写作", "teacher": "Chen", "term": "2026 秋"},
        {"id": "c4", "name": "跨文化交际", "teacher": "—", "term": "2026 秋"},
    ],
}

TREE = {
    "course": "c1", "class_name": "2026 秋 · 英语 2 班",
    "finish_points": 34, "total_points": 80, "group_count": 12, "required_count": 9,
    "units": [
        {"id": "u1", "name": "Unit 1 Living in a Digital World", "groups": [
            {"id": "g1", "name": "1.1 Reading comprehension", "required": True,
             "finished": True, "assignment_id": "6f91c9", "total_items": 10,
             "correct_items": 10, "score": 100},
            {"id": "g2", "name": "1.2 Vocabulary focus", "required": True,
             "finished": False, "assignment_id": "7a22d1", "total_items": 8},
            {"id": "g3", "name": "1.3 Listening practice", "required": False,
             "finished": False, "assignment_id": "7b33e2", "total_items": 6},
        ]},
        {"id": "u2", "name": "Unit 2 Culture Exchange", "groups": [
            {"id": "g4", "name": "2.1 Reading comprehension", "required": True,
             "finished": False, "assignment_id": "8c44f3", "total_items": 10},
            {"id": "g5", "name": "2.2 Translation", "required": True,
             "finished": False, "assignment_id": "8d55a4", "total_items": 4},
        ]},
    ],
}

BRUSH_JOB = {
    "job_id": "j-901", "platform": "unipus", "confirm": True, "status": "done",
    "started_at": "2026-10-20T19:01:00", "finished_at": "2026-10-20T19:03:10",
    "summary": "提交 3 组 · 跳过 6 组 · 记账 612 秒",
    "counts": {"submitted": 3, "skipped": 6, "failed": 0, "targets": 8,
               "credited_seconds": 612},
    "logs": [
        {"at": "2026-10-20T19:01:00", "level": "info", "text": "$ 读取教材结构"},
        {"at": "2026-10-20T19:01:06", "level": "ok", "text": "→ 必修 9 组，已完成 3 组"},
        {"at": "2026-10-20T19:02:41", "level": "ok", "text": "✓ 组 6f91c9 提交成功"},
        {"at": "2026-10-20T19:03:10", "level": "warn", "text": "! 本轮记账 612 秒"},
    ],
}

ASSIGN = {
    "kind": "table", "title": "作业 / 任务（3 项）",
    "columns": [
        {"key": "title", "label": "作业"},
        {"key": "assignment_id", "label": "任务号", "mono": True},
        {"key": "status", "label": "状态"},
    ],
    "rows": [
        {"title": "Unit 1 Reading comprehension", "assignment_id": "6f91c9d2",
         "status": "succeeded"},
        {"title": "Unit 1 Vocabulary focus", "assignment_id": "7a22d1e8", "status": "pending"},
        {"title": "Unit 2 Translation", "assignment_id": "8d55a4b1", "status": "pending"},
    ],
}

DETAIL = {
    "kind": "detail", "title": "Unit 1 Vocabulary focus",
    "fields": [
        {"label": "任务号", "value": "7a22d1e8"},
        {"label": "题量", "value": "8"},
        {"label": "满分", "value": "100"},
    ],
    "sections": [{
        "label": "题目", "kind": "table",
        "columns": [
            {"key": "no", "label": "题号", "mono": True},
            {"key": "stem", "label": "题干"},
            {"key": "answer", "label": "答案"},
        ],
        "rows": [
            {"no": "1", "stem": "The company aims to ___ its market share.",
             "answer": "expand"},
            {"no": "2", "stem": "We must ___ the risks before launching.", "answer": "assess"},
        ],
    }],
}

ITEST_JOB = {
    "job_id": "a-77", "kind": "itest-auto", "platform": "itest", "exam_id": "exam-88",
    "status": "succeeded",
    "paper": {"title": "视听说 Unit 3 单元测试", "question_count": 18, "slot_count": 34},
    "steps": [
        {"name": "取卷", "ok": True, "detail": "18 题"},
        {"name": "听力转写", "ok": True, "detail": "6 段"},
        {"name": "本地作答", "ok": True, "detail": "16/18"},
        {"name": "填卷", "ok": True, "detail": "34 槽位"},
        {"name": "逐项核验", "ok": True, "detail": "34 槽位"},
        {"name": "重进核验", "ok": False, "detail": "读回为空"},
    ],
    "verify": {"persisted": False, "matched": 0, "missing": ["q101"],
               "checked": {"total": 0}, "mismatched": []},
    "manual": [{
        "qid": "q101", "kind": "radio", "kind_label": "单选",
        "stem": "What does the speaker suggest about online learning?",
        "options": [
            {"sub": 1, "letter": "A", "text": "It replaces classrooms"},
            {"sub": 1, "letter": "B", "text": "It needs self-discipline"},
            {"sub": 1, "letter": "C", "text": "It is always cheaper"},
            {"sub": 1, "letter": "D", "text": "It is not effective"},
        ],
    }],
    "logs": [
        {"at": "2026-10-20T18:50:02", "level": "info", "text": "$ 打开考试 exam-88"},
        {"at": "2026-10-20T18:50:19", "level": "ok", "text": "✓ 取卷成功：18 题 / 34 槽位"},
        {"at": "2026-10-20T18:55:58", "level": "warn", "text": "! q101 转为人工"},
    ],
}

MANUAL_OK = {"exam_id": "exam-88", "saved": 1, "missing": []}

ATTENDANCE = {
    "platform": "neusoft", "kind": "table", "title": "考勤场次（2 场）",
    "columns": [
        {"key": "attendance_id", "label": "场次号", "mono": True},
        {"key": "course", "label": "课程"},
        {"key": "status", "label": "状态"},
        {"key": "type", "label": "类型"},
        {"key": "location", "label": "定位"},
    ],
    "rows": [
        {"attendance_id": "att-901", "course": "大学英语I（三）", "status": "可签",
         "type": "签到", "location": "需定位"},
        {"attendance_id": "att-903", "course": "大学物理", "status": "已签",
         "type": "签到", "location": "—"},
    ],
}

SIGN_RESULT = {"kind": "message", "title": "场次 att-901 已提交",
               "message": "签到请求已受理，请回读状态。"}

PIGAI_ASSIGN = {
    "kind": "table",
    "columns": [
        {"key": "id", "label": "作文号", "mono": True},
        {"key": "title", "label": "题目"},
        {"key": "state", "label": "状态"},
    ],
    "rows": [{"id": "200000001", "title": "My Campus Life", "state": "—"}],
}

PIGAI_DETAIL = {
    "kind": "detail", "title": "My Campus Life",
    "fields": [{"label": "作文号", "value": "200000001"},
               {"label": "满分", "value": "100"}],
    "sections": [{"label": "题目要求", "kind": "text",
                  "text": "Write about your campus life. 150 words."}],
}

PINCO_ASSIGN = {
    "kind": "table",
    "columns": [
        {"key": "id", "label": "任务号", "mono": True},
        {"key": "title", "label": "名称"},
        {"key": "state", "label": "状态"},
    ],
    "rows": [{"id": "hw-1", "title": "Unit 3 作业", "state": "—"}],
}

PINCO_PLAN = {
    "kind": "table", "platform": "pinco", "action": "pinco-plan",
    "title": "品课提交计划（2 项，可提交 1）",
    "digest": "a1b2c3d4e5f60718293a4b5c6d7e8f90",
    "counts": {"total": 2, "planned": 1, "needs_human": 1, "skipped": 0, "failed": 0},
    "rows": [
        {"state": "planned", "state_label": "可提交", "title": "Unit 3 作业",
         "assignment_id": "hw-1", "course": "大学英语", "question_count": 8,
         "human_count": 0, "digest": "11aa22bb", "reason": "—"},
        {"state": "needs_human", "state_label": "需要人工", "title": "Unit 5 作文",
         "assignment_id": "hw-2", "course": "大学英语", "question_count": 2,
         "human_count": 2, "digest": "", "reason": "2 题没有可靠答案"},
    ],
}

PINCO_SIGNIN_DRY = {
    "platform": "pinco", "action": "signin", "label": "课堂签到（写操作）",
    "kind": "detail", "title": "课堂签到 · 干跑",
    "note": "干跑：只解析签到码，没有发送任何请求。",
    "fields": [{"label": "模式", "value": "干跑"},
               {"label": "签到码", "value": "a1b2****（32 位）"},
               {"label": "结果", "value": "未发送（干跑）"}],
}

PINCO_SIGNIN_OK = {
    "platform": "pinco", "action": "signin", "label": "课堂签到（写操作）",
    "kind": "detail", "title": "课堂签到 · 写入",
    "note": "写操作：平台接受了这次签到。",
    "fields": [{"label": "模式", "value": "写入"},
               {"label": "结果", "value": "已发送，签到成功"}],
}

PINCO_SUBMIT = {
    "platform": "pinco", "action": "pinco-submit-reviewed", "assignment_id": "hw-1",
    "title": "Unit 3 作业", "submitted": True, "ok": True, "score": None,
    "message": "已提交 8 题", "question_count": 8, "answered": 8,
}


# ---------------------------------------------------------------- 小工具

@dataclass
class Result:
    passed: int = 0
    failures: list[str] = field(default_factory=list)

    def check(self, ok: bool, label: str, detail: str = "") -> None:
        if ok:
            self.passed += 1
            print(f"  PASS  {label}")
        else:
            self.failures.append(label + (f" — {detail}" if detail else ""))
            print(f"  FAIL  {label}" + (f" — {detail}" if detail else ""))


def login_token(base: str, username: str, password: str) -> str:
    """用账号密码换一个会话 cookie（失败直接退出，后面全跑不了）。"""
    body = json.dumps({"username": username, "password": password}).encode()
    req = urllib.request.Request(
        base.rstrip("/") + "/api/auth/login", data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            cookie = resp.headers.get("Set-Cookie") or ""
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"登录失败：HTTP {exc.code}（{exc.read()[:200]!r}）") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"连不上 {base}：{exc}。先起控制台再跑本脚本。") from exc
    for part in cookie.split(";"):
        name, _, value = part.strip().partition("=")
        if name == "kdb_session" and value:
            return value
    raise SystemExit("登录成功但响应里没有 kdb_session cookie。")


def main() -> int:
    parser = argparse.ArgumentParser(description="控制台 v2 UI 冒烟测试")
    parser.add_argument("--base", default="http://127.0.0.1:8765", help="控制台地址")
    parser.add_argument("--token", default="", help="已有的 kdb_session 值（省一次登录）")
    parser.add_argument("--username", default="admin", help="登录用户名")
    parser.add_argument("--password", default="", help="登录密码（不给且没 --token 就退出）")
    parser.add_argument("--path", default="/", help="控制台入口路径（默认 /；别名 /next）")
    parser.add_argument("--shots", default="", help="截图目录（可选）")
    parser.add_argument("--headed", action="store_true", help="显示浏览器窗口")
    args = parser.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:  # pragma: no cover - 环境问题
        raise SystemExit("需要 playwright：pip install playwright && playwright install chromium")

    base = args.base.rstrip("/")
    entry = base + ("" if args.path in ("", "/") else "/" + args.path.strip("/"))
    token = args.token
    if not token:
        if not args.password:
            raise SystemExit("给 --token，或给 --username/--password 让脚本自己登录。")
        token = login_token(base, args.username, args.password)

    shots = Path(args.shots) if args.shots else None
    if shots:
        shots.mkdir(parents=True, exist_ok=True)
    result = Result()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not args.headed)
        ctx = browser.new_context(viewport={"width": 1440, "height": 1000})
        ctx.add_cookies([{
            "name": "kdb_session", "value": token, "domain": "127.0.0.1",
            "path": "/", "httpOnly": True, "sameSite": "Strict",
        }])
        page = ctx.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.on("console", lambda msg: errors.append(msg.text)
                if msg.type == "error" and "frame-ancestors" not in msg.text else None)
        # 把 4xx/5xx 的 URL 也记下来：控制台只会说「Failed to load resource」，
        # 不带 URL 的话根本看不出是哪一条。favicon 是浏览器行为，忽略。
        page.on("response", lambda resp: errors.append(
            f"HTTP {resp.status} {resp.url}")
            if resp.status >= 400 and "favicon" not in resp.url else None)

        def mock(route, payload, status=200):
            route.fulfill(status=status, content_type="application/json",
                          body=json.dumps(payload, ensure_ascii=False))

        def shoot(name: str) -> None:
            if shots:
                page.screenshot(path=str(shots / f"{name}.png"))

        # ---- 会访问平台的三类接口全部 mock，绝不真登录平台 ----
        page.route("**/api/platforms/unipus/actions/courses",
                   lambda r: mock(r, COURSES))
        page.route("**/api/platforms/unipus/actions/assignments",
                   lambda r: mock(r, ASSIGN))
        page.route("**/api/platforms/unipus/course-tree*", lambda r: mock(r, TREE))
        page.route("**/api/platforms/unipus/brush", lambda r: mock(r, BRUSH_JOB, 202))
        page.route("**/api/jobs/j-901", lambda r: mock(r, BRUSH_JOB))
        page.route("**/api/assignments/detail", lambda r: mock(r, DETAIL))
        page.route("**/api/platforms/itest/actions/assignments",
                   lambda r: mock(r, {"kind": "table", "rows": []}))
        page.route("**/api/platforms/pigai/actions/*",
                   lambda r: mock(r, {"kind": "table", "rows": []}))
        page.route("**/api/platforms/pigai/actions/assignments",
                   lambda r: mock(r, PIGAI_ASSIGN))
        page.route("**/api/platforms/pinco/actions/*",
                   lambda r: mock(r, {"kind": "table", "rows": []}))
        page.route("**/api/platforms/pinco/actions/assignments",
                   lambda r: mock(r, PINCO_ASSIGN))
        page.route("**/api/platforms/pinco/plan", lambda r: mock(r, PINCO_PLAN))
        page.route("**/api/platforms/pinco/submit-reviewed",
                   lambda r: mock(r, PINCO_SUBMIT))
        signin_calls: list[dict] = []

        def pinco_signin(route):
            body = json.loads(route.request.post_data or "{}")
            signin_calls.append(body)
            mock(route, PINCO_SIGNIN_OK if body.get("confirm") in (1, "1", True)
                 else PINCO_SIGNIN_DRY)

        page.route("**/api/platforms/pinco/actions/signin", pinco_signin)
        page.route("**/api/platforms/itest/auto", lambda r: mock(r, ITEST_JOB, 202))
        page.route("**/api/jobs/a-77", lambda r: mock(r, ITEST_JOB))
        page.route("**/api/platforms/itest/manual", lambda r: mock(r, MANUAL_OK))

        print("· 外壳与导航")
        page.goto(entry + "/#/overview", wait_until="domcontentloaded")
        page.wait_for_selector("#app:not([hidden])", timeout=15_000)
        page.wait_for_timeout(900)
        result.check(page.locator(".kpi").count() >= 4, "概览渲染出 KPI 卡片")
        result.check(page.locator(".plat-pill").count() >= 2, "顶栏平台药丸已渲染")

        page.get_by_role("button", name="U校园（Unipus）").click()
        page.wait_for_timeout(900)
        result.check("/unipus" in page.url, "切平台写进 hash")

        # 顶栏账号：信息 chip（看详情）+ 多号时的切换器（换号）
        result.check(page.is_visible("#acct-info"), "顶栏显示当前平台账号信息")
        page.click("#acct-info")
        page.wait_for_timeout(400)
        rows = page.locator(".menu-row").count()
        result.check(page.is_visible("#acct-menu") and rows >= 1, "账号下拉列出账号")
        result.check(page.is_visible("#acct-switch-wrap") == (rows >= 2),
                     "只有多号时才出现切换器")
        page.get_by_role("button", name="详情").first.click()
        page.wait_for_timeout(600)
        result.check("平台账号详情" in page.locator("#inspector-title").inner_text(),
                     "账号下拉可看详细信息")
        page.keyboard.press("Escape")
        page.wait_for_timeout(200)

        page.keyboard.press("Control+k")
        page.wait_for_timeout(300)
        result.check(page.is_visible("#palette"), "⌘K 打开命令面板")
        page.fill("#palette-input", "统计")
        page.wait_for_timeout(200)
        page.keyboard.press("Enter")
        page.wait_for_timeout(800)
        result.check("stats" in page.url, "命令面板可跳转功能页")
        result.check(page.locator("table.tbl tbody tr").count() >= 3, "统计表按平台出行")

        print("· 作业列表与检查器")
        page.goto(entry + "/#/assignments/unipus", wait_until="domcontentloaded")
        page.wait_for_timeout(1200)
        result.check(page.locator(".tbl tbody tr").count() == 3, "作业行式列表渲染 3 行")
        page.locator(".tbl tbody tr").nth(1).click()
        page.wait_for_timeout(800)
        result.check(page.is_visible("#inspector"), "行点击打开右侧检查器")
        foot = page.locator("#inspector-foot").inner_text()
        result.check("上一条" in foot and "下一条" in foot, "检查器有逐条翻页")
        page.get_by_role("button", name="下一条 →").click()
        page.wait_for_timeout(600)
        result.check("3 / 3" in page.locator("#inspector-foot").inner_text(),
                     "翻页后序号前进（2 / 3 → 3 / 3）")
        page.keyboard.press("Escape")
        result.check(not page.is_visible("#inspector"), "Esc 关闭检查器")

        print("· 刷课工作区")
        page.get_by_role("button", name="刷课", exact=True).click()
        page.wait_for_timeout(900)
        result.check(page.locator(".book-row").count() == 4, "教材列表渲染 4 本")
        page.get_by_role("button", name="打开").first.click()
        page.wait_for_timeout(900)
        result.check(page.locator(".tree-unit").count() == 2, "教材树渲染 2 个单元")
        result.check(page.locator(".tree-group").count() == 5, "教材树渲染 5 个分组")
        # 完成度按「组」算而不是「得分点」：fixture 里必修 2 组、完成 1 组
        prog_val = page.locator(".progress-value").first.inner_text() if page.locator(
            ".progress-value").count() else ""
        # 期望值从 fixture 现算：必修组数 + 其中已完成数（改 fixture 不会假失败）
        req_groups = [g for u in TREE["units"] for g in u["groups"] if g.get("required")]
        req_done = sum(1 for g in req_groups if g.get("finished"))
        expect = f"{req_done}/{len(req_groups)}"
        result.check(expect in prog_val.replace(" ", ""),
                     f"完成度按必修组数计算（{expect} 组，不是得分点）",
                     f"实际 {prog_val!r}")
        result.check("得分点" in page.locator(".progress-caption").first.inner_text(),
                     "caption 里保留得分点口径")
        shoot("smoke-brush")
        page.get_by_role("button", name="提交未完成（干跑）").click()
        page.wait_for_timeout(1800)
        result.check(page.locator(".log-view div").count() >= 3, "干跑日志逐行追加")
        result.check("记账 612" in page.locator(".pills").last.inner_text(),
                     "状态区显示计数")
        result.check(page.get_by_role("button", name="提交未完成（真交）").is_enabled(),
                     "干跑结束后真交按钮恢复可用")

        print("· iTEST 一键自动化")
        page.goto(entry + "/#/assignments/itest", wait_until="domcontentloaded")
        page.wait_for_timeout(900)
        page.get_by_role("button", name="一键自动化", exact=True).click()
        page.wait_for_timeout(400)
        page.fill(".workspace input.mono", "exam-88")
        page.get_by_role("button", name="开始（取卷 → 答题 → 核验）").click()
        page.wait_for_timeout(1600)
        result.check(page.locator(".step").count() == 6, "六个步骤状态条")
        result.check(page.locator(".auto-q").count() == 1, "缺答案的题转为人工卡片")
        result.check(page.get_by_role("button", name="确认交卷").is_disabled(),
                     "有缺答案时交卷禁用")
        page.select_option(".q-subs select", "B")
        page.get_by_role("button", name="保存并复核").click()
        page.wait_for_timeout(900)
        result.check("已保存" in page.locator(".auto-foot").inner_text(),
                     "人工答案可以保存")
        result.check(page.locator(".auto-q").count() == 0, "保存后缺答案清单清空")
        shoot("smoke-itest")

        print("· 批改网评分循环")
        page.goto(entry + "/#/assignments/pigai", wait_until="domcontentloaded")
        page.wait_for_selector("main .tbl tbody tr", timeout=10_000)
        # pigai 没有取题能力：行内「提交」不该出现
        result.check(page.get_by_role("button", name="提交", exact=True).count() == 0,
                     "pigai 行内不出「提交」（没有取题能力）")
        page.locator("main .tbl tbody tr").first.click()
        page.wait_for_timeout(900)
        result.check(page.get_by_text("批改网评分循环（提分）").count() >= 1,
                     "详情里挂出评分循环面板")
        for label in ("开始提分循环", "只取分（不提交）", "继续等待", "历史轮次"):
            result.check(page.get_by_role("button", name=label).count() >= 1,
                         f"评分面板有「{label}」")
        shoot("smoke-pigai")
        page.keyboard.press("Escape")
        page.wait_for_timeout(200)

        print("· 品课：计划 → 复核 → 提交")
        page.goto(entry + "/#/assignments/pinco", wait_until="domcontentloaded")
        page.wait_for_selector("main .tbl tbody tr", timeout=10_000)
        result.check(page.get_by_role("button", name="提交", exact=True).count() == 0,
                     "pinco 行内不出「提交」（必须走复核）")
        page.get_by_role("button", name="提交计划（复核）").click()
        page.wait_for_timeout(1100)
        result.check(page.locator("#inspector-body .rowx").count() == 2, "计划表列出 2 项")
        result.check(page.get_by_role("button", name="复核并提交").count() == 1,
                     "只有「可提交」的行给复核按钮")
        result.check("a1b2c3d4e5f6" in page.locator("#inspector-body").inner_text(),
                     "计划摘要可见")
        shoot("smoke-pinco-plan")
        page.get_by_role("button", name="复核并提交").first.click()
        page.wait_for_timeout(600)
        page.get_by_role("button", name="确认提交").click()
        page.wait_for_timeout(200)
        result.check(page.get_by_role("button", name="再点一次，真的提交").count() == 1,
                     "提交需要二次确认")
        page.get_by_role("button", name="再点一次，真的提交").click()
        page.wait_for_timeout(1200)
        result.check("提交结果" in page.locator("#inspector-title").inner_text(),
                     "复核后提交得到结果")
        page.keyboard.press("Escape")
        page.wait_for_timeout(200)

        print("· 导航按能力裁剪")
        # iTEST 没有考勤动作 → 左轨不该有「考勤」，直接访问也要退回概览
        page.get_by_role("button", name="iTEST 智能测评").click()
        page.wait_for_timeout(1200)
        rail = page.locator(".nav-item").all_inner_texts()
        result.check(all("考勤" not in x for x in rail), "iTEST 左轨没有「考勤」")
        page.goto(entry + "/#/attendance/itest", wait_until="domcontentloaded")
        page.wait_for_timeout(900)
        result.check("#/overview" in page.url, "直接访问不支持的页面会退回概览")
        # 东软有考勤 → 左轨有
        page.get_by_role("button", name="东软智慧教育").click()
        page.wait_for_timeout(1200)
        rail = page.locator(".nav-item").all_inner_texts()
        result.check(any("考勤" in x for x in rail), "东软左轨有「考勤」")
        page.get_by_role("button", name="希沃品课").click()
        page.wait_for_timeout(1200)
        rail = page.locator(".nav-item").all_inner_texts()
        result.check(any("考勤" in x for x in rail), "品课左轨有「考勤」（签到码）")

        print("· 品课签到码模式")
        page.goto(entry + "/#/attendance/pinco", wait_until="domcontentloaded")
        page.wait_for_selector("main input.mono", timeout=10_000)
        page.wait_for_timeout(400)
        result.check(page.get_by_text("考勤 · 签到码").count() >= 1,
                     "品课考勤页是「签到码」模式")
        page.fill("main input.mono",
                  "https://x.edu/student/signin?attendanceCode=abc123")
        page.get_by_role("button", name="解析（干跑）").click()
        page.wait_for_timeout(800)
        result.check(len(signin_calls) == 1 and signin_calls[-1].get("confirm") in (0, "0"),
                     "干跑只解析、confirm=0")
        result.check("未发送" in page.locator("main").inner_text(), "干跑不发请求")
        page.get_by_role("button", name="确认签到").click()
        page.wait_for_timeout(200)
        result.check(page.get_by_role("button", name="再点一次，真的签到").count() == 1,
                     "签到需要二次确认")
        page.get_by_role("button", name="再点一次，真的签到").click()
        page.wait_for_timeout(800)
        result.check(signin_calls[-1].get("confirm") in (1, "1"), "确认后 confirm=1")
        result.check("签到成功" in page.locator("main").inner_text(), "签到结果显示成功")

        print("· 工具页是统一 LLM 配置中心（不放平台相关内容）")
        page.goto(entry + "/#/tools", wait_until="domcontentloaded")
        page.wait_for_selector("main", timeout=10_000)
        page.wait_for_timeout(900)
        main_text = page.locator("main").inner_text()
        result.check("主模型" in main_text, "工具页有主模型配置")
        result.check("OCR" in main_text, "工具页有 OCR 模型配置")
        result.check("低置信度升级" in main_text, "工具页有低置信度升级配置")
        result.check("置信度阈值" in main_text, "升级阈值可配")
        result.check("注意事项" in main_text or "只配一处" in main_text, "写了注意事项")
        result.check(page.locator("main .tabs .tab").count() == 0,
                     "工具页不再列平台动作（无动作页签）")

        print("· 计划页（未实现功能标注）")
        page.goto(entry + "/#/roadmap", wait_until="domcontentloaded")
        page.wait_for_timeout(800)
        result.check(page.locator(".rowx").count() == 5, "计划页列出 5 项")
        result.check(page.get_by_text("U校园口语评测").count() >= 1, "口语评测在计划里")
        page.get_by_text("U校园口语评测").first.click()
        page.wait_for_timeout(300)
        page.get_by_role("button", name="看计划").first.click()
        page.wait_for_timeout(600)
        detail = page.locator("#inspector-body").inner_text()
        result.check("阶段 1" in detail and "阶段 3" in detail, "口语评测给出分阶段计划")
        result.check("留存策略" in detail, "标出必须先确认的硬点")
        shoot("smoke-roadmap")
        page.keyboard.press("Escape")
        page.wait_for_timeout(200)

        print("· 考勤与签到")
        page.route("**/api/platforms/neusoft/actions/attendance",
                   lambda r: mock(r, ATTENDANCE))
        sign_calls: list[int] = []
        page.route("**/api/platforms/neusoft/actions/sign",
                   lambda r: (sign_calls.append(1), mock(r, SIGN_RESULT)))
        page.goto(entry + "/#/attendance/neusoft", wait_until="domcontentloaded")
        page.wait_for_selector(".tbl tbody tr", timeout=10_000)
        page.wait_for_timeout(300)
        actual_rows = page.locator("main .tbl tbody tr").count()
        dump = [t.replace(chr(10), " | ") for t in page.locator("main .tbl tbody tr").all_inner_texts()]
        titles = [t.inner_text().strip() for t in page.locator(".panel-head h2").all()]
        result.check(actual_rows == 2, "考勤页渲染场次", f"实际 {actual_rows} 行")
        result.check(page.get_by_role("button", name="签到", exact=True).count() == 1,
                     "只有「可签」场次给签到按钮")
        page.locator(".tbl tbody tr").first.get_by_role(
            "button", name="签到", exact=True).click()
        page.wait_for_timeout(400)
        foot = page.locator("#inspector-foot")
        foot.get_by_role("button", name="签到", exact=True).click()
        page.wait_for_timeout(200)
        foot.get_by_role("button", name="再点一次，确认签到").click()
        page.wait_for_timeout(400)
        result.check(not sign_calls, "需定位场次缺坐标时不发请求")
        inputs = page.locator("#inspector-body input.mono")
        inputs.nth(0).fill("30.5123")
        inputs.nth(1).fill("114.4123")
        page.locator("#inspector-foot").get_by_role(
            "button", name="再点一次，确认签到").click()
        page.wait_for_timeout(900)
        result.check(len(sign_calls) == 1, "填了坐标后签到请求发出")
        result.check("签到结果" in page.locator("#inspector-title").inner_text(),
                     "签到结果显示在检查器")
        page.keyboard.press("Escape")
        page.wait_for_timeout(200)

        print("· 状态与主题")
        page.goto(entry + "/#/bank", wait_until="domcontentloaded")
        page.wait_for_timeout(700)
        result.check(page.locator(".state-gate").count() == 1,
                     "未选平台时可见 gate 而不是空表")
        page.goto(entry + "/#/overview", wait_until="domcontentloaded")
        page.wait_for_timeout(600)
        page.click("#theme-btn")
        page.wait_for_timeout(300)
        result.check(
            page.evaluate("document.documentElement.getAttribute('data-theme')") == "light",
            "可切到浅色主题",
        )
        page.click("#theme-btn")

        result.check(not errors, "控制台无 JS 错误", "；".join(errors[:5]))

        # 未登录应当看到登录门
        ctx2 = browser.new_context(viewport={"width": 1440, "height": 900})
        page2 = ctx2.new_page()
        page2.goto(entry + "/", wait_until="domcontentloaded")
        page2.wait_for_timeout(700)
        result.check(page2.is_visible("#login"), "无会话时显示登录门")
        browser.close()

    print()
    total = result.passed + len(result.failures)
    print(f"结果：{result.passed}/{total} 通过")
    if result.failures:
        for item in result.failures:
            print("  ✗", item)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
