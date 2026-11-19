import types
from decimal import Decimal

import graphene
from django.test import TestCase

from core.test_helpers import create_test_interactive_user
from deduplication.schema import Query
from individual.models import Individual
from payment_cycle.models import PaymentCycle
from payroll.models import (
    BenefitConsumption, BenefitConsumptionStatus, Payroll, PayrollBenefitConsumption, PayrollStatus,
)
from social_protection.models import Beneficiary, BeneficiaryStatus, BenefitPlan


class DeduplicationColumnsTestCase(TestCase):
    password = "EdfmD3!12@#"
    schema = graphene.Schema(query=Query)

    @classmethod
    def setUpTestData(cls):
        cls.requester = create_test_interactive_user(username="dedup_requester", password=cls.password)
        cls.other_user = create_test_interactive_user(username="dedup_other_user", password=cls.password)
        cls.other_user.i_user.set_password(cls.password, private_key="c3" * 64)
        cls.other_user.i_user.save()

        cls.benefit_plan = BenefitPlan(
            code="DEDUPCOL", name="DEDUPCOLS", max_beneficiaries=10, ceiling_per_beneficiary=Decimal("1.00"),
            type=BenefitPlan.BenefitPlanType.INDIVIDUAL_TYPE,
            beneficiary_data_schema={"properties": {"household_code": {"type": "string"}}},
        )
        cls.benefit_plan.save(user=cls.other_user)
        cls.payment_cycle = PaymentCycle(code="DEDUPCOLS", start_date="2026-01-01", end_date="2026-01-31")
        cls.payment_cycle.save(user=cls.other_user)
        payroll = Payroll(name="DEDUPCOLS", payment_cycle=cls.payment_cycle, status=PayrollStatus.PENDING_APPROVAL)
        payroll.save(user=cls.other_user)

        for _ in range(2):
            individual = Individual(
                first_name="Dupont", last_name="Jean", dob="1980-01-01", json_ext={"household_code": "H-1"},
            )
            individual.save(user=cls.other_user)
            Beneficiary(
                individual=individual, benefit_plan=cls.benefit_plan, status=BeneficiaryStatus.ACTIVE,
                json_ext={"household_code": "H-1"},
            ).save(user=cls.other_user)
            benefit = BenefitConsumption(
                individual=individual, code="DEDUPCOLS", amount=Decimal("1.00"),
                status=BenefitConsumptionStatus.ACCEPTED,
            )
            benefit.save(user=cls.other_user)
            PayrollBenefitConsumption(payroll=payroll, benefit=benefit).save(user=cls.other_user)

    def _beneficiary_summary(self, columns):
        query = """
            query ($columns: [String]!, $benefitPlanId: UUID!) {
                beneficiaryDeduplicationSummary(columns: $columns, benefitPlanId: $benefitPlanId) {
                    rows { columnValues count }
                }
            }
        """
        return self.schema.execute(
            query,
            variables={"columns": columns, "benefitPlanId": str(self.benefit_plan.id)},
            context_value=types.SimpleNamespace(user=self.requester),
        )

    def _benefit_summary(self, columns):
        query = """
            query ($columns: [String]!, $paymentCycleId: ID) {
                benefitDeduplicationSummary(columns: $columns, paymentCycleId: $paymentCycleId) {
                    rows { columnValues count }
                }
            }
        """
        return self.schema.execute(
            query,
            variables={"columns": columns, "paymentCycleId": str(self.payment_cycle.id)},
            context_value=types.SimpleNamespace(user=self.requester),
        )

    def _assert_refused(self, result):
        secrets = (self.other_user.i_user.private_key, self.other_user.i_user.password)
        self.assertFalse(
            any(secret in str(result.data) for secret in secrets),
            "the summary carries another user's credentials",
        )
        self.assertIsNotNone(result.errors)
        self.assertIn("cannot be used for deduplication", str(result.errors[0]))

    def test_beneficiary_summary_refuses_related_paths(self):
        for column in ("user_created__i_user__private_key", "user_created__i_user__password", "benefit_plan"):
            with self.subTest(column=column):
                self._assert_refused(self._beneficiary_summary(["first_name", column]))

    def test_benefit_summary_refuses_related_paths(self):
        for column in ("user_created__i_user__private_key", "individual__user_created__i_user__password", "location"):
            with self.subTest(column=column):
                self._assert_refused(self._benefit_summary(["first_name", column]))

    def test_summaries_refuse_relations_and_non_identifiers(self):
        for column in ("user_created", "individual", "household-code", "", "a__b"):
            with self.subTest(column=column):
                beneficiary = self._beneficiary_summary(["first_name", column])
                benefit = self._benefit_summary(["first_name", column])
                self.assertIsNotNone(beneficiary.errors)
                self.assertIsNotNone(benefit.errors)
                self.assertIn("cannot be used for deduplication", str(beneficiary.errors[0]))
                self.assertIn("cannot be used for deduplication", str(benefit.errors[0]))

    def test_beneficiary_summary_groups_on_fe_columns(self):
        result = self._beneficiary_summary(["first_name", "last_name", "dob", "household_code"])

        self.assertIsNone(result.errors)
        rows = result.data["beneficiaryDeduplicationSummary"]["rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["count"], 2)
        self.assertIn("Dupont", rows[0]["columnValues"])
        self.assertIn("H-1", rows[0]["columnValues"])

    def test_benefit_summary_groups_on_individual_columns(self):
        result = self._benefit_summary(["first_name", "last_name", "dob", "household_code"])

        self.assertIsNone(result.errors)
        rows = result.data["benefitDeduplicationSummary"]["rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["count"], 2)
        self.assertIn("Dupont", rows[0]["columnValues"])
