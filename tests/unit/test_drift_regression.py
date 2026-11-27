"""M6.2 regression guards: M6/M6.1 behaviour is unchanged with the monitor off, tenants never
leak into a monitor, and a drift overlay can never produce an unsafe choice."""

import hashlib
import json
import random
from pathlib import Path

import pytest
from test_controller_evidence import COST_STALE, RATES_STALE  # noqa: F401
from test_drift_controller import defn

from controller_support import (
    CAP,
    CAP_KEY,
    correlated_world,
    ctx,
    match_with,
    prior_of,
    task,
)
from experience_engine.controller.drift import MonitorFleet
from experience_engine.controller.policy import AdaptiveController
from experience_engine.controller.simulate import SimConfig, run_sequence
from experience_engine.domain.controller import (
    PLANNING_KEY,
    ControllerKind,
    ControllerParams,
    DecisionOutcomeRecord,
    paired_key,
)
from experience_engine.domain.drift import DriftCellStatus, DriftOverlay, DriftState
from experience_engine.domain.outcome import OutcomeStatus
from experience_engine.execution.controller import SELECTED_CAPABILITY_KEY

# Decision digests computed under the M6.1 code (commit 351e7a1) BEFORE any M6.2 change; they are
# the same under the current code, which is the regression proof for "monitor off == M6.1".
M61_DIGESTS = {
    "cost_aware_m6": "f8b917b443609cbebbb8856c39a1cc7733aef16269b170d34601d4fb02797c6f",
    "bandit_m6": "cefd9ec1f36a7fc8e52961a04a553d843db8dafdea14ff0aa38bad388a9af6cc",
    "evidence_prior": "39972c4995197e854c1871677a15f99d035ef302ee192bdf69930f6921eeae5d",
    "evidence_cold": "1a53d00802d2366c9f4cfbef6820553324511d6b11b2805a2e2d410a5fac9d2b",
}
WORLD_RATES = {PLANNING_KEY: 0.55, "DIRECT_CAPABILITY:pool.00000000aa@1": 0.55}
WORLD_COST = {PLANNING_KEY: 5000, "DIRECT_CAPABILITY:pool.00000000aa@1": 9000}


def digest(kind: ControllerKind, evidence: bool, prior: bool) -> str:
    world = correlated_world(500, WORLD_RATES, WORLD_COST, seed=2, prefix="w")
    params: dict[str, object] = {
        "exploration_risk_levels": ["low"],
        "exploration_families": ["pool"],
        "epsilon": 0.1,
        "min_total_verified": 0,
    }
    if evidence:
        params["evidence"] = {}
    pr = prior_of(correlated_world(240, WORLD_RATES, WORLD_COST, seed=1, prefix="v"))
    cfg = SimConfig(
        kind, ControllerParams.model_validate(params), prior=pr if prior else None, refresh_every=10
    )
    steps = run_sequence(world, list(range(500)), cfg, label="g")
    rows = [(s.chosen, s.status.value, s.tokens, s.mode, round(s.probability, 6)) for s in steps]
    return hashlib.sha256(json.dumps(rows).encode()).hexdigest()


@pytest.mark.parametrize(
    ("name", "kind", "evidence", "prior"),
    [
        ("cost_aware_m6", ControllerKind.COST_AWARE, False, False),
        ("bandit_m6", ControllerKind.BANDIT, False, False),
        ("evidence_prior", ControllerKind.EVIDENCE_DRIVEN, True, True),
        ("evidence_cold", ControllerKind.EVIDENCE_DRIVEN, True, False),
    ],
)
def test_with_the_monitor_off_every_m6_and_m61_decision_is_unchanged(
    name: str, kind: ControllerKind, evidence: bool, prior: bool
) -> None:
    assert digest(kind, evidence, prior) == M61_DIGESTS[name]


def rec(i: int, tenant: str, ok: bool) -> DecisionOutcomeRecord:
    return DecisionOutcomeRecord(
        decision_id=f"d-{i:05d}", run_id=f"r-{i}", tenant_id=tenant, policy_version="adaptive/1",
        selection_probability=1.0, family="pool", risk_level="low", action=PLANNING_KEY,
        status=OutcomeStatus.VERIFIED_SUCCESS if ok else OutcomeStatus.FAILURE, tokens=3000,
        default_key=CAP_KEY,
    )  # fmt: skip


def test_a_monitor_never_ingests_another_tenants_outcomes() -> None:
    from test_drift_monitor import P, ref

    r = ref(family="pool")
    r2 = type(r)(**{**r.__dict__, "default_key": CAP_KEY, "alt_key": PLANNING_KEY,
                    "cell_key": paired_key("pool", "low", CAP_KEY, PLANNING_KEY)})  # fmt: skip
    mine = MonitorFleet([r2], P, "bench")
    mine.ingest([rec(i, "other", False) for i in range(300)])
    assert mine.states[r2.cell_key].n_alt == 0 and not mine.transitions
    mine.ingest([rec(i, "bench", False) for i in range(300)])
    assert mine.states[r2.cell_key].n_alt == 300 and mine.transitions
    open_fleet = MonitorFleet([r2], P)  # unscoped (single-tenant simulation only)
    open_fleet.ingest([rec(i, "any", False) for i in range(300)])
    assert open_fleet.states[r2.cell_key].n_alt == 300


def test_no_overlay_state_can_produce_an_unsafe_or_infeasible_choice() -> None:
    d = defn("ENFORCE")
    rng = random.Random(4)
    key = paired_key("pool", "low", CAP_KEY, PLANNING_KEY)
    for i in range(400):
        state = rng.choice(list(DriftState))
        ov = DriftOverlay(
            cells={key: DriftCellStatus(state=state, seq=i, probes_remaining=rng.randint(0, 50))}
        )
        risk = rng.choice(["low", "medium", "high"])
        family = rng.choice(["pool", "timeout"])
        dec = AdaptiveController(d, ov).decide(ctx(task(risk, family), match_with(CAP), f"e-{i}"))
        feasible = {"GENERAL_PLANNING", CAP_KEY}
        ref = dec.feature_snapshot.get(SELECTED_CAPABILITY_KEY)
        chosen = (
            PLANNING_KEY
            if dec.selected_action.value == "GENERAL_PLANNING"
            else f"DIRECT_CAPABILITY:{ref}"
        )
        assert chosen in feasible
        if (
            risk != "low" or family != "pool"
        ):  # not allow-listed: never anything but the greedy rule
            assert (dec.selection_probability or 1.0) == 1.0
        if risk == "high":
            assert chosen == CAP_KEY  # protected: the rule baseline, whatever the overlay says


def test_the_drift_overlay_can_only_restrict_never_enlarge_the_alternative_set() -> None:
    # with the prior disabled the controller can pick the default or (bounded probes) the
    # alternatives it already considered feasible; it never invents an action or a capability
    d = defn("ENFORCE")
    key = paired_key("pool", "low", CAP_KEY, PLANNING_KEY)
    for state in (DriftState.QUARANTINED, DriftState.RECOVERING):
        ov = DriftOverlay(cells={key: DriftCellStatus(state=state, seq=1, probes_remaining=5)})
        seen = {
            AdaptiveController(d, ov)
            .decide(ctx(task("low", "pool"), match_with(CAP), f"e-{i}"))
            .selected_action
            for i in range(300)
        }
        assert {s.value for s in seen} <= {"GENERAL_PLANNING", "DIRECT_CAPABILITY"}


def test_the_synthetic_scenarios_and_perturbations_are_deterministic() -> None:
    from experience_engine.controller.scenarios import SCENARIOS, build_stream

    a, b = build_stream(SCENARIOS["S5"], 99), build_stream(SCENARIOS["S5"], 99)
    assert [t.outcomes for t in a.tasks[:50]] == [t.outcomes for t in b.tasks[:50]]
    assert a.prior.cells == b.prior.cells and a.material == b.material
    other = build_stream(SCENARIOS["S5"], 100)
    assert [t.outcomes for t in other.tasks[:50]] != [t.outcomes for t in a.tasks[:50]]


def test_drift_report_renders_acceptance_and_run_sensitivity(tmp_path: Path) -> None:
    """drift-sim -> drift-report round trip on one seed (verdicts are meaningless at this size; the
    point is that the report is generated from stored runs and exposes how (a)-(c) depend on the
    run that measured them)."""
    from experience_engine.controller_cli import main

    loop, mon, out = (tmp_path / n for n in ("loop.json", "mon.json", "acc.md"))
    base = ["drift-sim", "--seeds", "1", "--workers", "1", "--seed-start", "5000000"]
    assert main([*base, "--out", str(tmp_path / "l.md"), "--json-out", str(loop)]) == 0
    assert (
        main([*base, "--variants", "monitor", "--out", str(tmp_path / "m.md"),
              "--json-out", str(mon)]) == 0
    )  # fmt: skip
    assert (
        main(["drift-report", "--loop", str(loop), "--monitor", str(mon), "--out", str(out)]) == 0
    )
    text = out.read_text()
    assert "Sensitivity: criteria (a)-(c)" in text
    for label in ("monitor-only (used)", "in-loop SHADOW", "in-loop ENFORCE"):
        assert label in text
