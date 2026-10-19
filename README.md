# 课代表 · kedaibiao

> 多平台作业 / 课程一体化自动化框架 —— 把散落在各个教学平台上的待办、作业、考试、题库收进同一套调度里。

课代表负责「收作业」：统一登录态、统一任务模型、统一作答与提交链路、统一执行留痕。
平台差异全部关在适配器里，核心调度一行都不用改。

**当前状态：框架骨架（v0.1.0）。** 核心层与题库层已完成并通过全量单测（零第三方依赖）；
尚无任何真实平台适配器接入。

---

## 1. 它解决什么问题

一个人手上往往同时有多个教学平台：东软系教学平台、超星/学习通、智慧树、U校园……
每个平台一套登录、一套待办列表、一套题型、一套提交接口。重复劳动有两个来源：

1. **来回切换**：作业散落在 N 个站点的 N 个角落里，靠人肉巡检，容易漏交；
2. **重复作答**：同一道题在不同平台、不同学期反复出现，明明答过一次却要再答一遍。

课代表的应对是**分层 + 沉淀**：

- 分层：`core`（与平台无关） / `platforms`（平台适配） / `tasks`（编排） / `bank`（题库）；
- 沉淀：每次作答的题目与答案进题库，被平台判分过的答案带「已验证」标记，可高置信复用。

---

## 2. 架构

```
                    ┌───────────────────────────────────────────┐
                    │  CLI (__main__)   /   Web API (api/)      │   控制面
                    └───────────────────┬───────────────────────┘
                                        │
                    ┌───────────────────▼───────────────────────┐
                    │  runtime.py   运行时装配                   │
                    │  配置·凭据·题库·传输·通知·调度 → 流水线     │
                    └───────────────────┬───────────────────────┘
                                        │
                    ┌───────────────────▼───────────────────────┐
                    │  tasks/pipeline.py   任务流水线            │
                    │  登录 → 拉课 → 拉作业 → 作答 → 提交 → 回填 │
                    └───┬───────────┬───────────┬───────────────┘
                        │           │           │
        ┌───────────────▼──┐  ┌─────▼──────┐  ┌─▼──────────────┐
        │ tasks/answerer   │  │ core/      │  │ bank/store     │
        │ 题库 / 规则 / 链式│  │ scheduler  │  │ 题库 + 留痕     │
        └──────────────────┘  │ 并发·限速·重试│  └────────────────┘
                              └─────┬──────┘
                                    │
                    ┌───────────────▼───────────────────────────┐
                    │  platforms/base.PlatformAdapter  (契约)    │
                    └───┬───────────┬───────────┬───────────────┘
                        │           │           │
                    unipus/       超星/       智慧树/   …         适配层
                        │           │           │
            ┌───────────┴───────────┴───────────┴───────────────┐
            │ 两种传输方式，按平台二选一                          │
            │  browser/session.py  Playwright 会话池             │
            │  net/http.py         协议型 HTTP 客户端            │
            └───────────────────────────────────────────────────┘
```

### 分层规则（硬约束）

| 层 | 目录 | 可以依赖 | 禁止 |
| --- | --- | --- | --- |
| 核心 | `core/` | 仅标准库 | 依赖 `platforms` / `browser` / `api` |
| 配置 / 凭据 | `config/`、`security/` | `core` | 依赖平台或流水线 |
| 传输 | `net/`、`browser/` | 仅标准库 | 依赖 `platforms` / `tasks` |
| 题库 | `bank/` | `core` | 依赖 `platforms` |
| 适配 | `platforms/` | `core`、`net`、`browser` | 依赖 `tasks`（不能反向调用流水线） |
| 编排 | `tasks/` | 以上全部 | — |
| 装配 / 控制面 | `runtime.py`、`api/`、`__main__` | 全部 | — |

违反这条规则意味着平台私有概念泄漏进了核心，新增平台的成本会立刻反弹。

详情见 [docs/architecture.md](docs/architecture.md)。

---

## 3. 快速开始

核心层零第三方依赖，克隆下来就能跑测试：

```bash
git clone <repo> && cd 刷题刷课

make test          # 全量单测，纯标准库即可跑
make doctor        # 环境自检（Python 版本、可选依赖、存储可写、凭据状态）
make platforms     # 列出已注册的平台适配器
```

不装 make 就直接：

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -t .
PYTHONPATH=src python3 -m kedaibiao doctor
```

### 跑一次任务

平台账号**只在控制台「账号」页绑定** —— `.env` 不再保存任何平台凭据。

```bash
# 1. 起控制台，登录后左侧「账号」→ 添加平台账号（会真登录一次，只存会话）
PYTHONPATH=src python3 -m kedaibiao web

# 2. 干跑 —— 只规划、不产生任何写操作，看清楚它打算交什么
PYTHONPATH=src python3 -m kedaibiao run <平台名>

# 3. 确认无误后，才真正提交
PYTHONPATH=src python3 -m kedaibiao run <平台名> --live
```

命令行也用同一份账号（没有登录态，取该平台绑定过的号）。没绑定就直接报错，
**不会回退去读 `.env`**。

常用限定参数：`--limit 3`（本次最多 3 个作业）、`--kinds homework,exam`（只处理指定类型）。

跑完可以用 `make bank-stats` 看题库命中率与执行记录。

### Web 控制台

```bash
PYTHONPATH=src python3 -m kedaibiao web            # 默认绑 127.0.0.1:8765
```

![控制台 · 概览](docs/screenshots/ui-overview-pigai.png)

**左侧栏一列两段**：上半切平台，下半切该平台的功能。功能按任务命名 ——
概览 / 课程 / 作业 / 题库 / 工具 / 运行日志 / 统计 / 账号。所有数据按「当前平台」
过滤，另有「全部平台」聚合视图（跨平台对比只在「统计」页）。

「课程」「作业」「工具」会**真的登录并读取平台数据**（只读 GET），因此带「只读」
徽标与加载态；cookie 会落盘，同一次会话内后续访问会先探 `is_logged_in()`，
有效就跳过登录。

![控制台 · 作业](docs/screenshots/ui-assignments.png)

默认只绑回环地址。要绑到别的地址（例如 Tailscale 网内）用
`--host <地址> --allow-host <主机名>`，启动时会打印警告 —— 能访问到该地址的设备
都能操作控制台，包括触发跑批。

### 平台只读动作（CLI）

各平台除框架契约之外的只读能力（批改网的待办作业 / 历史作文 / 作文详情 /
作业号查询等）走同一套通用机制，CLI 与控制台**共用**：

```bash
PYTHONPATH=src python3 -m kedaibiao actions pigai              # 看该平台有哪些动作
PYTHONPATH=src python3 -m kedaibiao action pigai pending       # 待办作业
PYTHONPATH=src python3 -m kedaibiao action pigai essay rid=200000001 eid=1000002
```

> 这套机制的设计目标是**接入新平台不用改前端**：适配器能力 → 统一「展示信封」
> （`table` / `detail` / `message`），前端只按 `kind` 渲染。
> 接口见 [docs/web-console-api.md](docs/web-console-api.md#L237)。

### 可选依赖

```bash
make install-browser   # playwright + chromium，浏览器型平台需要
```

> 控制台**不用 FastAPI**：本机装不上（没有 pip，`python3 -m venv` 缺 ensurepip），
> 所以用标准库 `http.server`。逻辑全在 `api/service.py`，与 HTTP 框架无关 ——
> 将来要换 FastAPI 只换外壳。

---

## 4. 安全默认（重要）

这套框架的默认值是**保守**的，因为它的写操作不可撤销：

| 开关 | 默认 | 含义 |
| --- | --- | --- |
| `dry_run` | `true` | 只规划不提交；跑批会打印「打算交什么」 |
| `min_confidence` | `0.75` | 任一题置信度低于阈值就整份不提交 |
| `skip_finished` | `true` | 平台已完成 / 本地已成功过的作业直接跳过 |
| `headless` | `true` | 无头运行；撞验证码时截图并放弃，不硬闯 |
| `max_concurrency` | `3` | 全局并发上限，避免把对方站点打崩 |

要真提交必须**显式**关掉干跑：`kedaibiao run <平台> --live`（或 `KEDAIBIAO_DRY_RUN=0`）。

`Credentials.__repr__` 被重写为屏蔽密码，`.env`、`data/`、`*.storage_state.json`、
`*.cookies.json` 全在 `.gitignore` 里 —— 凭据和登录态永远不进版本库。

---

## 5. 目录结构

```
.
├── pyproject.toml          依赖与 entry point 注册
├── Makefile                常用命令
├── config/                 default.toml / local.toml（本地配置不入库）
├── docs/
│   ├── architecture.md     分层、数据流、扩展点
│   ├── integration-guide.md 多窗口并行开发与整合的边界纪律
│   ├── license-policy.md   第三方代码引用政策与已评估清单
│   ├── security.md         敏感信息与入库纪律（凭据/截图/db 绝不入库）
│   ├── naming.md           命名与标识符规范
│   └── research/           平台与依赖的调研报告（含证据来源）
├── src/kedaibiao/
│   ├── runtime.py          运行时装配：配置·凭据·存储·传输 → 流水线
│   ├── __main__.py         CLI
│   ├── core/               模型 · 注册表 · 事件总线 · 调度器
│   ├── platforms/          平台适配器（base.py 是契约）
│   ├── browser/            Playwright 会话池（浏览器型平台）
│   ├── net/                协议型 HTTP 客户端（协议型平台）
│   ├── tasks/              流水线 · 答题器
│   ├── bank/               题库与执行留痕（SQLite）
│   ├── security/           凭据装载与脱敏
│   ├── notify/             通知渠道
│   ├── api/                Web 控制台（service.py 与 HTTP 框架无关）
│   │   └── static/         零构建前端（纯 HTML + 原生 JS）
│   └── config/             配置加载（TOML + .env + 环境变量）
└── tests/                  单测：零外部依赖，可离线全量跑
```

---

## 6. 新增一个平台适配器

1. 建目录 `src/kedaibiao/platforms/<平台>/`；
2. 继承 `PlatformAdapter`，按需实现 6 个钩子：

   ```python
   class XxxAdapter(PlatformAdapter):
       name = "xxx"                 # 机器名，用作 registry key 与 DB 字段
       display_name = "某某平台"
       capabilities = Capabilities(course_playback=True)

       async def login(self, credentials: Credentials) -> bool: ...
       async def is_logged_in(self) -> bool: ...
       async def list_courses(self) -> list[Course]: ...
       async def list_assignments(self, course: Course) -> list[Assignment]: ...
       async def fetch_questions(self, assignment: Assignment) -> list[Question]: ...
       async def submit(self, assignment, answers) -> SubmissionResult: ...
   ```

3. 在 `pyproject.toml` 的 `[project.entry-points."kedaibiao.adapters"]` 里登记；
4. 用 `FakeAdapter` 的写法补单测 —— **适配器必须能在不联网的前提下被验证**，
   把网络层抽成可替换的 client。

适配器里禁止出现的东西：`time.sleep`（用 `Scheduler`）、裸 `print`
（用 `self.log`）、私有的「成功码」判定（集中写在一处并注释来源）。

---

## 7. 第三方代码引用政策

**引用任何开源项目之前，先读它的 LICENSE 并把结论写进
[docs/license-policy.md](docs/license-policy.md)，再动代码。**
已有评估结论：

| 项目 | 结论 | 原因摘要 |
| --- | --- | --- |
| [`Yunlk/uai-core`](https://github.com/Yunlk/uai-core) | ❌ 不可引用，只可借鉴思路 | 自定义非商业协议（无 SPDX），禁商用 + 衍生作品须同等条款；且是 U校园专用 Rust CLI，与本项目平台零重合。详见 [调研报告](docs/research/uai-core.md) |

不引入 copyleft 代码、不引入许可不可核验的代码、不复制他人代码/注释/文档表达。

---

## 8. 路线图

- [x] **M0 骨架**：分层、模型、调度、题库、流水线、运行时装配、CLI、单测
- [ ] **M1 首个真实平台（代码完成，待联调）**
  - [x] U校园适配器只读链路：登录 → 拉课程 → 拉任务 → 拉题目
        (`src/kedaibiao/platforms/unipus/`，190 项离线测试)
  - [x] 传输层、凭据、运行时装配、`kedaibiao run` 入口
  - [ ] **用真实账号联调**：离线测试锁的是逻辑，不证明线上接口可用
  - [ ] 规格里两处待实测确认：目录容器键名、各应用是否接受字符串成功码
- [ ] **M2 作答闭环**：题型解析、题库回填、提交与判分校验
- [ ] **M3 调度与观测**：Web 控制台、定时跑批、通知渠道、失败重试面板
- [ ] **M4 多平台**：接入第二个平台，验证适配器契约是否真的够用

**M1 的验收标准是「只读跑通」** —— 在没有任何一次写操作的前提下，把某个平台的
课程与作业清单完整拉下来并落库。

> ⚠️ M1 目前的状态是「代码写完、离线全绿」，**不等于「能用」**。U校园的题面与
> 标准答案语义在规格阶段就无法从参考实现确认（见
> [调研报告](docs/research/unipus-protocol.md) 第 10-11 节），必须用真实账号
> 跑一次只读链路才能验证。在联调通过之前，不要打开 `--live`。

---

## 9. 免责声明

本项目是自动化工程实践，使用者需自行负责：

- 各教学平台的服务条款通常禁止自动化访问，**违规可能导致账号封禁**；
- 用自动化工具替代本人完成课程与作业，可能违反所在院校的学术诚信规定。

框架默认 `dry_run=True` 正是为此 —— 先看清楚它打算做什么，再决定是否放手。
作者不对任何因使用本项目产生的后果负责。

---

## 10. 许可

**GNU Affero General Public License v3.0（AGPL-3.0-only）**，全文见 [LICENSE](LICENSE)。

```
Copyright (C) 2026  HB <wzdshb@126.com>

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU Affero General Public License as published
by the Free Software Foundation, version 3 of the License.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU Affero General Public License for more details.
```

### 这对「接入别的平台」意味着什么

AGPL-3.0 是**强传染**许可，且覆盖网络服务（第 13 条：通过网络提供服务也算分发，
必须向用户提供完整源码）。因此：

- ✅ 并入的第三方代码必须是 **AGPL 兼容**的（MIT / BSD / Apache-2.0 / ISC / GPL 都可以）；
- ❌ **不能**并入带附加限制的代码 —— 包括「禁止商用」「衍生作品须同等条款」
  「仅限非商业使用」这类条款。AGPL-3.0 第 7 条明确不允许附加限制。
  具体到本项目：[`Yunlk/uai-core`](https://github.com/Yunlk/uai-core) 就属于这一类，
  **不能并入本仓库**，详见 [docs/license-policy.md](docs/license-policy.md)。

接入新平台时，请先过一遍 [docs/license-policy.md](docs/license-policy.md) 的决策流程。

> 想改用 `AGPL-3.0-or-later`（允许未来升到 AGPL-4.0）：把 `pyproject.toml` 的
> `license` 改成 `"AGPL-3.0-or-later"` 即可，其余不变。
