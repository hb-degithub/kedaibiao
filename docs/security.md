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
| 抓包 | `*.har`、`docs/research/_raw/`、`tests/fixtures/` 里**未脱敏**的原始响应 | 可能含身份信息 |

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

## 6. 发布与历史清理

**工作区干净 ≠ 历史干净。** 提交过一次的手机号 / uid / 姓名会永远留在
`git log` 里，所以发布前必须连历史一起清。

### 6.1 边界：清什么、留什么

| 类别 | 例子 | 处理 |
| --- | --- | --- |
| 账号级标识 | 手机号、`uid`、`personId`、`cpi`、页面令牌（`token=` / `enc=` / `uploadEnc` / `openc`） | **清除**：账号号段 → `100000001` / `100000002`，令牌 → `0f1e2d3c…e1f0` |
| 文字型身份 | 真实姓名、学校、班级、年级 | **清除**：→「张老师」「示例大学」「示例班级」 |
| 个人记录号 | 作文号 `rid` / 稿件号 `eid` | **清除**：→ 7 位 / 9 位假值（位数必须一致，测试断言依赖位数） |
| 课程级内容 | `courseid` / `clazzid` / 章节 / 题目 / 教材与试卷正文 | 保留 —— 同门课所有学生共享，不指向个人 |
| 平台事实 | 域名、端点、表单字段名、成功码 | 保留 —— 实现所必需 |

### 6.2 历史清理（发布前跑一次）

```bash
# 0) 先备份：整仓 bundle 到仓库外
git bundle create ~/backups/kedaibiao-$(date +%Y%m%d).bundle --all

# 1) 写替换表（一行一个 原值==>假值）。原值只写进 /tmp，绝不提交。
#    真实值从自己机器的 .secret-denylist / 提交记录里取。
cat > /tmp/replace.txt <<'EOF'
<真实手机号>==>13800138000
<真实 uid>==>100000001
<真实 personId / cpi>==>100000002
EOF

# 2) 清全部历史（所有分支 / 标签）
git filter-repo --force --replace-text /tmp/replace.txt

# 3) 让旧对象真正消失
git reflog expire --expire=now --all && git gc --prune=now --aggressive
```

`git filter-repo` 没装就先 `pip install git-filter-repo`。

### 6.3 校验（三条都必须为空 / 全绿）

```bash
git log --all --oneline -S "<任意一个真实值>"        # 无输出
git rev-list --objects --all | grep -i screenshots   # 无输出
python3 scripts/check_secrets.py --all && .venv/bin/python -m pytest -q tests/test_privacy.py
```

发布后再看一眼 GitHub：`Settings → Code security` 无 secret 告警，
仓库里没有 `docs/screenshots/`、`data/`、`.env`。

### 6.4 每台机器上的常驻闸

```bash
python3 scripts/check_secrets.py --install-hook   # 装成 pre-commit 钩子
```

`.secret-denylist` 只在本机存在：真实姓名 / 学校 / 班级 / 作业号这类正则认不出的值
登记在那里，`scripts/check_secrets.py` 与 `tests/test_privacy.py` 都会逐条比对。

---

## 7. 代码层的约束

- **平台凭据只来自控制台账号库**（`security.accounts`）：绑定时真登录一次换
  cookie，密码用完即弃。`.env` 不再保存任何平台账号密码 —— 两套来源并存时
  用户搞不清实际用的是哪个号，而且 `.env` 是明文；
- `security.credentials`（环境变量那条路）已**弃用**，只留给没有登录态的独立脚本；
- 控制台的平台账号**不存密码**，只存 cookie（会话失效让用户重输一次）；
- `Credentials.__repr__` 屏蔽密码，日志里打 `security.describe(creds)`；
- 用户密码用 `scrypt` + 随机盐存哈希，接口永不返回；
- 会话令牌走 HttpOnly cookie，不落 localStorage。
