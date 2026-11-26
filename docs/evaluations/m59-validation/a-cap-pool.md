# Evaluation: pool.84eaac69f8 v1

**Verdict: FAIL** (risk class `low`, policy `default-low` hash `7b6f0944819b…`)

Reasons:
- source replay success 19% < 80% (necessary, not sufficient)
- held-out success 3% < 75%
- transfer success 17% < 75%
- 95% lower bound on held-out+transfer success 5% < 50%
- 19 regressions vs baseline > allowed 1

## Reproducibility metadata

- DISCOVERY: `connection-debug-sim` v3.0.0 hash `8d480d64c35a…`
- HIDDEN_TRANSFER: `connection-debug-sim` v3.0.0-private-x3 hash `1ca9b77e7d15…`
- VALIDATION: `connection-debug-sim` v3.0.0-private-x3 hash `bed166c4332f…`
- Model: `vertex-ai:global` / `google/gemini-2.5-flash`; seeds [0, 1]
- Runtime `baseline-runtime/0.1.0`, verifier `sim-verifier/1`, environment `sim-env/1`, evaluator `evaluator/1`

## Results

| Suite | Arm | Runs | Verified | Success (95% Wilson CI) | Tokens / success | Tool calls / success | Mean latency ms | Notes |
|---|---|---|---|---|---|---|---|---|
| SOURCE_REPLAY | A_BASELINE | 16 (8 tasks) | 7 | 44% (23%-67%) | 12264 | 9.7 | 25417 |  |
| SOURCE_REPLAY | C_CAPABILITY | 16 (8 tasks) | 3 | 19% (7%-43%) | 10313 | 21.3 | 9491 | applicable 16/16 |
| HELD_OUT | A_BASELINE | 36 (18 tasks) | 4 | 11% (4%-25%) | 44338 | 36.5 | 21918 |  |
| HELD_OUT | C_CAPABILITY | 36 (18 tasks) | 1 | 3% (0%-14%) | 77500 | 144.0 | 10448 | applicable 36/36 |
| TRANSFER | A_BASELINE | 36 (18 tasks) | 10 | 28% (16%-44%) | 19134 | 15.6 | 21445 |  |
| TRANSFER | C_CAPABILITY | 36 (18 tasks) | 6 | 17% (8%-32%) | 11554 | 23.8 | 9852 | applicable 36/36 |
| NEGATIVE (correct refusals) | C_CAPABILITY | 8 | 8 | 100% (0%-32%) | n/a | n/a | 8 | applicable 0/8 |
| REGRESSION | C_CAPABILITY | 21 | 2 | 10% (3%-29%) | 22202 | 42.0 | 10444 | applicable 21/21 |

- False applicability: 0%; regressions vs baseline: 19; 95% lower bound on held-out+transfer success: 5%

## Lifecycle cost (includes learning and evaluation overhead)

- Learning: 0 tokens, 3005 ms wall time
- Evaluation overhead (control arms A+B): 454536 tokens
- Capability execution: 177764 tokens for 10 verified successes
- **Lifecycle tokens per verified success (C): 63230** vs baseline execution only: 21645

## Notes

- Source replay is necessary but never sufficient; held-out, transfer and negative-applicability evidence is required for a PASS.
- Success is decided by the independent verifier from final environment state for every arm.
- Lifecycle cost charges the capability for the control-arm tokens spent evaluating it; amortization over future use is not credited here.
- Small samples: treat differences within the confidence intervals as unproven.
- With several seeds every task is re-run once per seed. Those runs are correlated (same task), so the Wilson intervals are optimistic; sample-size gates count distinct tasks, not runs.
