from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import TestCase

from deduplication.models import DuplicateCandidate
from deduplication.tests.data.dedup_candidates import individuals_data
from deduplication.tests.test_candidates import _BrokenSource, _PairSource
from deduplication.tests.helpers import LogInHelper
from individual.models import Individual


class ScanDuplicatesCommandTest(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = LogInHelper().get_or_create_user_api()
        for data in individuals_data:
            Individual(**data).save(username=cls.user.username)

    def test_scan_duplicates_command_records_candidates(self):
        if connection.vendor == 'microsoft':
            self.skipTest("This test can only be executed for PSQL database")

        out = StringIO()
        call_command('scan_duplicates', '--kind', 'demographic', '--user', self.user.username, stdout=out)

        self.assertIn('demographic', out.getvalue())
        self.assertTrue(DuplicateCandidate.objects.filter(kind='demographic').exists())

    def test_scan_duplicates_command_fails_when_a_source_fails(self):
        out = StringIO()
        with mock.patch("deduplication.services.registered_sources",
                        return_value=[_BrokenSource(), _PairSource()]):
            with self.assertRaises(CommandError) as raised:
                call_command('scan_duplicates', '--user', self.user.username, stdout=out)

        self.assertIn('broken', str(raised.exception))
        self.assertIn('pairs: 1 candidate(s) recorded', out.getvalue())
        self.assertIn('broken', out.getvalue())
        self.assertTrue(DuplicateCandidate.objects.filter(kind='pairs').exists())
