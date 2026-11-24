# Capability vs memory (cross-model): gemini-2.5-pro

Research question: with equivalent historical experience, does a compiled, validated, executable capability (E) outperform memory-based reuse (B episodic, C consolidated, D instructions) and the stateless baseline (A)? The design is [capability-vs-memory-design.md](../experiments/capability-vs-memory-design.md); the experiment is built to be able to falsify the hypothesis.

## 1. Configuration (reproducibility)

- Model: `vertex-ai:global` / `google/gemini-2.5-pro` (temperature 0.0); repetitions per task: 1; workers: 8. Repetitions are repeated draws from a non-deterministic model (the provider ignores seeds), not independent samples.
- Arms: A stateless baseline, B episodic memory, C consolidated memory, D instruction reuse, E compiled capability.
- Datasets: HIDDEN_TRANSFER: `connection-debug-sim` v3.0.0-private `1591f2d3c2d0…`; VALIDATION: `connection-debug-sim` v3.0.0-private `bbd93b374080…`
- Discovery experience: 63 eligible trajectories (DISCOVERY split only), families ['pool', 'timeout']; the same set feeds B, C, D and the compiler.
- Capabilities available to D and E: pool.f12f4fa920@1, timeout.de1091cd4d@1 (catalog states ['APPROVED', 'REJECTED']). **Experimental catalog: not every version is ACTIVE; this is a research run, not promotion evidence.**
- Memory preamble sha256 `c1196b1a6e4e92f9…`; up to 3 episodes; token estimator `chars/4`; verifier `sim-verifier/1`; runtime `baseline-runtime/0.1.0`; evaluator `evaluator/1`.
- Runs: 150 (0 UNKNOWN, excluded as missing data); distinct tasks: 30.
- Note: environment sim-env/1

## 2. Success by arm

Cells are the mean of per-task success rates (each task weighted equally); the number of distinct tasks is shown next to the group.

| Group | tasks | A | B | C | D | E |
|---|---|---|---|---|---|---|
| all tasks | 30 | 63% | 60% | 83% | 80% | 83% |

### By task family

| Family | tasks | A | B | C | D | E |
|---|---|---|---|---|---|---|
| pool | 12 | 50% | 75% | 100% | 100% | 100% |
| queue | 6 | 17% | 0% | 33% | 0% | 17% |
| timeout | 12 | 100% | 75% | 92% | 100% | 100% |

### By transfer level

`intra_variant`: seen libraries, new numbers. `cross_variant`: a learned family with an unseen library/layout. `cross_family`: a family never seen in discovery.

| Level | tasks | A | B | C | D | E |
|---|---|---|---|---|---|---|
| cross_family | 6 | 17% | 0% | 33% | 0% | 17% |
| cross_variant | 12 | 67% | 67% | 100% | 100% | 100% |
| intra_variant | 12 | 83% | 83% | 92% | 100% | 100% |

### By variant

| Variant | tasks | A | B | C | D | E |
|---|---|---|---|---|---|---|
| pool/hikari_shared | 6 | 33% | 50% | 100% | 100% | 100% |
| pool/psycopg_shared | 3 | 67% | 100% | 100% | 100% | 100% |
| pool/sqlalchemy_shared | 3 | 67% | 100% | 100% | 100% | 100% |
| queue/amqp_channels | 6 | 17% | 0% | 33% | 0% | 17% |
| timeout/httpx_budget | 3 | 100% | 100% | 67% | 100% | 100% |
| timeout/okhttp_budget | 6 | 100% | 83% | 100% | 100% | 100% |
| timeout/requests_budget | 3 | 100% | 33% | 100% | 100% | 100% |

## 3. Paired statistical comparisons

Unit = distinct task; repetitions are averaged inside a task. Paired bootstrap over tasks (10,000 resamples, fixed seed); exact McNemar on tasks "solved" in >= 50% of repetitions; Holm correction within each table. Wilson intervals are not used for inference here.

### 3.1 Primary: compiled capability (E) against every alternative

| Comparison | tasks | rate X | rate Y | mean paired diff [95% bootstrap CI] | discordant X-only/Y-only | McNemar p | Holm-adj p | reading |
|---|---|---|---|---|---|---|---|---|
| E vs A | 30 | 83% | 63% | +20% [+7%, +37%] | 6/0 | 0.031 | 0.094 | E ahead by bootstrap only (McNemar n.s.) |
| E vs B | 30 | 83% | 60% | +23% [+10%, +40%] | 7/0 | 0.016 | 0.062 | E ahead by bootstrap only (McNemar n.s.) |
| E vs C | 30 | 83% | 83% | +0% [-10%, +10%] | 1/1 | 1.000 | 1.000 | not distinguishable |
| E vs D | 30 | 83% | 80% | +3% [+0%, +10%] | 1/0 | 1.000 | 1.000 | not distinguishable |

E vs the best memory arm (**C**, chosen *post hoc* by overall rate, so this comparison favours memory):

| Comparison | tasks | rate X | rate Y | mean paired diff [95% bootstrap CI] | discordant X-only/Y-only | McNemar p | Holm-adj p | reading |
|---|---|---|---|---|---|---|---|---|
| E vs C | 30 | 83% | 83% | +0% [-10%, +10%] | 1/1 | 1.000 | 1.000 | not distinguishable |

### 3.2 Attribution ladder (where does any improvement come from?)

A->B/C: value of *information*. C->D: value of a procedure stated as *instructions*. D->E: value of *executing* the procedure instead of asking the model to follow it.

| Comparison | tasks | rate X | rate Y | mean paired diff [95% bootstrap CI] | discordant X-only/Y-only | McNemar p | Holm-adj p | reading |
|---|---|---|---|---|---|---|---|---|
| B vs A | 30 | 60% | 63% | -3% [-23%, +17%] | 4/5 | 1.000 | 1.000 | not distinguishable |
| C vs A | 30 | 83% | 63% | +20% [+3%, +37%] | 7/1 | 0.070 | 0.281 | C ahead by bootstrap only (McNemar n.s.) |
| C vs B | 30 | 83% | 60% | +23% [+7%, +40%] | 8/1 | 0.039 | 0.195 | C ahead by bootstrap only (McNemar n.s.) |
| D vs C | 30 | 80% | 83% | -3% [-13%, +7%] | 1/2 | 1.000 | 1.000 | not distinguishable |
| E vs D | 30 | 83% | 80% | +3% [+0%, +10%] | 1/0 | 1.000 | 1.000 | not distinguishable |

### 3.3 E vs alternatives, intra_variant tasks only

| Comparison | tasks | rate X | rate Y | mean paired diff [95% bootstrap CI] | discordant X-only/Y-only | McNemar p | Holm-adj p | reading |
|---|---|---|---|---|---|---|---|---|
| E vs A | 12 | 100% | 83% | +17% [+0%, +42%] | 2/0 | 0.500 | 1.000 | not distinguishable |
| E vs B | 12 | 100% | 83% | +17% [+0%, +42%] | 2/0 | 0.500 | 1.000 | not distinguishable |
| E vs C | 12 | 100% | 92% | +8% [+0%, +25%] | 1/0 | 1.000 | 1.000 | not distinguishable |
| E vs D | 12 | 100% | 100% | +0% [+0%, +0%] | 0/0 | 1.000 | 1.000 | not distinguishable |

### 3.3 E vs alternatives, cross_variant tasks only

| Comparison | tasks | rate X | rate Y | mean paired diff [95% bootstrap CI] | discordant X-only/Y-only | McNemar p | Holm-adj p | reading |
|---|---|---|---|---|---|---|---|---|
| E vs A | 12 | 100% | 67% | +33% [+8%, +58%] | 4/0 | 0.125 | 0.500 | E ahead by bootstrap only (McNemar n.s.) |
| E vs B | 12 | 100% | 67% | +33% [+8%, +58%] | 4/0 | 0.125 | 0.500 | E ahead by bootstrap only (McNemar n.s.) |
| E vs C | 12 | 100% | 100% | +0% [+0%, +0%] | 0/0 | 1.000 | 1.000 | not distinguishable |
| E vs D | 12 | 100% | 100% | +0% [+0%, +0%] | 0/0 | 1.000 | 1.000 | not distinguishable |

### 3.3 E vs alternatives, cross_family tasks only

| Comparison | tasks | rate X | rate Y | mean paired diff [95% bootstrap CI] | discordant X-only/Y-only | McNemar p | Holm-adj p | reading |
|---|---|---|---|---|---|---|---|---|
| E vs A | 6 | 17% | 17% | +0% [+0%, +0%] | 0/0 | 1.000 | 1.000 | not distinguishable |
| E vs B | 6 | 17% | 0% | +17% [+0%, +50%] | 1/0 | 1.000 | 1.000 | not distinguishable |
| E vs C | 6 | 17% | 33% | -17% [-50%, +0%] | 0/1 | 1.000 | 1.000 | not distinguishable |
| E vs D | 6 | 17% | 0% | +17% [+0%, +50%] | 1/0 | 1.000 | 1.000 | not distinguishable |

### 3.4 E vs alternatives, family `pool` only

| Comparison | tasks | rate X | rate Y | mean paired diff [95% bootstrap CI] | discordant X-only/Y-only | McNemar p | Holm-adj p | reading |
|---|---|---|---|---|---|---|---|---|
| E vs A | 12 | 100% | 50% | +50% [+25%, +75%] | 6/0 | 0.031 | 0.125 | E ahead by bootstrap only (McNemar n.s.) |
| E vs B | 12 | 100% | 75% | +25% [+0%, +50%] | 3/0 | 0.250 | 0.750 | not distinguishable |
| E vs C | 12 | 100% | 100% | +0% [+0%, +0%] | 0/0 | 1.000 | 1.000 | not distinguishable |
| E vs D | 12 | 100% | 100% | +0% [+0%, +0%] | 0/0 | 1.000 | 1.000 | not distinguishable |

### 3.4 E vs alternatives, family `queue` only

| Comparison | tasks | rate X | rate Y | mean paired diff [95% bootstrap CI] | discordant X-only/Y-only | McNemar p | Holm-adj p | reading |
|---|---|---|---|---|---|---|---|---|
| E vs A | 6 | 17% | 17% | +0% [+0%, +0%] | 0/0 | 1.000 | 1.000 | not distinguishable |
| E vs B | 6 | 17% | 0% | +17% [+0%, +50%] | 1/0 | 1.000 | 1.000 | not distinguishable |
| E vs C | 6 | 17% | 33% | -17% [-50%, +0%] | 0/1 | 1.000 | 1.000 | not distinguishable |
| E vs D | 6 | 17% | 0% | +17% [+0%, +50%] | 1/0 | 1.000 | 1.000 | not distinguishable |

### 3.4 E vs alternatives, family `timeout` only

| Comparison | tasks | rate X | rate Y | mean paired diff [95% bootstrap CI] | discordant X-only/Y-only | McNemar p | Holm-adj p | reading |
|---|---|---|---|---|---|---|---|---|
| E vs A | 12 | 100% | 100% | +0% [+0%, +0%] | 0/0 | 1.000 | 1.000 | not distinguishable |
| E vs B | 12 | 100% | 75% | +25% [+0%, +50%] | 3/0 | 0.250 | 1.000 | not distinguishable |
| E vs C | 12 | 100% | 92% | +8% [+0%, +25%] | 1/0 | 1.000 | 1.000 | not distinguishable |
| E vs D | 12 | 100% | 100% | +0% [+0%, +0%] | 0/0 | 1.000 | 1.000 | not distinguishable |

## 4. Execution efficiency and equal-budget success

Tokens include the retrieved memory, which sits inside every prompt of the run.

| Arm | context tokens (est., first call) | retrieved tokens | tokens/run | tokens/verified success |
|---|---|---|---|---|
| A stateless baseline | 307 | 0 | 9,419 | 14,872 |
| B episodic memory | 499 | 192 | 10,526 | 17,543 |
| C consolidated memory | 516 | 208 | 11,170 | 13,404 |
| D instruction reuse | 675 | 368 | 11,321 | 14,151 |
| E compiled capability | n/a (runtime arm) | 0 | 6,837 | 8,205 |

Success rate when a run only counts if it verified within a token cap (equal resource budget):

| Arm | <= 3,000 | <= 6,000 | <= 12,000 | <= 24,000 | <= 60,000 |
|---|---|---|---|---|---|
| A stateless baseline | 0% | 0% | 60% | 63% | 63% |
| B episodic memory | 0% | 0% | 53% | 60% | 60% |
| C consolidated memory | 0% | 0% | 63% | 83% | 83% |
| D instruction reuse | 0% | 0% | 77% | 80% | 80% |
| E compiled capability | 50% | 57% | 60% | 83% | 83% |

## 5. Lifecycle cost and break-even

`lifecycle_cost(N) = L + N * e + M`; break-even is the number of future *verified successes* after which the learning cost is recovered relative to the stateless baseline (`N* = L / (c_A - c_X)`, `c` = tokens per verified success, failures' cost included). Maintenance M = 0 tokens (**not measured**, which flatters every learning arm).

Learning ledger: discovery 381,533 tokens over 63 runs (263 tool calls); analysis 0; consolidation 0 (0 ms); compilation 0 (1251 ms); validation 1,808,832.
- analysis/compilation/consolidation are deterministic (0 model tokens); only wall time is charged
- discovery cost is the model tokens of the discovery runs that every learning arm reuses

### 5.1 As built (only the capability pays for validation)

| Arm | runs | success | tokens/run | tokens/success | tool calls/run | latency s/run | retrieved tok/run | retrieval ms | learning tokens L | break-even (verified successes) |
|---|---|---|---|---|---|---|---|---|---|---|
| A stateless baseline | 30 | 63% | 9,419 | 14,872 | 4.8 | 61 | 0 | 0.0 | 0 | reference |
| B episodic memory | 30 | 60% | 10,526 | 17,543 | 4.8 | 60 | 192 | 6.4 | 381,533 | never (execution is not cheaper per success: never recovered) |
| C consolidated memory | 30 | 83% | 11,170 | 13,404 | 5.0 | 62 | 208 | 0.0 | 381,533 | 260 |
| D instruction reuse | 30 | 80% | 11,321 | 14,151 | 5.0 | 59 | 368 | 0.0 | 381,533 | 529 |
| E compiled capability | 30 | 83% | 6,837 | 8,205 | 5.8 | 48 | 0 | 0.0 | 2,190,365 | 329 |

### 5.2 Sensitivity: every learning arm pays the same validation cost

| Arm | runs | success | tokens/run | tokens/success | tool calls/run | latency s/run | retrieved tok/run | retrieval ms | learning tokens L | break-even (verified successes) |
|---|---|---|---|---|---|---|---|---|---|---|
| A stateless baseline | 30 | 63% | 9,419 | 14,872 | 4.8 | 61 | 0 | 0.0 | 0 | reference |
| B episodic memory | 30 | 60% | 10,526 | 17,543 | 4.8 | 60 | 192 | 6.4 | 2,190,365 | never (execution is not cheaper per success: never recovered) |
| C consolidated memory | 30 | 83% | 11,170 | 13,404 | 5.0 | 62 | 208 | 0.0 | 2,190,365 | 1,492 |
| D instruction reuse | 30 | 80% | 11,321 | 14,151 | 5.0 | 59 | 368 | 0.0 | 2,190,365 | 3,037 |
| E compiled capability | 30 | 83% | 6,837 | 8,205 | 5.8 | 48 | 0 | 0.0 | 2,190,365 | 329 |

### 5.3 Family `pool` (family-specific learning ledger)

| Arm | runs | success | tokens/run | tokens/success | tool calls/run | latency s/run | retrieved tok/run | retrieval ms | learning tokens L | break-even (verified successes) |
|---|---|---|---|---|---|---|---|---|---|---|
| A stateless baseline | 12 | 50% | 8,499 | 16,998 | 4.5 | 59 | 0 | 0.0 | 0 | reference |
| B episodic memory | 12 | 75% | 8,977 | 11,970 | 4.8 | 53 | 193 | 11.5 | 168,870 | 34 |
| C consolidated memory | 12 | 100% | 9,333 | 9,333 | 5.0 | 53 | 196 | 0.0 | 168,870 | 22 |
| D instruction reuse | 12 | 100% | 9,915 | 9,915 | 5.0 | 52 | 345 | 0.0 | 168,870 | 24 |
| E compiled capability | 12 | 100% | 2,214 | 2,214 | 5.0 | 16 | 0 | 0.0 | 1,061,389 | 72 |

### 5.3 Family `timeout` (family-specific learning ledger)

| Arm | runs | success | tokens/run | tokens/success | tool calls/run | latency s/run | retrieved tok/run | retrieval ms | learning tokens L | break-even (verified successes) |
|---|---|---|---|---|---|---|---|---|---|---|
| A stateless baseline | 12 | 100% | 9,996 | 9,996 | 5.0 | 61 | 0 | 0.0 | 0 | reference |
| B episodic memory | 12 | 75% | 11,156 | 14,874 | 4.7 | 63 | 191 | 1.8 | 212,663 | never (execution is not cheaper per success: never recovered) |
| C consolidated memory | 12 | 92% | 12,550 | 13,691 | 5.0 | 66 | 162 | 0.0 | 212,663 | never (execution is not cheaper per success: never recovered) |
| D instruction reuse | 12 | 100% | 11,132 | 11,132 | 5.0 | 56 | 278 | 0.0 | 212,663 | never (execution is not cheaper per success: never recovered) |
| E compiled capability | 12 | 100% | 9,630 | 9,630 | 6.9 | 71 | 0 | 0.0 | 1,128,976 | 3,082 |

## 6. Compiled capability: applicability, fallback and boundaries

| Family | tasks | eligible capability found | used | fell back to planning |
|---|---|---|---|---|
| pool | 12 | 100% | 100% | 0% |
| queue | 6 | 0% | 0% | 0% |
| timeout | 12 | 100% | 100% | 58% |

Applicability gate (deterministic, no model): tasks admitted per family and synthetic precondition violations admitted (must be 0).

| Capability | learned family | admitted/total by family | synthetic violations admitted |
|---|---|---|---|
| pool.f12f4fa920@1 | pool | pool: 12/12, queue: 0/6, timeout: 0/12 | 0/4 |
| timeout.de1091cd4d@1 | timeout | pool: 0/12, queue: 0/6, timeout: 12/12 | 0/4 |

## 7. Failure and regression analysis

Non-verified runs by termination:

| Arm | non-verified runs | termination counts |
|---|---|---|
| A stateless baseline | 11 | {'FINAL': 11} |
| B episodic memory | 12 | {'FINAL': 11, 'INVALID_ACTION': 1} |
| C consolidated memory | 5 | {'FINAL': 5} |
| D instruction reuse | 6 | {'FINAL': 6} |
| E compiled capability | 5 | {'FINAL': 5} |

Regressions against the baseline: tasks A solved (>= half of repetitions) that the arm did not.

| Arm | regressed tasks | improved tasks | task ids that regressed |
|---|---|---|---|
| B episodic memory | 5 | 4 | hid-pool-hikari_shared-03, hid-queue-amqp_channels-01, hid-timeout-okhttp_budget-01, val-timeout-requests_budget-00, val-timeout-requests_budget-01 |
| C consolidated memory | 1 | 7 | val-timeout-httpx_budget-00 |
| D instruction reuse | 1 | 6 | hid-queue-amqp_channels-01 |
| E compiled capability | 0 | 6 | - |

## 8. Mechanical verdicts (restating the paired statistics, not interpreting them)

- Does E outperform episodic memory? Paired difference +23% [+10%, +40%] over 30 tasks; McNemar p=0.016; reading: **E better (bootstrap and McNemar)**.
- Does E outperform consolidated memory? Paired difference +0% [-10%, +10%] over 30 tasks; McNemar p=1.000; reading: **not distinguishable**.
- Does E outperform instruction-based reuse? Paired difference +3% [+0%, +10%] over 30 tasks; McNemar p=1.000; reading: **not distinguishable**.
- Does E outperform the stateless baseline? Paired difference +20% [+7%, +37%] over 30 tasks; McNemar p=0.031; reading: **E better (bootstrap and McNemar)**.

## 9. Statistical assumptions and limitations

- Tasks inside a family come from one generator and are treated as exchangeable draws; results do not generalise beyond these families, this simulated environment and the model(s) tested.
- Repetitions are collapsed inside each task, so they sharpen the per-task rate but add no tasks; with a few dozen tasks the intervals are wide.
- McNemar binarises each task and discards the size of the difference; the bootstrap keeps it. They can disagree at small n.
- Holm adjusts within each table only; across the whole report many comparisons are made and isolated small p-values should not be read as findings.
- Token counts include retrieval in the prompt but no price list is applied; maintenance cost is an input, not a measurement.
- Memory and instruction texts were fixed before any evaluation and never tuned on evaluation data; a better-engineered memory could do better.
