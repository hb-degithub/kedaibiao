# U校园｜控制台接入实测（2026-10-20）

> 本文记录「U校园是否真的接进 Web 控制台」的实测证据。
> 结论：**已接入**。平台列表、课程/作业页、工具页动作、单作业提交（干跑 + 确认）
> 全部在真实浏览器 UI 里跑过；截图在本地 `data/console-selftest/unipus/`
> （`data/` 已 gitignore，**不入库**）。

---

## 1. 验证方式

- 控制台：`PYTHONPATH=src python3 -m kedaibiao web --port 8766`（回环地址）
- 浏览器：Playwright + Chromium，无头模式
- 身份：**临时**为本地 `admin` 建了一条会话 token 进控制台（没有改密码、
  没有新建账号）；验完立刻删除该 session（`sessions` 表现在为 0）
- 账号：控制台里已绑定的 `unipus` 账号（cookie 会话复用，不重新登录）

---

## 2. 实测结果

| # | 检查项 | 结果 |
| --- | --- | --- |
| 1 | 平台列表 | 左侧出现「U校园（Unipus）」，带 **可提交** 徽标（`capabilities.submit = true`） |
| 2 | 概览 / 课程 / 作业页 | 作业页真实加载 **2908 项**「全部课程」任务；课程名、任务号、动作 `assignments`、`kind: table` 正常 |
| 3 | 工具页动作清单 | 列出 **题目与标准答案（answers）**、**提交任务组（写操作）（submit）**、**学习时长上报（写操作）（duration）**；通用 `courses/assignments/detail` 仍走专属页 |
| 4 | 工具页执行动作 | 填任务号 → 执行 `answers`，结果按 `kind: detail` 渲染（含「标准答案」「计分点」表头） |
| 5 | 作业页「提交」 | 点开后是**两步提交**：干跑计划渲染成表格（题号 / 题目 / 计划答案 / 置信度 / 标记），底部有「确认真提交（不可撤销）」；顶部明确写「这是干跑（confirm=false）的结果」 |
| 6 | 作业详情（双击行） | 抽屉展示任务号 / 名称 / 课程 / 题目表 / 平台原始数据 |
| 7 | 控制台后端链路 | `ConsoleService.submit_assignment` 走 `native_submit_flow`：`confirm=false` → `native_plan`（只读），`confirm=true` → `native_submit`；不会经过通用 answerer |

截图（本地，不入库）：

```
data/console-selftest/unipus/01-overview.png
data/console-selftest/unipus/02-platform-unipus.png
data/console-selftest/unipus/03-tools.png
data/console-selftest/unipus/04-assignments.png
data/console-selftest/unipus/05-submit-dryrun.png
data/console-selftest/unipus/06-detail.png
data/console-selftest/unipus/08-tools-answers.png
```

---

## 3. 本次顺手修掉的一个 UI 问题

第一次点「提交」时，抽屉只能看到「**这个响应没有可渲染的结构（kind=—）**」+
原始 JSON —— 因为 `native_plan` 只返回机器字段，而控制台的通用抽屉按
「展示信封」（`kind: detail` + `fields` + `sections`）渲染。

修法（`platforms/unipus/adapter.py`）：

- `native_plan` / `native_submit` 在**保留全部机器字段**（`can_submit` / `note` /
  `questions` / `ok` / `code` / `readback`…）的前提下，额外挂一份展示信封
  （`kind` / `title` / `fields` / `sections`）；
- 流水线与 CLI 动作仍读机器字段，行为不变；
- 修完后同一个抽屉变成表格化计划，且「展开后端原始响应（kind: detail）」可对照。

---

## 4. 边界

- 控制台里**没有点过**「确认真提交」按钮（真提交已在 CLI 上验证过一组，
  见 [unipus-submit.md](unipus-submit.md) §10；UI 的真交按钮与 CLI 走同一个
  `native_submit`，只差一次点击）。
- `duration` 动作只在 CLI 做过真实连接验证；控制台里只列了动作、没有点执行
  （它默认干跑，点执行不会连平台）。
- 语音评测没有控制台入口（报文形状大部分未实测，见
  [unipus-speech.md](unipus-speech.md)）。
- 验证用的临时 admin 会话已删除，服务已关闭。

---

## 5. 「一键全部提交」实测（2026-10-20 新增）

### 5.1 做了什么

- 后端：`POST /api/platforms/<name>/batch-submit`（202 + job），后台线程逐组
  「读 `progress/v2` → native_plan →（确认后）native_submit → 回读」，
  进度写 `job.logs` 并同步到平台日志；
- 前端：作业页顶部「一键全部提交」按钮（仅 U校园显示）→ 面板（最多处理数 /
  组间间隔 / 跳过已满分）→ **① 先干跑** → 看完清单再 **② 确认全部真交**；
  日志 1.2s 轮询滚动，切页不中断；
- 安全默认：干跑不写平台；真交必须二次确认；已满分（`score_pct >= 1`）默认跳过；
  组间默认停 8 秒防限流；撞限流会按 `retry_after` 等待后继续。

### 5.2 实测结果

| 项 | 结果 |
| --- | --- |
| 服务层批量干跑（课程 `20000943839`，limit=3） | `done`；可提交 2 / 跳过 1（已满分）/ 被拦 0；**零 POST** |
| HTTP 路由（limit=2） | 202 返回 `b-1`；轮询 `GET /api/jobs/b-1` → `done`，日志完整 |
| 浏览器 UI（Playwright，真实 8765） | 按钮可见 → 面板打开 → 干跑完成 → 计数「可提交 2 · 已提交 0 · …」→「② 确认全部真交」自动出现 |
| 离线测试 | `tests/test_api_service.py::TestBatchSubmit`（干跑零提交、真交只交可提交项、limit、课程不匹配、501/404）+ `native_assignment_state` 用例；全量 1514 条通过 |

截图：`data/console-selftest/unipus/10-batch-dryrun.png`（本地，不入库）。

### 5.3 边界

- **批量真交没有在真实账号上跑过**（只跑了批量干跑）：单组真交已在
  [unipus-submit.md](unipus-submit.md) §10 验证；批量真交与它共用
  `native_submit`，差异只在循环、节流与计数。
- 口语/角色扮演/上传类任务可能因「客户端自报 record」策略被平台拒绝：
  批量任务会把它记成 `failed` 并继续下一组，不会拖垮整批。
- 批量提交会跳过已满分，但**做过但 0 分**的组会被重交（转发标准答案，重交只会更好）。


---

## 6. 「已满分」到底怎么判（2026-10-20 修正）

### 6.1 发现的坑：平台的 `score_pct` 不是正确率

批量提交最初用 `progress/v2` 的 `data.state.score_pct >= 1` 判「满分」，实测是**错的**。
对视听说教程2 的 **261 个任务组**逐组对比「平台 `score_pct`」与「逐项得分
`state.score` 的真实得分率」：

| `score_pct` | 逐项真实得分率 | 组数 |
| --- | --- | --- |
| 1 | 1.0（全对） | 144 |
| **1** | **0.5 / 0.78 / 0.25 / 0.22（没全对）** | **15** |
| 0 | 0.0 | 24 |

典型样本（`groupId=6f917c7d1082d9d`）：

```json
{"score": "[1,1,1,1,1,0,0,0,0,0]", "score_avg": 100, "score_pct": 1, "state": 1}
```

十项只对五项，平台却回 `score_pct=1`、`score_avg=100`。所以 `score_pct` 更像是
「是否达到某个平台内部门槛 / 是否完成」，**不能当正确率**。

### 6.2 现在的判定规则

只看 `state.score`（逐项 0/1）：

| 情况 | 判定 | 日志 |
| --- | --- | --- |
| 有计分点且**逐项全 1** | 跳过 | `跳过 X（已满分（10/10 项全对））` |
| 有计分点、但没全对 | **重交** | `X 做过但未全对（5/10 项），重交` |
| 无计分点（纯学习/内容组）且 `first_pass` 有值 | 跳过 | `跳过 X（已完成（无计分点：纯学习/内容组，平台已记过账））` |
| 无计分点且没做过 | 提交 | `✓ 可提交 X（…）` |

`UnipusAdapter.native_assignment_state` 现在返回 `total_items` / `correct_items` /
`full_marks` 三个**可信**字段（`score_pct` 仍保留，只作参考）。

### 6.3 干跑与真交的关系

干跑与真交是**两次独立请求**，但**不需要重复干跑**：

- ① 干跑：只读内容 + 标准答案，列出「可提交 / 跳过 / 被拦」，不写平台；
- ② 确认全部真交：用**同样的参数**重新读一遍内容与标准答案，逐组提交并回读进度。

看完 ① 的清单后**直接点 ②** 即可；① 的按钮会变成「重新干跑（参数改过才需要）」。
