# M6.3 — Controlled Revalidation and Recovery: frozen design and protocol

Status: **frozen before any M6.3 data was generated.** Sections 1–8 are the design; §9 is the
pre-registered experiment; §10 is the log of protocol corrections (empty at freeze); §11 is filled
after the held-out run. Audit: [m6_3_revalidation_audit.md](../research/m6_3_revalidation_audit.md).
Predecessors: [M6.2 design](./evidence-reliability-design.md), ADR 0017.

**Brief truncation.** The brief stops mid-sentence at §2.4. This design implements §0–§2.3 as
written and completes §2.4 (lineage) and the remaining phases (experiment, tests, documentation) in
the M6.1/M6.2 pattern. Everything below that is not in the brief is marked *(assumed)*.

## 1. Research question and principles

**Question.** Can a quarantined cell be restored through *bounded, independently verified,
out-of-band* revalidation, reason by reason, so that (i) cells whose cause has gone away are
restored, (ii) structurally invalidated but healthy cells are restored by compatibility
validation, (iii) materially degraded cells are not restored, (iv) all of it is explainable from
stored evidence — and at what lifecycle cost compared with M6.2's probe-based recovery and with
simply staying quarantined?

Principles (non-negotiable, enforced by tests): M6.1/M6.2 semantics and stored hashes unchanged;
revalidation is opt-in per policy version and **shadow-first**; no recovery from a timer alone;
every recovery decision reproducible from persisted evidence + policy version; negative results are
results.

## 2. Semantics (Phase 1)

### 2.1 Quarantine reasons — by projection, no new detector triggers

`QuarantineReason` is a **projection** of the existing `DriftTrigger` of the DRIFTED transition
that preceded the QUARANTINED one (same decision id), because the spec's names differ from the
trigger names and equivalent values must not be duplicated:

| `DriftTrigger` (existing) | `QuarantineReason` | Protocol |
|---|---|---|
| `SUCCESS_DEGRADATION` (WINDOW detector) | `PERFORMANCE_DEGRADATION` | statistical |
| `SUCCESS_DEGRADATION` (CUSUM detector) | `STATISTICAL_DRIFT` | statistical |
| `COST_DEGRADATION`, `UTILITY_DEGRADATION` | `COST_DEGRADATION` | statistical + cost guard |
| `CONTEXT_MISMATCH` | `STRUCTURAL_INVALIDATION` | structural (compatibility) |
| `EVIDENCE_STALE` | `EVIDENCE_STALE` | statistical, rebases freshness |
| *(none today)* | `VERIFICATION_FAILURE` | ineligible until verification is available; reachable only through an externally supplied reason |
| no identifiable DRIFTED transition | `UNKNOWN` | **ineligible** |

### 2.2 Recovery eligibility — a gate list, never automatic

`check_eligibility` returns the list of failed gates (one reason per gate; empty = eligible) and a
`permanent` flag for conditions that cannot be fixed by waiting:

1. `reason_known` — reason ≠ UNKNOWN (permanent).
2. `reason_revalidatable` — VERIFICATION_FAILURE requires `verification_available` (transient).
3. `tenant` — executor tenant = policy tenant = case tenant (permanent).
4. `risk` — risk class ∈ `allowed_risks` (default `["low"]`) (permanent).
5. `identity` — the executor supports the alternative's action key / capability reference
   (transient).
6. `versions_known` — the executor reports the current runtime and verifier versions (transient);
   without them lineage and structural rebasing are impossible.
7. `verification_available` — the executor reports the independent verifier reachable (transient).
8. `pool` — at least `min_pool` unused revalidation tasks (transient).
9. `budget` — cycles used < `max_cycles`; lifetime tokens spent + planned minimum ≤
   `cell_token_budget` (permanent when it can never fit).
10. `ttl` — case age ≤ `case_ttl_hours` (permanent → EXPIRED).
11. `readiness` — strategy-specific (§4), transient.
12. `severity` — not a gate: historical severity (success drop at the DRIFTED transition) selects
    the error rate (§5) and the cooldown multiplier.

Hard safety constraints are not weakened: protected risk classes never read the overlay and are not
eligible (gate 4).

Structural vs statistical: a `STRUCTURAL_INVALIDATION` case never counts a version change as
evidence of recovery — it runs the same *performance* hypothesis test under the **current**
versions and only then rebases the reference's runtime/verifier labels. A statistical case whose
trials report versions different from the reference's is a lineage violation (§7).

### 2.3 Case states

Reuse the M6.2 machine unchanged; the *revalidation case* is a second, separate machine per
quarantine episode (a genuine distinction: evidence from bounded, verified, out-of-band trials
rather than user traffic):

```
                    ┌──────────────── cycle failed (cycle+1) ───────────────┐
                    ▼                                                       │
QUARANTINED ──ready──▶ REVALIDATION_PENDING ──trials start──▶ REVALIDATING ─┤
     ▲                                                          │  criteria met
     │                                                          ▼            │
     └────────── confirmation failed ◀──────────── RECOVERY_CANDIDATE ◀──────┘
                                                          │ confirmed
                                                          ▼
                                                     RECOVERED  ──▶ cell NORMAL (re-armed)
REVALIDATING | RECOVERY_CANDIDATE | PENDING | QUARANTINED ──ttl──▶ EXPIRED (terminal)
REVALIDATING | RECOVERY_CANDIDATE ──trial/token cap, cycles exhausted──▶ BUDGET_EXHAUSTED (terminal)
```

`EXPIRED` and `BUDGET_EXHAUSTED` are terminal for the episode: the monitor stays `locked`
(quarantined) until a new policy version (same meaning as M6.2's episode cap).

Integration without changing M6.2: when the M6.2 machine enters QUARANTINED the fleet opens a case
and sets the existing `MonitorState.locked` flag, which disables native recovery. The decision-facing
overlay is the M6.2 overlay: QUARANTINED/DRIFTED cells (including cases in PENDING, REVALIDATING and
RECOVERY_CANDIDATE) are *prior-disabled*, with `probes_remaining = 0` (no live probes). Only
RECOVERED removes the cell: the monitor state is reset to NORMAL with empty windows (`seq`,
`episode` and counters preserved, so a relapse still counts toward `max_episodes`). Two additive
helpers on `MonitorFleet` (`restore`, `replace_reference`) implement this; `advance` is untouched.
In `SHADOW` mode cases open and eligibility/readiness are recorded but **no trial is executed**
(zero cost; `applied=false`).

### 2.4 State invariants (each has a test)

* I1 A quarantined cell is prior-disabled in the overlay in every case state except RECOVERED.
* I2 No trial verdict is a success without independent verification; `VERIFICATION_UNAVAILABLE` is
  never a success, never counted in n, consumes budget, and is reported.
* I3 `RECOVERY_CANDIDATE` is prior-disabled.
* I4 Evidence used for a recovery decision is newer than the evidence that triggered quarantine
  (trial `observed_at` > the trigger transition's `observed_at`) and carries the case's tenant;
  violators are rejected and recorded, never silently dropped.
* I5 Every RECOVERED transition lists its evidence (trial ordinals, versions), measurements,
  strategy, error rates and parameter hash.
* I6 Trials ≤ `max_trials`, tokens ≤ cycle cap, lifetime tokens ≤ `cell_token_budget`.
* I7 No path from time alone: with an executor that never returns a verified success, no sequence
  of ticks reaches RECOVERED (property test).
* I8 Tenant isolation: foreign-tenant observations and trials never enter a case.
* I9 Reproducibility: replaying the persisted observations and trials with the persisted policy
  version recomputes every transition.

## 3. Verification (Phase 2.3)

`TrialVerdict` ∈ {`SUCCESS`, `FAILURE`, `TIMEOUT`, `INVALID_OUTPUT`, `VERIFICATION_UNAVAILABLE`}.
`classify_run_outcome` maps the existing `OutcomeStatus`/`VerificationResult`: VERIFIED_SUCCESS →
SUCCESS only when every goal criterion has `passed is True`; UNKNOWN or any `passed is None` →
`VERIFICATION_UNAVAILABLE`; PARTIAL → FAILURE (never success). The executor adapter (not free-text
parsing) reports TIMEOUT and INVALID_OUTPUT. Counted evidence: SUCCESS = 1; FAILURE, TIMEOUT and
INVALID_OUTPUT = 0 (they are in n); VERIFICATION_UNAVAILABLE excluded from n but reported. If after
≥ 10 trials of a cycle more than 20 % are unavailable, the cycle fails with
`VERIFICATION_UNAVAILABLE` (never a recovery; counted as a failed cycle; the tokens are spent). The
revalidation harness cannot manipulate any success metric: outcomes are written only by the executor
port and stored as trial rows.

## 4. Strategies (Phase 2.2)

One statistical hypothesis for all three, so they are comparable. Let `p0` be the reference
success of the alternative (k_alt/n of the prior), `δ = 0.10` (the drift definition),
`p_b = p0 − δ`, `p_g = p0`. **Recovery criterion:** reject "p ≤ p_b" with error ≤ α, power ≥ 1 − β
at p_g, plus a cost guard (mean trial tokens ≤ 1.15 × the reference mean, the M6.2 value). All
error control is exact (binomial), not asymptotic.

| | A — FIXED | B — EVIDENCE_THRESHOLD | C — SEQUENTIAL |
|---|---|---|---|
| Readiness | dwell ≥ 50 default-served decisions since opening or the last failed cycle | dwell ≥ 50 **and** ≥ 30 default-served *verified* outcomes since then **and** the default arm not confidently degraded (Newcombe UB at 0.90 of its success change < −δ) | dwell ≥ 50 |
| Test | N trials, N = smallest size whose exact one-sided test has size ≤ α and exact power ≥ 1 − β at p_g; recover iff k ≥ k_crit | same as A | one trial at a time; SPRT p_b vs p_g, boundaries ln((1−β)/α) / ln(β/(1−α)); truncated at `max_trials` |
| Decision | only after all N trials (no peeking) | same | after every trial |
| Inconclusive | – | – | truncation → BUDGET_EXHAUSTED |

A uses a deliberately simple fixed interval/size (the baseline); B adds an *evidence* gate on
when to spend (the default arm's concurrent verified outcomes are free); C adds sequential
stopping with the same error targets. No Bayesian or adaptive policy is included.

## 5. Frozen parameters (`RevalidationParams` defaults)

`margin δ 0.10`, `alpha 0.05`, `alpha_severe 0.025` when severity ≥ `severe_drop 0.25` (severity =
reference success − success at the DRIFTED transition; 0 when unavailable), `beta 0.20`,
`dwell 50` (severe: ×2), `trials_per_round 50` with `round_hours 10` (E2; originally 10), `max_trials 320`,
`cost_futility_min_trials 15` and `cost_futility_confidence 0.95` (E1),
`max_unverified_fraction 0.20` after `min_unverified_trials 10`, `max_cycles 3`,
`max_cycle_hours 400`, `case_ttl_hours 500`, `cell_token_budget 3,000,000`, `min_pool 50`,
`cost_guard 1.15`, `confirm_gap_hours 20`, `confirm_trials 20`, `confirm_alpha 0.01`,
`allowed_risks ["low"]`, `ready_default_min 30`, `ready_confidence 0.90`.

**Confirmation (RECOVERY_CANDIDATE → RECOVERED).** After the criterion is met the cell waits
`confirm_gap_hours`, then runs `confirm_trials` fresh trials. Confirmation is a **collapse and
instability guard, not a second proof of non-inferiority** (20 trials cannot resolve a 10-point
margin): it fails only if the exact one-sided binomial test rejects "p ≥ p_b" at `confirm_alpha`
(for p_b = 0.42 that needs ≤ 2 successes of 20) or the unverified rule triggers. It exists to catch
a regime flip between the evidence and the restoration; whether it is worth its cost is an
ablation, not an assumption.

**Rebasing.** On RECOVERED: STRUCTURAL_INVALIDATION → the reference's runtime and verifier labels
become the trials' (all trials must report one version pair); EVIDENCE_STALE → the reference's
`recorded_at` becomes the time of the last trial; otherwise nothing changes. The historical prior
record is never edited; the rebase is an event in the revalidation log, re-applied by replay.

## 6. Resource limits

Per cycle: `max_trials`, a token cap of `1.5 × max_trials × reference mean tokens`, `max_cycle_hours`.
Per cell: `max_cycles`, `cell_token_budget`. Per round: `trials_per_round`. Exceeding the trial or
token caps without a decision → BUDGET_EXHAUSTED; exceeding the time limits → EXPIRED.
Wall-clock is measured in the experiment's simulated hours (decision i is at SIM_EPOCH + i hours;
trial j of a round at the round's time + j × `round_hours / trials_per_round`, see E2); real
executors report their own timestamps.

## 7. Lineage (Phase 2.4, assumed completion)

Every trial row records: tenant (table scope), policy id/version, cell key, episode, cycle,
ordinal, stage (EVIDENCE | CONFIRMATION), round anchor (last ingested decision id), verdict,
tokens, latency (when measured), runtime and verifier versions, capability reference, task
reference, observation time, `accepted` and `reject_reason`. Rejection reasons: `NOT_NEWER`
(≤ the trigger's observation time), `TENANT_MISMATCH`, `DUPLICATE` (task reference reused),
`VERSION_MISMATCH` (statistical case, versions ≠ reference). Five `VERSION_MISMATCH` rejections
end the cycle with `LINEAGE_VIOLATION` and reclassify the case as STRUCTURAL_INVALIDATION for its
next cycle. Every transition row records: cell, episode, cycle, per-cell sequence, from/to state,
trigger, reason, strategy, measurements, textual detail (failed gates, rebases), trials and
unverified counts, cycle tokens, round anchor and time, and the parameter hash.

## 8. Persistence and replay

Migration 0008: append-only `controller_revalidation_transitions` and
`controller_revalidation_trials` (database triggers reject UPDATE/DELETE, as in 0007; downgrade
refuses to drop non-empty tables). `RevalidationService.replay` recomputes the fleet from the
ordered outcomes and the persisted trials (a replay executor returns persisted trials by
(cell, episode, cycle, ordinal) and nothing else), checks that the persisted transitions are a
prefix of the recomputation, and returns the overlay; `.run(executor)` appends the live part.
Execution never waits for it: the overlay is read at controller build.

## 9. Pre-registered experiment

**Variants** (identical task streams, perturbations and prior; common random numbers):
`m61` (coarse guard), `m62` (M6.2 ENFORCE, native probe recovery), `m62_locked` (M6.2 ENFORCE with
recovery disabled: `min_quarantine = 10⁹`; permanent quarantine), `m63_A`, `m63_B`, `m63_C`
(M6.2 ENFORCE + revalidation, native recovery disabled, no live probes).

**Scenarios** (single family, 800 decisions, onset 250, world and prior as in M6.2): *should
restore* — S5 temporary disturbance [250,400); S8 runtime change, performance unchanged; S8b
stale evidence (`max_evidence_age_days = 250/24`); R3 common-cause outage (alternative survival
0.35 *and* default survival 0.50 over [250,400)); R4 temporary disturbance [250,400) plus a
revalidation-verifier outage over [380,600) (trials return VERIFICATION_UNAVAILABLE); R5 relapse
(degraded [250,400), healthy [400,550), degraded again from 550). *Must not restore* — S2
persistent collapse (survival 0.35 from 250); S4 persistent cost drift (×1.5); R1 residual
degradation (survival 0.35 over [250,400), then 0.70 from 400 — a 15.6-point drop, still material);
R2 structural change *and* degradation (runtime rt-2, survival 0.35 from 250).

**Ground truth.** "Degraded at position i" = the alternative's success drop ≥ 0.10 or cost
increase ≥ 30 % (the M6.2 `material` definition **without** the runtime flag, which is a structural
change, not a performance change). A restoration is *false* if the cell is degraded at the round
that restores it.

**Seeds.** Development (smoke and any calibration, none planned): 110000–110199. Audit exploration
seeds 7100000–7100011 and all M6.2 seeds are **excluded**. Held-out: **9000000–9000099** (100
streams per scenario). Unit-test seeds 5,000,000+ as before.

**Metrics per stream and variant:** verified successes; production tokens; revalidation tokens and
trials (separately and summed); utility = successes − (production + revalidation tokens)/20,000;
restoration (yes/no, position), time to restoration after the world recovered, false restoration;
cycles started/failed; terminal states; verification-unavailable trials; evidence rejected.

**Acceptance criteria** (a strategy "meets" a criterion if its results satisfy it; pooled exact
Clopper–Pearson one-sided 95 % bounds):
* (a) **Safety.** False-restoration streams over the must-not-restore scenarios (S2, S4, R1, R2;
  400 streams) have exact upper bound ≤ 5 %, and R1 alone ≤ 10 %.
* (b) **Recovery.** Restored by the end of the stream: S5 ≥ 70 %, R3 ≥ 70 %, R4 ≥ 70 %.
* (c) **Structural.** S8 ≥ 90 % and S8b ≥ 90 % restored; in R2 false restoration (included in a).
* (d) **Verification.** Zero recovery transitions whose evidence counts an unavailable trial as
  success (exact 0 required; also unit-tested); in R4 no restoration whose evidence set includes
  more than 20 % unavailable trials.
* (e) **Economics.** Pooled over S5, R3, R4, S8, S8b: net utility (revalidation tokens included)
  of the strategy minus that of `m62` has a paired-bootstrap 95 % CI lower bound > 0; reported
  against `m62_locked` too.
* (f) **Sequential saving (comparative).** C's mean revalidation tokens per stream ≤ 80 % of A's
  with the paired 95 % CI upper bound of the ratio < 1.0, at no worse safety (a).
* (g) **Reproducibility.** The in-the-loop JSON of two runs is byte-identical; persisted-log replay
  reproduces every transition.
* (h) **Backward compatibility.** M6/M6.1/M6.2 definition hashes and decision digests unchanged.

**Verdict rule (clauses evaluated in order).**
1. NOT SUPPORTED if (a) fails for every strategy, or no strategy restores more S5 streams than
   `m62`.
2. SUPPORTED if one strategy meets (a), (b), (c), (d) and (e) (and (g), (h) hold).
3. Otherwise PARTIALLY SUPPORTED if one strategy meets (a) and (d) and at least one of (b), (c).
4. Otherwise INCONCLUSIVE.
The strategy reported as the candidate is, among those meeting (a)–(d), the one with the lowest
mean revalidation tokens. (b)–(c) failing while (a) holds is a safe but unhelpful design, and is
reported as such.

**Analysis.** Streams are independent units (one seed = one stream); CIs by bootstrap over streams
(paired across variants), exact binomial bounds for rates; no multiplicity correction is applied
across the ten scenarios (descriptive per-scenario tables), the pre-registered criteria are the
pooled ones above. Sensitivity runs (exploratory, development seeds only): `confirm_trials 0`
ablation; `alpha 0.10`; `trials_per_round 10` (the pre-E2 throughput); `margin 0.15`. They cannot change the frozen configuration.

**No threshold may be changed after the held-out run starts.** Calibration is not planned: every
parameter above is derived from the M6.2 definitions or conventional error rates. A development
smoke run is for protocol defects only; any resulting change is logged in §10 *before* held-out
data exists.

**Real-model track (exploratory, if time allows).** The M6.2 real-table machinery supplies the
revalidation pool from recorded planner outcomes with the same synthetic overlays; reported
separately and never used to select anything.

## 10. Protocol corrections (before held-out data)

Found by a **development smoke run** (seeds 110000–110002, 6 variants, all 10 scenarios; protocol
defects only; no held-out seed was generated). Each correction applies to **all strategies
equally** and was committed before any held-out data existed.

* **E1 — cost futility.** The frozen cycle checked the cost guard only when the *success* criterion
  was reached. In S4 (cost ×1.5) every cycle therefore ran to its full length (A: the whole 3M
  lifetime budget, ≈ 300 trials) before failing on cost. A cost problem is visible in the first
  ~15 trials (token CV 0.35 → a 1.5× cost is about four standard errors). Added: after
  `cost_futility_min_trials = 15` counted trials a cycle fails with `CRITERIA_NOT_MET` ("cost
  futility") when the one-sided 95 % *lower* bound of mean tokens exceeds `cost_guard × reference
  mean`. It can only end a cycle earlier with a failure, so it cannot create a recovery or inflate
  the false-recovery rate; it can raise the false-failure rate (≤ 5 % per cycle, only when cost is
  genuinely about 15 % too high). It is a deliberate exception to A's "no peeking" because the
  peeked endpoint (cost) can only veto. In the smoke run S4 cost dropped from 3.0M to 0.44M tokens
  per stream with no change in outcome (no restoration).
* **E2 — trial throughput.** The frozen clock mapped one trial to one simulated hour, so a
  ≈ 200-trial test occupied ≈ 200 simulated hours of an 800-decision stream, and the smoke run
  showed that almost no cycle could complete: the experiment would have measured the clock mapping,
  not revalidation. The decision clock (one decision per hour) is itself an M6.2 modelling choice,
  far slower than real task execution. Changed `trials_per_round` 10 → **50** and added
  `round_hours = 10` (the learning-plane round length): trials of a round are spaced
  `round_hours / trials_per_round` = 12 simulated minutes, so a 200-trial test takes about 4 rounds
  (40 simulated hours). Token costs are unaffected. `trials_per_round = 10` (the frozen value)
  remains an exploratory sensitivity setting (§9). The time-to-restoration results are bounded by
  this assumption and are reported as such.

Smoke-run observations that were **not** changed (they are consequences of the frozen design, not
defects): (i) a cycle fails by chance in about a quarter of healthy worlds (exact power at the
planned effect ≈ 0.73–0.80 given the reference's own sampling error; the reference is unbiased,
mean 0.521 for a true 0.52); (ii) the truncated SPRT is inconclusive (BUDGET_EXHAUSTED) when the
true success lies between p_bad and p_good, which costs ≈ 2M tokens; (iii) with α_severe the lifetime
budget of 3M tokens fits about two cycles; (iv) A and B are identical wherever the default arm is
healthy (they differ only in R3); (v) revalidation spends 0.4–2.7M tokens per stream, against a
restored-decision benefit of ≈ 0.145 utility (≈ 2,900 tokens), so the economic criterion (e) is
expected to be hard to meet; this was known from the audit's §8 arithmetic.

## 11. Outcome

*(filled after the held-out run)*
