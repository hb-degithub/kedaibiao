"""凭据装载测试。重点不是「能读到」，而是「读不到时报错清楚」和「绝不泄漏」。"""

from __future__ import annotations

import logging
import unittest

from kedaibiao.core.models import Credentials
from kedaibiao.security.credentials import (
    CredentialError,
    CredentialSource,
    available_platforms,
    describe,
    env_key,
    load_credentials,
)


class TestEnvKey(unittest.TestCase):
    def test_naming_convention(self) -> None:
        self.assertEqual(env_key("unipus", "user"), "KEDAIBIAO_UNIPUS_USER")
        self.assertEqual(env_key("pigai", "PASS"), "KEDAIBIAO_PIGAI_PASS")

    def test_case_insensitive_platform(self) -> None:
        self.assertEqual(env_key("UniPus", "token"), "KEDAIBIAO_UNIPUS_TOKEN")


class TestLoadCredentials(unittest.TestCase):
    def test_happy_path(self) -> None:
        creds = load_credentials(
            "unipus", environ={"KEDAIBIAO_UNIPUS_USER": "s1", "KEDAIBIAO_UNIPUS_PASS": "pw"}
        )
        assert creds is not None
        self.assertEqual(creds.username, "s1")
        self.assertEqual(creds.password, "pw")

    def test_token_only_is_valid(self) -> None:
        creds = load_credentials("unipus", environ={"KEDAIBIAO_UNIPUS_TOKEN": "tok"})
        assert creds is not None
        self.assertEqual(creds.username, "")
        self.assertEqual(creds.extra["token"], "tok")

    def test_extra_fields_captured(self) -> None:
        creds = load_credentials(
            "neusoft",
            environ={
                "KEDAIBIAO_NEUSOFT_USER": "u",
                "KEDAIBIAO_NEUSOFT_SCHOOL": "东软学院",
                "KEDAIBIAO_NEUSOFT_UNRELATED": "x",
            },
        )
        assert creds is not None
        self.assertEqual(creds.extra, {"school": "东软学院"})

    def test_missing_raises_with_actionable_message(self) -> None:
        with self.assertRaises(CredentialError) as cm:
            load_credentials("unipus", environ={})
        message = str(cm.exception)
        self.assertIn("KEDAIBIAO_UNIPUS_USER", message)
        self.assertIn(".env", message)

    def test_missing_returns_none_when_not_required(self) -> None:
        self.assertIsNone(load_credentials("unipus", environ={}, require=False))

    def test_whitespace_username_treated_as_missing(self) -> None:
        with self.assertRaises(CredentialError):
            load_credentials("unipus", environ={"KEDAIBIAO_UNIPUS_USER": "   "})

    def test_other_platforms_credentials_do_not_leak(self) -> None:
        with self.assertRaises(CredentialError):
            load_credentials("unipus", environ={"KEDAIBIAO_PIGAI_USER": "someone"})

    def test_password_preserves_inner_spaces(self) -> None:
        creds = load_credentials(
            "unipus", environ={"KEDAIBIAO_UNIPUS_USER": "u", "KEDAIBIAO_UNIPUS_PASS": " a b "}
        )
        assert creds is not None
        self.assertEqual(creds.password, " a b ")

    def test_reads_os_environ_by_default(self) -> None:
        import os
        from unittest import mock

        with mock.patch.dict(os.environ, {"KEDAIBIAO_UNIPUS_USER": "fromenv"}, clear=True):
            creds = load_credentials("unipus")
        assert creds is not None
        self.assertEqual(creds.username, "fromenv")


class TestNoLeak(unittest.TestCase):
    def test_describe_masks_password_and_token(self) -> None:
        creds = Credentials(username="stu", password="topsecret", extra={"token": "tok123"})
        text = describe(creds)
        self.assertIn("stu", text)
        self.assertIn("已设置", text)
        self.assertIn("token", text)
        self.assertNotIn("topsecret", text)
        self.assertNotIn("tok123", text)

    def test_describe_handles_empty_password(self) -> None:
        text = describe(Credentials(username="stu"))
        self.assertIn("未设置", text)
        self.assertIn("无", text)

    def test_logging_credentials_does_not_leak(self) -> None:
        """把 Credentials 塞进日志参数也不能泄漏密码。"""
        creds = Credentials(username="stu", password="topsecret")
        with self.assertLogs("kedaibiao.test", level="INFO") as captured:
            logging.getLogger("kedaibiao.test").info("登录中 %s", creds)
        self.assertNotIn("topsecret", "\n".join(captured.output))

    def test_repr_in_container_does_not_leak(self) -> None:
        creds = Credentials(username="stu", password="topsecret")
        self.assertNotIn("topsecret", repr({"unipus": creds}))


class TestAvailablePlatforms(unittest.TestCase):
    def test_scans_user_keys_only(self) -> None:
        environ = {
            "KEDAIBIAO_UNIPUS_USER": "u",
            "KEDAIBIAO_PIGAI_USER": "p",
            "KEDAIBIAO_PIGAI_PASS": "x",
            "KEDAIBIAO_UNIPUS_TOKEN": "t",
            "KEDAIBIAO_MAX_CONCURRENCY": "3",
            "PATH": "/bin",
        }
        self.assertEqual(available_platforms(environ), ["pigai", "unipus"])

    def test_empty(self) -> None:
        self.assertEqual(available_platforms({"PATH": "/bin"}), [])


class TestCredentialSource(unittest.TestCase):
    def test_describe_lists_keys_not_values(self) -> None:
        source = CredentialSource("unipus", ["KEDAIBIAO_UNIPUS_USER"])
        self.assertIn("KEDAIBIAO_UNIPUS_USER", source.describe())

    def test_describe_empty(self) -> None:
        self.assertIn("<无>", CredentialSource("unipus").describe())


if __name__ == "__main__":
    unittest.main()
