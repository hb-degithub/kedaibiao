"""品课接口常量与端点路径的**单一出处**。

全部结论来自 Lead 的实测与 pinco-recon 的静态提取。每条常量都标注证据等级：

- ``MEASURED``（实测确认）：直接请求过，HTTP 状态与响应体都核对过；
- ``STATIC``（静态提取）：从学生端 workbench bundle 里读出来的名字，
  **尚未联网验证**，字段语义也未全部验证；
- ``DEAD``（实测确认无效）：空壳或恒 500，见 :data:`DEAD_ACTIONS`；
- ``OUT_OF_SCOPE``（范围外）：教师端接口，见 :data:`OUT_OF_SCOPE_ACTIONS` ——
  它不是「坏了」而是「不该做」，两类别混写。

为什么把「危险动作」单独列出来：提交类 action 一旦被误调就是不可撤销的写操作。
:data:`DANGEROUS_ACTIONS` 配合 ``PincoClient.post_action(..., dangerous=...)``
构成第二道闸门 —— 抓取路径上的任何代码都不可能顺手调到提交接口。

改动纪律：任何路径、action 名、字段名只在这里出现一次。样本更新时先改本文件，
再改解析器。没有证据的新条目必须先问 Lead，不许自己猜。

范围界定（用户裁决 2026-10-19）：**只做学生端**。

- 教师端一律不做：教师工作台、章节目录管理、布置/催交、批改与统计类 action，
  即使能从 bundle 里读到名字也不实现、不当成可用端点（已实测教师端接口用学生
  身份调用会回 ``6571 没有该课程权限``）。
- 别被名字误导：:data:`SSO_SYSTEM` 是**登录用的 system 码** ``PincoTeacher``
  （实测学生账号也走这个值），不代表本适配器做教师功能。
- 本账号在品课里同时带教师身份，SPA 会跳到 ``/student/workbench/teacher`` ——
  那是**账号属性**，不是适配器范围，别据此认为该走教师端接口。
"""

from __future__ import annotations

import urllib.parse
from enum import Enum

from kedaibiao.platforms.pinco.errors import PincoEndpointUnavailable

# --------------------------------------------------------------------------
# 证据等级
# --------------------------------------------------------------------------


class Evidence(str, Enum):
    """一条端点事实是怎么来的。"""

    #: 实测确认：请求过，状态码与响应体都核对过
    MEASURED = "measured"
    #: 静态提取：从 bundle / APK 里读出来的，尚未联网验证
    STATIC = "static"
    #: **实测确认无效**：空壳（永远返回 ``{}``）或恒 500。
    #: 登记它们是为了让后来者一眼看到「这条路走过，是死的」，而不是再试一遍。
    DEAD = "dead"
    #: **范围外**：教师端接口。不是「坏了」，是「不该做」（用户裁决只做学生端）。
    #: 与 DEAD 分开登记：混在一起会让后来者以为「哪天修好了就能用」。
    OUT_OF_SCOPE = "out_of_scope"


#: 每条常量的证据等级（值 = 常量名）。新增常量必须在这里登记。
EVIDENCE: dict[str, Evidence] = {}

# --------------------------------------------------------------------------
# 基址与路径
# --------------------------------------------------------------------------

#: 学生端站点根。实测确认。
BASE_URL = "https://pinco.seewo.com"
EVIDENCE["BASE_URL"] = Evidence.MEASURED

#: 学生端工作台入口（SPA）。实测确认：GET 200。
STUDENT_WORKBENCH_PATH = "/student/workbench/"
EVIDENCE["STUDENT_WORKBENCH_PATH"] = Evidence.MEASURED

#: RPC 入口。学生端全部业务 action 都 POST 到这里，action 名走 query。
#: 实测确认：未登录时 HTTP 200 + ``{"message":"missing csrf token"}``；
#: 对照 /teacher/api.json 是 500，说明学生端这条是活的。
STUDENT_API_PATH = "/student/api.json"
EVIDENCE["STUDENT_API_PATH"] = Evidence.MEASURED

#: REST 兜底前缀（App 走的那条）。静态确认：未登录返回
#: ``{"code":403,"msg":"access forbidden"}``。
REST_API_PREFIX = "/server-main/api/v1"
EVIDENCE["REST_API_PREFIX"] = Evidence.STATIC

# --------------------------------------------------------------------------
# action 名（命名规律：Method + Version + PathCamel）
# --------------------------------------------------------------------------

#: ☠️ **实测恒为 500，不要再用**：``GetV1Pincofaq`` 在真实会话下也一律返回
#: ``500 {"code":"ERR_ASSERTION"}``。它曾被我当成「匿名可用」，导致
#: ``health_check()`` 恒假 —— 排查「网络 vs 会话」时反而把方向带偏了。
#: 匿名连通性改用 REST 的 ``/system/currentTime``（:data:`REST_PATH_CURRENT_TIME`，
#: 实测 200 ``{"code":0}``）。
ACTION_FAQ = "GetV1Pincofaq"
EVIDENCE["ACTION_FAQ"] = Evidence.DEAD

#: 网页版作业列表。静态提取；**实测走通的是下面那个不带 Web 的 action**，
#: 所以主路径用 :data:`ACTION_HOMEWORK_LIST`。
ACTION_HOMEWORK_LIST_WEB = "GetV1StudenthomeworkListWeb"
EVIDENCE["ACTION_HOMEWORK_LIST_WEB"] = Evidence.STATIC

#: 作业列表。**实测确认**：``{courseId, page, pageSize}`` → ``data.content[]``
#: （**没有 total**，只有 content/size）；参数缺失会 401 illegal argument。
ACTION_HOMEWORK_LIST = "GetV1StudenthomeworkList"
EVIDENCE["ACTION_HOMEWORK_LIST"] = Evidence.MEASURED

#: 作业详情。**实测确认**：``{studentHomeworkId}`` → 标题/截止/是否已交/是否过期/
#: 是否可交/附件/课程。
ACTION_HOMEWORK_DETAIL = "GetV1StudenthomeworkByStudentHomeworkIdDetail"
EVIDENCE["ACTION_HOMEWORK_DETAIL"] = Evidence.MEASURED

#: 作业题目。**实测确认**：``{studentHomeworkId}`` →
#: ``data.shortAnswers[]`` / ``data.singleChoices[]`` / ``data.multipleChoices[]`` /
#: ``data.trueOrFalses[]``（四个分组，见 :data:`QUESTION_GROUPS`）。
ACTION_HOMEWORK_QUESTIONS = "GetV1StudenthomeworkByStudentHomeworkIdQuestions"
EVIDENCE["ACTION_HOMEWORK_QUESTIONS"] = Evidence.MEASURED

#: 班级列表。**实测确认**：``{}`` → ``data[]``（裸数组）。
ACTION_CLASS_LIST = "GetV1StudentClassList"
EVIDENCE["ACTION_CLASS_LIST"] = Evidence.MEASURED

#: 单题已答内容。静态提取；读路径实测没用到它，先不接。
ACTION_QUESTION_ANSWER = (
    "GetV1StudenthomeworkByStudentHomeworkIdQuestionByQuestionIdAnswer"
)
EVIDENCE["ACTION_QUESTION_ANSWER"] = Evidence.STATIC

#: 章节学习任务列表 / 详情 / 已答。
#: ☠️ **实测确认无效，不要实现**：
#: - ``GetV1StudentChapterTaskList`` 是空壳：24 门课 × ``{}``/``{courseId}``/
#:   ``{classId}``/组合参数一律返回 ``{}``（连信封都不是）；网关对**不存在**的
#:   action 返回 500 ERR_ASSERTION，对它返回 ``{}``，所以它只是"存在但没用"；
#: - ``GetV1ChapterdirectoryNodes`` / ``...MaterialsLearningprogress`` 是**教师端**
#:   接口，学生身份调用回 ``6571 没有该课程权限``；
#: - ``...Localization`` / ``...Material`` 回 ``{}`` / 500。
#: 「学习任务」的真实形态是**课程材料**，见 :data:`ACTION_MATERIAL_LIST`。
ACTION_CHAPTER_TASK_LIST = "GetV1StudentChapterTaskList"
ACTION_CHAPTER_TASK_DETAIL = "GetV1StudentChapterTaskDetail"
ACTION_CHAPTER_TASK_ANSWERS = "GetV1StudentChapterTaskAnswers"
EVIDENCE["ACTION_CHAPTER_TASK_LIST"] = Evidence.DEAD
EVIDENCE["ACTION_CHAPTER_TASK_DETAIL"] = Evidence.DEAD
EVIDENCE["ACTION_CHAPTER_TASK_ANSWERS"] = Evidence.DEAD

#: 学习任务 = 课程材料。**实测确认**：
#: ``{courseId}`` → ``data.courseMaterials[]``，``learningRatio`` 就是学习进度（0~1）。
ACTION_MATERIAL_LIST = "GetV1CourseByCourseIdStudentMaterialList"
ACTION_MATERIAL_DETAIL = "GetV1CourseByCourseIdStudentMaterial"
EVIDENCE["ACTION_MATERIAL_LIST"] = Evidence.MEASURED
EVIDENCE["ACTION_MATERIAL_DETAIL"] = Evidence.MEASURED

#: ☠️ **实测确认无效**（空壳 / 恒 500）—— 学生端里存在但拿不到数据的接口。
#: 与 :data:`OUT_OF_SCOPE_ACTIONS` 分开写：那边是「不该做」，这边是「坏了」。
DEAD_ACTIONS = frozenset(
    {
        # 恒 500 ERR_ASSERTION（真实会话实测），曾被误当健康检查接口
        ACTION_FAQ,
        ACTION_CHAPTER_TASK_LIST,
        ACTION_CHAPTER_TASK_DETAIL,
        ACTION_CHAPTER_TASK_ANSWERS,
    }
)

#: 🚫 **范围外：教师端**。学生身份调用回 ``6571 没有该课程权限``（实测）。
#: 用户裁决「只做学生端」，所以即使 bundle 里读得到名字也不实现、不调用。
#: 登记它们只为让后来者一眼看到「这条路是范围外」，而不是再试一遍。
ACTION_TEACHER_WORKBENCH = "GetV1WorkbenchTeacherApp"
ACTION_CHAPTER_DIRECTORY_NODES = "GetV1ChapterdirectoryNodes"
ACTION_CHAPTER_DIRECTORY_LEARNING_PROGRESS = "GetV1ChapterdirectoryMaterialsLearningprogress"
ACTION_CHAPTER_DIRECTORY_MATERIALS_LOCALIZATION = "GetV1ChapterdirectoryMaterialsLocalization"
ACTION_CHAPTER_DIRECTORY_MATERIAL = "GetV1ChapterdirectoryMaterial"
for _name in (
    "ACTION_TEACHER_WORKBENCH",
    "ACTION_CHAPTER_DIRECTORY_NODES",
    "ACTION_CHAPTER_DIRECTORY_LEARNING_PROGRESS",
    "ACTION_CHAPTER_DIRECTORY_MATERIALS_LOCALIZATION",
    "ACTION_CHAPTER_DIRECTORY_MATERIAL",
):
    EVIDENCE[_name] = Evidence.OUT_OF_SCOPE

OUT_OF_SCOPE_ACTIONS = frozenset(
    {
        ACTION_TEACHER_WORKBENCH,
        ACTION_CHAPTER_DIRECTORY_NODES,
        ACTION_CHAPTER_DIRECTORY_LEARNING_PROGRESS,
        ACTION_CHAPTER_DIRECTORY_MATERIALS_LOCALIZATION,
        ACTION_CHAPTER_DIRECTORY_MATERIAL,
    }
)

#: 常量名 → 一句话原因（死接口 / 范围外）。自检、测试与后来者都靠它 ——
#: 只留一个集合的话，后来者看不出「为什么死」「为什么不做」。
BAD_ACTION_NOTES: dict[str, str] = {
    "ACTION_FAQ": "实测恒回 500 ERR_ASSERTION（真实会话），曾把 health_check 变成恒假",
    "ACTION_CHAPTER_TASK_LIST": "空壳：24 门课 × 各种参数组合一律返回 {}（实测）",
    "ACTION_CHAPTER_TASK_DETAIL": "同上，属于空壳 chapterTask 家族",
    "ACTION_CHAPTER_TASK_ANSWERS": "同上，属于空壳 chapterTask 家族",
    "ACTION_TEACHER_WORKBENCH": "教师端，超出范围（用户裁决只做学生端）",
    "ACTION_CHAPTER_DIRECTORY_NODES": "教师端章节目录，学生身份调用回 6571 没有该课程权限",
    "ACTION_CHAPTER_DIRECTORY_LEARNING_PROGRESS": "教师端学习进度，同上回 6571",
    "ACTION_CHAPTER_DIRECTORY_MATERIALS_LOCALIZATION": "教师端素材本地化，实测 {} / 500",
    "ACTION_CHAPTER_DIRECTORY_MATERIAL": "教师端素材详情，实测 {} / 500",
}


def note_of(name: str) -> str:
    """取某个常量「为什么死 / 为什么不做」的一句话；没有登记就返回空串。"""
    return BAD_ACTION_NOTES.get(name, "")

#: 课程列表。**实测确认**：``{}`` → ``data.content[]``（本账号 24 门）。
ACTION_COURSE_WORKBENCH = "GetV1CourseStudentworkbench"
ACTION_CLASS_COURSES = "GetV1StudentClassresourcesCourse"
EVIDENCE["ACTION_COURSE_WORKBENCH"] = Evidence.MEASURED
EVIDENCE["ACTION_CLASS_COURSES"] = Evidence.STATIC

#: ⚠️ 提交类 action —— 写操作，不可撤销。静态提取，**从未调用过**。
#: 只有经过人工复核的计划才能把它们发出去（见 guard.py）。
ACTION_SUBMIT_HOMEWORK = "PostV1StudenthomeworkByStudentHomeworkIdSubmit"
ACTION_SUBMIT_CHAPTER_TASK = "PostV1StudentChapterTaskSubmit"
ACTION_SUBMIT_COURSE_GROUP_TASK = "PostV1CourseGroupTaskByCourseGroupTaskIdSubmit"
EVIDENCE["ACTION_SUBMIT_HOMEWORK"] = Evidence.STATIC
EVIDENCE["ACTION_SUBMIT_CHAPTER_TASK"] = Evidence.STATIC
EVIDENCE["ACTION_SUBMIT_COURSE_GROUP_TASK"] = Evidence.STATIC

#: 危险动作名单。客户端对这些 action 要求显式 ``dangerous=True`` 才肯发。
DANGEROUS_ACTIONS = frozenset(
    {
        ACTION_SUBMIT_HOMEWORK,
        ACTION_SUBMIT_CHAPTER_TASK,
        ACTION_SUBMIT_COURSE_GROUP_TASK,
    }
)

#: REST 兜底路径。实测确认：匿名可用，返回 code:0 + data.currentTime。
REST_PATH_CURRENT_TIME = "/system/currentTime"
EVIDENCE["REST_PATH_CURRENT_TIME"] = Evidence.MEASURED

# --------------------------------------------------------------------------
# 请求头 / cookie
# --------------------------------------------------------------------------

#: 学生端 bundle 里确认存在的请求头。
HEADER_CSRF = "x-csrf-token"
HEADER_AUTHORIZATION = "authorization"
HEADER_TRACE_ID = "x-req-traceid"
HEADER_LANGUAGE = "x-language"
for _name in ("HEADER_CSRF", "HEADER_AUTHORIZATION", "HEADER_TRACE_ID", "HEADER_LANGUAGE"):
    EVIDENCE[_name] = Evidence.STATIC

#: csrf token 存在这个 cookie 里（值直接放进 ``x-csrf-token`` 头）。静态提取。
CSRF_COOKIE = "csrfToken"
EVIDENCE["CSRF_COOKIE"] = Evidence.STATIC

#: 学生端 RPC **必需** 的 ``x-server`` 头。
#:
#: ⚠️ 这里原本写的是「教师端专有，学生端不适用」，那是**推断，且被实测推翻**。
#: 2026-10-19 用真实登录态做的 A/B 对照（同一 action、同一会话、只差这个头）：
#:
#:   - 不发（只有 trace-id/language/csrf） → ``500 {"code":"ERR_ASSERTION"}``
#:   - 发 ``x-server: main``              → ``200 {"statusCode":0,"data":{"total":24,...}}``
#:   - 只加 ``x-app-code``                → 仍然 500（说明必需的是 x-server，不是 x-app-code）
#:
#: 注意失败形状：缺这个头返回的 ``ERR_ASSERTION`` 与「action 名写错」**长得一模一样**，
#: 只看错误码会把人引到「接口不存在」的错误方向上去。
HEADER_SERVER = "x-server"
#: ``x-server`` 的取值。浏览器学生端发的是 ``main``（实测）。
SERVER_NAME = "main"
for _name in ("HEADER_SERVER", "SERVER_NAME"):
    EVIDENCE[_name] = Evidence.MEASURED

#: 语言值：请求头默认简体中文。
DEFAULT_LANGUAGE = "zh-CN"

# --------------------------------------------------------------------------
# 信封字段
# --------------------------------------------------------------------------

FIELD_CODE = "code"
FIELD_MSG = "msg"
FIELD_DATA = "data"
FIELD_STATUS_CODE = "statusCode"
FIELD_MESSAGE = "message"
for _name in ("FIELD_CODE", "FIELD_MSG", "FIELD_DATA", "FIELD_STATUS_CODE", "FIELD_MESSAGE"):
    EVIDENCE[_name] = Evidence.MEASURED

#: RPC 家族的成功码：``statusCode`` **必须恰好是 0**（实测）。
SUCCESS_CODE = 0

#: 「算成功」的码值集合。用于各个家族**自己那个字段**：RPC 看 ``statusCode``、
#: REST 看 ``code``。REST 侧的 ``statusCode`` 是 HTTP 镜像（200），所以这个集合里
#: 也含 200 —— 只对 REST 的 code 与两族的兜底字段生效，不用于 RPC 的 statusCode。
SUCCESS_CODES = frozenset({0, 200, "0", "200", True})

#: 需要登录 / 权限不足的状态码（HTTP 与 statusCode 一致）。实测确认。
AUTH_STATUS_CODES = (401, 403)

#: 只有 ``message``、没有码时，什么样的 message 算「登录态问题」。
#: 实测形态：``{"message":"missing csrf token"}``（HTTP 200）。
AUTH_MESSAGE_HINTS = ("csrf token", "access forbidden", "unauthorized", "not login", "未登录")

# --------------------------------------------------------------------------
# 业务字段名
# --------------------------------------------------------------------------
#
# 标 MEASURED 的是 Lead 真实会话只读实测确认过的；标 STATIC 的是 bundle 静态
# 清单（语义未验证）。分界线要守住：STATIC 的字段不许进「已确认」的解析路径。

# -- 实测确认（读路径）----------------------------------------------------

FIELD_STUDENT_HOMEWORK_ID = "studentHomeworkId"
FIELD_CHAPTER_TASK_ID = "chapterTaskId"
FIELD_COURSE_GROUP_TASK_ID = "courseGroupTaskId"
FIELD_COURSE_ID = "courseId"
FIELD_CLASS_ID = "classId"
FIELD_CLASSROOM_ID = "classroomId"
FIELD_COURSE_NAME = "courseName"
FIELD_TITLE = "title"
#: 三种语义都实测存在：作业描述、题干 HTML、分页列表的元素数组。
FIELD_CONTENT = "content"
FIELD_SIZE = "size"
FIELD_DEADLINE = "deadline"
FIELD_SEND_TIME = "sendTime"
FIELD_CREATE_TIME = "createTime"
FIELD_STANDARD_SCORE = "standardScore"
FIELD_QUESTION_TOTAL = "questionTotal"
FIELD_SUBMITTED = "submitted"
FIELD_EXPIRED = "expired"
FIELD_CORRECTED = "corrected"
FIELD_REJECTED = "rejected"
FIELD_CAN_REDO = "canRedo"
FIELD_CAN_SUBMITTED = "canSubmitted"
FIELD_ATTACHMENTS = "attachments"
FIELD_COURSE = "course"
#: 题目分组（实测**同时出现四个**：主观题 shortAnswers、单选 singleChoices、
#: 多选 multipleChoices、判断 trueOrFalses）。字段名逐个从真实响应读出，不猜。
FIELD_SHORT_ANSWERS = "shortAnswers"
FIELD_SINGLE_CHOICES = "singleChoices"
FIELD_MULTIPLE_CHOICES = "multipleChoices"
FIELD_TRUE_OR_FALSES = "trueOrFalses"
FIELD_OPTION_ITEMS = "optionItems"
FIELD_OPTION = "option"
FIELD_LABELS = "labels"
FIELD_ANSWER_BLOCK = "answer"
FIELD_ANSWER_ID = "answerId"
FIELD_SCORE = "score"
FIELD_REVIEW = "review"
FIELD_SOLUTION = "solution"
FIELD_PUBLISH_STANDARD_SOLUTION = "publishStandardSolution"
FIELD_QUESTION_ID = "questionId"
#: 班级列表字段（实测：data[] 裸数组）。
FIELD_CLASS_NAME = "className"
FIELD_CREATOR_NAME = "creatorName"
FIELD_STUDENT_COUNT = "studentCnt"
#: 课程里的教师嵌套对象（实测字段名：creatorUser.creatorName）。
FIELD_CREATOR_USER = "creatorUser"
FIELD_PAGE = "page"
FIELD_PAGE_SIZE = "pageSize"

for _name in (
    "FIELD_STUDENT_HOMEWORK_ID",
    "FIELD_CHAPTER_TASK_ID",
    "FIELD_COURSE_GROUP_TASK_ID",
    "FIELD_COURSE_ID",
    "FIELD_CLASS_ID",
    "FIELD_CLASSROOM_ID",
    "FIELD_COURSE_NAME",
    "FIELD_TITLE",
    "FIELD_CONTENT",
    "FIELD_SIZE",
    "FIELD_DEADLINE",
    "FIELD_SEND_TIME",
    "FIELD_CREATE_TIME",
    "FIELD_STANDARD_SCORE",
    "FIELD_QUESTION_TOTAL",
    "FIELD_SUBMITTED",
    "FIELD_EXPIRED",
    "FIELD_CORRECTED",
    "FIELD_REJECTED",
    "FIELD_CAN_REDO",
    "FIELD_CAN_SUBMITTED",
    "FIELD_ATTACHMENTS",
    "FIELD_COURSE",
    "FIELD_SHORT_ANSWERS",
    "FIELD_SINGLE_CHOICES",
    "FIELD_MULTIPLE_CHOICES",
    "FIELD_TRUE_OR_FALSES",
    "FIELD_OPTION_ITEMS",
    "FIELD_OPTION",
    "FIELD_LABELS",
    "FIELD_ANSWER_BLOCK",
    "FIELD_ANSWER_ID",
    "FIELD_SCORE",
    "FIELD_REVIEW",
    "FIELD_SOLUTION",
    "FIELD_PUBLISH_STANDARD_SOLUTION",
    "FIELD_QUESTION_ID",
    "FIELD_CLASS_NAME",
    "FIELD_CREATOR_NAME",
    "FIELD_STUDENT_COUNT",
    "FIELD_CREATOR_USER",
    "FIELD_PAGE",
    "FIELD_PAGE_SIZE",
):
    EVIDENCE[_name] = Evidence.MEASURED

# -- 静态清单（语义未验证，别当已确认）------------------------------------

#: 提交报文里的答案字段：静态清单有它，但**提交报文结构未验证**，所以只用于
#: 拼一个「结构未确认」的请求体（``submit_payload_confirmed=False`` 时不会发出）。
FIELD_ANSWER_CONTENT = "answerContent"
FIELD_ANSWERS = "answers"
FIELD_OPTIONS = "options"
FIELD_COURSE_GROUP_ID = "courseGroupId"
FIELD_REMARK = "remark"
FIELD_DURATION = "duration"
FIELD_IS_SUBMIT = "isSubmit"
FIELD_END_TIME = "endTime"
for _name in (
    "FIELD_ANSWER_CONTENT",
    "FIELD_ANSWERS",
    "FIELD_OPTIONS",
    "FIELD_COURSE_GROUP_ID",
    "FIELD_REMARK",
    "FIELD_DURATION",
    "FIELD_IS_SUBMIT",
    "FIELD_END_TIME",
):
    EVIDENCE[_name] = Evidence.STATIC

#: 已知的题目分组：分组名 → 对应的核心题型。**实测四组，全部登记在册。**
#:
#: ☠️ 事故记录（静默丢 42 题）：这里一度只列了 ``shortAnswers`` 与
#: ``singleChoices``，注释还写着「只列实测响应里出现过的两个」。前半句是对的
#: ——分组名不许猜；但**结论下早了**：真实响应里一直有四个分组，
#: ``multipleChoices``（多选）与 ``trueOrFalses``（判断）被当成未知分组按空处理，
#: 于是全账号 30 道多选 + 12 道判断无声蒸发（美学概论原始 44 道只解析出 32 道）。
#:
#: 教训不是「可以猜分组名」，而是：**不猜 + 看到新分组必须回头核对**。解析时遇到
#: 未知分组会告警（``parsing.parse_questions``），那条告警就是为这类事故留的
#: 探针 —— 它是「有题没被解析」，不是噪声。
QUESTION_GROUPS: dict[str, str] = {
    FIELD_SHORT_ANSWERS: "essay",
    FIELD_SINGLE_CHOICES: "single",
    FIELD_MULTIPLE_CHOICES: "multiple",
    FIELD_TRUE_OR_FALSES: "judge",
}

#: 学生端**没有**这些字段（Lead 与 pinco-recon 都已核对，0 命中）。
#: 写在这里是为了防止后来者「顺手用」：用了它们会静默取到 None，
#: 然后看起来像「这个平台没有作业」。
NOT_IN_STUDENT_API = frozenset({"homeworkId", "pageNum", "pageNo", "answerList"})

#: 分页默认值。字段名 ``page``/``pageSize`` 实测确认。
#: **服务端把 pageSize 封顶 20**（课程 total=24 时只回 20 条），所以适配器必须
#: 翻页取全；这里也只能填 20，填大了会让「返回条数 < 请求条数」的判断提前收工。
DEFAULT_PAGE = 1
DEFAULT_PAGE_SIZE = 20
#: 翻页的硬上限：万一服务端忽略分页参数（页页都返回同一批），不能无限循环。
MAX_PAGES = 20

# --------------------------------------------------------------------------
# 学习任务（课程材料）字段
# --------------------------------------------------------------------------

FIELD_COURSE_MATERIALS = "courseMaterials"
FIELD_MATERIAL_ID = "materialId"
FIELD_MATERIAL_NAME = "materialName"
FIELD_MATERIAL_SIZE = "materialSize"
FIELD_UPDATE_TIME = "updateTime"
FIELD_MATERIAL_SOURCE = "materialSource"
#: **学习进度（0~1）**，直接映射到 ``Assignment.progress``。实测确认。
FIELD_LEARNING_RATIO = "learningRatio"
FIELD_MEDIA = "media"
FIELD_MEDIA_ID = "mediaId"
FIELD_RESOURCE_ID = "resourceId"
FIELD_DOWNLOAD_URL = "downloadUrl"
#: 材料详情接口的入参名：直接取列表里的 ``materialId`` 值（实测通过）。
FIELD_CHAPTER_DIRECTORY_MATERIAL_ID = "chapterDirectoryMaterialId"
FIELD_RESOURCE_TYPE_DOWNLOADABLE = "resourceTypeDownloadable"
FIELD_VIEWED = "viewed"
#: 课程里的学习进度对象（``learnInfo.learningProgress``）。
FIELD_LEARN_INFO = "learnInfo"
FIELD_LEARNING_PROGRESS = "learningProgress"
FIELD_TOTAL = "total"
for _name in (
    "FIELD_COURSE_MATERIALS",
    "FIELD_MATERIAL_ID",
    "FIELD_MATERIAL_NAME",
    "FIELD_MATERIAL_SIZE",
    "FIELD_UPDATE_TIME",
    "FIELD_MATERIAL_SOURCE",
    "FIELD_LEARNING_RATIO",
    "FIELD_MEDIA",
    "FIELD_MEDIA_ID",
    "FIELD_RESOURCE_ID",
    "FIELD_DOWNLOAD_URL",
    "FIELD_CHAPTER_DIRECTORY_MATERIAL_ID",
    "FIELD_RESOURCE_TYPE_DOWNLOADABLE",
    "FIELD_VIEWED",
    "FIELD_LEARN_INFO",
    "FIELD_LEARNING_PROGRESS",
    "FIELD_TOTAL",
):
    EVIDENCE[_name] = Evidence.MEASURED


# --------------------------------------------------------------------------
# URL 拼装
# --------------------------------------------------------------------------


def action_url(action: str, *, base: str = BASE_URL, path: str = STUDENT_API_PATH) -> str:
    """拼出某个 action 的 RPC 地址（action 名走 query，不做额外编码）。"""
    query = urllib.parse.urlencode({"actionName": action})
    return f"{base.rstrip('/')}{path}?{query}"


def rest_url(path: str, *, base: str = BASE_URL, prefix: str = REST_API_PREFIX) -> str:
    """拼出 REST 兜底地址。``path`` 以 ``/`` 开头或直接给路径段都行。"""
    return f"{base.rstrip('/')}{prefix.rstrip('/')}/{path.lstrip('/')}"


def evidence_of(name: str) -> Evidence | None:
    """查某条常量的证据等级（测试与自检用）。"""
    return EVIDENCE.get(name)


# --------------------------------------------------------------------------
# 协议选择：同一个 action 的两条落地方式
# --------------------------------------------------------------------------


class Protocol(str, Enum):
    """业务请求走哪个接口家族 —— **也是「用哪个字段判成功」的唯一依据**。

    ⚠️ 两个家族的信封长得像、语义却不同（真实实测，第三次踩坑后才写清）：

    ==========  ======================================  ===============================
    家族        地址形态                                成功判据
    ==========  ======================================  ===============================
    RPC(``WEB``) ``POST /student/api.json?actionName=``  ``statusCode == 0``
    REST        ``/server-main/api/v1/…``               ``code == 0``（``statusCode`` 是
                                                        HTTP 镜像 200）
    ==========  ======================================  ===============================

    实测样本：

    - RPC 成功：``{"statusCode":0,"data":{…},"message":"ok"}``（**没有 code**）；
    - REST 成功：``{"code":0,"msg":"ok","data":{…},"statusCode":200,"message":"ok"}``
      —— 若拿 ``statusCode == 0`` 去判它，每次成功都会被判成失败（``health_check``
      就是这样恒假的）；
    - REST 失败：``{"code":403,"msg":"access forbidden","statusCode":403,…}``。

    所以**别用一个字段硬套两个家族**：:func:`parsing.unwrap_envelope` 按 ``family``
    分流。

    pinco-recon 已核验 Web RPC 与 App REST 之间 98/122 条路径可机械互转，
    但**逐条映射表还没交付**，所以 REST 一侧目前只认已经实测过的那几条；其余走
    :func:`rest_path_for_action`，没有映射就直接报错。
    """

    #: ``POST {base}/student/api.json?actionName=<Op>`` —— 成功看 ``statusCode == 0``
    WEB = "web"
    #: ``{base}/server-main/api/v1/<path>`` —— 成功看 ``code == 0``
    REST = "rest"


#: action 名 → REST 路径。**故意留空**：互转规则要来自逐条核对过的清单，
#: 猜一条映射比报错危险得多 —— 它会静默打到别的接口上。
#: TODO(endpoint): pinco-recon 交付 98 条互转清单后填这里。
REST_ACTION_PATHS: dict[str, str] = {}


def rest_path_for_action(action: str) -> str:
    """取某个 action 的 REST 路径；没有核对过的映射就报错，绝不猜。"""
    path = REST_ACTION_PATHS.get(action)
    if not path:
        raise PincoEndpointUnavailable(
            f"action {action!r} 还没有核对过的 REST 路径映射（TODO(endpoint)）。"
            "Web/REST 互转规则必须来自 pinco-recon 的逐条核验结果，不能在这里猜。"
        )
    return path


# --------------------------------------------------------------------------
# SSO（id.seewo.com）：账号密码登录链路 —— Lead 实测跑通
# --------------------------------------------------------------------------

#: SSO 域。实测确认（登录三步都在这里）。
SSO_BASE_URL = "https://id.seewo.com"
EVIDENCE["SSO_BASE_URL"] = Evidence.MEASURED

#: 取登录公钥。实测确认：返回 base64 DER(SPKI)（无 PEM 头尾）。
SSO_PUBLIC_KEY_PATH = "/common/getPublicKeyV3"
#: 密码登录。实测确认。
SSO_LOGIN_PATH = "/login/v3"
#: 把 SSO token 写进 cookie。实测确认：返回 ``{"message":"设置token成功"}``。
SSO_SET_TOKEN_PATH = "/common/setToken"
for _name in ("SSO_PUBLIC_KEY_PATH", "SSO_LOGIN_PATH", "SSO_SET_TOKEN_PATH"):
    EVIDENCE[_name] = Evidence.MEASURED

#: 登录时带的 system 值。实测确认：``PincoTeacher`` 可用（学生账号也走这个）。
SSO_SYSTEM = "PincoTeacher"
SSO_CLIENT_TYPE = "pc"
SSO_PHONE_COUNTRY_CODE = "86"
for _name in ("SSO_SYSTEM", "SSO_CLIENT_TYPE", "SSO_PHONE_COUNTRY_CODE"):
    EVIDENCE[_name] = Evidence.MEASURED

#: SSO 成功后下发的 cookie：``x-token`` 的值就是 SSO token。
#: 注意：**它不等于应用层可用** —— 应用层 RPC 还要 csrf，而 csrf 与浏览器
#: 会话上下文绑定（纯 HTTP 复刻不出来，实测回 403 invalid csrf token）。
SSO_TOKEN_COOKIE = "x-token"
SSO_SAMESITE_COOKIE = "x-samesite-none-token"
SSO_COOKIE_DOMAIN = ".seewo.com"
for _name in ("SSO_TOKEN_COOKIE", "SSO_SAMESITE_COOKIE", "SSO_COOKIE_DOMAIN"):
    EVIDENCE[_name] = Evidence.MEASURED

#: 登录表单字段名。全部实测确认（Lead 抓到了完整表单）。
FIELD_SSO_DATA_ID = "dataId"
FIELD_SSO_USERNAME = "username"
FIELD_SSO_PASSWORD = "password"
FIELD_SSO_CAPTCHA = "captcha"
FIELD_SSO_SYSTEM = "system"
FIELD_SSO_CLIENT_TYPE = "clientType"
FIELD_SSO_IS_RE_ME = "isReMe"
FIELD_SSO_SECURE = "secure"
FIELD_SSO_WITH_TICKET = "withticket"
FIELD_SSO_IS_IFRAME = "isIframe"
FIELD_SSO_PHONE_COUNTRY_CODE = "phoneCountryCode"
FIELD_SSO_RST = "rst"
FIELD_SSO_LST = "lst"
FIELD_SSO_TOKEN = "tk"
FIELD_SSO_IS_FORCE_SYSTEM = "isForceSystem"
for _name in (
    "FIELD_SSO_DATA_ID",
    "FIELD_SSO_USERNAME",
    "FIELD_SSO_PASSWORD",
    "FIELD_SSO_CAPTCHA",
    "FIELD_SSO_SYSTEM",
    "FIELD_SSO_CLIENT_TYPE",
    "FIELD_SSO_IS_RE_ME",
    "FIELD_SSO_SECURE",
    "FIELD_SSO_WITH_TICKET",
    "FIELD_SSO_IS_IFRAME",
    "FIELD_SSO_PHONE_COUNTRY_CODE",
    "FIELD_SSO_RST",
    "FIELD_SSO_LST",
    "FIELD_SSO_TOKEN",
    "FIELD_SSO_IS_FORCE_SYSTEM",
):
    EVIDENCE[_name] = Evidence.MEASURED

#: 登录响应里取 token / 用户资料的字段（实测：``data.data.token`` / ``data.data.user``）。
FIELD_SSO_INNER_DATA = "data"
FIELD_SSO_TOKEN_FIELD = "token"
FIELD_SSO_USER_FIELD = "user"
for _name in ("FIELD_SSO_INNER_DATA", "FIELD_SSO_TOKEN_FIELD", "FIELD_SSO_USER_FIELD"):
    EVIDENCE[_name] = Evidence.MEASURED


def sso_url(path: str, *, base: str = SSO_BASE_URL) -> str:
    """拼 SSO 域的表单地址。"""
    return f"{base.rstrip('/')}/{path.lstrip('/')}"
