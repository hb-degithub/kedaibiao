"""iTEST 进考场链路测试（离线：用假浏览器，不打真实平台）。

真实链路（登录态 → 设备检测 → 开始考试 → 填表 → 交卷）必须靠浏览器，没法在
单测里跑；这里锁的是**编排逻辑**：步骤顺序、失败时的判定、答案解析与交卷门槛。

假浏览器只回放「页面会返回什么」，因此也不能证明平台行为 —— 平台行为以
``docs/research/itest.md`` 的实测记录为准。
"""

from __future__ import annotations

import http.cookiejar
import json
import unittest
from typing import Any
from unittest import mock

from kedaibiao.platforms.itest.exam import (
    ExamFlowError,
    ExamRunner,
    cookies_from_jar,
)


class FakeBrowser:
    """只回放 evaluate 结果的假浏览器。"""

    def __init__(self) -> None:
        self.expressions: list[str] = []
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.navigated: list[str] = []
        self.dialogs_seen: list[dict[str, Any]] = []
        # 页面状态
        self.location = "https://itestcloud.unipus.cn/itest-api/itest/s/answer/index"
        self.sections = 0
        self.html = ""
        self.body = ""
        self.answers: dict[str, str] = {}
        self.fill_result: dict[str, Any] = {"blank": 0, "radio": 0, "textarea": 0, "missing": []}
        self.verify_result: dict[str, Any] = {"bad": [], "total": 0}
        self.judge_payload: dict[str, Any] = {
            "code": 1, "data": {"url": "https://itestcloud.unipus.cn/itest-api/itest/s/answer/index?token=t"}
        }
        self.click_hits: list[str] = []
        self.switch_ok = False
        self.injected = 0
        self.body = "26-27-1 B2U5（ID：1000106858） 进入考试"

    # -- Browser 接口 -----------------------------------------------------

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append((method, params or {}))
        if method == "Network.setCookie":
            self.injected += 1
            return {"success": True}
        return {}

    def evaluate(self, expression: str, **_: Any) -> Any:
        self.expressions.append(expression)
        if "location.href" in expression:
            return self.location
        if "itest-section').length" in expression:
            return self.sections
        if "documentElement.outerHTML" in expression:
            return self.html
        if "collect('input.blankinput'" in expression:
            return json.dumps(self.answers, ensure_ascii=False)
        if "out = {blank" in expression:
            return json.dumps(self.fill_result)
        if "bad = []" in expression:
            return json.dumps(self.verify_result)
        if "judgeEntry" in expression:
            return json.dumps(self.judge_payload, ensure_ascii=False)
        if "getBoundingClientRect" in expression:
            for hint in self.click_hits:
                if hint in expression:
                    return json.dumps({"x": 10, "y": 20, "t": hint})
            return None
        if "document.body.innerText" in expression:
            return self.body
        return True

    def navigate(self, url: str) -> None:
        self.navigated.append(url)

    def capture_response(
        self, url_pattern: str, *, trigger_script: str = "",
        timeout: float = 30.0, settle: float = 3.0,
    ) -> dict[str, str]:
        """模拟「点进入考试 → 平台自己发 judgeEntry」。"""
        return {"url": "https://x/utest/itest/s/exam/judgeEntry",
                "body": json.dumps(self.judge_payload, ensure_ascii=False)}

    def cookies(self) -> list[Any]:
        from types import SimpleNamespace

        return [SimpleNamespace(name="p_user"), SimpleNamespace(name="p_examtype")]

    def switch_to_target(
        self, url_contains: str, *, timeout: float = 30.0, newest: bool = False
    ) -> bool:
        return self.switch_ok

    def enable_network(self) -> None:
        pass

    def mouse(self, *args: Any, **kwargs: Any) -> None:
        pass

    def screenshot(self) -> bytes:
        return b"\x89PNG-fake"

    def close(self) -> None:
        pass


class BaseCase(unittest.TestCase):
    """把 sleep 打掉：这些用例只验编排，不该真的等 60 秒。"""

    def setUp(self) -> None:
        patches = [
            mock.patch("kedaibiao.platforms.itest.exam.time.sleep", lambda *_: None),
            # 等列表渲染是真实时间轮询，单测里没意义，直接放行
            mock.patch.object(ExamRunner, "_wait_plaza_ready", lambda *a, **k: True),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)


def make_runner(browser: FakeBrowser) -> ExamRunner:
    return ExamRunner(
        [{"name": "jwt", "value": "v", "domain": "itestcloud.unipus.cn", "path": "/"}],
        the_browser=browser,  # type: ignore[arg-type]
    )


class TestCookies(BaseCase):
    def test_cookies_from_jar_keeps_only_needed_fields(self) -> None:
        jar = http.cookiejar.CookieJar()
        jar.set_cookie(http.cookiejar.Cookie(
            version=0, name="jwt", value="abc", port=None, port_specified=False,
            domain=".unipus.cn", domain_specified=True, domain_initial_dot=True,
            path="/", path_specified=True, secure=True, expires=None, discard=False,
            comment=None, comment_url=None, rest={},
        ))
        cookies = cookies_from_jar(jar)
        self.assertEqual(len(cookies), 1)
        self.assertEqual(cookies[0]["name"], "jwt")
        self.assertEqual(cookies[0]["value"], "abc")
        self.assertEqual(cookies[0]["domain"], ".unipus.cn")
        self.assertTrue(cookies[0]["secure"])

    def test_cookies_from_empty_jar(self) -> None:
        self.assertEqual(cookies_from_jar(http.cookiejar.CookieJar()), [])


class TestLogin(BaseCase):
    def test_injects_every_cookie(self) -> None:
        browser = FakeBrowser()
        runner = make_runner(browser)
        injected = runner.login_with_cookies()
        self.assertEqual(injected, 1)
        method, params = browser.calls[0]
        self.assertEqual(method, "Network.setCookie")
        self.assertEqual(params["name"], "jwt")

    def test_no_cookies_is_a_clear_error(self) -> None:
        runner = ExamRunner([], the_browser=FakeBrowser())  # type: ignore[arg-type]
        with self.assertRaises(ExamFlowError) as ctx:
            runner.login_with_cookies()
        self.assertEqual(ctx.exception.stage, "login")


class TestEnter(BaseCase):
    def test_enter_success(self) -> None:
        browser = FakeBrowser()
        browser.sections = 11
        browser.click_hits = ["开始考试"]
        runner = make_runner(browser)
        result = runner.enter("1099342202", ready_timeout=0.05)
        self.assertTrue(result.ok, result.message)
        names = [s.name for s in result.steps]
        self.assertTrue(any("换令牌" in name for name in names), names)
        self.assertIn("切到答题页", names)
        # judgeEntry 的 url 必须补 returnUrl（缺了平台报参数缺失）
        answer_nav = [u for u in browser.navigated if "/answer/" in u][0]
        self.assertIn("returnUrl=", answer_nav)

    def test_enter_fails_when_answer_tab_never_appears(self) -> None:
        browser = FakeBrowser()
        result = make_runner(browser).enter("1099342202", ready_timeout=0.05)
        self.assertFalse(result.ok)
        self.assertIn("没切到答题页", result.message)

    def test_enter_fails_when_paper_never_renders(self) -> None:
        browser = FakeBrowser()
        browser.switch_ok = True  # 切到页了，但 .itest-section 一直是 0
        result = make_runner(browser).enter("1099342202", ready_timeout=0.05)
        self.assertFalse(result.ok)
        self.assertIn("没有渲染出", result.message)

    def test_judge_entry_error_surfaces_code(self) -> None:
        browser = FakeBrowser()
        browser.body = ""  # 列表没卡片 → 点不到，只能兜底裸调 judgeEntry
        browser.judge_payload = {"code": 500, "msg": "考试不存在"}
        runner = make_runner(browser)
        with self.assertRaises(ExamFlowError) as ctx:
            runner.enter("bad-id", ready_timeout=0.05)
        self.assertEqual(ctx.exception.stage, "entry")
        self.assertIn("500", str(ctx.exception))

    def test_judge_entry_non_json(self) -> None:
        browser = FakeBrowser()
        browser.judge_payload = None  # type: ignore[assignment]
        browser.evaluate = lambda expr, **_: (  # type: ignore[method-assign]
            "<html>登录页</html>" if "judgeEntry" in expr else False
        )
        runner = make_runner(browser)
        with self.assertRaises(ExamFlowError):
            runner.enter("x", ready_timeout=0.05)


class TestPaperAndAnswers(BaseCase):
    def test_read_paper_without_sections_is_an_error(self) -> None:
        runner = make_runner(FakeBrowser())
        with self.assertRaises(ExamFlowError) as ctx:
            runner.read_paper()
        self.assertEqual(ctx.exception.stage, "paper")

    def test_read_answers_parses_json(self) -> None:
        browser = FakeBrowser()
        browser.answers = {"1001": "A B", "2002": "C"}
        self.assertEqual(make_runner(browser).read_answers(), {"1001": "A B", "2002": "C"})

    def test_read_answers_tolerates_garbage(self) -> None:
        browser = FakeBrowser()
        browser.evaluate = lambda expr, **_: "not-json"  # type: ignore[method-assign]
        self.assertEqual(make_runner(browser).read_answers(), {})

    def test_fill_rejects_empty_answers(self) -> None:
        with self.assertRaises(ExamFlowError) as ctx:
            make_runner(FakeBrowser()).fill({})
        self.assertEqual(ctx.exception.stage, "fill")

    def test_fill_and_verify_return_counts(self) -> None:
        browser = FakeBrowser()
        browser.fill_result = {"blank": 80, "radio": 9, "textarea": 15, "missing": []}
        browser.verify_result = {"bad": [], "total": 0}
        runner = make_runner(browser)
        self.assertEqual(runner.fill({"1001": "A"}).get("blank"), 80)
        self.assertEqual(runner.verify({"1001": "A"})["total"], 0)


class TestSubmit(BaseCase):
    def test_submit_finds_button_and_reports_success(self) -> None:
        browser = FakeBrowser()
        browser.click_hits = ["交卷"]
        browser.body = "交卷成功，本次得分 92"
        result = make_runner(browser).submit(timeout=1)
        self.assertTrue(result["ok"])
        self.assertIn("交卷成功", result["text"])

    def test_submit_without_button_is_an_error(self) -> None:
        with self.assertRaises(ExamFlowError) as ctx:
            make_runner(FakeBrowser()).submit(timeout=1)
        self.assertEqual(ctx.exception.stage, "submit")

    def test_submit_without_success_evidence_reports_false(self) -> None:
        browser = FakeBrowser()
        browser.click_hits = ["交卷"]
        browser.body = "试卷已保存"
        result = make_runner(browser).submit(timeout=0.1)
        self.assertFalse(result["ok"])


if __name__ == "__main__":
    unittest.main()


# --------------------------------------------------------------------------
# 动作层：参数校验、交卷门槛、取卷信封
# --------------------------------------------------------------------------


class FakeAdapter:
    """动作层只用到 name / ctx.settings / client().http.cookies。"""

    name = "itest"

    def __init__(self) -> None:
        from types import SimpleNamespace

        jar = http.cookiejar.CookieJar()
        jar.set_cookie(http.cookiejar.Cookie(
            version=0, name="jwt", value="v", port=None, port_specified=False,
            domain="itestcloud.unipus.cn", domain_specified=True,
            domain_initial_dot=False, path="/", path_specified=True,
            secure=True, expires=None, discard=False, comment=None,
            comment_url=None, rest={},
        ))
        self.ctx = SimpleNamespace(settings=SimpleNamespace(session_dir="./data/sessions"))
        self._client = SimpleNamespace(http=SimpleNamespace(cookies=jar))

    def client(self) -> Any:
        return self._client

    async def exam_meta(self, exam_id: str) -> Any:
        from types import SimpleNamespace

        return SimpleNamespace(title="26-27-1 B2U5", raw={"ksId": "1000106858"})


class TestActionInputs(BaseCase):
    def test_answers_json_must_be_object(self) -> None:
        from kedaibiao.api.actions import ActionInputError, _itest_parse_answers

        with self.assertRaises(ActionInputError):
            _itest_parse_answers("[1,2]")
        with self.assertRaises(ActionInputError):
            _itest_parse_answers("not json")
        with self.assertRaises(ActionInputError):
            _itest_parse_answers("")

    def test_answers_values_become_strings(self) -> None:
        from kedaibiao.api.actions import _itest_parse_answers

        self.assertEqual(_itest_parse_answers('{"1": "A B", "2": "  x  "}'),
                         {"1": "A B", "2": "x"})

    def test_nested_answer_value_is_rejected(self) -> None:
        from kedaibiao.api.actions import ActionInputError, _itest_parse_answers

        with self.assertRaises(ActionInputError):
            _itest_parse_answers('{"1": {"a": 1}}')


class TestSubmitGuard(BaseCase):
    def test_requires_confirm_word(self) -> None:
        import asyncio

        from kedaibiao.api.actions import ActionInputError, _itest_submit

        with self.assertRaises(ActionInputError) as ctx:
            asyncio.run(_itest_submit(FakeAdapter(), {"exam_id": "1", "answers": '{"1": "A"}',
                                                      "confirm": "确定"}))
        self.assertIn("不可逆", str(ctx.exception))

    def test_refuses_to_submit_when_verification_failed(self) -> None:
        import asyncio

        from kedaibiao.api import actions

        fake = {
            "entered": None, "paper": None, "current": {},
            "applied": {}, "filled": {}, "checked": {"bad": ["1#x"], "total": 1},
            "shot": "", "submitted": {"ok": True, "steps": [], "text": ""},
        }
        with (
            mock.patch.object(actions, "_itest_fill_blocking", lambda *a, **k: fake),
            self.assertRaises(actions.ActionInputError) as ctx,
        ):
            asyncio.run(actions._itest_submit(
                FakeAdapter(),
                {"exam_id": "1", "answers": '{"1": "A"}', "confirm": "交卷"},
            ))
        self.assertIn("拒绝交卷", str(ctx.exception))


class TestPaperEnvelope(BaseCase):
    def test_paper_envelope_has_rows_and_timer_warning(self) -> None:
        import asyncio

        from kedaibiao.api import actions
        from kedaibiao.platforms.itest.paper import parse_paper
        from tests.test_itest_paper import PAPER

        paper = parse_paper(PAPER)
        fake = {
            "entered": type("E", (), {"steps": [], "to_dict": lambda self: {}})(),
            "paper": paper, "current": {paper.sections[0].questions[0].qid: "F I"},
            "shot": "data/itest/paper-1.png",
        }
        with mock.patch.object(actions, "_itest_paper_blocking", lambda *a, **k: fake):
            out = asyncio.run(actions._itest_paper(FakeAdapter(), {"exam_id": "1"}))
        self.assertEqual(out["kind"], "table")
        self.assertEqual(len(out["rows"]), paper.question_count)
        self.assertIn("计时", out["note"])
        row = out["rows"][0]
        self.assertEqual(set(row), {"section", "qid", "kind", "number", "text", "slots", "filled"})
