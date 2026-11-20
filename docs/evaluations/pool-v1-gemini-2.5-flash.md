# Evaluation: pool.f12f4fa920 v1

**Verdict: FAIL** (risk class `low`, policy `default-low` hash `7b6f0944819b…`)

Reasons:
- source replay success 50% < 80% (necessary, not sufficient)
- 2 regressions vs baseline > allowed 1

## Reproducibility metadata

- DISCOVERY: `connection-debug-sim` v1.0.0 hash `8170dc437881…`
- HIDDEN_TRANSFER: `connection-debug-sim` v1.0.0-private hash `51ea9f29c421…`
- VALIDATION: `connection-debug-sim` v1.0.0-private hash `6a0c4ba80a13…`
- Model: `vertex-ai:global` / `google/gemini-2.5-flash`; seeds [0]
- Runtime `baseline-runtime/0.1.0`, verifier `sim-verifier/1`, environment `sim-env/1`, evaluator `evaluator/1`

## Results

| Suite | Arm | Tasks | Verified | Success (95% Wilson CI) | Tokens / success | Tool calls / success | Mean latency ms | Notes |
|---|---|---|---|---|---|---|---|---|
| SOURCE_REPLAY | A_BASELINE | 6 | 5 | 83% (44%-97%) | 9315 | 6.6 | 35437 |  |
| SOURCE_REPLAY | B_MEMORY | 6 | 5 | 83% (44%-97%) | 8403 | 6.6 | 25540 |  |
| SOURCE_REPLAY | C_CAPABILITY | 6 | 3 | 50% (19%-81%) | 3065 | 9.7 | 8656 | applicable 6/6 |
| HELD_OUT | A_BASELINE | 4 | 4 | 100% (51%-100%) | 5650 | 4.5 | 23726 |  |
| HELD_OUT | B_MEMORY | 4 | 3 | 75% (30%-95%) | 8225 | 7.3 | 22570 |  |
| HELD_OUT | C_CAPABILITY | 4 | 4 | 100% (51%-100%) | 1722 | 5.0 | 7420 | applicable 4/4 |
| TRANSFER | A_BASELINE | 4 | 4 | 100% (51%-100%) | 5478 | 5.0 | 22259 |  |
| TRANSFER | B_MEMORY | 4 | 4 | 100% (51%-100%) | 5515 | 5.0 | 19526 |  |
| TRANSFER | C_CAPABILITY | 4 | 4 | 100% (51%-100%) | 1570 | 5.0 | 6650 | applicable 4/4 |
| NEGATIVE (correct refusals) | C_CAPABILITY | 8 | 8 | 100% (0%-32%) | n/a | n/a | 3 | applicable 0/8 |
| REGRESSION | C_CAPABILITY | 13 | 11 | 85% (58%-96%) | 1884 | 5.8 | 7740 | applicable 13/13 |

- False applicability: 0%; regressions vs baseline: 2; 95% lower bound on held-out+transfer success: 68%

## Lifecycle cost (includes learning and evaluation overhead)

- Learning: 0 tokens, 1412 ms wall time
- Evaluation overhead (control arms A+B): 179837 tokens
- Capability execution: 22362 tokens for 11 verified successes
- **Lifecycle tokens per verified success (C): 18382** vs baseline execution only: 7007

## Notes

- Source replay is necessary but never sufficient; held-out, transfer and negative-applicability evidence is required for a PASS.
- Success is decided by the independent verifier from final environment state for every arm.
- Lifecycle cost charges the capability for the control-arm tokens spent evaluating it; amortization over future use is not credited here.
- Small samples: treat differences within the confidence intervals as unproven.
