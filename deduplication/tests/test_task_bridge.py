from django.test import TestCase

from deduplication.models import DuplicateCandidate
from deduplication.services import create_review_tasks
from deduplication.sources import order_pair
from deduplication.tests.helpers import LogInHelper
from individual.models import Individual
from tasks_management.models import Task
from tasks_management.services import TaskService


class TaskBridgeTest(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = LogInHelper().get_or_create_user_api()

    def _make_pair(self):
        a = Individual(first_name="A", last_name="One", dob="1990-01-01")
        a.save(username=self.user.username)
        b = Individual(first_name="B", last_name="Two", dob="1990-01-01")
        b.save(username=self.user.username)
        subject_a, subject_b = order_pair(str(a.id), str(b.id))
        candidate = DuplicateCandidate.objects.create(
            subject_model="individual.Individual", subject_a=subject_a, subject_b=subject_b,
            kind="demographic", source="TestSource", evidence={},
        )
        return a, b, candidate

    def test_create_review_tasks_sets_task_fk(self):
        a, b, candidate = self._make_pair()

        result = create_review_tasks([candidate.id], self.user)

        self.assertTrue(result['success'])
        candidate.refresh_from_db()
        self.assertIsNotNone(candidate.task_id)
        task = Task.objects.get(id=candidate.task_id)
        self.assertEqual(task.source, 'deduplication_candidate')
        self.assertEqual(task.data['id'], str(candidate.id))

    def test_completing_task_resolves_candidate_via_signal_bridge(self):
        a, b, candidate = self._make_pair()
        create_review_tasks([candidate.id], self.user)
        candidate.refresh_from_db()
        task_id = candidate.task_id

        task_service = TaskService(self.user)
        task_service.resolve_task({
            'id': task_id,
            'business_status': {},
            'additional_data': {'decision': 'same', 'keep': str(a.id), 'note': 'confirmed duplicate'},
        })
        task_service.complete_task({'id': task_id})

        candidate.refresh_from_db()
        self.assertEqual(candidate.status, DuplicateCandidate.Status.CONFIRMED)
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertFalse(a.is_deleted)
        self.assertTrue(b.is_deleted)

    def test_completing_task_with_different_decision_dismisses_without_merge(self):
        a, b, candidate = self._make_pair()
        create_review_tasks([candidate.id], self.user)
        candidate.refresh_from_db()
        task_id = candidate.task_id

        task_service = TaskService(self.user)
        task_service.resolve_task({
            'id': task_id,
            'business_status': {},
            'additional_data': {'decision': 'different', 'note': 'not a duplicate'},
        })
        task_service.complete_task({'id': task_id})

        candidate.refresh_from_db()
        self.assertEqual(candidate.status, DuplicateCandidate.Status.DISMISSED)
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertFalse(a.is_deleted)
        self.assertFalse(b.is_deleted)
