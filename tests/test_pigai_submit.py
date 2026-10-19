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
