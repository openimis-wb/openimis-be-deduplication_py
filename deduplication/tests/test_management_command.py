from io import StringIO

from django.core.management import call_command
from django.db import connection
from django.test import TestCase

from deduplication.models import DuplicateCandidate
from deduplication.tests.data.dedup_candidates import individuals_data
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
