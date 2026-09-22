from django.db import connection
from django.test import TestCase

from deduplication.models import DuplicateCandidate
from deduplication.services import record_candidate, run_scan, scan_subject
from deduplication.sources import Candidate
from deduplication.tests.data.dedup_candidates import individuals_data
from deduplication.tests.helpers import LogInHelper
from individual.models import Individual


class RecordCandidateTest(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = LogInHelper().get_or_create_user_api()

    def test_record_candidate_idempotent_and_merges_evidence(self):
        c1 = Candidate(
            subject_model="individual.Individual", subject_a="a", subject_b="b",
            kind="demographic", score=0.5, evidence={"x": 1},
        )
        obj, created = record_candidate(c1, source="TestSource")
        self.assertTrue(created)
        self.assertEqual(obj.score, 0.5)

        c2 = Candidate(
            subject_model="individual.Individual", subject_a="b", subject_b="a",
            kind="demographic", score=0.9, evidence={"y": 2},
        )
        obj2, created2 = record_candidate(c2, source="TestSource")
        self.assertFalse(created2)
        self.assertEqual(obj2.id, obj.id)
        self.assertEqual(obj2.score, 0.9)
        self.assertEqual(obj2.evidence, {"x": 1, "y": 2})

    def test_record_candidate_never_reopens_dismissed(self):
        c1 = Candidate(
            subject_model="individual.Individual", subject_a="c", subject_b="d",
            kind="identifier", score=None, evidence={},
        )
        obj, _created = record_candidate(c1, source="TestSource")
        obj.status = DuplicateCandidate.Status.DISMISSED
        obj.save()

        obj2, created2 = record_candidate(c1, source="TestSource")
        self.assertFalse(created2)
        self.assertEqual(obj2.status, DuplicateCandidate.Status.DISMISSED)


class RunScanTest(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = LogInHelper().get_or_create_user_api()
        for data in individuals_data:
            Individual(**data).save(username=cls.user.username)

    def test_run_scan_advances_watermark_and_second_run_is_noop(self):
        if connection.vendor == 'microsoft':
            self.skipTest("This test can only be executed for PSQL database")

        first = run_scan(kinds=["demographic"], actor=self.user)
        self.assertGreater(first.get("demographic", 0), 0)
        self.assertTrue(DuplicateCandidate.objects.filter(kind="demographic").exists())

        second = run_scan(kinds=["demographic"], actor=self.user)
        self.assertEqual(second.get("demographic", 0), 0)


class ScanSubjectTest(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = LogInHelper().get_or_create_user_api()
        cls.inds = []
        for data in individuals_data:
            i = Individual(**data)
            i.save(username=cls.user.username)
            cls.inds.append(i)

    def test_scan_subject_records_only_pairs_touching_subject(self):
        if connection.vendor == 'microsoft':
            self.skipTest("This test can only be executed for PSQL database")

        results = scan_subject("individual.Individual", str(self.inds[0].id))
        self.assertTrue(results)
        for candidate in results:
            self.assertIn(str(self.inds[0].id), (candidate.subject_a, candidate.subject_b))
