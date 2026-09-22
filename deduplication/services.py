import copy
import logging
from functools import lru_cache
from typing import Dict, List, Optional, Union, Tuple, Type
from datetime import datetime

from django.apps import apps
from django.contrib.postgres.aggregates import ArrayAgg
from django.core.exceptions import FieldDoesNotExist
from django.db import models
from django.db.models import Count, Q
from django.db.models.fields.json import KeyTextTransform
from django.db.models.functions import Cast
from django.db import transaction

from core.datetimes.ad_datetime import AdDate
from core.models import ExtendableModel, HistoryModel, User, HistoryBusinessModel
from core.services.utils import model_representation, output_exception
from core.signals import register_service_signal
from core.utils import to_json_safe_value
from deduplication.apps import DeduplicationConfig
from deduplication.column_resolution import resolve_columns, is_model_column
from deduplication.models import DuplicateCandidate, ScanState
from deduplication.sources import Candidate, Watermark, order_pair, sources as registered_sources
from deduplication.validations import CreateDeduplicationReviewTasksValidation, \
    CreateDeduplicationPaymentReviewTasksValidation
from individual.models import Individual
from invoice.models import Bill, BillItem
from payroll.models import BenefitConsumption, BenefitConsumptionStatus, \
    PayrollBenefitConsumption, PayrollStatus, BenefitAttachment
from payment_cycle.models import PaymentCycle
from social_protection.models import Beneficiary, BenefitPlan
from tasks_management.apps import TasksManagementConfig
from tasks_management.models import Task
from tasks_management.services import TaskService

logger = logging.getLogger(__name__)


class CreateDeduplicationReviewTasksService:
    def __init__(self, user, validation_class=CreateDeduplicationReviewTasksValidation):
        self.user = user
        self.validation_class = validation_class

    def create_beneficiary_duplication_tasks(self, summary):
        try:
            task_service = TaskService(self.user)
            tasks = []

            for task_data in summary:
                self.validation_class.validate_create_deduplication_task(task_data, self.get_class_name())
                create_task_data = {
                    'source': self.get_class_name(),
                    'executor_action_event': TasksManagementConfig.default_executor_event,
                    'business_data_serializer': (
                        'deduplication.services.CreateDeduplicationReviewTasksService'
                        '.create_beneficiary_duplication_task_serializer'
                    ),
                    'business_event': '',
                    'data': task_data,
                }
                tasks.append(task_service.create(create_task_data))

            return {
                "success": True, "message": "Ok", "detail": "", "data": tasks,
            }
        except Exception as exc:
            return output_exception(model_name='', method="create_beneficiary_duplication_tasks", exception=exc)

    def create_beneficiary_duplication_task_serializer(self, data):
        def serialize_individual(value):
            excluded_fields = self._get_excluded_model_fields()
            individual = Individual.objects.filter(id=value).first()
            individual_dict = model_representation(individual)

            exclude_fields_from_dict(individual_dict, excluded_fields)

            for k, v in individual_dict.items():
                individual_dict[k] = to_json_safe_value(v)

            return individual_dict

        def serialize_benefit_plan(value):
            benefit_plan = BenefitPlan.objects.filter(id=value).first()
            return benefit_plan.__str__()

        def exclude_fields_from_dict(dictionary, fields_to_exclude):
            for field in fields_to_exclude:
                dictionary.pop(field, None)

        def beneficiary_serializer(data):
            for key, value in data.items():
                if key == 'individual':
                    data[key] = serialize_individual(value)
                elif key == 'benefit_plan':
                    data[key] = serialize_benefit_plan(value)
                else:
                    data[key] = to_json_safe_value(value)
            return data

        def get_headers(benefit_plan):
            individual_fields = [field.name for field in Individual._meta.fields]
            beneficiary_fields = [field.name for field in Beneficiary._meta.fields]
            beneficiary_data_schema_fields = list(benefit_plan.beneficiary_data_schema.get('properties', {}).keys())

            excluded_fields = self._get_excluded_model_fields()
            excluded_fields.add('benefit_plan')
            headers = [field for field in individual_fields + beneficiary_fields + beneficiary_data_schema_fields if
                       field not in excluded_fields]

            return headers

        def serializer(key, value):
            excluded_fields = self._get_excluded_model_fields(exclude_json_ext=False)
            excluded_fields.discard("date_created")
            excluded_fields.discard("is_deleted")
            if key == 'ids':
                beneficiary_list = []
                beneficiaries = Beneficiary.objects.filter(id__in=value)

                for beneficiary in beneficiaries:
                    beneficiary_dict = model_representation(beneficiary)
                    exclude_fields_from_dict(beneficiary_dict, excluded_fields)

                    beneficiary_dict = beneficiary_serializer(beneficiary_dict)
                    beneficiary_list.append(beneficiary_dict)

                return beneficiary_list
            else:
                return to_json_safe_value(value)

        serialized_data = copy.deepcopy(data)
        beneficiary_id = serialized_data['ids'][0]
        benefit_plan = BenefitPlan.objects.filter(beneficiary__id=beneficiary_id).first()
        for key, value in data.items():
            serialized_data[key] = serializer(key, value)

        serialized_data['headers'] = get_headers(benefit_plan)

        return serialized_data

    @classmethod
    def get_class_name(cls):
        return cls.__name__

    @lru_cache(maxsize=None)
    def _get_excluded_model_fields(self, exclude_json_ext=True):
        fields_to_exclude = set(field.name for field in HistoryBusinessModel._meta.fields)
        if not exclude_json_ext:
            fields_to_exclude.discard('json_ext')

        return fields_to_exclude


class CreateDeduplicationPaymentReviewTasksService:
    def __init__(self, user, validation_class=CreateDeduplicationPaymentReviewTasksValidation):
        self.user = user
        self.validation_class = validation_class

    def create_payment_benefit_duplication_tasks(self, summary, payment_cycle_id):
        try:
            task_service = TaskService(self.user)
            tasks = []
            payment_cycle = PaymentCycle.objects.get(id=payment_cycle_id)
            for task_data in summary:
                self.validation_class.validate_create_deduplication_task(task_data, self.get_class_name())
                create_task_data = {
                    'source': self.get_class_name(),
                    'executor_action_event': TasksManagementConfig.default_executor_event,
                    'business_data_serializer': (
                        'deduplication.services.CreateDeduplicationPaymentReviewTasksService'
                        '.create_payment_benefit_duplication_task_serializer'
                    ),
                    'business_event': '',
                    'entity': payment_cycle,
                    'data': task_data,
                }
                tasks.append(task_service.create(create_task_data))
                self.flag_duplicated_benefit_payments(task_data)
            return {
                "success": True, "message": "Ok", "detail": "", "data": tasks,
            }
        except Exception as exc:
            return output_exception(model_name='', method="create_payment_benefit_duplication_tasks", exception=exc)

    def flag_duplicated_benefit_payments(self, task_data):
        # flag payroll marked as duplicated
        benefits = BenefitConsumption.objects.filter(id__in=task_data['ids'])
        for benefit in benefits:
            json_ext_benefit = benefit.json_ext if benefit.json_ext else {}
            if 'duplicated' not in json_ext_benefit:
                json_ext_benefit['duplicated'] = 'duplicated'
                benefit.json_ext = json_ext_benefit
                benefit.save(user=self.user)

    def create_payment_benefit_duplication_task_serializer(self, data):
        def serialize_individual(value):
            excluded_fields = self._get_excluded_model_fields()
            individual = Individual.objects.filter(id=value).first()
            individual_dict = model_representation(individual)

            exclude_fields_from_dict(individual_dict, excluded_fields)

            for k, v in individual_dict.items():
                individual_dict[k] = to_json_safe_value(v)

            return individual_dict

        def exclude_fields_from_dict(dictionary, fields_to_exclude):
            for field in fields_to_exclude:
                dictionary.pop(field, None)

        def benefit_serializer(data):
            for key, value in data.items():
                if key == 'individual':
                    data[key] = serialize_individual(value)
                else:
                    data[key] = to_json_safe_value(value)
            payroll_benefit = PayrollBenefitConsumption.objects.filter(benefit=data['benefit']).first()
            if payroll_benefit:
                payroll = payroll_benefit.payroll
                data['payroll_name'] = payroll.name if payroll else None
                data['payment_cycle'] = \
                    payroll.payment_cycle.code if payroll and payroll.payment_cycle else None
            else:
                data['payroll_name'] = ""
                data['payment_cycle'] = ""
            return data

        def get_headers():
            individual_fields = [field.name for field in Individual._meta.fields]
            benefit_fields = [field.name for field in BenefitConsumption._meta.fields]

            excluded_fields = self._get_excluded_model_fields()
            headers = [field for field in individual_fields + benefit_fields if
                       field not in excluded_fields]
            headers.insert(0, 'benefit')
            headers.insert(1, 'payroll_name')
            headers.insert(2, 'payment_cycle')
            return headers

        def serializer(key, value):
            excluded_fields = self._get_excluded_model_fields(exclude_json_ext=False)
            excluded_fields.discard("date_created")
            excluded_fields.discard("is_deleted")
            if key == 'ids':
                benefits_list = []
                benefits = BenefitConsumption.objects.filter(id__in=value)

                for benefit in benefits:
                    benefit_dict = model_representation(benefit)
                    exclude_fields_from_dict(benefit_dict, excluded_fields)
                    benefit_dict['benefit'] = benefit.id
                    benefit_dict = benefit_serializer(benefit_dict)
                    benefits_list.append(benefit_dict)

                return benefits_list
            else:
                return to_json_safe_value(value)
        serialized_data = copy.deepcopy(data)
        for key, value in data.items():
            serialized_data[key] = serializer(key, value)
        serialized_data['headers'] = get_headers()
        return serialized_data

    @classmethod
    def get_class_name(cls):
        return cls.__name__

    @lru_cache(maxsize=None)
    def _get_excluded_model_fields(self, exclude_json_ext=True):
        fields_to_exclude = set(field.name for field in HistoryBusinessModel._meta.fields)
        if not exclude_json_ext:
            fields_to_exclude.discard('json_ext')

        return fields_to_exclude


def get_beneficiary_duplication_aggregation(columns: List[str] = None, benefit_plan_id: str = None):
    if not columns:
        raise ValueError("At least one column required")
    if not benefit_plan_id:
        raise ValueError("Benefit Plan not specified")

    db_columns, json_keys = _resolve_columns(Beneficiary, columns)
    filters = [Q(benefit_plan_id=benefit_plan_id), Q(is_deleted=False)]

    return get_duplication_aggregation(Beneficiary, columns=db_columns, json_ext_keys=json_keys, filters=filters)


def get_benefit_consumption_duplication_aggregation(columns: List[str] = None, payment_cycle_id: str = None):
    if not columns:
        raise ValueError("At least one column required")
    if not payment_cycle_id:
        raise ValueError("Payment Cycle not specified")

    db_columns, json_keys = _resolve_columns(Individual, columns)
    benefit_consumption_ids = PayrollBenefitConsumption.objects.filter(
        payroll__payment_cycle_id=payment_cycle_id,
        payroll__status=PayrollStatus.PENDING_APPROVAL
    ).values_list('benefit_id', flat=True)

    filters = [
        Q(id__in=benefit_consumption_ids),
        Q(is_deleted=False),
        Q(status=BenefitConsumptionStatus.ACCEPTED)
    ]

    return get_duplication_benefit_aggregation(BenefitConsumption, columns=db_columns, json_ext_keys=json_keys, filters=filters)


def get_duplication_aggregation(model: Union[Type[ExtendableModel], Type[HistoryModel]], columns: List[str] = None,
                                json_ext_keys: List[str] = None, filters: List = None):
    queryset = model.objects.filter(*filters)
    if json_ext_keys:
        json_ext_aggr = {key: Cast(KeyTextTransform(key, 'json_ext'), models.TextField())
                         for key in json_ext_keys}
        queryset = queryset.annotate(**json_ext_aggr)

    values = (columns or list()) + (json_ext_keys or list())

    if not values:
        raise ValueError("At least one column required")
    queryset = queryset.values(*values).annotate(id_count=Count('id'), ids=ArrayAgg('id', distinct=True)).filter(
        id_count__gt=1).order_by()

    return queryset


def get_duplication_benefit_aggregation(
    model: Union[Type[ExtendableModel], Type[HistoryModel]],
    columns: List[str] = None,
    json_ext_keys: List[str] = None, filters: List = None
):
    queryset = model.objects.filter(*filters).select_related('individual')
    if json_ext_keys:
        json_ext_aggr = {key: Cast(KeyTextTransform(key, 'individual__json_ext'), models.TextField())
                         for key in json_ext_keys}
        queryset = queryset.annotate(**json_ext_aggr)

    individual_columns = ['first_name', 'last_name', 'dob']
    columns = [f'individual__{column}' if column in individual_columns else column for column in columns]
    values = (columns or list()) + (json_ext_keys or list())
    if not values:
        raise ValueError("At least one column required")

    queryset = queryset.values(*values).annotate(id_count=Count('id'), ids=ArrayAgg('id', distinct=True)).filter(
        id_count__gt=1).order_by()
    return queryset


def _resolve_columns(model: Union[Type[ExtendableModel], Type[HistoryModel]], columns: List[str]) -> Tuple[
        List[str], List[str]]:
    return resolve_columns(model, columns)


def _is_model_column(model: Union[Type[ExtendableModel], Type[HistoryModel]], column: str) -> bool:
    return is_model_column(model, column)


def _update_instance_if_different_value(instance, instance_kwargs, user):
    is_instance_updated = False
    if instance_kwargs:
        for field, field_value in instance_kwargs.items():
            if field == 'dob':
                date_object = datetime.strptime(field_value, "%Y-%m-%d").date()
                field_value = AdDate(date_object.year, date_object.month, date_object.day)
            if getattr(instance, field) != field_value:
                is_instance_updated = True
                setattr(instance, field, field_value)
        if is_instance_updated:
            instance.save(username=user.username)


def _update_instance_json_ext_if_different_value(instance, json_ext_kwargs, user):
    is_instance_updated = False
    if json_ext_kwargs:
        json_ext = instance.json_ext or {}
        for key, value in json_ext_kwargs.items():
            current_value = json_ext.get(key)
            if not current_value and not value:
                continue
            if current_value != value:
                is_instance_updated = True
                if value:
                    json_ext[key] = value
                else:
                    del json_ext[key]
        instance.json_ext = json_ext
        if is_instance_updated:
            instance.save(username=user.username)


@transaction.atomic
def merge_duplicate_beneficiaries(task_data, user_id):
    individual_fields = {"first_name", "last_name", "dob"}
    beneficiary_fields = {"status"}

    user = User.objects.get(id=user_id)
    # additional_resolve_data is a key in task__json_ext that stores data selected by user during resolving a task
    additional_resolve_data = task_data.get("json_ext", {}).get("additional_resolve_data", {})
    merge_data = list(additional_resolve_data.values())[0]
    field_values = merge_data.get('values', {})
    beneficiary_ids = merge_data.get("beneficiaryIds", [])

    oldest_record_beneficiary = Beneficiary.objects.filter(id__in=beneficiary_ids).order_by('date_created').first()
    if oldest_record_beneficiary is None:
        return  # No beneficiaries found to merge

    beneficiaries_to_delete = Beneficiary.objects.filter(id__in=beneficiary_ids).exclude(
        id=oldest_record_beneficiary.id)

    individual_kwargs = {}
    beneficiary_kwargs = {}
    json_kwargs = {}

    for key, value in field_values.items():
        if key in individual_fields:
            individual_kwargs[key] = value
        elif key in beneficiary_fields:
            beneficiary_kwargs[key] = value
        else:
            json_kwargs[key] = value

    _update_instance_if_different_value(oldest_record_beneficiary.individual, individual_kwargs, user)
    _update_instance_if_different_value(oldest_record_beneficiary, beneficiary_kwargs, user)
    _update_instance_json_ext_if_different_value(oldest_record_beneficiary, json_kwargs, user)

    # needs to be done this way because overridden .delete() does not work on qs
    for beneficiary_to_delete in beneficiaries_to_delete:
        beneficiary_to_delete.delete(username=user.username)


def on_deduplication_task_complete_service_handler(**kwargs):
    try:
        result = kwargs.get('result', {})
        task = result['data']['task']
        source = result['data']['task']["source"]
        user_id = result['data']['user']["id"]
        if (result and result['success'] and task['status'] == Task.Status.COMPLETED
                and source == CreateDeduplicationReviewTasksService.get_class_name()):
            merge_duplicate_beneficiaries(task, user_id)
    except Exception as e:
        logger.error("Error while executing on_task_complete", exc_info=e)
        return [str(e)]


@transaction.atomic
def remove_duplicate_benefit_payments(task_data):
    # additional_resolve_data is a key in task__json_ext that stores data selected by user during resolving a task
    additional_resolve_data = task_data.get("json_ext", {}).get("additional_resolve_data", {})
    merge_data = list(additional_resolve_data.values())[0]
    benefit_ids = merge_data.get("benefitIds", [])
    benefits_to_delete = BenefitConsumption.objects.filter(id__in=benefit_ids)\
        .values_list('id', 'benefitattachment__bill')
    if len(benefits_to_delete) > 0:
        benefits, related_bills = zip(*benefits_to_delete)

        BenefitAttachment.objects.filter(
            benefit_id__in=benefits
        ).delete()

        BillItem.objects.filter(
            bill__id__in=related_bills
        ).delete()

        Bill.objects.filter(
            id__in=related_bills
        ).delete()

        PayrollBenefitConsumption.objects.filter(benefit__id__in=benefits).delete()

        BenefitConsumption.objects.filter(
            id__in=benefits,
            is_deleted=False
        ).delete()


def on_payment_benefit_deduplication_task_complete_service_handler(**kwargs):
    try:
        result = kwargs.get('result', {})
        task = result['data']['task']
        source = result['data']['task']["source"]
        if (result and result['success'] and task['status'] == Task.Status.COMPLETED
                and source == CreateDeduplicationPaymentReviewTasksService.get_class_name()):
            remove_duplicate_benefit_payments(task)
    except Exception as e:
        logger.error("Error while executing on_task_complete", exc_info=e)
        return [str(e)]


# --- Candidate sources: recording, scanning, resolving, merging (seam §4) ---

DUPLICATE_CANDIDATE_TASK_SOURCE = "deduplication_candidate"


def record_candidate(c: Candidate, *, source: str) -> Tuple[DuplicateCandidate, bool]:
    """Get-or-create a DuplicateCandidate for c's unique key; never reopens a dismissed row."""
    subject_a, subject_b = order_pair(c.subject_a, c.subject_b)
    obj, created = DuplicateCandidate.objects.get_or_create(
        subject_model=c.subject_model, subject_a=subject_a, subject_b=subject_b, kind=c.kind,
        defaults={'source': source, 'score': c.score, 'evidence': c.evidence or {}},
    )
    if created:
        return obj, True
    if obj.status != DuplicateCandidate.Status.OPEN:
        return obj, False

    changed = False
    if c.score is not None and (obj.score is None or c.score > obj.score):
        obj.score = c.score
        changed = True
    merged_evidence = {**(obj.evidence or {}), **(c.evidence or {})}
    if merged_evidence != (obj.evidence or {}):
        obj.evidence = merged_evidence
        changed = True
    if changed:
        obj.save()
    return obj, False


def run_scan(*, kinds: Optional[List[str]] = None, actor) -> Dict[str, int]:
    """Scan registered sources (optionally filtered by kind), record candidates, advance watermarks."""
    counts = {}
    for source in registered_sources():
        if kinds and source.kind not in kinds:
            continue
        state, _created = ScanState.objects.get_or_create(kind=source.kind)
        since = None
        if state.updated_at is not None or state.last_id:
            since = Watermark(updated_at=state.updated_at, last_id=state.last_id or None)

        count = 0
        for candidate in source.scan(since):
            record_candidate(candidate, source=type(source).__name__)
            count += 1

        new_watermark = source.watermark()
        state.updated_at = new_watermark.updated_at
        state.last_id = new_watermark.last_id or ""
        state.last_scan_at = datetime.now()
        state.summary = {'count': count}
        state.save()
        counts[source.kind] = count
    return counts


def scan_subject(subject_model: str, subject_id: str) -> List[DuplicateCandidate]:
    """On-demand full scan across all sources, recording only pairs touching subject_id."""
    subject_id = str(subject_id)
    results = []
    for source in registered_sources():
        for candidate in source.scan(None):
            if candidate.subject_model != subject_model:
                continue
            if subject_id not in (candidate.subject_a, candidate.subject_b):
                continue
            obj, _created = record_candidate(candidate, source=type(source).__name__)
            results.append(obj)
    return results


class _MergeSignalEmitter:
    """Wraps the signal-carrying call so `deduplication.subject_merged` fires with a clean payload."""

    @classmethod
    @register_service_signal('deduplication.subject_merged')
    def emit(cls, *, subject_model, kept_id, retired_id, actor, policy):
        return {
            'subject_model': subject_model,
            'kept_id': kept_id,
            'retired_id': retired_id,
            'actor': actor,
            'policy': policy,
        }


_SUBJECT_AUDIT_FIELD_NAMES = (
    {f.name for f in HistoryModel._meta.fields}
    | {f.name for f in HistoryBusinessModel._meta.fields}
    | {'json_ext'}
)


def _is_empty(value) -> bool:
    return value is None or value == ""


def _mergeable_fields(model):
    return [f for f in model._meta.fields if f.name not in _SUBJECT_AUDIT_FIELD_NAMES]


def _merge_field_values(kept, retired, actor) -> bool:
    """Fill empty fields on kept from retired; journal differing non-empty values, never overwrite."""
    changed = False
    conflicts = []
    now = datetime.now()

    for f in _mergeable_fields(type(kept)):
        kept_value = getattr(kept, f.attname)
        retired_value = getattr(retired, f.attname)
        if _is_empty(kept_value) and not _is_empty(retired_value):
            setattr(kept, f.attname, retired_value)
            changed = True
        elif not _is_empty(kept_value) and not _is_empty(retired_value) and kept_value != retired_value:
            conflicts.append({
                'field': f.name,
                'kept': to_json_safe_value(kept_value),
                'retired': to_json_safe_value(retired_value),
                'retired_id': str(retired.id),
                'at': now.isoformat(),
                'actor': actor.username,
            })

    kept_ext = dict(kept.json_ext or {})
    retired_ext = dict(retired.json_ext or {})
    for key, retired_value in retired_ext.items():
        if key == 'merge_conflicts':
            continue
        kept_value = kept_ext.get(key)
        if _is_empty(kept_value) and not _is_empty(retired_value):
            kept_ext[key] = retired_value
            changed = True
        elif not _is_empty(kept_value) and not _is_empty(retired_value) and kept_value != retired_value:
            conflicts.append({
                'field': key,
                'kept': to_json_safe_value(kept_value),
                'retired': to_json_safe_value(retired_value),
                'retired_id': str(retired.id),
                'at': now.isoformat(),
                'actor': actor.username,
            })

    if conflicts:
        kept_ext['merge_conflicts'] = kept_ext.get('merge_conflicts', []) + conflicts
        changed = True

    kept.json_ext = kept_ext
    return changed


@transaction.atomic
def merge_subjects(kept, retired, actor, *, policy: Optional[str] = None):
    """
    Merge retired into kept: fill-empty + conflict journal on both policies, then
    soft-delete retired ("delete") or mark it retired_into kept before soft-deleting ("retire").
    Emits deduplication.subject_merged after the transaction commits.
    """
    policy = policy or DeduplicationConfig.merge_policy
    if policy not in ('delete', 'retire'):
        raise ValueError(f"unknown merge policy {policy!r}")

    changed = _merge_field_values(kept, retired, actor)
    if changed:
        kept.save(user=actor)

    subject_model_label = type(kept)._meta.label
    kept_id = str(kept.id)
    retired_id = str(retired.id)

    if policy == 'retire':
        retired_ext = dict(retired.json_ext or {})
        retired_ext['retired_into'] = kept_id
        retired.json_ext = retired_ext
        retired.save(user=actor)
    retired.delete(user=actor)

    transaction.on_commit(lambda: _MergeSignalEmitter.emit(
        subject_model=subject_model_label, kept_id=kept_id, retired_id=retired_id,
        actor=actor.username, policy=policy,
    ))
    return kept


def resolve(candidate: DuplicateCandidate, *, decision: str, keep: Optional[str] = None, actor, note: str = ""):
    """different -> DISMISSED. same -> merge_subjects then CONFIRMED. Never reopens a dismissed row."""
    if decision not in ('same', 'different'):
        raise ValueError(f"unknown decision {decision!r}")
    if candidate.status == DuplicateCandidate.Status.DISMISSED:
        return candidate

    with transaction.atomic():
        if decision == 'different':
            candidate.status = DuplicateCandidate.Status.DISMISSED
        else:
            keep_id = keep or candidate.subject_a
            retired_id = candidate.subject_b if keep_id == candidate.subject_a else candidate.subject_a
            model = apps.get_model(candidate.subject_model)
            kept = model.objects.get(id=keep_id)
            retired = model.objects.get(id=retired_id)
            merge_subjects(kept, retired, actor)
            candidate.status = DuplicateCandidate.Status.CONFIRMED

        candidate.reviewed_by = actor.username
        candidate.reviewed_at = datetime.now()
        candidate.decision_note = note
        candidate.save()
    return candidate


def create_review_tasks(candidate_ids, actor) -> dict:
    """Create one tasks_management.Task per candidate, source=deduplication_candidate, task FK set."""
    try:
        task_service = TaskService(actor)
        tasks = []
        for candidate in DuplicateCandidate.objects.filter(id__in=candidate_ids):
            task_data = {
                'source': DUPLICATE_CANDIDATE_TASK_SOURCE,
                'executor_action_event': TasksManagementConfig.default_executor_event,
                'business_event': '',
                'data': {
                    'id': str(candidate.id),
                    'subject_model': candidate.subject_model,
                    'subject_a': candidate.subject_a,
                    'subject_b': candidate.subject_b,
                    'kind': candidate.kind,
                    'score': candidate.score,
                    'evidence': candidate.evidence,
                },
            }
            result = task_service.create(task_data)
            if result.get('success'):
                candidate.task_id = result['data']['id']
                candidate.save()
            tasks.append(result)
        return {"success": True, "message": "Ok", "detail": "", "data": tasks}
    except Exception as exc:
        return output_exception(model_name='DuplicateCandidate', method="create_review_tasks", exception=exc)


def on_duplicate_candidate_task_complete_service_handler(**kwargs):
    """Bridge: a completed deduplication_candidate task resolves its candidate with the reviewer's decision."""
    try:
        result = kwargs.get('result', {})
        if not result or not result.get('success'):
            return
        task = result['data']['task']
        if task.get('source') != DUPLICATE_CANDIDATE_TASK_SOURCE:
            return
        if task.get('status') != Task.Status.COMPLETED:
            return

        candidate_id = (task.get('data') or {}).get('id')
        if not candidate_id:
            return
        additional_resolve_data = (task.get('json_ext') or {}).get('additional_resolve_data', {})
        if not additional_resolve_data:
            return
        resolve_data = next(iter(additional_resolve_data.values()))

        user_id = result['data']['user']['id']
        actor = User.objects.get(id=user_id)
        candidate = DuplicateCandidate.objects.get(id=candidate_id)
        resolve(
            candidate,
            decision=resolve_data.get('decision'),
            keep=resolve_data.get('keep'),
            actor=actor,
            note=resolve_data.get('note', ''),
        )
    except Exception as e:
        logger.error("Error while executing on_duplicate_candidate_task_complete", exc_info=e)
        return [str(e)]
