# Evaluation: mem_episodic_timeout v1

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
| SOURCE_REPLAY | A_BASELINE | 16 (8 tasks) | 10 | 62% (39%-82%) | 10004 | 6.9 | 28917 |  |
| SOURCE_REPLAY | M_MEMORY_ASSET | 16 (8 tasks) | 14 | 88% (64%-97%) | 8583 | 5.5 | 30966 | applicable 16/16 |
| HELD_OUT | A_BASELINE | 36 (18 tasks) | 20 | 56% (40%-70%) | 12565 | 7.4 | 33534 |  |
| HELD_OUT | M_MEMORY_ASSET | 36 (18 tasks) | 31 | 86% (71%-94%) | 9492 | 5.5 | 29626 | applicable 36/36 |
| TRANSFER | A_BASELINE | 36 (18 tasks) | 19 | 53% (37%-68%) | 12421 | 7.8 | 27244 |  |
| TRANSFER | M_MEMORY_ASSET | 36 (18 tasks) | 28 | 78% (62%-88%) | 9208 | 5.9 | 25595 | applicable 36/36 |
| NEGATIVE (correct refusals) | M_MEMORY_ASSET | 8 | 8 | 100% (0%-32%) | n/a | n/a | 0 | applicable 0/8 |
| REGRESSION | M_MEMORY_ASSET | 49 | 46 | 94% (83%-98%) | 8222 | 5.0 | 29005 | applicable 49/49 |

- False applicability: 0%; regressions vs baseline: 3; 95% lower bound on held-out+transfer success: 72%

## Lifecycle cost (includes learning and evaluation overhead)

- Learning: 0 tokens, 0 ms wall time
- Evaluation overhead (control arms A+B): 587339 tokens
- Capability execution: 672244 tokens for 73 verified successes
- **Lifecycle tokens per verified success (C): 17255** vs baseline execution only: 11987

## Notes

- Source replay is necessary but never sufficient; held-out, transfer and negative-applicability evidence is required for a PASS.
- Success is decided by the independent verifier from final environment state for every arm.
- Lifecycle cost charges the capability for the control-arm tokens spent evaluating it; amortization over future use is not credited here.
- Small samples: treat differences within the confidence intervals as unproven.
- With several seeds every task is re-run once per seed. Those runs are correlated (same task), so the Wilson intervals are optimistic; sample-size gates count distinct tasks, not runs.
