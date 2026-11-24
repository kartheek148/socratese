# Capability vs memory: gemini-2.5-flash

Research question: with equivalent historical experience, does a compiled, validated, executable capability (E) outperform memory-based reuse (B episodic, C consolidated, D instructions) and the stateless baseline (A)? The design is [capability-vs-memory-design.md](../experiments/capability-vs-memory-design.md); the experiment is built to be able to falsify the hypothesis.

## 1. Configuration (reproducibility)

- Model: `vertex-ai:global` / `google/gemini-2.5-flash` (temperature 0.0); repetitions per task: 2; workers: 8. Repetitions are repeated draws from a non-deterministic model (the provider ignores seeds), not independent samples.
- Arms: A stateless baseline, B episodic memory, C consolidated memory, D instruction reuse, E compiled capability.
- Datasets: HIDDEN_TRANSFER: `connection-debug-sim` v3.0.0-private-x2 `83d1359282a6…`; VALIDATION: `connection-debug-sim` v3.0.0-private-x2 `4d3213253423…`
- Discovery experience: 63 eligible trajectories (DISCOVERY split only), families ['pool', 'timeout']; the same set feeds B, C, D and the compiler.
- Capabilities available to D and E: pool.f12f4fa920@1, timeout.de1091cd4d@1 (catalog states ['CANDIDATE', 'VALIDATING']). **Experimental catalog: not every version is ACTIVE; this is a research run, not promotion evidence.**
- Memory preamble sha256 `c1196b1a6e4e92f9…`; up to 3 episodes; token estimator `chars/4`; verifier `sim-verifier/1`; runtime `baseline-runtime/0.1.0`; evaluator `evaluator/1`.
- Runs: 600 (0 UNKNOWN, excluded as missing data); distinct tasks: 60.
- Note: environment sim-env/1

## 2. Success by arm

Cells are the mean of per-task success rates (each task weighted equally); the number of distinct tasks is shown next to the group.

| Group | tasks | A | B | C | D | E |
|---|---|---|---|---|---|---|
| all tasks | 60 | 32% | 41% | 48% | 48% | 58% |

### By task family

| Family | tasks | A | B | C | D | E |
|---|---|---|---|---|---|---|
| pool | 24 | 25% | 58% | 90% | 90% | 98% |
| queue | 12 | 17% | 4% | 12% | 4% | 12% |
| timeout | 24 | 46% | 42% | 25% | 29% | 42% |

### By transfer level

`intra_variant`: seen libraries, new numbers. `cross_variant`: a learned family with an unseen library/layout. `cross_family`: a family never seen in discovery.

| Level | tasks | A | B | C | D | E |
|---|---|---|---|---|---|---|
| cross_family | 12 | 17% | 4% | 12% | 4% | 12% |
| cross_variant | 24 | 40% | 56% | 60% | 60% | 73% |
| intra_variant | 24 | 31% | 44% | 54% | 58% | 67% |

### By variant

| Variant | tasks | A | B | C | D | E |
|---|---|---|---|---|---|---|
| pool/hikari_shared | 12 | 33% | 62% | 92% | 88% | 100% |
| pool/psycopg_shared | 6 | 17% | 75% | 100% | 100% | 100% |
| pool/sqlalchemy_shared | 6 | 17% | 33% | 75% | 83% | 92% |
| queue/amqp_channels | 12 | 17% | 4% | 12% | 4% | 12% |
| timeout/httpx_budget | 6 | 25% | 17% | 17% | 0% | 33% |
| timeout/okhttp_budget | 12 | 46% | 50% | 29% | 33% | 46% |
| timeout/requests_budget | 6 | 67% | 50% | 25% | 50% | 42% |

## 3. Paired statistical comparisons

Unit = distinct task; repetitions are averaged inside a task. Paired bootstrap over tasks (10,000 resamples, fixed seed); exact McNemar on tasks "solved" in >= 50% of repetitions; Holm correction within each table. Wilson intervals are not used for inference here.

### 3.1 Primary: compiled capability (E) against every alternative

| Comparison | tasks | rate X | rate Y | mean paired diff [95% bootstrap CI] | discordant X-only/Y-only | McNemar p | Holm-adj p | reading |
|---|---|---|---|---|---|---|---|---|
| E vs A | 60 | 58% | 32% | +27% [+15%, +38%] | 13/4 | 0.049 | 0.147 | E ahead by bootstrap only (McNemar n.s.) |
| E vs B | 60 | 58% | 41% | +18% [+8%, +28%] | 12/2 | 0.013 | 0.052 | E ahead by bootstrap only (McNemar n.s.) |
| E vs C | 60 | 58% | 48% | +10% [+4%, +16%] | 6/2 | 0.289 | 0.359 | E ahead by bootstrap only (McNemar n.s.) |
| E vs D | 60 | 58% | 48% | +10% [+2%, +18%] | 10/4 | 0.180 | 0.359 | E ahead by bootstrap only (McNemar n.s.) |

E vs the best memory arm (**C**, chosen *post hoc* by overall rate, so this comparison favours memory):

| Comparison | tasks | rate X | rate Y | mean paired diff [95% bootstrap CI] | discordant X-only/Y-only | McNemar p | Holm-adj p | reading |
|---|---|---|---|---|---|---|---|---|
| E vs C | 60 | 58% | 48% | +10% [+4%, +16%] | 6/2 | 0.289 | 0.289 | E ahead by bootstrap only (McNemar n.s.) |

### 3.2 Attribution ladder (where does any improvement come from?)

A->B/C: value of *information*. C->D: value of a procedure stated as *instructions*. D->E: value of *executing* the procedure instead of asking the model to follow it.

| Comparison | tasks | rate X | rate Y | mean paired diff [95% bootstrap CI] | discordant X-only/Y-only | McNemar p | Holm-adj p | reading |
|---|---|---|---|---|---|---|---|---|
| B vs A | 60 | 41% | 32% | +9% [-2%, +22%] | 12/13 | 1.000 | 1.000 | not distinguishable |
| C vs A | 60 | 48% | 32% | +17% [+4%, +29%] | 13/8 | 0.383 | 1.000 | C ahead by bootstrap only (McNemar n.s.) |
| C vs B | 60 | 48% | 41% | +8% [-2%, +18%] | 9/3 | 0.146 | 0.730 | not distinguishable |
| D vs C | 60 | 48% | 48% | +0% [-7%, +7%] | 4/6 | 0.754 | 1.000 | not distinguishable |
| E vs D | 60 | 58% | 48% | +10% [+2%, +18%] | 10/4 | 0.180 | 0.730 | E ahead by bootstrap only (McNemar n.s.) |

### 3.3 E vs alternatives, intra_variant tasks only

| Comparison | tasks | rate X | rate Y | mean paired diff [95% bootstrap CI] | discordant X-only/Y-only | McNemar p | Holm-adj p | reading |
|---|---|---|---|---|---|---|---|---|
| E vs A | 24 | 67% | 31% | +35% [+15%, +56%] | 8/2 | 0.109 | 0.438 | E ahead by bootstrap only (McNemar n.s.) |
| E vs B | 24 | 67% | 44% | +23% [+8%, +38%] | 4/0 | 0.125 | 0.438 | E ahead by bootstrap only (McNemar n.s.) |
| E vs C | 24 | 67% | 54% | +12% [+2%, +23%] | 3/1 | 0.625 | 1.000 | E ahead by bootstrap only (McNemar n.s.) |
| E vs D | 24 | 67% | 58% | +8% [-4%, +21%] | 4/2 | 0.688 | 1.000 | not distinguishable |

### 3.3 E vs alternatives, cross_variant tasks only

| Comparison | tasks | rate X | rate Y | mean paired diff [95% bootstrap CI] | discordant X-only/Y-only | McNemar p | Holm-adj p | reading |
|---|---|---|---|---|---|---|---|---|
| E vs A | 24 | 73% | 40% | +33% [+19%, +50%] | 5/0 | 0.062 | 0.250 | E ahead by bootstrap only (McNemar n.s.) |
| E vs B | 24 | 73% | 56% | +17% [-4%, +38%] | 7/2 | 0.180 | 0.539 | not distinguishable |
| E vs C | 24 | 73% | 60% | +12% [+4%, +21%] | 3/0 | 0.250 | 0.539 | E ahead by bootstrap only (McNemar n.s.) |
| E vs D | 24 | 73% | 60% | +12% [+2%, +25%] | 4/1 | 0.375 | 0.539 | E ahead by bootstrap only (McNemar n.s.) |

### 3.3 E vs alternatives, cross_family tasks only

| Comparison | tasks | rate X | rate Y | mean paired diff [95% bootstrap CI] | discordant X-only/Y-only | McNemar p | Holm-adj p | reading |
|---|---|---|---|---|---|---|---|---|
| E vs A | 12 | 12% | 17% | -4% [-17%, +8%] | 0/2 | 0.500 | 1.000 | not distinguishable |
| E vs B | 12 | 12% | 4% | +8% [+0%, +21%] | 1/0 | 1.000 | 1.000 | not distinguishable |
| E vs C | 12 | 12% | 12% | +0% [-12%, +12%] | 0/1 | 1.000 | 1.000 | not distinguishable |
| E vs D | 12 | 12% | 4% | +8% [-8%, +29%] | 2/1 | 1.000 | 1.000 | not distinguishable |

### 3.4 E vs alternatives, family `pool` only

| Comparison | tasks | rate X | rate Y | mean paired diff [95% bootstrap CI] | discordant X-only/Y-only | McNemar p | Holm-adj p | reading |
|---|---|---|---|---|---|---|---|---|
| E vs A | 24 | 98% | 25% | +73% [+60%, +83%] | 13/0 | 0.000 | 0.001 | E better (bootstrap and McNemar) |
| E vs B | 24 | 98% | 58% | +40% [+23%, +56%] | 7/0 | 0.016 | 0.047 | E better (bootstrap and McNemar) |
| E vs C | 24 | 98% | 90% | +8% [+2%, +17%] | 1/0 | 1.000 | 1.000 | E ahead by bootstrap only (McNemar n.s.) |
| E vs D | 24 | 98% | 90% | +8% [+0%, +19%] | 2/0 | 0.500 | 1.000 | not distinguishable |

### 3.4 E vs alternatives, family `queue` only

| Comparison | tasks | rate X | rate Y | mean paired diff [95% bootstrap CI] | discordant X-only/Y-only | McNemar p | Holm-adj p | reading |
|---|---|---|---|---|---|---|---|---|
| E vs A | 12 | 12% | 17% | -4% [-17%, +8%] | 0/2 | 0.500 | 1.000 | not distinguishable |
| E vs B | 12 | 12% | 4% | +8% [+0%, +21%] | 1/0 | 1.000 | 1.000 | not distinguishable |
| E vs C | 12 | 12% | 12% | +0% [-12%, +12%] | 0/1 | 1.000 | 1.000 | not distinguishable |
| E vs D | 12 | 12% | 4% | +8% [-8%, +29%] | 2/1 | 1.000 | 1.000 | not distinguishable |

### 3.4 E vs alternatives, family `timeout` only

| Comparison | tasks | rate X | rate Y | mean paired diff [95% bootstrap CI] | discordant X-only/Y-only | McNemar p | Holm-adj p | reading |
|---|---|---|---|---|---|---|---|---|
| E vs A | 24 | 42% | 46% | -4% [-15%, +4%] | 0/2 | 0.500 | 1.000 | not distinguishable |
| E vs B | 24 | 42% | 42% | +0% [-17%, +15%] | 4/2 | 0.688 | 1.000 | not distinguishable |
| E vs C | 24 | 42% | 25% | +17% [+6%, +27%] | 5/1 | 0.219 | 0.875 | E ahead by bootstrap only (McNemar n.s.) |
| E vs D | 24 | 42% | 29% | +12% [-2%, +27%] | 6/3 | 0.508 | 1.000 | not distinguishable |

## 4. Execution efficiency and equal-budget success

Tokens include the retrieved memory, which sits inside every prompt of the run.

| Arm | context tokens (est., first call) | retrieved tokens | tokens/run | tokens/verified success |
|---|---|---|---|---|
| A stateless baseline | 307 | 0 | 6,000 | 18,947 |
| B episodic memory | 499 | 192 | 7,724 | 18,916 |
| C consolidated memory | 516 | 208 | 7,804 | 16,147 |
| D instruction reuse | 675 | 368 | 7,823 | 16,185 |
| E compiled capability | n/a (runtime arm) | 0 | 5,635 | 9,660 |

Success rate when a run only counts if it verified within a token cap (equal resource budget):

| Arm | <= 3,000 | <= 6,000 | <= 12,000 | <= 24,000 | <= 60,000 |
|---|---|---|---|---|---|
| A stateless baseline | 0% | 10% | 30% | 32% | 32% |
| B episodic memory | 0% | 2% | 41% | 41% | 41% |
| C consolidated memory | 0% | 5% | 45% | 48% | 48% |
| D instruction reuse | 0% | 2% | 48% | 48% | 48% |
| E compiled capability | 39% | 48% | 56% | 58% | 58% |

## 5. Lifecycle cost and break-even

`lifecycle_cost(N) = L + N * e + M`; break-even is the number of future *verified successes* after which the learning cost is recovered relative to the stateless baseline (`N* = L / (c_A - c_X)`, `c` = tokens per verified success, failures' cost included). Maintenance M = 0 tokens (**not measured**, which flatters every learning arm).

Learning ledger: discovery 381,533 tokens over 63 runs (263 tool calls); analysis 0; consolidation 0 (0 ms); compilation 0 (1251 ms); validation 1,808,832.
- analysis/compilation/consolidation are deterministic (0 model tokens); only wall time is charged
- discovery cost is the model tokens of the discovery runs that every learning arm reuses

### 5.1 As built (only the capability pays for validation)

| Arm | runs | success | tokens/run | tokens/success | tool calls/run | latency s/run | retrieved tok/run | retrieval ms | learning tokens L | break-even (verified successes) |
|---|---|---|---|---|---|---|---|---|---|---|
| A stateless baseline | 120 | 32% | 6,000 | 18,947 | 4.2 | 27 | 0 | 0.0 | 0 | reference |
| B episodic memory | 120 | 41% | 7,724 | 18,916 | 4.5 | 27 | 192 | 0.3 | 381,533 | 12,345 |
| C consolidated memory | 120 | 48% | 7,804 | 16,147 | 4.6 | 28 | 208 | 0.0 | 381,533 | 136 |
| D instruction reuse | 120 | 48% | 7,823 | 16,185 | 4.6 | 23 | 368 | 0.0 | 381,533 | 138 |
| E compiled capability | 120 | 58% | 5,635 | 9,660 | 5.7 | 31 | 0 | 0.0 | 2,190,365 | 236 |

### 5.2 Sensitivity: every learning arm pays the same validation cost

| Arm | runs | success | tokens/run | tokens/success | tool calls/run | latency s/run | retrieved tok/run | retrieval ms | learning tokens L | break-even (verified successes) |
|---|---|---|---|---|---|---|---|---|---|---|
| A stateless baseline | 120 | 32% | 6,000 | 18,947 | 4.2 | 27 | 0 | 0.0 | 0 | reference |
| B episodic memory | 120 | 41% | 7,724 | 18,916 | 4.5 | 27 | 192 | 0.3 | 2,190,365 | 70,872 |
| C consolidated memory | 120 | 48% | 7,804 | 16,147 | 4.6 | 28 | 208 | 0.0 | 2,190,365 | 782 |
| D instruction reuse | 120 | 48% | 7,823 | 16,185 | 4.6 | 23 | 368 | 0.0 | 2,190,365 | 793 |
| E compiled capability | 120 | 58% | 5,635 | 9,660 | 5.7 | 31 | 0 | 0.0 | 2,190,365 | 236 |

### 5.3 Family `pool` (family-specific learning ledger)

| Arm | runs | success | tokens/run | tokens/success | tool calls/run | latency s/run | retrieved tok/run | retrieval ms | learning tokens L | break-even (verified successes) |
|---|---|---|---|---|---|---|---|---|---|---|
| A stateless baseline | 48 | 25% | 5,154 | 20,618 | 4.2 | 22 | 0 | 0.0 | 0 | reference |
| B episodic memory | 48 | 58% | 7,256 | 12,439 | 4.6 | 24 | 193 | 0.3 | 168,870 | 21 |
| C consolidated memory | 48 | 90% | 7,601 | 8,485 | 5.0 | 24 | 196 | 0.0 | 168,870 | 14 |
| D instruction reuse | 48 | 90% | 7,411 | 8,273 | 5.0 | 20 | 345 | 0.0 | 168,870 | 14 |
| E compiled capability | 48 | 98% | 2,245 | 2,292 | 5.2 | 9 | 0 | 0.0 | 1,061,389 | 58 |

### 5.3 Family `timeout` (family-specific learning ledger)

| Arm | runs | success | tokens/run | tokens/success | tool calls/run | latency s/run | retrieved tok/run | retrieval ms | learning tokens L | break-even (verified successes) |
|---|---|---|---|---|---|---|---|---|---|---|
| A stateless baseline | 48 | 46% | 5,961 | 13,006 | 4.2 | 27 | 0 | 0.0 | 0 | reference |
| B episodic memory | 48 | 42% | 7,543 | 18,103 | 4.2 | 28 | 191 | 0.0 | 212,663 | never (execution is not cheaper per success: never recovered) |
| C consolidated memory | 48 | 25% | 7,197 | 28,789 | 4.1 | 30 | 162 | 0.0 | 212,663 | never (execution is not cheaper per success: never recovered) |
| D instruction reuse | 48 | 29% | 6,681 | 22,906 | 4.0 | 25 | 278 | 0.0 | 212,663 | never (execution is not cheaper per success: never recovered) |
| E compiled capability | 48 | 42% | 8,597 | 20,632 | 7.0 | 53 | 0 | 0.0 | 1,128,976 | never (execution is not cheaper per success: never recovered) |

## 6. Compiled capability: applicability, fallback and boundaries

| Family | tasks | eligible capability found | used | fell back to planning |
|---|---|---|---|---|
| pool | 24 | 100% | 100% | 4% |
| queue | 12 | 0% | 0% | 0% |
| timeout | 24 | 100% | 100% | 88% |

Applicability gate (deterministic, no model): tasks admitted per family and synthetic precondition violations admitted (must be 0).

| Capability | learned family | admitted/total by family | synthetic violations admitted |
|---|---|---|---|
| pool.f12f4fa920@1 | pool | pool: 24/24, queue: 0/12, timeout: 0/24 | 0/4 |
| timeout.de1091cd4d@1 | timeout | pool: 0/24, queue: 0/12, timeout: 24/24 | 0/4 |

## 7. Failure and regression analysis

Non-verified runs by termination:

| Arm | non-verified runs | termination counts |
|---|---|---|
| A stateless baseline | 82 | {'FINAL': 81, 'INVALID_ACTION': 1} |
| B episodic memory | 71 | {'FINAL': 71} |
| C consolidated memory | 62 | {'FINAL': 62} |
| D instruction reuse | 62 | {'FINAL': 60, 'ESCALATED': 1, 'INVALID_ACTION': 1} |
| E compiled capability | 50 | {'FINAL': 41, 'INVALID_ACTION': 7, 'MAX_STEPS': 1, 'ESCALATED': 1} |

Regressions against the baseline: tasks A solved (>= half of repetitions) that the arm did not.

| Arm | regressed tasks | improved tasks | task ids that regressed |
|---|---|---|---|
| B episodic memory | 13 | 12 | hid-pool-hikari_shared-04, hid-pool-hikari_shared-10, hid-pool-hikari_shared-11, hid-queue-amqp_channels-02, hid-queue-amqp_channels-04, hid-queue-amqp_channels-05, hid-timeout-okhttp_budget-00, hid-timeout-okhttp_budget-02 |
| C consolidated memory | 8 | 13 | hid-queue-amqp_channels-04, hid-timeout-okhttp_budget-00, hid-timeout-okhttp_budget-02, hid-timeout-okhttp_budget-04, val-timeout-httpx_budget-02, val-timeout-httpx_budget-03, val-timeout-requests_budget-00, val-timeout-requests_budget-03 |
| D instruction reuse | 12 | 15 | hid-pool-hikari_shared-11, hid-queue-amqp_channels-02, hid-queue-amqp_channels-04, hid-queue-amqp_channels-05, hid-queue-amqp_channels-07, hid-timeout-okhttp_budget-00, hid-timeout-okhttp_budget-02, hid-timeout-okhttp_budget-04 |
| E compiled capability | 4 | 13 | hid-queue-amqp_channels-02, hid-queue-amqp_channels-04, val-timeout-requests_budget-00, val-timeout-requests_budget-03 |

## 8. Mechanical verdicts (restating the paired statistics, not interpreting them)

- Does E outperform episodic memory? Paired difference +18% [+8%, +28%] over 60 tasks; McNemar p=0.013; reading: **E better (bootstrap and McNemar)**.
- Does E outperform consolidated memory? Paired difference +10% [+4%, +16%] over 60 tasks; McNemar p=0.289; reading: **E ahead by bootstrap only (McNemar n.s.)**.
- Does E outperform instruction-based reuse? Paired difference +10% [+2%, +18%] over 60 tasks; McNemar p=0.180; reading: **E ahead by bootstrap only (McNemar n.s.)**.
- Does E outperform the stateless baseline? Paired difference +27% [+15%, +38%] over 60 tasks; McNemar p=0.049; reading: **E better (bootstrap and McNemar)**.

## 9. Statistical assumptions and limitations

- Tasks inside a family come from one generator and are treated as exchangeable draws; results do not generalise beyond these families, this simulated environment and the model(s) tested.
- Repetitions are collapsed inside each task, so they sharpen the per-task rate but add no tasks; with a few dozen tasks the intervals are wide.
- McNemar binarises each task and discards the size of the difference; the bootstrap keeps it. They can disagree at small n.
- Holm adjusts within each table only; across the whole report many comparisons are made and isolated small p-values should not be read as findings.
- Token counts include retrieval in the prompt but no price list is applied; maintenance cost is an input, not a measurement.
- Memory and instruction texts were fixed before any evaluation and never tuned on evaluation data; a better-engineered memory could do better.
