# Production-aligned learning evaluation (generated)

Generated from recorded runs by `ee-exp serving-report`. Serving policy `serving-low-v1` (hash `59c4bd37dc79…`), frozen in [production-aligned-evaluation-design.md](../experiments/production-aligned-evaluation-design.md) §4 before the data existed. **The policy is applied as a counterfactual to experiment rows: no asset was promoted, activated or changed.** E1 is derived from the capability phase of the E2 production runs (same runs, same tasks), never executed separately.

## Set C

Model `google/gemini-2.5-flash`; repetitions 1; asset states readable: ['ACTIVE', 'CANDIDATE']; capabilities offered: pool.f12f4fa920@1, timeout.e2fb15fa79@1; memory assets: mem_consolidated_pool@1, mem_consolidated_timeout@1, mem_episodic_pool@1, mem_episodic_timeout@1, mem_procedural_pool@1, mem_procedural_timeout@1. Runs 990 (1 UNKNOWN, missing data); distinct tasks 150; phase attribution recorded: yes. **Experimental asset states: candidate assets were readable; this is a research run, not promotion evidence.**

### Success by family (mean of per-task success; n = distinct tasks)

| Family | tasks | A stateless baseline | B episodic memory | C consolidated memory | D procedural memory | E1 capability only (derived) | E2 capability + production fallback | F empty-registry runtime |
|---|---|---|---|---|---|---|---|---|
| all | 150 | 29% (n=150) | 63% (n=150) | 66% (n=149) | 76% (n=150) | 92% (n=120) | 79% (n=150) | 30% (n=150) |
| pool | 60 | 23% (n=60) | 65% (n=60) | 95% (n=60) | 95% (n=60) | 97% (n=60) | 100% (n=60) | 22% (n=60) |
| queue | 30 | 10% (n=30) | 23% (n=30) | 7% (n=30) | 7% (n=30) | n/a (n=0) | 10% (n=30) | 23% (n=30) |
| timeout | 60 | 45% (n=60) | 82% (n=60) | 66% (n=59) | 92% (n=60) | 88% (n=60) | 92% (n=60) | 42% (n=60) |

### Success by variant

| Variant | tasks | A stateless baseline | B episodic memory | C consolidated memory | D procedural memory | E1 capability only (derived) | E2 capability + production fallback | F empty-registry runtime |
|---|---|---|---|---|---|---|---|---|
| pool/hikari_shared | 30 | 27% | 67% | 97% | 97% | 100% | 100% | 17% |
| pool/psycopg_shared | 15 | 20% | 73% | 100% | 93% | 93% | 100% | 40% |
| pool/sqlalchemy_shared | 15 | 20% | 53% | 87% | 93% | 93% | 100% | 13% |
| queue/amqp_channels | 30 | 10% | 23% | 7% | 7% | n/a | 10% | 23% |
| timeout/httpx_budget | 15 | 40% | 80% | 67% | 100% | 93% | 93% | 40% |
| timeout/okhttp_budget | 30 | 50% | 80% | 50% | 93% | 90% | 90% | 37% |
| timeout/requests_budget | 15 | 40% | 87% | 100% | 80% | 80% | 93% | 53% |

### Paired comparisons with the stateless baseline (task level; Holm across this table)

| Scope | Arm vs A | tasks | rate arm | rate A | paired diff [95% cluster-bootstrap CI] | discordant arm-only/A-only | McNemar p (Holm) |
|---|---|---|---|---|---|---|---|
| all | B episodic memory | 150 | 63% | 29% | +34.0% [+24.7%, +43.3%] | 59/8 | 0.000 (0.000) |
| all | C consolidated memory | 149 | 66% | 30% | +36.2% [+26.2%, +45.6%] | 64/10 | 0.000 (0.000) |
| all | D procedural memory | 150 | 76% | 29% | +46.7% [+38.0%, +55.3%] | 74/4 | 0.000 (0.000) |
| all | E1 capability only (derived) | 120 | 92% | 34% | +58.3% [+48.3%, +67.5%] | 72/2 | 0.000 (0.000) |
| all | E2 capability + production fallback | 150 | 79% | 29% | +49.3% [+40.0%, +58.0%] | 79/5 | 0.000 (0.000) |
| all | F empty-registry runtime | 150 | 30% | 29% | +0.7% [-7.3%, +9.3%] | 21/20 | 1.000 (1.000) |
| pool | B episodic memory | 60 | 65% | 23% | +41.7% [+26.7%, +56.7%] | 28/3 | 0.000 (0.000) |
| pool | C consolidated memory | 60 | 95% | 23% | +71.7% [+58.3%, +83.3%] | 44/1 | 0.000 (0.000) |
| pool | D procedural memory | 60 | 95% | 23% | +71.7% [+58.3%, +83.3%] | 44/1 | 0.000 (0.000) |
| pool | E1 capability only (derived) | 60 | 97% | 23% | +73.3% [+61.7%, +83.3%] | 44/0 | 0.000 (0.000) |
| pool | E2 capability + production fallback | 60 | 100% | 23% | +76.7% [+65.0%, +86.7%] | 46/0 | 0.000 (0.000) |
| pool | F empty-registry runtime | 60 | 22% | 23% | -1.7% [-16.7%, +13.3%] | 9/10 | 1.000 (1.000) |
| queue | B episodic memory | 30 | 23% | 10% | +13.3% [-3.3%, +30.0%] | 6/2 | 0.289 (1.000) |
| queue | C consolidated memory | 30 | 7% | 10% | -3.3% [-13.3%, +6.7%] | 1/2 | 1.000 (1.000) |
| queue | D procedural memory | 30 | 7% | 10% | -3.3% [-13.3%, +6.7%] | 1/2 | 1.000 (1.000) |
| queue | E1 capability only (derived) | 0 | 0% | 0% | +0.0% [+0.0%, +0.0%] | 0/0 | 1.000 (1.000) |
| queue | E2 capability + production fallback | 30 | 10% | 10% | +0.0% [-16.7%, +16.7%] | 3/3 | 1.000 (1.000) |
| queue | F empty-registry runtime | 30 | 23% | 10% | +13.3% [+3.3%, +26.7%] | 4/0 | 0.125 (1.000) |
| timeout | B episodic memory | 60 | 82% | 45% | +36.7% [+21.7%, +51.7%] | 25/3 | 0.000 (0.000) |
| timeout | C consolidated memory | 59 | 66% | 46% | +20.3% [+3.4%, +35.6%] | 19/7 | 0.029 (0.290) |
| timeout | D procedural memory | 60 | 92% | 45% | +46.7% [+33.3%, +60.0%] | 29/1 | 0.000 (0.000) |
| timeout | E1 capability only (derived) | 60 | 88% | 45% | +43.3% [+28.3%, +56.7%] | 28/2 | 0.000 (0.000) |
| timeout | E2 capability + production fallback | 60 | 92% | 45% | +46.7% [+31.7%, +60.0%] | 30/2 | 0.000 (0.000) |
| timeout | F empty-registry runtime | 60 | 42% | 45% | -3.3% [-16.7%, +10.0%] | 8/10 | 0.815 (1.000) |

### Stratified production estimate (strata = family; weights = sampled task counts)

| Arm | stratum means (family: success) | overall, weighted by task counts [95% CI] | overall diff vs A [95% CI] |
|---|---|---|---|
| A stateless baseline | pool: 23% (n=60), queue: 10% (n=30), timeout: 45% (n=60) | 29.3% [23.3%, 35.3%] | +0.0% [+0.0%, +0.0%] |
| B episodic memory | pool: 65% (n=60), queue: 23% (n=30), timeout: 82% (n=60) | 63.3% [57.3%, 69.3%] | +34.0% [+26.7%, +41.3%] |
| C consolidated memory | pool: 95% (n=60), queue: 7% (n=30), timeout: 66% (n=59) | 65.8% [61.1%, 70.5%] | +36.2% [+29.5%, +43.0%] |
| D procedural memory | pool: 95% (n=60), queue: 7% (n=30), timeout: 92% (n=60) | 76.0% [72.7%, 79.3%] | +46.7% [+40.0%, +53.3%] |
| E2 capability + production fallback | pool: 100% (n=60), queue: 10% (n=30), timeout: 92% (n=60) | 78.7% [76.0%, 81.3%] | +49.3% [+42.7%, +56.0%] |
| F empty-registry runtime | pool: 22% (n=60), queue: 23% (n=30), timeout: 42% (n=60) | 30.0% [24.0%, 36.0%] | +0.7% [-6.0%, +8.0%] |

### Cost, tool calls and latency (all runs; fallback cost is inside E2)

| Arm | runs | success | tokens/run | tokens per verified success | tool calls/run | latency s/run |
|---|---|---|---|---|---|---|
| A stateless baseline | 150 | 29% | 6,003 | 20,465 | 4.3 | 25 |
| B episodic memory | 150 | 63% | 7,585 | 11,976 | 4.7 | 25 |
| C consolidated memory | 149 | 66% | 7,458 | 11,339 | 4.7 | 26 |
| D procedural memory | 150 | 76% | 8,027 | 10,562 | 4.9 | 23 |
| E1 capability only (derived) | 120 | 92% | 2,180 | 2,356 | 5.0 | 9 |
| E2 capability + production fallback | 150 | 79% | 3,466 | 4,407 | 5.1 | 14 |
| F empty-registry runtime | 150 | 30% | 5,966 | 19,885 | 4.2 | 25 |
| UB ungated episodic | 30 | 0% | 8,375 | nan | 4.9 | 26 |
| UC ungated consolidated | 30 | 3% | 9,051 | 271,535 | 4.9 | 26 |
| UD ungated instructions | 30 | 0% | 9,530 | nan | 5.0 | 19 |

### Capability / fallback attribution

| Capability | tasks attempted | E1 capability-only success | A on the same tasks | fallback frequency | recovery: final success after capability failed + fallback | A on those same tasks | E2 final success | capability tokens | fallback tokens | fallback share of tokens | fallback causes |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `pool.f12f4fa920@1` | 60 (capability outcome unknown: 0) | 97% | 23% | 2/60 (3%) | 2/2 (100%) | 0% | 100% | 110,685 | 11,833 | 10% | {'DECISION_FAILURE': 1, 'VERIFICATION_FAILURE': 1} |
| `timeout.e2fb15fa79@1` | 60 (capability outcome unknown: 0) | 88% | 45% | 7/60 (12%) | 2/7 (29%) | 29% | 92% | 150,863 | 46,565 | 24% | {'VERIFICATION_FAILURE': 4, 'DECISION_FAILURE': 3} |

### Applicability

| Arm | asset | applied in-family | applied out-of-family (false application) | in-family tasks with no asset applied |
|---|---|---|---|---|
| B episodic memory | `mem_episodic_pool@1` | 60 | 0 | 0/60 |
| B episodic memory | `mem_episodic_timeout@1` | 60 | 0 | 0/60 |
| C consolidated memory | `mem_consolidated_pool@1` | 60 | 0 | 0/60 |
| C consolidated memory | `mem_consolidated_timeout@1` | 60 | 0 | 0/60 |
| D procedural memory | `mem_procedural_pool@1` | 60 | 0 | 0/60 |
| D procedural memory | `mem_procedural_timeout@1` | 60 | 0 | 0/60 |
| E2 capability + production fallback | `pool.f12f4fa920@1` | 60 | 0 | 0/60 |
| E2 capability + production fallback | `timeout.e2fb15fa79@1` | 60 | 0 | 0/60 |

### Negative transfer (ungated memory on families never learned)

Family `queue` (30 tasks): A stateless baseline 10%; UB ungated episodic 0%; UC ungated consolidated 3%; UD ungated instructions 0%; B episodic memory 23%; C consolidated memory 7%; D procedural memory 7%; E2 capability + production fallback 10%

## Set D

Model `google/gemini-2.5-flash`; repetitions 1; asset states readable: ['ACTIVE', 'CANDIDATE']; capabilities offered: pool.f12f4fa920@1, timeout.e2fb15fa79@1; memory assets: mem_consolidated_pool@1, mem_consolidated_timeout@1, mem_episodic_pool@1, mem_episodic_timeout@1, mem_procedural_pool@1, mem_procedural_timeout@1. Runs 990 (3 UNKNOWN, missing data); distinct tasks 150; phase attribution recorded: yes. **Experimental asset states: candidate assets were readable; this is a research run, not promotion evidence.**

### Success by family (mean of per-task success; n = distinct tasks)

| Family | tasks | A stateless baseline | B episodic memory | C consolidated memory | D procedural memory | E1 capability only (derived) | E2 capability + production fallback | F empty-registry runtime |
|---|---|---|---|---|---|---|---|---|
| all | 150 | 34% (n=149) | 65% (n=150) | 61% (n=150) | 72% (n=149) | 92% (n=120) | 78% (n=150) | 34% (n=149) |
| pool | 60 | 22% (n=60) | 63% (n=60) | 95% (n=60) | 88% (n=60) | 93% (n=60) | 97% (n=60) | 28% (n=60) |
| queue | 30 | 13% (n=30) | 17% (n=30) | 10% (n=30) | 7% (n=30) | n/a (n=0) | 10% (n=30) | 10% (n=30) |
| timeout | 60 | 56% (n=59) | 90% (n=60) | 52% (n=60) | 90% (n=59) | 92% (n=60) | 93% (n=60) | 51% (n=59) |

### Success by variant

| Variant | tasks | A stateless baseline | B episodic memory | C consolidated memory | D procedural memory | E1 capability only (derived) | E2 capability + production fallback | F empty-registry runtime |
|---|---|---|---|---|---|---|---|---|
| pool/hikari_shared | 30 | 17% | 73% | 100% | 93% | 100% | 100% | 30% |
| pool/psycopg_shared | 15 | 33% | 67% | 93% | 93% | 100% | 100% | 40% |
| pool/sqlalchemy_shared | 15 | 20% | 40% | 87% | 73% | 73% | 87% | 13% |
| queue/amqp_channels | 30 | 13% | 17% | 10% | 7% | n/a | 10% | 10% |
| timeout/httpx_budget | 15 | 57% | 87% | 60% | 86% | 100% | 100% | 50% |
| timeout/okhttp_budget | 30 | 50% | 90% | 47% | 87% | 93% | 93% | 47% |
| timeout/requests_budget | 15 | 67% | 93% | 53% | 100% | 80% | 87% | 60% |

### Paired comparisons with the stateless baseline (task level; Holm across this table)

| Scope | Arm vs A | tasks | rate arm | rate A | paired diff [95% cluster-bootstrap CI] | discordant arm-only/A-only | McNemar p (Holm) |
|---|---|---|---|---|---|---|---|
| all | B episodic memory | 149 | 64% | 34% | +30.9% [+21.5%, +40.3%] | 56/10 | 0.000 (0.000) |
| all | C consolidated memory | 149 | 60% | 34% | +26.8% [+16.8%, +36.9%] | 54/14 | 0.000 (0.000) |
| all | D procedural memory | 148 | 72% | 33% | +39.2% [+29.7%, +48.6%] | 65/7 | 0.000 (0.000) |
| all | E1 capability only (derived) | 119 | 92% | 39% | +53.8% [+43.7%, +63.0%] | 67/3 | 0.000 (0.000) |
| all | E2 capability + production fallback | 149 | 78% | 34% | +44.3% [+34.9%, +53.0%] | 71/5 | 0.000 (0.000) |
| all | F empty-registry runtime | 148 | 34% | 34% | +0.0% [-8.8%, +8.8%] | 22/22 | 1.000 (1.000) |
| pool | B episodic memory | 60 | 63% | 22% | +41.7% [+25.0%, +58.3%] | 30/5 | 0.000 (0.000) |
| pool | C consolidated memory | 60 | 95% | 22% | +73.3% [+61.7%, +83.3%] | 44/0 | 0.000 (0.000) |
| pool | D procedural memory | 60 | 88% | 22% | +66.7% [+51.7%, +80.0%] | 42/2 | 0.000 (0.000) |
| pool | E1 capability only (derived) | 60 | 93% | 22% | +71.7% [+58.3%, +83.3%] | 44/1 | 0.000 (0.000) |
| pool | E2 capability + production fallback | 60 | 97% | 22% | +75.0% [+61.7%, +86.7%] | 46/1 | 0.000 (0.000) |
| pool | F empty-registry runtime | 60 | 28% | 22% | +6.7% [-8.3%, +21.7%] | 13/9 | 0.523 (1.000) |
| queue | B episodic memory | 30 | 17% | 13% | +3.3% [-16.7%, +23.3%] | 5/4 | 1.000 (1.000) |
| queue | C consolidated memory | 30 | 10% | 13% | -3.3% [-20.0%, +13.3%] | 3/4 | 1.000 (1.000) |
| queue | D procedural memory | 30 | 7% | 13% | -6.7% [-23.3%, +10.0%] | 2/4 | 0.688 (1.000) |
| queue | E1 capability only (derived) | 0 | 0% | 0% | +0.0% [+0.0%, +0.0%] | 0/0 | 1.000 (1.000) |
| queue | E2 capability + production fallback | 30 | 10% | 13% | -3.3% [-16.7%, +10.0%] | 2/3 | 1.000 (1.000) |
| queue | F empty-registry runtime | 30 | 10% | 13% | -3.3% [-20.0%, +13.3%] | 3/4 | 1.000 (1.000) |
| timeout | B episodic memory | 59 | 90% | 56% | +33.9% [+20.3%, +47.5%] | 21/1 | 0.000 (0.000) |
| timeout | C consolidated memory | 59 | 51% | 56% | -5.1% [-18.6%, +8.5%] | 7/10 | 0.629 (1.000) |
| timeout | D procedural memory | 58 | 90% | 55% | +34.5% [+22.4%, +48.3%] | 21/1 | 0.000 (0.000) |
| timeout | E1 capability only (derived) | 59 | 92% | 56% | +35.6% [+22.0%, +49.2%] | 23/2 | 0.000 (0.000) |
| timeout | E2 capability + production fallback | 59 | 93% | 56% | +37.3% [+23.7%, +50.8%] | 23/1 | 0.000 (0.000) |
| timeout | F empty-registry runtime | 58 | 52% | 57% | -5.2% [-19.0%, +8.6%] | 6/9 | 0.607 (1.000) |

### Stratified production estimate (strata = family; weights = sampled task counts)

| Arm | stratum means (family: success) | overall, weighted by task counts [95% CI] | overall diff vs A [95% CI] |
|---|---|---|---|
| A stateless baseline | pool: 22% (n=60), queue: 13% (n=30), timeout: 56% (n=59) | 33.6% [28.2%, 38.9%] | +0.0% [+0.0%, +0.0%] |
| B episodic memory | pool: 63% (n=60), queue: 17% (n=30), timeout: 90% (n=60) | 64.7% [59.3%, 70.0%] | +30.9% [+23.5%, +38.3%] |
| C consolidated memory | pool: 95% (n=60), queue: 10% (n=30), timeout: 52% (n=60) | 60.7% [56.0%, 65.3%] | +26.8% [+20.1%, +33.6%] |
| D procedural memory | pool: 88% (n=60), queue: 7% (n=30), timeout: 90% (n=59) | 72.5% [68.5%, 76.5%] | +39.2% [+32.4%, +45.9%] |
| E2 capability + production fallback | pool: 97% (n=60), queue: 10% (n=30), timeout: 93% (n=60) | 78.0% [74.7%, 81.3%] | +44.3% [+37.6%, +50.3%] |
| F empty-registry runtime | pool: 28% (n=60), queue: 10% (n=30), timeout: 51% (n=59) | 33.6% [27.5%, 39.6%] | +0.0% [-7.4%, +7.4%] |

### Cost, tool calls and latency (all runs; fallback cost is inside E2)

| Arm | runs | success | tokens/run | tokens per verified success | tool calls/run | latency s/run |
|---|---|---|---|---|---|---|
| A stateless baseline | 149 | 34% | 6,149 | 18,324 | 4.2 | 26 |
| B episodic memory | 150 | 65% | 7,414 | 11,465 | 4.7 | 24 |
| C consolidated memory | 150 | 61% | 7,221 | 11,902 | 4.6 | 26 |
| D procedural memory | 149 | 72% | 7,781 | 10,735 | 4.9 | 22 |
| E1 capability only (derived) | 120 | 92% | 2,323 | 2,511 | 5.0 | 10 |
| E2 capability + production fallback | 150 | 78% | 3,563 | 4,568 | 5.1 | 15 |
| F empty-registry runtime | 149 | 34% | 5,988 | 17,845 | 4.2 | 25 |
| UB ungated episodic | 30 | 10% | 8,603 | 86,031 | 4.8 | 27 |
| UC ungated consolidated | 30 | 7% | 10,342 | 155,127 | 4.9 | 32 |
| UD ungated instructions | 30 | 3% | 9,423 | 282,677 | 5.0 | 20 |

### Capability / fallback attribution

| Capability | tasks attempted | E1 capability-only success | A on the same tasks | fallback frequency | recovery: final success after capability failed + fallback | A on those same tasks | E2 final success | capability tokens | fallback tokens | fallback share of tokens | fallback causes |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `pool.f12f4fa920@1` | 60 (capability outcome unknown: 0) | 93% | 22% | 4/60 (7%) | 2/4 (50%) | 25% | 97% | 121,021 | 24,775 | 17% | {'VERIFICATION_FAILURE': 4} |
| `timeout.e2fb15fa79@1` | 60 (capability outcome unknown: 0) | 92% | 56% | 5/60 (8%) | 1/5 (20%) | 40% | 93% | 157,744 | 31,313 | 17% | {'DECISION_FAILURE': 3, 'VERIFICATION_FAILURE': 2} |

### Applicability

| Arm | asset | applied in-family | applied out-of-family (false application) | in-family tasks with no asset applied |
|---|---|---|---|---|
| B episodic memory | `mem_episodic_pool@1` | 60 | 0 | 0/60 |
| B episodic memory | `mem_episodic_timeout@1` | 60 | 0 | 0/60 |
| C consolidated memory | `mem_consolidated_pool@1` | 60 | 0 | 0/60 |
| C consolidated memory | `mem_consolidated_timeout@1` | 60 | 0 | 0/60 |
| D procedural memory | `mem_procedural_pool@1` | 60 | 0 | 0/60 |
| D procedural memory | `mem_procedural_timeout@1` | 60 | 0 | 0/60 |
| E2 capability + production fallback | `pool.f12f4fa920@1` | 60 | 0 | 0/60 |
| E2 capability + production fallback | `timeout.e2fb15fa79@1` | 60 | 0 | 0/60 |

### Negative transfer (ungated memory on families never learned)

Family `queue` (30 tasks): A stateless baseline 13%; UB ungated episodic 10%; UC ungated consolidated 7%; UD ungated instructions 3%; B episodic memory 17%; C consolidated memory 10%; D procedural memory 7%; E2 capability + production fallback 10%

## Asset decisions (three separate questions + mandatory safety)

| Set | Asset | in-scope tasks | Safety | Intrinsic (Layer A) | Serving improves (Layer B) | Economic | Classification |
|---|---|---|---|---|---|---|---|
| C | `mem_episodic_pool@1` | 60 | PASS | FAIL | INCONCLUSIVE | INCONCLUSIVE | insufficiently evaluated |
| C | `mem_episodic_timeout@1` | 60 | PASS | INCONCLUSIVE | INCONCLUSIVE | INCONCLUSIVE | insufficiently evaluated |
| C | `mem_consolidated_pool@1` | 60 | PASS | INCONCLUSIVE | INCONCLUSIVE | INCONCLUSIVE | insufficiently evaluated |
| C | `mem_consolidated_timeout@1` | 59 | PASS | FAIL | INCONCLUSIVE | INCONCLUSIVE | insufficiently evaluated |
| C | `mem_procedural_pool@1` | 60 | PASS | INCONCLUSIVE | INCONCLUSIVE | INCONCLUSIVE | insufficiently evaluated |
| C | `mem_procedural_timeout@1` | 60 | PASS | PASS | PASS | PASS | intrinsically reliable and economically useful |
| C | `pool.f12f4fa920@1` | 60 | PASS | PASS | PASS | PASS | intrinsically reliable and economically useful |
| C | `timeout.e2fb15fa79@1` | 60 | PASS | INCONCLUSIVE | INCONCLUSIVE | INCONCLUSIVE | insufficiently evaluated |
| D | `mem_episodic_pool@1` | 60 | PASS | FAIL | INCONCLUSIVE | INCONCLUSIVE | insufficiently evaluated |
| D | `mem_episodic_timeout@1` | 59 | PASS | PASS | PASS | PASS | intrinsically reliable and economically useful |
| D | `mem_consolidated_pool@1` | 60 | PASS | INCONCLUSIVE | INCONCLUSIVE | INCONCLUSIVE | insufficiently evaluated |
| D | `mem_consolidated_timeout@1` | 59 | PASS | FAIL | INCONCLUSIVE | INCONCLUSIVE | insufficiently evaluated |
| D | `mem_procedural_pool@1` | 60 | PASS | INCONCLUSIVE | INCONCLUSIVE | INCONCLUSIVE | insufficiently evaluated |
| D | `mem_procedural_timeout@1` | 58 | PASS | PASS | PASS | INCONCLUSIVE | insufficiently evaluated |
| D | `pool.f12f4fa920@1` | 60 | PASS | INCONCLUSIVE | INCONCLUSIVE | INCONCLUSIVE | insufficiently evaluated |
| D | `timeout.e2fb15fa79@1` | 59 | PASS | PASS | PASS | PASS | intrinsically reliable and economically useful |

Reasons and evidence requirements:

* `C:mem_episodic_pool@1`
  * safety: no violation observed on the exercised constraints (untested: cross-tenant access (single-tenant benchmark); forbidden-resource modification (no protected resources in the simulated environment))
  * intrinsic FAIL: asset-alone success 65% < 75% over 60 tasks; asset-alone regression 3/14 baseline-solved tasks, exact 95% bounds [6%, 47%]: cannot show <= 20%.
  * serving INCONCLUSIVE: serving regression 3/14 baseline-solved tasks, exact 95% bounds [6%, 47%]: cannot show <= 20%. **Requires:** 23 more independent baseline-solved tasks (at the observed 3 regressions; 14 minimum).
  * economic INCONCLUSIVE: total tokens per verified success (incl. any failed capability attempt and the fallback) 0.51x the baseline's, 95% one-sided bounds [0.33, 0.71]; savings never count unless safety and serving pass.
* `C:mem_episodic_timeout@1`
  * safety: no violation observed on the exercised constraints (untested: cross-tenant access (single-tenant benchmark); forbidden-resource modification (no protected resources in the simulated environment))
  * intrinsic INCONCLUSIVE: asset-alone regression 3/27 baseline-solved tasks, exact 95% bounds [3%, 26%]: cannot show <= 20%. **Requires:** 10 more independent baseline-solved tasks (at the observed 3 regressions; 14 minimum).
  * serving INCONCLUSIVE: serving regression 3/27 baseline-solved tasks, exact 95% bounds [3%, 26%]: cannot show <= 20%. **Requires:** 10 more independent baseline-solved tasks (at the observed 3 regressions; 14 minimum).
  * economic INCONCLUSIVE: total tokens per verified success (incl. any failed capability attempt and the fallback) 0.68x the baseline's, 95% one-sided bounds [0.51, 0.86]; savings never count unless safety and serving pass.
* `C:mem_consolidated_pool@1`
  * safety: no violation observed on the exercised constraints (untested: cross-tenant access (single-tenant benchmark); forbidden-resource modification (no protected resources in the simulated environment))
  * intrinsic INCONCLUSIVE: asset-alone regression 1/14 baseline-solved tasks, exact 95% bounds [0%, 30%]: cannot show <= 20%. **Requires:** 8 more independent baseline-solved tasks (at the observed 1 regressions; 14 minimum).
  * serving INCONCLUSIVE: serving regression 1/14 baseline-solved tasks, exact 95% bounds [0%, 30%]: cannot show <= 20%. **Requires:** 8 more independent baseline-solved tasks (at the observed 1 regressions; 14 minimum).
  * economic INCONCLUSIVE: total tokens per verified success (incl. any failed capability attempt and the fallback) 0.36x the baseline's, 95% one-sided bounds [0.23, 0.49]; savings never count unless safety and serving pass.
* `C:mem_consolidated_timeout@1`
  * safety: no violation observed on the exercised constraints (untested: cross-tenant access (single-tenant benchmark); forbidden-resource modification (no protected resources in the simulated environment))
  * intrinsic FAIL: asset-alone success 66% < 75% over 59 tasks; asset-alone regression 7/27 baseline-solved tasks, exact 95% bounds [13%, 43%]: cannot show <= 20%.
  * serving INCONCLUSIVE: serving regression 7/27 baseline-solved tasks, exact 95% bounds [13%, 43%]: cannot show <= 20%. **Requires:** 36 more independent baseline-solved tasks (at the observed 7 regressions; 14 minimum).
  * economic INCONCLUSIVE: total tokens per verified success (incl. any failed capability attempt and the fallback) 0.82x the baseline's, 95% one-sided bounds [0.63, 1.05]: cheaper per success is not demonstrated. **Requires:** more independent tasks.
* `C:mem_procedural_pool@1`
  * safety: no violation observed on the exercised constraints (untested: cross-tenant access (single-tenant benchmark); forbidden-resource modification (no protected resources in the simulated environment))
  * intrinsic INCONCLUSIVE: asset-alone regression 1/14 baseline-solved tasks, exact 95% bounds [0%, 30%]: cannot show <= 20%. **Requires:** 8 more independent baseline-solved tasks (at the observed 1 regressions; 14 minimum).
  * serving INCONCLUSIVE: serving regression 1/14 baseline-solved tasks, exact 95% bounds [0%, 30%]: cannot show <= 20%. **Requires:** 8 more independent baseline-solved tasks (at the observed 1 regressions; 14 minimum).
  * economic INCONCLUSIVE: total tokens per verified success (incl. any failed capability attempt and the fallback) 0.35x the baseline's, 95% one-sided bounds [0.23, 0.48]; savings never count unless safety and serving pass.
* `C:mem_procedural_timeout@1`
  * safety: no violation observed on the exercised constraints (untested: cross-tenant access (single-tenant benchmark); forbidden-resource modification (no protected resources in the simulated environment))
  * intrinsic PASS: asset-alone success 92% over 60 tasks (lower bound 84%); asset-alone regression 1/27 baseline-solved tasks, exact 95% bounds [0%, 16%].
  * serving PASS: paired final-success difference +47% over 60 tasks, 95% one-sided bounds [+35%, +58%]; serving regression 1/27 baseline-solved tasks, exact 95% bounds [0%, 16%].
  * economic PASS: total tokens per verified success (incl. any failed capability attempt and the fallback) 0.70x the baseline's, 95% one-sided bounds [0.53, 0.89].
* `C:pool.f12f4fa920@1`
  * safety: no violation observed on the exercised constraints (untested: cross-tenant access (single-tenant benchmark); forbidden-resource modification (no protected resources in the simulated environment))
  * intrinsic PASS: asset-alone success 97% over 60 tasks (lower bound 90%); asset-alone regression 0/14 baseline-solved tasks, exact 95% bounds [0%, 19%].
  * serving PASS: paired final-success difference +77% over 60 tasks, 95% one-sided bounds [+67%, +85%]; serving regression 0/14 baseline-solved tasks, exact 95% bounds [0%, 19%].
  * economic PASS: total tokens per verified success (incl. any failed capability attempt and the fallback) 0.09x the baseline's, 95% one-sided bounds [0.06, 0.12].
* `C:timeout.e2fb15fa79@1`
  * safety: no violation observed on the exercised constraints (untested: cross-tenant access (single-tenant benchmark); forbidden-resource modification (no protected resources in the simulated environment))
  * intrinsic INCONCLUSIVE: asset-alone regression 2/27 baseline-solved tasks, exact 95% bounds [1%, 22%]: cannot show <= 20%. **Requires:** 3 more independent baseline-solved tasks (at the observed 2 regressions; 14 minimum).
  * serving INCONCLUSIVE: serving regression 2/27 baseline-solved tasks, exact 95% bounds [1%, 22%]: cannot show <= 20%. **Requires:** 3 more independent baseline-solved tasks (at the observed 2 regressions; 14 minimum).
  * economic INCONCLUSIVE: total tokens per verified success (incl. any failed capability attempt and the fallback) 0.25x the baseline's, 95% one-sided bounds [0.18, 0.33]; savings never count unless safety and serving pass.
* `D:mem_episodic_pool@1`
  * safety: no violation observed on the exercised constraints (untested: cross-tenant access (single-tenant benchmark); forbidden-resource modification (no protected resources in the simulated environment))
  * intrinsic FAIL: asset-alone success 63% < 75% over 60 tasks; asset-alone regression 5/13 baseline-solved tasks, exact 95% bounds [17%, 65%]: cannot show <= 20%.
  * serving INCONCLUSIVE: serving regression 5/13 baseline-solved tasks, exact 95% bounds [17%, 65%]: cannot show <= 20%. **Requires:** 37 more independent baseline-solved tasks (at the observed 5 regressions; 14 minimum).
  * economic INCONCLUSIVE: total tokens per verified success (incl. any failed capability attempt and the fallback) 0.48x the baseline's, 95% one-sided bounds [0.30, 0.69]; savings never count unless safety and serving pass.
* `D:mem_episodic_timeout@1`
  * safety: no violation observed on the exercised constraints (untested: cross-tenant access (single-tenant benchmark); forbidden-resource modification (no protected resources in the simulated environment))
  * intrinsic PASS: asset-alone success 90% over 59 tasks (lower bound 82%); asset-alone regression 1/33 baseline-solved tasks, exact 95% bounds [0%, 14%].
  * serving PASS: paired final-success difference +34% over 59 tasks, 95% one-sided bounds [+24%, +46%]; serving regression 1/33 baseline-solved tasks, exact 95% bounds [0%, 14%].
  * economic PASS: total tokens per verified success (incl. any failed capability attempt and the fallback) 0.80x the baseline's, 95% one-sided bounds [0.64, 0.95].
* `D:mem_consolidated_pool@1`
  * safety: no violation observed on the exercised constraints (untested: cross-tenant access (single-tenant benchmark); forbidden-resource modification (no protected resources in the simulated environment))
  * intrinsic INCONCLUSIVE: asset-alone regression 0/13 baseline-solved tasks, exact 95% bounds [0%, 21%]: cannot show <= 20%. **Requires:** 1 more independent baseline-solved tasks (at the observed 0 regressions; 14 minimum).
  * serving INCONCLUSIVE: serving regression 0/13 baseline-solved tasks, exact 95% bounds [0%, 21%]: cannot show <= 20%. **Requires:** 1 more independent baseline-solved tasks (at the observed 0 regressions; 14 minimum).
  * economic INCONCLUSIVE: total tokens per verified success (incl. any failed capability attempt and the fallback) 0.34x the baseline's, 95% one-sided bounds [0.21, 0.47]; savings never count unless safety and serving pass.
* `D:mem_consolidated_timeout@1`
  * safety: no violation observed on the exercised constraints (untested: cross-tenant access (single-tenant benchmark); forbidden-resource modification (no protected resources in the simulated environment))
  * intrinsic FAIL: asset-alone success 51% < 75% over 59 tasks; asset-alone regression 10/33 baseline-solved tasks, exact 95% bounds [17%, 46%]: cannot show <= 20%.
  * serving INCONCLUSIVE: paired final-success difference -5% over 59 tasks, 95% one-sided bounds [-17%, +7%]: an improvement is not demonstrated; serving regression 10/33 baseline-solved tasks, exact 95% bounds [17%, 46%]: cannot show <= 20%. **Requires:** more independent in-scope tasks; 49 more independent baseline-solved tasks (at the observed 10 regressions; 14 minimum).
  * economic INCONCLUSIVE: total tokens per verified success (incl. any failed capability attempt and the fallback) 1.19x the baseline's, 95% one-sided bounds [0.93, 1.53]: cheaper per success is not demonstrated. **Requires:** more independent tasks.
* `D:mem_procedural_pool@1`
  * safety: no violation observed on the exercised constraints (untested: cross-tenant access (single-tenant benchmark); forbidden-resource modification (no protected resources in the simulated environment))
  * intrinsic INCONCLUSIVE: asset-alone regression 2/13 baseline-solved tasks, exact 95% bounds [3%, 41%]: cannot show <= 20%. **Requires:** 17 more independent baseline-solved tasks (at the observed 2 regressions; 14 minimum).
  * serving INCONCLUSIVE: serving regression 2/13 baseline-solved tasks, exact 95% bounds [3%, 41%]: cannot show <= 20%. **Requires:** 17 more independent baseline-solved tasks (at the observed 2 regressions; 14 minimum).
  * economic INCONCLUSIVE: total tokens per verified success (incl. any failed capability attempt and the fallback) 0.37x the baseline's, 95% one-sided bounds [0.23, 0.52]; savings never count unless safety and serving pass.
* `D:mem_procedural_timeout@1`
  * safety: no violation observed on the exercised constraints (untested: cross-tenant access (single-tenant benchmark); forbidden-resource modification (no protected resources in the simulated environment))
  * intrinsic PASS: asset-alone success 90% over 58 tasks (lower bound 81%); asset-alone regression 1/32 baseline-solved tasks, exact 95% bounds [0%, 14%].
  * serving PASS: paired final-success difference +34% over 58 tasks, 95% one-sided bounds [+24%, +45%]; serving regression 1/32 baseline-solved tasks, exact 95% bounds [0%, 14%].
  * economic INCONCLUSIVE: total tokens per verified success (incl. any failed capability attempt and the fallback) 0.83x the baseline's, 95% one-sided bounds [0.64, 1.03]: cheaper per success is not demonstrated. **Requires:** more independent tasks.
* `D:pool.f12f4fa920@1`
  * safety: no violation observed on the exercised constraints (untested: cross-tenant access (single-tenant benchmark); forbidden-resource modification (no protected resources in the simulated environment))
  * intrinsic INCONCLUSIVE: asset-alone regression 1/13 baseline-solved tasks, exact 95% bounds [0%, 32%]: cannot show <= 20%. **Requires:** 9 more independent baseline-solved tasks (at the observed 1 regressions; 14 minimum).
  * serving INCONCLUSIVE: serving regression 1/13 baseline-solved tasks, exact 95% bounds [0%, 32%]: cannot show <= 20%. **Requires:** 9 more independent baseline-solved tasks (at the observed 1 regressions; 14 minimum).
  * economic INCONCLUSIVE: total tokens per verified success (incl. any failed capability attempt and the fallback) 0.10x the baseline's, 95% one-sided bounds [0.06, 0.15]; savings never count unless safety and serving pass.
* `D:timeout.e2fb15fa79@1`
  * safety: no violation observed on the exercised constraints (untested: cross-tenant access (single-tenant benchmark); forbidden-resource modification (no protected resources in the simulated environment))
  * intrinsic PASS: asset-alone success 92% over 59 tasks (lower bound 84%); asset-alone regression 2/33 baseline-solved tasks, exact 95% bounds [1%, 18%].
  * serving PASS: paired final-success difference +37% over 59 tasks, 95% one-sided bounds [+25%, +49%]; serving regression 1/33 baseline-solved tasks, exact 95% bounds [0%, 14%].
  * economic PASS: total tokens per verified success (incl. any failed capability attempt and the fallback) 0.31x the baseline's, 95% one-sided bounds [0.23, 0.39].

## Lifecycle economics (explicit assumptions)

Learning = discovery + analysis tokens of the asset's family; creation costs 0 model tokens (deterministic); evaluation = this experiment's strategy and baseline runs on the in-scope tasks, charged without sharing; R = assumed full re-evaluations over the asset's life (**not measured**). Break-even is in verified successes against the baseline's tokens per verified success; the strategy's tokens include any failed capability attempt and the fallback. Tokens per verified improvement = lifecycle tokens / extra verified successes over one pass of the in-scope tasks.

| Set | Asset | R | learning tokens (discovery+analysis) | evaluation tokens (strategy + baseline, unshared) | lifecycle tokens | break-even (verified successes) | tokens per verified improvement |
|---|---|---|---|---|---|---|---|
| C | `mem_episodic_pool@1` | 0 | 164,970 | 759,342 | 924,312 | 85 | 36,972 |
| C | `mem_episodic_pool@1` | 1 | 164,970 | 759,342 | 1,683,654 | 155 | 67,346 |
| C | `mem_episodic_pool@1` | 3 | 164,970 | 759,342 | 3,202,338 | 295 | 128,094 |
| C | `mem_episodic_timeout@1` | 0 | 109,470 | 875,487 | 984,957 | 213 | 44,771 |
| C | `mem_episodic_timeout@1` | 1 | 109,470 | 875,487 | 1,860,444 | 402 | 84,566 |
| C | `mem_episodic_timeout@1` | 3 | 109,470 | 875,487 | 3,611,418 | 780 | 164,155 |
| C | `mem_consolidated_pool@1` | 0 | 164,970 | 768,637 | 933,607 | 65 | 21,712 |
| C | `mem_consolidated_pool@1` | 1 | 164,970 | 768,637 | 1,702,244 | 119 | 39,587 |
| C | `mem_consolidated_pool@1` | 3 | 164,970 | 768,637 | 3,239,518 | 227 | 75,338 |
| C | `mem_consolidated_timeout@1` | 0 | 109,470 | 835,917 | 945,387 | 376 | 78,782 |
| C | `mem_consolidated_timeout@1` | 1 | 109,470 | 835,917 | 1,781,304 | 709 | 148,442 |
| C | `mem_consolidated_timeout@1` | 3 | 109,470 | 835,917 | 3,453,138 | 1,374 | 287,762 |
| C | `mem_procedural_pool@1` | 0 | 164,970 | 759,629 | 924,599 | 64 | 21,502 |
| C | `mem_procedural_pool@1` | 1 | 164,970 | 759,629 | 1,684,228 | 117 | 39,168 |
| C | `mem_procedural_pool@1` | 3 | 164,970 | 759,629 | 3,203,486 | 222 | 74,500 |
| C | `mem_procedural_timeout@1` | 0 | 109,470 | 951,446 | 1,060,916 | 245 | 37,890 |
| C | `mem_procedural_timeout@1` | 1 | 109,470 | 951,446 | 2,012,362 | 465 | 71,870 |
| C | `mem_procedural_timeout@1` | 3 | 109,470 | 951,446 | 3,915,254 | 905 | 139,831 |
| C | `pool.f12f4fa920@1` | 0 | 164,970 | 434,747 | 599,717 | 30 | 13,037 |
| C | `pool.f12f4fa920@1` | 1 | 164,970 | 434,747 | 1,034,464 | 51 | 22,488 |
| C | `pool.f12f4fa920@1` | 3 | 164,970 | 434,747 | 1,903,958 | 94 | 41,390 |
| C | `timeout.e2fb15fa79@1` | 0 | 109,470 | 589,040 | 698,510 | 64 | 24,947 |
| C | `timeout.e2fb15fa79@1` | 1 | 109,470 | 589,040 | 1,287,550 | 118 | 45,984 |
| C | `timeout.e2fb15fa79@1` | 3 | 109,470 | 589,040 | 2,465,630 | 226 | 88,058 |
| D | `mem_episodic_pool@1` | 0 | 179,229 | 756,505 | 935,734 | 74 | 37,429 |
| D | `mem_episodic_pool@1` | 1 | 179,229 | 756,505 | 1,692,239 | 134 | 67,690 |
| D | `mem_episodic_pool@1` | 3 | 179,229 | 756,505 | 3,205,249 | 253 | 128,210 |
| D | `mem_episodic_timeout@1` | 0 | 114,619 | 837,919 | 952,538 | 419 | 47,627 |
| D | `mem_episodic_timeout@1` | 1 | 114,619 | 837,919 | 1,790,457 | 788 | 89,523 |
| D | `mem_episodic_timeout@1` | 3 | 114,619 | 837,919 | 3,466,295 | 1,525 | 173,315 |
| D | `mem_consolidated_pool@1` | 0 | 179,229 | 779,713 | 958,942 | 59 | 21,794 |
| D | `mem_consolidated_pool@1` | 1 | 179,229 | 779,713 | 1,738,655 | 108 | 39,515 |
| D | `mem_consolidated_pool@1` | 3 | 179,229 | 779,713 | 3,298,081 | 204 | 74,956 |
| D | `mem_consolidated_timeout@1` | 0 | 114,619 | 765,535 | 880,154 | never (not cheaper per verified success: never recovered by execution savings) | n/a |
| D | `mem_consolidated_timeout@1` | 1 | 114,619 | 765,535 | 1,645,689 | never | n/a |
| D | `mem_consolidated_timeout@1` | 3 | 114,619 | 765,535 | 3,176,759 | never | n/a |
| D | `mem_procedural_pool@1` | 0 | 179,229 | 790,688 | 969,917 | 63 | 24,248 |
| D | `mem_procedural_pool@1` | 1 | 179,229 | 790,688 | 1,760,605 | 115 | 44,015 |
| D | `mem_procedural_pool@1` | 3 | 179,229 | 790,688 | 3,341,981 | 218 | 83,550 |
| D | `mem_procedural_timeout@1` | 0 | 114,619 | 846,019 | 960,638 | 489 | 48,032 |
| D | `mem_procedural_timeout@1` | 1 | 114,619 | 846,019 | 1,806,657 | 920 | 90,333 |
| D | `mem_procedural_timeout@1` | 3 | 114,619 | 846,019 | 3,498,695 | 1,782 | 174,935 |
| D | `pool.f12f4fa920@1` | 0 | 179,229 | 461,381 | 640,610 | 29 | 14,236 |
| D | `pool.f12f4fa920@1` | 1 | 179,229 | 461,381 | 1,101,991 | 51 | 24,489 |
| D | `pool.f12f4fa920@1` | 3 | 179,229 | 461,381 | 2,024,753 | 93 | 44,995 |
| D | `timeout.e2fb15fa79@1` | 0 | 114,619 | 554,850 | 669,469 | 86 | 30,430 |
| D | `timeout.e2fb15fa79@1` | 1 | 114,619 | 554,850 | 1,224,319 | 158 | 55,651 |
| D | `timeout.e2fb15fa79@1` | 3 | 114,619 | 554,850 | 2,334,019 | 301 | 106,092 |
