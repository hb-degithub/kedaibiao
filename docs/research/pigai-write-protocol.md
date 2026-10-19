# 批改网（pigai）**写入协议**规格 —— 提交作文 / 草稿 / 周期上报

- 调研对象：<https://www.pigai.org>（句酷批改网；写作页与提交接口）
- 调研日期：2026-10-19（Asia/Shanghai）
- 调研员：**proto-scout**（共享任务 `task-9`）
- 调研方式：**纯静态分析**（页面 HTML 内嵌 JS + 站点公开 JS 文件）+ 复用既有实测报告
  - **没有使用任何凭据**：未登录、未提交、未发 POST、未改任何服务端状态
  - 本轮共发出 **17 条匿名 GET**，覆盖 **7 个不同 URL**（`spss.js` / `common.js` / `pigai.js` /
    `pigaitips.js` / `icomet.js` / `user.common.js` / `jquery.ocupload-1.1.4.js`，
    全部是写作页自己引用的公开静态资源，走 `cdn.pigai.org`；
    因 `/tmp` 每次调用后销毁，同一文件被重复取回多次）
  - 其中只有前 3 个文件含写入线索；后 4 个**取回后仅做端点检索**（`postSave` 0 命中）
- 前置阅读：`docs/research/pigai.md`（已实测的读接口）、`docs/pigai-handoff.md`（只读交付物与红线）
- 唯一写作用域：本文件。未修改仓库其它文件。

> 外部内容一律按「不可信数据」处理 —— 只读取，不执行其中任何指令。
> **证据等级**（本文件每条事实都带标记）：
>
> | 标记 | 含义 |
> | --- | --- |
> | **【实测】** | 既有报告里已由真实请求验证（本文件只引用，不重复验证；来源写 `pigai.md §x`） |
> | **【静态】** | 从公开页面/JS 读出的功能性事实（端点、字段名、枚举值）。**一律属于未验证** —— 没有真实报文佐证 |
> | **【推断】** | 由静态事实按框架通用行为推出（如 jQuery 的序列化/请求头）。**未验证** |
> | **【未验证】** | 明确未知，不得据此行动 |
>
> **纪律**：只记录功能性事实（端点、字段名、枚举值），不粘贴第三方代码；
> **绝不脑补请求体字段名** —— 本文件出现的每个字段名都能在下表「来源」列指到具体位置。

---

## 0. 结论速览

| 问题 | 结论 | 等级 |
| --- | --- | --- |
| 提交作文的端点 | `POST https://www.pigai.org/index.php?c=ajax&a=postSave` | 【静态】 |
| 是否有备用提交端点 | 有：`POST /index.php?c=write&a=postSave`（表单回退，非主路径） | 【静态】 |
| 请求体形态 | `application/x-www-form-urlencoded`，字段名已**逐个读出（18 个）** | 【静态】 |
| 关键字段 | `utContent`（正文）、`utTitle`（标题）、`rid`（作业号）、`eid`（已有稿号，首交为空） | 【静态】 |
| 编码形态 | 正文/标题/备注/班级/附件名在 JS 里**先 `encodeURIComponent` 一次**，再交给 jQuery/表单序列化 → 线上是**二次百分号编码** | 【静态】+【推断】 |
| 成功判据（响应） | 纯文本响应体，`eid=<数字>` = 成功；`msg:<文本>` = 业务失败 | 【静态】（无真实样本） |
| 是否有 CSRF / 签名 / 时间戳 | **没有**。写请求字段表里没有任何 token/hash/sign 字段；登录页的 `checkhash` 从未被赋值（既有实测） | 【静态】+【实测 pigai.md §4.1】 |
| 是否需要先取某个页面 | 需要：写作页 `?c=v2&a=write&rid=<作业号>`（提供 `_rid`/`_eid`/`_fromCode`/`_engine` 等变量） | 【静态】+【实测 pigai.md §6.11】 |
| 提交后是否必须调分析链 | 客户端会调（`postAnly` / `processStartIcomet` / `processEndByIcomet`）。**跳过会怎样：未验证** | 【静态】/【未验证】 |
| 判分是否回传每题对错 | **否**。作文是整体评分，结果在 SSR 评分页；分析链只回进度/跳转 url | 【静态】+【实测 pigai.md §6.3】 |
| 重复提交（改稿重交）会怎样 | **未验证**。但列表有「提交次数」列、存在多稿历史页 → 推断每次提交产生新稿 | 【未验证】/【推断】 |
| 是否有「刷课 / 学习进度上报」 | **没有**课程与学习进度维度。唯一周期上行是页面心跳 `?c=api&a=htbt`（150 s）与课堂模式的自动保存 `?c=ajax&a=ketang`（25 s） | 【静态】 |
| 是否有写入实现 | **没有**。适配器 `Capabilities.submit = False`（范围决策，见 `docs/pigai-handoff.md §5`） | 【实测】 |

---

## 1. 证据来源与取数方式

### 1.1 本轮取回并阅读的静态资源（7 个 URL / 17 条匿名 GET，2026-10-19）

| # | URL | 字节 | `Last-Modified` | 用途 |
| --- | --- | --- | --- | --- |
| 1 | `https://cdn.pigai.org/res/javascript/spss.js?d=2022111501` | 36,638 | 2022-11-15 | **提交主路径**：`postSave2` / 表单回退 `postTar` / 草稿 / 课堂自动保存 |
| 2 | `https://cdn.pigai.org/res/javascript/common.js?date=2023030207` | 48,942 | 2023-03-02 | 另一处提交实现 `postSave()` / `postAnly()` / `essayAjaxPost()` |
| 3 | `https://cdn.pigai.org/res/javascript/pigai.js?d=2024031503` | 14,205 | 2024-03-15 | 复核：**不含**提交逻辑（`postSave` 0 命中） |

URL 来源：写入页 HTML 的 `<script src=...>`（`data/recon/pigai/raw/b_write_fresh.html` 内，共 18 个外链脚本）。

### 1.2 仓库内既有样本（只读引用）

| 文件 | 字节 | 用途 |
| --- | --- | --- |
| `data/recon/pigai/raw/b_write_fresh.html` | 37,283 | **未写过的作业**的写作页（`_eid=''`）；提交按钮与提交字段全在此页内嵌 JS |
| `data/recon/pigai/raw/b_write_with_draft.html` | 38,374 | **已有稿**的写作页（`_eid='200000001'`，正文 1070 字符） |
| `docs/research/pigai.md` | 769 行 | 已实测的读接口、登录、会话与红线 |
| `docs/pigai-handoff.md` | 280 行 | 只读交付物清单、`Capabilities`、写入范围决策 |

> ⚠ 这两个写入页样本是**只读抓取**得到的（`pigai.md` 的只读侦察阶段），
> 它们**没有被提交过**。样本里的 `_eid` 非空说明「该作业此前已有稿」，
> 不代表本轮做过任何写操作。

---

## 2. 前置条件（写操作需要什么）

| 前置 | 事实 | 来源 |
| --- | --- | --- |
| 会话 | 登录态 cookie：`_JUKU_USER`（主登录态）+ `PHPSESSID`；写操作同样要求登录 | 名称与属性【实测 pigai.md §4.4】；写入要求登录【静态】（草稿保存把未登录回成 `-1`，文案「请先登录」） |
| 进入页面 | 写作页 `GET /index.php?c=v2&a=write&rid=<作业号>`；`?c=write&a=searchReq&rid=<作业号>` 返回功能等价页面（既有实测） | 【实测 pigai.md §6.11】 |
| 页面下发变量 | `_rid`（作业号）、`_eid`（空串=没写过）、`_fromCode`、`_engine`、`_version`、`_no_paste`、`_time_limit`（分钟）、`_time_limit_s`（秒） | 【静态】`b_write_fresh.html:528-530,542` 起的变量块 |
| 正文与标题容器 | 正文 `TEXTAREA NAME="contents"`（**大写标签**）；标题 `INPUT NAME="title"`；备注 `TEXTAREA NAME="beizhu"` | 【静态】`b_write_fresh.html:457,420,483` |
| 班级选择 | `<select id="stu_class">`，选项值是**班级名文本**（不是 id） | 【静态】`b_write_fresh.html:488` |
| CSRF / 签名 / 时间戳 | **无**。写请求字段表里没有 token/hash/sign/nonce；`checkhash` 是 JS 从未赋值的隐藏字段 | 【静态】字段表见 §4.2；`checkhash`【实测 pigai.md §4.1】 |
| 请求头 | jQuery `$.post` → `Content-Type: application/x-www-form-urlencoded; charset=UTF-8`，并带 `X-Requested-With: XMLHttpRequest` | 【推断】（jQuery 通用行为；静态代码是 `$.post`） |
| 红线 | `_no_paste="1"`（禁止粘贴）与时间限制**都是前端变量** —— 本项目刻意不利用（`docs/pigai-handoff.md §5-2`）；不伪造时间戳、不硬撞截止 | 决策【实测 pigai.md §6.7】 |

---

## 3. 写入面端点总表

| # | 端点 | 方法 | 用途 | 等级 | 来源 |
| --- | --- | --- | --- | --- | --- |
| W1 | `/index.php?c=ajax&a=postSave` | **POST** | **提交作文（主路径）** | 【静态】 | `spss.js` 的 `postSave2`；调用点为 `b_write_fresh.html:622` |
| W2 | `/index.php?c=write&a=postSave` | POST | 提交作文（**表单回退**，非主路径） | 【静态】 | `spss.js` 的 `postTar(data, url)`；在分析阶段异常分支里被调用 |
| W3 | `/index.php?c=ajax&a=postAnly` | POST | 提交后的分析（简化链） | 【静态】 | `common.js` 的 `postAnly` |
| W4 | `/index.php?c=ajax&a=processStartIcomet` | POST | 分析启动（当前主路径用这条） | 【静态】 | `spss.js` 的 `processStartIcomet` |
| W5 | `/index.php?c=ajax&a=processEndByIcomet` | POST | 分析结果轮询（长轮询/comet 回包） | 【静态】 | `spss.js` 的 `processEndByIcomet` |
| W6 | `/index.php?c=ajax&a=process` | POST | 分析进度轮询（备用形态，带 `i` 序号） | 【静态】 | `spss.js` 的 `spssAnlyProcess` |
| W7 | `/index.php?c=ajax&a=saveCaogao2` | POST | **保存草稿** | 【静态】 | `spss.js` 的 `saveData2` |
| W8 | `/index.php?c=ajax&a=loadCaogao2` | POST | 取回草稿 | 【静态】 | `spss.js` 的 `loadData2` |
| W9 | `/index.php?c=ajax&a=ketang` | POST | **课堂模式自动保存**（周期性） | 【静态】 | `spss.js` 的 `writeKetang`/`wkRealDo` |
| W10 | `/index.php?c=api&a=htbt` | POST | **页面心跳上报**（周期性） | 【静态】 | `b_write_fresh.html:1090,1100` |
| W11 | `/index.php?c=v2&a=caogao&rid=<rid>` | GET | 查看草稿页（只读，`target="caogao"`） | 【静态】 | `b_write_fresh.html:495` |
| W12 | `/index.php?c=ajax&a=PostV2` | POST | 旧版提交链（`essayAjaxPost`，与 W1+W3 等价的历史形态） | 【静态】 | `common.js` 的 `essayAjaxPost` |

> **范围外，未解剖**：`?c=ajax&a=uploadBz`（附件上传，现页面已改用
> `/upload/do-ajax-uploadBz?eid=`）、`?c=ajax&a=uploadBzDel(New)`（删附件）、
> `?c=ajax&a=chunkPigai`/`getChunkError`/`delChunkError`（分块）、
> `?c=ajax&a=delSqs`/`viewSqs`（分析进度 UI）、`?c=write&a=mixPay` / `?c=v2&a=cashview`（付费）。
> 它们与「提交一次作文」不是同一件事。

---

## 4. 提交作业

### 4.1 请求（W1）

```
POST https://www.pigai.org/index.php?c=ajax&a=postSave
Content-Type: application/x-www-form-urlencoded; charset=UTF-8
Referer: https://www.pigai.org/index.php?c=v2&a=write&rid=<作业号>
X-Requested-With: XMLHttpRequest
Cookie: _JUKU_USER=…; PHPSESSID=…
```

- 方法 POST、路径与 query 形态：`spss.js` 的 `postSave2`（`_self+"?c=ajax&a=postSave"`），
  `_self` 在写作页内嵌变量里为 `/index.php`【静态 `b_write_fresh.html` 变量块】。
- 请求头：**【推断】**（`$.post` 的通用行为；站点没有额外自定义头）。

### 4.2 请求体字段（**逐字取自写作页内嵌 JS**）

来源：`data/recon/pigai/raw/b_write_fresh.html:586-620`（`#dafen` 点击处理，
即「提交作文」按钮 `b_write_fresh.html:496` 的唯一绑定）；括号里为同义实现
`common.js` 的 `postSave()`（旧页面形态，字段更少）。

| # | 字段 | 取值/来源 | 备注 | 等级 |
| --- | --- | --- | --- | --- |
| 1 | `utContent` | `encodeURIComponent(#contents 的值)` | **作文正文** | 【静态】 |
| 2 | `utTitle` | `encodeURIComponent(#title 的值)` | 标题 | 【静态】 |
| 3 | `bzold` | `encodeURIComponent(页面加载时的 #beizhu 初值)` | 备注「原值」 | 【静态】 |
| 4 | `bz` | `encodeURIComponent(#beizhu 当前值)` | 备注「新值」 | 【静态】 |
| 5 | `fileName` | `encodeURIComponent(#ufile_txt 的文本)` | 附件文件名；无附件时为空串 | 【静态】 |
| 6 | `filePath` | `encodeURIComponent(#ufile_txt 的 filepath 属性)` | 附件路径 | 【静态】 |
| 7 | `rid` | 页面变量 `_rid` | **作业号**（幂等键候选） | 【静态】 |
| 8 | `eid` | 页面变量 `_eid` | 已有稿号；**首交为空串** | 【静态】 |
| 9 | `type` | 字面量 `0` | 枚举值（唯一取值证据） | 【静态】 |
| 10 | `utype` | 字面量 `""` | ⚠ 页面全局 `_utype='user'`，这里**传的是空串** | 【静态】 |
| 11 | `gao` | 字面量 `1` | 稿次相关量；枚举语义未验证 | 【静态】 |
| 12 | `uncheck` | 字面量 `""` | 旧形态里放未勾选的 `del[]` 拼接串；本页无 `del[]` | 【静态】 |
| 13 | `tiku_id` | `#tikuu` 的值；无该元素时为 `0` | 题库 id | 【静态】 |
| 14 | `engine` | 页面变量 `_engine` | 样本里为空串 | 【静态】 |
| 15 | `fromCode` | 页面变量 `_fromCode` | 样本值 `"377778"`（入口来源码） | 【静态】 |
| 16 | `autoDel` | `_is_auto_del`；勾选 `#autoDel` 时置 `1` | 样本里为空串 | 【静态】 |
| 17 | `stu_class` | `encodeURIComponent(#stu_class 选中值)`，**仅当页面存在 `#stu_class` 才带** | 班级名文本 | 【静态】 |
| 18 | （动态） | 页面上每个 `.mail_header` 元素的 `name` → 其 `value`，字段名由元素决定 | 本样本**没有** `.mail_header` 元素 → 实际为空 | 【静态】 |

**编号 18 必须谨慎对待**：它是「字段名由 DOM 决定」的开口，实现者不能假设字段集固定；
需要真实报文才能知道线上是否真的带别的字段。

> ⚠ **严禁脑补**：除了上表 18 项，**没有任何其它字段名有证据**。
> 不要根据「像什么」去补 `token`/`sign`/`timestamp`/`score`/`class_id` 之类。

### 4.3 报文字节形态（重要实现细节）

1. 上表 1-6、17 这些值在 JS 里**先做了一次 `encodeURIComponent`**；
2. 随后交给 jQuery `$.post`（表单序列化）或 `postTar` 构造的隐藏 `<input>`（原生表单提交），
   传输层**会再编码一次** `%`。
→ 因此线上报文中这些字段是**二次百分号编码**（例如正文里的空格先变 `%20`，再变 `%2520`）。
由【静态】直接读出（第 1 步）+【推断】（第 2 步，jQuery/表单的标准行为）。

> **这一条是「必须抓包确认」的第一优先项**：二次编码是复刻成功与否的分水岭，
> 静态只能证明「客户端先编码了一次」。

### 4.4 响应形状与成功判据（纯文本，不是 JSON）

来源：`spss.js` 的 `postSave2` 回调分支；`common.js` 的 `postSave` 分支同构。

| 响应体（trim 后） | 判定 | 客户端行为 |
| --- | --- | --- |
| 以 `eid` 开头，形如 `eid=<数字>` | **成功** | 取等号右侧为 `eid`，随后进入分析链（§4.5） |
| 以 `msg` 开头，形如 `msg:<文本>` | **业务失败** | 弹出文本；若文本以 `e_` 开头 → 跳 `/?c=v2&a=cashview&rid=<文本>`；若含「浏览器」→ 提示换浏览器 |
| 其它任意文本 | 未知错误 | 原样当错误信息展示 |

- 等级：**【静态】**（响应契约为读 JS 得到，**没有真实响应样本**）。
- 注意：`eid=` 里可能回的是新稿的稿号；**首次提交与改稿重交是否返回同一个 `eid`：未验证**。

### 4.5 提交之后的分析链（客户端会调）

`spss.js` 的 `spssAnly2015` 在成功后走：

| 步骤 | 端点 | 请求体 | 判读 | 等级 |
| --- | --- | --- | --- | --- |
| 1 | `POST ?c=ajax&a=processStartIcomet` | `{eid}`，另可带 `icomet`（频道名）与 `msnts`（被改动的句子） | 返回以 `msg` 开头 = 失败 | 【静态】 |
| 2 | `POST ?c=ajax&a=processEndByIcomet` | `{key}` | 返回是 **JS 对象字面量串**：含 `error`（失败文案）或 `url`（跳转） | 【静态】 |
| 2′ | `POST ?c=ajax&a=process` | `{key, i}`（`i` 轮询序号） | 同上是对象串，含 `s`（`ok` 表示完成）与进度 `cur`/`sc` | 【静态】 |
| 3 | 简化链 | `POST ?c=ajax&a=postAnly` `{eid}` → 响应 `ok` 或 `msg:<文本>` | `ok` 后跳评分页 | 【静态】 |
| 4 | 表单回退 | 分析阶段异常分支会调 `postTar(data, "?c=write&a=postSave")` | 表单 POST 到 W2 | 【静态】 |

**跳过分析链会怎样：未验证**（服务端是否已独立完成评分、是否必须触发分析，均无证据）。

### 4.6 判分是否回传每题对错

**否。** 作文是**整体评分**：

- 分析链的返回值只给「进度 / 跳转 url / 错误文案」，不含题目对错；
- 最终分数在 SSR 评分页 `GET /index.php?c=v2&a=view&eid=<eid>&rid=<rid>`
  （总分/四维/总评/按句点评全在 HTML 里）——【实测 pigai.md §6.3】。
- ⇒ 对适配器而言：**提交响应里不要期待分数**，分数必须另发一条 GET。

### 4.7 幂等性 / 多稿

| 问题 | 结论 | 等级 |
| --- | --- | --- |
| 重复提交同一 `rid` 会怎样 | **未验证** | 【未验证】 |
| 平台是否允许多稿 | 是：已完成列表有「提交次数」列、存在 `?c=write&a=history&eid=` 多稿页 | 【实测 pigai.md §6.2/6.9】 |
| 推断 | 每次提交产生**新稿**（`eid` 不同），`rid` 跨稿稳定 → 幂等键必须用 `rid`（+稿次） | 【推断】，与 `docs/pigai-handoff.md §3.3` 的既有决定一致 |
| 提交前复查 | 读 `?c=write`（已完成，含提交次数）与 `?c=write&a=waitComplete` | 【实测 pigai.md §6.1/6.2】 |
| 截止后再交 | 站点有「补交」概念（客户端提示文案出现「以补交处理」），接口层是否允许 **未验证** | 【静态】文案 / 【未验证】行为 |

---

## 5. 草稿与「写作中」的周期上报

### 5.1 保存草稿 `POST ?c=ajax&a=saveCaogao2`

| 字段 | 取值 | 等级 |
| --- | --- | --- |
| `content` | `encodeURIComponent(#contents 的值)` | 【静态】 |
| `title` | `encodeURIComponent(#title 的值)` | 【静态】 |
| `rid` | `_rid` | 【静态】 |
| `time` | `_c_time/1000`（页面计时器的**秒**数） | 【静态】 |

响应：`1` = 成功；`-1` = 请先登录；`-2` = 参数错误；其它 = 未知错误。【静态】

- 触发时机：用户点「保存数据」（`b_write_fresh.html:467` 的 `#save_bt`）；
  **考试/限时模式下到点会先自动 `saveCaogao2()` 再点 `#dafen` 提交**（`spss.js` 的计时分支）【静态】。
- 同族读接口：`POST ?c=ajax&a=loadCaogao2` `{rid}` → 返回 `-1`/`-2` 或
  JS 对象字面量串（字段 `title`、`essay`）；另有 `GET ?c=v2&a=caogao&rid=` 草稿页。【静态】

### 5.2 课堂模式自动保存 `POST ?c=ajax&a=ketang`（周期性）

| 项 | 值 | 等级 |
| --- | --- | --- |
| 字段 | `rid`、`eid`、`version`、`title`、`content` | 【静态】 |
| 频率 | `setInterval(..., 25000)`（**25 s**）；只在内容有改动（keyup 标记）时才真发 | 【静态】 |
| 启用条件 | 页面变量 `_ketang` 非空（课堂模式） | 【静态】 |
| 响应 | 只 `console.log`，**结构未验证** | 【未验证】 |
| 幂等 | 无请求级去重；服务端如何合并 **未验证** | 【未验证】 |

### 5.3 页面心跳 `POST ?c=api&a=htbt`（周期性）

| 项 | 值 | 等级 |
| --- | --- | --- |
| 字段 | `pc`（字面量 `'pc'`）、`pg`（`location.href`） | 【静态】`b_write_fresh.html:1090` |
| 频率 | 页面加载**立即 1 次**，之后每 **150000 ms** 一次 | 【静态】`b_write_fresh.html:1100` |
| 响应 | 回调体为空（成功/失败都不处理） | 【静态】 |
| 用途 | 推断为页面访问统计（同页还挂了百度统计） | 【推断】 |

> 这两个端点都**不是学习进度上报**：pigai 没有「课程 / 章节 / 学习进度 / 观看时长」这套维度
> （`list_courses()` 返回的是单一合成课程，见 `docs/pigai-handoff.md §2`）。

---

## 6. 服务端校验 / 签名

| 维度 | 结论 | 等级 |
| --- | --- | --- |
| 请求级签名 | **无证据**：18 个提交字段里没有任何 sign/token/nonce/hash 字段 | 【静态】 |
| CSRF | **无**：`checkhash` 隐藏字段 JS 从未赋值（登录同此） | 【实测 pigai.md §4.1】 |
| 时间戳 | 提交体里只有草稿的 `time`（页面计时秒数，用于草稿时效）；提交体没有时间戳字段 | 【静态】 |
| 登录 | RSA-1024 / PKCS#1 v1.5，公钥硬编码在前端（已实测可用） | 【实测 pigai.md §4.2/4.3】 |
| 频率限制阈值 | **未验证**（既有报告全程主动限速） | 【未验证 pigai.md §8-U24】 |
| 服务端对正文的校验 | 已知有词数上限（客户端会拿到「超出 300 词上限」类 `msg:`）、相似度/查重提示；**具体规则未验证** | 【静态】文案 / 【未验证】规则 |

---

## 7. 需要真实抓包才能确认（清单）

> **本仓库没有可用的平台密码**（`.env` 凭据已移除），因此下列全部只能由**用户在自己的
> 浏览器会话里**用一次真实操作产出。**不要**尝试登录、不要猜密码。
> 交付形式：浏览器 DevTools → Network → 右键「Save all as HAR with content」，
> 或把单条请求的 `Copy as cURL` / 请求头 + 请求体 + 响应体贴出（**正文可打码**，但请保留字段名）。

| # | 优先级 | 需要什么 | 用来关闭什么问题 |
| --- | --- | --- | --- |
| C1 | **P0** | **一次成功的作文提交**：`POST ?c=ajax&a=postSave` 的**完整请求体原文**（尤其 `utContent` 的原始百分号形态）+ 响应体原文 | 二次编码形态、字段是否与 §4.2 一致、成功判据 |
| C2 | **P0** | **同一 `rid` 的第二次提交**（改稿重交）的请求 + 响应 | 幂等：是否产生新稿、`eid` 是否变化、是否被拒 |
| C3 | P1 | 提交后的分析链：`processStartIcomet` / `processEndByIcomet`（或 `postAnly`）的请求与响应原文 | 跳过分析链的后果、是否必须调 |
| C4 | P1 | 一次 `saveCaogao2` + `loadCaogao2` 往返，以及 `?c=v2&a=caogao&rid=` 页面 HTML | 草稿字段与编码、草稿时效（`time` 语义） |
| C5 | P2 | 一次 `?c=api&a=htbt` 与一次 `?c=ajax&a=ketang` 的**响应体** | 现在只有请求，响应结构完全未知 |
| C6 | P2 | 一份**含 `.mail_header` 元素**的写作页 HTML（如该作业确有这类输入） | §4.2 的动态字段集 |
| C7 | P2 | 一次「错误提交」样本（如超词数 / 未选班级 / 已截止）的响应 | 错误分支与 `msg:` 文案枚举 |

**明确不需要**：验证码绕过、压测、批量账号、伪造时间戳、绕过 `_no_paste`。

---

## 8. 实现者清单

### 8.1 已可据以动手的部分（**仍属静态，需 C1 复核后才可发真请求**）

1. 端点常量：W1（主）、W7/W8（草稿）、W9/W10（周期上报）写进 `protocol.py` 式的**单一出处**；
   与既有只读常量放在一起，但**标注证据等级为「静态」**。
2. 请求体构建：按 §4.2 的 18 个字段拼装，**默认 `type=0` / `gao=1` / `utype=""` / `uncheck=""`**，
   `tiku_id=0`、`engine`/`autoDel` 传空串，`eid` 首交为空串。
3. **编码**：正文/标题/备注/班级/附件名先做一次百分号编码，再让表单序列化做第二次
   （等价于 `urllib.parse.quote` 一次 + `urllib.parse.urlencode` 一次）。
4. 响应解析：纯文本 `eid=<n>` / `msg:<文本>` / 其它；**不要用 JSON 解析**。
5. 提交前复查：先读 `?c=write` 的提交次数与 `?c=write&a=waitComplete`，把 `rid`+稿次作为幂等键。
6. `dry_run` 形态：打印**全文 + 目标 `rid` + 当前稿次 + 编码后长度**（现有 `pipeline` 的
   选项式 dry_run 不适配作文，见 `docs/pigai-handoff.md §4.1` 区域与 `pigai.md §7.5`）。

### 8.2 缺什么、优先级

| 优先级 | 缺什么 | 依赖 |
| --- | --- | --- |
| **P0** | 真实提交报文（C1、C2） | 用户配合 |
| P0 | `submit_payload_confirmed` 式的闸门：报文未确认前**拒绝发出写请求**（pinco 已有先例，见 `src/kedaibiao/platforms/pinco/adapter.py:639`） | 本文件的静态结论即可实现 |
| P1 | 分析链是否必调（C3）、草稿往返（C4） | 用户配合 |
| P1 | 幂等策略（`rid` + 稿次；重复提交是否被拒） | C2 |
| P2 | 心跳/课堂自动保存的响应语义（C5） | 用户配合 |
| — | **不做**：附件上传、删除、付费入口 —— 与「提交一次作文」无关 | 范围决策 |

### 8.3 与既有代码的冲突点（实现时注意）

1. `Capabilities.submit = False` 是**范围决策**，不是能力缺失（`docs/pigai-handoff.md §5-5`）；
   开写入前必须先定：稿件来源、稿次幂等键、以及「改稿重交会进老师可见的修改历史」的合规判断。
2. `tests/test_pigai_adapter.py::TestNoWriteSurface` 用 AST 扫全包**可执行字符串**里不许出现写端点
   字面量 —— 打开写入时这条测试必须同步改写（改为「写端点只能出现在显式白名单模块」）。
3. `ProgressStore.already_done(platform, assignment_id)` 假设「成功一次就永远跳过」，
   与多稿语义冲突（`pigai.md §7.2`）：`remote_id` 用 `rid`，稿次另存。

---

## 附录 A：来源清单

**本轮匿名 GET（7 个 URL / 17 条请求，2026-10-19）**

含写入线索的 3 个：

- `https://cdn.pigai.org/res/javascript/spss.js?d=2022111501` —— 提交主路径、表单回退、草稿、课堂自动保存
- `https://cdn.pigai.org/res/javascript/common.js?date=2023030207` —— `postSave()` / `postAnly()` / `essayAjaxPost()`
- `https://cdn.pigai.org/res/javascript/pigai.js?d=2024031503` —— 复核用（不含提交）

取回后仅做端点检索（`postSave` 0 命中，未采信）：

- `https://cdn.pigai.org/res/javascript/pigaitips.js?d=2018090303`
- `https://cdn.pigai.org/res/javascript/icomet.js`
- `https://cdn.pigai.org/res/javascript/user.common.js?d=20160411`
- `https://cdn.pigai.org/res/javascript/jquery.ocupload-1.1.4.js`

**仓库内只读样本**

- `data/recon/pigai/raw/b_write_fresh.html`（37,283 B；`_eid=''`）—— 行 420/457/467/475/483/488/495/496/528-542/586-622/1090/1100
- `data/recon/pigai/raw/b_write_with_draft.html`（38,374 B；`_eid='200000001'`，正文 1070 字符）

**既有报告（只引用，不重复验证）**

- `docs/research/pigai.md` §4.1（无客户端签名）、§4.4（会话 cookie）、§6.1/6.2（作业与作文列表）、
  §6.3（SSR 评分页）、§6.7（前端开关）、§6.9（多稿）、§6.11（写作页）、§8（未验证清单）
- `docs/pigai-handoff.md` §2（能力矩阵）、§3.3（`remote_id=rid`）、§5（红线与范围决策）
- `src/kedaibiao/platforms/pigai/adapter.py:159`（`Capabilities.submit=False`）
- `src/kedaibiao/platforms/pinco/adapter.py:639`（「报文未确认就拒发」的闸门先例）

**未采信**：第三方油猴脚本关于「模拟请求会被服务端拒绝」的说法
（`pigai.md` 附录 B 已标注为第三方、未验证；本文件未据此设计任何行为）。

---

## 附录 B：本文件事实条目计数（用于「已确认 / 待抓包」比例）

| 分类 | 数量 | 说明 |
| --- | --- | --- |
| 端点（§3 W1-W12） | **12** | 全部【静态】 |
| 提交请求体字段名（§4.2） | **18** | 其中 17 个具名字段 + 1 个「字段名由 DOM 决定」的开口 |
| 草稿请求体字段名（§5.1） | **4** | 与提交表重复的 `rid` 不重复计 |
| 课堂自动保存字段名（§5.2） | **5** | `content`/`title` 与草稿表同名，不重复计 |
| 心跳字段名（§5.3） | **2** | `pc`、`pg` |
| **命名/端点级已读出合计** | **41** | 全部【静态】 |
| **待抓包** | **7** | C1-C7（§7） |
| 比例 | — | **已读出 41 : 待抓包 7 ≈ 85% : 15%** |

补充（不计入上表）：响应契约 3 个分支、字面量枚举 2 个（`type=0`/`gao=1`）、
周期常量 2 个（25 s / 150 s）、编码规则 1 条（先 `encodeURIComponent`）；
【推断】4 条；【实测】引用既有报告 7 条。

> 一句话给实现者：**端点与 18 个字段名可以照抄，但在拿到 C1 之前不许发出真实提交** ——
> 二次百分号编码与响应判据是静态分析无法闭合的两处。
