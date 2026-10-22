"""定时跑批（``tasks/schedule.py``）的单测 —— 全部离线，不联网、不睡真实时间。

钉住四件事：

1. **间隔解析**：单位写错必须报错，不能把「24h」静默当成 24 秒跑；
2. **配置解析**：任务名重复 / 平台缺失 / 未知任务类型要在启动前就拦住；
3. **到期判定**：首次立即跑，之后按间隔；禁用的任务永远不跑；
4. **跑批器行为**：单任务失败不拖垮整轮、串行不并发、状态快照可读。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from kedaibiao.core.models import TaskKind
from kedaibiao.tasks.schedule import (
    ScheduleConfigError,
    ScheduleJob,
    SchedulePlan,
    ScheduleRunner,
    due_jobs,
    load_plan,
    parse_interval,
    parse_job,
    parse_plan,
)


class ParseIntervalTest(unittest.TestCase):
    def test_units(self) -> None:
        self.assertEqual(parse_interval("45s"), 45)
        self.assertEqual(parse_interval("30m"), 1800)
        self.assertEqual(parse_interval("6h"), 21600)
        self.assertEqual(parse_interval("1d"), 86400)
        self.assertEqual(parse_interval("900"), 900)
        self.assertEqual(parse_interval(90), 90)
        self.assertEqual(parse_interval("1.5h"), 5400)

    def test_rejects_garbage(self) -> None:
        for bad in ("", "abc", "24x", "0", "-5", "-3h"):
            with self.subTest(bad=bad), self.assertRaises(ScheduleConfigError):
                parse_interval(bad)


class ParseJobTest(unittest.TestCase):
    def test_minimal_job_defaults_to_dry_run(self) -> None:
        job = parse_job({"name": "j1", "platform": "unipus", "every": "6h"})
        self.assertFalse(job.live, "默认必须干跑")
        self.assertTrue(job.enabled)
        self.assertEqual(job.limit, 0)
        self.assertEqual(job.kinds, frozenset())

    def test_full_job(self) -> None:
        job = parse_job(
            {
                "name": "j1",
                "platform": "unipus",
                "every": "24h",
                "live": True,
                "limit": 3,
                "kinds": "homework,exam",
                "enabled": False,
            }
        )
        self.assertTrue(job.live)
        self.assertEqual(job.limit, 3)
        self.assertEqual(job.kinds, frozenset({TaskKind.HOMEWORK, TaskKind.EXAM}))
        self.assertFalse(job.enabled)

    def test_rejects_missing_fields_and_bad_values(self) -> None:
        cases = [
            {"platform": "unipus", "every": "1h"},
            {"name": "j", "every": "1h"},
            {"name": "j", "platform": "unipus"},
            {"name": "j", "platform": "unipus", "every": "1h", "limit": -1},
            {"name": "j", "platform": "unipus", "every": "1h", "limit": True},
            {"name": "j", "platform": "unipus", "every": "1h", "kinds": "nope"},
        ]
        for raw in cases:
            with self.subTest(raw=raw), self.assertRaises(ScheduleConfigError):
                parse_job(raw)


class ParsePlanTest(unittest.TestCase):
    def test_empty_plan_is_disabled(self) -> None:
        plan = parse_plan(None)
        self.assertFalse(plan.enabled)
        self.assertEqual(plan.jobs, [])
        self.assertEqual(plan.tick, 30.0)

    def test_full_plan(self) -> None:
        plan = parse_plan(
            {
                "enabled": True,
                "tick": 5,
                "jobs": [
                    {"name": "a", "platform": "unipus", "every": "1h"},
                    {"name": "b", "platform": "neusoft", "every": "2h", "live": True},
                ],
            }
        )
        self.assertTrue(plan.enabled)
        self.assertEqual(plan.tick, 5.0)
        self.assertEqual([j.name for j in plan.jobs], ["a", "b"])

    def test_duplicate_names_rejected(self) -> None:
        with self.assertRaises(ScheduleConfigError):
            parse_plan(
                {
                    "jobs": [
                        {"name": "a", "platform": "unipus", "every": "1h"},
                        {"name": "a", "platform": "neusoft", "every": "1h"},
                    ]
                }
            )

    def test_bad_tick_rejected(self) -> None:
        for bad in (0, -1, "abc"):
            with self.subTest(tick=bad), self.assertRaises(ScheduleConfigError):
                parse_plan({"tick": bad})


class LoadPlanTest(unittest.TestCase):
    def _write(self, root: Path, name: str, text: str) -> None:
        (root / "config").mkdir(parents=True, exist_ok=True)
        (root / "config" / name).write_text(text, encoding="utf-8")

    def test_missing_config_returns_disabled_plan(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            plan = load_plan(folder)
        self.assertFalse(plan.enabled)
        self.assertEqual(plan.jobs, [])

    def test_local_overrides_default(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self._write(
                root,
                "default.toml",
                "[schedule]\nenabled = false\ntick = 30\n"
                "[[schedule.jobs]]\nname = \"a\"\nplatform = \"unipus\"\nevery = \"1h\"\n",
            )
            self._write(root, "local.toml", "[schedule]\nenabled = true\ntick = 5\n")
            plan = load_plan(root)
        self.assertTrue(plan.enabled)
        self.assertEqual(plan.tick, 5.0)
        self.assertEqual([j.name for j in plan.jobs], ["a"])

    def test_broken_schedule_section_raises(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self._write(
                root,
                "default.toml",
                "[schedule]\n[[schedule.jobs]]\nname = \"a\"\nplatform = \"x\"\nevery = \"0\"\n",
            )
            with self.assertRaises(ScheduleConfigError):
                load_plan(root)


class DueJobsTest(unittest.TestCase):
    def _job(self, name: str = "a", every: float = 60, enabled: bool = True) -> ScheduleJob:
        return ScheduleJob(
            name=name, platform="unipus", every_seconds=every, enabled=enabled
        )

    def test_first_run_is_immediately_due(self) -> None:
        job = self._job()
        self.assertEqual(due_jobs([job], {}, now=1000.0), [job])

    def test_not_due_before_interval(self) -> None:
        job = self._job()
        self.assertEqual(due_jobs([job], {"a": 1000.0}, now=1059.0), [])
        self.assertEqual(due_jobs([job], {"a": 1000.0}, now=1060.0), [job])

    def test_disabled_never_due(self) -> None:
        job = self._job(enabled=False)
        self.assertEqual(due_jobs([job], {}, now=1.0), [])


class RunnerTest(unittest.TestCase):
    def _plan(self) -> SchedulePlan:
        return SchedulePlan(
            enabled=True,
            tick=1,
            jobs=[
                ScheduleJob(name="a", platform="unipus", every_seconds=60),
                ScheduleJob(name="b", platform="neusoft", every_seconds=120),
            ],
        )

    def test_runs_serially_and_respects_interval(self) -> None:
        calls: list[str] = []
        clock = {"now": 1000.0}

        def execute(job: ScheduleJob):
            calls.append(job.name)
            return {"summary": f"{job.name} ok"}

        runner = ScheduleRunner(self._plan(), execute, clock=lambda: clock["now"])

        first = runner.run_pending()
        self.assertEqual([item["name"] for item in first], ["a", "b"])
        self.assertEqual(calls, ["a", "b"])

        # 立刻再跑：都没到期
        self.assertEqual(runner.run_pending(), [])

        clock["now"] += 61
        self.assertEqual([item["name"] for item in runner.run_pending()], ["a"])

        clock["now"] += 60
        self.assertEqual([item["name"] for item in runner.run_pending()], ["a", "b"])
        self.assertTrue(runner.status()["enabled"])
        self.assertEqual(runner.status()["triggered"], 5)

    def test_failure_does_not_block_other_jobs(self) -> None:
        def execute(job: ScheduleJob):
            if job.name == "a":
                raise RuntimeError("boom")
            return {"summary": "ok"}

        runner = ScheduleRunner(self._plan(), execute, clock=lambda: 1000.0)
        finished = runner.run_pending()
        self.assertEqual([item["name"] for item in finished], ["a", "b"])
        self.assertFalse(finished[0]["ok"])
        self.assertTrue(finished[1]["ok"])
        self.assertIn("boom", runner.status()["jobs"][0]["last_error"])

    def test_no_duplicate_run_while_previous_still_recorded(self) -> None:
        """同一时刻同一任务只触发一次（last_run 以「开始时刻」记）。"""
        calls = []
        runner = ScheduleRunner(
            SchedulePlan(enabled=True, tick=1, jobs=[ScheduleJob("a", "unipus", 60)]),
            lambda job: calls.append(job.name) or {},
            clock=lambda: 500.0,
        )
        runner.run_pending()
        runner.run_pending()
        self.assertEqual(calls, ["a"])

    def test_thread_lifecycle(self) -> None:
        runner = ScheduleRunner(
            SchedulePlan(enabled=True, tick=0.05, jobs=[]),
            lambda job: {},
        )
        self.assertFalse(runner.is_running())
        runner.start()
        self.assertTrue(runner.is_running())
        runner.stop()
        self.assertFalse(runner.is_running())


if __name__ == "__main__":
    unittest.main()
