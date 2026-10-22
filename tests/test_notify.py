"""通知渠道单测：邮件（SMTP，标准库）与渠道选择。全部离线，不真发信。

钉住四件事：

1. 必填项缺失时**降级为控制台**，不是静默什么都不发；
2. 发送成功路径的通知头（主题含 level、收件人正确、starttls 有走）；
3. 发送失败只返回 ``False``，绝不抛异常把跑批带崩；
4. 密码不出现在日志、异常文案或 ``repr`` 里。
"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import MagicMock, patch

from kedaibiao.config.settings import Settings
from kedaibiao.notify.base import ConsoleNotifier, EmailNotifier
from kedaibiao.runtime import build_notifier

ENV = {
    "KEDAIBIAO_SMTP_HOST": "smtp.example.com",
    "KEDAIBIAO_SMTP_PORT": "587",
    "KEDAIBIAO_SMTP_USER": "bot@example.com",
    "KEDAIBIAO_SMTP_PASSWORD": "s3cret-pass",
    "KEDAIBIAO_SMTP_TO": "a@example.com, b@example.com",
}


class FromEnvironTest(unittest.TestCase):
    def test_missing_required_returns_none(self) -> None:
        self.assertIsNone(EmailNotifier.from_environ({}))
        self.assertIsNone(EmailNotifier.from_environ({"KEDAIBIAO_SMTP_HOST": "h"}))
        self.assertIsNone(EmailNotifier.from_environ({"KEDAIBIAO_SMTP_TO": "a@b.c"}))

    def test_builds_from_env(self) -> None:
        notifier = EmailNotifier.from_environ(ENV)
        assert notifier is not None
        self.assertEqual(notifier.host, "smtp.example.com")
        self.assertEqual(notifier.port, 587)
        self.assertEqual(notifier.recipients, ["a@example.com", "b@example.com"])
        self.assertEqual(notifier.security, "starttls")
        self.assertEqual(notifier.sender, "bot@example.com")

    def test_bad_numbers_fall_back_to_defaults(self) -> None:
        notifier = EmailNotifier.from_environ(
            {**ENV, "KEDAIBIAO_SMTP_PORT": "abc", "KEDAIBIAO_SMTP_TIMEOUT": "?"}
        )
        assert notifier is not None
        self.assertEqual(notifier.port, 587)
        self.assertEqual(notifier.timeout, 15.0)

    def test_repr_hides_password(self) -> None:
        notifier = EmailNotifier.from_environ(ENV)
        assert notifier is not None
        self.assertNotIn("s3cret-pass", repr(notifier))


class SendTest(unittest.TestCase):
    def test_send_uses_starttls_and_login(self) -> None:
        notifier = EmailNotifier.from_environ(ENV)
        assert notifier is not None
        fake = MagicMock()
        with patch("kedaibiao.notify.base.smtplib.SMTP", return_value=fake) as factory:
            ok = asyncio.run(notifier.send("作业失败", "正文", level="error"))

        self.assertTrue(ok)
        factory.assert_called_once()
        self.assertEqual(factory.call_args.args[0], "smtp.example.com")
        fake.starttls.assert_called_once()
        fake.login.assert_called_once_with("bot@example.com", "s3cret-pass")
        sent = fake.send_message.call_args.args[0]
        self.assertIn("[ERROR]", sent["Subject"])
        self.assertIn("作业失败", sent["Subject"])
        self.assertEqual(sent["To"], "a@example.com, b@example.com")
        self.assertEqual(sent.get_content().strip(), "正文")
        fake.quit.assert_called_once()

    def test_ssl_mode_uses_smtp_ssl(self) -> None:
        notifier = EmailNotifier.from_environ({**ENV, "KEDAIBIAO_SMTP_SECURITY": "ssl"})
        assert notifier is not None
        fake = MagicMock()
        with patch("kedaibiao.notify.base.smtplib.SMTP_SSL", return_value=fake) as factory:
            self.assertTrue(asyncio.run(notifier.send("t", "b")))
        factory.assert_called_once()
        fake.starttls.assert_not_called()

    def test_send_failure_returns_false_and_hides_password(self) -> None:
        notifier = EmailNotifier.from_environ(ENV)
        assert notifier is not None
        with (
            patch("kedaibiao.notify.base.smtplib.SMTP", side_effect=RuntimeError("boom")),
            self.assertLogs("kedaibiao.notify.base", level="ERROR") as logs,
        ):
            ok = asyncio.run(notifier.send("t", "b", level="warning"))
        self.assertFalse(ok)
        joined = "\n".join(logs.output)
        self.assertNotIn("s3cret-pass", joined)
        # 只记异常类型：SMTP 的异常文案有时会带认证细节，不适合原样进日志
        self.assertIn("RuntimeError", joined)

    def test_quit_failure_does_not_mask_success(self) -> None:
        notifier = EmailNotifier.from_environ(ENV)
        assert notifier is not None
        fake = MagicMock()
        fake.quit.side_effect = OSError("already closed")
        with patch("kedaibiao.notify.base.smtplib.SMTP", return_value=fake):
            self.assertTrue(asyncio.run(notifier.send("t", "b")))


class BuildNotifierTest(unittest.TestCase):
    def test_default_is_console(self) -> None:
        self.assertIsInstance(build_notifier(Settings(), environ={}), ConsoleNotifier)

    def test_email_channel(self) -> None:
        notifier = build_notifier(Settings(notify="email"), environ=ENV)
        self.assertIsInstance(notifier, EmailNotifier)

    def test_email_missing_config_falls_back_with_warning(self) -> None:
        with self.assertLogs("kedaibiao.runtime", level="WARNING"):
            notifier = build_notifier(Settings(notify="email"), environ={})
        self.assertIsInstance(notifier, ConsoleNotifier)

    def test_env_file_values_are_used_for_webhook(self) -> None:
        """``.env`` 合并后的值也要生效（以前只看 os.environ）。"""
        env = {"KEDAIBIAO_NOTIFY_WEBHOOK_URL": "https://example.com/hook"}
        notifier = build_notifier(Settings(notify="webhook"), environ=env)
        self.assertEqual(notifier.name, "webhook")


if __name__ == "__main__":
    unittest.main()
