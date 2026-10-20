"""品课「课堂签到」：把签到链接里的码提交给考勤接口（**写操作**）。

## 机制（2026-10-20 静态 + 失败路径实测）

老师发起课堂签到后，学生端拿到的签到入口是一条 H5 链接::

    https://pinco.seewo.com/student/signin?attendanceCode=<code>

学生端 ``/student/signin`` 页面（``signin_c7880cbe...js``）读 ``location.search``
里的 ``attendanceCode``，然后只发一个请求::

    POST /student/api.json?actionName=PostV1TeachingAttendanceAttend
    {"attendanceKey": "<attendanceCode>"}

H5 里的成功判据是响应 ``code === 200``。失败码（H5 常量）：

- ``7207`` 考勤签到码无效（**实测**：真实会话随便填码即得此码）
- ``7208`` 签到已结束 / 超时
- ``10419`` 签到码不匹配

空 body 会回 ``401``「attendanceKey 不能为null」—— 字段名由此确认（**实测**）。

「链接参数叫 ``attendanceCode``、请求字段叫 ``attendanceKey``」这个不对称是
学生端原样行为，不是笔误。

## 证据边界

- 字段名 / 失败码：**实测**（真实会话）；
- 成功路径与「签到成功」文案：来自 H5 静态代码，**尚未用有效签到码实测**。
  文档里保留这条未验证边界，不把「发出去没报错」当成「签到成功」之外的新证据。
- 老师端的二维码是滚动生成的（防作弊考勤，约 10 秒一换），所以实现只能接
  「当次拿到的链接 / 码」，不能离线预生成或复用旧码。

## 安全边界

- 默认 dry-run：只解析签到码，不发请求；
- 真发必须 ``dry_run=False``，且该 action 在 ``DANGEROUS_ACTIONS`` 里，
  客户端要求显式放行；
- 只做当前学生自己的一次签到，不查别人、不批量刷、不碰作业提交。
"""

from __future__ import annotations

import logging
import re
import urllib.parse
from dataclasses import dataclass
from typing import Any

from kedaibiao.platforms.pinco import endpoints as ep
from kedaibiao.platforms.pinco.errors import PincoApiError

log = logging.getLogger(__name__)

#: 签到链接里的参数名（H5 静态代码里读的就是它）。
ATTENDANCE_LINK_PARAM = "attendanceCode"

#: 签到码长度上限 —— 只为挡住「把整篇文章粘进来」，不是平台限制。
MAX_CODE_LENGTH = 512

#: 学生端 H5 里出现的失败码。
ERROR_CODE_INVALID = 7207
ERROR_CODE_OVERDUE = 7208
ERROR_CODE_NOT_MATCH = 10419

_ERROR_HINTS = {
    ERROR_CODE_INVALID: "考勤签到码无效（多半已过期或输错）",
    ERROR_CODE_OVERDUE: "签到已结束或超时",
    ERROR_CODE_NOT_MATCH: "签到码与当前课堂不匹配",
}

_CODE_IN_TEXT = re.compile(r"(?:^|[?&#])" + ATTENDANCE_LINK_PARAM + r"=([^&#\s]+)")


@dataclass(frozen=True, slots=True)
class SignInOutcome:
    """一次签到的结果。``signed`` 只表示平台是否接受，不推断考勤统计。"""

    dry_run: bool
    attendance_key: str
    sent: bool
    signed: bool
    message: str
    error_code: object | None = None

    @property
    def masked_key(self) -> str:
        return mask_attendance_code(self.attendance_key)

    def summary(self) -> str:
        if self.dry_run:
            return f"干跑：签到码 {self.masked_key} 已解析，没有发送请求"
        if self.signed:
            return "签到成功"
        return f"签到失败：{self.message}"


def mask_attendance_code(code: str) -> str:
    """签到码是滚动码，日志/展示里打码，避免把可用凭据留在输出里。"""
    if len(code) <= 4:
        return "*" * len(code)
    return code[:2] + "*" * (len(code) - 4) + code[-2:]


def extract_attendance_code(raw: str) -> str:
    """从整条签到链接 / 含链接的文本 / 裸码里取出 ``attendanceCode``。

    支持三种输入：

    - 完整链接：``https://pinco.seewo.com/student/signin?attendanceCode=abc``
    - 粘贴的一整段文字（里面含 ``attendanceCode=abc``）
    - 直接给码：``abc``

    取不出来或形状不对时抛 :class:`ValueError`（动作层转成 400）。
    """
    text = str(raw or "").strip()
    if not text:
        raise ValueError("签到码为空：请填入 /student/signin?attendanceCode=... 链接或码")

    match = _CODE_IN_TEXT.search(text)
    if match is not None:
        code = urllib.parse.unquote(match.group(1)).strip()
    elif "://" in text:
        query = urllib.parse.urlsplit(text).query
        values = urllib.parse.parse_qs(query).get(ATTENDANCE_LINK_PARAM) or []
        code = values[0].strip() if values else ""
        if not code:
            raise ValueError(f"链接里没有 {ATTENDANCE_LINK_PARAM} 参数，不像是签到链接")
    else:
        code = text

    if not code:
        raise ValueError(f"签到码为空：链接里的 {ATTENDANCE_LINK_PARAM} 是空值")
    if any(ch.isspace() for ch in code):
        raise ValueError("签到码里不应有空白字符")
    if len(code) > MAX_CODE_LENGTH:
        raise ValueError(f"签到码过长（{len(code)} > {MAX_CODE_LENGTH}）")
    return code


def _failure_text(exc: PincoApiError) -> str:
    """平台拒绝时给一句人话；未知码值保留平台原文。"""
    code = getattr(exc, "code", None)
    try:
        number = int(code)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        number = None
    if number is not None and number in _ERROR_HINTS:
        return _ERROR_HINTS[number]
    return str(exc) or "平台拒绝了这次签到"


async def sign_in(client: Any, raw_code: str, *, dry_run: bool = True) -> SignInOutcome:
    """解析签到码并（在 ``dry_run=False`` 时）真的提交签到。

    :param client: ``PincoClient``（或测试替身），需有 ``post_action``。
    :param raw_code: 签到链接或裸码。
    :param dry_run: 默认只解析、不发请求。
    """
    code = extract_attendance_code(raw_code)
    if dry_run:
        return SignInOutcome(
            dry_run=True,
            attendance_key=code,
            sent=False,
            signed=False,
            message="干跑：只解析签到码，没有发送请求",
        )

    try:
        await client.post_action(
            ep.ACTION_ATTENDANCE_ATTEND,
            {ep.FIELD_ATTENDANCE_KEY: code},
            dangerous=True,
        )
    except PincoApiError as exc:
        # 业务拒绝（码过期 / 已结束 / 不匹配）是「签到没成功」这个结果的一部分，
        # 不是基础设施错误 —— 作为结果返回，让控制台/CLI 说清楚原因。
        log.info("品课签到被平台拒绝：%s", exc)
        return SignInOutcome(
            dry_run=False,
            attendance_key=code,
            sent=True,
            signed=False,
            message=_failure_text(exc),
            error_code=getattr(exc, "code", None),
        )
    return SignInOutcome(
        dry_run=False,
        attendance_key=code,
        sent=True,
        signed=True,
        message="签到成功",
    )
