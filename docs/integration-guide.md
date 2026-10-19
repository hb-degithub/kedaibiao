# 并行开发与整合指南

> 适用场景：多个窗口/会话**同时**在同一个工作区里各做一个平台适配器，
> 最后合并成一套。

当前工作区就是这种情况：本窗口做 U校园，另一个窗口做批改网。

---

## 1. 为什么需要这份文档

并行开发的失败模式不是「写错代码」，是**互相踩文件**：

- 两个窗口都去改 `platforms/__init__.py` → 后提交的把先提交的覆盖掉；
- 两个窗口各自发明一套凭据读取方式 → 整合时得重写；
- 有人把平台私有对象塞进 `core.models` → 核心被污染，第二个平台就接不进来。

下面把边界写死。

---

## 2. 文件归属（硬规则）

### 2.1 平台窗口**只准**写这两个地方

```
src/kedaibiao/platforms/<平台名>/     # 整个包，结构自定
tests/test_<平台名>.py                # 单测
```

### 2.2 以下文件属于**共享区，平台窗口一律不要改**

| 文件 | 为什么 |
| --- | --- |
| `src/kedaibiao/core/**` | 所有平台共用的地基，改错全体遭殃 |
| `src/kedaibiao/platforms/base.py` | 契约。要改必须由 lead 统一改，否则各窗口的契约会分叉 |
| `src/kedaibiao/platforms/__init__.py` | 注册入口，多人同时改必冲突 |
| `src/kedaibiao/net/**`、`browser/**` | 传输层，共用 |
| `src/kedaibiao/config/**`、`security/**` | 配置与凭据 |
| `pyproject.toml` | entry point 注册表 |
| `.env.example`、`.gitignore` | 全局约定 |
| `README.md`、`docs/architecture.md` | 全局文档 |

**需要框架配合改动时，告诉 lead 去改**，不要自己动手 —— 一个窗口改了
`base.py`，其他窗口就是对着旧契约写的，整合时必然返工。

### 2.3 提交纪律

并行工作时**只 `git add` 自己的文件**：

```bash
git add src/kedaibiao/platforms/<平台名>/ tests/test_<平台名>.py
git commit -m "feat(<平台名>): ..."
```

不要用 `git add -A` / `git add .` —— 那会把别人未完成的产物一起提交。

---

## 3. 适配器契约

### 3.1 必须实现的

```python
class XxxAdapter(PlatformAdapter):
    name = "xxx"                 # 机器名：全小写，用作 registry key、DB 字段、文件名前缀
    display_name = "某某平台"     # 中文展示名
    capabilities = Capabilities(...)

    async def login(self, credentials: Credentials) -> bool: ...
```

### 3.2 按能力声明实现其余钩子

```python
async def is_logged_in(self) -> bool: ...                      # 会话是否仍然有效
async def list_courses(self) -> list[Course]: ...
async def list_assignments(self, course: Course) -> list[Assignment]: ...
async def fetch_questions(self, assignment: Assignment) -> list[Question]: ...
async def submit(self, assignment: Assignment, answers: list[Answer]) -> SubmissionResult: ...
```

**没实现的能力必须在 `Capabilities` 里声明为 `False`** —— 基类会自动抛
`NotSupportedError`，比返回空列表安全得多（空列表会被误读成「这个平台没有作业」）。

### 3.3 两种传输方式

| 类型 | 用 `AdapterContext` 的 | 适用 |
| --- | --- | --- |
| 浏览器型 | `ctx.browser`（`BrowserSession`） | 重 JS、接口难直接调的平台 |
| 协议型 | `ctx.client()`（`HttpClient`） | 能直接打 HTTP 接口的平台 |

协议型适配器**不要**直接用 `urllib` / `requests` / `socket` —— 走
`HttpClient` 才能复用 cookie 持久化、统一的默认头与超时，也才能在单测里
替换成假传输层。

---

## 4. 数据翻译规则

`core.models` 是**跨平台的公共语言**。平台私有概念必须在这里被翻译掉：

| 规则 | 说明 |
| --- | --- |
| 平台原始结构进 `raw` | `Question.raw` / `Answer.raw` / `Course.raw` / `Assignment.raw` 都是给平台自用的 |
| 平台类型名不进 `QuestionType` | 映射到现有枚举；映射不了就 `UNKNOWN`，原始类型名放 `raw` |
| 平台 ID 必须填 `remote_id` | 题干不可得的平台**尤其重要**：没有 `remote_id` 的题目入不了库 |
| `remote_id` 要能唯一定位 | 只收到 `Assignment` 就要能调内容接口，所以必要时用复合键（如 `f"{instanceId}/{groupId}"`） |
| 19 位以上数字 ID 不要过浮点 | JSON 里当字符串处理，别被 `float` 吃掉精度 |

### 关于 `remote_id` 的一个重要提醒

`Assignment.remote_id` 同时是：

- `RunRecord.assignment_id`；
- `ProgressStore.already_done(platform, assignment_id)` 的**幂等键**。

一旦上线就不能随便改格式，否则历史记录全部对不上。请在适配器里把这个
约定写进 docstring。

---

## 5. 凭据

统一用 `kedaibiao.security.credentials`：

```python
from kedaibiao.security import load_credentials

creds = load_credentials("unipus")          # KEDAIBIAO_UNIPUS_USER / _PASS / _TOKEN
creds = load_credentials("unipus", require=False)   # 没配就返回 None
```

环境变量命名：`KEDAIBIAO_<平台大写>_USER` / `_PASS` / `_TOKEN` / `_SCHOOL` / `_TENANT` / `_OPENID`。

优先级：内置默认值 < `config/default.toml` < `config/local.toml` < `.env` < 真实环境变量。

**不要**自己读 `os.environ`，**不要**把凭据写进配置文件或日志。要打印就用
`kedaibiao.security.describe(creds)`（密码与令牌值已屏蔽）。

---

## 6. 测试要求

### 6.1 硬性要求

**适配器必须能在完全离线的环境下被验证。** 做不到这一点的适配器等于不可维护。

协议型：注入假 `Transport`（见 `tests/test_net.py` 的 `FakeTransport`）。
浏览器型：注入假的页面对象，或把页面操作抽成可替换的 client。

不要写「访问真实站点做冒烟测试」的测试 —— 它会在 CI 上失败，也会产生
真实的写操作。

### 6.2 至少覆盖

- 登录成功与失败两条路径；
- 会话/cookie/令牌的处理与续期；
- 课程、任务、题目的解析（用固定样本，不要用真实响应）；
- 题型映射；
- **异常响应不能被静默吞掉**：返回 HTML 登录页、非 JSON、业务错误码，
  都必须变成明确的异常，而不是空列表。

### 6.3 跑测试

```bash
cd /home/hb/刷题刷课 && PYTHONPATH=src python3 -m unittest discover -s tests -t .
```

全部必须绿，且不能拖垮已有用例的通过数。

---

## 7. 注册

适配器写完后，由 **lead** 统一完成注册（避免 `platforms/__init__.py` 冲突）：

```toml
# pyproject.toml
[project.entry-points."kedaibiao.adapters"]
unipus = "kedaibiao.platforms.unipus:UnipusAdapter"
```

或在 `platforms/__init__.py` 里手工注册。注册后 `make platforms` 能列出。

---

## 8. 整合检查清单

合并一个平台窗口的产出时，逐条过：

- [ ] `git diff` 只涉及 `platforms/<平台名>/` 与 `tests/test_<平台名>.py`
- [ ] 全量测试通过，且**没有删改别人的测试**来让它变绿
- [ ] 适配器没有任何一行 import 了另一个平台的模块
- [ ] `capabilities` 与实际实现一致（没实现的能力必须是 `False`）
- [ ] 网络请求全部走 `HttpClient` / `BrowserSession`，没有裸 `urllib`/`requests`
- [ ] 没有 `time.sleep`（限速交给 `Scheduler`）、没有 `print`（用 `self.log`）
- [ ] 凭据走 `security.load_credentials`，且**能用 `grep` 确认没有硬编码密码**
- [ ] 离线下测试全绿（断网跑一遍）
- [ ] `remote_id` 的约定写进了 docstring
- [ ] 平台私有概念没有泄漏进 `core.models`
- [ ] 第三方代码引用已过 `docs/license-policy.md` 的流程（本项目是 AGPL-3.0，
      非商业/附加限制类协议**不可并入**）

---

## 9. 常见错误

| 症状 | 原因 |
| --- | --- |
| 题目入库后互相覆盖 | 没填 `remote_id`，而无题干题目的指纹会退化 |
| 幂等失效，同一作业重复提交 | `Assignment.remote_id` 每次调用都变了（用了时间戳/序号） |
| 提交被拒但看不出原因 | 用一套 `is_ok` 判定所有接口；不同 API 家族成功码常常不统一 |
| CI 上偶发失败 | 测试访问了真实站点，或依赖了本地缓存的登录态 |
| 整合时契约对不上 | 某个窗口擅自改了 `platforms/base.py` |
| 别人的文件被误提交 | 用了 `git add -A` |
