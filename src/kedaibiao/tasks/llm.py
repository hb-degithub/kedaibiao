"""大模型答题器（OpenAI 兼容协议）。

## 为什么是「兼容协议」而不是绑定某一家

OpenAI 的 ``/chat/completions`` 已经成了事实标准，国内外大多数服务都兼容
（DeepSeek、通义、智谱、vLLM、Ollama 都行）。所以这里只依赖这一个协议形状，
换服务只改三个环境变量：

```
KEDAIBIAO_LLM_BASE_URL=https://api.deepseek.com/v1
KEDAIBIAO_LLM_API_KEY=sk-...
KEDAIBIAO_LLM_MODEL=deepseek-chat
```

## 三条设计原则

1. **没配 key 不是错误**：降级成「不参与」，让答题链继续往下走。
   一个可选能力不该让整个跑批起不来。
2. **模型输出必须校验**：模型会返回选项越界、多选给了单值、置信度瞎报。
   越界的答案**一律丢弃**（返回空），而不是猜一个。
3. **低置信度不改」假装确定」**：模型自报的 confidence 会打折后再上报，
   因为实测模型对自己答案的把握普遍偏乐观。

## 与提交的关系

答题器只负责「答出来」，**不负责交**。提交仍然要走流水线的门槛
（最低置信度 + dry_run + 平台能力），所以模型答错也不会被悄悄交上去。

## 图片题（视觉作答）

学习通等平台的公式是 p.ananas.chaoxing.com 上的 png 截图，纯文本链路读不
到，只能发多模态消息。`VisionAnswerer` 在文本链路之上加了一层：

- 题干图、选项图来自 ``Question.raw``：``{"images": [url...],
  "option_images": {0: [url...], "A": [url...]}}``（键支持选项下标/字母）；
- 逐张下载（HttpClient，15s 超时，带浏览器 UA + Referer），转 base64
  data URL，单图 ≤1.5MB、每次最多 4 张；
- 一次请求只带一道图片题（图片与题目的对应关系无法跨题表达），没有图片的
  题仍然走原来的文本批量链路，行为完全不变；
- 下载全失败 / 请求失败 / 解析失败 → 空 Answer，不抛异常，交给下一级答題器。
- 视觉模型可用 ``KEDAIBIAO_LLM_VISION_MODEL`` 单独指定，默认沿用 LLM_MODEL。
"""

from __future__ import annotations

import base64
import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from kedaibiao.core.models import Answer, AnswerSource, Question, QuestionType
from kedaibiao.net.http import HttpClient
from kedaibiao.tasks.answerer import Answerer

log = logging.getLogger(__name__)

#: 模型自报置信度的折扣系数。实测模型普遍高估自己，所以打折上报。
CONFIDENCE_DISCOUNT = 0.85

#: 默认 User-Agent：很多中转网关前面挂了 Cloudflare，Python 默认 UA 会被
#: 直接 403（error code: 1010，浏览器完整性检查）。用浏览器 UA 才放行。
#: 可用 ``KEDAIBIAO_LLM_USER_AGENT`` 覆盖。
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

#: 单次请求最多带多少道题（太多会让模型漏题、格式跑偏）
MAX_BATCH = 10

#: 单张图片的原始字节上限。题面公式截图通常 5~80KB，1.5MB 已经非常宽松；
#: 再大就不要了 —— 一次请求塞几十 MB 只会让网关卡死。
MAX_IMAGE_BYTES = 1_500_000

#: 单次视觉请求最多带几张图。题干 + 选项加起来一般 1~3 张，4 张足够覆盖。
MAX_IMAGES = 4

#: 下载题面图片的超时（秒）。图片托管在 CDN 上，正常几十毫秒就能回。
IMAGE_TIMEOUT = 15.0

#: 下载图片时带的 Referer：学习通 CDN 对来源有校验，带上更像真实浏览器。
IMAGE_REFERER = "https://mooc1.chaoxing.com/"

#: 图片下载请求头。除了 UA（用户可配）其余固定，够过 CDN 的常规校验。
_IMAGE_ACCEPT = "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"

#: 通过 magic 判断图片类型；CDN 偶尔回 application/octet-stream。
_MIME_BY_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF8", "image/gif"),
    (b"BM", "image/bmp"),
)

_SYSTEM_PROMPT = """你是一个严谨的答题助手。你会收到若干道题，请逐题作答。

只能用 JSON 回答，格式严格如下：
{"answers": [{"index": 0, "choices": [1], "text": "", "confidence": 0.9, "reason": "简短理由"}]}

规则：
- sequence 对应题目的 index（从 0 开始），必须每题都出现，不得遗漏。
- 选择题：choices 填选项下标（从 0 开始）。单选只给 1 个；多选给全部正确项，升序。
- 填空/简答：choices 留空数组，把答案文本放进 text。
- 不确定就老实给低 confidence（0~1），不要编造。
- 不要输出 JSON 之外的任何文字。"""

_VISION_SYSTEM_PROMPT = """你是一个严谨的答题助手。你会收到一道题，题干
和选项里可能夹着图片（公式截图、电路图、几何图）。图片按「图1、图2…」的
顺序随消息附上，文字的「图片清单」里写明了每张图对应题干还是哪个选项。

只能用 JSON 回答，格式严格如下：
{"choices": ["A"], "confidence": 0.9, "text": "", "reason": "简短理由"}

规则：
- 先把图片里的公式/符号读出来，再据此作答；读不清就降低 confidence。
- 选择题：choices 填选项字母（A、B、C…，第一个选项是 A）。单选只给 1 个；
  多选给全部正确项。
- 判断题：也是按选项顺序给字母（第一个选项是 A）。
- 填空/简答：choices 留空数组，答案文本放进 text。
- 不确定就老实给低 confidence（0~1），不要编造。
- 不要输出 JSON 之外的任何文字。"""


@dataclass(slots=True)
class LLMConfig:
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    timeout: float = 60.0
    temperature: float = 0.0
    max_batch: int = MAX_BATCH
    user_agent: str = DEFAULT_USER_AGENT
    #: 视觉模型。留空表示沿用 ``model``（有些网关一个模型文本/图片通吃）。
    vision_model: str = ""
    #: 下载题面图片的超时（秒）。
    image_timeout: float = IMAGE_TIMEOUT
    #: 单次视觉请求最多带几张图。
    max_images: int = MAX_IMAGES
    #: 单张图片的字节上限。
    max_image_bytes: int = MAX_IMAGE_BYTES

    @property
    def enabled(self) -> bool:
        return bool(self.base_url and self.model)

    @property
    def vision_model_name(self) -> str:
        """视觉请求实际用的模型名：配了 VISION_MODEL 就用它。"""
        return self.vision_model or self.model

    @classmethod
    def from_env(cls, environ: dict[str, str]) -> LLMConfig:
        def get(name: str, default: str = "") -> str:
            return (environ.get(f"KEDAIBIAO_LLM_{name}") or default).strip()

        return cls(
            base_url=get("BASE_URL").rstrip("/"),
            api_key=get("API_KEY"),
            model=get("MODEL"),
            timeout=float(get("TIMEOUT", "60") or 60),
            temperature=float(get("TEMPERATURE", "0") or 0),
            user_agent=get("USER_AGENT") or DEFAULT_USER_AGENT,
            vision_model=get("VISION_MODEL"),
            image_timeout=float(get("IMAGE_TIMEOUT", str(IMAGE_TIMEOUT)) or IMAGE_TIMEOUT),
            max_images=int(get("MAX_IMAGES", str(MAX_IMAGES)) or MAX_IMAGES),
            max_image_bytes=int(get("MAX_IMAGE_BYTES", str(MAX_IMAGE_BYTES)) or MAX_IMAGE_BYTES),
        )

    def describe(self) -> str:
        """给日志/界面看的安全描述 —— **绝不带 api_key**。"""
        if not self.enabled:
            return "未配置"
        vision = f"（视觉 {self.vision_model}）" if self.vision_model else ""
        return (
            f"{self.model}{vision} @ {self.base_url}"
            f"（key={'已设置' if self.api_key else '未设置'}）"
        )


class LLMError(Exception):
    pass


#: JSON 合法的反斜杠转义字符
_JSON_ESCAPES = set('"\\/bfnrtu')


def _repair_json_escapes(text: str) -> str:
    """修掉模型输出里「非法 JSON 转义」。

    典型场景：模型在 reason 里写 LaTeX ``\\(\bar A\\)``，而 JSON 里 ``\\(``
    不是合法转义，``json.loads`` 直接报错 —— 内容其实是对的。做法是把
    「反斜杠 + 非合法转义字符」补成 ``\\\\``；已经合法的转义保持不动。
    """
    if not text or "\\" not in text:
        return text
    out: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char != "\\" or index + 1 >= length:
            out.append(char)
            index += 1
            continue
        nxt = text[index + 1]
        if nxt in _JSON_ESCAPES:
            if nxt == "u":
                hex_part = text[index + 2 : index + 6]
                if len(hex_part) == 4 and all(
                    ch in "0123456789abcdefABCDEF" for ch in hex_part
                ):
                    out.append(text[index : index + 6])
                    index += 6
                    continue
            out.append(char + nxt)
            index += 2
            continue
        # 非法转义：把反斜杠本身转义掉，后面的字符原样保留
        out.append("\\\\")
        index += 1
    return "".join(out)


def _extract_json(text: str) -> dict[str, Any]:
    """从模型输出里抠出 JSON。

    模型很喜欢加 ```json 围栏或者前后废话，所以先直接试、再抠第一个平衡的
    ``{...}``。都不成就抛错 —— 不猜。
    """
    text = (text or "").strip()
    if not text:
        raise LLMError("模型返回空内容")
    text = _repair_json_escapes(text)
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass

    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fenced:
        try:
            data = json.loads(fenced.group(1))
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass

    start = text.find("{")
    if start >= 0:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        data = json.loads(text[start : i + 1])
                        if isinstance(data, dict):
                            return data
                    except json.JSONDecodeError:
                        break
    raise LLMError(f"模型输出不是合法 JSON: {text[:120]!r}")


def _render_question(index: int, question: Question) -> str:
    stem = question.stem or "（平台未提供题干）"
    lines = [f"[{index}] 题型: {question.type.value}", f"题干: {stem}"]
    if question.options:
        lines.append("选项:")
        for i, option in enumerate(question.options):
            lines.append(f"  {i}. {option}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 图片题：从 Question.raw 里取图、下载、转 data URL
# --------------------------------------------------------------------------

_LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"

#: 只认 http(s) 图片地址。data URL 直接放行没有意义（上层拿到的就是 URL）。
_URL_PREFIXES = ("http://", "https://")


def _raw_images(question: Question) -> dict[str, Any]:
    """``question.raw`` 不一定是 dict（平台可能塞 None/其它），取不出来就当空。"""
    raw = question.raw
    return raw if isinstance(raw, dict) else {}


def _as_url_list(value: Any) -> list[str]:
    """把平台给的各种形状（str / list / {"url": ...}）统一成 URL 列表。"""
    if not value:
        return []
    if isinstance(value, str):
        value = [value]
    if isinstance(value, dict):
        return _as_url_list(value.get("url"))
    if not isinstance(value, (list, tuple)):
        return []

    urls: list[str] = []
    for item in value:
        if isinstance(item, str):
            url = item.strip()
            if url.startswith(_URL_PREFIXES):
                urls.append(url)
        elif isinstance(item, dict):
            urls.extend(_as_url_list(item.get("url") or item.get("src")))
    return urls


def _option_index(key: Any, count: int) -> int | None:
    """选项键 → 下标。支持 0 基下标、数字字符串、字母（大小写都行）。"""
    if isinstance(key, bool):
        return None
    if isinstance(key, int):
        return key if 0 <= key < count else None
    token = str(key or "").strip().upper()
    if not token:
        return None
    if len(token) == 1 and token.isalpha():
        index = ord(token) - ord("A")
    elif token.isdigit():
        index = int(token)
    else:
        return None
    return index if 0 <= index < count else None


def _option_images(question: Question) -> dict[int, list[str]]:
    """选项图片：``{"option_images": {0|"0"|"A": [url...]}}``，也接受按选项顺序的列表。"""
    raw = _raw_images(question)
    mapping = raw.get("option_images")
    count = len(question.options)
    out: dict[int, list[str]] = {}
    if isinstance(mapping, dict):
        for key, value in mapping.items():
            index = _option_index(key, count)
            if index is not None:
                out.setdefault(index, []).extend(_as_url_list(value))
    elif isinstance(mapping, (list, tuple)):
        for index, value in enumerate(mapping):
            if index < count:
                out.setdefault(index, []).extend(_as_url_list(value))
    return out


def _image_candidates(question: Question) -> list[tuple[str, str]]:
    """按发送顺序返回 ``(说明文字, url)``：题干图在前，选项图按选项顺序。

    题干图既认 ``images`` 也认 ``stem_images``（不同解析器叫法不一样）；
    同一 URL 只发一次，避免题干和选项里出现同一张图时重复计费。
    """
    raw = _raw_images(question)
    seen: set[str] = set()
    out: list[tuple[str, str]] = []

    def add(description: str, urls: list[str]) -> None:
        for url in urls:
            if url not in seen:
                seen.add(url)
                out.append((description, url))

    stem_urls = _as_url_list(raw.get("images")) + _as_url_list(raw.get("stem_images"))
    add("题干图片", stem_urls)
    for index, urls in sorted(_option_images(question).items()):
        letter = _LETTERS[index] if index < len(_LETTERS) else str(index)
        add(f"选项{letter}的图片", urls)
    return out


def image_urls(question: Question) -> list[str]:
    """这道题（题干 + 选项）里所有的图片地址，按发送顺序去重。

    给上层做「这题要不要走视觉」的判断，也方便报告里展示。
    """
    return [url for _, url in _image_candidates(question)]


def has_images(question: Question) -> bool:
    """这道题是否带图（决定走视觉链路还是原文本链路）。"""
    return bool(_image_candidates(question))


def _sniff_image_mime(headers: dict[str, str], body: bytes) -> str:
    """判断下载到的是不是图片、是什么图片。不是图片返回空串（调用方跳过）。

    先看 magic：CDN 有时回 ``application/octet-stream``，声明不可信；
    声明是 ``text/html`` 的大概率是登录页/风控页，必须拒。
    """
    for magic, mime in _MIME_BY_MAGIC:
        if body.startswith(magic):
            return mime
    if body[:4] == b"RIFF" and body[8:12] == b"WEBP":
        return "image/webp"

    declared = ""
    for key, value in headers.items():
        if key.lower() == "content-type":
            declared = value.split(";")[0].strip().lower()
            break
    return declared if declared.startswith("image/") else ""


def _data_url(mime: str, body: bytes) -> str:
    """字节 → OpenAI 多模态协议要的 ``data:`` URL。"""
    return f"data:{mime};base64,{base64.b64encode(body).decode('ascii')}"


def _render_vision_text(question: Question, descriptions: list[str], skipped: int = 0) -> str:
    """视觉请求的文本部分：题干 + 选项 + 「图N 是哪张图」的对应关系。"""
    lines = [
        f"题型: {question.type.value}",
        f"题干: {question.stem.strip() or '（题面只有图片）'}",
    ]
    if question.options:
        lines.append("选项:")
        for i, option in enumerate(question.options):
            letter = _LETTERS[i] if i < len(_LETTERS) else str(i)
            text = (option or "").strip() or "（见图片）"
            lines.append(f"  {letter}. {text}")
    if descriptions:
        lines.append("图片清单（消息里按此顺序附图）:")
        for number, description in enumerate(descriptions, 1):
            lines.append(f"- 图{number}: {description}")
    if skipped:
        lines.append(f"注意：另有 {skipped} 张图片下载失败，已跳过。")
    return "\n".join(lines)


def _parse_vision_reply(content: str) -> dict[str, Any]:
    """解析视觉模型的回答（沿用 ``_extract_json`` 的容错）。

    允许「单题对象」和「answers 数组」两种形状，也允许 ``choice`` 单值。
    """
    parsed = _extract_json(content)
    answers = parsed.get("answers")
    if isinstance(answers, list) and answers and isinstance(answers[0], dict):
        parsed = answers[0]
    if "choices" not in parsed and "choice" in parsed:
        parsed = {**parsed, "choices": parsed.get("choice")}
    return parsed


def _choice_tokens(value: Any) -> list[str]:
    """把模型给的选项值拆成 token：``"A,C"`` / ``["A", "C"]`` / ``[0, 2]`` 都行。"""
    if isinstance(value, bool):
        return []
    if isinstance(value, int):
        return [] if value < 0 else [str(value)]
    text = str(value or "").strip().upper()
    if not text:
        return []
    return [token for token in re.split(r"[\s,，、/;；.。]+", text) if token]


def _token_to_index(token: str, count: int) -> int | None:
    """单 token → 选项下标：字母按 A=0，数字按 0 基下标。越界返回 None。"""
    token = token.strip().upper()
    if not token:
        return None
    if len(token) == 1 and token.isalpha():
        index = ord(token) - ord("A")
    elif token.isdigit():
        index = int(token)
    else:
        return None
    return index if 0 <= index < count else None


def _normalize_vision_choices(raw_values: Any, count: int) -> list[int]:
    """视觉模型回的是字母（协议里要求的），这里统一成 0 基下标并去重升序。"""
    if raw_values is None:
        values: list[Any] = []
    elif isinstance(raw_values, list):
        values = raw_values
    else:
        values = [raw_values]

    chosen: set[int] = set()
    for value in values:
        for token in _choice_tokens(value):
            index = _token_to_index(token, count)
            if index is not None:
                chosen.add(index)
    return sorted(chosen)


class LLMAnswerer(Answerer):
    """用大模型作答。没有配置就完全不参与（不算错误）。"""

    name = "llm"

    def __init__(self, config: LLMConfig, client: HttpClient | None = None) -> None:
        self.config = config
        self._client = client
        self.last_error = ""

    # -- 网络 -------------------------------------------------------------

    def _http(self) -> HttpClient:
        if self._client is not None:
            return self._client
        from kedaibiao.net.http import UrllibTransport  # 延迟导入，便于测试注入

        self._client = HttpClient(UrllibTransport(), timeout=self.config.timeout)
        return self._client

    def _headers(self) -> dict[str, str]:
        """公共请求头。UA 必须是浏览器 UA（Cloudflare 会 403 Python 默认 UA）。"""
        headers = {
            "Content-Type": "application/json",
            "User-Agent": self.config.user_agent,
            "Accept": "application/json, text/plain, */*",
        }
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        return headers

    async def _ask(self, questions: list[Question], offset: int) -> dict[int, dict[str, Any]]:
        """问一批题，返回 {全局下标: 模型给的答案}。"""
        body = "\n\n".join(_render_question(i, q) for i, q in enumerate(questions))
        payload = {
            "model": self.config.model,
            "temperature": self.config.temperature,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": body},
            ],
        }

        response = await self._http().post_json(
            f"{self.config.base_url}/chat/completions", payload, headers=self._headers()
        )
        if not response.ok:
            raise LLMError(f"HTTP {response.status}: {response.text()[:200]}")

        data = response.json()
        choices = data.get("choices") or []
        if not choices:
            raise LLMError(f"响应里没有 choices: {str(data)[:200]}")
        content = ((choices[0].get("message") or {}).get("content")) or ""
        parsed = _extract_json(content)

        result: dict[int, dict[str, Any]] = {}
        for item in parsed.get("answers") or []:
            if not isinstance(item, dict):
                continue
            try:
                index = int(item.get("index"))
            except (TypeError, ValueError):
                continue
            if 0 <= index < len(questions):
                result[offset + index] = item
        return result

    async def complete(self, system: str, user: str, *, max_tokens: int = 1200) -> str:
        """通用补全（改写作文这类自由文本任务用）。没配置就抛，由调用方降级。"""
        if not self.config.enabled:
            raise LLMError("大模型未配置")
        payload = {
            "model": self.config.model,
            "temperature": self.config.temperature,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        response = await self._http().post_json(
            f"{self.config.base_url}/chat/completions", payload, headers=self._headers()
        )
        if not response.ok:
            raise LLMError(f"HTTP {response.status}: {response.text()[:200]}")
        data = response.json()
        choices = data.get("choices") or []
        if not choices:
            raise LLMError("模型没有返回 choices")
        return ((choices[0].get("message") or {}).get("content") or "").strip()

    # -- 答题 -------------------------------------------------------------

    async def answer(self, questions: list[Question]) -> list[Answer]:
        empty = [self._empty(q) for q in questions]
        if not self.config.enabled:
            return empty
        if not questions:
            return []

        out: list[Answer] = list(empty)
        batch_size = max(1, min(self.config.max_batch, len(questions)))
        for start in range(0, len(questions), batch_size):
            chunk = questions[start : start + batch_size]
            try:
                answers = await self._ask(chunk, start)
            except Exception as exc:  # noqa: BLE001 - 模型/网络问题不该炸整批
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.warning("大模型答题失败（%d 题），降级交给下一级答题器: %s",
                            len(chunk), self.last_error)
                continue

            for index, item in answers.items():
                answer = self._to_answer(questions[index], item)
                if answer is not None:
                    out[index] = answer
        return out

    def _to_answer(self, question: Question, item: dict[str, Any]) -> Answer | None:
        """把模型输出校验成 Answer。任何越界/不合法一律丢弃。"""
        raw_choices = item.get("choices")
        choices: list[int] = []
        if isinstance(raw_choices, list):
            for value in raw_choices:
                try:
                    index = int(value)
                except (TypeError, ValueError):
                    continue
                if 0 <= index < len(question.options):
                    choices.append(index)
        choices = sorted(set(choices))

        text = str(item.get("text") or "").strip()

        # 校验：选择题必须有合法选项；多选至少一个；填空必须有文本
        if question.type in (QuestionType.SINGLE, QuestionType.JUDGE, QuestionType.MULTIPLE):
            if not choices:
                return None
            if question.type is not QuestionType.MULTIPLE and len(choices) > 1:
                log.debug("模型给单选/判断回了多个选项，只取第一个: %s", choices)
                choices = choices[:1]
        elif question.type in (QuestionType.FILL, QuestionType.ESSAY):
            if not text:
                return None
            choices = []
        else:
            if not choices and not text:
                return None

        try:
            reported = float(item.get("confidence", 0.0))
        except (TypeError, ValueError):
            reported = 0.0
        confidence = max(0.0, min(1.0, reported)) * CONFIDENCE_DISCOUNT

        return Answer(
            question=question,
            choices=choices,
            text=text,
            source=AnswerSource.LLM,
            confidence=round(confidence, 4),
            raw={"reason": str(item.get("reason") or "")[:500]},
        )


class VisionAnswerer(LLMAnswerer):
    """能读图的答题器：图片题走多模态请求，纯文本题仍走原来的批量链路。

    为什么是子类而不是改 ``LLMAnswerer``：
    ``LLMAnswerer`` 的文本链路（批量、校验、降级）已经被大量调用方依赖，
    这里只「加一条旁路」，不改它的任何行为。``build_llm_answerer`` 返回的就
    是这个子类，所以 ``isinstance(x, LLMAnswerer)`` 的现有代码不受影响。

    图片题的输入约定（``Question.raw``）：

    ```python
    Question(
        stem="如图所示，物体加速度为",
        options=["2 m/s²", "4 m/s²"],
        raw={
            "images": ["https://p.ananas.chaoxing.com/star3/origin/xx.png"],  # 题干图
            "option_images": {0: ["https://.../a.png"], "B": ["https://.../b.png"]},  # 选项图
        },
    )
    ```

    - ``option_images`` 的键支持 0 基下标、数字字符串、选项字母（A/a 都行），
      也接受「按选项顺序的列表」；题干图也认 ``stem_images`` 这个别名；
    - 只有带图（且图能下下来）的题才走视觉，每次请求一道题；没图的题完全
      沿用父类，行为与纯文本链路一致；
    - 图下载全失败 / 请求失败 / 模型输出解析失败 → 空 Answer（source=UNKNOWN,
      confidence=0），**不抛异常**，让 ChainAnswerer 降级到下一级。
    """

    name = "llm-vision"

    #: 能力标记：上层可用 ``getattr(answerer, "supports_images", False)`` 判断
    #: 要不要把 ``Question.raw`` 里的图片 URL 交给它。
    supports_images = True

    async def answer(self, questions: list[Question]) -> list[Answer]:
        empty = [self._empty(q) for q in questions]
        if not self.config.enabled:
            return empty
        if not questions:
            return []

        out: list[Answer] = list(empty)
        visual = [i for i, q in enumerate(questions) if has_images(q)]
        if not visual:
            # 一道图片题都没有 —— 与纯文本答题器完全等价（含分批）。
            return await super().answer(questions)

        # 图片题一道一请求（图与题的对应关系没法跨题表达），文本题照旧批量。
        visual_set = set(visual)
        plain = [i for i in range(len(questions)) if i not in visual_set]
        if plain:
            produced = await super().answer([questions[i] for i in plain])
            for index, answer in zip(plain, produced, strict=True):
                out[index] = answer
        for index in visual:
            answer = await self._answer_visual(questions[index])
            if answer is not None:
                out[index] = answer
        return out

    # -- 视觉链路 ---------------------------------------------------------

    async def _download_image(self, url: str) -> tuple[str, bytes] | None:
        """下载一张题面图。任何失败都返回 ``None``（跳过这一张，不炸整题）。"""
        headers = {
            "User-Agent": self.config.user_agent,
            "Referer": IMAGE_REFERER,
            "Accept": _IMAGE_ACCEPT,
        }
        try:
            response = await self._http().get(
                url, headers=headers, timeout=self.config.image_timeout
            )
        except Exception as exc:  # noqa: BLE001 - 单张图失败不该影响整题
            log.debug("题面图片下载失败 %s: %s", url, exc)
            return None

        body = response.body
        if not response.ok or not body:
            log.debug("题面图片下载失败 %s: HTTP %s", url, response.status)
            return None
        if len(body) > self.config.max_image_bytes:
            log.warning(
                "题面图片过大（%d 字节 > %d），跳过: %s",
                len(body), self.config.max_image_bytes, url,
            )
            return None
        mime = _sniff_image_mime(response.headers, body)
        if not mime:
            log.debug("题面图片不是图片（Content-Type=%s），跳过: %s",
                      response.headers.get("Content-Type", ""), url)
            return None
        return mime, body

    async def _answer_visual(self, question: Question) -> Answer | None:
        """单道图片题：下载图 → 发多模态请求 → 校验成 Answer。失败返回 None。"""
        try:
            return await self._ask_visual(question)
        except Exception as exc:  # noqa: BLE001 - 视觉失败必须降级，不能抛给上层
            self.last_error = f"{type(exc).__name__}: {exc}"
            log.warning("图片题作答失败，降级交给下一级答题器: %s", self.last_error)
            return None

    async def _ask_visual(self, question: Question) -> Answer | None:
        candidates = _image_candidates(question)[: max(0, self.config.max_images)]
        if not candidates:
            # 只可能发生在把 MAX_IMAGES 配成 0 的时候；不是错误，但要说清楚。
            self.last_error = "没有可用的题面图片（检查 KEDAIBIAO_LLM_MAX_IMAGES）"
            log.warning("图片题没有可下载的图片地址，放弃该题: %s", question.stem[:40])
            return None

        descriptions: list[str] = []
        parts: list[dict[str, Any]] = []
        for description, url in candidates:
            downloaded = await self._download_image(url)
            if downloaded is None:
                continue
            mime, body = downloaded
            descriptions.append(description)
            parts.append({"type": "image_url", "image_url": {"url": _data_url(mime, body)}})

        if not descriptions:
            # 一张都没下下来：拿纯文本去问也没有意义（题面通常只有「如图」），
            # 直接交空答案，让 ChainAnswerer 走下一级。
            self.last_error = "题面图片全部下载失败，无法视觉作答"
            log.warning("题面图片全部下载失败，放弃该题: %s", question.stem[:40])
            return None

        body_text = _render_vision_text(
            question, descriptions, skipped=len(candidates) - len(descriptions)
        )
        payload = {
            "model": self.config.vision_model_name,
            "temperature": self.config.temperature,
            "messages": [
                {"role": "system", "content": _VISION_SYSTEM_PROMPT},
                {"role": "user", "content": [{"type": "text", "text": body_text}, *parts]},
            ],
        }
        response = await self._http().post_json(
            f"{self.config.base_url}/chat/completions", payload, headers=self._headers()
        )
        if not response.ok:
            raise LLMError(f"HTTP {response.status}: {response.text()[:200]}")

        data = response.json()
        choices = data.get("choices") or []
        if not choices:
            raise LLMError(f"响应里没有 choices: {str(data)[:200]}")
        content = ((choices[0].get("message") or {}).get("content")) or ""
        return self._to_visual_answer(question, _parse_vision_reply(content), len(parts))

    def _to_visual_answer(
        self, question: Question, item: dict[str, Any], image_count: int
    ) -> Answer | None:
        """把视觉模型的输出校验成 Answer。字母 → 下标后复用父类的校验逻辑。"""
        normalized = {
            "choices": _normalize_vision_choices(item.get("choices"), len(question.options)),
            "text": item.get("text"),
            "confidence": item.get("confidence", 0.0),
            "reason": item.get("reason"),
        }
        answer = self._to_answer(question, normalized)
        if answer is not None:
            answer.raw["vision_images"] = image_count
        return answer


def build_llm_answerer(
    environ: dict[str, str], client: HttpClient | None = None
) -> LLMAnswerer | None:
    """按环境变量造一个答题器；没配置返回 ``None``（调用方跳过即可）。

    返回的是 ``VisionAnswerer``（``LLMAnswerer`` 的子类）：纯文本题行为不变，
    带图题自动走视觉。``isinstance(x, LLMAnswerer)`` 的既有调用方无需改动。
    """
    config = LLMConfig.from_env(environ)
    if not config.enabled:
        return None
    return VisionAnswerer(config, client=client)

