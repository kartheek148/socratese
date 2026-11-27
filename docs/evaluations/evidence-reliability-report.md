# M6.2 — Evidence reliability, windowed drift detection and recovery: report

Status: **hypothesis PARTIALLY SUPPORTED; M6.2 is complete as an implementation and measurement milestone, with a negative result on recovery speed and two limits that matter (below).** Nothing was promoted, no default policy changed, M7 was not touched. Frozen design and protocol (committed before any M6.2 data): [evidence-reliability-design.md](../experiments/evidence-reliability-design.md); every number in this report is in [evidence-reliability-tables.md](./evidence-reliability-tables.md) with its reproduction command; calibration: [evidence-reliability-calibration.md](./evidence-reliability-calibration.md); ADR [0017](../decisions/0017-evidence-reliability-and-drift-monitor.md).

**What the mechanism does, in one paragraph.** A prior-backed controller decision now rests on evidence that is checked before use (explicit eligibility gates) and watched after use (a windowed monitor per prior-backed cell). When recent verified outcomes are inconsistent enough with the evidence, the cell walks `NORMAL → SUSPECT → DRIFTED → QUARANTINED`, the prior stops controlling decisions and the rule baseline serves; after a quarantine period bounded probes collect fresh outcomes and trust returns only through a stricter, explicit `RECOVERY_CRITERIA_MET` transition. Every transition is persisted and replayable. It never picks a new capability and never raises exploration beyond a small bounded probe budget.

**Three things to read first.**
1. On the frozen **synthetic** scenarios the monitor detects abrupt success collapse in a median of 47 decisions with no misses in 300 streams, cost-only drift in a median of 78 (3.3% missed), keeps false alarms at 0.0% (stable) and 2.3% (noisy) of streams, and improves verified successes over M6.1 by +19.8 per 800-decision stream [95% CI +15.7, +24.1] after an abrupt collapse. **Recovery is the weak point**: only 40% of temporary-disturbance streams returned to `NORMAL` by the end of the stream (pre-registered target 70%), so criterion (f) is **not met**.
2. **Structural invalidation is expensive when the world did not change.** Runtime-version and staleness rules quarantine the prior even if outcomes are unchanged; in S8/S8b the monitor spent +22–25% more tokens (+1.17M and +1.29M per stream) for 8–9 fewer verified successes than M6.1 (−2%), a utility loss of 67–74 units per stream against M6.1's 152, by design and by arithmetic, and nothing in M6.2 refreshes the evidence automatically.
3. **Part A (final-outcome evidence through the evaluation workflow) is demonstrated with the simulated benchmark and a deterministic test-double model, not with a real model.** The gates are tested exhaustively; a real-model `ee-eval evaluate --serving-evidence` run was not made in this milestone.

## 1. Repository audit

Full table with file references: design §1. Findings that shaped the work:

| question | finding |
|---|---|
| What does `paired-run` record? | An `ExperimentResult`: per run arm (`A` planner, `E` production path with fallback), task, repetition, split, family, verified status, tokens, `capability_used`, `fell_back`, `asset_ref`; config with dataset hashes, model, runtime/verifier/evaluator versions, `mode="stress"` and `STRESS_FIXTURE` catalog for fixtures. Arm `E` is the *final* outcome (matcher → rule controller → executor → planner fallback). It holds no tenant, risk class, evaluation id or eligibility. |
| What does the promotion evaluator record? | Arms A, B and **C = the capability alone** over SOURCE/HELD_OUT/TRANSFER; an `EvaluationReport` artifact plus aggregates in `capability_evaluations.metrics`. No final-outcome arm: the M6.1 gap. |
| Where did priors come from? | Only result files (`--paired-from`): a stress-fixture file and a real evaluation were indistinguishable; no check of tenant, risk class, duplicate use or whether the evidence was ever promoted. M6.1's own prior is therefore **research-only** under M6.2. |
| Existing drift guard | `evidence.py::_drift`: cumulative live Wilson bound vs the prior; no windows, no state, no recovery, no persisted decision; reacts only at snapshot publication. |
| Gaps | G1 no final-outcome evidence in the evaluation workflow; G2 no eligibility; G3 no monitor state or audit; G4 cumulative guard; G5 no recovery; G6 no shadow monitoring; G7 observations lacked version and time provenance. |

## 2. Architecture changes

* **Learning plane only.** The monitor (`controller/drift.py`, pure) and its service (`controller/drift_service.py`) read acted, verified decisions and publish an overlay; the controller reads the overlay **once when it is built** (`build_rollout(..., drift=...)`). Execution never waits for it and never reads live tables per decision.
* **Contracts** (`domain/drift.py`, `domain/evidence.py`): `DriftParams` (part of the immutable policy definition and its hash), `DriftState/Trigger/Effect/Mode`, `DriftTransition`, `DriftOverlay`, `EvidenceAssessment`, `EvaluationRecord`. New optional fields on `ControllerParams`, `EvidenceParams`, `PairedSource`, `DecisionFeatures`, `DecisionOutcomeRecord`, `TaskResult`, `EvaluationReport` are omitted from serialisation when unset: **M6.1 definition and snapshot hashes are unchanged** (pinned before the change).
* **Controller** (`policy.py`, `evidence.py`, `features.py`): in `ENFORCE` mode the windowed monitor replaces the coarse guard; a DRIFTED/QUARANTINED/RECOVERING cell cannot control the alternative, **including through the unpaired live rule** (live data contain pre-drift outcomes); `RECOVERING` cells receive bounded probes (`MODE_PROBE`) with exact logged propensities. Protected risk classes, foreign tenants and non-allow-listed families never read the overlay.
* **Scope respected:** no variant-level learning, no LLM in the loop, no automatic capability generation, no change to rule-baseline behaviour or to `default-low`, `default-low-v2`, `serving-low-v1`, M6 policies or any promotion decision.

## 3. Evidence integration (M6.2-A)

* `ee-eval evaluate --serving-evidence` adds `Arm.SERVING` (`E_SERVING`): the capability **as production serves it** (matcher → executor → planner fallback; an evaluation-only static catalog, never an ACTIVE one) on the HELD_OUT and TRANSFER tasks, **after and independently of the verdict**. It is not in `suites`, never read by `decide()`, and enters the config hash only when set: verdicts, suites, regressions and every earlier hash are unchanged (integration test: identical verdict/reasons/suites with and without the arm). Its tokens are counted as validation resources.
* `ServingEvidence` (stored in `capability_evaluations.metrics["serving_evidence"]`): the 2×2 table of the planner (alternative) against the served capability (default) per task by strict majority over seeds, with UNKNOWN, ties, tasks where the capability was not the one served, duplicate rows, missing-cost pairs and the task-set hash counted.
* `ee-ctl train --kind evidence --paired-from-registry CAP@VER` builds the prior from the stored evaluation. `EvidenceAssessment` is an explicit gate list (one reason per failed gate, no opaque score): kind `PROMOTION_EVALUATION`, tenant, risk class, held-out partitions only, runtime/verifier/environment versions, capability version, sample size, UNKNOWN/tie fraction ≤ 20%, complete token measurements, no duplicate rows or evaluation used twice, evaluation verdict PASS, not a test-double model, freshness (if configured) and drift state. Ineligible evidence refuses to train (exit 2, all reasons printed) unless `--allow-research`, which records `production_eligible=false`; `--allow-version-mismatch` keeps working, is listed in `overrides`, and weakens no other gate (tested). Result files are research-only. The offline gate reports and can require eligibility.
* **What was demonstrated:** a real evaluation run (simulated benchmark, deterministic fake model) records the arm, a stored evaluation yields an eligible prior with full provenance, and a synthetic/incomplete/duplicate/mismatched/other-tenant/other-risk/non-held-out/failed-verdict/stale evidence is refused with its own reason. **Not demonstrated:** a real-model evaluation; the "valid" integration case re-labels the same measurements with a non-test-double provider purely to exercise the gates.

## 4. Drift algorithm selection

Two detectors were implemented behind one state machine and calibrated on **development** seeds only (200 streams per scenario, rule fixed before the run, design §6.1): (W) a confidence-aware sliding window on success (Newcombe bound of recent − reference, reference uncertainty included), cost (delta-method bound on mean tokens), utility (tokens per verified success; also "dearer than the rule default") plus structural channels for runtime/verifier mismatch and evidence age; (C) a Bernoulli CUSUM on success with the same other channels.

* **WINDOW feasible points:** (0.995, 10), (0.999, 3), (0.999, 5) of 12 grid points; frozen: **confidence 0.995, persistence 10**. Aggressive points (0.95, 3) raised false alarms in 20% (stable) and 45% (noisy) of development streams: the noise scenario is what forces conservatism.
* **CUSUM: no feasible point** (best h=10: 2.5% / 8.5% false alarms in stable / noisy streams). By the pre-registered rule WINDOW (simpler) is the deployment candidate.
* **Held-out comparison (monitor-only, 300 streams)**, reported but not used to choose: CUSUM raised false alarms in 5/300 (S1) and 19/300 (S6, 6.3%, above the 5% target) streams against 0/300 and 7/300; it detected abrupt collapse later (64 vs 47 median) and gradual degradation earlier (138 vs 207). There is no clear win for the more sophisticated method; the simpler one is kept.

## 5. State machine

`NORMAL → SUSPECT → DRIFTED → QUARANTINED → RECOVERING → NORMAL` with `SUSPECT → NORMAL` (cleared) and `RECOVERING → QUARANTINED` (re-degradation, exhausted probe budget). `DRIFTED` is recorded and immediately followed by `QUARANTINED` (the response). There is no direct path from DRIFTED or QUARANTINED to NORMAL (property-tested over 30 random alternating streams: every observed transition is one of the seven legal edges). Transitions carry cell, previous/new state, trigger, effect, mode, applied flag, measurements, window size, the full threshold configuration and its hash, detector version, the causing decision and its time, policy version.

## 6. Recovery mechanism

After `min_quarantine` (50) rule-served decisions a cell enters RECOVERING and receives probes (probe mass 0.25, effective probability 0.125 per decision, at most 150 per episode, only on the existing exploration allow-list). Trust returns only when ≥ 30 fresh outcomes have a *lower* confidence bound (80%) above −0.05 (entry required the upper bound at 99.5% below −0.10: one window can never satisfy both, tested for every window composition), acceptable cost and utility, zero incompatible observations and fresh evidence, for 3 consecutive looks. A still-degraded probe window or an exhausted budget returns to QUARANTINED; after 3 episodes the cell stays quarantined until a new policy version. The historical prior is never edited and the recovery transition stores the fresh measurements as the new assessment. The rules of this mechanism are pinned by mutation-driven tests (§10).

## 7. Files changed

40 files, +5,980 / −50 lines since M6.1 (`git diff --stat 351e7a1..HEAD`): new `domain/{drift,evidence}.py`, `controller/{drift,drift_service,drift_eval,evidence_quality,perturb,scenarios}.py`, `evaluation/serving_evidence.py`, `adapters/db/drift_store.py`, `ports/drift_log.py`, `drift_commands.py`, `drift_db_commands.py`, `drift_report.py`, migration `0007`; modified `domain/{controller,evaluation}.py`, `controller/{policy,evidence,features,gates,rollout,simulate,datasets,modes}.py`, `evaluation/engine.py`, `adapters/db/{tables,registry_store,controller_store}.py`, `controller_cli.py`, `eval_cli.py`; tests `test_drift_{monitor,controller,statistics,regression}.py`, `test_evidence_quality.py`, `integration/test_{drift,serving_evidence}_e2e.py`, migration tests; docs and data `docs/evaluations/data/m62-rt_*.json`.

## 8. Database changes

One reversible migration, **0007**: `controller_drift_transitions` (append-only by trigger; `(tenant, policy, version)` FK to `controller_policies`; unique `(…, cell_key, seq)`; CHECKs on states and mode; one index). Downgrade refuses while the table holds rows (history is not rewritten); with no rows it drops the table and can be re-applied (tests). No other table changes; `capability_evaluations.metrics` is JSON and gains an optional `serving_evidence` key. Tenant isolation: every query is tenant-scoped; the monitor ignores another tenant's outcomes (tested).

## 9. Configuration

`DriftParams` (defaults are the frozen values): `detector WINDOW`, `mode SHADOW` (enforcement is opt-in), `window 40`, `min_samples 20`, `suspect_confidence 0.90`, `confidence 0.995`, `success_tolerance 0.10`, `cost_tolerance 0.30`, `utility_tolerance 0.30`, `persistence 10`, `clear_after 10`, `mismatch_min 5`, `max_evidence_age_days None`, `min_quarantine 50`, `recovery_threshold 0.05`, `recovery_confidence 0.80`, `min_recovery_samples 30`, `recovery_persistence 3`, `probe_rate 0.25`, `probe_budget 150`, `max_episodes 3`, `cusum_shift 0.15`, `cusum_threshold 6`. Validation rejects `recovery_threshold ≥ success_tolerance`. `evidence.require_production_eligible` (default off, set automatically for registry priors). CLI: `ee-ctl train --paired-from-registry … --drift-monitor shadow|enforce`, `evidence-inspect`, `drift-evaluate|status|replay|shadow-report`, `drift-calibrate|sim|real|report`, `ee-eval evaluate --serving-evidence`.

## 10. Tests

New: 14 monitor tests of windows, channels, small samples, state machine, persistence, hysteresis, recovery, probe budget, episode cap, UNKNOWN handling, context isolation, determinism and CUSUM (+5 mutation-driven); controller tests of overlay effects, probes, SHADOW non-interference, replay and tamper detection, protected/foreign tenants, rollout gates, in-the-loop S1/S2; statistical tests on independent seeds 5,000,000+ (false-alarm rates, detection bounds, gradual vs abrupt, small samples, structural alarms, CUSUM direction, exact binomial bound); 31 evidence-quality tests (every gate, override audit, stored-evaluation conversion); regression tests (decision digests of M6, M6.1 evidence-driven, bandit and cold policies pinned from the **M6.1 code**, tenant guard, safety of any overlay state, determinism of scenarios); PostgreSQL integration tests for persistence, append-only/tenant scoping, prefix verification, SHADOW non-interference, ENFORCE fallback and exact replay, the `ee-ctl` commands, the serving arm and registry-based priors; migration 0007 tests.

**Mutation checks:** 22 single-line mutants of the new rules (entry and recovery thresholds, persistence, quarantine dwell, UNKNOWN handling, shadow/tenant filters, structural channels, cost channel, sample minimum, episode lock, probe allow-list and budget, shadow and protected overlay reads, the coarse-guard replacement, the quarantine step). 13 were killed by the first tests; 6 survived and 3 patterns did not apply; targeted tests were added for the survivors and the mis-patterned mutants were re-applied: **22/22 killed**. (Four equivalent-looking guards survived the M6.1 mutation run; none of the M6.2 mutants did.)

## 11. Test results

Commands (Postgres and RabbitMQ from `deploy/docker-compose.yml`, project `ee-m62`):

```
uv run ruff check .                                    # All checks passed
uv run ruff format --check .                           # 330 files already formatted
uv run mypy src                                        # Success: no issues found in 173 source files
EE_TEST_DATABASE_URL=postgresql+psycopg://ee:ee_dev_password@127.0.0.1:55437/postgres \
EE_TEST_AMQP_URL='amqp://ee:ee_dev_password@127.0.0.1:57001//' \
  uv run pytest -p no:cacheprovider -q -ra --durations=3
```

Result: **exit code 0; 762 tests collected and run, none failed, errored or skipped** (the previous milestone's suite was 657; M6.2 adds the unit, integration and migration tests of §10). After that run one more test (the `drift-sim → drift-report` round trip, which verifies the sensitivity table of §13) was added: `tests/unit/test_drift_regression.py` 9 passed; the collection count is now 763. The repository sets `addopts = "-q"`, so an extra `-q` suppresses pytest's final summary line; the counts above come from the progress lines (762 dots), the collection count and the exit status, not from a summary line. The slowest tests are pre-existing M6.1 simulations (209 s, 142 s, 132 s on a loaded machine); M6.2 changed neither them nor `task_stats`.

Migration tests (including 0007: upgrade, append-only table, constraints, refusing downgrade with rows) and the integration tests with a real Postgres (drift end-to-end: 5; serving-evidence end-to-end: 5) are part of that run. Pinned and unchanged: the M6 definition hashes (`53b7c21a…`, `fe45fb19…`, `9f559935…`), the M6.1 evidence definition and snapshot hashes (`08b419f4…`, `b99dae48…`), the decision digests of the M6, M6.1 evidence, bandit and cold policies (computed with the M6.1 code before any M6.2 change) and `default-low`'s hash `7b6f0944819b…`. The 22/22 mutation result is in §10 (the mutation runs used a separate worktree and are not part of the suite).

## 12. Experiment protocol (frozen before held-out data)

Synthetic worlds (labelled): two families; default = a stale capability (success 0.50, 9,000 tokens), alternative = the planner (0.52, 6,500 tokens), outcome correlation 0.5, token CV 0.35; per stream a paired prior drawn from the world (240 pairs per family, the first draw whose paired bound resolves so a switch exists: conditioning stated) then 800 decisions; drift is a labelled overlay on what the controller observes when it executes the alternative (the default's outcomes untouched, common random numbers across controllers). Scenarios S1–S10b (design §6). Seeds: development 100000–100199, held-out 7000000–7000299 (controllers in the loop on the first 100), unit-test seeds 5,000,000+; fixed before the run. Variants on identical streams: rule baseline, M6.1 (coarse guard), M6.2 SHADOW, M6.2 ENFORCE. Acceptance criteria (a)–(f) and the decision rule were frozen with the design. **Protocol corrections made before any held-out data** (design errata E1–E5): the S9b onset, the S10 severity, probe-rate arithmetic, a fallback rule for calibration, and the requirement that a feasible detector must also detect.

## 13. Results across scenarios

Held-out, monitor-only (300 streams per scenario) and controllers in the loop (100 per scenario); full tables in the tables file.

| scenario | false-alarm streams | detected | median detection delay (decisions) [IQR] | ENFORCE − M6.1 verified successes / stream [95% CI] |
|---|---|---|---|---|
| S1 stable | 0/300 | – | – | +0 [+0, +1] |
| S2 abrupt success collapse | 0/300 | 300/300 | 47 [41, 70] | **+20 [+16, +24]** |
| S3 gradual degradation | 1/300 | 292/300 | 207 [163, 248] | +27 [+22, +32] |
| S4 cost-only drift | 0/300 | 290/300 | 78 [47, 152] | −7 [−9, −5] (tokens −287k [−310k, −263k]) |
| S5 temporary disturbance | 1/300 | 286/300 | 45 [40, 65] | +18 [+15, +20] (tokens +461k) |
| S6 noisy | 7/300 | – | – | +2 [+1, +3] |
| S7 context-specific | 0/300 (`pool`) | 298/300 (`timeout`) | 96 [79, 137] | +8 [+6, +10] |
| S8 runtime change | 0/300 | 300/300 | 13 | **−8 [−11, −6]** (tokens +1.17M) |
| S8b stale evidence | 0/300 | 300/300 | 10 | **−9 [−12, −7]** (tokens +1.29M) |
| S9a/S9b insufficient evidence | 0/600 | 0/300 (post-drift < min_samples) | – | +0 / −0 |
| S10 misleading early observations | 0/300 | – | – | +3 [+2, +5] |
| S10b severe early transient | 62/300 (20.7%) | – | – | +8 [+6, +11] |

Pre-registered acceptance (frozen text; full wording in the tables file): **(a) met** (false alarms S1 0/300, S6 7/300 = 2.3%, exact one-sided 95% upper bound 4.3%); **(b) met** (S2 median 47, 0/300 missed; S3 8/300 = 2.7% missed; S4 10/300 = 3.3% missed); **(c) met** (`pool` 0/300 false alarms; `timeout` 2/300 missed; the family sees half the traffic, so the delay in decisions doubles); **(d) met** (S9 0/600 DRIFTED; S10 0/300); **(e) met** (S2 +19.8 [+15.7, +24.1] successes per stream; S1 +0.06% [0.00%, +0.15%]; SHADOW identical to M6.1 in every stream of every scenario); **(f) NOT met** (S5 recovered 40/100; quarantine episodes per stream S1 0.00, S6 0.03).

**Dependence of the verdict on the measuring run (disclosed deviation).** The frozen text did not say which run measures (a)–(d); I chose the monitor-only run (alternative executed at every position) at evaluation time, after the in-loop results were visible, because a SHADOW controller keeps M6.1's coarse guard, which can pull it off the alternative before the monitor confirms. Evaluated on the in-loop runs instead (100 streams each; `drift-report`, "Sensitivity" table): ENFORCE meets (a), (b), (c); **SHADOW meets (a) but not (b) or (c)** (S2 6/100 missed, S7 `timeout` 10/100 missed), for exactly that reason. The choice favours the monitor and is not neutral; the (b)/(c) results should be read as properties of the detector given observations, not of the SHADOW deployment, which by design observes only what M6.1 executes. This does not change the overall verdict (already PARTIALLY SUPPORTED because of (f)).

Pooled over all scenarios except S10b (3,602 monitor-only streams): recall 98.4%, stream-level precision 99.6%, 9 streams with a false alarm, 0.004 false-alarm events per 1,000 decisions.

## 14. False alarm rate

0/300 (stable), 7/300 (noisy; 2.3%; upper bound 4.3%), 1/300 (gradual and temporary scenarios), 0/300 (S10 early misleading outcomes at moderate severity), 0/600 (small samples). **Two honest qualifiers.** (i) The 25-observation *severe* transient (S10b, informational) triggers a DRIFTED in 20.7% of streams: a 40-observation window still contains the transient when the alarm confirms. (ii) On the **real-model table** the first replay reported 20/20 false alarms; the cause was my harness stamping the simulator's version labels on observations compared with a prior recorded under other versions, so the *context-mismatch channel was right* and the harness was wrong. After the fix: 0/20 false alarms in stable real-outcome streams (exact 95% upper bound 13.9%: 20 streams cannot establish a rate below that), and the synthetic results were verified unchanged by re-running 60 streams byte-for-byte.

## 15. Detection delay

Abrupt collapse 47 decisions (IQR 41–70); cost-only 78 (47–152); gradual 207 (163–248); runtime change 13 and staleness 10 (structural channels need no outcome sample); context-specific 96 (the family receives half the traffic). The gradual figure is inherent in a confidence-aware window on a ramp (the effect is half-built by then); the persistence-of-10 rule adds a fixed nine looks and the calibrated confidence adds more. A more aggressive configuration is faster and, on development data, raises false alarms 8–45×.

## 16. Recovery delay

S5 (the disturbance ends at position 400, the alarm confirms at about 295): **recovered in 40/100 streams (40.0%; upper bound 48.7%), median recovery 284 decisions after the disturbance ended [IQR 244–314]**, 411 fallback decisions and 90 probes per stream. In persistent-drift scenarios (S2–S4, S8) recovery correctly never happens (0/100). On the real table S5 recovered 6/20. The delay is arithmetic: `min_quarantine` 50 + 30 fresh probes at an effective 12.5% probe rate (~240 decisions) + 3 persistence looks, with probes also spent on a cell that may still be degraded. **Exploratory (development seeds only, not a change to the frozen configuration):** probe_rate 0.5 raised S5 recovery from 38/100 to 63/100 and cut the median delay to 185, at +62% probe tokens, and in a persistent collapse (S2) reduced the success advantage over M6.1 from +19 to +9. Recovery speed is a trade between recovery delay and probe cost; the frozen setting leaned conservative.

## 17. Economic impact

Per 800-decision stream, utility = verified successes − tokens/20,000 (raw columns in the tables). Cost of monitoring: **0 model tokens**; about 50–170 µs CPU per observation. Selected rows (ENFORCE vs M6.1, net utility, 95% CI): S2 **+12.6** [+10.2, +15.4], S3 **+16.3** [+13.4, +18.8], S4 +7.7 [+5.6, +9.8], S7 +6.0 [+4.1, +8.1]; S1/S6/S10 positive because the monitor does not suffer the coarse guard's occasional false fallbacks; **S5 −5.3 [−11.4, +0.6]** (the recovery delay is the cost: +17.5 utility units lost on non-material positions while the cell stayed quarantined after the disturbance ended); **S8 −66.8 [−70.2, −62.9], S8b −73.8 [−77.4, −69.9]** (structural quarantine with unchanged outcomes). Cost of recovery: 3–97 probes and 25k–841k probe tokens per stream (the low end are the noisy and early-transient scenarios). Residual loss while the evidence was wrong, M6.1 → M6.2: S2 33.6 → 21.6, S3 33.1 → 17.6, S4 10.2 → 3.1, S7 20.3 → 15.1 utility units. Against the **rule baseline** the prior-backed controller still loses where drift is persistent (S2 −31 successes, S3 −44), because detection and probing are not free: the monitor limits the loss, it does not turn it into a gain.

## 18. Limitations

* **Synthetic drift, one world family.** Calibration and held-out streams come from the same synthetic generator (different seeds, different drift schedules, frozen thresholds); detection and false-alarm figures describe those scenarios, not production drift. The real-model track uses real outcomes with *synthetic overlays* and 238 recorded tasks resampled; it cannot establish real-world drift robustness.
* **No live deployment.** Shadow monitoring is tested against PostgreSQL with the fake model; M6.2 policies were not run in a live shadow or canary on the real model, and nothing was promoted.
* **Part A not run with a real model** (see §3); M6.1's prior is research-only under the new rules, so its AC-S result stands as research evidence.
* **Latency is not monitored** (not modelled by the simulated benchmark; informational only).
* **Recovery is slow and probe-driven**; structural invalidation (runtime/age) has no automatic revalidation: the remedy is a new evaluation and a new policy version, a manual step.
* **Effective probe probability is half of `probe_rate`** (the exploration draw includes the greedy action). The design's recovery arithmetic (errata E3) assumed the nominal rate; this was found after the held-out run and is reported, not tuned away.
* Per-scenario one-sided intervals without multiplicity correction; sequential looks inside one stream are handled by the persistence rule and calibration, not by an alpha-spending proof.
* The ground-truth "material" label is a function of the overlay and the frozen tolerances; precision/recall are therefore properties of the scenarios.
* The decision text of the frozen protocol was ambiguous when only criterion (f) failed (clause 1 vs clause 2); the conservative reading was adopted and disclosed.

## 19. Was the hypothesis supported?

**Partially.** *Supported* on the pre-registered criteria (a)–(e): the windowed monitor detects the drift it was designed for, scopes it to the right context, stays quiet on small samples and noise, never alters decisions in SHADOW, and improves outcomes over M6.1's coarse guard after a collapse. *Not supported* on (f): recovery after a temporary disturbance is too slow (40% vs a 70% target) at the frozen probe budget. Reported honestly: the monitor reduces, but does not remove, the loss from stale evidence; structural invalidation can cost more than it saves when outcomes did not actually change; and the evidence for real-world robustness is absent.

## 20. Is M6.2 complete?

As an implementation and measurement milestone, **yes**: final-outcome evidence recorded through the evaluation workflow with explicit eligibility and audited overrides; a windowed monitor with an explicit, persisted, replayable state machine and hysteresis; safe fallback and bounded explicit recovery; shadow-first rollout that cannot alter execution or bypass the M6 registry gates; a fresh-seed evaluation with false-alarm, detection-delay, recovery-delay and economic metrics; migration with downgrade tests; M6/M6.1 behaviour pinned unchanged. As a *deployment* milestone, **no**: the recovery criterion failed, nothing ran live, Part A was not exercised on a real model, and enforcement should only ship as a new policy version through offline → shadow → canary with fresh evidence.

## 21. Recommended next milestone

**M6.3 — Recovery economics and revalidation**, before M7: (1) an adaptive probe schedule (probe more while the recovery window is informative, less while degraded) evaluated on fresh seeds against the frozen setting; (2) automatic *revalidation* of structurally invalidated evidence (a new serving-evidence evaluation triggered by a runtime or age quarantine) so that S8-type quarantines resolve in hours, not never; (3) one real-model `ee-eval evaluate --serving-evidence` run to close Part A, and a live shadow run of an M6.2 policy to measure false fallback recommendations on real traffic; (4) a canary of an ENFORCE version with concurrent control and the existing non-inferiority gate.
