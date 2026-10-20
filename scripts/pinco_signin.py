#!/usr/bin/env python3
"""品课「课堂签到」：把签到链接/签到码提交给考勤接口（默认 dry-run）。

机制（2026-10-20 静态 + 失败路径实测）：老师发起课堂签到后，学生端入口是
一条 H5 链接 ``https://pinco.seewo.com/student/signin?attendanceCode=<code>``；
学生端页面把这个 ``attendanceCode`` 当 ``attendanceKey`` 发给
``PostV1TeachingAttendanceAttend``。字段名与失败码已用真实会话实测
（空 body → 401「attendanceKey 不能为null」；随便填码 → 7207 考勤签到码无效），
**成功路径尚未用有效签到码实测**。

签到码是老师端滚动生成的（约 10 秒一换），所以每次都要拿当次的链接/码::

    # 干跑：只解析，不发请求
    python3 scripts/pinco_signin.py --code 'https://pinco.seewo.com/student/signin?attendanceCode=xxx'

    # 真的签到
    python3 scripts/pinco_signin.py --code 'xxx' --live

会话从控制台绑定账号的落盘快照来（``data/sessions/accounts/`` 下唯一一份），
也可以用 ``--session`` 指定。

退出码：0 = 干跑成功或签到成功；2 = 没有可用会话；3 = 参数/会话问题；
4 = 请求已发出但平台拒绝（码无效/已结束/不匹配）。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kedaibiao.config.settings import resolve_environ  # noqa: E402
from kedaibiao.platforms import load_builtin_adapters  # noqa: E402
from kedaibiao.runtime import build_runtime, restore_adapter_session  # noqa: E402
from kedaibiao.security.credentials import load_credentials  # noqa: E402

PLATFORM = "pinco"


def _discover_session(root: Path) -> Path | None:
    """在控制台的账号目录里找唯一一份品课会话快照。"""
    folder = root / "data" / "sessions" / "accounts"
    matches = sorted(folder.glob(f"*-{PLATFORM}-*.cookies.json"))
    return matches[0] if len(matches) == 1 else None


async def run(args: argparse.Namespace) -> int:
    load_builtin_adapters()
    root = Path(args.root)
    session_file = Path(args.session) if args.session else _discover_session(root)
    runtime = build_runtime(
        root=root, cookie_file=str(session_file) if session_file else None
    )
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

        if not args.live:
            print("dry-run：只解析签到码，不发送任何请求。加 --live 才会真的签到。\n")

        try:
            outcome = await adapter.sign_in(args.code, dry_run=not args.live)
        except ValueError as error:
            print(f"✗ 签到码有问题：{error}", file=sys.stderr)
            return 3

        print(f"签到码：{outcome.masked_key}（{len(outcome.attendance_key)} 位）")
        print(f"结果：{outcome.summary()}")
        if outcome.dry_run:
            print("\n完成（未发送任何写请求）。")
            return 0
        if outcome.signed:
            print("\n完成：平台已接受这次签到。")
            return 0
        if outcome.error_code is not None:
            print(f"平台错误码：{outcome.error_code}", file=sys.stderr)
        return 4
    finally:
        runtime.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="品课课堂签到（默认 dry-run）")
    parser.add_argument("--root", default=".", help="项目根目录（读 config/ 与 .env）")
    parser.add_argument(
        "--session",
        default="",
        help="会话快照路径（默认自动找 data/sessions/accounts/ 下唯一一份品课会话）",
    )
    parser.add_argument(
        "--code",
        required=True,
        help="签到码或整条签到链接（/student/signin?attendanceCode=...）",
    )
    parser.add_argument("--live", action="store_true", help="真的发送签到（默认只解析）")
    args = parser.parse_args(argv)
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
