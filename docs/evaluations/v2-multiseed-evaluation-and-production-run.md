# Dataset v2: multi-seed evaluation, first promotion, and the production-mode A vs C run

Model `google/gemini-2.5-flash` (Vertex AI, global, temperature 0). Policy `default-low`, **unchanged**.

## What changed in the procedure
* `ee-eval evaluate --seeds 0 1 --workers 8` re-runs every task once per seed, concurrently,
  and keeps results in plan order. Regressions are judged per (task, seed) pair.
* Suites are larger: private validation and hidden-transfer data were generated with `--scale 3`
  (18 tasks per family in each), so held-out and transfer are 18 tasks x 2 seeds = 36 runs per arm.
* Sample-size gates count **distinct tasks**, not repeated runs (repeats of one task are
  correlated). The reports show "36 (18 tasks)" and warn that the Wilson intervals are optimistic.
* Fresh data everywhere: a new public discovery seed for learning (64 real runs, 20 verified
  successes, 44 failures), a new secret private seed for evaluation (x3), and a **third** secret
  private seed (x2) for the final A vs C comparison, which was never used for any gating decision.

## Step 1: evaluation (2 seeds)

| | Source replay C (A / B) | Held-out C (A / B) | Transfer C (A / B) | Negative | Regressions | Verdict |
|---|---|---|---|---|---|---|
| `pool.f12f4fa920` v1 | 100% (31% / 38%) | **94%** (25% / 31%) | **97%** (25% / 50%) | 8/8 | 1 (max 1) | **PASS** -> APPROVED |
| `timeout.e2fb15fa79` v1 | 94% (31% / 94%) | 94% (44% / 92%) | **83%** (33% / 97%) | 8/8 | **3** (max 1) | **FAIL** -> REJECTED: 3 regressions > 1 |

* Pool: the capability roughly triples-to-quadruples the baseline's success at ~1/10 of the tokens
  (~2.1k vs ~20k per verified success). 95% lower bound on held-out+transfer: 88%. Lifecycle
  tokens per verified success (control-arm evaluation cost charged): 13.4k vs 19.6k for the baseline,
  i.e. already amortised on this run.
* Timeout: **the memory-only control (one episodic hint) is as correct as, or more correct than, the
  capability** (transfer 97% vs 83%) at ~2.7x the capability's tokens. The hint tells the agent to read
  the log, which was the missing step. The capability still beats the baseline, but it did not
  beat the cheaper-to-build control on correctness, and it regressed on 3 task/seed pairs.
  This is a finding for M7 (stronger memory baselines) and for the compiler (procedure for this
  family leaves the decision step under-constrained), not something to hide by relaxing a gate.

## Step 2: rollout of the approved capability
Shadow (6 observations) and canary (6) via `ee-eval observe`, then `activate`: `pool.f12f4fa920`
v1 is **ACTIVE**, `timeout.e2fb15fa79` v1 stays REJECTED. Honest limit: the observations are
evaluator-driven runs on the first 6 validation tasks (which are also in the held-out suite), not live
traffic; true shadow/canary routing is still not wired.

## Step 3: production-mode A vs C on v2 (fresh private seed, 48 tasks x 2 seeds)

Arm C used the default ACTIVE-only catalog (no experimental flag; no run carries the
`capability_catalog` tag; 0 capability-assisted runs entered learning). All 96 C runs persisted a
decision (`rule-capability/1`, probability 1.0); 48 were `DIRECT_CAPABILITY` and 48 `GENERAL_PLANNING`.
Full report: [v2-production-runtime-vs-baseline-gemini-2.5-flash.md](./v2-production-runtime-vs-baseline-gemini-2.5-flash.md).

| | A baseline | C capability runtime |
|---|---|---|
| All 96 paired runs | 27 verified (28%, CI 20-38%) | **68 verified (71%, CI 61-79%)** |
| Tokens per verified success | 21.7k | 6.5k |
| Mean latency | 63 s | 44 s |
| **pool family (48 runs)** | 10 (21%) | **45 (94%)**; 3 failures, 4 fallbacks |
| timeout family (48 runs) | 17 (35%) | 23 (48%) |

Paired: both succeed 22, only C 46, only A 5, neither 23. Intervals for the whole set do not overlap.

How to read it:
* **The gain is entirely the pool family**, where an ACTIVE capability served the tasks
  (21% -> 94%). In the timeout family arm C used general planning (no ACTIVE capability), so
  35% vs 48% is the **same policy measured twice**: it shows the run-to-run noise (~13 points at
  this size), which is the yardstick for everything else here.
* Both pool variants and the unseen hikari layout (24/24 vs 2/24) were solved; three runs
  failed verification and four fell back to general planning.
* Runs are correlated across seeds; the Wilson intervals are optimistic. One model, one benchmark.

## Gate status
"At least one capability reaches ACTIVE through the real gates, and fresh-session production use
shows measurable correctness value": **met for the pool family**, with the caveats above (a
procedure identical to the one rejected earlier on a 6-task suite; see below).

### Disclosure: this is a second evaluation of the same pool procedure
The compiler produced the same pool procedure (`pool.f12f4fa920`) from fresh discovery data, and
an earlier evaluation of it on a 6-task suite was REJECTED (held-out 67% < 75%, one task short).
The evaluation design was then changed (more tasks, repeated seeds, task-count sample gates) because
the earlier suite could not resolve the 75% gate, and the new result is on different private data.
That is legitimate, but it is a second look at the same procedure, so the PASS should be
confirmed by the third-seed comparison above (94% on pool; consistent) and by live rollout later.
The gates themselves were not changed.

## Known limitations
* Single model; simulated environment; two task families.
* Shadow/canary not live; observations reuse validation tasks.
* `max_regressions=1` is an absolute count: with more runs per evaluation it gets stricter (it
  rejected the timeout capability with 3 of 33 pairs). Scaling it with n is a policy decision for
  review, not done here.
* The timeout capability is unpromoted; its procedure should be improved or retired.
