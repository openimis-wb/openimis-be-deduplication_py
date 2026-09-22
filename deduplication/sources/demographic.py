import itertools

from django.contrib.postgres.aggregates import ArrayAgg
from django.db import models
from django.db.models import Count
from django.db.models.fields.json import KeyTextTransform
from django.db.models.functions import Cast

from core.utils import to_json_safe_value
from deduplication.apps import DeduplicationConfig
from deduplication.column_resolution import is_model_column, resolve_columns
from deduplication.sources import Candidate, CandidateSource, Watermark, order_pair
from deduplication.sources.subject import get_subject_model, subject_model_label, subject_watermark, touched_ids


class DemographicSource(CandidateSource):
    """Groups subjects sharing the same values on the configured demographic columns."""

    kind = "demographic"

    def watermark(self) -> Watermark:
        return subject_watermark()

    def scan(self, since):
        model = get_subject_model()
        columns = DeduplicationConfig.demographic_columns or []
        if not columns:
            return

        changed = touched_ids(model, since)
        if changed is not None and not changed:
            return

        db_columns, json_keys = resolve_columns(model, columns)
        queryset = model.objects.all()
        if is_model_column(model, "is_deleted"):
            queryset = queryset.filter(is_deleted=False)
        if json_keys:
            annotations = {
                key: Cast(KeyTextTransform(key, "json_ext"), models.TextField()) for key in json_keys
            }
            queryset = queryset.annotate(**annotations)

        values = db_columns + json_keys
        queryset = queryset.values(*values).annotate(
            _count=Count("id"), _ids=ArrayAgg("id", distinct=True)
        ).filter(_count__gt=1).order_by()

        label = subject_model_label(model)
        for row in queryset:
            ids = [str(i) for i in row.pop("_ids")]
            row.pop("_count")
            if changed is not None and not (set(ids) & changed):
                continue
            column_values = {key: to_json_safe_value(value) for key, value in row.items()}
            for a, b in itertools.combinations(sorted(ids), 2):
                a, b = order_pair(a, b)
                yield Candidate(
                    subject_model=label, subject_a=a, subject_b=b,
                    kind=self.kind, score=None, evidence={"columns": column_values},
                )
