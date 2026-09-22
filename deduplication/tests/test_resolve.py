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
