"""`Id-Code` 签名的单测。

这里的**跨语言校验向量**是整个适配器最值钱的测试：签名算法错一位，服务端只
回一个笼统的 401，排查时完全看不出是签名问题。有了固定向量，移植错立刻定位。

向量来自上游 Python 版的真实输出（``[B]`` 级证据，经 APK dex 指令级证实），
在独立重写时用来对账 —— 只抄向量、不抄实现。
"""

from __future__ import annotations

import base64
import unittest

from kedaibiao.platforms.neusoft.signing import (
    ID_CODE_KEY,
    build_id_code_with_uuid,
    normalize_sign_path,
    zero_pad,
)

#: (userId, path, uuid, 期望的 base64 密文)
REFERENCE_VECTORS: tuple[tuple[str | None, str, str, str], ...] = (
    (
        "12345",
        "/web-api/system/auth/app/login",
        "fixed-uuid-0001",
        "iotSqIjwRj9OC7SfH9vpkVjC76EgCGj1RuuOS+76swm2y68/IZFbVQ7SVSGz+ef5k2311/J9jhXeiFr5DDkjzg==",
    ),
    (
        None,
        "/web-api/teachmanager/teach-dropdown/getTeachTermDropDown",
        "u2",
        "8lNAHr1Ibg4XtmgXn7oVPGinXR9fYqYgtRHXNQAH3RH6iNIc8qWYK+6xoCenJOpzakc13MmsJNzcJg6qa5nN0w==",
    ),
    ("", "/p", "u3", "HNDaHxTig8HRbqXtnH2Etg=="),
    (
        "1965315534179860482",
        "/web-api/teachmanager/teach-course-res-stu/study-task/page",
        "abcdef",
        "GSEjtzPehn+4nxluBSmY637vQxi0ljFeBJSJcajrpbBLc5waxb/+DauJbc1xkeDNvvOXNAi+XhMumsFuM4wxaZYkg/qH29tpKk+v1bNMsyXQ9lhCJQmvz7NCKnPpa+df",
    ),
)


def _decode(text: str) -> bytes:
    return base64.b64decode(text)


class ReferenceVectorTest(unittest.TestCase):
    """与上游 Python 版逐字节对账。"""

    def test_matches_reference_vectors(self) -> None:
        for user_id, path, request_uuid, want in REFERENCE_VECTORS:
            with self.subTest(path=path, user_id=user_id):
                got = build_id_code_with_uuid(user_id, path, request_uuid)
                self.assertEqual(got, want)

    def test_ciphertext_is_block_aligned(self) -> None:
        for _user_id, path, _request_uuid, want in REFERENCE_VECTORS:
            with self.subTest(path=path):
                self.assertEqual(len(_decode(want)) % 16, 0)

    def test_anonymous_user_is_zero(self) -> None:
        """未登录时 userId 记为 "0"，空串与 None 同义。"""
        none_code = build_id_code_with_uuid(None, "/p", "u")
        empty_code = build_id_code_with_uuid("", "/p", "u")
        zero_code = build_id_code_with_uuid("0", "/p", "u")
        self.assertEqual(none_code, empty_code)
        self.assertEqual(none_code, zero_code)


class ZeroPaddingTest(unittest.TestCase):
    """零填充与 PKCS#7 的差别 —— 用错会让签名整体失效。"""

    def test_exact_multiple_does_not_add_block(self) -> None:
        # 明文 = "1" + "_" + "u" + "_" + "/" + 11 个 a = 16 字节，正好整数倍，不该补成 32
        plain = "1_u_/" + "a" * 11
        self.assertEqual(len(plain), 16)
        self.assertEqual(len(zero_pad(plain.encode())), 16)
        cipher = _decode(build_id_code_with_uuid("1", "a" * 11, "u"))
        self.assertEqual(len(cipher), 16)

    def test_non_multiple_pads_to_next_block(self) -> None:
        for size in (1, 15, 17, 31, 33):
            with self.subTest(size=size):
                raw = b"x" * size
                padded = zero_pad(raw)
                self.assertEqual(len(padded) % 16, 0)
                self.assertGreaterEqual(len(padded), size)
                # 零填充：尾部全是 0
                self.assertTrue(set(padded[size:]) <= {0})

    def test_empty_input_becomes_one_block(self) -> None:
        self.assertEqual(zero_pad(b""), b"\x00" * 16)

    def test_key_and_iv_are_the_same_constant(self) -> None:
        # key == iv 是这个平台的怪癖，写死成断言防止有人「修好」它
        self.assertEqual(len(ID_CODE_KEY), 16)
        self.assertEqual(ID_CODE_KEY, b"neuedu_nse_12345")


class PathNormalizationTest(unittest.TestCase):
    """签名只覆盖相对路径：域名与 query 必须丢掉。"""

    def test_strips_domain_and_query(self) -> None:
        self.assertEqual(
            normalize_sign_path("https://study.example.edu.cn/web-api/x/y?a=1&b=2"),
            "/web-api/x/y",
        )
        self.assertEqual(normalize_sign_path("/web-api/x?pageNo=1"), "/web-api/x")
        self.assertEqual(normalize_sign_path("/web-api/x#frag"), "/web-api/x")

    def test_adds_leading_slash(self) -> None:
        self.assertEqual(normalize_sign_path("web-api/x"), "/web-api/x")

    def test_empty_path_degrades_to_root(self) -> None:
        self.assertEqual(normalize_sign_path(""), "/")

    def test_query_does_not_change_signature(self) -> None:
        with_query = build_id_code_with_uuid("1", "/web-api/x?a=1", "u")
        without = build_id_code_with_uuid("1", "/web-api/x", "u")
        self.assertEqual(with_query, without)


class RandomnessTest(unittest.TestCase):
    def test_uuid_makes_signature_differ(self) -> None:
        first = build_id_code_with_uuid("1", "/p", "uuid-a")
        second = build_id_code_with_uuid("1", "/p", "uuid-b")
        self.assertNotEqual(first, second)


if __name__ == "__main__":
    unittest.main()
