# ADR 0002 — Benchmark-first delivery; independent verifier; protected hidden data

Status: Proposed (Milestone 0)

## Context
Improvement must be shown on held-out and transfer tasks against a fixed no-learning baseline. The design requires that agent self-reports never count as success and that hidden evaluation data is inaccessible to learning components.

## Decision
- Milestone 1 delivers a deterministic benchmark harness and baseline with no database, queue, or API, using a `ResultStore` port (versioned JSON/JSONL) that M2 replaces with PostgreSQL.
- The verifier reads environment evidence only; agent output is not an input to pass/fail.
- Datasets are versioned and content-hashed, split into `DISCOVERY`, `VALIDATION`, `HIDDEN_TRANSFER`. The hidden loader is separate and requires an evaluator role; compiler/learning workers are not configured with it.
- The baseline configuration (A) stays runnable and its reports reproducible through all milestones.
- The M1 controller is a `FixedPolicy` that still emits full `ControllerDecision` records (eligible set, selected action, policy version, reason), satisfying engineering rule 10 from the start.

## Consequences
Slightly more up-front structure in M1, but later milestones compare against a trustworthy baseline and need no retrofit of decision logging.
