# 极验 Geetest v3 滑块：识别、开源方案与移植计划

> 目标：让 unipus / pinco / itest 三个平台（共用 Unipus SSO）的登录能过验证码。
> ⚠️ **合规声明**：这是在**用户本人账号**上做自动化登录，属于个人自用工具。
> 极验是商业反机器人产品，本文与随附代码**只服务于用户自己的账号**，
> 不得用于批量注册、撞库、薅羊毛等场景。

---

## 1. 版本判定：是 v3，不是 v4

实测抓包（2026-10-19，itestcloud.unipus.cn 的 SSO 登录）：

| 判据 | 实测值 | 指向 |
| --- | --- | --- |
| 端点 | `api.geetest.com/gettype.php` → 200 存活 | v3 |
| 端点 | `api.geevisit.com/get.php`、`api.geevisit.com/ajax.php` | v3 |
| 参数 | **`challenge=<32位hex>`** | v3（v4 用 `lot_number` / `process_token`） |
| 类型 | `type=slide3` | v3 |
| 模块 | `slide.7.9.3.js`、`fullpage.9.2.0-guwyxh.js`、`gct.<hash>.js`、`gt.0.4.9.js` | v3 模块族 |
| v4 特征 | 已加载模块中 `gcaptcha4` 出现 **0 次** | 不是 v4 |
| v4 端点 | `gcaptcha4.geetest.com/load` → **403**（未被使用） | 不是 v4 |

> `gt.0.4.9.js` 的 `0.4.9` 是**加载器**版本，不是协议版本。
> 协议版本由它加载的 `slide.7.9.3` / `fullpage.9.2.0` 与端点形态决定。

**本站点参数**：`gt = a7cee00a24b6749063b7029cef6fa76f`，
模式 `fullpage` + `offline=false`（在线校验，无自校验后门）。

---

## 2. 还有个「4 位图片验证码」——但当前风险等级下不会出现

同期逆出了 Unipus 自研的图片验证码接口：

```http
POST /sso/4.0/sso/cip/image_captcha2
body {"param": AES128CBC_upper(random8)}        ← 与 Unipus 登录加密同一把密钥
→ {"rs":{"image":"<base64 JPEG>","encodeCaptha":"<320字符>","codeType":"0"}}

POST /sso/4.0/sso/captcha_verify
headers: timestamp: <必需>
body {captchaCode, encodeCaptcha, expend}       ← 还差一个 sign 签名（1602）
```

**但实测：密码登录与短信登录两条路径当前都会弹极验滑块**，
这个图片验证码只会在别的风险等级下出现。所以两个需求合并成同一件事：**过极验 v3**。

---

## 3. 开源方案（已核协议）

| 项目 | 协议 | 星 | 能力 | 采用 |
| --- | --- | --- | --- | --- |
| [sijiyo/projects](https://github.com/sijiyo/projects) | **MIT** ✅ | 144 | `geetest3_slide` + `geetest3_fullpage` 全套 | **采用**（移植） |
| [vsmutok/PuzzleCaptchaSolver](https://github.com/vsmutok/PuzzleCaptchaSolver) | **MIT** ✅ | 102 | 仅缺口定位（OpenCV） | 参考 |
| [xKiian/GeekedTest](https://github.com/xKiian/GeekedTest) | MIT | 684 | **只支持 v4** | 不适用 |
| [DaKMaple/captcha_verify](https://github.com/DaKMaple/captcha_verify) | **无协议** ❌ | 0 | 有 geetest3 | **不能用**（无授权即默认保留全部权利） |

> MIT 与我们的 AGPL-3.0-only 兼容：可移植，但**必须保留版权声明**。
> 移植后的文件头部需注明来源与 MIT 版权（见 §6）。

---

## 4. 算法拆解（来自 MIT 项目的 `geetest3_slide/`）

### 4.1 完整流程

```
1. GET  www.geetest.com/demo/gt/register-slide-official   → gt, challenge
2. GET  api.geetest.com/get.php?is_next=true&type=slide3
        &gt=…&challenge=…&offline=false&…                 → {challenge, s, bg, fullbg, slice}
3. 下载 bg / fullbg / slice 三张图
4. **还原背景图**（v3 把背景切成 52 块打乱了，用置换表拼回）
5. 缺口定位：滑块图 vs 还原后的背景图 → distance
6. 生成轨迹：ease-out-expo + 抖动 → [[x, y, t], …]
7. w = get_w(distance, encode_trace(track, s), challenge, challenge[:32], passtime, rand, gt)
8. GET  api.geetest.com/ajax.php?gt=…&challenge=…&w=…     → geetest_validate / geetest_seccode
```

### 4.2 极验 v3 特有的「背景图还原表」

v3 的背景图是**打乱过的**，必须先还原才能找缺口。置换表（52 项）：

```python
UT = [39, 38, 48, 49, 41, 40, 46, 47, 35, 34, 50, 51, 33, 32, 28, 29, 27, 26,
      36, 37, 31, 30, 44, 45, 43, 42, 12, 13, 23, 22, 14, 15, 21, 20, 8, 9,
      25, 24, 6, 7, 3, 2, 0, 1, 11, 10, 4, 5, 19, 18, 16, 17]
# 每块 10×80；源坐标 x = UT[i] % 26 * 12 + 1，y = 80 if UT[i] > 25 else 0
# 目标坐标 x = (i % 26) * 10，y = 80 if i > 25 else 0
```

**这是 v3 的指纹**，v4 没有这一步。

### 4.3 `w` 的构造（核心难点）

`w` 由两段拼成，**AES + RSA**：

| 段 | 内容 |
| --- | --- |
| AES 部分 | `JSON.stringify({gt, challenge, userresponse, passtime, …})`，用由 `s` 推导的密钥做 AES-CBC |
| RSA 部分 | 上面的 **AES 密钥本身**，用 `get.php` 响应里的 RSA 公钥加密 |
| 拼装 | 两段按极验自定义字符表转成字符串 |

MIT 项目把这一步交给 `jiyan.js`（123 KB 的反混淆极验 JS）+ `execjs` 调 Node。

### 4.4 轨迹生成

`ease-out-expo` 缓动 + 时间抖动 + 起点随机偏移：
`x = round((1 - 2^(-10 * i/count)) * distance)`，`t` 每步 +10~20 ms。

---

## 5. 移植计划

### 依赖影响（重要）

原项目用了 **OpenCV + execjs(Node) + requests**。
本项目 **core 层零第三方依赖**，所以：

| 原依赖 | 我们的做法 |
| --- | --- |
| `requests` | 已有 `net/http.py`，直接用 |
| `execjs` + `jiyan.js` | **把 `get_w` 移植成纯 Python**（不引 JS 运行时） |
| `cv2`（模板匹配） | 用 **Pillow**（已是环境里的可选依赖）或纯 Python 实现缺口定位 |

### 落地位置

```
src/kedaibiao/platforms/unipus/geetest/
    __init__.py
    v3.py          # 取图 / 还原背景 / 缺口 / 轨迹 / w / 提交
    crypto.py      # AES + RSA 的纯 Python 实现
    NOTICE         # MIT 归属声明（sijiyo/projects）
docs/geetest-v3.md # 本文
```

### 分阶段

| 阶段 | 内容 | 验证方式 |
| --- | --- | --- |
| 1 | 逆出 `jiyan.js` 的 `get_w` / `get_encode_trace` | 用真实 `get.php` 响应，能算出 `w` |
| 2 | 缺口定位（还原背景 + 模板匹配） | 用仓库 `img/` 样本，断言偏移量 |
| 3 | 轨迹生成 | 断言长度/单调性/耗时合理 |
| 4 | **匿名验证**：对极验官方 demo 打一遍 | 拿到 `geetest_validate` |
| 5 | 接入 Unipus SSO 登录 | 换到真会话 |

**阶段 1–4 全程用极验官方 demo，不碰用户账号** —— 这样即使失败也不影响账号风险。

---

## 6. 归属声明（MIT 合规）

移植自 `sijiyo/projects` 的部分需保留：

```
Portions of this file are derived from sijiyo/projects
(https://github.com/sijiyo/projects), Copyright (c) 2023 sijiyo,
licensed under the MIT License.
```

---

## 7. 风险与边界

| 风险 | 说明 |
| --- | --- |
| **算法会失效** | 极验持续对抗，`w` 的构造随版本变化 |
| **`offline=false`** | 服务端判定，算错就是失败，没有自校验捷径 |
| **账号风控** | 反复失败会继续抬高账号风险等级（当前已是账号级） |
| **合规** | 仅限用户本人账号；不得用于批量/撞库/薅羊毛 |

**建议**：即使做通了，也应把它作为**兜底**保留 —— 主路径仍是「人工在控制台过一次验证码」（验证码中继），
因为人工过的成功率是 100%，且不依赖与极验的对抗。
