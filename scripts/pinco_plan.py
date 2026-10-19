#!/usr/bin/env python3
"""生成品课「可复核提交计划」—— 只读，**结构上不可能提交**。

为什么单独一个脚本：

1. 需求是「先看计划，再决定」。框架的 ``kedaibiao run`` 走通用流水线，而品课自己
   的计划更细 —— 逐题给答案、置信度、来源，并把「为什么需要人工」分三类列出；
2. 控制台的只读动作注册表在 ``api/actions.py``（共享区，本线不动），所以计划入口
   放这里，不往共享区塞东西；
3. 这个脚本**连 ``submit`` 都不导入** —— 想提交也提交不了，比「靠自觉」硬。

会话从浏览器来（应用层 csrf 与浏览器上下文绑定，纯账号密码走不通）::

    export KEDAIBIAO_PINCO_COOKIE='csrfToken=…; x-token=…'
    python3 scripts/pinco_plan.py --limit 5
    python3 scripts/pinco_plan.py --out data/plans/pinco.json

计划同时打印（人看的）与落盘（JSON + 内容哈希，供后续复核）。**提交报文结构
目前仍未由真实样本确认**（``submit_payload_confirmed=False``），所以即便有人拿这份
计划去调 ``submit()``，守卫放行也不会真的发出写请求。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kedaibiao.config.settings import resolve_environ  # noqa: E402
from kedaibiao.platforms import load_builtin_adapters  # noqa: E402
from kedaibiao.runtime import build_runtime  # noqa: E402
from kedaibiao.security.credentials import load_credentials  # noqa: E402

PLATFORM = "pinco"


async def run(*, root: Path, limit: int, out: Path | None) -> int:
    # 与 CLI 一致：适配器要先注册进全局注册表，否则 make_adapter 报「未注册的平台」。
    # CLI 在 main() 里做这件事，这里是独立脚本，得自己来 —— 漏了它就会以一个
    # 看起来像「平台不存在」的错误收场，与真正的原因（没引导）差得很远。
    load_builtin_adapters()
    runtime = build_runtime(root=root)
    try:
        adapter = runtime.make_adapter(PLATFORM)
        creds = load_credentials(
            PLATFORM, require=True, environ=resolve_environ(root)
        )
        if not await adapter.refresh_session(creds):
            print("✗ 登录失败：会话不可用。", file=sys.stderr)
            return 2

        # 只读：plan() 会抓课程 / 作业 / 学习任务并生成计划，全程没有任何写操作。
        plan = await adapter.plan(max_assignments=limit)
        plan.seal()  # 盖章：落盘前算内容哈希，复核后若被改动，verify() 会发现

        text = plan.render_text()
        print(text)
        print(
            "\n提示：这份计划只用于复核。提交报文结构尚未由真实样本确认，"
            "守卫放行也不会发出写请求。"
        )

        if out is not None:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(plan.json_text(), encoding="utf-8")
            print(f"\n已落盘：{out}（内容哈希 {plan.digest}）")
        return 0
    finally:
        runtime.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成品课可复核提交计划（只读）")
    parser.add_argument("--root", default=".", help="项目根目录（读 config/ 与 .env）")
    parser.add_argument(
        "--limit", type=int, default=0, help="最多处理多少项（0 = 不限）"
    )
    parser.add_argument(
        "--out", default="data/plans/pinco-plan.json", help="计划 JSON 落盘路径（--out '' 则不落盘）"
    )
    args = parser.parse_args(argv)
    out = Path(args.out) if args.out else None
    return asyncio.run(run(root=Path(args.root), limit=args.limit, out=out))


if __name__ == "__main__":
    raise SystemExit(main())
