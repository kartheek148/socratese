"""M6.2-A: final-outcome evidence from evaluation rows and the production-eligibility gates."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from experience_engine.controller.evidence_quality import (
    EvidenceFacts,
    ServingContext,
    assess_evidence,
    facts_from_source,
    source_and_cell,
)
from experience_engine.domain.controller import PLANNING_KEY, PairedCell, PairedSource, paired_key
from experience_engine.domain.evaluation import Arm, ServingEvidence, Suite, TaskResult
from experience_engine.domain.evidence import EvaluationRecord, EvidenceKind
from experience_engine.evaluation.serving_evidence import build_serving_evidence

REF = "pool.0123456789@1"
DEFAULT = f"DIRECT_CAPABILITY:{REF}"
KEY = paired_key("pool", "low", DEFAULT, PLANNING_KEY)
NOW = datetime(2026, 3, 1, tzinfo=UTC)


def row(
    suite: Suite,
    arm: Arm,
    task: str,
    ok: bool | None,
    seed: int = 0,
    tokens: int = 100,
    used: bool = True,
    ref: str = REF,
    family: str = "pool",
) -> TaskResult:
    status = "UNKNOWN" if ok is None else ("VERIFIED_SUCCESS" if ok else "FAILURE")
    serving = arm == Arm.SERVING
    return TaskResult(
        suite=suite,
        arm=arm,
        task_id=task,
        family=family,
        variant="v",
        status=status,
        tokens=tokens,
        tool_calls=1,
        latency_ms=1,
        seed=seed,
        capability_used=used if serving else None,
        asset_ref=ref if serving else None,
        fell_back=False if serving else None,
    )


def pair(
    task: str,
    a: bool | None,
    e: bool | None,
    suite: Suite = Suite.HELD_OUT,
    seeds: tuple[int, ...] = (0,),
    **kw: Any,
) -> list[TaskResult]:
    out = []
    for s in seeds:
        out.append(row(suite, Arm.BASELINE, task, a, s, 80))
        out.append(row(suite, Arm.SERVING, task, e, s, 120, **kw))
    return out


def build(rows: list[TaskResult]) -> ServingEvidence:
    return build_serving_evidence(rows, capability_ref=REF, family="pool", risk_class="low")


# ---- building the paired table ------------------------------------------------------------------
def test_pairs_count_the_planner_as_the_alternative_and_the_served_capability_as_default() -> None:
    rows = (
        pair("t1", True, True)
        + pair("t2", True, False)  # planner wins: capability failed
        + pair("t3", False, True)
        + pair("t4", False, False)  # capability wins
        + pair("t5", True, True, suite=Suite.TRANSFER)
    )
    ev = build(rows)
    assert (ev.both, ev.alt_only, ev.default_only, ev.neither, ev.n) == (2, 1, 1, 1, 5)
    assert ev.default_tokens == 5 * 120 and ev.alt_tokens == 5 * 80
    assert ev.suites == ["HELD_OUT", "TRANSFER"] and ev.catalog == "EVALUATION_STATIC_CATALOG"
    assert ev.n_runs == 5 and ev.serving_tokens == 5 * 120 and len(ev.tasks_sha256) == 64


def test_source_replay_other_families_and_unpaired_rows_never_count() -> None:
    rows = pair("replay", True, True, suite=Suite.SOURCE_REPLAY) + pair("o", True, True)
    rows += [r.model_copy(update={"family": "timeout"}) for r in pair("x", True, True)]
    rows += [row(Suite.HELD_OUT, Arm.BASELINE, "lonely", True)]  # no serving row
    ev = build(rows)
    assert ev.n == 1  # only the pool held-out pair


def test_repetitions_collapse_by_majority_and_ties_and_unknowns_are_dropped_and_counted() -> None:
    rows = pair("maj", True, False, seeds=(0, 1, 2))  # planner 3/3, capability 0/3 -> planner only
    rows += [row(Suite.HELD_OUT, Arm.BASELINE, "tie", s == 0, s) for s in (0, 1)]
    rows += [row(Suite.HELD_OUT, Arm.SERVING, "tie", True, s) for s in (0, 1)]  # 1-1 vs 2-0
    rows += pair("unk", None, True)  # the planner run is UNKNOWN: never a failure
    ev = build(rows)
    assert (ev.alt_only, ev.n, ev.n_tied_dropped, ev.n_unknown_dropped) == (1, 1, 1, 1)


def test_pairs_where_the_capability_was_not_the_one_served_are_not_this_comparison() -> None:
    rows = pair("ok", True, True) + pair("fell", True, True, used=False)
    rows += pair("other", True, True, ref="pool.other@3")
    ev = build(rows)
    assert ev.n == 1 and ev.n_not_offered == 2


def test_duplicate_rows_and_missing_costs_are_counted() -> None:
    rows = pair("a", True, True) + pair("a", True, True)  # the same (suite, arm, task, seed) twice
    rows += [
        row(Suite.HELD_OUT, Arm.BASELINE, "z", True, tokens=0),
        row(Suite.HELD_OUT, Arm.SERVING, "z", True),
    ]
    ev = build(rows)
    assert ev.n_duplicate_rows == 2 and ev.n_missing_cost == 1


def test_the_serving_evidence_field_is_omitted_when_absent_so_old_reports_are_unchanged() -> None:
    r = row(Suite.HELD_OUT, Arm.BASELINE, "t", True)
    assert "capability_used" not in r.model_dump(mode="json")
    assert "asset_ref" not in r.model_dump(mode="json")
    assert "capability_used" in row(Suite.HELD_OUT, Arm.SERVING, "t", True).model_dump(mode="json")


# ---- eligibility gates --------------------------------------------------------------------------
GOOD = PairedCell(
    n=60,
    both=20,
    default_only=12,
    alt_only=8,
    neither=20,
    default_tokens=600_000,
    alt_tokens=300_000,
)
CTX = ServingContext(
    tenant_id="acme",
    runtime_version="rt1",
    verifier_version="v1",
    environment_version="e1",
    n_min_pairs=30,
    now=NOW,
)


def facts(**over: Any) -> EvidenceFacts:
    base: dict[str, Any] = dict(
        kind=EvidenceKind.PROMOTION_EVALUATION.value,
        tenant_id="acme",
        risk_class="low",
        splits=["HELD_OUT", "TRANSFER"],
        runtime_version="rt1",
        verifier_version="v1",
        environment_version="e1",
        capabilities=[REF],
        n_unknown=1,
        n_tied=1,
        n_missing_cost=0,
        n_duplicate_rows=0,
        evaluation_id="ev-1",
        report_sha256="s",
        config_hash="c",
        verdict="PASS",
        model_provider="vertex",
        recorded_at=NOW - timedelta(days=2),
    )
    base.update(over)
    return EvidenceFacts(**base)


def test_valid_evidence_is_production_eligible_with_a_full_explainable_assessment() -> None:
    a = assess_evidence(KEY, GOOD, facts(), CTX)
    assert a.eligible and a.reasons == [] and a.verified_pair_count == 60
    assert all(a.compatibility.values()) and a.drift_state == "NORMAL"
    assert a.confidence["lower"] < a.confidence["upper"] and a.confidence["width"] > 0
    assert a.provenance["evaluation_id"] == "ev-1" and a.cost_measurement["complete"] == 1.0
    assert a.freshness["age_days"] == pytest.approx(2.0)


@pytest.mark.parametrize(
    ("over", "needle"),
    [
        ({"kind": EvidenceKind.STRESS_FIXTURE.value}, "research-only"),
        ({"kind": EvidenceKind.EXPERIMENT_FILE.value}, "research-only"),
        ({"tenant_id": "other"}, "tenant"),
        ({"risk_class": "medium"}, "risk class"),
        ({"splits": ["DISCOVERY", "HELD_OUT"]}, "held-out only"),
        ({"splits": []}, "held-out only"),
        ({"runtime_version": "old"}, "runtime"),
        ({"verifier_version": "old"}, "verifier"),
        ({"environment_version": "old"}, "environment"),
        ({"capabilities": ["pool.other@9"]}, "capability version"),
        ({"n_unknown": 30}, "UNKNOWN or tied"),
        ({"n_missing_cost": 3}, "lack a token measurement"),
        ({"n_missing_cost": None}, "incomplete or not recorded"),
        ({"n_duplicate_rows": 2}, "duplicate"),
        ({"verdict": "FAIL"}, "verdict was FAIL"),
        ({"model_provider": "fake"}, "synthetic"),
        ({"recorded_ineligible": True}, "synthetic"),
    ],
)
def test_each_gate_rejects_incompatible_evidence_with_its_own_reason(
    over: dict[str, Any], needle: str
) -> None:
    a = assess_evidence(KEY, GOOD, facts(**over), CTX)
    assert not a.eligible and any(needle in r for r in a.reasons), a.reasons


def test_sample_size_freshness_duplicates_across_evaluations_and_drift_state() -> None:
    small = PairedCell(
        n=10, both=5, default_only=0, alt_only=0, neither=5, default_tokens=1, alt_tokens=1
    )
    assert any("verified pairs" in r for r in assess_evidence(KEY, small, facts(), CTX).reasons)
    ctx = ServingContext(**{**CTX.__dict__, "max_age_days": 1.0})
    old = assess_evidence(KEY, GOOD, facts(), ctx)
    assert not old.eligible and old.freshness["fresh"] is False
    assert assess_evidence(KEY, GOOD, facts(recorded_at=NOW - timedelta(hours=1)), ctx).eligible
    twice = assess_evidence(KEY, GOOD, facts(), CTX, seen_evaluations=frozenset({"ev-1"}))
    assert not twice.eligible and any("duplicate" in r for r in twice.reasons)
    for state in ("DRIFTED", "QUARANTINED", "RECOVERING"):
        assert not assess_evidence(KEY, GOOD, facts(), CTX, drift_state=state).eligible
    assert assess_evidence(KEY, GOOD, facts(), CTX, drift_state="SUSPECT").eligible


def test_the_version_override_is_audited_and_never_weakens_another_gate() -> None:
    ctx = ServingContext(**{**CTX.__dict__, "allow_version_mismatch": True})
    a = assess_evidence(KEY, GOOD, facts(runtime_version="old", verifier_version="old2"), ctx)
    assert a.eligible and len(a.overrides) == 2 and "runtime" in a.overrides[0]
    b = assess_evidence(
        KEY, GOOD, facts(runtime_version="old", tenant_id="other", model_provider="fake"), ctx
    )
    assert (
        not b.eligible and b.overrides
    )  # still rejected on tenant and synthetic; override visible
    assert any("tenant" in r for r in b.reasons) and any("synthetic" in r for r in b.reasons)


def test_every_gate_is_evaluated_so_the_reasons_are_complete() -> None:
    bad = facts(
        kind="STRESS_FIXTURE",
        tenant_id="x",
        risk_class="high",
        runtime_version="o",
        n_missing_cost=5,
    )
    assert len(assess_evidence(KEY, GOOD, bad, CTX).reasons) >= 5


# ---- stored evaluation -> prior ----------------------------------------------------------------
def record(provider: str = "vertex", ev: ServingEvidence | None = None) -> EvaluationRecord:
    ev = ev or build(
        [
            r
            for i in range(40)
            for r in pair(
                f"t{i}", i % 2 == 0, i % 3 != 0, suite=Suite.HELD_OUT if i % 2 else Suite.TRANSFER
            )
        ]
    )
    return EvaluationRecord(
        evaluation_id="ev-9",
        tenant_id="acme",
        capability_id="pool.0123456789",
        version=1,
        verdict="PASS",
        risk_class="low",
        runtime_version="rt1",
        verifier_version="v1",
        environment_version="e1",
        evaluator_version="evaluator/1",
        report_sha256="abc",
        completed_at=NOW,
        model_settings={"provider": provider, "model": "m"},
        dataset_versions={
            "VALIDATION": {"content_hash": "h1"},
            "DISCOVERY": {"content_hash": "h0"},
        },
        metrics={"serving_evidence": ev.model_dump(mode="json"), "evaluation_config_hash": "cfg"},
    )


def test_a_stored_evaluation_yields_a_cell_a_source_and_facts_that_pass_the_gates() -> None:
    key, cell, source, f = source_and_cell(record())
    assert key == KEY and cell.n == 40 and source.n_pairs == 40
    assert source.evidence_kind == "PROMOTION_EVALUATION" and source.evaluation_id == "ev-9"
    assert source.splits == ["HELD_OUT", "TRANSFER"] and source.dataset_hashes == {
        "VALIDATION": "h1"
    }  # no discovery hash
    assert source.production_eligible is None and source.recorded_at == NOW
    assert assess_evidence(key, cell, f, CTX).eligible
    again = facts_from_source(source)
    assert again.evaluation_id == "ev-9" and again.capabilities == [REF]


def test_a_synthetic_provider_is_recorded_as_ineligible_at_build_time() -> None:
    _, cell, source, f = source_and_cell(record("fake"))
    assert source.production_eligible is False
    assert not assess_evidence(KEY, cell, f, CTX).eligible


def test_an_evaluation_without_serving_evidence_cannot_build_a_prior() -> None:
    rec = record()
    bare = EvaluationRecord(**{**rec.__dict__, "metrics": {}})
    with pytest.raises(ValueError, match="no serving"):
        source_and_cell(bare)


def test_provenance_fields_do_not_change_an_m61_source_serialisation() -> None:
    s = PairedSource(
        name="s",
        arms=["A"],
        splits=["VALIDATION"],
        dataset_hashes={},
        model="m",
        runtime_version="r",
        verifier_version="v",
        n_pairs=1,
        n_unknown_dropped=0,
        n_tied_dropped=0,
        tasks_sha256="t",
        validation_tokens=1,
    )
    dumped = s.model_dump(mode="json")
    assert not {
        "evidence_kind",
        "production_eligible",
        "tenant_id",
        "overrides",
        "recorded_at",
    } & set(dumped)
