"""成功码与错误体判定。

规格第 5.1 节的硬要求：**三套成功码不统一，必须按应用分别判定，不许写一个
通用的 ``is_ok``**。所以这里提供三个并列函数，各自只认自己那一套：

===========  ==========================  ====================
应用          成功判定                     说明
===========  ==========================  ====================
SSO          ``code == "0"``（数字 0 也认）  ``1`` 判失败
门户          ``code == 1``（数字）         ``0`` 判失败
ucontent     ``code == 0``（数字）         ``1`` 判失败
密文外壳       ``code`` 存在且 ``!= 0`` 才失败  取不到 ``code`` 视为不失败
===========  ==========================  ====================

布尔值单独挡掉：Python 里 ``True == 1``、``False == 0``，不挡就会把
``{"code": true}`` 误判成成功。
"""

from __future__ import annotations

from typing import Any

from kedaibiao.platforms.unipus.endpoints import (
    PORTAL_SUCCESS_CODES,
    RATE_LIMIT_CODES,
    SSO_SUCCESS_CODES,
    UCONTENT_SUCCESS_CODES,
)
from kedaibiao.platforms.unipus.errors import UnipusProtocolError

#: 错误消息字段的取值优先级（规格第 5.2 节）
MESSAGE_KEYS: tuple[str, ...] = ("message", "msg", "error", "errMsg")

#: 都取不到时的兜底文案
FALLBACK_MESSAGE = "平台未返回错误信息"


def _body_dict(body: Any) -> dict[str, Any] | None:
    return body if isinstance(body, dict) else None


def _has_code(body: Any) -> bool:
    payload = _body_dict(body)
    return payload is not None and "code" in payload


def _code_matches(body: Any, expected: frozenset[object]) -> bool:
    payload = _body_dict(body)
    if payload is None or "code" not in payload:
        return False
    code = payload["code"]
    if isinstance(code, bool):  # True/False 不等于 1/0，先挡掉
        return False
    return code in expected


def ok_sso(body: Any) -> bool:
    """SSO 家族成功判定：``code`` 为字符串 ``"0"``，数字 ``0`` 也接受。"""
    return _code_matches(body, SSO_SUCCESS_CODES)


def ok_portal(body: Any) -> bool:
    """门户家族成功判定：``code`` 为数字 ``1``。"""
    return _code_matches(body, PORTAL_SUCCESS_CODES)


def ok_ucontent(body: Any) -> bool:
    """ucontent 家族成功判定：``code`` 为数字 ``0``。"""
    return _code_matches(body, UCONTENT_SUCCESS_CODES)


def cipher_shell_failed(body: Any) -> bool:
    """内容/答案密文外壳：``code != 0`` 即失败，**取不到 ``code`` 视为不失败**。"""
    payload = _body_dict(body)
    if payload is None or "code" not in payload:
        return False
    code = payload["code"]
    if isinstance(code, bool):
        return False
    return code != 0


def result_code(body: Any) -> Any:
    """取出响应里的 ``code``（取不到返回 ``None``），仅用于报错与日志。"""
    payload = _body_dict(body)
    if payload is None:
        return None
    return payload.get("code")


def is_rate_limited(body: Any) -> bool:
    """``600001`` / ``600002`` 是唯二被当作限流的业务码。"""
    code = result_code(body)
    if isinstance(code, bool) or code is None:
        return False
    return code in RATE_LIMIT_CODES


def extract_message(body: Any) -> str:
    """按 ``message`` → ``msg`` → ``error`` → ``errMsg`` 取第一个非空字符串。

    都取不到时用兜底文案（规格第 5.2 节）。**绝不把整个响应体拼进消息**
    ——响应体里可能带令牌。
    """
    payload = _body_dict(body)
    if payload is not None:
        for key in MESSAGE_KEYS:
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, dict):
                nested = value.get("message") or value.get("msg")
                if isinstance(nested, str) and nested.strip():
                    return nested.strip()
    return FALLBACK_MESSAGE


def looks_like_html(text: str) -> bool:
    """正文是否像 HTML 页面（401 的真实形态是 HTML 登录页）。

    判据按规格第 5.2 节：以 ``<`` 开头，或含 ``<html``（忽略大小写）。
    """
    if not text:
        return False
    stripped = text.lstrip("\ufeff \t\r\n")
    if stripped.startswith("<"):
        return True
    return "<html" in stripped.lower()


def login_page_message(url: str) -> str:
    """HTML 登录页的统一提示文案（不打印令牌、不打印响应体）。"""
    return (
        f"{url} 返回的是 HTML 页面而不是 JSON，"
        "多半是鉴权失败被重定向到登录页（规格第 5.2、9 节）"
    )


#: SSO 风控要求图片/滑块验证码时返回的业务码（2026-10-20 实测）
CAPTCHA_REQUIRED_CODE: int = 1506


def is_captcha_required(body: Any) -> bool:
    """响应是不是「需要验证码」（``code == 1506``）。

    只认 SSO 登录这一步；但判定放在这里是因为「错误体长什么样」属于本模块的职责。
    数字/字符串 ``"1506"`` 都接受 —— 平台的 code 类型并不稳定。
    """
    code = result_code(body)
    if isinstance(code, bool) or code is None:
        return False
    try:
        return int(str(code).strip()) == CAPTCHA_REQUIRED_CODE
    except (TypeError, ValueError):
        return False


# --------------------------------------------------------------------------
# 按应用分派（客户端只传"这个接口属于哪个应用/哪套判定"）
# --------------------------------------------------------------------------

#: 内容/答案的密文外壳：判定规则与 ucontent 家族**不同**（缺 code 不算失败）
CIPHER_SHELL_FAMILY = "cipher_shell"

_CHECKERS = {
    "sso": ok_sso,
    "portal": ok_portal,
    "ucontent": ok_ucontent,
    CIPHER_SHELL_FAMILY: lambda body: not cipher_shell_failed(body),
}


def success_check(family: str, body: Any) -> bool:
    """按应用家族判定成功。

    ``family`` 由调用点显式给出（``"sso"`` / ``"portal"`` / ``"ucontent"`` /
    ``"cipher_shell"``），**没有**默认值 —— 规格第 5.1 节要求"按应用分别判定"，
    给默认值就等于给了一条会被误用的通用路径。
    """
    checker = _CHECKERS.get(family)
    if checker is None:
        raise UnipusProtocolError(f"未知的成功码判定家族: {family!r}")
    return checker(body)
