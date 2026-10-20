"""U校园（Unipus）平台适配器。

链路：登录（含验证码回退）→ 拉课程 → 拉任务 → 拉题目 → 提交。
提交**默认干跑**：只读内容与标准答案构造计划，``submit_live=True`` 才真发。
学习时长与语音评测见 :mod:`~kedaibiao.platforms.unipus.duration` /
:mod:`~kedaibiao.platforms.unipus.speech`，同样默认干跑。

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
