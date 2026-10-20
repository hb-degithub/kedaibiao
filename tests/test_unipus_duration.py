"""U校园学习时长上报（engine.io v3 over WSS）的**完全离线**测试。

约束（和适配器测试一致）：

- **不访问任何站点**：真实传输类 ``SocketDurationTransport`` 只在单测里注入
  假 socket（握手/帧收发），从不调用 ``socket.create_connection``；
- 业务会话用**虚拟时钟 + 假 transport** 跑完整时长，不产生真实 ``sleep``；
- 干跑路径用「一旦被调用就炸」的 transport 工厂，证明它**不会建连接**。

未验证项（业务帧形状、时长记账口径）不在测试里「猜对」，而是锁住：
默认不发业务帧、要发必须显式放行（``frames_verified=True``）。
"""

from __future__ import annotations

import base64
import hashlib
import json
import unittest

from kedaibiao.platforms.unipus import duration
from kedaibiao.platforms.unipus.duration import (
    DEFAULT_PING_INTERVAL,
    DURATION_PATH,
    ENGINE_CLOSE,
    ENGINE_MESSAGE,
    ENGINE_OPEN,
    ENGINE_PING,
    ENGINE_PONG,
    KICK_CLIENT,
    KICK_PAGE_URL,
    RSetUpdate,
    DurationConfigError,
    DurationGateError,
    DurationProtocolError,
    DurationTransportError,
    SessionDurationReporter,
    SocketDurationTransport,
    apply_mask,
    build_duration_url,
    build_handshake_request,
    build_join_frame,
    build_kick_frame,
    build_kick_payload,
    decode_frame,
    decode_packet,
    encode_frame,
    encode_packet,
    expected_accept,
    parse_r_set,
    read_frame,
    redact_duration_url,
    report_duration,
)

JOIN_FRAME = "40/userActivities,"

OPEN_FRAME = '0{"sid":"test-sid","upgrades":[],"pingInterval":20000,"pingTimeout":5000}'


# --------------------------------------------------------------------------
# 假对象
# --------------------------------------------------------------------------


class VirtualClock:
    """虚拟时钟：``sleep`` 推进时间，``__call__`` 读时间。"""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class ScriptedTransport:
    """按脚本返回帧的假 transport；``TIMEOUT`` 标记模拟 recv 超时。"""

    TIMEOUT = object()

    def __init__(self, frames: list[str | object] | None = None) -> None:
        self.frames = list(frames or [])
        self.sent: list[str] = []
        self.connected_url = ""
        self.connected = False
        self.closed = False

    def connect(self, url: str) -> None:
        self.connected_url = url
        self.connected = True

    def send(self, text: str) -> None:
        self.sent.append(text)

    def recv(self) -> str:
        if not self.frames:
            raise TimeoutError("脚本里没有更多帧")
        item = self.frames.pop(0)
        if item is self.TIMEOUT:
            raise TimeoutError("模拟 recv 超时")
        assert isinstance(item, str)
        return item

    def close(self) -> None:
        self.closed = True


class ExplodingTransport:
    """一旦被用于连接就失败；用来证明干跑不建连接。"""

    def connect(self, url: str) -> None:  # pragma: no cover - 不应该被调用
        raise AssertionError("干跑不应建立连接")

    def send(self, text: str) -> None:  # pragma: no cover
        raise AssertionError("干跑不应发送")

    def recv(self) -> str:  # pragma: no cover
        raise AssertionError("干跑不应接收")

    def close(self) -> None:  # pragma: no cover
        raise AssertionError("干跑不应关闭")


class ClientSocket:
    """假 socket：记录客户端发出的字节，并用脚本回放服务端字节。"""

    def __init__(self, incoming: bytes = b"") -> None:
        self.incoming = incoming
        self.sent = b""
        self.timeout: float | None = None
        self.closed = False

    def sendall(self, data: bytes) -> None:
        self.sent += data

    def recv(self, count: int) -> bytes:
        chunk, self.incoming = self.incoming[:count], self.incoming[count:]
        return chunk

    def settimeout(self, value: float) -> None:
        self.timeout = value

    def close(self) -> None:
        self.closed = True


class HandshakeSocket(ClientSocket):
    """收到请求后按 RFC 6455 现场算出正确的 ``Sec-WebSocket-Accept``。"""

    def __init__(self, *, status: str = "101 Switching Protocols", accept: str | None = None):
        super().__init__()
        self._status = status
        self._forced_accept = accept
        self._response = b""
        self._responded = False

    def recv(self, count: int) -> bytes:
        if not self._responded:
            request = self.sent.decode("latin-1")
            key = ""
            for line in request.split("\r\n"):
                if line.lower().startswith("sec-websocket-key:"):
                    key = line.split(":", 1)[1].strip()
            accept = self._forced_accept or expected_accept(key)
            self._response = (
                f"HTTP/1.1 {self._status}\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
            ).encode("latin-1")
            self._responded = True
            # 握手响应先于任何服务端数据帧（模拟同一 TCP 段里紧跟数据的情况）
            self.incoming = self._response + self.incoming
        return super().recv(count)


def _server_frame(opcode: int, payload: bytes = b"", *, mask_key: bytes | None = None) -> bytes:
    """构造服务端帧（默认不加掩码；传 mask_key 时加掩码，用于兼容性测试）。"""
    length = len(payload)
    header = bytearray([0x80 | opcode])
    if length < 126:
        header.append(length)
    elif length <= 0xFFFF:
        header += bytes([126]) + length.to_bytes(2, "big")
    else:
        header += bytes([127]) + length.to_bytes(8, "big")
    if mask_key is not None:
        header[1] |= 0x80
        header += mask_key
        payload = apply_mask(payload, mask_key)
    return bytes(header) + payload


# --------------------------------------------------------------------------
# engine.io v3 文本包编解码
# --------------------------------------------------------------------------


class TestEnginePacket(unittest.TestCase):
    def test_encode_ping_is_two(self) -> None:
        self.assertEqual(encode_packet(ENGINE_PING), "2")
        self.assertEqual(encode_packet(ENGINE_PONG), "3")
        self.assertEqual(encode_packet(ENGINE_OPEN, '{"sid":"x"}'), '0{"sid":"x"}')
        self.assertEqual(encode_packet(ENGINE_MESSAGE, '40["x"]'), '440["x"]')

    def test_decode_roundtrip(self) -> None:
        packet = decode_packet('0{"sid":"abc"}')
        assert packet is not None
        self.assertEqual(packet.type, ENGINE_OPEN)
        self.assertEqual(packet.data, '{"sid":"abc"}')
        self.assertEqual(decode_packet("3"), duration.EnginePacket(ENGINE_PONG, ""))

    def test_decode_rejects_empty_and_unknown(self) -> None:
        self.assertIsNone(decode_packet(""))
        self.assertIsNone(decode_packet("9"))
        self.assertIsNone(decode_packet("x"))

    def test_encode_rejects_unknown_type(self) -> None:
        with self.assertRaises(DurationConfigError):
            encode_packet("9")
        with self.assertRaises(DurationConfigError):
            encode_packet("")


# --------------------------------------------------------------------------
# RFC 6455 帧编解码
# --------------------------------------------------------------------------


class TestWebSocketFrames(unittest.TestCase):
    def test_client_frame_exact_bytes(self) -> None:
        frame = encode_frame(0x1, b"hi", mask_key=b"\x01\x02\x03\x04")
        self.assertEqual(frame, bytes([0x81, 0x82, 0x01, 0x02, 0x03, 0x04, 0x69, 0x6B]))

    def test_client_frame_uses_random_mask(self) -> None:
        first = encode_frame(0x1, b"hello")
        second = encode_frame(0x1, b"hello")
        self.assertNotEqual(first[2:6], second[2:6])
        self.assertEqual(first[0] & 0x0F, 0x1)

    def test_decode_unmasked_server_text_frame(self) -> None:
        frame = decode_frame(b"\x81\x05hello")
        assert frame is not None
        self.assertEqual((frame.opcode, frame.payload, frame.size), (0x1, b"hello", 7))

    def test_decode_masked_frame(self) -> None:
        raw = _server_frame(0x1, b"hello", mask_key=b"\xAA\xBB\xCC\xDD")
        frame = decode_frame(raw)
        assert frame is not None
        self.assertEqual(frame.opcode, 0x1)
        self.assertEqual(frame.payload, b"hello")

    def test_decode_incomplete_returns_none(self) -> None:
        self.assertIsNone(decode_frame(b""))
        self.assertIsNone(decode_frame(b"\x81"))
        self.assertIsNone(decode_frame(b"\x81\x05hel"))
        self.assertIsNone(decode_frame(b"\x81\xFE\x00"))

    def test_extended_lengths(self) -> None:
        frame_126 = encode_frame(0x1, b"x" * 200, mask_key=b"\x00\x00\x00\x00")
        self.assertEqual(frame_126[1], 0x80 | 126)
        decoded = decode_frame(_server_frame(0x1, b"x" * 200))
        assert decoded is not None
        self.assertEqual(decoded.payload, b"x" * 200)
        self.assertEqual(decoded.size, 4 + 200)

        frame_127 = decode_frame(_server_frame(0x1, b"y" * 70000))
        assert frame_127 is not None
        self.assertEqual(len(frame_127.payload), 70000)

    def test_oversized_frame_rejected(self) -> None:
        oversized = duration.MAX_FRAME_BYTES + 1
        with self.assertRaises(DurationProtocolError):
            decode_frame(b"\x81\x7F" + oversized.to_bytes(8, "big"))

    def test_fragmented_and_rsv_rejected(self) -> None:
        with self.assertRaises(DurationProtocolError):
            decode_frame(b"\x40\x00")  # RSV1 置位
        with self.assertRaises(DurationProtocolError):
            decode_frame(b"\x00\x00")  # continuation 帧（本模块不分片）

    def test_read_frame_from_socket(self) -> None:
        sock = ClientSocket(_server_frame(0x1, b"hello"))
        frame = read_frame(sock)
        self.assertEqual(frame.payload, b"hello")

    def test_read_frame_raises_on_close(self) -> None:
        sock = ClientSocket(b"")
        with self.assertRaises(DurationTransportError):
            read_frame(sock)


# --------------------------------------------------------------------------
# URL 构造、域名纪律与打码
# --------------------------------------------------------------------------


class TestDurationUrl(unittest.TestCase):
    def test_url_shape(self) -> None:
        url = build_duration_url("open-id-1", "tok-abc")
        self.assertTrue(url.startswith("wss://ucontent.unipus.cn/unipusiopoint/?"))
        self.assertIn("EIO=3", url)
        self.assertIn("transport=websocket", url)
        self.assertIn("uuid=open-id-1", url)
        self.assertIn("token=tok-abc", url)
        self.assertTrue(url.endswith("token=tok-abc"))

    def test_empty_parameters_rejected(self) -> None:
        with self.assertRaises(DurationConfigError):
            build_duration_url("", "tok")
        with self.assertRaises(DurationConfigError):
            build_duration_url("open", "")

    def test_wrong_host_rejected(self) -> None:
        with self.assertRaises(DurationProtocolError):
            duration.assert_duration_url("wss://evil.example.com/unipusiopoint/?EIO=3")
        with self.assertRaises(DurationProtocolError):
            duration.assert_duration_url("https://ucontent.unipus.cn/unipusiopoint/")
        with self.assertRaises(DurationProtocolError):
            duration.assert_duration_url("wss://ucontent.unipus.cn/other/")

    def test_token_is_redacted(self) -> None:
        url = build_duration_url("open-id-1", "SECRET-token.value")
        masked = redact_duration_url(url)
        self.assertNotIn("SECRET-token.value", masked)
        self.assertIn("token=REDACTED", masked)
        self.assertIn("uuid=open-id-1", masked)
        self.assertIn("EIO=3", masked)


# --------------------------------------------------------------------------
# HTTP Upgrade 握手
# --------------------------------------------------------------------------


class TestHandshake(unittest.TestCase):
    def test_request_shape(self) -> None:
        request = build_handshake_request(
            "ucontent.unipus.cn", 443, "/unipusiopoint/?EIO=3", "abc", origin="https://x"
        ).decode("ascii")
        self.assertTrue(request.startswith("GET /unipusiopoint/?EIO=3 HTTP/1.1\r\n"))
        self.assertIn("Host: ucontent.unipus.cn\r\n", request)
        self.assertIn("Upgrade: websocket\r\n", request)
        self.assertIn("Connection: Upgrade\r\n", request)
        self.assertIn("Sec-WebSocket-Key: abc\r\n", request)
        self.assertIn("Sec-WebSocket-Version: 13\r\n", request)
        self.assertIn("Origin: https://x\r\n", request)

    def test_no_origin_by_default(self) -> None:
        request = build_handshake_request("h", 443, "/p", "k").decode("ascii")
        self.assertNotIn("Origin:", request)

    def test_accept_computation(self) -> None:
        # RFC 6455 §1.3 的公开示例
        self.assertEqual(
            expected_accept("dGhlIHNhbXBsZSBub25jZQ=="),
            "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=",
        )

    def test_transport_connect_success(self) -> None:
        sock = HandshakeSocket()
        transport = SocketDurationTransport(
            socket_factory=lambda address, timeout: sock,
            tls_wrapper=lambda raw, host, timeout: raw,
        )
        transport.connect(build_duration_url("open-id-1", "tok"))
        request = sock.sent.decode("ascii")
        self.assertTrue(request.startswith("GET /unipusiopoint/?EIO=3&transport=websocket"))
        self.assertIn("Host: ucontent.unipus.cn\r\n", request)
        self.assertEqual(sock.timeout, duration.DEFAULT_TIMEOUT)
        transport.close()
        self.assertTrue(sock.closed)

    def test_transport_rejects_non_101(self) -> None:
        sock = HandshakeSocket(status="400 Bad Request", accept="ignored")
        transport = SocketDurationTransport(
            socket_factory=lambda address, timeout: sock,
            tls_wrapper=lambda raw, host, timeout: raw,
        )
        with self.assertRaises(DurationProtocolError):
            transport.connect(build_duration_url("open-id-1", "tok"))
        self.assertTrue(sock.closed)

    def test_transport_rejects_bad_accept(self) -> None:
        sock = HandshakeSocket(accept="not-the-right-accept")
        transport = SocketDurationTransport(
            socket_factory=lambda address, timeout: sock,
            tls_wrapper=lambda raw, host, timeout: raw,
        )
        with self.assertRaises(DurationProtocolError):
            transport.connect(build_duration_url("open-id-1", "tok"))

    def test_transport_rejects_wrong_host(self) -> None:
        transport = SocketDurationTransport(
            socket_factory=lambda address, timeout: ClientSocket(),
            tls_wrapper=lambda raw, host, timeout: raw,
        )
        with self.assertRaises(DurationProtocolError):
            transport.connect("wss://evil.example.com/unipusiopoint/?EIO=3")


class TestSocketTransportFrames(unittest.TestCase):
    def _connected_transport(self, incoming: bytes = b"") -> tuple[SocketDurationTransport, ClientSocket]:
        sock = HandshakeSocket()
        sock.incoming = incoming
        transport = SocketDurationTransport(
            socket_factory=lambda address, timeout: sock,
            tls_wrapper=lambda raw, host, timeout: raw,
        )
        transport.connect(build_duration_url("open-id-1", "tok"))
        return transport, sock

    def test_send_writes_masked_text_frame(self) -> None:
        transport, sock = self._connected_transport()
        before = len(sock.sent)
        transport.send("2")
        payload = sock.sent[before:]
        frame = decode_frame(payload)
        assert frame is not None
        self.assertEqual(frame.opcode, 0x1)
        self.assertEqual(frame.payload, b"2")
        self.assertTrue(payload[1] & 0x80)  # 客户端帧必须加掩码

    def test_recv_answers_ws_ping_with_pong(self) -> None:
        transport, sock = self._connected_transport(_server_frame(0x9, b"p"))
        # 紧随其后的文本帧
        sock.incoming += _server_frame(0x1, b"3")
        before = len(sock.sent)
        text = transport.recv()
        self.assertEqual(text, "3")
        pong = decode_frame(sock.sent[before:])
        assert pong is not None
        self.assertEqual(pong.opcode, 0xA)
        self.assertEqual(pong.payload, b"p")

    def test_recv_raises_on_close_frame(self) -> None:
        transport, _ = self._connected_transport(_server_frame(0x8))
        with self.assertRaises(DurationTransportError):
            transport.recv()


# --------------------------------------------------------------------------
# 上报器：干跑 / 闸门 / 参数校验
# --------------------------------------------------------------------------


class TestDryRun(unittest.TestCase):
    def test_dry_run_never_connects(self) -> None:
        reporter = SessionDurationReporter(
            "open-id-1", "tok", transport_factory=lambda: ExplodingTransport()
        )
        report = reporter.run(60)
        self.assertTrue(report.dry_run)
        self.assertFalse(report.connected)
        self.assertEqual(report.pings_sent, 0)
        self.assertTrue(report.ok)
        self.assertEqual(report.plan.planned_pings, 2)
        self.assertIn("token=REDACTED", report.plan.url)

    def test_plan_counts_pings_like_the_loop(self) -> None:
        reporter = SessionDurationReporter("open-id-1", "tok")
        for seconds, expected in [(5, 0), (20, 0), (25, 1), (60, 2), (61, 3), (100, 4)]:
            self.assertEqual(reporter.plan(seconds).planned_pings, expected, seconds)

    def test_dry_run_does_not_leak_token(self) -> None:
        reporter = SessionDurationReporter("open-id-1", "SECRET-token.value")
        with self.assertLogs("kedaibiao.platforms.unipus.duration", level="INFO") as logs:
            report = reporter.run(60)
        self.assertNotIn("SECRET-token.value", report.summary())
        self.assertNotIn("SECRET-token.value", "\n".join(logs.output))
        self.assertNotIn("SECRET-token.value", repr(report.plan))

    def test_plan_warns_when_kick_disabled(self) -> None:
        plan = SessionDurationReporter("open-id-1", "tok").plan(60)
        self.assertTrue(plan.join_namespace)
        self.assertFalse(plan.kick_enabled)
        self.assertTrue(any("不会真正记账" in warning for warning in plan.warnings))
        self.assertIn("不 kick", plan.mode())

    def test_plan_shows_kick_mode(self) -> None:
        reporter = SessionDurationReporter(
            "open-id-1", "tok", enable_kick=True, module="cmgt", module_group="h5"
        )
        plan = reporter.plan(60)
        self.assertTrue(plan.kick_enabled)
        self.assertIn("module=cmgt", plan.mode())
        self.assertEqual(plan.planned_pings, 2)

    def test_short_session_warns_no_ping(self) -> None:
        plan = SessionDurationReporter("open-id-1", "tok").plan(5)
        self.assertEqual(plan.planned_pings, 0)
        self.assertTrue(any("短于一个心跳周期" in warning for warning in plan.warnings))


class TestBusinessFrameGate(unittest.TestCase):
    def test_unverified_business_frames_are_refused(self) -> None:
        reporter = SessionDurationReporter(
            "open-id-1", "tok", business_frames=("40",), transport_factory=ScriptedTransport
        )
        with self.assertRaises(DurationGateError):
            reporter.run(60)
        with self.assertRaises(DurationGateError):
            reporter.run(60, confirm=True)

    def test_verified_business_frames_are_sent(self) -> None:
        clock = VirtualClock()
        transport = ScriptedTransport([OPEN_FRAME])
        reporter = SessionDurationReporter(
            "open-id-1",
            "tok",
            business_frames=("40",),
            transport_factory=lambda: transport,
            clock=clock,
            sleeper=clock.sleep,
        )
        report = reporter.run(5, confirm=True, frames_verified=True)
        self.assertEqual(transport.sent, ["40", JOIN_FRAME])
        self.assertEqual(report.business_frames_sent, ["40"])
        self.assertTrue(report.ok)
        self.assertEqual(report.plan.planned_pings, 0)


# --------------------------------------------------------------------------
# 上报器：心跳节拍与 pong 计时（虚拟时钟，零真实等待）
# --------------------------------------------------------------------------


class TestHeartbeatLoop(unittest.TestCase):
    def _run(
        self,
        frames: list[str | object],
        seconds: float,
        **kwargs: object,
    ) -> tuple[object, ScriptedTransport, VirtualClock]:
        clock = VirtualClock()
        transport = ScriptedTransport(frames)
        reporter = SessionDurationReporter(
            "open-id-1",
            "tok",
            transport_factory=lambda: transport,
            clock=clock,
            sleeper=clock.sleep,
        )
        report = reporter.run(seconds, confirm=True, **kwargs)  # type: ignore[arg-type]
        return report, transport, clock

    def test_pings_every_interval_and_counts_pongs(self) -> None:
        report, transport, clock = self._run([OPEN_FRAME, "3", "3"], 60)
        self.assertTrue(report.connected)  # type: ignore[attr-defined]
        self.assertEqual(report.pings_sent, 2)  # type: ignore[attr-defined]
        self.assertEqual(report.pongs_received, 2)  # type: ignore[attr-defined]
        self.assertEqual(report.pongs_missed, 0)  # type: ignore[attr-defined]
        self.assertEqual(report.elapsed_seconds, 60)  # type: ignore[attr-defined]
        self.assertEqual(transport.sent, [JOIN_FRAME, "2", "2"])
        self.assertTrue(transport.closed)
        self.assertEqual(report.error, "")  # type: ignore[attr-defined]

    def test_interval_is_configurable(self) -> None:
        clock = VirtualClock()
        transport = ScriptedTransport([OPEN_FRAME, "3", "3", "3"])
        reporter = SessionDurationReporter(
            "open-id-1",
            "tok",
            ping_interval=10,
            transport_factory=lambda: transport,
            clock=clock,
            sleeper=clock.sleep,
        )
        report = reporter.run(30, confirm=True)
        self.assertEqual(report.pings_sent, 2)
        self.assertEqual(report.elapsed_seconds, 30)

    def test_server_ping_gets_pong(self) -> None:
        report, transport, _ = self._run([OPEN_FRAME, "2", "3"], 25)
        self.assertEqual(report.pings_sent, 1)  # type: ignore[attr-defined]
        self.assertEqual(report.pongs_received, 1)  # type: ignore[attr-defined]
        self.assertEqual(transport.sent, [JOIN_FRAME, "2", "3"])

    def test_connection_failure_soft_lands(self) -> None:
        class Failing:
            def connect(self, url: str) -> None:
                raise OSError("connection refused")

            def send(self, text: str) -> None:  # pragma: no cover
                raise AssertionError

            def recv(self) -> str:  # pragma: no cover
                raise AssertionError

            def close(self) -> None:
                pass

        reporter = SessionDurationReporter(
            "open-id-1", "tok", transport_factory=Failing
        )
        with self.assertLogs("kedaibiao.platforms.unipus.duration", level="WARNING"):
            report = reporter.run(60, confirm=True)
        self.assertFalse(report.connected)
        self.assertFalse(report.ok)
        self.assertIn("connection refused", report.error)
        # 软着陆：不抛给调用方
        with self.assertRaises(OSError):
            reporter.run(60, confirm=True, raise_on_error=True)

    def test_bad_open_frame_is_reported(self) -> None:
        with self.assertLogs("kedaibiao.platforms.unipus.duration", level="WARNING"):
            report, _, _ = self._run(["3"], 60)
        self.assertTrue(report.connected)  # type: ignore[attr-defined]
        self.assertIn("open", report.error)  # type: ignore[attr-defined]

    def test_missing_pong_terminates_after_threshold(self) -> None:
        with self.assertLogs("kedaibiao.platforms.unipus.duration", level="WARNING"):
            report, _, _ = self._run([OPEN_FRAME, ScriptedTransport.TIMEOUT], 120)
        self.assertEqual(report.pongs_missed, 2)  # type: ignore[attr-defined]
        self.assertIn("pong", report.error)  # type: ignore[attr-defined]
        self.assertEqual(report.pongs_received, 0)  # type: ignore[attr-defined]

    def test_custom_max_missed_pongs(self) -> None:
        clock = VirtualClock()
        transport = ScriptedTransport([OPEN_FRAME, ScriptedTransport.TIMEOUT])
        reporter = SessionDurationReporter(
            "open-id-1",
            "tok",
            max_missed_pongs=1,
            transport_factory=lambda: transport,
            clock=clock,
            sleeper=clock.sleep,
        )
        with self.assertLogs("kedaibiao.platforms.unipus.duration", level="WARNING"):
            report = reporter.run(120, confirm=True)
        self.assertEqual(report.pongs_missed, 1)
        self.assertIn("pong", report.error)

    def test_received_message_frames_are_recorded(self) -> None:
        report, _, _ = self._run([OPEN_FRAME, '42["duration",{"n":1}]', "3"], 25)
        self.assertEqual(len(report.received_frames), 1)  # type: ignore[attr-defined]
        self.assertIn('42["duration"', report.received_frames[0])  # type: ignore[attr-defined]

    def test_engine_close_frame_is_reported(self) -> None:
        with self.assertLogs("kedaibiao.platforms.unipus.duration", level="WARNING"):
            report, _, _ = self._run([OPEN_FRAME, ENGINE_CLOSE], 25)
        self.assertIn("close", report.error)  # type: ignore[attr-defined]


# --------------------------------------------------------------------------
# 参数校验与便捷 API
# --------------------------------------------------------------------------


class TestValidation(unittest.TestCase):
    def test_reporter_validates_interval_and_timeout(self) -> None:
        for kwargs in (
            {"ping_interval": 0},
            {"ping_interval": -1},
            {"ping_interval": float("inf")},
            {"timeout": 0},
            {"max_missed_pongs": 0},
        ):
            with self.assertRaises(DurationConfigError):
                SessionDurationReporter("open-id-1", "tok", **kwargs)  # type: ignore[arg-type]

    def test_run_validates_seconds(self) -> None:
        reporter = SessionDurationReporter("open-id-1", "tok")
        for seconds in (0, -1, float("nan"), float("inf"), True, "60"):
            with self.assertRaises(DurationConfigError):
                reporter.run(seconds)  # type: ignore[arg-type]
        with self.assertRaises(DurationConfigError):
            reporter.run(duration.MAX_DURATION_SECONDS + 1)

    def test_report_duration_wrapper_is_dry_by_default(self) -> None:
        report = report_duration(
            "open-id-1", "tok", 60, transport_factory=lambda: ExplodingTransport()
        )
        self.assertTrue(report.dry_run)
        self.assertFalse(report.connected)
        self.assertEqual(report.plan.ping_interval, DEFAULT_PING_INTERVAL)
        self.assertTrue(report.plan.url.startswith(f"wss://ucontent.unipus.cn{DURATION_PATH}"))

    def test_report_duration_live_with_fake_transport(self) -> None:
        clock = VirtualClock()
        transport = ScriptedTransport([OPEN_FRAME, "3"])
        report = report_duration(
            "open-id-1",
            "tok",
            25,
            confirm=True,
            transport_factory=lambda: transport,
            clock=clock,
            sleeper=clock.sleep,
        )
        self.assertTrue(report.connected)
        self.assertEqual(report.pings_sent, 1)
        self.assertEqual(transport.sent, [JOIN_FRAME, "2"])

    def test_apply_mask_roundtrip(self) -> None:
        key = b"\x11\x22\x33\x44"
        data = "学习时长".encode()
        self.assertEqual(apply_mask(apply_mask(data, key), key), data)
        with self.assertRaises(DurationConfigError):
            apply_mask(b"x", b"short")

    def test_expected_accept_matches_hashlib(self) -> None:
        key = base64.b64encode(b"0123456789abcdef").decode()
        manual = base64.b64encode(
            hashlib.sha1((key + duration.WS_GUID).encode()).digest()
        ).decode()
        self.assertEqual(expected_accept(key), manual)

# --------------------------------------------------------------------------
# Socket.IO 入房 / kick（形状来自 facts §10.3；未实测，所以只锁「按事实拼帧」）
# --------------------------------------------------------------------------


class TestJoinAndKickFrames(unittest.TestCase):
    def test_join_frame_exact(self) -> None:
        self.assertEqual(build_join_frame(), "40/userActivities,")
        self.assertEqual(build_join_frame("/room"), "40/room,")
        with self.assertRaises(DurationConfigError):
            build_join_frame("userActivities")

    def test_kick_payload_fields(self) -> None:
        payload = build_kick_payload("oid", "cmgt", "h5", now_ms=1_700_000_000_000)
        self.assertEqual(
            set(payload),
            {
                "module", "moduleGroup", "client", "url", "tag1", "tag2", "tag3",
                "appId", "serviceId", "sendTimestamp", "openId", "timer",
            },
        )
        self.assertEqual(payload["client"], KICK_CLIENT)
        self.assertEqual(payload["url"], KICK_PAGE_URL)
        self.assertEqual(payload["tag1"], "cmgt")
        self.assertEqual(payload["tag2"], "h5")
        self.assertEqual(payload["appId"], 116)
        self.assertEqual(payload["serviceId"], "point")
        self.assertEqual(payload["sendTimestamp"], 1_700_000_000_000)
        self.assertEqual(payload["timer"], payload["sendTimestamp"])
        self.assertEqual(
            json.loads(str(payload["tag3"])),
            {"microBlock": "cmgt/h5", "source": "cloud", "version": "1"},
        )
        # tag3 是字符串化 JSON，键按字母序输出
        self.assertEqual(
            payload["tag3"],
            '{"microBlock":"cmgt/h5","source":"cloud","version":"1"}',
        )

    def test_kick_frame_prefix_and_json(self) -> None:
        frame = build_kick_frame("oid", "cmgt", "h5", now_ms=1_700_000_000_000)
        prefix = "42/userActivities,1"
        self.assertTrue(frame.startswith(prefix + '["kick-v2",{'))
        array = json.loads(frame[len(prefix):])
        self.assertEqual(array[0], "kick-v2")
        self.assertEqual(array[1]["openId"], "oid")
        # JSON 键按字母序（模仿参考实现的 serde_json 默认行为）
        body = frame[len(prefix):]
        self.assertLess(body.index('"appId"'), body.index('"client"'))
        self.assertLess(body.index('"client"'), body.index('"module"'))

    def test_kick_payload_validation(self) -> None:
        with self.assertRaises(DurationConfigError):
            build_kick_payload("oid", "", "h5", now_ms=1)
        with self.assertRaises(DurationConfigError):
            build_kick_payload("oid", "cmgt", "", now_ms=1)
        with self.assertRaises(DurationConfigError):
            build_kick_payload("oid", "cmgt", "h5", now_ms=0)


class TestRSetParsing(unittest.TestCase):
    def test_flat_object(self) -> None:
        update = parse_r_set('42/userActivities,{"delta":60,"msUpdate":1700}')
        self.assertEqual(update, RSetUpdate(60.0, 1700))

    def test_nested_object(self) -> None:
        update = parse_r_set('42["x",{"nested":{"delta":12.5,"msUpdate":42}}]')
        self.assertEqual(update, RSetUpdate(12.5, 42))

    def test_non_r_set_frames(self) -> None:
        self.assertIsNone(parse_r_set("3"))
        self.assertIsNone(parse_r_set("2"))
        self.assertIsNone(parse_r_set("42/userActivities,not-json"))
        self.assertIsNone(parse_r_set("42/userActivities,[1,2,3]"))
        self.assertIsNone(parse_r_set('42/userActivities,{"delta":"x","msUpdate":1}'))


class TestKickSession(unittest.TestCase):
    def _reporter(self, transport: ScriptedTransport, clock: VirtualClock, **kwargs: object):
        return SessionDurationReporter(
            "open-id-1",
            "tok",
            transport_factory=lambda: transport,
            clock=clock,
            sleeper=clock.sleep,
            wall_clock_ms=lambda: 1_700_000_000_000,
            enable_kick=True,
            module="cmgt",
            module_group="h5",
            **kwargs,  # type: ignore[arg-type]
        )

    def test_kick_sent_once_after_ack(self) -> None:
        clock = VirtualClock()
        transport = ScriptedTransport([OPEN_FRAME, "40/userActivities,", "3"])
        report = self._reporter(transport, clock).run(25, confirm=True)
        self.assertTrue(report.join_frame_sent)
        self.assertTrue(report.namespace_acked)
        self.assertTrue(report.kick_sent)
        self.assertTrue(report.ok)
        self.assertEqual(len(transport.sent), 3)
        self.assertEqual(transport.sent[0], JOIN_FRAME)
        self.assertTrue(transport.sent[1].startswith("42/userActivities,1"))
        self.assertIn('"openId":"open-id-1"', transport.sent[1])
        self.assertIn('"sendTimestamp":1700000000000', transport.sent[1])
        self.assertEqual(transport.sent[2], "2")
        self.assertEqual(
            sum(frame.startswith("42/userActivities") for frame in transport.sent), 1
        )

    def test_no_ack_skips_kick(self) -> None:
        clock = VirtualClock()
        transport = ScriptedTransport([OPEN_FRAME, "3"])
        report = self._reporter(transport, clock).run(25, confirm=True)
        self.assertTrue(report.join_frame_sent)
        self.assertFalse(report.namespace_acked)
        self.assertFalse(report.kick_sent)
        self.assertTrue(any("入房回执" in warning for warning in report.warnings))
        self.assertEqual(transport.sent, [JOIN_FRAME, "2"])
        self.assertTrue(report.ok)  # 跳 kick 是软着陆，不是错误

    def test_default_confirm_never_kicks(self) -> None:
        clock = VirtualClock()
        transport = ScriptedTransport([OPEN_FRAME, "3"])
        report = SessionDurationReporter(
            "open-id-1",
            "tok",
            transport_factory=lambda: transport,
            clock=clock,
            sleeper=clock.sleep,
        ).run(25, confirm=True)
        self.assertTrue(report.join_frame_sent)
        self.assertFalse(report.kick_sent)
        self.assertFalse(any(frame.startswith("42/userActivities") for frame in transport.sent))

    def test_join_namespace_can_be_disabled(self) -> None:
        clock = VirtualClock()
        transport = ScriptedTransport([OPEN_FRAME, "3"])
        report = SessionDurationReporter(
            "open-id-1",
            "tok",
            join_namespace=False,
            transport_factory=lambda: transport,
            clock=clock,
            sleeper=clock.sleep,
        ).run(25, confirm=True)
        self.assertFalse(report.join_frame_sent)
        self.assertEqual(transport.sent, ["2"])

    def test_kick_requires_join_and_module(self) -> None:
        with self.assertRaises(DurationConfigError):
            SessionDurationReporter("open-id-1", "tok", enable_kick=True)
        with self.assertRaises(DurationConfigError):
            SessionDurationReporter(
                "open-id-1", "tok", enable_kick=True, module="cmgt", module_group="h5",
                join_namespace=False,
            )


class TestCreditedObservation(unittest.TestCase):
    def test_r_set_delta_accumulates_and_dedupes(self) -> None:
        clock = VirtualClock()
        transport = ScriptedTransport(
            [
                OPEN_FRAME,
                '42/userActivities,{"delta":60,"msUpdate":111}',
                '42/userActivities,{"delta":60,"msUpdate":111}',  # 同一 msUpdate：去重
                "3",
            ]
        )
        reporter = SessionDurationReporter(
            "open-id-1",
            "tok",
            transport_factory=lambda: transport,
            clock=clock,
            sleeper=clock.sleep,
        )
        report = reporter.run(25, confirm=True)
        self.assertEqual(report.r_set_updates, 1)
        self.assertEqual(report.credited_seconds, 60.0)
        self.assertEqual(len(report.received_frames), 2)

    def test_credited_is_observation_only_not_stop_condition(self) -> None:
        # 记账已经达到 60s，但墙钟只跑 5s：默认仍按墙钟停，不会提前/延后
        clock = VirtualClock()
        transport = ScriptedTransport([OPEN_FRAME])
        report = SessionDurationReporter(
            "open-id-1",
            "tok",
            transport_factory=lambda: transport,
            clock=clock,
            sleeper=clock.sleep,
        ).run(5, confirm=True)
        self.assertEqual(report.elapsed_seconds, 5)
        self.assertEqual(report.credited_seconds, 0.0)



if __name__ == "__main__":
    unittest.main()


# --------------------------------------------------------------------------
# 真实任务模式（刷课）：unipusio + start/stop
# --------------------------------------------------------------------------


class TestTaskModeFrames(unittest.TestCase):
    def _target(self) -> Any:
        return duration.TaskTarget(
            instance="course-v2:abc+book+2024", unit="u1", group="u1g2", name="Exercise 1"
        )

    def test_task_url_uses_unipusio_path(self) -> None:
        url = duration.build_task_duration_url("open-1", "tok")
        self.assertIn("/unipusio/", url)
        self.assertIn("EIO=3", url)
        self.assertIn("uuid=open-1", url)
        self.assertNotIn("/unipusiopoint/", url)

    def test_task_url_rejects_other_host(self) -> None:
        with self.assertRaises(duration.DurationProtocolError):
            duration.assert_task_duration_url("wss://evil.example.com/unipusio/")

    def test_page_base_shape(self) -> None:
        base = duration.build_task_page_base(
            class_id="123", curricula_id="456", course_resource_id="789"
        )
        self.assertIn("/_explorationpc_default/pc.html?", base)
        self.assertIn("cid=123", base)
        self.assertIn("cloudCurriculaId=456", base)
        self.assertIn("courseResourceId=789", base)

    def test_page_base_without_resource_id(self) -> None:
        base = duration.build_task_page_base(class_id="1", curricula_id="2")
        self.assertNotIn("courseResourceId", base)

    def test_task_page_url_and_tag3(self) -> None:
        target = self._target()
        url = target.page_url("https://ucontent.unipus.cn/pc.html?x=1")
        self.assertTrue(url.endswith("#/course-v2:abc+book+2024/courseware/u1/u1/u1g2"))
        self.assertEqual(
            json.loads(target.tag3()),
            {"microBlock": "u1/u1", "version": "1", "source": "ucontent"},
        )

    def test_join_frame_has_query_and_trailing_comma(self) -> None:
        frame = duration.build_task_join_frame("open-1", "tok")
        self.assertTrue(frame.startswith("40/userActivities?uuid=open-1"))
        self.assertIn("token=tok", frame)
        self.assertTrue(frame.endswith(","))

    def test_start_frame_shape(self) -> None:
        frame = duration.build_task_start_frame(self._target(), page_base="PB")
        self.assertTrue(frame.startswith('42/userActivities,0["start",'))
        payload = json.loads(frame.split('42/userActivities,0', 1)[1])
        event, body = payload
        self.assertEqual(event, "start")
        self.assertEqual(body["module"], "u1g2")
        self.assertEqual(body["moduleGroup"], "course-v2:abc+book+2024")
        self.assertEqual(body["client"], "U校园pc")
        self.assertEqual(body["tag1"], "u1")
        self.assertTrue(body["url"].startswith("PB#/"))
        self.assertNotIn("timer", body)

    def test_stop_frame_carries_timer(self) -> None:
        frame = duration.build_task_stop_frame(
            self._target(), page_base="PB", timer_ms=1700000000000
        )
        self.assertTrue(frame.startswith("42/userActivities,2"))
        body = json.loads(frame.split('42/userActivities,2', 1)[1])[1]
        self.assertEqual(body["timer"], 1700000000000)


class TestTaskModeRun(unittest.TestCase):
    def _target(self) -> Any:
        return duration.TaskTarget(instance="inst", unit="u1", group="u1g1")

    def test_dry_run_never_connects(self) -> None:
        report = duration.run_task_duration(
            "open-1", "tok", [self._target()], seconds=300, page_base="PB",
            transport_factory=ExplodingTransport,
        )
        self.assertTrue(report.dry_run)
        self.assertFalse(report.connected)
        self.assertEqual(report.starts_sent, 0)
        self.assertTrue(report.plan.start_frames)
        self.assertTrue(report.plan.join_frame)

    def test_empty_targets_rejected(self) -> None:
        with self.assertRaises(duration.DurationConfigError):
            duration.run_task_duration("open-1", "tok", [], seconds=60, page_base="PB")

    def test_short_session_warns_about_ramp(self) -> None:
        plan = duration.plan_task_duration(
            "open-1", "tok", [self._target()], seconds=30, page_base="PB"
        )
        self.assertTrue(any("60" in w for w in plan.warnings))

    def test_run_sends_join_start_and_stop(self) -> None:
        r_set = '42/userActivities,["r_set",{"delta":20,"msUpdate":1000}]'
        transport = ScriptedTransport(["0{\"sid\":\"x\"}", "40/userActivities,", r_set])
        ticks = {"n": 0}

        def clock() -> float:
            ticks["n"] += 1
            return float(ticks["n"])   # 每次调用推进 1 秒，留够处理帧的轮次

        report = duration.run_task_duration(
            "open-1", "tok", [self._target()], seconds=20, page_base="PB",
            confirm=True, transport_factory=lambda: transport,
            clock=clock, sleeper=lambda _s: None,
        )
        self.assertTrue(report.connected)
        self.assertTrue(report.joined)
        self.assertGreaterEqual(report.starts_sent, 1)
        self.assertEqual(report.credited_seconds, 20)
        self.assertTrue(any("40/userActivities?" in f for f in transport.sent))
        self.assertTrue(any(f.startswith('42/userActivities,0["start"') for f in transport.sent))
        self.assertTrue(any(f.startswith('42/userActivities,2["stop"') for f in transport.sent))
        self.assertTrue(transport.closed)

    def test_duplicate_r_set_not_double_counted(self) -> None:
        r_set = '42/userActivities,["r_set",{"delta":20,"msUpdate":1000}]'
        transport = ScriptedTransport(["0{}", "40/userActivities,", r_set, r_set])
        ticks = {"n": 0}

        def clock() -> float:
            ticks["n"] += 1
            return float(ticks["n"])

        report = duration.run_task_duration(
            "open-1", "tok", [self._target()], seconds=10, page_base="PB",
            confirm=True, transport_factory=lambda: transport,
            clock=clock, sleeper=lambda _s: None,
        )
        self.assertEqual(report.credited_seconds, 20, "同一 msUpdate 只能记一次")

    def test_connection_error_lands_softly(self) -> None:
        report = duration.run_task_duration(
            "open-1", "tok", [self._target()], seconds=60, page_base="PB",
            confirm=True, transport_factory=ExplodingTransport,
        )
        self.assertFalse(report.ok)
        self.assertTrue(report.error)


class TestRoomPool(unittest.TestCase):
    """多房间刷时长：单房间服务端不记账（实测），所以至少 2 个房间。"""

    def test_pool_order_and_size(self) -> None:
        pool = duration.build_room_pool(6)
        self.assertEqual(len(pool), 6)
        self.assertEqual(pool[0], ("cmgt", "h5"))
        self.assertEqual(pool[5], ("tla", "h5"))

    def test_pool_clamped_to_max(self) -> None:
        pool = duration.build_room_pool(9999)
        self.assertEqual(len(pool), duration.MAX_ROOMS)

    def test_pool_rejects_zero(self) -> None:
        with self.assertRaises(duration.DurationConfigError):
            duration.build_room_pool(0)

    def test_plan_warns_single_room(self) -> None:
        plan = duration.plan_duration_pool("oid", "tok", seconds=600, rooms=1)
        self.assertTrue(any("不记账" in w for w in plan.warnings))
        self.assertEqual(len(plan.rooms), 1)

    def test_plan_estimates_ratio(self) -> None:
        plan = duration.plan_duration_pool("oid", "tok", seconds=600, rooms=6)
        self.assertAlmostEqual(plan.estimated_ratio, 4.2, places=2)
        self.assertIn("6 个房间", plan.summary())

    def test_pool_dry_run_never_connects(self) -> None:
        report = duration.run_duration_pool("oid", "tok", seconds=60, rooms=6)
        self.assertTrue(report.dry_run)
        self.assertEqual(report.credited_seconds, 0)
        self.assertEqual(report.rooms_connected, 0)

    def test_pool_aggregates_rooms(self) -> None:
        def fake_reporter(module: str, group: str):
            return duration.DurationReport(
                plan=duration.plan_duration_pool("oid", "tok", seconds=60, rooms=1),
                dry_run=False,
                connected=True,
                kick_sent=True,
                credited_seconds=100.0,
                r_set_updates=5,
            )

        report = duration.run_duration_pool(
            "oid", "tok", seconds=60, rooms=3, confirm=True, reporter=fake_reporter
        )
        self.assertEqual(report.rooms_connected, 3)
        self.assertEqual(report.rooms_kicked, 3)
        self.assertEqual(report.credited_seconds, 300.0)
        self.assertEqual(report.r_set_updates, 15)
