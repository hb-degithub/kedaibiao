"""控制台可改配置（app_settings）测试：存 SQLite、覆盖 .env、只写不读密钥。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from kedaibiao.api.service import ConsoleError, ConsoleService, Viewer
from kedaibiao.config.overrides import OverrideStore

SUPER = Viewer(user_id=1, is_superadmin=True, username="admin")
NORMAL = Viewer(user_id=2, is_superadmin=False, username="bob")


def make_service(root: Path) -> ConsoleService:
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "config" / "local.toml").write_text(
        "[kedaibiao]\n"
        f'db_url = "sqlite:///{root}/settings.db"\n'
        f'session_dir = "{root}/sessions"\n'
        'notify = "null"\n',
        encoding="utf-8",
    )
    return ConsoleService(root=root)


class TestOverrideStore(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = OverrideStore(Path(self.tmp.name) / "x.db")

    def test_put_get_delete(self) -> None:
        self.assertEqual(self.store.put_many({"llm.model": "m1", "llm.timeout": "30"}), 2)
        self.assertEqual(self.store.all("llm."), {"llm.model": "m1", "llm.timeout": "30"})
        self.store.put_many({"llm.model": "m2"})
        self.assertEqual(self.store.all("llm.")["llm.model"], "m2", "同键应覆盖")
        self.store.delete(["llm.model"])
        self.assertEqual(self.store.all("llm."), {"llm.timeout": "30"})

    def test_missing_db_is_not_an_error(self) -> None:
        empty = OverrideStore(Path(self.tmp.name) / "nope" / "deep" / "y.db")
        self.assertEqual(empty.all("llm."), {})


class TestLlmConfig(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.service = make_service(self.root)

    # -- 状态 -------------------------------------------------------------

    def test_default_is_not_configured(self) -> None:
        status = self.service.llm_status()
        self.assertFalse(status["enabled"])
        self.assertEqual(status["source"], "none")
        self.assertTrue(status["editable"])

    def test_console_values_win_over_env(self) -> None:
        service = ConsoleService(
            root=self.root,
            environ={
                "KEDAIBIAO_LLM_BASE_URL": "https://env.example/v1",
                "KEDAIBIAO_LLM_MODEL": "env-model",
                "KEDAIBIAO_LLM_API_KEY": "env-key",
            },
        )
        self.assertEqual(service.llm_status()["source"], "env")

        service.update_llm_config(SUPER, {"model": "console-model"})
        status = service.llm_status()
        self.assertEqual(status["model"], "console-model")
        self.assertEqual(status["api_base"], "https://env.example/v1", "没改的字段回落到 .env")
        self.assertEqual(status["source"], "console")
        self.assertEqual(status["override_fields"], ["model"])

    def test_api_key_is_write_only(self) -> None:
        self.service.update_llm_config(SUPER, {
            "base_url": "https://g.example/v1", "model": "m", "api_key": "sk-secret",
        })
        status = self.service.llm_status()
        self.assertTrue(status["api_key_set"])
        blob = repr(status)
        self.assertNotIn("sk-secret", blob, "状态里绝不能出现密钥明文")

    def test_clear_api_key(self) -> None:
        self.service.update_llm_config(SUPER, {
            "base_url": "https://g.example/v1", "model": "m", "api_key": "sk-x",
        })
        self.service.update_llm_config(SUPER, {"clear_api_key": True})
        self.assertFalse(self.service.llm_status()["api_key_set"])

    def test_blank_field_removes_override(self) -> None:
        self.service.update_llm_config(SUPER, {"base_url": "https://g.example/v1", "model": "m"})
        self.service.update_llm_config(SUPER, {"model": ""})
        status = self.service.llm_status()
        self.assertEqual(status["model"], "", "清空后不再有覆盖值")
        self.assertNotIn("model", status["override_fields"])

    def test_status_is_readonly_view_of_center(self) -> None:
        """工具页首屏读的就是这个视图：必须可读，且不含密钥。"""
        self.service.update_llm_config(SUPER, {
            "base_url": "https://g.example/v1", "model": "m", "api_key": "sk-hidden",
        })
        status = self.service.llm_status()
        for key in ("enabled", "ocr_model", "escalate_enabled",
                    "escalate_threshold", "fallback_model", "editable"):
            self.assertIn(key, status)
        self.assertNotIn("sk-hidden", repr(status))

    # -- OCR / 升级 -------------------------------------------------------

    def test_ocr_and_escalation_fields_roundtrip(self) -> None:
        self.service.update_llm_config(SUPER, {
            "base_url": "https://g.example/v1",
            "model": "cheap",
            "ocr_model": "qwen-vl-max",
            "ocr_base_url": "https://ocr.example/v1",
            "ocr_api_key": "ocr-secret",
            "escalate_threshold": 0.8,
            "fallback_model": "reasoner",
            "fallback_api_key": "fb-secret",
        })
        status = self.service.llm_status()
        self.assertEqual(status["ocr_model"], "qwen-vl-max")
        self.assertEqual(status["ocr_api_base"], "https://ocr.example/v1")
        self.assertTrue(status["ocr_api_key_set"])
        self.assertEqual(status["escalate_threshold"], 0.8)
        self.assertTrue(status["escalate_enabled"])
        self.assertEqual(status["fallback_model"], "reasoner")
        self.assertTrue(status["fallback_api_key_set"])
        # 两个密钥都只写不读
        blob = repr(status)
        self.assertNotIn("ocr-secret", blob)
        self.assertNotIn("fb-secret", blob)

    def test_threshold_out_of_range_rejected(self) -> None:
        for bad in (-0.1, 1.5, "abc"):
            with self.subTest(value=bad), self.assertRaises(ConsoleError) as ctx:
                self.service.update_llm_config(SUPER, {"escalate_threshold": bad})
            self.assertEqual(ctx.exception.status, 400)

    def test_threshold_zero_disables_escalation(self) -> None:
        self.service.update_llm_config(SUPER, {"fallback_model": "big", "escalate_threshold": 0.8})
        self.assertTrue(self.service.llm_status()["escalate_enabled"])
        self.service.update_llm_config(SUPER, {"escalate_threshold": 0})
        status = self.service.llm_status()
        self.assertFalse(status["escalate_enabled"])
        self.assertNotIn("escalate_threshold", status["override_fields"], "0 = 删除覆盖")

    def test_clear_extra_keys(self) -> None:
        self.service.update_llm_config(SUPER, {
            "model": "m", "ocr_api_key": "a", "fallback_api_key": "b",
        })
        self.service.update_llm_config(SUPER, {"clear_ocr_api_key": True})
        status = self.service.llm_status()
        self.assertFalse(status["ocr_api_key_set"], "OCR key 清空后应回落到主模型")
        self.assertTrue(status["fallback_api_key_set"])

    def test_ocr_base_url_validated(self) -> None:
        with self.assertRaises(ConsoleError) as ctx:
            self.service.update_llm_config(SUPER, {"ocr_base_url": "ftp://x"})
        self.assertEqual(ctx.exception.status, 400)

    # -- 校验 -------------------------------------------------------------

    def test_non_superadmin_is_forbidden(self) -> None:
        with self.assertRaises(ConsoleError) as ctx:
            self.service.update_llm_config(NORMAL, {"model": "x"})
        self.assertEqual(ctx.exception.status, 403)

    def test_bad_base_url(self) -> None:
        with self.assertRaises(ConsoleError) as ctx:
            self.service.update_llm_config(SUPER, {"base_url": "ftp://x"})
        self.assertEqual(ctx.exception.status, 400)
        self.assertIn("http", str(ctx.exception))

    def test_bad_numbers(self) -> None:
        for payload in ({"temperature": 9}, {"temperature": -1}, {"timeout": 0},
                        {"timeout": 9999}, {"timeout": "abc"}):
            with self.assertRaises(ConsoleError, msg=str(payload)):
                self.service.update_llm_config(SUPER, payload)

    def test_timeout_temperature_stored_compactly(self) -> None:
        self.service.update_llm_config(SUPER, {"timeout": 45.0, "temperature": 0.3})
        overrides = OverrideStore(self.service._db_path()).all("llm.")
        self.assertEqual(overrides["llm.timeout"], "45")
        self.assertEqual(overrides["llm.temperature"], "0.3")

    # -- 测试连接 ---------------------------------------------------------

    def test_test_connection_requires_config(self) -> None:
        with self.assertRaises(ConsoleError) as ctx:
            self.service.test_llm_connection(SUPER)
        self.assertEqual(ctx.exception.status, 400)

    def test_test_connection_reports_failure_as_502(self) -> None:
        from unittest import mock

        self.service.update_llm_config(SUPER, {"base_url": "https://g.example/v1", "model": "m"})
        with (
            mock.patch(
                "kedaibiao.tasks.llm.LLMAnswerer.complete",
                side_effect=RuntimeError("boom"),
            ),
            self.assertRaises(ConsoleError) as ctx,
        ):
            self.service.test_llm_connection(SUPER)
        self.assertEqual(ctx.exception.status, 502)
        self.assertIn("boom", str(ctx.exception))

    def test_test_connection_success_shape(self) -> None:
        from unittest import mock

        self.service.update_llm_config(SUPER, {"base_url": "https://g.example/v1", "model": "m"})

        async def fake_complete(self, system: str, user: str, *, max_tokens: int = 1200) -> str:
            return "OK"

        with mock.patch("kedaibiao.tasks.llm.LLMAnswerer.complete", fake_complete):
            out = self.service.test_llm_connection(SUPER)
        self.assertTrue(out["ok"])
        self.assertEqual(out["reply"], "OK")
        self.assertEqual(out["model"], "m")
        self.assertIn("latency_ms", out)


if __name__ == "__main__":
    unittest.main()
