"""iTEST 听力题的音频转写（本地 faster-whisper）。

## 为什么需要它

iTEST 的短文听力是「听一段音频，回答 3 个小题」。文字模型听不到音频，所以
AI 只能把这类题**跳过**、交卷时因缺口被拒绝。把音频转成文字后，听力题就和
普通单选题一样能答了。

## 依赖与降级

``faster-whisper`` 是**可选依赖**（``pip install faster-whisper``，首次用会从
HuggingFace 下模型）。没装 / 下载失败时 :func:`available` 返回 False，
调用方照常跳过听力题，不影响其它题型 —— 不要让一个可选能力把整条链拖死。

模型与设备用环境变量调：

```
KEDAIBIAO_STT_MODEL=base.en     # tiny.en / base.en / small.en / medium.en
KEDAIBIAO_STT_DEVICE=cpu        # cpu / cuda
```

## 音频从哪来

``load`` 返回的试卷 HTML 里，听力 section 带 ``itest-hear-reslist``，里面有 CDN
上的 mp3 直链（实测 ``itestres.unipus.cn`` 公开可下，不需要登录态）。
"""

from __future__ import annotations

import hashlib
import logging
import os
import urllib.request
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: 默认模型：英文听力用 ``.en`` 版本，比多语版准且小
DEFAULT_MODEL = "base.en"
#: 默认设备/精度：CPU + int8，普通笔记本能跑
DEFAULT_DEVICE = "cpu"
DEFAULT_COMPUTE = "int8"

_MODEL_CACHE: dict[tuple[str, str, str], Any] = {}


def _patch_av_open() -> None:
    """兼容 PyAV 19+：faster-whisper 仍传 ``metadata_errors=``，新版 av 已经删掉这个参数。

    不改第三方包，只在进程内把 ``av.open`` 包一层把该参数丢掉 —— 其余参数原样透传。
    """
    try:
        import av
        from faster_whisper import audio as fw_audio
    except Exception:  # noqa: BLE001 - 没装就交给调用方降级
        return
    current = getattr(av, "open", None)
    if current is None or getattr(current, "_kdb_compat", False):
        return
    real_open = current

    def open_compat(*args: Any, **kwargs: Any) -> Any:
        kwargs.pop("metadata_errors", None)
        return real_open(*args, **kwargs)

    open_compat._kdb_compat = True  # type: ignore[attr-defined]
    fw_audio.av.open = open_compat


def model_name(environ: dict[str, str] | None = None) -> str:
    env = environ or os.environ
    return (env.get("KEDAIBIAO_STT_MODEL") or DEFAULT_MODEL).strip()


def available() -> bool:
    """faster-whisper 装没装（不触发模型下载）。"""
    try:
        import faster_whisper  # noqa: F401
    except Exception:  # noqa: BLE001 - 没装就是不可用
        return False
    return True


def _load_model(environ: dict[str, str] | None = None) -> Any:
    from faster_whisper import WhisperModel

    env = environ or os.environ
    name = model_name(env)
    device = (env.get("KEDAIBIAO_STT_DEVICE") or DEFAULT_DEVICE).strip()
    compute = (env.get("KEDAIBIAO_STT_COMPUTE") or DEFAULT_COMPUTE).strip()
    key = (name, device, compute)
    if key not in _MODEL_CACHE:
        log.info("加载语音模型 %s（%s/%s），首次会下载", name, device, compute)
        _MODEL_CACHE[key] = WhisperModel(name, device=device, compute_type=compute)
    return _MODEL_CACHE[key]


def transcribe_file(path: str | Path, *, environ: dict[str, str] | None = None) -> str:
    """转写一个音频文件，返回纯文本。失败抛异常，由调用方决定是否降级。"""
    _patch_av_open()
    model = _load_model(environ)
    segments, info = model.transcribe(str(path), beam_size=5, vad_filter=True)
    text = " ".join(seg.text.strip() for seg in segments if seg.text.strip())
    log.info("转写完成 %s：%.1fs 音频 → %d 字", Path(path).name,
             getattr(info, "duration", 0.0), len(text))
    return text.strip()


def download(url: str, target_dir: str | Path, *, timeout: float = 60.0) -> Path:
    """把音频下到本地（按 URL 哈希命名，重复调用不会重复下载）。"""
    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    path = target_dir / f"audio-{digest}.mp3"
    if path.is_file() and path.stat().st_size > 0:
        return path
    req = urllib.request.Request(  # noqa: S310 - 平台 CDN 的固定 https 地址
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
            "Referer": "https://itestcloud.unipus.cn/",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        data = resp.read()
    path.write_bytes(data)
    return path


def transcribe_paper(
    paper: Any, target_dir: str | Path, *, environ: dict[str, str] | None = None
) -> dict[int, str]:
    """给整份试卷里**所有带音频的 section** 做转写。

    返回 ``{section.index: 文本}``；单个 section 失败不影响其它（记日志后跳过）。
    """
    out: dict[int, str] = {}
    for section in getattr(paper, "sections", []) or []:
        if not section.audios:
            continue
        texts: list[str] = []
        for url in section.audios:
            try:
                path = download(url, target_dir)
                text = transcribe_file(path, environ=environ).strip()
                if text:
                    texts.append(text)
            except Exception as exc:  # noqa: BLE001 - 单段失败就跳过这段
                log.warning("听力音频转写失败（section %s）: %s", section.index, exc)
        joined = "\n".join(texts).strip()
        if joined:
            out[section.index] = joined
    return out
