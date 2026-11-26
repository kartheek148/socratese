# Evaluation: mem_consolidated_pool v1

**Verdict: FAIL** (risk class `low`, policy `default-low` hash `7b6f0944819b…`)

Reasons:
- source replay success 62% < 80% (necessary, not sufficient)
- transfer success 47% < 75%
- 9 regressions vs baseline > allowed 1

## Reproducibility metadata

- DISCOVERY: `connection-debug-sim` v3.0.0 hash `9c4791e9b5a6…`
- HIDDEN_TRANSFER: `connection-debug-sim` v3.0.0-private-x3 hash `1ca9b77e7d15…`
- VALIDATION: `connection-debug-sim` v3.0.0-private-x3 hash `bed166c4332f…`
- Model: `vertex-ai:global` / `google/gemini-2.5-flash`; seeds [0, 1]
- Runtime `baseline-runtime/0.1.0`, verifier `sim-verifier/1`, environment `sim-env/1`, evaluator `evaluator/1`

## Results

| Suite | Arm | Runs | Verified | Success (95% Wilson CI) | Tokens / success | Tool calls / success | Mean latency ms | Notes |
|---|---|---|---|---|---|---|---|---|
| SOURCE_REPLAY | A_BASELINE | 16 (8 tasks) | 4 | 25% (10%-49%) | 21965 | 17.0 | 27858 |  |
| SOURCE_REPLAY | M_MEMORY_ASSET | 16 (8 tasks) | 10 | 62% (39%-82%) | 13054 | 7.3 | 23032 | applicable 16/16 |
| HELD_OUT | A_BASELINE | 36 (18 tasks) | 4 | 11% (4%-25%) | 44338 | 36.5 | 21918 |  |
| HELD_OUT | M_MEMORY_ASSET | 36 (18 tasks) | 29 | 81% (65%-90%) | 10349 | 5.9 | 27181 | applicable 36/36 |
| TRANSFER | A_BASELINE | 36 (18 tasks) | 10 | 28% (16%-44%) | 19134 | 15.6 | 21445 |  |
| TRANSFER | M_MEMORY_ASSET | 36 (18 tasks) | 17 | 47% (32%-63%) | 13715 | 9.6 | 22092 | applicable 36/36 |
| NEGATIVE (correct refusals) | M_MEMORY_ASSET | 8 | 8 | 100% (0%-32%) | n/a | n/a | 0 | applicable 0/8 |
| REGRESSION | M_MEMORY_ASSET | 18 | 9 | 50% (29%-71%) | 13476 | 9.0 | 23857 | applicable 18/18 |

- False applicability: 0%; regressions vs baseline: 9; 95% lower bound on held-out+transfer success: 52%

## Lifecycle cost (includes learning and evaluation overhead)

- Learning: 0 tokens, 0 ms wall time
- Evaluation overhead (control arms A+B): 456550 tokens
- Capability execution: 663822 tokens for 56 verified successes
- **Lifecycle tokens per verified success (C): 20007** vs baseline execution only: 25364

## Notes

- Source replay is necessary but never sufficient; held-out, transfer and negative-applicability evidence is required for a PASS.
- Success is decided by the independent verifier from final environment state for every arm.
- Lifecycle cost charges the capability for the control-arm tokens spent evaluating it; amortization over future use is not credited here.
- Small samples: treat differences within the confidence intervals as unproven.
- With several seeds every task is re-run once per seed. Those runs are correlated (same task), so the Wilson intervals are optimistic; sample-size gates count distinct tasks, not runs.
