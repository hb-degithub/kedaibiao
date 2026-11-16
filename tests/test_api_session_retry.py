"""会话恢复的网络重试：DNS/超时抖动不该让整轮刷课失败。

2026-11-16 实测：刷课刚开始就撞上
``TransportError: GET https://uai.unipus.cn/api/account/user/info 失败:
<urlopen error [Errno -3] Temporary failure in name resolution>``，
几秒后同一域名解析正常 —— 那是本机网络/DNS 抖动，不是账号问题。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from kedaibiao.api.service import ConsoleService


class FlakyAdapter:
    """前 n 次 refresh_session 抛网络异常，之后成功。"""

    def __init__(self, failures: int, exc: BaseException | None = None) -> None:
        self.failures = failures
        self.exc = exc or OSError(
            "GET https://uai.unipus.cn/api/account/user/info 失败: "
            "<urlopen error [Errno -3] Temporary failure in name resolution>"
        )
        self.calls = 0

    async def refresh_session(self, credentials: object) -> bool:
        self.calls += 1
        if self.calls <= self.failures:
            raise self.exc
        return True


class SessionRetryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        (root / "config").mkdir()
        (root / "config" / "local.toml").write_text(
            "[kedaibiao]\n"
            f'db_url = "sqlite:///{root}/api.db"\n'
            f'session_dir = "{root}/sessions"\n'
            'notify = "null"\n',
            encoding="utf-8",
        )
        self.service = ConsoleService(root=root)
        self.logs: list[tuple[str, str]] = []
        self.service._job_log = lambda jid, level, text: self.logs.append((level, text))
        # 退避等待在测试里跳过（生产仍是真的等 2s/4s）
        self.service._sleep_interruptible = lambda jid, seconds: False

    def _restore(self, adapter: FlakyAdapter) -> None:
        self.service._restore_session("j-1", adapter, object())

    def test_dns_failure_is_retried_and_then_succeeds(self) -> None:
        adapter = FlakyAdapter(failures=2)
        self._restore(adapter)
        self.assertEqual(adapter.calls, 3, "两次抖动后第三次应当成功")
        self.assertTrue(any("网络抖动" in text for _, text in self.logs))

    def test_network_failure_gives_actionable_message(self) -> None:
        adapter = FlakyAdapter(failures=99)
        with self.assertRaises(RuntimeError) as ctx:
            self._restore(adapter)
        message = str(ctx.exception)
        self.assertIn("网络异常", message)
        self.assertIn("不需要重新登录", message, "别把网络问题说成账号问题")
        self.assertEqual(adapter.calls, 3, "最多试 3 次")

    def test_credential_failure_is_not_retried(self) -> None:
        class Refused(FlakyAdapter):
            async def refresh_session(self, credentials: object) -> bool:
                self.calls += 1
                return False  # 平台明确拒绝：不抛异常

        adapter = Refused(failures=0)
        with self.assertRaises(RuntimeError) as ctx:
            self._restore(adapter)
        self.assertIn("账号", str(ctx.exception))
        self.assertEqual(adapter.calls, 1, "凭据错重试没用，不该反复打平台")

    def test_non_network_error_is_not_retried(self) -> None:
        adapter = FlakyAdapter(failures=9, exc=ValueError("协议解析失败"))
        with self.assertRaises(ValueError):
            self._restore(adapter)
        self.assertEqual(adapter.calls, 1)


if __name__ == "__main__":
    unittest.main()
