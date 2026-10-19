# 敏感信息与入库纪律

> 这个仓库有多个窗口并行开发，最多时有 4 个人同时在改同一个工作区。
> 一次 `git add -A` 就能把 `.env`、cookie、数据库或者含真实数据的截图带进去，
> **而历史一旦写进去就很难彻底清掉**（本仓库已经为这个付过一次代价，见 §5）。

---

## 1. 绝对不进 git 的东西

| 类别 | 具体 | 为什么 |
| --- | --- | --- |
| 凭据文件 | `.env`、`config/local.toml`、`*.pem`、`id_rsa` | 明文账号密码 |
| 会话 | `*.cookies.json`、`storage_state*.json`、`data/accounts/` | 等同于登录态，拿到就能用 |
| 数据 | `data/*.db`、`*.sqlite3` | 里面有用户表（密码哈希）、会话令牌、平台账号 |
| 本地截图 | `docs/screenshots/` | 截图里有真实课程名、作业号、账号信息 |
| 抓包 | `*.har`、`tests/fixtures/` 里的原始响应 | 可能含身份信息 |

这些都在 `.gitignore` 里。**新增任何会写文件的功能时，先想清楚它写到哪、要不要加忽略。**

## 2. 数据分类：什么算敏感

| 等级 | 例子 | 处理 |
| --- | --- | --- |
| 高 | 密码、平台 cookie、会话令牌 | **永不落盘明文**（控制台的平台账号就不存密码，只存 cookie） |
| 中 | 真实作业号、学号、姓名、课程名、班级 | 文档/测试里一律换成假值；确实需要登记就写进 `.secret-denylist` |
| 低 | 平台域名、端点路径、表单字段名 | 可以写进代码与文档（这是实现所必需的） |

### 换成什么假值

| 真实 | 假值 |
| --- | --- |
| 7 位作业号（真实值不在此转录） | `1000001`、`1000002`… |
| 9 位稿件号（真实值不在此转录） | `200000001`、`200000002`… |
| 课程名（真实值不在此转录） | `示例课程（三）- Unit 5` |
| 教师姓名 | `张老师` |

**保持位数一致** —— 测试里常有「7 位是 rid、9 位是 eid」这类断言，位数变了会把测试改坏。

## 3. 入库前检查

```bash
make check-secrets            # 检查暂存区（git add 之后）
python3 scripts/check_secrets.py --all   # 检查全部已跟踪文件
```

它会拦三类东西：

1. **禁止入库的路径**（见 §1）；
2. **硬编码凭据**：`password = "长字面量"`、`Bearer <长令牌>`、私钥块；
3. **本地 deny 列表**：`.secret-denylist`（已 gitignore）里登记的真实值。

装成 pre-commit 钩子（推荐每台机器装一次）：

```bash
python3 scripts/check_secrets.py --install-hook
```

装了之后每次 `git commit` 都会先跑一遍，有问题直接拒绝提交。
确认是误报可以 `git commit --no-verify` 跳过，或者在被标记那行加注释：

```python
_PASSWORD = "Hu7!Secret-P@ssw0rd-2026"  # noqa: secret-check - 故意的脱敏哨兵
```

`.secret-denylist` 的用法（本文件不入库，每台机器自己维护）：

```
# 每行一个真实值（下面是格式示例，用假值演示）
1000001
示例课程
```

## 4. 并行开发时的额外规矩

- **不要 `git add -A` / `git add .`** —— 只 add 自己负责的文件；
- 提交前先 `git status` 扫一眼，出现 `.env`、`data/`、截图就是不正常；
- 另一个窗口的产物里如果含真实数据，**提醒它**，不要默默提交；
- `tests/fixtures/` 里的样本必须是**脱敏后**的：真实姓名 → 占位、班级串 → 示例、课程名 → 中性名。

## 5. 已经付过的代价（记录在案）

第一版控制台截图被提交进了 `34f97a2`。那几张图里能看到真实的
课程名、作业号、账号配置状态。处理过程：

1. `git rm --cached docs/screenshots/` + 加 `.gitignore`；
2. 用 `git filter-branch --index-filter` 把截图从**全部历史**里摘掉；
3. `git reflog expire --expire=now --all && git gc --prune=now` 让旧对象真正消失；
4. 验证：`git log --all --pretty=format: --name-only | grep screenshots` 无输出，
   `git rev-list --objects --all | grep '\.png$'` 也无输出。

同时把文档、代码提示、测试夹具里的真实作业号与课程名全部换成了假值。

**教训：截图默认就是敏感的。** 它看起来是「界面」，实际上是把你的账号内容
渲染成图片。以后要么不提交截图，要么用假数据渲染。

## 6. 代码层的约束

- 凭据只从环境变量 / `.env` 读（`security.credentials`），不写死在代码里；
- 控制台的平台账号**不存密码**，只存 cookie（会话失效让用户重输一次）；
- `Credentials.__repr__` 屏蔽密码，日志里打 `security.describe(creds)`；
- 用户密码用 `scrypt` + 随机盐存哈希，接口永不返回；
- 会话令牌走 HttpOnly cookie，不落 localStorage。
