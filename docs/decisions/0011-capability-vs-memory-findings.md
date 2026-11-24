# ADR 0011 — Capability-vs-memory experiment: findings and decisions

Status: Accepted. Evidence: [capability-vs-memory-report.md](../evaluations/capability-vs-memory-report.md).

## Context
M5.5 suggested a one-hint episodic memory could match a compiled capability. The architecture's premise is that executable, validated capabilities beat memory. We built an experiment able to falsify that.

## Decisions
1. **Five arms on identical tasks/repetitions** (A stateless, B episodic, C consolidated, D instruction reuse, E compiled runtime), all built from the same discovery trajectories; the memory/instruction texts are fixed before evaluation. `memory/` is a pure package that sees only trajectories and definitions (architecture test), so hidden data cannot leak into memory.
2. **Statistics at task level**: repetitions averaged inside a task, paired bootstrap CI, exact McNemar, Holm; a winner is named only when bootstrap and McNemar agree. (A first version of the report named winners on the bootstrap alone and over-claimed; fixed and tested.)
3. **Economics are explicit**: `L + N*e + M`, break-even per verified success, as-built and equal-validation views, maintenance as an input (0 = not measured).
4. **New `queue` family (dataset v3)** for cross-family transfer; v1/v2 frozen. `ee-exp` never writes to the registry; promotion gates and thresholds untouched.
5. **Cross-model** run through the existing Vertex adapter (no new provider).

## Findings (see report for numbers)
* Capability vs weak memory (B): clearly better on pool. Capability vs good memory (C/D): **no demonstrable correctness advantage**; ~3.7x lower tokens per verified success and ~2.5x lower latency on pool.
* Most of the gain is information (what evidence to read and combine), not execution. Execution gives cost, determinism and an audit/validation surface.
* The compiler can produce a harmful procedure (timeout). The promotion gate stopped it; ungated memory arms degraded the same family. Value of validation is real but was measured on one family.
* Cross-family transfer: none for any arm; memory may transfer negatively. The capability's refusal is safe but not general.
* A capability inherits the model's reasoning quality: flash-lite 0% on pool, pro 100% (also for the baseline-strong timeout).
* Break-even for pool is tens of verified successes for every arm; memory recovers fastest unless held to the same validation standard.

## Consequences
* The product claim is narrowed: "compiled capabilities lower the cost of recurring, validated task families and add a safety gate", not "they are more accurate than memory".
* Next: gate memory too (gated-vs-gated), capture the sizing decision in the capability, cross-family abstraction, >=60 tasks per cell, drift/maintenance measurement. M6 (learned strategy selection) should choose among {memory, capability, planning} per family, not assume the capability dominates.
