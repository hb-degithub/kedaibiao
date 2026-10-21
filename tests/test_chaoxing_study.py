"""学习通刷课引擎（study）的纯函数单测：不联网、不启浏览器。"""

from __future__ import annotations

import json

from kedaibiao.platforms.chaoxing import study


def test_marg_skips_empty_assignment() -> None:
    payload = {
        "attachments": [
            {
                "attDuration": 144,
                "jobid": "1",
                "property": {"module": "insertvideo", "name": "a.mp4"},
            }
        ],
        "defaults": {"reportTimeInterval": 60},
    }
    html = 'mArg = ""; try { mArg = ' + json.dumps(payload) + "; } catch (e) {}"
    parsed = study._marg(html)
    assert parsed is not None
    assert parsed["attachments"][0]["attDuration"] == 144


def test_marg_returns_none_on_garbage() -> None:
    assert study._marg("") is None
    assert study._marg("mArg = {broken") is None


def test_playback_limits_reads_face_capture_plan() -> None:
    marg = {
        "attachments": [
            {
                "attDuration": 422,
                "isPassed": False,
                "randomFaceCaptureTimeList": [363],
            }
        ],
        "defaults": {"reportUrl": "https://x/log", "reportTimeInterval": 60,
                     "playingCapture": 1},
    }
    limits = study.playback_limits(marg)
    assert limits["duration"] == 422
    assert limits["report_interval"] == 60
    assert limits["face_capture"] is True
    assert limits["face_times"] == [363]
    assert limits["is_passed"] is False


def test_module_kind() -> None:
    assert study.module_kind("insertvideo", ".mp4") == "video"
    assert study.module_kind("insertdoc", ".pdf") == "document"
    assert study.module_kind("work", "workid") == "quiz"
    assert study.module_kind("insertimage", ".png") == "image"
    assert study.module_kind("weird", "?") == "unknown"


def test_taskpoint_label_and_flags() -> None:
    task = study.TaskPoint(
        chapter_id="1", section_title="1.1 基本概念", index_text="1.1",
        kind="video", name="1.1.mp4", duration=144,
    )
    assert task.is_video and not task.is_document and not task.is_quiz
    assert "2 分 24 秒" in task.label()
    assert task.key.startswith("1:")


def test_study_report_summary() -> None:
    task = study.TaskPoint(chapter_id="1", section_title="s", index_text="1.1", kind="video")
    report = study.StudyReport(course_id="c")
    report.add(study.StepResult(task, "done"))
    report.add(study.StepResult(task, "partial"))
    report.add(study.StepResult(task, "done"))
    report.finished_at = report.started_at + 60
    summary = report.summary()
    assert "共 3 个任务点" in summary
    assert "完成 2" in summary
    assert "部分 1" in summary
    assert report.done == 2
