"""iTEST 进考场 → 取卷 → 填答 → 交卷（浏览器全链路，可复用）。

## 为什么最后一步必须落在浏览器里

纯 HTTP 能走到 ``judgeEntry``（换 token），但 ``answer/load`` 与作答页始终 400：
平台要求两个**浏览器端生成/校验**的不透明 cookie ——

- ``envTest<32位大写>``：本设备通过键盘/耳机检测后下发；
- ``iTMExam``（64 字节 hex）：``answer/index`` 拿到的本机作答会话。

再加上「考试冲突 → solveUikConflict」「开始考试」这些交互，浏览器兜底是当前
唯一走通的路线（实测见 ``docs/research/itest.md`` §3.7）。

## 边界与纪律

- **进入考试就开始计时**（服务端记 ``ansBeginTime``），不可撤销；调用方先确认。
- **交卷不可逆**：:meth:`ExamRunner.submit` 只在调用方显式调用时才执行，动作层
  还要求一个确认串，避免误点。
- **不自己拼 ``ansData``**：只操作界面（设值 + 派发事件 + 点单选 + 点交卷），
  让页面自己组装载荷，格式猜错的风险归零。

## 登录态

不需要账号密码：适配器已经用 HTTP 登录过，把 cookie 灌进浏览器即可
（实测 8 条 cookie 就能直接进考试列表）。密码只在「绑定账号」那一刻用过。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kedaibiao.browser.cdp import Browser, CDPError, find_browser
from kedaibiao.platforms.itest.paper import (
    Paper,
    fill_script,
    parse_paper,
    verify_script,
)

log = logging.getLogger(__name__)

#: 考试列表页（点「进入考试」的那一页）
EXAM_PAGE = "https://itestcloud.unipus.cn/utest/itest/s/exam"
#: 答题页关闭/返回时回到的页面，judgeEntry 的 url 必须带 returnUrl，否则报参数缺失
RETURN_URL = "https%3A%2F%2Fitestcloud.unipus.cn%2Futest%2Fitest%2Fs%2Fexam"
#: 答题页（切目标用）
ANSWER_TARGET_HINT = "/answer/"

#: 设备检测 / 冲突弹层里会出现的按钮文字
_NEXT_LABELS = (r"/^(下一步)$/", r"/^(检查正常)$/", r"/^(知道了|我知道了)$/")
#: 冲突弹层 / 提示弹层
_CONFLICT_LABELS = (r"/^(确定|继续|继续作答)$/",)
#: 开始（或继续）作答
_START_LABELS = (r"/^(开始考试|开始答题|继续作答|继续考试)$/",)
#: 交卷按钮
_SUBMIT_LABELS = (r"/^(交卷|提交试卷|提交答卷)$/",)
#: 交卷后的确认
_CONFIRM_LABELS = (r"/^(确定|确认|确认交卷|确定交卷)$/",)
#: 交卷成功的页面证据
_DONE_WORDS = ("交卷成功", "已交卷", "提交成功", "考试已结束", "成绩", "得分")


class ExamFlowError(RuntimeError):
    """进考场/取卷/交卷流程失败（带失败阶段，便于定位）。"""

    def __init__(self, message: str, *, stage: str = "") -> None:
        self.stage = stage
        super().__init__(f"[{stage}] {message}" if stage else message)


@dataclass(slots=True)
class Step:
    """一步操作的结果（回给控制台当日志看）。"""

    name: str
    ok: bool
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


@dataclass(slots=True)
class EnterResult:
    ok: bool
    steps: list[Step] = field(default_factory=list)
    message: str = ""
    location: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok, "message": self.message, "location": self.location,
            "steps": [s.to_dict() for s in self.steps],
        }


def cookies_from_jar(jar: Any) -> list[dict[str, Any]]:
    """把 :mod:`http.cookiejar` 的 cookie 转成 CDP 能注入的形状。

    只取「name/value/domain/path/secure」，不碰其它字段 —— 注入只需要这些。
    """
    out: list[dict[str, Any]] = []
    for c in getattr(jar, "__iter__", lambda: [])():
        try:
            out.append({
                "name": c.name,
                "value": c.value,
                "domain": c.domain or "",
                "path": c.path or "/",
                "secure": bool(getattr(c, "secure", False)),
            })
        except AttributeError:  # pragma: no cover - 非 cookiejar 的鸭子类型
            continue
    return out


class ExamRunner:
    """一场考试的浏览器会话（同步，动作层用线程跑）。

    用法::

        with ExamRunner(cookies={...}) as run:
            run.enter("1099342202")
            paper = run.read_paper()
            run.fill(answers)
            run.verify(answers)
            run.submit()          # 不可逆，调用方必须确认过
    """

    def __init__(
        self,
        cookies: list[dict[str, Any]] | None = None,
        *,
        executable: str | None = None,
        timeout: float = 40.0,
        the_browser: Browser | None = None,
    ) -> None:
        self.cookies = list(cookies or [])
        self.timeout = timeout
        self._executable = executable
        self._browser: Browser | None = the_browser
        self._own_browser = the_browser is None

    # -- 生命周期 ---------------------------------------------------------

    @property
    def browser(self) -> Browser:
        if self._browser is None:
            raise ExamFlowError(
                "浏览器还没启动（先调用 start() 或用作上下文管理器）", stage="browser"
            )
        return self._browser

    def start(self) -> None:
        if self._browser is not None:
            return
        exe = self._executable or find_browser()
        if not exe:
            raise ExamFlowError(
                "找不到 Chrome/Chromium —— 考试流程必须用浏览器（设备检测在那里面）。",
                stage="browser",
            )
        self._browser = Browser(executable=exe, timeout=self.timeout)
        self._browser.start()
        self._browser.enable_network()

    def close(self) -> None:
        if self._browser is not None and self._own_browser:
            try:
                self._browser.close()
            except Exception:  # noqa: BLE001 - 关闭失败不影响结果
                log.debug("关闭浏览器失败", exc_info=True)
        self._browser = None

    def __enter__(self) -> ExamRunner:
        self.start()
        self.login_with_cookies()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- 登录与进考场 -----------------------------------------------------

    def login_with_cookies(self) -> int:
        """把 HTTP 会话的 cookie 灌进浏览器，返回注入成功的条数。"""
        if not self.cookies:
            raise ExamFlowError("没有可注入的会话 cookie —— 先让适配器登录", stage="login")
        injected = 0
        for item in self.cookies:
            if not item.get("name"):
                continue
            params: dict[str, Any] = {
                "name": item["name"], "value": item.get("value", ""),
                "path": item.get("path") or "/",
            }
            domain = item.get("domain") or ""
            if domain:
                params["domain"] = domain
            if item.get("secure"):
                params["secure"] = True
            try:
                if self.browser.call("Network.setCookie", params).get("success", False):
                    injected += 1
            except CDPError:
                continue
        if not injected:
            raise ExamFlowError("cookie 注入全部失败（浏览器可能没起来）", stage="login")
        return injected

    def enter(self, exam_id: str, *, ks_id: str = "", wait: float = 8.0,
              ready_timeout: float | None = None) -> EnterResult:
        """列表页 → 点「进入考试」换令牌 → 设备检测 → 开始作答 → 切到答题页。

        ``ks_id`` 是考试卡片上印的那个 ID（列表 JSON 的 ``ksId``，**不是** ``ksdId``）。
        点卡片走平台自己的入口是最稳的：它会先做 ``examToken`` 再 ``judgeEntry``，
        直接从页面里裸调 judgeEntry 会偶发 ``code=0「考试不存在」``（实测）。

        注意：这一步会让服务端开始计时（或恢复到计时中），是不可逆的。
        """
        steps: list[Step] = []
        self.browser.navigate(EXAM_PAGE)
        plaza_ready = self._wait_plaza_ready(ks_id)
        steps.append(Step(
            "打开考试列表", plaza_ready,
            self._location() + ("" if plaza_ready else "（列表卡片没等到，继续试）"),
        ))

        answer_url = self._entry_url_by_click(ks_id) if ks_id else ""
        if answer_url:
            steps.append(Step("点「进入考试」换令牌", True, answer_url.split("?")[0]))
        else:
            answer_url = self._judge_entry(exam_id)
            steps.append(Step("直接换令牌（兜底）", True, answer_url.split("?")[0]))

        # 点卡片可能已经把作答页开成了新标签页；没有就自己在当前页打开
        if not self.browser.switch_to_target(ANSWER_TARGET_HINT, timeout=4.0):
            self.browser.navigate(answer_url)
            time.sleep(wait)

        # ① 「考试页面被拦截，是否继续」 ② 个人信息核对 ③ 键盘/耳机检测
        blocked = self._click_first((r"/^确定$/",), wait=3.5)
        if blocked:
            steps.append(Step("过拦截提示", True, blocked))
        for _ in range(8):
            if self._section_count() > 0 or "waiting" in self._location().lower():
                break
            clicked = self._click_first(_NEXT_LABELS, wait=3.5)
            if not clicked:
                break
            steps.append(Step("设备检测", True, clicked))

        # ④ 「考试信息」页点开始（续答时是「继续作答」）
        started = self._click_first(_START_LABELS, wait=5.0)
        steps.append(Step("开始/继续作答", bool(started), started or "页面上没有开始按钮"))

        # ⑤ 开始后可能弹「考试冲突」确认，也可能把作答页开在新标签页
        conflict = self._click_first(_CONFLICT_LABELS, wait=3.0)
        if conflict:
            steps.append(Step("处理考试冲突", True, conflict))
        switched = self.browser.switch_to_target(
            ANSWER_TARGET_HINT, timeout=max(25.0, self.timeout), newest=True
        )
        steps.append(Step("切到答题页", switched, self._location() if switched else "还在原页"))
        if not switched and self._section_count() == 0:
            return EnterResult(False, steps, "没切到答题页 —— 设备检测或开始考试那步没走通",
                               self._location())

        self.browser.enable_network()
        ready = self.wait_ready(timeout=ready_timeout or max(60.0, self.timeout * 2))
        steps.append(Step("等试卷渲染", ready, f"大题 {self._section_count()}"))
        return EnterResult(ready, steps,
                           "" if ready else "答题页没有渲染出 .itest-section（可能还在设备检测）",
                           self._location())

    def wait_ready(self, *, timeout: float = 90.0) -> bool:
        """等作答页把试卷渲染出来（.itest-section 出现）。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._section_count() > 0:
                return True
            if not self._click_first(_CONFLICT_LABELS + _NEXT_LABELS, wait=3.0):
                time.sleep(3.0)
        return False

    # -- 取卷 / 填答 ------------------------------------------------------

    def html(self) -> str:
        return str(self.browser.evaluate("document.documentElement.outerHTML") or "")

    def section_count(self) -> int:
        return self._section_count()

    def read_paper(self) -> Paper:
        html = self.html()
        paper = parse_paper(html)
        if not paper.sections:
            raise ExamFlowError(
                "DOM 里没有 itest-section —— 试卷没渲染完或页面结构变了", stage="paper"
            )
        return paper

    def read_answers(self) -> dict[str, str]:
        """读回页面上**当前**填了什么（qid → 字母串/整句）。"""
        raw = self.browser.evaluate(_READ_ANSWERS_JS)
        try:
            data = json.loads(str(raw or "{}"))
        except json.JSONDecodeError:
            return {}
        return {str(k): str(v) for k, v in data.items() if str(v).strip()}

    def fill(self, answers: dict[str, str]) -> dict[str, Any]:
        """把答案填进界面（不提交）。返回 ``{blank, radio, textarea, missing}``。"""
        if not answers:
            raise ExamFlowError("答案表是空的", stage="fill")
        raw = self.browser.evaluate(fill_script(answers))
        return _json_dict(raw)

    def verify(self, answers: dict[str, str]) -> dict[str, Any]:
        """填完必须读回逐项核验：``{bad: [...], total: N}``，total 必须为 0。"""
        raw = self.browser.evaluate(verify_script(answers))
        return _json_dict(raw)

    def screenshot(self) -> bytes:
        return self.browser.screenshot()

    def save_screenshot(self, path: str | Path) -> str:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(self.screenshot())
        return str(target)

    # -- 交卷（不可逆）----------------------------------------------------

    def submit(self, *, timeout: float = 90.0) -> dict[str, Any]:
        """点「交卷」并确认。**不可逆** —— 调用方必须先拿到人工确认。

        返回 ``{ok, steps, text, dialogs}``；``ok`` 只表示页面上出现了成功证据，
        最终仍应以考试列表里的 ``finish`` 为准（动作层会再查一次）。
        """
        self.browser.dialogs_seen.clear()
        steps: list[Step] = []
        # 作答页的交卷是左侧固定的 <a class="tijiao">，按 class 点最稳（文字匹配会撞上
        # 弹层里的「确认交卷」）
        clicked = self._click_submit_button() or self._click_first(_SUBMIT_LABELS, wait=3.0)
        if not clicked:
            raise ExamFlowError("页面上没找到「交卷」按钮", stage="submit")
        steps.append(Step("点交卷", True, clicked))

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            hit = self._click_first(_CONFIRM_LABELS, wait=2.5)
            if hit:
                steps.append(Step("确认交卷", True, hit))
            text = self._body_text(240)
            done = any(word in text for word in _DONE_WORDS)
            # 交卷后左侧的「交卷」按钮会消失 —— 这也是「已经交上」的证据，
            # 不用等满超时（实测交完页面不一定出现「交卷成功」字样）
            if not done and self.browser.evaluate(_FIND_TIJIAO_JS) is None:
                done = True
            if done:
                return {
                    "ok": True, "steps": [s.to_dict() for s in steps],
                    "text": text, "dialogs": list(self.browser.dialogs_seen),
                }
            time.sleep(2.0)
        return {
            "ok": False, "steps": [s.to_dict() for s in steps],
            "text": self._body_text(200), "dialogs": list(self.browser.dialogs_seen),
        }

    # -- 内部 -------------------------------------------------------------

    def _judge_entry(self, exam_id: str, *, attempts: int = 3) -> str:
        """换答题令牌。

        实测**会偶发** ``code=0 考试不存在或已被删除``：刚登录完的第一下容易撞上，
        刷新一下考试列表页再试就正常（同一个会话，同一个 examId）。所以这里带重试，
        而不是把一次抖动当成「考试没了」。
        """
        last = ""
        for attempt in range(attempts):
            out = self.browser.evaluate(_JUDGE_ENTRY_JS % json.dumps(exam_id))
            text = str(out or "")
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                last = f"judgeEntry 返回的不是 JSON: {text[:100]}"
            else:
                url = str(((payload.get("data") or {}).get("url")) or "")
                if payload.get("code") in (0, 1) and url:
                    return url if "returnUrl=" in url else f"{url}&returnUrl={RETURN_URL}"
                last = (
                    f"code={payload.get('code')} "
                    f"msg={payload.get('msg') or payload.get('message') or '（无消息）'}"
                )
            if attempt + 1 < attempts:
                log.debug("judgeEntry 第 %d 次失败（%s），刷新列表页重试", attempt + 1, last)
                time.sleep(2.0)
                self.browser.navigate(EXAM_PAGE)
                self._wait_plaza_ready()
        raise ExamFlowError(f"judgeEntry 失败: {last}", stage="entry")

    def _wait_plaza_ready(self, ks_id: str = "", *, timeout: float = 25.0) -> bool:
        """等考试列表把卡片渲染出来。

        只等固定的秒数不可靠：网络慢的时候卡片还没画出来就去点「进入考试」会找不到。
        这里等**卡片文字**出现（带 ID 就等那张卡），最多 ``timeout`` 秒。
        """
        marker = f"ID：{ks_id}" if ks_id else "进入考试"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            text = self._body_text(4000)
            if marker in text and "进入考试" in text:
                return True
            time.sleep(0.7)
        return False

    def _entry_url_by_click(self, ks_id: str) -> str:
        """点考试卡片上的「进入考试」，截获平台自己发的 judgeEntry，返回答题 URL。

        为什么要绕这一下：平台点按钮时会先调 ``examToken`` 再把考试上下文准备好，
        裸调 judgeEntry 会偶发「考试不存在」。让平台自己发这个请求最稳。
        """
        try:
            captured = self.browser.capture_response(
                "judgeEntry", trigger_script=_ENTER_CARD_JS % json.dumps(ks_id),
                timeout=20.0, settle=5.0,
            )
        except CDPError:
            return ""
        body = str(captured.get("body") or "")
        if not body:
            return ""
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return ""
        url = str(((payload.get("data") or {}).get("url")) or "")
        if payload.get("code") not in (0, 1) or not url:
            return ""
        return url if "returnUrl=" in url else f"{url}&returnUrl={RETURN_URL}"

    def _location(self) -> str:
        try:
            return str(self.browser.evaluate("location.href") or "")
        except CDPError:
            return ""

    def _body_text(self, limit: int = 160) -> str:
        try:
            raw = self.browser.evaluate(
                f"(document.body.innerText||'').replace(/\\s+/g,' ').slice(0,{int(limit)})"
            )
        except CDPError:
            return ""
        return str(raw or "")

    def _section_count(self) -> int:
        try:
            raw = self.browser.evaluate("document.querySelectorAll('.itest-section').length")
            return int(raw or 0)
        except (CDPError, TypeError, ValueError):
            return 0

    def _click_submit_button(self) -> str | None:
        """点作答页左侧的「交卷」（``a.tijiao``）。找不到返回 None。"""
        probe = self.browser.evaluate(_FIND_TIJIAO_JS)
        if not probe:
            return None
        try:
            box = json.loads(str(probe))
        except json.JSONDecodeError:
            return None
        x, y = float(box.get("x", 0)), float(box.get("y", 0))
        try:
            self.browser.mouse("pressed", x, y)
            time.sleep(0.12)
            self.browser.mouse("released", x, y)
        except CDPError:
            log.debug("鼠标点击交卷失败，退回 JS 点击")
        time.sleep(0.5)
        self.browser.evaluate(_CLICK_TIJIAO_JS)
        time.sleep(1.5)
        return str(box.get("t") or "交卷")

    def _click_first(self, patterns: tuple[str, ...], *, wait: float = 5.0) -> str | None:
        for pattern in patterns:
            hit = self._click(pattern, wait=wait)
            if hit:
                return hit
        return None

    def _click(self, pattern: str, *, wait: float = 5.0) -> str | None:
        """按「可见叶子节点的文字」匹配并点击；返回点中的文字。

        先派发真实鼠标事件（走 click 的流程与坐标校验），再补一次元素 ``click()``
        兜住被遮挡的情况 —— 平台把按钮做在多层 div 里，纯 JS 点击有时不触发。
        """
        probe = self.browser.evaluate(_FIND_JS % pattern)
        if not probe:
            return None
        try:
            box = json.loads(str(probe))
        except json.JSONDecodeError:
            return None
        x, y = float(box.get("x", 0)), float(box.get("y", 0))
        try:
            self.browser.mouse("pressed", x, y)
            time.sleep(0.12)
            self.browser.mouse("released", x, y)
        except CDPError:
            log.debug("鼠标点击失败，退回 JS 点击")
        time.sleep(0.4)
        self.browser.evaluate(_CLICK_JS % pattern)
        time.sleep(wait)
        return str(box.get("t") or "")


def _json_dict(raw: Any) -> dict[str, Any]:
    try:
        data = json.loads(str(raw or "{}"))
    except json.JSONDecodeError:
        raise ExamFlowError(
            f"页面返回的填表结果不是 JSON: {str(raw)[:120]}", stage="fill"
        ) from None
    return data if isinstance(data, dict) else {"value": data}


# --------------------------------------------------------------------------
# 页面里的 JS（找不到元素返回 null，不抛错）
# --------------------------------------------------------------------------

_JUDGE_ENTRY_JS = """(async () => {
  const r = await fetch('/utest/itest/s/exam/judgeEntry', {
    method: 'POST', credentials: 'include',
    headers: {'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8'},
    body: 'examId=%s&examCode='
  });
  return await r.text();
})()"""

#: 点考试卡片里的「进入考试」：卡片的 ID 印的是 ksId（不是 ksdId）
_ENTER_CARD_JS = r"""(() => {
  const ks = %s;
  const cards = [...document.querySelectorAll('*')].filter(
    (e) => (e.innerText || '').includes('ID：' + ks) && (e.innerText || '').includes('进入考试')
  );
  const card = cards[cards.length - 1];
  if (!card) return 'no-card';
  const btn = [...card.querySelectorAll('a,button,div,span')].find(
    (e) => (e.innerText || '').trim() === '进入考试'
  );
  if (!btn) return 'no-btn';
  btn.click();
  return 'clicked';
})()"""

#: 作答页左侧的「交卷」按钮（class="tijiao"）
_FIND_TIJIAO_JS = r"""(() => {
  const el = document.querySelector('a.tijiao');
  if (!el || !el.offsetParent) return null;
  const r = el.getBoundingClientRect();
  return JSON.stringify({x: r.x + r.width / 2, y: r.y + r.height / 2,
                         t: (el.innerText || '交卷').trim().slice(0, 16)});
})()"""

_CLICK_TIJIAO_JS = r"""(() => {
  const el = document.querySelector('a.tijiao');
  if (!el) return false;
  el.click();
  return true;
})()"""

_FIND_JS = r"""(() => {
  const re = %s;
  const el = [...document.querySelectorAll('*')].filter(
    x => x.offsetParent && x.children.length === 0 && re.test((x.textContent || '').trim())
  )[0];
  if (!el) return null;
  const r = el.getBoundingClientRect();
  if (!r.width && !r.height) return null;
  return JSON.stringify({x: r.x + r.width / 2, y: r.y + r.height / 2,
                         t: (el.textContent || '').trim().slice(0, 16)});
})()"""

_CLICK_JS = r"""(() => {
  const re = %s;
  const el = [...document.querySelectorAll('*')].filter(
    x => x.offsetParent && x.children.length === 0 && re.test((x.textContent || '').trim())
  )[0];
  if (!el) return false;
  (el.closest('button,a,input,div[role=button],[onclick]') || el).click();
  return true;
})()"""

#: 读回页面当前答案：填空按 qsubindex 排序拼字母串；单选按 label 字母；翻译取原文
_READ_ANSWERS_JS = r"""(() => {
  const out = {};
  const collect = (sel, pick) => {
    const groups = {};
    document.querySelectorAll(sel).forEach((el) => {
      const qid = el.getAttribute('qid');
      if (!qid) return;
      const sub = parseInt(el.getAttribute('qsubindex') || '1', 10);
      (groups[qid] ||= {})[sub] = pick(el);
    });
    for (const [qid, subs] of Object.entries(groups)) {
      out[qid] = Object.keys(subs).sort((a, b) => a - b).map((k) => subs[k]).join(' ');
    }
  };
  collect('input.blankinput', (el) => (el.value || '').trim());
  collect('textarea', (el) => (el.value || '').trim());
  collect('input[type=radio]', (el) => {
    if (!el.checked) return '';
    const label = el.closest('label');
    const text = (label ? label.textContent : '').trim();
    const m = text.match(/^[\s\u00a0]*([A-Z])[\s.、]/);
    return m ? m[1] : (el.value || '');
  });
  const clean = {};
  for (const [qid, value] of Object.entries(out)) {
    if (String(value).trim()) clean[qid] = String(value).trim();
  }
  return JSON.stringify(clean);
})()"""
