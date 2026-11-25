# Scoped but unvalidated assets (EXPERIMENTAL, generated)

**Gated mode.** Arms B-E read only ACTIVE governed assets (memory and capabilities were validated and promoted by the same policy); a task without an applicable ACTIVE asset runs under general planning with a recorded reason. F is the noise control: the same path with an empty registry.

Research question: with equivalent historical experience, does a compiled, validated, executable capability (E) outperform memory-based reuse (B episodic, C consolidated, D instructions) and the stateless baseline (A)? The design is [capability-vs-memory-design.md](../experiments/capability-vs-memory-design.md); the experiment is built to be able to falsify the hypothesis.

## 1. Configuration (reproducibility)

- Model: `vertex-ai:global` / `google/gemini-2.5-flash` (temperature 0.0); repetitions per task: 1; workers: 8. Repetitions are repeated draws from a non-deterministic model (the provider ignores seeds), not independent samples.
- Arms: A stateless baseline, B ACTIVE episodic memory only, C ACTIVE consolidated memory only, D ACTIVE procedural memory only, E ACTIVE compiled capability only, F noise control (empty registry).
- Datasets: HIDDEN_TRANSFER: `connection-debug-sim` v3.0.0-private-x5 `cb5fa61ae850…`; VALIDATION: `connection-debug-sim` v3.0.0-private-x5 `bf6d7922c51a…`
- Discovery experience: 45 eligible trajectories (DISCOVERY split only), families ['pool', 'timeout']; the same set feeds B, C, D and the compiler.
- ACTIVE memory assets: mem_consolidated_pool@1, mem_consolidated_timeout@1, mem_episodic_pool@1, mem_episodic_timeout@1, mem_procedural_pool@1, mem_procedural_timeout@1.
- Capabilities available to E (and rendered for D in legacy mode): pool.f12f4fa920@1, timeout.e2fb15fa79@1 (catalog states ['ACTIVE', 'REJECTED']). **Experimental catalog: not every version is ACTIVE; this is a research run, not promotion evidence.**
- Memory preamble sha256 `c1196b1a6e4e92f9…`; up to 3 episodes; token estimator `chars/4`; verifier `sim-verifier/1`; runtime `baseline-runtime/0.1.0`; evaluator `evaluator/1`.
- Runs: 900 (5 UNKNOWN, excluded as missing data); distinct tasks: 150.
- Note: environment sim-env/1

## 2. Success by arm

Cells are the mean of per-task success rates (each task weighted equally); the number of distinct tasks is shown next to the group.

| Group | tasks | A | B | C | D | E | F |
|---|---|---|---|---|---|---|---|
| all tasks | 150 | 32% | 43% | 76% | 81% | 83% | 36% |

### By task family

| Family | tasks | A | B | C | D | E | F |
|---|---|---|---|---|---|---|---|
| pool | 60 | 28% | 27% | 92% | 92% | 98% | 30% |
| queue | 30 | 10% | 10% | 7% | 30% | 23% | 20% |
| timeout | 60 | 47% | 76% | 95% | 97% | 98% | 50% |

### By transfer level

`intra_variant`: seen libraries, new numbers. `cross_variant`: a learned family with an unseen library/layout. `cross_family`: a family never seen in discovery.

| Level | tasks | A | B | C | D | E | F |
|---|---|---|---|---|---|---|---|
| cross_family | 30 | 10% | 10% | 7% | 30% | 23% | 20% |
| cross_variant | 60 | 42% | 42% | 97% | 93% | 98% | 45% |
| intra_variant | 60 | 34% | 61% | 90% | 95% | 98% | 35% |

### By variant

| Variant | tasks | A | B | C | D | E | F |
|---|---|---|---|---|---|---|---|
| pool/hikari_shared | 30 | 40% | 23% | 93% | 90% | 100% | 40% |
| pool/psycopg_shared | 15 | 13% | 40% | 80% | 93% | 93% | 13% |
| pool/sqlalchemy_shared | 15 | 20% | 20% | 100% | 93% | 100% | 27% |
| queue/amqp_channels | 30 | 10% | 10% | 7% | 30% | 23% | 20% |
| timeout/httpx_budget | 15 | 50% | 93% | 92% | 93% | 100% | 47% |
| timeout/okhttp_budget | 30 | 43% | 60% | 100% | 97% | 97% | 50% |
| timeout/requests_budget | 15 | 53% | 93% | 87% | 100% | 100% | 53% |

## 3. Paired statistical comparisons

Unit = distinct task; repetitions are averaged inside a task. Paired bootstrap over tasks (10,000 resamples, fixed seed); exact McNemar on tasks "solved" in >= 50% of repetitions; Holm correction within each table. Wilson intervals are not used for inference here.

### 3.1 Primary: compiled capability (E) against every alternative

| Comparison | tasks | rate X | rate Y | mean paired diff [95% bootstrap CI] | discordant X-only/Y-only | McNemar p | Holm-adj p | reading |
|---|---|---|---|---|---|---|---|---|
| E vs A | 149 | 83% | 32% | +51% [+43%, +59%] | 77/1 | 0.000 | 0.000 | E better (bootstrap and McNemar) |
| E vs B | 149 | 83% | 43% | +40% [+32%, +48%] | 61/1 | 0.000 | 0.000 | E better (bootstrap and McNemar) |
| E vs C | 148 | 83% | 76% | +7% [+2%, +13%] | 15/4 | 0.019 | 0.038 | E better (bootstrap and McNemar) |
| E vs D | 149 | 83% | 81% | +2% [-3%, +8%] | 11/8 | 0.648 | 0.648 | not distinguishable |
| E vs F | 150 | 83% | 36% | +47% [+39%, +56%] | 75/4 | 0.000 | 0.000 | E better (bootstrap and McNemar) |

E vs the best memory arm (**D**, chosen *post hoc* by overall rate, so this comparison favours memory):

| Comparison | tasks | rate X | rate Y | mean paired diff [95% bootstrap CI] | discordant X-only/Y-only | McNemar p | Holm-adj p | reading |
|---|---|---|---|---|---|---|---|---|
| E vs D | 149 | 83% | 81% | +2% [-3%, +8%] | 11/8 | 0.648 | 0.648 | not distinguishable |

### 3.2 Attribution ladder (where does any improvement come from?)

A->B/C: value of *information*. C->D: value of a procedure stated as *instructions*. D->E: value of *executing* the procedure instead of asking the model to follow it.

| Comparison | tasks | rate X | rate Y | mean paired diff [95% bootstrap CI] | discordant X-only/Y-only | McNemar p | Holm-adj p | reading |
|---|---|---|---|---|---|---|---|---|
| B vs A | 149 | 43% | 32% | +11% [+2%, +19%] | 31/15 | 0.026 | 0.078 | B ahead by bootstrap only (McNemar n.s.) |
| C vs A | 148 | 76% | 32% | +43% [+34%, +52%] | 68/4 | 0.000 | 0.000 | C better (bootstrap and McNemar) |
| C vs B | 148 | 76% | 43% | +33% [+24%, +42%] | 56/7 | 0.000 | 0.000 | C better (bootstrap and McNemar) |
| D vs C | 148 | 82% | 76% | +6% [+1%, +12%] | 14/5 | 0.064 | 0.127 | D ahead by bootstrap only (McNemar n.s.) |
| E vs D | 149 | 83% | 81% | +2% [-3%, +8%] | 11/8 | 0.648 | 0.648 | not distinguishable |

### 3.3 E vs alternatives, intra_variant tasks only

| Comparison | tasks | rate X | rate Y | mean paired diff [95% bootstrap CI] | discordant X-only/Y-only | McNemar p | Holm-adj p | reading |
|---|---|---|---|---|---|---|---|---|
| E vs A | 59 | 98% | 34% | +64% [+53%, +76%] | 38/0 | 0.000 | 0.000 | E better (bootstrap and McNemar) |
| E vs B | 59 | 98% | 61% | +37% [+25%, +49%] | 22/0 | 0.000 | 0.000 | E better (bootstrap and McNemar) |
| E vs C | 58 | 98% | 90% | +9% [+0%, +17%] | 6/1 | 0.125 | 0.250 | not distinguishable |
| E vs D | 59 | 98% | 95% | +3% [-3%, +10%] | 3/1 | 0.625 | 0.625 | not distinguishable |
| E vs F | 60 | 98% | 35% | +63% [+52%, +75%] | 38/0 | 0.000 | 0.000 | E better (bootstrap and McNemar) |

### 3.3 E vs alternatives, cross_variant tasks only

| Comparison | tasks | rate X | rate Y | mean paired diff [95% bootstrap CI] | discordant X-only/Y-only | McNemar p | Holm-adj p | reading |
|---|---|---|---|---|---|---|---|---|
| E vs A | 60 | 98% | 42% | +57% [+43%, +68%] | 34/0 | 0.000 | 0.000 | E better (bootstrap and McNemar) |
| E vs B | 60 | 98% | 42% | +57% [+43%, +68%] | 34/0 | 0.000 | 0.000 | E better (bootstrap and McNemar) |
| E vs C | 60 | 98% | 97% | +2% [-3%, +8%] | 2/1 | 1.000 | 1.000 | not distinguishable |
| E vs D | 60 | 98% | 93% | +5% [-2%, +13%] | 4/1 | 0.375 | 0.750 | not distinguishable |
| E vs F | 60 | 98% | 45% | +53% [+40%, +67%] | 32/0 | 0.000 | 0.000 | E better (bootstrap and McNemar) |

### 3.3 E vs alternatives, cross_family tasks only

| Comparison | tasks | rate X | rate Y | mean paired diff [95% bootstrap CI] | discordant X-only/Y-only | McNemar p | Holm-adj p | reading |
|---|---|---|---|---|---|---|---|---|
| E vs A | 30 | 23% | 10% | +13% [+0%, +30%] | 5/1 | 0.219 | 0.898 | not distinguishable |
| E vs B | 30 | 23% | 10% | +13% [+0%, +30%] | 5/1 | 0.219 | 0.898 | not distinguishable |
| E vs C | 30 | 23% | 7% | +17% [+0%, +37%] | 7/2 | 0.180 | 0.898 | not distinguishable |
| E vs D | 30 | 23% | 30% | -7% [-27%, +13%] | 4/6 | 0.754 | 1.000 | not distinguishable |
| E vs F | 30 | 23% | 20% | +3% [-17%, +23%] | 5/4 | 1.000 | 1.000 | not distinguishable |

### 3.4 E vs alternatives, family `pool` only

| Comparison | tasks | rate X | rate Y | mean paired diff [95% bootstrap CI] | discordant X-only/Y-only | McNemar p | Holm-adj p | reading |
|---|---|---|---|---|---|---|---|---|
| E vs A | 60 | 98% | 28% | +70% [+58%, +82%] | 42/0 | 0.000 | 0.000 | E better (bootstrap and McNemar) |
| E vs B | 60 | 98% | 27% | +72% [+60%, +83%] | 43/0 | 0.000 | 0.000 | E better (bootstrap and McNemar) |
| E vs C | 60 | 98% | 92% | +7% [+0%, +15%] | 5/1 | 0.219 | 0.438 | not distinguishable |
| E vs D | 60 | 98% | 92% | +7% [+0%, +15%] | 5/1 | 0.219 | 0.438 | not distinguishable |
| E vs F | 60 | 98% | 30% | +68% [+57%, +80%] | 41/0 | 0.000 | 0.000 | E better (bootstrap and McNemar) |

### 3.4 E vs alternatives, family `queue` only

| Comparison | tasks | rate X | rate Y | mean paired diff [95% bootstrap CI] | discordant X-only/Y-only | McNemar p | Holm-adj p | reading |
|---|---|---|---|---|---|---|---|---|
| E vs A | 30 | 23% | 10% | +13% [+0%, +30%] | 5/1 | 0.219 | 0.898 | not distinguishable |
| E vs B | 30 | 23% | 10% | +13% [+0%, +30%] | 5/1 | 0.219 | 0.898 | not distinguishable |
| E vs C | 30 | 23% | 7% | +17% [+0%, +37%] | 7/2 | 0.180 | 0.898 | not distinguishable |
| E vs D | 30 | 23% | 30% | -7% [-27%, +13%] | 4/6 | 0.754 | 1.000 | not distinguishable |
| E vs F | 30 | 23% | 20% | +3% [-17%, +23%] | 5/4 | 1.000 | 1.000 | not distinguishable |

### 3.4 E vs alternatives, family `timeout` only

| Comparison | tasks | rate X | rate Y | mean paired diff [95% bootstrap CI] | discordant X-only/Y-only | McNemar p | Holm-adj p | reading |
|---|---|---|---|---|---|---|---|---|
| E vs A | 59 | 98% | 47% | +51% [+37%, +63%] | 30/0 | 0.000 | 0.000 | E better (bootstrap and McNemar) |
| E vs B | 59 | 98% | 76% | +22% [+12%, +32%] | 13/0 | 0.000 | 0.001 | E better (bootstrap and McNemar) |
| E vs C | 58 | 98% | 95% | +3% [-3%, +10%] | 3/1 | 0.625 | 1.000 | not distinguishable |
| E vs D | 59 | 98% | 97% | +2% [-3%, +8%] | 2/1 | 1.000 | 1.000 | not distinguishable |
| E vs F | 60 | 98% | 50% | +48% [+35%, +60%] | 29/0 | 0.000 | 0.000 | E better (bootstrap and McNemar) |

## 4. Execution efficiency and equal-budget success

Tokens include the retrieved memory, which sits inside every prompt of the run.

| Arm | context tokens (est., first call) | retrieved tokens | tokens/run | tokens/verified success |
|---|---|---|---|---|
| A stateless baseline | 307 | 0 | 6,045 | 18,766 |
| B ACTIVE episodic memory only | 460 | 153 | 6,839 | 15,922 |
| C ACTIVE consolidated memory only | 464 | 156 | 7,588 | 10,027 |
| D ACTIVE procedural memory only | 573 | 266 | 7,588 | 9,344 |
| E ACTIVE compiled capability only | n/a (runtime arm) | 0 | 3,475 | 4,170 |
| F noise control (empty registry) | 307 | 0 | 5,876 | 16,323 |

Success rate when a run only counts if it verified within a token cap (equal resource budget):

| Arm | <= 3,000 | <= 6,000 | <= 12,000 | <= 24,000 | <= 60,000 |
|---|---|---|---|---|---|
| A stateless baseline | 0% | 9% | 32% | 32% | 32% |
| B ACTIVE episodic memory only | 0% | 4% | 42% | 43% | 43% |
| C ACTIVE consolidated memory only | 0% | 1% | 72% | 76% | 76% |
| D ACTIVE procedural memory only | 0% | 1% | 80% | 81% | 81% |
| E ACTIVE compiled capability only | 59% | 80% | 83% | 83% | 83% |
| F noise control (empty registry) | 0% | 13% | 35% | 36% | 36% |

## 5. Lifecycle cost and break-even

`lifecycle_cost(N) = L + N * e + M`; break-even is the number of future *verified successes* after which the learning cost is recovered relative to the stateless baseline (`N* = L / (c_A - c_X)`, `c` = tokens per verified success, failures' cost included). Maintenance M = 0 tokens (**not measured**, which flatters every learning arm).

Learning ledger: discovery 257,325 tokens over 45 runs (192 tool calls); analysis 0; consolidation 0 (0 ms); compilation 0 (979 ms); validation 9,437,873.
- analysis, memory construction and compilation are deterministic (0 model tokens); only wall time is charged
- discovery cost is the model tokens of the discovery runs that every learning arm reuses
- validation tokens are summed over every stored evaluation of the arm's asset type, rejected assets included; the shared baseline was run once per session but each asset is charged the control tokens its own evaluation consumed

### 5.1 As built (each arm pays the recorded validation of its own asset type, rejected assets included)

| Arm | runs | success | tokens/run | tokens/success | tool calls/run | latency s/run | retrieved tok/run | retrieval ms | learning tokens L | break-even (verified successes) |
|---|---|---|---|---|---|---|---|---|---|---|
| A stateless baseline | 149 | 32% | 6,045 | 18,766 | 4.2 | 26 | 0 | 0.0 | 0 | reference |
| B ACTIVE episodic memory only | 149 | 43% | 6,839 | 15,922 | 4.4 | 25 | 153 | 76.2 | 2,525,111 | 888 |
| C ACTIVE consolidated memory only | 148 | 76% | 7,588 | 10,027 | 4.8 | 25 | 156 | 66.3 | 2,622,462 | 300 |
| D ACTIVE procedural memory only | 149 | 81% | 7,588 | 9,344 | 4.9 | 23 | 266 | 58.2 | 2,655,703 | 282 |
| E ACTIVE compiled capability only | 150 | 83% | 3,475 | 4,170 | 5.0 | 16 | 0 | 0.0 | 2,663,897 | 183 |
| F noise control (empty registry) | 150 | 36% | 5,876 | 16,323 | 4.2 | 28 | 0 | 0.0 | 0 | never (reference) |

### 5.2 Equal governance: every learning arm pays the same mean validation cost

| Arm | runs | success | tokens/run | tokens/success | tool calls/run | latency s/run | retrieved tok/run | retrieval ms | learning tokens L | break-even (verified successes) |
|---|---|---|---|---|---|---|---|---|---|---|
| A stateless baseline | 149 | 32% | 6,045 | 18,766 | 4.2 | 26 | 0 | 0.0 | 0 | reference |
| B ACTIVE episodic memory only | 149 | 43% | 6,839 | 15,922 | 4.4 | 25 | 153 | 76.2 | 2,616,793 | 920 |
| C ACTIVE consolidated memory only | 148 | 76% | 7,588 | 10,027 | 4.8 | 25 | 156 | 66.3 | 2,616,793 | 299 |
| D ACTIVE procedural memory only | 149 | 81% | 7,588 | 9,344 | 4.9 | 23 | 266 | 58.2 | 2,616,793 | 278 |
| E ACTIVE compiled capability only | 150 | 83% | 3,475 | 4,170 | 5.0 | 16 | 0 | 0.0 | 2,616,793 | 179 |
| F noise control (empty registry) | 150 | 36% | 5,876 | 16,323 | 4.2 | 28 | 0 | 0.0 | 0 | never (reference) |

### 5.3 Family `pool` (family-specific learning ledger)

| Arm | runs | success | tokens/run | tokens/success | tool calls/run | latency s/run | retrieved tok/run | retrieval ms | learning tokens L | break-even (verified successes) |
|---|---|---|---|---|---|---|---|---|---|---|
| A stateless baseline | 60 | 28% | 5,336 | 18,831 | 4.3 | 23 | 0 | 0.0 | 0 | reference |
| B ACTIVE episodic memory only | 60 | 27% | 6,175 | 23,156 | 4.2 | 22 | 188 | 128.1 | 1,184,941 | never (execution is not cheaper per success: never recovered) |
| C ACTIVE consolidated memory only | 60 | 92% | 7,357 | 8,026 | 5.0 | 23 | 196 | 106.3 | 1,306,218 | 121 |
| D ACTIVE procedural memory only | 60 | 92% | 7,663 | 8,360 | 5.0 | 21 | 321 | 80.8 | 1,309,690 | 125 |
| E ACTIVE compiled capability only | 60 | 98% | 2,124 | 2,160 | 5.0 | 9 | 0 | 0.0 | 1,234,438 | 74 |
| F noise control (empty registry) | 60 | 30% | 5,316 | 17,720 | 4.3 | 23 | 0 | 0.0 | 0 | never (reference) |

### 5.3 Family `timeout` (family-specific learning ledger)

| Arm | runs | success | tokens/run | tokens/success | tool calls/run | latency s/run | retrieved tok/run | retrieval ms | learning tokens L | break-even (verified successes) |
|---|---|---|---|---|---|---|---|---|---|---|
| A stateless baseline | 59 | 47% | 6,304 | 13,284 | 4.1 | 29 | 0 | 0.0 | 0 | reference |
| B ACTIVE episodic memory only | 59 | 76% | 7,887 | 10,340 | 4.7 | 28 | 195 | 44.0 | 1,340,170 | 455 |
| C ACTIVE consolidated memory only | 58 | 95% | 8,127 | 8,570 | 5.0 | 25 | 196 | 38.1 | 1,316,244 | 279 |
| D ACTIVE procedural memory only | 59 | 97% | 7,750 | 8,022 | 5.0 | 22 | 346 | 45.2 | 1,346,013 | 256 |
| E ACTIVE compiled capability only | 60 | 98% | 2,963 | 3,013 | 5.3 | 15 | 0 | 0.0 | 1,429,459 | 139 |
| F noise control (empty registry) | 60 | 50% | 5,882 | 11,764 | 4.2 | 32 | 0 | 0.0 | 0 | never (reference) |

### 5.4 Sensitivity to maintenance (assumed re-validations) and to reuse volume

Maintenance is **not measured**. Each row assumes the arm's own validation is repeated R times over the asset's life (e.g. after drift). Break-even is in verified successes vs the stateless baseline; `tokens per verified success` is the all-in lifecycle cost (learning + maintenance + execution) per success after N future tasks.

| Arm | break-even, R=0 | R=1 | R=3 | all-in tokens/success at N=100 | N=1,000 | N=10,000 |
|---|---|---|---|---|---|---|
| B | 888 | 1,686 | 3,281 | 127,507 | 27,081 | 17,038 |
| C | 300 | 571 | 1,112 | 75,935 | 16,618 | 10,686 |
| D | 282 | 536 | 1,046 | 71,580 | 15,568 | 9,967 |
| E | 183 | 347 | 677 | 65,016 | 10,255 | 4,779 |

(The N columns assume R=1 and the measured success rate and tokens per task; the stateless baseline's all-in cost per success is its execution cost per success.)

## 6. Compiled capability: applicability, fallback and boundaries

| Family | tasks | eligible capability found | used | fell back to planning |
|---|---|---|---|---|
| pool | 60 | 100% | 100% | 2% |
| queue | 30 | 0% | 0% | 0% |
| timeout | 60 | 100% | 100% | 8% |

Applicability gate (deterministic, no model): tasks admitted per family and synthetic precondition violations admitted (must be 0).

| Capability | learned family | admitted/total by family | synthetic violations admitted |
|---|---|---|---|
| pool.f12f4fa920@1 | pool | pool: 60/60, queue: 0/30, timeout: 0/60 | 0/4 |
| timeout.e2fb15fa79@1 | timeout | pool: 0/60, queue: 0/30, timeout: 60/60 | 0/4 |

## 6b. Asset usage, fallback and false application (gated mode)

Every run records the exact asset version used (`asset_ref`) or the reason general planning ran instead. *False application* = an asset applied to a task outside the family it declares.

| Arm | family | tasks | asset applied | fell back to planning | false application | top fallback reasons |
|---|---|---|---|---|---|---|
| B ACTIVE episodic memory only | pool | 60 | 60/60 | 0/60 | 0/60 | - |
| B ACTIVE episodic memory only | queue | 30 | 0/30 | 30/30 | 0/30 | no applicable ACTIVE EPISODIC memory: mem_episodic_pool@1: task_family (x30) |
| B ACTIVE episodic memory only | timeout | 59 | 59/59 | 0/59 | 0/59 | - |
| C ACTIVE consolidated memory only | pool | 60 | 60/60 | 0/60 | 0/60 | - |
| C ACTIVE consolidated memory only | queue | 30 | 0/30 | 30/30 | 0/30 | no applicable ACTIVE CONSOLIDATED memory: mem_consolidated_pool@1: tas (x30) |
| C ACTIVE consolidated memory only | timeout | 58 | 58/58 | 0/58 | 0/58 | - |
| D ACTIVE procedural memory only | pool | 60 | 60/60 | 0/60 | 0/60 | - |
| D ACTIVE procedural memory only | queue | 30 | 0/30 | 30/30 | 0/30 | no applicable ACTIVE PROCEDURAL memory: mem_procedural_pool@1: task_fa (x30) |
| D ACTIVE procedural memory only | timeout | 59 | 59/59 | 0/59 | 0/59 | - |
| E ACTIVE compiled capability only | pool | 60 | 60/60 | 0/60 | 0/60 | - |
| E ACTIVE compiled capability only | queue | 30 | 0/30 | 30/30 | 0/30 | no eligible ACTIVE capability (catalog size 2) (x30) |
| E ACTIVE compiled capability only | timeout | 60 | 60/60 | 0/60 | 0/60 | - |
| F noise control (empty registry) | pool | 60 | 0/60 | 60/60 | 0/60 | no ACTIVE EPISODIC memory for this tenant (x60) |
| F noise control (empty registry) | queue | 30 | 0/30 | 30/30 | 0/30 | no ACTIVE EPISODIC memory for this tenant (x30) |
| F noise control (empty registry) | timeout | 60 | 0/60 | 60/60 | 0/60 | no ACTIVE EPISODIC memory for this tenant (x60) |

Exact asset versions used (arm, version): B:mem_episodic_pool@1 x60, B:mem_episodic_timeout@1 x59, C:mem_consolidated_pool@1 x60, C:mem_consolidated_timeout@1 x58, D:mem_procedural_pool@1 x60, D:mem_procedural_timeout@1 x59, E:pool.f12f4fa920@1 x60, E:timeout.e2fb15fa79@1 x60

## 7. Failure and regression analysis

Non-verified runs by termination:

| Arm | non-verified runs | termination counts |
|---|---|---|
| A stateless baseline | 101 | {'FINAL': 99, 'INVALID_ACTION': 2} |
| B ACTIVE episodic memory only | 85 | {'FINAL': 84, 'ESCALATED': 1} |
| C ACTIVE consolidated memory only | 36 | {'FINAL': 35, 'ESCALATED': 1} |
| D ACTIVE procedural memory only | 28 | {'FINAL': 28} |
| E ACTIVE compiled capability only | 25 | {'FINAL': 25} |
| F noise control (empty registry) | 96 | {'FINAL': 95, 'INVALID_ACTION': 1} |

Regressions against the baseline: tasks A solved (>= half of repetitions) that the arm did not.

| Arm | regressed tasks | improved tasks | task ids that regressed |
|---|---|---|---|
| B ACTIVE episodic memory only | 15 | 31 | hid-pool-hikari_shared-01, hid-pool-hikari_shared-11, hid-pool-hikari_shared-16, hid-pool-hikari_shared-19, hid-pool-hikari_shared-21, hid-pool-hikari_shared-22, hid-pool-hikari_shared-27, hid-pool-hikari_shared-28 |
| C ACTIVE consolidated memory only | 4 | 68 | hid-queue-amqp_channels-21, hid-queue-amqp_channels-27, hid-queue-amqp_channels-28, val-pool-psycopg_shared-14 |
| D ACTIVE procedural memory only | 3 | 76 | hid-pool-hikari_shared-11, hid-queue-amqp_channels-28, hid-timeout-okhttp_budget-00 |
| E ACTIVE compiled capability only | 1 | 77 | hid-queue-amqp_channels-27 |
| F noise control (empty registry) | 18 | 23 | hid-pool-hikari_shared-16, hid-pool-hikari_shared-19, hid-pool-hikari_shared-21, hid-pool-hikari_shared-26, hid-pool-hikari_shared-27, hid-pool-hikari_shared-28, hid-queue-amqp_channels-21, hid-queue-amqp_channels-27 |

## 8. Mechanical verdicts (restating the paired statistics, not interpreting them)

- Does E outperform episodic memory? Paired difference +40% [+32%, +48%] over 149 tasks; McNemar p=0.000; reading: **E better (bootstrap and McNemar)**.
- Does E outperform consolidated memory? Paired difference +7% [+2%, +13%] over 148 tasks; McNemar p=0.019; reading: **E better (bootstrap and McNemar)**.
- Does E outperform instruction-based reuse? Paired difference +2% [-3%, +8%] over 149 tasks; McNemar p=0.648; reading: **not distinguishable**.
- Does E outperform the stateless baseline? Paired difference +51% [+43%, +59%] over 149 tasks; McNemar p=0.000; reading: **E better (bootstrap and McNemar)**.

## 9. Statistical assumptions and limitations

- Tasks inside a family come from one generator and are treated as exchangeable draws; results do not generalise beyond these families, this simulated environment and the model(s) tested.
- Repetitions are collapsed inside each task, so they sharpen the per-task rate but add no tasks; with a few dozen tasks the intervals are wide.
- McNemar binarises each task and discards the size of the difference; the bootstrap keeps it. They can disagree at small n.
- Holm adjusts within each table only; across the whole report many comparisons are made and isolated small p-values should not be read as findings.
- Token counts include retrieval in the prompt but no price list is applied; maintenance cost is an input, not a measurement.
- Memory and instruction texts were fixed before any evaluation and never tuned on evaluation data; a better-engineered memory could do better.
