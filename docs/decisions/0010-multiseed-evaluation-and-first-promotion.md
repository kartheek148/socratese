# ADR 0010 — Multi-seed evaluation, task-count sample gates, first promotion

Status: Accepted

## Decisions
1. **Evaluation repeats tasks per seed** (`EvaluationPlan.seeds`, `ee-eval --seeds`), concurrently (`--workers`) with results kept in plan order. Refusal (negative) checks run once because they are decided before any step executes. `TaskResult.seed` records the repetition.
2. **Sample-size gates count distinct tasks** (`ArmMetrics.n_tasks`). Repeating one task adds precision about model variance, not new evidence about generalisation; counting runs would let 2 tasks x 6 seeds pass a 4-task gate (tested). Rates and Wilson intervals still use runs and the report says they are optimistic.
3. **Larger private suites** via `ee-bench generate --private --scale N`; scaled suites extend the base suite (task i is unchanged), and the version string records the scale.
4. **Promotion policy not changed.** `default-low` is identical. It promoted `pool.f12f4fa920` and rejected `timeout.e2fb15fa79` (3 regressions > 1) even though that capability beat the baseline, because the memory-only control matched it on correctness.
5. **Final A vs C comparison uses a third private seed** that played no role in any gate, with the default ACTIVE-only catalog.
6. **Disclosed re-evaluation.** The pool procedure was compiled identically from fresh data after an earlier REJECT on a 6-task suite; the evaluation design (not the gates) changed because that suite could not resolve the gate. Confirmed by the independent third-seed run, still to be confirmed by live rollout.

## Consequences / open
Live shadow/canary routing; scaling `max_regressions` with n (policy review); improving or retiring the timeout capability; stronger memory baseline (M7) since the one-hint control was competitive; M6 now has one ACTIVE capability and a family without one, which is a real choice set for the controller.
