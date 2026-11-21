# Evaluation: timeout.e2fb15fa79 v1

**Verdict: FAIL** (risk class `low`, policy `default-low` hash `7b6f0944819b…`)

Reasons:
- source replay success 75% < 80% (necessary, not sufficient)
- transfer success 67% < 75%

## Reproducibility metadata

- DISCOVERY: `connection-debug-sim` v2.0.0 hash `7d96f8fc39a2…`
- HIDDEN_TRANSFER: `connection-debug-sim` v2.0.0-private hash `64b40219f11b…`
- VALIDATION: `connection-debug-sim` v2.0.0-private hash `6cfb899289d6…`
- Model: `vertex-ai:global` / `google/gemini-2.5-flash`; seeds [0]
- Runtime `baseline-runtime/0.1.0`, verifier `sim-verifier/1`, environment `sim-env/1`, evaluator `evaluator/1`

## Results

| Suite | Arm | Tasks | Verified | Success (95% Wilson CI) | Tokens / success | Tool calls / success | Mean latency ms | Notes |
|---|---|---|---|---|---|---|---|---|
| SOURCE_REPLAY | A_BASELINE | 8 | 5 | 62% (31%-86%) | 12329 | 7.0 | 75909 |  |
| SOURCE_REPLAY | B_MEMORY | 8 | 4 | 50% (22%-78%) | 12322 | 8.2 | 66124 |  |
| SOURCE_REPLAY | C_CAPABILITY | 8 | 6 | 75% (41%-93%) | 3691 | 6.7 | 18479 | applicable 8/8 |
| HELD_OUT | A_BASELINE | 6 | 2 | 33% (10%-70%) | 16624 | 12.5 | 64951 |  |
| HELD_OUT | B_MEMORY | 6 | 2 | 33% (10%-70%) | 14198 | 11.0 | 58436 |  |
| HELD_OUT | C_CAPABILITY | 6 | 6 | 100% (61%-100%) | 2967 | 5.0 | 19653 | applicable 6/6 |
| TRANSFER | A_BASELINE | 6 | 2 | 33% (10%-70%) | 17090 | 12.0 | 64387 |  |
| TRANSFER | B_MEMORY | 6 | 2 | 33% (10%-70%) | 16015 | 12.0 | 67654 |  |
| TRANSFER | C_CAPABILITY | 6 | 4 | 67% (30%-90%) | 3140 | 7.0 | 41549 | applicable 6/6 |
| NEGATIVE (correct refusals) | C_CAPABILITY | 8 | 8 | 100% (0%-32%) | n/a | n/a | 6 | applicable 0/8 |
| REGRESSION | C_CAPABILITY | 9 | 8 | 89% (56%-98%) | 2502 | 5.5 | 18313 | applicable 9/9 |

- False applicability: 0%; regressions vs baseline: 1; 95% lower bound on held-out+transfer success: 55%

## Lifecycle cost (includes learning and evaluation overhead)

- Learning: 0 tokens, 1004 ms wall time
- Evaluation overhead (control arms A+B): 238787 tokens
- Capability execution: 52508 tokens for 16 verified successes
- **Lifecycle tokens per verified success (C): 18206** vs baseline execution only: 14342

## Notes

- Source replay is necessary but never sufficient; held-out, transfer and negative-applicability evidence is required for a PASS.
- Success is decided by the independent verifier from final environment state for every arm.
- Lifecycle cost charges the capability for the control-arm tokens spent evaluating it; amortization over future use is not credited here.
- Small samples: treat differences within the confidence intervals as unproven.
