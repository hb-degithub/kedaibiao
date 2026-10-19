"""iTEST 一键自动化 + 人工补答案（离线）。

后台任务本身要开浏览器，单测里不跑真流程；这里锁的是：
- 任务登记 / 日志与步骤追加（前端靠它铺终端）；
- 人工答案合并进答案单缓存、缺项重算；
- 没有试卷缓存时的报错文案（告诉用户先跑自动化）。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kedaibiao.api.service import ConsoleError, ConsoleService, Viewer
from kedaibiao.platforms.itest import cache
from kedaibiao.platforms.itest.paper import parse_paper
from tests.test_itest_paper import PAPER

SUPER = Viewer(user_id=1, is_superadmin=True, username="admin")


def make_service(root: Path) -> ConsoleService:
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "config" / "local.toml").write_text(
        "[kedaibiao]\n"
        f'db_url = "sqlite:///{root}/auto.db"\n'
        f'session_dir = "{root}/sessions"\n'
        'notify = "null"\n',
        encoding="utf-8",
    )
    return ConsoleService(root=root)


class TestAutoJob(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.service = make_service(self.root)

    def test_job_registered_and_logs_appended(self) -> None:
        with mock.patch.object(ConsoleService, "_run_itest_auto", autospec=True) as runner:
            def fake(self_, job_id, exam_id, viewer, wait):
                self_._job_log(job_id, "info", "hello")
                self_._job_step(job_id, "登录", True, "ok")
                with self_._lock:
                    job = self_._jobs_by_id[job_id]
                    job["status"] = "succeeded"
            runner.side_effect = fake
            job = self.service.start_itest_auto("1099", viewer=SUPER)
        # 线程可能已经跑完（mock 很快），两种状态都算正常
        self.assertIn(job["status"], ("running", "succeeded"))
        self.assertEqual(job["kind"], "itest-auto")
        stored = self.service.job(job["job_id"])
        self.assertEqual(stored["exam_id"], "1099")
        self.assertTrue(stored["logs"])
        self.assertEqual(stored["steps"][0]["name"], "登录")
        self.assertEqual(stored["status"], "succeeded", "后台线程里的收尾应写回任务")

    def test_missing_exam_id_rejected(self) -> None:
        with self.assertRaises(ConsoleError) as ctx:
            self.service.start_itest_auto("  ", viewer=SUPER)
        self.assertEqual(ctx.exception.status, 400)

    def test_requesting_unknown_job_is_404(self) -> None:
        with self.assertRaises(ConsoleError) as ctx:
            self.service.job("nope")
        self.assertEqual(ctx.exception.status, 404)


class TestManualAnswers(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.service = make_service(self.root)
        self.settings = self.service.settings
        self.paper = parse_paper(PAPER)
        cache.write_json(cache.paper_path(self.settings, "1"), self.paper.to_dict())
        self.radio = [q for s in self.paper.sections for q in s.questions if q.kind == "radio"]
        self.blank = [q for s in self.paper.sections for q in s.questions if q.kind == "blank"]
        cache.write_json(
            cache.answers_path(self.settings, "1"),
            {"answers": {self.blank[0].qid: "A B"}, "skipped": [
                {"qid": self.radio[0].qid, "kind": "radio", "reason": "没有转写"},
            ]},
        )

    def test_manual_answers_merge_and_clear_missing(self) -> None:
        writes = [q for s in self.paper.sections for q in s.questions if q.kind == "write"]
        answers = {self.radio[0].qid: "C D"}
        answers.update({q.qid: f"translation {q.qid}" for q in writes})
        out = self.service.save_itest_manual("1", answers, viewer=SUPER)
        self.assertEqual(out["answered"], 1 + 1 + len(writes))
        self.assertEqual(out["missing"], [])
        self.assertTrue(out["can_submit"])
        sheet = json.loads(
            cache.answers_path(self.settings, "1").read_text(encoding="utf-8")
        )
        self.assertEqual(sheet["answers"][self.radio[0].qid], "C D")
        self.assertEqual(sheet["skipped"], [], "补上的题应从 skipped 里移除")

    def test_partial_manual_answers_keep_missing(self) -> None:
        # 试卷里还有翻译题没答
        out = self.service.save_itest_manual(
            "1", {self.radio[0].qid: "A"}, viewer=SUPER
        )
        self.assertFalse(out["can_submit"])
        self.assertTrue(out["missing"])
        self.assertNotIn(self.radio[0].qid, out["missing"])

    def test_blank_values_are_ignored(self) -> None:
        out = self.service.save_itest_manual("1", {self.radio[0].qid: "   "}, viewer=SUPER)
        self.assertIn(self.radio[0].qid, out["missing"], "空值不该算答上")

    def test_no_paper_cache_is_a_clear_error(self) -> None:
        with self.assertRaises(ConsoleError) as ctx:
            self.service.save_itest_manual("404", {"1": "A"}, viewer=SUPER)
        self.assertEqual(ctx.exception.status, 400)
        self.assertIn("一键自动化", str(ctx.exception))

    def test_empty_answers_rejected(self) -> None:
        with self.assertRaises(ConsoleError) as ctx:
            self.service.save_itest_manual("1", {}, viewer=SUPER)
        self.assertEqual(ctx.exception.status, 400)


if __name__ == "__main__":
    unittest.main()
