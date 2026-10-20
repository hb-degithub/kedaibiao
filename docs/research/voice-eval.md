# U校园口语评测：参考项目 `Yunlk/uai-core` 的做法、我们的差距与最小接入方案

- 调研对象：<https://github.com/Yunlk/uai-core>，`main`，commit `4628e444e8e618767544ddffb27d9e6e20ed97f5`（v0.1.2，2026-09-27）
- 调研日期：2026-10-20（Asia/Shanghai）
- 调研方式：**只读**。读本仓库既有调研/规格 + `read_url` 直接读参考项目在固定 commit 下的源码 + 少量 `web_search` 交叉检索。未克隆、未构建、未执行参考项目任何脚本，**未向 `*.unipus.cn` 发出任何请求**。
- 关联文件：[docs/research/uai-core.md](uai-core.md)（整体调研）、[docs/research/unipus-protocol.md](unipus-protocol.md) §3.5/§4.3/§4.4、[docs/research/uai-core-submit-facts.md](uai-core-submit-facts.md) §2-§4/§8、[docs/verification/unipus-speech.md](../verification/unipus-speech.md)、[docs/audit-2026-10-20.md](../audit-2026-10-20.md):35-39。

## 0. 许可边界（先读这段）

`Yunlk/uai-core` 是**自定义《UAI-Core 非商业使用许可协议 1.0》**，无 SPDX 标识符，
禁商用 + 衍生作品须同等条款，与本项目 `AGPL-3.0-only` **不兼容**
（[docs/license-policy.md](../license-policy.md) §3.1、§3.2）。

因此本文：

- ✅ 只转述**功能事实**（端点、常量、帧形状、参数、量纲、调用顺序）与**设计思路**；
- ❌ **不复制**其代码、注释、文档文本、函数/类型命名。下文所有"参考项目做法"都是
  我方语言的重写，必要时标注"事实"与"其自述"；
- ⚠️ 参考项目源码里的注释/文档属于它的表达，本文引用的源码行**只用于提供证据坐标**，
  不作为实现依据；真正落地时必须按我们的 `speech.py` 既有风格独立实现。

另：参考项目 README 自述"接口随时可能变更，不保证长期可用"；其源码里的
`secret` / `llmSK` 都是**前端 bundle 里的明文常量**，可能随时轮换。

---

## 1. 结论速览

| 问题 | 结论 |
| --- | --- |
| 参考项目怎么处理口语题？ | **三条路并存**：①默认给"满分 record"（分数客户端自报、`audioUrl` 空）；②`uai eval` 把真实 WAV 送 WSS 评测，**只评分、不提交**；③真实录音上传链（token→上传→登记→回填）**标为未接入** |
| 它把评测结果自动写回提交了吗？ | **没有**。`eval` 只打印分数与音频地址，明确写"仅评分，未提交"；上传链 `UPLOAD_WIRED = false` |
| 我们的 `speech.py` 差在哪？ | 差 **6 步**：音频规范化（WAV→PCM）、认证帧真实嵌套、参数帧具体形状、200ms 分片、结果解析（尤其 `audioUrl`）、结果回填与触发入口；另有量纲与超时参数差异 |
| 参考项目把我们的哪些"未验证"变成了"有据可查"？ | `speech.py` 验证报告 §7 的 **S1–S7**（认证帧键名、帧序列、上传方式、分片、PCM 参数、结果结构、结束帧）大部分已被其源码事实回答；**但仍是"参考项目自称实测"，我们未独立复现** |
| 能不能接？ | **能。** 评测链不需要登录态（本地签名即可），纯标准库可完成，干跑成本极低 |
| 值不值得先接干跑？ | **值得，且应作为第一步。** 但"真跑 + 回填"在解决两个硬问题前不要接：① 平台留存策略下"先交空录音再补"无效；② 量纲参考项目内部自相矛盾 |

---

## 2. 参考项目做法（分步骤）

> 证据坐标均锚定 commit `4628e44`。以下为**思路转述**，不是代码引用。

### 2.1 定位：评测 ≠ 提交

参考项目把口语题拆成两件互不依赖的事：

1. **拿分**：`uai eval` 把一段 WAV 送评测服务，拿回服务端产生的分数与音频地址；
   **不写任何服务端状态**（不改进度、不改成绩）。
2. **写成绩**：`uai submit` / `auto` 组提交体时，`record` 包由**客户端**填分数
   （默认直接满分），`audioUrl` 留空。

也就是说：**默认路径根本不评测**，评测只是给"想要真实分数/真实 audioUrl"的人
提供的一条手动旁路。来源：`src/api/speech_eval.rs` 模块文档（"只评分、不写任何
服务端状态"）、`src/question/record.rs` 模块文档（"默认发满分"）、
`src/bin/uai/commands.rs#L701-L850`（`eval` 命令，末尾打印"仅评分，未提交"）、
[README](https://github.com/Yunlk/uai-core/blob/4628e444e8e618767544ddffb27d9e6e20ed97f5/README.md)
「语音评测」段（"只评分，不提交任何作答，所以用了也不涨进度"）。

### 2.2 音频从哪来：**用户自备 WAV，不是浏览器录音**

- CLI 入参就是一个 WAV 文件路径；README 建议用 Windows SAPI 直接生成
  **16 kHz / 单声道 / 16-bit** 的 WAV，从而绕开重采样。
- 参考项目**没有**浏览器麦克风采集、没有录音权限、没有 TTS 自动合成；
  音频来源完全在工具之外。
- 音频规范化是**硬校验**而非转换：只接受未压缩 PCM（`audioFormat=1`）、
  16-bit、16000 Hz、单声道，任何一项不符就**明确报错**并说明原因，
  不悄悄送一段服务端听不懂的数据（`src/api/speech_eval.rs` 的 WAV 解析段）。
- 参考文本也从内容接口提取：优先 `rule.text`（朗读标准原文 / 开放题题目本身），
  取不到再退回 `<p>` 段落；提取要在**原始未反转义串**上定位再反转义，
  否则引号会把文本腰斩（`commands.rs#L852-L880` 一带）。

> ⚠️ 参考项目自己踩过并写进注释的坑，值得直接当验收条件：
> **多子题必须逐子题分别评测**。把 5 个朗读子题拼成一段送评，服务端按 5 句算
> 完整度、音频只读了一句，实测总分从 **92 掉到 29**（`commands.rs#L757-L785`、
> `#L858-L860`）。所以它的 CLI 强制用 `--index N` 选一个子题。

### 2.3 签名与认证

- `sig = SHA1(applicationId + secret + timestamp)`，`timestamp` 是**秒级字符串**
  （前端 `Math.floor(Date.now()/1e3)` 同款）。常量：
  `applicationId = 138000000000001`，secret 为前端 bundle 明文常量，
  认证帧带 `alg: "sha1"`。
- 认证帧是**嵌套**结构（这是关键差异）：
  `{sdk: {version, source: 4, protocol: "websocket"}, app: {applicationId, sig, timestamp, userId, alg: "sha1"}}`。
- 另有一条 `https://zt.unipus.cn/soe/api/csAuth` 取签名通道（返回 `{sig, timestamp}`），
  但主路径是**本地算签名**，不需要登录、不需要 cookie。

来源：`src/api/speech_eval.rs`（常量表、签名函数、认证帧构造函数）、
[unipus-protocol.md](unipus-protocol.md) §3.5（同一事实的既有记载）。

### 2.4 帧序列（四段，顺序固定）

```text
① 认证帧   {sdk:{…}, app:{applicationId, sig, timestamp, userId, alg}}
② 参数帧   {tokenId, audio:{audioType:"wav", channel, sampleRate, sampleBytes:2},
            request:{apiName, transcript, userId, sig, parameters:{details:{adjust:0}}}}
③ 音频     16-bit LE PCM 二进制帧，按 **200ms 一片**
④ 结束帧   {"stop": true}
```

- `tokenId` 是客户端生成的 uuid v4 形态串（无需密码学强度）。
- 分片**不是可选项**：其注释称"一次性把 3MB 全推进去，服务端不会回结果"；
  200ms @16kHz/16bit/mono = 3200 采样 = **6400 字节/片**。
- 结束帧必发，之后才开始收结果。

来源：`src/api/speech_eval.rs` 模块文档的帧序列表、参数帧构造函数、PCM 分片函数、
`speech_eval.rs` 的 `evaluate_async`（发帧顺序与收结果循环）。

### 2.5 两种 `apiName`，两条评测口径

| `apiName` | 适用 | 额外字段 | 说明 |
| --- | --- | --- | --- |
| `en.sent.score` | 朗读题（`sentencerecord` 等） | 无 | 评**发音**，返回逐词 `detail` |
| `open.steam` | 开放题（`presentation` / `oral-personal-state`） | `llmSK` + `extraParam.openQuesType: 22`，且 `userId` 加 `gd-uai-edu-` 前缀 | 走 LLM 评**内容**，答案稿必须真切题 |

`llmSK` 是**前端 bundle 里别人名下的明文 key**，参考项目刻意不内置、要求显式传入，
并在 CLI 里提示"会用那把 key 的额度"。来源：`speech_eval.rs` 的枚举与参数帧
条件逻辑、`commands.rs#L799-L816`。

### 2.6 结果结构与量纲

成功响应（其注释称实测）：

```json
{"code": 0,
 "finalResult": {"uuid": "…", "url": "https://clio-audios…/x.mp3",
                 "result": {"total": 100, "accuracy": 91, "completeness": 100,
                            "audio_time": 103.75, "asr_detail": "…", "detail": "…"}}}
```

- `url` 是**服务端存下来的音频地址**，正是 `recordDetail.audioUrl` 需要的值；
- 解析规则：`code != 0` → 报错（不解析成 0 分）；`finalResult` 与 `result` 两层都可
  缺省回退；**缺 `result.total` 视为结构变化，直接报错**，不当 0 分；
- 收结果循环：跳过没有 `total` 的中间帧，直到拿到带 `total` 的帧或超时；
  超时默认 **120 秒**（开放题走 LLM 可能慢）。

来源：`speech_eval.rs` 的响应解析函数与 `evaluate_async` 收包循环。

> ⚠️ **参考项目内部对量纲自相矛盾，必须实测**：
> - `src/question/record.rs` 说 `specific_scores` 四项是 **0~100**（"实测服务端快照里
>   真实录音存的是 total = 100"）；
> - `src/api/speech_eval.rs` 的折算函数说前端做的是 `score/100`，所以
>   `specific_scores.total` 应是 **0~1**。
>
> 两处不能同时为真。落地前必须抓一条真实提交快照确认，否则会出现
> "看起来满分"或"全零"两种极端。

### 2.7 回填与上报：参考项目**没做完**

参考项目把"真实录音 + 真实 audioUrl"的完整链画成了 5 步，但自己标注**未接入**：

```text
1. GET  /media/user_resource/cms/token       取上传 token（响应结构未实测）
2. 上传音频到对象存储                        得到真实 URL
3. POST /media/user_resource/fetch           登记资源
4. 把 URL 填进 record.url / recordDetail.audioUrl
5. POST /course/api/v3/voiceEvaluation       上报分数
```

- `UPLOAD_WIRED = false`，且有单测钉住"上传链确实没接"；
- 上报体只有三字段 `block_id` / `course_id` / `score_details`，走
  `x-annotator-auth-token`；它明确写"**服务端不做评测，只存客户端给的分**"，
  所以上报函数"不做任何分数编造，调用方传什么就发什么"；
- `score_details` 的结构在参考项目里也没有被写成"实测确认"，只给了数组形态。

来源：`src/api/media.rs` 模块文档与三个函数、`src/endpoints.rs`（`VOICE_EVALUATION`
与媒体端点）、`commands.rs#L1724` 附近（`uai media` 只读自检）、
[README](https://github.com/Yunlk/uai-core/blob/4628e444e8e618767544ddffb27d9e6e20ed97f5/README.md)
「`uai media`」段（"媒体上传链状态与口语上报自检，只读"）。

### 2.8 它明确没做的事（边界）

- 不自动提交评测结果；`eval` 与 `submit` 之间没有数据传递；
- 不采集录音、不做 TTS、不做浏览器录音；
- 不接上传链（因此 `audioUrl` 在真实提交里**从未**非空过）；
- 不做多子题批量评测（要求人工 `--index`）；
- 不做考试类任务。

---

## 3. 我们的现状（文件:行）

### 3.1 `platforms/unipus/speech.py`（1573 行，纯标准库）

| 能力 | 位置 | 现状 |
| --- | --- | --- |
| 端点/常量 | `speech.py:87-118` | 主/备 WSS、`csAuth`、`open_proxy`、`applicationId`、`secret`、`alg` 都在；`voiceEvaluation` 端点也在 |
| 签名 | `speech.py:208-226` | `sign_speech()` 与参考公式一致（秒级时间戳、SHA1、小写默认、大小写可切） |
| 认证帧 | `speech.py:229-279` | 已实现，但**是顶层平铺** `{applicationId,timestamp,sig,alg}`（`applicationId,timestamp,sig,alg` 固定顺序），**没有 `sdk`/`app` 嵌套、没有 `userId`** |
| 音频帧 | `speech.py:320-394` | `read_audio_bytes()` 原样读 bytes/文件；`build_audio_frames()` 支持 `binary` / `base64-json`，`chunk_size` 可切；**默认 `chunk_size=0` = 整段一帧**；**不看 WAV 头、不校验采样率/位宽/声道** |
| 计划构造 | `speech.py:468-622` | `plan_speech_eval()`：认证帧必发；`param_frame` 调用方整对象注入、**默认不发**；`send_stop` **默认关闭**；`upload_mode` **默认 `"none"`** |
| WSS 客户端 | `speech.py:757-1027` | 自写 RFC 6455（握手/掩码/分片/ping-pong/close），只连 `.unipus.cn` |
| 评测执行 | `speech.py:1094-1274` | `run_speech_eval(confirm=False)` 默认干跑；`_exchange()` 顺序发帧、最多读 `MAX_RESULT_MESSAGES=8` 条、遇到"像结果/像错误"就停；超时 `DEFAULT_TIMEOUT_SECONDS=15.0`（`speech.py:142`） |
| 结果解析 | `speech.py:632-744` | **启发式**：`looks_like_error` / `looks_like_result` / `collect_score_fields`（深度 2 递归找 `score/total/accuracy/…`）；**不提取 `finalResult.url`**（真实 `audioUrl` 拿不到） |
| 量纲 | — | **不做任何折算**，`score_fields` 原样给调用方（`unipus-speech.md` §3.3 明确这是产品决定） |
| 评分上报 | `speech.py:1361-1504` | `build_voice_evaluation_plan()` 纯函数 + `report_voice_evaluation()` async（默认干跑，`sender` 注入才发）；三字段与参考一致 |
| 未实现通道 | `speech.py:1512-1515` | `csAuth` / `open_proxy` **只登记地址，不实现** |
| 生产调用方 | — | **0 个**。全仓库只有 `tests/test_unipus_speech.py` 在调（`grep -rn speech src/kedaibiao` 仅命中 `unipus/__init__.py` 的 docstring） |

### 3.2 `platforms/unipus/submit.py`（832 行）

| 位置 | 现状 |
| --- | --- |
| `submit.py:77` | `SCORED_TYPES = {oral, oral-state, role-play}` —— 录音类题型的唯一判定 |
| `submit.py:176-199` | `_empty_record()` 返回 `{"url": ""}`；`full_record()` 返回满分 + **空 `audioUrl`**（`submit.py:195`） |
| `submit.py:238` | `record = full_record() if self_report_scores else _empty_record()` —— 二选一，**没有"真实分数/真实 URL"通道** |
| `submit.py:291-292` | `build_judge_object()` 同款二选一；`specific_scores` 只在 `self_report_scores=True` 时出现 |
| `submit.py:517-530` | `build_submit_plan()` 入参只有 `self_report_scores: bool`，**没有按题注入 record 的入口** |
| `submit.py:702-706` | 组内含口语题且未自报分数时，`warnings` 写明"这些题只能拿完成度、拿不到分数" |

### 3.3 适配器与产品层

- `adapter.py:117,125`：`UnipusAdapter(self_report_scores=False, …)`，默认关；
- `adapter.py:891`：`_build_plan()` 把它透传给 `build_submit_plan`；
- 控制台/CLI **没有**语音评测入口（`grep` 无 `speech` 命中），也没有"上传录音"动作；
- [docs/audit-2026-10-20.md](../audit-2026-10-20.md):35-39 已把本条登记为高风险：
  "口语/角色扮演题平台记 0 分"，建议"先干跑接上"。

**一句话**：链路后半段（签名、WSS、上报计划、干跑闸门）我们**已经写好了**，
缺的是前半段（音频规范化 + 真实帧形状）和收尾（结果解析成 `record` + 触发入口）。

---

## 4. 差距表

| # | 步骤 | 参考项目 | 我们 | 缺口 |
| --- | --- | --- | --- | --- |
| 1 | 录音采集 | 无（用户自备 WAV；README 建议 SAPI 生成） | 无（`read_audio_bytes` 只读文件/bytes） | **双方都缺**。需要产品决策：本地 WAV / 浏览器录音 / 不接 |
| 2 | 音频规范化 | 硬校验 WAV：PCM/16-bit/16kHz/mono，不符即报错；抽 `data` 块 | 无 WAV 解析、无校验 | 缺 `WAV → PCM` 与格式断言；重采样会引依赖，与"零依赖"冲突 → 只接受已合规 WAV |
| 3 | 签名 | `SHA1(appId+secret+timestamp)`，秒级 | 同（`sign_speech`） | ✅ 已对齐 |
| 4 | 认证帧 | 嵌套 `{sdk, app{…, userId}}`，含 `sdk.version/source/protocol` | 顶层平铺，无 `userId`/`sdk` | **形状不对**，真实发送大概率被拒；需按事实重做（并保留旧形状开关做 A/B） |
| 5 | 参数帧 | 具体形状：`tokenId` + `audio{audioType,channel,sampleRate,sampleBytes:2}` + `request{apiName,transcript,userId,sig,parameters.details.adjust}`；`open.steam` 另加 `llmSK`/`extraParam.openQuesType=22`/`gd-uai-edu-` 前缀 | `param_frame` 由调用方整对象给，**默认不发** | 缺一个"按事实构造参数帧"的函数与 `apiName` 选择；缺 `tokenId` 生成 |
| 6 | 音频分片 | **200ms/片（6400 字节）**，必须分片 | 支持 `chunk_size`，但**默认 0 = 整段一帧** | 缺 200ms 默认与"大 payload 单帧无结果"的提示 |
| 7 | 结束帧 | 必发 `{"stop": true}` | `send_stop` **默认 False** | 默认值反了；真跑必须发 |
| 8 | 收结果/超时 | 跳过无 `total` 的中间帧；**120s** 超时 | 最多 8 条、遇"像结果"即停；**15s** 超时 | 超时偏短（开放题必超时）；"像结果"判定可能过早停 |
| 9 | 结果解析 | 固定结构：`finalResult{url, uuid, result{total, accuracy, completeness, audio_time, asr_detail, detail}}`；缺 `total` 报错 | 启发式递归找分数字段；**丢 `url`** | 缺结构化解析与 `audioUrl` 提取（这正是回填的关键） |
| 10 | 量纲 | 参考内部矛盾（0~100 vs 0~1），见 §2.6 | 不折算，原样给 | **需实测确认**；`submit.py` 的 `FULL_MARKS` 是 0~100 |
| 11 | 结果回填 `record` | 只提供 `full_marks_record(url)` 的形状能力；**无自动回填** | `record` 恒 `{"url":""}` 或满分包 | 缺"按题注入 `record`"的参数通道（`build_submit_plan` 只有 bool） |
| 12 | 评分上报 | 有函数、`UPLOAD_WIRED=false`、CLI `media` 只读自检 | 有 `build_voice_evaluation_plan` + `report_voice_evaluation`，**无人调用** | 缺触发点与 `block_id`/`course_id` 来源；`score_details` 结构未确认 |
| 13 | 音频上传链 | **未接入**（token/上传/登记三步只有占位） | **无** | 缺整条；参考项目也没跑通 |
| 14 | 触发入口 | CLI `uai eval take.wav --text/--from/--kind/--llm-sk/--index` | 无（控制台/CLI/适配器都没有） | 缺入口与参数校验；也缺"多子题逐句评"的强制 |

---

## 5. 最小接入方案（分阶段）

### 阶段 0：把"未验证"改成"有据可查"（0 行代码，只改文档）

- 在 [docs/verification/unipus-speech.md](../verification/unipus-speech.md) 的 S1–S7 上
  追加"参考项目 commit `4628e44` 的源码事实"作为**二手证据**（仍标注"未独立复现"）：
  认证帧嵌套、参数帧形状、200ms 分片、`{"stop":true}`、结果结构与 `url`。
- 明确两个**仍未知**：`specific_scores` 量纲（参考自相矛盾）、`score_details` 结构。
- 这一步不改代码、不联网，零风险，且能直接决定阶段 1 的字段。

### 阶段 1：干跑（只构造、不连接、不上报）—— **建议先做这个**

目标：能在控制台/CLI 里看到"这一次评测会发什么"，且**绝不建连**。

1. `speech.py` 新增两个纯函数（不改现有默认）：
   - `wav_to_pcm_16k_mono(data) -> (pcm, sample_rate, channels)`：解析 RIFF/WAVE，
     遍历 chunk 找 `fmt `/`data`，硬校验 PCM/16-bit/16000/mono，不符抛
     `SpeechEvalProtocolError`；**不做重采样**。
   - `build_speech_param_frame(*, api_name, transcript, user_id, token_id, sig, …)`：
     按 §2.4 的事实形状构造参数帧；`open.steam` 分支要求显式 `llm_sk`，
     缺则本地报错（不猜、不内置）。
2. `plan_speech_eval()` 增加 `api_name="en.sent.score"` 参数，分片默认值改成
   "200ms 对应字节数"，`send_stop` 在该模式下默认 `True`（新开关，不动旧默认）。
3. 适配器新增只读方法 `speech_eval_plan(assignment_id, *, audio, text, api_name)`：
   读内容 → 取 `rule.text`（逐子题）→ 返回 plan 摘要（`describe()`，secret 已屏蔽）。
4. 单测：WAV 解析（合法/非 PCM/44.1k/8-bit/立体声/WAV 头前有 LIST chunk）、
   参数帧形状、200ms 字节数、`open.steam` 缺 key 报错、干跑不建连。

**输入**：一段 16k/mono/16-bit WAV（测试夹具可用正弦波合成，不必真人）；
参考文本直接从内容接口取。**不需要登录态也能干跑。**

### 阶段 2：真评（连接、只评分、不上报）

前置：阶段 1 通过 + 人工确认一次。

1. 复用 `run_speech_eval(confirm=True)`，但显式传 `api_name`、`send_stop=True`、
   `upload_mode=UPLOAD_BINARY`、200ms 分片、`timeout=120`；
2. `_exchange()` 之后加**结构化解析**（`finalResult.result.total` 必须存在，
   否则报"结构变化"；提取 `url`/`uuid`/`accuracy`/`completeness`/`audio_time`）；
3. 结果落盘成 JSON（`total`、`audio_url`、`uuid`、`timestamp`、`transcript` 摘要）
   到 `data/` 下按实例/分组归档，**不写服务端**；
4. 控制台只读展示 + "这个分数来自服务端，未提交"的醒目说明。

**输入**：真实或合成的 WAV（**合成音频用于刷分属于产品/合规决策**，见风险 R5）、
逐子题参考文本、`open.steam` 还需要 `llmSK`。**仍然不需要登录。**
**每个子题单独评**（拼接会让完整度对不上，参考实测 92→29）。

### 阶段 3：回填提交（把真实 record 写进 `submit` 计划）

前置：阶段 2 拿到真实 `total` 与 `audioUrl`；**确认量纲**；确认该分组**尚未提交过**。

1. `submit.py` 的 `build_submit_plan` / `build_ques_data` / `build_judge_object`
   新增可选参数 `oral_records: Mapping[str, dict]`（键：`instanceId` 或题下标），
   命中时用它替代 `full_record()`/`_empty_record()`；
2. `record` 形状：`url` 与 `recordDetail.audioUrl` 同值（参考项目的前端补法）、
   `recordDetail.score` 用 0~100 的 `total`、`specific_scores` 按**实测确认**的量纲填、
   `details` 保持 `[]`；
3. 未命中评测结果的录音题**仍然**只发 `{"url": ""}`，并在 `warnings` 里保留
   `submit.py:702-706` 的"拿不到分"提示；
4. 提交仍走既有闸门（`submit_live` / 干跑预览），**不在本阶段引入自动链**。

### 阶段 4：评分上报 `voiceEvaluation`（可选，最后做）

只有当阶段 3 证明"提交 `record` 里带的分数/URL 不被服务端采纳"时才需要。

1. 接 `report_voice_evaluation(...)`：`sender` 注入 `HttpClient.post_json` 包装，
   `annotator_token` 从 ucontent 鉴权头取；
2. `block_id` = 题目的 `instanceId`、`course_id` = 课程实例 ID（**取值域仍待实测**）；
3. `score_details` 结构未知 → 先只发最小结构并保留原始响应，失败不重试（幂等性未知）。

### 音频上传链（阶段 5，**不建议现在做**）

参考项目自己都没跑通（token/上传/登记三步无实测），我们也没有对象存储依赖。
没有它，`audioUrl` 永远为空——但**分数不依赖它**。若产品只在乎分数，跳过即可。

---

## 6. 风险与未知

| # | 风险 | 影响 | 应对 |
| --- | --- | --- | --- |
| R1 | **留存策略**：`record_every_submit == false` 时一组只留**首次**记录，后续提交不改写（[unipus-protocol.md](unipus-protocol.md) §8.3，参考项目声明、我方未独立复现） | 一旦先用空录音交过，之后再接真实评测**也补不回来** | 阶段 3 必须只对"未提交过"的分组启用；UI 明示"该组已交过，改不了" |
| R2 | **量纲矛盾**：参考项目 `record.rs` 说 0~100、`speech_eval.rs` 说 0~1 | 填错 → 记录看起来满分或全零 | 抓一条真实提交快照确认；代码里量纲作为显式参数、不写死 |
| R3 | **认证帧/参数帧仍是"二手事实"** | 形状错 → 直接拒连或永不出分 | 阶段 1 干跑先把帧文本打出来；阶段 2 首次真跑人工盯；失败即回退，不做自动重试 |
| R4 | **secret 是前端明文常量**，可能随前端改版轮换（其自述，U11 未独立核对） | 突然全部失败 | 常量集中一处（`speech.py:96-101`），失败信息直指"签名常量可能过期" |
| R5 | **音频来源的合规性**：TTS/合成音频冒充本人朗读 | 学术诚信与平台 ToS 风险；分数失真 | 默认只接受"用户提供的真实录音"；合成路径必须显式显名、默认关闭 |
| R6 | **多子题合并评测** | 完整度对不上，实测 92→29 | 强制逐子题、`--index` 式选择；拼接口直接拒绝 |
| R7 | **`llmSK` 属于第三方额度**（前端 bundle 里的别人 key） | 可能被轮换/吊销，且消耗他人额度 | 不内置、不默认；显式传参 + 终端提示（参考项目同款做法） |
| R8 | **并发/重放** | 无幂等键；评测服务可能按 `userId` 计量/风控；提交侧有节奏配额（`600002`） | 串行、逐组间隔、禁止自动重试；`tokenId` 每次新生成 |
| R9 | **超时**：开放题走 LLM，我们默认 15s 必然不够 | 假失败 | 评测路径超时提到 120s，且与"没结果"区分开 |
| R10 | **纯标准库约束**：重采样需要第三方库 | 引入依赖违反 `dependencies = []` | 只接受已合规 WAV，不符就报错让用户转格式 |
| R11 | **隐私**：真实录音上传到平台并落到服务端 URL | 数据留存于第三方 | 本地只留评测摘要不留音频；日志不打 bytes |

**仍未知（不得据此行动）**：
`score_details` 结构、`block_id`/`course_id` 取值域、上传链三步的响应结构、
`userId` 是否必须与会话 `open_id` 一致、评测服务是否校验来源/限流、
平台是否真会采纳提交体里的 `recordDetail.score`（我方从未实测）。

---

## 7. 结论

1. **参考项目的答案不是"自动评测"，而是"手动评测旁路 + 默认自报满分"。**
   它把"评测"和"提交"彻底解耦：`eval` 只打分、不写状态；提交体里的分数永远是客户端填的。
   它自己也没把评测结果自动回填过（上传链未接）。
2. **我们的差距不在"能不能评测"，而在"帧对不对 + 结果认不认"。**
   签名已对齐；真正缺的是 WAV 规范化、认证帧嵌套、参数帧形状、200ms 分片、
   必发结束帧、结构化解析（尤其 `audioUrl`）、按题注入 `record` 的通道，
   以及一个触发入口。
3. **可以先接、也应该先接干跑。**
   评测链**不需要登录态**（本地签名），干跑不建连、零网络、零依赖，
   阶段 1 的成本只是一对纯函数 + 单测；它能把"要不要真跑"变成一次可审阅的输出。
4. **真跑的回填有顺序陷阱**：平台"只留首次记录"意味着
   **必须在第一次提交前就带上真实 record**，不能"先空交、后补分"。
   这与"默认干跑、事后补"的直觉相反，是本方案最需要用户拍板的一条。
5. **不建议现在接上传链**：参考项目都没跑通，且分数不依赖它；
   没有它只是 `audioUrl` 为空，不影响"拿到服务端分数"这一核心价值。

---

## 8. 来源清单

**参考项目（锚定 commit `4628e444e8e618767544ddffb27d9e6e20ed97f5`）**

- 仓库：<https://github.com/Yunlk/uai-core>
- 许可：<https://github.com/Yunlk/uai-core/blob/4628e444e8e618767544ddffb27d9e6e20ed97f5/LICENSE>（判定见 [license-policy.md](../license-policy.md) §3.1）
- 评测实现：<https://github.com/Yunlk/uai-core/blob/4628e444e8e618767544ddffb27d9e6e20ed97f5/src/api/speech_eval.rs>
- 录音/上报/上传链：<https://github.com/Yunlk/uai-core/blob/4628e444e8e618767544ddffb27d9e6e20ed97f5/src/api/media.rs>
- `record` 满分包与量纲：<https://github.com/Yunlk/uai-core/blob/4628e444e8e618767544ddffb27d9e6e20ed97f5/src/question/record.rs>
- CLI `eval`：<https://github.com/Yunlk/uai-core/blob/4628e444e8e618767544ddffb27d9e6e20ed97f5/src/bin/uai/commands.rs>（`eval_cmd`，L701-850）
- README「语音评测」段：<https://github.com/Yunlk/uai-core/blob/4628e444e8e618767544ddffb27d9e6e20ed97f5/README.md>
- 端点表：<https://github.com/Yunlk/uai-core/blob/4628e444e8e618767544ddffb27d9e6e20ed97f5/src/endpoints.rs>

**本项目（本地路径）**

- [src/kedaibiao/platforms/unipus/speech.py](../../src/kedaibiao/platforms/unipus/speech.py)：签名 `:208`、认证帧 `:229`、音频帧 `:320`、计划 `:468`、WSS `:757`、执行 `:1094`、上报 `:1361`、常量 `:87`
- [src/kedaibiao/platforms/unipus/submit.py](../../src/kedaibiao/platforms/unipus/submit.py)：`SCORED_TYPES:77`、`record:176-199`、选择 `:238`、`build_submit_plan:517`
- [src/kedaibiao/platforms/unipus/adapter.py](../../src/kedaibiao/platforms/unipus/adapter.py)：`self_report_scores:117,125`、透传 `:891`
- [docs/verification/unipus-speech.md](../verification/unipus-speech.md)：S1–S12 未验证清单
- [docs/research/unipus-protocol.md](unipus-protocol.md) §3.5/§4.3/§4.4/§7.4/§8.3
- [docs/research/uai-core-submit-facts.md](uai-core-submit-facts.md) §2-§4、§8
- [docs/audit-2026-10-20.md](../audit-2026-10-20.md)：35-39（口语空录音已登记为高风险）

**外部检索**（只作线索，未作为证据）

- 论坛贴「记一次修改u校园 role-play websocket 报文」：<https://bbs.binmt.cc/forum.php?mod=viewthread&tid=97050>（第三方内容，未采信）
- 检索命中一份含 `audio` 参数的接口 PDF：<https://jwc.biem.edu.cn/__local/A/AA/30/5ED93F6A67E09815CE9E742FA55_EC700658_2D2FAA.pdf>（来源不明，未采信）
