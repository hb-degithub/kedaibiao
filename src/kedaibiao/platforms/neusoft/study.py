"""刷课：视频与文档的完成链路。

## 为什么所有完成函数都返回 ``bool`` 而不是 ``None``

这个平台最贵的一课是：**整条链路 ``code == 0`` 也可能什么都没发生**。文档类实测
出现过「``startStudy`` / ``startStudyForDocPicMd`` / ``endStudyForDocPicMd``
全部 ``code == 0``，回读 ``studyStatus`` 纹丝不动」（档案 §4.3）。所以这里的
完成函数一律是「做事 → 回读 → 用 ``studyStatus`` 说话」，返回 ``bool`` 表示
**是否确认完成**；回读拿不到结果按未完成处理，绝不能把失败当成功。

## 为什么只有文档流程里有一个 sleep

网络重试与退避属于 ``core.scheduler``，``NeusoftClient`` 也不 sleep —— 这个模块
同样不重试。唯一允许的 ``asyncio.sleep`` 是文档流程里的 ``DOC_SETTLE_SECONDS``：
服务端把学习记录落库是异步的，``end`` 之后立刻回读会拿到旧的 0。这是**协议需要**，
不是退避重试。

## 为什么不做「cron 式死循环」

一个资源跑一遍、回读不达标就返回 ``False``，由调度器决定要不要再来一次 ——
在业务层写重试会把「平台压根不认这个资源」变成无限刷。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from kedaibiao.platforms.neusoft import endpoints as ep
from kedaibiao.platforms.neusoft.client import NeusoftClient
from kedaibiao.platforms.neusoft.models import NeusoftCourse, Resource, as_int

log = logging.getLogger(__name__)


def record_body(course: NeusoftCourse, res_id: str) -> dict[str, Any]:
    """学习记录类接口的公共 body（档案 §4：三个字段缺一不可）。

    每个请求都新建一份字典，调用方往里合并 ``playStatus`` 之类不会污染公共部分。
    """
    return {
        "teachCourseId": course.teach_course_id,
        "currentClassId": course.current_class_id,
        "resId": res_id,
    }


def parse_study_status(payload: Any) -> int:
    """从 ``getRecordInfo`` 的 ``data`` 里读 ``studyStatus``。

    两种形状都要接：实测同一账号不同资源回来时而是数字 ``2``，时而字符串 ``"2"``
    （档案 §4.3）。``as_int`` 都认，读不出来就给 0 —— **宁可多刷一遍，也不能把
    「不知道」当完成**。
    """
    if not isinstance(payload, dict):
        return 0
    return as_int(payload.get("studyStatus"))


async def study_status(client: NeusoftClient, course: NeusoftCourse, res_id: str) -> int:
    """回读一个资源的学习状态 —— 这是完成判定的唯一依据。

    不做任何兜底：``client.request`` 抛出的业务/协议异常照常上抛，由调用方
    决定是重登、重试还是跳过；静默返回 0 会让「接口挂了」看起来像「没学完」。
    """
    payload = await client.request(
        "POST", ep.GET_RECORD_INFO, body=record_body(course, res_id)
    )
    return parse_study_status(payload)


async def _start_study(client: NeusoftClient, course: NeusoftCourse, res_id: str) -> None:
    """``startStudy``：两类资源的共同第一步。

    单独抽出来是因为视频与文档都要它，且**顺序**有要求（先 start 再上报，
    否则服务端不记这次学习）。
    """
    await client.request("POST", ep.START_STUDY, body=record_body(course, res_id))


async def complete_video(client: NeusoftClient, course: NeusoftCourse, res_id: str) -> bool:
    """视频：``startStudy`` → 三段进度上报 → 回读。

    三段 ``(playStatus, watchProgress)`` 的顺序与数值都是实测钉死的，见
    :data:`endpoints.PROGRESS_STEPS`：``(1, 0)`` 开始、``(2, 99999)`` 暂停带进度、
    ``(3, 99999)`` 结束。进度用 **99999** 而不是 100 —— 改成 100 服务端不算看完。
    """
    await _start_study(client, course, res_id)
    for play_status, watch_progress in ep.PROGRESS_STEPS:
        body = record_body(course, res_id)
        body["playStatus"] = play_status
        body["watchProgress"] = watch_progress
        await client.request("POST", ep.STUDY_AUDIO_VIDEO, body=body)

    done = await study_status(client, course, res_id) == ep.STUDY_STATUS_DONE
    if not done:
        log.warning(
            "视频上报完成但回读未确认完成：course=%s res=%s",
            course.teach_course_id,
            res_id,
        )
    return done


async def complete_doc(client: NeusoftClient, course: NeusoftCourse, res_id: str) -> bool:
    """文档：``startStudy`` → start → **等待落库** → end → 回读。

    为什么中间要等 ``DOC_SETTLE_SECONDS``（档案 §4.2 实测 ~1.6s）：文档学习时长由
    服务端后台结算，``end`` 返回成功只表示请求被受理；立刻回读会拿到旧的
    ``studyStatus``，于是「明明跑完了却判失败」，下一轮又刷一遍。这是本模块唯一
    允许的 sleep，理由写在模块 docstring 里 —— 它是协议要求，不是重试退避。
    """
    await _start_study(client, course, res_id)
    await client.request("POST", ep.START_DOC, body=record_body(course, res_id))
    await asyncio.sleep(ep.DOC_SETTLE_SECONDS)
    await client.request("POST", ep.END_DOC, body=record_body(course, res_id))

    done = await study_status(client, course, res_id) == ep.STUDY_STATUS_DONE
    if not done:
        log.warning(
            "文档上报完成但回读未确认完成：course=%s res=%s",
            course.teach_course_id,
            res_id,
        )
    return done


async def complete_resource(
    client: NeusoftClient, course: NeusoftCourse, resource: Resource
) -> bool:
    """按资源类型分派；不可完成的类型返回 ``False`` 并说明原因（**不抛**）。

    为什么这里不抛：资源里混着类型不认识的条目是正常情况（档案 §3 实测只有
    1/3/4 三种，但那是三个学期的样本），一个怪资源不该让整门课的刷课中断。
    真正的网络/协议错误仍由 ``client.request`` 上抛，不会被吞成 ``False``。

    图片（``res_type=4``）暂时返回 ``False``：``startStudyForDocPicMd`` 的接口名字
    暗示它管「文档/图片/Markdown」，但档案里 ``[A]`` 的实测只覆盖视频与文档，
    图片到底走哪条链路**没有证据**，所以不猜。
    """
    if not resource.is_completable:
        log.warning(
            "资源类型不在可完成范围，跳过：res=%s res_type=%s（可完成类型：%s）",
            resource.res_id,
            resource.res_type,
            sorted(ep.COMPLETABLE_RES_TYPES),
        )
        return False
    if resource.is_image:
        log.info(
            "图片资源没有实测的完成链路，跳过（不计失败、不占 limit）：res=%s",
            resource.res_id,
        )
        return False
    if resource.is_video:
        return await complete_video(client, course, resource.res_id)
    if resource.is_document:
        return await complete_doc(client, course, resource.res_id)
    log.warning(
        "资源类型的完成链路未验证，跳过：res=%s res_type=%s",
        resource.res_id,
        resource.res_type,
    )
    return False
