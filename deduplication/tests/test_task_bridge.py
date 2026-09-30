from django.test import TestCase
from core.test_helpers import create_test_interactive_user

from deduplication.models import DuplicateCandidate
from deduplication.services import create_review_tasks, resolve
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

    def test_stale_task_does_not_merge_into_the_retired_subject(self):
        a, b, candidate = self._make_pair()
        create_review_tasks([candidate.id], self.user)
        candidate.refresh_from_db()
        task_id = candidate.task_id
        resolve(candidate, decision="same", keep=str(a.id), actor=self.user, note="decided on the page")

        task_service = TaskService(self.user)
        task_service.resolve_task({
            'id': task_id,
            'business_status': {},
            'additional_data': {'decision': 'same', 'keep': str(b.id), 'note': 'stale task'},
        })
        task_service.complete_task({'id': task_id})

        candidate.refresh_from_db()
        self.assertEqual(candidate.status, DuplicateCandidate.Status.CONFIRMED)
        self.assertEqual(candidate.decision_note, "decided on the page")
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertFalse(a.is_deleted)
        self.assertTrue(b.is_deleted)

    def test_stale_different_task_keeps_a_merged_pair_confirmed(self):
        a, b, candidate = self._make_pair()
        create_review_tasks([candidate.id], self.user)
        candidate.refresh_from_db()
        task_id = candidate.task_id
        resolve(candidate, decision="same", keep=str(a.id), actor=self.user)

        task_service = TaskService(self.user)
        task_service.resolve_task({
            'id': task_id,
            'business_status': {},
            'additional_data': {'decision': 'different', 'note': 'stale task'},
        })
        task_service.complete_task({'id': task_id})

        candidate.refresh_from_db()
        self.assertEqual(candidate.status, DuplicateCandidate.Status.CONFIRMED)

    def test_create_review_tasks_skips_resolved_candidates(self):
        a, b, candidate = self._make_pair()
        resolve(candidate, decision="same", keep=str(a.id), actor=self.user)

        result = create_review_tasks([candidate.id], self.user)

        self.assertTrue(result['success'])
        self.assertEqual(result['data'], [])
        candidate.refresh_from_db()
        self.assertIsNone(candidate.task_id)

    def test_create_review_tasks_skips_a_candidate_with_an_open_task(self):
        a, b, candidate = self._make_pair()
        create_review_tasks([candidate.id], self.user)
        candidate.refresh_from_db()
        first_task_id = candidate.task_id

        result = create_review_tasks([candidate.id], self.user)

        self.assertEqual(result['data'], [])
        candidate.refresh_from_db()
        self.assertEqual(candidate.task_id, first_task_id)
        self.assertEqual(Task.objects.filter(data__id=str(candidate.id)).count(), 1)

    def test_create_review_tasks_replaces_a_closed_task(self):
        a, b, candidate = self._make_pair()
        create_review_tasks([candidate.id], self.user)
        candidate.refresh_from_db()
        first_task_id = candidate.task_id
        Task.objects.filter(id=first_task_id).update(status=Task.Status.FAILED)

        create_review_tasks([candidate.id], self.user)

        candidate.refresh_from_db()
        self.assertIsNotNone(candidate.task_id)
        self.assertNotEqual(candidate.task_id, first_task_id)

    def _complete_task_with(self, candidate, additional_data):
        create_review_tasks([candidate.id], self.user)
        candidate.refresh_from_db()
        task_service = TaskService(self.user)
        task_service.resolve_task({
            'id': candidate.task_id,
            'business_status': {},
            'additional_data': additional_data,
        })
        task_service.complete_task({'id': candidate.task_id})

    def test_completing_task_with_a_keep_outside_the_pair_changes_nothing(self):
        a, b, candidate = self._make_pair()
        x = Individual(first_name="X", last_name="Outside", dob="1985-05-05")
        x.save(username=self.user.username)

        self._complete_task_with(candidate, {'decision': 'same', 'keep': str(x.id), 'note': 'wrong keep'})

        candidate.refresh_from_db()
        self.assertEqual(candidate.status, DuplicateCandidate.Status.OPEN)
        for subject in (a, b, x):
            subject.refresh_from_db()
            self.assertFalse(subject.is_deleted)

    def test_completing_task_keeping_a_soft_deleted_subject_changes_nothing(self):
        a, b, candidate = self._make_pair()
        a.delete(user=self.user)

        self._complete_task_with(candidate, {'decision': 'same', 'keep': str(a.id), 'note': 'deleted keep'})

        candidate.refresh_from_db()
        self.assertEqual(candidate.status, DuplicateCandidate.Status.OPEN)
        b.refresh_from_db()
        self.assertFalse(b.is_deleted)


class TaskBridgeApproverAndRollbackTest(TestCase):
    """The completing approver's decision is applied inside the completion's transaction."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = LogInHelper().get_or_create_user_api()
        first = create_test_interactive_user(username="DedupApproverOne", password="DedupApprover1!")
        second = create_test_interactive_user(username="DedupApproverTwo", password="DedupApprover2!")
        # jsonb returns keys sorted; the completing approver sorts last so that reading
        # the first stored entry picks the other approver's decision.
        cls.other_approver, cls.completing_approver = sorted((first, second), key=lambda u: str(u.id))

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
        create_review_tasks([candidate.id], self.user)
        candidate.refresh_from_db()
        return a, b, candidate

    def _resolve_as(self, user, task_id, additional_data):
        result = TaskService(user).resolve_task({
            'id': task_id, 'business_status': {}, 'additional_data': additional_data,
        })
        self.assertTrue(result['success'], result)

    def test_the_completing_approvers_decision_is_applied(self):
        a, b, candidate = self._make_pair()
        self._resolve_as(self.other_approver, candidate.task_id, {'decision': 'different', 'note': 'other'})
        self._resolve_as(self.completing_approver, candidate.task_id,
                         {'decision': 'same', 'keep': str(a.id), 'note': 'completing'})

        result = TaskService(self.completing_approver).complete_task({'id': candidate.task_id})

        self.assertTrue(result['success'], result)
        candidate.refresh_from_db()
        self.assertEqual(candidate.status, DuplicateCandidate.Status.CONFIRMED)
        self.assertEqual(candidate.decision_note, "completing")
        self.assertEqual(candidate.reviewed_by, self.completing_approver.username)
        b.refresh_from_db()
        self.assertTrue(b.is_deleted)

    def test_a_completing_approver_without_a_decision_is_refused_while_the_candidate_is_open(self):
        a, b, candidate = self._make_pair()
        self._resolve_as(self.other_approver, candidate.task_id, {'decision': 'same', 'keep': str(a.id)})

        result = TaskService(self.completing_approver).complete_task({'id': candidate.task_id})

        self.assertFalse(result['success'])
        self.assertIn("deduplication.resolve.decision_missing", result['detail'])
        self.assertNotEqual(Task.objects.get(id=candidate.task_id).status, Task.Status.COMPLETED)
        candidate.refresh_from_db()
        self.assertEqual(candidate.status, DuplicateCandidate.Status.OPEN)
        b.refresh_from_db()
        self.assertFalse(b.is_deleted)

    def test_a_task_whose_candidate_was_resolved_elsewhere_completes_without_a_decision(self):
        a, b, candidate = self._make_pair()
        resolve(candidate, decision="different", actor=self.user, note="on the page")

        result = TaskService(self.completing_approver).complete_task({'id': candidate.task_id})

        self.assertTrue(result['success'], result)
        self.assertEqual(Task.objects.get(id=candidate.task_id).status, Task.Status.COMPLETED)
        candidate.refresh_from_db()
        self.assertEqual(candidate.status, DuplicateCandidate.Status.DISMISSED)
        self.assertEqual(candidate.decision_note, "on the page")

    def test_a_refused_resolve_rolls_the_completion_back_and_a_retry_succeeds(self):
        a, b, candidate = self._make_pair()
        a.delete(user=self.user)
        self._resolve_as(self.completing_approver, candidate.task_id,
                         {'decision': 'same', 'keep': str(a.id), 'note': 'retry me'})

        refused = TaskService(self.completing_approver).complete_task({'id': candidate.task_id})

        self.assertFalse(refused['success'])
        self.assertIn("deduplication.resolve.subject_deleted", refused['detail'])
        self.assertNotEqual(Task.objects.get(id=candidate.task_id).status, Task.Status.COMPLETED)
        candidate.refresh_from_db()
        self.assertEqual(candidate.status, DuplicateCandidate.Status.OPEN)

        Individual.objects.filter(id=a.id).update(is_deleted=False)
        retried = TaskService(self.completing_approver).complete_task({'id': candidate.task_id})

        self.assertTrue(retried['success'], retried)
        self.assertEqual(Task.objects.get(id=candidate.task_id).status, Task.Status.COMPLETED)
        candidate.refresh_from_db()
        self.assertEqual(candidate.status, DuplicateCandidate.Status.CONFIRMED)
        self.assertEqual(candidate.decision_note, "retry me")
        b.refresh_from_db()
        self.assertTrue(b.is_deleted)
