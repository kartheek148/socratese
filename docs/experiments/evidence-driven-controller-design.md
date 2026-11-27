# M6.1 design — paired-evidence priors and evidence-driven exploration

Written **before any M6.1 implementation or fresh data**. It extends [adaptive-controller-design.md](./adaptive-controller-design.md) (M6). §1 states why, §2–§5 the design, §6 the frozen parameters, §7 the frozen protocol and acceptance rules, §8 files and risks. Parameters and acceptance rules in §6–§7 are committed before the fresh private datasets exist and are not changed afterwards; deviations are reported in an "Outcome" section.

## 0. Scope (M6.1 only)

In scope: (a) seed the controller's estimator from **paired held-out validation outcomes of the production path** (planning arm A vs the capability-with-planner-fallback arm E, the M5.10 final-outcome arm) so that a stale default can be recognised with no production exploration; (b) replace constant-ε exploration with **evidence-driven exploration** that runs only while the non-inferiority question is unresolved and stops; (c) a prior-consistency (drift) guard so a stale prior cannot keep a policy off the rule baseline; (d) a pre-registered confirmation on fresh seeds.
Out of scope: variant-level features, stale-capability deprecation loop, harder benchmark, new models, M7. The M6 policies (`COST_AWARE`, `BANDIT`), `rule-capability/1`, and `default-low` / `default-low-v2` / `serving-low-v1` are unchanged; every stored M6 definition keeps its hash and its decisions still replay exactly.

## 1. Why (evidence from M6, not assumptions)

M6 showed that at the frozen parameters the controller never beat the rule baseline: in the stale-capability regime (+54% tokens per verified success at equal success) it needed ≈130 independent samples of the alternative to prove non-inferiority at a 10-point margin and got ≈3 per 120-task stream; the bandit's ε-exploration was a pure premium in the rule-right regime (−3 points success, +8–10% tokens per verified success). Two things in that design were avoidable:
1. **The evidence needed already exists as paired data.** Validation runs the baseline and the treatment on the *same* tasks. A paired difference has far less variance than a difference of independent samples because tasks are the dominant noise source. In the development data (D2 + H1, 118 stale-timeout tasks) 24.6% of tasks are discordant between planning and the production path; the independent Newcombe bound needs ≈133 per arm, the paired one needs ≈180–240 *pairs in total* and, crucially, **no production exploration at all**.
2. **Exploration was not conditional on being needed.** Constant ε keeps exploring a cell whose question is already answered (alternative confidently worse) or cannot pay back.

Layer note (M5.10): the promotion evaluator's `C_CAPABILITY` arm runs the capability **alone** (Layer A); using it as a prior for a decision whose unit is the final production outcome would re-introduce the gate/production mismatch. Priors therefore come only from paired `A` vs `E` rows (`ee-exp` / `ee-ctl paired-run`), whose `E` outcome includes the planner fallback. Wiring the promotion evaluator to also record the production-path arm is **deferred** (it would be M6.2).

## 2. Evidence model

**Paired prior.** For each context `(family, risk)` and rule default `d`, and each alternative `k` that has outcomes: a `PairedCell` with the 2×2 task table `(both succeed, default only, alternative only, neither)` and the tokens of each action over the same tasks. Source rules (hard checks, recorded in `PairedSource` provenance): arms `A` and `E` only; splits `VALIDATION` / `HIDDEN_TRANSFER` only (never discovery/source tasks); UNKNOWN pairs dropped and counted; repetitions collapsed per task by strict majority (ties dropped and counted, M5.10 rule); verifier and runtime versions must equal the current ones unless explicitly overridden (recorded); dataset content hashes, model, task-id hash and the validation token cost are stored. A paired cell with fewer than `n_min_pairs` tasks is **not used** (excluded and reported).
**Tenant/version binding:** action keys embed `id@version`; a prior for one capability version never applies to another; the snapshot is tenant-scoped.

**Two evidence tiers, chosen by data availability only (never by outcome)** for each alternative `k`:
* **Tier P** — a paired cell with `n ≥ n_min_pairs` exists: the decision uses the paired Newcombe (method 10) one-sided 95% bounds of `θ = p_k − p_d` (uses the within-task correlation; exact coverage enumerated in tests). Live unpaired data cannot tighten a paired bound, so **tier P never explores**.
* **Tier L** — no usable paired cell: exactly the M6 rule (live cells, `n_min` each, independent Newcombe bound).

**Tier-P states** for alternative `k`: ADMISSIBLE (`LB(θ) ≥ −δ_risk`), INADMISSIBLE (`UB(θ) < −δ_risk`), UNRESOLVED (otherwise; the decision then records `req_pairs`, the number of paired tasks needed at the observed discordance, i.e. an evidence requirement for the validation plane, as in M5.10). Only ADMISSIBLE alternatives can replace the rule default, and only if the saving condition holds.

**Cost objective** (unchanged unit): expected tokens per verified success `mean_tokens / p̂` with `p̂` the Beta(1,1) posterior mean, from the same paired sample for the pair `(d, k)`; replace the default only if the best admissible alternative saves ≥ `min_cost_saving` (10%). Reliability constraints stay outside the cost objective; cost never compensates a failed bound.

**Drift guard (prior consistency).** Once live outcomes exist for a prior-backed action: the alternative is invalidated if its live success Wilson one-sided 95% **upper** bound is below `prior_rate_k − δ` (it is doing much worse than validation said), or the default's live **lower** bound is above `prior_rate_d + δ` (the default has become much better); both need `live n ≥ drift_min_n`. Invalid ⇒ the alternative is treated as INADMISSIBLE, so the policy returns to the rule default. This is deliberately coarse (it catches collapses, not small drifts) and is reported as a limitation.

**Protected classes and foreign tenants** are unchanged: rule decision, probability 1, no estimate read — **priors are never read for protected risk classes** (tested).

## 3. Evidence-driven exploration (kind `EVIDENCE_DRIVEN`)

Applies only to alternatives in tier L, on the same allow-list (`exploration_families` × `exploration_risk_levels`), with the same data gate as the M6 bandit (calibration ok, ≥ `min_total_verified` live verified decisions, default cell `n ≥ n_min`). Alternative `k` is **explored only while all hold**:
1. **Unresolved:** `LB(θ) < −δ ≤ UB(θ)` (a cell with no data is unresolved).
2. **Plausible saving:** optimistic tokens per verified success `cost_hat_k / min(1, UB_p_k)` (one-sided 95% Wilson upper bound of `p_k`) `≤ (1 − min_cost_saving) × cps_default`.
3. **Below its sample target:** live `n_k < target_k`, where `target_k` is the smallest `n_k` for which the independent lower bound would reach `−δ` if the current point estimates held (cap `explore_target_cap`); unreachable ⇒ hopeless ⇒ stop.
When no alternative qualifies the decision is deterministic (probability 1). Otherwise the exploration pool is `{greedy} ∪ {qualifying alternatives}` with the M6 ε-allocation (`ε/m` each, greedy gets the rest) and the same exact logged propensities and hash-derived draw, so decisions replay exactly. `COST_AWARE` + priors never explores. Priors and exploration are independent: a `COST_AWARE` policy may carry priors.

## 4. Policy versions and persistence

`ControllerParams.evidence: EvidenceParams | None` (None ⇒ M6 behaviour, byte-identical). `EstimatorSnapshot.paired` / `.prior_sources` (empty ⇒ omitted from the serialised form). Both are dropped from serialisation when empty/None, so **every M6 definition hash and snapshot hash is unchanged** (pinned by tests computed before the change: COST_AWARE `53b7c21a…`, BANDIT `fe45fb19…`, snapshot `9f559935…`). `DecisionFeatures` gains defaulted fields only (old stored decisions still validate and replay). Migration 0006 only widens the `kind` CHECK to include `EVIDENCE_DRIVEN`. The offline gate gains: a prior-backed policy must carry complete provenance for every paired cell (no provenance ⇒ FAIL) and reports prior size and resolution in its metrics.

## 5. Statistical notes
* Paired bound: Newcombe (1998) method 10 (Wilson marginals combined with a continuity-shrunk φ). Exact one-sided coverage by enumerating all tables is 0.947–0.975 at nominal 0.95 for n = 40 and 80 across five parameter points (computed while writing this design; becomes a test). One-sided slightly below nominal at isolated points is the known behaviour of the method; the 10-point margin and the drift guard absorb it.
* No multiplicity correction across cells (each cell is its own decision; a wrong deviation is bounded by the per-cell margin and monitored), reported as a limitation.
* A prior and a production stream differ in distribution (validation task mix vs production mix). The guard covers collapses only; the report says so.

## 6. Frozen parameters
`n_min_pairs = 30`, `drift_min_n = 30`, `explore_target_cap = 400`; all M6 defaults otherwise (`n_min 30`, `min_total_verified 100`, `ε 0.10`, `δ_low 0.10`, `confidence 0.95`, `min_cost_saving 10%`, calibration gate ECE ≤ 0.10 over ≥100). `required_success` must be empty when the evidence module is active (explicit validation error, not silently ignored).

## 7. Frozen protocol and acceptance (S5)

**Data (fresh private seeds, generated after this commit; seeds not recorded; real `gemini-2.5-flash`; one repetition; arms A and E; the hand-authored stress portfolio `heterogeneous`: compiler-shaped pool capability = rule is right; timeout capability frozen to a retired key = stale, rule is wrong):**

| set | purpose | content | tasks |
|---|---|---|---|
| V-timeout | paired prior (validation) | dataset v3, scale 20, family timeout, both splits | 240 |
| V-pool | paired prior | scale 5, family pool, both splits | 60 |
| H2-timeout | held-out, stale regime | fresh seed, scale 20, timeout | 240 |
| H2-pool | held-out, rule-right regime | fresh seed, scale 5, pool | 60 |

Slices (`--offset/--max-tasks`) may be used to run in parallel and survive failures; their union is the set. A warm rule-only live log (for the calibration/data gate; the same for every controller) is the M6 development set D2 (different tasks).
**Sizes are justified by the development-data power analysis** (24.6% discordance, θ ≈ −0.008): 240 paired tasks give P(prior proves planning non-inferior at 10 points) = 0.89 and the same power for the held-out per-family test; 60 tasks give 0.43 (reported as the sample-size curve, §below).

**Controllers on H2 (20 seeded orders, seed 20260601, estimator republished every 10 tasks, full-information prequential replay as in M6):** rule baseline; M6 `bandit` (frozen M6 defaults); `cost_aware+prior` (prior V); `evidence+prior` (prior V); `evidence` cold (no prior; exploration allow-listed for pool and timeout).

**Verdict rule.** Per family, per-task cluster bootstrap (10,000 resamples), one-sided 95%: *cell-level* verdict = the M6 amended rule (A2) with the non-inferiority margin set to the controller's contractual margin `δ_low = 0.10` (the controller only ever claims 10-point non-inferiority for a cell): BEATS = success-difference lower bound ≥ −0.10 and tokens-per-verified-success ratio upper bound < 1; EQUAL / WORSE / INCONCLUSIVE as in A2. The whole-stream M6 verdict at margin 0.05 (A2) is reported as **secondary** (it is under-powered by construction: at 5 points it would need ≈ 670 held-out tasks).

**Acceptance (pre-registered; evaluated for `evidence+prior` and `cost_aware+prior`):**
* **AC-R (rule is right; H2-pool):** deviation rate ≤ 1%, exploration rate ≤ 1%, cell verdict not WORSE. Secondary (seen data, labelled): M5.10 sets C and D, each with the other as prior.
* **AC-S (rule is wrong; H2-timeout):** with prior V-timeout (240 pairs): deviation rate ≥ 80% of timeout decisions, exploration rate ≤ 1%, cell verdict **BEATS**.
* **Exploration cost near zero:** exploration steps ≤ 1% of decisions in both regimes; exploration cost per 100 decisions (successes lost, tokens vs default) reported next to the M6 bandit's on the same stream.
**Decision:** SUPPORTED iff AC-R and AC-S hold for `evidence+prior`. AC-R holds but the prior is UNRESOLVED (no deviation) ⇒ INCONCLUSIVE (sample size), with `req_pairs` reported. AC-R fails ⇒ NOT SUPPORTED. Reported either way; no parameter is changed after seeing H2.

**Pre-declared secondary analyses (descriptive):** (i) sample-size curve: priors of 30 / 60 / 120 / 240 timeout pairs (seeded subsamples, seed 20260602): deviation and verdict; (ii) cold `evidence` vs the M6 `bandit`: exploratory steps and verdicts on the same stream; (iii) drift: replay of the stale-regime stream where planning's outcomes are degraded from the midpoint (synthetic, labelled) to show the guard returns the policy to the rule default; (iv) cost accounting: validation tokens of V (A + E arms) as the learning cost, per-decision saving, break-even horizon with its assumptions.
**Reading rules:** BEATS/EQUAL/WORSE/INCONCLUSIVE per set; nothing pooled across regimes; negative and inconclusive outcomes reported with the sample size that explains them; the prior's validation cost is part of the lifecycle cost.

## 8. Files and risks
New: `controller/paired.py` (cells, sources, prior builder), `domain` additions, `ee-ctl train --paired-from`, `simulate --prior`, migration `0006`, `tests/unit/test_controller_evidence.py`, integration tests, `docs/decisions/0016-*.md`, report. Modified: `domain/controller.py`, `domain/stats.py` (paired bound), `controller/{estimator,features,policy,replay,gates,simulate,datasets}.py`, `controller_cli.py`, `adapters/db/tables.py`, docs.
Risks: prior staleness / validation-vs-production shift (guard is coarse); the prior is only as large as validation (a 60-task validation resolves the stale case < 45% of the time); paired data from the promotion evaluator is Layer A and unusable without an E arm there; validation cost can exceed the exploration premium it saves on short horizons (accounted for in §7 iv); multiple cells share no multiplicity correction.
