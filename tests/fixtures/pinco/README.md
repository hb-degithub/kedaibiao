# Pinco 测试样本（fixtures）

## 纪律（**必读，血泪三条**）

> **fixture 必须来自真实抓包字节。手写 / 精简过的样本必须显式标注。**

违反这条纪律的代价，本项目已经付过三次：

| # | 事故 | 为什么离线全绿 |
| --- | --- | --- |
| 1 | 缺 `x-server: main` 请求头 → 线上每个业务 action 都 500 | 假 Transport 只看响应、不看请求 |
| 2 | header 断言写反（`assertNotIn` 锁住了错误假设） | 断言与实现一起错，互相背书 |
| 3 | `system_current_time` fixture 缺 `statusCode` → `health_check` 恒假 | 回放框架对、**回放内容假** |

第 3 条尤其阴险：fixture 少了决定成败的那个字段，代码里「字段缺失就放行」的
兜底就让单测绿了；真实响应里那个字段存在且语义不同，于是线上直接失败。

所以本目录的规矩是：

1. **原始字节优先**：能从抓包直接落盘的，就逐字节落盘，不要重新序列化、不要
   「顺手精简掉用不到的字段」—— 你精简掉的可能正是决定成败的字段。
2. **必须标注来源**：每个文件在下面表格里登记「真实抓包」还是「合成」。
   `tests/test_pinco.py::TestFixtureProvenance` 会检查这里有没有漏登记的文件。
3. **合成样本要标明**：合成文件的顶层 JSON 里带 `"_fixture": "synthetic"` 标记；
   测试会断言除清单外没有别的合成文件混进来。
4. **改了字段名就要重抓**：字段名是从抓包读出来的，不是猜的；改 fixture 前先
   确认真实响应，别照着旧 fixture 改出「自洽但错」的形状。

## 清单

| 文件 | 来源 | 对应接口 |
| --- | --- | --- |
| `course_list.json` | 真实抓包（课程名/教师名已脱敏，ID 与结构保留） | `GetV1CourseStudentworkbench` |
| `class_list.json` | 真实抓包（班级名脱敏） | `GetV1StudentClassList` |
| `homework_list.json` | 真实抓包（标题脱敏；补了 2 条状态组合样本，结构同真实） | `GetV1StudenthomeworkList` |
| `homework_detail.json` | 真实抓包（标题/课程名脱敏） | `GetV1StudenthomeworkByStudentHomeworkIdDetail` |
| `homework_questions.json` | 真实抓包（题干保留 HTML 形态，含 `rich-img` / `rich-formula` / 上传题） | `GetV1StudenthomeworkByStudentHomeworkIdQuestions` |
| `material_list.json` | 真实抓包（文件名脱敏，`learningRatio` 补了 0.5 / 1.0 两档） | `GetV1CourseByCourseIdStudentMaterialList` |
| `material_detail.json` | 真实抓包（文件名脱敏） | `GetV1CourseByCourseIdStudentMaterial` |
| `system_current_time.json` | **真实抓包逐字节**：`{"code":0,"msg":"ok","data":{"currentTime":…},"statusCode":200,"message":"ok"}` | `GET /server-main/api/v1/system/currentTime` |
| `homework_questions_plain.json` | **合成**（合成样本，用于「无图片、纯文本题」分支） | 同上（题目接口） |

> `homework_questions_plain.json` 是合成的：真实样本里每道主观题都带图片或公式，
> 拿不到「全纯文本」的真实样本。它的信封形状与真实一致（测试会校验），但**不要**
> 拿它去证明线上行为 —— 只用来覆盖解析分支。

## 成功的判据（两个家族不同）

| 家族 | 地址 | 成功判据 |
| --- | --- | --- |
| RPC | `POST /student/api.json?actionName=…` | `statusCode == 0` |
| REST | `/server-main/api/v1/…` | `code == 0`（`statusCode` 是 HTTP 镜像 200） |

同一个 JSON `{"statusCode":200,"code":0,…}` 在 REST 家族是成功、在 RPC 家族是失败，
`tests/test_pinco.py` 有一条用例专门把这两种判分钉在一起。
