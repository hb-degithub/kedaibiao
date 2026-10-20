"""流水线测试：用假适配器把「干跑 / 真提交 / 幂等 / 降级」全跑一遍。

这些测试不联网、不开浏览器 —— 流水线的正确性必须能离线验证。
"""

from __future__ import annotations

import unittest

from kedaibiao.bank.store import ProgressStore, QuestionBank
from kedaibiao.core.events import EventBus
from kedaibiao.core.models import (
    Answer,
    AnswerSource,
    Assignment,
    Course,
    Credentials,
    Question,
    QuestionType,
    RunRecord,
    RunStatus,
    SubmissionResult,
    TaskKind,
)
from kedaibiao.core.scheduler import Scheduler
from kedaibiao.notify.base import NullNotifier
from kedaibiao.platforms.base import AdapterContext, Capabilities, PlatformAdapter
from kedaibiao.tasks.answerer import BankAnswerer, RuleAnswerer
from kedaibiao.tasks.pipeline import (
    EVENT_ASSIGNMENT_DONE,
    Pipeline,
    PipelineConfig,
)

CREDS = Credentials(username="stu", password="pw")


class FakeAdapter(PlatformAdapter):
    name = "fake"
    display_name = "假平台"
    capabilities = Capabilities()

    def __init__(
        self,
        ctx: AdapterContext | None = None,
        *,
        questions: list[Question] | None = None,
        assignments: list[Assignment] | None = None,
        login_ok: bool = True,
        submit_ok: bool = True,
        verdicts: bool = False,
    ) -> None:
        super().__init__(ctx)
        self._questions = questions or []
        self._assignments = assignments
        self._login_ok = login_ok
        self._submit_ok = submit_ok
        self._verdicts = verdicts
        self.login_calls = 0
        self.fetch_calls = 0
        self.submit_calls: list[tuple[Assignment, list[Answer]]] = []

    async def login(self, credentials: Credentials) -> bool:
        self.login_calls += 1
        return self._login_ok

    async def is_logged_in(self) -> bool:
        return self._login_ok

    async def list_courses(self) -> list[Course]:
        return [Course(remote_id="c1", name="软件工程")]

    async def list_assignments(self, course: Course) -> list[Assignment]:
        if self._assignments is not None:
            return self._assignments
        return [Assignment(remote_id="a1", title="第一章作业", kind=TaskKind.HOMEWORK)]

    async def fetch_questions(self, assignment: Assignment) -> list[Question]:
        self.fetch_calls += 1
        return list(self._questions)

    async def submit(self, assignment: Assignment, answers: list[Answer]) -> SubmissionResult:
        self.submit_calls.append((assignment, answers))
        raw = {}
        if self._verdicts:
            raw = {"questions": [{"id": q.remote_id, "correct": True} for q in self._questions]}
        return SubmissionResult(
            assignment_id=assignment.remote_id,
            ok=self._submit_ok,
            score=95.0 if self._submit_ok else None,
            message="交卷成功" if self._submit_ok else "已被拒绝",
            raw=raw,
        )


def _known_questions() -> list[Question]:
    return [
        Question(
            stem="下列哪项属于结构化数据",
            type=QuestionType.SINGLE,
            options=["关系表", "图片", "音频"],
            remote_id="q1",
        )
    ]


def _seed_bank(bank: QuestionBank, questions: list[Question]) -> None:
    for i, q in enumerate(questions):
        bank.upsert(
            q,
            Answer(question=q, choices=[i % max(len(q.options), 1) or 0],
                   source=AnswerSource.BANK_HIT, confidence=1.0),
        )


def _pipeline(adapter: FakeAdapter, *, config: PipelineConfig | None = None, **kw):
    bank = kw.pop("bank", None)
    store = kw.pop("store", None)
    answerer = kw.pop("answerer", None) or BankAnswerer(bank)
    return (
        Pipeline(
            answerer=answerer,
            bank=bank,
            store=store,
            bus=EventBus(),
            scheduler=Scheduler(),
            notifier=NullNotifier(),
            config=config or PipelineConfig(),
        ),
        bank,
        store,
    )


class TestPipelineDryRun(unittest.IsolatedAsyncioTestCase):
    async def test_dry_run_never_submits(self) -> None:
        bank = QuestionBank(":memory:")
        self.addCleanup(bank.close)
        questions = _known_questions()
        _seed_bank(bank, questions)
        adapter = FakeAdapter(AdapterContext(), questions=questions)

        pipe, _, _ = _pipeline(adapter, bank=bank, config=PipelineConfig(dry_run=True))
        report = await pipe.run(adapter, CREDS)

        self.assertEqual(adapter.submit_calls, [], "干跑不允许有任何写操作")
        self.assertEqual(report.skipped, 1)
        record = report.records[0]
        self.assertIs(record.status, RunStatus.SKIPPED)
        self.assertIn("dry_run", record.detail["reason"])
        self.assertEqual(record.detail["question_count"], 1)
        self.assertEqual(record.detail["answered"], 1)
        self.assertEqual(record.detail["avg_confidence"], 1.0)
        self.assertEqual(len(record.detail["plan"]), 1)

    async def test_dry_run_summary_text(self) -> None:
        bank = QuestionBank(":memory:")
        self.addCleanup(bank.close)
        questions = _known_questions()
        _seed_bank(bank, questions)
        adapter = FakeAdapter(AdapterContext(), questions=questions)
        pipe, _, _ = _pipeline(adapter, bank=bank)
        report = await pipe.run(adapter, CREDS)
        self.assertIn("假平台", report.summary())
        self.assertIn("跳过 1", report.summary())


class TestPipelineLive(unittest.IsolatedAsyncioTestCase):
    async def test_live_submits_and_records(self) -> None:
        bank = QuestionBank(":memory:")
        store = ProgressStore(":memory:")
        self.addCleanup(bank.close)
        self.addCleanup(store.close)
        questions = _known_questions()
        _seed_bank(bank, questions)
        adapter = FakeAdapter(AdapterContext(), questions=questions)

        pipe, _, _ = _pipeline(
            adapter, bank=bank, store=store, config=PipelineConfig(dry_run=False)
        )
        report = await pipe.run(adapter, CREDS)

        self.assertEqual(len(adapter.submit_calls), 1)
        self.assertEqual(report.succeeded, 1)
        self.assertEqual(report.records[0].detail["submit"]["ok"], True)
        self.assertTrue(store.already_done("fake", "a1"))
        subs = store._conn.execute("SELECT * FROM submissions").fetchall()  # noqa: SLF001
        self.assertEqual(len(subs), 1)
        self.assertEqual(subs[0]["score"], 95.0)

    async def test_rejected_submission_marks_failed(self) -> None:
        bank = QuestionBank(":memory:")
        self.addCleanup(bank.close)
        questions = _known_questions()
        _seed_bank(bank, questions)
        adapter = FakeAdapter(AdapterContext(), questions=questions, submit_ok=False)

        pipe, _, _ = _pipeline(adapter, bank=bank, config=PipelineConfig(dry_run=False))
        report = await pipe.run(adapter, CREDS)

        self.assertEqual(report.failed, 1)
        self.assertIn("提交被拒", report.records[0].error)

    async def test_verdicts_written_back_to_bank(self) -> None:
        bank = QuestionBank(":memory:")
        self.addCleanup(bank.close)
        questions = _known_questions()
        _seed_bank(bank, questions)
        adapter = FakeAdapter(AdapterContext(), questions=questions, verdicts=True)

        pipe, _, _ = _pipeline(adapter, bank=bank, config=PipelineConfig(dry_run=False))
        await pipe.run(adapter, CREDS)

        self.assertEqual(bank.stats()["graded"], 1, "平台给了对错就必须回填")
        self.assertEqual(bank.stats()["correct"], 1)

    async def test_no_verdicts_leaves_bank_ungraded(self) -> None:
        bank = QuestionBank(":memory:")
        self.addCleanup(bank.close)
        questions = _known_questions()
        _seed_bank(bank, questions)
        adapter = FakeAdapter(AdapterContext(), questions=questions, verdicts=False)

        pipe, _, _ = _pipeline(adapter, bank=bank, config=PipelineConfig(dry_run=False))
        await pipe.run(adapter, CREDS)

        self.assertEqual(bank.stats()["graded"], 0, "平台没判分就不能瞎标对错")


class TestPipelineGuards(unittest.IsolatedAsyncioTestCase):
    async def test_low_confidence_blocks_submit(self) -> None:
        bank = QuestionBank(":memory:")
        self.addCleanup(bank.close)
        adapter = FakeAdapter(AdapterContext(), questions=_known_questions())

        pipe, _, _ = _pipeline(
            adapter,
            bank=bank,
            answerer=RuleAnswerer(),
            config=PipelineConfig(dry_run=False, min_confidence=0.75),
        )
        report = await pipe.run(adapter, CREDS)

        self.assertEqual(adapter.submit_calls, [])
        self.assertEqual(report.skipped, 1)
        self.assertIn("置信度", report.records[0].detail["reason"])

    async def test_unanswerable_questions_skipped(self) -> None:
        bank = QuestionBank(":memory:")
        self.addCleanup(bank.close)
        adapter = FakeAdapter(AdapterContext(), questions=[Question(stem="没入库的题")])

        pipe, _, _ = _pipeline(adapter, bank=bank, config=PipelineConfig(dry_run=False))
        report = await pipe.run(adapter, CREDS)

        self.assertEqual(report.skipped, 1)
        self.assertIn("一题都没答出来", report.records[0].detail["reason"])

    async def test_no_questions_skipped(self) -> None:
        bank = QuestionBank(":memory:")
        self.addCleanup(bank.close)
        adapter = FakeAdapter(AdapterContext(), questions=[])

        pipe, _, _ = _pipeline(adapter, bank=bank, config=PipelineConfig(dry_run=False))
        report = await pipe.run(adapter, CREDS)

        self.assertEqual(report.skipped, 1)
        self.assertIn("没有取到题目", report.records[0].detail["reason"])

    async def test_platform_finished_is_skipped_without_fetch(self) -> None:
        bank = QuestionBank(":memory:")
        self.addCleanup(bank.close)
        finished = Assignment(remote_id="a9", title="已完成的作业", finished=True)
        adapter = FakeAdapter(AdapterContext(), assignments=[finished], questions=_known_questions())

        pipe, _, _ = _pipeline(adapter, bank=bank, config=PipelineConfig(dry_run=False))
        report = await pipe.run(adapter, CREDS)

        self.assertEqual(report.skipped, 1)
        self.assertEqual(adapter.fetch_calls, 0)
        self.assertIn("平台已标记完成", report.records[0].detail["reason"])

    async def test_locally_done_is_skipped(self) -> None:
        bank = QuestionBank(":memory:")
        store = ProgressStore(":memory:")
        self.addCleanup(bank.close)
        self.addCleanup(store.close)
        run_id = store.start_run(RunRecord(platform="fake", assignment_id="a1"))
        store.finish_run(run_id, RunStatus.SUCCEEDED)

        adapter = FakeAdapter(AdapterContext(), questions=_known_questions())
        pipe, _, _ = _pipeline(
            adapter, bank=bank, store=store, config=PipelineConfig(dry_run=False)
        )
        report = await pipe.run(adapter, CREDS)

        self.assertEqual(report.skipped, 1)
        self.assertEqual(adapter.fetch_calls, 0)
        self.assertIn("本地已成功记录", report.records[0].detail["reason"])

    async def test_login_failure_reported(self) -> None:
        bank = QuestionBank(":memory:")
        self.addCleanup(bank.close)
        adapter = FakeAdapter(AdapterContext(), login_ok=False, questions=_known_questions())

        pipe, _, _ = _pipeline(adapter, bank=bank, config=PipelineConfig(dry_run=False))
        report = await pipe.run(adapter, CREDS)

        self.assertEqual(report.failed, 1)
        self.assertEqual(report.records[0].assignment_id, "<login>")
        self.assertIn("登录失败", report.records[0].error)


class TestPipelineSelection(unittest.IsolatedAsyncioTestCase):
    async def _run(self, config: PipelineConfig, assignments: list[Assignment]) -> list[str]:
        bank = QuestionBank(":memory:")
        self.addCleanup(bank.close)
        questions = _known_questions()
        _seed_bank(bank, questions)
        adapter = FakeAdapter(AdapterContext(), assignments=assignments, questions=questions)
        pipe, _, _ = _pipeline(adapter, bank=bank, config=config)
        report = await pipe.run(adapter, CREDS)
        return [r.assignment_id for r in report.records]

    async def test_kind_filter(self) -> None:
        items = [
            Assignment(remote_id="hw", title="作业", kind=TaskKind.HOMEWORK),
            Assignment(remote_id="ex", title="考试", kind=TaskKind.EXAM),
        ]
        config = PipelineConfig(dry_run=False, kinds=frozenset({TaskKind.EXAM}))
        self.assertEqual(await self._run(config, items), ["ex"])

    async def test_empty_kinds_means_all(self) -> None:
        items = [
            Assignment(remote_id="hw", title="作业", kind=TaskKind.HOMEWORK),
            Assignment(remote_id="ex", title="考试", kind=TaskKind.EXAM),
        ]
        self.assertEqual(sorted(await self._run(PipelineConfig(dry_run=False), items)), ["ex", "hw"])

    async def test_max_assignments_cap(self) -> None:
        items = [Assignment(remote_id=f"a{i}", title=f"作业{i}") for i in range(5)]
        config = PipelineConfig(dry_run=False, max_assignments=2)
        self.assertEqual(len(await self._run(config, items)), 2)


class TestPipelineEvents(unittest.IsolatedAsyncioTestCase):
    async def test_assignment_done_event_emitted(self) -> None:
        bank = QuestionBank(":memory:")
        self.addCleanup(bank.close)
        questions = _known_questions()
        _seed_bank(bank, questions)
        adapter = FakeAdapter(AdapterContext(), questions=questions)
        pipe, _, _ = _pipeline(adapter, bank=bank, config=PipelineConfig(dry_run=False))

        seen: list[dict] = []
        pipe.bus.on(EVENT_ASSIGNMENT_DONE, lambda event: seen.append(event.payload))
        await pipe.run(adapter, CREDS)

        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["assignment_id"], "a1")
        self.assertEqual(seen[0]["status"], "succeeded")


class _CourseScopedError(RuntimeError):
    """模拟品课 ``6569 课程不存在``：只影响某一门课的业务错误。"""


class TwoCourseAdapter(FakeAdapter):
    """第一门课正常、第二门课抛课程级错误；声明了它可跳过。"""

    skippable_course_errors = (_CourseScopedError,)

    async def list_courses(self) -> list[Course]:
        return [
            Course(remote_id="c1", name="软件工程"),
            Course(remote_id="c2", name="已删除的课"),
        ]

    async def list_assignments(self, course: Course) -> list[Assignment]:
        if course.remote_id == "c2":
            raise _CourseScopedError("6569 课程不存在")
        return await super().list_assignments(course)


class TestCourseScopedCollection(unittest.IsolatedAsyncioTestCase):
    """一门课坏了不许让整次抓取失败；但没声明可跳过的异常必须上抛。"""

    async def test_bad_course_does_not_abort_other_courses(self) -> None:
        bank = QuestionBank(":memory:")
        self.addCleanup(bank.close)
        questions = _known_questions()
        _seed_bank(bank, questions)
        adapter = TwoCourseAdapter(AdapterContext(), questions=questions)
        pipe, _, _ = _pipeline(adapter, bank=bank)

        report = await pipe.run(adapter, CREDS)

        self.assertEqual([r.assignment_id for r in report.records], ["a1"])
        self.assertIs(report.records[0].status, RunStatus.SKIPPED)

    async def test_error_the_platform_did_not_declare_still_aborts(self) -> None:
        bank = QuestionBank(":memory:")
        self.addCleanup(bank.close)
        questions = _known_questions()
        _seed_bank(bank, questions)
        adapter = TwoCourseAdapter(AdapterContext(), questions=questions)
        # 平台没声明 = 不跳：登录失效/网络错误绝不能被当成「这门课没作业」。
        adapter.skippable_course_errors = ()  # type: ignore[attr-defined]
        pipe, _, _ = _pipeline(adapter, bank=bank)

        with self.assertRaises(_CourseScopedError):
            await pipe.run(adapter, CREDS)


if __name__ == "__main__":
    unittest.main()
