# Capability vs memory (cross-model): gemini-2.5-flash-lite

Research question: with equivalent historical experience, does a compiled, validated, executable capability (E) outperform memory-based reuse (B episodic, C consolidated, D instructions) and the stateless baseline (A)? The design is [capability-vs-memory-design.md](../experiments/capability-vs-memory-design.md); the experiment is built to be able to falsify the hypothesis.

## 1. Configuration (reproducibility)

- Model: `vertex-ai:global` / `google/gemini-2.5-flash-lite` (temperature 0.0); repetitions per task: 1; workers: 8. Repetitions are repeated draws from a non-deterministic model (the provider ignores seeds), not independent samples.
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
| all tasks | 30 | 10% | 33% | 3% | 0% | 10% |

### By task family

| Family | tasks | A | B | C | D | E |
|---|---|---|---|---|---|---|
| pool | 12 | 0% | 33% | 0% | 0% | 0% |
| queue | 6 | 0% | 0% | 17% | 0% | 0% |
| timeout | 12 | 25% | 50% | 0% | 0% | 25% |

### By transfer level

`intra_variant`: seen libraries, new numbers. `cross_variant`: a learned family with an unseen library/layout. `cross_family`: a family never seen in discovery.

| Level | tasks | A | B | C | D | E |
|---|---|---|---|---|---|---|
| cross_family | 6 | 0% | 0% | 17% | 0% | 0% |
| cross_variant | 12 | 0% | 42% | 0% | 0% | 17% |
| intra_variant | 12 | 25% | 42% | 0% | 0% | 8% |

### By variant

| Variant | tasks | A | B | C | D | E |
|---|---|---|---|---|---|---|
| pool/hikari_shared | 6 | 0% | 33% | 0% | 0% | 0% |
| pool/psycopg_shared | 3 | 0% | 33% | 0% | 0% | 0% |
| pool/sqlalchemy_shared | 3 | 0% | 33% | 0% | 0% | 0% |
| queue/amqp_channels | 6 | 0% | 0% | 17% | 0% | 0% |
| timeout/httpx_budget | 3 | 33% | 67% | 0% | 0% | 0% |
| timeout/okhttp_budget | 6 | 0% | 50% | 0% | 0% | 33% |
| timeout/requests_budget | 3 | 67% | 33% | 0% | 0% | 33% |

## 3. Paired statistical comparisons

Unit = distinct task; repetitions are averaged inside a task. Paired bootstrap over tasks (10,000 resamples, fixed seed); exact McNemar on tasks "solved" in >= 50% of repetitions; Holm correction within each table. Wilson intervals are not used for inference here.

### 3.1 Primary: compiled capability (E) against every alternative

| Comparison | tasks | rate X | rate Y | mean paired diff [95% bootstrap CI] | discordant X-only/Y-only | McNemar p | Holm-adj p | reading |
|---|---|---|---|---|---|---|---|---|
| E vs A | 30 | 10% | 10% | +0% [-17%, +17%] | 3/3 | 1.000 | 1.000 | not distinguishable |
| E vs B | 30 | 10% | 33% | -23% [-43%, -3%] | 2/9 | 0.065 | 0.262 | B ahead by bootstrap only (McNemar n.s.) |
| E vs C | 30 | 10% | 3% | +7% [-7%, +20%] | 3/1 | 0.625 | 1.000 | not distinguishable |
| E vs D | 30 | 10% | 0% | +10% [+0%, +23%] | 3/0 | 0.250 | 0.750 | not distinguishable |

E vs the best memory arm (**B**, chosen *post hoc* by overall rate, so this comparison favours memory):

| Comparison | tasks | rate X | rate Y | mean paired diff [95% bootstrap CI] | discordant X-only/Y-only | McNemar p | Holm-adj p | reading |
|---|---|---|---|---|---|---|---|---|
| E vs B | 30 | 10% | 33% | -23% [-43%, -3%] | 2/9 | 0.065 | 0.065 | B ahead by bootstrap only (McNemar n.s.) |

### 3.2 Attribution ladder (where does any improvement come from?)

A->B/C: value of *information*. C->D: value of a procedure stated as *instructions*. D->E: value of *executing* the procedure instead of asking the model to follow it.

| Comparison | tasks | rate X | rate Y | mean paired diff [95% bootstrap CI] | discordant X-only/Y-only | McNemar p | Holm-adj p | reading |
|---|---|---|---|---|---|---|---|---|
| B vs A | 30 | 33% | 10% | +23% [+3%, +43%] | 9/2 | 0.065 | 0.262 | B ahead by bootstrap only (McNemar n.s.) |
| C vs A | 30 | 3% | 10% | -7% [-20%, +7%] | 1/3 | 0.625 | 1.000 | not distinguishable |
| C vs B | 30 | 3% | 33% | -30% [-50%, -10%] | 1/10 | 0.012 | 0.059 | B ahead by bootstrap only (McNemar n.s.) |
| D vs C | 30 | 0% | 3% | -3% [-10%, +0%] | 0/1 | 1.000 | 1.000 | not distinguishable |
| E vs D | 30 | 10% | 0% | +10% [+0%, +23%] | 3/0 | 0.250 | 0.750 | not distinguishable |

### 3.3 E vs alternatives, intra_variant tasks only

| Comparison | tasks | rate X | rate Y | mean paired diff [95% bootstrap CI] | discordant X-only/Y-only | McNemar p | Holm-adj p | reading |
|---|---|---|---|---|---|---|---|---|
| E vs A | 12 | 8% | 25% | -17% [-50%, +17%] | 1/3 | 0.625 | 1.000 | not distinguishable |
| E vs B | 12 | 8% | 42% | -33% [-58%, -8%] | 0/4 | 0.125 | 0.500 | B ahead by bootstrap only (McNemar n.s.) |
| E vs C | 12 | 8% | 0% | +8% [+0%, +25%] | 1/0 | 1.000 | 1.000 | not distinguishable |
| E vs D | 12 | 8% | 0% | +8% [+0%, +25%] | 1/0 | 1.000 | 1.000 | not distinguishable |

### 3.3 E vs alternatives, cross_variant tasks only

| Comparison | tasks | rate X | rate Y | mean paired diff [95% bootstrap CI] | discordant X-only/Y-only | McNemar p | Holm-adj p | reading |
|---|---|---|---|---|---|---|---|---|
| E vs A | 12 | 17% | 0% | +17% [+0%, +42%] | 2/0 | 0.500 | 1.000 | not distinguishable |
| E vs B | 12 | 17% | 42% | -25% [-67%, +17%] | 2/5 | 0.453 | 1.000 | not distinguishable |
| E vs C | 12 | 17% | 0% | +17% [+0%, +42%] | 2/0 | 0.500 | 1.000 | not distinguishable |
| E vs D | 12 | 17% | 0% | +17% [+0%, +42%] | 2/0 | 0.500 | 1.000 | not distinguishable |

### 3.3 E vs alternatives, cross_family tasks only

| Comparison | tasks | rate X | rate Y | mean paired diff [95% bootstrap CI] | discordant X-only/Y-only | McNemar p | Holm-adj p | reading |
|---|---|---|---|---|---|---|---|---|
| E vs A | 6 | 0% | 0% | +0% [+0%, +0%] | 0/0 | 1.000 | 1.000 | not distinguishable |
| E vs B | 6 | 0% | 0% | +0% [+0%, +0%] | 0/0 | 1.000 | 1.000 | not distinguishable |
| E vs C | 6 | 0% | 17% | -17% [-50%, +0%] | 0/1 | 1.000 | 1.000 | not distinguishable |
| E vs D | 6 | 0% | 0% | +0% [+0%, +0%] | 0/0 | 1.000 | 1.000 | not distinguishable |

### 3.4 E vs alternatives, family `pool` only

| Comparison | tasks | rate X | rate Y | mean paired diff [95% bootstrap CI] | discordant X-only/Y-only | McNemar p | Holm-adj p | reading |
|---|---|---|---|---|---|---|---|---|
| E vs A | 12 | 0% | 0% | +0% [+0%, +0%] | 0/0 | 1.000 | 1.000 | not distinguishable |
| E vs B | 12 | 0% | 33% | -33% [-58%, -8%] | 0/4 | 0.125 | 0.500 | B ahead by bootstrap only (McNemar n.s.) |
| E vs C | 12 | 0% | 0% | +0% [+0%, +0%] | 0/0 | 1.000 | 1.000 | not distinguishable |
| E vs D | 12 | 0% | 0% | +0% [+0%, +0%] | 0/0 | 1.000 | 1.000 | not distinguishable |

### 3.4 E vs alternatives, family `queue` only

| Comparison | tasks | rate X | rate Y | mean paired diff [95% bootstrap CI] | discordant X-only/Y-only | McNemar p | Holm-adj p | reading |
|---|---|---|---|---|---|---|---|---|
| E vs A | 6 | 0% | 0% | +0% [+0%, +0%] | 0/0 | 1.000 | 1.000 | not distinguishable |
| E vs B | 6 | 0% | 0% | +0% [+0%, +0%] | 0/0 | 1.000 | 1.000 | not distinguishable |
| E vs C | 6 | 0% | 17% | -17% [-50%, +0%] | 0/1 | 1.000 | 1.000 | not distinguishable |
| E vs D | 6 | 0% | 0% | +0% [+0%, +0%] | 0/0 | 1.000 | 1.000 | not distinguishable |

### 3.4 E vs alternatives, family `timeout` only

| Comparison | tasks | rate X | rate Y | mean paired diff [95% bootstrap CI] | discordant X-only/Y-only | McNemar p | Holm-adj p | reading |
|---|---|---|---|---|---|---|---|---|
| E vs A | 12 | 25% | 25% | +0% [-42%, +42%] | 3/3 | 1.000 | 1.000 | not distinguishable |
| E vs B | 12 | 25% | 50% | -25% [-67%, +17%] | 2/5 | 0.453 | 1.000 | not distinguishable |
| E vs C | 12 | 25% | 0% | +25% [+0%, +50%] | 3/0 | 0.250 | 1.000 | not distinguishable |
| E vs D | 12 | 25% | 0% | +25% [+0%, +50%] | 3/0 | 0.250 | 1.000 | not distinguishable |

## 4. Execution efficiency and equal-budget success

Tokens include the retrieved memory, which sits inside every prompt of the run.

| Arm | context tokens (est., first call) | retrieved tokens | tokens/run | tokens/verified success |
|---|---|---|---|---|
| A stateless baseline | 307 | 0 | 31,338 | 313,381 |
| B episodic memory | 499 | 192 | 10,292 | 30,877 |
| C consolidated memory | 516 | 208 | 11,571 | 347,123 |
| D instruction reuse | 675 | 368 | 19,900 | never |
| E compiled capability | n/a (runtime arm) | 0 | 28,903 | 289,031 |

Success rate when a run only counts if it verified within a token cap (equal resource budget):

| Arm | <= 3,000 | <= 6,000 | <= 12,000 | <= 24,000 | <= 60,000 |
|---|---|---|---|---|---|
| A stateless baseline | 0% | 3% | 7% | 10% | 10% |
| B episodic memory | 0% | 10% | 27% | 27% | 33% |
| C consolidated memory | 0% | 0% | 3% | 3% | 3% |
| D instruction reuse | 0% | 0% | 0% | 0% | 0% |
| E compiled capability | 0% | 0% | 3% | 3% | 10% |

## 5. Lifecycle cost and break-even

`lifecycle_cost(N) = L + N * e + M`; break-even is the number of future *verified successes* after which the learning cost is recovered relative to the stateless baseline (`N* = L / (c_A - c_X)`, `c` = tokens per verified success, failures' cost included). Maintenance M = 0 tokens (**not measured**, which flatters every learning arm).

Learning ledger: discovery 381,533 tokens over 63 runs (263 tool calls); analysis 0; consolidation 0 (0 ms); compilation 0 (1251 ms); validation 1,808,832.
- analysis/compilation/consolidation are deterministic (0 model tokens); only wall time is charged
- discovery cost is the model tokens of the discovery runs that every learning arm reuses

### 5.1 As built (only the capability pays for validation)

| Arm | runs | success | tokens/run | tokens/success | tool calls/run | latency s/run | retrieved tok/run | retrieval ms | learning tokens L | break-even (verified successes) |
|---|---|---|---|---|---|---|---|---|---|---|
| A stateless baseline | 30 | 10% | 31,338 | 313,381 | 6.3 | 27 | 0 | 0.0 | 0 | reference |
| B episodic memory | 30 | 33% | 10,292 | 30,877 | 5.0 | 15 | 192 | 1.0 | 381,533 | 1 |
| C consolidated memory | 30 | 3% | 11,571 | 347,123 | 5.8 | 16 | 208 | 0.0 | 381,533 | never (execution is not cheaper per success: never recovered) |
| D instruction reuse | 30 | 0% | 19,900 | never | 4.7 | 20 | 368 | 0.0 | 381,533 | never (no verified successes: never recovered) |
| E compiled capability | 30 | 10% | 28,903 | 289,031 | 9.9 | 28 | 0 | 0.0 | 2,190,365 | 90 |

### 5.2 Sensitivity: every learning arm pays the same validation cost

| Arm | runs | success | tokens/run | tokens/success | tool calls/run | latency s/run | retrieved tok/run | retrieval ms | learning tokens L | break-even (verified successes) |
|---|---|---|---|---|---|---|---|---|---|---|
| A stateless baseline | 30 | 10% | 31,338 | 313,381 | 6.3 | 27 | 0 | 0.0 | 0 | reference |
| B episodic memory | 30 | 33% | 10,292 | 30,877 | 5.0 | 15 | 192 | 1.0 | 2,190,365 | 8 |
| C consolidated memory | 30 | 3% | 11,571 | 347,123 | 5.8 | 16 | 208 | 0.0 | 2,190,365 | never (execution is not cheaper per success: never recovered) |
| D instruction reuse | 30 | 0% | 19,900 | never | 4.7 | 20 | 368 | 0.0 | 2,190,365 | never (no verified successes: never recovered) |
| E compiled capability | 30 | 10% | 28,903 | 289,031 | 9.9 | 28 | 0 | 0.0 | 2,190,365 | 90 |

### 5.3 Family `pool` (family-specific learning ledger)

| Arm | runs | success | tokens/run | tokens/success | tool calls/run | latency s/run | retrieved tok/run | retrieval ms | learning tokens L | break-even (verified successes) |
|---|---|---|---|---|---|---|---|---|---|---|
| A stateless baseline | 12 | 0% | 42,803 | never | 5.2 | 32 | 0 | 0.0 | 0 | reference |
| B episodic memory | 12 | 33% | 16,037 | 48,110 | 5.0 | 19 | 193 | 0.2 | 168,870 | 0 |
| C consolidated memory | 12 | 0% | 17,491 | never | 8.4 | 21 | 196 | 0.0 | 168,870 | never (no verified successes: never recovered) |
| D instruction reuse | 12 | 0% | 11,938 | never | 6.3 | 16 | 345 | 0.0 | 168,870 | never (no verified successes: never recovered) |
| E compiled capability | 12 | 0% | 34,172 | never | 9.8 | 30 | 0 | 0.0 | 1,061,389 | never (no verified successes: never recovered) |

### 5.3 Family `timeout` (family-specific learning ledger)

| Arm | runs | success | tokens/run | tokens/success | tool calls/run | latency s/run | retrieved tok/run | retrieval ms | learning tokens L | break-even (verified successes) |
|---|---|---|---|---|---|---|---|---|---|---|
| A stateless baseline | 12 | 25% | 20,585 | 82,342 | 4.4 | 21 | 0 | 0.0 | 0 | reference |
| B episodic memory | 12 | 50% | 5,850 | 11,699 | 5.0 | 12 | 191 | 2.2 | 212,663 | 3 |
| C consolidated memory | 12 | 0% | 5,328 | never | 4.1 | 10 | 162 | 0.0 | 212,663 | never (no verified successes: never recovered) |
| D instruction reuse | 12 | 0% | 4,421 | never | 3.4 | 9 | 278 | 0.0 | 212,663 | never (no verified successes: never recovered) |
| E compiled capability | 12 | 25% | 23,128 | 92,513 | 8.9 | 24 | 0 | 0.0 | 1,128,976 | never (execution is not cheaper per success: never recovered) |

## 6. Compiled capability: applicability, fallback and boundaries

| Family | tasks | eligible capability found | used | fell back to planning |
|---|---|---|---|---|
| pool | 12 | 100% | 100% | 100% |
| queue | 6 | 0% | 0% | 0% |
| timeout | 12 | 100% | 100% | 100% |

Applicability gate (deterministic, no model): tasks admitted per family and synthetic precondition violations admitted (must be 0).

| Capability | learned family | admitted/total by family | synthetic violations admitted |
|---|---|---|---|
| pool.f12f4fa920@1 | pool | pool: 12/12, queue: 0/6, timeout: 0/12 | 0/4 |
| timeout.de1091cd4d@1 | timeout | pool: 0/12, queue: 0/6, timeout: 12/12 | 0/4 |

## 7. Failure and regression analysis

Non-verified runs by termination:

| Arm | non-verified runs | termination counts |
|---|---|---|
| A stateless baseline | 27 | {'FINAL': 17, 'MAX_STEPS': 6, 'MAX_TOKENS': 3, 'ESCALATED': 1} |
| B episodic memory | 20 | {'FINAL': 19, 'ESCALATED': 1} |
| C consolidated memory | 29 | {'FINAL': 22, 'MAX_STEPS': 6, 'INVALID_ACTION': 1} |
| D instruction reuse | 30 | {'FINAL': 22, 'MAX_TOKENS': 6, 'MAX_STEPS': 2} |
| E compiled capability | 27 | {'MAX_STEPS': 17, 'FINAL': 10} |

Regressions against the baseline: tasks A solved (>= half of repetitions) that the arm did not.

| Arm | regressed tasks | improved tasks | task ids that regressed |
|---|---|---|---|
| B episodic memory | 2 | 9 | val-timeout-requests_budget-00, val-timeout-requests_budget-02 |
| C consolidated memory | 3 | 1 | val-timeout-httpx_budget-01, val-timeout-requests_budget-00, val-timeout-requests_budget-02 |
| D instruction reuse | 3 | 0 | val-timeout-httpx_budget-01, val-timeout-requests_budget-00, val-timeout-requests_budget-02 |
| E compiled capability | 3 | 3 | val-timeout-httpx_budget-01, val-timeout-requests_budget-00, val-timeout-requests_budget-02 |

## 8. Mechanical verdicts (restating the paired statistics, not interpreting them)

- Does E outperform episodic memory? Paired difference -23% [-43%, -3%] over 30 tasks; McNemar p=0.065; reading: **B ahead by bootstrap only (McNemar n.s.)**.
- Does E outperform consolidated memory? Paired difference +7% [-7%, +20%] over 30 tasks; McNemar p=0.625; reading: **not distinguishable**.
- Does E outperform instruction-based reuse? Paired difference +10% [+0%, +23%] over 30 tasks; McNemar p=0.250; reading: **not distinguishable**.
- Does E outperform the stateless baseline? Paired difference +0% [-17%, +17%] over 30 tasks; McNemar p=1.000; reading: **not distinguishable**.

## 9. Statistical assumptions and limitations

- Tasks inside a family come from one generator and are treated as exchangeable draws; results do not generalise beyond these families, this simulated environment and the model(s) tested.
- Repetitions are collapsed inside each task, so they sharpen the per-task rate but add no tasks; with a few dozen tasks the intervals are wide.
- McNemar binarises each task and discards the size of the difference; the bootstrap keeps it. They can disagree at small n.
- Holm adjusts within each table only; across the whole report many comparisons are made and isolated small p-values should not be read as findings.
- Token counts include retrieval in the prompt but no price list is applied; maintenance cost is an input, not a measurement.
- Memory and instruction texts were fixed before any evaluation and never tuned on evaluation data; a better-engineered memory could do better.
