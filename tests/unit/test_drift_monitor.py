"""M6.2 drift monitor: windows, channels, state machine, hysteresis, recovery, replay, isolation."""

import random
from datetime import UTC, datetime, timedelta

import pytest

from experience_engine.controller.drift import (
    CellReference,
    MonitorFleet,
    MonitorState,
    Observation,
    advance,
    evaluate,
    parse_paired_key,
    recovery_ok,
    reference_of,
    replay,
)
from experience_engine.domain.controller import (
    DecisionOutcomeRecord,
    PairedCell,
    PairedSource,
    paired_key,
)
from experience_engine.domain.drift import (
    PRIOR_DISABLED,
    Detector,
    DriftMode,
    DriftParams,
    DriftState,
    DriftTrigger,
)
from experience_engine.domain.outcome import OutcomeStatus

DEF = "DIRECT_CAPABILITY:cap@1"
ALT = "GENERAL_PLANNING"
T0 = datetime(2026, 1, 1, tzinfo=UTC)
P = DriftParams()  # the frozen configuration


def ref(
    p_alt: float = 0.5,
    p_def: float = 0.5,
    n: int = 240,
    tok_alt: int = 6000,
    tok_def: int = 9000,
    family: str | None = "timeout",
    **kw: object,
) -> CellReference:
    k_alt, k_def = round(p_alt * n), round(p_def * n)
    return CellReference(
        cell_key=paired_key(family, "low", DEF, ALT),
        family=family,
        risk="low",
        default_key=DEF,
        alt_key=ALT,
        n=n,
        k_alt=k_alt,
        k_def=k_def,
        tokens_alt=tok_alt * n,
        tokens_def=tok_def * n,
        runtime_version="rt-1",
        verifier_version="v-1",
        recorded_at=T0,
        **kw,  # type: ignore[arg-type]
    )


def obs(i: int, success: bool, tokens: int = 6000, action: str = ALT, **kw: object) -> Observation:
    return Observation(
        decision_id=f"d-{i:05d}",
        action=action,
        known=True,
        success=success,
        tokens=tokens,
        at=T0 + timedelta(hours=i),
        runtime_version=str(kw.pop("runtime_version", "rt-1")),
        verifier_version="v-1",
        **kw,  # type: ignore[arg-type]
    )


def stream(n: int, p: float, seed: int, start: int = 0, tokens: int = 6000) -> list[Observation]:
    rng = random.Random(seed)
    return [obs(start + i, rng.random() < p, tokens) for i in range(n)]


def states(trs: list) -> list[tuple[str, str]]:  # type: ignore[type-arg]
    return [(t.from_state.value, t.to_state.value) for t in trs]


# ---- window statistics and channels -----------------------------------------------------------
def test_a_stable_window_raises_nothing_and_a_collapsed_one_alarms() -> None:
    r = ref()
    ok = evaluate(r, P, [(i % 2, 6000, True) for i in range(40)], 0.0, T0)
    assert ok.level == 0 and ok.trigger is None
    bad = evaluate(r, P, [(1 if i % 6 == 0 else 0, 6000, True) for i in range(40)], 0.0, T0)
    assert bad.level == 2 and bad.trigger == DriftTrigger.SUCCESS_DEGRADATION
    assert bad.measurements["success_diff_ub"] < -P.success_tolerance


def test_small_samples_never_raise_a_statistical_alarm() -> None:
    r = ref()
    ev = evaluate(r, P, [(0, 6000, True)] * (P.min_samples - 1), 0.0, T0)
    assert ev.level == 0 and "success_rate" not in ev.measurements  # nothing computed yet
    full = evaluate(r, P, [(0, 6000, True)] * P.min_samples, 0.0, T0)
    assert full.level == 2  # twenty straight failures against a 50% reference


def test_cost_only_degradation_is_detected_through_cost_and_utility() -> None:
    r = ref()
    win = [(i % 2, 9500, True) for i in range(40)]  # same success, +58% tokens
    ev = evaluate(r, P, win, 0.0, T0)
    assert ev.levels[DriftTrigger.COST_DEGRADATION] == 2
    # utility carries the success-sampling noise as well, so it is the weaker channel here
    assert ev.levels[DriftTrigger.UTILITY_DEGRADATION] >= 1
    assert ev.levels[DriftTrigger.SUCCESS_DEGRADATION] == 0
    mild = evaluate(r, P, [(i % 2, 6600, True) for i in range(40)], 0.0, T0)  # +10%: tolerated
    assert mild.levels[DriftTrigger.COST_DEGRADATION] < 2 and mild.level <= 1  # at most suspect


def test_utility_alarm_fires_when_the_alternative_is_dearer_per_success_than_the_default() -> None:
    r = ref(p_alt=0.9, p_def=0.9, tok_alt=6000, tok_def=6500)  # a thin saving
    win = [(1, 8500, True)] * 40  # every run succeeds but costs 42% more than the reference
    ev = evaluate(r, P, win, 0.0, T0)
    assert ev.levels[DriftTrigger.UTILITY_DEGRADATION] == 2
    assert ev.measurements["saving_gone_lb"] > 0  # dearer than the default, with confidence
    cheap = evaluate(r, P, [(1, 6000, True)] * 40, 0.0, T0)
    assert cheap.level == 0 and cheap.measurements["saving_gone_lb"] < 0


def test_context_mismatch_and_staleness_are_structural_and_need_no_sample() -> None:
    r = ref()
    win = [(1, 6000, False)] * 5
    ev = evaluate(r, P, win, 0.0, T0)
    assert (
        ev.levels[DriftTrigger.CONTEXT_MISMATCH] == 2
        and ev.trigger == DriftTrigger.CONTEXT_MISMATCH
    )
    stale = P.model_copy(update={"max_evidence_age_days": 10.0})
    ev2 = evaluate(r, stale, [(1, 6000, True)], 0.0, T0 + timedelta(days=11))
    assert ev2.levels[DriftTrigger.EVIDENCE_STALE] == 2 and ev2.level == 2
    assert evaluate(r, stale, [(1, 6000, True)], 0.0, T0 + timedelta(days=9)).level == 0


def test_the_reference_uncertainty_is_part_of_the_bound() -> None:
    small = ref(n=30, p_alt=0.5)  # a thin prior cannot support a confident alarm
    big = ref(n=2000, p_alt=0.5)
    win = [(1 if i % 3 == 0 else 0, 6000, True) for i in range(40)]  # 33% vs 50%
    assert evaluate(small, P, win, 0.0, T0).level <= evaluate(big, P, win, 0.0, T0).level


# ---- state machine ------------------------------------------------------------------------------
def test_sustained_degradation_walks_normal_suspect_drifted_quarantined() -> None:
    r = ref()
    seq = stream(60, 0.5, 1) + stream(80, 0.1, 2, start=60)
    st, trs = replay(r, P, seq)
    path = states(trs)
    assert path[0] == ("NORMAL", "SUSPECT")
    assert ("SUSPECT", "DRIFTED") in path and ("DRIFTED", "QUARANTINED") in path
    assert path.index(("SUSPECT", "DRIFTED")) + 1 == path.index(("DRIFTED", "QUARANTINED"))
    assert st.state == DriftState.QUARANTINED and st.episode == 1
    drifted = next(t for t in trs if t.to_state == DriftState.DRIFTED)
    assert drifted.trigger == DriftTrigger.SUCCESS_DEGRADATION and drifted.measurements["m"] >= 20
    assert [t.seq for t in trs] == list(range(1, len(trs) + 1))


def test_persistence_blocks_a_short_burst() -> None:
    r = ref()
    burst = stream(60, 0.5, 3) + [obs(60 + i, False) for i in range(6)] + stream(60, 0.5, 4, 66)
    st, trs = replay(r, P, burst)
    assert not any(t.to_state == DriftState.DRIFTED for t in trs)
    assert st.state in (DriftState.NORMAL, DriftState.SUSPECT)


def test_suspect_clears_only_after_sustained_normal_looks_and_does_not_flap() -> None:
    r = ref()
    # a block of bad luck creates SUSPECT, a long normal stretch clears it
    seq = stream(40, 0.5, 5) + stream(15, 0.15, 6, 40) + stream(200, 0.5, 7, 55)
    _, trs = replay(r, P, seq)
    path = states(trs)
    assert ("NORMAL", "SUSPECT") in path
    if ("SUSPECT", "NORMAL") in path:
        i = path.index(("SUSPECT", "NORMAL"))
        assert trs[i].trigger == DriftTrigger.SIGNAL_CLEARED
    flips = sum(
        1
        for a, b in zip(path, path[1:], strict=False)
        if a == ("NORMAL", "SUSPECT") and b == ("SUSPECT", "NORMAL")
    )
    assert flips <= 2


def test_drifted_never_returns_to_normal_directly_and_all_transitions_are_adjacent() -> None:
    r = ref()
    allowed = {
        ("NORMAL", "SUSPECT"),
        ("SUSPECT", "NORMAL"),
        ("SUSPECT", "DRIFTED"),
        ("DRIFTED", "QUARANTINED"),
        ("QUARANTINED", "RECOVERING"),
        ("RECOVERING", "NORMAL"),
        ("RECOVERING", "QUARANTINED"),
    }
    rng = random.Random(11)
    for _seed in range(30):
        seq = [
            obs(
                i,
                rng.random() < (0.5 if (i // 150) % 2 == 0 else 0.1),
                action=ALT if rng.random() < 0.6 else DEF,
            )
            for i in range(900)
        ]
        _, trs = replay(r, P, seq)
        assert all((t.from_state.value, t.to_state.value) in allowed for t in trs)


def _drive_to_quarantine(r: CellReference, params: DriftParams = P) -> tuple[MonitorState, int]:
    st = MonitorState()
    i = 0
    for o in stream(60, 0.5, 1) + stream(80, 0.05, 2, 60):
        st, _ = advance(r, params, st, o)
        i += 1
        if st.state == DriftState.QUARANTINED:
            break
    assert st.state == DriftState.QUARANTINED
    return st, i


def test_quarantine_waits_then_recovers_only_with_fresh_probes_and_stays_auditable() -> None:
    r = ref()
    st, i = _drive_to_quarantine(r)
    trs = []
    # default-served decisions advance the quarantine clock; the prior stays disabled meanwhile
    for k in range(P.min_quarantine - 1):
        st, t = advance(r, P, st, obs(i + k, True, 9000, action=DEF))
        trs += t
    assert st.state == DriftState.QUARANTINED and not trs
    st, t = advance(r, P, st, obs(i + 99, True, 9000, action=DEF))
    assert [x.to_state for x in t] == [DriftState.RECOVERING]
    assert t[0].trigger == DriftTrigger.QUARANTINE_ELAPSED
    # one good probe is not recovery
    st, t = advance(r, P, st, obs(i + 100, True))
    assert st.state == DriftState.RECOVERING and not t
    # a long run of healthy fresh probes restores trust, via an explicit transition
    rng = random.Random(7)
    done = []
    for k in range(200):
        st, t = advance(r, P, st, obs(i + 101 + k, rng.random() < 0.6))
        done += t
        if st.state == DriftState.NORMAL:
            break
    assert st.state == DriftState.NORMAL
    last = done[-1]
    assert (
        last.trigger == DriftTrigger.RECOVERY_CRITERIA_MET
        and last.from_state == DriftState.RECOVERING
    )
    assert last.measurements["m_fresh"] >= P.min_recovery_samples
    assert st.window == () and st.fresh == ()  # the history is not reused to claim recovery
    assert st.episode == 1  # the prior reference itself was never edited


def test_a_still_degraded_probe_window_sends_the_cell_back_to_quarantine() -> None:
    r = ref()
    st, i = _drive_to_quarantine(r)
    for k in range(P.min_quarantine):
        st, _ = advance(r, P, st, obs(i + k, True, 9000, action=DEF))
    assert st.state == DriftState.RECOVERING
    out = []
    for k in range(60):
        st, t = advance(r, P, st, obs(i + 200 + k, False))
        out += t
        if t:
            break
    assert (
        out[-1].to_state == DriftState.QUARANTINED
        and out[-1].trigger == DriftTrigger.RECOVERY_FAILED
    )
    assert st.episode == 2


def test_the_probe_budget_and_episode_cap_bound_recovery_attempts() -> None:
    tight = P.model_copy(update={"probe_budget": 30, "max_episodes": 1, "min_quarantine": 5})
    r = ref()
    st, i = _drive_to_quarantine(r, tight)
    for k in range(5):
        st, _ = advance(r, tight, st, obs(i + k, True, 9000, action=DEF))
    assert st.state == DriftState.RECOVERING
    rng = random.Random(3)
    for k in range(60):  # healthy but not decisively: the budget runs out first
        st, _ = advance(r, tight, st, obs(i + 50 + k, rng.random() < 0.38))
        if st.state != DriftState.RECOVERING:
            break
    assert st.state == DriftState.QUARANTINED and st.locked  # no further automatic attempt
    for k in range(500):
        st, t = advance(r, tight, st, obs(i + 400 + k, True, 9000, action=DEF))
        assert not t and st.state == DriftState.QUARANTINED


def test_entry_and_recovery_use_different_thresholds_so_one_window_cannot_do_both() -> None:
    r = ref()
    assert P.recovery_threshold < P.success_tolerance
    for k in range(0, 41):  # every window composition of 40 observations
        win = [(1, 6000, True)] * k + [(0, 6000, True)] * (40 - k)
        entering = evaluate(r, P, win, 0.0, T0).levels[DriftTrigger.SUCCESS_DEGRADATION] == 2
        leaving, _ = recovery_ok(r, P, win, T0)
        assert not (entering and leaving)
    with pytest.raises(ValueError):
        DriftParams(recovery_threshold=0.10, success_tolerance=0.10)


def test_context_mismatch_in_probes_blocks_recovery() -> None:
    r = ref()
    ok, _ = recovery_ok(r, P, [(1, 6000, True)] * 29 + [(1, 6000, False)], T0)
    assert not ok
    ok2, meas = recovery_ok(r, P, [(1 if i % 2 else 0, 6000, True) for i in range(30)], T0)
    assert meas["m_fresh"] == 30.0 and isinstance(ok2, bool)


def test_unknown_outcomes_are_visible_but_never_counted() -> None:
    r = ref()
    st = MonitorState()
    unknown = Observation("d-1", ALT, False, False, 100, T0)
    for _ in range(10):
        st, t = advance(r, P, st, unknown)
        assert not t
    assert st.n_unknown == 10 and st.window == () and st.n_alt == 0


def test_other_actions_and_default_observations_do_not_touch_the_alternative_window() -> None:
    r = ref()
    st = MonitorState()
    st, _ = advance(r, P, st, obs(1, False, action="DIRECT_CAPABILITY:other@1"))
    st, _ = advance(r, P, st, obs(2, False, action=DEF))
    assert st.window == () and st.n_default == 1 and st.n_alt == 0


def test_the_fold_is_deterministic_and_incremental_equals_replay() -> None:
    r = ref()
    seq = stream(300, 0.5, 21) + stream(200, 0.1, 22, 300) + stream(300, 0.5, 23, 500)
    s1, t1 = replay(r, P, seq)
    s2, t2 = replay(r, P, seq)
    assert (s1, t1) == (s2, t2)
    st, acc = MonitorState(), []
    for o in seq:
        st, t = advance(r, P, st, o)
        acc += t
    assert (st, acc) == (s1, t1)
    assert {t.config_hash for t in t1} == {P.config_hash()}  # every transition names its config


def test_cusum_detects_and_is_wired_to_the_success_channel_only() -> None:
    c = P.model_copy(update={"detector": Detector.CUSUM, "cusum_threshold": 6.0, "persistence": 3})
    r = ref()
    _, trs = replay(r, c, stream(60, 0.5, 1) + stream(80, 0.1, 2, 60))
    assert any(
        t.to_state == DriftState.DRIFTED and t.trigger == DriftTrigger.SUCCESS_DEGRADATION
        for t in trs
    )
    _, quiet = replay(r, c, stream(40, 0.5, 8))
    assert not any(t.to_state == DriftState.DRIFTED for t in quiet)


# ---- fleet: isolation, shadow decisions, references --------------------------------------------
def rec(
    i: int,
    family: str,
    action: str,
    ok: bool,
    acted: bool = True,
    tenant: str = "bench",
    default: str = DEF,
    tokens: int = 6000,
) -> DecisionOutcomeRecord:
    return DecisionOutcomeRecord(
        decision_id=f"d-{i:05d}",
        run_id=f"r-{i}",
        tenant_id=tenant,
        policy_version="adaptive/1",
        acted=acted,
        selection_probability=1.0,
        family=family,
        risk_level="low",
        action=action,
        status=OutcomeStatus.VERIFIED_SUCCESS if ok else OutcomeStatus.FAILURE,
        tokens=tokens,
        default_key=default,
        observed_at=T0 + timedelta(hours=i),
        runtime_version="rt-1",
        verifier_version="v-1",
    )


def test_drift_is_isolated_to_its_context_and_shadow_decisions_are_ignored() -> None:
    a, b = ref(family="timeout"), ref(family="pool")
    fleet = MonitorFleet([a, b], P)
    rng = random.Random(5)
    records = []
    for i in range(400):
        bad = i >= 100
        records.append(rec(2 * i, "timeout", ALT, rng.random() < (0.05 if bad else 0.5)))
        records.append(rec(2 * i + 1, "pool", ALT, rng.random() < 0.5))
    fleet.ingest(records)
    assert fleet.states[a.cell_key].state == DriftState.QUARANTINED
    assert fleet.states[b.cell_key].state in (DriftState.NORMAL, DriftState.SUSPECT)
    assert set(fleet.overlay().cells) >= {a.cell_key}
    shadow = MonitorFleet([a], P)
    shadow.ingest([rec(i, "timeout", ALT, False, acted=False) for i in range(300)])
    assert shadow.states[a.cell_key].n_alt == 0 and not shadow.transitions  # never observations


def test_a_fleet_only_sees_records_of_its_own_default_context() -> None:
    a = ref(family="timeout")
    fleet = MonitorFleet([a], P)
    fleet.ingest(
        [rec(i, "timeout", ALT, False, default="DIRECT_CAPABILITY:other@2") for i in range(100)]
    )
    assert fleet.states[a.cell_key].n_alt == 0


def test_references_come_from_the_paired_cell_and_its_source() -> None:
    cell = PairedCell(
        n=240,
        both=90,
        default_only=30,
        alt_only=40,
        neither=80,
        default_tokens=2_000_000,
        alt_tokens=1_300_000,
    )
    src = PairedSource(
        name="s",
        arms=["A", "E"],
        splits=["VALIDATION"],
        dataset_hashes={},
        model="m",
        runtime_version="rt-9",
        verifier_version="v-9",
        n_pairs=240,
        n_unknown_dropped=0,
        n_tied_dropped=0,
        tasks_sha256="x" * 64,
        validation_tokens=1,
        capabilities=["cap@1"],
        recorded_at=T0,
    )
    key = paired_key("timeout", "low", DEF, ALT)
    r = reference_of(key, cell, [src])
    assert (r.k_alt, r.k_def, r.n) == (130, 120, 240)
    assert r.runtime_version == "rt-9" and r.recorded_at == T0 and r.monitorable
    assert parse_paired_key(key) == ("timeout", "low", DEF, ALT)
    dead = reference_of(
        key,
        PairedCell(
            n=10, both=0, default_only=5, alt_only=0, neither=5, default_tokens=1, alt_tokens=1
        ),
        [src],
    )
    assert not dead.monitorable  # a cell with no alternative successes cannot be monitored


def test_shadow_and_enforce_differ_only_in_the_recorded_mode() -> None:
    r = ref()
    seq = stream(60, 0.5, 1) + stream(80, 0.1, 2, 60)
    _, a = replay(r, P.model_copy(update={"mode": DriftMode.SHADOW}), seq)
    _, b = replay(r, P.model_copy(update={"mode": DriftMode.ENFORCE}), seq)
    assert [t.signature() for t in a] == [t.signature() for t in b]
    assert not any(t.applied for t in a) and all(t.applied for t in b)
    assert {DriftState.DRIFTED, DriftState.QUARANTINED, DriftState.RECOVERING} == PRIOR_DISABLED
