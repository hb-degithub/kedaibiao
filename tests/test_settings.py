"""配置加载测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from kedaibiao.config.settings import Settings, load_settings


class TestFromEnv(unittest.TestCase):
    def test_bool_true_variants(self) -> None:
        for raw in ("1", "true", "TRUE", "yes", "on"):
            s = Settings.from_env({"KEDAIBIAO_HEADLESS": raw})
            self.assertTrue(s.headless, raw)

    def test_bool_false_variants(self) -> None:
        for raw in ("0", "false", "no", "off", ""):
            s = Settings.from_env({"KEDAIBIAO_HEADLESS": raw})
            self.assertFalse(s.headless, raw)

    def test_dry_run_env_is_not_inverted(self) -> None:
        self.assertFalse(Settings.from_env({"KEDAIBIAO_DRY_RUN": "0"}).dry_run)
        self.assertTrue(Settings.from_env({"KEDAIBIAO_DRY_RUN": "1"}).dry_run)

    def test_int_and_float(self) -> None:
        s = Settings.from_env({"KEDAIBIAO_MAX_CONCURRENCY": "7", "KEDAIBIAO_MIN_CONFIDENCE": "0.5"})
        self.assertEqual(s.max_concurrency, 7)
        self.assertEqual(s.min_confidence, 0.5)

    def test_unknown_env_ignored(self) -> None:
        s = Settings.from_env({"KEDAIBIAO_NOT_A_FIELD": "x"})
        self.assertEqual(s, Settings())

    def test_defaults_are_safe(self) -> None:
        s = Settings()
        self.assertTrue(s.dry_run, "默认必须干跑，否则第一次跑就真交卷了")
        self.assertEqual(s.max_concurrency, 3)

    def test_db_path_conversion(self) -> None:
        self.assertEqual(Settings(db_url="sqlite:///./data/a.db").db_path, Path("./data/a.db"))
        self.assertEqual(Settings(db_url="/tmp/raw.db").db_path, Path("/tmp/raw.db"))


class TestMerged(unittest.TestCase):
    def test_merge_overrides(self) -> None:
        s = Settings().merged({"max_concurrency": 5, "dry_run": False})
        self.assertEqual(s.max_concurrency, 5)
        self.assertFalse(s.dry_run)

    def test_unknown_key_rejected(self) -> None:
        with self.assertRaises(ValueError) as cm:
            Settings().merged({"nope": 1})
        self.assertIn("nope", str(cm.exception))

    def test_to_dict_roundtrip(self) -> None:
        s = Settings(max_concurrency=9)
        self.assertEqual(s.to_dict()["max_concurrency"], 9)


class TestLoadSettings(unittest.TestCase):
    def test_missing_toml_is_fine(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(load_settings(root=tmp), Settings())

    def test_toml_then_env_priority(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config"
            cfg.mkdir()
            (cfg / "default.toml").write_text(
                "[kedaibiao]\nmax_concurrency = 4\nheadless = false\n", encoding="utf-8"
            )
            (cfg / "local.toml").write_text("[kedaibiao]\nmax_concurrency = 6\n", encoding="utf-8")

            s = load_settings(root=tmp)
            self.assertEqual(s.max_concurrency, 6, "local 覆盖 default")
            self.assertFalse(s.headless)

            s2 = load_settings(root=tmp, environ={"KEDAIBIAO_MAX_CONCURRENCY": "8"})
            self.assertEqual(s2.max_concurrency, 8, "环境变量优先级最高")

    def test_flat_toml_without_section(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config"
            cfg.mkdir()
            (cfg / "default.toml").write_text("max_concurrency = 2\n", encoding="utf-8")
            self.assertEqual(load_settings(root=tmp).max_concurrency, 2)

    def test_invalid_toml_key_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config"
            cfg.mkdir()
            (cfg / "default.toml").write_text("[kedaibiao]\nbogus = 1\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_settings(root=tmp)


if __name__ == "__main__":
    unittest.main()
