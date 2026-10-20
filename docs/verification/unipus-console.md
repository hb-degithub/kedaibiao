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
