# M5.10 design — production-aligned learning evaluation and fallback-aware promotion

Written **before any implementation**. Sections 1-2 are the audit (read from the code and from stored M5.8/M5.9 data), 3-5 the revised evaluation model and statistics, 6 the frozen experiment protocol. Parameters in §4-§6 are frozen here, committed before any M5.10 data exist, and are not changed after seeing results. No existing policy (`default-low`, `default-low-v2`) is modified; nothing is promoted by this milestone.

## 1. Audit: the six paths

| # | Path | Where | What is measured / who decides |
|---|---|---|---|
| 1 | **Direct capability evaluation** (the gate) | `evaluation/arms.py::make_capability_arm` → `CapabilityExecutor.run` | The capability alone. A failure is a failure; no planner. Final state verified by the independent verifier. Tokens = capability decision calls only. Used by `ee-eval evaluate/observe`, `engine.evaluate`, the `default-low`/`-v2` verdicts. |
| 2 | **Capability in the production runtime** | `experiment_arms.make_runtime_arm` → `TaskOrchestrator.execute` (matcher → `RuleCapabilityPolicy` → `_execute_strategy`) | Matcher gates eligibility deterministically (typed preconditions). Controller picks the most similar eligible capability (probability 1.0). Executor runs it. |
| 3 | **Failure and planner fallback** | `orchestrator._execute_strategy` / `_fallback` | Any `ExecStatus.FALLBACK` result hands the *current* (possibly mutated) environment to `AgentRuntime` with the budget the capability left (tokens, steps, tool calls, deadline are never reset). Result tokens/tool calls are summed. |
| 4 | **Memory-assisted execution** | `memory/runtime.select_memory` → `MemoryAugmentedLLM` → general agent | ACTIVE-only, typed applicability. No separate fallback: an inapplicable memory means plain planning with a recorded reason. Layer A and Layer B coincide for memory. |
| 5 | **Final verification** | `orchestrator.execute` | One independent verification of the final environment snapshot after whichever strategy ran. `LLM_ERROR` without success → UNKNOWN. |
| 6 | **Promotion and activation** | `governance/registry.py` | State machine on evaluation verdicts (path 1 / memory analogue). ACTIVE = available to the runtime. There is **no** separate approval of "this asset may serve this task", only the deterministic matcher. |

### 1.1 Which fallback events are credited as capability success today
The orchestrator records `RunOutcome.capability_version = <ref>` whenever a capability was selected, **including runs finished by the planner**; the controller decision is back-filled with the final status; arm E rows carry `asset_ref` + the final `verified`; `ee-exp` "capability used / success" (§6 of its report) and the M5 production-mode result ("pool 21% → 94%") aggregate final success by capability. Nothing records the capability's own outcome, the state it left, or how many tokens it spent versus the planner. Measured on stored runs (arm E, rows with a capability applied):

| Run set | rows | fell back | fallback-finished successes credited to the capability |
|---|---|---|---|
| M5.8 production (ACTIVE assets) | 60 | 1 | 0 |
| M5.8 scoped (experimental) | 120 | 6 | 4 |
| M5.9 P4 set B (`pool.f12f4fa920`, `timeout.e2fb15fa79`) | 120 | 9 | 3 |
| M5.9 P4 set A, pool capability | 60 | **59** | **44** (of 45 final successes) |
| M5.9 P4 set A, timeout capability | 60 | 48 | 17 (of 29 final successes) |

So the mismatch is negligible for a good capability and dominant for a poor one: set A's pool capability looked +48% beneficial in production although it succeeded alone on ~3% of held-out tasks.

### 1.2 Why a run falls back (exact code paths)
| Cause | Mechanism | Fallback? | Attributed today |
|---|---|---|---|
| Capability **inapplicable** | matcher rejects before the controller; executor `NOT_APPLICABLE` (re-check) | matcher: no capability offered → plain planning (`fell_back=False`); executor `NOT_APPLICABLE` is not handled and **falls into `_fallback`** | reason string only |
| Capability **execution failure** | `ASSERT_FAILED, INVALID_ARGS, AMBIGUOUS_PATH, UNBOUND, DECISION_FAILED, UNSUPPORTED_STEP, RETRY_EXHAUSTED, DEPENDENCY_UNAVAILABLE, COMPOSITION_LIMIT` → `ExecStatus.FALLBACK` | yes | final outcome credited to capability |
| **Tool** failure | `TOOL_FAILED` → FALLBACK; `TOOL_NOT_ALLOWED` → `ABORTED` | failure yes; **permission violation no** (aborts, `CAPABILITY_ABORTED`) | – |
| **Timeout / budget** | `TIMEOUT, MAX_STEPS, MAX_TOKENS` → FALLBACK; if the shared budget is already spent the planner is not started (`fallback_reason` set, no planner run) | yes / "triggered but not attempted" | – |
| **Verification** failure | `VERIFY_FAILED`, `VERIFY_UNKNOWN` → FALLBACK | yes | – |
| **Runtime exception** | not caught in `_execute_strategy`; propagates (e.g. "controller selected an ineligible capability") | no; the caller's grid marks the run UNKNOWN | – |
| **Explicit escalation** | capability `HUMAN_APPROVAL_REQUIRED` → `ESCALATE`; planner `ESCALATED` termination; `HUMAN_ESCALATION` strategy | no planner for capability escalation | – |
| Other aborts | `TENANT_MISMATCH, INVALID_DEFINITION, CANCELLED` → `ABORTED` | no | – |

### 1.3 Statistical audit (Phase 5, findings about the existing code)
1. **Fractional counts into an exact interval.** `evaluation/risk.build_risk_evidence` sets `regression_mass = Σ_t (regressed_t / baseline_solved_t)`, a fraction when a task has several repetitions, and `risk_policy.regression_gate` passes it to `cp_upper/cp_lower` (Clopper-Pearson). CP assumes integer Bernoulli counts; a fractional "count" has no binomial likelihood. Impact on the M5.9 evidence (2 repetitions): 8 of 16 assets had a fractional mass (e.g. `mem_consolidated_timeout` 14.5 of 26). The retrospective "tasks" scenario (`regressions / seeds`) had the same defect. The decision `default-low-v2` made on integer-mass assets is unaffected; for the others the bound is an interpolation, not an exact interval. `default-low-v2`'s thresholds and code are **not** changed (frozen, rejected candidate); the defect is documented and the new estimator below replaces it for the serving policy.
2. **Source-replay tasks inside the regression estimand.** `SCORED` includes SOURCE_REPLAY; those are the discovery tasks the asset was learned from, so they are not independent evidence of regression risk on new tasks. The serving policy excludes them.
3. **Repetitions vs tasks.** Repetitions of a task are correlated draws; the new estimators take the distinct task as the unit and collapse repetitions to a task-level binary outcome by a strict majority rule (ties are reported as indeterminate and dropped, never counted as evidence).
4. **Evidence requirement.** When too few baseline-solved tasks exist the policy must say how many more independent tasks it needs, not weaken δ and not inflate n with repetitions.

## 2. The mismatch in one sentence
The gate evaluates entity **C** (capability alone). Customers receive **S = (match → C → planner fallback → verify)**. `S` can beat or lose to the baseline for reasons that are not `C`'s quality (a failed capability can still leave useful partial work for the planner, or waste budget), and the gate is silent about both.

## 3. Revised evaluation model
### 3.1 Execution-level attribution (Phase 1)
New pure domain model `domain/strategy.py::StrategyExecutionResult` (frozen, additive; old records unaffected):

* identity: `strategy_id` (`direct-capability`, `memory-assisted`, `planning`), `asset_kind`, `asset_ref` (exact `id@version`), `asset_family`, `policy_version` of the controller;
* `applicable`, `attempted` (capability/memory actually run);
* capability phase `CapabilityPhase`: `status` (`COMPLETED|FALLBACK|ABORTED|ESCALATE|NOT_APPLICABLE`), `reason` (`ExecReason`), `detail`, `outcome` (`OutcomeStatus` of the **independent verifier on the environment as the capability left it**: at the handover for fallbacks, at the end otherwise), `verified: bool|None` (`None` = UNKNOWN), `tokens`, `tool_calls`, `latency_ms`, `steps`;
* fallback phase `FallbackPhase`: `triggered`, `attempted` (planner actually started), `cause` (`FallbackCause`: `EXECUTION_FAILURE|TOOL_FAILURE|TIMEOUT_OR_BUDGET|VERIFICATION_FAILURE|DECISION_FAILURE|INAPPLICABLE_AT_EXECUTION`), `reason`, remaining budget at handover, `tokens`, `prompt/completion_tokens`, `tool_calls`, `latency_ms`, `outcome` and `verified` of the final state;
* `final_status`/`final_verified` (`None` = UNKNOWN), totals, `budget_tokens`, `final_termination`.
Rules: capability success is never inferred from final success; final failure is never inferred from capability failure; UNKNOWN stays UNKNOWN; totals equal the sum of phases (tested). The handover verification (one extra, read-only verifier call) is **opt-in** (`TaskOrchestrator(attribute_phases=True)`); production behaviour, budgets and traces are unchanged by default. `RunRow.strategy` stores it (optional; historical rows load unchanged).

### 3.2 Two layers (Phase 2) and the arms (Phase 3)
* **Layer A, intrinsic asset quality.** Capability: the capability phase of the *same production run* (arm **E1**, derived, no extra model cost, paired with E2 by construction); the standalone `make_capability_arm` gate stays as is. Memory: arms B/C/D (no fallback exists).
* **Layer B, serving-policy quality.** The unchanged production runtime (arm **E2**, id `E` in rows) and the memory-assisted arms against the stateless baseline A on identical tasks/repetitions. No re-implemented fallback: attribution is recorded *from* `TaskOrchestrator`.
* Arms: **A** stateless; **B/C/D** ACTIVE episodic/consolidated/procedural memory; **E1** capability phase only (derived); **E2** capability + production fallback; **F** empty-registry runtime (noise control); **UB/UC/UD** ungated memory on unseen families (negative transfer). Same tasks, model, tools, verifier, budgets. Production mode reads only ACTIVE assets; candidate access only through the existing `--experimental-states` override (bannered, never promotion evidence).

### 3.3 Activation vs approval vs routing (Phase 4D)
1. **Activation** (existing): asset state ACTIVE after validation. 2. **Strategy approval** (new, opt-in, deterministic): an approved-refs filter over the catalogs (`ApprovedCapabilityCatalog`, `ApprovedMemoryCatalog`); an ACTIVE but unapproved asset is never offered. Default (no filter) = today's behaviour. 3. **Per-task routing** stays the deterministic matcher + rule controller; learned routing is M6.

## 4. Serving-policy evaluation `serving-low-v1` (experimental, frozen)
Three separate decisions per asset, each PASS / FAIL / INCONCLUSIVE, plus mandatory safety. Nothing trades off against token savings.

**Unit and estimators (Phase 5).** The distinct task. R repetitions → task binary outcome = strict majority success; exactly half → *indeterminate* (dropped and counted). Exact Clopper-Pearson only on **integer** task-level Bernoulli counts (the function rejects non-integers). Paired differences and cost ratios: cluster bootstrap with the task as the resampling unit (10,000 resamples, seed 20260101, one-sided 95%). Wilson for single proportions. Multiple assets are decided independently (an intersection-union conjunction per asset needs no alpha split); comparisons reported together use Holm.

**S. Safety (zero tolerance, any failure ⇒ strategy FAIL).** (S1) no capability or planner termination `TOOL_NOT_ALLOWED`, no `TENANT_MISMATCH`; (S2) no false application: an asset applied to a task outside its declared family; (S3) bounded fallback: at most one planner phase and total tokens ≤ 1.25 × the task budget in every run; (S4) cross-tenant access and forbidden-resource modification cannot be exercised by the simulated benchmark and are reported as **untested**, not passed.

**I. Intrinsically reliable (Layer A).** Capability: capability-phase success over *attempted* tasks; memory: final success over applicable tasks. PASS if point rate ≥ 0.75, Wilson 95% lower bound ≥ 0.50 and regression risk bounded: among baseline-solved tasks the integer CP one-sided 95% upper bound of P(asset-alone fails) ≤ 0.20 with ≥ 14 baseline-solved tasks. FAIL if the rate is < 0.75 with ≥ 20 attempted tasks or the CP lower bound of the regression rate > 0.20. Otherwise INCONCLUSIVE with the **evidence requirement** (additional independent baseline-solved tasks needed: `n_needed = min n such that CP_ucb(0, n) ≤ 0.20` is 14 clean, 22 with one regression, computed exactly; never inflated by repetitions).

**U. Serving improves outcomes (Layer B, in-scope = the asset's family).** Paired task-level difference of final success (strategy − baseline), cluster bootstrap. PASS if the one-sided 95% lower bound > 0 **and** the final-outcome regression risk among baseline-solved tasks has CP upper bound ≤ 0.20 (≥ 14 baseline-solved tasks). FAIL if the upper bound of the difference < −0.05 or the CP regression lower bound > 0.20. Otherwise INCONCLUSIVE (not demonstrated). ≥ 20 in-scope tasks required for any non-INCONCLUSIVE verdict.

**E. Economically beneficial.** Total tokens per verified success of the whole strategy **including the failed capability attempt and the fallback**, divided by the baseline's on the same tasks; cluster-bootstrap one-sided 95% interval of the ratio. PASS if the upper bound < 1.0 **and** U is PASS **and** S is PASS; FAIL if the lower bound > 1.0; else INCONCLUSIVE. Not gated, reported: fallback frequency, capability-only success, fallback recovery rate (final success among capability-failed tasks that fell back, versus the baseline success on the *same* tasks), tool calls, latency, cost split, and lifecycle break-even (discovery + creation (0 model tokens) + evaluation cost charged as in M5.8 without baseline sharing + R ∈ {0,1,3} revalidations of that evaluation, an explicit assumption).

**Classification (Phase 7).** Safety FAIL or U FAIL → *harmful in the production execution policy*. Any decision INCONCLUSIVE → *insufficiently evaluated*. I PASS, U PASS, E PASS → *intrinsically reliable and economically useful*. I PASS, U PASS, E FAIL → *reliable but not worth its runtime cost*. I FAIL, U PASS (and the fallback recovered the capability's failures) → *imperfect alone but useful within a safe fallback strategy*. No promotion follows from any label; promotion policy changes stay opt-in and need independent confirmation.

## 5. What is implemented (files)
New: `domain/strategy.py`, `execution/attribution.py`, `execution/approval.py`, `evaluation/task_stats.py`, `evaluation/serving_policy.py`, `evaluation/serving_economics.py`, `evaluation/serving_report.py`; modified: `execution/runtime.py` (`RuntimeResult.capability_phase`), `execution/orchestrator.py` (`attribute_phases`, phase capture), `evaluation/arms.py`/`experiment_arms.py`/`experiment.py`, `domain/experiment.py` (`RunRow.strategy`, arms `E1`), `experiment_cli.py` (`--mode serving`, `--approved`, `serving-report`). Tests: attribution (fallback, UNKNOWN, abort/escalation paths, budget-exhausted), cost sums, estimators (integer-only CP, majority collapse, cluster bootstrap), policy decisions/classification, approved-catalog isolation, historical-record readability, ACTIVE-only isolation in the serving CLI.

## 6. Experiment protocol (frozen)
* **Fresh data.** Two new public discovery seeds (sets C, D; 4 real-model repetitions each) → learn → compile → build memory → 16 candidates (8 per set). Fresh private test seeds P5 (set C) and P6 (set D), scale 5 (60 pool, 60 timeout, 30 queue tasks); never used for design or tuning; seeds are not recorded. Real model `gemini-2.5-flash`, repetitions 1.
* **Assets.** No asset is validated or promoted in this milestone. Because nothing would be ACTIVE, candidate access uses the explicit experimental mechanism (`--experimental-states ACTIVE CANDIDATE`), labelled research. A production-mode (ACTIVE-only) smoke run on a task subset demonstrates isolation (no asset may be used).
* **Run.** `ee-exp run --mode serving` over arms A, B, C, D, E (=E2), F plus UB/UC/UD on `queue`; E1 is derived from E2 rows. One execution per task and arm.
* **Analysis.** Per asset: S, I, U, E decisions and the classification above (policy applied as a counterfactual to the experiment rows; nothing acted on); per family and per variant outcomes; stratified (family) metrics and the overall production estimate weighted by the declared task counts; capability-only vs final; fallback recovery vs baseline on the same tasks; cost split; negative transfer (UB/UC/UD vs gated on `queue`); lifecycle economics with R ∈ {0,1,3}.
* **Reading rules fixed in advance.** INCONCLUSIVE is reported as such with its evidence requirement; no threshold or asset is changed after seeing the results; budget shortfalls are labelled pilot; generalisation is not claimed from `queue` alone; the two sets share the baseline task grid only within a set (P5 vs P6 are different tasks).

## 7. Outcome (added after the run; nothing above was changed)
The protocol was executed as frozen (sets C and D, 990 runs each, real model, no threshold or asset changed afterwards). One deviation: the ACTIVE-only production-mode smoke run was not executed live; isolation is covered by an integration test. Results, limitations and the negative/inconclusive findings are in [../evaluations/production-aligned-learning-evaluation.md](../evaluations/production-aligned-learning-evaluation.md); decision in [ADR 0014](../decisions/0014-validated-assets-vs-validated-serving-policies.md). Nothing was promoted.
