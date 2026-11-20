# Evaluation, promotion and the capability registry (Milestone 4)

A compiled candidate (M3) is untrusted. In M4 it earns — or fails to earn — the right to be used, through an isolated evaluator, an explicit promotion policy and an audited registry. Nothing here wires capabilities into task execution; that is milestone 5.

Code: [`capabilities/executor.py`](../src/experience_engine/capabilities/executor.py), [`applicability.py`](../src/experience_engine/capabilities/applicability.py), [`registry.py`](../src/experience_engine/capabilities/registry.py), [`rollout.py`](../src/experience_engine/capabilities/rollout.py); [`evaluation/`](../src/experience_engine/evaluation); [`eval_cli.py`](../src/experience_engine/eval_cli.py); [`adapters/db/registry_store.py`](../src/experience_engine/adapters/db/registry_store.py); migration `0003`.

## Sandboxed executor

`CapabilityExecutor` interprets the DSL; it is not a code runner. It has no `eval`/`exec`/import/subprocess/file/network access (an AST test enforces this on the module), resolves values only through structured references, and performs every tool call through the injected, task-scoped `ToolGateway`. Enforced on every step: maximum executed steps (nested/retried steps count), token budget, wall-clock timeout, cancellation, tool allowlist (`definition.permissions ∩ task.allowed_tools ∩ approved tools`), argument contract, tenant boundary, and deterministic path-role resolution that **refuses ambiguity** (exactly one file must match a role). The definition is re-validated at run time (defense in depth against tampering). `LLM_DECISION` goes through a `DecisionProvider` with an approved template; its output must match the declared typed schema exactly or the step fails.

Outcomes: `COMPLETED`, `NOT_APPLICABLE` (a precondition failed, nothing ran), `FALLBACK` (budget/tool/decision/verification problem; safe to hand to general planning), `ABORTED` (tampered definition, tenant mismatch, cancellation, disallowed tool), `ESCALATE` (human approval needed). A `VERIFY` step calls the independent verifier; the executor never scores the task itself.

> Isolation here is *interpreter-level* plus a simulated, per-task in-memory environment and tenant checks. It is not an OS/container sandbox; a real deployment that executes against live systems needs one (deferred, see ADR 0007).

## Evaluation (`ee-eval evaluate`)

Requires evaluator access and **private** validation/hidden data (`--private-root`); refuses otherwise. Evaluation runs are **not** stored as task runs, never create outbox events or learning jobs, and cannot enter `experience_cases` (a test scans the package).

| Suite | Tasks | Purpose |
|---|---|---|
| SOURCE_REPLAY | discovery tasks of the family | necessary, **never sufficient** |
| HELD_OUT | validation tasks (same variants, new values) | generalization |
| TRANSFER | hidden-transfer tasks (new libraries/layouts) | transfer |
| NEGATIVE | other-family tasks + synthetic precondition violations (missing tool / file role) | the capability must refuse; any acceptance is a false applicability |
| REGRESSION | tasks the baseline already solves | the capability must not break them |

Arms, identical model/tools/budgets/verifier: **A** fixed baseline agent; **B** memory-only control (baseline + one retrieved episodic hint of a prior verified success; a minimal stand-in for configuration B, not the M7 memory compiler); **C** compiled capability. Every arm is scored from final environment state by the same verifier.

**Lifecycle cost** (reported, not hidden): `(learning tokens + control-arm evaluation tokens + capability execution tokens) / verified successes of C`, next to the baseline's execution-only tokens per success. Amortization over future use is deliberately not credited, so a capability looks expensive until it is reused enough.

## Promotion policy

`PromotionPolicy` per risk class (`low`/`medium`/`high`), versioned and hashed (`policy_hash`), loaded from built-in defaults or a reviewed JSON file (`--policy-file` / `EE_PROMOTION_POLICY_FILE`; all three classes required). A capability's class is its declared `risk_level_at_most` precondition; **no declaration ⇒ `high`** (strictest). All gates must hold; none can be traded for token savings:

* minimum evidence (held-out, transfer, negative-applicability counts) → otherwise `INCONCLUSIVE`, never `PASS`;
* source-replay, held-out and transfer success rates; Wilson 95% lower bound on held-out+transfer success;
* max false applicability (default 0), max regressions vs baseline;
* optional cost gate (execution tokens/success ≤ ratio × baseline);
* rollout gates: minimum shadow/canary observations, max shadow failure rate, min canary success, canary fraction.

Default `high` needs 8 held-out and 8 transfer tasks and so stays `INCONCLUSIVE` on the bundled benchmark by design. Promotion is also blocked if the policy changed after the evaluation (hash mismatch) — re-evaluate.

## Registry lifecycle

```
CANDIDATE -> VALIDATING -> APPROVED -> SHADOW -> CANARY -> ACTIVE
   evaluation verdict: PASS -> APPROVED, FAIL -> REJECTED, INCONCLUSIVE -> stays VALIDATING
   ACTIVE -> DEPRECATED (when a newer version is activated)   SHADOW/CANARY/ACTIVE -> ROLLED_BACK
   DEPRECATED -> ACTIVE only as the restore step of a rollback
```

`RegistryService` is the only code that moves states. Each change is **atomic and audited**: version state(s), `capability_pointers` (active/previous), `capability_transitions` and `audit_log` rows commit together, under row locks, with an expected-state check (`StaleStateError` on races). Activating v2 deprecates v1 and remembers it as `previous`; **rolling back v2 restores v1 in the same transaction**, so the previous active version is never lost. `rollout.route()` gives deterministic SERVE / SHADOW / SKIP decisions (canary by stable hash bucket) for later rollout work; M4 records shadow/canary *observations* (`ee-eval observe`) that the gates read.

### Database-enforced rules (also hold for direct SQL)

* trigger: only the legal transition graph (CANDIDATE→ACTIVE, skipping SHADOW, etc. are rejected); `APPROVED` requires a `PASS` evaluation row for that version; definitions stay immutable;
* CHECK: an evaluation can be `PASS` only if held-out, transfer and negative counts are > 0 ⇒ **source replay alone can never promote**;
* unique partial index: at most one `ACTIVE` version per capability;
* `capability_evaluations`, `capability_transitions`, `rollout_observations` are append-only.

## Operating

```
export EE_EVALUATOR_ROLE=evaluator EE_PRIVATE_DATASET_ROOT=/secure/path/ee-private   # see docs/benchmark.md
uv run ee-eval evaluate --tenant bench --capability pool.<hash> --version 1 --llm vertex --out docs/eval.md
uv run ee-eval observe  --tenant bench --capability pool.<hash> --version 1 --mode SHADOW
uv run ee-eval promote  --tenant bench --capability pool.<hash> --version 1 --to shadow|canary|active
uv run ee-eval rollback --tenant bench --capability pool.<hash> --version 2 --reason "regression"
uv run ee-eval status   --tenant bench
```
`ee-eval` refuses to run **without** evaluator access; `ee-learn` refuses **with** it. Learning-plane modules cannot import the registry, executor, evaluation or benchmark code (architecture test).

## First real-model evaluation (Gemini 2.5 Flash, one seed, private data)

Candidates compiled from 12 real discovery runs; evaluated with arms A/B/C on private validation/hidden splits generated from a secret seed ([v1 report](./evaluations/pool-v1-gemini-2.5-flash.md), [v2 report](./evaluations/pool-v2-gemini-2.5-flash.md)). **Both versions were REJECTED, correctly:**

| | v1 (`rule-compiler/1`) | v2 (`rule-compiler/2`) |
|---|---|---|
| Source replay (C) | 3/6 — below the 80% gate | 4/6 (67%) — below the gate |
| Held-out (C vs A) | 4/4 vs 4/4 | 3/4 vs 3/4 |
| Transfer (C vs A) | 4/4 vs 4/4 | 4/4 vs 4/4 |
| Negative applicability | 8/8 refused, 0% false applicability | 8/8 refused, 0% |
| C tokens/success (transfer) | ~1.6k vs A ~5.5k | ~1.6k vs A ~4.9k |
| Lifecycle tokens/success (C) | ~18.4k vs baseline ~7.0k | ~17.4k vs baseline ~7.7k |

What this shows, and what it does not:
* The evaluator works end to end on a real model: it caught a procedure that looked fine on some suites but failed the necessary source-replay gate, and rejected it without any human judgement.
* **v1 → v2 is a real bug found by evaluation:** intermittent `DECISION_FAILED` ("no JSON object in the model reply") because the compiler capped each `LLM_DECISION` at 2000 tokens and a thinking model can spend that on hidden reasoning. The compiler default became 4000 (`rule-compiler/2`); v2 was registered as a *new immutable version*, v1 stayed REJECTED. Remaining failures are all `VERIFY_FAILED` on psycopg-layout pool tasks — the same sizing mistakes the baseline makes — so they come from the model's reasoning, not the procedure.
* The capability matched the baseline's correctness here at roughly **3-4x fewer tokens and latency per success** on solved tasks, but that is not promotable: correctness gates come first, and with the evaluation overhead charged the lifecycle cost per success is still higher than the baseline's (reuse over many future tasks would be needed to amortize it).
* Single seed, 4-6 tasks per suite, wide intervals, model run-to-run variance (e.g. baseline source replay was 5/6 in one run and 3/6 in the other): these results are indicative, not statistically conclusive. No claim of improvement is made.

## Known limitations

* Evaluation uses the simulated benchmark and very small suites (4 held-out, 4 transfer per family); confidence intervals are wide and one repetition/seed per task is used. Real-model results vary run to run.
* The memory-only control is a single-hint stand-in; a stronger memory baseline arrives with M7.
* Shadow/canary use evaluator-driven observations on held-out tasks; live SHADOW/CANARY traffic routing is not wired (M5 serves ACTIVE versions only; adaptive rollout is M6).
* No OS-level sandbox, no separate evaluator credentials/roles in the database yet (application-level and process-level separation only).
* `SUBCAPABILITY` is executable since M5 (see [runtime.md](./runtime.md)), but the compiler never emits it, so composition is only tested with hand-written definitions.
