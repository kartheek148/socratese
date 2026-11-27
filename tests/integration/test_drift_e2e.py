"""M6.2 integration (real PostgreSQL): drift transitions are persisted, append-only, tenant
scoped and reproducible; SHADOW never alters execution; ENFORCE falls back to the rule baseline and
its stored decisions replay; the ee-ctl commands work."""

from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy import Engine
from sqlalchemy.exc import DBAPIError
from test_controller_e2e import REAL_CAP, pool_tasks, run_tasks

from controller_support import PLANNING_KEY
from experience_engine import RUNTIME_VERSION, controller_cli
from experience_engine.adapters.clock import SystemClock
from experience_engine.adapters.db import tables as t
from experience_engine.adapters.db.controller_store import SqlControllerPolicyStore, SqlDecisionLog
from experience_engine.adapters.db.drift_store import SqlDriftLog
from experience_engine.bench.verifier import SimulationVerifier
from experience_engine.controller.drift_service import (
    DriftReplayMismatch,
    DriftService,
    shadow_assessments,
    verify_prefix,
)
from experience_engine.controller.policy import AdaptiveController
from experience_engine.controller.replay import replay_decision
from experience_engine.domain.controller import (
    CONTROLLER_KEY,
    ControllerKind,
    ControllerParams,
    ControllerPolicyDefinition,
    DecisionFeatures,
    EstimatorSnapshot,
    PairedCell,
    PairedSource,
    paired_key,
)
from experience_engine.domain.drift import DriftState, DriftTransition
from experience_engine.domain.outcome import VerificationResult
from experience_engine.execution.controller import RuleCapabilityPolicy

pytestmark = pytest.mark.integration
TENANT = "bench"
DEFAULT = REAL_CAP
KEY = paired_key("pool", "low", DEFAULT, PLANNING_KEY)
CAP_REF = REAL_CAP.split(":", 1)[1]


class FlakyVerifier:
    """The simulation verifier, or a forced failure once `fail` is set."""

    version = SimulationVerifier.version

    def __init__(self) -> None:
        self.inner = SimulationVerifier()
        self.fail = False

    def verify(self, task: Any, snapshot: Any) -> list[VerificationResult]:
        if self.fail:
            return [
                VerificationResult(criterion_kind="forced", role="goal", passed=False, detail="x")
            ]
        return self.inner.verify(task, snapshot)


def drift_definition(version: int, mode: str, tenant: str = TENANT) -> ControllerPolicyDefinition:
    cell = PairedCell(
        n=240, both=90, default_only=30, alt_only=30, neither=90,
        default_tokens=240 * 9000, alt_tokens=240 * 3000,
    )  # fmt: skip
    source = PairedSource(
        name="evaluation:test", arms=["A_BASELINE", "E_SERVING"], splits=["HELD_OUT", "TRANSFER"],
        dataset_hashes={}, model="m", runtime_version=RUNTIME_VERSION,
        verifier_version=SimulationVerifier.version, n_pairs=240, n_unknown_dropped=0,
        n_tied_dropped=0, tasks_sha256="t" * 64, validation_tokens=1, capabilities=[CAP_REF],
        evidence_kind="PROMOTION_EVALUATION", production_eligible=True, tenant_id=tenant,
        risk_class="low", n_missing_cost=0,
    )  # fmt: skip
    return ControllerPolicyDefinition(
        policy_id="adaptive",
        version=version,
        kind=ControllerKind.EVIDENCE_DRIVEN,
        params=ControllerParams.model_validate(
            {
                "evidence": {},
                "drift": {
                    "mode": mode,
                    "cost_tolerance": 1000.0,  # the fake model's token counts are not under test
                    "utility_tolerance": 1000.0,
                    "min_quarantine": 5,
                },
            }
        ),
        snapshot=EstimatorSnapshot(tenant_id=tenant, paired={KEY: cell}, prior_sources=[source]),
    )


def service(engine: Engine) -> DriftService:
    log = SqlDecisionLog(engine)
    return DriftService(
        lambda tenant, versions: log.outcome_records(tenant, policy_versions=versions),
        SqlDriftLog(engine),
        SystemClock().now,
    )


def tasks(n: int) -> list[Any]:
    base = pool_tasks(6)
    return [base[i % len(base)] for i in range(n)]


def run(engine: Engine, tmp_path: Path, d: ControllerPolicyDefinition, n: int, verifier: Any,
        overlay: Any = None) -> list[Any]:  # fmt: skip
    import test_controller_e2e as base

    original = base.SimulationVerifier
    base.SimulationVerifier = lambda: verifier  # type: ignore[assignment,misc]
    try:
        return run_tasks(engine, tmp_path, AdaptiveController(d, overlay), tasks(n))
    finally:
        base.SimulationVerifier = original  # type: ignore[misc]


@pytest.fixture
def registered(engine: Engine) -> Any:
    store = SqlControllerPolicyStore(engine)

    def add(d: ControllerPolicyDefinition) -> ControllerPolicyDefinition:
        store.add_version(d, SystemClock().now())
        return d

    return add


def test_shadow_monitor_persists_an_auditable_quarantine_and_never_alters_execution(
    engine: Engine, tmp_path: Path, registered: Any
) -> None:
    d = registered(drift_definition(1, "SHADOW"))
    v = FlakyVerifier()
    healthy = run(engine, tmp_path, d, 30, v)
    assert all(r.outcome.capability_version is None for r in healthy)  # the prior plans
    assert not service(engine).sync(d).new  # healthy: nothing to record
    v.fail = True
    degraded = run(engine, tmp_path, d, 60, v)
    # shadow: execution is untouched by the monitor (the controller never even read an overlay)
    assert all(r.outcome.capability_version is None for r in degraded)
    res = service(engine).sync(d)
    path = [(x.from_state, x.to_state) for x in res.new]
    assert (DriftState.SUSPECT, DriftState.DRIFTED) in path
    assert (DriftState.DRIFTED, DriftState.QUARANTINED) in path
    assert res.overlay is None  # SHADOW publishes no overlay
    stored = SqlDriftLog(engine).transitions(TENANT, "adaptive", 1)
    assert [x.signature() for x in stored] == [x.signature() for x in res.new]
    assert all(not x.applied and x.config_hash == d.params.drift.config_hash() for x in stored)  # type: ignore[union-attr]
    # idempotent and reproducible
    assert service(engine).sync(d).new == []
    again = service(engine).replay(d)
    assert [x.signature() for x in again.fleet.transitions] == [x.signature() for x in stored]  # type: ignore[union-attr]
    # hindsight: the alternative kept failing, so the recommendation is confirmed
    with engine.connect() as conn:
        assert conn.execute(
            sa.select(sa.func.count()).select_from(t.controller_drift_transitions)
        ).scalar_one() == len(stored)
    recs = SqlDecisionLog(engine).outcome_records(TENANT, policy_versions=["adaptive/1"])
    rows = shadow_assessments(d, stored, recs)
    assert rows and rows[0].verdict in ("CONFIRMED", "PENDING")


def test_transitions_are_append_only_tenant_scoped_and_checked_against_the_log(
    engine: Engine, tmp_path: Path, registered: Any
) -> None:
    d = registered(drift_definition(1, "SHADOW"))
    v = FlakyVerifier()
    v.fail = True
    run(engine, tmp_path, d, 60, v)
    service(engine).sync(d)
    with engine.begin() as conn, pytest.raises(DBAPIError):
        conn.execute(sa.update(t.controller_drift_transitions).values(trigger="X"))
    with engine.begin() as conn, pytest.raises(DBAPIError):
        conn.execute(sa.delete(t.controller_drift_transitions))
    assert SqlDriftLog(engine).transitions("other-tenant", "adaptive", 1) == []
    persisted = SqlDriftLog(engine).transitions(TENANT, "adaptive", 1)
    forged = [
        persisted[0].model_copy(
            update={"trigger": persisted[0].trigger.__class__("COST_DEGRADATION")}
        )
    ]
    with pytest.raises(DriftReplayMismatch):
        verify_prefix(forged, persisted)
    with pytest.raises(DriftReplayMismatch):
        verify_prefix(persisted, persisted[:1])  # more persisted than recomputed
    # a transition for a policy version that does not exist is refused by the foreign key
    with pytest.raises(Exception):  # noqa: B017
        SqlDriftLog(engine).append(TENANT, "adaptive", 99, [persisted[0]], SystemClock().now(), {})


def test_enforce_falls_back_to_the_rule_baseline_and_stored_decisions_replay(
    engine: Engine, tmp_path: Path, registered: Any
) -> None:
    d = registered(drift_definition(1, "ENFORCE"))
    svc = service(engine)
    v = FlakyVerifier()
    run(engine, tmp_path, d, 30, v)  # healthy: the prior plans
    v.fail = True
    run(engine, tmp_path, d, 60, v)  # degraded alternative, still executed (overlay not yet read)
    res = svc.sync(d)
    assert res.overlay is not None and res.overlay.cells[KEY].state in (
        DriftState.QUARANTINED,
        DriftState.RECOVERING,
    )
    v.fail = False
    after = run(engine, tmp_path, d, 6, v, overlay=res.overlay)
    assert all(r.outcome.capability_version == CAP_REF for r in after)  # the rule default serves
    assert all(r.decision.acted for r in after)
    for r in after:
        check = replay_decision(r.decision, d)
        assert check.reproducible, check.differences
    stored = SqlDecisionLog(engine).decisions(TENANT, "adaptive/1", acted=True)
    views = [
        DecisionFeatures.model_validate(x.feature_snapshot[CONTROLLER_KEY]).drift for x in stored
    ]
    assert any(v and v[0].state in (DriftState.QUARANTINED, DriftState.RECOVERING) for v in views)
    assert all(x.applied for x in SqlDriftLog(engine).transitions(TENANT, "adaptive", 1))


def test_ee_ctl_drift_and_evidence_commands(
    engine: Engine, tmp_path: Path, registered: Any, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    monkeypatch.setenv("EE_DATABASE_URL", engine.url.render_as_string(hide_password=False))
    monkeypatch.setenv("EE_ARTIFACT_ROOT", str(tmp_path / "a"))
    d = registered(drift_definition(1, "SHADOW"))
    v = FlakyVerifier()
    v.fail = True
    run(engine, tmp_path, d, 60, v)

    def cli(*argv: str) -> tuple[int, str]:
        capsys.readouterr()
        code = controller_cli.main(list(argv))
        return code, capsys.readouterr().out

    code, out = cli("drift-evaluate", "--version", "1")
    assert code == 0 and "QUARANTINED" in out and "new transition(s) persisted" in out
    code, out = cli("drift-status", "--version", "1")
    assert code == 0 and "QUARANTINED" in out and "mode=SHADOW applied=False" in out
    code, out = cli("drift-replay", "--version", "1")
    assert code == 0 and "reproduce exactly" in out
    code, out = cli("drift-shadow-report", "--version", "1")
    assert code == 0 and "fallback recommendation" in out
    code, out = cli("evidence-inspect", "--version", "1")
    assert '"eligible"' in out and "QUARANTINED" in out and "drift_state" in out
    assert code == 1  # a quarantined cell is not eligible evidence
    with pytest.raises(SystemExit, match="adaptive/7 not found"):
        cli("drift-evaluate", "--version", "7")


def test_rule_baseline_rows_do_not_become_observations(
    engine: Engine, tmp_path: Path, registered: Any
) -> None:
    d = registered(drift_definition(1, "SHADOW"))
    run_tasks(engine, tmp_path, RuleCapabilityPolicy(), tasks(30))  # decisions of rule-capability/1
    assert service(engine).replay(d).n_observations == 0
    assert isinstance(DriftTransition, type)
