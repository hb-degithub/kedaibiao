"""课代表 kedaibiao —— 多平台作业 / 课程一体化自动化框架。

顶层只暴露版本号与全局注册表，避免 import 时拉起重依赖。
"""

from __future__ import annotations

from kedaibiao.core.registry import Registry

__version__ = "0.1.0"

#: 平台适配器注册表（类型为 ``type[PlatformAdapter]``，此处用 Any 避免循环导入）
adapters: Registry[object] = Registry("平台适配器")
#: 答题器注册表
answerers: Registry[object] = Registry("答题器")
#: 通知渠道注册表
notifiers: Registry[object] = Registry("通知渠道")

__all__ = ["__version__", "adapters", "answerers", "notifiers"]
