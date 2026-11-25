# Design: governed memory (M5.8) and gated memory-vs-capability evaluation

Written before implementation. Scope: memory artifacts become versioned, validated, promotable and
roll-back-able assets under the **same** lifecycle, policy and statistics as capabilities. No
controller changes (that is M6). Promotion thresholds (`default-low`) are not modified.

## 1. Audit summary (what exists, what is reused)
| Area | Today | Decision |
|---|---|---|
| Lifecycle states + legal graph | `CapabilityState`, `ALLOWED_TRANSITIONS` (domain/capability.py); DB trigger enforces it | **reuse** the states and graph for memory (alias `AssetState`) |
| Promotion service | `capabilities/registry.RegistryService`: evaluation -> APPROVED/REJECTED, shadow/canary gates, activate, rollback restoring previous | **extract the logic into a generic `GovernedRegistry[D]`** (new pure package `governance/`); `RegistryService` becomes a thin capability subclass (same API, same behaviour); memory gets `MemoryRegistryService` |
| Registry port | `RegistryStore` with `VersionRecord` (capability-typed) | make `VersionRecord[D]` / `RegistryStore[D]` generic; the id parameter keeps the name `capability_id` and is documented as "asset id" (naming debt, avoids touching capability call sites) |
| SQL store | `SqlRegistryStore` hard-wired to capability tables | parameterise with an `AssetSpec` (tables, id column, decoder, audit label); capability behaviour unchanged (existing tests) |
| Policy / verdict | `default-low` etc.; `decide_verdict` reads the `C_CAPABILITY` arm | add an optional `treatment` arm argument (default unchanged); no threshold changes |
| Evaluation engine | `evaluate(plan)` with arms A/B/C, suites, negatives, regression, lifecycle | add `treatment: Arm = Arm.CAPABILITY`; memory evaluation passes `Arm.MEMORY_ASSET` |
| Memory | `memory/` pure builders + `ExperienceMemory` (ungated, always injects, nearest-family fallback for unseen families) | keep for the legacy experiment; add typed **MemoryArtifact** + applicability + gated selection |
| DB | capability_* tables, no memory persistence | **one migration (0004)**: `memories`, `memory_versions`, `memory_pointers`, `memory_transitions`, `memory_evaluations`, `memory_observations`, with the same triggers/constraints; capability tables untouched |
| Catalog (production) | `SqlExecutableCatalog` ACTIVE-only | add `SqlActiveMemoryCatalog` (ACTIVE-only, tenant-scoped, explicit experimental override) |
| Experiment | `ee-exp` legacy arms A-E | add `--mode gated` (arms A-F reading ACTIVE assets from the DB); legacy mode unchanged |

## 2. Memory artifact (domain/memory.py, Pydantic, separate from capabilities)
`MemoryDefinition` (immutable body of one version): `memory_id` (stable per (type, family)), `version`,
`memory_type` EPISODIC | CONSOLIDATED | PROCEDURAL, `task_family`, `content` (rendered text that is
injected; for EPISODIC it is rendered from the bank), `episodes` (EPISODIC only: a versioned
**retrieval bank** of normalised episode entries + `RetrievalSpec`; raw trajectories stay in the artifact
store and are referenced by `source_experience_ids`/`evidence_refs`), typed `applicability_conditions`
(the *same* precondition classes capabilities use) and `exclusion_conditions` (memory is withheld if any
holds), `failure_conditions`, `known_limitations`, `source_capability_refs` (PROCEDURAL), `provenance`
(builder version, pipeline version), `content_hash` (sha256 of the canonical body; verified on load),
`created_at`. Lifecycle fields live in the DB version row (state, evaluations, promotion metadata via
`memory_transitions`/`memory_evaluations`, pointers for active/previous), not in the immutable body.
**Applicability is never similarity**: `memory/applicability.py::select_memory` checks typed conditions
(family, tools, file roles, risk) and exclusions; similarity may only rank among applicable artifacts of
the same type. An artifact with no family boundary is representable (so it can be *tested and rejected*)
but cannot pass the false-applicability gate.

## 3. Gating memory (same policy, memory-specific measurement)
Treatment arm `M_MEMORY_ASSET` = baseline agent + the artifact's text, injected only when its
applicability check passes; otherwise the task is refused (negative suite) / falls back to planning
(production). Suites and gates are the existing ones: SOURCE_REPLAY (necessary only), HELD_OUT,
TRANSFER (cross-variant), NEGATIVE = other-family tasks + synthetic violations where the artifact must
**not** apply (false applicability = injected anyway), REGRESSION (tasks the baseline solved). Mismatches
documented rather than patched: (a) `min_negative_n` etc. are task counts and apply unchanged;
(b) `max_exec_token_ratio` is None for `low`, so memory's token overhead is reported, not gated;
(c) cross-family transfer is *reported* (not gated) for both asset types because both are family-scoped by
declaration; (d) "negative transfer" is the regression count plus the cross-family comparison against the
ungated memory of the previous experiment. Proposed (not applied) extension for review: a per-asset
token-ratio gate for memory.
Baseline outcomes are computed once per (task, seed) and shared by all assets evaluated in one `ee-gov`
invocation (paired, and cheaper); each asset is still *charged* the control tokens it needs (see 5).

## 4. Governance interface
`governance/` (pure): `AssetKind` (CAPABILITY|MEMORY), generic `GovernedRegistry[D]` (all transitions,
policy hashing, shadow/canary gates, activation with previous-version retention, rollback), exceptions.
Lifecycle `DISCOVERED -> CANDIDATE -> VALIDATING -> APPROVED -> SHADOW -> CANARY -> ACTIVE`, plus
REJECTED / DEPRECATED / ROLLED_BACK, enforced in code **and** by the memory version trigger.
CLIs: `ee-learn build-memory` (learning plane, refuses evaluator access, registers CANDIDATE versions,
idempotent on content hash, new version on changed content); `ee-gov` (evaluator plane): `evaluate`,
`observe`, `promote`, `rollback`, `status` for memory (and `status` for both kinds). `ee-eval` keeps
governing capabilities unchanged.

## 5. Production mode and the gated benchmark
`memory/runtime.py::select_memory(catalog, tenant, memory_type, facts)` consults only `list_active`
(ACTIVE rows) and returns an artifact or a `fallback_reason`. `ee-exp --mode gated` arms:
A stateless; B ACTIVE episodic only; C ACTIVE consolidated only; D ACTIVE procedural only; E ACTIVE
capability only; F noise control (production path with an empty registry -> planning, reason
`no active asset`). Every row records `asset_ref` (`id@version`), `asset_kind`, `fallback_reason`.
Candidates/validating/rejected assets are unreachable by construction (catalog filter) and by test.
The experimental override needs `--experimental-states` and tags the report.

## 6. Data and statistics
Fresh public discovery seed (4 repetitions) -> learn -> build memory -> compile. New private seed **P1**
(`--scale 3`, 2 seeds) for validation of every asset; a different private seed **P2** (`--scale 5`: 60
tasks for pool and timeout, 30 for queue; budget-limited and labelled) for the final gated comparison,
never used for any decision. Existing task-level framework: per-task means, paired bootstrap, exact
McNemar, Holm, winner named only if both agree; per-family/variant/level, regressions, retrieval and
fallback rates.

## 7. Economics
Ledger per asset: discovery tokens (shared), experience processing, memory consolidation, retrieval-bank
construction and capability compilation (deterministic: 0 model tokens, wall time reported), **validation
tokens read from the stored evaluation of that exact asset** (treatment + control tokens), runtime
retrieval (tokens inside prompts + retrieval ms). Views: *as built* (each pays its recorded costs) and
*equal governance* (every learned arm pays the mean validation cost per asset). Maintenance is an explicit
**assumption**: `revalidations` x validation cost, shown for 0/1/3 (never silently zero). Break-even per
verified success and per task; sensitivity to reuse volume (N = 100 / 1k / 10k); wall-clock.

## 8. Files
New: `domain/memory.py`, `governance/{__init__,registry}.py`, `memory/{applicability,runtime,service}.py`,
`ports/memory.py`, `adapters/db/{memory_registry_store,memory_catalog,asset_spec}.py`,
`evaluation/memory_eval.py`, `gov_cli.py`, `alembic/versions/0004_memory_governance.py`,
tests (domain, applicability, runtime isolation, registry lifecycle/rollback/tenancy, migration,
evaluation, gated experiment, economics), ADR 0012, report.
Changed (behaviour-preserving): `capabilities/registry.py`, `ports/registry.py`, `adapters/db/registry_store.py`,
`domain/evaluation.py`, `evaluation/{policy,engine,arms,paired,economics,experiment*}.py`,
`experiment_cli.py`, `learn_cli.py`, `capabilities/applicability.py`, `tables.py`, `conftest.py`.
