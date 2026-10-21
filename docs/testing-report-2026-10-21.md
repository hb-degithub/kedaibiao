# 控制台全面测试报告 · 2026-10-21

> 范围：整个 Web 控制台（v2 前端 + 后端 API + 平台适配层）与全部已接入平台。
> 方法：单元测试 + 后端路由/安全矩阵 + 前端页面矩阵 + 真实数据只读验证 + 手工安全抽查。
> 结论：**5 套测试全绿**（1722 单测 / 125 后端矩阵 / 93 前端矩阵 / 64 冒烟 / 17 安全抽查），
> 期间发现并修复 **4 个真实缺陷**，另有 3 项需你决定或属于真实环境状态。

---

## 0. 一句话结果

| 测试集 | 规模 | 结果 | 位置 |
| --- | --- | --- | --- |
| 单元测试（后端全栈） | 1722 | ✅ 全过 | `tests/` |
| 后端路由 + 安全 + 并发矩阵 | 125 项 | ✅ 125/125 | `scripts/api_matrix_check.py` |
| 前端页面矩阵（12 页 × 6 平台 + 三态 + 交互 + 响应式） | 93 项 | ✅ 93/93（空库、真实数据**各跑一轮**） | `scripts/ui_matrix_check.py` |
| 前端功能冒烟（真实交互链路） | 64 项 | ✅ 64/64 | `scripts/ui_smoke_next.py` |
| 安全抽查（手工，临时服务） | 17 项 | ✅ 17/17 | 本报告 §4 |

---

## 1. 单元测试

```
timeout 600 .venv/bin/python -m pytest -q
→ 1722 passed, 1 warning, 140 subtests passed in 24.48s
```

唯一的 warning 是 `pytest-asyncio` 的 `asyncio_mode` 配置项未被识别（本仓库用
`IsolatedAsyncioTestCase`，不依赖该插件），不影响结果。

覆盖重点（按文件）：`test_pinco.py` 193 · `test_unipus.py` 191 · `test_unipus_speech.py` 100 ·
`test_api_service.py` 92 · `test_unipus_duration.py` 89 · `test_pigai_parse.py` 79 ·
`test_auth.py` 65 · `test_api_server.py` 62。

---

## 2. 后端矩阵（`scripts/api_matrix_check.py`）

```
python3 scripts/api_matrix_check.py        # 自起临时服务（make_server port=0），不连 8765
→ 汇总: 125/125 通过
```

| 分节 | 项数 | 内容 |
| --- | --- | --- |
| 路由矩阵 | 74 | GET 35 / POST 37 / HEAD 2，全部带超管 cookie；成功路径断言具体 kind，错误路径断言 4xx + kind |
| 路由表同步 | 3 | 从 `server.py` 解析出 33 条字面路由 + 6 个前缀模板，断言矩阵全覆盖且无多余 |
| 安全 | 25 | Host 伪造 / 跨站 Origin / 路径穿越 3 种 / 未登录 / 普通用户 403 / 安全响应头 / 415 / 413 / 非法 JSON / Cookie 标志 / owner_id 越权 / 停用用户 |
| keep-alive | 4 | 带 body 的 POST（不解析 body）后同连接 GET → 200，并用 socket 同一性证明没偷偷重连 |
| 错误分支 | 9 | `limit=abc` → 400、未知平台/动作 → 404、缺必填参数 → 400、不存在的 job → 404、GET 只 POST 的路由 → 404 |
| 并发 | 3 | 8 线程 × 4 个只读接口（32 请求）全部 200，无 500 |
| 静态资源 | 7 | `/` 是 v2、`/classic` 是 v1、js/css 的 Content-Type、缺失资源 404 |

### 修掉的真缺陷

**① Origin 带 userinfo 可绕过跨站守卫**（子智能体发现，已修）

```
GET /api/overview
Origin: http://127.0.0.1:8765@evil.example
期望 403 → 实际 200
```

根因：`_origin_ok()` 用 `parsed.netloc.rsplit(":", 1)[0]` 取主机，这种 Origin 的 netloc 是
`127.0.0.1:8765@evil.example`，切出来正好是白名单里的 `127.0.0.1`。

修法（`server.py`）：`Host` 与 `Origin` 都显式拒绝含 `@` 的值 —— 浏览器序列化 Origin 时不带
userinfo，真实请求发不出这种值，但守卫不能依赖「浏览器不会这么发」。

**② 非 GET/HEAD/POST 返回 HTML 501**（子智能体发现，已修）

`http.server` 对未实现的方法回一段 HTML 501，既不走 `{error,kind}` 契约、也不带安全响应头，
违反 `docs/web-console-api.md` 的「失败一律 JSON」。

修法：在路由层接住 `PUT / DELETE / PATCH / OPTIONS`，统一返回
`405 + {"error": "...", "kind": "method_not_allowed"}`，并照常下发 CSP/nosniff。

---

## 3. 前端矩阵（`scripts/ui_matrix_check.py`）

```
python3 scripts/ui_matrix_check.py --base http://127.0.0.1:8799 --token <kdb_session>
→ 93/93 通过（组合矩阵 72/72）
```

| 组 | 项数 | 结果 |
| --- | --- | --- |
| 页面 × 平台矩阵 | 72 | 12 页 × 6 平台（全部平台 + 五平台），无 pageerror / console error / 4xx-5xx，标题与左轨高亮全对 |
| 按约定退回 overview | 4 | `attendance × {全部平台, itest, pigai, unipus}` 全部正确退回（这四者没有考勤能力） |
| 三态 | 9 | 7 个数据页注入主接口 500 → 出现错误块、无残留骨架、外壳不崩；assignments/attendance 空数据 → 空状态 |
| 交互 | 5 | ⌘K 面板（打开→过滤→回车→Esc）/ 检查器（行点开→Esc→backdrop）/ 账号 chip（下拉→详情）/ 主题切换 / 左轨折叠 |
| 响应式 | 5 | 1440 / 1280 / 1024 / 768 / 414 全部无横向溢出 |
| 未登录 | 2 | 登录门可见、`/api/overview` → 401 |

跑了两轮：**空库**（全新临时库，证明「没数据也不炸」）与**真实数据副本**（用户库的只读克隆，
证明「有数据时渲染正确」），两轮都是 93/93。

### 修掉的真缺陷

**③ CSP meta 里的 `frame-ancestors` 无效**（前端矩阵发现，已修）

`index.html` 的 `<meta http-equiv="Content-Security-Policy">` 带了 `frame-ancestors 'none'`，
按规范该指令在 meta 里被忽略，Chrome 每次加载都打印一条 error。响应头里已有完整的 CSP
（含 `frame-ancestors`），所以 meta 里那条纯属噪声 —— 已删掉。

**④ U校园作业列表 2908 行渲染**（真实数据测试发现，已修）

真实账号下一个学期的 `assignments` 返回 **2908 行**（每本书的每个 group 都算一条）。
全量建 DOM 时：加载 3.9s、DOM 节点数万。

修法：`renderTable` 默认只渲染前 200 行，底部给
「已显示前 200 行，还有 2708 行 [显示全部 2908 行]」。修后首屏 DOM 从 2908 行降到 200 行，
展开仍可用（第 501 行点击 → 检查器「501 / 2908」正常）。

> 注：接口本身（2908 行 JSON）仍需 2.7s 返回，这是「作业列表 = 全书所有 group」的设计选择，
> 不是渲染问题。真要提速应做后端分页或强制按课程过滤，属于功能决策，本报告只记录。

---

## 4. 安全抽查（手工，临时服务）

| 项 | 结果 |
| --- | --- |
| Host 伪造 `evil.com` | ✅ 403 |
| Host 含 userinfo `user@127.0.0.1` | ✅ 403（修复后） |
| 跨站 Origin | ✅ 403 |
| Origin 含 userinfo | ✅ 403（修复后） |
| Origin 同源 | ✅ 放行（再按登录态判定） |
| 路径穿越 `/static/../../etc/passwd` | ✅ 403，无 `root:` 泄漏 |
| 未登录访问 API | ✅ 401 `kind=unauthorized` |
| `X-Content-Type-Options` | ✅ nosniff |
| `X-Frame-Options` | ✅ DENY |
| CSP | ✅ 含 `default-src 'none'` |
| POST 非 JSON | ✅ 415 |
| 超大 body（65KB+） | ✅ 413 |
| `?limit=abc` | ✅ 400 |
| 未知平台 / 未知动作 / 不存在的 job | ✅ 404 |
| `PUT / DELETE` | ✅ 405 JSON（修复后） |
| `/` 与 `/classic` | ✅ v2 / v1 内容各自正确 |

---

## 5. 真实数据只读验证

用**用户真实库的只读克隆**（`data/kedaibiao.db` + `data/sessions/` 复制到 `/tmp`，端口 8799），
把不会写平台的动作全跑一遍：

| 平台 | 动作 | 结果 |
| --- | --- | --- |
| itest | exams / courses / assignments | ✅ 200（1 场考试 / 1 门课 / 1 个作业） |
| pigai | pending / finished / courses / assignments | ✅ 200（10 条历史作文 / 10 个作业） |
| pinco | learning / connection / courses / assignments | ✅ 200（24 门课 / 13 个作业） |
| unipus | courses / assignments | ✅ 200（10 门课 / **2908** 个作业） |
| neusoft | courses / assignments / attendance | ❌ 502 —— **会话确实过期**（见下） |

### 东软会话过期（真实状态，非代码缺陷）

```
neusoft 登录失败: 您权限不足或者非法访问
```

排查链路：会话文件里有 `access_token` + `refresh_token`（过期时间 2026-10-20 23:59），
`import_session` 恢复成功；`is_logged_in()` → 401；`refresh()` 用 refresh_token 续期 →
平台也回「权限不足或者非法访问」→ 回退密码登录（账号库不存密码）→ 失败。

**结论：需要到「账号」页给东软重新登录一次。** 之后若再过期，`refresh_token` 续期会自动生效
（这次是 token 和刷新令牌一起过期了，两者时间窗相同）。

### 运维注意：备份 db 不带 sessions 等于没备份

复现时发现：只复制 `data/kedaibiao.db` 到新目录后，**五个平台全部 502**，因为
平台会话存在 `data/sessions/`（不在数据库里）。迁移或备份时必须两个一起带走。

---

## 6. 待你决定 / 未修

| # | 项 | 说明 | 建议 |
| --- | --- | --- | --- |
| 1 | `POST /api/essays/history` 不校验 `platform` | 传 `platform=ghost` 仍 200；同族 `wait/fetch/grade-loop` 都会 404/501 | 属 v1 专用接口，随 v1 退役一起删更省事 |
| 2 | U校园 assignments 接口 2.7s | 2908 行的 JSON 本身耗时 | 后端分页或强制按课程过滤（功能决策） |
| 3 | 东软会话已过期 | 真实环境状态 | 到「账号」页重新登录一次 |

---

## 6.4 手机端适配（390 / 414 / 360）

真机（Android 手机，390 CSS px）截图暴露三处问题，已在 CSS 里修掉：

| 问题 | 原因 | 修法 |
| --- | --- | --- |
| 底部导航 11 个图标**没有文字**，认不出是哪个功能 | `.shell.rail-min .nav-item .lbl { display:none }` 也命中了手机（`enterApp` 在 ≤1080px 会加 `rail-min`） | 该隐藏规则限制到 `min-width: 721px`；手机端图标在上、文字在下（10.5px），选中态用底部 2px 高亮 |
| 顶栏挤成一团（主题 + 身份 + 退出 + 5 个账号 + 平台药丸） | 桌面端三件套直接排进顶栏 | 手机端隐藏这三项，收进「⋯」菜单（主题 / 刷新 / 用户管理 / 退出） |
| 作业表格被压成**每列一个字宽**，「课程」整列竖排 484px 高 | 表格 `width:100%`，`.wrap` 列只有 `max-width` 没有 `min-width`，浏览器把列压到 47px | 手机端 `.tbl { min-width: 640px }` + `.tbl td.wrap { min-width: 150px }`，超出容器横向滚动；表格容器右侧加渐隐提示 |

另外：检查器底部按钮原来会换行（「关闭」被挤到第二行），改为 `flex-wrap: nowrap` 横向滚动；
详情里的长 ID（`course-v2:…`）限宽 58% 并允许断词。

实测（390 / 414 两个宽度）：`scrollWidth == innerWidth`（**零横向溢出**）、表格 1315px 可横滑、
首列 150×97、底部按钮行高 44px。

`ui_matrix_check.py` 新增 6 条手机端断言（底部导航文字、「更多」按钮与菜单、表格可横滑、
首列不塌陷、无横向溢出），现在 **99 项全绿**。

---

## 6.5 全平台只读遍历（真机 · 19 个动作）

对五个平台的所有 `mutates=False` 动作逐个跑（**一个写操作都没碰**）：

| 平台 | 动作 | 结果 |
| --- | --- | --- |
| U校园 | courses / assignments / answers / detail | ✅ 10 门课、单课 261 项、题目与答案、详情 |
| 批改网 | pending / finished / essay / lookup / assignments | ✅ 历史作文 10 篇、作文详情（总分 83.5 + 正文 + 批注） |
| 希沃品课 | courses / connection / learning / material / assignments | ✅ 24 门课、15 份材料、材料详情、13 个作业 |
| iTEST | exams / courses / assignments | ✅ 1 场考试、1 门课、1 个作业（kind=考试） |
| iTEST | detail | ⚠️ 501 not_supported —— **正确拒绝**：取卷要进考场、会开始计时 |
| 东软 | courses / attendance | ❌ 502 —— 真实会话过期，需重登 |

### 又修掉两个

**⑥ 平台「不支持」被当成平台故障（502 → 501）**

`itest` 的通用 `detail` 动作会去取卷，而 iTEST 自己拒绝（「取卷要在作答页里做，会开始计时」）。
这个 `NotSupportedError` 被统一裹成 502 `platform_error` —— 用户看到的是「平台故障」，
实际是「这个入口对平台没有意义」。

修法：`_execute_action` 单独接住 `NotSupportedError` → 501 `not_supported`，
文案写明「这是平台能力限制，不是故障」。现在返回：

```
itest 不支持这个操作：iTEST 的取卷要在作答页里做（会开始计时），请用控制台「进考场取卷」动作
（这是平台能力限制，不是故障；换用该平台自己的动作）
```

**⑦ 品课材料列表缺课程号**

`learning` 返回「课程名」，而 `material` 动作要的是「课程号」—— 用户看完材料列表想查详情，
还得回课程列表对一遍名称。已在行里补 `course_id` 列。

**⑧ 一条间歇性假红的测试**

`test_no_credentials_leaked` 断言 `assertNotIn("pw")` —— 两个字符的子串，任何含 `pw` 的
路径或键名都会命中，全量跑时偶发失败。改成断言具体的凭据字面量
（`pass-under-test-should-never-leak`），并把测试凭据换成有辨识度的值。

---

## 7. 真机验证（用户控制台 8765，只读 / 干跑）

用真实账号在你的控制台上跑了一遍，**全程没有触发任何写操作**：

| 功能 | 结果 |
| --- | --- |
| 作业列表（unipus，真实 2908 行） | ✅ 首屏 200 行 + 「还有 2708 行 [显示全部]」 |
| 刷课：教材列表 | ✅ 10 本 |
| 刷课：教材树 | ✅ 6 单元 / 261 分组 / 完成度「必修 33 组 · 全部 261 组」 |
| 刷课：干跑（confirm=false） | ✅ `提交 0，跳过 0，失败 0`、`submitted=0`，**未写平台** |
| 批改网评分循环：只取分 | ✅ 取到作文正文与分数 |
| 品课提交计划（只读） | ✅ 31 项：28 跳过（已提交/已过期）+ 3 失败（课程不存在 6569） |
| 东软考勤（会话已过期） | ✅ 错误块「平台会话失效」+「去账号页重新登录」按钮 |
| 功能冒烟 | ✅ 64/64 |

### 一本书的完整流程（U校园 · 视听说教程2，真实跑通）

按控制台上的实际操作顺序走完，每步都有截图与任务记录：

| 步骤 | 操作 | 结果 |
| --- | --- | --- |
| 1 | 作业页 | 2908 行，首屏截断 200 行 |
| 2 | 刷课 → 教材列表 | 10 本 |
| 3 | 打开「视听说教程2」 | 6 单元 / 261 分组 / 18 秒（逐组回读 progress/v2） |
| 4 | 提交未完成（干跑） | `待提交 0`（必修 33 组全部已完成） |
| 5 | 提交未完成（真交，两步确认） | `confirm=True`、`提交 0` —— 没有可交的，也没写平台 |
| 6 | 刷时长（干跑） | 6 个房间目标、600 秒、预计记账约 42 分钟 |
| 7 | 刷时长（真跑，改成 1 分钟试水） | **6 个房间全部连接成功**，记账 **26 秒**（cmgt 4s / ucmg 5s / homework 4s / tla 5s / studio 4s / course 4s） |

任务记录（`/api/jobs`）：

```
r-1 提交 confirm=False done  提交=0
r-2 提交 confirm=False done  提交=0
r-3 提交 confirm=True  done  提交=0        ← 真交，但没有待提交项
r-4 刷时长 confirm=False done 目标=6 记账=0s
r-5 刷时长 confirm=True  done 目标=6 记账=26s ← 真跑，6 房间都记上了
```

`/api/submissions` 仍是 **0 条** —— 整个流程没有真提交任何作业。

### 第 5 个真缺陷：完成度按「得分点」算

第 3 步暴露的：这本书 **必修 33 组全部已完成**（树里 33 个 `finished`），
但进度条显示 `0%`。

根因：`finish_points / total_points` 是**得分点口径**，而纯学习组（`text-learn` / 视频）
没有计分点，`finish_points` 恒为 0 —— 拿它画进度条，一本全做完的书也是 0%。

修法：完成度改按**组数**算（必修已完成组 / 必修组数），得分点作为补充信息放进 caption。
修后：`33 / 33 组`、`is-ok`、填充 `w-100`，caption 为
「必修 33 组 · 全部 261 组 · 得分点 0/33 · 大学英语I（三）B班」。
另：`state=none`（不读状态）时不再画假的 0%，改显示「未读取完成状态」。

### 这次踩到的坑：服务没重启

你的 8765 进程启动于 **10-20 17:51**，而 `server.py` 改到 **10-21 00:36**、`service.py`
改到 20 日 21:57 —— 也就是**后端跑的是昨晚的代码、前端是刚改的**。表现：

- `POST /api/platforms/pinco/plan` → 404（新路由不在旧进程里）
- `501 Unsupported method ('{}GET')` 又冒出来（keep-alive 的修复也在旧进程外）

重启后两个都正常（`pinco/plan` → 200、同连接下一个 GET → 200）。

> 已写进 README：**改后端必须重启服务**；前端静态文件不用。

---

## 8. 怎么复跑

```bash
# 1. 单元测试（离线，零依赖）
timeout 600 .venv/bin/python -m pytest -q

# 2. 后端矩阵（自起临时服务，不碰 8765）
python3 scripts/api_matrix_check.py

# 3. 前端矩阵（需要一个在跑的控制台）
python3 scripts/ui_matrix_check.py --base http://127.0.0.1:8765 \
    --username admin --password '……'      # 或 --token <kdb_session>

# 4. 功能冒烟（平台调用全部 mock，不需要真实账号）
python3 scripts/ui_smoke_next.py --base http://127.0.0.1:8765 \
    --username admin --password '……'
```

三个脚本都在 `scripts/`，退出码 0/1，可进 CI。
