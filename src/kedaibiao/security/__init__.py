"""安全层：凭据装载与脱敏输出。

本包的代码**不得**把密码、令牌、cookie 写入日志、异常消息或返回值 repr。
"""

from kedaibiao.security.credentials import (
    KNOWN_EXTRA_FIELDS,
    CredentialError,
    CredentialSource,
    available_platforms,
    describe,
    env_key,
    load_credentials,
)

__all__ = [
    "KNOWN_EXTRA_FIELDS",
    "CredentialError",
    "CredentialSource",
    "available_platforms",
    "describe",
    "env_key",
    "load_credentials",
]
