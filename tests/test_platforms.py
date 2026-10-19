"""适配器契约测试：能力门禁、生命周期、会话续期。"""

from __future__ import annotations

import unittest

from kedaibiao.core.models import (
    Answer,
    Assignment,
    Course,
    Credentials,
    Question,
    SubmissionResult,
)
from kedaibiao.platforms.base import (
    AdapterContext,
    Capabilities,
    NotSupportedError,
    PlatformAdapter,
)

CREDS = Credentials(username="u", password="p")


class MinimalAdapter(PlatformAdapter):
    """只实现登录，其余走基类默认行为。"""

    name = "minimal"
    capabilities = Capabilities(
        login=True,
        list_courses=True,
        list_assignments=False,
        fetch_questions=False,
        submit=False,
    )

    def __init__(self, ctx: AdapterContext | None = None, *, login_ok: bool = True) -> None:
        super().__init__(ctx)
        self._login_ok = login_ok
        self.closed = False
        self.logged_in = False

    async def login(self, credentials: Credentials) -> bool:
        self.logged_in = self._login_ok
        return self._login_ok

    async def is_logged_in(self) -> bool:
        return self.logged_in

    async def close(self) -> None:
        self.closed = True


class TestCapabilityGating(unittest.IsolatedAsyncioTestCase):
    async def test_unsupported_raises(self) -> None:
        a = MinimalAdapter()
        with self.assertRaises(NotSupportedError):
            await a.list_assignments(Course(remote_id="c", name="n"))
        with self.assertRaises(NotSupportedError):
            await a.fetch_questions(Assignment(remote_id="a", title="t"))
        with self.assertRaises(NotSupportedError):
            await a.submit(Assignment(remote_id="a", title="t"), [])

    async def test_supported_defaults_return_empty(self) -> None:
        a = MinimalAdapter()
        self.assertEqual(await a.list_courses(), [])

    async def test_not_supported_is_not_implemented_error(self) -> None:
        self.assertTrue(issubclass(NotSupportedError, NotImplementedError))

    async def test_submit_not_implemented_message_names_platform(self) -> None:
        a = MinimalAdapter()
        with self.assertRaises(NotSupportedError) as cm:
            await a.submit(Assignment(remote_id="a", title="t"), [])
        self.assertIn("minimal", str(cm.exception))


class TestLifecycle(unittest.IsolatedAsyncioTestCase):
    async def test_async_context_manager_closes(self) -> None:
        a = MinimalAdapter()
        async with a as handle:
            self.assertIs(handle, a)
        self.assertTrue(a.closed)

    async def test_refresh_session_skips_login_when_valid(self) -> None:
        a = MinimalAdapter()
        a.logged_in = True
        self.assertTrue(await a.refresh_session(CREDS))

    async def test_refresh_session_relogins_when_stale(self) -> None:
        a = MinimalAdapter()
        self.assertFalse(a.logged_in)
        self.assertTrue(await a.refresh_session(CREDS))
        self.assertTrue(a.logged_in)

    async def test_refresh_session_reports_failure(self) -> None:
        a = MinimalAdapter(login_ok=False)
        self.assertFalse(await a.refresh_session(CREDS))


class TestAdapterContext(unittest.TestCase):
    def test_defaults(self) -> None:
        ctx = AdapterContext()
        self.assertIsNone(ctx.browser)
        self.assertIsNone(ctx.bank)
        self.assertEqual(ctx.bus._handlers, {})  # noqa: SLF001

    def test_logger_is_child_of_root(self) -> None:
        a = MinimalAdapter(AdapterContext())
        self.assertEqual(a.log.name, "kedaibiao.minimal")

    def test_name_defaults_empty(self) -> None:
        self.assertEqual(PlatformAdapter.name, "")

    def test_capabilities_are_frozen(self) -> None:
        caps = Capabilities()
        with self.assertRaises(Exception):
            caps.login = False  # type: ignore[misc]


class TestAbstractness(unittest.TestCase):
    def test_cannot_instantiate_without_login(self) -> None:
        class Incomplete(PlatformAdapter):
            name = "x"

        with self.assertRaises(TypeError):
            Incomplete()  # type: ignore[abstract]


if __name__ == "__main__":
    unittest.main()
