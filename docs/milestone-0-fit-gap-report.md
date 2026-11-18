# Milestone 0 — Repository Discovery and Architecture Fit

Status: **Proposed — awaiting review.** No functional code was added. Milestone 1 must not start until this report is approved.

## 1. Discovery summary

Inspected on 2026-11-18, branch `main` (no commits yet), remote `origin = github.com/kartheek148/socratese`.

| Area | Finding |
|---|---|
| Tracked/untracked files | `README.md`, `.github/copilot-instructions.md`, `.github/prompts/implement-milestone.prompt.md`, `docs/architecture.md`, `docs/implementation-roadmap.md`. Nothing else. |
| Language / packaging | None. No `pyproject.toml`, lockfile, `package.json`, or `go.mod`. |
| Application / API / runtime | None. |
| Database, ORM, migrations | None. |
| Queue / workers | None. |
| Agent runtime, tool abstractions | None. |
| Auth / tenancy | None. |
| Tests, lint, type check | None; no standard commands exist to run. |
| CI / deployment | None (no `.github/workflows`, Dockerfile, compose). |
| Observability | None. |

Local toolchain: Python 3.12.3, `uv` (`~/.local/bin/uv`), `python3 -m venv` works, `pip` is not installed system-wide, Docker 28.3.3 + Compose v2.39.2 installed but the daemon was **not running** (so PostgreSQL/RabbitMQ integration tests could not be exercised during discovery). `fastapi`, `pydantic`, `sqlalchemy`, `pytest`, `ruff`, `mypy`, `psql` are not installed.

**Conclusion:** the repository is greenfield. Per `.github/copilot-instructions.md`, the preferred defaults apply (Python, FastAPI, Pydantic, PostgreSQL, SQLAlchemy/Alembic, RabbitMQ/Celery, LangGraph adapter, pgvector only when justified). There is no existing architecture to conflict with, so no adaptation of the design is required beyond the gaps listed in §8. "Standard commands" are therefore *proposed* (§9), not discovered.

## 2. Reuse / modify / missing

**Reusable (as-is):** the four documents above — the design, contracts (`TaskSpec`, `RunOutcome`, capability JSON, execution event), roadmap, and milestone prompt. Nothing executable exists to reuse.

**Needs modification (documents only):**
- `docs/architecture.md`: tighten contracts (§8 gaps G1–G8) and add a repo-fit pointer to this report. Done minimally in this milestone (pointer only); contract changes are applied in the milestone that implements them.
- `README.md`: add development commands once M1 creates tooling.

**Missing (everything executable):** project scaffolding; domain models; ports; benchmark harness; baseline agent; verifier; persistence + migrations; trace/outbox; Celery workers; learning pipeline; DSL + executor; registry; evaluator; controller; API; CI; docker-compose for PostgreSQL/RabbitMQ; ADR process.

## 3. Proposed package and module structure

Single distributable, `src` layout, managed with `uv` (lockfile committed). One-way dependency rule: `domain` ← `ports` ← (`execution`, `learning`, `capabilities`, `bench`) ← `adapters` ← `api`/`workers` (entrypoints). `domain`, `ports`, `execution`, `learning`, `capabilities`, `bench` must not import FastAPI, Celery, SQLAlchemy, LangGraph, or an LLM SDK. Enforced by an AST-based architecture test (no new dependency).

```
pyproject.toml, uv.lock
src/experience_engine/
  domain/            # pure Pydantic models + enums + state machines (no I/O)
    ids.py time.py   # stable ID factories, UTC clock helpers
    task.py          # TaskSpec, SuccessCriterion (typed union), Budgets
    outcome.py       # RunOutcome, OutcomeStatus, VerificationResult
    trace.py         # TraceEvent, EventType, ArtifactRef
    capability.py    # CapabilityDefinition, CapabilityVersion, LifecycleState + transitions
    decision.py      # ControllerDecision (eligible set, selected, propensity, ...)
    benchmark.py     # DatasetVersion, BenchmarkRun, Split enum
  ports/             # typing.Protocol interfaces only
    llm.py tools.py artifacts.py repositories.py events.py verifier.py clock.py retrieval.py
  execution/         # request path
    orchestrator.py analyzer.py controller.py runtime.py context.py (Null/lexical first) verifier.py
  capabilities/      # DSL schema, static validator, sandboxed executor, registry service
    dsl.py validator.py executor.py registry.py
  learning/          # async path
    collector.py trajectory.py miner.py compiler.py evaluation.py promotion.py policy_trainer.py
  bench/             # task families, fixtures, harness, report generator
  adapters/
    db/ (models.py, repositories.py, uow.py)     # SQLAlchemy
    mq/ (celery_app.py, outbox_publisher.py, consumers.py)
    llm/ (fake.py, <provider>.py)  artifacts/ (local_fs.py, s3.py later)
    orchestration/langgraph_adapter.py (only if needed; M5+)
  api/               # FastAPI routers, deps, schemas
  config.py          # pydantic-settings, safe defaults
alembic/ (env.py, versions/)
benchmarks/          # dataset definitions: discovery/, validation/, hidden/ (see §8 G6)
tests/ unit/ integration/ migration/ regression/ architecture/
deploy/docker-compose.yml  (postgres, rabbitmq, artifact volume)
.github/workflows/ci.yml
docs/ (architecture.md, decisions/, milestone reports, schema.md, events.md)
```

## 4. Component map (design → proposed path → milestone)

| Design component | Path | Milestone | Notes |
|---|---|---|---|
| Task API / orchestrator | `execution/orchestrator.py`, `api/` | M1 (orchestrator, in-process), M2 (API) | Orchestrator is a plain class; FastAPI is a thin adapter. |
| Task analyzer | `execution/analyzer.py` | M1 (deterministic), versioned | Persist analyzer version + features. |
| Context compiler (PMC) | `execution/context.py` | M1 `NullContextCompiler`; M7 real | Keeps config A–E switchable. |
| Adaptive controller | `execution/controller.py` | M1 fixed policy (logs decision); M6 rule-based/bandit | Decision record exists from the start (rule 10). |
| Runtime + tool gateway | `execution/runtime.py`, `ports/tools.py` | M1 (fake/deterministic tools), M2 audit, M5 capability exec | |
| Outcome verifier | `execution/verifier.py` | M1 | Independent of agent output; runs in benchmark env. |
| Collector / trajectory | `learning/collector.py`, `trajectory.py` | M2 (collector), M3 | |
| Miner / compiler | `learning/miner.py`, `compiler.py` | M3 | Candidates only; never executable. |
| DSL + executor | `capabilities/dsl.py`, `executor.py` | M3 (schema), M4 (executor) | Executor built once in M4, reused unchanged in M5. |
| Evaluation / promotion | `learning/evaluation.py`, `promotion.py` | M4 | |
| Registry | `capabilities/registry.py` | M4 | Audited state machine. |
| Policy trainer | `learning/policy_trainer.py` | M6 | |
| Outbox / MQ | `adapters/mq/` | M2 | |
| Persistence | `adapters/db/`, `alembic/` | M2 | M1 uses in-memory/JSONL result store behind the same ports. |
| Benchmark harness | `bench/` | M1 | |

## 5. Database schema changes

No schema exists. Introduced incrementally (each via Alembic; none in M1). All tenant-owned tables carry `tenant_id NOT NULL`, IDs are app-generated (ULID/UUIDv7 text/uuid), timestamps are `timestamptz` UTC. Child tables reference parents with `(tenant_id, id)` composite FKs so cross-tenant references are impossible. Row-level security is an open question (Q3).

| Milestone | Tables | Key columns / constraints |
|---|---|---|
| M2 | `tasks` | `tenant_id, task_id, spec jsonb (validated), spec_hash, created_at`; unique `(tenant_id, task_id)` |
| M2 | `task_runs` | `run_id, task_id, config_id (A–E), model_settings jsonb, runtime_version, analyzer_version, status enum, started_at, ended_at, correlation_id` |
| M2 | `trace_events` | `event_id, run_id, sequence, parent_event_id, type, component, component_version, action, status, metrics jsonb (size-capped), artifact_refs jsonb, ts`; unique `(run_id, sequence)`; append-only (trigger rejects UPDATE/DELETE); index `(tenant_id, run_id, sequence)` |
| M2 | `artifacts` | `artifact_id, tenant_id, uri, sha256, size, content_type, redaction_status`; unique `(tenant_id, sha256)` |
| M2 | `run_outcomes` | `run_id unique, status enum(VERIFIED_SUCCESS/FAILURE/PARTIAL/UNKNOWN), quality_score, tokens_used, tool_calls, execution_time_ms, human_interventions, failure_reasons, evidence_refs, verifier_version` |
| M2 | `policy_decisions` | `decision_id, run_id, step_index, policy_version, feature_snapshot jsonb, eligible_actions jsonb, estimates jsonb, selected_action, selection_probability null, reason, outcome_status null (filled after verification)` |
| M2 | `outbox_events` | `event_id, tenant_id, type, aggregate_id, payload jsonb, correlation_id, status, attempts, next_attempt_at, published_at`; partial index on unpublished |
| M2 | `consumed_events` (inbox) | unique `(consumer, event_id)` for idempotency |
| M2 | `learning_jobs` | unique `(job_type, dedupe_key)` e.g. `(run_id, pipeline_version)`; state, attempts, last_error, dead-lettered flag |
| M2 | `audit_log` | `actor, action, entity_type, entity_id, before, after, ts, correlation_id` |
| M3 | `experience_cases` | `tenant_id, run_id, task_family, features jsonb, outcome_status, pipeline_version, trajectory_artifact_id` |
| M3/M4 | `capabilities`, `capability_versions` | `definition jsonb` immutable (trigger), `definition_hash`, `state`, `compiler_version`, `prompt_version`, `source_run_ids` |
| M3/M4 | `capability_evidence`, `capability_edges` | evidence rows link version↔run; relational dependency edges |
| M4 | `evaluation_datasets`, `capability_evaluations` | dataset `split` enum (`DISCOVERY/VALIDATION/HIDDEN_TRANSFER`), version, content hash; evaluation: candidate version, dataset version, seeds, model/runtime/env-image versions, metrics, verdict |
| M4 | `rollouts` | per-version state transitions, canary %, rollback pointer to previous ACTIVE |
| M6 | `policy_versions` | immutable policy artifact, state, training-data window, calibration report |
| M7 | `memory_items` (+pgvector) | only if M7 experiment justifies |
| M1 | *(none)* | benchmark results written as versioned JSON/JSONL files via a `ResultStore` port |

## 6. Workflows

### Execution (synchronous request path; never waits on learning)
1. API/orchestrator validates `TaskSpec` (tenant, budgets, permissions) → assigns `task_id`, `run_id`, `correlation_id`.
2. Analyzer → features (version recorded). 3. Context compiler (null in M1–M6). 4. Controller selects action from the eligible set; decision persisted. 5. Runtime executes via tool gateway (allow-list, budgets, timeouts); trace events appended. 6. Independent verifier produces `RunOutcome` from environment evidence. 7. **One transaction:** run status + outcome + decision outcome back-fill + `outbox_events(outcome.verified)`. 8. Return result.

### Learning (asynchronous)
`outbox publisher → RabbitMQ → collector (inbox dedupe) → trajectory builder → experience case (verified outcomes only; UNKNOWN/PARTIAL recorded but excluded from success mining) → miner/compiler → candidate (CANDIDATE) → evaluation (M4) → promotion state machine → shadow → canary → ACTIVE`; rollback restores previous ACTIVE. Handlers are at-least-once/idempotent via `learning_jobs` dedupe keys; bounded retries with backoff + jitter, dead-letter queue, and a reconciliation job for stale outbox rows.

### Isolation of untrusted artifacts
Candidates and policies are data only. Normal execution queries the registry for `ACTIVE` (later `CANARY` by routing) versions; no code path loads a `CANDIDATE`. Evaluation runs in a separate worker with its own allow-list and no access to hidden datasets from the compiler process.

## 7. Implementation plan (concrete)

### Milestone 1 — Baseline agent and reproducible benchmark (smallest vertical slice, no DB/MQ/API)
New dependencies (justified): `pydantic`, `pytest`, `ruff`, `mypy` — nothing else. Agent logic is behind `LLMClient`; a deterministic `FakeLLM` drives CI, and a real provider adapter is optional/behind config.

Files:
- `pyproject.toml`, `uv.lock`, `.github/workflows/ci.yml`, `.gitignore`
- `src/experience_engine/domain/{ids,time,task,outcome,trace,benchmark}.py`
- `src/experience_engine/ports/{llm,tools,verifier,artifacts,clock}.py`
- `src/experience_engine/execution/{orchestrator,analyzer,controller,runtime,verifier,context}.py`
- `src/experience_engine/bench/{harness,report,store}.py`, `bench/families/<family>.py`
- `benchmarks/` dataset v1 (`discovery`, `validation`, `hidden`) with a manifest + content hashes
- `tests/unit/*`, `tests/architecture/test_import_boundaries.py`, `tests/regression/test_baseline_reproducible.py`
- `docs/baseline-report.md` (generated), `docs/decisions/` updates

Interfaces (sketch):
```python
class LLMClient(Protocol):  def complete(self, req: LLMRequest) -> LLMResponse: ...   # returns token usage
class ToolGateway(Protocol): def call(self, run: RunContext, tool: str, args: Mapping[str, object]) -> ToolResult: ...
class OutcomeVerifier(Protocol): def verify(self, task: TaskSpec, env: EnvironmentHandle) -> RunOutcome: ...
class ResultStore(Protocol): def save_run(self, record: BenchmarkRunRecord) -> None: ...
class Controller(Protocol): def decide(self, ctx: ControllerInput) -> ControllerDecision: ...  # M1: FixedPolicy
```
Benchmark domain: deterministic, locally simulated "connection-pool / API-timeout debugging" tasks (config files + log fixtures + an objective checker over resulting config/test results) — matches the roadmap's recommended experiment, needs no Docker, and is verifiable from environment state, not agent claims.

Acceptance tests:
1. Re-running the same dataset version + seed + FakeLLM yields identical result records (hash-equal) and a comparable diff.
2. Verifier returns `FAILURE` when the agent claims success but the environment check fails (and `UNKNOWN` when evidence is missing).
3. Run record captures success, tokens, tool calls, latency, human interventions, dataset/seed/model/runtime/env versions.
4. Fresh-session mode: no state leaks between runs (second run with state wiped equals first).
5. Budget/step/tool-allowlist violations terminate the run with a recorded reason.
6. Architecture test: pure packages import no FastAPI/Celery/SQLAlchemy/LangGraph/LLM SDK.
7. Hidden split is not importable/readable via the discovery/compiler-facing dataset loader.
8. `docs/baseline-report.md` generated, contains no secrets.

### Milestone 2 — Tracing, persistence, outbox
Add `sqlalchemy`, `alembic`, `psycopg`, `celery`, `fastapi` (justification: Postgres/RabbitMQ semantics cannot be faked for outbox/idempotency tests). Files: `adapters/db/*`, `alembic/versions/0001_core.py`, `adapters/mq/*`, `adapters/artifacts/local_fs.py`, `api/*`, `deploy/docker-compose.yml`, `docs/schema.md`, `docs/events.md`, `tests/integration/*`, `tests/migration/*`. Acceptance: roadmap M2 list, plus migration up/down on empty and populated DB, trigger-enforced append-only, secret-redaction test, tenant isolation test, kill-publisher recovery test, duplicate-delivery test.

### Milestones 3–8 (outline only; detailed plans produced at each milestone start)
- **M3:** `domain/capability.py`, `capabilities/{dsl,validator}.py`, `learning/{trajectory,miner,compiler}.py`, alembic `0002`. Tests: schema rejections (unknown op, code strings, missing preconditions, bad tool), provenance, deterministic compile with recorded versions, candidates absent from execution queries.
- **M4:** `capabilities/{executor,registry}.py`, `learning/{evaluation,promotion}.py`, `0003`. Executor isolation: interpreter over DSL only, no `eval/exec/subprocess/import`; limits tested. Promotion config per risk tier; source replay alone cannot promote.
- **M5:** `execution/{matcher,runtime}` extensions; eligibility = deterministic checks, retrieval advisory. Needs fresh-session transfer win to proceed.
- **M6:** `execution/controller.py` rule policy, `learning/policy_trainer.py`, `policy_versions`; offline replay, shadow, canary.
- **M7:** real `execution/context.py`; pgvector only if lexical/structured baseline is beaten; negative result acceptable.
- **M8:** optional; separate ADR and human approval before any work.

## 8. Risks and architectural gaps (design issues to resolve)

| ID | Gap / risk | Proposed resolution |
|---|---|---|
| G1 | `RunOutcome` lacks `task_id`, `tenant_id`, `config_id`, `capability_version`, `policy_version`; `TaskSpec.success_criteria` is untyped `dict`. | Add fields; make `SuccessCriterion` a discriminated union (M1). |
| G2 | Rule 10 requires controller decisions logged, but controller arrives in M6. | M1 ships a `FixedPolicy` that emits full `ControllerDecision` records (single eligible action); table added in M2. |
| G3 | DSL executor appears in M4 (evaluation) and M5 (runtime). | Build once in M4; M5 only adds matching/eligibility. |
| G4 | `Never trust self-report` vs. verifier needing env access. | Verifier operates on environment evidence in the benchmark env; agent output is never an input to the pass/fail decision. |
| G5 | Event ordering/idempotency under at-least-once delivery. | Inbox table + `(run_id, pipeline_version)` dedupe keys; outbox ordering per aggregate. |
| G6 | Hidden-transfer data protection is stated but unspecified. | Separate manifest + loader; hidden loader requires evaluator role (distinct DB role/path), absent from compiler worker config; hashes recorded; tested in M1/M4. |
| G7 | Tenant isolation in mining/retrieval is stated but needs mechanics. | Composite tenant FKs, repository APIs require `tenant_id`, isolation tests per data path; RLS decision pending (Q3). |
| G8 | Lifecycle cost accounting has no schema. | Add `cost_ledger` (learning/eval/maintenance tokens, compute, tool calls) by M3/M4. |
| R1 | Greenfield + large scope → premature infrastructure. | Strict milestone gating; M1 has no DB/MQ/API. |
| R2 | LLM nondeterminism hurts reproducibility. | FakeLLM for CI; record provider/model/params/seed; report variance with multiple seeds for real models. |
| R3 | Small benchmarks give no statistical power for "improvement". | Report uncertainty/CIs; enlarge task families before M5 gate. |
| R4 | Integration tests need Docker (daemon not running in this environment). | Compose-based CI services; Docker needed before M2. |
| R5 | Benchmark overfitting / leakage via traces. | Split by family/config/time; hidden set untouched by learners. |

## 9. Proposed standard commands (to be created in M1; not yet runnable)

```
uv sync
uv run ruff check . && uv run ruff format --check .
uv run mypy src
uv run pytest tests/unit tests/architecture tests/regression
# from M2:
docker compose -f deploy/docker-compose.yml up -d postgres rabbitmq
uv run alembic upgrade head
uv run pytest tests/integration tests/migration
```
Baseline test state today: no tests or commands exist, so none were run.

## 10. Assumptions and open questions

Assumptions: greenfield defaults apply; Python 3.12 + `uv`; single-repo modular monolith; GitHub Actions for CI; simulated debugging benchmark for M1.

Open questions for review:
1. **Benchmark domain:** approve the simulated connection-pool/API-timeout debugging family for M1 (vs. a real-repository SWE-style harness)?
2. **LLM provider:** which provider/model for non-fake runs, and are real-model runs in scope for M1 or only FakeLLM?
3. **Tenancy:** application-level scoping only, or Postgres RLS from M2?
4. **Identifiers:** ULID vs UUIDv7?
5. **Artifact store:** local filesystem adapter acceptable until S3/MinIO is required?
6. **CI:** is GitHub Actions the target, and is Docker available on runners/dev machines?
7. **Packaging:** `uv` + `pyproject.toml` acceptable as the dependency manager?
8. **Initial commit/branching:** commit the blueprint docs to `main` first, then one PR per milestone?
