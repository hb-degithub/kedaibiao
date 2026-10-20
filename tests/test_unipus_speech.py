"""U校园语音评测模块测试（**完全离线**：假 socket、假连接、固定时间戳）。

覆盖四件事：

1. **签名与认证帧是纯函数**：固定 timestamp → 固定 sig（向量锁死）；
2. **干跑不建连接**：``run_speech_eval()`` 默认连 ``socket.create_connection``
   都不该碰；
3. **未知项是显式参数**：``upload_mode`` 默认 ``none``、帧顺序可换、结果结构
   只做启发式分类；
4. **错误软着陆**：连不上 / 握手失败 / 对端关闭 / 服务端报错 / 非 JSON，
   全部落成 outcome 的 ``status`` + ``error``，不抛异常。

**没有任何一条测试访问真实站点**：所有 socket 都是本地假对象。
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kedaibiao.platforms.unipus import speech

LOGGER_NAME = speech.__name__


def setUpModule() -> None:
    # 这些测试会大量触发 WARNING/INFO（软着陆路径就是被测行为）。把模块 logger
    # 压到 CRITICAL，避免整跑输出被淹没；需要断言的用例自己用 assertLogs 抬起来。
    logging.getLogger(LOGGER_NAME).setLevel(logging.CRITICAL)


def tearDownModule() -> None:
    logging.getLogger(LOGGER_NAME).setLevel(logging.NOTSET)

#: 测试用的固定时间戳（2023-11-14T22:13:20Z）
TS = "1700000000"
#: 由 ``SHA1("138000000000001" + secret + TS)`` 算出的期望摘要（小写）
TS_SIG = "b6f8dff0daa5ff41e42443d7f8c7a0ed50ffbb8b"
TS_SIG_UPPER = "B6F8DFF0DAA5FF41E42443D7F8C7A0ED50FFBB8B"
_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


# --------------------------------------------------------------------------
# 假 socket（RFC 6455 离线测试用）
# --------------------------------------------------------------------------


class FakeSocket:
    """最小的假 socket：记录发出的字节，按队列吐出收到的字节。"""

    def __init__(
        self,
        *,
        status_line: str = "101 Switching Protocols",
        accept_override: str | None = None,
        close_after_handshake: bool = False,
    ) -> None:
        self.sent = bytearray()
        self.incoming = bytearray()
        self.timeout: float | None = None
        self.closed = False
        self._status_line = status_line
        self._accept_override = accept_override
        self._close_after_handshake = close_after_handshake
        self._handshaken = False

    # -- socket 协议 ------------------------------------------------------

    def settimeout(self, timeout: float | None) -> None:
        self.timeout = timeout

    def sendall(self, data: bytes) -> None:
        self.sent += data
        if not self._handshaken and bytes(data).startswith(b"GET "):
            self._handshaken = True
            request = bytes(data).decode("latin-1")
            key = ""
            for line in request.split("\r\n"):
                if line.lower().startswith("sec-websocket-key:"):
                    key = line.split(":", 1)[1].strip()
            accept = self._accept_override
            if accept is None:
                accept = base64.b64encode(
                    hashlib.sha1((key + _WS_GUID).encode("ascii")).digest()
                ).decode("ascii")
            response = (
                f"HTTP/1.1 {self._status_line}\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
            )
            if not self._close_after_handshake:
                self.incoming += response.encode("latin-1")

    def recv(self, size: int) -> bytes:
        if not self.incoming:
            return b""
        chunk = bytes(self.incoming[:size])
        del self.incoming[:size]
        return chunk

    def close(self) -> None:
        self.closed = True

    # -- 测试辅助 ---------------------------------------------------------

    def feed(self, data: bytes) -> None:
        self.incoming += data

    @property
    def handshake_request(self) -> str:
        head = bytes(self.sent).split(b"\r\n\r\n", 1)[0]
        return head.decode("latin-1")

    def client_frames(self) -> list[tuple[int, bool, bytes]]:
        """把手握之后发出去的帧解析成 ``(opcode, masked, payload)``。"""
        data = bytes(self.sent)
        index = data.find(b"\r\n\r\n")
        if index < 0:
            return []
        data = data[index + 4:]
        frames: list[tuple[int, bool, bytes]] = []
        while len(data) >= 2:
            opcode = data[0] & 0x0F
            masked = bool(data[1] & 0x80)
            length = data[1] & 0x7F
            offset = 2
            if length == 126:
                length = struct.unpack("!H", data[offset:offset + 2])[0]
                offset += 2
            elif length == 127:
                length = struct.unpack("!Q", data[offset:offset + 8])[0]
                offset += 8
            mask = data[offset:offset + 4] if masked else b""
            if masked:
                offset += 4
            payload = data[offset:offset + length]
            offset += length
            if masked:
                payload = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
            frames.append((opcode, masked, payload))
            data = data[offset:]
        return frames


def server_frame(opcode: int, payload: bytes, *, fin: bool = True) -> bytes:
    """构造一帧**服务端→客户端**的帧（不掩码）。"""
    header = bytearray([(0x80 if fin else 0x00) | opcode])
    length = len(payload)
    if length < 126:
        header.append(length)
    elif length < 65536:
        header.append(126)
        header += struct.pack("!H", length)
    else:
        header.append(127)
        header += struct.pack("!Q", length)
    return bytes(header) + payload


def open_connection(sock: FakeSocket, url: str = speech.SPEECH_WS_PRIMARY) -> speech.WssConnection:
    connection = speech.WssConnection(
        url,
        timeout=1.0,
        socket_factory=lambda host, port, timeout: sock,
        tls_wrapper=lambda raw, host, timeout: raw,
    )
    return connection.connect()


# --------------------------------------------------------------------------
# 签名 / 时间戳
# --------------------------------------------------------------------------


class TestSignature(unittest.TestCase):
    def test_vector_matches_sha1_of_concatenation(self) -> None:
        expected = hashlib.sha1(
            (speech.SPEECH_APPLICATION_ID + speech.SPEECH_SECRET + TS).encode()
        ).hexdigest()
        self.assertEqual(speech.sign_speech(timestamp=TS), expected)
        # 向量本身也钉住（防止常量被悄悄改掉后测试跟着一起变）
        self.assertEqual(expected, TS_SIG)

    def test_fixed_timestamp_gives_fixed_sig(self) -> None:
        self.assertEqual(speech.sign_speech(timestamp=TS), TS_SIG)
        self.assertEqual(speech.sign_speech(timestamp=int(TS)), TS_SIG)

    def test_uppercase_option(self) -> None:
        self.assertEqual(speech.sign_speech(timestamp=TS, uppercase=True), TS_SIG_UPPER)

    def test_timestamp_changes_signature(self) -> None:
        self.assertNotEqual(
            speech.sign_speech(timestamp=TS),
            speech.sign_speech(timestamp=str(int(TS) + 1)),
        )

    def test_application_id_and_secret_are_source_facts(self) -> None:
        self.assertEqual(speech.SPEECH_APPLICATION_ID, "138000000000001")
        self.assertEqual(speech.SPEECH_SECRET, "example-speech-eval-secret-0000000001")
        self.assertEqual(speech.SPEECH_ALG, "sha1")

    def test_normalize_timestamp_accepts_int_str_and_clock(self) -> None:
        self.assertEqual(speech.normalize_timestamp(TS), TS)
        self.assertEqual(speech.normalize_timestamp(1700000000), TS)
        self.assertEqual(speech.normalize_timestamp(" 1700000000 "), TS)
        self.assertEqual(speech.normalize_timestamp(clock=lambda: 1700000000.9), TS)

    def test_normalize_timestamp_rejects_bad_values(self) -> None:
        for bad in (-1, "12a", "", True, 1.5, object()):
            with self.subTest(bad=bad), self.assertRaises(speech.SpeechEvalProtocolError):
                speech.normalize_timestamp(bad)  # type: ignore[arg-type]

    def test_empty_secret_rejected(self) -> None:
        with self.assertRaises(speech.SpeechEvalProtocolError):
            speech.sign_speech(timestamp=TS, secret="")


class TestAuthFrame(unittest.TestCase):
    def test_shape_and_key_order(self) -> None:
        frame = speech.build_auth_frame(timestamp=TS)
        self.assertEqual(list(frame), ["applicationId", "timestamp", "sig", "alg"])
        self.assertEqual(frame["applicationId"], speech.SPEECH_APPLICATION_ID)
        self.assertEqual(frame["timestamp"], TS)
        self.assertEqual(frame["alg"], "sha1")
        self.assertEqual(frame["sig"], TS_SIG)
        self.assertIsInstance(frame["timestamp"], str, "timestamp 必须是秒级字符串")

    def test_secret_not_included_by_default(self) -> None:
        self.assertNotIn("secret", speech.build_auth_frame(timestamp=TS))

    def test_include_secret_is_explicit(self) -> None:
        frame = speech.build_auth_frame(timestamp=TS, include_secret=True)
        self.assertEqual(frame["secret"], speech.SPEECH_SECRET)

    def test_extra_fields_are_appended_and_duplicates_rejected(self) -> None:
        frame = speech.build_auth_frame(timestamp=TS, extra={"nonce": "abc"})
        self.assertEqual(frame["nonce"], "abc")
        self.assertEqual(list(frame)[-1], "nonce")
        with self.assertRaises(speech.SpeechEvalProtocolError):
            speech.build_auth_frame(timestamp=TS, extra={"sig": "x"})

    def test_json_is_compact_and_ordered(self) -> None:
        text = speech.auth_frame_json(speech.build_auth_frame(timestamp=TS))
        self.assertNotIn(": ", text, "紧凑 JSON 不应有空格")
        self.assertTrue(text.startswith('{"applicationId":"138000000000001","timestamp":"1700000000"'))
        self.assertEqual(json.loads(text)["sig"], TS_SIG)


# --------------------------------------------------------------------------
# 计划与帧（纯函数）
# --------------------------------------------------------------------------


class TestPlan(unittest.TestCase):
    def test_default_is_auth_only(self) -> None:
        plan = speech.plan_speech_eval(timestamp=TS)
        self.assertEqual(len(plan.frames), 1)
        self.assertEqual(plan.frames[0].kind, "text")
        self.assertEqual(plan.upload_mode, speech.UPLOAD_NONE)
        self.assertEqual(plan.fallback_urls, (speech.SPEECH_WS_BACKUP,))
        self.assertTrue(any("upload_mode" in warning for warning in plan.warnings))

    def test_auth_frame_first_by_default(self) -> None:
        plan = speech.plan_speech_eval(
            timestamp=TS, audio=b"RIFF", upload_mode=speech.UPLOAD_BINARY
        )
        self.assertEqual([frame.kind for frame in plan.frames], ["text", "binary"])
        self.assertEqual(plan.frames[0].payload, speech.auth_frame_json(plan.auth_frame))

    def test_auth_first_can_be_disabled_with_warning(self) -> None:
        plan = speech.plan_speech_eval(
            timestamp=TS, audio=b"RIFF", upload_mode=speech.UPLOAD_BINARY, auth_first=False
        )
        self.assertEqual([frame.kind for frame in plan.frames], ["binary", "text"])
        self.assertTrue(any("帧顺序" in warning for warning in plan.warnings))

    def test_binary_upload_keeps_raw_bytes(self) -> None:
        plan = speech.plan_speech_eval(
            timestamp=TS, audio=b"\x00\x01\x02", upload_mode=speech.UPLOAD_BINARY
        )
        self.assertEqual(plan.frames[1].payload, b"\x00\x01\x02")
        self.assertEqual(plan.frames[1].size, 3)

    def test_base64_json_upload_shape(self) -> None:
        plan = speech.plan_speech_eval(
            timestamp=TS,
            audio=b"abc",
            reference_text="hello",
            upload_mode=speech.UPLOAD_BASE64_JSON,
        )
        payload = json.loads(str(plan.frames[1].payload))
        self.assertEqual(base64.b64decode(payload["audio"]), b"abc")
        self.assertEqual(payload["text"], "hello")

    def test_base64_json_custom_keys(self) -> None:
        plan = speech.plan_speech_eval(
            timestamp=TS, audio=b"abc", upload_mode=speech.UPLOAD_BASE64_JSON,
            audio_key="audio_data", text_key="ref_text", reference_text="hi",
        )
        payload = json.loads(str(plan.frames[1].payload))
        self.assertIn("audio_data", payload)
        self.assertIn("ref_text", payload)

    def test_chunking_splits_frames(self) -> None:
        plan = speech.plan_speech_eval(
            timestamp=TS, audio=b"0123456789", upload_mode=speech.UPLOAD_BINARY, chunk_size=4
        )
        binary = [frame for frame in plan.frames if frame.kind == "binary"]
        self.assertEqual([frame.payload for frame in binary], [b"0123", b"4567", b"89"])

    def test_audio_from_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.wav"
            path.write_bytes(b"wave-bytes")
            self.assertEqual(speech.read_audio_bytes(path), b"wave-bytes")
            plan = speech.plan_speech_eval(
                timestamp=TS, audio=str(path), upload_mode=speech.UPLOAD_BINARY
            )
            self.assertEqual(plan.frames[1].payload, b"wave-bytes")

    def test_missing_audio_file_rejected(self) -> None:
        with self.assertRaises(speech.SpeechEvalProtocolError) as cm:
            speech.plan_speech_eval(
                timestamp=TS, audio="/nonexistent/a.wav", upload_mode=speech.UPLOAD_BINARY
            )
        self.assertIn("不存在", str(cm.exception))

    def test_empty_audio_rejected(self) -> None:
        with self.assertRaises(speech.SpeechEvalProtocolError):
            speech.plan_speech_eval(
                timestamp=TS, audio=b"", upload_mode=speech.UPLOAD_BINARY
            )

    def test_unknown_upload_mode_rejected(self) -> None:
        with self.assertRaises(speech.SpeechEvalProtocolError):
            speech.plan_speech_eval(timestamp=TS, upload_mode="telepathy")

    def test_audio_without_upload_mode_warns(self) -> None:
        plan = speech.plan_speech_eval(timestamp=TS, audio=b"abc")
        self.assertEqual(len(plan.frames), 1)
        self.assertTrue(any("音频不会被发送" in warning for warning in plan.warnings))

    def test_opt_in_upload_mode_still_warns_unverified(self) -> None:
        """显式开上传不等于「已验证」：告警必须仍在。"""
        plan = speech.plan_speech_eval(
            timestamp=TS, audio=b"abc", upload_mode=speech.UPLOAD_BINARY
        )
        joined = "；".join(plan.warnings)
        self.assertIn("未验证", joined)
        self.assertIn("U9", joined)

    def test_non_unipus_host_rejected(self) -> None:
        with self.assertRaises(speech.SpeechEvalProtocolError):
            speech.plan_speech_eval(timestamp=TS, url="wss://example.com/speech/proxy/wss")

    def test_backup_can_be_disabled(self) -> None:
        plan = speech.plan_speech_eval(timestamp=TS, backup=False)
        self.assertEqual(plan.fallback_urls, ())

    def test_describe_masks_secret(self) -> None:
        plan = speech.plan_speech_eval(timestamp=TS, include_secret=True)
        description = plan.describe()
        self.assertNotIn(speech.SPEECH_SECRET, description)
        self.assertIn("***", description)
        # to_dict 是审计用的，保留原文（文档已警示不要写日志）
        self.assertEqual(plan.to_dict()["auth_frame"]["secret"], speech.SPEECH_SECRET)

    def test_describe_counts_frames(self) -> None:
        plan = speech.plan_speech_eval(
            timestamp=TS, audio=b"0123456789", upload_mode=speech.UPLOAD_BINARY, chunk_size=4
        )
        description = plan.describe()
        self.assertIn("共 4 帧", description)
        self.assertIn("音频 3 帧", description)

    def test_to_dict_is_json_serializable(self) -> None:
        plan = speech.plan_speech_eval(
            timestamp=TS, audio=b"abc", upload_mode=speech.UPLOAD_BASE64_JSON
        )
        json.dumps(plan.to_dict()["frames"], ensure_ascii=False)

    def test_param_frame_goes_between_auth_and_audio(self) -> None:
        """调研摘要的帧序列：认证帧 → 参数帧 → PCM 分片 → 结束帧。"""
        plan = speech.plan_speech_eval(
            timestamp=TS,
            audio=b"0123",
            upload_mode=speech.UPLOAD_BINARY,
            chunk_size=2,
            param_frame={"tokenId": "t-1", "audio": {"format": "pcm"}, "request": {}},
            send_stop=True,
        )
        self.assertEqual(
            [frame.kind for frame in plan.frames], ["text", "text", "binary", "binary", "text"]
        )
        param = json.loads(str(plan.frames[1].payload))
        self.assertEqual(param["tokenId"], "t-1")
        self.assertEqual(json.loads(str(plan.frames[-1].payload)), {"stop": True})

    def test_param_frame_and_stop_are_off_by_default(self) -> None:
        plan = speech.plan_speech_eval(
            timestamp=TS, audio=b"x", upload_mode=speech.UPLOAD_BINARY
        )
        self.assertNotIn("stop", "".join(str(frame.payload) for frame in plan.frames))
        self.assertTrue(any("参数帧" in w or "结束帧" in w for w in plan.warnings) is False)

    def test_stop_frame_can_be_overridden(self) -> None:
        plan = speech.plan_speech_eval(
            timestamp=TS, send_stop=True, stop_frame={"stop": True, "seq": 3}
        )
        self.assertEqual(json.loads(str(plan.frames[-1].payload)), {"stop": True, "seq": 3})

    def test_empty_param_frame_rejected(self) -> None:
        with self.assertRaises(speech.SpeechEvalProtocolError):
            speech.plan_speech_eval(timestamp=TS, param_frame={})

    def test_param_and_stop_raise_warnings(self) -> None:
        plan = speech.plan_speech_eval(
            timestamp=TS, param_frame={"tokenId": "t"}, send_stop=True
        )
        joined = "；".join(plan.warnings)
        self.assertIn("参数帧", joined)
        self.assertIn("结束帧", joined)
        self.assertIn("未验证", joined)

    def test_research_frame_hints_are_registered(self) -> None:
        self.assertEqual(speech.SPEECH_PARAM_FRAME_HINTS, ("tokenId", "audio", "request"))
        self.assertEqual(speech.SPEECH_STOP_FRAME, {"stop": True})


# --------------------------------------------------------------------------
# 干跑：不建连接
# --------------------------------------------------------------------------


class TestDryRun(unittest.TestCase):
    def test_default_does_not_connect(self) -> None:
        connector = mock.Mock(side_effect=AssertionError("干跑不该建连接"))
        outcome = speech.run_speech_eval(connector=connector, timestamp=TS)
        self.assertEqual(outcome.status, speech.STATUS_DRY_RUN)
        self.assertTrue(outcome.ok is False)
        connector.assert_not_called()
        self.assertIsNotNone(outcome.plan)
        self.assertIn("干跑", outcome.summary())

    def test_default_does_not_touch_socket(self) -> None:
        with mock.patch.object(
            speech.socket, "create_connection", side_effect=AssertionError("不该建 socket")
        ) as factory:
            outcome = speech.run_speech_eval(timestamp=TS)
        self.assertEqual(outcome.status, speech.STATUS_DRY_RUN)
        factory.assert_not_called()

    def test_dry_run_flag_overrides_confirm(self) -> None:
        connector = mock.Mock(side_effect=AssertionError("不该建连接"))
        outcome = speech.run_speech_eval(
            confirm=True, dry_run=True, connector=connector, timestamp=TS,
            audio=b"abc", upload_mode=speech.UPLOAD_BINARY,
        )
        self.assertEqual(outcome.status, speech.STATUS_DRY_RUN)
        connector.assert_not_called()

    def test_confirm_without_upload_mode_is_blocked(self) -> None:
        connector = mock.Mock(side_effect=AssertionError("不该建连接"))
        outcome = speech.run_speech_eval(confirm=True, connector=connector, timestamp=TS)
        self.assertEqual(outcome.status, speech.STATUS_BLOCKED)
        self.assertIn("upload_mode", outcome.error)
        connector.assert_not_called()

    def test_confirm_with_upload_mode_but_no_audio_is_blocked(self) -> None:
        connector = mock.Mock(side_effect=AssertionError("不该建连接"))
        outcome = speech.run_speech_eval(
            confirm=True, connector=connector, timestamp=TS,
            upload_mode=speech.UPLOAD_BINARY,
        )
        self.assertEqual(outcome.status, speech.STATUS_BLOCKED)
        self.assertIn("没给音频", outcome.error)
        connector.assert_not_called()


# --------------------------------------------------------------------------
# 假连接上的主流程
# --------------------------------------------------------------------------


class FakeConnection:
    """假的已连接通道（契约与 :class:`speech.WssConnection` 一致）。"""

    def __init__(
        self,
        messages: list[object] | None = None,
        *,
        send_error: Exception | None = None,
        recv_error: Exception | None = None,
    ) -> None:
        self.messages = list(messages or [])
        self.send_error = send_error
        self.recv_error = recv_error
        self.sent: list[tuple[str, object]] = []
        self.url = ""
        self.close_count = 0

    def send_text(self, text: str) -> None:
        if self.send_error is not None:
            raise self.send_error
        self.sent.append(("text", text))

    def send_binary(self, data: bytes) -> None:
        if self.send_error is not None:
            raise self.send_error
        self.sent.append(("binary", data))

    def recv_json(self) -> object:
        if self.recv_error is not None:
            error, self.recv_error = self.recv_error, None
            raise error
        if not self.messages:
            raise speech.SpeechEvalConnectionClosed("没有更多消息")
        item = self.messages.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def close(self) -> None:
        self.close_count += 1


class TestRunWithFakeConnection(unittest.TestCase):
    def _run(self, connection: FakeConnection, **kwargs):
        calls: list[str] = []

        def connector(url: str, timeout: float) -> FakeConnection:
            calls.append(url)
            connection.url = url
            return connection

        outcome = speech.run_speech_eval(
            confirm=True,
            connector=connector,
            timestamp=TS,
            audio=b"\x00\x01",
            upload_mode=speech.UPLOAD_BINARY,
            **kwargs,
        )
        return outcome, calls

    def test_scored(self) -> None:
        connection = FakeConnection([{"overall": 86.5, "fluency": 0.9}])
        outcome, calls = self._run(connection)
        self.assertEqual(outcome.status, speech.STATUS_SCORED)
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.score_fields, {"overall": 86.5, "fluency": 0.9})
        self.assertEqual(calls, [speech.SPEECH_WS_PRIMARY])
        self.assertEqual(len(connection.sent), 2, "先认证帧，再音频帧")
        self.assertEqual(connection.sent[0][0], "text")
        self.assertEqual(json.loads(str(connection.sent[0][1]))["alg"], "sha1")
        self.assertEqual(connection.sent[1], ("binary", b"\x00\x01"))
        self.assertEqual(connection.close_count, 1, "连接必须被关闭")

    def test_server_error_is_rejected(self) -> None:
        connection = FakeConnection([{"code": 4001, "msg": "invalid audio"}])
        outcome, _ = self._run(connection)
        self.assertEqual(outcome.status, speech.STATUS_REJECTED)
        self.assertFalse(outcome.ok)
        self.assertTrue(outcome.needs_quarantine)
        self.assertIn("invalid audio", outcome.error)

    def test_unclassifiable_messages_are_kept(self) -> None:
        connection = FakeConnection([{"type": "ready"}])
        outcome, _ = self._run(connection)
        self.assertEqual(outcome.status, speech.STATUS_UNKNOWN)
        self.assertEqual(outcome.messages, [{"type": "ready"}])
        self.assertTrue(outcome.needs_quarantine)
        self.assertFalse(outcome.ok, "unknown 不能当「拿到分数」用")
        self.assertIn("无法判定", outcome.summary())

    def test_timeout_after_message_soft_lands(self) -> None:
        connection = FakeConnection([{"type": "ready"}, TimeoutError("timed out")])
        outcome, _ = self._run(connection)
        self.assertEqual(outcome.status, speech.STATUS_UNKNOWN)
        self.assertEqual(len(outcome.messages), 1)

    def test_closed_before_any_message(self) -> None:
        outcome, _ = self._run(FakeConnection([]))
        self.assertEqual(outcome.status, speech.STATUS_UNREACHABLE)
        self.assertIn("关闭", outcome.error)

    def test_send_failure_soft_lands(self) -> None:
        connection = FakeConnection(send_error=OSError("broken pipe"))
        outcome, _ = self._run(connection)
        self.assertEqual(outcome.status, speech.STATUS_UNREACHABLE)
        self.assertIn("broken pipe", outcome.error)

    def test_all_channels_unreachable_lists_both_hosts(self) -> None:
        calls: list[str] = []

        def connector(url: str, timeout: float) -> FakeConnection:
            calls.append(url)
            raise OSError("connection refused")

        outcome = speech.run_speech_eval(
            confirm=True, connector=connector, timestamp=TS,
            audio=b"x", upload_mode=speech.UPLOAD_BINARY,
        )
        self.assertEqual(outcome.status, speech.STATUS_UNREACHABLE)
        self.assertEqual(calls, [speech.SPEECH_WS_PRIMARY, speech.SPEECH_WS_BACKUP])
        self.assertIn("speech.unipus.cn", outcome.error)
        self.assertIn("speech-eval.unipus.cn", outcome.error)
        self.assertTrue(outcome.needs_quarantine)

    def test_fallback_can_be_disabled(self) -> None:
        calls: list[str] = []

        def connector(url: str, timeout: float) -> FakeConnection:
            calls.append(url)
            raise OSError("refused")

        speech.run_speech_eval(
            confirm=True, fallback=False, connector=connector, timestamp=TS,
            audio=b"x", upload_mode=speech.UPLOAD_BINARY,
        )
        self.assertEqual(calls, [speech.SPEECH_WS_PRIMARY])

    def test_primary_fails_backup_succeeds(self) -> None:
        good = FakeConnection([{"score": 90}])

        def connector(url: str, timeout: float) -> FakeConnection:
            if "speech-eval" not in url:
                raise OSError("primary down")
            good.url = url
            return good

        outcome = speech.run_speech_eval(
            confirm=True, connector=connector, timestamp=TS,
            audio=b"x", upload_mode=speech.UPLOAD_BINARY,
        )
        self.assertEqual(outcome.status, speech.STATUS_SCORED)
        self.assertEqual(outcome.url, speech.SPEECH_WS_BACKUP)
        self.assertEqual(outcome.score_fields, {"score": 90.0})

    def test_probe_only_sends_auth_frame_only(self) -> None:
        connection = FakeConnection([{"code": 0}])

        def connector(url: str, timeout: float) -> FakeConnection:
            return connection

        outcome = speech.run_speech_eval(
            confirm=True, probe_only=True, connector=connector, timestamp=TS,
        )
        self.assertEqual(outcome.status, speech.STATUS_UNKNOWN)
        self.assertEqual([kind for kind, _ in connection.sent], ["text"])
        self.assertEqual(outcome.messages, [{"code": 0}])

    def test_unknown_upload_mode_raises(self) -> None:
        with self.assertRaises(speech.SpeechEvalProtocolError):
            speech.run_speech_eval(confirm=True, upload_mode="telepathy", timestamp=TS)

    def test_non_unipus_url_raises(self) -> None:
        with self.assertRaises(speech.SpeechEvalProtocolError):
            speech.run_speech_eval(
                confirm=True, url="wss://evil.example.com/x", timestamp=TS,
                audio=b"x", upload_mode=speech.UPLOAD_BINARY,
            )


# --------------------------------------------------------------------------
# RFC 6455 客户端（假 socket，零网络）
# --------------------------------------------------------------------------


class TestWssConnection(unittest.TestCase):
    def test_handshake_request_shape(self) -> None:
        sock = FakeSocket()
        connection = open_connection(sock)
        request = sock.handshake_request
        self.assertTrue(request.startswith("GET /speech/proxy/wss HTTP/1.1"))
        self.assertIn("Host: speech.unipus.cn", request)
        self.assertIn("Upgrade: websocket", request)
        self.assertIn("Connection: Upgrade", request)
        self.assertIn("Sec-WebSocket-Key: ", request)
        self.assertIn("Sec-WebSocket-Version: 13", request)
        self.assertEqual(connection.host, "speech.unipus.cn")
        self.assertEqual(connection.port, 443)

    def test_extra_headers_are_sent(self) -> None:
        sock = FakeSocket()
        speech.WssConnection(
            speech.SPEECH_WS_PRIMARY,
            timeout=1.0,
            extra_headers={"Origin": "https://uai.unipus.cn"},
            socket_factory=lambda host, port, timeout: sock,
            tls_wrapper=lambda raw, host, timeout: raw,
        ).connect()
        self.assertIn("Origin: https://uai.unipus.cn", sock.handshake_request)

    def test_handshake_rejected(self) -> None:
        sock = FakeSocket(status_line="403 Forbidden")
        with self.assertRaises(speech.SpeechEvalConnectionError) as cm:
            open_connection(sock)
        self.assertIn("握手被拒", str(cm.exception))

    def test_accept_mismatch_rejected(self) -> None:
        sock = FakeSocket(accept_override="not-the-right-key")
        with self.assertRaises(speech.SpeechEvalConnectionError) as cm:
            open_connection(sock)
        self.assertIn("Accept", str(cm.exception))

    def test_connection_failure_wrapped(self) -> None:
        def factory(host: str, port: int, timeout: float):
            raise OSError("refused")

        connection = speech.WssConnection(
            speech.SPEECH_WS_PRIMARY, timeout=1.0, socket_factory=factory,
            tls_wrapper=lambda raw, host, timeout: raw,
        )
        with self.assertRaises(speech.SpeechEvalConnectionError) as cm:
            connection.connect()
        self.assertIn("refused", str(cm.exception))

    def test_non_unipus_host_rejected(self) -> None:
        with self.assertRaises(speech.SpeechEvalProtocolError):
            speech.WssConnection("wss://example.com/x")

    def test_plaintext_scheme_rejected(self) -> None:
        with self.assertRaises(speech.SpeechEvalProtocolError):
            speech.WssConnection("ws://speech.unipus.cn/x")

    def test_send_text_is_masked(self) -> None:
        sock = FakeSocket()
        connection = open_connection(sock)
        connection.send_text("你好")
        frames = sock.client_frames()
        self.assertEqual(len(frames), 1)
        opcode, masked, payload = frames[0]
        self.assertEqual(opcode, 0x1)
        self.assertTrue(masked, "客户端帧必须掩码（RFC 6455）")
        self.assertEqual(payload.decode(), "你好")

    def test_send_binary(self) -> None:
        sock = FakeSocket()
        connection = open_connection(sock)
        connection.send_binary(b"\x00\x01\x02")
        opcode, masked, payload = sock.client_frames()[0]
        self.assertEqual((opcode, masked, payload), (0x2, True, b"\x00\x01\x02"))

    def test_recv_json(self) -> None:
        sock = FakeSocket()
        connection = open_connection(sock)
        sock.feed(server_frame(0x1, b'{"overall":0.8}'))
        self.assertEqual(connection.recv_json(), {"overall": 0.8})

    def test_recv_non_json_keeps_raw_text(self) -> None:
        sock = FakeSocket()
        connection = open_connection(sock)
        sock.feed(server_frame(0x1, b"not json"))
        self.assertEqual(connection.recv_json(), {"_raw_text": "not json"})

    def test_recv_binary_is_not_decoded(self) -> None:
        sock = FakeSocket()
        connection = open_connection(sock)
        sock.feed(server_frame(0x2, b"abc"))
        self.assertEqual(connection.recv_json(), {"_binary": True, "_size": 3})

    def test_close_frame_raises_closed(self) -> None:
        sock = FakeSocket()
        connection = open_connection(sock)
        sock.feed(server_frame(0x8, b""))
        with self.assertRaises(speech.SpeechEvalConnectionClosed):
            connection.recv_json()

    def test_eof_raises_closed(self) -> None:
        sock = FakeSocket()
        connection = open_connection(sock)
        with self.assertRaises(speech.SpeechEvalConnectionClosed):
            connection.recv_json()

    def test_ping_is_answered_with_pong(self) -> None:
        sock = FakeSocket()
        connection = open_connection(sock)
        sock.feed(server_frame(0x9, b"hi"))
        sock.feed(server_frame(0x1, b'{"ok":true}'))
        self.assertEqual(connection.recv_json(), {"ok": True})
        pongs = [frame for frame in sock.client_frames() if frame[0] == 0xA]
        self.assertEqual([payload for _, _, payload in pongs], [b"hi"])

    def test_fragmented_message_is_assembled(self) -> None:
        sock = FakeSocket()
        connection = open_connection(sock)
        sock.feed(server_frame(0x1, b'{"a"', fin=False))
        sock.feed(server_frame(0x0, b':1}', fin=True))
        self.assertEqual(connection.recv_json(), {"a": 1})

    def test_close_is_idempotent_and_sends_close_frame(self) -> None:
        sock = FakeSocket()
        connection = open_connection(sock)
        connection.close()
        connection.close()
        self.assertTrue(sock.closed)
        self.assertEqual([frame[0] for frame in sock.client_frames()], [0x8])

    def test_send_before_connect_raises(self) -> None:
        connection = speech.WssConnection(speech.SPEECH_WS_PRIMARY, timeout=1.0)
        with self.assertRaises(speech.SpeechEvalConnectionError):
            connection.send_text("x")


# --------------------------------------------------------------------------
# 结果启发式
# --------------------------------------------------------------------------


class TestHeuristics(unittest.TestCase):
    def test_looks_like_error(self) -> None:
        cases = {
            "code 0": ({"code": 0}, False),
            "code '0'": ({"code": "0"}, False),
            "code 200": ({"code": 200}, False),
            "code 1": ({"code": 1}, True),
            "code str 4001": ({"code": "4001"}, True),
            "success false": ({"success": False}, True),
            "success true": ({"success": True}, False),
            "error string": ({"error": "bad"}, True),
            "status error": ({"status": "error"}, True),
            "status finished": ({"status": "finished"}, False),
            "not a dict": ("oops", False),
        }
        for name, (document, expected) in cases.items():
            with self.subTest(name):
                self.assertEqual(speech.looks_like_error(document), expected)

    def test_looks_like_result(self) -> None:
        self.assertTrue(speech.looks_like_result({"overall": 1}))
        self.assertTrue(speech.looks_like_result({"data": {"score": 0.5}}))
        self.assertTrue(speech.looks_like_result({"result": {"fluency": 0.9}}))
        self.assertFalse(speech.looks_like_result({"type": "ready"}))
        self.assertFalse(speech.looks_like_result("nope"))

    def test_collect_score_fields_nested(self) -> None:
        document = {"data": {"score": 0.5, "fluency": 87, "name": "x"}, "overall": "n/a"}
        self.assertEqual(
            speech.collect_score_fields(document),
            {"data.score": 0.5, "data.fluency": 87.0},
        )


# --------------------------------------------------------------------------
# 评分上报
# --------------------------------------------------------------------------


class TestVoiceEvaluationPlan(unittest.TestCase):
    def test_payload_keys_exact(self) -> None:
        plan = speech.build_voice_evaluation_plan(
            block_id="b-1", course_id="course-v2:x", score_details=[{"score": 0.9}],
            annotator_token="tok",
        )
        self.assertEqual(set(plan.payload), {"block_id", "course_id", "score_details"})
        self.assertEqual(plan.url, speech.VOICE_EVALUATION_URL)
        self.assertEqual(plan.headers["Content-Type"], "application/json")
        self.assertEqual(plan.headers[speech.UCONTENT_AUTH_HEADER], "tok")

    def test_ids_are_not_coerced(self) -> None:
        big = 1234567890123456789
        plan = speech.build_voice_evaluation_plan(
            block_id=big, course_id="course-v2:abc", score_details=[],
        )
        self.assertEqual(plan.payload["block_id"], big)
        self.assertEqual(plan.payload["course_id"], "course-v2:abc")

    def test_float_id_rejected(self) -> None:
        with self.assertRaises(speech.SpeechEvalProtocolError) as cm:
            speech.build_voice_evaluation_plan(
                block_id=1.5, course_id="c", score_details=[],
            )
        self.assertIn("浮点", str(cm.exception))

    def test_bool_and_empty_ids_rejected(self) -> None:
        for bad in (True, "", "   "):
            with self.subTest(bad=bad), self.assertRaises(speech.SpeechEvalProtocolError):
                speech.build_voice_evaluation_plan(
                    block_id=bad, course_id="c", score_details=[],
                )

    def test_score_details_passthrough(self) -> None:
        details = {"total": 88, "items": [1, 2, 3]}
        plan = speech.build_voice_evaluation_plan(
            block_id="b", course_id="c", score_details=details,
        )
        self.assertIs(plan.payload["score_details"], details, "原样透传，不改写")

    def test_unserializable_score_details_rejected(self) -> None:
        with self.assertRaises(speech.SpeechEvalProtocolError):
            speech.build_voice_evaluation_plan(
                block_id="b", course_id="c", score_details=object(),
            )

    def test_warnings_expose_unverified_parts(self) -> None:
        plan = speech.build_voice_evaluation_plan(
            block_id="b", course_id="c", score_details=[], annotator_token="tok",
        )
        joined = "；".join(plan.warnings)
        self.assertIn("score_details", joined)
        self.assertIn("未在规格", joined)
        self.assertIn("响应体", joined)

    def test_missing_token_warns(self) -> None:
        plan = speech.build_voice_evaluation_plan(
            block_id="b", course_id="c", score_details=[],
        )
        self.assertTrue(any("x-annotator-auth-token" in warning for warning in plan.warnings))

    def test_describe_masks_header_values(self) -> None:
        plan = speech.build_voice_evaluation_plan(
            block_id="b", course_id="c", score_details=[], annotator_token="SECRET-TOKEN",
        )
        self.assertNotIn("SECRET-TOKEN", plan.describe())
        self.assertIn(speech.UCONTENT_AUTH_HEADER, plan.describe())
        self.assertEqual(plan.to_dict()["headers"][speech.UCONTENT_AUTH_HEADER], "SECRET-TOKEN")

    def test_non_unipus_url_rejected(self) -> None:
        with self.assertRaises(speech.SpeechEvalProtocolError):
            speech.build_voice_evaluation_plan(
                block_id="b", course_id="c", score_details=[],
                url="https://example.com/course/api/v3/voiceEvaluation",
            )


class FakeSender:
    def __init__(self, document: object = None, *, error: Exception | None = None) -> None:
        self.document = document
        self.error = error
        self.calls: list[dict[str, object]] = []

    async def __call__(self, *, url, headers, payload):  # noqa: ANN001, ANN003
        self.calls.append({"url": url, "headers": dict(headers), "payload": dict(payload)})
        if self.error is not None:
            raise self.error
        return self.document


class TestVoiceEvaluationReport(unittest.IsolatedAsyncioTestCase):
    def _kwargs(self, **kwargs):
        base = dict(block_id="b-1", course_id="course-v2:x", score_details=[{"score": 0.9}],
                    annotator_token="tok")
        base.update(kwargs)
        return base

    async def test_dry_run_does_not_send(self) -> None:
        sender = FakeSender({"code": 0})
        outcome = await speech.report_voice_evaluation(sender=sender, **self._kwargs())
        self.assertEqual(outcome.status, speech.STATUS_DRY_RUN)
        self.assertEqual(sender.calls, [])
        self.assertIn("干跑", outcome.summary())

    async def test_confirm_without_sender_is_blocked(self) -> None:
        outcome = await speech.report_voice_evaluation(confirm=True, **self._kwargs())
        self.assertEqual(outcome.status, speech.STATUS_BLOCKED)
        self.assertIn("sender", outcome.error)

    async def test_reported(self) -> None:
        sender = FakeSender({"code": 0, "data": {}})
        outcome = await speech.report_voice_evaluation(
            confirm=True, sender=sender, **self._kwargs()
        )
        self.assertEqual(outcome.status, speech.STATUS_REPORTED)
        self.assertTrue(outcome.ok)
        self.assertEqual(len(sender.calls), 1)
        call = sender.calls[0]
        self.assertEqual(call["url"], speech.VOICE_EVALUATION_URL)
        self.assertEqual(set(call["payload"]), {"block_id", "course_id", "score_details"})
        self.assertEqual(call["headers"][speech.UCONTENT_AUTH_HEADER], "tok")

    async def test_string_zero_code_accepted(self) -> None:
        outcome = await speech.report_voice_evaluation(
            confirm=True, sender=FakeSender({"code": "0"}), **self._kwargs()
        )
        self.assertEqual(outcome.status, speech.STATUS_REPORTED)

    async def test_rejected(self) -> None:
        outcome = await speech.report_voice_evaluation(
            confirm=True, sender=FakeSender({"code": 1, "msg": "no permission"}), **self._kwargs()
        )
        self.assertEqual(outcome.status, speech.STATUS_REJECTED)
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.code, 1)
        self.assertIn("no permission", outcome.summary())

    async def test_non_mapping_response_soft_lands(self) -> None:
        outcome = await speech.report_voice_evaluation(
            confirm=True, sender=FakeSender(["unexpected"]), **self._kwargs()
        )
        self.assertEqual(outcome.status, speech.STATUS_UNKNOWN)
        self.assertIn("未验证", outcome.error)

    async def test_sender_failure_soft_lands(self) -> None:
        outcome = await speech.report_voice_evaluation(
            confirm=True, sender=FakeSender(error=RuntimeError("network down")), **self._kwargs()
        )
        self.assertEqual(outcome.status, speech.STATUS_FAILED)
        self.assertIn("network down", outcome.error)

    async def test_dry_run_flag_overrides_confirm(self) -> None:
        sender = FakeSender({"code": 0})
        outcome = await speech.report_voice_evaluation(
            confirm=True, dry_run=True, sender=sender, **self._kwargs()
        )
        self.assertEqual(outcome.status, speech.STATUS_DRY_RUN)
        self.assertEqual(sender.calls, [])

    async def test_sender_is_awaited(self) -> None:
        """sender 契约是 async：同步可调用对象会 TypeError → 软着陆成 failed。"""
        outcome = await speech.report_voice_evaluation(
            confirm=True, sender=lambda **_: {"code": 0}, **self._kwargs()
        )
        self.assertEqual(outcome.status, speech.STATUS_FAILED)


# --------------------------------------------------------------------------
# 事实登记与风格约束
# --------------------------------------------------------------------------


class TestSourceFactsAndStyle(unittest.TestCase):
    def test_speech_hosts_are_unipus_only(self) -> None:
        for url in (speech.SPEECH_WS_PRIMARY, speech.SPEECH_WS_BACKUP,
                    speech.SPEECH_CS_AUTH_URL, speech.SPEECH_HTTP_PROXY_URL,
                    speech.VOICE_EVALUATION_URL):
            self.assertTrue(url.split("://", 1)[1].startswith(
                ("speech.unipus.cn", "speech-eval.unipus.cn", "zt.unipus.cn",
                 "open-eval.unipus.cn", "ucontent.unipus.cn")), url)

    def test_registered_but_unimplemented_channels(self) -> None:
        self.assertEqual(set(speech.SIGNATURE_CHANNELS), {"cs_auth", "open_proxy"})
        for url in speech.SIGNATURE_CHANNELS.values():
            self.assertIn("unipus.cn", url)

    def test_module_is_stdlib_only(self) -> None:
        source = Path(speech.__file__).read_text(encoding="utf-8")
        for forbidden in ("import requests", "import httpx", "import aiohttp",
                          "import websockets", "from websockets", "import urllib.request"):
            self.assertNotIn(forbidden, source, f"语音评测模块必须纯标准库，不得出现 {forbidden}")

    def test_no_print_no_sleep(self) -> None:
        source = Path(speech.__file__).read_text(encoding="utf-8")
        self.assertNotIn("\nprint(", source, "用 logging，不要 print（仓库约定）")
        self.assertNotIn("time.sleep", source, "限速交给 Scheduler，模块内不 sleep")

    def test_uses_logging_not_print(self) -> None:
        with self.assertLogs(LOGGER_NAME, level="INFO") as captured:
            speech.run_speech_eval(timestamp=TS)
        joined = "\n".join(captured.output)
        self.assertIn("干跑", joined)
        self.assertIn("认证帧", joined)

    def test_evidence_levels_are_marked_in_docstrings(self) -> None:
        self.assertIn("【源码事实】", speech.__doc__ or "")
        self.assertIn("【推断】", speech.__doc__ or "")
        self.assertIn("【未验证】", speech.__doc__ or "")
        for func in (speech.build_auth_frame, speech.build_audio_frames,
                     speech.plan_speech_eval, speech.build_voice_evaluation_plan):
            self.assertIn("未验证", func.__doc__ or "", func.__name__)


if __name__ == "__main__":
    unittest.main()
