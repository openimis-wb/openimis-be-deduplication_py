import json
import types
from decimal import Decimal

import graphene
from django.core.exceptions import PermissionDenied
from django.test import TestCase, override_settings

from core.test_helpers import create_test_interactive_user, create_test_role
from deduplication.gql_mutations import CreateDeduplicationReviewMutation, CreateDeduplicationPaymentReviewMutation
from deduplication.schema import Query
from deduplication.services import (
    CreateDeduplicationReviewTasksService, CreateDeduplicationPaymentReviewTasksService,
)
from individual.models import Individual
from location.test_helpers import create_test_village, assign_user_districts
from payment_cycle.models import PaymentCycle
from payroll.models import (
    BenefitConsumption, BenefitConsumptionStatus, Payroll, PayrollBenefitConsumption, PayrollStatus,
)
from social_protection.models import Beneficiary, BeneficiaryStatus, BenefitPlan
from tasks_management.models import Task

OUT_OF_SCOPE_BENEFICIARIES = "Beneficiaries outside the districts of your account cannot be reviewed"
OUT_OF_SCOPE_PAYMENTS = "Payments outside the districts of your account cannot be reviewed"


@override_settings(ROW_SECURITY=True)
class DeduplicationRowScopeTestCase(TestCase):
    """
    Two duplicate pairs: one in district A, one in district B. The district
    account is limited to district A; the admin account sees both.
    """
    password = "EdfmD3!12@#"
    schema = graphene.Schema(query=Query)
    columns = ["first_name", "last_name", "dob"]

    @classmethod
    def setUpTestData(cls):
        cls.admin = create_test_interactive_user(username="dedup_scope_admin", password=cls.password)
        role = create_test_role(
            ["gql_beneficiary_search_perms", "gql_create_deduplication_review_perms"],
            name="DedupScopeDistrictRole",
        )
        cls.district_user = create_test_interactive_user(
            username="dedup_scope_district", password=cls.password, roles=[role.id],
        )
        village_a = create_test_village({"code": "DDSCA", "name": "Dedup Scope A"})
        village_b = create_test_village({"code": "DDSCB", "name": "Dedup Scope B"})
        assign_user_districts(cls.district_user, [village_a.parent.parent.code])

        cls.benefit_plan = BenefitPlan(
            code="DEDUPSCP", name="DEDUPSCOPE", max_beneficiaries=10, ceiling_per_beneficiary=Decimal("1.00"),
            type=BenefitPlan.BenefitPlanType.INDIVIDUAL_TYPE, beneficiary_data_schema={"properties": {}},
        )
        cls.benefit_plan.save(user=cls.admin)
        cls.payment_cycle = PaymentCycle(code="DEDUPSCOPE", start_date="2026-01-01", end_date="2026-01-31")
        cls.payment_cycle.save(user=cls.admin)
        payroll = Payroll(name="DEDUPSCOPE", payment_cycle=cls.payment_cycle, status=PayrollStatus.PENDING_APPROVAL)
        payroll.save(user=cls.admin)

        cls.beneficiaries = {"A": [], "B": []}
        cls.benefits = {"A": [], "B": []}
        for district, village, first_name in (("A", village_a, "Dupont"), ("B", village_b, "Nguema")):
            for _ in range(2):
                individual = Individual(first_name=first_name, last_name="Jean", dob="1980-01-01", location=village)
                individual.save(user=cls.admin)
                beneficiary = Beneficiary(
                    individual=individual, benefit_plan=cls.benefit_plan, status=BeneficiaryStatus.ACTIVE,
                )
                beneficiary.save(user=cls.admin)
                cls.beneficiaries[district].append(beneficiary)
                benefit = BenefitConsumption(
                    individual=individual, code="DEDUPSCOPE", amount=Decimal("1.00"),
                    status=BenefitConsumptionStatus.ACCEPTED,
                )
                benefit.save(user=cls.admin)
                PayrollBenefitConsumption(payroll=payroll, benefit=benefit).save(user=cls.admin)
                cls.benefits[district].append(benefit)

    def _execute(self, query, variables, user):
        result = self.schema.execute(query, variables=variables, context_value=types.SimpleNamespace(user=user))
        self.assertIsNone(result.errors)
        return result.data

    def _beneficiary_names(self, user):
        query = """
            query ($columns: [String]!, $benefitPlanId: UUID!) {
                beneficiaryDeduplicationSummary(columns: $columns, benefitPlanId: $benefitPlanId) {
                    rows { columnValues count }
                }
            }
        """
        data = self._execute(query, {"columns": self.columns, "benefitPlanId": str(self.benefit_plan.id)}, user)
        return self._names(data["beneficiaryDeduplicationSummary"]["rows"], "individual__first_name")

    def _benefit_names(self, user):
        query = """
            query ($columns: [String]!, $paymentCycleId: ID) {
                benefitDeduplicationSummary(columns: $columns, paymentCycleId: $paymentCycleId) {
                    rows { columnValues count }
                }
            }
        """
        data = self._execute(query, {"columns": self.columns, "paymentCycleId": str(self.payment_cycle.id)}, user)
        return self._names(data["benefitDeduplicationSummary"]["rows"], "individual__first_name")

    @staticmethod
    def _names(rows, first_name_key):
        names = {}
        for row in rows:
            values = row["columnValues"]
            values = json.loads(values) if isinstance(values, str) else values
            names[values[first_name_key]] = row["count"]
        return names

    @staticmethod
    def _summary(rows):
        return [
            {"count": len(rows), "ids": [str(row.id) for row in rows], "column_values": {"first_name": "x"}}
        ]

    def test_district_account_beneficiary_summary_leaves_out_other_districts(self):
        self.assertEqual(self._beneficiary_names(self.district_user), {"Dupont": 2})

    def test_district_account_benefit_summary_leaves_out_other_districts(self):
        self.assertEqual(self._benefit_names(self.district_user), {"Dupont": 2})

    def test_admin_summaries_cover_every_district(self):
        self.assertEqual(self._beneficiary_names(self.admin), {"Dupont": 2, "Nguema": 2})
        self.assertEqual(self._benefit_names(self.admin), {"Dupont": 2, "Nguema": 2})

    def test_summaries_unscoped_when_row_security_is_off(self):
        with override_settings(ROW_SECURITY=False):
            self.assertEqual(self._beneficiary_names(self.district_user), {"Dupont": 2, "Nguema": 2})
            self.assertEqual(self._benefit_names(self.district_user), {"Dupont": 2, "Nguema": 2})

    def test_review_tasks_created_for_own_district(self):
        errors = CreateDeduplicationReviewMutation.async_mutate(
            self.district_user, summary=self._summary(self.beneficiaries["A"]),
        )

        self.assertIsNone(errors)
        self.assertEqual(Task.objects.filter(source=CreateDeduplicationReviewTasksService.get_class_name()).count(), 1)

    def test_review_tasks_refuse_ids_outside_the_account_districts(self):
        summary = self._summary(self.beneficiaries["A"]) + self._summary(self.beneficiaries["B"])

        errors = CreateDeduplicationReviewMutation.async_mutate(self.district_user, summary=summary)

        self.assertIn(OUT_OF_SCOPE_BENEFICIARIES, str(errors))
        self.assertIn(str(self.beneficiaries["B"][0].id), str(errors))
        self.assertNotIn(str(self.beneficiaries["A"][0].id), str(errors))
        self.assertFalse(Task.objects.filter(source=CreateDeduplicationReviewTasksService.get_class_name()).exists())

    def test_payment_review_tasks_refuse_ids_outside_the_account_districts(self):
        summary = self._summary(self.benefits["A"]) + self._summary(self.benefits["B"])

        errors = CreateDeduplicationPaymentReviewMutation.async_mutate(
            self.district_user, summary=summary, payment_cycle=str(self.payment_cycle.id),
        )

        self.assertIn(OUT_OF_SCOPE_PAYMENTS, str(errors))
        self.assertIn(str(self.benefits["B"][0].id), str(errors))
        self.assertFalse(
            Task.objects.filter(source=CreateDeduplicationPaymentReviewTasksService.get_class_name()).exists()
        )
        for benefit in self.benefits["A"] + self.benefits["B"]:
            benefit.refresh_from_db()
            self.assertNotIn("duplicated", benefit.json_ext or {})

    def test_review_task_serializers_refuse_ids_outside_the_account_districts(self):
        beneficiary_data = {"ids": [str(b.id) for b in self.beneficiaries["B"]], "count": 2}
        benefit_data = {"ids": [str(b.id) for b in self.benefits["B"]], "count": 2}

        with self.assertRaisesMessage(PermissionDenied, OUT_OF_SCOPE_BENEFICIARIES):
            CreateDeduplicationReviewTasksService(self.district_user) \
                .create_beneficiary_duplication_task_serializer(beneficiary_data)
        with self.assertRaisesMessage(PermissionDenied, OUT_OF_SCOPE_PAYMENTS):
            CreateDeduplicationPaymentReviewTasksService(self.district_user) \
                .create_payment_benefit_duplication_task_serializer(benefit_data)

        serialized = CreateDeduplicationReviewTasksService(self.admin) \
            .create_beneficiary_duplication_task_serializer(beneficiary_data)
        self.assertEqual(len(serialized["ids"]), 2)
