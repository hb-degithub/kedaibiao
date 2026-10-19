"""平台适配器层。每个平台一个子包，只依赖 ``core``。"""

from kedaibiao.platforms.base import (
    AdapterContext,
    Capabilities,
    NotSupportedError,
    PlatformAdapter,
)

__all__ = ["AdapterContext", "Capabilities", "NotSupportedError", "PlatformAdapter"]
