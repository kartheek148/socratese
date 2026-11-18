# Repository instructions: Experience-Driven Self-Improving Agent

## Product goal
Build an agent that improves future task execution from verified experience. It must convert execution traces into reusable, testable capabilities; select capabilities adaptively; and demonstrate better task success, transfer, or lifecycle efficiency. Storing more conversations or generating more skills is not, by itself, improvement.

## Architecture boundaries
- Keep the first implementation a modular monolith with explicit interfaces. Do not introduce microservices, a graph database, online foundation-model fine-tuning, or autonomous source-code modification without an approved milestone and measured need.
- Separate the **execution plane** (task analysis, context retrieval, controller, runtime, verification) from the **learning plane** (trace collection, trajectory analysis, candidate compilation, evaluation, promotion, policy training).
- Execution must not wait for asynchronous learning. A completed run is persisted and an outbox event is published; background workers perform learning.
- Treat generated capabilities and policies as untrusted candidates. Only the evaluation/promotion workflow may make a candidate available to normal execution.
- Preserve a fixed-model, no-learning baseline and reproducible benchmark results throughout development.

## Preferred implementation defaults
Use the repository's existing conventions first. If this is a greenfield repository, prefer Python, FastAPI, Pydantic, PostgreSQL, SQLAlchemy/Alembic, pgvector where semantic retrieval is justified, RabbitMQ/Celery for asynchronous jobs, and a LangGraph adapter where graph orchestration is useful. Keep provider, model, vector-store, object-store, and agent-framework dependencies behind interfaces. Do not upgrade dependencies or change framework versions without checking the repository lockfiles and compatibility.

## Non-negotiable engineering rules
1. Inspect the repository, tests, configuration, and existing patterns before editing. Reuse existing abstractions; do not replace working infrastructure merely to match this document.
2. Implement one milestone at a time. Do not silently implement future milestones. At the start, state the milestone, scope, assumptions, and files expected to change.
3. Prefer small, typed, testable modules. Use dependency injection for external services. Keep business logic independent of FastAPI, Celery, LangGraph, and database adapters.
4. Use Pydantic/typed schemas at boundaries, explicit enums for state, UTC timestamps, stable IDs, tenant scoping, and database migrations for schema changes.
5. Make event consumers idempotent. Use a transactional outbox for events emitted as part of database transactions. Add bounded retries, dead-letter handling, and correlation IDs.
6. Persist execution traces append-only. Store large logs, patches, and tool payloads as artifact references; do not put unbounded payloads in relational event rows.
7. Never execute arbitrary model-generated Python, shell, SQL, or code in the agent process. Capabilities must use a validated, allow-listed DSL and isolated execution adapters with strict time, step, tool, network, and resource limits.
8. Separate candidate generation, evaluation, approval, rollout, and rollback. LLM self-assessment is not verification. Use independent tests or environment evidence.
9. Do not learn from `UNKNOWN`, unverified, or partial outcomes as if they were successes. Record uncertainty and provenance. Do not let tenant data cross tenant boundaries.
10. Every controller decision must record the eligible action set, selected action, policy version, estimates, selection probability (when applicable), reason, and subsequent verified outcome.
11. Keep hard safety, permission, and reliability constraints outside learned reward optimization. The controller may optimize cost only among eligible actions that meet those constraints.
12. Add unit, integration, migration, and regression tests for every behavior change. Run relevant tests, lint, and type checks; report exact commands and results. Never claim tests passed unless they were run.
13. Do not add new infrastructure or dependencies without explaining why the current stack is insufficient. Provide configuration examples and safe defaults.
14. Update architecture/API/schema docs when contracts or state transitions change.

## Definition of improvement
Evaluate on held-out and transfer tasks, not only replay of source examples. Report success/quality, tokens and tool calls per verified success, latency, human interventions, learning cost, lifecycle cost, transfer performance, and regression/retention. Include the cost of learning and maintaining capabilities. A lower token count is not an improvement if correctness, safety, or required verification falls.

## Implementation workflow
- Read `docs/architecture.md` and `docs/implementation-roadmap.md` before implementation.
- Follow `.github/prompts/implement-milestone.prompt.md` for milestone work.
- If the repository differs from the proposed design, document the difference and adapt the design rather than forcing a rewrite.
- Before coding, identify the smallest vertical slice and acceptance tests. After coding, summarize changed files, migrations, API/event contract changes, tests run, risks, and deferred work.
