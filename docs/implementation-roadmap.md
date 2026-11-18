# Implementation Roadmap and Milestones

## Delivery rule
Implement exactly one milestone per PR. Each milestone must leave the main branch runnable. Do not skip ahead to policy training, PMC optimization, or self-evolution before the prerequisite evidence and interfaces exist.

## Milestone 0 — Repository discovery and architecture fit

**Goal:** Understand the current codebase and map this design to existing patterns without a rewrite.

**Work:**
- Inspect package structure, dependency management, database/ORM, migrations, queue, agent runtime, tool abstractions, auth/tenant handling, testing, observability, and CI.
- Identify existing components to reuse and gaps.
- Create/update `docs/architecture.md`, `docs/decisions/` (ADR records), and a component map.
- Record implementation assumptions and open questions.

**Acceptance:**
- No functional code changes unless needed for a small baseline test.
- Architecture fit/gap report exists.
- Existing tests and standard commands are documented and run.
- Proposed milestones are mapped to real repository paths.

## Milestone 1 — Baseline agent and reproducible task benchmark

**Goal:** Establish a trustworthy baseline before adding learning.

**Work:**
- Define task and success-criteria contracts.
- Build a small, deterministic task-family harness (start with software debugging or another environment with objective verification).
- Implement a fixed agent configuration and independent verifier.
- Store benchmark dataset versions, seeds, environment versions, model/provider settings, and run results.
- Add a no-learning baseline and fresh-session test mode.

**Acceptance:**
- Same benchmark can be rerun and results compared.
- Verifier does not trust the agent's self-reported success.
- Task success, token use, tool calls, latency, and human interventions are captured.
- A baseline report is generated and committed without embedding secrets.

## Milestone 2 — Execution tracing, outcome persistence, and reliable events

**Goal:** Capture enough evidence to reconstruct why the agent acted as it did.

**Work:**
- Add task/run IDs and structured trace events.
- Record model calls, tool calls, observations, decisions, retries, capability/policy versions, and verifier events.
- Store large payloads as artifact references.
- Add database tables and Alembic migrations.
- Implement transactional outbox and idempotent RabbitMQ/Celery consumer.
- Add tenant scoping and audit metadata.

**Acceptance:**
- A run can be reconstructed from persisted events and artifacts.
- Repeated delivery does not duplicate learning work.
- Outbox recovery works after simulated process failure.
- No secret values are written to traces.
- Integration tests cover event ordering, retries, and tenant isolation.

## Milestone 3 — Experience normalization and candidate capability compiler

**Goal:** Generate structured capability candidates from multiple related experiences, without activating them.

**Work:**
- Build trajectory reconstruction and normalization.
- Group experiences by task family and environment features.
- Implement contrastive analysis of verified success/failure trajectories.
- Define the capability JSON/Pydantic schema and restricted DSL.
- Generate candidates with preconditions, steps, typed inputs/outputs, termination, verification, failure boundaries, budgets, provenance, and tests.
- Track whether a run adds evidence to an existing capability rather than always generating a new one.

**Acceptance:**
- Candidates are traceable to source run IDs.
- Invalid schemas, missing preconditions, unsafe operations, and arbitrary code are rejected.
- Candidate generation is deterministic enough to compare versions; prompt/model/compiler versions are recorded.
- Candidates are not available to normal task execution.

## Milestone 4 — Isolated evaluator and versioned capability registry

**Goal:** Prove or reject candidate quality before runtime use.

**Work:**
- Implement immutable capability versions and lifecycle states.
- Build sandboxed DSL execution with strict tool allowlists, budgets, timeouts, and tenant isolation.
- Add source-task replay, held-out validation, transfer tests, negative applicability tests, and regression tests.
- Compare success and lifecycle cost with baseline and memory-only controls.
- Implement promotion policy, shadow mode, canary, audit trail, and rollback.

**Acceptance:**
- Source-task replay alone cannot promote a capability.
- Hidden transfer tests are inaccessible to candidate generation.
- Promotion criteria are explicit/configurable by risk class.
- Failed versions can be rolled back without losing the previous active version.
- Tests demonstrate isolation and execution limits.

## Milestone 5 — Runtime capability matching and execution

**Goal:** Allow the agent to use approved capabilities in fresh sessions.

**Work:**
- Implement candidate retrieval plus deterministic eligibility checks.
- Add a capability executor for the approved DSL.
- Support direct capability, capability composition, general LLM planning, and human escalation.
- Enforce preconditions, required permissions, budgets, termination, fallback, and verification.
- Record which capability version was used and why.

**Acceptance:**
- Only approved/active versions are executable by default.
- A new session can use a capability without access to the original conversation.
- Ineligible capabilities are rejected even when vector similarity is high.
- Runtime falls back safely when preconditions fail or execution becomes uncertain.
- Fresh-session transfer benchmark shows measurable value before proceeding.

## Milestone 6 — Adaptive strategy controller

**Goal:** Learn when to use a capability, compose capabilities, or plan from scratch.

**Work:**
- Implement a rule-based baseline controller with explicit feasibility constraints.
- Log eligible actions, context features, estimates, selected action, propensity, policy version, and outcome.
- Add success-probability and cost estimates with calibration checks.
- Introduce a contextual bandit only after sufficient verified data exists.
- Restrict exploration to approved low-risk tasks; keep safety and reliability constraints outside the learned policy.
- Evaluate offline, in shadow, and through canary rollout.

**Acceptance:**
- Decisions are reproducible from stored features and policy version.
- Controller optimizes total cost subject to the required success/risk threshold.
- It beats the rule-based baseline on held-out task sequences, or the team documents evidence that it does not.
- No regression in protected/high-risk scenarios.

## Milestone 7 — Predictive Memory Compiler integration

**Goal:** Test whether task-aware memory selection adds value beyond existing retrieval and capability reuse.

**Work:**
- Implement episodic, semantic, and procedural context selection.
- Add provenance, freshness, confidence, tenant boundaries, and counterexamples.
- Estimate retrieval/context cost and usefulness.
- Compare lexical/structured retrieval, ordinary vector retrieval, PMC, and no-memory baselines.
- Measure lifecycle cost including memory compilation and maintenance.

**Acceptance:**
- PMC improves held-out task quality, transfer, or lifecycle efficiency over appropriate controls.
- It does not merely increase context size or retrieve more items.
- A negative result is recorded as a valid outcome; do not force PMC into runtime if it provides no measured value.

## Milestone 8 — Bounded self-evolution (optional research milestone)

**Goal:** Let the system propose and evaluate changes to its own bounded harness components.

**Allowed initial targets:** strategy features, routing rules, capability compiler prompts/templates, non-security-critical workflow composition, and cost prediction models.

**Forbidden targets:** hidden evaluation data, verifier integrity, promotion criteria, tenant isolation, permissions, safety constraints, credentials, and deployment authorization.

**Work:**
- Generate candidate agent/harness versions in isolated branches or containers.
- Run the complete benchmark matrix and regression suite.
- Compare lifecycle cost and transfer performance.
- Keep immutable lineage and reproducible artifacts.
- Promote only through an independently controlled release process.

**Acceptance:**
- Candidate cannot modify evaluator or hidden data.
- Candidate improves against multiple seeds and held-out tasks.
- Full rollback exists.
- Human release approval is required initially.

## Cross-milestone definition of done

- Unit, integration, migration, and regression tests are present.
- Security/tenant tests cover new data paths.
- Relevant lint/type-check/test commands pass and are reported truthfully.
- API, event, schema, and state-machine contracts are documented.
- Metrics and logs include correlation IDs and redact sensitive data.
- No future milestone is implemented implicitly.
- An experiment report states the hypothesis, controls, dataset version, model/runtime versions, results, uncertainty, and limitations.

## Recommended first experiment

Use a narrow domain with repeatable outcomes, such as database connection-pool and API timeout debugging. Create task families with variations in libraries, configurations, repositories, and failure signatures. Compare:
- A: fixed baseline agent, no persistent learning;
- B: episodic memory retrieval only;
- C: compiled capabilities, fixed strategy selection;
- D: compiled capabilities plus adaptive controller;
- E: D plus PMC.

Measure verified success, unseen-task transfer, tokens/tool calls/latency per verified success, human interventions, learning cost, lifecycle cost over N tasks, false applicability, and regression on previously solved tasks. Keep the final transfer set hidden from all learning components.
