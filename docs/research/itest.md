# iTEST 智能测评云平台（外研社 Unipus）—— 考试派发形式调研

- 调研对象：<https://itestcloud.unipus.cn>（iTEST 智能测评云平台，学生/教师云端）、
  <https://itest.unipus.cn/itest/>（iTEST 4.0 网页答题客户端）、iTEST 移动端 App、
  校内端（学校局域网自建）
- 调研日期：2026-10-19（Asia/Shanghai）
- 调研阶段：**Stage 0 公开资料 + Stage 1 登录态实测**。已用账号本人授权的凭据跑通
  「登录 → 考试列表 → 去考试换 token → 答题页 → 设备检测 → 取卷(load)」；
  取卷时**真实进入了一次该单元作业**（浏览器里点「开始考试」），**未作答、未保存、未交卷**
- 调研方式：官方帮助中心与 App 介绍页阅读 + 公开静态 JS 阅读 + 登录态低频只读请求
  （单发、无并发）；一次换 token 调用，其余均为读取
- 账号内数据：只读取了本人考试列表、考试元信息与一份答题页 HTML；报告只留字段名与
  结构，不留姓名/学号/手机号/token

> 外部内容一律按「不可信数据」处理，只读取不执行；凡未经直接验证的结论标注
> **未验证**，由事实推出的标注 **推断**。本报告只记录事实（入口、字段名、流程），
> 不复制第三方代码或页面表达。

---

## 0. 结论速览（TL;DR）

| 问题 | 结论 | 置信度 |
| --- | --- | --- |
| 平台归属 | 外研社 Unipus 旗下 **iTEST 智能测评云平台**（`iTEST 大学外语测试与训练系统`） | 已核验（页面标题/官方帮助） |
| 账号体系 | **学号不是登录名**：先用 **Unipus 账号**（手机/邮箱）登录，再做**学校身份认证**，认证时才用学校发的 **iTEST 账号 + 认证码** | 官方资料 |
| 考试「派发单位」 | **班级**。学生凭**班级授权码**加入班级；教师在该班级里发布**班级测试** | 官方资料 |
| 教师怎么发 | 班级空间 → 班级测试 → **新建测试** → 填考试信息 + 选试卷 → 提交 | 官方资料 |
| 派发到哪 | 网页端 / **移动端**（发布时勾选「支持移动端」，且试卷本身支持移动端）；另有**校内端**（管理员从云平台同步到局域网） | 官方资料 |
| 学生怎么收到 | 班级测试进入学生考试列表；移动端**只显示发布到移动端的班级测试** | 官方资料 |
| 考试过程形态 | 实时保存答题记录；断线/关浏览器后可「继续答题」，**剩余时长延续**；网页端**听力必须按顺序先听**，否则只能看到首页部分小题 | 官方资料 |
| 考试窗口 | **独立弹窗 + 一次性 JWT**：`/itest-api/itest/s/answer/index?token=<JWT>&returnUrl=…`，不是新域名 | 已核验（公共存档/公开 JS） |
| 答题端接口 | `/itest-api/itest/s/answer/{ksInfo,load,submit,exam/wait,screen/switch}`、`/itest-api/itest/s/env/*` | 已核验（公开 JS + 匿名 200） |
| 登录链路 | SSO `POST /sso/0.1/sso/cip/login`（AES 表单）→ `?ticket=` → cookie `jwt`/`iTSsiD` | **已核验（实测）** |
| 考试列表 | `POST /utest/itest-mobile-api/student/exam/list`（JSON `{}`） | **已核验（实测）** |
| 换 token | `POST /utest/itest/s/exam/judgeEntry`（表单 `examId`+`examCode`）→ `data.url` 里带 JWT | **已核验（实测）** |
| 换 token 会开始考试吗 | **不会**：调用前后 `ansBeginTime` 仍为 null、`finish` 仍为 0 | **已核验（实测）** |
| 答题页 | `url + "&returnUrl=" + 列表页`，`$GLOBAL` 里带 sppid/dataType=8/dataUser/openId | **已核验（实测）** |
| 能读到什么 | `ksInfo`（考试名/时长/起止）、`exam/wait`（waitingTime/isExpire） | **已核验（实测）** |
| 取卷 `load` | 浏览器里已通：返回 `D_HTML`/`C_HTML`/`UIK`/`TL`，题号+题干+选项全在里面 | **已核验（浏览器）** |
| load 的门槛 | 需带 `envTest*`、`iTMExam` 两个不透明 cookie（设备检测 + 作答会话） | **已核验（实测）** |
| 交卷报文 | `POST /itest/s/answer/submit`：`ansData`（`{al,sl}`）+ `act=save`（cache=自动保存） | 已核验（JS 常量，未真发） |
| 监考参数 | 全在 token 里：`screenLimit`（切屏次数）、`capture`、`rtcRecord`、`faceRecognize`、`minSubmitLimit`、`ksLimit` | 已核验（JWT 字段） |
| 自动化抓手 | **token 是钥匙**：拿到 token 不必开窗口，直接走 JSON 接口；交卷是写操作 | 推断（ksInfo/wait 已验证） |
| 非派发的练习形态 | **诊后训练 / 班级训练 / 模考训练**（学生自主发起，不属于教师派发） | 官方资料 |
| 匿名能拿到什么 | 只有入口、登录流程、静态资源版本与少量路径线索；**业务数据一律拿不到** | 已核验 |
| 登录拦截 | Unipus SSO（`sso.unipus.cn`），用户名/密码 AES 加密后提交；失败若干次后出图形验证码 | 已核验（登录页 JS）+ 实测 |

**一句话**：iTEST 的「派发」不是发一个考试链接，而是**围绕班级组织**——教师把试卷包装成
「班级测试」发布到班级，学生先加入班级（授权码）才能在考试列表里看到；网页端与移动端是
两条可见性不同的通道，听力与断线续答是答题端的硬约束。

---

## 1. 入口与账号体系

### 1.1 入口（已核验）

| 入口 | 地址 | 形态 |
| --- | --- | --- |
| 云平台（学生/教师 Web） | `https://itestcloud.unipus.cn/` | CMS 首页；「登录」走 `javascript:sign_url()` → `/utest/itest` |
| 云平台登录 | `https://itestcloud.unipus.cn/utest/itest/login` | **302** → `https://sso.unipus.cn/sso/login?service=…`（Unipus SSO） |
| iTEST 4.0 网页客户端 | `https://itest.unipus.cn/itest/itest/login` | 自有账号密码表单（`POST /itest/itest/login`），登录成功 → `/itest/index` |
| 移动端 App | `https://itestcloud.unipus.cn/app.html` | iPhone / Android / HarmonyOS NEXT 三端 |
| 校内端 | 学校内网地址（各校不同） | 局域网离线考试，管理员连公网同步考试/基础数据 |
| 管理员入口 | `https://itestcloud.unipus.cn/itest/` | 平台管理后台（另有校内端管理员后台） |

> 云平台与 iTEST 4.0 是**两套登录面**：前者 Unipus SSO，后者是 iTEST 自己的账号密码表单
> （页面标题「iTEST4.0登录界面」）。两者账号是否同一套，官方帮助没有明说；**未验证**。

### 1.2 账号与认证（官方帮助中心）

1. 注册/登录 **Unipus 账号**（手机号或邮箱）；
2. **选择学校，完成身份认证**——此时输入的才是学校发的 **iTEST 账号 + iTEST 密码/认证码**；
3. 认证通过后进入云平台；
4. 学生凭**班级授权码**加入班级（Web：班级空间 → 我的班级 → 加入班级）。

补充事实（官方帮助）：

- 认证提示「iTEST 账号已被认证」= 该 iTEST 账号被别的 Unipus 账号绑走，需学校管理员解绑；
- 学生**不能自主退出班级**，只能由教师移除；
- 教师/学生登录用的是自己注册的 **Unipus 账号**，学校管理员只创建/重置 iTEST 账号，
  重置后学生用该账号去认证，而不是拿它当 Unipus 登录名。

**对自动化的含义（推断）**：登录链路要先过 Unipus SSO，再用学校 iTEST 账号做一次绑定认证；
若账号已认证过，Cookie/令牌续期即可复用，不需要每次重新认证。

### 1.3 云平台登录链路（2026-10-19 实测，已核验）

```http
GET  https://itestcloud.unipus.cn/utest/itest/login
     → 302 https://sso.unipus.cn/sso/login?service=<itestcloud 登录地址>
POST https://sso.unipus.cn/sso/0.1/sso/cip/login          # JSON
     {"service": <service>, "username": <AES>, "password": <AES>}
     → {"code": 0, "rs": {"serviceTicket": "<ticket>"}}
GET  <service>?ticket=<ticket>
     → 302 回 /utest/itest/...，落 cookie：jwt、iTSsiD、SSOExpireTime、TGC
```

- AES 参数与 U 校园同源：key `8AD70B641C024C7ADA2ECD082EC0334F`、IV
  `0102030405060708090A0B0C0D0E0F10`、CBC+PKCS7、**大写十六进制**——
  `platforms/unipus/crypto.py` 的 `encrypt_login_field()` 可直接复用。
- 登录后云平台的学生页（`/utest/itest/s/exam`）直接 200，不再跳 SSO。
- 首次登录**没有**出图形验证码；错误次数累积后才可能要求（本次未触发）。

---

## 2. 考试派发机制（官方帮助中心口径）

### 2.1 教师侧：把试卷发布成「班级测试」

官方步骤（教师帮助）：

1. 进入**班级空间**；
2. 找到**班级测试**，点击**新建测试**；
3. 填写**考试信息**（开放时间/时长等，页面字段以实际为准）、**选择试卷**；
4. 点**提交**发布。

发布时还可选择：

- **是否支持/发布到移动端**——勾选后学生才能在 App 里看到并作答；
- 若试卷含移动端不支持的题型，则**不能**发布到移动端（系统会限制选择）。

### 2.2 学生侧：三件事决定「能不能看到考试」

1. **在班级里**（授权码加入，或被教师直接拉入）；
2. 教师把班级测试发布到了**你用的那个端**（网页端 / 移动端）；
3. 考试在开放时间内（考试列表按时间展示，具体规则**未验证**）。

移动端的额外限制（官方原文要点）：

- **只有发布到移动端的班级测试才能在移动端作答**；
- 收不到时先确认自己在班级和考生名单里。

### 2.3 校内端（局域网考试）——另一种派发通道

- 校内端**只能在校园网环境**使用；
- 管理员在云平台**开启校内端考试**后，校内端**连接公网同步**，考试才会出现在校内端列表；
- 校内端**只覆盖考试阶段**；考试结束后要回云平台（`itestcloud.unipus.cn`）阅卷、归档；
- 数据同步完成后可断开公网，在局域网内考试。

### 2.4 非派发的练习形态

iTEST 移动端/云平台还有学生自主发起的训练：

- **诊后训练**（成绩报告后按诊断推荐）；
- **班级训练**；
- **模考训练**。

这些不经过教师「发布考试」动作，属于题库练习，与「班级测试」是两个任务族
（**推断**：接口前缀/权限可能不同，待登录后核对）。

### 2.5 考试过程约束（影响自动作答链路）

官方帮助明确写出的行为：

| 约束 | 细节 |
| --- | --- |
| 断线续答 | 系统实时保存答题记录；重新登录后点「继续答题」；**剩余答题时间 = 异常发生时的剩余时长** |
| 关浏览器/关 App | 同上，重进可继续 |
| 听力顺序解锁 | 网页端考试**必须按顺序先听听力**；不听听力只能回答首页的部分小题，其余题目**看不到也无法作答** |
| 听力重置 | 教师可「重置听力」并顺带**加时**；学生需先关闭所有考试界面再重进 |
| 成绩报告 | 考后师生可看成绩报告：正误、解析、排名 |
| 阅卷 | 校内端考完回云平台阅卷、归档 |

**对自动化的含义（推断）**：答题不是「一次拉全部题目」——听力类试卷存在**按进度解锁**，
题目内容可能随听力进度分页下发；提交/保存大概率是**逐题或分段**保存，而不是只在交卷时
一次性提交（官方说「实时保存」）。

---

## 3. 考试窗口与答题端接口（公开面，已核验）

> 本节证据来自两类**公开**资源：
> (a) Wayback Machine 收录的真实考试入口 URL（登录后的考试窗口 URL 会随浏览记录进入公共存档）；
> (b) 答题端公开静态 JS（`https://itestres.unipus.cn/itest-mobile/static/public/common.js`，匿名 HTTP 200 可直接下载）。
> **未使用任何账号、未提交任何考试请求**；个人信息字段一律只记字段名，不记值。

### 3.1 形态：考试确实是一个「独立弹窗 + 一次性令牌」的应用

点击「去考试」后，列表页弹出独立窗口，地址形如（真实存档样本，token 已省略）：

```
https://itestcloud.unipus.cn/itest-api/itest/s/answer/index
    ?token=<JWT>
    &returnUrl=https://itestcloud.unipus.cn/utest/itest/s/exam
    &skipEnvTest=true
```

已核验的事实：

- 弹窗不是新域名，而是**同一域下的独立 H5 答题端**：页面在 `/itest-api/` 前缀下，
  静态资源在 `itestres.unipus.cn/itest-mobile/...`。
- 它是**令牌鉴权**：考试参数全部编码在 URL 的 JWT 里，不依赖列表页的 cookie 会话
  （列表页会话用于「去考试」时换 token，答题端只认 token）。
- 答题端会主动切断与列表页的联系：`window.opener=null; window.open("","_self")`；
  交卷后还会尝试 `window.close()`。
- `skipEnvTest=true` 只出现在部分样本里，服务端语义**未验证**（可能是"设备检测已做过"的标记）。
- 弹窗是公开 JS 里写死的：窗口名 `student_answer_window`，尺寸 `screen.availWidth × (availHeight-60)`，
  `toolbar=no,menubar=no,scrollbars=yes,resizable=no,location=no,status=no` —— 看起来像
  "专用考试窗口"，且**固定窗口名**意味着重复点「去考试」会复用同一个窗口。
- 同一份公开 JS 里，答题端还挂了一组 hash 路由：`/itest/s/answer/index`、
  `/itest/s/env/{checkKeyBord,checkHeadset,checkRecording,checkRtc,waiting}` —— 设备检测
  与答题页是同一个 SPA、同一个 token。

**结论**：窗口本身不是障碍——**token 才是接口的钥匙**。拿到 token，就可以不开窗口、
直接访问答题端的 JSON 接口。

### 3.2 token（JWT）里的考试参数

HS256 JWT。payload 字段（按字母序；含义为**推断**，括号里是样本里见过的取值）：

| 字段 | 含义（推断） |
| --- | --- |
| `detailId` / `extSourceId` | 试卷 / 考试 ID |
| `ksName` / `gradeName` / `course` | 考试名 / 年级 / 课程 |
| `clsId` / `clsesIds` / `clsNames` | 班级 |
| `schoolId` / `schoolCode` / `facultyId` / `facultyName` | 学校 / 院系 |
| `userName` / `realName` / `openId` / `extUserId` | 考生标识 |
| `restrictBeginTime` / `restrictEndTime` | 考试开放窗口 |
| `exp` | token 过期时间 |
| `ksLimit` | 考试时长（样本 90 / 120 / 150 / 999 / -1，分钟） |
| `minSubmitLimit` | 最短作答时间（样本 0 / 30，分钟） |
| `terminal` | 终端类型（样本 1 / 7） |
| `enter` | 是否允许进入 |
| `screenFlag` / `screenLimit` | 切屏监测开关 / 切屏次数上限（样本 5 / 10） |
| `capture` / `photo` | 摄像头抓拍 |
| `rtcRecord` / `rtcFlag` / `videoSpeak` | 音视频监考 / 录制 |
| `faceRecognize` / `faceRecognizeTime` | 人脸识别 |
| `accid` / `accidToken` / `extSourceType` / `isThirdPlatform` | 来源系统与第三方平台信息 |

**关键含义**：监考强度（切屏上限、摄像头、人脸、录屏录播）是**教师发布考试时就配置好的**，
不是答题端临时决定的。「派发形式」不只是发个考试，还包含这套监考参数。

### 3.3 答题端接口清单（公开 JS 里逐个核验）

答题端 base = `window.CTX` = `https://itestcloud.unipus.cn/itest-api`；请求头带
`sign-token: <token>`（只有 `screen/switch` 的调用点明确带了这个头，**是否全局拦截器统一加头未验证**）。

| 类别 | 方法与路径（相对 `/itest-api`） | 说明 |
| --- | --- | --- |
| 考试信息 | `GET /itest/s/answer/ksInfo?token=<JWT>` | 考试/考生/设置；缺 token 时返回 `{"code":2,"msg":"缺少请求参数"}`（已实测） |
| 试卷资源 | `GET /api/mobile/paper/paper/resource/info` | 试卷资源信息 |
| 载入试卷 | `POST /itest/s/answer/load` | 拉题目与已答记录；无参数时返回 `{"code":400}`（已实测） |
| 保存 / 交卷 | `POST /itest/s/answer/submit` | `ansData` + `act`，见 §3.4 |
| 候考 | `POST /itest/s/answer/exam/wait` | `{token}` → `waitingTime` / `isExpire`，倒计时结束才进考试 |
| 切屏上报 | `POST /itest/s/answer/screen/switch` | 头 `sign-token`；返回 `switchCount` / `forceSubmit` |
| 设备检测 | `GET /itest/s/env/checkHeadset`、`checkKeyBord`、`checkCamera`、`checkRecording`、`checkRtc`、`waiting` | 独立 HTML 检测页；**匿名可访问**（已实测 200） |
| 口语 / 上传 | `POST /itest/s/answer/submit-speech`、`GET /itest/s/answer/speech`、`GET /itest/s/answer/getUploadToken`、`POST /itest/s/answer/upload` | 录音上传（`upload.qiniup.com`） |
| 双人对话 | `POST /itest/s/answer/pairwork/match/start`、`match/query`、`chatresult/save` | 双人对话题型 |
| 监考通知 | `GET /itest/s/answer/unReadNotice`、`POST /itest/s/answer/submit-read-notice` | 老师发通知 / 已读回执 |
| 视频监考 | `POST /itest/s/answer/rtc` | RTC 房间 |
| 移动端 token | `POST {itestStudentUrl}/itest-mobile-api/student/exam/examToken` | 手机端换 token（`itestStudentUrl` 来自页面全局 `$GLOBAL`） |

列表页 / 班级页路由（Wayback 公共存档里出现过的登录后 URL，均 302 回登录页）：

```
/utest/itest/s/exam                 # 考试总列表
/utest/itest/s/exam/clazz           # 班级测试
/utest/itest/s/exam/school          # 学校考试
/utest/itest/s/competition          # 比赛
/utest/itest/s/space/clazz          # 班级空间
/utest/itest/s/space/clsMainIndex?clsId=<id>
/utest/itest/s/personInfo/studentInfo?pId=<id>
/utest/itest/biz/exam/papershow/showPaper?clsEpId=<id>   # 试卷预览
/utest/itest-mobile-api/student/v1/diagnosis/after/to-plan  # 诊后训练
```

### 3.4 保存 / 交卷报文的形状（公开 JS 里的构造逻辑）

`POST /itest/s/answer/submit`（表单编码，`Object(r.e)` 请求器），字段：

```
ansData  = JSON.stringify(answerMap)   # 全部作答
act      = "cache" | "save"            # cache = 自动保存（节流）; save = 交卷
exitFlag = act == "save" ? 1 : ""
clearUik = act == "save"
sppid / __t / uik                      # 试卷/请求标识（__t 是随机数）
dataid / extSourceType / extSource / extUser / openId
```

- `act` 枚举在公开 JS 里就是 `autoSave="cache"`、`submit="save"` 两个值；
- 单题作答对象形如 `{q: <题号>, d: [...], o: [...], rnp: "<播放次数>"}`（听力题的
  `rnp` 记录播放次数）；
- 考试类型枚举（移动端答题端）：`preview=0, mock=5, class=6, school=8`；旧 4.0 客户端
  另有一套 `0=学校考试 / 1=班级考试 / 2=模考` 的编号 —— **两代编号不同，不能混用**。

### 3.5 防作弊是「服务端记账 + 前端锁焦点」

| 机制 | 细节 |
| --- | --- |
| 切屏计数 | 页面失焦/可见性变化时调 `screen/switch`，**次数记在服务端**；达到 `screenLimit` 后服务端返回 `forceSubmit=true`，前端自动交卷 |
| 焦点锁 | 答题页 `Object.defineProperty(window,"onblur"/"onfocus",{writable:false})` 锁死事件，并提示「离开考试页面，老师将收到异常报告」 |
| 设备检测 | 耳机 / 键盘 / 摄像头 / 录音 / RTC 逐项检测（`/itest/s/env/*`） |
| 视频监考 | 除电脑摄像头外，支持**手机扫码当第二机位**：手机放侧后方 1.2–1.5 米，拍摄作答屏幕、上半身与手部 |
| 人脸识别 | token 里 `faceRecognize` 开启时，答题端会调人脸校验回调 |
| 早交卷限制 | `minSubmitLimit` 分钟内不允许交卷 |
| 自动收卷 | 倒计时（`ksLimit`）结束自动提交；切屏超限也自动提交 |

### 3.6 对自动化的含义（两条路线）

- **浏览器路线**：Playwright 用 `context.on("page")` / `page.expect_popup()` 捕获弹窗 Page，
  直接驱动独立窗口；需要 `--disable-popup-blocking`，有头模式更接近真实；注意自动化抢焦点/
  最小化会被记成切屏。
- **协议路线（推荐，先只读）**：登录 → 拿到 token → `ksInfo` / `load` 拉题与已答；
  `act=cache` 可做自动保存草稿，`act=save` 交卷必须人工确认。token 是 HS256 签名的，
  密钥不在公开资源里，**不能伪造，只能从「去考试」动作里拿**。
- **风险**：教师开了 `capture` / `rtcRecord` / `videoSpeak` / `faceRecognize` 时，无人值守
  自动作答会被监考端看到；`minSubmitLimit` 会拒绝过早交卷；切屏超限会被自动交卷。

### 3.7 实测：进入考试的完整链路（2026-10-19，已核验）

用账号本人授权的凭据实测，全程**只读到「拿到答题页并确认 token 可用」，未作答、未交卷**。
顺序如下（每一步都是真实请求，不是推断）：

```
① 登录（Unipus SSO，见 §1.3）
   GET  https://itestcloud.unipus.cn/utest/itest/login
        → 302 https://sso.unipus.cn/sso/login?service=<itestcloud 登录地址>
   POST https://sso.unipus.cn/sso/0.1/sso/cip/login   (JSON)
        {"service": <上面的 service>, "username": <AES 加密>, "password": <AES 加密>}
        → {"code": 0, "rs": {"serviceTicket": "..."}}
   GET  <service>?ticket=<serviceTicket>
        → 落 cookie：jwt / iTSsiD / SSOExpireTime（itestcloud + .unipus.cn）

② 考试列表
   POST https://itestcloud.unipus.cn/utest/itest-mobile-api/student/exam/list   (JSON {})
        → {"code":0,"rs":{"data":[{"ksdId":<考试详情ID>,"ksId":<试卷/考试ID>,"ksName":...,
           "examType":0,"examTag":2,"beingOrOver":2,"finish":0,"ksLimit":999,
           "restrictBeginTime":...,"restrictEndTime":...,
           "captureFlag":0,"faceRecognize":0,"rtcFlag":0,"screenMonitoringFlag":0}]}}

③ 点「去考试」→ 换 token（就是列表页那个 XHR）
   POST https://itestcloud.unipus.cn/utest/itest/s/exam/judgeEntry   (表单)
        examId=<ksdId>&examCode=<比赛码，普通考试为空>
        → {"code":1,"data":{"ksId":"...","openId":"...","isNeiWangKs":false,
           "xiaoNeiAddr":"","url":"https://itestcloud.unipus.cn/itest-api/itest/s/answer/index?token=<JWT>"}}
   * 本次实测的考试 `item.type == 1`，走的是 `/itest/s/exam/judgeEntry`（**已核验**）；
     打包代码里 `item.type == 0` 走 `/itest/s/clsanswer/judgeEntry`（`type` 的语义未定，
     可能是考试通道而非 examType，待补测）。

④ 弹窗
   window.open(url + "&returnUrl=" + encodeURIComponent(当前页), "student_answer_window", ...)
   * 缺 returnUrl 时答题页直接报「没有权限 / 参数缺失」——已实测，参数是必需的。

⑤ 答题页（服务端渲染的壳，再加载 SPA）
   GET <url>&returnUrl=...  → HTML 注入 window.$GLOBAL：
       { examName, username(学号), realName, schoolName, facultyName, gradeName, clsNames,
         hasRecord, capture, hasRecognize, itestStudentUrl, rtcFlag, rtcRecord,
         prepareBroadcast, kcId, sppid=<ksId>, dataSource:"itest", dataType:"8",
         dataUser=<uid>, openId, cdnUrl:"https://itestres.unipus.cn/itest-mobile/static" }
       window.CTX = "/itest-api"
   * 页面还挂 vendor.js / common.js / checkService.js（答题 SPA），并锁 F12/右键。

⑥ 读考试信息（JWT 可用）
   GET  /itest-api/itest/s/answer/ksInfo?token=<JWT>
        → {"code":0,"rs":{"ksName":...,"ksLimit":999,"beginTime":"2026-10-19 04:15:00",
           "endTime":"2026-11-16 23:55:00","kcName":""}}
   POST /itest-api/itest/s/answer/exam/wait   (表单 token=<JWT>)
        → {"code":0,"rs":{"waitingTime":-34076,"isExpire":0}}
          waitingTime 为负 = 已过开考时间、可进入；isExpire=1 才是窗口关闭

⑦ 取卷（2026-10-19 打通，浏览器环境下已核验）
   POST /itest-api/itest/s/answer/load?t=<rand>   (表单)
        dataid=<ksId>&examId=&dataType=8&dataSource=itest
        &dataUser=<uid>&openId=<openId>
   → {"code":1,"data":{
        "D_HTML":  "<div class='dtk'>…题号目录，104 个 <li qid=… sectionKey=…>…",
        "C_HTML":  "<div class='itest-section' sectionType='sNormal|sHear'>…11 个 section、
                    26 道大题、填空 <input class='blankinput' qSubIndex=…>…",
        "OPTION_ORDER": false, "IS_SHOW_QUESPARSE": false, "BACK_PLAY": true,
        "UIK": <作答版本号，每 load 一次 +1>, "TL": 59940, "NTL": 59940,
        "TYPE": 0 }}
   * 实测 TL/NTL = 59940 = 999 分钟（单位秒），与 ksLimit 对得上。
   * 题目结构：section（sectionType sNormal/sHear 区分听/读）→ itest-ques[qid] →
     题型容器 class="itest-danxuan|itest-write|…"（即答案采集函数名）；
     填空为 input.blankinput[qSubIndex][qSubSubIndex]，<u> 标出要填的原文词。
   * 交卷 = POST 到 #submitAnswerForm 的 action（即 /itest-api/itest/s/answer/submit），
     报文 {ansData: JSON.stringify({al:[…], sl:[…]}), act:'save'|'cache',
      sppid, uik, clearUik, dataType, dataSource, dataUser, openId, exitFlag, __t}。
     act='cache' 是每分钟自动保存，act='save' 才是交卷（写操作）。
```

**取卷为什么之前一直 400（已定位）**：`load` 不只认 token，还认「本设备已进入该考试」的
服务端会话，由下面两个不透明 cookie 承载：

| cookie | 由谁下发 | 作用 |
| --- | --- | --- |
| `envTest<32位大写>` | 设备检测（键盘/耳机）通过后 | 证明这台设备做过考中设备检测 |
| `iTMExam`（64 字节 hex） | `GET /answer/index?token=…` | 本机持有该考试的作答会话 |

真实浏览器里还有一步 `POST /itest-mobile-api/student/exam/examToken`（body `{}`）——
它把 `p_examtype` 从 `0` 改成 `8`，之后 `GET /answer/index` 才会下发 `iTMExam`；
若账号已在别处进入同一考试，页面会先弹「考试冲突」，点确定触发
`POST /itest-api/itest/s/answer/solveUikConflict`
（`sppid&dataType&dataSource&dataUser&openId&__r`）把作答会话「抢」到本设备。

> 实测：纯 HTTP（requests + 同一套 cookie）复刻到 `solveUikConflict` 为止仍然 `load → 400`；
> 说明 `envTest*`/`iTMExam` 这两个值不是 cookie jar 里能自然长出来的（浏览器端生成/校验）。
> 结论：**协议路线到取卷这一层需要浏览器兜一次底**，或者后续把这套设备检测的接口补齐。

**两条重要的实测结论：**

1. **`judgeEntry` 本身不开始考试。** 调用前后 `ansBeginTime` 始终为 `null`、
   `finish` 仍为 0 —— 真正开始答题是答题页里「确认信息 → 开始答题」那一步。
   所以「换 token」可以单独做，不会消耗/启动考试。
2. **token 可以直接用**：`ksInfo` 与 `exam/wait` 都接受 URL 上那个 JWT；
   `sign-token` 头在 `screen/switch`、上传/人脸接口里用的也是它。

**仍未知的两小块**：
1. `envTest*` / `iTMExam` 的生成规则（浏览器端还是边缘网关下发，值不可伪造）；
2. `submit` 的 `ansData` 精确结构——`getAllUserAnswer()` 按题型 class 分派采集函数
   （`itest-danxuan` → `itest_danxuan` …），要拿到每种题型的字段名，最省事的办法仍是
   浏览器里填一题、抓一次 `act=cache` 的报文。

### 3.8 附：iTEST 4.0 老客户端的登录线索（公开静态资源）

| 线索 | 值 | 来源 |
| --- | --- | --- |
| 登录提交 | `POST /itest/itest/login`，表单字段 `username`、`password`、`act=doLogin`、`itestlogin=1`、`_rp`、`csrf`、可选 `inputvcode` | iTEST 4.0 登录页 |
| 登录结果码 | `code == 8` → 跳转 `data.data`（默认 `/itest/index`）；`code == 9/10` → 提示错误并**显示图形验证码** | 登录页 JS |
| 验证码 | `GET /itest/comon/oo?username=<用户名>&t=<毫秒时间戳>` | 登录页 JS |
| 学生答题页（旧） | `/itest/s/answer`（匿名 404；`window.examInfo`、`#submitAnswerForm`） | 公共 JS 常量 |
| 考试类型（旧） | `0=学校考试 / 1=班级考试 / 2=模考`（`examInfo.examId == "-999"` 时为模考） | `itest_common.js` |
| 教师考试管理 | `/itest/a/kaoshi/kaoshiManagement/selectTeacherGridData` | 公共 JS 常量 |
| 静态资源版本 | `V4.14.1.20240625.GA` | 所有静态资源 `?v=` |
| 埋点 | `/itest/online/tongji/req` | 登录页 |

> 这些路径只说明「长什么样」；登录态下的字段与成功码一律待实测。

---

### 3.9 取卷脚本（`scripts/itest_exam.py`，已跑通）

把 §3.7 的链路固化成可复用脚本，**取到卷就停手**（不点「确定」、不 submit、不计时）：

```
python3 scripts/itest_exam.py list            # 登录 + 列考试（只读）
python3 scripts/itest_exam.py paper <ksdId>   # 浏览器进窗口 → 设备检测 → 开始考试 → 抓 load
```

产物（`data/recon/itest/`，已被 `.gitignore` 忽略）：

| 文件 | 内容 |
| --- | --- |
| `load_<ksdId>.json` | `load` 原始响应（D_HTML / C_HTML / UIK / TL …） |
| `paper_<ksdId>.json` | 解析后的试卷：section → question → 题型 / 题干 / 选项 / 空位 / 听力音频 |

实现要点：

- 登录与换 token 用 `urllib`（零第三方依赖），只把 cookie 灌进 Playwright context；
- 浏览器在**列表页点该考试卡片的「进入考试」**（直接打开窗口 URL 会触发「考试页面被拦截」）；
- 自动处理「考试冲突 → 确定」与键盘/耳机检测；看到「加载成功」立即停止；
- 解析按 `itest-section` → `itest-ques` → 题型容器切分，选项兼容两种来源
  （`<label><input type=radio>` 与 `option-head` 里的 `A) … B) …` 文本）。

---

### 3.10 控制台里的作答链路（已进仓库，可复用）

链路的可执行版本在 `src/kedaibiao/platforms/itest/exam.py`，控制台通过三个动作
使用它（注册在 `api/actions.py`）：

| 动作 | 做什么 | 不可逆？ |
| --- | --- | --- |
| `paper` | 进考场把试卷读出来：大题 → 题 → 槽位，并读回页面当前已填 | **会开始/继续计时**，但不改答案 |
| `fill` | 按答案单（JSON）填进作答页，然后**逐项读回核验** | 只改页面；不交卷 |
| `submit` | 填 → 核验（有不符就**拒绝交卷**）→ 点交卷 → 回查考试列表 `finish` | **是**，且必须把确认串写成「交卷」 |

实现要点（都是实测踩出来的）：

1. **不需要在浏览器里再登录一次**：适配器 HTTP 登录后，把 cookie jar 灌进 CDP
   浏览器（`Network.setCookie`）即可，8 条 cookie 就够。
2. **等 SPA 初始化 cookie 再换 token**：页面加载后 SPA 会种 `p_user` / `p_examtype` /
   `p_examid`；没种完就调 `judgeEntry` 会返回 `code=0「考试不存在或已被删除」`。
   `enter()` 先轮询这些 cookie，再换 token，并且对偶发失败刷新重试。
3. **`judgeEntry` 的 URL 必须补 `returnUrl`**，否则答题页报参数缺失。
4. **设备检测 / 考试冲突 / 开始考试都是浏览器交互**：按文字点「下一步」「检查正常」
   「确定/继续」「开始考试/继续作答」，再 `switch_to_target("/answer/")` 切到新标签页
   （iTEST 的作答页在新标签页打开，不切会看起来像「点了没反应」）。
5. **填表只碰界面**：`paper.py` 生成按 qid 的数据驱动 JS（填空设值 + 派发
   input/change、单选按 label 字母点、翻译设 textarea），然后同一份答案单读回核验。
   不自己拼 `ansData`，避开未验证的载荷格式。
6. **交卷后以服务端为准**：页面提示（交卷成功/得分…）只是证据之一，动作层会再查一次
   考试列表的 `finish`；两者不一致时以 `finish` 为准。
7. 截图落在 `data/itest/`（已被 `.gitignore` 排除），只留本地排查用。

**交卷实测（2026-10-19，B2U5）：** 作答页左侧的「交卷」是 `<a class="tijiao">`，
点它 → 确认弹层点「确定」→ 考试列表里 `finish` 从 0 变 1，即真的交上了。
注意**页面不一定出现「交卷成功」字样**（本次就没有），所以动作层以**服务端
`finish`** 为准，页面文字只作辅助证据；交卷后 `a.tijiao` 会消失，也可用作
「已交」的信号。

## 4. 待实测清单（需登录态，按优先级）

| # | 目标 | 要确认的东西 | 状态 |
| --- | --- | --- | --- |
| 1 | 登录态建立 | SSO `cip/login` + `?ticket=` → cookie `jwt`/`iTSsiD`；无图形验证码 | **已核验**（2026-10-19） |
| 2 | 考试列表 | `POST /utest/itest-mobile-api/student/exam/list`（JSON `{}`）→ 分页考试数组 | **已核验** |
| 3 | **去考试 → token 签发** | `POST /utest/itest/s/exam/judgeEntry`（表单 `examId`/`examCode`）→ `data.url` 内嵌 JWT | **已核验** |
| 4 | 答题页 | `url + "&returnUrl=" + 列表页` 打开；`$GLOBAL`（sppid/dataType=8/dataUser/openId）| **已核验** |
| 5 | 答题端读取 | `ksInfo`、`exam/wait`、`load`（`D_HTML` 题号 `/ C_HTML` 题干+选项 `/ UIK`） | **已核验（浏览器）** |
| 6 | 自动保存 | `submit` + `act=cache` 的响应；断线续答（`load` 返回已答） | 待测（写操作） |
| 7 | 交卷 | 点 `a.tijiao` → 确认 → 列表 `finish=1`；`act=save` 响应体未单独抓 | **已核验（浏览器，2026-10-19）** |
| 8 | 确认页/设备检测 | 信息确认 → 键盘/耳机检测 → `examToken` → `考试冲突` → `solveUikConflict` → `load`；下发 `envTest*`/`iTMExam` | **已核验（浏览器交互）** |
| 9 | 班级 | 班级列表、授权码加入、考生名单（只读） | 待测 |
| 10 | 训练族 | 诊后 / 班级 / 模考训练的列表与题目接口 | 待测（低优先） |
| 11 | 移动端标记 | 试卷/考试上的「支持移动端」字段在哪里体现 | 待测 |

---

## 5. 风险与纪律

- **考试是红线**：本项目先把只读链路跑通（列表 → 详情 → 题目），任何 `submit` 类写操作
  必须由人确认；考试作答代做涉及学术诚信，见 README 免责声明。
- **考试 token 按凭据处理**：JWT payload 含姓名/学号/手机号/班级，等价于登录态；
  不写进仓库、不进日志、不进聊天记录；`docs/` 只记字段名。
- **不伪造 token / 不绕过监考**：HS256 密钥不可得，伪造即攻击；切屏计数、摄像头、
  人脸、录屏是监考配置，**不做规避**（不模拟 `screen/switch` 洗次数、不屏蔽 RTC）。
- **不爆破密码**：本轮用现有账号试了一次 iTEST 4.0 登录（返回「用户名或密码错误，还可以
  尝试4次」）后立即停手，未再尝试；后续凭据由账号本人提供。
- **低频只读**：公开面探测全部单发、无并发；登录后仍按此纪律（≥1.2s 间隔）。
- **凭据不落盘**：账号/密码只进 `.env`（已被 `.gitignore` 忽略），报告与日志不出现明文。
- **本轮实际动作清单**（可审计）：SSO 登录 3 次、考试列表 2 次、`judgeEntry` 2 次
  （只为取 token，已确认不写 `ansBeginTime`）、答题页 GET 3 次、`ksInfo`/`exam/wait`
  各 2 次、`load` 若干次（均为 400，无副作用）、`/env/*` 匿名探测若干。**没有**调用
  `submit`、`screen/switch`、`upload`、`face/acquire`、`rtc`。
