"""东软智慧教育平台的协议侧模型。

只放**跨模块共用**的结构。作业、考勤各自的解析产物放在各自模块里，
避免这个文件变成并行开发的冲突热点。

字段一律用字符串接收：平台同一个字段时而数字、时而字符串（``studyStatus``、
``questionType`` 都是这样），在解析层统一成 ``str`` 比在每个调用点判断便宜。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from kedaibiao.platforms.neusoft import endpoints as ep


def as_str(value: Any) -> str:
    """把一个可能是数字/字符串/null 的字段读成 ``str``。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(int(value)) if float(value).is_integer() else str(value)
    return str(value)


def as_int(value: Any, default: int = 0) -> int:
    """把可能是字符串的字段读成 ``int``；读不出来给默认值。"""
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def as_list(value: Any) -> list[Any]:
    """三态字段（数组 / 逗号串 / null）统一成列表。"""
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return [value]


@dataclass(slots=True)
class Session:
    """登录会话。

    这个平台的会话**不只在 cookie 里** —— 业务请求靠 bearer token，而账号库
    不存密码。所以会话必须能导出/导入落盘，否则进程一重启就卡在「要密码」上。
    """

    user_id: str = ""
    tenant_id: str = ""
    access_token: str = ""
    refresh_token: str = ""
    expires_time: int = 0

    @classmethod
    def from_payload(cls, payload: Any) -> Session:
        """从登录/刷新接口的 ``data`` 解析。缺 ``accessToken`` 由调用方判失败。"""
        if not isinstance(payload, dict):
            return cls()
        return cls(
            user_id=as_str(payload.get("userId") or payload.get("user_id")),
            tenant_id=as_str(payload.get("tenantId") or payload.get("tenant_id")),
            access_token=as_str(payload.get("accessToken") or payload.get("access_token")),
            refresh_token=as_str(payload.get("refreshToken") or payload.get("refresh_token")),
            expires_time=as_int(payload.get("expiresTime") or payload.get("expires_time")),
        )

    @property
    def is_expired(self) -> bool:
        """本地是否已过期（毫秒时间戳）。

        ⚠️ **只能用来省一次必然失败的请求**，不能当作「token 有效」的依据 ——
        这个平台的 token 会被服务端提前吊销。真正的兜底是 :meth:`NeusoftClient.with_reauth`。
        """
        return self.expires_time > 0 and _now_ms() >= self.expires_time

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "tenant_id": self.tenant_id,
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_time": self.expires_time,
        }

    @classmethod
    def from_dict(cls, data: Any) -> Session:
        if not isinstance(data, dict):
            return cls()
        return cls(
            user_id=as_str(data.get("user_id")),
            tenant_id=as_str(data.get("tenant_id")),
            access_token=as_str(data.get("access_token")),
            refresh_token=as_str(data.get("refresh_token")),
            expires_time=as_int(data.get("expires_time")),
        )


def _now_ms() -> int:
    import time

    return int(time.time() * 1000)


@dataclass(slots=True, frozen=True)
class Term:
    """学期。"""

    id: str
    name: str = ""


@dataclass(slots=True, frozen=True)
class NeusoftCourse:
    """一门课。

    ``teach_class_id`` 就是资源接口要的 ``currentClassId`` —— 两者是同一个值，
    平台在两个接口里叫法不同，这里只保留一个名字避免搞混。
    """

    teach_course_id: str
    teach_class_id: str
    name: str = ""
    term_id: str = ""
    teacher: str = ""

    @property
    def current_class_id(self) -> str:
        return self.teach_class_id


@dataclass(slots=True)
class Resource:
    """课程资源（视频 / 文档 / 图片）。"""

    res_id: str
    name: str = ""
    res_type: int = 0
    study_status: int = 0
    parent_id: str = "0"
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_completed(self) -> bool:
        return self.study_status == ep.STUDY_STATUS_DONE

    @property
    def is_completable(self) -> bool:
        return self.res_type in ep.COMPLETABLE_RES_TYPES

    @property
    def is_video(self) -> bool:
        return self.res_type == ep.RES_TYPE_VIDEO

    @property
    def is_document(self) -> bool:
        return self.res_type == ep.RES_TYPE_DOC
