from unittest import mock

from django.db import connection
from django.test import TestCase

from deduplication.models import DuplicateCandidate
from deduplication.services import record_candidate, run_scan, scan_subject
from deduplication.sources import Candidate, CandidateSource, order_pair
from deduplication.sources.subject import subject_watermark, touched_ids
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


class _ConcurrentWriteSource(CandidateSource):
    """Proposes one pair per touched subject; writes a new subject while its first scan iterates."""

    kind = "probe"

    def __init__(self, username):
        self.username = username
        self.written = None

    def watermark(self):
        return subject_watermark()

    def scan(self, since):
        touched = touched_ids(Individual, since)
        ids = sorted(str(i) for i in Individual.objects.values_list("id", flat=True)
                     if touched is None or str(i) in touched)
        for subject_id in ids:
            a, b = order_pair(subject_id, "zzz-partner")
            yield Candidate(
                subject_model="individual.Individual", subject_a=a, subject_b=b,
                kind=self.kind, score=None, evidence={},
            )
            if self.written is None:
                self.written = Individual(first_name="Late", last_name="Writer", dob="2001-02-03")
                self.written.save(username=self.username)


class RunScanConcurrentWriteTest(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = LogInHelper().get_or_create_user_api()
        Individual(first_name="Early", last_name="Bird", dob="1999-01-01").save(username=cls.user.username)

    def test_subject_written_during_a_scan_is_seen_by_the_next_scan(self):
        source = _ConcurrentWriteSource(self.user.username)
        with mock.patch("deduplication.services.registered_sources", return_value=[source]):
            run_scan(kinds=["probe"], actor=self.user)
            self.assertIsNotNone(source.written)
            late_id = str(source.written.id)

            run_scan(kinds=["probe"], actor=self.user)

        pairs = DuplicateCandidate.objects.filter(kind="probe")
        self.assertTrue(
            any(late_id in (c.subject_a, c.subject_b) for c in pairs),
            "the subject written while the first scan iterated was never scanned",
        )


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
