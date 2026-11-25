# Evaluation: mem_consolidated_pool v1

**Verdict: FAIL** (risk class `low`, policy `default-low` hash `7b6f0944819b…`)

Reasons:
- 3 regressions vs baseline > allowed 1

## Reproducibility metadata

- DISCOVERY: `connection-debug-sim` v3.0.0 hash `e9e306f8f2c1…`
- HIDDEN_TRANSFER: `connection-debug-sim` v3.0.0-private-x3 hash `1830838e93cc…`
- VALIDATION: `connection-debug-sim` v3.0.0-private-x3 hash `a83f1d256b36…`
- Model: `vertex-ai:global` / `google/gemini-2.5-flash`; seeds [0, 1]
- Runtime `baseline-runtime/0.1.0`, verifier `sim-verifier/1`, environment `sim-env/1`, evaluator `evaluator/1`

## Results

| Suite | Arm | Runs | Verified | Success (95% Wilson CI) | Tokens / success | Tool calls / success | Mean latency ms | Notes |
|---|---|---|---|---|---|---|---|---|
| SOURCE_REPLAY | A_BASELINE | 16 (8 tasks) | 6 | 38% (18%-61%) | 14348 | 11.7 | 27149 |  |
| SOURCE_REPLAY | M_MEMORY_ASSET | 16 (8 tasks) | 14 | 88% (64%-97%) | 8717 | 5.7 | 25291 | applicable 16/16 |
| HELD_OUT | A_BASELINE | 36 (18 tasks) | 6 | 17% (8%-32%) | 31160 | 24.5 | 23421 |  |
| HELD_OUT | M_MEMORY_ASSET | 36 (18 tasks) | 34 | 94% (82%-98%) | 7803 | 5.3 | 23409 | applicable 36/36 |
| TRANSFER | A_BASELINE | 36 (18 tasks) | 15 | 42% (27%-58%) | 12769 | 10.4 | 23211 |  |
| TRANSFER | M_MEMORY_ASSET | 36 (18 tasks) | 33 | 92% (78%-97%) | 8410 | 5.5 | 23515 | applicable 36/36 |
| NEGATIVE (correct refusals) | M_MEMORY_ASSET | 8 | 8 | 100% (0%-32%) | n/a | n/a | 0 | applicable 0/8 |
| REGRESSION | M_MEMORY_ASSET | 27 | 24 | 89% (72%-96%) | 8354 | 5.5 | 23508 | applicable 27/27 |

- False applicability: 0%; regressions vs baseline: 3; 95% lower bound on held-out+transfer success: 85%

## Lifecycle cost (includes learning and evaluation overhead)

- Learning: 0 tokens, 0 ms wall time
- Evaluation overhead (control arms A+B): 464587 tokens
- Capability execution: 664893 tokens for 81 verified successes
- **Lifecycle tokens per verified success (C): 13944** vs baseline execution only: 17207

## Notes

- Source replay is necessary but never sufficient; held-out, transfer and negative-applicability evidence is required for a PASS.
- Success is decided by the independent verifier from final environment state for every arm.
- Lifecycle cost charges the capability for the control-arm tokens spent evaluating it; amortization over future use is not credited here.
- Small samples: treat differences within the confidence intervals as unproven.
- With several seeds every task is re-run once per seed. Those runs are correlated (same task), so the Wilson intervals are optimistic; sample-size gates count distinct tasks, not runs.
