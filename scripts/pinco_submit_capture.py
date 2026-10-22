#!/usr/bin/env python3
"""品课「真提交」报文抓取：**抓一次真实提交请求，但让它不真的提交**。

## 为什么需要它

``pinco`` 的提交被 ``submit_payload_confirmed = False`` 挡住：计划与复核都能走，
最后一步拒绝发写请求。挡住的理由不是「不敢写」，而是**提交报文的确切结构没有
真实样本**（静态清单只有字段名，答案编码、必填项、是否要 duration 全没验证）。

本脚本用**已保存的登录态**打开作业页，拦截浏览器自己发出的提交请求：先把
URL / 请求头 / 请求体完整落盘，然后 ``route.abort()`` —— 请求不会到达平台，
**这次作业不会被交掉**。拿到样本后人工核对，再把
``PincoAdapter.submit_payload_confirmed`` 改 True。

## 用法

```bash
# 1) 从浏览器复制作业页 URL（打开某个作业、能看到作答内容的那一页）
python3 scripts/pinco_submit_capture.py --url '<作业页 URL>'

# 2) 在弹出的浏览器窗口里正常点「提交」—— 请求会被截获并中止
#    看到「已抓取 …」即成功；文件在 data/recon/pinco/submit-capture-*.json
```

`--headless` 只适合页面上有自动提交按钮的场景；默认有头，方便你手动点。

## 安全边界

- **只读会话**：cookie 来自控制台账号快照，不接触密码；
- **写操作全部拦截**：``Submit`` / ``Save`` / ``Update`` / ``Delete`` / ``Upload``
  / ``Create`` 这类 action 一律 abort（默认开启，``--allow-other-writes`` 可关），
  所以连草稿自动保存也不会落库；
- 抓到的仍是**你自己的作答内容**，文件落在 ``data/recon/``（已 gitignore），
  别把它提交进仓库。

退出码：0 = 抓到；2 = 没有可用会话；3 = 参数/浏览器问题；4 = 超时未抓到。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kedaibiao.platforms.pinco import endpoints as ep  # noqa: E402

PLATFORM = "pinco"

#: 需要抓的目标：三个提交动作（作业 / 章节任务 / 小组任务）
SUBMIT_ACTIONS = {
    ep.ACTION_SUBMIT_HOMEWORK,
    ep.ACTION_SUBMIT_CHAPTER_TASK,
    ep.ACTION_SUBMIT_COURSE_GROUP_TASK,
}

#: 默认拦截一切写动作（防止「只是点了一下」就把草稿/状态写到平台上）
BLOCKED_WRITE_MARKERS = ("Submit", "Save", "Update", "Delete", "Upload", "Create", "Finish")

#: 判定一个请求是不是品课的业务写请求
WRITE_HINTS = ("actionName=",)

PREFIX = "[capture]"


def _discover_session(root: Path) -> Path | None:
    """在控制台的账号目录里找唯一一份品课会话快照。"""
    folder = root / "data" / "sessions" / "accounts"
    matches = sorted(folder.glob(f"*-{PLATFORM}-*.cookies.json"))
    return matches[0] if len(matches) == 1 else None


def _load_cookies(path: Path) -> list[dict[str, Any]]:
    """读 cookie 快照（浏览器导出的 cookies 数组）。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("cookie 快照不是数组，无法注入浏览器")
    cookies: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        cookie: dict[str, Any] = {
            "name": str(item["name"]),
            "value": str(item.get("value", "")),
            "domain": str(item.get("domain") or ".seewo.com"),
            "path": str(item.get("path") or "/"),
        }
        if isinstance(item.get("expires"), (int, float)) and item["expires"] > 0:
            cookie["expires"] = float(item["expires"])
        cookies.append(cookie)
    return cookies


def action_of(url: str) -> str:
    """从 ``?actionName=...`` 里取动作名；没有就返回空串。"""
    from urllib.parse import parse_qs, urlparse

    match = parse_qs(urlparse(url).query)
    values = match.get("actionName") or []
    return values[0] if values else ""


def write_action_of(url: str) -> bool:
    """这个请求是不是「会改平台状态」的写请求。"""
    if not any(hint in url for hint in WRITE_HINTS):
        return False
    name = action_of(url)
    if name in SUBMIT_ACTIONS:
        return True
    return any(marker in name for marker in BLOCKED_WRITE_MARKERS)


def _save(out_dir: Path, record: dict[str, Any]) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = out_dir / f"submit-capture-{stamp}.json"
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _summary(record: dict[str, Any]) -> str:
    body = record.get("post_data") or ""
    try:
        parsed = json.loads(body)
    except (TypeError, ValueError):
        parsed = None
    keys = sorted(parsed) if isinstance(parsed, dict) else []
    lines = [
        f"动作   : {record.get('action')}",
        f"URL    : {record.get('url')}",
        f"方法   : {record.get('method')}",
        f"顶层键 : {keys or '（不是 JSON 对象，见文件）'}",
    ]
    if isinstance(parsed, dict):
        for key in keys:
            value = parsed[key]
            if isinstance(value, list):
                lines.append(f"  {key}: list[{len(value)}]")
            elif isinstance(value, dict):
                lines.append(f"  {key}: dict{sorted(value)}")
            else:
                lines.append(f"  {key}: {type(value).__name__} = {str(value)[:60]}")
    return "\n".join(lines)


def run(args: argparse.Namespace) -> int:
    from playwright.sync_api import sync_playwright

    root = Path(args.root)
    session_file = Path(args.session) if args.session else _discover_session(root)
    if not session_file or not session_file.is_file():
        print(f"{PREFIX} 找不到品课会话快照：{session_file}", file=sys.stderr)
        print(
            f"{PREFIX} 先在控制台「账号」页绑定一次品课账号，或用 --session 指定",
            file=sys.stderr,
        )
        return 2

    cookies = _load_cookies(session_file)
    out_dir = Path(args.out) if args.out else root / "data" / "recon" / "pinco"
    captured: list[str] = []
    blocked: list[str] = []

    def handle(route: Any, request: Any) -> None:
        url = request.url
        name = action_of(url)
        target = name in SUBMIT_ACTIONS
        should_block = target or (args.block_other_writes and write_action_of(url))
        if not should_block:
            route.continue_()
            return

        record = {
            "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "action": name,
            "url": url,
            "method": request.method,
            "headers": dict(request.headers),
            "post_data": request.post_data,
            "note": (
                "请求已被中止，未发送到平台；仅用于确认提交报文结构。"
                "该文件含本人作答内容，已落在 data/ 下（gitignore），不要提交。"
            ),
        }
        path = _save(out_dir, record)
        blocked.append(name)
        # 关键：先存后 abort —— 请求绝不落到平台上
        route.abort()
        if target and not captured:
            captured.append(str(path))
            print(f"\n{PREFIX} 已抓取提交请求（已中止，未真的提交）：{path}")
            print(_summary(record))
            print(f"\n{PREFIX} 可以关闭窗口，或继续点其它提交动作再抓几份。")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=args.headless)
        context = browser.new_context()
        context.add_cookies(cookies)
        page = context.new_page()
        if args.verbose:
            page.on("request", lambda r: print(f"{PREFIX} … {r.method} {r.url[:110]}"))
        page.route("**/*", handle)
        print(f"{PREFIX} 打开作业页：{args.url}")
        print(
            f"{PREFIX} 请在页面里点一次「提交」；请求会被截获并中止，不会真的交卷。"
        )
        page.goto(args.url, wait_until="domcontentloaded", timeout=60_000)

        deadline = time.time() + args.timeout
        while time.time() < deadline and not captured:
            page.wait_for_timeout(500)
        context.close()
        browser.close()

    if not captured:
        print(f"\n{PREFIX} 超时（{args.timeout}s）没有抓到提交请求。", file=sys.stderr)
        print(f"{PREFIX} 已拦截的写动作：{blocked or '无'}", file=sys.stderr)
        return 4
    if blocked:
        print(f"\n{PREFIX} 期间拦下的写动作：{sorted(set(blocked))}")
    print(f"\n{PREFIX} 下一步：核对抓到的请求体与 PincoAdapter._submit_payload 的差异；")
    print(f"{PREFIX} 确认无误后再把 submit_payload_confirmed 改成 True。")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="品课提交报文抓取（抓取即中止，不真的提交）")
    parser.add_argument("--url", required=True, help="作业页完整 URL（浏览器地址栏复制）")
    parser.add_argument("--session", help="品课 cookie 快照路径；默认自动找控制台落盘的那份")
    parser.add_argument("--out", help="抓取结果目录；默认 data/recon/pinco")
    parser.add_argument(
        "--root", default=str(Path(__file__).resolve().parents[1]), help="项目根目录"
    )
    parser.add_argument(
        "--headless", action="store_true", help="无头模式（默认有头，方便手动点提交）"
    )
    parser.add_argument("--timeout", type=int, default=300, help="等待多久没有抓包就退出（秒）")
    parser.add_argument("--verbose", action="store_true", help="打印每一个网络请求（调试用）")
    parser.add_argument(
        "--allow-other-writes",
        action="store_true",
        help="不拦截非提交类写动作（默认连草稿保存一起拦，避免误写）",
    )
    args = parser.parse_args(argv)
    args.block_other_writes = not args.allow_other_writes
    try:
        return run(args)
    except Exception as exc:  # noqa: BLE001 - 脚本边界：把异常变成可读退出码
        print(f"{PREFIX} 失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
