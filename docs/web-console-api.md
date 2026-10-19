# Web 控制台接口契约

> 后端（`api/service.py` + `api/server.py`）与前端（`api/static/`）之间的唯一约定。
> 前端**只**依赖本文档；后端实现变更必须同步改这里。

## 0. 为什么是标准库，不是 FastAPI

本机 `python3` 没有 pip，`python3 -m venv` 因缺 `ensurepip` 而失败，装不上
FastAPI / uvicorn。于是控制台用标准库 `http.server` 实现。

这个取舍本身也站得住：

- 本项目核心刻意零第三方依赖，控制台是本地单人用，不需要 Web 框架；
- **服务层（`service.py`）与 HTTP 层（`server.py`）是分开的** —— 将来要换
  FastAPI，只换外壳，`service.py` 一行不动。

`pyproject.toml` 里的 `api = ["fastapi", "uvicorn"]` 保留，作为将来的可选路径。

## 1. 通用约定

| 项 | 约定 |
| --- | --- |
| 基址 | `http://127.0.0.1:<port>`，**只绑定回环地址**（见第 5 节安全） |
| 编码 | 一律 UTF-8，`Content-Type: application/json; charset=utf-8` |
| 成功 | HTTP **2xx**（读接口 200，`POST /api/run` 与动作调用返回 **202** 或 200）+ JSON 对象 |
| 失败 | HTTP 4xx/5xx + `{"error": "<中文说明>", "kind": "<机器可读类型>"}` |
| 时间 | ISO 8601 字符串（`2026-10-19T11:20:00`） |
| 得分/准确率 | 可能为 `null`，前端要能显示 `—` |

前端判定成功请用 `2xx`，不要写死 `=== 200`。

### 平台过滤参数

所有「历史/统计」类读接口都接受可选的 `?platform=<机器名>`：

- **省略** = 全部平台（聚合视图）；
- `<机器名>` = 只看该平台；
- 传了不存在的平台名 → **404 + `kind: "unknown_platform"`**（不要静默当成空结果）。

响应里一律回带 `"platform": "<值或 null>"`，前端据此确认当前作用域，
不要靠自己的状态猜。

**哪些接口接受 `?platform=`：**

| 接口 | 接受 | 说明 |
| --- | --- | --- |
| `GET /api/overview` | ✅ | 题库与跑批计数都按平台过滤 |
| `GET /api/runs` | ✅ | |
| `GET /api/submissions` | ✅ | |
| `GET /api/bank` | ✅ | 题库现在也有平台维度了 |
| `GET /api/jobs` | ✅ | 但**前端本地筛选也完全可以** —— 任务轮询不受平台切换影响更重要 |
| `GET /api/statistics` | ❌ | 该接口本身就是跨平台对比，返回全部平台的分组 |
| `GET /api/settings` | ❌ | 配置与凭据状态是全局的 |
| `GET /api/platforms` | ❌ | 本来就返回全部平台 |
| `GET /api/platforms/<name>/actions` | ❌ | 路径里已经带了平台 |

`limit` 一律钳制到 **1..500**，越界不报错、按边界取值。默认值各接口不同
（见各自小节）。

### 聚合视图约定

「全部平台」不是简单相加：跨平台的数据要能看出**来自哪个平台**，
所以列表类响应每行都必须带 `platform` 字段。

### 错误响应

```json
{ "error": "未注册的平台: 'ghost'", "kind": "unknown_platform" }
```

`kind` 全部取值：

| kind | HTTP | 含义 |
| --- | --- | --- |
| `bad_request` | 400 / 413 / 415 | 参数、请求体或 Content-Type 不对 |
| `unknown_platform` | 404 | 平台没注册 |
| `unknown_action` | 404 | 该平台没有这个只读动作 |
| `not_found` | 404 | 路由或对象不存在 |
| `no_credentials` | 400 | 该平台没配凭据（`error` 里会写明期望的环境变量键名） |
| `busy` | 409 | 已有跑批在跑，同一时刻只允许一个 |
| `forbidden` | 403 | Host 或 Origin 被安全策略拒绝 |
| `platform_error` | 502 | 平台侧失败（未登录、找不到内容、网络异常），另带 `detail` = 平台异常类名 |
| `internal` | 500 | 服务端自身异常 |

---

## 2. 接口

### `GET /api/overview?platform=<可选>` —— 概览卡片

`?platform=` 省略时为全量；给了就只统计该平台。

```json
{
  "version": "0.1.0",
  "python": "3.14.4",
  "repo_root": "/home/hb/刷题刷课",
  "platform": null,
  "env": "dev",
  "dry_run": true,
  "headless": true,
  "max_concurrency": 3,
  "min_confidence": 0.75,
  "db_path": "data/kedaibiao.db",
  "db_exists": true,
  "platform_count": 3,
  "credentialed_platforms": ["unipus"],
  "optional_deps": { "playwright": false, "fastapi": false, "uvicorn": false },
  "bank": { "total": 12, "graded": 5, "correct": 4, "accuracy": 0.8,
            "ungraded": 7, "by_source": {"bank_hit": 12},
            "by_platform": {"unipus": 12} },
  "runs": { "total": 40, "succeeded": 30, "failed": 4, "skipped": 6 }
}
```

> `accuracy` 可能为 `null`。
> **`runs` 的计数是全量的**（数据库中所有执行记录），不是「最近 N 次」——
> 前端照原样展示，不要加「最近」字样。
> `platform` 回带当前作用域：`null` = 全部平台。

### `GET /api/platforms` —— 平台清单

```json
{
  "platforms": [
    {
      "name": "unipus",
      "display_name": "U校园（Unipus）",
      "capabilities": {
        "login": true, "list_courses": true, "list_assignments": true,
        "fetch_questions": true, "submit": false, "course_playback": false
      },
      "has_credentials": false
    }
  ]
}
```

> `capabilities.submit === false` 时前端**必须**禁用「真提交」开关并说明原因 ——
> 该平台本阶段不具备提交能力。

### `GET /api/runs?limit=50` —— 跑批历史

```json
{
  "runs": [
    {
      "id": 41, "platform": "unipus", "assignment_id": "123/456",
      "status": "succeeded", "attempts": 1, "error": "",
      "detail": {}, "started_at": "2026-10-19T11:20:00",
      "finished_at": "2026-10-19T11:20:03"
    }
  ]
}
```

`status` ∈ `pending` / `running` / `succeeded` / `failed` / `skipped`。

### `GET /api/submissions?limit=50` —— 提交记录

```json
{ "submissions": [ { "id": 1, "platform": "unipus", "assignment_id": "123/456",
                     "ok": true, "score": 95.0, "message": "交卷成功",
                     "created_at": "2026-10-19T11:20:03" } ] }
```

### `GET /api/bank?platform=<可选>` —— 题库统计

**打平对象**（不是 `{"bank": {...}}`）：与 `overview.bank` 同结构，另加
`platform`（当前作用域，`null` = 全部）与 `db_path`。
**没有导出端点**，`db_path` 只是给人看数据库在哪。

```json
{ "platform": null,
  "total": 12, "graded": 5, "correct": 4, "accuracy": 0.8,
  "ungraded": 7, "by_source": {"bank_hit": 12},
  "by_platform": {"unipus": 12},
  "db_path": "data/kedaibiao.db" }
```

> `by_platform` **始终是全量的**（即使传了 `?platform=` 也一样），
> 这样「统计」页能拿它画各平台对比。

### `GET /api/statistics` —— 统计页（不接受 `?platform=`）

跨平台对比数据，本身就是要看全部平台。

```json
{
  "bank": { "total": 12, "graded": 5, "correct": 4, "accuracy": 0.8,
            "ungraded": 7, "by_source": {"bank_hit": 12},
            "by_platform": {"unipus": 12} },
  "platforms": [
    { "platform": "unipus", "total": 40, "succeeded": 30, "failed": 4,
      "skipped": 6, "submissions": 3, "submit_ok": 3,
      "accuracy": 0.8, "questions": 12 }
  ]
}
```

`platforms` 每行的 `total/succeeded/failed/skipped` 来自跑批记录，
`submissions/submit_ok` 来自提交记录，`questions` 是该平台在题库里的题数。
没有数据的平台也会出现（各项为 0），这样对比表不会缺行。

### `POST /api/run` —— 触发一次跑批

**请求**（必须 `Content-Type: application/json`）：

```json
{ "platform": "unipus", "live": false, "limit": 0, "kinds": ["homework"] }
```

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `platform` | string | 必填 | 平台机器名 |
| `live` | bool | `false` | **`false` = 干跑**（只规划不提交）。前端默认关闭，打开需二次确认 |
| `limit` | int | `0` | 0 = 不限；用于首次试跑限流 |
| `kinds` | string[] | `[]` | 空 = 全部；取值 `course` / `homework` / `exam` / `practice` |

**响应**（202 Accepted）：

```json
{ "job_id": "j-3", "platform": "unipus", "live": false,
  "status": "running", "started_at": "2026-10-19T11:20:00" }
```

同一时刻只允许一个跑批任务；已有任务在跑时返回 **409** + `kind: "busy"`。

### `GET /api/jobs` —— 任务列表（新的在前）

```json
{ "jobs": [ { "job_id": "j-3", "platform": "unipus", "live": false,
              "status": "running", "started_at": "...", "finished_at": null,
              "summary": "", "error": "",
              "records": [ { "assignment_id": "123/456", "status": "skipped",
                             "note": "dry_run=True，未提交" } ] } ] }
```

`job.status` ∈ `running` / `succeeded` / `failed`。（没有「撤销」功能，
所以不存在 `aborted`。）

⚠️ `job.status` 与 run 的 `status` 是**两套不同枚举**，不要互相映射：
run 是「一个作业的处理结果」，job 是「一次跑批的整体结果」。
按各自枚举上色即可。

### `GET /api/jobs/<job_id>` —— 单个任务

**裸 job 对象**（不是 `{"job": {...}}`），与 `/api/jobs` 里数组的单个元素同结构。
不存在返回 404 + `kind: "not_found"`。

### `GET /api/events?limit=100` —— 事件流

后端把 `EventBus` 上的事件记进环形缓冲，前端轮询它做「实时日志」。
**已实现**，无需降级处理。

```json
{ "events": [ { "at": "2026-10-19T11:20:01", "name": "assignment.done",
                "payload": { "assignment_id": "123/456", "status": "skipped" } } ] }
```

---

### `GET /api/settings` —— 设置页

配置现状 + 各平台凭据状态 + 可选依赖。**绝不返回凭据值**。

```json
{
  "env": "dev",
  "dry_run": true,
  "headless": true,
  "max_concurrency": 3,
  "min_interval": 0.0,
  "max_attempts": 3,
  "min_confidence": 0.75,
  "notify": "console",
  "db_path": "data/kedaibiao.db",
  "db_exists": true,
  "session_dir": "data/sessions",
  "dotenv_present": true,
  "env_keys_seen": ["KEDAIBIAO_UNIPUS_USER", "KEDAIBIAO_UNIPUS_PASS"],
  "optional_deps": { "playwright": false, "fastapi": false, "uvicorn": false },
  "platforms": [
    { "name": "unipus", "display_name": "U校园（Unipus）",
      "has_credentials": true,
      "credentials_hint": "KEDAIBIAO_UNIPUS_USER / _PASS",
      "capabilities": { "login": true, "submit": false, "...": true } }
  ]
}
```

> `env_keys_seen` **只列键名，不列值** —— 让用户确认「我的 `.env` 到底被读到没有」，
> 这是本项目踩过的坑（`.env` 里的凭据一度完全读不到）。

---

## 3. 平台只读动作（通用机制）

各平台除了框架契约（课程/作业/题目/提交）之外，还会有自己的只读能力
（例如批改网的「待办作业 / 历史作文 / 作文详情 / 作业号查询」）。

**不要在控制台里为每个平台写专属页面** —— 那样每接一个平台就要改一次前端。
改成让适配器**声明**自己的只读动作，前端通用渲染。

### `GET /api/platforms/<name>/actions` —— 该平台有哪些动作

```json
{
  "platform": "pigai",
  "actions": [
    { "name": "pending",  "label": "待办作业",
      "description": "未完成 / 未截止的作文任务",
      "params": [] },
    { "name": "finished", "label": "历史作文",
      "description": "已提交的作文列表", "params": [] },
    { "name": "essay",    "label": "作文详情",
      "description": "按作文号 + 稿件号取分数、四维、正文与批注",
      "params": [
        { "name": "rid", "label": "作文号", "type": "string", "required": true },
        { "name": "eid", "label": "稿件号", "type": "string", "required": true }
      ] },
    { "name": "lookup",   "label": "按作业号查询",
      "description": "用作文号直接打开写作页",
      "params": [
        { "name": "rid", "label": "作文号", "type": "string", "required": true }
      ] }
  ]
}
```

`params[].type` ∈ `string` / `int`。`required` 为 true 的必须给值。

### `POST /api/platforms/<name>/actions/<action>` —— 执行

**请求**（必须 `Content-Type: application/json`）：

```json
{ "rid": "200000004", "eid": "1000006" }
```

**响应**：统一的「展示信封」，前端按 `kind` 渲染，不需要认识平台细节。

```json
{
  "platform": "pigai",
  "action": "pending",
  "label": "待办作业",
  "kind": "table",
  "title": "待办作业（2 项）",
  "note": "",
  "columns": [
    { "key": "rid",     "label": "作文号", "mono": true },
    { "key": "title",   "label": "题目" },
    { "key": "deadline","label": "截止时间" },
    { "key": "status",  "label": "状态" }
  ],
  "rows": [
    { "rid": "200000004", "title": "My Campus Life",
      "deadline": "2026-10-25 23:59", "status": "未提交" }
  ]
}
```

`kind` 三种取值：

| kind | 结构 | 用途 |
| --- | --- | --- |
| `table` | `columns` + `rows` | 列表类（待办、历史） |
| `detail` | `fields` + `sections` | 单条详情（作文详情：分数/四维/正文/批注） |
| `message` | `message` | 没有结构化结果时的纯文案 |

`detail` 的形状：

```json
{
  "kind": "detail",
  "title": "作文详情 · 200000004 / 1000006",
  "fields": [ { "label": "总分", "value": "88.5" },
              { "label": "语言", "value": "85" } ],
  "sections": [
    { "label": "正文", "kind": "text", "text": "……" },
    { "label": "批注", "kind": "table",
      "columns": [ { "key": "range", "label": "位置" },
                   { "key": "note", "label": "批注" } ],
      "rows": [ { "range": "第 2 段", "note": "……" } ] }
  ]
}
```

> ⚠️ 作文正文是**个人内容**。前端渲染时不要外发（CSP 已禁止），
> 也不要在界面上到处复制粘贴。

### 错误

| 情况 | HTTP | kind |
| --- | --- | --- |
| 平台不存在 | 404 | `unknown_platform` |
| 动作不存在 | 404 | `unknown_action` |
| 缺必填参数 | 400 | `bad_request` |
| 凭据缺失 | 400 | `no_credentials` |
| 平台侧失败（未登录、找不到作文、网络） | 502 | `platform_error` |

`platform_error` 的 `error` 是可以直接显示给用户的中文，另外带
`detail`（平台异常类名）便于排查。

---

## 4. 静态资源

| 路径 | 文件 |
| --- | --- |
| `GET /` | `api/static/index.html` |
| `GET /static/app.js` | `api/static/app.js` |
| `GET /static/style.css` | `api/static/style.css` |

前端**不加构建步骤**：纯 HTML + 原生 JS + 一个 CSS 文件，改完刷新即生效。
不要引入 npm / 打包器 / CDN 依赖（离线也要能用）。

---

## 5. 界面结构与前端行为要求

### 5.1 导航结构（本版重构）

```
┌──────────────┬────────────────────────────────────────────────────┐
│ 课代表        │  [概览] [记录] [统计] [设置]      ▸ 当前：U校园  ⬤干跑 │  ← 顶栏
│              ├────────────────────────────────────────────────────┤
│ 平台          │                                                    │
│  ▸ 全部平台   │                                                    │
│  ▸ 批改网     │                     内容区                         │
│  ▸ 希沃品课   │              （随「当前平台」过滤）                  │
│  ▸ U校园      │                                                    │
│              │                                                    │
│ ──────────   │                                                    │
│  （状态区）    │                                                    │
└──────────────┴────────────────────────────────────────────────────┘
     ↑ 左侧栏                      ↑ 顶栏
```

- **左侧栏 = 平台列表**。点一下就切换「当前平台」，**不需要刷新页面**，
  切换后当前页面的数据立即重取。
- **顶栏 = 功能导航**：概览 / 记录 / 统计 / 设置，**四个独立入口**。
- 顶栏右侧固定显示：当前平台名 + 「干跑 / 真提交环境」徽标。
- 左侧栏底部的状态区显示连接状态与刷新按钮。

### 5.2 各页职责

| 页面 | 内容 |
| --- | --- |
| **概览** | 当前平台的卡片：平台数、题库总量与判分准确率、跑批成败计数；平台能力表；跑批触发区 |
| **记录** | 跑批历史 + 提交记录（**按当前平台过滤**，聚合视图下每行显示平台列） |
| **统计** | 题库构成（按来源）、判分准确率、各平台成败对比、最近趋势 |
| **设置** | 配置现状、各平台凭据状态（含期望的环境变量**键名**）、可选依赖、平台能力矩阵 |

### 5.3 行为要求

1. **默认干跑**：`live` 开关默认关闭。打开时必须二次确认，并在按钮上讲清楚
   「这会真的提交作业，不可撤销」。
2. **`capabilities.submit === false` 的平台**：禁用 `live` 开关并显示原因。
3. **轮询**：任务运行中每 1 秒拉一次 `/api/jobs/<id>`；结束后停止轮询。
4. **失败要显眼**：跑批失败、凭据缺失、平台未注册，都要在界面上说明白，
   不要只 console.log。
5. **空状态**：题库为空、没有跑批记录时给出人话提示，不要显示空白表格。
6. **不显示凭据**：界面任何地方都不展示密码 / 令牌（后端也不会返回）。
7. **平台只读动作**：不要为某个平台写专属页面。用
   `GET /api/platforms/<name>/actions` 拿到动作清单后**通用渲染**，
   按 `kind`（`table` / `detail` / `message`）选视图。这样接入新平台不用改前端。
8. **作文正文是个人内容**：只在详情视图里展示，不缓存、不外发、不做导出。

## 6. 安全约定（后端强制，前端配合）

- **只绑定 `127.0.0.1`**。要对外暴露必须显式传 `--host`，且启动时打印警告。
- `POST` 一律要求 `Content-Type: application/json`，并**拒绝跨站 `Origin`**。
  这两条合起来足以挡住 CSRF：浏览器对跨站 JSON 请求会先发预检，而本服务
  不返回任何 CORS 头，预检必然失败。
- 不返回任何凭据、cookie、令牌。
- 跑批写操作永不「默认真提交」—— 必须请求体里显式 `live: true`。
