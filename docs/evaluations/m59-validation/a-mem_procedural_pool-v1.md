# Evaluation: mem_procedural_pool v1

**Verdict: FAIL** (risk class `low`, policy `default-low` hash `7b6f0944819b…`)

Reasons:
- source replay success 12% < 80% (necessary, not sufficient)
- held-out success 19% < 75%
- transfer success 6% < 75%
- 95% lower bound on held-out+transfer success 7% < 50%
- 20 regressions vs baseline > allowed 1

## Reproducibility metadata

- DISCOVERY: `connection-debug-sim` v3.0.0 hash `8d480d64c35a…`
- HIDDEN_TRANSFER: `connection-debug-sim` v3.0.0-private-x3 hash `1ca9b77e7d15…`
- VALIDATION: `connection-debug-sim` v3.0.0-private-x3 hash `bed166c4332f…`
- Model: `vertex-ai:global` / `google/gemini-2.5-flash`; seeds [0, 1]
- Runtime `baseline-runtime/0.1.0`, verifier `sim-verifier/1`, environment `sim-env/1`, evaluator `evaluator/1`

## Results

| Suite | Arm | Runs | Verified | Success (95% Wilson CI) | Tokens / success | Tool calls / success | Mean latency ms | Notes |
|---|---|---|---|---|---|---|---|---|
| SOURCE_REPLAY | A_BASELINE | 16 (8 tasks) | 5 | 31% (14%-56%) | 17083 | 13.4 | 28854 |  |
| SOURCE_REPLAY | M_MEMORY_ASSET | 16 (8 tasks) | 2 | 12% (3%-36%) | 53026 | 32.0 | 20441 | applicable 16/16 |
| HELD_OUT | A_BASELINE | 36 (18 tasks) | 5 | 14% (6%-29%) | 36434 | 29.6 | 22111 |  |
| HELD_OUT | M_MEMORY_ASSET | 36 (18 tasks) | 7 | 19% (10%-35%) | 30129 | 20.7 | 18522 | applicable 36/36 |
| TRANSFER | A_BASELINE | 36 (18 tasks) | 12 | 33% (20%-50%) | 16032 | 13.0 | 21441 |  |
| TRANSFER | M_MEMORY_ASSET | 36 (18 tasks) | 2 | 6% (2%-18%) | 110660 | 72.0 | 19942 | applicable 36/36 |
| NEGATIVE (correct refusals) | M_MEMORY_ASSET | 8 | 8 | 100% (0%-32%) | n/a | n/a | 0 | applicable 0/8 |
| REGRESSION | M_MEMORY_ASSET | 22 | 2 | 9% (3%-28%) | 62358 | 44.5 | 20090 | applicable 22/22 |

- False applicability: 0%; regressions vs baseline: 20; 95% lower bound on held-out+transfer success: 7%

## Lifecycle cost (includes learning and evaluation overhead)

- Learning: 0 tokens, 0 ms wall time
- Evaluation overhead (control arms A+B): 459969 tokens
- Capability execution: 538279 tokens for 11 verified successes
- **Lifecycle tokens per verified success (C): 90750** vs baseline execution only: 20908

## Notes

- Source replay is necessary but never sufficient; held-out, transfer and negative-applicability evidence is required for a PASS.
- Success is decided by the independent verifier from final environment state for every arm.
- Lifecycle cost charges the capability for the control-arm tokens spent evaluating it; amortization over future use is not credited here.
- Small samples: treat differences within the confidence intervals as unproven.
- With several seeds every task is re-run once per seed. Those runs are correlated (same task), so the Wilson intervals are optimistic; sample-size gates count distinct tasks, not runs.
