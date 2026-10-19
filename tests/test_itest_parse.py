"""iTEST 取卷解析测试：不联网，只喂一段手工构造的 C_HTML。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "scripts" / "itest_exam.py"
spec = importlib.util.spec_from_file_location("itest_exam", ROOT)
assert spec and spec.loader
itest_exam = importlib.util.module_from_spec(spec)
sys.modules["itest_exam"] = itest_exam
spec.loader.exec_module(itest_exam)

C_HTML = """
<div class="itest-section" pageNumber="1" sectionKey="sk1" sectionType="sNormal"
     part1="第一次作业" part2="词汇选择" resNeedPlay="-1">
  <div class="itest-ques-set">
    <div class="itest-ques" qid="1001" resNeedPlay="-1">
      <div class="itest-danxuan">
        <div class="option-head">A) 养分； B) 电池；</div>
        <input type="text" qid="1001" qIndex="1" qSubIndex="1" qSubSubIndex="1" class="blankinput">
        <input type="text" qid="1001" qIndex="2" qSubIndex="2" qSubSubIndex="1" class="blankinput">
      </div>
    </div>
    <div class="itest-ques" qid="1002" resNeedPlay="1">
      <div class="itest-hear-reslist">["https://cdn/a.mp3","5"]</div>
      <div class="itest-danxuan">
        <label><input type="radio" name="1002_1_1" value="1" qid="1002" qIndex="1"
               qSubIndex="1" qSubSubIndex="1" qOO="[1,0]">A. first</label>
        <label><input type="radio" name="1002_1_1" value="0" qid="1002" qIndex="1"
               qSubIndex="1" qSubSubIndex="1" qOO="[1,0]">B. second</label>
      </div>
    </div>
  </div>
</div>
<div class="itest-section" sectionKey="sk2" sectionType="sNormal" part1="翻译" part2="翻译7句">
  <div class="itest-ques" qid="1003" resNeedPlay="-1">
    <div class="itest-write">
      <div class="title-text">中国人已经习惯了移动支付</div>
      <textarea qid="1003" qIndex="1" qSubIndex="1"></textarea>
    </div>
  </div>
</div>
"""


def _parse() -> dict:
    return itest_exam.parse_paper({"data": {"C_HTML": C_HTML, "UIK": 7, "TL": 1000, "TYPE": 0}})


def test_sections_and_questions() -> None:
    paper = _parse()
    assert paper["sectionCount"] == 2
    assert paper["questionCount"] == 3
    assert paper["uik"] == 7
    first, second = paper["sections"]
    assert first["sectionKey"] == "sk1"
    assert first["part1"] == "第一次作业"
    assert second["part2"] == "翻译7句"


def test_question_types() -> None:
    types = [q["type"] for s in _parse()["sections"] for q in s["questions"]]
    assert types == ["itest-danxuan", "itest-danxuan", "itest-write"]


def test_option_head_is_parsed() -> None:
    q = _parse()["sections"][0]["questions"][0]
    assert [o["value"] for o in q["options"]] == ["A", "B"]
    assert q["options"][0]["text"] == "养分"


def test_radio_options_keep_shuffle_values() -> None:
    q = _parse()["sections"][0]["questions"][1]
    assert [o["value"] for o in q["options"]] == ["1", "0"]
    assert q["options"][1]["text"] == "B. second"
    assert q["options"][0]["qOO"] == "[1,0]"


def test_blanks_skip_radio_and_keep_indices() -> None:
    danxuan, hearing, write = [q for s in _parse()["sections"] for q in s["questions"]]
    assert [(b["qIndex"], b["qSubIndex"]) for b in danxuan["inputs"]] == [(1, 1), (2, 2)]
    assert hearing["inputs"] == []  # radio 属于 options，不算填空位
    assert write["inputs"][0]["tag"] == "textarea"


def test_audios_and_prompt() -> None:
    paper = _parse()
    assert paper["sections"][0]["audios"] == ["https://cdn/a.mp3"]
    write = paper["sections"][1]["questions"][0]
    assert "移动支付" in write["text"]
