# Fresh-session capability runtime vs baseline (Gemini 2.5 Flash)

> **EXPERIMENTAL CATALOG.** Arm C used capabilities that were **not promoted** to ACTIVE by the evaluation workflow. This measures the runtime mechanism only; it is not evidence that any capability is approved, and must not be used to promote one.

## Setup

- Paired tasks: 16 (identical task and environment per arm; every run in a fresh session; arm C sees only capabilities in the catalog, no transcripts).
- Arm A: `A_BASELINE`, policy `fixed-general-planning/1`.
- Arm C: `C_CAPABILITIES`, policy `rule-capability/1`.
- Model: `vertex-ai:global` / `google/gemini-2.5-flash`.
- Dataset: `connection-debug-sim` v1.0.0-private, seed 0.

## Outcomes

| Arm | Runs | Verified | Verified success (95% Wilson CI) | Fail/Partial/Unknown | False claims | Tokens / verified success | Tool calls / verified success | Mean latency ms | Human interventions |
|---|---|---|---|---|---|---|---|---|---|
| A baseline | 16 | 16 | 100% (81%-100%) | 0/0/0 | 0 | 4961 | 4.6 | 32649 | 0 |
| C capability runtime | 16 | 16 | 100% (81%-100%) | 0/0/0 | 0 | 1791 | 5.0 | 12237 | 0 |

Paired: both succeed 16, only C 0, only A 0, neither 0.

Confidence intervals **overlap**: the difference is not statistically distinguishable at this sample size.

## Capability usage (arm C)

- Strategies selected: {'DIRECT_CAPABILITY': 16}
- Runs that used a capability: 16/16; versions: {'pool.633bfc2335@1': 8, 'timeout.416789d902@1': 8}
- Fell back to general planning: 0
- Capability aborts: 0; escalations: 0

## Notes

- Success is decided only by the independent verifier from final environment state.
- Tokens and tool calls include capability-internal decisions and any fallback work.
- The cost of learning and evaluating the capability is **not** in this table; see the evaluation report for it. Lifecycle cost must be amortised over expected reuse.
- Differences need more tasks/seeds before being treated as improvements.

## How this was produced, and how to read it

1. 12 real Gemini 2.5 Flash discovery runs were persisted (10 verified successes, 2 failures) and
   flowed through relay → RabbitMQ → worker → `ee-learn compile` (run **without** evaluator access).
   That produced two `CANDIDATE` capabilities (`pool.633bfc2335@1`, `timeout.416789d902@1`).
2. Private validation + hidden-transfer splits were generated from a fresh secret seed (not any seed
   used in M3/M4). Arms A and C ran the same 16 tasks, each in a new session. Arm C's runtime was
   built only from the database catalog; it had no transcripts or in-process learning state.
3. The candidates were **not** evaluated or promoted in this experiment. Arm C was therefore served
   from an explicitly **experimental** catalog (`--experimental-capability-states CANDIDATE`). In
   production the default catalog is ACTIVE-only and would have served nothing (tested).
4. All 16 arm-C runs and their decisions, outcomes (`capability_version`) and trace events
   (`CAPABILITY_MATCH`, `TOOL_CALL`, `CAPABILITY_EXEC`, `VERIFICATION`) were persisted in PostgreSQL.

What it shows:
* The runtime mechanism works with a real model in fresh sessions: both families, including the
  unseen hikari/okhttp layouts, were matched by deterministic eligibility, executed and verified by
  the independent verifier, with no fallback, abort or escalation needed.
* Cost: ~1.8k vs ~5.0k tokens per verified success (-64%) and ~12 s vs ~33 s mean latency
  (-62%). Tool calls per success are slightly higher (5.0 vs 4.6).

What it does not show:
* **No correctness gain.** The baseline already solves 16/16 (ceiling effect), so the benchmark
  has no headroom for a success-rate improvement; the CIs overlap completely. The value found is
  cost/latency only.
* Not promotion evidence (experimental catalog; one seed; 16 tasks; the M4 evaluation gates were not
  applied to these versions). The learning/evaluation cost of the capability is not charged here.
  Earlier M4 evaluations of earlier compilations of the same capabilities were REJECTED on the
  source-replay gate; these new candidates would still need to pass that workflow.
* Single run, a real model that is non-deterministic even at temperature 0.
