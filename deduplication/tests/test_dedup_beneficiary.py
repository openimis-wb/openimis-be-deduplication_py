from django.test import TestCase
from django.db import connection
from deduplication.services import get_beneficiary_duplication_aggregation
from deduplication.tests.data.dedup_beneficiary import benefit_plan_data, individuals_data
from deduplication.tests.helpers import LogInHelper
from individual.models import Individual
from social_protection.models import Beneficiary, BenefitPlan, BeneficiaryStatus


class DedupBeneficiaryTestCase(TestCase):
    user = None
    bp = None
    inds = None
    benfs = None

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = LogInHelper().get_or_create_user_api()
        cls.bp = BenefitPlan(**benefit_plan_data)
        cls.bp.save(username=cls.user.username)
        cls.inds = list()
        cls.bens = list()

        for i_data in individuals_data:
            i = Individual(**i_data)
            i.save(username=cls.user.username)
            cls.inds.append(i)

        for i in cls.inds:
            b = Beneficiary(
                individual=i,
                benefit_plan=cls.bp,
                status=BeneficiaryStatus.ACTIVE,
                json_ext=i.json_ext
            )
            b.save(username=cls.user.username)
            cls.bens.append(b)

    def test_deduplication_aggregation(self):
        if connection.vendor == 'microsoft':
            self.skipTest("This test can only be executed for PSQL database")
        else:
            res = get_beneficiary_duplication_aggregation(['individual__first_name', 'k1'], self.bp.id, self.user)
            listed = list(res)
            self.assertEqual(len(listed), 1)
            response = listed[0]
            self.assertEqual(response['id_count'], 2)
            self.assertEqual(response['individual__first_name'], 'first name 1')
