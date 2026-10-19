"""命令行入口：``python -m kedaibiao <子命令>``。

骨架阶段只提供只读/诊断类命令 —— 真正跑批要等第一个平台适配器落地。
"""

from __future__ import annotations

import argparse
import json
import sys
from importlib.util import find_spec
from pathlib import Path

from kedaibiao import __version__, adapters
from kedaibiao.bank.store import QuestionBank, ProgressStore
from kedaibiao.config.settings import load_settings
from kedaibiao.security.credentials import available_platforms

OPTIONAL_DEPS = {
    "playwright": "浏览器自动化（必需，才能真跑平台）",
    "fastapi": "Web 控制台（可选）",
    "uvicorn": "Web 控制台服务器（可选）",
    "sqlalchemy": "ORM（可选，当前用标准库 sqlite3）",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kedaibiao",
        description="课代表 —— 多平台作业/课程一体化自动化框架",
    )
    parser.add_argument("--version", action="version", version=f"kedaibiao {__version__}")
    parser.add_argument("--root", default=".", help="项目根目录（读取 config/ 用）")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("platforms", help="列出已注册的平台适配器")
    sub.add_parser("doctor", help="自检运行环境")
    sub.add_parser("config", help="打印生效配置（已脱敏）")

    stats = sub.add_parser("bank-stats", help="题库与跑批统计")
    stats.add_argument("--db", default=None, help="覆盖配置里的数据库路径")
    stats.add_argument("--runs", type=int, default=10, help="显示最近多少条执行记录")

    export = sub.add_parser("bank-export", help="导出题库为 JSONL")
    export.add_argument("out", help="输出文件路径")
    export.add_argument("--db", default=None)

    imp = sub.add_parser("bank-import", help="从 JSONL 导入题库")
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

    configured = available_platforms()
    if configured:
        print(f"凭据   : ✓ 已配置 {', '.join(configured)}（值不显示）")
    else:
        print("凭据   : （未配置任何平台凭据，见 .env.example）")

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


HANDLERS = {
    "platforms": cmd_platforms,
    "config": cmd_config,
    "doctor": cmd_doctor,
    "bank-stats": cmd_bank_stats,
    "bank-export": cmd_bank_export,
    "bank-import": cmd_bank_import,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return HANDLERS[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
