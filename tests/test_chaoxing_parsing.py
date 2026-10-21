"""超星/学习通 页面解析与「速览」脚本的离线测试。

全部用固定 HTML 片段，不联网、不启浏览器 —— 解析器的价值在宽容度，
而宽容度只能用「已知的那些版式」反复喂来验。
"""

from __future__ import annotations

import unittest
from datetime import datetime
from pathlib import Path

from kedaibiao.core.models import TaskKind
from kedaibiao.platforms.chaoxing import client, endpoints, parsing

ROOT = Path(__file__).resolve().parents[1]

COURSES_HTML = """
<div class="course-list">
  <div class="course-item">
    <a class="course" href="https://mooc2-ans.chaoxing.com/mooc2-ans/mycourse/stu?courseid=231234567&clazzid=123456&enc=abc123&cpi=987654">
      <img src="cover.jpg">
      <span class="course-name">高等数学（上）</span>
    </a>
    <a class="course" href="https://mooc2-ans.chaoxing.com/mooc2-ans/mycourse/stu?courseid=231234567&clazzid=123456&enc=abc123&cpi=987654">高等数学（上）</a>
  </div>
  <div class="course-item">
    <a href="/mycourse/stu?courseid=7654321&clazzid=654321&enc=def456&cpi=111222">
      <span>大学英语</span>
    </a>
  </div>
  <a href="/notice">通知公告</a>
</div>
"""

WORKS_HTML = """
<ul class="ulDiv">
  <li>
    <div class="titTxt"><a href="https://mooc1.chaoxing.com/mooc2/work/doHomeWorkNew?courseId=1&workId=555&enc=x">第一次作业</a></div>
    <p class="time">发布于 2026-10-01 截止 2026-10-30 23:59</p>
    <p class="status">未交</p>
  </li>
  <li>
    <div class="titTxt"><a href="https://mooc1.chaoxing.com/exam/test?examId=99">期中考试</a></div>
    <p class="time">2026-11-05 14:00</p>
  </li>
  <li>
    <div class="titTxt"><a href="https://mooc1.chaoxing.com/mooc2/work/doHomeWorkNew?workId=777">第二次作业</a></div>
    <p class="time">2026-10-10 23:59</p>
    <p class="status">已完成</p>
  </li>
  <li><a href="/nav">首页</a></li>
</ul>
"""

LOGIN_HTML = '<html><head><title>用户登录</title></head><body>请输入密码 <a href="https://passport2.chaoxing.com/login">登录</a></body></html>'


class TestParseCourses(unittest.TestCase):
    def test_extracts_courses(self) -> None:
        courses = parsing.parse_courses(COURSES_HTML)
        self.assertEqual([c.name for c in courses], ["高等数学（上）", "大学英语"])

    def test_dedupes_cover_and_title_anchors(self) -> None:
        courses = parsing.parse_courses(COURSES_HTML)
        self.assertEqual(len(courses), 2)

    def test_remote_id_and_params(self) -> None:
        course = parsing.parse_courses(COURSES_HTML)[0]
        self.assertEqual(course.remote_id, "231234567:123456")
        self.assertEqual(course.raw["courseid"], "231234567")
        self.assertEqual(course.raw["clazzid"], "123456")
        self.assertEqual(course.raw["enc"], "abc123")

    def test_relative_href_is_supported(self) -> None:
        course = parsing.parse_courses(COURSES_HTML)[1]
        self.assertEqual(course.raw["courseid"], "7654321")

    def test_ignores_links_without_courseid(self) -> None:
        names = [c.name for c in parsing.parse_courses(COURSES_HTML)]
        self.assertNotIn("通知公告", names)

    def test_empty_html(self) -> None:
        self.assertEqual(parsing.parse_courses(""), [])


class TestParseWorks(unittest.TestCase):
    def test_counts_only_work_rows(self) -> None:
        works = parsing.parse_works(WORKS_HTML, course_name="高等数学（上）")
        self.assertEqual([w.title for w in works], ["第一次作业", "期中考试", "第二次作业"])

    def test_kind_detection(self) -> None:
        works = parsing.parse_works(WORKS_HTML)
        self.assertEqual(works[0].kind, TaskKind.HOMEWORK)
        self.assertEqual(works[1].kind, TaskKind.EXAM)

    def test_last_date_wins_as_deadline(self) -> None:
        works = parsing.parse_works(WORKS_HTML)
        self.assertEqual(works[0].deadline, datetime(2026, 10, 30, 23, 59))

    def test_date_without_time(self) -> None:
        works = parsing.parse_works(WORKS_HTML)
        self.assertEqual(works[1].deadline, datetime(2026, 11, 5, 14, 0))

    def test_finished_flag(self) -> None:
        works = parsing.parse_works(WORKS_HTML)
        self.assertFalse(works[0].finished)
        self.assertTrue(works[2].finished)

    def test_work_id_from_query(self) -> None:
        works = parsing.parse_works(WORKS_HTML)
        self.assertEqual(works[0].remote_id, "555")
        self.assertEqual(works[1].remote_id, "99")

    def test_course_name_propagates(self) -> None:
        works = parsing.parse_works(WORKS_HTML, course_name="高等数学（上）")
        self.assertTrue(all(w.course_name == "高等数学（上）" for w in works))

    def test_empty_page(self) -> None:
        self.assertEqual(parsing.parse_works("<html><body>没有作业</body></html>"), [])


class TestHelpers(unittest.TestCase):
    def test_looks_like_login(self) -> None:
        self.assertTrue(parsing.looks_like_login(LOGIN_HTML, endpoints.LOGIN_MARKERS))
        self.assertFalse(parsing.looks_like_login(COURSES_HTML, endpoints.LOGIN_MARKERS))

    def test_summarise(self) -> None:
        summary = parsing.summarise(LOGIN_HTML)
        self.assertEqual(summary["title"], "用户登录")
        self.assertIn("请输入密码", summary["text"])

    def test_clean_collapses_whitespace(self) -> None:
        self.assertEqual(parsing.clean("  作业 \n\t 一  "), "作业 一")


class TestCandidateFallback(unittest.TestCase):
    """入口失效时要换下一条，不能停在第一条上解读成「没有课」。"""

    def test_falls_back_when_first_is_login_page(self) -> None:
        def fake_fetch(url: str, cookie: str, timeout: float):
            if url == endpoints.COURSE_LIST_URLS[0]:
                return 200, LOGIN_HTML
            return 200, COURSES_HTML

        original = client.fetch
        client.fetch = fake_fetch
        try:
            courses, notes = client.load_courses("fid=1", 1.0)
        finally:
            client.fetch = original

        self.assertEqual([c.name for c in courses], ["高等数学（上）", "大学英语"])
        self.assertTrue(notes[0].endswith("登录态无效（返回登录页）"))

    def test_returns_empty_when_all_fail(self) -> None:
        original = client.fetch
        client.fetch = lambda url, cookie, timeout: (0, "<network-error>down")
        try:
            courses, notes = client.load_courses("fid=1", 1.0)
        finally:
            client.fetch = original

        self.assertEqual(courses, [])
        self.assertEqual(len(notes), len(endpoints.COURSE_LIST_URLS))

    def test_works_url_is_built_from_course_params(self) -> None:
        seen: list[str] = []

        def fake_fetch(url: str, cookie: str, timeout: float):
            seen.append(url)
            return 200, WORKS_HTML

        original = client.fetch
        client.fetch = fake_fetch
        try:
            course = parsing.parse_courses(COURSES_HTML)[0]
            works, note = client.load_works(course, "fid=1", 1.0)
        finally:
            client.fetch = original

        self.assertEqual(len(works), 3)
        self.assertEqual(note, "")
        self.assertIn("courseId=231234567", seen[0])
        self.assertIn("classId=123456", seen[0])
        self.assertIn("cpi=987654", seen[0])

    def test_collect_shape(self) -> None:
        original_fetch = client.fetch
        client.fetch = lambda url, cookie, timeout: (200, COURSES_HTML)
        try:
            payload = client.collect("fid=1", timeout=1.0, include_works=False)
        finally:
            client.fetch = original_fetch

        self.assertTrue(payload["ok"])
        self.assertEqual(len(payload["courses"]), 2)
        self.assertEqual(payload["works"], [])


if __name__ == "__main__":
    unittest.main()
