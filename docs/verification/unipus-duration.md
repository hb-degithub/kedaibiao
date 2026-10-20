# U校园「学习时长上报」验证报告（2026-10-20）

- 验证者：unipus-duration（task-3 实现者）
- 环境：本机 Python 3.14.4；纯标准库
- 范围：学习时长 WS 链路（engine.io v3 over WSS）的**协议实现与离线验证**
- **本报告没有向 `*.unipus.cn` 发过任何真实请求**，也没有做任何联网冒烟测试。
  所有网络相关断言都来自「只读源码事实」+「假 socket / 假 transport 的离线用例」。
- 结论一句话：**可以用纯标准库拼出与参考实现一致的 engine.io v3 帧序列，并在
  完全离线条件下跑完整会话；但服务端今天是否仍接受、是否记账，仍未实测。**

---

## 0. 结论表

| 结论 | 证据等级 | 详见 |
| --- | --- | --- |
| URL = `wss://ucontent.unipus.cn/unipusiopoint/?EIO=3&transport=websocket&uuid=<open_id>&token=<annotator token>` | 【源码事实】 | §2.1 |
| `uuid` 用 `open_id`，`token` 用**自铸 annotator token**（不是门户 JWT） | 【源码事实】 | §2.1 |
| 帧序列：open → `40/userActivities,` → 回执 → 单次 kick → 每 20s `2` / 回 `3` | 【源码事实】 | §2.2 |
| kick 载荷字段名与取值（module/moduleGroup/client/url/tag1-3/appId/serviceId/sendTimestamp/openId/timer） | 【源码事实】 | §2.3 |
| kick 每条连接只发一次，重发会停表 | 【源码事实·参考实现注释】 | §2.3 |
| 客户端从不发 duration；账由服务端 `r_set` 的 `delta` 累加 | 【源码事实】 | §2.4 |
| 时长是账号级 `(module, moduleGroup)` 记账，无法定位到教材 | 【源码事实】 | §2.4 |
| 无 token/uuid 刷新；参考实现固定 5s 无限重连 | 【源码事实】 | §2.5 |
| `r_set` 广播的信封形状 | **【推断】** | §3.1 |
| `delta` 单位是秒 | **【推断（源码用法）】** | §3.1 |
| 服务端今天仍接受该帧序列 / 是否触发风控 | **【未验证】** | §3.2 |
| `40/userActivities` 回执的确切内容与时机 | **【未验证】** | §3.2 |
| 「冒充 mobile 才记账」 | **【未独立核对】**（参考实现自述） | §3.2 |
| 本模块的离线行为（帧编解码、握手、心跳计时、干跑、软着陆） | 【实测·离线】 | §4 |

---

## 1. 实现位置与 API

- 模块：`src/kedaibiao/platforms/unipus/duration.py`（新建；纯标准库，
  不 import `adapter.py`/`endpoints.py` 之外的同包模块，也不改 `browser/`）
- 单测：`tests/test_unipus_duration.py`（**65 个离线用例**）
- 模块级 API（不依赖 adapter，Lead 直接接线）：

```python
plan   = SessionDurationReporter(open_id, annotator_token).plan(600)        # 干跑计划
report = report_duration(open_id, annotator_token, 600)                     # 干跑（默认）
report = report_duration(open_id, annotator_token, 600, confirm=True)       # 真连：入房+心跳
report = report_duration(
    open_id, annotator_token, 600,
    confirm=True, enable_kick=True, module="cmgt", module_group="h5",
)  # 真连 + 单次 kick（显式闸门）
```

`token` 必须传 **annotator token**（`UnipusSession` 里对应自铸的那个，不是门户 JWT）。

---

## 2. 源码事实（来自 `docs/research/uai-core-submit-facts.md` §10）

> 证据来源：调研员对 `Yunlk/uai-core` commit `4628e444` 的**只读源码分析**
> （clone 到一次性沙箱，未执行；`duration.rs` 相关行号见研究文档 §10）。
> 这些是「代码这么写」的事实，不等于「服务端今天还接受」。

### 2.1 URL 与编码

`src/api/duration.rs#L240-L259`：

```text
wss://ucontent.unipus.cn/unipusiopoint/?EIO=3&transport=websocket&uuid=<open_id>&token=<annotator token>
```

- 自写 `urlencode` 只保留 `A-Za-z0-9-_.~`，其余 `%XX` 大写。
  本模块用 `urllib.parse.quote(safe="-_.~")`，行为等价。
- 源码注释：不带 `uuid`/`token` 也能完成握手，但服务端**不记账**。

### 2.2 帧序列（全部是文本帧）

| 时机 | 报文 |
| --- | --- |
| 服务端 open | `0{...}` |
| 客户端入命名空间 | `40/userActivities,`（**末尾有逗号**） |
| 收到以 `40/userActivities` 开头的回执后 | `42/userActivities,1` + kick JSON（**每连接一次**） |
| 每 20 秒（首拍跳过） | `2` |
| 收到服务端 `2` | `3` |
| 收到服务端 `3` | 忽略 |

### 2.3 kick 载荷

`["kick-v2", {...}]`，字段只有：

| 字段 | 值 |
| --- | --- |
| `module` / `moduleGroup` | 参考实现从房间池取（本模块要求调用方显式给） |
| `client` | 固定 `"U校园mobile"` |
| `url` | 固定 `https://ucontent.unipus.cn/_explorationpc_default/pc.html` |
| `tag1` / `tag2` | 同 `module` / `moduleGroup` |
| `tag3` | 字符串化 JSON：`{"microBlock":"<module>/<group>","source":"cloud","version":"1"}` |
| `appId` | `116` |
| `serviceId` | `"point"` |
| `sendTimestamp` / `timer` | 同一个毫秒时间戳 |
| `openId` | 会话 `open_id` |

serde_json 未开 `preserve_order`，参考实现的键按字母序输出；本模块用
`json.dumps(..., sort_keys=True, separators=(",", ":"), ensure_ascii=False)` 模仿。
`kick` 载荷里**没有课程/分组 ID**。

### 2.4 时长口径

- 客户端**从不发送 duration/秒数**；不存在「POST 一条带时长的消息」。
- 参考实现把服务端 `r_set` 广播里的 `delta` 按 `msUpdate` 去重后累加，
  `delta` 按秒使用；达标判断用累计记账秒数而不是墙钟。
- 所以本模块没有「上报 duration 的报文」，只有「维持连接 + 入房 + 可选一次 kick」。
- 记账是**账号级** `(module, moduleGroup)`，与教材/必修项无关。

### 2.5 重连与刷新

- 没有 token/uuid 刷新：过期表现是握手失败，参考实现会无限重连。
- 重连固定 5 秒、无指数退避、无上限；心跳 20s、状态汇报 5s。
- 本模块**不实现重连循环**（失败软着陆，由调用方决定是否重试）。

---

## 3. 推断与未验证（如实边界）

### 3.1 推断

| 项 | 为什么是推断 |
| --- | --- |
| `r_set` 广播的信封形状 | 研究文档只给出「报文含 `delta` 与 `msUpdate`」及解析行号，没有逐字给出完整帧；本模块用「找到第一个同时含这两个键的对象」的容错解析 |
| `delta` 单位为秒 | 源码把 `delta` 累加进注释为「已记账秒数」的 `credited_secs`，属用法推断 |
| `40/userActivities` 回执的匹配方式 | 按源码「以 `40/userActivities` 开头」判断，未实测真实回执文本 |
| 本模块按墙钟停止 | 参考实现按累计记账秒数达标停止；本模块为了确定性按墙钟停，`credited_seconds` 只做观测（不是停止条件） |

### 3.2 未验证

- **服务端是否仍接受该链路**：本次没有任何真实请求，无法判断。
- **kick 是否真的产生记账、倍率/批次（`r_set` 节奏）**：未实测。
- **`client="U校园mobile"` 才记账**：参考实现自述，本调研未核对平台前端/服务端。
- **风控与配额**：源码注释称 60 房间会随机拒绝握手等，属自述，未复现。
- **token 有效期**：本模块不刷新；实际多久过期未知。
- **`Origin` 头是否必需**：做成显式参数 `origin`，默认 `None`（与源码最小请求一致）。
- **多房间提速**：参考实现的 boost 池（10 module × 6 group）本模块**刻意不实现**；
  它既未验证，也属于把学习时长放大的规避行为，合规风险需单独立项评估。

---

## 4. 实测记录（全部离线）

### 4.1 覆盖点

`tests/test_unipus_duration.py`（65 用例，0 真实网络）：

- engine.io 文本包编解码：`0/1/2/3/4/5/6`，空串与未知类型；
- RFC 6455：客户端帧掩码逐字节断言、服务端帧解析、126/127 扩展长度、
  RSV/分片拒绝、超大帧拒绝、流式 `read_frame`；
- HTTP Upgrade：请求头形状、`Sec-WebSocket-Accept`（RFC 公开样例 + hashlib 对照）、
  非 101、Accept 不匹配、错误域名；
- 传输层：客户端帧必须加掩码、WS ping 自动回 pong、close 帧上抛；
- 干跑：**用「一旦被调用就 AssertionError」的 transport 工厂证明不建连接**；
- 心跳：虚拟时钟 + 假 transport 跑 60s/25s/30s，断言 ping 次数、pong 计数、
  实际耗时、`2`→`3` 回包、pong 连续丢失阈值、软着陆；
- kick：入房回执后**只发一次** kick；无回执则跳过并告警；默认 `confirm=True`
  不 kick；`enable_kick` 缺 module/group 或与 `join_namespace=False` 组合被拒；
- r_set：容错解析、按 `msUpdate` 去重、只观测不决定停止；
- 凭据：计划/摘要/日志中 token 打码，不出现在任何输出里。

### 4.2 怎么跑

```bash
cd /home/hb/刷题刷课
PYTHONPATH=src python3 -m unittest tests.test_unipus_duration -v
PYTHONPATH=src python3 -m unittest discover -s tests -t .
```

本报告落笔时的结果：`Ran 1470 tests ... OK`（其中本模块 65 个）。

---

## 5. 遗留风险

1. **全部线上行为未验证**：源码事实 ≠ 服务端现状。真连前建议先用
   `confirm=True` 的**短会话**（例如 60s）看 open/入房回执/pong 是否正常，
   再决定是否 `enable_kick=True`。
2. **账号级记账**：即使成功，也无法把时长归到某本教材/某个必修项；
   按教材的时长账要走门户考核接口，两者别混。
3. **kick 的冒烟成本**：一旦发送就可能在平台留下记录；建议只在用户明确同意后启用。
4. **合规风险**：参考实现是非商业许可，其「时长加速」涉及平台 ToS 与校内规范；
   本模块只做单房间单次 kick，不做多房间倍率，但**是否启用仍需产品/合规决策**。
5. **不重连**：网络抖动会直接结束本次会话；重连与退避留给上层调度。
