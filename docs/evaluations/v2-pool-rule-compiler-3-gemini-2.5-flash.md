# Evaluation: pool.f12f4fa920 v1

**Verdict: FAIL** (risk class `low`, policy `default-low` hash `7b6f0944819b…`)

Reasons:
- held-out success 67% < 75%

## Reproducibility metadata

- DISCOVERY: `connection-debug-sim` v2.0.0 hash `7d96f8fc39a2…`
- HIDDEN_TRANSFER: `connection-debug-sim` v2.0.0-private hash `64b40219f11b…`
- VALIDATION: `connection-debug-sim` v2.0.0-private hash `6cfb899289d6…`
- Model: `vertex-ai:global` / `google/gemini-2.5-flash`; seeds [0]
- Runtime `baseline-runtime/0.1.0`, verifier `sim-verifier/1`, environment `sim-env/1`, evaluator `evaluator/1`

## Results

| Suite | Arm | Tasks | Verified | Success (95% Wilson CI) | Tokens / success | Tool calls / success | Mean latency ms | Notes |
|---|---|---|---|---|---|---|---|---|
| SOURCE_REPLAY | A_BASELINE | 8 | 3 | 38% (14%-69%) | 14288 | 11.7 | 63718 |  |
| SOURCE_REPLAY | B_MEMORY | 8 | 1 | 12% (2%-47%) | 39813 | 33.0 | 58265 |  |
| SOURCE_REPLAY | C_CAPABILITY | 8 | 7 | 88% (53%-98%) | 2583 | 5.6 | 25276 | applicable 8/8 |
| HELD_OUT | A_BASELINE | 6 | 2 | 33% (10%-70%) | 16849 | 13.0 | 63471 |  |
| HELD_OUT | B_MEMORY | 6 | 3 | 50% (19%-81%) | 10600 | 8.3 | 58828 |  |
| HELD_OUT | C_CAPABILITY | 6 | 4 | 67% (30%-90%) | 2476 | 7.2 | 14587 | applicable 6/6 |
| TRANSFER | A_BASELINE | 6 | 3 | 50% (19%-81%) | 14113 | 9.0 | 62282 |  |
| TRANSFER | B_MEMORY | 6 | 0 | 0% (0%-39%) | n/a | n/a | 53862 |  |
| TRANSFER | C_CAPABILITY | 6 | 6 | 100% (61%-100%) | 1926 | 5.0 | 14312 | applicable 6/6 |
| NEGATIVE (correct refusals) | C_CAPABILITY | 8 | 8 | 100% (0%-32%) | n/a | n/a | 3 | applicable 0/8 |
| REGRESSION | C_CAPABILITY | 8 | 8 | 100% (68%-100%) | 1799 | 5.0 | 14184 | applicable 8/8 |

- False applicability: 0%; regressions vs baseline: 0; 95% lower bound on held-out+transfer success: 55%

## Lifecycle cost (includes learning and evaluation overhead)

- Learning: 0 tokens, 782 ms wall time
- Evaluation overhead (control arms A+B): 216209 tokens
- Capability execution: 39538 tokens for 17 verified successes
- **Lifecycle tokens per verified success (C): 15044** vs baseline execution only: 14862

## Notes

- Source replay is necessary but never sufficient; held-out, transfer and negative-applicability evidence is required for a PASS.
- Success is decided by the independent verifier from final environment state for every arm.
- Lifecycle cost charges the capability for the control-arm tokens spent evaluating it; amortization over future use is not credited here.
- Small samples: treat differences within the confidence intervals as unproven.
