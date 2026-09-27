from django.test import TestCase

from deduplication.models import DuplicateCandidate
from deduplication.services import resolve
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
