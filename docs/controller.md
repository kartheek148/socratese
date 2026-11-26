# Adaptive strategy controller (Milestone 6)

The controller decides, per task, *which eligible action to run*: general planning, or one of the capabilities the matcher already admitted. It is a learning-plane artifact (a versioned, hashed policy) consumed by the execution plane; execution never waits for training and never reads live tables. Design, audit and the frozen protocol: [experiments/adaptive-controller-design.md](./experiments/adaptive-controller-design.md); results: [evaluations/adaptive-controller-report.md](./evaluations/adaptive-controller-report.md); ADR [0015](./decisions/0015-adaptive-controller-bounded-learning.md).

## Constraints are outside the learned policy

```
task ─► matcher (permissions, tenant, preconditions, budgets, ACTIVE-only)   existing, deterministic
          │  eligible capabilities
          ▼
   feasibility layer (controller/feasibility.py)       rule baseline default + feasible set F(x)
          │  protected risk class / foreign tenant ► rule decision, no estimate read, probability 1
          ▼
   reliability constraint   non-inferiority (Newcombe-Wilson lower bound ≥ −δ_risk), optional
          │                 absolute success floor, minimum verified evidence per cell
          ▼
   cost objective           expected tokens per verified success, only among admissible actions
          ▼
   exploration (bandit)     ε-greedy over an explicit family/risk allow-list, behind a data gate
```

`high` risk can never be a learned class (rejected by the parameter type). The orchestrator still refuses a capability the matcher did not admit. Cost never compensates a failed constraint.

## Policies

| version | behaviour |
|---|---|
| `fixed-general-planning/1`, `rule-capability/1` | unchanged baselines (the rule baseline is also the default action of every adaptive policy) |
| `adaptive/N`, kind `COST_AWARE` | deterministic: deviates from the rule default only with enough verified evidence; never explores (probability 1.0) |
| `adaptive/N`, kind `BANDIT` | cost-aware plus gated ε-greedy exploration; the exact distribution is logged (`estimates["pi:<action>"]`) and the uniform draw is a hash of the decision id, so a decision replays exactly |

A policy version is an immutable `ControllerPolicyDefinition` (typed parameters + frozen estimator snapshot: per `(family, risk, action)` verified trials, successes, tokens; calibration summary; training provenance), identified by a content hash. Estimates use only **verified, acted** outcomes of the tenant; UNKNOWN and shadow (non-acted) decisions are excluded and counted.

Every decision stores the typed `DecisionFeatures` (feasible set, rule default, the evidence cells it read, snapshot hash, calibration flag, draw), the estimates, the selected action, the exact selection probability, the policy version and the reason; `outcome_status` is back-filled by the verified run (rule 10). `ee-ctl replay` recomputes stored decisions from the features and the registered definition.

## Evidence, calibration, off-policy estimates

* **Calibration** (`controller/calibration.py`): prequential (each prediction uses only earlier outcomes): Brier score, Brier skill vs the global rate (reported, not gated: design A1), ECE, reliability table, cost bias and MAPE. Gate: ≥100 scored predictions and ECE ≤ 0.10.
* **Off-policy** (`controller/replay.py`): IPS, SNIPS, direct and doubly-robust estimates with explicit support coverage. A log written by the rule baseline has zero coverage for any deviation, which the report states instead of inventing a counterfactual.
* **Full-information replay** (`controller/simulate.py`): paired recorded experiments (arms A and E) give every task's outcome under every feasible action; the controller learns only from the action it chose and pays for exploration. Verdict rule (design A2): BEATS / EQUAL / WORSE / INCONCLUSIVE from per-task cluster-bootstrap bounds.

## Rollout: offline → shadow → canary → active, with rollback

`ControllerPolicyRegistry` is the only component that changes a policy's state. A forward step needs a stored **PASS** for the previous stage computed for exactly this definition hash and the current gate set (and PostgreSQL re-checks it in a trigger):

| step | evidence | PASS requires |
|---|---|---|
| CANDIDATE → SHADOW | OFFLINE replay on the decision log | ≥100 decisions, calibration gate, 0 protected deviations, 0 choices outside the feasible set, no predicted harm where the log supports the candidate |
| SHADOW → CANARY | the candidate's non-acted decisions next to the acted ones | ≥100 decisions with verified outcomes and scored predictions, live ECE ≤ 0.10, 0 protected deviations, 0 shadow errors |
| CANARY → ACTIVE | concurrent comparison, stable hash split of task ids (`canary_fraction`, low-risk learned classes only) | ≥30 per arm, non-inferiority lower bound ≥ −0.10, no protected exposure; canary control must be the currently ACTIVE version |

A FAIL in the canary rolls the candidate back automatically (`apply_canary_guard`). `rollback` of an ACTIVE version restores the previously superseded one in the same transaction; REJECTED and ROLLED_BACK are terminal (re-admission is a new version). At most one SHADOW, CANARY and ACTIVE version per policy (partial unique indexes). `build_rollout` reads the registry, so a rollback takes effect on the next controller build.

The orchestrator can attach a **shadow** controller (`TaskOrchestrator(shadow=...)`): it decides alongside the acting one, its decision is stored with `acted=false` and never executed, and any exception in it is recorded as a `controller_shadow` error trace event without touching the run.

## Commands

```
uv run ee-ctl train      --tenant bench --policy-id adaptive --kind bandit --explore-families pool timeout   # CANDIDATE from the decision log
uv run ee-ctl evaluate   offline|shadow|canary --version N                                                   # stored evidence, exit 1 on FAIL
uv run ee-ctl promote    --version N --to shadow|canary|active                                                # blocked without PASS evidence
uv run ee-ctl rollback   --version N --reason "..."      /  reject  /  status  /  replay --version N
uv run ee-ctl simulate   --heldout NAME RESULTS --warm OTHER.json --out report.md                           # offline, full information
uv run ee-ctl paired-run --portfolio heterogeneous --root PUB --private-root PRIV --out r.json               # real model, arms A and E
uv run ee-ctl live       --portfolio heterogeneous --root PUB --private-root PRIV --canary-fraction 0.5      # persisted, registry-driven
```
`paired-run` and `live` use hand-authored **stress fixtures** (`controller_fixtures.py`; a compiler-shaped capability and a *drifted* one whose target key is frozen to its original layout). They are validated through the normal DSL validator, held in memory, never registered or promoted, and every run is tagged experimental.

## Schema (migration 0005)

`controller_policies` (immutable definition + hash; only `state` changes, along the legal graph, with a trigger requiring stage evidence), `controller_policy_evaluations` and `controller_policy_transitions` (append-only), partial unique indexes for one ACTIVE/CANARY/SHADOW version per policy, and `policy_decisions.acted boolean NOT NULL DEFAULT true`. See [schema.md](./schema.md).

## Limits

* The estimator is tabular over `(family, risk, action)`; it cannot see within-family variant differences (a capability that is good on some layouts and bad on others averages out).
* Non-inferiority at a 10-point margin needs on the order of 100+ verified trials per action; the learned controller is therefore conservative at benchmark scale and defers to the rule baseline until it has them.
* Exploration costs successes and tokens; whether it pays back depends on the horizon (see the report).
* `HUMAN_ESCALATION` is never selected by a learned policy; no cost model for humans exists.
