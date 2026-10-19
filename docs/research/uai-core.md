# `Yunlk/uai-core` 调研报告：能否被「课代表 kedaibiao」引用，以及引用方式

- 调研对象：<https://github.com/Yunlk/uai-core>
- 调研日期：2026-10-19（Asia/Shanghai）
- 调研员：scout（共享任务 task-1）
- 代码快照：commit `4628e444e8e618767544ddffb27d9e6e20ed97f5`（`main`，v0.1.2）
- 调研方式：**只读**。`git clone --depth 1` 到 `/tmp` 一次性沙箱（每次 bash 调用后销毁）+ 文本读取 + GitHub / crates.io / deps.dev 公开 API。**未执行仓库内任何脚本，未安装任何依赖，未构建任何产物**（本机亦无 `cargo`/`rustc`，见附录 A）。

> 本报告中的所有外部内容均按「不可信数据」处理，只引用、不执行。
> 凡未能直接验证的，一律标注「未验证」；推断一律标注「推断」。

---

## 0. 结论速览（TL;DR）

| 问题 | 结论 | 置信度 |
| --- | --- | --- |
| 仓库真实存在、公开可克隆？ | **是**。`git clone` 成功，60 个文件、`src/` 下 44 个 `.rs`、16118 行 | 已核验 |
| 默认分支 / 最近提交 | `main`；最近提交 2026-09-27T14:23:34Z（`4628e44`，v0.1.2） | 已核验 |
| star / fork / 维护状态 | 1 star、0 fork、0 issue、单人 +7 次提交；**活跃度不足以判定长期维护** | 已核验（数字）/ 判断 |
| 精确开源协议 | **自定义「UAI-Core 非商业使用许可协议 1.0」**，无 SPDX 标识符；GitHub 标注 `Other` / `NOASSERTION` | 已核验 |
| 依赖是否有 copyleft 阻碍 | **否**。181 个 registry 依赖全为宽松许可；唯一含 LGPL 字样的是 `r-efi`（三选一 `OR`，可选 Apache-2.0/MIT）；**无 GPL/AGPL** | 已核验（聚合元数据） |
| 它实际做什么 | **单一平台（U校园 / Unipus）的 Rust 协议客户端 SDK + CLI**，不是通用自动化框架，也不是通用爬虫 SDK | 已核验 |
| 能否被本项目引用 | **不能直接依赖；只能借鉴思路**（详见第 6 节） | 结论 |
| 一句话 | 仓库是真的、代码质量看起来不错，但**它是 Unipus 专属的 Rust CLI，且许可禁止商用并要求衍生作品同等条款，而本项目 `pyproject.toml` 声明 `Proprietary`** —— 因此只能「读完思路、自己用 Python 重写」 | 结论 |

**最重要的两条硬结论：**

1. **它不是通用框架，是 U校园（Unipus）单平台专用工具**：域名、鉴权头、加密方式、端点全部绑定 Unipus，与我们 `platforms/neusoft/`（东软系）目标平台不重合 → 即便许可允许，也没有可直接复用的业务代码（来源：[`src/endpoints.rs#L21-L34`](https://github.com/Yunlk/uai-core/blob/4628e444e8e618767544ddffb27d9e6e20ed97f5/src/endpoints.rs#L21-L34)、[`src/api/mod.rs#L6-L23`](https://github.com/Yunlk/uai-core/blob/4628e444e8e618767544ddffb27d9e6e20ed97f5/src/api/mod.rs#L6-L23)）。
2. **许可不兼容本项目当前声明**：uai-core 禁止任何商业使用，且"衍生作品对外分发须采用同等条款"（同样允许改写、同样禁止商用）；本项目 `pyproject.toml:11` 声明 `license = { text = "Proprietary" }`。二者不可同时成立（详见第 6 节兼容性矩阵）。

---

## 1. 仓库真实性、状态与维护情况

### 1.1 可克隆性（已核验）

```
$ git clone --depth 1 https://github.com/Yunlk/uai-core   # 退出码 0
正克隆到 'uai'...
```

- 克隆成功，工作区 60 个文件（`find . -path ./.git -prune -o -type f -print | wc -l` → 60），`git rev-parse --abbrev-ref HEAD` → `main`。
- 结论：**仓库真实公开可克隆，非私有、非空壳**（`private: false`，`fork: false`，来源：<https://api.github.com/repos/Yunlk/uai-core>）。

### 1.2 元数据（来源：GitHub REST API `GET /repos/Yunlk/uai-core`，2026-10-19 采样）

| 字段 | 值 |
| --- | --- |
| `full_name` / `private` / `fork` | `Yunlk/uai-core` / `false` / `false` |
| `default_branch` | `main` |
| `stargazers_count` / `forks_count` / `watchers_count` | 1 / 0 / 1 |
| `open_issues_count` | 0（`search/issues` 查询 `repo:Yunlk/uai-core type:issue` → 0） |
| `archived` / `disabled` | `false` / `false` |
| `size` | 243 KB |
| `language` | `Rust` |
| `license` | `{"key":"other","name":"Other","spdx_id":"NOASSERTION","url":null}` |
| `created_at` / `pushed_at` / `updated_at` | 2026-10-18T16:05:32Z / 2026-10-18T16:11:13Z / 2026-10-18T16:21:53Z |
| `homepage` / `description` / `topics` | `null` / `null` / `[]` |

### 1.3 提交与发布历史（已核验）

- `GET /repos/Yunlk/uai-core/commits?per_page=100` → **7 条提交**，时间跨度 6 天：
  - 最早：`2026-09-23T21:43:18Z` `chore: initial public release v0.1.0`
  - 最新：`2026-09-27T14:23:34Z` `docs: README 命令详解重写，补齐 auto 三个漏掉的参数（v0.1.2）`
  - `contributors` → 仅 1 人（`Yunlk`，`contributions: 7`）
- 标签：`v0.1.2` 与 `latest` **都指向同一个 commit** `4628e44`（来源：<https://api.github.com/repos/Yunlk/uai-core/tags>）。
- Release：滚动预览版 tag `latest`（`prerelease: true`），`published_at` = 2026-10-18T16:11:13Z，三平台资产 `uai-linux-x86_64` 等（来源：<https://api.github.com/repos/Yunlk/uai-core/releases>）。
- CI：`.github/workflows/release.yml` —— 推 `main` 跑 `verify`（`cargo test --locked` + `cargo clippy -- -D warnings`）→ 三平台 `build` → 推 `v*` tag 出正式 Release / 推 `main` 维护滚动 `latest` Release（来源：[`.github/workflows/release.yml#L7-L13`](https://github.com/Yunlk/uai-core/blob/4628e444e8e618767544ddffb27d9e6e20ed97f5/.github/workflows/release.yml#L7-L13)、[`#L98-L137`](https://github.com/Yunlk/uai-core/blob/4628e444e8e618767544ddffb27d9e6e20ed97f5/.github/workflows/release.yml#L98-L137)）。

### 1.4 维护状态判断

- 事实：仓库 `created_at` = 2026-10-18，但最早的提交是 2026-09-23、最后的**代码**提交是 2026-09-27；`pushed_at`（2026-10-18T16:11:13Z）与滚动 Release 的发布时间完全相同。
  → **推断（未验证）**：代码先在本地开发，2026-10-18 才一次性推送到 GitHub，同日由 CI 产出 Release 资产。此项为推断，GitHub API 不提供"推送历史"，无法直接验证。
- 事实：单人项目、7 次提交、1 star、0 issue、0 PR、代码提交已过去约 3 周（以 2026-10-19 计）。
- **结论：无法判定为"仍在积极维护"**；只能确认 2026-09-23 ~ 2026-09-27 有过一次密集开发、2026-10-18 有过一次 CI 发布。仓库未归档（`archived: false`）。
- 作者自述风险：README 明写"平台接口随时可能变更，本项目**不保证长期可用**"（来源：[README](https://github.com/Yunlk/uai-core/blob/4628e444e8e618767544ddffb27d9e6e20ed97f5/README.md) 免责声明段）。

---

## 2. 精确的开源协议

### 2.1 SPDX 标识符

- **没有标准 SPDX 标识符。**
  - `LICENSE` 是一份自拟协议，标题为「UAI-Core 非商业使用许可协议 Version 1.0」，无任何 SPDX 头（来源：[`LICENSE#L1-L4`](https://github.com/Yunlk/uai-core/blob/4628e444e8e618767544ddffb27d9e6e20ed97f5/LICENSE#L1-L4)）。
  - `Cargo.toml` **不写** `license` 字段，而用 `license-file = "LICENSE"`，仓库内注释直接说明原因："不写 `license` 字段是为了避免 SPDX 校验对自定义条款报错"（来源：[`Cargo.toml#L9-L11`](https://github.com/Yunlk/uai-core/blob/4628e444e8e618767544ddffb27d9e6e20ed97f5/Cargo.toml#L9-L11)）。
  - GitHub API 归类为 `Other` / `NOASSERTION` —— 与"非标准协议"一致（来源：<https://api.github.com/repos/Yunlk/uai-core>）。
- 若必须在 SPDX 语境中登记，只能用自定义 `LicenseRef-`。**本报告建议标识符：`LicenseRef-UAI-Core-NonCommercial-1.0`** —— ⚠️ 这是我方自拟标签，**不是官方 SPDX 标识符**，作者未使用。

### 2.2 LICENSE 原文关键条款（全文 74 行，中文为准，英文摘要见第 66-74 行）

| 条款 | 位置 | 要点 |
| --- | --- | --- |
| 一、许可授予 | [`LICENSE#L7-L18`](https://github.com/Yunlk/uai-core/blob/4628e444e8e618767544ddffb27d9e6e20ed97f5/LICENSE#L7-L18) | 免费、不可撤销、全球、非独占；允许使用/复制/发布/分发 (a)、修改/改写/翻译/重构并创作衍生作品 (b)，**但限于学习、研究、教学、评估、个人娱乐及其它任何非商业目的** (c)；不限源码或编译形式 |
| 二、禁止商业使用 | [`LICENSE#L20-L32`](https://github.com/Yunlk/uai-core/blob/4628e444e8e618767544ddffb27d9e6e20ed97f5/LICENSE#L20-L32) | **禁止任何商业性使用**，明确列举：销售/出租/许可/订阅 (a)；作为付费产品或付费服务（**含 SaaS、托管服务、技术支持**）的组成部分 (b)；营利性组织内为直接商业收益使用 (c)；**以此为基础提供收费的咨询、代做、代刷等有偿服务 (d)**。商业使用须事先取得版权持有人**书面授权** |
| 三、条件 | [`LICENSE#L34-L42`](https://github.com/Yunlk/uai-core/blob/4628e444e8e618767544ddffb27d9e6e20ed97f5/LICENSE#L34-L42) | (a) 复制/分发/衍生作品须**完整保留**本协议与版权声明；(b) 修改版须显著标明已修改，不得暗示原作者背书；(c) **衍生作品若对外分发，须采用与本协议同等的许可条款**（同样允许自由改写、同样禁止商业使用）；(d) 不得移除或规避免责声明与合规提示 |
| 四、免责声明 | [`LICENSE#L44-L54`](https://github.com/Yunlk/uai-core/blob/4628e444e8e618767544ddffb27d9e6e20ed97f5/LICENSE#L44-L54) | 按"现状"提供，无任何担保；作者不承担任何责任；使用者须自行确保符合平台服务条款、学校规章与当地法律 |
| 五、终止 | [`LICENSE#L56-L62`](https://github.com/Yunlk/uai-core/blob/4628e444e8e618767544ddffb27d9e6e20ed97f5/LICENSE#L56-L62) | 违约自动终止全部授权，须停止使用并销毁副本；**版权持有人保留在后续版本中变更许可条款的权利**；已按旧版本获取的副本适用其获取时的条款 |

### 2.3 NOTICE / 额外限制 / 传染性

- **不存在 NOTICE 或 COPYING 等附加文件**：全仓检索许可类文件名，仅命中 `./LICENSE` 一个（`find . -iname 'NOTICE*' -o -iname 'COPYING*' -o -iname '*LICENSE*'`）。（已核验）
- **是否 AGPL 式网络传染？** **不是**。LICENSE 全文不含 `AGPL`/`GPL`/`Affero` 字样；其"传染性"来自第三(c)条，**仅"对外分发"时触发**，未声明"网络交互即视为分发"（来源：LICENSE 全文，见上表）。
- **是否有专利/商标条款？** 全文未提及专利与商标（事实：检索无"专利"字样）。→ 即**无明示专利授权**；其法律后果属法律解读，**未验证**，需法务确认。
- **是否为 OSI 认证协议？** **否**。"禁止商业使用"与开源定义第 6 条（不得歧视领域）冲突，因此该协议不是 OSI 认可的开源许可，属"源码可见 / 非商业许可"。→ 事实依据：条款二；"非 OSI 认证"为据此得出的判断，未查证 OSI 官方名录（**未验证**）。

### 2.4 若仓库无 LICENSE 会怎样

不适用：本仓库**有** LICENSE 文件（74 行，4099 字节），故不写"保留所有权利，不可复用"。但需注意其实际效果更接近：**"源码可见、非商业可复用、商业需书面授权、分发衍生作品须同等条款"**。

---

## 3. 第三方依赖清单及其协议

### 3.1 直接依赖（来源：[`Cargo.toml#L25-L39`](https://github.com/Yunlk/uai-core/blob/4628e444e8e618767544ddffb27d9e6e20ed97f5/Cargo.toml#L25-L39)，共 12 项）

| crate | Cargo.toml 声明 | Cargo.lock 实际解析 | 协议（deps.dev 元数据） |
| --- | --- | --- | --- |
| `aes` | 0.9.3 | 0.9.3 | Apache-2.0 OR MIT |
| `cbc` | 0.2.1 (`alloc`) | 0.2.1 | Apache-2.0 OR MIT |
| `futures-util` | 0.3.32 | 0.3.32 | Apache-2.0 OR MIT |
| `hex` | 0.4.3 | 0.4.3 | Apache-2.0 OR MIT |
| `hmac` | 0.12.1 | 0.12.1 | Apache-2.0 OR MIT |
| `reqwest` | 0.12.28 (blocking/cookies/json/rustls-tls) | 0.12.28 | Apache-2.0 OR MIT |
| `serde` | 1.0.229 (derive) | 1.0.229 | Apache-2.0 OR MIT |
| `serde_json` | 1.0.151 | 1.0.151 | Apache-2.0 OR MIT |
| `sha1` | 0.10.7 | 0.10.7 | Apache-2.0 OR MIT |
| `sha2` | 0.10.9 | 0.10.9 | Apache-2.0 OR MIT |
| `tokio` | 1.52.3 (rt-multi-thread/time/macros/signal) | **1.53.1**（lock 解析结果） | MIT |
| `tokio-tungstenite` | 0.28.0 (rustls-tls-webpki-roots) | 0.28.0 | MIT |

### 3.2 全量传递依赖（来源：`Cargo.lock` 解析 + deps.dev v3 API 逐包查询）

- `Cargo.lock` 共 **182** 个 package = 1 个工作区根（`uai-core 0.1.2`，`source` 为空）+ **181** 个 crates.io registry 包。
- 181 个 registry 包的协议分布（逐包查询 <https://api.deps.dev/v3/systems/cargo/packages/{name}/versions/{version}> 的 `licenses` 字段）：

| 协议表达式（元数据原样） | 包数 | 代表 |
| --- | --- | --- |
| `Apache-2.0 OR MIT` | 119 | `reqwest`、`serde`、`aes`、`tokio-rustls`、`tungstenite`… |
| `MIT` | 23 | `tokio`、`hyper`、`tower`、`tracing`、`tokio-tungstenite`… |
| `Unicode-3.0` | 18 | `icu_*`、`zerovec`、`yoke`（`url`/`idna` 的传递依赖） |
| `Apache-2.0 OR MIT OR Apache-2.0 WITH LLVM-exception` | 3 | `wasi`、`wasip2`、`wit-bindgen` |
| `Apache-2.0 OR ISC OR MIT` | 2 | `hyper-rustls`、`rustls` |
| `Apache-2.0 OR MIT OR Zlib` | 2 | `lru-slab`、`tinyvec` |
| **`Apache-2.0 OR LGPL-2.1-or-later OR MIT`** | 2 | **`r-efi 5.3.0` / `r-efi 6.0.0`** |
| `ISC` | 2 | `rustls-webpki`、`untrusted` |
| `CDLA-Permissive-2.0` | 2 | `webpki-roots` |
| `Apache-2.0 OR BSD-2-Clause OR MIT` | 2 | `zerocopy`、`zerocopy-derive` |
| 其它单项（均宽松） | 6 | `memchr`(MIT OR Unlicense)、`ring`(Apache-2.0 AND ISC)、`ryu`(Apache-2.0 OR BSL-1.0)、`subtle`(BSD-3-Clause)、`sync_wrapper`(Apache-2.0)、`unicode-ident`(Unicode-3.0 AND (Apache-2.0 OR MIT)) |

### 3.3 copyleft 项判定

- 用正则 `GPL|AGPL|LGPL|MPL|CDDL|EPL|CC-BY-SA|SSPL|OSL|CPL` 扫描 181 个包的协议串，**仅命中 2 条**：`r-efi 5.3.0`、`r-efi 6.0.0`，均为三选一 `Apache-2.0 OR LGPL-2.1-or-later OR MIT`。
- **这三者是 `OR`（择一）关系**：采用方可以选择 `Apache-2.0` 或 `MIT`，因此**不产生 LGPL 的传染义务**（这是对 `OR` 语义的通行解读，非法律鉴证）。
- **全库无 GPL / AGPL / LGPL-only / MPL / SSPL 依赖。**
- ⚠️ 方法学限制：本清单来自 **crates.io 发布元数据的第三方聚合（deps.dev）**，**不是**逐包阅读 LICENSE 原文。因此"逐包 LICENSE 原文与元数据一致"这一层**未验证**；但"依赖侧无 copyleft 障碍"这一结论置信度高（181/181 均已查到非空协议串，无查询失败项）。
- 补充事实：本环境访问 crates.io 官方 API（`/api/v1/crates/{name}`）时 `license` 字段**缺失**（已核验返回体无 `license` 键）；因此改用 deps.dev 作为可核验来源。这一现象**不代表**线上 crates.io 的行为。
- **结论：依赖许可不是引用障碍；障碍只在 uai-core 项目自身的非商业许可。**

---

## 4. 技术栈与架构

### 4.1 技术栈（来源：[`Cargo.toml#L1-L23`](https://github.com/Yunlk/uai-core/blob/4628e444e8e618767544ddffb27d9e6e20ed97f5/Cargo.toml#L1-L23)）

- 语言：**Rust**，`edition = "2024"`，`rust-version = "1.87"`（注释说明真实 MSRV 是 1.87，因为用了 1.87 才稳定的 `is_multiple_of`）。
- 构建：Cargo，提交了 `Cargo.lock`；`Cargo.toml` **没有** `repository` / `homepage` / `documentation` 字段。
- 网络栈：`reqwest`（`rustls-tls`，不依赖系统 OpenSSL）+ `tokio` + `tokio-tungstenite`（WebSocket）。
- 双 target：`[lib] name = "uai_core"`（`src/lib.rs`）+ `[[bin]] name = "uai"`（`src/bin/uai/main.rs`）。作者明确把"接口层做成可 import 的 lib"当作与旧工程的关键差异（来源：[`Cargo.toml#L13-L23`](https://github.com/Yunlk/uai-core/blob/4628e444e8e618767544ddffb27d9e6e20ed97f5/Cargo.toml#L13-L23)）。
- **未发布到 crates.io**：`GET https://crates.io/api/v1/crates/uai-core` → **HTTP 404 Not Found**（已核验）→ 不存在 `cargo add uai-core` 这条路径。

### 4.2 目录结构与入口

```
uai-core/
├── Cargo.toml / Cargo.lock / LICENSE / README.md (18829 B) / AGENTS.md (4711 B)
├── .github/workflows/release.yml      CI：verify → 三平台 build → Release(latest / v*)
├── examples/                          8 个诊断探针（dur_blitz, dur_map_probe, grade_audit,
│                                      hours_audit, kick_real_rooms, leafs_audit, rate_probe, url_probe）
└── src/                               44 个 .rs，共 16118 行
    ├── lib.rs            (47)   crate 入口 + 分层文档 + 两套令牌说明
    ├── endpoints.rs      (253)  全部域名与路径常量的唯一出处
    ├── transport.rs      (464)  HTTP 传输：鉴权头 + 限流退避 + 错误呈现
    ├── crypto.rs         (144)  AES-ECB 解密（内容） / AES-CBC 加密（登录）
    ├── annotator.rs      (182)  自铸 x-annotator-auth-token
    ├── session.rs        (185)  登录态（门户 JWT 与接口 token 分开存）
    ├── error.rs          (161)  统一错误与类别
    ├── api/              (18 文件)  接口层：一文件一端点
    ├── question/         (16 文件)  题型层：一文件一题型
    └── bin/uai/          main.rs(46) / cli.rs(991) / commands.rs(2334)  CLI
```

（行数来源：`wc -l` 一次性只读统计；目录来源：`find`。入口文件：`src/lib.rs`、`src/bin/uai/main.rs`。）

### 4.3 核心模块职责（来源：[`src/lib.rs#L15-L35`](https://github.com/Yunlk/uai-core/blob/4628e444e8e618767544ddffb27d9e6e20ed97f5/src/lib.rs#L15-L35)、[`AGENTS.md#L38-L53`](https://github.com/Yunlk/uai-core/blob/4628e444e8e618767544ddffb27d9e6e20ed97f5/AGENTS.md#L38-L53)）

- `api/`：**接口层不做业务判断**，只拼 URL（路径常量取自 `endpoints.rs`）、发请求、把响应解析成强类型，不缓存。
- `question/`：**题型层不发网络请求**，把平台返回的题目还原成可作答结构；覆盖 16 种题型（`single_choice`/`multi_choice`/`fill_blank`/`banked_cloze`/`dropdown`/`sequence`/`oral`/`role_play`/`subjective`/`study`/`upload`/`objective`/`record`/`parse`/`oral_state`/`mod`）。
- `transport.rs`：鉴权头 + 限流退避 + 错误呈现（AGENTS.md 规定批量提交每组至少间隔 20 秒，限流退避 45 秒）。
- `annotator.rs` / `crypto.rs` / `session.rs` / `endpoints.rs` / `error.rs`：见上表。
- **读写分离**是它的显式设计目标：只有 `api/submit.rs` 会写服务端（外加 `media` 未接入的上传链）（来源：[`src/api/mod.rs#L25-L35`](https://github.com/Yunlk/uai-core/blob/4628e444e8e618767544ddffb27d9e6e20ed97f5/src/api/mod.rs#L25-L35)）。
- 自述测试规模：README/AGENTS.md 称 `cargo test --offline` 有 **319 个离线测试**、clippy 0 warning → **未验证**（未运行；本机无 `cargo`/`rustc`，且按任务约束不得运行仓库脚本）。

---

## 5. 它实际做了什么：通用框架、平台脚本，还是爬虫 SDK？

**结论：三者都不是"通用"意义上的 —— 它是「U校园（Unipus）单一平台」的协议客户端 SDK（Rust lib）+ CLI，并附带一组只读诊断探针。**

### 5.1 平台覆盖（唯一平台，已核验）

来源：[`src/endpoints.rs#L20-L34`](https://github.com/Yunlk/uai-core/blob/4628e444e8e618767544ddffb27d9e6e20ed97f5/src/endpoints.rs#L20-L34)、[`src/api/mod.rs#L6-L23`](https://github.com/Yunlk/uai-core/blob/4628e444e8e618767544ddffb27d9e6e20ed97f5/src/api/mod.rs#L6-L23)

| 域名 | 用途 | 鉴权 | 成功码 |
| --- | --- | --- | --- |
| `uai.unipus.cn` | 门户（书架、用户信息、考核方案、综合成绩） | 门户 SSO JWT（`Authorization`） | `code: 1` |
| `sso.unipus.cn` | 统一登录（密码 / 二维码 / ticket 换 JWT） | — | `code: "0"` |
| `ucontent.unipus.cn` | 内容（目录、题目、答案、进度、分组状态、提交、时长 WS） | 自铸 `x-annotator-auth-token` | `code: 0` |
| `umcs.unipus.cn` | 二维码登录 Socket.IO | — | — |
| `speech.unipus.cn` | 语音评测（WebSocket，**只评分不提交**） | SHA1 签名 | — |

- 全部是 `*.unipus.cn`（U校园 / Unipus），**没有任何其它平台**的适配。
- 端点表（15 个接口文件）见 `src/api/mod.rs` 的表格；唯一写接口是 `submit`。

### 5.2 能力边界（来源：README「能做什么 / 不做什么」段）

- **能**：登录并缓存会话（凭据只来自环境变量 `UAI_USER`/`UAI_PASS`）、读书架/班级/教材、读目录、解密题目内容、取标准答案、读作答状态与快照、读考核方案与综合成绩、按班级筛必修、组装并提交（限速、分批、默认先预览）、语音评测、学习时长上报（两条 WebSocket 链）。
- **不做**：考试/测验类任务（Roadmap 明示"尚未实现"，且规定只许写只读探针）、不伪造时间戳或分数、不内置账号/Cookie/代理。
- 形态上更接近 **SDK + CLI**（`uai-core` lib + `uai` bin），**不是**通用自动化框架（无插件/适配器抽象、无跨平台调度层），**也不是**通用爬虫 SDK（无通用抓取/解析引擎，全部是硬编码的 Unipus 端点与题型）。

### 5.3 与本项目的匹配度

- 本项目 `src/kedaibiao/platforms/neusoft/`（东软系）与 uai-core 的 Unipus 端点**零重合**；本项目有自己的适配器契约 `platforms/base.py`（`PlatformAdapter` / `Capabilities` / `AdapterContext`），uai-core 没有对应的可映射抽象。
- 本项目 `platforms/neusoft/__init__.py` 的现成说明即为："具体实现待 `docs/research/uai-core.md` 调研结论落地后再动工，避免在白盒未知的情况下写死接口" —— 本报告即该结论（来源：[`src/kedaibiao/platforms/neusoft/__init__.py:1-L5`](src/kedaibiao/platforms/neusoft/__init__.py)）。

---

## 6. 复用结论：可直接依赖 / 需 fork 改造 / 只能借鉴思路 / 不可用

### 6.1 结论

> **只能借鉴思路（架构、工程约束、协议研究纪律），不能直接依赖，也不建议 fork 改造后并入本项目。**

补充一句可操作的边界：**如果只是"你本人在自己机器上、非商业地用它的 CLI 处理 U校园 侧事务"，这属于 LICENSE 第一(c)条允许的个人非商业用途；但不能把它的代码/二进制并入本仓库、不能再分发、不能用于任何商业目的。**（该建议基于 LICENSE 文本理解，不构成法律意见 —— **需法务确认，未验证**。）

### 6.2 逐条排除过程

| 方案 | 技术可行性 | 许可可行性 | 判定 |
| --- | --- | --- | --- |
| A. `cargo add uai-core` 作为依赖 | ❌ 未发布到 crates.io（404 已核验）；且本项目是 Python，无法 import Rust crate | — | **不可行** |
| B. Git 依赖 / vendored 源码（Rust） | ⚠️ Rust 侧可行；对本 Python 项目无意义 | ❌ 需保留协议 + 衍生作品同等条款 | **不采用** |
| C. 把 Release 三平台二进制当外部 CLI 用（`subprocess`） | ✅ 技术上可行（有 `uai-linux-x86_64` 等资产） | ⚠️ 自用非商业可以；**随本项目分发则冲突**（见 6.3） | **仅限个人非商业自用；不进仓库、不分发** |
| D. 只借鉴思路，用 Python 独立重写 | ✅ 与本项目 `PlatformAdapter` 契约天然契合 | ✅ 思路/方法/协议事实不受版权保护（通识判断，未作法律验证），但**严禁复制代码、注释、文档文本与命名** | **推荐** |

**补充理由（为什么"连 fork 改造也不划算"）：**
1. 目标平台不同：uai-core 的"可复用资产"= Unipus 的端点表 + Unipus 的 AES/annotator 鉴权 + 16 种 Unipus 题型解析。对东软系适配器，这些资产的复用率接近 0，改造成本高于重写。
2. 语言不同：Rust（edition 2024 / MSRV 1.87）→ Python（`requires-python >= 3.11`），跨语言只能进程间调用或 FFI/PyO3，工程复杂度高。
3. 维护风险：单人、7 次提交的项目，README 自述"不保证长期可用"，`节点` 随时可能变更（其自身 README 也这么写）。
4. 本项目当前策略是**核心零依赖**（`pyproject.toml:15-L16`：`dependencies = []`，"核心运行时只依赖标准库"） —— 引入一个外部二进制运行时与其许可约束，与既定策略相悖。

### 6.3 许可证兼容性矩阵（本项目 `pyproject.toml:11` = `Proprietary`）

| 使用场景 | uai-core 是否允许 | 与本项目当前许可（Proprietary）是否兼容 |
| --- | --- | --- |
| 只读学习、阅读源码 | ✅ 第一(c)条 | ✅ |
| 本地个人非商业使用（含跑 CLI） | ✅ 第一(c)条 | ✅ |
| 修改并自用、不分发 | ✅ 第一(b)条 | ✅ |
| **随本项目分发**（源码或二进制，含打包进 Release/镜像） | ⚠️ 允许，但须完整保留协议与版权声明、标明修改、且**衍生作品对外分发须采用同等条款**（同样禁商用） | ❌ **与 `Proprietary` 冲突**（Proprietary 保留全部权利，无法满足"同等条款"） |
| 作为 Rust 库链接进同一产物 | 同上（分发即受约束） | ❌（且本项目是 Python，不适用） |
| **任何商业使用**（付费、SaaS、托管、技术支持、有偿代做代刷） | ❌ 第二(a)-(d)条，须书面授权 | ❌ |
| 依据其代码/文档独立重写为 Python | ✅（不受版权约束，前提是不复制表达） | ✅ |

### 6.4 如果将来必须用（决策前置条件）

满足**全部**条件才可重新评估：① 本项目明确定位为非商业且愿意整体采用"同等条款"（即放弃 Proprietary）；② 不介意交付物中携带 uai-core 的 LICENSE 与修改声明；③ 确实要做 Unipus 平台适配（当前是东软系，不是）；④ 接受单人项目的维护风险。任何一条不满足，则维持"只能借鉴思路"。

### 6.5 借鉴什么（可安全借鉴的"思路"，不涉表达复制）

- **分层纪律**：接口层只拼包/解析、不做业务判断；题型层不发网络请求；写操作集中到可一眼审完的少数文件（对应本项目 `platforms/base.py` + `tasks/` 的划分）。
- **端点常量唯一出处**（对照它 `endpoints.rs` 的思路）—— 本项目应在每个平台适配器内集中维护端点常量，避免多处重复。
- **"两套令牌别混用"的教训**：不同域名用不同鉴权体系，成功码还不统一（门户 `1` / ucontent `0` / SSO `"0"`）。→ 本项目适配器应显式区分"鉴权体系"与"成功判定"，不要用一套 success 判定套所有接口。
- **读写分离 + 默认干跑**：它与本项目 `settings.dry_run = True`（`config/settings.py:40`，"默认干跑，必须显式关闭才会真正提交"）理念一致，可互相印证。
- **限速纪律**：批量写操作强制间隔、限流后退避而非硬撞。
- **诊断探针与"实测"标注文化**：只读探针放 `examples/`，凡结论标注证据来源 —— 值得本项目 `docs/research/` 沿用。

---

## 7. 风险与未知项（`未验证` 清单）

**已核验的事实性风险：**

1. **许可风险（最高）**：非商业 + 衍生作品同等条款 + 无 SPDX 标识符 + 版权人保留后续改条款的权利（`LICENSE` 五、第 61-62 行）。用于任何商业场景即违约。
2. **许可不可逆地绑住分发**：一旦随本项目分发源码或二进制，必须保留协议、标明修改，且衍生作品须同等条款 —— 与 `Proprietary` 直接冲突。
3. **商业授权渠道不明**：`LICENSE`（74 行）与 README 均**未提供**版权持有人的联系方式或商业授权流程，只在 README「许可」段写"需要商业授权请先联系版权持有人取得书面许可"。→ **商业授权能否取得 = 未知**（已核验：两文件无邮箱/联系方式）。
4. **维护风险**：单人、7 次提交、5 天开发窗口、1 star、0 issue；最近代码提交 2026-09-27；README 自述"不保证长期可用"。
5. **它不是通用框架**：平台覆盖仅 Unipus；与本项目东软系目标零重合。
6. **合规/学术风险**：该工具会自动提交"必修"任务并上报学习时长，可能与 U校园服务条款、学校规章及学术诚信要求冲突（其 README/LICENSE 免责声明第 4 条把责任全部推给使用者）。本项目若借鉴同类能力，需自行完成合规评估。
7. **本项目侧许可尚未落地**：仓库当前无 `LICENSE` 文件，`pyproject.toml:11` 写的是 `Proprietary`。若后续改许可，第 6 节的兼容性判断需重做。

**未验证清单（不得据此行动）：**

| # | 未验证项 | 原因/说明 |
| --- | --- | --- |
| U1 | README/AGENTS.md 声称的 **319 个测试全绿、clippy 0 warning** | 未运行（约束禁止运行仓库脚本；本机无 `cargo`/`rustc`）→ **未验证** |
| U2 | 各依赖 crate 的**LICENSE 原文**是否与 crates.io 元数据一致 | 采用 deps.dev 聚合元数据，未逐包下载原文核对 → **未验证** |
| U3 | 仓库是否为旧工程 `UAI-Textbook-Console` 的迁移/重写 | `src/lib.rs#L6-L11` 提到该旧工程名，但其存在性、许可、代码血缘均未核验 → **未验证** |
| U4 | 2026-09-27 的提交是否在 2026-10-18 一次性推送到 GitHub | 由 `created_at` 晚于最后提交推断，GitHub API 无推送历史可查 → **推断，未验证** |
| U5 | 是否曾被发布到 crates.io（其它名字/已撤回） | 仅核验 `uai-core` 与 `uai_core` 风格命名下的 404；未做穷举搜索 → **未验证** |
| U6 | 是否存在作者主页/README 之外的商业授权联系渠道 | 未检索作者其它仓库与主页 → **未验证** |
| U7 | 该协议在具体司法辖区的可执行性、"组合作品 vs 衍生作品"边界 | 属法律判断，本报告只陈述文本 → **未验证，需法务** |
| U8 | 依赖树中 `Unicode-3.0`（18 个 `icu_*`/`zerovec` 等）在**再分发**时的署名/声明义务细节 | 未核对 Unicode 许可原文的分发义务条款 → **未验证**（对"仅借鉴思路"路线无影响） |
| U9 | GitHub Actions 资产是否可下载、二进制是否可运行 | 未下载、未执行任何二进制 → **未验证** |

---

## 附录 A：核验方法与可复现命令

**只读约束声明**：本次调研**没有**运行仓库内任何脚本，**没有**安装其依赖，**没有**在项目仓库内 clone。所有克隆都在 `/tmp` 下的**一次性沙箱**（每次 bash 调用结束即销毁，故本报告不引用 `/tmp` 路径作为持久来源，仅引用 GitHub 永久链接与 API）。

可用工具（已核验）：`git`、`wget`、`python3`、`jq`、`node` 存在；**`curl`、`cargo`、`rustc` 不存在**。

```bash
# 1) 可克隆性 + 元数据
git clone --depth 1 https://github.com/Yunlk/uai-core /tmp/uai    # 退出码 0
cd /tmp/uai && git rev-parse --abbrev-ref HEAD                    # main
git log -1 --format='%H %ad %an %s' --date=iso                    # 4628e444... 2026-09-27
find . -path ./.git -prune -o -type f -print | wc -l              # 60
wc -l $(find src -name '*.rs') | tail -1                          # 16118 total

# 2) 协议与依赖（文本）
cat LICENSE ; cat Cargo.toml ; find . -iname 'NOTICE*' -o -iname 'COPYING*' -o -iname '*LICENSE*'

# 3) GitHub API（本环境 web_fetch 可达）
#    https://api.github.com/repos/Yunlk/uai-core            → stars/forks/license.spdx_id/created_at
#    https://api.github.com/repos/Yunlk/uai-core/commits    → 7 条提交
#    https://api.github.com/repos/Yunlk/uai-core/tags       → v0.1.2 / latest
#    https://api.github.com/repos/Yunlk/uai-core/releases   → latest 预发布资产

# 4) 依赖协议审计（deps.dev 聚合元数据；bash 内网络可达，本环境 curl 缺失故用 python3）
python3 - <<'PY'
import re,json,urllib.request
lock=open('Cargo.lock',encoding='utf-8').read()
for n,v in re.findall(r'\[\[package\]\]\s*\nname = "([^"]+)"\s*\nversion = "([^"]+)"(?:\nsource = "([^"]*)")?', lock):
    pass  # 逐包 GET https://api.deps.dev/v3/systems/cargo/packages/{n}/versions/{v} → licenses
PY

# 5) 未发布核验
python3 -c "import urllib.request;urllib.request.urlopen('https://crates.io/api/v1/crates/uai-core')"  # HTTP 404
```

**环境限制记录**：`read_url` 工具对 `crates.io`、`docs.rs` 报 "resolves to a non-public IP address"（工具侧 DNS 限制），故 crates.io 相关核验改由 bash 内 `python3 urllib` 完成；bash 侧 `curl` 缺失，同样改用 `python3`。

## 附录 B：来源清单

**仓库与代码（全部锚定 commit `4628e444e8e618767544ddffb27d9e6e20ed97f5`）**

- 仓库主页：<https://github.com/Yunlk/uai-core>
- LICENSE：<https://github.com/Yunlk/uai-core/blob/4628e444e8e618767544ddffb27d9e6e20ed97f5/LICENSE>（raw 版已与克隆内容逐字比对一致）
- Cargo.toml：<https://github.com/Yunlk/uai-core/blob/4628e444e8e618767544ddffb27d9e6e20ed97f5/Cargo.toml>
- Cargo.lock：<https://github.com/Yunlk/uai-core/blob/4628e444e8e618767544ddffb27d9e6e20ed97f5/Cargo.lock>
- README.md：<https://github.com/Yunlk/uai-core/blob/4628e444e8e618767544ddffb27d9e6e20ed97f5/README.md>
- AGENTS.md：<https://github.com/Yunlk/uai-core/blob/4628e444e8e618767544ddffb27d9e6e20ed97f5/AGENTS.md>
- src/lib.rs：<https://github.com/Yunlk/uai-core/blob/4628e444e8e618767544ddffb27d9e6e20ed97f5/src/lib.rs>
- src/endpoints.rs：<https://github.com/Yunlk/uai-core/blob/4628e444e8e618767544ddffb27d9e6e20ed97f5/src/endpoints.rs>
- src/api/mod.rs：<https://github.com/Yunlk/uai-core/blob/4628e444e8e618767544ddffb27d9e6e20ed97f5/src/api/mod.rs>
- .github/workflows/release.yml：<https://github.com/Yunlk/uai-core/blob/4628e444e8e618767544ddffb27d9e6e20ed97f5/.github/workflows/release.yml>

**API 来源**

- <https://api.github.com/repos/Yunlk/uai-core>（stars/forks/license/created_at/pushed_at）
- <https://api.github.com/repos/Yunlk/uai-core/commits?per_page=100>
- <https://api.github.com/repos/Yunlk/uai-core/tags>
- <https://api.github.com/repos/Yunlk/uai-core/releases>
- <https://api.github.com/repos/Yunlk/uai-core/contributors>
- <https://api.deps.dev/v3/systems/cargo/packages/{name}/versions/{version}>（逐包 `licenses` 字段）
- <https://crates.io/api/v1/crates/uai-core> → HTTP 404（未发布）

**本项目（本地路径）**

- [pyproject.toml](pyproject.toml) —— `license = { text = "Proprietary" }`（第 11 行）、`dependencies = []`（第 15-16 行）、`requires-python = ">=3.11"`（第 10 行）
- [src/kedaibiao/platforms/base.py](src/kedaibiao/platforms/base.py) —— 适配器契约 `PlatformAdapter` / `Capabilities` / `AdapterContext`
- [src/kedaibiao/platforms/neusoft/__init__.py](src/kedaibiao/platforms/neusoft/__init__.py) —— 东软系适配器，实现待本报告结论
- [src/kedaibiao/config/settings.py](src/kedaibiao/config/settings.py) —— `dry_run: bool = True`（第 40 行）
