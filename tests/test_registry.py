"""注册表测试。"""

from __future__ import annotations

import inspect
import unittest
from unittest import mock

from kedaibiao.core.registry import AlreadyRegisteredError, NotFoundError, Registry


class _FakeEntryPoint:
    def __init__(self, name: str, obj: object | Exception) -> None:
        self.name = name
        self._obj = obj

    def load(self) -> object:
        if isinstance(self._obj, Exception):
            raise self._obj
        return self._obj


class TestRegistry(unittest.TestCase):
    def setUp(self) -> None:
        self.reg: Registry[str] = Registry("测试项")

    def test_register_and_get(self) -> None:
        self.reg.register("a", "A")
        self.assertEqual(self.reg.get("a"), "A")

    def test_get_missing_raises_with_hint(self) -> None:
        self.reg.register("a", "A")
        with self.assertRaises(NotFoundError) as cm:
            self.reg.get("zzz")
        self.assertIn("a", str(cm.exception))

    def test_duplicate_raises(self) -> None:
        self.reg.register("a", "A")
        with self.assertRaises(AlreadyRegisteredError):
            self.reg.register("a", "B")

    def test_override_allowed(self) -> None:
        self.reg.register("a", "A")
        self.reg.register("a", "B", override=True)
        self.assertEqual(self.reg.get("a"), "B")

    def test_empty_name_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.reg.register("", "A")

    def test_names_sorted_and_len(self) -> None:
        self.reg.register("b", "B")
        self.reg.register("a", "A")
        self.assertEqual(self.reg.names(), ["a", "b"])
        self.assertEqual(len(self.reg), 2)
        self.assertIn("a", self.reg)

    def test_unregister(self) -> None:
        self.reg.register("a", "A")
        self.reg.unregister("a")
        self.assertEqual(self.reg.names(), [])
        self.reg.unregister("不存在")  # 不应抛错

    def test_load_entry_points_skips_broken_plugin(self) -> None:
        eps = [_FakeEntryPoint("good", "OK"), _FakeEntryPoint("bad", RuntimeError("炸了"))]
        # 必须用 mock.patch.object：它从 __dict__ 取原值（拿到的是 staticmethod 对象），
        # 还原时也原样放回。直接 ``Registry._iter_entry_points = original`` 会拿
        # getattr 解析后的**普通函数**覆盖回去，把 staticmethod 永久降级成实例方法
        # —— 那会让此后所有 load_entry_points 调用报 "takes 1 positional argument"。
        with mock.patch.object(
            Registry, "_iter_entry_points", staticmethod(lambda group: eps)
        ):
            loaded = self.reg.load_entry_points("whatever")
        self.assertEqual(loaded, 1)
        self.assertEqual(self.reg.names(), ["good"])

    def test_iter_entry_points_stays_static_after_patching(self) -> None:
        """回归测试：上面那条测试不能污染类属性。"""
        self.assertIsInstance(
            inspect.getattr_static(Registry, "_iter_entry_points"), staticmethod
        )
        # 真的调一次，确认签名没被改成实例方法
        self.assertIsInstance(list(Registry._iter_entry_points("no.such.group")), list)


if __name__ == "__main__":
    unittest.main()
