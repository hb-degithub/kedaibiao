#!/usr/bin/env python3
"""学习通 / 超星「自动化台」：一个本地网页，点按钮就能刷课、刷题。

## 它是什么

- 后端只绑 ``127.0.0.1``，用 ``storage_state`` 里的登录态驱动无头 Chromium；
- 前端三个页签：**任务点**（刷课）、**测验/作业**（刷题）、**运行日志**；
- 刷课：按任务点逐个播放视频/读文档，进度来自播放器自己的上报，
  完成判定回读 ``isPassed`` / 章节树；
- 刷题：解析题面（含字体混淆解码）→ 作答（LLM 不在线时降级规则）→ 填卷 →
  逐题回读核验 → **默认不交卷**，点「填卷并交卷」才真交。

## 用法

```bash
# 先登录一次（会话落到 data/sessions/chaoxing.storage_state.json）
python3 scripts/chaoxing_auto.py login --user 手机号

# 起自动化台
python3 scripts/chaoxing_studio.py
# → http://127.0.0.1:8900/
```

## 边界

- 只在回环地址上跑；没有账号体系统，别往公网放；
- 同一时刻只允许一个任务（同一个账号并发刷课会互相顶掉播放器）；
- 验证码/人脸采集不代过：遇到就停在日志里，等人工处理。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kedaibiao.browser.pw import PwBrowser, PwConfig  # noqa: E402
from kedaibiao.platforms.chaoxing import login, parsing  # noqa: E402
from kedaibiao.platforms.chaoxing.quizrun import ChaoxingQuiz  # noqa: E402
from kedaibiao.platforms.chaoxing.study import ChaoxingStudy  # noqa: E402

log = logging.getLogger("chaoxing-studio")
STATE = ROOT / "data" / "sessions" / "chaoxing.storage_state.json"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
COURSE_LIST_URL = (
    "https://mooc2-ans.chaoxing.com/mooc2-ans/visit/courselistdata"
    "?courseType=1&courseFolderId=0&courseFolderSize=0&superstarClass=0"
)


# ---------------------------------------------------------------------------
# 任务管理：同一时刻只跑一个（同账号并发会互相顶掉播放器）
# ---------------------------------------------------------------------------


@dataclass
class Job:
    job_id: str
    kind: str
    label: str
    status: str = "running"  # running | done | failed | stopped
    logs: list[dict[str, Any]] = field(default_factory=list)
    summary: str = ""
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    stop: bool = False
    #: 最近一帧页面截图（前台显示用；JPEG 字节）
    live: bytes | None = None
    live_at: float = 0.0
    #: 交卷后平台回显的分数
    score: float | None = None
    score_text: str = ""
    #: 正在等人工认证时的提示（人脸采集/验证码）
    human: str = ""

    def add(self, level: str, message: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self.logs.append({"t": stamp, "level": level, "message": message})
        del self.logs[:-500]

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "kind": self.kind,
            "label": self.label,
            "status": self.status,
            "summary": self.summary,
            "logs": self.logs,
            "has_live": self.live is not None,
            "score": self.score,
            "score_text": self.score_text,
            "human": self.human,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "elapsed": round((self.finished_at or time.time()) - self.started_at, 1),
        }


class Studio:
    """后台任务的唯一入口：课程列表、任务点、刷课、刷题都从这里走。"""

    def __init__(self, *, state: Path, headless: bool = True) -> None:
        self.state = state
        self.headless = headless
        self._lock = threading.Lock()
        self._job: Job | None = None
        self._seq = 0
        self._courses: list[dict[str, str]] = []
        self._courses_at = 0.0
        #: job_id → 人工认证等待事件（POST /api/human 置位）
        self._human_events: dict[str, threading.Event] = {}
        self._exams: dict[tuple[str, str], tuple[float, list[dict[str, Any]]]] = {}

    # -- 浏览器会话 -------------------------------------------------------

    def _config(self) -> PwConfig:
        return PwConfig(
            headless=self.headless,
            user_agent=UA,
            storage_state=str(self.state) if self.state.is_file() else None,
        )

    async def _ensure_login(self, page: Any) -> None:
        await login.ensure_login(page, state_path=self.state)

    # -- 前台显示 / 人工认证 ----------------------------------------------

    async def _live_loop(self, page: Any, job: Job) -> None:
        """后台每 2 秒抓一帧页面截图，供前端「前台显示」用。"""
        while job.status == "running":
            try:
                job.live = await page.raw.screenshot(type="jpeg", quality=60)
                job.live_at = time.time()
            except Exception:  # noqa: BLE001 - 截图失败不影响任务
                log.debug("截图失败", exc_info=True)
            await asyncio.sleep(2.0)

    async def _wait_human(self, job: Job, kind: str, message: str) -> bool:
        """暂停任务等人工认证（人脸采集/验证码）；前端点按钮后继续。"""
        event = threading.Event()
        self._human_events[job.job_id] = event
        job.human = message
        job.add("warn", message + "（在页面上点「我已完成认证」继续）")
        handled = False
        try:
            # 分段等：用户点「停止」时也要能立刻结束，不要干等 10 分钟
            for _ in range(60):
                if job.stop:
                    break
                if await asyncio.to_thread(event.wait, 10):
                    handled = True
                    break
        finally:
            job.human = ""
            self._human_events.pop(job.job_id, None)
        if handled:
            job.add("ok", "人工认证已完成，任务继续")
        return bool(handled)

    def human_done(self, job_id: str) -> bool:
        """前端按钮：告诉正在等待的任务「人已经处理完了」。"""
        event = self._human_events.get(job_id)
        if event is None:
            return False
        event.set()
        return True

    def live(self) -> bytes | None:
        return self._job.live if self._job is not None else None

    # -- 课程与任务点（同步接口，内部跑 asyncio） -------------------------

    def courses(self, *, refresh: bool = False) -> list[dict[str, str]]:
        if self._courses and not refresh and time.time() - self._courses_at < 300:
            return self._courses

        async def run() -> list[dict[str, str]]:
            async with PwBrowser(self._config()) as browser:
                page = await browser.new_page()
                await self._ensure_login(page)
                html = await page.fetch_text(COURSE_LIST_URL)
                return [
                    {
                        "course_id": course.raw.get("courseid", ""),
                        "clazz_id": course.raw.get("clazzid", ""),
                        "name": course.name or course.remote_id,
                    }
                    for course in parsing.parse_courses(html)
                ]

        data = asyncio.run(run())
        self._courses = data
        self._courses_at = time.time()
        return data

    def tasks(self, course_id: str, clazz_id: str) -> dict[str, Any]:
        async def run() -> dict[str, Any]:
            async with PwBrowser(self._config()) as browser:
                page = await browser.new_page()
                await self._ensure_login(page)
                study = ChaoxingStudy(page, course_id=course_id, clazz_id=clazz_id)
                await study.open_course()
                points = await study.task_points(only_pending=True)
                return {
                    "course_id": course_id,
                    "clazz_id": clazz_id,
                    "tasks": [
                        {
                            "kind": point.kind,
                            "chapter_id": point.chapter_id,
                            "index": point.index_text,
                            "title": point.section_title,
                            "name": point.name,
                            "duration": point.duration,
                            "passed": point.passed,
                        }
                        for point in points
                    ],
                }

        return asyncio.run(run())

    # -- 后台任务 ---------------------------------------------------------

    def start(self, kind: str, label: str, worker) -> dict[str, Any]:
        with self._lock:
            if self._job is not None and self._job.status == "running":
                raise RuntimeError(f"已有任务在跑（{self._job.label}），先等它结束")
            self._seq += 1
            job = Job(job_id=f"j{self._seq}", kind=kind, label=label)
            self._job = job

        def emit(level: str, message: str) -> None:
            job.add(level, message)

        def runner() -> None:
            job.add("info", f"任务开始：{label}")
            try:
                summary = worker(job, emit)
                job.summary = summary or "完成"
                job.status = "done"
                job.add("ok", f"任务结束：{job.summary}")
            except Exception as exc:  # noqa: BLE001 - 失败要落在日志里而不是吞掉
                log.exception("任务失败")
                job.status = "failed"
                job.summary = f"{type(exc).__name__}: {exc}"
                job.add("error", f"任务失败：{exc}")
            finally:
                job.finished_at = time.time()

        threading.Thread(target=runner, name=f"chaoxing-{kind}", daemon=True).start()
        return job.to_dict()

    def job(self) -> dict[str, Any] | None:
        return self._job.to_dict() if self._job is not None else None

    def stop(self) -> bool:
        if self._job is not None and self._job.status == "running":
            self._job.stop = True
            self._job.add("warn", "收到停止请求……")
            return True
        return False

    # -- 两个动作的 worker ------------------------------------------------

    def study(self, *, course_id: str, clazz_id: str, limit: int, rate: float,
              kinds: str, doc_seconds: float) -> dict[str, Any]:
        def worker(job: Job, emit) -> str:
            async def run() -> str:
                async with PwBrowser(self._config()) as browser:
                    page = await browser.new_page()
                    await self._ensure_login(page)
                    live = asyncio.create_task(self._live_loop(page, job))
                    try:
                        study = ChaoxingStudy(
                            page,
                            course_id=course_id,
                            clazz_id=clazz_id,
                            emit=emit,
                            rate=rate,
                            human_gate=lambda kind, message: self._wait_human(
                                job, kind, message
                            ),
                        )
                        report = await study.run(
                            kinds=tuple(
                                k.strip() for k in kinds.split(",") if k.strip()
                            ),
                            limit=limit,
                            doc_seconds=doc_seconds,
                            cancel=lambda: job.stop,
                        )
                        return report.summary()
                    finally:
                        live.cancel()

            return asyncio.run(run())

        return self.start("study", f"刷课 {course_id}", worker)

    def quiz(self, *, course_id: str, clazz_id: str, chapter_id: str,
             confirm: bool) -> dict[str, Any]:
        def worker(job: Job, emit) -> str:
            async def run() -> str:
                answerer = _answerer()
                async with PwBrowser(self._config()) as browser:
                    page = await browser.new_page()
                    await self._ensure_login(page)
                    live = asyncio.create_task(self._live_loop(page, job))
                    try:
                        runner = ChaoxingQuiz(
                            page,
                            course_id=course_id,
                            clazz_id=clazz_id,
                            answerer=answerer,
                            emit=emit,
                            human_gate=lambda kind, message: self._wait_human(
                                job, kind, message
                            ),
                        )
                        report = await runner.run_section_quiz(
                            chapter_id, confirm=confirm
                        )
                    finally:
                        live.cancel()
                for item in report.items:
                    emit(
                        "ok" if item.verified else "warn",
                        f"{item.number}. {item.type_name} → {item.answer or '（未答）'}"
                        f"（{item.confidence:.2f} {item.source}）",
                    )
                if report.submitted:
                    job.score = report.score
                    job.score_text = report.score_text or report.submit_note
                return report.summary()

            return asyncio.run(run())

        mode = "填卷并交卷" if confirm else "干跑填卷"
        return self.start("quiz", f"{mode} {chapter_id}", worker)

    # -- 考试 / 作业 ------------------------------------------------------

    def exams(self, course_id: str, clazz_id: str, *, refresh: bool = False
              ) -> list[dict[str, Any]]:
        """考试/作业列表（含待做/已完成/只允许手机 APP 等状态）。"""
        key = (course_id, clazz_id)
        cached = self._exams.get(key)
        if cached and not refresh and time.time() - cached[0] < 120:
            return cached[1]

        async def run() -> list[dict[str, Any]]:
            async with PwBrowser(self._config()) as browser:
                page = await browser.new_page()
                await self._ensure_login(page)
                runner = ChaoxingQuiz(
                    page, course_id=course_id, clazz_id=clazz_id
                )
                items = await runner.list_exams()
                return [
                    {
                        "title": item.title,
                        "status": item.status,
                        "exam_id": item.exam_id,
                        "relation_id": item.relation_id,
                        "end_time": item.end_time_text,
                        "mobile_only": item.mobile_only,
                        "action": item.action,
                    }
                    for item in items
                ]

        data = asyncio.run(run())
        self._exams[key] = (time.time(), data)
        return data

    def exam(self, *, course_id: str, clazz_id: str, exam_id: str,
             title: str, relation_id: str = "", confirm: bool = False
             ) -> dict[str, Any]:
        """做一场「网页端可做」的考试/作业：干跑填卷或交卷并回读分数。"""
        def worker(job: Job, emit) -> str:
            async def run() -> str:
                from kedaibiao.platforms.chaoxing.quiz import ExamItem

                answerer = _answerer()
                item = ExamItem(
                    title=title or exam_id,
                    status="todo",
                    exam_id=exam_id,
                    relation_id=relation_id,
                    action="goTest",
                )
                async with PwBrowser(self._config()) as browser:
                    page = await browser.new_page()
                    await self._ensure_login(page)
                    live = asyncio.create_task(self._live_loop(page, job))
                    try:
                        runner = ChaoxingQuiz(
                            page,
                            course_id=course_id,
                            clazz_id=clazz_id,
                            answerer=answerer,
                            emit=emit,
                            human_gate=lambda kind, message: self._wait_human(
                                job, kind, message
                            ),
                        )
                        report = await runner.run_exam(item, confirm=confirm)
                    finally:
                        live.cancel()
                for plan_item in report.items:
                    emit(
                        "ok" if plan_item.verified else "warn",
                        f"{plan_item.number}. {plan_item.type_name} → "
                        f"{plan_item.answer or '（未答）'}"
                        f"（{plan_item.confidence:.2f} {plan_item.source}）",
                    )
                if report.submitted:
                    job.score = report.score
                    job.score_text = report.score_text or report.submit_note
                return report.summary()

            return asyncio.run(run())

        mode = "考试交卷" if confirm else "考试干跑"
        return self.start("exam", f"{mode} {title or exam_id}", worker)


def _answerer() -> Any:
    """优先大模型；连不上就退到规则答题器（置信度低，报告里能看到）。"""
    from kedaibiao.tasks.answerer import ChainAnswerer, RuleAnswerer
    from kedaibiao.tasks.llm import build_llm_answerer

    env = dict(os.environ)
    env_path = ROOT / ".env"
    if env_path.is_file():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                env.setdefault(key.strip(), value.strip())
    llm = build_llm_answerer(env)
    chain = [RuleAnswerer()]
    if llm is not None:
        chain.insert(0, llm)
    return ChainAnswerer(chain, min_confidence=0.0)


# ---------------------------------------------------------------------------
# 页面
# ---------------------------------------------------------------------------


PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>学习通 · 自动化台</title>
<style>
  :root{--bg:#0f1218;--panel:#161b24;--panel2:#1c2230;--line:#262e3d;--fg:#e6ebf3;
        --muted:#8b97a8;--accent:#4f8cff;--ok:#3ecf8e;--warn:#f5b942;--bad:#ff6b6b}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--fg);
       font:14px/1.6 -apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
  header{display:flex;align-items:center;gap:12px;padding:12px 18px;
         border-bottom:1px solid var(--line);background:var(--panel);position:sticky;top:0}
  .brand{font-weight:600}
  .muted{color:var(--muted)}
  main{display:grid;grid-template-columns:260px 1fr;min-height:calc(100vh - 55px)}
  aside{border-right:1px solid var(--line);padding:12px;background:var(--panel)}
  .course{padding:8px 10px;border-radius:8px;cursor:pointer;margin-bottom:6px;
          border:1px solid transparent}
  .course:hover{border-color:var(--line)}
  .course.active{background:var(--panel2);border-color:var(--accent)}
  section{padding:16px 20px}
  .tabs{display:flex;gap:8px;margin-bottom:14px}
  .tab{padding:6px 14px;border:1px solid var(--line);border-radius:8px;cursor:pointer;
       color:var(--muted)}
  .tab.active{color:var(--fg);border-color:var(--accent);background:var(--panel2)}
  .btn{background:var(--panel2);color:var(--fg);border:1px solid var(--line);
       border-radius:8px;padding:6px 12px;cursor:pointer}
  .btn.primary{background:var(--accent);border-color:var(--accent);color:#fff}
  .btn.danger{background:#3a1d22;border-color:#5c2a32;color:#ffb4bd}
  .btn:disabled{opacity:.45;cursor:not-allowed}
  table{width:100%;border-collapse:collapse}
  th,td{padding:7px 8px;border-bottom:1px solid var(--line);text-align:left;font-size:13px}
  th{color:var(--muted);font-weight:500}
  tr:hover td{background:var(--panel)}
  .pill{display:inline-block;padding:1px 8px;border-radius:99px;font-size:12px;
        border:1px solid var(--line)}
  .pill.ok{color:var(--ok);border-color:#235c42}
  .pill.warn{color:var(--warn);border-color:#5c4a1d}
  .pill.bad{color:var(--bad);border-color:#5c2a32}
  .controls{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:10px 0 14px}
  input[type=number]{width:80px;background:var(--panel2);color:var(--fg);
        border:1px solid var(--line);border-radius:6px;padding:5px 8px}
  #score{display:none;margin:0 0 10px;padding:10px 14px;border-radius:10px;
         border:1px solid #235c42;background:#12211b;color:var(--ok);font-weight:600}
  #human{display:none;margin:0 0 10px;padding:10px 14px;border-radius:10px;
         border:1px solid #5c4a1d;background:#221d12;color:var(--warn)}
  #livebox{display:none;margin-bottom:10px;border:1px solid var(--line);border-radius:10px;
           overflow:hidden;background:#000}
  #livebox img{width:100%;display:block}
  #log{background:#0b0e13;border:1px solid var(--line);border-radius:10px;padding:12px;
       height:calc(100vh - 320px);overflow:auto;font-family:ui-monospace,Consolas,monospace;
       font-size:12.5px;white-space:pre-wrap}
  .l-ok{color:var(--ok)} .l-warn{color:var(--warn)} .l-error{color:var(--bad)}
  .l-info{color:#9fb3cc}
  .hint{color:var(--muted);font-size:12.5px}
  label.chk{display:flex;align-items:center;gap:6px;color:var(--muted);cursor:pointer}
</style></head><body>
<header>
  <div class="brand">学习通 · 自动化台</div>
  <span class="muted" id="state">正在检查登录态…</span>
  <label class="chk"><input type="checkbox" id="livechk">前台显示</label>
  <button class="btn" id="login">账号密码登录</button>
  <button class="btn" id="stop">停止当前任务</button>
</header>
<main>
  <aside>
    <div class="muted" style="margin:4px 8px 8px">课程</div>
    <div id="courses"></div>
  </aside>
  <section>
    <div class="tabs">
      <div class="tab active" data-tab="tasks">任务点 / 刷课</div>
      <div class="tab" data-tab="quiz">章节测验</div>
      <div class="tab" data-tab="exam">作业 / 考试</div>
      <div class="tab" data-tab="log">运行日志</div>
    </div>
    <div id="pane-tasks">
      <div class="controls">
        <label>最多任务点 <input type="number" id="limit" value="0" min="0"></label>
        <label>倍速 <input type="number" id="rate" value="2" min="1" max="2" step="0.5"></label>
        <label>文档停留(秒) <input type="number" id="docsec" value="0" min="0"></label>
        <button class="btn primary" id="study">开始刷课（写操作）</button>
        <button class="btn" id="refresh">刷新任务点</button>
      </div>
      <p class="hint">刷课会真的播放视频/打开文档，进度由播放器自己上报；
        完成判定回读平台记录。人脸采集会暂停，等你点「我已完成认证」。</p>
      <table><thead><tr><th>类型</th><th>小节</th><th>名称</th><th>时长</th><th>状态</th></tr></thead>
      <tbody id="tasks"></tbody></table>
    </div>
    <div id="pane-quiz" style="display:none">
      <p class="hint">刷题默认干跑：解析题面（含公式截图，交给视觉模型）→ 作答 → 填卷 →
        逐题回读核验，不点提交。点「填卷并交卷」才真交，交完显示平台回显的分数。</p>
      <table><thead><tr><th>小节</th><th>标题</th><th>操作</th></tr></thead>
      <tbody id="quizzes"></tbody></table>
    </div>
    <div id="pane-exam" style="display:none">
      <div class="controls">
        <button class="btn" id="refresh-exams">刷新考试列表</button>
        <span class="hint">只允许手机 APP 的条目网页端做不了，会标出来。</span>
      </div>
      <table><thead><tr><th>名称</th><th>状态</th><th>截止</th><th>操作</th></tr></thead>
      <tbody id="exams"></tbody></table>
    </div>
    <div id="pane-log" style="display:none">
      <div id="score"></div>
      <div id="human"></div>
      <div id="livebox"><img id="live" alt="前台画面"></div>
      <div id="log">等待任务……</div>
    </div>
  </section>
</main>
<script>
var current = null, courses = [], timer = null, liveTimer = null, tab = 'tasks';
function q(id){return document.getElementById(id)}
function api(path, body){
  var opt = body ? {method:'POST', headers:{'Content-Type':'application/json'},
                    body: JSON.stringify(body)} : {};
  return fetch(path, opt).then(function(r){return r.json()});
}
function setState(text, good){
  var el = q('state'); el.textContent = text;
  el.style.color = good ? 'var(--ok)' : 'var(--muted)';
}
function pill(kind, passed){
  if (kind === 'quiz') return '<span class="pill">测验</span>';
  return passed === true ? '<span class="pill ok">已完成</span>'
                         : '<span class="pill warn">待完成</span>';
}
function statusPill(status){
  var map = {todo:'<span class="pill warn">待做</span>',
             done:'<span class="pill ok">已完成</span>',
             expired:'<span class="pill bad">已过期</span>'};
  return map[status] || '<span class="pill">' + status + '</span>';
}
function cell(text){ return '<td>' + text + '</td>'; }
function renderCourses(){
  q('courses').innerHTML = courses.map(function(c){
    return '<div class="course' + (current && current.course_id === c.course_id ? ' active' : '') +
      '" data-id="' + c.course_id + '" data-clazz="' + c.clazz_id + '">' +
      c.name + '<div class="muted" style="font-size:12px">' + c.course_id + '</div></div>';
  }).join('') || '<div class="muted" style="padding:8px">没有课程</div>';
  Array.prototype.forEach.call(document.querySelectorAll('.course'), function(el){
    el.onclick = function(){ selectCourse(el.dataset.id, el.dataset.clazz); };
  });
}
function selectCourse(id, clazz){
  current = {course_id: id, clazz_id: clazz};
  renderCourses(); loadTasks();
  if (tab === 'exam') loadExams();
}
function loadTasks(){
  if (!current) return;
  q('tasks').innerHTML = '<tr><td colspan="5" class="muted">读取任务点…（首次要几十秒）</td></tr>';
  api('/api/tasks?course=' + current.course_id + '&clazz=' + current.clazz_id)
    .then(function(res){
    if (res.error) {
      q('tasks').innerHTML = '<tr><td colspan="5">' + res.error + '</td></tr>';
      return;
    }
    var rows = [], quizzes = [];
    (res.tasks || []).forEach(function(t){
      if (t.kind === 'quiz') { quizzes.push(t); return; }
      rows.push('<tr>' + cell(t.kind) + cell(t.index) + cell(t.name) +
        cell(Math.round(t.duration / 60) + ' 分') + cell(pill(t.kind, t.passed)) + '</tr>');
    });
    q('tasks').innerHTML = rows.join('') ||
      '<tr><td colspan="5" class="muted">没有待办任务点</td></tr>';
    q('quizzes').innerHTML = quizzes.map(function(t){
      var dry = '<button class="btn" data-act="dry" data-ch="' + t.chapter_id +
        '">干跑填卷</button> ';
      var real = '<button class="btn danger" data-act="confirm" data-ch="' +
        t.chapter_id + '">填卷并交卷</button>';
      return '<tr>' + cell(t.index) + cell(t.name) + cell(dry + real) + '</tr>';
    }).join('') || '<tr><td colspan="3" class="muted">这门课没有章节测验</td></tr>';
    wireQuizButtons();
  });
}
function wireQuizButtons(){
  Array.prototype.forEach.call(document.querySelectorAll('#quizzes button'), function(btn){
    btn.onclick = function(){ startQuiz(btn.dataset.ch, btn.dataset.act === 'confirm'); };
  });
}
function loadExams(){
  if (!current) return;
  q('exams').innerHTML = '<tr><td colspan="4" class="muted">读取考试列表…</td></tr>';
  api('/api/exams?course=' + current.course_id + '&clazz=' + current.clazz_id)
    .then(function(res){
    if (res.error) {
      q('exams').innerHTML = '<tr><td colspan="4">' + res.error + '</td></tr>';
      return;
    }
    var rows = (res.exams || []).map(function(e){
      var ops = '<span class="muted">—</span>';
      if (e.mobile_only) {
        ops = '<span class="pill bad">只允许手机 APP</span>';
      } else if (e.status === 'todo') {
        ops = '<button class="btn" data-act="dry" data-exam="' + e.exam_id +
          '" data-title="' + e.title.replace(/"/g, '&quot;') +
          '" data-rel="' + e.relation_id + '">干跑填卷</button> ' +
          '<button class="btn danger" data-act="confirm" data-exam="' + e.exam_id +
          '" data-title="' + e.title.replace(/"/g, '&quot;') +
          '" data-rel="' + e.relation_id + '">交卷</button>';
      } else if (e.action === 'viewExamAnswer') {
        ops = '<span class="muted">已交卷（可查看答案）</span>';
      }
      return '<tr>' + cell(e.title) + cell(statusPill(e.status)) +
        cell(e.end_time || '—') + cell(ops) + '</tr>';
    });
    q('exams').innerHTML = rows.join('') ||
      '<tr><td colspan="4" class="muted">没有考试/作业</td></tr>';
    Array.prototype.forEach.call(document.querySelectorAll('#exams button'), function(btn){
      btn.onclick = function(){
        startExam(btn.dataset.exam, btn.dataset.title, btn.dataset.rel,
                  btn.dataset.act === 'confirm');
      };
    });
  });
}
function startStudy(){
  if (!current) return;
  var body = {course: current.course_id, clazz: current.clazz_id,
              limit: +q('limit').value || 0, rate: +q('rate').value || 2,
              doc_seconds: +q('docsec').value || 0, kinds: 'video,document'};
  api('/api/study', body).then(handleStart);
}
function startQuiz(chapter, confirm){
  if (!current) return;
  if (confirm && !window.confirm('交卷不可逆，确认提交？')) return;
  api('/api/quiz', {course: current.course_id, clazz: current.clazz_id,
                    chapter: chapter, confirm: confirm}).then(handleStart);
}
function startExam(examId, title, rel, confirm){
  if (!current) return;
  if (confirm && !window.confirm('考试交卷不可逆，确认提交？')) return;
  api('/api/exam', {course: current.course_id, clazz: current.clazz_id,
                    exam_id: examId, title: title, relation_id: rel,
                    confirm: confirm}).then(handleStart);
}
function handleStart(res){
  if (res.error) { alert(res.error); return; }
  switchTab('log'); pollJob();
}
function switchTab(name){
  tab = name;
  Array.prototype.forEach.call(document.querySelectorAll('.tab'), function(el){
    el.classList.toggle('active', el.dataset.tab === name);
  });
  q('pane-tasks').style.display = name === 'tasks' ? '' : 'none';
  q('pane-quiz').style.display = name === 'quiz' ? '' : 'none';
  q('pane-exam').style.display = name === 'exam' ? '' : 'none';
  q('pane-log').style.display = name === 'log' ? '' : 'none';
  if (name === 'exam') loadExams();
}
function renderLive(hasLive){
  var box = q('livebox');
  if (q('livechk').checked && hasLive) {
    box.style.display = 'block';
    q('live').src = '/api/live?t=' + Date.now();
  } else {
    box.style.display = 'none';
  }
}
function pollJob(){
  api('/api/job').then(function(payload){
    var job = payload.job;
    if (!job) return;
    var log = q('log');
    log.innerHTML = (job.logs || []).map(function(item){
      return '<div class="l-' + item.level + '">[' + item.t + '] ' + item.message + '</div>';
    }).join('');
    log.scrollTop = log.scrollHeight;
    setState(job.status === 'running' ? ('运行中：' + job.label) : ('上次任务：' + job.summary),
             job.status === 'done');
    var score = q('score');
    if (job.score_text || job.score !== null && job.score !== undefined) {
      score.style.display = 'block';
      score.textContent = '交卷结果：' + (job.score_text ||
        ('得分 ' + job.score)) + '（' + (job.summary || '') + '）';
    } else {
      score.style.display = 'none';
    }
    var human = q('human');
    if (job.human) {
      human.style.display = 'block';
      human.innerHTML = job.human +
        ' <button class="btn" id="human-done">我已完成认证，继续</button>';
      q('human-done').onclick = function(){
        api('/api/human', {job_id: job.job_id}).then(function(){ human.style.display = 'none'; });
      };
    } else {
      human.style.display = 'none';
    }
    renderLive(job.has_live);
    q('study').disabled = job.status === 'running';
    clearTimeout(timer);
    if (job.status === 'running') timer = setTimeout(pollJob, 1500);
    else if (tab === 'tasks') loadTasks();
  });
}
q('study').onclick = startStudy;
q('refresh').onclick = loadTasks;
q('refresh-exams').onclick = loadExams;
q('stop').onclick = function(){ api('/api/stop', {}).then(pollJob); };
q('livechk').onchange = function(){
  if (q('livechk').checked) {
    clearInterval(liveTimer);
    liveTimer = setInterval(function(){ renderLive(true); }, 2000);
  } else {
    clearInterval(liveTimer);
    q('livebox').style.display = 'none';
  }
};
q('login').onclick = function(){
  var user = prompt('手机号 / 超星号'); if (!user) return;
  var pwd = prompt('密码（只用于这一次登录，不落盘）'); if (!pwd) return;
  api('/api/login', {user: user, password: pwd}).then(function(res){
    if (res.error) alert(res.error); else { setState('登录成功', true); boot(); }
  });
};
Array.prototype.forEach.call(document.querySelectorAll('.tab'), function(el){
  el.onclick = function(){ switchTab(el.dataset.tab); };
});
function boot(){
  api('/api/state').then(function(res){
    if (res.logged_in) setState('已登录：' + (res.account || '学习通账号'), true);
    else setState('未登录：请先点右上角「账号密码登录」，或运行 chaoxing_auto.py login', false);
    courses = res.courses || [];
    renderCourses();
    if (courses.length) selectCourse(courses[0].course_id, courses[0].clazz_id);
  });
}
boot();
</script></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    studio: Studio

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003 - 基类接口
        log.debug(fmt, *args)

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, payload: dict[str, Any]) -> None:
        self._send(code, json.dumps(payload, ensure_ascii=False).encode(), "application/json")

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    def do_GET(self) -> None:  # noqa: N802 - 基类接口
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            self._send(200, PAGE.encode(), "text/html; charset=utf-8")
            return
        if parsed.path == "/api/state":
            self._json(200, self._state())
            return
        if parsed.path == "/api/tasks":
            query = parse_qs(parsed.query)
            course = (query.get("course") or [""])[0]
            clazz = (query.get("clazz") or [""])[0]
            try:
                self._json(200, self.studio.tasks(course, clazz))
            except Exception as exc:  # noqa: BLE001
                log.exception("读取任务点失败")
                self._json(200, {"error": f"{type(exc).__name__}: {exc}"})
            return
        if parsed.path == "/api/job":
            self._json(200, {"job": self.studio.job()})
            return
        if parsed.path == "/api/live":
            frame = self.studio.live()
            if frame is None:
                self._send(404, b"no frame", "text/plain")
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(frame)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(frame)
            return
        if parsed.path == "/api/exams":
            query = parse_qs(parsed.query)
            course = (query.get("course") or [""])[0]
            clazz = (query.get("clazz") or [""])[0]
            try:
                self._json(200, {"exams": self.studio.exams(course, clazz)})
            except Exception as exc:  # noqa: BLE001
                log.exception("读取考试列表失败")
                self._json(200, {"error": f"{type(exc).__name__}: {exc}"})
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - 基类接口
        parsed = urlparse(self.path)
        body = self._body()
        try:
            if parsed.path == "/api/login":
                self._json(200, self._login(body))
            elif parsed.path == "/api/study":
                self._json(200, self.studio.study(
                    course_id=str(body.get("course") or ""),
                    clazz_id=str(body.get("clazz") or ""),
                    limit=int(body.get("limit") or 0),
                    rate=float(body.get("rate") or 2.0),
                    kinds=str(body.get("kinds") or "video,document"),
                    doc_seconds=float(body.get("doc_seconds") or 0),
                ))
            elif parsed.path == "/api/quiz":
                self._json(200, self.studio.quiz(
                    course_id=str(body.get("course") or ""),
                    clazz_id=str(body.get("clazz") or ""),
                    chapter_id=str(body.get("chapter") or ""),
                    confirm=bool(body.get("confirm")),
                ))
            elif parsed.path == "/api/exam":
                self._json(200, self.studio.exam(
                    course_id=str(body.get("course") or ""),
                    clazz_id=str(body.get("clazz") or ""),
                    exam_id=str(body.get("exam_id") or ""),
                    title=str(body.get("title") or ""),
                    relation_id=str(body.get("relation_id") or ""),
                    confirm=bool(body.get("confirm")),
                ))
            elif parsed.path == "/api/human":
                self._json(200, {
                    "ok": self.studio.human_done(str(body.get("job_id") or ""))
                })
            elif parsed.path == "/api/stop":
                self._json(200, {"stopped": self.studio.stop()})
            else:
                self._json(404, {"error": "not found"})
        except RuntimeError as exc:
            self._json(200, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            log.exception("接口失败")
            self._json(200, {"error": f"{type(exc).__name__}: {exc}"})

    def _state(self) -> dict[str, Any]:
        settings = _env_map()
        logged_in = self.studio.state.is_file()
        try:
            courses = self.studio.courses() if logged_in else []
        except Exception as exc:  # noqa: BLE001 - 会话失效时页面也要能开
            log.warning("课程列表读取失败: %s", exc)
            courses = []
            logged_in = False
        return {
            "logged_in": logged_in,
            "account": settings.get("CHAOXING_USER", ""),
            "state": str(self.studio.state),
            "courses": courses,
        }

    def _login(self, body: dict[str, Any]) -> dict[str, Any]:
        user = str(body.get("user") or "").strip()
        password = str(body.get("password") or "")
        if not user or not password:
            return {"error": "账号和密码都不能为空"}

        def run() -> None:
            async def inner() -> None:
                async with PwBrowser(self.studio._config()) as browser:  # noqa: SLF001
                    page = await browser.new_page()
                    await login.login_with_password(page, user, password)
                    await browser.save_state(self.studio.state)

            asyncio.run(inner())

        try:
            threading.Thread(target=run, daemon=True).start()
            time.sleep(0.1)
            return {"ok": True}
        except Exception as exc:  # noqa: BLE001
            return {"error": f"{type(exc).__name__}: {exc}"}


def _env_map() -> dict[str, str]:
    env = dict(os.environ)
    env_path = ROOT / ".env"
    if env_path.is_file():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                env.setdefault(key.strip(), value.strip())
    return env


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="学习通自动化台（本地网页）")
    parser.add_argument("--host", default="127.0.0.1", help="只建议绑回环地址")
    parser.add_argument("--port", type=int, default=8900)
    parser.add_argument("--state", default=str(STATE))
    parser.add_argument("--show", action="store_true", help="显示浏览器（人工过验证码/人脸）")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(message)s")
    studio = Studio(state=Path(args.state), headless=not args.show)
    Handler.studio = studio
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"学习通自动化台: http://{args.host}:{args.port}/")
    print(f"会话文件: {studio.state}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
