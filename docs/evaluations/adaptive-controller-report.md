# Adaptive strategy controller: evaluation report (M6)

Real model `gemini-2.5-flash` (Vertex) on the simulated benchmark v3 (pool and timeout families), plus synthetic worlds with known ground truth for the mechanism. Design, audit, the frozen protocol and every amendment: [experiments/adaptive-controller-design.md](../experiments/adaptive-controller-design.md) (§11 protocol was committed before any recorded or real-model result). Generated tables: [adaptive-controller-tables.md](./adaptive-controller-tables.md); recorded data: `data/m6-s3-*.json`, `data/m510-serving-set-{c,d}.json`. Operating guide: [../controller.md](../controller.md). Decision: [ADR 0015](../decisions/0015-adaptive-controller-bounded-learning.md).

**Nothing was promoted and no learned policy was activated in production. At the frozen parameters the controller did not beat the rule baseline on held-out sequences; the evidence below documents why, and what the mechanism can do when given more exploration.**

## 1. Verdicts at a glance (frozen rule, design A2)

BEATS = success non-inferior (cluster-bootstrap lower bound ≥ −5 points) **and** tokens per verified success lower (upper bound of the ratio < 1). EQUAL = non-inferior, not shown cheaper, not shown worse. WORSE = confidently inferior, or non-inferior and confidently > 5% costlier. One order set = 20 seeded orders of the recorded tasks; unit of inference = the task; exploration cost is inside the numbers.

| Study | Regime | Primary (warm start) cost-aware | Primary bandit | Cold start (cost-aware / bandit) |
|---|---|---|---|---|
| S2 set C (M5.10, real) | rule is right (good capabilities) | EQUAL | **WORSE** (−3.0 pts, +9.5% tokens/success) | EQUAL / EQUAL |
| S2 set D (M5.10, real) | rule is right | EQUAL | **WORSE** (−2.8 pts, +7.9%) | EQUAL / EQUAL |
| S3 held-out H1 (real, fresh seed) | rule is wrong for timeout (stale capability) | EQUAL | EQUAL (−1.9 pts, ratio 1.024 [1.012, 1.039]) | EQUAL / EQUAL |

**The learned controller never beat the rule baseline at the frozen parameters.** The cost-aware policy is a safe no-op at this scale (it deviates in 0% of decisions: a rule-only log contains no evidence about any alternative). The bandit pays an exploration premium that is not recovered within 120 tasks. Post-hoc sensitivity (§5) shows it can beat the rule baseline when the rule is wrong, with more exploration and longer streams.

## 2. What was evaluated

* **Controllers:** `rule-capability/1` (baseline), `adaptive/N` cost-aware (deterministic) and bandit (ε-greedy with an explicit family/risk allow-list, data gate, exact logged propensities). Frozen defaults: n_min 30 verified trials per cell, ≥100 verified in total, ε 0.10, 10-point non-inferiority margin against the default (Newcombe–Wilson, 95% one-sided), ≥10% cost saving, calibration gate ECE ≤ 0.10 over ≥100 scored predictions, high risk never learned.
* **Outcome unit:** the final verified outcome of the whole run including the planner fallback (the M5.10 lesson); UNKNOWN excluded. Cost = tokens per verified success.
* **Method:** prequential full-information replay (the controller learns only from the action it chose, estimator republished every 10 tasks) over paired recorded runs (arm A planning, arm E production path); 20 seeded task orders; per-task cluster bootstrap (10,000 resamples). One recorded draw per task and arm, so this replays a stochastic process rather than repeating it.
* **Stress portfolio (S3):** hand-authored through the validated DSL, in memory, never registered or promoted: a compiler-shaped pool capability and a timeout capability whose target key is frozen to a **retired** layout (a stale capability: the tool reports "key not found", the capability hands over to the planner after spending its own tokens). The first fixture (D1: key frozen to a layout that still exists in some variants) turned out to be variant-dependent and did not create a family-level regime where the rule is wrong; it was redesigned once (design §12) before the held-out set was generated.

## 3. S1: mechanism (synthetic worlds, ground truth known; not evidence about real models)

From `tests/unit/test_controller_*.py` (all pass):

| Property | Result |
|---|---|
| Empty evidence reproduces the rule baseline exactly | yes (selected action, capability and probability identical to `rule-capability/1` for 0, 1 and 2 eligible capabilities) |
| Only matcher-eligible capabilities are selectable | yes (evidence for a non-offered capability is never used) |
| Protected risk classes and foreign tenants get the rule decision, probability 1, no estimate read, no draw | 200 random snapshots × {high, medium}, both kinds, ε = 0.5: 0 deviations |
| Cheaper-but-less-successful alternative is never chosen | yes (tokens never buy correctness); optional success floor displaces a default that misses it |
| Non-inferiority needs evidence, not a point estimate | identical point estimates: deviates at 300 trials, not at 30 |
| Exploration off unless family **and** risk are allow-listed, calibration OK and ≥100 verified | yes |
| Logged propensities exact and empirically correct | 6,000 draws: 10.0% / 90.0% as logged; probabilities sum to 1 |
| Decisions replay exactly from stored features + version; tampering and version drift are detected | yes |
| Rule right (cap 90% / 2k vs planning 30% / 5k tokens) | passive policy EQUAL (never deviates); bandit EQUAL at 3,000 tasks, but **not free at 600 tasks** (WORSE: confidently costlier per verified success, 10+ successes lost to exploration) |
| Rule wrong (stale capability: same success, 2.4× the tokens) | bandit **BEATS** over 3,000 tasks (>90% planning in the late half); passive policy cannot deviate |
| Drift (capability collapses mid-stream) | a bounded training window tracks it; cumulative estimates lag |
| Calibration gate | accepts a stationary world; rejects a drifting one (ECE) and too little history |
| Off-policy estimators | unbiased (IPS/SNIPS/DR within 3 points) on a 50/50 randomised log; **zero coverage** on a rule-only log (it cannot say anything about a deviation) |

## 4. S2 and S3: real recorded data (primary, frozen)

### Per-action truth in the recorded tables (full information)

| Set | family | capability (final outcome incl. fallback) | planning | tokens per verified success: capability vs planning |
|---|---|---|---|---|
| S2 C (M5.10) | pool | 100% | 23% | 2.0k vs 22.3k |
| S2 C | timeout | 92% | 45% | 3.6k vs 14.5k |
| S3 H1 | pool (good) | 100% | 20% | 1.9k vs 26.3k |
| S3 H1 | timeout (**stale**) | 52% | 50% | **17.2k vs 11.2k** (+54%) |

S2 is the "rule is right" regime: the capability dominates planning in every cell, so the best a controller can do is not to lose. S3 timeout is the "rule is wrong" regime: no success gain, 54% more tokens per verified success. Development sets: D1 (pre-redesign fixture) timeout 55% vs 47% at equal cost; D2 (redesigned) timeout 50% vs 50% at 17.9k vs 12.9k tokens per verified success, 60/60 runs fell back to the planner.

### Results (20 orders; warm start from the other set's rule-only log; both kinds)

| Set | controller | verdict | success (ctl vs rule) | tokens/success (ctl vs rule) | success diff [one-sided 95% bounds] | cost ratio [bounds] | deviates / explores | exploration cost per order (lost successes / tokens vs default) | protected deviations |
|---|---|---|---|---|---|---|---|---|---|
| S2 C | cost-aware | EQUAL | 95.8% vs 95.8% | 2,782 vs 2,782 | +0.0 [0.0, 0.0] | 1.000 | 0% / 0% | 0 / 0 | 0 |
| S2 C | bandit | **WORSE** | 92.8% vs 95.8% | 3,045 vs 2,782 | −3.0 [−3.7, −2.4] | 1.095 [1.077, 1.114] | 4.7% / 10.6% | 3.6 / +19,286 | 0 |
| S2 D | cost-aware | EQUAL | 95.0% vs 95.0% | 2,946 vs 2,946 | +0.0 | 1.000 | 0% / 0% | 0 / 0 | 0 |
| S2 D | bandit | **WORSE** | 92.2% vs 95.0% | 3,180 vs 2,946 | −2.8 [−3.4, −2.1] | 1.079 [1.063, 1.098] | 4.7% / 10.6% | 3.3 / +15,942 | 0 |
| S3 H1 | cost-aware | EQUAL | 76.3% vs 76.3% | 7,027 vs 7,027 | +0.0 | 1.000 | 0% / 0% | 0 / 0 | 0 |
| S3 H1 | bandit | EQUAL | 74.4% vs 76.3% | 7,199 vs 7,027 | −1.9 [−2.5, −1.3] | 1.024 [1.012, 1.039] | 4.6% / 10.4% | 2.2 / −708 | 0 |

Secondary (cold start) is EQUAL for every set and kind (the data gate delays exploration: deviation 0.2–0.4%). With 50% of tasks relabelled high risk the protected deviations are **0** in all six runs and the bandit's premium shrinks proportionally.

**Why the controller did not win S3.** In the stale timeout cell planning is 35% cheaper per verified success at equal success, but to *replace* the rule default the policy must show the alternative non-inferior at a 10-point margin with 95% confidence. The alternative has no evidence at first and is explored ~5% of the time (ε/m), so a 120-task order yields ~3 planning samples in the timeout cell (measured); even in a 1,500-task stream the cell reaches ~39 planning samples against ~750 for the capability, and the Newcombe lower bound on the difference (about −0.20 at equal point estimates) does not clear −0.10. The conservative gate protects against a bad switch, and it is also what prevents a good one at this scale.

**Why the bandit loses in S2.** When the rule is right every exploratory run of planning is a pure loss. Acquiring the minimum evidence (30 planning trials per learned family) costs about 60 bad runs: ≈3.5 successes and +16–19k tokens per 120-task stream, and 33–38 successes per 1,500-task stream, because the premium is paid at a 5% rate until each cell has 30 samples. It is a one-off insurance premium (per cell), not a loss per decision, but it is not recovered when the rule was already right.

## 5. Exploratory post-hoc sensitivity (NOT the frozen parameters; run on the held-out table after the verdict)

Streams resampled with replacement from the recorded H1 table (a model-based extrapolation: repeated draws re-use recorded outcomes and add no information):

| ε | horizon | verdict | success diff [bounds] | cost ratio [bounds] | deviates | exploration cost per stream |
|---|---|---|---|---|---|---|
| 0.10 (frozen) | 1,500 | EQUAL | −1.5 [−2.0, −1.0] | 1.013 [1.004, 1.023] | 5.2% | 23 successes |
| 0.20 | 1,500 | BEATS | −1.8 [−3.7, 0.0] | 0.938 [0.911, 0.967] | 18.2% | 26 successes |
| 0.20 | 3,000 | BEATS | −1.2 [−3.7, +1.3] | 0.875 [0.840, 0.913] | 24.3% | 26 successes |
| 0.30 | 1,500 | BEATS | −1.7 [−4.0, +0.7] | 0.907 [0.874, 0.942] | 23.0% | 24 successes |
| 0.30 | 3,000 | BEATS | −1.3 [−4.7, +2.0] | 0.832 [0.788, 0.880] | 31.7% | 26 successes |

So the mechanism *can* beat the rule baseline when the rule is wrong (12–17% fewer tokens per verified success), once it has explored enough, and the success loss stays inside the 5-point margin. This is not a claim that ε 0.2–0.3 is right: the values were chosen after seeing the held-out table, the horizons are extrapolated, and the same exploration would cost more in a rule-right regime (S2: ε 0.1 is already WORSE at every horizon tried). The same mechanism that wins here loses there; what separates them is knowing which regime one is in, which the current estimator learns only by paying for exploration.

## 6. S4: live registry rollout on the real model (mechanism evidence, small n)

Fresh private seed L1 (120 pool/timeout tasks), the same stress portfolio, real `gemini-2.5-flash`, a PostgreSQL registry (`ee-ctl live` builds the controller from it). Full output: [data/m6-s4-live-rollout.log](./data/m6-s4-live-rollout.log). Each stage re-runs the same 120 tasks (fresh stochastic draws), so decisions are not independent across stages.

| Stage | What happened | Evidence |
|---|---|---|
| 0 | `rule-capability/1` serves 120 tasks; decisions logged | 88/120 verified, 0.70 M tokens |
| train | `adaptive/1` (bandit; explore timeout, pool; defaults) from the log | 120 verified decisions, calibration n=110, ECE 0.050, ok |
| OFFLINE | replay on the log | **PASS** (0 protected deviations, 0 infeasible choices, coverage 0.95: the candidate puts 5% of its probability on unsupported exploration) → SHADOW |
| SHADOW | candidate decides alongside the rule on 120 tasks (120 non-acted decisions stored) | **PASS** (agreement 98.3%, live ECE 0.058, 0 errors) → CANARY |
| CANARY (fraction 0.5) | candidate serves 60 tasks, rule serves 60 | see below |
| after | v2 trained on all 360 acted decisions | OFFLINE **PASS** (coverage 0.958, agreement 99.7%, 349 scored predictions, ECE 0.031) |

**A defect in my canary evaluation was found by this run and corrected.** The first canary evidence compared the candidate (60 decisions) with **300** control decisions: every `rule-capability/1` decision of the earlier non-concurrent stages was counted as control. That is not the concurrent comparison the design requires, yet the registry accepted the PASS and `adaptive/1` was promoted to ACTIVE (14:54:35). The audit trail kept all of it (append-only evidence and transitions):

1. canary PASS on non-concurrent control (promotion);
2. an evaluation run *after* the promotion that used the candidate as its own control (INCONCLUSIVE, `control_version=adaptive/1`) is invalid, which is why `ee-ctl evaluate canary` now refuses any version that is not in CANARY;
3. rollback of `adaptive/1` (ACTIVE → ROLLED_BACK, terminal), because it had been promoted on defective evidence;
4. the corrected concurrent comparison, recorded afterwards from the stored window ([data/m6-s4-canary-corrected.json](./data/m6-s4-canary-corrected.json)): candidate 45/60 = 75.0%, control 41/60 = 68.3%, difference lower bound −0.068 (≥ −0.10), tokens-per-success ratio 0.82: **PASS** at 60/60.

Fixes with regression tests (each kills its mutant): canary control must come from the canary window (`outcome_records(since=…)`) and only from the risk classes the candidate may serve; the registry rejects CANARY evidence that predates the canary period; canary evaluation is only valid in the CANARY state. **Reading of the corrected result:** the promotion would have been justified on the corrected evidence, so the rollback was a policy response to a defective procedure rather than evidence of harm. The 0.82 token ratio is **not** attributable to the policy: the candidate deviated from the rule in 1 of 60 decisions (1.7%), so the difference is task mix and model noise, and 60 per arm leaves the non-inferiority bound only just inside the margin (with truly equal rates the 10-point margin at 95% needs about 100 per arm at 75% success, about 133 at 50%, about 53 at 90%; computed with the same Newcombe–Wilson bound).

What S4 does show: every registry path works on the real model (logging, training from the log, offline/shadow/canary evidence, DB-enforced promotion, replay of all 180 stored candidate decisions with 0 differences, rollback restoring the baseline), and an evaluation bug of the kind that matters was caught by running it live. What it does not show: any controller benefit (the candidate behaved like the rule baseline; the log contains 1 acted and 2 shadow planning decisions, so even v2 has almost no counterfactual evidence about planning).

## 7. Cost accounting

* **Learning cost of the controller: 0 model tokens.** Tabular training, calibration, replay and the simulator are deterministic CPU work; `ee-ctl train` took well under a second on 120 decisions.
* **Exploration cost** (reported on every comparison): bandit ≈ 2.2–3.6 successes and ≈ −0.7k…+19k tokens per 120-task stream at ε 0.10, rising to ≈ 23–38 successes per 1,500-task stream until each explored cell has 30 trials.
* **Validation cost (real model):** D1 1.24 M tokens, D2 1.36 M, H1 1.29 M (240 runs each: arms A and E), plus S4 (≈1.98 M tokens: 0.70 M + 0.65 M + 0.64 M over the three live stages). M5.10 sets C/D reused (no new tokens). Evidence-gate tokens are the controller's real lifecycle cost; at ~1.3 M tokens per 120-task paired set versus a per-stream saving of ~0.9–2.1k tokens per verified success, this is only repaid over long production streams.
* Not counted: canary traffic risk (none here: the canary served stress tasks only), human review time. The canary defect (§6) cost one extra rollback and a re-evaluation, not model tokens.

## 8. Limitations, negative and inconclusive results

* **Primary result is negative/neutral:** no BEATS at frozen parameters; the bandit is WORSE in the rule-right regime (S2) by a small, bounded premium.
* The estimator is tabular over `(family, risk, action)`: within-family variant effects are invisible (the D1 fixture showed this: a capability good on some layouts and bad on others looks like "no difference").
* One recorded draw per task and arm; orders resample the same tasks, so effective sample size is the 120 tasks, not the 2,400 order-task pairs; long-horizon streams are resamples, not new data.
* The S3 portfolio is a hand-authored stress fixture (labelled); real stale capabilities may differ. The retired-key fixture fails 100% on the capability phase by construction; only the *cost* and fallback behaviour were measured, not discovered.
* Shared development and held-out *process* (same generator and model), different private seeds; both sets of M5.10 and the H1 set use the same model and family structure. No claim is made about other models, tasks, or tenants. Safety clauses on cross-tenant access and protected resources are exercised only by property tests and relabelled-risk replays, not by a real multi-tenant or protected-resource benchmark.
* HUMAN_ESCALATION is never selected; no latency model.
* Exploratory sensitivity (§5) was run after the verdict on the held-out table and must not be used to claim improvement.

## 9. Acceptance criteria (roadmap M6)

| Criterion | Status |
|---|---|
| Decisions reproducible from stored features and policy version | **Met**: `replay_decision` + `ee-ctl replay`; unit and DB-backed integration tests (stored adaptive decisions reproduce after a Postgres round-trip; tampering detected). |
| Controller optimises total cost subject to the required success/risk threshold | **Met by construction and tests**: constraints (eligibility, protected classes, non-inferiority, floor) outside the cost objective; tokens never buy lost success. |
| Beats the rule baseline on held-out sequences, or documents evidence that it does not | **Documented: does not at frozen parameters** (S2 WORSE for the bandit, S3 EQUAL); exploratory sensitivity shows capacity under different parameters. |
| No regression in protected/high-risk scenarios | **Met**: 0 protected deviations in property tests (400 random snapshots) and six relabelled-risk replays; canary router never routes protected tasks; the DB blocks any promotion without evidence. |
