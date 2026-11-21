# Dataset v2: baseline headroom and first capability evaluations (real model)

Model `google/gemini-2.5-flash` (Vertex AI, global, temperature 0). Private validation and
hidden-transfer splits generated from a fresh secret seed (outside the repository, not recorded).
Everything below ran through the unchanged M3/M4 pipeline and the **unchanged `default-low`
promotion policy**.

## 1. The benchmark now has headroom

| Suite | v1 baseline | v2 baseline (3 seeds per task) |
|---|---|---|
| Discovery | 10/12 (83%) | 22/64 (34%) over 4 seeds |
| Validation (private) | 6/8 (75%) | 16/36 (44%, CI 30-60%) |
| Hidden transfer (private) | 8/8 (100%) | 10/36 (28%, CI 16-44%) |

Per-variant results and the full table: [baseline-report-v2-vertex.md](../baseline-report-v2-vertex.md).
The baseline fails mostly by *confidently under-sizing* (every failure is a false "done" claim that
the independent verifier caught), typically because it never reads the log (connections per
request) or reads `infra/` without combining it with the replica count / retry budget. All 72
v2 tasks are proven solvable from the files alone (`tests/unit/test_dataset_v2.py`).

## 2. What the learning plane found on v2 (and a compiler fix it exposed)

64 real discovery runs (4 seeds; 22 verified successes, 42 failures) were persisted and learned from
by `ee-learn`, which ran **without** evaluator access. With `rule-compiler/2` the log read was left
out of the procedure: only ~40% of successes read it (below the 60% skeleton support) although
**none** of the failures did. Contrastive analysis already had that signal but did not use it.
`rule-compiler/3` adds *read-only* observation steps that discriminate (>= 30% of successes, <= 20% of
failures, gap >= 30 points, >= 3 failures); it never adds a mutating step. This is the first case in
which failed runs improved the procedure, and it was found by looking at the evidence rather than
planned in advance.

## 3. Evaluation under the unchanged gates (one seed, n = 6 per suite)

| Capability | Source replay C (A) | Held-out C (A) | Transfer C (A) | Negative | Tokens / success C vs A | Verdict |
|---|---|---|---|---|---|---|
| `pool.f12f4fa920` v1 | 88% (38%) | **67%** (33%) | 100% (50%) | 8/8 | ~2.0-2.6k vs ~14-17k | **REJECTED**: held-out 67% < 75% |
| `timeout.e2fb15fa79` v1 | 75% (62%) | 100% (33%) | **67%** (33%) | 8/8 | ~3.0-3.7k vs ~12-17k | **REJECTED**: source replay 75% < 80%, transfer 67% < 75% |

Interpretation, without flattering the result:
* **Real correctness value appears for the first time.** Arm C beats the baseline and the
  memory-only control on every suite (e.g. transfer 100% vs 50% for pool; held-out 100% vs 33% for
  timeout) at roughly one-sixth of the tokens. On v1 the baseline was already at the ceiling, so
  this effect could not be seen.
* **Neither capability was promoted, and the policy was not changed.** Each missed one gate by about one
  task (6 tasks per suite: one failure is 17 points, and the gates need 5/6). Sample sizes this
  small make the gates noisy in *both* directions; a REJECT here is the conservative outcome, and
  the CIs (e.g. held-out+transfer lower bound 55%) overlap with the baseline's.
* The capabilities' remaining failures are `VERIFY_FAILED` after an LLM decision, i.e. the same
  kind of sizing mistake as the baseline's, made less often because the procedure forces the
  relevant files to be read. They were not analysed run by run.
* Lifecycle cost: with control-arm tokens charged, tokens per verified success are 15.0k (pool) and
  18.2k (timeout) against a baseline execution-only 14.9k / 14.3k, i.e. **not yet amortised** at
  this evaluation size. Reuse over many future tasks is what recovers the cost.
* No retries or re-evaluation were performed to "get a pass": a REJECTED version is terminal, and
  re-sampling the same private data until it passes would invalidate the gate. Another attempt
  needs a changed procedure (a new immutable version) **and fresh private data**.

## 4. Gate for the next step

The criterion "at least one capability reaches ACTIVE through the real gates" is **not met**.
What is met: headroom exists, the learning pipeline benefits from it, and capabilities show a
measurable advantage. Options, in the order I would take them:
1. Increase the evidence per suite (more tasks per variant and several seeds *inside* the
   evaluation, e.g. 12+ held-out and transfer tasks) so the 75% gates are statistically
   meaningful, then re-learn on fresh seeds and re-evaluate on a new private seed. Supported by the
   dataset generator; needs `ee-eval` multi-seed support (it records seeds but runs one).
2. Improve decision reliability (a reviewed prompt template `select_fix_parameters/2`), evaluated
   only on fresh private data.
3. Proceed to M6 with capabilities served from the experimental catalog for offline policy
   analysis only. Not recommended before (1).
