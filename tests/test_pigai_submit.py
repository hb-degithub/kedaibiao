"""pigai 提交载荷与响应解析测试。

最重要的一条：**编码形态是「单次」百分号编码** —— 这是用真实会话探测出来的
（保存草稿 → 只读接口读回比对），推翻了静态分析推断的「二次编码」。

如果照推断实现二次编码，交上去的作文会变成 `%20` 字面量乱码，而且只有交完
才会被发现。所以这里把结论钉死。
"""

from __future__ import annotations

import unittest
import urllib.parse

from kedaibiao.platforms.pigai.submit import (
    SubmitPayloadError,
    build_submit_payload,
    encode_body,
    encode_field,
    parse_submit_response,
)


class TestEncoding(unittest.TestCase):
    """编码形态：单次是对的，二次是错的（实测）。"""

    def test_single_encode_roundtrips(self) -> None:
        text = "hello 世界 空格"
        encoded = encode_field(text, double=False)
        self.assertEqual(urllib.parse.unquote(encoded), text)
        self.assertNotIn("%25", encoded, "单次编码不该出现 %25")

    def test_double_encode_produces_literal_percent(self) -> None:
        """二次编码会把 % 变成 %25 —— 这正是实测里服务端存下的错误内容。"""
        text = "hello 世界"
        encoded = encode_field(text, double=True)
        self.assertIn("%25", encoded)
        self.assertNotEqual(urllib.parse.unquote(encoded), text)

    def test_default_is_single(self) -> None:
        """默认必须是单次：这是实测结论，不是偏好。"""
        plan = build_submit_payload(essay="a b", title="t", rid="1")
        self.assertFalse(plan.double_encoded)
        self.assertEqual(urllib.parse.unquote(plan.payload["utContent"]), "a b")

    def test_wire_form_is_percent_twice_but_semantically_single(self) -> None:
        """线上报文是 `%25E4…` 形态 —— 因为**服务端会解码两次**。

        实测结论：
        - 客户端做法：值 `quote` 一次，再交给 `urlencode`（传输层再转义一次）
          → 线上得到 `%25XX`；
        - 服务端解码两次 → 还原出原文；
        - 若客户端再预编码一次（`quote` 两次），解码两次后剩下的是 `%XX` 字面量
          —— 实测读回正是这种乱码。

        所以「单次编码」指的是**语义上一次**，不是线上字节只有一层 %。
        """
        plan = build_submit_payload(essay="中文 空格", title="t", rid="1")
        body = encode_body(plan.payload).decode()
        self.assertIn("%25E4%25B8%25AD", body.upper(), "线上应是 %25XX 形态")

        once = urllib.parse.parse_qs(body)["utContent"][0]      # 服务端第 1 次解码
        twice = urllib.parse.unquote(once)                       # 服务端第 2 次解码
        self.assertEqual(twice, "中文 空格", "两次解码后必须还原原文")


class TestPayload(unittest.TestCase):
    def test_required_fields(self) -> None:
        plan = build_submit_payload(essay="正文", title="标题", rid="123", eid="")
        for key in ("utContent", "utTitle", "rid", "eid", "type", "utype",
                    "gao", "uncheck", "tiku_id", "engine", "fromCode", "autoDel",
                    "bzold", "bz", "fileName", "filePath"):
            self.assertIn(key, plan.payload, key)
        self.assertEqual(plan.payload["type"], "0")
        self.assertEqual(plan.payload["gao"], "1")
        self.assertEqual(plan.payload["eid"], "", "首交时 eid 是空串")

    def test_no_extra_fields(self) -> None:
        """严禁脑补：字段集之外不许出现 token/sign/timestamp 之类。"""
        plan = build_submit_payload(essay="正文", title="t", rid="1")
        for forbidden in ("token", "sign", "timestamp", "score", "class_id"):
            self.assertNotIn(forbidden, plan.payload)

    def test_stu_class_optional(self) -> None:
        without = build_submit_payload(essay="x", title="t", rid="1")
        self.assertNotIn("stu_class", without.payload)
        with_class = build_submit_payload(essay="x", title="t", rid="1", stu_class="1班")
        self.assertIn("stu_class", with_class.payload)

    def test_empty_essay_rejected(self) -> None:
        with self.assertRaises(SubmitPayloadError) as cm:
            build_submit_payload(essay="   ", title="t", rid="1")
        self.assertIn("正文", str(cm.exception))

    def test_missing_rid_rejected(self) -> None:
        with self.assertRaises(SubmitPayloadError):
            build_submit_payload(essay="x", title="t", rid="  ")

    def test_summary(self) -> None:
        plan = build_submit_payload(essay="x", title="t", rid="1000001")
        self.assertIn("1000001", plan.summary())
        self.assertIn("单次", plan.summary())


class TestResponse(unittest.TestCase):
    def test_success_eid(self) -> None:
        result = parse_submit_response("eid=1234567")
        self.assertTrue(result.ok)
        self.assertEqual(result.eid, "1234567")

    def test_success_with_whitespace(self) -> None:
        self.assertTrue(parse_submit_response("  eid=99  \n").ok)

    def test_business_failure_msg(self) -> None:
        result = parse_submit_response("msg:提交过于频繁")
        self.assertFalse(result.ok)
        self.assertEqual(result.message, "提交过于频繁")

    def test_unknown_text(self) -> None:
        result = parse_submit_response("系统繁忙")
        self.assertFalse(result.ok)
        self.assertIn("系统繁忙", result.message)

    def test_empty(self) -> None:
        self.assertFalse(parse_submit_response("").ok)

    def test_eid_without_value(self) -> None:
        self.assertFalse(parse_submit_response("eid=").ok)


if __name__ == "__main__":
    unittest.main()


class TestAdapterSubmit(unittest.IsolatedAsyncioTestCase):
    """适配器层 submit 的接线测试。

    回归背景：这两个 bug 让第一次真实提交**发出去了但结果构造失败**——
    ``client`` 是 property 却被当方法调（TypeError）、``SubmissionResult``
    漏导入（NameError）。只测纯函数构造器抓不到这类问题，必须跑一遍方法本体。
    """

    def _adapter(self):
        from kedaibiao.net.http import HttpClient
        from kedaibiao.platforms.base import AdapterContext
        from kedaibiao.platforms.pigai.adapter import PigaiAdapter

        calls: list[dict] = []

        class FakeTransport:
            async def request(self, method, url, *, headers=None, body=None, timeout=15.0):
                from kedaibiao.net.http import Response

                calls.append({"method": method, "url": url, "body": body})
                if "a=write" in url:
                    # 写作页：带页面变量
                    return Response(
                        status=200, url=url,
                        body=b"<script>_rid='1'; _eid=''; _fromCode='377778'; _engine='';</script>",
                    )
                if "a=postSave" in url:
                    return Response(status=200, url=url, body=b"eid=999888")
                return Response(status=200, url=url, body=b"{}")

        adapter = PigaiAdapter(AdapterContext(http=HttpClient(FakeTransport())))
        return adapter, calls

    async def test_submit_happy_path(self) -> None:
        from kedaibiao.core.models import Answer, Assignment

        adapter, calls = self._adapter()
        assignment = Assignment(remote_id="1000001", title="我的作文")
        result = await adapter.submit(
            assignment, [Answer(question=None, text="An essay about AI.", choices=[])]  # type: ignore[arg-type]
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.raw["eid"], "999888")

        posts = [c for c in calls if c["method"] == "POST"]
        self.assertEqual(len(posts), 1, "只应发一次提交请求")
        self.assertIn("a=postSave", posts[0]["url"])
        body = posts[0]["body"].decode()
        self.assertIn("utContent", body)
        self.assertIn("rid=1000001", body)
        # 正文按单次编码：解码两次能还原
        import urllib.parse
        once = urllib.parse.parse_qs(body)["utContent"][0]
        self.assertEqual(urllib.parse.unquote(once), "An essay about AI.")

    async def test_submit_requires_text(self) -> None:
        from kedaibiao.core.models import Assignment

        adapter, calls = self._adapter()
        with self.assertRaises(Exception):
            await adapter.submit(Assignment(remote_id="1", title="t"), [])
        self.assertEqual([c for c in calls if c["method"] == "POST"], [],
                         "没正文时不该发任何写请求")

    async def test_gate_closed_blocks_everything(self) -> None:
        from kedaibiao.core.models import Answer, Assignment
        from kedaibiao.platforms.base import NotSupportedError

        adapter, calls = self._adapter()
        adapter.submit_payload_confirmed = False
        with self.assertRaises(NotSupportedError):
            await adapter.submit(
                Assignment(remote_id="1", title="t"),
                [Answer(question=None, text="x")],  # type: ignore[arg-type]
            )
        self.assertEqual(calls, [], "闸门关闭时一个请求都不该发")


class TestGradeLoop(unittest.IsolatedAsyncioTestCase):
    """评分循环的判停逻辑。

    最关键的是把「达到目标」「轮次用尽」「平台不给分」三种情况分开 ——
    尤其**未截止时不能重试**，否则会平白多交几篇作文。
    """

    class _Snap:
        def __init__(self, score=None, graded=True, state="graded"):
            self.score, self.graded, self.state = score, graded, state

        def to_dict(self):
            return {
                "score": self.score, "full_marks": 100.0, "graded": self.graded,
                "score_pct": (self.score or 0) / 100, "raw_state": self.state,
                "dimensions": [{"name": "词汇", "ratio": 0.9}],
                "weak_dimensions": ["词汇"], "overall_comment": "ok",
                "feedback_lines": ["[w] 连词"], "essay_text": "x",
            }

    def _loop(self, scores, *, target=90, max_attempts=3):
        from kedaibiao.platforms.pigai.grade_loop import GradeLoop

        class Submit:
            ok = True
            message = "ok"
            raw = {"eid": "E"}

        class Adapter:
            def __init__(self):
                self.submits = 0

            async def submit(self, a, ans):
                self.submits += 1
                return Submit()

            async def list_pending_assignments(self):
                from kedaibiao.core.models import Assignment
                return [Assignment(remote_id="R", title="T")]

            async def list_finished_essays(self):
                return []

        adapter = Adapter()
        rewrites: list[int] = []

        async def rewrite(snap, essay, n):
            rewrites.append(n)
            return f"v{n}"

        loop = GradeLoop(adapter=adapter, http=object(), base="", target_score=target,
                         max_attempts=max_attempts, rewrite=rewrite)
        return loop, adapter, rewrites

    async def _run(self, loop, scores):
        import kedaibiao.platforms.pigai.grading as grading

        seq = iter(scores)

        async def fake_wait(*a, **k):
            return self._Snap(*next(seq))

        original = grading.wait_for_grade
        grading.wait_for_grade = fake_wait
        try:
            return await loop.run(rid="R", topic="T", initial_essay="v1")
        finally:
            grading.wait_for_grade = original

    async def test_reaches_target(self) -> None:
        loop, adapter, rewrites = self._loop(None)
        result = await self._run(loop, [(80, True), (92, True)])
        self.assertEqual(result.stop_reason, "reached")
        self.assertEqual(result.best_score, 92)
        self.assertEqual(adapter.submits, 2)
        self.assertEqual(rewrites, [2], "只在未达标时改写一次")

    async def test_already_above_target_stops_immediately(self) -> None:
        loop, adapter, rewrites = self._loop(None)
        result = await self._run(loop, [(95, True)])
        self.assertEqual(result.stop_reason, "reached")
        self.assertEqual(adapter.submits, 1)
        self.assertEqual(rewrites, [], "第一轮就达标，不该改写")

    async def test_exhausted(self) -> None:
        loop, adapter, _ = self._loop(None, max_attempts=3)
        result = await self._run(loop, [(70, True), (75, True), (80, True)])
        self.assertEqual(result.stop_reason, "exhausted")
        self.assertEqual(adapter.submits, 3)
        self.assertEqual(result.best_score, 80)

    async def test_unavailable_does_not_retry(self) -> None:
        """未截止 = 平台不给分 —— 必须停，绝不能继续交。"""
        loop, adapter, rewrites = self._loop(None)
        result = await self._run(loop, [(None, False, "未截止")])
        self.assertEqual(result.stop_reason, "unavailable")
        self.assertEqual(adapter.submits, 1, "只交一次，不重试")
        self.assertEqual(rewrites, [])
        self.assertIn("未截止", result.message)

    async def test_submit_failure_stops(self) -> None:
        from kedaibiao.platforms.pigai.grade_loop import GradeLoop

        class BadSubmit:
            ok = False
            message = "提交被拒"
            raw = {}

        class Adapter:
            async def submit(self, a, ans):
                return BadSubmit()

            async def list_pending_assignments(self):
                from kedaibiao.core.models import Assignment
                return [Assignment(remote_id="R", title="T")]

            async def list_finished_essays(self):
                return []

        loop = GradeLoop(adapter=Adapter(), http=object(), base="", target_score=90,
                         max_attempts=3)
        result = await loop.run(rid="R", topic="T", initial_essay="v1")
        self.assertEqual(result.stop_reason, "error")
        self.assertIn("提交被拒", result.message)
