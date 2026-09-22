# openIMIS Backend deduplication reference module

## Legacy deduplication (beneficiaries, benefit payments)

Summary-based duplicate detection over `Beneficiary` and `BenefitConsumption`, reviewed
through `tasks_management` tasks (`CreateDeduplicationReviewTasksService`,
`CreateDeduplicationPaymentReviewTasksService`) and merged by
`merge_duplicate_beneficiaries` / `remove_duplicate_benefit_payments` on task completion.
GraphQL: `beneficiaryDeduplicationSummary`, `benefitDeduplicationSummary`,
`createDeduplicationTasks`, `createDeduplicationPaymentTasks`.

## Candidate sources

`deduplication.sources` is the seam other modules (e.g. `biometric_verification`) use to
feed duplicate-subject candidates into this module, without a hard dependency in either
direction:

- `Watermark(updated_at, last_id)` — a scan cursor.
- `Candidate(subject_model, subject_a, subject_b, kind, score, evidence)` — one suspected
  duplicate pair; `subject_a < subject_b` as strings, `kind` identifies the detector
  (`"demographic"`, `"identifier"`, `"biometric"`, or any other registered kind).
- `CandidateSource` — ABC with `scan(since)` and `watermark()`.
- `register(source)` / `sources()` — a process-wide registry, in registration order.
- `order_pair(a, b)` — sorts two subject ids into the canonical `(a, b)` order.

Two built-in sources are registered automatically (`DeduplicationConfig.ready()`):

- `DemographicSource` (`deduplication/sources/demographic.py`) groups subjects sharing the
  same values on `demographic_columns` (module config, default `first_name`, `last_name`,
  `dob`; a name not on the subject model is read as a `json_ext` key).
- `IdentifierSource` (`deduplication/sources/identifier.py`) exact-matches subjects on each
  `identifier_keys` `json_ext` key independently, normalised (trimmed, case-folded).

Both scan the configured subject model (`subject_model`, default `individual.Individual`,
resolved with `django.apps.apps.get_model`).

## Persisted candidates

`DuplicateCandidate` (table `deduplication_candidate`) stores one row per
`(subject_model, subject_a, subject_b, kind)`, with `source`, `score`, `evidence`, a
`status` (`OPEN` / `CONFIRMED` / `DISMISSED`), an optional `task` FK, and review metadata
(`reviewed_by`, `reviewed_at`, `decision_note`). `ScanState` keeps one watermark row per
source `kind`.

Services (`deduplication/services.py`):

- `record_candidate(candidate, source=...)` — get-or-creates the row; an `OPEN` row keeps
  the higher score and merges evidence; a `DISMISSED` row is never reopened.
- `run_scan(kinds=None, actor=...)` — runs registered sources (optionally filtered by
  kind), records candidates, advances each source's `ScanState`. Also available as
  `manage.py scan_duplicates [--kind KIND] [--user USERNAME]`.
- `scan_subject(subject_model, subject_id)` — on-demand scan across all sources for one
  subject.
- `resolve(candidate, decision="same"|"different", keep=None, actor=..., note="")` —
  `different` dismisses the candidate; `same` merges the subjects (see below) and confirms
  it. A dismissed candidate is never reopened.
- `create_review_tasks(candidate_ids, actor)` — one `tasks_management.Task` per candidate
  (`source="deduplication_candidate"`), FK'd back onto the candidate. Completing such a task
  (existing `task_service.complete_task` flow) resolves the candidate from
  `task.json_ext["additional_resolve_data"]` (`{"decision", "keep", "note"}`).

## Merge policy

`merge_subjects(kept, retired, actor, policy=None)` merges `retired` into `kept`:

- an empty field on `kept` is filled from `retired`; a differing non-empty value is kept
  and journaled into `kept.json_ext["merge_conflicts"]`
  (`{"field", "kept", "retired", "retired_id", "at", "actor"}`) — never overwritten.
  `json_ext` keys are merged the same way.
- `MERGE_POLICY` (module config `merge_policy`, default `"delete"`) then decides how
  `retired` is retired: `"delete"` soft-deletes it; `"retire"` first sets
  `retired.json_ext["retired_into"] = kept.id`, then soft-deletes it.
- After the transaction commits, the `deduplication.subject_merged` service signal fires
  with `subject_model`, `kept_id`, `retired_id`, `actor`, `policy` (read off
  `kwargs["result"]` by an `AFTER` receiver, the same pattern as this module's other
  service signals).

## Module configuration

Loaded the same way as the legacy rights, via `ModuleConfiguration` onto
`DeduplicationConfig`:

| key | default |
| --- | --- |
| `subject_model` | `"individual.Individual"` |
| `demographic_columns` | `["first_name", "last_name", "dob"]` |
| `identifier_keys` | `[]` |
| `merge_policy` | `"delete"` |
| `gql_create_deduplication_review_perms` | `["172001"]` |
| `gql_create_deduplication_payment_review_perms` | `["172002"]` |
| `gql_resolve_duplicate_perms` | `["172003"]` |
| `gql_run_scan_perms` | `["172004"]` |
| `gql_query_duplicates_perms` | `["172005"]` |

## GraphQL

- `duplicateCandidates(status, kind, subjectId, first, offset, orderBy)` — paginated
  connection over `DuplicateCandidate` (`gql_query_duplicates_perms`).
- `runDuplicateScan(kinds)` — mutation, runs `run_scan` (`gql_run_scan_perms`).
- `resolveDuplicateCandidate(id, decision, keep, note)` — mutation, runs `resolve`
  (`gql_resolve_duplicate_perms`).
- `createDuplicateReviewTasks(ids)` — mutation, runs `create_review_tasks`
  (`gql_create_deduplication_review_perms`, same right as the legacy
  `createDeduplicationTasks`).
