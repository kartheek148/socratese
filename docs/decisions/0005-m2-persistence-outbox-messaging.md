# ADR 0005 — Milestone 2 decisions: persistence, outbox, messaging

Status: Accepted (Milestone 2)

## Context
M2 must make runs reconstructable, persist outcomes and traces append-only, and hand completed runs to asynchronous learning reliably, without coupling the execution plane to learning (ADR 0003 sets the intended design).

## Decisions
1. **Dependencies added:** `sqlalchemy`, `alembic`, `psycopg[binary]`, `celery` (pulls `kombu`/`amqp`). The stack could not be faked: outbox claiming (`SKIP LOCKED`), triggers, composite tenant FKs and broker dead-lettering are the behaviours under test. **FastAPI is not added**: no M2 requirement needs an HTTP API, and the orchestrator is a plain class. A task/read API is deferred to a milestone that needs it.
2. **Pure core, ports at the edge.** `execution/`, `learning/`, `domain/` depend only on `ports/` protocols (`RunLifecycle`, `TraceSink`, `ArtifactStore`, `LearningStore`, `EventPublisher`); SQLAlchemy/Celery live in `adapters/`. The architecture test now covers `learning/`. Persistence is opt-in on the orchestrator, so the DB-free benchmark path from M1 is unchanged (the M1 tests pass with no changes to their assertions).
3. **SQLAlchemy Core, not ORM.** Explicit statements keep tenant scoping and transactions visible; tables are Core `Table`s, the migration was generated with `alembic --autogenerate` then hand-extended with triggers, and a test guards against drift.
4. **Events are written incrementally; completion is one transaction.** Partial traces survive crashes; outcome, run status, decision back-fill, audit and outbox row commit atomically.
5. **`execution.completed` for every completed run**, not only verified ones, carrying the outcome status; eligibility for success mining is decided downstream and enforced by a CHECK constraint.
6. **Relay = reconciliation.** One transactional `SKIP LOCKED` loop both publishes and recovers unpublished rows; no separate reconciliation job.
7. **Dead-lettering is both broker-level (DLX queue) and DB-level (`dead_letters`)** for visibility.
8. **Celery tasks are registered per app (`shared=False`)** so multiple app instances in one process cannot reach each other's handlers (found by the integration tests).

## Consequences / known gaps
- Append-only is trigger-enforced; privileged roles can bypass it. Role/grant setup is a deployment task (documented in `docs/schema.md`).
- No stale-`RUNNING`-run reaper yet; no object-store adapter beyond local filesystem; no log/metrics export beyond correlation-tagged logs.
- Redaction is pattern-based and cannot catch unknown secret formats.
- Integration tests need Docker services; they skip (with a reason) when `EE_TEST_DATABASE_URL` / `EE_TEST_AMQP_URL` are unset.
