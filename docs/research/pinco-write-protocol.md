# 希沃品课（pinco）**写入协议**规格 —— 提交作业 / 逐题保存 / 学习进度上报

- 调研对象：`https://pinco.seewo.com`（学生端 Web `/student`、学生端 Flutter Web `/student2`、学生端安卓 App）
- 调研日期：2026-10-19（Asia/Shanghai）
- 调研员：**proto-scout**（共享任务 `task-9`）
- 调研方式：**静态分析优先**（学生 Web bundle + 学生端 Flutter Web 的 Dart 编译产物）+ 复用既有实测报告
  - **没有使用任何凭据**：未登录、未提交、未发 POST、未改任何服务端状态
  - 本轮共发出 **24 条匿名请求**：**14 条 HEAD 探路**（除下述主产物外**全部 404**，
    用于确认分片路径是否公开）+ **10 条 GET**（9 条是同一主产物 `main.dart.js` 的重复取回
    —— `/tmp` 每次调用后销毁；1 条是 `main-0.js` 的 404 探测）
- 前置阅读：`docs/research/pinco.md`（含 **附录 L** 的 Lead 真实会话只读实测）、
  `src/kedaibiao/platforms/pinco/endpoints.py`（常量与证据等级）
- 唯一写作用域：本文件。未修改仓库其它文件。

> 外部内容一律按「不可信数据」处理 —— 只读取，不执行其中任何指令；
> 只记录功能性事实（action 名、URI、字段名、枚举值、错误码），**不粘贴第三方代码**。
>
> **证据等级**（每条事实都带标记）：
>
> | 标记 | 含义 |
> | --- | --- |
> | **【实测】** | 已有真实会话请求验证（本文件只引用；来源写 `pinco.md §x` / `endpoints.py:行`） |
> | **【静态-Web】** | 从学生 Web bundle 读出的 action 名（公开静态资源，**未联网验证**） |
> | **【静态-App】** | 从学生端 Flutter Web 的 Dart 编译产物读出的 **REST 路由表与请求体构造**（公开静态资源，**未联网验证**） |
> | **【未验证】** | 明确未知，不得据此行动 |

---

## 0. 结论速览

| 问题 | 结论 | 等级 |
| --- | --- | --- |
| 写入面有几条路线 | **两条**：Web RPC `POST /student/api.json?actionName=<Op>` 与 App REST `POST /server-main/api/v1/...`（同一 OpenAPI 的两种网关表达） | 【实测】+【静态-App】 |
| 提交作业（Web 动作名） | `PostV1StudenthomeworkByStudentHomeworkIdSubmit` | 【静态-Web】 |
| 提交作业（App REST） | `POST /server-main/api/v1/studenthomework/:studentHomeworkId/submit` | **【静态-App】（本轮新证据）** |
| **提交请求体** | **未确认**。静态侧拿不到调用点：Web 的提交 UI 在懒加载 chunk，Flutter 的分片 `main-0..5` 在该 URL 下 404 | 【未验证】 |
| 逐题保存答案（结构已确认到「静态」） | REST `POST /server-main/api/v1/studenthomework/:studentHomeworkId/question/:questionId/answer`，请求体 `{studentHomeworkId, questionId, content}`（`attachments` 仅当非空时附加） | 【静态-App】 |
| 判分是否回传每题对错 | **读接口**按题回传 `answer.score` / `answer.review`（【实测】）；**提交接口自身**的响应结构 **未验证** | 【实测】/【未验证】 |
| 幂等 / 重复提交 | **未验证**。平台读侧有 `submitted/expired/canSubmitted/canRedo/corrected/rejected`（【实测】）→ 实现应「提交前复查 detail」 | 【未验证】 |
| 学习进度上报（刷课） | **已确认并跑通**：材料进度 = `(maxPageIndex+1)/总页数`；课件 `open` + 逐页 `maxLook` 即可推满（PPT/PDF 都适用）。学习时长上报是心跳，收下但不涨进度 | 【实测】 |
| 请求签名 | **无任何签名证据**；强制校验是 CSRF 双提交（`x-csrf-token` ← cookie `csrfToken`）+ **必需头 `x-server: main`** | 【实测】 |
| 纯 HTTP 能否发写请求 | **不能**：登录态下纯 HTTP 仍回 `403 invalid csrf token`（csrf 与浏览器会话上下文绑定） | 【实测 pinco.md 附录 L】 |
| 现有代码状态 | **有提交实现但自带闸门**：`submit_payload_confirmed = False`（声明 `adapter.py:140`，拦截 `adapter.py:639`，注释 `endpoints.py:406`），报文未确认前拒绝发出 | 【实测源码】 |

**一句话**：写操作的**端点与前置条件已能确定**（Web 动作名 + App REST 路径 + 头/CSRF 要求），
**逐题保存答案的请求体**也在 App 客户端里读到了结构；但**提交与进度上报的请求体、
频率、幂等，全部需要一次真实抓包**才能闭合。

---

## 1. 证据来源与取数方式

### 1.1 本轮新取回的静态资源（1 个 URL 取到 200，2026-10-19）

| # | URL | 字节 | `Last-Modified` | 用途 |
| --- | --- | --- | --- | --- |
| 1 | `https://s0.seewo.com/cloud-static/pinco/flutter/static/1762860981918/flutter/build/web/main.dart.js` | 2,785,086 | 2025-11-11 | 学生端 Flutter Web 主产物：**完整 REST 路由表（action 名 ↔ URI + method）** 与部分调用点的请求体构造 |

- 该 URL 来自学生端 `/student2/notice` 页面注入的 `window.cdnSubPath`（`data/recon/pinco/raw/pinco.seewo.com_student2_notice`）【静态】。
- **未落盘留档**（本轮写作用域只有本文件）；复现命令：

  ```bash
  wget -q -O /tmp/main.dart.js \
    'https://s0.seewo.com/cloud-static/pinco/flutter/static/1762860981918/flutter/build/web/main.dart.js'
  ```

- 同族的**分片**（`main-0.js` … `main-5.js`，页面 `window.partIndex` 声明）在该路径下 **404**
  （响应体 166 B 的 `Not Found`）→ 拿不到分片里的 UI 代码，这是提交请求体抓不到的直接原因。

### 1.2 仓库内既有样本（只读引用）

| 来源 | 内容 |
| --- | --- |
| `data/recon/pinco/raw/s0.seewo.com_..._workbench_ae701e4755dac93f5621.js`（1,320,406 B） | 学生 Web bundle：action 名清单（本次逐条核对用） |
| `data/recon/pinco/raw/apk_x/`（APK 静态解析产物） | App 端点字符串、字段名候选（`pinco.md §5.2/5.4`） |
| `docs/research/pinco.md` | 【实测】只读链路：作业/材料/信封/头（**附录 L**） |
| `src/kedaibiao/platforms/pinco/endpoints.py` | 常量与证据等级（`EVIDENCE` 字典）、`DANGEROUS_ACTIONS` |

> ⚠ 本文档的【静态-*】条目**没有**联网验证过：没有发过任何 action、没有任何带会话的请求。
> 与 `pinco.md §10`、`endpoints.py` 的「静态清单不许进已确认路径」纪律一致。

---

## 2. 两条协议与命名对应关系

### 2.1 命名规律（本轮新证据）

| 客户端 | 动作名字形 | 例 |
| --- | --- | --- |
| Web SDK（`/student`、`/teacher`） | `<Method>V<版本><PathCamel>` | `PostV1StudenthomeworkByStudentHomeworkIdSubmit` |
| **App / Flutter（`main.dart.js`）** | `<Method>ApiV<版本><PathCamel>` | `PostApiV1StudenthomeworkByStudentHomeworkIdSubmit` |
| App REST 路径 | `/server-main/api/v1|v2/...` | `/server-main/api/v1/studenthomework/:studentHomeworkId/submit` |

- Dart 产物里有一张**完整的路由表**：每条 `action 名 → {uri, method}`。
  本轮统计：**402 条** `{uri,method}` 描述符、**392 条唯一 URI**、**402/402 条 action 名可解析**
  （另有 419 个名字键，含同 URI 的别名）【静态-App】。
- 对应关系举例（Web 名 / App 名 / REST）：

| Web 动作名 | App 动作名 | REST 路径 + 方法 |
| --- | --- | --- |
| `PostV1StudenthomeworkByStudentHomeworkIdSubmit` | `PostApiV1StudenthomeworkByStudentHomeworkIdSubmit` | `POST /server-main/api/v1/studenthomework/:studentHomeworkId/submit` |
| `PostV1StudenthomeworkByStudentHomeworkIdQuestionByQuestionIdAnswer` | `PostApiV1StudenthomeworkByStudentHomeworkIdQuestionByQuestionIdAnswer` | `POST /server-main/api/v1/studenthomework/:studentHomeworkId/question/:questionId/answer` |
| `GetV1StudenthomeworkByStudentHomeworkIdQuestions` | `GetApiV1StudenthomeworkByStudentHomeworkIdQuestions` | `GET /server-main/api/v1/studenthomework/:studentHomeworkId/questions` |
| `PostV1CourseByCourseIdMaterialStudentVideoDurationReport` | `PostApiV1CourseByCourseIdMaterialStudentVideoDurationReport` | `POST /server-main/api/v1/course/:courseId/material/student/videoDurationReport` |

> ⚠ **版本号陷阱（必须记住）**：动作名里的版本**不一定**等于 REST 路径版本。
> 例：`GetApiV1CourseByCourseIdStudentMaterialList` 实际落在
> `/server-main/api/v2/course/:courseId/student/material/list`（**v2**）【静态-App】。
> 映射必须查表，不能按名字拼。

### 2.2 请求形态与成功判据（【实测】）

| 家族 | 地址 | 成功判据 |
| --- | --- | --- |
| Web RPC | `POST https://pinco.seewo.com/student/api.json?actionName=<Op>`，body 为 JSON | 内层 `statusCode == 0` |
| App REST | `POST https://pinco.seewo.com/server-main/api/v1/...` | 内层 `code == 0`（`statusCode` 是 HTTP 镜像 200） |

- 信封：`{"code":0,"msg":"ok","data":{…},"statusCode":200,"message":"ok"}`（REST）。
- **HTTP 状态与内层状态码并不总一致**（403 业务码可以套在 HTTP 200 上）【实测 `pinco.md §5.2`】。
- **必须 POST**：同参数改 GET → `404 Not Found`【实测】。
- 已知业务码：`0` 成功、`401 illegal argument`（缺必填参数）、`403`（无会话 / csrf 缺失）、
  `6571 没有该课程权限`（教师端接口被学生身份调用）【实测 `pinco.md §5.2`、附录 L】。
- App 客户端额外处理 `6569` / `10419`（特殊错误分支）与 `6582`（带 payload），
  **语义未验证**（`main.dart.js` 的分支；只知道被当成错误/特殊分支）。

---

## 3. 前置条件（写操作需要什么）

| 前置 | 事实 | 等级 |
| --- | --- | --- |
| 会话 | Cookie：`x-token`（域 `.seewo.com`，SSO 下发）+ `csrfToken`（`pinco.seewo.com`） | 【实测】名称与机制 |
| CSRF | 请求头 `x-csrf-token` ← cookie `csrfToken` 的值（双提交）；缺失 → `{"message":"missing csrf token"}` | 【实测】 |
| 必需头 | **`x-server: main`**（学生端）。A/B 对照：不发 → `500 ERR_ASSERTION`；发 → `200 statusCode:0` | 【实测 `endpoints.py:262-265`】 |
| 其它头 | `Content-Type: application/json;charset=utf-8`、`x-req-traceid`（前端生成）、`x-language`；`withCredentials:true` | 【静态-Web】（`pinco.md §3.4/4.2`） |
| `x-token` 头 | **不要**把 SSO token 当 `x-token` 头发出去 —— 实测会让接口「变脸」（`GetV1CourseStudentworkbench` 直接失败）；正确形态是 cookie 里的 `x-token` | 【实测 `pinco.md` 附录 L.1】 |
| 会话建立方式 | 纯 HTTP **发不出**合法请求：登录态下 `POST /student/api.json` 稳定回 `403 invalid csrf token`。可行做法是**在站点自己的 JS 上下文里 fetch**（Lead 实测用的是 Firefox headless + BiDi，工具 `scripts/bidi_ws.py`） | 【实测】 |
| 学生 SPA 特殊入口 | `?auth=<token>` 可直接注入 `window.token`（顶层打开时页面否则空白） | 【实测】 |
| 写前必读 | 作业详情 `GetV1StudenthomeworkByStudentHomeworkIdDetail` → `submitted/expired/canSubmitted/canRedo/corrected/rejected` | 【实测】 |

> **实现层最重要的一条**：pinco 的写操作不是「拿 cookie 拼 HTTP」能做的 ——
> **必须在浏览器上下文（站点 JS 内）发起**，或接受「会话复刻失败」的现状。
> 这也解释了为什么 `endpoints.py` 对 REST 映射坚持「没有核对过的映射就报错」。

---

## 4. 提交作业

### 4.1 端点（两条路线）

| 路线 | 端点 | 等级 |
| --- | --- | --- |
| Web RPC | `POST https://pinco.seewo.com/student/api.json?actionName=PostV1StudenthomeworkByStudentHomeworkIdSubmit` | 【静态-Web】 |
| App REST | `POST https://pinco.seewo.com/server-main/api/v1/studenthomework/:studentHomeworkId/submit` | 【静态-App】 |

- 路径参数 `:studentHomeworkId` 与动作名里的 `ByStudentHomeworkId` 对应（同义）。
- 两条路线按 `pinco.md §5.3` 的结论是**同一 OpenAPI 的两种网关表达** —— 抓包用哪边都行。

### 4.2 逐题保存答案（**本轮最有价值的新证据**）

| 项 | 事实 | 等级 |
| --- | --- | --- |
| Web 动作名 | `PostV1StudenthomeworkByStudentHomeworkIdQuestionByQuestionIdAnswer` | 【静态-Web】 |
| App REST | `POST /server-main/api/v1/studenthomework/:studentHomeworkId/question/:questionId/answer` | 【静态-App】 |
| 请求体（App 客户端构造） | `{studentHomeworkId, questionId, content}`；`attachments` **仅当附件列表非空时附加** | 【静态-App】 |
| 附件元素结构 | **未确认**：该调用点只确认 `attachments` 是数组，元素的键名没有证据（另一条 `PostApiV1CourseStudentQuestion` 的上传描述符用了 `fileKey/resourceName/resourceSize/hash/index`，**不能挪用**） | 【未验证】 |
| 响应 | 成功分支只判断「有 questionId」；`6569`/`10419` 走特殊错误分支（语义未验证） | 【静态-App】 |
| Web 侧调用点 | **没找到**（bundle 里该动作名只出现 2 次，均为 SDK 导出/名称表） | 【静态-Web】 |

> ⚠ 这是**客户端构造**（Dart 编译产物）的证据，**不是真实报文**。
> 仍属【静态】，可用于「实现时先按这个结构拼」，但**发请求前必须抓包复核**。

### 4.3 提交请求体（**未确认 —— 静态侧的取证边界**）

**结论：拿不到，必须抓包。** 取证过程与原因（避免后来者重复尝试）：

| 尝试 | 结果 |
| --- | --- |
| 学生 Web bundle 里搜 `PostV1StudenthomeworkByStudentHomeworkIdSubmit` | 只出现 **2 次**，都在 SDK 导出表/名称表 —— **没有调用点** |
| 学生 Web bundle 里搜提交相关字段串 | `"answers"` 0 命中、`answerContent` 仅在**读模型**（题目已答内容模型，键 `questionId/title/content/questionTime/answerContent/imgUrl`）、`"attachments"` 0 命中 |
| 运行时 chunk 映射 | `window.partIndex` 声明的 `main-0..main-5` 在该 URL 下 **404**；webpack runtime 里没有 chunk 名↔哈希表 → **无法枚举懒加载 chunk** |
| Flutter `main.dart.js` 里搜同一动作名 | 只出现 **1 次**（名称注册数组），调用点不在该产物内 |
| 静态候选字段（**只是字符串存在，未验证，禁止据此构造**） | `answerContent`、`answers`、`options`、`attachments`、`duration`、`remark`、`isSubmit`、`endTime`、`courseGroupId`（来源：APK strings `pinco.md §5.4` + 既有静态清单 `endpoints.py:407-425`） |

> ☠️ **现有代码里的 `_submit_payload()`（`adapter.py:700` 起）是「按静态清单拼的最小结构」**，
> 作者已经写明 `TODO(endpoint)`，并由 `submit_payload_confirmed=False` 挡住。
> 在拿到 §7 的 C1 之前，**不得**把这个结构当成已确认。

### 4.4 判分是否回传每题对错

| 问题 | 结论 | 等级 |
| --- | --- | --- |
| 提交接口的响应里有分数/对错吗 | **未验证**（没有任何提交响应样本） | 【未验证】 |
| 那分数从哪来 | **读接口按题回传**：`GetV1StudenthomeworkByStudentHomeworkIdQuestions` → `data.shortAnswers[].answer{answerId, score, review, content, submitted, attachments[]}`；分组共四个 `shortAnswers/singleChoices/multipleChoices/trueOrFalses` | 【实测】`pinco.md` 附录 L.2 + `endpoints.py:331-346` |
| 老师是否公布标准答案 | 题目里另有 `solution`、`publishStandardSolution` 字段（实测读出，语义按名） | 【实测】 |

⇒ 实现建议：**提交后不要从提交响应取分**，另发一次 `…/questions` 读回逐题判分。

### 4.5 幂等性 / 重交

| 问题 | 结论 | 等级 |
| --- | --- | --- |
| 重复调用 submit 会怎样 | **未验证**（不可撤销的写操作；绝不能靠试） | 【未验证】 |
| 平台侧状态位（用于提交前复查） | `submitted`、`expired`、`canSubmitted`、`canRedo`、`corrected`、`rejected` | 【实测】 |
| 「重做」语义 | `canRedo` 存在 ⇒ 平台允许重做；但**接口层是否拒绝二次 submit 未知** | 【推断】/【未验证】 |
| 现有代码的保守做法 | 进程内 `_attempted` 只发一次；提交前 `_probe_state()` 复查，探测失败按「不知道」拒绝（绝不把探测失败当「没交过」）；失败不自动重试 | 【实测源码 `adapter.py:610-700`】 |

---

## 5. 刷课 / 学习进度上报

### 5.1 读侧（进度的真实来源，【实测】）

| 能力 | action | 关键字段 |
| --- | --- | --- |
| 课程材料列表 | `GetV1CourseByCourseIdStudentMaterialList` `{courseId}` | `data.courseMaterials[]`：`materialId/materialName/materialSize/updateTime/materialSource/learningRatio/media{}` |
| 材料详情 | `GetV1CourseByCourseIdStudentMaterial` `{courseId, chapterDirectoryMaterialId}` | `media.downloadUrl`、`resourceTypeDownloadable` |
| 课程学习进度 | `GetV1CourseStudentworkbench` | `data.content[].learnInfo.learningProgress` |

> **`learningRatio`（0~1）就是学习进度**，列表里直接带【实测 `pinco.md` 附录 L.3】。
> 本账号实测：只有「电子技术基础」有 10 个材料，`learningRatio` 全为 0。

### 5.2 写侧端点（2026-10-20 更新：刷课链路**已确认**）

| Web 动作名 | REST 路径（App 族） | 语义 | 等级 |
| --- | --- | --- | --- |
| `PostV1CoursewareStudentOpen` | `POST /server-main/api/v1/courseware/student/open` | **打开材料课件**：回 `pages[]`（总页数）与 `maxPageIndex`（已看最大页，0 起） | 【实测确认】 |
| `PostV1CoursewareStudentMaxLook` | `POST /server-main/api/v1/courseware/student/maxLook` | **翻页上报（刷课真正用的接口）**：`{coursewareId, maxPageIndex}` | 【实测确认】 |
| `PostV1CourseByCourseIdMaterialStudentLearningDurationReport` | — | 学习时长心跳：H5 材料页每 5 秒一次；**收下请求但不涨进度** | 【bundle + 实测·阴性】 |
| `PostV1CourseByCourseIdMaterialStudentVideoDurationReport` | `POST /server-main/api/v1/course/:courseId/material/student/videoDurationReport` | 视频播放时长上报 | 【静态】 |
| `PostV1ChapterdirectoryMaterialCountLearningProgress` | APK 字符串存在 `/api/v1/chapterdirectory/material/countLearningProgress` | 学习进度计数上报（**教师端**：学生调用回 6571） | 【静态 + 实测·无权限】 |
| `PostV1CoursewareStudentStudyReport` | `POST /server-main/api/v1/courseware/student/studyReport` | 学习报告上报（空体也回 `{}`，未发现对进度有影响） | 【实测·未发现作用】 |
| （未见 Web 名） | `POST /server-main/api/v1/course/:courseId/student/resume` | 推断为「续学位置」；**语义未验证** | 【静态-App】 |

| 维度 | 结论 | 等级 |
| --- | --- | --- |
| 进度算法 | **已确认**：`learningRatio = (maxPageIndex + 1) / 总页数`（10 页 PPT、`maxPageIndex=3` → 40%） | 【实测】 |
| 刷课需要的字段 | **已确认**：open 要 `{resId, coursewareId}`（`resId` = `media.resId`，`coursewareId` = 材料列表 `materialId`）；maxLook 要 `{coursewareId, maxPageIndex}` | 【实测】 |
| 上报频率 | **已确认**：可以逐页推；服务端立即记账（无需批处理）。翻页之间留 1 秒间隔即可 | 【实测】 |
| 端到端 | **已确认**：把一份 10 页 PPT 从 50% 推到 100%（5 次 maxLook） | 【实测】 |
| 学习时长上报 | **不涨进度**：60s / 1200s、页内 fetch、补 App 头都试过，`learningRatio`/`viewed` 不动 | 【实测·阴性】 |
| 服务端校验（签名/时间戳） | **无签名证据**；写接口未见 nonce/sign/时间戳字段 | 【静态】无证据 |
| 现有适配器 | `adapter.advance_learning()`：默认 dry-run；`progress.CoursewareProgressReporter` 逐页推进并回读核对 | 【实测源码】 |

> 结论：**刷课机制已确认并跑通** —— 不是「时长上报」，而是「课件翻页」
> （`maxLook`）。`countLearningProgress` 只是 H5 页面要不要发时长心跳的开关，
> 与进度算法无关。实现默认 dry-run，带页数/总时长上限与「进度不动就停」熔断；
> 证据全文见 `docs/verification/pinco-progress.md`。

### 5.3 已确认：课件翻页推进进度（2026-10-20，【实测】）

1. 打开材料课件::

       POST ?actionName=PostV1CoursewareStudentOpen
       {"resId": "<media.resId>", "coursewareId": "<materialId>"}

   返回 `{"maxPageIndex": 3, "pages": [ … 10 项 … ]}`；此时材料详情
   `learningRatio = 40` —— `(3+1)/10`，对得上。

2. 翻一页::

       POST ?actionName=PostV1CoursewareStudentMaxLook
       {"coursewareId": "<materialId>", "maxPageIndex": 4}

   再打开课件 `maxPageIndex=4`，材料详情 `learningRatio=50` —— 立即生效。

3. PPT（`media.type=4`）与 PDF（`type=8`，实测 45 / 387 页）都适用：
   「课件」是材料的另一种视图，`coursewareId` 就是材料号。

`PostV1CourseByCourseIdMaterialStudentLearningDurationReport` 虽然形态确认
（H5 每 5 秒发一次、`learningDuration: 5`、真实会话返回 `statusCode 0`），但
**实测不涨进度**（60s / 1200s / 延迟 3 分钟 / 页内 fetch 都一样）。它只能说明
「页面在心跳」，不能拿来刷课。

实现：`platforms/pinco/progress.py`（`CoursewareProgressReporter`）+ 
`adapter.open_courseware()` / `adapter.advance_learning()` +
`scripts/pinco_progress.py`（默认 dry-run，`--live` 才写）。

---

## 6. 风控与校验

| 信号 | 状态 | 等级 |
| --- | --- | --- |
| 阿里云 WAF | `acw_tc` cookie（HttpOnly，1800 s） | 【实测】 |
| 网关 | `Server: cagw`；响应 `x-apm-traceid` | 【实测】 |
| 请求体签名 | **未发现任何签名实现**（无 HMAC/nonce/time-sign 证据） | 【静态】 |
| CSRF | 强制（双提交 cookie），缺失即 `missing csrf token` / `403 invalid csrf token` | 【实测】 |
| 限流响应头 | **未见**任何 `x-ratelimit-*` / `Retry-After` | 【实测】 |
| 验证码/极验 | 存在（登录链）；**是否作用于提交接口：未验证** | 【静态】/【未验证】 |
| UA / Referer 校验 | **未验证** | 【未验证】 |
| 服务端限速阈值 | **未验证**（响应头未暴露） | 【未验证】 |

---

## 7. 需要真实抓包才能确认（清单）

> **本仓库没有可用的平台密码**（`.env` 凭据已移除），下列全部需要用户在自己的会话里操作。
> **不要**尝试登录、不要猜密码、不要对提交接口做压测。
>
> **交付形式（推荐 HAR）**：浏览器 DevTools → Network → 完成一次目标操作 →
> 右键 → **Save all as HAR with content**；或用 mitmproxy 抓 App（该 App 未做证书固定、
> 信任用户 CA，见 `pinco.md §0`）。
> **脱敏要求**：可以打码姓名/学号/正文以外的个人内容；**但请保留 URL、
> 请求头名、请求体字段名与值形态、响应体结构**（这几项正是要确认的东西）。

| # | 优先级 | 需要什么 | 用来关闭什么问题 |
| --- | --- | --- | --- |
| **C1** | **P0** | **一次作业提交**：完整 URL（含 `actionName` 或 REST 路径）、全部请求头、**请求体原文**、**响应原文** | 提交请求体结构、必填字段、成功响应形状 → 可以打开 `submit_payload_confirmed` 闸门 |
| **C2** | **P0** | 同一次提交**前后各一次**作业详情（`…/detail`）的响应 | 幂等与状态位变化（`submitted/canRedo/corrected`） |
| **C3** | **P0** | 对**已提交**作业再点一次提交的请求 + 响应 | 重复提交是否被拒（幂等语义） |
| **C4** | P1 | 一次**逐题保存答案**（含一道带附件的题） | 复核 §4.2 的结构 + **附件元素键名** |
| **C5** | P1 | 一次**视频播放**期间的 `videoDurationReport` 请求（最好连抓 3-5 次，标注时间） | 上报频率、字段（时长/位置）、累计语义 |
| **C6** | P1 | 一次材料学习（打开→看完）前后的材料列表响应 | `learningRatio` 变化与哪个接口对应 |
| **C7** | P2 | `courseware/student/open`、`maxLook`、`studyReport` 各一次请求/响应 | 课件类上报的真实字段 |
| **C8** | P2 | 一次失败提交（如已过期作业）的响应 | 错误码枚举（含 `6569/10419/6582` 的语义） |

**明确不需要 / 不做**：反编译脱壳、改包重打包、爆破验证码、批量账号、
对提交接口压测、绕过教师设置的限制（红线见 `pinco.md §8`）。

---

## 8. 实现者清单

### 8.1 现在就能做的事（不涉及真写请求）

1. **端点常量入库**：把 §4.1、§4.2、§5.2 的 action 名与 REST 路径按
   `endpoints.py` 的 `EVIDENCE[...] = Evidence.STATIC` 纪律登记（单一出处）。
   注意 **App 动作名带 `Api` 中缀**、**版本号与路径版本不一致**这两点。
2. **REST 映射表**：本轮已经把学生端 App 的 `action → {uri, method}` 全表读出来了
   （402 条）。可以把**写操作那几条**填进 `REST_ACTION_PATHS`，但**仍标 STATIC**，
   并且只用在与 Web 动作一一对应、路径形态一致的条目上（不要整表灌入）。
3. **闸门保持不变**：`submit_payload_confirmed=False` 与 `DANGEROUS_ACTIONS`
   必须继续挡住写请求；C1 到位后再改为按样本构造。
4. **判分读取**：提交后的分数走 `…/questions` 的 `answer.score/review`，不要从提交响应取。
5. **刷课**：机制已确认并端到端跑通（§5.3）：`open` 取页数 + 逐页 `maxLook`。
   实现默认 dry-run，`--live` 才写；带页数/总时长上限与「不涨就停」熔断。

### 8.2 缺什么、优先级

| 优先级 | 缺什么 | 依赖 |
| --- | --- | --- |
| **P0** | 提交请求体 + 响应形状 | 用户提供 **C1** |
| **P0** | 重复提交语义（幂等） | 用户提供 **C2/C3** |
| P0 | 逐题保存答案的附件结构 | 用户提供 **C4** |
| — | ~~进度上报语义~~ 已完成：刷课 = 课件翻页（`maxLook`），见 §5.3 | 实测确认 |
| P1 | 错误码语义（6569/10419/6582） | 用户提供 **C8** |
| P2 | 浏览器内发请求的落地形态（BiDi / Playwright） | 工程决策；现有实测路径是 `scripts/bidi_ws.py` |
| — | **不做**：教师端任何写入（范围裁决只做学生端） | 决策 |

### 8.3 与既有代码的冲突点

1. `adapter._submit_payload()`（`adapter.py:700` 起）**不是**已确认结构；C1 到位前必须保持拦截。
2. `adapter._answer_content()` 现在把选择题答案按「1 起的序号拼接」——
   **没有任何证据**支持这个编码；C1/C4 到位后必须重写。
3. `endpoints.REST_ACTION_PATHS` 故意留空是对的（猜映射会静默打到别的接口）；
   本轮新证据建议**只补写操作相关的、路径形态无歧义的条目**。
4. ~~`DANGEROUS_ACTIONS` 只有 3 条提交类动作~~ 已处理：进度上报
   `PostV1CourseByCourseIdMaterialStudentLearningDurationReport` 已纳入
   `DANGEROUS_ACTIONS`，任何调用都必须显式 `dangerous=True`。

---

## 附录 A：来源清单

**本轮匿名请求（2026-10-19）**

- `https://s0.seewo.com/cloud-static/pinco/flutter/static/1762860981918/flutter/build/web/main.dart.js`
  （2,785,086 B，Last-Modified 2025-11-11）—— App 路由表 + 逐题保存请求体构造

**同族探测（均 404，记录以免重复）**

- `…/flutter/build/web/main-0.js` … `main-5.js`（166 B 的 Not Found）
- `…/flutter/build/web/flutter_bootstrap.js`、`main-0.part.js`、`main.dart.part.js`
- `https://s0.seewo.com/cloud-static/pinco/flutter/static/1762860981918/main-0.js`

**仓库内既有证据**

- `data/recon/pinco/raw/s0.seewo.com_...workbench_ae701e4755dac93f5621.js`（1,320,406 B）
- `data/recon/pinco/raw/apk_x/`（`pinco.md §5.1/5.2/5.4` 的 APK 静态事实）
- `docs/research/pinco.md`：§0（结论）、§3.4/4.2（协议与头）、§5.2/5.3（REST 与交叉验证）、
  §6（鉴权汇总）、§7（反自动化）、§8（路线对比）、§10（拿不到的部分）、**附录 L（Lead 实测）**
- `src/kedaibiao/platforms/pinco/endpoints.py`：`EVIDENCE` 分级、`DANGEROUS_ACTIONS`、
  `HEADER_SERVER`/`SERVER_NAME`、字段清单（`endpoints.py:215-227,240-265,407-425`）
- `src/kedaibiao/platforms/pinco/adapter.py:610-740`（三道守卫、`submit_payload_confirmed`）
- `src/kedaibiao/platforms/pinco/guard.py`（提交拒绝策略）

---

## 附录 B：本文件事实条目计数（用于「已确认 / 待抓包」比例）

| 层 | 已确认 | 待抓包 |
| --- | --- | --- |
| 协议与端点（Web 动作名 + App REST 路径） | **10 条**【静态】：提交 2 条路线、逐题保存 2 条路线、进度上报候选 6 条；另读出 App 路由表 402 条 | — |
| 前置条件（会话 / CSRF / 必需头 / 发起上下文） | **7 条**【实测】 | — |
| 请求体 | **1 条**【静态-App】（逐题保存：`studentHomeworkId/questionId/content`） | **提交请求体（C1）** |
| 频率 / 幂等 / 累计语义 | **0 / 3** | C2、C3、C5、C6 |
| 合计 | **18 条**（实测 7 + 静态 11） | **8 项**（C1-C8，其中 **P0 三项**） |
| 比例 | — | **已确认 18 : 待抓包 8 ≈ 69% : 31%**（**报文层**几乎全空：提交体 0、频率 0、幂等 0） |

**实测条目明细（便于复核）**：§2.2 六条（两个家族的地址与成功判据、信封、HTTP/内层码不一致、必须 POST、业务码）、
§3 七条（`x-token` cookie、`csrfToken` + `x-csrf-token`、`x-server: main` 必需、`x-token` 头误用会失败、
纯 HTTP `403 invalid csrf token`、`?auth=` 注入、写前必读的状态位）、§4.4 两条（读接口逐题判分、`solution`）、
§4.5 一条（提交前复查的保守做法）、§5.1 三条（`learningRatio`、材料两接口、`learnInfo.learningProgress`）。

> 一句话给实现者：**端点、头、CSRF 要求已经能写了；请求体一个字都还不能定** ——
> 最需要的是 §7 的 **C1（一次提交的请求体 + 响应体）**，它同时解锁提交与闸门两侧。
