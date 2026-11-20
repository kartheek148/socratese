# ADR 0008 — Milestone 5 decisions: runtime capability matching and execution

Status: Accepted (Milestone 5)

## Decisions
1. **Retrieval is advisory; eligibility is a separate deterministic gate.** `CapabilityRetriever` is a port (lexical placeholder now, embeddings later). `check_eligibility` never reads similarity, so a 0.99 match of the wrong family is rejected exactly like a 0.01 one (tested, plus a mutation check that granting eligibility on similarity fails the tests).
2. **Only ACTIVE versions are executable, enforced at the catalog.** `SqlExecutableCatalog` is read-only, tenant-scoped on every call and returns nothing but `EXECUTABLE_STATES` unless explicitly constructed as *experimental*. Experimental use is opt-in on the CLI, tags every run (`capability_catalog=experimental`), prints a warning and puts a banner in reports. Production wiring never builds it.
3. **The orchestrator re-enforces what the matcher decided.** A controller that selects a capability the gate did not pass raises; the executor independently re-validates the definition, tenant, permissions and preconditions, so a broken matcher still cannot execute a tampered or out-of-scope capability (tested with a deliberately bypassing matcher).
4. **Composition shares budgets.** `SUBCAPABILITY` children run inside every ancestor's steps/tokens/wall-clock (minimum along the chain), with a fresh variable scope, permissions ⊆ parent, depth ≤ 2, cycle detection, and ACTIVE-only resolution.
5. **Fallback never resets budgets.** After a recoverable capability failure general planning receives only the remaining tokens, steps, tool calls and deadline. Structural/safety failures abort without fallback; `HUMAN_APPROVAL` escalates. Known gap: a capability may already have mutated the environment; the simulated benchmark tolerates this, real systems need declared rollback (deferred).
6. **Verifier independence is unchanged.** Success is the independent verifier's verdict on final state; the capability's own `VERIFY` only controls fallback.
7. **Fixed-rule controller with full decision logging.** `RuleCapabilityPolicy` (`rule-capability/1`) records eligible actions, candidates with similarity and rejection reasons, selected `id@version`, probability 1.0 and the later verified outcome. It is not a learned policy; M6 adds estimates and exploration.
8. **Capability-assisted runs are excluded from learning** (`CAPABILITY_ASSISTED`) so outputs of the system are not re-mined as independent experience.
9. **No new infrastructure, dependency or migration.** Trace event types and exclusion reasons are free text; `capability_version` already existed on `run_outcomes`.
10. **Shadow/canary live routing is not wired.** M5 serves ACTIVE versions; adaptive rollout belongs with M6.

## Consequences / deferred
Learned strategy selection and calibrated estimates (M6); embedding retrieval; rollback for partially applied capabilities; a benchmark with headroom; multi-seed evaluation of runtime value. The real-model fresh-session run shows cost value only, from unpromoted candidates (see `docs/runtime.md`).
