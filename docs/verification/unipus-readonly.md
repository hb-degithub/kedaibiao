# U校园｜只读链路真实联调记录（2026-10-20）

> 本文记录 U校园适配器第一次**用真实账号**跑通的过程与证据边界。
> 结论先行：**登录态复用、课程、任务、题目内容、标准答案都已真实跑通**；
> 提交、学习时长、语音评测仍未实测（各自文档见 `unipus-submit.md`、
> `unipus-duration.md`、`unipus-speech.md`）。
>
> 所有抓取样本只落在本地 `data/recon/unipus/`（`data/` 已 gitignore），
> 未入库、未进日志；文中账号标识已脱敏。

---

## 0. 环境与账号

| 项 | 值 |
| --- | --- |
| 平台 | U校园 5.0（`uai.unipus.cn` 门户 + `ucontent.unipus.cn` 内容 + `sso.unipus.cn` 登录） |
| 账号 | 控制台已绑定的 `unipus` 账号（学号脱敏，`ssoId` 32 位十六进制） |
| 会话 | `data/sessions/accounts/1-unipus-6.cookies.json`（SSO cookie，含 `jwt`） |
| 时间 | 2026-10-20（Asia/Shanghai） |

联调方式：**不重新登录**，直接从已落盘的 SSO cookie 里取 `jwt`
（它就是门户 JWT）复原会话，再打只读接口。这也是
`UnipusAdapter.is_logged_in()` 现在的兜底路径（cookie 里有 `jwt` 就能自恢复）。

---

## 1. 鉴权：参考实现的常量已经过期（本次最大发现）

### 现象

用规格里的常量（来自 `Yunlk/uai-core@4628e44`，2026-09 快照）签 annotator token：

| 接口 | 结果 |
| --- | --- |
| `GET /api/mobile/user_module/{inst}/{gid}/progress/v2` | **401**，正文是 HTML `/unAuthenticated` |
| `GET /course/api/v3/answer/{inst}/{gid}/default` | **401**，同上 |
| `GET /course/api/v3/content/{inst}/{gid}/default` | 200 但 `code:1`、`content:""`（空壳） |

换 `Authorization: <门户 JWT>`、带上全部 cookie、改大小写、加 `x-csrftoken` —— 结果完全一样。
所以问题不在头名，而在 token 本身。

### 取证

U校园教程学习前端产物（公开静态资源，2026-10-20 抓取）：

```
https://unicloud.cdn.unipus.cn/tutorial/pc/55-b9de8d09.js
```

该 bundle 里的 token 构造逻辑（转述，不复制代码）：

- payload 键与顺序：`open_id, name, email, administrator, exp, iss, aud`
- `exp = Date.now() + 31536e6`（约一年）
- `iss`、HS256 密钥、`aud` 三个常量都在前端明文里

当前值（已写入 `platforms/unipus/endpoints.py`）：

| 常量 | 参考实现（2026-09） | 线上当前（2026-10-20） |
| --- | --- | --- |
| issuer | `example-annotator-issuer` | `c4f772063dcfa98e9c50` |
| HS256 secret | `example-annotator-signing-secret-0001` | 见 `endpoints.ANNOTATOR_SIGNING_SECRET` |
| audience | `edx.unipus.cn` | `edx.unipus.cn`（未变） |

> 前端把 secret 明文下发，说明它**不是**服务端按账号签发的凭据；真正标识账号的是
> `open_id`（只能从已登录会话取）。这与参考实现的结论一致，只是**值被轮换了**。

### 验证矩阵（换常量之后，同一批接口）

| 请求 | 结果 |
| --- | --- |
| `progress/v2` + 新 token（不带 cookie） | **200** `{"code":0,...}` |
| `content` + 新 token | **200**，密文可解密 |
| `answer` + 新 token | **200**，密文可解密 |
| 三种组合（token / token+jwt / token+csrftoken） | 全部 200，**没有**额外头依赖 |

→ 结论：`x-annotator-auth-token` 一个头就够；`Authorization` 与 cookie 不是必需。

### 复现

```bash
# 1) 用已保存的 cookie 复原会话（不动密码）
PYTHONPATH=src python3 - <<'PY'
import asyncio, json, pathlib
from kedaibiao.platforms import load_builtin_adapters; load_builtin_adapters()
from kedaibiao.runtime import build_runtime, restore_adapter_session
cookie_file = "data/sessions/accounts/1-unipus-6.cookies.json"
rt = build_runtime(root=".", cookie_file=cookie_file)
ad = rt.make_adapter("unipus"); restore_adapter_session(ad, cookie_file)
ad._client = ad.client()
client = ad.client()
client.session.portal_token = next(c["value"] for c in json.loads(pathlib.Path(cookie_file).read_text()) if c["name"] == "jwt")
async def main():
    client.apply_user_info(await client.fetch_user_info())
    print("open_id:", client.session.open_id)
    print("courses:", len(await ad.list_courses()))
asyncio.run(main())
PY

# 2) 控制台/CLI 同一份能力
PYTHONPATH=src python3 -m kedaibiao action unipus courses
PYTHONPATH=src python3 -m kedaibiao action unipus detail assignment_id='course-v2:…/<groupId>'
```

---

## 2. 书架：线上结构与规格不同（已修）

规格第 6.4 节记录的是「班级节点 + `courseResourceList`」。线上真实结构是：

```
value.groups.CLASS_COURSE[] / PERSONAL[] / EXPIRED[]   # 三个分组，元素本身就是教材
   └─ resourceId / instanceId / name / activation / classCourses[]
```

`parse_bookshelf` 现在两种结构都认（`value.groups` 优先），旧结构的单测仍然保留。

实测结果：**10 门**「班级 × 教材」绑定，`activation=0` 与现有约定一致（0 = 已激活）；
未激活的教材 `activation` 缺失（返回 `None` 而不是 `False`）。

---

## 3. 课程与任务

- `list_courses()`：10 门，带 `finishPointNum/totalPointNum`（门户真实进度）；
- `list_assignments(course)`：以「新标准大学英语（第三版）视听说教程1」为例 **264 个任务**，
  单元名、分组名、分组 ID 都正常；`Assignment.remote_id` 仍是
  `<实例ID>/<分组ID>`（幂等键约定不变）；
- `kind` 仍标 `kind_unconfirmed`：目录节点确实没有「作业/考试」信息，没有瞎猜。

```bash
PYTHONPATH=src python3 -m kedaibiao action unipus assignments course=20000943839
```

---

## 4. 题目内容与标准答案（U1/U2 部分解决）

### 4.1 解密后的真实结构

内容接口（`course-v2:` 实例 ID）解密后是数组：

```
[
  {"id": 1474574371373907969, "content": "<字符串化 JSON>"},
  ...
]
```

`content` 解出来的对象：

| 键 | 说明 |
| --- | --- |
| `replyType` | `singlechoice` / `text-area` / `discussion` / … |
| `contents[]` | 媒体项：`type`（**1=音频、2=视频**，4/5/6 为正文等）、`path`、`duration` |
| `children[]` | **子题**：`quesText`（HTML 题干）、`options[]`（`{name,text,value}`）、`replyType` |
| `direction.text` | 整组说明（HTML） |
| `quesText/options/rule` | 题组级字段（选择题的 options 在子题里） |

答案接口解密后是数组，元素：

```
{"id": …, "content": "", "answer": "<字符串化 JSON>", "analysis": "<…>", "quesId": …}
```

`answer` 形如 `{"children":[{"answers":["A"]},{"answers":["B"]},…]}` ——
**按子题顺序**给标准答案；主观/口语题在答案侧为空（合法）。

### 4.2 对适配器的影响（已改）

- `ContentQuestion` 新增 `stem` / `options` / `children` / `media_indexes`；
  `adapter._to_question` 不再一律把题面留空 —— 真实样本里
  **题干与选项都能取到**（旧规格 U1 的「不可得」结论对当前平台不成立）；
- `media_indexes` 是 `contents` 里的**下标**（提交 `progress` 的键），
  修掉了「把 `type` 当索引」的旧 bug；
- `raw.children` 原样保留，提交时按子题对齐。

实测样例（脱敏后）：

```
Exercise 1（视听说教程1 U1）→ 1 题 fill-blank，3 个计分点
  stem: It's Janet's first day as a student at Hertford College. …
Exercise 1（视听说教程1 U1 听力）→ 5 个子题 singlechoice，选项 True./False.
  标准答案: A B A B B
```

### 4.3 仍未解决

- **答案字符串的语义**仍只做「原样转发」（多选/排序保序，不排序不去重）；
- 主观题、口语题的 `record`/音频上传链未实测（见语音评测文档）；
- 口语/角色扮演题映射为 `UNKNOWN`（核心枚举没有「口语」类型），需要人工或专项链路。

---

## 5. 证据边界

| 项 | 状态 |
| --- | --- |
| 会话复用（cookie 里的 `jwt`） | ✅ 实测 |
| 登录 1506 → 浏览器求解回退 | ⚠️ 代码已接（复用 iTEST 已验证的 `login_via_browser`），**本次没有触发**（会话一直有效） |
| 课程 / 任务列表 | ✅ 实测（10 门 / 264 任务） |
| 目录（单元 → 分组） | ✅ 实测（6 单元，分组 ID 与 `url` 一致） |
| 内容解密 + 题干/选项 | ✅ 实测（多门教材、多种题型） |
| 标准答案解密 | ✅ 实测（选择题答案逐子题对齐） |
| 进度 `progress/v2` | ✅ 实测（鉴权探针） |
| 必修集 `leafs` | ⚠️ 未联调（接口已实现，未验证 tasks 参数与口径） |
| 提交 | ❌ 未实测（见 `unipus-submit.md`） |
| 学习时长 / 语音评测 | ❌ 未实测（见各自文档） |
| 限流退避的真实表现 | ❌ 未触发过 |

---

## 6. 维护提醒

- **常量会轮换**：`ANNOTATOR_ISSUER/SECRET` 是前端公开常量，平台换前端就会变。
  症状是「目录/内容能读，但 progress/answer 401」；排查时先打 `progress/v2`，
  再去 `unicloud.cdn.unipus.cn/tutorial/pc/` 找当前教程学习 bundle 核对常量。
- **不要用旧规格的常量表**：`docs/research/unipus-protocol.md` 已在本节所指处
  加了校正说明；参考实现（非商业许可）只可读功能事实，不可并码。
- 联调产生的样本都在 `data/recon/unipus/`，含真实课程内容，**不要提交入库**。
