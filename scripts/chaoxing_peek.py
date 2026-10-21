#!/usr/bin/env python3
"""超星 / 学习通 只读速览：列出课程、作业与考试。

只读：本脚本只有 GET，没有提交、没有进度上报、没有签到。

```bash
# A) 从本机已登录的 Chrome 用户目录取会话（不碰密码、不碰验证码）
python3 scripts/chaoxing_peek.py --profile ~/.config/google-chrome --works

# B) 手贴 cookie（DevTools → Network → 任一请求 → Cookie 头）
python3 scripts/chaoxing_peek.py --cookie "_uid=...; fid=...; ..." --works

# 落盘原始 HTML，首次跑建议带上，便于校准入口
python3 scripts/chaoxing_peek.py --profile ~/.config/google-chrome --works --dump data/chaoxing
```

想要页面版：``python3 scripts/chaoxing_web.py``。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kedaibiao.browser.capture import CaptureError, capture_cookies  # noqa: E402
from kedaibiao.platforms.chaoxing import client, endpoints  # noqa: E402


def resolve_cookie(args: argparse.Namespace) -> str:
    if args.cookie:
        return args.cookie.strip()
    if args.cookie_file:
        return Path(args.cookie_file).read_text(encoding="utf-8").strip()
    if args.profile:
        print(f"从浏览器用户目录抓取会话: {args.profile}", file=sys.stderr)
        try:
            result = capture_cookies(
                url=endpoints.COURSE_LIST_URLS[0],
                profile=args.profile,
                domain_hint="chaoxing.com",
            )
        except CaptureError as exc:
            raise SystemExit(f"抓取会话失败: {exc}") from exc
        print(result.summary(), file=sys.stderr)
        return result.cookie_header
    raise SystemExit(
        "需要一个登录态：--profile <Chrome 用户目录>，或 --cookie '<Cookie 头>'。\n"
        "学习通的登录要扫码/验证码，脚本不去代过 —— 请先在你自己浏览器里登录一次。"
    )


def print_courses(courses: list[dict]) -> None:
    print(f"\n课程 {len(courses)} 门")
    for index, course in enumerate(courses, start=1):
        print(f"  {index:>2}. {course['name']}")
        print(f"       courseid={course['courseid']} clazzid={course['clazzid']}")


def print_works(group: dict) -> None:
    print(f"\n[{group['course']}]")
    if not group["items"]:
        print(f"  （没有读到作业/考试：{group['note']}）")
        return
    for item in group["items"]:
        due = item["deadline"].replace("T", " ")[:16] if item["deadline"] else "无截止"
        status = "已完成" if item["finished"] else "未完成"
        kind = {"exam": "考试", "homework": "作业"}.get(item["kind"], item["kind"])
        print(f"  {status:<4} {kind}  {item['title']}  截止 {due}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="超星/学习通 只读速览（不写任何东西）")
    parser.add_argument("--cookie", help="整串 Cookie 头")
    parser.add_argument("--cookie-file", help="从文件读 Cookie 头")
    parser.add_argument("--profile", help="已登录过学习通的 Chrome 用户目录")
    parser.add_argument("--works", action="store_true", help="同时列每门课的作业/考试")
    parser.add_argument("--dump", metavar="DIR", help="把原始 HTML 落盘，便于校准入口")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--timeout", type=float, default=20.0)
    args = parser.parse_args(argv)

    cookie = resolve_cookie(args)
    dump_dir = Path(args.dump) if args.dump else None
    payload = client.collect(
        cookie, timeout=args.timeout, dump_dir=dump_dir, include_works=args.works
    )

    if not payload["ok"]:
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        print(
            "没有读到课程。三种可能：\n"
            "  1) 登录态失效 —— 重新在浏览器里登录后复制新的 Cookie；\n"
            "  2) 入口变了 —— 看 --dump 出来的 HTML，把有效的 URL 提到候选列表前面；\n"
            "  3) 这个账号确实没课。\n"
            "逐条结果：\n  " + "\n  ".join(payload["notes"]),
            file=sys.stderr,
        )
        return 1

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print_courses(payload["courses"])
    for group in payload["works"]:
        print_works(group)
    if dump_dir is not None:
        print(f"\n原始页面已写入 {dump_dir}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
