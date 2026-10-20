"""U校园适配器的异常体系。

设计要点：

- 业务码失败与协议结构错误**不能静默吞掉**，一律上抛；
- 只有网络层错误（``kedaibiao.net.http.TransportError``，继承 ``OSError``）
  才交给调度层重试，所以这里的异常**刻意不继承** ``OSError``；
- 限流（``600001``/``600002``）单独成一类，携带规格第 9 节的退避建议，
  但**不在适配器里 sleep** —— 是否等待由调度层决定。
"""

from __future__ import annotations

from typing import Any

from kedaibiao.core.scheduler import current_attempt
from kedaibiao.net.http import TransportError


class UnipusError(Exception):
    """适配器异常基类。"""


class UnipusProtocolError(UnipusError, ValueError):
    """响应与规格不符：结构缺字段、正文不是 JSON、密文不可解析。

    继承 ``ValueError`` 是刻意的：这类错误重试没有意义，应当立刻上抛。
    """


class UnipusCryptoError(UnipusProtocolError):
    """加解密失败（密钥长度、十六进制、块对齐、UTF-8 等任一不合法）。"""


class UnipusAuthError(UnipusError):
    """鉴权失败：401、被重定向到 HTML 登录页、令牌被拒、本地无会话。"""


class UnipusNotLoggedIn(UnipusAuthError):
    """本地会话为空，尚未登录。"""


class UnipusHttpError(UnipusError):
    """HTTP 状态非 2xx，且正文不像 HTML 登录页。

    规格第 5.2 节：状态非 2xx 时**不解析 JSON**，直接带正文摘要报错。
    """

    def __init__(self, *, status: int, url: str, body_preview: str) -> None:
        super().__init__(f"HTTP {status} {url}；正文摘要: {body_preview!r}")
        self.status = status
        self.url = url
        self.body_preview = body_preview


class UnipusApiError(UnipusError):
    """HTTP 通了，但业务码判定为失败。

    ``app`` 是应用家族（``sso`` / ``portal`` / ``ucontent``），
    因为三套成功码不统一，排查时必须知道用的是哪一套判定。
    """

    def __init__(
        self,
        *,
        app: str,
        url: str,
        message: str,
        code: Any = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        detail = f"code={code!r}" if code is not None else "无 code"
        super().__init__(f"[{app}] 业务失败（{detail}）{url}: {message}")
        self.app = app
        self.url = url
        self.code = code
        self.message = message
        self.payload = payload


class UnipusCaptchaRequired(UnipusApiError):
    """SSO 要求图片/滑块验证码（业务码 ``1506``）。

    **这不是「凭据错了」**：密码可能是对的，只是风控要求先过验证码。
    适配器捕获它之后退回「浏览器触发 + 极验求解 + 注入登录」那条已实测链路
    （与 iTEST 共用 :mod:`kedaibiao.platforms.unipus.geetest.browser`）。
    把它和普通业务失败分开，是为了不把「需要验证码」显示成「密码不对」。
    """


class UnipusRateLimited(UnipusApiError, TransportError):
    """撞上限流码（``600001`` / ``600002``）。

    规格第 9 节的退避是 ``20 秒 × (尝试序号 + 1)``，尝试序号从 0 开始，
    最多 4 次。

    **刻意同时继承** ``TransportError``（``OSError`` 子类）：这样
    ``core.scheduler.RETRYABLE`` 自动覆盖它，适配器不必自己写重试；
    而 :attr:`retry_after` 会被 ``Scheduler`` 读取，与本地指数退避取较大值。
    **适配器不 sleep** —— 等待由调度层执行。

    ``retry_after`` 用**调度层的尝试序号**算：不传 ``attempt`` 时读
    ``core.scheduler.current_attempt()``（从 1 起；不在 ``Scheduler.run`` 内
    时为 0，故取下限 1）。在 ``__init__`` 里**当场算好**成普通属性：抛异常的
    那一刻上下文仍然有效，而异常被存下来之后再读上下文已经复位。
    """

    #: 规格第 9 节的退避基数（秒）
    BACKOFF_BASE_SECONDS = 20.0
    #: 规格第 9 节的最大重试次数
    MAX_ATTEMPTS = 4

    def __init__(
        self,
        *,
        app: str,
        url: str,
        message: str,
        code: Any = None,
        payload: dict[str, Any] | None = None,
        attempt: int | None = None,
    ) -> None:
        super().__init__(app=app, url=url, message=message, code=code, payload=payload)
        self.attempt = attempt
        self.retry_after = self.BACKOFF_BASE_SECONDS * self.attempt_number

    @property
    def attempt_number(self) -> int:
        """用于计算等待的尝试序号（**从 1 起**，不在 Scheduler 内也至少为 1）。"""
        if self.attempt is None:
            return max(1, current_attempt())
        try:
            return max(1, int(self.attempt))
        except (TypeError, ValueError):
            return 1

    @property
    def suggested_backoff(self) -> float:
        """:attr:`retry_after` 的别名，供只想读建议值、不接 Scheduler 的调用方使用。"""
        return self.retry_after
