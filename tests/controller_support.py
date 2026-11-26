"""Builders shared by the M6 controller tests (pure, no I/O)."""

import random
from collections.abc import Sequence

from test_capability_runtime import eligible, make

from experience_engine.controller.estimator import build_snapshot
from experience_engine.controller.feasibility import feasible_actions
from experience_engine.domain.controller import (
    PLANNING_KEY,
    CellStats,
    ControllerKind,
    ControllerParams,
    ControllerPolicyDefinition,
    DecisionOutcomeRecord,
    EstimatorSnapshot,
    cell_key,
)
from experience_engine.domain.outcome import OutcomeStatus
from experience_engine.domain.task import TaskSpec
from experience_engine.execution.controller import ControllerInput
from experience_engine.execution.matcher import MatchResult
from support import first_task

TENANT = "bench"
CAP = "pool.00000000aa@1"
CAP_KEY = f"DIRECT_CAPABILITY:{CAP}"


def task(risk: str = "low", family: str = "pool", tenant: str = TENANT) -> TaskSpec:
    t = first_task().task
    return t.model_copy(update={"risk_level": risk, "task_family": family, "tenant_id": tenant})


def match_with(*ids: str) -> MatchResult:
    caps = [(make(id=i.split("@")[0]), 0.9 - 0.1 * n) for n, i in enumerate(ids)]
    return MatchResult(n_catalog=len(caps), eligible=[eligible(d, s) for d, s in caps])


def ctx(t: TaskSpec, match: MatchResult | None, decision_id: str = "d-0000") -> ControllerInput:
    return ControllerInput(task=t, run_id="run-1", decision_id=decision_id, match=match)


def snapshot_of(
    cells: dict[str, tuple[int, int, int]],
    *,
    family: str = "pool",
    risk: str = "low",
    calibrated: bool = True,
    n_verified: int | None = None,
) -> EstimatorSnapshot:
    """cells: action key -> (n, successes, token_sum)."""
    from experience_engine.domain.controller import CalibrationSummary

    cs = {
        cell_key(family, risk, k): CellStats(n=n, successes=s, token_sum=tok)
        for k, (n, s, tok) in cells.items()
    }
    total = sum(c.n for c in cs.values()) if n_verified is None else n_verified
    return EstimatorSnapshot(
        tenant_id=TENANT,
        cells=cs,
        n_verified=total,
        calibration=CalibrationSummary(
            n=500, brier=0.1, brier_skill=0.1, ece=0.02, cost_bias=0.0, cost_mape=0.1, ok=calibrated
        ),
    )


def definition(
    snapshot: EstimatorSnapshot,
    kind: ControllerKind = ControllerKind.COST_AWARE,
    version: int = 1,
    **params: object,
) -> ControllerPolicyDefinition:
    return ControllerPolicyDefinition(
        policy_id="adaptive",
        version=version,
        kind=kind,
        params=ControllerParams.model_validate(params),
        snapshot=snapshot,
    )


def record(
    i: int,
    action: str,
    success: bool,
    tokens: int,
    *,
    family: str = "pool",
    risk: str = "low",
    status: OutcomeStatus | None = None,
    acted: bool = True,
    tenant: str = TENANT,
    p: float | None = 1.0,
    with_actions: bool = True,
    propensities: dict[str, float] | None = None,
) -> DecisionOutcomeRecord:
    st = status or (OutcomeStatus.VERIFIED_SUCCESS if success else OutcomeStatus.FAILURE)
    fs = feasible_actions(match_with(CAP))
    return DecisionOutcomeRecord(
        decision_id=f"d-{i:06d}",
        run_id=f"r-{i:06d}",
        tenant_id=tenant,
        policy_version="rule-capability/1",
        acted=acted,
        selection_probability=p,
        family=family,
        risk_level=risk,  # type: ignore[arg-type]
        action=action,
        status=st,
        tokens=tokens,
        n_allowed_tools=4,
        actions=fs.actions if with_actions else [],
        default_key=fs.default_key,
        propensities=propensities or {action: p or 1.0},
    )


def synthetic_log(
    rates: dict[str, float],
    cost: dict[str, int],
    n: int,
    seed: int = 0,
    mix: dict[str, float] | None = None,
) -> list[DecisionOutcomeRecord]:
    """Outcomes drawn from known success rates; actions drawn from `mix` (default: all default)."""
    rng = random.Random(seed)
    mix = mix or {CAP_KEY: 1.0}
    keys = sorted(mix)
    out: list[DecisionOutcomeRecord] = []
    for i in range(n):
        u, acc, a = rng.random(), 0.0, keys[-1]
        for k in keys:
            acc += mix[k]
            if u < acc:
                a = k
                break
        out.append(record(i, a, rng.random() < rates[a], cost[a], propensities=dict(mix)))
    return out


def trained(
    records: Sequence[DecisionOutcomeRecord],
    kind: ControllerKind = ControllerKind.COST_AWARE,
    **params: object,
) -> ControllerPolicyDefinition:
    p = ControllerParams.model_validate(params)
    return ControllerPolicyDefinition(
        policy_id="adaptive",
        version=1,
        kind=kind,
        params=p,
        snapshot=build_snapshot(records, TENANT, p),
    )


__all__ = [
    "CAP",
    "CAP_KEY",
    "PLANNING_KEY",
    "TENANT",
    "ctx",
    "definition",
    "match_with",
    "record",
    "snapshot_of",
    "synthetic_log",
    "task",
    "trained",
]


# ---- in-memory ControllerPolicyStore (unit tests; the SQL store has its own integration tests) --
from datetime import datetime  # noqa: E402

from experience_engine.domain.rollout import PolicyState, Stage, StageReport  # noqa: E402
from experience_engine.ports.controller_store import (  # noqa: E402
    PolicyRecord,
    PolicyTransition,
    StageEvidence,
    TransitionRecord,
)


class MemoryPolicyStore:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str, int], PolicyRecord] = {}
        self.evals: list[tuple[str, str, int, StageEvidence]] = []
        self.log: list[TransitionRecord] = []

    def add_version(self, definition: ControllerPolicyDefinition, now: datetime) -> PolicyRecord:
        key = (definition.tenant_id, definition.policy_id, definition.version)
        if key in self.rows:
            raise ValueError("version exists")
        rec = PolicyRecord(
            definition.tenant_id,
            definition.policy_id,
            definition.version,
            definition,
            definition.definition_hash(),
            PolicyState.CANDIDATE,
            now,
        )
        self.rows[key] = rec
        return rec

    def get_version(self, tenant_id: str, policy_id: str, version: int) -> PolicyRecord | None:
        return self.rows.get((tenant_id, policy_id, version))

    def list_versions(self, tenant_id: str, policy_id: str) -> list[PolicyRecord]:
        return sorted(
            (r for r in self.rows.values() if (r.tenant_id, r.policy_id) == (tenant_id, policy_id)),
            key=lambda r: r.version,
        )

    def add_evaluation(
        self, tenant_id: str, policy_id: str, version: int, report: StageReport, now: datetime
    ) -> str:
        ev = StageEvidence(f"e{len(self.evals)}", report, now)
        self.evals.append((tenant_id, policy_id, version, ev))
        return ev.evaluation_id

    def latest_evaluation(
        self, tenant_id: str, policy_id: str, version: int, stage: Stage
    ) -> StageEvidence | None:
        found = [
            e
            for t, p, v, e in self.evals
            if (t, p, v) == (tenant_id, policy_id, version) and e.report.stage == stage
        ]
        return found[-1] if found else None

    def apply_transitions(
        self,
        tenant_id: str,
        policy_id: str,
        transitions: Sequence[PolicyTransition],
        actor: str,
        reason: str,
        evaluation_id: str | None,
        now: datetime,
    ) -> None:
        import dataclasses

        staged = dict(self.rows)
        for t in transitions:
            rec = staged[(tenant_id, policy_id, t.version)]
            if rec.state != t.from_state:
                raise ValueError("stale transition")
            staged[(tenant_id, policy_id, t.version)] = dataclasses.replace(rec, state=t.to_state)
        for state in (PolicyState.ACTIVE, PolicyState.CANARY, PolicyState.SHADOW):
            n = sum(
                1
                for r in staged.values()
                if (r.tenant_id, r.policy_id) == (tenant_id, policy_id) and r.state == state
            )
            if n > 1:
                raise ValueError(f"two {state.value} versions")
        self.rows = staged
        for t in transitions:
            self.log.append(
                TransitionRecord(
                    policy_id,
                    t.version,
                    t.from_state,
                    t.to_state,
                    actor,
                    reason,
                    evaluation_id,
                    now,
                )
            )

    def history(self, tenant_id: str, policy_id: str) -> list[TransitionRecord]:
        return [h for h in self.log if h.policy_id == policy_id]


# ---- synthetic full-information worlds (labelled synthetic wherever they are used) -----------
from experience_engine.controller.simulate import TaskOutcome  # noqa: E402
from experience_engine.domain.controller import CandidateFeature  # noqa: E402
from experience_engine.domain.decision import Strategy  # noqa: E402


def world(
    n: int,
    rates: dict[str, float],
    cost: dict[str, int],
    *,
    family: str = "pool",
    risk: str = "low",
    seed: int = 0,
    prefix: str = "t",
    drift_at: int | None = None,
    drifted_rates: dict[str, float] | None = None,
) -> list[TaskOutcome]:
    rng = random.Random(seed)
    acts = [
        CandidateFeature(key=PLANNING_KEY, strategy=Strategy.GENERAL_PLANNING),
        CandidateFeature(key=CAP_KEY, strategy=Strategy.DIRECT_CAPABILITY, ref=CAP),
    ]
    out = []
    for i in range(n):
        r = drifted_rates if drift_at is not None and i >= drift_at and drifted_rates else rates
        oc = {
            k: (
                OutcomeStatus.VERIFIED_SUCCESS if rng.random() < r[k] else OutcomeStatus.FAILURE,
                max(1, int(rng.gauss(cost[k], cost[k] * 0.1))),
            )
            for k in (PLANNING_KEY, CAP_KEY)
        }
        out.append(TaskOutcome(f"{prefix}{i}", family, risk, acts, CAP_KEY, oc))  # type: ignore[arg-type]
    return out
