# uai-core 提交载荷事实取证（只读源码调研）

本文件补齐 U校园（Unipus）适配器里两处**取值语义未确认**的字段（`isCompleted` / `thirdPartyJudges`），
并顺带回答子题结构、题型判定、目录遍历、成功码、写端点等 8 个问题。
规格层面的事实见 [`unipus-protocol.md`](unipus-protocol.md)；本文件只补它标为「未验证 / 未读取」的部分。

## 头部

| 项 | 值 |
| --- | --- |
| 调研对象 | [Yunlk/uai-core](https://github.com/Yunlk/uai-core)（Rust，`uai-core` lib + `uai` CLI） |
| 版本 | `v0.1.2`（`Cargo.toml` 里的 `version = "0.1.2"`） |
| commit | `4628e444e8e618767544ddffb27d9e6e20ed97f5`（main，2026-09-27 提交，提交信息为 README/CLI 帮助对齐 + 版本号 0.1.1→0.1.2） |
| 调研日期 | 2026-10-20 |
| 取证方式 | 在 `/tmp` 下做**一次性只读克隆**（`git clone --depth 1` 后 `git fetch --depth 1 <commit>` + `checkout FETCH_HEAD` 钉到上表 commit），只用 `git ls-tree` / `grep` / `nl -ba` 阅读文本；**未编译、未运行、未执行参考实现的任何代码**；调研结束即删除该目录 |
| 网络边界 | 全程只访问 `github.com` 取源码；**没有对 `*.unipus.cn` 发任何请求** |
| 许可边界 | 参考实现采用自定义「UAI-Core 非商业使用许可 1.0」：允许使用/改写/再分发，**禁止商业使用**，衍生作品须同等条款。本文件因此**只记录功能性事实 + 本项目自己的复述 + `file#Lx-Ly` 引用**，不含参考实现任何代码、注释、文档原文的成段转载（代码键名、常量、JSON 形状属于协议事实，单独记录）。 |

**一句话总览**：`isCompleted` 的元素**恒为 `true`**；`thirdPartyJudges` 是**逐计分点的对象数组再字符串化**，元素形状随题型而定；两者长度由「内容侧 `child_count` + 题型规则」决定，与 `submitType` 无关。

---

## 1. `isCompleted` 到底怎么填

### 结论（可直接照做的规则）

1. **元素值恒为 JSON 布尔 `true`，全库不存在 `false`。** 提交侧唯一的填充动作是「按项数 push `true`」，
   没有任何分支会写 `false`（在 `src/` 下检索 `Bool(false)` 零命中）。所以「哪些子题做对了」**不通过**这个数组表达。
2. **数组长度 = 该组所有「非学习题」的贡献项数之和**：
   - 学习题（`study`）：**0 项**，且整个题目在填充时被 `continue` 跳过；
   - `role-play`、`oral-state`：**恒 1 项**，与子题数无关；
   - 其余题型（`oral` / `upload` / `subjective` / 6 种客观题 / 兜底 `objective`）：`max(child_count, 1)` 项
     （`child_count == 0` 但仍算真题目时按 1 项——对应前端对 0 子题会回退成 1 项的行为）。
3. **`submitType` 不改变这个数组的填法。** 构造请求体的函数里没有任何 `submitType` 分支；
   `submitType` 是**纯派生值**（见第 4 节）。因此：
   - `submitType == 1`（混合组）：数组非空，逐非学习题展开；
   - `submitType == 2`（整组都是学习题 / 纯内容块）：数组为 `[]`，`thirdPartyJudges` 为字符串 `"[]"`；
   - 不存在「`submitType=2` 却带非空数组」的正常路径。
4. **`submitType == 2` 的组也必须提交**：两个数组虽空，但 `quesDatas` 里的 `answer.progress`（媒体已播标记）
   与 `children[].isDone` 就是靠这一次写请求记录的（参考实现明确反对「学习题组不值得提交」的旧逻辑）。
5. **与 `thirdPartyJudges` 严格等长**：填充循环里两边用同一组计数（`is_completed_len()` 与 `judge_count()` 恒等），
   并在调试构建里断言相等；提交前还有一次显式校验（不等就本地报错，不去撞服务端 `300100`）。

### 证据

- 填充循环（跳过学习题 → 逐项 push `true` → 逐项 push 一个 judge）：`src/question/mod.rs#L560-L579`。
- 具体 push 的语句：`src/question/mod.rs#L570-L572`（`completed.push(Value::Bool(true))`）。
- 长度定义与「judge 数恒等于 isCompleted 数」：`src/question/mod.rs#L115-L131`。
- 学习题判据（含 `video-popup` 例外、`discussion`/`rich-text-read`、纯内容块）：
  `src/question/mod.rs#L87-L108`；学习题项数恒 0：`src/question/study.rs#L46-L48`。
- 恒 1 项的两种题型：`src/question/role_play.rs#L51-L54`、`src/question/oral_state.rs#L42-L45`。
- 默认按子题展开（至少 1）：`src/question/mod.rs#L258-L261`。
- `submitType` 纯派生：`src/question/mod.rs#L505-L513`；请求体里两者一起写入：`src/question/mod.rs#L587-L598`。
- 等长断言与提交前校验：`src/question/mod.rs#L581-L585`、`src/api/submit.rs#L587-L612`。
- 纯学习题组要提交的理由：`src/question/mod.rs#L515-L529`；`src/api/submit.rs#L276-L283`。
- 空题组的边界：`all()` 对空集合为真，所以「0 道题」会让 `submitType` 算成 `2`；但参考实现在解析不出任何题目项时
  **直接报错拒绝提交**（`src/api/submit.rs#L175-L183`），不会走到这一步。

### 仍未确认

- 服务端是否**逐项校验** `true`（即传 `false` 会被如何对待）——源码只证明客户端**只发 `true`**，没试过 `false`。
- `isCompleted` 长度是否还必须在服务端与 `quesDatas` 的某种计数相等——源码只按 `child_count` 对齐，未做线上核对。

---

## 2. `thirdPartyJudges` 的元素内容与序列化

### 结论

1. **类型**：请求体里它是一个**字符串**（`thirdPartyJudges` 键的值是 JSON 字符串），内容是一个 JSON **数组**；
   数组元素是**对象**，`{}` 也不是空字符串，**绝不存在 `""` 或 `null` 元素**。
2. **长度**：与 `isCompleted` 逐项一一对齐（同一循环里同时 push 两边，且 `judge_count() == is_completed_len()`）。
   整组纯学习题时是空数组，序列化后为字符串 `"[]"`。
3. **元素的形状由题型决定**（这是「怎么序列化」之外的真正关键）。按 `question_type`/`reply_type` 分派，
   元素至少含 `question_type`、`reply_type`、`versions`，再按类型增删：

   | 题型 | 元素字段（我按源码归纳） |
   | --- | --- |
   | 客观题族（single/multi/dropdown/fill-blank/banked-cloze/sequence/兜底 `objective`） | `value`（字符串，多值逗号连接）、`question_type`、`reply_type`、`versions`、`payloads: []` |
   | `subjective` | 与客观题**完全相同**的 5 个键（`value` 字符串、`payloads: []`） |
   | `upload` | `question_type`、`reply_type`、`payloads: []`、`versions`、`desc: ""`（**没有 `value`**） |
   | `oral` | `value`、`payloads`（**1 个**满分 record）、`specific_scores`（四项 100）、`versions`、`question_type`、`reply_type` |
   | `oral-state` | `specific_scores`、`payloads`（1 个满分 record）、`versions`、`question_type`、`reply_type`、`value`（**对象/映射**：前 n-1 个子题按下标收进去，单值给字符串、多值给数组） |
   | `role-play` | `role: ""`、`specific_scores`、`payloads`（**长度 = 子题数**，逐子题一个满分 record）、`versions`、`question_type`、`reply_type`（**没有 `value`**） |
   | `study` | 不产生元素（该题的 `judge` 实现存在但不会被调用） |

   其中 `versions` 默认是 `{"course": "", "group": 1, "template": 1, "answer": 3, "content": "0"}`
   （`course` 取书目版本，`content` 默认 `"0"`；参考实现称服务端不强校验这组值）。
4. **填空/未作答等场景下 `value` 是空串 `""`，不是 `null`**：服务端会因数量/形状不匹配而报错，这是踩过的坑。
5. **序列化方式**：Rust 侧对 `Vec<Value>` 直接做紧凑序列化（无空格、非 ASCII 不转义），再作为字符串字段塞进请求体。
   Python 等价写法：`json.dumps(judges, ensure_ascii=False, separators=(",", ":"))`。
6. **计分点的粒度**：`oral` / `subjective` / `upload` 是「按子题一项」，`oral-state` / `role-play` 是「整题一项」
   （但 `role-play` 那一项内部的 `payloads` 又按子题展开——这是全库唯一「内外长度故意不一致」的题型）。

### 证据

- 字符串化与写入请求体：`src/question/mod.rs#L594`；等长断言：`src/question/mod.rs#L581-L585`。
- 各题型 `judge()` 的字段集合：`src/question/objective.rs#L36-L44`、`src/question/subjective.rs#L63-L71`、
  `src/question/upload.rs#L41-L49`、`src/question/oral.rs#L50-L59`、`src/question/oral_state.rs#L47-L70`、
  `src/question/role_play.rs#L56-L65`（含「无 `value`」与「payloads 逐子题」两处说明）、`src/question/study.rs#L50-L54`。
- 逗号连接（多值）：`src/question/mod.rs#L227-L235`；空作答为空串：`src/question/single_choice.rs#L95-L113`。
- 满分 record 与四项分数：`src/question/record.rs#L41-L57`、`#L60-L71`、`#L77-L90`。
- `versions` 默认值：`src/question/mod.rs#L464-L494`。
- 「`thirdPartyJudges` 发空数组才是 `300100` 的真正原因」与三种长度组合的对照：`src/api/submit.rs#L23-L33`；
  客户端校验：`src/api/submit.rs#L587-L612`。

### 仍未确认

- 服务端是否要求对象里**每个**键都存在（例如客观题少 `versions` 会不会被拒）——源码只给了一套默认值，未做字段裁剪实验。
- `specific_scores` 的量纲（0~100，已在源码注释中说明并给出 `total=100` 的实测快照）在本项目中是否照搬，是产品决策。

---

## 3. `answer` 内部结构与 `progress` / `record`

### 结论

`answer` 本身是**字符串化 JSON**，内部四个键：

1. `value`：**题级恒为空数组 `[]`**，不使用。
2. `children`：数组，长度是 `max(child_count, 1)`——注意**按内容侧子题数**，不是按 `isCompleted` 项数。
   - 每项固定两键：`value` 与 `isDone: true`（恒为 `true`，不存在 `false` 的写法）。
   - `children[i].value` 的形状：
     - **默认是字符串数组**，元素就是服务端标准答案里 `children[i].answers[]` 的字符串**原样**；
       多选/排序**保持数组原顺序**，这里**不做逗号连接**（逗号只出现在 `thirdPartyJudges[].value`）。
     - **主观题例外：必须是单个字符串**，实现上把该子题的多个值用换行符 `\n` 连接（实测交空数组会让完成度恒 0）。
   - 学习题在 `quesDatas` 里**也有** `answer`（children 按 `max(child_count,1)` 生成），只是不贡献判分项。
3. `progress`：对象。**键 = 媒体项在内容 `contents` 数组里的下标**（字符串形式），值恒 `true`；
   只对 `contents[].type` 为音频(1)/视频(2) 的项打标记。空的时候就是 `{}`。
   - **主观题额外加一个 `progress.reply = true`**（与媒体下标那套互不相干，是文本作答组件的完成事件）。
4. `record`：
   - 非口语/非角色扮演：恒为 `{"url": ""}`；
   - `oral` / `oral-state` / `role-play`（凡「录音类」）：填**满分 record**：
     `{"url": "", "specific_scores": {total:100, accuracy:100, integrity:100, fluency:100},
       "recordDetail": {score:100, smooth:100, completed:100, correctness:100, audioUrl:"", details:[], relevance:100}}`；
   - 「录音类」的判定= `reply_type == "record"`（口语）或 `question_type` 命中一批口语题型名（含 `record`/`oral` 子串）
     或 `question_type ∈ {role-play, roleplay}`。
   - `record` 为满分的原因是协议事实：分数由客户端本地赋值、服务端照存（不是服务端评测结果）；`audioUrl` 留空即可。
5. 其余外层键（`quesDatas` 每项）：`instanceId`（**字符串**，19 位，绝不能过 `f64`）、`context`（字符串化
   `{"state":"submitted"}`）、`contextVersion: 1`、`answerVersion`（从 1 开始）。

### 证据

- `answer` 的完整构造：`src/question/mod.rs#L358-L401`；`progress` 键的来源与主观题 `reply`：`#L380-L386`；
  `record` 分支：`#L388-L392`。
- children 长度与 `value` 形状分派：`src/question/mod.rs#L370-L378`、`#L263-L269`；
  主观题的字符串化（`\n` 连接）：`src/question/subjective.rs#L57-L61`。
- `quesDatas` 每项五键：`src/question/mod.rs#L403-L412`。
- 媒体下标只在 `type ∈ {1,2}` 上打点：`src/question/parse.rs#L29-L33`、`#L172-L211`。
- 满分 record：`src/question/record.rs#L41-L90`；录音类判定：`src/question/mod.rs#L148-L166`、`#L175-L185`。
- 未作答 `value` 为空串而非 null：`src/question/single_choice.rs#L95-L113`。
- 多选/排序保序、逗号只用于 judge：`src/question/multi_choice.rs#L3-L17`、`src/question/sequence.rs#L3-L15`。

### 仍未确认

- `children[i].value` 里的字符串在**非多选**题型上是「选项字母」还是「选项原文」：源码只做原样转发（规格 U2）。
  唯一硬证据是多选题的服务端存回快照形如 `"A,B,D,F,G"`（`src/question/multi_choice.rs#L13` 引用的实测）；
  单选示例给的是 `["D"]`；填空/下拉的注释声称是「答案文本 / 选中项文本」，但**未独立验证**。
- `progress` 是否需要按 `contents` 里所有可播放项都给 `true`（还是只给已播的）——源码默认「全部标记已播」。

---

## 4. 题型判定（13 个类型名与顺序）

### 结论

分派是**两段式**：先判「学习题」，再按注册表顺序问「谁认领」，最后落到兜底。类型名共 13 个：

| # | 名字 | 判定条件（`reply`=`replyType`，`kind`=`type`） | 贡献项数 |
| --- | --- | --- | --- |
| 0 | `study` | `kind != "video-popup"` 且满足其一：`category == 0`；`kind ∈ {discussion, rich-text-read}`；`child_count == 0` 且 `reply` 为空（纯内容块） | 0 |
| 1 | `role-play` | `kind ∈ {role-play, roleplay}` | 1 |
| 2 | `oral-state` | `kind == "oral-state"`（`reply` 也是 `record`，**必须排在 oral 前**） | 1 |
| 3 | `oral` | `reply == "record"`；或 `kind` 命中 10 个口语题型名；或 `kind` 含 `record` / `oral` | `max(child_count,1)` |
| 4 | `upload` | `kind == "multiFileUpload"`；或 `reply ∈ {multiFileUpload, multimedia_upload}` | 同上 |
| 5 | `subjective` | `reply == "text-area"` | 同上 |
| 6 | `single-choice` | `reply == "singlechoice"` | 同上 |
| 7 | `multi-choice` | `reply == "multichoice"` | 同上 |
| 8 | `dropdown` | `reply ∈ {dropdownSelection, dropdownchoic, dropdown}`（真实值是 `dropdownSelection`，另一个是历史拼法） | 同上 |
| 9 | `fill-blank` | `reply ∈ {fillblank, fillblankScoop}` | 同上 |
| 10 | `banked-cloze` | `reply ∈ {bankedcloze, material-banked-cloze}`；或 `kind` 含 `bankedcloze`/`banked-cloze` | 同上 |
| 11 | `sequence` | `reply == "sequence"`；或 `kind` 含 `sequence` | 同上 |
| 12 | `objective` | 兜底：前面都不匹配（含教师自建 `magic_19_*` 等未知类型） | 同上 |

`submitType` 恒为 1 或 2：**该组全部题目都是学习题 → 2，否则 1**。判据只有 `is_study()`，与题型种类数无关；
空题组因空集合 `all()` 为真也会得到 2，但参考实现在「解析不出任何题目项」时先报错（见第 1 节）。

`category` 的实测含义：`0` 学习题、`1` 客观题、`7` 口语、`8` 主观题——**真实客观题是 1 不是 0**。

### 证据

- 学习题判据：`src/question/mod.rs#L76-L108`；注册表顺序：`#L278-L298`；兜底分派：`#L300-L311`。
- 判据实现：`src/question/role_play.rs#L47-L49`、`src/question/oral_state.rs#L37-L40`、`src/question/oral.rs#L41-L48`、
  `src/question/upload.rs#L37-L39`、`src/question/subjective.rs#L54-L56`、`src/question/single_choice.rs#L31-L33`、
  `src/question/multi_choice.rs#L31-L33`、`src/question/dropdown.rs#L26-L32`、`src/question/fill_blank.rs#L34-L36`、
  `src/question/banked_cloze.rs#L22-L28`、`src/question/sequence.rs#L30-L34`、`src/question/objective.rs#L31-L34`。
- `submitType`：`src/question/mod.rs#L505-L513`。
- `category` 实测分布：`src/question/study.rs#L17-L27`；`src/question/mod.rs#L64-L67`。
- 「oral-state 必须先于 oral」的原因：`src/question/mod.rs#L282-L285`、`src/question/oral_state.rs#L17-L22`。

### 仍未确认

- 口语题型名那 10 个字符串来自前端枚举名的**小写猜测**（真实枚举成员名在另一个 bundle 里，参考实现手上没有）；
  真正可靠的判据是 `reply_type == "record"`。本项目的 `classify_question` 已同样以 `record` 为主，可保留。

---

## 5. 解密后的子题结构（规格 U1）与答案字符串的对应（U2）

### 结论

**先说实话：参考实现并没有「读懂题目」。** 它读的字段是一个很小的子集，其余结构在源码中**从未被读取**，
因此下面分两块：**已确证的字段** 与 **源码未包含、必须抓包的部分**。

#### 5.1 解密后内容（`course-v2:` 数组 schema）有源码依据的字段

| 层级 | 字段 | 说明 |
| --- | --- | --- |
| 数组元素（外层） | `id` | 题目 `instanceId`（19 位，需按字符串处理） |
| | `type` | 题型名（`question_type` 的优先来源） |
| | `replyType` | 兜底用（优先取 `inner.replyType`） |
| | `content` | **字符串化 JSON**（也可能已经是对象）→ 解析成 `inner` |
| `inner` | `children` | 子题数组；参考实现**只取长度** |
| | `contents` | 内容块数组（媒体/正文等），见下 |
| | `replyType` / `type` / `category` | 题型与分类判别 |
| `children[]` | `rule.text` | **朗读/口语题的题干文本**（HTML，如 `<p>…</p>`） |
| | `rule.keywords`、`rule.words` | 与 `rule.text` 同级的判分辅助字段（源码示例里出现过，未参与逻辑） |
| | `config.recordTime` | 录音时长上限（示例值 `9000`，单位按毫秒理解） |
| | `answers` | ⚠️ 这是**答案接口**那一侧的 `children[].answers[]`，不是内容侧；见 5.3 |
| `contents[]` | `type` | `1` 音频、`2` 视频、`4` 正文、`5` 词汇/术语条目、`6` 单词卡（实测集合） |
| | `text` | 文本正文（`type=4` 等）；`<p>` 段落就在里面 |
| | `path`、`duration` | 媒体路径与时长（`type=1/2`） |
| | `vttPath`、`subtitles`、`subtitlesPath` | 字幕/字幕文件（`type=1/2`） |
| | `children`、`explanations` | `type=4` 正文的嵌套结构/解析 |
| | `id`、`name` | `type=5` 只有这两个（不可播放）；`type=6` 还有 `category`/`examples`/`sound` |

- **「题干」取自 `children[].rule.text`**（仅朗读/口语类是确证的；其它题型的题干字段源码没读）。
- **「选项文本」在源码里完全没有出现**：没有任何一处读取选项数组、选项字母表或选项原文。
  客观题的 `value` 直接来自答案接口，因此参考实现**不需要**选项文本也能提交。
- 媒体判定：只认 `contents[].type ∈ {1, 2}`，其下标即 `answer.progress` 的键。

#### 5.2 答案字符串（U2）与这些字段的对应

- 答案侧外层也是数组，元素里有 `id`（未使用）与 `answer`（**字符串化 JSON**）。
- 二次解析后的 `answer.children[i].answers[]` 是**字符串列表**，参考实现按下标一一取出，
  **原样**填进 `answer.children[i].value`（数组）或逗号连接后填进 `thirdPartyJudges[i].value`。
- 也就是说：**内容侧 `children[i]` 与答案侧 `children[i]` 只靠下标对齐**，两边字段名不同
  （内容侧 `children[]` 是子题对象，答案侧 `children[].answers[]` 才是答案），中间**没有任何语义解码**。
- 「`"A"` 是选项序号还是选项原文」在源码里无解——它从不解码。唯一旁证：多选题的服务端存回快照是字母形式
  `"A,B,D,F,G"`，单选示例是 `["D"]`，而填空/下拉的注释声称是文本。**结论：转发服务端标准答案即可，别自己造答案。**

#### 5.3 证据

- 内容解析（只读 `id`/`content`/`children` 长度/`replyType`/`type`/`category`/`contents[].type`）：
  `src/question/parse.rs#L126-L170`；媒体下标：`#L172-L211`；`contents[].type` 的实测取值表：`#L176-L192`。
- 两套 schema（`course-v1:` 对象 / `course-v2:` 数组）：`src/api/content.rs#L3-L10`、`src/question/parse.rs#L13-L20`。
- 答案侧取 `children[].answers[]` 并原样返回：`src/question/parse.rs#L216-L264`；外层下标对齐：
  `src/api/answer.rs#L49-L59`。
- 答案接口只有客观题有内容（口语/主观题为空）：`src/api/answer.rs#L16-L24`。
- `children[].rule.text` / `config.recordTime` 的样例来自源码内的测试夹具（注释称其形态取自实测组 `u5g341`，
  本调研未看到落盘的真实解密内容），以及据此实现的「逐子题取朗读文本」逻辑：
  `src/bin/uai/commands.rs#L862-L892`、`#L2267-L2300`；HTML 反转义与剥标签：`#L894-L941`。
- 「只显示题型与媒体下标、不试图解析题干」的 CLI 行为：`src/bin/uai/commands.rs#L650-L699`。

### 仍未确认（明确说：源码未包含，仍需抓包）

- **选项字段名与选项文本的取法**：源码零读取。要做「显示题面/选项」必须补一次只读抓包，
  把一份真实解密内容落盘分析（规格 U1 的原始建议）。
- **非朗读类题型的题干字段**：只有 `rule.text` 是确证的（口语类）；客观题题干在哪个键，源码未读。
- **答案字符串的语义**（字母 vs 原文）在单选/填空/下拉上未独立验证（规格 U2）。
- `children[]` 里是否还有 `explanation`/`analysis`/`score` 等字段——源码未读，不能断言存在或不存在。
- 5.1 表里 `children[].rule.*` 与 `config.recordTime` 的来源是**测试夹具**（注释称取自实测），不是本调研直接看到的
  线上明文；真实内容里这些键是否稳定存在，仍需一次抓包确认。

---

## 6. 目录容器的键名（规格 §11.1 的未确认项）

### 结论

**目录响应里没有固定的「单元/分组容器键名」**，参考实现也不找容器键：

1. 外层拿 `course` 字段；它是**字符串化 JSON**，需要二次解析；`course` 缺失时退化为「整个响应体就是 course」。
2. 层级是 `unit → node（可嵌套）→ group`，但**不靠键名**区分，而是递归遍历整棵 JSON 树，
   看每个对象的 `role` 字段：`role == "unit"` 是单元，`role == "group"` 是分组。
3. ID 一律取节点的 `url` 字段（单元 ID、分组 ID 都是），名字取 `name`，分组另有 `base`（逗号分隔的多值标签）
   与 `tab_type`。
4. 单元可能被重复引用：按单元 ID 去重，重复出现时把分组**并入同一个单元**。
5. 分组与单元的归属是**结构嵌套**：从单元节点往下递归收分组，因此嵌套的中间节点不影响归属。
6. 目录里**没有**必修标记（分组节点可见字段只有 `base`/`name`/`ref-out`/`role`/`tab_type`/`url`）；
   必修在另外两个接口（单元级 `progress` 的 `rt.units.<uN>.strategies.required`、任务级 `flowStrategy.required`）。
7. 对照组：**门户的任务树**是有容器键的——`value.list[]` 递归、子层 `children[]`，叶子 `role == "group"`，
   叶子上的 `required` 才是「本班口径」。

### 证据

- `course` 字段二次解析 + 退化到整个 body：`src/api/catalog.rs#L163-L180`。
- 递归找单元（`role == "unit"`、ID 取 `url`、同 ID 合并）：`src/api/catalog.rs#L182-L219`。
- 递归找分组（`role == "group"`、`base`/`name`、去重）：`src/api/catalog.rs#L221-L261`。
- 分组节点只有 6 个可见字段、无 `required`/`flowStrategy`：`src/api/catalog.rs#L16-L42`。
- 目录端点与 `courseId` 不做 URL 编码：`src/endpoints.rs#L113-L117`、`#L154-L157`。
- 门户任务树 `value.list[].children[]` + 叶子 `role == "group"` 的 `required`：`src/api/class_required.rs#L16-L27`、
  `#L252-L258`。

### 仍未确认

- 目录里是否有参考实现没见过的 `role` 取值（如小节/章节节点）——实现是「不认识就跳过」，不会崩，但可能漏分组。
- 单元与分组是否**总是**结构嵌套（即分组是否可能出现在单元节点之外）——参考实现只按嵌套收，未做兜底。

---

## 7. 三套成功码（SSO / 门户 / ucontent）

### 结论

| 家族 | 成功判定 | `"0"`（字符串） | `0`（数字） | `"1"` | `1`（数字） |
| --- | --- | --- | --- | --- | --- |
| SSO（`sso.unipus.cn`） | 取 `code`，字符串或数字都转成文本后 `== "0"` | ✅ 成功 | ✅ 成功 | ❌ 失败 | ❌ 失败 |
| 门户（`uai.unipus.cn`） | 必须匹配 **JSON 数字 `1`**（字符串形式不算） | ❌ | ❌ | ❌（会被判失败） | ✅ 成功 |
| ucontent（`ucontent.unipus.cn`） | 分端点，见下 | ⚠️ 视端点 | ⚠️ 视端点 | ❌ | ❌ |

ucontent 内部**并不统一**，这是本节最容易踩的地方：

- 有一个「ucontent 成功 = 数字 0」的判定函数，但它在业务代码里**没有被使用**（只有单测在用）。
- **内容/答案密文外壳**：按整数取 `code`，取到且非 0 就失败；取不到（含 `code` 是字符串 `"0"`）就**不判失败**，
  继续往下解 密文。所以 `"0"` 在这里「意外地能过」。
- **提交响应**：按整数取 `code`，`accepted` 要求 `code == 0`；字符串 `"0"` 会被取成「无 code」（记 `-1`）→ 判**未接受**。
- **目录 / 进度类**：完全不检查 `code`，只看 HTTP 是否 2xx。
- 门户的任务树接口把 `code == 1` 硬编码在业务代码里（同 `portal_ok`，字符串 `"1"` 同样不被接受）。

**实现建议**：写三个独立判定（SSO / 门户 / ucontent），不要复用；ucontent 内部再按端点区分
「外壳用宽松判定、提交用严格数字 0」。

### 证据

- SSO：`code` 的字符串/数字归一 + 只认 `"0"`：`src/api/login.rs#L42-L54`；四条断言（`"0"`/`0` 成功、`"1"`/`1` 失败）：
  `src/api/login.rs#L175-L185`。
- 门户只认数字 1：`src/transport.rs#L299-L305`；单测：`#L366-L369`；业务调用点：
  `src/api/user_info.rs#L86`、`src/api/bookshelf.rs#L121`、`src/api/course_list.rs#L121`、`src/api/assessment.rs#L401`、`#L493`。
- ucontent 判定函数（数字 0，未被业务使用）：`src/transport.rs#L294-L297`。
- 密文外壳：整数非 0 才失败、取不到 code 视为不失败：`src/question/parse.rs#L60-L67`。
- 提交响应：`code` 按整数取、`accepted = (code == 0)`：`src/api/submit.rs#L109-L142`、`#L70-L79`。
- 任务树硬编码 `code == 1`：`src/api/class_required.rs#L244-L250`。

### 仍未确认

- 线上 `code` 的**真实 JSON 类型**（数字 or 字符串）——源码只证明客户端做了什么假设，本调研未发请求核对。
- ucontent 各端点在失败时是否都带可读 `message`/`msg`（源码按 `message`→`msg`→`error`→`errMsg` 顺序取）。

---

## 8. 媒体 / 学习时长 / 语音：参考实现了哪些写端点

### 结论（一句话级别，不做深挖）

| # | 端点 | 方法 | 请求体形状（一句） | 状态 |
| --- | --- | --- | --- | --- |
| 1 | `/course/api/v3/newExploration/submit` | POST | 见第 1–3 节的整包提交体 | **已接线**，唯一主写接口 |
| 2 | `/course/api/v3/voiceEvaluation` | POST | `{block_id, course_id, score_details: []}`；服务端**不评测**，只存客户端算的分数 | 已接线（但默认不用） |
| 3 | `/media/user_resource/cms/token` | GET | 无参，取七牛上传 token；响应结构参考实现自称**未实测**，原样返回 | 未接线 |
| 4 | `/media/user_resource/fetch` | POST | 原样透传调用方给的 payload（登记已上传资源）；**未实测** | 未接线 |
| 5 | `wss://ucontent.unipus.cn/unipusiopoint/?EIO=3&transport=websocket` | WS | namespace `/userActivities`，事件 `kick-v2`，载荷是 `["kick-v2", {module, moduleGroup, client:"U校园mobile", url, tag1, tag2, tag3, appId:116, serviceId:"point", sendTimestamp, openId, timer}]`；每 20 秒发心跳 `2`；**载荷里没有课程/分组 ID**（账号级记账） | 已接线（时长加速） |
| 6 | `wss://speech.unipus.cn`（评测） | WS | 认证帧 + 参数帧（`tokenId`、`audio` 格式、`request.apiName/transcript/sig`）+ PCM 分片 + `{"stop":true}` | 已接线，但**只评分、不写服务端状态** |

补充事实（学习时长的逐字报文、URL 与常量见第 10 节附录）：

- 学习时长走的是**账号级 `(module, moduleGroup)` 记账**，无法定位到某本教材的必修项；门户另有按教材的时长账本
  （考核接口），两者别混。
- `kick-v2` 只在「已入命名空间」的状态下被接受；重复 kick 会让计时停止（参考实现的经验，未独立复现）。
- 媒体上传链（token → 上传 → 登记 → 把 URL 回填 `record.url`/`recordDetail.audioUrl` → 语音评测）**参考实现没跑通**，
  因此「真实录音 + 真实 audioUrl」这条路在本项目里仍属未验证。

### 证据

- 写接口常量：`src/endpoints.rs#L129-L130`（提交）、`#L146-L150`（口语上报、媒体登记/上传）。
- 口语上报的请求体与「服务端不评测」：`src/api/media.rs#L45-L87`；token 与登记未实测：`#L89-L108`（含「上传链未接线」常量）。
- 时长加速：端点/命名空间/事件：`src/api/duration.rs#L1-L34`、`#L117-L135`；`kick-v2` 载荷构造：`#L192-L220`；
  心跳与重连参数：`#L120-L127`；账号级记账的限制：`#L43-L56`。
- 语音评测的帧序列与返回：`src/api/speech_eval.rs#L1-L50`。

### 仍未确认

- 媒体上传链全链路（规格 U8）、语音评测服务端行为（U9）、时长加速的倍率与稳定性（U12 一类）。
- 上述 WS 链路都属于**风控敏感**能力，本项目的合规定位需要单独决策，本文件只登记事实。

---

## 9. 对 kedaibiao 的落地建议

### 9.1 可以直接改 `src/kedaibiao/platforms/unipus/submit.py`（事实已确定）

1. **`isCompleted` 的默认值已经是对的**：元素恒为 `true`，长度 = 各非学习题的贡献项数之和。
   `build_submit_plan` 里 `[True] * item_count` 与参考实现逐字节等价，可以把 docstring 里
   「取值语义未确认」的措辞改掉，只保留「长度语义已确认、逐项恒 true」。
2. **`thirdPartyJudges` 必须从「`[None] * n`」改成「逐项对象」**（当前默认序列化会得到 `[null,null,…]`，
   与参考实现差得最远）。建议：
   - 新增 `build_judge_object(question, standard_answer, index)`，按 `question.unipus_type` 分派（第 2 节表）；
   - `versions` 用默认值 `{"course": "", "group": 1, "template": 1, "answer": 3, "content": "0"}`；
   - 客观题族/主观题：`value` 为逗号连接的答案字符串、`payloads: []`；
   - `upload`：无 `value`，`payloads: []`，`desc: ""`；
   - `oral`：`value` + 1 个满分 record + `specific_scores`；
   - `oral-state`：`value` 是映射（前 n-1 个子题）、1 个满分 record；
   - `role-play`：无 `value`、`payloads` 长度 = 子题数、`role: ""`；
   - 序列化用 `json.dumps(..., ensure_ascii=False, separators=(",", ":"))`。
3. **`children` 的长度要用内容侧 `child_count`，不是 `len(standard.children)`**：
   参考实现是 `max(child_count, 1)`。当前 `build_ques_data` 直接用答案侧的 children 长度，
   答案缺项时会把 `answer` 发短（结构合法但判错）。建议改为按 `child_count` 生成，逐项从
   `standard.children[i]` 取，缺项填空数组并在 `warnings` 里写明。
4. **主观题的 `children[i].value` 必须是字符串**（多个值用 `\n` 连接），不是数组；
   并补占位文本（参考实现用 `[已提交]`，本项目若要沿用需在 UI 上明示「这句不是学生写的」）。
5. **`progress` 的键是 `contents` 下标，不是 `type` 值**——这是当前最实在的一个 bug：
   `parsing.py` 的 `media_types` 存的是 `contents[].type` 的**取值**（1/2/…），
   `submit.py` 却把它当 `media_indexes` 用，结果会发出 `{"1": true, "2": true}`。
   修法：`parsing.py` 在遍历 `contents` 时用 `enumerate` 另存 `media_indexes`（只收 type ∈ {1,2}），
   `submit.py` 改用它。
6. **主观题额外 `progress.reply = true`**：当前 `build_answer_object(reply=...)` 已有开关，但 `build_ques_data`
   从没传 `reply`。应由 `question.unipus_type == "subjective"` 决定。
7. **口语/角色扮演的 `record`**：当前恒 `{"url": ""}`，等于不报分（口语类大概率拿不到分）。若要拿分，需要发满分 record
   （第 3 节结构 + 第 8 节证据）；是否照搬「客户端自报满分」属产品/合规决策，代码上建议做成显式开关，
   默认保守（只交完成度，不刷分），并在 `warnings` 里说明「本题组拿不到口语分」。
8. **`submitType` 判据改为复用 `parsing.classify_question` 的结果**（`unipus_type == "study"`）：
   现有 `_is_learning` 只看 `category == 0`，漏了 `video-popup` 例外、`discussion`/`rich-text-read`、
   以及「`child_count == 0` 且 `reply_type` 为空」的纯内容块——后者恰恰是最常见的「纯学习组」。
9. **守住两条本地校验**（参考实现都做了，服务端不会告诉你哪里错）：
   `len(isCompleted) == len(thirdPartyJudges)`；`quesDatas` 为空时**不发**（规格 U7 未验证，参考实现选择报错）。
   另建议补一条参考实现**没做**的校验：内容侧子题数 vs 答案侧子题数，不一致就标记该组「不可提交」
   （这正是规格 §11.2 第 2 点指出的风险）。
10. **提交成功判定的口径**：现有 `parse_submit_result` 用 `str(code) == "0"`，数字 `0` 与字符串 `"0"` 都接受；
    参考实现的提交判定要求**整数** `code == 0`（字符串 `"0"` 会被当成「无 code」→ 判未接受）。
    数字场景两者一致，只有 `code` 真是字符串时才分歧。建议「优先整数比较、取不到整数再退回字符串比较」，
    并把实际类型记进日志，用一次真实响应确认后固化。
    `data.state.state == 1` 只表示「判分通过」，不等于班级计分点达标（要回读 `progress/v2`）。

### 9.2 可以改 `src/kedaibiao/platforms/unipus/parsing.py`

1. 新增 `media_indexes` 字段（`contents` 下标），保留 `media_types`（类型值）供题型判断/展示用。
2. **目录解析改为「结构嵌套归属」**：现在的 `order[-1]`（最近一个单元）在目录出现「先 unit 后并列 node」
   或重复引用时会挂错单元；参考实现是从单元节点内部递归收分组、按单元 ID 合并。
   另外 `course` 字段缺失时参考实现退化为「整个 body 就是 course」，本项目目前直接抛错，可放宽。
3. **可解析朗读题的题面**：`children[].rule.text`（HTML → 反转义 `\u003c`/`\u0026` → 剥标签），
   以及 `config.recordTime`。这是目前唯一能从源码确证的「题干」字段，可用来填口语类题目的
   `stem`/指纹；**其余题型的题面与选项仍应保持 `stem_unavailable`**。
4. 答案侧对照新事实复核：`children[].answers[]` 原样保留、不排序不去重、不解释语义（已符合）。
5. 若要让 `Category`/`QuestionType` 映射与参考实现一致，注意 `sequence`/`role-play`/`oral-state` 在参考实现里
   都有专属名字（本项目当前把它们映射为 `UNKNOWN`），这不影响提交，只影响展示。

### 9.3 仍需真实抓包才能定，**不要猜**

| 事项 | 为什么源码不够 |
| --- | --- |
| 选项文本字段名、客观题题干字段 | 参考实现零读取（U1）；要做题面展示必须有一次真实解密内容落盘 |
| 答案字符串语义（字母 vs 原文）在单选/填空/下拉上 | 源码只原样转发（U2）；多选有字母形式的旁证，其它题型没有 |
| `quesDatas: []` 是否被接受 | 参考实现明确「没测过，所以不发」（U7） |
| 提交响应 `score` 的粒度（按题 vs 按子题） | 参考实现只打印，未核对（U6） |
| 三套 `code` 的真实 JSON 类型 | 源码只反映客户端假设（第 7 节） |
| `300100` / `index out of range` 的真实触发条件 | 来自参考实现注释，本调研未复现 |
| 口语满分 record 是否被服务端照存 | 参考实现自称实测（快照回读），本项目需自行验证后才会开启 |

> 合规提醒：参考实现是非商业许可、且其「客户端自报口语满分」「时长加速」等能力涉及平台 ToS 与校内规范风险。
> 本文件只陈述协议事实，不构成「应该这么做」的建议；是否启用由本项目产品与合规决定。

---

## 10. 附录：学习时长上报（`src/api/duration.rs`）的逐字事实

本附录应 task-3（时长上报）的要求补写；结论只覆盖「源码里能读到什么」，不涉及是否该做。

### 10.1 `#L118-L130` 是什么

这 14 行**不是**连接逻辑，而是 5 个常量：命名空间 `/userActivities`、心跳间隔 20 秒、断线重连间隔 5 秒、
状态汇报间隔 5 秒、打点页面 URL（`https://ucontent.unipus.cn/_explorationpc_default/pc.html`）。
真正的连接/重连/分段在别处：

- 单房间生命周期 `run_room_once`：`#L500-L589`（连上 → 握手 → 入命名空间 → **只 kick 一次** → 心跳/收广播/回 pong）；
- 房间层重连循环 `room_loop`：`#L474-L498`（一次连接返回/失败后计一次重连、固定睡 5 秒、再连，直到停止标志置位）；
- 总控 `boost_main`：`#L400-L472`（每房间一个 tokio task；主循环每 5 秒汇报一次；`target_secs` 达到就停）。

没有「分段上传」概念：整个协议只有一条 WebSocket 连接 + 心跳 + 收到的服务端广播。

### 10.2 URL 构造（`#L240-L259`）

拼装函数是 `format!("{DURATION_SOCKET_BASE}?EIO=3&transport=websocket&uuid={}&token={}", urlencode(open_id), urlencode(token))`，
其中 `DURATION_SOCKET_BASE = "wss://ucontent.unipus.cn/unipusiopoint/"`（`src/endpoints.rs#L33-L34`）。
即：

```text
wss://ucontent.unipus.cn/unipusiopoint/?EIO=3&transport=websocket&uuid=<open_id>&token=<annotator token>
```

- `uuid` 传的是会话的 **`open_id`**，`token` 传的是**自铸的 annotator token**（不是门户 JWT）。
- 自写的 `urlencode`（`#L261-L275`）只保留 `A-Za-z0-9-_.~`，其余字节一律 `%XX` 大写。
- 注释里明确：少了 `uuid`/`token` 也能握手成功，但服务端**不记账**——即「连上了」不等于「有时长」。

### 10.3 报文逐字（`#L524-L589`，`#L535-L556` 是其中心跳分支）

全部是 engine.io v3 的**文本帧**，没有二进制帧：

| 时机 | 发送的报文（逐字） | 行号 |
| --- | --- | --- |
| 每 20 秒（首拍跳过，即约 20s 后首次） | `2`（单个字符，engine.io ping） | `#L534-L540` |
| 收到服务端 `2`（ping） | `3`（pong） | `#L550-L554` |
| 收到以 `0` 开头的握手包 | `40/userActivities,`（**注意末尾那个逗号**） | `#L558-L562` |
| 收到以 `40/userActivities` 开头的入房回执，且本连接**尚未** kick | `42/userActivities,1` + kick 载荷 JSON | `#L563-L578` |

kick 帧的精确拼法是 `format!("42{NAMESPACE},1{payload}")`（`#L572`），`payload` 是紧凑 JSON 数组，第一个元素是
字符串 `"kick-v2"`，第二个元素是对象，字段如下（**这是全部字段名，无其它**）：

| 字段 | 值 |
| --- | --- |
| `module` | 房间的 module（池中取值，如 `cmgt`/`course`） |
| `moduleGroup` | 房间的 group（池中取值，如 `h5`/`pc`） |
| `client` | 固定 `"U校园mobile"`（桌面端名字记账为 0，所以这里必须冒充 mobile 协议） |
| `url` | 固定打点页 URL（见 10.1） |
| `tag1` | 同 `module` |
| `tag2` | 同 `moduleGroup` |
| `tag3` | **字符串化 JSON**：`{"microBlock":"<module>/<group>","source":"cloud","version":"1"}`（键序按字母序） |
| `appId` | 数字 `116` |
| `serviceId` | `"point"` |
| `sendTimestamp` | 当前毫秒时间戳 |
| `openId` | 会话的 open_id |
| `timer` | 与 `sendTimestamp` 同一个毫秒时间戳 |

⚠️ serde_json 未开启 `preserve_order`（`Cargo.toml#L33` 只是普通依赖），所以序列化时对象键按**字母序**输出：
报文形如 `42/userActivities,1["kick-v2",{"appId":116,"client":"U校园mobile","module":...,...}]`。
JSON 语义上键序无所谓，若要逐字节模仿就按字母序输出。

⚠️ kick **每条连接只发一次**（`!joined` 判断，`#L564-L566`）；重复发会让服务端回「任务没有信息变更」并**停止计时**。

### 10.4 时长口径：不是客户端上报，是服务端广播累加

- 客户端**从来不发送 duration/秒数**。`credited_secs` 的唯一来源是服务端主动推送的 `r_set` 广播：
  报文含 `delta` 与 `msUpdate`（毫秒时间戳，用于去重）；客户端按 `msUpdate` 去重后把 `delta` 累加
  （`#L579-L585`，解析器 `#L222-L238`）。字段名 `credited_secs` 与「已记账秒数」的注释（`#L287-L294`）
  是「`delta` 单位为秒」的依据。
- 也就是说「时长」= **维持连接 + kick 一次 → 服务端周期记账 → 客户端只负责累加广播**。
  心跳 `2` 本身不产生时长；没有「发一条带 duration 的消息」这种接口。
- 达到 `target_secs` 的判据用的是**累加到的记账秒数**，不是墙钟（`#L463-L470`）。
- 单位：`delta` 按秒累加到 `credited_secs`，汇报时换算成小时（`#L329-L340`）；`sendTimestamp`/`timer`/`msUpdate` 才是毫秒。

### 10.5 token/uuid 刷新、退避与间隔

- **没有任何 token/uuid 刷新**：`open_id` 与 `token` 在 `start_boost` 入参里固定，重连复用同一个；
  过期/被拒的表现就是握手失败 → 无限重连，模块内没有换 token 的路径（`#L352-L398`）。
- 重连退避是**固定 5 秒、无限次**，没有指数退避、没有上限（`#L474-L498` + `RECONNECT_DELAY`）。
- 间隔常量：心跳 20 秒（服务端 45 秒断，留余量）、重连 5 秒、状态汇报 5 秒（`#L120-L127`）；
  首拍 ping 被显式跳过，所以第一次心跳在约 20 秒后（`#L521-L522`）。
- 房间数：池上限 `10 module × 6 group = 60`，请求更多会被**静默钳制**到 60；`SAFE_ROOMS = 20` 只是建议常量，
  代码不强制（`#L132-L166`、`#L591-L612`）。同一 `(module, moduleGroup)` 重复会被服务端去重，所以池必须唯一。

### 10.6 仍未确认

- `r_set` 的 `delta` 单位源码只是「按秒累加」的用法，注释未逐字声明单位；60 秒一批的节奏来自注释描述，本调研未复现。
- 服务端对房间数、频率的容忍阈值（注释称 60 房间会随机拒绝握手）属参考实现自述，未独立验证。
- 该链路是否仍被平台接受、是否触发风控，本调研**没有发任何请求**，无法判断。
