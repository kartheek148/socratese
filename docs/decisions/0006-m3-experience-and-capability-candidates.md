# ADR 0006 — Milestone 3 decisions: experience, capability candidates, dataset protection

Status: Accepted (Milestone 3)

## Decisions
1. **Deterministic, rule-based compiler first.** No model is called in M3, so candidate generation is reproducible and comparable across compiler versions (roadmap acceptance). An LLM-assisted proposer is deferred behind the same validator; it would record prompt/model versions in `provenance`.
2. **Capabilities are data.** The schema lives in `domain/` (pure), semantic validation in `capabilities/`. Every field is typed; expressions are structured references; free-form prompt text and code are not representable. `LLM_DECISION` references an approved template id.
3. **Learn only from discovery.** Run provenance (`task_runs.tags`, migration 0002) records dataset name/version/split. `classify_eligibility` and a database CHECK (`eligible_requires_discovery`, written with `IS TRUE` because a NULL split would otherwise pass) make non-discovery, unverified (`UNKNOWN`/`PARTIAL`) or unknown-provenance runs ineligible. `ee-learn` refuses to run with evaluator access; `learning/` and `capabilities/` cannot import `bench` (architecture test).
4. **Private evaluation data (resolves the ADR 0004 open item, partially).** The v1 hidden split and its seed are in git history, so v1 is declared a **development set**: valid for baselines/regression, not as a hidden evaluation. `ee-bench generate --private` writes validation/hidden splits from a secret seed (`EE_PRIVATE_DATASET_SEED`, never recorded) to a directory outside the repository, refused if inside it; `DatasetLoader(private_root=…)` / `--private-root` / `EE_PRIVATE_DATASET_ROOT` load them (still evaluator-gated). *Not solved:* families/variants are visible in code (values are not), and file permissions/host separation of the private directory is a deployment concern. Milestone 4 must run evaluation against private data only.
5. **Evidence vs new skill.** Identity = `(tenant, skeleton signature)`; `procedure_hash` decides between "add evidence" and "new immutable version". Evidence rows are append-only and unique per `(capability, version, run, role)`.
6. **Immutability in the database.** `capability_versions` allow only `state` updates (trigger); `experience_cases`, `capability_evidence`, `cost_ledger` are append-only. `capabilities` unique `(tenant, skeleton_signature)`.
7. **Learning cost is accounted.** `cost_ledger` records wall time (and tokens/tool calls, zero for the deterministic pipeline) per case build and compile (engineering gap G8).
8. **Workers, not Celery, for M3 learning jobs.** `ee-learn process-jobs` claims jobs with `SKIP LOCKED` and a lease; M2's Celery consumer still only enqueues. Chaining the two through the broker is a straightforward later change; no new infrastructure was needed.

## Consequences / deferred
Executor, evaluator, registry transitions, promotion (M4); matching and runtime use (M5). No LLM proposer. Candidate semantics (is the procedure actually good?) are unmeasured until M4.
