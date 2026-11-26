# Evaluation: timeout.e2fb15fa79 v1

**Verdict: FAIL** (risk class `low`, policy `default-low` hash `7b6f0944819b…`)

Reasons:
- 6 regressions vs baseline > allowed 1

## Reproducibility metadata

- DISCOVERY: `connection-debug-sim` v3.0.0 hash `9c4791e9b5a6…`
- HIDDEN_TRANSFER: `connection-debug-sim` v3.0.0-private-x3 hash `1ca9b77e7d15…`
- VALIDATION: `connection-debug-sim` v3.0.0-private-x3 hash `bed166c4332f…`
- Model: `vertex-ai:global` / `google/gemini-2.5-flash`; seeds [0, 1]
- Runtime `baseline-runtime/0.1.0`, verifier `sim-verifier/1`, environment `sim-env/1`, evaluator `evaluator/1`

## Results

| Suite | Arm | Runs | Verified | Success (95% Wilson CI) | Tokens / success | Tool calls / success | Mean latency ms | Notes |
|---|---|---|---|---|---|---|---|---|
| SOURCE_REPLAY | A_BASELINE | 16 (8 tasks) | 8 | 50% (28%-72%) | 16886 | 8.8 | 39441 |  |
| SOURCE_REPLAY | C_CAPABILITY | 16 (8 tasks) | 13 | 81% (57%-93%) | 2775 | 6.0 | 11840 | applicable 16/16 |
| HELD_OUT | A_BASELINE | 36 (18 tasks) | 16 | 44% (30%-60%) | 15040 | 9.5 | 28795 |  |
| HELD_OUT | C_CAPABILITY | 36 (18 tasks) | 30 | 83% (68%-92%) | 2561 | 5.8 | 12341 | applicable 36/36 |
| TRANSFER | A_BASELINE | 36 (18 tasks) | 13 | 36% (22%-52%) | 17481 | 11.3 | 31406 |  |
| TRANSFER | C_CAPABILITY | 36 (18 tasks) | 35 | 97% (86%-100%) | 2701 | 5.1 | 12382 | applicable 36/36 |
| NEGATIVE (correct refusals) | C_CAPABILITY | 8 | 8 | 100% (0%-32%) | n/a | n/a | 8 | applicable 0/8 |
| REGRESSION | C_CAPABILITY | 37 | 31 | 84% (69%-92%) | 2635 | 5.8 | 12934 | applicable 37/37 |

- False applicability: 0%; regressions vs baseline: 6; 95% lower bound on held-out+transfer success: 81%

## Lifecycle cost (includes learning and evaluation overhead)

- Learning: 0 tokens, 2400 ms wall time
- Evaluation overhead (control arms A+B): 602987 tokens
- Capability execution: 207440 tokens for 78 verified successes
- **Lifecycle tokens per verified success (C): 10390** vs baseline execution only: 16297

## Notes

- Source replay is necessary but never sufficient; held-out, transfer and negative-applicability evidence is required for a PASS.
- Success is decided by the independent verifier from final environment state for every arm.
- Lifecycle cost charges the capability for the control-arm tokens spent evaluating it; amortization over future use is not credited here.
- Small samples: treat differences within the confidence intervals as unproven.
- With several seeds every task is re-run once per seed. Those runs are correlated (same task), so the Wilson intervals are optimistic; sample-size gates count distinct tasks, not runs.
