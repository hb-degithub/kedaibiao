# 课代表 · kedaibiao

> ⚠️ **仅供学习与技术研究，除此之外的一切用途都被明确禁止。**
> **严禁作弊、代考、代刷课、代写作业、刷分刷时长、操作他人账号或任何违法用途。**
> **违反者后果自负；使用前必须读完全文「免责声明」，不同意请立即删除本项目。**

> 多平台作业 / 课程一体化自动化框架 —— 把散落在各个教学平台上的待办、作业、考试、题库收进同一套调度里。

课代表负责「收作业」：统一登录态、统一任务模型、统一作答与提交链路、统一执行留痕。
平台差异全部关在适配器里，核心调度一行都不用改。

**当前状态：v0.1.0 —— 框架 + 6 个真实平台适配器。** 核心层与题库层零第三方依赖，
离线单测全绿（`pytest -q` 2075 项 / 约 30 秒；`make test` 走 unittest，1964 项）；
U校园、东软智慧教育、超星学习通、iTEST、批改网、希沃品课均已接入。
每个平台「实测到哪一步、哪些只有静态证据」写在
[docs/verification/](docs/verification/) 与 [docs/research/](docs/research/) 里 ——
README 只给结论，证据在文档里。

---

## ⚠️ 免责声明（使用前必读，违者后果自负）

### 一、定位：仅供学习，不附带任何「使用授权」

本项目是**自动化工程的学习样本**：它把多平台登录、任务模型、作答与提交链路抽象成一套
可测试的框架，供学习协议分析、工程分层与测试设计。**它不是学习工具之外的任何东西。**

`AGPL-3.0` 只授予你**著作权意义上的许可**，绝不等同于授权你绕过任何平台规则、学校纪律或
法律义务。**以本项目为工具实施违规行为时，许可证不构成任何抗辩理由。**

### 二、零容忍：以下行为一律禁止

- ❌ **任何作弊与学术不端**：代考、替考、代刷课、代写作业、自动答题交卷、刷分、刷时长、
  伪造学习 / 考勤记录、替他人完成课程任务；
- ❌ **任何商业性使用**：代做、代刷、代考、出售或出租本工具、打包进付费服务、对外提供
  「代刷 / 代考」服务；
- ❌ **任何越权与非法行为**：批量注册、撞库、密码爆破、薅羊毛、爬取或倒卖他人数据、
  操作**非本人**账号、绕过平台风控牟利；
- ❌ **任何违反法律法规、平台服务条款、学校规章或考试纪律的行为**。

**本项目不欢迎上述任何一类使用者。** 发现被用于这些用途时，作者会明确与其切割，
并保留配合平台、学校或司法机关提供相关信息的权利（在符合法律的前提下）。

### 三、严厉后果（如实说明，不是吓唬）

- **平台侧**：账号被限制、冻结或永久封禁；成绩作废、学分清零；已修课程记录被撤销；
  平台可依服务条款追究违约责任；
- **学校侧**：认定为学术不端可给予警告、记过、留校察看乃至**开除学籍**等处分，记入个人档案，
  影响保研、评优、毕业与就业；
- **法律侧**：非法获取计算机信息系统数据、破坏计算机信息系统、侵犯公民个人信息、
  帮助信息网络犯罪活动等，均可能承担行政或**刑事责任**；考试作弊情节严重的同样可能触犯刑法；
- **后果由使用者本人承担。** 作者既不承担、也无法为你兜底。

### 四、责任与担保

- 按**「现状」（AS IS）**提供，不提供任何明示或暗示的担保；
- **不保证**协议长期有效、不保证不被平台检测、不保证结果正确、不保证不封号；
- 作者**从未**提供「反检测」「免封号」「安全刷课」之类的承诺 —— 任何此类说法都不是本项目说的；
- 因使用本项目造成的**一切损失**（成绩、学分、学籍、账号、数据、法律后果等），
  作者不承担任何责任；
- 项目可能随时修改、停止维护或删除，不另行通知。

### 五、唯一被允许的用法

- 只在你**自己的账号、自己的设备**上，为了**学习**协议与工程实现而阅读、离线运行、写测试；
- 默认 `dry_run=True`；任何真实提交都必须由你**显式确认**，且你必须自行确认它不违反你所处的
  任何规定；
- 如果所在的课程、学校或平台禁止任何自动化操作 —— **本项目对你就是不可用的，请直接离开**。

### 六、同意条款

**使用本项目（包括但不限于克隆、运行、修改、分发、参考其代码）即视为你已完整阅读、
理解并同意上述全部内容；不同意请立即停止使用并删除全部副本。**
作者保留在不通知的情况下修改本声明的权利，以仓库最新版本为准。

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
                 unipus/ neusoft/ chaoxing/ itest/ pigai/ pinco/       适配层
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

### 命令一览（CLI）

| 子命令 | 作用 |
| --- | --- |
| `platforms` | 列出已注册的平台适配器 |
| `doctor` | 环境自检（Python、可选依赖、存储可写、凭据状态） |
| `config` | 打印生效配置（已脱敏） |
| `run <平台> [--live]` | 跑一次任务流水线；默认干跑，`--live` 才真提交 |
| `schedule [--once] [--dry-run]` | 定时跑批：按 `[[schedule.jobs]]` 执行；默认干跑 |
| `web` | 起 Web 控制台（默认 `127.0.0.1:8765`） |
| `capture <平台>` | 从浏览器用户目录抓取登录态 cookie（浏览器会话型平台） |
| `actions <平台>` | 列出该平台的只读动作 |
| `action <平台> <动作> k=v…` | 执行一个平台动作；写操作默认干跑，`confirm=true` 才真发 |
| `bank-stats` / `bank-export` / `bank-import` | 题库统计 / 导出 JSONL / 从 JSONL 导入 |

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

### 定时跑批

不用系统 cron 也能定时：在 `config/local.toml` 里写任务，`kedaibiao web` 启动时
自动拉起后台线程（退出时自动停），也可以单独常驻：

```toml
# config/local.toml（不入库）
[schedule]
enabled = true          # web 启动时是否自动起线程
tick = 30.0             # 每 30 秒检查一次到期任务

[[schedule.jobs]]
name = "unipus-daily"
platform = "unipus"
every = "24h"           # s / m / h / d；纯数字按秒
live = false            # 默认干跑，改成 true 才会真提交
limit = 0               # 0 = 不限
kinds = ""              # 空 = 全部；或 course,homework,exam,practice
```

```bash
kedaibiao schedule                 # 常驻循环（Ctrl-C 退出）
kedaibiao schedule --once          # 只跑到期的任务一次
kedaibiao schedule --once --dry-run  # 即使任务写了 live=true 也强制干跑
```

**安全默认**：整个功能默认关闭；每个任务默认 `live=false`；任务串行执行、
单任务失败不影响下一轮。到期判定与配置校验见
[`tasks/schedule.py`](src/kedaibiao/tasks/schedule.py)。

### Web 控制台

```bash
PYTHONPATH=src python3 -m kedaibiao web            # 默认绑 127.0.0.1:8765
```

> 📷 控制台 · 概览 —— 截图只在本机保留（含真实平台数据，不入库）：`docs/screenshots/ui-overview-pigai.png`

**左侧栏一列两段**：上半切平台，下半切该平台的功能。功能按任务命名 ——
概览 / 课程 / 作业 / 题库 / 工具 / 运行日志 / 统计 / 账号。所有数据按「当前平台」
过滤，另有「全部平台」聚合视图（跨平台对比只在「统计」页）。

「课程」「作业」「工具」会**真的登录并读取平台数据**（只读 GET），因此带「只读」
徽标与加载态；cookie 会落盘，同一次会话内后续访问会先探 `is_logged_in()`，
有效就跳过登录。

> 📷 控制台 · 作业 —— 本地截图：`docs/screenshots/ui-assignments.png`

默认只绑回环地址。要绑到别的地址（例如 Tailscale 网内）用
`--host <地址> --allow-host <主机名>`，启动时会打印警告 —— 能访问到该地址的设备
都能操作控制台（含作业提交等写操作）。

> ⚠️ **改了后端（`api/`、`platforms/`）必须重启服务**。前端是静态文件、刷新即生效，
> 但 Python 代码不会热加载 —— 新旧混跑会出现「页面是新的、接口还是旧的」这种怪状态
> （新路由 404、旧 bug 仍在）。实测踩过：服务从昨天 17:51 一直跑着，代码早已改完，
> 表现就是 pinco 计划 404 + keep-alive 那个 501 又冒出来。

#### 控制台 v2（新排版，就是 `/`）

新排版默认接在 `/`，原控制台保留在 `/classic`（重启服务后生效；重启前也可直接用
`/static/v1.html` 打开）：

```bash
PYTHONPATH=src python3 -m kedaibiao web
#  http://127.0.0.1:8765/          ← v2：平台切换在顶栏，详情进右侧检查器
#  http://127.0.0.1:8765/classic   ← v1：原控制台
#  http://127.0.0.1:8765/next      ← / 的别名，老书签不失效
```

> 📷 控制台 v2 · 概览 —— 本地截图：`docs/screenshots/v2/01-overview.png`

和 v1 的差别只有排版与交互，**接口完全共用**：

- 平台作用域升到顶栏药丸组，常驻可见；左轨只放功能，按「工作台 / 内容 / 分析 / 系统」分组；
- 每页统一为「页头（标题·作用域·主操作）→ 指标带 → 主列表 + 右侧上下文栏」；
- 顶栏右侧常驻「当前平台 + 当前账号」：多号时 `⇄` 直接换号（换号 = 换会话），
  账号 chip 点开能看每个平台账号的详情；
- 导航按平台真实能力裁剪：平台没声明的动作（如 iTEST 的考勤）就不出现对应页面，
  不做好看但点了必然失败的入口；
- 考勤按平台模型分流：东软是「场次列表 + 逐场签到」，希沃品课是「签到码 → 干跑解析 → 确认签到」；
- 「工具」页只做兜底：已经有专属页面的动作（刷课 / 考勤 / 签到）默认收起并指路，
  同一个操作不会有两个按钮；需要时一键切回全量；
- 详情一律进右侧检查器（作业详情支持上一条/下一条），说明文字收进页脚折叠块，
  表格只用于横向对比；
- 刷课（教材树 + 参数 + 日志流）与 iTEST 一键自动化都在作业页内就地展开，不再另开页面；
- 统一的 loading / empty / gate / error 四态；`⌘K` 命令面板；深色为主、可切浅色。

设计说明与扩展方式见 [docs/web-console-v2-layout.md](docs/web-console-v2-layout.md)。

> 📷 本地截图：运行日志（行式列表）、跨平台统计（`docs/screenshots/v2/04-logs.png`、`05-stats.png`）

> 📷 本地截图：账号（多号 + 大模型）、检查器（详情统一入口）（`06-accounts.png`、`12-inspector.png`）

> 📷 本地截图：刷课（教材树 + 完成度 + 参数 + 日志）、iTEST 一键自动化（`13-brush.png`、`15-itest.png`）

> 📷 本地截图：考勤 / 签到、签到前的定位校验（`32-attendance.png`、`33-sign-guard.png`）

> 📷 本地截图：批改网评分循环（提分）、品课（计划 → 复核 → 提交）（`40-pigai-grader.png`、`41-pinco-plan.png`）

**「计划」页**：没做完的功能在界面上单独标注，点开能看到「为什么没做、计划怎么做、
还差哪几步、有什么必须先确认的风险」——不让它们看起来像坏了。目前列了 5 项，
其中 U校园口语评测按当前决定**只做前端标注、不接评测链路**。

> 📷 计划页 —— 本地截图：`docs/screenshots/v2/45-roadmap-page.png`

改完前端想确认没写坏交互，跑自带的测试（都不碰真实平台账号）：

```bash
timeout 600 .venv/bin/python -m pytest -q            # 2075 单测（约 30 秒，离线）
python3 scripts/api_matrix_check.py                  # 后端路由/安全/并发 125 项（自起临时服务）
python3 scripts/ui_matrix_check.py  --base http://127.0.0.1:8765 --username admin --password '…'
python3 scripts/ui_smoke_next.py    --base http://127.0.0.1:8765 --username admin --password '…'
```

最近一次全面测试的结果与发现见 [docs/testing-report-2026-10-21.md](docs/testing-report-2026-10-21.md)
（报告写于 1722 单测时点，此后单测已增长到 2075）。

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

### 需要浏览器会话的平台（如 希沃品课）

有的平台业务接口要求**浏览器会话 cookie**（csrf 与浏览器上下文绑定，纯 HTTP
造不出来）。这类平台用 `capture` 从你自己的浏览器登录态里抓：

```bash
# 1) 用 Chrome 打开该平台并登录一次（过掉验证码/扫码）
# 2) 完全退出 Chrome
# 3) 用它的用户目录抓取（headless，不需要显示器）
PYTHONPATH=src python3 -m kedaibiao capture pinco --profile ~/.config/google-chrome

# 也可以手贴（浏览器 DevTools → Network → 任一 api 请求 → Cookie 头）
PYTHONPATH=src python3 -m kedaibiao capture pinco --cookie "csrfToken=…; x-token=…"
```

**抓完会真打一次只读接口验证** —— 因为未登录的登录页也会下发匿名 `csrfToken`，
只看「有没有这个 cookie」会把匿名会话当成登录成功。验证不过会删掉并报错。

> 抓取不接触密码、不碰验证码：用的是你已经登录过的浏览器用户目录。

#### 品课（pinco）已接入的只读动作

```bash
PYTHONPATH=src python3 -m kedaibiao actions pinco                  # 看动作清单
PYTHONPATH=src python3 -m kedaibiao action pinco connection        # 连接自检
PYTHONPATH=src python3 -m kedaibiao action pinco learning          # 学习任务/材料进度
PYTHONPATH=src python3 -m kedaibiao action pinco material course=… material_id=…
PYTHONPATH=src python3 -m kedaibiao action pinco answers assignment_id=…
```

四个动作都**只读**：`learning`（材料 + 进度）、`material`（材料详情/下载地址）、
`answers`（作业题目与作答槽）、`connection`（会话/健康检查/课程数）。

另有两个写操作（控制台「工具」页会显示「写操作」徽标）：

- `progress`（推进学习进度）：默认干跑，参数 `confirm=true` 才真的翻页；
  `material_id` 留空时处理整课未达标的材料（一次最多 `max_materials` 份，
  可重复执行续推）。批量推进仍用下面的脚本。
- `signin`（课堂签到）：把老师发的签到链接/签到码提交给考勤接口；默认干跑，
  `confirm=true` 才真发。见下面的「课堂签到」。

#### 品课「课堂签到」

老师发起课堂签到后，学生端入口是一条 H5 链接
`https://pinco.seewo.com/student/signin?attendanceCode=<码>`。学生端页面只做一件事：
把这个码当 `attendanceKey` 发给 `PostV1TeachingAttendanceAttend`。字段名与失败码
（7207 码无效 / 7208 已结束 / 10419 不匹配）已实测；**成功路径尚未用有效签到码
实测**（老师端二维码约 10 秒一换，只能接当次链接）。

```bash
# 干跑：只解析签到码，不发请求
PYTHONPATH=src python3 scripts/pinco_signin.py --code 'https://pinco.seewo.com/student/signin?attendanceCode=…'

# 真的签到（链接/码必须当次有效）
PYTHONPATH=src python3 scripts/pinco_signin.py --code '<签到链接或码>' --live
```

> ⚠️ 这是**写操作**（会记一次考勤签到），默认 dry-run。证据与未验证边界见
> [docs/verification/pinco-signin.md](docs/verification/pinco-signin.md)。
> 控制台动作：`action pinco signin code=… [confirm=true]`。

#### 品课「刷课」（按页推进学习进度）

机制已实测确认：**材料进度 = 看过的最大页 / 总页数**。把材料当课件打开
（`coursewareId` 就是材料号），逐页上报 `maxPageIndex` 即可推进；PPT 与 PDF
都适用。

```bash
# 默认 dry-run：只列材料与进度，不打开课件、不发任何写请求
PYTHONPATH=src python3 scripts/pinco_progress.py --course <courseId>
PYTHONPATH=src python3 scripts/pinco_progress.py --course <courseId> --material <materialId>

# 真推进必须显式 --live；--all 处理整门课；带页数/时长上限与「进度不动就停」
PYTHONPATH=src python3 scripts/pinco_progress.py --course <courseId> --material <materialId> --live
PYTHONPATH=src python3 scripts/pinco_progress.py --course <courseId> --all --live
```

> ⚠️ 这是**写操作**（会改平台上的学习进度），默认 dry-run。真实会话实测：单份
> 10 页 PPT 从 50% → 100%，整门课 15 份材料（9~387 页）全部推到 100%。证据、
> 阴性结果（学习时长上报不涨进度）与复现见
> [docs/verification/pinco-progress.md](docs/verification/pinco-progress.md)。

#### 东软智慧教育（neusoft）已接入的动作

```bash
PYTHONPATH=src python3 -m kedaibiao actions neusoft          # 动作清单
PYTHONPATH=src python3 -m kedaibiao action neusoft courses   # 课程（按学期聚合）
PYTHONPATH=src python3 -m kedaibiao action neusoft resources course=<课程号>
PYTHONPATH=src python3 -m kedaibiao action neusoft study course=<课程号> limit=1
PYTHONPATH=src python3 -m kedaibiao action neusoft attendance
PYTHONPATH=src python3 -m kedaibiao action neusoft sign attendance_id=<场次号>
```

- **登录**：走 App 接口（`/system/auth/app/login`），**平台侧没有图形验证码**，
  因此不涉及任何验证码对抗；租户从 `/system/tenant/app/option` 的「外网」条目解析。
- **刷课**：`startStudy` → 三段进度上报（`watchProgress=99999`）→ **回读**
  `getRecordInfo`，只有 `studyStatus == 2` 才算完成。接口 `code=0` 不算数。
- **作业**：学习任务是**跨课程的学期列表**（`study-task/page` 要 `termId`，
  传课程上下文会 403），`ctlId` 就是作答要的 `testId`。
- **交卷**：默认**只存草稿不交卷**（写路径只有 APK 静态证据），要交卷需设
  `KEDAIBIAO_NEUSOFT_ALLOW_SUBMIT=1`。
- **签到**：已签 / 已结束 / 需定位却无坐标，三条守卫挡在请求之前。
- 协议事实与实测记录见 [docs/research/neusoft.md](docs/research/neusoft.md)。

#### U校园（unipus）已接入的动作

```bash
PYTHONPATH=src python3 -m kedaibiao actions unipus        # 动作清单
PYTHONPATH=src python3 -m kedaibiao action unipus courses  # 班级×教材（带真实进度）
PYTHONPATH=src python3 -m kedaibiao action unipus assignments course=<课程号>
# 题目 + 服务端标准答案（U校园的作答就是转发服务端答案，先看清楚要交什么）
PYTHONPATH=src python3 -m kedaibiao action unipus answers \
    assignment_id='course-v2:…/<groupId>'
```

- **登录**：纯 HTTP 三步；被风控要求验证码（`1506`）时自动退回浏览器求解
  （与 iTEST 共用极验链路）。`jwt` cookie 还在时可以直接复原门户会话。
- **提交**：`capabilities.submit` 已打开，但**默认干跑**——只读内容与标准答案、
  构造并校验载荷（子题数对齐、`thirdPartyJudges` 等长），不发写请求。控制台
  「提交」点确认后才会真发；CLI 用 `kedaibiao run unipus --live`。
- **学习时长**：`action unipus duration seconds=600` 默认只出计划，`confirm=true`
  才建立 engine.io 会话；`kick=true` 才会发记账报文（形状未实测，默认关）。
- 证据边界（哪些实测、哪些没有）见
  [docs/verification/unipus-readonly.md](docs/verification/unipus-readonly.md)、
  [unipus-submit.md](docs/verification/unipus-submit.md)、
  [unipus-duration.md](docs/verification/unipus-duration.md)、
  [unipus-speech.md](docs/verification/unipus-speech.md)。

#### 作业页按教材分组（含完成进度）

作业页不再是一张 2908 行的平表：**先按教材分组**，每本书一行，显示
「书名 · 任务数 · 完成 18/18 + 进度条」，点书名才展开该书的单元与任务；
个人教材这类没有班级进度的独立项单独成行。门户的 `finishPointNum/totalPointNum`
就是这里的「完成进度」。

#### U校园「刷课」（控制台，已并入作业页）

导航 **刷课** → 教材列表（先只显示书）→「打开」一本书 → 单元与任务树 + 进度/必修数：

- **刷时长**：任务模式 WebSocket（`/unipusio/`，每 5 秒报一次 `start`，按真实任务页
  URL 归属到分组），默认干跑；
- **提交未完成**：只提交**没做过**的任务组（已做过的不重交 —— 平台留存首条记录，
  实测重交不改分）；每行的「提交」与作业页共用同一条 API；
  组间默认停 20 秒、单轮默认最多 60 组，**运行中可点「停止任务」**；
  遇限流或连续无权限会自己停手；
- **只处理必修**：按班级策略里的必修任务（门户进度就是按它算的），默认勾选。

- **翻译/写作**：服务端没有标准答案。可以勾选「**用大模型作答**」——按题面语言
  生成译文/短文再提交（默认关；生成内容会写进日志可复查；主观点提交后需等老师批阅
  才出分）。也可以只交占位文本拿完成分。

- **成绩构成**：门户把课程成绩分成「必修学习时长 50% + 教程学习成绩（任务点）50%」，
  刷课面板里直接显示这两项（当前账号时长 `00:00:00`、任务点 `0/204`）。

> ✅ 刷时长**已实测能记账**：单房间不记，**多房间并发**才有（6 房间 150 秒记 509 秒；
> 控制台 2 分钟记 239 秒）。但那是**账号级**时长，门户「必修学习时长」按教材归集，
> 两者还没打通（门户仍 `00:00:00`），详见
> [docs/verification/unipus-brush.md](docs/verification/unipus-brush.md)。

#### U校园「一键全部提交」（控制台）

作业页顶部按钮 → 面板里填「最多处理任务数 / 组间间隔 / 跳过已满分」：

1. **先干跑**：只读内容与标准答案，列出「可提交 / 跳过 / 被拦（子题数不一致等）」；
   「跳过」只认**逐项得分全 1**（平台 `score_pct` 不是正确率，实测十项对五项也回 1）；
2. 看完清单再点 **确认全部真交**：逐组提交并回读 `progress/v2`，日志实时滚动；
   任务在后台跑，切页不中断，同一时刻只允许一个后台任务。

对应接口 `POST /api/platforms/unipus/batch-submit`（见
[docs/web-console-api.md](docs/web-console-api.md)），默认 `confirm=false`。

> ⚠️ 提交 / 时长 / 语音评测都属**写操作**，一律默认干跑；语音评测的报文形状
> 大部分未实测，只有模块与闸门，没有控制台入口。

#### 超星 / 学习通（只读 + 浏览器自动化）

超星的登录要扫码/验证码，框架**不代过**。这里有三条入口，按「安全性」排序：

1. **只读**（控制台 / 脚本，HTTP）：课程、作业、考试、截止时间。适配器的
   `fetch_questions` / `submit` / `course_playback` 三个能力全是 `False`，
   流水线对应阶段直接跳过，不是「跑起来才拒绝」；
2. **浏览器自动化 · 刷课**：任务点树 → 逐个播放视频（2 倍速、静音）、读文档，
   进度由播放器**自己上报**，完成判定回读 `isPassed` 与章节树；
3. **浏览器自动化 · 刷题**：章节测验 / 作业 / 考试题面 → 字体混淆解码
   （`font-cxsecret`，字形比对，已实测 34 个伪字全中）→ LLM / 题库 / 规则作答
   → 逐题填卷 → 逐题回读核验 → **默认干跑，显式确认才交卷**。

原理、真实 DOM 与实测记录见
[docs/research/chaoxing-web.md](docs/research/chaoxing-web.md)。

**已接入主控制台**：顶栏平台切换里就有「超星学习通」，账号用**手机号 + 密码**
在「账号」页绑定一次（自动过登录页，会话落盘复用）：

- **课程**：6 门课，只读；
- **作业页 · 刷课**：按任务点播放视频 / 阅读文档，默认干跑先看计划，
  勾选确认后才真播；完成判定回读平台记录；
- **作业页 · 就地作答**：每一行都有「做考试 / 做测验」按钮，**不跳工具页** ——
  点开就在本页展开面板：考试信息（开始/截止时间、考试时长、**剩余提交次数**）、
  逐题题目信息（题号/题型/答案/置信度/来源/题目）、实时日志、交卷后的分数横幅。
  填课程号加载时还会自动补齐列表里的「开始」「剩余次数」两列
  （章节测验读 `#limitWorkSubmitTimes`/`#addTimes`；考试读须知页）；
  **可重复作答的场次**会显示「可重考」并给「重考」按钮（一行里挂着
  `重考` 的 `goTest` 就是入口），已完成也能再进；
- **工具页**：同样的能力也以平台动作形式提供（脚本/自动化用）。

> 📷 控制台里的学习通 —— 本地截图：`docs/screenshots/chaoxing/console-tab.png`

> 📷 作业页里就地作答（题目信息 + 日志）—— 本地截图：`docs/screenshots/chaoxing/console-chaoxing-done.png`

##### 自动化台（本地网页，推荐）

```bash
# 1) 用账号密码登录一次，会话落到 data/sessions/chaoxing.storage_state.json
python3 scripts/chaoxing_auto.py login --user 手机号

# 2) 起自动化台（只绑 127.0.0.1）
python3 scripts/chaoxing_studio.py
#   → http://127.0.0.1:8900/
```

页面左边选课程，右边四个页签：

- **任务点 / 刷课**：列出视频/文档任务点与完成状态，点「开始刷课」；可限制
  最多任务点数、倍速（上限 2x）、文档停留秒数。视频逐个播完，日志实时滚动；
- **章节测验**：「干跑填卷」只填卷并逐题回读核验、不提交；「填卷并交卷」才真交；
- **作业 / 考试**：列出考试/作业（待做/已完成/已过期/只允许手机 APP），
  网页端可做的可直接干跑或交卷；**交卷后显示平台回显的分数**
  （本次成绩/最终成绩），平台不公布时如实说明；
- **运行日志**：勾选顶栏「前台显示」，会显示浏览器当前画面（视频播放、答题页
  都能看到）和实时日志。

> 📷 学习通自动化台 —— 本地截图：`docs/screenshots/chaoxing/studio.png`

> 📷 考试列表与交卷 —— 本地截图：`docs/screenshots/chaoxing/studio-exam.png`

**滑块验证**：考试入口的风控滑块（`captcha.chaoxing.com`）默认**自动求解**
（拼图轮廓比对求缺口 → 逐帧拖拽）。作答面板里可以切换成**人工过滑块**：

- 自动（默认）：先自动解，解不开再交人工；
- 人工：遇到滑块就暂停，等你在可见窗口拖完点「继续」；
- 也可以用环境变量设默认值：`KEDAIBIAO_CHAXING_CAPTCHA=human`。

连续两次自动解不过会自动停下（避免把账号打进更严的风控）。

**人工认证**：只有视频/考试**真的**要求人脸时（隐藏域开关为 1 才算；考试须知模板里
的隐藏人脸 DOM 不算），任务会暂停，页面弹出「我已完成认证，继续」。

headless 服务器上完成不了，需要在有显示/摄像头的本机用 `--show` 跑：

```bash
python3 scripts/chaoxing_studio.py --show
```

在窗口里拖完滑块 / 完成人脸，再点「继续」。

##### 题面走 OCR（默认）

学习通题面有字体混淆 + 公式图片，所以章节测验/考试的题面**默认截图后交给视觉模型**
（每题截一张图，选项字母仍按 DOM 点选），截图失败才退回字体解码。关闭用
`KEDAIBIAO_CHAXING_OCR=0`。

##### 公式截图题

题面里的公式是图片，纯文本模型答不了。刷题链路会把题干/选项图片交给
`VisionAnswerer`（OpenAI 多模态格式，需要可读图的模型；可用
`KEDAIBIAO_LLM_VISION_MODEL` 单独指定，UA 用 `KEDAIBIAO_LLM_USER_AGENT`）。
模型不可用时降级规则答题器，报告里如实标低置信度。

> 📷 学习通只读看板 —— 本地截图：`docs/screenshots/chaoxing/overview.png`

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

### 仓库里不会出现的东西（隐私边界）

仓库要公开发布，所以**真实账号数据一律不进版本库**：

| 类别 | 处理 |
| --- | --- |
| 凭据 / 会话 | `.env`、cookie、`storage_state*.json`、`data/`、数据库 —— 全部 `.gitignore` |
| 截图 | `docs/screenshots/` 整目录忽略：截图里有真实课程名、作业号、账号信息，README 只留文字位置 |
| 平台抓包 | `.har`、`docs/research/_raw/` 忽略；进仓库的 `tests/fixtures/` 是**脱敏后**的样本（账号 uid / personId / 页面令牌已换成 `1000000xx` 假值） |
| 文档里的真实值 | 姓名 / 学校 / 班级 / 作业号换成占位或假值；真实值只登记在本机 `.secret-denylist`（该文件不入库） |

三道闸，提交前自动/手动都能跑：

```bash
make check-secrets                                     # 查暂存区；pre-commit 钩子会自动跑
python3 scripts/check_secrets.py --all                 # 查全部已跟踪文件
.venv/bin/python -m pytest -q tests/test_privacy.py     # 路径 + 账号号段 + 本机 deny 列表
```

> **历史也要干净**：本仓库发布前用 `git filter-repo --replace-text` 清过一遍，
> 校验命令与边界见 [docs/security.md §6](docs/security.md#6-发布与历史清理)。

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
│   ├── verification/       每个平台「实测到哪一步」的验收记录（含未验证边界）
│   └── research/           平台与依赖的调研报告（含证据来源）
├── src/kedaibiao/
│   ├── runtime.py          运行时装配：配置·凭据·存储·传输 → 流水线
│   ├── __main__.py         CLI
│   ├── core/               模型 · 注册表 · 事件总线 · 调度器
│   ├── platforms/          平台适配器（base.py 是契约）
│   │   └── unipus/geetest/vendor/  MIT 原样 vendor 的极验求解 JS + NOTICE
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

## 7. 开源项目与第三方代码

### 7.1 政策（硬门槛）

**引用任何开源项目之前，先读它的 LICENSE、把结论写进
[docs/license-policy.md](docs/license-policy.md)，再动代码。**

本项目以 **AGPL-3.0-only** 发布，因此：

- ✅ 可并入 / 可依赖：MIT、BSD-2/3、ISC、Apache-2.0、LGPL、GPL-3.0
  （须保留其声明并满足署名要求）；
- ❌ 不可并入：带附加限制的许可 —— 「禁止商用」「仅限非商业」「衍生作品须同等条款」，
  AGPL 第 7 条不允许在其之上再加限制；
- ❌ 不可并入：无 LICENSE、无 SPDX 标识符、许可无法核验的代码；GPL-2.0-only 与
  AGPL-3.0 不兼容，同样不可并入；
- ❌ 不复制他人的代码、注释、文档文本与命名 —— 借鉴思路可以，搬运表达不行。

### 7.2 已并入仓库的第三方代码（vendor）

| 项目 | 许可 | 并入方式 | 归属声明 |
| --- | --- | --- | --- |
| [`sijiyo/projects`](https://github.com/sijiyo/projects) | MIT ✅ | **原样 vendor** `geetest3_slide/jiyan.js`：计算极验 v3 的 `get_w` / `get_encode_trace`。反混淆 JS 逐行翻译极易出错，故不改逻辑，只在文件头加说明 | [`vendor/NOTICE`](src/kedaibiao/platforms/unipus/geetest/vendor/NOTICE) + [`LICENSE.sijiyo-projects`](src/kedaibiao/platforms/unipus/geetest/vendor/LICENSE.sijiyo-projects) |
| `driver.js` | AGPL-3.0-only | 本项目原创：把 `jiyan.js` 包成「stdin JSON → stdout JSON」，供 Python 侧 subprocess 调用 | 同上 |

> MIT 与 AGPL-3.0 兼容，前提是**保留版权声明与许可文本** —— 上面这两份文件就是为此存在的。
> 改 vendor 内容必须同步更新 `NOTICE`；完整清单见
> [`vendor/README.md`](src/kedaibiao/platforms/unipus/geetest/vendor/README.md)。

### 7.3 运行时依赖（全部可选）

核心层（`core/`、`bank/`、`net/`、`config/`）**只用标准库**：`pyproject.toml` 的
`dependencies = []`，什么都不装也能导入并跑全量单测。平台能力按需装：

| 依赖 | 许可 | 用途 | 安装 |
| --- | --- | --- | --- |
| [Playwright](https://github.com/microsoft/playwright) + Chromium | Apache-2.0 | 浏览器型平台的会话与自动化（超星、iTEST、品课）、极验登录链路 | `make install-browser` |
| [fontTools](https://github.com/fonttools/fonttools) | MIT | 学习通 `font-cxsecret` 字体混淆解码（读 cmap 做字形比对） | `pip install -e ".[chaoxing]"` |
| [Pillow](https://github.com/python-pillow/Pillow) | MIT-CMU | 字形渲染比对、极验缺口定位（原方案用 OpenCV，这里用 Pillow 顶替） | 同上 |
| [faster-whisper](https://github.com/SYSTRAN/faster-whisper)（含 CTranslate2） | MIT | iTEST 听力题本地转写，CPU 可跑 | `pip install -e ".[stt]"` |
| [pytest](https://github.com/pytest-dev/pytest) · [ruff](https://github.com/astral-sh/ruff) · [mypy](https://github.com/python/mypy) | MIT | 开发与 CI | `make install-dev` |

**预留未启用**：`pyproject.toml` 里的 `api`（FastAPI / uvicorn）与 `db`（SQLAlchemy）两组
extra 目前没用到 —— 控制台是标准库 `http.server`，存储是 `sqlite3`。

**外部程序**：极验 `w` 由 vendored `jiyan.js` 计算，需要本机有 **Node.js**（MIT）。
只在登录时用到；没装则只有「极验滑块登录」不可用，其余功能不受影响。

**外部服务**：大模型走任意 **OpenAI 兼容** HTTP 接口（`tasks/llm.py` 标准库实现，
不依赖官方 SDK）；地址与模型在控制台「账号 → 大模型」或 `.env` 里配，不可用时降级规则答题器。

### 7.4 只读参考、评估后不采用

| 项目 | 许可 | 结论 |
| --- | --- | --- |
| [`vsmutok/PuzzleCaptchaSolver`](https://github.com/vsmutok/PuzzleCaptchaSolver) | MIT | 只**参考**缺口定位思路（其 OpenCV 实现未复制，改用 Pillow 重写） |
| [`xKiian/GeekedTest`](https://github.com/xKiian/GeekedTest) | MIT | 只支持极验 v4，与本项目面对的 v3 场景不适用 |
| [`DaKMaple/captcha_verify`](https://github.com/DaKMaple/captcha_verify) | **无协议** ❌ | 不可用：无授权即默认保留全部权利 |
| [`whatsBeeHive/pigaiwang-toolkit`](https://github.com/whatsBeeHive/pigaiwang-toolkit) | 未评估 | 仅阅读其油猴脚本理解批改网页面结构，未复制、未并入 |
| [`Yunlk/uai-core`](https://github.com/Yunlk/uai-core) | 自定义非商业协议 ❌ | 不可引用，只可借鉴思路：禁商用 + 衍生作品须同等条款，且是 U校园专用 Rust CLI，与本项目平台零重合。详见 [调研报告](docs/research/uai-core.md) |

评估口径（七问清单）与决策流程见 [docs/license-policy.md](docs/license-policy.md)；
候选项目的原始对比表见 [docs/geetest-v3.md](docs/geetest-v3.md#3-开源方案已核协议)。

### 7.5 合规边界

极验是**商业反机器人产品**。vendored 求解器只服务于使用者本人账号的自动登录，
不用于批量注册、撞库、薅羊毛或任何未经授权的第三方账号操作 —— 见
[`vendor/NOTICE`](src/kedaibiao/platforms/unipus/geetest/vendor/NOTICE) 的「合规边界」一节。

### 7.6 致谢

感谢这些项目（按用途排序）：**Python 标准库与 CPython**（承载整个核心层）、
**Playwright**（浏览器自动化）、**fontTools** 与 **Pillow**（字形与图像）、
**faster-whisper / CTranslate2**（及其上游 OpenAI Whisper，本地语音转写）、
**sijiyo/projects**（极验 v3 求解）、**Node.js**、**SQLite**，
以及 **pytest / ruff / mypy** 工具链。许可与归属以上表为准。

> 各教学平台的协议事实来自本项目自己的抓包、实测与回读，不来自任何第三方 SDK 或
> 未授权的代码搬运；证据出处逐条写在 `docs/research/` 与 `docs/verification/`。

---

## 8. 路线图

- [x] **M0 骨架**：分层、模型、调度、题库、流水线、运行时装配、CLI、单测
- [x] **M1 首个真实平台：U校园（只读链路已用真实账号跑通）**
  - [x] U校园适配器只读链路：登录 → 拉课程 → 拉任务 → 拉题目
        (`src/kedaibiao/platforms/unipus/`，离线测试全绿)
  - [x] 传输层、凭据、运行时装配、`kedaibiao run` 入口
  - [x] **真实账号联调**（2026-10-20）：10 门班级×教材、264 个任务、
        题干/选项/标准答案全部真实可读；证据与复现见
        [docs/verification/unipus-readonly.md](docs/verification/unipus-readonly.md)
  - [x] 规格里两处待实测确认：目录容器键名（递归 `role=unit/group`）、
        三套成功码（SSO `"0"` / 门户 `1` / ucontent `0`）
  - [x] 鉴权常量轮换问题已定位并修复（旧参考实现的 annotator 常量已失效，
        症状是「内容/目录能读、进度/答案 401」）
- [ ] **M2 作答闭环**：题型解析、题库回填、提交与判分校验
  - [x] 内容/标准答案解密与子题对齐（U1/U2 部分解决）
  - [x] U校园提交链路：载荷语义已用**一组已满分任务**真实提交验证（`code=0`、
        得分率 1.0、回读 `finish=True`）；其余题型（口语 / 角色扮演 / 上传 / 纯学习组）未实测
  - [x] 东软：刷资源后回读 `studyStatus` 判定完成；交卷默认只存草稿，
        `KEDAIBIAO_NEUSOFT_ALLOW_SUBMIT=1` 才真交
  - [x] iTEST：取卷 → 大模型生成答案单 → 逐项填卷核验 → 交卷需把确认串写成「交卷」
  - [x] 批改网：提交编码形态已由真实探测确认（单次百分号编码）
  - [x] 超星：字体混淆解码 + 视觉模型答题 + 逐题回读，默认干跑
  - [x] 东软排版（2026-10-22）：图片资源跳过且不占 `limit`（单独记 `unsupported`）；
        课程 / 学习任务 / 考勤按 `total` + 短页翻页，服务端忽略 `pageNo` 时安全停止
  - [ ] 品课真提交：抓包工具已备好（`scripts/pinco_submit_capture.py`，抓取即中止、
        不会真的交），抓到一次真实报文并核对字段后再开 `submit_payload_confirmed`
  - [ ] 剩余：口语评测（U校园）、上传题、纯学习组 —— 只有模块与闸门，线上行为未实测
- [x] **M3 调度与观测**：Web 控制台（v2 `/` + v1 `/classic`，接口共用）、
      通知渠道（console / webhook，Server 酱 · 钉钉 · 飞书 · Bark 都能套）、执行留痕与题库
  - [x] 定时跑批（2026-10-22）：`[[schedule.jobs]]` + `kedaibiao schedule`，
        web 启动时自动拉起、退出自动停；默认关闭、默认干跑、任务串行
  - [ ] 失败重试面板：重试策略已在 `core/scheduler.py`，缺可视化
- [x] **M4 多平台**：适配器契约已被 **6 个平台**验证（U校园、东软、超星、iTEST、批改网、
      希沃品课），新增平台不需要改核心调度

**M1 的验收标准是「只读跑通」** —— 在没有任何一次写操作的前提下，把某个平台的
课程与作业清单完整拉下来并落库。

> U校园的写入能力（提交 / 学习时长 / 语音评测）都是**默认干跑**：模块与闸门已经
> 落地，但线上行为未实测，打开前请先读对应的 `docs/verification/unipus-*.md`。

---

## 9. 免责声明

完整的警示见文首「免责声明（使用前必读，违者后果自负）」，这里只留三条底线：

1. **仅供学习与技术研究 —— 对作弊、代考、代刷课、代写作业、刷分刷时长、操作他人账号、
   商业代做代刷以及任何违法用途零容忍，一律禁止；**
2. 平台服务条款通常禁止自动化访问：**账号封禁、成绩作废、学分清零、学籍处分乃至法律责任，
   全部由使用者自行承担**；
3. 代码按「现状」提供、不提供任何担保，**作者不对使用本项目产生的任何后果负责**。

框架默认 `dry_run=True` 正是为此 —— 先看清楚它打算做什么，再决定是否放手。
**使用即视为同意；如不同意上述任一条，请立即停止使用并删除本项目。**

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

接入新平台时，请先过一遍 [docs/license-policy.md](docs/license-policy.md) 的决策流程；
第三方代码的实际归属声明在
[vendor/NOTICE](src/kedaibiao/platforms/unipus/geetest/vendor/NOTICE)，
完整依赖清单与许可见 [§7 开源项目与第三方代码](#7-开源项目与第三方代码)。

> 想改用 `AGPL-3.0-or-later`（允许未来升到 AGPL-4.0）：把 `pyproject.toml` 的
> `license` 改成 `"AGPL-3.0-or-later"` 即可，其余不变。
