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
uv run ee-eval evaluate --tenant bench --capability pool.<hash> --version 1 --llm vertex --seeds 0 1 --workers 8 --out docs/eval.md
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

## Capability-vs-memory experiment (`ee-exp`)

`ee-exp` compares five arms on identical tasks and repetitions: **A** stateless, **B** episodic memory, **C** consolidated memory, **D** instruction reuse (the compiled procedure as natural language, executed by the general agent), **E** the compiled capability through the production runtime. B-E are built from the same eligible discovery trajectories. Design, fairness rules and statistics: [experiments/capability-vs-memory-design.md](./experiments/capability-vs-memory-design.md).

```
export EE_EVALUATOR_ROLE=evaluator EE_PRIVATE_DATASET_ROOT=/secure/path/fresh-private   # never used for any gate
uv run ee-bench generate --dataset v3 --private --scale 2 --root /secure/path/fresh-private   # needs EE_PRIVATE_DATASET_SEED
uv run ee-exp run --tenant bench --root benchmarks/v3-or-fresh-discovery --capabilities pool.<hash>@1 timeout.<hash>@1 \
    --llm vertex --model gemini-2.5-flash --reps 2 --workers 8 --out results.json --report report.md
uv run ee-exp report --results results.json --out report.md --maintenance-tokens 0
```
Statistics (`evaluation/paired.py`): task-level paired bootstrap CI, exact McNemar, Holm correction, equal-token-budget curves; repetitions are averaged inside a task and never counted as tasks; UNKNOWN runs are missing data. Economics (`evaluation/economics.py`): `L + N*e + M`, break-even per verified success, as-built and equal-validation sensitivity. `ee-exp` never writes to the registry. Dataset v3 adds the hidden-only `queue` family for cross-family transfer. Results: [evaluations/capability-vs-memory-report.md](./evaluations/capability-vs-memory-report.md).

## Governed memory and the gated comparison (M5.8)

Memory is now a governed asset with the same lifecycle and the **unchanged** `default-low` policy as capabilities (design: [experiments/gated-memory-vs-capability-design.md](./experiments/gated-memory-vs-capability-design.md), ADR [0012](./decisions/0012-governed-memory-and-gated-comparison.md)). The generic promotion logic lives in `governance/registry.py` (`GovernedRegistry[D]`); `capabilities/registry.RegistryService` and `governance/memory.MemoryRegistryService` are thin bindings.

```
uv run ee-learn build-memory --tenant bench                       # learning plane: CANDIDATE memory (episodic/consolidated/procedural)
uv run ee-gov evaluate --tenant bench --root DISC --private-root P1 --llm vertex --seeds 0 1 --workers 8 --out-dir evals/
uv run ee-gov promote  --tenant bench --memory mem_procedural_pool@1 --to shadow   # then observe/canary/active exactly as ee-eval
uv run ee-gov status   --tenant bench
uv run ee-exp run --mode gated --tenant bench --root DISC --private-root P2 --ungated-families queue --llm vertex --reps 1 --workers 10 --out r.json --report r.md
```
`ee-exp --mode gated` reads only ACTIVE assets (memory *and* capabilities) from the database; a task without an applicable ACTIVE asset runs under general planning and the row records the reason. Arm F is the noise control (same path, empty registry); UB/UC/UD are ungated memory on the listed families to measure the negative transfer that gating prevents. `--experimental-states` widens the catalogs for research runs (tagged and bannered in the report). Memory applicability is typed and deterministic (family, tools, file roles, risk, exclusions), never similarity. Results: [evaluations/gated-memory-vs-capability-report.md](./evaluations/gated-memory-vs-capability-report.md).

## Candidate risk-based policy and cheaper validation (M5.9)

`default-low` / `-medium` / `-high` are **unchanged** (a test pins `default-low`'s hash). A separately versioned candidate, `default-low-v2` (+ medium/high), replaces the absolute regression *count* and the source-replay *point* threshold by task-level evidence tests (exact Clopper-Pearson bound on `P(candidate fails | baseline succeeds)`, paired non-inferiority bound, source-replay upper bound, variant concealment guard, zero-tolerance critical violations). Gates are three-valued (PASS / FAIL / INCONCLUSIVE = more evidence needed). Audit: [experiments/promotion-policy-audit.md](./experiments/promotion-policy-audit.md); design and frozen parameters: [experiments/promotion-policy-v2-design.md](./experiments/promotion-policy-v2-design.md); ADR [0013](./decisions/0013-risk-based-promotion-policy-and-cheaper-validation.md).

```
uv run ee-gov --policy-set risk-v2 evaluate ...        # make the candidate policy govern (opt-in)
uv run ee-gov evaluate ... --counterfactual-policy-set risk-v2   # govern with default, record v2 on the same evidence
uv run ee-gov evaluate ... --baseline-cache DIR        # reuse recorded baseline runs across sessions
uv run ee-gov evaluate ... --staged                    # predefined sequential stopping (never an early PASS)
uv run ee-eval evaluate ... --skip-memory-control      # omit the B_MEMORY arm (it feeds no gate)
uv run ee-policy collect --tenant T --out reports.json # export reports with per-task rows
uv run ee-policy confirm --set A reports_a.json results_a.json --out report.md
python -m experience_engine.evaluation.policy_simulation   # retrospective simulation on M5.8 data
```
A new policy never reopens a REJECTED version or rewrites an evaluation row (both are enforced: terminal state, append-only tables); a re-admission is a new version. Reports now carry `risk_evidence`, `counterfactuals`, `evaluation_config_hash`, stage information and measured `resources` (executed vs reused runs/tokens, input/output tokens where the arm records them). Results: [evaluations/promotion-policy-confirmation-report.md](./evaluations/promotion-policy-confirmation-report.md).

## Production-aligned evaluation (M5.10)

The gate path judges a capability alone; production serves it with a planner fallback. M5.10 records and evaluates both, opt-in. Design: [experiments/production-aligned-evaluation-design.md](./experiments/production-aligned-evaluation-design.md); ADR [0014](./decisions/0014-validated-assets-vs-validated-serving-policies.md).

```
uv run ee-exp run --mode gated --attribute-phases ...     # record capability/fallback phases, causes, tokens (adds the E1 view)
uv run ee-exp run ... --approved pool.f12f4fa920@1 ...   # approval can only REMOVE assets from the ACTIVE set
uv run ee-exp serving-report --set C c.json --set D d.json --out report.md --json-out evals.json
```
`serving-report` applies the experimental policy `serving-low-v1` as a counterfactual: separate safety, intrinsic (capability phase alone), serving (final outcome incl. fallback) and economic decisions, three-valued, with task-level exact bounds and a cluster bootstrap. It never promotes or changes a state, and `default-low` / `default-low-v2` are unchanged. Results: [evaluations/production-aligned-learning-evaluation.md](./evaluations/production-aligned-learning-evaluation.md).

## Controller evaluation (M6)

Strategy-selection policies are evaluated separately from assets: prequential full-information replay of recorded paired experiments (`ee-ctl simulate`), live shadow/canary through the controller registry (`ee-ctl live`), with the frozen verdict rule BEATS / EQUAL / WORSE / INCONCLUSIVE (success non-inferiority and tokens-per-verified-success, per-task cluster bootstrap). Exploration cost is reported on every comparison. See [controller.md](./controller.md) and [evaluations/adaptive-controller-report.md](./evaluations/adaptive-controller-report.md). The asset promotion policies (`default-low`, `default-low-v2`, `serving-low-v1`) are unchanged.

M6.1 adds per-family *cell* verdicts at the controller's contractual 10-point margin, paired validation priors (`ee-ctl train --paired-from`, `simulate --prior`), the validation-size curve (`ee-ctl prior-curve`) and a synthetic drift replay (`simulate --drift-start`). Results, including the fragile margin and the negative drift result: [evaluations/evidence-driven-controller-report.md](./evaluations/evidence-driven-controller-report.md).

## Known limitations

* Evaluation uses the simulated benchmark. `--seeds` re-runs every task once per seed and `--workers` runs them concurrently; with `ee-bench generate --private --scale N` suites can be made larger (v2 results used x3 and 2 seeds). Repeats of one task are correlated, so the Wilson intervals are optimistic and the sample-size gates (`min_*_n`) count **distinct tasks**. `max_regressions` is an absolute count and gets stricter as runs grow. Real-model results still vary run to run. First multi-seed results: [evaluations/v2-multiseed-evaluation-and-production-run.md](./evaluations/v2-multiseed-evaluation-and-production-run.md).
* The memory-only control is a single-hint stand-in; a stronger memory baseline arrives with M7.
* Shadow/canary use evaluator-driven observations on held-out tasks; live SHADOW/CANARY traffic routing is not wired (M5 serves ACTIVE versions only; adaptive rollout is M6).
* No OS-level sandbox, no separate evaluator credentials/roles in the database yet (application-level and process-level separation only).
* `SUBCAPABILITY` is executable since M5 (see [runtime.md](./runtime.md)), but the compiler never emits it, so composition is only tested with hand-written definitions.
