import graphene
from graphene_django import DjangoObjectType

from core import ExtendedConnection
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
