import graphene
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import ValidationError, PermissionDenied

from core.schema import OpenIMISMutation
from deduplication.apps import DeduplicationConfig
from deduplication.models import DuplicateCandidate
from deduplication.services import (
    CreateDeduplicationReviewTasksService,
    CreateDeduplicationPaymentReviewTasksService,
    create_review_tasks,
    resolve,
    run_scan,
)


class SummaryGQLType(graphene.InputObjectType):
    count = graphene.Int()
    ids = graphene.List(graphene.String)
    column_values = graphene.JSONString()


class CreateDeduplicationReviewMutation(OpenIMISMutation):
    _mutation_module = "deduplication"
    _mutation_class = "CreateDeduplicationReviewMutation"

    class Input(OpenIMISMutation.Input):
        summary = graphene.List(SummaryGQLType, required=True)

    @classmethod
    def _validate(cls, user, **data):
        summary = data.get("summary")
        if type(user) is AnonymousUser or not user.id:
            raise ValidationError("mutation.authentication_required")
        if not user.has_perms(DeduplicationConfig.gql_create_deduplication_review_perms):
            raise PermissionDenied("unauthorized")
        if not summary or len(summary) == 0:
            raise ValidationError("mutation.columns_empty_list")

    @classmethod
    def async_mutate(cls, user, **data):
        try:
            cls._validate(user, **data)
            if "client_mutation_id" in data:
                data.pop('client_mutation_id')
            if "client_mutation_label" in data:
                data.pop('client_mutation_label')

            summary = data.get("summary")

            service = CreateDeduplicationReviewTasksService(user)
            res = service.create_beneficiary_duplication_tasks(summary)
            return res if not res['success'] else None
        except Exception as exc:
            return [
                {
                    'message': "deduplication.mutation.failed_to_create_deduplication_review",
                    'detail': str(exc)
                }]


class CreateDeduplicationPaymentReviewMutation(OpenIMISMutation):
    _mutation_module = "deduplication"
    _mutation_class = "CreateDeduplicationPaymentReviewMutation"

    class Input(OpenIMISMutation.Input):
        summary = graphene.List(SummaryGQLType, required=True)
        payment_cycle = graphene.String(required=False)

    @classmethod
    def _validate(cls, user, **data):
        summary = data.get("summary")
        if type(user) is AnonymousUser or not user.id:
            raise ValidationError("mutation.authentication_required")
        if not user.has_perms(DeduplicationConfig.gql_create_deduplication_review_perms):
            raise PermissionDenied("unauthorized")
        if not summary or len(summary) == 0:
            raise ValidationError("mutation.columns_empty_list")

    @classmethod
    def async_mutate(cls, user, **data):
        try:
            cls._validate(user, **data)
            if "client_mutation_id" in data:
                data.pop('client_mutation_id')
            if "client_mutation_label" in data:
                data.pop('client_mutation_label')

            summary = data.get("summary")
            payment_cycle_id = data.get("payment_cycle")
            service = CreateDeduplicationPaymentReviewTasksService(user)
            res = service.create_payment_benefit_duplication_tasks(summary, payment_cycle_id)
            return res if not res['success'] else None
        except Exception as exc:
            return [
                {
                    'message': "deduplication.mutation.failed_to_create_deduplication_review",
                    'detail': str(exc)
                }]


class RunDuplicateScanMutation(OpenIMISMutation):
    _mutation_module = "deduplication"
    _mutation_class = "RunDuplicateScanMutation"

    class Input(OpenIMISMutation.Input):
        kinds = graphene.List(graphene.String, required=False)

    @classmethod
    def _validate(cls, user, **data):
        if type(user) is AnonymousUser or not user.id:
            raise ValidationError("mutation.authentication_required")
        if not user.has_perms(DeduplicationConfig.gql_run_scan_perms):
            raise PermissionDenied("unauthorized")

    @classmethod
    def async_mutate(cls, user, **data):
        try:
            cls._validate(user, **data)
            data.pop('client_mutation_id', None)
            data.pop('client_mutation_label', None)
            run_scan(kinds=data.get('kinds'), actor=user)
            return None
        except Exception as exc:
            return [
                {
                    'message': "deduplication.mutation.failed_to_run_scan",
                    'detail': str(exc)
                }]


class ResolveDuplicateCandidateMutation(OpenIMISMutation):
    _mutation_module = "deduplication"
    _mutation_class = "ResolveDuplicateCandidateMutation"

    class Input(OpenIMISMutation.Input):
        id = graphene.UUID(required=True)
        decision = graphene.String(required=True)
        keep = graphene.String(required=False)
        note = graphene.String(required=False)

    @classmethod
    def _validate(cls, user, **data):
        if type(user) is AnonymousUser or not user.id:
            raise ValidationError("mutation.authentication_required")
        if not user.has_perms(DeduplicationConfig.gql_resolve_duplicate_perms):
            raise PermissionDenied("unauthorized")
        if data.get('decision') not in ('same', 'different'):
            raise ValidationError("deduplication.mutation.invalid_decision")

    @classmethod
    def async_mutate(cls, user, **data):
        try:
            cls._validate(user, **data)
            data.pop('client_mutation_id', None)
            data.pop('client_mutation_label', None)
            candidate = DuplicateCandidate.objects.get(id=data['id'])
            resolve(
                candidate,
                decision=data['decision'],
                keep=data.get('keep'),
                actor=user,
                note=data.get('note', ''),
            )
            return None
        except Exception as exc:
            return [
                {
                    'message': "deduplication.mutation.failed_to_resolve_duplicate",
                    'detail': str(exc)
                }]


class CreateDuplicateReviewTasksMutation(OpenIMISMutation):
    _mutation_module = "deduplication"
    _mutation_class = "CreateDuplicateReviewTasksMutation"

    class Input(OpenIMISMutation.Input):
        ids = graphene.List(graphene.UUID, required=True)

    @classmethod
    def _validate(cls, user, **data):
        if type(user) is AnonymousUser or not user.id:
            raise ValidationError("mutation.authentication_required")
        if not user.has_perms(DeduplicationConfig.gql_create_deduplication_review_perms):
            raise PermissionDenied("unauthorized")
        if not data.get('ids'):
            raise ValidationError("mutation.columns_empty_list")

    @classmethod
    def async_mutate(cls, user, **data):
        try:
            cls._validate(user, **data)
            data.pop('client_mutation_id', None)
            data.pop('client_mutation_label', None)
            res = create_review_tasks(data['ids'], user)
            return res if not res['success'] else None
        except Exception as exc:
            return [
                {
                    'message': "deduplication.mutation.failed_to_create_review_tasks",
                    'detail': str(exc)
                }]
