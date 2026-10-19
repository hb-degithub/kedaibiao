"""认证、角色、账号与**数据隔离**测试。

隔离是这个项目最不能出错的地方：普通用户之间串台等于把别人账号里的作业、
作文、题库全暴露出去。所以这里既测存储层也测服务层的越权路径。
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from kedaibiao.api.service import ConsoleError, ConsoleService, Viewer
from kedaibiao.bank.store import ProgressStore, QuestionBank
from kedaibiao.core.models import (
    Answer,
    AnswerSource,
    Question,
    RunRecord,
    RunStatus,
    SubmissionResult,
)
from kedaibiao.security.accounts import AccountError, AccountStore
from kedaibiao.security.auth import (
    ROLE_SUPERADMIN,
    ROLE_USER,
    AuthError,
    AuthStore,
    hash_password,
    validate_password,
    verify_password,
)


class TestPassword(unittest.TestCase):
    def test_roundtrip(self) -> None:
        stored = hash_password("correct horse")
        self.assertTrue(verify_password("correct horse", stored))
        self.assertFalse(verify_password("wrong horse", stored))

    def test_salt_is_random(self) -> None:
        self.assertNotEqual(hash_password("same"), hash_password("same"))

    def test_format(self) -> None:
        stored = hash_password("abcdefgh")
        self.assertTrue(stored.startswith("scrypt$"))
        self.assertEqual(len(stored.split("$")), 6)

    def test_malformed_stored_returns_false(self) -> None:
        for bad in ("", "garbage", "scrypt$1$2", "md5$$$$$", "scrypt$x$y$z$q$w"):
            self.assertFalse(verify_password("x", bad), bad)

    def test_empty_password_rejected(self) -> None:
        with self.assertRaises(ValueError):
            hash_password("")

    def test_min_length(self) -> None:
        with self.assertRaises(AuthError):
            validate_password("short")
        validate_password("longenough")


class AuthStoreTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.store = AuthStore(":memory:")
        self.addCleanup(self.store.close)


class TestAuthStore(AuthStoreTestBase):
    def test_create_and_fetch(self) -> None:
        user = self.store.create_user("alice", "password123")
        self.assertEqual(user.username, "alice")
        self.assertEqual(user.role, ROLE_USER)
        self.assertIsNotNone(self.store.get_user_by_name("alice"))
        self.assertIsNotNone(self.store.get_user_by_id(user.id))

    def test_duplicate_username(self) -> None:
        self.store.create_user("alice", "password123")
        with self.assertRaises(AuthError) as cm:
            self.store.create_user("alice", "password123")
        self.assertEqual(cm.exception.status, 409)

    def test_blank_username(self) -> None:
        with self.assertRaises(AuthError):
            self.store.create_user("  ", "password123")

    def test_bad_role(self) -> None:
        with self.assertRaises(AuthError):
            self.store.create_user("bob", "password123", role="root")

    def test_verify_login(self) -> None:
        self.store.create_user("alice", "password123")
        self.assertIsNotNone(self.store.verify_login("alice", "password123"))
        self.assertIsNone(self.store.verify_login("alice", "nope"))
        self.assertIsNone(self.store.verify_login("nobody", "password123"))

    def test_disabled_cannot_login(self) -> None:
        user = self.store.create_user("alice", "password123")
        self.store.set_disabled(user.id, True)
        self.assertIsNone(self.store.verify_login("alice", "password123"))

    def test_public_has_no_hash(self) -> None:
        user = self.store.create_user("alice", "password123")
        self.assertNotIn("password", repr(user.public()))
        self.assertNotIn("scrypt", repr(user.public()))

    def test_set_password_invalidates_sessions(self) -> None:
        user = self.store.create_user("alice", "password123")
        self.store.create_session(user.id)
        self.assertEqual(self.store.session_count(), 1)
        self.store.set_password(user.id, "newpassword")
        self.assertEqual(self.store.session_count(), 0)

    def test_rename_conflict(self) -> None:
        a = self.store.create_user("alice", "password123")
        self.store.create_user("bob", "password123")
        with self.assertRaises(AuthError):
            self.store.rename_user(a.id, "bob")

    def test_cannot_delete_last_superadmin(self) -> None:
        admin = self.store.create_user("admin", "password123", role=ROLE_SUPERADMIN)
        self.store.create_user("alice", "password123")
        # 有两个超管时可以删
        other = self.store.create_user("admin2", "password123", role=ROLE_SUPERADMIN)
        self.store.delete_user(admin.id)
        # 只剩 admin2 时不能删
        with self.assertRaises(AuthError):
            self.store.delete_user(other.id)

    def test_bootstrap_creates_one_admin_only(self) -> None:
        user, generated = self.store.ensure_bootstrap_admin()
        assert user is not None and generated
        self.assertEqual(user.role, ROLE_SUPERADMIN)
        again, again_pw = self.store.ensure_bootstrap_admin()
        self.assertIsNone(again)
        self.assertIsNone(again_pw)
        self.assertEqual(self.store.count_users(), 1)


class TestSessions(AuthStoreTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.user = self.store.create_user("alice", "password123")

    def test_create_and_lookup(self) -> None:
        session = self.store.create_session(self.user.id)
        found = self.store.get_session(session.token)
        assert found is not None
        self.assertEqual(found.user_id, self.user.id)

    def test_unknown_token(self) -> None:
        self.assertIsNone(self.store.get_session("nope"))
        self.assertIsNone(self.store.get_session(""))

    def test_expired_session_rejected_and_purged(self) -> None:
        session = self.store.create_session(self.user.id, ttl=timedelta(seconds=-1))
        self.assertIsNone(self.store.get_session(session.token))
        self.assertEqual(self.store.session_count(), 0)

    def test_logout(self) -> None:
        session = self.store.create_session(self.user.id)
        self.store.delete_session(session.token)
        self.assertIsNone(self.store.get_session(session.token))

    def test_disabling_user_kills_sessions(self) -> None:
        self.store.create_session(self.user.id)
        self.store.set_disabled(self.user.id, True)
        self.assertEqual(self.store.session_count(), 0)


class TestAccountStore(unittest.TestCase):
    def setUp(self) -> None:
        self.store = AccountStore(":memory:")
        self.addCleanup(self.store.close)

    def test_add_and_list(self) -> None:
        acc = self.store.add(1, "pigai", label="我的号", username="u1")
        self.assertEqual(acc.label, "我的号")
        self.assertEqual(len(self.store.list_for(1, "pigai")), 1)
        self.assertEqual(self.store.list_for(2), [], "别人的账号看不到")

    def test_first_account_becomes_active(self) -> None:
        acc = self.store.add(1, "pigai", username="u1")
        self.assertTrue(self.store.get(acc.id).is_active)
        active = self.store.active_for(1, "pigai")
        assert active is not None
        self.assertEqual(active.id, acc.id)

    def test_switch_active(self) -> None:
        a1 = self.store.add(1, "pigai", username="u1")
        a2 = self.store.add(1, "pigai", username="u2")
        self.store.set_active(1, "pigai", a2.id)
        active = self.store.active_for(1, "pigai")
        assert active is not None
        self.assertEqual(active.id, a2.id)
        self.assertFalse(self.store.get(a1.id).is_active)

    def test_cannot_activate_others_account(self) -> None:
        acc = self.store.add(1, "pigai", username="u1")
        with self.assertRaises(AccountError):
            self.store.set_active(2, "pigai", acc.id)

    def test_cookie_paths_are_distinct(self) -> None:
        a1 = self.store.add(1, "pigai", username="u1")
        a2 = self.store.add(1, "pigai", username="u2")
        self.assertNotEqual(
            a1.cookie_path("/tmp/s"), a2.cookie_path("/tmp/s"),
            "两个号的 cookie 必须分开存，否则切号等于没切",
        )

    def test_record_login(self) -> None:
        acc = self.store.add(1, "pigai", username="u1")
        self.store.record_login(acc.id, ok=False, note="密码不对")
        fresh = self.store.get(acc.id)
        assert fresh is not None
        self.assertFalse(fresh.last_login_ok)
        self.assertEqual(fresh.last_login_note, "密码不对")
        self.assertTrue(fresh.last_login_at)

    def test_delete(self) -> None:
        acc = self.store.add(1, "pigai", username="u1")
        self.store.delete(acc.id)
        self.assertIsNone(self.store.get(acc.id))
        self.assertIsNone(self.store.active_for(1, "pigai"))

    def test_public_has_no_secret(self) -> None:
        acc = self.store.add(1, "pigai", label="x", username="u1")
        blob = repr(acc.public())
        self.assertNotIn("password", blob)
        self.assertNotIn("cookie", blob)


class TestDataIsolation(unittest.TestCase):
    """**最关键的一组**：两个用户的数据必须互不可见。"""

    def setUp(self) -> None:
        self.bank = QuestionBank(":memory:")
        self.addCleanup(self.bank.close)
        self.q = Question(stem="同一道题", remote_id="q1")

    def test_same_question_isolated_per_owner(self) -> None:
        self.bank.upsert(self.q, Answer(question=self.q, choices=[0],
                                        source=AnswerSource.BANK_HIT), owner_id=1)
        self.bank.upsert(self.q, Answer(question=self.q, choices=[1],
                                        source=AnswerSource.BANK_HIT), owner_id=2)
        a1 = self.bank.lookup(self.q, owner_id=1)
        a2 = self.bank.lookup(self.q, owner_id=2)
        assert a1 is not None and a2 is not None
        self.assertEqual(a1.choices, [0])
        self.assertEqual(a2.choices, [1], "两个用户的答案不能互相覆盖")

    def test_user_cannot_see_others_bank(self) -> None:
        self.bank.upsert(self.q, Answer(question=self.q, choices=[0],
                                        source=AnswerSource.BANK_HIT), owner_id=1)
        self.assertIsNone(self.bank.lookup(self.q, owner_id=2))
        self.assertEqual(self.bank.stats(owner_id=2)["total"], 0)
        self.assertEqual(self.bank.stats(owner_id=1)["total"], 1)

    def test_other_owner_does_not_increment_hits(self) -> None:
        self.bank.upsert(self.q, Answer(question=self.q, choices=[0],
                                        source=AnswerSource.BANK_HIT), owner_id=1)
        self.bank.lookup(self.q, owner_id=2)
        row = self.bank.fetchone(
            "SELECT hits FROM questions WHERE owner_id=1 AND fingerprint=?",
            (self.q.fingerprint,),
        )
        self.assertEqual(row["hits"], 0, "别人查不能算到我头上")

    def test_runs_isolated(self) -> None:
        store = ProgressStore(":memory:")
        self.addCleanup(store.close)
        for owner in (1, 2):
            run_id = store.start_run(
                RunRecord(platform="p", assignment_id=f"a-{owner}"), owner_id=owner
            )
            store.finish_run(run_id, RunStatus.SUCCEEDED)
        self.assertEqual(len(store.recent_runs(owner_id=1)), 1)
        self.assertEqual(store.recent_runs(owner_id=1)[0]["assignment_id"], "a-1")
        self.assertEqual(len(store.recent_runs(owner_id=None)), 2, "超管看全部")

    def test_already_done_is_per_owner(self) -> None:
        store = ProgressStore(":memory:")
        self.addCleanup(store.close)
        run_id = store.start_run(RunRecord(platform="p", assignment_id="a1"), owner_id=1)
        store.finish_run(run_id, RunStatus.SUCCEEDED)
        self.assertTrue(store.already_done("p", "a1", owner_id=1))
        self.assertFalse(store.already_done("p", "a1", owner_id=2),
                         "别人跑过不该让我跳过")

    def test_submissions_isolated(self) -> None:
        store = ProgressStore(":memory:")
        self.addCleanup(store.close)
        store.record_submission("p", SubmissionResult(assignment_id="x", ok=True), owner_id=1)
        self.assertEqual(len(store.recent_submissions(owner_id=2)), 0)

    def test_claim_ownerless(self) -> None:
        self.bank.upsert(self.q, Answer(question=self.q, choices=[0],
                                        source=AnswerSource.BANK_HIT), owner_id=0)
        self.assertGreater(self.bank.ownerless_count(), 0)
        claimed = self.bank.claim_ownerless(7)
        self.assertEqual(claimed, 1)
        self.assertEqual(self.bank.stats(owner_id=7)["total"], 1)
        self.assertEqual(self.bank.ownerless_count(), 0)


class TestViewer(unittest.TestCase):
    def test_anonymous_passthrough(self) -> None:
        self.assertIsNone(Viewer().owner_filter(None))
        self.assertEqual(Viewer().owner_filter(7), 7)

    def test_user_pinned_to_self(self) -> None:
        viewer = Viewer(user_id=3)
        self.assertEqual(viewer.owner_filter(None), 3)
        self.assertEqual(viewer.owner_filter(3), 3)
        with self.assertRaises(ConsoleError) as cm:
            viewer.owner_filter(9)
        self.assertEqual(cm.exception.status, 403)

    def test_superadmin_can_pick(self) -> None:
        viewer = Viewer(user_id=1, is_superadmin=True)
        self.assertIsNone(viewer.owner_filter(None))
        self.assertEqual(viewer.owner_filter(9), 9)

    def test_superadmin_required(self) -> None:
        Viewer(user_id=1, is_superadmin=True).require_superadmin()  # 不抛
        with self.assertRaises(ConsoleError):
            Viewer(user_id=1).require_superadmin()


class TestServicePermission(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "config").mkdir()
        (self.root / "config" / "local.toml").write_text(
            "[kedaibiao]\n"
            f'db_url = "sqlite:///{self.root}/x.db"\n'
            f'session_dir = "{self.root}/s"\n',
            encoding="utf-8",
        )
        self.service = ConsoleService(root=self.root)

    def test_non_superadmin_cannot_list_users(self) -> None:
        with self.assertRaises(ConsoleError) as cm:
            self.service.users(Viewer(user_id=2))
        self.assertEqual(cm.exception.status, 403)

    def test_superadmin_can_list_users(self) -> None:
        _user, generated = self.service.bootstrap_admin()
        assert generated
        admin = Viewer(user_id=_user["id"], is_superadmin=True)
        data = self.service.users(admin)
        self.assertEqual(len(data["users"]), 1)
        self.assertNotIn("scrypt", repr(data))

    def test_bootstrap_claims_legacy_data(self) -> None:
        """升级前 owner_id=0 的数据必须认领给首个超管，否则升级后「数据全没了」。"""
        bank = QuestionBank(self.root / "x.db")
        q = Question(stem="老数据", remote_id="q")
        bank.upsert(q, Answer(question=q, choices=[0], source=AnswerSource.BANK_HIT))
        bank.close()

        user, _pw = self.service.bootstrap_admin()
        assert user is not None
        self.assertEqual(self.service.bank(owner_id=user["id"])["total"], 1)

    def test_login_wrong_password(self) -> None:
        self.service.bootstrap_admin()
        with self.assertRaises(ConsoleError) as cm:
            self.service.login("admin", "wrong")
        self.assertEqual(cm.exception.kind, "unauthorized")

    def test_account_ownership(self) -> None:
        store = AccountStore(self.root / "x.db")
        acc = store.add(1, "pigai", username="u1")
        store.close()
        with self.assertRaises(ConsoleError) as cm:
            self.service._require_account(Viewer(user_id=2), acc.id)  # noqa: SLF001
        self.assertEqual(cm.exception.status, 403)
        # 超管可以
        self.service._require_account(  # noqa: SLF001
            Viewer(user_id=9, is_superadmin=True), acc.id
        )


if __name__ == "__main__":
    unittest.main()
