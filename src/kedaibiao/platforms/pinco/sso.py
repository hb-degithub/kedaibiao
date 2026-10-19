"""品课 SSO 账号密码登录（Lead 实测跑通）。

实测链路（账号验证成功、返回真实用户资料、statusCode 200）：

1. ``POST {SSO}/common/getPublicKeyV3`` 表单 → base64 DER(SPKI)；
2. 用公钥加密 ``username`` 与 ``MD5(password)`` 的十六进制摘要；
3. ``POST {SSO}/login/v3`` 表单 → ``data.data.token``；
4. ``POST {SSO}/common/setToken`` 表单 ``{tk: token}`` → 在 ``.seewo.com`` 域
   下发 cookie ``x-token`` / ``x-samesite-none-token``。

**必须写进设计的边界**：SSO 登录成功 **≠** 应用层 RPC 可用。
``pinco.seewo.com/student/api.json`` 的 csrf 与浏览器会话上下文绑定，纯 HTTP
复刻不出来（实测：无论怎么组合 csrf cookie/header 都回
``403 invalid csrf token``）。所以会话的主路径是「浏览器导出 cookie →
``PincoAdapter.adopt_session()``」，密码登录只解决 SSO 那一层。
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from kedaibiao.platforms.pinco import crypto, endpoints as ep
from kedaibiao.platforms.pinco.client import PincoClient
from kedaibiao.platforms.pinco.errors import PincoApiError, PincoProtocolError

log = logging.getLogger(__name__)

#: ``getPublicKeyV3`` 响应里公钥可能出现的键（实测是 ``data`` 直接给字符串，
#: 包一层的情况先在候选里兜着，认不出来就报错，不猜）。
_PUBLIC_KEY_KEYS = ("publicKey", "publicKeyStr", "key", "value", "data")


def now_ms() -> int:
    """当前 Unix 毫秒。登录表单里的 ``rst`` / ``lst`` 用它。"""
    return int(time.time() * 1000)


@dataclass(slots=True, frozen=True)
class SsoLoginResult:
    """SSO 登录结果。

    ``token`` 是凭据，``__repr__`` 已屏蔽 —— 别绕过它去打日志。
    """

    token: str
    user: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    def __repr__(self) -> str:  # 防止 SSO token 经日志/异常栈泄露
        return f"SsoLoginResult(token=***, user_keys={sorted(self.user)[:8]})"


def _public_key_text(payload: Any) -> str:
    """从 ``getPublicKeyV3`` 的响应里取出 base64 公钥（实测：``{"data": "<b64>"}``）。"""
    if isinstance(payload, str) and payload.strip():
        return payload.strip()
    if isinstance(payload, dict):
        for key in _PUBLIC_KEY_KEYS:
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, dict):
                for inner in _PUBLIC_KEY_KEYS:
                    nested = value.get(inner)
                    if isinstance(nested, str) and nested.strip():
                        return nested.strip()
    raise PincoProtocolError(
        f"取不到登录公钥（响应结构与实测样本不符）：{str(payload)[:120]}"
    )


def _extract_login(payload: Any) -> SsoLoginResult:
    """从 ``login/v3`` 的响应里取 token 与用户资料。

    实测形状：``{"data": {"statusCode": 200, "data": {"user": {...}, "token": "..."}}}``。
    外层与内层都要看 —— 这也是「双层判错」的一部分：HTTP 200 不代表业务成功。
    """
    if not isinstance(payload, dict):
        raise PincoProtocolError(
            f"SSO 登录响应不是对象（{type(payload).__name__}），无法判定结果。"
        )
    outer = payload.get(ep.FIELD_SSO_INNER_DATA)
    if not isinstance(outer, dict):
        outer = payload
    inner = outer.get(ep.FIELD_SSO_INNER_DATA)
    if not isinstance(inner, dict):
        inner = {}
    status = outer.get(ep.FIELD_STATUS_CODE)
    message = (
        _text(outer.get(ep.FIELD_MSG))
        or _text(outer.get(ep.FIELD_MESSAGE))
        or _text(payload.get(ep.FIELD_MSG))
        or _text(payload.get(ep.FIELD_MESSAGE))
    )
    token = inner.get(ep.FIELD_SSO_TOKEN_FIELD) or outer.get(ep.FIELD_SSO_TOKEN_FIELD)
    if isinstance(token, str) and token:
        user = inner.get(ep.FIELD_SSO_USER_FIELD)
        return SsoLoginResult(
            token=token,
            user=user if isinstance(user, dict) else {},
            raw=payload,
        )
    details = f"（statusCode={status!r}，message={message!r}）"
    if status not in (None, 0, 200, "0", "200"):
        raise PincoApiError(f"SSO 登录失败{details}", code=status)
    raise PincoProtocolError(
        f"SSO 登录响应里没有 token{details}；结构可能与实测样本不符。"
    )


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


async def login_with_password(
    client: PincoClient,
    username: str,
    password: str,
    *,
    clock: Callable[[], int] = now_ms,
) -> SsoLoginResult:
    """走一遍实测过的 SSO 密码登录，并把 token 写进 cookie。

    ``clock`` 可注入，测试里用它固定 ``rst`` / ``lst``。
    """
    data_id = str(uuid.uuid4())

    key_payload = await client.post_form_raw(
        ep.sso_url(ep.SSO_PUBLIC_KEY_PATH),
        {
            ep.FIELD_SSO_DATA_ID: data_id,
            ep.FIELD_SSO_IS_FORCE_SYSTEM: "true",
            ep.FIELD_SSO_IS_IFRAME: "1",
            ep.FIELD_SSO_SYSTEM: ep.SSO_SYSTEM,
        },
    )
    modulus, exponent = crypto.decode_public_key(_public_key_text(key_payload))

    # rst / lst 是「加密开始」与「请求前」的毫秒时间戳，实测表单里必须带
    started = clock()
    encrypted_username, encrypted_password = crypto.encrypt_credentials(
        username, password, modulus, exponent
    )
    before_request = clock()

    payload = await client.post_form_raw(
        ep.sso_url(ep.SSO_LOGIN_PATH),
        {
            ep.FIELD_SSO_DATA_ID: data_id,
            ep.FIELD_SSO_USERNAME: encrypted_username,
            ep.FIELD_SSO_PASSWORD: encrypted_password,
            ep.FIELD_SSO_CAPTCHA: "",
            ep.FIELD_SSO_SYSTEM: ep.SSO_SYSTEM,
            ep.FIELD_SSO_CLIENT_TYPE: ep.SSO_CLIENT_TYPE,
            ep.FIELD_SSO_IS_RE_ME: "0",
            ep.FIELD_SSO_SECURE: "true",
            ep.FIELD_SSO_WITH_TICKET: "1",
            ep.FIELD_SSO_IS_IFRAME: "1",
            ep.FIELD_SSO_PHONE_COUNTRY_CODE: ep.SSO_PHONE_COUNTRY_CODE,
            ep.FIELD_SSO_RST: str(started),
            ep.FIELD_SSO_LST: str(before_request),
        },
    )
    result = _extract_login(payload)

    await client.post_form_raw(
        ep.sso_url(ep.SSO_SET_TOKEN_PATH), {ep.FIELD_SSO_TOKEN: result.token}
    )
    log.info("SSO 登录成功（用户资料字段 %d 个）", len(result.user))
    return result
