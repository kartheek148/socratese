# Does compiling experience into executable capabilities beat memory? (experimental report)

Date of runs: 2026-11-24. Design (written before implementation): [experiments/capability-vs-memory-design.md](../experiments/capability-vs-memory-design.md).
Raw, machine-generated reports (every number below is taken from them; results files in [data/](./data)):
[gemini-2.5-flash](./capability-vs-memory-gemini-2.5-flash.md) (main, 60 tasks x 2 repetitions x 5 arms),
[gemini-2.5-pro](./capability-vs-memory-gemini-2.5-pro.md) and [gemini-2.5-flash-lite](./capability-vs-memory-gemini-2.5-flash-lite.md) (bounded cross-model, 30 tasks x 1 repetition);
capability validation (separate private data): [pool](./v3-validation-pool-gemini-2.5-flash.md), [timeout](./v3-validation-timeout-gemini-2.5-flash.md).

## Short answer
**The hypothesis "compiled capabilities outperform memory-based reuse" is only partly supported, and the part that is supported is about cost, not correctness.**
* A compiled capability clearly beats *no* experience and beats *weak* memory (episodic examples) on the family where it works (pool).
* Against *good* memory (a consolidated summary with failure patterns, or the same procedure as natural-language instructions) it is **not distinguishable on correctness** (pool: 98% vs 90%, McNemar p = 1.0; on gemini-2.5-pro all three reach 100%). Its advantage is ~3.7x fewer tokens per verified success and ~2.5x lower latency on pool.
* The compiler produced a procedure that was **harmful** for the second family (timeout: 17-21% vs 42-54% baseline on validation data). Only the capability arm has a validation gate, and the gate caught it; the ungated memory arms made the same family worse (consolidated 25%, instructions 29% vs baseline 46%).
* Nothing transferred cross-family, memory included.
* With a weak model (flash-lite) the capability did not help at all (0% on pool): its one remaining model decision still needs the model to reason.

## 1. Hypothesis
H1: with equivalent historical experience, E (compiled, validated, executable capability) has a higher verified-success rate on unseen tasks than the best memory arm *and* its lifecycle cost per verified success breaks even against the stateless baseline. Null: E is not distinguishable from the best memory arm. **Result: the null is not rejected for correctness; the cost half of H1 is supported for the pool family.**

## 2. Design and 3. Arms
See the design doc. A stateless; B episodic (3 past episodes with outcomes incl. one failure); C consolidated (applicability, shared procedure, discriminative failure patterns, false-"done" rate); D the compiled procedure rendered as instructions executed by the general agent; E the production capability runtime (matcher, controller, sandboxed executor, fallback to planning). All arms: same model, tools, budgets, tasks, independent verifier. B-E are built from the same 63 eligible discovery trajectories; C uses the same contrastive analysis as the compiler; D is rendered from the compiled definition. Texts were fixed before any evaluation run.

## 4. Data and 6. Separation
Dataset v3 (v2 generators + hidden-only `queue` family). Discovery: public dev seed 918273, 4 repetitions x 16 tasks, 63 usable trajectories (one run was UNKNOWN from a provider outage and was excluded from learning by design). Capability validation (`ee-eval`, 2 seeds): private seed P1. **Experiment: a different private seed P2** (24 validation + 36 hidden tasks; never used for any gate, prompt or text change). Cross-model: a third private seed P3 (30 tasks). Private seeds are not recorded. The compiler, consolidator and instruction renderer take only trajectories/definitions; an architecture test forbids `memory/`, `learning/` from importing `bench`/`evaluation`.

Transfer levels: intra-variant (validation: seen libraries, new numbers), cross-variant (hidden hikari/okhttp: unseen libraries), cross-family (hidden `queue`: never in discovery).

## 5. Models
gemini-2.5-flash (main; also the discovery/validation model), gemini-2.5-pro and gemini-2.5-flash-lite for the cross-model check, via the existing Vertex adapter, temperature 0. The provider ignores seeds, so "repetitions" are repeated draws from a non-deterministic model; the experiment treats them as such. No provider was added.

## 7. Success by family (gemini-2.5-flash, mean of per-task rates; tasks in brackets)
| Family | A | B | C | D | E |
|---|---|---|---|---|---|
| pool (24) | 25% | 58% | 90% | 90% | **98%** |
| timeout (24) | 46% | 42% | 25% | 29% | 42% |
| queue, cross-family (12) | 17% | 4% | 12% | 4% | 12% |
| all (60) | 32% | 41% | 48% | 48% | 58% |

By level: cross-variant A 40 / B 56 / C 60 / D 60 / E 73; intra-variant 31 / 44 / 54 / 58 / 67; cross-family 17 / 4 / 12 / 4 / 12.

## 8. Paired statistics (task-level bootstrap CI, exact McNemar, Holm within each table)
| Comparison (flash) | tasks | diff [95% CI] | McNemar p (Holm) | reading |
|---|---|---|---|---|
| all: E vs A | 60 | +27% [+15, +38] | 0.049 (0.147) | ahead by bootstrap; McNemar n.s. after Holm |
| all: E vs B | 60 | +18% [+8, +28] | 0.013 (0.052) | ahead by bootstrap; McNemar borderline after Holm |
| all: E vs C | 60 | +10% [+4, +16] | 0.289 (0.359) | ahead by bootstrap only |
| all: E vs D | 60 | +10% [+2, +18] | 0.180 (0.359) | ahead by bootstrap only |
| **pool: E vs A** | 24 | +73% [+60, +83] | <0.001 (0.001) | E better |
| **pool: E vs B** | 24 | +40% [+23, +56] | 0.016 (0.047) | E better |
| **pool: E vs C** | 24 | +8% [+2, +17] | 1.000 | bootstrap only; 1 discordant task |
| **pool: E vs D** | 24 | +8% [0, +19] | 0.500 | not distinguishable |
| timeout: E vs C | 24 | +17% [+6, +27] | 0.219 | bootstrap only |
| cross-family: all four | 12 | n.s. | >=0.5 | not distinguishable |

On gemini-2.5-pro (30 tasks, 1 repetition) E vs C and E vs D are 83/83 and 83/80 (n.s.); pool is 100% for C, D and E. On flash-lite nothing works (below).
Caveat on the bootstrap "ahead" rows: with one or two discordant tasks the bootstrap interval is narrow but the evidence is a single task; the dual-test rule exists for this reason.

## 9. Execution efficiency (flash)
Pool family: tokens/run A 5.2k, B 7.3k, C 7.6k, D 7.4k, **E 2.2k**; tokens per verified success A 20.6k, B 12.4k, C 8.5k, D 8.3k, **E 2.3k**; latency/run 22 / 24 / 24 / 20 / **9 s**. Retrieved memory is 190-370 tokens per run (counted inside every prompt) and retrieval time is negligible, so memory's cost is mostly the longer agent loop, not retrieval.
Equal token budgets (all tasks): success within <=3k tokens: E 39%, all others 0%; <=6k: E 48% vs A 10 / B 2 / C 5 / D 2%; at >=12k memory arms catch up (C/D 45-48%, E 56-58%).

## 10. Lifecycle cost and 11. break-even (tokens; maintenance not measured = 0, which flatters every learning arm)
Ledger: discovery 381,533 tokens (63 runs, 263 tool calls); analysis/consolidation/compilation 0 model tokens (deterministic; ~1.3 s wall); validation 1,808,832 tokens (both capabilities, controls A+B charged as in M4). Break-even = verified successes needed to recover learning cost vs the stateless baseline.
| | B | C | D | E |
|---|---|---|---|---|
| pool, as built (only E pays validation) | 21 | **14** | **14** | 58 |
| all families, as built | 12,345 | 136 | 138 | 236 |
| all families, every arm pays the same validation | 70,872 | 782 | 793 | **236** |
| timeout family | never | never | never | never |
So: for the pool family every learning arm pays itself back within tens of verified successes; the memory arms recover fastest *if they are not validated*. If memory is held to the same validation standard, the compiled capability is the cheapest to own. For the timeout family no arm recovers its cost.

## 12. Generalisation
* Intra-/cross-variant: E 67% / 73% overall; on pool the unseen hikari variant is 100% (E), 92% (C), 88% (D), 62% (B), 33% (A). Timeout does not transfer for any arm.
* **Cross-family: no arm transferred** (E 12%, C 12%, A 17%, B and D 4%). The capability's applicability gate refused all 12 tasks by design (eligible 0%) so E equals planning. Memory arms retrieved the nearest experience and the two that surfaced concrete steps (B, D) did *worse* than no memory (n = 12, not significant) - plausible negative transfer.
* Applicability boundaries (deterministic, no model): each capability admitted 24/24 of its own family, 0/36 of other families and 0/4 synthetic precondition violations. Validation also showed 0% false applicability (8/8 correct refusals for each capability). A capability that refuses is safe but not general.

## 13. Is the improvement better reasoning, better information, or an executable procedure?
Attribution ladder on pool (flash): A 25% -> B 58% (information, weak form) -> C 90% (information incl. failure patterns) -> D 90% (the same as instructions: +0) -> E 98% (+8, n.s.). **Most of the gain comes from information about what evidence to combine** (read the log and infra before editing, do not stop at a plausible change). Execution adds determinism, cost and latency (3-4x), and a small, not significant, accuracy gain. It is not "better reasoning": E still delegates the sizing decision to the model, which is why flash-lite (0% on pool, baseline 0%) gains nothing from the identical procedure, while on pro the baseline already reaches 50% and every learning arm reaches 100%.

## 14. Failure analysis
* Baseline failures are almost all confident, wrong "done" claims (81 of 82 non-verified runs terminated FINAL).
* E on timeout fell back to planning in 88% of runs (E's non-verified runs overall include 7 `INVALID_ACTION` and 1 `MAX_STEPS` terminations); validation shows source-replay 12% vs baseline 44%, 20 regressions. The procedure compiled from fresh discovery data omitted the log read, and unlike the pool capability the remaining decision was not made reliably by flash; on pro the same capability scores 100% (baseline 100%): the quality of a capability is model-dependent.
* Memory arms on timeout (C 25%, D 29%) regressed against the baseline (46%) with no gate to stop them.

## 15. Regression analysis (tasks A solved that the arm did not; flash)
E 4 regressed / 13 improved; C 8 / 13; D 12 / 15; B 13 / 12. E's regressions are 2 cross-family and 2 timeout tasks.

## 16. Limitations and reproducibility
* One benchmark family set, a simulated environment, generator-made tasks; 24 tasks in the decisive pool comparison (12 for cross-model) - wide intervals, and McNemar loses information at this size.
* Repetitions are not independent; seeds are not honoured by the provider, so runs are not bit-reproducible. The result files and `ee-exp report` regenerate every table exactly; the experiment itself needs fresh private seeds.
* The catalog for D/E was experimental (versions were CANDIDATE/VALIDATING while `ee-eval` was running); pool.f12f4fa920@1 passed validation (held-out 96%, transfer 100%, lifecycle 14.2k tokens/success vs baseline 17.7k) and timeout.de1091cd4d@1 was REJECTED. Promotion thresholds were not changed.
* Memory texts are one fixed design; a better-engineered memory could do better. The compiler is the existing rule-based one.
* Maintenance cost (drift, re-validation) is not measured; validation was charged to E only in the "as built" view.
* The cross-model runs are single repetition on 30 tasks and are indicative only; flash-lite is a floor-level model for this benchmark.
Commands: see [docs/evaluation.md](../evaluation.md#capability-vs-memory-experiment-ee-exp).

## 17. Conclusions and when to prefer memory
1. Compiled capabilities **do not demonstrably outperform good memory on correctness** here; they win on cost/latency per verified success and on auditability (deterministic steps, validation gate, versioning).
2. Prefer **memory** when: volume per family is low (break-even is tens to hundreds of verified successes plus the validation bill), the procedure is unstable or needs several judgement calls (timeout), the task may fall outside any known family (memory at least retrieves related experience; the capability refuses), or the model is weak (the procedure does not remove the reasoning step).
3. Prefer a **compiled capability** when: one family recurs at volume, per-task cost and latency matter, and you need a gate: only the capability path stopped a harmful procedure before production. Memory needs its own validation gate to be trusted.
4. Next research: (a) put memory through the same promotion gate and compare gated vs gated; (b) capability that also captures the sizing logic (less delegation to the model); (c) cross-family transfer via a learned/abstracted family boundary; (d) more tasks per cell (>=60) and a second environment; (e) measure maintenance under drift.
