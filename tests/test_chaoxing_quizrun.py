"""学习通刷题引擎（quizrun）的纯逻辑单测：不联网、不启浏览器。"""

from __future__ import annotations

import asyncio

from kedaibiao.core.models import Answer, AnswerSource
from kedaibiao.platforms.chaoxing import quiz as quiz_mod
from kedaibiao.platforms.chaoxing import quizrun


def _single_question() -> quiz_mod.Question:
    return quiz_mod.Question(
        qid="210547688",
        number=1,
        qtype=0,
        type_name="单选题",
        text="设表示三个事件，下列表述不正确的是",
        options=[
            quiz_mod.Option("A", "表示A，B互斥"),
            quiz_mod.Option("B", "表示A，B至少有一个发生"),
            quiz_mod.Option("C", "表示A发生"),
            quiz_mod.Option("D", "表示A有且仅有一个"),
        ],
    )


def test_answer_to_letters_single() -> None:
    question = _single_question()
    answer = Answer(question=question, choices=[1], source=AnswerSource.LLM, confidence=0.8)  # type: ignore[arg-type]
    letters, text, confidence, source = quizrun.ChaoxingQuiz._answer_to_letters(question, answer)
    assert letters == "B"
    assert text == ""
    assert confidence == 0.8
    assert source == "llm"


def test_answer_to_letters_multiple() -> None:
    question = quiz_mod.Question(
        qid="2", number=2, qtype=1, type_name="多选题",
        options=[
            quiz_mod.Option("A", "甲"),
            quiz_mod.Option("B", "乙"),
            quiz_mod.Option("C", "丙"),
        ],
    )
    answer = Answer(question=question, choices=[0, 2], confidence=0.6)  # type: ignore[arg-type]
    letters, _, _, _ = quizrun.ChaoxingQuiz._answer_to_letters(question, answer)
    assert letters == "AC"


def test_answer_to_letters_fill_uses_text() -> None:
    question = quiz_mod.Question(qid="3", number=3, qtype=2, type_name="填空题")
    answer = Answer(question=question, text="0.5", confidence=0.7)  # type: ignore[arg-type]
    letters, text, _, _ = quizrun.ChaoxingQuiz._answer_to_letters(question, answer)
    assert letters == ""
    assert text == "0.5"


def test_answer_to_letters_empty_answer() -> None:
    question = _single_question()
    assert quizrun.ChaoxingQuiz._answer_to_letters(question, None) == ("", "", 0.0, "empty")


class _FakeAnswerer:
    def __init__(self) -> None:
        self.seen: list[object] = []

    async def answer(self, questions):
        self.seen = list(questions)
        return [
            Answer(question=q, choices=[0], source=AnswerSource.LLM, confidence=0.9)
            for q in questions
        ]


def test_plan_maps_answers_and_marks_unsupported() -> None:
    work = quiz_mod.Work(
        title="章节测验",
        questions=[
            _single_question(),
            quiz_mod.Question(
                qid="9", number=2, qtype=-1, type_name="未知题型",
                unsupported="暂不支持自动作答",
            ),
        ],
        deobfuscated=True,
    )
    answerer = _FakeAnswerer()
    runner = quizrun.ChaoxingQuiz(
        None,  # type: ignore[arg-type]
        course_id="c", clazz_id="z", answerer=answerer,
    )
    items = asyncio.run(runner.plan(work))
    assert len(items) == 2
    assert items[0].answer == "A"
    assert items[0].confidence == 0.9
    assert items[1].unsupported.startswith("暂不支持")
    # 未支持的题不应进入作答器
    assert len(answerer.seen) == 1


def test_quiz_report_summary() -> None:
    report = quizrun.QuizReport(title="t")
    report.items = [
        quizrun.PlanItem(1, "q1", "单选题", "题", ["A"], 0, "A", 0.8, "llm", verified=True),
        quizrun.PlanItem(2, "q2", "单选题", "题", ["A"], 0, "", 0.0, "empty"),
    ]
    report.finished_at = report.started_at + 3
    summary = report.summary()
    assert "共 2 题" in summary
    assert "作答 1" in summary
    assert "核验通过 1" in summary
    assert "未提交（干跑）" in summary


def test_parse_score_text_real_exam_view() -> None:
    # 实测已完成考试页原文：交卷成功 / 本次成绩85.0分 / 最终成绩85.0分
    text = "交卷成功\n当前时间2026-10-21 16:47:54\n本次成绩85.0分\n最终成绩85.0分"
    score, snippet = quizrun.parse_score_text(text)
    assert score == 85.0
    assert "85" in snippet


def test_parse_score_text_variants() -> None:
    assert quizrun.parse_score_text("得分：72")[0] == 72.0
    assert quizrun.parse_score_text("本次成绩：90 分")[0] == 90.0
    assert quizrun.parse_score_text("最终成绩 100")[0] == 100.0
    # 「满分 100.0」不是得分，不能误判
    assert quizrun.parse_score_text("题量: 5 满分: 100.0")[0] is None
    assert quizrun.parse_score_text("")[0] is None
