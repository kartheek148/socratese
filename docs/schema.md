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

## Governed memory (M5.8, migration 0004)

Memory artifacts have their own typed tables that mirror the capability tables and the same lifecycle graph: `memories` (immutable identity), `memory_versions` (immutable body + `content_hash` + state), `memory_evaluations`, `memory_transitions`, `memory_observations` (SHADOW/CANARY) and `memory_pointers` (active/previous). Evaluation, transition and observation tables are append-only (trigger); the `ee_memory_version_guard` trigger enforces the legal state graph and that `APPROVED` requires a PASS evaluation, so the rules hold for direct SQL as well. At most one ACTIVE version per memory id. Capability tables are unchanged. The memory body is typed (`domain/memory.py`): type (EPISODIC | CONSOLIDATED | PROCEDURAL), task family, applicability and exclusion conditions (the same typed precondition classes capabilities use), source experience references, provenance and a content hash that is verified on load.

## Adaptive controller (M6, migration 0005)

| Table | Purpose | Key constraints |
|---|---|---|
| `controller_policies` | immutable controller policy versions: typed parameters + frozen estimator snapshot (`definition`, `definition_hash`) and lifecycle `state` | unique `(tenant, policy_id, version)`; state ∈ `CANDIDATE/SHADOW/CANARY/ACTIVE/SUPERSEDED/REJECTED/ROLLED_BACK`; partial unique indexes: at most one ACTIVE, one CANARY and one SHADOW version per policy; trigger `ee_controller_policy_guard`: only `state` may change, only along the legal graph, and CANDIDATE→SHADOW / SHADOW→CANARY / CANARY→ACTIVE need a PASS evaluation of OFFLINE / SHADOW / CANARY for exactly this `definition_hash` (SUPERSEDED→ACTIVE is the rollback restore); no deletes |
| `controller_policy_evaluations` | **append-only** stage evidence: verdict, metrics, reasons, `definition_hash`, `gate_hash` | FK → `controller_policies` |
| `controller_policy_transitions` | **append-only** lifecycle history (from/to, actor, reason, evaluation id) | FK → versions and evaluations |

`policy_decisions.acted boolean NOT NULL DEFAULT true` (migration 0005): `false` marks a shadow decision that was recorded for evaluation and never executed; existing rows are `true`. The decision's `feature_snapshot` additionally carries the typed `DecisionFeatures` for adaptive policies (key `controller`) and a per-candidate `strategy` in the match snapshot. Run provenance, the verified outcome and the learning-plane join used for training are unchanged (`policy_decisions ⋈ run_outcomes`). Tenant scoping: every table has `tenant_id`; the registry, the decision log and `build_rollout` filter by it.

Migration 0006 (M6.1) only replaces the CHECK on `controller_policies.kind` with `('COST_AWARE','BANDIT','EVIDENCE_DRIVEN')`. Paired validation priors and their provenance are part of the immutable JSON `definition` (and its hash): optional `snapshot.paired`, `snapshot.prior_sources` and `params.evidence`, omitted when empty so the stored hashes of earlier definitions do not change. Downgrading 0006 raises while an `EVIDENCE_DRIVEN` version exists (history is not rewritten).

## Schema changes in later milestones

Planned per [fit/gap report §5](./milestone-0-fit-gap-report.md): `experience_cases` (M3), `capabilities*`/evaluation/rollout tables (M3–M4), `memory_items` (M7), `cost_ledger` (G8). Each ships with its own Alembic revision.
