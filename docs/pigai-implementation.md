# 批改网（pigai）实现文档：逻辑、接口与踩过的坑

> 面向：后续维护者 / 想照着接下一个平台的人。
> 协议证据见 [pigai-write-protocol.md](research/pigai-write-protocol.md)，
> 读接口调研见 [pigai.md](research/pigai.md)。
> 本文只讲**我们的实现怎么组织的、为什么这么做、哪里会翻车**。

---

## 1. 一张图看懂全链路

```
                          ┌─────────────────────────────┐
  账号（cookie 文件） ────→ │ pigai/client.py  只读请求层  │ ← 拒绝非 GET（_check_read_only_path）
                          └─────────────────────────────┘
                                    │
        ┌───────────────────────────┼─────────────────────────────┐
        ▼                           ▼                             ▼
  list_pending_assignments   fetch_essay_detail            submit()  ← 唯一的写操作
  list_finished_essays        （分数/维度/批注）              │
        │                           │                         ▼
        │                           │                  grade_loop.py
        │                           │                  ┌──────────────────┐
        │                           └─────────────────→│ 1. 提交           │
        │                                              │ 2. 触发分析+轮询  │
        └──────────────────────────────────────────────│ 3. 取分/批注      │
                                                       │ 4. 未达标→改写→再交│
                                                       └──────────────────┘
                                                                │
                                                     api/service.py + server.py
                                                                │
                                                          控制台 / CLI
```

---

## 2. 三个关键机制

### 2.1 提交：编码形态是「语义单次」

**这是整个项目里最容易错、错了最贵的一处。**

批改网的正文要做 URL 编码，但到底编几层？

| 客户端做法 | 服务端存下的内容 | 结论 |
| --- | --- | --- |
| `quote` 一次 + `urlencode` | `kedaibiao-probe 空格 中文 测试` | ✅ 正确 |
| `quote` 两次 + `urlencode` | `kedaibiao-probe%20%E7%A9%BA%E6%A0%BC…` | ❌ `%20` 成字面量 |

真实机制：**服务端解码两次**。所以线上报文是 `%25E4…` 形态（两层的 `%`），
但客户端语义上只编码一次。

实现：`submit.py::encode_field(double=False)`，默认单次；`double_encode=True`
只为复现那次探测保留。

> 当初静态分析的推断是「二次编码」。如果照它实现，**交上去的每篇作文都会是
> `%20` 乱码，而且只有交完才会发现**。这是「写操作不许猜」最硬的论据。

### 2.2 出分：必须主动触发分析并轮询

提交后平台**不会自动给分**。学生端 JS 会做：

```
POST ?c=ajax&a=processStartIcomet  {eid}        → 返回 {s:"start", key, sleep}
POST ?c=ajax&a=process              {key, i}     → 返回 {s:"start|ok", sleep}
     ↑ 按服务端回的 sleep 轮询，直到 s == "ok"
```

实现见 `grading.py::start_analysis` / `poll_analysis`。
**不要自己定轮询间隔** —— 服务端每次会给 `sleep`（毫秒），按它等就好。

### 2.3 「未截止」≠ 没有分（2026-10-19 实测纠正）

原来以为：列表里写「未截止」= 平台不给分。**实测是错的。**

同一篇作文（rid `…112`，eid `…354`）：

| 来源 | 看到的东西 |
| --- | --- |
| 已提交列表 `list_finished_essays()` | `score_raw = "未截止"`（提交次数 3） |
| **详情页 `fetch_essay_detail()`** | **83 分 / 4 个维度 / 21 条逐句批注 / 总评** |

也就是说老师开了「提前批改」时，列表还没放分，详情页早就有分有评价了；
这类作业通常也**允许反复提交修改**。

所以：

- `fetch_snapshot()` **一律先读详情页**，读不到才按列表状态当「还没出分」；
- `wait_for_grade()` 不再因为「未截止」直接返回，避免把已有分数漏掉；
- `grade_loop` **先取分、展示分，再决定要不要重交**：分够了直接停，一次都不提交。

`grade_loop` 的停止原因：

| 原因 | 含义 | 该怎么做 |
| --- | --- | --- |
| `reached` | 达到目标分 | 成功（旧稿已达标也不会再提交） |
| `exhausted` | 轮次用完仍未达标 | 调大 `max_attempts` 或降低目标 |
| `timeout` | 等批改出分超时（稿子已交） | 「继续等待 N 秒」或「手动获取」，**不用重交** |
| `unavailable` | 详情页确实还没分 | 手动获取 / 继续等待 / 确认可改后「直接改写重交」 |

### 2.3.1 日志里必须能看到「分 + 评价 + 作文 + 改写提示词」

控制台的执行日志（作业页「详细日志」/ 详情抽屉里那块终端）现在会记录：

- 取分：分数、满分、四维得分率、总评、逐条批注、作文正文；
- 改写：**完整提示词**（含上面这些依据）与**改写后的正文**；
- 提交：稿件号与平台返回。

实现要点：`_plog_long()` 按行切分、超长行折行，单条不截断成一行 ——
否则作文正文在日志里只剩开头一小截，等于没记。日志条数上限 400 行/平台。

### 2.4 老作文的详情页是中转页（2026-10-19 实测）

较早的作文，`a=view&eid=…` 返回的只是一个 JS 跳转壳（1.6KB）：

```html
<div id="redirect">
  <div id="redirect_msg">待分析</div>
  <script>var url="/index.php?c=v2&a=view3&eid=…";</script>
```

真详情在 `a=view3`。适配器现在会识别 `id="redirect"` 并跟着跳一次。

跳过去之后页面有正文、但没有我们认识的分数锚点（分数是页面里 JS 填的）。
这种情况下**分数回落到「已提交列表」**（那里明明有 `87.5` 这样的数字），
快照里带 `detail_error` 说明「只拿到分数、没有四维和批注」——
绝不能再显示成「平台未给分」，用户会看到自相矛盾的提示。

`resubmit_without_score=true` 是给「确实没分但学生想继续改」的口子：
只有界面上点「直接改写重交」（带二次确认）才会传，默认关闭。

---

## 3. 接口清单

### 3.1 适配器层（`platforms/pigai/adapter.py`）

| 方法 | 性质 | 说明 |
| --- | --- | --- |
| `login(credentials)` | 写（登录） | cookie 落盘，后续跨进程复用 |
| `is_logged_in()` | 只读 | |
| `list_pending_assignments()` | 只读 | 待办作文 |
| `list_finished_essays()` | 只读 | 已提交列表（**含分数**；「未截止」不代表没分，见 §2.3） |
| `fetch_essay_detail(rid, eid)` | 只读 | 分数 / 四维度 / 总评 / 逐条批注 / 正文 |
| `fetch_assignment_by_rid(rid)` | 只读 | 写作页（题目要求、字数区间、满分） |
| **`submit(assignment, answers)`** | **写（不可逆）** | 提交作文；正文取 `Answer.text` |
| `capabilities.submit` | — | `True`（编码已实测确认） |

### 3.2 评分与循环（`grading.py` / `grade_loop.py`）

| 单元 | 说明 |
| --- | --- |
| `EssaySnapshot` | 一次评分的完整快照：分数、维度、总评、批注、正文、`raw_state` |
| `wait_for_grade(...)` | 触发分析 → 轮询 → 读分（已出分则直接读，不重复触发） |
| `GradeLoop.run(...)` | 提交 → 等分 → 记录 → 未达标改写 → 再交，直到达标/用尽/不可得 |
| `build_rewrite_prompt(...)` | 把分数 + 弱项维度 + 批注拼成改写指令 |

### 3.3 HTTP（控制台）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/essays/grade-loop` | 启动循环 `{platform, assignment_id, target_score, max_attempts, initial_essay?}` → 返回 job |
| `GET` | `/api/essays/jobs/<id>` | 轮询：`events[]`（每步日志）+ `attempts[]`（每轮分数与批注） |
| `POST` | `/api/essays/fetch` | 手动取分 `{rid, eid, trigger_analysis?}`，**不提交任何东西** |
| `POST` | `/api/essays/history` | 某作业的历史轮次（分数/批注/正文） |

---

## 4. 数据模型

`essay_attempts` 表（`bank/store.py`）：

| 列 | 说明 |
| --- | --- |
| `owner_id` | 归属用户（隔离用，与其它表一致） |
| `rid` / `eid` | 作业号 / 稿号 |
| `attempt` | 第几轮 |
| `score` / `full_marks` / `graded` | 分数；`graded=0` 表示这一轮没出分 |
| `raw_state` | 平台原始状态（`未截止` / `待分析`） |
| `essay_text` | 这一轮提交的正文 |
| `feedback` / `dimensions` / `overall` | 批注、维度得分率、总评 |

**为什么存正文**：界面上要能点开看「这一轮交的是什么」，否则没法和分数对应。

---

## 5. 守护线

### 5.1 写面白名单

`tests/test_pigai_adapter.py::TestNoWriteSurface` 用 AST 扫全包的可执行字符串：

- 写端点**只能**出现在 `submit.py`，且**只能**是 `a=postSave`；
- 显式禁止 `saveCaogao` / `ketang` / `htbt` / `delete` / `submit` 等其它写端点混入；
- `a=write` 是**写作页的 GET 地址**，不是写接口。

> 原先这条测试是「一个写端点都不许有」。提交落地后改成白名单而不是删掉 ——
> 每多一个写端点，就多一处不可逆操作的风险面。

### 5.2 闸门

`PigaiAdapter.submit_payload_confirmed`（默认 `True`）：置 `False` 时
`submit()` 立刻抛 `NotSupportedError`，**一个请求都不发**。

### 5.3 两道提交门槛（服务层）

即使 `confirm=true`，也拦：

1. 一题都没答出来 → 400；
2. 最低置信度低于 `min_confidence` → 400（**宁可交白卷也不交错答案**）。

---

## 6. 踩过的坑（按代价排序）

| # | 坑 | 代价 | 怎么发现的 |
| --- | --- | --- | --- |
| 1 | 正文编码层数猜错 | 交上去全是 `%20` 乱码 | 草稿端点探测 + 只读接口读回比对 |
| 2 | `client` 是 property 却被当方法调 | **请求已发出**，本地 TypeError，上层会误判成没交而重试 → 重复提交 | 第一次真实提交 |
| 3 | `SubmissionResult` 漏导入 | 同上（NameError 在 POST 之后） | 同上 |
| 4 | 未截止时分数读成空 | 会误判成 0 分并无限重交 | 真实提交后看到「未截止」 |
| 5 | 提交后不触发分析 | 作文永远停在「待分析」 | 对比历史已出分作文 |

**第 2、3 条的教训**：写操作的方法本体必须被测试。只测纯函数构造器，
抓不到「请求发出去了但结果构造失败」这种半成功状态。已补接线测试
（`tests/test_pigai_submit.py::TestAdapterSubmit`）。

---

## 7. 给下一个平台的移植清单

接新平台时按这个顺序做，能把上面大部分坑提前踩掉：

1. **只读先行**：列表 → 详情 → 提交页（拿页面变量）；
2. **写面白名单测试**：先写「一个写端点都不许有」，落地时再改成白名单；
3. **编码用探测法确认**：找一个**低风险写端点**（草稿/预览）存一条带
   「空格 + 中文 + 标记」的内容，再用只读接口读回比对。别信静态推断；
4. **写操作的方法本体必须有测试**（假传输层，断言请求次数与 body）；
5. **区分「不可得」与「失败」**：未截止 / 待分析 / 超时都不该触发重试；
6. **幂等**：提交前查平台侧状态，提交后记 `eid`，避免重复交。
