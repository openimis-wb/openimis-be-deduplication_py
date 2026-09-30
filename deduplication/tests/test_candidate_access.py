import json
from types import SimpleNamespace

import graphene
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import PermissionDenied
from core.models.openimis_graphql_test_case import openIMISGraphQLTestCase, BaseTestContext
from core.test_helpers import create_test_interactive_user, create_test_role
from deduplication.apps import DeduplicationConfig
from deduplication.gql_queries import DuplicateCandidateGQLType
from deduplication.models import DuplicateCandidate
from deduplication.sources import order_pair
from deduplication.tests.helpers import LogInHelper
from individual.models import Individual
from tasks_management.models import Task, TaskExecutor, TaskGroup


class DuplicateCandidateAccessTest(openIMISGraphQLTestCase):
    """The right to read candidates (gql_query_duplicates_perms) holds on every read path."""

    NODE_QUERY = """
    query Node($id: ID!) {
      node(id: $id) { ... on DuplicateCandidateGQLType { id kind } }
    }
    """
    TASK_CANDIDATES_QUERY = """
    query TaskNode($id: ID!) {
      node(id: $id) {
        ... on TaskGQLType { duplicateCandidates { edges { node { id kind } } } }
      }
    }
    """

    @classmethod
    def setUpTestData(cls):
        cls.admin = LogInHelper().get_or_create_user_api()
        cls.admin_token = BaseTestContext(user=cls.admin).get_jwt()

        role = create_test_role([], name="DedupNoRights", is_system=0)
        cls.limited = create_test_interactive_user(
            username="DedupNoRightsUser", password="TestPasswordTest2!", roles=[role.id],
        )
        cls.limited_token = BaseTestContext(user=cls.limited).get_jwt()

        a = Individual(first_name="A", last_name="One", dob="1990-01-01")
        a.save(username=cls.admin.username)
        b = Individual(first_name="B", last_name="Two", dob="1990-01-01")
        b.save(username=cls.admin.username)
        subject_a, subject_b = order_pair(str(a.id), str(b.id))
        # The limited user executes the task, so the task is readable to it; the candidates it
        # links to still require the candidate right.
        group = TaskGroup(code="dedup-access", completion_policy=TaskGroup.TaskGroupCompletionPolicy.ANY)
        group.save(username=cls.admin.username)
        TaskExecutor(user=cls.limited, task_group=group).save(username=cls.admin.username)
        cls.task = Task(
            source="deduplication_candidate", executor_action_event="x", business_event="", data={},
            status=Task.Status.ACCEPTED, task_group=group,
        )
        cls.task.save(username=cls.admin.username)
        cls.candidate = DuplicateCandidate.objects.create(
            subject_model="individual.Individual", subject_a=subject_a, subject_b=subject_b,
            kind="demographic", source="TestSource", evidence={}, task=cls.task,
        )
        cls.candidate_gid = graphene.relay.Node.to_global_id("DuplicateCandidateGQLType", str(cls.candidate.id))
        cls.task_gid = graphene.relay.Node.to_global_id("TaskGQLType", str(cls.task.id))

    def _run(self, query, gid, token=None):
        headers = {"HTTP_AUTHORIZATION": f"Bearer {token}"} if token else {}
        response = self.query(query, variables={"id": gid}, headers=headers)
        return json.loads(response.content)

    def test_role_without_the_right_lacks_it(self):
        self.assertFalse(self.limited.has_perms(DeduplicationConfig.gql_query_duplicates_perms))
        self.assertTrue(self.admin.has_perms(DeduplicationConfig.gql_query_duplicates_perms))

    def test_node_is_refused_to_an_anonymous_user(self):
        body = self._run(self.NODE_QUERY, self.candidate_gid)
        self.assertTrue(body.get("errors"), body)
        self.assertIsNone((body.get("data") or {}).get("node"))

    def test_node_is_refused_without_the_query_right(self):
        body = self._run(self.NODE_QUERY, self.candidate_gid, self.limited_token)
        self.assertTrue(body.get("errors"), body)
        self.assertIsNone((body.get("data") or {}).get("node"))

    def test_node_is_returned_with_the_query_right(self):
        body = self._run(self.NODE_QUERY, self.candidate_gid, self.admin_token)
        self.assertFalse(body.get("errors"), body)
        self.assertEqual(body["data"]["node"]["kind"], "demographic")

    def test_task_reverse_connection_is_refused_to_an_anonymous_user(self):
        body = self._run(self.TASK_CANDIDATES_QUERY, self.task_gid)
        self.assertTrue(body.get("errors"), body)
        self.assertNotIn("duplicateCandidates", json.dumps(body.get("data")))

    def test_task_reverse_connection_is_refused_without_the_query_right(self):
        body = self._run(self.TASK_CANDIDATES_QUERY, self.task_gid, self.limited_token)
        self.assertTrue(body.get("errors"), body)
        self.assertNotIn(str(self.candidate.id), json.dumps(body.get("data")))

    def test_task_reverse_connection_lists_candidates_with_the_query_right(self):
        body = self._run(self.TASK_CANDIDATES_QUERY, self.task_gid, self.admin_token)
        self.assertFalse(body.get("errors"), body)
        edges = body["data"]["node"]["duplicateCandidates"]["edges"]
        self.assertEqual([e["node"]["kind"] for e in edges], ["demographic"])

    def test_type_queryset_refuses_an_anonymous_context(self):
        info = SimpleNamespace(context=SimpleNamespace(user=AnonymousUser()))
        with self.assertRaises(PermissionDenied):
            DuplicateCandidateGQLType.get_queryset(DuplicateCandidate.objects.all(), info)
