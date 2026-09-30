from unittest import mock

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from deduplication.models import DuplicateCandidate
from deduplication.services import merge_subjects, resolve
from deduplication.sources import order_pair
from deduplication.tests.helpers import LogInHelper
from individual.models import Individual


class ResolveTest(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = LogInHelper().get_or_create_user_api()

    def _make_pair(self):
        a = Individual(first_name="A", last_name="One", dob="1990-01-01")
        a.save(username=self.user.username)
        b = Individual(first_name="B", last_name="Two", dob="1990-01-01")
        b.save(username=self.user.username)
        subject_a, subject_b = order_pair(str(a.id), str(b.id))
        candidate = DuplicateCandidate.objects.create(
            subject_model="individual.Individual", subject_a=subject_a, subject_b=subject_b,
            kind="demographic", source="TestSource", evidence={},
        )
        return a, b, candidate

    def test_resolve_different_dismisses_without_merging(self):
        a, b, candidate = self._make_pair()

        resolve(candidate, decision="different", actor=self.user, note="not a duplicate")

        candidate.refresh_from_db()
        self.assertEqual(candidate.status, DuplicateCandidate.Status.DISMISSED)
        self.assertEqual(candidate.decision_note, "not a duplicate")
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertFalse(a.is_deleted)
        self.assertFalse(b.is_deleted)

    def test_resolve_same_confirms_and_merges(self):
        a, b, candidate = self._make_pair()

        resolve(candidate, decision="same", keep=str(a.id), actor=self.user)

        candidate.refresh_from_db()
        self.assertEqual(candidate.status, DuplicateCandidate.Status.CONFIRMED)
        self.assertEqual(candidate.reviewed_by, self.user.username)
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertFalse(a.is_deleted)
        self.assertTrue(b.is_deleted)

    def test_resolve_never_reopens_a_dismissed_candidate(self):
        a, b, candidate = self._make_pair()
        candidate.status = DuplicateCandidate.Status.DISMISSED
        candidate.save()

        resolve(candidate, decision="same", keep=str(a.id), actor=self.user)

        candidate.refresh_from_db()
        self.assertEqual(candidate.status, DuplicateCandidate.Status.DISMISSED)
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertFalse(a.is_deleted)
        self.assertFalse(b.is_deleted)

    def test_resolve_leaves_a_confirmed_candidate_unchanged(self):
        a, b, candidate = self._make_pair()
        resolve(candidate, decision="same", keep=str(a.id), actor=self.user, note="first decision")

        resolve(candidate, decision="same", keep=str(b.id), actor=self.user, note="stale decision")

        candidate.refresh_from_db()
        self.assertEqual(candidate.status, DuplicateCandidate.Status.CONFIRMED)
        self.assertEqual(candidate.decision_note, "first decision")
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertFalse(a.is_deleted)
        self.assertTrue(b.is_deleted)

    def test_resolve_never_dismisses_a_confirmed_candidate(self):
        a, b, candidate = self._make_pair()
        resolve(candidate, decision="same", keep=str(a.id), actor=self.user)

        resolve(candidate, decision="different", actor=self.user, note="stale decision")

        candidate.refresh_from_db()
        self.assertEqual(candidate.status, DuplicateCandidate.Status.CONFIRMED)
        self.assertEqual(candidate.decision_note, "")

    def test_resolve_reads_the_current_status_not_the_passed_instance(self):
        a, b, candidate = self._make_pair()
        stale = DuplicateCandidate.objects.get(id=candidate.id)
        resolve(candidate, decision="same", keep=str(a.id), actor=self.user)

        resolve(stale, decision="same", keep=str(b.id), actor=self.user)

        a.refresh_from_db()
        b.refresh_from_db()
        self.assertFalse(a.is_deleted)
        self.assertTrue(b.is_deleted)

    def _make_outsider(self):
        x = Individual(first_name="X", last_name="Outside", dob="1985-05-05")
        x.save(username=self.user.username)
        return x

    def test_resolve_refuses_a_keep_outside_the_pair(self):
        a, b, candidate = self._make_pair()
        x = self._make_outsider()

        with self.assertRaisesMessage(ValueError, "deduplication.resolve.keep_not_in_pair"):
            resolve(candidate, decision="same", keep=str(x.id), actor=self.user)

        candidate.refresh_from_db()
        self.assertEqual(candidate.status, DuplicateCandidate.Status.OPEN)
        self.assertEqual(candidate.reviewed_by, "")
        for subject in (a, b, x):
            subject.refresh_from_db()
            self.assertFalse(subject.is_deleted)
        self.assertNotIn('merge_conflicts', x.json_ext or {})

    def test_resolve_refuses_a_soft_deleted_kept_subject(self):
        a, b, candidate = self._make_pair()
        a.delete(user=self.user)

        with self.assertRaisesMessage(ValueError, "deduplication.resolve.subject_deleted"):
            resolve(candidate, decision="same", keep=str(a.id), actor=self.user)

        candidate.refresh_from_db()
        self.assertEqual(candidate.status, DuplicateCandidate.Status.OPEN)
        b.refresh_from_db()
        self.assertFalse(b.is_deleted)

    def test_resolve_refuses_a_soft_deleted_retired_subject(self):
        a, b, candidate = self._make_pair()
        b.delete(user=self.user)

        with self.assertRaisesMessage(ValueError, "deduplication.resolve.subject_deleted"):
            resolve(candidate, decision="same", keep=str(a.id), actor=self.user)

        candidate.refresh_from_db()
        self.assertEqual(candidate.status, DuplicateCandidate.Status.OPEN)
        a.refresh_from_db()
        self.assertFalse(a.is_deleted)


class ResolveSameSubjectsAcrossKindsTest(TestCase):
    """One pair proposed by several sources yields one candidate per kind; a merge settles them all."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = LogInHelper().get_or_create_user_api()

    def _make_subjects(self):
        a = Individual(first_name="A", last_name="One", dob="1990-01-01")
        a.save(username=self.user.username)
        b = Individual(first_name="B", last_name="Two", dob="1990-01-01")
        b.save(username=self.user.username)
        return a, b

    def _candidate(self, a, b, kind):
        subject_a, subject_b = order_pair(str(a.id), str(b.id))
        return DuplicateCandidate.objects.create(
            subject_model="individual.Individual", subject_a=subject_a, subject_b=subject_b,
            kind=kind, source="TestSource", evidence={},
        )

    def test_confirming_one_kind_confirms_the_other_open_kinds_of_the_pair(self):
        a, b = self._make_subjects()
        demographic = self._candidate(a, b, "demographic")
        identifier = self._candidate(a, b, "identifier")
        other_pair = self._candidate(a, self._make_subjects()[1], "demographic")

        with mock.patch("deduplication.services.merge_subjects", wraps=merge_subjects) as merge:
            resolve(demographic, decision="same", keep=str(a.id), actor=self.user, note="same person")

        self.assertEqual(merge.call_count, 1)
        identifier.refresh_from_db()
        self.assertEqual(identifier.status, DuplicateCandidate.Status.CONFIRMED)
        self.assertEqual(identifier.reviewed_by, self.user.username)
        self.assertIsNotNone(identifier.reviewed_at)
        other_pair.refresh_from_db()
        self.assertEqual(other_pair.status, DuplicateCandidate.Status.OPEN)
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertFalse(a.is_deleted)
        self.assertTrue(b.is_deleted)

    def test_confirming_leaves_a_dismissed_kind_of_the_pair_dismissed(self):
        a, b = self._make_subjects()
        demographic = self._candidate(a, b, "demographic")
        identifier = self._candidate(a, b, "identifier")
        identifier.status = DuplicateCandidate.Status.DISMISSED
        identifier.save()

        resolve(demographic, decision="same", keep=str(a.id), actor=self.user)

        identifier.refresh_from_db()
        self.assertEqual(identifier.status, DuplicateCandidate.Status.DISMISSED)

    def test_same_on_a_kind_whose_pair_is_already_merged_confirms_without_merging(self):
        a, b = self._make_subjects()
        resolve(self._candidate(a, b, "demographic"), decision="same", keep=str(a.id), actor=self.user)
        late = self._candidate(a, b, "biometric")

        with mock.patch("deduplication.services.merge_subjects") as merge:
            resolve(late, decision="same", keep=str(a.id), actor=self.user, note="second source")

        merge.assert_not_called()
        late.refresh_from_db()
        self.assertEqual(late.status, DuplicateCandidate.Status.CONFIRMED)
        self.assertEqual(late.decision_note, "second source")
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertFalse(a.is_deleted)
        self.assertTrue(b.is_deleted)

    def test_same_on_a_merged_pair_with_the_other_keep_is_refused(self):
        a, b = self._make_subjects()
        resolve(self._candidate(a, b, "demographic"), decision="same", keep=str(a.id), actor=self.user)
        late = self._candidate(a, b, "biometric")

        with self.assertRaisesMessage(ValueError, "deduplication.resolve.keep_contradicts_merge"):
            resolve(late, decision="same", keep=str(b.id), actor=self.user)

        late.refresh_from_db()
        self.assertEqual(late.status, DuplicateCandidate.Status.OPEN)
        a.refresh_from_db()
        self.assertFalse(a.is_deleted)

    def test_same_on_a_merged_pair_whose_kept_subject_is_gone_is_refused(self):
        a, b = self._make_subjects()
        resolve(self._candidate(a, b, "demographic"), decision="same", keep=str(a.id), actor=self.user)
        a.delete(user=self.user)
        late = self._candidate(a, b, "biometric")

        with self.assertRaisesMessage(ValueError, "deduplication.resolve.subject_deleted"):
            resolve(late, decision="same", keep=str(a.id), actor=self.user)

        late.refresh_from_db()
        self.assertEqual(late.status, DuplicateCandidate.Status.OPEN)

    def test_different_on_a_kind_whose_pair_is_already_merged_is_refused(self):
        a, b = self._make_subjects()
        resolve(self._candidate(a, b, "demographic"), decision="same", keep=str(a.id), actor=self.user)
        late = self._candidate(a, b, "biometric")

        with self.assertRaisesMessage(ValueError, "deduplication.resolve.pair_already_merged"):
            resolve(late, decision="different", actor=self.user, note="late dismissal")

        late.refresh_from_db()
        self.assertEqual(late.status, DuplicateCandidate.Status.OPEN)
        self.assertEqual(late.reviewed_by, "")
        self.assertEqual(late.decision_note, "")

    def test_different_is_still_allowed_next_to_a_dismissed_kind(self):
        a, b = self._make_subjects()
        resolve(self._candidate(a, b, "demographic"), decision="different", actor=self.user)
        other = self._candidate(a, b, "identifier")

        resolve(other, decision="different", actor=self.user)

        other.refresh_from_db()
        self.assertEqual(other.status, DuplicateCandidate.Status.DISMISSED)


class ResolveLocksSubjectsTest(TestCase):
    """Both subject rows are locked, in sorted id order, before the candidate row and any check."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = LogInHelper().get_or_create_user_api()

    def _pair(self):
        a = Individual(first_name="Lock", last_name="One", dob="1990-01-01")
        a.save(username=self.user.username)
        b = Individual(first_name="Lock", last_name="Two", dob="1990-01-01")
        b.save(username=self.user.username)
        low, high = order_pair(str(a.id), str(b.id))
        candidate = DuplicateCandidate.objects.create(
            subject_model="individual.Individual", subject_a=low, subject_b=high,
            kind="demographic", source="TestSource", evidence={},
        )
        return low, high, candidate

    def _locks(self, queries):
        subject_table = Individual._meta.db_table
        candidate_table = DuplicateCandidate._meta.db_table
        locks = []
        for query in queries:
            sql = query["sql"]
            if "FOR UPDATE" not in sql:
                continue
            if f'FROM "{subject_table}"' in sql:
                locks.append(("subject", sql))
            elif f'FROM "{candidate_table}"' in sql:
                locks.append(("candidate", sql))
        return locks

    def _assert_subjects_locked_first_in_order(self, low, high, queries):
        locks = self._locks(queries)
        self.assertGreaterEqual(len(locks), 3, locks)
        self.assertEqual([kind for kind, _sql in locks[:3]], ["subject", "subject", "candidate"])
        self.assertIn(low, locks[0][1])
        self.assertIn(high, locks[1][1])
        statements = [q["sql"] for q in queries if not q["sql"].startswith(("SAVEPOINT", "RELEASE SAVEPOINT"))]
        self.assertIn("FOR UPDATE", statements[0], "a query ran before the first subject lock")

    def test_same_locks_both_subjects_in_sorted_order_first(self):
        low, high, candidate = self._pair()

        with CaptureQueriesContext(connection) as ctx:
            resolve(candidate, decision="same", keep=high, actor=self.user)

        self._assert_subjects_locked_first_in_order(low, high, ctx.captured_queries)

    def test_different_locks_both_subjects_in_sorted_order_first(self):
        low, high, candidate = self._pair()

        with CaptureQueriesContext(connection) as ctx:
            resolve(candidate, decision="different", actor=self.user)

        self._assert_subjects_locked_first_in_order(low, high, ctx.captured_queries)
