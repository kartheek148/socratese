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

### Real model: Vertex AI (Gemini) with a service-account key

```
export EE_GCP_CREDENTIALS_FILE=/path/to/service-account.json   # git-ignored; never commit it
export EE_VERTEX_LOCATION=global                               # or e.g. us-central1
export EE_LLM_MODEL=gemini-2.5-flash                           # sent as google/gemini-2.5-flash
export EE_EVALUATOR_ROLE=evaluator
uv run ee-bench run --llm vertex --split all --seed 0 --out runs/vertex-baseline-seed0.jsonl
uv run ee-bench report --runs runs/vertex-baseline-seed0.jsonl --out docs/baseline-report-vertex.md --title "..."
```

Uses Vertex AI's OpenAI-compatible endpoint. The key file is read only to mint short-lived OAuth tokens (`google-auth`, refreshed per request attempt); it is never logged, copied or put in reports (reports show only `vertex-ai:<location>`). Project id comes from the key file unless `EE_GCP_PROJECT` is set. Gemini "thinking" tokens are billed but excluded from `completion_tokens`, so token usage is taken from `total_tokens`; they also count against `max_tokens`, hence the 4096 default (`EE_LLM_MAX_OUTPUT_TOKENS` overrides). Note some models (e.g. `gemini-2.0-flash`) return 404 for this project.

### Real model: any OpenAI-compatible endpoint

Standard-library HTTP; no SDK dependency:

```
export EE_LLM_API_KEY=...        # never committed or printed
export EE_LLM_MODEL=<model-name>
export EE_LLM_BASE_URL=https://api.openai.com/v1   # optional
export EE_LLM_TEMPERATURE=0                          # optional
uv run ee-bench run --llm openai --seed 0 --out runs/real-seed0.jsonl
```

Run-output directory `runs/` is git-ignored. Provider failures retry twice with backoff, then end the run as `LLM_ERROR` → `UNKNOWN`. Providers are not bit-deterministic even at temperature 0; run several seeds and compare outcome rates with the Wilson intervals in the report rather than expecting identical digests.

## Protecting validation and hidden-transfer data

The committed `benchmarks/v1` is a **development set**: all its splits and its seed are in git history, so its hidden split is burned for evaluation purposes (fine for baselines and regression). For real validation/transfer evaluation generate private splits from a secret seed into a directory *outside* the repository and point runs at it:

```
export EE_PRIVATE_DATASET_SEED=<secret integer, not the public seed>
uv run ee-bench generate --private --root /secure/path/ee-private      # refuses repo paths / missing seed
export EE_EVALUATOR_ROLE=evaluator EE_PRIVATE_DATASET_ROOT=/secure/path/ee-private
uv run ee-bench run --llm vertex --split all --out runs/private-eval.jsonl
```

The seed is never written to the manifest; private manifests are versioned `…-private`. Learning-plane code (`learning/`, `capabilities/`, `ee-learn`) cannot import the benchmark package and refuses to run with evaluator access. Remaining gap: the task *families/variants* are visible in code (only their numeric values are secret), and OS-level separation of the private directory is a deployment concern.

## What is recorded per run (`BenchmarkRunRecord`)

Dataset name/version/split/content hash, seed, agent config (prompt version, provider, model, temperature, seed), runtime/verifier/environment/policy versions, the controller decision (eligible set, selection, probability, reason, back-filled verified outcome), verification results with detail, tokens, tool calls, latency, human interventions, termination reason, whether the agent claimed completion, and bounded step summaries (size + sha256, never raw payloads). `digest()` hashes all deterministic fields (excludes IDs, timestamps, latency).

## Status of the committed baseline reports

- [baseline-report.md](./baseline-report.md): scripted `FakeLLM` — proves the harness, verifier and reproducibility; **not** a measurement of any model. (The fake agent solves discovery/validation and fails hidden-transfer while falsely claiming success, which the verifier catches.)
- [baseline-report-vertex.md](./baseline-report-vertex.md): **real model**, `google/gemini-2.5-flash` on Vertex AI (global), temperature 0, seed 0, one run: **24/28 verified** (discovery 10/12, validation 6/8, hidden-transfer 8/8). All 4 failures are `psycopg_pool` tasks where the model under-sized the pool and then claimed success (4 false claims, all caught by the verifier).

Read these results with care:
- **Single seed, tiny samples** (Wilson intervals are wide). Even at temperature 0 the model is not deterministic: two discovery runs failed on different tasks (`-00`/`-02` in a smoke run, `-01`/`-02` in the committed run). Multiple seeds are required before any configuration comparison.
- **Limited headroom.** The baseline already passes 86% overall and 100% of hidden-transfer, so this benchmark cannot yet demonstrate large improvements from learning. Before any claim of learning-driven improvement it needs harder task variants and more tasks per family (risk R3 in the fit/gap report).
- The hidden-transfer split was run by the evaluator role, as designed; it must still move out of the repository before M3 (ADR 0004).
