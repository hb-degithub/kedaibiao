# U校园（Unipus）协议规格（clean-room 版）

> **本文的定位**：给**不接触参考实现源码**的开发者照着写 Python 客户端用的协议规格。
> 内容全部是从参考实现 `Yunlk/uai-core` 的源码中抽出的**功能性事实**（端点路径、请求头名、枚举值、算法参数、载荷形状），
> 已刻意改写为独立描述，不复制其代码、注释、文档文本与命名。
>
> - 参考快照：`uai-core@4628e444e8e618767544ddffb27d9e6e20ed97f5`（`main`，v0.1.2）
> - 取证方式：`/tmp` 内一次性只读克隆后读取文本（每次调用后销毁），未执行其任何脚本、未构建、未安装依赖、**未向真实平台发出任何请求**
> - 引用格式：`uai-core@4628e44 src/xxx.rs#L起-L止`
> - 标注约定：**「未验证」**＝源码中无法确认；**「参考实现实测」**＝参考实现的注释声称的线上观测结果，本次调研**未独立复现**；**「推断」**＝本次调研由代码得出的推论
> - ⚠️ 文中所有端点路径、头名、枚举值均为功能性事实，按实现需要逐字保留。

**本规格的目标能力**：登录 → 拉课程（班级/教材）→ 拉任务（单元/分组）→ 拉题目（内容 + 标准答案），并**记录**提交语义（不实现提交）。

---

## 目录

1. [平台与域名](#1-平台与域名)
2. [认证流程](#2-认证流程)
3. [请求签名与加密机制](#3-请求签名与加密机制)
4. [端点表](#4-端点表)
5. [成功码与错误体约定](#5-成功码与错误体约定)
6. [领域模型](#6-领域模型)
7. [题型分类与答案编码](#7-题型分类与答案编码)
8. [提交语义（记录，不实现）](#8-提交语义记录不实现)
9. [限速与风控](#9-限速与风控)
10. [未验证与无法确定项](#10-未验证与无法确定项)
11. [实现者自检：还差什么](#11-实现者自检还差什么)

---

## 1. 平台与域名

平台是 U校园（Unipus）。**同一个业务分散在多个主机上，且各自主机的"成功码"都不一样**，这是最容易出错的地方。

| 主机（基址） | 角色 | 成功码 | 鉴权方式 | 来源 |
| --- | --- | --- | --- | --- |
| `https://uai.unipus.cn` | 门户：用户信息、书架、班级进度、教材激活标记、班级教材详情、考核方案/成绩、按班策略与任务树（`/api/tla/*`） | `code == 1` | 门户 SSO JWT（`Authorization`） | `uai-core@4628e44 src/endpoints.rs#L21`、`#L13` |
| `https://sso.unipus.cn` | 统一登录（密码登录、二维码登录、ticket 换 JWT） | `code == "0"`（字符串） | 无（登录本身） | `src/endpoints.rs#L22`、`#L14` |
| `https://ucontent.unipus.cn` | 内容平台：目录、题目内容、标准答案、分组状态、进度、**提交**、时长上报、媒体资源 | `code == 0` | 自铸 `x-annotator-auth-token` | `src/endpoints.rs#L25`、`#L15` |
| `wss://umcs.unipus.cn` | 二维码登录用的 Socket.IO | 未验证 | 未验证 | `src/endpoints.rs#L31` |
| `wss://ucontent.unipus.cn/unipusiopoint/` | 学习时长上报（engine.io v3） | 不适用（WS） | URL 上带 `uuid`/`token` | `src/endpoints.rs#L34`、`src/api/duration.rs#L253-L256` |
| `wss://speech.unipus.cn/speech/proxy/wss` | 语音评测 WebSocket 主通道 | 不适用 | 签名帧 | `src/api/speech_eval.rs#L89` |
| `wss://speech-eval.unipus.cn/speech/proxy/wss` | 语音评测备用通道 | 不适用 | 同上 | `src/api/speech_eval.rs#L92` |
| `https://zt.unipus.cn/soe/api/csAuth` | 取语音评测签名（免认证） | 未验证 | 无 | `src/api/speech_eval.rs#L95` |
| `https://open-eval.unipus.cn/open_proxy` | 语音评测 HTTP 通道 | 未验证 | 未验证 | `src/api/speech_eval.rs#L98` |

固定常量：

- 门户 SSO 的 `service` 参数固定为 `https://uai.unipus.cn/portal`（`src/endpoints.rs#L28`）。
- ucontent 受保护接口要求的鉴权**头名**是 `x-annotator-auth-token`，**不是** `Authorization`（`src/endpoints.rs#L36-L41`）。
- 内容/答案/进度接口固定使用模式路径段 `default`（`src/endpoints.rs#L43-L44`）。
- 门户 `/api/tla/*` 也在门户域名下；**不要**把它们拼到 ucontent（`src/endpoints.rs#L24`）。

⚠️ **域名纪律**：参考实现专门写了一个「URL 必须以预期基址开头」的断言，起因是历史事故（把门户令牌发到了 ucontent 域名，导致整批结论作废）。实现时建议同样在发请求前断言目标主机（`src/transport.rs#L345-L356`）。

---

## 2. 认证流程

### 2.1 密码登录：端到端步骤

```
① POST https://sso.unipus.cn/sso/0.1/sso/cip/login
   body(JSON): {"service": "https://uai.unipus.cn/portal",
                "username": <加密后的用户名>,
                "password": <加密后的密码>}
   → 成功条件：响应 code 为字符串 "0"（数字 0 也接受）
   → 取 rs.serviceTicket

② GET https://sso.unipus.cn/sso/serviceTicket/validate?ticket=<serviceTicket>&service=https://uai.unipus.cn/portal
   → 成功条件：同上 code == "0"
   → 取 rs.jwt  ← 这就是「门户 JWT」

③ GET https://uai.unipus.cn/api/account/user/info
   header: Authorization: <门户 JWT>
   → 成功条件：code == 1
   → 取 ssoId / school / appUserId / 展示名
```

来源：`src/api/login.rs#L65-L98`（步骤①）、`#L127-L151`（步骤②）、`src/api/user_info.rs#L77-L113`（步骤③）、`src/bin/uai/commands.rs#L264-L278`（三步串起来的顺序）。

**必须遵守的两条工程约束：**

1. **步骤①与步骤②必须共用同一个「带 cookie 存储」的 HTTP 会话。** 参考实现在登录函数注释里明确要求调用方使用带 cookie 罐的客户端，并说明步骤②依赖它（`src/api/login.rs#L65-L68`、`src/transport.rs#L56-L64`）。→ 用 Python 时不要用 `requests.get()` 这种一次性调用，要用 `requests.Session()` / `httpx.Client()`。
2. **用户名与密码必须按第 3.1 节加密后再放进 body**，且输出是**大写十六进制**；大小写写错会被服务端当作密码错误（`src/api/login.rs#L76-L86`、`src/crypto.rs#L82-L90`）。

**步骤③ 的字段读取规则（关键，容易错）：**

- `open_id` **只能取 `ssoId`**；**不能取 `appUserId`**（`src/api/user_info.rs#L1-L16`、`#L96-L97`）。
- `school` 在响应里可能是**数字**（参考实现注释示例为 `1000`），必须能当文本取出来；它是考核类接口 `u-school` 头的值（`src/api/user_info.rs#L47-L71`、`#L99-L100`）。
- 展示名可按 `name` / `nickName` / `username` / `userName` / `phone` 顺序找第一个非空字符串，可作可读性降级（`src/api/user_info.rs#L90-L94`）。
- 参考实现用的是「在整棵 JSON 里递归找第一个非空候选键」的策略，因为响应外层结构在不同版本间变过（`src/api/user_info.rs#L26-L45`）。**登录这条链之外不要照搬这种宽松解析**（第 6 节会说明宽松解析在别处会出事）。

### 2.2 会话里要存什么

| 字段 | 用途 | 作用域 |
| --- | --- | --- |
| `portal_token`（门户 JWT） | `Authorization` 头，打门户接口 | `uai.unipus.cn` |
| `open_id`（= `ssoId`） | ① 铸 annotator token；② 出现在 ucontent 进度 URL 路径与提交体 `openId` | `ucontent.unipus.cn` |
| `display_name` | 仅展示 | — |
| `school` | `u-school` 头，考核类/任务树接口必需 | `uai.unipus.cn` |
| `app_user_id`（= `appUserId`） | 仅作标识；**绝不能**当 open_id 用 | — |

来源：`src/session.rs#L19-L39`、`#L41-L69`。

会话缓存：参考实现把会话 JSON 写进系统临时目录的 `uai-session.json`（`src/bin/uai/cli.rs#L583-L604`、`src/bin/uai/main.rs#L19-L23`）。凭据只从环境变量 `UAI_USER` / `UAI_PASS` 读，不走命令行参数（`src/bin/uai/main.rs#L19-L22`、`src/bin/uai/cli.rs#L570-L576`）。→ 本项目实现时建议沿用「凭据走环境变量/密钥库、会话缓存在临时目录、日志绝不打印令牌」这条纪律。

### 2.3 有几套令牌，为什么不能混

| 令牌 | 生成方 | 打到哪 | 放进哪个头/字段 | 有效期 |
| --- | --- | --- | --- | --- |
| 门户 JWT | SSO 服务端签发 | 门户系列接口 | `Authorization: <jwt>` | **未验证**（源码无过期处理、无刷新逻辑） |
| annotator token（JWT） | **客户端自己铸**（HS256） | ucontent 受保护接口 | `x-annotator-auth-token: <token>` | 铸造时写死 `exp = 当前毫秒 + 31,536,000,000`（≈1 年），每次用前现铸、不落盘 |
| 宿主身份四头 | 从会话取值拼装 | 门户 `/api/aca/*` 与 `/api/tla/*` 部分接口 | 见第 3.4 节 | 随会话 |

来源：`src/session.rs#L1-L12`、`#L54-L57`；`src/annotator.rs#L53-L54`、`#L60-L82`；`src/transport.rs#L103-L133`。

**不能混用的原因（功能性事实）：**

- ucontent 的**受保护**接口（进度/答案/提交/分组状态）不认门户 JWT：用 `Authorization` 打过去会失败，而且失败形态是 **401 + HTML 登录页**，不是 JSON（`src/endpoints.rs#L36-L41`、`src/transport.rs#L12-L17`）。
- 反过来，ucontent 的**目录/内容**接口**不校验鉴权**：发错头也返回 `code:0`，所以"只测目录/内容"发现不了鉴权错误（`src/endpoints.rs#L39-L40`、`src/api/content.rs#L22-L25`）。
- 结论：**必须按"目标接口属于哪个应用"选择头**；实现时建议把「域名 + 头名」绑定成一个不可自由组合的参数，而不是随手传 token。

### 2.4 二维码登录（可选，目标能力不需要）

流程骨架：`POST /sso/7.0/sso/code`，body `{deviceId, service}` → 取 `rs.content`（二维码内容）→ 轮询 UMCS Socket.IO 拿 ticket → 走与密码登录相同的 `serviceTicket/validate` → 再调一次 `GET /sso/7.0/sso/add/cookie?ticket=&service=` 补充 cookie（部分接口依赖 cookie 而不只是 JWT）。

来源：`src/api/login.rs#L100-L125`、`#L153-L163`。UMCS 侧的 Socket.IO 报文细节**未验证**（本规格未读、源码中亦只有地址常量）。

---

## 3. 请求签名与加密机制

本章是本规格最需要精确的部分。**共四套机制，互不通用**，参数全部给出，不需要猜。

### 3.1 登录字段加密（用户名 / 密码）

| 项 | 值 | 来源 |
| --- | --- | --- |
| 算法 | AES-128-CBC | `src/crypto.rs#L82-L90` |
| 填充 | PKCS7 | `src/crypto.rs#L84-L88` |
| 密钥 | 16 字节固定值，十六进制 `8AD70B641C024C7ADA2ECD082EC0334F` | `src/crypto.rs#L27-L30` |
| IV | 16 字节固定值，十六进制 `0102030405060708090A0B0C0D0E0F10`（即 0x01…0x10 递增） | `src/crypto.rs#L32-L35` |
| 明文 | 用户名/密码的 UTF-8 字节 | `src/crypto.rs#L85-L89` |
| 输出编码 | **大写十六进制**字符串（长度必为 32 的整数倍） | `src/crypto.rs#L84`、`#L124-L135` |
| 随机性 | **无**：IV 固定，同一输入必须产出同一密文（确定性） | `src/crypto.rs#L137-L143` |
| 适用范围 | `username` 与 `password` 两个字段各自独立加密后放 body | `src/api/login.rs#L76-L86` |

⚠️ 常见坑：用随机 IV 或输出小写十六进制。参考实现的注释明确记载：大小写写错会被服务端当成密码错误（`src/crypto.rs#L84`）。另需注意 PKCS7 会把长度补齐到 16 字节块的整数倍——不要事先去填充。

### 3.2 annotator token（`x-annotator-auth-token`）：客户端自铸的 HS256 JWT

这是 ucontent 受保护接口的唯一凭据，**由客户端本地生成**，算法参数完全固定：

| 项 | 值 | 来源 |
| --- | --- | --- |
| 类型 | JWT，三段式 `header.payload.signature` | `src/annotator.rs#L72-L81` |
| 签名算法 | HS256（HMAC-SHA256） | `src/annotator.rs#L44-L48`、`#L76-L79` |
| header | `{"alg":"HS256","typ":"JWT"}`，其 base64url 结果固定为 `eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9` | `src/annotator.rs#L72`、`#L160-L161` |
| 签名密钥 | ASCII 字符串 `example-annotator-signing-secret-0001` | `src/annotator.rs#L47-L48` |
| payload 字段与顺序 | `open_id`, `name`, `email`, `administrator`, `exp`, `iss`, `aud` | `src/annotator.rs#L85-L93` |
| payload 固定值 | `name` 与 `email` 为空串；`administrator` 为 `false`；`iss` = `example-annotator-issuer`；`aud` = `edx.unipus.cn` | `src/annotator.rs#L44-L51`、`#L85-L93` |
| `exp` | 毫秒时间戳（Unix epoch 毫秒）+ `31536000000` | `src/annotator.rs#L53-L54`、`#L65-L69` |
| `open_id` | 必填且非空，来自登录后的 `ssoId`；空值必须直接报错 | `src/annotator.rs#L60-L63` |
| 签名输入 | `base64url(header) + "." + base64url(payload)` 的 ASCII 字节 | `src/annotator.rs#L72-L79` |
| base64 变体 | **base64url 且无填充**（字母表含 `-`、`_`，不输出 `=`） | `src/annotator.rs#L111-L134` |
| 签名字段编码 | HMAC 结果再做 base64url（无填充） | `src/annotator.rs#L79` |
| JSON 转义 | `open_id` 需做最小 JSON 转义（`"`、`\`、`\n`、`\r`、`\t`） | `src/annotator.rs#L95-L109` |

⚠️ **`payload` 的字段顺序会影响签名结果**，必须按上表顺序手工构造（用 Python 的 `json.dumps(..., separators=(",", ":"))` 加固定顺序的 dict 即可，但注意别用会重排键的实现）。

**关于这几个"公开常量"的性质**（参考实现的说明，非本调研的独立结论）：`iss`、`aud`、签名密钥都取自平台前端公开 JS 包中的常量，属于"客户端自己签发"的令牌，服务端只校验签名与 `open_id`，不是按账号下发的凭据；`open_id` 才是真正的账号标识，必须来自已登录会话（`src/annotator.rs#L3-L36`）。→ **本调研未独立核对前端 JS 包**，该"公开常量"的说法属参考实现自述，见第 10 节。

### 3.3 内容/答案密文解密（`unipus.` 前缀）

响应外壳（两个接口结构相同，**只有密文那一个键名不同**）：

| 接口 | 密文字段 | 来源 |
| --- | --- | --- |
| `GET /course/api/v3/content/…` | `content` | `src/question/parse.rs#L35-L36`、`#L93-L96` |
| `GET /course/api/v3/answer/…` | **`data`**（不是 `content`） | `src/question/parse.rs#L38-L54`、`#L98-L101` |

解密规则（**照此实现即可**）：

1. 取响应里的 `k` 字段（**短数字字符串**；参考实现举的例子形如 `20260921`、`20260923`；它的生成规则**未验证**）（`src/crypto.rs#L21-L22`、`src/question/parse.rs#L82`）。
2. 若密文串**不以 `unipus.` 开头**，则它是**明文**，原样返回，不要尝试解密（`src/crypto.rs#L24-L25`、`#L42-L45`）。
3. 去掉前缀后，剩余部分是**十六进制**文本；解码失败要报错，不要产出乱码（`src/crypto.rs#L54-L55`、`#L110-L115`）。
4. 密钥 = ASCII 字符串 `1a2b3c4d` 直接拼接 `k` 的文本，**长度必须恰好 16 字节**（参考实现把长度不对当硬错误）（`src/crypto.rs#L21-L22`、`#L47-L52`、`#L103-L108`）。
5. 算法是 **AES-128-ECB**：逐 16 字节分组解密，**没有 IV**（`src/crypto.rs#L63-L71`）。
6. 密文字节长度必须非空且是 **16 的整数倍**（`src/crypto.rs#L56-L61`、`#L117-L122`）。
7. 填充处理是**手工去掉尾部 `\0`**（即 ZeroPadding，不是 PKCS7）：解密后从末尾不断弹出 `0x00`，直到最后一个字节非 0（`src/crypto.rs#L7`、`#L73-L76`）。
8. 结果必须是合法 UTF-8（`src/crypto.rs#L78-L79`）。

⚠️ 参考实现特别强调：**不要把这套与 3.1 的登录加密"统一"** —— 一个没有 IV、一个固定 IV；一个去尾部 `\0`、一个是 PKCS7，方向恰好相反（`src/crypto.rs#L3-L11`）。

**容错读取（可选，但建议保留）**：若按名字取不到密文字段，可以在响应顶层再扫一遍「以 `unipus.` 开头的字符串值」作为兜底；取不到才报错（`src/question/parse.rs#L68-L81`）。

### 3.4 门户"微应用宿主"请求头（考核/任务树类接口必需）

`/api/aca/*` 与 `/api/tla/*` 的一部分接口**只带 `Authorization` 会被拒**，必须同时带四个头：

| 头名 | 取值 | 来源 |
| --- | --- | --- |
| `u-app-id` | `116`（固定常量） | `src/endpoints.rs#L100-L101`、`src/endpoints.rs#L106` |
| `u-platform` | `2`（网页端；枚举为 移动=1 / PC=2 / 平板=3） | `src/endpoints.rs#L103-L104`、`#L107` |
| `u-school` | 用户信息里的 `school` | `src/transport.rs#L129-L132` |
| `u-openid` | `ssoId`（即 `open_id`） | `src/transport.rs#L131-L132` |
| `Authorization` | 门户 JWT | `src/transport.rs#L118` |

四项缺任何一项都会被服务端当成「认不出是哪个应用」，返回 `{"code":6011,"msg":"feature denied"}`（`src/endpoints.rs#L89-L98`、`src/transport.rs#L103-L122`）。

### 3.5 其他加密/签名（本规格目标能力**不需要**，仅登记）

- **语音评测签名**：`sig = SHA1(applicationId + secret + timestamp)`，`timestamp` 是**秒级**字符串；`applicationId` 常量 `138000000000001`，`secret` 常量 `example-speech-eval-secret-0000000001`（参考实现称其为前端包里的明文常量）；认证帧里带 `alg: "sha1"`。来源：`src/api/speech_eval.rs#L29-L36`、`#L89-L107`、`#L338-L345`、`#L357-L375`。
- **学习时长 WebSocket**：engine.io v3，客户端需每 20 秒主动发 ping（报文 `"2"`），连不上时 URL 需带 `EIO=3&transport=websocket&uuid=<open_id>&token=<token>`。来源：`src/api/duration.rs#L118-L130`、`#L247-L256`、`#L535-L556`。

---

## 4. 端点表

路径全部逐字给出。**未标"完整 URL"的，按第 1 节的基址拼接。**

### 4.1 SSO（基址 `https://sso.unipus.cn`）

| 方法 | 路径 | 用途 | 关键请求字段 | 关键响应字段 | 来源 |
| --- | --- | --- | --- | --- | --- |
| POST | `/sso/0.1/sso/cip/login` | 密码登录 | `service`、`username`(加密)、`password`(加密) | `code`、`rs.serviceTicket`、`message`/`msg` | `src/endpoints.rs#L48-L49`、`src/api/login.rs#L76-L98` |
| POST | `/sso/7.0/sso/code` | 申请二维码 | `deviceId`、`service` | `code`、`rs.content` | `src/endpoints.rs#L51-L52`、`src/api/login.rs#L100-L125` |
| GET | `/sso/serviceTicket/validate?ticket=<t>&service=<s>` | ticket 换 JWT | query: `ticket`、`service` | `code`、`rs.jwt` | `src/endpoints.rs#L54-L55`、`src/api/login.rs#L127-L151` |
| GET | `/sso/7.0/sso/add/cookie?ticket=<t>&service=<s>` | 补 cookie（二维码链路） | query: `ticket`、`service` | 未解析（只取成功与否） | `src/endpoints.rs#L57-L58`、`src/api/login.rs#L153-L163` |

### 4.2 门户（基址 `https://uai.unipus.cn`，成功码 `1`）

| 方法 | 路径 | 用途 | 关键请求字段 | 关键响应字段 | 鉴权 | 来源 |
| --- | --- | --- | --- | --- | --- | --- |
| GET | `/api/account/user/info` | 当前用户 | — | `ssoId`、`school`、`appUserId`、`name`/`nickName` | JWT | `src/endpoints.rs#L62-L63`、`src/api/user_info.rs#L77-L113` |
| GET | `/api/cmgt/course/my/bookshelf` | 门户书架（**不含进度**） | — | 班级列表 → 教材（`instanceId`/`resourceId`/`activation`） | JWT | `src/endpoints.rs#L65-L66`、`src/api/bookshelf.rs#L111-L125` |
| GET | `/api/cmgt/course/getCourseListByStudent` | **班级×教材真实进度** | — | `value.courseList[]` | JWT | `src/endpoints.rs#L68-L69`、`src/api/course_list.rs#L111-L125` |
| POST | `/api/product/course/courseActiveFlag` | 教材激活标记（只读，不激活） | `resourceId`（**数字就发数字，否则发字符串**） | 嵌套布尔（`active`/`isActive`/`activated`/`isActivated`/`courseActiveFlag`） | JWT | `src/endpoints.rs#L71-L72`、`src/api/active_flag.rs#L54-L88` |
| GET | `/api/cmgt/course/getCourseResourceInfoById/{courseResourceId}` | 单个"班级教材"详情 | 路径参数 | `classId`、`courseId`、`strategyId`、`courseInstanceId` | JWT | `src/endpoints.rs#L74-L81`、`#L204-L207`、`src/api/assessment.rs#L384-L411` |
| POST | `/api/aca/plan/detail` | 考核方案（权重/时长标准/记分周期） | `courseInstanceId`（**数字 courseId**）、`classId`（字符串） | 逐考核项与逐教材标准 | JWT + **宿主四头** | `src/endpoints.rs#L83-L84`、`src/api/assessment.rs#L376-L380`、`#L455-L490` |
| POST | `/api/aca/achievement/queryUserScore` | 综合成绩 | 同上 | 各考核项得分、逐教材 | JWT + **宿主四头** | `src/endpoints.rs#L86-L87` |
| POST | `/api/tla/courseStudyStrategy/detail` | **按班**策略：逐单元必修任务 + 及格线 | `id`(**必填**，等于 `strategyId`)、`strategyId`、`courseResourceId`、`courseId`、`resourceId`、`courseInstanceId` | `value.courseUnitStrategyList[]`（`unitId`/`unitName`/`passScore`/`requiredTask[]`） | JWT + **宿主四头** | `src/api/class_required.rs#L77-L78`、`#L121-L214` |
| GET | `/api/tla/learningDetail/chapters/unitTaskSituation?id=<courseResourceId>&nodeId=<单元>` | **按班**任务树，叶子带 `required` | query: `id`、`nodeId` | `value.list[]` 递归，叶子 `role=="group"` 的 `required` | JWT + **宿主四头** | `src/api/class_required.rs#L16-L27`、`#L217-L258` |

⚠️ `/api/tla/*` 两条端点的路径常量定义在业务模块里而非端点常量文件，实现时别再抄错（`src/api/class_required.rs#L78`、`#L218`）。

### 4.3 ucontent（基址 `https://ucontent.unipus.cn`，成功码 `0`）

| 方法 | 路径 | 用途 | 关键请求字段 | 关键响应字段 | 鉴权 | 来源 |
| --- | --- | --- | --- | --- | --- | --- |
| GET | `/course/api/course/{courseId}/default` | 教材目录 | 路径：`course-v1:` 或 `course-v2:` 都可 | `course`（**字符串化 JSON**）、可含 `course-v1:` 资源 ID | 不校验 | `src/endpoints.rs#L113-L117`、`#L154-L157`、`src/api/catalog.rs#L156-L180` |
| GET | `/course/api/v3/content/{courseId}/{groupId}/default` | 分组题目内容（加密） | 路径 | `code`、`k`、`content`（`unipus.<hex>`） | 不校验 | `src/endpoints.rs#L119-L124`、`#L159-L162` |
| GET | `/course/api/v3/answer/{courseId}/{groupId}/default` | 标准答案（加密） | 路径 | `code`、`k`、**`data`**（`unipus.<hex>`） | annotator | `src/endpoints.rs#L126-L127`、`#L164-L167` |
| POST | `/course/api/v3/newExploration/submit` | **唯一的写接口** | 见第 8 节 | 见第 8 节 | annotator | `src/endpoints.rs#L129-L130`、`#L194-L197` |
| GET | `/api/mobile/user_module/{courseId}/{groupId}/progress/v2` | 单任务状态（真源） | 路径 | `data.first_pass`、`data.state`、`data.flowStrategy`、`data.strategy` | annotator | `src/endpoints.rs#L132-L136`、`#L169-L172` |
| GET | `/api/mobile/user_module/{courseId}/{groupId}-{lastSubmit}` | 作答快照 | 路径：**`lastSubmit` 必填** | `data.state.__EXTEND_DATA__.__SUBMIT_INFO__` 等 | annotator | `src/endpoints.rs#L174-L180`、`src/api/group_state.rs#L205-L213` |
| GET | `/course/api/v2/course_progress/{courseId}/{openId}/default` | 整课**单元级**进度 | 路径 | `rt.units.<uN>.strategies.required`、`min_score_pct` | annotator | `src/endpoints.rs#L138-L139`、`#L182-L185` |
| GET | `/course/api/v2/course_progress/{courseId}/tasks/{openId}/default?tasks=<逗号分隔分组ID>` | **分组级**必修（`leafs`） | query `tasks` **必须逗号分隔** | `rt.leafs.<gid>.strategies.required`、`min_score_pct` | annotator | `src/endpoints.rs#L141-L144`、`#L187-L192` |
| POST | `/course/api/v3/voiceEvaluation` | 口语评分上报（服务端不评测） | `block_id`、`course_id`、`score_details` | 未解析 | annotator | `src/endpoints.rs#L146-L147`、`src/api/media.rs#L56-L87` |
| POST | `/media/user_resource/fetch` | 媒体资源登记（**未接入**） | 未验证 | 未验证 | annotator | `src/endpoints.rs#L149-L150`、`src/api/media.rs#L98-L108` |
| GET | `/media/user_resource/cms/token` | 取上传 token（**未接入**） | — | 未验证 | annotator | `src/api/media.rs#L89-L96` |

**URL 拼接的三条硬规则**（都是踩过的坑）：

1. `courseId`（`course-v1:`/`course-v2:`）**不能做 URL 编码**：冒号必须原样出现，否则服务端返回空壳 `code:2`（`src/endpoints.rs#L113-L116`、`#L222-L229`）。→ Python 里别用会自动转义的拼接方式，注意 `+` 也需保持原样。
2. 快照 URL 是把 `lastSubmit` 用 **`-`** 直接接在 `groupId` 后面（`src/endpoints.rs#L174-L180`、`#L231-L236`）。
3. 批量进度的 `tasks` 参数**必须逗号分隔**；传空格、JSON 数组、`all`、`*` 都会**静默返回空 `leafs` 且不报错**（`src/endpoints.rs#L238-L242`、`src/api/progress.rs#L22-L28`）。

### 4.4 WebSocket（登记，非目标能力）

| 地址 | 用途 | 关键参数 |
| --- | --- | --- |
| `wss://ucontent.unipus.cn/unipusiopoint/?EIO=3&transport=websocket&uuid=<open_id>&token=<token>` | 学习时长上报 | `EIO=3`、`uuid`、`token`（`src/api/duration.rs#L247-L256`） |
| `wss://speech.unipus.cn/speech/proxy/wss` | 语音评测 | 签名帧（`src/api/speech_eval.rs#L89-L99`） |

---

## 5. 成功码与错误体约定

### 5.1 三套成功码（**不统一，必须按应用分别判定**）

| API 家族 | 成功判定 | 说明 | 来源 |
| --- | --- | --- | --- |
| SSO（`sso.unipus.cn`） | `code == "0"`（**字符串**） | 源码同时接受数字 `0`；`1` 会被判失败 | `src/api/login.rs#L42-L54`、`#L175-L185` |
| 门户（`uai.unipus.cn`） | `code == 1`（数字） | `0` 会被判失败 | `src/transport.rs#L299-L305` |
| ucontent（`ucontent.unipus.cn`） | `code == 0`（数字） | `1` 会被判失败 | `src/transport.rs#L294-L297` |
| 内容/答案密文外壳 | `code != 0` 即失败（取不到 `code` 视为不失败） | 失败时把 `message`/`msg` 一并报出 | `src/question/parse.rs#L60-L67` |

实现建议：写三个独立的 `ok_xxx(body) -> bool`，**不要**写一个共用判定。

### 5.2 错误体结构与消息提取

- 消息字段按 `message` → `msg` → `error` → `errMsg` 顺序取第一个非空字符串，都没有则用兜底文案（`src/transport.rs#L307-L317`）。
- **HTTP 状态非 2xx 时**：不解析 JSON，直接带着响应正文摘要报错；若正文看起来是 HTML（以 `<` 开头或含 `<html`），应提示"多半是鉴权失败被重定向到登录页"——这正是 401 的真实形态（`src/transport.rs#L86-L101`、`#L281-L292`）。
- 值得重试的错误类别：参考实现只把**网络层**错误标为可重试，业务码错误不重试（限流码除外，见第 9 节）（`src/error.rs#L104-L107`）。

### 5.3 已知业务码

| 码 | 含义/触发条件 | 来源 |
| --- | --- | --- |
| `600001` / `600002` | **限流**（唯二被当作限流的码） | `src/transport.rs#L319-L322` |
| `6011` | `feature denied`：`/api/aca/*`（及任务树）缺宿主四头或 `u-school` 不对 | `src/endpoints.rs#L89-L98` |
| `300100` | 提交体里 `isCompleted` 与 `thirdPartyJudges` 长度不一致（参考实现记录：往往是 `thirdPartyJudges` 发成空数组） | `src/api/submit.rs#L23-L33`、`#L587-L612` |
| `2` + `runtime error: index out of range` | `thirdPartyJudges` 比 `isCompleted` 长/短导致的越界 | `src/api/submit.rs#L23-L33` |
| `2`（空壳） | 目录 URL 里 `courseId` 被 URL 编码（冒号丢失） | `src/endpoints.rs#L113-L116` |
| `1` + `data: null` + `msg: "data not exist"` | 请求作答快照时少了 `lastSubmit` | `src/endpoints.rs#L174-L180` |
| `6` | 进度接口的 `courseId` 传了 `course-v1:` 资源 ID（需要实例 ID） | `src/annotator.rs#L31-L36`（参考实现实测） |

---

## 6. 领域模型

### 6.1 层级与标识字段

```
教学班 class
  ├─ classId          字符串（进度接口的键之一）
  ├─ name             班级名（展示用；另有 className 通常是行政班名）
  ├─ id               数字，即「数字 courseId」
  └─ courseResourceList[]  该班下的教材
        └─ 班-教材绑定
              ├─ id                数字 → 门户"班级教材ID" courseResourceId（任务页 URL 里的那个）
              ├─ resourceId        形如 course-v2:unipus+…（ucontent 的课程资源 ID）
              ├─ instanceId        形如 course-v2:…（实例 ID）
              ├─ strategyId        数字，该(班,书)的策略 ID
              ├─ finishPointNum / totalPointNum   该班该书的完成点数/总点数
              └─ name / resourceName
教材/课程实例（ucontent 视角）
  ├─ 实例 ID   course-v2:…   ← 进度、内容、提交都用它
  └─ 资源 ID   course-v1:…   ← 目录可用，进度/提交不行
        └─ 单元 unit   id 形如 u5，name
              └─ 分组 group   id 形如 u1g2，name，base（逗号分隔标签），role=="group"
                    └─ 题目 item  instanceId（内容接口的 id，19 位十进制字符串）
                          ├─ type / replyType / category
                          └─ children[]  子题数组（内容侧只用到长度）
```

来源：`src/api/course_list.rs#L53-L80`、`#L159-L217`；`src/api/bookshelf.rs#L30-L51`；`src/api/catalog.rs#L51-L124`、`#L221-L261`；`src/question/parse.rs#L125-L170`。

### 6.2 各标识符从哪来（实现时按此取数）

| 标识 | 取值位置 | 大小写/类型注意 | 来源 |
| --- | --- | --- | --- |
| `open_id` | `/api/account/user/info` 的 `ssoId` | **不是** `appUserId` | `src/api/user_info.rs#L96-L104` |
| `school` | 同上的 `school` | **可能是数字**，需转文本 | `src/api/user_info.rs#L47-L71`、`#L99-L100` |
| 实例 ID `course-v2:…` | 书架/进度接口的 `instanceId`（进度接口也可能叫 `courseInstanceId`） | 字符串；服务端对类型敏感 | `src/api/course_list.rs#L184-L190` |
| 资源 ID `course-v1:…` | 书架的 `resourceId`；**缺失时从目录响应里回收** | 可能只藏在封面 `imgUrl` 里 | `src/api/bookshelf.rs#L10-L14`、`src/api/catalog.rs#L263-L292` |
| `courseResourceId` | **教材节点自己的 `id`**（数字） | 不是 `resourceId`，也不是班级节点 `id` | `src/api/course_list.rs#L70-L75`、`#L200-L204` |
| `strategyId` | 进度接口的 `strategyId`（数字） | 按(班,书)绑定 | `src/api/course_list.rs#L78-L79`、`#L210` |
| 分组 ID `u1g2` | 目录里 `role=="group"` 节点的 `url` 字段 | 目录里**没有必修标记** | `src/api/catalog.rs#L221-L248` |
| 单元 ID `u5` | 目录里 `role=="unit"` 节点的 `url` 字段 | — | `src/api/catalog.rs#L182-L207` |
| 题目 `instanceId` | 解密后内容项里的 `id` | **19 位十进制**，必须按字符串/整数处理，**不能经浮点**（超过 2^53） | `src/question/parse.rs#L1-L12`、`#L113-L123`、`#L153-L154` |

⚠️ **精度是硬约束**：参考实现记载，前端 JS 因为 `JSON.parse` 走双精度把 19 位 ID 舍入过（例：`…042765` 变 `…042800`）（`src/question/mod.rs#L56-L59`、`src/question/parse.rs#L3-L12`）。Python 的 `json` 保留任意精度整数，但**一旦经过 `float()` 就会损坏**——实现时全程用 `str`/`int`。

### 6.3 三条"账"（进度的口径，影响"拉任务"的设计）

| 账 | 含义 | 决定方 | 权威接口 |
| --- | --- | --- | --- |
| **必修** | 本班要求完成哪些任务 | **按班级**（同一教材挂在不同班级，必修集可完全不同） | `rt.leafs.<gid>.strategies.required`（ucontent）；或门户按班端点 `/api/tla/*` |
| **已完成** | 该任务是否做过 | 按**教材实例**（跨班共享，会串号） | `first_pass.datetime` 是否为空 |
| **计分点** | 该任务得分是否达标 | **按班级**，由考核方案计算 | 门户 `/api/cmgt/course/getCourseListByStudent` 的 `finishPointNum/totalPointNum` |

来源：`AGENTS.md#L55-L66`（本报告不引用其文字，仅取事实）；`src/api/course_list.rs#L24-L44`；`src/api/group_state.rs#L8-L27`；`src/api/progress.rs#L1-L37`。

**对"拉任务"的实现结论（推荐路径）：**

1. **目录**给层级与分组 ID（1 次请求）。
2. **必修集**取 `leafs`：调 tasks 端点时**必须显式带上目录里收集到的全部分组 ID（逗号分隔）**；`leafs` 只在这个端点里有，单元级端点没有（`src/api/progress.rs#L10-L20`、`#L252-L275`）。
3. ⚠️ **ucontent 的 `leafs` 按 (实例, openId) 记账、不随班级变**，因此同一教材在多个班时会给出"合并"的口径；要拿**按班**口径必须用门户的 `/api/tla/learningDetail/chapters/unitTaskSituation` 或 `/api/tla/courseStudyStrategy/detail`（`src/api/class_required.rs#L1-L27`、`src/api/targets.rs#L1-L44`）。
4. ⚠️ 若 `leafs` 为空，**必须当成"信息缺失"而不是"没有必修"**，不要据此提交或统计（`src/api/progress.rs#L127-L130`、`src/api/targets.rs#L41-L44`）。
5. 单元级的 `state.pass` 与 tasks 端点的 `state.pass` **都不可信**（参考实现实测：对从未做过的教材也回 `pass:1`）；核算完成度只能用门户的 `finishPointNum/totalPointNum`（`src/api/progress.rs#L22-L37`）。

### 6.4 各主接口的响应结构要点

- **书架**：根可能是 `data` 或 `value`；班级列表可能在 `classList`/`classes`/`courseList`；教材节点判据是「有 `instanceId` 或 `resourceId`」；`activation` 字段**数字 `0` 表示已激活**（与命名直觉相反），也接受布尔/字符串形式（`src/api/bookshelf.rs#L92-L109`、`#L127-L214`）。
- **班级进度**：必须走到 `value.courseList[]`；每个班级节点取 `classId`、`name`/`className`、`id`；教材取 `finishPointNum`、`totalPointNum`、`instanceId`/`courseInstanceId`、`name`/`resourceName`、`id`、`resourceId`、`strategyId`；**缺失 `instanceId` 或 `classId` 的条目要丢弃**（避免跨班混淆）（`src/api/course_list.rs#L127-L223`）。
- **目录**：`course` 字段是**字符串化 JSON**，需要二次解析；节点用 `role` 区分（`unit`/`group`），ID 取 `url` 字段；同一单元 ID 可能重复出现，需要合并去重（`src/api/catalog.rs#L163-L219`）。分组节点的可见字段只有 `base`/`name`/`ref-out`/`role`/`tab_type`/`url` 一类，**没有 `required`、没有 `flowStrategy`**（`src/api/catalog.rs#L17-L26`）。
- **内容**：见第 7.1 节（两套 schema）。
- **分组状态**（`progress/v2`）：`data.first_pass.datetime`（首次通过时间，`null` = 从未做过；注意 `first_pass` 对象**总是存在**）、`data.state.lastSubmit`、`data.state.state`（`1` 只表示"服务端有痕迹"，**不是班级完成度**）、`data.state.score`（形如 `[1,1,1,0]` 或 `[]`）、`data.state.score_pct`（0~1）、`data.flowStrategy.required`（任务级必修）、`data.strategy.task_mini_score_pct`（及格门槛，参考实现实测只有 `0` 与 `0.6` 两种取值）（`src/api/group_state.rs#L39-L58`、`#L143-L173`）。

---

## 7. 题型分类与答案编码

### 7.1 内容接口的两套 schema（**先看 ID 前缀**）

| 传入的 `courseId` | 解密后顶层 | 元素形状 | 用途 |
| --- | --- | --- | --- |
| `course-v1:`（资源 ID） | **对象**，键形如 `questions:questions` | — | 界面预览 |
| `course-v2:`（实例 ID） | **数组** | `{"id": …, "content": "<JSON 字符串>"}` | **取题目、提交都要用这条** |

来源：`src/endpoints.rs#L119-L124`、`src/api/content.rs#L1-L25`、`src/question/parse.rs#L13-L20`。

解析实现只需读下列字段（这些是参考实现实际读取的）：

- 每个内容项的 `id` → 题目 `instanceId`。
- 每个内容项的 `content`：可能是**字符串化 JSON**，也可能是对象；解析后为 `inner`。
- `inner.children`（数组）→ **子题数量**（只取长度）。
- 题型判别字段：`inner.replyType` 优先、`item.replyType` 兜底；`question_type` 取 `item.type` 优先、`inner.type` 兜底；`category` 取 `inner.category` 优先、`item.category` 兜底。
- `inner.contents`（数组）→ **媒体项**：只把 `type == 1`（音频）与 `type == 2`（视频）算媒体；参考实现实测还见过 `4`（正文）、`5`（词汇条目）、`6`（单词卡）（`src/question/parse.rs#L29-L33`、`#L172-L211`）。

⚠️ 除上述字段外，**解密后内容的内部结构（每个子题有哪些字段、选项如何表示）在源码中未被读取**，因此本规格无法给出 —— 见第 10 节 U1、第 11 节。

### 7.2 题型总表（13 个类型名；**顺序即优先级**）

分派顺序：先判"学习题"，再按注册表顺序匹配，最后落到兜底"客观题"（`src/question/mod.rs#L278-L311`）。

| # | 类型名 | 判定条件（`reply_type` = `replyType`，`question_type` = `type`） | 贡献项数 | 来源 |
| --- | --- | --- | --- | --- |
| 0 | `study`（学习题） | `question_type == "video-popup"` → **不是**学习题；否则 `category == 0` → 是；`question_type` 为 `discussion`/`rich-text-read` → 是；`child_count == 0` 且 `reply_type` 为空 → 是（纯内容块） | **0** | `src/question/mod.rs#L87-L108`、`src/question/study.rs#L41-L48` |
| 1 | `role-play` | `question_type` ∈ {`role-play`, `roleplay`} | **1**（与子题数无关） | `src/question/role_play.rs#L47-L54` |
| 2 | `oral-state` | `question_type == "oral-state"` | **1** | `src/question/oral_state.rs#L32-L45` |
| 3 | `oral` | `reply_type == "record"`，或 `question_type` ∈ {`basic-oral`, `paragraph-follow`, `oral-personal-state`, `video-dub`, `oral-aloud`, `ai-spoken`, `sentencerecord`, `paragraphrecord`, `paragraphrecord2`, `video_dubbing`}，或 `question_type` 含 `record`/`oral` | `max(child_count, 1)` | `src/question/mod.rs#L148-L166`、`src/question/oral.rs#L36-L49` |
| 4 | `upload` | `question_type == "multiFileUpload"` 或 `reply_type` ∈ {`multiFileUpload`, `multimedia_upload`} | `max(child_count, 1)` | `src/question/mod.rs#L206-L211`、`src/question/upload.rs#L37-L40` |
| 5 | `subjective` | `reply_type == "text-area"` | `max(child_count, 1)` | `src/question/mod.rs#L202-L204`、`src/question/subjective.rs#L54-L56` |
| 6 | `single-choice` | `reply_type == "singlechoice"` | `max(child_count, 1)` | `src/question/single_choice.rs#L31-L33` |
| 7 | `multi-choice` | `reply_type == "multichoice"` | `max(child_count, 1)` | `src/question/multi_choice.rs#L31-L33` |
| 8 | `dropdown` | `reply_type` ∈ {`dropdownSelection`, `dropdownchoic`, `dropdown`}（注意服务端真实值是 `dropdownSelection`，另有少一个 `n` 的历史拼法） | `max(child_count, 1)` | `src/question/dropdown.rs#L21-L32` |
| 9 | `fill-blank` | `reply_type` ∈ {`fillblank`, `fillblankScoop`} | `max(child_count, 1)` | `src/question/fill_blank.rs#L34-L36` |
| 10 | `banked-cloze` | `reply_type` ∈ {`bankedcloze`, `material-banked-cloze`}，或 `question_type` 含 `bankedcloze`/`banked-cloze` | `max(child_count, 1)` | `src/question/banked_cloze.rs#L22-L28` |
| 11 | `sequence` | `reply_type == "sequence"`，或 `question_type == "sequence"`，或含 `sequence` | `max(child_count, 1)` | `src/question/sequence.rs#L30-L34` |
| 12 | `objective`（兜底） | 其余全部（含教师自建活动等未知类型） | `max(child_count, 1)` | `src/question/objective.rs#L31-L34` |

⚠️ **顺序是语义的一部分**：`oral-state` 的 `reply_type` 也是 `record`，若排在 `oral` 之后，「整题只算 1 项」的规则会静默失效，提交时子题数对不上（`src/question/mod.rs#L282-L285`）。

⚠️ `category` 的真实含义（参考实现实测分布）：`0` 学习题、`1` 客观题、`7` 口语、`8` 主观题；**真实客观题是 `1` 不是 `0`**，按直觉填 0 会把所有客观题判成学习题（`src/question/mod.rs#L64-L67`、`src/question/study.rs#L17-L27`）。

### 7.3 答案从哪里来、怎么编码

**关键结论：目标能力可以不"做题"——标准答案由服务端下发，客户端只是原样转发。**

| 环节 | 规则 | 来源 |
| --- | --- | --- |
| 答案来源 | `GET /course/api/v3/answer/…` 解密后，每题形如 `{"answer": "<字符串化 JSON>", …}`，二次解析后取 `children[i].answers[]` | `src/api/answer.rs#L1-L24`、`src/question/parse.rs#L216-L264` |
| 与题目的对应 | 外层下标与 `content` 解析出的题目顺序**一一对应** | `src/api/answer.rs#L49-L56` |
| 只有客观题有答案 | 口语、主观题在答案接口处为**空**（→ 主观题需要补占位文本） | `src/api/answer.rs#L16-L19`、`src/api/submit.rs#L162-L166` |
| 单个子题的答案表示 | 字符串列表，例如 `["D"]`、`["A","B","D"]` | `src/question/mod.rs#L216-L225` |
| 答案是否要排序/去重 | **绝不**。多选顺序沿用标准答案；排序题的顺序**就是答案**，重排必然判错 | `src/question/multi_choice.rs#L15-L17`、`src/question/sequence.rs#L3-L15` |
| 判分语义 | 客观题满分条件是**逐字匹配**服务端标准答案（大小写、空格、多选顺序都可能影响） | `src/question/single_choice.rs#L10-L16` |

**载荷里的两种编码（实现时最容易写反）：**

| 位置 | 形状 | 例 | 来源 |
| --- | --- | --- | --- |
| `answer.children[i].value` | **数组**（字符串元素），**主观题例外：必须是单个字符串**（多值用 `\n` 连接） | `["A","B","D"]` / 主观题 `"你好"` | `src/question/mod.rs#L263-L269`、`src/question/subjective.rs#L57-L61` |
| `thirdPartyJudges[i].value` | **字符串**：同一子题的多个值用**逗号连接** | `"A,B,D"` | `src/question/mod.rs#L227-L235` |

### 7.4 逐题型的载荷差异（"拉题目"阶段只需知道判分槽位数）

| 题型 | `payloads` | `value` | 备注 | 来源 |
| --- | --- | --- | --- | --- |
| 客观题（single/multi/dropdown/fill-blank/banked-cloze/sequence/兜底） | **空数组** | 字符串（多值逗号连接） | 服务端判分，客户端不上报分数 | `src/question/objective.rs#L4-L11`、`#L36-L44` |
| `subjective` | **空数组** | 字符串 | 不评分，只看完成度；无答案时补占位 `[已提交]` | `src/question/subjective.rs#L3-L22`、`#L63-L71`、`src/question/mod.rs#L46-L50` |
| `oral` | 1 个满分 record | 朗读文本 | 分数由客户端自报（见下） | `src/question/oral.rs#L50-L59` |
| `oral-state` | 1 个满分 record | 前面子题收进 `value` **映射**，最后一个子题才是"真作答" | 整题 1 项 | `src/question/oral_state.rs#L47-L70` |
| `role-play` | `payloads` 长度 = **子题数**（逐子题各一个 record） | **没有 `value` 字段** | 外层数组仍只 1 项（唯一"内外长度故意不一致"的题型） | `src/question/role_play.rs#L1-L32`、`#L56-L65` |
| `upload` | **恒空数组**（上传链未接入） | 无 `value` 字段 | 只能拿完成度、拿不到分 | `src/question/upload.rs#L1-L54` |
| `study` | 不产生判分项 | — | 但仍要出现在 `quesDatas` 里 | `src/question/study.rs#L1-L7` |

**口语/录音类"满分 record"的含义**（参考实现的协议层结论）：`recordDetail.score` 等分数由客户端本地赋值，服务端照存；因此口语/角色扮演类默认直接报满分，`audioUrl` 留空（`src/question/record.rs#L1-L31`、`#L59-L90`）。分数**量纲是 0~100**（`specific_scores.total = 100`），与语音评测那条链折算出的 0~1 不是一回事（`src/question/record.rs#L64-L71`）。

⚠️ **本规格不鼓励照搬"客户端自报满分"这一行为**，但它属于协议事实，实现者需要知道它存在（否则无法解释为什么参考实现的口语题是满分）。本项目若要实现，是否采用由产品与合规决定。

---

## 8. 提交语义（记录，不实现）

> 本章按要求**只记录**，不作为目标能力。任何实现前请先看第 9、10 节的风险。

### 8.1 端点与请求体

`POST https://ucontent.unipus.cn/course/api/v3/newExploration/submit`，头 `x-annotator-auth-token`。

请求体形状（键名逐字）：

| 键 | 值形状 | 说明 | 来源 |
| --- | --- | --- | --- |
| `courseId` | 字符串 `course-v2:…` | 实例 ID | `src/question/mod.rs#L552-L599` |
| `openId` | 字符串 | 必须显式传入，**不能为空** | `src/api/submit.rs#L207-L220` |
| `version` | 固定 `"default"` | — | `src/question/mod.rs#L590` |
| `quesDatas` | 数组，每项 5 键：`instanceId`（字符串）、`answer`（**字符串化 JSON**）、`context`（字符串化 JSON，固定 `{"state":"submitted"}`）、`contextVersion`（`1`）、`answerVersion`（从 1 开始） | — | `src/question/mod.rs#L404-L412` |
| `groupId` | 字符串 `u1g2` | — | `src/question/mod.rs#L592` |
| `isCompleted` | 布尔数组 | 长度 = 各题贡献项数之和 | `src/question/mod.rs#L570-L572`、`#L593` |
| `thirdPartyJudges` | **字符串化的 JSON 数组** | 长度必须与 `isCompleted` **严格相等** | `src/question/mod.rs#L594`、`src/api/submit.rs#L587-L612` |
| `submitType` | `1` 或 `2` | 该组**全部**是学习题时为 `2`，否则 `1` | `src/question/mod.rs#L506-L513` |
| `associationGroupId` / `associationUnitId` | 空串 | — | `src/question/mod.rs#L596-L597` |

`answer`（字符串化 JSON）的内部形状：

| 键 | 值 |
| --- | --- |
| `value` | 空数组（题级） |
| `children` | 数组，每项 `{"value": <数组，主观题为字符串>, "isDone": true}` |
| `progress` | 对象：键是**媒体项在 `contents` 里的下标**（字符串），值恒 `true`；主观题额外加 `progress.reply = true` |
| `record` | 口语/角色扮演为满分 record；其余为 `{"url": ""}` |

来源：`src/question/mod.rs#L358-L401`。

### 8.2 响应与"是否回传每题对错"

| 字段 | 含义 | 来源 |
| --- | --- | --- |
| `code` | `0` = 服务端接受 | `src/api/submit.rs#L109-L142` |
| `data.state.state` | `1` 表示判分通过 | 同上 |
| `data.state.score` | 逐子题得分列表（例 `[1,1,1,0]`） | 同上、`src/api/group_state.rs#L44-L49` |
| `data.state.score_pct` | 0~1 得分率 | 同上 |
| `data.version` | 服务端分配的版本号 | 同上 |
| `data.strategy.record_every_submit` / `record_max_submit` | 留存策略 | 同上 |

- **判分是否回传每题对错**：**部分**——响应里的 `score` 是一个**有序得分列表**（按子题顺序），能看出哪几项 0 分、哪几项 1 分；参考实现的注释把它描述为"逐子题得分展开"。→ **本调研未核对线上真实响应的粒度（按子题还是按题）**，见第 10 节 U6。
- 非判分题（口语/主观/上传）该列表为 `[]`（`src/api/group_state.rs#L71-L76`）。

### 8.3 幂等性与重复提交

- 提交后平台有"留存策略"：当 `record_every_submit == false` 时，**一组只留首次记录，后续提交不改写**；因此重复刷同一组通常没有意义（`src/api/submit.rs#L35-L38`、`#L98-L104`）。→ 这是参考实现的**声明**（其注释称来自实测与策略字段），本调研**未独立复现**。
- "服务端接受（`code:0`）"**不等于**"成绩记下了"，也不等于"达标"；参考实现在提交后**再回读一次** `progress/v2` 才判定达标（`src/api/submit.rs#L546-L553`）。
- 单组提交的完整往返是 **4 次请求**：内容 → 答案 → 提交 → 回读（`src/api/submit.rs#L489-L496`）。
- 纯学习题组（`submitType = 2`）**也必须提交**，其进度靠这次写请求记录（`src/question/mod.rs#L515-L529`）。

---

## 9. 限速与风控

| 机制 | 参数 | 来源 |
| --- | --- | --- |
| 限流识别码 | `600001`、`600002`（唯二） | `src/transport.rs#L319-L322` |
| 退避公式 | 等待 = `20 秒 × (第几次尝试 + 1)`，`attempt` 从 0 开始 → 20/40/60/80 秒 | `src/transport.rs#L38-L42`、`#L324-L343` |
| 最大重试 | 4 次 | `src/transport.rs#L39` |
| 调度层冷却 | 撞限流后该组重排队尾，并**冷却 45 秒**；同一组最多重排 3 次 | `src/bin/uai/commands.rs#L23`、`#L31`、`#L1494`、`#L1525` |
| 默认并发 | **1**（`DEFAULT_JOBS = 1`；参考实现实测并发更慢、更易撞限流） | `src/bin/uai/cli.rs#L472`、`#L727` |
| 组间可选间隔 | `--sleep-ms`，默认 `0` | `src/bin/uai/cli.rs#L214-L234`、`#L729` |
| 项目纪律（文档级） | 批量提交**每组至少间隔 20 秒**；被限流后必须真正等待（当时退避 45 秒） | `AGENTS.md#L31-L32` |
| 超时 | 常规 30 秒；可取消路径 8 秒 | `src/transport.rs#L29-L36` |
| 平台配额观测（参考实现声明） | 连续打 11 组后全部返回 `600002`，故建议分批而非整本硬打 | `README.md#L243` |
| 401 处理 | 响应是 HTML 登录页 → 判定为鉴权失败，不要当业务码解析 | `src/transport.rs#L12-L17`、`#L86-L101` |
| 可重试错误 | 仅网络层（连接/超时/TLS） | `src/error.rs#L104-L107` |

**实现建议**：把「限流退避」和「组间节流」拆成两层——单请求层只做有限次退避（20/40/60/80），调度层负责冷却与重排队，否则并发 worker 会被单个限流组睡死（参考实现明确记录了这个教训：`src/api/submit.rs#L450-L454`）。

---

## 10. 未验证与无法确定项

> 本次调研是**只读源码分析**：没有账号、没有发出任何真实请求、没有运行参考实现的任何代码。因此下列内容必须视为未知。

| # | 未验证项 | 说明 |
| --- | --- | --- |
| U1 | **解密后内容里"子题"的内部结构** | 源码只读取 `children` 的**长度**、`contents[].type`、`category`、`type`、`replyType`。每个子题有哪些字段（题干、选项字母、空位标记）**在源码中没有被读取过**，本规格给不出 |
| U2 | **答案字符串的语义** | 源码把标准答案的字符串**原样转发**，从不解码其含义。`"A"` 是选项序号还是选项文本、填空题答案是原文还是编号，**无法从源码确定** |
| U3 | `k` 字段的生成规则 | 只知道它是短数字字符串（源码示例形如 8 位日期），以及"密钥 = `1a2b3c4d` + k"。它的取值规律、是否每次变化**未验证** |
| U4 | 门户 JWT 的有效期与刷新方式 | 源码没有过期处理、没有刷新链路；会话失效只能重新登录。实际有效期**未验证** |
| U5 | 二维码登录的 UMCS Socket.IO 报文协议 | 只读到地址常量与"用 deviceId 轮询"的说明，报文细节**未验证** |
| U6 | 提交响应 `score` 列表的粒度 | 参考实现注释称"逐子题"，本调研未核对真实响应；按题还是按子题**未验证** |
| U7 | `quesDatas: []` 是否被接受 | 纯内容分组若解析不出题目项，参考实现**主动失败而不发空包**，并明确写了"未实测"（`src/api/submit.rs#L176-L183`） |
| U8 | 媒体上传链全链路 | 参考实现自述**未接入、未实测**（`src/api/media.rs#L89-L108`） |
| U9 | 语音评测服务端行为 | 签名公式与常量已读到，但是否始终被接受、返回结构稳定性**未验证** |
| U10 | 考核接口（`/api/aca/*`）的完整响应字段 | 本规格只给出请求体与端点；响应字段只做了部分读取 |
| U11 | `iss`/`aud`/签名密钥"来自前端公开 JS 包"这一说法 | 属于参考实现的自述，本调研**未独立核对**平台前端产物 |
| U12 | 所有标注「参考实现实测」的线上观测 | 包括多选题逗号顺序、`task_mini_score_pct` 只有 `0`/`0.6`、`state.pass` 不可信、限流码表现等——**均为源码注释所述，本调研未复现** |
| U13 | 依赖与版本兼容性 | 参考实现用 Rust edition 2024 / MSRV 1.87；Python 侧无对应约束，但**平台接口本身随时可能变更**（其 README 自述"不保证长期可用"） |

**本规格明确不做的事**：不实现提交、不实现时长上报、不实现语音评测、不实现二维码登录（仅在 2.4 登记）。

⚠️ **合规提示（事实陈述，不构成法律意见）**：参考实现在其 README 与许可证中声明，使用者需自行确保使用方式符合平台服务条款、学校规章与当地法律，且不保证长期可用；其许可证为**非商业使用许可**（详见 `docs/research/uai-core.md`）。本项目若按本规格实现，请自行评估平台 ToS 与校内规范风险。

---

## 11. 实现者自检：还差什么

**结论：只读本规格，足以写出「登录 → 拉课程 → 拉任务 → 拉题目」的 Python 骨架与正确请求，但不足以写出"能自动作答并满分"的实现。** 逐项说明：

### 11.1 覆盖充分、可直接实现的部分

| 能力 | 依据章节 | 备注 |
| --- | --- | --- |
| 密码登录（含两种加密、cookie 连续性、ticket 换 JWT） | 2.1、3.1 | 参数齐全，包括密钥与 IV 的十六进制值 |
| 取用户信息 → 组装会话（`ssoId`/`school`/`appUserId`） | 2.1、2.2 | 字段名与取舍规则明确 |
| 铸 `x-annotator-auth-token` | 3.2 | JWT 三段的构造、字段顺序、密钥、编码全部给定 |
| 拉班级/教材（书架 + 真实进度） | 4.2、6.4 | 含 `courseResourceId`/`strategyId`/`instanceId` 的来源区分 |
| 拉目录（单元→分组） | 4.3、6.4 | 含"`courseId` 不编码"、"`course` 是字符串化 JSON" |
| 拉必修集（`leafs` / 按班端点） | 6.3 | 含"必须显式传全部 groupId"、"空 = 信息缺失" |
| 拉题目内容 + 标准答案（含解密） | 3.3、4.3、7.1 | 解密算法、密钥推导、填充处理全部给定 |
| 判题型、算子题数 | 7.2 | 13 个类型名 + 判定条件 + 顺序约束 |

### 11.2 实现者仍会卡住的地方（按严重度排序）

1. **不能"理解题目"**：解密后内容的子题结构与选项表示在源码中未被读取（U1），答案字符串的语义也未确认（U2）。→ 想实现"自己作答"必须先补一次只读实测（把一份真实解密内容落盘分析）。**本规格只能支撑"转发服务端标准答案"。**
2. **子题数量对齐风险**：`isCompleted` 与 `thirdPartyJudges` 的长度由**内容侧**的 `child_count` 决定，而答案侧 `children` 数量由答案接口决定；参考实现**没有**校验两者是否一致。若不一致，提交会被拒（`300100`/越界）。→ 实现时必须自己加一致性校验并把不匹配的组标为"不可提交"。
3. **纯内容分组没有载荷先例**：`quesDatas: []` 未被验证（U7）。→ 遇到"解析不出任何题目项"的分组，按参考实现的做法**明确失败**，不要凭空发空包。
4. **必修集口径会打架**：ucontent 的 `leafs` 按 (实例, openId) 记账、不随班级变化，而门户按班端点才是班级口径；同一教材挂多班时两者可给出不同集合。→ 设计"拉任务"时必须显式选口径并把口径写进数据模型（参考实现把"本次必修集来自哪个口径"显式展示给用户）。
5. **完成度不能用分组状态数**：`state.state == 1` 只代表"账号有痕迹"，跨班串号且与班级计分点不一致。→ 统计只能用门户的 `finishPointNum/totalPointNum`（按班）。
6. **精度陷阱**：19 位 `instanceId` 一旦经过浮点就损坏。→ Python 全程 `str`/`int`，解析时避免任何 `float()` 往返。
7. **登录的 cookie 连续性**：步骤①与②必须同一会话；用一次性请求的 HTTP 客户端会在 `validate` 步骤失败（且失败形态可能是 HTML，不易定位）。
8. **限速是真实成本**：单组 4 次往返；组间需要节流（文档纪律 20 秒起）、限流后要真等（20/40/60/80 + 45 秒冷却），默认并发 1。→ 任何"快跑"设计的收益都会被配额吃掉。
9. **鉴权头一旦选错，只在受保护接口暴露**：目录/内容接口发错头照样成功，容易在联调早期给人"鉴权没问题"的错觉。→ 联调第一步就应打一次 `progress/v2` 或 `answer` 来验证 token 是否正确。
10. **口语/角色扮演的"客户端自报满分"行为**：属协议事实（第 7.4 节），但要不要照搬是产品与合规决定，不是技术问题。

### 11.3 建议的落地顺序（供实现者参考）

1. 会话层：登录三步 + 会话缓存 + 三种鉴权头选择器（配域名断言）。
2. 解密层：`unipus.` 密文解密（先用离线已知样本做单测，避免依赖网络）。
3. 只读层：用户信息 → 书架 → 班级进度 → 目录 → 内容/答案。
4. 必修层：`leafs`（显式传 groupId）+ 门户按班端点，并显式记录所选口径。
5. （延后）提交层：`isCompleted`/`thirdPartyJudges` 等长校验、限流退避、提交后回读。
