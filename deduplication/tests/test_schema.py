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
