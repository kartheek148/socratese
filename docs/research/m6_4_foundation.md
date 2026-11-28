# M6.4 Phase 1 — verification semantics and evidence eligibility (foundation)

Status: **implemented, opt-in, not promoted.** Scope: the foundational correctness components only. Not included (and not started): reason-aware routing, economic eligibility, probation/relapse protection, shadow execution, adaptive or probabilistic recovery, escalation, any new exploration mechanism, production promotion, M7. The proposal this implements is [m6_4_design.md](./m6_4_design.md) §7 (outage protocol) and §8 (gates); the evidence behind it is [m6_4_audit.md](./m6_4_audit.md). Where the implementation differs from the design, §13 says so and why.

Everything is **per policy version and opt-in**: a revalidation configuration without a `recovery` block behaves exactly as under M6.3 (§12 shows the evidence).

## 1. What changed, in one page

| area | before (M6.3) | now (opt-in `RevalidationParams.recovery`) |
|---|---|---|
| unavailable verifier | `VERIFICATION_UNAVAILABLE` trials counted toward an "unverified fraction"; above 20 % the **cycle failed** (cycle + 1, dwell reset): an outage consumed recovery cycles (53 of 81 ended cycles in R4) | an attempt without a verdict is budget spend, **never evidence**, never a cycle; a circuit breaker **suspends** collection and the case waits for the verification path |
| outcome classes | five verdicts | seven classes (infrastructure failure, incomplete execution added); timeout **origin** stated by the adapter |
| accounting | one `tokens_cycle` / `n_unverified` | attempts and tokens (budget) kept apart from n/k (evidence); tokens by kind (unverified, infrastructure, incomplete, rejected) |
| provenance | version strings; permissive when absent | explicit, per-trial provenance document; **absent = unavailable = a failed gate** |
| recovery | statistics + confirmation | statistics + confirmation + **eleven gates over the recorded evidence**; no recovery without a persisted passing report |
| enforcement | tests | case machine → fleet guard → service guard → **database CHECK** |

## 2. Verification outcome semantics

`TrialVerdict` (stored, backward compatible) gains two members; `OutcomeClass` is the derived view that uses the seven names of the milestone. A trial result is written only by the executor adapter, never by the model.

| outcome class | `TrialVerdict` | evidence (n/k)? | budget (attempt + tokens) | feeds the outage breaker |
|---|---|---|---|---|
| `VERIFIED_SUCCESS` | `SUCCESS` | **yes, success** | yes | resets it |
| `VERIFIED_FAILURE` | `FAILURE` | **yes, failure** | yes | resets it |
| `CAPABILITY_TIMEOUT` | `TIMEOUT` (+ `timeout_origin = CAPABILITY`) | **yes, failure** | yes | resets it |
| `INVALID_OUTPUT` | `INVALID_OUTPUT` | **yes, failure** | yes | resets it |
| `VERIFICATION_UNAVAILABLE` | `VERIFICATION_UNAVAILABLE` | no | yes (ledger `tokens_unverified`) | **yes** |
| `INFRASTRUCTURE_FAILURE` | `INFRASTRUCTURE_FAILURE` (+ origin `INFRASTRUCTURE` for a timeout) | no | yes (`tokens_infra`) | **yes** |
| `EXECUTION_INCOMPLETE` | `EXECUTION_INCOMPLETE` | no | yes (`tokens_incomplete`) | neutral (neither extends nor resets) |

* **Timeout origin is explicit and validated.** `TIMEOUT` may carry only `CAPABILITY`; an infrastructure timeout must be reported as `INFRASTRUCTURE_FAILURE` with origin `INFRASTRUCTURE` (a `TIMEOUT` with origin `INFRASTRUCTURE`, or any other verdict with an origin, does not validate; the database repeats the rule). The orchestrator adapter says `MAX_STEPS` and `MAX_TOKENS` are the capability's; `DEADLINE` is the capability's **only if the infrastructure probe was healthy before and after the run**; a provider error (`LLM_ERROR`) or an exception that escaped the run is infrastructure; a run cancelled by its supervisor (`TrialInterrupted`) is incomplete.
* **An unattributed timeout is never hidden.** A `TIMEOUT` whose origin the adapter did not state still counts as a *failure* (conservative: it can only make a recovery harder) and gate 9 fails until the adapter attributes it.
* **Nothing unverified is ever a success.** `success` is true only for `SUCCESS`; `classify_run_outcome` gives incomplete and infrastructure precedence (a run that never finished, or whose provider failed, says nothing about the capability, whatever the environment looked like).

### Trial accounting versus evidence accounting

| counter (per cycle) | counts | drives |
|---|---|---|
| `ordinal`, `c_ordinal` | **every attempt**, including rejected, unavailable, infrastructure, incomplete | the attempt cap `max_trials` (evidence and confirmation each) |
| `tokens_cycle` and the cell's lifetime spend | actual tokens of every attempt (an incomplete trial's real tokens included) | the token cap and lifetime budget |
| `tokens_unverified`, `tokens_infra`, `tokens_incomplete`, `tokens_rejected`, `n_infra`, `n_incomplete`, `n_unverified` | the ledger, by kind | audit; written into every transition's `measurements` |
| `n`, `k`, `c_n`, `c_k`, `tokens_counted`, the SPRT statistic | **evidence only** | every statistical decision and the cost test |

Consequence: an intermittent outage (never three attempts in a row) cannot consume a cycle or distort a statistic; it is bounded by the attempt budget (`TRIAL_CAP`), which expiry of evidence cannot launder either (§4).

A task is *consumed* only by evidence. A task whose attempt came back without a verdict may be attempted again; a task delivered twice as evidence, or an execution id delivered twice, is rejected `DUPLICATE` (stored, tokens accounted, not counted).

## 3. Outage state machine

Availability is a flag on the case (`suspended`), not a new case state: the seven M6.3 states are unchanged and the flag applies to `REVALIDATING` and `RECOVERY_CANDIDATE` (the confirmation stage obeys the same rules). **Frozen experimental parameters** (`RecoveryPolicy`; not optimal values):

| parameter | value | meaning |
|---|---|---|
| `outage_threshold` | **3** | consecutive unavailable observations that open the breaker |
| `resume_probes` | **2** | consecutive healthy probes that close it |
| `evidence_window_hours` | **120** | how long evidence stays usable |
| `max_suspension_hours` | **500** | cumulative suspension per case before it expires |

An *observation* is one attempt result (`VERIFICATION_UNAVAILABLE` or `INFRASTRUCTURE_FAILURE` extend the streak; any evidence resets it; `EXECUTION_INCOMPLETE` is neutral) or one **pre-round probe** (`ExecutorFacts.verification_available` and `infrastructure_available`, zero tokens: unhealthy extends the streak and nothing is spent that round; healthy clears it).

```
          unavailable attempt / unhealthy probe, streak < 3
   ┌──────────────────────────────────────────────┐ (OUTAGE_PROBE row when a probe changes the streak)
   ▼                                              │
 ACTIVE ──────────────────────────────────────────┘
   │  streak reaches 3 (attempts: mid-round, the round stops; probes: no trial spent)
   ▼                    SUSPENDED
 SUSPENDED ──► healthy probe 1..m-1 ─► OUTAGE_PROBE (count restarts on an unhealthy probe)
   │    │
   │    └─ m = 2 consecutive healthy probes ─► RESUMED ─► ACTIVE (the same round goes on)
   │                                              evidence older than the window: EVIDENCE_EXPIRED
   └─ cumulative suspension > 500 h ─► SUSPENSION_TIMEOUT ─► EXPIRED (terminal; the prior stays disabled)
```

| trigger (new) | emitted when | state after |
|---|---|---|
| `SUSPENDED` | the streak reached the threshold | same state, `suspended = true`, streak and ledger in `measurements` |
| `OUTAGE_PROBE` | a probe changed the breaker (unhealthy before suspension; healthy after a streak; any probe that moves the resume count) | same state; the round's executor facts are persisted on the row |
| `RESUMED` | the resume count reached `resume_probes` | same state; `suspended = false`, counters reset, **clocks shifted** by the spell |
| `SUSPENSION_TIMEOUT` | cumulative suspended time `> 500 h` (inclusive boundary: exactly 500 h is still suspended) | `EXPIRED` |
| `EVIDENCE_EXPIRED` | the oldest evidence of the cycle is older than 120 h at the end of the round | `REVALIDATING`, evidence counters reset, **no cycle consumed**, trial ordinals continue (no key is reused) |
| `GATE_FAILED` | the statistics and confirmation passed but a gate did not | `QUARANTINED`, cycle + 1 (see §6) |

What the breaker guarantees (each is a test):
1. it opens after **exactly** the configured number of consecutive unavailable observations (boundary: threshold − 1 does not open it);
2. evidence accumulated before the outage is kept (within the window), and nothing is spent or executed while suspended;
3. resuming needs `resume_probes` *consecutive* healthy probes; an unhealthy probe restarts the count; probes are not trials and never become evidence;
4. the **case TTL and cycle limit do not run while suspended** (on resume the case and cycle anchors move by the spell), and a case suspended longer than the cap expires, never recovering from the passage of time;
5. an outage consumes **no cycle**;
6. counters reset deterministically on resume;
7. every transition is a persisted row, so the trail is auditable.

## 4. Freshness

* **Window**: 120 h, inclusive (exactly 120 h passes; one second more fails). Applied by gate 8 over every evidence trial, and proactively by `EVIDENCE_EXPIRED`.
* **Decision time is an input, not the clock.** A recovery is decided at the *end of the learning-plane round* (`round start + round_hours`), the latest instant the round's evidence can carry; both the gate and the expiry check measure age at that instant, so they cannot disagree. A trial stamped later than that is "in the future" and fails; there is no clock-skew tolerance (none is justified by the data). Consequence: the 120 h window is effectively conservative by up to one round.
* **Missing timestamp** (the executor's `datetime.min` placeholder, or no decision time) fails; **timestamps that cannot be compared** (naive versus aware) fail.
* **Interaction with outages** (shown by a test, not hidden): with the frozen 20 h confirmation gap, a long outage mid-cycle makes the pre-outage evidence stale before the confirmation can finish; it is discarded (no cycle consumed), re-collected, and the attempt budget ends the cycle if the outage ate too much of it. This is the intended fail-closed behaviour and it is a cost, reported in §14.

## 5. Provenance model

No new evidence table: first-class columns that already existed (capability, tenant, runtime and verifier version, outcome, timestamp, task, ordinal/key) stay where they are; the rest is one `TrialProvenance` document per trial. **`None` always means unavailable**; no field is defaulted to something permissive, and `tool_versions = {}` is an explicit statement that no tool was used (different from `None`).

| required provenance | stored as |
|---|---|
| capability id | `capability_ref` (column) — compared with the case's `alt_key` (id@version) |
| tenant / context | `result_tenant_id` (column); `provenance.task_family`, `provenance.risk_level` |
| executor identity | `provenance.executor_id` |
| model identity, model version | `provenance.model_id`, `provenance.model_version` |
| runtime version | `runtime_version` (column) |
| tool identity and version | `provenance.tool_versions` (id → version) |
| verifier identity and version | `provenance.verifier_id`, `verifier_version` (column), `provenance.verifier_independent` (attested by the adapter) |
| evidence source | `provenance.evidence_source` |
| execution id | `provenance.execution_id` |
| trial id | the key `cell|episode|cycle|stage+ordinal` (`TrialKey.ident()`); the table also has a unique constraint on it |
| timestamp | `observed_at` (column) |
| outcome classification | `verdict` (column) + `timeout_origin` (column) |
| verification status | `provenance.verification_status` (`VERIFIED`, `UNAVAILABLE`, `NOT_COMPLETED`) |
| policy version | the trial row's `(tenant, policy_id, version)` foreign key, plus `gates_version` and the configuration hash on the gate report |
| evidence lineage | `provenance.pool_id`, `task_ref`, `execution_id`, the case/cycle/stage key, and the M6.3 lineage verdict (`accepted`, `reject_reason`) |

The orchestrator-backed source (`OrchestratorTrialSource`) records provenance only when given a `TrialAttestation` (executor, model, tools, verifier, independence, source, pool, an infrastructure probe); **without one it behaves exactly as under M6.3 and attests nothing**, so an unattested adapter can never recover a cell under a gated policy.

## 6. Gates

Eleven deterministic gates, all fail-closed, evaluated by `controller/recovery_gates.py` (pure; the decision time is an input). **Short-circuit policy: none.** All eleven are always evaluated so a report names every failure; an **empty evidence set fails every gate** with the reason "no evidence to evaluate" (never vacuously passes). Allow-lists live in the policy (so in its hash); **an empty allow-list accepts nothing**, i.e. a gated policy that does not name its executors, models, tools, verifiers and sources can never recover a cell.

| # | gate id | passes when, for every evidence trial |
|---|---|---|
| 1 | `evidence_presence` | at least one accepted, counted evidence-stage trial; at least `confirm_trials` counted confirmation trials; no rejected or non-evidence trial in the set |
| 2 | `capability_identity` | `capability_ref` is attested and equals the capability being recovered (id@version) |
| 3 | `context_isolation` | the recovery scope names a tenant; the trial's tenant is attested and equal; `task_family` (when the cell has a family) and `risk_level` are attested and equal to the cell's |
| 4 | `executor_compatibility` | `executor_id` attested, in `accepted_executors`, and constant over the set |
| 5 | `runtime_compatibility` | runtime version attested and equal to the reference's **or** in `validated_runtimes`; model id and version attested and accepted (`accepted_models`); runtime, model and model version constant over the set |
| 6 | `tool_compatibility` | `tool_versions` attested and every tool/version in `accepted_tools` |
| 7 | `verifier_integrity` | verifier id and version attested and accepted (`accepted_verifiers`); independence attested; the verifier is neither the executor nor the model; constant over the set |
| 8 | `evidence_freshness` | timestamp present, comparable, not in the future, and within the 120 h window at the decision time |
| 9 | `verified_outcome` | the outcome is one of the four evidence classes; a timeout states its origin; `verification_status` is attested and `VERIFIED` |
| 10 | `lineage_integrity` | the trial belongs to this case and cycle; task reference, execution id, evidence source (accepted) and pool attested; newer than the evidence that triggered the quarantine (its time must be known); no duplicate trial, task or execution in the set |
| 11 | `policy_integrity` | `gates_version` is explicit and supported (`m6.4-gates-v1`); the revalidation configuration hash is present |

**Where they run.** (a) A **preflight** on the executor's facts before any tokens are spent (gates 4–7 on `ExecutorFacts`; an unattested or incompatible executor is `INELIGIBLE`, transient, with the failing gate named). (b) The authoritative **sweep** over the recorded trials of the cycle at the moment the statistics and confirmation would restore the cell. A failing sweep is `GATE_FAILED`: the cycle is consumed (its evidence is spent and cannot be reused), the case returns to `QUARANTINED`, and after `max_cycles` such failures the episode ends. A gate failure can therefore never produce a recovery, and cannot loop forever.

### Gate result contract (`GateReport`, persisted on the transition)

```json
{
  "eligible": false,
  "policy_version": "m6.4-gates-v1",
  "failed_gates": ["evidence_freshness", "runtime_compatibility"],
  "passed_gates": ["evidence_presence", "capability_identity", "..."],
  "evidence_ids": ["<cell>|e1|c0|E0", "<cell>|e1|c0|C0"],
  "results": [{"gate": "evidence_freshness", "passed": false, "checked": 4,
               "failed_evidence": ["<cell>|e1|c0|E0"], "reasons": ["older than the 120 h window"]}, "..."],
  "evaluated_at": "2026-01-10T00:00:00Z", "config_hash": "<sha256 of the revalidation configuration>",
  "tenant_id": "bench", "cell_key": "<cell>", "episode": 1, "cycle": 0
}
```

`eligible` is **derived**: a report that asserts it without all eleven results passing, an evidence reference and a policy version is structurally invalid (`GateReport` refuses to parse), so a populated payload is never proof that the gates passed.

### Recovery cannot occur without a passing report (four layers)

1. **Case machine**: `RECOVERED` is reachable only through a passing sweep.
2. **Fleet guard**: `RevalidationFleet.step` checks the restoring transition's report *before applying or recording anything*; a missing, ineligible, malformed, unversioned, evidence-less or other-configuration report raises `RecoveryGateViolation` and the cell stays prior-disabled.
3. **Service guard**: `RevalidationService._persist` refuses to append such a transition.
4. **Database**: CHECK `recovery_needs_passing_gates` — a `RECOVERED` row whose own `config` carries a `recovery` block must have `gates.eligible = 'true'`, a policy version, no failed gate, passed gates, evidence ids and `gates.config_hash = params_hash` (NULL-safe: a missing key fails; arrays are type-checked). A second CHECK says a `GATE_FAILED` row must carry a report that says *not* eligible.

On replay the sweep is recomputed from the persisted trials, so a stored `RECOVERED` whose evidence was altered no longer reproduces and is refused (`RevalidationReplayMismatch`).

## 7. Persistence (migration `0009`, audit)

**Is a migration necessary?** Yes, and it is the smallest one that is safe: the trial table has fixed columns (no document column for provenance, no place for a timeout origin) and CHECK constraints that list the old verdicts and triggers, and the transition table has nowhere to store a structured gate report that a CHECK can inspect. Reusing `facts`/`detail` would make the "recovery needs a passing report" rule impossible to enforce in the database. **No new table**, no data rewritten, the append-only triggers unchanged.

`alembic/versions/0009_recovery_evidence.py`:

| table | change |
|---|---|
| `controller_revalidation_transitions` | + `gates JSONB NOT NULL DEFAULT '{}'`; trigger CHECK widened (`SUSPENDED`, `OUTAGE_PROBE`, `RESUMED`, `SUSPENSION_TIMEOUT`, `EVIDENCE_EXPIRED`, `GATE_FAILED`); CHECKs `gates_object`, `recovery_needs_passing_gates`, `gate_failed_not_eligible` |
| `controller_revalidation_trials` | + `timeout_origin TEXT`, + `provenance JSONB NOT NULL DEFAULT '{}'`; verdict CHECK widened (`INFRASTRUCTURE_FAILURE`, `EXECUTION_INCOMPLETE`); CHECKs `timeout_origin_matches_verdict`, `provenance_object` |

Compatibility: every row written before the migration stays valid (`gates = {}`, `provenance = {}`, no origin; their `config` has no `recovery` block, so the RECOVERED rule does not apply to them). The downgrade **refuses while any row uses the new vocabulary** (new triggers, a non-empty report, new verdicts, an origin, a provenance document) and otherwise restores the old CHECKs and drops the columns. Table metadata (`adapters/db/tables.py`) matches the migration (the no-drift test and `alembic check` pass). Outage state itself needs no column: it is carried by the `measurements` of every transition and recomputed on replay.

## 8. Replay

`replay_revalidation` recomputes the fleet from the ordered outcomes and the *persisted* trials and transitions only. Reproduced exactly (tests compare full model dumps): every transition and measurement; verification availability; suspension, probes and resume; evidence expiry; evidence and budget ledgers; gate reports and the recovery decision; the case state and the M6.2 monitor state and overlay hash.

* **Availability is replayed from what was recorded, never re-probed.** The rule that makes this exact: every round whose probe changes the breaker emits a row carrying that round's facts, and `ReplaySource.facts` serves the facts of the row at the round (else the latest recorded). A suspended round whose unhealthy probe changes nothing needs no row, and the replay cannot diverge there (the last recorded facts are themselves unhealthy by construction).
* **No wall clock.** Time enters only as the round time (persisted on every row) and the decision time derived from it. A test patches `datetime` in the replay modules to raise on `now()`/`utcnow()`, a source scan forbids clock reads in the four modules, and three consecutive replays are byte-identical.
* **Tampering is detected**: changing a stored trial's provenance changes the replayed sweep (`GATE_FAILED` instead of `RECOVERED`); removing trials stops the replay where the evidence stops.
* **An interrupted round leaves no trace.** A round persists trials and transitions in one transaction after `step` returns; an executor that raises mid-round leaves the log, the fleet and the counters exactly as before (tests at the fleet level and against PostgreSQL), and a duplicate or partly duplicate append is refused whole.

## 9. Tests added

| file | tests | covers |
|---|---|---|
| `tests/unit/test_recovery_outcomes.py` | 46 | outcome classes, trial vs evidence accounting, breaker threshold/resume/hysteresis/cap/clock/window boundaries, confirmation-stage outages, duplicates, M6.3 contrast |
| `tests/unit/test_recovery_gates.py` | 71 | each gate failing alone (34 mutations), missing provenance, freshness boundaries, contract, determinism, recovery constraints, preflight |
| `tests/unit/test_recovery_replay.py` | 13 | replay equality (all strategies), no clock, tamper/truncation, interruption, expiry path, status listing |
| `tests/unit/test_recovery_contracts.py` | 17 | frozen parameters, M6.3 hash pins, facts serialisation, outcome classes, provenance |
| `tests/migration/test_migrations.py` (+8) | 18 total | 0009 vocabulary, the database's recovery rule, downgrade refusal/clean |
| `tests/integration/test_recovery_e2e.py` | 15 | PostgreSQL: gated restoration + exact replay, outage persisted, refusals at service and database, atomicity, provenance round trip, orchestrator source classification |

Support: `tests/recovery_support.py` (`AttestedScript`, a trial source that attests everything the gates need). Validation commands and results: §11.

## 10. Mutation checks

Single-line mutants of the new logic (gates, breaker, resume, caps, clocks, evidence window, sweep, guards, ledgers, serialisation, defaults) were run against the new unit tests (scratch harness, not committed: each mutant applied to a copy, the four unit files run with `-x`). **First pass: 55 valid mutants (one pattern did not match and was discarded), 49 killed, 6 survived.** The survivors exposed real test gaps, all closed: the confirmation-stage breaker and attempt cap, confirmation attempts counted as evidence, a runtime that equals the reference without being listed, a "missing timestamp" masked by the timezone check, a verifier equal to the model. **Second pass: those 6 plus 6 new mutants (gate detail/report dropped, recovery without a report, incomplete counted toward the streak, a suspension without a start time, a non-evidence verdict counted): 12 of 12 killed.** A last mutant (the service guard switched off) is killed by the PostgreSQL test (the database CHECK then refused the forged row, which is also the evidence that the two layers are independent). Total **62 mutants, all killed**.

## 11. Validation

Commands and results (all run; nothing here is a claim without its command). PostgreSQL 16 and RabbitMQ 4 from `deploy/docker-compose.yml` (project `ee-m64`).

```
uv run ruff check .
uv run ruff format --check .
uv run mypy src
EE_TEST_DATABASE_URL=postgresql+psycopg://ee:ee_dev_password@127.0.0.1:55437/postgres \
EE_TEST_AMQP_URL='amqp://ee:ee_dev_password@127.0.0.1:57001//' uv run pytest -p no:cacheprovider -v
EE_DATABASE_URL=postgresql+psycopg://ee:ee_dev_password@127.0.0.1:55437/<scratch> \
  uv run alembic upgrade head | check | downgrade -1 | upgrade head | check | downgrade base | upgrade head | check
uv run ee-ctl reval-sim --seed-start 7600000 --seeds 6 --workers 6 --json-out new.json   # vs the previous commit
```

| check | result |
|---|---|
| `ruff check .` / `ruff format --check .` | all checks passed / 362 files formatted |
| `mypy src` | no issues in 184 source files |
| full `pytest` (real PostgreSQL and RabbitMQ) | **1030 passed, 5 warnings, exit code 0, 28 min 9 s**. M6.3's suite had 860: **+170 new tests, 0 existing tests changed** (`git diff tests/` shows additions only). The 5 warnings are the pre-existing `os.fork` deprecation notices of the M6.2/M6.3 CLI round-trip tests. |
| new tests, by file | outcomes 46, gates 71, replay 13, contracts 17, PostgreSQL integration 15, migration +8 (18 in the file) |
| migration 0009 | `alembic upgrade head` → `0009 (head)`; `alembic check` → "No new upgrade operations detected."; `downgrade -1` → `0008`; `upgrade head` → `0009`; `check` clean; `downgrade base` and `upgrade head` again; `check` clean. Migration tests: new vocabulary, the database's recovery rule (nine refused payloads, three accepted forms), downgrade refused for five kinds of M6.4 rows and clean when only earlier vocabulary is present |
| deterministic replay | the replay tests compare full model dumps of transitions, trials, case state, monitor state and the overlay hash for all three strategies, for a recovery run and an expiry run, three times over with `datetime.now()` patched to raise; the PostgreSQL tests replay the stored log and find **0** new transitions |
| M6.3 byte comparison | `reval-sim` on seeds 7,600,000–7,600,005 (9 scenarios × all variants, 360 stream results): **byte-identical** output before and after the change (§12) |
| mutation | 62 of 62 killed (§10) |
| architecture | the layering test passes (`controller/recovery_gates.py` imports only `domain/` and `controller/`) |

## 12. Backward compatibility (M6.1 / M6.2 / M6.3)

* `RevalidationParams.recovery` is `None` by default and **omitted from the serialised form**, so every stored configuration hash is unchanged: the three strategies' default hashes and a tuned one are pinned in `test_recovery_contracts.py`, computed from the M6.3 code at the commit before this phase.
* `ExecutorFacts.to_json()` omits every M6.4 field at its default, so facts recorded by ungated policies serialise byte-for-byte as before, and M6.3 rows still load.
* A **M6.3 simulation byte-comparison**: `ee-ctl reval-sim --seed-start 7600000 --seeds 6` (all nine scenarios × all variants, 360 stream results) produced **byte-identical JSON** (sha256 `1f55413e…`) before and after the change.
* The M6.3 revalidation unit, integration and regression tests, the M6.1/M6.2 compatibility set and the pinned golden hashes pass **unchanged** (no existing test or baseline was edited).
* Under an ungated policy a `VERIFICATION_UNAVAILABLE` outage still consumes a cycle exactly as in M6.3 (a test contrasts it with the gated behaviour on the same script).

## 13. Differences from the design proposal (m6_4_design.md) and why

| design | implemented | why |
|---|---|---|
| `ControllerParams.recovery` | `RevalidationParams.recovery` | the block configures revalidation and shares its hash; `ControllerParams` already nests `revalidation` |
| outcome taxonomy with a derived "evidence?" column | `OutcomeClass` view over the stored `TrialVerdict` | reuse the persisted vocabulary; add only two members |
| `routing` JSONB column on transitions | **not added** | routing is out of scope for this phase |
| "unavailable observations" counted only by attempts or by a probe → suspend at once | both feed one streak of 3; an unhealthy probe spends nothing | follows the literal "consecutive unavailable threshold"; a single failed probe is not authoritative |
| TTL paused "up to a cap" | cumulative cap per case; anchors shift on resume | strictly bounds a case's lifetime (`case_ttl + max_suspension`) even under flapping |
| gate P3 "trial rejected + GATE_FAILED" per trial | one authoritative sweep + a static preflight | smaller change to the (unchanged) lineage admission; a per-trial early abort is deferred (§14) |
| per-trial `policy_version` | the trial's FK + `gates_version` and `config_hash` on the report | a trial is requested under one policy row; duplicating it per trial would not add information |
| `GATE_UNAVAILABLE` journalled for absent inputs | absent input = failed gate with the reason | same fail-closed effect, one mechanism |

## 14. Known limitations

* **Opt-in only.** The M6.3 simulator's executor attests nothing, so gated policies cannot yet be evaluated in simulation; the M6.4 harness step must give it an explicit, labelled synthetic attestation (not done here: it would change nothing in this phase's scope and would blur the "no invented provenance" rule).
* **Allow-lists are configuration.** They are only as good as their author; an empty or too-wide list is a policy problem the gates cannot detect. Verifier *independence* is attested by the adapter and checked only against the executor/model identities.
* **Gate failures are found at the sweep (and preflight), not trial by trial.** A systematically wrong executor attestation inside the trials (not in the facts) costs a cycle's tokens before it is caught; a per-trial early abort is the obvious follow-up.
* **Evidence expiry is all-or-nothing** and uses the attempt budget it already spent; with the frozen 20 h confirmation gap a long outage can exhaust a cycle without recovering (§4). The 120 h window and the 20 h gap were frozen independently; whether they fit together is an experimental question, not settled here.
* **The decision time is the round end**, so the window is conservative by up to a round (10 h by default).
* **Facts that are not reported** (`facts is None`) are treated as healthy before suspension and as unhealthy while suspended (a case cannot resume without a probe); `infrastructure_available` defaults to `True` for sources that cannot probe, which then rely on result-level `INFRASTRUCTURE_FAILURE`.
* **Tokens of a run that raised before returning are unobservable** and recorded as 0 (disclosed here, not estimated); a supervisor that knows the figure raises `TrialInterrupted(tokens)`.
* **The adapter classifies any exception that escapes a run as an infrastructure failure**, so a systematic bug looks like an outage (it will suspend the case after three, visibly); it never looks like a capability failure.
* **The database cannot verify the gate set.** Its CHECK ties a recovery to a passing, complete-looking report under the same configuration; that the report holds all eleven gates is enforced by the model validator, the service guard and the replay, not by SQL. A writer that records a `RECOVERED` row with a `config` lacking the recovery block bypasses the CHECK; the replay refuses such a log.
* **No real-model run** was made in this phase (nothing here is an experiment); the first measurements belong to the M6.4 harness and held-out steps.
* Cumulative suspension is capped, but a flapping verifier below the cap can still alternate suspend/resume; it is bounded by the attempt, token and lifetime budgets, and the cap.
