# Experiment design: compiled capabilities vs memory-based experience reuse

Status: design written **before** implementation. Anything not listed here is not part of the
experiment; deviations will be recorded in the report, not silently adopted.

## 1. Hypothesis, stated so it can fail
* **H1 (primary).** With equivalent historical experience, a compiled, validated, executable
  capability (arm E) yields a higher verified-success rate on unseen tasks than the best
  memory-based arm (B, C or D), **and** its lifecycle cost per verified success breaks even against
  the stateless baseline within a plausible reuse horizon.
* **Null.** E is not distinguishable from (or is worse than) the best memory arm. The previous
  M5.5 result (one episodic hint matched the timeout capability at 2.7x its tokens) is evidence
  that the null is live. A "memory is as good" outcome is an acceptable result.
* Decomposition questions (answered by paired differences along an *attribution ladder*):
  A->B/C = value of information, C->D = value of a procedure stated as instructions,
  D->E = value of *executing* the procedure instead of asking the model to follow it.

## 2. Arms (same model, tools, budgets, tasks, verifier, discovery experience)
| Arm | What the agent gets | Mechanism |
|---|---|---|
| A stateless | nothing | `AgentRuntime`, unchanged |
| B episodic | up to 3 retrieved past episodes of the same task family: normalized tool sequence, outcome (success/failure), claimed-done flag, cost | text appended to the system prompt |
| C consolidated | per-family summary: applicability conditions, the consistent procedure, **failure patterns** (steps failed runs skipped, false "done" claims), caveats | text appended to the system prompt |
| D instructions | the compiled capability's procedure rendered as natural-language numbered instructions + its known limitations and applicability | text appended to the system prompt; the general agent executes it |
| E compiled | the capability itself via matcher -> controller -> sandboxed executor, falling back to general planning when ineligible | `TaskOrchestrator` + `CapabilityRuntime` (production path) |

**Information parity.** B, C, D and E are all built from the *same* set of eligible discovery
trajectories (verified successes **and** failures, DISCOVERY split only), via the same normalized
representation (`Trajectory`). C is computed with the same contrastive analysis the compiler uses,
so memory cannot receive less than the compiler saw; D is rendered from the compiled definition,
so it contains everything the capability contains. Raw values are in neither memory nor
capability (the normalizer deliberately drops them). Arms B-D share one fixed preamble; none of
the texts was tuned on evaluation data (they are fixed in code before any evaluation run).
**Retrieval** is deterministic and advisory (family, then role/tool overlap, then run id). For a
family never seen in discovery, memory arms still retrieve the nearest experience (this is
their natural capability); arm E cannot (its applicability gate is a hard family precondition).

## 3. Data and separation
* Dataset **v3** = v2 generators (unchanged) + one new hidden-only family `queue`
  (message-broker channel sizing: same arithmetic structure as `pool`, different vocabulary, library
  and keys) used for *cross-family* transfer. v1/v2 stay frozen.
* Discovery: public dev seed (fresh, never used before), 4 repetitions -> learning only. Compiler,
  consolidator and instruction renderer receive Trajectories only (no task or dataset access; an
  architecture test forbids `memory/` and `learning/` from importing `bench`, `evaluation`).
* Capability validation (`ee-eval`) uses private seed **P1**. The final experiment uses a **new**
  private seed **P2** that is never used for any gate, prompt, capability or text change.
* Transfer levels (derived from discovery, not hand-labelled): **intra-variant** = validation
  tasks (variants seen, new numbers); **cross-variant** = hidden tasks of a learned family with an
  unseen library/layout (hikari, okhttp); **cross-family** = hidden `queue` tasks.
* Negative examples: other-family tasks and synthetic precondition violations (missing tool /
  file role) scored for **false applicability**, deterministic, no model calls.

## 4. Statistics
* Unit of analysis = the **distinct task**. Repetitions are averaged per task first (they are
  repeated draws from a non-deterministic model, not independent tasks; the provider ignores seeds).
* Paired comparisons on identical (task, repetition) grids. Per comparison: mean per-task
  difference with a **paired task-level bootstrap** 95% CI (10,000 resamples, fixed RNG seed);
  **exact McNemar** on task-level binarised outcomes ("solved" = success in >= half of the
  repetitions); Holm correction across the primary comparisons (E vs A, B, C, D) so a single lucky
  pair is not reported as a finding. Wilson intervals are shown only descriptively.
* Reported per family, per variant and per transfer level; E's applicability and fallback rates.
* Success at equal budgets: post-hoc token caps (a run counts only if verified within the cap;
  retrieved memory tokens are inside the run's prompt tokens, so they are charged).
* Assumptions/limits: tasks within a family are exchangeable draws from a generator; correlated
  runs inside a task are collapsed; ~30 tasks per comparison gives wide intervals (we say so);
  one benchmark family set; simulated environment; McNemar loses information by binarising.
  Promotion thresholds (`default-low`) are not touched.

## 5. Lifecycle economics
`lifecycle_cost_X(N) = L_X + N * e_X + M_X`, e = mean tokens per task, success-adjusted
`c_X = e_X / success_rate_X` (tokens per verified success). Break-even vs A for N *verified
successes*: `N* = L_X / (c_A - c_X)` if `c_X < c_A`, else "never". Learning cost `L_X` (tokens;
wall time and tool calls reported alongside): shared discovery execution + analysis/consolidation +
compilation (E) + **validation** (E only as built; sensitivity row charges the same validation to
every arm) + retrieval. Maintenance `M` is **not measured** (no drift events yet) and defaults to 0,
which flatters every learning arm; the report says so. Currency is tokens; no prices or external
compute are available.

## 6. Models
Provider layer already abstracts the model (`LLMClient`, Vertex OpenAI-compatible adapter). Tasks,
capabilities, memories and evaluation are model-independent; `--model` selects the model. Main
run: `gemini-2.5-flash`. Bounded cross-model run (1 repetition, smaller private set) on a second
already-configured Vertex model if reachable; otherwise commands + missing configuration documented.
No new provider is added.

## 7. Planned changes (smallest set)
| Area | Change |
|---|---|
| `memory/` (new, pure) | `builders.py` (episodic, consolidated, instructions), `retrieval.py`, `inject.py` (prompt augmentation + token estimate) |
| `bench/families_v3.py`, `bench/dataset.py` | `queue` family; `DATASETS["v3"]` |
| `evaluation/arms.py` | `ArmOutcome` gains `retrieved_tokens`, `context_tokens`, `retrieval_ms`, `fell_back`, `capability_used`; production-path E arm |
| `evaluation/paired.py` (new) | task-level aggregation, bootstrap, McNemar, Holm, budget curves |
| `evaluation/economics.py` (new) | lifecycle ledger, break-even |
| `evaluation/experiment.py` (new) | grid runner (paired, concurrent), `ExperimentResult` (typed, JSON) |
| `evaluation/experiment_report.py`, `experiment_cli.py` (`ee-exp`) | run / report commands; full config + seeds persisted |
| `execution/capability_runtime.py` | `StaticCapabilityCatalog` |
| tests | unit tests for every new module; architecture boundaries; one DB-backed integration test |
| docs | this file, runtime/evaluation docs, ADR 0011, results report |

Unchanged: promotion policy and gates, registry, learning pipeline and DB schema (no migration),
production runtime behaviour.
