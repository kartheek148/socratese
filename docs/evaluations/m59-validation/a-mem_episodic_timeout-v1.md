# Evaluation: mem_episodic_timeout v1

**Verdict: FAIL** (risk class `low`, policy `default-low` hash `7b6f0944819b…`)

Reasons:
- held-out success 64% < 75%
- transfer success 69% < 75%
- 5 regressions vs baseline > allowed 1

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
| SOURCE_REPLAY | M_MEMORY_ASSET | 16 (8 tasks) | 14 | 88% (64%-97%) | 10541 | 5.4 | 36346 | applicable 16/16 |
| HELD_OUT | A_BASELINE | 36 (18 tasks) | 16 | 44% (30%-60%) | 15040 | 9.5 | 28795 |  |
| HELD_OUT | M_MEMORY_ASSET | 36 (18 tasks) | 23 | 64% (48%-78%) | 13625 | 7.3 | 31216 | applicable 36/36 |
| TRANSFER | A_BASELINE | 36 (18 tasks) | 14 | 39% (25%-55%) | 15458 | 10.5 | 30555 |  |
| TRANSFER | M_MEMORY_ASSET | 36 (18 tasks) | 25 | 69% (53%-82%) | 11557 | 6.8 | 29287 | applicable 36/36 |
| NEGATIVE (correct refusals) | M_MEMORY_ASSET | 8 | 8 | 100% (0%-32%) | n/a | n/a | 0 | applicable 0/8 |
| REGRESSION | M_MEMORY_ASSET | 41 | 36 | 88% (74%-95%) | 10167 | 5.4 | 32825 | applicable 41/41 |

- False applicability: 0%; regressions vs baseline: 5; 95% lower bound on held-out+transfer success: 55%

## Lifecycle cost (includes learning and evaluation overhead)

- Learning: 0 tokens, 0 ms wall time
- Evaluation overhead (control arms A+B): 586470 tokens
- Capability execution: 749877 tokens for 62 verified successes
- **Lifecycle tokens per verified success (C): 21554** vs baseline execution only: 14304

## Notes

- Source replay is necessary but never sufficient; held-out, transfer and negative-applicability evidence is required for a PASS.
- Success is decided by the independent verifier from final environment state for every arm.
- Lifecycle cost charges the capability for the control-arm tokens spent evaluating it; amortization over future use is not credited here.
- Small samples: treat differences within the confidence intervals as unproven.
- With several seeds every task is re-run once per seed. Those runs are correlated (same task), so the Wilson intervals are optimistic; sample-size gates count distinct tasks, not runs.
