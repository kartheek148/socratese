# ADR 0003 — Transactional outbox, idempotent inbox, append-only traces

Status: Proposed (Milestone 0; implemented in Milestone 2)

## Decision
- Run completion writes run status, outcome, and an `outbox_events` row in one PostgreSQL transaction; a publisher relays to RabbitMQ; a reconciliation job republishes stale rows.
- Delivery is at-least-once. Consumers record `(consumer, event_id)` in `consumed_events`; learning jobs are deduplicated by `(job_type, dedupe_key)` such as `(run_id, pipeline_version)`. Retries are bounded with exponential backoff and jitter; exhausted messages go to a dead-letter queue and are visible in `learning_jobs`.
- `trace_events` is append-only (DB trigger rejects UPDATE/DELETE), unique on `(run_id, sequence)`, with size-capped metrics; large payloads are `artifacts` rows (tenant-scoped, sha256) referenced by ID. Secrets are redacted before persistence.
- All tenant-owned tables include `tenant_id` with composite foreign keys; repository methods require `tenant_id`.

## Consequences
Learning never blocks execution; duplicate deliveries cannot duplicate learning work. Requires PostgreSQL + RabbitMQ in integration tests (Docker Compose).
