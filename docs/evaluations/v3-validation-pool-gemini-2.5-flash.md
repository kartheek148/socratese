# Evaluation: pool.f12f4fa920 v1

**Verdict: PASS** (risk class `low`, policy `default-low` hash `7b6f0944819b…`)

Reasons:
- all promotion gates satisfied

## Reproducibility metadata

- DISCOVERY: `connection-debug-sim` v3.0.0 hash `f93198d3e3c5…`
- HIDDEN_TRANSFER: `connection-debug-sim` v3.0.0-private-x2 hash `9d225771b60e…`
- VALIDATION: `connection-debug-sim` v3.0.0-private-x2 hash `5ba6572dcd52…`
- Model: `vertex-ai:global` / `google/gemini-2.5-flash`; seeds [0, 1]
- Runtime `baseline-runtime/0.1.0`, verifier `sim-verifier/1`, environment `sim-env/1`, evaluator `evaluator/1`

## Results

| Suite | Arm | Runs | Verified | Success (95% Wilson CI) | Tokens / success | Tool calls / success | Mean latency ms | Notes |
|---|---|---|---|---|---|---|---|---|
| SOURCE_REPLAY | A_BASELINE | 16 (8 tasks) | 6 | 38% (18%-61%) | 15232 | 12.0 | 27539 |  |
| SOURCE_REPLAY | B_MEMORY | 16 (8 tasks) | 9 | 56% (33%-77%) | 10621 | 8.1 | 22818 |  |
| SOURCE_REPLAY | C_CAPABILITY | 16 (8 tasks) | 16 | 100% (81%-100%) | 2053 | 5.0 | 7811 | applicable 16/16 |
| HELD_OUT | A_BASELINE | 24 (12 tasks) | 6 | 25% (12%-45%) | 22016 | 16.8 | 24994 |  |
| HELD_OUT | B_MEMORY | 24 (12 tasks) | 20 | 83% (64%-93%) | 7980 | 5.8 | 24949 |  |
| HELD_OUT | C_CAPABILITY | 24 (12 tasks) | 23 | 96% (80%-99%) | 2152 | 5.2 | 8354 | applicable 24/24 |
| TRANSFER | A_BASELINE | 24 (12 tasks) | 8 | 33% (18%-53%) | 16363 | 13.0 | 23274 |  |
| TRANSFER | B_MEMORY | 24 (12 tasks) | 21 | 88% (69%-96%) | 7298 | 5.6 | 22972 |  |
| TRANSFER | C_CAPABILITY | 24 (12 tasks) | 24 | 100% (86%-100%) | 1972 | 5.0 | 7386 | applicable 24/24 |
| NEGATIVE (correct refusals) | C_CAPABILITY | 8 | 8 | 100% (0%-32%) | n/a | n/a | 50 | applicable 0/8 |
| REGRESSION | C_CAPABILITY | 20 | 20 | 100% (84%-100%) | 1925 | 5.0 | 7593 | applicable 20/20 |

- False applicability: 0%; regressions vs baseline: 0; 95% lower bound on held-out+transfer success: 89%

## Lifecycle cost (includes learning and evaluation overhead)

- Learning: 0 tokens, 1820 ms wall time
- Evaluation overhead (control arms A+B): 762834 tokens
- Capability execution: 129685 tokens for 63 verified successes
- **Lifecycle tokens per verified success (C): 14167** vs baseline execution only: 17720

## Notes

- Source replay is necessary but never sufficient; held-out, transfer and negative-applicability evidence is required for a PASS.
- Success is decided by the independent verifier from final environment state for every arm.
- Lifecycle cost charges the capability for the control-arm tokens spent evaluating it; amortization over future use is not credited here.
- Small samples: treat differences within the confidence intervals as unproven.
- With several seeds every task is re-run once per seed. Those runs are correlated (same task), so the Wilson intervals are optimistic; sample-size gates count distinct tasks, not runs.
