# iTEST 实现文档：接口逻辑与作业（考试）流程

> 面向：维护者，以及想照着接下一个考试类平台的人。
> 协议证据见 [itest.md](research/itest.md)（调研报告，含实测行号）。
> 本文讲**我们的实现**：接口怎么组织的、能力边界在哪、为什么敢/不敢做什么。

---

## 1. 平台定位：它不是作业平台，是考试系统

| 维度 | 批改网 | iTEST |
| --- | --- | --- |
| 派发单位 | 个人（老师直接发作文） | **班级**（学生凭授权码加入，老师发「班级测试」） |
| 「作业」是什么 | 一篇作文 | **一场考试** |
| 有没有课程/学习进度 | 无 | 无（考试平台） |
| 答案来源 | 自己写 | **自己做**（考试没有标准答案下发） |
| 提交可逆吗 | 改稿可重交 | **不可逆**（交卷即定） |

**所以 iTEST 不能被当成「刷一刷」的平台** —— 它是真实考试，交错了没有第二次。

---

## 2. 链路与状态机

```
未登录
  │  Unipus SSO（AES 加密用户名/密码）→ serviceTicket → 会话 cookie
  ▼
已登录（有 jwt / iTSsiD）
  │  POST /utest/itest-mobile-api/student/exam/list
  ▼
考试列表 ──── judgeEntry（换答题窗口令牌，**不开始考试**）──→ 答题窗口 URL
  │                                                          │
  │                                              （人决定要不要进）
  │                                                          ▼
  │                                              进入窗口 → 设备检测
  │                                                          ▼
  │                                              load 取卷（**开始计时**）
  │                                                          ▼
  └─────────────────────────────────────────────────  作答 → 提交（不可逆）
```

我们的实现**止步于「拿到答题窗口 URL」**，后面三步刻意不碰（见 §4）。

---

## 3. 接口

### 3.1 适配器（`platforms/itest/`）

| 方法 | 性质 | 说明 |
| --- | --- | --- |
| `login(credentials)` | 写（登录） | Unipus SSO；两步必须共用同一个 cookie 会话 |
| `is_logged_in()` | 只读 | 看有没有 `jwt` / `iTSsiD` cookie |
| `list_courses()` | 只读 | **合成一门**「iTEST 考试」—— 平台对学生没有课程层 |
| `list_assignments(course)` | 只读 | 考试列表，`kind=TaskKind.EXAM`，`remote_id` = `ksdId` |
| `answer_window_url(exam_id)` | 只读（签发令牌） | `judgeEntry` + `returnUrl`；**不会开始考试** |
| `exam_meta(exam_id)` | 只读 | 标题 / 起止 / 时长 |
| `fetch_questions` | — | **不实现**（理由见 §4） |
| `submit` | — | **不实现**（不可逆） |

### 3.2 端点常量（`client.py`）

| 常量 | 值 |
| --- | --- |
| `BASE` | `https://itestcloud.unipus.cn` |
| `SSO` | `https://sso.unipus.cn` |
| `LOGIN_PATH` | `/utest/itest/login` |
| `EXAM_PAGE_PATH` | `/utest/itest/s/exam` |
| `EXAM_LIST_PATH` | `/utest/itest-mobile-api/student/exam/list`（POST JSON `{}`） |
| `JUDGE_ENTRY_PATH` | `/utest/itest/s/exam/judgeEntry`（POST 表单 `examId`） |
| `SESSION_COOKIES` | `jwt` / `iTSsiD` |

### 3.3 控制台动作

| 动作 | 说明 |
| --- | --- |
| `exams` | 考试列表（考试号/名称/起止/时长） |
| `entry` | 换答题窗口 URL —— **只签发令牌，不开始考试**，返回时明确提示「打开后开始计时」 |
| `courses` / `assignments` / `detail` | 通用动作，自动适配（合成课程 + 考试列表） |

---

## 4. 四件刻意不做的事（以及为什么）

| 不做 | 原因 |
| --- | --- |
| **取卷 `load`** | 需要浏览器设备检测产生的两个不透明 cookie（`envTest*` / `iTMExam`）；而且**一进答题页就开始计时**，等于替你开考 |
| **作答** | 考试没有标准答案下发，答案得自己想；乱填就是交白卷 |
| **提交** | 交卷不可逆，且 `ansData` 的载荷结构只有 JS 常量证据、**从未真实发送过** |
| **刷课** | 考试平台没有「刷课」概念 |

**能力声明如实反映**：`fetch_questions=False`、`submit=False`、`course_playback=False`。

---

## 5. 会话与凭据

- iTEST 与 U校园共用 **Unipus SSO**，但**会话不通用**：
  `jwt` 挂在 `.unipus.cn` 上，iTEST 会收到它，但应用层仍会报
  `code=100 用户登录状态失效` —— **必须重新走一次 SSO**（实测）。
- 登录需要**用户名 + 密码**（AES 加密提交）。账号库刻意不存密码，
  所以：**添加账号时登录一次**，之后靠 cookie 复用；cookie 过期就得再输一次。
- 我们没有实现「用 TGC 静默换票」：实测 TGC 会过期（几小时），
  过期后 SSO 直接跳登录页，绕不过密码。

---

## 6. 已实现 / 未实现

| 能力 | 状态 |
| --- | --- |
| 登录（SSO） | ✅ 实现（需密码） |
| 考试列表 | ✅ 实现（字段名做了别名容错） |
| 换答题窗口令牌 | ✅ 实现（已核验「不开始考试」） |
| 考试详情展示 | ✅ 走通用 `detail` 动作（任务号/名称/起止/时长） |
| 取卷 | ❌ 需要浏览器设备检测 cookie |
| 作答 / 交卷 | ❌ 课程外，且不可逆 |

---

## 7. 如果要真的自动化考试（强烈不建议）

真要做，前置条件是**全部满足**：

1. 能够复现设备检测（键盘/耳机）拿到 `envTest*` / `iTMExam` cookie；
2. 能解析 `load` 返回的 `D_HTML` / `C_HTML` / `UIK` / `TL`（题号+题干+选项）；
3. 能构造 `ansData` 的 `{al, sl}` 结构 —— **目前只有 JS 常量证据**，
   必须先用一次真实交卷报文校准；
4. 接受「一进答题页就开始计时」与「交卷不可逆」两个后果。

**风险提示**：在线考试通常有监考参数（`screenLimit` / `capture` / `rtcRecord` /
`faceRecognize`，全在 JWT 里）。用自动化手段代考可能直接触发违纪判定，
后果远大于「作业没交」。这也是本适配器刻意停在「只给入口」的原因。

---

## 8. 踩过的坑

| # | 坑 | 说明 |
| --- | --- | --- |
| 1 | 以为 Unipus 会话能跨站复用 | `jwt` 挂在 `.unipus.cn` 会被发到 iTEST，但应用层仍判「登录失效」 |
| 2 | 以为 TGC 能静默换票 | 实测 TGC 过期后 SSO 直接跳登录页 |
| 3 | `judgeEntry` 当成了「开始考试」 | 实测**不会**：调用前后 `ansBeginTime` 仍为空 —— 所以它可以安全地当只读动作暴露 |
| 4 | 忘记补 `returnUrl` | 缺它平台会报「没有权限」 |
| 5 | `list_assignments` 想用真实课程 | iTEST 对学生没有课程层，只能合成一门 |
