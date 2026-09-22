from django.test import TestCase

from core.service_signals import ServiceSignalBindType
from core.signals import bind_service_signal
from deduplication.services import merge_subjects
from deduplication.tests.helpers import LogInHelper
from individual.models import Individual
from location.test_helpers import create_test_location


class MergeSubjectsFieldPolicyTest(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = LogInHelper().get_or_create_user_api()

    def test_fill_empty_field_and_json_ext_from_retired(self):
        village = create_test_location("V")
        kept = Individual(first_name="Kept", last_name="One", dob="1990-01-01", json_ext={})
        kept.save(username=self.user.username)
        retired = Individual(
            first_name="Kept", last_name="One", dob="1990-01-01",
            location=village, json_ext={"bar": "X"},
        )
        retired.save(username=self.user.username)

        merge_subjects(kept, retired, self.user, policy="delete")

        kept.refresh_from_db()
        self.assertEqual(kept.location_id, village.id)
        self.assertEqual(kept.json_ext.get("bar"), "X")

    def test_differing_non_empty_values_are_journaled_and_never_overwritten(self):
        kept = Individual(first_name="Kept", last_name="One", dob="1990-01-01", json_ext={"foo": "A"})
        kept.save(username=self.user.username)
        retired = Individual(first_name="Retired", last_name="Two", dob="1990-01-01", json_ext={"foo": "B"})
        retired.save(username=self.user.username)

        merge_subjects(kept, retired, self.user, policy="delete")

        kept.refresh_from_db()
        self.assertEqual(kept.first_name, "Kept")
        self.assertEqual(kept.json_ext.get("foo"), "A")

        conflicts = kept.json_ext.get("merge_conflicts", [])
        fields = {c["field"] for c in conflicts}
        self.assertIn("first_name", fields)
        self.assertIn("foo", fields)

        first_name_conflict = next(c for c in conflicts if c["field"] == "first_name")
        self.assertEqual(first_name_conflict["kept"], "Kept")
        self.assertEqual(first_name_conflict["retired"], "Retired")
        self.assertEqual(first_name_conflict["retired_id"], str(retired.id))
        self.assertEqual(first_name_conflict["actor"], self.user.username)


class MergeSubjectsPolicyTest(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = LogInHelper().get_or_create_user_api()

    def test_delete_policy_soft_deletes_retired_without_marker(self):
        kept = Individual(first_name="Kept", last_name="One", dob="1990-01-01")
        kept.save(username=self.user.username)
        retired = Individual(first_name="Retired", last_name="Two", dob="1990-01-01")
        retired.save(username=self.user.username)

        merge_subjects(kept, retired, self.user, policy="delete")

        retired.refresh_from_db()
        self.assertTrue(retired.is_deleted)
        self.assertNotIn("retired_into", retired.json_ext or {})

    def test_retire_policy_marks_retired_into_before_soft_delete(self):
        kept = Individual(first_name="Kept", last_name="One", dob="1990-01-01")
        kept.save(username=self.user.username)
        retired = Individual(first_name="Retired", last_name="Two", dob="1990-01-01")
        retired.save(username=self.user.username)

        merge_subjects(kept, retired, self.user, policy="retire")

        retired.refresh_from_db()
        self.assertTrue(retired.is_deleted)
        self.assertEqual(retired.json_ext.get("retired_into"), str(kept.id))


class MergeSubjectsSignalTest(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = LogInHelper().get_or_create_user_api()

    def test_subject_merged_signal_emitted_once_after_commit(self):
        received = []

        def receiver(sender, **kwargs):
            received.append(kwargs.get('result'))

        bind_service_signal('deduplication.subject_merged', receiver, bind_type=ServiceSignalBindType.AFTER)

        kept = Individual(first_name="Kept", last_name="One", dob="1990-01-01")
        kept.save(username=self.user.username)
        retired = Individual(first_name="Retired", last_name="Two", dob="1990-01-01")
        retired.save(username=self.user.username)

        with self.captureOnCommitCallbacks(execute=True):
            merge_subjects(kept, retired, self.user, policy="delete")

        self.assertEqual(len(received), 1)
        payload = received[0]
        self.assertEqual(payload['subject_model'], 'individual.Individual')
        self.assertEqual(payload['kept_id'], str(kept.id))
        self.assertEqual(payload['retired_id'], str(retired.id))
        self.assertEqual(payload['actor'], self.user.username)
        self.assertEqual(payload['policy'], 'delete')
