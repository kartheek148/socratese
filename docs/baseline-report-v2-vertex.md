# v2 baseline, real model (private validation + hidden-transfer, 3 seeds)

Configuration **A (fixed baseline, no persistent learning, no memory, no capabilities)**.

## Reproducibility metadata

- Dataset: `connection-debug-sim` v2.0.0-private
  - HIDDEN_TRANSFER: content hash `64b40219f11b72e0…` (12 tasks)
  - VALIDATION: content hash `6cfb899289d6ebc9…` (12 tasks)
- Seeds: [0, 1, 2]
- Provider / model: `vertex-ai:global` / `google/gemini-2.5-flash` (temperature 0.0, seed 0, max output tokens 4096)
- Prompt version: `sys-1`; runtime `baseline-runtime/0.1.0`; verifier `sim-verifier/1`; environment `sim-env/1`; policy `fixed-general-planning/1`
- Learning cost: 0 (this run performs no learning). Lifecycle cost equals execution cost; the cost of producing any capability used is reported separately.
- Run-record digests (deterministic fields only; IDs, timestamps and latency excluded) make two runs comparable via `ee-bench compare`.

## Results by split

| Group | Runs | Verified | Verified success (95% Wilson CI) | Fail/Partial/Unknown | False claims | Tokens / verified success | Tool calls / verified success | Mean latency ms | Human interventions |
|---|---|---|---|---|---|---|---|---|---|
| HIDDEN_TRANSFER | 36 | 10 | 28% (16%-44%) | 26/0/0 | 26 | 19942 | 15.2 | 30866 | 0 |
| VALIDATION | 36 | 16 | 44% (30%-60%) | 20/0/0 | 20 | 16560 | 9.9 | 38859 | 0 |

## Results by split and variant

| Group | Runs | Verified | Verified success (95% Wilson CI) | Fail/Partial/Unknown | False claims | Tokens / verified success | Tool calls / verified success | Mean latency ms | Human interventions |
|---|---|---|---|---|---|---|---|---|---|
| HIDDEN_TRANSFER / hikari_shared | 18 | 7 | 39% (20%-61%) | 11/0/0 | 11 | 14905 | 11.4 | 31175 | 0 |
| HIDDEN_TRANSFER / okhttp_budget | 18 | 3 | 17% (6%-39%) | 15/0/0 | 15 | 31695 | 24.0 | 30557 | 0 |
| VALIDATION / httpx_budget | 9 | 7 | 78% (45%-94%) | 2/0/0 | 2 | 9495 | 5.7 | 39902 | 0 |
| VALIDATION / psycopg_shared | 9 | 2 | 22% (6%-55%) | 7/0/0 | 7 | 22461 | 19.0 | 28479 | 0 |
| VALIDATION / requests_budget | 9 | 4 | 44% (19%-73%) | 5/0/0 | 5 | 23315 | 10.2 | 52259 | 0 |
| VALIDATION / sqlalchemy_shared | 9 | 3 | 33% (12%-65%) | 6/0/0 | 6 | 20102 | 13.3 | 34794 | 0 |

## Notes and limitations

- Success is decided only by the independent verifier from final environment state; `False claims` counts runs where the agent declared completion but the verifier did not confirm.
- UNKNOWN (e.g. provider outage) is reported separately and never counted as success or failure evidence.
- Samples are small (see CIs); differences between configurations need more tasks/seeds before being treated as improvements.
- The hidden-transfer split is protected only by an application-level access guard in M1.
