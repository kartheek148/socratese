# Evaluation: timeout.e2fb15fa79 v1

**Verdict: FAIL** (risk class `low`, policy `default-low` hash `7b6f0944819b…`)

Reasons:
- 3 regressions vs baseline > allowed 1

## Reproducibility metadata

- DISCOVERY: `connection-debug-sim` v2.0.0 hash `bf4801a3a24c…`
- HIDDEN_TRANSFER: `connection-debug-sim` v2.0.0-private-x3 hash `f996e68bf973…`
- VALIDATION: `connection-debug-sim` v2.0.0-private-x3 hash `818566de964c…`
- Model: `vertex-ai:global` / `google/gemini-2.5-flash`; seeds [0, 1]
- Runtime `baseline-runtime/0.1.0`, verifier `sim-verifier/1`, environment `sim-env/1`, evaluator `evaluator/1`

## Results

| Suite | Arm | Runs | Verified | Success (95% Wilson CI) | Tokens / success | Tool calls / success | Mean latency ms | Notes |
|---|---|---|---|---|---|---|---|---|
| SOURCE_REPLAY | A_BASELINE | 16 (8 tasks) | 5 | 31% (14%-56%) | 23863 | 13.2 | 74074 |  |
| SOURCE_REPLAY | B_MEMORY | 16 (8 tasks) | 15 | 94% (72%-99%) | 8172 | 5.3 | 68538 |  |
| SOURCE_REPLAY | C_CAPABILITY | 16 (8 tasks) | 15 | 94% (72%-99%) | 2650 | 5.3 | 18155 | applicable 16/16 |
| HELD_OUT | A_BASELINE | 36 (18 tasks) | 16 | 44% (30%-60%) | 16353 | 10.1 | 69596 |  |
| HELD_OUT | B_MEMORY | 36 (18 tasks) | 33 | 92% (78%-97%) | 7673 | 5.5 | 64911 |  |
| HELD_OUT | C_CAPABILITY | 36 (18 tasks) | 34 | 94% (82%-98%) | 3050 | 5.3 | 18781 | applicable 36/36 |
| TRANSFER | A_BASELINE | 36 (18 tasks) | 12 | 33% (20%-50%) | 17728 | 12.2 | 65313 |  |
| TRANSFER | B_MEMORY | 36 (18 tasks) | 35 | 97% (86%-100%) | 7524 | 5.1 | 66253 |  |
| TRANSFER | C_CAPABILITY | 36 (18 tasks) | 30 | 83% (68%-92%) | 2824 | 5.8 | 19102 | applicable 36/36 |
| NEGATIVE (correct refusals) | C_CAPABILITY | 8 | 8 | 100% (0%-32%) | n/a | n/a | 4 | applicable 0/8 |
| REGRESSION | C_CAPABILITY | 33 | 30 | 91% (76%-97%) | 2745 | 5.4 | 18902 | applicable 33/33 |

- False applicability: 0%; regressions vs baseline: 3; 95% lower bound on held-out+transfer success: 80%

## Lifecycle cost (includes learning and evaluation overhead)

- Learning: 0 tokens, 1652 ms wall time
- Evaluation overhead (control arms A+B): 1232829 tokens
- Capability execution: 228162 tokens for 79 verified successes
- **Lifecycle tokens per verified success (C): 18494** vs baseline execution only: 17991

## Notes

- Source replay is necessary but never sufficient; held-out, transfer and negative-applicability evidence is required for a PASS.
- Success is decided by the independent verifier from final environment state for every arm.
- Lifecycle cost charges the capability for the control-arm tokens spent evaluating it; amortization over future use is not credited here.
- Small samples: treat differences within the confidence intervals as unproven.
- With several seeds every task is re-run once per seed. Those runs are correlated (same task), so the Wilson intervals are optimistic; sample-size gates count distinct tasks, not runs.
