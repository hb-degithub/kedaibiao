"""命令行入口：``python -m kedaibiao <子命令>``。

骨架阶段只提供只读/诊断类命令 —— 真正跑批要等第一个平台适配器落地。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from importlib.util import find_spec
from pathlib import Path

from kedaibiao import __version__, adapters
from kedaibiao.bank.store import QuestionBank, ProgressStore
from kedaibiao.config.settings import load_settings, resolve_environ
from kedaibiao.core.models import TaskKind
from kedaibiao.platforms import load_builtin_adapters
from kedaibiao.runtime import build_runtime
from kedaibiao.security.accounts import AccountError, AccountStore
from kedaibiao.tasks.pipeline import PipelineConfig

OPTIONAL_DEPS = {
    "playwright": "浏览器自动化（必需，才能真跑平台）",
    "fastapi": "Web 控制台（可选）",
    "uvicorn": "Web 控制台服务器（可选）",
    "sqlalchemy": "ORM（可选，当前用标准库 sqlite3）",
}


def build_parser() -> argparse.ArgumentParser:
    # ``--root`` 允许出现在子命令前后两个位置。
    # 子命令那一份用 SUPPRESS 作默认值，这样「没写」时不会把全局那份的值冲掉。
    root_parent = argparse.ArgumentParser(add_help=False)
    root_parent.add_argument("--root", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    parser = argparse.ArgumentParser(
        prog="kedaibiao",
        description="课代表 —— 多平台作业/课程一体化自动化框架",
    )
    parser.add_argument("--version", action="version", version=f"kedaibiao {__version__}")
    parser.add_argument("--root", default=".", help="项目根目录（读取 config/ 与 .env）")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("platforms", parents=[root_parent], help="列出已注册的平台适配器")
    sub.add_parser("doctor", parents=[root_parent], help="自检运行环境")
    sub.add_parser("config", parents=[root_parent], help="打印生效配置（已脱敏）")

    run_p = sub.add_parser(
        "run", parents=[root_parent], help="对某个平台跑一次任务流水线"
    )
    run_p.add_argument("platform", help="平台机器名（先用 platforms 子命令查看）")
    run_p.add_argument(
        "--live",
        action="store_true",
        help="真正提交（不加则干跑：只规划、不产生任何写操作）",
    )
    run_p.add_argument("--limit", type=int, default=0, help="最多处理多少个作业，0 表示不限")
    run_p.add_argument(
        "--kinds",
        default="",
        help="只处理这些类型，逗号分隔：course,homework,exam,practice",
    )

    web_p = sub.add_parser("web", parents=[root_parent], help="启动 Web 控制台")
    web_p.add_argument(
        "--host", default="127.0.0.1", help="绑定地址（默认只绑回环，不要随便改）"
    )
    web_p.add_argument("--port", type=int, default=8765, help="端口，0 表示随机空闲端口")
    web_p.add_argument(
        "--allow-host",
        action="append",
        default=[],
        metavar="NAME",
        help="额外允许的 Host 头（绑非回环地址、且打算用主机名访问时用）",
    )

    actions_p = sub.add_parser(
        "actions", parents=[root_parent], help="列出某个平台的只读动作"
    )
    actions_p.add_argument("platform", help="平台机器名")

    action_p = sub.add_parser(
        "action", parents=[root_parent], help="执行某个平台的只读动作"
    )
    action_p.add_argument("platform", help="平台机器名")
    action_p.add_argument("action", help="动作名（先用 actions 子命令查看）")
    action_p.add_argument(
        "param",
        nargs="*",
        metavar="key=value",
        help="动作参数，例如 rid=200000001 eid=1000002",
    )

    stats = sub.add_parser("bank-stats", parents=[root_parent], help="题库与跑批统计")
    stats.add_argument("--db", default=None, help="覆盖配置里的数据库路径")
    stats.add_argument("--runs", type=int, default=10, help="显示最近多少条执行记录")

    export = sub.add_parser("bank-export", parents=[root_parent], help="导出题库为 JSONL")
    export.add_argument("out", help="输出文件路径")
    export.add_argument("--db", default=None)

    imp = sub.add_parser("bank-import", parents=[root_parent], help="从 JSONL 导入题库")
    imp.add_argument("src", help="输入文件路径")
    imp.add_argument("--db", default=None)

    return parser


def _db_path(settings, override: str | None) -> Path:
    return Path(override) if override else settings.db_path


def cmd_platforms(args: argparse.Namespace) -> int:
    names = adapters.names()
    if not names:
        print("尚未注册任何平台适配器。")
        print("预期位置: src/kedaibiao/platforms/<平台>/，并在 pyproject.toml 的")
        print('[project.entry-points."kedaibiao.adapters"] 中登记。')
        return 0
    print(f"已注册 {len(names)} 个平台适配器：")
    for name in names:
        adapter = adapters.get(name)
        display = getattr(adapter, "display_name", "") or "(无展示名)"
        print(f"  - {name:16s} {display}")
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    settings = load_settings(root=args.root)
    data = settings.to_dict()
    # 凭据类字段一律不出现在这里；当前 Settings 不含密码字段，留一道保险
    for key in list(data):
        if "password" in key or "secret" in key or "token" in key:
            data[key] = "***"
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    ok = True
    print(f"Python : {sys.version.split()[0]} ({sys.executable})")
    if sys.version_info < (3, 11):
        print("  ✗ 需要 Python >= 3.11（用到了 tomllib）")
        ok = False
    else:
        print("  ✓ 版本满足 >= 3.11")

    print("可选依赖：")
    for module, why in OPTIONAL_DEPS.items():
        present = find_spec(module) is not None
        mark = "✓" if present else "✗"
        print(f"  {mark} {module:12s} {why}")
        if module == "playwright" and not present:
            ok = False

    settings = load_settings(root=args.root)
    print(f"配置   : dry_run={settings.dry_run} headless={settings.headless} "
          f"concurrency={settings.max_concurrency}")
    if not settings.dry_run:
        print("  ! dry_run=False —— 当前配置会真的提交作业")

    db = settings.db_path
    try:
        db.parent.mkdir(parents=True, exist_ok=True)
        probe = db.parent / ".write_probe"
        probe.touch()
        probe.unlink()
        print(f"存储   : ✓ 可写 {db}")
    except OSError as exc:
        print(f"存储   : ✗ 不可写 {db}: {exc}")
        ok = False

    try:
        from kedaibiao.security.accounts import AccountStore as _AS

        _store = _AS(settings.db_path)
        try:
            _accounts = _store.list_for()
        finally:
            _store.close()
    except Exception:  # noqa: BLE001 - 还没建库时不该让 doctor 崩掉
        _accounts = []
    if _accounts:
        by_platform: dict[str, int] = {}
        for acc in _accounts:
            by_platform[acc.platform] = by_platform.get(acc.platform, 0) + 1
        detail = ", ".join(f"{k}×{v}" for k, v in sorted(by_platform.items()))
        print(f"账号   : ✓ 已绑定 {detail}（密码不落盘，只存会话）")
    else:
        print("账号   : （还没有绑定平台账号，去控制台「账号」页添加）")

    print("结论   : " + ("环境就绪" if ok else "环境不完整，见上方 ✗"))
    return 0 if ok else 1


def cmd_bank_stats(args: argparse.Namespace) -> int:
    settings = load_settings(root=args.root)
    path = _db_path(settings, args.db)
    if not path.is_file():
        print(f"数据库不存在: {path}（还没跑过批，属正常）")
        return 0

    bank = QuestionBank(path)
    store = ProgressStore(path)
    try:
        stats = bank.stats()
        print("题库统计：")
        for key, value in stats.items():
            print(f"  {key:10s} {value}")
        runs = store.recent_runs(args.runs)
        print(f"\n最近 {len(runs)} 条执行记录：")
        for run in runs:
            print(f"  #{run['id']:<4d} {run['platform']}/{run['assignment_id']} "
                  f"{run['status']:<10s} {run['error'][:60]}")
    finally:
        bank.close()
        store.close()
    return 0


def cmd_bank_export(args: argparse.Namespace) -> int:
    settings = load_settings(root=args.root)
    bank = QuestionBank(_db_path(settings, args.db))
    try:
        count = bank.export_jsonl(args.out)
    finally:
        bank.close()
    print(f"已导出 {count} 条到 {args.out}")
    return 0


def cmd_bank_import(args: argparse.Namespace) -> int:
    settings = load_settings(root=args.root)
    bank = QuestionBank(_db_path(settings, args.db))
    try:
        count = bank.import_jsonl(args.src)
    finally:
        bank.close()
    print(f"已从 {args.src} 导入 {count} 条")
    return 0


def _parse_kinds(raw: str) -> frozenset[TaskKind]:
    if not raw.strip():
        return frozenset()
    kinds = set()
    for part in raw.split(","):
        name = part.strip()
        if name:
            kinds.add(TaskKind(name))  # 非法值抛 ValueError，由调用方转成友好提示
    return frozenset(kinds)


def cmd_run(args: argparse.Namespace) -> int:
    settings = load_settings(root=args.root)

    try:
        kinds = _parse_kinds(args.kinds)
    except ValueError as exc:
        valid = ", ".join(k.value for k in TaskKind)
        print(f"✗ 未知的任务类型 {exc}；可选：{valid}")
        return 2

    if args.live:
        # 这一步之后所有写操作都不可撤销，所以把将要发生的事先摆出来
        settings = settings.merged({"dry_run": False})
        print("=" * 64)
        print("⚠️  --live：本次会**真正提交**作业，写操作不可撤销。")
        print(f"    平台={args.platform}  并发={settings.max_concurrency}  "
              f"置信度阈值={settings.min_confidence}")
        if args.limit:
            print(f"    本次最多处理 {args.limit} 个作业")
        print("=" * 64)
    else:
        print("干跑模式：只规划不提交（要真提交请加 --live）")

    config = PipelineConfig(
        dry_run=settings.dry_run,
        min_confidence=settings.min_confidence,
        kinds=kinds,
        max_assignments=args.limit,
    )

    # 凭据只来自账号库（不再读 .env）
    store = AccountStore(settings.db_path)
    try:
        creds, cookie_file = store.resolve_credentials(
            args.platform, session_dir=settings.session_dir
        )
    except AccountError as exc:
        store.close()
        print(f"✗ {exc}")
        return 2
    finally:
        store.close()

    runtime = build_runtime(
        root=args.root, settings=settings, config=config, cookie_file=cookie_file
    )
    try:
        report = asyncio.run(runtime.run(args.platform, credentials=creds))
    except CredentialError as exc:
        print(f"✗ {exc}")
        return 2
    except KeyError as exc:
        # KeyError 的 str() 会给消息再套一层引号，这里取原始消息
        print(f"✗ {exc.args[0] if exc.args else exc}")
        return 2
    finally:
        runtime.close()

    print()
    print(report.summary())
    for record in report.records:
        note = record.error or str(record.detail.get("reason", ""))
        print(f"  [{record.status.value:<9s}] {record.assignment_id:<28s} {note}")
    return 0 if report.failed == 0 else 1


def cmd_web(args: argparse.Namespace) -> int:
    # 延迟导入：不起控制台就不该加载 http.server
    from kedaibiao.api.server import serve
    from kedaibiao.api.service import ConsoleService

    if not Path(args.root).is_dir():
        print(f"✗ 项目根目录不存在: {args.root}")
        return 2

    service = ConsoleService(root=args.root)
    serve(service, host=args.host, port=args.port, allow_hosts=args.allow_host)
    return 0


def _display_width(text: str) -> int:
    """终端列宽：东亚宽字符算 2 列。表格对齐靠它。"""
    import unicodedata

    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _display_width(text))


def _render_table(columns: list[dict], rows: list[dict], empty_hint: str = "") -> None:
    if not rows:
        print(f"（{empty_hint or '没有数据'}）")
        return
    headers = [str(c.get("label") or c.get("key")) for c in columns]
    keys = [str(c.get("key")) for c in columns]
    body = [[str(row.get(k, "")) for k in keys] for row in rows]
    widths = [
        max(_display_width(headers[i]), *(_display_width(r[i]) for r in body))
        for i in range(len(headers))
    ]
    print("  ".join(_pad(h, widths[i]) for i, h in enumerate(headers)))
    print("  ".join("-" * w for w in widths))
    for row in body:
        print("  ".join(_pad(cell, widths[i]) for i, cell in enumerate(row)))


def _render_envelope(env: dict) -> None:
    """把展示信封渲染成终端文本。与控制台共用同一套结构。"""
    kind = env.get("kind")
    print()
    print(f"【{env.get('label', '')}】{env.get('title', '')}")
    if env.get("note"):
        print(f"  提示：{env['note']}")

    if kind == "table":
        print()
        _render_table(env.get("columns", []), env.get("rows", []), env.get("empty_hint", ""))
    elif kind == "detail":
        print()
        for field in env.get("fields", []):
            print(f"  {_pad(str(field.get('label', '')), 12)} {field.get('value', '')}")
        for section in env.get("sections", []):
            print()
            print(f"── {section.get('label', '')} " + "─" * 20)
            if section.get("kind") == "table":
                _render_table(section.get("columns", []), section.get("rows", []))
            else:
                text = section.get("text", "")
                print(text if text else "（空）")
    elif kind == "message":
        print()
        print(env.get("message", ""))
    print()


def _service(root: str):
    from kedaibiao.api.service import ConsoleService

    return ConsoleService(root=root)


def cmd_actions(args: argparse.Namespace) -> int:
    from kedaibiao.api.service import ConsoleError

    try:
        data = _service(args.root).actions(args.platform)
    except ConsoleError as exc:
        print(f"✗ {exc}")
        return 2

    actions = data["actions"]
    if not actions:
        print(f"{args.platform} 没有登记任何只读动作。")
        return 0
    print(f"{args.platform} 可用只读动作：")
    for item in actions:
        print(f"  {item['name']:<10s} {item['label']}")
        if item.get("description"):
            print(f"             {item['description']}")
        for param in item.get("params", []):
            mark = "必填" if param["required"] else "可选"
            print(f"             - {param['name']}（{param['label']}，{mark}）")
    print()
    print(f"执行：kedaibiao action {args.platform} <动作名> [key=value ...]")
    return 0


def cmd_action(args: argparse.Namespace) -> int:
    from kedaibiao.api.service import ConsoleError

    params: dict[str, str] = {}
    for item in args.param:
        if "=" not in item:
            print(f"✗ 参数要写成 key=value 形式：{item!r}")
            return 2
        key, value = item.split("=", 1)
        params[key.strip()] = value

    try:
        envelope = _service(args.root).run_action(args.platform, args.action, params)
    except ConsoleError as exc:
        print(f"✗ {exc}")
        return 2
    _render_envelope(envelope)
    return 0


HANDLERS = {
    "platforms": cmd_platforms,
    "config": cmd_config,
    "doctor": cmd_doctor,
    "bank-stats": cmd_bank_stats,
    "bank-export": cmd_bank_export,
    "bank-import": cmd_bank_import,
    "run": cmd_run,
    "web": cmd_web,
    "actions": cmd_actions,
    "action": cmd_action,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # 适配器靠自动发现注册；CLI 启动时统一装载一次
    load_builtin_adapters()
    return HANDLERS[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
