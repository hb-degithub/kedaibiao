#!/usr/bin/env python3
"""批改网（pigai）**只读**入口 —— 列出作业、看分数与逐句批注。

为什么单独写一个脚本，而不是挂 ``kedaibiao`` 子命令：

1. ``__main__.py`` / ``runtime.py`` 属**共享区**（docs/integration-guide.md §2.2），
   平台窗口不得改；框架级入口要由总 lead 统一接线。
2. ``tasks.pipeline.Pipeline`` 是为「取题 → 作答 → 提交」写的，只读适配器跑它会
   输出「全部跳过」，看不到任何作业/分数/批注。详见 adapter.py 的 docstring。
3. **框架级 bug 的绕过**：``load_settings()`` 读 ``.env`` 只放进局部字典、
   从不回写 ``os.environ``，而 ``load_credentials()`` 只读 ``os.environ`` ——
   于是 ``.env`` 里的凭据对框架不可见（``kedaibiao doctor`` 会报「未配置任何凭据」）。
   本脚本显式调用 ``load_dotenv()`` 把 ``.env`` 灌进 ``os.environ`` 来绕开。
   **这不改变「凭据只从环境变量/.env 读」的约束**，只是补上缺失的那一跳。

用法::

    python3 scripts/pigai_read.py pending            # 未完成作业（作业号 / 标题 / 截止时间）
    python3 scripts/pigai_read.py finished           # 已提交作文（作业号 / eid / 成绩 / 稿次）
    python3 scripts/pigai_read.py show <rid> <eid>   # 分数 / 四维 / 总评 / 逐句批注
    python3 scripts/pigai_read.py lookup <作业号>     # 按作业号定位一个作业

纪律：**只发 GET**（外加一次登录 POST），不提交任何内容；不打印密码与 cookie 值。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from kedaibiao.config.dotenv import load_dotenv  # noqa: E402
from kedaibiao.core.models import Credentials  # noqa: E402
from kedaibiao.net.http import HttpClient, UrllibTransport  # noqa: E402
from kedaibiao.platforms.base import AdapterContext  # noqa: E402
from kedaibiao.platforms.pigai import protocol  # noqa: E402
from kedaibiao.platforms.pigai.adapter import PigaiAdapter  # noqa: E402
from kedaibiao.security.credentials import CredentialError, load_credentials  # noqa: E402


def _cookie_file() -> Path:
    """与 ``runtime.build_http_client()`` 用同一个文件，跑批与脚本共享登录态。"""
    return ROOT / "data" / "sessions" / "http_cookies.json"


async def _make_adapter() -> PigaiAdapter:
    transport = UrllibTransport()
    path = _cookie_file()
    restored = transport.load_cookies(path)
    if restored:
        print(f"[会话] 复用 {restored} 条 cookie: {path.relative_to(ROOT)}")
    adapter = PigaiAdapter(AdapterContext(http=HttpClient(transport, timeout=30.0)))
    if not await adapter.is_logged_in():
        try:
            creds = load_credentials(protocol_endpoint_platform())
        except CredentialError as exc:
            raise SystemExit(f"✗ {exc}") from exc
        print(f"[登录] 会话无效，正在登录 {creds.username!r}（密码不显示）…")
        if not await adapter.login(Credentials(username=creds.username, password=creds.password)):
            raise SystemExit("✗ 登录失败：服务端仍返回登录页，请检查凭据")
        saved = transport.save_cookies(path)
        path.chmod(0o600)
        print(f"[登录] 成功，已保存 {saved} 条 cookie（mode 600）")
    return adapter


def protocol_endpoint_platform() -> str:
    """凭据平台名走适配器的机器名，避免两处硬编码。"""
    return PigaiAdapter.name


def _fmt_deadline(value: datetime | None) -> str:
    if value is None:
        return "（无/未解析）"
    left = value - datetime.now()
    if left.total_seconds() < 0:
        return f"{value:%Y-%m-%d %H:%M} 已截止"
    days = left.days
    return f"{value:%Y-%m-%d %H:%M} 剩 {days} 天" if days else f"{value:%Y-%m-%d %H:%M} 剩不到 1 天"


async def cmd_pending(adapter: PigaiAdapter) -> int:
    items = await adapter.list_pending_assignments()
    print(f"\n未完成作业 {len(items)} 条")
    for a in items:
        print(f"  [{a.remote_id}] {a.title}")
        print(f"        截止 {_fmt_deadline(a.deadline)}   出题人 {a.raw.get('author', '')}")
        print(f"        指定班级 {len(str(a.raw.get('classes', '')).strip(',').split(','))} 个")
    if not items:
        print("  （无）")
    return 0


async def cmd_finished(adapter: PigaiAdapter) -> int:
    rows = await adapter.list_finished_essays()
    print(f"\n已提交作文 {len(rows)} 条")
    for r in rows:
        score = r.score_raw or "（空）"
        print(f"  [{r.rid}] eid={r.eid}  成绩={score}  稿次={r.submit_count}  提交={r.submitted_at}")
        print(f"        {r.title}")
    if not rows:
        print("  （无）")
    return 0


async def cmd_show(adapter: PigaiAdapter, rid: str, eid: str) -> int:
    d = await adapter.fetch_essay_detail(rid, eid)
    print(f"\n第 {d.rid} 号作文（eid={d.eid}）")
    print(f"  总分 {d.score} / {d.full_marks}")
    for dim in d.dimensions:
        print(f"    {dim.name:6s} {dim.ratio:.4f}   (来源 {dim.source})")
    print(f"  题目：{d.topic}")
    print(f"  要求：{d.requirements}")
    for k, v in d.meta.items():
        if v:
            print(f"  {k}：{v}")
    print(f"  总评：{d.overall_comment}")
    if d.essay_text:
        print(f"\n  ── 正文（{len(d.essay_text)} 字符）──")
        print("  " + d.essay_text.replace("\n", "\n  "))
    else:
        print("\n  （正文：当前解析器尚未返回，见 task-6）")
    print(f"\n  ── 按句点评 {len(d.annotations)} 条 ──")
    for a in d.annotations:
        print(f"  [sentence {a.sid}] {a.level}/{a.cate}  {a.message[:80]}")
    return 0


async def cmd_lookup(adapter: PigaiAdapter, rid: str) -> int:
    fetch = getattr(adapter, "fetch_assignment_by_rid", None)
    if fetch is None:
        print("✗ 「按作业号定位」尚未落地：等待 task-6 的 parse.parse_write_page。")
        print("  已实测可用的是 pending / finished / show 三条。")
        return 2
    page = await fetch(rid)
    print(f"\n作业号 {page.rid}（eid={page.eid or '尚未写过'}）")
    print(f"  标题：{page.title}")
    print(f"  要求：{page.requirement}")
    print(f"  字数：{page.word_range}   满分：{page.full_marks}")
    print(f"  截止：{page.deadline}   服务器时间：{page.server_time}")
    print(f"  已有稿子：{'是，%d 字符' % len(page.essay_text) if page.has_draft else '否'}")
    print(f"  （禁止粘贴开关：{'开' if page.no_paste else '关'} —— 仅读取，不利用）")
    if page.essay_text:
        print(f"\n  ── 当前稿子（{len(page.essay_text)} 字符）──")
        print("  " + page.essay_text.replace("\n", "\n  "))
    return 0


async def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="pigai_read", description="批改网只读入口")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("pending", help="未完成作业")
    sub.add_parser("finished", help="已提交作文")
    show = sub.add_parser("show", help="某篇作文的分数与批注")
    show.add_argument("rid")
    show.add_argument("eid")
    look = sub.add_parser("lookup", help="按作业号定位一个作业")
    look.add_argument("rid")
    args = parser.parse_args(argv)

    count = load_dotenv(ROOT / ".env")
    print(f"[配置] 已从 .env 载入 {count} 个变量"
          f"（绕过 load_settings 不回写 os.environ 的框架问题）")

    adapter = await _make_adapter()
    try:
        if args.cmd == "pending":
            return await cmd_pending(adapter)
        if args.cmd == "finished":
            return await cmd_finished(adapter)
        if args.cmd == "show":
            return await cmd_show(adapter, args.rid, args.eid)
        if args.cmd == "lookup":
            return await cmd_lookup(adapter, args.rid)
    finally:
        await adapter.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
