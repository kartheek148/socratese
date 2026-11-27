# M6.3 Phase 0 — Revalidation and Recovery Audit

Status: audit only. No decision behaviour was modified while producing it. Every number below was
computed with the M6.2 code at commit `eefa9ea` by throw-away scripts (described, not committed);
nothing here is a held-out result. The design that follows from this audit is frozen separately in
[revalidation-design.md](../experiments/revalidation-design.md).

**Scope note.** The brief for M6.3 reached me truncated (it stops mid-sentence at §2.4 "Freshness
and evidence lineage — Every revalidation event"). Phases 0–2 are implemented as written. The
remainder (lineage rules, experiment, tests, documentation) follows the structure of M6.1/M6.2
(frozen design before data → calibration on development seeds only → held-out run → report with
negative results). This is an assumption, listed in §10.

## 1. What exists today

### 1.1 Components and where recovery lives

| Concern | Where | Notes |
|---|---|---|
| Experience / capability identity | `PairedCell` keyed by `paired_key(family, risk, default_key, alt_key)` (`domain/controller.py`); action keys `strategy[:ref]` (e.g. `DIRECT_CAPABILITY:cap@1`) | A "cell" is one paired prior: rule default vs one alternative in one (family, risk) context. The capability version is inside the key (`ref`). |
| Evidence registry / provenance | `PairedSource` (arms, splits, dataset hashes, model, `runtime_version`, `verifier_version`, `recorded_at`); `assess_evidence` gate list (`controller/evidence_quality.py`); migration 0006 registry-backed priors | Provenance exists for the *prior*; nothing links later observations to it except by cell key. |
| Promotion and eligibility | Evaluation engine and `default-low*` policies (M5); controller gates (M6, `controller/gates.py`); evidence eligibility (M6.2-A) | Promotion governs *assets*; the controller's own rollout is offline → shadow → canary → promote. |
| Outcome verification | `OutcomeStatus` ∈ {VERIFIED_SUCCESS, FAILURE, PARTIAL, UNKNOWN}; `RunOutcome.verification_results[*].passed: bool \| None` (None = verifier could not evaluate) | Independent verifier in `verification/`; UNKNOWN is excluded from every estimate (`DecisionOutcomeRecord.known`). |
| Adaptive controller | `controller/policy.py`, `evidence.py`, `features.py` | Reads a published `DriftOverlay` once at construction (never per decision). |
| Drift monitor | `controller/drift.py` (pure), `domain/drift.py`, `controller/drift_service.py` | Deterministic fold of ordered, executed, verified outcomes. |
| Quarantine | `DriftState.QUARANTINED`; effect `FALLBACK_TO_RULE`; `PRIOR_DISABLED = {DRIFTED, QUARANTINED, RECOVERING}` | A prior-disabled cell cannot move a decision off the rule default (also not through the live rule). |
| Recovery transitions | `advance()` QUARANTINED → RECOVERING → NORMAL (`controller/drift.py:390-420`) | The only recovery path today. |
| Persistence | `controller_drift_transitions` (migration 0007, append-only) and `DriftService.sync/replay` with prefix verification | One row per state change. No evidence rows. |
| Experiment harnesses | `controller/{scenarios,perturb,drift_eval,simulate}.py`; `drift-calibrate\|sim\|real\|report` | Synthetic worlds with ground truth; a real-model table track (`real_streams`). |
| Baselines / datasets | Rule baseline in every `StreamResult`; M6.1 coarse guard (`m61`); M6.2 SHADOW/ENFORCE; dataset v3 for the real-model track | Reproducible by seed; hash-based common random numbers. |
| CLI | `ee-ctl drift-*`, `evidence-inspect`, `train --paired-from-registry --drift-monitor`; `ee-eval evaluate --serving-evidence` | `controller_cli.py`, `drift_commands.py`, `drift_db_commands.py`, `drift_report.py`. |

### 1.2 The quarantine → recovery → reactivation lifecycle, step by step (M6.2, ENFORCE)

1. **Detection.** In NORMAL/SUSPECT each *alternative* outcome enters a 40-observation window; the
   channels (`evaluate`) are success, cost, utility (tokens per verified success), context mismatch
   (runtime/verifier version differs from the prior's) and evidence staleness (age of the prior).
   A level-2 alarm must persist for `persistence = 10` consecutive observations.
2. **Quarantine.** `DRIFTED` is immediately followed by `QUARANTINED` (two transitions at the same
   decision). The **reason is the trigger of the DRIFTED transition**; the QUARANTINED transition
   only says `QUARANTINE_APPLIED`. `episode` is incremented; all windows are cleared.
3. **Quarantine dwell.** Only *rule-default* decisions advance `dwell` (an alternative outcome
   while quarantined is counted as `n_ignored`, never as evidence). After `min_quarantine = 50`
   default-served decisions, and only if the evidence is not stale and the cell is not `locked`,
   the cell moves to `RECOVERING` (`QUARANTINE_ELAPSED`). **This is a timer; nothing about the
   world is verified.** The rule is a heuristic.
4. **Fresh evidence.** In RECOVERING the controller may *probe* the alternative
   (`probe_alternatives`, `controller/evidence.py:252`): an exploration draw over {greedy,
   alternative}, so the effective probe probability is `probe_rate/2 = 12.5 %` of the context's
   decisions, at most `probe_budget = 150` probes per episode, only for allow-listed
   families/risk levels. The probe outcome is a *production* outcome (a real task, verified by the
   production verifier); each probe exposes a user task to a capability that was just judged
   unreliable.
5. **Recovery decision.** `recovery_ok` on the last ≤ 40 probe outcomes (needs ≥ 30): the
   Newcombe lower bound of (fresh success − reference success) at confidence **0.80** must be
   ≥ −0.05; mean tokens ≤ 1.15 × reference; tokens-per-success ≤ 1.15 × reference; **zero**
   incompatible-version observations; evidence not stale. Three consecutive looks must pass
   (`recovery_persistence = 3`) → `RECOVERY_CRITERIA_MET`, `NORMAL`, windows reset.
6. **Failure.** A level-2 alarm on ≥ 20 fresh observations, or 150 probes without success →
   `QUARANTINED` (`RECOVERY_FAILED`), `episode + 1`; `locked` once `episode > max_episodes = 3`
   (permanent until a new policy version).
7. **Reactivation.** NORMAL removes the cell from the overlay; the controller's prior-backed
   behaviour resumes at the next snapshot refresh (every 10 decisions in the simulations). The
   reference (the historical prior) is never edited.

### 1.3 What kind of mechanism each part is

| Part | Nature | Basis |
|---|---|---|
| State machine, replay, persisted log, overlay | **Deterministic** | Pure function of (reference, parameters, observations); recomputed and prefix-verified (`verify_prefix`). |
| Detection channels | **Statistical** | Newcombe bounds, normal-approximation cost and delta-method utility bounds at pre-registered confidence levels, calibrated on development seeds. |
| Quarantine reason | **Evidence-derived but implicit** | Recoverable only from the preceding DRIFTED transition's trigger; not stored on the QUARANTINED row, not in the overlay. |
| Dwell before recovery (`min_quarantine = 50`) | **Heuristic** | A count of rule-served decisions. No evidence that 50 is right; never varied. |
| Probe mass / budget | **Heuristic** | Frozen on development seeds; the effective probability was misread at design time (errata E3). |
| Recovery criterion (80 % Newcombe LB ≥ −0.05, three looks) | **Statistical but not power-checked** | See finding F1: one look passes a *fully recovered* world only 34–41 % of the time. |
| Structural and staleness handling | **Absent for recovery** | See F2/F3. |
| Episode cap / lock | **Heuristic** | Bounds cost; says nothing about whether recovery is possible. |
| Verification of fresh evidence | **Inherited** | Probes are verified by the production verifier; the monitor cannot tell a verifier outage from a failure except through UNKNOWN (which it ignores). |
| Reactivation timing | **Controller-coupled** | Visible only at the next overlay publication. |

## 2. Current recovery thresholds (frozen M6.2 defaults, `DriftParams`)

`window 40`, `min_samples 20`, `confidence 0.995`, `suspect_confidence 0.90`,
`success_tolerance 0.10`, `cost_tolerance 0.30`, `utility_tolerance 0.30`, `persistence 10`,
`clear_after 10`, `mismatch_min 5`, `min_quarantine 50`, `recovery_threshold 0.05`,
`recovery_confidence 0.80`, `min_recovery_samples 30`, `recovery_persistence 3`,
`probe_rate 0.25` (effective 0.125), `probe_budget 150`, `max_episodes 3`.

## 3. Findings

All probes in this section used seeds 7100000–7100011 ("audit exploration seeds"). They are
**excluded from every M6.3 evaluation** and carry no confirmatory weight.

**F1 — The M6.2 recovery rule has low per-look power, independent of probe speed.** With the
reference of the experiments (n = 240 pairs, success 0.52, 6,500 tokens) and fresh windows drawn
*from a world that has fully recovered* (true success 0.52), 4,000–6,000 simulated windows each:

| fresh window | P(success criterion) | P(utility criterion) | P(`recovery_ok`, one look) |
|---|---|---|---|
| 30 | 0.37 | 0.75 | 0.34 |
| 40 | 0.41 | 0.79 | 0.41 |

(the cost criterion passes ≈ 99 %). With true success 0.45 (a 7-point drop, *inside* the 10-point
tolerance) one look passes 12–13 %; at 0.40 (a material 12-point drop) 4–5 %; at 0.30 ≈ 0. Recovery
needs three consecutive passes over overlapping windows, so the time to recover is a long-tailed
function of luck. The reference's own sampling error (±0.065 at 240 pairs) is larger than the 0.05
recovery margin, so the rule demands a *lucky* window. M6.2 attributed the 40/100 recovery rate
mainly to probe arithmetic; that matters, but it is not the only limit. This audit does **not**
claim the rule is "wrong": it is conservative against false recovery (0–5 % per look when
materially degraded), but that conservatism is bought with most of the recoveries. How the rule
behaves over many looks was not measured here.

**F2 — Structural invalidation can never recover natively (by construction).**
`CellReference.compatible` compares each observation's runtime/verifier label with the prior's.
Fresh observations under a changed runtime stay incompatible forever; `recovery_ok` requires
`bad == 0`, and ≥ 5 incompatible observations raise a level-2 `CONTEXT_MISMATCH` alarm inside
RECOVERING. Observed (12 streams, S8): 35 `QUARANTINE_ELAPSED → RECOVERING`, 24 `RECOVERY_FAILED`,
**0** `RECOVERY_CRITERIA_MET`. Each attempt spends live probes on a capability that cannot recover,
and the reference is never rebased, so even a capability that works perfectly under the new runtime
is permanently excluded.

**F3 — Stale evidence can never recover natively.** The QUARANTINED → RECOVERING step is blocked
while `age > max_evidence_age_days`, and `age` only grows (the reference's `recorded_at` is fixed).
Observed (12 streams, S8b): 12 quarantines, **0** attempts, **0** recoveries, zero probes spent.
There is no mechanism that produces fresh evidence of the right kind.

**F4 — Recovery evidence is user traffic.** All fresh evidence is production probes (12.5 % of
decisions in the context). Recovery therefore (i) needs ≥ 320 context decisions for 40 probes, (ii)
exposes users to the degraded alternative, (iii) cannot be scheduled, time-bounded or made
independent of production load. M6.2 measured 3–97 probes and 25k–841k probe tokens per stream.

**F5 — The quarantine reason is not a first-class value.** It can be reconstructed (the DRIFTED
transition immediately before the QUARANTINED one, same `decision_id`), but the overlay, the
controller and the recovery logic never see it, and recovery treats every reason identically.

**F6 — "Recovered" is one boolean with no lineage.** `RECOVERY_CRITERIA_MET` stores window
measurements but not *which* observations (decision ids), their versions, or their relation to the
evidence that triggered quarantine. Fresh probe observations are newer than the trigger only by
position in the log.

**F7 — Verification failure is invisible to recovery.** UNKNOWN outcomes are ignored (correct: they
are never successes) but there is no notion of "verification unavailable for N consecutive
attempts", so a verifier outage neither delays nor blocks recovery explicitly; it merely slows probe
accumulation. `PARTIAL` is not distinguished from `FAILURE` by the monitor.

**F8 — There is no resource accounting for recovery beyond probes.** `probe_budget` counts probes
(a production-traffic quantity); there is no token cap, no wall-clock expiry, no lifetime budget per
cell, and no estimate of whether recovery is worth its cost.

**F9 (carried from M6.2).** Known negatives: recovery 40/100 (target 70 %); structural invalidation
cost +22–25 % tokens for −2 % successes against M6.1; a severe 25-observation transient triggers
DRIFTED in 20.7 % of streams (those cells then depend entirely on recovery); latency is not
monitored; all drift is synthetic.

**Observed native paths (12 streams each, ENFORCE):** S5 (temporary disturbance): 12 quarantines,
12 `QUARANTINE_ELAPSED`, 8 `RECOVERY_CRITERIA_MET`. S2 (persistent collapse): 12 quarantines, 24
recovery attempts, 14 `RECOVERY_FAILED`, 0 recoveries (no false recovery seen in 12 streams — too
few to bound anything).

## 4. Dependencies on drift detection

* The **reason** and **severity** of a quarantine exist only as the trigger and measurements of the
  DRIFTED transition. Any revalidation design depends on them and therefore on the detector's
  quality: a false DRIFTED (2.3 % of noisy streams; 20.7 % under a severe transient) creates a
  case that revalidation should clear cheaply, and the cost of clearing it is part of the false
  alarm's true cost.
* The **reference** (`CellReference`) is built from the prior snapshot and is the only baseline.
  Revalidation compares against it; if the reference was optimistic (the experiments accept only
  prior draws whose paired bound resolves — `sample_prior`), revalidation inherits the bias.
* `MonitorState.locked` already means "native recovery disabled"; a wrapper can set it without
  changing `advance`.
* The overlay (`DriftCellStatus`: state, seq, probes_remaining) is what decisions read; adding
  fields to it would change decision hashes, so revalidation must project onto it, not extend it.

## 5. Reusable infrastructure

* Synthetic worlds, perturbations, hash-based common random numbers, `build_stream`, `run_stream`,
  `drift_eval` scoring and aggregation, `drift-sim` multiprocessing, the real-model table track,
  and `drift-report` conventions (pre-registered acceptance in code).
* The `DriftService` pattern: recompute, verify the persisted log is a prefix, append, return the
  overlay.
* The `assess_evidence` pattern: a gate list with one reason per failed gate (reused for
  eligibility).
* Migration/store/port/CLI patterns of 0007; the mutation-check workflow of M6.2.
* Not reusable: nothing executes the alternative *outside* production decisions today; the
  simulator has full-information tasks per stream but no independent revalidation task pool, and
  perturbations apply only to the alternative (the default is untouched).

## 6. Architectural limitations that prevent reliable recovery

| # | Limitation | Consequence |
|---|---|---|
| L1 | Fresh evidence only from production probes | Slow, unschedulable, exposes users (F4) |
| L2 | Immutable reference | Structural change and staleness unrecoverable (F2, F3) |
| L3 | Recovery rule not power-checked | Most full recoveries rejected per look (F1) |
| L4 | Reason/severity not carried into recovery | One rule for incomparable situations (F5) |
| L5 | No lineage for recovery evidence | Not explainable from stored evidence (F6) |
| L6 | No verification-availability state | Outage handling implicit (F7) |
| L7 | Probe budget counts traffic, not cost/time | No bound on lifecycle cost (F8) |
| L8 | Timer-based entry to recovery | A timer, not evidence, opens recovery (conflicts with the M6.3 invariant) |

## 7. Proposed M6.3 design (summary; frozen in the design document)

1. **Layer, don't rewrite.** The M6.2 `advance` state machine, `DriftParams`, the overlay and every
   stored M6.1/M6.2 hash are untouched. A separate *revalidation case* per quarantine episode is
   composed with the M6.2 fleet; the case disables native recovery through the existing `locked`
   flag and is the only way back to NORMAL in this mode.
2. **Reasons by projection.** `QuarantineReason` is a projection of the existing `DriftTrigger`
   (success → PERFORMANCE_DEGRADATION, cost/utility → COST_DEGRADATION, context mismatch →
   STRUCTURAL_INVALIDATION, staleness → EVIDENCE_STALE; STATISTICAL_DRIFT for CUSUM-detected
   success drift; VERIFICATION_FAILURE only from revalidation; UNKNOWN when no DRIFTED transition
   can be identified). No new detector triggers.
3. **Case states** `QUARANTINED → REVALIDATION_PENDING → REVALIDATING → RECOVERY_CANDIDATE →
   RECOVERED`, with `REVALIDATING|CANDIDATE → QUARANTINED` on a failed test and terminal `EXPIRED` /
   `BUDGET_EXHAUSTED`. A genuine distinction from M6.2's RECOVERING: the evidence comes from
   *bounded, verified, out-of-band tests*, not from user traffic.
4. **Eligibility is a gate list** (reason, tenant, risk, identity, runtime and verifier known,
   verifier available, minimum evidence, budget, readiness, severity), never automatic.
5. **Structural and statistical recovery are different protocols**: a structural case validates
   compatibility under the *current* runtime/verifier and, on success, **rebases the reference's
   versions** (recorded, lineage-linked); a statistical case needs fresh outcome evidence that the
   success rate is not worse than the reference by more than the margin.
6. **One statistical hypothesis for all strategies** so they are comparable: success rate not
   degraded by ≥ δ = 0.10 relative to the reference (the drift definition), with exact
   (non-asymptotic) error control: fixed sample (A, B) or SPRT (C).
7. **Independent verification**: five test verdicts (success, failure, timeout, invalid output,
   verification unavailable); unavailable outcomes are never successes, consume budget and are
   counted; too many of them end the cycle as VERIFICATION_FAILURE.
8. **Lineage**: every test row records case, ordinal, tick, capability ref, runtime/verifier
   versions, verdict, tokens and time; evidence older than the case, of another tenant or
   duplicated is rejected and recorded.
9. **Persistence and replay**: append-only `controller_revalidation_transitions` and
   `controller_revalidation_tests` (migration 0008), replay with prefix verification.
10. **Experiment**: held-out fresh seeds; M6.2 native recovery, a permanent-quarantine baseline and
    strategies A/B/C on nine scenarios with known ground truth; acceptance criteria and the verdict
    rule frozen before any data; negative and inconclusive results reported.

## 8. Cost reasoning that shapes the experiment (arithmetic, not a result)

Showing non-inferiority within 10 points of a 0.52 reference needs ≈ 150 tests at α = 0.05,
β = 0.20 (≈ 1M tokens at 6,500 each, ≈ 50 utility units at λ = 1/20,000). The simulated benefit of
a restored alternative is ≈ 0.145 utility per decision (alternative: 0.52 − 6,500/20,000 = 0.195;
default: 0.50 − 9,000/20,000 = 0.05), so revalidation breaks even only after ≈ 340 restored
decisions. In an 800-decision stream with recovery at position 400 the margin is thin. M6.3
therefore does not assume that safe recovery is economical; the economics are an outcome.

## 9. Not changed

`default-low`, `default-low-v2`, `serving-low-v1`, M6/M6.1/M6.2 policy definitions and hashes,
`DriftParams`, the overlay schema, `advance`. This document is the only artefact of Phase 0.

## 10. Assumptions and unresolved questions

1. The brief is truncated at §2.4; later phases follow the M6.2 pattern (assumption).
2. **Revalidation task source.** Tests need tasks with an independent verifier and no side
   effects. Assumed: a held-out revalidation pool per (tenant, family) supplied through a port. In
   the experiments it is an independent draw from the synthetic world; in a real deployment it
   would be the benchmark's verified tasks or sandboxed replays. *Unresolved:* whether such a pool
   exists for every family, and who pays for it.
3. **Representativeness.** Out-of-band tests are drawn from the revalidation pool, not from live
   traffic; a covariate shift between them is not modelled. *Unresolved.*
4. **The paired advantage is not retested.** Revalidation tests the alternative against its own
   historical level; the default's drift is observed only through its concurrent production
   outcomes.
5. **Reference uncertainty.** The reference success is a sample estimate (n = 240); the error
   guarantees hold relative to the estimate. The realised false-recovery rate against ground truth
   is reported, not assumed.
6. **Timing.** Tests run in the learning plane at publication boundaries; one test advances the
   clock by one simulated hour (documented in the design). *Unresolved:* latency of real
   revalidation.
7. **No real-model confirmation of the new states** unless the real-table track allows it
   (planned as exploratory, as in M6.2).
8. Whether `max_cycles`, TTL and budget values are right for any real deployment is not answerable
   with synthetic worlds.
