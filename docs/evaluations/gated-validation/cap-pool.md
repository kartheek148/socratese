# Evaluation: pool.f12f4fa920 v1

**Verdict: PASS** (risk class `low`, policy `default-low` hash `7b6f0944819b…`)

Reasons:
- all promotion gates satisfied

## Reproducibility metadata

- DISCOVERY: `connection-debug-sim` v3.0.0 hash `e9e306f8f2c1…`
- HIDDEN_TRANSFER: `connection-debug-sim` v3.0.0-private-x3 hash `1830838e93cc…`
- VALIDATION: `connection-debug-sim` v3.0.0-private-x3 hash `a83f1d256b36…`
- Model: `vertex-ai:global` / `google/gemini-2.5-flash`; seeds [0, 1]
- Runtime `baseline-runtime/0.1.0`, verifier `sim-verifier/1`, environment `sim-env/1`, evaluator `evaluator/1`

## Results

| Suite | Arm | Runs | Verified | Success (95% Wilson CI) | Tokens / success | Tool calls / success | Mean latency ms | Notes |
|---|---|---|---|---|---|---|---|---|
| SOURCE_REPLAY | A_BASELINE | 16 (8 tasks) | 4 | 25% (10%-49%) | 22206 | 17.0 | 25997 |  |
| SOURCE_REPLAY | B_MEMORY | 16 (8 tasks) | 1 | 6% (1%-28%) | 80728 | 64.0 | 21378 |  |
| SOURCE_REPLAY | C_CAPABILITY | 16 (8 tasks) | 14 | 88% (64%-97%) | 2502 | 5.7 | 8781 | applicable 16/16 |
| HELD_OUT | A_BASELINE | 36 (18 tasks) | 5 | 14% (6%-29%) | 35549 | 29.6 | 20690 |  |
| HELD_OUT | B_MEMORY | 36 (18 tasks) | 3 | 8% (3%-22%) | 57753 | 48.7 | 20010 |  |
| HELD_OUT | C_CAPABILITY | 36 (18 tasks) | 36 | 100% (90%-100%) | 2002 | 5.0 | 7406 | applicable 36/36 |
| TRANSFER | A_BASELINE | 36 (18 tasks) | 7 | 19% (10%-35%) | 26432 | 21.4 | 21908 |  |
| TRANSFER | B_MEMORY | 36 (18 tasks) | 7 | 19% (10%-35%) | 24107 | 20.6 | 18297 |  |
| TRANSFER | C_CAPABILITY | 36 (18 tasks) | 36 | 100% (90%-100%) | 2119 | 5.0 | 8221 | applicable 36/36 |
| NEGATIVE (correct refusals) | C_CAPABILITY | 8 | 8 | 100% (0%-32%) | n/a | n/a | 4 | applicable 0/8 |
| REGRESSION | C_CAPABILITY | 16 | 16 | 100% (81%-100%) | 2067 | 5.0 | 7949 | applicable 16/16 |

- False applicability: 0%; regressions vs baseline: 0; 95% lower bound on held-out+transfer success: 95%

## Lifecycle cost (includes learning and evaluation overhead)

- Learning: 0 tokens, 2185 ms wall time
- Evaluation overhead (control arms A+B): 874324 tokens
- Capability execution: 183376 tokens for 86 verified successes
- **Lifecycle tokens per verified success (C): 12299** vs baseline execution only: 28224

## Notes

- Source replay is necessary but never sufficient; held-out, transfer and negative-applicability evidence is required for a PASS.
- Success is decided by the independent verifier from final environment state for every arm.
- Lifecycle cost charges the capability for the control-arm tokens spent evaluating it; amortization over future use is not credited here.
- Small samples: treat differences within the confidence intervals as unproven.
- With several seeds every task is re-run once per seed. Those runs are correlated (same task), so the Wilson intervals are optimistic; sample-size gates count distinct tasks, not runs.
