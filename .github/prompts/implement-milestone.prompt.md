# Implement one Experience Engine milestone

You are working on the Experience-Driven Self-Improving Agent described in `docs/architecture.md` and `docs/implementation-roadmap.md`.

## Inputs
- Milestone: [MILESTONE_ID_AND_NAME]
- Additional constraints: [OPTIONAL_CONSTRAINTS]

## Required workflow
1. Read `.github/copilot-instructions.md`, `docs/architecture.md`, and `docs/implementation-roadmap.md`.
2. Inspect the actual repository structure, dependency/lock files, migrations, existing runtime, queue, auth/tenant model, tests, lint/type-check commands, and CI. Reuse existing patterns. Do not assume the proposed package names already exist.
3. Summarize the current implementation relevant to this milestone and identify the smallest vertical slice.
4. State the scope, out-of-scope work, files likely to change, schema/API/event changes, risks, and acceptance tests before editing.
5. Implement only the requested milestone. Do not implement later milestones or introduce unrelated refactors.
6. Add or update tests alongside the implementation. Use fakes for unit tests and real PostgreSQL/RabbitMQ integration tests where the behavior depends on them.
7. Run the relevant tests, migrations, lint, and type checks available in the repository. Report exact commands and actual results. If a check cannot run, explain why; do not claim it passed.
8. Update architecture, ADR, API, event, and data-model docs if contracts changed.

## Required engineering constraints
- Keep execution and learning planes separate. Learning is asynchronous and must not delay task completion.
- Persist immutable source traces and evidence; derived summaries must not replace them.
- Use typed contracts, explicit state transitions, stable IDs, UTC timestamps, tenant scoping, and database migrations.
- Use a transactional outbox for DB-transaction-coupled events and idempotent consumers for at-least-once delivery.
- Treat candidate capabilities and policy versions as untrusted until independently evaluated and promoted.
- Never execute arbitrary LLM-generated code, shell, or SQL. Use only approved DSL operations and allow-listed tools with bounded budgets.
- Do not use LLM self-reported success as ground truth. Use independent verification.
- Keep reliability, permission, safety, and risk constraints outside learned reward optimization.
- Record model, prompt/compiler, policy, capability, dataset, and environment versions needed to reproduce decisions and evaluations.
- Include learning and maintenance costs in lifecycle measurements.

## Completion response format
1. Milestone and scope delivered.
2. Architectural decisions and assumptions.
3. Files/modules changed.
4. Database/API/event contract changes.
5. Tests/checks run and exact results.
6. Security, data isolation, and operational considerations.
7. Known limitations and deferred milestones.
8. Suggested next milestone (do not implement it).

If the repository's current architecture materially conflicts with the proposed design, stop before a broad rewrite. Explain the mismatch and propose the smallest compatible adaptation.
