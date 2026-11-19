# Benchmark and baseline (Milestone 1)

Configuration **A**: fixed agent, no persistent learning, no memory, no capabilities. Every run uses a fresh session (new simulated environment, new LLM client, no carried state).

## Task families (`connection-debug-sim` v1.0.0, generator `gen/1`, seed 20260101)

Simulated, fully deterministic debugging tasks over an in-memory file tree (`bench/environment.py`); no real filesystem, network or process access.

| Family | Symptom | Fix | Guards (must stay unchanged) |
|---|---|---|---|
| `pool` | connection-pool exhaustion | configured pool capacity must cover `workers × threads` and stay ≤ `max_connections − reserved` | `infra/db.json`, concurrency settings |
| `timeout` | upstream client timeouts | read timeout must cover the worst observed latency and stay ≤ gateway max | `infra/gateway.json`, retry settings |

| Split | Tasks | Variants | Purpose |
|---|---|---|---|
| `DISCOVERY` | 12 | sqlalchemy, psycopg_pool, httpx, requests | the only split learning components may read |
| `VALIDATION` | 8 | same variants, new numbers | held-out validation |
| `HIDDEN_TRANSFER` | 8 | hikari, okhttp (new libraries, layouts, key names/units) | transfer evaluation |

Files: `benchmarks/v1/<split>/<task_id>.json` + `manifest.json` (per-file and per-split sha256). `DatasetLoader` verifies hashes on every load; tests assert the committed files equal the generator output.

**Access guard.** `DISCOVERY` is loadable anonymously. `VALIDATION`/`HIDDEN_TRANSFER` need an `EvaluatorAccess` (`EE_EVALUATOR_ROLE=evaluator`). This is an application-level guard, **not** a security boundary — all splits are in this repository. **Before Milestone 3 the hidden split must move to storage the learning plane cannot read** (tracked in ADR 0004).

## Verification

`SimulationVerifier` (`bench/verifier.py`) sees only the final environment snapshot and the task's criteria; the agent's messages and final claim are not inputs. Criteria carry a role: *goal* or *guard*. Status: any unevaluable criterion → `UNKNOWN`; a failed guard → `FAILURE`; otherwise goals decide (`VERIFIED_SUCCESS` / `PARTIAL` / `FAILURE`). A provider outage (`LLM_ERROR`) that does not end in verified success is recorded as `UNKNOWN`, never as a failure. Success criteria are never shown to the agent.

## Commands

```
uv sync
uv run ee-bench generate                                  # regenerate benchmarks/v1 (deterministic)
export EE_EVALUATOR_ROLE=evaluator                        # required to read validation/hidden splits
uv run ee-bench run --llm fake --out runs/a.jsonl         # scripted FakeLLM (harness validation)
uv run ee-bench report --runs runs/a.jsonl --out docs/baseline-report.md
uv run ee-bench compare runs/a.jsonl runs/b.jsonl         # exit 0 only if every digest matches
uv run ee-bench run --llm fake --persist --out runs/p.jsonl   # M2: also persist traces/outcomes/outbox (see events.md)
```

### Real model

Any OpenAI-compatible `/chat/completions` endpoint (standard-library HTTP; no SDK dependency):

```
export EE_LLM_API_KEY=...        # never committed or printed
export EE_LLM_MODEL=<model-name>
export EE_LLM_BASE_URL=https://api.openai.com/v1   # optional
export EE_LLM_TEMPERATURE=0                          # optional
uv run ee-bench run --llm openai --seed 0 --out runs/real-seed0.jsonl
```

Run-output directory `runs/` is git-ignored. Provider failures retry twice with backoff, then end the run as `LLM_ERROR` → `UNKNOWN`. Providers are not bit-deterministic even at temperature 0; run several seeds and compare outcome rates with the Wilson intervals in the report rather than expecting identical digests.

## What is recorded per run (`BenchmarkRunRecord`)

Dataset name/version/split/content hash, seed, agent config (prompt version, provider, model, temperature, seed), runtime/verifier/environment/policy versions, the controller decision (eligible set, selection, probability, reason, back-filled verified outcome), verification results with detail, tokens, tool calls, latency, human interventions, termination reason, whether the agent claimed completion, and bounded step summaries (size + sha256, never raw payloads). `digest()` hashes all deterministic fields (excludes IDs, timestamps, latency).

## Status of the committed baseline report

[baseline-report.md](./baseline-report.md) was produced with the scripted `FakeLLM`. It proves the harness, verifier, and reproducibility; it is **not** a measurement of any real model. The fake agent solves every discovery/validation task and fails all hidden-transfer tasks while falsely claiming success — which is exactly what the verifier is meant to catch. A real-model baseline has not been run: no model credentials were available in the development environment. Run the commands above and commit the resulting report to complete that acceptance item.
