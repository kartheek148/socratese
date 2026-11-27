# M6.1 — Evidence-driven controller: report

Status: **SUPPORTED under the pre-registered decision rule, with a fragile margin and one clear negative result (drift).** Nothing was promoted; the rule baseline `rule-capability/1` stays the default controller. Protocol, parameters and acceptance criteria were committed before any held-out outcome was inspected ([design](../experiments/evidence-driven-controller-design.md)); every number below is in [evidence-driven-controller-tables.md](./evidence-driven-controller-tables.md) with its reproduction command; ADR [0016](../decisions/0016-evidence-driven-controller.md).

## What was asked and what was built

M6 found that a constant-ε bandit pays an exploration premium in every regime (about −3 points success and +8–10% tokens per verified success when the rule is right) and could not prove non-inferiority when the rule is wrong. M6.1 replaces it with:

1. **Paired validation priors.** The estimator snapshot can carry 2×2 tables (default vs alternative on the *same* held-out tasks, final outcome including the planner fallback: arms A and E of the M5.10 final-outcome design) with full provenance (source files, dataset hashes, model, runtime and verifier versions, pair counts, UNKNOWN/tied drops, task hash, validation tokens). Discovery data is refused; repetitions collapse by strict majority; a source recorded under another runtime or verifier is refused unless explicitly overridden and recorded.
2. **Paired non-inferiority bound** (Newcombe 1998, method 10; exact one-sided coverage 0.947–0.975 at nominal 0.95 by enumeration). With a cell of ≥30 paired tasks the controller deviates from the rule default only if the lower bound of `p_alt − p_default` is ≥ −δ (δ = 0.10 for low risk) and the alternative saves ≥10% tokens per verified success; it never explores that cell (live unpaired data cannot tighten a paired bound).
3. **Evidence-driven exploration** (kind `EVIDENCE_DRIVEN`, only where no paired cell exists): explore an alternative only while its bound is unresolved, a saving is still plausible, and its sample target (the smallest n at which the independent bound could clear −δ, capped at 400) is not reached; otherwise decide deterministically. Same allow-list, gates, logged propensities and replay as the bandit.
4. **A drift guard**: live outcomes that contradict the prior (Wilson bounds with ≥30 live outcomes) invalidate it and return the rule default.

Constraints preserved: `default-low`, `default-low-v2`, `serving-low-v1`, `COST_AWARE`, `BANDIT` and `rule-capability/1` are untouched; the stored M6 definition and snapshot hashes are pinned by a test (`53b7c21a…`, `fe45fb19…`, `9f559935…`) and their decisions replay exactly; protected risk classes and foreign tenants never read a prior; reliability is a hard constraint and tokens never compensate a failed bound.

## Data

Fresh real-model data (gemini-2.5-flash) on the `heterogeneous` stress portfolio, generated with secret seeds that were never recorded in the repository: a compiler-shaped pool capability (the rule is right) and a timeout capability frozen to a retired key (stale; the rule is wrong).

| set | family | tasks | role |
|---|---|---|---|
| V-timeout | timeout | 238 usable of 240 (2 UNKNOWN dropped) | paired prior source |
| V-pool | pool | 60 | paired prior source |
| H2-timeout | timeout | 236 usable of 240 (4 UNKNOWN dropped) | held-out, rule wrong |
| H2-pool | pool | 60 | held-out, rule right |

One repetition per task; the held-out outcomes were first read when the frozen analysis ran.

## Pre-registered result

20 seeded orders, seed 20260601, estimator republished every 10 tasks, warm rule-only log = M6 development set D2 (different tasks); cell verdicts use the controller's contractual 10-point margin.

| regime | controller | deviates | explores | success diff vs rule [95% one-sided] | tokens/verified success ratio [bounds] | cell verdict |
|---|---|---|---|---|---|---|
| H2-pool (rule right) | M6 bandit | 4.8% | 9.8% | −3.4% [−4.3, −2.6] | 1.090 [1.066, 1.118] | **WORSE** |
| | `cost_aware+prior` | 0.0% | 0.0% | +0.0% | 1.000 | EQUAL |
| | **`evidence+prior`** | **0.0%** | **0.0%** | +0.0% | 1.000 | **EQUAL** |
| | `evidence` (cold) | 0.0% | 0.0% | +0.0% | 1.000 | EQUAL |
| H2-timeout (rule wrong) | M6 bandit | 4.7% | 10.5% | −0.3% [−0.6, +0.0] | 0.992 [0.986, 0.998] | BEATS (see below) |
| | `cost_aware+prior` | 100% | 0.0% | −0.4% [−5.9, +4.7] | 0.726 [0.652, 0.808] | BEATS |
| | **`evidence+prior`** | **100%** | **0.0%** | **−0.4% [−5.9, +4.7]** | **0.726 [0.652, 0.808]** | **BEATS** |
| | `evidence` (cold) | 1.8% | 3.8% | −0.2% [−0.4, −0.0] | 1.000 [0.997, 1.004] | EQUAL |

* **AC-R (rule right) holds**: deviation 0% ≤ 1%, exploration 0% ≤ 1%, verdict not WORSE. The prior shows planning confidently worse (paired upper bound −0.66 in V-pool), so the controller keeps the rule.
* **AC-S (rule wrong) holds**: deviation 100% ≥ 80%, exploration 0% ≤ 1%, verdict BEATS: about 27% fewer tokens per verified success (planning 6,496 vs the stale capability 9,024 mean tokens per task, 2,528 saved per task) at a success difference of −0.4 points whose lower bound (−5.9) stays inside the 10-point margin.
* **Exploration cost is zero** for `evidence+prior` in both regimes. Per 100 decisions the M6 bandit lost 3.3 successes and spent +13,200 tokens in the pool regime; in the timeout regime it lost 0.3 successes for a 0.8% saving.
* **Decision rule: SUPPORTED** (AC-R and AC-S both hold for `evidence+prior`).

## Reading the result honestly

1. **The prior cleared the margin by 0.0001.** The V-timeout paired table is 88 / 20 / 32 / 98 (both / alt-only / default-only / neither): θ̂ = −5.0 points and a lower bound of **−0.0999** against −0.10. H2-timeout shows θ̂ = −0.4 points. The development data that sized the prior (θ ≈ −0.8 points, 24.6% discordance) predicted a 0.89 chance of resolving at 240 pairs; V's own discordance (21.8%) and point estimate give a bootstrap resolution probability of **0.53** at 240 pairs (table below). The pre-registered criterion was met, but this was an edge case, not a comfortable margin. A second independent prior of the same size would have been about as likely as not to leave the rule in place (the safe outcome).
2. **Validation size is the binding constraint.** Subsamples of V-timeout: 30, 60 and 120 pairs do **not** resolve the cell (0% deviation, EQUAL to the rule: safe but no saving); only the full 238 does. Bootstrap probability that an independent validation of n pairs proves planning non-inferior at 10 points (V as the population, 1,000 draws): n=30 0.17, 60 0.23, 120 0.33, 240 0.53, 360 0.66. Closing the gap needs several hundred paired tasks per cell at these rates, or a looser margin.
3. **A BEATS verdict does not imply a material effect.** The M6 bandit also scores BEATS in the timeout regime (cost ratio 0.992, 0.8% saving): the verdict rule has no minimum effect size. The deviation ≥ 80% criterion (not the verdict alone) is what makes AC-S meaningful, and it is why the bandit does not satisfy it.
4. **The whole-stream M6 verdict (5-point margin) stays INCONCLUSIVE** for the prior-backed controllers (lower bound −5.9 < −5.0), as pre-declared: it would need roughly 670 held-out tasks.
5. **Cold `evidence` does not solve the stale case in 236 tasks** (EQUAL, 1.8% deviation): it needs about 130 independent samples per alternative. What it does show is the stop rule: it explored 3.8% of timeout decisions and 0% of pool decisions against the bandit's 10.5% and 9.8%, and it does not pay the bandit's rule-right premium.
6. **M5.10 sets C and D** (rule right; seen data, labelled; each the other's prior): `evidence+prior`, `cost_aware+prior` and cold `evidence` equal the rule baseline (0% deviation, 0% exploration; cold `evidence` explores 0.6% on set D timeout); the bandit is WORSE (−2 to −4 points, +7–15% tokens). Consistent with AC-R but not independent confirmation.

### Negative result: the drift guard is slow

Synthetic, labelled mechanism check on the H2-timeout stream: from the midpoint, each planning success the controller observes survives with probability 0.2 (the rule baseline's recorded outcomes are untouched).

| `evidence+prior` | deviates | success | success diff vs rule | verdict |
|---|---|---|---|---|
| guard off | 100% | 28.1% | −19.4 [−24.1, −14.8] | WORSE |
| guard on (frozen defaults) | 93.5% | 30.7% | −16.8 [−21.2, −12.4] | WORSE |

The guard recovers only a little: it compares cumulative live success with the prior, so pre-drift successes dilute the post-drift collapse (about 90 post-drift trials are needed before the Wilson bound crosses the threshold). A prior-backed policy that trusts stale evidence can therefore lose a lot of success before the guard trips. Not tuned (the parameters are frozen and the analysis was pre-declared); a windowed or sequential change detector is the obvious follow-up, and shadow/canary gating plus rollback remain the real protection. Do not describe the controller as drift-robust.

## Cost accounting

| item | tokens (model, actual) |
|---|---|
| V-timeout validation (both arms, 238 usable pairs) | 3,636,927 |
| V-pool validation (60 pairs) | 434,897 |
| **Prior validation total** | **4,071,824** |
| Saving per timeout task on H2 (planning 6,496 vs stale capability 9,024) | 2,528 |
| Break-even horizon, V-timeout cost / saving | ≈ 1,440 timeout tasks |
| Break-even horizon, total prior cost / saving (if all traffic were timeout tasks) | ≈ 1,610 tasks |
| Controller learning (tabular training, replay) | 0 model tokens |

As built, the prior is extra validation: the promotion evaluator's capability arm is capability-alone (M5.10 layer A) and cannot seed a prior, so the paired arms were run by `ee-ctl paired-run`. If promotion validation recorded the final-outcome arm, the incremental cost would drop to near zero (deferred, M6.2). Pool validation (0.43M tokens) buys no saving; it buys confirmation that the rule is right. Revalidation after a capability or model change is not free and is not counted (not zero); savings are replay measurements on tasks drawn from the same generators as the validation, and the validation-to-production distribution shift is not modelled.

## Limitations and what is not claimed

* One benchmark family pair (pool, timeout) on one model; 3–4 task templates per family; one repetition per task; H2 mirrors V's generators, the most favourable condition for a prior.
* Controller verdicts are replay measurements on recorded tables (one draw per task and action), not live traffic. Shadow/canary were not run for M6.1 policies; nothing was promoted.
* One-sided 95% bounds per cell with no multiplicity correction; SUPPORTED requires both pre-registered criteria, so the conjunction is not inflated, but the two regimes are not a family of tests. The Newcombe paired bound is slightly liberal at isolated points (coverage down to 0.947).
* Priors are only as large as validation; below about 240 paired tasks per cell they usually leave the rule in place.
* The drift guard is coarse (see above); distribution shift between validation and serving is unmodelled.
* Not done: wiring the promotion evaluator to record the final-outcome arm (M6.2), variant-level features, an automated stale-capability loop (a controller verdict that retires or recompiles a capability), larger benchmarks.

## Defects and corrections during this milestone

* A simulation test of the drift guard compared against a policy that was invalid by construction (priors without evidence parameters) and then asserted on a sign error; both were test defects, fixed. They did not affect any reported result.
* Mutation checks on the new rules (22 single-line mutants of the bound, the saving threshold, the drift guard, the exploration stop/gates and the protected/foreign-tenant prior reads): 16 were killed by the fast tests; targeted tests were then added for cost-hopeless alternatives, thin default evidence and foreign-tenant prior reads, killing 2 more. Four survive even the full test file (skipping a resolved cell, the sample-target stop, the controller-kind check, the default-evidence check): each overlaps another guard (the sample target, the absent uniform draw), so no constructed case distinguishes them. They were not proven equivalent and are kept as defence in depth.

## Reproduction

```
uv run ee-ctl simulate --heldout "H2-timeout (stale regime)" docs/evaluations/data/m61-ht_*.json \
  --heldout "H2-pool (rule-right regime)" docs/evaluations/data/m61-hp_0.json \
  --prior docs/evaluations/data/m61-vt_*.json docs/evaluations/data/m61-vp_0.json \
  --warm docs/evaluations/data/m6-s3-d2-development.json --families pool timeout --seed 20260601 \
  --kinds bandit cost_aware+prior evidence+prior evidence --explore-families pool timeout \
  --orders 20 --refresh-every 10 --out report.md --json-out report.json
```
Re-running this command after the later code changes reproduced the report byte-for-byte. All other commands are in the tables file. The data were generated with `ee-ctl paired-run --portfolio heterogeneous --llm vertex` over private datasets whose seeds are not recorded.
