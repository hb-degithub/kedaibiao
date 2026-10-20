# 希沃品课「刷课」链路验证报告（2026-10-20）

- 验证者：Lead（本轮实现者）
- 账号：控制台绑定的品课账号 #9（会话快照在 `data/sessions/accounts/`，**不入库**）
- 环境：本机 Python 3.14.4 / Chromium 1248（Playwright）
- 范围：只做学生端、只碰自己的账号；**没有任何教师端接口调用**
- 结论一句话：**刷课机制已确认并端到端跑通** —— 进度按「看过的最大页」算，
  逐页 `PostV1CoursewareStudentMaxLook` 即可推满；实测把一份 10 页 PPT
  从 50% 推到 100%。此前试的「学习时长上报」接口会收下请求但**不涨进度**。

---

## 0. 结论表

| 结论 | 证据等级 | 详见 |
| --- | --- | --- |
| 材料进度 = `(maxPageIndex + 1) / 总页数` | 【实测】 | §2 |
| 打开课件：`{"resId": <media.resId>, "coursewareId": <materialId>}` → `pages[]` + `maxPageIndex` | 【实测】 | §2.1 |
| 翻页上报：`{"coursewareId": <materialId>, "maxPageIndex": N}` 推一页 | 【实测】 | §2.2 |
| PPT（`media.type=4`）与 PDF（`type=8`）都适用（PDF 实测 45 / 387 页） | 【实测】 | §2.3 |
| 端到端：50% → 100%，5 次翻页，材料列表 `learningRatio` 同步变化 | 【实测】 | §2.4 |
| 学习时长上报 `…StudentLearningDurationReport` 收下请求但**不涨进度** | 【实测·阴性】 | §3 |
| `countLearningProgress` 只是 H5 页面「要不要发时长心跳」的开关，与进度算法无关 | 【bundle + 实测】 | §3 |

---

## 1. 怎么发现的

用户反馈：**在手机 App 里打开资料、滑动 PPT，百分比会跟着涨**。据此重新翻
学生端 bundle，发现 H5 材料页里只有「学习时长上报」，没有翻页逻辑 —— 说明
真正记进度的是 App 的课件（courseware）链路。品课把材料当成「课件」：
`coursewareId` 就是材料列表里的 `materialId`，`resId` 是 `media.resId`。

## 2. 实测记录

所有请求都走同一份浏览器会话（`student/api.json` + `x-server: main` +
`x-csrf-token`），只对自己账号的一门课、少量材料动手。

### 2.1 打开课件拿到页数与当前页

```
POST ?actionName=PostV1CoursewareStudentOpen
{"resId": "dc8a…8810", "coursewareId": "<materialId>"}
→ {"maxPageIndex": 3, "pages": [ … 共 10 项 … ]}
```

此时材料详情 `learningRatio = 40`：`(3+1)/10 = 40%`，对得上。
`pages[]` 每项带 `pageIndex`（1 起）、`coursewarePageId`、缩略图、下载地址。

### 2.2 翻一页，进度立刻涨

```
POST ?actionName=PostV1CoursewareStudentMaxLook
{"coursewareId": "<materialId>", "maxPageIndex": 4}
→ {}

再打开课件：maxPageIndex=4；材料详情 learningRatio = 50
```

所以「翻页」就是 `maxPageIndex` 递增；服务端立即记账，不用等批处理。

### 2.3 页数分布

本账号「电子技术基础」15 份材料里：

| 类型 | `media.type` | 页数 |
| --- | --- | --- |
| PPTX | 4 | 10 页（第一份实测） |
| PDF | 8 | 45 页 / 387 页（教材电子书） |

两类都能用同一套「打开 → 翻页」推进。

### 2.4 端到端跑通

```
$ scripts/pinco_progress.py --course <courseId> --material <materialId> --live
开始推进：1 份材料，目标 100%；每页间隔 1s，上限 30s / 400 页。
  翻到第 6/10 页，进度 —
  …
  翻到第 10/10 页，进度 100%
  材料 <materialId>：进度 50% → 100%（+50.00%），翻页 5 次（共 10 页），
  停止原因 target_reached。
全部到目标。
```

### 2.5 整门课跑通（15/15 到 100%）

```bash
scripts/pinco_progress.py --course <courseId> --all --live
```

结果：课程「电子技术基础」**15 份材料全部 100%**（13 份 PPT + 2 份 PDF，
页数 9~387 页不等，合计约 900 次翻页）。跑完再列材料确认所有
`learningRatio` 都是 100。

过程中遇到 1 次网关 **502 Bad Gateway**（单条翻页请求），脚本按设计只跳过该份、
继续后面的材料，并在 stderr 汇总「1 份没到目标」；重跑该份即补齐（90% → 100%）。
这属于传输层抖动，不是机制问题 —— 但说明批量跑要允许重试。

## 3. 阴性结果：学习时长上报不涨进度

官方 H5 材料页里确有（`countLearningProgress === true` 时每 5 秒一次）：

```
POST ?actionName=PostV1CourseByCourseIdMaterialStudentLearningDurationReport
{"courseId": …, "materialId": …, "learningDuration": 5}
```

真实会话调用返回 `{"statusCode":0,"message":"ok"}`，但：

- 12 次 × 5 秒（60 秒）后，`learningRatio` 仍是 0、`viewed` 仍是 `false`；
- 一次性 300 / 1200 秒也一样；
- 浏览器页内用 `fetch` 发、补 `x-app-code` / `x-token` 请求头，仍不涨；
- 延迟 3 分钟再读，仍不涨。

结论：**「接口收下」≠「进度累加」**。`countLearningProgress` 只是 H5 页面
「要不要发心跳」的开关，真正记进度的是课件翻页（`maxLook`）。这条时长上报
常量仍留在 `endpoints.py` 里作为阴性证据，不再用于刷课。

（历史假设：页面会话门槛 / App 上下文 / 服务端批处理 —— 现已被上面的
`maxLook` 实测排除，不需要再去验证。）

## 4. 实现

| 落点 | 内容 |
| --- | --- |
| `platforms/pinco/endpoints.py` | 登记 `ACTION_COURSEWARE_OPEN` / `ACTION_COURSEWARE_MAX_LOOK` 与 `maxPageIndex`/`pages`/`resId` 字段；两者都进 `DANGEROUS_ACTIONS` |
| `platforms/pinco/progress.py` | `CoursewareProgressReporter`：按页推进，**默认 dry-run**；带页数/总时长上限与「连续几次回读不涨就停」 |
| `platforms/pinco/adapter.py` | `open_courseware()`（页数 + 当前页）与 `advance_learning(dry_run=True, …)` |
| `scripts/pinco_progress.py` | CLI：不带 `--live` 只列材料/算计划；`--material` 单份；`--all` 全部 |
| `api/actions.py` | 控制台动作 `progress`（`mutates=True`）：默认干跑，`confirm=true` 才写；`material_id` 留空处理未达标材料，最多 `max_materials` 份 |
| `tests/test_pinco_progress.py` | 19 个离线用例：策略校验、dry-run 零请求、逐页报文、达标/页数/时长/停滞熔断、目标比例换算、从当前页续推、适配器接线 |

安全默认值：

- `dry_run=True`，且 dry-run **连课件都不打开**（打开也是写接口）；
- 翻页间隔默认 1 秒、不得低于 0.2 秒（比滑动更密没有依据）；
- 每份材料默认最多 900 秒 / 400 页；
- 连续 2 次回读进度不涨即停（`ratio_stalled`）；
- 日志只有材料号、页码、百分比，不含 cookie / 签名下载地址。

## 5. 复现命令

```bash
# 只列材料与进度（只读）
PYTHONPATH=src .venv/bin/python scripts/pinco_progress.py --course <courseId>

# dry-run：只读、不打开课件、不发翻页
PYTHONPATH=src .venv/bin/python scripts/pinco_progress.py --course <courseId> --material <materialId>

# 真推进单份（小步、带熔断）
PYTHONPATH=src .venv/bin/python scripts/pinco_progress.py --course <courseId> \
    --material <materialId> --live --max-seconds 60

# 全部材料推到 100%
PYTHONPATH=src .venv/bin/python scripts/pinco_progress.py --course <courseId> --all --live
```

返回码：`0` 成功；`2` 会话不可用；`3` 找不到课程/材料；`4` 有材料没到目标
（会在 stderr 列出材料号）。
