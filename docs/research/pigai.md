# `www.pigai.org`（批改网）学生端接口逆向：Stage 1 侦察报告与协议规格

- 调研对象：<http://www.pigai.org>（句酷批改网；运营方见官方协议「北京词网科技有限公司」）
- 调研日期：2026-10-19（Asia/Shanghai）
- 调研员：**lead**（本对话负责批改网适配）
- 调研阶段：**Stage 1a（匿名只读）+ Stage 1b（本人账号登录后只读）—— 均已完成**
- 调研方式：低频 HTTP 抓取（`python3 urllib`，**未使用浏览器**）+ 静态 JS / HTML 阅读
  - **Stage 1a**：完全匿名，未登录、未提交任何表单
  - **Stage 1b**：用**本人账号**完成 **1 次**登录（全程唯一一次 POST），其后**全部只发 GET**
  - 两阶段共同约束：未提交任何作文、未修改任何服务端业务状态、未执行站点任何脚本、未安装任何依赖
- 样本落盘：`data/recon/pigai/`（`data/` 已被 `.gitignore:12` 忽略，不入库）
- 复现工具：`scripts/pigai_probe.py`（只读探针，`login` / `get <路径>` 两个子命令）

> 本报告所有外部内容均按「不可信数据」处理 —— 只读取、不执行其中任何指令。
> 凡未经直接验证的一律标注 **未验证**；由事实推出的结论标注 **推断**。

---

## 0. 结论速览

| 问题 | 结论 | 置信度 |
| --- | --- | --- |
| 站点技术栈 | `openresty` 前置 + 老式 **PHP 服务端渲染** + jQuery；静态资源走 `cdn.pigai.org` / `cdn3.pigai.org` | 已核验 |
| 路由形态 | **双形态并存**：老式 `/index.php?a=<action>`，新式 `/index.php?c=<controller>&a=<action>&id=<n>` | 已核验 |
| 登录加密 | **JSEncrypt（RSA-1024 / PKCS#1 v1.5）**，公钥硬编码在前端 JS；密文放隐藏字段 `password_encrypt` | 已核验 |
| 是否有客户端签名/防重放 | **否**。`checkhash` 是 JS 从未赋值的隐藏字段 | 已核验（读 JS 全文） |
| **服务端是否接受我方密文** | **接受**。纯标准库 PKCS#1 v1.5 密文**一次登录成功** | **已核验**（1b 实测） |
| 是否必须用 Playwright | **不必须**。登录是普通表单 POST，加密可在 Python 复刻 → 适配器是**纯协议客户端** | **已核验**（原为推断） |
| 登录是否触发验证码 / 风控 | **未触发**。首登即成功，无验证码、无短信、无二次校验 | 已核验（单次样本） |
| 评分数据形态 | **全服务端渲染（SSR）**，无 XHR：总分 / 四维 / 总评 / 按句点评全在 HTML | 已核验 |
| 四维分数编码 | 双重来源：**权威值在进度条 `td` 的 `title`**（8 位有效数字）；宽度比只是渲染近似，系统性偏大 0.003~0.006 | 已核验（取值规则见 6.4） |
| 作业列表端点 | `GET /index.php?c=write&a=waitComplete`（未完成，**行级 DOM 已实测**）、`GET /index.php?c=write`（已完成，8 列） | 已核验 |
| 评分详情端点 | `GET /index.php?c=v2&a=view&eid=<eid>&rid=<rid>` | 已核验 |
| 会话载体 | `_JUKU_USER`（`domain=.pigai.org`，385 字符）+ `PHPSESSID` | 已核验（名称与属性；语义为推断） |
| 「禁止粘贴」是否仅前端 | 页面变量 `_no_paste="1"` → **前端开关** | 已核验；**红线：刻意不利用** |
| 官方 Open API 是否存在 | **存在**，`api.pigai.org/apibook/`，OAuth2 `client_credentials` | 已核验 |
| 本项目是否采用官方 API | **不采用**（用户决策，见第 2 节） | 决策 |

**一句话**：登录与数据面都已打通 —— 一次纯标准库 RSA 登录成功，端点地图与全部只读数据结构（作业列表 / 作文列表 / 总分 / 四维 / 总评 / 按句点评）均已实测。适配器可以是**零浏览器依赖、零第三方依赖的纯协议客户端 + HTML 解析**。剩余未知集中在**多稿、班级、频率限制**这些边角（第 8 节）。

---

## 1. 范围、红线与请求台账

### 1.1 范围（用户 2026-10-19 拍板）

| 项 | 决定 |
| --- | --- |
| 能力范围 | **仅只读**：作业 / 截止时间 / 历史分数 / 逐句批注。**不提交任何内容** |
| 接口路线 | **纯私有 Web 接口逆向**，不走官方 Open API |
| 账号 | 支持登录切换（多账号档案；见 7.4 的既有 bug） |
| 侦察授权 | 匿名只读 + 本人账号登录后的**只读**抓包 |

### 1.2 红线（与项目既有 dry_run / 幂等 / 限速文化一致）

1. **只支持本人账号**；不代刷他人账号。多账号档案是「可切换」能力，不是批量代刷。
2. **不利用老师的约束开关**：`_no_paste="1"`（禁止粘贴）、时间限制均为前端变量，接口层或许不校验 —— **刻意不去利用**（见 6.7）。
3. **不破解验证码**：遇到即人工介入或放弃。
4. **不伪造时间戳、不硬撞截止时间**。
5. **登录后只发 GET**；遇非 GET 先停下确认。
6. **凭据只走 `.env`**，不进日志、不进产物（现有 `Credentials.__repr__` 已有脱敏先例）。
7. **抓包产物必须脱敏**：不含 cookie 值、姓名、学号、手机号、邮箱、作文正文。

### 1.3 Stage 1a 请求台账（7 个匿名 GET）

| # | 请求 | 结果 |
| --- | --- | --- |
| 1 | `GET http://www.pigai.org/robots.txt` | 200，但**返回首页 HTML**（无 robots.txt，走 catch-all 路由） |
| 2 | `GET http://www.pigai.org/` | 200，34616 B |
| 3 | `GET http://www.pigai.org/index.php?a=login` | 200，1576 B，「请在正确的入口登录」→ **该端点只接受 POST** |
| 4 | `GET https://cdn.pigai.org//res/2013/2020.js` | 200，3527 B → **登录逻辑所在** |
| 5 | `GET https://cdn.pigai.org//res/javascript/jsencrypt.min.js` | 200，54919 B |
| 6 | `GET http://www.pigai.org/guest2016.html` | 200，营销落地页，**已排除** |
| 7 | `GET https://www.pigai.org/` | 200，与 #2 同内容（HTTPS 可用） |

### 1.4 Stage 1b 请求台账（**1 次 POST + 6 次 GET**）

| # | 请求 | 用途 | 结果 |
| --- | --- | --- | --- |
| 1 | `GET /` | 取服务端下发的 `old` cookie | 200 |
| 2 | **`POST /index.php?a=login`** | 登录（**全程唯一一次非 GET**） | 302 → 302 → 200，成功 |
| 3 | `GET /index.php?c=write` | 已完成作文列表 | 200，26740 B |
| 4 | `GET /index.php?c=write&a=waitComplete` | **未完成作业列表** | 200，13735 B，**0 行**（该账号无待完成作业） |
| 5 | `GET /index.php?c=v2&a=view&eid=…&rid=…` | 作文评分详情 | 200，82703 B |
| 6 | `GET /index.php?c=write&a=myclass` | 班级页 | 200，13834 B（未解剖） |
| 7 | `GET /index.php?c=write&a=history&eid=…` | 历史版本（多稿） | 200，17293 B（未解剖） |

**未发出任何提交、删除、修改类请求。** 第 2 步的 POST 只建立会话，不改动任何业务数据。

---

## 2. 官方 Open API：存在，但本项目不采用

`api.pigai.org/apibook/` 是一份公开可访问的 GitBook 官方手册，**真实存在**：

- **授权**：OAuth2 `client_credentials`，`POST http://api.pigai.org/oauth2/access_token` → `access_token`（7200 s）/ `Bearer` / `scope=all_json`。前置条件是**申请成为开发者**（《批改网API接入申请表》→ 审核 → 发 App Key/Secret）。
- **资源**：`/essays/rapid_experience_async`（作文分析，异步）、`/essays/read_write_async`（读后续写）、`/essays/translation_async`（翻译），另有 `other/hep_analysis`、`other/uxue_analysis`（未细读）。
- **返回结构**：总分 `score` + 四维 `score_cat`（**词汇 / 句子 / 篇章结构 / 内容相关**）+ 总评 `comment` + 逐句批注 `sentences[]`（`class` ∈ `warn` / `error` / `error_trp` / `great`，带 `msg`、`word_list`）。
- **性能承诺**：1 秒最多 20 篇（300 词内）、最高 500 并发；异步结果以 **POST 回调 push 给接入方**（需自备公网可回调端点）。
- **协议约束**：所有权归北京词网科技有限公司；实名申请；Key/Secret 保密；「访问应在合理频度之内」，超频限流；不得干扰 API 系统运行；不得暗示批改网背书。

**不采用的理由（用户决策）**：目标是逆向学生端接口，而官方 API 只覆盖**作文机评**，**不覆盖**「登录账号 → 拉班级作业 → 在老师布置的作业里提交」。

**保留价值（已兑现）**：官方文档是一手情报源。6.4 的四维分数编码正是靠它交叉印证 —— 官方 `score_cat` 与页面进度条是**同名、同序、同 0~1 量纲**的四个维度。

---

## 3. 站点技术画像

| 观测项 | 值 | 来源 |
| --- | --- | --- |
| `Server` | `openresty` | 响应头（已核验） |
| 前端 | jQuery（`/res/javascript/jquery-min.js?v=old10`）+ 服务端渲染 HTML | `home.html`（已核验） |
| 静态资源 CDN | `cdn.pigai.org`、`cdn3.pigai.org` | 页面引用（已核验） |
| 站点 cookie | `old=2012`，`domain=.pigai.org`，`Max-Age=31536000`，**所有子域共享** | 响应头（已核验） |
| 页面字符集 | `utf-8` | `<meta>`（已核验） |
| 路由 | `/index.php?a=<action>` 与 `/index.php?c=<controller>&a=<action>&id=<n>` | 已核验 |
| 合作登录 | QQ `/qq.php`、微信 `/d3/weixin/wxlogin.html`、微博 `/sina.php`、豆瓣 `/douban.php`、钉钉 `/d3/dding/qr.html` | 已核验，**对自动化无意义，已排除** |
| 旁支子域 | `tiku.pigai.org`（题库 / 自测 / 默写，另一套路由 `?m=&c=&a=`）、`rengong.pigai.org`（人工批改）、`zt.pigai.org`（活动）、`bbs.pigai.org` | 已核验（**未纳入本项目范围**） |

**「无 robots.txt」的含义（推断）**：站点未声明爬取策略，`/robots.txt` 被 catch-all 路由吃掉。*这不构成任何授权* —— 本项目的限速与只读纪律不因它放松。

---

## 4. 登录协议（已还原 + 真机验证）

### 4.1 表单契约（已核验）

登录表单位于**首页 `/`**，不在 `/index.php?a=login`（后者 GET 会提示「请在正确的入口登录」）：

```html
<form method="post" action="/index.php?a=login" id="lg_from_w">
  <input type="text"     name="username"          id="username">
  <input type="password" name="password"          id="password">
  <input type="checkbox" name="remember" id="rmb_c" value="1">
  <input type="hidden"   name="checkhash"         id="checkhash" value="">
  <input type="hidden"   name="password_encrypt"  id="password_encrypt">
</form>
```

提交时的 JS 行为（`2020.js` 第 55-59 行，**逐字核对**）：

```js
var psw = $.trim($('#password').val());
$('#password').val('');                            // 明文清空
$('#password_encrypt').val( encrypt(psw) );        // 密文进隐藏字段
$('#lg_from_w').submit();
```

→ **明文密码不进入网络**（原站设计如此），实际凭据在 `password_encrypt`。

**`checkhash` 从头到尾没有被赋过值** → **没有客户端签名、没有防重放挑战需要复刻**。这是 1a 最有价值的负面结论。

### 4.2 RSA 公钥与加密算法（已核验）

`2020.js` 第 139-145 行，**未混淆**，硬编码公钥：

```js
function encrypt(str) {
    var encrypt = new JSEncrypt();
    encrypt.setPublicKey('MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQCYlII16dSwQErDgjIl8BzU4NEL2IzvyWiLNxie3mkpw6eseF/iUVb3bisAFH+lzgnrv/mBOKUMkbqtW2+8en/6r0hj6ctvGT+UOtg4P5LF/jxkbE+cA2fVJK2RaBzeEEbrKOvauVnGkEOvPVl1/NK4NgeN6aSPIK9ECfXcjlEOHwIDAQAB');
    return encrypt.encrypt(str);
}
```

| 参数 | 值 |
| --- | --- |
| 算法 OID | `1.2.840.113549.1.1.1`（`rsaEncryption`） |
| 模数位长 | **1024 bit**（256 位十六进制） |
| 公开指数 `e` | `65537`（`0x10001`） |
| 模数 `n` | `0x98948235e9d4b0404ac3823225f01cd4e0d10b…4409f5dc8e510e1f`（完整值见附录 A 复现脚本） |
| 填充 | **PKCS#1 v1.5**（JSEncrypt `pkcs1pad2`：`00 02 ‖ 非零随机填充 ‖ 00 ‖ 明文`，遇零字节重试） |
| 输出编码 | base64（128 字节密文 → 172 字符） |
| 单块明文上限 | **117 字节**（`k - 11`） |

*安全观察*：1024-bit RSA 且全站共用同一公钥，按现代标准偏弱 —— 但这是**对方的实现选择**。另注意官方 API 域 `api.pigai.org` 以 **HTTP** 提供 `client_credentials`（`client_secret` 明文传输），而 `www.pigai.org` 的 HTTPS 可用 → **本路线走 HTTPS，不受前者影响**。

### 4.3 纯标准库复刻 —— 已通过真机验证

`scripts/pigai_probe.py` 用**仅标准库**（`pow()` + 手写 PKCS#1 v1.5 填充 + `secrets` 取非零随机字节）实现加密，**首次真机登录即成功**，服务端接受该密文。

> **由此推翻方案讨论第一轮的建议**：当时推荐「Playwright 登录 → 落盘 `storage_state` → 其余走 HTTP」，理由是「密码走 JSEncrypt，纯 HTTP 需复刻混淆 JS」。实测该 JS **未混淆（145 行）**、无签名、算法简单 —— 复刻成本远低于预期。
>
> **结论：适配器 = 纯协议客户端（标准库 RSA + 自管 cookie jar）。浏览器完全不需要**，与 `pyproject.toml:15-16` 的「核心零依赖」策略一致，也避开了 7.4 那个多账号会话互相覆盖的 bug。

### 4.4 真机登录实测结果（已核验）

**请求**：

```http
POST https://www.pigai.org/index.php?a=login
Content-Type: application/x-www-form-urlencoded
Referer: https://www.pigai.org/
Origin: https://www.pigai.org

username=<账号>&password=&remember=1&checkhash=&password_encrypt=<RSA密文b64>
```

**响应链**（登录成功判据）：

```
POST /index.php?a=login  → 302  Location: /index.php?f2=login
                         → 302  Location: /index.php?c=write&f2=login
                         → 200  落地页（标题「批改网」）
```

**成功判据（可直接用于适配器）**：登录后存在 `_JUKU_USER` cookie，**且**落地页不含登录表单标识 `lg_from_w`。

**会话 cookie 实测**（只记名称与属性，不记值）：

| cookie | domain | path | 值长度 | 备注 |
| --- | --- | --- | --- | --- |
| `old` | `.pigai.org` | `/` | 4 | 语言/旧版偏好；**匿名阶段就已存在**（`old=2012`） |
| **`_JUKU_USER`** | `.pigai.org` | `/` | 385 | **主登录态**（推断：登录后才出现，含用户标识与签名） |
| `_JKU` | `.pigai.org` | `/` | 11 | 用途未知 |
| `isPrize` | `.pigai.org` | `/` | 1 | 用途未知 |
| `JK_GCNT` | `.pigai.org` | `/` | 1 | 用途未知 |
| `PHPSESSID` | `www.pigai.org` | `/` | 26 | PHP 原生会话 |

- 均为 **session cookie**（无 `Expires`）；`HttpOnly` / `Secure` 标志**未观测到**（响应头未打印属性，**未验证**）。
- 会话有效期**未验证**（见 8-U18）。

### 4.5 会话失效时的页面 —— 会话判据必须用**正面**标记

2026-10-19 匿名实测（不带任何 cookie，模拟失效会话）：

| 请求 | 状态 | 大小 | `id="lg_from_w"` | `#essayList` | 跳转 |
| --- | --- | --- | --- | --- | --- |
| `GET /index.php?c=write`（匿名） | 200 | 1742 B | **无** | **无** | JS → `?a=loginForm&reurl=…` |
| `GET /index.php?c=write&a=waitComplete`（匿名） | 200 | 1799 B | **无** | **无** | 同上 |
| `GET /index.php?c=write`（已登录） | 200 | 26740 B | 无 | **有** | — |

失效页正文是「使用此功能，请先登录　3 秒后系统将自动跳转」，样本存在
`data/recon/pigai/raw/b_anon_write.html` / `b_anon_waitComplete.html`。

> ⚠ **重要结论：不能用「页面不含登录表单」当会话判据** —— 会话失效页同样不含它，
> 于是「不含登录表单 + jar 里有（哪怕陈旧的）`_JUKU_USER`」会双双成立 → **误判为已登录**，
> 于是跳过重登。这个缺陷由独立验证者指出，并由 Lead 的匿名实测补上了缺失样本。

**正确做法**：改用**正面判据** —— 合法列表页必含 `#essayList`
（`protocol.LIST_CONTAINER_ID`），会话失效页不含。该判据
**不依赖 cookie jar**，顺带关掉了「jar 中残留陈旧 cookie」的窗口。

**真正的登录页入口是 `?a=loginForm`**（不是 `?a=login`）：实测它返回与首页同内容的
30834 B 页面，含属性形态的 `id="lg_from_w"`、`jsencrypt`、`password_encrypt`
（样本 `data/recon/pigai/raw/b_loginform.html`）。会话失效页的 JS 跳转目标即指向它。

**可用的失效判据**（任一命中即判会话失效；实测覆盖 2 份失效页 / 7 份合法页）：

| 标记 | 失效页命中 | 合法页命中 |
| --- | --- | --- |
| `id="lg_from_w"` | 2 / 2 | 0 / 7 |
| `a=loginForm` | 2 / 2 | 0 / 7 |

`a=loginForm` 是站点**路由参数**，比「使用此功能，请先登录」那句中文文案**更耐改**。
契约里是 `protocol.SESSION_EXPIRED_MARKERS`，适配器的 `_guard_session()` 据此抛
`SessionExpiredError`。

> 勘误：初版守卫**只查登录表单标识**，而失效页并不含它 → 会话失效被误报成
> 「页面结构变了」（`HeaderMismatchError` / `PageShapeError`）。虽不是静默失败，
> 但**诊断错误**。由独立验证者「需知会-5」指出，已修并补了四条只读路径的回归用例。

---

## 5. 端点地图（实测）

### 5.1 本项目需要 / 关注的端点

| 端点 | 用途 | 方法 | 状态 |
| --- | --- | --- | --- |
| `/index.php?a=login` | 登录 | **POST** | 已实测 |
| `/index.php?a=loginForm` | **真正的登录页入口**（GET；返回与首页同内容的登录表单页，含 `id="lg_from_w"`） | GET | 已实测（§4.5） |
| `/index.php?c=write` | **已完成作文列表**（含成绩、提交次数） | GET | 已实测 |
| `/index.php?c=write&a=waitComplete` | **未完成作业列表**（含截止时间、指定班级） | GET | 已实测（0 行） |
| `/index.php?c=v2&a=view&eid=<eid>&rid=<rid>` | **作文评分详情**（总分/四维/总评/按句点评） | GET | 已实测 |
| `/index.php?c=write&a=history&eid=<eid>` | 历史版本（多稿） | GET | 已取到，**未解剖** |
| `/index.php?c=write&a=myclass` | 班级 | GET | 已取到，**未解剖** |
| `/index.php?c=v2&a=viewrequest&rid=<rid>` | 题目要求详情 | GET | 页面中引用，未单独取 |
| `/index.php?c=write&a=searchReq&rid=<作业号>` | 按作业号查找 | GET | 搜索表单 action，未取 |

### 5.2 已发现但本项目**不用**的端点

| 端点 | 用途 | 为什么不碰 |
| --- | --- | --- |
| `/index.php?c=v2&a=write&eid=…&rid=…` | **写作页**（写操作入口） | 只读范围，不接触 |
| `/index.php?c=write&a=history_gj` | 历史（另一形态） | 未解剖 |
| `/index.php?c=v2&a=view3` | 评分页变体 | 未解剖 |
| `/index.php?c=acorrect` / `rengong.pigai.org` | 人工批改（付费） | 不涉及 |
| `/index.php?c=write&bf=1` / `bf=2` | 我的题库 / 我的自测 | 属 `tiku.pigai.org` 线，**未纳入范围** |
| `/?c=teacher&a=chongzhi` | 批改卡充值 | 不涉及 |
| `/index.php?c=test&a=help_1` | 帮助 | 不涉及 |

### 5.3 命名规律（已核验）

- 新式路由 `?c=<controller>&a=<action>`，`eid` = 单篇作文实体 ID（数字，9 位），`rid` = 批改网**作文号**（数字，7 位）。
- **`eid` 与 `rid` 是两个不同的标识**：详情页 URL 同时需要两者；`eid` 只出现在链接 `href` 里，**不出现为文本** → 解析列表时必须从 `href` 提取。
- 官方 API 文档里的 `rid`（「批改网作文号」）与页面 `rid` **同名同义** —— 交叉印证。

---

## 6. 数据面规格（实测）

### 6.1 作业列表（未完成）`GET /index.php?c=write&a=waitComplete`

**容器结构（已核验）**：

```html
<div id="essayList" class="ydTable">
  <ul class="reItemM">                     <!-- 表头 -->
    <li style="width:80px;" class="ft">作业号</li>
    <li style="width:448px;">作业标题</li>
    <li style="width:50px;">类型</li>
    <li style="width:80px;">出题人</li>
    <li style="width:100px;">指定班级</li>
    <li style="width:80px;">截止时间</li>
    <li style="width:110px;">操作</li>
    <div style="clear:both"></div>
  </ul>
  <div class="line" style="background-color:#d9d9d9;height:1px;"></div>
  <ul class="reItemS"> …数据行… </ul>       <!-- 本轮为空 -->
</div>
```

**数据行（已实测，逐字）**：

```html
<ul class="reItemS">
  <li style="width:80px;">1000005</li>                        <!-- [0] 作业号 = rid -->
  <li style="width:448px;" class="stu_title">                 <!-- [1] 作业标题 -->
    <a href="?c=v2&a=write&rid=1000005" title="查看作文:">示例B层次大英I（三）- B2U6</a>
  </li>
  <li style="width:50px;">作文</li>                           <!-- [2] 类型 -->
  <li style="width:80px;">张老师</li>                           <!-- [3] 出题人 -->
  <li style="width:100px;" title="示例班级,…">示例班级,…</li>   <!-- [4] 指定班级 -->
  <li style="width:80px;">2026-11-16 23:59</li>               <!-- [5] 截止时间 -->
  <li style="width:110px;"><a href="?c=v2&a=write&rid=1000005">查看</a></li>  <!-- [6] 操作 -->
</ul>
```

七个 `<li>` 与 `PENDING_HEADERS` **逐字对齐**。三个易踩的细节：

1. **href 是相对路径**（`?c=v2&a=write&rid=1000005`，无前导 `/`），且**不含 `eid`** ——
   待完成作业的链接指向**写作页**而不是评分页；`eid` 只有提交之后才存在。
2. 标题链接的 `title` 属性是**空值**（`title="查看作文:"`），**不能当标题用**，标题取链接文本。
3. 第 4 列（指定班级）的**文本与 `title` 属性内容相同**，都是那串超长班级列表。

> **勘误（2026-10-19）**：本报告初版曾断言此页「只有表头、0 行、行级 DOM 未验证」（原 U15）。
> 那是**抽取错误** —— 第一版用关键词过滤正文，而这一行的单元格恰好不含所选的任何关键词，
> 于是被漏判为空态。**该断言已撤回，行级 DOM 现为已实测。** 由 `parser-dev` 独立复核发现。

### 6.2 作文列表（已完成）`GET /index.php?c=write`

**表头 `ul.reItemM`**：`作文ID(80px) | 作文标题(448px) | 体裁(50px) | 提交时间 | 截止日期 | 成绩(50px) | 提交次数(60px) | 操作(110px)`

**数据行（已实测，逐字）**：

```html
<ul class="reItemS">
  <li style="width:80px;">1000006</li>                       <!-- 作文ID = rid -->
  <li class="stu_title">
    <div class="stu_title_link">
      <a href="/index.php?c=v2&a=view&eid=200000004&rid=1000006"
         title="查看作文:第1000006号 作文">第1000006号 作文</a>
    </div>
    <div class="title_tag"></div>
  </li>
  <li style="width:50px;">作文</li>                          <!-- 体裁 -->
  <li>2026-10-16</li>                                        <!-- [3] 提交时间 -->
  <li>2026-10-19</li>                                        <!-- [4] 截止日期 -->
  <li style="width:50px;">                                   <!-- [5] 成绩 -->
    <span title="未到截止日期，暂不公布最终成绩"> 未截止 </span>
  </li>
  <li style="width:60px;">                                   <!-- [6] 提交次数 -->
    <a href="/index.php?c=write&a=history&eid=200000004" target="_blank">2</a>
  </li>
  <li style="width:…">查看 | 完善 | 删除</li>                  <!-- [7] 操作 -->
</ul>
```

**八个 `<li>` 按顺序取值（已逐字核验）**：

| 序号 | 列 | 取值 | 陷阱 |
| --- | --- | --- | --- |
| 0 | 作文ID | `1000006` → **= `rid`** | 纯数字文本 |
| 1 | 作文标题 | 链接文本 + `href` 里的 `eid`/`rid` | **`eid` 只在此处** |
| 2 | 体裁 | `作文` | — |
| 3 | 提交时间 | `2026-10-16` | 只有日期，无时间 |
| 4 | 截止日期 | `2026-10-19` | 只有日期 |
| 5 | **成绩** | 本行是 `<span title="未到截止日期，暂不公布最终成绩">未截止</span>` | **⚠ 截止前不公布成绩**，此列可能是文字而非分数 |
| 6 | 提交次数 | `<a href="?c=write&a=history&eid=…">2</a>` | **既是计数，也是多稿入口** |
| 7 | 操作 | `查看 / 完善 / 删除` | 含**删除**入口 —— 只读适配器绝不碰 |

> **⚠ 业务要点（本轮新发现）**：列表的「成绩」列在**截止日期之前不公布**（`未截止`）。
> 因此「读历史分数」**不能依赖列表列**，必须取评分详情页的 `#rescoreTxt`（实测该篇有 `85.5`）。
> 另外「提交次数 = 2」证实**多稿语义真实存在**（`?c=write&a=history&eid=`）。

**解析要点**：
- `eid` **只在 `href` 里**，且 `href` 未编码 `&`（直接可正则）。
- 行内 `<li>` **多数没有 class**，只能按**顺序**取值 → 解析器需容错，宁可少取不可错位。
- 表头的 `<div style="clear:both"></div>` 出现在 `ul` 内（非法 HTML），**HTML 解析器可能把它挪出 `ul`** → 不要依赖严格的 DOM 嵌套。
- 第 7 列含**删除**操作入口 → 适配器只解析、永不构造该类请求。

### 6.3 评分详情页 `GET /index.php?c=v2&a=view&eid=<eid>&rid=<rid>`

| 字段 | DOM 定位 | 实测样例（脱敏） |
| --- | --- | --- |
| **总分** | `<b id="rescoreTxt" title="满分:100" content="100">85.5</b>` | `85.5`；`content` 属性 = 满分 |
| **作业元信息** | `<ul class="stu_info_item">` | `开始时间：… / 截止时间：… / 满分: 100 / 字数: 100~200 / 批改方式：… / 批改公式：` |
| **题目** | `<div class="yaoqiu_info"><h3>题目：…</h3>` | `示例B层次大英I（三）- B2U5` |
| **要求** | `<div id="yaoqiuW">` 内 `.yq_itme` ×N | `满分: 100` / `字数: 100~200` / `要求：<题干>`；`更多` → `?c=v2&a=viewrequest&rid=` |
| **总评** | `<span id="allpingyu_txt">` | 「可适当增加从句的使用；作者词汇基本功很好…」 |
| **四维分数** | `.tbL` 标签 + 紧随的进度条 | 见 6.4 |
| **按句点评** | `<li sid=… class=… cate=…>` | 见 6.5 |
| **批注字典** | `var fb_js = {…}` | 见 6.6 |
| **作文正文** | `#contents` —— **按 id 匹配，不要按标签** | 1070 字符，见下 |

**⚠ 正文容器 `#contents`：同一个 id，在两个页面是不同标签**（实测，2026-10-19）：

| 页面 | 实测形态 |
| --- | --- |
| 本页（详情页） | `<div id="contents"><div id="parahtml" style="white-space:pre-wrap">…` |
| 写作页 / lookup 页（§6.11） | `<TEXTAREA NAME="contents" id="contents" class="from_contents">…` |

- **只按 id 匹配，不要按标签名。** 按 `TEXTAREA` 硬过滤会让**详情页读到 0 字**；
  若确需按标签匹配，必须**大小写不敏感**（写作页那个是**大写**标签）。
  契约里的 `DETAIL_BODY_TAG = "div"` / `WRITE_BODY_TAG = "TEXTAREA"` 就是这个区分。
- 页面上另有 **3 个** `<textarea class="section-content draft-top|main|tail">`
  （作文开头 / 主体 / 结尾的**常见表达模板**）—— **绝不能被当成正文**。
- 正文除 `strip()` 外**不做任何归一化**（要原样可读，将来要能回写）。
- 实测长度：详情页 strip 后 **1070**、写作页 **1070**，两者**逐字相同**。
  > 勘误：本报告相关的任务书里曾写「详情页 1066 字符」——那是用正则
  > `>(.*?)</div>\s*<div` **提前截断**得到的假值，已作废。用树解析复量为 1070。

**页面无任何 XHR 取分数** —— 全部 SSR。页面内出现的 ajax 目标只有 `?c=api&a=htbt`、`/zt/admin/api/?a=isCertificate` 等与分数无关的调用。

### 6.4 四维分数编码（已实测；**权威值在 `td[title]`**）

四个维度**不在 DOM 文本里给数字**，而是渲染成进度条；同时以两种形式给出数值：

```html
<div class="tbL" title="">词汇: </div>                       <!-- 标签；title 为空 -->
<div style="float:left;width:154px;…;height:10px;…">
  <table … style="width:138.26512px;float:left;…">…</table>   <!-- 渲染近似 -->
</div>
…
<td title="0.8951008">…</td>                                 <!-- ★ 权威分数 -->
```

实测四组（同一篇作文）：

| 维度 | 容器宽 | 条宽 | 宽度比 | **`td title`（权威）** | 偏差 |
| --- | --- | --- | --- | --- | --- |
| 词汇 | 154 px | 138.26512 px | 0.897825 | **0.8951008** | +0.00272 |
| 句子 | 154 px | 133.94655 px | 0.869783 | **0.86631035** | +0.00347 |
| 篇章结构 | 154 px | 121.47978 px | 0.788830 | **0.78319855** | +0.00563 |
| 内容相关 | 154 px | 121.30305 px | 0.787682 | **0.78202034** | +0.00566 |

**取值规则（已写入契约）**：**优先取 `td` 的 `title`**，缺失时才兜底用「条宽 / 容器宽」。
`protocol.Dimension.source` 记录本次来源（`"title"` / `"width"`）。
宽度比值是渲染近似，**系统性偏大 0.003~0.006** —— 不要因为"看起来更精确"而选它。
`title` 与宽度**来源独立**：宽度异常**不会**让一个有效的 `title` 失效。

> ⚠ **`bar_width` 可以是 `0`**：某一维度真得 0 分是**合法页面**（实测容器 200px、
> 进度条 0px、无 `title` → `ratio = 0.0`、`source = "width"`）。
> 要判断「这一维有没有数据」请看 **`source`**，**不要**断言 `bar_width > 0`。
> （契约初版把不变式误写为「一定有限**正**数」，由独立验证者「需知会-6」用这个最小复现纠正。）

**交叉印证**：官方 API 的 `score_cat` 是**同名、同序、同 0~1 量纲**的四维（`1 词汇 / 2 句子 / 3 篇章结构 / 4 内容相关`，样例值 `0.8426485 / 0.74554825 / 0.7881277 / 0.7233994`）→ 与 `title` 同量纲，互相印证。

**未解释（保留原始观察，未验证）**：宽度比相对 `title` 的偏差**不是线性缩放能消除的**（四维偏差从 0.0027 到 0.0057 不等）。推断宽度是「分数 → 百分比 → 定宽像素」多段取整后的产物，但无证据。容器宽度是否恒为 `154px` 同样**未验证** —— 适配器必须从同一 DOM 读取容器宽，不得硬编码。另一条可用作断言的交叉验证：四维加权 ≈ 总分（本篇 `85.5`）。

### 6.5 按句点评标注结构（已核验）

```html
<li sid="0"
    snt="Stay at Home or Going Out for Pleasure"          <!-- 原句 -->
    class="error_trp snt_hx"                              <!-- 标注等级 + 样式钩子 -->
    content="_stay"                                       <!-- 关键词 key -->
    cate="confusion"                                      <!-- 错误类别 -->
    short_msg="易混词汇: remain, linger, &lt;b&gt;stay&lt;/b&gt; 均有“停留，逗留”之意。">
  [学习提示]易混词汇: remain, linger, <b>stay</b> 均有“停留，逗留”之意。
  <span class="stips"> </span>
  <div class="snt_info"> … 近义词辨析详情（含 jukuu.com 外链）… </div>
</li>
```

实测统计（同一篇作文，**19 条**）：

- `class`：`error_trp snt_hx` ×17、`error snt_hx` ×2
- `cate`：`confusion` ×10、`i_suggest.word` ×3、`i_suggest.phrase` ×2、`e_pos.jj_to_nn` ×1、`i_tip.also` ×1、`e_trp.von` ×1、`i_tip` ×1

**等级映射（已核验）**：官方 API 定义了四类 `class` —— `warn`（警告）/ `error`（错误）/ `error_trp`（提示）/ `great`（好）。首轮样本只出现 `error` 与 `error_trp`；**线上端到端复核（§6.10）确认四类全部出现**。适配器仍应把未知 `class` 值**原样保留**，不要映射成枚举失败。

**可用的结构化字段**：`sid`（句序，从 0 开始）、`snt`（原句全文）、`content`（关键词）、`cate`（机器可判定的错误类型）、`short_msg`（批注文案，**HTML 转义**，需反转义）。

### 6.6 内嵌批注字典 `fb_js`（已核验）

页面脚本里有一个按关键词聚合的批注字典，**unicode 转义的 JSON**：

```js
var fb_js = {
  " stay": {"kp":"_stay","sid":[0,2,11],"txt":"易混词汇: remain, linger, <b>stay</b> 均有…","category":"学习提示"},
  "or@4":  {"kp":"or@4","sid":[0],"txt":"otherwise/if not/before/or else与<b>or</b>意思相近…","category":"推荐表达"},
  " with the development of jj": {"kp":"_with the development of _jj","sid":[1],"txt":"词性错误，建议…","category":"词性错误"},
  …
}
```

**价值**：`sid` 数组把「一个知识点 → 它出现在哪几句」直接给出，比从 `<li>` 反推更省事；`category` 是中文类别名（`学习提示` / `推荐表达` / `词性错误`），比 `cate` 的英文枚举更可读。**解析它需要 `json.loads` 前先解 unicode 转义并剥掉外层 JS 包装**。

### 6.7 前端开关变量（**红线：刻意不利用**）

评分页内嵌：

```js
var _no_paste      = "1";      // 禁止粘贴
var _snt_is_change = false;    // 句子是否被改动
var _school_opt    = '2';      // 学校配置
```

`_no_paste="1"` 证实「禁止粘贴」是**前端变量**。本项目**不利用**这一点（红线 1.2-2）；它只是解析时的上下文信息。

相似度 / 查重入口：`/?c=wx_t&a=img&scene=<eid>,<eid2>`（「点击查看相似详情」）—— 在本样本页面中**被 HTML 注释掉**。第三方的油猴脚本称「即使批改网不显示重复也可能是重复作文」，**第三方说法，未验证，本项目不涉及**。

### 6.8 班级页 `GET /index.php?c=write&a=myclass`（已核验）

按班级分组的**作业条目**，落在同一 `#essayList` 体系之外，实测正文形如：

```
ID：3431453   提交人数： 43   截止日期：2026-09-19 23:59   已截止
```

- 字段：**作业 ID（= `rid`）、提交人数、截止日期、状态（`已截止`）**。
- 用途：`list_courses` 的数据源（班级维度），并可交叉核对作业归属。
- 注意：**`提交人数` 是班级维度的聚合值**，与「我提交了几次」不是一回事（后者在 6.2 第 6 列）。
- **未解剖**：本页的完整 DOM 容器与 class 体系（本轮只做了正文级确认）。

### 6.9 多稿历史页 `GET /index.php?c=write&a=history&eid=<eid>`（已核验）

标题「第1000006号作文的历史作文版本」。实测正文含：

- **稿次对比图例**：`灰色 = 删除部分`、`黄色 = 增加部分`
- **逐稿【总评】**并列，例如：
  - 稿 A：「用词灵活多样，也能较灵活地使用一些高级词汇；可适当增加从句和复杂句使用；衔接成分太少，文章结构欠佳。」
  - 稿 B：「可适当增加从句的使用；作者词汇基本功很好，高级词汇表达也比较恰当；语言不流畅。」
- 另一入口 `?c=write&a=history_gj`（**未解剖**，疑为「高级/更完整」版本）。

→ **确认：同一 `rid` 下存在多稿，每稿有独立 `eid` 与独立评分。** 这直接决定 7.2 的幂等键设计。

### 6.10 线上端到端复核（Lead 实测，2026-10-19，只读）

用**新架构**（`AdapterContext.http` → `HttpClient` → `PigaiAdapter`）**复用已保存会话**
（**未再发登录 POST**）对真实站点跑通全链路，实测输出：

| 调用 | 结果 |
| --- | --- |
| `list_courses()` | 1 条合成课程（不发请求） |
| `list_pending_assignments()` | **1 条**：`rid=1000005`，`deadline=2026-11-16 23:59`，指定班级 34 个 |
| `list_finished_essays()` | **10 条**；其中成绩未公布 1 条、**稿次 > 1 的有 4 条** |
| `fetch_essay_detail(rid=1000003, eid=234022144)` | 总分 **89.0 / 100**；四维全部 `source="title"`；总评 41 字；**按句点评 24 条** |
| `list_assignments(course)` | 11 条（未完成 1 + 已完成 10，按 `rid` 去重） |

**由此新确证两条：**

1. **多稿真实存在**：10 篇里 4 篇的「提交次数」> 1（§6.2 第 6 列即多稿入口）。
   这坐实了 §7.2 的幂等键决定 —— `remote_id` 必须用 `rid`；用 `eid` 会把第二稿当成新作业。
2. **四类标注等级全部出现**：实测 `level` 集合 = `{error, error_trp, great, warn}`，
   与官方 API 定义的四类**完全一致**（原 U25 就此关闭）。

*证据性质*：以上是 Lead 对真实站点的观察，**不可由离线单测复现** ——
线上访问只由 Lead 发起（多 agent 同时登录同一账号会触发风控）。

> ⚠ **不要把它和离线样本混为一谈**（verify 者曾因此对不上）：本节详情数字来自
> **`rid=1000003 / eid=234022144`**；而仓库里唯一的离线详情样本是
> **`eid=200000004`（`§6.3` 那个）**，它是 **85.5/100、19 条点评、`level` 只有
> `{error, error_trp}`**。两篇不同的作文，两组数字都对。
> 四类 `level` 齐全的结论只对 `eid=234022144` 成立。

### 6.11 写作页 / 按作业号定位页（新增，2026-10-19）

**两个 URL 返回功能等价的页面**（实测：同长度、同 `_eid`、`#contents` 正文逐字相同）：

```
?c=v2&a=write&rid=<作业号>              ← 写作页（写语义）
?c=write&a=searchReq&rid=<作业号>       ← 站点自己的「查找作文」表单（读语义）
```

**本项目只用后者**（`protocol.Endpoints.lookup_by_rid`），因此源码里不出现写端点字面量，
静态审计（`TestNoWriteSurface`，AST 扫全包可执行字符串）得以保持成立。

实测内嵌 JS 变量（形态 `var _x = '...';`）：

| 变量 | 含义 |
| --- | --- |
| `_rid` | 作业号，如 `'1000005'` |
| `_eid` | **空串 = 这个作业没写过**；非空 = 已有稿（如 `'200000004'`） |
| `_no_paste` | 前端「禁止粘贴」开关（`"1"`）。**只读取、不利用**（红线 §1.2-2） |

页面文本区给出：`字数:120~150`、`满分:100`、`题目： [<rid>] <标题>`、`要求 <题目要求>`、
`截止时间：2026-11-16 23:59`、`当前时间： 2026-10-19 11:16:06`（**服务端时钟**）。

**正文**在 `<TEXTAREA NAME="contents" id="contents" class="from_contents">`（**大写标签**）：
未写过时为空；已写过时能**读回当前稿**（实测 1070 字符）—— 这就是
「未截止可以继续修改」的**只读一半**：先看到改到哪了。

**GET 无副作用（已实测）**：进入该页前后各取一次列表快照，稿次总和 **19 → 19**，
逐条 `rid`/稿次完全一致 —— **不建草稿、不加稿次**。这是把它纳入只读范围的依据。

---

## 7. 与 `kedaibiao` 骨架的接口错配点

### 7.1 作答阶段不需要实现（只读的简化）

`RuleAnswerer` 对 `QuestionType.ESSAY` 返回空（`src/kedaibiao/tasks/answerer.py:90`）。但**只读范围内根本不需要作答器** —— `Capabilities.submit = False`，`EssayAnswerer` 整体推迟到写入阶段。

### 7.2 幂等键与多稿语义冲突

`ProgressStore.already_done(platform, assignment_id)`（`src/kedaibiao/bank/store.py:348`）假设「成功一次就永远跳过」。批改网按**多稿**组织（`?c=write&a=history&eid=` 即历史版本），若将来启用写入，`remote_id` 必须带稿次（形如 `rid#3`）。

**只读范围内不触发**，但 `RunRecord.assignment_id` 的取值现在就要定死：**建议 `rid`（作文号）作为主键，`eid` 存 `detail`** —— 因为 `rid` 是跨稿稳定的，`eid` 是单篇实体的。

### 7.3 题库回填会污染数据

`QuestionBank` 的指纹是「题目 → 答案」映射，作文没有标准答案；`_extract_verdicts`（`src/kedaibiao/tasks/pipeline.py:303`）只认 `raw["questions"] = [{id, correct}]`，认不了批改网的 `score_cat` / `sentences`。

**决定**：分数与批注落 `runs.detail` / `submissions.raw`，**不写题库**，`write_back` 对批改网恒为 `False`。

### 7.4 多账号会话互相覆盖（既有 bug）

`BrowserSession.state_path(platform)`（`src/kedaibiao/browser/session.py:134`）只按**平台名**命名 → 多账号切换时登录态互相覆盖。

**主路径已绕开**（纯 HTTP + 自管 cookie jar，jar 按 `pigai.<账号>.json` 命名）；浏览器回退路径仍需修成 `platform + 账号标识` 二级命名。**待办，不阻塞。**

### 7.5 `dry_run` 的形态必须重做（写入阶段）

现在是打印「选项列表」（`src/kedaibiao/tasks/pipeline.py:261-267`）；作文场景应打印**全文 + 目标作业 + 稿次**。只读范围内无提交动作，推迟。

### 7.6 解析层是新增的依赖方向

现有骨架没有任何 HTML 解析设施（`dependencies = []`）。批改网数据面是 **SSR HTML**，因此适配器需要一层解析，**建议用标准库 `html.parser` 自写**而不是引入 `beautifulsoup4`/`lxml` —— 理由是 6.2 提到的非法 HTML（`ul` 内嵌 `div`）用严格解析器反而更麻烦，且能保住零依赖策略。

---

## 8. 未验证清单（**不得据此行动**）

**已在 1b 中转为「已验证」的原 1a 条目**：U1（服务端接受我方密文 ✅）、U2（登录成功判据 ✅ 见 4.4）、U3（登录未触发验证码 ✅ 单样本）、U6（数据形态 = SSR ✅）、U10（需要先取 `old` cookie ✅）。

**已在 1b 补充解剖后关闭的条目**：U16（已完成列表整行 8 列 ✅ 见 6.2）、U22（班级页正文结构 ✅ 见 6.8）、U23（多稿历史页 ✅ 见 6.9）。

**已在线上端到端复核后关闭的条目**：U25（`warn` / `great` 两类标注确实出现 ✅ 见 6.10）。

**已撤回的条目**：**U15（未完成作业列表的行级 DOM）** —— 初版断言「0 行、无样本」是**本报告作者的抽取错误**，实际该页一直有 1 条真实数据行。已由独立复核发现并撤回，行级 DOM 现为已实测（见 6.1 的勘误与真实行样本）。

| # | 未验证项 | 原因 / 说明 |
| --- | --- | --- |
| U17 | 四维分数是否恒有 `td[title]`，容器宽是否恒为 154px | 只有 1 篇样本。宽度比相对 `title` 的偏差非线性（0.0027~0.0057），成因未解释。**适配器必须优先 title、并从同一 DOM 读容器宽** |
| U18 | 会话有效期 | session cookie，未知何时失效 |
| U19 | `f2=login` 参数语义 | 出现在登录重定向链上，未考据 |
| U20 | `_JUKU_USER` 能否脱离 `PHPSESSID` 独立复用 | 未做拆分实验 |
| U21 | cookie 的 `HttpOnly` / `Secure` 标志 | 1b 未读取响应头属性 |
| U24 | 站点频率限制阈值 | 本轮主动限速，未压测 |
| U26 | 是否存在「打开即标记已读」类副作用请求 | 已发 6 个 GET 未观察到异常，但未逐页比对服务端状态 |
| U27 | 密码长度上限 | RSA-1024 单块明文 ≤117 字节，超长未测 |
| U28 | `tiku.pigai.org`（题库/自测/默写）是否纳入范围 | 属另一套路由与子域，**当前不在范围内** |
| U29 | 官方手册 `other/hep_analysis`、`other/uxue_analysis` 能力 | 未细读（本路线不用官方 API） |

---

## 9. 下一阶段

### ✅ Stage 1a / 1b —— 已完成

匿名侦察 + 登录后只读抓包，端点地图与数据面规格已落到本报告第 4-6 节。

### Stage 2｜协议规格（**进行中**）

本报告第 4-6 节即为规格草稿。剩余工作：
1. 解剖 `?c=write&a=myclass`（班级）与 `?c=write&a=history`（多稿）两页 → 补 U22 / U23；
2. 补全已完成列表整行 → 补 U16；
3. 把端点常量与 DOM 选择器固化到 `src/kedaibiao/platforms/pigai/protocol.py`（单一出处，沿用 `docs/research/uai-core.md` 的纪律）。

### Stage 3｜适配器实现

`src/kedaibiao/platforms/pigai/`：

- `rsa_login.py` —— 标准库 PKCS#1 v1.5（**已验证**）
- `client.py` —— cookie jar（`data/sessions/pigai.<账号>.json`，mode 600）+ 限速
- `parse.py` —— 标准库 `html.parser`，按 6.1-6.6 的 DOM 规格解析
- `adapter.py` —— `PlatformAdapter`：`login` / `is_logged_in` / `list_courses`（班级）/ `list_assignments`；**`Capabilities.submit = False`**
- 离线单测：`tests/fixtures/pigai/` 放脱敏 HTML 样本

### Stage 4｜验收

`make test` 全绿（含修掉当前既有的 2 个失败用例）、`kedaibiao doctor` 通过、单账号端到端**只读**跑通并把数据打进 `runs.detail`。

---

## 附录 A：可复现命令

**只读约束声明**：Stage 1a 全程匿名；Stage 1b 仅 1 次登录 POST + 6 次 GET。**未提交任何作文/删除/修改请求，未执行站点任何脚本，未安装任何依赖。**

```bash
# 探针（读 .env 里的 KEDAIBIAO_PIGAI_USER / _PASS，不打印凭据）
python3 scripts/pigai_probe.py login
python3 scripts/pigai_probe.py get "/index.php?c=write"
python3 scripts/pigai_probe.py get "/index.php?c=write&a=waitComplete"
python3 scripts/pigai_probe.py get "/index.php?c=v2&a=view&eid=<eid>&rid=<rid>"
```

纯标准库解析硬编码公钥并复刻加密（与 `scripts/pigai_probe.py` 同源）：

```python
import base64, secrets
KEY = "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQCYlII16dSwQErDgjIl8BzU4NEL2IzvyWiLNxie3mkpw6eseF/iUVb3bisAFH+lzgnrv/mBOKUMkbqtW2+8en/6r0hj6ctvGT+UOtg4P5LF/jxkbE+cA2fVJK2RaBzeEEbrKOvauVnGkEOvPVl1/NK4NgeN6aSPIK9ECfXcjlEOHwIDAQAB"
b = base64.b64decode(KEY)
def tlv(b, i):
    tag = b[i]; i += 1; ln = b[i]; i += 1
    if ln & 0x80:
        n = ln & 0x7f; ln = int.from_bytes(b[i:i+n], "big"); i += n
    return tag, b[i:i+ln], i+ln
_, spki, _ = tlv(b, 0); _, alg, i = tlv(spki, 0); _, _, _ = tlv(alg, 0)
_, bs, _ = tlv(spki, i); _, pk, _ = tlv(bs, 1); _, nb, j = tlv(pk, 0); _, eb, _ = tlv(pk, j)
n = int.from_bytes(nb, "big"); e = int.from_bytes(eb, "big")
k = (n.bit_length() + 7) // 8
def enc(msg: bytes) -> str:
    pad = bytearray()
    while len(pad) < k - 3 - len(msg):
        x = secrets.token_bytes(1)[0]
        if x:
            pad.append(x)
    em = b"\x00\x02" + bytes(pad) + b"\x00" + msg
    return base64.b64encode(pow(int.from_bytes(em, "big"), e, n).to_bytes(k, "big")).decode()
print(n.bit_length(), e, len(enc(b"test")))   # 1024 65537 172
```

## 附录 B：来源清单

**批改网官方（公开）**

- 官方开放平台手册首页：<http://api.pigai.org/apibook/>
- 授权流程：<http://api.pigai.org/apibook/handbooks/workflows.html>
- 作文分析（异步）：<http://api.pigai.org/apibook/resources/essays_async.html>
- 读后续写（异步）：<http://api.pigai.org/apibook/resources/rw_async.html>
- 开发者流程 / 性能指标：<http://api.pigai.org/apibook/intro/tutorial.html>
- Open API 使用协议：<http://api.pigai.org/apibook/api.html>
- 《批改网API接入申请表》：<http://api.pigai.org/static/docs/%E6%89%B9%E6%94%B9%E7%BD%91API%E6%8E%A5%E5%85%A5%E7%94%B3%E8%AF%B7%E8%A1%A8.doc>

**站点资源（脱敏样本已落 `data/recon/pigai/raw/`）**

- 首页（含登录表单）：<http://www.pigai.org/>
- 登录逻辑（145 行，未混淆）：<https://cdn.pigai.org/res/2013/2020.js>
- JSEncrypt 库：<http://www.pigai.org/res/javascript/jsencrypt.min.js>

**第三方（**未采信为事实**，仅作线索）**

- 油猴脚本 `whatsBeeHive/pigaiwang-toolkit`：<https://github.com/whatsBeeHive/pigaiwang-toolkit>
  —— 自述「强制补交：模拟请求会被服务器端拒绝」；提及批改网以**键盘监听**实现禁止粘贴、存在查重与时间限制开关。**均为第三方说法，未验证，本报告未据此设计任何行为。**

**本项目（本地路径）**

- `pyproject.toml:15-16` —— `dependencies = []`（核心零依赖）
- `src/kedaibiao/platforms/base.py` —— 适配器契约 `PlatformAdapter` / `Capabilities` / `AdapterContext`
- `src/kedaibiao/browser/session.py:134` —— `state_path(platform)` 多账号覆盖 bug
- `src/kedaibiao/bank/store.py:348` —— `already_done` 幂等假设
- `src/kedaibiao/tasks/pipeline.py:261-267,303` —— `dry_run` 打印形态、`_extract_verdicts` 约定格式
- `src/kedaibiao/tasks/answerer.py:90` —— `ESSAY` 返回空
- `scripts/pigai_probe.py` —— 本报告的只读探针
- `docs/research/uai-core.md` —— 本报告沿用的格式与纪律来源
