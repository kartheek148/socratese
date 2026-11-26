# M5.9 design — risk-based promotion policy (`*-v2`) and cost-efficient validation

Follows [promotion-policy-audit.md](./promotion-policy-audit.md). **Parameters in §3 and the stopping schedule in §6 are frozen by this document, before any retrospective simulation or fresh run**. They must not be changed after seeing Phase 3 or Phase 5 results; a change means a new policy version and a new confirmation. Disclosure: the author had seen the M5.8 aggregates (including regression counts) when choosing the values; the values were derived from the risk argument in §2, not fitted, but the retrospective simulation is development evidence only, and the confirmation on a fresh seed (Phase 5) is the test.

## 1. Non-negotiables
* `default-low`, `default-medium`, `default-high`, `decide_verdict` and their hashes are untouched (a test pins `default-low`'s hash `7b6f0944819b…`).
* The new policies are separate objects with their own names/versions (`default-low-v2`, …), selected explicitly (`--policy-set risk-v2`). Nothing is promoted by default under them.
* Only PASS under the *governing* policy changes state. Counterfactual verdicts (what another policy would have said about the same evidence) are recorded, never acted on.
* No promotion from retrospective simulation, from an early stage, or from token savings.

## 2. What the policy protects against, and the statistics used
Unit of analysis: the **distinct task**. Repetitions of a task are averaged inside the task (they are correlated draws of a stochastic model). UNKNOWN outcomes are missing data (the pair is dropped and counted), not failures.

**G1 Regression risk (replaces the regression *count*).** Among tasks the baseline solved, the probability that the candidate fails: `P(candidate fails | baseline succeeds) ≤ δ`. Estimator: for each task with at least one baseline-solved repetition, `r_t` = regressed repetitions / baseline-solved repetitions; `x = Σ r_t`, `n = #tasks`. Bound: exact one-sided Clopper–Pearson (beta quantile, real-valued `x`). A [0,1]-valued `r_t` has variance at most the Bernoulli variance, so this is conservative. Three-valued decision: **PASS** if `UCB(x,n) ≤ δ`; **FAIL** if `LCB(x,n) > δ` (the evidence says the rate is above tolerance); otherwise **INCONCLUSIVE** (more evidence needed). This is monotone in evidence: more data narrows the interval; it cannot turn a genuinely good asset into a rejection by sheer volume (audit F1).
**G2 Net benefit (non-inferiority).** Task-level paired difference `d_t = treatment rate − baseline rate` over HELD_OUT+TRANSFER tasks; one-sided lower confidence bound by deterministic paired bootstrap (fixed seed, recorded). PASS if `LCB ≥ −margin`; FAIL if the matching one-sided *upper* bound `< −margin` (credibly worse than baseline by more than the margin); else INCONCLUSIVE. Counts improvements as well as regressions (audit F4). **Token savings never enter G1–G5.**
**G3 Source replay as sanity check.** Point threshold on 8 tasks replaced by an evidence test: FAIL only if the one-sided CP *upper* bound of the source-replay success rate is below `min_source_replay_success_rate` (the data exclude meeting the requirement); still "necessary, never sufficient". (Audit F3.)
**G4 Held-out / transfer success and the Wilson lower bound** — unchanged from v1 (point thresholds and 95% Wilson bound; these were not the binding gates except for the genuinely bad asset).
**G5 False applicability** — unchanged, zero tolerance (a separate limit, never traded against anything).
**G6 Critical violations.** Any treatment run (any suite) that terminates with a reason in `critical_terminations` (`TOOL_NOT_ALLOWED`: an attempt to use a tool the task did not permit) fails the asset immediately. Zero tolerance for every risk class. The simulated benchmark has no protected-resource concept; this channel cannot test it (limitation, not claim).
**G7 No concealment.** A variant (library/layout) with at least `min_variant_pairs` baseline-solved pairs whose regression point rate exceeds `max_variant_regression_rate` fails the asset even if the aggregate is fine, so improvements on easy variants cannot hide a regression class.
**Severity rules** (recorded in the policy): `critical` = termination in `critical_terminations` (G6, zero tolerance); `functional` = verified-failure after the baseline verified (G1/G7). There is no `minor` class: nothing in the benchmark distinguishes it, and inventing one would be unmeasured.
**Multiple comparisons.** G1–G7 are a conjunction (intersection–union test): promotion needs every gate to hold at its own level, so no alpha splitting is needed for the promote decision. Staged looks (§6) spend alpha explicitly. Holm correction is used only when the confirmation experiment reports several comparisons at once.
**Evaluation configuration hash.** `evaluation_config_hash` = sha256 of {policy hash, seeds, dataset content hashes, model settings, runtime/verifier/environment/evaluator versions, stage plan}; stored in every report next to the policy hash.

## 3. Parameters (frozen)
| Field | `default-low-v2` | `-medium-v2` | `-high-v2` | Rationale |
|---|---|---|---|---|
| method | `task-cluster-clopper-pearson/1` | same | same | recorded |
| confidence (one-sided) | 0.95 | 0.95 | 0.975 | tighter for high risk |
| `max_regression_rate_ucb` δ | 0.20 | 0.10 | 0.05 | low: recoverable, shadow/canary-monitored; high: needs ≥59 clean tasks |
| `min_regression_tasks` (distinct baseline-solved tasks) | 8 | 15 | 30 | below this the verdict is INCONCLUSIVE |
| `noninferiority_margin` | 0.05 | 0.02 | 0.0 | |
| `max_variant_regression_rate` / `min_variant_pairs` | 0.5 / 6 | 0.3 / 6 | 0.15 / 6 | |
| `max_critical_violations` | 0 | 0 | 0 | |
| success, lower-bound, false-applicability, sample-size, token-ratio, rollout fields | as v1 | as v1 | as v1 | unchanged |
| `max_regressions` | superseded (sentinel 1,000,000) | | | field kept for schema compatibility; not read by v2 |
| bootstrap | 10,000 resamples, seed 20260101 | | | reproducible |

With zero regressions, δ=0.20 needs ≥ 14 baseline-solved tasks, δ=0.10 ≥ 29, δ=0.05 ≥ 59 (95% CP): high-risk promotion is intentionally hard.

## 4. Representation and versioning
`RiskBasedPolicy(PromotionPolicy)` adds the fields above as **required** fields (so a v1 JSON can never validate as v2 and vice versa); `EvaluationReport.policy` becomes `RiskBasedPolicy | PromotionPolicy`. `PromotionPolicy.policy_hash()` for v1 is bit-identical (new fields exist only on the subclass). `EvaluationReport` gains optional fields (defaults keep every old report loadable): `risk_evidence`, `counterfactuals`, `evaluation_config_hash`, `stage_reached`, `early_stopped`, `validation_resources`.
**Historical immutability and REJECTED assets.** Evaluation and transition tables are append-only and `REJECTED` is terminal. A new policy therefore never reopens a rejected version. Options: (a) *counterfactual* (record what v2 would say, no state change; used in Phase 3/5); (b) a re-admission is a **new version** (changed content / new builder or compiler version) evaluated from scratch under whichever policy governs. Identical content cannot be re-registered (content-hash idempotency); this is deliberate. No M5.8 evaluation record is rewritten.

## 5. Cheaper validation without weakening evidence
**A. Baseline cache** (`evaluation/baseline_cache.py`): persistent, file-backed, keyed by sha256 of {task content hash, dataset content hash, task id, repetition, model settings, prompt version, runtime/verifier/environment version, agent-config id}; any component change misses; corrupt entries are misses; atomic writes. In-session sharing (`SharedArm`) marks reuse; reports separate **executed** from **served-from-cache** runs. Independence: a reused baseline is one stochastic draw shared by all assets compared against it (their paired differences are correlated through it, each comparison remains valid; a lucky draw stays lucky). The cache is opt-in (`--baseline-cache`) and the decision-relevant evaluation always uses recorded draws, so results stay reproducible.
**B. Unused control arm.** `B_MEMORY` is not read by any gate. `ee-eval --skip-memory-control` omits it (opt-in; default behaviour unchanged).
**C. Resource accounting.** Per evaluation: runs executed vs cached, input/output tokens where the arm records them (agent arms; capability decisions report totals only, stated), tool calls, latency, cached tokens *as recorded when first computed*, per-arm totals. Savings are measured from runs that were actually avoided, never extrapolated.
**D. Staged validation** (§6), opt-in (`--staged`), predefined stopping rules, no early PASS.

## 6. Sequential procedure (frozen)
1. **Stage 1, static (0 tokens):** NEGATIVE suite (applicability refusals are decided before any step runs). Any false applicability → stop, FAIL.
2. **Stage 2, screening:** the first 8 held-out tasks (dataset order), first repetition only, treatment and baseline. Stop-FAIL if (a) *futility*: one-sided CP upper bound of the treatment success rate at α₂ = 0.01 is below `min_heldout_success_rate`, or (b) *harm*: exact one-sided sign test on baseline-only vs treatment-only successes rejects "treatment not worse" at α₂ = 0.01.
3. **Stage 3, full held-out:** the same tests on all held-out runs at α₃ = 0.02.
4. **Stage 4, transfer + source replay + regression suite and the final policy verdict** at nominal levels.
Spending: α₂ + α₃ = 0.03 total (union bound): if an asset truly meets the success requirement (or is truly not worse than baseline), the probability of stopping it early is at most 0.03. Stages never decide PASS. No other peeking is allowed; the ordinary 95% interval is never inspected to stop. Replayed on recorded outcomes (Phase 5) the rule is *validated*, not assumed: report false early stops and tokens actually not spent.

## 7. Files (interfaces)
New: `evaluation/risk.py` (CP bounds, `RiskEvidence`, `build_risk_evidence`), `evaluation/risk_policy.py` (`decide_verdict_v2`, `default_policies_v2`), `evaluation/sequential.py` (`StagePlan`, stop rules), `evaluation/baseline_cache.py`, `evaluation/policy_simulation.py` (retrospective), `evaluation/validation_cost.py` (resource and lifecycle accounting). Modified: `domain/evaluation.py` (`RiskBasedPolicy`, report fields, `TaskResult.cache_hit/prompt_tokens/completion_tokens`), `evaluation/{policy,engine,arms,memory_eval}.py`, `eval_cli.py`, `gov_cli.py` (`--policy-set`, `--counterfactual-policy-set`, `--baseline-cache`, `--staged`, `--skip-memory-control`), `adapters/db/registry_store.py` (extra metrics keys, no schema change).
## 8. Acceptance tests (named)
v1 hash pinned and v1 verdicts unchanged on every M5.8 aggregate; CP bounds vs reference values; regression-rate and cluster estimator; three-valued gates; risk-class thresholds; critical violation; variant concealment; paired bootstrap determinism; Holm; policy round-trip and versioning (v1/v2 JSON cannot cross-validate); counterfactual verdicts never change state; REJECTED stays terminal and old evaluation rows are not mutated; cache hit/miss/invalidations/corruption/concurrency; stopping rules (no early PASS, α spending, stage order); resource accounting reconciles with task rows; candidate vs ACTIVE isolation and rollback (regression of the M5.8 tests with v2 governing).

## 9. Confirmation protocol (frozen before the confirmation data exist)
**Data.** Two independent public discovery seeds (sets A and B; each yields 6 memory and 2 capability candidates, 16 assets in total), one fresh private validation seed P3 (`--scale 3`, 2 repetitions) and a different fresh private test seed P4 (`--scale 5`: 60 pool, 60 timeout, 30 queue tasks). P3 and P4 have never been used, neither for the policy design nor for the retrospective simulation, and are not used to change any parameter.
**Validation.** Every asset is evaluated once on P3 over the full grid (no early stopping, so both policies see identical complete evidence). The governing policy is the unchanged `default-low`; `default-low-v2` is recorded as a counterfactual on the same task/repetition grid. Both sets share one baseline cache (hit/miss counts are measured and reported).
**Truth labels** come only from P4 (forced-ACTIVE research run, one repetition, task-level paired against arm A on the same task) and are fixed here:
* *beneficial*: one-sided 95% paired-bootstrap lower bound of the success difference `> 0` AND point regression rate `<= 0.25`;
* *harmful*: the one-sided 95% upper bound of the success difference `< 0` OR point regression rate `> 0.5`;
* otherwise *unclear* (excluded from error counts, reported).
**Policy outcomes per asset** (PASS / FAIL / INCONCLUSIVE): *unjustified rejection* = beneficial and FAIL; *unjustified non-promotion* = beneficial and not PASS; *harmful promotion* = harmful and PASS.
**Primary comparison (task level).** For each policy, a *portfolio* is what production would serve: per P4 task, the compiled capability if promoted for the task's family, else the highest-ranked promoted memory (procedural > consolidated > episodic), else the baseline's own outcome. We compare, per family and overall, (i) portfolio success and (ii) portfolio regression rate `P(fail | baseline succeeded)` between the two policies with paired bootstrap CIs (10,000 resamples, seed 20260101), exact McNemar on tasks, Holm correction over the comparisons reported. "Reduces unjustified rejection without increasing harmful regressions" is *supported* only if (i) is not significantly lower and (ii) is not significantly higher under v2, and v2's unjustified rejections are fewer; with fewer than 5 discordant tasks or fewer than 5 assets with a decisive truth label the result is reported as **inconclusive**.
**Asset-level comparison** (exact McNemar on PASS vs not-PASS among beneficial assets) is reported but cannot be significant with this number of assets; it is shown for transparency.
**Sequential rule.** Evaluated by replaying the §6 stages on the recorded task rows (simulated, labelled as such: the full grid was executed). Reported: assets stopped, **false early stops** (stopped assets that the full policy passes or that P4 labels beneficial), and tokens that a real staged run would not have spent. No savings are counted from this replay as actual.
**Economics** use only measured tokens (executed vs cached from the task rows; discovery from the cost ledger); maintenance is an explicit assumption (0, 1 or 3 re-validations of each asset's evaluation).
