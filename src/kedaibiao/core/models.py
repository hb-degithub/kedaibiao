"""领域模型。

设计原则：核心层只认这里的结构，任何平台私有字段都必须在适配器层被
翻译成这些模型。这样新增平台 = 新增一个适配器，核心调度、题库、通知
一行都不用改。
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

# --------------------------------------------------------------------------
# 枚举
# --------------------------------------------------------------------------


class TaskKind(str, Enum):
    """一个「活」的种类。平台侧叫法五花八门，统一收敛到这四类。"""

    COURSE = "course"  # 视频 / 音频 / 课件学习
    HOMEWORK = "homework"  # 课后作业
    EXAM = "exam"  # 考试 / 测验
    PRACTICE = "practice"  # 刷题 / 题库练习


class QuestionType(str, Enum):
    SINGLE = "single"  # 单选
    MULTIPLE = "multiple"  # 多选
    JUDGE = "judge"  # 判断
    FILL = "fill"  # 填空
    ESSAY = "essay"  # 简答 / 论述
    UNKNOWN = "unknown"


class RunStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


class AnswerSource(str, Enum):
    """答案从哪来 —— 必须可追溯，否则无法评估准确率。"""

    BANK_HIT = "bank_hit"  # 题库精确命中
    BANK_FUZZY = "bank_fuzzy"  # 题库模糊命中
    LLM = "llm"  # 模型推理
    HEURISTIC = "heuristic"  # 规则/启发式（如选项长度、关键词）
    MANUAL = "manual"  # 人工填写
    UNKNOWN = "unknown"


# --------------------------------------------------------------------------
# 归一化
# --------------------------------------------------------------------------

_WS_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[，。！？；：、（）【】《》“”‘’·,.!?;:()\[\]<>\"']")


def normalize_question(text: str) -> str:
    """题干归一化：全角转半角、去空白、去标点、统一小写。

    归一化后的字符串是题库的主键，所以这段逻辑一旦上线就不能随便改，
    改了等于全库失效（需要写迁移）。
    """
    if not text:
        return ""
    s = unicodedata.normalize("NFKC", text)
    s = _PUNCT_RE.sub("", s)
    s = _WS_RE.sub("", s)
    return s.strip().lower()


def question_fingerprint(stem: str, options: list[str] | None = None) -> str:
    """题目指纹：题干 + 选项一起哈希，抗选项顺序抖动。"""
    parts = [normalize_question(stem)]
    if options:
        parts.extend(sorted(normalize_question(o) for o in options))
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:32]


# --------------------------------------------------------------------------
# 模型
# --------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class Credentials:
    """登录凭据。只在内存与加密存储中出现，禁止写日志。"""

    username: str
    password: str = ""
    extra: dict[str, str] = field(default_factory=dict)

    def __repr__(self) -> str:  # 防止密码进日志/异常栈
        return f"Credentials(username={self.username!r}, password=***)"


@dataclass(slots=True)
class Question:
    """一道题。``options`` 只对选择题有意义。"""

    stem: str
    type: QuestionType = QuestionType.UNKNOWN
    options: list[str] = field(default_factory=list)
    # 平台侧题目 ID，回填答案时要用
    remote_id: str | None = None
    score: float | None = None
    # 平台给出的原始 JSON，排查问题时很有用
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def fingerprint(self) -> str:
        return question_fingerprint(self.stem, self.options)


@dataclass(slots=True)
class Answer:
    question: Question
    # 统一用选项序号列表表示选择；填空题放文本
    choices: list[int] = field(default_factory=list)
    text: str = ""
    source: AnswerSource = AnswerSource.UNKNOWN
    confidence: float = 0.0

    def is_empty(self) -> bool:
        return not self.choices and not self.text.strip()


@dataclass(slots=True)
class Assignment:
    """一个可执行任务单元（作业 / 考试 / 章节视频 / 练习）。"""

    remote_id: str
    title: str
    kind: TaskKind = TaskKind.HOMEWORK
    course_name: str = ""
    deadline: datetime | None = None
    # 0.0 ~ 1.0
    progress: float = 0.0
    finished: bool = False
    questions: list[Question] = field(default_factory=list)
    url: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_overdue(self) -> bool:
        return self.deadline is not None and self.deadline < datetime.now()


@dataclass(slots=True)
class Course:
    remote_id: str
    name: str
    teacher: str = ""
    term: str = ""
    assignments: list[Assignment] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class SubmissionResult:
    assignment_id: str
    ok: bool
    score: float | None = None
    message: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class RunRecord:
    """一次任务执行的留痕，落库用。"""

    platform: str
    assignment_id: str
    status: RunStatus = RunStatus.PENDING
    started_at: datetime | None = None
    finished_at: datetime | None = None
    attempts: int = 0
    error: str = ""
    detail: dict[str, Any] = field(default_factory=dict)
