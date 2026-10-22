"""东软智慧教育平台的接口常量与协议事实。

事实出处与置信度见 ``docs/research/neusoft.md``。这个模块只有常量，**没有逻辑** ——
端点、魔数、请求头集中在这里，改协议时只改这一处。

置信度标记（沿用 ``docs/research/neusoft.md`` 的约定）：

- ``[A]`` 真实账号实测通过
- ``[B]`` APK 静态分析（结构确定，未经本仓库复测）
- ``[C]`` 单一来源，实现时必须用回读/校验兜底
"""

from __future__ import annotations

# --------------------------------------------------------------------------
# 域名与客户端身份
# --------------------------------------------------------------------------

#: 业务域名。可用环境变量 ``KEDAIBIAO_NEUSOFT_BASE_URL`` 覆盖。
DEFAULT_BASE_URL = "https://study.neusoft.edu.cn"

#: 登录接口对 UA 敏感：浏览器 UA 会走到另一条分支，[A]。
#: 这里用 uni-app 形态，与 App 端保持一致。
APP_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36 uni-app"
)

#: 东软外网租户。取不到租户列表时回退到它，[B]。
#: 注意 Web 端租户列表里大多是「（内网）」，用内网租户登录必失败，[A]。
FALLBACK_TENANT_ID = "0000000000000000000"

#: 租户名称里出现这个词就不选（内网租户登录必失败），[A]。
INTERNAL_TENANT_MARKER = "内网"

#: 默认的学校名匹配提示词，用于从租户列表挑生产租户。
DEFAULT_TENANT_HINT = "东软"

# --------------------------------------------------------------------------
# 请求头
# --------------------------------------------------------------------------

HEADER_ID_CODE = "Id-Code"
HEADER_TENANT = "Tenant-Id"
HEADER_AUTHORIZATION = "Authorization"
HEADER_CONTENT_TYPE = "Content-Type"
HEADER_USER_AGENT = "User-Agent"

CONTENT_TYPE_JSON = "application/json;charset=UTF-8"
ACCEPT_JSON = "application/json, text/plain, */*"

# --------------------------------------------------------------------------
# 端点
# --------------------------------------------------------------------------

WEB_API = "/web-api"

# -- 鉴权 --
#: 租户列表（登录前调用），[A]
TENANT_OPTION = f"{WEB_API}/system/tenant/app/option"
#: App 登录 —— **没有图形验证码**，这是本平台最关键的一条，[A]
APP_LOGIN = f"{WEB_API}/system/auth/app/login"
#: 刷新令牌，[C]
APP_REFRESH = f"{WEB_API}/system/auth/app/refresh"
#: 当前用户资料，用来探测会话是否还有效，[C]
USER_PROFILE = f"{WEB_API}/system/user/profile/get"

# -- 学期 / 课程 / 资源 --
TM = f"{WEB_API}/teachmanager"
#: 学期下拉，[A]
TERM_DROPDOWN = f"{TM}/teach-dropdown/getTeachTermDropDown"
#: 学期内课程分页，[A]
COURSE_PAGE = f"{TM}/teach-arrangement-stu/page"
#: 课程资源列表，[A]
RESOURCE_LIST = f"{TM}/teach-course-res-stu/listResource"
#: 课程目录树，[A]
DIRECTORY_TREE = f"{TM}/teach-course-directory-stu/listTree"

# -- 学习记录（刷课）--
RECORD = f"{TM}/teach-course-res-stu-record"
#: 回读学习记录 —— **完成判定的唯一依据**，[A]
GET_RECORD_INFO = f"{RECORD}/getRecordInfo"
#: 校验资源是否可学（有的资源会返回「未开放」），[A]
VALIDATE_RESOURCE = f"{RECORD}/validateStuResInfo"
#: 开始学习，[A]
START_STUDY = f"{RECORD}/startStudy"
#: 音视频进度上报，[A]
STUDY_AUDIO_VIDEO = f"{RECORD}/studyForAudioOrVideo"
#: 文档：开始（文档类专用），[A]
START_DOC = f"{RECORD}/startStudyForDocPicMd"
#: 文档：结束，[A]
END_DOC = f"{RECORD}/endStudyForDocPicMd"

# -- 学习任务（作业 / 测验）--
STU = f"{TM}/teach-course-res-stu"
#: 学习任务分页 —— **要 termId，不要 currentClassId**，[A]
STUDY_TASK_PAGE = f"{STU}/study-task/page"
#: 作业/测验作答接口前缀
EXAM = f"{WEB_API}/smartexam/test/student"
#: 试卷结构（不需要先 enter），[A]
EXAM_PAPER = f"{EXAM}/paper"
#: 进入作答（幂等，返回 candidateId / historyId），[A]
EXAM_ENTER = f"{EXAM}/enter"
#: 保存草稿，[B]
EXAM_ANSWER_SAVE = f"{EXAM}/answer/save"
#: 交卷 —— **不可逆**，[B]
EXAM_SUBMIT = f"{EXAM}/submit"
#: 是否分组组长，[B]
EXAM_CHECK_LEADER = f"{EXAM}/checkIsLeader"

# -- 考勤 --
ATT = f"{TM}/teach-course-attendance-detail"
#: 学生考勤分页（App 版），[A]
ATT_PAGE = f"{ATT}/getAppStuAttendancePage"
#: 提交签到，[A]
ATT_UPDATE = f"{ATT}/update"
#: 考勤主表详情（重开前取原状态用），[A]
ATT_GET = f"{TM}/teach-course-attendance/get"
#: 本人在某场次的明细 ID，[A]
ATT_DETAIL_ID = f"{ATT}/getAttendanceDetailId"

# --------------------------------------------------------------------------
# 枚举与魔数（全部来自实测，别凭直觉改）
# --------------------------------------------------------------------------

#: 资源类型 —— 实测 3 学期 489 个资源只出现这三种，[A]
RES_TYPE_VIDEO = 1
RES_TYPE_DOC = 3
RES_TYPE_IMAGE = 4
#: 可完成刷课的资源类型
COMPLETABLE_RES_TYPES = frozenset({RES_TYPE_VIDEO, RES_TYPE_DOC, RES_TYPE_IMAGE})

#: 学习状态，[A]
STUDY_STATUS_TODO = 0
STUDY_STATUS_DONE = 2

#: 学习任务里混着视频与作业，**必须按 type 过滤**，[A]
TASK_KIND_RESOURCE = 1
#: 单元测试 / 测验：走和作业同一套 smartexam 取卷-作答-交卷，只是类型值不同。
#: 之前只认 3，把这类任务整批丢掉了（见 docs/pigai… 类似事故：过滤太窄）。
TASK_KIND_TEST = 2
TASK_KIND_HOMEWORK = 3

#: 视频播放状态，[A]
PLAY_START = 1
PLAY_PAUSE = 2
PLAY_END = 3

#: 三段进度上报 `(playStatus, watchProgress)`。
#:
#: 进度用 **99999** 而不是 100 —— 服务端按「已看完」处理的是这个大数，[A]。
#: 顺序也别改：先开始、再暂停（带进度）、最后结束。
PROGRESS_STEPS: tuple[tuple[int, int], ...] = (
    (PLAY_START, 0),
    (PLAY_PAUSE, 99999),
    (PLAY_END, 99999),
)

#: 文档类资源上报后等待多久再回读（给服务端落库留时间），[A]
DOC_SETTLE_SECONDS = 1.6

#: 考勤：本人已签到后的 status，[A]
ATT_STATUS_PRESENT = 1
#: 考勤：已签的 signRole 取值，[A]
ATT_SIGNED_ROLES = frozenset({3, 4})
#: 考勤：需要定位的 locationType，[A]
ATT_LOCATION_TYPE = 1
#: 考勤：扫码签到（type=1）要带 refreshSeed，[A]
ATT_TYPE_SCAN = 1

#: 试卷里 `questionType` 的已知取值（**枚举不全**，只登记已验证的），[A]
#: 2026-10-22 用真实账号取到 50 题：``"1"`` 全是 4 选 1 的单选题，
#: ``"3"`` 全部是「A=正确 / B=错误」两选项的判断题（见
#: ``data/recon/neusoft-paper-*.json``）。
QUESTION_TYPE_SINGLE = "1"
QUESTION_TYPE_JUDGE = "3"
QUESTION_TYPE_FILL_MULTI = "4"
QUESTION_TYPE_COMPOSITE = ("8", "10")
