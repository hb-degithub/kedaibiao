"""iTEST 试卷解析与作答：把作答页 DOM 变成结构化题目，并能按答案填表。

## 为什么用 DOM 而不是拦截 ``load``

``load`` 返回的 ``C_HTML`` 本质上就是作答页的 HTML；直接从页面 DOM 取更稳：
少一个网络拦截环节（拦截容易漏、还要处理跨域 iframe），而且拿到的一定是
**当前真实渲染的那一份**，不会和界面不一致。

## 实测拿到的结构（2026-10-19，B2U5/B2U6）

```
div.itest-section[part1][part2][sectionType]
    └─ div.itest-ques[qid]
         ├─ 词汇选择/匹配：input.blankinput[qid][qsubindex]      ← 填选项字母
         ├─ 短文听力：input[type=radio][qid][qsubindex][value]   ← value 是**打乱后**的序号
         └─ 翻译：textarea[qid][qsubindex]                       ← 填整句译文
```

**单选必须按 label 文本里的字母选**：``value`` 与显示字母不一致，
``qoo="[2,0,3,1]"`` 就是置换表（实测 B 项 value=0、A 项 value=2）。
按顺序取第 N 个会选错。

## 不自己拼 ``ansData``

服务端的 ``ansData {al, sl}`` 结构只有 JS 常量证据、**从未真实发送过**。
所以这里**只操作界面**（设值 + 派发事件 + 点单选），让页面自己组装载荷 ——
猜测格式的风险直接归零。
"""

from __future__ import annotations

import html as html_mod
import json
import re
from dataclasses import dataclass, field
from typing import Any

#: 题型（来自 sectionType / 元素形态）
KIND_BLANK = "blank"        # 词汇选择 / 词汇匹配：一排填空，填选项字母
KIND_RADIO = "radio"        # 短文听力：单选
KIND_WRITE = "write"        # 翻译：整句文本

SECTION_KINDS = {
    "sNormal": KIND_BLANK,
    "sHear": KIND_RADIO,
    "sWrite": KIND_WRITE,
}


class PaperParseError(ValueError):
    pass


@dataclass(slots=True)
class Blank:
    """一个填空。"""

    index: int          # qsubindex
    placeholder: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"index": self.index, "placeholder": self.placeholder}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Blank:
        return cls(index=int(data.get("index") or 0),
                   placeholder=str(data.get("placeholder") or ""))


@dataclass(slots=True)
class Choice:
    """一个选项（听力用）。"""

    letter: str         # 显示字母 A/B/C/D
    text: str
    sub: int = 0        # 属于第几个小题（qsubindex）
    #: 线上 value —— 与 letter **不一致**（打乱过），填表时不能按它来
    value: str = ""

    def to_dict(self) -> dict[str, Any]:
        # sub 必须带上：radio 的 slot_count 按小题数算，丢了 sub 会少算空位
        return {"letter": self.letter, "text": self.text,
                "sub": self.sub, "value": self.value}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Choice:
        return cls(letter=str(data.get("letter") or ""),
                   text=str(data.get("text") or ""),
                   sub=int(data.get("sub") or 0),
                   value=str(data.get("value") or ""))


@dataclass(slots=True)
class Question:
    qid: str
    kind: str
    number: int = 0                    # 卷面序号（小题）
    text: str = ""                     # 题干 / 第一句
    blanks: list[Blank] = field(default_factory=list)
    choices: list[Choice] = field(default_factory=list)
    sentences: list[str] = field(default_factory=list)   # 挖空所在的原句
    answer: str = ""                   # 已填/应填（字母串或整句译文）

    @property
    def slot_count(self) -> int:
        if self.kind == KIND_BLANK:
            return len(self.blanks)
        if self.kind == KIND_RADIO:
            # 按小题数算：同一段听力下有多个小题，字母会重复
            subs = {c.sub for c in self.choices}
            return len(subs) or 1
        return 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "qid": self.qid, "kind": self.kind, "number": self.number,
            "text": self.text, "sentences": self.sentences,
            "blanks": [b.to_dict() for b in self.blanks],
            "choices": [c.to_dict() for c in self.choices],
            "slot_count": self.slot_count, "answer": self.answer,
        }


    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Question:
        return cls(
            qid=str(data.get("qid") or ""),
            kind=str(data.get("kind") or ""),
            number=int(data.get("number") or 0),
            text=str(data.get("text") or ""),
            blanks=[Blank.from_dict(b) for b in data.get("blanks") or []],
            choices=[Choice.from_dict(c) for c in data.get("choices") or []],
            sentences=[str(x) for x in data.get("sentences") or []],
            answer=str(data.get("answer") or ""),
        )


@dataclass(slots=True)
class Section:
    index: int
    part1: str = ""
    part2: str = ""
    section_type: str = ""
    audios: list[str] = field(default_factory=list)
    questions: list[Question] = field(default_factory=list)

    @property
    def kind(self) -> str:
        if self.questions:
            return self.questions[0].kind
        return SECTION_KINDS.get(self.section_type, "")

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index, "part1": self.part1, "part2": self.part2,
            "section_type": self.section_type, "kind": self.kind,
            "audios": self.audios,
            "questions": [q.to_dict() for q in self.questions],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Section:
        return cls(
            index=int(data.get("index") or 0),
            part1=str(data.get("part1") or ""),
            part2=str(data.get("part2") or ""),
            section_type=str(data.get("section_type") or ""),
            audios=[str(x) for x in data.get("audios") or []],
            questions=[Question.from_dict(q) for q in data.get("questions") or []],
        )


@dataclass(slots=True)
class Paper:
    sections: list[Section] = field(default_factory=list)
    title: str = ""
    ksd_id: str = ""

    @property
    def question_count(self) -> int:
        return sum(len(s.questions) for s in self.sections)

    @property
    def blank_count(self) -> int:
        return sum(q.slot_count for s in self.sections for q in s.questions)

    def by_qid(self) -> dict[str, Question]:
        return {q.qid: q for s in self.sections for q in s.questions}

    def apply_answers(self, answers: dict[str, str]) -> dict[str, Any]:
        """把答案套到题目上，返回统计（不校验正确性，只对位）。"""
        table = self.by_qid()
        applied, missing = 0, []
        for qid, answer in answers.items():
            question = table.get(qid)
            if question is None:
                missing.append(qid)
                continue
            question.answer = str(answer)
            applied += 1
        return {"applied": applied, "unknown_qids": missing,
                "questions": len(table), "answered": applied}

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title, "ksd_id": self.ksd_id,
            "section_count": len(self.sections),
            "question_count": self.question_count,
            "blank_count": self.blank_count,
            "sections": [s.to_dict() for s in self.sections],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Paper:
        """从 :meth:`to_dict` 的产物恢复（控制台缓存试卷用）。

        注意：``choices[].value`` / ``sub`` 不在 ``to_dict`` 里 —— 只有填表才需要
        它们，而填表是直接读页面 DOM 的，不靠缓存。
        """
        return cls(
            sections=[Section.from_dict(s) for s in data.get("sections") or []],
            title=str(data.get("title") or ""),
            ksd_id=str(data.get("ksd_id") or ""),
        )


# --------------------------------------------------------------------------
# 解析
# --------------------------------------------------------------------------

_TAG = re.compile(r"<[^>]+>")
_SCRIPT = re.compile(r"<script.*?</script>", re.S | re.I)


def _text(fragment: str) -> str:
    body = _SCRIPT.sub(" ", fragment or "")
    body = _TAG.sub(" ", body)
    return html_mod.unescape(re.sub(r"\s+", " ", body)).strip()


def _sections(html: str) -> list[str]:
    parts = re.split(r'(?=<div class="itest-section")', html or "")
    return [p for p in parts if p.startswith('<div class="itest-section"')]


def _attr(fragment: str, name: str) -> str:
    m = re.search(rf'{name}="([^"]*)"', fragment)
    return html_mod.unescape(m.group(1)).strip() if m else ""


def parse_paper(html: str) -> Paper:
    """把作答页 DOM 解析成 :class:`Paper`。

    结构不符时**返回已解析到的部分**而不是抛错 —— 页面可能在加载中，
    调用方据 ``sections`` 是否为空判断，比吞掉异常再看空列表更清楚。
    """
    paper = Paper()
    m = re.search(r"<title>([^<]*)</title>", html or "", re.I)
    if m:
        paper.title = html_mod.unescape(m.group(1)).strip()

    for idx, raw in enumerate(_sections(html), 1):
        section = Section(
            index=idx,
            part1=_attr(raw, "part1"),
            part2=_attr(raw, "part2"),
            section_type=_attr(raw, "sectionType"),
        )
        # 一个 section 里可能有多个 itest-hear-reslist（隐藏的占位是 []，真有音频的才带
        # URL），所以要**遍历全部**、只取带 http 的那些，不能只取第一个。
        for found in re.finditer(r'itest-hear-reslist[^>]*>\s*(\[[^\]]*\])', raw):
            try:
                items = json.loads(found.group(1))
            except json.JSONDecodeError:
                continue
            for item in items:
                if isinstance(item, str) and item.startswith("http") and item not in section.audios:
                    section.audios.append(item)
        section.questions = _questions(raw)
        paper.sections.append(section)

    # 标题里没有卷名时，用第一个 section 的 part1 兜底
    if not paper.title:
        for s in paper.sections:
            if s.part1:
                paper.title = s.part1
                break
    return paper


def _questions(raw: str) -> list[Question]:
    out: list[Question] = []
    blocks = re.split(r'(?=<div class="itest-ques)', raw)
    number = 0
    for block in blocks:
        if not block.startswith('<div class="itest-ques'):
            continue
        qid = _attr(block, "qid")
        if not qid:
            continue

        # 填空：逐个 tag 解析（属性顺序不固定，不能靠一条大正则）
        blanks: list[Blank] = []
        for tag in re.findall(r'<input[^>]*class="blankinput"[^>]*>', block):
            sub = re.search(r'qsubindex="(\d+)"', tag)
            blanks.append(Blank(index=int(sub.group(1)) if sub else 0,
                                placeholder=_attr(tag, "placeholder")))

        choices: list[Choice] = []
        for m in re.finditer(r"<input[^>]*type=\"radio\"[^>]*>(.*?)</label>", block, re.S):
            tag, label = m.group(0), m.group(1)
            # 先 unescape：`&nbsp;` 已变成 `\xa0`，不能再拿字面量 `&nbsp;` 去匹配
            letter = re.search(r"^\s*([A-Z])[\s.、]", html_mod.unescape(label))
            sub = re.search(r'qsubindex="(\d+)"', tag)
            choices.append(Choice(
                letter=letter.group(1) if letter else "",
                text=_text(label),
                value=_attr(tag, "value"),
                sub=int(sub.group(1)) if sub else 0,
            ))

        if blanks:
            kind = KIND_BLANK
        elif choices:
            kind = KIND_RADIO
        elif re.search(r"<textarea", block):
            kind = KIND_WRITE
        else:
            continue

        number += 1
        if kind == KIND_BLANK:
            text, sentences = _blank_sentences(block)
        else:
            text = _text(block)
            sentences = []
        out.append(Question(qid=qid, kind=kind, number=number, text=text,
                            blanks=blanks, choices=choices, sentences=sentences))
    return out


#: 原句里被考查的词用 ``<u>`` 标出来
_UNDERLINE = re.compile(r"<u>(.*?)</u>", re.S | re.I)


def _blank_sentences(block: str) -> tuple[str, list[str]]:
    """把「选项串 + 空 + 原句」拆开。

    返回 ``(选项说明, 各空对应的原句列表)``。原句里被考查的词包在 ``<u>`` 里，
    这正是推导答案的依据（实测 B2U5/B2U6 都有）。解析时把它标成 ``[[词]]``，
    否则把标签一剥，「要填哪个词」这条信息就丢了（AI 作答需要它）。
    """
    body = _UNDERLINE.sub(lambda m: f"[[{_TAG.sub('', m.group(1)).strip()}]]", block)
    body = re.sub(r"<input[^>]*>", "\n\x00\n", body)
    body = _SCRIPT.sub(" ", body)
    pieces = [html_mod.unescape(_TAG.sub(" ", p)).strip()
              for p in body.split("\x00")]
    lines = [re.sub(r"\s+", " ", p) for p in pieces if p.strip()]
    if not lines:
        return "", []
    # 第一段是选项说明（A) .. B) ..），其余是各空的原句
    head, rest = lines[0], lines[1:]
    if len(rest) < 2 and lines:
        rest = lines
        head = ""
    return head, rest


# --------------------------------------------------------------------------
# 生成填表脚本（在页面里执行）
# --------------------------------------------------------------------------

FILL_JS = r"""
(() => {
  const ANS = %s;
  const out = {blank: 0, radio: 0, textarea: 0, missing: []};
  const fire = (el) => {
    el.dispatchEvent(new Event('input',  {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
  };
  for (const [qid, answer] of Object.entries(ANS)) {
    const blanks = [...document.querySelectorAll('input.blankinput')]
        .filter(i => i.getAttribute('qid') === qid);
    if (blanks.length) {
      const letters = String(answer).split(/\s+/);
      blanks.forEach((el, i) => {
        const v = letters[i];
        if (!v) return;
        el.focus(); el.value = v; fire(el); el.blur(); out.blank++;
      });
      continue;
    }
    const areas = [...document.querySelectorAll('textarea')]
        .filter(t => t.getAttribute('qid') === qid);
    if (areas.length) {
      areas.forEach(el => {
        el.focus(); el.value = String(answer); fire(el); el.blur(); out.textarea++;
      });
      continue;
    }
    // ★ 单选按 label 里的字母选：value 与显示字母不一致（被 qoo 打乱过）
    const radios = [...document.querySelectorAll('input[type=radio]')]
        .filter(r => r.getAttribute('qid') === qid);
    if (radios.length) {
      const groups = {};
      radios.forEach(r => { (groups[r.getAttribute('qsubindex')] ||= []).push(r); });
      const letters = String(answer).split(/\s+/);
      Object.keys(groups).sort((a, b) => a - b).forEach((sub, idx) => {
        const want = letters[idx];
        if (!want) return;
        const hit = groups[sub].find(r => {
          const lb = r.closest('label');
          const t = (lb ? lb.textContent : '').trim();
          return new RegExp('^' + want + '\\s*[\\.、]').test(t);
        });
        if (hit) { hit.click(); out.radio++; }
        else { out.missing.push(qid + '#' + sub + '=' + want); }
      });
      continue;
    }
    out.missing.push(qid);
  }
  return JSON.stringify(out);
})()
"""

VERIFY_JS = r"""
(() => {
  const ANS = %s, bad = [];
  for (const [qid, ans] of Object.entries(ANS)) {
    const blanks = [...document.querySelectorAll('input.blankinput')]
        .filter(i => i.getAttribute('qid') === qid);
    if (blanks.length) {
      const want = String(ans).split(/\s+/);
      blanks.forEach((el, i) => {
        if ((el.value || '').trim() !== want[i]) {
          bad.push(qid + '#' + (i + 1) + ' got=' + el.value + ' want=' + want[i]);
        }
      });
      continue;
    }
    const areas = [...document.querySelectorAll('textarea')]
        .filter(t => t.getAttribute('qid') === qid);
    if (areas.length) {
      areas.forEach(el => {
        if ((el.value || '').trim() !== String(ans)) bad.push('textarea ' + qid);
      });
      continue;
    }
    const radios = [...document.querySelectorAll('input[type=radio]')]
        .filter(r => r.getAttribute('qid') === qid);
    if (radios.length) {
      const groups = {};
      radios.forEach(r => { (groups[r.getAttribute('qsubindex')] ||= []).push(r); });
      const want = String(ans).split(/\s+/);
      Object.keys(groups).sort((a, b) => a - b).forEach((sub, idx) => {
        const sel = groups[sub].find(r => r.checked);
        const t = sel ? ((sel.closest('label') || {}).textContent || '').trim() : '(未选)';
        if (!t.startsWith(want[idx])) {
          bad.push(qid + '#' + sub + ' got=' + t.slice(0, 14) + ' want=' + want[idx]);
        }
      });
    }
  }
  return JSON.stringify({bad: bad.slice(0, 20), total: bad.length});
})()
"""


def fill_script(answers: dict[str, str]) -> str:
    """生成「把答案填进界面」的 JS。"""
    return FILL_JS % json.dumps(answers, ensure_ascii=False)


def verify_script(answers: dict[str, str]) -> str:
    """生成「读回已填值并比对」的 JS —— 填完必须核验，不能只看填了多少。"""
    return VERIFY_JS % json.dumps(answers, ensure_ascii=False)
