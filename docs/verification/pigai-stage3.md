# 批改网 Stage 3 产出的独立验证报告

- 验证者：**verifier**（与三个产出的作者均非同一人）
- 验证方式：**全程离线**（只读代码 + 跑测试 + 构造对抗输入 + 读本地 `data/recon/` 既有抓包）
- 线上访问：**零**。本报告未访问 `www.pigai.org`，也未发起任何网络请求（见 §5 的断网复跑）
- 规格依据：`docs/research/pigai.md` §4–§6（唯一权威规格；U15 已撤回，见 §4.5）

---

## 0. 基线与 revision 钉桩

本报告分四轮，**每轮结论只对当轮 md5 有效**：

| 轮次 | 时间戳（Asia/Shanghai） | 内容 | 章节 |
| --- | --- | --- | --- |
| **第一轮（task-5）** | 2026-10-19 11:16 | Stage 3 首版：6 项复核 + 24 个对抗用例 | §1–§10 |
| **第二轮（task-11）** | **2026-10-19 11:52** | **冻结版（第 1 版）**：A/B/C 新能力 + task-5 缺陷修复 + 24 例增量 | **§11** |
| **第三轮（task-12）** | **2026-10-19 12:03** | **第 2 版**：需知会-5/-6 修复 + 建议-12/-13/-14（4 文件变更） | **§12** |
| **第四轮（task-13）** | **2026-10-19 12:12** | **第 3 版（定稿轮）**：需知会-7 修复（3 文件变更）→ **可以定稿** | **§13** |

### 0.1 第一轮 revision（task-5，md5 采集于 11:16）

| 文件 | md5 |
| --- | --- |
| `src/kedaibiao/platforms/pigai/protocol.py` | `fb1cfc1701cf2fa4c1a429feafd495e5` |
| `src/kedaibiao/platforms/pigai/parse.py` | `3885e547b80816b2cde264b63d796622` |
| `src/kedaibiao/platforms/pigai/adapter.py` | `92fbdf850462ec580e79bb1a6c2e62bb` |
| `src/kedaibiao/platforms/pigai/client.py` | `38c1a8892fbb0dbd2002516e2de75998` |
| `src/kedaibiao/platforms/pigai/rsa_login.py` | `c8b23f41ea4e4109e5f6cda64c70f0b7` |
| `tests/test_pigai_login.py` | `d801aab8783c738442e87591208096ef` |
| `tests/test_pigai_parse.py` | `56ff02a23d8113b460846a18e7879edf` |
| `tests/test_pigai_adapter.py` | `992683fd390435a0e2577f6058101baa` |

### 0.2 第二轮 / 冻结版 revision（task-11，md5 复核于 11:45–11:52，**全部与任务书一致**）

| 文件 | md5 | 行数 |
| --- | --- | --- |
| `src/kedaibiao/platforms/pigai/protocol.py` | `17e17286d26b239011b1f174ddb9e245` | 452 |
| `src/kedaibiao/platforms/pigai/rsa_login.py` | `c8b23f41ea4e4109e5f6cda64c70f0b7` | 171 |
| `src/kedaibiao/platforms/pigai/client.py` | `d791255c040b6312b8850668726523cd` | 317 |
| `src/kedaibiao/platforms/pigai/parse.py` | `ec961a7072379cc31fca86c3adc5ebae` | 1135 |
| `src/kedaibiao/platforms/pigai/adapter.py` | `30901002c8c8e3a316ee0b71976dc963` | 284 |
| `src/kedaibiao/platforms/pigai/__init__.py` | `d087d025d44cdcabfbeca8f5e3e91919` | 33 |
| `tests/test_pigai_login.py` | `d734dd7634795b29ec6ddc75c2b0f2de` | 659 |
| `tests/test_pigai_parse.py` | `61b0e03225136d841d7f2c4a553ff376` | 808 |
| `tests/test_pigai_adapter.py` | `27f7934e3a072b57a3f8622d9e9c8f60` | 366 |
| `scripts/pigai_read.py` | `03ed5bbaadf71843767e964dfe7a7bcc` | 186 |

fixtures（7 个，冻结版新增 2 个）：

```
8a892543f849245256e083f876191ebb  tests/fixtures/pigai/assignments_pending_empty.html
ffda457a303b518049c353d7093eb288  tests/fixtures/pigai/assignments_pending_row.html
d1193c2545fd9002ef1dd6506bd77150  tests/fixtures/pigai/essay_detail.html
9a106bf2bae19620ffe400ea357f6c0e  tests/fixtures/pigai/essays_done.html
dcaaa50e7aeadfd6633b04e1fce3cae0  tests/fixtures/pigai/myclass.html
eff725327b2999288df0be6420620769  tests/fixtures/pigai/write_fresh.html
2a93694f1a0ef001449002e3e4f00337  tests/fixtures/pigai/write_with_draft.html
```

```bash
$ md5sum src/kedaibiao/platforms/pigai/*.py tests/test_pigai_*.py scripts/pigai_read.py
$ md5sum tests/fixtures/pigai/*.html
（输出即上面两张表）
```

### 0.3 ⚠ 更正：第一轮 §6/§7 对 lead 基线的质疑**是我比错了样本**

第一轮我把 lead 的详情页基线（89.0/100、24 条点评、`level` 四类齐全）与**离线样本
`eid=200000004`（85.5/100、19 条、`{error, error_trp}`）**对比，判为「不自洽、需澄清」。
**这个质疑不成立**，是**我**拿错了对照物：lead 的基线来自**另一篇作文**
`rid=1000003 / eid=234022144`，规格 `docs/research/pigai.md` **§6.10 的表格里明确写了 eid**，
并在其后的 ⚠ 注里专门做了区分说明。两篇不同的作文，两组数字都对。

- 我核对的那篇（`eid=200000004`）→ 85.5 / 19 条 / `{error, error_trp}` —— 与规格 §6.3、§6.5 一致；
- Lead 基线那篇（`eid=234022144`）→ 89.0 / 24 条 / `{error, error_trp, great, warn}` —— 与规格 §6.10 一致，
  且是 **U25 关闭的依据**（四类 `level` 齐全只对这一篇成立）。

**因此第一轮的「需知会-4」撤回**（见 §7 的划线与 §11.0）。教训记在这里：离线只有一篇详情样本时，
不应把「另一篇线上样本」当成矛盾；规格里已经写了 eid，我应当先按 eid 对齐再下结论。

### 0.4 结论速览

- **第一轮（首版）**：6 个复核项 5 项通过、1 项部分通过（写入面字面口径）；0 阻断、4 需知会、6 建议。
  其中「需知会-4」已在本轮撤回；其余各项的修复情况见 **§11**。
- **第二轮（冻结版）**：全部 6 项**已在冻结版上复验通过**（§11.2）；A/B/C 新能力经真实 recon DOM 端到端验证通过（§11.3）；
  增量对抗 24 例（§11.5）。**仍无阻断级缺陷**；新增 2 条需知会（§11.6）。

---

> **说明（task-11 追加）**：下面 §1–§6 保留首版原文；这 6 项的全部结论**已在冻结版
> （§0.2 的 md5）上复验通过**，复验证据与新增结论汇总在 **§11**。首版「需知会-4」已在 §0.3 撤回。

## 1. 复核项 1：全量测试 —— ✅ 通过（已在冻结版上复验）

```bash
$ PYTHONPATH=src python3 -m unittest discover -s tests -t .
----------------------------------------------------------------------
Ran 601 tests in 1.556s

OK
```

- 亲自跑通，**601 tests / 0 failures / 0 errors**。注意：任务书写的基线是 598，实际为 **601**；
  差值来自其他窗口（pinco 等）新增用例，与 pigai 无关。**没有观察到任何失败**，故无需归因。
- 三个 pigai 模块单跑（亲自跑，非引用作者简报）：

```bash
$ PYTHONPATH=src python3 -m unittest tests.test_pigai_login   → Ran 33 tests  OK
$ PYTHONPATH=src python3 -m unittest tests.test_pigai_parse   → Ran 47 tests  OK
$ PYTHONPATH=src python3 -m unittest tests.test_pigai_adapter → Ran 20 tests  OK
$ PYTHONPATH=src python3 -m unittest tests.test_net           → Ran 26 tests  OK
```

pigai 合计 100 条，全绿。注册自检也通过：

```bash
$ PYTHONPATH=src python3 -m kedaibiao platforms
已注册 3 个平台适配器：pigai 批改网 / pinco 希沃品课 / unipus U校园
```

## 2. 复核项 2：密钥 / 隐私泄漏审计 —— ✅ 通过（已在冻结版上复验，含 2 个新 fixture）

### 2.1 账号密码字符串不出现在任何文件

不打印凭据，脚本从 `.env` 取 `KEDAIBIAO_PIGAI_USER/_PASS`、`KEDAIBIAO_PINCO_USER/_PASS`
的全部值，遍历仓库（排除 `.env`、`data/`、`.git/`、`__pycache__/`）：

```
== hits (path, which key, value length) ==
total hits: 0
```

**0 命中**。`.env` 本身被 `.gitignore` 排除，`git status` 亦确认未入库。

### 2.2 源码日志 / 异常路径不泄漏密文、表单体、cookie 值

逐行读了 `client.py` 与 `adapter.py` 的**全部**日志与异常构造点（不接受注释声明）：

| 位置 | 实际打什么 | 判定 |
| --- | --- | --- |
| `client.login` 首页请求 | `HTTP %s %dB`（只状态码与长度） | ✅ |
| `client.login` 失败分支 | cookie **名字** `_SUCCESS_COOKIE` | ✅ 仅名字 |
| `client.login` 成功 | cookie **名字** + 落地页字节数 | ✅ 仅名字 |
| `client.get` / `is_logged_in` | 路径 + 状态码 + 字节数 | ✅ |
| `PigaiResponseError` | `f"{method} {path} 返回 HTTP {status}"`，**刻意不携带响应体** | ✅ |
| `rsa_login` 明文过长 | 只报长度数字（`118 > 117 字节`） | ✅ |
| `SessionExpiredError` | 固定文案，无正文 | ✅ |
| `adapter.login` 拒绝分支 | `log.error("批改网登录被拒绝：%s", exc)`，`exc` 是 `PigaiResponseError` | ✅ |

不存在打印表单体（`form`）、密文（`password_encrypt`）或 cookie 值的语句。**作者的隐私声明属实。**

### 2.3 `tests/fixtures/pigai/` 无个人信息 / cookie 名

独立于作者自带断言，我另跑一遍扫描（先剔除 HTML 注释里的脱敏说明）：

```
assignments_pending_empty.html   email=- phone=- cookie=- 真实姓名/校=-
assignments_pending_row.html     email=- phone=- cookie=- 真实姓名/校=-
essay_detail.html                email=- phone=- cookie=- 真实姓名/校=-
essays_done.html                 email=- phone=- cookie=- 真实姓名/校=-
myclass.html                     email=- phone=- cookie=- 真实姓名/校=-
（cookie 名集合：_JUKU_USER / PHPSESSID / _JKU / isPrize / JK_GCNT  —— 均未出现）
（≥40 连续英文正文：5 个文件均为「无」）
```

全清。「张老师 / 示例N班 / 示例大学 / C0000001」是 fixture 头部注释里**自我声明**的占位值。

### 2.4 附带核对：fixture 是否真「基于真实 DOM 重写」

拿本地 `data/recon/pigai/raw/`（`.gitignore` 排除，不入库）逐项对：

| fixture 声称 | 真实 recon 里的计数 | 判定 |
| --- | --- | --- |
| `myclass.html` 保留 `.baseinfo/.detail/.m-score` 结构 | `class="baseinfo` ×3、`class="detail` ×6、`class="m-score` ×4 | ✅ 结构真实存在 |
| `assignments_pending_row.html` 该页**有 1 条**数据行（推翻 U15） | `class="reItemS"` ×1，含 `1000005` | ✅ 实锤 |
| `essays_done.html` 真实 10 行、1 条未公布、4 条多稿 | 10 行；`未截止` ×1；提交次数 `{1×6, 2, 2, 3, 6}` | ✅ 完全吻合 |
| `essay_detail.html` 分数 85.5 / 容器 154px / 19 条点评 | `#rescoreTxt`=85.5、`class="tbL"`×4、`<li sid=`×19 | ✅ 完全吻合 |

fixture 的「保留 / 替换 / 缩减」三段自述**经得起核对**，是老实标注。

## 3. 复核项 3：写入面审计 —— ⚠️ 部分通过（字面口径；冻结版已升级为 AST 全包审计，见 §11.2.9）

用 AST 区分**可执行字符串**与 **docstring**，扫 pigai 全包 6 个 `.py`：

```
__init__.py    可执行字符串命中=无  docstring 命中=无
adapter.py     可执行字符串命中=无  docstring 命中=无
client.py      可执行字符串命中=无  docstring 命中=无
parse.py       可执行字符串命中=无  docstring 命中=[(272,'a=write'), (463,'a=write')]
protocol.py    可执行字符串命中=无  docstring 命中=无
rsa_login.py   可执行字符串命中=无  docstring 命中=无
```

- **可执行代码里 0 处写入端点字面量** —— 这是要害，通过。
- 但任务书要求的口径是「`platforms/pigai/` **全部源码**确认不存在字面量」。严格照字面，
  `parse.py` 的 **docstring**（`parse.py:275`、`parse.py:486`）出现了 `?c=v2&a=write&rid=1000005`
  作为「实测 href 原样」的举例。这是**散文引用**，不是可构造请求；`_raw_href()` 保留它是**数据透传**，
  `Assignment.url` 里的写作页 href 属数据不属代码 —— 与任务书的区分口径一致。
  故判「部分通过」而非「不通过」。
- 另：作者自带的 `TestNoWriteSurface` 只静态审计 `adapter.py` + `protocol.py`，**未覆盖
  parse/client/rsa_login/__init__**。我用 AST 补上了全包审计（结论同上）。记为建议-9。
- 运行时验证：驱动 `list_courses()` + `list_assignments()`（pending + done）后回看假传输层：

```
GET https://www.pigai.org/index.php?c=write&a=waitComplete
GET https://www.pigai.org/index.php?c=write
含 a=write 的请求: 无
```

**能力声明与调用行为**（独立复跑，非引用作者测试）：

```
submit = False | fetch_questions = False | login = True | list_courses = True | list_assignments = True
submit:          NotSupportedError ✓ (pigai 不支持 submit)
fetch_questions: NotSupportedError ✓ (pigai 不支持 fetch_questions)
```

两者确实由基类（`base.py:127-139`）抛出，且 `Capabilities` 为 `False` —— 通过。

登录的唯一一次 POST 也被独立验证：`login()` 的调用序列为 **GET `/` → POST `/index.php?a=login`**，
表单字段恰为 `username / password="" / remember=1 / checkhash="" / password_encrypt=<172 字符密文>`，
明文密码不出现在请求体。

## 4. 复核项 4：契约一致性（抽 3 条逐字比对）—— ✅ 通过（已在冻结版上复验）

### 4.1 RSA 公钥

从规格 §4.2 的 `setPublicKey('...')` 抽出字面量，与 `protocol.RSA_PUBKEY_B64` 逐字比较：

```
spec len = 216 | protocol len = 216 | 逐字一致: True
```

**并用规格附录 A 的独立实现复算**（不调 `rsa_login`）后与 `rsa_login.parse_spki_public_key` 对比：

```
附录A: n.bit_length = 1024  e = 65537 | 与 protocol 解析结果一致: True
n 前缀 0x98948235e9d4b0404ac3 … 后缀 8e510e1f（与规格 §4.2 记载一致）
```

密文结构（e=1 退化钥匙，可把填充后的 EM 原样取回）：

```
len(msg)=1  : |EM|=128 prefix=0002 sep=0x00 ps_nonzero=True tail_ok=True
len(msg)=117: |EM|=128 prefix=0002 sep=0x00 ps_nonzero=True tail_ok=True
118 bytes   -> ValueError: 明文过长：118 > 117 字节（RSA-1024 单块上限）
```

`00 02 ‖ 非零 PS ‖ 00 ‖ M`、k=128、上限 k−11=117 —— 与规格 §4.2 完全一致。

### 4.2 `#rescoreTxt`

```
spec 含 `<b id="rescoreTxt"` = True
protocol.DETAIL_SCORE_ID = 'rescoreTxt'  → True
真实 recon 页面: id="rescoreTxt"  title="满分:100"  content="100" …>85.5
```

### 4.3 `DONE_HEADERS` / `PENDING_HEADERS` 列序

```
protocol.DONE_HEADERS    = ('作文ID','作文标题','体裁','提交时间','截止日期','成绩','提交次数','操作')
spec §6.2 表头行: 作文ID(80px)|作文标题(448px)|体裁(50px)|提交时间|截止日期|成绩(50px)|提交次数(60px)|操作(110px)
protocol.PENDING_HEADERS = ('作业号','作业标题','类型','出题人','指定班级','截止时间','操作')
spec §6.1 表头 li 文案: 作业号/作业标题/类型/出题人/指定班级/截止时间/操作  → 顺序一致: True
```

`protocol.ANNOTATION_LEVELS = ('warn','error','error_trp','great')` 与规格 §6.5 官方四类一致。

### 4.5 U15（已撤回）的交叉核对 —— 见 §8 建议-5

规格 §8 已撤回 U15（原断言「waitComplete 只有表头、0 行」）。本地真实样本
`data/recon/pigai/raw/b_index_php_c_write_a_waitComplete.html` 里 `class="reItemS"` **恰好 1 条**，
含 `1000005` —— **勘误成立，行级 DOM 确为已实测**。
但 `protocol.py:79` 仍写「已核验（0 行）」、`protocol.py:190` 仍写「**行级 DOM 未实测（推断）**」，
与勘误矛盾（见 §8 建议-5，纯文档，无行为影响）。

## 5. 复核项 5：离线可测性 —— ✅ 通过（已在冻结版上复验：148 tests 断网全绿）

把外连原语全部打成抛异常后复跑（保留 `socketpair`，否则会误伤 asyncio 的自管道）：

```python
socket.create_connection = socket.getaddrinfo = socket.gethostbyname = boom
http.client.HTTPConnection.connect = http.client.HTTPSConnection.connect = boom
```

```
pigai x3, outbound blocked: successful = True | tests = 100 | errors = 0 | failures = 0
```

- **三个 pigai 模块 100 条在出站网络被封死时仍全绿。**
- 连带跑的 `test_net` 有 6 个 error，全部来自它自己的 `TestUrllibTransportLoopback`
  （`127.0.0.1` 回环服务器，`test_net.py:237`），**与 pigai 无关，也不出网**，属预期。
- 三个 pigai 测试文件不构造 `UrllibTransport`，不 `urlopen`，不 `socket.*`（grep 确认）；
  `www.pigai.org` 只作为断言用的字符串常量出现。
- `parse.py` 的 import 面只有 `json/logging/re/html.parser/dataclasses/urllib.parse`；
  `adapter.py` 只多 `datetime/urllib.parse.urljoin` 与 core/base/pigai 内部模块。**无联网依赖。**

> 我第一次的断网脚本把 `socket.socket` 整个打掉，导致 asyncio 自管道失败、41 个假 error。
> 那是**我的方法缺陷**，已修正为只封外连原语。如实记录，避免把工具噪声当成被测对象的问题。

## 6. 复核项 6：对抗性用例 —— 实际行为如实记录（首版 24 例；冻结版增量见 §11.5）

`logging.disable(CRITICAL)` 关掉噪声日志，只记行为。**「比预期更好」与「更差」都记。**

| # | 输入 | 期望/担忧 | **实测行为** | 判定 |
| --- | --- | --- | --- | --- |
| A1 | `""` / `"   "` / `None` 打三个解析器 | 应报错不静默 | list 两个抛 `HeaderMismatchError`；detail 抛 `PageShapeError`；`None` 同样 | ✅ 符合 |
| A2 | 只有表头、无数据行 | `[]` | `[]`（pending/done 均 `[]`） | ✅ 符合 |
| A3 | 表头改名 / 前两列互换 / 多一列 | 拒绝而非错位 | 三种均 `HeaderMismatchError`，消息里带**实际表头** | ✅ 符合 |
| A4 | 进度条 `td` 无 `title` | 回落 `source="width"` | `('词汇', 0.75, 'width', 150.0, 200.0)` —— ratio/两宽/source 全部正确 | ✅ 符合 |
| A4b | `title` 存在但非数字（`"not-a-number"`） | 回落或报错 | 回落 `('width', 0.75)`，并发 warning | ✅ 可接受 |
| A4c | 同 DOM 上 `title=0.6`、宽度比 0.75 | 取 title | `(0.6, 'title')` | ✅ 符合 |
| A5 | 容器宽 `0px` | 防除零 | `dims=[]`（该维度被**跳过**，不抛异常） | ✅ 无除零，但见建议-6/7 |
| A5b | 容器 200、条宽 `0px` | 防除零 | `[(0.0, 'width')]` —— 不崩 | ✅ 无除零 |
| A5c | 两宽都 `0` | 防除零 | `dims=[]` | ✅ 无除零 |
| A6 | `class="weird_class other"` / 无 `class` / `class="snt_hx great"` | 未知原样保留 | `[('weird_class','brand.new'), ('',''), ('great','c')]` | ✅ 符合 |
| A7 | `ul` 内嵌 `div`（非法） | 仍能取行 | `[('1','T')]` 正确 | ✅ 符合 |
| A7b | `li` 内嵌嵌套 `ul` | 不数重/不错位 | 直系 7 个 `li`，标题取内层文本，**未数重** | ✅ 符合 |
| A7c | 全页标签不闭合 | 不崩 | `[('9','T')]` | ✅ 符合 |
| A8a | `snt`/`short_msg` 各 20 万字符 | 不崩、可承受 | 1 条点评，snt 长度 200000，0.012s | ✅ 符合 |
| A8b | 20000 个 class token | — | level 正确取 `'great'` | ✅ 符合 |
| A8c | 嵌套 5000 层 | 无递归溢出 | score=80.0（迭代实现，不爆栈） | ✅ 比预期更好 |
| A8d | **宽度 `9…9`（400 位数字）** | 不崩 | `ratio` 变 `0.0` / `inf` / **`nan`**（见建议-6） | ⚠️ 见建议-6 |
| A9 | 重复 `#essayList` 容器 | 取一个 | 取第一个容器的行 | ✅ 符合 |
| B1 | **未完成行：标题格无锚点、操作格有锚点** | 取标题格文本 | **`title='查看'`、`url='?c=v2&a=write&rid=9999'`（跨列错位）** | ❌ 缺陷（需知会-1） |
| B2 | 待完成 `Assignment.url` 绝对化 | 正确绝对 URL | **`https://www.pigai.org?c=v2&a=write&rid=1000005`（丢了 `/index.php`）** | ❌ 缺陷（需知会-2） |
| B3 | 缺 `_JUKU_USER`、首页无登录表单 | 保守判否 | `is_logged_in() = True`（无 cookie jar 也 True） | ⚠️ 需知会-3 |
| B3b | 首页是 200「500 Internal Error」页 | 保守判否 | `is_logged_in() = True` | ⚠️ 需知会-3 |
| B4 | 合法页含子串 `lg_from_w`（如帮助文案） | 不误判 | **抛 `SessionExpiredError`**（裸子串匹配） | ⚠️ 建议-8 |
| B5 | 走完 adapter 全部只读路径 | 无写请求 | 只有两条 GET，无 `a=write` | ✅ 符合 |
| B6 | 返回登录页（pending/finished/detail 三条路径） | 抛异常非空列表 | 三条均 `SessionExpiredError` | ✅ 符合 |
| B7 | 页面结构大变（维护页） | 抛异常非空列表 | `HeaderMismatchError`（拒绝解析） | ✅ 符合 |
| E3 | `client.get()` 传站外/绝对/相对 URL | 拒绝站外 | `https://…`/`//…`/无前导 `/`/`""` 全 `ValueError` | ✅ 符合 |
| E3b | `client.get("/index.php?c=v2&a=write&rid=1")` | — | **被接受并会发请求**（守卫只挡站外主机，不校验 `a=` 动词） | ℹ️ 建议-10 |

### 最强离线证据：把解析器打在**真实 recon DOM** 上（不联网）

fixture 是重写件，所以我另外直接拿本地真实抓包 `data/recon/pigai/raw/` 喂解析器：

```
== 已完成列表 ==
行数=10 | 全部有 rid/eid=True | score 为 None 的行数=1 | 提交次数>=2 的行数=4
提交次数分布=[1,1,1,1,1,1,2,2,3,6]
样例: rid=1000006 eid=200000004 score_raw='未截止' url=/index.php?c=v2&a=view&eid=200000004&rid=1000006

== 未完成列表（U15 实锤）==
行数=1 | rid=1000005 | title/kind/author/classes/deadline 均取到（真实出题人姓名与班级串本报告不转录）
url=?c=v2&a=write&rid=1000005

== 评分详情 ==
rid=1000006 eid=200000004 score=85.5 full_marks=100.0
dims=[('词汇',0.8951008,'title',154.0),('句子',0.86631035,'title',154.0),
      ('篇章结构',0.78319855,'title',154.0),('内容相关',0.78202034,'title',154.0)]
四维全部 source=title: True
annotations=19 | level 集合=['error','error_trp'] | sid 范围=0..11
meta keys=[字数,开始时间,截止时间,批改公式,批改方式,满分] | topic/requirements/总评 均非空
fb_js 条目数=17

== 班级页 ==
条目数=3 | 1000005/0/交作业 · 1000006/1367/交作业 · 3431453/43/已截止
```

**解析器在真实（非 fixture）DOM 上端到端正确** —— 这是本次验证能给出的最强离线结论。
班级页 3 条与规格 §6.8 的实测正文（`ID：3431453 提交人数：43 截止日期：2026-09-19 23:59 已截止`）逐字吻合。

### 与 lead 基线的交叉核对

| lead 简报 | 本地真实样本可核对性 | 判定 |
| --- | --- | --- |
| `list_pending_assignments()` 1 条 | 真实 waitComplete = 1 条 | ✅ 吻合 |
| `list_finished_essays()` 10 条 | 真实 = 10 条 | ✅ 吻合 |
| 其中 4 条多稿 | 提交次数 ≥2 的 = 4 条 | ✅ 吻合 |
| 其中 1 条成绩未公布 | `未截止` = 1 条 | ✅ 吻合 |
| 四维均 `source="title"` | 真实详情四维全 `title` | ✅ 吻合 |
| ~~某篇详情 **总分 89.0/100**~~ | ~~唯一本地详情样本 = 85.5/100~~ | ✅ **撤回质疑**：lead 那篇是 `eid=234022144`（§6.10），与我核对的 `eid=200000004` 是两篇 |
| ~~按句点评 **24 条**~~ | ~~唯一本地详情样本 = 19 条~~ | ✅ **撤回质疑**：同上，24 条属 `eid=234022144` |
| ~~`level` 四类齐全~~ | ~~本地样本 = `{error,error_trp}`~~ | ✅ **撤回质疑**：`eid=200000004` 只有两类，`eid=234022144` 四类齐全；U25 的关闭依据是后者 |

~~前三项与四维来源被本地真实样本独立证实；后三项与本地唯一真实详情样本不符。~~
**更正（2026-10-19 11:52，task-11）**：~~后三项~~ 全部 8 项与规格一致 —— 后三项的对照物应是
`docs/research/pigai.md` §6.10 表格里点名的 `rid=1000003 / eid=234022144`，而我当时错用了
仓库里那唯一一篇离线样本 `eid=200000004`。**「需知会-4」撤回**，详见 §0.3。
代码侧本无碍：`ANNOTATION_LEVELS` 已含四类，未知等级原样保留（A6 已验证）。

## 7. 问题清单（按影响面分级）

### 阻断级（0 条）

无。6 个复核项的核心语义均成立，解析器在真实 DOM 上端到端正确，无写请求、无泄漏、可离线复跑。

### 需知会（4 条）

**N-1｜未完成列表「标题格无锚点」时跨列回落，静默取到操作列**（`parse.py:497-509`）
- 最小复现：`assignments_pending_row.html` 的表头 + 标题格改为纯文本、操作格保留 `<?...rid=9999>查看` 锚点。
- 实测：`rid='123' title='查看' url='?c=v2&a=write&rid=9999'`；期望 `title='真正的标题（纯文本，无 a）'`。
- 原因：`anchor = _first_anchor(cells[1]) or _first_anchor(cells[6])`，随后拿**同一个 anchor** 同时当标题与 URL 来源。
- 影响：违反本项目自定的「宁可少取不可错位」纪律（§6.2）。真实 DOM 标题格当前恒有锚点，故线上暂不触发；
  一旦站点省略标题链接，就会静默产出**错标题 + 指向别处的 URL**，而非跳过该行。
- 建议修法：`anchor` 只从标题格取；取不到就退回 `cells[1]` 文本，URL 留空或另从操作格单独取并**不覆盖标题**。

**N-2｜待完成作业的 `Assignment.url` 绝对化丢掉 `/index.php`（`adapter.py:238`，`_to_pending_assignment`）**
- 最小复现：`urljoin("https://www.pigai.org", "?c=v2&a=write&rid=1000005")`
  → `'https://www.pigai.org?c=v2&a=write&rid=1000005'`。
- 页面 href 是**相对当前页**（`/index.php?c=write&a=waitComplete`）的查询串，正确绝对地址应为
  `https://www.pigai.org/index.php?c=v2&a=write&rid=1000005`。已完成列表因 href 带前导 `/` 而**没有这个问题**。
- 影响：`url` 在 docstring 里声明「原样绝对化仅供展示」，原始 href 另存于 `raw`/`raw_cells`，数据未丢；
  但展示出来的链接是坏的。降级为非阻断。
- 建议修法：以 `protocol.Endpoints.ASSIGNMENTS_PENDING` 为 base 做 urljoin，或保留原样 href 由消费方绝对化。

**N-3｜`is_logged_in()` 与规格的登录成功判据不一致（`client.py:166-179`；被 `base.py:143-148 refresh_session` 使用）**
- 规格 §4.4 的判据是**两条同时成立**：存在 `_JUKU_USER` cookie **且** 落地页不含 `lg_from_w`。
  `login()` 两条都查（正确），但 `is_logged_in()` **只查后者**，完全不看 cookie。
- 实测：无 cookie jar（`FakeTransport`）→ `True`；首页是 200 的「500 Internal Error」页 → `True`。
- 影响：任何**不含登录表单**的 200 页面（错误页/维护页/风控页）都会被判成「会话有效」，
  于是 `refresh_session()` 直接返回、不重登；后续才由 `_guard_session`/`HeaderMismatchError` 兜住。
  后果被兜底，但会让「登录态判断」这个语义失准。
- 建议修法：`is_logged_in()` 加一条 cookie 判据（与 `login()` 对称），或至少把它降级为「页面探针」并改名/注明。

**N-4｜~~lead 基线里的详情页数字与本地唯一真实样本不自洽~~ —— 已撤回（是我比错样本）**
- ~~本地真实样本：85.5/100、19 条点评、level∈`{error,error_trp}`；lead 简报：89.0/100、24 条、四类齐全。~~
- **撤回（2026-10-19 11:52）**：lead 基线来自 `rid=1000003 / eid=234022144`（规格 §6.10 表格里写明），
  我核对的离线样本是 `eid=200000004`。两篇作文，两组数字都对；U25 的关闭依据是前者。
- 详细更正见 §0.3；本轮不构成缺陷，也不影响任何代码结论。

### 建议（6 条）

**S-5｜~~`protocol.py` 仍引用已撤回的 U15 结论~~ —— ✅ 冻结版已订正（§11.2.5）**
~~（`protocol.py:79`「已核验（0 行）」、`protocol.py:190`「行级 DOM 未实测（推断）」）~~
冻结版 `protocol.py:87` 写「已核验（行级 DOM 亦已实测）」，`AssignmentRow` 的 docstring 改为
「行级 DOM 已实测」并附勘误说明。**已关闭。**

**S-6｜宽度为「超大数字」时 `ratio` 可变成 `0.0` / `inf` / `nan`**（首版 `parse.py:575-623`）
- `_style_px` 把 `"9"*400 + "px"` 交给 `float()` → `inf`；守卫 `not container_width` 只挡 0，**不挡非有限值**。
- 实测：容器 `inf` → `ratio=0.0`；条宽 `inf` → `ratio=inf`；两者 `inf` → `ratio=nan`（均 `source='width'`）且不抛异常。
- 影响：极端/畸形 DOM 才触发，真实页面不会；但 `nan` 一旦写进 DB/参与算术会污染下游。
- 建议：`math.isfinite()` 守卫，非有限则跳过该维度并 warning。

**S-7｜容器宽为 0 时维度被静默跳过，且日志文案误导**（`parse.py:582-583`）
- 实测 `container=0px` → `dims=[]`；日志说「宽度读不到」，但其实是宽度**为 0**。0 与缺失是两种
  「页面不对劲」的信号，建议区分措辞，并考虑是否为 0 时降级为 warning + 计数。

**S-8｜会话失效判据用裸子串 `lg_from_w`**（`parse.py:89`、`client.py:146,175`）
- 实测：一个**合法**页面仅因脚本文案含 `"lg_from_w"` 就抛 `SessionExpiredError`。
- 现状不紧急：真实 `home.html` 里该串只出现 1 次且在 `<form id="lg_from_w">` 上；真实详情页 0 次。
- 建议：按 `id="lg_from_w"` 或表单标签匹配，别用裸串。

**S-9｜作者的写入面静态用例覆盖面不足**（`tests/test_pigai_adapter.py::TestNoWriteSurface`）
- 只审计 `adapter.py` + `protocol.py`，未覆盖 `parse.py` / `client.py` / `rsa_login.py` / `__init__.py`；
  任务书要求的是「`platforms/pigai/` 全部源码」。全包 AST 审计我已代跑（结论：可执行字符串 0 命中），
  建议把这层审计写进用例，别依赖人工。

**S-10｜`client._check_read_only_path` 只挡站外主机，不校验 `a=` 动词**
- 实测 `client.get("/index.php?c=v2&a=write&rid=1")` 被接受并会真的发请求。
- 这是**刻意的轻量设计**（只防开放重定向），只读保证靠「调用方只传 `protocol.Endpoints`」。
  当前 adapter 也确实只传这三条端点。建议在 docstring 里点明这条前提，避免后来者误以为守卫能挡写端点。

**S-11｜`parse_class_rows` 的未上收选择器 + 规格 §6.8「未解剖」表述已过时**
- `parse.py` 自己声明 `.baseinfo`/`ul.detail`/`.m-score` 不在 `protocol.py`、待 lead 上收 —— 诚实。
- 但我核对 `data/recon` 后发现这些结构**真实存在**（baseinfo×3 / detail×6 / m-score×4），
  即证据比规格 §6.8 声称的更强。建议上收常量到 `protocol.py` 并更新 §6.8。
- 附带信息：`parse_class_rows` 与 `Endpoints.essay_history` / `request_detail` 目前**未被 adapter 接线**。

## 8. 我没能验证的东西（诚实列出）

1. **一切线上行为**：真机登录、302 重定向链在真实 `UrllibTransport` 下的表现、cookie 生命周期与
   `HttpOnly/Secure`、风控/验证码、站点频率限制、分页 —— 全部未验证（我按约束全程离线）。
2. **`data/recon` 之外的真实页面变体**：表头未来改名/换序、成绩列出现「--」「缺考」等新文字形态、
   多行待完成页（只有 1 行样本）、多稿 `history` 页（无解析器，未验证）。
3. **`login()` 的重定向行为**：`FakeTransport` 不模拟 302，真实 `urllib` opener 的 POST→GET 重定向
   语义我只按代码与规格推断，**未实测**。
4. **`_JUKU_USER` 的语义**：值、长度、能否脱离 `PHPSESSID` 独立复用（规格 U18/U20/U21）—— 未验证。
5. **`parse_fb_js` 的内容正确性**：只核对了条目数（真实页 17 条），未逐条核对 `category/txt/sids` 语义。
6. **A/B/C 新能力**（lead 刚通知）：本报告**不含**其验证；等落地后做增量复核。
7. **限速**：`client.py` 明确不限速（交给 `Scheduler`），我没有压测 scheduler 与 pigai 的配合。
8. **其他窗口**：`core/models.py` 有改动（`QuestionType.IMAGE` / `RunStatus.NEEDS_HUMAN` 等，pinco 窗口的），
   我确认其中**无 pigai 泄漏**（共享区 grep `pigai` = 0 命中），但没有验证那些改动本身。

## 9. 对作者「实测 vs 推断」标注是否老实的判断

**总体判断：老实，且质量高于一般水平；只有 protocol.py 一处未同步勘误。**

| 位置 | 标注 | 我的判断 |
| --- | --- | --- |
| `parse.parse_assignment_rows` docstring | 把「表头实测 / 行级模板实测 / 分页与字段语义推断 / raw_cells 保底」**分四类**写清，还主动指出与 §6.1/U15 旧结论不一致并说明以重取样本为准 | **最老实的一处**；我用 raw 核对，逐条属实 |
| `parse._parse_dimensions` | 明写「§6.4 的 154px 只是那一个样本，故绝不硬编码」，代码确实从 DOM 读两宽 | 老实（我验证了容器宽换成 200 也正确） |
| `parse.parse_class_rows` | 明写 §6.8「未解剖完整 DOM」、选择器未上收 | 老实，甚至**低估**了自己的证据（真实样本有这些结构） |
| `parse.parse_fb_js` | 明写「不是 protocol 契约的一部分、是加分项 API」 | 老实 |
| `adapter.py` 模块 docstring | 明确写出只读范围、`remote_id=rid` 的不可变约定、`Pipeline` 错配警告 | 老实；`remote_id` 约定已按 integration-guide §8 写进 docstring |
| `adapter._to_pending_assignment` | 注释说 URL「原样绝对化仅供展示」 | 诚实披露了性质，但**未发现绝对化结果是坏的**（N-2） |
| `tests/*` | 边界样本集中标注「**非实测**，仅用于测边界」；fixtures 头部写明保留/替换/缩减 | 老实 |
| `protocol.py` | 两处沿用已撤回的 U15 旧结论（「0 行」「未实测」） | 首版判「未同步勘误」（S-5）；**冻结版已订正**（§11.2.5） |
| lead 简报详情数字 | 89.0 / 24 条 / 四类 level | ✅ **首版质疑撤回**：那是我比错样本（§0.3）。lead 简报与规格 §6.10 一致，标注属实 |

特别值得肯定的两点：`rsa_login.py` 把「上限 117 字节」与「异常只报长度不报明文」都落到了代码与测试；
`parse.py` 与 `adapter.py` 的推断项**都带 §6.x 出处**，可回溯。

## 10. 复现命令汇总

```bash
cd /home/hb/刷题刷课

# 1. 全量 + 分组测试（基线 md5 见 §0）
PYTHONPATH=src python3 -m unittest discover -s tests -t .
PYTHONPATH=src python3 -m unittest tests.test_pigai_login tests.test_pigai_parse tests.test_pigai_adapter

# 2. 断网复跑（只封外连原语，保留 socketpair）
PYTHONPATH=src python3 - <<'PY'
import asyncio, ssl, http.client, unittest, socket
def boom(*a, **k): raise RuntimeError("network blocked")
socket.create_connection = boom; socket.getaddrinfo = boom; socket.gethostbyname = boom
http.client.HTTPConnection.connect = boom; http.client.HTTPSConnection.connect = boom
loader = unittest.TestLoader()
suite = unittest.TestSuite([loader.loadTestsFromName(m) for m in
    ("tests.test_pigai_login","tests.test_pigai_parse","tests.test_pigai_adapter")])
r = unittest.TextTestRunner(verbosity=0).run(suite)
print("successful =", r.wasSuccessful(), "| tests =", r.testsRun)
PY

# 3. 真实 recon DOM 上跑解析器（离线，不联网）
PYTHONPATH=src python3 - <<'PY'
import logging; logging.disable(logging.CRITICAL)
from pathlib import Path
from kedaibiao.platforms.pigai import parse
raw = Path("data/recon/pigai/raw")
print(len(parse.parse_essay_rows((raw/"b_index_php_c_write.html").read_text(encoding="utf-8"))))
print(len(parse.parse_assignment_rows((raw/"b_index_php_c_write_a_waitComplete.html").read_text(encoding="utf-8"))))
d = parse.parse_essay_detail((raw/"b_index_php_c_v2_a_view_eid_200000004_rid_1000006.html").read_text(encoding="utf-8"))
print(d.score, [x.source for x in d.dimensions], len(d.annotations))
print(len(parse.parse_class_rows((raw/"b_index_php_c_write_a_myclass.html").read_text(encoding="utf-8"))))
PY

# 4. 基线钉桩
md5sum src/kedaibiao/platforms/pigai/{protocol,parse,adapter,client,rsa_login}.py tests/test_pigai_{login,parse,adapter}.py
```

---

## 11. 增量复核（task-11，冻结版 revision）

- 复核时间：**2026-10-19 11:45–11:52（Asia/Shanghai）**
- 对象：§0.2 的**冻结版** md5（复核前逐条 `md5sum` 比对，**10 个文件全部一致**）
- 方式：仍在**完全离线**下进行（封死外连原语后跑测试；只读本地 `data/recon/` 既有抓包）
- 范围：task-6 / task-9 / task-10 的 A/B/C 新能力 + 我在 task-5 报的问题的修复情况 + 增量对抗

### 11.0 更正首版对 lead 基线的质疑 —— ✅ 已撤回（我比错了样本）

裁判结论：**lead 基线与我核对的离线样本都对，是两篇不同的作文。**

| | rid / eid | 总分 | 点评数 | level 集合 | 规格出处 |
| --- | --- | --- | --- | --- | --- |
| 我首版核对的离线样本 | `1000006` / **`200000004`** | 85.5 | 19 | `{error, error_trp}` | §6.3 / §6.5 |
| **lead 基线（U25 关闭依据）** | **`1000003` / `234022144`** | **89.0** | **24** | **`{error, error_trp, great, warn}`** | **§6.10** |

`docs/research/pigai.md` §6.10 的表格**点名了 `eid=234022144`**，其后还有一段 ⚠ 注专门提醒
「不要把它和离线样本混为一谈」——**是我没有先按 eid 对齐**。撤回首版「需知会-4」，
并在 §0.3、§6、§7、§9 四处做了更正标注。**此项不构成任何缺陷。**

### 11.1 复核方式与命令

```bash
# 冻结版 md5 自检（必须与任务书一致，实测全部一致）
$ md5sum src/kedaibiao/platforms/pigai/*.py tests/test_pigai_*.py scripts/pigai_read.py
$ md5sum tests/fixtures/pigai/*.html

# 全量 + 归属
$ PYTHONPATH=src python3 -m unittest discover -s tests -t .
$ PYTHONPATH=src python3 -m unittest tests.test_pigai_login tests.test_pigai_parse tests.test_pigai_adapter

# 断网复跑（只封外连原语，保留 socketpair，否则会误伤 asyncio 自管道）
# 真实 recon DOM 上跑新能力；注入式验证 AST 审计判别力（临时目录，仓库零改动）
```

### 11.2 task-5 问题逐条复核

| # | 首版问题 | 冻结版结论 | 关键证据 |
| --- | --- | --- | --- |
| **N-1** | 未完成行跨列回落（标题/URL 错位） | ✅ **已修** | 原最小复现 → `title='真正的标题（纯文本，无 a）'`（不再变成「查看」）；**反向**：操作格无锚点 → `url=''`（**不**借标题格）；rid 兜底只认标题格 href |
| **N-2** | `urljoin` 丢 `/index.php` | ✅ **已修** | `_absolute("?c=v2&a=write&rid=1000005")` → `https://www.pigai.org/index.php?c=v2&a=write&rid=1000005`；`_absolute("/index.php?…")` → 正确；`_absolute("")` → `https://www.pigai.org/`；`list_assignments()` 合并后 **4/4 条 url 全以 BASE_URL 开头** |
| **N-3** | `is_logged_in()` 用否定判据、忽略陈旧 cookie | ✅ **已修（改正面判据）** | 最坏场景：**匿名失效页 + jar 里塞陈旧 `_JUKU_USER`** → `is_logged_in=False`（三份真样本各验一遍）；已登录页（无 jar）→ `True`；探针是 `GET /index.php?c=write`；HTTP 503 → `False`；**网络异常 → `OSError` 上抛**（可被 Scheduler 重试，未被吞） |
| **S-5** | protocol 仍引用已撤回的 U15 | ✅ **已订正** | `protocol.py:87` = 「已核验（行级 DOM 亦已实测）」；`AssignmentRow` docstring 改为「行级 DOM 已实测」并附勘误说明。全包 grep「0 行」/「未实测」**只剩两处、且都是勘误自述**（`protocol.py:252`「初版注释曾写…对应已撤回的 U15」、`parse.py:566`「这与 §6.1/U15 旧结论不一致…以样本为准」）——**不再有把撤回结论当依据的表述** |
| **S-6** | 宽度 `inf` 令 `ratio` 变 `inf`/`nan` | ✅ **已修** | 容器/条宽任一非有限 → 该维度跳过；`ratio` 再过一次 `isfinite` 兜底。见 11.2.6 的 10 组矩阵，**无一组产出 `nan`/`inf`** |
| **S-7** | 容器宽 0 的日志把「0」说成「读不到」 | ✅ **已修** | 三种措辞分开：缺失→「宽度读不到」/ 非有限→「不是有限值」/ `==0`→「容器宽为 0，无法计算宽度比」 |
| **S-8** | 裸子串 `lg_from_w` 误判会话失效 | ✅ **已修**（有残留边界，见 11.6 建议-12） | 合法页仅脚本提及 `lg_from_w` → **不再抛**；真登录页 `id="lg_from_w"` → `SessionExpiredError` |
| **S-9** | 静态审计只覆盖 2 个文件 | ✅ **已修 + 判别力已证** | 见 11.2.9 |
| **S-11** | `parse_class_rows` 未接线 | ➖ **如实保留**（任务书不要求修） | `adapter.py` 仍不引用 `parse_class_rows`；`myclass` 选择器仍未上收 protocol |

#### 11.2.6 新语义重点打：**`title` 优先级与宽度解耦**（task-10 裁定）

10 组宽度矩阵（`DOC` 里带 `#rescoreTxt`，每例一个维度）：

| 容器宽 | 条宽 | `td[title]` | 实测结果（ratio, source, bar, container） | 判定 |
| --- | --- | --- | --- | --- |
| 200 | 0 | 无 | `(0.0, 'width', 0.0, 200.0)` | ✅ 不崩（**但违反不变式，见 11.4**） |
| 200 | 150 | 无 | `(0.75, 'width', 150.0, 200.0)` | ✅ |
| **inf** | 150 | 无 | `[]`（跳过） | ✅ 不再是 0.0 |
| 200 | **inf** | 无 | `[]` | ✅ 不再是 inf |
| inf | inf | 无 | `[]` | ✅ 不再是 nan |
| 0 | 150 | 无 | `[]` | ✅ |
| **inf** | 150 | **`0.6`** | **`(0.6, 'title', 150.0, inf)`** | ✅ **正是本轮新语义**：宽度坏掉不影响权威 title |
| **0** | 0 | **`0.6`** | `(0.6, 'title', 0.0, 0.0)` | ✅ title 保留，宽度如实记 0.0 |
| 200 | 无属性 | **`0.6`** | `(0.6, 'title', 0.0, 200.0)` | ✅ 同上 |
| 200 | 150 | **`"9"*400`（→inf）** | `(0.75, 'width', …)` | ✅ title 自身非有限 → 视为没有，走兜底 |

#### 11.2.9 `TestNoWriteSurface` 的判别力（**证明而非声称**）

用测试**自己的检查器** `TestNoWriteSurface._executable_strings` 在一个**临时目录**里跑（仓库零改动）：

```
覆盖文件: ['__init__.py','adapter.py','client.py','parse.py','protocol.py','rsa_login.py']
  注入 parse.py     -> 检出 ['a=write']  ✓ 有判别力
  注入 client.py    -> 检出 ['a=write']  ✓
  注入 rsa_login.py -> 检出 ['a=write']  ✓
  注入 __init__.py  -> 检出 ['a=write']  ✓
  注入 adapter.py   -> 检出 ['a=write']  ✓
  注入 protocol.py  -> 检出 ['a=write']  ✓
口径核对: docstring 里的 a=write -> 不报（符合文档）；可执行字符串里的 -> 报
真实包现状: offenders = 无
```

**任务书要求「可临时注入违规字符串验证，但不要在仓库里留改动」——已按要求完成**：
注入只写进 `tempfile.TemporaryDirectory()`，随后 `git status` 确认 pigai 目录无改动。

### 11.3 新增能力 A/B/C —— 在**真实 recon DOM** 上端到端验证

| 能力 | 实测（真实抓包，离线） | 判定 |
| --- | --- | --- |
| **A. `EssayDetail.essay_text`** | 详情页 `#contents` → **1070** 字符 | ✅ |
| **B. `parse_write_page` / `WritePage`** | `b_write_with_draft.html` → `rid=1000006 eid=200000004 text=1070`；`b_write_fresh.html` → `text='' eid='' rid='1000005' has_draft=False` | ✅ |
| **C. `fetch_assignment_by_rid`** | 实际请求 `GET https://www.pigai.org/index.php?c=write&a=searchReq&rid=1000006`（**读语义端点，无 `a=write` 字面量**）→ `WritePage` 同上 | ✅ |
| 两页正文一致性 | 详情页 `essay_text ==` 写作页 `essay_text` → **逐字相同（1070）** | ✅ 与规格 §6.3/§6.11 一致 |
| 写作页其它字段 | `title='示例B层次大英I（三）- B2U5'`、`word_range='100~200'`、`full_marks=100.0`、`deadline='2026-10-19 23:59'`、`server_time='2026-10-19 11:16:06'`、`no_paste=True`（只读取、不利用） | ✅ |

#### 11.3.1 契约「只按 id 匹配」的必要性（**专门构造反证**）

模拟「先按标签过滤、再取 `id=contents`」：

| 页面 | `#contents` 真实标签 | 按 `div` 过滤 | 按 `textarea` 过滤 | 按 `TEXTAREA` 过滤 |
| --- | --- | --- | --- | --- |
| 详情页 | `div` | **命中 1070** | **0 命中（读到 0 字）** | **0 命中** |
| 写作页 | `textarea`（树里已被 `html.parser` 小写化） | **0 命中** | **命中 1070** | **0 命中** |

→ **反证成立**：任何按标签硬过滤都会在其中一种页面上读到 **0 字**；只有「只按 id」两种页面都对。
另外 `WRITE_BODY_TAG="TEXTAREA"` 是**大写**的，而树里的标签是小写 `textarea`
（`_body_text` 内部用 `.lower()` 归一，且**只用于 debug 日志、不参与过滤**）——
这进一步说明「标签常量只能当文档，不能当选择器」。

#### 11.3.2 模板 textarea 不会被误取

写作页真实样本里有 **3 个** `class="section-content draft-top|main|tail"` 的模板 textarea；
另用「模板有内容 + 真正文」的合成页验证：`parse_write_page(...).essay_text == '我的真实正文'`（**未误取模板**）。
`#beizhu` 同理。

### 11.4 `Dimension.source` 不变式 vs 代码 —— ⚠️ **发现一处不一致**

`protocol.Dimension` docstring 写：`source == "width"` ⇒ `bar_width` / `container_width`
**一定**是**有限正数**且 `ratio == bar/container`。代码逐条核对：

| 不变式条款 | 代码是否满足 | 说明 |
| --- | --- | --- |
| `width` ⇒ `container_width` 有限正数 | ✅ | 显式排除非有限 与 `==0` |
| `width` ⇒ `ratio == bar/container` | ✅ | 单次除法后即赋值 |
| `width` ⇒ **`bar_width` 是有限正数** | ❌ **不成立** | `ratio == bar/container` 只要求 `container != 0`；**条宽可以为 `0.0`** |
| `title` ⇒ `ratio` 与宽度无关、宽度可能为 `0.0` | ✅ | `inf` 宽度也照用（本轮新语义），`ratio` 恒有限 |

最小复现（容器 200px、进度条 0px、无 `title`）：

```python
parse.parse_essay_detail(DOC + dim(cw=200, bw=0, title=None)).dimensions
# -> (Dimension(name='词汇', ratio=0.0, bar_width=0.0, container_width=200.0, source='width'),)
#    source=='width' 但 bar_width == 0.0，与 docstring 的「一定…有限正数」冲突
```

影响面：**消费方若按 docstring 断言 `bar_width > 0`**（例如拿它算像素、或据此判断「这段数据可信」）
会在「某一维真的得 0 分」这种**合理页面**上失败。记为 **需知会-6**。修法二选一：
把不变式改成「**非负**有限数」，或让 0 宽度的兜底路径也跳过。

### 11.5 增量对抗用例（首版 24 例之外，本轮 +24 例中的代表）

| # | 输入 | 实际行为 | 判定 |
| --- | --- | --- | --- |
| I1 | **详情页**喂给 `parse_write_page` | `PageShapeError`（消息里点明 `#contents=True` 但缺写作页独有锚点） | ✅ 不静默半空 |
| I2 | 只有 `#contents` 的页 → `parse_write_page` | `PageShapeError` | ✅ |
| I3 | **真登录页** → `parse_write_page` | `PageShapeError` | ✅ |
| I4 | **写作页（有稿/空稿）**喂给 `parse_essay_detail` | 两者均 `PageShapeError` | ✅ |
| I5 | 无 `var _eid` + 传入 `eid='PARAM'` | `eid='PARAM'`（回落参数） | ✅ |
| I6 | **`var _eid = ''`** + 传入 `eid='PARAM'` | **`eid=''`**（页面明确说「没写过」，**不**回落参数） | ✅ 正是契约要的语义 |
| I7 | `var _eid='REAL'` + 传入 `eid='PARAM'` | `eid='REAL'`（页内变量优先） | ✅ |
| I8 | 无 `var _rid` + 传入 `rid='9'` / `var _rid=''` + 传入 `'9'` | `'9'` / `''` | ✅ 同一套语义 |
| I9 | 容器宽可读、**进度条无 width 属性**、无 title | 该维度**跳过**（`()`） | ✅ |
| I10 | 同上但**有 title** | `(0.6,'title', bar=0.0, container=200.0)` | ✅ 权威值不受宽度影响 |
| I11 | `#contents` 里混入工具条文案 | `essay_text` **原样包含** `[工具条] 保存 提交 字数:123正文第一段。` | ⚠️ 见建议-13（取舍，非缺陷） |
| I12 | 模板 textarea 有内容 + 真正文 | 取到 `'我的真实正文'` | ✅ |
| I13 | 宽度 `"9"*400` + 有 title | 采用 title（见 11.2.6） | ✅ |
| I14 | 空白/`None`/纯非目标页 | 一律 `HeaderMismatchError` / `PageShapeError`，无静默 `[]` | ✅ |

### 11.6 本轮新增问题

#### 需知会（2 条）

**N-5｜真实的「会话失效页」不会触发 `SessionExpiredError`**（`adapter._guard_session`）
- 规格 §4.5 的匿名实测明确：失效页（`b_anon_write.html` / `b_anon_waitComplete.html`，200 + JS 跳转）
  **既不含 `id="lg_from_w"`、也不含 `#essayList`**；而 `_guard_session` 只认 `LOGIN_FORM_MARKER_STRICT`。
- 实测（把真样本喂给 adapter 四条只读路径）：

```
b_anon_write.html -> list_pending_assignments  → HeaderMismatchError
b_anon_write.html -> list_finished_essays      → HeaderMismatchError
b_anon_write.html -> fetch_essay_detail        → PageShapeError
b_anon_write.html -> fetch_assignment_by_rid   → PageShapeError
正控: b_loginform.html -> list_pending_assignments → SessionExpiredError ✓
```

- **不是静默失败**（仍显式抛错，满足 integration-guide §6.2），但**诊断错误**：调用方看到的是
  「找不到 #essayList / 找不到锚点」，而不是「会话已失效」；`is_logged_in()` 已能正确判 False，
  所以走标准流程（先探针再取数）时问题被掩盖，**直调数据方法时才暴露**。
- 建议：列表类路径在**缺 `#essayList` 且缺其它锚点**时升级为 `SessionExpiredError`，
  或让 `_guard_session` 同时接受「正面标记缺失」这一判据（与 `is_logged_in` 同源）。

**N-6｜`Dimension.source == "width"` 时 `bar_width` 可以是 `0.0`，与 docstring 不变式冲突** —— 见 §11.4。

#### 建议（4 条）

**S-12｜严格标记只认一种精确写法**（`_guard_session` / `_LIST_MARKER`）

```
id="lg_from_w"        -> 拦下 ✓
id = "lg_from_w"      -> 不拦 ⚠        id='lg_from_w'  -> 不拦 ⚠
ID="lg_from_w"        -> 不拦 ⚠（裸子串比对，未做大小写/空白归一）
```

当前三份真实样本都是精确形态，故不紧急。注意方向性：对 `_guard_session` 是「漏判会话失效」
（后果同 N-5）；对 `is_logged_in` 是**保守方向**（误判未登录 → 多一次重登），可接受。
建议改成属性级解析（在树里查 `id` 属性）而不是裸串比对。

**S-13｜`#contents` 内的非正文文案会原样进 `essay_text`**：实测把工具条塞进容器就会读到
`[工具条] 保存 提交 字数:123正文第一段。`。这与契约「除 `strip()` 外不归一化」的取舍一致，
且真实样本里 `#contents` 只包 `#parahtml`（我核对的样本如此）；但建议在 docstring 记明这一边界，
免得后来者以为正文一定干净。

**S-14｜`_absolute()` 对非 http 形态的 href 原样放行**：`javascript:;` → `'javascript:;'`、
`http://evil.example/x` → 原样。真实数据里操作列 href 恒为 `?c=…`，故未触发；但
`Assignment.url` 是给消费方点开的，建议对非 `http(s)`/非站内结果留空或标注。

**S-15｜`parse_class_rows` 与 `Endpoints.essay_history`/`request_detail` 仍未接线**（首版 S-11 的续）：
按要求如实标注，不要求修。`myclass` 的选择器仍以 module-private 常量落在 `parse.py`。

### 11.7 全量测试归属（如实）

```bash
$ PYTHONPATH=src python3 -m unittest discover -s tests -t .
Ran 739 tests in 3.696s
FAILED (failures=5, errors=38)
```

按 `test.id()` 归属：

| 模块 | FAIL | ERROR | 合计 |
| --- | --- | --- | --- |
| **`tests.test_pinco`** | **5** | **38** | **43** |
| 其它（含 pigai 三件套） | 0 | 0 | **0** |

- **pigai 相关失败数 = 0**（三件套：`FAIL=0 ERROR=0`）。
- 43 条**全部落在 `tests.test_pinco`** —— 即任务书所说**另一个窗口正在编辑**的 pinco 模块
  （例：`TestParseBusinessData.test_parse_questions_reads_option_letters_in_order` 期望 2 个选项、实得 `[]`；
  `TestAdapterOffline.test_list_courses_and_assignments` 等）。**与 pigai 无关，我未做任何修改。**
- pigai 三件套单独跑：

```bash
$ PYTHONPATH=src python3 -m unittest tests.test_pigai_login tests.test_pigai_parse tests.test_pigai_adapter
Ran 148 tests in 0.120s
OK
```

- 断网复跑（封死 `create_connection`/`getaddrinfo`/`gethostbyname`/`HTTPConnection.connect`）：

```
pigai x3 断网: successful = True | tests = 148 | errors = 0 | failures = 0
```

（首版是 100 条，冻结版新增 48 条；全部离线可跑。）

- 其余 5 项在冻结版上的复验证据：**§2** 凭据值全仓 **0 命中**、**7 个 fixture**（含 2 个新增）
  手机号/邮箱/真实姓名/≥40 英文正文/cookie 名**全清**（含禁用串
  `Stay at Home` / `Going Out for Pleasure` / `jukuu.com` / `张老师` 等）；**§3** 全包 grep 仅
  `client.py:247`、`parse.py:291/578` 三处 **docstring** 散文，可执行字符串 **0 命中**（AST 复验，§11.2.9）；
  **§4** 公钥/`#rescoreTxt`/表头列序逐字一致，新增常量 `DETAIL_BODY_ID='contents'`、
  `DETAIL_BODY_TAG='div'`、`WRITE_BODY_TAG='TEXTAREA'`、`LOGIN_FORM_MARKER_STRICT='id="lg_from_w"'`
  与规格 §4.5/§6.3/§6.11 一致，`lookup_by_rid` 用 `a=searchReq`（读语义）且无写端点字面量。

### 11.8 本轮仍未验证的

1. **线上一切**：`fetch_assignment_by_rid` 的真实响应、`lookup_by_rid` 的「GET 无副作用」结论、
   24 条点评那篇（`eid=234022144`）—— 全部是 lead 的线上观察，我**无法复现**（我全程离线）。
2. **写作页正文的回写闭环**：只验证了「读得回」（1070 逐字一致），未验证任何写回。
3. **`#contents` 的真实标签是否恒为 `div`/`TEXTAREA`**：我只核对了 3 份样本；站点若换标签，
   「只按 id」的策略仍然成立（这正是它的价值），但 `DETAIL_BODY_TAG`/`WRITE_BODY_TAG` 会失准。
4. **`_no_paste` 的语义**：只读取为 True/False，未（也不应）验证其服务端是否真被强制。
5. **多稿 `history` 页**：仍无解析器，未验证。
6. **测试仓库其它窗口**：pinco 的 43 条失败我只做归属，未分析其成因（不属于我的范围）。

### 11.9 本轮对作者标注的判断（补充）

- **修复质量高、且都带了回归用例**：N-1 的双向不回落、N-2 的 `_absolute` 用例、N-3 的
  「probe 完全忽略 cookie jar」用例，都是我首版最小复现的直接对应物；`parse.py` 的
  `new-semantic` 注释明确写了「lead 裁定（task-10）」和理由，**可追溯**。
- **文档与代码同步的意识明显提高**：`Dimension` 把不变式写进 docstring（虽然有一处与代码不符，N-6）、
  `LOGIN_FORM_MARKER` 保留裸串但**显式标注「仅供文档与注释引用」**、`is_logged_in` 的
  「有意不对称、请勿统一」写成了设计说明而非辩解 —— 这些都是诚实标注的样子。
- **仍有一处「声称强于事实」的地方**：`Dimension` 不变式里「一定…有限正数」（N-6）。
- 首版我误判 lead 的地方已在本轮更正；这也说明**「实测 vs 推断」的标注不只要看被验证方，
  验证方自己也要先对齐样本标识（eid）再下结论。**

### 11.10 结论

**冻结版无阻断级缺陷。** task-5 的 8 条问题里 **7 条已修且复验通过**、1 条（S-11）按要求保留；
新增 2 条需知会（N-5 会话失效页诊断、N-6 不变式与代码不符）与 4 条建议。
A/B/C 三项新能力在真实 recon DOM 上端到端正确，「只按 id」的契约约束经反证确认必要。
pigai 三件套 **148 tests 全绿**（断网亦然），全量套件的 43 条失败**全部归属 `tests.test_pinco`**。

---

## 12. 第 2 版增量复核（task-12）

- 复核时间：**2026-10-19 11:59–12:03（Asia/Shanghai）**
- 对象：**第 2 版**（protocol / adapter / test_pigai_adapter / docs 共 4 个文件变更）
- 方式：仍**完全离线**（未访问 `www.pigai.org`；只读 `data/recon/pigai/raw/` 既有抓包与 `tests/fixtures/pigai/`）

### 12.0 基线确认：4 个变更 + 7 个逐字节未变 ✅

复核前 `md5sum` 逐条比对，**与任务书完全一致**：

| 文件 | 本轮 md5 | 状态 |
| --- | --- | --- |
| `src/kedaibiao/platforms/pigai/protocol.py` | `34a64e91b8456beb35ef0375a6af34fd` | **已变更**（482 行） |
| `src/kedaibiao/platforms/pigai/adapter.py` | `486ca0c9d1b100713535982227ecc6a5` | **已变更**（313 行） |
| `tests/test_pigai_adapter.py` | `843f14bea0a83faf9dc7bf3410a9a362` | **已变更**（441 行） |
| `docs/research/pigai.md` | `ec972b4516df16ce6df5c47272b52ecd` | **已变更**（769 行，仅文档） |
| `parse.py` / `client.py` / `rsa_login.py` / `__init__.py` / `test_pigai_login.py` / `test_pigai_parse.py` / `scripts/pigai_read.py` | `ec961a70… / d791255c… / c8b23f41… / d087d025… / d734dd76… / 61b0e032… / 03ed5bba…` | **与第 1 版冻结逐字节一致** ✅ |

轮次记为**第三轮**并已加进 §0 的轮次表；§11 的第二轮结论仍然有效。

### 12.1 需知会-5（会话失效页误诊）—— ✅ **已修，且我用真实样本打穿**

**1) 两份真实失效样本 × 四条只读路径 = 8/8 全部 `SessionExpiredError`**（不再是 HeaderMismatch/PageShape）：

```
b_anon_write.html        pending  -> SessionExpiredError ✓   finished -> ✓   detail -> ✓   lookup -> ✓
b_anon_waitComplete.html pending  -> SessionExpiredError ✓   finished -> ✓   detail -> ✓   lookup -> ✓
```

**2) `_guard_session` 对 `data/recon/pigai/raw/` 全部 15 份 HTML 样本的判定**（宽于任务书要求的 7 份）：

```
b_anon_write.html / b_anon_waitComplete.html      SessionExpiredError  ← 命中 a=loginForm（正确，2/2）
home.html / b_home_prelaunch.html / b_loginform.html SessionExpiredError ← 命中 id=lg_from_w（正确：都是登录页）
其余 10 份（detail / write / history / myclass / waitComplete / after_login /
          write_fresh / write_with_draft / guest2016 / login.html）   放行   ← 0 误判
```

**3) `a=loginForm` 全域普查（误报率）**：15 份样本里该串**只出现在 2 份失效页**（各 3 次，均为
JS 跳转目标与「3 秒后自动跳转」链接），**其余 13 份 0 命中** —— 比任务书说的「合法页 0/7」更宽，
**误报率 0/13**。选路由参数而非中文文案的判断成立（中文文案会随 i18n/改版漂移，路由参数不会）。

**4) 建议-12（写法变体）**：作者列的 4 种 + 我另加的 3 种**全部拦下**，裸串负例放行：

```
id="lg_from_w" ✓   id='lg_from_w' ✓   id = "lg_from_w" ✓   ID="lg_from_w" ✓
id=lg_from_w（无引号）✓   id⇥=⇥"lg_from_w"（制表符）✓
<script>var h="lg_from_w";</script>  -> 放行 ✓（建议-8 未回退）
```

### 12.2 需知会-6（不变式过度声称）—— ✅ **已修，代码与 docstring 现已一致**

用 `inspect.getdoc(protocol.Dimension)` 机检（**剔除勘误段**后判断）：

| 检查 | 结果 |
| --- | --- |
| 非勘误部分仍出现「正数」 | **False**（旧措辞只出现在勘误引用里） |
| 出现「非负」 | **True** |
| 显式写明 `bar_width` 可以是 `0.0` | True（附「某一维度真得 0 分是合法页面」） |
| 显式写明「不要断言 `bar_width > 0`」 | True |
| 指向 `source` 作为「有没有数据」的判据 | True |

**最小复现（容器 200px / 条宽 0px / 无 title）**：

```python
parse.parse_essay_detail(DOC + dim(cw=200, bw=0, title=None)).dimensions
# -> (Dimension(name='词汇', ratio=0.0, bar_width=0.0, container_width=200.0, source='width'),)
#    ratio==0.0 ✓  source=='width' ✓  —— 与「有限非负」的新不变式一致
```

**36 组组合（容器/条宽/有无 title × {0, 150, 200, 10^400}）精确扫描**：
`source == "width"` 路径**零违例**（宽度恒有限非负、`ratio == bar/container`、`ratio` 恒有限）。

> **一处可选精修（建议级，非缺陷）**：`source == "title"` 时宽度不仅可能为 `0.0`，还可能是 `inf`
> —— 扫描里有 **7 例** `title` 可用而宽度为 `inf` 的 `Dimension`（宽度原样保留、`ratio` 仍来自 title）。
> docstring 只提了「可能为 `0.0`」。建议补一句「也可能是 `inf` 等异常读数（原样保留）」，
> 免得消费方把「可能为 0.0」读成「一定是有限值」。这不影响本轮任何结论。

### 12.3 建议-14（非 http / 站外 href）—— ✅ 已修，**但发现一个前缀绕过**（需知会-7）

**已修部分**（`_absolute` 现在拒绝这些）：

```
'#' / '#anchor' / 'javascript:;' / 'javascript:void(0)'  ->  ''
'//evil.example.com/x' / 'http://evil.example.com/x' / 'https://evil.example.com/x'  ->  ''
'?c=v2&a=write&rid=1'  -> 'https://www.pigai.org/index.php?c=v2&a=write&rid=1'   （正常路径未被误伤）
```

`"#"` 的坑（lead 说是我没报的那个）确实存在且已正确处理 —— 关键是**在 `urljoin` 之前**挡掉，
因为 `urljoin(base, "#")` 会得到 `https://www.pigai.org/#`，**仍以 BASE_URL 开头**，事后守卫拦不住。

**⚠ 需知会-7｜站外判定用字符串前缀，`www.pigai.org.evil.com` 可绕过**（低影响）

```python
_absolute("//www.pigai.org.evil.com/x")        # -> 'https://www.pigai.org.evil.com/x'
_absolute("https://www.pigai.org.evil.com/x")  # -> 'https://www.pigai.org.evil.com/x'（原样放行）
```

原因：第 143 行 `resolved.startswith(protocol.BASE_URL)` 是**纯字符串前缀**比较，
`https://www.pigai.org.evil.com/...` 满足前缀但 host 是 `www.pigai.org.evil.com`。

**端到端复现（经公开 API，不只是函数级）**：把待完成行第 6 列（操作格）的 href 换成
`//www.pigai.org.evil.com/steal`，走 `list_pending_assignments()`：

```
Assignment.url = 'https://www.pigai.org.evil.com/steal'
以 BASE_URL 开头（守卫据此放行）= True
真实 netloc = 'www.pigai.org.evil.com'
```

**影响面（不夸大）**：`Assignment.url` 在 docstring 里声明为**仅供展示**，
本适配器**不会**对该 URL 发起任何请求（只读端点全部来自 `protocol.Endpoints`），
且要触发它需要页面 DOM 里先出现这样一个 href（即站点被改/被投毒）。故**非阻断、低影响**。
但 `_absolute` 的 docstring 写的是「站外或非 http 的**一律**返回空串」——**这句比代码强**
（同 N-6 那一类「声称强于事实」）。

修法（一行）：比较 **netloc** 而不是字符串前缀，例如
`urlsplit(resolved).netloc == urlsplit(protocol.BASE_URL).netloc`。

另附一个小边界（无需修）：无前导 `?`/`/` 的值会被当作相对路径接受 ——
`_absolute("c=v2&a=write")` → `https://www.pigai.org/c=v2&a=write`（同站、无害，但无意义）。
真实数据里操作列 href 恒为 `?c=…`，未触发。

### 12.4 建议-13（`#contents` 边界）—— ✅ 已补

`protocol.EssayDetail.essay_text` 的 docstring 现有明确边界说明
（`protocol.py:379-380`）：「若页面把非正文文案（工具条、提示）混进了 `#contents`，
它会**原样**出现在这里 —— 这是「不归一化」的代价。消费方要纯正文需自行判断」。
与我在 §11.5/I11 实测到的行为一致。

### 12.5 回归抽样：上一轮已验证的行为未被破坏 ✅

| 复跑项 | 实测 |
| --- | --- |
| **N-1a** 标题格无锚点 | `title='真正的标题（纯文本，无 a）'`、`url` 仍取操作格 ✓ |
| **N-1b** 操作格无锚点 | `url=''`（不借标题格）✓ |
| **N-2a** `_absolute("?c=v2&a=write&rid=1000005")` | `https://www.pigai.org/index.php?c=v2&a=write&rid=1000005` ✓ |
| **N-2b** `list_assignments()` 合并结果 | 4/4 条 url 全以 BASE_URL 开头 ✓ |
| **N-3** 匿名失效页 + jar 塞陈旧 `_JUKU_USER` | `is_logged_in() = False` ✓ |
| **N-3** 已登录页（无 jar） | `is_logged_in() = True` ✓ |

（`client.py` / `parse.py` 与第 1 版冻结逐字节一致，N-3 的 `False` 与 N-1 的双向不回落
在字节层面就不可能被本轮改动影响 —— 这里仍按要求实跑一遍。）

### 12.6 测试

```bash
$ PYTHONPATH=src python3 -m unittest tests.test_pigai_login   → Ran 41 tests  OK
$ PYTHONPATH=src python3 -m unittest tests.test_pigai_parse   → Ran 79 tests  OK
$ PYTHONPATH=src python3 -m unittest tests.test_pigai_adapter → Ran 33 tests  OK
$ PYTHONPATH=src python3 -m unittest tests.test_pigai_login tests.test_pigai_parse tests.test_pigai_adapter
Ran 153 tests in 0.123s
OK
```

**41 + 79 + 33 = 153，与任务书预告完全一致。** 断网复跑（封死 `create_connection`/
`getaddrinfo`/`gethostbyname`/`HTTPConnection.connect`）：**153 tests 全绿**。
按任务书要求**未**再做全量归属分析（上一轮已确认 43 条失败全在 `tests.test_pinco`）。

新增用例质量（读过）：`TestSessionGuard` 覆盖失效页 + 4 种登录页写法 + 脚本裸串负例；
`TestSessionGuardAsync` 用 `IsolatedAsyncioTestCase` 对**四条只读路径**逐一断言
（并且文件里有元测试禁止普通 `TestCase` 里出现 `async def test_*` —— 防「假通过」，这点很扎实）；
`TestUrlRegression.test_rejects_offsite_and_non_http` 覆盖 5 种伪 href。

### 12.7 本轮仍未验证的

1. **线上一切**（含 `lookup_by_rid` 的无副作用结论）——我全程离线。
2. `a=loginForm` 的误报率结论只对**当前 15 份样本**成立；若将来合法页出现指向登录页的
   「登录/切换账号」链接（含该串），守卫会误判成会话失效 —— 这是**判据本身的固有代价**
   （漏判 vs 误判的取舍），不是缺陷，但值得在站点改版后重扫一次。
3. **需知会-7 的可达性**：我证明了函数与 API 会产出站外 URL，但无法证明真实站点会出现这类 href
   （需要页面被改）。
4. 第 2 版对 `docs/research/pigai.md` 的改动我只核对了 §4.5 / §6.4 与本次修复相关的段落。

### 12.8 本轮结论

**第 2 版无阻断级缺陷。** 我报的 **需知会-5、需知会-6 都已修好且我用真实样本/最小复现打穿验证**；
建议-12、建议-13、建议-14 均已落实（建议-14 还顺手修掉了我未报的 `"#"` 坑）。
上一轮的 N-1/N-2/N-3 行为**未被破坏**；pigai 三件套 **153 tests 全绿**（断网亦然）。

**新增 1 条需知会（低影响）+ 1 条可选精修**：
- **需知会-7**：`_absolute` 的站外守卫用字符串前缀，`www.pigai.org.evil.com` 可绕过
  （仅影响**展示用**的 `Assignment.url`，适配器不会请求它；docstring 的「一律」强于代码）。
- **建议**：`source=="title"` 时宽度也可能是 `inf`，docstring 只提了 `0.0`（可选补一句）。

---

## 13. 第 3 版增量复核（task-13，定稿轮）

- 复核时间：**2026-10-19 12:06–12:12（Asia/Shanghai）**
- 对象：**第 3 版** —— 只验需知会-7（`_absolute` 站外判定）的修复
- 方式：仍**完全离线**（未访问 `www.pigai.org`）

### 13.0 基线确认：3 个变更 + 8 个逐字节未变 ✅

| 文件 | 本轮 md5 | 状态 |
| --- | --- | --- |
| `src/kedaibiao/platforms/pigai/adapter.py` | `6034a531e8c764b35077ffcecd62bbb3` | **已变更**（319 行） |
| `src/kedaibiao/platforms/pigai/protocol.py` | `f29f7f506c640640b7c8172203eb3cab` | **已变更**（483 行） |
| `tests/test_pigai_adapter.py` | `dc9fc06dd9de06f57eb2a78e12bf2d95` | **已变更**（459 行） |
| `parse.py` / `client.py` / `rsa_login.py` / `__init__.py` / `test_pigai_login.py` / `test_pigai_parse.py` / `scripts/pigai_read.py` / `docs/research/pigai.md` | `ec961a70… / d791255c… / c8b23f41… / d087d025… / d734dd76… / 61b0e032… / 03ed5bba… / ec972b45…` | **与第 2 版冻结逐字节一致** ✅ |

> 一处与任务书的小出入（**非缺陷，如实记录**）：任务书写「其余 9 个与第 2 版一致」，
> 我列出的 11 个受测文件里是 **3 变更 + 8 未变**（第 9 个应是 `tests/fixtures/` 或其它未列入清单的路径）。
> 结论不受影响：所有**与本次修复相关的**文件状态均与任务书一致。

### 13.1 主目标：原来的绕过用例现在全部被拒 ✅

`_absolute` 已改为**按 `netloc` + `scheme` 双比对**（基准 `_BASE_URL_PARTS = urlsplit(protocol.BASE_URL)`，
`adapter.py:148-150`），不再是字符串前缀。我独立复跑（19 个拒绝用例，含任务书点名的 4 个）：

```
'//www.pigai.org.evil.com/steal'   -> ''   ✓（原绕过用例）
'https://www.pigai.org.evil.com/x' -> ''   ✓
'http://www.pigai.org.evil.com/x'  -> ''   ✓
'https://www.pigai.org.evil.com'   -> ''   ✓
'http://www.pigai.org/index.php'   -> ''   ✓（同 host、scheme 不同）
'https://www.pigai.org:8443/x'     -> ''   ✓（非默认端口）
'//evil.example.com/x' / 'http(s)://evil.example.com/x'  -> ''  ✓
'//www.pigai.org@evil.com/x'       -> ''   ✓（userinfo 伪装）
'https://evil.com/?x=www.pigai.org'-> ''   ✓
'javascript:;' / 'javascript:void(0)' / '#' / '#anchor'  -> ''  ✓
'data:…' / 'ftp://www.pigai.org/x' / 'file:///etc/passwd' / 'mailto:…'  -> ''  ✓
→ 19/19 全部拒绝
```

**同源正控（8/8 未被误伤）**：

```
'?c=v2&a=write&rid=1'                               -> 'https://www.pigai.org/index.php?c=v2&a=write&rid=1' ✓
'/index.php?c=write'                                -> 'https://www.pigai.org/index.php?c=write' ✓
'/index.php?c=v2&a=view&eid=200000004&rid=1000006'  -> 正确 ✓
'https://www.pigai.org/index.php?c=write'           -> 正确 ✓
'//www.pigai.org/index.php?c=write'                 -> 正确 ✓（协议相对、同源）
'HTTPS://www.pigai.org/index.php?c=write'           -> 正确 ✓（urlsplit 会把 scheme 小写化）
'/' 与 ''                                           -> 'https://www.pigai.org/' ✓
```

### 13.2 端到端复现（我上一轮报的那条）✅

把待完成行**操作格** href 换成站外地址，走公开 API：

```
href='//www.pigai.org.evil.com/steal'    -> Assignment.url = ''   ✓（不再是站外地址）
href='https://www.pigai.org.evil.com/x'  -> Assignment.url = ''   ✓
href='javascript:;'                      -> Assignment.url = ''   ✓
href='#'                                 -> Assignment.url = ''   ✓
正控 href='?c=v2&a=write&rid=1000005'    -> 'https://www.pigai.org/index.php?c=v2&a=write&rid=1000005' ✓
```

真实 recon 端到端（`waitComplete` + `write`）：合并 **11 条**，**非站内 URL = 0**、**空 url = 0**，
`pending` 与 `finished` 两类 url 均正确绝对化。

### 13.3 唯一风险面：同源误判扫描 —— 68 组，0 误伤 ✅

按任务书要求扫同源合法 href（11 条真实路径形态 × 6 种写法 + 2）：**共 68 组**：

```
接受 68 | 返回空串 0 | 放行了站外的 0
```

包含 `?query` / `/abs/path` / 无前导斜杠 / 绝对 URL / `//www.pigai.org/…` / `HTTPS://…`
六种写法，全部正确保留。**未发现任何同源合法 href 被误判成空串。**

### 13.4 docstring 与代码现已一致 ✅

`_absolute` 的 docstring 写「**站外或非 http(s) 的一律返回空串**」——本轮实测：
**所有**站外（含 `www.pigai.org.evil.com`、非默认端口、userinfo 伪装）与**所有**非 http(s)
（`javascript` / `data` / `ftp` / `file` / `mailto`）都返回 `''`。**这句现在是真的**，
与需知会-6 同类的「声称强于事实」问题在本文件已消除。docstring 也补上了坑 4（比 netloc 不比前缀）的说明。

### 13.5 `Dimension` 精修 ✅

`inspect.getdoc(protocol.Dimension)` 核验：`source == "title"` 分支已补齐
**「宽度也可能是 `inf` 等异常读数」「原样保留」「不一定被清洗」**三处措辞。
与我上一轮扫出的 7 例 `title` 可用而宽度为 `inf` 的观察一致。

### 13.6 测试

```bash
$ PYTHONPATH=src python3 -m unittest tests.test_pigai_login   → Ran 41 tests  OK
$ PYTHONPATH=src python3 -m unittest tests.test_pigai_parse   → Ran 79 tests  OK
$ PYTHONPATH=src python3 -m unittest tests.test_pigai_adapter → Ran 34 tests  OK
$ PYTHONPATH=src python3 -m unittest tests.test_pigai_login tests.test_pigai_parse tests.test_pigai_adapter
Ran 154 tests in 0.126s
OK
断网复跑（封死外连原语）：154 tests 全绿
```

> ⚠ **与任务书预告的 153 差 1（如实记录，非缺陷）**：实测 **41 + 79 + 34 = 154**。
> 差异来自 `tests/test_pigai_adapter.py` 新增了一个**正控用例** `test_accepts_same_origin`，
> 而 11 个拒绝用例是在原 `test_rejects_offsite_and_non_http` 里**就地扩展**的（没有新增方法名）。
> 多一条正控是好事。测试全绿。

新增用例质量：`test_rejects_offsite_and_non_http` 覆盖 11 例（含 3 个 `www.pigai.org.evil.com`
伪站与 1 个 scheme 不同），注释里直接写了「需知会-7：字符串前缀比较会放行这些」——
**把缺陷来源钉在用例注释里**，可回溯。

### 13.7 回归抽样：N-1 / N-2 / N-3 未被破坏 ✅

```
N-1a 标题格无锚点 -> title='真正的标题（纯文本，无 a）'、url 仍取操作格 ✓
N-1b 操作格无锚点 -> url=''（不借标题格）✓
N-2  list_assignments() 合并 4 条，非绝对 URL = 无 ✓
N-3  匿名失效页 + jar 塞陈旧 _JUKU_USER -> is_logged_in = False ✓
N-3  正常列表页（无 jar）              -> is_logged_in = True  ✓
```

### 13.8 观察（建议级，**不阻塞定稿**）

`netloc` 比较是**大小写敏感**的，于是这些「语义上同源但写法特殊」的 href 会被判空
（**方向安全**：只是展示用 url 变空，不会放行站外）：

| href | 结果 | 说明 |
| --- | --- | --- |
| `https://WWW.PIGAI.ORG/index.php?c=write` | `''` | `urlsplit().netloc` 保留大小写（`.hostname` 才小写化） |
| `https://www.pigai.org:443/index.php?c=write` | `''` | 显式默认端口未归一 |
| `https://user@www.pigai.org/x` | `''` | 带 userinfo |
| `https://www.pigai.org./x` | `''` | 尾部点 |
| `foo/bar` / `c=v2&a=write` | 同站 URL | 无前导 `?`/`/` 被当相对路径（同站、无害但无意义） |

**为什么我不把它算缺陷**：(1) 真实页面里这两列的 href **恒为相对形态**（`?c=…` / `/index.php?…`），
我核对的 11 份样本 + 3 份写页样本全部如此；(2) 失败方向是**保守**（返回空串＝不给链接），
不是放行站外；(3) 若要更宽，可改比 `urlsplit(resolved).hostname == base.hostname`（自动小写化）
并归一默认端口 —— 属可选硬化，**不建议**为它再开一轮。

### 13.9 本轮结论

**第 3 版无新问题，需知会-7 已修且被独立复现打穿。** 站外/伪协议 19/19 拒绝、
同源正控 8/8 通过、68 组同源形态 0 误伤、端到端站外地址不再进 `Assignment.url`、
docstring 与代码一致、N-1/N-2/N-3 未破坏、pigai 三件套全绿（154 条）。

> # ✅ 可以定稿。
> 本报告对 **第 3 版**（§13.0 的 3 个 md5）的结论是：**未发现阻断级缺陷，也未发现新的需知会级问题**；
> 遗留的只有 §13.8 一条**可选硬化**（保守方向、真实数据不触发），不构成定稿阻塞。

---

**验证者签署**：verifier ｜ 2026-10-19 ｜ 全程离线，未访问 `www.pigai.org`，未修改任何被测代码
（四轮写入均仅限 `docs/verification/pigai-stage3.md`；每轮复核后被测文件 md5 均未变）。
