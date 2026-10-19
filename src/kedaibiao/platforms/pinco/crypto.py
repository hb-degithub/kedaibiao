"""品课 SSO 的密码加密 —— 纯标准库实现（``hashlib`` + 大数 ``pow``）。

Lead 实测跑通的算法（账号验证成功、返回真实用户资料）：

1. ``/common/getPublicKeyV3`` 返回的 ``data`` 是 **base64 DER(SPKI)**，
   没有 PEM 头尾；
2. 从中取出 RSA 的 ``n`` / ``e``；
3. PKCS#1 v1.5 加密：``EM = 00 02 || 非零随机 PS || 00 || M``，密文 base64；
4. ``password`` 字段装的是 ``RSA(MD5(明文).hexdigest())``，
   ``username`` 字段装的是 ``RSA(明文用户名)``。

**踩过的坑，用测试钉住**：DER 里的 ``INTEGER n`` 常常带一个前导 ``0x00``
（最高位为 1 时必须带）。密钥字节数必须按 ``(n.bit_length() + 7) // 8`` 算，
照 DER 值的长度算会多一个字节，服务端直接回 ``500403 登录加解密验证失败``。

为什么不用 ``cryptography``：核心与平台层都保持零第三方依赖，RSA 只用到
「大数模幂」，标准库的 ``pow(m, e, n)`` 就够；自己实现的只有 DER 取值与
PKCS#1 v1.5 填充这两段，都是几十行且可离线验证。
"""

from __future__ import annotations

import base64
import hashlib
import secrets

from kedaibiao.platforms.pinco.errors import PincoProtocolError

#: DER 标签
_TAG_INTEGER = 0x02
_TAG_BIT_STRING = 0x03
_TAG_SEQUENCE = 0x30

#: PKCS#1 v1.5 的最小填充开销：``00 02`` + PS(>=8) + ``00``
_PKCS1_OVERHEAD = 11


def _read_length(data: bytes, offset: int) -> tuple[int, int]:
    """读 DER 长度字段，返回 (长度, 新偏移)。"""
    if offset >= len(data):
        raise PincoProtocolError("DER 长度字段被截断")
    first = data[offset]
    offset += 1
    if first < 0x80:
        return first, offset
    count = first & 0x7F
    if count == 0:
        raise PincoProtocolError("DER 不支持不定长（indefinite length）编码")
    if offset + count > len(data):
        raise PincoProtocolError("DER 长形式长度被截断")
    return int.from_bytes(data[offset : offset + count], "big"), offset + count


def _collect_integers(data: bytes) -> list[int]:
    """递归收集 DER 里的 INTEGER。

    为什么不做通用 ASN.1 解析器：RSA SPKI 里除了 ``n`` / ``e`` 没有别的
    INTEGER，通用解析器是另一个坑，而这里只需要这两个数。
    """
    values: list[int] = []
    offset = 0
    while offset < len(data):
        tag = data[offset]
        offset += 1
        length, offset = _read_length(data, offset)
        if offset + length > len(data):
            raise PincoProtocolError("DER 内容被截断")
        value = data[offset : offset + length]
        offset += length
        if tag == _TAG_INTEGER:
            values.append(int.from_bytes(value, "big"))
        elif tag == _TAG_SEQUENCE:
            values.extend(_collect_integers(value))
        elif tag == _TAG_BIT_STRING:
            # BIT STRING 第一个字节是「未使用位数」，后面的内容才是 DER
            values.extend(_collect_integers(value[1:]))
    return values


def parse_rsa_public_key(der: bytes) -> tuple[int, int]:
    """从 DER(SPKI) 里取出 ``(n, e)``。"""
    integers = _collect_integers(der)
    if len(integers) < 2:
        raise PincoProtocolError(
            f"公钥 DER 里没找到 n/e 两个 INTEGER（只找到 {len(integers)} 个）"
        )
    n, e = integers[0], integers[1]
    if n <= 1 or e <= 1:
        raise PincoProtocolError("公钥里的 n/e 不是合法的 RSA 参数")
    return n, e


def decode_public_key(payload: str) -> tuple[int, int]:
    """``base64 DER(SPKI)`` → ``(n, e)``。"""
    text = payload.strip()
    try:
        der = base64.b64decode(text, validate=False)
    except (ValueError, TypeError) as exc:
        raise PincoProtocolError(f"公钥不是合法的 base64：{exc}") from exc
    return parse_rsa_public_key(der)


def public_key_size_bytes(n: int) -> int:
    """RSA 模数的字节长度。

    **必须按位长算**：DER 里的 n 常带前导 ``0x00``，按字节长度算会多一个字节，
    服务端会回 ``500403 登录加解密验证失败``（Lead 实测踩过）。
    """
    return (n.bit_length() + 7) // 8


def _nonzero_random(length: int) -> bytes:
    """取 ``length`` 个非零随机字节。

    PKCS#1 v1.5 要求 PS 全非零；用滤零而不是"遇到零就重来整段"，
    避免极小概率下的死循环。
    """
    out = bytearray()
    while len(out) < length:
        for byte in secrets.token_bytes(length - len(out)):
            if byte:
                out.append(byte)
    return bytes(out)


def encrypt_pkcs1v15(message: bytes, n: int, e: int) -> bytes:
    """PKCS#1 v1.5 加密，返回定长的密文字节。"""
    key_bytes = public_key_size_bytes(n)
    if len(message) > key_bytes - _PKCS1_OVERHEAD:
        raise PincoProtocolError(
            f"明文 {len(message)} 字节超出 RSA-{key_bytes * 8} 的 PKCS#1 v1.5 容量"
        )
    ps_length = key_bytes - len(message) - 3
    em = b"\x00\x02" + _nonzero_random(ps_length) + b"\x00" + message
    return pow(int.from_bytes(em, "big"), e, n).to_bytes(key_bytes, "big")


def encrypt_field_b64(message: bytes, n: int, e: int) -> str:
    """加密一个字段并 base64 编码（服务端要求的形式）。"""
    return base64.b64encode(encrypt_pkcs1v15(message, n, e)).decode("ascii")


def encrypt_credentials(username: str, password: str, n: int, e: int) -> tuple[str, str]:
    """返回 ``(加密后的 username, 加密后的 password)``。

    password 加密的是 **MD5 十六进制摘要**，不是明文 —— 实测如此，服务端按同样
    方式校验。这里的 MD5 是平台协议的一部分（不是我们自己的安全设计），
    所以不要"升级"成 SHA-256。
    """
    password_digest = hashlib.md5(  # noqa: S324 - 平台协议就是这样，非自选
        password.encode("utf-8")
    ).hexdigest()
    return (
        encrypt_field_b64(username.encode("utf-8"), n, e),
        encrypt_field_b64(password_digest.encode("utf-8"), n, e),
    )
