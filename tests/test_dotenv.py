""".env 解析与优先级测试。"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kedaibiao.config.dotenv import DotEnvSyntaxError, load_dotenv, parse_dotenv
from kedaibiao.config.settings import load_settings, resolve_environ
from kedaibiao.security.credentials import load_credentials


def _write(tmp: str, content: str) -> Path:
    path = Path(tmp) / ".env"
    path.write_text(content, encoding="utf-8")
    return path


class TestParseDotenv(unittest.TestCase):
    def test_missing_file_returns_empty(self) -> None:
        self.assertEqual(parse_dotenv("/nonexistent/.env"), {})

    def test_basic_pairs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, "A=1\nB=hello\n")
            self.assertEqual(parse_dotenv(path), {"A": "1", "B": "hello"})

    def test_blank_lines_and_comments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, "\n# 整行注释\nA=1\n\n   # 缩进注释\nB=2\n")
            self.assertEqual(parse_dotenv(path), {"A": "1", "B": "2"})

    def test_inline_comment_without_quotes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, "A=value # 说明\n")
            self.assertEqual(parse_dotenv(path), {"A": "value"})

    def test_hash_inside_value_is_kept_when_no_space(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, "A=pa#ss\n")
            self.assertEqual(parse_dotenv(path), {"A": "pa#ss"})

    def test_double_quotes_protect_spaces_and_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, 'A="p@ss # word"\n')
            self.assertEqual(parse_dotenv(path), {"A": "p@ss # word"})

    def test_single_quotes_are_literal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, "A='no\\nescape'\n")
            self.assertEqual(parse_dotenv(path), {"A": "no\\nescape"})

    def test_double_quote_escape_sequences(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, 'A="line1\\nline2"\n')
            self.assertEqual(parse_dotenv(path), {"A": "line1\nline2"})

    def test_export_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, "export A=1\n")
            self.assertEqual(parse_dotenv(path), {"A": "1"})

    def test_equals_in_value(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, "TOKEN=abc=def==\n")
            self.assertEqual(parse_dotenv(path), {"TOKEN": "abc=def=="})

    def test_empty_value(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, "A=\n")
            self.assertEqual(parse_dotenv(path), {"A": ""})

    def test_invalid_line_raises_with_location(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, "A=1\nnot a pair\n")
            with self.assertRaises(DotEnvSyntaxError) as cm:
                parse_dotenv(path)
            self.assertIn(":2", str(cm.exception))


class TestLoadDotenv(unittest.TestCase):
    def tearDown(self) -> None:
        os.environ.pop("KEDAIBIAO_TEST_DOTENV", None)

    def test_writes_to_environ(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, "KEDAIBIAO_TEST_DOTENV=from_file\n")
            self.assertEqual(load_dotenv(path), 1)
            self.assertEqual(os.environ["KEDAIBIAO_TEST_DOTENV"], "from_file")

    def test_does_not_override_real_env_by_default(self) -> None:
        os.environ["KEDAIBIAO_TEST_DOTENV"] = "from_shell"
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, "KEDAIBIAO_TEST_DOTENV=from_file\n")
            self.assertEqual(load_dotenv(path), 0)
            self.assertEqual(os.environ["KEDAIBIAO_TEST_DOTENV"], "from_shell")

    def test_override_true_wins(self) -> None:
        os.environ["KEDAIBIAO_TEST_DOTENV"] = "from_shell"
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, "KEDAIBIAO_TEST_DOTENV=from_file\n")
            self.assertEqual(load_dotenv(path, override=True), 1)
            self.assertEqual(os.environ["KEDAIBIAO_TEST_DOTENV"], "from_file")


class TestLoadSettingsWithDotenv(unittest.TestCase):
    def test_dotenv_is_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".env").write_text(
                "KEDAIBIAO_MAX_CONCURRENCY=11\n", encoding="utf-8"
            )
            with mock.patch.dict(os.environ, {}, clear=True):
                self.assertEqual(load_settings(root=tmp).max_concurrency, 11)

    def test_real_env_beats_dotenv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".env").write_text(
                "KEDAIBIAO_MAX_CONCURRENCY=11\n", encoding="utf-8"
            )
            with mock.patch.dict(os.environ, {"KEDAIBIAO_MAX_CONCURRENCY": "5"}, clear=True):
                self.assertEqual(load_settings(root=tmp).max_concurrency, 5)

    def test_dotenv_beats_toml(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config"
            cfg.mkdir()
            (cfg / "default.toml").write_text("[kedaibiao]\nmax_concurrency = 7\n", encoding="utf-8")
            (Path(tmp) / ".env").write_text("KEDAIBIAO_MAX_CONCURRENCY=11\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {}, clear=True):
                self.assertEqual(load_settings(root=tmp).max_concurrency, 11)

    def test_use_dotenv_false_ignores_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".env").write_text(
                "KEDAIBIAO_MAX_CONCURRENCY=11\n", encoding="utf-8"
            )
            with mock.patch.dict(os.environ, {}, clear=True):
                self.assertEqual(
                    load_settings(root=tmp, use_dotenv=False).max_concurrency, 3
                )

    def test_explicit_environ_ignores_dotenv(self) -> None:
        """显式传 environ 时应完全跳过 .env，避免测试之间互相污染。"""
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".env").write_text(
                "KEDAIBIAO_MAX_CONCURRENCY=11\n", encoding="utf-8"
            )
            settings = load_settings(root=tmp, environ={})
            self.assertEqual(settings.max_concurrency, 3)


class TestResolveEnviron(unittest.TestCase):
    """`resolve_environ` 是配置与凭据共用的唯一环境来源。

    回归背景：早期 `load_settings` 只把 `.env` 合进局部字典，凭据层却自己读
    `os.environ`，导致 `.env` 里配的凭据在 CLI/控制台上完全看不见。
    """

    def test_file_then_real_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".env").write_text(
                "KEDAIBIAO_A=from_file\nKEDAIBIAO_B=from_file\n", encoding="utf-8"
            )
            with mock.patch.dict(os.environ, {"KEDAIBIAO_B": "from_shell"}, clear=True):
                env = resolve_environ(tmp)
            self.assertEqual(env["KEDAIBIAO_A"], "from_file")
            self.assertEqual(env["KEDAIBIAO_B"], "from_shell", "真实环境变量优先")

    def test_explicit_environ_ignores_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".env").write_text("KEDAIBIAO_A=from_file\n", encoding="utf-8")
            self.assertEqual(resolve_environ(tmp, environ={"X": "1"}), {"X": "1"})

    def test_use_dotenv_false(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".env").write_text("KEDAIBIAO_A=from_file\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {}, clear=True):
                self.assertNotIn("KEDAIBIAO_A", resolve_environ(tmp, use_dotenv=False))

    def test_does_not_mutate_os_environ(self) -> None:
        """刻意不回写 os.environ —— 全局副作用会污染测试与调用方。"""
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".env").write_text("KEDAIBIAO_SIDE_EFFECT=1\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {}, clear=True):
                resolve_environ(tmp)
                self.assertNotIn("KEDAIBIAO_SIDE_EFFECT", os.environ)

    def test_settings_and_credentials_share_one_source(self) -> None:
        """同一个 root 下，配置与凭据必须看到同一份环境。"""
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".env").write_text(
                "KEDAIBIAO_MAX_CONCURRENCY=9\nKEDAIBIAO_UNIPUS_USER=stu\n"
                "KEDAIBIAO_UNIPUS_PASS=pw\n",
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {}, clear=True):
                env = resolve_environ(tmp)
                settings = load_settings(root=tmp, environ=env)
                creds = load_credentials("unipus", environ=env)
            self.assertEqual(settings.max_concurrency, 9)
            assert creds is not None
            self.assertEqual(creds.username, "stu")


if __name__ == "__main__":
    unittest.main()
