# 希沃品课「课堂签到」链路验证报告（2026-10-20）

- 验证者：Lead（本轮实现者）
- 账号：控制台绑定的品课账号 #9（会话快照在 `data/sessions/accounts/`，**不入库**）
- 环境：本机 Python 3.14.4；学生会话来自浏览器快照
- 范围：学生端「课堂签到」；**没有**实现教师端任何能力
- 结论一句话：**签到链路已确认到「一次 POST」** —— 学生端 H5 读
  `attendanceCode`，当 `attendanceKey` 发给 `PostV1TeachingAttendanceAttend`。
  字段名与失败码已用真实会话实测；**成功路径还没用有效签到码验证**
  （老师端签到需要教师客户端发起，本轮没有可用的有效码）。

---

## 0. 结论表

| 结论 | 证据等级 | 详见 |
| --- | --- | --- |
| 学生端签到入口 = `/student/signin?attendanceCode=<码>` | 【静态-Web】 | §1 |
| 请求 = `PostV1TeachingAttendanceAttend {"attendanceKey": <码>}` | 【静态-Web】+【实测（字段名）】 | §1、§2 |
| 空 body → `401`「attendanceKey 不能为null」 | 【实测】 | §2.1 |
| 任意随机码 → `7207`「考勤签到码无效」 | 【实测】 | §2.2 |
| `7208` 已结束 / `10419` 不匹配 的映射 | 【静态-Web】 | §1 |
| 成功响应形状（`statusCode=0/200`?） | **【未验证】** | §3 |
| CLI dry-run / `--live`（无效码）端到端 | 【实测】 | §2.3 |
| 控制台动作 `signin` 干跑 / 写入（无效码） | 【实测】 | §2.4 |

---

## 1. 怎么发现的

1. 学生端 App 里的签到文案（英文资源）明确是扫码签到：
   「Please join the class first, then click "Sign in" to scan the code」
   「Scan code to sign in」「Sign in successfully」「The sign-in code has expired…」。
2. 带登录态 GET `https://pinco.seewo.com/student/signin`，服务端返回学生端 SPA
   （不是匿名时的「请在微信客户端打开」兜底页），chunk 是
   `signin_c7880cbe57ac0b489d64.js`。
3. 读该 chunk：页面从 `location.search` 取 `attendanceCode`，只调一个接口：

   ```js
   new URLSearchParams(location.search).get("attendanceCode")
   PostV1TeachingAttendanceAttend { attendanceKey: <attendanceCode> }
   // 成功：200 === response.code
   ```

   H5 里还登记了失败码 `7207 invalid` / `7208 overdue` / `10419 notMatch`。
4. 老师端出示的二维码就是
   `https://pinco.seewo.com/student/signin?attendanceCode=<码>`；码是**滚动**的
   （教师 Web 文案：「需单独发起签到活动，每10秒更换二维码，防止考勤作弊」），
   所以不能离线预生成或复用旧码。

---

## 2. 实测记录

所有请求都走绑定账号的浏览器会话（`student/api.json` + `x-server: main` +
`x-csrf-token`），只对自己的账号、只发签到接口。

### 2.1 字段名确认（空 body）

```
POST ?actionName=PostV1TeachingAttendanceAttend   {}
→ 401 「attendanceKey 不能为null」
```

### 2.2 失败路径确认（无效码）

```
POST ?actionName=PostV1TeachingAttendanceAttend   {"attendanceKey": "test"}
→ statusCode=7207 「考勤签到码无效」
```

换成 `"000000"`、随机 26 位串，结果相同。**无效码不会产生签到记录**，所以这些
探测是安全的；它们的价值是把「字段名 + 业务拒绝形状」钉死。

### 2.3 CLI 端到端

```console
$ PYTHONPATH=src .venv/bin/python scripts/pinco_signin.py \
    --code 'https://pinco.seewo.com/student/signin?attendanceCode=codex-verify-none-20261020' --live
平台错误码：7207
签到码：co**********************20（26 位）
结果：签到失败：考勤签到码无效（多半已过期或输错）
$ echo $?
4
```

干跑（不加 `--live`）只解析、不发请求，退出码 0。

### 2.4 控制台动作

用真实适配器直接跑 `signin` handler：

- `code=...` 且不填 `confirm` → 模式「干跑」、结果「未发送（干跑）」、无请求；
- `confirm=true` + 无效码 → 模式「写入」、结果「已发送，平台拒绝」、
  平台错误码 `7207`、失败原因「考勤签到码无效（多半已过期或输错）」。

---

## 3. 未验证的部分（不要当成已确认）

1. **成功响应形状**：H5 判 `code === 200`；我们的客户端按 Web RPC 家族
   （`statusCode` 0/200）判成功。有效码那一次会不会返回别的形状，**没有样本**。
2. **签到成功后的考勤状态**：需要平台侧的签到记录/统计来核对；本轮没有有效码，
   也没有教师端查看入口。
3. **是否还有额外服务端校验**（例如必须先进入课堂、同码只能用一次等）：
   H5 文案写了「先加入课堂再签到」，但错误码与请求里都没有课堂 id，
   推测是服务端按当前码反查课堂；**未实测**。

---

## 4. 实现与安全边界

- `platforms/pinco/attendance.py`：解析（整条链接 / 含链接文本 / 裸码）、
  提交、失败码文案；日志与展示都**打码**签到码。
- `adapter.sign_in(raw_code, *, dry_run=True)`：业务拒绝（7207/7208/10419）
  作为结果返回（`signed=False` + `error_code`），鉴权/协议错误照常上抛。
- 控制台动作 `signin`（`mutates=True`；`code` 必填；`confirm` 默认干跑）。
- `scripts/pinco_signin.py`：默认 dry-run，`--live` 才写；退出码
  0 成功/干跑、2 无会话、3 参数问题、4 平台拒绝。
- `PostV1TeachingAttendanceAttend` 已进 `DANGEROUS_ACTIONS`，抓取/计划路径
  不可能顺手发签到。

**范围说明（诚实披露）**：为确认「能不能自己发起一次签到」，本轮在用户口头
授权下调用过教师端**只读**接口（课程班级列表、进行中课堂），并试了 1 次
`PostV1ClassroomStart`（缺必填参数被拒，`illegal argument`，**无副作用**）。
教师 Web 前端没有发起课堂/签到的调用点（只有考勤报表），该能力在教师客户端，
所以最终没有可用的有效签到码。**产品代码没有新增任何教师端能力**，上述调用
不构成「教师端接口可用」的证据。

---

## 5. 还差什么（一次性验证清单）

> 有真实签到进行时，2 分钟就能闭合成功路径。

1. 老师/教师客户端发起课堂签到（或让学生把 App 里的签到二维码截图）；
2. 把二维码里的链接（或 `attendanceCode`）给到本机；
3. 运行：

   ```bash
   PYTHONPATH=src .venv/bin/python scripts/pinco_signin.py --code '<链接>' --live
   ```

4. 预期：退出码 0 + 「签到成功」；再在 App/教师端核对签到记录，把响应原文
   （脱敏）记到本文件 §2，并把 §3 第 1、2 条改成【实测】。

> 注意：签到码 10 秒一换，链接要在当次有效期内用；过期了重新取。
