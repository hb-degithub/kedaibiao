"""批改网 client 层离线单测：标准库 RSA + 注入式 ``HttpClient``。

**全程离线**：没有任何测试发真实请求 —— 传输层一律是 ``tests/test_net.py`` 的
``FakeTransport``（这里再包一层，加上 ``.cookies`` 这个 ``UrllibTransport`` 的
duck-typing 属性，用来验证「登录成功判据要看 cookie jar」这条分支）。

覆盖：
  * RSA：公钥解析、密文 128 字节 / base64 172 字符、填充结构（``00 02`` 前缀、
    非零 PS、``00`` 分隔符）、明文过长抛 ``ValueError``、同明文两次结果不同；
  * 客户端：登录成功 / 三种失败分支、HTTP 失败、``is_logged_in`` 探针不缓存、
    ``get`` 只收站内路径、异常与日志里不出现密码或密文、构造签名只有注入项。
"""

from __future__ import annotations

import ast
import asyncio
import base64
import http.cookiejar
import inspect
import logging
import re
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

from kedaibiao.net.http import HttpClient, Response, TransportError
from kedaibiao.platforms.pigai import protocol, rsa_login
from kedaibiao.platforms.pigai.client import (
    PigaiClient,
    PigaiError,
    PigaiResponseError,
)
from tests.test_net import FakeTransport
_CLIENT_PATH = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "kedaibiao"
    / "platforms"
    / "pigai"
    / "client.py"
)

#: 测试用口令：任何日志/异常里出现它就说明脱敏失败
_PASSWORD = "Hu7!Secret-P@ssw0rd-2026"  # noqa: secret-check - 故意的脱敏哨兵
#: 测试用 cookie 值：同上，只允许出现 cookie 名字
_COOKIE_SECRET = "juku-cookie-secret-value"  # noqa: secret-check - 故意的脱敏哨兵

_USER = "student@example.com"

#: 匿名首页（含登录表单标识，即 protocol.LOGIN_FORM_MARKER）
_ANON_HOME = (
    '<html><head><title>批改网</title></head><body>'
    '<form method="post" action="/index.php?a=login" id="lg_from_w">'
    '<input type="text" name="username" id="username">'
    "</form></body></html>"
)
#: 登录后的首页（不含登录表单标识）
_LOGGED_IN_HOME = "<html><head><title>批改网</title></head><body>我的作业</body></html>"
#: 合法页：只在**脚本文案**里提到裸串 ``lg_from_w``（本站 ``2020.js`` 就是这个写法），
#: 但没有 ``id="lg_from_w"`` 属性 —— 用来验证严格判据不会把正常页面误判成未登录。
_SCRIPT_MENTIONS_MARKER = (
    "<html><head><title>批改网</title></head><body>我的作业"
    "<script>var submitLogin = function () { $('#lg_from_w').submit(); };</script>"
    "</body></html>"
)
#: 200 的「Internal Error」错误页（验证者 B3b 场景）：不含登录表单，但绝不是合法页
_ERROR_PAGE_200 = (
    "<html><head><title>500 Internal Error</title></head>"
    "<body>Internal Error</body></html>"
)

# ---- 以下形态照 data/recon/pigai/raw/ 的真实样本构造（内联，不复制样本文件）----

#: 会话失效页（对应 ``b_anon_write.html``）：200、**不含**登录表单标识、也**不含**列表容器，
#: 正文「使用此功能，请先登录」+ JS 跳 ``?a=loginForm``。
#: ⚠ 它正是「页面不含登录表单」这条**否定**判据没有鉴别力的证据。
_ANON_EXPIRED_WRITE = (
    "<html><head><title>句酷批改网</title></head><body>使用此功能，请先登录"
    " 3 秒后系统将自动跳转......"
    '<script>var stime=3; var url="/index.php?a=loginForm&reurl=%2Findex.php%3Fc%3Dwrite";</script>'
    "</body></html>"
)
#: 已登录列表页（对应 ``b_index_php_c_write.html``）：含 ``<div id="essayList">``
_LOGGED_IN_WRITE = (
    "<html><head><title>批改网</title>"
    "<style>#essayList .select{background:#fff}</style></head><body>"
    '<div id="essayList" class="ydTable">'
    '<ul class="reItemM"><li style="width:80px;">作文ID</li></ul>'
    '<ul class="reItemS"><li style="width:80px;">1000002</li></ul>'
    "</div></body></html>"
)
#: 空列表页：只有表头但**仍含**容器（对应 parser fixture 的空列表形态）
_EMPTY_LIST_WRITE = (
    "<html><head><title>批改网</title></head><body>"
    '<div id="essayList" class="ydTable">'
    '<ul class="reItemM"><li style="width:80px;">作业号</li></ul>'
    '<div class="line" style="height:1px;"></div>'
    "</div></body></html>"
)
#: 只在脚本文案里提到 ``#essayList``、但没有容器属性 —— 正面标记也必须用属性形态
_SCRIPT_MENTIONS_CONTAINER = (
    "<html><head><title>批改网</title></head><body>我的作业"
    "<script>var hideList = function () { $('#essayList').hide(); };</script>"
    "</body></html>"
)

_REAL_N, _REAL_E = rsa_login.parse_spki_public_key(protocol.RSA_PUBKEY_B64)
#: 退化测试钥匙：位长 1024（→ k=128）且 e=1，于是 pow(EM,1,n)=EM，
#: 可以直接把填充后的 EM 原样取回来验证结构（只有测试会这么用）。
_TEST_N = (1 << 1023) + 1
_TEST_E = 1


# --------------------------------------------------------------------------
# 假传输层 / 工具
# --------------------------------------------------------------------------


class _JarTransport(FakeTransport):
    """``FakeTransport`` + ``UrllibTransport`` 那样的 ``.cookies`` / ``cookie_header()``。

    ``set_cookie_on_post`` 模拟「服务端在登录 POST 的响应里下发主登录态 cookie」。
    """

    def __init__(self, *responses: Response, set_cookie_on_post=None, with_jar: bool = True) -> None:
        super().__init__(*responses)
        self.cookies = http.cookiejar.CookieJar() if with_jar else None
        self.set_cookie_on_post = set_cookie_on_post

    async def request(self, method, url, *, headers=None, body=None, timeout=15.0):
        if method.upper() == "POST" and self.set_cookie_on_post is not None:
            self.cookies.set_cookie(self.set_cookie_on_post)
        return await super().request(method, url, headers=headers, body=body, timeout=timeout)


class _FailingTransport(FakeTransport):
    """每次请求都抛传输层错误（模拟断网/超时），用于验证探针的保守取向。"""

    async def request(self, method, url, *, headers=None, body=None, timeout=15.0):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": headers or {},
                "body": body,
                "timeout": timeout,
            }
        )
        raise TransportError("模拟网络不可达")


def _resp(html: str, *, status: int = 200, url: str = "") -> Response:
    return Response(
        status=status,
        url=url or protocol.BASE_URL + protocol.Endpoints.HOME,
        body=html.encode("utf-8"),
    )


def _cookie(name: str, value: str) -> http.cookiejar.Cookie:
    return http.cookiejar.Cookie(
        version=0,
        name=name,
        value=value,
        port=None,
        port_specified=False,
        domain=".pigai.org",
        domain_specified=True,
        domain_initial_dot=True,
        path="/",
        path_specified=True,
        secure=False,
        expires=None,
        discard=False,
        comment=None,
        comment_url=None,
        rest={},
        rfc2109=False,
    )


def _logger(name: str) -> logging.Logger:
    """每个用例一个独立 logger；不向上传播，免得日志噪音混进测试输出。"""
    log = logging.getLogger(f"kedaibiao.test.pigai.{name}")
    log.propagate = False
    log.addHandler(logging.NullHandler())
    return log


def _client(*responses: Response, set_cookie_on_post=None, with_jar: bool = True, logger=None):
    transport = _JarTransport(
        *responses, set_cookie_on_post=set_cookie_on_post, with_jar=with_jar
    )
    return PigaiClient(HttpClient(transport, timeout=5.0), logger=logger), transport


def _seed_login_cookie(transport: _JarTransport) -> None:
    """模拟「已登录」：往 jar 里放主登录态 cookie（只放名字与值，不产生请求）。"""
    transport.cookies.set_cookie(_cookie(protocol.LOGIN_SUCCESS_COOKIE, _COOKIE_SECRET))


def _em_from_degenerate_key(message: bytes) -> bytes:
    """用 e=1 的测试钥匙把填充后的 EM 原样取回（用于验证填充结构）。"""
    return base64.b64decode(rsa_login.pkcs1v15_encrypt(message, _TEST_N, _TEST_E))


def _assert_no_b64_blob(test: unittest.TestCase, text: str) -> None:
    """断言文本里没有 80+ 字符的 base64 片段（密文 172 字符，一旦进日志就会撞上）。"""
    test.assertIsNone(
        re.search(r"[A-Za-z0-9+/]{80,}={0,2}", text), f"疑似密文泄漏进日志/异常: {text[:120]!r}"
    )


# --------------------------------------------------------------------------
# RSA
# --------------------------------------------------------------------------


class TestPublicKeyParsing(unittest.TestCase):
    def test_frozen_key_is_rsa1024_e65537(self) -> None:
        n, e = rsa_login.parse_spki_public_key(protocol.RSA_PUBKEY_B64)
        self.assertEqual(n.bit_length(), 1024)
        self.assertEqual(e, 65537)
        self.assertEqual((n.bit_length() + 7) // 8, 128)

    def test_pem_style_whitespace_is_tolerated(self) -> None:
        wrapped = "\n".join(
            protocol.RSA_PUBKEY_B64[i : i + 64] for i in range(0, len(protocol.RSA_PUBKEY_B64), 64)
        )
        self.assertEqual(
            rsa_login.parse_spki_public_key(wrapped),
            rsa_login.parse_spki_public_key(protocol.RSA_PUBKEY_B64),
        )

    def test_rejects_invalid_input(self) -> None:
        for bad in ("", "!!!not-base64!!!", "   "):
            with self.assertRaises(ValueError):
                rsa_login.parse_spki_public_key(bad)
        truncated = base64.b64encode(b"\x30\x03\x02\x01").decode()
        with self.assertRaises(ValueError):
            rsa_login.parse_spki_public_key(truncated)
        with self.assertRaises(ValueError):
            rsa_login.parse_spki_public_key(123)  # type: ignore[arg-type]


class TestPkcs1v15(unittest.TestCase):
    def test_real_key_ciphertext_is_128_bytes_172_chars(self) -> None:
        out = rsa_login.pkcs1v15_encrypt(b"correct horse battery", _REAL_N, _REAL_E)
        self.assertEqual(len(out), 172)
        self.assertEqual(len(base64.b64decode(out)), 128)

    def test_padding_structure_prefix_separator_and_message(self) -> None:
        message = b"hunter2-secret"
        em = _em_from_degenerate_key(message)
        self.assertEqual(len(em), 128)
        self.assertEqual(em[:2], b"\x00\x02", "PKCS#1 v1.5 前缀必须是 00 02")
        ps_len = 128 - 3 - len(message)
        self.assertEqual(em[2 + ps_len], 0x00, "PS 与明文之间必须有一个 00 分隔符")
        self.assertEqual(em[2 + ps_len + 1 :], message)
        self.assertEqual(len(em[2 : 2 + ps_len]), ps_len)

    def test_padding_bytes_are_never_zero(self) -> None:
        """复刻 JSEncrypt pkcs1pad2：填充字节取到 0 要重试。30 轮足以抓住「忘了重试」。"""
        for _ in range(30):
            em = _em_from_degenerate_key(b"x")
            ps = em[2 : 128 - 1 - 1]
            self.assertEqual(len(ps), 124)
            self.assertTrue(all(byte != 0 for byte in ps), "填充里出现了 0x00")

    def test_random_padding_makes_ciphertext_unique(self) -> None:
        self.assertNotEqual(
            rsa_login.pkcs1v15_encrypt(b"same", _REAL_N, _REAL_E),
            rsa_login.pkcs1v15_encrypt(b"same", _REAL_N, _REAL_E),
        )
        self.assertNotEqual(_em_from_degenerate_key(b"same"), _em_from_degenerate_key(b"same"))

    def test_plaintext_too_long_raises_without_leaking_it(self) -> None:
        plaintext = ("S3CR3T-" + "x" * 111).encode()  # 118 字节 > k-11 = 117
        self.assertEqual(len(plaintext), 118)
        with self.assertRaises(ValueError) as ctx:
            rsa_login.pkcs1v15_encrypt(plaintext, _REAL_N, _REAL_E)
        message = str(ctx.exception)
        self.assertNotIn("S3CR3T", message)
        self.assertIn("118", message)

    def test_max_length_plaintext_is_accepted(self) -> None:
        cipher = rsa_login.pkcs1v15_encrypt(b"y" * 117, _REAL_N, _REAL_E)
        self.assertEqual(len(base64.b64decode(cipher)), 128)

    def test_rejects_bad_key_parameters(self) -> None:
        with self.assertRaises(ValueError):
            rsa_login.pkcs1v15_encrypt(b"x", _REAL_N, 0)
        with self.assertRaises(TypeError):
            rsa_login.pkcs1v15_encrypt("not-bytes", _REAL_N, _REAL_E)  # type: ignore[arg-type]

    def test_encrypt_password_uses_frozen_key_and_utf8(self) -> None:
        """把公钥换成退化测试钥匙，即可验证 encrypt_password 走的是冻结公钥 + UTF-8。"""
        with mock.patch.object(rsa_login, "_frozen_key", lambda: (_TEST_N, _TEST_E)):
            em = base64.b64decode(rsa_login.encrypt_password("中"))
        self.assertEqual(em[:2], b"\x00\x02")
        self.assertEqual(em[-3:], "中".encode("utf-8"))

    def test_encrypt_password_length_boundary(self) -> None:
        self.assertEqual(len(rsa_login.encrypt_password("中" * 39)), 172)  # 39×3 = 117 字节
        with self.assertRaises(ValueError):
            rsa_login.encrypt_password("z" * 118)
        with self.assertRaises(ValueError):
            rsa_login.encrypt_password("中" * 40)  # 120 字节

    def test_encrypt_password_rejects_non_str(self) -> None:
        with self.assertRaises(TypeError):
            rsa_login.encrypt_password(b"bytes-not-str")  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# 登录
# --------------------------------------------------------------------------


class TestLogin(unittest.TestCase):
    def test_login_success_and_form_contract(self) -> None:
        logger = _logger("login_success")
        client, transport = _client(
            _resp(_ANON_HOME),
            _resp(_LOGGED_IN_HOME),
            set_cookie_on_post=_cookie(protocol.LOGIN_SUCCESS_COOKIE, _COOKIE_SECRET),
            logger=logger,
        )
        with self.assertLogs(logger, level="DEBUG") as logs:
            ok = asyncio.run(client.login(_USER, _PASSWORD))

        self.assertTrue(ok)
        self.assertEqual([call["method"] for call in transport.calls], ["GET", "POST"])
        self.assertEqual(transport.calls[0]["url"], protocol.BASE_URL + protocol.Endpoints.HOME)

        post = transport.calls[1]
        self.assertEqual(post["url"], protocol.BASE_URL + protocol.LOGIN_PATH)
        self.assertEqual(post["headers"]["Content-Type"], "application/x-www-form-urlencoded")
        self.assertEqual(post["headers"]["Referer"], protocol.BASE_URL + "/")
        self.assertEqual(post["headers"]["Origin"], protocol.BASE_URL)

        body = post["body"].decode("utf-8")
        self.assertNotIn(_PASSWORD, body, "明文密码绝不能进请求体")
        form = urllib.parse.parse_qs(body, keep_blank_values=True)
        self.assertEqual(form[protocol.LOGIN_FIELD_USER], [_USER])
        self.assertEqual(form[protocol.LOGIN_FIELD_PASSWORD], [""], "password 字段必须是空串")
        self.assertEqual(form[protocol.LOGIN_FIELD_REMEMBER], ["1"])
        self.assertEqual(form[protocol.LOGIN_FIELD_CHECKHASH], [""])
        cipher = form[protocol.LOGIN_FIELD_PASSWORD_ENCRYPT][0]
        self.assertEqual(len(cipher), 172)
        self.assertEqual(len(base64.b64decode(cipher)), 128)

        self.assertIn(protocol.LOGIN_SUCCESS_COOKIE, "\n".join(logs.output))

    def test_requests_carry_protocol_user_agent(self) -> None:
        """UrllibTransport 自己不设 UA；本层必须把 protocol.USER_AGENT 带上。"""
        client, transport = _client(
            _resp(_ANON_HOME),
            _resp(_LOGGED_IN_HOME),
            set_cookie_on_post=_cookie(protocol.LOGIN_SUCCESS_COOKIE, _COOKIE_SECRET),
            logger=_logger("login_ua"),
        )
        asyncio.run(client.login(_USER, _PASSWORD))
        self.assertEqual([call["method"] for call in transport.calls], ["GET", "POST"])
        for call in transport.calls:
            self.assertEqual(call["headers"]["User-Agent"], protocol.USER_AGENT)
            self.assertEqual(call["headers"]["Accept-Language"], "zh-CN,zh;q=0.9")

    def test_login_accepts_page_that_only_mentions_marker_in_script(self) -> None:
        # 建议-8：落地页的脚本文案里有裸串 lg_from_w，但并没有登录表单 → 必须判成功
        client, _ = _client(
            _resp(_ANON_HOME),
            _resp(_SCRIPT_MENTIONS_MARKER),
            set_cookie_on_post=_cookie(protocol.LOGIN_SUCCESS_COOKIE, _COOKIE_SECRET),
            logger=_logger("login_script_marker"),
        )
        self.assertTrue(asyncio.run(client.login(_USER, _PASSWORD)))

    def test_login_fails_when_page_still_has_login_form(self) -> None:
        logger = _logger("login_form_branch")
        client, _ = _client(
            _resp(_ANON_HOME),
            _resp(_ANON_HOME),  # 落地页仍是登录表单
            set_cookie_on_post=_cookie(protocol.LOGIN_SUCCESS_COOKIE, _COOKIE_SECRET),
            logger=logger,
        )
        with self.assertLogs(logger, level="DEBUG"):
            self.assertFalse(asyncio.run(client.login(_USER, _PASSWORD)))

    def test_login_fails_when_success_cookie_missing(self) -> None:
        logger = _logger("login_cookie_branch")
        client, _ = _client(_resp(_ANON_HOME), _resp(_LOGGED_IN_HOME), logger=logger)
        with self.assertLogs(logger, level="DEBUG"):
            self.assertFalse(asyncio.run(client.login(_USER, _PASSWORD)))

    def test_login_fails_conservatively_when_transport_has_no_jar(self) -> None:
        """transport 不暴露 cookie jar 时保守判否，绝不因页面像登录页就报成功。"""
        logger = _logger("login_no_jar")
        client, _ = _client(
            _resp(_ANON_HOME), _resp(_LOGGED_IN_HOME), with_jar=False, logger=logger
        )
        with self.assertLogs(logger, level="DEBUG"):
            self.assertFalse(asyncio.run(client.login(_USER, _PASSWORD)))

    def test_login_http_error_raises_without_body_or_password(self) -> None:
        client, _ = _client(
            _resp(_ANON_HOME),
            _resp("<html>SECRET-BODY-403</html>", status=403),
            logger=_logger("login_403"),
        )
        with self.assertRaises(PigaiResponseError) as ctx:
            asyncio.run(client.login(_USER, _PASSWORD))
        self.assertEqual(ctx.exception.status, 403)
        self.assertEqual(ctx.exception.method, "POST")
        self.assertNotIn("SECRET-BODY-403", str(ctx.exception))
        self.assertNotIn(_PASSWORD, str(ctx.exception))

    def test_login_home_request_failure_raises(self) -> None:
        client, _ = _client(_resp("", status=500), logger=_logger("login_home_500"))
        with self.assertRaises(PigaiResponseError) as ctx:
            asyncio.run(client.login(_USER, _PASSWORD))
        self.assertEqual(ctx.exception.status, 500)
        self.assertEqual(ctx.exception.path, protocol.Endpoints.HOME)

    def test_login_rejects_empty_credentials_without_request(self) -> None:
        client, transport = _client(logger=_logger("login_empty_creds"))
        with self.assertRaises(ValueError):
            asyncio.run(client.login("", _PASSWORD))
        with self.assertRaises(ValueError):
            asyncio.run(client.login(_USER, ""))
        with self.assertRaises(ValueError):
            asyncio.run(client.login("   ", _PASSWORD))
        self.assertEqual(transport.calls, [])

    def test_logs_and_errors_never_contain_password_ciphertext_or_cookie_value(self) -> None:
        logger = _logger("login_privacy")
        client, _ = _client(
            _resp(_ANON_HOME),
            _resp(_LOGGED_IN_HOME),
            set_cookie_on_post=_cookie(protocol.LOGIN_SUCCESS_COOKIE, _COOKIE_SECRET),
            logger=logger,
        )
        with self.assertLogs(logger, level="DEBUG") as logs:
            asyncio.run(client.login(_USER, _PASSWORD))
        text = "\n".join(logs.output)
        self.assertNotIn(_PASSWORD, text)
        self.assertNotIn(_COOKIE_SECRET, text)
        self.assertNotIn(_USER, text, "账号也是个人信息，不进日志")
        _assert_no_b64_blob(self, text)


# --------------------------------------------------------------------------
# 会话探针 / GET / 生命周期
# --------------------------------------------------------------------------


class TestSessionProbe(unittest.TestCase):
    """``is_logged_in()`` = 探针页（``?c=write``）含**正面标记** ``id="essayList"``。

    四个用例形态对应 lead 在 ``data/recon/pigai/raw/`` 抓到的真实样本。
    """

    def test_probe_false_on_expired_page_with_stale_cookie(self) -> None:
        """用例 1（本任务的核心）：匿名失效页 + jar 里一条**陈旧** ``_JUKU_USER`` → False。

        这是旧实现（task-7：不看容器、只看「无登录表单」+「有 cookie」）会**误判 True**
        的场景 —— 失效页同样不含登录表单标识。
        """
        client, transport = _client(
            _resp(_ANON_EXPIRED_WRITE), logger=_logger("probe_stale_cookie")
        )
        _seed_login_cookie(transport)  # 陈旧 cookie 真的在 jar 里
        self.assertTrue(
            any(c.name == protocol.LOGIN_SUCCESS_COOKIE for c in transport.cookies),
            "前提：jar 里确实塞了陈旧 cookie",
        )
        self.assertFalse(asyncio.run(client.is_logged_in()))
        # 探针改打强制鉴权页（列表页），不再是首页
        self.assertEqual(
            transport.calls[0]["url"], protocol.BASE_URL + protocol.Endpoints.ESSAYS_DONE
        )

    def test_probe_true_on_logged_in_list_page(self) -> None:
        """用例 2：已登录 ``?c=write`` 形态（含 ``id="essayList"``）→ True。"""
        client, _ = _client(_resp(_LOGGED_IN_WRITE), logger=_logger("probe_list"))
        self.assertTrue(asyncio.run(client.is_logged_in()))

    def test_probe_true_on_empty_list_page(self) -> None:
        """用例 3：空列表页（只有表头，但仍含容器）→ True。"""
        client, _ = _client(_resp(_EMPTY_LIST_WRITE), logger=_logger("probe_empty_list"))
        self.assertTrue(asyncio.run(client.is_logged_in()))

    def test_probe_false_on_login_form_page(self) -> None:
        """用例 4：真登录页形态（``b_loginform.html``，有 ``id="lg_from_w"``）→ False。"""
        client, transport = _client(_resp(_ANON_HOME), logger=_logger("probe_login_page"))
        _seed_login_cookie(transport)  # 有 cookie 也不影响：探针只认正面标记
        self.assertFalse(asyncio.run(client.is_logged_in()))

    def test_probe_false_on_any_non_list_200_page(self) -> None:
        """任何 200 但不是列表页的页面（错误页/维护页/风控页）→ False。"""
        client, _ = _client(_resp(_ERROR_PAGE_200), logger=_logger("probe_error_page"))
        self.assertFalse(asyncio.run(client.is_logged_in()))

    def test_probe_ignores_cookie_jar_entirely(self) -> None:
        """正面判据不依赖 cookie jar：没有 jar 也能正确判「已登录」。"""
        client, _ = _client(_resp(_LOGGED_IN_WRITE), with_jar=False, logger=_logger("probe_no_jar"))
        self.assertTrue(asyncio.run(client.is_logged_in()))

    def test_probe_not_fooled_by_bare_container_name_in_script(self) -> None:
        """脚本里提到 ``#essayList`` 不算列表页（同 建议-8 的道理，正面标记也要严格形态）。"""
        self.assertIn(protocol.LIST_CONTAINER_ID, _SCRIPT_MENTIONS_CONTAINER)
        self.assertNotIn(f'id="{protocol.LIST_CONTAINER_ID}"', _SCRIPT_MENTIONS_CONTAINER)
        client, _ = _client(_resp(_SCRIPT_MENTIONS_CONTAINER), logger=_logger("probe_script"))
        self.assertFalse(asyncio.run(client.is_logged_in()))

    def test_probe_is_not_cached(self) -> None:
        client, transport = _client(
            _resp(_ANON_EXPIRED_WRITE), _resp(_LOGGED_IN_WRITE), logger=_logger("probe_nocache")
        )
        self.assertFalse(asyncio.run(client.is_logged_in()))
        self.assertTrue(asyncio.run(client.is_logged_in()))
        self.assertEqual(len(transport.calls), 2, "探针不得缓存结果")
        self.assertEqual(
            {call["url"] for call in transport.calls},
            {protocol.BASE_URL + protocol.Endpoints.ESSAYS_DONE},
        )

    def test_probe_false_on_non_ok_status(self) -> None:
        """HTTP 非 2xx 是**业务**判断 → False（走重登），不抛异常。"""
        client, _ = _client(_resp("", status=502), logger=_logger("probe_502"))
        self.assertFalse(asyncio.run(client.is_logged_in()))

    def test_probe_raises_transport_error_on_network_failure(self) -> None:
        """网络层异常 → **抛 TransportError**，不得吞成 False。

        ``refresh_session()`` 在 Scheduler 之外，吞成 False 换不来重试，只会紧接着
        发一次登录 POST（站点故障时的风控面）。抛出才能让 Scheduler 退避重试。
        """
        client = PigaiClient(
            HttpClient(_FailingTransport(), timeout=5.0), logger=_logger("probe_neterr")
        )
        with self.assertRaises(TransportError):
            asyncio.run(client.is_logged_in())

    def test_probe_still_raises_after_close(self) -> None:
        """「拿不到响应判否」不等于吞掉编程错误：aclose 之后仍抛 ``PigaiError``。"""
        client, _ = _client(_resp(_LOGGED_IN_WRITE), logger=_logger("probe_closed"))
        asyncio.run(client.aclose())
        with self.assertRaises(PigaiError):
            asyncio.run(client.is_logged_in())


class TestGetAndLifecycle(unittest.TestCase):
    def test_get_returns_page_text(self) -> None:
        client, transport = _client(
            _resp("<html>未完成作业</html>"), logger=_logger("get_ok")
        )
        html = asyncio.run(client.get(protocol.Endpoints.ASSIGNMENTS_PENDING))
        self.assertEqual(html, "<html>未完成作业</html>")
        self.assertEqual(
            transport.calls[0]["url"],
            protocol.BASE_URL + protocol.Endpoints.ASSIGNMENTS_PENDING,
        )
        self.assertEqual(transport.calls[0]["method"], "GET")
        self.assertEqual(transport.calls[0]["headers"]["User-Agent"], protocol.USER_AGENT)

    def test_get_raises_on_bad_status_without_leaking_body(self) -> None:
        client, _ = _client(
            _resp("<html>SECRET-BODY-404</html>", status=404), logger=_logger("get_404")
        )
        with self.assertRaises(PigaiResponseError) as ctx:
            asyncio.run(client.get(protocol.Endpoints.ESSAYS_DONE))
        self.assertEqual(ctx.exception.status, 404)
        self.assertNotIn("SECRET-BODY-404", str(ctx.exception))

    def test_get_rejects_paths_outside_the_site(self) -> None:
        client, transport = _client(logger=_logger("get_bad_path"))
        for bad in ("https://evil.example/x", "//evil.example/x", "index.php?c=write", ""):
            with self.assertRaises(ValueError):
                asyncio.run(client.get(bad))
        self.assertEqual(transport.calls, [])

    def test_aclose_is_idempotent_and_blocks_further_calls(self) -> None:
        client, transport = _client(_resp(_LOGGED_IN_HOME), logger=_logger("aclose"))
        asyncio.run(client.aclose())
        asyncio.run(client.aclose())  # 幂等
        for coro in (
            client.get(protocol.Endpoints.ESSAYS_DONE),
            client.is_logged_in(),
            client.login(_USER, _PASSWORD),
        ):
            with self.assertRaises(PigaiError):
                asyncio.run(coro)
        self.assertEqual(transport.calls, [])

    def test_async_context_manager_closes(self) -> None:
        client, _ = _client(logger=_logger("aenter"))

        async def _run() -> bool:
            async with client:
                return True

        self.assertTrue(asyncio.run(_run()))
        with self.assertRaises(PigaiError):
            asyncio.run(client.is_logged_in())


# --------------------------------------------------------------------------
# 架构约束（integration-guide §3.3 / §8）
# --------------------------------------------------------------------------


class TestClientArchitecture(unittest.TestCase):
    def test_client_has_no_direct_transport_imports(self) -> None:
        tree = ast.parse(_CLIENT_PATH.read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                imported.add(node.module.split(".")[0])
        forbidden = {"urllib", "requests", "socket", "http", "os", "time"}
        self.assertEqual(imported & forbidden, set(), "协议型适配器不得绕过 HttpClient")

        # 也不许出现 os.environ / time.sleep 之类的属性链
        chains = {
            ".".join(parts)
            for parts in _attribute_chains(tree)
            if parts[0] in {"os", "time", "urllib", "socket", "requests"}
        }
        self.assertEqual(chains, set())

    def test_constructor_is_injection_only(self) -> None:
        params = inspect.signature(PigaiClient.__init__).parameters
        self.assertEqual(list(params), ["self", "http", "logger"])
        self.assertIs(params["http"].kind, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        self.assertIs(params["logger"].kind, inspect.Parameter.KEYWORD_ONLY)


def _attribute_chains(tree: ast.AST) -> list[list[str]]:
    chains: list[list[str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        parts: list[str] = []
        cursor: ast.expr = node
        while isinstance(cursor, ast.Attribute):
            parts.append(cursor.attr)
            cursor = cursor.value
        if isinstance(cursor, ast.Name):
            parts.append(cursor.id)
            chains.append(list(reversed(parts)))
    return chains


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
