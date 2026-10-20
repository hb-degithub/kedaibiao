"""U校园语音评测通道（签名帧 WSS + 评分上报，**默认干跑**）。

## 这个模块解决什么

U校园的口语题（``record`` / ``role-play`` / ``oral-state`` 等）在服务端属于
「语音评测」范畴：客户端把录音送到语音评测服务，拿回发音分，再把分数**自己
上报**给 ucontent（服务端不评测，只存分）。两条链路：

1. **语音评测 WSS**（本模块主体）：签名帧认证 → 上传音频 → 收评测结果；
2. **评分上报 HTTP**：``POST /course/api/v3/voiceEvaluation``（``block_id`` /
   ``course_id`` / ``score_details``），走 ``x-annotator-auth-token``。

## 证据边界（先读这段再用）

规格见 ``docs/research/unipus-protocol.md`` §3.5 / §4.3 / §4.4 / §10（U9、U11）。
本模块把每条知识分成三类，docstring 里逐条标注：

- **【源码事实】** 参考实现源码里读到的常量与公式（如 ``sig = SHA1(applicationId
  + secret + timestamp)``、两个通道地址、``alg: "sha1"``）；
- **【推断】** 由源码事实推出的、但源码没有明说的东西（如「认证帧必须是第一帧」
  「音频可以一个二进制帧发完」）；
- **【未验证】** 源码里也看不出来的东西（帧顺序、上传方式、分片规则、结果结构、
  服务端是否接受签名、``csAuth`` / ``open_proxy`` 两条 HTTP 通道的行为）。

收尾时队友的调研（``docs/research/uai-core-submit-facts.md`` §8）给了一句更细的
**帧序列**摘要：**认证帧 → 参数帧（``tokenId`` / ``audio`` 格式 /
``request.apiName`` / ``request.transcript`` / ``request.sig``）→ PCM 分片 →
``{"stop": true}``**。这段只到「有哪几段」的粒度，**每个键的完整形状仍然没有**，
所以本模块把参数帧与结束帧做成**调用方显式提供、默认不发**的可选步骤
（见 :func:`plan_speech_eval` 的 ``param_frame`` / ``send_stop``）。

另外两条 HTTP 通道（``https://zt.unipus.cn/soe/api/csAuth`` 取签名、
``https://open-eval.unipus.cn/open_proxy`` 语音评测 HTTP 通道）在规格里**只有
地址**，方法/入参/响应/鉴权方式全部未验证 —— 本模块**只登记地址**
（:data:`SIGNATURE_CHANNELS`），**不实现、不发送**：猜一套请求形状出来比不做更糟。

因为后两类占多数，本模块的设计原则是：

1. **默认干跑**：``confirm=False``（默认）只构造「将要发送的帧/请求」并返回
   一个 plan 供人查看/落盘，**不建任何连接**（有单测盯着）；
2. **未知项做成显式参数、默认关闭**：``upload_mode`` 默认 ``"none"``（只发认证
   帧）；帧顺序用 ``auth_first``；音频分片用 ``chunk_size``；结果结构**不解析死**，
   只做「像不像结果/像不像错误」的启发式分类，原始消息全部保留；
3. **错误软着陆**：连不上、握手失败、对端关闭、返回非 JSON、业务码失败 —— 都
   变成一个带 ``error`` 文本的 :class:`SpeechEvalOutcome` / :class:`VoiceReportOutcome`，
   不向调用方抛异常（只有**参数非法**才抛，因为那属于调用方的 bug）。

## 合规

参考实现 ``Yunlk/uai-core`` 是非商业许可（见 ``docs/license-policy.md`` §3.1），
**只能读功能事实、不能抄代码**。本模块是独立实现：纯标准库，没有一行来自参考
实现；算法、常量与端点来自上述 clean-room 规格。

## 依赖

纯标准库（``socket`` / ``ssl`` / ``hashlib`` / ``json`` …），不依赖
``net.http``、不依赖 ``adapter``。评分上报的 HTTP 发送由调用方注入 ``sender``，
本模块只负责构造请求与判定响应（这样它可以在完全离线的情况下被单测）。
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import logging
import secrets
import socket
import ssl
import struct
import time
import urllib.parse
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# 常量（【源码事实】，唯一出处；因为 ``endpoints.py`` 不是本模块的写范围，
# 这些常量只能落在这里，理由写在 docs/verification/unipus-speech.md）
# --------------------------------------------------------------------------

#: 语音评测 WebSocket 主通道（规格 §1、§4.4）
SPEECH_WS_PRIMARY = "wss://speech.unipus.cn/speech/proxy/wss"
#: 语音评测 WebSocket 备用通道（规格 §1）
SPEECH_WS_BACKUP = "wss://speech-eval.unipus.cn/speech/proxy/wss"

#: 取语音评测签名的 HTTP 通道（规格 §1；**行为未验证**，见文档 §4）
SPEECH_CS_AUTH_URL = "https://zt.unipus.cn/soe/api/csAuth"
#: 语音评测 HTTP 通道（规格 §1；**行为未验证**）
SPEECH_HTTP_PROXY_URL = "https://open-eval.unipus.cn/open_proxy"

#: 签名用的 applicationId（【源码事实】）
SPEECH_APPLICATION_ID = "138000000000001"
#: 签名用的 secret（【源码事实】；参考实现称它是前端包里的明文常量，U11 未独立核对）
SPEECH_SECRET = "example-speech-eval-secret-0000000001"
#: 认证帧里的算法名（【源码事实】）
SPEECH_ALG = "sha1"

#: 口语评分上报端点（规格 §4.3；ucontent 家族，成功码 ``code == 0``）
VOICE_EVALUATION_HOST = "https://ucontent.unipus.cn"
VOICE_EVALUATION_PATH = "/course/api/v3/voiceEvaluation"
VOICE_EVALUATION_URL = VOICE_EVALUATION_HOST + VOICE_EVALUATION_PATH

#: 队友调研（``docs/research/uai-core-submit-facts.md`` §8）对语音评测帧序列的
#: 一句话摘要：**认证帧 → 参数帧（含 ``tokenId``、``audio`` 格式、
#: ``request.apiName`` / ``request.transcript`` / ``request.sig``）→ PCM 分片 →
#: ``{"stop": true}``**。
#:
#: ⚠️ 这只是「有哪几段」的粒度，**每个键逐字长什么样并没有给出**（备注写的是
#: `src/api/speech_eval.rs#L1-L50` 的概述）。所以本模块只把「参数帧」与
#: 「结束帧」做成调用方显式提供的可选步骤（默认都不发），不替调用方猜键名。
SPEECH_PARAM_FRAME_HINTS: tuple[str, ...] = ("tokenId", "audio", "request")
#: 结束帧的调研结论：``{"stop": true}``（是否必需、是否还有别的字段**未验证**）
SPEECH_STOP_FRAME: dict[str, Any] = {"stop": True}

#: ucontent 受保护接口的鉴权头名（规格 §1、§2.3）
UCONTENT_AUTH_HEADER = "x-annotator-auth-token"
#: ucontent 家族的成功码（数字 0，字符串 "0" 容忍；规格 §5.1）
UCONTENT_SUCCESS_CODES: frozenset[object] = frozenset({0, "0"})

#: 只允许向这些主机发请求（域名纪律，规格 §1）
ALLOWED_HOST_SUFFIX = ".unipus.cn"
ALLOWED_HOSTS: tuple[str, ...] = (
    "speech.unipus.cn",
    "speech-eval.unipus.cn",
    "zt.unipus.cn",
    "open-eval.unipus.cn",
    "ucontent.unipus.cn",
)

#: 音频上传方式（**上传方式本身未验证**，所以默认关闭）
UPLOAD_NONE = "none"
UPLOAD_BINARY = "binary"
UPLOAD_BASE64_JSON = "base64-json"
UPLOAD_MODES: frozenset[str] = frozenset({UPLOAD_NONE, UPLOAD_BINARY, UPLOAD_BASE64_JSON})

#: 连接/读取超时（秒）
DEFAULT_TIMEOUT_SECONDS = 15.0
#: 一次评测最多读多少条消息（防止对端一直推）
MAX_RESULT_MESSAGES = 8
#: 二进制/文本帧的载荷上限（防御性，1 MiB 足够一段朗读音频的分片）
MAX_FRAME_BYTES = 1 << 20

#: WebSocket 握手固定 GUID（RFC 6455）
_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


# --------------------------------------------------------------------------
# 异常（只在**参数非法**时抛；网络/协议失败一律软着陆成 outcome）
# --------------------------------------------------------------------------


class SpeechEvalError(Exception):
    """语音评测模块的异常基类。"""


class SpeechEvalProtocolError(SpeechEvalError, ValueError):
    """参数或帧结构不合法（调用方的 bug，重试没有意义）。"""


class SpeechEvalConnectionError(SpeechEvalError, OSError):
    """连接、握手或帧读写失败。

    刻意继承 ``OSError``：网络层失败交给调度层重试是仓库既有约定
    （见 ``kedaibiao.net.http.TransportError``）。本模块内部把它软着陆成
    ``status="unreachable"``，只有直接调 :class:`WssConnection` 的调用方才会看到它。
    """


class SpeechEvalConnectionClosed(SpeechEvalConnectionError):
    """对端正常关闭了连接（收到 close 帧或读到 EOF）。"""


# --------------------------------------------------------------------------
# 签名与认证帧（纯函数：固定 timestamp → 固定 sig）
# --------------------------------------------------------------------------


def normalize_timestamp(
    timestamp: int | str | None = None, *, clock: Callable[[], float] | None = None
) -> str:
    """把时间戳规范成**秒级十进制字符串**。

    规格 §3.5：``timestamp`` 是秒级字符串。源码事实只到这一层，**不排除**
    平台其实接受毫秒；本模块只按秒级实现，要换就以 ``timestamp`` 显式传入。
    """
    if timestamp is None:
        source = clock or time.time
        timestamp = int(source())
    if isinstance(timestamp, bool):
        raise SpeechEvalProtocolError(f"timestamp 不能是布尔值: {timestamp!r}")
    if isinstance(timestamp, int):
        if timestamp < 0:
            raise SpeechEvalProtocolError(f"timestamp 不能为负: {timestamp}")
        return str(timestamp)
    if isinstance(timestamp, str):
        text = timestamp.strip()
        if not text or not text.isdigit():
            raise SpeechEvalProtocolError(f"timestamp 必须是纯数字字符串: {timestamp!r}")
        return text
    raise SpeechEvalProtocolError(f"timestamp 类型不支持: {type(timestamp).__name__}")


def sign_speech(
    *,
    timestamp: int | str,
    application_id: str = SPEECH_APPLICATION_ID,
    secret: str = SPEECH_SECRET,
    uppercase: bool = False,
) -> str:
    """``sig = SHA1(applicationId + secret + timestamp)``（规格 §3.5，【源码事实】）。

    摘要的**十六进制大小写**在规格里没有写明：默认小写，``uppercase=True``
    可切到大写（登录加密那里就因为大小写写错被当成密码错误，规格 §3.1 有先例，
    所以这里做成显式参数而不是猜一个）。
    """
    if not application_id or not secret:
        raise SpeechEvalProtocolError("applicationId 与 secret 都不能为空")
    stamp = normalize_timestamp(timestamp)
    raw = f"{application_id}{secret}{stamp}".encode()
    digest = hashlib.sha1(raw).hexdigest()  # noqa: S324 - 平台协议规定，不是安全用途
    return digest.upper() if uppercase else digest


def build_auth_frame(
    *,
    timestamp: int | str,
    application_id: str = SPEECH_APPLICATION_ID,
    secret: str = SPEECH_SECRET,
    alg: str = SPEECH_ALG,
    uppercase: bool = False,
    include_secret: bool = False,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """构造认证帧（**纯函数**，固定 timestamp → 固定内容）。

    【源码事实】认证帧里带 ``alg``（值为 ``"sha1"``），平台用
    ``SHA1(applicationId + secret + timestamp)`` 校验。

    【推断】字段名取 ``applicationId`` / ``timestamp`` / ``sig`` / ``alg`` ——
    规格只逐字给了 ``alg`` 的值与签名公式，没给认证帧的完整键名。所以：

    - ``include_secret``：**认证帧是否也要带 secret**（默认 ``False``，因为
      secret 已经参与了签名；真要带就显式打开）；
    - ``extra``：其它可能的字段（例如 nonce、version）原样并入，顺序在固定
      字段之后 —— 未知字段一律不猜。

    【未验证】认证帧的完整键名、``timestamp`` 是否真按秒、摘要大小写、
    以及服务端是否接受这帧 —— 规格里都只到「签名公式与 ``alg``」这一层
    （U9/U11）。所以这些全部是显式参数，不写成唯一实现。

    字段插入顺序固定（``applicationId, timestamp, sig, alg``），
    ``json.dumps`` 会保持该顺序，签名结果与帧文本因此可复现。
    """
    stamp = normalize_timestamp(timestamp)
    frame: dict[str, Any] = {
        "applicationId": application_id,
        "timestamp": stamp,
        "sig": sign_speech(
            timestamp=stamp, application_id=application_id, secret=secret, uppercase=uppercase
        ),
        "alg": alg,
    }
    if include_secret:
        frame["secret"] = secret
    for key, value in (extra or {}).items():
        if key in frame:
            raise SpeechEvalProtocolError(f"认证帧字段重复: {key!r}")
        frame[key] = value
    return frame


def auth_frame_json(frame: Mapping[str, Any]) -> str:
    """把认证帧序列化成**将要发送的文本帧**（紧凑 JSON，不转义非 ASCII）。"""
    return json.dumps(dict(frame), ensure_ascii=False, separators=(",", ":"))


# --------------------------------------------------------------------------
# 帧与计划
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OutgoingFrame:
    """一帧「将要发送」的东西（干跑时给人看，真连时逐帧发出）。

    ``note`` 记录这一帧的**依据等级**，干跑输出里会一并展示 —— 让人一眼看出
    哪一帧是源码事实、哪一帧是推断。
    """

    kind: str  # "text" | "binary"
    payload: str | bytes
    note: str = ""

    def __post_init__(self) -> None:
        if self.kind not in ("text", "binary"):
            raise SpeechEvalProtocolError(f"未知帧类型: {self.kind!r}")
        if self.kind == "text" and not isinstance(self.payload, str):
            raise SpeechEvalProtocolError("文本帧的载荷必须是 str")
        if self.kind == "binary" and not isinstance(self.payload, (bytes, bytearray)):
            raise SpeechEvalProtocolError("二进制帧的载荷必须是 bytes")

    @property
    def size(self) -> int:
        return len(self.payload)

    def preview(self, limit: int = 200) -> str:
        """可安全打日志的摘要（二进制帧只报长度与开头十六进制）。"""
        if self.kind == "text":
            text = str(self.payload)
            return text if len(text) <= limit else text[:limit] + f"…（共 {len(text)} 字符）"
        head = bytes(self.payload[:16]).hex()
        return f"<二进制 {self.size} 字节，开头 {head}…>"


def read_audio_bytes(audio: bytes | bytearray | str | Path) -> bytes:
    """读音频：``bytes`` 原样用，``str`` / ``Path`` 按文件读。"""
    if isinstance(audio, (bytes, bytearray)):
        return bytes(audio)
    if isinstance(audio, (str, Path)):
        path = Path(audio)
        if not path.is_file():
            raise SpeechEvalProtocolError(f"音频文件不存在: {path}")
        return path.read_bytes()
    raise SpeechEvalProtocolError(f"音频类型不支持: {type(audio).__name__}")


def build_audio_frames(
    audio: bytes | bytearray | str | Path,
    *,
    upload_mode: str,
    reference_text: str = "",
    audio_key: str = "audio",
    text_key: str = "text",
    chunk_size: int = 0,
) -> list[OutgoingFrame]:
    """按显式 ``upload_mode`` 构造音频帧。

    **上传方式在规格里未验证（U9）**，所以三种方式都只是「可能的形状」：

    - ``"binary"``：把音频原样放在**二进制帧**里（【推断】参考实现的
      ``speech_eval.rs`` 走的是 WebSocket，但一个帧还是多帧未验证）；
    - ``"base64-json"``：``{"<audio_key>": "<base64>", "<text_key>": "<参考文本>"}``
      的文本帧；键名可换（【推断】，键名完全未验证）；
    - ``"none"``：不构造音频帧（默认，等于只做认证）。
    """
    if upload_mode not in UPLOAD_MODES:
        raise SpeechEvalProtocolError(
            f"未知 upload_mode: {upload_mode!r}，可选 {sorted(UPLOAD_MODES)}"
        )
    if upload_mode == UPLOAD_NONE:
        return []
    data = read_audio_bytes(audio)
    if not data:
        raise SpeechEvalProtocolError("音频为空，拒绝构造上传帧")
    if chunk_size < 0:
        raise SpeechEvalProtocolError("chunk_size 不能为负")
    if chunk_size > MAX_FRAME_BYTES:
        raise SpeechEvalProtocolError(f"chunk_size 太大（上限 {MAX_FRAME_BYTES} 字节）")

    if upload_mode == UPLOAD_BINARY:
        if chunk_size:
            chunks = [data[i:i + chunk_size] for i in range(0, len(data), chunk_size)]
        else:
            chunks = [data]
        return [
            OutgoingFrame("binary", chunk, note="【推断】音频二进制帧；分片规则未验证")
            for chunk in chunks
        ]

    # base64-json
    encoded = base64.b64encode(data).decode("ascii")
    if not chunk_size:
        payload: dict[str, Any] = {audio_key: encoded}
        if reference_text:
            payload[text_key] = reference_text
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        return [OutgoingFrame("text", text, note="【推断】音频 base64 文本帧；键名与形状未验证")]
    frames: list[OutgoingFrame] = []
    step = chunk_size
    for index in range(0, len(data), step):
        piece = data[index:index + step]
        payload = {audio_key: base64.b64encode(piece).decode("ascii")}
        if index == 0 and reference_text:
            payload[text_key] = reference_text
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        frames.append(OutgoingFrame(
            "text", text, note="【推断】音频 base64 分片文本帧；分片规则未验证"
        ))
    return frames


def assert_speech_url(url: str, *, schemes: tuple[str, ...] = ("wss",)) -> str:
    """域名纪律：只允许 ``*.unipus.cn``，且 scheme 必须是给定集合。

    规格 §1 的历史事故是「把令牌发到了错误的域名」，参考实现为此在发请求前
    断言目标主机。这里同样在**建连/发请求前**断言，不只在测试里断言。
    """
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in schemes:
        raise SpeechEvalProtocolError(f"scheme 必须是 {schemes}，实际 {parts.scheme!r}: {url}")
    host = (parts.hostname or "").lower()
    if not host:
        raise SpeechEvalProtocolError(f"URL 里没有主机: {url}")
    if not (host.endswith(ALLOWED_HOST_SUFFIX) or host == ALLOWED_HOST_SUFFIX.lstrip(".")):
        raise SpeechEvalProtocolError(f"拒绝向非 unipus 主机发请求: {host}")
    if ALLOWED_HOSTS and host not in ALLOWED_HOSTS:
        raise SpeechEvalProtocolError(f"主机不在语音评测的已知主机清单内: {host}")
    return url


@dataclass(slots=True)
class SpeechEvalPlan:
    """一次语音评测「将要发送的东西」（构造完就能给人看，发送与否另说）。"""

    url: str
    auth_frame: dict[str, Any]
    frames: list[OutgoingFrame]
    upload_mode: str = UPLOAD_NONE
    timestamp: str = ""
    fallback_urls: tuple[str, ...] = ()
    warnings: list[str] = field(default_factory=list)

    def describe(self) -> str:
        """干跑展示用的一行摘要（认证帧里的 secret 已屏蔽）。"""
        shown = dict(self.auth_frame)
        if "secret" in shown:
            shown["secret"] = "***"
        audio_frames = [
            frame for frame in self.frames
            if frame.kind == "binary" or frame.note.startswith("【推断】音频")
        ]
        return (
            f"目标 {self.url}；认证帧 {json.dumps(shown, ensure_ascii=False)}；"
            f"共 {len(self.frames)} 帧（音频 {len(audio_frames)} 帧，上传方式 {self.upload_mode}）"
            + (f"；备用 {', '.join(self.fallback_urls)}" if self.fallback_urls else "")
        )

    def to_dict(self) -> dict[str, Any]:
        """审计/落盘用的完整结构。

        ⚠️ 含认证帧原文（可能含 secret）与音频帧的 base64/字节长度，
        **不要直接写进日志**；日志请用 :meth:`describe`。
        """
        return {
            "url": self.url,
            "fallback_urls": list(self.fallback_urls),
            "upload_mode": self.upload_mode,
            "timestamp": self.timestamp,
            "auth_frame": dict(self.auth_frame),
            "frames": [
                {
                    "kind": frame.kind,
                    "note": frame.note,
                    "size": frame.size,
                    "text": frame.payload if frame.kind == "text" else None,
                }
                for frame in self.frames
            ],
            "warnings": list(self.warnings),
        }


def plan_speech_eval(
    *,
    audio: bytes | bytearray | str | Path | None = None,
    reference_text: str = "",
    upload_mode: str = UPLOAD_NONE,
    timestamp: int | str | None = None,
    application_id: str = SPEECH_APPLICATION_ID,
    secret: str = SPEECH_SECRET,
    alg: str = SPEECH_ALG,
    uppercase: bool = False,
    include_secret: bool = False,
    extra_auth_fields: Mapping[str, Any] | None = None,
    auth_first: bool = True,
    param_frame: Mapping[str, Any] | None = None,
    send_stop: bool = False,
    stop_frame: Mapping[str, Any] | None = None,
    audio_key: str = "audio",
    text_key: str = "text",
    chunk_size: int = 0,
    url: str = SPEECH_WS_PRIMARY,
    backup: bool = True,
    clock: Callable[[], float] | None = None,
) -> SpeechEvalPlan:
    """构造一次语音评测的帧序列（**不建连接**）。

    ``upload_mode="none"``（默认）时只产出认证帧 —— 因为「怎么上传音频」在规格
    里未验证（U9），默认不猜。要让流程真正跑起来，调用方必须**显式**选一种
    上传方式，并自己承担「形状可能不对」的风险。

    ``auth_first``：认证帧是否必须在第一位（【推断】，默认 ``True``）。设成
    ``False`` 会得到「其余帧在前、认证帧在后」的顺序并附带警告。

    ``param_frame`` / ``send_stop``：调研摘要说帧序列里还有**参数帧**
    （``tokenId`` / ``audio`` / ``request.*``）与**结束帧** ``{"stop": true}``，
    但**没有给出这些键的完整形状**。所以默认两者都不发；要发就显式给：

    - ``param_frame``：原样 JSON 序列化成一帧文本帧，位置在认证帧之后、
      音频帧之前（按调研摘要的顺序，【推断】）；
    - ``send_stop=True``：在最后补一帧 ``{"stop": true}``（可用 ``stop_frame``
      覆盖成实测确认过的形状）。
    """
    if upload_mode not in UPLOAD_MODES:
        raise SpeechEvalProtocolError(
            f"未知 upload_mode: {upload_mode!r}，可选 {sorted(UPLOAD_MODES)}"
        )
    target = assert_speech_url(url)
    fallbacks: tuple[str, ...] = ()
    if backup:
        fallbacks = tuple(
            assert_speech_url(other) for other in (SPEECH_WS_BACKUP,) if other != target
        )

    stamp = normalize_timestamp(timestamp, clock=clock)
    auth = build_auth_frame(
        timestamp=stamp,
        application_id=application_id,
        secret=secret,
        alg=alg,
        uppercase=uppercase,
        include_secret=include_secret,
        extra=extra_auth_fields,
    )
    auth_frame = OutgoingFrame(
        "text", auth_frame_json(auth), note="【源码事实】签名认证帧；键名属推断"
    )

    param_out: OutgoingFrame | None = None
    if param_frame is not None:
        if not param_frame:
            raise SpeechEvalProtocolError("param_frame 不能是空对象（要么不给，要么给完整形状）")
        param_out = OutgoingFrame(
            "text",
            json.dumps(dict(param_frame), ensure_ascii=False, separators=(",", ":")),
            note="【调研摘要】参数帧（tokenId/audio/request.*）；键名细节未验证",
        )

    audio_frames: list[OutgoingFrame] = []
    if audio is not None and upload_mode != UPLOAD_NONE:
        audio_frames = build_audio_frames(
            audio,
            upload_mode=upload_mode,
            reference_text=reference_text,
            audio_key=audio_key,
            text_key=text_key,
            chunk_size=chunk_size,
        )

    stop_out: OutgoingFrame | None = None
    if send_stop:
        payload = dict(stop_frame) if stop_frame is not None else dict(SPEECH_STOP_FRAME)
        stop_out = OutgoingFrame(
            "text",
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            note='【调研摘要】结束帧 {"stop":true}；是否必需未验证',
        )

    middle: list[OutgoingFrame] = []
    if param_out is not None:
        middle.append(param_out)
    middle.extend(audio_frames)
    frames: list[OutgoingFrame] = []
    if auth_first:
        frames.append(auth_frame)
        frames.extend(middle)
    else:
        frames.extend(middle)
        frames.append(auth_frame)
    if stop_out is not None:
        frames.append(stop_out)

    warnings: list[str] = []
    if upload_mode == UPLOAD_NONE:
        warnings.append(
            "upload_mode='none'：只构造认证帧。音频上传方式在规格里未验证（U9），"
            "本模块不猜 —— 真实评测必须显式指定 upload_mode"
        )
    if audio is not None and upload_mode == UPLOAD_NONE:
        warnings.append("提供了音频但上传方式为 none，音频不会被发送")
    if audio is None and upload_mode != UPLOAD_NONE:
        warnings.append(
            f"upload_mode={upload_mode!r} 但没有提供音频：不会产出音频帧（只有认证帧）"
        )
    if audio_frames:
        warnings.append(
            f"音频上传方式 {upload_mode!r} 属【推断】：上传方式/分片规则/编码均未验证（U9），"
            "服务端是否接受未知 —— 这是调用方显式选定的，不是本模块的默认"
        )
    if not auth_first:
        warnings.append("认证帧不在首位：帧顺序未验证（U9），该顺序是调用方显式要求的")
    if param_out is not None:
        warnings.append(
            "含参数帧：形状由调用方提供（调研只给到 tokenId/audio/request.* 的粒度，"
            "键名与取值未验证）"
        )
    if stop_out is not None:
        warnings.append('含结束帧 {"stop":true}：调研摘要提到该帧，但缺什么字段/是否必需未验证')
    if include_secret:
        warnings.append("认证帧包含 secret 字段：是否该带未验证（U11），默认不带")
    if uppercase:
        warnings.append("签名用大写十六进制：规格未写明大小写，默认小写")
    if extra_auth_fields:
        warnings.append(f"认证帧含调用方附加字段: {sorted(extra_auth_fields)}（未验证）")

    log.debug("构造语音评测计划: %s", ", ".join(
        f"{frame.kind}:{frame.size}" for frame in frames
    ))
    return SpeechEvalPlan(
        url=target,
        auth_frame=auth,
        frames=frames,
        upload_mode=upload_mode,
        timestamp=stamp,
        fallback_urls=fallbacks,
        warnings=warnings,
    )


# --------------------------------------------------------------------------
# 结果启发式：**不解析死**，只判断「像不像错误 / 像不像结果」
# --------------------------------------------------------------------------

#: 可能承载错误的键（规格没有给结果结构，这几个是常见形态）
#: ⚠️ 刻意**不含** ``status``：评测结果里 ``status`` 更可能是状态字符串
#: （``"finished"`` 之类），把它当错误码会大面积误判。
_ERROR_CODE_KEYS = ("code", "errcode", "error_code", "err_no", "errno", "ret", "retcode")
#: ``status`` 取这些值时才视为错误
_ERROR_STATUS_VALUES: frozenset[str] = frozenset({"error", "failed", "fail", "failure"})
#: 视为「非错误」的成功码取值
_ERROR_OK_VALUES: frozenset[object] = frozenset(
    {0, 200, "0", "200", "ok", "success", "OK", "SUCCESS"}
)
#: 可能承载分数的键（大小写两种都认）
_SCORE_KEYS = (
    "score", "total_score", "overall", "overall_score", "pron", "pronunciation",
    "fluency", "integrity", "accuracy", "rhythm", "pron_score", "totalScore",
)


def _error_message(document: Mapping[str, Any]) -> str:
    for key in ("message", "msg", "error", "errMsg", "detail"):
        value = document.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, Mapping) and value:
            return json.dumps(value, ensure_ascii=False)[:200]
    return ""


def looks_like_error(document: Any) -> bool:
    """启发式：这帧消息像不像**错误**。

    规格没有给语音评测的结果/错误结构（U9），所以这里只认「显式的失败信号」：

    - ``success`` / ``ok`` 为 ``False``；
    - 错误码键存在且取值**不在** :data:`_ERROR_OK_VALUES`；
    - ``error`` 字段为真值（非空字符串/对象）。

    判定不了的返回 ``False``（宁可少判错误，让调用方看原始消息）。
    """
    if not isinstance(document, Mapping):
        return False
    for key in ("success", "ok"):
        if key in document and document[key] is False:
            return True
    for key in _ERROR_CODE_KEYS:
        if key in document:
            value = document[key]
            if isinstance(value, str):
                return value.strip() not in _ERROR_OK_VALUES
            if isinstance(value, bool):
                return value is False
            if isinstance(value, (int, float)):
                return value not in _ERROR_OK_VALUES
            return True
    status = document.get("status")
    if isinstance(status, str) and status.strip().lower() in _ERROR_STATUS_VALUES:
        return True
    error = document.get("error")
    if isinstance(error, str) and error.strip():
        return True
    return bool(isinstance(error, Mapping) and error)


def looks_like_result(document: Any) -> bool:
    """启发式：这帧消息像不像**评测结果**。

    顶层，或 ``data`` / ``result`` / ``detail`` / ``payload`` 里带分数键，即视为
    结果；结构未验证（U9），这只是「值得看一眼」的信号。
    """
    if not isinstance(document, Mapping):
        return False
    if _has_score_key(document):
        return True
    for key in ("data", "result", "detail", "payload"):
        nested = document.get(key)
        if isinstance(nested, Mapping) and _has_score_key(nested):
            return True
    return False


def _has_score_key(document: Mapping[str, Any]) -> bool:
    return any(key in document for key in _SCORE_KEYS)


def collect_score_fields(document: Any, *, max_depth: int = 2) -> dict[str, float]:
    """把评测结果里的**数字分数字段**收集成 ``{"路径": 数值}``。

    规格 §7.4 提醒：语音评测这条链的分数常是 **0~1**，而口语题 ``record`` 的
    分数量纲是 **0~100**，两者不是一回事。本函数**不做换算**（换算比例是产品
    决定），只把原始数值挑出来，交给调用方决定怎么填 ``score_details``。

    ⚠️ 取值规则完全来自「键名像分数」的启发式，结构未验证（U9）；拿到的东西
    仅供参考，不要当作稳定契约。
    """
    found: dict[str, float] = {}

    def walk(node: Any, path: str, depth: int) -> None:
        if depth > max_depth or not isinstance(node, Mapping):
            return
        for key, value in node.items():
            if (
                key in _SCORE_KEYS
                and isinstance(value, (int, float))
                and not isinstance(value, bool)
            ):
                found[f"{path}{key}" if path else str(key)] = float(value)
            elif key in ("data", "result", "detail", "payload"):
                walk(value, f"{path}{key}.", depth + 1)

    walk(document, "", 0)
    return found


# --------------------------------------------------------------------------
# 最小 WebSocket 客户端（RFC 6455，wss；只支持文本/二进制帧）
# --------------------------------------------------------------------------


def _default_socket_factory(host: str, port: int, timeout: float) -> socket.socket:
    return socket.create_connection((host, port), timeout=timeout)


def _default_tls_wrapper(sock: socket.socket, host: str, timeout: float) -> socket.socket:
    """给裸 socket 套 TLS（``server_hostname`` 必须给，否则证书校验会失败）。"""
    context = ssl.create_default_context()
    sock.settimeout(timeout)
    return context.wrap_socket(sock, server_hostname=host)


class WssConnection:
    """最小 RFC 6455 客户端。

    只做语音评测需要的事：**文本/二进制帧**、握手、ping→pong、close。
    不支持扩展协商、不支持压缩、不做代理。分片帧按 RFC 收齐再返回。

    ``socket_factory`` 与 ``tls_wrapper`` 可注入，是为了**离线单测**：测试里
    给一个假 socket 就能验证握手头、掩码、帧读取，不碰真实网络。
    """

    def __init__(
        self,
        url: str,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        extra_headers: Mapping[str, str] | None = None,
        socket_factory: Callable[[str, int, float], socket.socket] | None = None,
        tls_wrapper: Callable[[socket.socket, str, float], socket.socket] | None = None,
    ) -> None:
        self.url = assert_speech_url(url, schemes=("wss",))
        parts = urllib.parse.urlsplit(self.url)
        self.host = parts.hostname or ""
        self.port = parts.port or (443 if parts.scheme == "wss" else 80)
        self.scheme = parts.scheme
        self.path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        self.timeout = timeout
        self.extra_headers = dict(extra_headers or {})
        self._socket_factory = socket_factory or _default_socket_factory
        self._tls_wrapper = tls_wrapper
        self._sock: socket.socket | None = None
        self._buffer = b""
        self._closed = False

    # -- 生命周期 ---------------------------------------------------------

    def connect(self) -> WssConnection:
        """建 TCP（+ TLS）连接并完成 WebSocket 握手。"""
        if self._sock is not None:
            return self
        sock: socket.socket | None = None
        try:
            sock = self._socket_factory(self.host, self.port, self.timeout)
            if self.scheme == "wss":
                wrapper = self._tls_wrapper or _default_tls_wrapper
                sock = wrapper(sock, self.host, self.timeout)
            sock.settimeout(self.timeout)
            key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
            host_header = self.host if self.port in (80, 443) else f"{self.host}:{self.port}"
            lines = [
                f"GET {self.path} HTTP/1.1",
                f"Host: {host_header}",
                "Upgrade: websocket",
                "Connection: Upgrade",
                f"Sec-WebSocket-Key: {key}",
                "Sec-WebSocket-Version: 13",
            ]
            for name, value in self.extra_headers.items():
                lines.append(f"{name}: {value}")
            request = ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8")
            sock.sendall(request)
            response = self._read_headers(sock)
        except SpeechEvalError:
            if sock is not None:
                self._discard(sock)
            raise
        except (OSError, ssl.SSLError) as exc:
            if sock is not None:
                self._discard(sock)
            raise SpeechEvalConnectionError(f"连接 {self.url} 失败: {exc}") from exc

        status_line, headers = self._parse_handshake(response)
        if "101" not in status_line.split(" ")[:3]:
            self._discard(sock)
            raise SpeechEvalConnectionError(f"WebSocket 握手被拒: {status_line}")
        expected = base64.b64encode(
            hashlib.sha1((key + _WS_GUID).encode("ascii")).digest()  # noqa: S324
        ).decode("ascii")
        accept = headers.get("sec-websocket-accept", "")
        if accept and accept != expected:
            self._discard(sock)
            raise SpeechEvalConnectionError("WebSocket 握手校验失败：Sec-WebSocket-Accept 不匹配")
        self._sock = sock
        log.debug("WebSocket 已连接: %s", self.url)
        return self

    def close(self) -> None:
        if self._sock is None:
            return
        with contextlib.suppress(OSError, SpeechEvalError):
            self._send_frame(0x8, b"")
        sock, self._sock = self._sock, None
        self._closed = True
        self._discard(sock)

    def __enter__(self) -> WssConnection:
        return self.connect()

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- 发送 -------------------------------------------------------------

    def send_text(self, text: str) -> None:
        self._send_frame(0x1, text.encode("utf-8"))

    def send_binary(self, data: bytes | bytearray) -> None:
        self._send_frame(0x2, bytes(data))

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        sock = self._require_socket()
        if len(payload) > MAX_FRAME_BYTES:
            raise SpeechEvalProtocolError(f"帧太大（{len(payload)} > {MAX_FRAME_BYTES} 字节）")
        header = bytearray([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header += struct.pack("!H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack("!Q", length)
        mask = secrets.token_bytes(4)
        header += mask
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        try:
            sock.sendall(bytes(header) + masked)
        except OSError as exc:
            raise SpeechEvalConnectionError(f"发送 WebSocket 帧失败: {exc}") from exc

    # -- 接收 -------------------------------------------------------------

    def recv_json(self) -> Any:
        """读下一条数据帧并尽量解析成 JSON。

        - JSON 解析失败 → ``{"_raw_text": "..."}``（不丢消息，也不硬报错）；
        - 二进制帧 → ``{"_binary": True, "_size": N}``（结果结构未验证，
          这里不擅自解码）；
        - 收到 close 帧/EOF → :class:`SpeechEvalConnectionClosed`。
        """
        opcode, payload = self._recv_data_frame()
        if opcode == 0x2:
            return {"_binary": True, "_size": len(payload)}
        text = payload.decode("utf-8", errors="replace")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"_raw_text": text[:500]}

    def _recv_data_frame(self) -> tuple[int, bytes]:
        while True:
            fin, opcode, payload = self._recv_frame()
            if opcode == 0x8:
                self._closed = True
                raise SpeechEvalConnectionClosed("对端发送了 close 帧")
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:  # pong
                continue
            if opcode in (0x1, 0x2):
                if not fin:
                    payload = self._recv_fragments(payload)
                return opcode, payload
            # 其它（continuation 孤立出现等）忽略

    def _recv_fragments(self, first: bytes) -> bytes:
        data = bytearray(first)
        while True:
            fin, opcode, payload = self._recv_frame()
            if opcode == 0x8:
                self._closed = True
                raise SpeechEvalConnectionClosed("分片过程中对端关闭")
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode not in (0x0, 0x1, 0x2):
                continue
            data.extend(payload)
            if len(data) > MAX_FRAME_BYTES:
                raise SpeechEvalProtocolError("分片消息超过上限")
            if fin:
                return bytes(data)

    def _recv_frame(self) -> tuple[bool, int, bytes]:
        try:
            header = self._recv_exact(2)
        except SpeechEvalConnectionClosed:
            raise
        except OSError as exc:
            raise SpeechEvalConnectionError(f"读取 WebSocket 帧失败: {exc}") from exc
        fin = bool(header[0] & 0x80)
        opcode = header[0] & 0x0F
        masked = bool(header[1] & 0x80)
        length = header[1] & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._recv_exact(8))[0]
        if length > MAX_FRAME_BYTES:
            raise SpeechEvalProtocolError(f"对端帧太大（{length} 字节）")
        mask = self._recv_exact(4) if masked else b""
        payload = self._recv_exact(length) if length else b""
        if masked:
            payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        return fin, opcode, payload

    def _recv_exact(self, size: int) -> bytes:
        sock = self._require_socket()
        while len(self._buffer) < size:
            try:
                chunk = sock.recv(max(4096, size - len(self._buffer)))
            except (OSError, ssl.SSLError) as exc:
                raise SpeechEvalConnectionError(f"读取 WebSocket 数据失败: {exc}") from exc
            if not chunk:
                raise SpeechEvalConnectionClosed("连接在读取过程中被关闭")
            self._buffer += chunk
        data, self._buffer = self._buffer[:size], self._buffer[size:]
        return data

    # -- 握手辅助 ---------------------------------------------------------

    def _read_headers(self, sock: socket.socket) -> bytes:
        buffer = b""
        while b"\r\n\r\n" not in buffer:
            try:
                chunk = sock.recv(4096)
            except (OSError, ssl.SSLError) as exc:
                raise SpeechEvalConnectionError(f"握手阶段读取失败: {exc}") from exc
            if not chunk:
                raise SpeechEvalConnectionError("握手阶段连接被关闭")
            buffer += chunk
            if len(buffer) > 64 * 1024:
                raise SpeechEvalConnectionError("握手响应头过大")
        head, _, rest = buffer.partition(b"\r\n\r\n")
        self._buffer = rest
        return head

    @staticmethod
    def _parse_handshake(head: bytes) -> tuple[str, dict[str, str]]:
        lines = head.decode("latin-1", errors="replace").split("\r\n")
        status_line = lines[0] if lines else ""
        headers: dict[str, str] = {}
        for line in lines[1:]:
            name, _, value = line.partition(":")
            if name.strip():
                headers[name.strip().lower()] = value.strip()
        return status_line, headers

    # -- 杂项 -------------------------------------------------------------

    def _require_socket(self) -> socket.socket:
        if self._sock is None:
            raise SpeechEvalConnectionError("WebSocket 尚未连接；请先 connect()")
        return self._sock

    @staticmethod
    def _discard(sock: socket.socket) -> None:
        with contextlib.suppress(OSError):
            sock.close()


def _default_connector(url: str, timeout: float) -> WssConnection:
    """默认连接器：真实建 wss 连接（只有 ``confirm=True`` 时才会走到这里）。"""
    return WssConnection(url, timeout=timeout).connect()


# --------------------------------------------------------------------------
# 评测主流程
# --------------------------------------------------------------------------

#: 结果状态：只构造不看网络
STATUS_DRY_RUN = "dry_run"
#: 闸门挡住（例如没有上传方式、没有 sender）
STATUS_BLOCKED = "blocked"
#: 连不上 / 握手失败 / 对端提前关闭
STATUS_UNREACHABLE = "unreachable"
#: 服务端明确报错
STATUS_REJECTED = "rejected"
#: 收到像评测结果的消息
STATUS_SCORED = "scored"
#: 收到了消息，但既不像错误也不像结果（结构未验证，保留原始消息）
STATUS_UNKNOWN = "unknown"
#: 评分上报成功（HTTP 层 code == 0）
STATUS_REPORTED = "reported"
#: 发送过程中出现未预期异常（软着陆）
STATUS_FAILED = "failed"


@dataclass(slots=True)
class SpeechEvalOutcome:
    """一次语音评测的结果。**任何网络/协议问题都体现在这里，不抛异常。**"""

    status: str
    plan: SpeechEvalPlan | None = None
    url: str = ""
    messages: list[Any] = field(default_factory=list)
    score_fields: dict[str, float] = field(default_factory=dict)
    error: str = ""

    @property
    def ok(self) -> bool:
        """是否拿到了**可用的评测结果**。

        只有 ``scored`` 才算 —— ``unknown``（拿到了消息但结构判定不了）虽然不算
        失败，但也不能当「有分数」用，见 :attr:`needs_quarantine`。
        """
        return self.status == STATUS_SCORED

    @property
    def needs_quarantine(self) -> bool:
        """是否属于「结果不可信、需要人工看一眼」的状态。"""
        return self.status in (STATUS_UNKNOWN, STATUS_REJECTED, STATUS_UNREACHABLE, STATUS_FAILED)

    def summary(self) -> str:
        if self.status == STATUS_DRY_RUN:
            return f"干跑（未建连接）：{self.plan.describe() if self.plan else '-'}"
        if self.status == STATUS_BLOCKED:
            return f"被闸门挡住：{self.error}"
        if self.status == STATUS_SCORED:
            return f"收到评测结果：{self.score_fields or '（未识别出分数字段）'}"
        if self.status == STATUS_UNKNOWN:
            return f"收到 {len(self.messages)} 条消息但无法判定结构（U9 未验证）"
        return f"{self.status}: {self.error}"


def _classify_messages(messages: list[Any]) -> tuple[str, dict[str, float], str]:
    """按启发式给消息分类，返回 ``(status, score_fields, error)``。"""
    for message in messages:
        if looks_like_error(message):
            return STATUS_REJECTED, {}, _error_message(message) or "服务端返回了错误消息"
    for message in messages:
        if looks_like_result(message):
            return STATUS_SCORED, collect_score_fields(message), ""
    return STATUS_UNKNOWN, {}, ""


def run_speech_eval(
    *,
    audio: bytes | bytearray | str | Path | None = None,
    reference_text: str = "",
    upload_mode: str = UPLOAD_NONE,
    timestamp: int | str | None = None,
    dry_run: bool | None = None,
    confirm: bool = False,
    probe_only: bool = False,
    connector: Callable[[str, float], Any] | None = None,
    fallback: bool = True,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    max_messages: int = MAX_RESULT_MESSAGES,
    auth_first: bool = True,
    param_frame: Mapping[str, Any] | None = None,
    send_stop: bool = False,
    stop_frame: Mapping[str, Any] | None = None,
    url: str = SPEECH_WS_PRIMARY,
    logger: logging.Logger | None = None,
    clock: Callable[[], float] | None = None,
) -> SpeechEvalOutcome:
    """跑一次语音评测。

    ## 默认干跑

    ``confirm=False``（默认）**只构造计划、不建连接**：返回
    ``status="dry_run"`` 与一个 :class:`SpeechEvalPlan`，日志里有一条
    「将要发送 N 帧」的摘要。``dry_run=True`` 是「强制干跑」的显式开关，
    即使同时传了 ``confirm=True`` 也只构造（宁可少发，不可误发）。

    ## 真连的两道闸门

    ``confirm=True`` 之后还要满足其一，否则返回 ``status="blocked"``：

    - 显式给了 ``upload_mode``（``"binary"`` / ``"base64-json"``）—— 真的发音频；
    - 或者 ``probe_only=True`` —— 只发认证帧看服务端怎么回（连通性探测）。

    原因：音频上传方式与服务端行为都未验证（U9），默认不发任何真东西。

    ## 软着陆

    连不上、握手非 101、对端关闭、返回非 JSON、服务端报错 —— 全部落成
    :class:`SpeechEvalOutcome` 的 ``status``/``error``，**不抛异常**。只有参数
    非法（未知 ``upload_mode``、音频文件不存在、非 unipus 域名）才抛
    :class:`SpeechEvalProtocolError`。

    ``connector`` 是注入点（测试用假连接）；不给就真连
    （:func:`_default_connector`）。
    """
    log_ = logger or log
    plan = plan_speech_eval(
        audio=audio,
        reference_text=reference_text,
        upload_mode=upload_mode,
        timestamp=timestamp,
        auth_first=auth_first,
        param_frame=param_frame,
        send_stop=send_stop,
        stop_frame=stop_frame,
        url=url,
        backup=fallback,
        clock=clock,
    )

    effective_confirm = bool(confirm) and dry_run is not True
    if not effective_confirm:
        log_.info("语音评测干跑：%s", plan.describe())
        for warning in plan.warnings:
            log_.info("语音评测干跑提示：%s", warning)
        return SpeechEvalOutcome(status=STATUS_DRY_RUN, plan=plan, url=plan.url)

    if upload_mode == UPLOAD_NONE and not probe_only:
        reason = (
            "真实评测需要显式 upload_mode（音频上传方式未验证，U9）；"
            "只想探测认证帧是否被接受请传 probe_only=True"
        )
        log_.warning("语音评测被闸门挡住：%s", reason)
        return SpeechEvalOutcome(status=STATUS_BLOCKED, plan=plan, url=plan.url, error=reason)
    if audio is None and not probe_only:
        reason = (
            f"upload_mode={upload_mode!r} 但没给音频：这等于只发认证帧，"
            "不是一次真实的语音评测。确实只想探测就传 probe_only=True"
        )
        log_.warning("语音评测被闸门挡住：%s", reason)
        return SpeechEvalOutcome(status=STATUS_BLOCKED, plan=plan, url=plan.url, error=reason)

    targets = (plan.url,) + (plan.fallback_urls if fallback else ())
    errors: list[str] = []
    for target in targets:
        try:
            connection = (connector or _default_connector)(target, timeout)
        except Exception as exc:  # noqa: BLE001 - 连接失败要软着陆
            errors.append(f"{target}: {type(exc).__name__}: {exc}")
            log_.warning("语音评测连接 %s 失败：%s", target, exc)
            continue
        try:
            return _exchange(
                connection,
                plan=plan,
                target=target,
                max_messages=max_messages,
                logger=log_,
            )
        except Exception as exc:  # noqa: BLE001 - 交换失败要软着陆
            errors.append(f"{target}: {type(exc).__name__}: {exc}")
            log_.warning(
                "语音评测在 %s 上失败：%s", target, exc,
                exc_info=log_.isEnabledFor(logging.DEBUG),
            )
        finally:
            try:
                connection.close()
            except Exception:  # noqa: BLE001 - 关连接失败不影响结果
                log_.debug("关闭语音评测连接失败", exc_info=True)

    return SpeechEvalOutcome(
        status=STATUS_UNREACHABLE,
        plan=plan,
        url=plan.url,
        error="；".join(errors) or "没有可用的语音评测通道",
    )


def _exchange(
    connection: Any,
    *,
    plan: SpeechEvalPlan,
    target: str,
    max_messages: int,
    logger: logging.Logger,
) -> SpeechEvalOutcome:
    """在一条已连接的通道上发帧、收消息、分类。"""
    messages: list[Any] = []
    for frame in plan.frames:
        if frame.kind == "text":
            connection.send_text(frame.payload)
        else:
            connection.send_binary(frame.payload)
    logger.debug("语音评测已发送 %d 帧到 %s", len(plan.frames), target)

    for index in range(max_messages):
        try:
            message = connection.recv_json()
        except SpeechEvalConnectionClosed as exc:
            if messages:
                break
            return SpeechEvalOutcome(
                status=STATUS_UNREACHABLE,
                plan=plan,
                url=target,
                error=f"对端在返回任何消息前关闭：{exc}",
            )
        except SpeechEvalProtocolError:
            raise
        except Exception as exc:  # noqa: BLE001 - 读超时等软着陆
            if messages:
                logger.debug("语音评测读取第 %d 条消息失败，按已有消息收尾：%s", index + 1, exc)
                break
            return SpeechEvalOutcome(
                status=STATUS_UNREACHABLE, plan=plan, url=target,
                error=f"读取评测结果失败：{type(exc).__name__}: {exc}",
            )
        messages.append(message)
        # 第一条消息就明确报错 / 明确是结果时，不必等对端继续推
        if looks_like_error(message) or looks_like_result(message):
            break

    status, score_fields, error = _classify_messages(messages)
    if status == STATUS_UNKNOWN:
        logger.info(
            "语音评测收到 %d 条消息但结构与规格不符（U9 未验证），保留原始消息待分析",
            len(messages),
        )
    elif status == STATUS_SCORED:
        logger.info("语音评测成功：分数字段 %s", score_fields)
    else:
        logger.warning("语音评测被拒：%s", error)
    return SpeechEvalOutcome(
        status=status, plan=plan, url=target, messages=messages,
        score_fields=score_fields, error=error,
    )


# --------------------------------------------------------------------------
# 评分上报：POST /course/api/v3/voiceEvaluation
# --------------------------------------------------------------------------


@dataclass(slots=True)
class VoiceReportPlan:
    """一次评分上报「将要发送的东西」（干跑时给人看）。"""

    url: str
    headers: dict[str, str]
    payload: dict[str, Any]
    warnings: list[str] = field(default_factory=list)

    def describe(self) -> str:
        """摘要：**请求头的值一律屏蔽**（可能是 annotator token）。"""
        header_names = ", ".join(sorted(self.headers)) or "（无）"
        body = json.dumps(self.payload, ensure_ascii=False)
        if len(body) > 300:
            body = body[:300] + "…"
        return f"POST {self.url}；请求头 {header_names}（值已屏蔽）；体 {body}"

    def to_dict(self) -> dict[str, Any]:
        """审计用完整结构。⚠️ 含请求头取值（annotator token），**不要写日志**。"""
        return {
            "url": self.url,
            "headers": dict(self.headers),
            "payload": self.payload,
            "warnings": list(self.warnings),
        }


@dataclass(slots=True)
class VoiceReportOutcome:
    """一次评分上报的结果（同样软着陆）。"""

    status: str
    plan: VoiceReportPlan | None = None
    document: Any = None
    code: Any = None
    message: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.status == STATUS_REPORTED

    def summary(self) -> str:
        if self.status == STATUS_DRY_RUN:
            return f"干跑（未发送）：{self.plan.describe() if self.plan else '-'}"
        if self.status == STATUS_REPORTED:
            return "评分已上报（code=0）"
        if self.status == STATUS_REJECTED:
            return f"上报被服务端拒绝：code={self.code!r} {self.message}".rstrip()
        if self.status == STATUS_BLOCKED:
            return f"被闸门挡住：{self.error}"
        return f"{self.status}: {self.error or self.message}"


def _validate_identifier(value: Any, name: str) -> Any:
    """校验 ``block_id`` / ``course_id``。

    - 拒绝 ``bool`` 与 ``float``：19 位数字 ID 一旦过浮点就损坏（规格 §6.2 的
      精度硬约束），所以「浮点」在这里是**硬错误**而不是警告；
    - ``str`` / ``int`` 原样保留（不擅自转类型，服务端对类型敏感）。
    """
    if isinstance(value, bool):
        raise SpeechEvalProtocolError(f"{name} 不能是布尔值")
    if isinstance(value, float):
        raise SpeechEvalProtocolError(
            f"{name} 不能是浮点数：19 位 ID 过 float 会损坏精度（规格 §6.2）"
        )
    if isinstance(value, int):
        if value < 0:
            raise SpeechEvalProtocolError(f"{name} 不能为负: {value}")
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise SpeechEvalProtocolError(f"{name} 不能为空")
        return value
    raise SpeechEvalProtocolError(f"{name} 类型不支持: {type(value).__name__}")


def build_voice_evaluation_plan(
    *,
    block_id: str | int,
    course_id: str | int,
    score_details: Any,
    annotator_token: str = "",
    headers: Mapping[str, str] | None = None,
    url: str = VOICE_EVALUATION_URL,
) -> VoiceReportPlan:
    """构造评分上报请求（**纯函数**，固定输入 → 固定请求）。

    【源码事实】端点、字段名 ``block_id`` / ``course_id`` / ``score_details``、
    以及「服务端不评测、只存分」（规格 §4.3）。

    【未验证】

    - ``score_details`` 的**结构**（列表？字典？字符串化 JSON？）规格里没有；
      本函数原样透传，只要求 JSON 可序列化，不做任何改写；
    - 响应体结构（规格 §4.3 写「未解析」），所以只按 ucontent 家族的
      ``code == 0`` 判定接受；
    - ``block_id`` / ``course_id`` 的**取值范围与含义**（哪个 block？哪个
      course 实例？）未验证 —— 调用方传什么就发什么。
    """
    target = assert_speech_url(url, schemes=("https",))
    payload = {
        "block_id": _validate_identifier(block_id, "block_id"),
        "course_id": _validate_identifier(course_id, "course_id"),
        "score_details": score_details,
    }
    try:
        json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise SpeechEvalProtocolError(f"score_details 不可 JSON 序列化: {exc}") from exc

    merged: dict[str, str] = {"Content-Type": "application/json"}
    for name, value in (headers or {}).items():
        merged[str(name)] = str(value)
    if annotator_token:
        merged[UCONTENT_AUTH_HEADER] = annotator_token

    warnings: list[str] = [
        "score_details 的结构未在规格中给出（服务端只存不评），本模块原样透传",
        "响应体结构未验证：只按 ucontent 的 code == 0 判定接受（规格 §4.3/§5.1）",
    ]
    if UCONTENT_AUTH_HEADER not in {name.lower(): name for name in merged}:
        warnings.append(
            "缺少 x-annotator-auth-token：ucontent 受保护接口不认 Authorization，"
            "没有这个头真实发送一定失败（规格 §2.3）"
        )
    if not isinstance(score_details, (list, dict)):
        warnings.append(
            f"score_details 是 {type(score_details).__name__}（不是 list/dict）："
            "真实形状未验证，这里只是原样转发"
        )
    return VoiceReportPlan(url=target, headers=merged, payload=payload, warnings=warnings)


def _extract_message(document: Mapping[str, Any]) -> str:
    for key in ("message", "msg", "error", "errMsg"):
        value = document.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


async def report_voice_evaluation(
    *,
    block_id: str | int,
    course_id: str | int,
    score_details: Any,
    annotator_token: str = "",
    headers: Mapping[str, str] | None = None,
    sender: Callable[..., Any] | None = None,
    dry_run: bool | None = None,
    confirm: bool = False,
    url: str = VOICE_EVALUATION_URL,
    logger: logging.Logger | None = None,
) -> VoiceReportOutcome:
    """上报一次口语评分。

    ## 默认干跑

    ``confirm=False``（默认）只构造请求并返回计划，**不调用 ``sender``**。
    ``dry_run=True`` 强制干跑（即使 ``confirm=True``）。

    ## sender 契约

    ``sender`` 是一个 **async** 可调用对象，按
    ``await sender(url=..., headers=..., payload=...)`` 调用，返回**已解析的
    JSON 文档**。适配器接线时传 ``HttpClient.post_json`` 的包装即可；不传就
    视为「还没接线」，``confirm=True`` 时返回 ``status="blocked"``。

    为什么不在本模块里发：语音评测模块要对 adapter 保持零依赖（Lead 负责
    接线），而且这样它能在完全离线的情况下被单测。
    """
    log_ = logger or log
    plan = build_voice_evaluation_plan(
        block_id=block_id,
        course_id=course_id,
        score_details=score_details,
        annotator_token=annotator_token,
        headers=headers,
        url=url,
    )

    effective_confirm = bool(confirm) and dry_run is not True
    if not effective_confirm:
        log_.info("评分上报干跑：%s", plan.describe())
        for warning in plan.warnings:
            log_.info("评分上报干跑提示：%s", warning)
        return VoiceReportOutcome(status=STATUS_DRY_RUN, plan=plan)

    if sender is None:
        reason = (
            "没有注入 sender：真实上报需要 confirm=True 且提供 sender"
            "（例如 HttpClient.post_json 的包装）；本模块不直接发 HTTP"
        )
        log_.warning("评分上报被闸门挡住：%s", reason)
        return VoiceReportOutcome(status=STATUS_BLOCKED, plan=plan, error=reason)

    try:
        document = await sender(
            url=plan.url, headers=dict(plan.headers), payload=dict(plan.payload)
        )
    except Exception as exc:  # noqa: BLE001 - 网络层失败软着陆
        log_.warning("评分上报发送失败：%s", exc)
        return VoiceReportOutcome(
            status=STATUS_FAILED, plan=plan, error=f"{type(exc).__name__}: {exc}"
        )

    if not isinstance(document, Mapping):
        return VoiceReportOutcome(
            status=STATUS_UNKNOWN, plan=plan, document=document,
            error="响应不是 JSON 对象，结构未验证（规格 §4.3 写「未解析」）",
        )
    code = document.get("code")
    if code in UCONTENT_SUCCESS_CODES:
        log_.info("评分上报成功: block_id=%s", plan.payload["block_id"])
        return VoiceReportOutcome(status=STATUS_REPORTED, plan=plan, document=document, code=code)
    message = _extract_message(document)
    log_.warning("评分上报被拒: code=%r message=%s", code, message)
    return VoiceReportOutcome(
        status=STATUS_REJECTED, plan=plan, document=document, code=code, message=message
    )


# --------------------------------------------------------------------------
# 未接入的 HTTP 通道（只登记事实，不实现 —— 猜死比不做更糟）
# --------------------------------------------------------------------------

#: 登记但**不实现**的语音评测 HTTP 通道（行为均未验证，见验证文档 §4.3）：
SIGNATURE_CHANNELS: dict[str, str] = {
    "cs_auth": SPEECH_CS_AUTH_URL,        # 取签名（免认证？未验证）
    "open_proxy": SPEECH_HTTP_PROXY_URL,  # 语音评测 HTTP 通道（鉴权方式未验证）
}


__all__ = [
    "ALLOWED_HOSTS",
    "DEFAULT_TIMEOUT_SECONDS",
    "MAX_FRAME_BYTES",
    "MAX_RESULT_MESSAGES",
    "OutgoingFrame",
    "SIGNATURE_CHANNELS",
    "SPEECH_ALG",
    "SPEECH_APPLICATION_ID",
    "SPEECH_CS_AUTH_URL",
    "SPEECH_HTTP_PROXY_URL",
    "SPEECH_PARAM_FRAME_HINTS",
    "SPEECH_SECRET",
    "SPEECH_STOP_FRAME",
    "SPEECH_WS_BACKUP",
    "SPEECH_WS_PRIMARY",
    "STATUS_BLOCKED",
    "STATUS_DRY_RUN",
    "STATUS_FAILED",
    "STATUS_REJECTED",
    "STATUS_REPORTED",
    "STATUS_SCORED",
    "STATUS_UNKNOWN",
    "STATUS_UNREACHABLE",
    "SpeechEvalConnectionClosed",
    "SpeechEvalConnectionError",
    "SpeechEvalError",
    "SpeechEvalOutcome",
    "SpeechEvalPlan",
    "SpeechEvalProtocolError",
    "UCONTENT_AUTH_HEADER",
    "UCONTENT_SUCCESS_CODES",
    "UPLOAD_BASE64_JSON",
    "UPLOAD_BINARY",
    "UPLOAD_MODES",
    "UPLOAD_NONE",
    "VOICE_EVALUATION_PATH",
    "VOICE_EVALUATION_URL",
    "VoiceReportOutcome",
    "VoiceReportPlan",
    "WssConnection",
    "assert_speech_url",
    "auth_frame_json",
    "build_audio_frames",
    "build_auth_frame",
    "build_voice_evaluation_plan",
    "collect_score_fields",
    "looks_like_error",
    "looks_like_result",
    "normalize_timestamp",
    "plan_speech_eval",
    "read_audio_bytes",
    "report_voice_evaluation",
    "run_speech_eval",
    "sign_speech",
]
