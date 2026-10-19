"""iTEST AI 答题测试（离线：假模型，不打网络）。

锁三件事：
1. 填空能按选项字母数/合法字母校验，错了就跳过而不是瞎填；
2. 翻译按条数校验，条数不对整批跳过；
3. 听力题永远跳过（文字模型听不到音频）。
"""

from __future__ import annotations

import json
import unittest

from kedaibiao.platforms.itest.answer import (
    answer_paper,
    blank_letters,
    clean_chinese,
)
from kedaibiao.platforms.itest.paper import parse_paper
from tests.test_itest_paper import PAPER


class FakeLLM:
    """按提示词内容回放固定 JSON 的假模型。"""

    def __init__(self, *, letters=None, translations=None, raw=None) -> None:
        self.letters = letters if letters is not None else ["A", "C"]
        self.translations = translations
        self.raw = raw
        self.prompts: list[str] = []

    async def complete(self, system: str, user: str, *, max_tokens: int = 1200) -> str:
        self.prompts.append(user)
        if self.raw is not None:
            return self.raw
        if "translations" in user:
            if self.translations is not None:
                return json.dumps({"translations": self.translations}, ensure_ascii=False)
            return json.dumps({"translations": ["ok"]}, ensure_ascii=False)
        return json.dumps({"letters": self.letters})


def make_paper():
    return parse_paper(PAPER)


class TestHelpers(unittest.TestCase):
    def test_blank_letters_from_option_line(self) -> None:
        q = make_paper().sections[0].questions[0]
        self.assertEqual(blank_letters(q), ["A", "B", "C"])

    def test_clean_chinese_drops_counter(self) -> None:
        self.assertEqual(clean_chinese("把这句话译成英文。 字数统计： 12"), "把这句话译成英文。")
        self.assertEqual(clean_chinese(""), "")


class TestAnswerPaper(unittest.IsolatedAsyncioTestCase):
    async def test_answers_blanks_and_translations_skips_listening(self) -> None:
        paper = make_paper()
        sheet = await answer_paper(paper, FakeLLM(
            letters=["A", "C"],
            translations=["Sentence one.", "Sentence two."],
        ))
        blank_q = paper.sections[0].questions[0]
        self.assertEqual(sheet.answers[blank_q.qid], "A C")
        write_qs = [q for s in paper.sections for q in s.questions if q.kind == "write"]
        self.assertEqual(len(write_qs), 2)
        self.assertEqual(sheet.answers[write_qs[0].qid], "Sentence one.")
        # 听力必须被跳过，且给出原因
        kinds = {item["kind"] for item in sheet.skipped}
        self.assertIn("radio", kinds)
        radio = [i for i in sheet.skipped if i["kind"] == "radio"][0]
        self.assertIn("音频", radio["reason"])

    async def test_wrong_letter_count_is_skipped(self) -> None:
        sheet = await answer_paper(make_paper(), FakeLLM(letters=["A"]))
        self.assertEqual(sheet.answers, {}, "字母数不对就不该产出答案")
        reasons = [i["reason"] for i in sheet.skipped if i["kind"] == "blank"]
        self.assertTrue(any("字母数" in r for r in reasons), reasons)

    async def test_letter_not_in_options_is_skipped(self) -> None:
        sheet = await answer_paper(make_paper(), FakeLLM(letters=["Z", "Q"]))
        self.assertEqual(sheet.answers, {})
        reasons = [i["reason"] for i in sheet.skipped if i["kind"] == "blank"]
        self.assertTrue(any("不在选项" in r for r in reasons), reasons)

    async def test_broken_json_is_skipped_not_crashed(self) -> None:
        sheet = await answer_paper(make_paper(), FakeLLM(raw="这不是 JSON"))
        self.assertEqual(sheet.answers, {})

    async def test_translation_count_mismatch_skips_batch(self) -> None:
        sheet = await answer_paper(make_paper(), FakeLLM(translations=["only one"]))
        skipped_writes = [item for item in sheet.skipped if item["kind"] == "write"]
        self.assertEqual(len(skipped_writes), 2, "整批跳过并逐题记录")
        self.assertTrue(all("条数不对" in item["reason"] for item in skipped_writes))

    async def test_prompt_contains_marked_word_and_options(self) -> None:
        llm = FakeLLM()
        await answer_paper(make_paper(), llm)
        blank_prompt = [p for p in llm.prompts if '"letters"' in p][0]
        self.assertIn("[[battery]]", blank_prompt)
        self.assertIn("A)", blank_prompt)


class TestPaperRoundTrip(unittest.TestCase):
    def test_to_dict_from_dict_round_trip(self) -> None:
        from kedaibiao.platforms.itest.paper import Paper, Question

        paper = make_paper()
        restored = Paper.from_dict(json.loads(json.dumps(paper.to_dict(), ensure_ascii=False)))
        self.assertEqual(restored.title, paper.title)
        self.assertEqual(restored.question_count, paper.question_count)
        self.assertEqual(restored.blank_count, paper.blank_count)
        original = paper.sections[0].questions[0]
        again = restored.sections[0].questions[0]
        self.assertIsInstance(again, Question)
        self.assertEqual(again.qid, original.qid)
        self.assertEqual(again.sentences, original.sentences)
        self.assertEqual([b.index for b in again.blanks], [b.index for b in original.blanks])


if __name__ == "__main__":
    unittest.main()


class TestAnswersAction(unittest.IsolatedAsyncioTestCase):
    """动作层：读缓存试卷 → 调模型 → 落答案单（全程离线）。"""

    def _adapter(self, root):
        from types import SimpleNamespace

        class A:
            name = "itest"

            def __init__(self):
                self.ctx = SimpleNamespace(
                    settings=SimpleNamespace(session_dir=str(root / "sessions")),
                    environ={
                        "KEDAIBIAO_LLM_BASE_URL": "https://g.example/v1",
                        "KEDAIBIAO_LLM_MODEL": "m",
                        "KEDAIBIAO_LLM_API_KEY": "sk-test",
                    },
                )

        return A()

    async def test_answers_action_writes_sheet_and_reports_skips(self) -> None:
        import json
        import tempfile
        from pathlib import Path
        from unittest import mock

        from kedaibiao.api import actions

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = root / "itest" / "paper-1099.json"
            cache.parent.mkdir(parents=True)
            cache.write_text(
                json.dumps(make_paper().to_dict(), ensure_ascii=False), encoding="utf-8"
            )

            def cache_for(_adapter, kind, exam_id):
                return cache.parent / f"{kind}-{exam_id}.json"

            with mock.patch.object(actions, "_itest_cache", cache_for), \
                 mock.patch.object(actions, "_itest_shot_dir",
                                   lambda *a: cache.parent), \
                 mock.patch("kedaibiao.tasks.llm.LLMAnswerer.complete",
                            FakeLLM().complete):
                out = await actions._itest_answers(self._adapter(root), {"exam_id": "1099"})

            self.assertEqual(out["kind"], "table")
            self.assertTrue(any("跳过" in r["answer"] for r in out["rows"]))
            sheet = json.loads((cache.parent / "answers-1099.json").read_text(encoding="utf-8"))
            self.assertTrue(sheet["answers"])
            self.assertTrue(sheet["skipped"])

    async def test_answers_action_needs_paper_cache(self) -> None:
        import tempfile
        from pathlib import Path
        from unittest import mock

        from kedaibiao.api import actions

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            missing = root / "itest" / "paper-none.json"
            missing.parent.mkdir(parents=True)
            def cache_for(_adapter, kind, exam_id):
                return missing

            with (
                mock.patch.object(actions, "_itest_cache", cache_for),
                self.assertRaises(actions.ActionInputError) as ctx,
            ):
                await actions._itest_answers(self._adapter(root), {"exam_id": "none"})
            self.assertIn("取卷", str(ctx.exception))
