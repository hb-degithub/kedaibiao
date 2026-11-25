# 极验滑块：开源项目是怎么做的（2026-11-25 调研）

> 结论先行：**没人手写 `w`**。能找到的、还在维护的实现都做同一件事 ——
> **把平台自己那份 JS 跑起来**，让它算 `w`；手写的只有「图片 → 缺口坐标」。
> 另外，我们当前这套（`gt.0.4.9` + `slide.7.9.3` + `gct.js`，域名
> `api.geetest.com` / `api.geevisit.com`，参数 `gt/challenge/s/c`）属于
> **v3 "slide3" 世代**，而仓库里 vendor 的 `jiyan.js` 是更早的一代 ——
> 这就是 `ajax.php` 回 `{"message":"fail"}` 的原因。

## 1. 参考项目 A：`ravizhan/geetest-v4-slide-crack`（AGPL-3.0，纯 Python）

目标：**Geetest v4**（`gcaptcha4.geetest.com` + `gcaptcha4.js`）。

它的 `w` 构造（`crack.py`）：

```python
w = hex(AES_CBC_PKCS7(json(payload), key=随机16字节, iv=b"0000000000000000")) \
    + hex(RSA_PKCS1v15(key, 硬编码的 PEM 公钥))
```

payload 里必须带：

| 字段 | 值 |
| --- | --- |
| `setLeft` | 拼图块要放到的 x（**不是图片像素**，见下文） |
| `passtime` | 拖动耗时（毫秒） |
| `userresponse` | `setLeft / 1.0059466666666665 + 2` |
| `lot_number` / `process_token` | 来自 `load` 接口 |
| `pow_msg` / `pow_sign` | 工作量证明：`md5/sha1/sha256(version\|bits\|hashfunc\|datetime\|captcha_id\|lot_number\|\|<随机 hex>)` |
| `gee_guard` / `em` | 固定的一堆上报字段 |
| `<混淆参数名>` | 从 `gcaptcha4.js` 里**正则抽出来**的一个键值对 |

两个最值得记住的细节：

1. **坐标不是图片坐标**。作者用 YOLO 检测出 `x`，画出来明明对齐，但 JS 里的值
   对不上；最后用 Excel 拟合出线性关系：
   `setLeft = detect_x * 0.9862 - 11.317`。他在 README 里原话是
   「始终不知道那诡异的数是怎么来的」。
2. **参数要从当前 JS 里现抽**（`random_parameter.py`）：用正则从 `gcaptcha4.js`
   里取出混淆的键名、再用一段自定义 XOR 解出一串常量表 —— 所以**写死参数会过期**。

## 2. 参考项目 B：`Gaimoydev/Geetest-w`（**无 LICENSE**）

一个 1.2 MB、控制流平坦化 + 字符串表混淆的 `w.js`，README 自称
「25 最新 geetest w 参数」「25/1/28 还能用」。对外只暴露一个
`main(payload, params)`：

```js
// 伪代码，由 tail 段还原
key = random16()
if (params.c === 2) symmetric = AES({key, mode, iv})   // 类型 2 走 AES
else                symmetric = identity             // 类型 1 原样
asymmetric = RSA(params.s)
return asymmetric.encrypt(payload, key) + symmetric.encrypt(payload, key)
```

**不能并入本仓库**：仓库 `docs/license-policy.md` 要求 vendor 的代码必须是
MIT / Apache / BSD / AGPL 兼容且留 LICENSE。这个仓库没有 LICENSE 文件 ——
可以读来当参考，不能复制进仓库。

## 3. 对我们这个 build 意味着什么

| 事实 | 影响 |
| --- | --- |
| 我们是 v3 "slide3"（`get.php` + `ajax.php` + `s`） | v4 那套 `lot_number/pow` 用不上 |
| `jiyan.js` 是更早一代的 v3 | `compute_w` 出来的 `w` 服务端不认 → `fail` |
| payload 里有 `gct_path`（指向当前 `gct.js`） | **可以按 A 项目的思路，把这份 JS 拉下来跑** |
| 图片还原 + 缺口定位已实测正确 | 这一半不用动 |

## 4. 三条可行路线（按推荐顺序）

**① 运行时取 `gct.js`，在 Node 里算 `w`（推荐）**

payload 的 `gct_path` 给出当前 JS 地址；登录时**运行时下载**（不落库、不再分发），
丢进 Node + 最小 DOM 垫片，调它内部的 `w` 构造器。这与参考项目「一键反混淆 JS」
是同一思路，但**不在仓库里存 Geetest 的代码**，只存调用驱动的适配层。

- 优点：算法一变，取到新 JS 就自动跟上；
- 风险：`gct.js` 里可能探测 `navigator`/`canvas`，需要垫片；
- 前置：Node（仓库已有 `node` 依赖路径，`geetest/v3.py` 已经在用）。

**② 继续用浏览器拖拽，但把偏移量扫出来**

已实测：拖到位后拼图块与缺口**逐像素吻合**（`piece_x == gap`），但极验回
「请正确拼合图像」；拖太快回「怪兽吃了拼图」。说明位置容差比想象的严，
或 JS 判定值与画布像素之间有固定偏移（正是 A 项目拟合 `-11.317` 的那类东西）。

做法：对同一挑战扫 `dx-6 … dx+6`（步长 2），每次拖完读面板状态，命中即停。
成本低、不需要任何 JS，但**成功率取决于偏移是否稳定**。

**③ Cookie 导入（不碰极验）**

本机浏览器登录一次 → `kedaibiao capture unipus --cookie '…'` →
`_bootstrap_session_from_cookies()` 用 `jwt` 直接恢复门户会话。
这是最快让「U校园 全接口测试」跑起来的路径，极验可以之后再攻。

## 5. 不采用的做法

- ❌ 把 `Gaimoydev/Geetest-w/w.js` 复制进仓库 —— 无 LICENSE，违反本仓库许可政策；
- ❌ 把 `gct.js` 静态 vendor 进来 —— 那是 Geetest 的私有代码，同样不该再分发；
- ❌ 继续维护 `jiyan.js` 那条路 —— 它对应的是更早的一代，再改也追不上；
- ❌ 画死坐标偏移量（例如直接抄 `0.9862 / -11.317`）—— 实测套到我们的页面上
  仍然「请正确拼合图像」，说明那是 v4 页面的几何，不能跨版本复用。

---

## 6. 落地进展（2026-11-25，按方案① 试了一轮）

### 做了什么

1. 从 payload 的 `gct_path` 取到当前 JS：
   `https://static.geetest.com/static/js/gct.b71a9027509bc6bcfef9fc6a196424f5.js`
   —— 只有 **21 KB**（不是参考 B 那种 1.2 MB），静态资源可直接运行时下载。
2. 在 Node + 最小 DOM 垫片里跑起来：它确实注入了全局 `_gct`（源码里不含这个
   字符串，是**运行时从字符串表解出来的**，和参考 A 的 `random_parameter.py`
   是同一套手法）。
3. 探 API：
   | 调用 | 结果 |
   | --- | --- |
   | `_gct()` / `_gct(0..7)` | 返回一个数字（时间戳类） |
   | `_gct({})` | 返回对象，**只有 `lang` / `ep`** |
   | `_gct({}).w` | `undefined` |

   也就是说 `_gct` 是**配置获取器**，真正的密码工具箱（`n/s/e/es/en/w/wn/ws/f`
   九件套）是 IIFE 内部的私有对象。
4. 按「形状」在**活页面**里全量扫 `window`：找同时有 `w` 且 `en`/`es` 的对象
   —— **NOT FOUND**（工具藏在闭包里，不暴露到 window）。

### 拖拽路线的新数据点

- 给 CDP 的 `mouseMoved` 补上 `buttons=1`（按着左键）——这是之前缺的：
  没有它，页面的滑块处理器拿不到「正在拖动」状态，轨迹被判「怪兽吃了拼图」。
  已改进 `browser/cdp.py` 的 `mouse()` / `drag()`（生产代码）。
- 但补上之后仍在 `please 正确拼合图像` 与面板停留之间摆动；对同一 build 扫
  `delta = -6,-3,0,+3,+6`（每次新挑战）**没有一个能过**：

  ```
  delta=-6 gap=118 → shown      delta=+0 gap=123 → shown
  delta=-3 gap= 28 → wrong      delta=+3 gap=114 → wrong
  delta=+6 gap=104 → wrong
  ```

  结合参考 A 的经验（他也得靠 Excel 拟合坐标、并说「不知道那诡异的数怎么来的」），
  说明**坐标判定值与可视对齐之间确实存在非线性/版本相关的偏差**，
  靠扫偏移量不可靠。

### 结论

方案① 的**方向是对的**（运行时取 JS、不 vendor），但卡在 **`gct.js` 把入口藏进
闭包**：要么继续反混淆它的字符串表把导出挖出来（工作量不小，且下次改版还得再来），
要么换一个思路 —— **让页面自己算**：在页面里 hook 住 geetest 发出的 `ajax.php`
请求，读出它算好的 `w`，再用我们的 payload 重放。后者不碰加密，但要处理
「页面自己没有有效 payload」的问题。

再往下走之前建议先做一件事：把 U校园 的接口测试用 cookie 导入跑通（方案③），
不要让它继续被这一个环节卡住。
