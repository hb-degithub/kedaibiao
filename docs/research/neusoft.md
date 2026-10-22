# 东软智慧教育平台（NEUMOOC）协议档案

- 目标平台：`study.neusoft.edu.cn`（东软系，App 端 `*.neumooc.com` 同一套网关）
- 建档日期：2026-10-20（Asia/Shanghai）
- 用途：`platforms/neusoft/` 适配器的实现依据。**只记录事实，不转录第三方实现。**
- 事实来源与置信度：
  - `[A]` 已在真实账号上实测跑通（上游项目与油猴脚本共有的结论）
  - `[B]` 由 APK 静态分析 / dex 指令级证实（结构确定，未由本仓库复测）
  - `[C]` 单一来源、未交叉验证 —— 实现时必须用 `validate_*` 或回读兜底

> 本档案只写「接口长什么样、字段叫什么、坑在哪」，不写实现代码。
> 代码一律在本仓库独立重写（上游为 PolyForm NC，与本项目 AGPL 不兼容，见 `license-policy.md`）。

---

## 0. 结论速览

| 能力 | 端点 | 置信度 |
| --- | --- | --- |
| 登录（免验证码） | `POST /web-api/system/auth/app/login` | A |
| 租户解析 | `GET /web-api/system/tenant/app/option` | A |
| 刷新令牌 | `POST /web-api/system/auth/app/refresh` | C |
| 当前用户 | `GET /web-api/system/user/profile/get` | C |
| 学期列表 | `GET /web-api/teachmanager/teach-dropdown/getTeachTermDropDown` | A |
| 课程列表 | `GET /web-api/teachmanager/teach-arrangement-stu/page` | A |
| 资源列表 | `POST /web-api/teachmanager/teach-course-res-stu/listResource` | A |
| 目录树 | `POST /web-api/teachmanager/teach-course-directory-stu/listTree` | A |
| 视频完成 | `POST .../teach-course-res-stu-record/{startStudy,studyForAudioOrVideo,getRecordInfo}` | A |
| 文档完成 | `POST .../teach-course-res-stu-record/{startStudy,startStudyForDocPicMd,endStudyForDocPicMd}` | A |
| 作业列表 | `POST .../teach-course-res-stu/study-task/page` | A |
| 作业题目 | `GET /web-api/smartexam/test/student/paper?testId=` | A |
| 进入作答 | `GET /web-api/smartexam/test/student/enter?testId=` | A |
| 保存草稿 | `POST /web-api/smartexam/test/student/answer/save` | B |
| 交卷 | `PUT /web-api/smartexam/test/student/submit?historyId=` | B |
| 考勤列表 | `POST .../teach-course-attendance-detail/getAppStuAttendancePage` | A |
| 提交签到 | `PUT .../teach-course-attendance-detail/update` | A |

---

## 1. 公共请求约定

### 1.1 请求头

| 头 | 值 | 备注 |
| --- | --- | --- |
| `Content-Type` | `application/json;charset=UTF-8` | POST/PUT 一律 JSON |
| `Accept` | `application/json, text/plain, */*` | |
| `Id-Code` | 见 §1.2 | **每个请求都要**，包括登录 |
| `Tenant-Id` | 租户 ID | 登录前也要带，否则租户解析不出来 |
| `Authorization` | `Bearer <accessToken>` | 登录接口不带 |
| `User-Agent` | **uni-app 风格** | 登录接口对 UA 敏感，浏览器 UA 会走另一分支 |

### 1.2 `Id-Code` 签名 `[B]`

```
明文 = "{userId}_{uuid}_{相对路径}"        # 未登录时 userId 记为 "0"
密文 = AES-128-CBC(key = iv = "neuedu_nse_12345", 零填充, 明文)
Id-Code = base64(密文)
```

四条容易踩的：

1. key 与 iv **是同一个常量**；
2. **零填充**不是 PKCS#7：按 16 向上取整，**恰好整除时不补块**；空明文补一个整块；
3. 明文用**相对路径**（`/web-api/...`），**不含域名与 query**；
4. 无 timestamp / nonce，服务端**只校验能解密**，uuid 用随机 v4 即可。

> 跨语言校验向量见 `tests/test_neusoft_signing.py`，移植错一位立刻失败。

### 1.3 响应外壳

```json
{ "code": 0, "msg": "", "data": ... }
```

- `code == 0` 才算成功，**成功数据在 `data` 里**；
- 非 2xx 的 HTTP 状态也要当失败，把原文带进异常（否则只剩一个状态码）；
- **`code == 0` 不等于业务完成** —— 见 §4 的回读要求。

---

## 2. 鉴权

### 2.1 租户解析 `[A]`

```
GET /web-api/system/tenant/app/option        →  [{tenantId|id, name, website}, ...]
```

- Web 端租户列表大多是「（内网）」，**用内网租户登录必失败**；
- 必须选名称不含「内网」且匹配学校名的条目；
- 彻底失败时回退常量 `0000000000000000000`（`[B]`，东软外网租户）。

### 2.2 App 登录 `[A]`

```
POST /web-api/system/auth/app/login
     Headers: Tenant-Id, Id-Code(userId="0"), uni-app UA
     Body:    {"username": "...", "password": "..."}
     → data:  {"userId", "tenantId", "accessToken", "refreshToken", "expiresTime"}
```

坑：

- 登录返回的 `tenantId` 才是权威值，要覆盖前面的解析结果；
- `code == 0` 但 `accessToken` 为空 = 登录失败，不能当成功；
- **App 接口没有图形验证码**，不需要任何验证码对抗 —— 这是本平台最重要的一条。

### 2.3 会话与吊销 `[A]`

- token 会被**服务端提前吊销**，本地 `expiresTime` 仍显示有效；
- 因此每个业务请求都要能在 401 时**自动重登一次再重试**，不能只靠本地过期判断；
- 重登需要凭据。适配器从 `Credentials` 拿，但**账号库不存密码**，所以会话必须能 `export_session` / `import_session` 落盘（含 accessToken / refreshToken / tenantId / userId / expiresTime）。

---

## 3. 课程与资源

```
GET  /web-api/teachmanager/teach-dropdown/getTeachTermDropDown
     → [{id, name}] 或 {list: [...]}        # 两种形状都要收

GET  /web-api/teachmanager/teach-arrangement-stu/page?termId=&pageNo=1&pageSize=100
     → data.list[]: {teachCourseId, teachClassId, ...}
     # teachClassId 就是资源接口要的 currentClassId

POST /web-api/teachmanager/teach-course-res-stu/listResource
     Body: {teachCourseId, currentClassId, name:"", resType:"", studyStatus:"",
            sortType:1, isAssess:"", parentId:"0"}
     → data.resList[] 与 data.folderList[]

POST /web-api/teachmanager/teach-course-directory-stu/listTree
     → data.treeList[]
```

- 多数课程 `folderList` 为空、`resList` 直接给全部资源；有目录时**要递归**（带 seen 防环）；
- 资源类型实测只有三种：`resType` 1=视频、3=文档、4=图片；
- 学习状态：`studyStatus` 0=未完成、2=已完成。

---

## 4. 刷课

全部在 `POST /web-api/teachmanager/teach-course-res-stu-record/*`，body 公共部分：

```json
{"teachCourseId": "...", "currentClassId": "...", "resId": "..."}
```

### 4.1 视频 `[A]`（实测 6/6 成功）

```
POST {RECORD}/startStudy                  # 公共 body
POST {RECORD}/studyForAudioOrVideo        # ×3，加 playStatus / watchProgress
POST {RECORD}/getRecordInfo               # 回读，判 studyStatus
```

三段上报（顺序与数值都是实测出来的，别改）：

| playStatus | watchProgress | 含义 |
| --- | --- | --- |
| 1 | 0 | 开始播放 |
| 2 | **99999** | 暂停时上报进度。**用 99999 而不是 100** |
| 3 | **99999** | 结束 |

### 4.2 文档 `[A]`

```
POST {RECORD}/startStudy
POST {RECORD}/startStudyForDocPicMd
   等待 ~1.6s
POST {RECORD}/endStudyForDocPicMd
POST {RECORD}/getRecordInfo
```

### 4.3 完成判定（硬规则）

> **`code == 0` 不算完成，只有回读 `studyStatus == 2` 才算。**

- 文档类曾出现「全链路 code=0、`studyStatus` 纹丝不动」；
- 所以完成函数返回 `bool`（是否**确认**完成），不是 `None` / `Result`；
- 拿不到回读结果时按**未完成**处理，绝不能把失败当成功。

`getRecordInfo` 另有 `studyStatus` 时而数字时而字符串的情况，两种都要接。

---

## 5. 作业 `[A]`（读路径已验证；写路径 `[B]`，本仓库未实测）

### 5.1 入口：学习任务，不是课程资源

```
POST /web-api/teachmanager/teach-course-res-stu/study-task/page
     Body: {"pageNo": 1, "pageSize": 200, "termId": "..."}
     → data.list[]
```

两个致命坑：

1. **要 `termId`，不要 `currentClassId`**。传课程上下文一律 `403 您权限不足或者非法访问`；
2. 列表混着视频与作业，**必须按 `type` 过滤**：1=视频/资料，3=作业/测验。

### 5.2 `testId` 就是 `ctlId`

```
列表行: {resId, ctlId, dataId, resName, teachCourseName, type, studyStatus, openTime, ...}
                 ↑ ctlId == resCtl.id == 作答页要的 testId
```

### 5.3 取题、进入作答、保存、交卷

```
GET  /web-api/smartexam/test/student/paper?testId=      → 试卷结构（不需先 enter）
GET  /web-api/smartexam/test/student/enter?testId=      → candidateId / historyId / 完整试卷
POST /web-api/smartexam/test/student/answer/save
     Body: {"candidateId", "testId", "historyId",
            "answers": [{"paperId", "questionNo", "questionType", "answer"}]}
PUT  /web-api/smartexam/test/student/submit?historyId=  # 不可逆
```

- `enter` 幂等：连调多次返回同一个 `historyId`，不消耗作答次数；
- 已交过 → 「剩余作答次数不足」；过期 → 「作答时间已截止」（都是业务提示，不是权限问题）；
- `answer/save` 是**草稿**；返回体 `isSubmitPaper` 非空才表示同时交卷。

### 5.4 题目字段形状（都是坑）

| 字段 | 形状 |
| --- | --- |
| `questionType` | **字符串**枚举；`"1"`=四选一单选，`"3"`=正/误判断题，`"4"`=多空填空，`"8"`/`"10"`=组合题（枚举不全） |
| `stem` / `stemText` | 题干 HTML / 纯文本，优先 `stemText` |
| `choices[]` | `{choiceId, choiceContent}` |
| `candidateAnswer` / `standardAnswer` | **三态**：数组 / 逗号串 / `null` |
| `questionNo` | 字符串（响应里可能是数字）；**按试卷唯一**，题型分组后才会重复的风险没有出现 |

### 5.5 那张「交卷一条答案都映射不上」的教训（2026-10-22 真实响应）

`[A]` 真实账号取到 50 题（`data/recon/neusoft-paper-<testId>.json`），实测结构与
旧假设**不在同一层**：

```jsonc
{ "id": "68b0…c985", "name": "期末测试",
  "paperParts": [{
    "partId": "68b0…c986",
    "paperQuestionList": [{
      "id": "68b0…c997",        // ← 逐题唯一，题目行主键
      "paperId": "68b0…c985",   // ← 整份试卷共用（= 顶层 id）
      "partId": "68b0…c986",
      "questionType": "1",
      "paperQuestion": {        // ← 题面在此层，且这里**没有** paperId / id
        "questionNo": 17, "questionType": "1",
        "stemText": "下列对创新思维的性质的描述，错误的一项是？",
        "choices": [{"choiceId": "B", "choiceContent": "开放性"},
                    {"choiceId": "D", "choiceContent": "从众性"}, …],
        "candidateAnswer": null, "standardAnswer": null
      }}]}]}
```

四条必须先记住的事实：

1. **`paperId` 不是逐题标识**：50 道题共用一个 `paperId`。拿它当字典键建索引，
   会把整份试卷压成一条答案行 —— 保存时逐条校验又能过，最终表现是「没有一条
   答案能映射到平台题目」。逐题唯一的是外层 `id`。
2. **`choiceId` 是字母且顺序打乱**：选项数组不是 A、B、C、D 的顺序
   （实测第一题是 `B/D/C/A`）。所以「下标 → 该位置 `choiceId`」是唯一正确的换算，
   不能拿位置当字母用。
3. **文本字段会被二次转义**：`choiceContent` 可能是 `"\\t实用性"`（字面量反斜杠 t）。
   原样显示就是「\实用性」，进题库还会污染指纹。解析时解 `\t`/`\n`/`\r` 再 trim。
4. **`questionType` 是字符串**，`"1"` 与 `"3"` 在此之前没被登记 → 全部归 `UNKNOWN`，
   于是规则兜底不参与、界面没有题型标签。

`answer/save` 的行仍然是 `{"paperId", "questionNo", "questionType", "answer"}`：
`paperId` 原样取外层的值（全卷相同），`questionNo` 取内层，`answer` 是该题所选
选项的 `choiceId`（多选用逗号拼接）。写路径仍只有 `[B]` 级证据。

---

## 5.6 成绩回读（只读）

没有单独的「查分」接口可依；成绩就在**同一张试卷结构**里：交卷并被批改后，
`paperQuestion.candidateScore` / `questionScore` 会被写回，累加即本次得分。

```
GET /web-api/smartexam/test/student/paper?testId=    # 再取一次，看 candidateScore
```

- 交卷前这两个字段是 `null`（实测），所以「0 分」和「还没批改」必须分开报 ——
  `candidateScore` 全为 `null` 时只能说「平台还没批改」，不能报 0 分；
- 控制台动作 `score`（只读）走这条路径，返回「得分/满分 + 逐题得分」；
  交卷成功后也会**尽力回读一次**并把结果写进回执（读不到不影响交卷结论）；
- 交卷后会话可能被平台吊销（实测：交卷成功后紧接着的读请求 401），
  这时成绩要等重新登录后再查。

### 5.6.1 交卷之后，这份试卷视图会「变空」（2026-10-22 实测）

交卷两小时后复查（同一 testId、同一账号）：

| 观察 | 结果 | 含义 |
| --- | --- | --- |
| 学习任务列表 | 该任务**从列表消失**（7 项 → 6 项） | 平台只列未完成任务；`detail` 因此报「找不到作业」 |
| `paperQuestion.candidateAnswer` | 50 题**全部为空** | 交卷后试卷视图不再回传本人作答 |
| `paperQuestion.candidateScore` | 50 题**全部为 null** | 尚未批改/未发布，`score` 动作如实报「还没批改」 |
| 平台 App / 网页 | 学生侧同样看不到分数 | 不是接口问题，是平台还没放分 |

所以：**交卷成功 ≠ 立刻有分**。分数要么等平台批改发布，要么存在另一个
「成绩/答卷」接口（本仓库尚无证据，未登记 —— 不要凭猜测路径去撞）。
判断「已交卷」的可信依据是提交回执 `isSuccess/isPerfect` + 任务从待办消失，
不是分数出现。

## 6. 考勤 `[A]`

```
POST /web-api/teachmanager/teach-course-attendance-detail/getAppStuAttendancePage
     Body: {"studentId", "termId", "courseId": null,
            "attendanceStatus": null, "status": null}
     → data.list[]

PUT  /web-api/teachmanager/teach-course-attendance-detail/update
     Body: {"attendanceId", "id", "status": 1, "type", "signRole", "signUserId"}
     # 定位签到再带 signLongitude / signLatitude / signAddressName
     # type == 1（扫码）再带 refreshSeed
```

### 6.1 判「是否可签」的三条规则

1. **`status` 为 `null` 也算进行中** —— 新发布的场次服务端就这么返回，只判 `== 1` 会整批漏掉；
2. 已签判据是 **`signRole ∈ {3,4}` 或 `signTime` 非空**，只看一个会重复提交；
3. 定位签到（`locationType == 1`）需要坐标，没有坐标不要硬签。

`getAttendanceDetailId/{attendanceId}/{studentId}` 取明细 `id`；返回空串表示不在该教学班。

---

## 7. 本适配器的取舍

- **不做**：验证码对抗（本平台 App 接口没有验证码）、越权改他人数据、伪造监考日志；
- **不做**：批量交卷。`submit` 走 `dry_run` + 最低置信度门槛，与其它平台一致；
- **必须做**：回读确认、401 自动重登、会话导出/导入。
- 已实测能力之外的写路径（`answer/save`、`submit`）默认按 `[B]` 处理：
  提交前要求调用方显式放行，与 `pinco` 的 `dangerous=True` 同一套思路。

---

## 8. 待补

| # | 项 | 怎么补 |
| --- | --- | --- |
| 1 | `questionType` 完整枚举 | `GET /web-api/system/dict-data/simple-list?type=question_type` 或真实试卷统计 |
| 2 | 组合题 `parentQuestionNo` | 需要一份真实组合题样本 |
| 3 | `refreshToken` 的正确域名 | APK 里指向测试域名，生产需实测 |
| 4 | 考核类是否需要 `currentClassId` | 资源类必需（已双向确认），考核类未见 |

---

## 9. 本仓库实测记录（2026-10-20）

只读冒烟：不登录、不改任何数据，只用真实网关验证「域名 / 请求形状 / 外壳解析」。
脚本一次一跑，未落盘任何响应。

| # | 探测 | 结果 | 结论 |
| --- | --- | --- | --- |
| 1 | `GET /web-api/system/tenant/app/option` | HTTP 200，4 条租户，首条 `示例学院（外网）`，`website` 就是业务域名 | 域名可达、响应外壳与档案一致 |
| 2 | 按名称解析租户 | 命中 `示例学院（外网）` | §2.1 的解析规则可用 |
| 3 | 该部署的租户列表 | **没有「内网」条目** | 「内网」过滤是防御性代码，本部署不触发 |
| 4 | `POST /system/auth/app/login`（不存在的假账号） | `code=1010005006`「登录失败，账号不存在」 | `Tenant-Id` + App UA + 请求体形状被接受 |
| 5 | 同一请求，**故意改坏 / 删掉 `Id-Code`** | 与 #4 **完全相同**的业务错误 | **登录接口不校验 `Id-Code`**（见下） |

### 关于 #5 的结论边界

- **只能证明「登录路径不校验」**：业务接口是否校验 `Id-Code` 无法在未登录状态下验证。
- 实现上仍然每个请求都带 `Id-Code`：APK 是这样做的（`[B]`），且服务端随时可能收紧；
  带一个正确签名没有代价。
- 这条记录的价值是：**以后遇到 401 时不要先怀疑签名** —— 登录路径上它根本不是原因。

### 尚未实测（本仓库无可用账号）

| 能力 | 为什么没测 |
| --- | --- |
| 课程 / 资源列表 | 需要登录态 |
| 刷课（视频 / 文档） | 需要登录态，且会**改平台数据** |
| 作业取题 / 存草稿 / 交卷 | 写路径，且交卷不可逆 |
| 考勤查询 / 签到 | 需要登录态，签到不可逆 |

这些能力都有**离线测试**（用假传输层钉报文形状与判定逻辑），
但「线上真的能跑通」需要真实账号 —— 由使用者在控制台绑定账号后自行验证。

---

## 10. 本仓库实现状态

| 模块 | 文件 | 覆盖 |
| --- | --- | --- |
| 签名 | `platforms/neusoft/signing.py` | `Id-Code`（AES-CBC + 零填充），4 条跨语言向量钉住 |
| 请求层 | `platforms/neusoft/client.py` | 公共头、外壳解包、401 自动重登、租户解析、登录 |
| 学期 | `platforms/neusoft/terms.py` | 学期下拉（两种形状都收） |
| 课程/资源 | `platforms/neusoft/resources.py` | 课程分页、资源列表、目录递归（seen 防环） |
| 刷课 | `platforms/neusoft/study.py` | 视频三段上报、文档流程、**回读确认** |
| 作业 | `platforms/neusoft/homework.py` | 学习任务（过滤 type=3）、取题、进入、存草稿、交卷（默认拒绝） |
| 考勤 | `platforms/neusoft/attendance.py` | 场次列表、三条签到守卫、提交签到 |
| 适配器 | `platforms/neusoft/adapter.py` | 框架契约 + 刷课/签到编排 + 会话导出导入 |
| 控制台动作 | `api/actions.py`（`_register_neusoft`） | `resources` / `study` / `attendance` / `sign` |

两条贯穿全部实现的硬规则：

1. **`code == 0` 不等于完成** —— 刷课一律回读 `studyStatus == 2`；
2. **不可逆写操作默认不开闸** —— 交卷要 `KEDAIBIAO_NEUSOFT_ALLOW_SUBMIT=1`，
   签到与交卷的前置守卫在发请求之前拦截。
