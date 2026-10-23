"""验证码登录：自动识别 → 人工兜底（离线单测）。

钉住四件事：

1. **识别结果只接受纯字符**：带解释、空格、标点的输出一律丢弃，不猜；
2. **先自动**：OCR 拿到码就填进去提交，成功即结束（不弹人工）；
3. **失败才转人工**：识别不出、码不对、页面上找不到输入框，都落到
   ``manual_captcha`` 并把截图交给控制台；
4. **不硬撞**：自动重试有上限（``AUTO_RETRIES``），错码后不再无限试。
"""

from __future__ import annotations

import asyncio
import unittest

from kedaibiao.browser.captcha_ocr import normalize_code
from kedaibiao.browser.login_session import (
    AUTO_RETRIES,
    InteractiveLogin,
    LoginSessionError,
    LoginSpec,
)


class NormalizeCodeTest(unittest.TestCase):
    def test_accepts_plain_codes(self) -> None:
        # 模型经常带空格/引号/句号回来 —— 这些都按「同一个码」处理
        for raw in ("1234", "ab12", " 8x9y ", "`5678`", "1234。", "12 34"):
            with self.subTest(raw=raw):
                self.assertTrue(normalize_code(raw), raw)

    def test_rejects_explanations(self) -> None:
        for raw in ("", "图片里的验证码是 1234", "验证码", "abcdefgh", "12-34"):
            with self.subTest(raw=raw):
                self.assertEqual(normalize_code(raw), "", "不合形态就丢弃，不猜")


class FakePage:
    """够用的假页面：记录动作，按剧本返回验证码与登录态。"""

    def __init__(self, *, codes: list[str] | None = None, login_after: int = 99) -> None:
        self.codes = list(codes or ["1234"])
        self.login_after = login_after
        self.fills: list[tuple[str, str]] = []
        self.clicks: list[str] = []
        self.index = 0
        self.code_index = 0
        self._logged = False

    async def goto(self, url: str, **_k: object) -> None:
        self.url = url

    async def wait_for(self, selector: str, **_k: object) -> None:
        return None

    async def fill(self, selector: str, value: str) -> None:
        self.fills.append((selector, value))

    async def click(self, selector: str) -> None:
        self.clicks.append(selector)
        if selector == "#loginBtn":
            self.index += 1
            if self.index >= self.login_after:
                self._logged = True

    async def count(self, selector: str) -> int:
        return 1 if selector in ("#captcha img", "#vcode") else 0

    async def text(self, selector: str = "body") -> str:
        return "课程 收件箱" if self._logged else "用户登录 密码 验证码"

    async def screenshot_bytes(self, selector: str = "", **_k: object) -> bytes:
        return b"\x89PNG\r\n\x1a\nfake"

    async def eval(self, js: str) -> bool:
        return False


SPEC = LoginSpec(
    platform="test",
    url="https://example.test/login",
    username_selector="#user",
    password_selector="#pass",
    submit_selector="#loginBtn",
    captcha_image_selectors=("#captcha img",),
    captcha_input_selectors=("#vcode",),
    success_markers=("课程", "收件箱"),
)


def run(coro):  # noqa: ANN001, ANN201 - 测试小工具
    return asyncio.run(coro)


class AutoFirstTest(unittest.TestCase):
    def test_auto_ocr_success_skips_manual(self) -> None:
        page = FakePage(login_after=2)  # 第一次点登录弹码，第二次（带码）成功
        seen: list[bytes] = []

        async def ocr(image: bytes, _env: dict) -> str:
            seen.append(image)
            return "1234"

        login = InteractiveLogin(
            SPEC, username="u", password="p", page=page, ocr=ocr, settle_timeout=0.05
        )
        state = run(login.start())

        self.assertEqual(state.state, "done")
        self.assertEqual(len(seen), 1, "只该识别一次")
        self.assertIn(("#vcode", "1234"), page.fills, "识别的码要填进验证码框")

    def test_ocr_missing_falls_back_to_manual(self) -> None:
        page = FakePage(login_after=99)  # 永远登不进去

        async def ocr(_image: bytes, _env: dict) -> str:
            return ""

        login = InteractiveLogin(
            SPEC, username="u", password="p", page=page, ocr=ocr, settle_timeout=0.05
        )
        state = run(login.start())

        self.assertEqual(state.state, "manual_captcha")
        self.assertIn("人工", state.message)
        self.assertTrue(state.screenshot, "要把验证码图交给控制台")

    def test_wrong_auto_codes_stop_after_retries(self) -> None:
        page = FakePage(login_after=99)
        calls = {"n": 0}

        async def ocr(_image: bytes, _env: dict) -> str:
            calls["n"] += 1
            return "9999"  # 永远不对

        login = InteractiveLogin(
            SPEC, username="u", password="p", page=page, ocr=ocr, settle_timeout=0.05
        )
        state = run(login.start())

        self.assertEqual(state.state, "manual_captcha")
        self.assertLessEqual(calls["n"], AUTO_RETRIES + 1, "自动重试必须有上限")

    def test_manual_code_success(self) -> None:
        page = FakePage(login_after=2, codes=["1234"])

        async def ocr(_image: bytes, _env: dict) -> str:
            return ""  # 直接转人工

        login = InteractiveLogin(
            SPEC, username="u", password="p", page=page, ocr=ocr, settle_timeout=0.05
        )
        state = run(login.start())
        self.assertEqual(state.state, "manual_captcha")

        state = run(login.submit_captcha("5678"))
        self.assertEqual(state.state, "done")

    def test_manual_wrong_code_returns_new_image(self) -> None:
        page = FakePage(login_after=99)

        async def ocr(_image: bytes, _env: dict) -> str:
            return ""

        login = InteractiveLogin(
            SPEC, username="u", password="p", page=page, ocr=ocr, settle_timeout=0.05
        )
        run(login.start())
        state = run(login.submit_captcha("0000"))

        self.assertEqual(state.state, "manual_captcha")
        self.assertIn("验证码不对", state.message)
        self.assertTrue(state.screenshot)

    def test_empty_code_is_rejected_without_http(self) -> None:
        page = FakePage()
        login = InteractiveLogin(SPEC, username="u", password="p", page=page, ocr=None,
                                 settle_timeout=0.05)
        with self.assertRaises(LoginSessionError):
            run(login.submit_captcha("  "))
        self.assertEqual(page.clicks, [], "空码不该触发任何点击")


class SpecCoverageTest(unittest.TestCase):
    def test_only_verified_specs_are_shipped(self) -> None:
        """只允许出现「已核实登录页」的平台；新增平台必须先补 DOM 证据。"""
        from kedaibiao.browser.login_session import SPECS

        self.assertIn("chaoxing", SPECS)
        for platform, spec in SPECS.items():
            with self.subTest(platform=platform):
                self.assertTrue(spec.captcha_input_selectors, "必须写明验证码输入框候选")
                self.assertTrue(spec.url.startswith("https://"))


if __name__ == "__main__":
    unittest.main()


class AutoSliderPlatformsTest(unittest.TestCase):
    """滑块类平台复用已有的 geetest 求解器，不需要人工看图。"""

    def test_only_unipus_and_itest_are_slider_platforms(self) -> None:
        from kedaibiao.api.service import ConsoleService

        self.assertEqual(
            set(ConsoleService.AUTO_SLIDER_PLATFORMS), {"unipus", "itest"},
            "只有这两个平台的登录会弹极验，且已有自动求解",
        )

    def test_platforms_endpoint_lists_both_mechanisms(self) -> None:
        import tempfile
        from pathlib import Path

        from kedaibiao.api.service import ConsoleService

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "config").mkdir()
        (root / "config" / "local.toml").write_text(
            f'[kedaibiao]\ndb_url = "sqlite:///{root}/x.db"\n'
            f'session_dir = "{root}/s"\nnotify = "null"\n',
            encoding="utf-8",
        )
        service = ConsoleService(root=root, environ={})
        items = {i["platform"]: i["kind"] for i in service.auto_captcha_platforms()}
        self.assertEqual(items.get("unipus"), "slider")
        self.assertEqual(items.get("itest"), "slider")
        self.assertEqual(items.get("chaoxing"), "image")

    def test_auto_login_rejects_non_slider_platform(self) -> None:
        import tempfile
        from pathlib import Path

        from kedaibiao.api.service import ConsoleError, ConsoleService, Viewer

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "config").mkdir()
        (root / "config" / "local.toml").write_text(
            f'[kedaibiao]\ndb_url = "sqlite:///{root}/x.db"\n'
            f'session_dir = "{root}/s"\nnotify = "null"\n',
            encoding="utf-8",
        )
        service = ConsoleService(root=root, environ={})
        super_viewer = Viewer(user_id=1, is_superadmin=True, username="admin")
        with self.assertRaises(ConsoleError) as ctx:
            service.start_auto_login(
                super_viewer, "neusoft", username="u", password="p"
            )
        self.assertEqual(ctx.exception.status, 501)
