import graphene
from django.core.exceptions import PermissionDenied
from django.utils.translation import gettext as _
from graphene_django import DjangoObjectType

from core import ExtendedConnection
from deduplication.apps import DeduplicationConfig
from deduplication.models import DuplicateCandidate


class DeduplicationSummaryRowGQLType(graphene.ObjectType):
    count = graphene.Int()
    ids = graphene.List(graphene.UUID)
    column_values = graphene.JSONString()


class DeduplicationSummaryGQLType(graphene.ObjectType):
    rows = graphene.List(DeduplicationSummaryRowGQLType)


class DuplicateCandidateGQLType(DjangoObjectType):
    class Meta:
        model = DuplicateCandidate
        interfaces = (graphene.relay.Node,)
        # status/kind/subjectId are declared as explicit args on the connection
        # field (Query.duplicate_candidates) and filtered by hand in its
        # resolver, so they are deliberately left out here to avoid graphene
        # raising a duplicate-argument error.
        filter_fields = {
            "id": ["exact"],
            "subject_model": ["exact"],
            "subject_a": ["exact"],
            "subject_b": ["exact"],
            "source": ["exact"],
            "date_created": ["exact", "lt", "lte", "gt", "gte"],
            "date_updated": ["exact", "lt", "lte", "gt", "gte"],
        }
        connection_class = ExtendedConnection

    @classmethod
    def get_queryset(cls, queryset, info):
        """
        The queryset when the caller holds gql_query_duplicates_perms. Lookups by relay
        global id (the root node field) and the reverse connection from a task go through
        get_queryset, so they check the right the connection resolver checks.
        """
        user = info.context.user
        if user.is_anonymous or not user.id or not user.has_perms(DeduplicationConfig.gql_query_duplicates_perms):
            raise PermissionDenied(_("unauthorized"))
        return queryset
