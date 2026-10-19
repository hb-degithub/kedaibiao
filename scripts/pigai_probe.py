#!/usr/bin/env python3
"""批改网 www.pigai.org 学生端 —— 只读侦察探针（Stage 1b）。

纪律（见 docs/research/pigai.md 第 1.2 节）：
  * 登录 POST 是唯一允许的非 GET 请求，且不改动服务端业务状态；
  * 之后一律只发 GET；
  * 不打印密码、不打印 cookie 的值、不打印姓名/学号/手机号；
  * 产物只落 data/recon/pigai/（已被 .gitignore 忽略，不入库）。

用法：
    python3 scripts/pigai_probe.py login          # 登录 + 落地页清单
    python3 scripts/pigai_probe.py get <路径>      # 复用已保存的会话发一个 GET
"""

from __future__ import annotations

import base64
import http.cookiejar
import json
import re
import secrets
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "recon" / "pigai"
RAW = OUT / "raw"
JAR = OUT / "cookies.json"

BASE = "https://www.pigai.org"
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

#: 前端 2020.js 第 141 行硬编码的公钥（SPKI / base64）
PUBKEY = (
    "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQCYlII16dSwQErDgjIl8BzU4NEL2IzvyWiL"
    "Nxie3mkpw6eseF/iUVb3bisAFH+lzgnrv/mBOKUMkbqtW2+8en/6r0hj6ctvGT+UOtg4P5LF"
    "/jxkbE+cA2fVJK2RaBzeEEbrKOvauVnGkEOvPVl1/NK4NgeN6aSPIK9ECfXcjlEOHwIDAQAB"
)


# --------------------------------------------------------------------------
# 凭据与密钥
# --------------------------------------------------------------------------


def load_env(path: Path | None = None) -> dict[str, str]:
    path = path or ROOT / ".env"
    env: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
    return env


def _tlv(buf: bytes, i: int) -> tuple[int, bytes, int]:
    tag = buf[i]
    i += 1
    ln = buf[i]
    i += 1
    if ln & 0x80:
        n = ln & 0x7F
        ln = int.from_bytes(buf[i : i + n], "big")
        i += n
    return tag, buf[i : i + ln], i + ln


def rsa_pubkey(pem_b64: str) -> tuple[int, int]:
    """从 SPKI/PEM base64 解出 (n, e)。"""
    der = base64.b64decode(pem_b64)
    _, spki, _ = _tlv(der, 0)
    _, alg, i = _tlv(spki, 0)
    _, _, _ = _tlv(alg, 0)
    _, bs, _ = _tlv(spki, i)
    _, pk, _ = _tlv(bs, 1)
    _, nb, j = _tlv(pk, 0)
    _, eb, _ = _tlv(pk, j)
    return int.from_bytes(nb, "big"), int.from_bytes(eb, "big")


def pkcs1v15_encrypt(msg: bytes) -> str:
    """复刻 JSEncrypt 的 RSAES-PKCS1-v1_5，返回 base64。仅用标准库。"""
    n, e = _N, _E
    k = (n.bit_length() + 7) // 8
    if len(msg) > k - 11:
        raise ValueError(f"明文过长：{len(msg)} > {k - 11}")
    pad = bytearray()
    while len(pad) < k - 3 - len(msg):
        b = secrets.token_bytes(1)[0]
        if b:  # JSEncrypt pkcs1pad2 同样跳过零字节
            pad.append(b)
    em = b"\x00\x02" + bytes(pad) + b"\x00" + msg
    return base64.b64encode(pow(int.from_bytes(em, "big"), e, n).to_bytes(k, "big")).decode()


_N, _E = rsa_pubkey(PUBKEY)


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


class _Recorder(urllib.request.HTTPRedirectHandler):
    """记录重定向跳转（登录成功判据要靠它）。"""

    def __init__(self) -> None:
        self.hops: list[tuple[int, str]] = []

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        self.hops.append((code, newurl))
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def make_opener() -> tuple[urllib.request.OpenerDirector, http.cookiejar.CookieJar, _Recorder]:
    jar = http.cookiejar.CookieJar()
    rec = _Recorder()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(jar), rec
    )
    opener.addheaders = [("User-Agent", UA), ("Accept-Language", "zh-CN,zh;q=0.9")]
    return opener, jar, rec


def dump_cookies(jar: http.cookiejar.CookieJar) -> None:
    """只打印 cookie 的属性，绝不打印值。"""
    print("  cookie 数量:", len(jar))
    for c in jar:
        flags = []
        if c.secure:
            flags.append("Secure")
        if c.has_nonstandard_attr("HttpOnly") or c.has_nonstandard_attr("httponly"):
            flags.append("HttpOnly")
        print(
            f"    {c.name:20s} domain={c.domain:18s} path={c.path:6s} "
            f"len={len(c.value or ''):4d} {' '.join(flags)}"
        )


def save_cookies(jar: http.cookiejar.CookieJar) -> None:
    JAR.parent.mkdir(parents=True, exist_ok=True)
    data = [
        {
            "name": c.name,
            "value": c.value,
            "domain": c.domain,
            "path": c.path,
            "secure": c.secure,
        }
        for c in jar
    ]
    JAR.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    JAR.chmod(0o600)
    print(f"  会话已存: {JAR.relative_to(ROOT)} (mode 600)")


def redact(text: str) -> str:
    """抹掉疑似手机号 / 长数字串 / 邮箱，避免个人信息进终端与文档。"""
    text = re.sub(r"1[3-9]\d{9}", "<手机号>", text)
    text = re.sub(r"\b\d{8,}\b", "<数字>", text)
    text = re.sub(r"[\w.+-]+@[\w-]+\.[\w.]+", "<邮箱>", text)
    return text


def inventory(html: str) -> None:
    """列出页面里的端点与功能入口。"""
    print("\n  -- 端点清单 --")
    old = sorted(set(re.findall(r"/index\.php\?a=[A-Za-z0-9_]+", html)))
    new = sorted(set(re.findall(r"/index\.php\?c=[A-Za-z0-9_]+(?:&(?:amp;)?a=[A-Za-z0-9_]+)?", html)))
    for u in old:
        print("    ", u)
    for u in new:
        print("    ", u.replace("&amp;", "&"))
    print(f"    （老式 {len(old)} 个 / 新式 {len(new)} 个）")

    print("\n  -- 功能入口（含 作业/作文/成绩/班级/我的 的链接文本）--")
    seen = set()
    for m in re.finditer(r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', html, re.S | re.I):
        href, txt = m.group(1), re.sub(r"<[^>]+>", "", m.group(2)).strip()
        if not txt or not re.search(r"作业|作文|成绩|班级|我的|批改|写作|报告", txt):
            continue
        key = (txt, href)
        if key in seen:
            continue
        seen.add(key)
        print(f"     {redact(txt)[:24]:26s} -> {redact(href)[:90]}")
        if len(seen) >= 40:
            print("     ...（截断）")
            break


# --------------------------------------------------------------------------
# 命令
# --------------------------------------------------------------------------


def cmd_login() -> int:
    env = load_env()
    user = env.get("KEDAIBIAO_PIGAI_USER", "")
    pwd = env.get("KEDAIBIAO_PIGAI_PASS", "")
    if not user or not pwd:
        print("✗ .env 里 KEDAIBIAO_PIGAI_USER / _PASS 未填写", file=sys.stderr)
        return 2
    print(f"账号: {redact(user)}  密码: <已读取，长度 {len(pwd)}，不打印>")

    opener, jar, rec = make_opener()
    RAW.mkdir(parents=True, exist_ok=True)

    # 1) 先访首页，取服务端下发的 cookie（old=2012 等）
    r = opener.open(BASE + "/", timeout=30)
    pre = r.read()
    (RAW / "b_home_prelaunch.html").write_bytes(pre)
    print(f"\n[1] GET /            -> {r.status}  {len(pre)}B   cookie={len(jar)}")

    # 2) 登录（唯一允许的 POST）
    body = urllib.parse.urlencode(
        {
            "username": user,
            "password": "",
            "remember": "1",
            "checkhash": "",
            "password_encrypt": pkcs1v15_encrypt(pwd.encode("utf-8")),
        }
    ).encode()
    req = urllib.request.Request(
        BASE + "/index.php?a=login",
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": BASE + "/",
            "Origin": BASE,
        },
    )
    try:
        r2 = opener.open(req, timeout=30)
        code, html = r2.status, r2.read()
    except urllib.error.HTTPError as exc:
        code, html = exc.code, exc.read()
        print(f"[!] HTTPError {exc.code}")
    final = r2.geturl() if "r2" in dir() else "?"
    print(f"[2] POST ?a=login    -> {code}  最终 URL: {redact(final)}")
    print("    重定向链:", [(c, redact(u)) for c, u in rec.hops])
    (RAW / "b_after_login.html").write_bytes(html)
    dump_cookies(jar)
    save_cookies(jar)

    h = html.decode("utf-8", "replace")
    logged_in = "lg_from_w" not in h
    print(f"\n[3] 登录判据: 落地页{'不再是登录页 → 判定成功' if logged_in else '仍是登录表单 → 判定失败'}")
    title = re.search(r"<title>(.*?)</title>", h, re.S)
    print("    标题:", redact(title.group(1).strip()) if title else "(无)")

    if logged_in:
        inventory(h)
    else:
        for kw in ["密码", "错误", "验证码", "错误信息", "alert"]:
            for m in re.finditer(kw, h):
                print("    失败线索:", redact(re.sub(r"\s+", " ", h[max(0, m.start() - 60) : m.start() + 80])))
                break
    return 0


def cmd_get(path: str) -> int:
    env = load_env()
    del env
    opener, jar, _ = make_opener()
    for c in json.loads(JAR.read_text(encoding="utf-8")):
        jar.set_cookie(
            http.cookiejar.Cookie(
                version=0, name=c["name"], value=c["value"], port=None, port_specified=False,
                domain=c["domain"], domain_specified=True, domain_initial_dot=c["domain"].startswith("."),
                path=c["path"], path_specified=True, secure=c["secure"], expires=None,
                discard=False, comment=None, comment_url=None, rest={}, rfc2109=False,
            )
        )
    url = path if path.startswith("http") else BASE + path
    r = opener.open(url, timeout=30)
    html = r.read().decode("utf-8", "replace")
    name = re.sub(r"[^A-Za-z0-9]+", "_", path)[:60].strip("_") or "page"
    (RAW / f"b_{name}.html").write_text(html, encoding="utf-8")
    print(f"GET {redact(url)} -> {r.status} {len(html)}B  saved b_{name}.html")
    title = re.search(r"<title>(.*?)</title>", html, re.S)
    print("标题:", redact(title.group(1).strip()) if title else "(无)")
    inventory(html)
    return 0


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 1
    cmd, *rest = argv
    if cmd == "login":
        return cmd_login()
    if cmd == "get":
        if not rest:
            print("用法: pigai_probe.py get <路径>", file=sys.stderr)
            return 1
        return cmd_get(rest[0])
    print(f"未知命令: {cmd}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
