"""登录验证码的自动识别（图片码），走已配置的多模态模型。

## 为什么单独一个模块

登录验证码是**一个字符一个字符**的短文本（4~6 位数字/字母），和答题链路
（要理解题意、选项、公式）是两种任务：

- 提示词不同：只要「原样读出图片里的字符」，不要解释；
- 失败语义不同：识别不出**必须能降级到人工输入**（控制台弹图让用户输），
  而不是猜一个错码把账号打进更严的风控。

所以这里不依赖答题器，只用 ``LLMConfig`` 里的网关配置发一次多模态请求。

## 边界

- 没配大模型 / 配的模型不支持图片 → 返回 ``None``（调用方转人工）；
- 只接受「纯字符」结果：带空格、标点、解释文字的一律**丢弃**，不猜；
- 不做 OCR 引擎内置（不引第三方二进制依赖），也不训练/破解任何验证码模型。
"""

from __future__ import annotations

import base64
import logging
import re
from typing import Any

from kedaibiao.net.http import HttpClient, UrllibTransport
from kedaibiao.tasks.llm import LLMConfig

log = logging.getLogger(__name__)

#: 只认 3~6 位字母数字 —— 登录验证码就这个形态，超出范围的多半是模型在瞎说
_CODE_RE = re.compile(r"^[A-Za-z0-9]{3,6}$")

_SYSTEM_PROMPT = (
    "你是验证码识别器。只输出图片里出现的字符本身，"
    "不要解释、不要标点、不要空格、不要引号。"
)

_USER_PROMPT = "读出这张图片里的验证码字符。"

#: 单张验证码图的大小上限（正常几 KB，超过说明截错了区域）
MAX_BYTES = 300_000


def normalize_code(text: str) -> str:
    """把模型输出收敛成候选验证码；不合形态返回空串。"""
    cleaned = "".join(str(text or "").split())
    cleaned = cleaned.strip("`'\"。.，,：:")
    return cleaned if _CODE_RE.match(cleaned) else ""


def is_available(environ: dict[str, str] | None = None) -> bool:
    """大模型配好没有（没配就别浪费时间走自动识别）。"""
    env = environ if environ is not None else {}
    return LLMConfig.from_env(env).enabled


async def solve_image_captcha(
    image: bytes,
    *,
    environ: dict[str, str] | None = None,
    client: HttpClient | None = None,
) -> str:
    """识别一张验证码图；不可用/失败/结果不合形态都返回空串。

    永不抛异常 —— 登录流程里它只是「先自动试一次」，失败就该转人工。
    """
    if not image or len(image) > MAX_BYTES:
        return ""
    config = LLMConfig.from_env(environ or {})
    if not config.enabled:
        log.info("大模型未配置，跳过验证码自动识别")
        return ""

    mime = "image/png"
    if image[:3] == b"\xff\xd8\xff":
        mime = "image/jpeg"
    data_url = f"data:{mime};base64,{base64.b64encode(image).decode('ascii')}"
    payload = {
        "model": config.vision_model_name,
        "temperature": 0,
        "max_tokens": 16,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": _USER_PROMPT},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            },
        ],
    }
    headers = {
        "Content-Type": "application/json",
        "User-Agent": config.user_agent,
        "Accept": "application/json, text/plain, */*",
    }
    if config.api_key:
        headers["Authorization"] = f"Bearer {config.api_key}"

    http = client or HttpClient(UrllibTransport(), timeout=config.timeout)
    try:
        response: Any = await http.post_json(
            f"{config.base_url}/chat/completions", payload, headers=headers
        )
        if not response.ok:
            log.warning("验证码识别请求失败：HTTP %s", response.status)
            return ""
        data = response.json()
        choices = data.get("choices") or []
        if not choices:
            return ""
        content = ((choices[0].get("message") or {}).get("content")) or ""
        code = normalize_code(content)
        log.info("验证码识别结果：%r", code or content[:20])
        return code
    except Exception as exc:  # noqa: BLE001 - 识别失败只影响「自动试一次」
        log.warning("验证码识别异常，转人工：%s", exc)
        return ""


__all__ = ["MAX_BYTES", "is_available", "normalize_code", "solve_image_captcha"]
