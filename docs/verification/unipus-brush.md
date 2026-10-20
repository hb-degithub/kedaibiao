# U校园「刷课」调研：进度到底怎么涨（2026-10-20 对照实验）

> 结论先行：**U校园目前没有独立的「刷课」入口，而且现有两条链路都推不动门户进度**。
> 本文记录实验证据，避免后来的人再从头猜一遍。

## 1. 现在有什么（刷新后用）

| 位置 | 能力 | 默认 |
| --- | --- | --- |
| 工具页 →「学习时长上报」 | engine.io v3 挂时长会话（20s 心跳，可发一次 kick） | 干跑，`confirm=true` 才连 |
| 作业页 →「一键全部提交」 | 提交任务组（含纯学习组） | 干跑，`confirm=true` 才真交 |
| CLI | `action unipus duration seconds=600 confirm=true` | 同上 |

## 2. 实验：三条链路，逐条验证

目标课程：`20001188271`（三 B班 · 视听说教程2），门户进度 **0/33**，
但目录里 261 个任务组**全部**有首交时间（跨班教材实例串号，规格 §6.3 讲过）。

### 2.1 实验 A：提交一个「没全对」的组

- 组：`Exercise 1`（`6f917c7d1082d9d`），提交前 `score=[1,1,1,1,1,0,0,0,0,0]`；
- 提交：`code=0`、`score_pct=1.0`、`server_finish=true`（服务端接受）；
- **回读 `progress/v2`：`score` 仍然是 `[1,1,1,1,1,0,0,0,0,0]`，一个字节都没变**；
- 门户进度 0/33 不变（即时 + 15 秒后各查一次）。

→ **平台留存策略是「一组只留首条记录」**（`flowStrategy.record_every_submit=false`）。
**重交不会改分**，所以「批量重交已做过的组」是白跑，还可能白白触发风控。

### 2.2 实验 B：挂 180 秒学习时长（带 kick）

- `wss://ucontent.unipus.cn/unipusiopoint/?EIO=3&transport=websocket&uuid=<open_id>&token=<annotator>`
- 结果：连接成功、入房成功、kick 被接受（服务端回 `43/userActivities,1[{"code":0,"msg":"ok: 首次进入房间"}]`）、
  8 次 ping / 8 次 pong；
- **`credited_seconds = 0`**，全程**没有收到任何 `r_set` 记账广播**；
- 门户进度、单元 `state.pass` 全部不变。

→ 3 分钟尺度上，时长链路**没有任何记账证据**。是否阈值更长（例如按 5 分钟一记）、
或 `module/moduleGroup` 取值不对，仍未验证。

### 2.3 实验 C：门户进度是按班「必修任务」算的

`POST /api/tla/courseStudyStrategy/detail`（带宿主四头）给出按班策略：

| 单元 | passScore | requiredTask |
| --- | --- | --- |
| Get your foot in the door / Strength from within / Sports for all | 0 | **0**（不要求） |
| Horns of a dilemma / Time off / Science empowers | 60 | **11 / 11 / 11** |

合计 **33 个必修任务** —— 正好等于门户 `totalPointNum=33`。
所以 `finishPointNum/totalPointNum` 是**「通过（≥passScore）的必修任务数 / 必修任务总数」**，
不是计分点之和，也不是「做过多少组」。

这 33 个必修组里，19 个已全对或无数值，14 个没全对（含主观题组大面积 0 分）；
但门户仍是 0/33 —— 说明**门户口径与 ucontent 的组级记录不是同一套账**，
仅靠提交/挂时长都没有推动它。

## 3. 对产品的结论

1. 「一键全部提交」现在默认**跳过所有已完成组**（`skip_done=true`）：
   已完成但没全对的也跳过，日志会写明「平台留存首条记录，重交不改分」。
   想重交必须显式 `skip_done=false`（基本属于无效操作，仅供排查）。
2. 「刷课」若要做成真能涨进度的功能，还缺一次**更长的时长实验**（≥6 分钟，看是否出现
   `r_set` 记账帧）以及**必修口径对齐**（按班 33 个必修组，而不是全 261 组）。
3. 在上面的证据补齐之前，不要对外宣称「能刷课」。


---

## 4. 落地：控制台「刷课」页（2026-10-20）

### 4.1 界面（已合并进作业页，2026-10-20）

**不另开页面**：作业页本身按教材分组。

1. **先只显示「书」**：每本书一行 —— 书名 · 任务数 · **完成进度（如 18/18）+ 进度条**。
   进度来自门户 `finishPointNum/totalPointNum`（`actions/courses` 的「完成进度」列）；
2. **点开一本书**才是详情：任务列表（原表格，每行「详情 / 提交」），
   书名右侧的「刷课」打开该书的面板：进度（0/33）、任务组数（261）、必修数（33）、
   刷课时长 / 是否提交未完成 / 是否只处理必修；
3. 下面是**单元 → 任务**树（任务名 / 必修 / 状态 / 得分 / 分组号 / 提交）；
   每一行的「提交」与该平台作业页的提交走**同一条 API**
   （`POST /api/assignments/submit`，两步：干跑 → 确认）；
4. 「① 先干跑」出计划与日志，「② 确认真跑」才提交/建连接。

`PERSONAL`（非班级教材）这类独立项：教材列表里它们**单独成行**（不挂在班级下），
因为它们没有班级进度，点进去同样能看到单元与任务。

### 4.2 后端接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/platforms/unipus/course-tree?course=<课程号>&state=required` | 教材树（只读）。`state` 可选 `required`（默认，只查必修组状态，约 5 秒）/ `all`（261 组，几十秒）/ `none` |
| POST | `/api/platforms/unipus/brush` | 刷课后台任务（默认干跑） |

`brush` 请求体：

```json
{ "course": "20001188271", "minutes": 10, "confirm": false,
  "submit_pending": false, "only_required": true, "interval": 8 }
```

### 4.3 刷时长的机制（照参考实现的真实任务模式）

- 端点 `wss://ucontent.unipus.cn/unipusio/`（与房间池的 `unipusiopoint` 不同）；
- 入房 `40/userActivities?uuid=<open_id>&token=<annotator>,`；
- 每 5 秒发 `42/userActivities,0["start",{module:<分组ID>, moduleGroup:<实例ID>,
  client:"U校园pc", url:<真实任务页URL>, tag1:<单元>, tag2:<micro>,
  tag3:'{"microBlock":"<单元>/<micro>","version":"1","source":"ucontent"}'}]`；
- 轮换前补 `42/userActivities,2["stop",{…,"timer":<ms>}]`；
- 任务页 URL = `https://ucontent.unipus.cn/_explorationpc_default/pc.html?cid=<班级ID>&cloudCurriculaId=<班级课程ID>&source=cloud&courseResourceId=<班级教材ID>#/<实例>/courseware/<单元>/<micro>/<分组>`
  —— 服务端**靠这个 URL 认任务归属**，编造的 URL 只会落进通用桶。

### 4.3.1 2026-10-20 真跑一次（75 秒）的新证据

修掉一个自伤 bug（路径断言只认 `/unipusiopoint/`，任务模式的 `/unipusio/`
被自己拦成「学习时长路径不符」，一次 start 都没发出去）之后真跑：

```
connected: True | joined: True | start: 8 | stop: 1 | 记账: 0.0 | error: 无
收到帧:
  0{"sid":"…","pingInterval":25000,"pingTimeout":20000}
  40
  40/userActivities,
  43/userActivities,0[{"code":0,"msg":"ok","concurrent":1}]        ← 第一次 start 被接受
  43/userActivities,0[{"code":0,"msg":"ok: task没有信息变更"}]      ← 重复 start 的回应
```

结论：**帧已经正确发到服务端并被接受**（`code:0`、`concurrent:1`），
但 75 秒内仍**没有 `r_set` 记账广播**。下一步要确认的是「为什么服务端不记账」：
`url` 里的 `cloudCurriculaId` 是否应该是别的东西、是否要求任务页真的被打开过。

### 4.4 仍未验证 / 已知限制

- **记账仍未验证**：本机三次尝试（房间池 180s、单目标任务模式 150s、页面前端抓帧失败）
  都是 `credited_seconds=0`。参考实现声称任务模式「当场落账 1×」，差异原因未查明
  （怀疑 page URL 的 `cloudCurriculaId` 与班级实际不符，或账号当次不满足记账条件）。
- 需要一次**真实浏览器抓包**（任务页的 WS 帧）来对齐：抓法见 §5。
- 本书 33 个必修组**全部已做过**，且平台留存首条记录 → 「提交未完成」当前为 0 个；
  刷课对这本书只能挂时长（未验证）。

## 5. 下一次该怎么取证（留给后来人）

1. 用 Chrome 登录 U校园 → 进入任意一门课的教程学习页（`pc.html?...`）；
2. F12 → Network → WS → 找到 `unipusio`/`unipusiopoint` 连接 → Messages；
3. 复制 20 秒内的 `start`/`kick` 帧与服务端 `r_set` 帧（脱敏 token）；
4. 拿这些帧对着 `duration.py` 的 `build_task_start_frame` 逐字段比对，
   差异处就是记账失败的原因。


---

## 6. 未激活教材（2026-10-20 真跑暴露）

### 6.1 现象

对「三 B班 · 综合教程3」真交 55 个必修任务，前 6 个全部：

```
UnipusApiError: [ucontent] 业务失败（code=100002）…/newExploration/submit: 没有权限
```

随后全变成 `code=600001/600002 操作过于频繁`（我们连打触发限流）。

### 6.2 原因：这本书**不在书架里**（= 未激活）

| 课程 | 在书架 | activation | 结果 |
| --- | --- | --- | --- |
| 视听说教程2（三B） | ✅ CLASS_COURSE | 0（=已激活） | 可提交 |
| 综合教程2（三B） | ✅ CLASS_COURSE | 0 | 可提交 |
| **视听说教程3（三B）** | ❌ 不在任何分组 | — | **100002** |
| **综合教程3（三B）** | ❌ 不在任何分组 | — | **100002** |

书架（`/api/cmgt/course/my/bookshelf`）只有 4 本 CLASS_COURSE + 2 本 PERSONAL，
教程3 的两本根本不在其中 —— 学生还没输入激活码。未激活的教材，平台对提交一律
返回 `100002 没有权限`（这是权限，不是限流，重试无用）。

### 6.3 现在的处理

- `list_courses()` / 教材树新增 `in_bookshelf` 与 `activated`
  （不在书架 ⇒ `activated=False`；没查书架时保持 `None` = 未知）；
- 控制台作业页每本书名后显示徽标：**已激活 / 未激活**；
- 未激活的书：展开后有醒目提示，行内「提交」按钮**禁用**（title 写明 100002），
  「详情」仍可用；刷课面板的「提交未完成」按钮同样禁用，「刷时长」不受影响；
- 服务层：连续 2 个 `100002` 立即停止本轮提交（不再空打）；
- 限流（600001/600002）立即停止本轮，并提示「建议等 ≥180 秒」；
- 组间间隔默认 20 秒（下限 5 秒）。

### 6.4 需要用户做的事

未激活的教材只能在 U校园网页端**输入激活码**激活（平台侧权限，代码绕不过）。
激活后再刷新控制台，徽标会变成「已激活」，提交才会成功。


---

## 7. 题型与作答方式（2026-10-20，按真实目录统计）

参考实现里 U校园的题型不是"一套题一套接口"，而是**同一条提交 API + 不同的载荷形状**；
每类题在目录节点 `base` 里有明确标识（复合串如 `single-choice,short_answer`）。
本项目已把 `base`/`tab_type` 翻译成中文类型，显示在教材树的「类型」列。

| 类型 | 目录里的 `base` | 服务端有标准答案？ | 我们怎么交 |
| --- | --- | --- | --- |
| 客观题（听力/阅读/词汇） | `single-choice`、`multichoice`、`sequence`、`vocabulary`、`material-banked-cloze` | ✅ | 转发标准答案（已验证 `code=0`） |
| 阅读/内容、学习内容 | `basic-scoop-content`、`rich-text-read`、`text-learn` | —（无判分项） | 按 `submitType=2` 提交记进度 |
| 视频/听力学习 | `video-point-read`、`audio-learn` | —（学习行为） | 同上；时长另走 WS（见 §4） |
| **翻译** | `translation`、`short_answer` | ❌ | 需要自拟文本；可用「占位文本」选项（会得完成分，内容非人所写），或后续接大模型 |
| **写作** | `writing` | ❌ | 同上 |
| 讨论 | `discussion` | ❌ | 同上 |
| **单元测验** | `exit-ticket`（`tab_type=check`） | ✅（测验有标准答案） | 与客观题同一条提交 API |
| 口语 | `role-play`、`oral*`、`dialog-follow`、`ai-spoken` | ❌（客户端自报 record） | `self_report_scores=True` 才会带满分 record；默认关（合规决定） |
| 上传作业 | `multiFileUpload` | ❌ | 参考实现自述未接入，本项目同样不做 |

统计样例（综合教程2 的 325 个任务组）：词汇 47、阅读/内容 40、简答/翻译 39、
视频学习 36、客观题 32、选词填空 23、翻译 22、学习内容 21、口语（情景）10、
排序 8、多选 7、上传 6、写作 2、单元测验走 `check` 分组。

### 7.1 已实现 / 未实现

- 已实现：类型识别（树里 `type` / `type_label` / `needs_answer` / `is_study`）、
  客观题与测验的标准答案转发、学习类 `submitType=2`、占位文本选项
  （`subjective_placeholder`，默认空 = 不发占位）。
- 未实现（需要用户决策）：翻译/写作**自动生成内容**（可接本项目已有的大模型，
  但要先确认"AI 代写"是否可接受）、口语自报满分、上传作业。
