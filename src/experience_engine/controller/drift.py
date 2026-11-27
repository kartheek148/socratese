"""Windowed drift monitor for prior-backed controller decisions (M6.2, pure).

A cell is one paired prior (rule default vs one alternative in a context). The monitor folds the
ordered, executed and verified outcomes of that context through an explicit state machine
(NORMAL, SUSPECT, DRIFTED, QUARANTINED, RECOVERING). It is a deterministic function of the
reference, the parameters and the observation sequence, so any persisted transition log can be
recomputed and checked. It never chooses a capability; it only decides whether the historical
prior may still control a decision.
"""

import math
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime

from experience_engine.domain.controller import (
    DecisionOutcomeRecord,
    EstimatorSnapshot,
    PairedCell,
    PairedSource,
    paired_key,
)
from experience_engine.domain.drift import (
    DETECTOR_VERSION,
    Detector,
    DriftCellStatus,
    DriftOverlay,
    DriftParams,
    DriftState,
    DriftTransition,
    DriftTrigger,
    effect_of,
)
from experience_engine.domain.stats import newcombe_difference_bounds, normal_quantile

_PRIORITY = (
    DriftTrigger.CONTEXT_MISMATCH,
    DriftTrigger.EVIDENCE_STALE,
    DriftTrigger.SUCCESS_DEGRADATION,
    DriftTrigger.UTILITY_DEGRADATION,
    DriftTrigger.COST_DEGRADATION,
)


@dataclass(frozen=True)
class CellReference:
    """The historical evidence one cell's decisions rest on."""

    cell_key: str
    family: str | None
    risk: str
    default_key: str
    alt_key: str
    n: int
    k_alt: int
    k_def: int
    tokens_alt: int
    tokens_def: int
    runtime_version: str | None = None
    verifier_version: str | None = None
    recorded_at: datetime | None = None

    @property
    def monitorable(self) -> bool:
        return self.n > 0 and self.k_alt > 0 and self.k_def > 0 and self.tokens_alt > 0

    @property
    def p_alt(self) -> float:
        return self.k_alt / self.n

    @property
    def p_def(self) -> float:
        return self.k_def / self.n

    @property
    def mean_tokens(self) -> float:
        return self.tokens_alt / self.n

    @property
    def cps_alt(self) -> float:
        return self.tokens_alt / self.k_alt

    @property
    def cps_def(self) -> float:
        return self.tokens_def / self.k_def

    def compatible(self, obs: "Observation") -> bool:
        if (
            self.runtime_version
            and obs.runtime_version
            and self.runtime_version != obs.runtime_version
        ):
            return False
        return not (
            self.verifier_version
            and obs.verifier_version
            and self.verifier_version != obs.verifier_version
        )


@dataclass(frozen=True)
class Observation:
    """One executed decision in the cell's context with its verified outcome."""

    decision_id: str
    action: str
    known: bool
    success: bool
    tokens: int
    at: datetime | None = None
    runtime_version: str | None = None
    verifier_version: str | None = None


@dataclass(frozen=True)
class MonitorState:
    state: DriftState = DriftState.NORMAL
    seq: int = 0
    window: tuple[tuple[int, int, bool], ...] = ()  # (success, tokens, compatible), live
    fresh: tuple[tuple[int, int, bool], ...] = ()  # recovery probes
    n_alt: int = 0
    n_default: int = 0
    n_unknown: int = 0
    n_ignored: int = 0
    cusum: float = 0.0
    alarm_streak: int = 0
    clear_streak: int = 0
    dwell: int = 0
    recover_streak: int = 0
    probes_used: int = 0
    episode: int = 0
    locked: bool = False


@dataclass(frozen=True)
class Evaluation:
    level: int  # 0 ok, 1 suspect, 2 alarm
    trigger: DriftTrigger | None
    levels: dict[DriftTrigger, int]
    measurements: dict[str, float]


# ---- window statistics -------------------------------------------------------------------------
def _mean_sd(xs: Sequence[float]) -> tuple[float, float]:
    m = len(xs)
    mean = sum(xs) / m
    if m < 2:
        return mean, 0.0
    return mean, math.sqrt(sum((x - mean) ** 2 for x in xs) / (m - 1))


def _age_days(ref: CellReference, now: datetime | None) -> float | None:
    if ref.recorded_at is None or now is None:
        return None
    return (now - ref.recorded_at).total_seconds() / 86400.0


def _cusum_step(ref: CellReference, params: DriftParams, s: float, success: bool) -> float:
    p0 = min(max(ref.p_alt, 0.02), 0.98)
    p1 = min(max(p0 - params.cusum_shift, 0.01), p0 - 0.01)
    llr = math.log(p1 / p0) if success else math.log((1 - p1) / (1 - p0))
    return max(0.0, s + llr)


def evaluate(
    ref: CellReference,
    params: DriftParams,
    window: Sequence[tuple[int, int, bool]],
    cusum: float,
    now: datetime | None,
) -> Evaluation:
    """Channel levels over `window`. Statistical channels need `min_samples`; context mismatch and
    staleness do not (they are structural, not sampling questions)."""
    m = len(window)
    meas: dict[str, float] = {"m": float(m)}
    levels: dict[DriftTrigger, int] = {}
    n_bad = sum(1 for _, _, ok in window if not ok)
    meas["incompatible"] = float(n_bad)
    levels[DriftTrigger.CONTEXT_MISMATCH] = (
        2 if n_bad >= params.mismatch_min else 1 if n_bad >= 1 else 0
    )
    age = _age_days(ref, now)
    if age is not None:
        meas["age_days"] = age
    levels[DriftTrigger.EVIDENCE_STALE] = (
        2 if age is not None and params.max_evidence_age_days is not None
        and age > params.max_evidence_age_days else 0
    )  # fmt: skip
    if m >= params.min_samples:
        k = sum(s for s, _, _ in window)
        toks = [float(t) for _, t, _ in window]
        t_mean, t_sd = _mean_sd(toks)
        p_hat = k / m
        meas.update(success_rate=p_hat, ref_success=ref.p_alt, cost_mean=t_mean,
                    ref_cost_mean=ref.mean_tokens)  # fmt: skip
        z_a, z_s = normal_quantile(params.confidence), normal_quantile(params.suspect_confidence)
        # success
        if params.detector == Detector.CUSUM:
            meas["cusum"] = cusum
            lv = (
                2
                if cusum >= params.cusum_threshold
                else 1
                if cusum >= params.cusum_threshold / 2
                else 0
            )
        else:
            _, ub = newcombe_difference_bounds(k, m, ref.k_alt, ref.n, params.confidence)
            _, ub_s = newcombe_difference_bounds(k, m, ref.k_alt, ref.n, params.suspect_confidence)
            meas["success_diff_ub"] = ub
            lv = 2 if ub < -params.success_tolerance else 1 if ub_s < 0 else 0
        levels[DriftTrigger.SUCCESS_DEGRADATION] = lv
        # cost
        se = t_sd * math.sqrt(1.0 / m + 1.0 / ref.n)
        diff = t_mean - ref.mean_tokens
        meas["cost_lb"] = diff - z_a * se
        levels[DriftTrigger.COST_DEGRADATION] = (
            2 if diff - z_a * se > params.cost_tolerance * ref.mean_tokens
            else 1 if diff - z_s * se > 0 else 0
        )  # fmt: skip
        # utility: tokens per verified success
        if k >= 1 and t_mean > 0:
            cv2 = (t_sd / t_mean) ** 2
            var_u = cv2 / m + cv2 / ref.n + (1 - p_hat) / (m * p_hat)
            cps = sum(toks) / k
            r_ref = math.log(cps / ref.cps_alt)
            r_def = math.log(cps / ref.cps_def)
            se_ref = math.sqrt(var_u + (1 - ref.p_alt) / (ref.n * ref.p_alt))
            se_def = math.sqrt(var_u + (1 - ref.p_def) / (ref.n * ref.p_def))
            meas["utility_ratio"] = cps / ref.cps_alt
            meas["utility_lb"] = r_ref - z_a * se_ref
            meas["saving_gone_lb"] = r_def - z_a * se_def
            if (
                r_ref - z_a * se_ref > math.log(1 + params.utility_tolerance)
                or r_def - z_a * se_def > 0
            ):
                levels[DriftTrigger.UTILITY_DEGRADATION] = 2
            else:
                levels[DriftTrigger.UTILITY_DEGRADATION] = 1 if r_ref - z_s * se_ref > 0 else 0
        else:
            levels[DriftTrigger.UTILITY_DEGRADATION] = 0
    top = max(levels.values(), default=0)
    trigger = next((t for t in _PRIORITY if levels.get(t, 0) == top), None) if top else None
    return Evaluation(top, trigger, levels, meas)


def recovery_ok(
    ref: CellReference,
    params: DriftParams,
    fresh: Sequence[tuple[int, int, bool]],
    now: datetime | None,
) -> tuple[bool, dict[str, float]]:
    """Recovery needs more than absence of alarm: the lower bound of the fresh success rate must
    clear a *stricter* bound than the one that triggered drift, with acceptable cost and utility,
    compatible context and fresh evidence."""
    m = len(fresh)
    meas: dict[str, float] = {"m_fresh": float(m)}
    if m < params.min_recovery_samples:
        return False, meas
    k = sum(s for s, _, _ in fresh)
    toks = [float(t) for _, t, _ in fresh]
    t_mean, _ = _mean_sd(toks)
    lb, _ = newcombe_difference_bounds(k, m, ref.k_alt, ref.n, params.recovery_confidence)
    meas.update(fresh_success=k / m, fresh_success_lb=lb, fresh_cost_mean=t_mean)
    ok_s = lb >= -params.recovery_threshold
    ok_c = t_mean <= ref.mean_tokens * (1 + params.cost_tolerance / 2)
    ok_u = True
    if k >= 1:
        ratio = (sum(toks) / k) / ref.cps_alt
        meas["fresh_utility_ratio"] = ratio
        ok_u = ratio <= 1 + params.utility_tolerance / 2
    else:
        ok_u = False
    bad = sum(1 for _, _, ok in fresh if not ok)
    age = _age_days(ref, now)
    fresh_enough = not (
        age is not None
        and params.max_evidence_age_days is not None
        and age > params.max_evidence_age_days
    )
    return bool(ok_s and ok_c and ok_u and bad == 0 and fresh_enough), meas


# ---- state machine -----------------------------------------------------------------------------
_ENTER_QUARANTINE: dict[str, object] = {
    "window": (),
    "fresh": (),
    "dwell": 0,
    "probes_used": 0,
    "recover_streak": 0,
    "cusum": 0.0,
    "alarm_streak": 0,
    "clear_streak": 0,
}


def advance(
    ref: CellReference, params: DriftParams, st: MonitorState, obs: Observation
) -> tuple[MonitorState, list[DriftTransition]]:
    """Fold one observation. Pure; returns the new state and the transitions it caused."""
    if not obs.known:
        return replace(st, n_unknown=st.n_unknown + 1), []  # visible, never a success or failure
    is_alt = obs.action == ref.alt_key
    if not is_alt and obs.action != ref.default_key:
        return st, []
    state = st.state
    live = state in (DriftState.NORMAL, DriftState.SUSPECT)
    w = list(st.window)
    fresh = list(st.fresh)
    cusum, dwell, probes = st.cusum, st.dwell, st.probes_used
    n_alt, n_def, n_ign = st.n_alt, st.n_default, st.n_ignored
    evaluate_now = False
    if is_alt:
        n_alt += 1
        entry = (int(obs.success), obs.tokens, ref.compatible(obs))
        if live:
            w = (w + [entry])[-params.window :]
            cusum = _cusum_step(ref, params, cusum, obs.success)
            evaluate_now = True
        elif state == DriftState.RECOVERING:
            fresh = (fresh + [entry])[-params.window :]
            probes += 1
            evaluate_now = True
        else:
            n_ign += 1  # an alternative outcome while quarantined is not prior-backed evidence
    else:
        n_def += 1
        if not live:
            dwell += 1
            evaluate_now = state == DriftState.QUARANTINED
    cur = replace(
        st, window=tuple(w), fresh=tuple(fresh), cusum=cusum, dwell=dwell, probes_used=probes,
        n_alt=n_alt, n_default=n_def, n_ignored=n_ign,
    )  # fmt: skip
    if not evaluate_now:
        return cur, []
    out: list[DriftTransition] = []
    seq = cur.seq

    def move(
        to: DriftState, trig: DriftTrigger, meas: dict[str, float], n_win: int, **upd: object
    ) -> None:
        nonlocal cur, seq, state
        seq += 1
        out.append(
            DriftTransition(
                cell_key=ref.cell_key,
                seq=seq,
                from_state=state,
                to_state=to,
                trigger=trig,
                effect=effect_of(to, state),
                measurements={k: round(v, 6) for k, v in meas.items()},
                n_window=n_win,
                decision_id=obs.decision_id,
                observed_at=obs.at,
                detector_version=DETECTOR_VERSION,
                config_hash=params.config_hash(),
                mode=params.mode,
            )
        )
        state = to
        cur = replace(cur, state=to, seq=seq, **upd)  # type: ignore[arg-type]

    if state in (DriftState.NORMAL, DriftState.SUSPECT):
        ev = evaluate(ref, params, cur.window, cur.cusum, obs.at)
        n_win = len(cur.window)
        if state == DriftState.NORMAL:
            if ev.level >= 1 and ev.trigger is not None:
                streak = 1 if ev.level == 2 else 0
                move(DriftState.SUSPECT, ev.trigger, ev.measurements, n_win,
                     alarm_streak=streak, clear_streak=0)  # fmt: skip
        elif ev.level == 2 and ev.trigger is not None:
            streak = cur.alarm_streak + 1
            cur = replace(cur, alarm_streak=streak, clear_streak=0)
            if streak >= params.persistence:
                move(DriftState.DRIFTED, ev.trigger, ev.measurements, n_win)
                reset = {**_ENTER_QUARANTINE, "episode": cur.episode + 1}
                move(DriftState.QUARANTINED, DriftTrigger.QUARANTINE_APPLIED, ev.measurements,
                     n_win, **reset)  # fmt: skip
        elif ev.level == 0:
            clear = cur.clear_streak + 1
            cur = replace(cur, clear_streak=clear, alarm_streak=0)
            if clear >= params.clear_after:
                move(DriftState.NORMAL, DriftTrigger.SIGNAL_CLEARED, ev.measurements, n_win,
                     clear_streak=0, alarm_streak=0)  # fmt: skip
        else:
            cur = replace(cur, alarm_streak=0, clear_streak=0)
    elif state == DriftState.QUARANTINED:
        age = _age_days(ref, obs.at)
        limit = params.max_evidence_age_days
        stale = age is not None and limit is not None and age > limit
        if not cur.locked and cur.dwell >= params.min_quarantine and not stale:
            move(DriftState.RECOVERING, DriftTrigger.QUARANTINE_ELAPSED,
                 {"dwell": float(cur.dwell)}, 0, fresh=(), probes_used=0,
                 recover_streak=0)  # fmt: skip
    elif state == DriftState.RECOVERING:
        windowed = params.model_copy(update={"detector": Detector.WINDOW})
        ev = evaluate(ref, windowed, cur.fresh, 0.0, obs.at)
        ok, rmeas = recovery_ok(ref, params, cur.fresh, obs.at)
        n_win = len(cur.fresh)
        failed = ev.level == 2 and n_win >= params.min_samples
        exhausted = cur.probes_used >= params.probe_budget and not ok
        if failed or exhausted:
            why = {**ev.measurements, **rmeas, "probes_used": float(cur.probes_used)}
            nxt = cur.episode + 1
            reset = {**_ENTER_QUARANTINE, "episode": nxt, "locked": nxt > params.max_episodes}
            move(DriftState.QUARANTINED, DriftTrigger.RECOVERY_FAILED, why, n_win, **reset)
        else:
            streak = cur.recover_streak + 1 if ok else 0
            cur = replace(cur, recover_streak=streak)
            if streak >= params.recovery_persistence:
                done = {**ev.measurements, **rmeas}
                move(DriftState.NORMAL, DriftTrigger.RECOVERY_CRITERIA_MET, done, n_win,
                     window=(), fresh=(), cusum=0.0, alarm_streak=0, clear_streak=0,
                     recover_streak=0, probes_used=0)  # fmt: skip
    return cur, out


def replay(
    ref: CellReference, params: DriftParams, observations: Iterable[Observation]
) -> tuple[MonitorState, list[DriftTransition]]:
    st = MonitorState()
    all_t: list[DriftTransition] = []
    for obs in observations:
        st, ts = advance(ref, params, st, obs)
        all_t.extend(ts)
    return st, all_t


def status_of(st: MonitorState, params: DriftParams) -> DriftCellStatus:
    left = max(0, params.probe_budget - st.probes_used) if st.state == DriftState.RECOVERING else 0
    return DriftCellStatus(state=st.state, seq=st.seq, probes_remaining=left)


# ---- cells and fleets --------------------------------------------------------------------------
def parse_paired_key(key: str) -> tuple[str | None, str, str, str]:
    family, risk, rest = key.split("|", 2)
    default, alt = rest.split(">", 1)
    return (None if family == "-" else family), risk, default, alt


def _ref_of_action(key: str) -> str | None:
    _, sep, ref = key.partition(":")
    return ref if sep else None


def reference_of(key: str, cell: PairedCell, sources: Sequence[PairedSource]) -> CellReference:
    family, risk, default, alt = parse_paired_key(key)
    refs = {r for r in (_ref_of_action(default), _ref_of_action(alt)) if r}
    src = next((s for s in sources if refs & set(s.capabilities)), sources[0] if sources else None)
    recorded = getattr(src, "recorded_at", None)
    return CellReference(
        cell_key=key,
        family=family,
        risk=risk,
        default_key=default,
        alt_key=alt,
        n=cell.n,
        k_alt=cell.both + cell.alt_only,
        k_def=cell.both + cell.default_only,
        tokens_alt=cell.alt_tokens,
        tokens_def=cell.default_tokens,
        runtime_version=src.runtime_version if src else None,
        verifier_version=src.verifier_version if src else None,
        recorded_at=recorded,
    )


def references_from_snapshot(snapshot: EstimatorSnapshot) -> list[CellReference]:
    return [
        r
        for r in (
            reference_of(k, c, snapshot.prior_sources) for k, c in sorted(snapshot.paired.items())
        )
        if r.monitorable
    ]


def observation_of(r: DecisionOutcomeRecord) -> Observation:
    return Observation(
        decision_id=r.decision_id,
        action=r.action,
        known=r.known,
        success=r.success,
        tokens=r.tokens,
        at=r.observed_at,
        runtime_version=r.runtime_version,
        verifier_version=r.verifier_version,
    )


class MonitorFleet:
    """All monitored cells of one policy. Ingests executed decisions in order; incremental."""

    def __init__(self, refs: Sequence[CellReference], params: DriftParams):
        self.params = params
        self.refs = {r.cell_key: r for r in refs}
        self._by_ctx: dict[tuple[str | None, str, str], list[CellReference]] = defaultdict(list)
        for r in refs:
            self._by_ctx[(r.family, r.risk, r.default_key)].append(r)
        self.states: dict[str, MonitorState] = {r.cell_key: MonitorState() for r in refs}
        self.transitions: list[DriftTransition] = []

    def ingest(self, records: Iterable[DecisionOutcomeRecord]) -> list[DriftTransition]:
        new: list[DriftTransition] = []
        for rec in records:
            if not rec.acted:
                continue  # shadow decisions are never observations
            cells = self._by_ctx.get((rec.family, rec.risk_level, rec.default_key))
            if not cells:
                continue
            obs = observation_of(rec)
            for ref in cells:
                st, ts = advance(ref, self.params, self.states[ref.cell_key], obs)
                self.states[ref.cell_key] = st
                new.extend(ts)
        self.transitions.extend(new)
        return new

    def overlay(self) -> DriftOverlay:
        return DriftOverlay(
            cells={
                k: status_of(s, self.params)
                for k, s in sorted(self.states.items())
                if s.state != DriftState.NORMAL
            }
        )


def replay_fleet(
    snapshot: EstimatorSnapshot, params: DriftParams, records: Iterable[DecisionOutcomeRecord]
) -> MonitorFleet:
    fleet = MonitorFleet(references_from_snapshot(snapshot), params)
    fleet.ingest(records)
    return fleet


def probe_candidates(key_state: dict[str, DriftCellStatus], alt_keys: Sequence[str]) -> list[str]:
    return [k for k in alt_keys if k in key_state and key_state[k].probes_remaining > 0]


def cell_key_for(family: str | None, risk: str, default_key: str, alt_key: str) -> str:
    return paired_key(family, risk, default_key, alt_key)
