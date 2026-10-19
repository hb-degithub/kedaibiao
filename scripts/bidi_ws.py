"""WebDriver BiDi 的极简 WebSocket 客户端（只用标准库）。

为什么要自己写：本机是 Linux，computer use 后端不可用（它要拉 Windows 的
powershell.exe，直接 COMPUTER_USE_PAUSED）；而 Firefox 的 Remote Agent 只开了
WebDriver BiDi，没有 CDP 那套 /json/* HTTP 端点。BiDi 就是 WebSocket 上的
JSON-RPC，手写一百行就够驱动一个真实浏览器。

它解决的问题：登录某个平台后，在**页面自己的 JS 上下文**里 fetch 它的接口 ——
cookie、csrf token、x-token 全都天然正确，绕开「纯 HTTP 复刻会话」这类坑
（品课就是这样：SSO 能用协议复刻，应用层 csrf 复刻不了）。

用法::

    r = RPC("ws://127.0.0.1:9222/session")
    r.call("session.new", {"capabilities": {}})
    ctx = r.call("browsingContext.create", {"type": "tab"})["context"]
    r.call("browsingContext.navigate", {"context": ctx, "url": url, "wait": "complete"})
    out = r.call("script.evaluate", {"expression": "document.title",
                                     "target": {"context": ctx},
                                     "awaitPromise": False,
                                     "resultOwnership": "root"})

前置：以 ``firefox --headless --remote-debugging-port=9222`` 启动浏览器。
"""

import base64, json, os, socket, ssl, struct
from urllib.parse import urlsplit

class WS:
    def __init__(self, url: str, timeout: float = 30.0):
        u = urlsplit(url)
        self.host = u.hostname; self.port = u.port or (443 if u.scheme == "wss" else 80)
        self.path = u.path or "/"
        if u.query: self.path += "?" + u.query
        raw = socket.create_connection((self.host, self.port), timeout=timeout)
        if u.scheme == "wss":
            raw = ssl.create_default_context().wrap_socket(raw, server_hostname=self.host)
        self.sock = raw; self.sock.settimeout(timeout); self.buf = b""
        key = base64.b64encode(os.urandom(16)).decode()
        req = (f"GET {self.path} HTTP/1.1\r\nHost: {self.host}:{self.port}\r\n"
               "Upgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        self.sock.sendall(req.encode())
        while b"\r\n\r\n" not in self.buf:
            chunk = self.sock.recv(4096)
            if not chunk: raise ConnectionError("握手时连接被关闭")
            self.buf += chunk
        head, self.buf = self.buf.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n")[0]: raise ConnectionError("握手失败: " + head[:200].decode("latin1"))

    # -- 帧 --
    def _recv_exact(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk: raise ConnectionError("连接已关闭")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def send(self, text: str) -> None:
        data = text.encode(); n = len(data); mask = os.urandom(4)
        header = bytearray([0x81])
        if n < 126: header.append(0x80 | n)
        elif n < 65536: header.append(0x80 | 126); header += struct.pack(">H", n)
        else: header.append(0x80 | 127); header += struct.pack(">Q", n)
        header += mask
        self.sock.sendall(bytes(header) + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def recv(self) -> str | None:
        """收一条完整文本消息；控制帧就地处理。"""
        parts: list[bytes] = []
        while True:
            b0, b1 = self._recv_exact(2)
            fin, opcode = b0 & 0x80, b0 & 0x0F
            masked, length = b1 & 0x80, b1 & 0x7F
            if length == 126: length = struct.unpack(">H", self._recv_exact(2))[0]
            elif length == 127: length = struct.unpack(">Q", self._recv_exact(8))[0]
            key = self._recv_exact(4) if masked else b""
            payload = self._recv_exact(length)
            if masked: payload = bytes(c ^ key[i % 4] for i, c in enumerate(payload))
            if opcode == 0x8: return None                      # close
            if opcode == 0x9:                                  # ping -> pong
                self.sock.sendall(bytes([0x8A, len(payload)]) + payload); continue
            if opcode == 0xA: continue
            parts.append(payload)
            if fin: return b"".join(parts).decode("utf-8", "replace")

class RPC:
    """JSON-RPC over WS：按 id 配对响应，事件先丢进队列。"""
    def __init__(self, url: str, timeout: float = 30.0):
        self.ws = WS(url, timeout); self._id = 0; self.events: list[dict] = []
    def call(self, method: str, params: dict | None = None, timeout: float = 30.0):
        self._id += 1; mid = self._id
        self.ws.sock.settimeout(timeout)
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        while True:
            raw = self.ws.recv()
            if raw is None: raise ConnectionError("会话被关闭")
            msg = json.loads(raw)
            if msg.get("id") == mid:
                if "error" in msg: raise RuntimeError(f"{method} 失败: {msg['error']}")
                return msg.get("result")
            if "method" in msg: self.events.append(msg)
    def close(self):
        try: self.ws.sock.close()
        except OSError: pass
