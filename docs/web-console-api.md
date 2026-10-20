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
| `no_credentials` | 400 | 该平台还没绑定账号（`error` 会提示去「账号」页添加） |
| `busy` | 409 | 已有跑批在跑，同一时刻只允许一个 |
| `forbidden` | 403 | Host 或 Origin 被安全策略拒绝 |
| `platform_error` | 502 | 平台侧失败（未登录、找不到内容、网络异常），另带 `detail` = 平台异常类名 |
| `not_supported` | 501 | 该平台还没实现这个能力（例如提交）—— **界面应据此禁用入口**，不是错误重试 |
| `not_supported` | 501 | 该平台还没实现这个能力（例如提交） |
| `internal` | 500 | 服务端自身异常 |

---

## 1.5 认证、角色与数据隔离

### 角色

| 角色 | 能看什么 | 能做什么 |
| --- | --- | --- |
| `superadmin` | **所有人的数据**；默认视图是全部，可用 `?owner_id=` 收到某个人 | 管理用户（增删改、改密、停用、改角色）；管理任意账号 |
| `user` | **只有自己的数据** | 管理自己的平台账号 |

**隔离是存储层强制的，不是界面隐藏。** 普通用户带 `?owner_id=` 指向别人 → 403。

### 会话

登录成功后服务端下发 **HttpOnly Cookie**（`kdb_session`，`SameSite=Strict`）。
前端**不要**把令牌存到 localStorage —— cookie 由浏览器自动带上，前端不需要管。

未登录访问任何 `/api/*`（除 `auth/*`）→ **401 + `kind: "unauthorized"`**。
前端收到 401 就切到登录页。

### 接口

#### `POST /api/auth/login`

```json
{ "username": "admin", "password": "……" }
```

成功：`200` + `Set-Cookie` + `{"user": {…}}`；失败：`401` + `kind: "unauthorized"`。

#### `POST /api/auth/logout` —— 清会话

#### `GET /api/auth/me`

已登录：

```json
{ "user": { "id": 1, "username": "admin", "role": "superadmin" },
  "is_superadmin": true }
```

未登录：**`200` + `{"user": null, "is_superadmin": false}`**（不是 401）。
这个接口是给前端判断「要不要显示登录页」用的，必须公开可访问。

> ⚠️ `GET /`（index.html）**也是公开的** —— 登录页就在里面。
> 如果连它都要鉴权，用户会看到浏览器直接弹 401，连输入账号密码的地方都没有。

#### 用户管理（**仅超管**，否则 403）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/users` | 用户列表 + 每人账号数 |
| `POST` | `/api/users` | `{username, password, role, note}` 创建 |
| `POST` | `/api/users/<id>/update` | `{username?, password?, role?, disabled?, note?}` |
| `POST` | `/api/users/<id>/delete` | 删除（**不允许删掉最后一个超管**） |

> 密码规则：至少 8 位，否则 400。响应里**永远不返回密码或哈希**。

### 平台账号（换号）

每个用户对每个平台可以有**多个账号**，随时切换当前号。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/accounts?platform=` | 自己的账号列表（超管可加 `?owner_id=`） |
| `POST` | `/api/accounts` | `{platform, label, username, password, login?}` —— **创建时可选立即登录** |
| `POST` | `/api/accounts/<id>/activate` | 设为该平台的当前号 |
| `POST` | `/api/accounts/<id>/login` | `{password}` —— 重新登录（会话过期时用） |
| `POST` | `/api/accounts/<id>/update` | `{label?, username?}` |
| `POST` | `/api/accounts/<id>/delete` | 删除 |

账号对象：

```json
{ "id": 3, "owner_id": 1, "platform": "pigai", "label": "我的号",
  "display": "我的号", "username": "zhangsan", "is_active": true,
  "created_at": "2026-10-19T13:00:00",
  "last_login_at": "2026-10-19T13:05:00", "last_login_ok": true,
  "last_login_note": "" }
```

> **平台凭据只有这一个来源。** `.env` 里的 `KEDAIBIAO_<平台>_USER/_PASS`
> 已不再被读取 —— 之前两套来源并存，用户无法判断实际用的是哪个号。
>
> **也不落盘平台密码。** 密码只在登录那一刻用来换 cookie，换完就丢；数据库里
> 没有可解密的平台凭据。代价是会话过期后要重新输一次密码 —— 所以
> `last_login_ok` / `last_login_at` 要显眼地显示出来，失效时明确提示用户重登。
>
> 每个账号的 cookie 存在**自己的文件**里，切换当前号 = 切换 cookie，互不影响。

#### 数据接口的归属

登录后所有既有接口**自动按当前用户过滤**，前端不需要额外传参。
超管可用 `?owner_id=<id>` 看某个用户的数据；省略则看全部。
列表类响应每行都带 `owner_id`，聚合视图里要能看出「这条是谁的」。

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

> `llm` 是大模型答题的配置状态（**只报有没有设 key，不返回值**）：
> ```json
> { "enabled": true, "configured": true, "provider": "openai-compatible",
>   "model": "deepseek-chat", "api_base": "https://api.deepseek.com/v1",
>   "api_key_set": true, "configured_via": "环境变量 KEDAIBIAO_LLM_*" }
> ```
> `GET /api/settings` 里也有同样的 `llm` 字段。
>
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
>
> ⚠️ `capabilities` 是**声明**，不是承诺：声明 true 也可能因为缺凭据、会话失效
> 而失败。所以前端不能只靠它放行 —— 真收到 **501** 就把该平台记为「尚未实现提交」
> 并禁用入口；收到 502 则提示凭据/会话问题，允许重试。

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

### `POST /api/run` —— 触发一次跑批（**控制台界面已不再提供入口**）

> 2026-10-20 起，控制台 UI **移除**了「概览 · 触发跑批」面板（用户的明确要求）。
> 该 HTTP 接口保留，供 CLI / 脚本 / 自动化调用；界面内请用作业页的
> 「一键全部提交」（`batch-submit`）或平台动作。

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

### `GET /api/platforms/<name>/course-tree` —— 教材树（只读）

查询参数：`course`（课程号，必填）、`state`（`required` 默认 / `all` / `none`）、
`with_state`（`0` 关闭）。

返回：`course` / `course_remote_id` / `instance_id` / `class_id` / `class_name` /
`course_resource_id` / `page_base` / `finish_points` / `total_points` /
`group_count` / `required_count` / `units[]`（每单元含 `id`、`name`、`groups[]`；
每个分组含 `id` / `name` / `unit_id` / `unit_name` / `required` / `assignment_id` /
可选 `finished` / `score` / `total_items` / `correct_items` / `full_marks`）。

`state=required`（默认）只查**必修组**的状态，一本书约 5 秒；`all` 要查全部组（几十秒）。

### `POST /api/platforms/<name>/brush` —— 刷课（后台任务）

只对有原生链路且提供 `native_course_tree` 的平台开放（当前是 U校园）。返回 **202** + job。

```json
{ "course": "20001188271", "minutes": 10, "confirm": false,
  "submit_pending": false, "only_required": true, "interval": 8 }
```

`confirm=false` 只出计划（不提交、不建连接）。`job.counts` 含
`submitted / skipped / failed / targets / credited_seconds`；日志在 `job.logs`。

### `POST /api/platforms/<name>/batch-submit` —— 一键全部提交（后台任务）

只对**有原生提交链路**（`native_submit_flow = true`）且 `capabilities.submit = true`
的平台开放（当前是 U校园）。返回 **202** + 任务对象，进度用 `GET /api/jobs/<job_id>` 轮询。

**请求**：

```json
{ "course": "", "confirm": false, "limit": 50, "interval": 8, "skip_done": true }
```

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `course` | string | `""` | 课程号或课程名片段；留空 = 全部课程 |
| `confirm` | bool | `false` | `false` = 干跑（只读、只规划）；`true` = 逐组真交（不可逆） |
| `limit` | int | `0` | 最多处理多少个任务，0 = 不限 |
| `interval` | number | `8` | 真交时组间间隔秒数（0.5~120），防风控 |
| `skip_done` | bool | `true` | 跳过**所有已完成**（`first_pass` 有值）的任务组。平台留存首条记录、重交不改分（实测）；设为 `false` 才会重交 |

**响应**（202）：

```json
{ "job_id": "b-1", "kind": "batch-submit", "platform": "unipus",
  "confirm": false, "status": "running",
  "counts": { "planned": 0, "submitted": 0, "skipped": 0, "blocked": 0, "failed": 0 },
  "logs": [] }
```

任务对象比跑批多两样：`counts`（可提交/已提交/跳过/被拦/失败）与 `logs`
（逐行、带 `at`/`level`/`text`，前端直接滚动显示）。完成后
`status` ∈ `done` / `failed`，`summary` 是一行中文汇总。

同一时刻只允许一个后台任务（与跑批共用闸门）；再触发返回 **409 busy**。
平台没有原生提交链路或 `capabilities.submit = false` 时返回 **501 not_supported**。

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

响应里的 `llm` 是**可在控制台里改**的大模型配置（见下两节）：

```json
"llm": {
  "enabled": true, "configured": true, "provider": "openai-compatible",
  "model": "deepseek/deepseek-v4.1-flash",
  "api_base": "https://gateway.example.com/v1",
  "api_key_set": true,
  "timeout": 90.0, "temperature": 0.0,
  "editable": true,
  "override_fields": ["model", "timeout"],
  "source": "console",
  "configured_via": "控制台「大模型」面板"
}
```

- `source` ∈ `console`（界面保存过）/ `env`（只有 `.env`）/ `none`；
- `override_fields` 列出被界面覆盖的字段；
- **`api_key` 的明文永远不出现**，只有 `api_key_set: true/false`。

### `GET /api/platforms/<name>/logs` —— 该平台的执行日志

每个平台各存一份（内存环形缓冲，最多 400 行，进程重启后清空）：

```json
{
  "platform": "pigai",
  "total": 12,
  "logs": [
    { "at": "2026-10-19T22:41:03", "level": "info",
      "text": "$ 动作 assignments" },
    { "at": "2026-10-19T22:41:09", "level": "ok",
      "text": "→ assignments table 作业 / 任务（全部课程） —— 10 项" }
  ]
}
```

- `level` ∈ `info` / `ok` / `warn` / `error`（前端按级别着色）；
- 写日志的时机：动作调用（开始 + 结果）、`/api/assignments/submit`（干跑/真交 + 结果）、
  跑批任务（启动 + 结束/异常）、批改网评分循环的事件、iTEST 一键自动化的每一步；
- `limit` 默认 200、上限 400，取的是**末尾** N 行，`total` 是总行数；
- 未知平台 → 404 `unknown_platform`。

### `POST /api/platforms/itest/auto` —— 一键自动化（返回后台任务）

一次跑完「取卷 → 音频转写 + AI 答题 → 填写 → 逐项核验 → 等自动保存 → 重新抓取复核」，
**不交卷**。

```json
{ "exam_id": "1102016386", "autosave_wait": 65 }
```

- `autosave_wait` 可选：等平台自动保存的秒数（默认 65，`0` = 不等）；
- 返回 **202** + 任务对象（同 `GET /api/jobs/<id>` 的形状）：

```json
{
  "job_id": "a-3", "kind": "itest-auto", "status": "running",
  "exam_id": "1102016386",
  "logs":  [{ "at": "2026-10-19T22:10:01", "level": "ok", "text": "[2/6] 取卷完成…" }],
  "steps": [{ "name": "取卷", "ok": true, "detail": "26 题" }],
  "verify": { "total": 26, "matched": 26, "mismatched": [], "missing": [],
              "filled": { "blank": 80, "radio": 9, "textarea": 15, "missing": [] },
              "checked": { "bad": [], "total": 0 } },
  "manual": [ { "qid": "...", "kind": "radio", "kind_label": "单选",
                "stem": "...", "slots": 3,
                "options": [ { "letter": "A", "text": "...", "sub": 1 } ] } ],
  "paper": { "title": "26-27-1…B2U6", "question_count": 26, "slot_count": 104 },
  "error": ""
}
```

前端用法：拿到 `job_id` 后轮询 `GET /api/jobs/<id>`，把 `logs` 逐行铺进终端面板；
`manual` 非空时渲染人工选择表单；`status != running` 时停止轮询。

### `POST /api/platforms/itest/manual` —— 人工补答案

```json
{ "exam_id": "1102016386", "answers": { "1000608578": "A C D" } }
```

把人工选的答案并进答案单缓存（`data/itest/answers-<考试号>.json`），
并把补上的题从 `skipped` 里移除。返回：

```json
{ "exam_id": "1102016386", "answered": 26, "missing": [], "can_submit": true,
  "answers": { "1000608578": "A C D" } }
```

### `POST /api/settings/llm` —— 改大模型配置（超管）

```json
{ "base_url": "https://gateway.example.com/v1", "model": "deepseek-chat",
  "api_key": "sk-...", "timeout": 90, "temperature": 0 }
```

- 字段都可选；**留空字符串 = 删掉覆盖项、回落到 `.env`**；
- `api_key` 只写不读：传了就覆盖，`{"clear_api_key": true}` 清除；
- 校验：`base_url` 必须 `http(s)://`，`timeout` 1~600，`temperature` 0~2；
- 保存进本机 sqlite（`app_settings` 表），**不回写 `.env`，不用重启**，下一个请求即生效；
- 非超管 → 403 `forbidden`。

响应：同 `GET /api/settings` 里的 `llm` 对象。

### `POST /api/settings/llm/test` —— 测连通性（超管）

body 可为 `{}`（仍要求 `Content-Type: application/json`）。

```json
{ "ok": true, "model": "deepseek/deepseek-v4.1-flash",
  "api_base": "https://gateway.example.com/v1",
  "reply": "OK", "latency_ms": 1933 }
```

失败时返回 `kind=platform_error`、HTTP 502，`error` 是可直接展示的失败原因。

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

`params[].type` ∈ `string` / `int` / `text`（`text` 表示长文本，前端应渲染成多行输入框）。
`required` 为 true 的必须给值。

动作对象还可能带 `"mutates": true` —— 表示**会改平台侧状态**（开考计时、填答案、交卷），
前端应把「只读」徽标换成醒目的「写操作」，不要让人误当查询点。缺省（不出现）按只读处理。

> **列表行必须带 `assignment_id`。** 「作业」类动作（`assignments`）的每一行
> 除了展示用的 `id`，还要给 `assignment_id` —— 前端双击详情与提交都按它调
> `/api/assignments/*`。不要让前端去猜列名映射（`id` / `rid` / `task_id` 混用）。

### `POST /api/platforms/<name>/actions/<action>` —— 执行

**请求**（必须 `Content-Type: application/json`）：

```json
{ "rid": "200000001", "eid": "1000002" }
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
    { "rid": "200000001", "title": "My Campus Life",
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
  "title": "作文详情 · 200000001 / 1000002",
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

### 通用动作（所有平台都有）

框架契约里本来就有的读能力，注册成通用动作，前端所有平台通用：

| 动作 | 参数 | 说明 |
| --- | --- | --- |
| `courses` | 无 | `list_courses()` —— 该平台的课程 / 班级列表 |
| `assignments` | `course`（可选） | 各课程下的作业 / 任务；留空 = 遍历全部课程（单门课回课程级业务错误时跳过并点名，不整体失败） |

> 这两个会**真的登录并访问平台**（只读 GET）。它们与其他动作一样返回展示信封。
> 后端会落盘 cookie，所以同一次会话里后续调用会先探 `is_logged_in()`，
> 有效就跳过登录，不会每点一次就重新登录。
>
> **它们带 `"builtin": true`。** 「工具」页请**过滤掉 builtin 的动作** ——
> 课程与作业有专属功能页，在工具页重复列出只会让人困惑。
> 平台专属动作（如批改网的四个）`builtin` 为 `false`。

### 批改网（pigai）专属动作

| 动作 | 参数 | 说明 |
| --- | --- | --- |
| `pending` | 无 | 待办作业 |
| `finished` | 无 | 历史作文 |
| `essay` | `rid`（必填）、`eid`（必填） | 作文详情：分数、分项、正文、逐句批注 |
| `lookup` | `rid`（必填） | 按作文号打开写作页 |

> 这四个是**平台专属**的，其他平台没有。前端不要为 pigai 写专属页面 ——
> 按 `GET /api/platforms/<name>/actions` 返回的清单通用渲染即可。

### 希沃品课（pinco）专属动作

| 动作 | 参数 | 说明 |
| --- | --- | --- |
| `learning` | `course`（可选，留空=全部课程） | 学习任务 / 材料进度：课程、材料号、名称、大小、进度（单门课坏掉时跳过并点名） |
| `material` | `course`（必填）、`material_id`（必填） | 材料详情：大小、进度、下载地址（带签名，只展示不落库）+ 原始字段摘要 |
| `answers` | `assignment_id`（必填） | 作业题目与作答槽：逐题题型/题干/选项 + 已有 `score/review/submitted` |
| `connection` | 无 | 连接自检：会话、`is_logged_in`、`health_check`、课程数量 |
| `progress` | `course`（必填）、`material_id`（可选）、`target`、`confirm`、`max_seconds`、`max_materials`、`page_interval` | **推进学习进度（写操作）**：按页翻课件；`confirm=true` 才真写，默认干跑 |
| `signin` | `code`（必填，签到码或整条签到链接）、`confirm` | **课堂签到（写操作）**：把 `/student/signin?attendanceCode=…` 里的码提交给考勤接口；`confirm=true` 才真发，默认干跑 |

> 前四个都是**只读**（`mutates=false`）。`answers` 需要在作业列表里定位作业号
> （品课没有「按作业号取作业」的只读接口）；`connection` 是唯一一个「失败也
> 返回结果而不是 502」的动作 —— 自检的价值就是把失败点说清楚。
>
> 两个**写操作**（`mutates=true`，前端会显示「写操作」徽标）：
>
> - `progress`：机制是「打开课件拿总页数 → 逐页上报 `maxPageIndex`」，进度 =
>   `(maxPageIndex+1)/总页数`。默认干跑（`confirm` 留空），`confirm=true` 才真的
>   翻页；`material_id` 留空时处理该课程下未达标的材料，最多 `max_materials` 份
>   （默认 3，可再执行一次续推 —— 进度从当前页继续）。批量/长时间推进仍建议用
>   `scripts/pinco_progress.py --all --live`（带页数/时长双上限），机制与证据见
>   [docs/verification/pinco-progress.md](verification/pinco-progress.md)。
> - `signin`：解析 `attendanceCode`（也接受裸码 / 含链接的文本）后调
>   `PostV1TeachingAttendanceAttend {attendanceKey}`；平台拒绝（7207 码无效 /
>   7208 已结束 / 10419 不匹配）作为结果返回，不变成 502。签到码是滚动码，
>   只能接当次链接；机制与未验证边界见
>   [docs/verification/pinco-signin.md](verification/pinco-signin.md)。

### `POST /api/assignments/detail` —— 单个作业详情（双击作业行）

```json
{ "platform": "pigai", "assignment_id": "1000001" }
```

返回 `kind: detail` 的展示信封（含任务号、名称、课程、截止、题目列表、
平台原始数据）。等同于调用动作 `detail`。

### `POST /api/assignments/submit` —— 针对单个作业提交

```json
{ "platform": "pigai", "assignment_id": "1000001", "confirm": false }
```

- `confirm=false`（默认）：**只规划不提交**，返回每题打算怎么答 + 置信度：

  ```json
  { "platform": "pigai", "assignment_id": "1000001", "title": "…",
    "question_count": 3, "answered": 3, "min_confidence": 0.91,
    "threshold": 0.75, "dry_run": true, "submitted": false,
    "answers": [ { "index": 1, "stem": "…", "choices": [1], "text": "",
                   "source": "bank_hit", "confidence": 0.95, "needs_human": false } ],
    "note": "这是干跑结果，没有向平台提交任何内容。" }
  ```
- `confirm=true`：真的调平台写接口

**平台没实现提交时返回 `501 not_supported`**，`error` 会说明原因。
目前 pigai / unipus 都是 `capabilities.submit = false`，
pinco 的提交请求体结构也还没由真实样本确认 —— **不会假装成功**。

两条硬门槛（即使 `confirm=true` 也拦）：
1. 一题都没答出来 → 400
2. 最低置信度低于 `min_confidence` → 400（宁可交白卷也不交错答案）

### 错误

| 情况 | HTTP | kind |
| --- | --- | --- |
| 平台不存在 | 404 | `unknown_platform` |
| 动作不存在 | 404 | `unknown_action` |
| 缺必填参数 | 400 | `bad_request` |
| 凭据缺失 | 400 | `no_credentials` |
| 平台侧失败（未登录、找不到内容、网络） | 502 | `platform_error` |

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

### 5.1 导航结构（本轮定稿）

**两栏布局：左边一列两段（上=切平台，下=切该平台的功能），右边是内容区。**
不要横跨所有平台平铺能力矩阵 —— 当前选哪个平台，就只看那个平台的。

```
┌────────────────┬────────────────────────────────────────────┐
│ 课代表 kedaibiao│                        ▸ 批改网   ⬤干跑     │  ← 顶栏右侧常驻
│ ────────────── ├────────────────────────────────────────────┤
│ 平台  点击切换  │                                            │
│  全部平台       │                                            │
│  批改网    pigai│                                            │
│  希沃品课 pinco │                内容区                       │
│  U校园  unipus  │        （随「当前平台 + 当前功能」渲染）      │
│ ────────────── │                                            │
│ 功能            │                                            │
│  概览           │                                            │
│  课程           │                                            │
│  作业           │                                            │
│  题库           │                                            │
│  工具           │                                            │
│  运行日志       │                                            │
│  统计           │                                            │
│  账号           │                                            │
│ ────────────── │                                            │
│ 状态区 / 刷新   │                                            │
└────────────────┴────────────────────────────────────────────┘
        ↑ 左侧栏（两段）              ↑ 内容区
```

**命名用任务型词汇**（照参考产品的语言习惯）：课程 / 作业 / 题库 / 工具 /
运行日志 / 统计 / 账号。**不要**再用「记录」「设置」这种元信息命名。

### 5.2 各功能页职责

| 功能 | 数据来源 | 说明 |
| --- | --- | --- |
| **概览** | `/api/overview?platform=` | 当前平台卡片（名称、凭据、能力摘要）+ 运行参数 + 当前任务 |
| **课程** | 动作 `courses` | 该平台的课程 / 班级列表（**会真登录并访问平台**，只读） |
| **作业** | 动作 `assignments` | 各课程下的作业 / 任务（同上，只读） |
| **题库** | `/api/bank?platform=` | 题目数、判分准确率、按来源构成 |
| **工具** | 动作清单 | 平台只读动作（通用渲染）。批改网会看到待办/历史/详情/查询 |
| **运行日志** | `/api/runs?platform=`、`/api/submissions?platform=`、`/api/events` | 跑批历史、提交记录、事件流 |
| **统计** | `/api/statistics` | 跨平台对比（**不传 platform**，它本身就是横向视图） |
| **账号** | `/api/settings` | 配置现状、各平台凭据状态（含期望环境变量**键名**）、可选依赖 |

**「全部平台」时**：作业/课程/题库/工具这类需要具体平台的功能要给出提示
（例如「请先在左侧选择一个平台」），**不要**静默显示空表。
只有概览、运行日志、统计、账号在聚合视图下有意义。

### 5.3 平台能力怎么显示

**不要**做成横跨所有平台的对比矩阵，**也不要**在能力表里放「跑一次」按钮。

正确做法：当前选中哪个平台，就在**它自己的位置**显示它自己的能力
（概览页的能力摘要、账号页该平台那一行）。跨平台对比只在「统计」页出现。

### 5.4 行为要求

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
9. **会访问平台的功能要讲清楚**：课程 / 作业 / 工具 这些动作会真的登录并
   读取平台数据。界面上要标明「只读」，并给出加载状态；失败时显示后端的
   中文 `error`。

## 6. 安全约定（后端强制，前端配合）

- **只绑定 `127.0.0.1`**。要对外暴露必须显式传 `--host`，且启动时打印警告。
- `POST` 一律要求 `Content-Type: application/json`，并**拒绝跨站 `Origin`**。
  这两条合起来足以挡住 CSRF：浏览器对跨站 JSON 请求会先发预检，而本服务
  不返回任何 CORS 头，预检必然失败。
- 不返回任何凭据、cookie、令牌。
- 跑批写操作永不「默认真提交」—— 必须请求体里显式 `live: true`。
