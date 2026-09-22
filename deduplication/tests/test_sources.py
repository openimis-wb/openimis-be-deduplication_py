from django.core.exceptions import ImproperlyConfigured
from django.db import connection
from django.test import TestCase

from deduplication.sources import order_pair, sources
from deduplication.sources.demographic import DemographicSource
from deduplication.sources.identifier import IdentifierSource
from deduplication.tests.data.dedup_candidates import individuals_data
from deduplication.tests.helpers import LogInHelper, override_deduplication_config
from individual.models import Individual

# For IDENTIFIER_MATCH="all": a and b equal on both keys; c shares national_id
# with a/b but has no passport_no at all, so it must never match in "all" mode.
all_mode_individuals_data = [
    {
        'first_name': 'Fatou',
        'last_name': 'Diallo',
        'dob': '1990-01-15',
        'json_ext': {'national_id': ' EF-5678 ', 'passport_no': ' P0001 '},
    },
    {
        'first_name': 'Fatou',
        'last_name': 'Diallo',
        'dob': '1990-01-15',
        'json_ext': {'national_id': 'ef-5678', 'passport_no': 'p0001'},
    },
    {
        'first_name': 'Fatou',
        'last_name': 'Diallo',
        'dob': '1990-01-15',
        'json_ext': {'national_id': 'ef-5678'},
    },
]


class OrderPairTest(TestCase):
    def test_order_pair_sorts_strings(self):
        self.assertEqual(order_pair("b", "a"), ("a", "b"))
        self.assertEqual(order_pair("a", "b"), ("a", "b"))


class SourceRegistryTest(TestCase):
    def test_builtin_sources_registered(self):
        kinds = {s.kind for s in sources()}
        self.assertIn("demographic", kinds)
        self.assertIn("identifier", kinds)


class DemographicSourceTest(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = LogInHelper().get_or_create_user_api()
        cls.inds = []
        for data in individuals_data:
            i = Individual(**data)
            i.save(username=cls.user.username)
            cls.inds.append(i)

    def test_demographic_source_groups_on_configured_columns(self):
        if connection.vendor == 'microsoft':
            self.skipTest("This test can only be executed for PSQL database")

        with override_deduplication_config(demographic_columns=["first_name", "last_name", "dob"]):
            source = DemographicSource()
            candidates = list(source.scan(None))

        pairs = {(c.subject_a, c.subject_b) for c in candidates}
        expected = order_pair(str(self.inds[0].id), str(self.inds[1].id))
        self.assertIn(expected, pairs)
        for c in candidates:
            self.assertEqual(c.kind, "demographic")
            self.assertIsNone(c.score)
            self.assertIn("columns", c.evidence)

    def test_demographic_source_watermark_is_max_of_subject_table(self):
        with override_deduplication_config(demographic_columns=["first_name", "last_name", "dob"]):
            source = DemographicSource()
            watermark = source.watermark()

        latest = Individual.objects.order_by('-date_updated', '-id').first()
        self.assertEqual(watermark.updated_at, latest.date_updated)
        self.assertEqual(watermark.last_id, str(latest.id))


class IdentifierSourceTest(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = LogInHelper().get_or_create_user_api()
        cls.inds = []
        for data in individuals_data:
            i = Individual(**data)
            i.save(username=cls.user.username)
            cls.inds.append(i)

    def test_identifier_source_normalises_before_matching(self):
        if connection.vendor == 'microsoft':
            self.skipTest("This test can only be executed for PSQL database")

        # IDENTIFIER_MATCH="each" is the default and the pre-existing behaviour: pinned
        # explicitly so a change to the default can't silently change this test's meaning.
        with override_deduplication_config(identifier_keys=["national_id"], identifier_match="each"):
            source = IdentifierSource()
            candidates = list(source.scan(None))

        pairs = {(c.subject_a, c.subject_b) for c in candidates}
        expected = order_pair(str(self.inds[0].id), str(self.inds[1].id))
        self.assertIn(expected, pairs)
        for c in candidates:
            self.assertEqual(c.kind, "identifier")
            self.assertEqual(c.evidence["columns"]["national_id"], "ab-1234")

    def test_identifier_source_invalid_match_mode_raises(self):
        with override_deduplication_config(identifier_keys=["national_id"], identifier_match="bogus"):
            source = IdentifierSource()
            with self.assertRaises(ImproperlyConfigured):
                list(source.scan(None))

    def test_identifier_source_empty_string_match_mode_raises(self):
        # "" is falsy but distinct from unset (None); it must not silently fall back to "each".
        with override_deduplication_config(identifier_keys=["national_id"], identifier_match=""):
            source = IdentifierSource()
            with self.assertRaises(ImproperlyConfigured):
                list(source.scan(None))


class IdentifierSourceAllModeTest(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = LogInHelper().get_or_create_user_api()
        cls.inds = []
        for data in all_mode_individuals_data:
            i = Individual(**data)
            i.save(username=cls.user.username)
            cls.inds.append(i)

    def test_all_mode_matches_only_fully_equal_subjects(self):
        if connection.vendor == 'microsoft':
            self.skipTest("This test can only be executed for PSQL database")

        keys = ["national_id", "passport_no"]
        with override_deduplication_config(identifier_keys=keys, identifier_match="all"):
            source = IdentifierSource()
            candidates = list(source.scan(None))

        pairs = {(c.subject_a, c.subject_b) for c in candidates}
        expected = order_pair(str(self.inds[0].id), str(self.inds[1].id))
        self.assertIn(expected, pairs)
        third = str(self.inds[2].id)
        self.assertNotIn(order_pair(str(self.inds[0].id), third), pairs)
        self.assertNotIn(order_pair(str(self.inds[1].id), third), pairs)
        for c in candidates:
            self.assertEqual(c.kind, "identifier")
            self.assertIsNone(c.score)
            self.assertEqual(c.evidence["columns"], {"national_id": "ef-5678", "passport_no": "p0001"})

    def test_all_mode_skips_subject_missing_a_key(self):
        if connection.vendor == 'microsoft':
            self.skipTest("This test can only be executed for PSQL database")

        keys = ["national_id", "passport_no"]
        with override_deduplication_config(identifier_keys=keys, identifier_match="all"):
            source = IdentifierSource()
            candidates = list(source.scan(None))

        touched_ids = {c.subject_a for c in candidates} | {c.subject_b for c in candidates}
        self.assertNotIn(str(self.inds[2].id), touched_ids)
