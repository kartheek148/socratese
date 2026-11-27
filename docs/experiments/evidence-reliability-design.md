# M6.2 — Evidence reliability, windowed drift detection and recovery: audit and frozen design

Status: **design frozen before any M6.2 data or implementation.** Calibration parameters are set in a second, separately committed section (§9) from development streams only, before the held-out run. Nothing here changes `default-low`, `default-low-v2`, `serving-low-v1`, `rule-capability/1`, the M6 policies or any stored M6/M6.1 hash. M7 is out of scope.

## 0. Milestone, scope, assumptions

**Milestone:** M6.2 on top of M6/M6.1. **Smallest vertical slice:** (A) a final-outcome arm recorded by the real promotion evaluation and turned into an eligibility-checked paired prior; (B) a windowed drift monitor with an explicit, persisted state machine, safe fallback, bounded recovery and shadow-first rollout; frozen synthetic scenarios plus one fresh real-model stream.

**Assumptions (decided, not asked):**
1. *Baseline / alternative.* The comparison the controller needs is the one M6.1 used: the stateless planner (arm A, the promotion evaluator's existing baseline) against the capability **as production serves it**, i.e. with the planner fallback (arm E). "Baseline capability with planner fallback" is read as "the baseline of the existing evaluation"; a capability-vs-capability comparison is not needed by any controller decision and is not built.
2. The drift monitor watches **decisions that depend on a paired prior** (a prior-backed switch away from the rule default, plus bounded recovery probes). It does not watch the rule default, and it never selects a new capability.
3. Latency is not modelled in the simulated benchmark and the paired reference stores no latency; it is recorded as an informational measurement only and has no threshold (limitation).
4. Real-world drift cannot be induced on a real model; drift schedules are synthetic overlays and are labelled as such. Only the stable (S1) behaviour and the base noise come from a fresh real-model stream (§8, R-track).

## 1. Repository audit

| Question | Finding (file) |
|---|---|
| What does `ee-ctl paired-run` record? | An `ExperimentResult` (`domain/experiment.py`): per run `arm` (A planner, E production path), `task_id`, `rep`, `split`, `family`, `status`, `verified`, `tokens`, `tool_calls`, `latency_ms`, `applicable`, `capability_used`, `fell_back`, `asset_ref`, `termination`; config: model, dataset content hashes, `runtime_version`, `verifier_version`, `evaluator_version`, `mode`, `capability_catalog_states`. Arm E is `make_runtime_arm` (matcher → `RuleCapabilityPolicy` → executor → planner fallback): the **final** outcome. Fixture portfolios are tagged `mode="stress"`, `capability_catalog_states=["STRESS_FIXTURE"]`. |
| What does the promotion evaluator record? | `evaluation/engine.py::evaluate` runs arms A, B (memory control) and C (`make_capability_arm`: **capability alone**, no fallback) over the SOURCE/HELD_OUT/TRANSFER suites and stores a full `EvaluationReport` (artifact) plus aggregates in `capability_evaluations.metrics` (per-task rows are deliberately not relational). There is no final-outcome arm: this is the M6.1 gap. |
| Where would a prior come from today? | Only from a result file (`ee-ctl train --paired-from`). `paired_tasks` checks held-out splits, UNKNOWN and ties, versions (runtime/verifier) but not *what kind* of evidence it is: a stress-fixture file and a real evaluation are indistinguishable, and nothing records the evaluation id, tenant, risk class or whether the capability was ever promoted. M6.1's own prior was stress-fixture evidence; under the eligibility rule below it is **research-only** (reported as such). |
| Controller inputs | `DecisionOutcomeRecord` (acted, verified decisions) carries tokens but no timestamp, runtime or verifier version, or latency, although `task_runs`/`run_outcomes` have them (`runtime_version`, `verifier_version`, `execution_time_ms`, `created_at`). |
| Existing drift guard | `controller/evidence.py::_drift`: cumulative live Wilson bound vs the prior rate, no windows, no state, no persistence of the decision, no recovery (report: success 30.7% with the guard vs 28.1% without under a synthetic collapse). It is decided inside `choose` from the snapshot, so it cannot react between snapshot publications and leaves no audit record. |
| Persistence available | `controller_policies` (immutable definition + state), `controller_policy_evaluations`/`_transitions` (append-only, trigger-enforced), `policy_decisions.acted`. Nothing records evidence-level state transitions. |
| Rollout | `controller/rollout.py::build_rollout` builds controllers from the registry at start-up (rollback takes effect on the next build). The same point is the right place to load a drift overlay: the execution plane never reads live tables per decision. |
| Replay | `controller/replay.py` recomputes a decision from stored features + definition (pure `choose`). Anything the decision depends on must therefore be in `DecisionFeatures`. |
| Tenancy | Tenant scoping is by `tenant_id` columns and by the mismatch rule in `features_for` (foreign tenants never read estimates/priors). |

**Gaps this milestone closes:** G1 no final-outcome evidence in the evaluation workflow; G2 no evidence kind/eligibility/duplicate/tenant/risk checks; G3 no monitor state or transitions; G4 the guard is cumulative and slow; G5 no recovery; G6 no shadow monitoring; G7 observations lack version/time provenance.

## 2. Minimal design

### 2.1 M6.2-A — final-outcome evidence through the evaluation workflow
* `Arm.SERVING = "E_SERVING"` and `EvaluationPlan.serving_arm: ArmRunner | None = None`. When set, `evaluate()` runs the production-path arm (candidate in a static, experimental catalog, `RuleCapabilityPolicy`, planner fallback, same verifier and budgets) over the **HELD_OUT and TRANSFER** suites for every seed, **after and independent of** the verdict: the arm is not in `suites`, not in `decide()`, not in the config hash unless set, so every existing verdict and hash is unchanged. Its tokens are counted as validation resources (never hidden).
* `TaskResult` gains optional `capability_used`, `fell_back`, `asset_ref` (omitted from JSON when None). `EvaluationReport.serving_evidence: ServingEvidence | None` (omitted when None) holds the 2×2 paired table (default = capability with fallback vs alternative = planner, per task by strict majority over seeds, UNKNOWN and ties dropped and counted), token sums, the task-set hash, and provenance; `record_evaluation` copies it into `capability_evaluations.metrics["serving_evidence"]` only when present (append-only evaluation records are never rewritten).
* `ee-eval evaluate --serving-evidence` enables it. `ee-ctl train --kind evidence --paired-from-registry CAPABILITY@VERSION` builds the prior from stored evaluations; `--paired-from FILE` keeps working and is labelled research.
* **`EvidenceAssessment`** (pure, `controller/evidence_quality.py`) is an explicit gate list, not a score. A prior is *production-eligible* iff **all** hold: kind is `PROMOTION_EVALUATION` (not stress/synthetic/file); tenant equals the training tenant; evaluation risk class equals the cell's; only HELD_OUT/TRANSFER pairs; arms recorded with the capability actually offered (`capability_used`) for every pair; runtime, verifier, environment versions equal the serving ones; capability version equals the cell's action; `n_pairs ≥ n_min_pairs`; no UNKNOWN-dominated evidence (dropped fraction ≤ 20%); cost measurement complete (every pair has tokens for both arms); no duplicate (task, seed) rows and no evaluation id used twice; freshness within `max_evidence_age_days` when set; drift state not DRIFTED/QUARANTINED. Reasons are listed per failed gate; `--allow-version-mismatch` stays and is recorded in the assessment (`overrides`) and the source, and never weakens any other gate.
* Backward compatibility: new `PairedSource`/`EvidenceParams` fields are optional and omitted when unset, so M6.1 definitions keep their hashes (golden hash pinned by a test written before the change).

### 2.2 M6.2-B — windowed drift monitor (pure, `controller/drift.py`)
* **Cell** = `paired_key(family, risk, default, alternative)`; **reference** = the paired prior cell and its source (success rates of both actions, tokens, n, runtime/verifier versions, recorded time).
* **Observation** = an acted, verified-known decision in the cell's context (family, risk, default key): alternative outcomes drive statistics; default-served decisions advance the quarantine clock. Shadow decisions, UNKNOWN (counted separately) and synthetic outcomes are never observations. Runtime/verifier version and time come from the run record.
* **Channels** (window = last `window` alternative observations, evaluated after every cell observation once `min_samples` exist): success, cost, utility (tokens per verified success), context mismatch (version incompatibility), staleness (evidence age). Level 0 / 1 (suspect) / 2 (alarm) per channel; all formulas in §3.
* **State machine** (per cell, persisted transitions, deterministic replay): `NORMAL → SUSPECT → DRIFTED → QUARANTINED → RECOVERING → NORMAL`; allowed extra edges: `SUSPECT → NORMAL` (cleared), `RECOVERING → QUARANTINED` (re-degradation or probe budget exhausted). `DRIFTED` is recorded and immediately followed by `QUARANTINED` (the response). No direct `DRIFTED/QUARANTINED → NORMAL`.
* **Effects on the controller** (only in `ENFORCE` mode; features carry a `DriftView` so decisions replay): NORMAL — M6.1 behaviour; SUSPECT — unchanged decision, no exploration; DRIFTED/QUARANTINED — the paired prior cannot control the alternative (no tier-L fallback either: live data containing pre-drift outcomes must not re-admit it), rule default served; RECOVERING — prior still cannot control; **bounded probes**: with probability `probe_rate` (hash draw, exact logged propensity) a feasible alternative is executed, only for families/risk levels on the existing exploration allow-list, at most `probe_budget` per episode. Protected risk classes and foreign tenants are untouched (`choose` already returns the rule decision first).
* **Modes.** `SHADOW`: monitor runs on real verified outcomes, transitions are persisted with `applied=false`, features carry no drift view, the M6.1 coarse guard stays (execution is bit-identical to M6.1); a hindsight report counts recommendations later confirmed or not (false fallback recommendations). `ENFORCE`: the overlay is loaded at controller build, the coarse guard is replaced. Mode is a parameter of the immutable policy definition, so enforcement ships only as a new version that passes offline → shadow → canary → active; the monitor can only *restrict* a policy to the rule baseline, never promote, switch or explore beyond the probe budget.
* **Recovery:** after `min_quarantine` cell decisions the cell enters RECOVERING; fresh probe observations accumulate; recovery needs ≥ `min_recovery_samples` fresh outcomes, a more conservative criterion than entry (§3.4), compatible context, acceptable cost, and `recovery_persistence` consecutive passing looks. The historical prior is never edited; the recovery transition stores the fresh measurements as the new assessment.
* **Persistence:** table `controller_drift_transitions` (append-only, trigger-enforced, FK to the policy version, unique `(tenant, policy, version, cell, seq)`), migration `0007`, reversible (downgrade drops it; it holds audit rows only, so no history is rewritten elsewhere). State is derived from the last transition; `ee-ctl drift evaluate` replays the monitor over the stored outcomes and refuses to append if the stored transitions are not a prefix of the recomputation.

### 2.3 Files (expected)
New: `domain/drift.py`, `controller/drift.py`, `controller/evidence_quality.py`, `controller/scenarios.py`, `controller/drift_eval.py`, `evaluation/serving_evidence.py`, `adapters/db/drift_store.py`, `alembic/versions/0007_*.py`, tests (`test_drift_monitor.py`, `test_drift_statistics.py`, `test_evidence_quality.py`, integration/migration additions), `docs/decisions/0017-*.md`, report and tables. Modified: `domain/controller.py` (optional params/views), `domain/evaluation.py`, `controller/{features,policy,evidence,replay,gates,simulate,datasets,rollout}.py`, `evaluation/engine.py`, `adapters/db/{tables,registry_store,controller_store}.py`, `controller_cli.py`, `eval_cli.py`, docs.

## 3. Detection algorithms (frozen definitions)

Reference from the paired cell (`n` pairs): alternative success `p_ref=(both+alt_only)/n` (count `k_ref`), default success `p_def`, alternative mean tokens `μ_ref=alt_tokens/n`, alternative tokens per verified success `u_ref=alt_tokens/k_ref`, default `u_def`. `z(c)` = normal quantile. Let the window have `m` alternative observations, `k` successes, mean tokens `t̄`, sample sd `s`.

**3.1 Window detector (candidate W).**
* *Success:* `(lb,ub)` = Newcombe-Wilson bounds of `p̂ − p_ref` (existing `newcombe_difference_bounds`, reference uncertainty included) at confidence `c`. Alarm if `ub < −success_tolerance` at `confidence`; suspect if `ub < 0` at `suspect_confidence`.
* *Cost:* `se = s·sqrt(1/m + 1/n)`; lower bound `(t̄−μ_ref) − z·se`. Alarm if it exceeds `cost_tolerance·μ_ref` at `confidence`; suspect if `> 0` at `suspect_confidence`.
* *Utility:* `log(u_recent/u_ref)` with delta-method standard error `sqrt(cv²/m + cv²/n + (1−p̂)/(m p̂) + (1−p_ref)/(n p_ref))`; alarm if its lower bound exceeds `log(1+utility_tolerance)` **or** the lower bound of `log(u_recent/u_def)` exceeds 0 (the saving that justified the switch is gone); suspect if the former exceeds 0 at `suspect_confidence`. `k=0` with `m ≥ min_samples` is a success alarm.
* *Mismatch:* observations whose runtime or verifier version differs from the source's; alarm at ≥ `mismatch_min` in the last `window`, suspect at ≥ 1. Not gated by `min_samples`.
* *Staleness:* `age > max_evidence_age_days` (when set and timestamps exist): alarm.

**3.2 CUSUM detector (candidate C), success channel only.** Bernoulli log-likelihood ratio `p0 = clip(p_ref, .02, .98)` → `p1 = p0 − cusum_shift`; `S_t = max(0, S_{t-1} + llr(x_t))`; alarm at `S ≥ h`, suspect at `S ≥ h/2`; reset on RECOVERING entry. Cost/utility/mismatch/staleness use the window rules. Gated by `min_samples`.

**3.3 Persistence and hysteresis.** `NORMAL → SUSPECT` on any channel ≥ 1; `SUSPECT → DRIFTED` after `persistence` consecutive looks with any channel at 2; `SUSPECT → NORMAL` after `clear_after` consecutive looks at 0. A look occurs after every cell observation.

**3.4 Recovery criterion (stricter than entry).** Entry demands the *upper* bound of the recent-vs-reference success difference below `−success_tolerance` at `confidence` (0.95+); recovery demands the *lower* bound above `−recovery_threshold` at `recovery_confidence`, with `recovery_threshold < success_tolerance` so one window can never satisfy both; plus fresh mean tokens ≤ `μ_ref·(1+cost_tolerance/2)`, utility ratio ≤ `1+utility_tolerance/2`, zero incompatible fresh observations, ≥ `min_recovery_samples` fresh probe outcomes, `recovery_persistence` consecutive passes. Re-degradation (alarm-level on ≥ `min_samples` fresh outcomes) or an exhausted probe budget returns to QUARANTINED.

**3.5 Parameters.** Fixed a priori: `window 40`, `min_samples 20`, `suspect_confidence 0.90`, `success_tolerance 0.10` (the controller's own δ for low risk), `cost_tolerance 0.30`, `utility_tolerance 0.30`, `clear_after 10`, `mismatch_min 5`, `min_quarantine 50`, `recovery_threshold 0.05`, `recovery_confidence 0.80`, `min_recovery_samples 30`, `recovery_persistence 3`, `probe_rate 0.10`, `probe_budget 150`, `cusum_shift 0.15`, `max_evidence_age_days` unset (S8b sets it). **Calibrated on development streams only (§6):** `confidence`, `persistence` (and `cusum_threshold h` if C is considered).

## 4. EvidenceAssessment contract
`EvidenceAssessment{cell_key, eligible, production_eligible, reasons[], verified_pair_count, n_unknown_dropped, n_tied_dropped, confidence{lower, upper, level, width}, compatibility{tenant, risk, partition, runtime, verifier, environment, capability_version, model}, freshness{age_days, max_age_days, fresh}, drift_state, cost_measurement{complete_fraction, n_missing}, provenance{kind, evaluation_id, report_sha256, config_hash, dataset_hashes}, overrides[]}`. `ee-ctl evidence inspect` prints it; training refuses ineligible production priors (exit 2); the offline gate reports prior eligibility and FAILs when the policy requires it (`EvidenceParams.require_production_eligible`, default off, omitted from serialization).

## 5. Persistence and audit
Every transition row: cell, previous/new state, trigger, effect, mode and `applied`, measurements (the numbers behind the decision), window size, full threshold configuration and its hash, detector version, the causing decision id, observation timestamp, policy version, creation time. Replay: `drift.replay(reference, observations, params)` is a pure fold; the persisted log must equal it.

## 6. Experiment protocol (frozen)

**Worlds (synthetic, labelled).** Two families `timeout`, `pool`; default = stale capability (`p=0.50`, mean 9,000 tokens), alternative = planner (`p=0.52`, mean 6,500 tokens), per-task outcome correlation `ρ=0.5`, token noise CV 0.35. Each stream: one prior drawn from the world (240 pairs; the first draw whose paired bound resolves, so a prior-backed switch exists — conditional on that, stated), then `N=800` decisions. Drift is an overlay on the *alternative's observed outcome at stream position i*: success survival factor, cost multiplier, runtime label, per family; the rule default's outcomes are untouched. Ground truth "material" = true success drop ≥ `success_tolerance` or true cost rise ≥ `cost_tolerance` or incompatible version.

| id | scenario | overlay |
|---|---|---|
| S1 | stable | none |
| S2 | abrupt success collapse | from 250: survival 0.35 (0.52 → 0.18) |
| S3 | gradual degradation | survival 1 → 0.40 linearly over 250–550 |
| S4 | cost-only | from 250: tokens ×1.5, success unchanged |
| S5 | temporary disturbance | S2 on [250, 400), then normal |
| S6 | noisy | per-block (30 decisions) random effect on success sd 0.08, token CV 0.7 |
| S7 | context-specific | S2 in `timeout` only; `pool` stable |
| S8 | runtime change | from 250 observations carry a different runtime version; outcomes unchanged |
| S8b | evidence staleness | evidence age crosses `max_age` at 250 (1 decision = 1 hour) |
| S9 | insufficient evidence | N=40, S2 at 15 (fewer than `min_samples` post-drift observations) and stable N=40 *(see errata E1)* |
| S10 | misleading early observations | first 25 alternative outcomes drawn at survival 0.35, then baseline |

**Seeds.** Development streams seeds `100000–100199`; held-out `7000000–7000299` (monitor-in-the-loop economics on the first 100). The seed ranges are disjoint and fixed here; thresholds are chosen on development only.

**Controllers (identical streams, paired by seed):** rule baseline; M6.1 `evidence+prior` (coarse guard, no monitor); M6.2 `SHADOW` (must equal M6.1 decision-for-decision); M6.2 `ENFORCE` (window detector, or CUSUM only if selected by §6.1). A fixed-exploration baseline is not relevant to a prior-backed switch (no exploration happens) and is not run.

**6.1 Calibration procedure (development only, fixed now).** Grid: `confidence ∈ {0.95,0.99,0.995,0.999}`, `persistence ∈ {3,5,10}`, CUSUM `h ∈ {4,6,8,10}`. Feasible = dev false-alarm rate (≥ 1 DRIFTED in a stable 800-decision stream) ≤ 3% in **both** S1 and S6, and zero DRIFTED in the S9-stable and S10 streams in ≥ 95% of streams. Choose the feasible point with the smallest mean of the median detection delays on dev S2 and S3. Detector choice: **W unless** C is feasible and its dev median delay is ≥ 25% shorter on both S2 and S3. Both detectors are reported on held-out; only the pre-selected one is the deployment candidate.

**6.2 Metrics (all with n and 95% intervals).** Primary: verified success, tokens per verified success, utility per task (success − tokens/10,000 for a stable scale, reported with the raw columns), success loss during drift (vs M6.1 and vs the rule), recovery performance. Drift: detection delay (decisions after material onset, median/IQR), false-alarm rate per stream and per 1,000 decisions, missed-drift rate, quarantine duration, recovery delay, transitions, precision/recall against ground truth. Economic: tokens spent by probes (cost of recovery), tokens lost to fallback when not drifted (unnecessary quarantine), tokens lost by missed drift, cumulative tokens. Controller: rule-deviation rate, prior-backed decisions, quarantined decisions, fallbacks, recovery probes. The cost of monitoring is reported as 0 model tokens (statistics only) plus measured CPU time per observation.

**6.3 Pre-registered acceptance (held-out).**
* (a) False alarms: S1 and S6 ≤ 5% of streams (one-sided 95% Clopper-Pearson upper bound reported; claim holds if the point estimate ≤ 5%).
* (b) Detection: S2 median delay ≤ 100 decisions and missed ≤ 5%; S3 missed ≤ 15%; S4 missed ≤ 10%.
* (c) Isolation: S7 `pool` false-alarm rate ≤ 5%, `timeout` detected as S2.
* (d) Small samples: S9 zero DRIFTED when post-drift observations < `min_samples`; S10 DRIFTED in ≤ 5% of streams.
* (e) Safety/economics: M6.2 ENFORCE verified success ≥ M6.1 on S2 (paired by seed, 95% bootstrap interval of the difference excludes 0) and not worse than M6.1 on S1 by more than 1 point (interval); SHADOW identical to M6.1 on every stream.
* (f) Recovery (S5): ≥ 70% of streams back to NORMAL within 300 decisions of the drift ending; no re-quarantine oscillation (≤ 1 episode per stream in S1/S6).
* **Decision:** SUPPORTED iff (a)–(e) hold; PARTIALLY SUPPORTED if (a)–(c) hold but (e) or (f) fails; NOT SUPPORTED if (a) or (b) fails. Every miss is reported with its sample size; no threshold is changed after the held-out run.

## 7. Regression guarantees (tested)
Monitor off ⇒ byte-identical M6.1 behaviour and hashes; the golden M6 hashes (`53b7c21a…`, `fe45fb19…`, `9f559935…`) and a new golden M6.1-evidence hash pinned before the change; `default-low`/`default-low-v2`/`serving-low-v1` untouched; protected classes and foreign tenants never read priors or drift views; SHADOW never alters a decision; quarantined prior cannot control; replay exact.

## 8. R-track (fresh real-model stream)
One fresh real-model held-out stream (`timeout`, 240 tasks, arms A and E, new unrecorded seed, same stress portfolio and model as M6.1) is replayed with the M6.1 V-timeout prior (historical evidence, a different seed) and the *calibrated, frozen* monitor: S1 (stable: false alarms on real outcome noise) and S2/S4/S5 overlays on the real table (20 seeded orders). Reported as secondary: it checks the monitor on non-synthetic noise; the overlays remain synthetic.

## 9. Errata (made after a smoke run on 8 development seeds with placeholder thresholds, before any calibration or held-out data)

The smoke run exercised the code path only; no threshold was chosen from it. It exposed four defects of the *protocol*, each fixable by arithmetic rather than by looking for a favourable result:

* **E1 (S9b).** "S2 at 15 of N=40" leaves 25 post-drift observations, not fewer than `min_samples=20`. S9b is now N=40 with the drift at 25 (15 post-drift observations).
* **E2 (S10).** Survival 0.35 for 25 observations (success ≈ 0.18 against 0.52) is a severe transient, not a misleading one: any detector with a 95% bound alarms on it. S10 now uses survival 0.70 (success ≈ 0.36, the kind of early bad luck a 25-sample window shows about 5% of the time) with ground truth "no drift"; the severe version is kept as **S10b** (ground truth: material during 0–25), informational, no acceptance criterion.
* **E3 (recovery arithmetic).** With `probe_rate 0.10`, 30 fresh probes need ≈ 300 decisions after RECOVERING begins (plus `min_quarantine 50`), so criterion (f) "within 300 decisions of the drift ending" was unattainable by construction. `probe_rate` is raised to **0.25** (still bounded by `probe_budget 150` and the exploration allow-list) and criterion (f) becomes: **≥ 70% of S5 streams are back to NORMAL by the end of the stream (400 decisions after the drift ends)**, with at most one quarantine episode per stream in S1/S6.
* **E5 (feasibility needs detection).** A detector that never alarms would satisfy every false-alarm constraint, so a grid point is feasible only if it also detects S2 and S3 in at least 90% of development streams.
* **E4 (calibration fallback).** If no grid point meets the §6.1 false-alarm constraints, the most conservative grid point (`confidence 0.999`, `persistence 10`) is frozen and the failure to reach the constraints is itself a reported result.

## 10. Calibration outcome (frozen before the held-out run)

Development seeds 100000-100199, 200 streams per scenario, monitor-only; full table in [evidence-reliability-calibration.md](../evaluations/evidence-reliability-calibration.md). Applying the §6.1 rule (with E5):

* Feasible WINDOW points: (0.995, 10), (0.999, 3), (0.999, 5). The smallest mean of the S2 and S3 median delays is **WINDOW, confidence 0.995, persistence 10** (S1 false alarms 0.0%, S6 2.5%, S2 detected 100% at a median delay of 46 decisions, S3 detected 97.5% at 195).
* CUSUM has **no feasible point** on the grid (best: h=10, S1 false alarms 2.5%, S6 8.5%; CUSUM reacts to the per-block success noise of S6). By the pre-registered rule the simpler WINDOW detector is frozen. CUSUM is still run on the held-out streams and reported as a non-selected comparison.
* Observed on development data, reported because it constrains what can be claimed: the S6 (noisy) false-alarm constraint is what forces the conservative confidence; at the aggressive points (0.95, 3) the monitor raised false alarms in 20% (S1) and 45% (S6) of stable streams, and S3 (gradual) detection takes about 4x longer than S2.

**Frozen configuration for every held-out run:** `detector WINDOW, confidence 0.995, persistence 10, window 40, min_samples 20, suspect_confidence 0.90, success_tolerance 0.10, cost_tolerance 0.30, utility_tolerance 0.30, clear_after 10, mismatch_min 5, min_quarantine 50, recovery_threshold 0.05, recovery_confidence 0.80, min_recovery_samples 30, recovery_persistence 3, probe_rate 0.25, probe_budget 150, max_episodes 3, cusum_shift 0.15`. These are the code defaults of `DriftParams`; `max_evidence_age_days` is set only by S8b.

Held-out run (seeds 7000000-7000299 monitor-only comparisons, 7000000-7000099 controllers in the loop): `ee-ctl drift-sim --seed-start 7000000 --seeds 100 --variants m61 m62_shadow m62` and `--variants monitor --seeds 300`, plus `--detector CUSUM --threshold 10 --variants monitor` for the non-selected comparison.
