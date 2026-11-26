# Production-aligned learning evaluation (M5.10)

Real model `gemini-2.5-flash` (Vertex), simulated benchmark v3 families (`pool`, `timeout`, `queue`). Two fresh sets, C and D, each with its own public discovery seed and its own private test seed (scale 5: 60 pool, 60 timeout, 30 queue tasks = 150 tasks; one repetition per task and arm; seeds not recorded). Frozen design: [experiments/production-aligned-evaluation-design.md](../experiments/production-aligned-evaluation-design.md) (committed before any implementation or data). Complete generated tables: [production-aligned-serving-tables.md](./production-aligned-serving-tables.md), regenerable byte-identically from `data/m510-serving-set-{c,d}.json` with `ee-exp serving-report` (the evaluation decisions are in `data/m510-serving-evaluations.json`).

**Nothing was promoted. No asset changed state. `default-low` and `default-low-v2` are untouched. This is not promotion evidence**: candidate assets were read through the explicit experimental mechanism (`--experimental-states ACTIVE CANDIDATE`), because nothing is ACTIVE.

## 1. The existing architecture and its mismatch (confirmed)

The M5.9 gate path (`make_capability_arm` → `CapabilityExecutor`) judges a capability alone. The production path (`TaskOrchestrator` → matcher → policy → executor → `_fallback` → `AgentRuntime`) falls back to general planning on `ExecStatus.FALLBACK` (execution, tool, timeout/budget, verification and decision failures). `ABORTED` and `ESCALATE` do not fall back. A run that the planner finished was credited to the capability version. See design §1–§2 for the six-path audit and cause table.

## 2. What M5.10 adds (all opt-in)

* **Attribution** (`--attribute-phases`): every run records a capability phase and a fallback phase (outcome, tokens, tool calls, latency, `FallbackCause`), plus the final outcome. The capability outcome at the handover is measured by one extra read-only verifier call; with attribution off, behaviour is unchanged. UNKNOWN stays unknown (never counted as success or failure).
* **Two layers.** Layer A = intrinsic quality (capability phase only, derived as arm `E1`; it is the capability phase of the production runs, so it is paired with E2 by construction). Layer B = serving quality (arm `E2`: capability + production fallback). Memory has a single phase, so A and B coincide for it.
* **Serving policy `serving-low-v1`** (hash `59c4bd37dc79…`, frozen, pinned by a test): separate intrinsic, serving, economic and safety decisions, three-valued, and a six-class classification. It is applied only as a counterfactual over experiment rows.
* **Approval is not activation is not routing.** `ApprovedCapabilityCatalog` / `ApprovedMemoryCatalog` can only *remove* assets from what is ACTIVE (`--approved`); they cannot add one.
* **Task-level statistics** (`task_stats`): integer-only exact Clopper-Pearson bounds, strict-majority task outcome, whole-task cluster bootstrap (10,000 resamples, fixed seed), stratified (family-weighted) production estimate, token cost per verified success with a withheld bound if more than 5% of resamples are undefined.
* **Defect found in the M5.9 statistics.** `build_risk_evidence` feeds *fractional* regression mass into the exact bound; 8 of the 16 M5.9 confirmation assets had a fractional mass, so those bounds were not valid exact bounds. `default-low-v2` is frozen and unchanged (its published verdicts stay as recorded); `task_stats` raises on non-integer counts. I did not recompute the M5.9 verdicts, so the effect of the defect on them is unquantified.

## 3. Results

### 3.1 Per-arm outcomes (150 tasks per set; stratified by family)

| | Set C | Set D |
|---|---|---|
| A stateless | 29% | 34% |
| F empty-registry (noise control) | 30% | 34% |
| B episodic memory | 63% | 65% |
| C consolidated memory | 66% | 61% |
| D procedural memory | 76% | 72% |
| E1 capability only (attempted tasks only: 120) | 92% | 92% |
| **E2 capability + production fallback** | **79%** | **78%** |
| E2 tokens per verified success (A) | 4,407 (20,465) | 4,568 (18,324) |

A and F are statistically indistinguishable in both sets (F−A paired +0.7%, CI [−7.3%, +9.3%], set C), so the noise floor is small. Every learned arm beats A within the learned families (Holm-adjusted McNemar p < 0.001 for B, D, E1, E2 on pool and timeout), except consolidated memory on timeout (set C: +20%, adjusted p = 0.29, i.e. not significant after correction). The 1 (set C) and 3 (set D) UNKNOWN runs are excluded, not counted.

Per learned family, E2 versus A: pool 100% vs 23% (C), 97% vs 22% (D); timeout 92% vs 45% (C), 93% vs 56% (D).

### 3.2 Does the fallback hide capability weakness? (Layer A vs Layer B)

On the learned families the fallback adds little: E2 − E1 is +3.3% on pool and timeout in set C (2 tasks each; CI [0%, +8.3%]) and +3.3% / +1.7% in set D, with 0 tasks where the capability alone succeeded but the final run failed. So, unlike the defective M5.9 P4 set-A capability (59/60 runs fell back; 44 of 45 final successes were planner-finished), **these four capability instances are mostly intrinsically good**: fallback frequency 3–12%, and the fallback accounts for 10–24% of capability-plus-fallback tokens.

Fallback recovery (capability failed, planner then tried), both sets pooled over the 4 capability instances: **7 of 18 recovered (39%)** versus 5 of 18 (28%) for the stateless baseline on the same task instances. The difference is 2 tasks; it cannot be distinguished from the baseline's own success rate. The fallback is therefore a safety net with roughly baseline reliability, not a rescue. Causes: verification failure (11 of 18) and decision failure (7); no timeouts, tool-policy aborts or budget exhaustion occurred.

The overall E1 figures (92%) are over the 120 attempted tasks, i.e. excluding the 30 `queue` tasks where no capability matched; they must not be compared with the all-task E2 figure (78–79%). The like-for-like comparison is per family (above).

### 3.3 Negative transfer and unlearned family

`queue` has no capability or memory. E2 equals A there (10% = 10%; 10% vs 13% in D). Ungated memory on `queue` (UB/UC/UD) scored 0%/3%/0% (set C, A = 10%) and 10%/7%/3% (set D, A = 13%): point estimates at or below the baseline, never above, but with 30 tasks no significant harm is detectable either. Gating prevented it by construction (`queue` tasks ran as plain planning). No generalisation is claimed from `queue`.

### 3.4 Asset decisions under `serving-low-v1` (16 assets: 2 capabilities and 6 memories per set)

| Verdict | count (of 16) |
|---|---|
| Safety PASS | 16 (zero-tolerance events: none observed; cross-tenant access and forbidden-resource modification are **untested** in this benchmark) |
| Intrinsic PASS / FAIL / INCONCLUSIVE | 5 / 4 / 7 |
| Serving PASS / INCONCLUSIVE | 5 / 11 |
| Economic PASS | 4 |
| Classification: reliable and economically useful | 4 |
| Classification: insufficiently evaluated | 12 |
| Classified harmful, or reliable-but-not-worth-the-cost | 0 |

The four "reliable and economically useful" assets: set C `pool.f12f4fa920@1` (E1 97%, paired serving +77% [+67%, +85%], 0/14 regressions, 0.09× baseline tokens per verified success) and `mem_procedural_timeout@1`; set D `timeout.e2fb15fa79@1` (+37% [+25%, +49%], 1/33 regressions, 0.31× tokens) and `mem_episodic_timeout@1`.

**The two capabilities replicate only partially.** Their definitions are byte-identical in both sets (and to M5.8's), yet each passes in one set and is INCONCLUSIVE in the other: `pool` passes in C and is INCONCLUSIVE in D (1/13 regressions; the exact upper bound 32% needs ~9 more baseline-solved tasks); `timeout` is INCONCLUSIVE in C (2/27 regressions; bound 22% > 20%, needs ~3 more) and passes in D. Neither is a negative finding: no capability is FAIL or HARMFUL. But it means a single 60-task evaluation is not enough to settle a marginal asset, and no pooled analysis was pre-registered, so none is reported.

Four memory assets are intrinsically FAIL (asset-alone success under 75% and a regression bound that cannot show ≤ 20%): `mem_episodic_pool` and `mem_consolidated_timeout` in both sets. These are genuine quality shortfalls, not sample-size effects (success 65% on 60 tasks).

### 3.5 Economics

Lifecycle tokens are reported per asset for R ∈ {0, 1, 3} full re-evaluations in the tables (learning = discovery + analysis tokens of the family; evaluation tokens are charged unshared; creation costs no model tokens). Example, set C `pool.f12f4fa920@1` at R = 0: 599,717 tokens, break-even after 30 verified successes, 13,037 tokens per verified improvement; at R = 3: 1.90 M tokens, 41,390 per improvement. Memory assets cost about 1.5–3× more per verified improvement than the capability of the same family (e.g. C `mem_procedural_timeout@1`: 37,890 at R = 0). Savings never count unless safety and serving pass; `mem_procedural_timeout@1` in set D passes serving but its cost ratio is 0.83× with an upper bound of 1.03, so the economic verdict is INCONCLUSIVE. Validation still dominates learning cost (evaluation 434k–951k tokens per asset against 109k–165k for learning), as in M5.8.

## 4. Answers to the three separate questions

1. **Is the asset intrinsically good?** Layer A: yes for 5 of 16 (incl. one capability per set), no for 4 memory assets, undecided for 7.
2. **Does serving it with the production fallback improve outcomes?** Yes for the same 5; the fallback contributes ≤ 3.3 points on the learned families.
3. **Is it worth its lifecycle cost?** Demonstrated for 4; E2 is about 2.5× cheaper per verified success than the memory arms (4.4–4.6k vs 10.7–11.9k tokens; A 18–20k).

## 5. Recommendation

* Keep `default-low` governing. Keep `default-low-v2` and `serving-low-v1` experimental. Promote nothing from this experiment.
* Gate promotion decisions on Layer B (production path incl. fallback) **and** Layer A: a capability that only works through the fallback must be classified `IMPERFECT_BUT_USEFUL_WITH_FALLBACK`, not intrinsically reliable. In these runs no capability needed that class.
* Before adopting `serving-low-v1`: (a) a decision on pooling or extending evidence for INCONCLUSIVE assets (targeted accrual of baseline-solved tasks, which the report quantifies per asset), (b) a run with the policy actually *governing* instead of as a counterfactual, (c) a confirmation on a fresh seed, (d) a protected-resource and multi-tenant benchmark so the zero-tolerance clauses are exercised.

## 6. Limitations and negative/inconclusive results (stated plainly)

* **No supporting hypothesis test was pre-registered** for "serving-low-v1 beats default-low"; this milestone reports the counterfactual decisions and effect sizes only. The policy is therefore not shown to be better than any other policy.
* 12 of 16 assets are inconclusive on at least one gate; one repetition per task, so the within-task repeat noise is not measured. Task outcomes are single draws from a stochastic model.
* E1 is the capability phase of E2 runs and depends on the handover verification (one extra read-only verifier call). The handover check uses the same independent verifier as the final outcome and is read-only; it is not charged to any arm's token budget.
* The planned ACTIVE-only production-mode smoke run was **not** executed live; isolation (only ACTIVE assets are served; approval can only remove assets) is covered by the integration test `test_serving_cli_attributes_phases_serves_only_active_and_honours_strategy_approval`.
* Both capabilities are identical to M5.8's, so M5.8 had already shown their aggregate behaviour; the memory assets are new. Sets C and D share a design; P5 and P6 are different task sets, so the sets are independent replications of the procedure, not repeated measurements.
* The benchmark is simulated: no protected resources, no tenants, a fixed tool set. Safety PASS means only that the exercised constraints were not violated.
* `queue` is pilot-sized (30 tasks) and has no learned asset.
