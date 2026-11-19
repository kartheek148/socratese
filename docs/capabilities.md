# Capabilities, DSL and the candidate compiler (Milestone 3)

A **capability** is an immutable, versioned, *declarative* procedure. In M3 capabilities are only ever **candidates**: they are generated, validated and stored, and nothing in the execution path can load them. Evaluation, promotion and execution are milestones 4–5.

Code: [`domain/capability.py`](../src/experience_engine/domain/capability.py) (schema), [`capabilities/validator.py`](../src/experience_engine/capabilities/validator.py) and [`capabilities/tools.py`](../src/experience_engine/capabilities/tools.py) (static validation, approved tools), [`learning/`](../src/experience_engine/learning) (normalizer, contrast, compiler, pipeline).

## Restricted DSL

Allowed steps (`op`): `TOOL_CALL`, `CONDITION`, `LLM_DECISION`, `ASSERT`, `SUBCAPABILITY`, `RETRY`, `VERIFY`, `HUMAN_APPROVAL`. There is no code, shell, SQL, import or arithmetic. Values are **structured expressions**, never strings to evaluate: `{"literal": v}`, `{"input": name}`, `{"var": "bound.field"}`, `{"select_path": {"from_var": "files", "role": "config"}}`. File paths are chosen by deterministic *role* classification (`log` / `infra` / `config`, `classify_path`), so a procedure is not tied to one repository layout. Variable choices that need reasoning (e.g. which key to change) are delegated to an `LLM_DECISION`: an **approved template id** (templates live in code, not in the definition), declared inputs, a flat typed output schema, and a token cap.

A definition carries: `id`, `version`, `objective`, typed `inputs_schema`/`outputs_schema`, `preconditions` (must include `task_family_is` and `tools_available`; optionally `file_role_present`, `risk_level_at_most`), `steps`, `termination`, `failure_conditions`, `recovery` (always falls back to general planning or human escalation), `dependencies`, `permissions` (must cover every tool used), `budgets`, `known_limitations`, and `provenance` (source run ids, counterexample run ids, compiler/prompt/model/pipeline/dataset versions; no timestamps so hashes are stable).

## Static validation (`validate_definition`)

The only entry point for untrusted definitions. It rejects, with every problem listed: schema violations (unknown ops/fields, bad identifiers, out-of-range budgets); missing preconditions; unapproved tools, wrong tool arguments, unapproved templates/verifiers; variables or inputs used before binding; **any string value containing code-, shell-, SQL- or import-like content**; undeclared/self dependencies; duplicate step ids, excessive nesting (>3) or size (>50 steps); `budgets.max_steps` below the step count; permissions not covering the tools used. The compiler validates its own output and drops (reports) anything that fails.

> Limit: the string scan is a deny-list backstop on top of the structural allow-list; the allow-list (typed fields, approved tools/templates) is what actually prevents arbitrary execution. The executor (M4) must still interpret definitions only through the allow-list.

## Lifecycle

`DISCOVERED → CANDIDATE → VALIDATING → APPROVED → SHADOW → CANARY → ACTIVE`, with `REJECTED`, `DEPRECATED`, `ROLLED_BACK` as end states (`ALLOWED_TRANSITIONS`). M3 only creates `CANDIDATE` rows. `EXECUTABLE_STATES = {ACTIVE}`; the only catalog normal execution may use is `ExecutableCapabilityCatalog.list_executable`, which returns ACTIVE versions only. The registry service that performs transitions is milestone 4; the database already forbids changing anything but `state` on a version.

## Experience pipeline

1. **Case building** (`ExperiencePipeline.process_run`, driven by `ee-learn process-jobs` over `learning_jobs`): reconstruct the run from persisted events + artifacts, normalize it to a `Trajectory` (steps abstracted to tokens like `read_file:config`, no raw paths/values; observed file roles; config *shape* fingerprint = hash of top-level key names; value types), store it as an artifact, and write an append-only `experience_cases` row. Idempotent per `(tenant, run, pipeline_version)`. Cost (wall time; tokens 0) goes to `cost_ledger`.
2. **Eligibility** (`classify_eligibility`, enforced again by a DB CHECK): a case is eligible only if the run completed, its provenance tag says `dataset_split = DISCOVERY`, and its outcome is `VERIFIED_SUCCESS` or `FAILURE`. `UNKNOWN`/`PARTIAL`, `VALIDATION`, `HIDDEN_TRANSFER`, missing provenance and unreadable traces are recorded with an exclusion reason and never reach the compiler.
3. **Contrast** (`learning/contrast.py`): per step token, support among verified successes vs failures → `CRITICAL` / `AVOIDABLE` / `NEUTRAL`. These are associations over small samples, reported as *hypotheses* in `known_limitations`; with no failures nothing is claimed.
4. **Compile** (`learning/compiler.py`, `rule-compiler/1`, deterministic, no model call): per task family with ≥ `min_support` (3) verified successes, keep tokens present in ≥ 60% of successes ordered by mean first position; abstract paths to roles; insert the bounded `LLM_DECISION` before each mutating call; end with `VERIFY`; derive preconditions, budgets (1.5× observed p90 tokens/tool calls, timeout fixed 60 s), limitations, and **data-only tests** (source replay, counterexamples, negative applicability incl. other families). Rejected with a reason when: too little support, no mutating step, unapproved tool in the skeleton, or validation fails.
5. **Register** (`SqlCapabilityStore.register_candidate`), per tenant, serialized per capability:
   * unseen skeleton → **new capability**, version 1;
   * same capability, same `procedure_hash` → **evidence added** (new `capability_evidence` rows) or no change — *a new run does not create a new skill*;
   * same capability, different procedure (e.g. new compiler version, changed budgets) → **new immutable version** `n+1`; older versions stay.

`procedure_hash` ignores `version`, the source/counterexample run-id lists and descriptive limitations, so added evidence never looks like a new procedure. `capability id = <family>.<first 10 hex of skeleton signature>`.

## Determinism and versions

Same eligible trajectories → byte-identical bundles (tested, including input-order independence). Recorded per version: `compiler_version`, `pipeline_version`, `prompt_version` (null: no prompts in M3), `model_version` (null), dataset name/version. A future LLM-assisted proposer must go through the same validator and record its prompt/model versions.

## Operating

```
uv run ee-learn process-jobs [--once]      # learning_jobs -> experience_cases
uv run ee-learn compile --tenant bench     # eligible cases -> CANDIDATE capabilities
uv run ee-learn candidates --tenant bench  # read-only listing
```
`ee-learn` refuses to start if `EE_EVALUATOR_ROLE` is set: a process that can read validation/hidden data must never be able to compile capabilities.

## Demonstration on real model traces

Gemini 2.5 Flash (Vertex AI), discovery split, one seed, persisted with `ee-bench run --persist`: 12 runs (11 verified, 1 failure). Relay → RabbitMQ → Celery worker enqueued 12 jobs; `ee-learn process-jobs` built 12 eligible cases (≈1.1 s wall time in total, 0 tokens); `ee-learn compile` registered two CANDIDATE capabilities (`pool`, `timeout`, version 1) with 11 source-success and 1 counterexample evidence rows. The pool candidate's skeleton was `list_files → read log/config/infra → LLM_DECISION → set_json_value → VERIFY`, with a limitation noting that the one contrasting failure claimed success without verification. **This shows the pipeline works on real traces; it does not show the candidates are good** — they are unevaluated until M4.

## Known limitations (M3)

* The compiler learns a **procedure skeleton**, not the numeric reasoning; value selection stays an `LLM_DECISION` whose quality is untested until M4.
* Skeleton by token support ignores step *arguments' semantics* beyond path role and value type; two variants with the same tokens but different correct procedures would be merged.
* Evidence counts are small in the simulated benchmark; no claim of improvement is made from candidates (they are unevaluated).
* Job retries have no backoff (bounded attempts only); RUNNING jobs are re-claimed after a 10-minute lease.
