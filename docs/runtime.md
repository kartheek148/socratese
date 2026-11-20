# Runtime capability matching and execution (Milestone 5)

A fresh session can now serve a task from a capability that was learned elsewhere. Nothing from
the original conversation is available at runtime: the only inputs are the task, the environment
and the capability catalog in PostgreSQL.

## Flow

```
task + environment facts
        │
        ▼
CapabilityMatcher ── catalog (ACTIVE only, tenant-scoped, read-only)
  1. retrieval  : LexicalRetriever ranks/shortlists          (advisory, may be wrong)
  2. eligibility: check_eligibility() per shortlisted item   (deterministic, ignores similarity)
        │  MatchResult(eligible, rejected + reasons)
        ▼
RuleCapabilityPolicy (rule-capability/1)  → ControllerDecision (persisted, rule 10)
        │
        ├─ GENERAL_PLANNING        no eligible capability → baseline AgentRuntime
        ├─ DIRECT_CAPABILITY       best eligible capability, no dependencies
        ├─ COMPOSE_CAPABILITIES    best eligible capability has SUBCAPABILITY dependencies
        └─ HUMAN_ESCALATION        a capability reached HUMAN_APPROVAL / escalated
        ▼
TaskOrchestrator._execute_strategy → CapabilityExecutor (sandboxed DSL interpreter)
        │
   COMPLETED ─────────────► independent verifier (final environment state) decides success
   FALLBACK / NOT_APPLICABLE ► FALLBACK trace event → general planning with the REMAINING budget
   ABORTED (structural/safety) ► run ends, no fallback
   ESCALATE ────────────────► run ends, human_interventions = 1
```

Code: [`execution/matcher.py`](../src/experience_engine/execution/matcher.py),
[`execution/controller.py`](../src/experience_engine/execution/controller.py) (`RuleCapabilityPolicy`),
[`execution/capability_runtime.py`](../src/experience_engine/execution/capability_runtime.py)
(`CapabilityRuntime`, `TracingToolGateway`),
[`execution/orchestrator.py`](../src/experience_engine/execution/orchestrator.py),
[`adapters/db/catalog.py`](../src/experience_engine/adapters/db/catalog.py),
[`capabilities/executor.py`](../src/experience_engine/capabilities/executor.py).

## Eligibility (`check_eligibility`)

Retrieval only orders and shortlists. A capability may run only if **all** of these hold, whatever
its similarity score:

1. the definition passes static validation again (tampering after registration is caught);
2. every precondition holds (task family, tools available, file roles present, risk ceiling);
3. `permissions ⊆ task.allowed_tools`;
4. capability budgets fit the task: `max_steps`, `max_tokens`, `timeout_ms ≤ deadline_ms`, number of
   `TOOL_CALL` steps `≤ max_tool_calls`;
5. every declared dependency resolves to an executable (ACTIVE) version in the same tenant, the
   dependency graph has no cycle and is at most 3 deep, and no dependency needs a tool its parent does
   not declare.

Only ACTIVE versions are executable (`EXECUTABLE_STATES`); the catalog returns nothing else.
`SqlExecutableCatalog` is read-only and tenant-scoped on every call.

## Composition (`SUBCAPABILITY`)

A parent may call a declared dependency. The child runs **inside every ancestor's budget**: steps,
tokens and wall-clock are shared counters and the effective limit is the minimum along the chain. The
child gets a fresh variable scope (only the arguments the parent passes), may only use tools the
parent also permits, must pass its own preconditions, and nesting is limited to depth 2 with cycle
detection. An unavailable (non-ACTIVE) or undeclared dependency stops the run before any tool call.

## Fallback, abort, escalation

* **Fallback** (precondition failure, tool failure, decision failure, verification failed, budget
  exhausted): general planning continues with the *remaining* tokens, steps, tool calls and deadline.
  Budgets are never reset; if nothing is left the run terminates with that budget's reason.
  Caveat: the capability may already have modified the environment. The planner works on the current
  state and the independent verifier judges the final state. In the simulated benchmark this is safe
  (idempotent config edits); for real systems capabilities need declared rollback before this is
  acceptable (deferred).
* **Abort** (`TOOL_NOT_ALLOWED`, `TENANT_MISMATCH`, `INVALID_DEFINITION`, cancellation): structural
  or safety problem, no fallback.
* **Escalate**: a `HUMAN_APPROVAL` step ends the run with `human_interventions = 1`.

## What is recorded

* `policy_decisions`: eligible actions, `feature_snapshot` (catalog size, candidates with similarity,
  eligibility and rejection reasons — capped, selected `id@version`), similarity estimates,
  `selection_probability` 1.0 (deterministic rule), reason, and the verified outcome status.
* `run_outcomes.capability_version = "id@version"` and `config_id = C_CAPABILITIES`.
* Trace events: `DECISION`, `CAPABILITY_MATCH` (all candidates), `TOOL_CALL` for every capability
  tool call (component `capability_tool_gateway`), `CAPABILITY_EXEC` (status, reason, step log),
  `FALLBACK`, `RUN_TERMINATED`, `VERIFICATION`. No schema migration was needed: types are free text.
* Runs that used (or attempted) a capability are **excluded from learning**
  (`ExclusionReason.CAPABILITY_ASSISTED`) so the system cannot learn from its own output as if it
  were independent evidence.

## Running it

```
# ACTIVE capabilities only (production path)
uv run ee-bench run --llm vertex --capabilities --split hidden_transfer --out runs/c.jsonl
uv run ee-bench report-compare --baseline runs/a.jsonl --candidate runs/c.jsonl --out docs/cmp.md
```

`--experimental-capability-states STATE…` additionally serves capabilities in other states
(e.g. `REJECTED`). It exists only to measure the *mechanism* offline: runs are tagged
`capability_catalog=experimental`, a warning is printed, and the comparison report carries a banner
saying the result is not promotion evidence. It is never used by production wiring.

## Fresh-session result and gate status

Real Gemini 2.5 Flash, 16 private validation + hidden-transfer tasks, arm A vs arm C, every run in a
new session served only from the database catalog: both arms 16/16 verified; arm C used
~1.8k vs ~5.0k tokens per success and ~12 s vs ~33 s latency. Full report with caveats:
[evaluations/runtime-fresh-session-gemini-2.5-flash.md](./evaluations/runtime-fresh-session-gemini-2.5-flash.md).

Roadmap gate "fresh-session transfer benchmark shows measurable value before proceeding":
* **Mechanism and cost value: demonstrated** (fresh session, no transcript, deterministic gate, real model).
* **Correctness value: not demonstrated.** The baseline is already at the ceiling on this benchmark.
* **Caveat that matters:** the capabilities were *unpromoted candidates* served from an experimental,
  tagged catalog. With the production (ACTIVE-only) catalog nothing is served until a version passes
  the M4 evaluation and rollout. A harder benchmark with headroom (risk R3) is the most valuable
  input before relying on this for M6/M7 claims.

## Not in M5 (deferred)

* Live SHADOW/CANARY routing of real tasks (`capabilities/rollout.py` exists; adaptive rollout is M6).
* Learned strategy selection (M6): `RuleCapabilityPolicy` is a fixed rule with probability 1.0.
* Embedding retrieval: `CapabilityRetriever` is a port; the lexical retriever is a placeholder.
* Rollback/undo for capabilities that already mutated state before a fallback.
