"""Web 控制台。

分两层：

- :mod:`~kedaibiao.api.service` —— 数据与动作，**与 HTTP 框架无关**；
- :mod:`~kedaibiao.api.server` —— 标准库 ``http.server`` 外壳（路由 + JSON + 安全检查）。

接口契约见 ``docs/web-console-api.md``。
"""

from kedaibiao.api.service import ConsoleError, ConsoleService

__all__ = ["ConsoleError", "ConsoleService"]
