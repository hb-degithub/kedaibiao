# 批改网（pigai）适配器 —— 接入交接说明

> **写给谁看**：后续负责「统一接入前端控制（CLI / Web 控制台）」的人，以及需要判断
> 这个适配器能不能上线的人。
> **规格与证据**：[docs/research/pigai.md](research/pigai.md)（700 行，含每个端点的实测证据与未验证清单）
> **独立验证**：[docs/verification/pigai-stage3.md](verification/pigai-stage3.md)（对抗性复核，阻断级缺陷 0）

---

## 0. 一句话状态

**只读能力完整可用，写入不在范围内。**

| 用法 | 现在能用吗 |
| --- | --- |
| `scripts/pigai_read.py`（独立脚本） | ✅ **现在就能用**（四条命令线上实测通过） |
| 适配器 Python API（`PigaiAdapter`） | ✅ 可用 |
| 框架 CLI（`kedaibiao run pigai`） | ⚠️ **能跑但没用** —— 见 §3.1 |
| 框架 CLI 的只读子命令 | ❌ **尚未接线**，见 §3.2 |
| 写入（提交作文 / 改稿重交） | ❌ **明确不做**（决策，见 §5） |

---

## 1. 交付物清单（接入前请核对 md5）

| 文件 | 行数 | md5 | 作者 |
| --- | --- | --- | --- |
| `src/kedaibiao/platforms/pigai/protocol.py` | 483 | `f29f7f506c640640b7c8172203eb3cab` | lead |
| `src/kedaibiao/platforms/pigai/rsa_login.py` | 171 | `c8b23f41ea4e4109e5f6cda64c70f0b7` | client-dev |
| `src/kedaibiao/platforms/pigai/client.py` | 317 | `d791255c040b6312b8850668726523cd` | client-dev |
| `src/kedaibiao/platforms/pigai/parse.py` | 1135 | `ec961a7072379cc31fca86c3adc5ebae` | parser-dev |
| `src/kedaibiao/platforms/pigai/adapter.py` | 319 | `6034a531e8c764b35077ffcecd62bbb3` | lead |
| `src/kedaibiao/platforms/pigai/__init__.py` | 33 | `d087d025d44cdcabfbeca8f5e3e91919` | lead |
| `tests/test_pigai_login.py` | 659 | `d734dd7634795b29ec6ddc75c2b0f2de` | client-dev |
| `tests/test_pigai_parse.py` | 808 | `61b0e03225136d841d7f2c4a553ff376` | parser-dev |
| `tests/test_pigai_adapter.py` | 459 | `dc9fc06dd9de06f57eb2a78e12bf2d95` | lead |
| `tests/fixtures/pigai/` | 7 个 | 见目录（`md5sum tests/fixtures/pigai/*.html`） | parser-dev |
| `scripts/pigai_read.py` | 186 | `03ed5bbaadf71843767e964dfe7a7bcc` | lead |
| `scripts/pigai_probe.py` | 312 | `033e95b71aaa6a8ebbf09ec938cc0beb` | lead |

> **冻结时间：2026-10-19 12:10（Asia/Shanghai，第 3 版）。**
> 演进：v1 11:45（A/B/C 新能力）→ v2 12:05（修 verifier 的需知会-5/-6）→ v3 12:10（修需知会-7）。
> 三个测试模块全绿：**login 41 / parse 79 / adapter 34 = 154**。
> 后续任何增量修改都会让 md5 变化 —— **一律以 `md5sum` 实测为准**。
>
> **验证状态**：独立验证者做了 **4 轮**对抗性复核，最终结论 **「✅ 可以定稿」**，
> 全程**无阻断级缺陷**。报告 `docs/verification/pigai-stage3.md`（1163 行），
> §0 钉了四轮的 md5，谁改了什么、哪一版验过什么都可回溯。
>
> **已知且已接受的限制**（verifier §13.8，**不建议**为它再开一轮）：`_absolute` 的站外判定
> 比较 `urlsplit().netloc`，**大小写敏感** —— `https://WWW.PIGAI.ORG/…`、显式 `:443`、
> userinfo 伪装 这类「语义同源但写法特殊」的 href 会返回 `''`。方向**保守**
> （只让展示 url 变空，绝不放行站外），且真实页面这两列 href **恒为相对形态**
> （已核 11 份 raw + 3 份写页样本）。

---

## 2. 能力矩阵

| 能力 | 状态 | 入口 | 说明 |
| --- | --- | --- | --- |
| 登录（RSA-1024 / PKCS#1 v1.5） | ✅ | `PigaiAdapter.login(creds)` | 纯标准库，**已过真机验证**；不需要 Playwright |
| 会话探测 | ✅ | `is_logged_in()` | 正面判据（页面含 `id="essayList"`），**不看 cookie** |
| 未完成作业（含截止时间） | ✅ | `list_pending_assignments()` | 7 列实测 |
| 已提交作文（成绩 / 稿次） | ✅ | `list_finished_essays()` | 8 列实测 |
| 分数 / 四维 / 总评 | ✅ | `fetch_essay_detail(rid, eid)` | 全 SSR，无 XHR |
| **作文正文** | ✅ | 同上 → `EssayDetail.essay_text` | §6.3；注意大写 `<TEXTAREA>` 的坑 |
| 逐句批注 | ✅ | 同上 → `EssayDetail.annotations` | 24 条实测；四类 level 齐全 |
| **按作业号定位** | ✅ | `fetch_assignment_by_rid(rid)` | §6.1；GET 无副作用 |
| **读回当前稿子** | ✅ | 同上 → `WritePage.essay_text` | 「未截止可继续修改」的**只读一半** |
| 课程列表 | ⚠️ 合成 | `list_courses()` | 批改网无课程维度，返回单一合成课程 |
| 取题 / 提交 | ❌ | —— | `Capabilities` 里为 `False`，基类抛 `NotSupportedError` |

---

## 3. 接入点

### 3.1 不要用 `kedaibiao run pigai` 读数据

`tasks.pipeline.Pipeline` 是为「取题 → 作答 → 提交」写的。对只读适配器它**不会报错但毫无用处**：
待完成作业没有题目可取 → `AbortedBeforeSubmit("没有取到题目")` → 每条都记 SKIPPED，
你在输出里看不到任何作业、分数或批注。

**顺带一个安全结论**：`run pigai --live` **也不会造成写操作** —— `submit` 能力为 `False`，
且 `_solve_and_submit` 在取到 0 题时就中止了，**永远走不到 `adapter.submit()`**。

### 3.2 推荐接法：把四条只读命令接进 CLI / Web 控制台

`scripts/pigai_read.py` 里已经写好完整的取数与渲染逻辑，语义如下：

| 命令 | 适配器调用 | 建议的 CLI 形态 | 建议的 Web 形态 |
| --- | --- | --- | --- |
| `pending` | `list_pending_assignments()` | `kedaibiao pigai pending` | 「待办作业」卡片 |
| `finished` | `list_finished_essays()` | `kedaibiao pigai finished` | 「历史作文」表格 |
| `show <rid> <eid>` | `fetch_essay_detail(rid, eid)` | `kedaibiao pigai show <rid> <eid>` | 作文详情页（分数+四维+正文+批注） |
| `lookup <作业号>` | `fetch_assignment_by_rid(rid)` | `kedaibiao pigai lookup <rid>` | 「输入作业号」入口 |

`show` / `lookup` 会打印**作文正文**（本人作文）。Web 形态请注意这是个人内容。

### 3.3 适配器公开 API（编程调用）

```python
PigaiAdapter(AdapterContext(http=HttpClient(UrllibTransport())))

async def login(credentials: Credentials) -> bool
async def is_logged_in() -> bool
async def close() -> None

async def list_pending_assignments() -> list[Assignment]
async def list_finished_essays()    -> list[protocol.EssayRow]
async def fetch_essay_detail(rid: str, eid: str) -> protocol.EssayDetail
async def fetch_assignment_by_rid(rid: str) -> protocol.WritePage

# PlatformAdapter 契约（供将来统一调度）
async def list_courses() -> list[Course]                       # 单一合成课程
async def list_assignments(course: Course) -> list[Assignment] # 未完成 + 已完成，按 rid 去重
```

**`remote_id` 约定（上线后不可随便改，integration-guide §4）**：
`Assignment.remote_id` = **`rid`（作文号）**，不是 `eid`。`rid` 跨多稿稳定，是
`ProgressStore.already_done()` 的幂等键；`eid` 只对已提交的作文存在，放 `Assignment.raw`。

### 3.4 注册：**不需要改 `pyproject.toml`**

`platforms/__init__.py` 的 `load_builtin_adapters()` 会**自动发现**子包里的
`PlatformAdapter` 具体子类并按 `cls.name` 注册。实测 `kedaibiao platforms` 已列出
`pigai / pinco / unipus` 三个平台。**共享区零改动。**

### 3.5 前端控制台：**已经接线了**（由 api 窗口完成，Lead 已独立验证）

`src/kedaibiao/api/actions.py` 里已有 `_register_pigai()`，把四条只读命令登记成控制台动作：

| 动作 | 处理器 | 调用的适配器方法 |
| --- | --- | --- |
| `pending` | `_pigai_pending` | `list_pending_assignments()` |
| `finished` | `_pigai_finished` | `list_finished_essays()` |
| `essay` | `_pigai_essay` | `fetch_essay_detail(rid, eid)` |
| `lookup` | `_pigai_lookup` | `fetch_assignment_by_rid(rid)` |

**Lead 的独立验证（离线，FakeTransport + `tests/fixtures/pigai/`）**：四个处理器全部执行
成功，字段与 `protocol` 契约逐一对上（含 `essay_text`、`Dimension.source`、
`WritePage.word_range/server_time/no_paste`）；`pending` 出 1 行、`finished` 出 3 行、
`essay` / `lookup` 返回 detail 信封。**接线无需改动。**

⚠ **交接时发现一处未修的、用户可见的缺陷**（在 `actions.py`，属 api 窗口，
按并行纪律 Lead **未越界修改**）：`essay` 动作的**两个参数提示写反了**：

```python
ActionParam("rid", "作文号", hint="例如 200000001")   # 200000001 实为 eid（9 位）
ActionParam("eid", "稿件号", hint="例如 1000002")     # 1000002 实为 rid（7 位）
```

实测口径：**`rid` 是 7 位作文号**（如 `1000002`），**`eid` 是 9 位稿件号**（如 `200000001`）。
照着提示填会**填反**，导致查不到或查错。另 `lookup.rid` 的 `hint` 为空，建议补示例。
改动只是两个字符串，请转交该窗口。

---

## 4. ⚠ 一个必须由框架侧修的阻塞（影响所有平台）

> **✅ 已于 `007b2dd` 修复（主窗口）。** 修法采用你建议的第 2 条思路的显式版本：
> 新增 `config.resolve_environ()` 算出「本次运行认哪些环境变量」，配置与凭据
> 共用同一份，由 `Runtime` / `ConsoleService` 显式传给 `load_credentials`。
> **没有**回写 `os.environ` —— 全局副作用会污染测试，也让「这次读了哪些变量」
> 不可追踪。
>
> 实测：`.env` 写入凭据后 `kedaibiao doctor` 正确显示「已配置 …」；
> `kedaibiao action pigai pending` 直接拉到真实待办（只读）。
> **`scripts/pigai_read.py` 里那句显式 `load_dotenv` 现在多余了，可以删。**
>
> 另外你的 §3.2 建议已采纳并做成通用机制：见
> [web-console-api.md §3](web-console-api.md) 的「平台只读动作」——
> 批改网四个动作已能在 CLI（`kedaibiao actions/action`）与 Web 控制台上用，
> 且**不需要前端为 pigai 写专属页面**。

### 4.1 `.env` 里的凭据读不到

**症状**（实测）：

```
.env 里 3 个平台的凭据都有值
kedaibiao doctor    → 凭据：（未配置任何平台凭据，见 .env.example）
kedaibiao run pigai → ✗ 未找到 pigai 的凭据
```

**根因**（两条链路没有接上）：

- `config/settings.py` 的 `load_settings()` 读 `.env` 后只放进**局部字典**
  `environ = {**file_env, **os.environ}`，**从不回写 `os.environ`**；
- `security/credentials.py` 的 `load_credentials()` / `available_platforms()`
  在无参调用时只读 `dict(os.environ)`。

后果：**整合指南 §5 承诺的优先级（`… < .env < 真实环境变量`）没有兑现，任何平台都无法从 CLI 取到凭据。**

**建议修法**（共享区，需框架侧决定；二选一）：
1. `load_settings()` 里改用 `dotenv.load_dotenv()`（它**会**写 `os.environ`）；
2. 或让 `Runtime` 把 `load_settings` 用到的 `environ` 传给 `load_credentials(platform, environ=…)`。

**当前绕过**：`scripts/pigai_read.py` 显式调用 `load_dotenv(ROOT/".env")` 补上这一跳。
**这不改变「凭据只从环境变量/`.env` 读」的约束**，只是补上缺失的那一跳。

---

## 5. 纪律与红线（请勿在接入时放宽）

1. **只读**。适配器只发 GET + 一次登录 POST；源码里**没有任何写入端点字面量**，
   由 `tests/test_pigai_adapter.py::TestNoWriteSurface` 用 **AST 扫全包可执行字符串**钉住。
2. **不利用前端开关**。写作页的 `_no_paste="1"`（禁止粘贴）、时间限制都是前端变量，
   接口层或许不校验 —— **刻意不利用**（`WritePage.no_paste` 只读取、只展示）。
3. **不破解验证码**、不伪造时间戳、不硬撞截止时间。
4. **凭据只走 `.env` / 环境变量**，不打印、不落盘明文；cookie 文件 mode 600。
5. **写入功能（提交/改稿重交）未做，且是明确的范围决策**。
   用户 2026-10-19 拍板：「先只补三个只读缺口」。
   若将来要开，必须先定三件事：稿件来源（工具不生成内容？）、稿次幂等键、
   以及 §6 里「改稿重交会进老师可见的修改历史」的合规判断。

---

## 6. 未验证清单（**不得据此行动**）

技术项见 [pigai.md §8](research/pigai.md)（10 条）。接入侧相关的：

| # | 未验证项 | 说明 |
| --- | --- | --- |
| H1 | 会话有效期 | 实测为 session cookie，未知何时失效 |
| H2 | `cookie` 的 `HttpOnly` / `Secure` 标志 | 未读响应头属性 |
| H3 | 站点频率限制阈值 | 全程主动限速，未压测 |
| H4 | 「登录着但服务端不渲染列表容器」的其它变体 | 正面判据只在 7 份真实样本上验证过。若真机出现「明明登录着却判未登录」，症状是**每次跑批多一次登录 POST** —— 把那页样本给过来即可修 |
| H5 | 断网时 CLI 的报错形态对用户是否清晰 | `is_logged_in()` 网络层异常会抛 `TransportError`（快速失败，不重发登录） |
| H6 | 多稿历史页 `?c=write&a=history&eid=` | 已取到样本，**无解析器** |
| H7 | 班级页完整 DOM | `parse_class_rows` 有实现但适配器未接线（`list_courses` 走合成课程） |
| H8 | `fb_js` 批注字典的内容语义 | 只核对了条目数=17，未逐条核对语义 |

---

## 7. 验收命令与期望输出

```bash
# 1) 全量测试（当前基线：728 tests OK）
cd /home/hb/刷题刷课 && PYTHONPATH=src python3 -m unittest discover -s tests -t .

# 2) pigai 三件套
PYTHONPATH=src python3 -m unittest tests.test_pigai_login tests.test_pigai_parse tests.test_pigai_adapter

# 3) 平台已注册（应为 3 个）
PYTHONPATH=src python3 -m kedaibiao platforms

# 4) 只读入口（四条，均为线上实测通过）
python3 scripts/pigai_read.py pending
python3 scripts/pigai_read.py finished
python3 scripts/pigai_read.py show <rid> <eid>
python3 scripts/pigai_read.py lookup <作业号>
```

实测样例输出（2026-10-19）：

```
未完成作业 1 条
  [1000001] 某课程 · 某单元
        截止 2026-11-16 23:59 剩 28 天   出题人 张老师
        指定班级 34 个

已提交作文 10 条
  [1000002] eid=200000001  成绩=未截止  稿次=2  提交=2026-10-16
  [1000003] eid=200000002  成绩=89  稿次=2  提交=2026-09-09

第 1000003 号作文（eid=200000002）
  总分 89.0 / 100.0
    词汇     0.9526   (来源 title)
    句子     0.7784   (来源 title)
    篇章结构   0.9061   (来源 title)
    内容相关   0.9128   (来源 title)
  ── 正文（1516 字符）──
  ── 按句点评 24 条 ──

作业号 1000002（eid=200000001）
  字数：100~200   满分：100.0
  截止：2026-10-19 23:59   服务器时间：2026-10-19 11:31:10
  已有稿子：是，1070 字符
```
