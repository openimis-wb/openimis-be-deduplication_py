import itertools

from django.contrib.postgres.aggregates import ArrayAgg
from django.db import models
from django.db.models import Count
from django.db.models.fields.json import KeyTextTransform
from django.db.models.functions import Cast, Lower, Trim

from deduplication.apps import DeduplicationConfig
from deduplication.column_resolution import is_model_column
from deduplication.sources import Candidate, CandidateSource, Watermark, order_pair
from deduplication.sources.subject import get_subject_model, subject_model_label, subject_watermark, touched_ids


class IdentifierSource(CandidateSource):
    """Exact-matches subjects on each configured identifier json_ext key, independently, normalised."""

    kind = "identifier"

    def watermark(self) -> Watermark:
        return subject_watermark()

    def scan(self, since):
        model = get_subject_model()
        keys = DeduplicationConfig.identifier_keys or []
        if not keys:
            return

        changed = touched_ids(model, since)
        if changed is not None and not changed:
            return

        base_queryset = model.objects.all()
        if is_model_column(model, "is_deleted"):
            base_queryset = base_queryset.filter(is_deleted=False)

        label = subject_model_label(model)
        for key in keys:
            normalised = Lower(Trim(Cast(KeyTextTransform(key, "json_ext"), models.TextField())))
            queryset = base_queryset.annotate(_value=normalised) \
                .exclude(_value="").exclude(_value__isnull=True) \
                .values("_value").annotate(_count=Count("id"), _ids=ArrayAgg("id", distinct=True)) \
                .filter(_count__gt=1).order_by()

            for row in queryset:
                ids = [str(i) for i in row["_ids"]]
                if changed is not None and not (set(ids) & changed):
                    continue
                for a, b in itertools.combinations(sorted(ids), 2):
                    a, b = order_pair(a, b)
                    yield Candidate(
                        subject_model=label, subject_a=a, subject_b=b,
                        kind=self.kind, score=None,
                        evidence={"columns": {key: row["_value"]}},
                    )
