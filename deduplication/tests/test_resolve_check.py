import json
import uuid
from unittest import mock

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from core.models.openimis_graphql_test_case import openIMISGraphQLTestCase, BaseTestContext
from core.test_helpers import create_test_interactive_user
from deduplication.models import DuplicateCandidate
from deduplication.services import REFUSAL_MESSAGES, ResolveRefusal, check_resolve, resolve
from deduplication.sources import order_pair
from deduplication.tests.helpers import LogInHelper
from individual.models import Individual
from social_protection.models import Beneficiary, BeneficiaryStatus, BenefitPlan

PREFIX = "deduplication.resolve."
WRITING_STATEMENTS = ("INSERT", "UPDATE", "DELETE")


def _role_with_rights(name, rights):
    from core.models import Role, RoleRight
    from core.utils import TimeUtils

    role = Role.objects.create(name=name, is_blocked=False, is_system=0, audit_user_id=-1)
    for right_id in rights:
        RoleRight.objects.create(role_id=role.id, right_id=right_id, audit_user_id=-1, validity_from=TimeUtils.now())
    return role


class _PairFixture:
    """Two individuals and the candidates over them, created as the admin user."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = LogInHelper().get_or_create_user_api()

    def _subjects(self):
        a = Individual(first_name="A", last_name="One", dob="1990-01-01")
        a.save(username=self.user.username)
        b = Individual(first_name="B", last_name="Two", dob="1990-01-01")
        b.save(username=self.user.username)
        return a, b

    def _candidate(self, a, b, kind="demographic"):
        subject_a, subject_b = order_pair(str(a.id), str(b.id))
        return DuplicateCandidate.objects.create(
            subject_model="individual.Individual", subject_a=subject_a, subject_b=subject_b,
            kind=kind, source="TestSource", evidence={},
        )

    def _enrol(self, individual):
        plan = BenefitPlan(
            code="CHK", name="CHK", type=BenefitPlan.BenefitPlanType.INDIVIDUAL_TYPE, beneficiary_data_schema={},
        )
        plan.save(username=self.user.username)
        Beneficiary(individual=individual, benefit_plan=plan, status=BeneficiaryStatus.ACTIVE).save(
            username=self.user.username,
        )

    def _merged_pair_with_open_sibling(self):
        a, b = self._subjects()
        resolve(self._candidate(a, b, "demographic"), decision="same", keep=str(a.id), actor=self.user)
        return a, b, self._candidate(a, b, "biometric")


class ResolveCheckServiceTest(_PairFixture, TestCase):

    def _refusal(self, candidate, decision, keep=None):
        try:
            check_resolve(candidate, decision=decision, keep=keep)
        except ResolveRefusal as refusal:
            return refusal.code
        return None

    def _scenarios(self):
        """(name, candidate, decision, keep, expected code or None) for every outcome of resolve()."""
        cases = []

        a, b = self._subjects()
        cases.append(("same_ok", self._candidate(a, b), "same", str(a.id), None))

        a, b = self._subjects()
        cases.append(("same_default_keep_ok", self._candidate(a, b), "same", None, None))

        a, b = self._subjects()
        cases.append(("different_ok", self._candidate(a, b), "different", None, None))

        a, b = self._subjects()
        cases.append(
            ("keep_not_in_pair", self._candidate(a, b), "same", str(uuid.uuid4()), PREFIX + "keep_not_in_pair"),
        )

        a, b = self._subjects()
        candidate = self._candidate(a, b)
        b.delete(user=self.user)
        cases.append(("subject_deleted", candidate, "same", str(a.id), PREFIX + "subject_deleted"))

        a, b, late = self._merged_pair_with_open_sibling()
        cases.append(("keep_contradicts_merge", late, "same", str(b.id), PREFIX + "keep_contradicts_merge"))

        a, b = self._subjects()
        candidate = self._candidate(a, b)
        self._enrol(b)
        cases.append(("retired_subject_enrolled", candidate, "same", str(a.id), PREFIX + "retired_subject_enrolled"))

        a, b, late = self._merged_pair_with_open_sibling()
        cases.append(("pair_already_merged", late, "different", None, PREFIX + "pair_already_merged"))

        a, b, late = self._merged_pair_with_open_sibling()
        cases.append(("sibling_confirmed_pass_through", late, "same", str(a.id), None))
        return cases

    def test_each_outcome_of_resolve_is_reported_by_the_check(self):
        for name, candidate, decision, keep, expected in self._scenarios():
            with self.subTest(name):
                self.assertEqual(self._refusal(candidate, decision, keep), expected)

    def test_the_check_and_resolve_refuse_with_the_same_code(self):
        for name, candidate, decision, keep, expected in self._scenarios():
            with self.subTest(name):
                checked = self._refusal(candidate, decision, keep)
                try:
                    resolve(candidate, decision=decision, keep=keep, actor=self.user)
                    resolved = None
                except ResolveRefusal as refusal:
                    resolved = refusal.code
                self.assertEqual(checked, resolved)

    def test_a_pass_through_pair_with_an_enrolled_deleted_subject_is_not_refused(self):
        a, b = self._subjects()
        self._enrol(b)
        merged = self._candidate(a, b, "demographic")
        DuplicateCandidate.objects.filter(pk=merged.pk).update(status=DuplicateCandidate.Status.CONFIRMED)
        b.delete(user=self.user)
        late = self._candidate(a, b, "biometric")

        self.assertIsNone(self._refusal(late, "same", str(a.id)))

    def test_a_candidate_that_is_no_longer_open_needs_no_decision(self):
        a, b = self._subjects()
        candidate = self._candidate(a, b)
        resolve(candidate, decision="different", actor=self.user)
        candidate.refresh_from_db()

        self.assertIsNone(self._refusal(candidate, "same", str(uuid.uuid4())))

    def test_an_unknown_decision_is_rejected_like_resolve_does(self):
        a, b = self._subjects()
        with self.assertRaisesMessage(ValueError, "unknown decision"):
            check_resolve(self._candidate(a, b), decision="maybe")

    def test_every_refusal_code_has_a_message(self):
        for name, _candidate, _decision, _keep, expected in self._scenarios():
            if expected:
                self.assertIn(expected, REFUSAL_MESSAGES, name)

    def test_the_check_writes_nothing_and_takes_no_lock(self):
        for name, candidate, decision, keep, _expected in self._scenarios():
            with self.subTest(name):
                candidate_before = DuplicateCandidate.objects.filter(pk=candidate.pk).values().get()
                subjects_before = list(Individual.objects.order_by("id").values_list("id", "is_deleted", "json_ext"))

                with mock.patch("deduplication.services._MergeSignalEmitter.emit") as emit, \
                        CaptureQueriesContext(connection) as ctx:
                    self._refusal(candidate, decision, keep)

                statements = [q["sql"].lstrip().upper() for q in ctx.captured_queries]
                self.assertEqual([s for s in statements if s.startswith(WRITING_STATEMENTS)], [])
                self.assertEqual([s for s in statements if "FOR UPDATE" in s], [])
                emit.assert_not_called()
                self.assertEqual(DuplicateCandidate.objects.filter(pk=candidate.pk).values().get(), candidate_before)
                self.assertEqual(
                    list(Individual.objects.order_by("id").values_list("id", "is_deleted", "json_ext")),
                    subjects_before,
                )


class ResolveCheckQueryTest(_PairFixture, openIMISGraphQLTestCase):

    QUERY = """
    query Check($candidateId: UUID!, $decision: String!, $keep: String) {
      duplicateCandidateResolveCheck(candidateId: $candidateId, decision: $decision, keep: $keep) {
        ok code message
      }
    }
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.admin_token = BaseTestContext(user=cls.user).get_jwt()
        # 172005 alone: the check is gated like the candidate queries, not like the resolve mutation.
        reader_role = _role_with_rights("DedupCheckReader", [172005])
        cls.reader = create_test_interactive_user(
            username="DedupCheckReader", password="TestPasswordTest2!", roles=[reader_role.id],
        )
        cls.reader_token = BaseTestContext(user=cls.reader).get_jwt()
        blind_role = _role_with_rights("DedupCheckBlind", [172003])
        cls.blind = create_test_interactive_user(
            username="DedupCheckBlind", password="TestPasswordTest2!", roles=[blind_role.id],
        )
        cls.blind_token = BaseTestContext(user=cls.blind).get_jwt()

    def _check(self, candidate, decision, keep=None, token=None):
        headers = {"HTTP_AUTHORIZATION": f"Bearer {token}"} if token else {}
        variables = {"candidateId": str(candidate.id), "decision": decision, "keep": keep}
        return json.loads(self.query(self.QUERY, variables=variables, headers=headers).content)

    def test_the_query_reports_a_refusal_with_its_code_and_message(self):
        a, b = self._subjects()
        candidate = self._candidate(a, b)

        body = self._check(candidate, "same", str(uuid.uuid4()), self.admin_token)

        self.assertFalse(body.get("errors"), body)
        self.assertEqual(body["data"]["duplicateCandidateResolveCheck"], {
            "ok": False,
            "code": PREFIX + "keep_not_in_pair",
            "message": REFUSAL_MESSAGES[PREFIX + "keep_not_in_pair"],
        })

    def test_the_query_reports_ok_without_a_code(self):
        a, b = self._subjects()
        candidate = self._candidate(a, b)

        body = self._check(candidate, "same", str(a.id), self.admin_token)

        self.assertFalse(body.get("errors"), body)
        self.assertEqual(body["data"]["duplicateCandidateResolveCheck"], {"ok": True, "code": None, "message": None})

    def test_the_query_reports_the_pass_through_of_a_merged_pair_as_ok(self):
        a, b, late = self._merged_pair_with_open_sibling()

        body = self._check(late, "same", str(a.id), self.admin_token)

        self.assertTrue(body["data"]["duplicateCandidateResolveCheck"]["ok"], body)

    def test_the_query_reports_the_other_refusals(self):
        a, b, late = self._merged_pair_with_open_sibling()
        expected = {
            ("different", None): "pair_already_merged",
            ("same", str(b.id)): "keep_contradicts_merge",
        }
        for (decision, keep), code in expected.items():
            with self.subTest(code):
                body = self._check(late, decision, keep, self.admin_token)
                self.assertEqual(body["data"]["duplicateCandidateResolveCheck"]["code"], PREFIX + code)

    def test_the_query_writes_nothing(self):
        a, b = self._subjects()
        candidate = self._candidate(a, b)
        before = DuplicateCandidate.objects.filter(pk=candidate.pk).values().get()

        self._check(candidate, "same", str(a.id), self.admin_token)
        self._check(candidate, "different", None, self.admin_token)

        self.assertEqual(DuplicateCandidate.objects.filter(pk=candidate.pk).values().get(), before)
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertFalse(a.is_deleted)
        self.assertFalse(b.is_deleted)

    def test_a_holder_of_the_candidate_query_right_alone_is_served(self):
        a, b = self._subjects()
        candidate = self._candidate(a, b)

        body = self._check(candidate, "same", str(a.id), self.reader_token)

        self.assertFalse(body.get("errors"), body)
        self.assertTrue(body["data"]["duplicateCandidateResolveCheck"]["ok"])

    def test_without_the_candidate_query_right_the_check_is_refused(self):
        a, b = self._subjects()
        candidate = self._candidate(a, b)

        for name, token in (("resolve right only", self.blind_token), ("anonymous", None)):
            with self.subTest(name):
                body = self._check(candidate, "same", str(a.id), token)
                self.assertTrue(body.get("errors"), body)
                self.assertIsNone((body.get("data") or {}).get("duplicateCandidateResolveCheck"))
                self.assertRegex(body["errors"][0]["message"], r"(?i)unauthori[sz]ed")

    def test_an_unknown_decision_is_a_graphql_error(self):
        a, b = self._subjects()
        candidate = self._candidate(a, b)

        body = self._check(candidate, "maybe", None, self.admin_token)

        self.assertTrue(body.get("errors"), body)
        self.assertIsNone((body.get("data") or {}).get("duplicateCandidateResolveCheck"))
