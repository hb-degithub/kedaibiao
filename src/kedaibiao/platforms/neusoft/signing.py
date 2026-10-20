"""`Id-Code` 请求头签名。

平台每个请求都要带它，包括登录。算法（``docs/research/neusoft.md`` §1.2）：::

    明文   = "{userId}_{uuid}_{相对路径}"      # 未登录时 userId 记为 "0"
    密文   = AES-128-CBC(key = iv = "neuedu_nse_12345", 零填充, 明文)
    Id-Code = base64(密文)

四条踩过的坑，全部用测试钉住：

1. key 与 iv **是同一个常量**；
2. **零填充不是 PKCS#7**：按 16 向上取整，恰好整除时**不补块**；空明文补一个整块；
3. 明文用**相对路径**（``/web-api/...``），**不含域名与 query**；
4. 无 timestamp / nonce，服务端只校验能解密 —— uuid 用随机 v4 即可。

AES 原语复用 ``platforms/unipus/crypto.py`` 里的纯 Python 实现（同样的约定：
核心层与平台层零第三方依赖，标准库没有 AES）。
"""

from __future__ import annotations

import base64
import uuid as uuid_module

from kedaibiao.platforms.neusoft.errors import NeusoftProtocolError
from kedaibiao.platforms.unipus.crypto import BLOCK_SIZE, aes128_cbc_encrypt

#: 平台固定密钥，**key 与 iv 是同一个值**。
ID_CODE_KEY = b"neuedu_nse_12345"

#: 未登录时的 userId 占位值（APK 行为）。
ANONYMOUS_USER_ID = "0"


def zero_pad(data: bytes) -> bytes:
    """AES-CBC 用的零填充。

    与 PKCS#7 的区别就在这里：**恰好整除时不追加整块**。
    用 PKCS#7 的话，长度正好是 16 倍数的明文会多出一个块，
    服务端解出来的明文尾部多 16 个 ``\\0``，签名直接失效。
    """
    if not data:
        return b"\x00" * BLOCK_SIZE
    remainder = len(data) % BLOCK_SIZE
    if remainder == 0:
        return data
    return data + b"\x00" * (BLOCK_SIZE - remainder)


def aes_cbc_zero_padding_b64(text: str) -> str:
    """AES-128-CBC + 零填充 + 标准 base64（带 ``=`` 填充）。"""
    padded = zero_pad(text.encode("utf-8"))
    try:
        cipher = aes128_cbc_encrypt(ID_CODE_KEY, ID_CODE_KEY, padded)
    except Exception as exc:  # noqa: BLE001 - 底层抛的是 UnipusCryptoError
        raise NeusoftProtocolError(f"Id-Code 加密失败: {exc}") from exc
    return base64.b64encode(cipher).decode("ascii")


def normalize_sign_path(path: str) -> str:
    """把路径规范成签名要的形状：以 ``/`` 开头，**丢掉 query 与域名**。

    平台签名只覆盖相对路径。带 query 进去不会立刻报错，
    只会在服务端得到一个笼统的 401 —— 所以这里主动截断。
    """
    raw = (path or "").strip()
    if not raw:
        return "/"
    # 绝对 URL 只取 path 部分
    if "://" in raw:
        after_scheme = raw.split("://", 1)[1]
        slash = after_scheme.find("/")
        raw = after_scheme[slash:] if slash >= 0 else "/"
    for separator in ("?", "#"):
        cut = raw.find(separator)
        if cut >= 0:
            raw = raw[:cut]
    return raw if raw.startswith("/") else f"/{raw}"


def build_id_code_with_uuid(
    user_id: str | None,
    path: str,
    request_uuid: str,
) -> str:
    """可注入 uuid 的版本 —— 让签名**可复现**，测试与对账靠它。"""
    uid = ANONYMOUS_USER_ID if not user_id else str(user_id)
    plain = f"{uid}_{request_uuid}_{normalize_sign_path(path)}"
    return aes_cbc_zero_padding_b64(plain)


def build_id_code(user_id: str | None, path: str) -> str:
    """构造 ``Id-Code`` 请求头。每次调用用新的随机 uuid。"""
    return build_id_code_with_uuid(user_id, path, str(uuid_module.uuid4()))
