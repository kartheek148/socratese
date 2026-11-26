# ADR 0014 — Validated assets versus validated serving policies (M5.10)

Status: Accepted (serving policy **experimental, not adopted**). Evidence: [design](../experiments/production-aligned-evaluation-design.md), [report](../evaluations/production-aligned-learning-evaluation.md), [tables](../evaluations/production-aligned-serving-tables.md).

## Context
M5.9 exposed that gates judge a capability alone while production serves it with a planner fallback, and that a run finished by the planner is credited to the capability version.

## Decisions
1. **Two questions, never merged.** Layer A (intrinsic: the capability phase alone) and Layer B (serving: the production runtime including fallback) are evaluated and reported separately, plus a safety decision and an economic decision. A capability that works only via the fallback is classified `IMPERFECT_BUT_USEFUL_WITH_FALLBACK`.
2. **Attribution is opt-in and non-invasive.** `--attribute-phases` records capability/fallback phases, causes, tokens and the handover outcome (one extra read-only verifier call). Default execution is unchanged. UNKNOWN is never converted to success or failure.
3. **`serving-low-v1` is a separately versioned experimental policy** (frozen hash pinned by a test), applied as a counterfactual over experiment rows; it never promotes, activates or changes a state. `default-low` and `default-low-v2` are unchanged.
4. **Approval is separate from activation and routing.** The approval catalogs can only remove assets from the ACTIVE set. A runtime that is not approved for an asset cannot serve it.
5. **Task-level statistics are integer-only.** Exact bounds reject non-integer counts; the cluster bootstrap resamples whole tasks. This fixes the fractional-mass defect found in M5.9's evidence builder (8 of 16 assets); M5.9's frozen policy and published verdicts are left as recorded.
6. **Economic savings never compensate** for a failed serving or safety decision.

## Findings
* On two fresh real-model sets, the fallback added at most 3.3 points over the capability alone on the learned families and recovered 7 of 18 capability failures (39%) versus 28% for the baseline on the same tasks: it is a safety net, not a hidden rescue, for these capabilities.
* 4 of 16 assets were reliable and economically useful in their set, 4 memory assets were intrinsically inadequate, and 12 were insufficiently evaluated on at least one gate. The same two capabilities passed in one set and were INCONCLUSIVE in the other (marginal baseline-solved-task counts); none was harmful.
* Nothing was promoted. No hypothesis that `serving-low-v1` outperforms another policy was pre-registered or tested.

## Consequences
* Keep `default-low` governing; `default-low-v2` and `serving-low-v1` stay opt-in.
* Before adoption: a governing (not counterfactual) run, a plan for evidence accrual on INCONCLUSIVE assets, a fresh-seed confirmation, and a benchmark with protected resources and tenants so the zero-tolerance clauses are exercised.
* Debt: one repetition per task; E1 is derived from the production runs rather than executed separately; the planned live ACTIVE-only smoke run was replaced by an integration test.
