"""U校园协议常量与端点路径的**单一出处**。

所有基址、路径、请求头名、固定参数都只在这里出现一次；``client`` 只引用这里
的常量，不在业务代码里写字面量 URL。

三条容易踩的规则也编码在这里（规格第 1、4 节）：

1. **域名纪律**：每个应用（SSO / 门户 / ucontent）绑死自己的基址与鉴权头，
   调用方只能通过 :func:`url_for` 拼 URL，拼完立刻断言域名，杜绝"把门户令牌
   发到 ucontent"这类事故；
2. **``courseId`` 不能做 URL 编码**：``course-v2:…`` 里的冒号与 ``+`` 必须
   原样出现，所以路径参数用 ``str.format`` 直接替换，**不**走 ``quote``；
3. 内容/答案/进度接口固定带模式路径段 ``default``。
"""

from __future__ import annotations

import urllib.parse
from enum import Enum
from typing import Final

from kedaibiao.platforms.unipus.errors import UnipusProtocolError

# --------------------------------------------------------------------------
# 应用家族：域名 + 鉴权方式 + 成功码，绑定成一个不可自由组合的单位
# --------------------------------------------------------------------------


class UnipusApp(str, Enum):
    """三个业务应用。三者的成功码**不统一**，必须分开判定。"""

    SSO = "sso"
    PORTAL = "portal"
    UCONTENT = "ucontent"


BASE_URLS: Final[dict[UnipusApp, str]] = {
    UnipusApp.SSO: "https://sso.unipus.cn",
    UnipusApp.PORTAL: "https://uai.unipus.cn",
    UnipusApp.UCONTENT: "https://ucontent.unipus.cn",
}

#: 门户 SSO 的 ``service`` 参数固定值
PORTAL_SERVICE: Final[str] = "https://uai.unipus.cn/portal"

# --------------------------------------------------------------------------
# 鉴权头
# --------------------------------------------------------------------------

#: 门户系列接口用门户 JWT
PORTAL_AUTH_HEADER: Final[str] = "Authorization"
#: ucontent 受保护接口**不认** Authorization，只认这个头
UCONTENT_AUTH_HEADER: Final[str] = "x-annotator-auth-token"

#: 门户"微应用宿主"四头（``/api/aca/*`` 与部分 ``/api/tla/*`` 必需）
HOST_HEADER_APP_ID: Final[str] = "u-app-id"
HOST_HEADER_PLATFORM: Final[str] = "u-platform"
HOST_HEADER_SCHOOL: Final[str] = "u-school"
HOST_HEADER_OPENID: Final[str] = "u-openid"

#: 固定常量：应用 ID 与平台枚举（网页端 = 2）
HOST_APP_ID: Final[str] = "116"
HOST_PLATFORM_WEB: Final[str] = "2"

# --------------------------------------------------------------------------
# 端点路径（未标"完整 URL"的，按 :data:`BASE_URLS` 拼接）
# --------------------------------------------------------------------------

# -- SSO（https://sso.unipus.cn，成功码 "0"） ------------------------------
SSO_PASSWORD_LOGIN: Final[str] = "/sso/0.1/sso/cip/login"
SSO_QRCODE_REQUEST: Final[str] = "/sso/7.0/sso/code"
SSO_TICKET_VALIDATE: Final[str] = "/sso/serviceTicket/validate"
SSO_ADD_COOKIE: Final[str] = "/sso/7.0/sso/add/cookie"

# -- 门户（https://uai.unipus.cn，成功码 1） -------------------------------
PORTAL_USER_INFO: Final[str] = "/api/account/user/info"
PORTAL_BOOKSHELF: Final[str] = "/api/cmgt/course/my/bookshelf"
PORTAL_COURSE_LIST: Final[str] = "/api/cmgt/course/getCourseListByStudent"
PORTAL_ACTIVE_FLAG: Final[str] = "/api/product/course/courseActiveFlag"
PORTAL_COURSE_RESOURCE_INFO: Final[str] = (
    "/api/cmgt/course/getCourseResourceInfoById/{course_resource_id}"
)
# 以下两条属于考核/任务树家族，必须带宿主四头；本阶段只登记端点，未接线
PORTAL_PLAN_DETAIL: Final[str] = "/api/aca/plan/detail"
PORTAL_USER_SCORE: Final[str] = "/api/aca/achievement/queryUserScore"
PORTAL_STUDY_STRATEGY: Final[str] = "/api/tla/courseStudyStrategy/detail"
PORTAL_UNIT_TASK_SITUATION: Final[str] = "/api/tla/learningDetail/chapters/unitTaskSituation"

# -- ucontent（https://ucontent.unipus.cn，成功码 0） ----------------------
UCONTENT_CATALOG: Final[str] = "/course/api/course/{course_id}/default"
UCONTENT_CONTENT: Final[str] = "/course/api/v3/content/{course_id}/{group_id}/default"
UCONTENT_ANSWER: Final[str] = "/course/api/v3/answer/{course_id}/{group_id}/default"
#: **唯一的写接口**：规格第 8 节已登记语义，但本阶段只登记端点，**不实现、不接线**
UCONTENT_SUBMIT: Final[str] = "/course/api/v3/newExploration/submit"
UCONTENT_GROUP_PROGRESS: Final[str] = (
    "/api/mobile/user_module/{course_id}/{group_id}/progress/v2"
)
UCONTENT_GROUP_SNAPSHOT: Final[str] = (
    "/api/mobile/user_module/{course_id}/{group_id}-{last_submit}"
)
UCONTENT_COURSE_PROGRESS: Final[str] = (
    "/course/api/v2/course_progress/{course_id}/{open_id}/default"
)
UCONTENT_TASK_PROGRESS: Final[str] = (
    "/course/api/v2/course_progress/{course_id}/tasks/{open_id}/default"
)

#: 内容/答案/进度接口固定使用的模式路径段
MODE_SEGMENT: Final[str] = "default"

# --------------------------------------------------------------------------
# 固定加密/签名参数（规格第 3 节）
# --------------------------------------------------------------------------

#: 登录字段加密：AES-128-CBC，固定密钥与固定 IV（无随机性）
LOGIN_AES_KEY_HEX: Final[str] = "8AD70B641C024C7ADA2ECD082EC0334F"
LOGIN_AES_IV_HEX: Final[str] = "0102030405060708090A0B0C0D0E0F10"

#: annotator token：客户端自铸的 HS256 JWT
ANNOTATOR_SIGNING_SECRET: Final[str] = "example-annotator-signing-secret-0001"
ANNOTATOR_ISSUER: Final[str] = "example-annotator-issuer"
ANNOTATOR_AUDIENCE: Final[str] = "edx.unipus.cn"
#: exp = 当前毫秒 + 31536000000（≈1 年）
ANNOTATOR_TTL_MS: Final[int] = 31_536_000_000

#: 内容/答案密文前缀
CIPHER_PREFIX: Final[str] = "unipus."
#: 密文密钥前缀（真实密钥 = 该前缀 + 响应里的 k，长度必须恰好 16 字节）
CIPHER_KEY_PREFIX: Final[str] = "1a2b3c4d"

# --------------------------------------------------------------------------
# 成功码与业务码（判定函数见 ``codes.py``）
# --------------------------------------------------------------------------

#: SSO：字符串 "0"（源码同时接受数字 0）
SSO_SUCCESS_CODES: Final[frozenset[object]] = frozenset({"0", 0})
#: 门户：数字 1（0 会被判失败）
PORTAL_SUCCESS_CODES: Final[frozenset[object]] = frozenset({1})
#: ucontent：数字 0（1 会被判失败）
UCONTENT_SUCCESS_CODES: Final[frozenset[object]] = frozenset({0})

#: 唯二被当作限流的业务码
RATE_LIMIT_CODES: Final[frozenset[int]] = frozenset({600001, 600002})
#: 缺宿主四头（或 u-school 不对）时的业务码
FEATURE_DENIED_CODE: Final[int] = 6011

#: 门户主机的域名（域名断言的期望值）
_EXPECTED_HOSTS: Final[dict[UnipusApp, str]] = {
    UnipusApp.SSO: "sso.unipus.cn",
    UnipusApp.PORTAL: "uai.unipus.cn",
    UnipusApp.UCONTENT: "ucontent.unipus.cn",
}


# --------------------------------------------------------------------------
# URL 拼接与域名断言
# --------------------------------------------------------------------------


def assert_url_for_app(app: UnipusApp, url: str) -> None:
    """断言 ``url`` 确实指向 ``app`` 的域名。

    规格第 1 节记载的历史事故是把门户令牌发到了 ucontent 域名，所以这道
    断言放在**每次发请求之前**，而不是只放在测试里。
    """
    base = BASE_URLS[app]
    if not url.startswith(base + "/"):
        raise UnipusProtocolError(
            f"URL 域名与应用不匹配：app={app.value} 期望基址 {base}，实际 {url}"
        )
    host = urllib.parse.urlsplit(url).hostname
    if host != _EXPECTED_HOSTS[app]:
        raise UnipusProtocolError(f"URL 主机与应用不匹配：app={app.value}，实际主机 {host}")


def url_for(app: UnipusApp, path: str, **params: object) -> str:
    """把路径模板按 ``app`` 的基址拼成完整 URL。

    **不做百分号编码**：``courseId``（``course-v2:…``）里的冒号与 ``+`` 必须
    原样出现（规格第 4 节硬规则 1）。
    """
    try:
        rendered = path.format(**params) if params else path
    except KeyError as exc:
        raise UnipusProtocolError(f"路径模板 {path!r} 缺少参数: {exc}") from exc
    url = BASE_URLS[app] + rendered
    assert_url_for_app(app, url)
    return url


def query_url(base_url: str, **params: object) -> str:
    """给 URL 追加查询串。

    逗号保持字面量（``tasks`` 参数必须是逗号分隔），其余按 query 规则编码。
    """
    if not params:
        return base_url
    items: list[tuple[str, str]] = []
    for key, value in params.items():
        if value is None:
            continue
        if isinstance(value, bool):
            text = "true" if value else "false"
        else:
            text = str(value)
        items.append((key, text))
    encoded = urllib.parse.urlencode(items, quote_via=urllib.parse.quote, safe=",")
    return f"{base_url}?{encoded}"


def comma_join(values: list[str] | tuple[str, ...]) -> str:
    """分组 ID 批量参数：**必须**逗号分隔（空格 / JSON 数组 / ``all`` 都会静默空结果）。"""
    return ",".join(values)
