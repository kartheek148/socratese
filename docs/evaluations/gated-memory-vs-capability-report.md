# Gated memory vs. gated capability (M5.8)

Design: [gated-memory-vs-capability-design.md](../experiments/gated-memory-vs-capability-design.md). ADR: [0012](../decisions/0012-governed-memory-and-gated-comparison.md).
Generated tables (every number below is copied from them; nothing is hand-edited):
[production-mode run](./gated-production-run-gemini-2.5-flash.md) · [scoped-but-unvalidated run (experimental)](./gated-scoped-unvalidated-run-gemini-2.5-flash.md) · [per-asset validation reports](./gated-validation/) · raw rows: [data/gated-production-gemini-2.5-flash.json](./data/gated-production-gemini-2.5-flash.json), [data/gated-scoped-unvalidated-gemini-2.5-flash.json](./data/gated-scoped-unvalidated-gemini-2.5-flash.json).

**Headline (read the limitations in section 14 first).** Memory can be governed exactly like a capability, and the unchanged `default-low` gate promoted 1 of 6 memory assets and 1 of 2 capabilities. On fresh private data the promoted capability and the promoted procedural memory are **indistinguishable on correctness** (pool 98% vs 97%); the capability is **~3.6x cheaper per verified success and ~2x faster** on the family where both are active. The gate's most important finding is a negative one about the *gate*: it rejected five assets that, in a separate, clearly-labelled experimental run on the same fresh data, were highly beneficial (e.g. timeout memory 95-97% vs 47% baseline), because the absolute `max_regressions <= 1` rule is dominated by model noise at these sample sizes. Thresholds were **not** changed.

## 1. Experimental hypothesis
H1: when memory is held to the same lifecycle and promotion standard as capabilities, a validated capability still outperforms validated memory on correctness or on cost. H0: gated memory matches the capability. The experiment is built so either can win; the report states which did.
Pre-registered (design doc) decision rules: a winner is named only when the task-level paired bootstrap and exact McNemar agree after Holm correction; cost claims need tokens per *verified success*, with failures' cost included; ACTIVE-only production retrieval; no threshold changes.

## 2. Architecture changes
* `governance/` (pure): generic `GovernedRegistry[D]` holds the transition, shadow/canary, activation and rollback logic extracted from `capabilities/registry.py`; `RegistryService` (capabilities) and `MemoryRegistryService` are thin bindings. Capability behaviour, tables and gates are unchanged (all prior tests pass).
* `domain/memory.py`: typed `MemoryDefinition` (separate from `CapabilityDefinition`). `memory/` (pure): applicability, ACTIVE-only selection with explicit fallback, builders/registration. `adapters/db/`: `SqlMemoryStore`, `SqlMemoryRegistryStore` (shared generic `SqlAssetRegistryStore[D]` driven by `AssetSpec`), `SqlActiveMemoryCatalog`. Migration `0004` (+ DB triggers). `evaluation/memory_eval.py` reuses the unchanged engine and policy (`treatment` arm parameter). CLIs: `ee-learn build-memory`, `ee-gov`, `ee-exp --mode gated`.
* Import boundaries are enforced by architecture tests (`governance`/`memory` are pure; the learning plane cannot import the registry or catalogs).

## 3. Memory asset schema
Typed, immutable, versioned body: id `mem_<type>_<family>`, type (EPISODIC | CONSOLIDATED | PROCEDURAL), family, rendered content, episode bank (episodic), typed applicability and exclusion conditions (the same precondition classes capabilities use), failure conditions, known limitations, source experience ids, evidence refs, source capability refs, provenance, sha256 `content_hash` (verified on every selection; a tampered body is never injected). Lifecycle state, evaluations, transitions, observations and active/previous pointers live in separate append-only tables ([schema.md](../schema.md)). Applicability is deterministic and typed, never similarity; the builder always emits a `TaskFamilyIs` boundary plus exclusions for the other discovered families.

## 4. Promotion lifecycle
`CANDIDATE -> VALIDATING -> APPROVED -> SHADOW -> CANARY -> ACTIVE`, plus `REJECTED`, `DEPRECATED`, `ROLLED_BACK`; legal graph enforced in code and by the DB trigger (APPROVED requires a PASS evaluation; one ACTIVE per id). Exercised for real in this run: 8 assets validated, 2 promoted through 5 SHADOW + 5 CANARY observations to ACTIVE, 1 rollback drill (`mem_procedural_pool@1` ACTIVE -> ROLLED_BACK via `ee-gov rollback`; the production catalog then stops serving it). Tests: registry-level promotion/rollback, DB-enforced rules for direct SQL, tenant isolation, and gated-arm tests proving CANDIDATE/REJECTED assets are never served.

## 5. Gating criteria
Unchanged `default-low` (policy hash `7b6f0944819b…`) for both asset kinds: source replay >= 80% (necessary, not sufficient); held-out and transfer success >= 75%; 95% lower bound on held-out+transfer >= 50%; false applicability 0; `max_regressions <= 1` (an **absolute count**); distinct-task sample-size minimums. Memory-specific measurement (documented mismatches): `max_exec_token_ratio` is `None` at `low`, so memory token overhead is reported, not gated; cross-family transfer is reported, not gated, for both kinds; negative suite = other-family tasks plus synthetic violations. Validation used private seed P1 (`--scale 3`, 2 repetitions); the final comparison used a different private seed P2 (`--scale 5`). P2 was never used to tune anything. Seeds are not recorded.

## 6. Dataset and model details
Dataset v3 (`gen/3`): public discovery seed 58058 (12 tasks x 4 persisted real-model runs; 45 eligible trajectories after verification), private validation/hidden splits P1 (x3) and P2 (x5; 60 pool, 60 timeout, 30 queue tasks = 150 distinct tasks; queue is never seen in discovery). Model `gemini-2.5-flash` via Vertex (temperature 0; the provider ignores seeds, so repetitions are repeated draws). 1 repetition per task in the final run (budget), 990 runs (9 UNKNOWN excluded as missing data) in production mode; 900 runs (5 UNKNOWN) in the scoped run. Arms: A stateless; B/C/D ACTIVE episodic/consolidated/procedural memory only; E ACTIVE capability only; F noise control (same path as B, empty registry); UB/UC/UD ungated always-inject memory on the unseen `queue` family only.

## 7. Per-family performance (production mode, ACTIVE assets only)
ACTIVE after validation: `pool.f12f4fa920@1` (E) and `mem_procedural_pool@1` (D). Everything else ran under general planning with a recorded reason.

| Family (tasks) | A | B | C | D | E | F |
|---|---|---|---|---|---|---|
| pool (60) | 23% | 33% | 40% | **97%** | **98%** | 33% |
| timeout (60) | 40% | 46% | 45% | 51% | 53% | 46% |
| queue (30, never learned) | 13% | 17% | 27% | 23% | 23% | 17% |

On `timeout` and `queue` no ACTIVE asset applied (E and D fell back to planning in 100% of those tasks), so all six arms are the same procedure: the spread (A 40%, F 46%, B 46%, C 45%, D 51%, E 53%) is the **noise floor** of this experiment (~±6 points at 60 tasks, 1 repetition), and B and C "beating" A there or overall (B vs A +7% [-1%, +16%]; C vs A +11% [+3%, +18%], Holm p=0.045) is **not** a memory effect: B and C applied no asset on any run. The report generator flags such arms automatically. Per-variant pool success: D 97/93/100% and E 100/93/100% (hikari / psycopg / sqlalchemy; the latter two are unseen layouts).

## 8. Statistical comparisons (task-level; paired bootstrap 10,000, exact McNemar, Holm)
Primary, all 150 tasks (production mode): E vs A +38% [+29%, +47%] (61/5 discordant, Holm p<0.001); E vs B +30%; E vs C +27%; E vs F +31%; **E vs D +1% [-6%, +9%], 16/14 discordant, McNemar p=0.86: not distinguishable**. On the one family where both have an ACTIVE asset (pool): E vs A +75% [+63%, +85%], E vs B +65%, E vs C +58%, **E vs D +2% [-3%, +7%] (2/1 discordant): not distinguishable**. D vs C +25% (Holm p<0.001) is a real effect of *having an ACTIVE asset* (D) vs *not having one* (C), not of the representation. Attribution: information alone (episodic/consolidated) was never promoted, so the production run cannot attribute information vs execution; the scoped run below can.

## 9. Generalization results
* **Within family, unseen libraries (cross-variant):** both promoted assets hold up (pool psycopg/sqlalchemy: D 93%/100%, E 93%/100%), consistent with the validation suites (held-out 100%/100%, transfer 97%/100%).
* **Cross-family (queue, never in discovery):** neither generalizes. Applicability correctly refuses (0/30 applied; 0 false applications), so D and E equal planning (23%, 23% vs A 13%, differences not significant: E vs A +10% [-7%, +27%], p=0.45). The ungated arms confirm that "just inject nearest experience" does not generalize either: UB 17%, UC 13%, UD 0% (see 11).
* **Negative-transfer reduction by gating (queue, 30 tasks):** ungated instruction memory UD succeeded on 0/30 vs 13% for the stateless baseline (-13 points; bootstrap CI for A-UD [+3%, +27%], exact McNemar 4/0 p=0.125: *not significant*). UB (+3 points) and UC (0 points) showed no measurable harm. Gated D (= planning) vs UD: +23% [+10%, +40%], 7/0, McNemar p=0.016 but Holm-adjusted 0.094. So: harm was observed for one of three ungated memory forms, in the direction predicted, and **gating removed it by construction, but the sample is too small to call it statistically established**. The earlier experiment ([report](./capability-vs-memory-report.md)) found larger negative transfer on `timeout`.

## 10. Lifecycle economics (tokens)
As built (each arm pays the validation recorded for its own asset type, **rejected attempts included**; equal-governance charges every learning arm the mean recorded validation cost). Learning ledger: discovery 257,325 tokens; analysis/build/compile 0 tokens (deterministic; wall time only: compilation 979 ms); validation **9,437,873 tokens** over 8 assets (B 2,267,786; C 2,365,137; D 2,398,378; E 2,406,572 for two assets each). **Validation is ~37x the cost of discovery**; it is the dominant lifecycle cost for every learned asset, rejected ones included.

| Scope | Arm | success | tokens/verified success | latency s/run | L (learning tokens) | break-even vs A (verified successes) |
|---|---|---|---|---|---|---|
| pool only | A | 23% | 22,462 | 23 | 0 | reference |
| pool only | D (procedural, ACTIVE) | 97% | 7,637 | 20 | 1,309,690 | 88 |
| pool only | E (capability, ACTIVE) | 98% | **2,129** | **9** | 1,234,438 | **61** |
| all tasks | D | 64% | 11,126 | 27 | 2,655,703 | 258 (equal governance 254) |
| all tasks | E | 66% | 7,639 | 22 | 2,663,897 | 193 (equal governance 190) |

Maintenance is **not measured** (assumption, explicit in the generated tables): if each asset's validation is repeated once (R=1) or three times (R=3) over its life, all-tasks break-even moves to E 368 / 717 and D 491 / 957 verified successes. At N=10,000 future tasks the all-in cost per verified success is 8,413 tokens (E) vs 11,919 (D) (R=1, mixed families). Caveat: B and C economics are meaningless in production mode (they applied no asset; they carry the cost of validating rejected assets); ungated arms are never validated, hence cheap to build and harmful/unhelpful to run. Answer to "does execution justify compilation + validation?": **yes for a recurring family** (pool: lowest tokens, latency and break-even), **no case is made for rarely-recurring families**, and because validation dominates, the capability's *relative* advantage over a validated memory is small (E 61 vs D 88 successes to break even).

## 11. Rejected memory assets and why they failed (policy `default-low`, P1)
| Asset | held-out (asset vs baseline) | transfer | verdict reason |
|---|---|---|---|
| `mem_episodic_pool@1` | 28% vs 17% | 25% vs 42% | source replay 25% < 80%; held-out 28% < 75%; transfer 25% < 75%; lower bound 18% < 50%; **20 regressions** (the only asset the gate was clearly right to reject; the same text in the scoped run: 27% vs 28% on P2) |
| `mem_consolidated_pool@1` | 94% vs 17% | 92% vs 42% | 3 regressions > 1 |
| `mem_episodic_timeout@1` | 86% vs 56% | 78% vs 53% | 3 regressions > 1 |
| `mem_consolidated_timeout@1` | 100% vs 56% | 89% vs 53% | 2 regressions > 1 |
| `mem_procedural_timeout@1` | 100% vs 56% | 100% vs 53% | 2 regressions > 1 |
| `mem_procedural_pool@1` | 100% vs 17% | 97% vs 42% | **PASS**, ACTIVE |

**What this means.** Five of six memory assets, including four with large, wide-margin gains, were rejected for 2-3 "regressions" (each counts one (task, repetition) pair where the baseline's draw verified and the asset's draw did not; a single flaky task counts once per repetition) against an allowed 1. With a stochastic model, a weak baseline that solves ~50% of tasks at random and 2 repetitions over 40+ tasks, a few such events occur by chance for *any* asset. In the scoped (unvalidated, experimental) run on fresh P2 data the same assets were: timeout B 76%, C 95%, D 97% vs A 47% (F 50%); pool C 92%, D 92% vs A 28%; episodic pool 27% (harmful or useless, consistent with the gate). So **the gate correctly rejected the one bad memory (episodic pool) and falsely rejected four good ones**. This is evidence about the policy design (absolute regression count that gets stricter as runs grow, noted in [evaluation.md](../evaluation.md)), not about memory. It was not tuned away; see Conclusions for a proposal that must be validated on fresh data.

## 12. Rejected capabilities and why they failed
| Asset | held-out (asset vs baseline) | transfer | verdict reason |
|---|---|---|---|
| `timeout.e2fb15fa79@1` | 75% vs 47% | 94% vs 56% | source replay 75% < 80%; **5 regressions** > 1 |
| `pool.f12f4fa920@1` | 100% vs 14% | 100% vs 19% | **PASS**, ACTIVE (0 regressions) |

In the scoped run the rejected timeout capability achieved 98% vs 47% baseline on 60 fresh timeout tasks (a different compiled version from the harmful timeout capability reported in the earlier experiment: discovery data and seeds changed, so no claim is made about why this one works). It is another false rejection of the same kind as in section 11 and one that the capability cannot escape: its source-replay shortfall (75% < 80% on 8 tasks) is within sampling noise.

## 13. Failure analysis
* Production mode: pool E fell back to planning on 2% of tasks (decision failure), 0 false applications by any arm. 9 of 990 runs were UNKNOWN (provider errors), excluded, not counted as failures.
* Scoped run (experimental, **not promotion evidence**): with scope-correct applicability every memory form applied only inside its family (0 false applications; queue fell back to planning in 30/30). Ranking by correctness on pool/timeout: E 98/98, D 92/97, C 92/95, B 27/76 vs A 28/47 and F 30/50. Episodic memory (raw past episodes) is the weakest representation and *hurts* on pool (long tool traces, 20 validation regressions); procedural and consolidated forms carry most of the gain; the capability adds ~2x speed and ~3.6x token efficiency on top.
* Queue (unseen family) is hard for every arm: 10-30% regardless of asset (n=30, differences n.s.). The ungated UD arm scored 0/30.

## 14. Limitations
Single model (gemini-2.5-flash) and the simulated benchmark; 1 repetition per task in the final run and 2 in validation (repetitions are repeated draws, not independent); 60 tasks per learned family and 30 for queue (the target of >=60 was met for pool and timeout, **not for queue**: pilot-sized, labelled as such); noise floor ~±6 points, so per-family differences below that are not interpretable; the P1 validation, P2 comparison and discovery are single draws of the dataset generators; the compiler/builder designs were developed while the v2/v3 families were visible, so the families are not blind to the approach designers; maintenance is an explicit assumption, not measured; validation tokens are charged per asset although the shared baseline was computed once per session (conservative for every learning arm equally); only 2 of 8 assets were promoted, so the *production-mode* comparison of memory representations is limited to procedural memory (the scoped run, which is not governed, supplies the rest); the scoped run is post-hoc/exploratory and must not be used to tune the gate on P2; the gated memory arms are not wired into `ee-run` yet (M6 concern); Postgres row-level security and live shadow/canary routing remain out of scope (observations are evaluator-driven).

## 15. Conclusions and answers
1. **Can memory be governed as a versioned, validated learning asset?** Yes. Typed schema, immutable content-hashed versions, the same lifecycle graph (code and DB triggers), independent evaluation under the same policy, promotion and rollback, ACTIVE-only retrieval; capability governance unchanged. 6 memory assets went through it end to end (1 promoted, 5 rejected with recorded reasons).
2. **Which memory representation has the highest utility?** Procedural (instruction) memory: it was the only one to pass the unchanged gate, and in the scoped run it was best or tied for best (pool 92-97%, timeout 97%); consolidated is close behind (92%, 95%); raw episodic memory is clearly worst (hurts on pool).
3. **Does memory outperform capabilities for any task family?** No. Memory tied the capability on correctness on pool (97% vs 98%) and, in the scoped run, on timeout (97% vs 98%); on the unseen queue family D 30% vs E 23% (scoped) and 23% vs 23% (production) are noise. Memory never beat the capability significantly.
4. **Does capability execution reduce cost enough to justify compilation and validation?** For a recurring family, yes on per-task cost (pool: 2,129 vs 7,637 tokens per verified success, 9 s vs 20 s) and break-even (61 vs 88 verified successes), but compilation is free (deterministic) and **validation (not compilation) is ~97% of learning cost**, so the case is "cost and latency of recurring tasks", not accuracy, and break-even grows with maintenance assumptions (R=3: 717 successes over all tasks).
5. **Which learned assets should never be activated?** `mem_episodic_pool@1` (20 regressions, 28% vs 17% held-out, 25% vs 42% transfer) was correctly kept out. The other four rejected memory assets and the timeout capability are **not** shown to be harmful; they were rejected by a noise-sensitive rule, and the answer for them is "not by this gate", not "never".
6. **How much does gating reduce negative transfer?** Measured on one unseen family: -13 points for ungated instruction memory (UD 0% vs A 13%) removed by gating; UB/UC showed none; the effect is not statistically established (McNemar p=0.125; gated vs ungated D Holm p=0.094; n=30). Gating also had a cost: it blocked four beneficial memory assets and one beneficial capability (section 11-12).
7. **Does either approach generalize beyond its original task family?** No. Both generalize within a family to unseen libraries/layouts and fail (or correctly refuse) outside it; ungated injection into an unseen family did not help (UB 17%, UC 13%, UD 0% vs A 13%).

**Recommended next steps (for review; none applied).** (a) Replace the absolute `max_regressions` count with a rate/paired-test rule evaluated on baseline-solved tasks with repetition (e.g. paired exact test on regression vs improvement discordants), then **validate the revised gate on a fresh seed before adopting it**: the scoped P2 data motivated the concern but must not be the tuning set. (b) Report validation cost per asset in the lifecycle view because it dominates. (c) Gather >=60 queue-family tasks and more repetitions and a second model before claiming generalization or negative-transfer effects. (d) M6 (controller) should choose among {procedural memory, capability, planning} per family using verified outcomes, since the capability's advantage is cost, not accuracy.
