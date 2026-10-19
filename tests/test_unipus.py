"""U校园适配器测试：**完全离线**。

全部请求都注入假 ``Transport``（不发起任何真实网络请求，也不做真实站点冒烟）；
平台响应体要么手工构造，要么用本文件内的 :func:`wrap_cipher` 现场加密，
不依赖任何抓包样本。

规格没有给出可核验的登录密文/内容密文样例（第 3 节只给算法与参数），
所以密码学部分用**公开标准向量**验证 AES 实现本身：

- FIPS-197 附录 C.1（AES-128 单块）
- NIST SP 800-38A 附录 F.2.1（AES-128-CBC）

3.1/3.3 的整链则用「往返 + 结构断言」验证（已在测试名里标注）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import unittest
from typing import Any

from kedaibiao.core.models import (
    Assignment,
    Course,
    Credentials,
    QuestionType,
)
from kedaibiao.net.http import (
    HttpClient,
    Response,
    TransportError,
)
from kedaibiao.platforms.base import AdapterContext, NotSupportedError
from kedaibiao.platforms.unipus import codes, crypto, endpoints, parsing
from kedaibiao.platforms.unipus.adapter import UnipusAdapter
from kedaibiao.platforms.unipus.client import REQUEST_TIMEOUT_SECONDS, UnipusClient
from kedaibiao.platforms.unipus.endpoints import UnipusApp
from kedaibiao.platforms.unipus.errors import (
    UnipusApiError,
    UnipusAuthError,
    UnipusCryptoError,
    UnipusHttpError,
    UnipusNotLoggedIn,
    UnipusProtocolError,
    UnipusRateLimited,
)

# --------------------------------------------------------------------------
# 假传输层与响应构造
# --------------------------------------------------------------------------

#: 规格示例里的 k 是 8 位日期串 → 密钥 = 8 + 8 = 16 字节
K = "20260921"
#: 固定的「现在」（毫秒），让 annotator token 可复现
NOW_MS = 1_700_000_000_000
ANNOTATOR_HEADER_B64 = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"


class FakeTransport:
    """按脚本返回响应的假传输层；支持异常与可调用项。"""

    def __init__(self, *responses: Any) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def request(self, method, url, *, headers=None, body=None, timeout=15.0):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers or {}),
                "body": body,
                "timeout": timeout,
            }
        )
        if not self.responses:
            raise AssertionError(f"FakeTransport 没有更多预设响应，但收到了 {method} {url}")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        if callable(item):
            return item(method, url, headers or {}, body)
        return item


class SessionTransport(FakeTransport):
    """带 cookie 罐的假传输层：模拟浏览器会话的 cookie 连续性。"""

    def __init__(self, *responses: Any) -> None:
        super().__init__(*responses)
        self.jar: dict[str, str] = {}

    async def request(self, method, url, *, headers=None, body=None, timeout=15.0):
        effective = dict(headers or {})
        if self.jar:
            effective["Cookie"] = "; ".join(f"{name}={value}" for name, value in self.jar.items())
        response = await super().request(
            method, url, headers=effective, body=body, timeout=timeout
        )
        set_cookie = response.headers.get("Set-Cookie")
        if set_cookie:
            name, _, rest = set_cookie.partition("=")
            self.jar[name] = rest.split(";")[0]
        return response


def json_response(payload: Any, status: int = 200, url: str = "https://x/") -> Response:
    return Response(
        status=status,
        url=url,
        body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
    )


def raw_response(body: str, status: int = 200, url: str = "https://x/") -> Response:
    return Response(status=status, url=url, body=body.encode("utf-8"))


def html_login_response(status: int = 401) -> Response:
    return raw_response(
        "<!DOCTYPE html><html><body>请登录</body></html>", status=status
    )


# -- 密文构造（3.3 的 ZeroPadding + ECB） ---------------------------------


def ecb_encrypt(key: bytes, data: bytes) -> bytes:
    padded = data + b"\x00" * ((-len(data)) % 16) or b"\x00" * 16
    return b"".join(
        crypto.aes128_encrypt_block(key, padded[offset : offset + 16])
        for offset in range(0, len(padded), 16)
    )


def wrap_cipher(text: str, k: str = K) -> str:
    key = ("1a2b3c4d" + k).encode("ascii")
    return "unipus." + ecb_encrypt(key, text.encode("utf-8")).hex()


def encrypt_block_b64_for_test(open_id: str, now_ms: int) -> str:
    """独立复算 annotator token，用来验证签名（不复用被测函数）。"""
    header = base64.urlsafe_b64encode(b'{"alg":"HS256","typ":"JWT"}').rstrip(b"=").decode()
    payload_json = crypto.annotator_payload_json(open_id, now_ms=now_ms)
    payload = (
        base64.urlsafe_b64encode(payload_json.encode("utf-8")).rstrip(b"=").decode()
    )
    signing_input = f"{header}.{payload}"
    signature = hmac.new(
        b"example-annotator-signing-secret-0001",
        signing_input.encode("ascii"),
        hashlib.sha256,
    ).digest()
    return f"{signing_input}." + base64.urlsafe_b64encode(signature).rstrip(b"=").decode()


# -- 平台响应构造 ---------------------------------------------------------


def user_info_response(
    sso_id: Any = "900001", school: Any = 1000, app_user_id: Any = 555, name: str = "张三"
) -> Response:
    return json_response(
        {
            "code": 1,
            "data": {
                "ssoId": sso_id,
                "school": school,
                "appUserId": app_user_id,
                "name": name,
            },
        }
    )


def sso_ticket_response(ticket: str = "TICKET-1") -> Response:
    return json_response({"code": "0", "rs": {"serviceTicket": ticket}})


def sso_validate_response(jwt: str = "PORTAL-JWT") -> Response:
    return json_response({"code": "0", "rs": {"jwt": jwt}})


def course_list_response(classes: list[dict[str, Any]]) -> Response:
    return json_response({"code": 1, "value": {"courseList": classes}})


def make_class(
    *,
    class_id: str = "C1",
    name: str = "英语1班",
    class_pk: Any = 10001,
    books: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "classId": class_id,
        "name": name,
        "id": class_pk,
        "courseResourceList": books if books is not None else [make_book()],
    }


def make_book(
    *,
    instance_id: str = "course-v2:unipus+eng1+2026_T1",
    resource_id: str = "course-v1:unipus+eng1+2026",
    book_pk: Any = 70001,
    strategy_id: Any = 88001,
    name: str = "新视野大学英语1",
    finish: Any = 3,
    total: Any = 10,
) -> dict[str, Any]:
    return {
        "instanceId": instance_id,
        "resourceId": resource_id,
        "id": book_pk,
        "strategyId": strategy_id,
        "name": name,
        "finishPointNum": finish,
        "totalPointNum": total,
    }


def catalog_nodes() -> list[dict[str, Any]]:
    return [
        {
            "role": "unit",
            "url": "u1",
            "name": "Unit 1",
            "children": [
                {
                    "role": "group",
                    "url": "u1g1",
                    "name": "Listening",
                    "base": "a,b",
                    "tab_type": "x",
                },
                {"role": "group", "url": "u1g2", "name": "", "base": "", "tab_type": ""},
            ],
        },
        {
            "role": "unit",
            "url": "u2",
            "name": "Unit 2",
            "children": [{"role": "group", "url": "u2g1", "name": "Reading"}],
        },
    ]


def catalog_response(
    nodes: list[dict[str, Any]] | None = None,
    *,
    code: int = 0,
    extra: dict[str, Any] | None = None,
) -> Response:
    payload: dict[str, Any] = {
        "code": code,
        "course": json.dumps(catalog_nodes() if nodes is None else nodes),
    }
    if extra:
        payload.update(extra)
    return json_response(payload)


def content_item(
    *,
    item_id: Any = "1976531090427650001",
    inner: dict[str, Any] | None = None,
    item_extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "id": item_id,
        "content": json.dumps(inner if inner is not None else inner_for_choice()),
    }
    if item_extra:
        payload.update(item_extra)
    return payload


def inner_for_choice(*, reply_type: str = "singlechoice", category: int = 1) -> dict[str, Any]:
    return {
        "replyType": reply_type,
        "type": "choice",
        "category": category,
        "children": [{"stem": "占位"}],
        "contents": [{"type": 1}, {"type": 4}],
    }


def content_response(
    items: list[dict[str, Any]], *, field: str = "content", k: str = K, code: int = 0
) -> Response:
    cipher = wrap_cipher(json.dumps(items, ensure_ascii=False), k)
    return json_response({"code": code, "k": k, field: cipher})


def answer_response(
    answers: list[dict[str, Any]], *, field: str = "data", k: str = K, code: int = 0
) -> Response:
    cipher = wrap_cipher(json.dumps(answers, ensure_ascii=False), k)
    return json_response({"code": code, "k": k, field: cipher})


def answer_item(
    children: list[list[str]] | None = None, *, with_answer: bool = True
) -> dict[str, Any]:
    item: dict[str, Any] = {"questionId": "1976531090427650001"}
    if with_answer:
        item["answer"] = json.dumps({"children": [{"answers": group} for group in children or []]})
    return item


def progress_response(
    *,
    first_pass: Any = None,
    state: dict[str, Any] | None = None,
    flow_required: Any = True,
    task_mini_score_pct: Any = 0.6,
    code: int = 0,
) -> Response:
    return json_response(
        {
            "code": code,
            "data": {
                "first_pass": {"datetime": first_pass},
                "state": state
                if state is not None
                else {"state": 1, "lastSubmit": "5", "score": [1, 1, 0], "score_pct": 0.66},
                "flowStrategy": {"required": flow_required},
                "strategy": {"task_mini_score_pct": task_mini_score_pct},
            },
        }
    )


def make_client(
    *responses: Any, transport: FakeTransport | None = None
) -> tuple[UnipusClient, FakeTransport]:
    """造一个已登录的客户端（会话预填），网络走假传输层。"""
    fake = transport or FakeTransport(*responses)
    clock = lambda: NOW_MS  # noqa: E731 - 测试里就是要个假时钟
    client = UnipusClient(HttpClient(fake), timeout=REQUEST_TIMEOUT_SECONDS, clock=clock)
    client.session.portal_token = "PORTAL-JWT"
    client.session.open_id = "900001"
    client.session.school = "1000"
    return client, fake


def make_adapter(*responses: Any) -> tuple[UnipusAdapter, FakeTransport]:
    fake = FakeTransport(*responses)
    ctx = AdapterContext(http=HttpClient(fake))
    adapter = UnipusAdapter(ctx, clock=lambda: NOW_MS)
    adapter.client().session.portal_token = "PORTAL-JWT"
    adapter.client().session.open_id = "900001"
    adapter.client().session.school = "1000"
    return adapter, fake


def course_from(raw: dict[str, Any], *, name: str = "英语1班 · 新视野大学英语1") -> Course:
    return Course(remote_id=str(raw.get("course_resource_id") or "70001"), name=name, raw=raw)


# --------------------------------------------------------------------------
# 成功码：三套必须分开判定
# --------------------------------------------------------------------------


class TestSuccessCodeFamilies(unittest.TestCase):
    def test_sso_accepts_string_zero_and_number_zero(self) -> None:
        self.assertTrue(codes.ok_sso({"code": "0"}))
        self.assertTrue(codes.ok_sso({"code": 0}))

    def test_sso_rejects_one_and_missing(self) -> None:
        for body in ({"code": 1}, {"code": "1"}, {}, {"code": None}, "not-a-dict"):
            self.assertFalse(codes.ok_sso(body), body)

    def test_bool_is_not_zero(self) -> None:
        self.assertFalse(codes.ok_sso({"code": False}))
        self.assertFalse(codes.ok_ucontent({"code": False}))
        self.assertFalse(codes.ok_portal({"code": True}))

    def test_portal_accepts_only_number_one(self) -> None:
        self.assertTrue(codes.ok_portal({"code": 1}))
        for body in ({"code": 0}, {"code": "1"}, {}, {"code": True}):
            self.assertFalse(codes.ok_portal(body), body)

    def test_ucontent_accepts_only_number_zero(self) -> None:
        self.assertTrue(codes.ok_ucontent({"code": 0}))
        for body in ({"code": 1}, {"code": "0"}, {}, {"code": False}):
            self.assertFalse(codes.ok_ucontent(body), body)

    def test_same_body_judged_differently_per_family(self) -> None:
        """同一份响应在三套判定下结果不同 —— 证明没有通用 is_ok。"""
        self.assertTrue(codes.ok_sso({"code": "0"}))
        self.assertFalse(codes.ok_ucontent({"code": "0"}))
        self.assertTrue(codes.ok_portal({"code": 1}))
        self.assertFalse(codes.ok_ucontent({"code": 1}))
        self.assertTrue(codes.ok_ucontent({"code": 0}))
        self.assertFalse(codes.ok_portal({"code": 0}))

    def test_cipher_shell_missing_code_is_not_failure(self) -> None:
        self.assertFalse(codes.cipher_shell_failed({"anything": 1}))
        self.assertTrue(codes.cipher_shell_failed({"code": 2}))
        self.assertFalse(codes.cipher_shell_failed({"code": 0}))
        self.assertFalse(codes.cipher_shell_failed({"code": False}))

    def test_success_check_dispatches_and_rejects_unknown_family(self) -> None:
        self.assertTrue(codes.success_check("sso", {"code": "0"}))
        self.assertTrue(codes.success_check("portal", {"code": 1}))
        self.assertTrue(codes.success_check(codes.CIPHER_SHELL_FAMILY, {"code": 0}))
        with self.assertRaises(UnipusProtocolError):
            codes.success_check("whatever", {"code": 0})

    def test_message_extraction_priority_and_fallback(self) -> None:
        self.assertEqual(codes.extract_message({"message": "M", "msg": "m"}), "M")
        self.assertEqual(codes.extract_message({"msg": "m", "error": "e"}), "m")
        self.assertEqual(codes.extract_message({"error": "e", "errMsg": "x"}), "e")
        self.assertEqual(codes.extract_message({"errMsg": "x"}), "x")
        self.assertEqual(codes.extract_message({}), codes.FALLBACK_MESSAGE)
        self.assertEqual(codes.extract_message({"message": "  "}), codes.FALLBACK_MESSAGE)

    def test_rate_limit_codes(self) -> None:
        self.assertTrue(codes.is_rate_limited({"code": 600001}))
        self.assertTrue(codes.is_rate_limited({"code": 600002}))
        self.assertFalse(codes.is_rate_limited({"code": 6011}))
        self.assertFalse(codes.is_rate_limited({}))

    def test_looks_like_html(self) -> None:
        self.assertTrue(codes.looks_like_html("<html></html>"))
        self.assertTrue(codes.looks_like_html("  \n<!DOCTYPE html>"))
        self.assertTrue(codes.looks_like_html("<div>x</div>"))
        self.assertFalse(codes.looks_like_html('{"code":0}'))
        self.assertFalse(codes.looks_like_html(""))


# --------------------------------------------------------------------------
# 端点与 URL 拼接
# --------------------------------------------------------------------------


class TestEndpoints(unittest.TestCase):
    def test_url_for_keeps_colon_and_plus_raw(self) -> None:
        url = endpoints.url_for(
            UnipusApp.UCONTENT,
            endpoints.UCONTENT_CONTENT,
            course_id="course-v2:unipus+eng1+2026_T1",
            group_id="u1g2",
        )
        self.assertEqual(
            url,
            "https://ucontent.unipus.cn/course/api/v3/content/"
            "course-v2:unipus+eng1+2026_T1/u1g2/default",
        )
        self.assertNotIn("%3A", url)
        self.assertNotIn("%2B", url)

    def test_catalog_and_query_paths(self) -> None:
        self.assertEqual(
            endpoints.url_for(UnipusApp.UCONTENT, endpoints.UCONTENT_CATALOG, course_id="c"),
            "https://ucontent.unipus.cn/course/api/course/c/default",
        )
        self.assertEqual(endpoints.MODE_SEGMENT, "default")

    def test_url_for_missing_param_raises(self) -> None:
        with self.assertRaises(UnipusProtocolError):
            endpoints.url_for(UnipusApp.UCONTENT, endpoints.UCONTENT_CONTENT, course_id="c")

    def test_domain_assertion_rejects_other_host(self) -> None:
        with self.assertRaises(UnipusProtocolError):
            endpoints.assert_url_for_app(
                UnipusApp.PORTAL, "https://ucontent.unipus.cn/course/api/course/c/default"
            )
        with self.assertRaises(UnipusProtocolError):
            endpoints.assert_url_for_app(UnipusApp.UCONTENT, "https://evil.example/x")

    def test_query_url_keeps_commas_and_encodes_spaces(self) -> None:
        url = endpoints.query_url("https://ucontent.unipus.cn/p", tasks="u1g1,u1g2", q="a b")
        self.assertIn("tasks=u1g1,u1g2", url)
        self.assertIn("q=a%20b", url)

    def test_comma_join(self) -> None:
        self.assertEqual(endpoints.comma_join(("u1g1", "u2g1")), "u1g1,u2g1")
        self.assertEqual(endpoints.comma_join([]), "")

    def test_fixed_constants_from_spec(self) -> None:
        self.assertEqual(endpoints.PORTAL_SERVICE, "https://uai.unipus.cn/portal")
        self.assertEqual(endpoints.UCONTENT_AUTH_HEADER, "x-annotator-auth-token")
        self.assertEqual(endpoints.PORTAL_AUTH_HEADER, "Authorization")
        self.assertEqual(endpoints.HOST_APP_ID, "116")
        self.assertEqual(endpoints.HOST_PLATFORM_WEB, "2")
        self.assertEqual(endpoints.CIPHER_KEY_PREFIX, "1a2b3c4d")
        self.assertEqual(endpoints.CIPHER_PREFIX, "unipus.")
        self.assertEqual(endpoints.FEATURE_DENIED_CODE, 6011)
        self.assertEqual(endpoints.RATE_LIMIT_CODES, frozenset({600001, 600002}))

    def test_all_three_bases_present(self) -> None:
        self.assertEqual(
            {app.value: base for app, base in endpoints.BASE_URLS.items()},
            {
                "sso": "https://sso.unipus.cn",
                "portal": "https://uai.unipus.cn",
                "ucontent": "https://ucontent.unipus.cn",
            },
        )


# --------------------------------------------------------------------------
# 密码学：标准向量 + 往返
# --------------------------------------------------------------------------


class TestAesPrimitives(unittest.TestCase):
    def test_fips197_c1_encrypt_vector(self) -> None:
        key = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
        plain = bytes.fromhex("00112233445566778899aabbccddeeff")
        self.assertEqual(
            crypto.aes128_encrypt_block(key, plain).hex(),
            "69c4e0d86a7b0430d8cdb78070b4c55a",
        )

    def test_fips197_c1_decrypt_vector(self) -> None:
        key = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
        cipher = bytes.fromhex("69c4e0d86a7b0430d8cdb78070b4c55a")
        self.assertEqual(
            crypto.aes128_decrypt_block(key, cipher).hex(),
            "00112233445566778899aabbccddeeff",
        )

    def test_block_roundtrip_and_wrong_key(self) -> None:
        key = bytes(range(16))
        block = bytes(range(16, 32))
        encrypted = crypto.aes128_encrypt_block(key, block)
        self.assertEqual(crypto.aes128_decrypt_block(key, encrypted), block)
        self.assertNotEqual(crypto.aes128_decrypt_block(bytes(reversed(key)), encrypted), block)

    def test_nist_sp800_38a_cbc_vector(self) -> None:
        key = bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c")
        iv = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
        plain = bytes.fromhex(
            "6bc1bee22e409f96e93d7e117393172a" "ae2d8a571e03ac9c9eb76fac45af8e51"
        )
        self.assertEqual(
            crypto.aes128_cbc_encrypt(key, iv, plain).hex(),
            "7649abac8119b246cee98e9b12e9197d" "5086cb9b507219ee95db113a917678b2",
        )

    def test_cbc_requires_alignment_and_iv(self) -> None:
        key = bytes(16)
        with self.assertRaises(UnipusCryptoError):
            crypto.aes128_cbc_encrypt(key, bytes(16), b"short")
        with self.assertRaises(UnipusCryptoError):
            crypto.aes128_cbc_encrypt(key, bytes(8), bytes(16))

    def test_ecb_decrypt_rejects_empty_and_unaligned(self) -> None:
        with self.assertRaises(UnipusCryptoError):
            crypto.aes128_ecb_decrypt(bytes(16), b"")
        with self.assertRaises(UnipusCryptoError):
            crypto.aes128_ecb_decrypt(bytes(16), b"12345")

    def test_key_schedule_rejects_wrong_key_size(self) -> None:
        with self.assertRaises(UnipusCryptoError):
            crypto.aes128_encrypt_block(bytes(15), bytes(16))

    def test_pkcs7_pad_always_adds_a_full_block(self) -> None:
        self.assertEqual(len(crypto.pkcs7_pad(b"")), 16)
        self.assertEqual(crypto.pkcs7_pad(b"")[:1], b"\x10")
        self.assertEqual(len(crypto.pkcs7_pad(b"x" * 15)), 16)
        self.assertEqual(crypto.pkcs7_pad(b"x" * 15)[-1:], b"\x01")
        self.assertEqual(len(crypto.pkcs7_pad(b"x" * 16)), 32)
        self.assertEqual(crypto.pkcs7_pad(b"x" * 16)[-1:], b"\x10")

    def test_strip_trailing_zeros(self) -> None:
        self.assertEqual(crypto.strip_trailing_zeros(b"abc\x00\x00"), b"abc")
        self.assertEqual(crypto.strip_trailing_zeros(b"\x00\x00"), b"")


class TestLoginFieldEncryption(unittest.TestCase):
    """规格第 3.1 节没有给出密文样例 —— 这里验证结构与往返。"""

    KEY = bytes.fromhex(endpoints.LOGIN_AES_KEY_HEX)
    IV = bytes.fromhex(endpoints.LOGIN_AES_IV_HEX)

    def _cbc_decrypt(self, data: bytes) -> bytes:
        out = bytearray()
        previous = self.IV
        for offset in range(0, len(data), 16):
            block = crypto.aes128_decrypt_block(self.KEY, data[offset : offset + 16])
            out += bytes(a ^ b for a, b in zip(block, previous))
            previous = data[offset : offset + 16]
        return bytes(out)

    def _roundtrip(self, plaintext: str) -> str:
        cipher_hex = crypto.encrypt_login_field(plaintext)
        raw = bytes.fromhex(cipher_hex)
        padded = self._cbc_decrypt(raw)
        return padded[: -padded[-1]].decode("utf-8")

    def test_output_is_uppercase_hex_multiple_of_32(self) -> None:
        out = crypto.encrypt_login_field("alice")
        self.assertEqual(out, out.upper())
        self.assertEqual(len(out) % 32, 0)
        self.assertRegex(out, r"^[0-9A-F]+$")

    def test_deterministic(self) -> None:
        self.assertEqual(
            crypto.encrypt_login_field("secret"), crypto.encrypt_login_field("secret")
        )

    def test_roundtrip_ascii_and_unicode(self) -> None:
        for plaintext in ("alice", "s3cret!", "学号20260001", "中文密码"):
            self.assertEqual(self._roundtrip(plaintext), plaintext)

    def test_empty_password_still_produces_a_block(self) -> None:
        self.assertEqual(len(crypto.encrypt_login_field("")), 32)
        self.assertEqual(self._roundtrip(""), "")

    def test_different_plaintexts_differ(self) -> None:
        self.assertNotEqual(
            crypto.encrypt_login_field("alice"), crypto.encrypt_login_field("alicf")
        )

    def test_fixed_key_and_iv_match_spec(self) -> None:
        self.assertEqual(len(self.KEY), 16)
        self.assertEqual(self.IV, bytes(range(1, 17)))
        self.assertEqual(endpoints.LOGIN_AES_KEY_HEX, "8AD70B641C024C7ADA2ECD082EC0334F")


class TestAnnotatorToken(unittest.TestCase):
    def test_header_segment_is_the_fixed_constant(self) -> None:
        token = crypto.build_annotator_token("900001", now_ms=NOW_MS)
        self.assertEqual(token.split(".")[0], ANNOTATOR_HEADER_B64)

    def test_three_segments_and_no_padding(self) -> None:
        token = crypto.build_annotator_token("900001", now_ms=NOW_MS)
        self.assertEqual(len(token.split(".")), 3)
        self.assertNotIn("=", token)
        self.assertNotIn("+", token)
        self.assertNotIn("/", token)

    def test_payload_field_order_and_fixed_values(self) -> None:
        token = crypto.build_annotator_token("900001", now_ms=NOW_MS)
        payload_segment = token.split(".")[1]
        raw = base64.urlsafe_b64decode(payload_segment + "=" * (-len(payload_segment) % 4))
        text = raw.decode("utf-8")
        self.assertEqual(
            text,
            '{"open_id":"900001","name":"","email":"","administrator":false,'
            f'"exp":{NOW_MS + endpoints.ANNOTATOR_TTL_MS},'
            '"iss":"example-annotator-issuer","aud":"edx.unipus.cn"}',
        )
        self.assertEqual(
            [key for key in json.loads(text)],
            list(crypto.ANNOTATOR_PAYLOAD_ORDER),
        )

    def test_signature_verifies_independently(self) -> None:
        self.assertEqual(
            crypto.build_annotator_token("900001", now_ms=NOW_MS),
            encrypt_block_b64_for_test("900001", NOW_MS),
        )

    def test_expected_payload_is_exact(self) -> None:
        self.assertEqual(
            crypto.annotator_payload_json("abc", now_ms=0),
            '{"open_id":"abc","name":"","email":"","administrator":false,'
            f'"exp":{endpoints.ANNOTATOR_TTL_MS},'
            '"iss":"example-annotator-issuer","aud":"edx.unipus.cn"}',
        )

    def test_empty_open_id_raises(self) -> None:
        for value in ("", None):
            with self.assertRaises(UnipusProtocolError):
                crypto.build_annotator_token(value, now_ms=NOW_MS)  # type: ignore[arg-type]

    def test_minimal_json_escaping(self) -> None:
        tricky = 'a"b\\c\nd\re\tf'
        token = crypto.build_annotator_token(tricky, now_ms=NOW_MS)
        segment = token.split(".")[1]
        raw = base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
        self.assertEqual(json.loads(raw.decode("utf-8"))["open_id"], tricky)

    def test_non_ascii_open_id_kept_parsable(self) -> None:
        token = crypto.build_annotator_token("学号1", now_ms=NOW_MS)
        segment = token.split(".")[1]
        raw = base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
        self.assertEqual(json.loads(raw.decode("utf-8"))["open_id"], "学号1")

    def test_b64url_uses_urlsafe_alphabet(self) -> None:
        encoded = crypto.b64url_encode(bytes([0xFB, 0xFF, 0xBF]))
        self.assertEqual(encoded, "-_-_")
        self.assertNotIn("=", encoded)


class TestCipherDecryption(unittest.TestCase):
    def test_plaintext_passthrough(self) -> None:
        self.assertEqual(crypto.decrypt_unipus_cipher("hello", K), "hello")
        self.assertEqual(crypto.decrypt_unipus_cipher("", K), "")

    def test_uppercase_prefix_is_treated_as_plaintext(self) -> None:
        """规则是「不以 ``unipus.`` 开头即明文」，大小写敏感。"""
        self.assertEqual(crypto.decrypt_unipus_cipher("UNIPUS.AB", K), "UNIPUS.AB")

    def test_roundtrip_with_zero_padding(self) -> None:
        text = "学英语的题目内容"
        self.assertEqual(crypto.decrypt_unipus_cipher(wrap_cipher(text), K), text)

    def test_roundtrip_when_plaintext_is_block_aligned(self) -> None:
        text = "abcdefghijklmnop"  # 恰好 16 字节，无填充
        self.assertEqual(crypto.decrypt_unipus_cipher(wrap_cipher(text), K), text)

    def test_all_zero_plaintext_becomes_empty(self) -> None:
        cipher = "unipus." + ecb_encrypt(("1a2b3c4d" + K).encode(), b"\x00" * 16).hex()
        self.assertEqual(crypto.decrypt_unipus_cipher(cipher, K), "")

    def test_key_must_be_exactly_16_bytes(self) -> None:
        with self.assertRaises(UnipusCryptoError):
            crypto.cipher_key("123")
        with self.assertRaises(UnipusCryptoError):
            crypto.cipher_key("123456789")
        self.assertEqual(len(crypto.cipher_key(K)), 16)

    def test_non_hex_payload_raises(self) -> None:
        with self.assertRaises(UnipusCryptoError):
            crypto.decrypt_unipus_cipher("unipus.zzzz", K)

    def test_empty_or_unaligned_payload_raises(self) -> None:
        with self.assertRaises(UnipusCryptoError):
            crypto.decrypt_unipus_cipher("unipus.", K)
        with self.assertRaises(UnipusCryptoError):
            crypto.decrypt_unipus_cipher("unipus.0011", K)

    def test_invalid_utf8_raises(self) -> None:
        cipher = "unipus." + ecb_encrypt(("1a2b3c4d" + K).encode(), b"\xff\xfe").hex()
        with self.assertRaises(UnipusCryptoError):
            crypto.decrypt_unipus_cipher(cipher, K)

    def test_bool_or_none_k_raises(self) -> None:
        for value in (None, True, False):
            with self.assertRaises(UnipusCryptoError):
                crypto.cipher_key(value)


# --------------------------------------------------------------------------
# 解析：用户信息 / 课程 / 目录
# --------------------------------------------------------------------------


class TestParseSession(unittest.TestCase):
    def test_open_id_comes_from_sso_id_only(self) -> None:
        info = parsing.parse_session(
            {"data": {"ssoId": "S-1", "appUserId": "APP-9", "school": 1000}}
        )
        self.assertEqual(info.open_id, "S-1")
        self.assertEqual(info.app_user_id, "APP-9")

    def test_app_user_id_alone_is_not_enough(self) -> None:
        with self.assertRaises(UnipusProtocolError):
            parsing.parse_session({"data": {"appUserId": "APP-9"}})

    def test_school_may_be_numeric(self) -> None:
        info = parsing.parse_session({"data": {"ssoId": "S", "school": 1000}})
        self.assertEqual(info.school, "1000")

    def test_display_name_fallback_order(self) -> None:
        self.assertEqual(parsing.parse_session({"ssoId": "S", "name": "甲"}).display_name, "甲")
        self.assertEqual(
            parsing.parse_session({"ssoId": "S", "nickName": "乙", "name": ""}).display_name, "乙"
        )
        self.assertEqual(
            parsing.parse_session({"ssoId": "S", "username": "丙"}).display_name, "丙"
        )
        self.assertEqual(parsing.parse_session({"ssoId": "S"}).display_name, "")

    def test_deep_search_for_nested_structure(self) -> None:
        info = parsing.parse_session(
            {"code": 1, "data": {"user": {"profile": {"ssoId": "N-1", "school": "9"}}}}
        )
        self.assertEqual((info.open_id, info.school), ("N-1", "9"))

    def test_non_dict_response_raises(self) -> None:
        with self.assertRaises(UnipusProtocolError):
            parsing.parse_session([])


class TestParseCourseList(unittest.TestCase):
    def test_happy_path(self) -> None:
        bindings = parsing.parse_course_list(
            {
                "code": 1,
                "value": {"courseList": [make_class()]},
            }
        )
        self.assertEqual(len(bindings), 1)
        binding = bindings[0]
        self.assertEqual(binding.class_id, "C1")
        self.assertEqual(binding.class_name, "英语1班")
        self.assertEqual(binding.class_numeric_id, "10001")
        self.assertEqual(binding.course_instance_id, "course-v2:unipus+eng1+2026_T1")
        self.assertEqual(binding.course_resource_id, "70001")
        self.assertEqual(binding.resource_id, "course-v1:unipus+eng1+2026")
        self.assertEqual(binding.strategy_id, "88001")
        self.assertEqual((binding.finish_points, binding.total_points), (3, 10))
        self.assertEqual(binding.remote_id, "70001")

    def test_entries_missing_class_or_instance_are_dropped(self) -> None:
        classes = [
            {"classId": "C1", "name": "no-books", "courseResourceList": []},
            {"name": "no-class-id", "courseResourceList": [make_book()]},
            {
                "classId": "C2",
                "courseResourceList": [{"id": 1, "name": "no instance"}],
            },
            make_class(),
        ]
        bindings = parsing.parse_course_list({"value": {"courseList": classes}})
        self.assertEqual([b.class_id for b in bindings], ["C1"])

    def test_missing_course_list_raises(self) -> None:
        with self.assertRaises(UnipusProtocolError):
            parsing.parse_course_list({"code": 1, "value": {}})

    def test_course_instance_id_fallback_key(self) -> None:
        book = make_book()
        book.pop("instanceId")
        book["courseInstanceId"] = "course-v2:fallback"
        bindings = parsing.parse_course_list(
            {"value": {"courseList": [make_class(books=[book])]}}
        )
        self.assertEqual(bindings[0].course_instance_id, "course-v2:fallback")

    def test_unknown_textbook_list_key_is_still_found(self) -> None:
        class_node = {
            "classId": "C1",
            "name": "X",
            "somethingElse": [make_book()],
        }
        bindings = parsing.parse_course_list({"value": {"courseList": [class_node]}})
        self.assertEqual(len(bindings), 1)

    def test_numeric_like_strings_are_preserved(self) -> None:
        bindings = parsing.parse_course_list(
            {"value": {"courseList": [make_class(class_pk="10001")]}}
        )
        self.assertEqual(bindings[0].class_numeric_id, "10001")


class TestParseBookshelf(unittest.TestCase):
    def test_root_and_list_key_variants(self) -> None:
        for root_key in ("data", "value"):
            for list_key in ("classList", "classes", "courseList"):
                body = {
                    root_key: {
                        list_key: [{"courseResourceList": [make_book() | {"activation": 0}]}]
                    }
                }
                books = parsing.parse_bookshelf(body)
                self.assertEqual(len(books), 1, (root_key, list_key))
                self.assertTrue(books[0].activated)

    def test_activation_zero_means_activated(self) -> None:
        book = make_book() | {"activation": 0}
        books = parsing.parse_bookshelf({"data": {"classList": [{"courseResourceList": [book]}]}})
        self.assertIs(books[0].activated, True)

    def test_activation_one_means_not_activated(self) -> None:
        book = make_book() | {"activation": 1}
        books = parsing.parse_bookshelf({"data": {"classList": [{"courseResourceList": [book]}]}})
        self.assertIs(books[0].activated, False)

    def test_activation_boolean_and_string_forms(self) -> None:
        cases = [(True, True), (False, False), ("true", True), ("0", True), ("1", False)]
        for raw_value, expected in cases:
            book = make_book() | {"activation": raw_value}
            books = parsing.parse_bookshelf(
                {"data": {"classList": [{"courseResourceList": [book]}]}}
            )
            self.assertIs(books[0].activated, expected, raw_value)

    def test_missing_activation_is_none_not_false(self) -> None:
        books = parsing.parse_bookshelf(
            {"data": {"classList": [{"courseResourceList": [make_book()]}]}}
        )
        self.assertIsNone(books[0].activated)

    def test_resource_id_only_node_is_kept(self) -> None:
        book = {"resourceId": "course-v1:only", "name": "X"}
        books = parsing.parse_bookshelf({"data": {"classList": [{"courseResourceList": [book]}]}})
        self.assertEqual(books[0].resource_id, "course-v1:only")
        self.assertEqual(books[0].course_instance_id, "")

    def test_missing_class_list_raises(self) -> None:
        with self.assertRaises(UnipusProtocolError):
            parsing.parse_bookshelf({"data": {}})


class TestParseCatalog(unittest.TestCase):
    def test_units_and_groups(self) -> None:
        catalog = parsing.parse_catalog(catalog_response().json())
        self.assertEqual([unit.unit_id for unit in catalog.units], ["u1", "u2"])
        self.assertEqual([unit.name for unit in catalog.units], ["Unit 1", "Unit 2"])
        self.assertEqual(
            [group.group_id for group in catalog.units[0].groups], ["u1g1", "u1g2"]
        )
        self.assertEqual(catalog.units[0].groups[0].base, "a,b")
        self.assertEqual(catalog.units[0].groups[0].tab_type, "x")
        self.assertEqual(catalog.units[0].groups[0].name, "Listening")

    def test_duplicate_unit_ids_are_merged(self) -> None:
        nodes = [
            {"role": "unit", "url": "u1", "name": "Unit 1"},
            {"role": "group", "url": "u1g1", "name": "A"},
            {"role": "unit", "url": "u1", "name": "Unit 1", "children": []},
            {"role": "group", "url": "u1g2", "name": "B"},
            {"role": "group", "url": "u1g1", "name": "A dup"},
        ]
        catalog = parsing.parse_catalog(catalog_response(nodes).json())
        self.assertEqual(len(catalog.units), 1)
        self.assertEqual(
            [group.group_id for group in catalog.units[0].groups], ["u1g1", "u1g2"]
        )

    def test_course_field_may_be_object(self) -> None:
        body = {"code": 0, "course": catalog_nodes()}
        catalog = parsing.parse_catalog(body)
        self.assertEqual(len(catalog.units), 2)

    def test_group_before_unit_goes_to_empty_unit_bucket(self) -> None:
        nodes = [{"role": "group", "url": "g9", "name": "orphan"}]
        catalog = parsing.parse_catalog(catalog_response(nodes).json())
        self.assertEqual(catalog.units[0].unit_id, "")
        self.assertEqual(catalog.units[0].groups[0].group_id, "g9")

    def test_missing_course_field_raises(self) -> None:
        with self.assertRaises(UnipusProtocolError):
            parsing.parse_catalog({"code": 0})

    def test_non_json_course_field_raises(self) -> None:
        with self.assertRaises(UnipusProtocolError):
            parsing.parse_catalog({"code": 0, "course": "not json"})

    def test_resource_id_recovered_from_cover_url(self) -> None:
        nodes = catalog_nodes()
        nodes[0]["imgUrl"] = "https://cdn/x?r=course-v1:unipus+eng1+2026&v=1"
        catalog = parsing.parse_catalog(catalog_response(nodes).json())
        self.assertEqual(catalog.resource_id, "course-v1:unipus+eng1+2026")

    def test_resource_id_absent_is_empty(self) -> None:
        self.assertEqual(parsing.parse_catalog(catalog_response().json()).resource_id, "")

    def test_catalog_group_ids_order(self) -> None:
        catalog = parsing.parse_catalog(catalog_response().json())
        self.assertEqual(
            parsing.catalog_group_ids(catalog), ("u1g1", "u1g2", "u2g1")
        )


# --------------------------------------------------------------------------
# 题型分派
# --------------------------------------------------------------------------


class TestClassifyQuestion(unittest.TestCase):
    def classify(self, **kwargs: Any) -> tuple[str, int]:
        defaults = {"reply_type": "", "question_type": "", "category": 1, "child_count": 3}
        return parsing.classify_question(**{**defaults, **kwargs})

    def test_study_rules(self) -> None:
        self.assertEqual(self.classify(category=0), ("study", 0))
        self.assertEqual(self.classify(question_type="discussion"), ("study", 0))
        self.assertEqual(self.classify(question_type="rich-text-read"), ("study", 0))
        self.assertEqual(
            self.classify(reply_type="", question_type="", child_count=0), ("study", 0)
        )

    def test_video_popup_is_not_study(self) -> None:
        kind, count = self.classify(question_type="video-popup", reply_type="video")
        self.assertNotEqual(kind, "study")
        self.assertEqual(count, 3)

    def test_category_one_is_not_study(self) -> None:
        self.assertEqual(self.classify(category=1, reply_type="singlechoice")[0], "single-choice")

    def test_role_play_counts_one_regardless_of_children(self) -> None:
        self.assertEqual(self.classify(question_type="role-play", child_count=5), ("role-play", 1))
        self.assertEqual(
            self.classify(question_type="roleplay", reply_type="record", child_count=0),
            ("role-play", 1),
        )

    def test_study_predicate_runs_first_even_for_typed_questions(self) -> None:
        """规格字面顺序：0 子题且 reply_type 为空 → 先判学习题（贡献 0 项）。

        这条在规格第 7.2 节里没有例外说明，所以照字面实现并在此固化；
        若真实平台对 role-play/upload 有不同行为，需要规格澄清。
        """
        self.assertEqual(self.classify(question_type="role-play", child_count=0), ("study", 0))
        self.assertEqual(
            self.classify(question_type="multiFileUpload", child_count=0), ("study", 0)
        )

    def test_oral_state_precedes_oral(self) -> None:
        """``oral-state`` 的 replyType 也是 ``record``：顺序错了就静默少算。"""
        self.assertEqual(
            self.classify(question_type="oral-state", reply_type="record", child_count=4),
            ("oral-state", 1),
        )

    def test_oral_variants(self) -> None:
        for question_type in parsing.ORAL_QUESTION_TYPES:
            self.assertEqual(self.classify(question_type=question_type)[0], "oral", question_type)
        self.assertEqual(self.classify(reply_type="record", child_count=0), ("oral", 1))
        self.assertEqual(self.classify(reply_type="record", child_count=2), ("oral", 2))
        self.assertEqual(self.classify(question_type="my-record-thing")[0], "oral")

    def test_upload(self) -> None:
        self.assertEqual(
            self.classify(question_type="multiFileUpload", child_count=2), ("upload", 2)
        )
        self.assertEqual(
            self.classify(question_type="multiFileUpload", reply_type="x", child_count=0),
            ("upload", 1),
        )
        self.assertEqual(self.classify(reply_type="multimedia_upload")[0], "upload")

    def test_subjective(self) -> None:
        self.assertEqual(self.classify(reply_type="text-area", child_count=0), ("subjective", 1))

    def test_choice_types(self) -> None:
        self.assertEqual(self.classify(reply_type="singlechoice")[0], "single-choice")
        self.assertEqual(self.classify(reply_type="multichoice")[0], "multi-choice")

    def test_dropdown_spellings(self) -> None:
        for reply_type in ("dropdownSelection", "dropdownchoic", "dropdown"):
            self.assertEqual(self.classify(reply_type=reply_type)[0], "dropdown", reply_type)

    def test_fill_blank_and_banked_cloze(self) -> None:
        self.assertEqual(self.classify(reply_type="fillblank")[0], "fill-blank")
        self.assertEqual(self.classify(reply_type="fillblankScoop")[0], "fill-blank")
        self.assertEqual(self.classify(reply_type="bankedcloze")[0], "banked-cloze")
        self.assertEqual(self.classify(reply_type="material-banked-cloze")[0], "banked-cloze")
        self.assertEqual(self.classify(question_type="x-banked-cloze-y")[0], "banked-cloze")

    def test_sequence(self) -> None:
        self.assertEqual(self.classify(reply_type="sequence")[0], "sequence")
        self.assertEqual(self.classify(question_type="sequence")[0], "sequence")
        self.assertEqual(self.classify(question_type="re-sequence")[0], "sequence")

    def test_objective_fallback(self) -> None:
        self.assertEqual(
            self.classify(reply_type="mystery", question_type="mystery"), ("objective", 3)
        )
        self.assertEqual(self.classify(reply_type="mystery", child_count=0), ("objective", 1))

    def test_core_type_mapping_covers_all_thirteen(self) -> None:
        self.assertEqual(
            set(parsing.CORE_TYPE_BY_UNIPUS_TYPE),
            {
                "study",
                "role-play",
                "oral-state",
                "oral",
                "upload",
                "subjective",
                "single-choice",
                "multi-choice",
                "dropdown",
                "fill-blank",
                "banked-cloze",
                "sequence",
                "objective",
            },
        )
        self.assertEqual(
            parsing.core_question_type("single-choice"), QuestionType.SINGLE
        )
        self.assertEqual(parsing.core_question_type("multi-choice"), QuestionType.MULTIPLE)
        self.assertEqual(parsing.core_question_type("fill-blank"), QuestionType.FILL)
        self.assertEqual(parsing.core_question_type("banked-cloze"), QuestionType.FILL)
        self.assertEqual(parsing.core_question_type("subjective"), QuestionType.ESSAY)
        for name in ("oral", "oral-state", "role-play", "upload", "sequence", "objective", "study"):
            self.assertEqual(parsing.core_question_type(name), QuestionType.UNKNOWN, name)


# --------------------------------------------------------------------------
# 解析：内容 / 答案 / 状态 / 必修集
# --------------------------------------------------------------------------


class TestParseContent(unittest.TestCase):
    BIG_ID = "1976531090427650421"

    def test_happy_path_and_nineteen_digit_precision(self) -> None:
        body = content_response([content_item(item_id=self.BIG_ID)]).json()
        items = parsing.parse_content(body)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].instance_id, self.BIG_ID)
        self.assertEqual(items[0].instance_id, str(self.BIG_ID))
        self.assertEqual(items[0].child_count, 1)
        self.assertEqual(items[0].unipus_type, "single-choice")
        self.assertEqual(items[0].core_type, QuestionType.SINGLE)
        self.assertEqual(items[0].item_count, 1)

    def test_id_as_json_number_is_exact(self) -> None:
        item = content_item(item_id=int(self.BIG_ID))
        body = content_response([item]).json()
        parsed = parsing.parse_content(body)
        self.assertEqual(parsed[0].instance_id, self.BIG_ID)

    def test_float_id_is_rejected(self) -> None:
        """浮点会损坏 19 位 ID —— 必须显式报错，不能悄悄取整。"""
        item = content_item(item_id=1.9765310904276504e18)
        body = content_response([item]).json()
        with self.assertRaises(UnipusProtocolError):
            parsing.parse_content(body)

    def test_field_precedence(self) -> None:
        inner = {
            "replyType": "multichoice",
            "type": "inner-type",
            "category": 1,
            "children": [{"a": 1}, {"b": 2}],
        }
        item = content_item(
            inner=inner, item_extra={"replyType": "singlechoice", "type": "item-type"}
        )
        parsed = parsing.parse_content(content_response([item]).json())[0]
        self.assertEqual(parsed.reply_type, "multichoice")
        self.assertEqual(parsed.question_type, "item-type")
        self.assertEqual(parsed.child_count, 2)
        self.assertEqual(parsed.unipus_type, "multi-choice")

    def test_media_types_are_recorded(self) -> None:
        inner = inner_for_choice()
        inner["contents"] = [{"type": 1}, {"type": 2}, {"type": 4}, {"type": 6}]
        parsed = parsing.parse_content(content_response([content_item(inner=inner)]).json())[0]
        self.assertEqual(parsed.media_types, (1, 2, 4, 6))
        self.assertEqual(
            [t for t in parsed.media_types if t in parsing.MEDIA_CONTENT_TYPES], [1, 2]
        )

    def test_content_field_may_be_object(self) -> None:
        item = {"id": self.BIG_ID, "content": inner_for_choice()}
        parsed = parsing.parse_content(content_response([item]).json())[0]
        self.assertEqual(parsed.unipus_type, "single-choice")

    def test_fallback_scan_when_field_name_missing(self) -> None:
        cipher = wrap_cipher(json.dumps([content_item(item_id=self.BIG_ID)]))
        body = {"code": 0, "k": K, "surprise": cipher}
        parsed = parsing.parse_content(body)
        self.assertEqual(parsed[0].instance_id, self.BIG_ID)

    def test_preview_object_schema_raises(self) -> None:
        cipher = wrap_cipher(json.dumps({"questions:questions": []}))
        body = {"code": 0, "k": K, "content": cipher}
        with self.assertRaises(UnipusProtocolError) as cm:
            parsing.parse_content(body)
        self.assertIn("course-v2", str(cm.exception))

    def test_missing_id_or_content_raises(self) -> None:
        with self.assertRaises(UnipusProtocolError):
            parsing.parse_content(content_response([{"content": "{}"}]).json())
        with self.assertRaises(UnipusProtocolError):
            parsing.parse_content(content_response([{"id": self.BIG_ID}]).json())

    def test_missing_k_raises(self) -> None:
        body = {"code": 0, "content": wrap_cipher("[]")}
        with self.assertRaises(UnipusProtocolError):
            parsing.parse_content(body)

    def test_decrypted_non_array_raises(self) -> None:
        body = {"code": 0, "k": K, "content": wrap_cipher('"just a string"')}
        with self.assertRaises(UnipusProtocolError):
            parsing.parse_content(body)

    def test_bad_hex_cipher_raises(self) -> None:
        body = {"code": 0, "k": K, "content": "unipus.nothex"}
        with self.assertRaises(UnipusCryptoError):
            parsing.parse_content(body)


class TestParseAnswers(unittest.TestCase):
    def test_happy_path(self) -> None:
        body = answer_response([answer_item([["D"], ["A", "B", "D"]])]).json()
        answers = parsing.parse_answers(body)
        self.assertEqual(len(answers), 1)
        self.assertEqual(answers[0].children, (("D",), ("A", "B", "D")))

    def test_uses_data_field_not_content(self) -> None:
        """答案的密文字段是 ``data``；若误取 ``content`` 会拿错东西。"""
        good = wrap_cipher(json.dumps([answer_item([["A"]])]))
        decoy = wrap_cipher(
            json.dumps([{"answer": json.dumps({"children": [{"answers": ["Z"]}]})}])
        )
        body = {"code": 0, "k": K, "data": good, "content": decoy}
        answers = parsing.parse_answers(body)
        self.assertEqual(answers[0].children, (("A",),))

    def test_missing_answer_is_legal_for_oral_or_subjective(self) -> None:
        body = answer_response([answer_item(with_answer=False)]).json()
        answers = parsing.parse_answers(body)
        self.assertEqual(answers[0].children, ())

    def test_non_string_answer_element_raises(self) -> None:
        item = {"answer": json.dumps({"children": [{"answers": [1, 2]}]})}
        with self.assertRaises(UnipusProtocolError):
            parsing.parse_answers(answer_response([item]).json())

    def test_decrypted_non_array_raises(self) -> None:
        body = {"code": 0, "k": K, "data": wrap_cipher("{}")}
        with self.assertRaises(UnipusProtocolError):
            parsing.parse_answers(body)

    def test_order_preserved_no_sorting(self) -> None:
        body = answer_response([answer_item([["B", "A", "D"]])]).json()
        self.assertEqual(parsing.parse_answers(body)[0].children, (("B", "A", "D"),))


class TestParseGroupState(unittest.TestCase):
    def test_first_pass_datetime_decides_finished(self) -> None:
        never = parsing.parse_group_state(progress_response(first_pass=None).json())
        done = parsing.parse_group_state(progress_response(first_pass="2026-09-21 10:00:00").json())
        self.assertFalse(never.finished)
        self.assertTrue(done.finished)

    def test_server_state_alone_is_not_finished(self) -> None:
        """``state.state == 1`` 只表示"服务端有痕迹"，不是完成度。"""
        body = progress_response(
            first_pass=None, state={"state": 1, "score": [], "score_pct": 0}
        ).json()
        state = parsing.parse_group_state(body)
        self.assertEqual(state.server_state, 1)
        self.assertFalse(state.finished)

    def test_scores_and_thresholds(self) -> None:
        state = parsing.parse_group_state(progress_response().json())
        self.assertEqual(state.score, (1, 1, 0))
        self.assertEqual(state.score_pct, 0.66)
        self.assertTrue(state.flow_required)
        self.assertEqual(state.task_mini_score_pct, 0.6)
        self.assertEqual(state.last_submit, "5")

    def test_missing_data_raises(self) -> None:
        with self.assertRaises(UnipusProtocolError):
            parsing.parse_group_state({"code": 1, "data": None, "msg": "data not exist"})

    def test_empty_score_list(self) -> None:
        state = parsing.parse_group_state(
            progress_response(state={"state": 1, "score": []}).json()
        )
        self.assertEqual(state.score, ())


class TestParseRequiredLeafs(unittest.TestCase):
    def test_happy_path(self) -> None:
        body = {
            "code": 0,
            "rt": {"leafs": {"u1g1": {"strategies": {"required": True, "min_score_pct": 0.6}}}},
        }
        result = parsing.parse_required_leafs(body)
        self.assertFalse(result.missing)
        self.assertEqual(result.scope, parsing.SCOPE_UCONTENT_LEAFS)
        self.assertEqual(result.groups[0].group_id, "u1g1")
        self.assertTrue(result.groups[0].required)
        self.assertEqual(result.groups[0].min_score_pct, 0.6)

    def test_empty_leafs_means_missing_not_absent_requirement(self) -> None:
        for body in ({"code": 0, "rt": {"leafs": {}}}, {"code": 0, "rt": {}}, {"code": 0}):
            result = parsing.parse_required_leafs(body)
            self.assertTrue(result.missing, body)
            self.assertEqual(result.groups, ())

    def test_required_coercion(self) -> None:
        body = {
            "rt": {
                "leafs": {
                    "a": {"strategies": {"required": 1}},
                    "b": {"strategies": {"required": 0}},
                }
            }
        }
        result = parsing.parse_required_leafs(body)
        self.assertIs(result.groups[0].required, True)
        self.assertIs(result.groups[1].required, False)

    def test_unknown_required_stays_none(self) -> None:
        result = parsing.parse_required_leafs({"rt": {"leafs": {"a": {}}}})
        self.assertIsNone(result.groups[0].required)

    def test_leafs_of_wrong_type_raises_instead_of_looking_missing(self) -> None:
        with self.assertRaises(UnipusProtocolError):
            parsing.parse_required_leafs({"rt": {"leafs": ["u1g1"]}})

    def test_leafs_absent_is_missing_but_type_error_is_not(self) -> None:
        self.assertTrue(parsing.parse_required_leafs({"rt": {}}).missing)


class TestAssignmentIdConvention(unittest.TestCase):
    def test_roundtrip(self) -> None:
        packed = parsing.pack_assignment_id("course-v2:unipus+eng1", "u1g2")
        self.assertEqual(packed, "course-v2:unipus+eng1/u1g2")
        self.assertEqual(
            parsing.split_assignment_id(packed), ("course-v2:unipus+eng1", "u1g2")
        )

    def test_group_id_with_separator_is_rejected(self) -> None:
        with self.assertRaises(UnipusProtocolError):
            parsing.pack_assignment_id("c", "u/1g2")

    def test_bad_ids_raise(self) -> None:
        for bad in ("nogroup", "/x", "x/", ""):
            with self.assertRaises(UnipusProtocolError):
                parsing.split_assignment_id(bad)


# --------------------------------------------------------------------------
# 客户端：登录与响应处理
# --------------------------------------------------------------------------


class TestClientLogin(unittest.IsolatedAsyncioTestCase):
    async def test_happy_path_three_steps_four_requests(self) -> None:
        client, fake = make_client(
            sso_ticket_response("T1"), sso_validate_response("JWT-1"), user_info_response()
        )
        ok = await client.login("alice", "pw")
        self.assertTrue(ok)
        self.assertEqual(
            [call["url"] for call in fake.calls],
            [
                "https://sso.unipus.cn/sso/0.1/sso/cip/login",
                "https://sso.unipus.cn/sso/serviceTicket/validate"
                "?ticket=T1&service=https%3A%2F%2Fuai.unipus.cn%2Fportal",
                "https://uai.unipus.cn/api/account/user/info",
            ],
        )
        self.assertEqual(client.session.portal_token, "JWT-1")
        self.assertEqual(client.session.open_id, "900001")
        self.assertEqual(client.session.school, "1000")
        self.assertEqual(client.session.app_user_id, "555")

    async def test_login_body_carries_encrypted_uppercase_hex(self) -> None:
        client, fake = make_client(
            sso_ticket_response(), sso_validate_response(), user_info_response()
        )
        await client.login("alice", "pw")
        sent = json.loads(fake.calls[0]["body"].decode("utf-8"))
        self.assertEqual(sent["service"], endpoints.PORTAL_SERVICE)
        self.assertEqual(sent["username"], crypto.encrypt_login_field("alice"))
        self.assertEqual(sent["password"], crypto.encrypt_login_field("pw"))
        self.assertEqual(sent["username"], sent["username"].upper())

    async def test_cookie_continuity_between_step_one_and_two(self) -> None:
        """规格第 2.1 节：①②必须共用同一个带 cookie 的会话。"""
        login_with_cookie = Response(
            status=200,
            url="https://sso.unipus.cn/sso/0.1/sso/cip/login",
            body=json.dumps({"code": "0", "rs": {"serviceTicket": "T1"}}).encode(),
            headers={"Set-Cookie": "SESSION=abc123; Path=/"},
        )
        transport = SessionTransport(
            login_with_cookie, sso_validate_response(), user_info_response()
        )
        client, _ = make_client(transport=transport)
        self.assertTrue(await client.login("alice", "pw"))
        self.assertEqual(transport.jar, {"SESSION": "abc123"})
        self.assertIn("SESSION=abc123", transport.calls[1]["headers"].get("Cookie", ""))
        # 同一次登录的三步都打在同一个传输层实例上
        self.assertEqual(len(transport.calls), 3)

    async def test_login_rejected_by_business_code_returns_false(self) -> None:
        client, fake = make_client(json_response({"code": "1", "message": "用户名或密码错误"}))
        with self.assertLogs("kedaibiao.unipus", level="WARNING"):
            self.assertFalse(await client.login("alice", "bad"))
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(client.session.portal_token, "")

    async def test_login_rate_limited_code_is_retried_not_treated_as_bad_password(self) -> None:
        """限流不是"凭据错了"：必须上抛（可重试），不能返回 False。"""
        limited, _ = make_client(json_response({"code": 600001, "msg": "请求过于频繁"}))
        with self.assertRaises(UnipusRateLimited):
            await limited.login("alice", "pw")

    async def test_validate_step_failure_raises_not_false(self) -> None:
        """凭据过了但链路坏了：抛异常，不伪装成"密码错了"。"""
        client, _ = make_client(
            sso_ticket_response(), json_response({"code": "1", "msg": "ticket 无效"})
        )
        with self.assertRaises(UnipusApiError) as cm:
            await client.login("alice", "pw")
        self.assertIn("ticket 无效", str(cm.exception))

    async def test_missing_ticket_raises(self) -> None:
        client, _ = make_client(json_response({"code": "0", "rs": {}}))
        with self.assertRaises(UnipusProtocolError):
            await client.login("alice", "pw")

    async def test_missing_jwt_raises(self) -> None:
        client, _ = make_client(sso_ticket_response(), json_response({"code": "0", "rs": {}}))
        with self.assertRaises(UnipusProtocolError):
            await client.login("alice", "pw")

    async def test_step_three_business_failure_clears_session(self) -> None:
        client, _ = make_client(
            sso_ticket_response(), sso_validate_response(), json_response({"code": 0})
        )
        with self.assertRaises(UnipusApiError):
            await client.login("alice", "pw")
        self.assertEqual(client.session.portal_token, "")

    async def test_step_three_html_login_page_raises_auth_error(self) -> None:
        client, _ = make_client(
            sso_ticket_response(), sso_validate_response(), html_login_response(401)
        )
        with self.assertRaises(UnipusAuthError):
            await client.login("alice", "pw")
        self.assertEqual(client.session.portal_token, "")

    async def test_login_is_not_leaking_password_into_logs(self) -> None:
        client, _ = make_client(json_response({"code": "1", "message": "失败"}))
        with self.assertLogs("kedaibiao.unipus", level="WARNING") as captured:
            await client.login("alice", "topsecret")
        self.assertNotIn("topsecret", "\n".join(captured.output))


class TestClientResponses(unittest.IsolatedAsyncioTestCase):
    async def test_401_html_becomes_auth_error_with_login_hint(self) -> None:
        client, _ = make_client(html_login_response(401))
        with self.assertRaises(UnipusAuthError) as cm:
            await client.fetch_course_bindings()
        self.assertIn("登录页", str(cm.exception))

    async def test_status_error_without_html_carries_status_and_preview(self) -> None:
        client, _ = make_client(raw_response('{"boom":1}', status=503))
        with self.assertRaises(UnipusHttpError) as cm:
            await client.fetch_course_bindings()
        self.assertEqual(cm.exception.status, 503)
        self.assertIn("boom", cm.exception.body_preview)

    async def test_200_html_is_not_treated_as_json(self) -> None:
        client, _ = make_client(html_login_response(200))
        with self.assertRaises(UnipusAuthError):
            await client.fetch_course_bindings()

    async def test_non_json_body_raises_protocol_error(self) -> None:
        client, _ = make_client(raw_response("not json at all"))
        with self.assertRaises(UnipusProtocolError) as cm:
            await client.fetch_course_bindings()
        self.assertIn("not json at all", str(cm.exception))

    async def test_empty_body_raises_protocol_error(self) -> None:
        client, _ = make_client(raw_response(""))
        with self.assertRaises(UnipusProtocolError):
            await client.fetch_course_bindings()

    async def test_business_failure_raises_with_message_and_code(self) -> None:
        client, _ = make_client(json_response({"code": 6011, "msg": "feature denied"}))
        with self.assertRaises(UnipusApiError) as cm:
            await client.fetch_course_bindings()
        self.assertEqual(cm.exception.code, 6011)
        self.assertIn("feature denied", str(cm.exception))
        self.assertEqual(cm.exception.app, "portal")

    async def test_rate_limit_is_retryable_and_carries_backoff(self) -> None:
        from kedaibiao.core.scheduler import RETRYABLE

        client, _ = make_client(json_response({"code": 600002, "msg": "太频繁"}))
        error: UnipusRateLimited | None = None
        try:
            await client.fetch_course_bindings()
        except UnipusRateLimited as exc:
            error = exc
        assert error is not None
        self.assertEqual(error.retry_after, 20.0)
        self.assertEqual(error.suggested_backoff, 20.0)
        self.assertTrue(isinstance(error, TransportError))
        self.assertTrue(isinstance(error, UnipusApiError))
        self.assertTrue(any(isinstance(error, kind) for kind in RETRYABLE))

    def test_rate_limit_backoff_follows_spec_formula(self) -> None:
        """尝试序号从 1 起 → 20/40/60/80（规格第 9 节）。"""
        values = [
            UnipusRateLimited(
                app="portal", url="u", message="m", code=600001, attempt=i
            ).retry_after
            for i in (1, 2, 3, 4)
        ]
        self.assertEqual(values, [20.0, 40.0, 60.0, 80.0])
        self.assertEqual(UnipusRateLimited.MAX_ATTEMPTS, 4)

    def test_rate_limit_backoff_clamps_bad_attempt_numbers(self) -> None:
        for attempt in (0, -3, None):
            error = UnipusRateLimited(
                app="portal", url="u", message="m", code=600001, attempt=attempt
            )
            self.assertEqual(error.retry_after, 20.0, attempt)
            self.assertEqual(error.attempt_number, 1)

    def test_rate_limit_attempt_channel_is_used_by_default(self) -> None:
        """不传 attempt 时读调度层上下文；不在 Scheduler 内 → 下限 1 → 20s。"""
        from kedaibiao.core.scheduler import current_attempt

        self.assertEqual(current_attempt(), 0)
        error = UnipusRateLimited(app="portal", url="u", message="m", code=600002)
        self.assertIsNone(error.attempt)
        self.assertEqual(error.retry_after, 20.0)

    async def test_consecutive_rate_limits_back_off_20_then_40(self) -> None:
        """端到端：同一个 Scheduler.run 里连续两次限流 → 20s、40s。"""
        import asyncio
        from unittest import mock

        from kedaibiao.core.scheduler import RetryPolicy, Scheduler

        client, _ = make_client(
            json_response({"code": 600002, "msg": "太频繁"}),
            json_response({"code": 600002, "msg": "太频繁"}),
            json_response({"code": 600002, "msg": "太频繁"}),
        )
        scheduler = Scheduler(max_concurrency=1)
        slept: list[float] = []
        hints: list[float] = []

        async def fake_sleep(delay: float) -> None:
            slept.append(delay)

        def on_retry(attempt: int, exc: BaseException) -> None:
            hints.append(exc.retry_after)  # type: ignore[attr-defined]

        with mock.patch.object(asyncio, "sleep", fake_sleep):
            with self.assertRaises(UnipusRateLimited) as cm:
                await scheduler.run(
                    client.fetch_course_bindings,
                    key="unipus",
                    policy=RetryPolicy(attempts=3, base_delay=0.1, jitter=False),
                    on_retry=on_retry,
                )
        self.assertEqual(hints, [20.0, 40.0])
        self.assertEqual(slept, [20.0, 40.0])
        # 最后一次尝试（第 3 次）在上下文复位后才抛出，但值已在 __init__ 里算好
        self.assertEqual(cm.exception.retry_after, 60.0)

    async def test_rate_limit_600001_also_detected(self) -> None:
        client, _ = make_client(json_response({"code": 600001}))
        with self.assertRaises(UnipusRateLimited):
            await client.fetch_course_bindings()

    async def test_transport_error_propagates_for_scheduler(self) -> None:
        client, _ = make_client(TransportError("connection reset"))
        with self.assertRaises(TransportError):
            await client.fetch_course_bindings()

    async def test_headers_are_selected_by_app(self) -> None:
        client, fake = make_client(
            json_response({"code": 1, "value": {"courseList": []}}),
            json_response({"code": 0, "course": "[]"}),
        )
        await client.fetch_course_bindings()
        portal_headers = fake.calls[0]["headers"]
        self.assertEqual(portal_headers[endpoints.PORTAL_AUTH_HEADER], "PORTAL-JWT")
        self.assertNotIn(endpoints.UCONTENT_AUTH_HEADER, portal_headers)

        await client.fetch_catalog("course-v2:x")
        ucontent_headers = fake.calls[1]["headers"]
        self.assertNotIn(endpoints.PORTAL_AUTH_HEADER, ucontent_headers)
        token = ucontent_headers[endpoints.UCONTENT_AUTH_HEADER]
        self.assertEqual(token, crypto.build_annotator_token("900001", now_ms=NOW_MS))

    async def test_host_headers_only_when_asked(self) -> None:
        client, fake = make_client(json_response({"code": 1, "data": {"ssoId": "9"}}))
        await client.request(
            UnipusApp.PORTAL,
            "POST",
            endpoints.PORTAL_PLAN_DETAIL,
            family=UnipusApp.PORTAL.value,
            payload={"courseInstanceId": 1},
            host_headers=True,
        )
        headers = fake.calls[0]["headers"]
        self.assertEqual(headers[endpoints.HOST_HEADER_APP_ID], "116")
        self.assertEqual(headers[endpoints.HOST_HEADER_PLATFORM], "2")
        self.assertEqual(headers[endpoints.HOST_HEADER_SCHOOL], "1000")
        self.assertEqual(headers[endpoints.HOST_HEADER_OPENID], "900001")
        self.assertEqual(headers["Content-Type"], "application/json")

    async def test_portal_request_without_token_raises(self) -> None:
        client, fake = make_client(json_response({"code": 1}))
        client.session.clear()
        with self.assertRaises(UnipusNotLoggedIn):
            await client.fetch_course_bindings()
        self.assertEqual(fake.calls, [])

    async def test_ucontent_request_without_open_id_raises_before_sending(self) -> None:
        client, fake = make_client(json_response({"code": 0}))
        client.session.open_id = ""
        with self.assertRaises(UnipusProtocolError):
            await client.fetch_catalog("course-v2:x")
        self.assertEqual(fake.calls, [])

    async def test_request_timeout_is_thirty_seconds(self) -> None:
        client, fake = make_client(json_response({"code": 1, "value": {"courseList": []}}))
        await client.fetch_course_bindings()
        self.assertEqual(fake.calls[0]["timeout"], 30.0)
        self.assertEqual(REQUEST_TIMEOUT_SECONDS, 30.0)

    async def test_is_logged_in_without_token_makes_no_request(self) -> None:
        client, fake = make_client()
        client.session.clear()
        self.assertFalse(await client.is_logged_in())
        self.assertEqual(fake.calls, [])

    async def test_is_logged_in_true_and_false(self) -> None:
        client, _ = make_client(user_info_response())
        self.assertTrue(await client.is_logged_in())

        stale, _ = make_client(html_login_response(401))
        self.assertFalse(await stale.is_logged_in())

        failing, _ = make_client(json_response({"code": 0}))
        self.assertFalse(await failing.is_logged_in())

    async def test_all_urls_stay_on_platform_hosts(self) -> None:
        client, fake = make_client(
            json_response({"code": 1, "value": {"courseList": [make_class()]}}),
            catalog_response(),
            content_response([content_item()]),
            answer_response([answer_item([["A"]])]),
            progress_response(),
            json_response(
                {"code": 0, "rt": {"leafs": {"u1g1": {"strategies": {"required": True}}}}}
            ),
        )
        await client.fetch_course_bindings()
        await client.fetch_catalog("course-v2:x")
        await client.fetch_content("course-v2:x", "u1g1")
        await client.fetch_answers("course-v2:x", "u1g1")
        await client.fetch_group_state("course-v2:x", "u1g1")
        await client.fetch_required_leafs("course-v2:x", ("u1g1", "u1g2"))
        allowed = tuple(endpoints.BASE_URLS.values())
        for call in fake.calls:
            self.assertTrue(call["url"].startswith(allowed), call["url"])

    async def test_required_leafs_query_is_comma_separated_and_has_open_id(self) -> None:
        client, fake = make_client(json_response({"code": 0, "rt": {"leafs": {}}}))
        await client.fetch_required_leafs("course-v2:x", ("u1g1", "u1g2"))
        self.assertIn(
            "/course/api/v2/course_progress/course-v2:x/tasks/900001/default",
            fake.calls[0]["url"],
        )
        self.assertIn("tasks=u1g1,u1g2", fake.calls[0]["url"])

    async def test_content_business_error_is_not_swallowed(self) -> None:
        client, _ = make_client(json_response({"code": 2, "message": "空壳（courseId 被编码）"}))
        with self.assertRaises(UnipusApiError) as cm:
            await client.fetch_content("course-v2:x", "u1g1")
        self.assertEqual(cm.exception.code, 2)


# --------------------------------------------------------------------------
# 适配器：契约、翻译、端到端
# --------------------------------------------------------------------------


class TestAdapterContract(unittest.IsolatedAsyncioTestCase):
    def test_name_and_capabilities(self) -> None:
        adapter = UnipusAdapter()
        self.assertEqual(adapter.name, "unipus")
        self.assertEqual(adapter.display_name, "U校园（Unipus）")
        self.assertTrue(adapter.capabilities.login)
        self.assertTrue(adapter.capabilities.list_courses)
        self.assertTrue(adapter.capabilities.list_assignments)
        self.assertTrue(adapter.capabilities.fetch_questions)
        self.assertFalse(adapter.capabilities.submit)
        self.assertFalse(adapter.capabilities.course_playback)

    async def test_submit_is_not_supported(self) -> None:
        adapter = UnipusAdapter()
        assignment = Assignment(remote_id="course-v2:x/u1g1", title="t")
        with self.assertRaises(NotSupportedError):
            await adapter.submit(assignment, [])

    async def test_missing_http_client_fails_loudly(self) -> None:
        adapter = UnipusAdapter(AdapterContext())
        with self.assertRaises(RuntimeError):
            await adapter.list_courses()
        with self.assertRaises(RuntimeError):
            await adapter.login(Credentials(username="u", password="p"))

    async def test_is_logged_in_without_login_is_false_and_offline(self) -> None:
        adapter, fake = make_adapter()
        adapter._client = None  # noqa: SLF001 - 验证未登录时不发请求
        self.assertFalse(await adapter.is_logged_in())
        self.assertEqual(fake.calls, [])

    async def test_empty_username_raises(self) -> None:
        adapter, _ = make_adapter()
        with self.assertRaises(UnipusProtocolError):
            await adapter.login(Credentials(username="", password="p"))

    async def test_login_delegates(self) -> None:
        adapter, fake = make_adapter(
            sso_ticket_response(), sso_validate_response(), user_info_response()
        )
        self.assertTrue(await adapter.login(Credentials(username="alice", password="pw")))
        self.assertEqual(len(fake.calls), 3)
        self.assertEqual(adapter.client().session.open_id, "900001")

    async def test_logger_is_child_of_root(self) -> None:
        adapter = UnipusAdapter()
        self.assertEqual(adapter.log.name, "kedaibiao.unipus")


class TestAdapterCourses(unittest.IsolatedAsyncioTestCase):
    async def test_list_courses_maps_fields(self) -> None:
        adapter, fake = make_adapter(
            course_list_response([make_class()]),
            json_response(
                {
                    "code": 1,
                    "data": {
                        "classList": [
                            {"courseResourceList": [make_book() | {"activation": 0}]}
                        ]
                    },
                }
            ),
        )
        courses = await adapter.list_courses()
        self.assertEqual(len(courses), 1)
        course = courses[0]
        self.assertEqual(course.remote_id, "70001")
        self.assertEqual(course.name, "英语1班 · 新视野大学英语1")
        self.assertEqual(course.teacher, "")
        self.assertEqual(course.term, "")
        self.assertEqual(course.raw["course_instance_id"], "course-v2:unipus+eng1+2026_T1")
        self.assertEqual(course.raw["resource_id"], "course-v1:unipus+eng1+2026")
        self.assertEqual(course.raw["strategy_id"], "88001")
        self.assertEqual(course.raw["finish_point_num"], 3)
        self.assertEqual(course.raw["total_point_num"], 10)
        self.assertIs(course.raw["activated"], True)
        self.assertEqual(len(fake.calls), 2)

    async def test_list_courses_without_bookshelf_sends_one_request(self) -> None:
        adapter, fake = make_adapter(course_list_response([make_class()]))
        courses = await adapter.list_courses(with_bookshelf=False)
        self.assertEqual(len(fake.calls), 1)
        self.assertIsNone(courses[0].raw["activated"])

    async def test_bookshelf_recovers_missing_resource_id(self) -> None:
        book = make_book(resource_id="")
        adapter, _ = make_adapter(
            course_list_response([make_class(books=[book])]),
            json_response(
                {
                    "code": 1,
                    "data": {
                        "classList": [{"courseResourceList": [make_book() | {"activation": 0}]}]
                    },
                }
            ),
        )
        courses = await adapter.list_courses()
        self.assertEqual(courses[0].raw["resource_id"], "course-v1:unipus+eng1+2026")

    async def test_bookshelf_error_is_not_swallowed(self) -> None:
        adapter, _ = make_adapter(
            course_list_response([make_class()]), json_response({"code": 0, "msg": "书架上挂了"})
        )
        with self.assertRaises(UnipusApiError):
            await adapter.list_courses()

    async def test_course_name_falls_back_to_remote_id(self) -> None:
        book = make_book(name="")
        adapter, _ = make_adapter(course_list_response([make_class(name="", books=[book])]))
        courses = await adapter.list_courses(with_bookshelf=False)
        self.assertEqual(courses[0].name, "70001")


class TestAdapterAssignments(unittest.IsolatedAsyncioTestCase):
    async def _courses(self, adapter: UnipusAdapter) -> Course:
        return (await adapter.list_courses(with_bookshelf=False))[0]

    async def test_list_assignments_from_catalog(self) -> None:
        adapter, fake = make_adapter(
            course_list_response([make_class()]),
            catalog_response(),
        )
        course = await self._courses(adapter)
        assignments = await adapter.list_assignments(course)
        self.assertEqual(
            [item.remote_id for item in assignments],
            [
                "course-v2:unipus+eng1+2026_T1/u1g1",
                "course-v2:unipus+eng1+2026_T1/u1g2",
                "course-v2:unipus+eng1+2026_T1/u2g1",
            ],
        )
        self.assertEqual(assignments[0].title, "Listening")
        self.assertEqual(assignments[1].title, "u1g2")
        self.assertEqual(assignments[0].course_name, course.name)
        self.assertEqual(assignments[0].kind.value, "homework")
        self.assertTrue(assignments[0].raw["kind_unconfirmed"])
        self.assertTrue(assignments[0].raw["state_unconfirmed"])
        self.assertEqual(assignments[0].raw["unit_id"], "u1")
        self.assertEqual(assignments[0].raw["unit_name"], "Unit 1")
        self.assertEqual(assignments[0].raw["group_id"], "u1g1")
        self.assertFalse(assignments[0].finished)
        self.assertEqual(assignments[0].deadline, None)
        self.assertEqual(assignments[0].url, "")
        # 目录请求的 URL 必须保留冒号（不能 URL 编码）
        self.assertEqual(
            fake.calls[1]["url"],
            "https://ucontent.unipus.cn/course/api/course/course-v2:unipus+eng1+2026_T1/default",
        )

    async def test_list_assignments_with_state_fills_progress_and_finished(self) -> None:
        adapter, fake = make_adapter(
            course_list_response([make_class()]),
            catalog_response(),
            progress_response(first_pass="2026-09-21 09:00:00"),
            progress_response(first_pass=None, state={"state": 1, "score": [], "score_pct": 0}),
            progress_response(first_pass=None, state={"state": 0, "score_pct": 0.5}),
        )
        course = await self._courses(adapter)
        assignments = await adapter.list_assignments(course, with_state=True)
        self.assertTrue(assignments[0].finished)
        self.assertEqual(assignments[0].progress, 0.66)
        self.assertFalse(assignments[1].finished)
        self.assertFalse(assignments[2].finished)
        self.assertEqual(assignments[2].progress, 0.5)
        self.assertNotIn("state_unconfirmed", assignments[0].raw)
        self.assertIn("first_pass_datetime", assignments[0].raw)
        # 目录 1 次 + 每题 1 次
        self.assertEqual(len(fake.calls), 2 + 3)

    async def test_missing_instance_id_in_raw_raises(self) -> None:
        adapter, _ = make_adapter()
        broken = Course(remote_id="70001", name="x", raw={})
        with self.assertRaises(UnipusProtocolError):
            await adapter.list_assignments(broken)

    async def test_fetch_required_groups_uses_comma_separated_ids(self) -> None:
        adapter, fake = make_adapter(
            course_list_response([make_class()]),
            catalog_response(),
            json_response(
                {"code": 0, "rt": {"leafs": {"u1g1": {"strategies": {"required": True}}}}}
            ),
        )
        course = await self._courses(adapter)
        result = await adapter.fetch_required_groups(course)
        self.assertFalse(result.missing)
        self.assertEqual(result.scope, "ucontent_leafs")
        self.assertIn("tasks=u1g1,u1g2,u2g1", fake.calls[2]["url"])

    async def test_missing_leafs_is_reported_as_missing(self) -> None:
        adapter, _ = make_adapter(
            course_list_response([make_class()]),
            catalog_response(),
            json_response({"code": 0, "rt": {"leafs": {}}}),
        )
        course = await self._courses(adapter)
        result = await adapter.fetch_required_groups(course)
        self.assertTrue(result.missing)

    async def test_empty_catalog_reports_missing_without_extra_request(self) -> None:
        adapter, fake = make_adapter(course_list_response([make_class()]), catalog_response([]))
        course = await self._courses(adapter)
        result = await adapter.fetch_required_groups(course)
        self.assertTrue(result.missing)
        self.assertEqual(len(fake.calls), 2)

    async def test_fetch_group_state(self) -> None:
        adapter, fake = make_adapter(progress_response(first_pass="2026-09-21"))
        assignment = Assignment(remote_id="course-v2:x/u1g1", title="t")
        state = await adapter.fetch_group_state(assignment)
        self.assertTrue(state.finished)
        self.assertIn("/api/mobile/user_module/course-v2:x/u1g1/progress/v2", fake.calls[0]["url"])


class TestAdapterQuestions(unittest.IsolatedAsyncioTestCase):
    BIG_ID = "1976531090427650421"

    async def test_fetch_questions_mapping_and_raw(self) -> None:
        inner = inner_for_choice(reply_type="singlechoice", category=1)
        adapter, _ = make_adapter(
            content_response(
                [
                    content_item(item_id=self.BIG_ID, inner=inner),
                    content_item(
                        item_id="1976531090427650002",
                        inner={
                            "replyType": "multichoice",
                            "type": "choice",
                            "category": 1,
                            "children": [{}, {}],
                        },
                    ),
                    content_item(
                        item_id="1976531090427650003",
                        inner={
                            "replyType": "text-area",
                            "type": "essay",
                            "category": 8,
                            "children": [],
                        },
                    ),
                ]
            )
        )
        assignment = Assignment(remote_id="course-v2:x/u1g1", title="t")
        questions = await adapter.fetch_questions(assignment)
        self.assertEqual(len(questions), 3)
        self.assertEqual(
            [question.remote_id for question in questions],
            [self.BIG_ID, "1976531090427650002", "1976531090427650003"],
        )
        self.assertEqual(
            [question.type for question in questions],
            [QuestionType.SINGLE, QuestionType.MULTIPLE, QuestionType.ESSAY],
        )
        self.assertEqual([question.stem for question in questions], ["", "", ""])
        self.assertEqual([question.options for question in questions], [[], [], []])
        self.assertTrue(all(question.raw["stem_unavailable"] for question in questions))
        self.assertEqual(questions[0].raw["unipus_type"], "single-choice")
        self.assertEqual(questions[0].raw["inner"], inner)
        self.assertEqual(questions[0].raw["media_types"], [1, 4])
        self.assertEqual(questions[1].raw["item_count"], 2)

    async def test_questions_are_identifiable_through_remote_id(self) -> None:
        """题面不可得，但 ID 必须能让题目入库且指纹互不相同。"""
        adapter, _ = make_adapter(
            content_response(
                [
                    content_item(item_id="1976531090427650001"),
                    content_item(item_id="1976531090427650002"),
                ]
            )
        )
        questions = await adapter.fetch_questions(
            Assignment(remote_id="course-v2:x/u1g1", title="t")
        )
        self.assertTrue(all(question.is_identifiable for question in questions))
        self.assertNotEqual(questions[0].fingerprint, questions[1].fingerprint)

    async def test_fetch_questions_rejects_bad_assignment_id(self) -> None:
        adapter, _ = make_adapter()
        with self.assertRaises(UnipusProtocolError):
            await adapter.fetch_questions(Assignment(remote_id="broken", title="t"))

    async def test_fetch_standard_answers_matches_count(self) -> None:
        adapter, fake = make_adapter(
            answer_response([answer_item([["A"]]), answer_item([["B"]])])
        )
        assignment = Assignment(remote_id="course-v2:x/u1g1", title="t")
        answers = await adapter.fetch_standard_answers(assignment, expected_count=2)
        self.assertEqual([item.children for item in answers], [(("A",),), (("B",),)])
        self.assertIn("/course/api/v3/answer/course-v2:x/u1g1/default", fake.calls[0]["url"])

    async def test_fetch_standard_answers_count_mismatch_fails(self) -> None:
        adapter, _ = make_adapter(answer_response([answer_item([["A"]])]))
        assignment = Assignment(remote_id="course-v2:x/u1g1", title="t")
        with self.assertRaises(UnipusProtocolError) as cm:
            await adapter.fetch_standard_answers(assignment, expected_count=3)
        self.assertIn("不可提交", str(cm.exception))

    async def test_fetch_standard_answers_checks_per_question_child_counts(self) -> None:
        """规格第 11.2 节第 2 条：内容侧子题数与答案侧子题数必须自己校验。"""
        adapter, _ = make_adapter(
            answer_response([answer_item([["A"], ["B"]]), answer_item([["C"]])])
        )
        assignment = Assignment(remote_id="course-v2:x/u1g1", title="t")
        answers = await adapter.fetch_standard_answers(
            assignment, expected_count=2, expected_child_counts=[2, 1]
        )
        self.assertEqual(len(answers), 2)

    async def test_fetch_standard_answers_child_count_mismatch_fails(self) -> None:
        adapter, _ = make_adapter(answer_response([answer_item([["A"]])]))
        assignment = Assignment(remote_id="course-v2:x/u1g1", title="t")
        with self.assertRaises(UnipusProtocolError) as cm:
            await adapter.fetch_standard_answers(assignment, expected_child_counts=[3])
        self.assertIn("不可提交", str(cm.exception))

    async def test_fetch_standard_answers_child_count_arity_mismatch_fails(self) -> None:
        adapter, _ = make_adapter(answer_response([answer_item([["A"]])]))
        assignment = Assignment(remote_id="course-v2:x/u1g1", title="t")
        with self.assertRaises(UnipusProtocolError):
            await adapter.fetch_standard_answers(assignment, expected_child_counts=[1, 1])

    async def test_fetch_questions_does_not_touch_answer_endpoint(self) -> None:
        adapter, fake = make_adapter(content_response([content_item()]))
        await adapter.fetch_questions(Assignment(remote_id="course-v2:x/u1g1", title="t"))
        self.assertEqual(len(fake.calls), 1)
        self.assertIn("/content/", fake.calls[0]["url"])
        self.assertNotIn("/answer/", fake.calls[0]["url"])


if __name__ == "__main__":
    unittest.main()
