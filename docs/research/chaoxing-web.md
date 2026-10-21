# 学习通 / 超星：浏览器页面级自动化（刷课 + 刷题）

> 实测日期：2026-10-21｜账号：学生测试账号（手机号登录）｜设备：Linux headless Chromium
> 结论先行：**页面驱动可行**。视频播完 `isPassed` 会变 true；章节测验题面有字体混淆，
> 已实现字形比对解码（34 个伪字全中）；填卷 + 逐题回读核验 5/5 通过，提交按钮可定位，
> 但交卷默认干跑，需显式确认。

## 0. 一句话架构

```
登录（password → storage_state）
  → 课程列表（mooc2-ans /visit/courselistdata）
  → 章节树（studentcourse HTML，tree.py 解析）
  → 每个小节：knowledge/cards?num=0（课件）/ num=1（测验）
       ├─ mArg.attachments[]：每个附件 = 一个任务点（jobid / 时长 / isPassed）
       ├─ 视频：ananas/modules/video iframe，play() + 2x + muted，让播放器自己上报
       └─ 文档：pdf / pan-yz 阅读器 iframe，停留 + 翻页（按阅读时长记账）
  → 回读：mArg.isPassed（附件级）+ studentcourse 章节树 icon_yiwanc（小节级）
  → 测验/作业：解析题面（fontdec 解字体混淆）→ LLM/规则作答 → 填卷 → verify → 提交闸门
```

## 1. 登录

- 登录页：`https://passport2.chaoxing.com/login?fid=&newversion=true&refer=https%3A%2F%2Fi.chaoxing.com`
- 表单：`#phone`（手机号/超星号）、`#pwd`、`#loginBtn`
- 本次实测**没有验证码**，直接跳到 `i.chaoxing.com/base`；失败文案在 `#err-txt`/`#pwdMsg`
- 会话落盘：Playwright `storage_state` → `data/sessions/chaoxing.storage_state.json`
- 纪律：弹验证码/滑块不代过，报 `needs_human`，人工过完再复用会话

## 2. 课程与章节

| 东西 | 入口 | 备注 |
|---|---|---|
| 课程列表 | `https://mooc2-ans.chaoxing.com/mooc2-ans/visit/courselistdata?courseType=1&courseFolderId=0&courseFolderSize=0&superstarClass=0` | 服务端渲染 HTML；`parsing.parse_courses` 可复用 |
| 课程页 | `https://mooc1.chaoxing.com/visit/stucoursemiddle?courseid=..&clazzid=..&cpi=..&ismooc2=1&v=2` | 302 到 `mooc2-ans/mycourse/stu?...&enc=<hex>`；**enc 是后续请求的钥匙** |
| 章节树 | `https://mooc2-ans.chaoxing.com/mooc2-ans/mycourse/studentcourse?courseid=..&clazzid=..&cpi=..&ut=s&t=<ms>&stuenc=<enc>` | 服务端渲染；`div.chapter_item[id=cur<chapterId>]`；完成状态 `icon_yiwanc` |
| 小节课件 | `https://mooc1.chaoxing.com/mooc-ans/knowledge/cards?clazzid=..&courseid=..&knowledgeid=<chapter>&num=<0|1>...` | `num=0` 课件，`num=1` 测验（tab 序号 - 1） |

要点：

- **目录页没有 tab**。课件/测验/扩展资源是点开小节后在 `studentstudy` 学习页里渲染的
  `li[cardid]`；自动化不需要去点，直接按 `num` 请求 `cards` 页更稳（`tree.parse_tabs`
  仍保留给需要 dom 的路由）。
- 请求 `cards` 页要带 `Referer: https://mooc2-ans.chaoxing.com/...`，否则返回空壳。
- 页面里 `mArg = ""` 先出现一次空串，再出现真正的 JSON；解析要跳过空串（`study._marg`）。

## 3. 任务点与完成判定

`mArg.attachments[]` 每个元素是一个任务点（同一小节可以有 4 个视频）：

```json
{"jobid": "1757647259823620", "attDuration": 144, "isPassed": false,
 "property": {"module": "insertvideo", "name": "万有引力做功推导-2.mp4"},
 "randomFaceCaptureTimeList": [363]}
```

- `jobid` 同时出现在页面上每个播放器 iframe 的 `data` 属性里 —— 用它定位「该播哪个」；
- `isPassed` 是附件级完成标记。**实测：把 146 秒视频完整播完 → `isPassed` 变 true**；
- 章节树状态是**小节级**：小节的多个附件全部完成才会 `icon_yiwanc`。
- 因此判定链：先看 `isPassed`，再回读章节树；两者都不认「播放器说播完」。

## 4. 视频播放（实测细节）

- iframe：`https://mooc1.chaoxing.com/ananas/modules/video/index.html`，里面是 video.js；
- **懒初始化**：iframe 不在可视区时不建 `<video>`、`duration` 一直是 `NaN`。
  必须 `scrollIntoView` + 等 `duration > 0`；
- **倍速白名单最高 2x**：直接 `playbackRate = 8` 会被播放器打回 1，且暂停；
- 心跳：`/mooc-ans/multimedia/log/a/100000002/<hash>?...&playingTime=<位置>&duration=<总长>&isdrag=..`
  返回 `{"isPassed":true,...}`；`playingTime` 与**播放位置**同步（2x 时按 2 倍速度推进）；
- 声音：`muted = true` 即可，headless 没有音频设备也不报错；
- **人脸采集**：`randomFaceCaptureTimeList` 非空（实测 422 秒视频在 363 秒、146 秒视频在
  123 秒）表示到点要采集人脸；不代过，检测到就交给人工（脚本会报 `needs_human`）。

## 5. 文档任务点

- iframe：`ananas/modules/pdf/index.html` + `pan-yz.chaoxing.com/screen/...`（分页图片）；
- 按**阅读时长**记账，没有「播放完」这种明确终态；自动化策略：保持页面打开、
  逐页翻动（「下一页/下一张」文本、`[class*=next-page]`、滚动兜底）。

## 6. 测验 / 作业 / 考试

### 6.1 入口

- 考试列表：课程页「考试」tab → `mooc1.chaoxing.com/exam-ans/mooc2/exam/exam-list?...`
  - 条目 `div.bottomList li`，`onclick="goTest('<courseid>',<examId>,...)"` 或
    `viewExamAnswer('<examId>','<answerId>')`；状态在 `p.status`（待做/已完成/已过期）；
  - 「只允许手机APP考试」是纯文本标记 → 网页端做不了，自动跳过；
- 点条目会弹**考试须知**：`exam-ans/exam/test/examcode/examnotes?courseId=..&classId=..&examId=..&cpi=..`，
  勾 `.face_agreement` 后点 `#startBtn` 进答题页；
- 章节测验：`cards?num=1` → iframe `doHomeWorkNew?courseId=..&workId=..&api=1...`。

### 6.2 题面字体混淆（font-cxsecret）

答题页内联一份子集字体，把一批常用字替换成伪码位（`厙厠厦厩…`）：

```html
<style>@font-face{font-family:'font-cxsecret';
  src:url('data:application/font-ttf;charset=utf-8;base64,...')}</style>
```

字形轮廓是**真字**（思源黑体），`cmap` 把伪码位映射到字形。`innerText` 读到的是伪字，
人眼看却是正常中文。

解决办法（`platforms/chaoxing/fontdec.py`，无需 OCR、不联网）：

1. 从 HTML 抠出 base64 TTF；
2. fontTools 读 cmap，Pillow 渲染每个字形；
3. 与参考字体（Noto Sans CJK SC）的 GB2312 常用字（6763）逐字比对：
   按墨迹包围盒裁切 → 40×40 网格 → 平均像素差取最近；
4. 距离 ≤ 0.12 才认，否则保留原字符（宁可漏字不可错字）。

实测：34 个伪字全部命中，最佳距离 0.02~0.07，次优通常 > 0.2。

题面里还有**公式截图**（`<img src="p.ananas.chaoxing.com/...">`）。文本模型看不到图，
所以带图的题目在报告里标低置信度，不作「已答对」的承诺。

### 6.2.1 公式截图题 → 视觉模型

题干/选项里的公式是 png 截图，纯文本模型答不了。实测当前 LLM 网关支持
OpenAI 多模态格式（content 数组里的 `image_url` data URL），但**必须带浏览器
User-Agent**，否则被 Cloudflare 挡成 403（error 1010）。

做法（`tasks/llm.py` 的 `VisionAnswerer`）：

- 从 `Question.raw` 取 `images` / `option_images`，逐张下载转 base64（单图 ≤1.5MB、≤4 张）；
- 文本里标注「图1/图2…」与题干/选项的对应关系，要求模型返回
  `{"choices":["A"],"confidence":0.8,"reason":"..."}`；
- 视觉模型可用 `KEDAIBIAO_LLM_VISION_MODEL` 单独指定，UA 可用
  `KEDAIBIAO_LLM_USER_AGENT` 覆盖；
- 下载/请求/解析失败一律降级为空答案，不阻塞其它题。

### 6.3 答题页结构（已核实）

- 题块：`div.singleQuesId[id="question<qid>"] > div.TiMu`；
- 题面：`.Zy_TItle .font-cxsecret.fontLabel`，含 `<span class="newZy_TItle">【单选题】</span>`；
- 选项：`ul.Zy_ulTop li.before-after[qid][qtype][onclick=addChoice(this)]`，
  字母在 `span.num_option[data="A"]`，正文在 `a.fl.after`；
- 提交：`a.bluebtn[onclick=submitCheckTimes()]`，二次确认层 `#submitBack`。

### 6.4 考试 / 作业（网页端）

> 实测（2026-10-21 20:14）：大学物理「课后测试4」全自动完成 ——
> 两次滑块自动通过 → 5 道单选由 LLM 作答（置信度 0.77~0.85）→ 提交表单
> → 平台回显「100 分」，列表回读得分 100，全程 100 秒。

- 列表：课程页「考试」tab → `exam-ans/mooc2/exam/exam-list`；条目 onclick
  `goTest('<courseid>',<考试id>,<作答记录id>,<截止时间>,...)`；
  **实测 `examnotes?examId=` 要填 `goTest` 的第 2 个参数**（第 3 个参数是作答记录 id）；
- 「只允许手机 APP 考试」只能跳过；
- 进入考试：考试须知 `.face_agreement` 勾选 → `#startBtn`；
- **「人脸识别」是误报**：考试须知模板里**永远**带着人脸比对的 DOM
  （`face_Box`、「人脸比对中…」），只是隐藏着。真正的开关是隐藏域
  `#needFaceRecognition` / `#webNeedFaceRecognition`（本场实测都是 `0`），
  所以判定改成「开关为 1，或人脸弹层真的可见」才算要求认证；
- **真正的门是滑块安全验证**：点「进入考试 → 确认」后超星风控可能弹
  `captcha.chaoxing.com` 的图片滑块（`#captcha` / `#eject`，拖动 `.cx_rightBtn`
  填充拼图）。这是风控触发（反复进入同一场考试更容易命中），不是每场必有。
  **已实现自动求解**（`platforms/chaoxing/captcha.py`）：
  拼图 alpha 轮廓与背景梯度做相关 → 求缺口 x → 用 Playwright 鼠标逐帧缓动拖拽；
  实测连续两次自动通过并进入答题页（2026-10-21）。解得开就全自动，
  解不开才退回人工（可见窗口里拖完点「继续」）；
- 答题页是**一题一屏**（`.questionLi`，题号/题型/选项/填空都在里面）：
  读当前题 → 作答 → 点「下一题/下一步」，循环到没有下一题；
- 提交：直接 `document.getElementById('submitTest').submit()` 走页面自己的表单，
  返回 `{"msg":"提交成功","status":"success","consumeMinutes":9}`，比点按钮稳；
- 填空题：题干里 `input[type=text]`/`textarea` 设值并派发 input/change/blur；
- **交卷后分数**：结果页回显「本次成绩 85.0 分 / 最终成绩 85.0 分」，用
  `parse_score_text` 抠出来展示；平台关闭成绩显示时如实说「没有回显分数」，
  不编数字；
- 交卷保险（`submit_block_reason`）：
  - 一题都没答上 → 拒绝交卷（不交白卷）；
  - **题数 ≤ 3** → 停住不交，界面变成「待人工确认」，按钮切「强制交卷」；
  - **有题置信度 < 0.6** → 同上，并列出具体题号；
  - 显式「强制交卷」（`force=true`）才越过保险。

## 6.5 接入主控制台（2026-10-21）

- 适配器新增 `ChaoxingAutomation` 门面（任务点 / 刷课 / 考试列表 / 做考试 / 章节测验），
  控制台动作 `tasks`、`study`、`exams`、`exam`、`quiz` 全部走它；
- 登录：控制台「账号」页填**手机号 + 密码**即可（无头浏览器走登录页 → 存
  `data/sessions/chaoxing.storage_state.json`），不再强制贴 Cookie；
- 作业/测验**就地作答**：作业页每行「做考试 / 做测验」→ 后台任务
  （`POST /api/platforms/chaoxing/jobs` + `/api/jobs/{id}` 轮询）→ 面板里实时显示
  题目信息、日志、分数；题目信息一题一推，不用等整份跑完；
- 章节测验也会并进作业列表（`remote_id = quiz:<chapterId>`），点「做测验」即可；
- 提交次数与时间窗口：
  - 章节测验/作业页隐藏域 `#limitWorkSubmitTimes`（上限，100 = 不限）与
    `#addTimes`（已提交次数）→ 剩余次数；
  - 考试须知表格给出 `考试时间 10-12 14:07 至 10-25 22:00`、`考试时长`；
    答题页 `.mark_info` 给绝对时间（含年份）、题量、满分；
  - 面板里展示，列表在填了课程号加载时自动补齐「开始 / 剩余次数」两列；
- **可重考**：已完成的行里如果还有 `重考` 按钮（嵌套的
  `goTest('<courseid>',<examId>,<recordId>,<重考截止>,...)`），说明可以再交一次；
  解析成 `ExamItem.retake/retake_exam_id/retake_deadline_text`，控制台显示
  「可重考」并给「重考」按钮；平台没给出具体剩余次数时只标「可重考」；
- **重考要按平台自己的入口点**：直接拼 `examnotes?examId=` 只会落到「查看答案」页
  （0 题）。现在改成在课程页「考试」tab 里找到这一行的 `goTest(...)` 并执行，
  打开的是带 `&reset=true` 的须知页（按钮文案「开始重考」），
  勾同意 → 开始重考 → 滑块 → 「进入考试」确认 → 答题页；
- **风控是硬门**：同一场连续进多次会一直弹滑块；自动解开了前端遮罩但平台仍
  判不通过（换新题）。现在的策略是最多自动解 2 次，仍不过就停下并提示人工
  （`--show`），不再反复刷验证码；
- **踩到的坑**：学习通前置 tengine 对非浏览器 UA 直接 403（httpx/urllib 默认 UA
  一律 403，带 Chrome UA 就 200）。只读请求的 `_HTML_HEADERS` 必须带 UA，
  否则控制台会一路报「会话已失效」。

## 6.6 题面走 OCR（2026-10-21 定为默认方案）

学习通的题面有三个叠在一起的问题：子集字体把常用字换成伪汉字、公式是图片、
不同页面（章节测验 / 考试）的 DOM 还不一样。与其逐层破解，**干脆截图交给视觉模型**：

1. 章节测验：每道题按 `#question<qid>` 截一张图；
2. 考试：当前一屏就是一道题，截 `.questionLi`；
3. 截图转 data URL（`data:image/png;base64,...`）塞进 `Question.raw["images"]`，
   由 `VisionAnswerer` 直接读图；选项字母仍取自 DOM（点击要按字母点）；
4. 截图失败才退回原来的「字体解码 + 图片 URL」路径，不会因为一次截图失败整题放弃。

默认开启，`KEDAIBIAO_CHAXING_OCR=0` 可关。实测（概率论 1.1 章节测验，5 题）：
干跑填卷 5/5、核验 5/5、耗时 90 秒（每题约 18 秒，含截图 + 视觉请求）。
截图出来的题面是**正常中文**（浏览器按子集字体渲染），确实没有伪汉字，
模型能读到 `设A,B,C表示三个事件，下列表述不正确的是` 这种完整题干。

## 7. 实测记录（2026-10-21）

| 项目 | 结果 |
|---|---|
| 账号密码登录 | 成功，无验证码 |
| 课程枚举 | 6 门 |
| 任务点枚举（大学物理） | 47 小节 / 24 个附件（含 4 个一节的 11.1） |
| 视频 146 秒完整播放 | 2x 用 73 秒，`isPassed: false → true` |
| 视频 422 秒完整播放 | 播完但 `isPassed` 仍 false（该视频有 363 秒人脸采集点）→ 判为 partial |
| 章节测验（概率论 1.1） | 5 题、单选题、字体解码成功；LLM 作答 5 题、填卷 5 题、回读核验 5/5；提交按钮定位成功 |
| **真交卷 + 分数**（1.4 独立性，2026-10-21 19:26） | 5 题全对，交卷后页面回显「本次成绩100分」，`parse_score_text` 解析为 **100.0** 并在界面/日志显示 |
| OCR 题面 | 章节测验 5 题截图识别 → 作答 5/5、核验 5/5；截图题面为正常中文 |
| OCR 考试 | 大学物理「热学测试」10 题全自动：截图识别 → 逐题作答 → 交卷 → **100 分**（约 2 分钟） |
| 控制台接入 | 平台页 6 门课、任务点动作 7 秒返回 35 条、考试动作 3 秒返回 8 场（含 100/95 分回读） |
| **考试全自动**（大学物理 课后测试4，20:14） | 滑块自动过 ×2 → 5 题作答 → 表单提交 → 列表回读 **100 分**，耗时 100 秒 |
| 滑块自动求解 | 真实验证码连续 2 次通过；单元测试用真实抓图固定断言缺口 x=271 |
| 公式截图题 | 视觉链路可用：题干公式图直接进多模态请求，返回「C / 0.59」并给出图像推理；修复了模型在 reason 里写 LaTeX `\(` 导致的「非法 JSON 转义」解析失败 |
| 考试流程 | 考试列表/须知/考试 id/确认弹层/一题一屏作答/表单交卷/分数回读全部打通；真实考试 100 分 |
| 人脸判定 | 之前把须知页模板里的隐藏人脸 DOM 当成了要求认证（误报），已改成「隐藏域开关 + 弹层可见性」判定；本账号本场考试实测**不需要人脸** |
| 提交闸门 | 默认干跑；`--confirm` / 界面「填卷并交卷」才点；一题都没填上时拒绝交卷 |

## 8. 风险与边界

- **交卷不可逆**：默认干跑，二次确认；
- **人脸采集/防作弊**：不代过，交回人工；
- **账号风控**：同账号并发会互相顶掉播放器，任务串行；2x 是平台允许的上限，
  更高倍速会被打回，不建议硬改；
- **答案质量**：LLM 不可用时降级规则答题器（置信度 0.35），报告里如实标注；
  带公式截图的题模型看不到图，置信度必须打折；
- **验证码**：考试入口的滑块已实现自动求解；解不开的其它形态（点选/短信）
  仍交人工。人脸若真被要求（隐藏域开关=1）也交人工，`--show` 在可见窗口处理；
- **考试**：网页端可做的考试/作业支持答题与交卷（含分数回读）；
  「只允许手机 APP」的条目直接跳过。
