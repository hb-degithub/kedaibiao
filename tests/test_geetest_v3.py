"""极验 v3 求解器的离线测试。

真实链路已在极验**官方 demo** 上验证通过（2026-10-19）：
    register → get.php → 还原背景 → 缺口 → 轨迹 → w → ajax.php
    → {"success": 1, "validate": "...", "score": "1"}

这里锁的是**纯函数与结构**，不发网络请求，也不碰任何真实账号。
"""

from __future__ import annotations

import json
import unittest

from kedaibiao.platforms.unipus.geetest import v3


class TestUnscrambleTable(unittest.TestCase):
    """v3 的指纹：52 项置换表。表错了缺口就算错。"""

    def test_table_is_a_permutation_of_0_to_51(self) -> None:
        table = v3.UNSCRAMBLE_TABLE
        self.assertEqual(len(table), 52)
        self.assertEqual(sorted(table), list(range(52)), "必须恰好是 0..51 的一个排列")

    def test_canvas_math(self) -> None:
        # 26 列 × 10px = 260；2 行 × 80px = 160
        self.assertEqual(26 * v3.BLOCK_W, v3.CANVAS_W)
        self.assertEqual(2 * v3.BLOCK_H, v3.CANVAS_H)


class TestTrace(unittest.TestCase):
    def test_monotonic_and_ends_at_distance(self) -> None:
        trace = v3.build_trace(120)
        self.assertGreater(len(trace), 20)
        xs = [p[0] for p in trace[2:]]
        self.assertEqual(xs, sorted(xs), "横向位移必须单调不减")
        self.assertEqual(trace[-1][0], 120, "最后一点应到达目标距离")

    def test_timestamps_increase(self) -> None:
        trace = v3.build_trace(100)
        ts = [p[2] for p in trace[1:]]
        self.assertEqual(ts, sorted(ts))
        self.assertGreater(ts[-1], 0)

    def test_starts_with_out_of_canvas_jitter(self) -> None:
        """前两个点是负坐标抖动 + (0,0,0) —— 拟人起步。"""
        trace = v3.build_trace(100)
        self.assertEqual(trace[1], [0, 0, 0])

    def test_zero_distance_ok(self) -> None:
        self.assertTrue(v3.build_trace(0))

    def test_negative_rejected(self) -> None:
        with self.assertRaises(v3.GeetestError):
            v3.build_trace(-1)


class TestJsonp(unittest.TestCase):
    def test_callback_wrapped(self) -> None:
        data = v3._parse_jsonp('geetest_123({"success": 1, "validate": "abc"})')
        self.assertEqual(data["success"], 1)

    def test_bare_json(self) -> None:
        self.assertEqual(v3._parse_jsonp('{"success": 1}')["success"], 1)

    def test_garbage_raises(self) -> None:
        with self.assertRaises(v3.GeetestError):
            v3._parse_jsonp("not json at all")


class TestNodeDriver(unittest.TestCase):
    """w 的计算依赖 vendor 的 jiyan.js + Node。"""

    def test_node_present(self) -> None:
        if not v3.node_available():
            self.skipTest("环境里没有 node")
        self.assertTrue(v3.node_available())

    def test_driver_produces_w(self) -> None:
        if not v3.node_available():
            self.skipTest("环境里没有 node")
        w = v3.compute_w(
            slide_length=120,
            trace=[[0, 0, 0], [10, 0, 100], [60, 0, 300], [120, 0, 600]],
            s="753f5043",
            challenge="1dace4f4f37d25ab97fe40f0770845ae8e92f08d381eefbd88349da40b071bb9",
            passtime=600, imgload=150, gt="a7cee00a24b6749063b7029cef6fa76f",
        )
        self.assertIsInstance(w, str)
        self.assertGreater(len(w), 100, "w 是 AES 密文拼 RSA 密文，不会很短")

    def test_driver_rejects_unknown_op(self) -> None:
        if not v3.node_available():
            self.skipTest("环境里没有 node")
        with self.assertRaises(v3.GeetestError):
            v3.call_driver({"op": "nope"})


class TestVendorIntegrity(unittest.TestCase):
    """vendor 的 jiyan.js 必须完整 —— 缺函数会静默算错。"""

    def test_driver_and_js_present(self) -> None:
        self.assertTrue(v3.DRIVER.is_file())
        self.assertTrue((v3.VENDOR_DIR / "jiyan.js").is_file())

    def test_notice_carries_attribution(self) -> None:
        """MIT 要求保留版权声明。"""
        notice = (v3.VENDOR_DIR / "NOTICE").read_text(encoding="utf-8")
        self.assertIn("sijiyo", notice)
        self.assertIn("MIT", notice)

    def test_license_text_present(self) -> None:
        text = (v3.VENDOR_DIR / "LICENSE.sijiyo-projects").read_text(encoding="utf-8")
        self.assertIn("MIT License", text)
        self.assertIn("sijiyo", text)

    def test_js_exposes_both_functions(self) -> None:
        js = (v3.VENDOR_DIR / "jiyan.js").read_text(encoding="utf-8")
        self.assertIn("function get_w(", js)
        self.assertIn("function get_encode_trace(", js)


if __name__ == "__main__":
    unittest.main()
