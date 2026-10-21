"""超星 / 学习通：只读课程与作业。

## 这个包做什么

只回答一个问题：**我的账号下有哪些课、哪些作业和考试、什么时候截止。**

## 这个包不做什么

不交作业、不推进度、不签到、不碰任何写接口。适配器的 ``Capabilities`` 里
``submit=False`` / ``course_playback=False`` / ``fetch_questions=False`` 就是
这条边界在代码里的写法 —— 流水线看到能力没开，对应阶段直接跳过，不存在
「配错了就悄悄交了」的路径。

## 组成

- :mod:`endpoints` —— 只读入口（候选，待首跑校准）；
- :mod:`parsing` —— HTML → 模型（纯函数，可离线测）；
- :mod:`client` —— CLI / 独立看板共用的取数路径；
- :mod:`adapter` —— 注册进框架的适配器，控制台「课程 / 作业」页直接用它。
"""

from kedaibiao.platforms.chaoxing import client, endpoints, parsing
from kedaibiao.platforms.chaoxing.adapter import (
    ChaoxingAdapter,
    ChaoxingError,
    ChaoxingLoginError,
)

__all__ = [
    "ChaoxingAdapter",
    "ChaoxingError",
    "ChaoxingLoginError",
    "client",
    "endpoints",
    "parsing",
]
