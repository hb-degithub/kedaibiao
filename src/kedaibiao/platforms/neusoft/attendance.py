"""考勤查询与签到。

协议事实见 ``docs/research/neusoft.md`` §6。这个模块和刷课/作业最大的区别是：
它有一个**不可逆的写动作**（签到）。所以这里刻意把「能不能签」拆成纯函数，
并且让 :func:`submit_sign` 在**发出任何请求之前**先用这些纯函数把三条守卫跑完。

三条守卫为什么非有不可（每条都对应一个真实坑）：

1. **已签还要再判一次** —— 已签的记录再提交，轻则被服务端拒绝，重则覆盖掉
   原来正确的出勤/请假记录；而「已签」有**两个独立判据**（``signRole`` 与
   ``signTime``），只判一个会漏掉另一类已签记录，于是重复提交。
2. **已结束的不能签** —— 窗口关了之后提交只会拿到业务错误；更麻烦的是
   「已结束」有时只体现在 ``finishTime`` 上，而 ``status`` 还是 ``1``，
   所以状态与时间都要看。
3. **需定位却没有坐标** —— 服务端会按考勤点半径校验坐标，没坐标硬签只会得到
   一条状态不明的记录；宁可拦住让人补坐标。

守卫不满足时抛 :class:`AttendanceGuardError` 而不是返回 ``False``：``False``
在调用方很容易被当成「这次没签成，下一轮再说」而继续走补签分支，异常则当场
中断；被拦住的调用**一个请求都不会发出**。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from kedaibiao.platforms.neusoft import endpoints as ep
from kedaibiao.platforms.neusoft.client import NeusoftClient
from kedaibiao.platforms.neusoft.errors import NeusoftError
from kedaibiao.platforms.neusoft.models import as_int, as_list, as_str

#: 场次状态「进行中」。``endpoints`` 里只登记了明细状态
#: :data:`~...endpoints.ATT_STATUS_PRESENT`（同值 1，但那是「出勤」的语义），
#: 场次状态的 1 是「进行中」，所以在本模块单独命名，避免读代码时把两件事搞混。
STATUS_IN_PROGRESS = 1

#: 学生自签提交的 ``signRole``。``endpoints.ATT_SIGNED_ROLES`` 登记的是「已签」
#: 的两个取值 ``{3, 4}``，但**哪个值代表学生**协议档案未写死（``[C]``，单一样本），
#: 这里按学生形态用 4；教师代签（3）不在本模块的职责内。
SIGN_ROLE_STUDENT = 4

#: 扫码签到的 ``refreshSeed`` 兜底值。协议档案只确认「type==1 要带 refreshSeed」，
#: 具体取值来自单一样本（``[C]``）—— 调用方能从二维码/接口拿到真实值时必须传入。
DEFAULT_REFRESH_SEED = "0"


class AttendanceGuardError(NeusoftError):
    """签到前置条件不满足 —— 调用方状态问题，不是平台错误。

    单独一个类型是为了让上层能区分「被我们拦住」与「请求发出去后被平台拒绝」：
    前者无需重试、不该计入失败统计；后者才需要看服务端文案。
    """


@dataclass(slots=True)
class Attendance:
    """一场考勤（列表的一行）。

    字段一律收成好比较的形态：时间统一成毫秒整数，其余统一成字符串，平台原样
    丢进 ``raw`` 备查。``status`` 保留 ``None`` 的语义 —— 服务端把新发布的场次
    返回成 ``null``，把它压成 0 就会和「未开始」混在一起（见 §6.1）。
    """

    attendance_id: str
    detail_id: str = ""
    status: int | None = None
    type: int = 0
    sign_role: int = 0
    sign_user_id: str = ""
    sign_time: str = ""
    location_type: int = 0
    start_time: int | None = None
    end_time: int | None = None
    course_name: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


def parse_attendance(payload: Any) -> list[Attendance]:
    """解析考勤分页的 ``data``（正常形状是 ``{list: [...]}``，也容忍直接给数组）。

    没有 ``attendanceId`` 的行直接丢掉：签到、去重、取明细都靠场次 ID，留着只会在
    提交时炸在离现场更远的地方。注意列表里的 ``id`` 是**明细 ID**，不是场次 ID，
    两者不能互相兜底。
    """
    rows: list[Any]
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        rows = as_list(payload.get("list"))
    else:
        rows = []

    result: list[Attendance] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        attendance_id = as_str(row.get("attendanceId"))
        if not attendance_id:
            continue
        result.append(
            Attendance(
                attendance_id=attendance_id,
                detail_id=as_str(row.get("id") or row.get("detailId")),
                status=_optional_int(row.get("status")),
                type=as_int(row.get("type")),
                sign_role=as_int(row.get("signRole")),
                sign_user_id=as_str(row.get("signUserId")),
                sign_time=as_str(row.get("signTime")),
                location_type=as_int(
                    _first_present(row, ("attendanceLocationType", "locationType"))
                ),
                start_time=_optional_timestamp(_first_present(row, ("startTime", "openTime"))),
                end_time=_optional_timestamp(_first_present(row, ("endTime", "finishTime"))),
                course_name=as_str(_first_present(row, ("courseName", "title"))),
                raw=dict(row),
            )
        )
    return result


async def list_attendance(
    client: NeusoftClient, student_id: str, term_id: str
) -> list[Attendance]:
    """拉某学期全部考勤场次（App 端分页接口）。

    ``courseId`` / ``attendanceStatus`` / ``status`` 显式传 ``null`` 而不是省略：
    这是「不要过滤」的表达方式（协议档案 §6 的实测报文），省字段时的服务端行为
    没有验证过，别拿它当默认值。
    """
    payload = await client.request(
        "POST",
        ep.ATT_PAGE,
        body={
            "studentId": student_id,
            "termId": term_id,
            "courseId": None,
            "attendanceStatus": None,
            "status": None,
        },
    )
    return parse_attendance(payload)


def is_open(att: Attendance, now_ms: int | None = None) -> bool:
    """场次现在是否进行中（**只看状态与结束时间，不看是否已签**）。

    顺序是硬约定：

    1. 先看 ``status``：``null`` 与 ``1`` 都算进行中（新发布的场次服务端返回
       ``null``，只判 ``== 1`` 会整批漏掉）；其它值（0 未开始、2 已结束）直接结束；
    2. 再看 ``end_time``：给了结束时间且已经过了，就算结束 —— 因为服务端有时
       只更新窗口不更新 ``status``。
    """
    moment = _now_ms() if now_ms is None else now_ms
    if att.status is not None and att.status != STATUS_IN_PROGRESS:
        return False
    return att.end_time is None or moment < att.end_time


def is_signed(att: Attendance) -> bool:
    """本人是否已经签过到。

    ``signRole ∈ {3,4}`` **或** ``signTime`` 非空，两个判据缺一不可：服务端
    确实会只填其中一个（协议档案 §6.1 第 2 条要求两个都判），只判一个就会把
    「已签」当成「未签」而重复提交。
    """
    return att.sign_role in ep.ATT_SIGNED_ROLES or bool(att.sign_time)


def needs_location(att: Attendance) -> bool:
    """这场签到是否要求现场定位（服务端会校验坐标与半径）。"""
    return att.location_type == ep.ATT_LOCATION_TYPE


async def detail_id(
    client: NeusoftClient, attendance_id: str, student_id: str
) -> str:
    """取本人在某场次的明细 ID（提交签到时就是报文里的 ``id``）。

    返回空串表示「不在该教学班」（协议档案 §6），调用方应当跳过而不是当错误。
    服务端时而返回数字时而返回字符串，这里统一成字符串：提交报文里按字符串发，
    与 App 行为一致。
    """
    payload = await client.request(
        "GET", f"{ep.ATT_DETAIL_ID}/{attendance_id}/{student_id}"
    )
    if isinstance(payload, dict):
        payload = _first_present(payload, ("id", "detailId"))
    return as_str(payload)


async def submit_sign(
    client: NeusoftClient,
    att: Attendance,
    *,
    detail_id: str,
    latitude: str | float | None = None,
    longitude: str | float | None = None,
    address: str = "",
    refresh_seed: str = "",
) -> dict[str, Any]:
    """提交签到（**不可逆写动作**）。

    三条守卫全部在发请求之前执行，任一条不满足即抛 :class:`AttendanceGuardError`，
    此时 ``transport`` 上不会留下任何请求：

    - 已签（:func:`is_signed`）—— 拒绝重复提交；
    - 已结束（:func:`is_open` 为假）—— 窗口关了再发只能吃业务错误；
    - 需定位（:func:`needs_location`）但 ``latitude`` / ``longitude`` 缺失或为空串
      —— 没坐标硬签只会换来一条状态不明的记录。

    ``detail_id`` 为空串（「不在该教学班」）不在这里拦：它不是一个签到状态问题，
    由调用方在 :func:`detail_id` 之后自行跳过；传空 ID 提交必然被服务端拒绝。

    返回值是响应 ``data``（通常为空对象）。**``code == 0`` 只代表服务端收下了
    请求**，是否真的签到成功要回读列表里的 ``signRole`` / ``signTime``
    （与刷课「``code == 0`` 不等于完成」同一套约定）。
    """
    if is_signed(att):
        raise AttendanceGuardError(
            f"场次 {att.attendance_id} 已经签过到（signRole={att.sign_role}, "
            f"signTime={att.sign_time!r}），拒绝重复提交"
        )
    if not is_open(att):
        raise AttendanceGuardError(
            f"场次 {att.attendance_id} 已结束（status={att.status}, "
            f"endTime={att.end_time}），不能签到"
        )
    located = needs_location(att)
    if located and (_is_blank(latitude) or _is_blank(longitude)):
        raise AttendanceGuardError(
            f"场次 {att.attendance_id} 需要现场定位，但缺少经纬度"
            f"（latitude={latitude!r}, longitude={longitude!r}）"
        )

    body: dict[str, Any] = {
        "attendanceId": att.attendance_id,
        "id": detail_id,
        "status": ep.ATT_STATUS_PRESENT,
        "type": att.type,
        # 学生形态：签到人固定是当前登录账号；列表里的 signUserId 只有已签记录才有值。
        "signRole": SIGN_ROLE_STUDENT,
        "signUserId": client.user_id or att.sign_user_id,
    }
    if located:
        # 三个字段要一起给（协议档案 §6 + §6.1 第 3 条）；coord 统一成字符串。
        body["signLongitude"] = as_str(longitude)
        body["signLatitude"] = as_str(latitude)
        body["signAddressName"] = address
    if att.type == ep.ATT_TYPE_SCAN:
        body["refreshSeed"] = refresh_seed or DEFAULT_REFRESH_SEED

    result = await client.request("PUT", ep.ATT_UPDATE, body=body)
    return result if isinstance(result, dict) else {}


# --------------------------------------------------------------------------
# 内部工具
# --------------------------------------------------------------------------


def _first_present(row: dict[str, Any], keys: tuple[str, ...]) -> Any:
    """按顺序取第一个「有值」的字段。

    平台会为同一语义给多个名字（``openTime``/``startTime``、
    ``attendanceLocationType``/``locationType``），且真值可能是数字 0 ——
    所以判「有值」要看是否 ``None`` / 空串，不能用 ``or`` 链（0 会被吞掉）。
    """
    for key in keys:
        value = row.get(key)
        if value is not None and value != "":
            return value
    return None


def _optional_int(value: Any) -> int | None:
    """读一个可能为 ``null``、也可能数字/字符串的字段，并保留 ``None``。

    不能用 :func:`~...models.as_int`：它把 ``None`` 落到 0，而考勤里
    ``status=null``（进行中）与 ``status=0``（未开始）是相反的意思。
    """
    if value is None:
        return None
    if isinstance(value, str) and value.strip().lower() in ("", "null", "none"):
        return None
    if isinstance(value, bool):
        return int(value)
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _optional_timestamp(value: Any) -> int | None:
    """读毫秒时间戳；``0`` 与不可解析值都当缺失。

    0 在业务上不可能（1970 年），它更可能是「服务端没给」。若当成有效结束时间，
    :func:`is_open` 会把所有场次判成已结束 —— 宁可当缺失，交给 ``status`` 判断。
    """
    number = _optional_int(value)
    if number is None or number <= 0:
        return None
    return number


def _is_blank(value: Any) -> bool:
    """坐标是否缺失。空串与 ``None`` 一样都算没有坐标。"""
    return value is None or as_str(value) == ""


def _now_ms() -> int:
    """当前毫秒时间戳。与平台的 ``openTime`` / ``finishTime`` 同一单位。"""
    return int(time.time() * 1000)
