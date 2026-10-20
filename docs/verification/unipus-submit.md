# U校园（Unipus）提交链路验证报告（2026-10-20）

- 验证者：**unipus-submit 队友**（task-2 实现者）
- 实现文件：`src/kedaibiao/platforms/unipus/submit.py`（重写）、
  `src/kedaibiao/platforms/unipus/adapter.py`（只改提交相关：`capabilities.submit`、
  `submit()`、`native_plan()`/`native_submit()` 及其私有辅助；登录/解析部分未动）
- 单测：`tests/test_unipus_submit.py`（**57 条，全部离线**）
- 依据：`docs/research/uai-core-submit-facts.md`（§1–§5、§7、§9.1）、
  `docs/research/unipus-protocol.md` §7/§8/§10/§11
- 参考实现许可：`Yunlk/uai-core` 是非商业许可。本模块只使用调研文档里已经
  clean-room 复述过的**协议事实**（键名、取值规则），没有复制任何参考实现代码/注释。
- **线上访问：零。** 本报告与本次实现均未向 `*.unipus.cn` 发出任何请求，
  所有网络交互都是单测里的假 `Transport`（断网可跑）。

> 结论一句话：**载荷的字段语义这次有源码事实可依（`isCompleted` 恒 true、
> `thirdPartyJudges` 是对象数组、长度由内容侧 child_count 决定、`submitType` 纯派生），
> 这些都已按事实写死并用离线用例锁住；但「提交请求本身」仍然一次都没真发过 ——
> 服务端是否照收、成功码的真实 JSON 类型、口语自报分数是否被接受，全部未实测。**
> 因此实现默认**干跑**（只读 GET 构造 plan，绝不 POST），真发必须显式打开。

---

## 0. 结论表

| # | 结论 | 证据等级 | 依据 / 详见 |
| --- | --- | --- | --- |
| 1 | `isCompleted` 元素**恒为 JSON `true`**（源码只 push true，无 false 分支） | 【源码事实】 | facts §1（`src/question/mod.rs#L560-L572`） |
| 2 | `isCompleted` 长度 = 各非学习题贡献项数之和：学习题 0、`role-play`/`oral-state` 恒 1、其余 `max(child_count,1)` | 【源码事实】 | facts §1-2（`mod.rs#L115-L131`、`#L258-L261`、`role_play.rs#L51-L54`、`oral_state.rs#L42-L45`） |
| 3 | `thirdPartyJudges` 是**字符串化的 JSON 对象数组**；元素是对象，长度与 `isCompleted` **严格相等** | 【源码事实】 | facts §2（`mod.rs#L581-L594`、`submit.rs#L587-L612`） |
| 4 | 元素形状按题型分派：客观题族/主观题 5 键、`upload` 无 `value`、`oral` 1 个 record、`oral-state` 的 `value` 是映射、`role-play` 无 `value` 且 `payloads` 按子题展开 | 【源码事实】 | facts §2 表（各题型 `judge()` 实现行号见 facts §2「证据」） |
| 5 | `versions` 默认 `{"course":"","group":1,"template":1,"answer":3,"content":"0"}` | 【源码事实】 | facts §2-3（`mod.rs#L464-L494`） |
| 6 | `answer.children` 长度 = **内容侧** `max(child_count,1)`；每项 `{"value":…, "isDone":true}`；主观题 `value` 是**字符串**（`\n` 连接） | 【源码事实】 | facts §3-2、§3-3（`mod.rs#L358-L401`、`subjective.rs#L57-L61`） |
| 7 | `answer.progress` 的键是**媒体项在 `contents` 里的下标**（只对 type∈{1,2} 打点），值是 `true`；主观题额外 `progress.reply=true` | 【源码事实】 | facts §3-3（`mod.rs#L380-L386`、`parse.rs#L172-L211`） |
| 8 | `submitType` 纯派生：整组学习题 → 2，否则 1，与题型种类数无关；纯学习题组**也必须提交** | 【源码事实】 | facts §1-4、§4（`mod.rs#L505-L529`） |
| 9 | 答案 = 服务端标准答案**原样转发**；多选/排序保持顺序，`answer.children[].value` 不逗号连接（逗号只用于 judge） | 【源码事实】 | facts §5.2、§7.3（`parse.rs#L216-L264`、`multi_choice.rs#L3-L17`） |
| 10 | ucontent 提交成功 = **整数 `code == 0`**（字符串 `"0"` 在参考实现里算「未接受」） | 【源码事实】 | facts §7（`submit.rs#L109-L142`） |
| 11 | 「服务端接受」≠「达标」：参考实现提交后回读 `progress/v2`；区间是 4 次往返（内容→答案→提交→回读） | 【源码事实】 | 规格 §8.3（`submit.rs#L489-L496`、`#L546-L553`） |
| 12 | 鉴权常量、内容/答案/进度的真实可读性 | 【线上已实测，2026-10-20 | commit `4b6d968`】 | 本次联调；下文 §2 |
| 13 | 内容解密后 `[{id, content:"<JSON字符串>"}]`；inner 有 `replyType/contents/children/options/quesText` | 【线上已实测】 | 同上；真实样本落在 `data/recon/unipus/`（`data/` 已 gitignore，**不入库**） |
| 14 | 答案解密后 `[{id, content, answer:"<JSON字符串>", analysis, quesId}]`，`answer` 形如 `{"children":[{"answers":["A"]},…]}` | 【线上已实测】 | 同上 |
| 15 | 实测样本里内容的 `children[i]` 与答案的 `children[i]` **按序对齐**（4↔4、5↔5）；`contents[].type` 1=音频 2=视频，progress 的键是下标 | 【线上已实测（样本级）】 | 同上；本节 §3 |
| 15b | 讨论题组（`…/7302fcb43004729`，`replyType=discussion`）答案接口返回 `{"answer": ""}`（**空字符串**），内容侧 1 项、答案侧 0 项 —— 这是「没有标准答案」的合法形态 | 【线上已实测，2026-10-20 真号跑批】 | 见 §2.1「空 answer 的处理」 |
| 16 | **提交 POST 本身**：服务端是否接受这套载荷、`300100`/越界的真实触发条件 | **【未验证】** | 本次**没有发过任何提交请求**；见 §4 |
| 17 | 提交响应 `code` 的真实 JSON 类型（整数还是字符串） | **【未验证】** | facts §7「仍未确认」；客户端已按严格整数判定，parse 层记下实际类型 |
| 18 | 口语/角色扮演「客户端自报满分 record」是否被服务端照收 | **【未验证】** | facts §9.3 表；默认关闭（`self_report_scores=False`） |
| 19 | 主观题占位文本（参考实现 `[已提交]`）是否让完成度非 0 | **【未验证】** | 默认发空串；显式参数 `subjective_placeholder` |
| 20 | `quesDatas: []` 是否被接受 | **【未验证】（所以不发）** | 规格 U7；解析不出题目 → 明确失败 |
| 21 | 全量离线单测通过（提交相关 **57 条**；全量 **1504 条 OK**，无网络、无真实站点） | 【离线实测】 | §7 |

---

## 1. 实现位置与 API

### 1.1 `submit.py`（纯函数，无 IO）

```python
plan = build_submit_plan(
    course_id="course-v2:<实例ID>", group_id="u1g1", open_id="<ssoId>",
    questions=[ContentQuestion…], standard_answers=[StandardAnswer…],
    self_report_scores=False,          # 口语/角色扮演是否自报满分（默认保守）
    subjective_placeholder="",         # 主观题占位文本（默认空串）
)
plan.payload            # 可直接 POST 的整包（键名逐字对齐规格 §8.1）
plan.summary()          # 一行摘要
plan.as_dict()          # 控制台/干跑视图（含逐题 rows、warnings、blockers）
plan.submittable        # False = 有 blocker，真实提交必须拒绝
```

逐项构造器也单独可测：`build_answer_object` / `build_judge_object` /
`build_ques_data` / `judge_item_count` / `parse_submit_result`。

### 1.2 `adapter.py`

```python
adapter = UnipusAdapter(ctx)                     # 默认 submit_live=False（干跑）
await adapter.submit(assignment, answers)        # 通用契约：默认只读，返回 plan 摘要
await adapter.native_plan(assignment_id)         # 控制台干跑：dict，绝不写
await adapter.native_submit(assignment_id)       # 控制台确认真交：POST + 回读

UnipusAdapter(..., submit_live=True)             # CLI/流水线真发开关
UnipusAdapter(..., self_report_scores=True)      # 口语自报满分（合规决策）
UnipusAdapter(..., subjective_placeholder="[已提交]")
```

- `capabilities.submit = True`（能力与实现一致）；`native_submit_flow = True`。
- `submit()` 的 `answers` 参数**不参与载荷**：U校园转发服务端标准答案，通用
  answerer 的产出只出现在干跑摘要的对照字段里（`raw["answers_argument_*"]`）。
- 限流 `UnipusRateLimited` **原样上抛**给调度层；本层不 sleep、不重试。
- 回读失败软着陆：不影响「提交请求已被接受」这一事实，只把原因记进
  `raw["readback_error"]`；回读遇限流同样上抛（不吞）。
- 非 0 的提交响应在**请求层**就变成 `UnipusApiError`（ucontent 家族严格要求整数
  `code==0`，异常不静默），不会走到 `parse_submit_result` 的「未接受」分支。

---

## 2. 线上已实测的部分（2026-10-20 联调，commit `4b6d968`）

这一节只陈述**已经发生过**的事实，均来自只读接口：

| 项 | 结果 |
| --- | --- |
| SSO/门户/ucontent 三套鉴权常量 | 已修正（`endpoints.py`），登录链可用 |
| 用户信息 → `open_id`（`ssoId`） | 可读 |
| 目录（单元 → 分组） | 可读 |
| `progress/v2`（分组状态） | 可读，`first_pass`/`state`/`score` 结构符合规格 |
| 内容接口解密 | 可读：`[{id, content}]`，inner 有 `replyType/contents/children/options/quesText` |
| 答案接口解密 | 可读：`[{id, content, answer, analysis, quesId}]`，`answer` 形如 `{"children":[{"answers":["A"]}]}` |
| 内容/答案 children 对齐 | 实测样本 4↔4、5↔5（按序）；主观题答案侧 children 存在但**无 `answers`** |
| 媒体下标 | `contents[].type` 1=音频 2=视频；样本里 `contents=[{type:4},{type:2}]` → 媒体下标是 **1** 不是 2 |

样本已落盘在 `data/recon/unipus/`（`sample-content-*.json` / `sample-answer-*.json` /
`content-*.json` / `answer-*.json`）。**`data/` 在 `.gitignore` 里，不入库**；
本报告的离线用例只用**同形状**的现场加密数据，不依赖这些文件。

### 2.1 空 answer 的处理（2026-10-20 真实样本修正的规则）

真号跑批（`kedaibiao run unipus --limit 3`，只读）暴露了一个规则冲突：
讨论题组 `…/7302fcb43004729` 的 `replyType` 是 `discussion`，答案接口返回
`{"answer": ""}`（**空字符串**，不是 `null`）。旧实现把「内容侧 1 项、答案侧 0 项」
一律当 blocker，于是这个**本来就该提交**（纯学习题组，提交才能记观看进度）的组
被本地校验拦住。

收敛后的规则（已实现 + 用例锁住）：

| 题型 | 答案侧 child 数与内容侧 `child_count` 不一致时 |
| --- | --- |
| **客观题族**（single / multi / dropdown / fill-blank / banked-cloze / sequence / 兜底 objective） | **blocker**（不可提交）—— 客观题必须原样转发标准答案，数量对不上必然判错 |
| 主观题 / 口语族（oral、oral-state、role-play）/ 学习题（含 `discussion`、`rich-text-read`）/ 上传 | **不 block**：按内容侧 `child_count` 补齐 children（空作答为空串 / 空数组，占位文本由 `subjective_placeholder` 决定），只记一条 warning |

注意 `discussion` 在题型分派里属于**学习题**（`classify_question` → `study`，
贡献项数 0），所以它的组通常是 `submitType=2`、两个数组为空 —— 这正是 facts §1-4
说的「纯学习题组也必须提交」。

> 注意：以上全部是 **GET 只读**。`newExploration/submit`（唯一的写端点）在联调中
> **一次都没有被调用**。

---

## 3. 仍未实测的部分（提交本身）

| 事项 | 为什么不能算已验证 | 本实现的处理 |
| --- | --- | --- |
| 提交请求是否被接受 | 从没真发过 | 默认干跑；`submit_live` / 控制台 confirm 才发 |
| `isCompleted` / `thirdPartyJudges` 的**长度与取值**是否被服务端逐项校验 | 源码只证明客户端怎么写，没有服务端行为 | 按 facts 写死 + 两条本地等长校验 |
| 提交响应 `code` 的真实 JSON 类型 | 没有真实响应 | 优先整数比较；`parse_submit_result` 记录 `code`/`code_type`，一次真实响应后即可固化 |
| `300100` / `index out of range` 的真实触发条件 | 参考实现注释，未复现 | 子题数不一致在本地就拦（规格 §11.2-2） |
| 口语/角色扮演满分 record 是否照存 | 参考实现自称实测 | 默认**不报分**（只交完成度）；`self_report_scores=True` 才发 |
| 主观题占位文本是否让完成度非 0 | 未验证 | 默认空串；`subjective_placeholder` 显式开启 |
| `quesDatas: []` 是否被接受 | 未验证（U7） | **绝不发**：解析不出题目项 → 明确抛错 |
| `progress` 是否要求所有可播放项都打点 | 源码默认全打，未核对 | 按内容侧媒体下标全打（保守，与参考实现一致） |
| 提交后多久 `progress/v2` 才反映成绩 | 未验证 | 回读只做一次；「接受 ≠ 达标」在 message 里明说 |

**一次真实提交就能推进的项**：①`code` 类型；②提交是否被接受；③响应 `data.state`
粒度；④回读是否立即可见。建议由 Lead 先用**一个已完成的、非必修的小组**做一次
人工确认的真发（`native_submit`），再把结果回填本报告。

> ⚠️ 一个连带风险：ucontent 提交的成功码判定在**请求层**（`client.request` 的
> `family="ucontent"`）就是「严格整数 `code==0`」。如果真实响应把 `code` 返回成
> **字符串 `"0"`**，请求层会抛 `UnipusApiError`（这与参考实现的行为一致，但会把
> 「成功」误判成失败）。届时只需要改 `client`/`codes` 的 ucontent 判定；
> `parse_submit_result` 已把 `code` 原值与类型记进 `SubmitResult.code/code_type`，
> 可直接据此固化。本任务未改 `codes.py`/`client.py`（不属写文件范围）。

---

## 4. 两处「按事实实现但默认保守」的开关

这两项都属于**产品/合规决策**，不是技术不确定：

1. **口语 / 角色扮演的满分 record**（facts §2、§9.1-7）
   - 默认 `self_report_scores=False`：不发 `specific_scores`，`payloads` /
     `answer.record` 只用 `{"url": ""}` 空记录（长度仍按题型给足），
     组内 `warnings` 会写「这些题只能拿完成度、拿不到分数」；
   - `self_report_scores=True`：按 facts §3-4 发满分 record
     （`specific_scores` 四项 100 + `recordDetail` 全 100、`audioUrl` 空）。
2. **主观题占位文本**（facts §9.1-4）
   - 默认发空串 `""`（形状合法，完成度可能为 0），`warnings` 明确提示；
   - `subjective_placeholder="[已提交]"` 可开启，但**那句不是学生写的**，
     UI 必须对外明示（参考实现的做法不直接照搬）。

---

## 5. 本地校验（服务端不会告诉你哪里错了）

| 校验 | 行为 | 依据 |
| --- | --- | --- |
| `len(isCompleted) == len(thirdPartyJudges)` | 内部同一计数生成；显式传参时长度不等 → `SubmitPayloadError` | facts §1-5 |
| 解析不出任何题目项 | `SubmitPayloadError`，**绝不发空 `quesDatas`** | 规格 §11.2-3 / U7 |
| **客观题族**：内容侧 `child_count` ≠ 答案侧 `children` 数 | 该组记 `blockers`（不可提交），`native_plan.can_submit=False`，真发前拒绝并给原因 | 规格 §11.2-2 + §2.1 |
| **非客观题**：答案侧为空/数量不同 | 按内容侧 `child_count` 补齐，只记 warning，**不 block** | §2.1（2026-10-20 真实样本） |
| 客观题没有服务端标准答案 | 记 blocker（转发标准答案才有意义） | 规格 §7.3 |
| 答案条数 ≠ 题目条数 | `SubmitPayloadError`（外层按下标对齐，错位比不交更糟） | 规格 §7.3 |
| 缺 `courseId` / `groupId` / `openId` | `SubmitPayloadError` | 规格 §8.1 |
| 纯学习题组 | 允许（`isCompleted=[]`、`thirdPartyJudges="[]"`、`submitType=2`），**仍要提交** | facts §1-4 |
| 解析出题目但无判分项且不全是学习题 | blocker（防御） | — |

---

## 6. 干跑输出示例（离线，真实样本形状）

一次 `native_plan("80001/u1g1")`（内容 1 道单选 4 子题 + 1 道主观 5 子题）：

```json
{
  "question_count": 2,
  "item_count": 9,
  "submit_type": 1,
  "can_submit": true,
  "answered": 1,
  "dry_run": true,
  "platform": "unipus",
  "assignment_id": "80001/u1g1",
  "note": "干跑结果，没有向平台提交任何内容。 本地校验通过，确认后会真发。",
  "questions": [
    {"index": 0, "instanceId": "1474574379963842561", "kind": "single-choice",
     "child_count": 4, "item_count": 4, "media_indexes": [1],
     "answer": "A,B,A,B", "has_standard_answer": true},
    {"index": 1, "instanceId": "1474574379963842560", "kind": "subjective",
     "child_count": 5, "item_count": 5, "media_indexes": [1],
     "answer": "", "has_standard_answer": false}
  ],
  "warnings": ["组内有主观题且没有服务端标准答案：children[].value 发空串，完成度可能为 0；…"]
}
```

同一份数据用 `submit()`（默认干跑）得到：

```
干跑（submit_live=False）：未发送任何写请求。groupId=u1g1 submitType=1
题目数=2 计分点=9（可提交）。U校园转发服务端标准答案，answers 参数不参与载荷（收到 0 条，仅作对照）。
```

两次调用的网络记录都只有 `["GET", "GET"]`（内容 + 答案），**没有任何 POST**。

---

## 7. 测试覆盖（`tests/test_unipus_submit.py`，57 条，全离线）

| 分组 | 锁住的行为 |
| --- | --- |
| `TestAnswerObject` | children 形状 / 主观题字符串与占位 / progress 键是下标 / `reply` 标记 / 满分 record 仅显式开启 |
| `TestJudgeObject` | 逐题型键集合（客观题 5 键、upload 无 value、oral record、oral-state 映射、role-play payloads 长度） / `versions` 默认值 / 逗号连接 / 项数规则 |
| `TestQuesData` | 标准答案原样转发 / children 长度按内容侧 / 缺项填空 / **客观题**子题数不一致 blocker / **非客观题空答案补齐不抛错** / 答案条数不一致拒绝 |
| `TestSubmitPlan` | 10 个键齐全 / `isCompleted` 全 true 且等长 / `submitType` 纯派生 / 纯学习题组可提交 / **discussion + 空 answer 可提交** / **主观题空答案可提交** / 空题目拒绝 / 缺 ID 拒绝 / 显式数组校验 / 摘要可读 |
| `TestParseResult` | 整数 0 接受、字符串 "0" 记录类型后接受、非 0 拒绝 / 空 score / 脏数据容忍 |
| `TestAdapterDryRun` | 默认干跑**零 POST** / `answers` 只做对照 / plan 摘要字段 / 无 http 时的说明 |
| `TestAdapterLive` | 真发 POST 的 URL 与逐键载荷 / 回读 `finished` / 校验不过拒绝且零 POST / 非 0 响应抛 `UnipusApiError` / 限流上抛不重试 / 回读失败软着陆 / 回读限流不吞 / 空内容拒绝 |
| `TestNativeHooks` | 能力与 `native_submit_flow` / `native_plan` 零 POST / **discussion 空 answer 不被拦** / blockers 呈现 / `native_submit` 真发并回读 / blocked 时拒绝 / 空内容明确失败 |

---

## 8. 接线状态与需要 Lead 处理的事

- **控制台链路已通**：`api/service.py:_submit_one` 在 `native_submit_flow=True` 时
  走 `native_plan`（confirm=false）或 `native_submit`（confirm=true），
  并消费 `question_count`/`answered`/`submitted`/`server_finish`/`dry_run` 等字段。
- **CLI 跑批已由 Lead 接线**：`tasks/pipeline.py` 新增
  `_native_solve_and_submit`，对 `native_submit_flow=True` 的平台先调
  `native_plan`（干跑只读；`dry_run=True` 时不会调 `native_submit`），
  blocker 非空时明确失败。该改动属 Lead 的文件，本报告登记、未参与修改。
- **`api/actions.py` 仍没有注册 unipus 的提交动作**：`kedaibiao action unipus …`
  目前无此动作（api/ 属 Lead）；控制台「作业」页的干跑/真交不受影响。
- **旧断言已更新**：`tests/test_unipus.py::TestAdapterContract` 的两条旧断言
  （`capabilities.submit=False`、`submit` 抛 `NotSupportedError`）已由 Lead 改成
  「能力为 True / 默认干跑且零 POST」；当前全量 1499 条测试 **全绿**。
- **仍待 Lead 顺手改的注释**：`src/kedaibiao/platforms/unipus/__init__.py` 的
  docstring 与 `api/service.py` 的几处注释还写着「unipus 提交未实现
  （capabilities.submit = False）」，属共享区，我没有改。

---

## 9. 写文件纪律

本任务只改：

```
src/kedaibiao/platforms/unipus/submit.py       （重写）
src/kedaibiao/platforms/unipus/adapter.py      （只改提交相关；登录/is_logged_in/解析未动）
tests/test_unipus_submit.py                    （重写）
docs/verification/unipus-submit.md             （新建）
```

未改 `api/`、`endpoints.py`、`parsing.py`、`client.py`、`README.md`，未 `git commit`，
未提交 `data/` 下的联调样本（已被 `.gitignore` 覆盖）。
（工作区里 `tests/test_unipus.py`、`tasks/pipeline.py` 的改动是 Lead 的，不属于本任务。）

---

## 10. Lead 补充：2026-10-20 真实提交实测（一组）

> 上面正文里「线上访问：零」描述的是 **task-2 实现与验证过程**（确实零请求）。
> 本节是 Lead 在同一账号上做的**唯一一次真实提交**，用来确认载荷字段语义。

| 项 | 值 |
| --- | --- |
| 任务组 | 「大学英语I（二）B班 · 视听说教程1」Exercise 1，`groupId=6ea8bfd48002a2c` |
| 为什么选它 | 平台已记录 `first_pass` 且 `score=[1,1,1]`、`score_pct=1`（已满分完成）；提交内容是**服务端标准答案**，即使平台重录也不会把成绩改差 |
| 请求 | `POST /course/api/v3/newExploration/submit`（`x-annotator-auth-token`） |
| 响应 | `code=0`（**整数**）、`score_pct=1.0` |
| 回读 `progress/v2` | `server_finish=True`、`score=[1,1,1]`、`score_pct=1.0`、`first_pass` 更新为本次提交时间 |
| 课程进度 | 提交前后都是 **18/18**，未回退 |
| 结论 | `isCompleted` / `thirdPartyJudges` 的长度与形状、`answer.children`、`submitType=1` 全部被服务端接受 —— **载荷语义第一次得到线上确认** |
| 仍未验证 | 非客观题（oral / role-play / upload）、字符串 `"0"` 成功码、纯学习题组（`submitType=2`）、限流表现 |

复现（同一条命令，注意 `confirm=true` 才会真发）：

```bash
PYTHONPATH=src python3 -m kedaibiao action unipus submit \
  assignment_id='course-v2:74fa98b310020a7+nsce_3_vls_1+20240130/6ea8bfd48002a2c'
# 加 confirm=true 才是真提交
```

同时修掉两处**线上真实形状**与规格不一致导致的解析崩溃（`parsing.parse_group_state`）：

- `first_pass.datetime` 是**整数**（epoch 秒），规格记的是字符串；
- `state.score` 是**字符串化的 JSON 数组**（`"[1,1,1]"`），规格记的是数组。

两者现在都兼容，并各有一条离线用例锁住（`test_live_shape_int_datetime_and_stringified_score`）。
