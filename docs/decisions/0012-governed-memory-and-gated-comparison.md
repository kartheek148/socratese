# ADR 0012 — Governed memory and the gated memory-vs-capability comparison (M5.8)

Status: Accepted. Evidence: [gated-memory-vs-capability-report.md](../evaluations/gated-memory-vs-capability-report.md). Design: [gated-memory-vs-capability-design.md](../experiments/gated-memory-vs-capability-design.md).

## Context
The previous experiment compared a validated capability with *ungated* memory, which is not a fair fight: the capability had paid for validation and memory had not. M5.8 makes both equally accountable.

## Decisions
1. **Memory is a first-class governed asset, separate from capabilities.** Typed `MemoryDefinition` (domain), own tables (migration 0004) with the same lifecycle graph, append-only audit tables and DB-enforced transitions. Capability domain objects, tables and gates are unchanged.
2. **One promotion engine.** The generic logic was extracted into `governance/registry.GovernedRegistry[D]` and the SQL store into `SqlAssetRegistryStore[D]` driven by an `AssetSpec`; capabilities and memory are thin bindings. Known debt: the id parameter keeps the name `capability_id` and holds the asset id.
3. **Same policy, no threshold changes.** `default-low` gates both kinds through the unchanged evaluation engine (`treatment` arm parameter). Documented mismatches (token ratio gate absent at `low`; cross-family transfer reported not gated) are not "fixed" by relaxing anything.
4. **Applicability is typed and deterministic** (family, tools, file roles, risk, exclusions) and checked before injection; similarity can only rank among applicable artifacts. An unrestricted memory is representable (so it can be tested) but must fail the false-applicability gate.
5. **Production mode reads only ACTIVE versions**, tenant-scoped, with an explicit recorded fallback to general planning; experimental catalogs are an explicit, bannered override for research runs only.
6. **Fair economics.** Validation tokens come from stored evaluation rows, rejected attempts included; equal-governance prices every learning arm identically; maintenance is an explicit assumption (re-validations R), never zero-by-default silently.
7. **Experiment design.** `ee-exp --mode gated`: A, B/C/D (ACTIVE memory by type), E (ACTIVE capability), F (noise control), UB/UC/UD (ungated memory on listed unseen families to measure negative transfer). The report auto-flags arms that applied no asset and labels scoped/unvalidated runs as non-evidence.
8. No controller changes (M6), no new infrastructure or provider.

## Findings
* Governance works end to end for memory (6 candidates validated, 1 promoted, 5 rejected with reasons; rollback drill).
* On fresh private data the promoted capability and procedural memory tie on correctness (pool 98% vs 97%); the capability is ~3.6x cheaper per verified success and ~2x faster. Memory did not outperform the capability on any family.
* **The gate is noise-sensitive.** The absolute `max_regressions <= 1` rule rejected five assets (4 memory, 1 capability) that were highly beneficial in a separate, labelled experimental run on the same fresh data, while correctly rejecting one harmful asset (episodic/pool). Not changed here; proposed for review with validation on a fresh seed.
* Validation, not compilation, dominates lifecycle cost (~97% of learning tokens, ~37x discovery).
* Gating removed observed negative transfer on one unseen family (ungated instruction memory 0% vs 13%), but the effect is not statistically established at n=30.
* Neither approach generalizes across families; both generalize across libraries within a family.

## Consequences
* The product claim stays narrow: governed memory and governed capabilities both improve a recurring family; the capability's edge is cost and latency, and its validation cost is shared by memory.
* Before M6: fix the regression rule (validated on fresh data), gather more queue-family and multi-repetition data, add a second model. M6 should choose among {memory, capability, planning} per family using verified outcomes.
* Debts: `capability_id` naming in generic ports; memory arms not wired into `ee-run`; maintenance unmeasured.
