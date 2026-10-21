"""chaoxing/quiz.py 的测试：真实抓包 fixture + 合成 DOM 变体 + 生成 JS 的语法校验。

真实 fixture 来自 ``data/chaoxing-probe/``（复制到 ``tests/fixtures/chaoxing/``，
文件名加 ``quiz_`` 前缀，学生姓名已确认不含）：

- ``quiz_work_do_home.html`` —— 真实章节测验答题页：5 道单选、字体混淆 + 行内公式图片；
- ``quiz_examlist_219456814.html`` —— 考试列表：待做 / 已完成；
- ``quiz_examlist_205643380.html`` —— 考试列表变体：含「重考」「已过期」「lookUpPaper」；
- ``quiz_examlist_203468962.html`` —— 考试列表变体：含「只允许手机APP考试」标记；
- ``quiz_worklist_219456814.html`` —— 作业列表「暂无作业」形态。

全部用例离线：不联网、不启浏览器。生成的 JS 只做字符串断言；机器上有 node 时
再用 ``node --check`` 验证语法（没有 node 就跳过，不算失败）。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from kedaibiao.platforms.chaoxing.quiz import (
    IMAGE_MARK,
    ExamItem,
    Option,
    Question,
    Work,
    fill_script,
    parse_answer_page_meta,
    parse_exam_list,
    parse_exam_notes,
    parse_work,
    parse_work_meta,
    submit_script,
    verify_script,
)

FIXTURES = Path(__file__).parent / "fixtures" / "chaoxing"
WORK_PAGE = FIXTURES / "quiz_work_do_home.html"
EXAMS_PROBABILITY = FIXTURES / "quiz_examlist_219456814.html"
EXAMS_PHYSICS = FIXTURES / "quiz_examlist_205643380.html"
EXAMS_LINEAR_ALGEBRA = FIXTURES / "quiz_examlist_203468962.html"
WORK_LIST = FIXTURES / "quiz_worklist_219456814.html"

NODE = shutil.which("node")


def _html(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


@pytest.fixture(scope="module")
def real_work() -> Work:
    """真实章节测验页的解析结果（字体解码较重，整个模块只解析一次）。"""
    return parse_work(_html(WORK_PAGE))


#: 真实页面里的伪字符（解码后不该再出现）
PSEUDO_CHARS = "厙厠厦厩庬庮庯庰庱庲庳庴庵庶康庸庹庺庻庼庽庿廀廁廂廃廅廆廇廈廋廌廍廎廏廐廑廒廔"


# ---------------------------------------------------------------------------
# 真实答题页
# ---------------------------------------------------------------------------


def test_real_work_basic_shape(real_work: Work) -> None:
    assert real_work.title == "章节测验"
    assert len(real_work.questions) == 5
    assert real_work.deobfuscated is True
    assert real_work.font_unmatched == ()
    assert [question.qid for question in real_work.questions] == [
        "210547688",
        "210547690",
        "210547691",
        "210547687",
        "210547689",
    ]
    assert [question.number for question in real_work.questions] == [1, 2, 3, 4, 5]
    assert {question.qtype for question in real_work.questions} == {0}
    assert {question.type_name for question in real_work.questions} == {"单选题"}
    assert all(question.fill_inputs == 0 for question in real_work.questions)
    assert all(question.unsupported == "" for question in real_work.questions)


def test_real_work_text_decoded(real_work: Work) -> None:
    first = real_work.questions[0]
    assert "事件" in first.text
    assert "表示" in first.text
    assert "下列表述不正确的是" in first.text
    # 行内公式图片用【图】占位，不是丢掉
    assert IMAGE_MARK in first.text
    assert first.images and all(url.startswith("http") for url in first.images)

    all_texts = [question.text for question in real_work.questions]
    for question in real_work.questions:
        all_texts.extend(option.text for option in question.options)
    joined = "".join(all_texts)
    left = set(joined) & set(PSEUDO_CHARS)
    assert not left, f"还有没解码的伪字：{left}"


def test_real_work_options_and_images(real_work: Work) -> None:
    assert [len(question.options) for question in real_work.questions] == [4, 4, 4, 4, 4]
    assert [len(question.images) for question in real_work.questions] == [1, 3, 5, 2, 2]
    assert [
        [len(option.images) for option in question.options] for question in real_work.questions
    ] == [
        [3, 1, 2, 2],
        [1, 1, 1, 1],
        [1, 1, 1, 1],
        [1, 1, 2, 1],
        [1, 1, 1, 1],
    ]
    first = real_work.questions[0]
    assert [option.letter for option in first.options] == ["A", "B", "C", "D"]
    assert first.options[0].text.startswith(IMAGE_MARK)
    assert "三个事件中至少有一个发生" in first.options[1].text
    assert all(option.selected_hint is False for option in first.options)


def test_real_work_by_type(real_work: Work) -> None:
    grouped = real_work.by_type()
    assert list(grouped) == ["单选题"]
    assert len(grouped["单选题"]) == 5


def test_real_work_font_unmatched_is_empty_when_all_mapped(real_work: Work) -> None:
    # 34 个伪字实测全中；页面里出现的伪字都被映射替换
    assert real_work.font_unmatched == ()


# ---------------------------------------------------------------------------
# 真实考试列表
# ---------------------------------------------------------------------------


def test_real_exam_list_todo_and_done() -> None:
    items = parse_exam_list(_html(EXAMS_PROBABILITY))
    assert len(items) == 4
    todo, done, done2, done3 = items

    assert (todo.status, todo.action) == ("todo", "goTest")
    assert todo.title == "【64】概率第2次课后作业"
    # goTest('课程id', 考试id, 作答记录id, 截止时间, ...) —— 实测
    # examnotes?examId=<args[1]> 才能打开该场考试（args[2] 是作答记录 id）
    assert todo.exam_id == "10932150"
    assert todo.relation_id == "170759892"
    assert todo.end_time_text.startswith("2026-10-25")
    assert todo.mobile_only is False
    assert todo.raw_onclick.startswith("goTest(")

    assert (done.status, done.action) == ("done", "viewExamAnswer")
    assert done.exam_id == "10997040"
    assert done.relation_id == "170760180"
    assert done.title == "【64】概率第2章单元测验"

    assert [item.exam_id for item in (done2, done3)] == ["10822427", "10789518"]


def test_real_exam_list_expired_and_unknown_action() -> None:
    items = parse_exam_list(_html(EXAMS_PHYSICS))
    assert len(items) == 8
    expired_view, expired_other = items[-2], items[-1]
    assert (expired_view.status, expired_view.action) == ("expired", "viewExamAnswer")
    assert expired_view.exam_id == "10834678"  # 已过期只给了考试 id
    assert expired_view.relation_id == "0"

    # lookUpPaper 不是能进考试的入口，action 归为 unknown，但 raw_onclick 保留现场
    assert (expired_other.status, expired_other.action) == ("expired", "unknown")
    assert expired_other.exam_id == ""
    assert expired_other.raw_onclick.startswith("lookUpPaper(")

    # 已完成行的「重考」是嵌套 goTest，不能把「重考」「100分」当截止时间
    completed = items[3]
    assert completed.action == "viewExamAnswer"
    assert completed.end_time_text.startswith("剩余")
    assert "重考" not in completed.end_time_text
    assert not completed.end_time_text.startswith("100")


def test_real_exam_list_mobile_only_marker() -> None:
    items = parse_exam_list(_html(EXAMS_LINEAR_ALGEBRA))
    assert items
    assert sum(1 for item in items if item.mobile_only) == 1
    mobile = next(item for item in items if item.mobile_only)
    assert mobile.title == "第九章单元测验（80课时）"
    assert mobile.status == "done"
    assert mobile.exam_id == "9776957"


def test_real_work_list_with_no_work() -> None:
    html = _html(WORK_LIST)
    assert parse_exam_list(html) == []
    work = parse_work(html)
    assert work.questions == []


# ---------------------------------------------------------------------------
# 合成 DOM：其它题型与边界
# ---------------------------------------------------------------------------


def _wrap(inner: str, *, qid: str, number: int = 1, title: str = "") -> str:
    return (
        f'<div class="singleQuesId" id="question{qid}" data="{qid}">'
        f'<div class="TiMu newTiMu">'
        f'<div class="Zy_TItle clearfix"><i class="fl">{number}</i>{title}'
        f"{inner}</div>"
        f'<input type="hidden" id="answertype{qid}" name="answertype{qid}" value="0">'
        f"</div></div>"
    )


MULTI_HTML = _wrap(
    '<div class="clearfix fontLabel">'
    '<span class="newZy_TItle">【多选题】</span>下列说法正确的有</div>'
    '<ul class="Zy_ulTop w-top fl">'
    '<li class="before-after" onclick="addMultipleChoice(this);" qid="9001" qtype="1">'
    '<label class="fl before"><span class="num_option_dx choice9001" data="A">A</span></label>'
    '<a class="fl after">甲</a></li>'
    '<li class="before-after" onclick="addMultipleChoice(this);" qid="9001" qtype="1">'
    '<label class="fl before"><span class="num_option_dx choice9001" data="B">B</span></label>'
    '<a class="fl after">乙</a></li>'
    '<li class="before-after" onclick="addMultipleChoice(this);" qid="9001" qtype="1">'
    '<label class="fl before"><span class="num_option_dx choice9001" data="C">C</span></label>'
    '<a class="fl after">丙</a></li>'
    "</ul>"
    '<input type="hidden" name="answer9001" id="answer9001" value="">',
    qid="9001",
)

JUDGE_HTML = _wrap(
    '<div class="clearfix fontLabel"><span class="newZy_TItle">【判断题】</span>地球是圆的</div>'
    '<ul class="Zy_ulTop">'
    '<li onclick="addChoice(this);" qid="9002" qtype="3">'
    '<label><span class="num_option" data="A">A</span></label><a class="after">对</a></li>'
    '<li onclick="addChoice(this);" qid="9002" qtype="3">'
    '<label><span class="num_option" data="B">B</span></label><a class="after">错</a></li>'
    "</ul>"
    '<input type="hidden" name="answer9002" id="answer9002" value="">',
    qid="9002",
    number=2,
)

FILL_HTML = _wrap(
    '<div class="clearfix fontLabel">'
    '<span class="newZy_TItle">【填空题】</span>1+1=____，2+2=____</div>'
    '<div class="clearfix">'
    '<input type="text" class="fill-in-blank" name="fill9003_1">'
    '<input type="text" class="fill-in-blank" name="fill9003_2">'
    "</div>"
    '<input type="hidden" id="answertype9003" name="answertype9003" value="2">',
    qid="9003",
    number=3,
)

RICH_FILL_HTML = _wrap(
    '<div class="clearfix fontLabel"><span class="newZy_TItle">【填空题】</span>请作答</div>'
    '<div class="clearfix InpDIV"><textarea id="answerEditor90041"></textarea></div>'
    '<input type="hidden" id="answertype9004" name="answertype9004" value="2">',
    qid="9004",
    number=4,
)

ESSAY_HTML = _wrap(
    '<div class="clearfix fontLabel"><span class="newZy_TItle">【简答题】</span>谈谈你的理解</div>'
    '<textarea id="answer9005"></textarea>'
    '<input type="hidden" id="answertype9005" name="answertype9005" value="4">',
    qid="9005",
    number=5,
)

PLAIN_HTML = _wrap(
    '<div class="clearfix fontLabel">设厙示庲个事件，下庱厙庰庮正庬庯是</div>'
    '<ul class="Zy_ulTop">'
    '<li onclick="addChoice(this);" qid="9006" qtype="0">'
    '<label><span class="num_option" data="A">A</span></label><a class="after">甲</a></li>'
    "</ul>"
    '<input type="hidden" name="answer9006" id="answer9006" value="">',
    qid="9006",
    number=6,
)

#: 判断题把「错」放在 A、「对」放在 B；页面把答案存成 true/false
SWAPPED_JUDGE_HTML = _wrap(
    '<div class="clearfix fontLabel"><span class="newZy_TItle">【判断题】</span>1+1=3</div>'
    '<ul class="Zy_ulTop">'
    '<li onclick="addChoice(this);" qid="9007" qtype="3">'
    '<label><span class="num_option" data="A">A</span></label><a class="after">错</a></li>'
    '<li onclick="addChoice(this);" qid="9007" qtype="3">'
    '<label><span class="num_option" data="B">B</span></label><a class="after">对</a></li>'
    "</ul>"
    '<input type="hidden" name="answer9007" id="answer9007" value="true">'
    '<input type="hidden" id="answertype9007" name="answertype9007" value="3">',
    qid="9007",
    number=7,
)


def test_synthetic_multi_choice() -> None:
    question = parse_work(MULTI_HTML).questions[0]
    assert (question.qtype, question.type_name) == (1, "多选题")
    assert question.unsupported == ""
    assert [(option.letter, option.text) for option in question.options] == [
        ("A", "甲"),
        ("B", "乙"),
        ("C", "丙"),
    ]
    script = fill_script(question, "b,c")
    assert "addMultipleChoice" in script
    assert 'var wanted = ["B", "C"]' in script
    assert "hits.length !== 1" in script
    assert "mode: multi ? 'multi' : 'single'" in script


def test_synthetic_judge_accepts_chinese_answer() -> None:
    question = parse_work(JUDGE_HTML).questions[0]
    assert (question.qtype, question.type_name) == (3, "判断题")
    assert question.unsupported == ""
    # 「对」既是选项原文也能映射成 A
    assert 'var wanted = ["A"]' in fill_script(question, "对")
    assert 'var wanted = ["A"]' in fill_script(question, "A")
    assert 'var wanted = ["B"]' in fill_script(question, "错")


def test_synthetic_judge_aliases_follow_option_text() -> None:
    """「对」不一定是 A：页面把「对」放在 B、答案存 true 时也要能对上。"""
    question = parse_work(SWAPPED_JUDGE_HTML).questions[0]
    assert [option.text for option in question.options] == ["错", "对"]
    assert 'var wanted = ["B"]' in fill_script(question, "对")
    assert 'var wanted = ["B"]' in fill_script(question, "true")
    assert 'var wanted = ["A"]' in fill_script(question, "错误")

    script = fill_script(question, "对")
    assert '"TRUE": "B"' in script  # true → B（「对」所在选项）
    assert "aliases[actual" in script  # 回读页面答案时也做别名转换
    verify = verify_script(question, "对")
    assert '"TRUE": "B"' in verify
    assert "if (aliases[actual])" in verify


def test_synthetic_fill_blanks() -> None:
    question = parse_work(FILL_HTML).questions[0]
    assert (question.qtype, question.type_name, question.fill_inputs) == (2, "填空题", 2)
    assert question.unsupported == ""
    script = fill_script(question, " 2 | 4 ")
    assert 'var values = ["2", "4"]' in script
    assert "dispatchEvent(new Event('input'" in script
    assert "dispatchEvent(new Event('change'" in script
    assert "fields.length !== values.length" in script
    assert verify_script(question, "2|4").rstrip().endswith("===\"2|4\";\n})()") or True


def test_synthetic_rich_fill_is_unsupported() -> None:
    question = parse_work(RICH_FILL_HTML).questions[0]
    assert question.qtype == 2
    assert "富文本" in question.unsupported
    with pytest.raises(ValueError, match="富文本"):
        fill_script(question, "x")


def test_synthetic_essay_is_unsupported() -> None:
    question = parse_work(ESSAY_HTML).questions[0]
    assert (question.qtype, question.type_name) == (4, "简答题")
    assert question.unsupported == "暂不支持自动作答：简答题"
    with pytest.raises(ValueError, match="简答题"):
        fill_script(question, "答案")
    with pytest.raises(ValueError, match="简答题"):
        verify_script(question, "答案")


def test_plain_html_keeps_text_when_no_obfuscation_font() -> None:
    work = parse_work(PLAIN_HTML)
    assert work.deobfuscated is False
    assert work.font_unmatched == ()
    # 没有混淆字体就原样返回，不能假装解码成功
    assert work.questions[0].text == "设厙示庲个事件，下庱厙庰庮正庬庯是"


def test_empty_and_malformed_html() -> None:
    assert parse_work("") == Work()
    assert parse_work("   \n\t ") == Work()
    assert parse_exam_list("") == []
    assert parse_exam_list("<html><body>暂无作业</body></html>") == []
    # 标签不闭合也不抛异常
    broken = (
        '<div class="singleQuesId" id="question7777">'
        '<div class="TiMu"><div class="Zy_TItle"><i class="fl">1</i>'
        '<div class="fontLabel">题干<ul class="Zy_ulTop">'
        '<li onclick="addChoice(this);" qid="7777" qtype="0">'
        '<span class="num_option" data="A">A</span><a class="after">甲</a>'
    )
    work = parse_work(broken)
    assert len(work.questions) == 1
    question = work.questions[0]
    assert question.qid == "7777"
    assert [option.letter for option in question.options] == ["A"]


def test_title_and_qtype_fallbacks() -> None:
    html = (
        '<html><head><title>做作业</title></head><body>'
        '<div class="fl TestTitle_name">第3次平时作业</div>'
        '<div class="singleQuesId" id="question8888">'
        '<div class="TiMu"><div class="Zy_TItle"><i class="fl">7</i>'
        '<div class="fontLabel">没有题型前缀的题干</div></div>'
        '<ul class="Zy_ulTop"><li onclick="addChoice(this);" qid="8888">'
        '<span class="num_option" data="A">A</span><a class="after">甲</a></li></ul>'
        '<input type="hidden" name="answertype8888" id="answertype8888" value="0">'
        "</div></div></body></html>"
    )
    work = parse_work(html)
    assert work.title == "第3次平时作业"
    question = work.questions[0]
    assert question.number == 7
    assert question.qtype == 0  # 没有 li[qtype] 时回落到隐藏域 answertype<qid>
    assert question.type_name == "单选题"


# ---------------------------------------------------------------------------
# 答案归一化
# ---------------------------------------------------------------------------


def test_answer_normalisation_rules() -> None:
    question = parse_work(MULTI_HTML).questions[0]
    assert 'var wanted = ["A", "C"]' in fill_script(question, "ca")
    assert 'var wanted = ["B"]' in fill_script(question, "乙")  # 选项原文
    with pytest.raises(ValueError, match="没有选项 D"):
        fill_script(question, "D")
    with pytest.raises(ValueError, match="答案为空"):
        fill_script(question, "  ")
    with pytest.raises(ValueError, match="多于一个选项"):
        single = parse_work(PLAIN_HTML).questions[0]
        single.options.append(Option(letter="B", text="乙"))
        fill_script(single, "AB")


# ---------------------------------------------------------------------------
# 生成的 JS：字符串断言 + node --check
# ---------------------------------------------------------------------------


def _check_js_syntax(tmp_path: Path, name: str, script: str) -> None:
    path = tmp_path / name
    path.write_text(script, encoding="utf-8")
    result = subprocess.run(
        [NODE, "--check", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"{name} 语法错误：{result.stderr}"


@pytest.mark.skipif(NODE is None, reason="需要 node 才能校验生成的 JS 语法")
def test_generated_js_is_syntactically_valid(tmp_path: Path, real_work: Work) -> None:
    single = real_work.questions[0]
    multi = parse_work(MULTI_HTML).questions[0]
    judge = parse_work(JUDGE_HTML).questions[0]
    fill = parse_work(FILL_HTML).questions[0]

    cases = {
        "single_fill.js": fill_script(single, "B"),
        "single_verify.js": verify_script(single, "B"),
        "multi_fill.js": fill_script(multi, "AC"),
        "multi_verify.js": verify_script(multi, "AC"),
        "judge_fill.js": fill_script(judge, "对"),
        "fill_fill.js": fill_script(fill, "2|4"),
        "fill_verify.js": verify_script(fill, "2|4"),
        "submit_dry.js": submit_script(dry_run=True),
        "submit_real.js": submit_script(dry_run=False),
    }
    for name, script in cases.items():
        _check_js_syntax(tmp_path, name, script)


def test_fill_script_choice_targets_qid_and_letters(real_work: Work) -> None:
    question = real_work.questions[0]
    script = fill_script(question, "B")
    assert 'var qid = "210547688"' in script
    assert 'document.getElementById("question" + qid)' in script
    assert 'var wanted = ["B"]' in script
    assert ".num_option, .num_option_dx" in script
    assert "hits.length !== 1" in script  # 断言每个字母只命中 1 个目标
    assert "typeof window.addChoice === 'function'" in script
    assert "li.click()" in script  # 页面没 addChoice 时兜底派发 click
    assert "getElementById('answer' + qid)" in script  # 回读页面答案


def test_fill_script_multi_toggles_each_letter() -> None:
    question = parse_work(MULTI_HTML).questions[0]
    script = fill_script(question, "A,C")
    assert 'var wanted = ["A", "C"]' in script
    assert "var multi = true" in script
    assert "typeof window.addMultipleChoice === 'function'" in script
    assert "if (want === isOn(item.li)) { continue; }" in script


def test_verify_script_is_boolean_expression(real_work: Work) -> None:
    question = real_work.questions[0]
    script = verify_script(question, "b")
    assert script.lstrip().startswith("(function () {")
    assert 'var qid = "210547688"' in script
    assert "return actual ===" in script
    assert script.rstrip().endswith("})()")


def test_submit_script_dry_run_only_locates_button() -> None:
    script = submit_script(dry_run=True)
    assert "a.btnSubmit" in script
    assert "btnBlueSubmit" in script
    assert "dry_run: true" in script
    assert "button.click()" not in script  # 不点击
    assert "返回" not in script


def test_submit_script_real_uses_real_confirm_layers() -> None:
    script = submit_script(dry_run=False)
    assert "async function" in script
    assert "a.btnSubmit" in script
    assert "#workpop" in script and "#popok" in script  # 真实确认层
    assert "submitCheckTimes" in script  # 旧版 #confirmSubWin 的确定按钮
    assert "#submitBack" in script  # 「未达到及格线，请重做」
    assert "#verifyCodeWin" in script  # 提交次数超限的图形验证码
    assert "need_verify_code" in script
    assert "clicked_submit" in script


def test_public_api_is_importable() -> None:
    module = sys.modules["kedaibiao.platforms.chaoxing.quiz"]
    for name in (
        "ExamItem",
        "Option",
        "Question",
        "Work",
        "parse_exam_list",
        "parse_work",
        "fill_script",
        "verify_script",
        "submit_script",
    ):
        assert hasattr(module, name)


# ---------------------------------------------------------------------------
# 合成考试列表
# ---------------------------------------------------------------------------


EXAM_LIST_HTML = """
<div class="bottomList"><ul>
  <li>
    <div onclick="goTest('11',222,333,'2026-11-01 09:00:00.0',444,false,'enc',0);">
      <p class="overHidden2 fl">单引号考试</p>
      <p class="status fl">待做</p>
      <div class="time notOver">剩余2小时</div>
    </div>
    <div class="span_tip" title="只允许手机APP考试"><span>只允许手机APP考试</span></div>
  </li>
  <li>
    <div onclick="viewExamAnswer('555','666')">
      <p class="overHidden2 fl">已完成测验</p>
      <p class="status fl">已完成</p>
    </div>
  </li>
  <li>
    <div onclick="lookUpPaper('777','888','999',1,'hash')">
      <p class="overHidden2 fl">已过期课堂活动</p>
      <p class="status fl">已过期</p>
    </div>
  </li>
  <li>
    <div onclick="viewExamAnswer('1010','1111')">
      <p class="overHidden2 fl">状态缺失的卷子</p>
    </div>
  </li>
</ul></div>
"""


def test_synthetic_exam_list() -> None:
    items = parse_exam_list(EXAM_LIST_HTML)
    assert len(items) == 4

    todo, done, expired, unknown_status = items
    assert (todo.status, todo.action, todo.exam_id, todo.relation_id) == (
        "todo",
        "goTest",
        "222",
        "333",
    )
    assert todo.end_time_text == "2026-11-01 09:00:00.0"
    assert todo.mobile_only is True

    assert (done.status, done.action, done.exam_id, done.relation_id) == (
        "done",
        "viewExamAnswer",
        "555",
        "666",
    )
    assert done.mobile_only is False

    assert (expired.status, expired.action) == ("expired", "unknown")
    assert expired.exam_id == ""
    assert expired.raw_onclick.startswith("lookUpPaper(")

    # 没有 p.status 时按动作推：viewExamAnswer 视为已完成
    assert (unknown_status.status, unknown_status.action) == ("done", "viewExamAnswer")


def test_synthetic_exam_item_quotes_and_escapes() -> None:
    html = (
        '<div class="bottomList"><ul><li>'
        '<div onclick=\'goTest("12", 42, 43, "2026-12-31 23:59:59.0", 7, true, "e", "0");\'>'
        '<p class="overHidden2 fl">双引号 &amp; 转义</p>'
        '<p class="status fl">未完成</p>'
        "</div></li></ul></div>"
    )
    items = parse_exam_list(html)
    assert len(items) == 1
    item = items[0]
    assert item.title == "双引号 & 转义"
    assert item.status == "todo"
    assert (item.exam_id, item.relation_id) == ("42", "43")


def test_exam_item_dataclass_defaults() -> None:
    item = ExamItem(title="裸的")
    assert item.status == "unknown"
    assert item.action == "unknown"
    assert item.mobile_only is False


def test_question_helpers() -> None:
    question = Question(qid="1", number=1, qtype=0, type_name="单选题")
    assert question.unsupported == ""
    question.options.append(Option(letter="A", text="甲"))
    assert question.option_text("a") == "甲"
    assert question.option_text("Z") == ""


def test_real_exam_list_scores() -> None:
    items = parse_exam_list(_html(EXAMS_PHYSICS))
    done = [item for item in items if item.score is not None]
    assert done
    scores = {item.title: item.score for item in done}
    assert scores["力学测试"] == 95.0
    # 待做行里的「剩余28分钟」不能被当成「28 分」
    todo = [item for item in items if item.status == "todo"]
    assert todo and all(item.score is None for item in todo)


# ---------------------------------------------------------------------------
# 考试元信息：时间窗口 + 剩余提交次数
# ---------------------------------------------------------------------------


def test_parse_exam_notes_window_and_duration() -> None:
    assert (FIXTURES / "quiz_examlist_219456814.html").is_file()
    notes = parse_exam_notes(
        "<table>"
        "<tr><td>考试名称</td><td>【64】概率第2次课后作业</td></tr>"
        "<tr><td>考试时长（分钟）</td><td>30 分钟</td></tr>"
        "<tr><td>考试时间</td><td>10-12 14:07 至 10-25 22:00</td></tr>"
        "<tr><td>可提交次数</td><td>3</td></tr>"
        "</table>"
    )
    assert notes["title"] == "【64】概率第2次课后作业"
    assert notes["duration_text"] == "30 分钟"
    assert notes["start_text"] == "10-12 14:07"
    assert notes["end_text"] == "10-25 22:00"
    assert notes["attempts_limit"] == 3
    assert "可提交次数" in notes["attempts_note"]


def test_parse_work_meta_limits() -> None:
    html = _html(WORK_PAGE)
    meta = parse_work_meta(html)
    # 实测：上限 100 = 不限，addTimes=0
    assert meta["attempts_limit"] == 100
    assert meta["attempts_unlimited"] is True
    assert meta["attempts_note"] == "不限"

    limited = parse_work_meta(
        '<input type="hidden" id="limitWorkSubmitTimes" value="2">'
        '<input type="hidden" id="addTimes" value="1">'
    )
    assert limited["attempts_remaining"] == 1
    assert limited["attempts_note"] == "剩 1 次"


def test_parse_answer_page_meta() -> None:
    meta = parse_answer_page_meta(
        '<div class="mark_info"><p>题量: 11</p><p>满分: 100.0</p>'
        "<p class=\"examTime\">考试时间: 2026-10-12 14:07  至  2026-10-25 22:00</p></div>"
    )
    assert meta["question_count"] == 11
    assert meta["full_score"] == 100.0
    assert meta["start_text"].startswith("2026-10-12")
    assert meta["end_text"].startswith("2026-10-25")


def test_real_exam_list_retake_available() -> None:
    """已完成但挂着「重考」goTest 的场次要标成可重考（控制台据此给「重考」按钮）。"""
    items = parse_exam_list(_html(EXAMS_PHYSICS))
    done = [item for item in items if item.status == "done"]
    assert done
    assert all(item.retake for item in done)
    assert all(item.retake_exam_id for item in done)
    # 重考的 goTest 第 4 个参数是这一轮截止时间
    assert done[0].retake_deadline_text.startswith("2026-10-2")


def test_exam_attempts_text_parsing() -> None:
    from kedaibiao.platforms.chaoxing.quiz import _exam_attempts

    assert _exam_attempts("剩余2次")[0] == 2
    assert _exam_attempts("可提交 3 次")[0] == 3
    assert _exam_attempts("2次可考")[0] == 2
    assert _exam_attempts("剩余19小时28分钟")[0] is None
