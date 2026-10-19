# 命名与标识符规范

## 1. 项目名

**课代表**，仓库 / 包名 **`kedaibiao`**。

### 为什么叫课代表

| 维度 | 说明 |
| --- | --- |
| 语义吻合 | 课代表这个角色的本职就是**收作业、催进度、对齐各科要求** —— 正好是这个项目在干的事（多平台作业聚合 + 统一调度 + 提交）。中文语境里不需要解释。 |
| 不绑平台 | 名字里没有任何平台名。后续接东软系、超星、智慧树、U校园都不用改名 —— 平台专用名（如 `uai-core` 里的 "uai"）在接入第二个平台时就会变成负担。 |
| 可读可写 | `kedaibiao` 全小写拼音，ASCII 安全：能做 Python 包名、目录名、域名前缀、npm 包名，无空格无特殊字符，跨平台文件系统都认。 |
| 好念好记 | 四个音节，输入法一次打完；英文场景可缩写成 `kdb`。 |
| 留有余地 | 不叫 `xxx-bot` / `xxx-auto`，避免把项目钉死在「脚本」的定位上 —— 后续要做 Web 控制台、题库服务、调度中心都塞得下。 |

### 考虑过但没选的方案

| 方案 | 为什么不选 |
| --- | --- |
| `纽伴 / neumate` | Neu(soft)+mate，中性、英文友好，但丢了"收作业"这个最核心的语义，更像通用助手。 |
| `学业领航 / neu-pilot` | Pilot 强调"替你驾驶"，气质偏自动化中台，同时把"东软"写进了名字，接别的平台就尴尬。 |
| `东软课业中台 / neu-coursehub` | "中台"太工程化，且同样硬绑"东软"；`coursehub` 也容易与既有项目重名。 |
| `刷课精灵 / sk-elf` | 直白但把工具性格写死了，且"精灵/助手"类命名在开源项目里重名率极高。 |

---

## 2. 标识符规范

| 用途 | 取值 | 说明 |
| --- | --- | --- |
| 仓库目录名 | `kedaibiao`（当前工作区为 `刷题刷课`，保持不动） | 工作区路径变更会打断已有的会话与工具链记录 |
| Python 分发包名 | `kedaibiao` | `pyproject.toml` 的 `name` |
| Python 导入包名 | `kedaibiao` | 位于 `src/kedaibiao/`（src-layout） |
| CLI 命令 | `kedaibiao` | `[project.scripts]` |
| 环境变量前缀 | `KEDAIBIAO_` | 如 `KEDAIBIAO_DRY_RUN` |
| 事件名 | `<域>.<动作>` | `run.start`、`assignment.done`、`answer.low_confidence` |
| entry point 组 | `kedaibiao.adapters` / `kedaibiao.answerers` / `kedaibiao.notifiers` | |
| 平台机器名 | 全小写、无连字符 | 用作 registry key 与数据库字段，如 `neusoft`、`chaoxing` |
| 数据库表 | 复数小写下划线 | `questions`、`runs`、`submissions` |

### 为什么平台机器名不能带连字符

它同时是「注册表 key」「数据库里的 `platform` 字段值」「文件名前缀」
（`data/sessions/<platform>.storage_state.json`）。连字符在文件名里安全，
但在某些 shell / SQL / 日志解析场景会诱发引号问题，统一用下划线或纯小写字母最省事。

### 为什么工作区目录不改名

当前工作目录是 `/home/hb/刷题刷课`。改成 `kedaibiao` 会打断会话里已登记的
路径引用与工具链记录，收益（一致性）不抵成本。**仓库的对外身份由
`pyproject.toml` 与 README 决定，不取决于它落在哪个目录。**

---

## 3. 中文命名的使用边界

- **可以**出现在：README、`docs/`、代码注释、`display_name`、用户可见的日志与通知；
- **不要**出现在：标识符、文件名、目录名、数据库字段、事件名、配置键。

理由：中文路径在 zip 解包、CI、容器挂载、跨平台归档时仍偶发编码问题。
`display_name` 是唯一的例外出口 —— 机器名与展示名分离，正是为此。
