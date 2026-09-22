import itertools

from django.contrib.postgres.aggregates import ArrayAgg
from django.core.exceptions import ImproperlyConfigured
from django.db import models
from django.db.models import Count
from django.db.models.fields.json import KeyTextTransform
from django.db.models.functions import Cast, Lower, Trim

from deduplication.apps import DeduplicationConfig
from deduplication.column_resolution import is_model_column
from deduplication.sources import Candidate, CandidateSource, Watermark, order_pair
from deduplication.sources.subject import get_subject_model, subject_model_label, subject_watermark, touched_ids

MATCH_EACH = "each"
MATCH_ALL = "all"


class IdentifierSource(CandidateSource):
    """
    Matches subjects on configured identifier json_ext keys, normalised (trimmed,
    case-folded). IDENTIFIER_MATCH selects the mode: "each" (default) matches a pair
    when any single key is equal; "all" matches only when every key is equal and
    non-empty for both subjects.
    """

    kind = "identifier"

    def watermark(self) -> Watermark:
        return subject_watermark()

    def scan(self, since):
        model = get_subject_model()

        mode = DeduplicationConfig.identifier_match
        mode = MATCH_EACH if mode is None else mode
        if mode not in (MATCH_EACH, MATCH_ALL):
            raise ImproperlyConfigured(
                f"DEDUPLICATION['IDENTIFIER_MATCH'] must be '{MATCH_EACH}' or '{MATCH_ALL}', got {mode!r}"
            )

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
        if mode == MATCH_ALL:
            yield from self._scan_all(base_queryset, keys, label, changed)
        else:
            yield from self._scan_each(base_queryset, keys, label, changed)

    def _scan_each(self, base_queryset, keys, label, changed):
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

    def _scan_all(self, base_queryset, keys, label, changed):
        value_fields = [f"_value_{i}" for i in range(len(keys))]
        annotations = {
            field: Lower(Trim(Cast(KeyTextTransform(key, "json_ext"), models.TextField())))
            for field, key in zip(value_fields, keys)
        }
        queryset = base_queryset.annotate(**annotations)
        for field in value_fields:
            queryset = queryset.exclude(**{field: ""}).exclude(**{f"{field}__isnull": True})

        queryset = queryset.values(*value_fields) \
            .annotate(_count=Count("id"), _ids=ArrayAgg("id", distinct=True)) \
            .filter(_count__gt=1).order_by()

        for row in queryset:
            ids = [str(i) for i in row["_ids"]]
            if changed is not None and not (set(ids) & changed):
                continue
            columns = {key: row[field] for key, field in zip(keys, value_fields)}
            for a, b in itertools.combinations(sorted(ids), 2):
                a, b = order_pair(a, b)
                yield Candidate(
                    subject_model=label, subject_a=a, subject_b=b,
                    kind=self.kind, score=None,
                    evidence={"columns": dict(columns)},
                )
