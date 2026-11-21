# Evaluation: pool.f12f4fa920 v1

**Verdict: PASS** (risk class `low`, policy `default-low` hash `7b6f0944819b…`)

Reasons:
- all promotion gates satisfied

## Reproducibility metadata

- DISCOVERY: `connection-debug-sim` v2.0.0 hash `bf4801a3a24c…`
- HIDDEN_TRANSFER: `connection-debug-sim` v2.0.0-private-x3 hash `f996e68bf973…`
- VALIDATION: `connection-debug-sim` v2.0.0-private-x3 hash `818566de964c…`
- Model: `vertex-ai:global` / `google/gemini-2.5-flash`; seeds [0, 1]
- Runtime `baseline-runtime/0.1.0`, verifier `sim-verifier/1`, environment `sim-env/1`, evaluator `evaluator/1`

## Results

| Suite | Arm | Runs | Verified | Success (95% Wilson CI) | Tokens / success | Tool calls / success | Mean latency ms | Notes |
|---|---|---|---|---|---|---|---|---|
| SOURCE_REPLAY | A_BASELINE | 16 (8 tasks) | 5 | 31% (14%-56%) | 16955 | 13.4 | 63395 |  |
| SOURCE_REPLAY | B_MEMORY | 16 (8 tasks) | 6 | 38% (18%-61%) | 15104 | 11.7 | 59741 |  |
| SOURCE_REPLAY | C_CAPABILITY | 16 (8 tasks) | 16 | 100% (81%-100%) | 2172 | 5.0 | 15104 | applicable 16/16 |
| HELD_OUT | A_BASELINE | 36 (18 tasks) | 9 | 25% (14%-41%) | 20168 | 16.4 | 56610 |  |
| HELD_OUT | B_MEMORY | 36 (18 tasks) | 11 | 31% (18%-47%) | 19656 | 14.2 | 60190 |  |
| HELD_OUT | C_CAPABILITY | 36 (18 tasks) | 34 | 94% (82%-98%) | 2105 | 5.3 | 13837 | applicable 36/36 |
| TRANSFER | A_BASELINE | 36 (18 tasks) | 9 | 25% (14%-41%) | 20524 | 17.0 | 56848 |  |
| TRANSFER | B_MEMORY | 36 (18 tasks) | 18 | 50% (34%-66%) | 11513 | 9.0 | 59487 |  |
| TRANSFER | C_CAPABILITY | 36 (18 tasks) | 35 | 97% (86%-100%) | 2048 | 5.1 | 14331 | applicable 36/36 |
| NEGATIVE (correct refusals) | C_CAPABILITY | 8 | 8 | 100% (0%-32%) | n/a | n/a | 11 | applicable 0/8 |
| REGRESSION | C_CAPABILITY | 23 | 22 | 96% (79%-99%) | 2087 | 5.2 | 14132 | applicable 23/23 |

- False applicability: 0%; regressions vs baseline: 1; 95% lower bound on held-out+transfer success: 88%

## Lifecycle cost (includes learning and evaluation overhead)

- Learning: 0 tokens, 1238 ms wall time
- Evaluation overhead (control arms A+B): 965067 tokens
- Capability execution: 177987 tokens for 85 verified successes
- **Lifecycle tokens per verified success (C): 13448** vs baseline execution only: 19609

## Notes

- Source replay is necessary but never sufficient; held-out, transfer and negative-applicability evidence is required for a PASS.
- Success is decided by the independent verifier from final environment state for every arm.
- Lifecycle cost charges the capability for the control-arm tokens spent evaluating it; amortization over future use is not credited here.
- Small samples: treat differences within the confidence intervals as unproven.
- With several seeds every task is re-run once per seed. Those runs are correlated (same task), so the Wilson intervals are optimistic; sample-size gates count distinct tasks, not runs.
