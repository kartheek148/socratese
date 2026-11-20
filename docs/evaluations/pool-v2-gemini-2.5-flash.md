# Evaluation: pool.f12f4fa920 v2

**Verdict: FAIL** (risk class `low`, policy `default-low` hash `7b6f0944819b…`)

Reasons:
- source replay success 67% < 80% (necessary, not sufficient)

## Reproducibility metadata

- DISCOVERY: `connection-debug-sim` v1.0.0 hash `8170dc437881…`
- HIDDEN_TRANSFER: `connection-debug-sim` v1.0.0-private hash `51ea9f29c421…`
- VALIDATION: `connection-debug-sim` v1.0.0-private hash `6a0c4ba80a13…`
- Model: `vertex-ai:global` / `google/gemini-2.5-flash`; seeds [0]
- Runtime `baseline-runtime/0.1.0`, verifier `sim-verifier/1`, environment `sim-env/1`, evaluator `evaluator/1`

## Results

| Suite | Arm | Tasks | Verified | Success (95% Wilson CI) | Tokens / success | Tool calls / success | Mean latency ms | Notes |
|---|---|---|---|---|---|---|---|---|
| SOURCE_REPLAY | A_BASELINE | 6 | 3 | 50% (19%-81%) | 12126 | 9.7 | 27450 |  |
| SOURCE_REPLAY | B_MEMORY | 6 | 4 | 67% (30%-90%) | 10519 | 8.5 | 27080 |  |
| SOURCE_REPLAY | C_CAPABILITY | 6 | 4 | 67% (30%-90%) | 2915 | 7.5 | 8333 | applicable 6/6 |
| HELD_OUT | A_BASELINE | 4 | 3 | 75% (30%-95%) | 7111 | 6.3 | 21208 |  |
| HELD_OUT | B_MEMORY | 4 | 4 | 100% (51%-100%) | 6127 | 5.2 | 21219 |  |
| HELD_OUT | C_CAPABILITY | 4 | 3 | 75% (30%-95%) | 2214 | 6.7 | 7063 | applicable 4/4 |
| TRANSFER | A_BASELINE | 4 | 4 | 100% (51%-100%) | 4880 | 4.5 | 21866 |  |
| TRANSFER | B_MEMORY | 4 | 4 | 100% (51%-100%) | 5635 | 5.0 | 20263 |  |
| TRANSFER | C_CAPABILITY | 4 | 4 | 100% (51%-100%) | 1636 | 5.0 | 6098 | applicable 4/4 |
| NEGATIVE (correct refusals) | C_CAPABILITY | 8 | 8 | 100% (0%-32%) | n/a | n/a | 5 | applicable 0/8 |
| REGRESSION | C_CAPABILITY | 10 | 10 | 100% (72%-100%) | 1777 | 5.0 | 7041 | applicable 10/10 |

- False applicability: 0%; regressions vs baseline: 0; 95% lower bound on held-out+transfer success: 53%

## Lifecycle cost (includes learning and evaluation overhead)

- Learning: 0 tokens, 2159 ms wall time
- Evaluation overhead (control arms A+B): 166354 tokens
- Capability execution: 24848 tokens for 11 verified successes
- **Lifecycle tokens per verified success (C): 17382** vs baseline execution only: 7723

## Notes

- Source replay is necessary but never sufficient; held-out, transfer and negative-applicability evidence is required for a PASS.
- Success is decided by the independent verifier from final environment state for every arm.
- Lifecycle cost charges the capability for the control-arm tokens spent evaluating it; amortization over future use is not credited here.
- Small samples: treat differences within the confidence intervals as unproven.
