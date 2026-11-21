# Baseline report (A) - gemini-2.5-flash on Vertex AI, seed 0

Configuration **A (fixed baseline, no persistent learning, no memory, no capabilities)**.

## Reproducibility metadata

- Dataset: `connection-debug-sim` v1.0.0
  - DISCOVERY: content hash `8170dc437881ac06…` (12 tasks)
  - HIDDEN_TRANSFER: content hash `e0efa6367a1bb8b0…` (8 tasks)
  - VALIDATION: content hash `0193090c4c31461b…` (8 tasks)
- Seeds: [0]
- Provider / model: `vertex-ai:global` / `google/gemini-2.5-flash` (temperature 0.0, seed 0, max output tokens 4096)
- Prompt version: `sys-1`; runtime `baseline-runtime/0.1.0`; verifier `sim-verifier/1`; environment `sim-env/1`; policy `fixed-general-planning/1`
- Learning cost: 0 (baseline performs no learning). Lifecycle cost equals execution cost.
- Run-record digests (deterministic fields only; IDs, timestamps and latency excluded) make two runs comparable via `ee-bench compare`.

## Results by split

| Group | Runs | Verified | Verified success (95% Wilson CI) | Fail/Partial/Unknown | False claims | Tokens / verified success | Tool calls / verified success | Mean latency ms | Human interventions |
|---|---|---|---|---|---|---|---|---|---|
| DISCOVERY | 12 | 10 | 83% (55%-95%) | 2/0/0 | 2 | 6565 | 6.0 | 23713 | 0 |
| HIDDEN_TRANSFER | 8 | 8 | 100% (68%-100%) | 0/0/0 | 0 | 4372 | 4.4 | 21130 | 0 |
| VALIDATION | 8 | 6 | 75% (41%-93%) | 2/0/0 | 2 | 5908 | 5.5 | 20638 | 0 |

## Results by split and variant

| Group | Runs | Verified | Verified success (95% Wilson CI) | Fail/Partial/Unknown | False claims | Tokens / verified success | Tool calls / verified success | Mean latency ms | Human interventions |
|---|---|---|---|---|---|---|---|---|---|
| DISCOVERY / httpx | 3 | 3 | 100% (44%-100%) | 0/0/0 | 0 | 4544 | 4.7 | 18190 | 0 |
| DISCOVERY / psycopg_pool | 3 | 1 | 33% (6%-79%) | 2/0/0 | 2 | 17274 | 15.0 | 27046 | 0 |
| DISCOVERY / requests | 3 | 3 | 100% (44%-100%) | 0/0/0 | 0 | 4430 | 4.7 | 21514 | 0 |
| DISCOVERY / sqlalchemy | 3 | 3 | 100% (44%-100%) | 0/0/0 | 0 | 7152 | 5.7 | 28101 | 0 |
| HIDDEN_TRANSFER / hikari | 4 | 4 | 100% (51%-100%) | 0/0/0 | 0 | 5063 | 4.5 | 25116 | 0 |
| HIDDEN_TRANSFER / okhttp | 4 | 4 | 100% (51%-100%) | 0/0/0 | 0 | 3682 | 4.2 | 17144 | 0 |
| VALIDATION / httpx | 2 | 2 | 100% (34%-100%) | 0/0/0 | 0 | 4100 | 4.5 | 18888 | 0 |
| VALIDATION / psycopg_pool | 2 | 0 | 0% (0%-66%) | 2/0/0 | 2 | n/a | n/a | 21547 | 0 |
| VALIDATION / requests | 2 | 2 | 100% (34%-100%) | 0/0/0 | 0 | 3678 | 4.5 | 19500 | 0 |
| VALIDATION / sqlalchemy | 2 | 2 | 100% (34%-100%) | 0/0/0 | 0 | 4778 | 3.5 | 22617 | 0 |

## Notes and limitations

- Success is decided only by the independent verifier from final environment state; `False claims` counts runs where the agent declared completion but the verifier did not confirm.
- UNKNOWN (e.g. provider outage) is reported separately and never counted as success or failure evidence.
- Samples are small (see CIs); differences between configurations need more tasks/seeds before being treated as improvements.
- The hidden-transfer split is protected only by an application-level access guard in M1.
