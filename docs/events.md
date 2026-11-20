# Events, state transitions and reliability (Milestone 2)

## Trace events (`trace_events`)

Emitted by the runtime/orchestrator through the `TraceSink` port; one append-only row each. Fields: `event_id, tenant_id, task_id, run_id, trace_id (= correlation id), sequence (0-based, unique per run), parent_event_id, type, component, component_version, action, status, metrics (≤32 scalar keys, strings ≤256 chars), artifact_refs, ts (UTC)`.

| Type | Emitted when | `component_version` | Artifacts |
|---|---|---|---|
| `DECISION` | controller selected a strategy | policy version | – (full record in `policy_decisions`) |
| `MODEL_CALL` | each LLM call (status `ok`/`error`) | model name | `request` (messages), `response` |
| `TOOL_CALL` | each tool call (`ok`/`failed`/`denied`) | runtime version | `args`, `output` |
| `INVALID_ACTION` | unparseable model reply (`retry`/`fatal`) | runtime version | – |
| `RUN_TERMINATED` | loop ended (`action` = termination reason) | runtime version | – |
| `CAPABILITY_MATCH` | (M5) matcher result: catalog size, eligible and rejected candidates with reasons | matcher | `candidates` |
| `CAPABILITY_EXEC` | (M5) capability executor finished (`status` = COMPLETED/FALLBACK/ABORTED/ESCALATE) | runtime | `steps` |
| `FALLBACK` | (M5) capability handed over to general planning; metrics hold the remaining budgets | runtime | — |
| `VERIFICATION` | independent verifier finished (`status` = verifier status) | verifier version | `results` |

Parent links: `MODEL_CALL → previous event`, `TOOL_CALL → its MODEL_CALL`, `RUN_TERMINATED`/`VERIFICATION → DECISION`. Capability version (`id@version`) is recorded on `run_outcomes.capability_version`; it is null for runs that did not use a capability.

Events are written as they happen (own transaction each), so a crashed run leaves a `RUNNING` run with a partial, inspectable trace. **Deferred:** a reaper that marks stale `RUNNING` runs (e.g. `ABANDONED`/`UNKNOWN`).

## Run state machine

`RUNNING --complete_run--> COMPLETED`. `complete_run` is **one transaction**: update run (guarded by `status='RUNNING'` and tenant) → insert `run_outcomes` → back-fill `policy_decisions.outcome_status` → insert `outbox_events` → insert audit row. Any failure rolls all of it back; a second completion raises `RunStateError`.

## Outbox event `execution.completed` (schema_version 1)

Published for **every** completed run (including `FAILURE`/`PARTIAL`/`UNKNOWN`) so uncertainty is recorded; consumers decide eligibility.

```json
{"schema_version": 1, "event_id": "<uuid7 = outbox_events.event_id>", "tenant_id": "...",
 "run_id": "...", "task_id": "...", "correlation_id": "<run_id>",
 "outcome_status": "VERIFIED_SUCCESS|FAILURE|PARTIAL|UNKNOWN",
 "config_id": "A_BASELINE", "policy_version": "...", "verifier_version": "...",
 "occurred_at": "<UTC ISO-8601>"}
```

Small by design: consumers load traces/artifacts by reference. Unknown fields or a different `schema_version` shape are rejected as poison.

## Delivery path and guarantees

```
orchestrator --(1 txn)--> outbox_events(PENDING)
ee-relay / SqlOutbox.relay_once --> RabbitMQ (Celery task ee.learning.process_execution_completed)
   exchange ee.learning.x --> queue ee.learning (durable, DLX -> ee.learning.dead.x)
Celery worker (acks_late, prefetch 1) --> ExecutionCompletedHandler
   --(1 txn)--> consumed_events(inbox) + learning_jobs(PENDING)
```

* **At-least-once, idempotent.** The relay may publish a row more than once (crash after publish, before commit). The consumer's inbox `(consumer, event_id)` and the job key `(tenant, job_type, "<run_id>:<pipeline_version>")` are written in one transaction, so redelivery never creates a second job and a crash never loses one. Outcomes: `ENQUEUED`, `DUPLICATE_DELIVERY`, `DUPLICATE_JOB`.
* **Claiming.** `SELECT … FOR UPDATE SKIP LOCKED` inside the publishing transaction: concurrent relays never publish the same row at once; a crashed relay releases its rows (they stay `PENDING`). This is also the reconciliation path for rows that were never published — no separate job is needed.
* **Relay retry.** On `PublishError` the batch stops at the first failing row (a broker outage must not burn every row's budget), `attempts += 1`, `next_attempt_at = now + full-jitter exponential backoff` (default `max_attempts=10`, base 1 s, cap 300 s). On exhaustion the row becomes `DEAD_LETTERED` with an `outbox.dead_lettered` audit row; operators can `requeue_dead_letter` (audited as `outbox.requeued`). Publisher confirms are enabled so broker rejection surfaces as a failure.
* **Consumer retry.** Transient exceptions: `retry` with full-jitter backoff, bounded by `RetryPolicy(max_attempts=5, base 1 s, cap 60 s)`. Poison (`NonRetryableEventError` from validation, or an `IntegrityError` such as a missing run) and exhausted retries: write a `dead_letters` row, then `Reject(requeue=False)` so RabbitMQ routes the original message to `ee.learning.dead`. If the DB is unavailable for that write, the DLQ copy still preserves the message.
* **Topology order.** The dead-letter queue is declared before the main queue (worker `worker_init` and publisher), otherwise dead-lettered messages would be dropped silently.
* **Ordering.** Not guaranteed across events; M2 emits one event per run so nothing depends on it.
* **Learning boundary (rule 9).** `learning_jobs.eligible_for_success_mining` is true only for `VERIFIED_SUCCESS`; a database CHECK enforces it. M2 only enqueues; no trajectory is built until M3.

## Correlation ids

`run_id` is the correlation/trace id. It appears in `task_runs.correlation_id`, every `trace_events.trace_id`, the audit rows, `outbox_events.correlation_id`, the event payload, the Celery message header `correlation_id`, `learning_jobs.correlation_id`, and structured log `extra` fields in the relay and worker.

## Running it

```
docker compose -f deploy/docker-compose.yml -p ee up -d --wait          # dev Postgres + RabbitMQ (loopback only)
export EE_DATABASE_URL=postgresql+psycopg://ee:ee_dev_password@127.0.0.1:5432/ee
export EE_AMQP_URL=amqp://ee:ee_dev_password@127.0.0.1:5672//
export EE_ARTIFACT_ROOT=runs/artifacts   # optional: EE_LEARNING_QUEUE, EE_DEAD_LETTER_QUEUE
uv run alembic upgrade head
uv run celery -A experience_engine.adapters.mq.worker:app worker -P solo -Q ee.learning -l INFO
EE_EVALUATOR_ROLE=evaluator uv run ee-bench run --llm fake --persist --out runs/a.jsonl
uv run ee-relay            # loop; add --once for a single pass
```

The compose credentials are throwaway development defaults; override `EE_PG_*` / `EE_AMQP_*` and never reuse them elsewhere. Host ports are overridable (`EE_PG_PORT`, `EE_AMQP_PORT`) if 5432/5672 are taken.
