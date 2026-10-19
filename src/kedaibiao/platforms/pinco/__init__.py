"""希沃品课（Pinco）学生端适配器包。

对外暴露适配器本体，以及接线时会用到的几个类型（计划、提交被拒异常）。
注册（entry point 或手工 ``register``）由 Lead 统一做 —— 平台包不许改共享的
注册文件。

包内分层（依赖只能自上向下）：

```
adapter.py    契约实现：抓取 / 计划 / 提交守卫入口
  ├─ solving.py   作答 + 计划生成（谁需要人工）
  ├─ sso.py       SSO 密码登录（实测跑通；只解决身份层）
  ├─ crypto.py    DER 取模数 + PKCS#1 v1.5（纯标准库）
  ├─ guard.py     提交三条硬规则
  ├─ plan.py      计划结构与内容哈希
  ├─ client.py    RPC 调用（唯一出口，含危险动作闸门与双层判错）
  ├─ parsing.py   信封与业务字段解析
  ├─ question_map.py  题型翻译 + 图片题识别
  ├─ endpoints.py 常量单一出处（含证据等级）
  ├─ models.py    本包自用数据结构
  └─ errors.py    异常类型（给 core.scheduler 分流用）
```
"""

from kedaibiao.platforms.pinco.adapter import PincoAdapter
from kedaibiao.platforms.pinco.errors import PincoError
from kedaibiao.platforms.pinco.guard import SubmissionRefused
from kedaibiao.platforms.pinco.plan import PincoPlan

__all__ = ["PincoAdapter", "PincoError", "PincoPlan", "SubmissionRefused"]
