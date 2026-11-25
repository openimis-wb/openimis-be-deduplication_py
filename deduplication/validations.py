import json
import uuid

from django.db.models import Q
from django.core.exceptions import ValidationError
from django.utils.translation import gettext as _

from social_protection.models import Beneficiary
from payroll.models import BenefitConsumption
from tasks_management.models import Task


def normalise_ids(ids):
    """
    Canonical (lowercase, hyphenated) form of each record id, duplicates dropped.
    Raises ValidationError on an id that is not a UUID.
    """
    normalised = []
    for value in ids or []:
        try:
            canonical = str(uuid.UUID(str(value)))
        except (ValueError, TypeError, AttributeError):
            raise ValidationError(f"Invalid record id: {json.dumps(str(value))}")
        if canonical not in normalised:
            normalised.append(canonical)
    return normalised


def ids_in_open_tasks(service_name):
    """Canonical ids held by the open (accepted or received) tasks of a source, whatever form they were stored in."""
    open_tasks = Task.objects.filter(
        Q(is_deleted=False),
        Q(status=Task.Status.ACCEPTED) | Q(status=Task.Status.RECEIVED),
        Q(source=service_name),
    )
    held = set()
    for task_ids in open_tasks.values_list('data__ids', flat=True):
        for value in task_ids if isinstance(task_ids, list) else []:
            try:
                held.add(str(uuid.UUID(str(value))))
            except (ValueError, TypeError, AttributeError):
                continue
    return held


class CreateDeduplicationReviewTasksValidation:
    @classmethod
    def validate_create_deduplication_task(cls, task_data, service_name):
        errors = cls.get_errors_deduplication_task_data(task_data, service_name)
        if errors:
            raise ValidationError(errors)

    @classmethod
    def get_errors_deduplication_task_data(cls, task_data, service_name):
        count = task_data.get("count")
        ids = normalise_ids(task_data.get("ids"))
        column_values = task_data.get("column_values")

        errors = [
            *cls._validate_arg_existence(
                count,
                "deduplication.validations.CreateDeduplicationReviewTasksValidation.count_missing"),
            *cls._validate_arg_existence(
                ids,
                "deduplication.validations.CreateDeduplicationReviewTasksValidation.ids_missing"),
            *cls._validate_arg_existence(
                column_values,
                "deduplication.validations.CreateDeduplicationReviewTasksValidation.column_values_missing")
        ]

        not_existing_ids = cls._validate_existence_of_beneficiary(ids)
        beneficiary_in_task = cls._validate_beneficiary_already_in_task(ids, service_name)

        errors.extend(
            [
                *cls._validate_no_errors(
                    not_existing_ids,
                    "deduplication.validations.CreateDeduplicationReviewTasksValidation.not_existing_ids",
                    "ids"
                ),
                *cls._validate_no_errors(
                    beneficiary_in_task,
                    "deduplication.validations.CreateDeduplicationReviewTasksValidation.beneficiary_in_task",
                    "beneficiaries"
                )
            ]
        )

        return errors

    @classmethod
    def _validate_arg_existence(cls, kwarg, error_message_id):
        if not kwarg:
            return [{"message": _(error_message_id)}]
        return []

    @classmethod
    def _validate_no_errors(cls, errors, error_message_id, dict_key):
        if errors:
            return [{"message": _(error_message_id) % {dict_key: json.dumps(errors)}}]
        return []

    @classmethod
    def _validate_existence_of_beneficiary(cls, ids):
        not_existing_ids = []
        for beneficiary_id in ids:
            if not Beneficiary.objects.filter(id=beneficiary_id).exists():
                not_existing_ids.append(beneficiary_id)
        return not_existing_ids

    @classmethod
    def _validate_beneficiary_already_in_task(cls, ids, service_name):
        beneficiary_in_task = {}
        held = ids_in_open_tasks(service_name)

        for beneficiary_id in ids:
            if beneficiary_id in held:
                beneficiary = Beneficiary.objects.get(id=beneficiary_id)
                beneficiary_in_task[beneficiary_id] = beneficiary.individual.__str__()

        return beneficiary_in_task


class CreateDeduplicationPaymentReviewTasksValidation:
    @classmethod
    def validate_create_deduplication_task(cls, task_data, service_name):
        errors = cls.get_errors_deduplication_task_data(task_data, service_name)
        if errors:
            raise ValidationError(errors)

    @classmethod
    def get_errors_deduplication_task_data(cls, task_data, service_name):
        count = task_data.get("count")
        ids = normalise_ids(task_data.get("ids"))
        column_values = task_data.get("column_values")

        errors = [
            *cls._validate_arg_existence(
                count,
                "deduplication.validations.CreateDeduplicationPaymentReviewTasksValidation.count_missing"),
            *cls._validate_arg_existence(
                ids,
                "deduplication.validations.CreateDeduplicationPaymentReviewTasksValidation.ids_missing"),
            *cls._validate_arg_existence(
                column_values,
                "deduplication.validations.CreateDeduplicationPaymentReviewTasksValidation.column_values_missing")
        ]

        not_existing_ids = cls._validate_existence_of_benefits(ids)
        benefits_in_task = cls._validate_benefits_already_in_task(ids, service_name)

        errors.extend(
            [
                *cls._validate_no_errors(
                    not_existing_ids,
                    "deduplication.validations.CreateDeduplicationPaymentReviewTasksValidation.not_existing_ids",
                    "ids"
                ),
                *cls._validate_no_errors(
                    benefits_in_task,
                    "deduplication.please_solve_the_created_tasks_first",
                    "benefits"
                )
            ]
        )

        return errors

    @classmethod
    def _validate_arg_existence(cls, kwarg, error_message_id):
        if not kwarg:
            return [{"message": _(error_message_id)}]
        return []

    @classmethod
    def _validate_no_errors(cls, errors, error_message_id, dict_key):
        if errors:
            return [{"message": _(error_message_id) % {dict_key: json.dumps(errors)}}]
        return []

    @classmethod
    def _validate_existence_of_benefits(cls, ids):
        not_existing_ids = []
        for benefits_id in ids:
            if not BenefitConsumption.objects.filter(id=benefits_id).exists():
                not_existing_ids.append(benefits_id)
        return not_existing_ids

    @classmethod
    def _validate_benefits_already_in_task(cls, ids, service_name):
        benefit_in_task = {}
        held = ids_in_open_tasks(service_name)

        for benefit_id in ids:
            if benefit_id in held:
                benefit = BenefitConsumption.objects.get(id=benefit_id)
                benefit_in_task[benefit_id] = benefit.individual.__str__()

        return benefit_in_task
