# Gated memory vs capability: production mode (generated)

**Gated mode.** Arms B-E read only ACTIVE governed assets (memory and capabilities were validated and promoted by the same policy); a task without an applicable ACTIVE asset runs under general planning with a recorded reason. F is the noise control: the same path with an empty registry.

Research question: with equivalent historical experience, does a compiled, validated, executable capability (E) outperform memory-based reuse (B episodic, C consolidated, D instructions) and the stateless baseline (A)? The design is [capability-vs-memory-design.md](../experiments/capability-vs-memory-design.md); the experiment is built to be able to falsify the hypothesis.

## 1. Configuration (reproducibility)

- Model: `vertex-ai:global` / `google/gemini-2.5-flash` (temperature 0.0); repetitions per task: 1; workers: 10. Repetitions are repeated draws from a non-deterministic model (the provider ignores seeds), not independent samples.
- Arms: A stateless baseline, B ACTIVE episodic memory only, C ACTIVE consolidated memory only, D ACTIVE procedural memory only, E ACTIVE compiled capability only, F noise control (empty registry), UB ungated episodic memory, UC ungated consolidated memory, UD ungated instruction reuse.
- Datasets: HIDDEN_TRANSFER: `connection-debug-sim` v3.0.0-private-x5 `cb5fa61ae850…`; VALIDATION: `connection-debug-sim` v3.0.0-private-x5 `bf6d7922c51a…`
- Discovery experience: 45 eligible trajectories (DISCOVERY split only), families ['pool', 'timeout']; the same set feeds B, C, D and the compiler.
- ACTIVE memory assets: mem_procedural_pool@1.
- Capabilities available to E (and rendered for D in legacy mode): pool.f12f4fa920@1 (catalog states ['ACTIVE']). 
- Memory preamble sha256 `c1196b1a6e4e92f9…`; up to 3 episodes; token estimator `chars/4`; verifier `sim-verifier/1`; runtime `baseline-runtime/0.1.0`; evaluator `evaluator/1`.
- Runs: 990 (9 UNKNOWN, excluded as missing data); distinct tasks: 150.
- Note: environment sim-env/1

## 2. Success by arm

Cells are the mean of per-task success rates (each task weighted equally); the number of distinct tasks is shown next to the group.

| Group | tasks | A | B | C | D | E | F | UB | UC | UD |
|---|---|---|---|---|---|---|---|---|---|---|
| all tasks | 150 | 28% | 35% | 39% | 64% | 66% | 35% | 17% | 13% | 0% |

### By task family

| Family | tasks | A | B | C | D | E | F | UB | UC | UD |
|---|---|---|---|---|---|---|---|---|---|---|
| pool | 60 | 23% | 33% | 40% | 97% | 98% | 33% | n/a | n/a | n/a |
| queue | 30 | 13% | 17% | 27% | 23% | 23% | 17% | 17% | 13% | 0% |
| timeout | 60 | 40% | 46% | 45% | 51% | 53% | 46% | n/a | n/a | n/a |

### By transfer level

`intra_variant`: seen libraries, new numbers. `cross_variant`: a learned family with an unseen library/layout. `cross_family`: a family never seen in discovery.

| Level | tasks | A | B | C | D | E | F | UB | UC | UD |
|---|---|---|---|---|---|---|---|---|---|---|
| cross_family | 30 | 13% | 17% | 27% | 23% | 23% | 17% | 17% | 13% | 0% |
| cross_variant | 60 | 38% | 38% | 48% | 73% | 76% | 35% | n/a | n/a | n/a |
| intra_variant | 60 | 24% | 41% | 36% | 75% | 76% | 44% | n/a | n/a | n/a |

### By variant

| Variant | tasks | A | B | C | D | E | F | UB | UC | UD |
|---|---|---|---|---|---|---|---|---|---|---|
| pool/hikari_shared | 30 | 33% | 40% | 43% | 97% | 100% | 33% | n/a | n/a | n/a |
| pool/psycopg_shared | 15 | 20% | 33% | 53% | 93% | 93% | 47% | n/a | n/a | n/a |
| pool/sqlalchemy_shared | 15 | 7% | 20% | 20% | 100% | 100% | 20% | n/a | n/a | n/a |
| queue/amqp_channels | 30 | 13% | 17% | 27% | 23% | 23% | 17% | 17% | 13% | 0% |
| timeout/httpx_budget | 15 | 29% | 43% | 23% | 43% | 50% | 57% | n/a | n/a | n/a |
| timeout/okhttp_budget | 30 | 43% | 37% | 53% | 50% | 52% | 37% | n/a | n/a | n/a |
| timeout/requests_budget | 15 | 43% | 67% | 47% | 60% | 60% | 53% | n/a | n/a | n/a |

## 3. Paired statistical comparisons

Unit = distinct task; repetitions are averaged inside a task. Paired bootstrap over tasks (10,000 resamples, fixed seed); exact McNemar on tasks "solved" in >= 50% of repetitions; Holm correction within each table. Wilson intervals are not used for inference here.

### 3.1 Primary: compiled capability (E) against every alternative

| Comparison | tasks | rate X | rate Y | mean paired diff [95% bootstrap CI] | discordant X-only/Y-only | McNemar p | Holm-adj p | reading |
|---|---|---|---|---|---|---|---|---|
| E vs A | 146 | 66% | 27% | +38% [+29%, +47%] | 61/5 | 0.000 | 0.000 | E better (bootstrap and McNemar) |
| E vs B | 147 | 65% | 35% | +30% [+20%, +39%] | 54/10 | 0.000 | 0.000 | E better (bootstrap and McNemar) |
| E vs C | 147 | 65% | 39% | +27% [+17%, +36%] | 50/11 | 0.000 | 0.000 | E better (bootstrap and McNemar) |
| E vs D | 147 | 65% | 64% | +1% [-6%, +9%] | 16/14 | 0.856 | 1.000 | not distinguishable |
| E vs F | 147 | 65% | 34% | +31% [+22%, +40%] | 54/8 | 0.000 | 0.000 | E better (bootstrap and McNemar) |
| E vs UB | 30 | 23% | 17% | +7% [-17%, +30%] | 7/5 | 0.774 | 1.000 | not distinguishable |
| E vs UC | 30 | 23% | 13% | +10% [-10%, +30%] | 6/3 | 0.508 | 1.000 | not distinguishable |
| E vs UD | 30 | 23% | 0% | +23% [+10%, +40%] | 7/0 | 0.016 | 0.062 | E ahead by bootstrap only (McNemar n.s.) |

E vs the best memory arm (**D**, chosen *post hoc* by overall rate, so this comparison favours memory):

| Comparison | tasks | rate X | rate Y | mean paired diff [95% bootstrap CI] | discordant X-only/Y-only | McNemar p | Holm-adj p | reading |
|---|---|---|---|---|---|---|---|---|
| E vs D | 147 | 65% | 64% | +1% [-6%, +9%] | 16/14 | 0.856 | 0.856 | not distinguishable |

### 3.2 Attribution ladder (where does any improvement come from?)

A->B/C: value of *information*. C->D: value of a procedure stated as *instructions*. D->E: value of *executing* the procedure instead of asking the model to follow it.

| Comparison | tasks | rate X | rate Y | mean paired diff [95% bootstrap CI] | discordant X-only/Y-only | McNemar p | Holm-adj p | reading |
|---|---|---|---|---|---|---|---|---|
| B vs A | 148 | 35% | 28% | +7% [-1%, +16%] | 27/16 | 0.126 | 0.379 | not distinguishable |
| C vs A | 147 | 39% | 28% | +11% [+3%, +18%] | 26/10 | 0.011 | 0.045 | C better (bootstrap and McNemar) |
| C vs B | 148 | 39% | 35% | +4% [-5%, +14%] | 28/22 | 0.480 | 0.960 | not distinguishable |
| D vs C | 148 | 64% | 39% | +25% [+16%, +34%] | 51/14 | 0.000 | 0.000 | D better (bootstrap and McNemar) |
| E vs D | 147 | 65% | 64% | +1% [-6%, +9%] | 16/14 | 0.856 | 0.960 | not distinguishable |

### 3.3 E vs alternatives, intra_variant tasks only

| Comparison | tasks | rate X | rate Y | mean paired diff [95% bootstrap CI] | discordant X-only/Y-only | McNemar p | Holm-adj p | reading |
|---|---|---|---|---|---|---|---|---|
| E vs A | 57 | 77% | 25% | +53% [+39%, +67%] | 32/2 | 0.000 | 0.000 | E better (bootstrap and McNemar) |
| E vs B | 58 | 76% | 41% | +34% [+19%, +50%] | 24/4 | 0.000 | 0.001 | E better (bootstrap and McNemar) |
| E vs C | 58 | 76% | 36% | +40% [+24%, +53%] | 26/3 | 0.000 | 0.000 | E better (bootstrap and McNemar) |
| E vs D | 58 | 76% | 76% | +0% [-10%, +10%] | 5/5 | 1.000 | 1.000 | not distinguishable |
| E vs F | 58 | 76% | 43% | +33% [+16%, +48%] | 24/5 | 0.001 | 0.003 | E better (bootstrap and McNemar) |
| E vs UB | 0 | 0% | 0% | +0% [+0%, +0%] | 0/0 | 1.000 | 1.000 | no data |
| E vs UC | 0 | 0% | 0% | +0% [+0%, +0%] | 0/0 | 1.000 | 1.000 | no data |
| E vs UD | 0 | 0% | 0% | +0% [+0%, +0%] | 0/0 | 1.000 | 1.000 | no data |

### 3.3 E vs alternatives, cross_variant tasks only

| Comparison | tasks | rate X | rate Y | mean paired diff [95% bootstrap CI] | discordant X-only/Y-only | McNemar p | Holm-adj p | reading |
|---|---|---|---|---|---|---|---|---|
| E vs A | 59 | 76% | 37% | +39% [+25%, +53%] | 24/1 | 0.000 | 0.000 | E better (bootstrap and McNemar) |
| E vs B | 59 | 76% | 39% | +37% [+24%, +51%] | 24/2 | 0.000 | 0.000 | E better (bootstrap and McNemar) |
| E vs C | 59 | 76% | 47% | +29% [+15%, +42%] | 20/3 | 0.000 | 0.002 | E better (bootstrap and McNemar) |
| E vs D | 59 | 76% | 73% | +3% [-7%, +14%] | 6/4 | 0.754 | 1.000 | not distinguishable |
| E vs F | 59 | 76% | 34% | +42% [+31%, +56%] | 25/0 | 0.000 | 0.000 | E better (bootstrap and McNemar) |
| E vs UB | 0 | 0% | 0% | +0% [+0%, +0%] | 0/0 | 1.000 | 1.000 | no data |
| E vs UC | 0 | 0% | 0% | +0% [+0%, +0%] | 0/0 | 1.000 | 1.000 | no data |
| E vs UD | 0 | 0% | 0% | +0% [+0%, +0%] | 0/0 | 1.000 | 1.000 | no data |

### 3.3 E vs alternatives, cross_family tasks only

| Comparison | tasks | rate X | rate Y | mean paired diff [95% bootstrap CI] | discordant X-only/Y-only | McNemar p | Holm-adj p | reading |
|---|---|---|---|---|---|---|---|---|
| E vs A | 30 | 23% | 13% | +10% [-7%, +27%] | 5/2 | 0.453 | 1.000 | not distinguishable |
| E vs B | 30 | 23% | 17% | +7% [-13%, +27%] | 6/4 | 0.754 | 1.000 | not distinguishable |
| E vs C | 30 | 23% | 27% | -3% [-23%, +17%] | 4/5 | 1.000 | 1.000 | not distinguishable |
| E vs D | 30 | 23% | 23% | +0% [-20%, +20%] | 5/5 | 1.000 | 1.000 | not distinguishable |
| E vs F | 30 | 23% | 17% | +7% [-10%, +23%] | 5/3 | 0.727 | 1.000 | not distinguishable |
| E vs UB | 30 | 23% | 17% | +7% [-17%, +30%] | 7/5 | 0.774 | 1.000 | not distinguishable |
| E vs UC | 30 | 23% | 13% | +10% [-10%, +30%] | 6/3 | 0.508 | 1.000 | not distinguishable |
| E vs UD | 30 | 23% | 0% | +23% [+10%, +40%] | 7/0 | 0.016 | 0.125 | E ahead by bootstrap only (McNemar n.s.) |

### 3.4 E vs alternatives, family `pool` only

| Comparison | tasks | rate X | rate Y | mean paired diff [95% bootstrap CI] | discordant X-only/Y-only | McNemar p | Holm-adj p | reading |
|---|---|---|---|---|---|---|---|---|
| E vs A | 60 | 98% | 23% | +75% [+63%, +85%] | 45/0 | 0.000 | 0.000 | E better (bootstrap and McNemar) |
| E vs B | 60 | 98% | 33% | +65% [+53%, +77%] | 39/0 | 0.000 | 0.000 | E better (bootstrap and McNemar) |
| E vs C | 60 | 98% | 40% | +58% [+47%, +70%] | 35/0 | 0.000 | 0.000 | E better (bootstrap and McNemar) |
| E vs D | 60 | 98% | 97% | +2% [-3%, +7%] | 2/1 | 1.000 | 1.000 | not distinguishable |
| E vs F | 60 | 98% | 33% | +65% [+52%, +78%] | 40/1 | 0.000 | 0.000 | E better (bootstrap and McNemar) |
| E vs UB | 0 | 0% | 0% | +0% [+0%, +0%] | 0/0 | 1.000 | 1.000 | no data |
| E vs UC | 0 | 0% | 0% | +0% [+0%, +0%] | 0/0 | 1.000 | 1.000 | no data |
| E vs UD | 0 | 0% | 0% | +0% [+0%, +0%] | 0/0 | 1.000 | 1.000 | no data |

### 3.4 E vs alternatives, family `queue` only

| Comparison | tasks | rate X | rate Y | mean paired diff [95% bootstrap CI] | discordant X-only/Y-only | McNemar p | Holm-adj p | reading |
|---|---|---|---|---|---|---|---|---|
| E vs A | 30 | 23% | 13% | +10% [-7%, +27%] | 5/2 | 0.453 | 1.000 | not distinguishable |
| E vs B | 30 | 23% | 17% | +7% [-13%, +27%] | 6/4 | 0.754 | 1.000 | not distinguishable |
| E vs C | 30 | 23% | 27% | -3% [-23%, +17%] | 4/5 | 1.000 | 1.000 | not distinguishable |
| E vs D | 30 | 23% | 23% | +0% [-20%, +20%] | 5/5 | 1.000 | 1.000 | not distinguishable |
| E vs F | 30 | 23% | 17% | +7% [-10%, +23%] | 5/3 | 0.727 | 1.000 | not distinguishable |
| E vs UB | 30 | 23% | 17% | +7% [-17%, +30%] | 7/5 | 0.774 | 1.000 | not distinguishable |
| E vs UC | 30 | 23% | 13% | +10% [-10%, +30%] | 6/3 | 0.508 | 1.000 | not distinguishable |
| E vs UD | 30 | 23% | 0% | +23% [+10%, +40%] | 7/0 | 0.016 | 0.125 | E ahead by bootstrap only (McNemar n.s.) |

### 3.4 E vs alternatives, family `timeout` only

| Comparison | tasks | rate X | rate Y | mean paired diff [95% bootstrap CI] | discordant X-only/Y-only | McNemar p | Holm-adj p | reading |
|---|---|---|---|---|---|---|---|---|
| E vs A | 56 | 54% | 39% | +14% [+2%, +27%] | 11/3 | 0.057 | 0.459 | E ahead by bootstrap only (McNemar n.s.) |
| E vs B | 57 | 53% | 47% | +5% [-9%, +19%] | 9/6 | 0.607 | 1.000 | not distinguishable |
| E vs C | 57 | 53% | 44% | +9% [-5%, +23%] | 11/6 | 0.332 | 1.000 | not distinguishable |
| E vs D | 57 | 53% | 51% | +2% [-12%, +16%] | 9/8 | 1.000 | 1.000 | not distinguishable |
| E vs F | 57 | 53% | 44% | +9% [-4%, +21%] | 9/4 | 0.267 | 1.000 | not distinguishable |
| E vs UB | 0 | 0% | 0% | +0% [+0%, +0%] | 0/0 | 1.000 | 1.000 | no data |
| E vs UC | 0 | 0% | 0% | +0% [+0%, +0%] | 0/0 | 1.000 | 1.000 | no data |
| E vs UD | 0 | 0% | 0% | +0% [+0%, +0%] | 0/0 | 1.000 | 1.000 | no data |

## 4. Execution efficiency and equal-budget success

Tokens include the retrieved memory, which sits inside every prompt of the run. Ungated arms (U*) ran only on families ['queue']; their rows are not comparable with all-family averages (see section 7b).

| Arm | context tokens (est., first call) | retrieved tokens | tokens/run | tokens/verified success |
|---|---|---|---|---|
| A stateless baseline | 307 | 0 | 5,933 | 21,417 |
| B ACTIVE episodic memory only | 307 | 0 | 6,169 | 17,677 |
| C ACTIVE consolidated memory only | 307 | 0 | 6,330 | 16,152 |
| D ACTIVE procedural memory only | 436 | 129 | 7,094 | 11,126 |
| E ACTIVE compiled capability only | n/a (runtime arm) | 0 | 5,007 | 7,639 |
| F noise control (empty registry) | 307 | 0 | 6,008 | 17,214 |
| UB ungated episodic memory | 512 | 188 | 8,411 | 50,467 |
| UC ungated consolidated memory | 684 | 360 | 10,658 | 79,936 |
| UD ungated instruction reuse | 959 | 635 | 10,839 | never |

Success rate when a run only counts if it verified within a token cap (equal resource budget):

| Arm | <= 3,000 | <= 6,000 | <= 12,000 | <= 24,000 | <= 60,000 |
|---|---|---|---|---|---|
| A stateless baseline | 0% | 8% | 28% | 28% | 28% |
| B ACTIVE episodic memory only | 0% | 9% | 34% | 35% | 35% |
| C ACTIVE consolidated memory only | 0% | 14% | 38% | 39% | 39% |
| D ACTIVE procedural memory only | 0% | 11% | 61% | 64% | 64% |
| E ACTIVE compiled capability only | 38% | 47% | 64% | 65% | 66% |
| F noise control (empty registry) | 0% | 14% | 35% | 35% | 35% |
| UB ungated episodic memory | 0% | 3% | 17% | 17% | 17% |
| UC ungated consolidated memory | 0% | 0% | 13% | 13% | 13% |
| UD ungated instruction reuse | 0% | 0% | 0% | 0% | 0% |

## 5. Lifecycle cost and break-even

`lifecycle_cost(N) = L + N * e + M`; break-even is the number of future *verified successes* after which the learning cost is recovered relative to the stateless baseline (`N* = L / (c_A - c_X)`, `c` = tokens per verified success, failures' cost included). Maintenance M = 0 tokens (**not measured**, which flatters every learning arm).

Learning ledger: discovery 257,325 tokens over 45 runs (192 tool calls); analysis 0; consolidation 0 (0 ms); compilation 0 (979 ms); validation 9,437,873.
- analysis, memory construction and compilation are deterministic (0 model tokens); only wall time is charged
- discovery cost is the model tokens of the discovery runs that every learning arm reuses
- validation tokens are summed over every stored evaluation of the arm's asset type, rejected assets included; the shared baseline was run once per session but each asset is charged the control tokens its own evaluation consumed

### 5.1 As built (each arm pays the recorded validation of its own asset type, rejected assets included)

| Arm | runs | success | tokens/run | tokens/success | tool calls/run | latency s/run | retrieved tok/run | retrieval ms | learning tokens L | break-even (verified successes) |
|---|---|---|---|---|---|---|---|---|---|---|
| A stateless baseline | 148 | 28% | 5,933 | 21,417 | 4.2 | 27 | 0 | 0.0 | 0 | reference |
| B ACTIVE episodic memory only | 149 | 35% | 6,169 | 17,677 | 4.2 | 27 | 0 | 70.8 | 2,525,111 | 675 |
| C ACTIVE consolidated memory only | 148 | 39% | 6,330 | 16,152 | 4.2 | 28 | 0 | 70.7 | 2,622,462 | 498 |
| D ACTIVE procedural memory only | 149 | 64% | 7,094 | 11,126 | 4.6 | 27 | 129 | 81.7 | 2,655,703 | 258 |
| E ACTIVE compiled capability only | 148 | 66% | 5,007 | 7,639 | 4.6 | 22 | 0 | 0.0 | 2,663,897 | 193 |
| F noise control (empty registry) | 149 | 35% | 6,008 | 17,214 | 4.3 | 26 | 0 | 0.1 | 0 | never (reference) |
| UB ungated episodic memory | 30 | 17% | 8,411 | 50,467 | 4.5 | 30 | 188 | 0.1 | 257,325 | never (execution is not cheaper per success: never recovered) |
| UC ungated consolidated memory | 30 | 13% | 10,658 | 79,936 | 4.9 | 33 | 360 | 3.2 | 257,325 | never (execution is not cheaper per success: never recovered) |
| UD ungated instruction reuse | 30 | 0% | 10,839 | never | 5.0 | 27 | 635 | 0.0 | 257,325 | never (no verified successes: never recovered) |

### 5.2 Equal governance: every learning arm pays the same mean validation cost

| Arm | runs | success | tokens/run | tokens/success | tool calls/run | latency s/run | retrieved tok/run | retrieval ms | learning tokens L | break-even (verified successes) |
|---|---|---|---|---|---|---|---|---|---|---|
| A stateless baseline | 148 | 28% | 5,933 | 21,417 | 4.2 | 27 | 0 | 0.0 | 0 | reference |
| B ACTIVE episodic memory only | 149 | 35% | 6,169 | 17,677 | 4.2 | 27 | 0 | 70.8 | 2,616,793 | 700 |
| C ACTIVE consolidated memory only | 148 | 39% | 6,330 | 16,152 | 4.2 | 28 | 0 | 70.7 | 2,616,793 | 497 |
| D ACTIVE procedural memory only | 149 | 64% | 7,094 | 11,126 | 4.6 | 27 | 129 | 81.7 | 2,616,793 | 254 |
| E ACTIVE compiled capability only | 148 | 66% | 5,007 | 7,639 | 4.6 | 22 | 0 | 0.0 | 2,616,793 | 190 |
| F noise control (empty registry) | 149 | 35% | 6,008 | 17,214 | 4.3 | 26 | 0 | 0.1 | 0 | never (reference) |

**Arms B, C applied no asset on any run** (nothing of their type reached ACTIVE), so they executed exactly like F. Their economics below price learning that was never used, and their differences from A measure run-to-run noise, not memory.

### 5.3 Family `pool` (family-specific learning ledger)

| Arm | runs | success | tokens/run | tokens/success | tool calls/run | latency s/run | retrieved tok/run | retrieval ms | learning tokens L | break-even (verified successes) |
|---|---|---|---|---|---|---|---|---|---|---|
| A stateless baseline | 60 | 23% | 5,241 | 22,462 | 4.2 | 23 | 0 | 0.0 | 0 | reference |
| B ACTIVE episodic memory only | 60 | 33% | 5,328 | 15,985 | 4.3 | 23 | 0 | 98.8 | 1,184,941 | 183 |
| C ACTIVE consolidated memory only | 60 | 40% | 5,562 | 13,904 | 4.3 | 24 | 0 | 122.7 | 1,306,218 | 153 |
| D ACTIVE procedural memory only | 60 | 97% | 7,383 | 7,637 | 5.0 | 20 | 321 | 136.3 | 1,309,690 | 88 |
| E ACTIVE compiled capability only | 60 | 98% | 2,093 | 2,129 | 5.1 | 9 | 0 | 0.0 | 1,234,438 | 61 |
| F noise control (empty registry) | 60 | 33% | 5,180 | 15,540 | 4.2 | 22 | 0 | 0.2 | 0 | never (reference) |

### 5.3 Family `timeout` (family-specific learning ledger)

| Arm | runs | success | tokens/run | tokens/success | tool calls/run | latency s/run | retrieved tok/run | retrieval ms | learning tokens L | break-even (verified successes) |
|---|---|---|---|---|---|---|---|---|---|---|
| A stateless baseline | 58 | 40% | 6,250 | 15,762 | 4.2 | 29 | 0 | 0.0 | 0 | reference |
| B ACTIVE episodic memory only | 59 | 46% | 6,416 | 14,020 | 4.1 | 29 | 0 | 52.4 | 1,340,170 | 769 |
| C ACTIVE consolidated memory only | 58 | 45% | 7,090 | 15,816 | 4.2 | 32 | 0 | 40.1 | 1,316,244 | never (execution is not cheaper per success: never recovered) |
| D ACTIVE procedural memory only | 59 | 51% | 6,751 | 13,277 | 4.3 | 31 | 0 | 46.2 | 1,346,013 | 542 |
| E ACTIVE compiled capability only | 58 | 53% | 7,354 | 13,760 | 4.2 | 33 | 0 | 0.0 | 1,429,459 | 714 |
| F noise control (empty registry) | 59 | 46% | 5,942 | 12,984 | 4.2 | 28 | 0 | 0.1 | 0 | never (reference) |

### 5.4 Sensitivity to maintenance (assumed re-validations) and to reuse volume

Maintenance is **not measured**. Each row assumes the arm's own validation is repeated R times over the asset's life (e.g. after drift). Break-even is in verified successes vs the stateless baseline; `tokens per verified success` is the all-in lifecycle cost (learning + maintenance + execution) per success after N future tasks.

| Arm | break-even, R=0 | R=1 | R=3 | all-in tokens/success at N=100 | N=1,000 | N=10,000 |
|---|---|---|---|---|---|---|
| B | 675 | 1,281 | 2,494 | 155,012 | 31,410 | 19,050 |
| C | 498 | 947 | 1,846 | 143,422 | 28,879 | 17,425 |
| D | 258 | 491 | 957 | 90,395 | 19,053 | 11,919 |
| E | 193 | 368 | 717 | 85,003 | 15,375 | 8,413 |

(The N columns assume R=1 and the measured success rate and tokens per task; the stateless baseline's all-in cost per success is its execution cost per success.)

## 6. Compiled capability: applicability, fallback and boundaries

| Family | tasks | eligible capability found | used | fell back to planning |
|---|---|---|---|---|
| pool | 60 | 100% | 100% | 2% |
| queue | 30 | 0% | 0% | 0% |
| timeout | 58 | 0% | 0% | 0% |

Applicability gate (deterministic, no model): tasks admitted per family and synthetic precondition violations admitted (must be 0).

| Capability | learned family | admitted/total by family | synthetic violations admitted |
|---|---|---|---|
| pool.f12f4fa920@1 | pool | pool: 60/60, queue: 0/30, timeout: 0/60 | 0/4 |

## 6b. Asset usage, fallback and false application (gated mode)

Every run records the exact asset version used (`asset_ref`) or the reason general planning ran instead. *False application* = an asset applied to a task outside the family it declares.

| Arm | family | tasks | asset applied | fell back to planning | false application | top fallback reasons |
|---|---|---|---|---|---|---|
| B ACTIVE episodic memory only | pool | 60 | 0/60 | 60/60 | 0/60 | no ACTIVE EPISODIC memory for this tenant (x60) |
| B ACTIVE episodic memory only | queue | 30 | 0/30 | 30/30 | 0/30 | no ACTIVE EPISODIC memory for this tenant (x30) |
| B ACTIVE episodic memory only | timeout | 59 | 0/59 | 59/59 | 0/59 | no ACTIVE EPISODIC memory for this tenant (x59) |
| C ACTIVE consolidated memory only | pool | 60 | 0/60 | 60/60 | 0/60 | no ACTIVE CONSOLIDATED memory for this tenant (x60) |
| C ACTIVE consolidated memory only | queue | 30 | 0/30 | 30/30 | 0/30 | no ACTIVE CONSOLIDATED memory for this tenant (x30) |
| C ACTIVE consolidated memory only | timeout | 58 | 0/58 | 58/58 | 0/58 | no ACTIVE CONSOLIDATED memory for this tenant (x58) |
| D ACTIVE procedural memory only | pool | 60 | 60/60 | 0/60 | 0/60 | - |
| D ACTIVE procedural memory only | queue | 30 | 0/30 | 30/30 | 0/30 | no applicable ACTIVE PROCEDURAL memory: mem_procedural_pool@1: task_fa (x30) |
| D ACTIVE procedural memory only | timeout | 59 | 0/59 | 59/59 | 0/59 | no applicable ACTIVE PROCEDURAL memory: mem_procedural_pool@1: task_fa (x59) |
| E ACTIVE compiled capability only | pool | 60 | 60/60 | 0/60 | 0/60 | - |
| E ACTIVE compiled capability only | queue | 30 | 0/30 | 30/30 | 0/30 | no eligible ACTIVE capability (catalog size 1) (x30) |
| E ACTIVE compiled capability only | timeout | 58 | 0/58 | 58/58 | 0/58 | no eligible ACTIVE capability (catalog size 1) (x58) |
| F noise control (empty registry) | pool | 60 | 0/60 | 60/60 | 0/60 | no ACTIVE EPISODIC memory for this tenant (x60) |
| F noise control (empty registry) | queue | 30 | 0/30 | 30/30 | 0/30 | no ACTIVE EPISODIC memory for this tenant (x30) |
| F noise control (empty registry) | timeout | 59 | 0/59 | 59/59 | 0/59 | no ACTIVE EPISODIC memory for this tenant (x59) |

Exact asset versions used (arm, version): D:mem_procedural_pool@1 x60, E:pool.f12f4fa920@1 x60

## 7b. Negative transfer: what gating prevents

On families `['queue']` (never seen in discovery) the ungated memory arms (UB/UC/UD: always inject, nearest experience when the family is unknown) were run beside the gated ones. Paired on identical tasks:

Family `queue`:

| Comparison | tasks | rate X | rate Y | mean paired diff [95% bootstrap CI] | discordant X-only/Y-only | McNemar p | Holm-adj p | reading |
|---|---|---|---|---|---|---|---|---|
| A vs UB | 30 | 13% | 17% | -3% [-17%, +10%] | 2/3 | 1.000 | 1.000 | not distinguishable |
| A vs UC | 30 | 13% | 13% | +0% [-13%, +13%] | 2/2 | 1.000 | 1.000 | not distinguishable |
| A vs UD | 30 | 13% | 0% | +13% [+3%, +27%] | 4/0 | 0.125 | 0.625 | A ahead by bootstrap only (McNemar n.s.) |
| B vs UB | 30 | 17% | 17% | +0% [-20%, +17%] | 4/4 | 1.000 | 1.000 | not distinguishable |
| C vs UC | 30 | 27% | 13% | +13% [+0%, +30%] | 5/1 | 0.219 | 0.875 | not distinguishable |
| D vs UD | 30 | 23% | 0% | +23% [+10%, +40%] | 7/0 | 0.016 | 0.094 | D ahead by bootstrap only (McNemar n.s.) |

- UB: +3% vs the stateless baseline (17% vs 13%); gating removes this effect by construction (no asset applies, so the run equals planning).
- UC: +0% vs the stateless baseline (13% vs 13%); gating removes this effect by construction (no asset applies, so the run equals planning).
- UD: -13% vs the stateless baseline (0% vs 13%); gating removes this effect by construction (no asset applies, so the run equals planning).

## 7. Failure and regression analysis

Non-verified runs by termination:

| Arm | non-verified runs | termination counts |
|---|---|---|
| A stateless baseline | 107 | {'FINAL': 105, 'INVALID_ACTION': 2} |
| B ACTIVE episodic memory only | 97 | {'FINAL': 95, 'INVALID_ACTION': 2} |
| C ACTIVE consolidated memory only | 90 | {'FINAL': 87, 'INVALID_ACTION': 2, 'ESCALATED': 1} |
| D ACTIVE procedural memory only | 54 | {'FINAL': 51, 'INVALID_ACTION': 2, 'ESCALATED': 1} |
| E ACTIVE compiled capability only | 51 | {'FINAL': 49, 'INVALID_ACTION': 1, 'ESCALATED': 1} |
| F noise control (empty registry) | 97 | {'FINAL': 95, 'INVALID_ACTION': 1, 'ESCALATED': 1} |
| UB ungated episodic memory | 25 | {'FINAL': 25} |
| UC ungated consolidated memory | 26 | {'FINAL': 26} |
| UD ungated instruction reuse | 30 | {'FINAL': 30} |

Regressions against the baseline: tasks A solved (>= half of repetitions) that the arm did not.

| Arm | regressed tasks | improved tasks | task ids that regressed |
|---|---|---|---|
| B ACTIVE episodic memory only | 16 | 27 | hid-pool-hikari_shared-05, hid-pool-hikari_shared-10, hid-pool-hikari_shared-15, hid-pool-hikari_shared-22, hid-pool-hikari_shared-28, hid-queue-amqp_channels-05, hid-queue-amqp_channels-10, hid-queue-amqp_channels-28 |
| C ACTIVE consolidated memory only | 10 | 26 | hid-pool-hikari_shared-05, hid-pool-hikari_shared-09, hid-pool-hikari_shared-10, hid-pool-hikari_shared-28, hid-timeout-okhttp_budget-18, val-timeout-httpx_budget-11, val-timeout-requests_budget-03, val-timeout-requests_budget-07 |
| D ACTIVE procedural memory only | 8 | 61 | hid-pool-hikari_shared-04, hid-queue-amqp_channels-05, hid-queue-amqp_channels-10, hid-queue-amqp_channels-20, hid-queue-amqp_channels-28, hid-timeout-okhttp_budget-08, hid-timeout-okhttp_budget-14, val-timeout-requests_budget-14 |
| E ACTIVE compiled capability only | 5 | 61 | hid-queue-amqp_channels-05, hid-queue-amqp_channels-10, hid-timeout-okhttp_budget-23, val-timeout-httpx_budget-04, val-timeout-requests_budget-03 |
| F noise control (empty registry) | 14 | 25 | hid-pool-hikari_shared-01, hid-pool-hikari_shared-09, hid-pool-hikari_shared-10, hid-pool-hikari_shared-28, hid-queue-amqp_channels-05, hid-queue-amqp_channels-20, hid-timeout-okhttp_budget-08, hid-timeout-okhttp_budget-18 |
| UB ungated episodic memory | 2 | 3 | hid-queue-amqp_channels-20, hid-queue-amqp_channels-28 |
| UC ungated consolidated memory | 2 | 2 | hid-queue-amqp_channels-05, hid-queue-amqp_channels-28 |
| UD ungated instruction reuse | 4 | 0 | hid-queue-amqp_channels-05, hid-queue-amqp_channels-10, hid-queue-amqp_channels-20, hid-queue-amqp_channels-28 |

## 8. Mechanical verdicts (restating the paired statistics, not interpreting them)

- Does E outperform episodic memory? Paired difference +30% [+20%, +39%] over 147 tasks; McNemar p=0.000; reading: **E better (bootstrap and McNemar)**.
- Does E outperform consolidated memory? Paired difference +27% [+17%, +36%] over 147 tasks; McNemar p=0.000; reading: **E better (bootstrap and McNemar)**.
- Does E outperform instruction-based reuse? Paired difference +1% [-6%, +9%] over 147 tasks; McNemar p=0.856; reading: **not distinguishable**.
- Does E outperform the stateless baseline? Paired difference +38% [+29%, +47%] over 146 tasks; McNemar p=0.000; reading: **E better (bootstrap and McNemar)**.

## 9. Statistical assumptions and limitations

- Tasks inside a family come from one generator and are treated as exchangeable draws; results do not generalise beyond these families, this simulated environment and the model(s) tested.
- Repetitions are collapsed inside each task, so they sharpen the per-task rate but add no tasks; with a few dozen tasks the intervals are wide.
- McNemar binarises each task and discards the size of the difference; the bootstrap keeps it. They can disagree at small n.
- Holm adjusts within each table only; across the whole report many comparisons are made and isolated small p-values should not be read as findings.
- Token counts include retrieval in the prompt but no price list is applied; maintenance cost is an input, not a measurement.
- Memory and instruction texts were fixed before any evaluation and never tuned on evaluation data; a better-engineered memory could do better.
