"""批改网（pigai）**只读**适配器包。

## 注册方式：不需要改任何共享文件

``platforms.load_builtin_adapters()`` 会自动发现本目录下的子包，
把模块命名空间里 ``PlatformAdapter`` 的具体子类按 ``cls.name`` 注册
（见 ``platforms/__init__.py`` 与 docs/integration-guide.md §7）。

所以本包只要把 ``PigaiAdapter`` 暴露在 ``__init__`` 命名空间里就完成了注册 ——
**既不碰共享的 ``platforms/__init__.py``，也不碰 ``pyproject.toml``。**
（entry point 只在装成 wheel 发布时才需要，那份注册由总 lead 统一加。）

## 能力边界

只读：作业列表 / 截止时间 / 已提交作文 / 历史分数 / 逐句批注。
``Capabilities.submit`` 与 ``fetch_questions`` 均为 ``False``。
详见 ``adapter.py`` 的模块 docstring（含 ``remote_id`` 约定与「别用 Pipeline 读数据」的说明）。
"""

from kedaibiao.platforms.pigai.adapter import PigaiAdapter, SessionExpiredError
from kedaibiao.platforms.pigai.client import PigaiClient, PigaiError, PigaiResponseError
from kedaibiao.platforms.pigai.parse import PigaiParseError
from kedaibiao.platforms.pigai.protocol import Endpoints

__all__ = [
    "Endpoints",
    "PigaiAdapter",
    "PigaiClient",
    "PigaiError",
    "PigaiParseError",
    "PigaiResponseError",
    "SessionExpiredError",
]
