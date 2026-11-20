# Data model (Milestone 2)

Source of truth: [`src/experience_engine/adapters/db/tables.py`](../src/experience_engine/adapters/db/tables.py). Migration: [`alembic/versions/0001_core_schema.py`](../alembic/versions/0001_core_schema.py) (a test asserts the two never drift: `alembic check` equivalent via `compare_metadata`).

Conventions: PostgreSQL 16; ids are UUIDv7 (`uuid`); timestamps are `timestamptz` and always UTC (the engine forces `timezone=UTC`); every tenant-owned table has `tenant_id text NOT NULL`; child tables reference parents with **composite `(tenant_id, id)` foreign keys**, so a row can never point at another tenant's parent. Tenant isolation is application-level (every repository method takes the tenant; no row-level security, per ADR 0004).

| Table | Purpose | Key constraints |
|---|---|---|
| `tasks` | immutable task spec per tenant | PK `(tenant_id, task_id)`; `spec_hash` — reusing a `task_id` with a different spec is rejected |
| `task_runs` | one row per run; `RUNNING` → `COMPLETED` | PK `run_id`; unique `(tenant_id, run_id)`; FK → `tasks`; status check |
| `trace_events` | **append-only** ordered execution events | unique `(tenant_id, run_id, sequence)`; FK → `task_runs`; parent FK `(tenant_id, run_id, parent_event_id)`; CHECK `metrics ≤ 8 KiB`, `artifact_refs ≤ 16 KiB` |
| `artifacts` | **append-only** metadata for blobs in the artifact store | PK `(tenant_id, sha256)`; hex check; flags `redacted`, `truncated` |
| `run_outcomes` | **append-only** verified outcome (one per run) | PK `run_id`; FK → `task_runs`; status ∈ `VERIFIED_SUCCESS/FAILURE/PARTIAL/UNKNOWN` |
| `policy_decisions` | controller decisions (eligible set, estimates, probability, reason, policy version; M5: `feature_snapshot` holds the match candidates and the selected `id@version`) | immutable except a **one-time back-fill** of `outcome_status`; FK → `task_runs` |
| `outbox_events` | transactional outbox | status `PENDING/PUBLISHED/DEAD_LETTERED`; partial index on pending rows by `next_attempt_at` |
| `consumed_events` | consumer inbox for idempotency | PK `(consumer, event_id)` |
| `learning_jobs` | queued learning work (M2 enqueues only) | unique `(tenant_id, job_type, dedupe_key)`; FK → `task_runs`; CHECK `eligible_for_success_mining ⇒ outcome_status = 'VERIFIED_SUCCESS'` |
| `dead_letters` | visible record of poison/exhausted messages | `raw` ≤ 4096 chars; `tenant_id`/`event_id` nullable (poison payloads may lack them) |
| `audit_log` | **append-only** audit (`run.started`, `run.completed`, `outbox.dead_lettered`, `outbox.requeued`) | indexed by `(tenant_id, entity_type, entity_id)` |
| `experience_cases` (M3) | **append-only** derived learning view of one run: family, outcome, `dataset_split`, `eligible` / `excluded_reason`, features, trajectory artifact hash | unique `(tenant_id, run_id, pipeline_version)`; FK → `task_runs`; CHECK `eligible ⇒ discovery split (IS TRUE) ∧ outcome ∈ {VERIFIED_SUCCESS, FAILURE}`; CHECK excluded rows carry a reason; M5 adds exclusion reason `CAPABILITY_ASSISTED` (runs with `run_outcomes.capability_version` set are never learned from; free-text column, no migration) |
| `capabilities` (M3) | stable capability identity | PK `(tenant_id, capability_id)`; unique `(tenant_id, skeleton_signature)` |
| `capability_versions` (M3) | immutable versions; M3 writes `CANDIDATE` only | unique `(tenant, capability, version)`; trigger: only `state` may change, no deletes; `definition_hash`, `procedure_hash`, `tests`, compiler/prompt/model versions |
| `capability_evidence` (M3) | **append-only** source successes / counterexamples per version | unique `(tenant, capability, version, run, role)`; FKs → `task_runs`, `capability_versions` (tenant-scoped) |
| `cost_ledger` (M3) | **append-only** learning/maintenance cost (wall ms, tokens, tool calls) per pipeline step | indexed `(tenant, pipeline)` |
| `capability_evaluations` (M4) | **append-only** evaluation record: verdict, risk class, policy snapshot + hash, per-suite counts, aggregate metrics, dataset versions, model/seed/runtime/verifier/environment/evaluator versions, report hash | FK → `capability_versions`; CHECK `PASS ⇒ held-out, transfer and negative counts > 0` |
| `capability_transitions` (M4) | **append-only** lifecycle history (from/to state, actor, reason, evaluation id, policy hash) | FK → versions, evaluation |
| `capability_pointers` (M4) | per capability: `active_version`, `previous_version` (rollback target) | PK `(tenant, capability)`; updated only in the transition transaction |
| `rollout_observations` (M4) | **append-only** shadow/canary observations (capability vs baseline outcome, tokens) | mode ∈ {SHADOW, CANARY}; FK → versions |

`task_runs.tags` (M3, jsonb) holds run provenance: `dataset_name`, `dataset_version`, `dataset_split`, `dataset_content_hash`, `variant`, `seed`. Missing/unknown provenance makes a run ineligible for learning.

## Immutability enforcement

Triggers (`ee_reject_mutation`, `ee_policy_decision_guard`, `ee_capability_version_guard`) reject `UPDATE`/`DELETE` on `trace_events`, `artifacts`, `run_outcomes`, `audit_log`, `experience_cases`, `capability_evidence`, `cost_ledger`, `capability_evaluations`, `capability_transitions`, `rollout_observations`; allow only the one-time outcome back-fill on `policy_decisions`; and allow only `state` changes on `capability_versions` — and (M4) only along the legal lifecycle graph, with `APPROVED` requiring a `PASS` evaluation. A partial unique index allows at most one `ACTIVE` version per capability. **Limits:** superusers/table owners can still `TRUNCATE` or drop triggers. Production application roles must be granted only `SELECT, INSERT` (plus `UPDATE` on `task_runs`, `outbox_events`, `learning_jobs`, and `UPDATE(outcome_status)` on `policy_decisions`). Roles are not created by the migration (deployment concern, deferred).

## Large payloads

Model requests/responses, tool arguments/outputs, and verifier results are **never** stored in event rows. They go to the `ArtifactStore` (content-addressed; local filesystem adapter at `$EE_ARTIFACT_ROOT/<sha256(tenant)[:32]>/<sha[:2]>/<sha>`), and the event keeps `{role, sha256, size_bytes, content_type, redacted, truncated}`. Payloads over 2 MiB are truncated (flagged). The tenant id is hashed for the directory name, reads re-verify the hash. `artifacts.uri` is an opaque `local://…` handle.

## Secret handling

`domain/redaction.py` masks known secret values (e.g. the LLM key, supplied to the `Redactor`) and common shapes (OpenAI/GitHub/Slack/AWS tokens, bearer tokens, private-key blocks, credentials in URLs, `password=`/`api_key:` pairs) in every artifact, metric string and action text **before** persistence. This is defence in depth, not a guarantee: unrecognised secret formats are not detected. Secrets must also stay out of prompts and tool output by design. Task specs are persisted verbatim and must not contain secrets.

## Schema changes in later milestones

Planned per [fit/gap report §5](./milestone-0-fit-gap-report.md): `experience_cases` (M3), `capabilities*`/evaluation/rollout tables (M3–M4), `policy_versions` (M6), `memory_items` (M7), `cost_ledger` (G8). Each ships with its own Alembic revision.
