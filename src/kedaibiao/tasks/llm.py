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
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
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

_SYSTEM_PROMPT = """你是一个严谨的答题助手。你会收到若干道题，请逐题作答。

只能用 JSON 回答，格式严格如下：
{"answers": [{"index": 0, "choices": [1], "text": "", "confidence": 0.9, "reason": "简短理由"}]}

规则：
- sequence 对应题目的 index（从 0 开始），必须每题都出现，不得遗漏。
- 选择题：choices 填选项下标（从 0 开始）。单选只给 1 个；多选给全部正确项，升序。
- 填空/简答：choices 留空数组，把答案文本放进 text。
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

    @property
    def enabled(self) -> bool:
        return bool(self.base_url and self.model)

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
        )

    def describe(self) -> str:
        """给日志/界面看的安全描述 —— **绝不带 api_key**。"""
        if not self.enabled:
            return "未配置"
        return f"{self.model} @ {self.base_url}（key={'已设置' if self.api_key else '未设置'}）"


class LLMError(Exception):
    pass


def _extract_json(text: str) -> dict[str, Any]:
    """从模型输出里抠出 JSON。

    模型很喜欢加 ```json 围栏或者前后废话，所以先直接试、再抠第一个平衡的
    ``{...}``。都不成就抛错 —— 不猜。
    """
    text = (text or "").strip()
    if not text:
        raise LLMError("模型返回空内容")
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
    lines = [f"[{index}] 题型: {question.type.value}", f"题干: {question.stem or '（平台未提供题干）'}"]
    if question.options:
        lines.append("选项:")
        for i, option in enumerate(question.options):
            lines.append(f"  {i}. {option}")
    return "\n".join(lines)


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
        headers = {
            "Content-Type": "application/json",
            "User-Agent": self.config.user_agent,
            "Accept": "application/json, text/plain, */*",
        }
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"

        response = await self._http().post_json(
            f"{self.config.base_url}/chat/completions", payload, headers=headers
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
        headers = {
            "Content-Type": "application/json",
            "User-Agent": self.config.user_agent,
            "Accept": "application/json, text/plain, */*",
        }
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        response = await self._http().post_json(
            f"{self.config.base_url}/chat/completions", payload, headers=headers
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


def build_llm_answerer(environ: dict[str, str], client: HttpClient | None = None) -> LLMAnswerer | None:
    """按环境变量造一个答题器；没配置返回 ``None``（调用方跳过即可）。"""
    config = LLMConfig.from_env(environ)
    if not config.enabled:
        return None
    return LLMAnswerer(config, client=client)
