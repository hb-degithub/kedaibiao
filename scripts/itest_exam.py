#!/usr/bin/env python3
"""iTEST 智能测评云平台 —— 考试窗口取卷探针（Stage 1：只读取卷，绝不交卷）。

链路（已核验，见 docs/research/itest.md §1.3 / §3.7）：

    Unipus SSO 登录（AES 加密的 cip/login）
      → 考试列表 /utest/itest-mobile-api/student/exam/list
      → judgeEntry 换 token（拿到答题窗口 URL，本身不开始考试）
      → 浏览器打开窗口 → 个人信息确认 → 键盘/耳机检测 → 开始考试
      → 拦截 /itest-api/itest/s/answer/load 的响应（题号 + 题干 + 选项 + 听力资源）
      → 解析成结构化 JSON

纪律：

  * 取到卷就关浏览器：**不点「加载成功 → 确定」之后的任何按钮**，
    不调用 submit / screen/switch / upload / rtc；
  * 不打印姓名、学号、openId、token、cookie 值；
  * 产物只落 data/recon/itest/（已被 .gitignore 忽略，不入库）。

依赖：playwright（`pip install playwright`，浏览器复用 ~/.cache/ms-playwright）。
仓库里的 .venv 已经装好；也可以 `python3 -m venv .venv && .venv/bin/pip install playwright`。
凭据：`KEDAIBIAO_ITEST_USER` / `KEDAIBIAO_ITEST_PASS`（环境变量或 .env，二者取其一）。

用法：

    python3 scripts/itest_exam.py list            # 登录并列考试（只读）
    python3 scripts/itest_exam.py paper <ksdId>   # 打开考试窗口取卷并解析
"""

from __future__ import annotations

import html as html_mod
import http.cookiejar
import json
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from kedaibiao.config.settings import resolve_environ  # noqa: E402
from kedaibiao.platforms.unipus.crypto import encrypt_login_field  # noqa: E402
from kedaibiao.security.credentials import load_credentials  # noqa: E402

OUT = ROOT / "data" / "recon" / "itest"

BASE = "https://itestcloud.unipus.cn"
SSO = "https://sso.unipus.cn"
LOGIN = f"{BASE}/utest/itest/login"
EXAM_PAGE = f"{BASE}/utest/itest/s/exam"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


# --------------------------------------------------------------------------
# HTTP：登录 / 列表 / 换 token
# --------------------------------------------------------------------------


class Session:
    """带 cookie jar 的极简 urllib 会话（仓库核心不引入第三方依赖）。"""

    def __init__(self) -> None:
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.opener.addheaders = [
            ("User-Agent", UA),
            ("Accept-Language", "zh-CN,zh;q=0.9"),
            ("Accept", "application/json, text/plain, */*"),
        ]

    def _open(self, req: urllib.request.Request, timeout: int = 30) -> tuple[int, bytes, str]:
        with self.opener.open(req, timeout=timeout) as resp:
            return resp.status, resp.read(), resp.geturl()

    def get(self, url: str, headers: dict[str, str] | None = None) -> tuple[int, bytes, str]:
        return self._open(urllib.request.Request(url, headers=headers or {}))

    def post_form(
        self, url: str, fields: dict[str, str], headers: dict[str, str] | None = None
    ) -> tuple[int, bytes, str]:
        hdrs = {"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"}
        hdrs.update(headers or {})
        body = urllib.parse.urlencode(fields).encode()
        return self._open(urllib.request.Request(url, data=body, headers=hdrs, method="POST"))

    def post_json(
        self, url: str, obj: dict, headers: dict[str, str] | None = None
    ) -> tuple[int, bytes, str]:
        hdrs = {"Content-Type": "application/json; charset=UTF-8"}
        hdrs.update(headers or {})
        body = json.dumps(obj).encode()
        return self._open(urllib.request.Request(url, data=body, headers=hdrs, method="POST"))


def login() -> Session:
    """Unipus SSO 登录，返回带 iTEST 会话 cookie 的 Session。"""
    env = resolve_environ(ROOT)
    creds = load_credentials("itest", environ=env)
    sess = Session()

    _, _, url = sess.get(LOGIN)
    m = re.search(r"[?&]service=([^&]+)", url)
    if not m:
        raise SystemExit(f"登录页没有跳转到 SSO：{url}")
    service = urllib.parse.unquote(m.group(1))

    payload = {
        "service": service,
        "username": encrypt_login_field(creds.username),
        "password": encrypt_login_field(creds.password),
    }
    _, raw, _ = sess.post_json(f"{SSO}/sso/0.1/sso/cip/login", payload, {"Referer": url})
    data = json.loads(raw or b"{}")
    rs = data.get("rs") or {}
    if str(data.get("code")) != "0" or "serviceTicket" not in rs:
        raise SystemExit(f"SSO 登录失败：code={data.get('code')} msg={data.get('msg')!r}")

    ticket = rs["serviceTicket"]
    sess.get(f"{LOGIN}?{urllib.parse.urlencode({'ticket': ticket})}")
    sess.get(EXAM_PAGE)
    return sess


def exam_list(sess: Session) -> list[dict]:
    """学生端考试列表（分页接口，这里只取一页）。"""
    _, raw, _ = sess.post_json(
        f"{BASE}/utest/itest-mobile-api/student/exam/list", {}, {"Referer": EXAM_PAGE}
    )
    return json.loads(raw)["rs"]["data"]


def judge_entry(sess: Session, ksd_id: str, exam_code: str = "") -> str:
    """换答题窗口 URL。此调用只签发令牌，不会开始考试（已核验）。"""
    _, raw, _ = sess.post_form(
        f"{BASE}/utest/itest/s/exam/judgeEntry",
        {"examId": ksd_id, "examCode": exam_code},
        {"Referer": EXAM_PAGE},
    )
    data = json.loads(raw)
    if data.get("code") != 1:
        raise SystemExit(f"judgeEntry 失败：{data}")
    return data["data"]["url"]


def window_url(answer_url: str) -> str:
    """弹窗实际 URL = judgeEntry url + returnUrl（缺 returnUrl 会「没有权限」）。"""
    return answer_url + "&returnUrl=" + urllib.parse.quote(EXAM_PAGE, safe="")


# --------------------------------------------------------------------------
# 浏览器：进窗口 → 设备检测 → 开始考试 → 截获 load
# --------------------------------------------------------------------------


def _chromium_executable() -> str | None:
    cands = sorted(Path.home().glob(".cache/ms-playwright/chromium-*/chrome-linux*/chrome"))
    return str(cands[-1]) if cands else None


def _cookies(jar: http.cookiejar.CookieJar) -> list[dict]:
    return [
        {
            "name": c.name,
            "value": c.value or "",
            "domain": c.domain,
            "path": c.path or "/",
            "secure": bool(c.secure),
        }
        for c in jar
    ]


def _click_first(page, labels: list[str]) -> str | None:
    """按优先级点第一个出现的按钮，返回点中的文字。"""
    for label in labels:
        loc = page.get_by_text(label, exact=False)
        try:
            if loc.count() > 0:
                loc.first.click(timeout=8000)
                return label
        except Exception:  # noqa: BLE001 - 元素被遮挡/已消失时继续试下一个
            continue
    return None


def capture_load(url: str, ksd_id: str, jar: http.cookiejar.CookieJar) -> dict:
    """在浏览器里走到「开始考试」，返回 /answer/load 的 JSON。

    只点设备检测与「开始考试」；看到「加载成功」就停手（点确定才会真正开考计时）。
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        launch_kw = {"headless": True, "args": ["--disable-popup-blocking"]}
        try:
            browser = pw.chromium.launch(**launch_kw)
        except Exception:  # noqa: BLE001 - 版本与本地 browser build 不匹配时兜底
            exe = _chromium_executable()
            if not exe:
                raise SystemExit("找不到 Chromium，请先 `playwright install chromium`") from None
            browser = pw.chromium.launch(executable_path=exe, **launch_kw)

        ctx = browser.new_context(viewport={"width": 1440, "height": 900}, locale="zh-CN")
        ctx.add_cookies(_cookies(jar))
        page = ctx.new_page()
        page.set_default_timeout(30000)

        captured: dict[str, object] = {}

        page.goto(EXAM_PAGE, wait_until="networkidle")
        page.wait_for_timeout(2500)

        # 用「（ID：xxx）」定位考试卡片，再点卡片里的「进入考试」
        page.evaluate(
            """(ksd) => {
                const hit = [...document.querySelectorAll('*')].find(
                    (e) => e.children.length === 0 && (e.textContent || '').includes('ID：' + ksd)
                );
                let card = hit;
                while (card && !(card.innerText || '').includes('进入考试')) {
                    card = card.parentElement;
                }
                const btn = card && [...card.querySelectorAll('a,button,div,span')].find(
                    (e) => (e.innerText || '').trim() === '进入考试'
                );
                if (btn) btn.setAttribute('data-itest-target', '1');
            }""",
            ksd_id,
        )

        target = page.locator("[data-itest-target='1']").first
        with ctx.expect_page(timeout=20000) as popup_info:
            target.click()
        popup = popup_info.value

        def on_response(resp):
            if "/itest-api/itest/s/answer/load" in resp.url and "load" not in captured:
                captured["load"] = resp

        popup.on("response", on_response)
        popup.wait_for_timeout(3500)

        for _ in range(12):
            try:
                body = popup.inner_text("body")[:600]
            except Exception:  # noqa: BLE001 - 弹窗跳转瞬间取不到 body
                body = ""
            if "load" in captured:
                break
            if "考试冲突" in body:
                popup.locator(".layui-layer-btn0").first.click(timeout=8000)
                popup.wait_for_timeout(5000)
                continue
            if "加载成功" in body:
                break
            if not _click_first(popup, ["下一步", "检查正常", "开始考试", "继续作答"]):
                break
            popup.wait_for_timeout(5000)

        # 「加载成功」对话框不点确定：点它才开始计时，本脚本到此为止
        popup.wait_for_timeout(3000)
        result: dict = {}
        if "load" in captured:
            try:
                result = json.loads(captured["load"].text())
            except Exception as exc:  # noqa: BLE001
                raise SystemExit(f"读取 load 响应失败：{exc}") from exc
        browser.close()

    if not result:
        raise SystemExit("没有截获 /answer/load：设备检测或开始考试那一步没走通")
    return result


# --------------------------------------------------------------------------
# 解析：C_HTML → 结构化试卷
# --------------------------------------------------------------------------


def _attrs(tag_attrs: str) -> dict[str, str]:
    return {k: html_mod.unescape(v) for k, v in re.findall(r'([\w-]+)="([^"]*)"', tag_attrs)}


def _text(fragment: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", fragment)
    text = re.sub(r"<[^>]+>", "", text)
    text = html_mod.unescape(text).replace("\xa0", " ")
    return re.sub(r"[ \t]*\n[ \t\n]*", "\n", text).strip()


def _inputs(fragment: str) -> list[dict]:
    out = []
    for m in re.finditer(r"<(input|textarea)([^>]*)>", fragment):
        tag, raw = m.group(1), m.group(2)
        a = _attrs(raw)
        # 单选/复选的 radio、checkbox 属于 options，不算「填空位」
        if not a.get("qid") or a.get("type") in {"radio", "checkbox", "hidden"}:
            continue
        out.append(
            {
                "tag": tag,
                "qid": a.get("qid"),
                "qIndex": int(a.get("qIndex") or 0) or None,
                "qSubIndex": int(a.get("qSubIndex") or 0) or None,
                "qSubSubIndex": int(a.get("qSubSubIndex") or 0) or None,
                "kind": a.get("class") or tag,
                "value": a.get("value") or "",
            }
        )
    return out


def _options(fragment: str) -> list[dict]:
    """选项：优先 radio 的 <label>，其次 option-head 里的 "A) xxx B) yyy" 文本。"""
    out = []
    for m in re.finditer(r"<label[^>]*>(.*?)</label>", fragment, re.S):
        block = m.group(1)
        inp = re.search(r'<input[^>]*type="radio"[^>]*>', block)
        if not inp:
            continue
        a = _attrs(inp.group(0))
        out.append({"value": a.get("value", ""), "text": _text(block), "qOO": a.get("qOO", "")})
    if out:
        return out

    head = re.search(r'<div class="option-head">(.*?)</div>', fragment, re.S)
    if not head:
        return out
    text = _text(head.group(1))
    parts = re.findall(r"([A-Z])[)）.、]\s*([^A-Z]*?)(?=(?:[A-Z][)）.、])|$)", text, re.S)
    for value, label in parts:
        label = re.sub(r"\s+", " ", label).strip(" ；;")
        if label:
            out.append({"value": value, "text": label, "qOO": ""})
    return out


def _audios(fragment: str) -> list[str]:
    """听力资源块里内嵌的 JSON 数组（url, 时长, url, 时长…）。"""
    urls: list[str] = []
    for m in re.finditer(
        r'<div class="itest-hear-reslist"[^>]*>\s*(\[.*?\])\s*</div>', fragment, re.S
    ):
        try:
            arr = json.loads(html_mod.unescape(m.group(1)))
        except json.JSONDecodeError:
            continue
        urls.extend(str(x) for x in arr if isinstance(x, str) and x.startswith("http"))
    return urls


def parse_paper(payload: dict) -> dict:
    data = payload.get("data") or {}
    c_html = data.get("C_HTML") or ""

    marks = [m for m in re.finditer(r'<div class="itest-section"([^>]*)>', c_html)]
    sections = []
    for idx, mark in enumerate(marks):
        start = mark.end()
        end = marks[idx + 1].start() if idx + 1 < len(marks) else len(c_html)
        chunk = c_html[start:end]
        meta = _attrs(mark.group(1))

        questions = []
        for seg in re.split(r'(?=<div class="itest-ques")', chunk)[1:]:
            qm = re.search(r"<div class=\"itest-ques\"([^>]*)>", seg)
            if not qm:
                continue
            qattrs = _attrs(qm.group(1))
            # 题型容器：题目块里第一个真正的题型 div（听力题前面会先出现 reslist）
            skip = {
                "itest-ques",
                "itest-ques-set",
                "itest-section",
                "itest-direction",
                "itest-hear-reslist",
                "itest-hear-reslist-duration",
            }
            kinds = [
                m.group(1)
                for m in re.finditer(r'class="(itest-[a-z-]+)"', seg)
                if m.group(1) not in skip
            ]
            questions.append(
                {
                    "qid": qattrs.get("qid"),
                    "type": kinds[0] if kinds else "",
                    "resNeedPlay": qattrs.get("resNeedPlay", ""),
                    "text": _text(seg)[:1200],
                    "options": _options(seg),
                    "inputs": _inputs(seg),
                }
            )

        sections.append(
            {
                "sectionKey": meta.get("sectionKey", ""),
                "sectionType": meta.get("sectionType", ""),
                "part1": meta.get("part1", "").strip(),
                "part2": meta.get("part2", "").strip(),
                "resNeedPlay": meta.get("resNeedPlay", ""),
                "audios": _audios(chunk),
                "questions": questions,
            }
        )

    return {
        "uik": data.get("UIK"),
        "tl": data.get("TL"),
        "ntl": data.get("NTL"),
        "type": data.get("TYPE"),
        "optionOrder": data.get("OPTION_ORDER"),
        "backPlay": data.get("BACK_PLAY"),
        "sectionCount": len(sections),
        "questionCount": sum(len(s["questions"]) for s in sections),
        "sections": sections,
    }


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def cmd_list() -> None:
    sess = login()
    exams = exam_list(sess)
    print(f"{len(exams)} 场考试：")
    for e in exams:
        print(
            f"  {e['ksName']}\n"
            f"    ksdId={e['ksdId']} ksId={e['ksId']} 时长={e.get('ksLimit')}分钟 "
            f"已完成={e.get('finish')} 状态={e.get('beingOrOver')} "
            f"监考(capture/face/rtc/screenMon)={e.get('captureFlag')}/{e.get('faceRecognize')}/"
            f"{e.get('rtcFlag')}/{e.get('screenMonitoringFlag')}"
        )


def cmd_paper(ksd_id: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    sess = login()
    exams = {str(e["ksdId"]): e for e in exam_list(sess)}
    exam = exams.get(ksd_id)
    if not exam:
        raise SystemExit(f"列表里没有 ksdId={ksd_id}，先跑 list")

    print(f"[1/3] 换 token：{exam['ksName']}")
    url = judge_entry(sess, ksd_id)

    print("[2/3] 浏览器进窗口 → 设备检测 → 开始考试（不点「确定」，不交卷）")
    # 卡片上显示的是 ksId（列表 ID），judgeEntry 用的是 ksdId，别混用
    payload = capture_load(window_url(url), str(exam["ksId"]), sess.jar)

    raw_path = OUT / f"load_{ksd_id}.json"
    raw_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    print("[3/3] 解析试卷结构")
    paper = parse_paper(payload)
    paper["ksdId"] = ksd_id
    paper["ksName"] = exam["ksName"]
    paper_path = OUT / f"paper_{ksd_id}.json"
    paper_path.write_text(json.dumps(paper, ensure_ascii=False, indent=2), encoding="utf-8")

    print(
        f"    sections={paper['sectionCount']} questions={paper['questionCount']} "
        f"uik={paper['uik']} tl={paper['tl']}\n"
        f"    raw   → {raw_path.relative_to(ROOT)}\n"
        f"    paper → {paper_path.relative_to(ROOT)}"
    )
    for sec in paper["sections"]:
        kinds = sorted({q["type"] for q in sec["questions"] if q["type"]})
        print(
            f"    - {sec['part1'] or '(无标题)'} / {sec['part2'] or '-'} "
            f"[{sec['sectionType']}] 题{len(sec['questions'])} {kinds} "
            f"音频{len(sec['audios'])}"
        )


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] not in {"list", "paper"}:
        print(__doc__)
        return 2
    if argv[1] == "list":
        cmd_list()
        return 0
    if len(argv) < 3:
        print("用法：python3 scripts/itest_exam.py paper <ksdId>")
        return 2
    cmd_paper(argv[2])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
