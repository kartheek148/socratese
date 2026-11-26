# Evaluation: timeout.de1091cd4d v1

**Verdict: FAIL** (risk class `low`, policy `default-low` hash `7b6f0944819b…`)

Reasons:
- source replay success 0% < 80% (necessary, not sufficient)
- held-out success 3% < 75%
- transfer success 0% < 75%
- 95% lower bound on held-out+transfer success 0% < 50%
- 41 regressions vs baseline > allowed 1

## Reproducibility metadata

- DISCOVERY: `connection-debug-sim` v3.0.0 hash `8d480d64c35a…`
- HIDDEN_TRANSFER: `connection-debug-sim` v3.0.0-private-x3 hash `1ca9b77e7d15…`
- VALIDATION: `connection-debug-sim` v3.0.0-private-x3 hash `bed166c4332f…`
- Model: `vertex-ai:global` / `google/gemini-2.5-flash`; seeds [0, 1]
- Runtime `baseline-runtime/0.1.0`, verifier `sim-verifier/1`, environment `sim-env/1`, evaluator `evaluator/1`

## Results

| Suite | Arm | Runs | Verified | Success (95% Wilson CI) | Tokens / success | Tool calls / success | Mean latency ms | Notes |
|---|---|---|---|---|---|---|---|---|
| SOURCE_REPLAY | A_BASELINE | 16 (8 tasks) | 11 | 69% (44%-86%) | 11765 | 6.3 | 37999 |  |
| SOURCE_REPLAY | C_CAPABILITY | 16 (8 tasks) | 0 | 0% (0%-19%) | n/a | n/a | 20102 | applicable 16/16 |
| HELD_OUT | A_BASELINE | 36 (18 tasks) | 16 | 44% (30%-60%) | 15040 | 9.5 | 28795 |  |
| HELD_OUT | C_CAPABILITY | 36 (18 tasks) | 1 | 3% (0%-14%) | 32427 | 118.0 | 20309 | applicable 36/36 |
| TRANSFER | A_BASELINE | 36 (18 tasks) | 14 | 39% (25%-55%) | 14761 | 10.6 | 27722 |  |
| TRANSFER | C_CAPABILITY | 36 (18 tasks) | 0 | 0% (0%-10%) | n/a | n/a | 20036 | applicable 36/36 |
| NEGATIVE (correct refusals) | C_CAPABILITY | 8 | 8 | 100% (0%-32%) | n/a | n/a | 14 | applicable 0/8 |
| REGRESSION | C_CAPABILITY | 41 | 0 | 0% (0%-9%) | n/a | n/a | 20432 | applicable 41/41 |

- False applicability: 0%; regressions vs baseline: 41; 95% lower bound on held-out+transfer success: 0%

## Lifecycle cost (includes learning and evaluation overhead)

- Learning: 0 tokens, 2210 ms wall time
- Evaluation overhead (control arms A+B): 576710 tokens
- Capability execution: 75219 tokens for 1 verified successes
- **Lifecycle tokens per verified success (C): 651929** vs baseline execution only: 14066

## Notes

- Source replay is necessary but never sufficient; held-out, transfer and negative-applicability evidence is required for a PASS.
- Success is decided by the independent verifier from final environment state for every arm.
- Lifecycle cost charges the capability for the control-arm tokens spent evaluating it; amortization over future use is not credited here.
- Small samples: treat differences within the confidence intervals as unproven.
- With several seeds every task is re-run once per seed. Those runs are correlated (same task), so the Wilson intervals are optimistic; sample-size gates count distinct tasks, not runs.
