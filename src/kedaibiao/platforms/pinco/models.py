"""本包自用的数据模型。

为什么不复用 ``core.models`` 里同名的结构：核心是另一个会话在维护的共享
文件，平台包的独立性不能建立在别人的未提交改动上。这里只放本包真正需要的
东西，字段也按品课的语义命名。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class PlanState(str, Enum):
    """一个作业在计划里的状态。"""

    #: 逐题都真答出来了（题库命中或人工录入）且达到置信度门槛 —— 复核后可提交
    PLANNED = "planned"
    #: 有图片题 / 没真答出来的题 —— 必须人工处理，**绝不提交**
    NEEDS_HUMAN = "needs_human"
    #: 平台已完成 / 没有题目
    SKIPPED = "skipped"
    #: 抓取或作答阶段失败
    FAILED = "failed"


@dataclass(slots=True, frozen=True)
class PincoSubmissionState:
    """平台侧「这个作业交了没 / 还能不能交」的快照。

    ``None``（探测不到）与 ``submitted=False``（确定没交）必须严格区分：
    前者意味着无法排除「已经交过」，只能拒绝提交；后者才允许继续。

    实测事实：``submitted`` / ``expired`` / ``canSubmitted`` **互相独立**，
    所以「没交过」不足以说明「能交」。三个标记都要带回来给守卫判断。
    """

    assignment_id: str
    submitted: bool
    #: 平台是否显示已过期（实测：与 submitted 独立）。
    expired: bool = False
    #: 平台是否允许提交（``canSubmitted``）；``None`` = 平台没给这个字段。
    can_submit: bool | None = None
    score: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class PincoClass:
    """品课班级。实测：``GetV1StudentClassList`` → ``data[]``（裸数组）。"""

    class_id: str
    name: str = ""
    teacher: str = ""
    student_count: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class PincoMaterialDetail:
    """课程材料详情。

    ⚠️ ``download_url`` **带签名/时效**（实测形如
    ``/server-main/api/resource/<id>/url?…``）：只当次使用，**不要落库、
    不要写进计划文件** —— 过期链接和错误链接一样会把人带沟里。
    """

    material_id: str
    name: str = ""
    download_url: str = ""
    downloadable: bool | None = None
    progress: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class PincoSession:
    """登录成功后拿到的会话信息。

    ``storage`` 是平台自己的登录态原始载荷（cookie / token 等），提交时可能要
    原样回传，所以留着；但**绝不进日志、绝不进计划文件** —— ``__repr__`` 已经
    把它的值屏蔽掉，别绕过它去打印 ``storage``。
    """

    username: str
    logged_in: bool
    storage: dict[str, Any] = field(default_factory=dict)
    expires_at: datetime | None = None

    def __repr__(self) -> str:  # 防止令牌经日志/异常栈泄露
        keys = ", ".join(sorted(self.storage)) or "无"
        return (
            f"PincoSession(username={self.username!r}, logged_in={self.logged_in}, "
            f"storage_keys=[{keys}])"
        )
