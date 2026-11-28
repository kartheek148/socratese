"""M6.4 Phase 1 - deterministic persistence and replay of the outage, suspension, evidence-window
and gate semantics: a replay from the persisted evidence and configuration alone reproduces
every transition, accounting figure and gate result, never reads the clock, detects tampering,
and an interrupted round leaves the state untouched."""

import random
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from test_drift_monitor import P, rec, ref
from test_revalidation_core import ALT, T0, F, S, U, hours, new_fleet

from experience_engine.controller import recovery_gates, revalidation
from experience_engine.controller.revalidation import (
    ReplaySource,
    RevalidationFleet,
    replay_revalidation,
)
from experience_engine.controller.revalidation_service import (
    RevalidationReplayMismatch,
    verify_transitions,
)
from experience_engine.domain.drift import DriftMode
from experience_engine.domain.recovery import GateReport
from experience_engine.domain.revalidation import RevalState, RevalStrategy, RevalTrigger
from recovery_support import POLICY, AttestedScript

FIRST_ROUND = 200


class World:
    """The schedule of one live run: which rounds the verifier is reported down and which rounds'
    attempts come back without a verdict (an outage discovered only through the attempts)."""

    def __init__(self) -> None:
        self.down = False
        self.attempts_fail = False

    def verdict(self, n: int) -> Any:
        return U if self.attempts_fail else (S if n % 11 else F)


def schedule(k: int, w: World) -> None:
    w.attempts_fail = k == 2  # round 2: three attempts without a verdict open the breaker
    w.down = k in (3, 5, 6)  # unhealthy probes; round 4 is one healthy probe (the count resets)
    # rounds 7 and 8 are healthy probes: round 8 resumes and evidence collection goes on


def live(
    strategy: RevalStrategy = RevalStrategy.FIXED, rounds: int = 40, gap: float = 5.0
) -> tuple[RevalidationFleet, list[Any]]:
    fleet = new_fleet(strategy, dwell=10, recovery=POLICY, confirm_gap_hours=gap)
    records = [rec(i, "timeout", ALT, random.Random(9 + i).random() < 0.05) for i in range(150)]
    records += [rec(150 + i, "timeout", "DIRECT_CAPABILITY:cap@1", i % 2 == 0) for i in range(40)]
    fleet.ingest(records)
    world = World()
    script = AttestedScript(world.verdict, down=lambda _now: world.down)
    for k in range(rounds):
        schedule(k, world)
        fleet.step(script, records[-1].decision_id, T0 + hours(FIRST_ROUND + 10 * k))
    return fleet, records


def replayed(fleet: RevalidationFleet, records: list[Any], trials: Any = None) -> RevalidationFleet:
    drift = P.model_copy(update={"mode": DriftMode.ENFORCE})
    return replay_revalidation(
        [ref(0.5)], drift, fleet.params, "bench", records, fleet.transitions,
        fleet.trials if trials is None else trials,
    )  # fmt: skip


def seq(fleet: RevalidationFleet) -> list[str]:
    return [t.trigger.value for t in fleet.transitions]


# ---- the live run exercises every new transition ----------------------------------------------
@pytest.fixture(scope="module")
def run() -> tuple[RevalidationFleet, list[Any]]:
    return live()


def test_the_live_run_goes_through_suspension_probes_resume_and_a_gated_recovery(
    run: tuple[RevalidationFleet, list[Any]],
) -> None:
    fleet, _ = run
    t = iter(seq(fleet))
    for expected in ("SUSPENDED", "OUTAGE_PROBE", "RESUMED", "CRITERIA_MET", "CONFIRMED"):
        assert expected in t, expected  # in this order (each search continues after the last hit)
    case = fleet.cases[next(iter(fleet.cases))]
    assert case.state == RevalState.RECOVERED and case.cycle == 0  # the outage cost no cycle
    done = fleet.transitions[-1]
    assert done.trigger == RevalTrigger.CONFIRMED
    report = GateReport.model_validate(done.gates)
    assert report.eligible and len(report.evidence_ids) == len(
        [x for x in fleet.trials if x.accepted and x.result.counted]
    )
    probes = [x for x in fleet.transitions if x.trigger == RevalTrigger.OUTAGE_PROBE]
    assert [p.measurements["healthy"] for p in probes] == [1.0, 0.0, 1.0]  # H, U (reset), H
    assert not fleet.monitor.states[done.cell_key].locked  # and only now the cell is released


def test_a_replay_reproduces_every_transition_trial_and_accounting_figure(
    run: tuple[RevalidationFleet, list[Any]],
) -> None:
    fleet, records = run
    again = replayed(fleet, records)
    assert [t.model_dump() for t in again.transitions] == [
        t.model_dump() for t in fleet.transitions
    ]
    assert [t.key.ident() for t in again.trials] == [t.key.ident() for t in fleet.trials]
    assert again.cases == fleet.cases  # outage counters, ledgers, evidence, suspension clocks
    assert again.spent == fleet.spent
    assert again.monitor.states == fleet.monitor.states
    assert again.overlay().overlay_hash() == fleet.overlay().overlay_hash()
    for a, b in zip(again.transitions, fleet.transitions, strict=True):
        assert a.gates == b.gates and a.measurements == b.measurements and a.facts == b.facts


def test_repeated_replays_are_identical_and_never_read_the_clock(
    run: tuple[RevalidationFleet, list[Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    fleet, records = run

    class NoClock(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> "NoClock":
            raise AssertionError("replay read the wall clock")

        @classmethod
        def utcnow(cls) -> "NoClock":
            raise AssertionError("replay read the wall clock")

    monkeypatch.setattr(revalidation, "datetime", NoClock)
    monkeypatch.setattr(recovery_gates, "datetime", NoClock)
    runs = [replayed(fleet, records) for _ in range(3)]
    dumps = [[t.model_dump_json() for t in r.transitions] for r in runs]
    assert dumps[0] == dumps[1] == dumps[2] == [t.model_dump_json() for t in fleet.transitions]


def test_no_replay_relevant_module_reads_the_wall_clock() -> None:
    root = Path(revalidation.__file__).parents[1]
    for name in ("controller/revalidation.py", "controller/recovery_gates.py",
                 "domain/recovery.py", "domain/revalidation.py"):  # fmt: skip
        text = (root / name).read_text()
        assert not re.search(r"datetime\.(now|utcnow|today)\(|time\.(time|monotonic)\(", text), name


def test_the_replay_source_serves_the_recorded_availability_of_each_round(
    run: tuple[RevalidationFleet, list[Any]],
) -> None:
    fleet, _ = run
    src = ReplaySource(fleet.trials, fleet.transitions, "bench")
    probes = [x for x in fleet.transitions if x.trigger == RevalTrigger.OUTAGE_PROBE]
    for p in probes:
        assert p.observed_at is not None
        got = src.facts(ref(0.5), p.observed_at)
        assert got.verification_available == bool(p.measurements["healthy"])
    assert src.facts(ref(0.5), T0 - hours(1)).verification_available  # nothing known: default


@pytest.mark.parametrize("strategy", list(RevalStrategy))
def test_every_strategy_replays_with_outages_and_gates(strategy: RevalStrategy) -> None:
    fleet, records = live(strategy)
    assert RevalState.RECOVERED in {t.to_state for t in fleet.transitions}
    again = replayed(fleet, records)
    assert [t.model_dump() for t in again.transitions] == [
        t.model_dump() for t in fleet.transitions
    ]
    assert again.cases == fleet.cases


# ---- tampering, truncation and interruption ------------------------------------------------------
def test_tampered_evidence_changes_the_replayed_decision_and_is_detected(
    run: tuple[RevalidationFleet, list[Any]],
) -> None:
    fleet, records = run
    tampered = [
        t.model_copy(update={"result": t.result.model_copy(update={
            "provenance": t.result.provenance.model_copy(update={"executor_id": "rogue"})})})
        if t.key.stage.value == "CONFIRMATION" else t
        for t in fleet.trials
    ]  # fmt: skip
    again = replayed(fleet, records, tampered)
    assert RevalState.RECOVERED not in {t.to_state for t in again.transitions}
    assert any(t.trigger == RevalTrigger.GATE_FAILED for t in again.transitions)
    with pytest.raises(RevalidationReplayMismatch):
        verify_transitions(fleet.transitions, again.transitions)  # the stored RECOVERED is refused


def test_a_removed_trial_stops_the_replay_where_the_evidence_stops(
    run: tuple[RevalidationFleet, list[Any]],
) -> None:
    fleet, records = run
    cut = [
        t for t in fleet.trials if not (t.key.stage.value == "CONFIRMATION" and t.key.ordinal < 5)
    ]
    again = replayed(fleet, records, cut)
    assert RevalState.RECOVERED not in {t.to_state for t in again.transitions}


def test_an_interrupted_round_leaves_the_persisted_state_untouched_and_resumable() -> None:
    fleet, records = live(rounds=2)  # healthy collection under way
    before = (
        [t.model_dump_json() for t in fleet.transitions], list(fleet.trials), dict(fleet.cases),
        dict(fleet.spent),
    )  # fmt: skip

    class Crash(AttestedScript):
        def run(self, *a: Any, **k: Any) -> Any:
            if self.calls == 7:
                raise RuntimeError("the worker was killed mid-round")
            return super().run(*a, **k)

    with pytest.raises(RuntimeError):
        fleet.step(Crash(S), records[-1].decision_id, T0 + hours(FIRST_ROUND + 20))
    after = (
        [t.model_dump_json() for t in fleet.transitions], list(fleet.trials), dict(fleet.cases),
        dict(fleet.spent),
    )  # fmt: skip
    assert after == before  # nothing half-written: neither trials, transitions nor counters
    control, _ = live(rounds=2)
    for f in (fleet, control):  # and the next healthy round proceeds exactly as if nothing happened
        f.step(AttestedScript(S), records[-1].decision_id, T0 + hours(FIRST_ROUND + 20))
    assert [t.model_dump_json() for t in fleet.transitions] == [
        t.model_dump_json() for t in control.transitions
    ]
    assert fleet.cases == control.cases


def test_a_run_where_the_outage_outlasts_the_evidence_replays_identically_too() -> None:
    """With the frozen 20 h confirmation gap the pre-outage evidence leaves the 120 h window before
    the confirmation can finish: it is discarded (no cycle consumed), re-collected, and the
    attempt budget - which expiry cannot launder - ends the cycle."""
    fleet, records = live(gap=20.0)
    t = seq(fleet)
    assert "EVIDENCE_EXPIRED" in t and t[-1] == "TRIAL_CAP"
    case = fleet.cases[next(iter(fleet.cases))]
    assert case.cycle == 0 and case.expired_evidence == 1
    assert case.state == RevalState.BUDGET_EXHAUSTED
    again = replayed(fleet, records)
    assert [x.model_dump() for x in again.transitions] == [
        x.model_dump() for x in fleet.transitions
    ]
    assert again.cases == fleet.cases


def test_the_status_listing_shows_the_gate_report_of_a_decision(
    run: tuple[RevalidationFleet, list[Any]], capsys: pytest.CaptureFixture[str]
) -> None:
    from experience_engine.revalidation_commands import _print

    fleet, _ = run
    done = fleet.transitions[-1]
    report = {**done.gates, "eligible": False, "failed_gates": ["evidence_freshness"]}
    refused = done.model_copy(update={"gates": report})
    _print([done, refused])
    lines = capsys.readouterr().out.splitlines()
    assert "gates=ok[m6.4-gates-v1]" in lines[0]
    assert "gates=FAILED:evidence_freshness[m6.4-gates-v1]" in lines[1]
