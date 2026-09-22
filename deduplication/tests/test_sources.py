from django.db import connection
from django.test import TestCase

from deduplication.sources import order_pair, sources
from deduplication.sources.demographic import DemographicSource
from deduplication.sources.identifier import IdentifierSource
from deduplication.tests.data.dedup_candidates import individuals_data
from deduplication.tests.helpers import LogInHelper, override_deduplication_config
from individual.models import Individual


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

        with override_deduplication_config(identifier_keys=["national_id"]):
            source = IdentifierSource()
            candidates = list(source.scan(None))

        pairs = {(c.subject_a, c.subject_b) for c in candidates}
        expected = order_pair(str(self.inds[0].id), str(self.inds[1].id))
        self.assertIn(expected, pairs)
        for c in candidates:
            self.assertEqual(c.kind, "identifier")
            self.assertEqual(c.evidence["columns"]["national_id"], "ab-1234")
