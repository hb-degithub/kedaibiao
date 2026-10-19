#!/usr/bin/env python3
"""HAR 摘要器：把浏览器导出的抓包文件压成「接口清单」，顺带把凭据打码。

为什么要自己写一个：

1. 抓到的 HAR 里有 cookie、token、可能还有明文密码。把 HAR 原样丢进仓库、
   丢进聊天窗口、或者直接喂给模型，都等于泄露登录态；
2. 但适配器要的其实只是**结构** —— 路径长什么样、带哪些参数、响应有哪些字段；
3. 所以这里只提取结构，任何值都不落盘：敏感字段名一律 `***`，长字符串只留长度。

输出是「可以安全入库到 docs/research/」的接口清单，而不是流量副本。

用法::

    python3 scripts/har_mine.py 抓包.har                        # 打印 markdown 摘要
    python3 scripts/har_mine.py 抓包.har --out docs/research/pinco-api.md
    python3 scripts/har_mine.py 抓包.har --json /tmp/endpoints.json
    python3 scripts/har_mine.py 抓包.har --check-secrets        # 只体检敏感值

本脚本只用标准库，不联网，不上传任何东西。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

# --------------------------------------------------------------------------
# 打码规则
# --------------------------------------------------------------------------

#: 字段名里出现这些词，值一律替换为 `***`（大小写不敏感）
SENSITIVE_KEYS = (
    "password",
    "passwd",
    "pwd",
    "secret",
    "token",
    "ticket",
    "session",
    "cookie",
    "authorization",
    "auth",
    "sign",
    "signature",
    "credential",
    "captcha",
    "verifycode",
    "smscode",
    "openid",
    "unionid",
    "idcard",
    "id_card",
    "mobile",
    "phone",
    "telephone",
    "email",
    "studentno",
    "student_no",
    "realname",
    "real_name",
)

#: 这些请求头只保留名字，不保留值（值可能带 token）
SENSITIVE_HEADERS = ("cookie", "authorization", "set-cookie", "x-token", "x-auth-token")

#: 路径里像 ID 的片段：纯数字、UUID、长十六进制
_ID_RE = re.compile(r"^(?:\d+|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}|[0-9a-fA-F]{16,})$")

#: 与「作业/题目/提交」相关的关键字，用来把适配器要用的端点挑出来
INTERESTING = (
    "homework", "task", "exam", "paper", "question", "answer", "submit",
    "upload", "image", "img", "course", "class", "lesson", "study", "learn",
    "list", "detail", "info", "login", "user", "student",
)


def is_sensitive(name: str) -> bool:
    low = name.lower()
    return any(word in low for word in SENSITIVE_KEYS)


def mask_value(name: str, value: Any) -> str:
    """把值换成「不可还原但保留类型信息」的占位符。"""
    if is_sensitive(name):
        return "***"
    if isinstance(value, bool) or isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        if len(value) > 64:
            # 长值多半是密文/签名/编码过的内容，长度本身就是有用的线索
            return f"<str:{len(value)}>"
        if re.fullmatch(r"[A-Za-z0-9+/=]{32,}", value):
            return f"<b64:{len(value)}>"
        return value
    if isinstance(value, list):
        return f"<list:{len(value)}>"
    if isinstance(value, dict):
        return "{" + ", ".join(sorted(value)[:8]) + "}"
    return f"<{type(value).__name__}>"


def mask_pairs(pairs: list[tuple[str, Any]], limit: int = 40) -> dict[str, str]:
    return {k: mask_value(k, v) for k, v in pairs[:limit]}


# --------------------------------------------------------------------------
# HAR 解析
# --------------------------------------------------------------------------


@dataclass
class Endpoint:
    method: str
    host: str
    path: str  # 规范化后的路径模板
    count: int = 0
    statuses: Counter = field(default_factory=Counter)
    query_keys: dict[str, str] = field(default_factory=dict)
    body_keys: dict[str, str] = field(default_factory=dict)
    body_kind: str = ""
    response_keys: list[str] = field(default_factory=list)
    response_kind: str = ""
    request_headers: list[str] = field(default_factory=list)

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.method, self.host, self.path)

    @property
    def interesting(self) -> bool:
        blob = f"{self.host}{self.path}{' '.join(self.body_keys)}".lower()
        return any(word in blob for word in INTERESTING)


def normalize_path(path: str) -> str:
    out = []
    for seg in path.split("/"):
        out.append("{id}" if _ID_RE.match(seg) else seg)
    return "/".join(out) or "/"


def _json_keys(text: str, limit: int = 40) -> list[str]:
    """只取 JSON 的顶层键名，不取值 —— 这样就够还原接口形状了。"""
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return []
    if isinstance(data, dict):
        return sorted(data)[:limit]
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return sorted(data[0])[:limit]
    return []


def _nested_keys(text: str, depth: int = 5, limit: int = 60) -> list[str]:
    """递归取键名（点号路径），帮助看清嵌套结构，例如 data.list[].id。"""
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return []

    found: list[str] = []

    def walk(node: Any, prefix: str, level: int) -> None:
        if len(found) >= limit or level > depth:
            return
        if isinstance(node, dict):
            for k, v in node.items():
                path = f"{prefix}.{k}" if prefix else k
                found.append(path)
                walk(v, path, level + 1)
        elif isinstance(node, list) and node:
            walk(node[0], f"{prefix}[]", level + 1)

    walk(data, "", 1)
    return found[:limit]


def parse_har(har: dict[str, Any]) -> list[Endpoint]:
    entries = har.get("log", {}).get("entries", [])
    table: dict[tuple[str, str, str], Endpoint] = {}

    for entry in entries:
        req = entry.get("request", {})
        resp = entry.get("response", {})
        url = req.get("url", "")
        split = urlsplit(url)
        method = (req.get("method") or "GET").upper()
        host = split.netloc
        path = normalize_path(split.path)

        ep = table.get((method, host, path))
        if ep is None:
            ep = Endpoint(method=method, host=host, path=path)
            table[(method, host, path)] = ep

        ep.count += 1
        ep.statuses[resp.get("status", 0)] += 1

        # 查询参数
        if split.query:
            for k, v in parse_qsl(split.query, keep_blank_values=True):
                ep.query_keys.setdefault(k, mask_value(k, v))

        # 请求体：只认 form-urlencoded 与 JSON 两种
        post = req.get("postData") or {}
        mime = (post.get("mimeType") or "").lower()
        text = post.get("text") or ""
        if text and "json" in mime:
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict):
                ep.body_kind = "json"
                ep.body_keys.update(mask_pairs(list(payload.items())))
        elif text and "form" in mime:
            ep.body_kind = "form"
            ep.body_keys.update(mask_pairs(parse_qsl(text, keep_blank_values=True)))

        # 响应体
        content = resp.get("content") or {}
        rmime = (content.get("mimeType") or "").lower()
        rtext = content.get("text") or ""
        if "json" in rmime and rtext:
            ep.response_kind = "json"
            keys = _nested_keys(rtext)
            for k in keys:
                if k not in ep.response_keys:
                    ep.response_keys.append(k)

        # 请求头只留名字（值可能带 token）
        for h in req.get("headers", []) or []:
            name = (h.get("name") or "").lower()
            if name not in ep.request_headers:
                ep.request_headers.append(name)

    return sorted(table.values(), key=lambda e: (e.host, e.path, e.method))


# --------------------------------------------------------------------------
# 体检：HAR 里有没有明文敏感值
# --------------------------------------------------------------------------


def secret_report(har: dict[str, Any]) -> list[str]:
    """检查 HAR 里是否真的存在明文敏感值 —— 提醒使用者别外传这个文件。"""
    findings: list[str] = []
    for entry in har.get("log", {}).get("entries", []):
        req = entry.get("request", {})
        url = req.get("url", "")
        split = urlsplit(url)
        for k, v in parse_qsl(split.query, keep_blank_values=True):
            if is_sensitive(k) and v:
                findings.append(f"查询参数含敏感值: {split.netloc}{normalize_path(split.path)} 参数名 {k}")
        post = req.get("postData") or {}
        text = post.get("text") or ""
        for k, v in parse_qsl(text, keep_blank_values=True):
            if is_sensitive(k) and v and v != "***":
                findings.append(f"请求体含敏感值: {split.netloc}{normalize_path(split.path)} 字段 {k}")
        resp = entry.get("response", {})
        for h in resp.get("headers", []) or []:
            if (h.get("name") or "").lower() == "set-cookie":
                findings.append(f"响应下发 cookie: {split.netloc}{normalize_path(split.path)}")
    # 去重保序
    seen: set[str] = set()
    out: list[str] = []
    for f in findings:
        if f not in seen:
            seen.add(f)
            out.append(f)
    return out


# --------------------------------------------------------------------------
# 渲染
# --------------------------------------------------------------------------


def render_markdown(endpoints: list[Endpoint], source: str) -> str:
    lines = [
        "# 抓包接口清单（脚本生成，可安全入库）",
        "",
        f"- 来源 HAR：`{source}`（该文件含登录态，**不入库、不外传**）",
        f"- 端点总数：{len(endpoints)}（去重后按 host+path 归并）",
        "- 本清单只含结构：路径模板、参数名、响应字段名；所有敏感值均已打码",
        "",
        "> 路径中形如 `{id}` 的片段是原路径里的数字 / UUID / 长十六进制 ID。",
        "",
        "## 1. 与作业/题目/提交相关的端点",
        "",
    ]

    def block(eps: list[Endpoint]) -> list[str]:
        out: list[str] = []
        for ep in eps:
            out.append(f"### `{ep.method} {ep.path}`")
            out.append("")
            out.append(f"- host: `{ep.host}`；出现 {ep.count} 次；状态码 {dict(ep.statuses)}")
            if ep.query_keys:
                out.append(f"- 查询参数：`{json.dumps(ep.query_keys, ensure_ascii=False)}`")
            if ep.body_keys:
                out.append(f"- 请求体（{ep.body_kind}）：`{json.dumps(ep.body_keys, ensure_ascii=False)}`")
            if ep.response_keys:
                keys = ", ".join(f"`{k}`" for k in ep.response_keys[:30])
                more = " …" if len(ep.response_keys) > 30 else ""
                out.append(f"- 响应字段：{keys}{more}")
            else:
                out.append(f"- 响应：非 JSON 或未保存（{ep.response_kind or 'n/a'}）")
            out.append("")
        return out

    interesting = [e for e in endpoints if e.interesting]
    others = [e for e in endpoints if not e.interesting]
    lines += block(interesting) or ["（没有命中关键字的端点，见第 2 节全集）", ""]

    lines += ["## 2. 其余端点（全集）", ""]
    lines += block(others)

    hosts = Counter(e.host for e in endpoints)
    lines += ["## 3. 主机分布", ""]
    for host, n in hosts.most_common():
        lines.append(f"- `{host}`：{n} 个端点")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="HAR 摘要器：提取接口结构并打码")
    parser.add_argument("har", help="HAR 文件路径")
    parser.add_argument("--out", help="把 markdown 清单写到该文件")
    parser.add_argument("--json", dest="json_out", help="把端点结构写成 JSON")
    parser.add_argument("--check-secrets", action="store_true", help="只做敏感值体检并退出")
    args = parser.parse_args(argv)

    src = Path(args.har)
    if not src.is_file():
        print(f"找不到 HAR 文件: {src}", file=sys.stderr)
        return 2
    har = json.loads(src.read_text(encoding="utf-8"))

    findings = secret_report(har)
    if args.check_secrets:
        print(f"敏感值体检：命中 {len(findings)} 处")
        for f in findings:
            print(f"  - {f}")
        if findings:
            print("\n结论：这个 HAR 含可用登录态，禁止入库 / 禁止外传。")
        return 0

    endpoints = parse_har(har)
    if not endpoints:
        print("HAR 里没有 entries —— 抓包时是不是没勾选 Preserve log？", file=sys.stderr)
        return 1

    md = render_markdown(endpoints, src.name)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(md, encoding="utf-8")
        print(f"已写入 {out}（{len(endpoints)} 个端点）")
    else:
        print(md)

    if args.json_out:
        data = [
            {
                "method": e.method,
                "host": e.host,
                "path": e.path,
                "count": e.count,
                "statuses": dict(e.statuses),
                "query": e.query_keys,
                "body_kind": e.body_kind,
                "body": e.body_keys,
                "response_keys": e.response_keys,
            }
            for e in endpoints
        ]
        Path(args.json_out).write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"已写入 {args.json_out}")

    print(f"\n注意：{len(findings)} 处敏感值已打码（原文仍在 {src.name} 里，该文件不要外传）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
