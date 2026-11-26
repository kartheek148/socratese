# M5.9 Phase 0 — Promotion-policy and validation-cost audit

Written **before** any code change. Question: do the two M5.8 problems — (1) the absolute `max_regressions <= 1` rule rejected assets that looked clearly beneficial in a separate comparison, (2) validation was ~97% of measured learning cost — come from **policy design**, **evaluation methodology** or **inefficient execution**? The current policy is not assumed wrong; each claim below is tied to evidence.

## 0. Evidence and its limits

| Source | What it gives | Limit |
|---|---|---|
| `docs/evaluations/gated-validation/*.md` (8 per-asset reports, validation seed P1, 2 repetitions) | per suite: runs, distinct tasks, verified, tokens; regressions; false applicability; verdict reasons | aggregates only |
| `docs/evaluations/data/gated-scoped-unvalidated-gemini-2.5-flash.json` (P2, 150 tasks, 1 repetition, every asset forced ACTIVE) | task-level paired outcomes of baseline A and every asset on **fresh** data | experimental, not promotion evidence; 1 draw per task |
| `docs/evaluations/data/gated-production-gemini-2.5-flash.json` | task-level rows for the two promoted assets | same |
| code: `evaluation/policy.py`, `engine.py`, `governance/registry.py`, `economics.py` | exact gate logic and cost accounting | – |

**Data gap (important).** The task-level validation results (`EvaluationReport.task_results`) lived in the M5.8 scratch database, which was torn down when the milestone closed. They cannot be recovered, so validation-time regressions cannot be re-clustered by task or by variant. Phase 3 therefore works from the aggregates above (which are sufficient to re-run the existing policy exactly) and from the task-level P2 rows. The Phase 5 run persists `task_results` and the raw rows into `docs/evaluations/data/` so this gap does not repeat.

## 1. How the current policy decides (exact)

`default-low` (hash `7b6f0944819b…`) is a conjunction of gates (`decide_verdict`): source-replay success ≥ 80% (point estimate; "necessary, not sufficient"), held-out ≥ 75%, transfer ≥ 75%, Wilson 95% lower bound of held-out+transfer ≥ 50%, false applicability ≤ 0, **regressions ≤ 1**, optional token ratio (None at `low`). Sample-size shortfalls give INCONCLUSIVE, never PASS.

`regressions` (`engine.evaluate`) = the number of **(task, seed) pairs** over SOURCE_REPLAY + HELD_OUT + TRANSFER in which the baseline's draw verified and the treatment's draw did not. Consequences:

1. It is a **count over runs**, not a rate and not over tasks: the same flaky task counts once per repetition, and doubling the evidence roughly doubles the expected count.
2. The baseline is **one stochastic draw** per (task, seed). A "baseline success" on a task the baseline solves ~50% of the time is partly luck, so even an *identical* agent regresses on a sizeable fraction of such pairs.
3. Improvements are ignored: the gate looks only at one cell of the paired 2x2 table.
4. Medium/high use `max_regressions = 0`: with any realistic evidence volume these cannot be met by a stochastic model, they are effectively "never promote".

## 2. Why each M5.8 asset was rejected (P1 validation) and what P2 says about it

Regression rate = regressions / baseline-solved pairs. UCB = exact Clopper–Pearson one-sided 95% upper bound (pair-level, i.e. *optimistic*: repeats are correlated). P2 columns are task-level on fresh data, treating asset-forced-ACTIVE vs the baseline draw of the same task.

| Asset | Gates failed under `default-low` | P1 regressions (rate, UCB) | P1 held-out / transfer | P2 regression (rate, UCB) | P2 success A→asset (family) | Classification |
|---|---|---|---|---|---|---|
| `mem_episodic_pool@1` | source 25%, held-out 28%, transfer 25%, lower bound 18%, regressions | 20/27 (74%, 87%) | 28% / 25% | 12/17 (71%, 88%) | 27% vs A 28% | **Incorrect memory (genuine, severe).** Correct rejection. |
| `mem_consolidated_pool@1` | regressions only | 3/27 (11%, 26%) | 94% / 92% | 1/17 (6%, 25%) | 92% vs 28% | **Sample-size / count-rule effect**; not shown harmful |
| `mem_consolidated_timeout@1` | regressions only | 2/49 (4%, 12%) | 100% / 89% | 0/28 (0%, 10%) | 95% vs 47% | Count-rule effect |
| `mem_episodic_timeout@1` | regressions only | 3/49 (6%, 15%) | 86% / 78% | 1/28 (4%, 16%) | 76% vs 47% | Count-rule effect; weaker representation (lower bound 72%) |
| `mem_procedural_timeout@1` | regressions only | 2/49 (4%, 12%) | 100% / 100% | 1/28 (4%, 16%) | 97% vs 47% | Count-rule effect |
| `timeout.e2fb15fa79@1` (capability) | source replay 75% < 80% **and** regressions | 5/45 (11%, 22%) | 75% / 94% | 0/28 (0%, 10%) | 98% vs 47% | **Insufficient evidence / point-threshold on 8 source tasks** (Wilson 51–90%) plus count-rule effect |
| `mem_procedural_pool@1`, `pool.f12f4fa920@1` | none (PASS) | 0/27, 0/16 | 100%/97%, 100%/100% | 1/17, 0/17 | 92–98% vs 28% | PASS (both) |

Category totals over the six rejections: **incorrect/genuinely harmful 1**, genuine task-level regressions that are material (rate high enough to matter) **1 (the same asset)**, false applicability **0** (all eight assets: 0%), insufficient evidence **1** (source-replay point threshold on n = 8 tasks), regression-count rule alone **5** (4 memory + 1 capability, the latter jointly with the source-replay gate), excessively conservative statistical thresholds: the regression-count and source-replay *point* thresholds (the Wilson-bounded gates did not bind on any asset except episodic/pool), sample-size effects: dominant — see §3.

Two caveats that keep this from being a clean "the gate is wrong" story:

* P2 regressions are not zero for the assets the gate rejected (1/17 and 1/28 are nonzero), and an upper bound of 25% on 1/17 does not exclude a materially risky asset. What the data support is "no evidence of material harm and strong evidence of benefit", not "proven safe".
* P2 is one baseline draw per task, so P2 regression rates share the same noisy-baseline property (§1.2).

## 3. Findings

**F1 — Policy design: the false-rejection probability of an absolute count rises with evidence volume.** For a true per-pair regression probability p and n baseline-solved pairs, P(reject) = P(Binomial(n, p) ≥ 2):

| n pairs | p=1% | 2% | 3% | 5% | 10% |
|---|---|---|---|---|---|
| 16 | 0.01 | 0.04 | 0.08 | 0.19 | 0.49 |
| 27 | 0.03 | 0.10 | 0.19 | 0.39 | 0.77 |
| 49 | 0.09 | 0.26 | 0.43 | 0.71 | 0.96 |
| 100 | 0.26 | 0.60 | 0.81 | 0.96 | 1.00 |
| 200 | 0.60 | 0.91 | 0.98 | 1.00 | 1.00 |

Evidence-matched example: M5.8 validation had 27–49 pairs, so an asset whose true regression rate is a modest 3–5% is rejected 19–71% of the time, **and collecting more validation data makes it worse**. A sound gate must be monotone in evidence: more data should sharpen the decision, not flip it. This is the core design defect; it is a property of the rule, independent of the model.

**F2 — Evaluation methodology: regressions are measured against a single noisy baseline draw, over correlated pairs.** (§1.2, §1.1). With the baseline solving ~50% of timeout tasks, baseline "successes" are partly luck. Task-level clustering means the effective sample is closer to the number of distinct tasks (8/18/18 per family) than to the number of pairs. Any bound computed on pairs is optimistic; the candidate policy must compute bounds at the task level.

**F3 — Policy design: source replay is a point-estimate gate on 8 tasks.** 12/16 runs (75%) on 8 tasks has a Wilson interval of 51–90%; the 80% threshold rejected the timeout capability on a difference of one run. The same capability met the held-out (75%) and transfer (94%) gates. A gate whose outcome flips on one run is an artifact of n.

**F4 — Policy design: net benefit is invisible to the gate.** Improvements (baseline failed, asset succeeded) are not counted. In P2 the rejected assets had 27–39 improvements against 0–1 regressions. The gate cannot distinguish "removes 1 of 28 successes while adding 30" from "removes 1 and adds 0".

**F5 — What must not be loosened.** The existing gates correctly rejected `mem_episodic_pool@1` on every correctness gate at once. False applicability is already zero tolerance and was never triggered. The candidate policy keeps every success/lower-bound/false-applicability gate and only changes *how regression and small-sample point thresholds are evaluated*; it must still reject that asset by a wide margin.

**F6 — No severity, no critical-violation channel.** The engine knows only verified/not-verified. The benchmark has one permission-style signal (`TOOL_NOT_ALLOWED` termination); there is no protected-resource concept. The candidate policy adds zero tolerance for that signal and a structure for severity classes, and states that protected-resource violations cannot be tested on this simulated benchmark.

## 4. Validation-cost audit (problem 2)

M5.8's "validation = 9,437,873 tokens, ~97% of learning cost" is the sum of per-asset `evaluation_overhead_tokens + capability_exec_tokens`. Reconstructing the spend from the per-asset reports (tokens/success × verified, per arm):

| Component | Tokens | Share of actual spend | Note |
|---|---|---|---|
| Treatment arm (6 memory assets + 2 capabilities) | 4.29M | 58% | necessary: this *is* the evidence |
| Baseline A, ee-gov session (pool 464,583 + timeout 587,339), computed **once** via `SharedArm` | 1.05M | 14% | necessary once |
| Baseline A, two separate ee-eval sessions (pool 451,593 + timeout 558,866) | 1.01M | 14% | **duplicated**: same dataset, model and verifier as the ee-gov baseline, no cross-session reuse |
| `B_MEMORY` control arm in the capability evaluations (422,736 + 557,480) | 0.98M | 13% | **not used by any gate**: `decide_verdict` never reads it; it only feeds the lifecycle "overhead" figure |
| **Actual spend** | **≈7.33M** | 100% | |
| Charged in the M5.8 ledger | 9.44M | | the shared baseline (1.05M) was charged to **each** asset that used it: 3x per family (+2.11M) |

Findings:

* **Accounting overstated by ~22%** (9.44M vs 7.33M) because a baseline computed once was charged to every asset. M5.8 disclosed this as conservative but also used it as the headline ("97%"). It must be reported as *charged* vs *actual* separately.
* **~27% of actual spend was avoidable without touching the evidence standard** (duplicate baseline 14% + unused `B_MEMORY` 13%).
* **97% is mostly a denominator effect.** Discovery (257k tokens, 45 trajectories) and deterministic memory building/compilation (0 tokens) are cheap; validating 8 candidates on ~1,000 runs each is, by design, ~20x the run volume of discovery. Validation share is not a defect by itself; the question is cost per *promoted asset* and per decision.
* **No early termination**: `mem_episodic_pool@1` spent 544k treatment tokens + the shared baseline to reach 28% held-out success; a 10-task screening stage would have shown it (rate 25–28% vs 75% required).
* No input/output token split is recorded (only totals), so the true provider cost structure cannot be audited; cache hit rate, validation run counts and per-candidate cost are not reported.

## 5. Attribution

| Problem | Primary cause | Secondary |
|---|---|---|
| Beneficial assets rejected | **Policy design** (F1, F3, F4: count rule is not monotone in evidence, ignores improvements, point threshold at n = 8) | **Evaluation methodology** (F2: single noisy baseline draw, pair-level counting of correlated runs) |
| Validation dominates cost | **Execution inefficiency** (duplicate baseline, unused control arm, no early stopping: ~27% avoidable + early-stop potential) | accounting (22% overcharge); evidence volume is otherwise inherent |

## 6. What follows (Phase 1 design, next document)

Replace the *regression-count* and *source-replay point* tests, not the success/lower-bound/applicability tests, with task-level risk-based tests; add net-benefit and critical-violation gates; ship it as a separately versioned `default-low-v2` (opt-in) leaving `default-low` bit-for-bit unchanged; then make validation cheaper without changing what counts as evidence (baseline cache, drop the unused control arm from capability validation or make it optional, staged evaluation with predefined alpha spending). Design: [promotion-policy-v2-design.md](./promotion-policy-v2-design.md).
