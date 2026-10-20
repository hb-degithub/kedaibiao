"""U校园「学习时长上报」：engine.io v3 over WSS，**默认干跑**。

## 这份模块的定位

参考实现 `uai-core` 的 `src/api/duration.rs`（commit `4628e44`）里有一条
学习时长链路；本模块用纯标准库（``socket``/``ssl``/``struct``/``hashlib``）
把它重写成 Python 版本，**不依赖 adapter、不依赖任何第三方库**，方便 Lead
在 ``UnipusSession`` 上接线，也方便离线单测。

## 协议事实（来自 `docs/research/uai-core-submit-facts.md` §10，只读源码整理）

- **URL**：``wss://ucontent.unipus.cn/unipusiopoint/``，query 固定
  ``EIO=3&transport=websocket&uuid=<open_id>&token=<annotator token>``；
  ``uuid`` 是会话的 ``open_id``，``token`` 是**自铸 annotator token**（不是
  门户 JWT）。编码只保留 ``A-Za-z0-9-_.~``，其余 ``%XX``（本模块用
  ``urllib.parse.quote(safe="-_.~")``，与参考实现等价）。
- **帧序列**（全是 engine.io v3 文本帧，无二进制）：
  1. 服务端先发 ``0{...}`` open 帧；
  2. 客户端发 ``40/userActivities,``（**末尾有逗号**）入命名空间；
  3. 收到以 ``40/userActivities`` 开头的入房回执后，**每条连接只发一次**
     ``42/userActivities,1`` + kick 载荷（重发会被服务端回「任务没有信息
     变更」并停表）；
  4. 每 **20 秒**（首拍跳过，首次约 20s 后）发 ``2``；收到服务端 ``2`` 回
     ``3``；收到 ``3`` 忽略。
- **kick 载荷**：``["kick-v2",{...}]``，字段只有 module、moduleGroup、
  client（固定 ``"U校园mobile"``）、url（打点页）、tag1/tag2（同
  module/moduleGroup）、tag3（字符串化 JSON
  ``{"microBlock":"<module>/<group>","source":"cloud","version":"1"}``）、
  appId=116、serviceId="point"、sendTimestamp(ms)、openId、timer(同一 ms)。
  载荷里**没有课程/分组 ID**：这是账号级 ``(module, moduleGroup)`` 记账，
  无法定位到某本教材（源码事实，也是本模块的显式限制）。
- **时长口径**：客户端**从不发送 duration/秒数**。时长靠维持连接 + kick 一次，
  再由服务端 ``r_set`` 广播里的 ``delta``（按秒）累加；``msUpdate`` 用于去重。
  心跳本身不产生时长；不存在「POST 一条带 duration 的消息」这种接口。
- 参考实现**没有 token/uuid 刷新**，重连是固定 5 秒无限次；本模块**不实现
  重连循环**（失败软着陆，由调用方决定是否重试）。

## 证据边界（如实写）

- 以上报文形状是**源码事实**（与参考实现逐字一致），但本模块**没有向
  ``*.unipus.cn`` 发过任何真实请求**，所以「服务端今天是否仍接受」属**未实测**。
- ``r_set`` 广播的**信封形状**与 ``delta`` 单位（源码按秒累加）属**推断**；
  本模块只做**容错观测**（``credited_seconds``），不拿它决定循环是否结束。
- 参考实现的「时长加速」是多房间 boost 池（10 module × 6 group，最高 60 房间）；
  本模块**只支持单房间、一次 kick**，不做倍率/多房间，也不做断线重连。
- kick 只是**冒充 mobile 协议**（参考实现注释称桌面端名字记账为 0）：这是
  参考实现的自述，本模块未独立核对。合规风险（平台 ToS / 校内规范）请自行评估。

## 安全与纪律

- ``dry_run``（默认）：不建立任何连接，只返回「将要上报什么」的计划；
  只有 ``confirm=True`` 才真的建连。
- ``enable_kick=False``（默认）：即便 ``confirm=True`` 也只连接 + 入房 + 心跳，
  **不发送 kick**（源码事实显示不 kick 不记账，所以默认不替调用方做这个决定）。
  要真正尝试记账必须显式 ``enable_kick=True`` 并给出 ``module``/``module_group``。
- ``business_frames`` 是通用逃生口（例如以后核对出别的帧），必须显式
  ``frames_verified=True`` 才发送，否则抛 :class:`DurationGateError`。
- 日志与计划里对 token 打码，任何摘要都不含凭据。
- 只认 ``wss://ucontent.unipus.cn/unipusiopoint/``，否则直接拒绝 ——
  防止把带 token 的 URL 发错主机（规格 §1 记载过这类事故）。
- 业务路径不 ``sleep``；只有心跳节拍会等待，且 ``clock``/``sleeper`` 均可注入，
  单测用虚拟时钟跑完整会话，不产生真实等待。
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import logging
import math
import secrets
import socket
import ssl
import struct
import time
import urllib.parse
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Protocol

from kedaibiao.platforms.unipus import endpoints
from kedaibiao.platforms.unipus.endpoints import UnipusApp
from kedaibiao.platforms.unipus.errors import UnipusError

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# 协议常量
# --------------------------------------------------------------------------

#: 学习时长 WS 的主机（规格 §1/§4.4；不认别的域名）
EXPECTED_WS_HOST: Final[str] = "ucontent.unipus.cn"
#: 路径，末尾的 ``/`` 是 URL 的一部分（§4.4 逐字给出）
DURATION_PATH: Final[str] = "/unipusiopoint/"

#: Socket.IO 命名空间（duration.rs 常量，facts §10.1）
NAMESPACE: Final[str] = "/userActivities"
#: 入房后要发的事件名（facts §10.3）
KICK_EVENT: Final[str] = "kick-v2"
#: kick 载荷里的 client：参考实现称桌面端名字记账为 0，必须冒充 mobile（自述，未独立核对）
KICK_CLIENT: Final[str] = "U校园mobile"
#: kick 载荷里的打点页 URL（duration.rs 常量，facts §10.1/§10.3）
KICK_PAGE_URL: Final[str] = "https://ucontent.unipus.cn/_explorationpc_default/pc.html"
#: kick 载荷里的 appId / serviceId（facts §10.3）
KICK_APP_ID: Final[int] = 116
KICK_SERVICE_ID: Final[str] = "point"

#: 参考实现的断线重连间隔与状态汇报间隔（**仅登记**；本模块不实现重连循环）
RECONNECT_DELAY_SECONDS: Final[float] = 5.0
STATUS_REPORT_INTERVAL_SECONDS: Final[float] = 5.0

#: engine.io v3 的包类型（首字符）
ENGINE_OPEN: Final[str] = "0"
ENGINE_CLOSE: Final[str] = "1"
ENGINE_PING: Final[str] = "2"
ENGINE_PONG: Final[str] = "3"
ENGINE_MESSAGE: Final[str] = "4"
ENGINE_UPGRADE: Final[str] = "5"
ENGINE_NOOP: Final[str] = "6"
ENGINE_PACKET_TYPES: Final[frozenset[str]] = frozenset(
    {ENGINE_OPEN, ENGINE_CLOSE, ENGINE_PING, ENGINE_PONG,
     ENGINE_MESSAGE, ENGINE_UPGRADE, ENGINE_NOOP}
)

#: 规格 §3.5：客户端每 20 秒主动发一次 ping（本模块默认值，可显式覆盖）
DEFAULT_PING_INTERVAL: Final[float] = 20.0
#: 单次 WS 操作（握手 / 等 pong）的超时
DEFAULT_TIMEOUT: Final[float] = 10.0
#: 单次会话允许的最长时长（防误传一个巨大值把进程挂住）
MAX_DURATION_SECONDS: Final[float] = 8 * 3600.0
#: 连续多少次 ping 收不到 pong 就终止本轮（软着陆，不抛给调用方）
MAX_MISSED_PONGS: Final[int] = 2
#: 等一个 pong 时最多绕过多少个其它帧（业务帧/服务端 ping）
MAX_FRAMES_PER_PONG: Final[int] = 16
#: 等 namespace 入房回执时最多绕过多少个其它帧
MAX_FRAMES_FOR_ACK: Final[int] = 16
#: 报告里最多保留多少条收到的帧（只用于排查，不参与业务）
MAX_RECORDED_FRAMES: Final[int] = 32

#: RFC 6455 的固定 GUID（Sec-WebSocket-Accept 计算用）
WS_GUID: Final[str] = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
#: RFC 6455 opcode
WS_OPCODE_TEXT: Final[int] = 0x1
WS_OPCODE_BINARY: Final[int] = 0x2
WS_OPCODE_CLOSE: Final[int] = 0x8
WS_OPCODE_PING: Final[int] = 0x9
WS_OPCODE_PONG: Final[int] = 0xA

#: 握手响应头的大小上限（防止对端一直不发 ``\r\n\r\n``）
MAX_HANDSHAKE_BYTES: Final[int] = 64 * 1024
#: 单个 WebSocket 帧的载荷上限（防止对端声明一个天文长度把读循环挂住）
MAX_FRAME_BYTES: Final[int] = 1024 * 1024


# --------------------------------------------------------------------------
# 异常
# --------------------------------------------------------------------------


class DurationError(UnipusError):
    """学习时长上报的基类异常。"""


class DurationConfigError(DurationError, ValueError):
    """参数不合法（URL/时长/间隔/超时）。"""


class DurationProtocolError(DurationError):
    """协议不符（握手失败、首帧不是 open、pong 连续丢失等）。"""


class DurationGateError(DurationError):
    """未验证的报文细节没有显式放行 —— 属于**配置错误**，不能软着陆。"""


class DurationTransportError(DurationError):
    """传输层错误（WS 关闭、分片帧等）。"""


# --------------------------------------------------------------------------
# 类型别名与传输契约
# --------------------------------------------------------------------------


Clock = Callable[[], float]
Sleeper = Callable[[float], None]


class DurationTransport(Protocol):
    """时长会话所需的传输契约；换成假实现即可完全离线测试。"""

    def connect(self, url: str) -> None: ...

    def send(self, text: str) -> None: ...

    def recv(self) -> str: ...

    def close(self) -> None: ...


TransportFactory = Callable[[], DurationTransport]


# --------------------------------------------------------------------------
# engine.io v3 文本帧编解码（纯函数）
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EnginePacket:
    """一个 engine.io 文本包：``type`` 是首字符，``data`` 是余下部分。"""

    type: str
    data: str = ""


def encode_packet(packet_type: str, data: str = "") -> str:
    """编码 engine.io 文本包（例如 ``encode_packet(ENGINE_PING) == "2"``）。"""
    if packet_type not in ENGINE_PACKET_TYPES:
        raise DurationConfigError(f"不是 engine.io v3 的包类型: {packet_type!r}")
    return packet_type + data


def decode_packet(text: str) -> EnginePacket | None:
    """解码 engine.io 文本包；空串/未知类型返回 ``None``（调用方决定怎么处理）。"""
    if not isinstance(text, str) or not text:
        return None
    packet_type = text[0]
    if packet_type not in ENGINE_PACKET_TYPES:
        return None
    return EnginePacket(packet_type, text[1:])


# --------------------------------------------------------------------------
# RFC 6455 最小 WebSocket 客户端（只做文本帧，不分片、不协商扩展）
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WsFrame:
    """一个完整的 WebSocket 帧。``size`` 是它在流里占用的字节数。"""

    opcode: int
    payload: bytes
    size: int


def apply_mask(payload: bytes, mask_key: bytes) -> bytes:
    """按 RFC 6455 用 4 字节掩码异或（加掩与去掩是同一运算）。"""
    if len(mask_key) != 4:
        raise DurationConfigError("WebSocket 掩码必须是 4 字节")
    return bytes(byte ^ mask_key[index % 4] for index, byte in enumerate(payload))


def encode_frame(opcode: int, payload: bytes, *, mask_key: bytes | None = None) -> bytes:
    """编码一个**客户端**帧（RFC 6455 要求客户端必须加掩码）。

    ``mask_key`` 仅供单测固定输出用；正常调用不传，模块会用 ``secrets`` 随机生成。
    """
    if not 0 <= opcode <= 0x0F:
        raise DurationConfigError(f"非法 opcode: {opcode}")
    if mask_key is None:
        mask_key = secrets.token_bytes(4)
    header = bytearray([0x80 | opcode])  # FIN + opcode，不支持分片
    length = len(payload)
    if length < 126:
        header.append(0x80 | length)
    elif length <= 0xFFFF:
        header.append(0x80 | 126)
        header += struct.pack("!H", length)
    else:
        header.append(0x80 | 127)
        header += struct.pack("!Q", length)
    header += mask_key
    return bytes(header) + apply_mask(payload, mask_key)


def decode_frame(buffer: bytes) -> WsFrame | None:
    """从字节缓冲里解析一个完整帧；数据不完整返回 ``None``。

    只处理服务端帧（不加掩码）与带掩码帧（兼容测试），不支持分片与扩展。
    """
    if len(buffer) < 2:
        return None
    first, second = buffer[0], buffer[1]
    if first & 0x70:
        raise DurationProtocolError("收到带 RSV 位的帧：本模块不协商扩展")
    opcode = first & 0x0F
    if opcode == 0x0:
        raise DurationProtocolError("收到分片帧：本模块不支持分片")
    masked = bool(second & 0x80)
    length = second & 0x7F
    offset = 2
    if length == 126:
        if len(buffer) < offset + 2:
            return None
        length = struct.unpack("!H", buffer[offset:offset + 2])[0]
        offset += 2
    elif length == 127:
        if len(buffer) < offset + 8:
            return None
        length = struct.unpack("!Q", buffer[offset:offset + 8])[0]
        offset += 8
    if length > MAX_FRAME_BYTES:
        raise DurationProtocolError(f"WebSocket 帧载荷过大: {length} 字节")
    mask_key = b""
    if masked:
        if len(buffer) < offset + 4:
            return None
        mask_key = buffer[offset:offset + 4]
        offset += 4
    if len(buffer) < offset + length:
        return None
    payload = buffer[offset:offset + length]
    if masked:
        payload = apply_mask(payload, mask_key)
    return WsFrame(opcode, payload, offset + length)


def _recv_exact(sock: Any, count: int) -> bytes:
    """从 socket 读满 ``count`` 字节；对端关闭则抛 :class:`DurationTransportError`。"""
    buffer = b""
    while len(buffer) < count:
        chunk = sock.recv(count - len(buffer))
        if not chunk:
            raise DurationTransportError("连接在读取 WebSocket 帧时被关闭")
        buffer += chunk
    return buffer


def read_frame(sock: Any) -> WsFrame:
    """从 socket 上读一个完整 WebSocket 帧。"""
    header = _recv_exact(sock, 2)
    first, second = header[0], header[1]
    if first & 0x70:
        raise DurationProtocolError("收到带 RSV 位的帧：本模块不协商扩展")
    opcode = first & 0x0F
    if opcode == 0x0:
        raise DurationProtocolError("收到分片帧：本模块不支持分片")
    masked = bool(second & 0x80)
    length = second & 0x7F
    if length == 126:
        length = struct.unpack("!H", _recv_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", _recv_exact(sock, 8))[0]
    if length > MAX_FRAME_BYTES:
        raise DurationProtocolError(f"WebSocket 帧载荷过大: {length} 字节")
    mask_key = _recv_exact(sock, 4) if masked else b""
    payload = _recv_exact(sock, length)
    if masked:
        payload = apply_mask(payload, mask_key)
    return WsFrame(opcode, payload, 2 + (4 if masked else 0) + length)


def build_handshake_request(
    host: str,
    port: int,
    path: str,
    key: str,
    *,
    origin: str | None = None,
) -> bytes:
    """构造 RFC 6455 的 HTTP Upgrade 请求（``path`` 已含 query）。"""
    host_header = host if port in (80, 443) else f"{host}:{port}"
    lines = [
        f"GET {path} HTTP/1.1",
        f"Host: {host_header}",
        "Upgrade: websocket",
        "Connection: Upgrade",
        f"Sec-WebSocket-Key: {key}",
        "Sec-WebSocket-Version: 13",
    ]
    if origin is not None:
        lines.append(f"Origin: {origin}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii")


def expected_accept(key: str) -> str:
    """按 RFC 6455 计算 ``Sec-WebSocket-Accept``。"""
    digest = hashlib.sha1((key + WS_GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def perform_handshake(
    sock: Any,
    key: str,
    *,
    host: str,
    port: int,
    path: str,
    origin: str | None = None,
) -> None:
    """发送 Upgrade 请求并校验 101 与 ``Sec-WebSocket-Accept``（RFC 要求校验）。"""
    sock.sendall(build_handshake_request(host, port, path, key, origin=origin))
    response = _read_until_headers(sock)
    head, _, _ = response.partition(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    status_line = lines[0].decode("latin-1", errors="replace")
    if " 101 " not in status_line:
        raise DurationProtocolError(f"WebSocket 握手失败: {status_line[:120]}")
    headers = _parse_headers(lines[1:])
    accept = headers.get("sec-websocket-accept", "")
    if accept != expected_accept(key):
        raise DurationProtocolError(
            f"WebSocket 握手校验失败：Sec-WebSocket-Accept={accept!r} 与期望不符"
        )


def _read_until_headers(source: Any) -> bytes:
    """读到响应头结束标记；多读到的字节 push 回去，不能丢（服务端可能紧跟数据帧）。"""
    buffer = b""
    while b"\r\n\r\n" not in buffer:
        chunk = source.recv(4096)
        if not chunk:
            raise DurationTransportError("连接在 HTTP 握手阶段被关闭")
        buffer += chunk
        if len(buffer) > MAX_HANDSHAKE_BYTES:
            raise DurationProtocolError("WebSocket 握手响应头超长")
    head, marker, rest = buffer.partition(b"\r\n\r\n")
    if rest and hasattr(source, "push_back"):
        source.push_back(rest)
    return head + marker


def _parse_headers(lines: list[bytes]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for line in lines:
        name, sep, value = line.partition(b":")
        if not sep:
            continue
        headers[name.strip().lower().decode("latin-1")] = value.strip().decode("latin-1")
    return headers


def _default_tls_wrap(sock: Any, host: str, timeout: float) -> Any:
    context = ssl.create_default_context()
    wrapped = context.wrap_socket(sock, server_hostname=host)
    wrapped.settimeout(timeout)
    return wrapped


class _BufferedSocket:
    """在 socket 外包一层缓冲：握手过读的字节留在这里，喂给后续的帧解析。"""

    def __init__(self, sock: Any) -> None:
        self._sock = sock
        self._buffer = b""

    def push_back(self, data: bytes) -> None:
        self._buffer = data + self._buffer

    def recv(self, count: int) -> bytes:
        if self._buffer:
            chunk, self._buffer = self._buffer[:count], self._buffer[count:]
            return chunk
        return self._sock.recv(count)

    def sendall(self, data: bytes) -> None:
        self._sock.sendall(data)

    def settimeout(self, value: float) -> None:
        self._sock.settimeout(value)

    def close(self) -> None:
        self._sock.close()


class SocketDurationTransport:
    """真实 WSS 传输（唯一会建连接的实现；离线单测不实例化它）。

    ``socket_factory`` / ``tls_wrapper`` 可注入，方便在**不联网**的前提下测握手与
    帧收发；业务代码不应该替换它们。
    """

    def __init__(
        self,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        origin: str | None = None,
        socket_factory: Callable[[tuple[str, int], float], Any] | None = None,
        tls_wrapper: Callable[[Any, str, float], Any] | None = None,
    ) -> None:
        if timeout <= 0:
            raise DurationConfigError(f"超时必须为正数: {timeout}")
        self.timeout = timeout
        self.origin = origin
        self._socket_factory = socket_factory or socket.create_connection
        self._tls_wrapper = tls_wrapper or _default_tls_wrap
        self._sock: _BufferedSocket | None = None

    def connect(self, url: str) -> None:
        # 发请求前的最后一道域名/路径/协议断言（规格 §1 的域名纪律）
        assert_duration_url(url)
        parts = urllib.parse.urlsplit(url)
        port = parts.port or 443
        path = parts.path or "/"
        if parts.query:
            path = f"{path}?{parts.query}"
        sock = self._socket_factory((parts.hostname, port), self.timeout)
        sock.settimeout(self.timeout)
        if parts.scheme == "wss":
            sock = self._tls_wrapper(sock, parts.hostname, self.timeout)
        reader = _BufferedSocket(sock)
        key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
        try:
            perform_handshake(
                reader, key, host=parts.hostname, port=port, path=path, origin=self.origin
            )
        except Exception:
            with contextlib.suppress(Exception):
                sock.close()
            raise
        self._sock = reader

    def send(self, text: str) -> None:
        if self._sock is None:
            raise DurationTransportError("WebSocket 尚未连接")
        self._sock.sendall(encode_frame(WS_OPCODE_TEXT, text.encode("utf-8")))

    def recv(self) -> str:
        if self._sock is None:
            raise DurationTransportError("WebSocket 尚未连接")
        while True:
            frame = read_frame(self._sock)
            if frame.opcode in (WS_OPCODE_TEXT, WS_OPCODE_BINARY):
                return frame.payload.decode("utf-8", errors="replace")
            if frame.opcode == WS_OPCODE_PING:
                self._sock.sendall(encode_frame(WS_OPCODE_PONG, frame.payload))
                continue
            if frame.opcode == WS_OPCODE_CLOSE:
                with contextlib.suppress(Exception):
                    self._sock.sendall(encode_frame(WS_OPCODE_CLOSE, b""))
                raise DurationTransportError("对端关闭了 WebSocket")
            # WS 层 pong 等：与 engine.io 无关，忽略后继续读
            log.debug("忽略 WebSocket opcode=0x%X 的帧", frame.opcode)

    def close(self) -> None:
        sock, self._sock = self._sock, None
        if sock is None:
            return
        with contextlib.suppress(Exception):
            sock.sendall(encode_frame(WS_OPCODE_CLOSE, b""))
        with contextlib.suppress(Exception):
            sock.close()


# --------------------------------------------------------------------------
# URL 构造与打码
# --------------------------------------------------------------------------


def _ws_base() -> str:
    """从 ``endpoints`` 的 ucontent 基址推导 WS 基址（保持「单一出处」）。"""
    http_base = endpoints.BASE_URLS[UnipusApp.UCONTENT]
    parts = urllib.parse.urlsplit(http_base)
    if parts.scheme != "https":
        raise DurationProtocolError(f"ucontent 基址不是 https：{http_base}")
    return f"wss://{parts.netloc}"


def build_duration_url(open_id: str, token: str) -> str:
    """拼出学习时长的 WSS URL；空参数或错误域名直接拒绝。"""
    if not isinstance(open_id, str) or not open_id:
        raise DurationConfigError("open_id 不能为空")
    if not isinstance(token, str) or not token:
        raise DurationConfigError("token 不能为空")
    query = urllib.parse.urlencode(
        {"EIO": "3", "transport": "websocket", "uuid": open_id, "token": token},
        quote_via=urllib.parse.quote,
        safe="-_.~",
    )
    url = f"{_ws_base()}{DURATION_PATH}?{query}"
    assert_duration_url(url)
    return url


def assert_duration_url(url: str) -> None:
    """发请求前的域名/路径断言（规格 §1 的历史事故就是发错主机）。

    ⚠️ 允许**两条**路径：房间池的 ``/unipusiopoint/`` 与任务模式的 ``/unipusio/``
    （后者由 :func:`assert_task_duration_url` 单独再收紧）。早期这里只认前者，
    结果任务模式真跑时被自己拦成「学习时长路径不符」，一次 start 都没发出去。
    """
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "wss":
        raise DurationProtocolError(f"学习时长必须走 wss：{url}")
    if parts.hostname != EXPECTED_WS_HOST:
        raise DurationProtocolError(
            f"学习时长只允许 {EXPECTED_WS_HOST}，实际 {parts.hostname}"
        )
    allowed = (DURATION_PATH, TASK_DURATION_PATH)
    if not any(parts.path.startswith(path) for path in allowed):
        raise DurationProtocolError(
            f"学习时长路径不符：{parts.path}（只允许 {' / '.join(allowed)}）"
        )


def redact_duration_url(url: str) -> str:
    """把 ``token`` 的值打码，其余原样（用于计划/日志/报告）。"""
    parts = urllib.parse.urlsplit(url)
    pairs = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    masked = [
        (name, "REDACTED" if name.lower() in {"token"} else value) for name, value in pairs
    ]
    query = urllib.parse.urlencode(masked, quote_via=urllib.parse.quote, safe="-_.~")
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


# --------------------------------------------------------------------------
# Socket.IO 入房 / kick 报文（形状来自 facts §10.3，未实测）
# --------------------------------------------------------------------------


def build_join_frame(namespace: str = NAMESPACE) -> str:
    """入命名空间帧：``40/userActivities,``（末尾那个逗号是协议的一部分）。"""
    if not isinstance(namespace, str) or not namespace.startswith("/"):
        raise DurationConfigError(f"Socket.IO 命名空间必须以 / 开头: {namespace!r}")
    return f"40{namespace},"


def build_kick_payload(
    open_id: str,
    module: str,
    module_group: str,
    *,
    now_ms: int,
    client: str = KICK_CLIENT,
    page_url: str = KICK_PAGE_URL,
    app_id: int = KICK_APP_ID,
    service_id: str = KICK_SERVICE_ID,
) -> dict[str, object]:
    """构造 ``kick-v2`` 的第二个元素（对象）；字段名与取值来自 facts §10.3。

    ``tag3`` 是**字符串化的 JSON**（参考实现如此），键按字母序；``sendTimestamp``
    与 ``timer`` 用同一个毫秒时间戳。``module``/``module_group`` 没有默认值：
    参考实现是从房间池里取的，选哪一组属于调用方的决定。
    """
    if not open_id:
        raise DurationConfigError("open_id 不能为空")
    if not module or not module_group:
        raise DurationConfigError("kick 需要显式给出 module 与 module_group")
    if not isinstance(now_ms, int) or now_ms <= 0:
        raise DurationConfigError(f"now_ms 必须是正的毫秒时间戳: {now_ms!r}")
    tag3 = json.dumps(
        {"microBlock": f"{module}/{module_group}", "source": "cloud", "version": "1"},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return {
        "module": module,
        "moduleGroup": module_group,
        "client": client,
        "url": page_url,
        "tag1": module,
        "tag2": module_group,
        "tag3": tag3,
        "appId": app_id,
        "serviceId": service_id,
        "sendTimestamp": now_ms,
        "openId": open_id,
        "timer": now_ms,
    }


def build_kick_frame(
    open_id: str,
    module: str,
    module_group: str,
    *,
    now_ms: int,
    namespace: str = NAMESPACE,
    client: str = KICK_CLIENT,
    page_url: str = KICK_PAGE_URL,
    app_id: int = KICK_APP_ID,
    service_id: str = KICK_SERVICE_ID,
) -> str:
    """拼出逐字的 kick 帧：``42{namespace},1`` + 紧凑 JSON 数组。

    JSON 键按字母序输出（模仿参考实现的 serde_json 默认行为），``kick`` 每条
    连接只发一次 —— 重发会被服务端认为「任务没有信息变更」并停表。
    """
    if not isinstance(namespace, str) or not namespace.startswith("/"):
        raise DurationConfigError(f"Socket.IO 命名空间必须以 / 开头: {namespace!r}")
    payload = build_kick_payload(
        open_id,
        module,
        module_group,
        now_ms=now_ms,
        client=client,
        page_url=page_url,
        app_id=app_id,
        service_id=service_id,
    )
    body = json.dumps(
        [KICK_EVENT, payload], ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return f"42{namespace},1{body}"


# --------------------------------------------------------------------------
# 服务端 r_set 广播解析（容错观测；信封形状属推断）
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RSetUpdate:
    """一条 ``r_set`` 广播里的记账增量。``delta`` 单位为秒（源码用法推断）。"""

    delta: float
    ms_update: int


def parse_r_set(text: str) -> RSetUpdate | None:
    """从文本帧里容错地抠出 ``{delta, msUpdate}``；不是 r_set 返回 ``None``。

    参考实现直接按某个确定的 JSON 路径取；本模块没有实测信封，所以退化为
    「找到第一个同时有 ``delta`` 与 ``msUpdate`` 的对象」。**只用于观测**，
    不参与「是否停止」的判定。
    """
    packet = decode_packet(text)
    if packet is None or packet.type != ENGINE_MESSAGE:
        return None
    body = packet.data
    starts = [index for index in (body.find("["), body.find("{")) if index >= 0]
    if not starts:
        return None
    try:
        decoded = json.loads(body[min(starts):])
    except (json.JSONDecodeError, ValueError):
        return None
    hit = _find_r_set_object(decoded)
    if hit is None:
        return None
    delta, ms_update = hit
    return RSetUpdate(delta, ms_update)


def _find_r_set_object(value: Any) -> tuple[float, int] | None:
    if isinstance(value, dict):
        if "delta" in value and "msUpdate" in value:
            delta = value["delta"]
            ms_update = value["msUpdate"]
            if (
                isinstance(delta, (int, float))
                and not isinstance(delta, bool)
                and isinstance(ms_update, (int, float))
                and not isinstance(ms_update, bool)
            ):
                return float(delta), int(ms_update)
        children = value.values()
    elif isinstance(value, list):
        children = value
    else:
        return None
    for child in children:
        hit = _find_r_set_object(child)
        if hit is not None:
            return hit
    return None


# --------------------------------------------------------------------------
# 计划与报告
# --------------------------------------------------------------------------


@dataclass(slots=True)
class DurationPlan:
    """干跑时返回的「将要上报什么」。

    ``planned_pings`` 是**估算值**：按「每 ``ping_interval`` 秒一次、只统计严格
    早于截止时刻的 ping」推出来的，实际次数会受真实调度抖动影响。
    """

    url: str
    host: str
    requested_seconds: float
    ping_interval: float
    planned_pings: int
    namespace: str = NAMESPACE
    join_namespace: bool = True
    kick_enabled: bool = False
    module: str = ""
    module_group: str = ""
    business_frames: tuple[str, ...] = ()
    heartbeat_only: bool = True
    warnings: list[str] = field(default_factory=list)

    def mode(self) -> str:
        if self.kick_enabled:
            return f"入房 + 单次 kick（module={self.module}, group={self.module_group}）"
        if self.join_namespace:
            return "仅入房 + 心跳（不 kick，不会产生记账）"
        return "仅连接 + 心跳（不入房、不 kick）"

    def summary(self) -> str:
        frames = "、".join(_preview(frame) for frame in self.business_frames) or "无"
        return (
            f"目标 {self.url}（时长 {self.requested_seconds:g}s，"
            f"心跳间隔 {self.ping_interval:g}s，预计 {self.planned_pings} 次 ping；"
            f"模式：{self.mode()}；额外业务帧：{frames}）"
        )


@dataclass(slots=True)
class DurationReport:
    """一次时长上报（或干跑）的结果。

    ⚠️ ``ok`` 只说明「计划成立 / 连接与心跳正常结束」，**不说明平台真的记了账**：
    协议没有业务成功码可判定（见模块 docstring 的未验证项）。
    """

    plan: DurationPlan
    dry_run: bool
    connected: bool = False
    open_frame: str = ""
    join_frame_sent: bool = False
    namespace_acked: bool = False
    kick_sent: bool = False
    kick_frame_sent: str = ""
    pings_sent: int = 0
    pongs_received: int = 0
    pongs_missed: int = 0
    elapsed_seconds: float = 0.0
    credited_seconds: float = 0.0
    r_set_updates: int = 0
    business_frames_sent: list[str] = field(default_factory=list)
    received_frames: list[str] = field(default_factory=list)
    error: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """干跑成功、或真跑没有错误，都算 ok（不代表平台已记账）。"""
        return not self.error

    def summary(self) -> str:
        mode = "干跑（未连接）" if self.dry_run else ("已连接" if self.connected else "未连接")
        if self.error:
            return f"{mode}：{self.plan.summary()} —— 失败：{self.error}"
        extra = (
            f"ping {self.pings_sent} 次 / pong {self.pongs_received} 次"
            f" / 实际 {self.elapsed_seconds:g}s"
        )
        if self.pongs_missed:
            extra += f" / 丢失 pong {self.pongs_missed} 次"
        if not self.dry_run:
            extra += f" / kick {'已发' if self.kick_sent else '未发'}"
            if self.r_set_updates:
                extra += f" / 观测记账 {self.credited_seconds:g}s（{self.r_set_updates} 条广播）"
        if self.dry_run:
            extra = f"预计 ping {self.plan.planned_pings} 次"
        return f"{mode}：{self.plan.summary()} —— {extra}"


def _preview(text: str, limit: int = 120) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


def _system_clock_ms() -> int:
    """默认毫秒时间源（kick 的 sendTimestamp/timer 用）。"""
    return int(time.time() * 1000)


def _validate_seconds(seconds: float, max_seconds: float) -> float:
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        raise DurationConfigError(f"时长必须是数字: {seconds!r}")
    value = float(seconds)
    if not math.isfinite(value) or value <= 0:
        raise DurationConfigError(f"时长必须为正的有限数: {seconds!r}")
    if value > max_seconds:
        raise DurationConfigError(f"时长 {value:g}s 超过上限 {max_seconds:g}s")
    return value


def _planned_pings(seconds: float, ping_interval: float) -> int:
    """与心跳循环一致的估算：``ceil(seconds / interval) - 1``（严格早于截止时刻）。"""
    return max(0, math.ceil(seconds / ping_interval) - 1)


# --------------------------------------------------------------------------
# 上报器
# --------------------------------------------------------------------------


class SessionDurationReporter:
    """一次学习时长上报会话（默认干跑）。

    典型用法（Lead 接线时）：

    >>> reporter = SessionDurationReporter(session.open_id, annotator_token)
    >>> plan = reporter.plan(600)            # 干跑：只看计划，不建连接
    >>> report = reporter.run(600)           # 仍然干跑
    >>> report = reporter.run(600, confirm=True)   # 真连（入房 + 心跳 10 分钟）
    >>> report = reporter.run(600, confirm=True, enable_kick=True)  # 再发一次 kick

    ``token`` 必须是**自铸的 annotator token**（facts §10.2；不是门户 JWT）。
    本模块不读取凭据、不刷新 token。``enable_kick=False`` 是刻意默认值：
    源码事实显示「不 kick 不记账」，是否替用户做这个动作由调用方决定。
    """

    def __init__(
        self,
        open_id: str,
        token: str,
        *,
        ping_interval: float = DEFAULT_PING_INTERVAL,
        timeout: float = DEFAULT_TIMEOUT,
        max_seconds: float = MAX_DURATION_SECONDS,
        max_missed_pongs: int = MAX_MISSED_PONGS,
        namespace: str = NAMESPACE,
        join_namespace: bool = True,
        enable_kick: bool = False,
        module: str = "",
        module_group: str = "",
        kick_client: str = KICK_CLIENT,
        kick_page_url: str = KICK_PAGE_URL,
        business_frames: Sequence[str] = (),
        origin: str | None = None,
        transport_factory: TransportFactory | None = None,
        clock: Clock | None = None,
        sleeper: Sleeper | None = None,
        wall_clock_ms: Callable[[], int] | None = None,
    ) -> None:
        if not isinstance(ping_interval, (int, float)) or isinstance(ping_interval, bool):
            raise DurationConfigError(f"ping_interval 必须是数字: {ping_interval!r}")
        if not math.isfinite(float(ping_interval)) or float(ping_interval) <= 0:
            raise DurationConfigError(f"ping_interval 必须为正数: {ping_interval!r}")
        if timeout <= 0:
            raise DurationConfigError(f"timeout 必须为正数: {timeout}")
        if max_seconds <= 0:
            raise DurationConfigError(f"max_seconds 必须为正数: {max_seconds}")
        if max_missed_pongs < 1:
            raise DurationConfigError(f"max_missed_pongs 至少为 1: {max_missed_pongs}")
        for frame in business_frames:
            if not isinstance(frame, str) or not frame:
                raise DurationConfigError(f"业务帧必须是非空字符串: {frame!r}")
        build_join_frame(namespace)  # 校验命名空间格式
        if enable_kick:
            if not join_namespace:
                raise DurationConfigError("kick 必须在入房之后发送，不能同时 join_namespace=False")
            if not module or not module_group:
                raise DurationConfigError(
                    "enable_kick=True 时必须显式给出 module 与 module_group"
                    "（参考实现从房间池取值，本模块不替你猜）"
                )
        self.open_id = open_id
        self.ping_interval = float(ping_interval)
        self.timeout = float(timeout)
        self.max_seconds = float(max_seconds)
        self.max_missed_pongs = int(max_missed_pongs)
        self.namespace = namespace
        self.join_namespace = bool(join_namespace)
        self.enable_kick = bool(enable_kick)
        self.module = module
        self.module_group = module_group
        self.kick_client = kick_client
        self.kick_page_url = kick_page_url
        self.business_frames = tuple(business_frames)
        self.origin = origin
        self._url = build_duration_url(open_id, token)
        self._transport_factory = transport_factory
        self._clock: Clock = clock or time.monotonic
        self._sleeper: Sleeper = sleeper or time.sleep
        self._wall_clock_ms: Callable[[], int] = wall_clock_ms or _system_clock_ms
        #: 本轮已计入的 r_set 时间戳（去重用），每次 run 重置
        self._seen_r_set: set[int] = set()

    # -- 计划 -------------------------------------------------------------

    def plan(self, seconds: float) -> DurationPlan:
        """干跑：算出「将要上报什么」，**不建立任何连接**。"""
        requested = _validate_seconds(seconds, self.max_seconds)
        planned = _planned_pings(requested, self.ping_interval)
        warnings: list[str] = []
        if planned == 0:
            warnings.append(
                "会话短于一个心跳周期，期间不会发送任何 ping；服务端可能在心跳超时后断开"
            )
        if self.enable_kick:
            warnings.append(
                "kick-v2 报文形状来自源码事实但**未实测**；且这是账号级 "
                "(module, moduleGroup) 记账，无法定位到具体教材"
            )
        else:
            warnings.append(
                "未启用 kick：源码事实显示心跳本身不产生时长（需入房后 kick 一次），"
                "所以本次连接不会真正记账"
            )
        if not self.join_namespace:
            warnings.append("join_namespace=False：不会发送 namespace 入房帧")
        if self.business_frames:
            warnings.append(
                "已启用自定义业务帧：形状需调用方自行确认（frames_verified=True）"
            )
        return DurationPlan(
            url=redact_duration_url(self._url),
            host=EXPECTED_WS_HOST,
            requested_seconds=requested,
            ping_interval=self.ping_interval,
            planned_pings=planned,
            namespace=self.namespace,
            join_namespace=self.join_namespace,
            kick_enabled=self.enable_kick,
            module=self.module,
            module_group=self.module_group,
            business_frames=self.business_frames,
            heartbeat_only=not (self.enable_kick or self.business_frames),
            warnings=warnings,
        )

    # -- 执行 -------------------------------------------------------------

    def run(
        self,
        seconds: float,
        *,
        confirm: bool = False,
        frames_verified: bool = False,
        raise_on_error: bool = False,
    ) -> DurationReport:
        """执行上报。

        :param confirm: ``False``（默认）只干跑，**不建连接**；``True`` 才真连。
        :param frames_verified: 显式声明「我核对过 ``business_frames`` 的报文形状」；
            没传业务帧时无意义，传了业务帧却不置 ``True`` 会抛
            :class:`DurationGateError`（这是配置错误，不受 ``raise_on_error`` 影响）。
        :param raise_on_error: 真跑失败时是否抛出；默认软着陆，把错误写进报告。
        """
        plan = self.plan(seconds)
        if self.business_frames and not frames_verified:
            raise DurationGateError(
                "business_frames 是未验证的报文形状；确认来源后请显式传 frames_verified=True"
            )
        if not confirm:
            report = DurationReport(plan=plan, dry_run=True, warnings=list(plan.warnings))
            log.info("学习时长上报干跑（未建立任何连接）：%s", report.summary())
            return report

        report = DurationReport(plan=plan, dry_run=False, warnings=list(plan.warnings))
        self._seen_r_set = set()
        transport: DurationTransport | None = None
        try:
            factory = self._transport_factory or self._default_transport
            transport = factory()
            transport.connect(self._url)
            report.connected = True
            self._expect_open(transport, report)
            self._send_business_frames(transport, report)
            self._join_and_maybe_kick(transport, report)
            self._heartbeat_loop(transport, report)
        except (DurationError, OSError) as exc:
            report.error = str(exc) or exc.__class__.__name__
            if raise_on_error:
                raise
            log.warning("学习时长上报失败（已软着陆）：%s", report.error)
        finally:
            if transport is not None:
                with contextlib.suppress(Exception):
                    transport.close()
        if not report.error:
            log.info("学习时长上报结束：%s", report.summary())
        return report

    def _default_transport(self) -> DurationTransport:
        return SocketDurationTransport(timeout=self.timeout, origin=self.origin)

    # -- 内部步骤 ---------------------------------------------------------

    def _expect_open(self, transport: DurationTransport, report: DurationReport) -> None:
        """首帧必须是 engine.io open（``0{...}``）；只记录，不据此改节奏。"""
        text = transport.recv()
        packet = decode_packet(text)
        if packet is None or packet.type != ENGINE_OPEN:
            raise DurationProtocolError(f"首帧不是 engine.io open 帧：{_preview(text)!r}")
        report.open_frame = _preview(text)
        if packet.data:
            try:
                payload = json.loads(packet.data)
            except json.JSONDecodeError:
                log.debug("open 帧 payload 不是 JSON（忽略）：%r", _preview(packet.data))
            else:
                if isinstance(payload, dict):
                    log.debug("engine.io open：sid=%r", payload.get("sid"))

    def _send_business_frames(
        self, transport: DurationTransport, report: DurationReport
    ) -> None:
        """发送调用方显式提供、且已声明核对过的业务帧（默认一条都不发）。"""
        for frame in self.business_frames:
            transport.send(frame)
            report.business_frames_sent.append(_preview(frame))

    def _join_and_maybe_kick(
        self, transport: DurationTransport, report: DurationReport
    ) -> None:
        """入命名空间；若启用 kick，等入房回执后**只发一次** kick。

        源码事实：kick 重发会被服务端判定「任务没有信息变更」并停表，所以本函数
        没有重试路径。等不到回执就跳过 kick（软着陆），继续心跳。
        """
        if not self.join_namespace:
            return
        transport.send(build_join_frame(self.namespace))
        report.join_frame_sent = True
        if not self.enable_kick:
            return
        if not self._await_namespace_ack(transport, report):
            report.warnings.append(
                "未等到 namespace 入房回执，已跳过 kick（本次连接不会产生记账）"
            )
            return
        now_ms = int(self._wall_clock_ms())
        frame = build_kick_frame(
            self.open_id,
            self.module,
            self.module_group,
            now_ms=now_ms,
            namespace=self.namespace,
            client=self.kick_client,
            page_url=self.kick_page_url,
        )
        transport.send(frame)
        report.kick_sent = True
        report.kick_frame_sent = _preview(frame)

    def _await_namespace_ack(
        self, transport: DurationTransport, report: DurationReport
    ) -> bool:
        """等 ``40/userActivities`` 开头的入房回执；其它消息照常记录。"""
        prefix = f"40{self.namespace}"
        for _ in range(MAX_FRAMES_FOR_ACK):
            try:
                text = transport.recv()
            except TimeoutError:
                return False
            packet = decode_packet(text)
            if packet is None:
                report.warnings.append(f"收到无法解析的文本帧：{_preview(text)!r}")
                continue
            if packet.type == ENGINE_OPEN:
                continue
            if packet.type == ENGINE_PING:
                transport.send(encode_packet(ENGINE_PONG))
                continue
            if packet.type == ENGINE_MESSAGE:
                if text.startswith(prefix):
                    report.namespace_acked = True
                    return True
                self._record_message(text, report)
                continue
            if packet.type == ENGINE_CLOSE:
                raise DurationProtocolError("服务端发送了 engine.io close 帧")
            log.debug("等待入房回执时忽略 engine.io 包：%r", _preview(text))
        return False

    def _record_message(self, text: str, report: DurationReport) -> None:
        """记录收到的消息帧，并容错累加 r_set 的记账增量（仅观测）。"""
        if len(report.received_frames) < MAX_RECORDED_FRAMES:
            report.received_frames.append(_preview(text))
        update = parse_r_set(text)
        if update is None:
            return
        if update.ms_update in self._seen_r_set:
            return
        self._seen_r_set.add(update.ms_update)
        report.r_set_updates += 1
        report.credited_seconds += update.delta

    def _heartbeat_loop(
        self, transport: DurationTransport, report: DurationReport
    ) -> None:
        """按固定节拍发 ``2`` 并等 ``3``；截止时刻前结束（严格早于 deadline）。"""
        start = self._clock()
        deadline = start + report.plan.requested_seconds
        next_ping = start + self.ping_interval
        missed_streak = 0
        try:
            while True:
                now = self._clock()
                if now >= deadline:
                    break
                wait = min(next_ping, deadline) - now
                if wait > 0:
                    self._sleeper(wait)
                now = self._clock()
                if now >= deadline:
                    break
                transport.send(encode_packet(ENGINE_PING))
                report.pings_sent += 1
                if self._await_pong(transport, report):
                    report.pongs_received += 1
                    missed_streak = 0
                else:
                    missed_streak += 1
                    report.pongs_missed += 1
                    if missed_streak >= self.max_missed_pongs:
                        raise DurationProtocolError(
                            f"连续 {missed_streak} 次 ping 未收到 pong，终止本轮上报"
                        )
                next_ping += self.ping_interval
        finally:
            report.elapsed_seconds = max(0.0, self._clock() - start)

    def _await_pong(self, transport: DurationTransport, report: DurationReport) -> bool:
        """等一个 pong（``3``）；顺手回服务端 ping、记录业务帧。

        超时/空窗返回 ``False``，由心跳循环决定是否终止。
        """
        for _ in range(MAX_FRAMES_PER_PONG):
            try:
                text = transport.recv()
            except TimeoutError:
                return False
            packet = decode_packet(text)
            if packet is None:
                report.warnings.append(f"收到无法解析的文本帧：{_preview(text)!r}")
                continue
            if packet.type == ENGINE_PONG:
                return True
            if packet.type == ENGINE_PING:
                # engine.io v3 服务端通常不发 ping；收到就按协议回 pong，保持兼容
                transport.send(encode_packet(ENGINE_PONG))
                continue
            if packet.type == ENGINE_MESSAGE:
                self._record_message(text, report)
                continue
            if packet.type == ENGINE_CLOSE:
                raise DurationProtocolError("服务端发送了 engine.io close 帧")
            log.debug("忽略 engine.io 包：%r", _preview(text))
        return False


# --------------------------------------------------------------------------
# 模块级便捷 API（Lead 接线用）
# --------------------------------------------------------------------------


def report_duration(
    open_id: str,
    token: str,
    seconds: float,
    *,
    confirm: bool = False,
    ping_interval: float = DEFAULT_PING_INTERVAL,
    timeout: float = DEFAULT_TIMEOUT,
    namespace: str = NAMESPACE,
    join_namespace: bool = True,
    enable_kick: bool = False,
    module: str = "",
    module_group: str = "",
    kick_client: str = KICK_CLIENT,
    kick_page_url: str = KICK_PAGE_URL,
    business_frames: Sequence[str] = (),
    frames_verified: bool = False,
    origin: str | None = None,
    transport_factory: TransportFactory | None = None,
    clock: Clock | None = None,
    sleeper: Sleeper | None = None,
    wall_clock_ms: Callable[[], int] | None = None,
    raise_on_error: bool = False,
) -> DurationReport:
    """一次学习时长上报（默认干跑）。

    与 :class:`SessionDurationReporter` 等价，只是把构造参数摊平，方便一行调用：

        report = report_duration(session.open_id, annotator_token, 600)  # 干跑
        report = report_duration(
            session.open_id, annotator_token, 600,
            confirm=True, enable_kick=True, module="cmgt", module_group="h5",
        )  # 真连 + 单次 kick
    """
    reporter = SessionDurationReporter(
        open_id,
        token,
        ping_interval=ping_interval,
        timeout=timeout,
        namespace=namespace,
        join_namespace=join_namespace,
        enable_kick=enable_kick,
        module=module,
        module_group=module_group,
        kick_client=kick_client,
        kick_page_url=kick_page_url,
        business_frames=business_frames,
        origin=origin,
        transport_factory=transport_factory,
        clock=clock,
        sleeper=sleeper,
        wall_clock_ms=wall_clock_ms,
    )
    return reporter.run(
        seconds, confirm=confirm, frames_verified=frames_verified, raise_on_error=raise_on_error
    )


__all__ = [
    "DEFAULT_PING_INTERVAL",
    "DEFAULT_TIMEOUT",
    "DURATION_PATH",
    "ENGINE_CLOSE",
    "ENGINE_MESSAGE",
    "ENGINE_OPEN",
    "ENGINE_PACKET_TYPES",
    "ENGINE_PING",
    "ENGINE_PONG",
    "EXPECTED_WS_HOST",
    "KICK_APP_ID",
    "KICK_CLIENT",
    "KICK_EVENT",
    "KICK_PAGE_URL",
    "KICK_SERVICE_ID",
    "NAMESPACE",
    "RSetUpdate",
    "EnginePacket",
    "DurationConfigError",
    "DurationError",
    "DurationGateError",
    "DurationPlan",
    "DurationProtocolError",
    "DurationReport",
    "DurationTransport",
    "DurationTransportError",
    "MAX_DURATION_SECONDS",
    "SessionDurationReporter",
    "SocketDurationTransport",
    "WsFrame",
    "apply_mask",
    "assert_duration_url",
    "build_duration_url",
    "build_handshake_request",
    "decode_frame",
    "decode_packet",
    "build_join_frame",
    "build_kick_frame",
    "build_kick_payload",
    "encode_frame",
    "encode_packet",
    "expected_accept",
    "parse_r_set",
    "perform_handshake",
    "read_frame",
    "redact_duration_url",
    "report_duration",
    "TASK_DURATION_PATH",
    "TaskDurationPlan",
    "TaskDurationReport",
    "TaskTarget",
    "build_task_page_base",
    "build_task_duration_url",
    "build_task_join_frame",
    "build_task_start_frame",
    "build_task_stop_frame",
    "plan_task_duration",
    "run_task_duration",
    "ROOM_MODULES",
    "ROOM_GROUPS",
    "MAX_ROOMS",
    "SAFE_ROOMS",
    "build_room_pool",
    "DurationPoolPlan",
    "DurationPoolReport",
    "plan_duration_pool",
    "run_duration_pool",
]


# ==========================================================================
# 真实任务模式（刷课）—— 与上面的「房间池」是两条不同的账
# ==========================================================================
#
# 依据：参考实现 `src/api/duration.rs` 的 `start_task_boost`（只读源码事实）。
# 两条链路不要混：
#
# | | 房间池（report_duration） | 真实任务（run_task_duration） |
# | --- | --- | --- |
# | 端点 | `/unipusiopoint/` | `/unipusio/` |
# | 事件 | `kick-v2`（每条连接一次） | `start` 每 5 秒重发 + 轮换前补 `stop` |
# | client | `U校园mobile` | `U校园pc` |
# | 归属 | 假名 (module, group) → 账号级 | **(真实分组, 真实课程)** |
#
# 归属靠 `start` 里的 `url`：必须是**真实任务页** URL
# （`…/pc.html?cid=…&cloudCurriculaId=…&source=cloud&courseResourceId=…#/<实例>/courseware/<单元>/<micro>/<分组>`），
# 编造的 URL 只会落进通用桶（`module:"menu"`）。
#
# ⚠️ 记账未验证：2026-10-20 用真实账号跑「房间池 180s」与「单目标任务模式 150s」
# 两次，`credited_seconds` 都是 0（连接、入房、事件都正常，但没收到 `r_set`）。
# 参考实现声称「当场落账 1×」，差异原因未查明（怀疑 page URL 里的
# `cloudCurriculaId` 与实际班级不符，或账号当次不满足记账条件）。
# 因此本模式**默认干跑**，且真跑只在前端二次确认后发生。

#: 任务模式专用端点（与房间池的 `/unipusiopoint/` 不同）
TASK_DURATION_PATH: Final[str] = "/unipusio/"
#: 官方页面 `start` 的重发间隔（秒）
TASK_START_INTERVAL: Final[float] = 5.0
#: 每多久轮换一个任务（秒）。真页面 30 分钟停表一次，这里取 4 分钟避开固定节奏
TASK_ROTATE_SECONDS: Final[float] = 240.0
#: 任务模式固定用 PC 客户端名（参考实现实测：mobile 名字在 start 事件下不认）
TASK_CLIENT: Final[str] = "U校园pc"
#: 任务页基础路径（真实页面就是这个）
TASK_PAGE_PATH: Final[str] = "/_explorationpc_default/pc.html"


#: 房间池：服务端按 (module, moduleGroup) 成对独立记账；**单房间不记账**（实测），
#: 多房间并发才有 r_set（6 房间 150 秒实测 509 秒记账）。
ROOM_MODULES: Final[tuple[str, ...]] = (
    "cmgt", "ucmg", "course", "homework", "studio", "tla", "ach", "exam", "point", "cmgt2",
)
ROOM_GROUPS: Final[tuple[str, ...]] = ("h5", "pc", "app", "wx", "web", "v2")
#: 最多房间数（参考实现：再多握手会被随机拒绝）
MAX_ROOMS: Final[int] = len(ROOM_MODULES) * len(ROOM_GROUPS)
#: 实测安全房间数
SAFE_ROOMS: Final[int] = 20


def build_room_pool(rooms: int = 6) -> tuple[tuple[str, str], ...]:
    """构造 (module, moduleGroup) 房间池；按参考实现的顺序（先 group 后 module）。"""
    if rooms < 1:
        raise DurationConfigError("房间数至少 1")
    pool: list[tuple[str, str]] = []
    for group in ROOM_GROUPS:
        for module in ROOM_MODULES:
            if len(pool) >= min(rooms, MAX_ROOMS):
                return tuple(pool)
            pool.append((module, group))
    return tuple(pool)


@dataclass(slots=True)
class DurationPoolPlan:
    """多房间刷时长的干跑计划。"""

    url: str
    rooms: tuple[tuple[str, str], ...]
    requested_seconds: float
    ping_interval: float = DEFAULT_PING_INTERVAL
    warnings: list[str] = field(default_factory=list)

    @property
    def estimated_ratio(self) -> float:
        # 参考实现实测：单房间约 0.7~0.79×，房间越多摊得越匀；只做粗略估值
        return 0.7 * len(self.rooms)

    def summary(self) -> str:
        rooms = "、".join(f"{m}/{g}" for m, g in self.rooms[:6])
        more = f" 等 {len(self.rooms)} 个房间" if len(self.rooms) > 6 else ""
        return (
            f"多房间时长：{self.requested_seconds:g} 秒，{len(self.rooms)} 个房间"
            f"（{rooms}{more}），预计记账约 {self.requested_seconds * self.estimated_ratio / 60:.1f} 分钟"
        )


@dataclass(slots=True)
class DurationPoolReport:
    plan: DurationPoolPlan
    dry_run: bool
    credited_seconds: float = 0.0
    r_set_updates: int = 0
    rooms_connected: int = 0
    rooms_kicked: int = 0
    per_room: dict[str, float] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.rooms_connected > 0


def plan_duration_pool(
    open_id: str, token: str, *, seconds: float, rooms: int = 6
) -> DurationPoolPlan:
    requested = _validate_seconds(seconds, MAX_DURATION_SECONDS)
    pool = build_room_pool(rooms)
    warnings: list[str] = []
    if requested < 60:
        warnings.append("r_set 记账约每 60 秒一批：短于 60 秒的会话大概率记 0")
    if len(pool) < 2:
        warnings.append("只开 1 个房间时服务端**不记账**（实测），至少 2 个房间")
    warnings.append(
        "房间池是**账号级**记账（不含课程/分组信息），门户按教材归集可能有延迟"
    )
    return DurationPoolPlan(
        url=build_duration_url(open_id, token),
        rooms=pool,
        requested_seconds=requested,
        warnings=warnings,
    )


def run_duration_pool(
    open_id: str,
    token: str,
    *,
    seconds: float,
    rooms: int = 6,
    confirm: bool = False,
    transport_factory: Callable[[], Any] | None = None,
    reporter: Callable[..., Any] | None = None,
) -> DurationPoolReport:
    """多房间刷时长：每个房间一条连接（各自 kick 一次 + 心跳），汇总记账。

    ``confirm=False``（默认）只出计划，**不建任何连接**。
    实测：单房间 0 记账；6 房间 150 秒合计 509 秒。
    """
    plan = plan_duration_pool(open_id, token, seconds=seconds, rooms=rooms)
    report = DurationPoolReport(plan=plan, dry_run=not confirm, warnings=list(plan.warnings))
    if not confirm:
        return report

    run_one = reporter or (
        lambda module, group: report_duration(
            open_id, token, seconds, confirm=True,
            enable_kick=True, module=module, module_group=group,
            transport_factory=transport_factory,
        )
    )
    import threading

    results: dict[str, Any] = {}

    def worker(module: str, group: str) -> None:
        try:
            results[f"{module}/{group}"] = run_one(module, group)
        except Exception as exc:  # noqa: BLE001 - 单房间失败不影响其它
            report.errors.append(f"{module}/{group}: {type(exc).__name__}: {exc}")

    threads = [
        threading.Thread(target=worker, args=(module, group), daemon=True)
        for module, group in plan.rooms
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    for name, item in results.items():
        credited = float(getattr(item, "credited_seconds", 0) or 0)
        report.per_room[name] = credited
        report.credited_seconds += credited
        report.r_set_updates += int(getattr(item, "r_set_updates", 0) or 0)
        if getattr(item, "connected", False):
            report.rooms_connected += 1
        if getattr(item, "kick_sent", False):
            report.rooms_kicked += 1
        if getattr(item, "error", ""):
            report.errors.append(f"{name}: {item.error}")
    return report


def build_task_duration_url(open_id: str, token: str) -> str:
    """任务模式的 WSS URL（``/unipusio/``）。"""
    if not isinstance(open_id, str) or not open_id:
        raise DurationConfigError("open_id 不能为空")
    if not isinstance(token, str) or not token:
        raise DurationConfigError("token 不能为空")
    query = urllib.parse.urlencode(
        {"EIO": "3", "transport": "websocket", "uuid": open_id, "token": token},
        quote_via=urllib.parse.quote,
        safe="-_.~",
    )
    url = f"{_ws_base()}{TASK_DURATION_PATH}?{query}"
    assert_task_duration_url(url)
    return url


def assert_task_duration_url(url: str) -> None:
    """任务模式的域名/路径断言（只允许 ucontent 的 ``/unipusio/``）。"""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "wss":
        raise DurationProtocolError(f"任务模式必须走 wss：{url}")
    if parts.hostname != EXPECTED_WS_HOST:
        raise DurationProtocolError(
            f"任务模式只允许 {EXPECTED_WS_HOST}，实际 {parts.hostname}"
        )
    if not parts.path.startswith(TASK_DURATION_PATH):
        raise DurationProtocolError(f"任务模式路径不符：{parts.path}")


@dataclass(slots=True, frozen=True)
class TaskTarget:
    """一个真实必修任务：``(课程实例, 单元, micro, 分组)``。

    ``micro`` 只是页面 URL 里的过场段；参考实现实测「填单元 ID 照样落进该分组」，
    所以默认与 ``unit`` 相同。
    """

    instance: str
    unit: str
    group: str
    micro: str = ""
    name: str = ""

    def __post_init__(self) -> None:
        for field_name in ("instance", "unit", "group"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise DurationConfigError(f"TaskTarget.{field_name} 不能为空")
        if not self.micro:
            object.__setattr__(self, "micro", self.unit)

    def tag3(self) -> str:
        return json.dumps(
            {"microBlock": f"{self.unit}/{self.micro}", "version": "1", "source": "ucontent"},
            ensure_ascii=False,
            separators=(",", ":"),
        )

    def page_url(self, page_base: str) -> str:
        return (
            f"{page_base}#/{self.instance}/courseware/"
            f"{self.unit}/{self.micro}/{self.group}"
        )

    def describe(self) -> str:
        return f"{self.name or self.group}（{self.unit}）"


def build_task_page_base(
    *,
    class_id: str,
    curricula_id: str,
    course_resource_id: str = "",
    host_base: str = "",
) -> str:
    """拼真实任务页前缀（照抄浏览器地址栏的形状）。

    ``host_base`` 默认取 ucontent 的 https 基址；``course_resource_id`` 是门户
    「班级教材 ID」（教材节点自己的 ``id``），缺省时省掉该参数。
    """
    for name, value in (("class_id", class_id), ("curricula_id", curricula_id)):
        if not isinstance(value, str) or not value.strip():
            raise DurationConfigError(f"{name} 不能为空")
    base = host_base or endpoints.BASE_URLS[UnipusApp.UCONTENT]
    query = [
        ("cid", class_id),
        ("cloudCurriculaId", curricula_id),
        ("source", "cloud"),
    ]
    if course_resource_id:
        query.append(("courseResourceId", course_resource_id))
    tail = "&".join(
        f"{name}={urllib.parse.quote(str(value), safe='')}" for name, value in query
    )
    return f"{base}{TASK_PAGE_PATH}?{tail}"


def build_task_join_frame(
    open_id: str, token: str, *, namespace: str = NAMESPACE
) -> str:
    """任务模式的入房帧：``40/userActivities?uuid=…&token=…,``（末尾带逗号）。"""
    if not open_id or not token:
        raise DurationConfigError("入房帧需要 open_id 与 token")
    query = urllib.parse.urlencode(
        {"uuid": open_id, "token": token}, quote_via=urllib.parse.quote, safe="-_.~"
    )
    return f"40{namespace}?{query},"


def build_task_start_payload(
    target: TaskTarget, *, page_base: str, client: str = TASK_CLIENT
) -> dict[str, Any]:
    return {
        "module": target.group,
        "moduleGroup": target.instance,
        "client": client,
        "url": target.page_url(page_base),
        "tag1": target.unit,
        "tag2": target.micro,
        "tag3": target.tag3(),
    }


def build_task_start_frame(
    target: TaskTarget, *, page_base: str, namespace: str = NAMESPACE,
    client: str = TASK_CLIENT, message_id: int = 0,
) -> str:
    payload = build_task_start_payload(target, page_base=page_base, client=client)
    body = json.dumps(["start", payload], ensure_ascii=False, separators=(",", ":"))
    return f"42{namespace},{message_id}{body}"


def build_task_stop_frame(
    target: TaskTarget, *, page_base: str, timer_ms: int, namespace: str = NAMESPACE,
    client: str = TASK_CLIENT, message_id: int = 2,
) -> str:
    payload = build_task_start_payload(target, page_base=page_base, client=client)
    payload["timer"] = int(timer_ms)
    body = json.dumps(["stop", payload], ensure_ascii=False, separators=(",", ":"))
    return f"42{namespace},{message_id}{body}"


@dataclass(slots=True)
class TaskDurationPlan:
    """任务模式的干跑计划（不发任何连接）。"""

    url: str
    page_base: str
    targets: tuple[TaskTarget, ...]
    requested_seconds: float
    start_interval: float = TASK_START_INTERVAL
    rotate_seconds: float = TASK_ROTATE_SECONDS
    namespace: str = NAMESPACE
    join_frame: str = ""
    start_frames: tuple[str, ...] = ()
    stop_frame: str = ""
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> str:
        names = "、".join(target.describe() for target in self.targets[:3])
        more = f" 等 {len(self.targets)} 个" if len(self.targets) > 3 else ""
        return (
            f"任务模式：{self.requested_seconds:g} 秒，{self.start_interval:g}s 报一次 start，"
            f"{self.rotate_seconds:g}s 轮换；目标 {names}{more}"
        )


@dataclass(slots=True)
class TaskDurationReport:
    plan: TaskDurationPlan
    dry_run: bool
    connected: bool = False
    joined: bool = False
    starts_sent: int = 0
    stops_sent: int = 0
    rotations: int = 0
    pings_sent: int = 0
    pongs_received: int = 0
    credited_seconds: float = 0.0
    r_set_updates: int = 0
    received_frames: list[str] = field(default_factory=list)
    elapsed_seconds: float = 0.0
    error: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.error


def plan_task_duration(
    open_id: str,
    token: str,
    targets: Sequence[TaskTarget],
    *,
    seconds: float,
    page_base: str,
    start_interval: float = TASK_START_INTERVAL,
    rotate_seconds: float = TASK_ROTATE_SECONDS,
    namespace: str = NAMESPACE,
) -> TaskDurationPlan:
    """算出「将要发什么」，不建立任何连接。"""
    if not targets:
        raise DurationConfigError("任务模式至少要有一个目标（必修分组）")
    if not page_base:
        raise DurationConfigError("任务模式需要真实任务页前缀（page_base）")
    requested = _validate_seconds(seconds, MAX_DURATION_SECONDS)
    if start_interval <= 0:
        raise DurationConfigError("start_interval 必须为正数")
    if rotate_seconds <= 0:
        raise DurationConfigError("rotate_seconds 必须为正数")
    url = build_task_duration_url(open_id, token)
    first = targets[0]
    warnings: list[str] = []
    if requested < 60:
        warnings.append("r_set 记账约每 60 秒一批：短于 60 秒的会话大概率记 0")
    if len(targets) == 1:
        warnings.append("只有 1 个目标：任务模式是 1× 封顶（不像房间池会放大）")
    warnings.append(
        "任务模式的记账**尚未验证**（本机两次实测 credited=0）；真跑前请先看这个计划"
    )
    return TaskDurationPlan(
        url=url,
        page_base=page_base,
        targets=tuple(targets),
        requested_seconds=requested,
        start_interval=start_interval,
        rotate_seconds=rotate_seconds,
        namespace=namespace,
        join_frame=build_task_join_frame(open_id, token, namespace=namespace),
        start_frames=(
            build_task_start_frame(first, page_base=page_base, namespace=namespace),
        ),
        stop_frame=build_task_stop_frame(
            first, page_base=page_base, timer_ms=0, namespace=namespace
        ),
        warnings=warnings,
    )


def run_task_duration(
    open_id: str,
    token: str,
    targets: Sequence[TaskTarget],
    *,
    seconds: float,
    page_base: str,
    confirm: bool = False,
    start_interval: float = TASK_START_INTERVAL,
    rotate_seconds: float = TASK_ROTATE_SECONDS,
    namespace: str = NAMESPACE,
    transport_factory: Callable[[], Any] | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
    wall_clock_ms: Callable[[], int] | None = None,
    raise_on_error: bool = False,
) -> TaskDurationReport:
    """真实任务模式：一条连接、轮换目标、按周期重发 ``start``。默认干跑。"""
    plan = plan_task_duration(
        open_id, token, targets,
        seconds=seconds, page_base=page_base,
        start_interval=start_interval, rotate_seconds=rotate_seconds, namespace=namespace,
    )
    report = TaskDurationReport(plan=plan, dry_run=not confirm, warnings=list(plan.warnings))
    if not confirm:
        return report

    now_ms = wall_clock_ms or (lambda: int(time.time() * 1000))
    factory = transport_factory or (lambda: SocketDurationTransport())
    transport = factory()
    started = clock()
    seen_updates: set[int] = set()
    index = 0
    next_start = 0.0
    next_rotate = clock() + rotate_seconds
    next_ping = clock() + DEFAULT_PING_INTERVAL
    try:
        transport.connect(plan.url)
        report.connected = True
        while True:
            elapsed = clock() - started
            report.elapsed_seconds = elapsed
            if elapsed >= plan.requested_seconds:
                break
            # 到期/轮换前补 stop，再换目标
            if index < len(plan.targets) - 1 and clock() >= next_rotate:
                transport.send(
                    build_task_stop_frame(
                        plan.targets[index], page_base=page_base,
                        timer_ms=now_ms(), namespace=namespace,
                    )
                )
                report.stops_sent += 1
                index += 1
                report.rotations += 1
                next_start = 0.0
                next_rotate = clock() + rotate_seconds
            if report.joined and clock() >= next_start:
                transport.send(
                    build_task_start_frame(
                        plan.targets[index], page_base=page_base, namespace=namespace
                    )
                )
                report.starts_sent += 1
                next_start = clock() + start_interval
            if clock() >= next_ping:
                transport.send("2")
                report.pings_sent += 1
                next_ping = clock() + DEFAULT_PING_INTERVAL

            if hasattr(transport, "settimeout"):
                transport.settimeout(0.5)
            try:
                message = transport.recv()
            except TimeoutError:
                # 没有新帧是常态（心跳/start 由时钟驱动），继续下一轮
                continue
            if not message:
                continue
            if len(report.received_frames) < MAX_RECORDED_FRAMES:
                report.received_frames.append(_preview(message))
            if message == "2":
                transport.send("3")
                report.pongs_received += 1
                continue
            if message == "3":
                continue
            if message.startswith("0") and not message.startswith("40"):
                transport.send(plan.join_frame)
                continue
            if message.startswith(f"40{namespace}"):
                report.joined = True
                continue
            update = parse_r_set(message)
            if update is not None:
                key = int(update.ms_update)
                if key not in seen_updates:
                    seen_updates.add(key)
                    report.credited_seconds += float(update.delta)
                    report.r_set_updates += 1
    except DurationError as exc:
        report.error = str(exc)
        if raise_on_error:
            raise
    except Exception as exc:  # noqa: BLE001 - 连接/协议异常一律软着陆
        report.error = f"{type(exc).__name__}: {exc}"
        if raise_on_error:
            raise
    finally:
        with contextlib.suppress(Exception):
            if plan.targets and report.starts_sent:
                transport.send(
                    build_task_stop_frame(
                        plan.targets[min(index, len(plan.targets) - 1)],
                        page_base=page_base, timer_ms=now_ms(), namespace=namespace,
                    )
                )
                report.stops_sent += 1
        with contextlib.suppress(Exception):
            transport.close()
    report.elapsed_seconds = clock() - started
    return report
