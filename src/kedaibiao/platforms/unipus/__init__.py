"""U校园（Unipus）平台适配器。

只读链路：登录 → 拉课程 → 拉任务 → 拉题目。提交未实现
（``capabilities.submit is False``）。

模块划分：

- :mod:`~kedaibiao.platforms.unipus.endpoints` —— 基址/路径/请求头/固定参数的**单一出处**
- :mod:`~kedaibiao.platforms.unipus.codes` —— 三套成功码与错误体判定（按应用分开）
- :mod:`~kedaibiao.platforms.unipus.crypto` —— 加解密与 annotator token，**纯函数**
- :mod:`~kedaibiao.platforms.unipus.parsing` —— 响应体 → 内部结构（含 19 位 ID 精度处理）
- :mod:`~kedaibiao.platforms.unipus.client` —— 请求编排，只依赖 ``net.http.HttpClient``
- :mod:`~kedaibiao.platforms.unipus.adapter` —— ``PlatformAdapter`` 实现
"""

from kedaibiao.platforms.unipus.adapter import UnipusAdapter
from kedaibiao.platforms.unipus.client import UnipusClient, UnipusSession
from kedaibiao.platforms.unipus.errors import (
    UnipusApiError,
    UnipusAuthError,
    UnipusCryptoError,
    UnipusError,
    UnipusHttpError,
    UnipusNotLoggedIn,
    UnipusProtocolError,
    UnipusRateLimited,
)

__all__ = [
    "UnipusAdapter",
    "UnipusApiError",
    "UnipusAuthError",
    "UnipusClient",
    "UnipusCryptoError",
    "UnipusError",
    "UnipusHttpError",
    "UnipusNotLoggedIn",
    "UnipusProtocolError",
    "UnipusRateLimited",
    "UnipusSession",
]
