# Evaluation: timeout.de1091cd4d v1

**Verdict: FAIL** (risk class `low`, policy `default-low` hash `7b6f0944819b…`)

Reasons:
- source replay success 12% < 80% (necessary, not sufficient)
- held-out success 17% < 75%
- transfer success 21% < 75%
- 95% lower bound on held-out+transfer success 10% < 50%
- 20 regressions vs baseline > allowed 1

## Reproducibility metadata

- DISCOVERY: `connection-debug-sim` v3.0.0 hash `f93198d3e3c5…`
- HIDDEN_TRANSFER: `connection-debug-sim` v3.0.0-private-x2 hash `9d225771b60e…`
- VALIDATION: `connection-debug-sim` v3.0.0-private-x2 hash `5ba6572dcd52…`
- Model: `vertex-ai:global` / `google/gemini-2.5-flash`; seeds [0, 1]
- Runtime `baseline-runtime/0.1.0`, verifier `sim-verifier/1`, environment `sim-env/1`, evaluator `evaluator/1`

## Results

| Suite | Arm | Runs | Verified | Success (95% Wilson CI) | Tokens / success | Tool calls / success | Mean latency ms | Notes |
|---|---|---|---|---|---|---|---|---|
| SOURCE_REPLAY | A_BASELINE | 16 (8 tasks) | 7 | 44% (23%-67%) | 16854 | 9.6 | 38432 |  |
| SOURCE_REPLAY | B_MEMORY | 16 (8 tasks) | 6 | 38% (18%-61%) | 16500 | 10.8 | 27955 |  |
| SOURCE_REPLAY | C_CAPABILITY | 16 (8 tasks) | 2 | 12% (3%-36%) | 7730 | 27.0 | 19476 | applicable 16/16 |
| HELD_OUT | A_BASELINE | 24 (12 tasks) | 13 | 54% (35%-72%) | 10907 | 7.5 | 28439 |  |
| HELD_OUT | B_MEMORY | 24 (12 tasks) | 16 | 67% (47%-82%) | 10846 | 6.2 | 28624 |  |
| HELD_OUT | C_CAPABILITY | 24 (12 tasks) | 4 | 17% (7%-36%) | 5004 | 19.8 | 19987 | applicable 24/24 |
| TRANSFER | A_BASELINE | 24 (12 tasks) | 10 | 42% (24%-61%) | 15043 | 10.0 | 28766 |  |
| TRANSFER | B_MEMORY | 24 (12 tasks) | 12 | 50% (31%-69%) | 11746 | 8.3 | 25499 |  |
| TRANSFER | C_CAPABILITY | 24 (12 tasks) | 5 | 21% (9%-40%) | 11429 | 17.8 | 19850 | applicable 24/24 |
| NEGATIVE (correct refusals) | C_CAPABILITY | 8 | 8 | 100% (0%-32%) | n/a | n/a | 20 | applicable 0/8 |
| REGRESSION | C_CAPABILITY | 30 | 10 | 33% (19%-51%) | 5207 | 10.6 | 20103 | applicable 30/30 |

- False applicability: 0%; regressions vs baseline: 20; 95% lower bound on held-out+transfer success: 10%

## Lifecycle cost (includes learning and evaluation overhead)

- Learning: 0 tokens, 3092 ms wall time
- Evaluation overhead (control arms A+B): 823692 tokens
- Capability execution: 92621 tokens for 11 verified successes
- **Lifecycle tokens per verified success (C): 83301** vs baseline execution only: 13673

## Notes

- Source replay is necessary but never sufficient; held-out, transfer and negative-applicability evidence is required for a PASS.
- Success is decided by the independent verifier from final environment state for every arm.
- Lifecycle cost charges the capability for the control-arm tokens spent evaluating it; amortization over future use is not credited here.
- Small samples: treat differences within the confidence intervals as unproven.
- With several seeds every task is re-run once per seed. Those runs are correlated (same task), so the Wilson intervals are optimistic; sample-size gates count distinct tasks, not runs.
