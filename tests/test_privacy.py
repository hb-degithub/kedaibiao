"""仓库隐私边界：**任何一次提交都不能把真实账号数据带进版本库**。

这个仓库会公开发布（AGPL-3.0），而开发过程用的全是真实账号：一旦某次
``git add -A`` 把手机号、cookie、截图、抓包带进去，历史里就永久留下了。

三条断言（全部离线，不联网，也不读 ``data/``）：

1. **已跟踪文件里没有机器可识别的身份字段** —— 手机号、``uid`` / ``personId`` /
   ``cpi`` 这类账号号段，以及硬编码凭据；
2. **敏感路径永远不被跟踪** —— ``.env``、``data/``、``docs/screenshots/``、
   数据库、cookie / storage_state、本地 deny 列表；
3. **本机 ``.secret-denylist`` 里的真实值不出现在仓库文件里** —— 姓名、学校、
   班级、作业号这类正则认不出来的值，只能靠人工登记 + 这条断言兜底
   （列表不在仓库里，所以别的机器上这条自动跳过）。

真值修复方式见 ``docs/security.md``；临时绕过用 ``git commit --no-verify``。
"""

from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: 假账号号段：脱敏后统一用 1000000xx（见 docs/security.md 的「换成什么假值」）。
#: 真实 uid / personId / cpi 都是 7~9 位，所以只查 7 位以上，6 位的构造样本放行。
FAKE_ID_PREFIX = "1000000"

#: 允许出现在仓库里的「手机号」——只有公认的占位号
ALLOWED_PHONES = {"13800138000"}

PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
ACCOUNT_RE = re.compile(
    r"""(?:
        (?:\bpersonId|\bcpi|\buid|\buserId|_uid)\s*[=:]\s*['"]?(\d{7,})
      | window\["uid"\]\s*=\s*['"](\d+)
    )""",
    re.VERBOSE,
)
BEARER_RE = re.compile(r"Authorization\s*[:=]\s*[\"']?Bearer\s+[A-Za-z0-9._-]{20,}")
SK_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{20,}")
PRIVATE_KEY_RE = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")

#: 永远不允许被跟踪的路径
FORBIDDEN_PATHS = [
    (re.compile(r"(^|/)\.env$"), "凭据文件"),
    (re.compile(r"(^|/)\.env\.(?!example$)"), "凭据文件"),
    (re.compile(r"(^|/)data/"), "运行期数据（含账号/数据库）"),
    (re.compile(r"(^|/)logs/"), "运行日志"),
    (re.compile(r"(^|/)docs/screenshots/"), "含真实平台数据的截图"),
    (re.compile(r"\.(db|sqlite3?)$"), "数据库"),
    (re.compile(r"storage_state.*\.json$"), "登录态快照"),
    (re.compile(r"\.cookies?\.json$"), "cookie 快照"),
    (re.compile(r"\.session\.json$"), "会话快照"),
    (re.compile(r"(^|/)config/local\."), "本地配置"),
    (re.compile(r"(^|/)\.secret-denylist$"), "本地敏感字面量登记表"),
    (re.compile(r"\.har$"), "抓包"),
    (re.compile(r"(^|/)id_rsa"), "私钥"),
]

TEXT_SUFFIXES = {
    "", ".py", ".md", ".toml", ".cfg", ".ini", ".txt", ".json", ".yaml", ".yml",
    ".js", ".html", ".css", ".sh", ".env", ".example",
}


def _git(*args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args], cwd=ROOT, capture_output=True, text=True, check=False
        )
    except OSError:  # pragma: no cover - 没有 git 的环境（sdist 里跑测试）
        return ""
    return result.stdout if result.returncode == 0 else ""


def _tracked_files() -> list[str]:
    return [line for line in _git("ls-files").splitlines() if line]


def _denylist() -> list[str]:
    path = ROOT / ".secret-denylist"
    if not path.is_file():
        return []
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


class TestSensitivePathsNeverTracked(unittest.TestCase):
    """凭据 / 数据 / 截图 / 会话：一次都不许进版本库。"""

    def test_tracked_paths_are_clean(self) -> None:
        files = _tracked_files()
        if not files:
            self.skipTest("没有 git 元数据（在 sdist / 归档里跑测试）")
        for rel in files:
            for pattern, why in FORBIDDEN_PATHS:
                if pattern.search(rel):
                    self.fail(f"{rel} 属于「{why}」，不允许被 git 跟踪")


class TestNoAccountDataInTrackedFiles(unittest.TestCase):
    """账号级标识（手机号 / uid / personId / 令牌）不得留在仓库里。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.files = [
            rel for rel in _tracked_files()
            if Path(rel).suffix.lower() in TEXT_SUFFIXES
        ]

    def _texts(self):
        for rel in self.files:
            path = ROOT / rel
            try:
                yield rel, path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue

    def test_no_real_phone_numbers(self) -> None:
        for rel, text in self._texts():
            for match in PHONE_RE.finditer(text):
                number = match.group(0)
                if number in ALLOWED_PHONES:
                    continue
                self.fail(f"{rel}: 疑似真实手机号（{number[:3]}****{number[-2:]}）")

    def test_account_ids_are_fake(self) -> None:
        for rel, text in self._texts():
            for match in ACCOUNT_RE.finditer(text):
                value = match.group(1) or match.group(2) or ""
                if not value.startswith(FAKE_ID_PREFIX):
                    self.fail(
                        f"{rel}: 账号号段 {value} 不是约定的假值前缀 {FAKE_ID_PREFIX}xx"
                    )

    def test_no_hardcoded_credentials(self) -> None:
        for rel, text in self._texts():
            for pattern, label in (
                (BEARER_RE, "Bearer 令牌"),
                (SK_RE, "API Key"),
                (PRIVATE_KEY_RE, "私钥"),
            ):
                if pattern.search(text):
                    self.fail(f"{rel}: 疑似硬编码{label}")

    def test_local_denylist_absent(self) -> None:
        denylist = _denylist()
        if not denylist:
            self.skipTest("本机没有 .secret-denylist（该文件不入库）")
        for rel, text in self._texts():
            for literal in denylist:
                if literal in text:
                    self.fail(f"{rel}: 命中本机 deny 列表（{literal!r}）")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
