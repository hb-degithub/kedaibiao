"""iTEST 听力转写测试（离线：不装模型、不联网）。

锁的是**降级行为**：没装 faster-whisper / 下载失败 / 转写失败时，都应该安静跳过，
而不是把整条答题链拖死。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kedaibiao.platforms.itest import stt
from kedaibiao.platforms.itest.paper import Paper, Question, Section, parse_paper
from tests.test_itest_paper import PAPER


class TestAvailability(unittest.TestCase):
    def test_available_false_when_module_missing(self) -> None:
        real = sys.modules.get("faster_whisper")
        sys.modules["faster_whisper"] = None  # type: ignore[assignment]
        try:
            self.assertFalse(stt.available())
        finally:
            if real is not None:
                sys.modules["faster_whisper"] = real
            else:
                sys.modules.pop("faster_whisper", None)

    def test_model_name_default_and_override(self) -> None:
        self.assertEqual(stt.model_name({}), stt.DEFAULT_MODEL)
        self.assertEqual(stt.model_name({"KEDAIBIAO_STT_MODEL": "small.en"}), "small.en")


class TestTranscribePaper(unittest.TestCase):
    def setUp(self) -> None:
        self.paper = parse_paper(PAPER)  # 第 2 个 section 是听力，带 1 个音频
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_no_audio_sections_returns_empty(self) -> None:
        paper = Paper(sections=[Section(index=1)])
        self.assertEqual(stt.transcribe_paper(paper, self.tmp.name), {})

    def test_transcribes_each_audio_section(self) -> None:
        with mock.patch.object(stt, "download", lambda url, d, **k: Path(d) / "a.mp3"), \
             mock.patch.object(stt, "transcribe_file", lambda p, **k: "hello world"):
            out = stt.transcribe_paper(self.paper, self.tmp.name)
        self.assertEqual(out, {2: "hello world"})

    def test_single_section_failure_is_skipped(self) -> None:
        def boom(*_args, **_kwargs):
            raise RuntimeError("model exploded")

        with mock.patch.object(stt, "download", boom):
            self.assertEqual(stt.transcribe_paper(self.paper, self.tmp.name), {})

    def test_empty_transcript_not_added(self) -> None:
        with mock.patch.object(stt, "download", lambda url, d, **k: Path(d) / "a.mp3"), \
             mock.patch.object(stt, "transcribe_file", lambda p, **k: "   "):
            self.assertEqual(stt.transcribe_paper(self.paper, self.tmp.name), {})


class TestAudioParsing(unittest.TestCase):
    def test_section_audios_found_among_empty_lists(self) -> None:
        """实测 DOM 里同一个 section 有多个 itest-hear-reslist，多数是空数组。"""
        html = """
        <div class="itest-section" sectionType="sHear" part1="作业">
          <div class="itest-hear-reslist">[]</div>
          <div class="itest-hear-reslist">
            ["https://itestres.unipus.cn/a.mp3","5","https://itestres.unipus.cn/a.mp3","5"]
          </div>
          <div class="itest-ques" qid="1">
            <div class="option"><label><input type="radio" qid="1" qsubindex="1" value="0">
              &nbsp;A. one</label></div>
          </div>
        </div>
        """
        paper = parse_paper(html)
        self.assertEqual(paper.sections[0].audios, ["https://itestres.unipus.cn/a.mp3"])


class TestModelCache(unittest.TestCase):
    def test_model_is_loaded_once(self) -> None:
        calls: list[tuple] = []

        class FakeModel:
            def __init__(self, name, device, compute_type):
                calls.append((name, device, compute_type))

        fake_module = type("M", (), {"WhisperModel": FakeModel})
        stt._MODEL_CACHE.clear()
        self.addCleanup(stt._MODEL_CACHE.clear)
        with mock.patch.dict(sys.modules, {"faster_whisper": fake_module}):
            first = stt._load_model({})
            second = stt._load_model({})
        self.assertIs(first, second)
        self.assertEqual(len(calls), 1)


class TestBuildSheet(unittest.IsolatedAsyncioTestCase):
    async def test_build_sheet_notes_when_stt_missing(self) -> None:
        from kedaibiao.platforms.itest.answer import build_sheet

        class FakeLLM:
            async def complete(self, system, user, *, max_tokens=1200):
                return '{"letters": ["A", "B"], "translations": ["a", "b"]}'

        paper = parse_paper(PAPER)
        with mock.patch.object(stt, "available", lambda: False):
            sheet, transcripts, note = await build_sheet(
                paper, FakeLLM(), "/tmp", transcribe=True
            )
        self.assertEqual(transcripts, {})
        self.assertIn("faster-whisper", note)
        radio = [i for i in sheet.skipped if i["kind"] == "radio"]
        self.assertTrue(radio, "没转写时听力必须跳过")


class TestQuestionsHelper(unittest.TestCase):
    def test_question_kinds_in_fixture(self) -> None:
        paper = parse_paper(PAPER)
        kinds = {q.kind for s in paper.sections for q in s.questions}
        self.assertEqual(kinds, {"blank", "radio", "write"})
        self.assertIsInstance(paper.sections[0].questions[0], Question)


if __name__ == "__main__":
    unittest.main()
