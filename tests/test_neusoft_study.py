"""东软刷课与课程资源模块的单测 —— 全部离线，绝不联网、也不真 sleep。

这里钉住的是本平台最容易静默失败的五件事：

1. **``code == 0`` 不等于完成**：整条链路成功但回读 ``studyStatus`` 没到 2，
   完成函数必须返回 ``False``（档案 §4.3 的真实事故）；
2. **视频三段上报的顺序与数值**：``(1, 0)`` → ``(2, 99999)`` → ``(3, 99999)``，
   进度用 99999 而不是 100 —— 改一个数服务端就不认；
3. **``studyStatus`` 时数字时字符串**：``"2"`` 也必须判完成；
4. **文档流程的等待**：start → 等待 → end → 回读，等待是协议要求（落库延迟），
   这里用替身替换掉 ``asyncio.sleep`` 只断言「等了多少」，避免测试真卡 1.6s；
5. **资源目录递归与防环**：``folderList`` 每层 ``id`` 变下一层 ``parentId``，
   目录自环时不能死循环。

测试数据是**按协议档案的字段形状手写**的，不是线上抓包 —— 因此它证明的是
「解析与判定逻辑自洽」，不证明字段名与生产完全一致（未验证点见各用例注释）。
"""

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from kedaibiao.net.http import Response
from kedaibiao.platforms.neusoft import endpoints as ep
from kedaibiao.platforms.neusoft import study as study_module
from kedaibiao.platforms.neusoft.errors import NeusoftApiError
from kedaibiao.platforms.neusoft.models import NeusoftCourse, Resource
from kedaibiao.platforms.neusoft.resources import (
    ROOT_PARENT_ID,
    list_courses,
    list_resources,
    parse_courses,
    parse_folders,
    parse_resources,
)
from kedaibiao.platforms.neusoft.study import (
    complete_doc,
    complete_resource,
    complete_video,
    parse_study_status,
    study_status,
)
from tests.neusoft_support import FakeTransport, RecordedRequest, make_client

COURSE = NeusoftCourse(
    teach_course_id="tc-1",
    teach_class_id="cl-1",
    name="示例课程",
    term_id="term-1",
)

#: 资源行样本。resType 1=视频、3=文档、4=图片（档案 §3 实测只有这三种）。
VIDEO_ROW: dict[str, Any] = {
    "resId": "r-video",
    "resName": "视频一",
    "resType": 1,
    "studyStatus": 0,
}
DOC_ROW: dict[str, Any] = {
    "resId": "r-doc",
    "resName": "文档一",
    "resType": 3,
    "studyStatus": 2,
}
IMAGE_ROW: dict[str, Any] = {
    "resId": "r-image",
    "resName": "图片一",
    "resType": 4,
    "studyStatus": 0,
}


class _ParentTransport(FakeTransport):
    """按请求体里的 ``parentId`` 返回不同响应。

    ``FakeTransport`` 只按「方法 + 路径后缀」路由，而资源递归打的永远是同一个
    端点、只有请求体里的 ``parentId`` 不同 —— 所以这里补一层请求体路由，
    其余请求照旧交给父类，保持「离线 + 复用公共夹具」两条约定。
    """

    def __init__(self) -> None:
        super().__init__()
        self.by_parent: dict[str, dict[str, Any]] = {}

    def folder(
        self,
        parent_id: str,
        *,
        resources: list[Any] | None = None,
        folders: list[Any] | None = None,
    ) -> None:
        self.by_parent[parent_id] = {
            "resList": resources or [],
            "folderList": folders or [],
        }

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        timeout: float = 15.0,
    ) -> Response:
        raw = body.decode("utf-8") if isinstance(body, (bytes, bytearray)) else None
        recorded = RecordedRequest(method.upper(), url, dict(headers or {}), raw)
        if recorded.path.endswith(ep.RESOURCE_LIST):
            payload = json.loads(raw) if raw else {}
            parent_id = str(payload.get("parentId", ""))
            if parent_id in self.by_parent:
                self.requests.append(recorded)
                data = json.dumps(
                    {"code": 0, "msg": "", "data": self.by_parent[parent_id]},
                    ensure_ascii=False,
                )
                return Response(
                    status=200,
                    url=url,
                    body=data.encode("utf-8"),
                    headers={"content-type": "application/json"},
                )
        return await super().request(
            method, url, headers=headers, body=body, timeout=timeout
        )


def _fake_asyncio(events: list[str]) -> SimpleNamespace:
    """造一个只记录 sleep 的假 ``asyncio``，替换掉 study 模块里的那个名字。

    只替换模块自己的绑定（``patch.object(study_module, "asyncio", ...)``），
    不动全局 asyncio —— 否则会波及事件循环本身。
    """

    async def fake_sleep(seconds: float) -> None:
        events.append(f"sleep:{seconds}")

    return SimpleNamespace(sleep=fake_sleep)


def _bodies(transport: FakeTransport, suffix: str) -> list[Any]:
    """按时间顺序取打到某端点的所有请求体。"""
    return [item.json() for item in transport.requests if item.path.endswith(suffix)]


# --------------------------------------------------------------------------
# 课程与资源列表
# --------------------------------------------------------------------------


class CourseListTest(unittest.IsolatedAsyncioTestCase):
    async def test_list_courses_query_and_filter(self) -> None:
        """分页参数照档案固定，缺 teachCourseId/teachClassId 的行必须被丢掉。"""
        transport = FakeTransport()
        transport.json(
            "GET",
            ep.COURSE_PAGE,
            {
                "list": [
                    {
                        "teachCourseId": "tc-1",
                        "teachClassId": "cl-1",
                        "teachCourseName": "高等数学",
                        "teacherName": "张老师",
                    },
                    {"teachCourseId": "tc-2"},  # 缺 teachClassId：进不了资源接口
                    {"teachClassId": "cl-3"},  # 缺 teachCourseId
                    "不是对象",
                ]
            },
        )
        courses = await list_courses(make_client(transport), "term-9")

        self.assertEqual(len(courses), 1)
        self.assertEqual(courses[0].teach_course_id, "tc-1")
        self.assertEqual(courses[0].current_class_id, "cl-1")
        self.assertEqual(courses[0].name, "高等数学")
        self.assertEqual(courses[0].teacher, "张老师")
        self.assertEqual(courses[0].term_id, "term-9")

        request = transport.requests[0]
        self.assertEqual(request.method, "GET")
        self.assertEqual(request.path, ep.COURSE_PAGE)
        self.assertEqual(
            request.query, {"termId": "term-9", "pageNo": "1", "pageSize": "100"}
        )

    def test_parse_courses_tolerates_missing_name_fields(self) -> None:
        """课程名/教师名字段未实测：取不到就留空，但不能丢课程。"""
        courses = parse_courses(
            {"list": [{"teachCourseId": "tc-1", "teachClassId": "cl-1"}]}
        )
        self.assertEqual(len(courses), 1)
        self.assertEqual(courses[0].name, "")
        self.assertEqual(courses[0].teacher, "")
        self.assertEqual(parse_courses(None), [])


class ResourceListTest(unittest.IsolatedAsyncioTestCase):
    async def test_flat_resource_list(self) -> None:
        """多数课程 folderList 为空，资源直接给全 —— 一次请求就要全拿到。"""
        transport = FakeTransport()
        transport.json(
            "POST",
            ep.RESOURCE_LIST,
            {"resList": [VIDEO_ROW, DOC_ROW, IMAGE_ROW], "folderList": []},
        )
        resources = await list_resources(make_client(transport), COURSE)

        self.assertEqual([item.res_id for item in resources], ["r-video", "r-doc", "r-image"])
        self.assertEqual([item.res_type for item in resources], [1, 3, 4])
        self.assertEqual(resources[1].study_status, ep.STUDY_STATUS_DONE)
        self.assertTrue(resources[1].is_completed)
        self.assertTrue(resources[0].is_video)
        self.assertTrue(resources[1].is_document)

        self.assertEqual(len(transport.requests), 1)
        body = transport.body_of(ep.RESOURCE_LIST)
        # 筛选字段必须显式给空串，parentId 是根值
        self.assertEqual(
            body,
            {
                "teachCourseId": "tc-1",
                "currentClassId": "cl-1",
                "name": "",
                "resType": "",
                "studyStatus": "",
                "sortType": 1,
                "isAssess": "",
                "parentId": ROOT_PARENT_ID,
            },
        )

    async def test_recursive_folder_tree(self) -> None:
        """folderList 非空时要逐层下钻，每层 folder.id 就是下一层的 parentId。"""
        transport = _ParentTransport()
        transport.folder(
            ROOT_PARENT_ID,
            resources=[VIDEO_ROW],
            folders=[{"id": "f1", "name": "第一章"}],
        )
        transport.folder("f1", resources=[DOC_ROW], folders=[{"id": "f2", "name": "1-1"}])
        transport.folder("f2", resources=[IMAGE_ROW])
        resources = await list_resources(make_client(transport), COURSE)

        self.assertEqual(
            [item.res_id for item in resources], ["r-video", "r-doc", "r-image"]
        )
        self.assertEqual(
            [item.parent_id for item in resources],
            [ROOT_PARENT_ID, "f1", "f2"],
        )
        self.assertEqual(len(transport.requests), 3)
        self.assertEqual(
            [item.json()["parentId"] for item in transport.requests],
            [ROOT_PARENT_ID, "f1", "f2"],
        )

    async def test_seen_prevents_folder_cycle(self) -> None:
        """目录自环（f1 的 folderList 里又有 f1）时不能无限请求。"""
        transport = _ParentTransport()
        transport.folder(ROOT_PARENT_ID, folders=[{"id": "f1"}])
        transport.folder("f1", folders=[{"id": "f1"}, {"id": "f2"}])
        transport.folder("f2", folders=[{"id": "f1"}])
        resources = await list_resources(make_client(transport), COURSE)

        self.assertEqual(resources, [])
        # 根、f1、f2 各一次；重复出现的 f1 被 seen 拦下，不会无限循环
        self.assertEqual(len(transport.requests), 3)

    def test_parse_resources_and_folders_skip_unusable_rows(self) -> None:
        self.assertEqual(parse_resources({"resList": [{"resName": "无 ID"}]}), [])
        self.assertEqual(parse_folders({"folderList": [{"id": "f1"}, {"name": "无 ID"}]}), ["f1"])
        self.assertEqual(parse_resources(None), [])
        self.assertEqual(parse_folders(None), [])


# --------------------------------------------------------------------------
# 回读状态
# --------------------------------------------------------------------------


class StudyStatusTest(unittest.IsolatedAsyncioTestCase):
    async def test_accepts_number_and_string(self) -> None:
        """同一个字段时而是数字 2、时而字符串 "2"，两种都要判完成。"""
        for payload in ({"studyStatus": 2}, {"studyStatus": "2"}):
            with self.subTest(payload=payload):
                transport = FakeTransport()
                transport.json("POST", ep.GET_RECORD_INFO, payload)
                self.assertEqual(
                    await study_status(make_client(transport), COURSE, "r-1"),
                    ep.STUDY_STATUS_DONE,
                )

    async def test_missing_or_broken_payload_reads_as_todo(self) -> None:
        """读不到状态一律按 0：我宁可多刷一遍，也不把「不知道」当完成。"""
        self.assertEqual(parse_study_status(None), ep.STUDY_STATUS_TODO)
        self.assertEqual(parse_study_status({}), ep.STUDY_STATUS_TODO)
        self.assertEqual(parse_study_status("2"), ep.STUDY_STATUS_TODO)
        self.assertEqual(parse_study_status({"studyStatus": "abc"}), ep.STUDY_STATUS_TODO)

    async def test_readback_goes_after_common_body(self) -> None:
        transport = FakeTransport()
        transport.json("POST", ep.GET_RECORD_INFO, {"studyStatus": 0})
        await study_status(make_client(transport), COURSE, "r-1")
        self.assertEqual(
            transport.body_of(ep.GET_RECORD_INFO),
            {"teachCourseId": "tc-1", "currentClassId": "cl-1", "resId": "r-1"},
        )


# --------------------------------------------------------------------------
# 视频链路
# --------------------------------------------------------------------------


class VideoFlowTest(unittest.IsolatedAsyncioTestCase):
    async def test_three_reports_in_order_then_confirmed(self) -> None:
        """start → 三段上报 → 回读；顺序与 (playStatus, watchProgress) 都是实测值。"""
        transport = FakeTransport()
        transport.json("POST", ep.START_STUDY, {})
        transport.json("POST", ep.STUDY_AUDIO_VIDEO, {})
        transport.json("POST", ep.GET_RECORD_INFO, {"studyStatus": 2})

        self.assertTrue(await complete_video(make_client(transport), COURSE, "r-video"))

        self.assertEqual(
            transport.paths(),
            [
                ep.START_STUDY,
                ep.STUDY_AUDIO_VIDEO,
                ep.STUDY_AUDIO_VIDEO,
                ep.STUDY_AUDIO_VIDEO,
                ep.GET_RECORD_INFO,
            ],
        )
        reports = _bodies(transport, ep.STUDY_AUDIO_VIDEO)
        self.assertEqual(
            [(item["playStatus"], item["watchProgress"]) for item in reports],
            [(1, 0), (2, 99999), (3, 99999)],
        )
        # 99999 不是 100：这里钉死它，避免有人「顺手改成百分比」
        self.assertNotIn(100, [item["watchProgress"] for item in reports])
        for body in reports:
            self.assertEqual(body["teachCourseId"], "tc-1")
            self.assertEqual(body["currentClassId"], "cl-1")
            self.assertEqual(body["resId"], "r-video")

    async def test_code_zero_but_not_done_is_failure(self) -> None:
        """本平台最大的坑：整条链路 code=0，但回读 studyStatus=0 → 未完成。"""
        transport = FakeTransport()
        transport.json("POST", ep.START_STUDY, {})
        transport.json("POST", ep.STUDY_AUDIO_VIDEO, {})
        transport.json("POST", ep.GET_RECORD_INFO, {"studyStatus": ep.STUDY_STATUS_TODO})

        self.assertFalse(await complete_video(make_client(transport), COURSE, "r-video"))

    async def test_string_status_two_is_done(self) -> None:
        transport = FakeTransport()
        transport.json("POST", ep.START_STUDY, {})
        transport.json("POST", ep.STUDY_AUDIO_VIDEO, {})
        transport.json("POST", ep.GET_RECORD_INFO, {"studyStatus": "2"})

        self.assertTrue(await complete_video(make_client(transport), COURSE, "r-video"))

    async def test_api_error_is_not_swallowed(self) -> None:
        """业务错误必须上抛，不能吞成 False —— 否则「413 未开放」看起来像没学完。"""
        transport = FakeTransport()
        transport.api_error("POST", ep.START_STUDY, 500, "资源未开放")
        with self.assertRaises(NeusoftApiError):
            await complete_video(make_client(transport), COURSE, "r-video")
        self.assertEqual(transport.paths(), [ep.START_STUDY])


# --------------------------------------------------------------------------
# 文档链路
# --------------------------------------------------------------------------


class DocFlowTest(unittest.IsolatedAsyncioTestCase):
    async def test_doc_flow_order_and_settle_wait(self) -> None:
        """start → startDoc → 等待 → endDoc → 回读；等待发生在 end 之前。"""
        transport = FakeTransport()
        transport.json("POST", ep.START_STUDY, {})
        transport.json("POST", ep.START_DOC, {})
        transport.json("POST", ep.END_DOC, {})
        transport.json("POST", ep.GET_RECORD_INFO, {"studyStatus": 2})
        events: list[str] = []

        with patch.object(study_module, "asyncio", _fake_asyncio(events)):
            done = await complete_doc(make_client(transport), COURSE, "r-doc")

        self.assertTrue(done)
        self.assertEqual(
            transport.paths(),
            [ep.START_STUDY, ep.START_DOC, ep.END_DOC, ep.GET_RECORD_INFO],
        )
        self.assertEqual(events, [f"sleep:{ep.DOC_SETTLE_SECONDS}"])
        for suffix in (ep.START_DOC, ep.END_DOC):
            body = transport.body_of(suffix)
            self.assertEqual(body["resId"], "r-doc")
            self.assertEqual(body["teachCourseId"], "tc-1")

    async def test_doc_reports_success_but_readback_still_zero(self) -> None:
        """文档类实测事故：全链路成功、studyStatus 纹丝不动 → 必须 False。"""
        transport = FakeTransport()
        transport.json("POST", ep.START_STUDY, {})
        transport.json("POST", ep.START_DOC, {})
        transport.json("POST", ep.END_DOC, {})
        transport.json("POST", ep.GET_RECORD_INFO, {"studyStatus": ep.STUDY_STATUS_TODO})
        events: list[str] = []

        with patch.object(study_module, "asyncio", _fake_asyncio(events)):
            done = await complete_doc(make_client(transport), COURSE, "r-doc")

        self.assertFalse(done)
        self.assertEqual(len(events), 1)


# --------------------------------------------------------------------------
# 类型分派
# --------------------------------------------------------------------------


class DispatchTest(unittest.IsolatedAsyncioTestCase):
    async def test_video_resource_dispatches_to_video_flow(self) -> None:
        transport = FakeTransport()
        transport.json("POST", ep.START_STUDY, {})
        transport.json("POST", ep.STUDY_AUDIO_VIDEO, {})
        transport.json("POST", ep.GET_RECORD_INFO, {"studyStatus": 2})

        resource = Resource(res_id="r-video", name="视频", res_type=ep.RES_TYPE_VIDEO)
        self.assertTrue(
            await complete_resource(make_client(transport), COURSE, resource)
        )
        self.assertIn(ep.STUDY_AUDIO_VIDEO, transport.paths())
        self.assertNotIn(ep.START_DOC, transport.paths())

    async def test_doc_resource_dispatches_to_doc_flow(self) -> None:
        transport = FakeTransport()
        transport.json("POST", ep.START_STUDY, {})
        transport.json("POST", ep.START_DOC, {})
        transport.json("POST", ep.END_DOC, {})
        transport.json("POST", ep.GET_RECORD_INFO, {"studyStatus": 2})

        resource = Resource(res_id="r-doc", name="文档", res_type=ep.RES_TYPE_DOC)
        with patch.object(study_module, "asyncio", _fake_asyncio([])):
            done = await complete_resource(make_client(transport), COURSE, resource)
        self.assertTrue(done)
        self.assertIn(ep.START_DOC, transport.paths())
        self.assertNotIn(ep.STUDY_AUDIO_VIDEO, transport.paths())

    async def test_unsupported_type_returns_false_without_requests(self) -> None:
        """不认识的 res_type：返回 False 且一个请求都不发，也不抛异常。"""
        transport = FakeTransport()
        resource = Resource(res_id="r-x", name="未知", res_type=99)
        self.assertFalse(
            await complete_resource(make_client(transport), COURSE, resource)
        )
        self.assertEqual(transport.requests, [])

    async def test_image_type_is_not_completed_without_evidence(self) -> None:
        """图片（res_type=4）虽在可完成集合里，但完成链路未验证 → 明确返回 False。

        档案 §3 实测只有 1/3/4 三种类型，§4 的 [A] 只覆盖视频与文档；
        ``startStudyForDocPicMd`` 的命名暗示它管图片，但没有实测证据，
        所以这里不猜着走文档流程 —— 与其「猜对了算运气」，不如让人看见。
        """
        transport = FakeTransport()
        resource = Resource(res_id="r-image", name="图片", res_type=ep.RES_TYPE_IMAGE)
        self.assertTrue(resource.is_completable)
        self.assertFalse(
            await complete_resource(make_client(transport), COURSE, resource)
        )
        self.assertEqual(transport.requests, [])


if __name__ == "__main__":
    unittest.main()
