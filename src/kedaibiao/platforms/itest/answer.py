"""iTEST 用大模型作答 —— 把结构化试卷变成答案单 ``{qid: 答案}``。

## 能自动答的两类

- **填空（词汇选择 / 词汇匹配）**：题干里是一组中文释义选项（``A) … B) …``），
  原句里用 ``[[词]]`` 标出要填的那个英文词。让模型把每个词对到释义字母。
- **翻译**：中文句子 → 英文译文。

## 听力（单选）为什么不代答

短文听力的小题答案取决于**音频内容**，文字模型听不到。没有音频转写就不猜 ——
留空由人补。这样交卷动作会因为「核验有缺口」而拒绝，不会把没答的卷子悄悄交上去。

## 答案形态

与 ``paper.fill_script`` 完全一致：填空是空格分隔的字母串（``"F I G C"``），
翻译是整句英文。所以生成完可以直接喂给填表 / 交卷动作。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from kedaibiao.platforms.itest.paper import (
    KIND_BLANK,
    KIND_RADIO,
    KIND_WRITE,
    Paper,
    Question,
)
from kedaibiao.tasks.llm import LLMAnswerer, LLMError, _extract_json

log = logging.getLogger(__name__)

#: 一次问多少道翻译（太多模型会漏题，太少慢）
WRITE_BATCH = 5

_SYSTEM = "你是严谨的大学英语题目解答器。只输出 JSON，不要输出任何解释。"

_LETTERS = re.compile(r"([A-Z])\s*[)）]")

#: 「字数统计： 0」这种界面残留要清掉再喂模型
_COUNTER = re.compile(r"字数统计.*$", re.S)


class AnswerError(RuntimeError):
    """模型答案不合法（数量/字母对不上）。"""


@dataclass(slots=True)
class AnswerSheet:
    answers: dict[str, str] = field(default_factory=dict)
    #: 每题一条，给控制台看清楚模型给了什么
    details: list[dict[str, Any]] = field(default_factory=list)
    #: 跳过没答的题（听力等），附原因
    skipped: list[dict[str, str]] = field(default_factory=list)

    @property
    def answered(self) -> int:
        return len(self.answers)

    def to_dict(self) -> dict[str, Any]:
        return {
            "answers": self.answers,
            "details": self.details,
            "skipped": self.skipped,
            "answered": self.answered,
        }


def blank_letters(question: Question) -> list[str]:
    """从题干里取「A) B) C)…」这些选项字母（去重、保序）。"""
    seen: list[str] = []
    for letter in _LETTERS.findall(question.text or ""):
        if letter not in seen:
            seen.append(letter)
    return seen


def clean_chinese(text: str) -> str:
    """去掉题面里的界面残留（字数统计之类）。"""
    return _COUNTER.sub("", text or "").strip()


async def answer_paper(
    paper: Paper, llm: LLMAnswerer, *, kinds: tuple[str, ...] = (KIND_BLANK, KIND_WRITE)
) -> AnswerSheet:
    """给整份试卷生成答案单。逐题型处理，失败的题记进 ``skipped``。"""
    sheet = AnswerSheet()
    for section in paper.sections:
        blanks = [q for q in section.questions if q.kind == KIND_BLANK and KIND_BLANK in kinds]
        writes = [q for q in section.questions if q.kind == KIND_WRITE and KIND_WRITE in kinds]

        for question in blanks:
            try:
                letters = await _answer_blank(llm, question)
            except (LLMError, AnswerError) as exc:
                log.warning("AI 答填空题失败 %s: %s", question.qid, exc)
                sheet.skipped.append({
                    "qid": question.qid, "kind": "blank", "reason": str(exc)[:120],
                })
                continue
            sheet.answers[question.qid] = " ".join(letters)
            sheet.details.append(
                {"qid": question.qid, "kind": "blank", "answer": " ".join(letters),
                 "slots": len(letters)}
            )

        for start in range(0, len(writes), WRITE_BATCH):
            chunk = writes[start:start + WRITE_BATCH]
            try:
                texts = await _answer_writes(llm, chunk)
            except (LLMError, AnswerError) as exc:
                log.warning("AI 答翻译题失败（%d 题）: %s", len(chunk), exc)
                for question in chunk:
                    sheet.skipped.append(
                        {"qid": question.qid, "kind": "write", "reason": str(exc)[:120]}
                    )
                continue
            for question, text in zip(chunk, texts, strict=False):
                sheet.answers[question.qid] = text
                sheet.details.append(
                    {"qid": question.qid, "kind": "write", "answer": text[:60],
                     "slots": 1}
                )

        for question in section.questions:
            if question.kind == KIND_RADIO:
                sheet.skipped.append({
                    "qid": question.qid, "kind": "radio",
                    "reason": "听力题依赖音频内容，文字模型听不到；需要音频转写或人工填",
                })
    return sheet


async def _answer_blank(llm: LLMAnswerer, question: Question) -> list[str]:
    letters = blank_letters(question)
    count = len(question.blanks) or len(question.sentences)
    if not letters or not question.sentences:
        raise AnswerError("这题没有选项或句子，无法作答")
    if count and len(question.sentences) < count:
        count = len(question.sentences)

    numbered = "\n".join(
        f"{i}. {sentence}" for i, sentence in enumerate(question.sentences[:count], 1)
    )
    prompt = (
        "下面是大学英语词汇题：一组中文释义选项 + 若干句子。\n"
        "每个句子里用 [[英文词]] 标出的词，就是要用选项释义去对的那个词。\n\n"
        f"选项：\n{question.text}\n\n"
        f"句子（按顺序，共 {count} 个）：\n{numbered}\n\n"
        f"请为每个 [[词]] 选最合适的选项字母（只能用 {', '.join(letters)} 里的）。\n"
        f'只输出 JSON：{{"letters": ["A", "B", ...]}}，必须正好 {count} 个字母。'
    )
    if len(question.sentences) > count:
        log.debug("qid %s 句子比空多：%d > %d", question.qid, len(question.sentences), count)

    last = ""
    for _attempt in range(2):
        content = await llm.complete(_SYSTEM, prompt, max_tokens=600)
        try:
            data = _extract_json(content)
            raw = data.get("letters")
            picks = [str(x).strip().upper()[:1] for x in raw if str(x).strip()]
        except Exception:  # noqa: BLE001 - 模型偶尔会返回烂 JSON
            last = f"返回不是合法 JSON: {content[:80]}"
            continue
        if len(picks) != count:
            last = f"字母数不对：要 {count} 个，给了 {len(picks)} 个"
            continue
        bad = [p for p in picks if p not in letters]
        if bad:
            last = f"给了不在选项里的字母：{bad}"
            continue
        return picks
    raise AnswerError(last or "模型没给出可用答案")


async def _answer_writes(llm: LLMAnswerer, questions: list[Question]) -> list[str]:
    numbered = "\n".join(
        f"{i}. {clean_chinese(q.text)}" for i, q in enumerate(questions, 1)
    )
    prompt = (
        "把下面的中文句子翻译成英文（句意准确、语法正确、符合英语表达习惯）。\n\n"
        f"{numbered}\n\n"
        "只输出 JSON：{\"translations\": [\"第一句译文\", \"第二句译文\", ...]}，"
        f"必须正好 {len(questions)} 条，顺序与题目一致。"
    )
    content = await llm.complete(_SYSTEM, prompt, max_tokens=4096)
    try:
        data = _extract_json(content)
        raw = data.get("translations")
        texts = [str(x).strip() for x in raw if str(x).strip()]
    except Exception as exc:  # noqa: BLE001
        raise AnswerError(f"返回不是合法 JSON: {content[:80]}") from exc
    if len(texts) != len(questions):
        raise AnswerError(f"译文条数不对：要 {len(questions)} 条，给了 {len(texts)} 条")
    return texts


def answers_json(sheet: AnswerSheet) -> str:
    """给控制台/落盘用的 JSON（只含 qid → 答案）。"""
    return json.dumps(sheet.answers, ensure_ascii=False)
