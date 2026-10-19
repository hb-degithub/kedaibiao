"""领域模型测试：归一化与指纹是题库的根，必须锁死行为。"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from kedaibiao.core.models import (
    Assignment,
    Credentials,
    Question,
    QuestionType,
    normalize_question,
    question_fingerprint,
)


class TestNormalize(unittest.TestCase):
    def test_fullwidth_to_halfwidth(self) -> None:
        self.assertEqual(normalize_question("ＡＢＣ１２３"), "abc123")

    def test_strip_whitespace(self) -> None:
        self.assertEqual(normalize_question(" 下列  哪项\n正确 "), "下列哪项正确")

    def test_strip_chinese_punctuation(self) -> None:
        self.assertEqual(normalize_question("下列说法，正确的是？"), "下列说法正确的是")

    def test_lowercase_ascii(self) -> None:
        self.assertEqual(normalize_question("HTTP Status"), "httpstatus")

    def test_empty(self) -> None:
        self.assertEqual(normalize_question(""), "")
        self.assertEqual(normalize_question("   ，。！"), "")

    def test_equivalent_phrasings_collide(self) -> None:
        a = normalize_question("下列 哪项，正确？")
        b = normalize_question("下列哪项正确")
        self.assertEqual(a, b)


class TestFingerprint(unittest.TestCase):
    def test_option_order_does_not_matter(self) -> None:
        f1 = question_fingerprint("题干", ["A选项", "B选项"])
        f2 = question_fingerprint("题干", ["B选项", "A选项"])
        self.assertEqual(f1, f2)

    def test_stem_change_changes_fingerprint(self) -> None:
        self.assertNotEqual(question_fingerprint("题干一"), question_fingerprint("题干二"))

    def test_punctuation_variance_does_not_matter(self) -> None:
        f1 = question_fingerprint("下列哪项正确？", ["甲", "乙"])
        f2 = question_fingerprint("下列哪项正确", ["甲", "乙"])
        self.assertEqual(f1, f2)

    def test_question_property_matches_function(self) -> None:
        q = Question(stem="题干", options=["甲", "乙"])
        self.assertEqual(q.fingerprint, question_fingerprint("题干", ["甲", "乙"]))


class TestCredentials(unittest.TestCase):
    def test_repr_hides_password(self) -> None:
        creds = Credentials(username="u1", password="s3cr3t")
        self.assertIn("u1", repr(creds))
        self.assertNotIn("s3cr3t", repr(creds))

    def test_repr_hides_password_in_container(self) -> None:
        creds = Credentials(username="u1", password="s3cr3t")
        self.assertNotIn("s3cr3t", str([creds]))


class TestAssignment(unittest.TestCase):
    def test_overdue(self) -> None:
        past = Assignment(remote_id="1", title="t", deadline=datetime.now() - timedelta(hours=1))
        future = Assignment(remote_id="2", title="t", deadline=datetime.now() + timedelta(hours=1))
        self.assertTrue(past.is_overdue)
        self.assertFalse(future.is_overdue)

    def test_no_deadline_is_not_overdue(self) -> None:
        self.assertFalse(Assignment(remote_id="3", title="t").is_overdue)


class TestQuestionDefaults(unittest.TestCase):
    def test_defaults(self) -> None:
        q = Question(stem="s")
        self.assertIs(q.type, QuestionType.UNKNOWN)
        self.assertEqual(q.options, [])
        self.assertEqual(q.raw, {})


if __name__ == "__main__":
    unittest.main()
