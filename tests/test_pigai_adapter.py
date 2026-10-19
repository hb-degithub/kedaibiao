"""批改网（pigai）只读适配器测试 —— **全离线**。

用 ``tests/test_net.py`` 的 ``FakeTransport`` 注入固定响应，
再喂 ``tests/fixtures/pigai/`` 的**脱敏**样本。**不访问任何真实站点。**
"""

from __future__ import annotations

import unittest
from datetime import datetime
from pathlib import Path

from kedaibiao.core.models import Credentials
from kedaibiao.net.http import HttpClient, Response
from kedaibiao.platforms.base import AdapterContext, NotSupportedError
from kedaibiao.platforms.pigai import protocol
from kedaibiao.platforms.pigai.adapter import (
    SYNTHETIC_COURSE_ID,
    PigaiAdapter,
    SessionExpiredError,
    parse_deadline,
)
from kedaibiao.platforms.pigai.parse import HeaderMismatchError
from tests.test_net import FakeTransport

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "pigai"
ADAPTER_SRC = Path(__file__).resolve().parent.parent / "src/kedaibiao/platforms/pigai/adapter.py"

CREDS = Credentials(username="stu", password="pw")


def _body(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _fixture(*names: str, status: int = 200) -> list[Response]:
    return [
        Response(status=status, url=protocol.BASE_URL + "/", body=_body(n)) for n in names
    ]


def _adapter(*responses: Response) -> PigaiAdapter:
    ctx = AdapterContext(http=HttpClient(FakeTransport(*responses)))
    return PigaiAdapter(ctx)


class TestCapabilities(unittest.TestCase):
    def test_identity(self) -> None:
        self.assertEqual(PigaiAdapter.name, "pigai")
        self.assertEqual(PigaiAdapter.display_name, "批改网")

    def test_capabilities(self) -> None:
        """提交已可用（编码形态经真实探测确认）；题面仍不可得。"""
        caps = PigaiAdapter.capabilities
        self.assertTrue(caps.submit, "提交已实现：编码已由真实数据确认")
        self.assertFalse(caps.fetch_questions, "作文题面不通过接口取")
        self.assertTrue(caps.login)
        self.assertTrue(caps.list_courses)
        self.assertTrue(caps.list_assignments)

    def test_submit_requires_essay_text(self) -> None:
        """作文提交要整篇正文；空的或只有 choices 的答案必须拒绝。"""
        import asyncio

        from kedaibiao.core.models import Assignment, Answer

        adapter = _adapter()
        assignment = Assignment(remote_id="1000001", title="t")
        with self.assertRaises(Exception) as cm:
            asyncio.run(adapter.submit(assignment, []))
        self.assertIn("正文", str(cm.exception))

        # 只有选项、没有 text 的答案也不行（选择题那套不适用）
        with self.assertRaises(Exception):
            asyncio.run(adapter.submit(assignment, [Answer(question=None, choices=[0])]))  # type: ignore[arg-type]

    def test_submit_gate_can_be_closed(self) -> None:
        """闸门关掉时必须以 NotSupportedError 拒绝 —— 保留紧急制动能力。"""
        import asyncio

        from kedaibiao.core.models import Assignment

        adapter = _adapter()
        adapter.submit_payload_confirmed = False
        with self.assertRaises(NotSupportedError):
            asyncio.run(adapter.submit(Assignment(remote_id="1", title="t"), []))

    def test_fetch_questions_raises_not_supported(self) -> None:
        import asyncio

        adapter = _adapter()
        with self.assertRaises(NotSupportedError):
            asyncio.run(adapter.fetch_questions(None))  # type: ignore[arg-type]


class TestDeadline(unittest.TestCase):
    def test_three_formats(self) -> None:
        self.assertEqual(parse_deadline("2026-11-16 23:59"), datetime(2026, 11, 16, 23, 59))
        self.assertEqual(
            parse_deadline("2026-11-16 23:59:30"), datetime(2026, 11, 16, 23, 59, 30)
        )
        self.assertEqual(parse_deadline("2026-10-19"), datetime(2026, 10, 19))

    def test_garbage_is_none_not_raise(self) -> None:
        for raw in ("", "   ", "未截止", "2026-13-45", None):
            self.assertIsNone(parse_deadline(raw), raw)


class TestPendingAssignments(unittest.IsolatedAsyncioTestCase):
    async def test_row_fixture(self) -> None:
        adapter = _adapter(*_fixture("assignments_pending_row.html"))
        items = await adapter.list_pending_assignments()
        self.assertEqual(len(items), 1)
        a = items[0]
        self.assertEqual(a.remote_id, "1000001", "remote_id 必须是 rid")
        self.assertFalse(a.finished)
        self.assertEqual(a.deadline, datetime(2026, 11, 16, 23, 59))
        self.assertEqual(a.kind.value, "homework")
        self.assertIn("raw_cells", a.raw)
        self.assertEqual(len(a.raw["raw_cells"]), 7)

    async def test_empty_page_returns_empty_list(self) -> None:
        adapter = _adapter(*_fixture("assignments_pending_empty.html"))
        self.assertEqual(await adapter.list_pending_assignments(), [])

    async def test_uses_the_right_endpoint(self) -> None:
        adapter = _adapter(*_fixture("assignments_pending_empty.html"))
        await adapter.list_pending_assignments()
        transport = adapter.ctx.http.transport
        self.assertEqual(len(transport.calls), 1)
        self.assertTrue(transport.calls[0]["url"].endswith(protocol.Endpoints.ASSIGNMENTS_PENDING))

    async def test_session_expired_raises_instead_of_empty(self) -> None:
        """integration-guide §6.2：登录页不能被静默吞成空列表。

        登录页样本**内联**构造，不往 ``tests/fixtures/pigai/``（parser-dev 的范围）里加文件。
        """
        login_page = (
            f'<html><body><form id="{protocol.LOGIN_FORM_MARKER}" '
            'action="/index.php?a=login"></form></body></html>'
        )
        response = Response(status=200, url=protocol.BASE_URL + "/", body=login_page.encode())
        adapter = _adapter(response)
        with self.assertRaises(SessionExpiredError):
            await adapter.list_pending_assignments()


class TestFinishedEssays(unittest.IsolatedAsyncioTestCase):
    async def test_rows(self) -> None:
        adapter = _adapter(*_fixture("essays_done.html"))
        rows = await adapter.list_finished_essays()
        self.assertGreaterEqual(len(rows), 1)
        self.assertTrue(all(r.rid for r in rows))
        self.assertTrue(all(r.eid for r in rows), "已提交作文必须带 eid")

    async def test_unpublished_score_is_none(self) -> None:
        adapter = _adapter(*_fixture("essays_done.html"))
        rows = await adapter.list_finished_essays()
        unpublished = [r for r in rows if r.score_raw and r.score is None]
        for row in unpublished:
            self.assertEqual(row.score_raw, protocol.UNPUBLISHED_SCORE_MARKER)


class TestMergedAssignments(unittest.IsolatedAsyncioTestCase):
    async def test_merge_pending_and_finished(self) -> None:
        adapter = _adapter(*_fixture("assignments_pending_row.html", "essays_done.html"))
        course = (await adapter.list_courses())[0]
        items = await adapter.list_assignments(course)
        rids = [a.remote_id for a in items]
        self.assertEqual(len(rids), len(set(rids)), "rid 不能重复")
        self.assertIn("1000001", rids)
        finished = [a for a in items if a.finished]
        self.assertTrue(finished, "已提交的作文应以 finished=True 出现")
        self.assertIsNotNone(finished[0].raw.get("eid"))
        self.assertIn("submit_count", finished[0].raw)

    async def test_course_name_is_filled(self) -> None:
        adapter = _adapter(*_fixture("assignments_pending_row.html", "essays_done.html"))
        course = (await adapter.list_courses())[0]
        items = await adapter.list_assignments(course)
        self.assertTrue(all(a.course_name == course.name for a in items))


class TestListCourses(unittest.IsolatedAsyncioTestCase):
    async def test_single_synthetic_course_and_no_request(self) -> None:
        adapter = _adapter()  # 不给任何响应：不该发请求
        courses = await adapter.list_courses()
        self.assertEqual(len(courses), 1)
        self.assertEqual(courses[0].remote_id, SYNTHETIC_COURSE_ID)
        self.assertEqual(adapter.ctx.http.transport.calls, [])


class TestEssayDetail(unittest.IsolatedAsyncioTestCase):
    async def test_detail(self) -> None:
        adapter = _adapter(*_fixture("essay_detail.html"))
        detail = await adapter.fetch_essay_detail("1000002", "200000001")
        self.assertEqual(detail.rid, "1000002")
        self.assertEqual(detail.eid, "200000001")
        self.assertIsNotNone(detail.score)
        self.assertEqual(len(detail.dimensions), 4)
        self.assertTrue(all(d.source == "title" for d in detail.dimensions))
        self.assertTrue(detail.overall_comment)
        self.assertTrue(detail.annotations)

    async def test_uses_essay_view_endpoint(self) -> None:
        adapter = _adapter(*_fixture("essay_detail.html"))
        await adapter.fetch_essay_detail("1000002", "200000001")
        url = adapter.ctx.http.transport.calls[0]["url"]
        self.assertTrue(url.endswith(protocol.Endpoints.essay_view("200000001", "1000002")))


class TestLookupByRid(unittest.IsolatedAsyncioTestCase):
    """用户需求「每次新作业都要作业号」的落点 —— 只读。"""

    async def test_fresh_assignment(self) -> None:
        adapter = _adapter(*_fixture("write_fresh.html"))
        page = await adapter.fetch_assignment_by_rid("1000001")
        self.assertEqual(page.rid, "1000001")
        self.assertEqual(page.eid, "", "未写过的作业 eid 应为空")
        self.assertFalse(page.has_draft)
        self.assertEqual(page.word_range, "120~150")
        self.assertTrue(page.deadline)

    async def test_existing_draft_is_readable(self) -> None:
        """「未截止可以继续修改」的只读一半：先看到改到哪了。"""
        adapter = _adapter(*_fixture("write_with_draft.html"))
        page = await adapter.fetch_assignment_by_rid("1000002")
        self.assertEqual(page.eid, "200000001")
        self.assertTrue(page.has_draft)
        self.assertTrue(page.essay_text.strip(), "应能读回已有稿子的正文")

    async def test_only_gets_the_lookup_endpoint(self) -> None:
        adapter = _adapter(*_fixture("write_fresh.html"))
        await adapter.fetch_assignment_by_rid("1000001")
        call = adapter.ctx.http.transport.calls[0]
        self.assertEqual(call["method"], "GET")
        self.assertTrue(call["url"].endswith(protocol.Endpoints.lookup_by_rid("1000001")))
        self.assertNotIn("a=write", call["url"], "不得使用写语义端点")

    async def test_session_expired_raises(self) -> None:
        page = f'<html><body><form id="{protocol.LOGIN_FORM_MARKER}"></form></body></html>'
        adapter = _adapter(
            Response(status=200, url=protocol.BASE_URL + "/", body=page.encode())
        )
        with self.assertRaises(SessionExpiredError):
            await adapter.fetch_assignment_by_rid("1000001")


class TestUrlRegression(unittest.TestCase):
    """回归：验证者「需知会-2」—— `urljoin(BASE_URL, "?c=…")` 会丢掉 `/index.php`。"""

    def test_relative_href_keeps_index_php(self) -> None:
        from kedaibiao.platforms.pigai.adapter import _absolute

        self.assertEqual(
            _absolute("?c=v2&a=write&rid=1000001"),
            protocol.BASE_URL + "/index.php?c=v2&a=write&rid=1000001",
        )
        self.assertTrue(
            _absolute("/index.php?c=v2&a=view&eid=1&rid=2").endswith("/index.php?c=v2&a=view&eid=1&rid=2")
        )
        self.assertEqual(_absolute(""), protocol.BASE_URL + "/")

    def test_rejects_offsite_and_non_http(self) -> None:
        """建议-14 + 需知会-7：站外 / 伪协议 / 锚点一律不采纳（这个值只用于展示）。"""
        from kedaibiao.platforms.pigai.adapter import _absolute

        for bogus in (
            "javascript:;",
            "javascript:void(0)",
            "#",
            "#anchor",
            "http://evil.example.com/x",
            "https://evil.example.com/x",
            "//evil.example.com/x",
            # 需知会-7：字符串前缀比较会放行这些"以 BASE_URL 开头"的假站
            "//www.pigai.org.evil.com/x",
            "https://www.pigai.org.evil.com/x",
            "http://www.pigai.org.evil.com/x",
            # 同 host 但 scheme 不同（本站只走 https）
            "http://www.pigai.org/index.php?c=write",
        ):
            self.assertEqual(_absolute(bogus), "", bogus)

    def test_accepts_same_origin(self) -> None:
        from kedaibiao.platforms.pigai.adapter import _absolute

        for good in (
            "?c=v2&a=write&rid=1",
            "/index.php?c=v2&a=write&rid=1",
            "https://www.pigai.org/index.php?c=write",
        ):
            self.assertTrue(_absolute(good).startswith(protocol.BASE_URL), good)

    def test_pending_assignment_url_is_usable(self) -> None:
        # 注意：本类继承普通 unittest.TestCase，**不能**写 async def ——
        # unittest 不会 await，测试会「假通过」（第一版就是这么错的）。
        import asyncio

        adapter = _adapter(*_fixture("assignments_pending_row.html"))
        items = asyncio.run(adapter.list_pending_assignments())
        url = items[0].url
        self.assertTrue(url.startswith(protocol.BASE_URL + "/index.php?"), url)
        self.assertIn("rid=1000001", url)

    def test_no_async_test_methods_in_plain_testcase(self) -> None:
        """守住上面那条教训：普通 TestCase 里不得出现 async def test_*。"""
        import ast

        def base_name(base: ast.expr) -> str:
            if isinstance(base, ast.Name):
                return base.id
            if isinstance(base, ast.Attribute):  # unittest.IsolatedAsyncioTestCase
                return base.attr
            return ""

        tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
        offenders: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            if "IsolatedAsyncioTestCase" in {base_name(b) for b in node.bases}:
                continue
            for item in node.body:
                if isinstance(item, ast.AsyncFunctionDef) and item.name.startswith("test_"):
                    offenders.append(f"{node.name}.{item.name}")
        self.assertEqual(offenders, [], f"这些测试不会被 await，等于没跑: {offenders}")


class TestSessionGuardStrictness(unittest.TestCase):
    """「合法页仅因文案提到 lg_from_w 就被判未登录」是验证者的建议-8。"""

    def test_prose_mention_does_not_trigger_session_expired(self) -> None:
        import asyncio

        page = (
            "<html><body><script>var hint = 'lg_from_w';</script>"
            "<div>这是一个正常页面，只是脚本文案里提到了那个标识</div></body></html>"
        )
        response = Response(status=200, url=protocol.BASE_URL + "/", body=page.encode())
        adapter = _adapter(response)
        # 不是登录页 → 不该抛 SessionExpiredError；
        # 但它确实不是作业列表，所以应当是「表头不符」。
        with self.assertRaises(HeaderMismatchError):
            asyncio.run(adapter.list_pending_assignments())


class TestSessionGuard(unittest.TestCase):
    """会话失效守卫：既要认登录页，也要认「请先登录」跳转页。

    后者是独立验证者的「需知会-5」：真实失效页**不含** `id="lg_from_w"`，
    只查登录表单标识会把「会话失效」误报成「页面结构变了」。
    """

    #: 按 data/recon/pigai/raw/b_anon_write.html 的实测形态构造（脱敏）
    ANON_PAGE = (
        "<html><head><title>句酷批改网</title></head><body>"
        "<div>使用此功能，请先登录　3 秒后系统将自动跳转</div>"
        "<script>var url='/index.php?a=loginForm&reurl=%2Findex.php%3Fc%3Dwrite';"
        "window.location.href=url;</script></body></html>"
    )
    LOGIN_PAGE_VARIANTS = (
        '<form id="lg_from_w"></form>',
        "<form id='lg_from_w'></form>",
        '<form id = "lg_from_w"></form>',
        '<FORM ID="lg_from_w"></FORM>',
    )

    def _guard(self, html: str) -> None:
        from kedaibiao.platforms.pigai.adapter import _guard_session

        _guard_session(html)

    def test_anon_page_raises(self) -> None:
        with self.assertRaises(SessionExpiredError):
            self._guard(self.ANON_PAGE)

    def test_login_page_writing_variants(self) -> None:
        """建议-12：容忍 `id = "…"` / 单引号 / 大写 ID。"""
        for variant in self.LOGIN_PAGE_VARIANTS:
            with self.assertRaises(SessionExpiredError, msg=variant):
                self._guard(variant)

    def test_prose_mention_is_not_login_page(self) -> None:
        """建议-8：脚本文案提到裸串不算登录页。"""
        self._guard("<script>var hint='lg_from_w';</script><p>正常页</p>")

class TestSessionGuardAsync(unittest.IsolatedAsyncioTestCase):
    """四条只读路径都必须把「会话失效页」判成 SessionExpiredError（需知会-5）。

    注意：**必须**是 ``IsolatedAsyncioTestCase`` —— 写成普通 ``TestCase`` 里的
    ``async def`` 不会被 await，测试会「假通过」（本文件有元测试专门钉这条）。
    """

    async def test_anon_page_raises_on_all_four_read_paths(self) -> None:
        body = TestSessionGuard.ANON_PAGE.encode()
        for name, call in (
            ("pending", lambda a: a.list_pending_assignments()),
            ("finished", lambda a: a.list_finished_essays()),
            ("detail", lambda a: a.fetch_essay_detail("1", "2")),
            ("lookup", lambda a: a.fetch_assignment_by_rid("1")),
        ):
            adapter = _adapter(
                Response(status=200, url=protocol.BASE_URL + "/", body=body)
            )
            with self.assertRaises(SessionExpiredError, msg=name):
                await call(adapter)


class TestNoWriteSurface(unittest.TestCase):
    """静态审计：整个 pigai 包的**可执行字符串**里不得出现写入类端点。

    口径（与独立验证者对齐）：只扫 AST 里的字符串**字面量**（含 f-string 的常量片段），
    **注释与 docstring 不算** —— 它们是说明文字，构不成可构造的请求。
    上一版用纯文本 grep 审 `adapter.py`，结果被自己 docstring 里的举例打脸；
    这一版同时把覆盖面从 2 个文件扩到**整个包**（验证者建议-9）。
    """

    FORBIDDEN = ("a=write", "a=delete", "a=submit", "a=post", "a=save")
    PACKAGE = Path(__file__).resolve().parent.parent / "src/kedaibiao/platforms/pigai"

    @staticmethod
    def _executable_strings(path: Path) -> list[str]:
        import ast

        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        docstring_nodes: set[int] = set()
        owners = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        for node in ast.walk(tree):
            body = getattr(node, "body", [])
            if not isinstance(node, owners) or not body:
                continue
            first = body[0]
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                docstring_nodes.add(id(first.value))
        return [
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstring_nodes
        ]

    def test_package_files_are_actually_covered(self) -> None:
        """防止目录写错导致「审计了 0 个文件」的假绿。"""
        files = sorted(p.name for p in self.PACKAGE.glob("*.py"))
        self.assertIn("adapter.py", files)
        self.assertIn("parse.py", files)
        self.assertIn("client.py", files)
        self.assertIn("protocol.py", files)

    #: 唯一允许的写端点：提交作文（编码已由真实探测确认）。
    ALLOWED_WRITE_TOKENS = ("a=postSave",)
    #: 这些**绝不允许**出现在可执行字符串里（草稿/心跳/删除/课堂自动保存等）。
    FORBIDDEN_WRITES = ("a=saveCaogao", "a=ketang", "a=htbt", "a=delete",
                        "a=submit", "a=del")

    def test_only_submit_is_a_write_surface(self) -> None:
        """写面必须**恰好**是「提交作文」这一个动作。

        原先是「一个写入端点都不许有」。提交功能落地后，这条守护改成白名单：
        仍然禁止草稿/心跳/删除/课堂自动保存等其它写端点混进来 ——
        每多一个写端点，就多一处不可逆操作的风险面。
        """
        offenders: list[tuple[str, str, str]] = []
        allowed: list[tuple[str, str]] = []
        for path in sorted(self.PACKAGE.glob("*.py")):
            for value in self._executable_strings(path):
                for token in self.FORBIDDEN_WRITES:
                    if token in value:
                        offenders.append((path.name, token, value[:70]))
                if any(token in value for token in self.ALLOWED_WRITE_TOKENS):
                    allowed.append((path.name, value[:70]))
        self.assertEqual(offenders, [], f"出现被禁止的写端点: {offenders}")
        self.assertTrue(allowed, "提交端点应当存在")
        self.assertTrue(
            all(name == "submit.py" for name, _ in allowed),
            f"写端点只应集中在 submit.py，实际: {allowed}",
        )

    def test_write_page_url_is_get_only(self) -> None:
        """`a=write` 是**写作页**的 GET 地址，不是写接口 —— 不要误判也不要用它发 POST。"""
        for path in sorted(self.PACKAGE.glob("*.py")):
            text = path.read_text(encoding="utf-8")
            if "a=write" in text:
                self.assertNotIn('"POST"', text.split("a=write")[0][-200:],
                                 f"{path.name}: a=write 附近出现 POST")


class TestDiscovery(unittest.TestCase):
    def test_adapter_is_discoverable(self) -> None:
        import inspect

        from kedaibiao import adapters
        from kedaibiao.platforms import load_builtin_adapters

        load_builtin_adapters()  # 容错：别的窗口的半成品不该让这条失败
        self.assertIn("pigai", adapters.names())
        self.assertIs(adapters.get("pigai"), PigaiAdapter)
        self.assertFalse(inspect.isabstract(PigaiAdapter))


if __name__ == "__main__":
    unittest.main()
