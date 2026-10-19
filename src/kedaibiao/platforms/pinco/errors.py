"""品课适配器异常。

为什么分这么多类型：``core.scheduler`` 只重试「网络抖了」这类
``OSError`` / 超时，业务错误重试没有意义、只会放大问题。异常类型就是
给调度层的分流依据，所以不能什么都塞进一个 ``Exception``。
"""

from __future__ import annotations


class PincoError(RuntimeError):
    """本包所有异常的共同基类。"""


class PincoEndpointUnavailable(PincoError):
    """端点尚未采集 —— 拒绝联网。

    这是本包最要紧的一个异常：没有真实抓包样本时，任何请求都必须停在
    这里，而不是打到一个猜出来的路径上。猜的路径最坏情况不是 404 ——
    是恰好命中了别的接口并返回 200，于是「抓取成功」变成假象。
    """


class PincoNotLoggedIn(PincoError):
    """需要登录态的操作在未登录时被调用。"""


class PincoProtocolError(PincoError):
    """响应不是预期结构（HTML 登录页、非 JSON、关键字段缺失等）。"""


class PincoApiError(PincoError):
    """平台返回了业务错误码。"""

    def __init__(self, message: str, *, code: object = None) -> None:
        super().__init__(message)
        self.code = code
