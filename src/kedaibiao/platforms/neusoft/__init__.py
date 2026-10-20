"""东软智慧教育平台（NEUMOOC）适配器。

协议事实见 ``docs/research/neusoft.md``；模块分工：

- :mod:`~kedaibiao.platforms.neusoft.signing` —— ``Id-Code`` 签名（AES-CBC + 零填充）
- :mod:`~kedaibiao.platforms.neusoft.client` —— 请求层（公共头、外壳解包、401 自动重登）
- :mod:`~kedaibiao.platforms.neusoft.terms` —— 学期
- :mod:`~kedaibiao.platforms.neusoft.resources` —— 课程与资源
- :mod:`~kedaibiao.platforms.neusoft.study` —— 刷课（视频 / 文档，**以回读确认完成**）
- :mod:`~kedaibiao.platforms.neusoft.homework` —— 学习任务与作业答题
- :mod:`~kedaibiao.platforms.neusoft.attendance` —— 考勤与签到

本平台的两条硬规则：**免验证码走 App 登录**，**``code == 0`` 不等于完成**。
"""

from __future__ import annotations

from kedaibiao.platforms.neusoft.adapter import NeusoftAdapter

__all__ = ["NeusoftAdapter"]
