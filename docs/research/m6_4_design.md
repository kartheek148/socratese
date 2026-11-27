# M6.4 — Reason-aware recovery routing and economic eligibility: design proposal

Status: **proposal for review. Nothing here is implemented, frozen or approved.** No code, migration, default or policy was changed to produce it; M6.1/M6.2/M6.3 behaviour is untouched; nothing was promoted; M7 was not started. Evidence base: [m6_4_audit.md](./m6_4_audit.md) (read it first; "A.x/B.x/C.x" below refer to its sections). **This design does not claim that M6.4 will improve utility.** M6.3's held-out result (net utility −29.7 versus M6.2) is context, not a mandate: a negative or inconclusive M6.4 outcome is an acceptable and fully reportable result, and §18–19 say when to stop.

Items marked **[H]** are hypotheses to be tested, **[D]** are decisions requiring user review (collected in §20), **[P]** are proposed parameter values that would be frozen *before* any development data and may be revised only through pre-registered errata.

## 1. Audit findings (summary)

1. The M6.2 monitor, M6.3 case machine, append-only persistence and exact replay are sound and reusable (A.1–A.7).
2. Native recovery cannot recover structural or stale quarantines **by construction** (A.1.1); it recovers temporary disturbances slowly and cheaply in tokens, but with a false-restoration rate of 5/100 in the boundary scenario R1 and a loss of 7–16 verified successes per stream on persistent degradation (B.2).
3. M6.3 recovers far more, faster and without live probes, but costs ≈ 0.6–0.73M tokens per restoration (evidence) plus 15–19 % confirmation plus 11–17 % chance-failed cycles; one extra verified success costs ≈ **142k tokens** (B.1–B.2).
4. A verification outage is treated as a failed cycle: 53/81 cycles in R4 ended `VERIFICATION_UNAVAILABLE`, 9 of 30 audit streams exhausted their cycles during one outage, 127k tokens/stream wasted (B.4).
5. Relapse exposure equals the M6.2 detector's latency (median 47 decisions); thresholds can shorten it by at most ≈ 31 % at a 6.7 % false-alarm cost; `min_samples` is irrelevant (B.5).
6. Provenance and freshness are recorded but dropped or permissively defaulted (B.8).
7. The reason is a priority-ordered *label*: reliable for single causes (100 % in M6.3), overlapping (derived utility channel), unstable under mixed causes (38 % flip in M1) and masked for structural/stale pre-emption (C.1–C.2).
8. The benchmark utility is a reporting metric, not an eligibility criterion; its sign flips at 142k tokens per success (B.9).

## 2. M6.3 failure analysis (summary)

| question | answer in one line |
|---|---|
| why negative utility | +4.84 successes for +690k net tokens ≈ 142k tokens per success versus a 20k valuation (B.1) |
| why inefficient where native works | statistics fix ≈ 90–113 evidence trials; native pays in exposure, not tokens; the two currencies are not comparable without a declared exchange rate (B.2) |
| why structural/stale benefited | native is impossible there; at 800 decisions C only matches the lock-in, at 2,400 it wins by +176 (B.3) |
| why outages consumed cycles | availability is checked only before a cycle; an outage is discovered through ≥ 10 paid trials and then counted as a failed cycle (B.4) |
| why relapse hurt | the restored cell has no probation; exposure = detection latency (B.5) |
| why B ≈ A | the default arm is untouched in every scenario but R3 (B.6) |
| what was not exercised | lineage rejections, UNKNOWN/CUSUM/VERIFICATION_FAILURE reasons, the pre-check, most gates (B.7) |
| what gates were missing | provenance, execution/verifier identity, model/tool version, freshness, enforcement (B.8) |
| utility alignment | arbitrary λ; ignores exposure; fine for reporting, wrong for eligibility (B.9) |

## 3. Research objective

Determine, by a frozen, reproducible, paired experiment, whether a **deterministic, versioned, auditable recovery policy** that (i) normalises the quarantine reason into a set with evidence, (ii) routes each quarantine to the mechanism whose cost structure fits it, (iii) enforces fail-closed provenance and freshness gates, (iv) separates verification/infrastructure outages from capability evidence, and (v) applies a transparent economic *necessary-condition* gate, achieves a better trade-off between verified successes, tokens, safety and user exposure than M6.2 native recovery and than M6.3 universal revalidation — **or establish that it does not**, and which component is responsible.

Non-goals: no learned or LLM router, no new exploration mechanism, no Bayesian machinery, no new detector unless a measured limitation requires it, no change to M6.1/M6.2 behaviour or defaults, no production promotion, no M7.

## 4. Research questions and hypotheses

| id | question | hypothesis [H] (falsifiable; thresholds frozen in §13) |
|---|---|---|
| RQ1 | Are quarantine reasons reliable enough to route on? | The normalised reason *set* is stable across seeds (≥ 95 % identical on single-cause scenarios, ≥ 90 % on mixed-cause), whereas the primary label is not (the audit's 38 % flip in M1). |
| RQ2 | Does routing beat universal replacement? | The full policy is not worse than M6.3-C on verified successes (margin −1 per 800 decisions) and uses fewer total tokens. |
| RQ3 | Does the outage protocol remove outage-induced cycle consumption without raising false restoration? | Outage-consumed cycles = 0 by construction; restoration under outages ≥ M6.3-C; tokens wasted on unavailable trials reduced. |
| RQ4 | Do provenance/freshness gates block ineligible evidence without blocking eligible evidence? | Gate leaks = 0 (exact) in the gate scenarios; false-block rate on eligible streams ≤ 2 %. |
| RQ5 | Does the economic gate avoid unprofitable revalidations without forgoing profitable ones? | Regret versus an oracle that knows the outcome ≤ 10 % of cases in each direction at each declared horizon. |
| RQ6 | Does a post-recovery probation profile reduce relapse exposure at acceptable false re-quarantine? | Median exposure reduction ≥ 20 % at ≤ 2 % false re-quarantine per 400 decisions; otherwise probation is rejected as a component. |
| RQ7 | Is boundary safety preserved when statistical reasons go to native recovery? | False restoration in R1 stays ≤ M6.2's observed 5 % *and* post-restoration monitoring bounds the consequence; otherwise statistical reasons stay on C. |
| RQ8 | Which effect is routing, which recovery, which system utility? | Separable by construction (§12.3). |

## 5. Proposed architecture

Reuse everything; add the smallest set of pure, replayable components behind the existing ports.

```
M6.2 MonitorFleet ──QUARANTINE_APPLIED──▶ ReasonNormaliser ──ReasonSet──▶ RecoveryRouter(policy v1) ──RouteDecision──▶ journal
                                                                              │
              ┌───────────────────────────────┬────────────────────────────────┴───────────────┐
              ▼ A NATIVE                      ▼ B REVALIDATE                                   ▼ E HOLD
   M6.2 recovery, unchanged        EvidenceGates ─▶ EconomicGate ─▶ M6.3 case machine        locked, no recovery,
   (no lock set)                   + outage protocol (SUSPEND/RESUME) + trial taxonomy        journal only
                                              │ RECOVERED
                                              ▼
                                   Probation (existing monitor, probation profile) ─▶ relapse = new case (episode+1)
```

| component | new / reuse | notes |
|---|---|---|
| `ReasonNormaliser` (pure) | new, small | derives the set, evidence and source from the M6.2 DRIFTED look; no new detector, no schema change beyond a JSON field |
| `RecoveryRouter` (pure, `routing-policy/1`) | new | ordered rule table (§6); versioned and hashed into the policy definition; no learning |
| `EvidenceGates` (pure) | new | fail-closed provenance/freshness gates producing a persisted `GateReport` (§8) |
| `EconomicGate` (pure) | new | token-denominated necessary condition (§9) |
| Case machine, fleet, replay, executor port, SQL store | **reuse** | M6.3 unchanged except: outcome taxonomy (§7), suspend/resume (self-transitions), gate/route journal triggers |
| `OrchestratorTrialSource` | **reuse + extend** | map infrastructure failures and interruptions; record executor/model/tool/verifier identity |
| Post-recovery monitor | **reuse** `MonitorFleet` | per-cell parameter *profile* for a probation window (additive hook, §10) |
| Journal | **reuse** `controller_revalidation_transitions` | new triggers; **one** migration 0009 (§5.1) |

### 5.1 Persistence (minimal)

One migration, `0009`, widening only: the `trigger` CHECK (new triggers `ROUTED`, `GATE_FAILED`, `ECON_NO_GO`, `SUSPENDED`, `RESUMED`, `EVIDENCE_EXPIRED`, `SUSPENSION_TIMEOUT`, `PROBATION_STARTED`, `PROBATION_PASSED`), the trial `verdict` CHECK (§7), and **two JSONB columns** on the transitions table — `routing` (route, rule id, policy version, inputs hash, reason set) and `gates` (the `GateReport`) — plus one database CHECK that a `RECOVERED` row carries a non-empty, all-passed `gates` report (so enforcement is *database-checked*, not only unit-tested). No new table; downgrade refuses with rows, as in 0007/0008. `ControllerParams.recovery` (the new parameter block) is omitted when unset, so every earlier hash is unchanged.

### 5.2 What is explicitly *not* added

New agents, new exploration, learned routers, Bayesian models, multiple interacting learned policies, a traffic-share ramp after restoration, changes to M6.1/M6.2 decisions, promotion.

## 6. Routing matrix

**Inputs**: `ReasonSet` (primary, contributing, per-channel evidence, source), risk class, availability facts, gate report, economic report. **Output**: `RouteDecision{route, rule, policy_version, inputs_hash, reasons}` persisted in the journal. Rules are evaluated in order; the first match wins; every route is deterministic and replayable.

Route glossary: **A** native recovery (M6.2, unchanged); **B** controlled revalidation (M6.3 case machine); **E** conservative hold (stay quarantined, no recovery attempt, journaled). **C (economic evaluation)** is a *guard evaluated inside B's entry*, not a destination (it either lets B proceed or sends the case to E); **D (suspend)** is a *modifier* of B (a flag on the case), not a route — adding either as a state would add states without adding distinctions.

| rule | condition on the reason set and context | route | rationale | evidence status |
|---|---|---|---|---|
| RT-0 | risk class not in `allowed_risks`, or protected | E | hard safety constraints stay outside learning | M6.3 gate; unchanged |
| RT-1 | set contains `UNKNOWN`, source missing/inconsistent, or reason unsupported | E | conservative on the unknown | `UNKNOWN` is unreachable today (A.6): the rule guards future sources |
| RT-2 | reference provenance gate fails | E (`GATE_FAILED`) | ineligible evidence must not be revived | design requirement (§8) |
| RT-3 | set ∩ {`STRUCTURAL_INVALIDATION`, `EVIDENCE_STALE`} ≠ ∅ (**regardless of contributing performance reasons**) | B | native is impossible by construction; the case tests performance anyway (R2: 0/100 false restorations) | **supported** (C.4); cost of a doomed mixed case ≈ 92k tokens/cycle |
| RT-4 | set = {`COST_DEGRADATION`} (cost-only) | B only if the economic gate says *go*, else E | the only information needed is cost, which is cheap to learn but pointless to learn if the benefit cannot pay | **hypothesis [H]** (S4: C spends 0.44M tokens for a certain refusal) |
| RT-5 | primary ∈ {`PERFORMANCE_DEGRADATION`, `STATISTICAL_DRIFT`} and set ⊆ {PERFORMANCE, STATISTICAL, COST}, where COST appears only as an independent contributor | A | native monitoring/recovery already exists, is cheap in tokens and catches up on temporary disturbances | **partly supported** (B.2); carries RQ7's safety risk |
| RT-6 | anything else (unsupported combination) | E | conservative | — |

Modifier **D** (suspend): while the verifier or infrastructure is unavailable (§7) a B case is *suspended*, never failed; an A case needs nothing (production outcomes with unverified status are already `UNKNOWN` and ignored by the monitor, and do not advance the dwell).

**Escalation (optional, off in the primary treatment) [D]:** when native recovery exhausts `max_episodes` (the monitor locks), re-route once through the economic gate to B. It is an ablation (T5, §14), not part of the pre-registered primary comparison.

### 6.1 Analysis of overlap, misclassification and fallback

| situation | what happens under the rules | residual risk |
|---|---|---|
| success + cost drift (M1) | normaliser emits {PERFORMANCE, COST} (stable) → RT-5 → A | the audit's 38 % primary-label flip no longer changes the route; the set's stability is the RQ1 measurement |
| success collapse also raising utility | utility is *derived* from success: not a contributing reason (§6.2) | the derivation rule must be validated on development seeds |
| structural + degradation (R2, M2) | RT-3 → B; the degradation is unobservable at classification time (C.2) | a doomed case spends ≈ 92k tokens/cycle; the economic gate may stop it earlier only if the reference benefit cannot pay |
| stale + success (M3) | RT-3 → B | same |
| cost-only | RT-4 | gate inputs partly unavailable (§9): fail closed to E |
| mild degradation never detected | no quarantine, no route | out of scope (a detection-side limit) |
| misrouted *to native* a persistent degradation | −7 to −16 successes per stream (B.2) | the principal cost of RT-5; measured by RQ7 and by the oracle router |
| misrouted *to B* a temporary disturbance | tokens ≈ 0.7–1.1M | the principal cost of RT-3/RT-4; measured by the economic metrics |
| gate/economic failure after routing | E with an observable `GATE_FAILED`/`ECON_NO_GO` row | foregone benefit measured as "recovery opportunity lost" |

### 6.2 Reason model (recommendation: extend, do not replace)

Keep `QuarantineReason` as the **primary** (C.3). Persist, as JSON in the journal row: `contributing` (set), `evidence` (per evaluated channel: level, statistic, threshold), `not_evaluated` (channels without enough samples — this is what makes "performance unobserved" explicit for structural/stale pre-emption), `source` ∈ {`MONITOR_CHANNEL`, `REVALIDATION_FINDING`, `EXTERNAL`, `DEFAULT`}. Normalisation (deterministic, version `reason-norm/1`): the `UTILITY` channel is contributing only when it alarms at level 2 while `SUCCESS` is below level 2 (otherwise it is *derived* from success and dropped); `COST` contributes when the direct cost channel is at level 2; structural/stale always contribute; a reclassification found during revalidation (e.g. `LINEAGE_VIOLATION` → structural) records `REVALIDATION_FINDING`. **No probability-like confidence is stored** (C.3). If the RQ1 stability criterion fails on development seeds, routing is *not* implemented and M6.4 stops at the audit/outage/gate components (§18).

## 7. Verification-outage protocol

### 7.1 Outcome taxonomy

Seven outcome classes (the existing five plus two). **Attempt** = any execution started; **evidence** = a counted result that enters the success statistics.

| outcome | attempt budget | token budget | counts as evidence (n) | affects success/failure statistics | consumes a cycle | feeds the availability breaker |
|---|---|---|---|---|---|---|
| `SUCCESS` (verified) | yes | yes | yes | success | — | no |
| `FAILURE` (verified) | yes | yes | yes | failure | — | no |
| `TIMEOUT`, capability-attributable (`MAX_STEPS`, `MAX_TOKENS`) | yes | yes | yes | failure | — | no |
| `TIMEOUT`, wall-clock `DEADLINE` | yes | yes | **only if** the infrastructure probe was healthy over the trial window, else reclassified `INFRASTRUCTURE_FAILURE` | failure / none | — | if reclassified |
| `INVALID_OUTPUT` | yes | yes | yes | failure | — | no |
| `VERIFICATION_UNAVAILABLE` | yes | yes (charged, ledgered as *wasted*) | **no** | **none** | **no** | **yes** |
| `INFRASTRUCTURE_FAILURE` (provider/executor error, e.g. `LLM_ERROR`, crash after start) | yes | measured (partial) | **no** | **none** | **no** | **yes** |
| `NOT_COMPLETED` (interrupted, cancelled, lease expired, duplicate suppressed) | yes (reserved) | measured if any, else 0 with `tokens_unknown` | **no** | **none** | **no** | no (but counted against a retry limit) |

A verifier outage is therefore never a capability failure, and the resources it burns are always accounted: the ledger keeps `tokens_evidence`, `tokens_confirmation`, `tokens_wasted_unverified`, `tokens_infra`, `tokens_unknown` separately (today one `tokens_cycle` number mixes them).

### 7.2 When to suspend, how to resume

* **Pre-round probe** (no tokens): the executor reports `verification_available` and a new `infrastructure_available` (a golden-snapshot verifier self-test and a provider ping) before every round; failing either suspends the case.
* **In-round circuit breaker** [P]: `k = 3` consecutive non-evidence attempts (`VERIFICATION_UNAVAILABLE` or `INFRASTRUCTURE_FAILURE`) abort the round, **persist a `SUSPENDED` self-transition** and stop spending. The existing "> 20 % unavailable over ≥ 10 results" rule is kept as a backstop but now *suspends* instead of failing the cycle.
* **Resume** [P]: `m = 2` consecutive healthy probes at least one `round_hours` apart (hysteresis against flapping), then a `RESUMED` self-transition. Accumulated evidence is kept if its age ≤ `evidence_window_hours` [P: 120 h]; otherwise an `EVIDENCE_EXPIRED` self-transition resets the evidence counters **without** incrementing the cycle.
* **Persistent outage**: case and cycle clocks do not run while suspended, up to `max_suspend_hours` [P: 500 h]; beyond it a `SUSPENSION_TIMEOUT` moves the case to `EXPIRED` (prior stays disabled; not `BUDGET_EXHAUSTED`: no budget was consumed). Nothing is spent while suspended except probe overhead (≈ 0 tokens).
* **Preserving state**: the quarantined cell stays prior-disabled throughout; no state is lost; restoration remains possible later.

### 7.3 Duplicates, interruptions, determinism

* Every attempt gets a **new ordinal** (the existing unique key `(cell, episode, cycle, stage, ordinal)`); evidence index is derived at fold time, so **no schema change** is needed for retries.
* A task is *consumed* only by a **counted** result: the existing `DUPLICATE` rule applies to counted results; a task whose attempt was unavailable/infra/not-completed may be attempted again (the outcome of that attempt is independent of the capability's behaviour on the task, so this introduces no selection bias).
* The executor must be idempotent per attempt key: a replayed attempt returns the persisted result and never re-executes.
* **Replay**: every availability observation (probe results with their times) is persisted in the `facts` of `SUSPENDED`/`RESUMED` transitions and every attempt in the trial table; the breaker and the resume rule are pure functions of the persisted sequence; replay never re-probes. This extends the M6.3 `ReplaySource` unchanged in kind.
* **Optional optimisation [D, Phase 2 only]**: *deferred verification* — persist the final environment snapshot of an `VERIFICATION_UNAVAILABLE` attempt as an artifact and re-verify it when the verifier returns (the verifier sees only the snapshot), converting waste into evidence at zero model tokens. Not in the first implementation: the circuit breaker removes most of the 127k tokens/stream (B.4) without new storage or a new trust question (snapshot immutability).

## 8. Provenance and freshness gates

Design rule: **every gate is deterministic, fail-closed, observable and replayable.** *Absent data is a failure, never a pass*; an explicit, audited policy flag (`allow_unattested_reference`) is the only way to relax a gate and is recorded in the report. A **final gate sweep** re-evaluates all gates over the persisted trial rows at the instant of `RECOVERED`; the transition is written only if every gate passed, and the `GateReport` (gate id, inputs, expected, actual, pass/fail) is stored with it.

| id | gate | evaluated | deterministic? | fail-closed behaviour |
|---|---|---|---|---|
| P1 | reference source `evidence_kind` is production-eligible (or the policy explicitly allows research evidence) | case open, sweep | yes | `production_eligible is None` → fail |
| P2 | reference source tenant = policy tenant = executor tenant = trial tenant | open, each trial, sweep | yes | any `None` → fail (today `None` skips the check) |
| P3 | capability identity: trial `capability_ref` = `ref.alt_key` (id@version) | each trial | yes | mismatch → trial rejected + `GATE_FAILED` |
| P4 | execution identity: executor id and build recorded and constant within a case | each trial | yes | missing → fail |
| P5 | verifier identity and version recorded, constant, and equal to the accepted set; independence attested by the adapter | each trial, sweep | yes | missing/unknown → fail (today `None` is permissive) |
| P6 | runtime, **model id** and **tool-catalogue version** recorded and equal to the reference (statistical case), or one consistent set (structural case, then rebased) | each trial, sweep | yes | reference lacking a field → fail unless `allow_unattested_reference` |
| P7 | lineage: unique tasks, newer than the trigger, **disjoint from the reference's task set** (per-task id hashes stored with the source) | each trial | yes | missing task digest → fail |
| P8 | the verdict was produced by the verifier port, not the model | construction | yes (type-level) | adapter writes the verdict; asserted in tests |
| F1 | reference age ≤ `max_evidence_age` (else the stale protocol applies) | open | yes | unknown age → fail |
| F2 | each decision-window trial is no older than `trial_max_age_hours` [P: 120] at the decision | each look, sweep | yes | stale trials dropped (journaled) |
| F3 | a version change *during* a cycle invalidates the cycle (restart without consuming a cycle) | each trial | yes | — |
| F4 | after a rebase the reference carries `revalidated_at`; freshness restarts from the evidence time | `RECOVERED` | yes | — |

**What needs statistical inference (and is therefore *not* a gate):** (i) whether the revalidation pool represents current production traffic (covariate shift) — a threat to validity (§16) and a possible *report* (family-mix comparison), not a pass/fail gate; (ii) whether a permitted version change altered performance — that is precisely what the revalidation test measures.

**Enforcement beyond unit tests (requirements):** (1) adversarial integration fixtures per gate through the full service and PostgreSQL path (ineligible evidence cannot reach `RECOVERED`); (2) the database CHECK of §5.1; (3) an audit-query test: no `RECOVERED` row exists without an all-passed report; (4) replay detects a tampered or removed trial row; (5) experiment scenarios G1–G5 (§12) with **0 restorations** as an exact criterion; (6) a mutation run killing every gate mutant; (7) `reval-status --gates` shows each failure with its inputs.

Consequence for the simulator: the synthetic priors currently carry `None` provenance; the harness must attest them explicitly (`evidence_kind = SIMULATION_ATTESTED`, a `SIM` executor/verifier identity) — otherwise every M6.3-style simulation would fail closed, which is the intended behaviour. **[D]**

## 9. Economic eligibility proposal

### 9.1 Variable inventory

| variable | status | source / note |
|---|---|---|
| per-decision token advantage Δτ = tokens(default) − tokens(alternative) | **measured** | the reference's paired table (`tokens_def/n`, `tokens_alt/n`) |
| per-decision success difference Δs | **measured, uncertain** | reference paired table; ≈ +0.02 here, interval spans zero |
| decision rate r of the context | **measured** | decision log (per cell) |
| mean tokens per trial τ, plan N, confirmation trials, power | **measured / computed** | executor history; `plan_fixed` |
| remaining benefit horizon H | **unavailable → declared** | an operator-declared policy parameter; no historical basis; absence ⇒ the gate is *unavailable* and fails closed |
| probability that the capability has recovered | **unavailable** | no recovery base rates exist (synthetic only); **not used** |
| cost of a false restoration | **estimable** (≈ exposure × degradation ≈ detection latency × success loss) | M6.2 latency, measured; not in v1 of the gate |
| cost of leaving it quarantined | **derived** = Δτ·r·H (and Δs·r·H·ρ if ρ is declared) | |
| cost of native recovery | **measurable but different currency** | exposure/successes, not tokens (B.2) |
| alternative capability cost | **measured** | the rule default is the alternative |
| exchange rate ρ (tokens per verified success) | **declared, optional** | no default; the benchmark's 20,000 is *not* a deployment value |

### 9.2 Simplest gate (proposal `economic-gate/1`)

A **necessary-condition, token-denominated, weight-free** rule:

> *go* ⇔ Δτ·r·H_eff ≥ cost_est **and** the reference's non-inferiority holds (lower bound of Δs ≥ −margin); otherwise *no-go* (`ECON_NO_GO`, route E).

with `H_eff = H − t_cycle` (expected time to restore), `cost_est = (N_plan + confirm_trials) · τ · (1 + (1 − power))` (one expected failed cycle's worth, from the exact plan — no probability of recovery is invented), and `H` declared. It never *promises* profitability: it only excludes cases that **cannot pay even if recovery were certain**. Success difference is used only as a **constraint** (non-inferiority), never traded for tokens — consistent with the engineering rule that token savings must not compensate for correctness. If an operator declares ρ, the benefit adds Δs·r·H_eff·ρ; the experiment reports results with and without it. **Fail-closed**: any missing input (including an undeclared H) → `ECON_NO_GO` with reason `GATE_UNAVAILABLE`, which for structural/stale reasons means the case holds (conservative) — flagged **[D]** because it blocks the one proven benefit unless H is declared.

Where it applies: RT-3 and RT-4 entries to B (not to A: native recovery uses production traffic and is guarded by exposure/safety metrics, not tokens).

### 9.3 Three roles, kept separate

| role | definition | may it depend on the others? |
|---|---|---|
| **benchmark utility** (reporting only) | verified successes − tokens·λ, λ swept (1/5k … 1/300k) and the break-even λ reported | no input to the policy |
| **eligibility policy** (controller) | `economic-gate/1` above plus non-inferiority | uses measured Δτ, r, plan; declared H (and ρ); **never λ of the benchmark** |
| **evaluation metrics** (pre-registered, §13) | Δ successes and Δ tokens as a *pair*, price per extra success, exposure, false restoration, regret of the gate | evaluated for the policy at *several declared H* (factor), not tuned to one |

Anti-gaming rule: gate parameters are *declared* (H as a factor with ≥ 3 values; margin inherited from M6.3), never fitted on any seed; the dev-seed work may *calibrate nothing in the gate*. **The gate is judged by its regret against an oracle** (cases where revalidation was net-token-negative vs net-token-positive in hindsight), not by the benchmark utility.

## 10. Relapse protection

Findings that shape it (B.5): exposure = detector latency ≈ 45–47 decisions; thresholds alone move it to ≈ 31 at a 6.7 % false-alarm cost; `min_samples` is not binding. **Proposal: reuse the M6.2 detector; do not add one.**

* **Probation** = a window after `RECOVERED` during which the cell's monitor runs a *probation profile* of the existing `DriftParams` (confidence/persistence), then returns to the ordinary profile. Length [P]: 2 × `window` = 80 alternative observations (so a relapse in the first stretch is seen with the sensitive profile; counted in *known alternative observations*, not wall-clock, so verification outages cannot end it early).
* **Candidate profiles (to be compared on development seeds; the selection rule is pre-registered):** PR0 frozen (0.995, persistence 10); PR1 (0.99, 5); PR2 (0.975, 5); PR3 (0.975, 3). **Selection rule:** the profile with the largest median exposure reduction among those with ≤ 2/60 control false alarms on the development control stream (from the audit's frontier only PR1 qualifies, −7 decisions ≈ −16 %); adoption additionally requires RQ6's ≥ 20 % reduction on the full development experiment, **otherwise probation is rejected and relapse is reported as an accepted limitation**. Recovery thresholds are *not* reused (they answer "may we trust again", with confidence 0.80 and tolerance 0.05); relapse thresholds answer "has it broken again" and inherit the *detection* family, justified by the measured frontier.
* **Consequence of a relapse:** a new case (`episode + 1`) with `relapse_of` recorded as a contributing flag; it is routed like any other (RT-5 → native for a statistical reason, so a false re-quarantine is cheap); after `max_episodes` the cell locks (existing M6.2 rule), bounding flapping.
* **Maximum degraded exposure:** reported as the 95th percentile of exposure in relapse scenarios; **no guarantee is claimed** — a bound below ≈ 30 decisions would require limiting the alternative's traffic share, a new mechanism not proposed without a measured need.
* **Context-specific relapse and verification outages:** monitors are per cell (family, risk); a relapse in one context must not quarantine another (regression test modelled on M6.2's S7); unverified production outcomes are `UNKNOWN`, ignored, and do not advance probation.
* **Evaluation:** relapse rate, exposure (median, 95th), re-quarantine latency, false re-quarantine rate on the post-recovery healthy tail (control RL4).

## 11. State machine

No new case states (the seven M6.3 states stay). New **triggers** are journaled as transitions; availability is a **flag** on the case, not a state.

```
M6.2 monitor: ... SUSPECT ─persistence─▶ DRIFTED ─▶ QUARANTINED (QUARANTINE_APPLIED)
                                                        │ normalise + route + journal (ROUTED)
        ┌───────────────────────────────┬───────────────┴───────────────┐
        ▼ A: no case; M6.2 RECOVERING …  ▼ B: case QUARANTINED          ▼ E: case QUARANTINED, inert (HOLD)
          (monitor not locked)             │ gates (GATE_FAILED → E), economic gate (ECON_NO_GO → E)
                                           ▼ readiness ─▶ REVALIDATION_PENDING ─▶ REVALIDATING ─▶ RECOVERY_CANDIDATE ─▶ RECOVERED ─▶ probation ─▶ NORMAL
                                           (SUSPENDED/RESUMED/EVIDENCE_EXPIRED are self-transitions of PENDING, REVALIDATING, RECOVERY_CANDIDATE)
 terminal: EXPIRED (TTL, SUSPENSION_TIMEOUT)   BUDGET_EXHAUSTED (trial/token/lifetime caps, cycles exhausted by criteria failures only)
```

| from | trigger | to | notes |
|---|---|---|---|
| (new quarantine) | `ROUTED` | QUARANTINED (self) | route, rule, reasons, inputs hash persisted; for route A this is the only row (no case) |
| QUARANTINED | `GATE_FAILED` / `ECON_NO_GO` | QUARANTINED (self, inert) | transient gates re-evaluated at the next round; permanent ones end the episode as today |
| PENDING / REVALIDATING / CANDIDATE | `SUSPENDED` | same | `suspended = true`; clocks paused; facts persisted |
| same | `RESUMED` | same | after `m` healthy probes |
| REVALIDATING / CANDIDATE | `EVIDENCE_EXPIRED` | same | counters reset, **no cycle consumed** |
| any suspended | `SUSPENSION_TIMEOUT` | EXPIRED | prior stays disabled |
| RECOVERED | `PROBATION_STARTED` / `PROBATION_PASSED` | (monitor-level) | journaled; profile restored after the window |
| REVALIDATING / CANDIDATE | `CRITERIA_NOT_MET`, `CONFIRMATION_FAILED`, `LINEAGE_VIOLATION` | QUARANTINED (cycle + 1) | **only** capability evidence consumes cycles |

Invariants kept from M6.3 (I1–I9) plus: **I10** a suspended case consumes no cycle, trial-evidence or TTL; **I11** no `RECOVERED` row without an all-passed gate report; **I12** every route decision is reproducible from its persisted inputs and policy version; **I13** route A never sets the monitor lock; routes B/E always do.

## 12. Experiment design

### 12.1 Variants (identical paired streams, common random numbers)

| id | variant | purpose |
|---|---|---|
| B1 | M6.2 native recovery (`m62`) | baseline 1 |
| B2 | M6.3 controlled revalidation C (`m63_C`) | baseline 2 |
| B3 | **simple reason routing**: structural/stale → C; everything else → native; no gates, outage semantics, economics, probation | baseline 3 (isolates what *routing alone* buys) |
| T1 | B3 + verification-outage protocol (§7) | component |
| T2 | T1 + provenance/freshness gates (§8) | component |
| T3 | T2 + economic gate (§9), at each declared H | **treatment** |
| T4 | T3 + probation (§10), only if the dev rule adopts a profile | component |
| O | **oracle router** (uses the scenario's ground-truth best route per stream) | upper bound; separates routing error from recovery quality |
| anchors | `m61`, `m62_locked` | references, not hypotheses |

Incremental components (T1→T4) rather than a full factorial keep the run within budget; an escalation ablation (T5) is run on development seeds only unless approved **[D]**.

### 12.2 Scenarios (truth-labelled; weights are *not* assumed equal)

Tier 1 (held-out): routing-truth, single cause — **S5** temporary, **R3** common-cause, **S2** persistent, **R1** boundary/residual, **S4** cost-only, **S8** structural (harmless), **S8b** stale; mixed — **R2** structural+degraded, **X1** success+cost, **X2** structural+cost, **X3** stale+success; verification/infrastructure — **R4** outage during revalidation, **V3** infrastructure-failure window (30 % of trials error for 200 h); gates — **G1** ineligible reference provenance, **G2** foreign-tenant pool; unknown — **U1** externally injected unknown reason; post-recovery — **R5** relapse (+100 after restoration). Tier 2 (development and tests only unless extended): **V2** persistent outage beyond the TTL, **V4** interrupted/duplicate trials, **G3** mid-cycle version change, **G4** stale pool, **G5** unattested reference, **RL2** early relapse (+30), **RL3** flapping (two relapses), **RL4** healthy tail after recovery (false re-quarantine control), **MF1** two families (one structural, one statistical) in one stream.

Each scenario carries ground truth for (i) the correct route set, (ii) whether any restoration is legitimate at each position, (iii) the expected gate outcome. Scenario construction code is part of the frozen protocol and is reviewed for circularity (§16).

### 12.3 Keeping routing quality, recovery quality and system utility distinguishable

| layer | question | measured how | uses |
|---|---|---|---|
| **routing quality** | was the route right? | confusion matrix of the normalised set and route against the scenario's ground-truth route labels; label *stability* across seeds; **regret = outcome(actual route) − outcome(oracle route)** per stream | no execution difference needed; also computed in shadow |
| **recovery quality** | given the route, how well did the mechanism work? | outcomes **conditional on the route taken** (e.g. B-routed cases only: T vs B2; A-routed only: T vs B1): restoration, false restoration, latency, overhead, exposure | stratified paired comparison |
| **system utility** | end to end, is the policy better? | paired Δ verified successes and Δ total tokens versus B1 and B2, per scenario class and for **declared** mixtures; price per extra success; utility at a λ sweep | headline, interpreted last |

A result is only attributed to routing if O − T is small *and* T − B3 is large; to a component if the incremental variant differs; to nothing if only the pooled utility moves.

### 12.4 Seeds, sample size, integrity

Development seeds **7,500,000–7,500,299**; held-out seeds **9,100,000–9,100,199** (blocks disjoint from every earlier one and from the audit's 7,400,000–7,400,059). Fixed-sample design, no interim looks. Held-out size: **150 streams per scenario** (200 for the safety-critical hold scenarios R1, S2, S4, R2, G1, G2, U1: 0 events then bounds the rate at ≈ 1.5 %). Rationale from the M6.3 data: the paired-utility standard deviation is ≈ 45 in restoring scenarios (≈ 5 in hold scenarios), so 150 streams give a 95 % half-width of ≈ 7 utility per scenario and ≈ 2.3 pooled over 10 equally weighted restoring-type scenarios (an upper bound, since hold scenarios have far smaller variance); at 80 % power and α = 0.05 that detects a pooled difference of ≈ 3.3 utility, i.e. effects much smaller than M6.3's −29.7 are resolvable, while a few-percent effect is not. Estimated compute ≈ 17 scenarios × 8 variants × 150–200 ≈ 22k stream-runs ≈ 5–6 h on 8 workers (the M6.3 held-out took ≈ 90 min for 6k runs under load) **[D]**.

**Real-model stage:** the M6.1 stress portfolio contains hand-authored stale capabilities (a timeout capability whose key is frozen to a retired layout) — a *real-execution* degraded alternative, but **induced by construction, not naturally drifting**; the real-model experiment uses such fixtures plus induced runtime/provenance changes on fresh private tasks, so it tests the pipeline end to end, not real-world drift frequency. Cost cap ≈ 6M tokens (≈ 900 trials at 6.5k) **[D]**; exploratory unless it meets a pre-registered sample size.

## 13. Frozen metrics

Primary hypotheses (**4**, Holm-corrected; everything else is descriptive):

| id | hypothesis | measure and decision rule |
|---|---|---|
| H1 efficiency | T3 dominates B2 | paired Δtokens(T3−B2) 95 % CI upper < 0 **and** Δsuccesses(T3−B2) lower ≥ −1.0 per 800 decisions, pooled over the Tier-1 should-restore classes, at each declared H |
| H2 safety | T3 is safe | false restoration in R1/S2/S4/R2/G1/G2/U1: exact 95 % UB ≤ 5 % (R1 ≤ 10 %); **gate leaks = 0 exactly** in G1/G2 (held-out) and G3–G5 (development and integration tests) |
| H3 outages | the protocol removes outage-induced cycle loss | in R4/V3: cycles consumed by non-evidence attempts = 0 (exact); restoration ≥ B2's (lower CI of the difference ≥ −5 pp); wasted tokens < B2's |
| H4 routing | the reason set is routable | stability ≥ 95 % (single cause) / ≥ 90 % (mixed); route accuracy vs the oracle labels ≥ 95 % / ≥ 90 % |

Secondary: H5 versus B1 (successes non-inferior, exposure not worse, price per extra success reported), H6 probation (RQ6 rule), H7 economic-gate regret ≤ 10 % each way, RQ7 boundary.

**Metric families** (all computed per stream, then aggregated):

| family | metrics |
|---|---|
| primary outcome | total verified successes; total tokens (production + revalidation, with the wasted/infra/unknown ledger); cost per verified success; **price per additional success** vs B1/B2; benchmark utility at λ ∈ {1/5k, 1/20k, 1/50k, 1/100k, 1/300k} and the break-even λ |
| safety | false restoration; gate leaks; post-recovery degraded exposure (median, 95th); relapse rate; false re-quarantine rate |
| recovery | restoration rate, latency (decisions after the world recovered), recovery overhead (tokens per restoration); structural/stale restoration |
| verification | outage-consumed cycles; capability failures vs infrastructure failures vs unavailable outcomes; evidence invalidated (`EVIDENCE_EXPIRED`); budget consumption by outcome category |
| routing | correct / incorrect route; unnecessary revalidation (net-token-negative in hindsight); native bypassed; routing fallback (escalations); unknown-reason handling; label stability; regret vs O |
| economic | recovery attempts avoided; tokens avoided; recovery opportunity lost; cost of false restoration; net incremental verified success |

**Independence, duplication, conflict analysis (frozen before data):** *dependent* — utility is a function of Δsuccesses and Δtokens (report the pair, utility as a derived view); false restoration and exposure move together; "attempts avoided" and "tokens avoided" are one quantity in two units (primary: tokens); correct/incorrect route are complementary (report the confusion matrix, not both rates). *Conflicts* — restoration speed vs false restoration; the economic gate vs opportunity lost (the gate may only be credited if regret is symmetric); probation sensitivity vs false re-quarantine; routing to native vs R1 safety. *Multiplicity* — four primary hypotheses, Holm; the other ~25 metrics are descriptive and never used to claim success.

## 14. Baselines

B1 `m62` (unchanged M6.2 ENFORCE), B2 `m63_C` (frozen M6.3 configuration, including E1/E2), B3 simple reason routing, plus the incremental components T1–T4, the oracle router O, and the anchors `m61` and `m62_locked`. B3 uses the *primary* reason label (no set) — precisely what a naive implementation of the routing hypothesis would do — so that RQ1/RQ2 measure the value of the reason *set* and the gates, not of routing in the abstract.

## 15. Statistical methodology

Paired streams (one seed = one stream; identical task streams, perturbations and prior; common random numbers); percentile **bootstrap over streams** (2,000 resamples, fixed RNG) for mean differences; **exact Clopper–Pearson** for rates; non-inferiority by CI bound against pre-registered margins; **Holm** across H1–H4; hierarchical gating — H2 (safety) is evaluated first and a safety failure voids the efficiency claim; median [IQR] for latencies; no thresholds tuned on held-out data; every parameter in §5–§10 marked [P] is frozen at pre-registration; development-seed work may only (a) check implementation, (b) apply the pre-registered probation selection rule, (c) log errata *before* held-out data exists. **Practical effect sizes:** a difference is "practically meaningful" if it is ≥ 2 verified successes per 800 decisions **or** ≥ 5 % of M6.2's total tokens **or** ≥ 10 degraded decisions of exposure; smaller significant differences are reported, not claimed. **Verdict rule (clauses in order):** NOT SUPPORTED if H2 fails or T3 is dominated by B2; SUPPORTED if H1–H4 all hold; PARTIALLY SUPPORTED if H2 and H3 hold and at least one of H1/H4; otherwise INCONCLUSIVE. Component-level conclusions (T1…T4 vs their predecessors) are reported in all cases.

## 16. Threats to validity

* **Synthetic worlds designed by the author with the mechanisms in mind** (circularity): scenarios are built to exhibit the phenomena the routing exploits; mitigation: scenario code frozen and reviewed, ground truth written independently of the policy, real-model stage, explicit "unexercised" reporting.
* **The design was shaped by M6.3 held-out data**: M6.4 uses new seed blocks, and the audit's seeds are excluded; the M6.3 held-out numbers motivate hypotheses but are not reused as evidence.
* **Benchmark utility and declared horizon**: conclusions depend on the exchange rate; mitigated by reporting the pair (Δsuccess, Δtokens), break-even λ and the H factor.
* **Reason stability measured on scenario types it was built for**; real drift is more varied; mixed causes with more than two channels are untested.
* **Representativeness** of the revalidation pool (covariate shift) is unmodelled; **the paired advantage is not retested**.
* **Reference uncertainty and winner's-curse** (priors are accepted only when they resolve); guarantees are relative to the *estimated* reference.
* **Coupling**: routing depends on detector behaviour (priority, minimum samples); changing M6.2 thresholds later changes labels.
* **Fail-closed posture changes baselines**: attested simulation provenance must be introduced for all variants alike, or M6.3-style runs would fail closed.
* **Complexity risk**: five new components interacting; mitigated by incremental variants and the stop conditions.
* **Compute and real-model cost** limit sample sizes; the real stage cannot establish ground truth for drift timing.
* **Multiplicity and forking paths**: bounded by four primary hypotheses and a frozen analysis plan.

## 17. Implementation milestones and ordered task plan

Each step ends with tests, lint, type checks, an updated doc, and a commit; none starts before design approval; none touches M6.1/M6.2 behaviour.

| step | task | depends on | exit criterion |
|---|---|---|---|
| M6.4-0 | design review; pre-registration draft (§13–15); decisions in §20 | — | user approval |
| M6.4-1 | harness: attested simulation provenance; scenarios X1–X3, V3, G1, G2, U1 (+ Tier 2); oracle labels; baselines B1–B3 run on development seeds | M6.4-0 | scenario truth tests pass; B1–B3 reproduce M6.3 numbers within CI on dev seeds |
| M6.4-2 | `ReasonNormaliser` + persisted reason set (journal JSON, no routing) | M6.4-1 | RQ1 stability measured on dev seeds; **stop if below criterion** |
| M6.4-3 | outcome taxonomy + attempt ledger + availability probes + SUSPEND/RESUME/EVIDENCE_EXPIRED (+ migration 0009 part) | M6.4-1 | property tests (I10), replay, outage scenarios R4/V3 on dev |
| M6.4-4 | `EvidenceGates` + `GateReport` + final sweep + DB CHECK + CLI `--gates` | M6.4-1 | adversarial integration tests; G1–G5 leak = 0; mutation 100 % |
| M6.4-5 | `RecoveryRouter` + dispatch (route A never locks; B/E lock) + journal; shadow mode | M6.4-2 (and 3, 4 for the full policy) | routing tests; B3 and T1/T2 variants implemented |
| M6.4-6 | `EconomicGate` | M6.4-1 | regret computed on dev seeds at declared H values |
| M6.4-7 | probation profile via the existing monitor (additive hook) | M6.4-5 | pre-registered selection rule applied on dev; adopt or reject |
| M6.4-8 | freeze configuration; dev smoke (errata only); held-out run ×2 (reproducibility); report, ADR, tables | M6.4-2…7 | pre-registered verdict computed by code |
| M6.4-9 | real-model experiment (budgeted) | M6.4-8 | cost cap respected; reported as exploratory/confirmatory per sample size |
| M6.4-10 | live shadow design and implementation (M6.2 authoritative) | M6.4-8 | §19 shadow gate |
| (later) | canary — only if §19's evidence exists and the user approves | M6.4-10 | — |

Critical path: 0 → 1 → {2, 3, 4, 6} in parallel → 5 → 7 → 8. Steps 3 (outages) and 4 (gates) are deterministic, independently valuable, and are recommended **first** because they do not depend on the routing hypothesis (B.4, B.8).

## 18. Stop conditions

Stop and report (do not push on) when: **RQ1 fails** on development seeds (reason set stability below criterion) — routing is then not built and M6.4 delivers only the outage protocol and gates; **any gate leak** appears on development seeds; the outage protocol raises false restoration; the economic gate's regret exceeds 20 % in either direction at every declared H; the full policy is dominated by B1 or B2 on development seeds on both successes and tokens (no point spending the held-out budget); development compute exceeds twice the estimate; or the real-model budget is exhausted. Do not add adaptive complexity (learned thresholds, new detectors, traffic ramps, Bayesian priors) before a *specific measured weakness* is shown and a new pre-registered experiment is approved.

## 19. Go / no-go criteria

* **Go to implementation:** user approval of this design and of the decisions in §20.
* **Go to the held-out run:** M6.4-1…7 complete; every [P] parameter frozen and committed; dev errata logged; tests, lint, types, mutation, migration 0009 up/check/down/up all green; M6/M6.1/M6.2/M6.3 compatibility set unchanged.
* **No-go (negative result)** if the verdict rule yields NOT SUPPORTED or INCONCLUSIVE: report it, keep M6.2 native recovery and the M6.3 machinery as they are, and carry forward only the components whose own incremental result is positive (the outage protocol and the gates are expected to be retained on correctness grounds regardless of utility — a decision for review).
* **Shadow (live), M6.2 remains authoritative:** the router, gates and economic gate run on every real quarantine event; M6.4 **cannot alter a decision** (overlay unchanged; `applied=false`); every proposed route and gate report is logged and replayable; outcomes are evaluated independently by the existing outcome verification; disagreements with the authoritative path are adjudicated; real resource overhead (probe cost, journal volume, optional budgeted shadow trials) is measured. *Shadow trials*, if run, are out-of-band with a hard token cap and never touch the overlay **[D]**.
* **Evidence required to justify a canary (necessary, not sufficient, and never automatic):** ≥ 20 real quarantine events covering ≥ 3 reason classes (or an explicit statement that real traffic cannot supply them); 100 % replay reproducibility; **0** gate leaks and **0** restorations with a failed gate; every disagreement adjudicated with route accuracy ≥ 90 % against the adjudicated truth; measured overhead within a declared token budget; the pre-registered held-out verdict at least PARTIALLY SUPPORTED with H2 met; authoritative decisions bit-identical during shadow; and a rollback path tested. Absent this evidence the milestone ends at shadow.

## 20. Decisions requiring user review

1. **Reason model**: extend (primary + contributing + evidence + source), with **no** probability-like confidence — accept or request one?
2. **Fail-closed gates**: absent provenance/identity data fails the gate (a stricter posture than M6.3); the simulator must attest its synthetic provenance. Accept the breaking behavioural change *for M6.4 policies only*?
3. **Outage protocol parameters** [P]: breaker `k = 3`, resume `m = 2` probes, evidence window 120 h, `max_suspend_hours` 500 h, time paused while suspended. Is "outage never consumes a cycle" acceptable, and is deferred verification wanted later?
4. **Economic gate**: token-denominated necessary condition with a **declared horizon H**; an undeclared H fails closed (structural/stale cases then hold). Is an operator-declared ρ (tokens per verified success) allowed, and which H values should be the experiment's factor?
5. **Route for statistical reasons**: native (the hypothesis), C, or a *futility-screened native* (a short one-sided SPRT screen that can only veto native probing — it would avoid the 7–16 successes lost on persistent degradation at a screening cost that has not been measured; not in the primary design) — include the screened variant as an ablation?
6. **Escalation** from exhausted native recovery to B: off in the primary treatment, ablation only?
7. **Probation**: reuse the detector with a profile chosen by the pre-registered dev rule; confirm that a traffic-share ramp (a new mechanism) stays out of scope.
8. **Migration 0009** (widened CHECKs, two JSONB columns, one RECOVERED-requires-gates CHECK) — acceptable as the only schema change?
9. **Experiment scope**: Tier-1 list, 150/200 streams per scenario, the incremental variants, declared mixtures, ≈ 5–6 h of compute (and a second run for byte-identical reproducibility, or a 20 % subset?).
10. **Real-model budget** ≈ 6M tokens, an induced stale-capability fixture (retired-layout key) as the real degraded alternative.
11. **Shadow trials**: allow budgeted out-of-band trials during live shadow, or routes-only?
12. **Retention if the verdict is negative**: keep the outage protocol and the gates on correctness grounds regardless of utility?

## Appendix — how the design maps to the brief

Audit findings §1, failure analysis §2, objective §3, questions §4, architecture §5, routing matrix §6, verification-outage protocol §7, provenance/freshness §8, economics §9, relapse §10, state machine §11, experiment §12, metrics §13, baselines §14, statistics §15, threats §16, milestones and ordered plan §17, stop conditions §18, go/no-go §19, decisions §20.
