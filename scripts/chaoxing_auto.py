#!/usr/bin/env python3
"""学习通 / 超星：自动刷课与自动刷题（浏览器页面驱动，命令行版）。

## 用法

```bash
# 第一次：用账号密码登录并落盘会话（之后不用再登录）
python3 scripts/chaoxing_auto.py login --user 手机号

# 列出课程
python3 scripts/chaoxing_auto.py courses

# 列出某门课的任务点（视频/文档/测验）
python3 scripts/chaoxing_auto.py tasks --course 205643380

# 刷课：默认 2 倍速、静音、只做没完成的；--limit 先跑 1 个验证
python3 scripts/chaoxing_auto.py study --course 205643380 --limit 1

# 刷题：默认干跑（填卷 + 核验，不交卷）；--confirm 才真交
python3 scripts/chaoxing_auto.py quiz --course 219456814 --chapter 461423614
python3 scripts/chaoxing_auto.py quiz --course 219456814 --chapter 461423614 --confirm
```

## 纪律

- 登录：优先复用 ``data/sessions/chaoxing.storage_state.json``；需要时用
  ``--user/--password``（或环境变量 ``CHAOXING_USER`` / ``CHAOXING_PASSWORD``）。
  真弹出验证码会直接报错，不代过。
- 刷课是写操作：进度由播放器自己上报，本工具只驱动页面；``--limit`` 便于先验证。
- 刷题默认干跑：只填卷与核验，不点提交；``--confirm`` 才交卷（不可逆）。

退出码：0 成功；2 参数/登录问题；3 页面操作失败。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kedaibiao.browser.pw import PwBrowser, PwConfig, PwError  # noqa: E402
from kedaibiao.platforms.chaoxing import login, parsing  # noqa: E402
from kedaibiao.platforms.chaoxing.quizrun import ChaoxingQuiz  # noqa: E402
from kedaibiao.platforms.chaoxing.study import ChaoxingStudy  # noqa: E402

DEFAULT_STATE = ROOT / "data" / "sessions" / "chaoxing.storage_state.json"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
COURSE_LIST_URL = (
    "https://mooc2-ans.chaoxing.com/mooc2-ans/visit/courselistdata"
    "?courseType=1&courseFolderId=0&courseFolderSize=0&superstarClass=0"
)

log = logging.getLogger("chaoxing-auto")


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
    )


def make_emit(verbose: bool):
    def emit(level: str, message: str) -> None:
        prefix = {"ok": "✓", "warn": "!", "error": "✗"}.get(level, "·")
        print(f"{prefix} {message}", flush=True)

    return emit


async def resolve_course(page: Any, ref: str) -> tuple[str, str, str]:
    """把课程号/课程名解析成 ``(courseid, clazzid, name)``。"""
    html = await page.fetch_text(COURSE_LIST_URL)
    courses = parsing.parse_courses(html)
    if not courses:
        raise PwError("课程列表为空：学习通会话可能已失效")
    if ref.isdigit():
        for course in courses:
            if course.raw.get("courseid") == ref:
                return course.raw["courseid"], course.raw["clazzid"], course.name
        raise PwError(f"课程列表里没有 courseid={ref}")
    for course in courses:
        if ref in (course.name or ""):
            return course.raw["courseid"], course.raw["clazzid"], course.name
    raise PwError(f"没找到课程名包含 {ref!r} 的课程")


async def cmd_login(args: argparse.Namespace) -> int:
    user = args.user or os.environ.get("CHAOXING_USER", "")
    password = args.password or os.environ.get("CHAOXING_PASSWORD", "")
    if not user or not password:
        print("需要 --user/--password 或环境变量 CHAOXING_USER/CHAOXING_PASSWORD", file=sys.stderr)
        return 2
    async with PwBrowser(PwConfig(headless=not args.show, user_agent=UA)) as browser:
        page = await browser.new_page()
        await login.login_with_password(page, user, password)
        await browser.save_state(args.state)
        print(f"登录成功，会话已保存: {args.state}")
    return 0


async def cmd_courses(args: argparse.Namespace) -> int:
    async with PwBrowser(PwConfig(storage_state=args.state, user_agent=UA)) as browser:
        page = await browser.new_page()
        await login.ensure_login(
            page,
            username=args.user or os.environ.get("CHAOXING_USER", ""),
            password=args.password or os.environ.get("CHAOXING_PASSWORD", ""),
            state_path=args.state,
        )
        html = await page.fetch_text(COURSE_LIST_URL)
        for course in parsing.parse_courses(html):
            print(
                f"{course.raw.get('courseid', ''):<12} "
                f"{course.raw.get('clazzid', ''):<12} {course.name}"
            )
    return 0


async def _open_course_study(args: argparse.Namespace) -> tuple[Any, ChaoxingStudy, str]:
    browser = PwBrowser(PwConfig(storage_state=args.state, user_agent=UA))
    await browser.start()
    page = await browser.new_page()
    await login.ensure_login(
        page,
        username=args.user or os.environ.get("CHAOXING_USER", ""),
        password=args.password or os.environ.get("CHAOXING_PASSWORD", ""),
        state_path=args.state,
    )
    course_id, clazz_id, name = await resolve_course(page, args.course)
    study = ChaoxingStudy(
        page,
        course_id=course_id,
        clazz_id=clazz_id,
        emit=make_emit(args.verbose),
        rate=float(getattr(args, "rate", 2.0)),
    )
    await study.open_course()
    return browser, study, name


async def cmd_tasks(args: argparse.Namespace) -> int:
    browser, study, name = await _open_course_study(args)
    try:
        points = await study.task_points(only_pending=not args.all)
        print(f"课程：{name}（{study.course_id}）共 {len(points)} 个待处理任务点")
        for point in points:
            flag = "已完成" if point.passed is True else "待完成"
            print(
                f"  {point.kind:<9} {point.index_text:<6} {flag:<4} "
                f"{point.duration // 60:>4} 分  {point.name[:40]}"
            )
    finally:
        await browser.close()
    return 0


async def cmd_study(args: argparse.Namespace) -> int:
    browser, study, name = await _open_course_study(args)
    try:
        kinds = tuple(k.strip() for k in args.kinds.split(",") if k.strip())
        report = await study.run(
            kinds=kinds,
            limit=args.limit,
            only_pending=not args.all,
            doc_seconds=args.doc_seconds,
        )
        print(f"\n■ {name}：{report.summary()}")
        if args.json:
            print(json.dumps(
                [
                    {
                        "task": step.task.name,
                        "kind": step.task.kind,
                        "status": step.status,
                        "detail": step.detail,
                        "seconds": round(step.seconds, 1),
                    }
                    for step in report.steps
                ],
                ensure_ascii=False,
                indent=1,
            ))
    finally:
        await browser.close()
    return 0


async def cmd_quiz(args: argparse.Namespace) -> int:
    from kedaibiao.tasks.answerer import ChainAnswerer, RuleAnswerer
    from kedaibiao.tasks.llm import build_llm_answerer

    chain: list[Any] = []
    if not args.no_llm:
        llm = build_llm_answerer(_env_map())
        if llm is not None:
            chain.append(llm)
    chain.append(RuleAnswerer())
    answerer = ChainAnswerer(chain, min_confidence=0.0)
    browser = PwBrowser(PwConfig(storage_state=args.state, user_agent=UA))
    await browser.start()
    page = await browser.new_page()
    try:
        await login.ensure_login(
            page,
            username=args.user or os.environ.get("CHAOXING_USER", ""),
            password=args.password or os.environ.get("CHAOXING_PASSWORD", ""),
            state_path=args.state,
        )
        course_id, clazz_id, name = await resolve_course(page, args.course)
        runner = ChaoxingQuiz(
            page,
            course_id=course_id,
            clazz_id=clazz_id,
            answerer=answerer,
            emit=make_emit(args.verbose),
        )
        report = await runner.run_section_quiz(args.chapter, confirm=args.confirm)
        print(f"\n■ {name} 章节测验「{report.title}」：{report.summary()}")
        if report.submitted:
            print(f"  交卷结果：{report.score_text or report.submit_note or '平台未回显分数'}")
            if report.score is not None:
                print(f"  得分：{report.score:g}")
        for item in report.items:
            mark = "✓" if item.verified else ("-" if not item.answer else "?")
            print(
                f"  {mark} {item.number:>2}. {item.type_name} "
                f"答案={item.answer or '（未答）'} "
                f"置信度={item.confidence:.2f} 来源={item.source}"
            )
            if item.unsupported:
                print(f"     跳过：{item.unsupported}")
            if item.text:
                print(f"     题面：{item.text[:70]}")
    finally:
        await browser.close()
    return 0


async def cmd_exams(args: argparse.Namespace) -> int:
    """列出考试/作业（只读）。"""
    browser = PwBrowser(PwConfig(storage_state=args.state, user_agent=UA))
    await browser.start()
    page = await browser.new_page()
    try:
        await login.ensure_login(
            page,
            username=args.user or os.environ.get("CHAOXING_USER", ""),
            password=args.password or os.environ.get("CHAOXING_PASSWORD", ""),
            state_path=args.state,
        )
        course_id, clazz_id, name = await resolve_course(page, args.course)
        runner = ChaoxingQuiz(page, course_id=course_id, clazz_id=clazz_id)
        items = await runner.list_exams()
        print(f"课程：{name}（{course_id}）共 {len(items)} 场考试/作业")
        for item in items:
            flag = {
                "todo": "待做", "done": "已完成", "expired": "已过期",
            }.get(item.status, item.status)
            mobile = "（只允许手机APP）" if item.mobile_only else ""
            print(
                f"  {flag:<4} {item.exam_id:<10} {item.title[:36]:<38} "
                f"{item.end_time_text[:20]}{mobile}"
            )
    finally:
        await browser.close()
    return 0


async def cmd_exam(args: argparse.Namespace) -> int:
    """做一场考试/作业（默认干跑，--confirm 才交卷并回读分数）。"""
    from kedaibiao.platforms.chaoxing.quiz import ExamItem

    answerer = _build_chain_answerer(no_llm=args.no_llm)
    browser = PwBrowser(PwConfig(storage_state=args.state, user_agent=UA))
    await browser.start()
    page = await browser.new_page()
    try:
        await login.ensure_login(
            page,
            username=args.user or os.environ.get("CHAOXING_USER", ""),
            password=args.password or os.environ.get("CHAOXING_PASSWORD", ""),
            state_path=args.state,
        )
        course_id, clazz_id, name = await resolve_course(page, args.course)
        runner = ChaoxingQuiz(
            page,
            course_id=course_id,
            clazz_id=clazz_id,
            answerer=answerer,
            emit=make_emit(args.verbose),
            human_gate=_cli_human_gate,
        )
        item = ExamItem(
            title=args.title or args.exam_id,
            status="todo",
            exam_id=args.exam_id,
            relation_id=args.relation_id,
            action="goTest",
        )
        report = await runner.run_exam(item, confirm=args.confirm)
        print(f"\n■ {name} 考试/作业「{report.title}」：{report.summary()}")
        if report.submitted:
            print(f"  交卷结果：{report.score_text or report.submit_note or '平台未回显分数'}")
        for plan_item in report.items:
            print(
                f"  {plan_item.number:>2}. {plan_item.type_name} "
                f"答案={plan_item.answer or '（未答）'} "
                f"置信度={plan_item.confidence:.2f} 来源={plan_item.source}"
            )
    finally:
        await browser.close()
    return 0


def _build_chain_answerer(*, no_llm: bool = False):
    from kedaibiao.tasks.answerer import ChainAnswerer, RuleAnswerer
    from kedaibiao.tasks.llm import build_llm_answerer

    chain: list[Any] = []
    if not no_llm:
        llm = build_llm_answerer(_env_map())
        if llm is not None:
            chain.append(llm)
    chain.append(RuleAnswerer())
    return ChainAnswerer(chain, min_confidence=0.0)


async def _cli_human_gate(kind: str, message: str) -> bool:
    """CLI 版人工认证：打印原因，等用户回车。

    人脸认证只有在有摄像头的可见浏览器里才能完成；CLI 至少要做到「别偷偷跳过」。
    """
    print(f"\n⚠ {message}")
    answer = await asyncio.to_thread(
        input, "完成认证后按回车继续（输入 n 放弃）："
    )
    return answer.strip().lower() not in ("n", "no", "q")


def _env_map() -> dict[str, str]:
    env = dict(os.environ)
    env_path = ROOT / ".env"
    if env_path.is_file():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            env.setdefault(key.strip(), value.strip())
    return env


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="chaoxing_auto",
        description="学习通 / 超星：浏览器页面驱动的自动刷课与自动刷题",
    )
    parser.add_argument("--state", default=str(DEFAULT_STATE), help="登录态 storage_state 路径")
    parser.add_argument("--user", default="", help="手机号/超星号（默认读 CHAOXING_USER）")
    parser.add_argument("--password", default="", help="密码（默认读 CHAOXING_PASSWORD）")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--json", action="store_true", help="结果以 JSON 输出")
    sub = parser.add_subparsers(dest="command", required=True)

    p_login = sub.add_parser("login", help="账号密码登录并保存会话")
    p_login.add_argument("--show", action="store_true", help="显示浏览器窗口（人工过验证码用）")
    p_login.set_defaults(func=cmd_login)

    p_courses = sub.add_parser("courses", help="列出课程")
    p_courses.set_defaults(func=cmd_courses)

    p_tasks = sub.add_parser("tasks", help="列出任务点")
    p_tasks.add_argument("--course", required=True, help="课程号或课程名")
    p_tasks.add_argument("--all", action="store_true", help="包含已完成")
    p_tasks.set_defaults(func=cmd_tasks)

    p_study = sub.add_parser("study", help="自动刷课（写操作）")
    p_study.add_argument("--course", required=True)
    p_study.add_argument(
        "--kinds", default="video,document", help="要处理的类型，默认 video,document"
    )
    p_study.add_argument("--limit", type=int, default=0, help="最多处理几个任务点（0=不限）")
    p_study.add_argument("--rate", type=float, default=2.0, help="播放倍速（上限 2）")
    p_study.add_argument("--doc-seconds", type=float, default=0.0, help="每个文档停留秒数")
    p_study.add_argument("--all", action="store_true", help="包含已完成的")
    p_study.set_defaults(func=cmd_study)

    p_exams = sub.add_parser("exams", help="列出考试/作业")
    p_exams.add_argument("--course", required=True)
    p_exams.set_defaults(func=cmd_exams)

    p_exam = sub.add_parser("exam", help="做考试/作业（默认干跑，--confirm 才交卷）")
    p_exam.add_argument("--course", required=True)
    p_exam.add_argument("--exam-id", required=True, help="考试 id（考试列表里那一列）")
    p_exam.add_argument("--title", default="", help="考试名称（只用于显示）")
    p_exam.add_argument("--relation-id", default="", help="作答记录 id（可留空）")
    p_exam.add_argument("--confirm", action="store_true", help="真交卷（不可逆）")
    p_exam.add_argument("--no-llm", action="store_true", help="不用大模型，只查题库/规则")
    p_exam.set_defaults(func=cmd_exam)

    p_quiz = sub.add_parser("quiz", help="自动刷题（默认干跑，--confirm 才交卷）")
    p_quiz.add_argument("--course", required=True)
    p_quiz.add_argument("--chapter", required=True, help="小节 id（章节测验的 knowledgeid）")
    p_quiz.add_argument("--confirm", action="store_true", help="真交卷（不可逆）")
    p_quiz.add_argument("--no-llm", action="store_true", help="不用大模型，只查题库")
    p_quiz.set_defaults(func=cmd_quiz)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose)
    try:
        return asyncio.run(args.func(args))
    except (PwError, login.ChaoxingLoginError) as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print("已中断", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
