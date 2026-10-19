"""批改网登录加密：**纯标准库**复刻 JSEncrypt 的 RSAES-PKCS1-v1_5。

规格来源：``docs/research/pigai.md`` §4.2 / §4.3（真机登录已验证服务端接受该密文）。
公钥的唯一出处是 ``protocol.RSA_PUBKEY_B64``，本模块不硬编码任何密钥材料。

为什么自己实现而不用 pycryptodome / rsa / cryptography：``pyproject.toml`` 声明
``dependencies = []``（核心零依赖）。RSA **公钥加密**只需要一次模幂（``pow``）加一段
填充，标准库足够，不值得为此引入 C 扩展依赖。

算法（与前端 ``2020.js`` 调用的 JSEncrypt 完全一致）：

    EM = 00 02 ‖ 非零随机填充 PS ‖ 00 ‖ 明文 M     （PKCS#1 v1.5，块长 k）
    密文 = base64( EM^e mod n )

填充字节取到 0x00 必须重试（JSEncrypt ``pkcs1pad2`` 同样如此）。

安全约定：
  * 本模块只做加密，不持有、不打印明文；异常信息里只有长度数字，
    不含明文、不含密文；
  * 明文长度上限 ``k - 11``（RSA-1024 → 117 字节），超出抛 ``ValueError``。
"""

from __future__ import annotations

import base64
import binascii
import re
import secrets
from functools import lru_cache

from kedaibiao.platforms.pigai import protocol

__all__ = ["encrypt_password", "parse_spki_public_key", "pkcs1v15_encrypt"]

#: PKCS#1 v1.5 加密的固定开销：``00 02`` + 至少 8 字节 PS + ``00``
_PKCS1_OVERHEAD: int = 11


# --------------------------------------------------------------------------
# DER / SPKI 解析
# --------------------------------------------------------------------------


def _read_tlv(buf: bytes, offset: int) -> tuple[int, bytes, int]:
    """读一个 DER TLV，返回 ``(tag, value, 下一个偏移)``。越界一律 ``ValueError``。"""
    if offset + 2 > len(buf):
        raise ValueError("DER 结构截断：读不到 tag/length")
    tag = buf[offset]
    first = buf[offset + 1]
    cursor = offset + 2
    length = first
    if first & 0x80:
        count = first & 0x7F
        if count == 0:
            raise ValueError("不支持的 DER 不定长编码")
        if cursor + count > len(buf):
            raise ValueError("DER 结构截断：长度字段越界")
        length = int.from_bytes(buf[cursor : cursor + count], "big")
        cursor += count
    end = cursor + length
    if end > len(buf):
        raise ValueError("DER 结构截断：内容越界")
    return tag, buf[cursor:end], end


def parse_spki_public_key(b64: str) -> tuple[int, int]:
    """从 SPKI（``SubjectPublicKeyInfo``）base64 里解出 ``(n, e)``。

    只认标准库，不依赖任何密码学库。输入允许带换行/空白的 PEM 风格 base64。
    结构不合法时抛 ``ValueError``。
    """
    if not isinstance(b64, str):
        raise ValueError("公钥必须是 base64 字符串")
    compact = re.sub(r"\s+", "", b64)
    if not compact:
        raise ValueError("公钥为空")
    try:
        der = base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("公钥不是合法的 base64") from exc

    tag, spki, _ = _read_tlv(der, 0)
    if tag != 0x30:
        raise ValueError("SPKI 顶层不是 SEQUENCE")
    tag, algorithm, cursor = _read_tlv(spki, 0)
    if tag != 0x30:
        raise ValueError("SPKI 缺少 AlgorithmIdentifier")
    _read_tlv(algorithm, 0)  # rsaEncryption OID（1.2.840.113549.1.1.1），只需跳过
    tag, bit_string, _ = _read_tlv(spki, cursor)
    if tag != 0x03:
        raise ValueError("SPKI 缺少 BIT STRING")
    if not bit_string or bit_string[0] != 0x00:
        raise ValueError("BIT STRING 存在未用位，不是裸 RSA 公钥")

    tag, rsa_key, _ = _read_tlv(bit_string, 1)
    if tag != 0x30:
        raise ValueError("BIT STRING 里不是 RSAPublicKey SEQUENCE")
    tag, modulus, cursor = _read_tlv(rsa_key, 0)
    if tag != 0x02:
        raise ValueError("RSAPublicKey 缺少 INTEGER modulus")
    tag, exponent, _ = _read_tlv(rsa_key, cursor)
    if tag != 0x02:
        raise ValueError("RSAPublicKey 缺少 INTEGER publicExponent")

    n = int.from_bytes(modulus, "big")
    e = int.from_bytes(exponent, "big")
    if n <= 0 or e < 1:
        raise ValueError("RSA 公钥参数非法（n>0 且 e>=1）")
    return n, e


# --------------------------------------------------------------------------
# PKCS#1 v1.5 加密
# --------------------------------------------------------------------------


def _pkcs1v15_pad(message: bytes, k: int) -> bytes:
    """构造 ``EM = 00 02 ‖ 非零随机 PS ‖ 00 ‖ M``（复刻 JSEncrypt ``pkcs1pad2``）。"""
    ps_len = k - 3 - len(message)
    if ps_len < 8:
        # 正常路径已在 pkcs1v15_encrypt 里按住 k-11 拦下；这里只作最后一道保险。
        raise ValueError(f"明文过长：{len(message)} > {k - _PKCS1_OVERHEAD}")
    padding = bytearray()
    while len(padding) < ps_len:
        byte = secrets.token_bytes(1)[0]
        if byte:  # JSEncrypt 同样跳过 0x00
            padding.append(byte)
    return b"\x00\x02" + bytes(padding) + b"\x00" + message


def pkcs1v15_encrypt(message: bytes, n: int, e: int) -> str:
    """用 ``(n, e)`` 加密单块 ``message``，返回 base64 密文（标准库实现）。

    * 明文长度 > ``k - 11``（``k`` = 模数字节长）抛 ``ValueError``，消息里只有长度；
    * 填充字节由 ``secrets`` 取非零随机值 → 同一明文两次加密结果不同。
    """
    if not isinstance(message, (bytes, bytearray, memoryview)):
        raise TypeError("message 必须是 bytes")
    payload = bytes(message)
    if n <= 0 or e < 1:
        raise ValueError("RSA 公钥参数非法（n>0 且 e>=1）")

    k = (n.bit_length() + 7) // 8
    if len(payload) > k - _PKCS1_OVERHEAD:
        # 只报长度，绝不带明文
        raise ValueError(
            f"明文过长：{len(payload)} > {k - _PKCS1_OVERHEAD} 字节"
            f"（RSA-{n.bit_length()} 单块上限）"
        )

    em = _pkcs1v15_pad(payload, k)
    cipher = pow(int.from_bytes(em, "big"), e, n)
    return base64.b64encode(cipher.to_bytes(k, "big")).decode("ascii")


@lru_cache(maxsize=1)
def _frozen_key() -> tuple[int, int]:
    """``protocol.RSA_PUBKEY_B64`` 的 ``(n, e)``；延迟解析，避免 import 期副作用。"""
    return parse_spki_public_key(protocol.RSA_PUBKEY_B64)


def encrypt_password(password: str) -> str:
    """用站点冻结公钥（``protocol.RSA_PUBKEY_B64``）加密密码，返回 base64 密文。

    与前端一致：密码按 **UTF-8** 编码后单块加密（JSEncrypt 内部就是逐字符写 UTF-8）。
    明文不进日志、不进异常；密码超过 117 字节时抛 ``ValueError``。
    """
    if not isinstance(password, str):
        raise TypeError("password 必须是 str")
    n, e = _frozen_key()
    return pkcs1v15_encrypt(password.encode("utf-8"), n, e)
