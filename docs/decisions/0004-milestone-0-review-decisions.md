# ADR 0004 — Milestone 0 review decisions

Status: Accepted (review of Milestone 0, 2026-11-18)

| Question | Decision |
|---|---|
| Benchmark domain for M1 | Simulated connection-pool / API-timeout debugging; no real-repository harness |
| LLM | Real model required for non-test runs, via an OpenAI-compatible adapter on stdlib HTTP (no vendor SDK). Deterministic `FakeLLM` is used only for tests and harness validation and is labelled as such in reports |
| Tenant isolation | Application-level scoping (tenant on every contract and repository method); no Postgres RLS |
| Identifiers | UUIDv7 (`domain/ids.py`) |
| Artifact store | Local filesystem adapter until S3/MinIO is needed |
| CI | GitHub Actions is **not** the target; checks run locally. No workflow file is added. Docker is available on the developer machine (needed from M2) |
| Packaging | `uv` + `pyproject.toml`, `uv.lock` committed |
| Git flow | Commit directly to `main`; no PRs |

## Deviations from the Milestone 0 plan
- `execution/analyzer.py` and `execution/context.py` are not created in M1; nothing consumes them until M5–M7.
- Hidden-split protection in M1 is an in-repo guard only. **Open item before M3:** move `HIDDEN_TRANSFER` (and preferably `VALIDATION`) out of the repository tree into storage unreadable by the learning plane. *(Addressed in M3 by ADR 0006 §4: v1 declared burned; private seeded splits outside the repo; partial — see that ADR.)*
- `RunOutcome.verification_results` is a typed `VerificationResult` list (not `list[dict]`), and `RunOutcome` gains `task_id`, `tenant_id`, `config_id`, `policy_version`, `capability_version` (gap G1).
- `TaskSpec.success_criteria` is a discriminated union with `goal`/`guard` roles (gap G1).
