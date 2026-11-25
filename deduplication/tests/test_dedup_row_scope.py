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
from tasks_management.models import Task, TaskExecutor, TaskGroup

OUT_OF_SCOPE_BENEFICIARIES = "Beneficiaries outside the districts of your account cannot be reviewed"
OUT_OF_SCOPE_PAYMENTS = "Payments outside the districts of your account cannot be reviewed"


@override_settings(ROW_SECURITY=True)
class DeduplicationScopeFixture(TestCase):
    """
    Two duplicate pairs: one in district A, one in district B, plus a single
    district A record ("other") in no pair. The district account is limited to
    district A and executes the review tasks; the admin account sees both.
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

        task_group = TaskGroup(
            code="DEDUPSCOPE", completion_policy=TaskGroup.TaskGroupCompletionPolicy.ANY,
            json_ext={"task_sources": [
                CreateDeduplicationReviewTasksService.get_class_name(),
                CreateDeduplicationPaymentReviewTasksService.get_class_name(),
            ]},
        )
        task_group.save(user=cls.admin)
        TaskExecutor(user=cls.district_user, task_group=task_group).save(user=cls.admin)

        cls.beneficiaries = {"A": [], "B": [], "other": []}
        cls.benefits = {"A": [], "B": [], "other": []}
        for district, village, first_name, count in (
                ("A", village_a, "Dupont", 2), ("B", village_b, "Nguema", 2), ("other", village_a, "Martin", 1)):
            for _ in range(count):
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


class DeduplicationRowScopeTestCase(DeduplicationScopeFixture):
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


def _id_forms(value):
    """Non-canonical spellings of a UUID that the ORM still resolves to the same row."""
    canonical = str(value)
    return {
        "upper": canonical.upper(),
        "no_hyphen": canonical.replace("-", ""),
        "braces": "{" + canonical + "}",
    }


class DeduplicationIdFormsTestCase(DeduplicationScopeFixture):
    def _summary(self, ids):
        return [{"count": len(ids), "ids": ids, "column_values": {"first_name": "x"}}]

    def _forms(self, rows):
        forms = {}
        for row in rows:
            for name, value in _id_forms(row.id).items():
                forms.setdefault(name, []).append(value)
        return forms

    def test_review_tasks_refuse_other_district_ids_in_any_form(self):
        for form, ids in self._forms(self.beneficiaries["B"]).items():
            with self.subTest(form=form):
                errors = CreateDeduplicationReviewMutation.async_mutate(self.district_user, summary=self._summary(ids))

                self.assertIn(OUT_OF_SCOPE_BENEFICIARIES, str(errors))
                self.assertFalse(Task.objects.exists())

    def test_payment_review_tasks_refuse_other_district_ids_in_any_form(self):
        for form, ids in self._forms(self.benefits["B"]).items():
            with self.subTest(form=form):
                errors = CreateDeduplicationPaymentReviewMutation.async_mutate(
                    self.district_user, summary=self._summary(ids), payment_cycle=str(self.payment_cycle.id),
                )

                self.assertIn(OUT_OF_SCOPE_PAYMENTS, str(errors))
                self.assertFalse(Task.objects.exists())
                for benefit in self.benefits["B"]:
                    benefit.refresh_from_db()
                    self.assertNotIn("duplicated", benefit.json_ext or {})

    def test_serializers_refuse_other_district_ids_in_any_form(self):
        for form, ids in self._forms(self.beneficiaries["B"]).items():
            with self.subTest(form=form), self.assertRaisesMessage(PermissionDenied, OUT_OF_SCOPE_BENEFICIARIES):
                CreateDeduplicationReviewTasksService(self.district_user) \
                    .create_beneficiary_duplication_task_serializer({"ids": ids, "count": 2})
        for form, ids in self._forms(self.benefits["B"]).items():
            with self.subTest(form=form), self.assertRaisesMessage(PermissionDenied, OUT_OF_SCOPE_PAYMENTS):
                CreateDeduplicationPaymentReviewTasksService(self.district_user) \
                    .create_payment_benefit_duplication_task_serializer({"ids": ids, "count": 2})

    def test_review_tasks_store_canonical_ids(self):
        ids = self._forms(self.beneficiaries["A"])["upper"]

        errors = CreateDeduplicationReviewMutation.async_mutate(self.district_user, summary=self._summary(ids))

        self.assertIsNone(errors)
        self.assertEqual(Task.objects.get().data["ids"], [str(b.id) for b in self.beneficiaries["A"]])

    def test_second_review_task_on_the_same_rows_is_refused_in_any_form(self):
        canonical = [str(b.id) for b in self.beneficiaries["A"]]
        self.assertIsNone(
            CreateDeduplicationReviewMutation.async_mutate(self.district_user, summary=self._summary(canonical))
        )
        for form, ids in self._forms(self.beneficiaries["A"]).items():
            with self.subTest(form=form):
                errors = CreateDeduplicationReviewMutation.async_mutate(
                    self.district_user, summary=self._summary(ids),
                )

                self.assertIn("beneficiary_in_task", str(errors))
                self.assertEqual(Task.objects.count(), 1)

    def test_second_payment_review_task_on_the_same_rows_is_refused_in_any_form(self):
        canonical = [str(b.id) for b in self.benefits["A"]]
        self.assertIsNone(CreateDeduplicationPaymentReviewMutation.async_mutate(
            self.district_user, summary=self._summary(canonical), payment_cycle=str(self.payment_cycle.id),
        ))
        for form, ids in self._forms(self.benefits["A"]).items():
            with self.subTest(form=form):
                errors = CreateDeduplicationPaymentReviewMutation.async_mutate(
                    self.district_user, summary=self._summary(ids), payment_cycle=str(self.payment_cycle.id),
                )

                self.assertIn("please_solve_the_created_tasks_first", str(errors))
                self.assertEqual(Task.objects.count(), 1)

    def test_unparseable_id_is_refused(self):
        errors = CreateDeduplicationReviewMutation.async_mutate(
            self.district_user, summary=self._summary([str(self.beneficiaries["A"][0].id), "not-a-uuid"]),
        )

        self.assertIn("Invalid record id", str(errors))
        self.assertFalse(Task.objects.exists())


NOT_IN_TASK_BENEFICIARIES = "Beneficiaries that are not part of this review task cannot be merged"
NOT_IN_TASK_PAYMENTS = "Payments that are not part of this review task cannot be removed"


class DeduplicationResolutionScopeTestCase(DeduplicationScopeFixture):
    """The district account resolves review tasks it executes."""

    def _beneficiary_task(self, user=None, rows=None):
        rows = rows or self.beneficiaries["A"]
        result = CreateDeduplicationReviewTasksService(user or self.district_user).create_beneficiary_duplication_tasks(
            [{"count": len(rows), "ids": [str(b.id) for b in rows], "column_values": {"first_name": "x"}}]
        )
        self.assertTrue(result["success"], result)
        return Task.objects.get(source=CreateDeduplicationReviewTasksService.get_class_name())

    def _payment_task(self, user=None, rows=None):
        rows = rows or self.benefits["A"]
        result = CreateDeduplicationPaymentReviewTasksService(user or self.district_user) \
            .create_payment_benefit_duplication_tasks(
                [{"count": len(rows), "ids": [str(b.id) for b in rows], "column_values": {"first_name": "x"}}],
                str(self.payment_cycle.id),
            )
        self.assertTrue(result["success"], result)
        return Task.objects.get(source=CreateDeduplicationPaymentReviewTasksService.get_class_name())

    def _resolve(self, task, additional_data):
        from tasks_management.services import TaskService
        return TaskService(self.district_user).resolve_task({
            "id": str(task.id),
            "business_status": {str(self.district_user.id): "APPROVED"},
            "additional_data": additional_data,
        })

    def _assert_unresolved(self, task):
        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.ACCEPTED)
        self.assertNotIn("additional_resolve_data", task.json_ext or {})

    def _assert_no_beneficiary_deleted(self):
        rows = self.beneficiaries["A"] + self.beneficiaries["B"] + self.beneficiaries["other"]
        self.assertFalse(Beneficiary.objects.filter(id__in=[b.id for b in rows], is_deleted=True).exists())

    def _assert_no_benefit_deleted(self):
        rows = self.benefits["A"] + self.benefits["B"] + self.benefits["other"]
        self.assertEqual(BenefitConsumption.objects.filter(id__in=[b.id for b in rows]).count(), len(rows))

    def test_in_scope_beneficiary_resolution_merges(self):
        task = self._beneficiary_task()

        result = self._resolve(task, {"beneficiaryIds": [str(b.id) for b in self.beneficiaries["A"]], "values": {}})

        self.assertTrue(result["success"], result)
        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.COMPLETED)
        self.assertEqual(
            Beneficiary.objects.filter(id__in=[b.id for b in self.beneficiaries["A"]], is_deleted=True).count(), 1,
        )

    def test_beneficiary_resolution_refuses_other_district_ids(self):
        task = self._beneficiary_task(self.admin, self.beneficiaries["A"] + self.beneficiaries["B"])
        submitted = [str(self.beneficiaries["A"][0].id), _id_forms(self.beneficiaries["B"][0].id)["upper"]]

        with self.assertRaisesMessage(PermissionDenied, OUT_OF_SCOPE_BENEFICIARIES):
            self._resolve(task, {"beneficiaryIds": submitted, "values": {}})

        self._assert_unresolved(task)
        self._assert_no_beneficiary_deleted()

    def test_beneficiary_resolution_refuses_ids_outside_the_task(self):
        task = self._beneficiary_task()
        submitted = [str(self.beneficiaries["A"][0].id), str(self.beneficiaries["other"][0].id)]

        with self.assertRaisesMessage(PermissionDenied, NOT_IN_TASK_BENEFICIARIES):
            self._resolve(task, {"beneficiaryIds": submitted, "values": {}})

        self._assert_unresolved(task)
        self._assert_no_beneficiary_deleted()

    def test_resolve_task_mutation_reports_the_refusal(self):
        from tasks_management.gql_mutations import ResolveTaskMutation
        task = self._beneficiary_task(self.admin)
        submitted = [str(self.beneficiaries["A"][0].id), str(self.beneficiaries["other"][0].id)]

        errors = ResolveTaskMutation.async_mutate(
            self.admin, id=str(task.id), business_status={str(self.admin.id): "APPROVED"},
            additional_data={"beneficiaryIds": submitted, "values": {}},
        )

        self.assertIn(NOT_IN_TASK_BENEFICIARIES, errors[0]["detail"])
        self._assert_unresolved(task)
        self._assert_no_beneficiary_deleted()

    def test_in_scope_payment_resolution_removes(self):
        task = self._payment_task()

        result = self._resolve(task, {"benefitIds": [str(self.benefits["A"][1].id)]})

        self.assertTrue(result["success"], result)
        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.COMPLETED)
        self.assertFalse(BenefitConsumption.objects.filter(id=self.benefits["A"][1].id).exists())
        self.assertTrue(BenefitConsumption.objects.filter(id=self.benefits["A"][0].id).exists())

    def test_payment_resolution_refuses_other_district_and_outside_task_ids(self):
        task = self._payment_task(self.admin, self.benefits["A"] + self.benefits["B"])
        for submitted, message in (
                ([_id_forms(self.benefits["B"][0].id)["no_hyphen"]], OUT_OF_SCOPE_PAYMENTS),
                ([str(self.benefits["other"][0].id)], NOT_IN_TASK_PAYMENTS)):
            with self.subTest(submitted=submitted), self.assertRaisesMessage(PermissionDenied, message):
                self._resolve(task, {"benefitIds": submitted})

            self._assert_unresolved(task)
            self._assert_no_benefit_deleted()

    def test_completion_refuses_stored_resolution_outside_the_resolver_districts(self):
        """Resolution data stored without the resolve check is checked again before the task completes."""
        from tasks_management.services import TaskService
        task = Task.objects.get(id=self._beneficiary_task().id)
        task.data = {**task.data, "ids": task.data["ids"] + [str(self.beneficiaries["B"][0].id)]}
        task.json_ext = {"additional_resolve_data": {str(self.admin.id): {
            "beneficiaryIds": [str(self.beneficiaries["A"][0].id), str(self.beneficiaries["B"][0].id)],
            "values": {},
        }}}
        task.save(user=self.admin)

        with self.assertRaisesMessage(PermissionDenied, OUT_OF_SCOPE_BENEFICIARIES):
            TaskService(self.district_user).complete_task({"id": str(task.id)})

        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.ACCEPTED)
        self._assert_no_beneficiary_deleted()

    def test_resolution_completing_on_a_refused_stored_entry_leaves_the_task_open(self):
        """
        tasks_management swallows the completion refusal inside resolve_task: the
        resolution itself succeeds, the task stays open and nothing is merged.
        """
        task = self._beneficiary_task(self.admin, self.beneficiaries["A"] + self.beneficiaries["B"])
        task.json_ext = {"additional_resolve_data": {str(self.admin.id): {
            "beneficiaryIds": [str(b.id) for b in self.beneficiaries["B"]], "values": {},
        }}}
        task.save(user=self.admin)

        result = self._resolve(task, None)

        self.assertTrue(result["success"], result)
        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.ACCEPTED)
        self._assert_no_beneficiary_deleted()

    def test_merge_and_removal_refuse_ids_outside_the_task_or_the_user_districts(self):
        from deduplication.services import merge_duplicate_beneficiaries, remove_duplicate_benefit_payments
        beneficiary_task = {
            "data": {"ids": [str(b.id) for b in self.beneficiaries["A"] + self.beneficiaries["B"]]},
            "json_ext": {"additional_resolve_data": {"1": {
                "beneficiaryIds": [str(b.id) for b in self.beneficiaries["B"]], "values": {},
            }}},
        }
        payment_task = {
            "data": {"ids": [str(b.id) for b in self.benefits["A"]]},
            "json_ext": {"additional_resolve_data": {"1": {"benefitIds": [str(self.benefits["other"][0].id)]}}},
        }

        with self.assertRaisesMessage(PermissionDenied, OUT_OF_SCOPE_BENEFICIARIES):
            merge_duplicate_beneficiaries(beneficiary_task, self.district_user.id)
        with self.assertRaisesMessage(PermissionDenied, NOT_IN_TASK_PAYMENTS):
            remove_duplicate_benefit_payments(payment_task, self.district_user.id)

        self._assert_no_beneficiary_deleted()
        self._assert_no_benefit_deleted()
