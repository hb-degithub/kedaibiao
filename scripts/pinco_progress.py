#!/usr/bin/env python3
"""品课「刷课」：按页推进课件学习进度（默认 dry-run，绝不发写请求）。

机制（2026-10-20 实测确认）：材料进度 = ``(maxPageIndex + 1) / 总页数``。
打开课件拿到总页数后，逐页 ``PostV1CoursewareStudentMaxLook`` 即可推进。
PPT 与 PDF 都适用。

会话从控制台绑定账号的落盘快照来（``data/sessions/accounts/`` 下唯一一份），
也可以用 ``--session`` 指定::

    python3 scripts/pinco_progress.py --course <courseId>                  # 只列材料与进度
    python3 scripts/pinco_progress.py --course <courseId> --material <id>  # dry-run 看计划
    python3 scripts/pinco_progress.py --course <courseId> --all --live     # 全部推到目标

⚠️ ``--live`` 会真的写平台（翻页上报）。带三道熔断：页数上限、总时长上限、
连续几次回读进度不涨就停。停下来时如实回报，不假装成功。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kedaibiao.config.settings import resolve_environ  # noqa: E402
from kedaibiao.platforms import load_builtin_adapters  # noqa: E402
from kedaibiao.platforms.pinco.progress import (  # noqa: E402
    STOP_RATIO_STALLED,
    STOP_TARGET_REACHED,
    ProgressPolicy,
    ProgressTick,
)
from kedaibiao.runtime import build_runtime, restore_adapter_session  # noqa: E402
from kedaibiao.security.credentials import load_credentials  # noqa: E402

PLATFORM = "pinco"


def _fmt_size(raw: object) -> str:
    size = (raw or {}).get("materialSize") if isinstance(raw, dict) else None
    try:
        number = float(size)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "—"
    if number <= 0:
        return "—"
    return f"{number / (1024 * 1024):.1f} MB"


def _discover_session(root: Path) -> Path | None:
    """在控制台的账号目录里找唯一一份品课会话快照。"""
    folder = root / "data" / "sessions" / "accounts"
    matches = sorted(folder.glob(f"*-{PLATFORM}-*.cookies.json"))
    return matches[0] if len(matches) == 1 else None


async def _fill_one(adapter, course, material, policy, target: float) -> bool:
    """推进一份材料；返回是否已到目标。"""
    print(f"\n== {material.title}（{material.remote_id}）", flush=True)

    def on_page(tick: ProgressTick) -> None:
        print(f"  {tick.as_line()}", flush=True)

    report = await adapter.advance_learning(
        course,
        material.remote_id,
        dry_run=False,
        policy=policy,
        target_ratio=target,
        on_page=on_page,
    )
    print(f"  {report.summary()}", flush=True)
    if report.stopped == STOP_RATIO_STALLED:
        print("  ⚠️ 连续回读进度都没涨，已按熔断停止。", flush=True)
    return report.stopped == STOP_TARGET_REACHED and report.ratio_after + 1e-9 >= target


async def run(args: argparse.Namespace) -> int:
    load_builtin_adapters()
    root = Path(args.root)
    session_file = Path(args.session) if args.session else _discover_session(root)
    runtime = build_runtime(root=root, cookie_file=str(session_file) if session_file else None)
    try:
        adapter = runtime.make_adapter(PLATFORM)
        restored = bool(session_file) and restore_adapter_session(adapter, session_file)
        if not restored:
            creds = load_credentials(PLATFORM, require=True, environ=resolve_environ(root))
            if not await adapter.refresh_session(creds):
                print(
                    "✗ 登录失败：没有可用的会话。控制台里绑定过品课账号的话，"
                    "可用 --session <会话文件> 指定快照。",
                    file=sys.stderr,
                )
                return 2
        elif not await adapter.is_logged_in():
            print(f"✗ 会话快照不可用或已过期：{session_file}", file=sys.stderr)
            return 2

        courses = await adapter.list_courses()
        course = next((c for c in courses if c.remote_id == args.course), None)
        if course is None:
            print(f"✗ 找不到课程 {args.course}（这个账号有 {len(courses)} 门课）", file=sys.stderr)
            return 3

        materials = await adapter.list_learning_tasks(course)
        if not args.material and not args.all:
            print(f"课程：{course.name}（{course.remote_id}）—— {len(materials)} 份材料\n")
            print(f"{'材料号':<22} {'进度':>5}  {'大小':>10}  名称")
            for item in materials:
                print(
                    f"{item.remote_id:<22} {item.progress * 100:>4.0f}%  "
                    f"{_fmt_size(item.raw):>10}  {item.title}"
                )
            print(
                "\n提示：--material <材料号> 看单份；--all 处理全部；"
                "不加 --live 一律只算不写。"
            )
            return 0

        targets = materials
        if args.material:
            targets = [m for m in materials if m.remote_id == args.material]
            if not targets:
                print(f"✗ 课程里没有材料 {args.material}", file=sys.stderr)
                return 3

        policy = ProgressPolicy(
            page_interval_seconds=args.page_interval,
            max_pages=args.max_pages,
            max_total_seconds=args.max_seconds,
            refresh_every_pages=args.refresh_every,
            ratio_stall_checks=args.stall_checks,
        )

        if not args.live:
            print("dry-run：只读材料/进度，不打开课件、不发翻页。加 --live 才会真的推进。\n")
            for material in targets:
                report = await adapter.advance_learning(
                    course,
                    material.remote_id,
                    dry_run=True,
                    policy=policy,
                    target_ratio=args.target,
                )
                print(f"{material.remote_id}  {material.progress * 100:>3.0f}%  {material.title}")
                print(f"  {report.summary()}")
            print("\n完成（未发送任何写请求）。")
            return 0

        print(
            f"开始推进：{len(targets)} 份材料，目标 {args.target * 100:.0f}%；"
            f"每页间隔 {policy.page_interval_seconds:g}s，上限 "
            f"{policy.max_total_seconds:g}s / {policy.max_pages} 页。",
            flush=True,
        )
        failed: list[str] = []
        for material in targets:
            if material.progress + 1e-9 >= args.target:
                print(f"\n== {material.title}：已 {material.progress * 100:.0f}%，跳过", flush=True)
                continue
            try:
                reached = await _fill_one(adapter, course, material, policy, args.target)
            except Exception as error:  # 单份失败不影响后面的材料
                print(f"  ✗ {type(error).__name__}: {error}", file=sys.stderr, flush=True)
                failed.append(material.remote_id)
                continue
            if not reached:
                failed.append(material.remote_id)

        if failed:
            print(
                f"\n⚠️ {len(failed)} 份没到目标：{', '.join(failed)}",
                file=sys.stderr,
            )
            return 4
        print("\n全部到目标。")
        return 0
    finally:
        runtime.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="品课按页推进学习进度（默认 dry-run）")
    parser.add_argument("--root", default=".", help="项目根目录（读 config/ 与 .env）")
    parser.add_argument(
        "--session",
        default="",
        help="会话快照路径（默认自动找 data/sessions/accounts/ 下唯一一份品课会话）",
    )
    parser.add_argument("--course", required=True, help="课程号")
    parser.add_argument("--material", default="", help="材料号（省略且不加 --all 时只列出）")
    parser.add_argument("--all", action="store_true", help="处理课程下所有材料")
    parser.add_argument("--live", action="store_true", help="真的翻页上报（默认只算不写）")
    parser.add_argument("--target", type=float, default=1.0, help="目标进度 0~1（默认 1.0）")
    parser.add_argument("--max-pages", type=int, default=400, help="本次每份最多翻多少页")
    parser.add_argument(
        "--max-seconds", type=float, default=900.0, help="本次每份最多花多少秒"
    )
    parser.add_argument(
        "--page-interval", type=float, default=1.0, help="翻页间隔秒数（不得小于 0.2）"
    )
    parser.add_argument("--refresh-every", type=int, default=10, help="每多少页回读一次进度")
    parser.add_argument("--stall-checks", type=int, default=2, help="连续几次不涨就停")
    args = parser.parse_args(argv)
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
