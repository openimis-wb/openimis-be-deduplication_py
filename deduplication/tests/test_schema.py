import json

from core.models import MutationLog
from core.models.openimis_graphql_test_case import openIMISGraphQLTestCase, BaseTestContext
from deduplication.models import DuplicateCandidate
from deduplication.sources import order_pair
from deduplication.tests.helpers import LogInHelper
from individual.models import Individual


class DuplicateCandidateSchemaTest(openIMISGraphQLTestCase):

    @classmethod
    def setUpTestData(cls):
        cls.user = LogInHelper().get_or_create_user_api()
        cls.user_token = BaseTestContext(user=cls.user).get_jwt()

        cls.a = Individual(first_name="A", last_name="One", dob="1990-01-01")
        cls.a.save(username=cls.user.username)
        cls.b = Individual(first_name="B", last_name="Two", dob="1990-01-01")
        cls.b.save(username=cls.user.username)
        subject_a, subject_b = order_pair(str(cls.a.id), str(cls.b.id))
        cls.candidate = DuplicateCandidate.objects.create(
            subject_model="individual.Individual", subject_a=subject_a, subject_b=subject_b,
            kind="demographic", source="TestSource", evidence={}, status=DuplicateCandidate.Status.OPEN,
        )

    def _assert_mutation_success(self, client_mutation_id):
        log = MutationLog.objects.get(client_mutation_id=client_mutation_id)
        self.assertEqual(log.status, MutationLog.SUCCESS, log.error)

    def test_duplicate_candidates_query(self):
        response = self.query(
            """
            query {
              duplicateCandidates(status: "OPEN", first: 10) {
                totalCount
                edges { node { id kind status } }
              }
            }
            """,
            headers={"HTTP_AUTHORIZATION": f"Bearer {self.user_token}"}
        )
        self.assertResponseNoErrors(response)
        data = json.loads(response.content)['data']['duplicateCandidates']
        self.assertGreaterEqual(data['totalCount'], 1)

    def test_duplicate_candidate_by_uuid_variable_reports_status_and_task(self):
        response = self.query(
            """
            query DuplicateCandidateStatus($id: ID) {
              duplicateCandidates(id: $id, first: 1) {
                edges { node { id status task { id status } } }
              }
            }
            """,
            variables={"id": str(self.candidate.id)},
            headers={"HTTP_AUTHORIZATION": f"Bearer {self.user_token}"}
        )
        self.assertResponseNoErrors(response)
        edges = json.loads(response.content)['data']['duplicateCandidates']['edges']
        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0]['node']['status'], 'OPEN')
        self.assertIsNone(edges[0]['node']['task'])

    def test_run_duplicate_scan_mutation(self):
        mutation = """
        mutation RunDuplicateScan($input: RunDuplicateScanMutationInput!) {
          runDuplicateScan(input: $input) { clientMutationId internalId }
        }
        """
        response = self.query(
            mutation,
            variables={"input": {"kinds": ["demographic"], "clientMutationId": "scan-1"}},
            headers={"HTTP_AUTHORIZATION": f"Bearer {self.user_token}"}
        )
        self.assertResponseNoErrors(response)
        self._assert_mutation_success("scan-1")

    def test_resolve_duplicate_candidate_mutation(self):
        mutation = """
        mutation ResolveDuplicateCandidate($input: ResolveDuplicateCandidateMutationInput!) {
          resolveDuplicateCandidate(input: $input) { clientMutationId internalId }
        }
        """
        response = self.query(
            mutation,
            variables={"input": {
                "id": str(self.candidate.id), "decision": "different",
                "note": "not a duplicate", "clientMutationId": "resolve-1",
            }},
            headers={"HTTP_AUTHORIZATION": f"Bearer {self.user_token}"}
        )
        self.assertResponseNoErrors(response)
        self._assert_mutation_success("resolve-1")

        self.candidate.refresh_from_db()
        self.assertEqual(self.candidate.status, DuplicateCandidate.Status.DISMISSED)

    def test_create_duplicate_review_tasks_mutation(self):
        mutation = """
        mutation CreateDuplicateReviewTasks($input: CreateDuplicateReviewTasksMutationInput!) {
          createDuplicateReviewTasks(input: $input) { clientMutationId internalId }
        }
        """
        response = self.query(
            mutation,
            variables={"input": {"ids": [str(self.candidate.id)], "clientMutationId": "tasks-1"}},
            headers={"HTTP_AUTHORIZATION": f"Bearer {self.user_token}"}
        )
        self.assertResponseNoErrors(response)
        self._assert_mutation_success("tasks-1")

        self.candidate.refresh_from_db()
        self.assertIsNotNone(self.candidate.task_id)

    def test_resolve_mutation_refuses_a_resolved_candidate(self):
        candidate = DuplicateCandidate.objects.get(id=self.candidate.id)
        candidate.status = DuplicateCandidate.Status.CONFIRMED
        candidate.save()
        mutation = """
        mutation ResolveDuplicateCandidate($input: ResolveDuplicateCandidateMutationInput!) {
          resolveDuplicateCandidate(input: $input) { clientMutationId internalId }
        }
        """
        response = self.query(
            mutation,
            variables={"input": {
                "id": str(self.candidate.id), "decision": "different", "clientMutationId": "resolve-stale",
            }},
            headers={"HTTP_AUTHORIZATION": f"Bearer {self.user_token}"}
        )
        self.assertResponseNoErrors(response)
        log = MutationLog.objects.get(client_mutation_id="resolve-stale")
        self.assertEqual(log.status, MutationLog.ERROR)
        self.assertIn("deduplication.mutation.candidate_not_open", log.error)

        candidate.refresh_from_db()
        self.assertEqual(candidate.status, DuplicateCandidate.Status.CONFIRMED)

    def _resolve_same(self, keep, client_mutation_id):
        mutation = """
        mutation ResolveDuplicateCandidate($input: ResolveDuplicateCandidateMutationInput!) {
          resolveDuplicateCandidate(input: $input) { clientMutationId internalId }
        }
        """
        response = self.query(
            mutation,
            variables={"input": {
                "id": str(self.candidate.id), "decision": "same", "keep": keep,
                "clientMutationId": client_mutation_id,
            }},
            headers={"HTTP_AUTHORIZATION": f"Bearer {self.user_token}"}
        )
        self.assertResponseNoErrors(response)
        return MutationLog.objects.get(client_mutation_id=client_mutation_id)

    def test_resolve_mutation_refuses_a_keep_outside_the_pair(self):
        x = Individual(first_name="X", last_name="Outside", dob="1985-05-05")
        x.save(username=self.user.username)

        log = self._resolve_same(str(x.id), "resolve-outsider")

        self.assertEqual(log.status, MutationLog.ERROR)
        self.assertIn("deduplication.resolve.keep_not_in_pair", log.error)
        self.candidate.refresh_from_db()
        self.assertEqual(self.candidate.status, DuplicateCandidate.Status.OPEN)
        for subject in (self.a, self.b, x):
            subject.refresh_from_db()
            self.assertFalse(subject.is_deleted)

    def test_resolve_mutation_refuses_a_soft_deleted_kept_subject(self):
        self.a.delete(user=self.user)

        log = self._resolve_same(str(self.a.id), "resolve-deleted-keep")

        self.assertEqual(log.status, MutationLog.ERROR)
        self.assertIn("deduplication.resolve.subject_deleted", log.error)
        self.candidate.refresh_from_db()
        self.assertEqual(self.candidate.status, DuplicateCandidate.Status.OPEN)
        self.b.refresh_from_db()
        self.assertFalse(self.b.is_deleted)
