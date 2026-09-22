import uuid

from django.db import models
from django.db.models import F, Q
from django.utils.translation import gettext_lazy as _


class DuplicateCandidate(models.Model):
    """A suspected duplicate pair of subjects, detected by a CandidateSource."""

    class Status(models.TextChoices):
        OPEN = "OPEN", _("Open")
        CONFIRMED = "CONFIRMED", _("Confirmed")
        DISMISSED = "DISMISSED", _("Dismissed")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    subject_model = models.CharField(max_length=255)
    subject_a = models.CharField(max_length=64)
    subject_b = models.CharField(max_length=64)
    kind = models.CharField(max_length=32)
    source = models.CharField(max_length=64)
    score = models.FloatField(null=True, blank=True)
    evidence = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.OPEN)
    task = models.ForeignKey(
        'tasks_management.Task', models.DO_NOTHING, null=True, blank=True,
        related_name='duplicate_candidates',
    )
    reviewed_by = models.CharField(max_length=64, blank=True, default="")
    reviewed_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.TextField(blank=True, default="")
    date_created = models.DateTimeField(auto_now_add=True)
    date_updated = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "deduplication_candidate"
        constraints = [
            models.UniqueConstraint(
                fields=["subject_model", "subject_a", "subject_b", "kind"],
                name="dedup_candidate_unique_pair",
            ),
            models.CheckConstraint(
                check=Q(subject_a__lt=F("subject_b")),
                name="dedup_candidate_subject_order",
            ),
        ]
        indexes = [
            models.Index(fields=["status", "kind"], name="dedup_candidate_status_kind"),
        ]

    def __str__(self):
        return f"{self.subject_model} {self.subject_a}/{self.subject_b} ({self.kind})"


class ScanState(models.Model):
    """Watermark bookkeeping for a candidate source, one row per source kind."""

    kind = models.CharField(max_length=32, primary_key=True)
    updated_at = models.DateTimeField(null=True, blank=True)
    last_id = models.CharField(max_length=64, blank=True, default="")
    last_scan_at = models.DateTimeField(null=True, blank=True)
    summary = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "deduplication_scan_state"

    def __str__(self):
        return f"ScanState({self.kind})"
