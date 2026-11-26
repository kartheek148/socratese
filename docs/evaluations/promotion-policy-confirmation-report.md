# M5.9 — Promotion-policy calibration and cost-efficient validation: results

Documents: [audit](../experiments/promotion-policy-audit.md) → [design and frozen protocol](../experiments/promotion-policy-v2-design.md) → [retrospective simulation](./promotion-policy-retrospective.md) → this report. Generated tables (every number below comes from them): [confirmation tables](./promotion-policy-confirmation-tables.md). Raw data: [`data/m59-confirmation-*`](./data/) (per-asset reports with per-task rows; the P4 comparison rows) and [per-asset validation reports](./m59-validation/). ADR: [0013](../decisions/0013-risk-based-promotion-policy-and-cheaper-validation.md).

## 0. Headline (read §8 first)

* **The two M5.8 problems have different causes.** Rejection of beneficial assets was mainly **policy design** (an absolute regression count whose false-rejection rate *grows* with evidence; a point threshold on 8 source tasks). Validation cost was mainly **execution inefficiency** (duplicate baselines, an unused control arm, no reuse), plus an accounting artefact; the evidence standard itself is expensive by construction.
* **The candidate policy `default-low-v2` did what it was designed to do on rejections and failed the frozen confirmation on promotion.** On 16 fresh assets it cut unjustified rejections from 5 to 2 (post hoc, with capabilities judged alone: 4 to 1) and promoted no harmful asset. But it promoted **fewer** assets than the existing policy (1 vs 2): the beneficial pool capability that `default-low` promoted became INCONCLUSIVE ("cannot show a regression rate ≤ 20% from 14 baseline-solved tasks"). Served through a production portfolio, that loss is large and significant on set B (pool 98% → 20%, overall 79% → 47%). The frozen decision rule therefore reads **NOT SUPPORTED**. The policy is **not adopted**; `default-low` remains the governing policy; `default-low-v2` stays an opt-in experimental candidate.
* **Validation got cheaper without lowering the evidence standard:** 625k executed tokens per evaluated asset vs ≈917k in M5.8 (-32%, not a controlled comparison), 40% of the runs that would otherwise have executed were reused from baseline sharing/cache, and the staged rule, replayed on the recorded grids, would have skipped 40% of executed tokens (simulated, population-dependent, see §5).
* **No asset was promoted by any of this work.**

## 1. What was asked and what was built
Audit (Phase 0) → frozen design (1-2) → retrospective simulation (3) → validation efficiency (4: persistent baseline cache, unused-control removal, input/output accounting, predefined sequential stopping) → fresh confirmation (5) → economics (6) → tests and docs (7). Existing `default-low` is bit-for-bit unchanged (hash `7b6f0944819b…` pinned by a test; all 8 M5.8 verdicts and reason lists are reproduced exactly from the stored aggregates). The candidate policy, cache, staging and accounting are opt-in.

## 2. Phase 0-3 recap
* Audit: of six M5.8 rejections, one was genuinely bad (`mem_episodic_pool`), five were rejected mainly by the absolute regression limit (one together with a source-replay point threshold on 8 tasks). Probability of rejecting a good asset with true regression rate 3% rises from 8% (16 pairs) to 81% (100 pairs).
* Retrospective (development evidence only): under `default-low-v2` the five count-rule rejections became PASS (3 in the optimistic clustering scenario, 2 in the conservative one) or INCONCLUSIVE; the genuinely bad asset still FAILed in both scenarios.
* Honest caution from the retrospective already pointed at the problem confirmed below: on M5.8's P2 data the then-ACTIVE `mem_procedural_pool` was INCONCLUSIVE under G1 (1 regression among 17 baseline-solved tasks).

## 3. Phase 5 — fresh confirmation (frozen protocol, design §9)
**Setup.** Two independent public discovery seeds (sets A and B) → 16 candidates (12 memory, 4 capabilities). Fresh private validation seed P3 (2 repetitions) and a different private test seed P4 (150 tasks) for truth labels; neither seed influenced design. The governing policy was the unchanged `default-low`; `default-low-v2` was recorded as a counterfactual **on the identical task/repetition grid** for every asset. Capability and memory assets are reported separately (§3.2). Two capabilities in set B (`pool.f12f4fa920`, `timeout.e2fb15fa79`) are byte-identical procedures to the M5.8 capabilities (content-hash ids): the compiler is deterministic on equivalent evidence. Their *data* are fresh, but M5.8 had already shown their aggregates.

### 3.1 Decisions and independent truth (P4, forced-ACTIVE, one repetition)
| Set | Asset | `default-low` | `default-low-v2` | P4 diff [95% one-sided bounds] | P4 regression | Truth |
|---|---|---|---|---|---|---|
| A | pool capability | FAIL | FAIL | +48% [+35%, +62%] | 3/16 | beneficial (alone: harmful) |
| A | consolidated pool / procedural pool | FAIL / FAIL | FAIL / FAIL | -17% / -13% | 15/16, 14/16 | harmful / harmful |
| A | episodic pool | FAIL | FAIL | +27% | 6/16 | unclear |
| A | timeout capability | FAIL | FAIL | +2% | 6/28 | unclear (alone: harmful) |
| A | consolidated / procedural timeout | FAIL / FAIL | FAIL / FAIL | 0% / -7% | 6/28, 7/28 | unclear |
| A | episodic timeout | FAIL | FAIL | +38% [+27%, +50%] | 2/28 | beneficial |
| B | pool capability | **PASS** | INCONCLUSIVE | +78% [+70%, +87%] | 0/12 | beneficial |
| B | procedural pool | FAIL | INCONCLUSIVE | +70% [+60%, +80%] | 0/12 | beneficial |
| B | consolidated pool | FAIL | FAIL | +43% | 4/12 | unclear |
| B | episodic pool | FAIL | FAIL | -2% | 9/12 | harmful |
| B | timeout capability | FAIL | INCONCLUSIVE | +33% [+22%, +45%] | 3/35 | beneficial |
| B | episodic timeout | FAIL | INCONCLUSIVE | +28% | 2/35 | beneficial |
| B | procedural timeout | **PASS** | **PASS** | +35% [+23%, +47%] | 2/35 | beneficial |
| B | consolidated timeout | FAIL | FAIL | -2% | 11/35 | unclear |

Full reasons are in the tables file. Set A's candidates were poor (its discovery produced weaker procedures: the capabilities succeeded on 0-3% of held-out tasks alone); set B's resemble M5.8's.

| Metric (16 assets; 7 beneficial, 3 harmful, 6 unclear) | `default-low` | `default-low-v2` |
|---|---|---|
| PASS / FAIL / INCONCLUSIVE | 2 / 14 / 0 | 1 / 11 / 4 |
| unjustified rejections (beneficial & FAIL) | 5 | **2** |
| unjustified non-promotions (beneficial & not PASS) | 5 | **6** |
| beneficial promoted | 2 | 1 |
| harmful promoted | 0 | 0 |
| (post hoc, capabilities judged alone) unjustified rejections | 4 | 1 |

The two remaining v2 rejections are `A: pool capability` (a label artefact: see 3.3) and `A: episodic timeout`, which fails the **retained** held-out success gate (64% < 75%): a beneficial but modest asset the success thresholds, which v2 deliberately kept, reject. Asset-level exact McNemar (illustrative; 7 beneficial assets): candidate-only 0, governing-only 1, p = 1.0.

### 3.2 Task-level portfolio comparison (what production would serve), capability and memory kept in separate rankings
Set A: nothing promoted by either policy: no difference (150 tasks, 0 discordant). Set B: v1 79% vs v2 47% (-31.3% [-38.7%, -24.0%]), 47 tasks lost only by v2 and 0 only by v1 (McNemar p < 0.001, Holm < 0.001); pool 98% vs 20%; timeout identical (93%) because both promoted the same procedural memory; regression rate on baseline-solved tasks 4% vs 4%. **Caveat that matters for inference:** the 47 discordant tasks all come from one asset decision (the pool capability), so the strong p-value reflects one decision, not 47 independent ones.

### 3.3 Capability labels and the gate (post-hoc finding)
The gate evaluates a capability **alone**; the production runtime falls back to planning when it fails, so arm E credits the fallback. `A: pool capability` succeeded on 3% of held-out tasks alone yet shows +48% in the runtime because the fallback finishes the job. Under the frozen label it counts as an "unjustified rejection" by both policies; judged alone it is harmful. This mismatch between *gate entity* and *production entity* is real and unresolved: either the gate should evaluate the runtime path (including fallback), or the claim "capability X is beneficial" must be restricted to the capability alone. It is a policy-evaluation question separate from regression counting.

### 3.4 Mechanical reading of the frozen rule
Fewer unjustified rejections, no harmful promotion, but a demonstrated loss in portfolio success under v2 (set B) → **NOT SUPPORTED**. A second reading is not hidden: if the INCONCLUSIVE verdicts were followed by an evidence-extension step (which the policy intends but the single-shot protocol did not run), the pool capability would plausibly clear: adding P4's 0/12 regressions to P3's evidence gives ≈1/26 baseline-solved tasks, 95% upper bound 17% ≤ 20%. That is **post hoc, uses the labelling data, and is not a confirmation**; it is a hypothesis for a new confirmation.

### 3.5 Why v2 stalled: a sample-size property of G1
G1 needs about 14 clean baseline-solved tasks for δ = 20% (≈ 22 if one task regressed). On weak-baseline families the number of baseline-solved tasks is small by construction: the pool baseline solves only 20-27% of tasks (P4), and the set-B pool evidence contained 14 baseline-solved tasks. The gate converts "not enough evidence" into INCONCLUSIVE correctly, but the information it needs scales with the *baseline's* success, which the evaluator does not control. This is a design flaw to fix in a new version (candidates, none applied: targeted accrual of baseline-solved tasks; evidence extension on INCONCLUSIVE; or leading with the paired net-benefit gate and bounding regressions on a family-level pooled history). Parameters were **not** retuned.

## 4. Phase 4 — validation efficiency (measured)
* **Baseline reuse.** Totals over the 16 evaluations: executed 9,996,399 tokens over 1,825 runs; reused 6,677,378 tokens over 1,119 runs (40% of what would otherwise have run; tokens as recorded when first computed). Under the M5.8 accounting (baseline charged to every asset) the same work would be 16,673,777 tokens. Persistent-cache hits (recorded by each process): set A gov 54, A pool 41, A timeout 12; set B gov 160, B pool 76, B timeout 72 runs; set B's only misses were its own discovery-set source-replay tasks (16 + 12 + 16), because source-replay tasks come from each set's own discovery data; held-out and transfer baselines were fully reused. Concurrent starters (set A) race and both compute, as expected.
* **Unused control arm.** `ee-eval --skip-memory-control` removed the `B_MEMORY` arm from capability validation (it feeds no gate): "other arms executed" is 0 for every asset (M5.8: 423k-557k tokens per capability).
* **Input/output tokens** are now recorded for general-planning model calls (agent arms); capability decision calls report totals only (stated). Example: set B procedural-timeout evaluation, treatment arm: 435,666 input / 228,156 output tokens over the 88 runs with the split.
* **Per-asset cost.** 625k executed tokens per evaluated asset (9.996M / 16) vs ≈917k in M5.8 (7.33M / 8). Not a controlled comparison (different candidates) but the evidence standard (suites, seeds, thresholds) is unchanged.
* **Sequential rule (simulated replay, frozen stages).** Stopped 8 of 16 assets (A: 7 of 8; B: 1 of 8), skipping 4.04M of 9.996M executed tokens (A: 67.5%; B: 10.8%). Every stopped asset was rejected by both policies in full; the one "false early stop" by the frozen label is `A: pool capability` (the §3.3 artefact; under the capability-alone label there are none). **The saving is population-dependent**: set A was mostly unfit candidates; for a candidate stream like M5.8's (1 unfit in 8) the saving is a few percent. These are simulated, not realised savings; the staged execution path is implemented and tested but was not exercised on the real model.

## 5. Phase 6 — economics (as built, with explicit assumptions)
Discovery 680,914 tokens; memory building/compilation 0 model tokens (deterministic); candidate evaluation 9,996,399 executed tokens; baseline reuse already excluded. Maintenance is an assumption (R re-validations of each *promoted* asset's own evaluation):

| Policy | promoted | R | lifecycle tokens | per promoted asset | extra verified successes on P4 | tokens per extra verified success |
|---|---|---|---|---|---|---|
| `default-low` | 2 | 0 / 1 / 3 | 10.68M / 11.61M / 13.47M | 5.34M / 5.80M / 6.73M | 68 | 157k / 171k / 198k |
| `default-low-v2` | 1 | 0 / 1 / 3 | 10.68M / 11.37M / 12.75M | 10.68M / 11.37M / 12.75M | 21 | 508k / 541k / 607k |

Finding a useful asset is expensive: 16 candidates, ~10M tokens, 1-2 promoted. Most of the spend is on assets that end up rejected, so cost per promoted asset is dominated by candidate quality, not by policy. Both policies share the same evaluation spend; the candidate policy's cost per extra success is worse here only because it promotes less.

## 6. Answers
1. *Policy design, methodology or execution?* Rejections: policy design first (count rule, point threshold), methodology second (single noisy baseline draw, correlated pairs, gate entity ≠ production entity). Cost: execution inefficiency (≈27% avoidable in M5.8; achieved −32% per asset) plus an accounting artefact (22% overcharge).
2. *Does the candidate policy reduce unjustified rejection without increasing harmful regressions?* It reduced unjustified rejection (5 → 2) and increased no harmful promotion or regression rate, **but** it blocked a beneficial promotion the existing policy allowed; the frozen rule is NOT SUPPORTED.
3. *Should `default-low-v2` replace `default-low`?* No. Keep `default-low`; keep v2 as an opt-in candidate; revise G1's evidence requirement and confirm again on a new fresh seed.
4. *Is validation efficiency improved?* Yes and measured: reuse and removal of the unused arm are real; the sequential rule's savings are simulated and population-dependent.

## 7. Negative and inconclusive results (kept visible)
* v2 PASSed 1 asset vs 2, so it is more conservative where evidence is thin; it did not rescue `A: episodic timeout` (success gates retained).
* The set A population shows the gate also rejects genuinely poor assets; one capability that looked beneficial in the runtime was harmful alone.
* The portfolio comparison rests on one decisive asset; set A carries no information about the policy difference.
* Truth labels are statistical (1 repetition) and the retrospective rows are development evidence.

## 8. Limits
Single model; simulated benchmark; 16 assets from 2 discovery seeds that share tasks and the baseline cache (a lucky or unlucky baseline draw is shared by all assets, by design); P4 truth is a one-repetition forced-ACTIVE run; sequential savings are simulated; maintenance is assumed; no OS-level or protected-resource testing of the critical-violation channel; two set-B capabilities were seen in M5.8; policy parameters were frozen before data and not changed.
