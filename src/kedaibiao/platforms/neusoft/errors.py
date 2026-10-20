"""东软智慧教育平台的错误类型。

分三类，调用方据此决定「重试 / 重登 / 跳过」：

- :class:`NeusoftTransportError` —— 网络层失败。**不在本模块**，直接复用
  ``net.http.TransportError``（它继承 ``OSError``，调度器自带重试）；
- :class:`NeusoftApiError` —— 业务错误，带平台 ``code`` 与 ``msg``。
  重试没有意义，但 401 那一个分支要触发重登；
- :class:`NeusoftProtocolError` —— 响应形状不对（不是 JSON、缺 ``code`` 等）。
  这类通常意味着平台改版了，必须让人看见，不能静默跳过。
"""

from __future__ import annotations

#: 平台表示「登录态失效」的业务码。401 也会以 HTTP 状态码形式出现，两处都要认。
AUTH_FAILED_CODES = frozenset({401, 1004010001, 1010070001})


class NeusoftError(Exception):
    """本平台所有错误的基类。"""


class NeusoftProtocolError(NeusoftError):
    """响应与协议不符 —— 平台可能改版了。"""


class NeusoftApiError(NeusoftError):
    """平台明确返回了业务错误（``code != 0``）。"""

    def __init__(self, code: int | str, message: str = "", path: str = "") -> None:
        self.code = code
        self.message = message
        self.path = path
        where = f" @ {path}" if path else ""
        super().__init__(f"code={code} {message}{where}")

    @property
    def is_auth_error(self) -> bool:
        try:
            return int(self.code) in AUTH_FAILED_CODES
        except (TypeError, ValueError):
            return False


class NeusoftNotLoggedIn(NeusoftApiError):
    """没有可用会话（未登录、或会话已被服务端吊销且无法重登）。"""

    def __init__(self, message: str = "登录态失效，且没有可用于重登的凭据") -> None:
        super().__init__(401, message)
