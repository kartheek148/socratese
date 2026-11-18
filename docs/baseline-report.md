# Baseline report (A) - harness validation with scripted FakeLLM

Configuration **A (fixed baseline, no persistent learning, no memory, no capabilities)**.

## Reproducibility metadata

- Dataset: `connection-debug-sim` v1.0.0
  - DISCOVERY: content hash `8170dc437881ac06…` (12 tasks)
  - HIDDEN_TRANSFER: content hash `e0efa6367a1bb8b0…` (8 tasks)
  - VALIDATION: content hash `0193090c4c31461b…` (8 tasks)
- Seed: 0
- Provider / model: `fake` / `heuristic-fake/1` (temperature 0.0, seed 0, max output tokens 1024)
- Prompt version: `sys-1`; runtime `baseline-runtime/0.1.0`; verifier `sim-verifier/1`; environment `sim-env/1`; policy `fixed-general-planning/1`
- Learning cost: 0 (baseline performs no learning). Lifecycle cost equals execution cost.
- Run-record digests (deterministic fields only; IDs, timestamps and latency excluded) make two runs comparable via `ee-bench compare`.

> **Caution:** this report was produced with the deterministic scripted `FakeLLM`. It validates the harness and verifier; it says **nothing** about real-model capability. Token counts are whitespace estimates.

## Results by split

| Group | Runs | Verified | Verified success (95% Wilson CI) | Fail/Partial/Unknown | False claims | Tokens / verified success | Tool calls / verified success | Mean latency ms | Human interventions |
|---|---|---|---|---|---|---|---|---|---|
| DISCOVERY | 12 | 12 | 100% (76%-100%) | 0/0/0 | 0 | 1339 | 5.0 | 5 | 0 |
| HIDDEN_TRANSFER | 8 | 0 | 0% (0%-32%) | 8/0/0 | 8 | n/a | n/a | 4 | 0 |
| VALIDATION | 8 | 8 | 100% (68%-100%) | 0/0/0 | 0 | 1334 | 5.0 | 5 | 0 |

## Results by split and variant

| Group | Runs | Verified | Verified success (95% Wilson CI) | Fail/Partial/Unknown | False claims | Tokens / verified success | Tool calls / verified success | Mean latency ms | Human interventions |
|---|---|---|---|---|---|---|---|---|---|
| DISCOVERY / httpx | 3 | 3 | 100% (44%-100%) | 0/0/0 | 0 | 1299 | 5.0 | 6 | 0 |
| DISCOVERY / psycopg_pool | 3 | 3 | 100% (44%-100%) | 0/0/0 | 0 | 1369 | 5.0 | 10 | 0 |
| DISCOVERY / requests | 3 | 3 | 100% (44%-100%) | 0/0/0 | 0 | 1283 | 5.0 | 2 | 0 |
| DISCOVERY / sqlalchemy | 3 | 3 | 100% (44%-100%) | 0/0/0 | 0 | 1405 | 5.0 | 3 | 0 |
| HIDDEN_TRANSFER / hikari | 4 | 0 | 0% (0%-49%) | 4/0/0 | 4 | n/a | n/a | 4 | 0 |
| HIDDEN_TRANSFER / okhttp | 4 | 0 | 0% (0%-49%) | 4/0/0 | 4 | n/a | n/a | 4 | 0 |
| VALIDATION / httpx | 2 | 2 | 100% (34%-100%) | 0/0/0 | 0 | 1297 | 5.0 | 6 | 0 |
| VALIDATION / psycopg_pool | 2 | 2 | 100% (34%-100%) | 0/0/0 | 0 | 1369 | 5.0 | 2 | 0 |
| VALIDATION / requests | 2 | 2 | 100% (34%-100%) | 0/0/0 | 0 | 1263 | 5.0 | 8 | 0 |
| VALIDATION / sqlalchemy | 2 | 2 | 100% (34%-100%) | 0/0/0 | 0 | 1405 | 5.0 | 2 | 0 |

## Notes and limitations

- Success is decided only by the independent verifier from final environment state; `False claims` counts runs where the agent declared completion but the verifier did not confirm.
- UNKNOWN (e.g. provider outage) is reported separately and never counted as success or failure evidence.
- Samples are small (see CIs); differences between configurations need more tasks/seeds before being treated as improvements.
- The hidden-transfer split is protected only by an application-level access guard in M1.
