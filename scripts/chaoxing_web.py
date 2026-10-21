#!/usr/bin/env python3
"""超星 / 学习通 只读看板（本地网页版）。

## 它做什么

在你自己的机器上起一个**只绑回环地址**的小服务，把「哪门课、有什么作业、
什么时候截止」渲染成一张页面。数据只来自 GET 请求。

## 它不做什么

没有提交、没有进度上报、没有签到 —— 后端只有一个取数接口，前端也只有一个
刷新按钮。想加写操作，得另外写。

## 用法

```bash
# 1) 起服务（会话可以用 --profile 从已登录的 Chrome 用户目录里取）
python3 scripts/chaoxing_web.py --profile ~/.config/google-chrome

# 2) 打开 http://127.0.0.1:8899/
#    没给 --profile/--cookie 时，页面会要求你贴一次 Cookie 头
```

Cookie 只在两个地方：服务进程内存、浏览器 ``sessionStorage``。不落盘、不进日志。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kedaibiao.browser.capture import CaptureError, capture_cookies  # noqa: E402
from kedaibiao.platforms.chaoxing import client, endpoints  # noqa: E402

log = logging.getLogger("chaoxing-web")

INDEX_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>学习通 · 只读看板</title>
<style>
  :root {
    --bg: #0f1218; --panel: #161b24; --panel-2: #1c2230; --line: #262e3d;
    --fg: #e6ebf3; --muted: #8b97a8; --accent: #4f8cff;
    --ok: #3ecf8e; --warn: #f5b942; --bad: #ff6b6b;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--fg);
    font: 14px/1.6 -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
  }
  header {
    display: flex; align-items: center; justify-content: space-between;
    padding: 14px 20px; border-bottom: 1px solid var(--line); background: var(--panel);
    position: sticky; top: 0; z-index: 10;
  }
  .brand { display: flex; align-items: center; gap: 10px; font-weight: 600; }
  .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--muted); }
  .dot.on { background: var(--ok); box-shadow: 0 0 8px var(--ok); }
  .dot.bad { background: var(--bad); }
  .actions { display: flex; align-items: center; gap: 10px; }
  .btn {
    background: var(--panel-2); color: var(--fg); border: 1px solid var(--line);
    border-radius: 8px; padding: 7px 14px; cursor: pointer; font-size: 13px;
  }
  .btn:hover { border-color: var(--accent); }
  .btn.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
  .btn.ghost { background: transparent; }
  main { display: grid; grid-template-columns: 240px 1fr; min-height: calc(100vh - 57px); }
  aside { border-right: 1px solid var(--line); padding: 16px 12px; background: var(--panel); }
  .aside-title { color: var(--muted); font-size: 12px; letter-spacing: .08em; margin: 0 8px 8px; }
  #courseList { list-style: none; margin: 0; padding: 0; }
  #courseList li {
    padding: 8px 10px; border-radius: 8px; cursor: pointer; margin-bottom: 2px;
    display: flex; justify-content: space-between; gap: 8px; align-items: center;
  }
  #courseList li:hover { background: var(--panel-2); }
  #courseList li.active { background: var(--panel-2); outline: 1px solid var(--line); }
  #courseList .n { color: var(--muted); font-size: 12px; }
  section { padding: 16px 20px; }
  .toolbar {
    display: flex; justify-content: space-between; align-items: center;
    margin-bottom: 14px;
  }
  .chips { display: flex; gap: 8px; }
  .chip {
    background: transparent; border: 1px solid var(--line); color: var(--muted);
    border-radius: 999px; padding: 5px 14px; cursor: pointer; font-size: 13px;
  }
  .chip.active { border-color: var(--accent); color: var(--fg); background: rgba(79,140,255,.12); }
  .muted { color: var(--muted); font-size: 13px; }
  .card {
    background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
    padding: 14px 16px; margin-bottom: 10px; display: flex; gap: 14px; align-items: flex-start;
  }
  .card.done { opacity: .55; }
  .card.overdue { border-color: rgba(255,107,107,.45); }
  .badge {
    font-size: 12px; padding: 2px 8px; border-radius: 6px; white-space: nowrap;
    background: rgba(79,140,255,.15); color: #9ec0ff; margin-top: 2px;
  }
  .badge.exam { background: rgba(245,185,66,.15); color: var(--warn); }
  .body { flex: 1; min-width: 0; }
  .title { font-weight: 600; margin-bottom: 2px; }
  .meta { color: var(--muted); font-size: 12.5px; display: flex; gap: 14px; flex-wrap: wrap; }
  .due-ok { color: var(--muted); }
  .due-soon { color: var(--warn); }
  .due-late { color: var(--bad); }
  .right { display: flex; flex-direction: column; align-items: flex-end; gap: 6px; }
  a { color: var(--accent); text-decoration: none; font-size: 12.5px; }
  a:hover { text-decoration: underline; }
  .empty { text-align: center; color: var(--muted); padding: 60px 20px; line-height: 2; }
  dialog {
    background: var(--panel); color: var(--fg); border: 1px solid var(--line);
    border-radius: 12px; padding: 22px; width: min(620px, 92vw);
  }
  dialog::backdrop { background: rgba(0,0,0,.6); }
  dialog h2 { margin: 0 0 6px; font-size: 16px; }
  dialog p { color: var(--muted); font-size: 13px; }
  textarea {
    width: 100%; min-height: 110px; background: var(--bg); color: var(--fg);
    border: 1px solid var(--line); border-radius: 8px; padding: 10px; font: 12.5px/1.5 monospace;
    resize: vertical;
  }
  .row { display: flex; gap: 10px; justify-content: flex-end; margin-top: 14px; }
</style>
</head>
<body>
<header>
  <div class="brand"><span class="dot" id="dot"></span> 学习通 · 只读看板</div>
  <div class="actions">
    <span class="muted" id="meta"></span>
    <button class="btn" id="refresh">刷新</button>
    <button class="btn ghost" id="settings">连接设置</button>
  </div>
</header>
<main>
  <aside>
    <div class="aside-title">课程</div>
    <ul id="courseList"></ul>
  </aside>
  <section>
    <div class="toolbar">
      <div class="chips" id="filters">
        <button class="chip active" data-f="all">全部</button>
        <button class="chip" data-f="todo">未完成</button>
        <button class="chip" data-f="exam">考试</button>
        <button class="chip" data-f="homework">作业</button>
      </div>
      <div class="muted" id="count"></div>
    </div>
    <div id="list"></div>
  </section>
</main>

<dialog id="setup">
  <h2>连接学习通</h2>
  <p>在已登录学习通的浏览器里打开 DevTools → Network → 任选一个请求 →
     复制请求头 <code>Cookie</code> 的整串值，粘到下面。
     Cookie 只存在本标签页与本地服务内存里，不落盘。</p>
  <textarea id="cookieInput" placeholder="fid=...; _uid=...; ..." spellcheck="false"></textarea>
  <div class="row">
    <button class="btn ghost" id="cancelSetup">取消</button>
    <button class="btn primary" id="saveSetup">保存并加载</button>
  </div>
</dialog>

<script>
const state = { data: null, cookie: "", course: "all", filter: "all" };
const $ = (id) => document.getElementById(id);

function cookie() { return state.cookie || sessionStorage.getItem("cx_cookie") || ""; }

async function load() {
  const c = cookie();
  if (!c) { $("setup").showModal(); return; }
  $("meta").textContent = "加载中…";
  try {
    const res = await fetch("/api/data", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ cookie: c }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || ("HTTP " + res.status));
    state.data = data;
    $("dot").className = "dot " + (data.ok ? "on" : "bad");
    $("meta").textContent = data.ok ? `${data.courses.length} 门课` : "读取失败";
    render();
  } catch (err) {
    state.data = null;
    $("dot").className = "dot bad";
    $("meta").textContent = "加载失败：" + err.message;
    renderError(err.message);
  }
}

function allItems() {
  if (!state.data || !state.data.works) return [];
  const out = [];
  for (const group of state.data.works) {
    for (const item of group.items) out.push({ ...item, course: group.course });
  }
  return out;
}

function filtered() {
  let items = allItems();
  if (state.course !== "all") items = items.filter((i) => i.course === state.course);
  if (state.filter === "todo") items = items.filter((i) => !i.finished);
  else if (state.filter === "exam") items = items.filter((i) => i.kind === "exam");
  else if (state.filter === "homework") items = items.filter((i) => i.kind === "homework");
  return items.sort((a, b) => {
    if (a.finished !== b.finished) return a.finished ? 1 : -1;
    if (!a.deadline) return 1;
    if (!b.deadline) return -1;
    return a.deadline < b.deadline ? -1 : 1;
  });
}

function dueInfo(iso, finished) {
  if (!iso) return { text: "无截止时间", cls: "due-ok" };
  const d = new Date(iso);
  const now = new Date();
  const diff = d - now;
  const pad = (n) => String(n).padStart(2, "0");
  const datePart = d.getFullYear() === now.getFullYear()
    ? `${d.getMonth() + 1}-${d.getDate()}`
    : `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  const abs = `${datePart} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  if (finished) return { text: `截止 ${abs}`, cls: "due-ok" };
  if (diff < 0) return { text: `已逾期 · ${abs}`, cls: "due-late" };
  const hours = Math.floor(diff / 3600000);
  if (hours < 24) return { text: `还剩 ${hours} 小时 · ${abs}`, cls: "due-soon" };
  return { text: `还剩 ${Math.floor(hours / 24)} 天 · ${abs}`, cls: "due-ok" };
}

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}

function render() {
  const list = $("list");
  const courseList = $("courseList");
  list.replaceChildren(); courseList.replaceChildren();

  if (!state.data || !state.data.ok) {
    renderError(state.data ? "没有读到课程" : "尚未连接");
    return;
  }

  const items = allItems();
  const addCourse = (key, label, count) => {
    const li = el("li", state.course === key ? "active" : "");
    li.append(el("span", "", label), el("span", "n", String(count)));
    li.onclick = () => { state.course = key; render(); };
    courseList.append(li);
  };
  addCourse("all", "全部课程", items.length);
  for (const course of state.data.courses) {
    const n = items.filter((i) => i.course === course.name).length;
    addCourse(course.name, course.name, n);
  }

  const rows = filtered();
  $("count").textContent = `${rows.length} 条`;
  if (!rows.length) {
    const box = el("div", "empty");
    box.append(el("div", "", "这里没有符合条件的条目"));
    box.append(el("div", "", state.data.notes.join("；") || ""));
    list.append(box);
    return;
  }
  for (const item of rows) {
    const due = dueInfo(item.deadline, item.finished);
    const classes = ["card"];
    if (item.finished) classes.push("done");
    if (due.cls === "due-late") classes.push("overdue");
    const card = el("div", classes.join(" "));
    const badge = el("span", "badge" + (item.kind === "exam" ? " exam" : ""),
                     item.kind === "exam" ? "考试" : "作业");
    card.append(badge);
    const body = el("div", "body");
    body.append(el("div", "title", item.title));
    const meta = el("div", "meta");
    meta.append(el("span", "", item.course));
    meta.append(el("span", due.cls, due.text));
    meta.append(el("span", "", item.finished ? "已完成" : "未完成"));
    body.append(meta);
    card.append(body);
    const right = el("div", "right");
    if (item.url) {
      const a = el("a", "", "打开原页面");
      a.href = item.url.startsWith("http") ? item.url : "https://mooc1.chaoxing.com" + item.url;
      a.target = "_blank"; a.rel = "noreferrer";
      right.append(a);
    }
    card.append(right);
    list.append(card);
  }
}

function renderError(message) {
  $("list").replaceChildren();
  const box = el("div", "empty");
  box.append(el("div", "", message));
  box.append(el("div", "", "检查：登录态是否失效 / 入口是否变化（跑一次 --dump 看看返回的 HTML）"));
  box.append(el("div", "", ""));
  if (state.data && state.data.notes) {
    for (const note of state.data.notes) box.append(el("div", "muted", note));
  }
  $("list").append(box);
}

$("refresh").onclick = load;
$("settings").onclick = () => $("setup").showModal();
$("cancelSetup").onclick = () => $("setup").close();
$("saveSetup").onclick = () => {
  const value = $("cookieInput").value.trim();
  if (!value) return;
  state.cookie = value;
  sessionStorage.setItem("cx_cookie", value);
  $("setup").close();
  load();
};
$("filters").onclick = (event) => {
  const chip = event.target.closest(".chip");
  if (!chip) return;
  state.filter = chip.dataset.f;
  for (const node of $("filters").querySelectorAll(".chip")) {
    node.classList.toggle("active", node === chip);
  }
  render();
};

state.cookie = sessionStorage.getItem("cx_cookie") || "";
load();
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "kedaibiao-chaoxing/1.0"
    #: 服务端默认会话（--cookie / --profile 提供）；请求里带 cookie 时优先用请求的。
    default_cookie: str = ""
    dump_dir: Path | None = None
    timeout: float = 20.0

    def log_message(self, fmt: str, *args) -> None:  # noqa: D102 - 基类接口
        # 只记方法与状态，不记请求体 —— 请求体里是 Cookie
        log.info("%s %s", self.address_string(), fmt % args)

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, payload: dict) -> None:
        self._send(code, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def do_GET(self) -> None:  # noqa: N802 - 基类接口
        if self.path in ("/", "/index.html"):
            self._send(200, INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8")
        elif self.path == "/api/health":
            self._json(200, {"ok": True, "default_cookie": bool(self.default_cookie)})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - 基类接口
        if self.path != "/api/data":
            self._json(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            self._json(400, {"error": "请求体不是合法 JSON"})
            return

        cookie = str(payload.get("cookie") or "").strip() or self.default_cookie
        if not cookie:
            self._json(400, {"error": "no_cookie"})
            return

        try:
            data = client.collect(cookie, timeout=self.timeout, dump_dir=self.dump_dir)
        except Exception as exc:  # noqa: BLE001 - 取数失败要回给页面，不能把服务打挂
            log.exception("取数失败")
            self._json(500, {"error": f"{type(exc).__name__}: {exc}"})
            return
        self._json(200, data)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="超星/学习通 只读看板（本地网页）")
    parser.add_argument("--host", default="127.0.0.1", help="绑定地址（默认仅回环）")
    parser.add_argument("--port", type=int, default=8899)
    parser.add_argument("--cookie", help="整串 Cookie 头（作为服务端默认会话）")
    parser.add_argument("--profile", help="已登录学习通的 Chrome 用户目录")
    parser.add_argument("--dump", metavar="DIR", help="把原始 HTML 落盘")
    parser.add_argument("--timeout", type=float, default=20.0)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    cookie = (args.cookie or "").strip()
    if not cookie and args.profile:
        log.info("从浏览器用户目录抓取会话: %s", args.profile)
        try:
            result = capture_cookies(
                url=endpoints.COURSE_LIST_URLS[0],
                profile=args.profile,
                domain_hint="chaoxing.com",
            )
        except CaptureError as exc:
            raise SystemExit(f"抓取会话失败: {exc}") from exc
        log.info("%s", result.summary())
        cookie = result.cookie_header

    Handler.default_cookie = cookie
    Handler.dump_dir = Path(args.dump) if args.dump else None
    Handler.timeout = args.timeout

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    shown = "127.0.0.1" if args.host in ("127.0.0.1", "localhost") else args.host
    print(f"只读看板: http://{shown}:{args.port}/", flush=True)
    if args.host not in ("127.0.0.1", "localhost"):
        print("⚠ 已绑定到非回环地址 —— 能访问该地址的设备都能读到你的课程数据。", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止", flush=True)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
