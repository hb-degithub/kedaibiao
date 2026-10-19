"""加解密与令牌铸造：**全部是纯函数**，可脱离网络独立单测。

规格第 3 节共四套机制，本模块实现本阶段需要的三套：

1. :func:`encrypt_login_field` —— 登录字段 AES-128-CBC + PKCS7 + 固定 IV，
   输出**大写十六进制**（3.1）；
2. :func:`build_annotator_token` —— 客户端自铸的 HS256 JWT（3.2）；
3. :func:`decrypt_unipus_cipher` —— ``unipus.`` 密文的 AES-128-ECB 解密 +
   去尾部 ``\\0``（3.3）。

⚠️ 第 1 套与第 3 套**绝对不能统一**：一个有固定 IV、一个是 ECB 无 IV；
PKCS7 与「去尾部 0x00」方向恰好相反（规格第 3.3 节末）。

AES 是纯 Python 实现：核心层约定「能零依赖就零依赖」，标准库没有 AES，
而引入第三方库会破坏本项目"裸 Python 可跑"的前提。正确性由 FIPS-197 /
NIST SP 800-38A 的标准向量在测试里锁住。
"""

from __future__ import annotations

import base64
import hashlib
import hmac

from kedaibiao.platforms.unipus.endpoints import (
    ANNOTATOR_AUDIENCE,
    ANNOTATOR_ISSUER,
    ANNOTATOR_SIGNING_SECRET,
    ANNOTATOR_TTL_MS,
    CIPHER_KEY_PREFIX,
    CIPHER_PREFIX,
    LOGIN_AES_IV_HEX,
    LOGIN_AES_KEY_HEX,
)
from kedaibiao.platforms.unipus.errors import UnipusCryptoError, UnipusProtocolError

BLOCK_SIZE = 16

# --------------------------------------------------------------------------
# AES-128（FIPS-197）
# --------------------------------------------------------------------------

_SBOX_SOURCE = """
63 7c 77 7b f2 6b 6f c5 30 01 67 2b fe d7 ab 76
ca 82 c9 7d fa 59 47 f0 ad d4 a2 af 9c a4 72 c0
b7 fd 93 26 36 3f f7 cc 34 a5 e5 f1 71 d8 31 15
04 c7 23 c3 18 96 05 9a 07 12 80 e2 eb 27 b2 75
09 83 2c 1a 1b 6e 5a a0 52 3b d6 b3 29 e3 2f 84
53 d1 00 ed 20 fc b1 5b 6a cb be 39 4a 4c 58 cf
d0 ef aa fb 43 4d 33 85 45 f9 02 7f 50 3c 9f a8
51 a3 40 8f 92 9d 38 f5 bc b6 da 21 10 ff f3 d2
cd 0c 13 ec 5f 97 44 17 c4 a7 7e 3d 64 5d 19 73
60 81 4f dc 22 2a 90 88 46 ee b8 14 de 5e 0b db
e0 32 3a 0a 49 06 24 5c c2 d3 ac 62 91 95 e4 79
e7 c8 37 6d 8d d5 4e a9 6c 56 f4 ea 65 7a ae 08
ba 78 25 2e 1c a6 b4 c6 e8 dd 74 1f 4b bd 8b 8a
70 3e b5 66 48 03 f6 0e 61 35 57 b9 86 c1 1d 9e
e1 f8 98 11 69 d9 8e 94 9b 1e 87 e9 ce 55 28 df
8c a1 89 0d bf e6 42 68 41 99 2d 0f b0 54 bb 16
"""


def _sbox() -> list[int]:
    values = [int(token, 16) for token in _SBOX_SOURCE.split()]
    if len(values) != 256:  # pragma: no cover - 常量自检
        raise AssertionError("S 盒必须是 256 项")
    return values


_SBOX = _sbox()
_INV_SBOX = [0] * 256
for _index, _value in enumerate(_SBOX):
    _INV_SBOX[_value] = _index
del _index, _value

#: Rijndael 轮常数
_RCON = (0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36)


def _xtime(value: int) -> int:
    value <<= 1
    if value & 0x100:
        value ^= 0x11B
    return value & 0xFF


def _gmul(a: int, b: int) -> int:
    """GF(2^8) 乘法（Rijndael 多项式 0x11B）。"""
    result = 0
    while b:
        if b & 1:
            result ^= a
        a = _xtime(a)
        b >>= 1
    return result


def _key_schedule(key: bytes) -> list[list[int]]:
    if len(key) != BLOCK_SIZE:
        raise UnipusCryptoError(f"AES-128 密钥必须是 16 字节，实际 {len(key)}")
    words = [list(key[4 * i : 4 * i + 4]) for i in range(4)]
    for index in range(4, 44):
        temp = list(words[index - 1])
        if index % 4 == 0:
            temp = temp[1:] + temp[:1]
            temp = [_SBOX[byte] for byte in temp]
            temp[0] ^= _RCON[index // 4 - 1]
        words.append([words[index - 4][j] ^ temp[j] for j in range(4)])
    return [sum(words[4 * round_index : 4 * round_index + 4], []) for round_index in range(11)]


def _shift_rows(state: list[int]) -> list[int]:
    out = [0] * BLOCK_SIZE
    for row in range(4):
        for column in range(4):
            out[row + 4 * column] = state[row + 4 * ((column + row) % 4)]
    return out


def _inv_shift_rows(state: list[int]) -> list[int]:
    out = [0] * BLOCK_SIZE
    for row in range(4):
        for column in range(4):
            out[row + 4 * column] = state[row + 4 * ((column - row) % 4)]
    return out


def _mix_columns(state: list[int]) -> list[int]:
    out = [0] * BLOCK_SIZE
    for column in range(4):
        a0, a1, a2, a3 = state[4 * column : 4 * column + 4]
        out[4 * column + 0] = _gmul(a0, 2) ^ _gmul(a1, 3) ^ a2 ^ a3
        out[4 * column + 1] = a0 ^ _gmul(a1, 2) ^ _gmul(a2, 3) ^ a3
        out[4 * column + 2] = a0 ^ a1 ^ _gmul(a2, 2) ^ _gmul(a3, 3)
        out[4 * column + 3] = _gmul(a0, 3) ^ a1 ^ a2 ^ _gmul(a3, 2)
    return out


def _inv_mix_columns(state: list[int]) -> list[int]:
    out = [0] * BLOCK_SIZE
    for column in range(4):
        a0, a1, a2, a3 = state[4 * column : 4 * column + 4]
        out[4 * column + 0] = _gmul(a0, 14) ^ _gmul(a1, 11) ^ _gmul(a2, 13) ^ _gmul(a3, 9)
        out[4 * column + 1] = _gmul(a0, 9) ^ _gmul(a1, 14) ^ _gmul(a2, 11) ^ _gmul(a3, 13)
        out[4 * column + 2] = _gmul(a0, 13) ^ _gmul(a1, 9) ^ _gmul(a2, 14) ^ _gmul(a3, 11)
        out[4 * column + 3] = _gmul(a0, 11) ^ _gmul(a1, 13) ^ _gmul(a2, 9) ^ _gmul(a3, 14)
    return out


def aes128_encrypt_block(key: bytes, block: bytes) -> bytes:
    """单块 AES-128 加密（ECB 原语）。"""
    if len(block) != BLOCK_SIZE:
        raise UnipusCryptoError(f"AES 分组必须是 16 字节，实际 {len(block)}")
    round_keys = _key_schedule(key)
    state = [byte ^ round_key for byte, round_key in zip(block, round_keys[0])]
    for round_index in range(1, 10):
        state = [_SBOX[byte] for byte in state]
        state = _shift_rows(state)
        state = _mix_columns(state)
        state = [byte ^ key_byte for byte, key_byte in zip(state, round_keys[round_index])]
    state = [_SBOX[byte] for byte in state]
    state = _shift_rows(state)
    state = [byte ^ key_byte for byte, key_byte in zip(state, round_keys[10])]
    return bytes(state)


def aes128_decrypt_block(key: bytes, block: bytes) -> bytes:
    """单块 AES-128 解密（ECB 原语，逆密码）。"""
    if len(block) != BLOCK_SIZE:
        raise UnipusCryptoError(f"AES 分组必须是 16 字节，实际 {len(block)}")
    round_keys = _key_schedule(key)
    state = [byte ^ key_byte for byte, key_byte in zip(block, round_keys[10])]
    for round_index in range(9, 0, -1):
        state = _inv_shift_rows(state)
        state = [_INV_SBOX[byte] for byte in state]
        state = [byte ^ key_byte for byte, key_byte in zip(state, round_keys[round_index])]
        state = _inv_mix_columns(state)
    state = _inv_shift_rows(state)
    state = [_INV_SBOX[byte] for byte in state]
    state = [byte ^ key_byte for byte, key_byte in zip(state, round_keys[0])]
    return bytes(state)


def aes128_cbc_encrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    """AES-128-CBC 加密。``data`` 必须已按 16 字节对齐（调用方先做 PKCS7）。"""
    if len(iv) != BLOCK_SIZE:
        raise UnipusCryptoError(f"AES-CBC 的 IV 必须是 16 字节，实际 {len(iv)}")
    if not data or len(data) % BLOCK_SIZE:
        raise UnipusCryptoError("AES-CBC 明文必须非空且按 16 字节对齐（先做 PKCS7 填充）")
    out = bytearray()
    previous = iv
    for offset in range(0, len(data), BLOCK_SIZE):
        block = bytes(a ^ b for a, b in zip(data[offset : offset + BLOCK_SIZE], previous))
        previous = aes128_encrypt_block(key, block)
        out += previous
    return bytes(out)


def aes128_ecb_decrypt(key: bytes, data: bytes) -> bytes:
    """AES-128-ECB 解密（逐块，无 IV）。"""
    if not data or len(data) % BLOCK_SIZE:
        raise UnipusCryptoError(
            f"密文必须非空且长度是 16 的整数倍，实际 {len(data)} 字节"
        )
    out = bytearray()
    for offset in range(0, len(data), BLOCK_SIZE):
        out += aes128_decrypt_block(key, data[offset : offset + BLOCK_SIZE])
    return bytes(out)


def pkcs7_pad(data: bytes) -> bytes:
    """PKCS7 填充：**总是**追加 1~16 字节（整块时补满一整块）。"""
    padding = BLOCK_SIZE - (len(data) % BLOCK_SIZE)
    return data + bytes([padding]) * padding


def strip_trailing_zeros(data: bytes) -> bytes:
    """去尾部 ``0x00``（ZeroPadding）—— 与 PKCS7 不是一回事，只在 3.3 用。"""
    return data.rstrip(b"\x00")


def to_upper_hex(data: bytes) -> str:
    """**大写**十六进制。规格第 3.1 节点名：小写会被服务端当成密码错误。"""
    return data.hex().upper()


# --------------------------------------------------------------------------
# 3.1 登录字段加密
# --------------------------------------------------------------------------


def encrypt_login_field(plaintext: str) -> str:
    """用户名/密码 → AES-128-CBC（固定密钥与 IV、PKCS7）→ 大写十六进制。

    确定性：同一输入必须得到同一输出（IV 固定，无随机性）。
    """
    key = bytes.fromhex(LOGIN_AES_KEY_HEX)
    iv = bytes.fromhex(LOGIN_AES_IV_HEX)
    padded = pkcs7_pad(plaintext.encode("utf-8"))
    return to_upper_hex(aes128_cbc_encrypt(key, iv, padded))


# --------------------------------------------------------------------------
# 3.2 annotator token（x-annotator-auth-token）
# --------------------------------------------------------------------------

#: payload 字段顺序**是签名的一部分**，不能改（规格第 3.2 节）
ANNOTATOR_PAYLOAD_ORDER: tuple[str, ...] = (
    "open_id",
    "name",
    "email",
    "administrator",
    "exp",
    "iss",
    "aud",
)

_JSON_ESCAPES = {
    '"': '\\"',
    "\\": "\\\\",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
}


def json_escape_minimal(value: str) -> str:
    """规格第 3.2 节的**最小** JSON 转义：只处理 ``" \\ \\n \\r \\t``。"""
    return "".join(_JSON_ESCAPES.get(char, char) for char in value)


def b64url_encode(data: bytes) -> str:
    """base64url 且**无填充**（字母表含 ``-``/``_``，不输出 ``=``）。"""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def annotator_payload_json(open_id: str, *, now_ms: int) -> str:
    """按固定字段顺序手工构造 payload JSON。

    不用 ``json.dumps`` 是为了让字段顺序与转义规则完全可控（规格第 3.2 节）。
    """
    escaped = json_escape_minimal(open_id)
    return (
        '{"open_id":"' + escaped + '",'
        '"name":"","email":"","administrator":false,'
        '"exp":' + str(int(now_ms) + ANNOTATOR_TTL_MS) + ","
        '"iss":"' + ANNOTATOR_ISSUER + '",'
        '"aud":"' + ANNOTATOR_AUDIENCE + '"}'
    )


def build_annotator_token(open_id: str, *, now_ms: int) -> str:
    """铸一个 HS256 JWT。

    ``open_id`` 必填非空（取自登录后的 ``ssoId``）；``now_ms`` 由调用方传入，
    这样函数保持纯函数、单测可复现。
    """
    if not isinstance(open_id, str) or not open_id:
        raise UnipusProtocolError(
            "annotator token 需要非空 open_id（来自登录后的 ssoId），空值必须直接报错"
        )
    header = b64url_encode(b'{"alg":"HS256","typ":"JWT"}')
    payload = b64url_encode(annotator_payload_json(open_id, now_ms=now_ms).encode("utf-8"))
    signing_input = f"{header}.{payload}"
    signature = hmac.new(
        ANNOTATOR_SIGNING_SECRET.encode("ascii"),
        signing_input.encode("ascii"),
        hashlib.sha256,
    ).digest()
    return f"{signing_input}.{b64url_encode(signature)}"


# --------------------------------------------------------------------------
# 3.3 内容/答案密文解密
# --------------------------------------------------------------------------


def cipher_key(k: object) -> bytes:
    """密钥 = ASCII ``1a2b3c4d`` 直接拼接 ``k`` 的文本，长度必须恰好 16 字节。"""
    if isinstance(k, bool) or k is None:
        raise UnipusCryptoError(f"密文外壳缺少可用的 k（实际 {k!r}）")
    key = (CIPHER_KEY_PREFIX + str(k)).encode("ascii", errors="strict")
    if len(key) != BLOCK_SIZE:
        raise UnipusCryptoError(
            f"密钥必须是 16 字节（前缀 8 字节 + k 的文本），实际 {len(key)} 字节（k={k!r}）"
        )
    return key


def decrypt_unipus_cipher(cipher_text: str, k: object) -> str:
    """解密 ``unipus.<hex>``；**不以该前缀开头的一律当明文原样返回**。"""
    if not isinstance(cipher_text, str):
        raise UnipusCryptoError(f"密文必须是字符串，实际 {type(cipher_text).__name__}")
    if not cipher_text.startswith(CIPHER_PREFIX):
        return cipher_text
    hex_text = cipher_text[len(CIPHER_PREFIX) :]
    try:
        raw = bytes.fromhex(hex_text)
    except ValueError as exc:
        raise UnipusCryptoError("密文前缀之后不是合法十六进制文本") from exc
    if not raw or len(raw) % BLOCK_SIZE:
        raise UnipusCryptoError(
            f"密文字节必须非空且是 16 的整数倍，实际 {len(raw)} 字节"
        )
    plain = aes128_ecb_decrypt(cipher_key(k), raw)
    plain = strip_trailing_zeros(plain)
    try:
        return plain.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UnipusCryptoError("解密结果不是合法 UTF-8") from exc
