# 架构说明

## 1. 设计目标与取舍

| 目标 | 做法 | 代价 |
| --- | --- | --- |
| 新增平台只写适配器 | `PlatformAdapter` 契约 + 注册表 + entry point | 契约要先定对，后面改契约等于全平台返工 |
| 核心可离线测试 | 核心层零第三方依赖 | 不能用 pydantic 做配置校验，自己写 `tomllib` 加载 |
| 写操作可撤销 | 默认 `dry_run=True`，置信度门槛 | 首次使用需要显式改配置，略麻烦 |
| 题库越用越准 | 只有平台判分过的答案才标 `correct` | 短期命中率低，需要多轮积累 |
| 不把对方站点打崩 | 全局并发闸门 + 单平台限速 + 退避重试 | 跑批更慢 |

核心判断：**这个项目最大的风险不是性能，是「悄悄干了错事」**（交了错答案、
重复交卷、账号被封）。所以所有默认值都偏向保守。

---

## 2. 分层与依赖方向

```
core  ←  bank  ←  tasks  ←  api / __main__
  ↑                 ↑
platforms ──────────┘
  ↑
browser
```

依赖只能单向向上。`core` 不知道任何平台的存在；`platforms` 不知道流水线的存在。

### 为什么 `core` 必须零依赖

`core` 是被所有层 import 的地基。如果它依赖 pydantic / httpx / playwright，
那么"跑一遍单测"就变成"装一堆东西"，骨架的迭代速度会直接塌掉。
现在 `make test` 在裸 Python 3.11+ 上 0.3 秒跑完 115 项测试。

### 为什么 `Registry` 写成泛型

`core/registry.py` 里是 `Registry[T]` 而不是 `Registry[PlatformAdapter]`。
否则 `core` 就得 import `platforms.base`，而 `platforms.base` 又 import `core.models`
—— 循环导入。用泛型把类型参数留给使用方，依赖方向就干净了。

---

## 3. 数据流

### 3.1 一次跑批

```
Pipeline.run(adapter, credentials)
  │
  ├─ adapter.refresh_session(creds)
  │     └─ is_logged_in()?  否 → login()      # 登录态来自 storage_state.json
  │
  ├─ _collect(adapter)
  │     └─ list_courses() → list_assignments(course) → 按 kinds 过滤
  │
  ├─ Scheduler.map(assignments, _process, key=平台名)
  │     ├─ 全局并发闸门（Semaphore）
  │     ├─ 单平台最小请求间隔（RateLimiter）
  │     └─ 指数退避 + full jitter 重试
  │
  └─ _process(adapter, assignment)          # 每个作业
        ├─ 幂等检查：平台 finished / 本地 already_done → SKIPPED
        ├─ store.start_run()                # 落一条 running 记录
        ├─ fetch_questions()
        ├─ answerer.answer(questions)       # 题库 → 规则 → （预留）模型
        ├─ 门槛检查：全部为空 / 最低置信度 < min_confidence → SKIPPED
        ├─ dry_run? → SKIPPED（带上完整的提交计划）
        ├─ adapter.submit()
        ├─ store.record_submission()
        ├─ bank.upsert(..., correct=<平台判分>)   # 质量闭环
        └─ store.finish_run() + 发事件
```

### 3.2 事件

`EventBus` 解耦生产者与消费者。已定义：

| 事件 | 触发点 | 典型消费者 |
| --- | --- | --- |
| `run.start` | 作业开始处理 | 日志、Web 控制台 |
| `run.finish` / `assignment.done` | 作业处理结束（含失败） | 通知、进度面板 |
| `answer.low_confidence` | 置信度不足被拦下 | 人工复核队列 |

单个订阅者抛错只记日志，不影响广播 —— 通知渠道炸了不该让跑批失败。

---

## 4. 关键机制

### 4.1 题目指纹与题库质量闭环

`models.question_fingerprint()`：题干 + 选项各自归一化（NFKC、去标点、去空白、转小写）
后拼接哈希。归一化后选项**排序**再入哈希，所以选项顺序抖动不影响命中。

```
题干 "下列哪项，正确？"  +  ["甲","乙"]
题干 "下列哪项正确"      +  ["乙","甲"]     →  同一个 fingerprint
```

`correct` 是三态的：`1` 对 / `0` 错 / `NULL` 未经平台确认。
`upsert` 的合并规则是**有判分背书的旧答案不能被未验证的新答案覆盖** ——
这是防止题库被污染的核心一条，有专门的单测锁住。

`_extract_verdicts()` 只认一种约定格式（`raw["questions"] = [{id, correct}]`），
不匹配就返回空字典。宁可少回填，也不能瞎标对错。

> ⚠️ 归一化算法是题库的主键。**上线后修改它等于全库失配，必须配套迁移。**

### 4.2 幂等

`ProgressStore.already_done(platform, assignment_id)` 查本地是否已有成功记录。
重复交卷在多数平台上等同于作弊信号，所以这是硬保护而非优化。

同一个作业如果上次失败，允许重跑；成功过就不再碰。

### 4.3 并发与限速

`Scheduler` 三件套：

- **并发闸门**：`asyncio.Semaphore(max_concurrency)`，全局生效；
- **限速器**：按 key（通常平台名）保证最小请求间隔，各 key 独立；
- **重试**：只对 `TimeoutError / ConnectionError / OSError` 重试。
  `ValueError` 这类业务错误立刻上抛 —— 重试没有意义，只会放大问题。

退避用 full jitter（`random.uniform(0, base * 2^(n-1))`），避免整批任务
在同一时刻集体重试形成尖峰。

`Scheduler.map()` 刻意不用 `gather` 的默认行为：它把每个元素的异常**就地捕获**
放进结果列表，一个作业挂掉不会带走整批。

### 4.4 配置优先级

```
内置默认值 < config/default.toml < config/local.toml < 环境变量 KEDAIBIAO_*
```

`Settings.with_env()` 必须是**实例方法**：早期写成 `from_env` classmethod 直接
`cls(**kwargs)`，会把调用方从 TOML 合并来的值整个丢掉 —— 这个 bug 由
`tests/test_settings.py::test_toml_then_env_priority` 抓出来。

---

## 5. 扩展点

| 扩展点 | 位置 | 注册方式 |
| --- | --- | --- |
| 平台适配器 | `platforms/<平台>/` | entry point `kedaibiao.adapters` |
| 答题器 | `tasks/answerer.py` 新增子类 | `kedaibiao.answerers` 注册表 |
| 通知渠道 | `notify/base.py` 新增子类 | `kedaibiao.notifiers` 注册表 |
| 验证码处理 | `browser/session.py` 的 `CaptchaHandler` 协议 | 构造 `BrowserSession` 时传入 |

`Registry.load_entry_points()` 对单个插件的 import 失败是**吞掉并继续**的：
一个坏插件不该让整个框架起不来。

### 预留但尚未实现

- **LLM 答题器**：接口位在 `answerers` 注册表，未写实现。需要先评估成本、
  准确率与平台题型覆盖，不猜着写。
- **`api/` Web 控制面**：目录已建，FastAPI 是可选依赖，尚未接线。
- **`course_playback` 能力**：`Capabilities` 里有开关，没有实现。

---

## 6. 测试策略

| 层 | 测试 | 依赖 |
| --- | --- | --- |
| `core.models` | 归一化与指纹的行为锁 | 无 |
| `core.registry` | 注册/覆盖/坏插件容错 | 无 |
| `core.scheduler` | 重试次数、非重试异常、并发上限、限速间隔、错误隔离 | 无 |
| `bank.store` | 增删查、模糊命中、判分保护、导入导出、文件持久化 | 无 |
| `tasks.answerer` | 规则答题、链式降级、部分降级只送未答的 | 无 |
| `tasks.pipeline` | 干跑不写、真提交、幂等跳过、置信度拦截、事件 | 无（`FakeAdapter`） |
| `config.settings` | 优先级、类型转换、未知键拒绝 | 无 |

**适配器必须能在不联网的前提下被验证**：把网络层抽成可替换的 client，
用 `FakeAdapter` 的模式测。做不到这一点的适配器等于不可维护。

---

## 7. 已知待办

- 东软系具体平台清单未落地：`platforms/neusoft/` 目前只有包占位。
  接入前需要先做一次平台调研（登录协议、题型、端点），产出放进 `docs/research/`。
- 本项目许可已定为 `AGPL-3.0-only`（见 `LICENSE` 与
  [license-policy.md](license-policy.md)）。**接入任何第三方代码前必须先过
  兼容性检查** —— AGPL 第 7 条禁止附加限制，非商业/同等条款类协议一律不可并入。
