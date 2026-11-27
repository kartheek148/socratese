# Adaptive strategy controller (Milestone 6)

The controller decides, per task, *which eligible action to run*: general planning, or one of the capabilities the matcher already admitted. It is a learning-plane artifact (a versioned, hashed policy) consumed by the execution plane; execution never waits for training and never reads live tables. Design, audit and the frozen protocol: [experiments/adaptive-controller-design.md](./experiments/adaptive-controller-design.md); results: [evaluations/adaptive-controller-report.md](./evaluations/adaptive-controller-report.md); ADR [0015](./decisions/0015-adaptive-controller-bounded-learning.md). The M6.1 evidence-driven extension (paired validation priors, bounded exploration) is described [below](#evidence-driven-controller-m61).

## Constraints are outside the learned policy

```
task ─► matcher (permissions, tenant, preconditions, budgets, ACTIVE-only)   existing, deterministic
          │  eligible capabilities
          ▼
   feasibility layer (controller/feasibility.py)       rule baseline default + feasible set F(x)
          │  protected risk class / foreign tenant ► rule decision, no estimate read, probability 1
          ▼
   reliability constraint   non-inferiority (Newcombe-Wilson lower bound ≥ −δ_risk), optional
          │                 absolute success floor, minimum verified evidence per cell
          ▼
   cost objective           expected tokens per verified success, only among admissible actions
          ▼
   exploration (bandit)     ε-greedy over an explicit family/risk allow-list, behind a data gate
```

`high` risk can never be a learned class (rejected by the parameter type). The orchestrator still refuses a capability the matcher did not admit. Cost never compensates a failed constraint.

## Policies

| version | behaviour |
|---|---|
| `fixed-general-planning/1`, `rule-capability/1` | unchanged baselines (the rule baseline is also the default action of every adaptive policy) |
| `adaptive/N`, kind `COST_AWARE` | deterministic: deviates from the rule default only with enough verified evidence; never explores (probability 1.0) |
| `adaptive/N`, kind `BANDIT` | cost-aware plus gated ε-greedy exploration; the exact distribution is logged (`estimates["pi:<action>"]`) and the uniform draw is a hash of the decision id, so a decision replays exactly |
| `adaptive/N`, kind `EVIDENCE_DRIVEN` (M6.1) | paired validation priors where a cell has ≥30 paired tasks, evidence-driven exploration (bounded by the evidence requirement) elsewhere; `COST_AWARE` also accepts a paired prior when the evidence parameters are set |

A policy version is an immutable `ControllerPolicyDefinition` (typed parameters + frozen estimator snapshot: per `(family, risk, action)` verified trials, successes, tokens; calibration summary; training provenance), identified by a content hash. Estimates use only **verified, acted** outcomes of the tenant; UNKNOWN and shadow (non-acted) decisions are excluded and counted.

Every decision stores the typed `DecisionFeatures` (feasible set, rule default, the evidence cells it read, snapshot hash, calibration flag, draw), the estimates, the selected action, the exact selection probability, the policy version and the reason; `outcome_status` is back-filled by the verified run (rule 10). `ee-ctl replay` recomputes stored decisions from the features and the registered definition.

## Evidence, calibration, off-policy estimates

* **Calibration** (`controller/calibration.py`): prequential (each prediction uses only earlier outcomes): Brier score, Brier skill vs the global rate (reported, not gated: design A1), ECE, reliability table, cost bias and MAPE. Gate: ≥100 scored predictions and ECE ≤ 0.10.
* **Off-policy** (`controller/replay.py`): IPS, SNIPS, direct and doubly-robust estimates with explicit support coverage. A log written by the rule baseline has zero coverage for any deviation, which the report states instead of inventing a counterfactual.
* **Full-information replay** (`controller/simulate.py`): paired recorded experiments (arms A and E) give every task's outcome under every feasible action; the controller learns only from the action it chose and pays for exploration. Verdict rule (design A2): BEATS / EQUAL / WORSE / INCONCLUSIVE from per-task cluster-bootstrap bounds.

## Rollout: offline → shadow → canary → active, with rollback

`ControllerPolicyRegistry` is the only component that changes a policy's state. A forward step needs a stored **PASS** for the previous stage computed for exactly this definition hash and the current gate set (and PostgreSQL re-checks it in a trigger):

| step | evidence | PASS requires |
|---|---|---|
| CANDIDATE → SHADOW | OFFLINE replay on the decision log | ≥100 decisions, calibration gate, 0 protected deviations, 0 choices outside the feasible set, no predicted harm where the log supports the candidate |
| SHADOW → CANARY | the candidate's non-acted decisions next to the acted ones | ≥100 decisions with verified outcomes and scored predictions, live ECE ≤ 0.10, 0 protected deviations, 0 shadow errors |
| CANARY → ACTIVE | concurrent comparison, stable hash split of task ids (`canary_fraction`, low-risk learned classes only) | ≥30 per arm, non-inferiority lower bound ≥ −0.10, no protected exposure; canary control must be the currently ACTIVE version |

A FAIL in the canary rolls the candidate back automatically (`ee-ctl evaluate canary` calls `apply_canary_guard`), and canary evidence is only accepted while the version is in CANARY, from the canary window only (concurrent control, same risk classes), never from before the canary started (the registry checks). `rollback` of an ACTIVE version restores the previously superseded one in the same transaction; REJECTED and ROLLED_BACK are terminal (re-admission is a new version). At most one SHADOW, CANARY and ACTIVE version per policy (partial unique indexes). `build_rollout` reads the registry, so a rollback takes effect on the next controller build.

The orchestrator can attach a **shadow** controller (`TaskOrchestrator(shadow=...)`): it decides alongside the acting one, its decision is stored with `acted=false` and never executed, and any exception in it is recorded as a `controller_shadow` error trace event without touching the run.

## Evidence-driven controller (M6.1)

Constant-ε exploration is paid in every regime (M6). M6.1 spends *validation* evidence instead. Details, protocol and results: [design](./experiments/evidence-driven-controller-design.md), [report](./evaluations/evidence-driven-controller-report.md), ADR [0016](./decisions/0016-evidence-driven-controller.md).

* **Paired prior** (`controller/paired.py`, `controller/datasets.py:paired_tasks`): per `(family, risk, default, alternative)` a 2×2 table of the rule default and the alternative on the same held-out tasks, final outcome including the planner fallback (arms A and E; the promotion evaluator's capability-alone arm is *not* usable), with provenance (`PairedSource`). Held-out splits only; repetitions collapse by strict majority; UNKNOWN and tied tasks are dropped and counted; sources recorded under another runtime or verifier are refused unless `--allow-version-mismatch`.
* **Decision** (`controller/evidence.py`, only when `evidence` parameters are set; otherwise the M6 rules run unchanged): with a paired cell of ≥ `n_min_pairs` (30) tasks the alternative replaces the rule default iff the Newcombe paired lower bound of `p_alt − p_default` is ≥ −δ and it saves ≥ `min_cost_saving` tokens per verified success; tokens never compensate a failed bound. Without a paired cell the M6 live rule applies and `EVIDENCE_DRIVEN` explores only while the alternative is unresolved, a saving is plausible and its sample target (≤ `explore_target_cap`) is not reached. Prior-backed cells never explore.
* **Drift guard**: ≥ `drift_min_n` live outcomes that contradict the prior by more than δ (Wilson one-sided) invalidate it and the rule default returns. Coarse: it compares cumulative live evidence (slow under a sudden collapse, see the report).
* **Unchanged**: protected risk classes and foreign tenants never read a prior; the stored M6 definition and snapshot hashes; `default-low`, `default-low-v2`, `serving-low-v1`.
* Each decision records `tier_paired:<action>`, the bound (`lb:`/`ub:`), the evidence size, the cost ratio, `req_pairs` (paired tasks still needed) and any drift invalidation; replay verifies the paired views against the registered snapshot. The offline gate adds `prior_cells`, `prior_pairs`, `prior_validation_tokens` and FAILs priors without complete provenance.

```
uv run ee-ctl paired-run --portfolio heterogeneous --llm vertex --root PUB --private-root PRIV --out v.json   # held-out arms A and E (the prior source)
uv run ee-ctl train      --kind evidence --paired-from v1.json v2.json --explore-families pool timeout       # CANDIDATE with a paired prior
uv run ee-ctl prior-curve --prior v1.json v2.json --sizes 30 60 120 240                                      # how large must validation be?
uv run ee-ctl simulate   --heldout NAME h1.json h2.json --prior v1.json v2.json --kinds bandit evidence+prior evidence   # offline, per-family cell verdicts
```

## Evidence reliability and drift monitoring (M6.2)

M6.1's prior is only as good as its provenance, and a prior can go stale. M6.2 adds (A) final-outcome evidence recorded by the real evaluation workflow with explicit eligibility gates and (B) a windowed drift monitor with an audited state machine. Audit, frozen design and protocol: [experiments/evidence-reliability-design.md](./experiments/evidence-reliability-design.md); results: [evaluations/evidence-reliability-report.md](./evaluations/evidence-reliability-report.md); ADR [0017](./decisions/0017-evidence-reliability-and-drift-monitor.md).

**A. Evidence through the evaluation workflow.** `ee-eval evaluate --serving-evidence` additionally runs the capability *as production serves it* (matcher, executor, planner fallback; an evaluation-only static catalog, never an ACTIVE one) on the held-out and transfer tasks, after and independently of the promotion verdict (`Arm.SERVING`, `EvaluationPlan.serving_arm`). The paired planner-vs-served table (`ServingEvidence`: strict majority over seeds, UNKNOWN/ties/not-offered/duplicate/missing-cost counted) is stored in `capability_evaluations.metrics["serving_evidence"]`; verdicts, suites and every earlier config hash are unchanged when the flag is off, and the arm never enters `decide()`. `ee-ctl train --kind evidence --paired-from-registry CAPABILITY@VERSION` builds the prior from the stored evaluation and checks every **gate** (`controller/evidence_quality.py`; `EvidenceAssessment` lists one reason per failed gate): kind `PROMOTION_EVALUATION`, tenant, risk class, held-out partitions only, runtime/verifier/environment versions, capability version, sample size (`n_min_pairs`), UNKNOWN/tie fraction ≤ 20%, complete token measurements, no duplicate rows or evaluation counted twice, evaluation verdict PASS, not a test-double model, freshness (when `max_evidence_age_days` is set), drift state not DRIFTED/QUARANTINED/RECOVERING. Ineligible evidence refuses to train (exit 2, every reason printed) unless `--allow-research`, which records the source as `production_eligible=false`; `--allow-version-mismatch` stays, is listed in `overrides` and never weakens another gate. Result files (`--paired-from`) are research-only (`EXPERIMENT_FILE` / `STRESS_FIXTURE`). `ee-ctl evidence-inspect --version N` prints the assessment of a stored policy's priors; the offline gate reports `prior_sources_ineligible` and FAILs when the policy requires eligibility (`evidence.require_production_eligible`, set automatically for registry priors).

**B. Windowed drift monitor** (`controller/drift.py`, pure; `domain/drift.py`). Per prior-backed cell (family, risk, default, alternative) it folds the executed, verified outcomes of the context (shadow decisions, UNKNOWN [counted separately] and other tenants are never observations) through `NORMAL → SUSPECT → DRIFTED → QUARANTINED → RECOVERING → NORMAL`:

| channel | alarm / suspect rule (windows of `window`=40 alternative outcomes, ≥ `min_samples`=20) |
|---|---|
| success | Newcombe upper bound of (recent − reference) < −`success_tolerance` at `confidence` (0.995) / < 0 at `suspect_confidence` (0.90); optional Bernoulli CUSUM (`detector: CUSUM`) |
| cost | lower bound of mean-token rise > `cost_tolerance` × reference (delta-method, reference uncertainty included) |
| utility | tokens per verified success: rise over the reference beyond `utility_tolerance`, **or** dearer than the rule default (the saving that justified the switch is gone) |
| context mismatch | ≥ `mismatch_min` observations under another runtime/verifier version (structural: no sample size needed) |
| staleness | evidence older than `max_evidence_age_days` |

An alarm must persist `persistence` (10) consecutive looks to move SUSPECT → DRIFTED; DRIFTED is recorded and followed immediately by QUARANTINED. A quarantined cell cannot control a decision, **including through the unpaired live rule** (live data contain pre-drift outcomes); after `min_quarantine` rule-served decisions it enters RECOVERING, where bounded probes (`probe_rate` exploration mass, `probe_budget` per episode, only on the existing exploration allow-list, exact logged propensities) collect fresh outcomes. Trust returns only through `RECOVERY_CRITERIA_MET`: ≥ `min_recovery_samples` fresh outcomes whose *lower* bound clears a stricter threshold (`recovery_threshold` 0.05 < `success_tolerance` 0.10, `recovery_confidence`), acceptable cost and utility, compatible context, for `recovery_persistence` consecutive looks. A still-degraded probe window or an exhausted budget returns to QUARANTINED; after `max_episodes` the cell stays quarantined until a new version. The historical prior is never edited.

* **Modes.** `SHADOW`: the monitor runs on real verified outcomes and persists transitions with `applied=false`; the decision is bit-identical to M6.1 (it keeps its coarse guard). `ENFORCE`: the overlay (loaded when the controller is built: `build_rollout(..., drift=DriftService)`; execution never reads live tables per decision) restricts decisions and replaces the coarse guard. The mode is part of the immutable definition, so enforcement ships as a new version through offline → shadow → canary → active; the monitor can only *restrict* a policy toward the rule baseline, never promote or switch, and protected classes/foreign tenants never read it.
* **Persistence.** `controller_drift_transitions` (migration 0007, append-only trigger): cell, previous/new state, trigger, effect, mode, `applied`, measurements, window size, full threshold configuration and its hash, detector version, causing decision id, observation time, policy version. `DriftService.sync` replays the monitor over the acted decisions of a version, **refuses** to append unless the persisted transitions are exactly a prefix of the recomputation, and publishes the overlay.
* **Replay.** `DecisionFeatures.drift` carries what a decision read; `ee-ctl replay` recomputes it and detects a removed or altered overlay.

```
uv run ee-eval evaluate --tenant T --capability C --version 1 --serving-evidence ...                    # records final-outcome evidence
uv run ee-ctl train --kind evidence --paired-from-registry C@1 --drift-monitor shadow --explore-families pool timeout
uv run ee-ctl evidence-inspect --version N                                                              # EvidenceAssessment of every prior
uv run ee-ctl drift-evaluate | drift-status | drift-replay | drift-shadow-report --version N            # monitor, audit, hindsight check
uv run ee-ctl drift-calibrate / drift-sim / drift-real / drift-report ...                               # frozen experiments (synthetic overlays)
```

## Controlled revalidation of quarantined cells (M6.3)

M6.2 quarantines a cell whose evidence stopped being trustworthy and lets it back through a *timer* followed by live probes. M6.3 replaces that path, per policy version and opt-in, with **bounded, independently verified, out-of-band revalidation** whose every step is explainable from stored evidence. Audit: [research/m6_3_revalidation_audit.md](./research/m6_3_revalidation_audit.md); frozen design and protocol: [experiments/revalidation-design.md](./experiments/revalidation-design.md); results: [evaluations/revalidation-report.md](./evaluations/revalidation-report.md); ADR [0018](./decisions/0018-controlled-revalidation.md). **M6.1/M6.2 semantics and every stored hash are unchanged**: the M6.2 state machine (`advance`) is untouched, `ControllerParams.revalidation` is omitted from the serialised form when unset, and revalidation changes no decision until a cell is quarantined (tested).

* **A case per quarantine episode** (`controller/revalidation.py`, pure; `domain/revalidation.py`), composed with the M6.2 `MonitorFleet` by `RevalidationFleet`. The case disables native recovery through the existing `MonitorState.locked` flag; a quarantined cell stays prior-disabled in the overlay in **every** case state except `RECOVERED` (no live probes: users are not exposed). `RECOVERED` returns the monitor to NORMAL with empty windows (sequence number and episode preserved, so a relapse still counts toward `max_episodes`) and, for structural and stale cases, rebases the reference's versions / evidence time (an event in the log; the stored prior is never edited).
* **Reason** is a *projection* of the existing `DriftTrigger` of the DRIFTED transition (`PERFORMANCE_DEGRADATION`, `STATISTICAL_DRIFT` for CUSUM, `COST_DEGRADATION`, `STRUCTURAL_INVALIDATION`, `EVIDENCE_STALE`; `VERIFICATION_FAILURE` only if supplied externally; `UNKNOWN` is ineligible).
* **Eligibility** is a gate list, never automatic: reason known/revalidatable, tenant, risk class (`allowed_risks`, default `low`), identity (executor supports the capability), runtime and verifier versions known, verification available, minimum pool, lifetime token budget and failed-cycle count, TTL, strategy readiness. One reason per failed gate; permanent failures end the episode.
* **States** `QUARANTINED → REVALIDATION_PENDING → REVALIDATING → RECOVERY_CANDIDATE → RECOVERED`; `REVALIDATING|CANDIDATE → QUARANTINED` on a failed cycle; terminal `EXPIRED` (case or cycle TTL) and `BUDGET_EXHAUSTED` (trial cap, token cap, lifetime budget, cycles exhausted). `RECOVERY_CANDIDATE` waits `confirm_gap_hours` and runs `confirm_trials` fresh trials; confirmation is a *collapse guard*, not a second proof.
* **Strategies** (one statistical hypothesis for all: success not worse than the reference by ≥ `margin` 0.10; exact binomial error control; a cost guard): `FIXED` (A: one fixed-size exact test), `EVIDENCE_THRESHOLD` (B: A's test, started once the rule default's concurrent verified outcomes show a healthy environment), `SEQUENTIAL` (C: SPRT, one trial at a time, truncated). A cost-futility stop applies to all (design erratum E1).
* **Verification.** Trial verdicts are `SUCCESS`, `FAILURE`, `TIMEOUT`, `INVALID_OUTPUT`, `VERIFICATION_UNAVAILABLE`. Only an explicitly verified pass is a success; an unavailable verdict is never a success, never counted in n, consumes budget, and too many end the cycle. `OrchestratorTrialSource` (`execution/revalidation.py`) runs trials through the production orchestrator and verifier with a **forced action and no run lifecycle**: nothing is written to the production run, decision or outcome tables.
* **Lineage.** Every trial row records case, cycle, stage, ordinal, round anchor/time, verdict, tokens, versions, task and capability references, and a lineage verdict (`NOT_NEWER` than the evidence that triggered quarantine, `TENANT_MISMATCH`, `DUPLICATE`, `VERSION_MISMATCH`; rejected trials are stored and still cost tokens). Every transition row records its measurements, failed gates, executor facts, rebase, trial counts, tokens and the parameter hash.
* **Persistence and replay.** `controller_revalidation_transitions` and `controller_revalidation_trials` (migration 0008, append-only triggers; the downgrade refuses while they hold rows). `RevalidationService.sync` (the controller-build entry) recomputes the fleet from the ordered outcomes and the *persisted trials only*, refuses to append unless what is stored is exactly what is recomputed, persists what the observations imply and publishes the overlay; `.run(definition, executor)` is the learning-plane worker that adds one live round (trials and transitions atomically). The M6.2 `DriftService` refuses revalidation policies (a plain replay would miss the restorations); `RevalidationService(fallback=DriftService)` serves both kinds to `build_rollout`. In `SHADOW` mode cases open and readiness is recorded (`applied=false`) but **no trial is executed**.

```
uv run ee-ctl reval-sim --seed-start 9000000 --seeds 100 --json-out heldout.json     # the frozen experiment (synthetic)
uv run ee-ctl reval-report --results heldout.json [--repro again.json] --out report.md  # pre-registered acceptance + verdict, computed
uv run ee-ctl reval-status | reval-evaluate | reval-replay --version N                # persisted cases, sync, verification
```

## Commands

```
uv run ee-ctl train      --tenant bench --policy-id adaptive --kind bandit --explore-families pool timeout   # CANDIDATE from the decision log
uv run ee-ctl evaluate   offline|shadow|canary --version N                                                   # stored evidence, exit 1 on FAIL
uv run ee-ctl promote    --version N --to shadow|canary|active                                                # blocked without PASS evidence
uv run ee-ctl rollback   --version N --reason "..."      /  reject  /  status  /  replay --version N
uv run ee-ctl simulate   --heldout NAME RESULTS --warm OTHER.json --out report.md                           # offline, full information
uv run ee-ctl paired-run --portfolio heterogeneous --root PUB --private-root PRIV --out r.json               # real model, arms A and E
uv run ee-ctl live       --portfolio heterogeneous --root PUB --private-root PRIV --canary-fraction 0.5      # persisted, registry-driven
```
`paired-run` and `live` use hand-authored **stress fixtures** (`controller_fixtures.py`; a compiler-shaped capability and a *drifted* one whose target key is frozen to its original layout). They are validated through the normal DSL validator, held in memory, never registered or promoted, and every run is tagged experimental.

## Schema (migrations 0005, 0006, 0007, 0008)

`controller_policies` (immutable definition + hash; only `state` changes, along the legal graph, with a trigger requiring stage evidence), `controller_policy_evaluations` and `controller_policy_transitions` (append-only), partial unique indexes for one ACTIVE/CANARY/SHADOW version per policy, and `policy_decisions.acted boolean NOT NULL DEFAULT true`. Migration 0006 only widens the `kind` CHECK to include `EVIDENCE_DRIVEN` (paired priors live inside the JSON definition); migration 0007 adds the append-only `controller_drift_transitions` audit table and migration 0008 the append-only `controller_revalidation_transitions` / `controller_revalidation_trials` (the downgrades refuse while the tables hold rows). See [schema.md](./schema.md).

## Limits

* The estimator is tabular over `(family, risk, action)`; it cannot see within-family variant differences (a capability that is good on some layouts and bad on others averages out).
* Non-inferiority at a 10-point margin needs on the order of 100+ verified trials per action; the learned controller is therefore conservative at benchmark scale and defers to the rule baseline until it has them.
* Exploration costs successes and tokens; whether it pays back depends on the horizon (see the report). M6.1 removes most of it where a paired prior exists, but a prior needs several hundred paired validation tasks per cell to resolve a 10-point margin (smaller priors leave the rule in place) and is extra validation cost until promotion records the final-outcome arm.
* The M6.1 coarse drift guard is slow after a sudden collapse; M6.2's windowed monitor replaces it in `ENFORCE` mode (see the report for what it does and does not achieve: detection delay, slow recovery, structural-invalidation cost).
* `HUMAN_ESCALATION` is never selected by a learned policy; no cost model for humans exists.
