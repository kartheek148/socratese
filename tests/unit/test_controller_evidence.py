"""M6.1 unit tests: paired bound, hash stability, paired priors, evidence-driven policy."""

import math
from pathlib import Path

import pytest
from pydantic import ValidationError

from controller_support import (
    CAP,
    CAP_KEY,
    PLANNING_KEY,
    correlated_world,
    ctx,
    definition,
    evidence_definition,
    match_with,
    pcell,
    prior_of,
    record,
    snapshot_of,
    source,
    task,
)
from experience_engine.controller.datasets import paired_tasks
from experience_engine.controller.paired import (
    build_paired_cells,
    make_prior,
    required_pairs,
    subsample,
    version_problems,
)
from experience_engine.controller.policy import AdaptiveController
from experience_engine.controller.replay import replay_decision
from experience_engine.controller.simulate import SimConfig, Verdict, compare, run_sequence
from experience_engine.domain.controller import (
    CONTROLLER_KEY,
    ControllerKind,
    ControllerParams,
    ControllerPolicyDefinition,
    DecisionFeatures,
    PairedCell,
    paired_key,
)
from experience_engine.domain.decision import Strategy
from experience_engine.domain.experiment import ExperimentResult
from experience_engine.domain.stats import newcombe_difference_bounds, newcombe_paired_bounds
from experience_engine.execution.controller import SELECTED_CAPABILITY_KEY


# ---- paired statistics --------------------------------------------------------------------------
def _exact_coverage(n: int, probs: tuple[float, float, float, float], conf: float = 0.95) -> float:
    """Exact probability that the lower bound is <= the true difference (enumerates every table)."""
    p11, p10, p01, p00 = probs
    theta = p10 - p01
    lg = [math.lgamma(i + 1) for i in range(n + 2)]
    cov = 0.0
    for a in range(n + 1):
        for b in range(n + 1 - a):
            for c in range(n + 1 - a - b):
                d = n - a - b - c
                logp = lg[n] - lg[a] - lg[b] - lg[c] - lg[d]
                for k, p in ((a, p11), (b, p10), (c, p01), (d, p00)):
                    if k:
                        logp += k * math.log(p)
                if theta >= newcombe_paired_bounds(a, b, c, d, conf)[0]:
                    cov += math.exp(logp)
    return cov


@pytest.mark.parametrize(
    "probs",
    [
        (0.4, 0.1, 0.1, 0.4),
        (0.3, 0.2, 0.1, 0.4),
        (0.45, 0.05, 0.05, 0.45),
        (0.25, 0.25, 0.25, 0.25),
    ],
)
def test_paired_bound_has_near_nominal_exact_coverage(
    probs: tuple[float, float, float, float],
) -> None:
    # nominal 0.95 one-sided; the hybrid score interval is slightly liberal at isolated points
    assert _exact_coverage(40, probs) >= 0.94


def test_paired_bound_uses_pairing_and_is_tighter_than_the_independent_bound() -> None:
    # 240 tasks, 25% discordant, equal marginals: pairing removes the shared task noise
    both, only1, only2, neither = 90, 30, 30, 90
    lo_p, hi_p = newcombe_paired_bounds(both, only1, only2, neither, 0.95)
    n = both + only1 + only2 + neither
    lo_i, hi_i = newcombe_difference_bounds(both + only1, n, both + only2, n, 0.95)
    assert lo_p > lo_i and hi_p < hi_i
    wald = 1.645 * math.sqrt(only1 + only2) / n  # SE of a paired difference depends on b + c only
    assert lo_p == pytest.approx(-wald, abs=5e-3) and hi_p == pytest.approx(wald, abs=5e-3)
    indep = 1.645 * math.sqrt(2 * 0.25 / n)  # two independent samples of 240 at p = 0.5
    assert lo_i == pytest.approx(-indep, abs=5e-3)
    assert lo_p > 0.75 * lo_i  # pairing narrows the half-width by ~29% at 25% discordance


def test_paired_bound_edge_cases_and_symmetry() -> None:
    assert newcombe_paired_bounds(0, 0, 0, 0, 0.95) == (-1.0, 1.0)
    lo, hi = newcombe_paired_bounds(10, 0, 0, 10, 0.95)  # perfect agreement: still not exactly 0
    assert -0.2 < lo < 0 < hi < 0.2
    l1, u1 = newcombe_paired_bounds(40, 12, 3, 45, 0.95)
    l2, u2 = newcombe_paired_bounds(40, 3, 12, 45, 0.95)  # swapping the outcomes negates the bounds
    assert l1 == pytest.approx(-u2) and u1 == pytest.approx(-l2)
    assert newcombe_paired_bounds(0, 5, 0, 0, 0.95)[0] > 0  # always only outcome 1 wins
    lo_z, _ = newcombe_paired_bounds(0, 0, 0, 30, 0.95)  # a zero marginal does not divide by zero
    assert -0.2 < lo_z <= 0


# ---- hashes and serialisation (M6 definitions must not change) ----------------------------------

M6_COST_AWARE_HASH = "53b7c21a4d290416373afd4556c7c5971efc6d32990746fe87c7904de10cd8bb"
M6_BANDIT_HASH = "fe45fb195ccf87f564bde0960fec84c777f85f86e8b5c6cb1159ccd59333c25c"
M6_SNAPSHOT_HASH = "9f559935c36b3de748b14724ebd21f35a57d3cd9995eb0b05d45b7fae16a24ef"
GOOD_CELLS = {PLANNING_KEY: (300, 270, 300 * 1000), CAP_KEY: (300, 255, 300 * 3000)}


def test_m6_definitions_keep_their_hashes_and_serialised_form() -> None:
    """Computed BEFORE the M6.1 change: adding the evidence fields must not alter any stored
    M6 definition (the DB guard compares stored hashes)."""
    ca = definition(snapshot_of(GOOD_CELLS))
    bandit = definition(
        snapshot_of(GOOD_CELLS),
        ControllerKind.BANDIT,
        exploration_risk_levels=["low"],
        exploration_families=["pool"],
        epsilon=0.2,
        min_total_verified=100,
    )
    assert ca.definition_hash() == M6_COST_AWARE_HASH
    assert bandit.definition_hash() == M6_BANDIT_HASH
    assert ca.snapshot.snapshot_hash() == M6_SNAPSHOT_HASH
    dumped = ca.model_dump(mode="json")
    assert "evidence" not in dumped["params"] and "paired" not in dumped["snapshot"]
    assert "prior_sources" not in dumped["snapshot"]
    again = ControllerPolicyDefinition.model_validate(dumped)  # an M6 JSON loads as before
    assert again.definition_hash() == M6_COST_AWARE_HASH


def test_evidence_definitions_serialise_their_priors_and_round_trip() -> None:
    d = evidence_definition(pcell(90, 30, 30, 90))
    dumped = d.model_dump(mode="json")
    assert dumped["params"]["evidence"]["n_min_pairs"] == 30 and dumped["snapshot"]["paired"]
    back = ControllerPolicyDefinition.model_validate(dumped)
    assert back.definition_hash() == d.definition_hash() != M6_COST_AWARE_HASH
    other = evidence_definition(pcell(90, 30, 31, 89))
    assert other.definition_hash() != d.definition_hash()  # the prior is part of the identity


def test_evidence_parameter_validation() -> None:
    with pytest.raises(ValidationError, match="required_success"):
        ControllerParams.model_validate({"evidence": {}, "required_success": {"low": 0.5}})
    with pytest.raises(ValidationError, match="evidence parameters"):
        ControllerPolicyDefinition(
            policy_id="a",
            version=1,
            kind=ControllerKind.EVIDENCE_DRIVEN,
            params=ControllerParams(),
            snapshot=snapshot_of({}),
        )
    paired_snap = snapshot_of({}).model_copy(
        update={"paired": {paired_key("pool", "low", CAP_KEY, PLANNING_KEY): pcell(9, 1, 1, 9)}}
    )
    with pytest.raises(ValidationError, match="paired priors"):
        ControllerPolicyDefinition(
            policy_id="a",
            version=1,
            kind=ControllerKind.COST_AWARE,
            params=ControllerParams(),
            snapshot=paired_snap,
        )
    with pytest.raises(ValidationError):
        PairedCell(
            n=10, both=3, default_only=3, alt_only=3, neither=3, default_tokens=0, alt_tokens=0
        )


# ---- paired prior builder -----------------------------------------------------------------------
def test_prior_builder_counts_tables_tokens_and_applies_the_minimum() -> None:
    tasks = correlated_world(
        120, {PLANNING_KEY: 0.5, CAP_KEY: 0.5}, {PLANNING_KEY: 5000, CAP_KEY: 8000}, seed=2
    )
    (cell,) = build_paired_cells(tasks).values()
    assert cell.n == 120 and cell.both + cell.default_only + cell.alt_only + cell.neither == 120
    d_succ = sum(t.outcomes[CAP_KEY][0].value == "VERIFIED_SUCCESS" for t in tasks)
    assert cell.both + cell.default_only == d_succ
    assert cell.default_tokens == sum(t.outcomes[CAP_KEY][1] for t in tasks)
    assert cell.alt_tokens == sum(t.outcomes[PLANNING_KEY][1] for t in tasks)
    small = make_prior(tasks[:20], [source(20)], n_min_pairs=30)
    assert small.cells == {} and list(small.excluded.values()) == [
        20
    ]  # too few: not used, reported
    assert make_prior(tasks, [source(120)], 30).validation_tokens == source().validation_tokens
    assert subsample(tasks, 40) == subsample(tasks, 40) and len(subsample(tasks, 40)) == 40
    assert subsample(tasks, 40) != subsample(tasks, 40, seed=1)


def test_required_pairs_is_an_evidence_requirement() -> None:
    cell = pcell(23, 7, 7, 23)  # the D2-like table: 60 pairs, planning == production
    need = required_pairs(cell, 0.10, 0.95)
    assert need is not None and 60 < need < 100
    assert required_pairs(pcell(10, 30, 0, 20), 0.10, 0.95) is None  # confidently worse: never
    big = pcell(90, 30, 30, 90)
    assert required_pairs(big, 0.10, 0.95) == big.n  # already resolved: n itself


def _result(path: str = "m6-s3-d2-development.json") -> ExperimentResult:

    root = Path(__file__).resolve().parents[2] / "docs" / "evaluations" / "data" / path
    return ExperimentResult.model_validate_json(root.read_text())


def test_paired_tasks_from_a_recorded_experiment_with_provenance() -> None:
    tasks, src = paired_tasks(_result(), name="d2", families=["timeout"])
    assert src.n_pairs == len(tasks) == 60 and src.splits == ["HIDDEN_TRANSFER", "VALIDATION"]
    assert src.arms == ["A", "E"] and src.model == "google/gemini-2.5-flash"
    assert src.validation_tokens == sum(tok for t in tasks for _, tok in t.outcomes.values())
    assert src.capabilities == ["timeout.5ca1e00002@1"] and len(src.tasks_sha256) == 64
    assert src.dataset_hashes["VALIDATION"] and src.runtime_version and src.verifier_version
    again = paired_tasks(_result(), name="d2", families=["timeout"])[1]
    assert again.tasks_sha256 == src.tasks_sha256  # content hash is deterministic


def test_priors_refuse_discovery_data_and_collapse_repetitions_by_majority() -> None:
    res = _result()
    rows = [r for r in res.rows if r.family == "timeout"]
    bad = res.model_copy(
        update={"rows": [rows[0].model_copy(update={"split": "DISCOVERY"}), *rows[1:]]}
    )
    with pytest.raises(ValueError, match="held-out splits"):
        paired_tasks(bad, name="x", families=["timeout"])
    # three repetitions of every task: A succeeds in 2 of 3, E in 1 of 3 -> majority outcomes
    out = []
    for r in rows:
        for rep, ok in enumerate((True, True, False) if r.arm == "A" else (True, False, False)):
            out.append(
                r.model_copy(
                    update={
                        "rep": rep,
                        "verified": ok,
                        "status": "VERIFIED_SUCCESS" if ok else "FAILURE",
                    }
                )
            )
    tasks, src = paired_tasks(res.model_copy(update={"rows": out}), name="r", families=["timeout"])
    assert all(
        t.outcomes[PLANNING_KEY][0].value == "VERIFIED_SUCCESS"
        and t.outcomes[t.default_key][0].value == "FAILURE"
        for t in tasks
    )
    assert src.n_pairs == 60 and src.n_tied_dropped == 0
    # a 1-1 split between two known repetitions is a tie: the task is dropped and counted
    tie = [
        r.model_copy(
            update={"rep": rep, "verified": ok, "status": "VERIFIED_SUCCESS" if ok else "FAILURE"}
        )
        for r in rows
        for rep, ok in enumerate((True, False))
    ]
    t2, s2 = paired_tasks(res.model_copy(update={"rows": tie}), name="t", families=["timeout"])
    assert t2 == [] and s2.n_tied_dropped == 60
    # UNKNOWN is never a success or a failure
    unk = [r.model_copy(update={"status": "UNKNOWN"}) if r.arm == "E" else r for r in rows]
    t3, s3 = paired_tasks(res.model_copy(update={"rows": unk}), name="u", families=["timeout"])
    assert t3 == [] and s3.n_unknown_dropped == 60


def test_version_problems_block_a_stale_source() -> None:
    s = source()
    assert version_problems(s, "rt", "v") == []
    msgs = version_problems(s, "rt2", "v2")
    assert len(msgs) == 2 and "runtime" in msgs[0] and "verifier" in msgs[1]


# ---- the policy: tier P (paired prior) ----------------------------------------------------------
def _decide(
    defn: ControllerPolicyDefinition, *, risk: str = "low", family: str = "pool", i: int = 0
) -> tuple[str, "object"]:
    d = AdaptiveController(defn).decide(ctx(task(risk, family), match_with(CAP), f"e-{i}"))
    ref = d.feature_snapshot.get(SELECTED_CAPABILITY_KEY)
    key = (
        PLANNING_KEY
        if d.selected_action == Strategy.GENERAL_PLANNING
        else f"DIRECT_CAPABILITY:{ref}"
    )
    return key, d


def test_stale_default_is_replaced_by_a_paired_prior_without_any_exploration() -> None:
    # 240 pairs, equal success (50% each), 25% discordant; planning is much cheaper
    defn = evidence_definition(
        pcell(90, 30, 30, 90, dt=8900, at=6000),
        kind=ControllerKind.EVIDENCE_DRIVEN,
        exploration_risk_levels=["low"],
        exploration_families=["pool"],
        epsilon=0.5,
        live={CAP_KEY: (200, 100, 200 * 8900)},
        min_total_verified=0,
    )
    for i in range(300):
        key, d = _decide(defn, i=i)
        assert (
            key == PLANNING_KEY and d.selection_probability == 1.0
        )  # deterministic: no exploration
    assert "paired n=240" in d.reason and d.estimates[f"tier_paired:{PLANNING_KEY}"] == 1.0
    assert (
        d.estimates[f"lb:{PLANNING_KEY}"] > -0.10 and d.estimates[f"cps_ratio:{PLANNING_KEY}"] < 0.9
    )
    assert not any(k.startswith("pi:") for k in d.estimates)


def test_a_prior_that_shows_the_alternative_is_worse_keeps_the_rule_default() -> None:
    # rule is right: planning 23% vs capability 100%, 60 pairs
    defn = evidence_definition(
        pcell(14, 46, 0, 0, dt=2000, at=5000),
        kind=ControllerKind.EVIDENCE_DRIVEN,
        exploration_risk_levels=["low"],
        exploration_families=["pool"],
        epsilon=0.5,
        live={CAP_KEY: (200, 190, 200 * 2000)},
        min_total_verified=0,
    )
    for i in range(200):
        key, d = _decide(defn, i=i)
        assert key == CAP_KEY and d.selection_probability == 1.0
    assert "confidently worse" in d.reason and d.estimates[f"ub:{PLANNING_KEY}"] < -0.10


def test_cheaper_but_worse_is_never_chosen_even_with_a_large_prior() -> None:
    # planning is 4x cheaper but loses 20 points: tokens must not buy correctness
    defn = evidence_definition(pcell(100, 70, 10, 60, dt=9000, at=2000))
    key, d = _decide(defn)
    assert key == CAP_KEY and "confidently worse" in d.reason


def test_unresolved_paired_evidence_keeps_the_default_and_reports_what_is_needed() -> None:
    defn = evidence_definition(
        pcell(23, 7, 7, 23),
        kind=ControllerKind.EVIDENCE_DRIVEN,
        exploration_risk_levels=["low"],
        exploration_families=["pool"],
        epsilon=0.5,
        live={CAP_KEY: (200, 100, 200 * 8000)},
        min_total_verified=0,
    )
    for i in range(200):
        key, d = _decide(defn, i=i)
        assert key == CAP_KEY and d.selection_probability == 1.0  # tier P never explores online
    need = d.estimates[f"req_pairs:{PLANNING_KEY}"]
    assert 60 < need < 100 and "needs about" in d.reason


def test_a_prior_below_the_minimum_is_not_used() -> None:
    defn = evidence_definition(pcell(9, 1, 1, 9))  # 20 pairs < n_min_pairs
    key, d = _decide(defn)
    assert key == CAP_KEY and "no usable evidence" in d.reason
    lenient = evidence_definition(pcell(9, 1, 1, 9), evidence={"n_min_pairs": 10})
    assert f"lb:{PLANNING_KEY}" in _decide(lenient)[1].estimates  # the floor is a parameter


def test_the_saving_threshold_applies_to_paired_evidence_too() -> None:
    near = evidence_definition(pcell(90, 30, 30, 90, dt=8000, at=7700))  # ~4% cheaper
    assert _decide(near)[0] == CAP_KEY
    cheap = evidence_definition(pcell(90, 30, 30, 90, dt=8000, at=7000))
    assert _decide(cheap)[0] == PLANNING_KEY


def test_protected_classes_never_read_priors() -> None:
    cell = pcell(90, 30, 30, 90, dt=9000, at=3000)
    for risk in ("high", "medium"):
        defn = evidence_definition(
            cell,
            risk=risk,
            kind=ControllerKind.EVIDENCE_DRIVEN,
            exploration_risk_levels=["low"],
            exploration_families=["pool"],
            epsilon=0.5,
        )
        key, d = _decide(defn, risk=risk)
        f = DecisionFeatures.model_validate(d.feature_snapshot[CONTROLLER_KEY])
        assert key == CAP_KEY and f.protected and f.paired == [] and d.estimates == {}
    other = evidence_definition(cell)  # a foreign tenant's task gets the rule decision as well
    d = AdaptiveController(other).decide(ctx(task("low", "pool", tenant="other"), match_with(CAP)))
    assert d.selected_action == Strategy.DIRECT_CAPABILITY


# ---- drift guard --------------------------------------------------------------------------------
def test_drift_guard_returns_to_the_default_when_live_outcomes_contradict_the_prior() -> None:
    cell = pcell(90, 30, 30, 90, dt=9000, at=3000)  # prior: planning 50%, capability 50%
    healthy = evidence_definition(
        cell, live={PLANNING_KEY: (40, 20, 40 * 3000), CAP_KEY: (10, 5, 10 * 9000)}
    )
    assert _decide(healthy)[0] == PLANNING_KEY
    collapsed = evidence_definition(
        cell, live={PLANNING_KEY: (40, 4, 40 * 3000), CAP_KEY: (10, 5, 10 * 9000)}
    )
    key, d = _decide(collapsed)
    assert (
        key == CAP_KEY and d.estimates[f"drift:{PLANNING_KEY}"] == 1.0 and "invalidated" in d.reason
    )
    # the guard needs evidence: 10 bad live outcomes are not enough to distrust the prior
    few = evidence_definition(cell, live={PLANNING_KEY: (10, 1, 10 * 3000)})
    assert _decide(few)[0] == PLANNING_KEY
    # the default has become much better than validation said: planning's margin is gone
    better = evidence_definition(
        cell, live={PLANNING_KEY: (5, 2, 5 * 3000), CAP_KEY: (60, 58, 60 * 9000)}
    )
    key2, d2 = _decide(better)
    assert key2 == CAP_KEY and "default live success" in d2.reason


# ---- evidence-driven exploration (tier L) -------------------------------------------------------


def _live(planning: tuple[int, int, int] | None, **kw: object) -> ControllerPolicyDefinition:
    live = {CAP_KEY: (200, 170, 200 * 6000)}
    if planning is not None:
        live[PLANNING_KEY] = planning
    opts: dict[str, object] = {
        "kind": ControllerKind.EVIDENCE_DRIVEN,
        "exploration_risk_levels": ["low"],
        "exploration_families": ["pool"],
        "epsilon": 0.2,
        "min_total_verified": 100,
    }
    opts.update(kw)
    return evidence_definition(None, live=live, **opts)  # type: ignore[arg-type]


def _probs(defn: ControllerPolicyDefinition, n: int = 400) -> dict[str, float]:
    seen: dict[str, float] = {}
    for i in range(n):
        key, d = _decide(defn, i=i)
        seen[key] = d.selection_probability or 0.0
    return seen


def test_unresolved_plausible_alternative_is_explored_with_exact_propensities() -> None:
    # planning looks as good and much cheaper but has only 10 trials: unresolved and worth a look
    seen = _probs(_live((10, 9, 10 * 2000)))
    assert set(seen) == {PLANNING_KEY, CAP_KEY}
    assert seen[PLANNING_KEY] == pytest.approx(0.1) and seen[CAP_KEY] == pytest.approx(0.9)
    counts = {PLANNING_KEY: 0, CAP_KEY: 0}
    for i in range(3000):
        counts[_decide(_live((10, 9, 10 * 2000)), i=i)[0]] += 1
    assert counts[PLANNING_KEY] / 3000 == pytest.approx(0.1, abs=0.02)


@pytest.mark.parametrize(
    "planning, why",
    [
        ((300, 60, 300 * 2000), "resolved: confidently worse"),
        (
            (300, 255, 300 * 2000),
            "resolved: non-inferior and cheaper -> chosen, nothing to explore",
        ),
        ((30, 21, 30 * 20000), "hopeless: even optimistically it cannot save enough"),
    ],
)
def test_exploration_stops_once_resolved_or_hopeless(
    planning: tuple[int, int, int], why: str
) -> None:
    seen = _probs(_live(planning))
    assert len(seen) == 1 and next(iter(seen.values())) == 1.0, why


def test_exploration_budget_is_bounded_by_the_evidence_requirement() -> None:
    open_ = _probs(_live((60, 50, 60 * 2000)))
    assert PLANNING_KEY in open_  # the independent bound could still be resolved within the cap
    capped = _probs(_live((60, 50, 60 * 2000), evidence={"explore_target_cap": 100}))
    assert set(capped) == {
        CAP_KEY
    }  # reaching the margin needs more than the cap: stop, don't bleed


def test_exploration_gates_match_the_bandit_gates() -> None:
    base = (10, 9, 10 * 2000)
    for over in (
        {"calibrated": False},
        {"epsilon": 0.0},
        {"exploration_families": []},
        {"exploration_risk_levels": []},
        {"min_total_verified": 10_000},
    ):
        seen = _probs(_live(base, **over), 200)
        assert set(seen) == {CAP_KEY} and seen[CAP_KEY] == 1.0, over
    cost_aware = _live(base, kind=ControllerKind.COST_AWARE, evidence={})
    key, d = _decide(cost_aware)
    assert key == CAP_KEY and d.selection_probability == 1.0
    f = DecisionFeatures.model_validate(d.feature_snapshot[CONTROLLER_KEY])
    assert f.u is None  # no draw is even made


def test_decisions_with_exploration_and_priors_replay_exactly() -> None:
    explore = _live((10, 9, 10 * 2000))
    prior = evidence_definition(pcell(90, 30, 30, 90, dt=8900, at=6000))
    seen = set()
    for defn in (explore, prior):
        c = AdaptiveController(defn)
        for i in range(300):
            d = c.decide(ctx(task(), match_with(CAP), f"r-{i}"))
            seen.add(d.selected_action)
            check = replay_decision(d, defn)
            assert check.reproducible, check.differences
    assert seen == {Strategy.DIRECT_CAPABILITY, Strategy.GENERAL_PLANNING}


def test_replay_detects_a_tampered_or_swapped_paired_prior() -> None:
    defn = evidence_definition(pcell(90, 30, 30, 90, dt=8900, at=6000))
    d = AdaptiveController(defn).decide(ctx(task(), match_with(CAP), "t-1"))
    assert replay_decision(d, defn).reproducible
    swapped = evidence_definition(pcell(90, 30, 31, 89, dt=8900, at=6000))
    check = replay_decision(d, swapped)
    assert not check.reproducible and any("paired prior" in x for x in check.differences)
    plain = definition(snapshot_of({}))
    assert not replay_decision(d, plain).reproducible  # not even the same policy version contents


# ---- full prequential simulation with priors (synthetic worlds, ground truth known) -------------
RATES_STALE = {PLANNING_KEY: 0.55, CAP_KEY: 0.55}
COST_STALE = {PLANNING_KEY: 5000, CAP_KEY: 9000}


def _cfg(kind: ControllerKind, prior=None, evidence=None, **params):  # type: ignore[no-untyped-def]
    p = ControllerParams.model_validate(
        {
            "evidence": (evidence or {}) if kind != ControllerKind.BANDIT else None,
            "exploration_risk_levels": ["low"],
            "exploration_families": ["pool"],
            "epsilon": 0.1,
            **params,
        }
    )
    return SimConfig(kind, p, prior=prior, refresh_every=10)


def test_stale_default_prior_resolves_immediately_with_zero_exploration() -> None:
    validation = correlated_world(240, RATES_STALE, COST_STALE, seed=1, prefix="v")
    held_out = correlated_world(300, RATES_STALE, COST_STALE, seed=2, prefix="h")
    prior = prior_of(validation)
    for kind in (ControllerKind.COST_AWARE, ControllerKind.EVIDENCE_DRIVEN):
        steps = run_sequence(held_out, list(range(300)), _cfg(kind, prior), label="s")
        assert sum(s.deviated for s in steps) / 300 > 0.95 and not any(s.explored for s in steps)
        cmp = compare([steps])
        fam = cmp.family_results["pool"]
        assert fam.verdict_cell == Verdict.BEATS and fam.exploration_rate == 0.0
        assert fam.cost_ratio[2] is not None and fam.cost_ratio[2] < 1.0


def test_rule_is_right_prior_keeps_the_default_with_no_deviation_or_exploration() -> None:
    rates, cost = {PLANNING_KEY: 0.25, CAP_KEY: 0.95}, {PLANNING_KEY: 5000, CAP_KEY: 2000}
    prior = prior_of(correlated_world(60, rates, cost, seed=3, prefix="v"))
    held_out = correlated_world(300, rates, cost, seed=4, prefix="h")
    for kind in (ControllerKind.COST_AWARE, ControllerKind.EVIDENCE_DRIVEN):
        steps = run_sequence(held_out, list(range(300)), _cfg(kind, prior), label="r")
        assert not any(s.deviated or s.explored for s in steps)
        assert compare([steps]).family_results["pool"].verdict_cell == Verdict.EQUAL


def test_a_small_prior_cannot_resolve_the_stale_case_and_the_default_stays() -> None:
    validation = correlated_world(240, RATES_STALE, COST_STALE, seed=1, prefix="v")
    held_out = correlated_world(200, RATES_STALE, COST_STALE, seed=2, prefix="h")
    small = prior_of(subsample(validation, 40))
    steps = run_sequence(
        held_out, list(range(200)), _cfg(ControllerKind.EVIDENCE_DRIVEN, small), label="s"
    )
    assert not any(
        s.deviated for s in steps
    )  # unresolved at 40 pairs: safe, and no online exploration
    big = run_sequence(
        held_out,
        list(range(200)),
        _cfg(ControllerKind.EVIDENCE_DRIVEN, prior_of(validation)),
        label="b",
    )
    assert sum(s.deviated for s in big) > 180  # the same world resolves with 240 pairs


def test_cold_evidence_driven_explores_far_less_than_the_bandit_and_then_stops() -> None:
    world = correlated_world(1500, RATES_STALE, COST_STALE, seed=5, rho=0.0, prefix="w")
    warm = [
        record(i, CAP_KEY, i % 2 == 0, 9000) for i in range(120)
    ]  # a rule-only log: opens the calibration / data gate for both
    order = list(range(1500))
    ev = run_sequence(
        world, order, _cfg(ControllerKind.EVIDENCE_DRIVEN, min_total_verified=100), warm, "ev"
    )
    bandit = run_sequence(
        world, order, _cfg(ControllerKind.BANDIT, min_total_verified=100), warm, "bd"
    )
    n_ev, n_bd = sum(s.explored for s in ev), sum(s.explored for s in bandit)
    assert 0 < n_ev < 0.6 * n_bd, (n_ev, n_bd)
    assert not any(s.explored for s in ev[900:])  # resolved or at target: exploration has stopped
    assert sum(s.chosen == PLANNING_KEY for s in ev[1000:]) / 500 > 0.9  # and it switched


def test_drift_guard_in_simulation_recovers_from_a_collapsed_alternative() -> None:
    validation = correlated_world(240, RATES_STALE, COST_STALE, seed=1, prefix="v")
    held_out = correlated_world(
        600,
        RATES_STALE,
        COST_STALE,
        seed=2,
        prefix="h",
        degrade_planning_at=150,
        degraded_rate=0.05,
    )
    prior = prior_of(validation)
    steps = run_sequence(
        held_out, list(range(600)), _cfg(ControllerKind.COST_AWARE, prior), label="d"
    )
    early = sum(s.chosen == PLANNING_KEY for s in steps[:140]) / 140
    late = sum(s.chosen == CAP_KEY for s in steps[400:]) / 200
    assert early > 0.9 and late > 0.9  # used the prior, then the guard sent it back to the default
    off = {"drift_min_n": 1_000_000}  # the same policy with the guard switched off
    unguarded = run_sequence(
        held_out, list(range(600)), _cfg(ControllerKind.COST_AWARE, prior, evidence=off), label="u"
    )
    assert (
        sum(s.chosen == PLANNING_KEY for s in unguarded[400:]) / 200 > 0.95
    )  # trusts a stale prior
    kept = sum(int(s.success) for s in steps[200:])
    trusted = sum(int(s.success) for s in unguarded[200:])
    assert (
        kept > trusted
    )  # the guarded run kept more verified successes than trusting the stale prior
