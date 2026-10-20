# U校园「刷课」调研：进度到底怎么涨（2026-10-20 对照实验）

> 结论先行：**U校园目前没有独立的「刷课」入口，而且现有两条链路都推不动门户进度**。
> 本文记录实验证据，避免后来的人再从头猜一遍。

## 1. 现在有什么（刷新后用）

| 位置 | 能力 | 默认 |
| --- | --- | --- |
| 工具页 →「学习时长上报」 | engine.io v3 挂时长会话（20s 心跳，可发一次 kick） | 干跑，`confirm=true` 才连 |
| 作业页 →「一键全部提交」 | 提交任务组（含纯学习组） | 干跑，`confirm=true` 才真交 |
| CLI | `action unipus duration seconds=600 confirm=true` | 同上 |

## 2. 实验：三条链路，逐条验证

目标课程：`20001188271`（三 B班 · 视听说教程2），门户进度 **0/33**，
但目录里 261 个任务组**全部**有首交时间（跨班教材实例串号，规格 §6.3 讲过）。

### 2.1 实验 A：提交一个「没全对」的组

- 组：`Exercise 1`（`6f917c7d1082d9d`），提交前 `score=[1,1,1,1,1,0,0,0,0,0]`；
- 提交：`code=0`、`score_pct=1.0`、`server_finish=true`（服务端接受）；
- **回读 `progress/v2`：`score` 仍然是 `[1,1,1,1,1,0,0,0,0,0]`，一个字节都没变**；
- 门户进度 0/33 不变（即时 + 15 秒后各查一次）。

→ **平台留存策略是「一组只留首条记录」**（`flowStrategy.record_every_submit=false`）。
**重交不会改分**，所以「批量重交已做过的组」是白跑，还可能白白触发风控。

### 2.2 实验 B：挂 180 秒学习时长（带 kick）

- `wss://ucontent.unipus.cn/unipusiopoint/?EIO=3&transport=websocket&uuid=<open_id>&token=<annotator>`
- 结果：连接成功、入房成功、kick 被接受（服务端回 `43/userActivities,1[{"code":0,"msg":"ok: 首次进入房间"}]`）、
  8 次 ping / 8 次 pong；
- **`credited_seconds = 0`**，全程**没有收到任何 `r_set` 记账广播**；
- 门户进度、单元 `state.pass` 全部不变。

→ 3 分钟尺度上，时长链路**没有任何记账证据**。是否阈值更长（例如按 5 分钟一记）、
或 `module/moduleGroup` 取值不对，仍未验证。

### 2.3 实验 C：门户进度是按班「必修任务」算的

`POST /api/tla/courseStudyStrategy/detail`（带宿主四头）给出按班策略：

| 单元 | passScore | requiredTask |
| --- | --- | --- |
| Get your foot in the door / Strength from within / Sports for all | 0 | **0**（不要求） |
| Horns of a dilemma / Time off / Science empowers | 60 | **11 / 11 / 11** |

合计 **33 个必修任务** —— 正好等于门户 `totalPointNum=33`。
所以 `finishPointNum/totalPointNum` 是**「通过（≥passScore）的必修任务数 / 必修任务总数」**，
不是计分点之和，也不是「做过多少组」。

这 33 个必修组里，19 个已全对或无数值，14 个没全对（含主观题组大面积 0 分）；
但门户仍是 0/33 —— 说明**门户口径与 ucontent 的组级记录不是同一套账**，
仅靠提交/挂时长都没有推动它。

## 3. 对产品的结论

1. 「一键全部提交」现在默认**跳过所有已完成组**（`skip_done=true`）：
   已完成但没全对的也跳过，日志会写明「平台留存首条记录，重交不改分」。
   想重交必须显式 `skip_done=false`（基本属于无效操作，仅供排查）。
2. 「刷课」若要做成真能涨进度的功能，还缺一次**更长的时长实验**（≥6 分钟，看是否出现
   `r_set` 记账帧）以及**必修口径对齐**（按班 33 个必修组，而不是全 261 组）。
3. 在上面的证据补齐之前，不要对外宣称「能刷课」。
