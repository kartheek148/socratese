# ADR 0009 — Milestone 5.5: harder benchmark (dataset v2) and compiler v3

Status: Accepted

## Context
M5 showed cost value but no correctness value because the v1 baseline was already at 83-100%. M6 (learned strategy selection) needs tasks where using, composing or skipping a capability changes the outcome.

## Decisions
1. **New dataset generation, v1 frozen.** `DATASETS` registers `v1` (hashes asserted by existing tests) and `v2` (`benchmarks/v2`, generator `gen/2`). Same tools, verifier, environment and families, so results stay comparable; difficulty comes from combining evidence across files (per-request connections from the log, replica count and other services from `infra/`, retry budget, platform ratio policy, mixed units, an other-client decoy).
2. **Difficulty must be earned and honest.** A reference solver that reads only the environment proves every task solvable; each shortcut (v1-style sizing, whole-database sizing, decoy, ignoring retries, greedy budget) is asserted to fail the independent verifier. The first version of the timeout family was too easy for the real model (89% on a probe), so the platform ratio policy was added *before* any learning, and the probe data were discarded.
3. **No policy weakening.** `default-low` is unchanged. Both v2 candidates were REJECTED by it (see `docs/evaluations/v2-benchmark-and-first-evaluations.md`).
4. **Compiler v3: contrast may add read-only steps.** The 60%-support skeleton dropped the one observation (the log) that all failures skipped and only some successes performed. Contrastive evidence now adds *read-only* steps with real counterexamples (>= 3 failures, success support >= 30%, failure support <= 20%, gap >= 30 points). It never adds a mutating step; the addition is written into `known_limitations`. Bumping the default compiler version means a changed procedure creates a new immutable version, as before.
5. **Paired reports key on (task, seed).** Repeating a task over seeds yields separate paired samples; reports list seeds and give per-variant tables.
6. **Evaluation still uses a single seed.** `EvaluationPlan.seeds` is recorded but not iterated; making evaluation repeat per seed is the first follow-up, because 6 tasks per suite make the 75% gates too coarse (one task = 17 points).

## Consequences
The benchmark has headroom (baseline 28-44% on private splits) and capabilities show a measurable advantage, but none is ACTIVE. M6 must not start from an empty registry without either multi-seed evaluation or an explicit decision to work offline from the experimental catalog.
