# ADR 0007 — Milestone 4 decisions: evaluator, promotion policy, registry

Status: Accepted (Milestone 4)

## Decisions
1. **Evaluation lives in its own package (`evaluation/`) that learning cannot import.** It needs the benchmark, the executor and private datasets; the learning plane must have none of them. Enforced by the architecture test (layering + explicit forbidden-module test) and by `ee-learn` refusing evaluator access while `ee-eval` requires it.
2. **Evaluation runs are not task runs.** They create no `task_runs`, outbox events, learning jobs or experience cases, so hidden/validation outcomes cannot leak into learning (tested structurally).
3. **Executor is an interpreter with hard limits, re-validating definitions.** In-process and allow-list based (no dynamic execution primitives); the task environment and tool gateway are the isolation boundary in the simulated benchmark. A real OS/container sandbox is deferred until capabilities act on real systems.
4. **Verifier independence preserved.** Arm C's own `VERIFY` result is advisory; every arm is scored by the benchmark verifier from final environment state.
5. **Promotion criteria are explicit data.** Per-risk-class `PromotionPolicy`, hashed, optionally loaded from a reviewed file; all gates are conjunctive; insufficient evidence is `INCONCLUSIVE`; a policy change invalidates earlier evaluations. A capability with no declared risk ceiling is treated as `high`.
6. **Controls and cost.** Arms A (baseline) and B (memory-only hint) are run alongside C. Lifecycle cost charges learning and control-arm tokens to the capability and is reported next to the baseline's execution cost; it is a report, and only the optional execution-token ratio is a gate (never able to override correctness).
7. **Registry transitions are atomic, audited and DB-guarded.** A trigger encodes the legal graph and the PASS-evaluation requirement; a CHECK makes source-replay-only evidence incapable of PASS; a partial unique index allows one ACTIVE version per capability; rollback restores the previous version in the same transaction via `capability_pointers`. `DEPRECATED → ACTIVE` was added to the graph solely for that restore (service-enforced).
8. **No outbox events for registry changes yet.** State changes are audited in `audit_log` in the same transaction; no consumer exists, and the Celery publisher only routes `execution.completed`. Emitting `capability.state_changed` through the outbox is deferred until a consumer (M5) needs it.
9. **Shadow/canary are modeled, not wired.** M4 provides deterministic routing, observation storage and gates; M5 routes real tasks.

## Consequences / deferred
Runtime matching/execution (M5); adaptive controller (M6); stronger memory control (M7). Open: OS-level sandbox, separate DB roles for evaluator/registry writes, larger and harder benchmark suites (the baseline already passes most tasks, leaving little headroom), multiple seeds per evaluation.
