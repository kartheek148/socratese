# ADR 0013 — Risk-based promotion policy (candidate) and cheaper validation (M5.9)

Status: Accepted (candidate policy **not adopted**). Evidence: [audit](../experiments/promotion-policy-audit.md), [design](../experiments/promotion-policy-v2-design.md), [retrospective](../evaluations/promotion-policy-retrospective.md), [confirmation](../evaluations/promotion-policy-confirmation-report.md).

## Context
M5.8's absolute `max_regressions <= 1` rejected assets that were beneficial in an independent comparison, and validation was ~97% of measured learning cost.

## Decisions
1. **`default-low`/`-medium`/`-high` are unchanged** (hash pinned by a test; all M5.8 verdicts and reasons are reproduced from stored aggregates). Nothing is promoted by default under any new code path.
2. **A separately versioned candidate, `default-low-v2` (+ medium/high), is added as `RiskBasedPolicy`**: task-level Clopper-Pearson bound on `P(fail | baseline succeeds)`, paired non-inferiority bound, source-replay upper-bound sanity test, variant-concealment guard, zero-tolerance critical violations, three-valued gates (PASS / FAIL / INCONCLUSIVE). v1 and v2 JSON cannot cross-validate; v1 hashes are unaffected. Selected explicitly (`--policy-set risk-v2`) or recorded as a counterfactual (`--counterfactual-policy-set`), which never changes state.
3. **History is immutable**: REJECTED is terminal and evaluation rows are append-only; a new policy never reopens a rejected version. Re-admission is a new version.
4. **Validation efficiency without a weaker standard**: persistent baseline cache (identity: task content, dataset, repetition, model, prompt/runtime/verifier/environment versions, agent config; any change misses; corrupt entries are misses), opt-in removal of the unused `B_MEMORY` arm, measured resource accounting (executed vs reused runs and tokens, input/output where known), and a predefined sequential rule (futility and harm, alpha spent 0.01 + 0.02, never an early PASS). Reuse shares one stochastic baseline draw across assets (documented independence caveat).
5. **Protocol frozen before data** and committed before the confirmation run; parameters were not changed afterwards.

## Findings
* Rejections were mainly a **policy-design** problem (count rule not monotone in evidence; point threshold on 8 tasks); cost was mainly **execution inefficiency** (duplicate baseline, unused arm, no reuse) plus a 22% accounting overcharge in M5.8.
* **The candidate policy failed its frozen confirmation**: on 16 fresh assets it reduced unjustified rejections (5 → 2) and promoted no harmful asset, but promoted fewer assets (1 vs 2) and left a clearly beneficial pool capability INCONCLUSIVE (its regression bound needs ~14-22 baseline-solved tasks, which a weak-baseline family cannot supply at this suite size). Portfolio success on that family fell 98% → 20% (one decision; 47 discordant tasks). Decision rule: NOT SUPPORTED.
* A **gate/production mismatch** was exposed: the gate judges a capability alone, production serves it with a planning fallback; a capability that succeeds on 3% alone looked +48% beneficial through the fallback.
* Measured efficiency: 625k executed tokens per evaluated asset (M5.8 ≈917k), 40% of would-be runs reused; sequential stopping simulated at 40% of executed tokens but population-dependent (67% on a poor population, 11% on a good one) and not yet exercised on a real run.

## Consequences
* Keep `default-low` as the governing policy. Keep `default-low-v2` opt-in and experimental. Do not tune its thresholds on the confirmation data.
* Before any adoption: redesign G1's evidence requirement (targeted accrual of baseline-solved tasks, evidence extension on INCONCLUSIVE, or family-level pooled history), decide whether the gate should evaluate the production runtime path including fallback, and run a new confirmation on a fresh seed.
* Operational: use `--baseline-cache` and `--skip-memory-control` for validation; run `--staged` on a real model before counting any sequential saving.
* Debt: the candidate population in the confirmation was half poor (set A), so the sequential saving is not a forecast; two set-B capabilities were already seen in M5.8.
