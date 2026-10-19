"""最小的 Chrome DevTools Protocol 客户端（纯标准库）。

## 为什么自己写

抓 pinco 的 csrf cookie 必须要有真实浏览器会话，而本机**装不了 playwright**
（没有 pip，`python3 -m venv` 缺 ensurepip）。但系统里往往已经有 Chrome/Chromium，
它自带 `--remote-debugging-port`，CDP 就是个 WebSocket 协议 —— 用标准库的
socket 实现一个够用的客户端即可，不需要任何第三方依赖。

## 能力边界（刻意只做需要的）

- 只做 **文本帧**、不做分片、不做扩展协商 —— CDP 的消息都是一条一条的 JSON；
- 不做 TLS（CDP 端口只监听回环，明文足够）；
- 只暴露 `Page.navigate` / `Network.getAllCookies` / `Runtime.evaluate` 这类读操作。

## 抓 cookie 的做法

不是「打开窗口让用户点」—— 这台机器没有显示器。而是：

1. 用一个**已经登录过目标站点**的 Chrome 用户目录（`--user-data-dir`）；
2. headless 启动，打开目标页面，让浏览器自己带上登录态；
3. 通过 `Network.getAllCookies` 把 cookie 读出来。

所以前提是「那个用户目录里登录过」—— 这比让用户去 DevTools 里翻 Cookie 头
省事得多，而且不接触密码、不碰验证码。
"""

from __future__ import annotations

import base64
import json
import logging
import os
import secrets
import shutil
import socket
import struct
import subprocess
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: 常见的浏览器可执行文件位置（按优先级）
BROWSER_CANDIDATES = (
    # playwright 缓存的 chromium（本机有）
    "~/.cache/ms-playwright/chromium-*/chrome-linux64/chrome",
    "~/.cache/ms-playwright/chromium-*/chrome-linux/chrome",
    # 系统安装
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/snap/bin/chromium",
    # macOS / Windows
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
)


class CDPError(RuntimeError):
    pass


def find_browser() -> str | None:
    """找一个可用的 Chrome/Chromium；找不到返回 None。"""
    from glob import glob

    for pattern in BROWSER_CANDIDATES:
        expanded = os.path.expanduser(pattern)
        if "*" in expanded:
            hits = sorted(glob(expanded), reverse=True)
            if hits:
                return hits[0]
            continue
        if Path(expanded).is_file():
            return expanded
    found = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chrome")
    return found


# --------------------------------------------------------------------------
# 极简 WebSocket 客户端（RFC 6455，够 CDP 用）
# --------------------------------------------------------------------------


class WebSocket:
    """客户端侧的 WebSocket。只支持文本帧、不支持分片与扩展。"""

    def __init__(self, host: str, port: int, path: str, timeout: float = 10.0) -> None:
        self._sock = socket.create_connection((host, port), timeout=timeout)
        self._sock.settimeout(timeout)
        key = base64.b64encode(secrets.token_bytes(16)).decode()
        handshake = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self._sock.sendall(handshake.encode())
        response = self._read_until(b"\r\n\r\n")
        if b"101" not in response.split(b"\r\n", 1)[0]:
            raise CDPError(f"WebSocket 握手失败: {response[:120]!r}")

    def _read_until(self, marker: bytes) -> bytes:
        buffer = b""
        while marker not in buffer:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise CDPError("连接在握手阶段被关闭")
            buffer += chunk
        return buffer

    def send_text(self, text: str) -> None:
        payload = text.encode("utf-8")
        header = bytearray([0x81])  # FIN + text
        mask_bit = 0x80
        length = len(payload)
        if length < 126:
            header.append(mask_bit | length)
        elif length < 65536:
            header.append(mask_bit | 126)
            header += struct.pack("!H", length)
        else:
            header.append(mask_bit | 127)
            header += struct.pack("!Q", length)
        mask = secrets.token_bytes(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self._sock.sendall(bytes(header) + masked)

    def _recv_exact(self, n: int) -> bytes:
        buffer = b""
        while len(buffer) < n:
            chunk = self._sock.recv(n - len(buffer))
            if not chunk:
                raise CDPError("连接已关闭")
            buffer += chunk
        return buffer

    def recv_text(self) -> str:
        while True:
            header = self._recv_exact(2)
            opcode = header[0] & 0x0F
            masked = bool(header[1] & 0x80)
            length = header[1] & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._recv_exact(8))[0]
            mask = self._recv_exact(4) if masked else b""
            payload = self._recv_exact(length)
            if masked:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))

            if opcode == 0x8:  # close
                raise CDPError("对端关闭了连接")
            if opcode == 0x9:  # ping → pong
                self._send_frame(0x8A, payload)
                continue
            if opcode in (0x1, 0x2):
                return payload.decode("utf-8", errors="replace")
            # 其它（pong 等）忽略，继续读

    def _send_frame(self, first_byte: int, payload: bytes) -> None:
        header = bytearray([first_byte])
        mask = secrets.token_bytes(4)
        header.append(0x80 | len(payload))
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self._sock.sendall(bytes(header) + masked)

    def close(self) -> None:
        try:
            self._send_frame(0x88, b"")
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass


# --------------------------------------------------------------------------
# CDP 会话
# --------------------------------------------------------------------------


@dataclass(slots=True)
class Cookie:
    name: str
    value: str
    domain: str = ""
    path: str = "/"
    http_only: bool = False
    secure: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "value": self.value, "domain": self.domain,
            "path": self.path, "httpOnly": self.http_only, "secure": self.secure,
        }


class Browser:
    """启动一个 headless 浏览器，连上它的调试端口。"""

    def __init__(
        self,
        *,
        executable: str | None = None,
        user_data_dir: str | Path | None = None,
        port: int = 0,
        timeout: float = 20.0,
    ) -> None:
        self.executable = executable or find_browser()
        if not self.executable:
            raise CDPError(
                "找不到 Chrome/Chromium。请安装 Chrome，或用 --browser 指定可执行文件路径。"
            )
        self.user_data_dir = Path(user_data_dir) if user_data_dir else None
        self.timeout = timeout
        #: 出现原生对话框时自动点「确定」（页面会被对话框阻塞，必须处理）
        self.auto_accept_dialogs = True
        #: 见过的对话框（排查用）
        self.dialogs_seen: list[dict[str, Any]] = []
        self._proc: subprocess.Popen[bytes] | None = None
        self._ws: WebSocket | None = None
        self._seq = 0
        self.port = port
        self._lock_path: Path | None = None

    # -- 生命周期 ---------------------------------------------------------

    def __enter__(self) -> Browser:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def start(self) -> None:
        if self.user_data_dir is not None:
            self.user_data_dir.mkdir(parents=True, exist_ok=True)
            lock = self.user_data_dir / "SingletonLock"
            if lock.exists() or lock.is_symlink():
                raise CDPError(
                    f"用户目录被占用（{lock}）—— 那个 Chrome 正在运行。"
                    "请先完全退出它，或复制一份 profile 目录再用 --profile 指定。"
                )

        args = [
            self.executable,
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-networking",
            "--remote-debugging-port=" + (str(self.port) if self.port else "0"),
        ]
        if self.user_data_dir is not None:
            args.append(f"--user-data-dir={self.user_data_dir}")
        else:
            import tempfile

            self.user_data_dir = Path(tempfile.mkdtemp(prefix="kdb-cdp-"))
            args.append(f"--user-data-dir={self.user_data_dir}")
        if os.name != "nt":
            args.append("--no-sandbox")

        log.debug("启动浏览器: %s", " ".join(args))
        self._proc = subprocess.Popen(  # noqa: S603 - 路径由用户/探测决定
            args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        self.port = self._wait_for_port()

    def _wait_for_port(self) -> int:
        """从 DevToolsActivePort 文件或进程参数里拿到实际端口。"""
        if self.port:
            port = self.port
            self._wait_http(port)
            return port
        assert self.user_data_dir is not None
        marker = self.user_data_dir / "DevToolsActivePort"
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            if marker.is_file():
                first = marker.read_text(encoding="utf-8").splitlines()
                if first and first[0].strip().isdigit():
                    port = int(first[0].strip())
                    self._wait_http(port)
                    return port
            if self._proc is not None and self._proc.poll() is not None:
                raise CDPError("浏览器启动后立刻退出了（可执行文件路径或参数有问题）")
            time.sleep(0.1)
        raise CDPError(f"等不到调试端口（超时 {self.timeout}s）")

    def _wait_http(self, port: int) -> None:
        url = f"http://127.0.0.1:{port}/json/version"
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(url, timeout=1) as resp:  # noqa: S310 - 回环
                    if resp.status == 200:
                        return
            except OSError:
                time.sleep(0.1)
        raise CDPError("调试端口没响应")

    def close(self) -> None:
        if self._ws is not None:
            self._ws.close()
            self._ws = None
        if self._proc is not None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            self._proc = None

    # -- 页面 -------------------------------------------------------------

    def _page_target(self) -> dict[str, Any]:
        url = f"http://127.0.0.1:{self.port}/json/list"
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(url, timeout=2) as resp:  # noqa: S310
                    targets = json.loads(resp.read())
                for target in targets:
                    if target.get("type") == "page" and target.get("webSocketDebuggerUrl"):
                        return target
            except OSError:
                pass
            time.sleep(0.1)
        raise CDPError("没有可用的页面目标")

    def connect(self) -> WebSocket:
        if self._ws is not None:
            return self._ws
        target = self._page_target()
        path = target["webSocketDebuggerUrl"].split(f"127.0.0.1:{self.port}", 1)[-1]
        self._ws = WebSocket("127.0.0.1", self.port, path)
        return self._ws

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        ws = self.connect()
        self._seq += 1
        message_id = self._seq
        ws.send_text(json.dumps({"id": message_id, "method": method, "params": params or {}}))
        while True:
            raw = ws.recv_text()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if data.get("id") != message_id:
                # 事件推送：对话框必须就地处理，否则页面会被阻塞住
                if self._handle_event(ws, data):
                    continue
                continue
            if "error" in data:
                raise CDPError(f"{method} 失败: {data['error']}")
            return data.get("result") or {}


    # -- 事件感知：抓特定响应体（登录验证码求解用）------------------------

    def evaluate(self, expression: str, *, await_promise: bool = True) -> Any:
        """在页面里跑一段 JS 并取回结果。"""
        result = self.call("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": await_promise,
        })
        return (result.get("result") or {}).get("value")

    def enable_network(self) -> None:
        """开好抓包所需的域。

        极验组件跑在跨域 iframe 里，**必须** ``Target.setAutoAttach`` ——
        否则它的请求不会出现在当前连接的会话上（实测）。
        """
        self.call("Page.enable")
        self.call("Runtime.enable")
        self.call("Network.enable")
        self.call("Target.setAutoAttach", {
            "autoAttach": True, "flatten": True, "waitForDebuggerOnStart": False,
        })

    def capture_response(
        self, url_pattern: str, *, trigger_script: str = "",
        timeout: float = 30.0, settle: float = 3.0,
    ) -> dict[str, str]:
        """等一个 URL 含 ``url_pattern`` 的响应，返回 ``{"url","body"}``。

        ``trigger_script`` 是**在被监听期间执行的一次 JS**（例如「点开始考试」）。
        它由本方法自己发送，**不经过** :meth:`call` —— 因为 ``call`` 会丢弃
        事件，那样会漏掉触发瞬间到达的请求；而且多线程共用一条 socket 会
        让消息错位、连接被对端关闭（实测踩过）。

        空闲 ``settle`` 秒没有新消息就认为「不会有响应了」，总时长不超过 ``timeout``。
        找不到就返回空 dict —— 由调用方决定重试还是报错。
        """
        ws = self.connect()
        tracked: dict[str, str] = {}
        hard = time.monotonic() + timeout
        idle = time.monotonic() + settle
        if trigger_script:
            self._seq += 1
            # awaitPromise=False：点一下就走，不等页面，避免把事件读窗口挤掉
            ws.send_text(json.dumps({
                "id": self._seq, "method": "Runtime.evaluate",
                "params": {"expression": trigger_script, "awaitPromise": False,
                           "returnByValue": True},
            }))
        while time.monotonic() < min(hard, idle):
            try:
                raw = ws.recv_text()
            except Exception:  # noqa: BLE001 - 读超时继续轮询
                continue
            idle = time.monotonic() + settle
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if self._handle_event(ws, data):
                continue
            method = data.get("method")
            params = data.get("params") or {}
            if method == "Network.requestWillBeSent":
                request = params.get("request") or {}
                if url_pattern in str(request.get("url") or ""):
                    tracked[params.get("requestId")] = request.get("url")
            elif method == "Network.loadingFinished":
                request_id = params.get("requestId")
                if request_id in tracked:
                    try:
                        body = self.call("Network.getResponseBody", {"requestId": request_id})
                    except CDPError:
                        continue
                    return {"url": tracked.pop(request_id), "body": body.get("body") or ""}
        return {}

    def _handle_event(self, ws: WebSocket, data: dict[str, Any]) -> bool:
        """处理需要立即响应的事件。返回 True 表示已处理。

        目前只处理**原生对话框**（alert/confirm/beforeunload）：
        它们一出现就会阻塞页面，而按钮点击经常正是通过 confirm 走的
        （iTEST 的「开始考试」就是这样）。不处理的话页面直接卡死。
        """
        method = data.get("method")
        if method == "Page.javascriptDialogOpening" and self.auto_accept_dialogs:
            params = data.get("params") or {}
            self.dialogs_seen.append({
                "type": params.get("type"), "message": params.get("message"),
            })
            log.info("自动接受对话框 [%s] %s", params.get("type"), params.get("message"))
            self._seq += 1
            ws.send_text(json.dumps({
                "id": self._seq, "method": "Page.handleJavaScriptDialog",
                "params": {"accept": True},
            }))
            return True
        return False

    def navigate(self, url: str) -> None:
        self.call("Page.enable")
        self.call("Page.navigate", {"url": url})

    # -- 画面与输入（验证码中继用）----------------------------------------

    def screenshot(self, *, format: str = "png", quality: int | None = None) -> bytes:
        """截一张当前视口。返回原始字节。"""
        params: dict[str, Any] = {"format": format}
        if quality is not None and format == "jpeg":
            params["quality"] = quality
        result = self.call("Page.captureScreenshot", params)
        return base64.b64decode(result.get("data", ""))

    def mouse(self, kind: str, x: float, y: float, *, button: str = "left",
              click_count: int = 1) -> None:
        """发一次鼠标事件。``kind`` ∈ pressed / released / moved。"""
        self.call("Input.dispatchMouseEvent", {
            "type": f"mouse{kind.capitalize()}",
            "x": x, "y": y, "button": button, "clickCount": click_count,
        })

    def click(self, x: float, y: float) -> None:
        self.mouse("pressed", x, y)
        self.mouse("released", x, y)

    def drag(self, path: list[tuple[float, float]], *, steps: int = 12) -> None:
        """按给定折线拖拽（用于滑块验证码）。

        轨迹本身就是「人怎么拖」的记录 —— 中继把用户的真实轨迹原样回放，
        不做任何「绕验证码」的轨迹伪造。
        """
        if not path:
            return
        start_x, start_y = path[0]
        self.mouse("pressed", start_x, start_y)
        previous = (start_x, start_y)
        for target in path[1:]:
            tx, ty = target
            for step in range(1, steps + 1):
                ratio = step / steps
                self.mouse("moved", previous[0] + (tx - previous[0]) * ratio,
                           previous[1] + (ty - previous[1]) * ratio)
            previous = (tx, ty)
        self.mouse("released", previous[0], previous[1])

    def insert_text(self, text: str) -> None:
        self.call("Input.insertText", {"text": text})

    def press_key(self, key: str) -> None:
        for kind in ("keyDown", "keyUp"):
            self.call("Input.dispatchKeyEvent", {"type": kind, "key": key})

    def cookies(self) -> list[Cookie]:
        result = self.call("Network.getAllCookies")
        out: list[Cookie] = []
        for item in result.get("cookies") or []:
            out.append(
                Cookie(
                    name=str(item.get("name", "")),
                    value=str(item.get("value", "")),
                    domain=str(item.get("domain", "")),
                    path=str(item.get("path", "/")),
                    http_only=bool(item.get("httpOnly")),
                    secure=bool(item.get("secure")),
                )
            )
        return out


def cookie_header(cookies: list[Cookie], *, domain_hint: str = "") -> str:
    """把 cookie 列表拼成 `a=1; b=2` 的请求头形态。

    ``domain_hint`` 给了就只保留域名相关的 cookie（例如 ``seewo.com``）。
    """
    picked = [
        c for c in cookies
        if c.name and (not domain_hint or domain_hint in c.domain)
    ]
    # 同名 cookie 以更具体域名为准（.seewo.com 与 pinco.seewo.com 可能都有）
    best: dict[str, Cookie] = {}
    for c in picked:
        current = best.get(c.name)
        if current is None or len(c.domain) > len(current.domain):
            best[c.name] = c
    return "; ".join(f"{c.name}={c.value}" for c in best.values())
