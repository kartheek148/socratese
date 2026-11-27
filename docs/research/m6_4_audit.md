# M6.4 Phase 0 — Audit of recovery, routing and eligibility (read-only)

Status: audit only. **No repository file other than this document and [m6_4_design.md](./m6_4_design.md) was created or modified; no code was written; no M6.1/M6.2/M6.3 behaviour, default or policy was changed; nothing was promoted; M7 was not touched.** Everything below was established by reading the implementation and by *throwaway* analysis scripts that import the repository read-only and live in `/tmp` (described, not committed). These results are context for a design review, not a mandate to make M6.4 succeed.

## 0. Method, provenance of numbers, and limits

* **Code traced**, not documentation: line references are to the tree at commit `3e122a0` (M6.3 complete).
* **Stored M6.3 held-out data** (`docs/evaluations/data/m63-heldout.json.gz`, seeds 9,000,000–9,000,099) was re-analysed *descriptively* (reason distributions, decompositions, a utility-weight sweep, variance). Nothing was tuned on it and nothing here is a new confirmatory result.
* **Fresh "audit-exploration" seeds 7,400,000–7,400,059** (new; disjoint from the M6.2 and M6.3 development, audit and held-out blocks and from every test seed) were used for four instrumented re-simulations: cycle economics and outage consumption (30 seeds per scenario), channel state at the look where DRIFTED is declared (40 seeds), relapse detection versus false alarms for a re-armed monitor (60 seeds). **These seeds are excluded from every future M6.4 development and held-out run.**
* All simulation numbers are from **synthetic** worlds with known ground truth (the M6.2/M6.3 worlds plus a few ad-hoc mixed-cause variants built only for this audit). They show what the *implementation does*, not what a deployment would see.
* Files inspected: `controller/drift.py`, `controller/drift_service.py`, `controller/revalidation.py`, `controller/revalidation_service.py`, `controller/revalidation_runtime.py`, `controller/{scenarios,perturb,simulate,reval_eval,drift_eval,evidence_quality,policy,features,rollout}.py`, `domain/{drift,revalidation,evidence,controller,outcome}.py`, `adapters/db/{revalidation_store,drift_store,tables}.py`, `alembic/versions/{0007,0008}*.py`, `ports/{drift_log,revalidation_log}.py`, `revalidation_commands.py`, `revalidation_report.py`, `drift_commands.py`, `drift_db_commands.py`; the M6.2 and M6.3 designs, reports, ADRs 0017/0018, the stored data files and the M6.2 calibration table; the M6.3 unit, integration and migration tests (for what is, and is not, pinned).

## A. Existing recovery architecture

### A.1 M6.2 native recovery (`controller/drift.py`)

A *cell* is one paired prior (rule default vs one alternative in one family/risk context). The monitor folds executed, verified outcomes through `NORMAL → SUSPECT → DRIFTED → QUARANTINED → RECOVERING → NORMAL`. Facts that matter for M6.4, each traced in code:

| fact | where | consequence |
|---|---|---|
| Detection channels: context mismatch (≥ 5 incompatible observations), staleness (age), success (Newcombe UB), cost, utility; the trigger is the first channel *in priority order* at the highest level | `_PRIORITY` (`drift.py:37-43`), `evaluate` (`:166-246`, trigger chosen at `:244`) | the "reason" is a priority-ordered label, not a diagnosis (C.2) |
| Statistical channels need ≥ `min_samples` = 20 observations; context mismatch and staleness do not | `evaluate` | a structural alarm can complete its persistence before any statistical channel is even evaluated |
| DRIFTED is always followed in the *same* `advance` call by QUARANTINED; the reason lives only on the DRIFTED row's trigger | `advance` | the QUARANTINED row cannot be classified on its own |
| Native recovery: `QUARANTINED → RECOVERING` after `min_quarantine` = 50 default-served decisions **unless** `locked` or the evidence is stale | `drift.py:390-396` | a *timer* (plus a staleness veto) opens recovery |
| Probes: `probe_rate`/2 = 12.5 % of the context's decisions, ≤ 150 per episode, only on the exploration allow-list; they are **production decisions** | `controller/evidence.py:252-269`, `policy.py` | recovery evidence is user traffic |
| `recovery_ok`: Newcombe LB at confidence 0.80 ≥ −0.05, cost/utility within 1.15×, **`bad == 0`** incompatible observations, evidence not stale; three consecutive looks | `drift.py:248-282` | structural change and staleness cannot recover (A.1.1) |
| Failure/lock: a still-degraded window or 150 probes → QUARANTINED, `episode + 1`; `locked` when `episode > max_episodes` = 3 | `:398-415` | permanent until a new policy version |
| `MonitorFleet.restore` (M6.3 addition) resets a cell to NORMAL with empty windows, preserving `seq`, `episode` and counters | `:527-536` | relapse still counts toward `max_episodes`; **thresholds after restoration are the ordinary detection thresholds** |

**A.1.1 Native recovery is impossible by construction for two reasons.** (i) `CellReference.compatible` (`:88`) compares each observation's runtime/verifier label with the prior's; fresh observations under another runtime stay incompatible, `recovery_ok` requires `bad == 0` (`:282`) and ≥ 5 incompatible observations raise a level-2 mismatch alarm inside RECOVERING. (ii) The staleness veto at `:393-394` uses an age that only grows. Held-out confirmation: S8 and S8b native restorations are **0/100 and 0/100** (M6.3 held-out, variant `m62`).

### A.2 M6.3 controlled revalidation (`controller/revalidation.py`)

A *case* is opened per quarantine episode by `RevalidationFleet._open` (`:770-799`): the reason is projected from the DRIFTED trigger (`reason_of` `:51`; `reason_and_severity` `:68`), native recovery is disabled by setting the existing `MonitorState.locked` flag (`:773`), and a `CASE_OPENED` row is recorded with `measurements = {severity}` only (`:794`).

```
QUARANTINED --ready--> REVALIDATION_PENDING --start--> REVALIDATING --criteria met--> RECOVERY_CANDIDATE --confirmed--> RECOVERED
     ^                                                      |  failed cycle (cycle+1)                |  confirmation failed (cycle+1)
     +------------------------------------------------------+----------------------------------------+
any non-terminal --case/cycle TTL--> EXPIRED      REVALIDATING|CANDIDATE|PENDING|QUARANTINED --caps, cycles--> BUDGET_EXHAUSTED
```

* **Eligibility** (`check_eligibility` `:278-328`): reason known, risk class ∈ `allowed_risks`, cycles < `max_cycles`, plan feasible, lifetime token budget, executor tenant, identity, versions known, verification available, pool ≥ `min_pool`; strategy readiness (`strategy_ready` `:331`). **The executor `facts` are consulted only at QUARANTINED (`:486-488`) and PENDING (`:507`) — never inside the trial loop.**
* **Trial fold** (`run_one` `:532-`): lineage verdicts `TENANT_MISMATCH`, `NOT_NEWER`, `DUPLICATE`, `VERSION_MISMATCH` (`:545-558`); only `counted` results (everything except `VERIFICATION_UNAVAILABLE`) enter n; the unverified-fraction rule needs ≥ `min_unverified_trials` = 10 results and > 20 % unavailable (`:619-620`).
* **Strategies**: A fixed exact test (N from exact size/power), B = A behind a readiness gate, C = SPRT truncated at `max_trials` = 320; cost futility (E1) and trial spacing (E2) were added before the held-out run.
* **Cycle failure** (`fail_cycle` `:450-457`): `cycle + 1`; the case becomes `BUDGET_EXHAUSTED(CYCLES_EXHAUSTED)` when `cycle ≥ max_cycles` = 3, otherwise QUARANTINED with **dwell reset to 0** (the cooldown is 50 decisions, ×2 when severity ≥ 0.25).

### A.3 Evidence flow

```
production outcomes --(acted, verified, known, same tenant)--> MonitorFleet --transitions--> DRIFTED/QUARANTINED
        |                                                                                         |
        |  default-served decisions (dwell, default_n/k)                                          v
        +----------------------------------------------------------------------> RevalidationFleet._open (reason, severity, lock)
revalidation pool --TrialSource.run(ref, key, at)--> TrialResult --lineage verdict--> case counters (n, k, llr, tokens) --criterion--> transitions
                                                         (verdict written ONLY by the executor adapter, from the independent verifier)
trials + transitions --append-only--> controller_revalidation_{trials,transitions} --ReplaySource--> exact recomputation (prefix-verified)
```

### A.4 Verification

`TrialVerdict` has five values (`domain/revalidation.py`). `classify_run_outcome` (`revalidation.py:178`) maps `OutcomeStatus`/`VerificationResult`: only `VERIFIED_SUCCESS` with every goal criterion explicitly `passed is True` is a success; UNKNOWN or any `passed is None` → `VERIFICATION_UNAVAILABLE`; PARTIAL → FAILURE. `OrchestratorTrialSource` (`revalidation_runtime.py`) maps `termination ∈ {DEADLINE, MAX_STEPS, MAX_TOKENS}` → `TIMEOUT` and `{INVALID_ACTION, TOOL_NOT_ALLOWED}` → `INVALID_OUTPUT` (observed once in the real-model case); every other non-success is FAILURE. **There is no verdict for infrastructure failure or an interrupted execution**, and the `LLM_ERROR` termination is currently classified as an ordinary capability FAILURE.

### A.5 Budget accounting

| budget | native (M6.2) | controlled revalidation (M6.3) |
|---|---|---|
| unit | live probes (production traffic) | out-of-band trials (tokens, trials) |
| per cycle/episode | `probe_budget` 150 probes; `max_episodes` 3 | `max_trials` 320 (evidence), token cap 1.5 × `max_trials` × reference mean, `max_cycle_hours` 400 |
| per cell lifetime | — | `cell_token_budget` 3M tokens; `max_cycles` 3; `case_ttl_hours` 500 |
| what is *not* accounted | probe tokens are ordinary production tokens (they complete real tasks) | tokens of `VERIFICATION_UNAVAILABLE` trials are charged but not separated from evidence tokens in the stored row (`tokens_cycle` is one number) |

### A.6 Quarantine reason classification

`reason_of` projects `SUCCESS_DEGRADATION → PERFORMANCE_DEGRADATION` (`STATISTICAL_DRIFT` under CUSUM), `COST_DEGRADATION`/`UTILITY_DEGRADATION → COST_DEGRADATION`, `CONTEXT_MISMATCH → STRUCTURAL_INVALIDATION`, `EVIDENCE_STALE → EVIDENCE_STALE`; everything else, or a missing DRIFTED row, → `UNKNOWN` (ineligible). **`UNKNOWN` is unreachable through the monitor path** (the DRIFTED transition is always emitted by the same `advance` call that emits QUARANTINED, and `_open` receives that call's transitions); `VERIFICATION_FAILURE` exists only for externally supplied reasons; `STATISTICAL_DRIFT` requires the CUSUM detector, for which M6.2 found *no feasible calibration point*.

### A.7 Persistence and replay

Migrations 0007 (`controller_drift_transitions`) and 0008 (`controller_revalidation_{transitions,trials}`), all append-only by trigger; downgrades refuse with rows. `RevalidationService.sync` recomputes the fleet from the ordered outcomes and the persisted trials only (`ReplaySource`: persisted trials by key, executor facts from persisted transitions, rounds keyed by `(anchor, round_at)`), verifies the stored log is exactly a prefix of the recomputation, persists what observations imply, and publishes the overlay; `.run` adds one live round. `DriftService` refuses revalidation policies. Replay is tested for all three strategies, with real PostgreSQL, and a changed configuration is detected.

### A.8 Post-recovery behaviour

`RECOVERED` → `restore()` (A.1) and, for structural/stale cases, a rebase of the reference's version labels or evidence time (an event in the log). The restored cell uses the **same detection thresholds as before**, the same reference, an empty window: nothing distinguishes a freshly restored cell from a long-healthy one.

### A.9 CLI, harness, frozen configurations

`ee-ctl reval-sim|reval-report|reval-status|reval-evaluate|reval-replay|reval-real` (plus the M6.2 `drift-*`); scenarios in `controller/scenarios.py` (`REVAL_SCENARIOS`: S2, S4, S5, S8, S8b, R1–R5), executor/variants/scoring in `reval_eval.py`; frozen `DriftParams` (M6.2 §10) and `RevalidationParams` defaults (design §5 with errata E1/E2). Variants: `m61`, `m62`, `m62_locked`, `m63_A/B/C`. Held-out report and generated tables are committed.

## B. M6.3 failure analysis

### B.1 Why net utility was negative

Pooled over the five restoring scenarios (S5, R3, R4, S8, S8b), C minus M6.2 native, per stream: **+4.84 verified successes [+4.01, +5.73]** and **+689,872 tokens** (revalidation +1,137k, production −447k because a restored alternative is cheaper). With the benchmark's weight (a success worth 20,000 tokens) that is 4.84 − 34.5 = **−29.7**. The result is *a price statement, not a correctness failure*: C buys one extra verified success per ≈ **142,000 tokens**. Re-weighting the same data: success valued at 5k tokens → −133.1; 20k → −29.7; 50k → −9.0; 100k → −2.1; **142k → 0.0**; 300k → +2.5. Whether that price is acceptable is a deployment question the benchmark's utility cannot answer (B.9).

### B.2 Why controlled revalidation was economically inefficient where native recovery works

Cycle economics from the instrumented re-simulation (C, audit seeds, share of cycle tokens):

| scenario | cycles/stream | restored cycle | chance-failed healthy cycles | cycles started while still degraded | confirmation share of a restored cycle | evidence trials to criterion |
|---|---|---|---|---|---|---|
| S5 | 1.33 | 58 % | 11 % | **1 %** | 17 % | 97 |
| R3 | 1.37 | 68 % | 13 % | 1 % | 15 % | 113 |
| S8 | 1.33 | 67 % | 17 % | 0 % | 18 % | 90 |
| S8b | 2.33 | 70 % | 16 % | 0 % | 19 % | 88 |
| R5 | 2.30 | 53 % | 26 % | 14 % | 18 % | 91 |

* **The dominant cost is the evidence itself**: 88–113 counted trials × ≈ 6.5k tokens ≈ **0.6–0.73M tokens per restoration**, set by the statistics (a 10-point margin against a ≈ 0.5 success rate; roughly ((z_α+z_β)σ/δ)² trials). Timing is *not* the problem: only 1 % of tokens in S5/R3 are spent in cycles that started while the world was still degraded.
* **Confirmation adds 15–19 %** (20 trials ≈ 130k tokens per restoration) for a guard whose benefit the M6.3 experiment could not isolate (no scenario had a regime flip between evidence and restoration).
* **11–17 % of tokens go to chance-failed cycles** on healthy worlds (the β = 0.20 of the SPRT plus reference sampling error): a cost inherent to the error rate.
* **Native recovery pays in a different currency.** Its ≈ 90–105 probes per stream cost 700–820k *production* tokens, but those complete real tasks (they are not overhead); what it costs is **exposure to the degraded alternative when the degradation is persistent**: relative to permanent quarantine native loses **−16.1 (S2), −14.7 (R2), −10.5 (R5), −7.3 (R1)** verified successes per stream (extra degraded decisions +48, +47, +37, +57) and **0.0 to +1.3** when the disturbance is temporary (S5, R3, R4). Revalidation C costs 0.28M (S2), 0.28M (R2) and 0.73M (R1) tokens and **no** successes on persistent degradation. Neither dominates: the choice depends on whether the degradation is temporary or persistent — which is exactly what is unknown when the quarantine is declared.
* **Horizon.** The benefit of a restored alternative accrues per decision (≈ 0.145 utility ≈ 2,500 tokens of the default's cost) so ≈ 0.7–1.1M tokens needs ≈ 400 restored decisions to repay (token terms). An 800-decision stream restores around position 480–600. Exploratory 2,400-decision runs (development seeds) showed native recovery catching up on temporary disturbances (19–20/20 restored) and revalidation winning only where native cannot recover (S8: +176 utility [+111, +233]).

### B.3 Why structural and stale scenarios benefited

Because native recovery is impossible there by construction (A.1.1): S8/S8b native 0/100 (in the M6.2 audit's 12-stream trace: 0 recovery attempts for S8b; for S8, 35 attempts, 24 failures and 0 recoveries) versus C 90/100. In utility, at 800 decisions C only matches the lock-in (S8: +47.5 vs native +46.4; S8b: −2.6 vs +40.0 because staleness recurs and is re-revalidated, 1.9 cases and 1.65 restorations per stream); at 2,400 decisions the structural case wins decisively. **M6.1's coarse guard, which never quarantines a harmless runtime change, still beats every quarantining variant in S8/S8b (+117.5)**: the M6.2 structural channel itself costs more than recovery can repay when the change is harmless — a *detection-side* cost that routing cannot remove.

### B.4 How verification outages consumed recovery cycles

Mechanism, traced: (1) the pre-round availability flag is consulted only at QUARANTINED/PENDING (A.2) — and the simulated executor's `facts()` always reports `verification_available = True`, so **the pre-check was never exercised; R4 is the hard case where the outage is discovered only through trial verdicts**; (2) a round runs up to 50 trials; the unverified rule needs ≥ 10 results and > 20 % unavailable, so each outage-hit cycle burns ≥ 10 trials ≈ 65k tokens before it can fail; (3) the failure is `fail_cycle(VERIFICATION_UNAVAILABLE)` (`revalidation.py:617-624`), which is **indistinguishable from "criteria not met"**: `cycle + 1`, dwell reset to 0 (100 decisions for a severe case); (4) after 3 such cycles `CYCLES_EXHAUSTED` ends the episode.

Instrumented R4 (outage over decisions 380–600, 30 audit streams): **53 of 81 ended cycles ended `VERIFICATION_UNAVAILABLE`**, 18 `CONFIRMED`, 9 `CYCLES_EXHAUSTED`, 1 `CRITERIA_NOT_MET`; cycle starts cluster exactly at the cooldown cadence (decades 380–400, 490–500, 590–600); 19.7 unavailable trials per stream = **127k tokens (18 % of the case's cycle tokens)** of pure waste; held-out result: 40/100 C streams ended `BUDGET_EXHAUSTED`, restoration 55 % (A/B 59 %) against a 70 % target. **The recovery target was unreachable because the design conflates an infrastructure state with capability evidence.**

### B.5 How relapse caused degraded exposure

In R5 the world degrades again at decision 550, after a restoration at ≈ 440–530. The restored cell is prior-enabled, so the alternative runs on (almost) every decision until the monitor re-detects: **median detection delay 47 decisions [IQR 43–69] after onset** (17 audit streams with a restoration before 550), i.e. the exposure *is* the M6.2 detector's own latency (held-out: +50.6 degraded decisions per stream versus permanent quarantine, 116.9 vs 66.3). The monitor re-arms correctly (second quarantine, `episode` 2), so exposure is bounded, not prevented. **Reusing the M6.2 detector with other thresholds buys little** (re-armed monitor, 60 audit streams per setting, relapse to success 0.35 at decision 550; control = stable world, 400 decisions):

| confidence | persistence | detected | median delay [IQR] | control false alarms |
|---|---|---|---|---|
| 0.995 (frozen) | 10 | 59/60 | 45 [39, 68] | 0/60 |
| 0.995 | 5 | 59/60 | 39 [34, 51] | 0/60 |
| 0.990 | 5 | 60/60 | 38 [32, 44] | 1/60 |
| 0.975 | 5 | 60/60 | 34 [30, 40] | 3/60 |
| 0.975 | 3 | 60/60 | **31** [28, 38] | 4/60 |

`min_samples` = 10 and 20 gave **identical** results in every row: the binding constraint is the success channel's statistical power at the chosen confidence, not the sample minimum. The best point shortens exposure by ≈ 31 % at a 6.7 % false-alarm rate (each false re-quarantine would trigger another recovery case). Exposure below ≈ 30 decisions would need a different *kind* of mechanism (limiting the alternative's traffic share after restoration), which would be a new exploration-like mechanism and is not proposed without a measured need.

### B.6 Why B and A were nearly identical

`strategy_ready` for B (`:331-350`) adds to A's dwell condition "≥ 30 default-served verified outcomes and the default arm not confidently degraded". In every scenario but R3 the default arm is untouched by construction, so the extra condition is true as soon as the dwell is reached; and `default_n` counts the same default-served decisions as `dwell`. Measured: B differs from A on **62/1,000** streams, all in R3 (the only scenario with a common-cause outage), where it spends 11 % fewer tokens (1.58M vs 1.78M) at identical restoration (82/82) and +5.7 utility. The readiness gate does what it was designed for; the held-out *worlds* almost never exercise it.

### B.7 What M6.3 features were not exercised

| feature | status | why |
|---|---|---|
| lineage rejections (`NOT_NEWER`, `TENANT_MISMATCH`, `DUPLICATE`, `VERSION_MISMATCH`) | **0 rejected trials** in 3,000 revalidating stream-results | the simulated executor is lineage-clean; unit and integration tests only |
| reason `UNKNOWN` | unreachable via the monitor | A.6 |
| reason `STATISTICAL_DRIFT` | not reachable with the frozen WINDOW detector | CUSUM had no feasible calibration |
| reason `VERIFICATION_FAILURE` | external only | no producer |
| pre-round `verification_available` gate | never false in simulation | `SimExecutor.facts` constant |
| `allowed_risks`, tenant, identity, pool gates | never failed | one family, one tenant, one risk class, pool of 1,600 |
| `EXPIRED` | occurred **3 times** (so it *was* exercised) | TTL paths otherwise unobserved |
| `LINEAGE_VIOLATION` reclassification | not triggered | no version mismatches |
| confirmation's value | undetermined | no regime flip between evidence and restoration |
| `SHADOW` mode in an experiment | unit/integration only | experiments ran `ENFORCE` |
| multi-cell/multi-family fleets | not simulated | single family per stream |
| provenance and freshness enforcement | absent | B.8 |

### B.8 Missing evidence gates (spec inputs vs implementation)

| input | M6.3 state | gap, with the code that shows it |
|---|---|---|
| evidence source / kind | recorded in `PairedSource` (`evidence_kind`, `production_eligible`) | `CellReference` copies only `runtime_version`, `verifier_version`, `recorded_at` (`drift.py:449-470`): provenance is **dropped before revalidation sees it**; `production_eligible` defaults to `None` |
| tenant / context | trial `tenant_id` checked | `TrialResult.tenant_id` is optional and **`None` skips the check**; the reference's source tenant is never compared |
| capability identity | `facts.supported` (executor's own claim) | the trial's `capability_ref` is stored but never compared with `ref.alt_key` |
| execution identity | none | no executor identity/build in `TrialResult` or `ExecutorFacts` |
| verifier identity | version compared in `_conflict` | **permissive when either side is `None`** (`revalidation.py:367-375`); no verifier *id*, no independence attestation field |
| runtime / model / tool version | runtime and verifier version only | model id and tool-catalogue version are not recorded on trials, so a model upgrade is invisible |
| reference–source link | `reference_of` picks the source that lists the capability | **falls back silently to `sources[0]`** when none matches (`drift.py:451-452`) |
| evidence lineage | uniqueness by `task_ref`, newer than the trigger | `trigger_at is None` skips the NOT_NEWER check; no check that pool tasks are disjoint from the *reference's* tasks (only a hash of the set is stored) |
| evidence age | EVIDENCE_STALE is a *reason* | no gate on the age of the trial evidence at decision time, none on how long a RECOVERED state may be trusted |
| independently verified | by construction (adapter writes the verdict) | not recorded as a checkable attribute |
| enforcement | — | a restoration records no gate report; the database cannot reject a `RECOVERED` row without one |

Several of these are *silently permissive when data is absent* — exactly what the M6.4 brief forbids.

### B.9 Is the utility function aligned with practical value?

`utility = verified successes − tokens/20,000` (`drift_eval.UTILITY_LAMBDA`) was fixed in M6.2 for continuity. Limits, from the data: (i) λ is an arbitrary exchange rate; the sign of the M6.3 result flips at 142k tokens per success, and no evidence in the repository says where a deployment sits; (ii) it charges revalidation tokens at the production price although they run out-of-band; (iii) it ignores **user-facing exposure** (degraded decisions: native +37 to +57 per stream on persistent degradation), latency, human interventions and safety; (iv) it aggregates scenarios with arbitrary equal weights; (v) it rewards the monitor's *least* valuable behaviour on harmless changes (M6.1 +117 in S8). **It is a legitimate reporting metric, a poor eligibility criterion and an insufficient sole evaluation metric**; M6.4 must keep the three roles separate (design §9).

## C. Routing feasibility

### C.1 How the reason is produced

Deterministic given the window and parameters (a threshold crossing), but *inferred*: it names the first alarming channel, not a cause. Three properties follow.

* **Reliable on single-cause scenarios.** M6.3 held-out, primary reason versus known cause: S2/S5/R1/R3/R4/R5 → `PERFORMANCE_DEGRADATION` 100 %; S4 → `COST_DEGRADATION` 97/97; S8 → `STRUCTURAL_INVALIDATION` 100/100; S8b → `EVIDENCE_STALE` 190/190 (the stale case recurs).
* **Overlapping because channels are derived from the same outcomes.** The utility channel (tokens per verified success) rises whenever success falls, so it is not an independent cause: at the DRIFTED look in a pure success collapse (S2) the alarm set was `SUCCESS` alone in 19/40 and `SUCCESS + UTILITY` in 21/40 (S5 23/36, R1 16/40, R3 17/38, R5 21/40).
* **Unstable under mixed causes.** For simultaneous success and cost drift (ad-hoc scenario M1, 40 audit seeds) the **primary label was `SUCCESS` 24, `UTILITY` 15, `COST` 1** — a 38 % flip to a label that M6.3 projects to `COST_DEGRADATION`. A routing rule keyed on the primary reason would send the same physical cause to different routes depending on sampling noise.

### C.2 Masking and incompleteness

* **Structural and stale alarms pre-empt.** Context mismatch fires at ≥ 5 incompatible observations and persistence 10, i.e. around the 14th observation; statistical channels need 20. In **every** structural-plus-cost (M2, 40/40 audit streams), stale-plus-success (M3, 40/40) and structural-plus-degraded (R2: 40/40 audit streams, 100/100 held-out) case the alarm set at the DRIFTED look contained only the structural/stale channel and the primary label was structural/stale: **a co-existing degradation cannot be seen at classification time.** M6.3 handled R2 correctly only because revalidation tests performance (0/100 false restorations) — at 92k tokens per cycle × 3 cycles.
* **Detection can be partial.** A mild degradation (survival 0.70, a 15.6-point drop; M5) was detected in only 13/40 streams, so some quarantine-worthy causes never produce a reason.
* **Incomplete reason space.** There is no reason for infrastructure failure, provenance mismatch, relapse, intermittent failure or an unavailable verifier; `UNKNOWN` is unreachable (A.6). Those situations currently either do not arise or are handled elsewhere (the unverified rule, version mismatch).

### C.3 Is a single reason sufficient?

No, but **a small extension of the existing model is sufficient**: keep `QuarantineReason` as the *primary* (it is deterministic and, for single causes, accurate); add a **set of contributing reasons**, the **per-channel evidence at the DRIFTED look** (which channels were evaluated, their level and the bound that crossed — the M6.2 `measurements` already hold the statistics, but not which channels were evaluated or their levels), and a **classification source** (`MONITOR_CHANNEL` | `REVALIDATION_FINDING` | `EXTERNAL` | `DEFAULT`). **A probability-like "confidence" is not recommended**: the channel statistics already carry their own confidence levels (0.995, 0.90), and a derived classification probability would be an invented number; "evidence strength" (the margin beyond the threshold) is reported, not interpreted. Routing must act on the *set*, not the primary (design §6). No new table is needed: the data fits in the existing JSON columns.

### C.4 Verdict on the routing hypothesis

| claim | audit finding |
|---|---|
| structural → revalidation | **Supported** (native impossible by construction; C 90 % vs 0 %). |
| stale → revalidation | **Supported** by the same argument and by S8b (0 % vs 90 %), at a recurring cost. |
| statistical → native | **Partly supported, with a safety caveat**: native is cheap and catches up on temporary disturbances, but its false restoration is **5/100 in R1** (upper bound 10.2 %) and it loses 7–16 successes on persistent degradation; it must therefore be tested against C, not assumed. |
| cost degradation → economic evaluation | **Plausible, unproven**: C's S4 spend is 0.44M tokens for a certain refusal; a deterministic gate could avoid it, but the inputs exist only partly (design §9). |
| verification unavailable → suspend | **Supported** by B.4 (the failure is an infrastructure state, not evidence). |
| unknown/mixed → conservative | **Necessary**: mixed causes exist and are mislabelled at the margin (C.1/C.2). |

## D. Summary of audit conclusions for the design

1. The case machine, persistence and replay are sound and should be reused unchanged except for the outcome taxonomy and one suspension mechanism.
2. The M6.3 deficit is mostly *economic and semantic*, not statistical: price per success (142k tokens), a conflated outage/failure semantics, a permissive provenance posture, and routing by an unstable primary label.
3. The two cheapest, best-evidenced fixes are the outage semantics (B.4) and fail-closed provenance/freshness gates (B.8); both are deterministic and testable end to end.
4. Routing is a hypothesis with partial support; its value depends on information the system does not have at quarantine time (temporary vs persistent), so the design must keep **routing quality, recovery quality and system utility measurable separately**.
5. Relapse protection should *reuse* the M6.2 detector; the measured frontier suggests at most ≈ 30 % exposure reduction from thresholds, so the question is a pre-registered trade-off, not a new detector.
6. Nothing in this audit shows that M6.4 will improve overall utility, and the design makes no such claim.
