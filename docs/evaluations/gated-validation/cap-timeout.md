# Evaluation: timeout.e2fb15fa79 v1

**Verdict: FAIL** (risk class `low`, policy `default-low` hash `7b6f0944819b…`)

Reasons:
- source replay success 75% < 80% (necessary, not sufficient)
- 5 regressions vs baseline > allowed 1

## Reproducibility metadata

- DISCOVERY: `connection-debug-sim` v3.0.0 hash `e9e306f8f2c1…`
- HIDDEN_TRANSFER: `connection-debug-sim` v3.0.0-private-x3 hash `1830838e93cc…`
- VALIDATION: `connection-debug-sim` v3.0.0-private-x3 hash `a83f1d256b36…`
- Model: `vertex-ai:global` / `google/gemini-2.5-flash`; seeds [0, 1]
- Runtime `baseline-runtime/0.1.0`, verifier `sim-verifier/1`, environment `sim-env/1`, evaluator `evaluator/1`

## Results

| Suite | Arm | Runs | Verified | Success (95% Wilson CI) | Tokens / success | Tool calls / success | Mean latency ms | Notes |
|---|---|---|---|---|---|---|---|---|
| SOURCE_REPLAY | A_BASELINE | 16 (8 tasks) | 8 | 50% (28%-72%) | 13614 | 8.8 | 32007 |  |
| SOURCE_REPLAY | B_MEMORY | 16 (8 tasks) | 8 | 50% (28%-72%) | 12479 | 9.0 | 26690 |  |
| SOURCE_REPLAY | C_CAPABILITY | 16 (8 tasks) | 12 | 75% (51%-90%) | 3648 | 6.7 | 11086 | applicable 16/16 |
| HELD_OUT | A_BASELINE | 36 (18 tasks) | 17 | 47% (32%-63%) | 14402 | 8.9 | 30754 |  |
| HELD_OUT | B_MEMORY | 36 (18 tasks) | 13 | 36% (22%-52%) | 19142 | 11.4 | 28063 |  |
| HELD_OUT | C_CAPABILITY | 36 (18 tasks) | 27 | 75% (59%-86%) | 3439 | 6.6 | 11750 | applicable 36/36 |
| TRANSFER | A_BASELINE | 36 (18 tasks) | 20 | 56% (40%-70%) | 10256 | 7.7 | 24627 |  |
| TRANSFER | B_MEMORY | 36 (18 tasks) | 22 | 61% (45%-75%) | 9491 | 7.0 | 23470 |  |
| TRANSFER | C_CAPABILITY | 36 (18 tasks) | 34 | 94% (82%-98%) | 2820 | 5.3 | 10664 | applicable 36/36 |
| NEGATIVE (correct refusals) | C_CAPABILITY | 8 | 8 | 100% (0%-32%) | n/a | n/a | 12 | applicable 0/8 |
| REGRESSION | C_CAPABILITY | 45 | 40 | 89% (77%-95%) | 3013 | 5.6 | 11141 | applicable 45/45 |

- False applicability: 0%; regressions vs baseline: 5; 95% lower bound on held-out+transfer success: 75%

## Lifecycle cost (includes learning and evaluation overhead)

- Learning: 0 tokens, 1667 ms wall time
- Evaluation overhead (control arms A+B): 1116331 tokens
- Capability execution: 232541 tokens for 73 verified successes
- **Lifecycle tokens per verified success (C): 18478** vs baseline execution only: 12419

## Notes

- Source replay is necessary but never sufficient; held-out, transfer and negative-applicability evidence is required for a PASS.
- Success is decided by the independent verifier from final environment state for every arm.
- Lifecycle cost charges the capability for the control-arm tokens spent evaluating it; amortization over future use is not credited here.
- Small samples: treat differences within the confidence intervals as unproven.
- With several seeds every task is re-run once per seed. Those runs are correlated (same task), so the Wilson intervals are optimistic; sample-size gates count distinct tasks, not runs.
