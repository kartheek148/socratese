"""The frozen M6.3 verdict rule, evaluated on synthetic results that force each branch."""

import json
from pathlib import Path
from typing import Any

import pytest

from experience_engine.controller.reval_eval import RevalResult, result_to_json
from experience_engine.revalidation_report import STRATEGIES, evaluate, load, report

N = 100
HOLD = ("S2", "S4", "R1", "R2")
RESTORE = ("S5", "R3", "R4", "S8", "S8b")


def stream(
    sid: str,
    variant: str,
    i: int,
    *,
    restored: bool = False,
    false: bool = False,
    util: float = 0.0,
    reval_tokens: int = 0,
    unverified: int = 0,
) -> RevalResult:
    tokens = 800 * 4000
    successes = round(util + (tokens + reval_tokens) / 20_000)
    return RevalResult(
        sid=sid, seed=i, variant=variant, expect="restore" if sid in RESTORE else "hold", n=800,
        successes=successes, tokens=tokens, rule_successes=400, rule_tokens=tokens,
        reval_tokens=reval_tokens, restorations=[500] if restored else [],
        false_restorations=int(false), unverified_evidence_restorations=unverified,
    )  # fmt: skip


def world(per: dict[Any, Any]) -> dict[str, dict[str, list[RevalResult]]]:
    """`per[variant]` = dict of keyword defaults for every stream of that variant; an entry
    `per[(variant, sid)]` overrides one scenario; callables are given the stream index."""
    by: dict[str, dict[str, list[RevalResult]]] = {}
    for sid in (*HOLD, *RESTORE):
        by[sid] = {}
        for v in ("m61", "m62", "m62_locked", *STRATEGIES):
            kw = {**per.get(v, {}), **per.get((v, sid), {})}
            by[sid][v] = [
                stream(sid, v, i, **{k: (x(i) if callable(x) else x) for k, x in kw.items()})
                for i in range(N)
            ]
    return by


GOOD = dict(restored=True, util=10.0, reval_tokens=100_000)
BASE = {"m62": dict(restored=lambda i: i < 40, util=0.0), "m62_locked": dict(util=-5.0)}


def good_world(extra: dict[Any, Any] | None = None) -> dict[str, dict[str, list[RevalResult]]]:
    per: dict[Any, Any] = {**BASE, **{s: dict(GOOD) for s in STRATEGIES}}
    for sid in HOLD:  # nothing is restored where it must not be
        for s in STRATEGIES:
            per[(s, sid)] = dict(restored=False, util=0.0, reval_tokens=100_000)
    per.update(extra or {})
    return world(per)


def test_a_strategy_that_meets_every_criterion_gives_a_supported_verdict() -> None:
    ev = evaluate(good_world())
    assert ev["verdict"] == "SUPPORTED"
    assert all(all(ev["strategies"][s][k]["ok"] for k in "abcde") for s in STRATEGIES)
    assert ev["candidate"] in STRATEGIES


def test_a_false_restoration_beyond_the_bound_fails_safety_for_that_strategy_only() -> None:
    w = good_world({("m63_A", "R2"): dict(restored=True, false=lambda i: i < 20, reval_tokens=1)})
    ev = evaluate(w)
    assert not ev["strategies"]["m63_A"]["a"]["ok"] and ev["strategies"]["m63_B"]["a"]["ok"]
    assert ev["verdict"] == "SUPPORTED"  # another strategy still meets everything
    assert ev["candidate"] != "m63_A"


def test_the_verdict_is_not_supported_when_safety_fails_for_every_strategy() -> None:
    bad = {(s, "R1"): dict(restored=True, false=True, reval_tokens=1) for s in STRATEGIES}
    assert evaluate(good_world(bad))["verdict"] == "NOT SUPPORTED"


def test_the_verdict_is_not_supported_when_nothing_beats_native_recovery_on_s5() -> None:
    weak = {(s, "S5"): dict(restored=lambda i: i < 40, util=10.0, reval_tokens=100_000)
            for s in STRATEGIES}  # fmt: skip
    ev = evaluate(good_world(weak))
    assert ev["best_s5_restored"] == ev["m62_s5_restored"] == 40
    assert ev["verdict"] == "NOT SUPPORTED"


def test_safe_and_helpful_but_uneconomical_is_only_partially_supported() -> None:
    costly = {s: dict(restored=True, util=-30.0, reval_tokens=2_000_000) for s in STRATEGIES}
    ev = evaluate(good_world(costly))
    assert all(not ev["strategies"][s]["e"]["ok"] for s in STRATEGIES)
    assert ev["verdict"] == "PARTIALLY SUPPORTED"


def test_a_restoration_resting_on_unverified_trials_blocks_every_positive_verdict() -> None:
    tainted = {(s, "R4"): dict(restored=True, util=10.0, reval_tokens=1, unverified=1)
               for s in STRATEGIES}  # fmt: skip
    ev = evaluate(good_world(tainted))
    assert all(not ev["strategies"][s]["d"]["ok"] for s in STRATEGIES)
    assert ev["verdict"] in ("INCONCLUSIVE", "NOT SUPPORTED")
    assert ev["candidate"] is None


def test_safe_but_barely_restoring_is_inconclusive_not_supported() -> None:
    few = {s: dict(restored=lambda i: i < 50, util=1.0, reval_tokens=100_000) for s in STRATEGIES}
    ev = evaluate(good_world(few))
    assert not any(
        ev["strategies"][s]["b"]["ok"] or ev["strategies"][s]["c"]["ok"] for s in STRATEGIES
    )
    assert ev["verdict"] == "INCONCLUSIVE"


def test_sequential_saving_needs_a_real_ratio_and_safe_c() -> None:
    cheap: dict[Any, Any] = {}
    for sid in (*HOLD, *RESTORE):
        restore = sid in RESTORE
        cheap[("m63_C", sid)] = dict(restored=restore, util=10.0 if restore else 0.0,
                                     reval_tokens=50_000)  # fmt: skip
        cheap[("m63_A", sid)] = dict(cheap[("m63_C", sid)], reval_tokens=100_000)
    ev = evaluate(good_world(cheap))
    assert ev["f"]["ratio"][0] == pytest.approx(0.5) and ev["f"]["ok"]
    same = evaluate(good_world())
    assert same["f"]["ratio"][0] == pytest.approx(1.0) and not same["f"]["ok"]


def test_the_rendered_report_states_every_criterion_and_the_verdict(tmp_path: Path) -> None:
    by = good_world()
    path = tmp_path / "r.json"
    path.write_text(
        json.dumps([result_to_json(r) for s in by.values() for v in s.values() for r in v])
    )
    repro = tmp_path / "r2.json"
    repro.write_text(path.read_text())
    text = report(str(path), str(repro), "test")
    for needle in ("(a)", "(b)", "(c)", "(d)", "(e)", "(f)", "(g)", "(h)", "Verdict", "SUPPORTED",
                   "byte-identical: **yes**", "Break-even horizon"):  # fmt: skip
        assert needle in text
    repro.write_text(path.read_text() + " ")
    assert "byte-identical: **NO**" in report(str(path), str(repro))
    assert set(load(str(path))) == set(by)
