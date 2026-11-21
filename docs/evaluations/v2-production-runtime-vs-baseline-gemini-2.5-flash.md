# Production-mode capability runtime vs baseline on dataset v2 (Gemini 2.5 Flash)

## Setup

- Paired runs: 96 over 48 tasks and seeds [0, 1] (identical task and environment per arm; every run in a fresh session; arm C sees only capabilities in the catalog, no transcripts).
- Arm A: `A_BASELINE`, policy `fixed-general-planning/1`.
- Arm C: `C_CAPABILITIES`, policy `rule-capability/1`.
- Model: `vertex-ai:global` / `google/gemini-2.5-flash`.
- Dataset: `connection-debug-sim` v2.0.0-private-x2.

## Outcomes

| Arm | Runs | Verified | Verified success (95% Wilson CI) | Fail/Partial/Unknown | False claims | Tokens / verified success | Tool calls / verified success | Mean latency ms | Human interventions |
|---|---|---|---|---|---|---|---|---|---|
| A baseline | 96 | 27 | 28% (20%-38%) | 69/0/0 | 67 | 21673 | 15.1 | 63428 | 2 |
| C capability runtime | 96 | 68 | 71% (61%-79%) | 27/0/1 | 24 | 6467 | 6.8 | 44190 | 0 |

Paired: both succeed 22, only C 46, only A 5, neither 23.

Confidence intervals **do not overlap**: the difference is unlikely to be chance.

## By task family

Families with no ACTIVE capability are served by general planning in arm C, so their A-vs-C difference measures run-to-run noise, not the capability.

| Arm | Runs | Verified | Verified success (95% Wilson CI) | Fail/Partial/Unknown | False claims | Tokens / verified success | Tool calls / verified success | Mean latency ms | Human interventions |
|---|---|---|---|---|---|---|---|---|---|
| A pool | 48 | 10 | 21% (12%-34%) | 38/0/0 | 38 | 24658 | 20.3 | 58600 | 0 |
| A timeout | 48 | 17 | 35% (23%-50%) | 31/0/0 | 29 | 19917 | 12.0 | 68257 | 2 |
| C pool | 48 | 45 | 94% (83%-98%) | 3/0/0 | 1 | 2794 | 5.7 | 21096 | 0 |
| C timeout | 48 | 23 | 48% (34%-62%) | 24/0/1 | 23 | 13652 | 8.9 | 67284 | 0 |

## By split and variant

| Arm | Runs | Verified | Verified success (95% Wilson CI) | Fail/Partial/Unknown | False claims | Tokens / verified success | Tool calls / verified success | Mean latency ms | Human interventions |
|---|---|---|---|---|---|---|---|---|---|
| A HIDDEN_TRANSFER / hikari_shared | 24 | 2 | 8% (2%-26%) | 22/0/0 | 22 | 58683 | 50.0 | 57487 | 0 |
| A HIDDEN_TRANSFER / okhttp_budget | 24 | 7 | 29% (15%-49%) | 17/0/0 | 16 | 24619 | 14.4 | 67595 | 1 |
| A VALIDATION / httpx_budget | 12 | 4 | 33% (14%-61%) | 8/0/0 | 8 | 17976 | 12.8 | 63928 | 0 |
| A VALIDATION / psycopg_shared | 12 | 4 | 33% (14%-61%) | 8/0/0 | 8 | 14250 | 12.5 | 57208 | 0 |
| A VALIDATION / requests_budget | 12 | 6 | 50% (25%-75%) | 6/0/0 | 5 | 15724 | 8.7 | 73909 | 1 |
| A VALIDATION / sqlalchemy_shared | 12 | 4 | 33% (14%-61%) | 8/0/0 | 8 | 18054 | 13.2 | 62218 | 0 |
| C HIDDEN_TRANSFER / hikari_shared | 24 | 24 | 100% (86%-100%) | 0/0/0 | 0 | 2182 | 5.2 | 17490 | 0 |
| C HIDDEN_TRANSFER / okhttp_budget | 24 | 8 | 33% (18%-53%) | 16/0/0 | 16 | 16744 | 12.6 | 61205 | 0 |
| C VALIDATION / httpx_budget | 12 | 6 | 50% (25%-75%) | 5/0/1 | 5 | 12407 | 8.5 | 68540 | 0 |
| C VALIDATION / psycopg_shared | 12 | 12 | 100% (76%-100%) | 0/0/0 | 0 | 1775 | 5.0 | 15293 | 0 |
| C VALIDATION / requests_budget | 12 | 9 | 75% (47%-91%) | 3/0/0 | 2 | 11734 | 5.8 | 78184 | 0 |
| C VALIDATION / sqlalchemy_shared | 12 | 9 | 75% (47%-91%) | 3/0/0 | 1 | 5788 | 8.0 | 34112 | 0 |

## Capability usage (arm C)

- Strategies selected: {'DIRECT_CAPABILITY': 48, 'GENERAL_PLANNING': 48}
- Runs that used a capability: 48/96; versions: {'pool.f12f4fa920@1': 48}
- Fell back to general planning: 4
- Capability aborts: 0; escalations: 0

## Notes

- Success is decided only by the independent verifier from final environment state.
- Tokens and tool calls include capability-internal decisions and any fallback work.
- The cost of learning and evaluating the capability is **not** in this table; see the evaluation report for it. Lifecycle cost must be amortised over expected reuse.
- Differences need more tasks/seeds before being treated as improvements.
