"""超星 / 学习通的只读页面入口（候选，待首跑校准）。

## 为什么是「一组候选」而不是「一个常量」

同一门课在不同账号上，超星可能给 PC 版页面、H5 版页面或新版个人空间，
入口域名也在 ``mooc1`` / ``mooc2-ans`` / ``i`` 之间换。写死一条，在别人账号上
就是 404；写死一条再「猜」还不如给几条、按顺序试。

## 校准流程

1. 带登录态跑一次 ``scripts/chaoxing_peek.py --dump data/chaoxing``；
2. 看 ``data/chaoxing/*.html`` 里哪条链路返回了真实内容；
3. 把那条提升到本文件顶部并标注「已确认 + 日期 + 账号类型」。

在那之前，``EVIDENCE`` 里的每一条都只能算**候选**。
"""

from __future__ import annotations

#: 证据等级标记，供文档/日志引用
EVIDENCE = "candidate"

#: 课程列表候选入口
COURSE_LIST_URLS: tuple[str, ...] = (
    # 新版个人空间的课程数据接口（返回 HTML 片段，卡片里带 courseid/clazzid/enc）
    "https://mooc2-ans.chaoxing.com/mooc2-ans/visit/courselistdata"
    "?courseType=1&courseFolderId=0&courseFolderSize=0&superstarClass=0",
    # PC 版旧入口
    "https://mooc1.chaoxing.com/visit/courselistdata"
    "?courseType=1&courseFolderId=0&courseFolderSize=0&superstarClass=0",
    # 个人空间首页（SPA；只有浏览器跑 JS 之后才有内容）
    "https://i.chaoxing.com/base",
)

#: 作业 / 考试列表候选入口模板。
#:
#: 参数从课程卡片的 href 里取（``courseid`` / ``clazzid`` / ``cpi`` / ``enc``）——
#: 同一门课换个入口参数名可能不同，所以用 ``{...}`` 占位、缺项就不填。
WORK_LIST_URL_TEMPLATES: tuple[str, ...] = (
    "https://mooc1.chaoxing.com/mooc2/work/list"
    "?courseId={courseid}&classId={clazzid}&cpi={cpi}&ut=s&enc={enc}",
    "https://mooc2-ans.chaoxing.com/mooc2/work/list"
    "?courseId={courseid}&classId={clazzid}&cpi={cpi}&ut=s&enc={enc}",
)

#: 判断「拿到的页面是不是登录页」的特征串。用于在解析出 0 条时区分
#: 「这门课确实没作业」和「登录态失效了」—— 两者都表现为空列表，但处理方式相反。
LOGIN_MARKERS: tuple[str, ...] = (
    "请输入密码",
    "用户登录",
    "passport2.chaoxing.com",
    "login",
)
