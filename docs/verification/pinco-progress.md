# 希沃品课「刷课」链路验证报告（2026-10-20）

- 验证者：Lead（本轮实现者）
- 账号：控制台绑定的品课账号 #9（会话文件在 `data/sessions/accounts/`，**不入库**）
- 环境：本机 Python 3.14.4 / Chromium 1248（Playwright）
- 范围：只做学生端、只碰自己的账号；**没有任何教师端接口调用**
- 结论一句话：**上报接口与报文已确认，但「进度会不会真的累加」没有确认** ——
  实现按「默认 dry-run + 进度不动就停」落地，不把未验证的语义包装成成功。

---

## 0. 结论表

| 结论 | 证据等级 | 详见 |
| --- | --- | --- |
| 材料详情 `countLearningProgress === true` 时，学生端页面每 5 秒上报一次学习时长 | 【bundle 调用点】 | §1 |
| 上报 action：`PostV1CourseByCourseIdMaterialStudentLearningDurationReport`，报文 `{courseId, materialId, learningDuration: 5}` | 【bundle + 实测被接受】 | §1 |
| `materialId` 必须是材料列表里的 `materialId`（= `chapterDirectoryMaterialId`）；传资源 id 会被 9105 拒绝 | 【实测】 | §2.4 |
| 真实会话直接调用返回 `{"statusCode":0,"message":"ok"}` | 【实测】 | §2.1 |
| **上报后 `learningRatio` / `viewed` 没有变化**（60s、1200s、浏览器页内发、加 App 头都试过） | 【实测·阴性】 | §2.2–§2.5 |
| `ReportViewed` / `ReportStudied` 收下请求但不改变 `viewed`；`CountLearningProgress` 是教师端（6571） | 【实测·阴性】 | §2.3 |
| 进度是否即时累加、是否需要「真实学习会话」/服务端批处理 | **未验证** | §3 |

---

## 1. 官方 bundle 里的调用点【bundle】

学生端材料页 chunk（`material.js`）里，详情返回后：

1. 只有当 `data.countLearningProgress` 为真，才启动定时器；
2. 定时器间隔 **5000 ms**；
3. 每次发 `PostV1CourseByCourseIdMaterialStudentLearningDurationReport`，报文
   `{"courseId": <query 里的 courseId>, "materialId": <query 里的 chapterDirectoryMaterialId>, "learningDuration": 5}`。

这解释了为什么「每 5 秒、每次 5 秒」—— 不是我们猜的节奏，是页面写死的。

## 2. 实测记录

所有请求都走同一份浏览器会话（`student/api.json`，`x-server: main` + `x-csrf-token`），
只对 1 门课的 1~2 份材料动手，总量受控。

### 2.1 接口接受请求

12 次、间隔 5.1 秒、`learningDuration: 5`（模拟页面 60 秒）：

```
tick 0:  http=200  {"statusCode": 0, "message": "ok"}
tick 11: http=200  {"statusCode": 0, "message": "ok"}
after:   viewed=False  ratio=0
```

### 2.2 一次性大时长也不动

同一材料再发 `learningDuration: 300`、`1200`，仍 `statusCode 0`；
材料详情 `learningRatio` 仍为 0、`viewed` 仍为 `false`。
**注意**：这说明服务端要么忽略了请求，要么累计了但比例不是即时可见 —— 两者都还没区分开。

### 2.3 其他候选写接口的阴性结果

| 尝试 | 结果 |
| --- | --- |
| `PostV1CourseByCourseIdMaterialStudentReportViewed {courseId, materialId}` | `{}` 成功信封，但 `viewed` 仍 `false`、比例不变 |
| `PostV1CourseByCourseIdMaterialStudentReportStudied {courseId, materialId}` | 同上 |
| `PostV1ChapterdirectoryMaterialCountLearningProgress {chapterDirectoryMaterialIds: [...]}` | `6571 没有该课程权限`（教师端接口，学生身份不可用） |
| `PostV1CoursewareStudentOpen {courseId, chapterDirectoryMaterialId, chapterDirectoryNodeId}` | `401：resId 不能为 null, coursewareId 不能为 null`（课件上下文，不是材料） |

### 2.4 `materialId` 的取值被证实

把资源 id（详情里的 `materialId` 字段）当作上报的 `materialId` 时：
`9105：章节目录资源关联不存在`。所以上报的 `materialId` 必须是**材料列表里的
`materialId`**（与详情请求的 `chapterDirectoryMaterialId` 同一个值）。

### 2.5 浏览器页内发也不动

- 学生工作台 `GET /student/workbench/student?auth=…` 能正常渲染并调
  `GetV1WorkbenchStudentApp`；
- 材料页 `GET /student/material/?auth=…&courseId=…&chapterDirectoryMaterialId=…`
  在 headless Chromium 里 **DOM 为空**（控制台唯一的报错是百度统计
  `hm.baidu.com` 连接被拒，与业务无关），所以无法从页面里观察它自己的上报；
- 退一步，在**已渲染的工作台页面上下文**里用 `fetch` 发同样的上报（带
  `x-app-code: university-student-mini-program`、`x-token`、`x-csrf-token`、
  `x-server`），仍 `statusCode 0`，进度不变。

### 2.6 延迟检查

上报后 3 分钟重新读列表/详情：`viewed=False`、`ratio` 不变（另一份已学 12% 的材料
也停在 12）。所以「马上看不到、等一会就有」这个解释在 3 分钟尺度上不成立。

---

## 3. 未确认的部分（写清楚，别包装）

1. **累计语义**：时长是累加、覆盖还是要配合别的接口，未知；
2. **门槛**：是否需要页面/App 的「学习会话」（例如先被某接口标记 viewed）或服务端
   批处理，未知；
3. **是否有比例阈值**：材料 22 MB、`learningRatio` 是整数百分比，60~1200 秒都不动，
   无法排除「必须跨过某个阈值才显示」。

下一步最省事的验证：**由用户在自己的浏览器/App 里打开一份材料 30~60 秒**，
看进度是否变化；若变化，再抓那一刻的完整请求序列（含页面加载时是否先发了
`ReportViewed` 之类的请求）。这比继续盲试接口有用。

---

## 4. 本轮实现（以及为什么这样实现）

| 落点 | 内容 |
| --- | --- |
| `platforms/pinco/endpoints.py` | 登记 action / 字段常量，`EVIDENCE=MEASURED` 只针对「接口形态被实测接受」；注释里明确写「进度累加未确认」 |
| `platforms/pinco/progress.py` | `LearningReporter`：按 5 秒节奏发上报，**默认 dry-run**；`max_total_seconds` / `max_ticks` / 连续 N 次刷新不涨就停 |
| `platforms/pinco/adapter.py` | `report_learning(course, material_id, *, dry_run=True, …)`：只发学习时长上报，不碰提交 |
| `platforms/pinco/client.py` | 进度上报加入 `DANGEROUS_ACTIONS`：任何调用都必须显式 `dangerous=True` |
| `scripts/pinco_progress.py` | CLI：不带 `--live` 只列材料/算计划；`--live` 才上报，停下时打印「进度没动」 |
| `tests/test_pinco_progress.py` | 15 个离线用例：节奏下限、dry-run 不发请求、达标即停、进度不动即停、两个上限、`countLearningProgress=false` 拒绝、适配器接线报文 |

安全默认值：

- `dry_run=True`（要真发必须显式关）；
- 间隔不得小于 5 秒（比页面更密没有依据）；
- 一次运行默认最多 1800 秒（CLI）/ 3600 秒（库）；
- 连续 3 次刷新进度不涨就停 —— 正因为「累计语义未确认」，不能让脚本刷几百次还
  以为在涨；
- 日志只记材料号与秒数，不记 cookie / 下载地址。

## 5. 复现命令（默认只读）

```bash
# 只列材料与进度（只读）
PYTHONPATH=src .venv/bin/python scripts/pinco_progress.py --course <courseId>

# 看某份材料的 dry-run 计划（不发请求）
PYTHONPATH=src .venv/bin/python scripts/pinco_progress.py --course <courseId> --material <materialId>

# 真上报（小步、带熔断；进度不动会自己停）
PYTHONPATH=src .venv/bin/python scripts/pinco_progress.py --course <courseId> \
    --material <materialId> --live --max-seconds 10 --refresh-every 2 --stall-checks 1
```

最后一档在本机实测的输出：2 次上报后 `ratio_stalled`，进度 0% → 0%，
返回码非 0（脚本用返回码告诉调用方「没刷上」）。
