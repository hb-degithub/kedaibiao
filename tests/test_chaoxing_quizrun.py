"""学习通刷题引擎（quizrun）的纯逻辑单测：不联网、不启浏览器。"""

from __future__ import annotations

import asyncio

from kedaibiao.core.models import Answer, AnswerSource, QuestionType
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


class _FakeFrame:
    """假的题目帧：只提供截图能力。"""

    def __init__(self, png: bytes = b"\x89PNG\r\n\x1a\nfake") -> None:
        self.png = png
        self.calls: list[str] = []

    async def screenshot_bytes(self, selector: str = "", **_kw: object) -> bytes:
        self.calls.append(selector)
        return self.png


def _runner(
    answerer=None, *, use_ocr: bool | None = None, captcha_mode: str | None = None
) -> quizrun.ChaoxingQuiz:
    return quizrun.ChaoxingQuiz(
        None,  # type: ignore[arg-type]
        course_id="c",
        clazz_id="z",
        answerer=answerer,
        use_ocr=use_ocr,
        captcha_mode=captcha_mode,
    )


def test_ocr_core_question_turns_screenshot_into_image_question() -> None:
    """OCR 方案：题面来自「题目截图的 data URL」，不是 DOM 文本。"""
    runner = _runner(use_ocr=True)
    frame = _FakeFrame()
    core = asyncio.run(
        runner._ocr_core_question(
            frame,
            "#question210547688",
            type_name="单选题",
            number=1,
            letters=["A", "B"],
            option_texts=["甲", "乙"],
        )
    )
    assert core is not None
    assert frame.calls == ["#question210547688"]
    assert core.raw["images"][0].startswith("data:image/png;base64,")
    assert core.options == ["A. 甲", "B. 乙"]
    assert core.type is QuestionType.SINGLE
    assert "截图" in core.stem


def test_ocr_supported_requires_image_capable_answerer() -> None:
    class _TextOnly:
        async def answer(self, questions):  # pragma: no cover - 不会被调用
            return []

    assert _runner(_TextOnly(), use_ocr=True)._ocr_supported() is False
    assert _runner(_TextOnly(), use_ocr=False)._ocr_supported() is False

    class _Vision(_TextOnly):
        supports_images = True

    assert _runner(_Vision(), use_ocr=True)._ocr_supported() is True


def test_use_ocr_defaults_on_and_can_be_disabled(monkeypatch) -> None:
    monkeypatch.delenv("KEDAIBIAO_CHAXING_OCR", raising=False)
    assert _runner().use_ocr is True
    monkeypatch.setenv("KEDAIBIAO_CHAXING_OCR", "0")
    assert _runner().use_ocr is False


def test_chain_reports_image_capability() -> None:
    """链里有视觉答题器时，整条链必须声明 supports_images（否则 OCR 白截）。"""
    from kedaibiao.tasks.answerer import ChainAnswerer, RuleAnswerer

    class _Vision(RuleAnswerer):
        supports_images = True

    assert ChainAnswerer([_Vision()], min_confidence=0.0).supports_images is True
    assert ChainAnswerer([RuleAnswerer()], min_confidence=0.0).supports_images is False


def test_ocr_plan_streams_items_one_by_one() -> None:
    """OCR 模式下一题一答、逐条推送（界面能看着题目信息出现）。"""
    from kedaibiao.platforms.chaoxing import quiz as quiz_mod

    class _Frame(_FakeFrame):
        pass

    class _Answerer:
        supports_images = True

        async def answer(self, questions):
            from kedaibiao.core.models import Answer, AnswerSource

            return [
                Answer(question=q, choices=[0], source=AnswerSource.LLM, confidence=0.9)
                for q in questions
            ]

    work = quiz_mod.Work(
        questions=[
            quiz_mod.Question(
                qid="1", number=1, qtype=0, type_name="单选题", text="题1",
                options=[quiz_mod.Option("A", "甲"), quiz_mod.Option("B", "乙")],
            ),
            quiz_mod.Question(
                qid="2", number=2, qtype=0, type_name="单选题", text="题2",
                options=[quiz_mod.Option("A", "甲"), quiz_mod.Option("B", "乙")],
            ),
        ]
    )
    snaps: list[int] = []
    runner = _runner(_Answerer(), use_ocr=True)
    items = asyncio.run(
        runner.plan(work, frame=_Frame(), on_items=lambda rows: snaps.append(len(rows)))
    )
    assert [item.answer for item in items] == ["A", "A"]
    assert snaps == [1, 2]  # 一题推一次，不是最后一次性推


def _item(number: int, answer: str = "A", confidence: float = 0.8, unsupported: str = ""):
    return quizrun.PlanItem(
        number=number,
        qid=str(number),
        type_name="单选题",
        text="题",
        options=["A", "B"],
        images=0,
        answer=answer,
        confidence=confidence,
        source="llm",
        unsupported=unsupported,
    )


def test_submit_block_reason_small_quiz() -> None:
    """2 题小测不自动交卷（实测：2 题各 50 分，错一题就是 50 分）。"""
    reason = quizrun.submit_block_reason([_item(1), _item(2)])
    assert "2 题" in reason and "强制交卷" in reason


def test_submit_block_reason_low_confidence() -> None:
    items = [_item(1), _item(2), _item(3), _item(4, confidence=0.3)]
    reason = quizrun.submit_block_reason(items)
    assert "置信度" in reason and "第 4 题" in reason


def test_submit_block_reason_allows_normal_quiz() -> None:
    items = [_item(index) for index in range(1, 6)]
    assert quizrun.submit_block_reason(items) == ""


def test_submit_block_reason_blocks_blank_submission() -> None:
    items = [_item(1, answer=""), _item(2, answer=""), _item(3, answer=""), _item(4, answer="")]
    assert "白卷" in quizrun.submit_block_reason(items)


def test_submit_block_reason_force_bypasses() -> None:
    assert quizrun.submit_block_reason([_item(1), _item(2)], force=True) == ""


def test_captcha_mode_selection(monkeypatch) -> None:
    """滑块策略可选：auto（默认）/ human；也能用环境变量设默认值。"""
    monkeypatch.delenv("KEDAIBIAO_CHAXING_CAPTCHA", raising=False)
    assert _runner().captcha_mode == "auto"
    assert _runner(captcha_mode="human").captcha_mode == "human"
    assert _runner(captcha_mode="whatever").captcha_mode == "auto"
    monkeypatch.setenv("KEDAIBIAO_CHAXING_CAPTCHA", "human")
    assert _runner().captcha_mode == "human"


def test_time_limit_seconds_parses_platform_message() -> None:
    """「限时提交：考试10分钟内不允许提交考试」→ 等 600 秒再交。"""
    result = {
        "ok": False,
        "response": {"msg": "限时提交：考试10分钟内不允许提交考试", "status": "error"},
    }
    assert quizrun.ChaoxingQuiz._time_limit_seconds(result) == 600
    assert quizrun.ChaoxingQuiz._time_limit_seconds(
        {"ok": False, "response": {"msg": "考试5分钟内不允许提交"}}
    ) == 300
    # 不是限时原因（真失败）→ 0，不要傻等
    assert quizrun.ChaoxingQuiz._time_limit_seconds(
        {"ok": False, "response": {"msg": "请先作答"}}
    ) == 0
    assert quizrun.ChaoxingQuiz._time_limit_seconds({"ok": False, "error": "网络错误"}) == 0
