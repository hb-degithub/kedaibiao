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
| 成功 | HTTP **2xx**（读接口 200，`POST /api/run` 返回 **202**）+ JSON 对象 |
| 失败 | HTTP 4xx/5xx + `{"error": "<中文说明>", "kind": "<机器可读类型>"}` |
| 时间 | ISO 8601 字符串（`2026-10-19T11:20:00`） |
| 得分/准确率 | 可能为 `null`，前端要能显示 `—` |

前端判定成功请用 `2xx`，不要写死 `=== 200`。

### 错误响应

```json
{ "error": "未注册的平台: 'ghost'", "kind": "unknown_platform" }
```

`kind` 取值：`bad_request` / `unknown_platform` / `no_credentials` / `not_found`
/ `busy` / `internal`。

---

## 2. 接口

### `GET /api/overview` —— 概览卡片

```json
{
  "version": "0.1.0",
  "python": "3.14.4",
  "repo_root": "/home/hb/刷题刷课",
  "env": "dev",
  "dry_run": true,
  "headless": true,
  "max_concurrency": 3,
  "min_confidence": 0.75,
  "db_path": "data/kedaibiao.db",
  "db_exists": true,
  "platform_count": 3,
  "credentialed_platforms": ["unipus"],
  "optional_deps": { "playwright": false, "fastapi": false },
  "bank": { "total": 12, "graded": 5, "correct": 4, "accuracy": 0.8,
            "ungraded": 7, "by_source": {"bank_hit": 12} },
  "runs": { "total": 40, "succeeded": 30, "failed": 4, "skipped": 6 }
}
```

> `accuracy` 与 `score` 可能为 `null`。
> **`runs` 的计数是全量的**（数据库中所有执行记录），不是「最近 N 次」——
> 前端照原样展示，不要加「最近」字样。

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

### `GET /api/bank` —— 题库统计

**打平对象**（不是 `{"bank": {...}}`）：与 `overview.bank` 同结构，另加一个
`db_path` 字段。**没有导出端点**，`db_path` 只是给人看数据库在哪。

```json
{ "total": 12, "graded": 5, "correct": 4, "accuracy": 0.8,
  "ungraded": 7, "by_source": {"bank_hit": 12},
  "db_path": "data/kedaibiao.db" }
```

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

## 3. 静态资源

| 路径 | 文件 |
| --- | --- |
| `GET /` | `api/static/index.html` |
| `GET /static/app.js` | `api/static/app.js` |
| `GET /static/style.css` | `api/static/style.css` |

前端**不加构建步骤**：纯 HTML + 原生 JS + 一个 CSS 文件，改完刷新即生效。
不要引入 npm / 打包器 / CDN 依赖（离线也要能用）。

---

## 4. 前端行为要求

1. **默认干跑**：`live` 开关默认关闭。打开时必须二次确认，并在按钮上讲清楚
   「这会真的提交作业，不可撤销」。
2. **`capabilities.submit === false` 的平台**：禁用 `live` 开关并显示原因。
3. **轮询**：任务运行中每 1 秒拉一次 `/api/jobs/<id>`；结束后停止轮询。
4. **失败要显眼**：跑批失败、凭据缺失、平台未注册，都要在界面上说明白，
   不要只 console.log。
5. **空状态**：题库为空、没有跑批记录时给出人话提示，不要显示空白表格。
6. **不显示凭据**：界面任何地方都不展示密码 / 令牌（后端也不会返回）。

## 5. 安全约定（后端强制，前端配合）

- **只绑定 `127.0.0.1`**。要对外暴露必须显式传 `--host`，且启动时打印警告。
- `POST` 一律要求 `Content-Type: application/json`，并**拒绝跨站 `Origin`**。
  这两条合起来足以挡住 CSRF：浏览器对跨站 JSON 请求会先发预检，而本服务
  不返回任何 CORS 头，预检必然失败。
- 不返回任何凭据、cookie、令牌。
- 跑批写操作永不「默认真提交」—— 必须请求体里显式 `live: true`。
