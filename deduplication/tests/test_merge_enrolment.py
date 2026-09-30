from django.test import TestCase

from deduplication.models import DuplicateCandidate
from deduplication.services import merge_subjects, resolve
from deduplication.sources import order_pair
from deduplication.tests.helpers import LogInHelper
from individual.models import Group, GroupIndividual, Individual
from social_protection.models import Beneficiary, BeneficiaryStatus, BenefitPlan, GroupBeneficiary

ENROLLED = "deduplication.resolve.retired_subject_enrolled"


class RetiredSubjectEnrolmentTest(TestCase):
    """A subject that still holds enrolment rows is not retired: payroll would keep paying it."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = LogInHelper().get_or_create_user_api()

    def setUp(self):
        self.kept = Individual(first_name="Kept", last_name="One", dob="1990-01-01")
        self.kept.save(username=self.user.username)
        self.retired = Individual(first_name="Retired", last_name="Two", dob="1990-01-01")
        self.retired.save(username=self.user.username)
        subject_a, subject_b = order_pair(str(self.kept.id), str(self.retired.id))
        self.candidate = DuplicateCandidate.objects.create(
            subject_model="individual.Individual", subject_a=subject_a, subject_b=subject_b,
            kind="demographic", source="TestSource", evidence={},
        )

    def _plan(self, code, plan_type):
        plan = BenefitPlan(code=code, name=code, type=plan_type, beneficiary_data_schema={})
        plan.save(username=self.user.username)
        return plan

    def _enrol(self, individual):
        plan = self._plan("IND", BenefitPlan.BenefitPlanType.INDIVIDUAL_TYPE)
        beneficiary = Beneficiary(individual=individual, benefit_plan=plan, status=BeneficiaryStatus.ACTIVE)
        beneficiary.save(username=self.user.username)
        return beneficiary

    def _add_to_group(self, individual):
        group = Group(code=f"G-{individual.first_name}")
        group.save(username=self.user.username)
        membership = GroupIndividual(individual=individual, group=group, role=GroupIndividual.Role.HEAD)
        membership.save(username=self.user.username)
        return group, membership

    def _enrol_group(self, group):
        plan = self._plan("GRP", BenefitPlan.BenefitPlanType.GROUP_TYPE)
        group_beneficiary = GroupBeneficiary(group=group, benefit_plan=plan, status=BeneficiaryStatus.ACTIVE)
        group_beneficiary.save(username=self.user.username)
        return group_beneficiary

    def _assert_not_merged(self):
        self.candidate.refresh_from_db()
        self.assertEqual(self.candidate.status, DuplicateCandidate.Status.OPEN)
        self.kept.refresh_from_db()
        self.retired.refresh_from_db()
        self.assertFalse(self.kept.is_deleted)
        self.assertFalse(self.retired.is_deleted)

    def _resolve_same(self):
        return resolve(self.candidate, decision="same", keep=str(self.kept.id), actor=self.user)

    def test_resolve_refuses_a_retired_subject_with_a_live_beneficiary(self):
        self._enrol(self.retired)

        with self.assertRaisesMessage(ValueError, ENROLLED) as ctx:
            self._resolve_same()

        self.assertIn("beneficiary=1", str(ctx.exception))
        self._assert_not_merged()

    def test_resolve_refuses_a_retired_subject_with_a_group_membership(self):
        self._add_to_group(self.retired)

        with self.assertRaisesMessage(ValueError, ENROLLED) as ctx:
            self._resolve_same()

        self.assertIn("group_individual=1", str(ctx.exception))
        self._assert_not_merged()

    def test_resolve_names_the_group_beneficiary_of_the_retired_subjects_group(self):
        group, _membership = self._add_to_group(self.retired)
        self._enrol_group(group)

        with self.assertRaisesMessage(ValueError, ENROLLED) as ctx:
            self._resolve_same()

        self.assertIn("group_individual=1", str(ctx.exception))
        self.assertIn("group_beneficiary=1", str(ctx.exception))
        self._assert_not_merged()

    def test_merge_subjects_refuses_a_retired_subject_with_a_live_beneficiary(self):
        self._enrol(self.retired)

        with self.assertRaisesMessage(ValueError, ENROLLED):
            merge_subjects(self.kept, self.retired, self.user)

        self.retired.refresh_from_db()
        self.assertFalse(self.retired.is_deleted)

    def test_enrolment_of_the_kept_subject_does_not_block(self):
        self._enrol(self.kept)
        self._add_to_group(self.kept)

        self._resolve_same()

        self.candidate.refresh_from_db()
        self.assertEqual(self.candidate.status, DuplicateCandidate.Status.CONFIRMED)
        self.retired.refresh_from_db()
        self.assertTrue(self.retired.is_deleted)

    def test_resolve_merges_once_the_retired_subjects_rows_are_gone(self):
        beneficiary = self._enrol(self.retired)
        group, membership = self._add_to_group(self.retired)
        self._enrol_group(group).delete(user=self.user)
        with self.assertRaisesMessage(ValueError, ENROLLED):
            self._resolve_same()

        beneficiary.delete(user=self.user)
        membership.delete(user=self.user)
        self._resolve_same()

        self.candidate.refresh_from_db()
        self.assertEqual(self.candidate.status, DuplicateCandidate.Status.CONFIRMED)
        self.retired.refresh_from_db()
        self.assertTrue(self.retired.is_deleted)
