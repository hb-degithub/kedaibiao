#!/usr/bin/env python3
"""品课「刷课」：按学生端页面的节奏上报学习时长。

**默认 dry-run**：只读材料、算计划、打印，不发任何写请求；要真的上报必须显式
``--live``。只发 ``PostV1CourseByCourseIdMaterialStudentLearningDurationReport``
这一个 action，绝不碰作业提交。

会话从浏览器来（应用层 csrf 与浏览器上下文绑定，纯账号密码走不通）::

    export KEDAIBIAO_PINCO_COOKIE='csrfToken=…; x-token=…'
    python3 scripts/pinco_progress.py --course 1321128857631563776
    python3 scripts/pinco_progress.py --course 1321128857631563776 --material 1322029806386077696
    python3 scripts/pinco_progress.py --course … --material … --live --max-seconds 300

⚠️ 实测「接口接受上报」但材料进度没动（见
``docs/verification/pinco-progress.md``）。所以 ``--live`` 带三道熔断：总时长
上限、次数上限、**连续几次刷新进度都不涨就停**；停下来时回报「没动」，而不是
继续刷。别把它当成「一定能刷满」的承诺。
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
    LearningReporter,
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
    """在控制台的账号目录里找唯一一份品课会话快照。

    控制台把「浏览器会话」落盘在 ``data/sessions/accounts/``；CLI 直接复用它，
    用户就不用再手动导一遍 cookie。只有恰好一份时才自动用，多了就要求显式
    ``--session``，避免刷错账号。
    """
    folder = root / "data" / "sessions" / "accounts"
    matches = sorted(folder.glob(f"*-{PLATFORM}-*.cookies.json"))
    return matches[0] if len(matches) == 1 else None


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
        if not args.material:
            print(f"课程：{course.name}（{course.remote_id}）—— {len(materials)} 份材料\n")
            print(f"{'材料号':<22} {'进度':>5}  {'大小':>10}  名称")
            for item in materials:
                print(
                    f"{item.remote_id:<22} {item.progress * 100:>4.0f}%  "
                    f"{_fmt_size(item.raw):>10}  {item.title}"
                )
            print(
                "\n提示：加 --material <材料号> 查看该材料的 dry-run 计划；"
                "再加 --live 才会真的上报（默认不发请求）。"
            )
            return 0

        target = next((m for m in materials if m.remote_id == args.material), None)
        if target is None:
            print(f"✗ 课程里没有材料 {args.material}", file=sys.stderr)
            return 3

        policy = ProgressPolicy(
            tick_seconds=args.tick_seconds,
            refresh_every=args.refresh_every,
            max_ticks=args.max_ticks,
            max_total_seconds=args.max_seconds,
            ratio_stall_checks=args.stall_checks,
        )

        def on_tick(tick: ProgressTick) -> None:
            print(f"  {tick.as_line()}", flush=True)

        if not args.live:
            initial = await adapter.fetch_material_detail(course, args.material)
            reporter = LearningReporter(adapter.client(), policy=policy)
            report = await reporter.run(
                course_id=course.remote_id,
                material_id=args.material,
                initial=initial,
                fetch_detail=lambda: adapter.fetch_material_detail(course, args.material),
                dry_run=True,
                target_ratio=args.target,
            )
            print(report.summary())
            print("dry-run：没有发送任何请求。加 --live 才会按上面的计划上报。")
            return 0

        print(
            f"开始上报：课程 {course.remote_id} / 材料 {args.material}"
            f"（目标 {args.target * 100:.0f}%，上限 {policy.max_total_seconds}s，"
            f"每 {policy.tick_seconds:g}s 一次）",
            flush=True,
        )
        report = await adapter.report_learning(
            course,
            args.material,
            dry_run=False,
            policy=policy,
            target_ratio=args.target,
            on_tick=on_tick,
        )
        print()
        print(report.summary())
        if report.ratio_delta <= 0:
            print(
                "⚠️ 上报被平台接受了，但进度没有变化。实测也会这样 —— 详见 "
                "docs/verification/pinco-progress.md。已按熔断停止，没有继续刷。"
                if report.stopped == STOP_RATIO_STALLED
                else (
                    "⚠️ 上报被平台接受了，但进度没有变化"
                    "（见 docs/verification/pinco-progress.md）。"
                )
            )
            return 4
        return 0
    finally:
        runtime.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="品课学习时长上报（默认 dry-run）")
    parser.add_argument("--root", default=".", help="项目根目录（读 config/ 与 .env）")
    parser.add_argument(
        "--session",
        default="",
        help="会话快照路径（默认自动找 data/sessions/accounts/ 下唯一一份品课会话）",
    )
    parser.add_argument("--course", required=True, help="课程号")
    parser.add_argument("--material", default="", help="材料号（省略则只列出材料与进度）")
    parser.add_argument(
        "--live", action="store_true", help="真的上报（默认只算计划，不发请求）"
    )
    parser.add_argument("--target", type=float, default=1.0, help="目标进度 0~1（默认 1.0）")
    parser.add_argument(
        "--max-seconds", type=int, default=1800, help="本次最多上报多少秒（默认 1800）"
    )
    parser.add_argument("--max-ticks", type=int, default=720, help="本次最多上报多少次")
    parser.add_argument(
        "--tick-seconds", type=float, default=5.0, help="两次上报的间隔秒数（不得小于 5）"
    )
    parser.add_argument("--refresh-every", type=int, default=6, help="每多少次刷新一次进度")
    parser.add_argument("--stall-checks", type=int, default=3, help="连续几次刷新进度不涨就停")
    args = parser.parse_args(argv)
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
