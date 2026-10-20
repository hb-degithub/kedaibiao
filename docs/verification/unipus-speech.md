# U校园语音评测链路验证报告（2026-10-20）

- 验证者：**unipus-speech 队友**（task-4 实现者）
- 实现文件：`src/kedaibiao/platforms/unipus/speech.py`（新建，未改任何共享文件/其他队友文件）
- 单测：`tests/test_unipus_speech.py`（100 条，**全部离线**）
- 规格依据：`docs/research/unipus-protocol.md` §3.5 / §4.3 / §4.4 / §10（U9、U11）；
  收尾时并入 `docs/research/uai-core-submit-facts.md` §8 的**帧序列一句话摘要**
- 参考实现许可：`Yunlk/uai-core` 是**非商业许可**（`docs/license-policy.md` §3.1）。
  本模块只使用了 clean-room 规格里的**功能事实**（端点、常量、算法参数），
  **没有复制任何代码/注释/命名**；实现是纯标准库独立编写
- **线上访问：零。** 本报告与本次实现均未向 `*.unipus.cn` 发出任何请求；
  所有 socket 都是单测里的假对象（断网可跑）

> 结论一句话：**签名与认证帧是源码事实、且已被固定向量锁死；帧序列只到「有哪几段」
> 的粒度；上传方式、分片规则、结果结构、服务端是否接受签名 —— 全部未实测。**
> 因此模块默认干跑，未知项一律显式参数 + 默认关闭 + 闸门，没有一处「猜死」。

---

## 0. 结论表

| # | 结论 | 证据等级 | 依据 / 详见 |
| --- | --- | --- | --- |
| 1 | `sig = SHA1(applicationId + secret + timestamp)`，`timestamp` 为**秒级字符串** | 【源码事实】 | 规格 §3.5（`uai-core@4628e44 src/api/speech_eval.rs#L29-L36`、`#L338-L345`、`#L357-L375`） |
| 2 | `applicationId = 138000000000001`、`secret = example-speech-eval-secret-0000000001`、认证帧 `alg: "sha1"` | 【源码事实】 | 同上 |
| 3 | 主通道 `wss://speech.unipus.cn/speech/proxy/wss`、备用 `wss://speech-eval.unipus.cn/speech/proxy/wss` | 【源码事实】 | 规格 §1、§4.4（`speech_eval.rs#L89`、`#L92`） |
| 4 | 固定 timestamp → 固定 sig（`1700000000` → `b6f8dff0…bb8b`） | 【离线实测】 | 本报告 §2.2、`tests/test_unipus_speech.py::TestSignature` |
| 5 | 认证帧的**完整键名**（`applicationId/timestamp/sig/alg`）、是否携带 `secret`、摘要大小写 | **【未验证】** | 规格只逐字给了 `alg` 的值与签名公式；本模块做成参数 |
| 6 | 帧序列：认证帧 → 参数帧（`tokenId`、`audio` 格式、`request.apiName/transcript/sig`）→ PCM 分片 → `{"stop":true}` | 【调研摘要】（一句话粒度，**键名未逐字给出**） | `docs/research/uai-core-submit-facts.md` §8 表第 6 行（来源 `speech_eval.rs#L1-L50`） |
| 7 | 音频上传方式（二进制帧？base64 JSON？分片多大？PCM 参数？） | **【未验证】** | U9；本模块 `upload_mode` 默认 `"none"` |
| 8 | 评测结果 / 错误消息的结构 | **【未验证】** | U9；本模块只做启发式分类并保留全部原始消息 |
| 9 | 服务端是否接受签名、是否限流/校验来源 | **【未验证】** | U9 |
| 10 | `https://zt.unipus.cn/soe/api/csAuth`（取签名）、`https://open-eval.unipus.cn/open_proxy`（HTTP 通道） | 仅登记地址；**行为【未验证】** | 规格 §1（`speech_eval.rs#L95`、`#L98`）。本模块**不实现、不发送** |
| 11 | 评分上报 `POST /course/api/v3/voiceEvaluation` 的字段名与「服务端不评测、只存分」 | 【源码事实】 | 规格 §4.3（`src/api/media.rs#L56-L87`）；研究摘要 §8 表第 2 行 |
| 12 | 评分上报的 `score_details` **结构**与响应体结构 | **【未验证】** | 规格 §4.3 明写「未解析」；本模块原样透传、只按 `code==0` 判接受 |
| 13 | 语音评测分数（常见 0~1）与口语题 `record` 分数（0~100）量纲不同 | 【源码事实】 | 规格 §7.4（`src/question/record.rs#L64-L71`） |
| 14 | `secret` / `applicationId` 是「前端公开 JS 包里的常量」这一说法 | **【未验证】** | 规格 §10 U11（参考实现自述，本次调研未独立核对前端产物） |
| 15 | 全量离线单测通过（100 条，无网络、无真实站点） | 【离线实测】 | 本报告 §6 |

---

## 1. 机制：链路总览

```
口语题（record / role-play / oral-state）
   │  ① 本地拿到录音（本模块不负责录音，也不做音频转码）
   ▼
[语音评测 WSS]  wss://speech.unipus.cn/speech/proxy/wss        ← 主
                wss://speech-eval.unipus.cn/speech/proxy/wss   ← 备用
   │  认证帧（签名）           ← 【源码事实】公式 + alg；键名【推断】
   │  参数帧（tokenId/audio/request.*）← 【调研摘要一句话】形状未逐字给出
   │  音频（PCM 分片？二进制帧？）     ← 【未验证】
   │  {"stop": true}                  ← 【调研摘要一句话】是否必需未验证
   ▼
  评测结果（分数，常见 0~1）          ← 结构【未验证】
   │  ② 分数由**客户端**填进 score_details
   ▼
[评分上报 HTTP] POST https://ucontent.unipus.cn/course/api/v3/voiceEvaluation
   header: x-annotator-auth-token（ucontent 家族；不认 Authorization）
   body:   {block_id, course_id, score_details}
   → 服务端**不评测**，只存客户端算出的分   ← 【源码事实】
```

两条链路的**证据强度完全不同**：签名公式是硬的，链路其余部分几乎全是未知。
本模块据此把能力切成「纯函数 + 干跑计划」与「真连闸门」两层。

---

## 2. 签名与认证帧

### 2.1 公式与常量（【源码事实】）

```
sig       = SHA1(applicationId + secret + timestamp)   # 拼接后按 UTF-8 取摘要
timestamp = 秒级十进制字符串                            # 例 "1700000000"
applicationId = "138000000000001"
secret        = "example-speech-eval-secret-0000000001"
认证帧带 alg  = "sha1"
```

实现：`sign_speech()`、`build_auth_frame()`、`auth_frame_json()`，**全部是纯函数**
（可注入 `clock`，默认 `time.time()`），无网络、无全局状态。

### 2.2 固定向量（【离线实测】）

`SHA1("138000000000001" + "example-speech-eval-secret-0000000001" + "1700000000")`

| timestamp | 小写摘要（默认） | 大写（`uppercase=True`） |
| --- | --- | --- |
| `1700000000` | `b6f8dff0daa5ff41e42443d7f8c7a0ed50ffbb8b` | `B6F8DFF0DAA5FF41E42443D7F8C7A0ED50FFBB8B` |
| `1729400000` | `1e6d794fef9858469a613d69d5ab3cd23c7eaecc` | `1E6D794FEF9858469A613D69D5AB3CD23C7EAECC` |

单测同时做了**独立复算**（测试里自己拼串再 `hashlib.sha1`）与**向量钉桩**
（常量被悄悄改掉时测试会失败），两道保险。

### 2.3 认证帧形状（【推断】+ 闸门）

规格逐字给出的只有 `alg` 的取值；帧的完整键名没有。实现取：

```json
{"applicationId":"138000000000001","timestamp":"1700000000","sig":"<sha1>","alg":"sha1"}
```

- 字段**插入顺序固定**（`applicationId, timestamp, sig, alg`），JSON 紧凑序列化，
  因此同一 timestamp 的帧文本可复现（便于将来与真实抓包逐字比对）；
- `include_secret=False`（默认）：**认证帧是否也要带 secret 未验证**（U11），
  真要带就显式打开；
- `extra_auth_fields`：其它可能字段（nonce/version…）原样并入，**不猜**；
- `uppercase`：摘要大小写规格未写明（登录加密那边就因大小写写错被当密码错误，
  规格 §3.1 有先例），默认小写、可切大写。

干跑输出（`SpeechEvalPlan.describe()`）会把 `secret` 屏蔽成 `***`；
`to_dict()` 保留原文，文档已警示**不要写日志**。

---

## 3. 帧序列、上传与结果（全部未实测）

### 3.1 已知到哪一层

`docs/research/uai-core-submit-facts.md` §8 的摘要说帧序列是：

> 认证帧 + 参数帧（`tokenId`、`audio` 格式、`request.apiName/transcript/sig`）+
> PCM 分片 + `{"stop":true}`

这是**「有哪几段」的粒度**，`speech_eval.rs#L1-L50` 的逐行细节没有落到文档里。
因此本模块的收敛方式是：

| 段 | 实现 | 默认 | 理由 |
| --- | --- | --- | --- |
| 认证帧 | `build_auth_frame` / `plan.frames[0]` | **必发** | 源码事实（公式 + alg） |
| 参数帧 | `plan_speech_eval(param_frame=...)`，调用方给完整对象 | **不发** | 键名/取值未逐字给出，猜不出来 |
| 音频 | `upload_mode ∈ {none, binary, base64-json}`，可 `chunk_size` 分片 | **`none`** | 上传方式未验证（U9） |
| 结束帧 | `send_stop=True`（默认 `{"stop":true}`，可 `stop_frame` 覆盖） | **不发** | 是否必需/还差什么字段未验证 |

帧顺序参数：`auth_first=True`（默认，【推断】认证帧必须首位）；
设 `False` 会把认证帧排到最后并附带警告 —— 这是给「将来实测发现顺序不同」
留的开关，不是本模块推荐值。

### 3.2 音频形态的三种可能（都是【推断】）

- `binary`：音频原样放二进制帧（`chunk_size` 可切成多帧）；
- `base64-json`：`{"audio": "<base64>", "text": "<参考文本>"}`，键名可换
  （`audio_key` / `text_key`）；
- `none`：不发。

**本模块不做音频转码**：要 PCM 就请调用方先转好再传 bytes —— 音频格式转换
依赖第三方库，与「纯标准库」冲突，而且采样率/位宽本身也未验证。

### 3.3 结果结构：不解析死

`looks_like_error()` / `looks_like_result()` / `collect_score_fields()` 是**启发式**：

- 错误：`success/ok is False`、错误码键（`code/errcode/err_no/ret/...`）取值不是
  `0/200/"0"/"ok"...`、`status ∈ {error,failed,fail,failure}`、非空 `error`；
- 结果：顶层或 `data/result/detail/payload` 里出现 `score/total_score/overall/
  fluency/integrity/accuracy/pron...`；
- 两者都不像 → `status="unknown"`，**原始消息全部保留**在 `outcome.messages`。

刻意**不**把 `status` 当错误码（评测结果里的 `status` 很可能是 `"finished"`
之类），避免大面积误判。分数**不做 0~1 → 0~100 换算**（规格 §7.4 提醒两条链
量纲不同，换算比例是产品决定，不是协议事实）。

---

## 4. 评分上报（`POST /course/api/v3/voiceEvaluation`）

### 4.1 【源码事实】

- 端点：ucontent 家族，`https://ucontent.unipus.cn/course/api/v3/voiceEvaluation`；
- 请求体三字段：`block_id`、`course_id`、`score_details`；
- **服务端不评测**，只存客户端给的分数；
- 鉴权：`x-annotator-auth-token`（ucontent 受保护接口**不认** `Authorization`，
  规格 §2.3）；成功码按 ucontent 家族 `code == 0` 判（规格 §5.1）。

### 4.2 【未验证】

- `score_details` 的**结构**（列表/字典/字符串化 JSON 都有可能）；
- `block_id` / `course_id` 的**取值范围与含义**（哪个 block？实例 ID 还是资源 ID？）；
- 响应体结构（规格 §4.3 明写「未解析」）。

### 4.3 实现

- `build_voice_evaluation_plan(...)`：**纯函数**，固定输入 → 固定请求；`score_details`
  原样透传（只要求 JSON 可序列化），不做任何改写；
- `report_voice_evaluation(...)`：**async**，默认干跑；`confirm=True` 时需要注入
  `sender`（契约 `await sender(url=..., headers=..., payload=...) -> 解析后的 JSON`），
  否则返回 `status="blocked"`。本模块**不直接发 HTTP**（对 adapter 零依赖，
  由 Lead 接线）；
- 精度硬约束：`block_id` / `course_id` **拒绝 float**（19 位 ID 过浮点会损坏，
  规格 §6.2），int/str 原样保留、不擅自转类型；
- `describe()` **屏蔽所有请求头取值**（可能是 annotator token），`to_dict()` 保留
  原文并警示不要写日志。

---

## 5. 实现边界：闸门与默认值

| 开关 | 默认 | 作用 |
| --- | --- | --- |
| `confirm` | `False` | **默认干跑**：只构造 plan、**不建任何连接**（单测用 mock 盯着 `socket.create_connection`） |
| `dry_run` | `None` | 传 `True` 时**强制干跑**，即使 `confirm=True`（宁可少发，不可误发） |
| `upload_mode` | `"none"` | 未验证项不猜；真实评测必须显式选一种上传方式 |
| `probe_only` | `False` | 显式允许「只发认证帧」的连通性探测（仍需 `confirm=True`） |
| `param_frame` | `None` | 参数帧形状未验证 → 默认不发，要发就整对象给 |
| `send_stop` | `False` | 结束帧是否必需未验证 → 默认不发 |
| `auth_first` | `True` | 帧顺序【推断】；可显式换序（会警告） |
| `fallback` | `True` | 主通道失败后试备用通道；错误信息里列出两个主机 |
| `sender`（上报） | `None` | 不注入就不发 → `status="blocked"` |

**错误软着陆**（不抛异常，落成 `status` + `error`）：连不上 / 握手非 101 /
`Sec-WebSocket-Accept` 不匹配 / 对端关闭 / 读超时 / 发送失败 / 返回非 JSON /
服务端报错。只有**参数非法**（未知 `upload_mode`、音频文件不存在、非 unipus
域名、浮点 ID）才抛 `SpeechEvalProtocolError`，因为那是调用方的 bug。

**域名纪律**：所有 URL 在**建连/构造请求前**断言只允许
`speech.unipus.cn` / `speech-eval.unipus.cn` / `zt.unipus.cn` /
`open-eval.unipus.cn` / `ucontent.unipus.cn`，且 WSS 只接受 `wss://`
（明文 `ws://` 直接拒绝）。这条断言在代码里，不只在测试里。

---

## 6. 验证方式与结果

### 6.1 环境

- 本机 Python 3.14.4（模块只要求 >= 3.11；**纯标准库**，无第三方依赖）
- 全程离线：假 socket、假连接、假 sender、固定 timestamp
- **未向任何真实站点发出请求**（本报告的每个"实测"都是本地单测结果）

### 6.2 覆盖的断言（100 条）

| 组 | 条数 | 关键断言 |
| --- | --- | --- |
| `TestSignature` | 8 | 固定 timestamp → 固定 sig（独立复算 + 向量钉桩）、大小写开关、时间戳非法值全部拒绝 |
| `TestAuthFrame` | 5 | 键顺序、`alg=="sha1"`、默认不带 secret、`extra` 并入/重复字段拒绝、紧凑 JSON |
| `TestPlan` | 24 | 默认只 1 帧、`auth_first` 换序、二进制/base64/分片、文件读取、未知模式/非 unipus 域名拒绝、`describe` 屏蔽 secret、参数帧位置与结束帧 |
| `TestDryRun` | 5 | **默认干跑不建连接**（`connector` 断言未被调用 + mock `socket.create_connection`）、`dry_run=True` 压过 `confirm`、两道闸门 |
| `TestRunWithFakeConnection` | 12 | 成功打分、服务端报错、无法判定、超时/关闭/发送失败软着陆、主备切换、`probe_only` |
| `TestWssConnection` | 18 | RFC 6455 握手头、101 断言、`Sec-WebSocket-Accept` 校验、**客户端帧掩码**、文本/二进制/分片/ping-pong/close、EOF、明文 scheme 拒绝 |
| `TestHeuristics` | 3 | 错误/结果启发式、嵌套分数字段收集 |
| `TestVoiceEvaluationPlan` | 10 | 三字段、ID 不过 float、`score_details` 原样透传、告警暴露未验证项、`describe` 屏蔽 token |
| `TestVoiceEvaluationReport` | 9 | 干跑不发、无 sender 被闸门挡、`code=0`/`"0"` 接受、拒绝/非对象/异常软着陆、async 契约 |
| `TestSourceFactsAndStyle` | 6 | 主机全为 unipus、纯标准库（无 requests/httpx/websockets/urllib.request）、无 `print`/`time.sleep`、证据等级标注存在、日志输出 |

### 6.3 命令与结果

```bash
cd /home/hb/刷题刷课
PYTHONPATH=src python3 -m unittest tests.test_unipus_speech -v   # Ran 100 tests ... OK
PYTHONPATH=src python3 -m unittest discover -s tests -t .         # 全量；见下
```

本模块 100 条单测：**OK**。全量单测（09:33 重跑）：**`Ran 1471 tests ... OK`**。

需要如实说明的插曲：09:24 前后全量曾**红过一次**，失败全部落在
`tests/test_unipus_duration.py`（另一队友正在写的文件，当时有 `SyntaxError`
与一条断言不稳），与本模块文件无交集；对方文件稳定后重跑即全绿。并行开发期
文件随时可能再变，**合并前请以 Lead 的全量跑为准**。本模块不含任何全局副作用
（不注册 logger handler、不改环境变量、不打补丁），不会影响别人的用例。

同时 `ruff check`（仓库 lint 门槛，`make lint` 的前半段）对本模块两个文件
**全部通过**（0 warning）。

### 6.4 文件钉桩（本报告结论只对下列版本有效）

```
9b81536bd158d6c2359983a24ce6cdcf  src/kedaibiao/platforms/unipus/speech.py   （1573 行）
84af0b3f2a3f6a6edda778d1b20ce302  tests/test_unipus_speech.py                （1072 行）
```

（采集时间 2026-10-20 09:35；只对本报告有效 —— 再改代码就要重跑单测并更新此表。）

---

## 7. 未验证项清单（如实登记）

| # | 未验证项 | 影响 | 怎么收敛（不改代码也能做） |
| --- | --- | --- | --- |
| S1 | 认证帧完整键名 / 是否带 secret / 摘要大小写 | 帧可能直接被拒 | 需要**一次真实 WebSocket 抓包**（用户提供，不由本模块发请求） |
| S2 | `timestamp` 是否真按秒（毫秒也接受？） | 签名过期/被拒 | 同上 |
| S3 | 音频上传方式（二进制 / base64 / 分片大小） | 无法出分 | 同上；或用户提供参考实现的抓包记录 |
| S4 | 参数帧（`tokenId`/`audio`/`request.*`）逐字形状 | 无法出分 | 同上；`uai-core-submit-facts.md` 只到「有哪几段」 |
| S5 | 结束帧 `{"stop":true}` 是否必需、还差什么字段 | 可能不触发评分 | 同上 |
| S6 | PCM 采样率/位宽/声道/编码 | 评错或拒收 | 规格未提；需抓包或前端 JS 常量 |
| S7 | 结果与错误消息结构 | 只能启发式分类 | 抓到一两条真实响应即可定结构，届时把启发式换成解析器 |
| S8 | 服务端是否校验来源/限流 | 可能被拒或被风控 | 规格 §9 的限流纪律同样适用 |
| S9 | `csAuth` / `open_proxy` 两条 HTTP 通道的方法、入参、响应 | 目前**完全没实现**（只登记地址） | 需要抓包；本模块刻意不猜 |
| S10 | 评分上报 `score_details` 结构与 `block_id`/`course_id` 取值域 | 上报可能被拒 | 需要一次真实上报的**请求体**（用户提供） |
| S11 | 语音评测分数 → `score_details` 的换算（0~1 → 0~100？） | 分数口径错 | 产品决定 + 真实响应样本；本模块不换算 |
| S12 | 「`secret`/`applicationId` 来自前端公开 JS 包」的说法（U11） | 常量可能随前端改版失效 | 需要独立核对前端产物（本调研未做） |

**为什么不去实测**：本任务与项目纪律都要求**绝不向 `*.unipus.cn` 发真实请求**
（`docs/security.md`、任务书），语音评测还属于风控敏感写操作。所以上面的收敛
路径统一是「**用户/Lead 提供抓包或前端产物，离线分析**」，而不是本模块自己去试。

---

## 8. 合规与来源边界

- 参考实现 `Yunlk/uai-core` 的许可是**自定义非商业协议**（无 SPDX，禁商用 +
  衍生作品须同等条款），与本项目 AGPL-3.0 **不兼容**：只能读功能事实，
  **不可复制代码、注释、文档文本、命名**（`docs/license-policy.md` §3.1）。
- 本模块的端点、常量、算法参数来自 clean-room 规格中的功能事实；
  实现结构（纯函数 + plan + outcome + 闸门）是本项目自己的设计，
  与参考实现的 Rust 函数/类型没有对应关系。
- 单测里的 `FakeSocket` / `FakeConnection` / `FakeSender` 全部是本项目新写的
  测试替身，不含任何第三方代码。
- 本模块**没有**新增依赖，`pyproject.toml` 零改动（纯标准库）。

---

## 9. 给 Lead 的接线提示

```python
from kedaibiao.platforms.unipus import speech

# ① 干跑（默认）：只返回计划，不建连接
outcome = speech.run_speech_eval(
    audio=wav_bytes, reference_text="…",
    upload_mode=speech.UPLOAD_BINARY,      # 必须显式开；默认 none
    timestamp=None,                        # 生产用默认时钟；单测传固定值
)
assert outcome.status == speech.STATUS_DRY_RUN
print(outcome.summary())                   # 帧数/认证帧（secret 已屏蔽）/告警

# ② 真连：必须 confirm=True；未验证项仍默认关闭
outcome = speech.run_speech_eval(
    audio=wav_bytes, upload_mode=speech.UPLOAD_BINARY, confirm=True,
    param_frame={"tokenId": "...", "audio": {...}, "request": {...}},  # 可选、未验证
    send_stop=True,                                                    # 可选、未验证
)
if outcome.status == speech.STATUS_SCORED:
    score_details = [outcome.score_fields]   # 口径未验证，建议先人工看

# ③ 评分上报：注入 sender（例如 HttpClient.post_json 的包装）
async def sender(*, url, headers, payload):
    return await http.post_json(url, payload, headers=headers)

report = await speech.report_voice_evaluation(
    block_id=block_id, course_id=course_instance_id,
    score_details=score_details,
    annotator_token=client.auth_headers(UnipusApp.UCONTENT)["x-annotator-auth-token"],
    sender=sender, confirm=True,
)
```

⚠️ 接线时请保持两条纪律：**默认不传 `confirm=True`**（流水线里必须显式打开）；
**不要把 `plan.to_dict()` / `plan.headers` 写日志**（含认证帧原文与 annotator token）。
