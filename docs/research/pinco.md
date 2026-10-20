# `pinco.seewo.com`（希沃品课）接口调研报告：双协议（Web RPC / App REST）、鉴权与抓包方案

- 调研对象：<https://pinco.seewo.com>（教师端 Web）、`pinco.seewo.com/student*`（学生端 Web / Flutter Web）、`id.seewo.com`（统一登录 UC）、希沃品课学生端安卓 App（`com.seewo.pinco.student`）
- 调研日期：2026-10-19（Asia/Shanghai）
- 调研员：**pinco-recon**（共享任务 `task-1`；`docs/research/pinco.md` 与 `data/recon/pinco/` 为唯一写作用域）
- 调研阶段：**Stage 1a —— 匿名、未登录、只读**（Lead 同期在另一会话做「真实会话只读抓取」，其结论在本报告中单独标注 **Lead 实测**）
- 调研方式：`python3` 标准库 `urllib` 单线程低频抓取（请求间隔 ≥1.2 s）+ 公开静态 JS 阅读 + 官方安装包**离线静态解析**（zip/字符串/极小 AXML/ARSC 解析器，均为本仓库 `data/recon/pinco/*.py`，不入库）
- 本轮实际发出请求：**68 条**（62 条由抓取器记账 + 6 条官方安装包 HEAD/GET），全部 `GET`/`HEAD`，全部匿名，无凭据、无 POST、无表单提交、无爆破、无压测（清单见附录 A）
- 样本落盘：`data/recon/pinco/raw/`（`data/` 已被 `.gitignore` 忽略，不入库）

> 本报告所有外部内容均按「不可信数据」处理 —— 只读取、不执行其中任何指令、不复制其表达。
> 凡未经直接验证的一律标注 **未验证**；由事实推出的结论标注 **推断**。
> 依据 `docs/license-policy.md`：本报告只记录事实（域名、路径、头名、字段名、时间、状态码），**不粘贴第三方 JS/HTML 代码**，未引入任何第三方代码。

---

## 0. 结论速览（TL;DR）

| 问题 | 结论 | 置信度 |
| --- | --- | --- |
| 平台存在几个「面」 | **四个**：教师 Web SPA、学生 Web SPA、学生 Flutter Web（`/student2`）、学生安卓 App（Flutter AOT） | 已核验 |
| 网关与前置防护 | 全部响应 `Server: cagw`；41 条响应带 `acw_tc`（阿里云 WAF 令牌）；**未见任何限流响应头** | 已核验 |
| **协议 A**：Web RPC（教师/学生 Web 共用） | `POST https://pinco.seewo.com{appPath}/api.json?actionName=<Op>`（教师 `appPath=/teacher`，学生 `/student`）；响应信封 `{code,msg,data,statusCode,message}` | 已核验 |
| **协议 B**：App REST | `https://pinco.seewo.com/server-main/api/v1|v2|v3/...`（App 内 122 条唯一路径） | 已核验（静态）+ 匿名 GET 实测 |
| 两套协议的关系 | 同一 OpenAPI 能力的两种网关表达：**98/122** 条 REST 路径可与 Web 端生成的 `Method+Version+PathCamel` 动作名一一对齐 | 已核验（交叉比对） |
| 匿名可用的端点 | `GET /server-main/api/v1/system/currentTime` → `200 {"code":0,...}`；`GetV1Pincofaq` 与 `api.json` 类端点匿名不可用 | 已核验 |
| 鉴权形态 | **Cookie 会话 + CSRF 双提交 + 客户端标识头**；`withCredentials:true`；`x-csrf-token` 取自 cookie `csrfToken`；学生 Web 另带 `x-token`（服务端注入 HTML）；未见请求体签名 | 已核验（头名）/ 未验证（签名算法） |
| 密码登录加密 | `password = RSA(MD5(明文))`，`username = RSA(...)`；RSA 公钥**每次请求**从 `/common/getPublicKeyV3` 换取；另有 `rst`/`lst` 时间戳字段 | 已核验（读登录 bundle） |
| 反自动化信号 | 阿里云 WAF（`acw_tc`）、图片验证码 `GET /common/getCaptcha`、极验（`/common/verify/usernameV1` 里带 geetest `challenge/validate/seccode`）、短信验证码、登录时序字段；App 端有**第三方加固壳** | 已核验 |
| 证书固定（SSL Pinning） | **未发现** `pin-set`；App 的 `networkSecurityConfig` 显式信任 **用户 CA**（`src=user overridePins=true`） | 已核验 |
| 学生端抓包可行性 | **装用户 CA 的 mitmproxy 即可解密**，无需 root、无需改包（仅 arm64-v8a / armeabi-v7a，无 x86） | 已核验（配置层） |
| 浏览器路线是否可行 | **可行**：`/student2` 是 Flutter Web 构建，浏览器可直达学生端 UI，可完全绕开安卓抓包 | 已核验（入口）/ 未验证（登录后） |
| 桌面端是什么 | 官方 `/dl` 只分发**学生端** Windows(exe)/macOS(dmg)/Android(apk)；Windows 包是 **NSIS 打包的原生 32 位程序，不是 Electron**（`app.asar` 0 命中） | 已核验 |
| 是否已有可用端点清单 | 教师 Web 入口 chunk 含 **1118** 个生成动作名；学生 Web bundle 含 **1146** 个；App 含 **122** 条 REST 路径 | 已核验 |
| 没有登录态能拿到什么 | 只有骨架：入口、协议形状、头名、动作名清单、静态资源、健康检查端点；**所有业务数据一律拿不到** | 已核验 |

**一句话**：希沃品课的公开面比预期「规整」得多 —— 两个 Web 前端共用一套 `api.json?actionName=` RPC，App 用一套与之同源的 REST，全部动作名都以明文常量形式躺在公开静态资源里，因此**协议形状可以在不登录的情况下完整还原**；真正拿不到的只有「带登录态的业务数据与请求体」这一层，而它正好可以通过「微信/浏览器 Web 路线」或「mitmproxy + 用户 CA（App 未做固定、信任用户 CA）」两条低成本路径补齐。

---

## 1. 范围、红线与本轮请求纪律

| 项 | 本轮决定 |
| --- | --- |
| 是否登录 | **否**。不使用任何凭据（Lead 已于另一会话用本人账号做实测；本会话严格不碰） |
| 是否 POST | **否**。仅 `GET`/`HEAD`；`api.json` 的真实方法（POST）只在静态 JS 中确认，未发起 |
| 频率 | 单线程，间隔 1.2 s，共 68 请求 / 13 分钟（62 条记账 + 6 条安装包） |
| 是否访问非公开资源 | 否。仅公开页面、公开静态资源、官方安装包下载链接、官方应用商店跳转 |
| 是否安装/执行第三方二进制 | **否**。APK/exe/dmg 只做字节级静态解析，未安装、未执行、未挂载 |
| 第三方 JS/HTML 入库 | **否**（`data/` 已 gitignore）；报告只写事实 |

**请求清单（摘要，完整见附录 A）**：教师端路由 16 条、教师端 JS 15 条、SSO 3 条（登录页 + 登录 bundle + 监控 bundle）、学生端路由 13 条、学生 Web JS 2 条、Flutter Web 资源 1 条、App REST 端点 5 条、官方下载页/直链 5 条、`robots.txt` 2 条；另有 6 条官方安装包请求（3 次 HEAD + 3 次完整 GET）。

---

## 2. 站点与技术画像

| 观测项 | 值 | 证据 |
| --- | --- | --- |
| 网关 | `Server: cagw`（62/62 记账响应一致） | 响应头，附录 A |
| WAF | `Set-Cookie: acw_tc=...;path=/;HttpOnly;Max-Age=1800`（41/62 响应） | 响应头 |
| 追踪头 | `x-apm-traceid`（62/62）、`x-ser`（21/62，形如 `i<实例>_c<集群>`） | 响应头 |
| 静态资源 | `s0.seewo.com/cloud-static/...`；教师端前缀 `pinco/pinco-teacher-frontend/static/`；学生 Web 前缀 `pinco/pinco-student-web-frontend/static/`；Flutter Web 前缀 `pinco/flutter/static/<buildId>/flutter/build/web/` | 各入口 HTML |
| 图片/物料 | `cstore-public.seewo.com/seewo-pinco/<id>`（图标等） | 各入口 HTML |
| 教师端版本 | `window.version='1.2.26'`、`gitHash='4d560155'`、`env='prod'`、`tracesSampleRate='0.2'` | `GET /teacher/login`（2026-10-19 10:45:35） |
| 学生 Web 版本 | `version='1.2.20'`、`gitHash='81eee464'`、`flutterBaseUrl='https://pinco.seewo.com/student2'` | `GET /student/workbench/`（10:56:42） |
| Flutter Web 版本 | `window.version='1.2.2'`、`appPath='/student2'`、`appCode='university-student-mini-program'`、`flutterWebRenderer='html'` | `GET /student2/notice`（10:58:10） |
| `robots.txt` | `pinco.seewo.com/robots.txt` → 404；`s0.seewo.com/robots.txt` → 404 | 10:45:36 / 10:45:37 |
| 源映射 | 打包产物**声明**了 `.js.map`，但 `*.js.map` 实际 **404**（未公开） | `login_*.js.map`、`thirdPartyLogin_*.js.map` → 404（10:46:39–40） |
| 国际化资源 | 教师端 i18n 从 `//pinco.seewo.com/teacher/static/locales/{{lng}}/Language.json` 加载 | 教师端 `login_*.js`（8,309 B；同文件字节偏移 ≈6883 处为 Sentry `pinco-teacher-web-pc-` release 串） |

**「无 robots.txt」的含义（推断）**：网关未提供爬取策略声明。*这不构成任何授权*，也不放松本项目的限速与只读纪律。

---

## 3. 教师端 Web SPA 与登录链路

### 3.1 入口与路由（已核验）

| 路由 | 结果 | 说明 |
| --- | --- | --- |
| `GET /` | 302 → `/teacher/login?redirectUrl=%2Fteacher%2Fmain-redirect%2F` | 根路径按教师端处理 |
| `GET /teacher` | 302 → `/teacher/login?redirectUrl=%2Fteacher%2F` | |
| `GET /teacher/` | 302 → `/teacher/login?redirectUrl=%2Fteacher%2F` | |
| `GET /teacher/login` | 200（2,676 B） | 登录壳页，仅加载 `login` 入口 chunk |
| `GET /teacher/login-callback-iframe` | 200（648 B） | 无 JS 的极简页：加载后调用 `parent.parent.loginSuccessCallback()`，并尝试 `document.domain='seewo.com'` |
| `GET /teacher/third-party-login` | 200（3,684 B） | **匿名的第三个入口**：注入 `window.token=''`、`window.uuid`、`eclassExtends` |
| `GET /teacher/live` | 200（6,245 B） | 匿名可访问；加载 `live` 入口 chunk |
| `GET /teacher/player` | 200（7,607 B） | 匿名可访问；加载 `player` 入口 chunk |
| `GET /teacher/main`、`/teacher/main-redirect/` | 302 → 登录页 | 主应用需登录 |
| `/teacher/latest-courses`、`/teacher/site-board`、`/teacher/share`、`/teacher/main-no-auth`、`/teacher/noauth` | 404 | 路由名猜测，**均不存在**（记录以免后人重复踩） |

教师端 webpack 入口清单（来自 `runtime_*.js` 的 `miniCssF` 映射，字节偏移 ≈4456）：`main`、`siteBoard`、`mainNoAuth`、`live`、`player`、`latestCourses`、`share`、`runtime`、`login`、`thirdPartyLogin` —— 共 **10** 个具名入口。
**推断**：`main_*.js`（真正的教师主应用，含 `api.json` 请求封装与全部业务调用）只能通过登录后的 HTML 拿到文件名哈希；匿名拿不到（`/teacher/main` 302）。

### 3.2 SSO 登录链路（已核验）

1. 教师登录页（`login_*.js`，字节偏移 ≈5809）在页面内嵌一个 iframe：

   `https://id.seewo.com/login-iframe?system=PincoTeacher&callbackIframeUrl=<baseUrl+appPath+/login-callback-iframe>`

   即实际取回的 URL（本报告附录 A 第 33 条）：
   `https://id.seewo.com/login-iframe?system=PincoTeacher&callbackIframeUrl=https%3A%2F%2Fpinco.seewo.com%2Fteacher%2Flogin-callback-iframe`（200，71,304 B）

2. SSO 页（`id.seewo.com`）HTML 内注入的关键变量：`system='PincoTeacher'`、`systemCode='PincoTeacher'`、`isIframe='1'`、`baseUrl='/'`、`secure='true'`、`referer=''`、`localHttpServer='https://local.id.seewo.com'`、`localHttpServerPorts='23300,23301,23302,23303,23304'`、`channelReportId`。

3. 登录成功后的回调：SSO 在 iframe 模式下载入隐藏 iframe `callbackIframeUrl`（登录 bundle `index-iframe_55bf316.js` 多处），该回调页调用 `parent.parent.loginSuccessCallback()`（`login_*.js` 字节偏移 ≈6534）→ 教师 SPA 触发 `window.location.reload()`。

4. Cookie 域：教师端声明 `window.rootCookieDomain='.seewo.com'`；`login_*.js` 中的工具模块直接读写 cookie `AC_ORG_ID`（字节偏移 ≈7258，删除时带 `domain: '.seewo.com'` 与 `.test.seewo.com`）。**推断**：UC 会话 cookie 由服务端以 `.seewo.com` 域下发，因此 `pinco.seewo.com` 与 `id.seewo.com` 共享登录态。

5. **PC 客户端 SSO 机制（已核验，来自 SSO bundle）**：登录页轮询 `GET {localHttpServer}:{23300..23304}/getData/SSOLOGIN`（JSONP，timeout 1 s）获取本机已登录用户；bundle 中存在 `localStorage.loginFrom='pcClient'` 分支与 `POST /sso/createToken`。⇒ **希沃 PC 客户端自带本地 HTTPS 服务（23300–23304）与浏览器配合完成 SSO**。

### 3.3 密码登录端点与参数（已核验；Lead 关心的部分）

证据文件：`data/recon/pinco/raw/s0.seewo.com_cloud-static_uc_seewo-login_modules_pc_js_index-iframe_55bf316.js`
（711,821 B / 5,792 行，由 SSO 页面 `<script src>` **直接引用**，不是懒加载 chunk；URL 见附录 A 第 34 条）

| 端点 | 方法 | 行号 | 参数 / 说明 |
| --- | --- | --- | --- |
| `/login/v3` | POST | L670 | `dataId`(前端 UUID)、`username`(RSA)、`password`(**RSA(MD5(明文))**)、`captcha`、`system`、`clientType`、`isReMe`、`secure`、`withticket`、`isIframe`、`phoneCountryCode`、`rst`(RSA 加密开始毫秒时间戳)、`lst`(发请求毫秒时间戳) |
| `/login/v3/fallback` | POST | L1938 | 同上（FallbackUtil） |
| `/common/getPublicKeyV3` | POST | L1695 | `dataId`、`isForceSystem`、`isIframe`（force + iframe 时附 `system`）→ 返回 PEM 公钥 |
| `/common/getPublicKeyV3Fallback` | POST | L1986 | 同上（备用） |
| `/common/getVerifyCode` | POST | L1265 | 明文 `username`，发短信验证码 |
| `/common/getVerifyCodeV1` | POST | L1286 | `username`=RSA(username) + `dataId` |
| `/common/getCaptcha` | GET | L550 / L5020 | 返回 `{key, captcha}`（验证码图 base64） |
| `/common/verify/usernameV1` | POST | L2004 | `{verifyDto:{verifyType:1, challenge, validate, seccode}, username:RSA(username), dataId, phoneCountryCode}`（极验三件套字段见 L1082–1084） |
| `/sso/createToken` | POST | L3854 | 空 body；40322 表示需重新授权 |
| `/api/v2/loginByCode` | POST | L4708 | 短信验证码登录 |
| `/autoLogin` / `/loginByToken` | POST | L759 / L783 | token 自动登录 / token 鉴权登录 |
| `/scan/cancelLogin` | POST | bundle 内 | 扫码登录流程 |
| `/authThirdUnitByPhone` | POST | bundle 内 | 第三方单位绑定手机号 |
| `/api/v2/getSchool`、`getStage`、`checkAgreement`、`isWeakPwd`、`PUT /api/v2/unbindSchool`、`POST /api/v2/resetPwd`、`/api/phone/validatePwdV1` | — | bundle 内 | 注册/找回相关 |

错误码表（同一 bundle 内，已核验存在）：`40003` 图片验证码不正确、`40101/40102` 账号或密码不正确、`40103` 请输入正确的验证码、`40104` 验证码已失效、`40105` token 失效、`40322`（`verifyUserNameEncrypted` 中特殊处理，语义未验证）。

**加密方式**：`JSEncrypt`（RSA，PKCS#1 v1.5）+ 前置 `MD5`；公钥**每请求新取**，请求标识 `dataId` 为前端 `crypto` 无关的 UUID v4 模板生成。
**推断**：`rst`/`lst` 供服务端做人机/时序风控；是否强校验 **未验证**。

### 3.4 协议 A：Web RPC（`api.json?actionName=`）

证据：教师端 `670_*.js`（字节偏移 ≈118273 起）；学生 Web `workbench_*.js`（字节偏移 ≈469117 起）。

```
POST https://pinco.seewo.com{window.appPath}/api.json?actionName=<ActionName>
Content-Type: application/json;charset=utf-8
x-req-traceid: <前端生成>
x-csrf-token: <cookie csrfToken 的值>
x-server: default | evaluation | stat     ← 教师端；学生端为 main | default
x-app-code: <客户端标识>                    ← 学生端独有
x-token:    <服务端注入 HTML 的 window.token> ← 学生端独有
x-language: navigator.language
withCredentials: true; timeout: 10s
```

- 成功判据：HTTP 200 且内层 `statusCode === 0 || 200`，业务数据在 `data`；否则抛 `{message,statusCode,data}`。
- 会话失效处理（教师端，`670_*.js` 字节偏移 ≈134594 附近）：`statusCode` 命中 `TokenExpired` / `KickOut` 时跳 `{appPath}/login?redirectUrl=<当前页>`。
- 教师端请求封装有 3 个变体（默认 / `server:"evaluation"` / `server:"stat"`），学生端为 `main`/`default`。

**响应信封（已核验，来自匿名 REST 实测与 Lead 实测）**：`{"code":0,"msg":"ok","data":{...},"statusCode":200,"message":"ok"}`。

### 3.5 动作命名规律与规模（已核验）

动作名 = `<Method><Version><PathCamel>`，与 OpenAPI 路径一一对应，例如 `GetV1StudenthomeworkList` ↔ `GET /v1/studenthomework/list`。

| 集合 | 唯一动作名数量 | 证据 |
| --- | --- | --- |
| 教师端入口 chunk `670_*.js` | **1118** | 正则提取 `"<Method><V*><Camel>"` 常量 |
| 学生 Web bundle `workbench_*.js` | **1146**（与教师端交集 1113，学生 Web 独有 33，多为「评教/督导」模块） | 同上 |
| 其中含 `Student` 的动作 | **163** | 同上 |

**推断**：学生 Web 与教师 Web 使用**同一份生成 SDK**（同一 OpenAPI 规格），因此学生端 bundle 里也能看到教师侧动作；反之亦然。这对本项目意味着「一套协议客户端可覆盖两个角色，差异主要在登录入口与 `x-app-code`/`x-server` 取值」。

### 3.6 证书与源映射

- `.js.map` 未公开（404），无法直接读原始源码；未发现 Sourcemap 泄露。
- 教师端 Sentry：`release='pinco-teacher-web-pc-<version>'`、`sentry.cvtapi.com`（DSN 存在于 bundle，**不在此记录具体值**）。
- 学生 Web Sentry：`release="pinco-student-web-mobile-<version>"`（chunk 内字符串）；静态前缀 `pinco-student-web-frontend`。

---

## 4. 学生端 Web（协议 A′：同构 RPC）

> 入口部分含 **Lead 实测** 结论，均已在括号内标注来源。

### 4.1 入口矩阵

| 入口 | 结果 | 说明 |
| --- | --- | --- |
| `GET /student/workbench/` | **200**（2,792 B） | 学生 Web SPA 主入口；`appPath='/student'`、`version=1.2.20`（本人 10:56:42 复现；**Lead 实测**同结果） |
| `GET /student/notice` | 200（2,796 B） | 公告入口，`notice` 入口 chunk |
| `GET /student/joinClass` | 200（2,718 B） | 加入课堂入口，`joinClass` 入口 chunk |
| `GET /student/` | **500**（48 B，网关错误页 `Internal Server Error, real status: 500`） | 本人 10:48:34 复现；**Lead 实测**同结果 |
| `GET /student/login` | **404** | 本人 10:48:36；**Lead 实测** |
| `GET /student/code` | **404**（prod） | 本人 10:56:41/10:56:43 两次复现；**Lead 实测**：仅 test/demo 环境存在该路由 |
| `/student/material`、`/student/groupTask`、`/student/classroomExam` | 404 | 路由名猜测，不存在（避免后人重复试） |
| `GET /student2` | 302 → `/student2/notice` | Flutter Web 构建，见 4.4 |
| `GET /student2/notice` | 200（2,277 B） | Flutter Web 入口 |

学生 Web 模块名（由入口 HTML 的共享 vendors chunk 名可读）：`classroomExam`、`download`、`feedback`、`groupTask`、`guide`、`imageCut`、`joinClass`、`joinSchool`、`material`、`notice`、`patrol`、`siteOrder`、`workbench`。

### 4.2 协议细节（已核验，且与教师端同构）

```
POST https://pinco.seewo.com/student/api.json?actionName=<ActionName>
Content-Type: application/json;charset=utf-8
x-req-traceid / x-csrf-token / x-token / x-server / x-app-code / x-language
withCredentials: true
```

- `x-app-code` 取值（学生 Web bundle 字节偏移 ≈468771 起）：`window.appCode` 优先，否则按 UA 判定 `university-student-android` / `university-student-ios`，Web 兜底 `university-student-mini-program`。
- `x-token` 取自 `window.token`（服务端渲染 HTML 时注入；匿名/登录壳页为空串）。
- **Lead 实测**：未登录 `POST /student/api.json` → HTTP 200 + `{"message":"missing csrf token"}`；`/teacher/api.json` 未登录反而 500（说明两条路径的网关校验策略不同）。
- **本人实测**：`GET /teacher/api.json?actionName=GetV1Pincofaq` 与 `GET /student/api.json?...` 均 404（该方法只接受 POST，`GET` 不被路由接受）。

### 4.3 学生侧关键动作清单（已核验：动作名取自公开 bundle；语义为**推断**）

| 能力 | 动作名 | 备注 |
| --- | --- | --- |
| 作业列表 | `GetV1StudenthomeworkList`、`GetV1StudenthomeworkListWeb` | Web 端用 `…ListWeb` 变体 |
| 作业详情 / 题目 | `GetV1StudenthomeworkByStudentHomeworkIdDetail`、`GetV1StudenthomeworkByStudentHomeworkIdQuestions` | 路径参数 `studentHomeworkId` |
| 作答 / 提交 | `PostV1StudenthomeworkByStudentHomeworkIdQuestionByQuestionIdAnswer`、`PostV1StudenthomeworkByStudentHomeworkIdSubmit` | 提交作业 |
| 章节任务 | `GetV1StudentChapterTaskList`、`…Detail`、`…Answers`、`PostV1StudentChapterTaskSubmit`、`GetV1StudentChapterTaskAnswerIsShareToTheme`、`PostV1StudentChapterTaskShareAnswerToTheme` | 学习任务主线 |
| 小组任务 | `GetV1StudentCourseGroupTaskByCourseGroupTaskIdDetails`、`…Answer`、`…CoverOtherAnswer`、`PostV1StudentCourseGroupTaskByCourseGroupTaskIdSubmit` | |
| 随堂测 | `GetV1StudentchapterexamByChapterExamIdQuestions`、`…QuestionByQuestionIdAnswer`、`PostV1StudentchapterexamByChapterExamIdSubmit`、`PostV1StudentchapterexamByChapterDirectoryMaterialIdClear` | |
| 课件/资源 | `GetV1CourseStudentworkbench`、`GetV1StudentClassresourcesCourse`、`GetV1StudentClassresourcesClass`、`PostV1CoursewareStudentOpen`、`PostV2CoursewareStudentMaxLook`、`PostV1StudentResourcesStar` | |
| 公告 / 消息 | `GetV1StudentnoticeListWeb`、`PostV1StudentnoticeByStudentNoticeIdDetail`、`…Star`、`GetV1MsgcenterStudentMsgs` | |
| 互动 | `GetV1ClassroomevaluationTrend`、`PostV1ClassroomevaluationSubmit`、`PostV1TeachingInteraction*Answer`、`PostV1BrainstormStudentText/Img/Msg/Start` | |
| 认证/设备 | `GetV1JsSdkTicket`（微信 JS-SDK）、`PostV1CaptchasSms`、`PostV2CaptchasSmsVerify`、`PutV1QrCodeAttendCourseClass`、`GetV1ClassroomCodeByCode` | |

### 4.4 Flutter Web 入口 `/student2`（已核验）

- `GET /student2` → 302 → `/student2/notice`（200）。
- 页面注入：`window.baseUrl='pinco.seewo.com'`（无协议前缀）、`appPath='/student2'`、`version='1.2.2'`、`appCode='university-student-mini-program'`、`flutterWebRenderer='html'`、`cdnSubPath='https://s0.seewo.com/cloud-static/pinco/flutter/static/1762860981918/flutter/build/web/'`，主产物分片 `main-0 … main-5`。
- **意义（推断）**：这是**同一套学生端业务在浏览器上的实现**，因此「不碰安卓设备、不改包、不 root」也能到达学生端 UI 与网络层 —— 对抓包与自动化来说是成本最低的路线（前提：账号可用、Playwright 可用）。

---

## 5. 学生端安卓 App（协议 B：REST `/server-main/api/v1`）

### 5.1 App 事实（已核验，静态解析官方 APK）

| 项 | 值 | 证据 |
| --- | --- | --- |
| 官方下载链 | `pinco.seewo.com/dl` → Android 跳 `https://e.seewo.com/download/file?code=seewoPincoAndroid` → 302 → 腾讯 COS 直链 | `/dl`（10:49:45）、302（10:49:55） |
| 文件名 / 大小 / 时间 | `seewoPincoStudent_1.2.44.7025_normal.apk`，103,219,814 B，Last-Modified 2024-07-19 | 响应头 |
| 包名 / 版本 | `com.seewo.pinco.student`，`versionName=1.2.44.7025`，`versionCode=7025` | `AndroidManifest.xml` 解析 |
| SDK | `minSdkVersion=26`、`targetSdkVersion=30`、`compileSdkVersion=33` | 同上 |
| 形态 | **Flutter AOT**：`lib/*/libflutter.so` + `libapp.so`（13.7 MB arm64）+ `assets/flutter_assets/`（990 项）；无 `kernel_blob.bin`（release 构建） | zip 目录 |
| ABI | **仅 `arm64-v8a`、`armeabi-v7a`**（无 x86/x86_64）⇒ x86 模拟器无法直接运行 | zip 目录 |
| 打包/加固 | `application name=com.Proxy.ShellApplication` + `meta-data _app_name=com.seewo.pinco.student.application.App` + `uxml_tool_version=3.7.5`；dex 内含 `jiagu/`、`shell/`、`libxloader.so`、`VenomSdk` 字符串 | Manifest 解析 + dex 字符串 |
| 网络配置 | `android:networkSecurityConfig=@0x7f130001`；对应 `res/4u.xml`：`base-config cleartextTrafficPermitted=true`、`trust-anchors` 含 `src=system` 与 **`src=user`（`overridePins=true`）**、**无 `pin-set`** | Manifest + 极小 AXML 解析 |
| 明文流量 | `android:usesCleartextTraffic=true` | Manifest |
| 深链 | `seewopinco://openapp`、`custom_scheme://com.seewo.pinco.student/spring_activity`、`https://ul.seewo.com/pinco/nfc`、`tencent1109862033` | Manifest + 前端 JS |
| iOS 对应 App | `https://itunes.apple.com/cn/app/id1476393682`（前端 JS 内硬编码，官方跳转） | `live_*.js` / `player_*.js` |
| 第三方 SDK | 华为/小米/OPPO/vivo 推送、腾讯 Bugly、Sentry、marsxlog、IJKPlayer、OpenCV、MLKit 条码、`libsqlite_encrypt.so`、`libCtaApiLib.so`、`libVenomSdk.so` | zip + strings |
| 硬编码凭据 | libapp 内存在一条指向 `itapis.cvte.com/iac/app/access_token` 的 `appid`/`secret` 串 ⇒ **存在硬编码凭据**（**本报告不记录其值，本项目不得使用**） | libapp strings |
| 未发现 | `Xposed`/`frida`/`/proc/self/maps` 等真实反调试串（此前的计数命中均为无关子串，如 `Friday`、TLD 列表） | strings 上下文核对 |

### 5.2 协议 B 的 base 与匿名行为（已核验）

- App 内**业务 API 的主前缀**：`/server-main/api/v1|v2|v3/...`（**122** 条唯一路径）；libapp 内另有主机常量 `https://pinco.seewo.com`、`https://pinco.seewo.com/student/code`（后者 prod 404，见 4.1）。
- 另有非业务/客户端专用路径（**仅字符串存在，用途未验证**）：`/v1/sdk/init`、`/v1/sdk/callback`、`/api/user/linkCode/info`、`/api/v1/update`、`/v2.0/pre_gateway`、`/v2.0/pre_report`、`/v2.0/clientreport`、`/server-main/api/v1/system/currentTime`、`/server-main/api/v3/...`。
- 匿名实测（本人，2026-10-19 10:52）：

| 请求 | 结果 |
| --- | --- |
| `GET /server-main/api/v1/system/currentTime` | **200** `{"code":0,"msg":"ok","data":{"currentTime":…},"statusCode":200,"message":"ok"}` |
| `GET /server-main/api/v1/studenthomework/list` | 200 + `{"code":403,"msg":"access forbidden","statusCode":403,"message":"access forbidden"}` |
| `GET /server-main/api/v1/app/functions` | 同上 403 |
| `GET /server-main/api/v1/open-source-license` | 200 + `{"code":401,"msg":"illegal argument",…}` |

⇒ **401 = 参数/校验不满足，403 = 无会话**；且**HTTP 状态与内层 `statusCode` 并不总是一致**（403 业务码可以套在 HTTP 200 上）——这一点对适配器的错误处理很关键。

### 5.3 协议 B 端点清单与两条协议的交叉验证（已核验）

- App 内 122 条 REST 路径中，**98 条可与 Web 端生成动作名一一对齐**（`Method` 由前端调用方决定，路径完全一致），例如：

| REST 路径（App） | 对应动作名（Web SDK） |
| --- | --- |
| `/api/v1/studenthomework/list` | `GetV1StudenthomeworkList` |
| `/api/v1/student/chapter/task/submit` | `PostV1StudentChapterTaskSubmit` |
| `/api/v1/course/student/questionsWithAnswerContent` | `GetV1CourseStudentQuestionsWithAnswerContent` |
| `/api/v1/captchas/sms` | `PostV1CaptchasSms` |
| `/api/v2/captchas/sms/verify` | `PostV2CaptchasSmsVerify` |
| `/api/v1/chapterdirectory/material/countLearningProgress` | `PostV1ChapterdirectoryMaterialCountLearningProgress` |
| `/api/v1/qrCode/attendCourseClass` | `GetV1QrCodeAttendCourseClass` / `PostV1…` |
| `/api/v1/teaching/classroomresources/student/list` | `GetV1TeachingClassroomresourcesStudentList` |

- 其余 24 条是**带尾斜杠的父路径**（如 `/api/v1/studenthomework/`、`/api/v1/student/course/`、`/api/v1/student/courseGroupTask/`），与「路径参数子资源」形态一致 ⇒ **推断**：形如 `/api/v1/studenthomework/{studentHomeworkId}/detail`、`/api/v1/student/courseGroupTask/{id}/submit`。
- **结论（重要）**：`/server-main/api/*` 与 Web 的 `api.json?actionName=*` **不是两套业务能力**，而是同一 OpenAPI 规格的两种网关表达。要把 Web 的抓包结果迁移到 App 路线（或反之），只需做「动作名 ↔ REST 路径」的机械映射，无需重新逆向。

### 5.4 App 端请求头与字段（静态字符串，语义**未验证**）

- 头名字符串确实存在于 `libapp.so`：`x-app-version`、`x-app-code`、`x-req-device`、`x-req-traceid`、`x-user-role`、`x-language`、`x-auth-token`、`authorization`、`X-CSRF-TOKEN`（APK dex 中另有 `X-CSRFTOKEN`）。**未发现** `x-server` 独立字符串（其他文件里的命中经上下文核对为 HLS `#EXT-X-SERVER-CONTROL` 子串），也**未发现**任何签名算法实现串；`signature` 一词在 Flutter 侧仅出现在微信 JS-SDK 上下文（`GetV1JsSdkTicket` 返回值里的 `signature`）。⇒ **App 是否对请求体签名：未验证**，但**没有证据**表明存在 HMAC/时间戳签名（无 `sign=`/`nonce` 之类实现串）。
- 可能用到的字段名（libapp 字符串，存在性已核验 / 语义未验证）：`studentHomeworkId`、`questionId`、`answerContent`、`answers`、`options`、`attachments`、`pageSize`、`chapterTaskId`、`courseGroupTaskId`、`courseGroupId`、`courseId`、`classroomId`、`classId`、`materialId`、`remark`、`duration`、`submitted`/`isSubmit`、`deadline`/`endTime`、`fileName`/`fileSize`/`url`。
- **不存在**（0 命中，避免构造错误请求体）：`homeworkId`、`pageNum`、`pageNo`、`answerList`、`submitType`、`studentAnswer`、`nonce`。⇒ **推断**：分页用 `page` + `pageSize`，作业一律以 `studentHomeworkId` 为主键。
- Dart 侧生成方法名同样出现：`PostApiV1StudenthomeworkByStudentHomeworkIdQuestionByQuestion`、`PostApiV1StudentCourseGroupTaskByCourseGroupTaskIdSubmit`（印证 5.3 的拼装形状）。

### 5.5 App 内可见的环境与第三方主机（已核验：字符串存在；用途**推断**）

| 主机 | 用途（推断） |
| --- | --- |
| `pinco.seewo.com` / `pinco.test.seewo.com` / `pinco-dev.test.seewo.com` / `pinco-demo.seewo.com` | 生产 / 测试 / 开发 / 演示环境 |
| `id.seewo.com` / `id.test.seewo.com` | UC 登录 |
| `pinco-swlog.test.seewo.com`（`/device/connect`、`/log/v1`） | 设备日志上报 |
| `mc.seewo.com` | 配置/消息中心（推断） |
| `itapis.cvte.com/iac/app`、`fbp.cvte.com/api/product`、`myou.cvte.com/friday/agent`、`mdc-console.cvte.com/buz/strategy` | 视源内部服务（IAC/产品目录/数据上报/策略） |
| `ul.seewo.com/pinco/nfc` | NFC 深链落地页 |
| `sentry.cvtapi.com/<project>` | 崩溃上报（多项目 ID） |

---

## 6. 鉴权形态汇总

| 维度 | 事实 | 证据 / 置信度 |
| --- | --- | --- |
| 会话载体 | **Cookie**（`withCredentials:true`）；域 `.seewo.com`；具体会话 cookie 名**未验证**（需登录态） | 已核验（机制）/ 未验证（名字） |
| CSRF | 双提交 Cookie：`x-csrf-token` ← cookie `csrfToken`；缺失时线上返回 `{"message":"missing csrf token"}` | 已核验 |
| 服务端注入 token | 学生 Web 用 `window.token` → 头 `x-token`（教师端未见） | 已核验 |
| 客户端标识 | `x-app-code`（`university-student-android/ios/mini-program` 等）、`x-req-device`、`x-app-version`、`x-user-role` | 已核验（头名） |
| 路由头 | `x-server: default/evaluation/stat/main` 决定后端集群 | 已核验 |
| 链路追踪 | `x-req-traceid`（前端生成）、响应侧 `x-apm-traceid` | 已核验 |
| 登录加密 | `RSA(MD5(pwd))`、`RSA(username)`；JSEncrypt；每请求公钥 | 已核验 |
| 时序/反重放 | `rst`、`lst`（毫秒时间戳）；无 `nonce` 字段证据 | 已核验（字段）/ 未验证（是否强校验） |
| 请求体签名 | **未发现**任何签名实现；`signature` 仅出现在微信 JS-SDK 返回结构 | 已核验（未见证据） |
| Token 失效语义 | `statusCode` ∈ `TokenExpired`/`KickOut` → 跳登录；REST 侧 401/403 业务码 | 已核验 |

---

## 7. 反自动化信号

| 信号 | 状态 | 证据 |
| --- | --- | --- |
| 阿里云 WAF | **有**：`acw_tc` cookie（HttpOnly，1800 s）出现在 41/62 响应 | 响应头 |
| 网关 | `Server: cagw`（统一 API 网关），单机有 `x-ser` 实例标识 | 响应头 |
| 图片验证码 | **有**：`GET /common/getCaptcha` 返回 `{key, captcha}`；`needVerifyCode` 由服务端触发，且**发短信前强制要求** | SSO bundle L550/L5020/L5051 |
| 极验 Geetest | **有**：`/common/verify/usernameV1` 的 `verifyDto` 带 `challenge/validate/seccode`；dex 内可见 `static.geetest.com`、`onepass.geetest.com` | SSO bundle L2004/L1082；dex strings |
| 免验码分支 | **有**：SSO bundle 内 `initNoCaptcha`（L267）与 `#j-loginNoCaptcha` 容器 ⇒ 服务端可下发免验码 | 已核验 |
| 短信验证码 | **有**：`/common/getVerifyCode(V1)`、`PostV1CaptchasSms`、`PostV2CaptchasSmsVerify` | 已核验 |
| 时序风控字段 | `rst`/`lst` | 已核验 |
| 限流响应头 | **未见**（无 `x-ratelimit-*`、无 `Retry-After`） | 62 条记账请求的响应头统计 |
| CORS | API 响应 `Access-Control-Allow-Origin: *` **且** `Access-Control-Allow-Credentials: true`（该组合会被浏览器按规范拒绝，因此跨域带凭据调用实际不可用；同源不受影响） | 403 响应头 |
| App 加固 | 第三方壳（`com.Proxy.ShellApplication`、`jiagu`、`xloader`、`VenomSdk`）⇒ 静态改包/重打包成本高 | Manifest/dex/dll |
| 证书固定 | **无 `pin-set`**；`networkSecurityConfig` 信任 **user CA**；`usesCleartextTraffic=true` | Manifest + `res/4u.xml` |
| 反调试串 | **未发现**真实 `frida`/`Xposed`/`/proc/self/maps` 检测串（此前计数命中经上下文核对为无关子串） | strings 上下文 |
| UA / Referer 校验 | **未验证** | — |
| 设备指纹/风控 SDK | `libCtaApiLib.so`（腾讯 CTA 合规）、`Bugly`、`VenomSdk`；**是否有业务风控判定：未验证** | zip/strings |

---

## 8. 抓包/接入可行路径对比

| 路线 | 前置条件 | 产出 | 风险 / 代价 | 判定 |
| --- | --- | --- | --- | --- |
| **A. 教师/学生 Web + 浏览器（Playwright 或本机浏览器）** | 本机浏览器；账号；若要自动化则需 Playwright（**本机未安装**） | 完整 `api.json` 请求/响应、cookie 全貌、登录流程 | 风控（验证码/设备指纹）、需要人工过验证码 | **首选**：协议已完整静态还原，抓包只需做「确认」 |
| **B. 学生 Flutter Web `/student2` + 浏览器** | 同上 | 学生侧 UI 与 REST/RPC 请求 | 同上；Flutter Web 的请求由 Dart 发起，devtools 可看 | **首选（学生侧）** |
| **C. 安卓 App + mitmproxy** | 安卓设备（**arm64**；x86 模拟器不可用）+ 用户 CA 安装 + 代理 | App 真实请求头（`x-req-device` 等）与字段 | 加固壳可能校验代理/环境；须只读 | **可行（配置层已核验支持用户 CA）** |
| **D. APK 静态分析** | 无（本机标准库即可，已实施） | 端点、字段名、权限、深链、加固/SDK 事实 | 无法得到请求体语义与运行时头 | **已做，产出见第 5 节** |
| **E. APK 反编译（jadx/apktool）** | 需安装工具（本机无、不装） | 更完整的 Kotlin 逻辑 | 壳加固后 dex 被加密，收益有限 | **不推荐**，除非先脱壳 |
| **F. PC 客户端抓包** | 需官方安装包 + Windows 环境 | PC 端自有协议（可能走本地 23300–23304 服务） | exe 为 NSIS 原生程序，静态不可读；需真机 | **低优先**；证据见 9.3 |
| **G. 官方 Open API** | 无（**未发现任何面向第三方的 Open API 文档/申请入口**） | — | — | **不存在（未发现）** |

**共同红线**：只读、限速、不绕过老师设置的限制、不批量、不代刷；遇到验证码即人工介入或放弃。

---

## 9. 其余已核验事实

### 9.1 `/student/code` 入口

- prod：404（本人两次；**Lead 实测**一致）。App/测试环境串里存在 `https://pinco.test.seewo.com/student/code`、`https://pinco-dev.test.seewo.com/student/code`、`https://pinco-demo.seewo.com/student/code`。
- **推断**：`/student/code` 是「输入课程码/扫码加入课堂」的 Web 落地页，仅测试/演示环境注册该路由；生产环境下该能力由 App 或 `/student/joinClass` 承担（后者 prod 200）。

### 9.2 教师端实名/品牌信息

- 页脚：`e.seewo.com`、备案号「粤ICP备12092924号」、`Copyright ©2026 GuangZhou Shirui. All Rights Reserved.`（`login_*.js` 字节偏移 ≈6883 区）。
- 第三方协议/隐私政策由 `AgreementLoader` 动态加载（SSO 页 `./agreementTemplate.html?__inline&system=PincoTeacher`）。

### 9.3 桌面端（Windows / macOS）

| 项 | 事实 |
| --- | --- |
| 官方下载入口 | `https://pinco.seewo.com/dl`（200）：桌面按 UA 跳 `e.seewo.com/download/file?code=seewoPincoWindows`（Windows）/ `code=seewoPincoMac`（macOS）；移动按 UA 跳 App Store id1476393682 或 `code=seewoPincoAndroid` |
| Windows 包 | `seewoPincoStudentSetup_1.2.7.1011.exe`，110,707,376 B，Last-Modified **2022-05-05**；**NSIS 2.46.5-Unicode / 原生 32 位**；`app.asar`、`electron` **0 命中**；含 `PSAPI.DLL` 进程/模块枚举字符串（`CreateToolhelp32Snapshot`、`EnumProcesses`） |
| macOS 包 | `seewoPincoStudent_1.2.1.2771.dmg`，17,248,698 B，Last-Modified **2021-01-14**；含 Apple 代码签名 `Developer ID Application: Guangzhou Shirui Electronics Co., Ltd. (P76M9BE8DQ)`（**厂商真实性旁证**）；镜像内部压缩，本机无法挂载 → 形态 **未验证** |
| PC 客户端 SSO | 浏览器侧轮询 `https://local.id.seewo.com:23300-23304/getData/SSOLOGIN`（JSONP）⇒ PC 客户端内置本地 HTTPS 服务 |
| 结论 | 官方只分发**学生端**桌面客户端；**未发现教师端 PC 安装包**；**不是 Electron**，因此「读 app.asar 拿端点」的思路对本产物不成立 |

### 9.4 未走通的探测（记录以免重复）

- `GET /teacher/api.json?...`、`GET /student/api.json?...` → 404（只接受 POST）。
- JS source map（`*.js.map`）→ 404（未公开）。
- 路由猜测 `/teacher/{latest-courses,site-board,share,main-no-auth,noauth}`、`/student/{material,groupTask,classroomExam}` → 均 404。
- `s0.seewo.com/robots.txt`、`pinco.seewo.com/robots.txt` → 404。
- `e.seewo.com/download` → 404（该站无目录页）。

---

## 10. 明确「没有真实登录态就拿不到」的部分

以下全部为 **未验证**，不得据此实现任何功能：

1. 作业/章节任务/随堂测/小组任务的**列表与详情数据**、题目内容与选项、答案与解析。
2. 提交类接口的**真实请求体**（字段名、必填项、多稿/幂等语义、草稿与重做的差异）与响应结构。
3. 成绩、排名、统计、评教、督导等报表数据。
4. 会话 cookie 的**确切名字与形态**（`csrfToken` 之外）、`x-token` 的实际值与生命周期、`AC_ORG_ID` 的写入方。
5. `authorization`/`x-auth-token`/`x-req-device`/`x-app-version` 等在 App 端**是否必需**、如何生成、是否与设备绑定。
6. 是否存在**请求体签名/验签**（静态侧无证据，但需要真实抓包否定或证实）。
7. 验证码/风控的**触发条件**（多少次失败后出现、是否绑定 IP/设备）。
8. 教师端主应用 chunk（`main_*.js`）的内容：其哈希只在登录后的 HTML 中出现。
9. 登录后的路由与深链行为（如 `/student/notice` 未登录展示什么、`/teacher/live` 匿名可访问到什么程度）。
10. 服务端限速阈值（响应头未暴露）。

---

## 11. 下一步最小联网实验清单（含前置条件）

> 原则：每条都「最小、只读、可回滚」，失败即停；遇到验证码人工介入；动到写操作前必须停下来确认。

| # | 实验 | 前置条件 | 预期产出 | 风险 |
| --- | --- | --- | --- | --- |
| E1 | **Web 端只读作业链路**：登录教师/学生 Web → 只发 `GetV1Student*`/`GetV1Studenthomework*` → 记录请求头与响应 | 本人账号；浏览器或 Playwright；只读动作 | 确认 cookie 名、`x-token` 必需性、信封一致性、分页字段 | 低（只读） |
| E2 | **验证 POST 语义**：`POST /student/api.json?actionName=GetV1StudenthomeworkListWeb`（只读动作） | 先 GET 页面拿到 `csrfToken` cookie | 确认「`api.json` 必须 POST」与 csrf 必需性 | 低 |
| E3 | **`/student2` Flutter Web 端到端**：浏览器打开 `https://pinco.seewo.com/student2/notice` 并登录 | 浏览器（自动化需装 Playwright） | 判断学生端浏览器路线是否可完全替代 App 抓包 | 低 |
| E4 | **App 抓包验证 REST 路线**：mitmproxy + 用户 CA，仅浏览作业列表 | arm64 安卓机（x86 模拟器不可用）；不改包 | 证实 App 走 `/server-main/api/v1`、拿到真实头集合 | 中（加固壳可能检测代理；保持只读） |
| E5 | **App 签名否定性验证**：对比同一动作两次请求的差异 | 同 E4 | 判定是否存在签名/时间戳/nonce | 低 |
| E6 | **PC 客户端 SSO 探测（本机服务）**：安装官方 PC 客户端后 `GET http://local.id.seewo.com:2330x/getData/SSOLOGIN` | Windows/macOS 环境（本机 Linux 不可行） | 判断桌面端是否能作为会话来源 | 中（涉及本机服务，仅 GET 只读） |
| E7 | **限速边界**：在只读前提下把间隔降到 1 s 观察是否出现限流 | 无需凭据 | 为适配器定速率参数 | 中（**建议只做极小样本，或直接采用保守 2–3 s**） |
| E8 | **test 环境 `/student/code`**：确认测试环境路由形态 | 仅当有测试账号时；**不得对 prod 反复探测** | 课程码入口语义 | 低（但需账号） |
| E9 | **`GetV1Pincofaq`/`system/currentTime` 作为健康检查** | 无 | 验证网关可用性与时钟偏移（`currentTime` 可用于校时） | 无 |

**明确不建议做**：反编译脱壳、改包重打包、爆破验证码、批量账号、对提交接口做压测、绕过教师设置的限制。

---

## 附录 A：本轮请求清单（68 条，全部 GET/HEAD，全部匿名）

- 时间窗：2026-10-19 10:45:32 – 10:58:18（+0800），单线程，间隔 ≥1.2 s（安装包请求同期间隔发出）。
- 完整机器可读日志：`data/recon/pinco/fetch_log.jsonl`（含每条的 URL、状态码、字节数、响应头、落盘文件名）。

| 分组 | 条数 | 备注 |
| --- | --- | --- |
| 教师端路由（HTML） | 16 | `/`、`/teacher`、`/teacher/`、`/teacher/login`、`/teacher/login-callback-iframe`、`/teacher/third-party-login`、`/teacher/live`、`/teacher/player`、`/teacher/main`、`/teacher/main-redirect/`、`/teacher/api.json`(404)，及 latest-courses/site-board/share/main-no-auth/noauth 5 条 404 探测 |
| 教师端 JS / 源映射 | 15 | runtime + login + thirdPartyLogin + 1291 + 6548 + 177 + 4200 + 2828 + 6934 + 388 + 670 + live + player（含 2 条 `.map` 404） |
| SSO | 3 | `login-iframe` HTML 1 + 登录 bundle 1 + 监控 bundle 1 |
| 学生端路由（HTML） | 13 | workbench/notice/joinClass 200；`/student/`500；`/student/login`、`/student/code`×2、material、groupTask、classroomExam 404；`/student2`302、`/student2/notice`200；`/student/api.json`(404) |
| 学生 Web JS | 2 | runtime~workbench、workbench |
| Flutter Web 资源 | 1 | `version.json`（HEAD，200/0 B） |
| App REST 端点 | 5 | currentTime、studenthomework/list、app/functions、open-source-license、pincofaq |
| 官方下载 | 5 | `/dl`、3 条 `e.seewo.com/download/file`、`e.seewo.com/download`(404) |
| `robots.txt` | 2 | 均 404 |
| **小计（记账）** | **62** | 见左列，机器可读日志 `fetch_log.jsonl` |
| 安装包（未入日志，单独记录） | 6 | 3 次 HEAD 探大小 + 3 次完整 GET（APK 103 MB、exe 110 MB、dmg 17 MB）；仅字节级静态解析，未安装未执行 |

**响应头统计（仅 62 条记账请求，不含安装包请求）**：`Server: cagw` 62/62；`x-apm-traceid` 62/62；`Set-Cookie: acw_tc` 41/62；`x-ser` 21/62；`x-cache` 21/62；`access-control-allow-credentials: true` 23/62；未见任何限流头。

## 附录 B：可复现命令

```bash
# 0) 目录
cd <repo>/data/recon/pinco

# 1) 教师端入口与路由（-n = 不跟随重定向，用于观察 Location）
python3 fetch.py -n https://pinco.seewo.com/ https://pinco.seewo.com/teacher https://pinco.seewo.com/teacher/login

# 2) SSO 登录页（公开）
python3 fetch.py -n "https://id.seewo.com/login-iframe?system=PincoTeacher&callbackIframeUrl=https%3A%2F%2Fpinco.seewo.com%2Fteacher%2Flogin-callback-iframe"

# 3) 学生端入口
python3 fetch.py https://pinco.seewo.com/student/workbench/ https://pinco.seewo.com/student/notice https://pinco.seewo.com/student/joinClass
python3 fetch.py -n https://pinco.seewo.com/student2        # 302 -> /student2/notice

# 4) 匿名 REST 行为（只读）
python3 fetch.py https://pinco.seewo.com/server-main/api/v1/system/currentTime
python3 fetch.py https://pinco.seewo.com/server-main/api/v1/studenthomework/list

# 5) 官方安装包（只读；下载后仅做静态解析，不安装不执行）
python3 fetch.py -n "https://e.seewo.com/download/file?code=seewoPincoWindows"   # 取 Location
python3 sample.py "<Location>" raw/seewoPincoStudentSetup_1.2.7.1011.exe full

# 6) 静态解析（本报告用到的自研极小工具，均只读）
python3 axml.py raw/apk_x/AndroidManifest.xml          # 二进制 XML：权限/组件/深链/引用型属性
python3 axml.py raw/apk_x/res_4u.xml                   # network-security-config
python3 res_id.py raw/apk_x/resources.arsc 0x7f130001  # 资源 ID 解析（辅助）
# 字符串/端点提取：见本期会话记录（正则扫描 libapp.so / classes.dex / *.js）
```

## 附录 C：证据文件清单（`data/recon/pinco/`，**不入库**）

| 路径 | 内容 |
| --- | --- |
| `fetch_log.jsonl` | 62 条请求的机读日志（URL/状态/字节/响应头/落盘名）；另有 6 条安装包请求未入该日志（见 9.3） |
| `fetch.py`、`sample.py` | 只读抓取器与 Range 取样器 |
| `axml.py`、`res_id.py` | 极小 AXML / ARSC 解析器 |
| `raw/pinco.seewo.com_teacher_login` | 教师登录壳页（含 `window.*` 配置） |
| `raw/s0.seewo.com_..._login_20fc1a5a6bbf444be010.js` | 教师登录入口 chunk（SSO iframe URL、`AC_ORG_ID`） |
| `raw/s0.seewo.com_..._670_8bc93efd516ff6d146b2.js` | 教师端生成 SDK 动作名（1118）+ `api.json` 请求封装 |
| `raw/s0.seewo.com_...workbench_ae701e4755dac93f5621.js` | 学生 Web bundle（1146 动作名、`x-token`/`x-app-code`、RPC 封装） |
| `raw/id.seewo.com_login-iframe_...` | SSO 页面（含 `localHttpServer`、`system`、`isIframe` 注入） |
| `raw/s0.seewo.com_..._index-iframe_55bf316.js` | SSO 登录 bundle（登录端点 L670/L1938、公钥 L1695、验证码、PC 客户端轮询） |
| `raw/pinco.seewo.com_dl` | 官方下载分流页 |
| `url_seewoPinco{Windows,Mac,Android}.txt` | 三个官方安装包直链 |
| `raw/seewoPincoStudent_1.2.44.7025_normal.apk` 等 | 官方安装包原件（只读解析，未执行） |
| `raw/apk_x/{AndroidManifest.xml,classes.dex,lib_arm64-v8a_libapp.so,res_4u.xml,manifest_dump.txt}` | APK 解析中间产物 |

## 附录 D：来源清单（URL + 取回时间 +0800）

| URL | 时间 | 用途 |
| --- | --- | --- |
| `https://pinco.seewo.com/`（302） | 10:45:32 | 根路径落在教师端 |
| `https://pinco.seewo.com/teacher/login` | 10:45:35 | 教师端 `window.*` 配置、入口 chunk |
| `https://pinco.seewo.com/teacher/third-party-login` | 10:46:27 | 匿名入口与 token/uuid 注入 |
| `https://pinco.seewo.com/teacher/live`、`/teacher/player` | 10:47:08 / 10:47:10 | 匿名可访问入口 |
| `https://s0.seewo.com/cloud-static/pinco/pinco-teacher-frontend/static/runtime_*.js` | 10:45:45 | 入口 chunk 映射 + `.map` 声明 |
| `.../static/670_8bc93efd516ff6d146b2.js` | 10:47:28 | 协议 A 请求封装、动作名清单 |
| `https://id.seewo.com/login-iframe?system=PincoTeacher&callbackIframeUrl=...` | 10:48:34 | SSO 入口与本地客户端配置 |
| `https://s0.seewo.com/cloud-static/uc/seewo-login/modules/pc/js/index-iframe_55bf316.js` | 10:48:57 | 登录端点与加密细节（L670 等） |
| `https://pinco.seewo.com/dl` | 10:49:45 | 官方下载分流 |
| `https://e.seewo.com/download/file?code=seewoPinco{Windows,Mac,Android}` | 10:49:52–55 | 官方安装包直链 |
| `https://pinco.seewo.com/server-main/api/v1/system/currentTime` | 10:52:09 | 匿名 200 健康检查 |
| `https://pinco.seewo.com/server-main/api/v1/studenthomework/list` | 10:52:22 | 匿名 403 行为 |
| `https://pinco.seewo.com/student/workbench/` | 10:56:42 | 学生 Web 入口与配置 |
| `https://s0.seewo.com/cloud-static/pinco/pinco-student-web-frontend/static/workbench_*.js` | 10:56:58 | 协议 A′ 封装与学生动作名 |
| `https://pinco.seewo.com/student/notice`、`/student/joinClass` | 10:57:49 / 10:57:53 | 学生 Web 其它入口 |
| `https://pinco.seewo.com/student2/notice` | 10:58:10 | Flutter Web 入口与资源配置 |
| 官方 APK（腾讯 COS 直链） | 10:50–10:51 | App 静态事实（Manifest/网络配置/字符串） |

## 附录 E：未验证清单（汇总，**不得作为决策依据**）

1. 会话 cookie 名称与生命周期；`x-token` 的值与签发方式；`AC_ORG_ID` 的写入方。
2. 请求体/签名：App 是否需要 `authorization`/`x-auth-token`/`x-req-device`/`x-app-version`/`signature`，及其生成规则；是否存在 HMAC/时间戳验签。
3. `rst`/`lst` 是否被服务端强制校验（若有窗口区间，值是多少）。
4. 验证码/极验触发条件与免验码（`initNoCaptcha`）的下发规则。
5. 所有业务端点的**请求体字段与响应结构**（本报告只给出「字符串存在性」，未验证其语义与必填性）。
6. `/student/code` 的真实交互（课程码/扫码）——prod 404，仅测试环境存在。
7. macOS dmg 内部的桌面端形态（需 macOS 挂载）；PC 客户端自有协议（需 Windows）。
8. 服务端限速阈值与 WAF 拦截阈值。
9. 桌面端与 App 端是否共享同一 UC 会话（cookie 域推断为 `.seewo.com`，未验证）。
10. 教师端未登录可访问的 `/teacher/live`、`/teacher/player` 在带登录态时的完整行为。

---

**（本报告为 Stage 1a 静态+匿名实测结论；Lead 的真实会话实测结果将由 Lead 回填至第 3.4/4.2/5.2 三节的「实测列」后，本报告升级为 Stage 1b。）**

---

# 附录 L：Lead 实测回填（真实登录 + 只读抓取，2026-10-19）

> **范围界定（用户裁决 2026-10-19）：只做学生端。** 教师端（教师工作台、章节目录
> 管理、布置/催交、批改与统计）一律不做，即使能从 bundle 里读到 action 名也不实现。
> 已实测：教师端接口用学生身份调用会回 `6571 没有该课程权限`。
> 注意别被名字误导 —— 本账号在品课里同时带教师身份（SPA 跳
> `/student/workbench/teacher`），且登录用的 system 码是 `PincoTeacher`，
> 这两者都与「做教师功能」无关。

> 抓取方式不是纯 HTTP：本机是 Linux，无法用 computer use（后端要拉 Windows 的
> `powershell.exe`），Firefox 的 Remote Agent 只开了 WebDriver BiDi。所以用
> **Firefox(headless) + BiDi + 站点自己的 JS 上下文**：先调站点自己的
> `Util.comLogin(...)` 完成登录，再在**页面内** `fetch` 接口 —— cookie / csrf /
> x-token 天然正确，绕开了「纯 HTTP 复刻应用层会话」这个坑（纯 HTTP 下
> `/student/api.json` 稳定回 `403 invalid csrf token`）。
> 复用工具：`scripts/bidi_ws.py`。全程只读，未调用任何提交动作。

## L.1 协议要点（实测修正 §3 的推断）

| 项 | 实测结论 |
| --- | --- |
| 学生端 Web RPC | `POST https://pinco.seewo.com/student/api.json?actionName=<Op>`，body 为 JSON |
| 传输方式 | **必须 POST**；同参数改 GET → `404 Not Found` |
| 鉴权 | 仅靠 cookie 即可（`.seewo.com` 的 `x-token` + `pinco.seewo.com` 的 `csrfToken`）；**把 SSO token 当成 `x-token` 头发出去反而会让接口变脸**（`GetV1CourseStudentworkbench` 直接失败） |
| 双层判错 | 实测确认：HTTP 200 里套 `statusCode:401/403/6571`；还有 `{}`（空壳）、`500 ERR_ASSERTION`（不存在的 action 名也是这个，**不能用它判断 action 是否存在**） |
| 已知业务码 | `6571 没有该课程权限`（教师端接口被学生身份调用时出现）、`401 illegal argument`（缺必填参数） |
| SSO 应用会话 | `GET https://id.seewo.com/common/userInfo` 可取用户资料；`POST /common/setToken {tk:<token>}` 下发 `.seewo.com` 的 `x-token` cookie；学生 SPA 支持 `?auth=<token>` 直接注入 `window.token`（顶层打开时没有父窗口 postMessage，不加这个参数页面会**一直空白**） |

## L.2 作业链路（全部实测确认）

| 用途 | action | 参数 | 关键返回 |
| --- | --- | --- | --- |
| 课程列表 | `GetV1CourseStudentworkbench` | `{page,pageSize≤20}` | `data.total`、`data.content[]`（courseId/courseName/creatorUser/learnInfo/teach） |
| 班级列表 | `GetV1StudentClassList` | `{}` | 21 个班级（classId/className/creatorName/studentCnt） |
| **作业列表** | `GetV1StudenthomeworkList` | `{courseId,page,pageSize}` | `data.content[]`：title/content/studentHomeworkId/**submitted**/**expired**/**canSubmitted**/canRedo/corrected/rejected/createTime/courseId。**无 total 字段** |
| 作业详情 | `GetV1StudenthomeworkByStudentHomeworkIdDetail` | `{studentHomeworkId}` | title/deadline/content/standardScore/questionTotal/sendTime/状态位/attachments[]/course{} |
| 作业题目 | `GetV1StudenthomeworkByStudentHomeworkIdQuestions` | `{studentHomeworkId}` | `data.shortAnswers[]`：questionId/standardScore/content(HTML)/labels[]/answer{answerId,score,review,content,submitted,attachments[]} |

**必须传 `courseId`**：只传 `{}` → `401 illegal argument`；传 `{courseId}` 不带分页 → 能通但 `page=0/size=0`。

**图片作答题的真实形态**（用户要求的重点）：题干 `content` 是 HTML，图直接嵌在题干里：

```html
<p>右图所示电路，试用节点电压法求得该电路的支路电流。</p>
<p><img id="rich-img-…" class="rich-img" src="https://cstore-public.seewo.com/seewo-pinco/…"/></p>
```

题面信息在图里，纯文本拿不到 → 必须交回人工；答题槽是 `answer.content` + `answer.attachments`。

## L.3 学习任务链路（= 课程材料，实测确认）

| 用途 | action | 参数 | 关键返回 |
| --- | --- | --- | --- |
| 材料列表 | `GetV1CourseByCourseIdStudentMaterialList` | `{courseId}` | `data.courseMaterials[]`：materialId/materialName/materialSize/updateTime/materialSource/**learningRatio**/media{} |
| 材料详情 | `GetV1CourseByCourseIdStudentMaterial` | `{courseId, chapterDirectoryMaterialId}` | `media.downloadUrl`、`resourceTypeDownloadable` |

- **`learningRatio` 就是学习进度**（0~1），列表里直接带 —— 这是「学习任务」的完成度来源。
- 详情接口的 `chapterDirectoryMaterialId` 取列表里的 `materialId` 值即可（实测通过）。
- **不是** `GetV1StudentChapterTaskList`：该 action 在 Web 网关上是**空壳**，对全部 24 门课、`{courseId}` / `{classId}` / 组合参数一律返回 `{}`。
- 教师端接口 `GetV1ChapterdirectoryNodes`、`GetV1ChapterdirectoryMaterialsLearningprogress` 用学生身份调用返回 `6571 没有该课程权限`，学生链路别走这两个。
- **2026-10-20 补充（刷课机制已确认）**：`learningRatio = (maxPageIndex + 1) / 总页数`。
  先 `PostV1CoursewareStudentOpen {resId: media.resId, coursewareId: materialId}`
  拿 `pages[]` 与 `maxPageIndex`，再逐页 `PostV1CoursewareStudentMaxLook
  {coursewareId, maxPageIndex}` 即可推进（PPT/PDF 通用）。此前试的
  `…LearningDurationReport` 只是 H5 心跳，收下但不涨进度。证据见
  `docs/verification/pinco-progress.md`，实现见 `platforms/pinco/progress.py`。

## L.4 本账号实测盘点（2026-10-19 11:40 前后）

- 课程 **24 门**（`pageSize` 服务端封顶 20，需翻页取全），班级 21 个
- 作业共 **13 份**，分布 3 门课；**未过期且未提交 = 0 份**（其余为已提交或已过期）
- 学习任务（课程材料）：**只有「电子技术基础」有**（最初盘点 10 个、现 15 个）PPT/PDF，
  `learningRatio` 起初全为 0（2026-10-20 已按页推进到 100%，见 L.3 补充）
- **3 门课是「幽灵课」**：课程列表里有（`qtq`、两个「如何使用品课教学平台」），
  但作业/材料接口回 `6569 课程不存在`。整课抓取时必须跳过并点名 —— 学生端
  课程列表不保证每门课都还能打开。
- 消息中心 `GetV1MsgcenterStudentMsgs {size,page}` 可用，能拿到「收到新作业」通知与 `extras.studentHomeworkId`

> 样本原文（不入库）：`data/recon/pinco/samples/all_courses_scan.json`、`materials_scan.json`、`homework_samples.json`
