"""iTEST 试卷解析与填表脚本测试（离线，用合成 DOM）。

结构照 2026-10-19 实测的作答页写，重点锁三件事：
1. 三种题型能从 DOM 正确识别；
2. **单选的 value 与显示字母不一致**（被 qoo 打乱）—— 必须按字母选；
3. 填表/核验脚本是数据驱动（按 qid），不是按位置。
"""

from __future__ import annotations

import json
import unittest

from kedaibiao.platforms.itest.paper import (
    KIND_BLANK,
    KIND_RADIO,
    KIND_WRITE,
    fill_script,
    parse_paper,
    verify_script,
)

PAPER = """
<html><head><title>试题作答 - iTEST</title></head><body>
<div class="itest-section" part1="第1次课后作业" part2="词汇选择一组"
     sectionKey="k1" sectionType="sNormal" resNeedPlay="-1">
  <div class="itest-ques" qid="1001">
    A) 养分； B) 电池； C) 农业的；
    <span><input type="text" qid="1001" qsubindex="1" placeholder="1" class="blankinput">
      &nbsp;Your <u>battery</u> is running out.</span>
    <span><input type="text" qid="1001" qsubindex="2" placeholder="2" class="blankinput">
      &nbsp;30 percent increase in <u>agricultural</u> production.</span>
  </div>
</div>
<div class="itest-section" part1="第1次课后作业" part2="短文听力"
     sectionKey="k2" sectionType="sHear" resNeedPlay="1">
  <div class="itest-hear-reslist">["https://itestres.unipus.cn/a.mp3","5"]</div>
  <div class="itest-ques" qid="2002">
    <div class="option-head">1. </div>
    <div class="option hear-row"><label><input type="radio" name="2002_1_1" value="2"
      qid="2002" qsubindex="1" qsubsubindex="1" qoo="[2,0,3,1]">
      &nbsp;&nbsp;A. Hide-and-seek.</label></div>
    <div class="option hear-row"><label><input type="radio" name="2002_1_1" value="0"
      qid="2002" qsubindex="1" qsubsubindex="1">&nbsp;&nbsp;B. Football match.</label></div>
    <div class="option hear-row"><label><input type="radio" name="2002_1_1" value="3"
      qid="2002" qsubindex="1" qsubsubindex="1">&nbsp;&nbsp;C. Tricks on others.</label></div>
    <div class="option hear-row"><label><input type="radio" name="2002_1_1" value="1"
      qid="2002" qsubindex="1" qsubsubindex="1">&nbsp;&nbsp;D. Games on the beach.</label></div>
    <div class="option-head">2. </div>
    <div class="option hear-row"><label><input type="radio" name="2002_1_2" value="1"
      qid="2002" qsubindex="2">&nbsp;&nbsp;A. It was dark inside.</label></div>
    <div class="option hear-row"><label><input type="radio" name="2002_1_2" value="2"
      qid="2002" qsubindex="2">&nbsp;&nbsp;B. It was small inside.</label></div>
    <div class="option hear-row"><label><input type="radio" name="2002_1_2" value="0"
      qid="2002" qsubindex="2">&nbsp;&nbsp;C. The door was locked.</label></div>
    <div class="option hear-row"><label><input type="radio" name="2002_1_2" value="3"
      qid="2002" qsubindex="2">&nbsp;&nbsp;D. It was raining.</label></div>
  </div>
</div>
<div class="itest-section" part1="第2次课后作业" part2="翻译7句"
     sectionKey="k3" sectionType="sNormal" resNeedPlay="-1">
  <div class="itest-ques" qid="3003">
    <span>书法给人一种很难掌握的印象。</span>
    <textarea qid="3003" qsubindex="1"></textarea>
  </div>
  <div class="itest-ques" qid="3004">
    <span>在中国，很少有人带钱包出门。</span>
    <textarea qid="3004" qsubindex="1"></textarea>
  </div>
</div>
</body></html>
"""


class TestParse(unittest.TestCase):
    def setUp(self) -> None:
        self.paper = parse_paper(PAPER)

    def test_sections_and_kinds(self) -> None:
        self.assertEqual(len(self.paper.sections), 3)
        kinds = [s.kind for s in self.paper.sections]
        self.assertEqual(kinds, [KIND_BLANK, KIND_RADIO, KIND_WRITE])

    def test_question_count(self) -> None:
        self.assertEqual(self.paper.question_count, 4, "1 词汇 + 1 听力 + 2 翻译")

    def test_blank_slots(self) -> None:
        q = self.paper.sections[0].questions[0]
        self.assertEqual(q.kind, KIND_BLANK)
        self.assertEqual(q.slot_count, 2)
        self.assertEqual([b.index for b in q.blanks], [1, 2])

    def test_blank_sentences_keep_underlined_word(self) -> None:
        """原句里被考查的词要保留 —— 那是推导答案的依据。"""
        q = self.paper.sections[0].questions[0]
        joined = " ".join(q.sentences)
        self.assertIn("battery", joined)
        self.assertIn("agricultural", joined)

    def test_radio_letters_differ_from_values(self) -> None:
        """★ 核心：value 与显示字母不一致（qoo 打乱过），选的时候必须按字母。"""
        q = self.paper.sections[1].questions[0]
        self.assertEqual(q.kind, KIND_RADIO)
        pairs = {c.letter: c.value for c in q.choices if c.sub == 1}
        self.assertEqual(pairs, {"A": "2", "B": "0", "C": "3", "D": "1"})
        self.assertNotEqual(pairs["A"], "0", "A 的 value 不是 0 —— 按顺序取会选错")

    def test_radio_slot_count_by_subquestion(self) -> None:
        q = self.paper.sections[1].questions[0]
        self.assertEqual(q.slot_count, 2, "两个小题")
        self.assertEqual(len(q.choices), 8)

    def test_audio_urls(self) -> None:
        self.assertEqual(self.paper.sections[1].audios,
                         ["https://itestres.unipus.cn/a.mp3"])

    def test_write_questions(self) -> None:
        qs = self.paper.sections[2].questions
        self.assertEqual([q.kind for q in qs], [KIND_WRITE, KIND_WRITE])
        self.assertIn("书法", qs[0].text)

    def test_total_slots(self) -> None:
        # 2 填空 + 2 单选 + 2 翻译 = 6
        self.assertEqual(self.paper.blank_count, 6)

    def test_empty_html_is_not_an_error(self) -> None:
        """页面可能还在加载 —— 返回空卷而不是抛错。"""
        self.assertEqual(parse_paper("").sections, [])
        self.assertEqual(parse_paper("<html></html>").question_count, 0)


class TestApplyAnswers(unittest.TestCase):
    def test_all_qids_matched(self) -> None:
        paper = parse_paper(PAPER)
        stat = paper.apply_answers({"1001": "B C", "2002": "A C", "3003": "x", "3004": "y"})
        self.assertEqual(stat["applied"], 4)
        self.assertEqual(stat["unknown_qids"], [])

    def test_unknown_qid_reported(self) -> None:
        paper = parse_paper(PAPER)
        stat = paper.apply_answers({"9999": "A"})
        self.assertEqual(stat["unknown_qids"], ["9999"])
        self.assertEqual(stat["applied"], 0)

    def test_answer_written_back(self) -> None:
        paper = parse_paper(PAPER)
        paper.apply_answers({"1001": "B C"})
        self.assertEqual(paper.by_qid()["1001"].answer, "B C")

    def test_to_dict_serialisable(self) -> None:
        paper = parse_paper(PAPER)
        paper.apply_answers({"1001": "B C"})
        blob = json.dumps(paper.to_dict(), ensure_ascii=False)
        self.assertIn("1001", blob)
        self.assertIn("battery", blob)


class TestScripts(unittest.TestCase):
    def test_fill_script_targets_by_qid(self) -> None:
        js = fill_script({"1001": "B C"})
        self.assertIn("1001", js)
        self.assertIn("blankinput", js)
        self.assertIn("qsubindex", js, "单选要按小题分组")

    def test_fill_script_matches_label_letter(self) -> None:
        """必须按 label 文本里的字母选，不能按第 N 个取。"""
        js = fill_script({"2002": "A C"})
        self.assertIn("closest('label')", js)

    def test_verify_script_reads_back(self) -> None:
        js = verify_script({"1001": "B C"})
        self.assertIn("el.value", js)
        self.assertIn("checked", js)

    def test_answers_are_json_escaped(self) -> None:
        """答案里的引号/反斜杠必须经 JSON 转义，否则生成的 JS 语法就破了。"""
        value = 'He said "hi" \\ backslash'
        js = fill_script({"3003": value})
        literal = js.split("const ANS =", 1)[1].split(";", 1)[0].strip()
        self.assertEqual(json.loads(literal), {"3003": value})


if __name__ == "__main__":
    unittest.main()
