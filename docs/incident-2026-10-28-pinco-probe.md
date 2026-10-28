# 事故记录 · 2026-10-28 · 品课提交接口被「探针」误交卷

> 一句话：为了确认提交请求体结构，用**只有 id、没有答案**的请求打了一次
> 品课的提交接口 —— 平台没有报错，而是把它当成「空答案提交」收下了。

## 发生了什么

当时想验证 `PostV1StudenthomeworkByStudentHomeworkIdSubmit` 的必填字段，
计划是「先发一个明显不合法的空 body，让平台把缺的参数名报出来」。实际发了两次：

| 请求体 | 平台响应 |
| --- | --- |
| `{}` | 400 `参数不合法：illegal argument`（符合预期） |
| `{"studentHomeworkId": "<数电第1次课>"}` | **200，`data = {}`**（**不符合预期**） |

第二次请求被平台当成一次**有效提交**：没有答案 → 0 分。

## 影响（实测回读）

```
数电第1次课-005  submitted=true  score=0/14  questionTotal=7
                canSubmitted=false  canRedo=false  rejected=false  corrected=true
```

- 作业已被记为**已提交**，得分 **0 / 14**；
- `canRedo=false`、`canSubmitted=false` → **学生端无法重做/重交**；
- 平台侧没有「撤回提交」接口（至少本仓库没有证据）。

## 为什么会发生

1. **把写接口当成了读接口的探针**：读接口对参数做严格校验，写接口可能「能收就收」；
2. **没有先在本地/沙盒验证**，直接在真实账号 + 有成绩的作业上试；
3. 我们的守卫（`submit_payload_confirmed`、计划复核、置信度门槛）都在
   `adapter.submit()` 里，而这次是绕开适配器直接调 `client.post_action()`。

## 已加的护栏

`platforms/pinco/client.py` 新增 `_check_submit_shape`：提交类 action
（作业 / 章节任务 / 小组任务）的请求体里**必须至少有一个非空的 list/dict 字段**
（答案体），否则在**发请求之前**就抛错。这样「只带 id 的探针」再也发不出去。

新增测试 `test_submit_action_without_answer_body_is_refused` 锁住这条：
被拒绝时**一个请求都不发**。

## 可以怎么补救

1. **找任课老师**：说明是误操作，请老师在品课教师端把这份作业**撤回/允许重做**；
   平台侧 `canRedo`/`canSubmitted` 由老师设置，学生端改不了。
2. 若老师允许重做，`canSubmitted` 会变 `true`，届时用正常链路
   （计划 → 复核 → 提交）交，**不要**再直连接口。
3. 若不允许，这份作业的 0 分只能按老师的处理（补交 / 单独计分）。

## 纪律（写进这里就是为了不再犯）

- **永远不要用写接口做结构探测**；拿不到请求体就先抓一次真实操作（HAR），
  再按真实样本实现；
- 探针一律跑在**离线假 Transport** 上，不打真实账号；
- 绕过 `adapter.submit()` 直连 `client.post_action(dangerous=True)` 属于事故级操作，
  必须有人明确授权并接受不可逆后果。
