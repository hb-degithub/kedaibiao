#!/usr/bin/env python3
"""入库前敏感信息检查。

用法：

    python3 scripts/check_secrets.py            # 检查暂存区（git add 之后）
    python3 scripts/check_secrets.py --all      # 检查全部已跟踪文件
    python3 scripts/check_secrets.py --install-hook   # 装成 pre-commit 钩子

## 为什么需要它

这个仓库有多个窗口并行开发，`git add -A` 一不小心就会把 `.env`、cookie、
数据库或者含真实数据的截图带进去。**一次提交就永久留在历史里**，清理要改写
historia（本仓库已经为这个付过一次代价）。

## 检查三类东西

1. **绝不该入库的路径**：凭据、cookie、数据库、本地截图、抓包；
2. **硬编码的凭据**：password/token/secret 后面跟着长字面量；
3. **本地 deny 列表**（``.secret-denylist``，已 gitignore）：真实 ID、姓名、
   课程名等——这些没法用正则识别，只能由人登记。

退出码：0 干净，1 发现问题。
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: 路径模式：命中即拒绝（即使是文本也要看内容）
FORBIDDEN_PATHS = [
    re.compile(r"(^|/)\.env$"),
    re.compile(r"(^|/)\.env\.(?!example$)[^/]+$"),
    re.compile(r"\.cookies\.json$"),
    re.compile(r"storage_state.*\.json$"),
    re.compile(r"\.db$"),
    re.compile(r"\.sqlite3?$"),
    re.compile(r"\.har$"),
    re.compile(r"(^|/)data/"),
    re.compile(r"(^|/)docs/screenshots/"),
    re.compile(r"(^|/)config/local\."),
    re.compile(r"\.pem$"),
    re.compile(r"(^|/)id_rsa"),
]

#: 内容模式：疑似硬编码凭据
SECRET_PATTERNS = [
    (re.compile(r"""(password|passwd|pwd)\s*=\s*["'][^"'\s]{8,}["']""", re.I), "疑似硬编码密码"),
    (re.compile(r"""(secret|api[_-]?key|access[_-]?key)\s*=\s*["'][^"'\s]{12,}["']""", re.I), "疑似硬编码密钥"),
    (re.compile(r"""Authorization\s*[:=]\s*["']?Bearer\s+[A-Za-z0-9._-]{20,}"""), "疑似真实 Bearer 令牌"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "私钥"),
]

#: 这些词出现在同一行时说明是测试/示例，不算泄漏
BENIGN_HINTS = re.compile(
    r"example|placeholder|dummy|fake|test|sample|xxx|your[_-]?|示例|占位|假|演示|"
    r"无实际|测试用|哨兵|sentinel|topsecret|noqa:\s*secret-check",
    re.I,
)

#: 这些「值」其实是字段名/常量名，不是凭据
NON_SECRET_VALUES = {
    "password", "passwd", "pwd", "username", "user_name", "token", "secret",
    "api_key", "access_key", "authorization", "cookie", "session", "captcha",
}

TEXT_SUFFIXES = {".py", ".md", ".toml", ".cfg", ".ini", ".txt", ".json", ".yaml", ".yml",
                 ".js", ".html", ".css", ".sh", ".env", ""}


def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, check=False
    )
    return result.stdout


def files_to_check(all_tracked: bool) -> list[str]:
    if all_tracked:
        return [f for f in _git("ls-files").splitlines() if f]
    staged = _git("diff", "--cached", "--name-only", "--diff-filter=ACMR").splitlines()
    return [f for f in staged if f]


def load_denylist() -> list[str]:
    """本地 deny 列表：每行一个敏感字面量（真实 ID、姓名、课程名…）。"""
    path = ROOT / ".secret-denylist"
    if not path.is_file():
        return []
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def check(files: list[str], denylist: list[str]) -> list[str]:
    problems: list[str] = []

    for rel in files:
        for pattern in FORBIDDEN_PATHS:
            if pattern.search(rel):
                problems.append(f"{rel}: 这类文件不允许入库（凭据/数据/本地截图）")
                break

        path = ROOT / rel
        if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue

        for lineno, line in enumerate(text.splitlines(), start=1):
            if BENIGN_HINTS.search(line):
                continue
            for pattern, label in SECRET_PATTERNS:
                match = pattern.search(line)
                if not match:
                    continue
                # 「值」本身就是字段名时不算泄漏（例：FIELD_SSO_PASSWORD = "password"）
                captured = match.group(1) if match.groups() else ""
                if captured.strip().lower() in NON_SECRET_VALUES:
                    continue
                problems.append(f"{rel}:{lineno}: {label}")
            for literal in denylist:
                if literal in line:
                    problems.append(f"{rel}:{lineno}: 命中本地 deny 列表（{literal!r}）")

    return problems


HOOK = """#!/bin/sh
# 由 scripts/check_secrets.py --install-hook 生成
if ! python3 scripts/check_secrets.py; then
  echo ""
  echo "提交被拒绝：发现疑似敏感信息。"
  echo "确认是误报可临时跳过： git commit --no-verify"
  exit 1
fi
exit 0
"""


def install_hook() -> int:
    hooks = ROOT / ".git" / "hooks"
    if not hooks.is_dir():
        print("✗ 找不到 .git/hooks，确认这是 git 仓库根目录")
        return 1
    target = hooks / "pre-commit"
    target.write_text(HOOK, encoding="utf-8")
    target.chmod(0o755)
    print(f"✓ 已安装 pre-commit 钩子: {target}")
    return 0


def main(argv: list[str]) -> int:
    if "--install-hook" in argv:
        return install_hook()

    files = files_to_check("--all" in argv)
    if not files:
        print("没有需要检查的文件。")
        return 0

    problems = check(files, load_denylist())
    if problems:
        print(f"✗ 发现 {len(problems)} 处疑似敏感信息：\n")
        for item in problems:
            print("  ", item)
        print(
            "\n处理方式：\n"
            "  - 凭据 / 数据 / 截图：加进 .gitignore 并 git rm --cached\n"
            "  - 文档里的真实 ID：换成明显的假值\n"
            "  - 若确实需要登记更多真实值，写进 .secret-denylist（该文件已被 gitignore）\n"
            "  - 确认误报可跳过本次检查：git commit --no-verify"
        )
        return 1

    print(f"✓ {len(files)} 个文件未发现敏感信息")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
