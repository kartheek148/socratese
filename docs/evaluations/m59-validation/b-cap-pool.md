# Evaluation: pool.f12f4fa920 v1

**Verdict: PASS** (risk class `low`, policy `default-low` hash `7b6f0944819b…`)

Reasons:
- all promotion gates satisfied

## Reproducibility metadata

- DISCOVERY: `connection-debug-sim` v3.0.0 hash `9c4791e9b5a6…`
- HIDDEN_TRANSFER: `connection-debug-sim` v3.0.0-private-x3 hash `1ca9b77e7d15…`
- VALIDATION: `connection-debug-sim` v3.0.0-private-x3 hash `bed166c4332f…`
- Model: `vertex-ai:global` / `google/gemini-2.5-flash`; seeds [0, 1]
- Runtime `baseline-runtime/0.1.0`, verifier `sim-verifier/1`, environment `sim-env/1`, evaluator `evaluator/1`

## Results

| Suite | Arm | Runs | Verified | Success (95% Wilson CI) | Tokens / success | Tool calls / success | Mean latency ms | Notes |
|---|---|---|---|---|---|---|---|---|
| SOURCE_REPLAY | A_BASELINE | 16 (8 tasks) | 5 | 31% (14%-56%) | 16661 | 13.8 | 24410 |  |
| SOURCE_REPLAY | C_CAPABILITY | 16 (8 tasks) | 16 | 100% (81%-100%) | 1915 | 5.0 | 7775 | applicable 16/16 |
| HELD_OUT | A_BASELINE | 36 (18 tasks) | 4 | 11% (4%-25%) | 44338 | 36.5 | 21918 |  |
| HELD_OUT | C_CAPABILITY | 36 (18 tasks) | 36 | 100% (90%-100%) | 2106 | 5.0 | 9592 | applicable 36/36 |
| TRANSFER | A_BASELINE | 36 (18 tasks) | 10 | 28% (16%-44%) | 19134 | 15.6 | 21445 |  |
| TRANSFER | C_CAPABILITY | 36 (18 tasks) | 35 | 97% (86%-100%) | 2009 | 5.1 | 8200 | applicable 36/36 |
| NEGATIVE (correct refusals) | C_CAPABILITY | 8 | 8 | 100% (0%-32%) | n/a | n/a | 11 | applicable 0/8 |
| REGRESSION | C_CAPABILITY | 19 | 18 | 95% (75%-99%) | 2112 | 5.3 | 8497 | applicable 19/19 |

- False applicability: 0%; regressions vs baseline: 1; 95% lower bound on held-out+transfer success: 93%

## Lifecycle cost (includes learning and evaluation overhead)

- Learning: 0 tokens, 2027 ms wall time
- Evaluation overhead (control arms A+B): 451994 tokens
- Capability execution: 176768 tokens for 87 verified successes
- **Lifecycle tokens per verified success (C): 7227** vs baseline execution only: 23789

## Notes

- Source replay is necessary but never sufficient; held-out, transfer and negative-applicability evidence is required for a PASS.
- Success is decided by the independent verifier from final environment state for every arm.
- Lifecycle cost charges the capability for the control-arm tokens spent evaluating it; amortization over future use is not credited here.
- Small samples: treat differences within the confidence intervals as unproven.
- With several seeds every task is re-run once per seed. Those runs are correlated (same task), so the Wilson intervals are optimistic; sample-size gates count distinct tasks, not runs.
